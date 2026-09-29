from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .jupyterlab_runtime import JupyterLabRuntimeAdapter


class C1WebRuntimeError(RuntimeError):
    """Raised when a C1 web session cannot prove a live, target-only state."""


@dataclass(frozen=True)
class C1WebRestoreResult:
    benchmark: str
    phase: str
    app_url: str
    detail: str
    artifacts: dict[str, Any]
    observation: dict[str, Any]
    actions_executed: bool = False
    status: str = "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class C1WebSemanticGrade:
    ok: bool
    status: str
    source: str
    detail: str
    observation: dict[str, Any]
    fingerprint: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class C1WebBindingResult:
    """Read-only binding of an Agent-S-produced live start to its grader spec."""

    benchmark: str
    app_url: str
    detail: str
    artifacts: dict[str, Any]
    observation: dict[str, Any]
    actions_executed: bool = False
    app_state_mutated: bool = False
    status: str = "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class C1WebTransitionResult:
    benchmark: str
    status: str
    detail: str
    artifacts: dict[str, Any]
    observation: dict[str, Any]
    action_count: int
    actions_executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class C1WebSession(Protocol):
    benchmark: str

    @property
    def page(self) -> Any: ...

    @property
    def app_url(self) -> str: ...

    def start(self) -> "C1WebSession": ...

    def clean_entry(self) -> C1WebRestoreResult: ...

    def restore_target_only(
        self,
        spec: dict[str, Any],
        spec_path: str | Path | None = None,
    ) -> C1WebRestoreResult: ...

    def bind_live_target_for_grading(
        self,
        spec: dict[str, Any],
        spec_path: str | Path | None = None,
    ) -> C1WebBindingResult: ...

    def screenshot(self, name: str, *, full_page: bool = False) -> str: ...

    def semantic_start(self, spec: dict[str, Any]) -> C1WebSemanticGrade: ...

    def apply_registered_transition(
        self, spec: dict[str, Any]
    ) -> C1WebTransitionResult: ...

    def semantic_goal(self, spec: dict[str, Any]) -> C1WebSemanticGrade: ...

    def close(self) -> None: ...


class _BaseC1WebSession:
    benchmark = ""
    semantic_source = ""
    capture_backend = "playwright_page_screenshot_v1"
    input_backend = "playwright_page_input_v1"
    execution_backend = "background_pixel_v1"
    foreground_required = False
    coordinate_space = "frame_pixels_top_left_v1"

    def __init__(self, *, require_headed: bool = True) -> None:
        self.require_headed = require_headed
        self.session_id = uuid.uuid4().hex
        self.last_policy_frame_id: int | None = None
        self._policy_frame_sequence = 0

    def __enter__(self) -> "_BaseC1WebSession":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def page(self) -> Any:
        page = getattr(self.runtime, "page", None)
        if page is None or page.__class__.__name__ == "_NoOpPage":
            raise C1WebRuntimeError(f"{self.benchmark} C1 session has no live browser page")
        return page

    @property
    def app_url(self) -> str:
        value = getattr(self.runtime, "app_url", None)
        if not isinstance(value, str) or not value:
            raise C1WebRuntimeError(f"{self.benchmark} C1 session has no live app URL")
        return value

    def _grade(self, result: dict[str, Any], *, phase: str) -> C1WebSemanticGrade:
        observation = result.get("observation")
        if not isinstance(observation, dict):
            observation = {}
        ok = result.get("ok") is True
        status = str(result.get("status") or ("success" if ok else "validate_failed"))
        detail = str(result.get("detail") or f"{phase} semantic validation {'passed' if ok else 'failed'}")
        fingerprint = _semantic_fingerprint(observation) if observation else None
        return C1WebSemanticGrade(
            ok=ok,
            status=status,
            source=self.semantic_source,
            detail=detail,
            observation=observation,
            fingerprint=fingerprint,
        )

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return 1280, 800

    def capture_policy_frame(self, path: str | Path) -> str:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(destination), timeout=10_000)
        self._policy_frame_sequence += 1
        self.last_policy_frame_id = self._policy_frame_sequence
        return str(destination)

    def after_input_batch(self, batch: Any) -> None:
        del batch


class JupyterLabC1WebSession(_BaseC1WebSession):
    benchmark = "JupyterLab"
    semantic_source = "jupyterlab_contents_kernel_live_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        headless: bool = False,
        require_headed: bool = True,
        runtime: JupyterLabRuntimeAdapter | None = None,
    ) -> None:
        super().__init__(require_headed=require_headed)
        self.runtime = runtime or JupyterLabRuntimeAdapter(
            Path(repo), Path(report_dir), headless=headless
        )

    def start(self) -> "JupyterLabC1WebSession":
        self.runtime.start_c1_session(require_headed=self.require_headed)
        return self

    def clean_entry(self) -> C1WebRestoreResult:
        result = self.runtime.clean_c1_entry(require_headed=self.require_headed)
        return _restore_result(self.benchmark, "clean_entry", self.app_url, result)

    def restore_target_only(
        self,
        spec: dict[str, Any],
        spec_path: str | Path | None = None,
    ) -> C1WebRestoreResult:
        result = self.runtime.restore_c1_target(
            spec,
            Path(spec_path).expanduser().resolve() if spec_path is not None else None,
            require_headed=self.require_headed,
        )
        return _restore_result(self.benchmark, "target_only", self.app_url, result)

    def bind_live_target_for_grading(
        self,
        spec: dict[str, Any],
        spec_path: str | Path | None = None,
    ) -> C1WebBindingResult:
        result = self.runtime.bind_c1_live_target(
            spec,
            Path(spec_path).expanduser().resolve() if spec_path is not None else None,
            require_headed=self.require_headed,
        )
        return _binding_result(self.benchmark, self.app_url, result)

    def screenshot(self, name: str, *, full_page: bool = False) -> str:
        return self.runtime.screenshot_c1_live(name, full_page=full_page)

    def semantic_start(self, spec: dict[str, Any]) -> C1WebSemanticGrade:
        return self._grade(self.runtime.grade_c1_start(spec), phase="start")

    def apply_registered_transition(
        self, spec: dict[str, Any]
    ) -> C1WebTransitionResult:
        return _transition_result(
            self.benchmark,
            self.runtime.apply_c1_state_transition(spec),
        )

    def semantic_goal(self, spec: dict[str, Any]) -> C1WebSemanticGrade:
        return self._grade(self.runtime.grade_c1_goal(spec), phase="goal")

    def close(self) -> None:
        self.runtime.close()


