"""Persistent, parent-owned bridge for the pinned Agent-S S3 baseline.

Formal C1 execution uses the ``background_pixel_v1`` contract: the child runs
the pinned Agent-S trajectory/reflection and visual grounding logic, but returns
only bounded structured mouse/keyboard actions.  The parent-owned runtime is
the sole action executor.  No model-authored Python is evaluated or executed in
that mode, and the worker never owns desktop focus, capture, or input.

No API key is accepted by this module's public configuration.  Key *environment
variable names* cross the JSONL boundary and the worker resolves their values from
its inherited environment.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Mapping, Sequence

from . import cost
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S
from .c1_task_deadline import (
    bounded_task_timeout_s,
    current_task_deadline,
    raise_if_task_timed_out,
)
from .model_request import MODEL_REQUEST_PROFILES_ENV, canonical_profiles_json


AGENT_S_PROTOCOL_VERSION = 2
BACKGROUND_PIXEL_BACKEND = "background_pixel_v1"
FOREGROUND_NATIVE_BACKEND = "foreground_native_v1"
FRAME_PIXEL_COORDINATE_SPACE = "frame_pixels_top_left_v1"
MAX_INPUT_BATCH_ACTIONS = 64
MAX_INPUT_TEXT_CHARS = 4096
MAX_INPUT_ACTION_SECONDS = 5.0
PINNED_AGENT_S_VERSION = "0.3.2"
PINNED_AGENT_S_COMMIT = "ae5ff9a577ecfcd54d865acc471de91e5560392e"
AGENT_S_PROVIDER_TIMEOUT_S = C1_PROVIDER_TIMEOUT_S
AGENT_S_REQUEST_MAX_TOKENS = 8192
AGENT_S_CONTROL_REQUEST_TIMEOUT_S = 360.0
AGENT_S_PREDICT_TIMEOUT_MODE = "child_provider_bounded_no_parent_deadline"
# Pinned S3's worst registered call graph is 3 reflection attempts + 9 formatted
# decision attempts + 8 drag-grounding attempts + one shared reasoning retry +
# two sticky temperature-rejection fallbacks (decision/reflection and grounder).
AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT = 23

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_SECRET_HEADER_NAMES = {"x-end-user", "x-custom-source"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AgentSBridgeError(RuntimeError):
    """Base error raised by the Agent-S bridge."""


class AgentSProtocolError(AgentSBridgeError):
    """Raised when the JSONL child violates the bridge protocol."""


class AgentSWorkerError(AgentSBridgeError):
    """Raised when the child reports an operation failure."""


class AgentSUsageAccountingError(AgentSProtocolError):
    """A paid prediction may have occurred but its final usage is unavailable."""

    usage_accounting_complete = False


@dataclass(frozen=True)
class AgentSBridgeConfig:
    """Non-secret configuration for one persistent Agent-S S3 process."""

    model: str
    base_url: str
    api_key_env: str
    grounder_model: str
    grounder_base_url: str
    grounder_api_key_env: str
    default_headers: Mapping[str, str] = field(default_factory=dict)
    grounder_default_headers: Mapping[str, str] = field(default_factory=dict)
    temperature: float | None = 0.2
    platform: str = "darwin"
    max_trajectory_length: int = 8
    timeout_s: float = AGENT_S_PROVIDER_TIMEOUT_S
    # SDK-internal retries cannot be assigned usage records.  The parent/task
    # retry layer is persistent and auditable, so worker transports are single-shot.
    max_retries: int = 0
    # Real desktop pixel dimensions the parent screenshots and pyautogui acts in.
    # There is no separate scaled "grounding" resolution: the grounding model sees
    # the same real screenshot as the decision/reflection models.
    native_screen_width: int = 0
    native_screen_height: int = 0
    # Keep direct API callers on the explicit diagnostic backend for backwards
    # compatibility. Formal C/D always passes background_pixel_v1 in the manifest
    # path; it is never selected implicitly from this default.
    execution_backend: str = FOREGROUND_NATIVE_BACKEND
    coordinate_space: str = FRAME_PIXEL_COORDINATE_SPACE

    def validated(self) -> "AgentSBridgeConfig":
        for label, value in (
            ("model", self.model),
            ("base_url", self.base_url),
            ("grounder_model", self.grounder_model),
            ("grounder_base_url", self.grounder_base_url),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        for label, value in (
            ("api_key_env", self.api_key_env),
            ("grounder_api_key_env", self.grounder_api_key_env),
        ):
            if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
                raise ValueError(f"{label} must name an environment variable")
        _validated_headers(self.default_headers, "default_headers")
        _validated_headers(self.grounder_default_headers, "grounder_default_headers")
        if self.platform not in {"darwin", "linux", "windows"}:
            raise ValueError("platform must be darwin, linux, or windows")
        if self.execution_backend not in {
            BACKGROUND_PIXEL_BACKEND,
            FOREGROUND_NATIVE_BACKEND,
        }:
            raise ValueError(
                "execution_backend must be background_pixel_v1 or foreground_native_v1"
            )
        if self.coordinate_space != FRAME_PIXEL_COORDINATE_SPACE:
            raise ValueError(
                f"coordinate_space must be {FRAME_PIXEL_COORDINATE_SPACE!r}"
            )
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
        ):
            raise ValueError("temperature must be a finite number or None")
        if not 1 <= int(self.max_trajectory_length) <= 64:
            raise ValueError("max_trajectory_length must be between 1 and 64")
        if float(self.timeout_s) <= 0:
            raise ValueError("timeout_s must be positive")
        if int(self.max_retries) < 0:
            raise ValueError("max_retries must be non-negative")
        for label, value in (
            ("native_screen_width", self.native_screen_width),
            ("native_screen_height", self.native_screen_height),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        return self

    def to_worker_payload(self, *, agent_s_root: Path) -> dict[str, Any]:
        self.validated()
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "grounder_model": self.grounder_model,
            "grounder_base_url": self.grounder_base_url,
            "grounder_api_key_env": self.grounder_api_key_env,
            "default_headers": dict(self.default_headers),
            "grounder_default_headers": dict(self.grounder_default_headers),
            "temperature": self.temperature,
            "platform": self.platform,
            "max_trajectory_length": int(self.max_trajectory_length),
            "timeout_s": float(self.timeout_s),
            "max_retries": int(self.max_retries),
            "agent_s_root": str(agent_s_root),
            "native_screen_width": int(self.native_screen_width),
            "native_screen_height": int(self.native_screen_height),
            "execution_backend": self.execution_backend,
            "coordinate_space": self.coordinate_space,
            # These are invariants, not caller-selectable switches.
            "enable_reflection": True,
            "enable_local_env": False,
            "worker_observation_mode": "screenshot",
        }


@dataclass(frozen=True)
class AgentSAction:
    """One bounded action returned by the worker for parent-side execution."""

    kind: str
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    key: str | None = None
    modifiers: tuple[str, ...] = ()
    text: str | None = None
    dx: int | None = None
    dy: int | None = None
    seconds: float | None = None
    duration: float | None = None
    button: str | None = None
    clicks: int | None = None
    overwrite: bool = False
    enter: bool = False
    reason: str = ""
    raw: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentSAction":
        kind = str(value.get("kind", "wait"))
        if kind not in {
            "click",
            "key",
            "type",
            "scroll",
            "drag",
            "wait",
            "done",
            "fail",
            "native_exec",
        }:
            raise AgentSProtocolError(f"worker returned unknown action kind {kind!r}")
        modifiers = value.get("modifiers") or []
        if not isinstance(modifiers, list) or not all(
            isinstance(item, str) for item in modifiers
        ):
            raise AgentSProtocolError("action modifiers must be a string list")
        return cls(
            kind=kind,
            x=_optional_int(value.get("x")),
            y=_optional_int(value.get("y")),
            x2=_optional_int(value.get("x2")),
            y2=_optional_int(value.get("y2")),
            key=_optional_str(value.get("key")),
            modifiers=tuple(modifiers),
            text=_optional_str(value.get("text")),
            dx=_optional_int(value.get("dx")),
            dy=_optional_int(value.get("dy")),
            seconds=_optional_float(value.get("seconds")),
            duration=_optional_float(value.get("duration")),
            button=_optional_str(value.get("button")),
            clicks=_optional_int(value.get("clicks")),
            overwrite=bool(value.get("overwrite", False)),
            enter=bool(value.get("enter", False)),
            reason=str(value.get("reason", ""))[:500],
            raw=str(value.get("raw", ""))[:500],
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["modifiers"] = list(self.modifiers)
        return payload


@dataclass(frozen=True)
class PolicyFrame:
    """Immutable policy observation and its input coordinate contract."""

    session_id: str
    frame_id: int
    path: str
    sha256: str
    width: int
    height: int
    coordinate_space: str = FRAME_PIXEL_COORDINATE_SPACE
    capture_backend: str = ""

    def validated(self) -> "PolicyFrame":
        if not self.session_id or not self.capture_backend:
            raise ValueError("policy frame requires session_id and capture_backend")
        if self.frame_id < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("policy frame id and dimensions are invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("policy frame sha256 must be lowercase hexadecimal")
        if self.coordinate_space != FRAME_PIXEL_COORDINATE_SPACE:
            raise ValueError("policy frame coordinate space is unsupported")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputBatch:
    """Exactly-once action batch bound to one fresh policy frame."""

    session_id: str
    frame_id: int
    batch_id: str
    actions: tuple[AgentSAction, ...]

    def validated(self, *, width: int, height: int) -> "InputBatch":
        if not self.session_id or not self.batch_id or self.frame_id < 0:
            raise ValueError("input batch identity is invalid")
        if not 1 <= len(self.actions) <= MAX_INPUT_BATCH_ACTIONS:
            raise ValueError("input batch action count is outside the allowed range")
        for action in self.actions:
            _validate_background_action(action, width=width, height=height)
        return self


@dataclass(frozen=True)
class ExecutionReceipt:
    """Runtime acknowledgement for one input batch."""

    batch_id: str
    accepted: bool
    event_count: int
    resulting_frame_id: int | None = None
    error: str = ""

    def validated(self) -> "ExecutionReceipt":
        if not self.batch_id or self.event_count < 0:
            raise ValueError("execution receipt is invalid")
        if self.accepted and self.error:
            raise ValueError("accepted execution receipt cannot carry an error")
        return self


@dataclass(frozen=True)
class AgentSPrediction:
    """One policy decision and its parent-executable structured action batch."""

    action: AgentSAction
    actions: tuple[AgentSAction, ...]
    usage_events: tuple[dict[str, Any], ...]
    info: dict[str, Any] = field(default_factory=dict)


class AgentSBridgeClient:
    """Persistent JSONL client used by the shared GUI phase runner.

    ``predict`` and ``next_action`` are aliases. The screenshot path is the
    real-desktop capture handed to upstream Agent-S; ``observation`` is only a
    non-empty placeholder kept for protocol compatibility (the child ignores it
    and reads the screenshot bytes).
    """

    def __init__(
        self,
        config: AgentSBridgeConfig,
        *,
        root: str | Path = Path.cwd(),
        python_executable: str | Path | None = None,
        request_timeout_s: float | None = None,
        process_factory: Any = subprocess.Popen,
    ) -> None:
        self.config = config.validated()
        self.root = Path(root).expanduser().resolve()
        self.agent_s_root = (self.root / "baselines" / "agent-s").resolve()
        self.python_executable = (
            _absolute_without_resolving_symlinks(python_executable)
            if python_executable is not None
            else _default_python(self.root)
        )
        # A production predict has no compound parent deadline.  Every child
        # provider request is bounded and the JSONL reader detects child exit;
        # adding a second whole-predict ceiling can only pre-empt the child's
        # auditable response and make paid usage unknowable.  Explicit injection
        # remains available solely for deterministic timeout tests.
        self.request_timeout_s = (
            None if request_timeout_s is None else float(request_timeout_s)
        )
        if self.request_timeout_s is not None and (
            not math.isfinite(self.request_timeout_s) or self.request_timeout_s <= 0
        ):
            raise ValueError("request_timeout_s must be a positive finite number")
        self.control_timeout_s = AGENT_S_CONTROL_REQUEST_TIMEOUT_S
        self.predict_timeout_mode = AGENT_S_PREDICT_TIMEOUT_MODE
        self.max_provider_attempts_per_predict = (
            AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
        )
        self._process_factory = process_factory
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._request_lock = threading.Lock()
        self._next_id = 0
        self._reader_threads: list[threading.Thread] = []
        self.metadata: dict[str, Any] = {}
        self.last_prediction: AgentSPrediction | None = None

    def __enter__(self) -> "AgentSBridgeClient":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> "AgentSBridgeClient":
        if self.running:
            return self
        # Give each child its own response queue. An old reader can therefore
        # never inject a late EOF/line into a restarted child's protocol stream.
        self._responses = queue.Queue()
        self._stderr.clear()
        if not self.agent_s_root.is_dir():
            raise AgentSBridgeError(
                f"Agent-S submodule is unavailable at {self.agent_s_root}; "
                "run `git submodule update --init baselines/agent-s`"
            )
        for env_name in {
            self.config.api_key_env,
            self.config.grounder_api_key_env,
        }:
            if not os.environ.get(env_name):
                raise AgentSBridgeError(
                    f"required API key environment variable {env_name} is unset"
                )
        _require_supported_python(self.python_executable)

        env = _worker_environment(self.config, self.python_executable)
        old_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(self.root)
            if not old_pythonpath
            else os.pathsep.join((str(self.root), old_pythonpath))
        )
        self._process = self._process_factory(
            [
                str(self.python_executable),
                "-u",
                "-m",
                "warptest.agent_s_worker",
            ],
            cwd=str(self.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise AgentSBridgeError("failed to create Agent-S worker pipes")
        self._reader_threads = [
            threading.Thread(
                target=self._read_stdout,
                args=(self._process.stdout, self._responses),
                name="agent-s-jsonl-reader",
                daemon=True,
            )
        ]
        if self._process.stderr is not None:
            self._reader_threads.append(
                threading.Thread(
                    target=self._read_stderr,
                    args=(self._process.stderr,),
                    name="agent-s-stderr-reader",
                    daemon=True,
                )
            )
        for thread in self._reader_threads:
            thread.start()

        try:
            self.metadata = self._request(
                "init",
                config=self.config.to_worker_payload(agent_s_root=self.agent_s_root),
            )
        except Exception:
            self._terminate()
            raise
        return self

    def reset(self, *, task: str | None = None) -> dict[str, Any]:
        self._ensure_started()
        self.last_prediction = None
        return self._request("reset", task=task)

    def configure_surface(self, *, width: int, height: int) -> dict[str, Any]:
        """Bind grounding and input coordinates to one task-stable pixel frame."""

        self._ensure_started()
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("surface width and height must be positive")
        result = self._request(
            "configure_surface", width=int(width), height=int(height)
        )
        self.metadata.update(result)
        return result

    def predict(
        self,
        *,
        task: str | None,
        instruction: str,
        observation: str,
        screenshot: str | Path,
        width: int,
        height: int,
        history: Sequence[str] | None = None,
        session_id: str | None = None,
        frame_id: int | None = None,
        capture_backend: str | None = None,
    ) -> AgentSPrediction:
        self._ensure_started()
        screenshot_path = Path(screenshot).expanduser().resolve()
        if not screenshot_path.is_file():
            raise FileNotFoundError(screenshot_path)
        if not isinstance(observation, str) or not observation.strip():
            raise ValueError("observation must be a non-empty placeholder string")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be non-empty")
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("width and height must be positive")
        if self.metadata.get("execution_backend") == BACKGROUND_PIXEL_BACKEND and (
            self.metadata.get("native_screen_width") != int(width)
            or self.metadata.get("native_screen_height") != int(height)
        ):
            self.configure_surface(width=int(width), height=int(height))
        policy_frame: PolicyFrame | None = None
        if self.metadata.get("execution_backend") == BACKGROUND_PIXEL_BACKEND:
            if (
                not isinstance(session_id, str)
                or not session_id
                or isinstance(frame_id, bool)
                or not isinstance(frame_id, int)
                or frame_id < 0
                or not isinstance(capture_backend, str)
                or not capture_backend
            ):
                raise ValueError(
                    "background prediction requires session_id, frame_id, and capture_backend"
                )
            policy_frame = PolicyFrame(
                session_id=session_id,
                frame_id=frame_id,
                path=str(screenshot_path),
                sha256=_sha256_file(screenshot_path),
                width=int(width),
                height=int(height),
                coordinate_space=self.config.coordinate_space,
                capture_backend=capture_backend,
            ).validated()
        result = self._request(
            "predict",
            task=task,
            instruction=instruction,
            observation=observation,
            screenshot=str(screenshot_path),
            width=int(width),
            height=int(height),
            history=list(history or []),
            policy_frame=policy_frame.to_dict() if policy_frame is not None else None,
        )
        action_value = result.get("action")
        if not isinstance(action_value, dict):
            raise AgentSProtocolError("predict result is missing an action object")
        usage_value = result.get("usage_events") or []
        if not isinstance(usage_value, list) or not all(
            isinstance(item, dict) for item in usage_value
        ):
            raise AgentSProtocolError("predict usage_events must be an object list")
        info = result.get("info") or {}
        if not isinstance(info, dict):
            raise AgentSProtocolError("predict info must be an object")
        action = AgentSAction.from_mapping(action_value)
        actions_value = result.get("actions")
        if actions_value is None:
            actions = (action,)
        elif not isinstance(actions_value, list) or not all(
            isinstance(item, dict) for item in actions_value
        ):
            raise AgentSProtocolError("predict actions must be an object list")
        else:
            actions = tuple(AgentSAction.from_mapping(item) for item in actions_value)
        if not actions:
            raise AgentSProtocolError("predict actions must not be empty")
        prediction = AgentSPrediction(
            action=action,
            actions=actions,
            usage_events=tuple(dict(item) for item in usage_value),
            info=dict(info),
        )
        # The child has its own contextvars context, so its model calls are not
        # visible to the parent task meter.  Import each event exactly once here;
        # ``next_action`` delegates to ``predict`` and does no additional recording.
        self._record_usage_events(prediction.usage_events)
        if prediction.info.get("usage_accounting_complete") is False:
            raise AgentSUsageAccountingError(
                "Agent-S returned an action but provider usage was incomplete; "
                "the action was not released to the GUI runtime"
            )
        # A formal task deadline must never kill an already-dispatched paid
        # prediction: doing so loses the child's response and makes its provider
        # usage permanently unknowable.  Wait for the child to quiesce, import
        # its complete usage above, then discard the stale action at this release
        # boundary so the GUI cannot change after the task wall clock expires.
        raise_if_task_timed_out()
        self.last_prediction = prediction
        return prediction

    def next_action(self, **kwargs: Any) -> AgentSPrediction:
        return self.predict(**kwargs)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("close", timeout_s=10.0)
            except AgentSBridgeError:
                pass
        self._terminate()

    def _ensure_started(self) -> None:
        if not self.running:
            self.start()

    def _request(
        self, op: str, *, timeout_s: float | None = None, **payload: Any
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AgentSBridgeError(self._dead_worker_message())
        # Captured once per request so every "unknown outcome" fallback ledger
        # entry below records when the (possibly lost) provider call was
        # dispatched, even though its prompt/usage never made it back intact.
        requested_at = datetime.now(timezone.utc).isoformat()
        with self._request_lock:
            effective_timeout_s = (
                float(timeout_s)
                if timeout_s is not None
                else self.request_timeout_s
                if op == "predict"
                else self.control_timeout_s
            )
            # Production predicts intentionally have no compound parent
            # deadline.  The child bounds every provider transport and returns
            # the resulting usage events; truncating this wait at the task
            # deadline would destroy that auditable response.  Explicit
            # diagnostic predict timeouts and all control requests remain
            # bounded exactly as before.
            if not (op == "predict" and effective_timeout_s is None):
                effective_timeout_s = bounded_task_timeout_s(effective_timeout_s)
            self._next_id += 1
            request_id = self._next_id
            request = {"id": request_id, "op": op, **payload}
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                raise AgentSBridgeError(self._dead_worker_message()) from error
            try:
                response = self._responses.get(timeout=effective_timeout_s)
            except queue.Empty as error:
                # A late response would corrupt the ordered JSONL stream for the
                # next request, so a timed-out child cannot be reused.
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                deadline = current_task_deadline()
                if deadline is not None and deadline.remaining_s() <= 0:
                    raise deadline.timeout_error() from error
                error_class = (
                    AgentSUsageAccountingError if op == "predict" else AgentSBridgeError
                )
                accounting_suffix = (
                    "; usage accounting is incomplete" if op == "predict" else ""
                )
                raise error_class(
                    f"Agent-S worker timed out during {op} after "
                    f"{float(effective_timeout_s):.1f}s"
                    f"{accounting_suffix}"
                ) from error
            if isinstance(response, BaseException):
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                error_class = (
                    AgentSUsageAccountingError
                    if op == "predict"
                    else AgentSProtocolError
                )
                raise error_class(str(response)) from response
            if response.get("id") != request_id:
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                error_class = (
                    AgentSUsageAccountingError
                    if op == "predict"
                    else AgentSProtocolError
                )
                raise error_class(
                    f"Agent-S response id {response.get('id')!r} does not match "
                    f"request id {request_id}"
                )
            if not response.get("ok"):
                error_value = response.get("error") or {}
                if not isinstance(error_value, dict):
                    error_value = {"message": str(error_value)}
                error_usage = error_value.get("usage_events") or []
                if isinstance(error_usage, list) and all(
                    isinstance(item, dict) for item in error_usage
                ):
                    # Failed policy/grounding calls are still paid calls. The
                    # worker clears this list at the start of every prediction.
                    self._record_usage_events(error_usage)
                error_type = str(error_value.get("type", "WorkerError"))
                message = str(error_value.get("message", "unknown worker failure"))
                worker_error = AgentSWorkerError(f"{error_type}: {message}")
                # The parent must be able to retain exact paid-call usage even
                # when the child rejects a no-thinking content contract. Keep
                # the raw events private to the exception; callers must reduce
                # them to a content-free ledger before persistence.
                worker_error.usage_events = tuple(dict(event) for event in error_usage)
                worker_error.request_contract_failed = any(
                    event.get("request_contract_passed") is False
                    for event in error_usage
                )
                status_code = error_value.get("status_code")
                if status_code is not None:
                    worker_error.status_code = status_code
                provider_capacity = error_value.get("provider_capacity")
                if isinstance(provider_capacity, dict):
                    worker_error.provider_capacity = dict(provider_capacity)
                    worker_error.infrastructure_error = False
                if (
                    op == "predict"
                    and not error_usage
                    and cost.error_may_have_generated(worker_error)
                ):
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                    raise AgentSUsageAccountingError(
                        "Agent-S provider request timed out after dispatch; usage is unknown"
                    ) from worker_error
                raise worker_error
            result = response.get("result") or {}
            if not isinstance(result, dict):
                raise AgentSProtocolError("Agent-S result must be an object")
            return result

    @staticmethod
    def _record_usage_events(events: Sequence[Mapping[str, Any]]) -> None:
        incomplete = False
        for event in events:
            prompt = _event_usage_int(
                event, "prompt_tokens", known_field="prompt_tokens_known"
            )
            completion = _event_usage_int(
                event,
                "completion_tokens",
                known_field="completion_tokens_known",
            )
            total = _event_usage_int(
                event, "total_tokens", known_field="total_tokens_known"
            )
            reasoning = _event_usage_int(
                event,
                "reasoning_tokens",
                known_field="reasoning_tokens_known",
            )
            missing = [
                field
                for field, value in (
                    ("prompt_tokens", prompt),
                    ("completion_tokens", completion),
                    ("total_tokens", total),
                )
                if value is None
            ]
            if (
                prompt is not None
                and completion is not None
                and total is not None
                and total != prompt + completion
            ):
                total = None
                missing.append("total_tokens")
            reasoning_expected = bool(event.get("reasoning_expected"))
            reasoning_detail_complete = bool(
                event.get("reasoning_detail_complete", reasoning is not None)
            )
            reasoning_detail_status = str(
                event.get("reasoning_detail_status")
                or ("exact" if reasoning is not None else "missing")
            )
            missing_reasoning: list[str] = []
            if (
                reasoning is not None
                and completion is not None
                and reasoning > completion
            ):
                reasoning = None
                reasoning_detail_complete = False
                reasoning_detail_status = "invalid"
            reasoning_expected = reasoning_expected or not reasoning_detail_complete
            if reasoning_expected and (
                reasoning is None or not reasoning_detail_complete
            ):
                missing_reasoning.append("reasoning_tokens")
            explicitly_incomplete = event.get("usage_complete") is False
            event_complete = not explicitly_incomplete and not missing
            response = event.get("response")
            meter = cost.get_meter()
            if meter is not None:
                meter.record(
                    cost.CallRecord(
                        model=str(event.get("model") or "unknown-agent-s-model"),
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        reasoning_tokens=reasoning,
                        cached_prompt_tokens=_usage_int(
                            event.get("cached_prompt_tokens")
                        ),
                        total_tokens=total,
                        latency_ms=_usage_int(event.get("latency_ms")),
                        kind=str(event.get("kind") or "agent_s_worker"),
                        usage_complete=event_complete,
                        missing_usage_fields=tuple(missing),
                        reasoning_expected=reasoning_expected,
                        reasoning_detail_complete=reasoning_detail_complete,
                        reasoning_detail_status=reasoning_detail_status,
                        reasoning_tokens_source=str(
                            event.get("reasoning_tokens_source") or "missing"
                        ),
                        missing_reasoning_detail_fields=tuple(missing_reasoning),
                        endpoint=(
                            None
                            if event.get("endpoint") is None
                            else str(event.get("endpoint"))
                        ),
                        finish_reason=(
                            None
                            if event.get("finish_reason") is None
                            else str(event.get("finish_reason"))
                        ),
                        provider_reported_model=(
                            None
                            if event.get("provider_reported_model") is None
                            else str(event.get("provider_reported_model"))
                        ),
                        provider_response_id_sha256=(
                            None
                            if event.get("provider_response_id_sha256") is None
                            else str(event.get("provider_response_id_sha256"))
                        ),
                        system_fingerprint=(
                            None
                            if event.get("system_fingerprint") is None
                            else str(event.get("system_fingerprint"))
                        ),
                        messages=cost.sanitize_messages_for_ledger(
                            event.get("messages")
                        ),
                        response=None if response is None else str(response),
                        requested_at=(
                            None
                            if event.get("requested_at") is None
                            else str(event.get("requested_at"))
                        ),
                    )
                )
            incomplete = incomplete or not event_complete
        if incomplete:
            raise AgentSUsageAccountingError(
                "Agent-S provider usage event was incomplete; paid-call tokens "
                "remain unknown and the attempt cannot be published"
            )

    def _record_unknown_predict_attempt(
        self, *, requested_at: str | None = None
    ) -> None:
        meter = cost.get_meter()
        if meter is None:
            return
        missing = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
        meter.record(
            cost.CallRecord(
                model=self.config.model,
                prompt_tokens=None,
                completion_tokens=None,
                reasoning_tokens=None,
                cached_prompt_tokens=None,
                total_tokens=None,
                kind="agent_s_worker",
                requested_at=requested_at,
                usage_complete=False,
                missing_usage_fields=missing,
            )
        )

    def _read_stdout(
        self,
        stream: IO[str],
        responses: queue.Queue[dict[str, Any] | BaseException],
    ) -> None:
        try:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    responses.put(
                        AgentSProtocolError(
                            f"non-JSON output from Agent-S worker: {stripped[:300]!r}"
                        )
                    )
                    continue
                if not isinstance(value, dict):
                    responses.put(
                        AgentSProtocolError("Agent-S response must be an object")
                    )
                    continue
                responses.put(value)
        finally:
            if self._process is not None and self._process.poll() is not None:
                responses.put(AgentSBridgeError(self._dead_worker_message()))

    def _read_stderr(self, stream: IO[str]) -> None:
        for line in stream:
            self._stderr.append(line.rstrip())

    def _dead_worker_message(self) -> str:
        code = self._process.poll() if self._process is not None else None
        detail = "\n".join(self._stderr)
        for env_name in {
            self.config.api_key_env,
            self.config.grounder_api_key_env,
        }:
            secret = os.environ.get(env_name)
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        suffix = f"; stderr tail:\n{detail}" if detail else ""
        return f"Agent-S worker is not running (exit={code}){suffix}"

    def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _validated_headers(value: Mapping[str, str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    for name, header_value in value.items():
        if not isinstance(name, str) or name.lower() not in _NON_SECRET_HEADER_NAMES:
            raise ValueError(
                f"{label} may contain only non-secret headers: "
                f"{', '.join(sorted(_NON_SECRET_HEADER_NAMES))}"
            )
        if (
            not isinstance(header_value, str)
            or "\n" in header_value
            or "\r" in header_value
        ):
            raise ValueError(f"{label}[{name!r}] must be a single-line string")


def _default_python(root: Path) -> Path:
    configured = os.environ.get("WARPTEST_AGENT_S_PYTHON")
    if configured:
        return _absolute_without_resolving_symlinks(configured)
    bundled = root / "baselines" / "agent-s" / ".venv" / "bin" / "python"
    if bundled.is_file():
        return _absolute_without_resolving_symlinks(bundled)
    return _absolute_without_resolving_symlinks(sys.executable)


def _absolute_without_resolving_symlinks(value: str | os.PathLike[str]) -> Path:
    """Keep a venv's ``bin/python`` path instead of following it to its base Python."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _worker_environment(
    config: AgentSBridgeConfig, python_executable: Path
) -> dict[str, str]:
    """Pass runtime essentials, selected API keys, and audited request profiles."""

    operational = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "__CF_USER_TEXT_ENCODING",
        "PYTHONPATH",
    }
    selected = operational | {config.api_key_env, config.grounder_api_key_env}
    env = {name: os.environ[name] for name in selected if name in os.environ}
    if MODEL_REQUEST_PROFILES_ENV in os.environ:
        # Parse and reserialize before crossing the process boundary so arbitrary
        # provider request kwargs can never be smuggled through this variable.
        env[MODEL_REQUEST_PROFILES_ENV] = canonical_profiles_json()
    from .image_history_backoff import AUDIT_ENV, POLICY_ENV, REVISION, enabled
    if enabled():
        audit_root = Path(os.environ.get(AUDIT_ENV, ""))
        if not audit_root.is_absolute() or not audit_root.is_dir():
            raise AgentSBridgeError("capacity history backoff audit directory is invalid")
        env[POLICY_ENV] = REVISION
        env[AUDIT_ENV] = str(audit_root)
    # Preserve the venv identity for child tools without inheriting an unrelated
    # parent VIRTUAL_ENV.
    if python_executable.parent.name in {"bin", "Scripts"}:
        env["VIRTUAL_ENV"] = str(python_executable.parent.parent)
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(
            part for part in (str(python_executable.parent), current_path) if part
        )
    return env


