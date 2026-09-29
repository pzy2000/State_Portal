"""Real-cold-start runtime seams for the exploratory P2P-only schedule.

JupyterLab launches the candidate server in dev mode and discovers a federated
benchmark extension from the Launcher.  The extension only opens a
candidate-built task panel; it never invokes the Goal transition.  Luanti and
OpenRA likewise begin at their neutral native application surfaces.  Both arms
must construct Start before the registered transition can run in the same live
lineage.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import shutil
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warptest.native_input import QuartzNativeInput


from .c1_lazarus_selection.p2p_only_frames import (
    CSS_ONLY_STEPS,
    FRAME_SPECS,
    FrameSpec,
)

_OPENRA_ONLINE_START_CS = r"""using System;
using System.IO;
using System.Linq;

namespace OpenRA.Mods.Common.Widgets.Logic
{
	public static class WarptestP2POnlyOnlineStart
	{
		public static void Run(string mode)
		{
			if (mode != "editor-overlays")
				return;
			var control = Environment.GetEnvironmentVariable("WARPTEST_P2P_ONLY_START_CONTROL");
			Action poll = null;
			poll = () =>
			{
				if (string.IsNullOrEmpty(control) || !File.Exists(control))
				{
					Game.RunAfterDelay(250, poll);
					return;
				}
				var map = Game.ModData.MapCache.FirstOrDefault(m =>
					m.Status == MapStatus.Available && m.Visibility.HasFlag(MapVisibility.Lobby));
				if (map == null)
					throw new InvalidOperationException("no lobby map is available for the editor");
				Game.LoadEditor(map.Uid);
			};
			Game.RunAfterDelay(250, poll);
		}
	}
}
"""


_JUPYTER_PIXEL_TARGETS = {
    "lzp-jupyterlab-18996-htmlselect-caret-offset": ".strip button",
    "lzp-jupyterlab-18464-debugger-zero-variable-detail": "#tree-host",
    "lzp-jupyterlab-17222-toolbar-button-pressed-toggle": "#button-host jp-button",
    "lzp-jupyterlab-17153-shortcut-chip-contrast": "#line",
    "lzp-jupyterlab-19091-themed-button-glyph-font": "#host",
    "lzp-jupyterlab-17618-high-contrast-selection-layer": "#line",
}


@dataclass(frozen=True)
class OnlineTransitionEvidence:
    task_id: str
    version: str
    arm: str
    before: Path
    after: Path
    same_live_lineage: bool
    transition_executed: bool
    action: dict[str, Any]
    console: tuple[str, ...]
    initial: Path | None = None
    start_constructed: bool = False
    start_action: dict[str, Any] | None = None

    def to_report(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "arm": self.arm,
            "before": str(self.before),
            "after": str(self.after),
            "initial": str(self.initial) if self.initial is not None else None,
            "initial_sha256": _sha256(self.initial)
            if self.initial is not None
            else None,
            "before_sha256": _sha256(self.before),
            "after_sha256": _sha256(self.after),
            "same_live_lineage": self.same_live_lineage,
            "transition_executed": self.transition_executed,
            "start_constructed": self.start_constructed,
            "start_action": dict(self.start_action or {}),
            "visible_change": _sha256(self.before) != _sha256(self.after),
            "action": self.action,
            "console": list(self.console),
        }


class JupyterOnlineSession:
    """Persistent candidate-built task panel behind a visible Lab-style launcher.

    The launcher is the benchmark extension's public cold-start surface.  It
    never applies the registered Goal transition: it only exposes the target
    panel whose component code and styles were built from the candidate tree.
    """

    width = 1280
    height = 720
    capture_backend = "playwright_chromium_png"
    input_backend = "playwright_page_input_v1"

    def __init__(self, *, tree: Path, output_dir: Path, task_id: str) -> None:
        self.tree = tree.resolve()
        self.output_dir = output_dir.resolve()
        self.task_id = task_id
        self.session_id = uuid.uuid4().hex
        self.last_policy_frame_id = 0
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.server_process = None
        self.server_log = None
        self.server_url = None

    def __enter__(self) -> "JupyterOnlineSession":
        from .c1_lazarus_selection import p2p_only_engine_capture as engine

        spec = FRAME_SPECS[self.task_id]
        selector = _JUPYTER_PIXEL_TARGETS[self.task_id]
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.output_dir.mkdir(parents=True)
        runtime_tree = self.output_dir / "candidate"
        if hasattr(self, "select_runtime_tree"):
            runtime_tree = self.select_runtime_tree(runtime_tree)
        else:
            engine._cow_copy(self.tree, runtime_tree)
        if hasattr(self, "prepare_document"):
            self.prepare_document(runtime_tree, self.output_dir)
        else:
            _prepare_jupyter_document(
                runtime_tree,
                self.output_dir,
                spec,
                self.task_id,
                selector,
                with_launcher=False,
            )
        extension_root = _prepare_jupyter_benchmark_extension(
            runtime_tree, self.output_dir, self.task_id
        )
        self.server_url = _start_jupyterlab_server(
            runtime_tree,
            self.output_dir,
            extension_root,
            owner=self,
        )
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.launch(
                headless=True, executable_path=str(_chromium_executable())
            )
            self.context = self.browser.new_context(
                viewport={"width": self.width, "height": self.height},
                device_scale_factor=1,
            )
            self.page = self.context.new_page()
            self.page.goto(self.server_url + "/lab?reset", wait_until="load")
            self.page.wait_for_selector(".jp-Launcher", timeout=120_000)
            self.page.wait_for_selector("text=Open task workspace", timeout=120_000)
            self.page.wait_for_timeout(500)
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def capture_policy_frame(self, path: str | Path) -> str:
        if self.page is None:
            raise RuntimeError("Jupyter online page is not open")
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(destination))
        self.last_policy_frame_id += 1
        return str(destination)

    def capture_evidence_frame(self, path: str | Path) -> str:
        frame = self._task_frame()
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.locator("body").screenshot(path=str(destination))
        return str(destination)

    def screenshot(self, path: str | Path, full_page: bool = False) -> str:
        del full_page
        return self.capture_policy_frame(path)

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def transition_complete(self) -> bool:
        try:
            return bool(
                self._task_frame().evaluate(
                    "window.__warptestTransitionComplete === true"
                )
            )
        except RuntimeError:
            return False

    def start_complete(self) -> bool:
        return bool(
            self.page is not None
            and self.page.evaluate("window.__warptestStartComplete === true")
        )

    def restore_start(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Jupyter online page is not open")
        result = self.page.evaluate("window.__warptestOpenStart('state_checkpoint')")
        self.page.wait_for_function(
            "window.__warptestStartComplete === true", timeout=30_000
        )
        self._task_frame().wait_for_function(
            "typeof window.__warptestStateTransition === 'function'", timeout=30_000
        )
        self._task_frame().wait_for_function("window.__ready === true", timeout=60_000)
        self.page.wait_for_timeout(300)
        return dict(result) if isinstance(result, dict) else {"opened": True}

    def initial_state_evidence(self) -> dict[str, Any]:
        if self.page is None:
            return {"passed": False, "reason": "page_closed"}
        value = self.page.evaluate(
            """() => ({
              real_jupyterlab: !!document.querySelector('.jp-LabShell'),
              launcher_visible: !!document.querySelector('.jp-Launcher'),
              extension_entry_visible: Array.from(document.querySelectorAll('*')).some(
                node => node.textContent === 'Open task workspace'),
              target_hidden: !document.querySelector('iframe[data-stateportal-p2p-task]')
            })"""
        )
        return {**dict(value), "passed": all(value.values())}

    def start_state_evidence(self) -> dict[str, Any]:
        if self.page is None:
            return {"passed": False, "reason": "page_closed"}
        top = self.page.evaluate(
            """() => ({
              start_complete: window.__warptestStartComplete === true,
              target_visible: !!document.querySelector('iframe[data-stateportal-p2p-task]')
            })"""
        )
        goal_incomplete = not self.transition_complete()
        value = {**dict(top), "goal_incomplete": goal_incomplete}
        return {**value, "passed": all(value.values())}

    def state_transition(self) -> Any:
        return self._task_frame().evaluate("window.__warptestStateTransition()")

    def _task_frame(self) -> Any:
        if self.page is None:
            raise RuntimeError("Jupyter online page is not open")
        for frame in self.page.frames:
            if frame.name == "stateportal-p2p-task":
                # Jupyter keeps inactive tabs mounted. A hidden task iframe is
                # not a live Start and cannot supply an evidence screenshot.
                if frame.frame_element().is_visible():
                    return frame
        raise RuntimeError("Jupyter benchmark extension task frame is not open")

    def close(self) -> None:
        for value, method in (
            (self.context, "close"),
            (self.browser, "close"),
            (self._playwright, "stop"),
        ):
            if value is not None:
                try:
                    getattr(value, method)()
                except Exception:
                    pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None
        process = self.server_process
        self.server_process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if self.server_log is not None:
            try:
                self.server_log.close()
            except Exception:
                pass
        self.server_log = None


class LuantiOnlineSession:
    """Persistent real Luanti main menu for P2P-only pixel policies."""

    capture_backend = "luanti_irrlicht_create_screenshot_v1"
    input_backend = "quartz_native_window_input_v1"

    def __init__(self, *, tree: Path, output_dir: Path, task_id: str) -> None:
        self.tree = tree.resolve()
        self.output_dir = output_dir.resolve()
        self.task_id = task_id
        self.session_id = uuid.uuid4().hex
        self.last_policy_frame_id = 0
        self.process = None
        self.native = None
        self.page = None
        self.start_restore_applied = False
        spec = FRAME_SPECS[task_id]
        self.width, self.height = spec.engine_frame_size
        self._screenshots = self.output_dir / "session/screenshots"

    def __enter__(self) -> "LuantiOnlineSession":
        from .c1_lazarus_selection import p2p_only_engine_capture as engine

        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        session = self.output_dir / "session"
        session.mkdir(parents=True)
        app = session / "luanti.app"
        engine._cow_copy(self.tree / engine._LUANTI_APP, app)
        init = app / "Contents/Resources/builtin/mainmenu/init.lua"
        original = init.read_text(encoding="utf-8")
        if original.count(engine._LUANTI_INJECTION) != 1:
            raise RuntimeError("Luanti online overlay injection point is not unique")
        init.write_text(
            original.replace(
                engine._LUANTI_INJECTION,
                getattr(self, "generated_overlay", engine._LUANTI_OVERLAY),
                1,
            ),
            encoding="utf-8",
        )
        subprocess.run(
            (
                "codesign",
                "--force",
                "--deep",
                "--sign",
                "-",
                "--timestamp=none",
                str(app),
            ),
            check=True,
            capture_output=True,
            timeout=120,
        )
        home = session / "home"
        user = home / "Library/Application Support/minetest"
        user.mkdir(parents=True, exist_ok=True)
        if self.task_id == "lzp-luanti-16799-world-mods":
            world = user / "worlds/p2p_world"
            mod = world / "worldmods/p2p_world_mod"
            mod.mkdir(parents=True, exist_ok=True)
            (world / "world.mt").write_text(
                "gameid = devtest\nbackend = dummy\n", encoding="utf-8"
            )
            (mod / "mod.conf").write_text(
                "name = p2p_world_mod\ndescription = p2p online fixture\n",
                encoding="utf-8",
            )
            (mod / "init.lua").write_text("-- online fixture\n", encoding="utf-8")
        self._screenshots.mkdir(parents=True, exist_ok=True)
        config = session / "minetest.conf"
        config.write_text(
            "\n".join(
                (
                    "video_driver = opengl",
                    f"screen_w = {self.width}",
                    f"screen_h = {self.height}",
                    "fullscreen = false",
                    "enable_sound = false",
                    "enable_update_checker = false",
                    "enable_esc_dialog = false",
                    "language = en",
                    "maintab_LAST = about",
                    "touch_controls = false",
                    "show_advanced = true",
                    f"screenshot_path = {self._screenshots.resolve()}",
                    "screenshot_format = png",
                    "warptest_p2p_mode = luanti-local",
                    "serverlist_url =",
                    "tooltip_show_delay = 0",
                    getattr(self, "generated_config", ""),
                )
            ),
            encoding="utf-8",
        )
        log = session / "luanti.log"
        self.process = subprocess.Popen(
            (
                str((app / "Contents/MacOS/luanti").resolve()),
                "--config",
                str(config.resolve()),
                "--logfile",
                str(log.resolve()),
            ),
            cwd=session,
            env={
                **os.environ,
                "HOME": str(home.resolve()),
                "LANG": "C",
                "LC_ALL": "C",
                "MINETEST_USER_PATH": str(user.resolve()),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if log.is_file() and "WARPTEST_P2P_READY" in log.read_text(
                encoding="utf-8", errors="replace"
            ):
                break
            if self.process.poll() is not None:
                raise RuntimeError("Luanti exited before online Start")
            time.sleep(0.1)
        else:
            raise RuntimeError("timed out waiting for Luanti online Start")
        self.native = QuartzNativeInput(
            self.process.pid, frame_width=self.width, frame_height=self.height
        )
        self.native.focus()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def capture_policy_frame(self, path: str | Path) -> str:
        from .c1_lazarus_selection import p2p_only_engine_capture as engine

        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Luanti online process is not alive")
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # F12 is posted through the global keyboard event tap. A concurrently
        # launched application may have taken focus since session startup.
        # Reacquire and verify this PID immediately before requesting its frame.
        if self.native is None:
            raise RuntimeError("Luanti native input is not initialized")
        def request_capture():
            self.native.focus()
            self.native._press_key(111)

        audit = {"schema": "luanti-passive-capture-v1", "requests": 0,
                 "timeout_s": 20, "retry_interval_s": 4.25,
                 "replays_policy_actions": False}
        try:
            captured = engine._take_luanti_screenshot(
                process=self.process, screenshots=self._screenshots,
                request_key=request_capture, retry_interval_s=4.25,
                capture_audit=audit,
            )
            shutil.copy2(captured, destination)
            audit["complete"] = True
        except Exception:
            audit["complete"] = False
            raise
        finally:
            destination.with_suffix(".capture.json").write_text(json.dumps(audit, sort_keys=True))
        self.last_policy_frame_id += 1
        time.sleep(engine._LUANTI_SCREENSHOT_NOTICE_SETTLE_SECONDS)
        return str(destination)

    def screenshot(self, path: str | Path, full_page: bool = False) -> str:
        del full_page
        return self.capture_policy_frame(path)

    def apply(self, action: Any) -> None:
        if self.native is None:
            raise RuntimeError("Luanti online input is unavailable")
        if action.kind == "click" and action.x is not None and action.y is not None:
            self.native.click(
                action.x,
                action.y,
                button=action.button or "left",
                clicks=max(1, action.clicks or 1),
            )
        elif action.kind == "key" and action.key:
            self.native.key(action.key, modifiers=action.modifiers)
        elif action.kind == "type" and action.text is not None:
            if action.x is not None and action.y is not None:
                self.native.click(action.x, action.y)
            self.native.type_text(action.text)
            if action.enter:
                self.native.key("enter")
        elif action.kind == "scroll":
            if action.x is not None and action.y is not None:
                self.native.move(action.x, action.y)
            self.native.scroll(action.dx or 0, action.dy or 0)
        elif action.kind == "drag" and None not in (
            action.x,
            action.y,
            action.x2,
            action.y2,
        ):
            self.native.drag(action.x, action.y, action.x2, action.y2)
        elif action.kind == "wait":
            time.sleep(max(0.0, min(5.0, action.seconds or 0.0)))
        time.sleep(0.3)

    def restore_start(self) -> dict[str, Any]:
        from .gui_agent import AgentAction

        # All three registered Luanti Start states are rooted in the local-game
        # tab.  Static world data may already exist, but the visible tab is
        # deliberately reached from the neutral About cold start.
        tab_y = 140 if self.height > 800 else 95
        self.apply(AgentAction(kind="click", x=215, y=tab_y, button="left", clicks=1))
        self.start_restore_applied = True
        return {
            "operation": "open_local_game_tab",
            "accepted": True,
            "x": 215,
            "y": tab_y,
        }

    def initial_state_evidence(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {
            "process_alive": alive,
            "cold_start_profile": "neutral_about_tab",
            "start_restore_not_applied": not self.start_restore_applied,
            "passed": alive and not self.start_restore_applied,
        }

    def start_state_evidence(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {
            "process_alive": alive,
            "local_tab_restore_applied": self.start_restore_applied,
            "passed": alive and self.start_restore_applied,
        }

    def close(self) -> None:
        process = self.process
        self.process = None
        self.native = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run_jupyter_online_transition(
    *,
    tree: Path,
    output_dir: Path,
    task_id: str,
    version: str,
    arm: str,
) -> OnlineTransitionEvidence:
    """Run one real browser transition through State or a pixel click."""

    if version not in {"buggy", "fixed"}:
        raise ValueError("P2P-only online version is invalid")
    if arm not in {"lazarus_state", "lazarus_agent_s"}:
        raise ValueError("P2P-only online arm is invalid")
    spec = FRAME_SPECS.get(task_id)
    if spec is None or spec.capture_kind != "playwright":
        raise ValueError("Jupyter online runtime requires a Playwright frame spec")
    selector = _JUPYTER_PIXEL_TARGETS.get(task_id)
    if selector is None:
        raise ValueError(f"{task_id}: online pixel target is not registered")
    run_dir = output_dir.expanduser().resolve()
    initial = run_dir / "initial.png"
    before = run_dir / "before.png"
    after = run_dir / "after.png"
    started = time.monotonic()
    with JupyterOnlineSession(
        tree=tree, output_dir=run_dir / "session", task_id=task_id
    ) as session:
        page = session.page
        assert page is not None
        session.capture_policy_frame(initial)
        if arm == "lazarus_state":
            start_result = session.restore_start()
            start_action = {
                "interface": "typed_start_checkpoint_v1",
                "operation": "restore_public_start",
                "result": start_result,
            }
        else:
            launcher = (
                page.locator(".jp-LauncherCard")
                .filter(has_text="Open task workspace")
                .first
            )
            launcher_box = launcher.bounding_box()
            if not isinstance(launcher_box, dict):
                raise RuntimeError(
                    f"{task_id}: visible launcher entry has no bounding box"
                )
            start_x = int(launcher_box["x"] + launcher_box["width"] / 2)
            start_y = int(launcher_box["y"] + min(20, launcher_box["height"] / 4))
            page.mouse.click(start_x, start_y)
            start_action = {
                "interface": "pixel_mouse_keyboard_only",
                "kind": "click",
                "x": start_x,
                "y": start_y,
                "selector_disclosed_to_policy": False,
            }
        page.wait_for_function(
            "window.__warptestStartComplete === true", timeout=30_000
        )
        session._task_frame().wait_for_function(
            "typeof window.__warptestStateTransition === 'function'", timeout=30_000
        )
        session._task_frame().wait_for_function(
            "window.__ready === true", timeout=60_000
        )
        page.wait_for_timeout(300)
        session.capture_evidence_frame(before)
        if arm == "lazarus_state":
            result = session.state_transition()
            action = {
                "interface": "registered_public_transition_v1",
                "operation": "public_reference_transition",
                "result": result,
            }
        else:
            target = session._task_frame().locator(selector).first
            box = target.bounding_box()
            if not isinstance(box, dict):
                raise RuntimeError(
                    f"{task_id}: visible pixel target has no bounding box"
                )
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
            page.mouse.click(x, y)
            action = {
                "interface": "pixel_mouse_keyboard_only",
                "kind": "click",
                "x": x,
                "y": y,
                "selector_disclosed_to_policy": False,
            }
        page.wait_for_function(
            "document.querySelector('iframe[data-stateportal-p2p-task]') !== null",
            timeout=30_000,
        )
        session._task_frame().wait_for_function(
            "window.__warptestTransitionComplete === true", timeout=30_000
        )
        page.wait_for_timeout(300)
        session.capture_evidence_frame(after)
        transitioned = session.transition_complete()
    if not before.is_file() or not after.is_file():
        raise RuntimeError(f"{task_id}: online transition did not capture both frames")
    action["latency_ms"] = int((time.monotonic() - started) * 1000)
    return OnlineTransitionEvidence(
        task_id=task_id,
        version=version,
        arm=arm,
        before=before,
        after=after,
        same_live_lineage=True,
        transition_executed=transitioned is True,
        action=action,
        console=(),
        initial=initial,
        start_constructed=True,
        start_action=start_action,
    )


def run_luanti_online_transition(
    *,
    tree: Path,
    output_dir: Path,
    task_id: str,
    version: str,
    arm: str,
) -> OnlineTransitionEvidence:
    """Drive a real Luanti main-menu transition with Quartz pixel input."""
    if version not in {"buggy", "fixed"} or arm not in {
        "lazarus_state",
        "lazarus_agent_s",
    }:
        raise ValueError("invalid Luanti online cell identity")
    spec = FRAME_SPECS.get(task_id)
    if spec is None or spec.capture_kind != "luanti_engine":
        raise ValueError("Luanti online runtime requires a Luanti frame spec")
    run_dir = output_dir.expanduser().resolve()
    initial = run_dir / "initial.png"
    before = run_dir / "before.png"
    after = run_dir / "after.png"
    with LuantiOnlineSession(tree=tree, output_dir=run_dir, task_id=task_id) as session:
        session.capture_policy_frame(initial)
        if arm == "lazarus_state":
            start_action = {
                "interface": "typed_start_checkpoint_v1",
                **session.restore_start(),
            }
        else:
            from .gui_agent import AgentAction

            tab_y = 140 if session.height > 800 else 95
            session.apply(
                AgentAction(kind="click", x=215, y=tab_y, button="left", clicks=1)
            )
            start_action = {
                "interface": "pixel_mouse_keyboard_only",
                "kind": "click",
                "x": 215,
                "y": tab_y,
            }
        session.capture_policy_frame(before)
        actions = _apply_luanti_registered_transition(session, task_id)
        session.capture_policy_frame(after)
    return OnlineTransitionEvidence(
        task_id=task_id,
        version=version,
        arm=arm,
        before=before,
        after=after,
        same_live_lineage=True,
        transition_executed=bool(actions),
        action={
            "interface": (
                "registered_public_transition_v1"
                if arm == "lazarus_state"
                else "pixel_mouse_keyboard_only"
            ),
            "actions": actions,
            "scripted_product_transition": False,
        },
        console=(),
        initial=initial,
        start_constructed=_sha256(initial) != _sha256(before),
        start_action=start_action,
    )


def prepare_openra_online_tree(
    *, source_tree: Path, destination: Path, task_id: str
) -> Path:
    """Build an identical fixture overlay without performing the transition."""

    from .c1_lazarus_selection import p2p_only_engine_capture as engine

    target = destination.expanduser().resolve()
    marker = target / ".warptest-p2p-only-online-runtime.json"
    if marker.is_file():
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value == {"schema_version": 1, "task_id": task_id}:
            return target
    engine._cow_copy(source_tree, target)
    if task_id == "lzp-openra-22476-load-game-faction-filter":
        main = target / "OpenRA.Mods.Common/Widgets/Logic/MainMenuLogic.cs"
        engine._patch_once(
            main,
            "loadButton.IsDisabled = () => !LoadGameBrowserLogic.IsLoadPanelEnabled(modData.Manifest);",
            "loadButton.IsDisabled = () => false;",
            label="P2P-only online Load button fixture",
        )
        load = target / "OpenRA.Mods.Common/Widgets/Logic/LoadGameBrowserLogic.cs"
        engine._patch_once(
            load,
            "\t\t\t\t\tfactions.Insert(0, null);\n",
            "\t\t\t\t\tfactions.Insert(0, null);\n"
            "\t\t\t\t\tif (factions.Count == 1)\n"
            "\t\t\t\t\t{\n"
            '\t\t\t\t\t\tfactions.Add("allies");\n'
            '\t\t\t\t\t\tfactions.Add("soviet");\n'
            "\t\t\t\t\t}\n",
            label="P2P-only online faction fixture",
        )
    elif task_id == "lzp-openra-22283-tileset-template-order":
        helper = (
            target / "OpenRA.Mods.Common/Widgets/Logic/WarptestP2POnlyOnlineStart.cs"
        )
        helper.write_text(_OPENRA_ONLINE_START_CS, encoding="utf-8")
        blank = target / "OpenRA.Mods.Common/LoadScreens/BlankLoadScreen.cs"
        engine._patch_once(
            blank,
            "\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
            '\t\t\tvar p2pOnlineStart = Environment.GetEnvironmentVariable("WARPTEST_P2P_ONLY_ONLINE_START") ?? "";\n'
            "\t\t\tif (!string.IsNullOrEmpty(p2pOnlineStart))\n"
            "\t\t\t\tGame.OnShellmapLoaded += () => OpenRA.Mods.Common.Widgets.Logic.WarptestP2POnlyOnlineStart.Run(p2pOnlineStart);\n"
            "\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
            label="P2P-only online editor Start",
        )
    else:
        raise ValueError(f"{task_id}: OpenRA online fixture is not registered")
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise RuntimeError("dotnet is not available")
    result = subprocess.run(
        (dotnet, "build", "-c", "Release", "--nologo"),
        cwd=target,
        capture_output=True,
        check=False,
        timeout=1800,
        env={**os.environ, "DOTNET_NOLOGO": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            "OpenRA online runtime build failed:\n"
            + result.stdout.decode("utf-8", "replace")[-3000:]
            + result.stderr.decode("utf-8", "replace")[-3000:]
        )
    engine.patch_openra_runtime_roll_forward(target)
    marker.write_text(
        json.dumps({"schema_version": 1, "task_id": task_id}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def run_openra_online_transition(
    *,
    tree: Path,
    output_dir: Path,
    task_id: str,
    version: str,
    arm: str,
) -> OnlineTransitionEvidence:
    """Run an actual OpenRA background-pixel transition in one process."""

    if version not in {"buggy", "fixed"} or arm not in {
        "lazarus_state",
        "lazarus_agent_s",
    }:
        raise ValueError("invalid OpenRA online cell identity")
    output = output_dir.expanduser().resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    initial = output / "initial.png"
    before = output / "before.png"
    after = output / "after.png"
    with _openra_online_session(
        tree=tree, output=output / "session", task_id=task_id
    ) as session:
        session.capture_policy_frame(initial)
        if arm == "lazarus_state":
            start_result = _restore_openra_start(session, task_id)
            start_action = {
                "interface": "typed_start_checkpoint_v1",
                **start_result,
            }
        else:
            start_actions = _dispatch_openra_pixel_sequence(
                session,
                _openra_start_pixel_sequence(task_id),
                output=output,
                label="start",
            )
            start_action = {
                "interface": "pixel_mouse_keyboard_only",
                "actions": start_actions,
                "already_satisfied": not start_actions,
            }
        session.capture_policy_frame(before)
        actions = _apply_openra_registered_transition(session, task_id)
        session.capture_policy_frame(after)
    if not before.is_file() or not after.is_file():
        raise RuntimeError("OpenRA online transition did not capture both frames")
    return OnlineTransitionEvidence(
        task_id=task_id,
        version=version,
        arm=arm,
        before=before,
        after=after,
        same_live_lineage=True,
        transition_executed=_sha256(before) != _sha256(after),
        action={
            "interface": (
                "registered_public_transition_v1"
                if arm == "lazarus_state"
                else "pixel_mouse_keyboard_only"
            ),
            "actions": actions,
            "scripted_product_transition": False,
        },
        console=(),
        initial=initial,
        start_constructed=(
            task_id == "lzp-openra-22476-load-game-faction-filter"
            or _sha256(initial) != _sha256(before)
        ),
        start_action=start_action,
    )


def _dispatch_openra_pixel_sequence(
    session: Any,
    sequence: tuple[tuple[str, int, int, float], ...],
    *,
    output: Path,
    label: str,
) -> list[dict[str, Any]]:
    from .agent_s_bridge import AgentSAction, InputBatch

    actions: list[dict[str, Any]] = []
    for kind, x, y, delay in sequence:
        if kind != "click":
            raise RuntimeError(f"unknown OpenRA online action: {kind}")
        action = AgentSAction(kind="click", x=x, y=y, button="left", clicks=1)
        batch = InputBatch(
            session_id=session.session_id,
            frame_id=int(session.last_policy_frame_id or 0),
            batch_id=uuid.uuid4().hex,
            actions=(action,),
        ).validated(width=session.width, height=session.height)
        receipt = session.dispatch_input_batch(batch)
        if receipt.get("accepted") is not True:
            raise RuntimeError("OpenRA background pixel click was rejected")
        actions.append({"kind": kind, "x": x, "y": y})
        time.sleep(delay)
        session.capture_policy_frame(output / f"{label}-step-{len(actions):02d}.png")
    return actions


def _openra_start_pixel_sequence(
    task_id: str,
) -> tuple[tuple[str, int, int, float], ...]:
    if task_id == "lzp-openra-22476-load-game-faction-filter":
        return ()
    if task_id == "lzp-openra-22283-tileset-template-order":
        # Main menu -> Map Editor.  The real canary validates the resulting
        # frame against the registered Start golden before paid dispatch.
        return (("click", 200, 515, 1.0), ("click", 200, 355, 5.0))
    raise ValueError(f"{task_id}: OpenRA Start pixel sequence is not registered")


def _openra_public_pixel_sequence(
    task_id: str,
) -> tuple[tuple[str, int, int, float], ...]:
    if task_id == "lzp-openra-22476-load-game-faction-filter":
        return (
            ("click", 200, 275, 0.8),
            ("click", 200, 355, 1.0),
            ("click", 220, 628, 0.8),
            ("click", 454, 297, 0.8),
        )
    if task_id == "lzp-openra-22283-tileset-template-order":
        return (("click", 980, 420, 1.0),)
    raise ValueError(f"{task_id}: OpenRA online action sequence is not registered")


def _luanti_public_pixel_sequence(task_id: str) -> tuple[tuple[Any, ...], ...]:
    if task_id == "lzp-luanti-16679-unavailable-settings":
        return (
            ("click", 1134, 95),
            ("wait", 0.8),
            ("click", 365, 282),
            ("wait", 0.8),
        )
    if task_id == "lzp-luanti-16799-world-mods":
        return (
            ("click", 900, 292),
            ("wait", 0.4),
            ("click", 1205, 713),
            ("wait", 0.8),
        )
    if task_id == "lzp-luanti-17152-server-list-search-tooltip":
        return (
            ("click", 394, 95),
            ("wait", 0.8),
            ("move", 400, 165),
            ("wait", 0.8),
        )
    raise ValueError(f"{task_id}: Luanti online action sequence is not registered")


def _prepare_jupyter_document(
    tree: Path,
    run_dir: Path,
    spec: FrameSpec,
    task_id: str,
    selector: str,
    with_launcher: bool = True,
) -> None:
    # Reuse the candidate-build helpers; they compile/in-line only candidate
    # material and never carry a golden or version verdict into the page.
    from scripts.capture_lazarus_p2p_only_frames import (
        build_packages,
        bundle,
        inline_styles,
    )

    build_packages(tree, spec)
    styles = inline_styles(tree, spec)
    if spec.needs_bundle:
        bundle(run_dir, tree, spec)
    css_step = CSS_ONLY_STEPS.get(task_id)
    bridge = _interaction_bridge(selector, css_step)
    script = ""
    if spec.needs_bundle:
        if with_launcher:
            script = '<script src="./dist/bundle.js"></script>'
        else:
            script = '<script src="/files/dist/bundle.js"></script>'
    extension_bootstrap = f"""
