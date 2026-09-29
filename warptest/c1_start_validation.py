"""Target-only C1 start-state validation without a GUI policy or model calls.

This runner is intentionally narrower than the formal C/D/E experiment.  It
loads the registered oracle checkpoint for each selected task, restores only
the target state, obtains a live semantic observation and captures one local
start-state image.  It never executes the registered transition, evaluates the
goal, constructs a GUI agent, calls an LLM provider, or invokes a visual judge.

Sessions are reused for the two selected tasks from each game so a lightweight
11-game validation pays roughly one runtime bootstrap per game rather than one
bootstrap per task.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import yaml
from PIL import Image, ImageStat

from .c1_desktop_runtime import desktop_session_for
from .c1_openra_runtime import (
    OpenRAGameplaySession,
    OpenRASettingsSession,
    target_settings,
)
from .c1_unity_runtime import create_c1_unity_session
from .c1_web_runtime import create_c1_web_session
from .c1_background_compatibility import (
    LuantiGuiRuntime,
    background_pixel_roundtrip,
)
from .luanti_state import LuantiWarmSession
from .native_input import native_runtime_lock
from .runner import load_spec
from .runtime_lock import runtime_lease
from .state_store import StateSession


DEFAULT_MANIFEST = Path(
    "benchmarks/p5/c1_lightweight_gpt56_terra_kimi_k26.yaml"
)
DEFAULT_TASKS = Path("benchmarks/p5/c1_tasks.yaml")
DEFAULT_REPORT_DIR = Path("reports/warptest/c1-start-only-11game")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StartTask:
    id: str
    benchmark: str
    repo: Path
    oracle_spec: Path
    preconditions: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(_json_safe(payload), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _mapping_file(path: Path, *, label: str) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must contain a mapping: {path}")
    return loaded


def load_selected_tasks(
    root: str | Path,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    tasks_path: str | Path = DEFAULT_TASKS,
) -> tuple[list[StartTask], Path, Path]:
    """Load the exact ordered task subset registered by a C1 run manifest."""

    base = Path(root).expanduser().resolve()
    manifest_file = _resolve(base, manifest_path)
    tasks_file = _resolve(base, tasks_path)
    manifest = _mapping_file(manifest_file, label="C1 manifest")
    task_payload = _mapping_file(tasks_file, label="C1 task registry")

    selected = manifest.get("c1_task_ids")
    select_registered_universe = selected is None and isinstance(
        manifest.get("c1_task_count"), int
    )
    if select_registered_universe:
        selected_ids: list[str] = []
    else:
        if not isinstance(selected, list) or not selected or any(
            not isinstance(item, str) or not item.strip() for item in selected
        ):
            raise ValueError("C1 manifest c1_task_ids must be a non-empty string list")
        selected_ids = [item.strip() for item in selected]
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("C1 manifest c1_task_ids contains duplicates")

    registered = task_payload.get("tasks")
    if not isinstance(registered, list):
        raise ValueError(f"C1 task registry must contain a tasks list: {tasks_file}")
    by_id: dict[str, Mapping[str, Any]] = {}
    for raw in registered:
        if not isinstance(raw, Mapping):
            continue
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if task_id in by_id:
            raise ValueError(f"C1 task registry contains duplicate id: {task_id}")
        by_id[task_id] = raw

    if select_registered_universe:
        selected_ids = list(by_id)
        if len(selected_ids) != int(manifest["c1_task_count"]):
            raise ValueError(
                "C1 registered task universe does not match manifest c1_task_count"
            )
        declared_counts = manifest.get("c1_benchmark_task_counts")
        observed_counts = Counter(
            str(raw.get("benchmark")) for raw in by_id.values()
        )
        if isinstance(declared_counts, dict) and dict(observed_counts) != {
            str(name): int(count) for name, count in declared_counts.items()
        }:
            raise ValueError(
                "C1 registered task universe does not match benchmark task counts"
            )

    missing = [task_id for task_id in selected_ids if task_id not in by_id]
    if missing:
        raise ValueError("C1 manifest selects unknown tasks: " + ", ".join(missing))

    tasks: list[StartTask] = []
    for task_id in selected_ids:
        raw = by_id[task_id]
        benchmark = raw.get("benchmark")
        repo = raw.get("repo")
        oracle_spec = raw.get("oracle_spec")
        preconditions = raw.get("preconditions")
        if not all(isinstance(item, str) and item.strip() for item in (benchmark, repo, oracle_spec, preconditions)):
            raise ValueError(f"C1 task {task_id} has incomplete start metadata")
        repo_path = _resolve(base, str(repo))
        spec_path = _resolve(base, str(oracle_spec))
        if not repo_path.is_dir():
            raise ValueError(f"C1 task {task_id} repo is missing: {repo_path}")
        if not spec_path.is_file():
            raise ValueError(f"C1 task {task_id} oracle spec is missing: {spec_path}")
        tasks.append(
            StartTask(
                id=task_id,
                benchmark=str(benchmark),
                repo=repo_path,
                oracle_spec=spec_path,
                preconditions=str(preconditions),
            )
        )
    return tasks, manifest_file, tasks_file


def validate_lightweight_matrix(tasks: Sequence[StartTask]) -> dict[str, int]:
    """Fail closed unless this is the requested two-by-ten task matrix."""

    counts = Counter(task.benchmark for task in tasks)
    if len(tasks) != 20 or len(counts) != 10 or any(count != 2 for count in counts.values()):
        raise ValueError(
            "start-only validation requires exactly 20 tasks: two tasks from each of 10 games; "
            f"observed {len(tasks)} tasks with counts {dict(counts)}"
        )
    return dict(counts)


def _start_spec(task: StartTask) -> dict[str, Any]:
    spec = copy.deepcopy(load_spec(task.oracle_spec))
    # This is the central safety boundary.  Even runtimes that defensively strip
    # these fields receive a target-only document from this caller.
    spec["actions"] = []
    spec["assertions"] = []
    return spec


def _image_evidence(path_value: str | Path | None) -> dict[str, Any]:
    if not path_value:
        return {
            "path": None,
            "exists": False,
            "informative": False,
            "reason": "screenshot path is missing",
        }
    path = Path(path_value).expanduser().resolve()
    evidence: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "informative": False,
    }
    if not path.is_file():
        evidence["reason"] = "screenshot file is missing"
        return evidence
    evidence["sha256"] = _sha256(path)
    evidence["bytes"] = path.stat().st_size
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            sample = rgb.copy()
            sample.thumbnail((128, 128))
            stats = ImageStat.Stat(sample)
            channel_span = [maximum - minimum for minimum, maximum in stats.extrema]
            evidence.update(
                width=rgb.width,
                height=rgb.height,
                channel_span=channel_span,
            )
            evidence["informative"] = bool(
                rgb.width >= 320
                and rgb.height >= 200
                and path.stat().st_size >= 2000
                and max(channel_span, default=0) >= 8
            )
            if not evidence["informative"]:
                evidence["reason"] = "capture is too small, flat, or empty"
    except Exception as error:
        evidence["reason"] = f"cannot inspect screenshot: {error}"
    return evidence


def _unity_start_screenshot(session: Any, task_dir: Path) -> tuple[str, dict[str, str] | None]:
    """Capture final GameView pixels; fail closed without a desktop fallback."""

    return session.capture_policy_frame(task_dir / "before.png"), None


def _pending_row(task: StartTask) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "repo": str(task.repo),
        "oracle_spec": str(task.oracle_spec),
        "spec_sha256": _sha256(task.oracle_spec),
        "preconditions": task.preconditions,
        "status": "pending",
        "passed": False,
        "runtime_started": False,
        "runtime_alive_at_observation": False,
        "semantic_start_ok": False,
        "actions_declared": len(load_spec(task.oracle_spec).get("actions") or []),
        "actions_submitted": 0,
        "assertions_submitted": 0,
        "assertions_evaluated": False,
        "transition_executed": False,
        "goal_evaluated": False,
        "gui_agent_invoked": False,
        "policy_model_calls": 0,
        "external_api_calls": 0,
        "external_api_attempts_observed": 0,
        "network_audit_status": "not_observed",
        "elapsed_ms": None,
    }


class StartValidationRun:
    def __init__(
        self,
        *,
        root: Path,
        manifest_path: Path,
        tasks_path: Path,
        report_dir: Path,
        tasks: Sequence[StartTask],
        benchmark_counts: Mapping[str, int],
        headless_browser: bool,
    ) -> None:
        self.root = root
        self.report_dir = report_dir
        self.artifact_path = report_dir / "start-only.json"
        self.tasks = list(tasks)
        self.rows = {task.id: _pending_row(task) for task in tasks}
        self.payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mode": "c1_target_only_start_validation",
            "complete": False,
            "passed": False,
            "started_at": _utc_now(),
            "finished_at": None,
            "root": str(root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "tasks_path": str(tasks_path),
            "tasks_sha256": _sha256(tasks_path),
            "validator_path": str(Path(__file__).resolve()),
            "validator_sha256": _sha256(Path(__file__).resolve()),
            "selected_task_ids": [task.id for task in tasks],
            "benchmark_task_counts": dict(benchmark_counts),
            "headless_browser": bool(headless_browser),
            "safety": {
                "gui_agent_invoked": False,
                "policy_model_calls": 0,
                "external_api_calls": 0,
                "credentials_read": False,
                "visual_judge_invoked": False,
                "transition_executed": False,
                "goal_evaluated": False,
                "registered_actions_submitted": 0,
                "registered_assertions_submitted": 0,
                "models_active": [],
                "reasoning_effort_active": False,
                "temperature_active": False,
                "preflight_required": False,
                "calibration_required": False,
            },
            "group_errors": [],
            "summary": {},
            "results": list(self.rows.values()),
        }
        self.flush()

    def flush(self) -> None:
        ordered = [self.rows[task.id] for task in self.tasks]
        counts = Counter(str(row.get("status")) for row in ordered)
        observed_attempts = sum(
            int(row.get("external_api_attempts_observed", 0) or 0)
            for row in ordered
        )
        self.payload["safety"]["external_api_calls"] = observed_attempts
        self.payload["safety"]["external_api_attempts_observed"] = observed_attempts
        self.payload["safety"]["network_audit_complete"] = all(
            row.get("network_audit_status") == "observed" for row in ordered
        )
        self.payload["safety"]["transition_executed"] = any(
            row.get("transition_executed") is True for row in ordered
        )
        self.payload["safety"]["goal_evaluated"] = any(
            row.get("goal_evaluated") is True for row in ordered
        )
        self.payload["safety"]["registered_assertions_evaluated"] = any(
            row.get("assertions_evaluated") is True for row in ordered
        )
        self.payload["results"] = ordered
        self.payload["summary"] = {
            "total": len(ordered),
            "passed": sum(row.get("passed") is True for row in ordered),
            "failed": sum(row.get("status") == "failure" for row in ordered),
            "pending": sum(row.get("status") == "pending" for row in ordered),
            "running": sum(row.get("status") == "running" for row in ordered),
            "interrupted": sum(row.get("status") == "interrupted" for row in ordered),
            "status_counts": dict(counts),
        }
        _atomic_json(self.artifact_path, self.payload)

    def update(self, task: StartTask, **fields: Any) -> None:
        self.rows[task.id].update(_json_safe(fields))
        self.flush()

    def fail_group(self, tasks: Sequence[StartTask], stage: str, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"
        self.payload["group_errors"].append(
            {
                "benchmark": tasks[0].benchmark if tasks else "unknown",
                "stage": stage,
                "detail": detail,
            }
        )
        for task in tasks:
            if self.rows[task.id]["status"] == "pending":
                self.update(task, status="failure", passed=False, failure_stage=stage, error=detail)

    def finish(self) -> dict[str, Any]:
        self.payload["finished_at"] = _utc_now()
        complete = all(
            row["status"] in {"success", "failure"} for row in self.rows.values()
        )
        self.payload["complete"] = complete
        self.payload["passed"] = bool(
            complete
            and not self.payload["group_errors"]
            and all(row.get("passed") is True for row in self.rows.values())
        )
        self.flush()
        return self.payload


def _run_task(
    run: StartValidationRun,
    task: StartTask,
    operation: Callable[[dict[str, Any], Path], Mapping[str, Any]],
) -> None:
    started = time.perf_counter()
    task_dir = run.report_dir / "tasks" / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    run.update(task, status="running")
    try:
        result = dict(operation(_start_spec(task), task_dir))
        runtime_started = result.get("runtime_started") is True
        semantic_ok = result.get("semantic_start_ok") is True
        runtime_alive = result.get("runtime_alive_at_observation") is True
        actions_not_executed = result.get("restore_actions_executed") is False
        assertions_not_evaluated = result.get("assertions_evaluated") is False
        goal_not_evaluated = result.get("goal_evaluated") is False
        network = result.get("network_api_calls")
        if not isinstance(network, Mapping):
            raise RuntimeError("runtime did not return network API audit evidence")
        attempted = network.get("attempted")
        attempted = attempted if isinstance(attempted, list) else []
        screenshot = _image_evidence(result.pop("screenshot", None))
        compatibility = result.get("background_pixel_compatibility")
        compatibility_ok = compatibility is None or bool(
            isinstance(compatibility, Mapping)
            and compatibility.get("passed") is True
            and compatibility.get("execution_backend") == "background_pixel_v1"
            and compatibility.get("foreground_required") is False
        )
        passed = bool(
            runtime_started
            and semantic_ok
            and runtime_alive
            and actions_not_executed
            and assertions_not_evaluated
            and goal_not_evaluated
            and not attempted
            and screenshot.get("informative") is True
            and compatibility_ok
        )
        result.update(
            screenshot=screenshot,
            background_pixel_compatibility_ok=compatibility_ok,
            runtime_started=runtime_started,
            semantic_start_ok=semantic_ok,
            runtime_alive_at_observation=runtime_alive,
            transition_executed=not actions_not_executed,
            assertions_evaluated=not assertions_not_evaluated,
            goal_evaluated=not goal_not_evaluated,
            status="success" if passed else "failure",
            passed=passed,
            network_api_calls=network,
            external_api_calls=len(attempted),
            external_api_attempts_observed=len(attempted),
            network_audit_status="observed",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        run.update(task, **result)
    except (KeyboardInterrupt, SystemExit) as error:
        run.update(
            task,
            status="interrupted",
            passed=False,
            failure_stage="target_only_start",
            error=f"{type(error).__name__}: {error}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    except Exception as error:
        run.update(
            task,
            status="failure",
            passed=False,
            failure_stage="target_only_start",
            error=f"{type(error).__name__}: {error}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _run_browser_group(
    run: StartValidationRun,
    tasks: Sequence[StartTask],
    *,
    headless: bool,
) -> None:
    session = StateSession(
        tasks[0].repo,
        run.report_dir / "sessions" / tasks[0].benchmark,
        headless=headless,
        experiment_seed=0,
    )
    try:
        session.start()
    except Exception as error:
        try:
            session.close()
        except Exception:
            pass
        run.fail_group(tasks, "session_start", error)
        return
    try:
        for task in tasks:
            def operation(spec: dict[str, Any], task_dir: Path, *, _task: StartTask = task) -> Mapping[str, Any]:
                outcome = session.restore_target_only(
                    spec,
                    _task.oracle_spec,
                    screenshot_name=str(task_dir / "before.png"),
                )
                phases = outcome.phase_statuses
                compatibility = background_pixel_roundtrip(
                    session.runtime, task_dir, image_evidence=_image_evidence
                )
                return {
                    "runtime_kind": "browser_state_session",
                    "runtime_started": True,
                    "runtime_alive_at_observation": session.runtime.server is not None
                    and session.runtime.server.poll() is None
                    and session.runtime.page is not None
                    and not session.runtime.page.is_closed(),
                    "semantic_start_ok": outcome.status == "success",
                    "semantic_start": outcome.to_dict(),
                    "restore_phase_statuses": phases,
                    "restore_actions_executed": False,
                    "assertions_evaluated": bool(
                        (outcome.report or {}).get("assertions_evaluated")
                    ),
                    "goal_evaluated": (outcome.report or {}).get("oracle_result")
                    != "not_run",
                    "seed_evidence": session.runtime.prng_metadata(),
                    "network_api_calls": session.runtime.network_api_calls,
                    "screenshot": outcome.screenshot,
                    "background_pixel_compatibility": compatibility,
                }

            _run_task(run, task, operation)
    finally:
        try:
            session.close()
        except Exception as error:
            run.fail_group(tasks, f"{tasks[0].benchmark}_session_close", error)


def _run_luanti_group(run: StartValidationRun, tasks: Sequence[StartTask]) -> None:
    session = LuantiWarmSession(
        tasks[0].repo,
        run.report_dir / "sessions" / "luanti",
        render=True,
        experiment_seed=0,
    )
    try:
        session.start()
    except Exception as error:
        try:
            session.close()
        except Exception:
            pass
        run.fail_group(tasks, "session_start", error)
        return
    try:
        gui_runtime = LuantiGuiRuntime(session)
        for task in tasks:
            def operation(spec: dict[str, Any], task_dir: Path, *, _task: StartTask = task) -> Mapping[str, Any]:
                outcome = session.restore(spec, actions=[], assertions=[])
                semantic = session.semantic_snapshot(_task.id, spec)
                screenshot = session.screenshot(str(task_dir / "before.png"))
                compatibility = background_pixel_roundtrip(
                    gui_runtime, task_dir, image_evidence=_image_evidence
                )
                return {
                    "runtime_kind": "luanti_warm_rendered",
                    "runtime_started": True,
                    "runtime_alive_at_observation": session.is_alive(),
                    "semantic_start_ok": outcome.status == "success"
                    and semantic.get("observed") is True,
                    "restore": outcome.to_dict(),
                    "semantic_start": semantic,
                    "seed_evidence": session.seed_metadata(),
                    "restore_actions_executed": False,
                    "assertions_evaluated": False,
                    "goal_evaluated": False,
                    "network_api_calls": getattr(
                        session._adapter,
                        "network_api_calls",
                        {"attempted": [], "source": "luanti_local_runtime"},
                    ),
                    "screenshot": screenshot,
                    "background_pixel_compatibility": compatibility,
                }

            _run_task(run, task, operation)
    finally:
        try:
            session.close()
        except Exception as error:
            run.fail_group(tasks, "luanti_session_close", error)


def _run_openra_group(run: StartValidationRun, tasks: Sequence[StartTask]) -> None:
    # Each task gets an isolated native process. Settings tasks boot the normal
    # shell with the requested public values; gameplay tasks boot the declared
    # map in interactive mode with actions/assertions stripped by _start_spec.
    for task in tasks:
        def operation(spec: dict[str, Any], task_dir: Path, *, _task: StartTask = task) -> Mapping[str, Any]:
            target = spec.get("target")
            if isinstance(target, dict) and target.get("kind") == "openra_map":
                runtime_context: Any = OpenRAGameplaySession(
                    _task.repo,
                    task_dir / "session",
                    spec=spec,
                    experiment_seed=0,
                )
                runtime_kind = "openra_live_gameplay"
                semantic_grade = lambda runtime: runtime.grade_target()
                network_source = "openra_offline_live_gameplay"
            else:
                requested = target_settings(spec)
                runtime_context = OpenRASettingsSession(
                    _task.repo,
                    task_dir / "session",
                    settings=requested,
                    experiment_seed=0,
                )
                runtime_kind = "openra_public_settings"
                semantic_grade = lambda runtime: runtime.grade_settings(requested)
                network_source = "openra_offline_public_settings_configuration"
            with runtime_context as runtime:
                grade = semantic_grade(runtime)
                screenshot = runtime.screenshot(task_dir / "before.png")
                compatibility = background_pixel_roundtrip(
                    runtime, task_dir, image_evidence=_image_evidence
                )
                return {
                    "runtime_kind": runtime_kind,
                    "runtime_started": True,
                    "runtime_alive_at_observation": runtime.is_alive(),
                    "semantic_start_ok": grade.ok,
                    "semantic_start": grade,
                    "seed_evidence": runtime.seed_evidence(_task.id),
                    "restore_actions_executed": False,
                    "assertions_evaluated": False,
                    "goal_evaluated": False,
                    "network_api_calls": {
                        "attempted": [],
                        "source": network_source,
                        "fetch_news": False,
                        "online_advertising": False,
                    },
                    "screenshot": screenshot,
                    "background_pixel_compatibility": compatibility,
                }

        _run_task(run, task, operation)


def _run_web_group(run: StartValidationRun, tasks: Sequence[StartTask]) -> None:
    session = create_c1_web_session(
        tasks[0].benchmark,
        tasks[0].repo,
        run.report_dir / "sessions" / tasks[0].benchmark,
        headless=True,
        require_headed=False,
    )
    try:
        session.start()
    except Exception as error:
        try:
            session.close()
        except Exception:
            pass
        run.fail_group(tasks, "session_start", error)
        return
    external_requests: list[str] = []
    web_runtime = getattr(session, "runtime", None)
    context = getattr(web_runtime, "context", None)

    def audit_request(request: Any) -> None:
        url = str(getattr(request, "url", ""))
        parsed = urlsplit(url)
        if parsed.scheme in {"data", "blob", "file", "about"}:
            return
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return
        external_requests.append(url)

    # JupyterLab creates its browser lazily during target restoration and records
    # requests in its runtime adapter.
    if context is not None:
        context.on("request", audit_request)
    try:
        for task in tasks:
            def operation(spec: dict[str, Any], task_dir: Path, *, _task: StartTask = task) -> Mapping[str, Any]:
                restore = session.restore_target_only(spec, _task.oracle_spec)
                semantic = session.semantic_start(spec)
                screenshot = session.screenshot(str(task_dir / "before.png"))
                compatibility = background_pixel_roundtrip(
                    session, task_dir, image_evidence=_image_evidence
                )
                runtime_network = getattr(web_runtime, "network_api_calls", {})
                runtime_attempted = (
                    runtime_network.get("attempted", [])
                    if isinstance(runtime_network, Mapping)
                    else []
                )
                attempted = list(
                    dict.fromkeys([*external_requests, *runtime_attempted])
                )
                return {
                    "runtime_kind": "c1_web_live",
                    "runtime_started": True,
                    "runtime_alive_at_observation": not session.page.is_closed(),
                    "semantic_start_ok": semantic.ok,
                    "restore": restore,
                    "semantic_start": semantic,
                    "restore_actions_executed": restore.actions_executed,
                    "assertions_evaluated": False,
                    "goal_evaluated": False,
                    "network_api_calls": {
                        "attempted": attempted,
                        "source": "c1_web_external_request_audit",
                        "local_app_url": session.app_url,
                    },
                    "screenshot": screenshot,
                    "background_pixel_compatibility": compatibility,
                }

            _run_task(run, task, operation)
    finally:
        try:
            session.close()
        except Exception as error:
            run.fail_group(tasks, f"{tasks[0].benchmark}_session_close", error)


def _run_desktop_group(run: StartValidationRun, tasks: Sequence[StartTask]) -> None:
    session = desktop_session_for(
        tasks[0].benchmark,
        tasks[0].repo,
        run.report_dir / "sessions" / tasks[0].benchmark,
    )
    try:
        session.start_clean()
    except Exception as error:
        try:
            session.close()
        except Exception:
            pass
        run.fail_group(tasks, "session_start", error)
        return
    try:
        liveness = session.probe_liveness()
        for task in tasks:
            def operation(spec: dict[str, Any], task_dir: Path) -> Mapping[str, Any]:
                restore = session.restore_target(spec)
                if restore.get("status") != "success":
                    raise RuntimeError(
                        f"{task.benchmark} calibration target restore failed: {restore}"
                    )
                semantic = session.probe_start(spec)
                desktop_shot = session.screenshot(task_dir / "desktop-before.png")
                screenshot = desktop_shot
                render_proof = None
                if task.benchmark.strip().lower() == "blender":
                    render_proof = session.render_proof(
                        task_dir / "before.png",
                        canonical_spec=spec,
                    )
                    screenshot = render_proof
                compatibility = background_pixel_roundtrip(
                    session, task_dir, image_evidence=_image_evidence
                )
                return {
                    "runtime_kind": "c1_native_desktop",
                    "runtime_started": True,
                    "runtime_alive_at_observation": liveness.get("passed") is True
                    and session.process is not None
                    and session.process.poll() is None,
                    "runtime_liveness": liveness,
                    "semantic_start_ok": semantic.get("passed") is True,
                    "restore": restore,
                    "semantic_start": semantic,
                    "desktop_screenshot": desktop_shot,
                    "render_proof": render_proof,
                    "restore_actions_executed": False,
                    "assertions_evaluated": False,
                    "goal_evaluated": False,
                    "network_api_calls": {
                        "attempted": [],
                        "source": "native_desktop_target_only_no_provider_client",
                    },
                    "screenshot": screenshot,
                    "background_pixel_compatibility": compatibility,
                }

            _run_task(run, task, operation)
    finally:
        try:
            session.close()
        except Exception as error:
            run.fail_group(tasks, f"{tasks[0].benchmark}_session_close", error)


def _run_unity_group(run: StartValidationRun, tasks: Sequence[StartTask]) -> None:
    session = create_c1_unity_session(
        tasks[0].benchmark,
        tasks[0].repo,
        run.report_dir / "sessions" / tasks[0].benchmark,
        experiment_seed=0,
        acquire_native_lock=False,
    )
    try:
        session.start()
    except Exception as error:
        try:
            session.close()
        except Exception:
            pass
        run.fail_group(tasks, "session_start", error)
        return
    try:
        for task in tasks:
            def operation(spec: dict[str, Any], task_dir: Path, *, _task: StartTask = task) -> Mapping[str, Any]:
                restore = session.restore_target_only(spec, _task.oracle_spec)
                semantic = session.semantic_start(spec)
                screenshot, screenshot_fallback = _unity_start_screenshot(
                    session, task_dir
                )
                compatibility = background_pixel_roundtrip(
                    session, task_dir, image_evidence=_image_evidence
                )
                return {
                    "runtime_kind": "c1_unity_live",
                    "runtime_started": True,
                    "runtime_alive_at_observation": session.is_alive(),
                    "semantic_start_ok": semantic.get("ok") is True,
                    "restore": restore,
                    "semantic_start": semantic,
                    "restore_actions_executed": restore.get("actions_executed"),
                    "assertions_evaluated": False,
                    "goal_evaluated": False,
                    "seed_evidence": session.seed_evidence(_task.id),
                    "network_api_calls": session.network_api_calls,
                    "screenshot_metadata": session.last_screenshot_metadata,
                    "screenshot_fallback": screenshot_fallback,
                    "screenshot": screenshot,
                    "background_pixel_compatibility": compatibility,
                }

            _run_task(run, task, operation)
    finally:
        try:
            session.close()
        except Exception as error:
            run.fail_group(tasks, f"{tasks[0].benchmark}_session_close", error)


def run_start_validation(
    root: str | Path = Path.cwd(),
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    tasks_path: str | Path = DEFAULT_TASKS,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    headless_browser: bool = True,
    selected_task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    registered_tasks, manifest_file, tasks_file = load_selected_tasks(
        base,
        manifest_path,
        tasks_path,
    )
    if selected_task_ids is not None:
        requested = [str(task_id).strip() for task_id in selected_task_ids]
        if (
            not requested
            or any(not task_id for task_id in requested)
            or len(set(requested)) != len(requested)
        ):
            raise ValueError("--task-id values must be unique non-empty task ids")
        by_id = {task.id: task for task in registered_tasks}
        missing = [task_id for task_id in requested if task_id not in by_id]
        if missing:
            raise ValueError("unknown selected task ids: " + ", ".join(missing))
        tasks = [by_id[task_id] for task_id in requested]
        counts = dict(Counter(task.benchmark for task in tasks))
        selection_mode = "explicit_subset"
    else:
        tasks = registered_tasks
        observed = dict(Counter(task.benchmark for task in tasks))
        manifest_payload = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
        expected_full_count = (
            manifest_payload.get("c1_task_count")
            if isinstance(manifest_payload, dict)
            else None
        )
        if len(tasks) == expected_full_count and len(observed) == 10:
            counts = observed
            selection_mode = "registered_full_c1_background_compatibility_matrix"
        else:
            counts = validate_lightweight_matrix(tasks)
            selection_mode = "registered_two_by_ten_matrix"
    destination = _resolve(base, report_dir)
    destination.mkdir(parents=True, exist_ok=True)
    run = StartValidationRun(
        root=base,
        manifest_path=manifest_file,
        tasks_path=tasks_file,
        report_dir=destination,
        tasks=tasks,
        benchmark_counts=counts,
        headless_browser=headless_browser,
    )
    run.payload["selection_mode"] = selection_mode
    run.flush()

    grouped: dict[tuple[str, Path], list[StartTask]] = {}
    for task in tasks:
        grouped.setdefault((task.benchmark, task.repo), []).append(task)

    for (benchmark, repo), group in grouped.items():
        normalized = benchmark.strip().lower()
        try:
            with runtime_lease(repo):
                if normalized in {"2048", "datingsim", "spacerpg"}:
                    _run_browser_group(run, group, headless=headless_browser)
                elif normalized in {"luanti", "minetest"}:
                    with native_runtime_lock():
                        _run_luanti_group(run, group)
                elif normalized == "openra":
                    with native_runtime_lock():
                        _run_openra_group(run, group)
                elif normalized == "jupyterlab":
                    _run_web_group(run, group)
                elif normalized in {"blender", "libreoffice"}:
                    # Both desktop session classes acquire the global native lock.
                    _run_desktop_group(run, group)
                elif normalized in {"jynew", "pal3", "pal3.unity"}:
                    _run_unity_group(run, group)
                else:
                    run.fail_group(
                        group,
                        "dispatch",
                        ValueError(f"unsupported C1 benchmark: {benchmark}"),
                    )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            run.fail_group(group, "runtime_lease", error)
    return run.finish()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate C1 target-only starts and zero-model background-pixel "
            "capture/input/capture compatibility."
        )
    )
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="Run only this registered task id; repeat for a bounded recovery subset.",
    )
    parser.add_argument(
        "--headed-browser",
        action="store_true",
        help="Show classic browser-game windows; native/web C1 runtimes remain headed regardless.",
    )
    args = parser.parse_args(argv)
    result = run_start_validation(
        args.root,
        manifest_path=args.manifest,
        tasks_path=args.tasks,
        report_dir=args.report_dir,
        headless_browser=not args.headed_browser,
        selected_task_ids=args.task_ids,
    )
    print(
        json.dumps(
            {
                "complete": result["complete"],
                "passed": result["passed"],
                "summary": result["summary"],
                "artifact": str(_resolve(Path(args.root).resolve(), args.report_dir) / "start-only.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