def _require_supported_python(executable: Path) -> None:
    if not executable.is_file():
        raise AgentSBridgeError(
            f"Agent-S Python executable does not exist: {executable}"
        )
    command = [
        str(executable),
        "-c",
        "import json,sys; print(json.dumps(list(sys.version_info[:3])))",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = json.loads(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise AgentSBridgeError(
            f"could not inspect Agent-S Python executable {executable}"
        ) from error
    if not isinstance(version, list) or len(version) < 2:
        raise AgentSBridgeError(f"invalid Python version response from {executable}")
    if tuple(version[:2]) < (3, 9) or tuple(version[:2]) > (3, 12):
        raise AgentSBridgeError(
            f"Agent-S 0.3.2 requires Python 3.9-3.12; got "
            f"{'.'.join(str(part) for part in version)} from {executable}"
        )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AgentSProtocolError("boolean is not a valid integer action field")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise AgentSProtocolError(f"invalid integer action field {value!r}") from error


def _validate_background_action(
    action: AgentSAction, *, width: int, height: int
) -> None:
    """Validate the bounded parent-side action vocabulary fail-closed."""

    if action.kind not in {
        "click",
        "key",
        "type",
        "scroll",
        "drag",
        "wait",
        "done",
        "fail",
    }:
        raise ValueError(f"background action kind {action.kind!r} is forbidden")
    if width <= 0 or height <= 0:
        raise ValueError("background action dimensions must be positive")
    for label, value, limit in (
        ("x", action.x, width),
        ("x2", action.x2, width),
        ("y", action.y, height),
        ("y2", action.y2, height),
    ):
        if value is not None and not 0 <= value < limit:
            raise ValueError(f"background action {label} is outside the policy frame")
    if action.text is not None and len(action.text) > MAX_INPUT_TEXT_CHARS:
        raise ValueError("background action text is too long")
    if action.clicks is not None and not 1 <= action.clicks <= 3:
        raise ValueError("background action clicks must be between 1 and 3")
    for label, value in (("seconds", action.seconds), ("duration", action.duration)):
        if value is not None and (
            not math.isfinite(value) or not 0 <= value <= MAX_INPUT_ACTION_SECONDS
        ):
            raise ValueError(f"background action {label} is outside 0..5 seconds")
    if action.kind == "click" and None in (action.x, action.y):
        raise ValueError("background click requires x and y")
    if action.kind == "key" and not action.key:
        raise ValueError("background key action requires key")
    if action.kind == "type" and action.text is None:
        raise ValueError("background type action requires text")
    if action.kind == "drag" and None in (action.x, action.y, action.x2, action.y2):
        raise ValueError("background drag requires both endpoints")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AgentSProtocolError("boolean is not a valid numeric action field")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise AgentSProtocolError(f"invalid numeric action field {value!r}") from error


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _event_usage_int(
    event: Mapping[str, Any],
    field: str,
    *,
    known_field: str,
) -> int | None:
    if event.get(known_field) is False or field not in event:
        return None
    value = event.get(field)
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "AGENT_S_CONTROL_REQUEST_TIMEOUT_S",
    "AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT",
    "AGENT_S_PREDICT_TIMEOUT_MODE",
    "AGENT_S_PROVIDER_TIMEOUT_S",
    "AGENT_S_REQUEST_MAX_TOKENS",
    "AGENT_S_PROTOCOL_VERSION",
    "BACKGROUND_PIXEL_BACKEND",
    "FOREGROUND_NATIVE_BACKEND",
    "FRAME_PIXEL_COORDINATE_SPACE",
    "PINNED_AGENT_S_COMMIT",
    "PINNED_AGENT_S_VERSION",
    "AgentSAction",
    "AgentSBridgeClient",
    "AgentSBridgeConfig",
    "AgentSBridgeError",
    "AgentSPrediction",
    "PolicyFrame",
    "InputBatch",
    "ExecutionReceipt",
    "AgentSProtocolError",
    "AgentSUsageAccountingError",
    "AgentSWorkerError",
]
