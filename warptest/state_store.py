"""State management for the long-horizon GUI Gen+Test loop.

The headline contribution of the State Loop MVP is making the *test* half of the
loop cheap. A naive PlayCoder-style loop re-pays the full cost of reaching a deep
GUI state on every iteration: boot the dev server, launch the browser, then replay
the restore sequence. For long-horizon software this dominates wall-clock time and,
when a GUI agent drives it, also stacks per-step latency and hallucination risk.

This module separates two costs that the existing one-shot ``run_spec`` always pays
together:

* **session bootstrap** -- starting the dev server and browser. Paid once and kept
  warm across the whole loop by :class:`StateSession`.
* **state restore** -- driving the warm runtime to the target start state. Measured
  per iteration. A localStorage snapshot of a reached deep state is cached by
  :class:`StateStore` so save-restorable states can be re-entered without replay.

``cold`` numbers (a fresh runtime per restore, i.e. state management OFF) are
measured against ``warm`` numbers (persistent session, state management ON) so the
loop can report a concrete speedup.

MVP scope: browser-backed warm sessions only (2048 / SpaceRPG / DatingSim).
Native/server profiles (Unity, OpenRA, Luanti, JupyterLab, LibreOffice, Blender) are
recognized and rejected with a clear message; :class:`StateSession` is the seam
where future profile-specific warm-restore adapters plug in.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .discovery import RepositoryInspector
from .models import PhaseResult
from .profiles import profile_for
from .runtime import BrowserRuntimeAdapter

# Profiles whose runtime is a local dev server + Playwright browser, which is what
# StateSession knows how to keep warm in the MVP.
BROWSER_PROFILES = frozenset({"Game2048Profile", "SpaceRPGProfile", "DatingSimProfile"})

# Non-browser-warm profiles run a native/Unity/server process per checkpoint
# request; warm state reuse needs a profile-specific adapter that does not exist yet.
ENGINE_PROFILES = frozenset(
    {"OpenRAProfile", "JupyterLabProfile", "LibreOfficeProfile", "BlenderProfile", "JynewProfile", "Pal3Profile", "LuantiProfile"}
)


class StateManagementUnsupported(RuntimeError):
    """Raised when a repo's profile cannot be kept warm in the MVP."""