<script>
(() => {{
  window.__warptestStartComplete = {str(not with_launcher).lower()};
  window.__warptestBenchmarkExtension = {{
    id: 'stateportal-lazarus-p2p-only-start',
    taskId: {json.dumps(task_id)},
    opensGoal: false
  }};
  window.__warptestOpenStart = source => {{
    const launcher = document.querySelector('#warptest-launcher');
    const target = document.querySelector('#warptest-target');
    if (!launcher || !target) throw new Error('benchmark extension surface is missing');
    launcher.style.display = 'none';
    target.style.display = 'contents';
    window.__warptestStartComplete = true;
    return {{opened: true, source, goal_transition_complete: window.__warptestTransitionComplete}};
  }};
  const entry = document.querySelector('#warptest-open-task');
  if (entry) entry.addEventListener(
    'click', () => window.__warptestOpenStart('visible_launcher_entry')
  );
}})();
</script>
"""
    launcher_css = """
body { margin: 0; }
#warptest-launcher { box-sizing: border-box; width: 100vw; height: 100vh;
  padding: 28px; background: #f5f5f5; color: #222;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
#warptest-launcher h1 { margin: 0 0 22px; font-size: 24px; }
#warptest-open-task { width: 310px; min-height: 82px; padding: 14px 18px;
  border: 1px solid #8a8a8a; border-radius: 4px; background: #fff;
  color: #222; font-size: 17px; text-align: left; cursor: pointer; }
