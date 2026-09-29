from __future__ import annotations

import json
from typing import Any, Callable

from .models import StoragePolicy


class CapabilityError(RuntimeError):
    pass


class LocalStorageSaveAdapter:
    """Policy-checked localStorage helper shared by save-restored benchmarks."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def write_many(
        self,
        values: dict[str, str],
        clear_keys: list[str] | None,
        policy: StoragePolicy,
    ) -> list[str]:
        clear = list(clear_keys or [])
        for key in clear:
            if not policy.allows_clear(key):
                raise CapabilityError(f"Storage policy does not allow clearing key: {key}")
        for key in values:
            if not policy.allows_write(key):
                raise CapabilityError(f"Storage policy does not allow overwriting key: {key}")
        if getattr(self.runtime, "page", None) is not None:
            self.runtime.page.evaluate(
                """({ values, clearKeys }) => {
                  for (const key of clearKeys) window.localStorage.removeItem(key);
                  for (const [key, value] of Object.entries(values)) window.localStorage.setItem(key, value);
                }""",
                {"values": values, "clearKeys": clear},
            )
            return clear + list(values)
        return self.runtime.install_storage_init(values, clear_keys=clear)

    def read_json(self, key: str) -> Any:
        raw = self.runtime.page.evaluate("(key) => window.localStorage.getItem(key)", key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise CapabilityError(f"localStorage key is not JSON: {key}") from error

    def memory_vs_persistence(
        self,
        memory: Any,
        key: str,
        summarizer: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        persisted = self.read_json(key)
        summarize = summarizer or (lambda value: value)
        memory_summary = summarize(memory) if memory is not None else None
        persistence_summary = summarize(persisted) if persisted is not None else None
        return {
            "persistence_key": key,
            "memory_present": memory is not None,
            "persistence_present": persisted is not None,
            "matches": json.dumps(memory_summary, sort_keys=True) == json.dumps(persistence_summary, sort_keys=True)
            if memory_summary is not None and persistence_summary is not None
            else None,
            "memory": memory_summary,
            "persistence": persistence_summary,
        }


class TestHookAdapter:
    """Open and call a browser-global test hook without benchmark semantics."""

    __test__ = False

    def __init__(self, runtime: Any, hook_name: str) -> None:
        self.runtime = runtime
        self.hook_name = hook_name

    def open_with_gate(self, path: str = "/", timeout_ms: int = 20_000) -> dict[str, Any]:
        self.runtime.open(path)
        self.runtime.wait_for_hook(self.hook_name, timeout_ms=timeout_ms)
        metadata = self.get_metadata()
        availability: dict[str, Any] = {"available": True, "name": self.hook_name}
        if isinstance(metadata, dict):
            availability.update(
                {
                    "gate": metadata.get("gate"),
                    "mode": metadata.get("mode"),
                    "enabled_by": metadata.get("enabledBy"),
                    "metadata": metadata,
                }
            )
        return availability

    def get_metadata(self) -> dict[str, Any] | None:
        return self.runtime.page.evaluate(
            """(hookName) => {
              const hook = window[hookName];
              return hook?.getMetadata?.() ?? hook?.__warptestGate ?? null;
            }""",
            self.hook_name,
        )

    def get_state(self) -> Any:
        return self.runtime.page.evaluate(
            """(hookName) => {
              const hook = window[hookName];
              if (!hook?.getState) throw new Error(`Hook has no getState: ${hookName}`);
              return hook.getState();
            }""",
            self.hook_name,
        )

    def call(self, method: str, args: list[Any] | None = None, *, state_method: bool = False) -> Any:
        if state_method:
            return self.runtime.page.evaluate(
                """({ hookName, method, args }) => {
                  const hook = window[hookName];
                  const store = hook?.getState?.();
                  const fn = store?.[method];
                  if (typeof fn !== "function") throw new Error(`Unsupported store method: ${method}`);
                  return fn(...(args || []));
                }""",
                {"hookName": self.hook_name, "method": method, "args": args or []},
            )
        return self.runtime.page.evaluate(
            """({ hookName, method, args }) => {
              const hook = window[hookName];
              const fn = hook?.[method];
              if (typeof fn !== "function") throw new Error(`Unsupported hook method: ${method}`);
              return fn(...(args || []));
            }""",
            {"hookName": self.hook_name, "method": method, "args": args or []},
        )


class ZustandHookAdapter:
    """Generic Zustand access routed through an explicit test hook."""

    def __init__(self, hook: TestHookAdapter) -> None:
        self.hook = hook

    def initialize(self, options: dict[str, Any]) -> Any:
        return self.hook.call("initialize", [options])

    def patch_state(self, patch: dict[str, Any]) -> Any:
        return self.hook.call("patchState", [patch])

    def set_state(self, state: dict[str, Any]) -> Any:
        return self.hook.call("setState", [state])

    def save(self) -> Any:
        return self.hook.call("save")

    def load(self) -> Any:
        return self.hook.call("load")

    def perform_action(self, action_id: str) -> Any:
        return self.hook.call("performAction", [action_id])

    def get_state(self) -> Any:
        return self.hook.get_state()


class VanillaDOMAdapter:
    """DOM and keyboard helpers for vanilla-browser games such as 2048."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def wait_for_tiles(self, timeout_ms: int = 10_000) -> None:
        self.runtime.page.wait_for_selector(".tile-container .tile", timeout=timeout_ms)

    def score(self) -> int:
        raw = self.runtime.page.locator(".score-container").inner_text(timeout=5_000)
        first = raw.strip().splitlines()[0] if raw.strip() else "0"
        return int(first)

    def dom_tiles(self) -> list[dict[str, Any]]:
        return self.runtime.page.evaluate(
            """() => [...document.querySelectorAll('.tile')].map((tile) => ({
              text: tile.textContent.trim(),
              cls: tile.getAttribute('class')
            }))"""
        )

    def press_move(self, direction: str, wait_ms: int = 350) -> None:
        key = {"left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown"}[direction]
        self.runtime.page.keyboard.press(key)
        self.runtime.page.wait_for_timeout(wait_ms)

    def restart(self) -> None:
        self.runtime.page.keyboard.press("r")


