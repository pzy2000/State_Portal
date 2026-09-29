"""Persistent, parent-owned bridge for the pinned ScaleCUA pixel baseline.

ScaleCUA is a single vision-language model driven by its released native
prompt; this bridge runs that loop in a JSONL child process under the same
``background_pixel_v1`` contract as the Agent-S bridge: the child returns only
bounded structured mouse/keyboard actions plus fail-closed usage events, and
the parent-owned runtime is the sole action executor.  Unlike Agent-S there is
no separate grounder model -- one decision model plans and grounds.

No API key is accepted by this module's public configuration.  Key
*environment variable names* cross the JSONL boundary and the worker resolves
their values from its inherited environment.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Mapping, Sequence

from . import cost
from .agent_s_bridge import (
    BACKGROUND_PIXEL_BACKEND,
    FRAME_PIXEL_COORDINATE_SPACE,
    AgentSAction,
    AgentSBridgeClient,
    AgentSUsageAccountingError,
    PolicyFrame,
    _sha256_file,
    _validated_headers,
)
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S
from .c1_task_deadline import (
    bounded_task_timeout_s,
    current_task_deadline,
    raise_if_task_timed_out,
)
from .model_request import MODEL_REQUEST_PROFILES_ENV, canonical_profiles_json
from .scalecua_worker import (
    PINNED_SCALECUA_COMMIT,
    SCALECUA_INFERENCE_REVISION,
    SCALECUA_MAX_PROVIDER_ATTEMPTS_PER_PREDICT,
    SCALECUA_PROTOCOL_VERSION,
)


SCALECUA_PROVIDER_TIMEOUT_S = C1_PROVIDER_TIMEOUT_S
SCALECUA_CONTROL_REQUEST_TIMEOUT_S = 360.0
SCALECUA_PREDICT_TIMEOUT_MODE = "child_provider_bounded_no_parent_deadline"
# One native decision per predict: single call + temperature-rejection
# fallback.  There is no reflection/grounding fan-out to budget for.
SCALECUA_REQUEST_MAX_TOKENS = 8192

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ScaleCUABridgeError(RuntimeError):
    """Base error raised by the ScaleCUA bridge."""


class ScaleCUAProtocolError(ScaleCUABridgeError):
    """Raised when the JSONL child violates the bridge protocol."""


class ScaleCUAWorkerError(ScaleCUABridgeError):
    """Raised when the child reports an operation failure."""


class ScaleCUAUsageAccountingError(AgentSUsageAccountingError):
    """A paid prediction may have occurred but its final usage is unavailable."""


@dataclass(frozen=True)
class ScaleCUABridgeConfig:
    """Non-secret configuration for one persistent ScaleCUA worker process."""

    model: str
    base_url: str
    api_key_env: str
    default_headers: Mapping[str, str] = field(default_factory=dict)
    temperature: float | None = 0.2
    platform: str = "darwin"
    timeout_s: float = SCALECUA_PROVIDER_TIMEOUT_S
    # SDK-internal retries cannot be assigned usage records.  The parent/task
    # retry layer is persistent and auditable, so worker transports are single-shot.
    max_retries: int = 0
    # Logical click-space dimensions of the parent's screenshot surface.
    native_screen_width: int = 0
    native_screen_height: int = 0
    execution_backend: str = BACKGROUND_PIXEL_BACKEND
    coordinate_space: str = FRAME_PIXEL_COORDINATE_SPACE

    def validated(self) -> "ScaleCUABridgeConfig":
        for label, value in (
            ("model", self.model),
            ("base_url", self.base_url),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        if not isinstance(self.api_key_env, str) or not _ENV_NAME.fullmatch(
            self.api_key_env
        ):
            raise ValueError("api_key_env must name an environment variable")
        _validated_headers(self.default_headers, "default_headers")
        if self.platform not in {"darwin", "linux", "windows"}:
            raise ValueError("platform must be darwin, linux, or windows")
        if self.execution_backend != BACKGROUND_PIXEL_BACKEND:
            raise ValueError(
                "ScaleCUA execution_backend must be background_pixel_v1"
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

    def to_worker_payload(self, *, scalecua_root: Path) -> dict[str, Any]:
        self.validated()
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "default_headers": dict(self.default_headers),
            "temperature": self.temperature,
            "platform": self.platform,
            "timeout_s": float(self.timeout_s),
            "max_retries": int(self.max_retries),
            "scalecua_root": str(scalecua_root),
            "native_screen_width": int(self.native_screen_width),
            "native_screen_height": int(self.native_screen_height),
            "execution_backend": self.execution_backend,
            "coordinate_space": self.coordinate_space,
        }


@dataclass(frozen=True)
class ScaleCUAPrediction:
    """One policy decision and its parent-executable structured action batch."""

    action: AgentSAction
    actions: tuple[AgentSAction, ...]
    usage_events: tuple[dict[str, Any], ...]
    info: dict[str, Any] = field(default_factory=dict)


class ScaleCUABridgeClient:
    """Persistent JSONL client used by the shared GUI phase runner.

    ``predict`` and ``next_action`` are aliases.  The screenshot path is the
    real-desktop capture handed to the ScaleCUA model; ``observation`` is only
    a non-empty placeholder kept for protocol compatibility.
    """

    def __init__(
        self,
        config: ScaleCUABridgeConfig,
        *,
        root: str | Path = Path.cwd(),
        python_executable: str | Path | None = None,
        request_timeout_s: float | None = None,
        process_factory: Any = subprocess.Popen,
    ) -> None:
        self.config = config.validated()
        self.root = Path(root).expanduser().resolve()
        self.scalecua_root = (self.root / "baselines" / "ScaleCUA").resolve()
        self.python_executable = (
            _absolute_without_resolving_symlinks(python_executable)
            if python_executable is not None
            else _default_python()
        )
        # A production predict has no compound parent deadline: every child
        # provider request is bounded and the JSONL reader detects child exit.
        self.request_timeout_s = (
            None if request_timeout_s is None else float(request_timeout_s)
        )
        if self.request_timeout_s is not None and (
            not math.isfinite(self.request_timeout_s) or self.request_timeout_s <= 0
        ):
            raise ValueError("request_timeout_s must be a positive finite number")
        self.control_timeout_s = SCALECUA_CONTROL_REQUEST_TIMEOUT_S
        self.predict_timeout_mode = SCALECUA_PREDICT_TIMEOUT_MODE
        self.max_provider_attempts_per_predict = (
            SCALECUA_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
        )
        self._process_factory = process_factory
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._request_lock = threading.Lock()
        self._next_id = 0
        self._reader_threads: list[threading.Thread] = []
        self.metadata: dict[str, Any] = {}
        self.last_prediction: ScaleCUAPrediction | None = None

    def __enter__(self) -> "ScaleCUABridgeClient":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> "ScaleCUABridgeClient":
        if self.running:
            return self
        self._responses = queue.Queue()
        self._stderr.clear()
        if not self.scalecua_root.is_dir():
            raise ScaleCUABridgeError(
                f"ScaleCUA submodule is unavailable at {self.scalecua_root}; "
                "run `git submodule update --init baselines/ScaleCUA`"
            )
        if not os.environ.get(self.config.api_key_env):
            raise ScaleCUABridgeError(
                f"required API key environment variable "
                f"{self.config.api_key_env} is unset"
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
                "warptest.scalecua_worker",
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
            raise ScaleCUABridgeError("failed to create ScaleCUA worker pipes")
        self._reader_threads = [
            threading.Thread(
                target=self._read_stdout,
                args=(self._process.stdout, self._responses),
                name="scalecua-jsonl-reader",
                daemon=True,
            )
        ]
        if self._process.stderr is not None:
            self._reader_threads.append(
                threading.Thread(
                    target=self._read_stderr,
                    args=(self._process.stderr,),
                    name="scalecua-stderr-reader",
                    daemon=True,
                )
            )
        for thread in self._reader_threads:
            thread.start()

        try:
            self.metadata = self._request(
                "init",
                config=self.config.to_worker_payload(scalecua_root=self.scalecua_root),
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
        """Bind input coordinates to one task-stable pixel frame."""

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
    ) -> ScaleCUAPrediction:
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
        if self.metadata.get("native_screen_width") != int(width) or (
            self.metadata.get("native_screen_height") != int(height)
        ):
            self.configure_surface(width=int(width), height=int(height))
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
            policy_frame=policy_frame.to_dict(),
        )
        action_value = result.get("action")
        if not isinstance(action_value, dict):
            raise ScaleCUAProtocolError("predict result is missing an action object")
        usage_value = result.get("usage_events") or []
        if not isinstance(usage_value, list) or not all(
            isinstance(item, dict) for item in usage_value
        ):
            raise ScaleCUAProtocolError("predict usage_events must be an object list")
        info = result.get("info") or {}
        if not isinstance(info, dict):
            raise ScaleCUAProtocolError("predict info must be an object")
        action = AgentSAction.from_mapping(action_value)
        actions_value = result.get("actions")
        if actions_value is None:
            actions = (action,)
        elif not isinstance(actions_value, list) or not all(
            isinstance(item, dict) for item in actions_value
        ):
            raise ScaleCUAProtocolError("predict actions must be an object list")
        else:
            actions = tuple(AgentSAction.from_mapping(item) for item in actions_value)
        if not actions:
            raise ScaleCUAProtocolError("predict actions must not be empty")
        prediction = ScaleCUAPrediction(
            action=action,
            actions=actions,
            usage_events=tuple(dict(item) for item in usage_value),
            info=dict(info),
        )
        # The child has its own contextvars context, so its model calls are not
        # visible to the parent task meter.  Import each event exactly once here.
        self._record_usage_events(prediction.usage_events)
        if prediction.info.get("usage_accounting_complete") is False:
            raise ScaleCUAUsageAccountingError(
                "ScaleCUA returned an action but provider usage was incomplete; "
                "the action was not released to the GUI runtime"
            )
        # Discard stale actions only after the paid prediction's usage has been
        # imported (see the Agent-S bridge for the full rationale).
        raise_if_task_timed_out()
        self.last_prediction = prediction
        return prediction

    def next_action(self, **kwargs: Any) -> ScaleCUAPrediction:
        return self.predict(**kwargs)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("close", timeout_s=10.0)
            except ScaleCUABridgeError:
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
            raise ScaleCUABridgeError(self._dead_worker_message())
        requested_at = datetime.now(timezone.utc).isoformat()
        with self._request_lock:
            effective_timeout_s = (
                float(timeout_s)
                if timeout_s is not None
                else self.request_timeout_s
                if op == "predict"
                else self.control_timeout_s
            )
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
                raise ScaleCUABridgeError(self._dead_worker_message()) from error
            try:
                response = self._responses.get(timeout=effective_timeout_s)
            except queue.Empty as error:
                # A late response would corrupt the ordered JSONL stream, so a
                # timed-out child cannot be reused.
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                deadline = current_task_deadline()
                if deadline is not None and deadline.remaining_s() <= 0:
                    raise deadline.timeout_error() from error
                error_class = (
                    ScaleCUAUsageAccountingError
                    if op == "predict"
                    else ScaleCUABridgeError
                )
                accounting_suffix = (
                    "; usage accounting is incomplete" if op == "predict" else ""
                )
                raise error_class(
                    f"ScaleCUA worker timed out during {op} after "
                    f"{float(effective_timeout_s):.1f}s"
                    f"{accounting_suffix}"
                ) from error
            if isinstance(response, BaseException):
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                error_class = (
                    ScaleCUAUsageAccountingError
                    if op == "predict"
                    else ScaleCUAProtocolError
                )
                raise error_class(str(response)) from response
            if response.get("id") != request_id:
                if op == "predict":
                    self._record_unknown_predict_attempt(requested_at=requested_at)
                self._terminate()
                error_class = (
                    ScaleCUAUsageAccountingError
                    if op == "predict"
                    else ScaleCUAProtocolError
                )
                raise error_class(
                    f"ScaleCUA response id {response.get('id')!r} does not match "
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
                    # Failed policy calls are still paid calls.  The worker
                    # clears this list at the start of every prediction.
                    try:
                        self._record_usage_events(error_usage)
                    except AgentSUsageAccountingError as accounting_error:
                        # Preserve the provider cause alongside the strict
                        # unknown-usage stop; otherwise diagnostics lose the
                        # distinction between a timeout and a rejected call.
                        failure = ScaleCUAUsageAccountingError(
                            "ScaleCUA provider usage is incomplete; worker "
                            f"{error_value.get('type', 'WorkerError')}: "
                            f"{error_value.get('message', 'unknown worker failure')}"
                        )
                        failure.usage_events = tuple(dict(event) for event in error_usage)
                        if error_value.get("status_code") is not None:
                            failure.status_code = error_value["status_code"]
                        raise failure from accounting_error
                error_type = str(error_value.get("type", "WorkerError"))
                message = str(error_value.get("message", "unknown worker failure"))
                worker_error = ScaleCUAWorkerError(f"{error_type}: {message}")
                worker_error.usage_events = tuple(
                    dict(event) for event in error_usage
                )
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
                    raise ScaleCUAUsageAccountingError(
                        "ScaleCUA provider request timed out after dispatch; "
                        "usage is unknown"
                    ) from worker_error
                raise worker_error
            result = response.get("result") or {}
            if not isinstance(result, dict):
                raise ScaleCUAProtocolError("ScaleCUA result must be an object")
            return result

    # Usage-event import is the Agent-S bridge's audited implementation; the
    # wire schema is shared so the ledger rows stay directly comparable.
    _record_usage_events = staticmethod(
        AgentSBridgeClient._record_usage_events
    )

    def _record_unknown_predict_attempt(
        self, *, requested_at: str | None = None
    ) -> None:
        meter = cost.get_meter()
        if meter is None:
            return
        meter.record(
            cost.CallRecord(
                model=self.config.model,
                prompt_tokens=None,
                completion_tokens=None,
                reasoning_tokens=None,
                cached_prompt_tokens=None,
                total_tokens=None,
                kind="scalecua_worker",
                requested_at=requested_at,
                usage_complete=False,
                missing_usage_fields=(
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                ),
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
                        ScaleCUAProtocolError(
                            f"non-JSON output from ScaleCUA worker: {stripped[:300]!r}"
                        )
                    )
                    continue
                if not isinstance(value, dict):
                    responses.put(ScaleCUAProtocolError("ScaleCUA response must be an object"))
                    continue
                responses.put(value)
        finally:
            if self._process is not None and self._process.poll() is not None:
                responses.put(ScaleCUABridgeError(self._dead_worker_message()))

    def _read_stderr(self, stream: IO[str]) -> None:
        for line in stream:
            self._stderr.append(line.rstrip())

    def _dead_worker_message(self) -> str:
        code = self._process.poll() if self._process is not None else None
        detail = "\n".join(self._stderr)
        secret = os.environ.get(self.config.api_key_env)
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
        suffix = f"; stderr tail:\n{detail}" if detail else ""
        return f"ScaleCUA worker is not running (exit={code}){suffix}"

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


def _absolute_without_resolving_symlinks(value: str | os.PathLike[str]) -> Path:
    """Keep a venv's ``bin/python`` path instead of following it to its base Python."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _default_python() -> Path:
    configured = os.environ.get("WARPTEST_SCALECUA_PYTHON")
    if configured:
        return _absolute_without_resolving_symlinks(configured)
    # The ScaleCUA worker needs only the OpenAI SDK and Pillow, both already
    # required by the parent harness; there is no dedicated child venv.
    return _absolute_without_resolving_symlinks(sys.executable)