def state_fingerprint(repo_commit: str | None, spec: dict[str, Any]) -> str:
    """Stable key for a deep state: repo revision + checkpoint + semantic target.

    Two configs that ask for the same start state share a snapshot, so iterating on
    the smoke actions (the part that changes most across loop rounds) reuses a cached
    restored state instead of replaying it.
    """

    payload = {
        "commit": repo_commit,
        "benchmark": spec.get("benchmark"),
        "target": spec.get("target"),
        "checkpoint": spec.get("checkpoint"),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class RestoreOutcome:
    status: str
    elapsed_ms: int
    screenshot: str | None = None
    localstorage: dict[str, str] = field(default_factory=dict)
    localstorage_keys: list[str] = field(default_factory=list)
    phase_statuses: dict[str, str] = field(default_factory=dict)
    failure_phase: str | None = None
    report: dict[str, Any] | None = None
    detail: str = ""
    source: str = "restore"  # "restore" | "snapshot"
    # Re-injectable deep-state snapshot captured at this restored state, if the
    # profile supports it (localStorage for save-restorable, hook state otherwise).
    runtime_state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # localStorage values + runtime_state can be large game saves; keep keys only.
        data.pop("localstorage", None)
        data.pop("runtime_state", None)
        return data


class StateSession:
    """A persistent dev-server + browser runtime for one browser-backed repo.

    Started once and reused for every restore in a loop. Each :meth:`restore` drives
    the already-warm runtime to a target state through the repo's existing profile,
    so the loop never re-pays server/browser startup after the first bootstrap.
    """

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        headless: bool = True,
        experiment_seed: int | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.headless = headless
        self.experiment_seed = int(experiment_seed) if experiment_seed is not None else None
        self.bootstrap_ms: int | None = None
        self.profile_name: str | None = None
        self.benchmark: str | None = None
        self.git_commit: str | None = None
        self._inspect = None
        self._profile = None
        self._runtime: BrowserRuntimeAdapter | None = None

    def __enter__(self) -> "StateSession":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def runtime(self) -> BrowserRuntimeAdapter:
        if self._runtime is None:
            raise RuntimeError("StateSession is not started.")
        return self._runtime

    def start(self) -> "StateSession":
        self._inspect = RepositoryInspector().inspect(self.repo)
        self.profile_name = self._inspect.profile
        self.git_commit = self._inspect.metadata.git_commit
        if self.profile_name not in BROWSER_PROFILES:
            hint = (
                "engine-backed profile is not yet supported for warm state reuse"
                if self.profile_name in ENGINE_PROFILES
                else "profile is not a known browser runtime"
            )
            raise StateManagementUnsupported(
                f"StateSession warm restore is browser-only in the MVP; "
                f"{self.profile_name} {hint}. (Interface reserved for future engine adapters.)"
            )
        self._profile = profile_for(self._inspect)
        self.benchmark = self._profile.benchmark
        self.report_dir.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        runtime = BrowserRuntimeAdapter(
            self.repo,
            self._inspect.metadata.launch_command,
            self.report_dir / "session",
            headless=self.headless,
            experiment_seed=self.experiment_seed,
        )
        try:
            runtime.start_server()
            runtime.start_browser()
        except BaseException:
            runtime.close()
            raise
        self._runtime = runtime
        self.bootstrap_ms = int((time.perf_counter() - start) * 1000)
        return self

    def restore_target_only(
        self,
        spec: dict[str, Any],
        spec_path: str | Path,
        *,
        screenshot_name: str = "start-state.png",
        settle_ms: int = 450,
    ) -> RestoreOutcome:
        """Restore and validate the target without smoke or oracle evaluation.

        Unlike :meth:`restore`, this entry point never calls ``profile.run``.
        The three browser profiles implement a dedicated Phase-A-only surface,
        so an empty action/assertion list cannot be mistaken for a no-op goal
        evaluation in start-state audits.
        """

        profile = self._profile
        if profile is None:
            raise RuntimeError("StateSession is not started.")
        runtime = self.runtime
        runtime.reset_browser_context()
        start = time.perf_counter()
        try:
            payload = profile.restore_target_only(copy.deepcopy(spec), runtime)
        except Exception as error:
            elapsed = int((time.perf_counter() - start) * 1000)
            return RestoreOutcome(
                status="failure",
                elapsed_ms=elapsed,
                failure_phase="target_restore_or_validation",
                detail=str(error),
                report={
                    "mode": "target_only",
                    "spec_path": str(Path(spec_path).expanduser().resolve()),
                    "actions_executed": False,
                    "assertions_evaluated": False,
                    "smoke_execution": "not_run",
                    "oracle_result": "not_run",
                },
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        self._settle(runtime, settle_ms)
        storage = self._dump_localstorage(runtime)
        screenshot = self._safe_screenshot(runtime, screenshot_name)
        status = "success" if payload.get("status") == "success" else "failure"
        restoration = payload.get("checkpoint_restoration")
        validation = payload.get("validation")
        phase_statuses = {
            "checkpoint_restoration": str(
                restoration.get("status", "unknown")
                if isinstance(restoration, dict)
                else "unknown"
            ),
            "validation": str(
                validation.get("status", "unknown")
                if isinstance(validation, dict)
                else "unknown"
            ),
            "smoke_execution": "not_run",
            "oracle_result": "not_run",
        }
        payload = {
            **payload,
            "mode": "target_only",
            "spec_path": str(Path(spec_path).expanduser().resolve()),
        }
        return RestoreOutcome(
            status=status,
            elapsed_ms=elapsed,
            screenshot=screenshot,
            localstorage=storage,
            localstorage_keys=sorted(storage),
            phase_statuses=phase_statuses,
            failure_phase=(None if status == "success" else "target_restore_or_validation"),
            report=payload,
            detail=str(payload.get("detail") or "Target-only restore completed."),
            runtime_state=(
                self._capture_runtime_state(runtime) if status == "success" else None
            ),
        )

    def restore(
        self,
        spec: dict[str, Any],
        spec_path: str | Path,
        *,
        actions: list[dict[str, Any]] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        screenshot_name: str = "state.png",
        settle_ms: int = 450,
        require_after: bool = False,
    ) -> RestoreOutcome:
        """Drive the warm runtime to ``spec``'s target and capture a screenshot.

        ``actions`` overrides the spec's smoke actions: pass ``[]`` to stop at the
        restored *before* state, or the full action list to land on the *after*
        state. Restore is timed without the bootstrap cost (already amortized).

        ``assertions`` overrides the spec's oracle checks the same way, so a caller
        can grade the goal with the registered oracle's assertions while still
        executing the candidate's own transition. Overriding here rather than in
        the caller's spec keeps the spec's snapshot identity stable.

        ``settle_ms`` lets tile/canvas appear-animations finish before the
        screenshot: 2048 tiles carry a `tile-new` scale-in keyframe, so an immediate
        capture would photograph an empty board even though the DOM is populated.
        """

        spec_variant = copy.deepcopy(spec)
        if actions is not None:
            spec_variant["actions"] = actions
        if assertions is not None:
            spec_variant["assertions"] = assertions
        runtime = self.runtime
        runtime.reset_browser_context()
        start = time.perf_counter()
        try:
            report = self._profile.run(spec_variant, Path(spec_path), runtime, mode="jump_and_validate")
        except Exception as error:
            elapsed = int((time.perf_counter() - start) * 1000)
            return RestoreOutcome(status="failure", elapsed_ms=elapsed, detail=str(error))
        elapsed = int((time.perf_counter() - start) * 1000)
        self._settle(runtime, settle_ms)
        storage = self._dump_localstorage(runtime)
        screenshot = self._safe_screenshot(runtime, screenshot_name)
        phase_statuses = self._phase_statuses(report)
        status, failure_phase, detail = self._restore_status(report, require_after=require_after)
        runtime_state = self._capture_runtime_state(runtime) if status == "success" else None
        return RestoreOutcome(
            status=status,
            elapsed_ms=elapsed,
            screenshot=screenshot,
            localstorage=storage,
            localstorage_keys=sorted(storage),
            phase_statuses=phase_statuses,
            failure_phase=failure_phase,
            report=report.to_dict(),
            detail=detail,
            runtime_state=runtime_state,
        )

    def fast_restore(
        self,
        spec: dict[str, Any],
        spec_path: str | Path,
        snapshot: dict[str, Any],
        *,
        actions: list[dict[str, Any]] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        screenshot_name: str = "state.png",
        settle_ms: int = 450,
    ) -> RestoreOutcome:
        """Re-enter the deep state by injecting ``snapshot`` instead of replaying.

        Injects the captured runtime state, validates it actually reconstructs the
        spec target, optionally applies smoke ``actions``, and screenshots. On a
        validation miss returns ``status='validate_failed'`` so the caller can fall
        back to a full restore (correctness guard).

        ``assertions`` are the oracle checks to evaluate after the actions. Passing
        them is what makes this path a real goal grade: without them the snapshot
        route would report success on nothing more than "target still valid and
        the actions did not raise". A failing assertion is a genuine goal failure,
        not a snapshot miss, so it returns ``status='failure'`` with
        ``failure_phase='oracle_result'`` and must not be retried as a full restore.
        """

        profile = self._profile
        if profile is None or not profile.supports_snapshot():
            return RestoreOutcome(status="validate_failed", elapsed_ms=0, detail="profile has no snapshot support")
        runtime = self.runtime
        runtime.reset_browser_context()
        target = spec.get("target", {}) if isinstance(spec.get("target"), dict) else {}
        start = time.perf_counter()
        try:
            profile.inject_runtime_state(runtime, snapshot)
        except Exception as error:
            elapsed = int((time.perf_counter() - start) * 1000)
            return RestoreOutcome(status="validate_failed", elapsed_ms=elapsed, detail=f"inject failed: {error}", source="snapshot")
        try:
            validation = profile._validate(runtime, target)
        except Exception as error:
            elapsed = int((time.perf_counter() - start) * 1000)
            return RestoreOutcome(status="validate_failed", elapsed_ms=elapsed, detail=f"validate raised: {error}", source="snapshot")
        if getattr(validation, "status", None) != "success":
            elapsed = int((time.perf_counter() - start) * 1000)
            return RestoreOutcome(
                status="validate_failed",
                elapsed_ms=elapsed,
                detail=f"snapshot validate mismatch: {getattr(validation, 'detail', '')}",
                source="snapshot",
            )
        # Captured before the actions run, matching what ``profile.run`` feeds its
        # oracle phase; score-delta assertions are meaningless without it.
        before_score = self._safe_score(profile, runtime)
        smoke_status = "skipped"
        if actions:
            try:
                smoke = profile._run_actions(runtime, actions)
            except Exception as error:
                elapsed = int((time.perf_counter() - start) * 1000)
                return RestoreOutcome(status="validate_failed", elapsed_ms=elapsed, detail=f"smoke raised: {error}", source="snapshot")
            smoke_status = getattr(smoke, "status", "unknown")
            if smoke_status != "success":
                elapsed = int((time.perf_counter() - start) * 1000)
                return RestoreOutcome(status="validate_failed", elapsed_ms=elapsed, detail="snapshot smoke failed", source="snapshot")
        oracle_status = "skipped"
        if assertions is not None:
            oracle = self._assert_oracles(profile, runtime, assertions, before_score)
            oracle_status = getattr(oracle, "status", "unknown")
            if oracle_status != "success":
                elapsed = int((time.perf_counter() - start) * 1000)
                self._settle(runtime, settle_ms)
                return RestoreOutcome(
                    status="failure",
                    elapsed_ms=elapsed,
                    screenshot=self._safe_screenshot(runtime, screenshot_name),
                    phase_statuses={
                        "checkpoint_restoration": "success",
                        "validation": "success",
                        "smoke_execution": smoke_status,
                        "oracle_result": oracle_status,
                    },
                    failure_phase="oracle_result",
                    detail=getattr(oracle, "detail", "") or "snapshot oracle assertions failed",
                    source="snapshot",
                )
        elapsed = int((time.perf_counter() - start) * 1000)
        self._settle(runtime, settle_ms)
        storage = self._dump_localstorage(runtime)
        screenshot = self._safe_screenshot(runtime, screenshot_name)
        runtime_state = self._capture_runtime_state(runtime)
        return RestoreOutcome(
            status="success",
            elapsed_ms=elapsed,
            screenshot=screenshot,
            localstorage=storage,
            localstorage_keys=sorted(storage),
            phase_statuses={
                "checkpoint_restoration": "success",
                "validation": "success",
                "smoke_execution": smoke_status,
                "oracle_result": oracle_status,
            },
            detail="Re-entered target from snapshot.",
            source="snapshot",
            runtime_state=runtime_state,
        )

    @staticmethod
    def _safe_score(profile: Any, runtime: BrowserRuntimeAdapter) -> int:
        score = getattr(profile, "_score", None)
        if score is None:
            return 0
        try:
            return int(score(runtime))
        except Exception:
            return 0

    @staticmethod
    def _assert_oracles(
        profile: Any,
        runtime: BrowserRuntimeAdapter,
        assertions: list[dict[str, Any]],
        before_score: int,
    ) -> PhaseResult:
        """Evaluate oracle assertions across profiles with differing signatures.

        Only the 2048 profile scores a delta, so only it takes ``before_score``.
        """

        evaluate = getattr(profile, "_assert_oracles", None)
        if evaluate is None:
            return PhaseResult(
                "failure", "profile cannot evaluate oracle assertions from a snapshot"
            )
        try:
            parameters = inspect.signature(evaluate).parameters
        except (TypeError, ValueError):
            parameters = {}
        try:
            if "before_score" in parameters:
                return evaluate(runtime, assertions, before_score)
            return evaluate(runtime, assertions)
        except Exception as error:
            return PhaseResult("failure", f"oracle assertions raised: {error}")

    def _capture_runtime_state(self, runtime: BrowserRuntimeAdapter) -> dict[str, Any] | None:
        profile = self._profile
        if profile is None or not profile.supports_snapshot():
            return None
        try:
            return profile.capture_runtime_state(runtime)
        except Exception:
            return None

    def supports_snapshot(self) -> bool:
        return self._profile is not None and self._profile.supports_snapshot()

    def capture(self, name: str) -> str | None:
        return self._safe_screenshot(self.runtime, name)

    def close(self) -> None:
        if self._runtime is not None:
            try:
                self._runtime.close()
            finally:
                self._runtime = None

    @staticmethod
    def _close_active_page(runtime: BrowserRuntimeAdapter) -> None:
        page = getattr(runtime, "page", None)
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        # Drop the reference so the next restore re-opens a fresh page. Profiles that
        # touch localStorage before opening (e.g. SpaceRPG) rely on `page is None` to take
        # the init-script path instead of evaluating on a closed page.
        runtime.page = None

    @staticmethod
    def _phase_statuses(report: Any) -> dict[str, str]:
        phases = (
            "checkpoint_restoration",
            "validation",
            "smoke_execution",
            "oracle_result",
        )
        return {
            phase: str(getattr(getattr(report, phase, None), "status", "unknown"))
            for phase in phases
        }

    @staticmethod
    def _restore_status(report: Any, *, require_after: bool) -> tuple[str, str | None, str]:
        required = ["checkpoint_restoration", "validation"]
        if require_after:
            required.extend(["smoke_execution", "oracle_result"])
        for phase in required:
            phase_result = getattr(report, phase, None)
            status = getattr(phase_result, "status", None)
            if status != "success":
                detail = getattr(phase_result, "detail", "") or f"{phase} did not succeed."
                return "failure", phase, str(detail)
        detail = (
            "Restored target and completed smoke/oracle phases."
            if require_after
            else "Restored target before smoke."
        )
        return "success", None, detail

    @staticmethod
    def _settle(runtime: BrowserRuntimeAdapter, settle_ms: int) -> None:
        if settle_ms <= 0:
            return
        try:
            runtime.page.wait_for_timeout(settle_ms)
            runtime.page.evaluate("() => document.fonts?.ready")
        except Exception:
            pass

    @staticmethod
    def _safe_screenshot(runtime: BrowserRuntimeAdapter, name: str) -> str | None:
        try:
            return runtime.screenshot(name)
        except Exception:
            return None

    @staticmethod
    def _dump_localstorage(runtime: BrowserRuntimeAdapter) -> dict[str, str]:
        try:
            return runtime.page.evaluate(
                """() => {
                  const out = {};
                  for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i);
                    if (key !== null) out[key] = localStorage.getItem(key);
                  }
                  return out;
                }"""
            )
        except Exception:
            return {}


@dataclass
class RestoreTiming:
    cold_restore_ms: int | None = None
    warm_restore_ms: int | None = None
    fast_restore_ms: int | None = None
    session_bootstrap_ms: int | None = None
    speedup: float | None = None
    fast_vs_full_speedup: float | None = None
    cache_hit: bool = False
    fast_restore_used: bool = False
    fast_restore_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateStore:
    """Per-repo deep-state cache layered on a warm :class:`StateSession`.

    Caches the localStorage snapshot of a reached deep state, keyed by
    :func:`state_fingerprint`, under ``.warptest-cache/state-loop-snapshots`` so
    repeated tests of the same start state can be recognized as cache hits. Also
    owns the cold-vs-warm restore timing that quantifies the state-management win.
    """

    def __init__(self, session: StateSession, *, cache_root: str | Path = ".warptest-cache") -> None:
        self.session = session
        self.cache_dir = Path(cache_root).expanduser().resolve() / "state-loop-snapshots"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, dict[str, Any]] = {}

    def fingerprint(self, spec: dict[str, Any]) -> str:
        return state_fingerprint(self.session.git_commit, spec)

    def _snapshot_path(self, fingerprint: str) -> Path:
        return self.cache_dir / f"{fingerprint}.json"

    def load_snapshot(self, fingerprint: str) -> dict[str, Any] | None:
        """Return the generic runtime-state snapshot for a fingerprint, if cached."""
        if fingerprint in self._memory:
            return self._memory[fingerprint]
        path = self._snapshot_path(fingerprint)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        snapshot = data.get("runtime_state") if isinstance(data, dict) else None
        if isinstance(snapshot, dict):
            self._memory[fingerprint] = snapshot
            return snapshot
        return None

    def save_snapshot(self, fingerprint: str, spec: dict[str, Any], runtime_state: dict[str, Any]) -> None:
        if not runtime_state:
            return
        self._memory[fingerprint] = runtime_state
        payload = {
            "fingerprint": fingerprint,
            "git_commit": self.session.git_commit,
            "benchmark": spec.get("benchmark"),
            "target": spec.get("target"),
            "checkpoint": spec.get("checkpoint"),
            "runtime_state": runtime_state,
        }
        self._snapshot_path(fingerprint).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def restore_before(
        self,
        spec: dict[str, Any],
        spec_path: str | Path,
        *,
        screenshot_name: str = "before.png",
        cold_restore_ms: int | None = None,
        probe_fast_vs_full: bool = True,
    ) -> tuple[RestoreOutcome, RestoreTiming]:
        """Reach the restored *before* state on the warm session.

        The full restore is authoritative and captures a fresh snapshot.  When
        ``probe_fast_vs_full`` is enabled, the method then performs one duplicate
        fast restore to measure the like-for-like speedup. ``cold_restore_ms`` is
        combined with the warm restore only for the optional diagnostic report.
        """

        fingerprint = self.fingerprint(spec)
        timing = RestoreTiming(
            session_bootstrap_ms=self.session.bootstrap_ms,
            cold_restore_ms=cold_restore_ms,
        )
        # 1) Full warm restore to the before-state (authoritative; captures snapshot).
        full = self.session.restore(spec, spec_path, actions=[], screenshot_name=screenshot_name)
        timing.warm_restore_ms = full.elapsed_ms
        if full.status == "success" and full.runtime_state:
            self.save_snapshot(fingerprint, spec, full.runtime_state)
        if timing.cold_restore_ms and timing.warm_restore_ms:
            timing.speedup = round(timing.cold_restore_ms / max(1, timing.warm_restore_ms), 3)
        before = full
        # 2) Optional apples-to-apples diagnostic: re-enter the SAME before-state
        #    (no smoke) from the just-captured snapshot.  Formal C1 E trials disable
        #    this duplicate restore; restore_after still uses the validated snapshot
        #    operationally when available.
        snapshot = self.load_snapshot(fingerprint) if probe_fast_vs_full else None
        if (
            probe_fast_vs_full
            and full.status == "success"
            and snapshot is not None
            and self.session.supports_snapshot()
        ):
            timing.cache_hit = True
            fast = self.session.fast_restore(spec, spec_path, snapshot, actions=[], screenshot_name=screenshot_name)
            if fast.status == "success":
                timing.fast_restore_used = True
                timing.fast_restore_ms = fast.elapsed_ms
                if timing.warm_restore_ms and timing.fast_restore_ms:
                    timing.fast_vs_full_speedup = round(timing.warm_restore_ms / max(1, timing.fast_restore_ms), 3)
                before = fast  # fast result is correct (validated) and proves the path works
            else:
                timing.fast_restore_fallback = True
        return before, timing

    def restore_after(
        self,
        spec: dict[str, Any],
        spec_path: str | Path,
        *,
        actions: list[dict[str, Any]] | None,
        assertions: list[dict[str, Any]] | None = None,
        screenshot_name: str = "after.png",
    ) -> tuple[RestoreOutcome, RestoreTiming]:
        """Reach the *after* state (restore + smoke) on the warm session.

        Operationally uses fast-restore (snapshot inject + smoke) on a cache hit to
        skip the multi-step restore, falling back to a full restore on a validation
        miss. The fair fast-vs-full speedup is reported by :meth:`restore_before`
        (no-smoke, like-for-like); here we just record which path produced the after
        screenshot and its time.

        ``assertions`` overrides the spec's oracle checks on both routes, so the
        caller can grade the goal with the registered oracle while still executing
        the candidate's own transition. It is passed as a keyword rather than
        merged into ``spec`` because the snapshot fingerprint is taken over the
        whole spec: rewriting the spec here would miss the cache that
        :meth:`restore_before` populated and silently destroy the fast path.
        """

        fingerprint = self.fingerprint(spec)
        snapshot = self.load_snapshot(fingerprint)
        timing = RestoreTiming(session_bootstrap_ms=self.session.bootstrap_ms, cache_hit=snapshot is not None)
        if snapshot is not None and self.session.supports_snapshot():
            fast = self.session.fast_restore(
                spec,
                spec_path,
                snapshot,
                actions=actions,
                assertions=assertions,
                screenshot_name=screenshot_name,
            )
            if fast.status == "success":
                timing.fast_restore_used = True
                timing.fast_restore_ms = fast.elapsed_ms
                return fast, timing
            if fast.failure_phase == "oracle_result":
                # The goal genuinely was not reached; replaying it as a full
                # restore would burn a minute to reach the same verdict.
                timing.fast_restore_used = True
                timing.fast_restore_ms = fast.elapsed_ms
                return fast, timing
            timing.fast_restore_fallback = True
        outcome = self.session.restore(
            spec,
            spec_path,
            actions=actions,
            assertions=assertions,
            screenshot_name=screenshot_name,
            require_after=True,
        )
        timing.warm_restore_ms = outcome.elapsed_ms
        return outcome, timing


def measure_cold_restore(
    repo: str | Path,
    spec: dict[str, Any],
    spec_path: str | Path,
    *,
    report_dir: str | Path,
    headless: bool = True,
    experiment_seed: int | None = None,
) -> int | None:
    """Time a fresh runtime restore (state management OFF) for the speedup baseline.

    Opens a throwaway :class:`StateSession`, restores to the target with no smoke,
    and returns ``bootstrap + restore`` ms -- the per-iteration cost a naive loop
    pays every time. Must be called when no other warm session is open in this
    thread (one sync Playwright instance at a time).
    """

    cold = StateSession(
        repo,
        report_dir,
        headless=headless,
        experiment_seed=experiment_seed,
    )
    try:
        cold.start()
    except Exception:
        return None
    try:
        outcome = cold.restore(spec, spec_path, actions=[], screenshot_name="cold.png")
    finally:
        cold.close()
    if outcome.status != "success":
        return None
    if cold.bootstrap_ms is None:
        return outcome.elapsed_ms
    return cold.bootstrap_ms + outcome.elapsed_ms