class ThreeCanvasAdapter:
    """Coarse browser-side readiness probe for WebGL/canvas games."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def wait_ready(self, timeout_ms: int = 20_000) -> dict[str, Any]:
        page = self.runtime.page
        page.wait_for_function(
            """() => {
              const canvas = document.querySelector('canvas');
              if (!canvas) return false;
              const rect = canvas.getBoundingClientRect();
              return rect.width > 32 && rect.height > 32;
            }""",
            timeout=timeout_ms,
        )
        before = self.readiness_signal()
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(100)
        after = self.readiness_signal()
        before_heartbeat = before.get("render_heartbeat")
        after_heartbeat = after.get("render_heartbeat")
        after.update(
            {
                "render_heartbeat_before": before_heartbeat,
                "render_heartbeat_after": after_heartbeat,
                "render_heartbeat_changed": before_heartbeat is not None
                and after_heartbeat is not None
                and before_heartbeat != after_heartbeat,
            }
        )
        return after

    def readiness_signal(self) -> dict[str, Any]:
        return self.runtime.page.evaluate(
            """() => {
              const canvas = document.querySelector('canvas');
              const rect = canvas?.getBoundingClientRect();
              const sample = { available: false, nonblank: null, reason: null, method: "toDataURL-length" };
              if (canvas) {
                try {
                  if (rect?.width && rect?.height && typeof canvas.toDataURL === 'function') {
                    const dataUrl = canvas.toDataURL('image/png');
                    sample.available = true;
                    sample.byte_length_estimate = dataUrl.length;
                    sample.nonblank = dataUrl.length > 512;
                  }
                } catch (error) {
                  sample.reason = String(error?.message || error);
                }
              }
              const heartbeat =
                window.__WARPTEST_RENDER_HEARTBEAT__ ??
                window.__SPACERPG_RENDER_HEARTBEAT__ ??
                window.__THREE_RENDER_HEARTBEAT__ ??
                null;
              return {
                present: !!canvas,
                width: rect?.width ?? 0,
                height: rect?.height ?? 0,
                visible: !!canvas && rect.width > 0 && rect.height > 0 && getComputedStyle(canvas).visibility !== 'hidden',
                scene_container_present: !!(
                  document.querySelector('[data-warptest-scene]') ||
                  document.querySelector('[data-testid="scene"]') ||
                  document.querySelector('#root canvas') ||
                  canvas
                ),
                render_heartbeat: heartbeat,
                nonblank_sample: sample
              };
            }"""
        )