def _worker_environment(config: ScaleCUABridgeConfig, python_executable: Path) -> dict[str, str]:
    """Pass runtime essentials, the selected API key, and audited request profiles."""

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
    selected = operational | {config.api_key_env}
    env = {name: os.environ[name] for name in selected if name in os.environ}
    if MODEL_REQUEST_PROFILES_ENV in os.environ:
        # Parse and reserialize before crossing the process boundary so arbitrary
        # provider request kwargs can never be smuggled through this variable.
        env[MODEL_REQUEST_PROFILES_ENV] = canonical_profiles_json()
    from .image_history_backoff import AUDIT_ENV, POLICY_ENV, REVISION, enabled

    if enabled():
        audit_root = Path(os.environ.get(AUDIT_ENV, ""))
        if not audit_root.is_absolute() or not audit_root.is_dir():
            raise ScaleCUABridgeError("capacity history backoff audit directory is invalid")
        env[POLICY_ENV] = REVISION
        env[AUDIT_ENV] = str(audit_root)
    if python_executable.parent.name in {"bin", "Scripts"}:
        env["VIRTUAL_ENV"] = str(python_executable.parent.parent)
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(
            part for part in (str(python_executable.parent), current_path) if part
        )
    return env


def _require_supported_python(executable: Path) -> None:
    if not executable.is_file():
        raise ScaleCUABridgeError(
            f"ScaleCUA Python executable does not exist: {executable}"
        )
    command = [
        str(executable),
        "-c",
        "import json,sys; print(json.dumps(list(sys.version_info[:3])))",
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=10
        )
        version = json.loads(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ScaleCUABridgeError(
            f"could not inspect ScaleCUA Python executable {executable}"
        ) from error
    if not isinstance(version, list) or len(version) < 2:
        raise ScaleCUABridgeError(f"invalid Python version response from {executable}")
    if tuple(version[:2]) < (3, 9) or tuple(version[:2]) > (3, 13):
        raise ScaleCUABridgeError(
            f"ScaleCUA worker requires Python 3.9-3.13; got "
            f"{'.'.join(str(part) for part in version)} from {executable}"
        )


__all__ = [
    "PINNED_SCALECUA_COMMIT",
    "SCALECUA_CONTROL_REQUEST_TIMEOUT_S",
    "SCALECUA_INFERENCE_REVISION",
    "SCALECUA_MAX_PROVIDER_ATTEMPTS_PER_PREDICT",
    "SCALECUA_PREDICT_TIMEOUT_MODE",
    "SCALECUA_PROTOCOL_VERSION",
    "SCALECUA_PROVIDER_TIMEOUT_S",
    "ScaleCUABridgeClient",
    "ScaleCUABridgeConfig",
    "ScaleCUABridgeError",
    "ScaleCUAPrediction",
    "ScaleCUAProtocolError",
    "ScaleCUAUsageAccountingError",
    "ScaleCUAWorkerError",
]