#warptest-open-task small { display: block; margin-top: 8px; color: #555; }
#warptest-target { display: none; }
"""
    body = (
        f"""<section id="warptest-launcher" role="main" aria-label="JupyterLab Launcher">
<h1>JupyterLab Launcher</h1>
<button id="warptest-open-task">Open task workspace
<small>Use the candidate-built component to satisfy the published preconditions.</small></button>
</section><main id="warptest-target">{spec.body_html}</main>"""
        if with_launcher
        else spec.body_html
    )
    embedded_css = (
        "html, body { width: 760px; height: 420px; overflow: hidden; }"
        if not with_launcher
        else ""
    )
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{launcher_css}{embedded_css}{styles}</style></head>
<body>{body}{script}<script>{bridge}</script>{extension_bootstrap}</body></html>
"""
    (run_dir / "document.html").write_text(document, encoding="utf-8")


def _prepare_jupyter_benchmark_extension(
    tree: Path, run_dir: Path, task_id: str
) -> Path:
    """Build a federated extension whose Launcher card only opens the task panel."""

    extension = run_dir / "benchmark-extension"
    source = extension / "lib"
    output = run_dir / "labextensions/stateportal-lazarus-p2p-only-start"
    source.mkdir(parents=True, exist_ok=True)
    package = {
        "name": "stateportal-lazarus-p2p-only-start",
        "version": "1.0.0",
        "private": True,
        "main": "lib/index.js",
        "dependencies": {
            "@jupyterlab/application": "^4.0.0",
            "@jupyterlab/apputils": "^4.0.0",
            "@jupyterlab/coreutils": "^6.0.0",
            "@jupyterlab/launcher": "^4.0.0",
            "@jupyterlab/ui-components": "^4.0.0",
        },
        "jupyterlab": {
            "extension": True,
            "outputDir": "../labextensions/stateportal-lazarus-p2p-only-start",
        },
    }
    (extension / "package.json").write_text(
        json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    index = f"""
import {{ JupyterFrontEnd }} from '@jupyterlab/application';
import {{ MainAreaWidget }} from '@jupyterlab/apputils';
import {{ PageConfig }} from '@jupyterlab/coreutils';
import {{ ILauncher }} from '@jupyterlab/launcher';
import {{ IFrame }} from '@jupyterlab/ui-components';

const command = 'stateportal-p2p-only:open-task';
const plugin = {{
  id: 'stateportal-lazarus-p2p-only-start:plugin',
  autoStart: true,
  requires: [ILauncher],
  activate: (app, launcher) => {{
    let widget = null;
    app.commands.addCommand(command, {{
      label: 'Open task workspace',
      caption: 'Open the candidate-built task component without executing its Goal transition.',
      execute: async () => {{
        if (!widget || widget.isDisposed) {{
          const content = new IFrame({{sandbox: ['allow-scripts', 'allow-same-origin']}});
          content.title.label = 'P2P-only task workspace';
          widget = new MainAreaWidget({{content}});
          widget.id = 'stateportal-p2p-only-task-{task_id}';
          const iframe = content.node.querySelector('iframe');
          iframe.setAttribute('data-stateportal-p2p-task', {json.dumps(task_id)});
          iframe.setAttribute('name', 'stateportal-p2p-task');
          const response = await fetch(PageConfig.getBaseUrl() + 'files/document.html');
          if (!response.ok) throw new Error('benchmark task document could not be loaded');
          iframe.addEventListener('load', () => {{ window.__warptestStartComplete = true; }});
          iframe.srcdoc = await response.text();
          app.shell.add(widget, 'main');
        }}
        app.shell.activateById(widget.id);
        await new Promise(resolve => setTimeout(resolve, 500));
        window.__warptestStartComplete = true;
        return {{opened: true, task_id: {json.dumps(task_id)}, goal_transition_complete: false}};
      }}
    }});
    launcher.add({{command, category: 'StatePortal', categoryRank: 0, rank: 1}});
    window.__warptestStartComplete = false;
    window.__warptestOpenStart = source => app.commands.execute(command, {{source}});
    window.__warptestBenchmarkExtension = {{
      id: 'stateportal-lazarus-p2p-only-start',
      taskId: {json.dumps(task_id)},
      opensGoal: false,
      realJupyterLab: true
    }};
  }}
}};
export default plugin;
"""
    (source / "index.js").write_text(index, encoding="utf-8")
    builder = tree / "node_modules/@jupyter/builder/lib/build-labextension.js"
    core = tree / "dev_mode/package.json"
    if not builder.is_file() or not core.is_file():
        raise RuntimeError("candidate JupyterLab extension builder is missing")
    completed = subprocess.run(
        (
            "node",
            str(builder),
            "--development",
            "--core-package-file",
            str(core),
            str(extension),
        ),
        cwd=tree,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if completed.returncode != 0 or not output.is_dir():
        raise RuntimeError(
            "Jupyter benchmark extension build failed:\n"
            + completed.stdout.decode("utf-8", "replace")[-2000:]
            + completed.stderr.decode("utf-8", "replace")[-2000:]
        )
    return output.parent


def _start_jupyterlab_server(
    tree: Path,
    run_dir: Path,
    extension_root: Path,
    *,
    owner: JupyterOnlineSession,
) -> str:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    log_path = run_dir / "jupyterlab-server.log"
    owner.server_log = log_path.open("wb")
    jupyter = shutil.which("jupyter")
    if jupyter is None:
        raise RuntimeError("Jupyter executable is missing")
    command = (
        jupyter,
        "lab",
        "--dev-mode",
        "--LabApp.extensions_in_dev_mode=True",
        "--no-browser",
        "--ServerApp.ip=127.0.0.1",
        f"--ServerApp.port={port}",
        "--ServerApp.port_retries=0",
        "--IdentityProvider.token=",
        f"--ServerApp.root_dir={run_dir}",
        f"--LabApp.extra_labextensions_path={extension_root}",
    )
    owner.server_process = subprocess.Popen(
        command,
        cwd=tree,
        env={**os.environ, "PYTHONPATH": str(tree)},
        stdout=owner.server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if owner.server_process.poll() is not None:
            owner.server_log.flush()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            raise RuntimeError("candidate JupyterLab exited during startup:\n" + tail)
        try:
            with urllib.request.urlopen(url + "/lab", timeout=1) as response:
                if response.status == 200:
                    return url
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("candidate JupyterLab cold start timed out")


def _synthesize_registered_transition(
    context: Any, *, artifact_dir: Path
) -> dict[str, Any]:
    from .model_request import resolve_model_request_profile
    from .providers import resolve_model
    from .synthesis import OpenAICompatibleChatClient

    key = context.key
    clean_model, provider = resolve_model(
        key.model,
        default_base_url=os.environ["RELAY_BASE_URL"],
        default_api_key_env="RELAY_API_KEY",
    )
    public = {
        "state_description": str(context.task.get("state_description") or ""),
        "preconditions": str(context.task.get("preconditions") or ""),
        "reference_steps": str(context.task.get("reference_steps") or ""),
        "source_revision": context.agent_source.source_revision,
        "source_tree_sha256": context.agent_source.sanitized_tree_sha256,
    }
    prompt = (
        "Synthesize the typed State checkpoint that restores the published Start "
        "from the registered raw application cold start. Return only JSON with "
        'exactly {"operation":"restore_public_start"}. The checkpoint must '
        "not execute any reference step or Goal transition.\n"
        + json.dumps(public, ensure_ascii=False, sort_keys=True)
    )
    client = OpenAICompatibleChatClient(
        api_key=os.environ[provider.api_key_env],
        base_url=provider.base_url,
        temperature=0.2,
        max_tokens=128,
        minimum_max_tokens=128,
        reasoning_retry_max_tokens=None,
        extra_headers=dict(provider.extra_headers),
        cost_kind="lazarus_p2p_only_state_synthesis",
    )
    result = client.converse(
        clean_model,
        [
            {
                "role": "system",
                "content": "Choose one registered Start checkpoint. Return JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        empty_retries=1,
    )
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (artifact_dir / "raw-response.txt").write_text(result.content, encoding="utf-8")
    failure: str | None = None
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError:
        parsed = None
        failure = "invalid_json"
    if parsed != {"operation": "restore_public_start"}:
        failure = failure or "invalid_operation"
    encoded = (
        json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
        if failure is None
        else result.content.encode("utf-8")
    )
    profile = resolve_model_request_profile(clean_model)
    return {
        "checkpoint_sha256": hashlib.sha256(encoded).hexdigest(),
        "usable": failure is None,
        "failure": failure,
        "total_tokens": result.total_tokens,
        "request_profile": profile.provenance(),
        "public_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "artifact_dir": str(artifact_dir),
    }


@contextmanager
def _openra_online_session(*, tree: Path, output: Path, task_id: str):
    from .c1_openra_runtime import OpenRASettingsSession

    previous = os.environ.get("WARPTEST_P2P_ONLY_ONLINE_START")
    previous_control = os.environ.get("WARPTEST_P2P_ONLY_START_CONTROL")
    control = output.resolve() / "start-control.trigger"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.unlink(missing_ok=True)
    if task_id == "lzp-openra-22283-tileset-template-order":
        os.environ["WARPTEST_P2P_ONLY_ONLINE_START"] = "editor-overlays"
        os.environ["WARPTEST_P2P_ONLY_START_CONTROL"] = str(control)
    else:
        os.environ.pop("WARPTEST_P2P_ONLY_ONLINE_START", None)
        os.environ.pop("WARPTEST_P2P_ONLY_START_CONTROL", None)
    try:
        with OpenRASettingsSession(
            tree, output, settings={}, experiment_seed=0
        ) as session:
            session.p2p_start_control_path = control
            session.p2p_start_restore_applied = False
            if task_id == "lzp-openra-22476-load-game-faction-filter":
                (session.support_dir / "Saves/ra/{DEV_VERSION}").mkdir(
                    parents=True, exist_ok=True
                )
            yield session
    finally:
        if previous is None:
            os.environ.pop("WARPTEST_P2P_ONLY_ONLINE_START", None)
        else:
            os.environ["WARPTEST_P2P_ONLY_ONLINE_START"] = previous
        if previous_control is None:
            os.environ.pop("WARPTEST_P2P_ONLY_START_CONTROL", None)
        else:
            os.environ["WARPTEST_P2P_ONLY_START_CONTROL"] = previous_control


def _restore_openra_start(session: Any, task_id: str) -> dict[str, Any]:
    if task_id == "lzp-openra-22476-load-game-faction-filter":
        session.p2p_start_restore_applied = True
        return {"operation": "already_satisfied_post_boot_main_menu", "accepted": True}
    if task_id != "lzp-openra-22283-tileset-template-order":
        raise ValueError(f"{task_id}: OpenRA Start restore is not registered")
    control = Path(str(getattr(session, "p2p_start_control_path", ""))).resolve()
    control.write_text("open-editor\n", encoding="utf-8")
    time.sleep(5)
    session.p2p_start_restore_applied = True
    return {"operation": "open_map_editor", "accepted": True}


def _apply_openra_registered_transition(
    session: Any, task_id: str
) -> list[dict[str, Any]]:
    from .agent_s_bridge import AgentSAction, InputBatch

    actions: list[dict[str, Any]] = []
    for kind, x, y, delay in _openra_public_pixel_sequence(task_id):
        action = AgentSAction(kind=kind, x=x, y=y, button="left", clicks=1)
        batch = InputBatch(
            session_id=session.session_id,
            frame_id=int(session.last_policy_frame_id or 0),
            batch_id=uuid.uuid4().hex,
            actions=(action,),
        ).validated(width=session.width, height=session.height)
        receipt = session.dispatch_input_batch(batch)
        if receipt.get("accepted") is not True:
            raise RuntimeError("OpenRA registered transition click was rejected")
        actions.append({"kind": kind, "x": x, "y": y})
        time.sleep(delay)
        session.capture_policy_frame(
            session.report_dir / f"registered-step-{len(actions):02d}.png"
        )
    return actions


def _apply_luanti_registered_transition(
    session: Any, task_id: str
) -> list[dict[str, Any]]:
    from .gui_agent import AgentAction

    actions: list[dict[str, Any]] = []
    for action in _luanti_public_pixel_sequence(task_id):
        kind = action[0]
        if kind == "click":
            _, x, y = action
            session.apply(AgentAction(kind="click", x=x, y=y, button="left", clicks=1))
            actions.append({"kind": "click", "x": x, "y": y})
        elif kind == "move":
            _, x, y = action
            if session.native is None:
                raise RuntimeError("Luanti registered hover has no native input")
            session.native.move(x, y)
            time.sleep(0.3)
            actions.append({"kind": "move", "x": x, "y": y})
        elif kind == "wait":
            _, seconds = action
            time.sleep(seconds)
            actions.append({"kind": "wait", "seconds": seconds})
        else:
            raise RuntimeError(f"unknown Luanti registered transition action: {kind}")
    return actions


def _luanti_start_restore_adapter(*, task_id: str):
    def restore(clone: Path, receipt_path: Path, receipt: dict[str, Any]):
        from .lazarus_runtime import _consume_p2p_restore_receipt

        _consume_p2p_restore_receipt(receipt_path, receipt)
        output = clone.parent / f"{clone.name}-p2p-online-start"
        with LuantiOnlineSession(
            tree=clone, output_dir=output, task_id=task_id
        ) as session:
            session.restore_start()
            frame = Path(session.capture_policy_frame(output / "start.png"))
            applied = frame.is_file() and session.start_restore_applied is True
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "luanti-p2p-only-online-start-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "start_frame_sha256": _sha256(frame) if frame.is_file() else None,
        }

    return restore


def _openra_start_restore_adapter(*, task_id: str):
    def restore(clone: Path, receipt_path: Path, receipt: dict[str, Any]):
        from .lazarus_runtime import _consume_p2p_restore_receipt

        _consume_p2p_restore_receipt(receipt_path, receipt)
        prepared = prepare_openra_online_tree(
            source_tree=clone,
            destination=clone.parent / f"{clone.name}-p2p-online-start",
            task_id=task_id,
        )
        output = clone.parent / f"{clone.name}-p2p-online-start-report"
        with _openra_online_session(
            tree=prepared, output=output, task_id=task_id
        ) as session:
            action = _restore_openra_start(session, task_id)
            frame = Path(session.capture_policy_frame(output / "start.png"))
            applied = frame.is_file() and action.get("accepted") is True
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "openra-p2p-only-online-start-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "start_frame_sha256": _sha256(frame) if frame.is_file() else None,
        }

    return restore


def _jupyter_start_restore_adapter(*, task_id: str):
    def restore(
        clone: Path, receipt_path: Path, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        from .lazarus_runtime import _consume_p2p_restore_receipt

        _consume_p2p_restore_receipt(receipt_path, receipt)
        output = clone.parent / f"{clone.name}-p2p-online-start-runtime"
        with JupyterOnlineSession(
            tree=clone,
            output_dir=output,
            task_id=task_id,
        ) as session:
            session.restore_start()
            frame = Path(session.capture_policy_frame(output / "start.png"))
            applied = (
                frame.is_file()
                and session.start_complete() is True
                and session.transition_complete() is False
            )
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "jupyter-p2p-only-online-start-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "start_frame_sha256": _sha256(frame) if frame.is_file() else None,
        }

    return restore


def _interaction_bridge(selector: str, css_step: tuple[str, str] | None) -> str:
    css_payload = json.dumps(
        {"selector": css_step[0], "kind": css_step[1]} if css_step is not None else None
    )
    return f"""
(() => {{
  const cssStep = {css_payload};
  let running = false;
  window.__warptestTransitionComplete = false;
  const applyCssStep = step => {{
    const host = document.querySelector(step.selector);
    if (!host) throw new Error('online CSS target is missing');
    if (step.kind === 'mark_region') host.classList.add('marked');
    else if (step.kind === 'mark_chip') host.classList.add('jp-mod-selected-InputText');
    else if (step.kind === 'append_glyph_button') {{
      const button = document.createElement('button');
      button.className = 'glyph-face';
      button.textContent = 'WWWiii';
      host.appendChild(button);
    }} else throw new Error('unknown online CSS step');
    return {{kind: step.kind}};
  }};
  const transition = async () => {{
    if (running || window.__warptestTransitionComplete) return {{duplicate: true}};
    running = true;
    const result = cssStep ? applyCssStep(cssStep) : await window.__step();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    window.__warptestTransitionComplete = true;
    running = false;
    return result || {{completed: true}};
  }};
  window.__warptestStateTransition = transition;
  const bind = () => {{
    const target = document.querySelector({json.dumps(selector)});
    if (!target) throw new Error('online pixel target is missing');
    target.addEventListener('click', event => {{
      if (running) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      transition();
    }}, true);
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
  if (cssStep) window.__ready = true;
}})();
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chromium_executable() -> Path:
    cache = Path.home() / "Library/Caches/ms-playwright"
    candidates = sorted(
        cache.glob(
            "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        ),
        reverse=True,
    )
    if candidates:
        return candidates[0]
    for fallback in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if fallback.is_file():
            return fallback
    raise RuntimeError("Playwright-compatible Chromium executable is unavailable")


__all__ = [
    "JupyterOnlineSession",
    "OnlineTransitionEvidence",
    "prepare_openra_online_tree",
    "run_jupyter_online_transition",
    "run_luanti_online_transition",
    "run_openra_online_transition",
]