def create_c1_web_session(
    benchmark: str,
    repo: str | Path,
    report_dir: str | Path,
    *,
    headless: bool = False,
    require_headed: bool = True,
) -> C1WebSession:
    normalized = benchmark.strip().lower().replace("-", "").replace("_", "")
    if normalized == "jupyterlab":
        return JupyterLabC1WebSession(
            repo,
            report_dir,
            headless=headless,
            require_headed=require_headed,
        )
    raise C1WebRuntimeError(f"Unsupported C1 web benchmark: {benchmark!r}")


def _restore_result(
    benchmark: str,
    phase: str,
    app_url: str,
    result: dict[str, Any],
) -> C1WebRestoreResult:
    if result.get("ok") is not True:
        raise C1WebRuntimeError(
            str(result.get("detail") or f"{benchmark} {phase} did not produce a proven live state")
        )
    if result.get("actions_executed") is not False:
        raise C1WebRuntimeError(
            f"{benchmark} {phase} did not attest target-only restore (actions_executed=false)"
        )
    observation = result.get("observation")
    if not isinstance(observation, dict) or not observation:
        raise C1WebRuntimeError(f"{benchmark} {phase} returned no live observation")
    artifacts = result.get("artifacts")
    return C1WebRestoreResult(
        benchmark=benchmark,
        phase=phase,
        app_url=app_url,
        detail=str(result.get("detail") or "Live target-only state is ready."),
        artifacts=artifacts if isinstance(artifacts, dict) else {},
        observation=observation,
    )


def _semantic_fingerprint(observation: dict[str, Any]) -> str:
    payload = json.dumps(
        observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _binding_result(
    benchmark: str,
    app_url: str,
    result: dict[str, Any],
) -> C1WebBindingResult:
    if result.get("ok") is not True:
        raise C1WebRuntimeError(
            str(result.get("detail") or f"{benchmark} live target binding failed")
        )
    if result.get("actions_executed") is not False:
        raise C1WebRuntimeError(
            f"{benchmark} live target binding did not attest actions_executed=false"
        )
    if result.get("app_state_mutated") is not False:
        raise C1WebRuntimeError(
            f"{benchmark} live target binding did not attest app_state_mutated=false"
        )
    observation = result.get("observation")
    if not isinstance(observation, dict) or not observation:
        raise C1WebRuntimeError(
            f"{benchmark} live target binding returned no live observation"
        )
    artifacts = result.get("artifacts")
    return C1WebBindingResult(
        benchmark=benchmark,
        app_url=app_url,
        detail=str(
            result.get("detail")
            or "Agent-S-produced live target is bound for read-only grading."
        ),
        artifacts=artifacts if isinstance(artifacts, dict) else {},
        observation=observation,
    )


def _transition_result(
    benchmark: str,
    result: dict[str, Any],
) -> C1WebTransitionResult:
    if result.get("ok") is not True or result.get("status") != "success":
        raise C1WebRuntimeError(
            str(result.get("detail") or f"{benchmark} registered State transition failed")
        )
    if result.get("actions_executed") is not True:
        raise C1WebRuntimeError(
            f"{benchmark} registered State transition did not attest actions_executed=true"
        )
    action_count = result.get("action_count")
    if not isinstance(action_count, int) or isinstance(action_count, bool) or action_count < 1:
        raise C1WebRuntimeError(
            f"{benchmark} registered State transition returned an invalid action_count"
        )
    observation = result.get("observation")
    if not isinstance(observation, dict) or not observation:
        raise C1WebRuntimeError(
            f"{benchmark} registered State transition returned no live observation"
        )
    artifacts = result.get("artifacts")
    return C1WebTransitionResult(
        benchmark=benchmark,
        status="success",
        detail=str(result.get("detail") or "Registered State transition completed."),
        artifacts=artifacts if isinstance(artifacts, dict) else {},
        observation=observation,
        action_count=action_count,
    )


__all__ = [
    "C1WebBindingResult",
    "C1WebRestoreResult",
    "C1WebRuntimeError",
    "C1WebSemanticGrade",
    "C1WebSession",
    "C1WebTransitionResult",
    "JupyterLabC1WebSession",
    "create_c1_web_session",
]
