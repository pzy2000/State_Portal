"""JSONL worker for WarpTest's Agent-S S3 bridge.

This module intentionally imports Agent-S lazily: the parent WarpTest process may
run on Python 3.13+, while Agent-S 0.3.2 is pinned to Python <=3.12.  The child
uses the upstream ``AgentS3.predict`` orchestration method with real screenshots
and upstream grounding. Formal ``background_pixel_v1`` execution replaces the
upstream eval/exec edge with a literal-call parser and structured action batch.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.metadata
import io
import json
import hashlib
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, IO, Mapping

from PIL import Image

from . import cost, run_context
from .agent_s_bridge import (
    AGENT_S_PROTOCOL_VERSION,
    AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT,
    AGENT_S_PREDICT_TIMEOUT_MODE,
    AGENT_S_PROVIDER_TIMEOUT_S,
    AGENT_S_REQUEST_MAX_TOKENS,
    BACKGROUND_PIXEL_BACKEND,
    FRAME_PIXEL_COORDINATE_SPACE,
    MAX_INPUT_ACTION_SECONDS,
    MAX_INPUT_BATCH_ACTIONS,
    MAX_INPUT_TEXT_CHARS,
    PINNED_AGENT_S_COMMIT,
    PINNED_AGENT_S_VERSION,
    AgentSBridgeConfig,
)
from .c1_provider_runtime import is_temperature_rejection
from .model_request import THINKING_DISABLED, resolve_model_request_profile


_MAX_ACTION_SOURCE_CHARS = 20_000


class WorkerConfigurationError(ValueError):
    """The non-secret worker configuration is invalid."""


class GroundingError(ValueError):
    """The visual grounder did not return an in-viewport point."""


class AgentSReasoningContentContractError(RuntimeError):
    """A disabled-thinking Agent-S route returned actual reasoning content."""


def _utc_now_iso() -> str:
    """UTC ISO8601 timestamp, captured immediately before a provider request."""

    return datetime.now(timezone.utc).isoformat()


def _typed_int_or_none(value: Any) -> int | None:
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    )


def _config_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkerConfigurationError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    model: str
    base_url: str
    api_key_env: str
    grounder_model: str
    grounder_base_url: str
    grounder_api_key_env: str
    default_headers: dict[str, str]
    grounder_default_headers: dict[str, str]
    temperature: float | None
    platform: str
    max_trajectory_length: int
    timeout_s: float
    max_retries: int
    agent_s_root: Path
    native_screen_width: int
    native_screen_height: int
    execution_backend: str
    coordinate_space: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerConfig":
        if not isinstance(value, Mapping):
            raise WorkerConfigurationError("config must be an object")
        forbidden = {
            key
            for key in value
            if "api_key" in str(key).lower()
            and str(key) not in {"api_key_env", "grounder_api_key_env"}
        }
        if forbidden:
            raise WorkerConfigurationError(
                "raw API keys are forbidden; pass environment variable names only"
            )
        if value.get("enable_reflection") is not True:
            raise WorkerConfigurationError("Agent-S reflection must remain enabled")
        if value.get("enable_local_env") is not False:
            raise WorkerConfigurationError(
                "Agent-S local-code environment is forbidden"
            )
        if value.get("worker_observation_mode") != "screenshot":
            raise WorkerConfigurationError(
                "worker_observation_mode must be 'screenshot'"
            )
        native_screen_width = _config_positive_int(
            value.get("native_screen_width"), "native_screen_width"
        )
        native_screen_height = _config_positive_int(
            value.get("native_screen_height"), "native_screen_height"
        )
        try:
            public = AgentSBridgeConfig(
                model=str(value["model"]),
                base_url=str(value["base_url"]),
                api_key_env=str(value["api_key_env"]),
                grounder_model=str(value["grounder_model"]),
                grounder_base_url=str(value["grounder_base_url"]),
                grounder_api_key_env=str(value["grounder_api_key_env"]),
                default_headers=dict(value.get("default_headers") or {}),
                grounder_default_headers=dict(
                    value.get("grounder_default_headers") or {}
                ),
                temperature=(
                    None
                    if value.get("temperature") is None
                    else float(value["temperature"])
                ),
                platform=str(value.get("platform", "darwin")),
                max_trajectory_length=int(value.get("max_trajectory_length", 8)),
                timeout_s=float(value.get("timeout_s", AGENT_S_PROVIDER_TIMEOUT_S)),
                max_retries=int(value.get("max_retries", 0)),
                native_screen_width=native_screen_width,
                native_screen_height=native_screen_height,
                execution_backend=str(
                    value.get("execution_backend", BACKGROUND_PIXEL_BACKEND)
                ),
                coordinate_space=str(
                    value.get("coordinate_space", FRAME_PIXEL_COORDINATE_SPACE)
                ),
            ).validated()
            agent_s_root = Path(str(value["agent_s_root"])).expanduser().resolve()
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerConfigurationError(str(error)) from error
        if not agent_s_root.is_dir():
            raise WorkerConfigurationError(
                f"Agent-S submodule is unavailable at {agent_s_root}"
            )
        for env_name in {public.api_key_env, public.grounder_api_key_env}:
            if not os.environ.get(env_name):
                raise WorkerConfigurationError(
                    f"required API key environment variable {env_name} is unset"
                )
        return cls(
            model=public.model,
            base_url=public.base_url,
            api_key_env=public.api_key_env,
            grounder_model=public.grounder_model,
            grounder_base_url=public.grounder_base_url,
            grounder_api_key_env=public.grounder_api_key_env,
            default_headers=dict(public.default_headers),
            grounder_default_headers=dict(public.grounder_default_headers),
            temperature=public.temperature,
            platform=public.platform,
            max_trajectory_length=public.max_trajectory_length,
            timeout_s=public.timeout_s,
            max_retries=public.max_retries,
            agent_s_root=agent_s_root,
            native_screen_width=int(public.native_screen_width),
            native_screen_height=int(public.native_screen_height),
            execution_backend=public.execution_backend,
            coordinate_space=public.coordinate_space,
        )


@dataclass(frozen=True)
class AgentSBindings:
    agent_s3_class: type
    reflection_prompt: str
    version: str
    commit: str
    worker_class: type | None = None
    worker_module: Any | None = None
    common_utils_module: Any | None = None


def load_agent_s_bindings(root: Path) -> AgentSBindings:
    """Load and verify the exact upstream checkout used by the native adapter."""

    root = root.resolve()
    # Verify bytes-on-disk before importing any upstream Python. This is part of
    # the safety boundary: a dirty checkout must not get a chance to execute.
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerConfigurationError(
            f"could not verify Agent-S gitlink at {root}"
        ) from error
    if commit != PINNED_AGENT_S_COMMIT:
        raise WorkerConfigurationError(
            f"expected Agent-S commit {PINNED_AGENT_S_COMMIT}, found {commit}"
        )
    try:
        dirty = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerConfigurationError(
            f"could not inspect Agent-S worktree at {root}"
        ) from error
    if dirty:
        raise WorkerConfigurationError(
            "the pinned Agent-S checkout has tracked modifications; refusing import"
        )
    parent_root = root.parent.parent
    try:
        gitlink = subprocess.run(
            [
                "git",
                "-C",
                str(parent_root),
                "ls-files",
                "-s",
                "--",
                str(root.relative_to(parent_root)),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise WorkerConfigurationError(
            f"could not verify the parent gitlink for {root}"
        ) from error
    fields = gitlink.split()
    if len(fields) < 4 or fields[0] != "160000" or fields[1] != PINNED_AGENT_S_COMMIT:
        raise WorkerConfigurationError(
            "parent repository does not pin baselines/agent-s to the expected commit"
        )
    version = importlib.metadata.version("gui-agents")
    if version != PINNED_AGENT_S_VERSION:
        raise WorkerConfigurationError(
            f"expected Agent-S {PINNED_AGENT_S_VERSION}, found {version}"
        )
    sys.path[:] = [entry for entry in sys.path if entry != str(root)]
    sys.path.insert(0, str(root))
    agent_module = importlib.import_module("gui_agents.s3.agents.agent_s")
    worker_module = importlib.import_module("gui_agents.s3.agents.worker")
    common_utils_module = importlib.import_module("gui_agents.s3.utils.common_utils")
    memory_module = importlib.import_module("gui_agents.s3.memory.procedural_memory")
    package_module = importlib.import_module("gui_agents")
    package_path = Path(package_module.__file__ or "").resolve()
    try:
        package_path.relative_to(root)
    except ValueError as error:
        raise WorkerConfigurationError(
            f"gui_agents resolved outside the pinned submodule: {package_path}"
        ) from error
    return AgentSBindings(
        agent_s3_class=agent_module.AgentS3,
        reflection_prompt=memory_module.PROCEDURAL_MEMORY.REFLECTION_ON_TRAJECTORY,
        version=version,
        commit=commit,
        worker_class=worker_module.Worker,
        worker_module=worker_module,
        common_utils_module=common_utils_module,
    )


class ChatCompletionsBackend:
    """OpenAI-compatible client holder used to pre-install native usage recording."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key_env: str,
        default_headers: Mapping[str, str],
        temperature: float | None,
        timeout_s: float,
        max_retries: int,
        usage_sink: list[dict[str, Any]],
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.default_headers = dict(default_headers)
        self.temperature = None if temperature is None else float(temperature)
        # Shared by every recording proxy installed from this backend (decision,
        # reflection, and text-span).  A provider rejection therefore changes the
        # remainder of the persistent worker, not just one LMMAgent instance.
        self.omit_temperature_after_rejection = False
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.usage_sink = usage_sink
        self._client = client
        self._client_factory = client_factory

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise WorkerConfigurationError(
                f"required API key environment variable {self.api_key_env} is unset"
            )
        factory = self._client_factory
        if factory is None:
            from openai import OpenAI

            factory = OpenAI
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": self.base_url,
            "timeout": self.timeout_s,
            "max_retries": self.max_retries,
        }
        if self.default_headers:
            kwargs["default_headers"] = self.default_headers
        self._client = factory(**kwargs)
        return self._client


def _quiet_call_llm_safe(
    agent: Any,
    temperature: float | None = None,
    use_thinking: bool = False,
    **kwargs: Any,
) -> str:
    """Upstream ``call_llm_safe`` without stdout prints.

    The JSONL worker owns stdout; upstream's ``print("Response success!")`` /
    ``Attempt N failed`` would otherwise corrupt the parent protocol. It keeps
    the pinned three attempts, but re-raises the final provider/transport error
    instead of turning an outage into an empty policy response.
    """

    del temperature, use_thinking
    response = ""
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            response = agent.get_response(**kwargs)
            if response is None:
                raise RuntimeError("response from Agent-S backend was None")
            return response
        except (cost.UsageAccountingError, AgentSReasoningContentContractError):
            raise
        except Exception as error:
            if run_context.provider_capacity_failure(error) is not None:
                raise
            last_error = error
    # Upstream returns an empty string after transport/provider failures.  In a
    # formal run that turns a 429/timeout into an apparent policy-format failure
    # and can incorrectly count an infrastructure outage as a model result.
    # Preserve the three bounded attempts, but fail closed with the original
    # provider exception so the parent can classify its status code and retain
    # the worker's auditable error ledger.
    if last_error is not None:
        raise last_error
    return response


class _NativeUsageRecordingCompletions:
    """Applies the registered request profile and records a real OpenAI call.

    Native mode reuses upstream ``LMMEngineOpenAI.generate`` verbatim, which
    discards the API response's ``usage`` once it extracts the message content.
    This proxy sits at the SDK boundary purely so provider usage stays
    auditable; every request/response/exception is forwarded unchanged.
    """

    def __init__(
        self,
        real_completions: Any,
        *,
        backend: ChatCompletionsBackend,
        kind: str,
    ) -> None:
        self._real = real_completions
        self._backend = backend
        self._kind = kind
        self._sink = backend.usage_sink

    def _record_error(
        self,
        error: Exception,
        *,
        model: str,
        messages: Any,
        requested_at: str,
        start: float,
        requested_temperature: Any,
        fallback_triggered: bool,
    ) -> None:
        error_usage = _extract_error_usage(error)
        if error_usage is None and not cost.error_may_have_generated(error):
            return
        usage_audit, _ = _usage_audit(
            error_usage,
            content="",
            has_reasoning_content=False,
            finish_reason=None,
            reasoning_expected=(
                resolve_model_request_profile(model).reasoning_expected
            ),
            reasoning_disabled=(
                resolve_model_request_profile(model).profile == "thinking_disabled"
            ),
        )
        self._sink.append(
            {
                "model": model,
                "kind": self._kind,
                **usage_audit,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "status": "error",
                "error_type": type(error).__name__,
                "endpoint": "chat.completions",
                "messages": messages,
                "requested_at": requested_at,
                "temperature_configured": self._backend.temperature,
                "temperature_requested": requested_temperature,
                "temperature_fallback_triggered": fallback_triggered,
                "temperature_omitted_after_rejection": (
                    self._backend.omit_temperature_after_rejection
                ),
            }
        )

    def create(self, **kwargs: Any) -> Any:
        from .image_history_backoff import create_with_backoff

        history_audit: list[dict] = []

        def send() -> Any:
            return create_with_backoff(
                self._real.create, kwargs, extract_error_usage=_extract_error_usage,
                kind=self._kind, audit=history_audit,
            )

        model = str(kwargs.get("model") or "")
        request_profile = resolve_model_request_profile(model)
        # Upstream Agent-S does not know WarpTest's strict request-profile
        # contract. Apply the same allowlisted kwargs used by synthesis,
        # GuiAgent, and preflight at the final SDK boundary.
        kwargs.update(request_profile.request_kwargs())
        # Pinned Agent-S's OpenAI engine accepts ``max_new_tokens`` but drops it
        # before the SDK call, leaving the gateway's smaller default in force.
        # Use the same 8192-token initial budget registered by C1 preflight so
        # reasoning-capable routes have room to emit the required ACI action.
        kwargs.setdefault("max_tokens", AGENT_S_REQUEST_MAX_TOKENS)
        if self._backend.temperature is not None:
            if self._backend.omit_temperature_after_rejection:
                kwargs.pop("temperature", None)
            else:
                kwargs["temperature"] = self._backend.temperature
        messages = cost.sanitize_messages_for_ledger(kwargs.get("messages"))
        requested_at = _utc_now_iso()
        start = time.perf_counter()
        fallback_triggered = False
        requested_temperature = kwargs.get("temperature")
        try:
            response = send()
        except Exception as error:
            messages = cost.sanitize_messages_for_ledger(kwargs.get("messages"))
            if "temperature" in kwargs and is_temperature_rejection(error):
                self._backend.omit_temperature_after_rejection = True
                fallback_triggered = True
                self._record_error(
                    error,
                    model=model,
                    messages=messages,
                    requested_at=requested_at,
                    start=start,
                    requested_temperature=requested_temperature,
                    fallback_triggered=True,
                )
                kwargs.pop("temperature", None)
                requested_at = _utc_now_iso()
                start = time.perf_counter()
                try:
                    response = send()
                except Exception as retry_error:
                    self._record_error(
                        retry_error,
                        model=model,
                        messages=cost.sanitize_messages_for_ledger(kwargs.get("messages")),
                        requested_at=requested_at,
                        start=start,
                        requested_temperature=None,
                        fallback_triggered=True,
                    )
                    raise
            else:
                self._record_error(
                    error,
                    model=model,
                    messages=messages,
                    requested_at=requested_at,
                    start=start,
                    requested_temperature=requested_temperature,
                    fallback_triggered=False,
                )
                raise
        messages = cost.sanitize_messages_for_ledger(kwargs.get("messages"))
        latency_ms = int((time.perf_counter() - start) * 1000)
        choices = _field(response, "choices", []) or []
        choice = choices[0] if choices else None
        message = _field(choice, "message", None)
        content = _message_content(_field(message, "content", ""))
        reasoning_value = _field(message, "reasoning_content", None)
        has_reasoning_content = (
            bool(reasoning_value.strip())
            if isinstance(reasoning_value, str)
            else bool(reasoning_value)
        )
        finish_reason = _optional_text(_field(choice, "finish_reason", None))
        usage = _field(response, "usage", None)
        usage_audit, _retry_reasoning_tokens = _usage_audit(
            usage,
            content=content,
            has_reasoning_content=has_reasoning_content,
            finish_reason=finish_reason,
            reasoning_expected=request_profile.reasoning_expected,
            reasoning_disabled=request_profile.profile == "thinking_disabled",
        )
        response_id = _optional_text(_field(response, "id", None))
        reasoning_disabled = request_profile.profile == THINKING_DISABLED
        reasoning_token_anomaly = bool(
            reasoning_disabled and int(usage_audit.get("reasoning_tokens") or 0) > 0
        )
        request_contract_passed = not (reasoning_disabled and has_reasoning_content)
        self._sink.append(
            {
                "model": model,
                "kind": self._kind,
                **usage_audit,
                "latency_ms": latency_ms,
                "finish_reason": finish_reason,
                "endpoint": "chat.completions",
                "provider_reported_model": _optional_text(
                    _field(response, "model", None)
                ),
                "provider_response_id_sha256": (
                    hashlib.sha256(response_id.encode("utf-8")).hexdigest()
                    if response_id
                    else None
                ),
                "system_fingerprint": _optional_text(
                    _field(response, "system_fingerprint", None)
                ),
                "status": "ok",
                "messages": messages,
                # A content-contract failure must retain token evidence but never
                # copy either public content or private reasoning into an artifact.
                "response": content if request_contract_passed else None,
                "reasoning_content_observed": has_reasoning_content,
                "reasoning_token_anomaly": reasoning_token_anomaly,
                "request_contract_passed": request_contract_passed,
                "requested_at": requested_at,
                "temperature_configured": self._backend.temperature,
                "temperature_requested": (
                    None if fallback_triggered else requested_temperature
                ),
                "temperature_fallback_triggered": fallback_triggered,
                "temperature_omitted_after_rejection": (
                    self._backend.omit_temperature_after_rejection
                ),
                "max_tokens_requested": kwargs.get("max_tokens"),
                "image_history_backoff": history_audit,
            }
        )
        if not request_contract_passed:
            raise AgentSReasoningContentContractError(
                "thinking_disabled Agent-S response contained reasoning content"
            )
        return response


class _NativeUsageRecordingClient:
    """Wraps a real ``openai.OpenAI`` client with the completions proxy above."""

    def __init__(
        self,
        real_client: Any,
        *,
        backend: ChatCompletionsBackend,
        kind: str,
    ) -> None:
        completions = _NativeUsageRecordingCompletions(
            real_client.chat.completions,
            backend=backend,
            kind=kind,
        )
        self.chat = type(
            "_NativeUsageRecordingChat", (), {"completions": completions}
        )()


def _install_native_usage_recording(
    agent: Any, *, backend: ChatCompletionsBackend, kind: str
) -> None:
    """Pre-set an upstream ``LMMAgent``'s lazily-created OpenAI client.

    ``LMMEngineOpenAI.generate`` only builds ``self.llm_client`` when it is
    falsy, so installing ours here means the pinned upstream code path never
    constructs (or needs to know about) the recording proxy.
    """

    engine = getattr(agent, "engine", None)
    if engine is None or not hasattr(engine, "llm_client"):
        raise WorkerConfigurationError(
            "native usage recording requires an upstream LMMEngineOpenAI-backed agent"
        )
    engine.llm_client = _NativeUsageRecordingClient(
        backend._get_client(), backend=backend, kind=kind
    )


def _native_pyautogui_size() -> tuple[int, int]:
    """Real logical-point screen size, measured by the library that will click.

    Imported lazily: this module is imported by the parent's top-level Python
    (which does not have ``pyautogui`` installed) as well as run as the Agent-S
    venv worker child; only the child ever calls this.
    """

    import pyautogui

    width, height = pyautogui.size()
    return int(width), int(height)


_BACKGROUND_AGENT_VERBS = frozenset(
    {
        "click",
        "type",
        "hotkey",
        "hold_and_press",
        "scroll",
        "drag_and_drop",
        "highlight_text_span",
        "wait",
        "done",
        "fail",
        "save_to_knowledge",
    }
)
_BACKGROUND_SKIPPED_VERBS = frozenset(
    {"open", "switch_applications", "set_cell_values", "call_code_agent"}
)


def _bounded_literal(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ValueError("Agent-S action literal nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_INPUT_TEXT_CHARS:
            raise ValueError("Agent-S action string is too long")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 1_000_000:
            raise ValueError("Agent-S action integer is out of bounds")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 1_000_000:
            raise ValueError("Agent-S action number is out of bounds")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_INPUT_BATCH_ACTIONS:
            raise ValueError("Agent-S action list is too long")
        return type(value)(_bounded_literal(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        if len(value) > MAX_INPUT_BATCH_ACTIONS:
            raise ValueError("Agent-S action object is too large")
        return {
            _bounded_literal(key, depth=depth + 1): _bounded_literal(
                item, depth=depth + 1
            )
            for key, item in value.items()
        }
    raise ValueError(f"Agent-S action literal type {type(value).__name__} is forbidden")


def parse_background_agent_call(
    code: str,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Parse exactly one allowlisted ``agent.<verb>(literal...)`` call."""

    if not isinstance(code, str) or not code.strip():
        raise ValueError("Agent-S action code is empty")
    if len(code) > _MAX_ACTION_SOURCE_CHARS:
        raise ValueError("Agent-S action code is too large")
    tree = ast.parse(code.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 256:
        raise ValueError("Agent-S action syntax tree is too large")
    call = tree.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        raise ValueError("Agent-S action must be one method call")
    if not isinstance(call.func.value, ast.Name) or call.func.value.id != "agent":
        raise ValueError("Agent-S action receiver must be agent")
    verb = call.func.attr
    if verb not in _BACKGROUND_AGENT_VERBS:
        raise ValueError(f"Agent-S action verb {verb!r} is forbidden")
    if len(call.args) + len(call.keywords) > 16:
        raise ValueError("Agent-S action has too many arguments")
    if any(keyword.arg is None for keyword in call.keywords):
        raise ValueError("Agent-S action **kwargs expansion is forbidden")
    args = [_bounded_literal(ast.literal_eval(arg)) for arg in call.args]
    kwargs = {
        str(keyword.arg): _bounded_literal(ast.literal_eval(keyword.value))
        for keyword in call.keywords
    }
    return verb, args, kwargs


def _safe_create_grounded_action(agent: Any, code: str, obs: Mapping[str, Any]) -> str:
    """Ground an allowlisted action without Python eval."""

    verb, args, kwargs = parse_background_agent_call(code)
    agent.assign_screenshot(obs)
    method = getattr(agent, verb, None)
    if method is None or not callable(method):
        raise ValueError(f"Agent-S action verb {verb!r} is unavailable")
    grounded = method(*args, **kwargs)
    if not isinstance(grounded, str) or len(grounded) > _MAX_ACTION_SOURCE_CHARS:
        raise ValueError("Agent-S grounded action is not a bounded string")
    return grounded


def _install_background_action_boundary(
    bindings: AgentSBindings, grounding_module: Any
) -> None:
    """Replace upstream eval and hide non-pixel actions inside the child only."""

    formatters_module = importlib.import_module("gui_agents.s3.utils.formatters")
    memory_module = importlib.import_module("gui_agents.s3.memory.procedural_memory")
    bindings.worker_module.create_pyautogui_code = _safe_create_grounded_action
    bindings.common_utils_module.create_pyautogui_code = _safe_create_grounded_action
    formatters_module.create_pyautogui_code = _safe_create_grounded_action

    procedural = memory_module.PROCEDURAL_MEMORY
    original = getattr(
        procedural,
        "_warptest_original_construct_simple_worker_procedural_memory",
        procedural.construct_simple_worker_procedural_memory,
    )
    procedural._warptest_original_construct_simple_worker_procedural_memory = original
    original_reflection = getattr(
        procedural,
        "_warptest_original_reflection_on_trajectory",
        procedural.REFLECTION_ON_TRAJECTORY,
    )
    procedural._warptest_original_reflection_on_trajectory = original_reflection

    def construct(agent_class: type, skipped_actions: list[str]) -> str:
        skipped = sorted(set(skipped_actions) | set(_BACKGROUND_SKIPPED_VERBS))
        return _background_procedural_prompt(original(agent_class, skipped))

    procedural.construct_simple_worker_procedural_memory = staticmethod(construct)
    procedural.REFLECTION_ON_TRAJECTORY = _background_reflection_prompt(
        original_reflection
    )
    bindings.worker_module.PROCEDURAL_MEMORY = procedural
    grounding_module.call_llm_safe = _quiet_call_llm_safe


def _background_procedural_prompt(upstream_prompt: str) -> str:
    """Keep upstream ACI docs/formatting while removing forbidden capabilities."""

    marker = "You are provided with:"
    marker_index = upstream_prompt.find(marker)
    if marker_index < 0:
        raise WorkerConfigurationError(
            "pinned Agent-S procedural prompt lacks the expected ACI marker"
        )
    aci_tail = upstream_prompt[marker_index:]
    # The pinned prompt embeds a public benchmark password/sudo hint even when
    # the corresponding actions are skipped. Formal background execution has
    # no shell or privilege boundary, so remove the text as well as the verbs.
    aci_tail = re.sub(
        r"(?m)^\s*\d+\. My computer's password is .*?sudo rights\.\s*$\n?",
        "",
        aci_tail,
    )
    boundary = (
        "BACKGROUND PIXEL EXECUTION CONTRACT:\n"
        "You are an expert graphical user-interface agent responsible for "
        "executing `TASK_DESCRIPTION` on CURRENT_OS. Use only the listed GUI "
        "actions. Never open or switch applications, invoke external coding "
        "tools, use semantic document APIs, shell commands, system clipboard "
        "shortcuts, "
        "or privilege escalation. The target application is already open. "
        "Complete the task only through visible pixels and the listed "
        "mouse/keyboard methods.\n\n"
    )
    prompt = boundary + aci_tail
    lowered = prompt.lower()
    forbidden = ("code agent", "call_code_agent", "password", "sudo")
    leaked = [token for token in forbidden if token in lowered]
    if leaked:
        raise WorkerConfigurationError(
            "background Agent-S prompt retained forbidden capability text: "
            + ", ".join(leaked)
        )
    return prompt


def _background_reflection_prompt(upstream_prompt: str) -> str:
    """Replace upstream reflection guidance with a pixel-only contract."""

    if "Your task is to generate a reflection" not in upstream_prompt:
        raise WorkerConfigurationError(
            "pinned Agent-S reflection prompt lacks the expected task marker"
        )
    prompt = """\
BACKGROUND PIXEL REFLECTION CONTRACT:
You are an expert graphical user-interface agent reflecting on a task trajectory.
The trajectory contains only real UI frames, the policy's prior reasoning, and
recorded low-level mouse, keyboard, text, scroll, drag, wait, done, or fail
actions. Only visible frame changes and those recorded actions are valid
evidence. Do not infer invisible file, application, document, or system changes.

Classify the trajectory as exactly one case:
1. It is not progressing, such as a repeated cycle with no visible progress.
   Explain the problem without prescribing a specific next action.
2. It is progressing. Briefly say to continue.
3. The visible target state has been reached. Say the task is complete.

Base the reflection only on the supplied task and trajectory. Do not propose or
invoke capabilities outside the recorded low-level UI action set.
"""
    lowered = prompt.lower()
    forbidden = ("code agent", "call_code_agent", "password", "sudo")
    leaked = [token for token in forbidden if token in lowered]
    if leaked:
        raise WorkerConfigurationError(
            "background Agent-S reflection retained forbidden capability text: "
            + ", ".join(leaked)
        )
    return prompt.strip()


def build_native_agent_s3(
    config: WorkerConfig,
    bindings: AgentSBindings,
    *,
    client_factory: Callable[..., Any] | None = None,
    pyautogui_size: tuple[int, int] | None = None,
) -> Any:
    """Build pinned S3 with either structured background or legacy execution.

    Formal background mode keeps upstream screenshot reasoning and grounding but
    replaces its model-code ``eval`` edge with ``parse_background_agent_call``.
    The returned grounded string is compiled to structured actions later and is
    never executed in the worker.

    The only WarpTest addition is a thin usage-recording proxy (see
    ``_NativeUsageRecordingClient``) installed on each upstream OpenAI client so
    every provider call remains auditable; it observes responses without altering
    requests, retries, or execution.

    ``pyautogui_size`` overrides the measured logical-point screen size (see
    ``_native_pyautogui_size``); tests inject a fixed value so they never touch
    the real display. See the ``width``/``height`` vs ``grounding_width``/
    ``grounding_height`` split below for why this must be measured separately
    from ``config.native_screen_width/height``.
    """

    if bindings.agent_s3_class is None or bindings.worker_class is None:
        raise WorkerConfigurationError(
            "Agent-S bindings are missing the S3 agent/worker classes"
        )
    if config.native_screen_width <= 0 or config.native_screen_height <= 0:
        raise WorkerConfigurationError(
            "native_screen_width/native_screen_height must be positive"
        )
    if config.execution_backend == BACKGROUND_PIXEL_BACKEND:
        click_width, click_height = (
            int(config.native_screen_width),
            int(config.native_screen_height),
        )
    else:
        click_width, click_height = (
            pyautogui_size if pyautogui_size is not None else _native_pyautogui_size()
        )
    if click_width <= 0 or click_height <= 0:
        raise WorkerConfigurationError(
            "pyautogui.size() returned a non-positive logical screen size"
        )

    grounding_module = importlib.import_module("gui_agents.s3.agents.grounding")

    # Upstream ``call_llm_safe`` prints to stdout; the JSONL protocol owns stdout,
    # so replace every module-local binding the S3 path actually calls through.
    bindings.worker_module.call_llm_safe = _quiet_call_llm_safe
    bindings.common_utils_module.call_llm_safe = _quiet_call_llm_safe
    grounding_module.call_llm_safe = _quiet_call_llm_safe
    if config.execution_backend == BACKGROUND_PIXEL_BACKEND:
        _install_background_action_boundary(bindings, grounding_module)

    usage_events: list[dict[str, Any]] = []
    worker_backend = ChatCompletionsBackend(
        model=config.model,
        base_url=config.base_url,
        api_key_env=config.api_key_env,
        default_headers=config.default_headers,
        temperature=config.temperature,
        timeout_s=config.timeout_s,
        max_retries=config.max_retries,
        usage_sink=usage_events,
        client_factory=client_factory,
    )
    grounder_backend = ChatCompletionsBackend(
        model=config.grounder_model,
        base_url=config.grounder_base_url,
        api_key_env=config.grounder_api_key_env,
        default_headers=config.grounder_default_headers,
        temperature=None,
        timeout_s=config.timeout_s,
        max_retries=config.max_retries,
        usage_sink=usage_events,
        client_factory=client_factory,
    )

    # Upstream ``LMMEngineOpenAI.generate`` unconditionally requires a truthy
    # ``self.api_key`` (or ``OPENAI_API_KEY``) even when ``self.llm_client`` is
    # already installed and that value is therefore never read or transmitted.
    # WarpTest never puts a real key in an engine_params dict (see
    # ``build_native_agent_s3``'s docstring and ``_install_native_usage_recording``
    # below), so a placeholder satisfies that upstream precondition only.
    _UNUSED_NATIVE_API_KEY = "unused-warptest-native-preinstalled-client"
    worker_engine_params = {
        "engine_type": "openai",
        "model": config.model,
        # The registered experiment temperature must reach the final SDK call.
        # The recording proxy retries once without it only after an explicit
        # provider-side temperature rejection, then keeps that omission sticky.
        "temperature": config.temperature,
        "api_key": _UNUSED_NATIVE_API_KEY,
    }
    # ``grounding_width/height`` must match the actual screenshot pixels handed
    # to the grounding model (the real, unscaled desktop capture from
    # ``native_window.capture_full_screen``, i.e. physical/backing-store pixels
    # on a Retina display). ``width``/``height`` below is deliberately a
    # *different* number: it is the coordinate space upstream
    # ``OSWorldACI.resize_coordinates`` rescales into before handing coordinates
    # to ``pyautogui.click``/``moveTo``/etc, and on macOS pyautogui's Quartz
    # backend clicks in *logical points*, not physical pixels. On a Retina
    # display those differ by the HiDPI scale factor (commonly 2x): a screenshot
    # measured at 3024x1964 physical pixels can have a 1512x982-point logical
    # desktop. Passing the same value to both here would make
    # ``resize_coordinates`` an identity mapping and every click would land at
    # roughly double (or clipped past the edge of) its intended position.
    grounder_engine_params = {
        "engine_type": "openai",
        "model": config.grounder_model,
        "temperature": None,
        "grounding_width": int(config.native_screen_width),
        "grounding_height": int(config.native_screen_height),
        "api_key": _UNUSED_NATIVE_API_KEY,
    }

    grounding_agent = grounding_module.OSWorldACI(
        env=None,
        platform=config.platform,
        engine_params_for_generation=worker_engine_params,
        engine_params_for_grounding=grounder_engine_params,
        width=int(click_width),
        height=int(click_height),
    )
    _install_native_usage_recording(
        grounding_agent.grounding_model,
        backend=grounder_backend,
        kind="agent_s_grounding",
    )
    _install_native_usage_recording(
        grounding_agent.text_span_agent,
        backend=worker_backend,
        kind="agent_s_text_span",
    )

    upstream_agent_s3_class = bindings.agent_s3_class

    class WarpTestNativeAgentS3(upstream_agent_s3_class):
        def reset(self) -> None:
            super().reset()
            _install_native_usage_recording(
                self.executor.generator_agent,
                backend=worker_backend,
                kind="agent_s_worker",
            )
            _install_native_usage_recording(
                self.executor.reflection_agent,
                backend=worker_backend,
                kind="agent_s_reflection",
            )

    agent = WarpTestNativeAgentS3(
        worker_engine_params=worker_engine_params,
        grounding_agent=grounding_agent,
        platform=config.platform,
        max_trajectory_length=config.max_trajectory_length,
        enable_reflection=True,
    )
    agent.warptest_usage_events = usage_events
    agent.warptest_agent_s_version = bindings.version
    agent.warptest_agent_s_commit = bindings.commit
    agent.warptest_native_click_width = int(click_width)
    agent.warptest_native_click_height = int(click_height)
    agent.warptest_execution_backend = config.execution_backend
    agent.warptest_coordinate_space = config.coordinate_space
    agent.warptest_temperature_contract = {
        "configured": config.temperature,
        "fallback": "omit_only_after_explicit_http_400_temperature_rejection",
        "scope": "persistent_worker_backend",
    }
    return agent


class AgentSJsonlServer:
    """Stateful init/reset/predict/close JSONL protocol server."""

    def __init__(
        self,
        in_stream: IO[str],
        out_stream: IO[str],
        *,
        agent_factory: Callable[
            [WorkerConfig, AgentSBindings], Any
        ] = build_native_agent_s3,
        bindings_loader: Callable[[Path], AgentSBindings] = load_agent_s_bindings,
    ) -> None:
        self.in_stream = in_stream
        self.out_stream = out_stream
        self.agent_factory = agent_factory
        self.bindings_loader = bindings_loader
        self.config: WorkerConfig | None = None
        self.bindings: AgentSBindings | None = None
        self.agent: Any | None = None
        self.current_task: str | None = None
        self.last_policy_frame_by_session: dict[str, int] = {}
        self.closed = False

    def serve(self) -> None:
        for line in self.in_stream:
            if self.closed:
                break
            stripped = line.strip()
            if not stripped:
                continue
            request_id: Any = None
            request: Any = None
            try:
                request = json.loads(stripped)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                request_id = request.get("id")
                result = self.handle(request)
                response = {"id": request_id, "ok": True, "result": result}
            except Exception as error:
                message = _redact(str(error), self.config)
                print(
                    f"[agent-s-worker] {type(error).__name__}: {message}",
                    file=sys.stderr,
                    flush=True,
                )
                error_payload: dict[str, Any] = {
                    "type": type(error).__name__,
                    "message": message,
                }
                status_code = getattr(error, "status_code", None)
                if status_code is not None:
                    error_payload["status_code"] = status_code
                provider_capacity = run_context.provider_capacity_failure(error)
                if provider_capacity is not None:
                    error_payload["provider_capacity"] = provider_capacity
                if (
                    isinstance(request, dict)
                    and request.get("op") == "predict"
                    and self.agent is not None
                ):
                    usage = getattr(self.agent, "warptest_usage_events", [])
                    if isinstance(usage, list):
                        error_payload["usage_events"] = [
                            dict(event) for event in usage if isinstance(event, dict)
                        ]
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": error_payload,
                }
            self.out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            self.out_stream.flush()
            if self.closed:
                break

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "init":
            if self.agent is not None:
                raise RuntimeError("worker is already initialized")
            config = WorkerConfig.from_mapping(request.get("config") or {})
            bindings = self.bindings_loader(config.agent_s_root)
            agent = self.agent_factory(config, bindings)
            if not isinstance(agent, bindings.agent_s3_class):
                raise TypeError("adapter must inherit pinned AgentS3")
            self.config = config
            self.bindings = bindings
            self.agent = agent
            background = config.execution_backend == BACKGROUND_PIXEL_BACKEND
            return {
                "protocol_version": AGENT_S_PROTOCOL_VERSION,
                "agent_s_version": bindings.version,
                "agent_s_commit": bindings.commit,
                "runtime_mode": "background_pixel" if background else "native",
                "execution_backend": config.execution_backend,
                "coordinate_space": config.coordinate_space,
                "raw_screenshot_observation": True,
                "capture_backend": "runtime_policy_frame",
                "action_execution_owner": "parent_runtime" if background else "worker",
                "foreground_required": not background,
                "upstream_worker": bool(
                    bindings.worker_class is not None
                    and isinstance(
                        getattr(agent, "executor", None), bindings.worker_class
                    )
                ),
                "reflection_enabled": True,
                "local_code_enabled": False,
                "worker_observation_mode": "screenshot",
                "native_exec_enabled": not background,
                "native_screen_width": int(config.native_screen_width),
                "native_screen_height": int(config.native_screen_height),
                "native_click_width": _typed_int_or_none(
                    getattr(agent, "warptest_native_click_width", None)
                ),
                "native_click_height": _typed_int_or_none(
                    getattr(agent, "warptest_native_click_height", None)
                ),
                "provider_timeout_s": float(config.timeout_s),
                "predict_timeout_mode": AGENT_S_PREDICT_TIMEOUT_MODE,
                "max_provider_attempts_per_predict": (
                    AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
                ),
                "model_request_profiles": {
                    "worker": resolve_model_request_profile(config.model).provenance(),
                    "reflection": resolve_model_request_profile(
                        config.model
                    ).provenance(),
                    "grounding": resolve_model_request_profile(
                        config.grounder_model
                    ).provenance(),
                },
                "temperature_contract": dict(
                    getattr(agent, "warptest_temperature_contract", {})
                ),
            }
        if op == "close":
            self.closed = True
            return {"closed": True}
        agent = self._require_agent()
        if op == "configure_surface":
            if self.config is None or self.bindings is None:
                raise RuntimeError("worker configuration is unavailable")
            if self.config.execution_backend != BACKGROUND_PIXEL_BACKEND:
                raise ValueError(
                    "configure_surface is available only in background mode"
                )
            width = _positive_int(request.get("width"), "width")
            height = _positive_int(request.get("height"), "height")
            if (
                width != self.config.native_screen_width
                or height != self.config.native_screen_height
            ):
                config = replace(
                    self.config,
                    native_screen_width=width,
                    native_screen_height=height,
                )
                agent = self.agent_factory(config, self.bindings)
                if not isinstance(agent, self.bindings.agent_s3_class):
                    raise TypeError("adapter must inherit pinned AgentS3")
                self.config = config
                self.agent = agent
                self.current_task = None
            return {
                "native_screen_width": width,
                "native_screen_height": height,
                "native_click_width": _typed_int_or_none(
                    getattr(agent, "warptest_native_click_width", None)
                ),
                "native_click_height": _typed_int_or_none(
                    getattr(agent, "warptest_native_click_height", None)
                ),
                "surface_generation": int(request.get("id") or 0),
            }
        if op == "reset":
            agent.reset()
            self.current_task = _optional_text(request.get("task"))
            return {"reset": True, "task": self.current_task}
        if op == "predict":
            return self._handle_predict_native(request, agent)
        raise ValueError(f"unknown operation {op!r}")

    def _handle_predict_native(
        self, request: Mapping[str, Any], agent: Any
    ) -> dict[str, Any]:
        """Run upstream reasoning/grounding and dispatch under the configured edge."""

        agent.warptest_usage_events.clear()
        task = _optional_text(request.get("task"))
        instruction = _nonempty_text(request.get("instruction"), "instruction")
        screenshot = Path(
            _nonempty_text(request.get("screenshot"), "screenshot")
        ).resolve()
        width = _positive_int(request.get("width"), "width")
        height = _positive_int(request.get("height"), "height")
        history = request.get("history") or []
        if not isinstance(history, list) or not all(
            isinstance(item, str) for item in history
        ):
            raise ValueError("history must be a string list")
        effective_task = task or instruction
        if self.current_task != effective_task:
            agent.reset()
            self.current_task = effective_task
        screenshot_dimensions_match(screenshot, width, height)
        if (
            self.config is not None
            and self.config.execution_backend == BACKGROUND_PIXEL_BACKEND
        ):
            frame = request.get("policy_frame")
            if not isinstance(frame, Mapping):
                raise ValueError("background prediction is missing PolicyFrame")
            session_id = _nonempty_text(
                frame.get("session_id"), "PolicyFrame.session_id"
            )
            frame_id_value = frame.get("frame_id")
            if isinstance(frame_id_value, bool) or not isinstance(frame_id_value, int):
                raise ValueError("PolicyFrame.frame_id must be a non-negative integer")
            frame_id = frame_id_value
            if frame_id <= self.last_policy_frame_by_session.get(session_id, -1):
                raise ValueError("PolicyFrame is stale or duplicated")
            if (
                Path(_nonempty_text(frame.get("path"), "PolicyFrame.path")).resolve()
                != screenshot
            ):
                raise ValueError(
                    "PolicyFrame path does not match prediction screenshot"
                )
            if (
                frame.get("width") != width
                or frame.get("height") != height
                or frame.get("coordinate_space") != FRAME_PIXEL_COORDINATE_SPACE
                or not isinstance(frame.get("capture_backend"), str)
                or not frame.get("capture_backend")
            ):
                raise ValueError("PolicyFrame dimensions or backend contract drifted")
            digest = hashlib.sha256(screenshot.read_bytes()).hexdigest()
            if frame.get("sha256") != digest:
                raise ValueError("PolicyFrame sha256 does not match screenshot bytes")
            self.last_policy_frame_by_session[session_id] = frame_id
        observation = {"screenshot": screenshot.read_bytes()}
        # Upstream Agent-S (grounding, call_llm_safe, etc.) prints diagnostics to
        # stdout. This worker's stdout is the JSONL wire; redirect during predict
        # so those prints cannot corrupt the parent protocol. Spill to stderr so
        # the same text remains visible in the worker log.
        leaked = io.StringIO()
        with contextlib.redirect_stdout(leaked):
            info, actions = agent.predict(instruction, observation)
        spilled = leaked.getvalue()
        if spilled:
            print(spilled, end="", file=sys.stderr, flush=True)
        if not isinstance(actions, list) or len(actions) != 1:
            raise RuntimeError("native Agent-S must return exactly one action")
        code = str(actions[0] or "")
        if len(code) > _MAX_ACTION_SOURCE_CHARS:
            raise RuntimeError("native Agent-S action source is too large")
        info_map = info if isinstance(info, Mapping) else {}
        plan_code = info_map.get("plan_code")
        background = bool(
            self.config is not None
            and self.config.execution_backend == BACKGROUND_PIXEL_BACKEND
        )
        if background:
            action, action_batch = _structured_dispatch(
                plan_code,
                code,
                width=width,
                height=height,
            )
        else:
            action = _native_dispatch(code, plan_code=plan_code)
            action_batch = [dict(action)]
        usage_events = [dict(event) for event in agent.warptest_usage_events]
        usage_complete = bool(usage_events) and all(
            event.get("usage_complete", True) for event in usage_events
        )
        return {
            "action": action,
            "actions": action_batch,
            "usage_events": usage_events,
            "info": {
                "turn": int(getattr(agent.executor, "turn_count", 0) or 0),
                "reflection_enabled": True,
                "local_code_enabled": False,
                "worker_observation_mode": "screenshot",
                "upstream_worker": True,
                "native_exec_enabled": not background,
                "execution_backend": (
                    self.config.execution_backend if self.config is not None else None
                ),
                "coordinate_space": (
                    self.config.coordinate_space if self.config is not None else None
                ),
                "action_execution_owner": "parent_runtime" if background else "worker",
                "foreground_required": not background,
                "usage_accounting_complete": usage_complete,
                "agent_s_version": agent.warptest_agent_s_version,
                "agent_s_commit": agent.warptest_agent_s_commit,
                # The model's full reasoning+decision text, its raw (ungrounded)
                # ``agent.<verb>(...)`` call, the reflection folded into this
                # step's prompt, the friendly verb name parsed from plan_code
                # (mirrors ``action["kind"]`` for the fallback "native_exec"
                # case where kind alone is not descriptive), and the exact
                # text-only prompt this step's decision model was shown.
                "plan": _optional_text(info_map.get("plan")),
                "plan_code": _optional_text(plan_code),
                "reflection": _optional_text(info_map.get("reflection")),
                "verb": _plan_code_verb(plan_code),
                "prompt_text": _extract_prompt_text(agent),
            },
        }

    def _require_agent(self) -> Any:
        if self.agent is None:
            raise RuntimeError("worker must be initialized before this operation")
        return self.agent


def screenshot_dimensions(path: Path) -> tuple[int, int]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def screenshot_dimensions_match(path: Path, width: int, height: int) -> None:
    actual = screenshot_dimensions(path)
    if actual != (width, height):
        raise GroundingError(
            f"declared viewport {width}x{height} does not match screenshot pixels "
            f"{actual[0]}x{actual[1]}"
        )


def _action(kind: str, **values: Any) -> dict[str, Any]:
    return {"kind": kind, **values}


def _extract_prompt_text(agent: Any) -> str | None:
    """Best-effort: the text-only parts of this step's final user turn.

    Reads back ``agent.executor.generator_agent.messages[-2]`` (the user turn
    ``generate_next_action`` just appended, right before its own assistant
    ``plan`` reply) so the report can show exactly what the decision model was
    shown, minus the embedded screenshot. Never raises; any unexpected message
    shape (e.g. a non-OpenAI-style engine) just yields ``None``.
    """

    try:
        messages = agent.executor.generator_agent.messages
        user_message = messages[-2]
        if user_message.get("role") != "user":
            return None
        parts = user_message.get("content")
        if not isinstance(parts, list):
            return None
        texts = [
            str(part.get("text"))
            for part in parts
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and part.get("text")
        ]
        return "\n".join(texts) if texts else None
    except Exception:
        return None


_CLICK_COORD_RE = re.compile(r"pyautogui\.click\(\s*(-?\d+)\s*,\s*(-?\d+)")
_DRAG_MOVE_RE = re.compile(r"pyautogui\.moveTo\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")
_DRAG_DRAGTO_RE = re.compile(r"pyautogui\.dragTo\(\s*(-?\d+)\s*,\s*(-?\d+)")


def _extract_pyautogui_click_point(exec_code: str) -> tuple[int, int] | None:
    match = _CLICK_COORD_RE.search(exec_code)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _extract_pyautogui_drag_points(
    exec_code: str,
) -> tuple[int, int, int, int] | None:
    move = _DRAG_MOVE_RE.search(exec_code)
    drag = _DRAG_DRAGTO_RE.search(exec_code)
    if move is None or drag is None:
        return None
    return (
        int(move.group(1)),
        int(move.group(2)),
        int(drag.group(1)),
        int(drag.group(2)),
    )


def _parse_plan_code_verb(
    plan_code: Any,
) -> tuple[str, list[Any], dict[str, Any]] | None:
    """Best-effort parse of upstream's single ``agent.<verb>(...)`` call.

    ``plan_code`` is upstream's ``parse_code_from_string(plan)`` output: exactly
    one call written by the model, enforced by ``SINGLE_ACTION_FORMATTER``. This
    is purely for human-readable reporting (friendlier ``kind``/params in the
    report and log viewer); it never raises and is never used for execution or
    safety gating -- native mode's real side effect is always ``_native_exec``
    on the *already-grounded* ``exec_code``, untouched by this parser.
    """

    if not isinstance(plan_code, str) or not plan_code.strip():
        return None
    try:
        tree = ast.parse(plan_code.strip(), mode="eval")
        call = tree.body
        if not isinstance(call, ast.Call):
            return None
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "agent"
        ):
            return None
        args = [ast.literal_eval(arg) for arg in call.args]
        kwargs = {
            kw.arg: ast.literal_eval(kw.value)
            for kw in call.keywords
            if kw.arg is not None
        }
        return func.attr, args, kwargs
    except Exception:
        return None


def _bind_verb_args(
    param_names: list[str],
    args: list[Any],
    kwargs: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    bound = dict(defaults)
    for name, value in zip(param_names, args):
        bound[name] = value
    bound.update({key: value for key, value in kwargs.items() if key in param_names})
    return bound


def _plan_code_verb(plan_code: Any) -> str | None:
    """The ``agent.<verb>`` name the model chose, or ``None`` if unparseable."""

    parsed = _parse_plan_code_verb(plan_code)
    return parsed[0] if parsed is not None else None


def _native_action_fields(plan_code: Any, exec_code: str) -> tuple[str, dict[str, Any]]:
    """Friendlier ``(kind, fields)`` for display, derived from the model's own
    ``agent.<verb>(...)`` call rather than the grounded pyautogui source.

    ``fields`` only ever contains keys ``AgentSAction`` already understands
    (x/y/x2/y2/key/modifiers/text/dx/dy/button/clicks/overwrite/enter); the verb
    name itself travels separately via ``info["verb"]`` (see
    ``_handle_predict_native``), since unknown action-dict keys are silently
    dropped crossing the wire (``AgentSAction.from_mapping`` only reads its
    known fields).

    Falls back to ``("native_exec", {})`` for upstream's fuller action surface
    (``open``, ``switch_applications``, ``highlight_text_span``,
    ``hold_and_press``, ``save_to_knowledge``, ...) that has no equivalent in
    the small kind enum below, and whenever ``plan_code`` is missing or does
    not parse. The returned ``kind`` is always one already accepted by
    ``AgentSAction.from_mapping`` (click/key/type/scroll/drag/native_exec), so
    no protocol change is required.
    """

    parsed = _parse_plan_code_verb(plan_code)
    if parsed is None:
        return "native_exec", {}
    verb, args, kwargs = parsed
    if verb == "click":
        bound = _bind_verb_args(
            ["element_description", "num_clicks", "button_type", "hold_keys"],
            args,
            kwargs,
            {"num_clicks": 1, "button_type": "left"},
        )
        fields: dict[str, Any] = {}
        button = bound.get("button_type")
        if isinstance(button, str):
            fields["button"] = button
        clicks = bound.get("num_clicks")
        if isinstance(clicks, int) and not isinstance(clicks, bool):
            fields["clicks"] = clicks
        point = _extract_pyautogui_click_point(exec_code)
        if point is not None:
            fields["x"], fields["y"] = point
        return "click", fields
    if verb == "type":
        bound = _bind_verb_args(
            ["element_description", "text", "overwrite", "enter"],
            args,
            kwargs,
            {"text": "", "overwrite": False, "enter": False},
        )
        text = bound.get("text")
        return "type", {
            "text": text if isinstance(text, str) else None,
            "overwrite": bool(bound.get("overwrite", False)),
            "enter": bool(bound.get("enter", False)),
        }
    if verb == "hotkey":
        bound = _bind_verb_args(["keys"], args, kwargs, {"keys": []})
        keys = bound.get("keys")
        keys_list = (
            [str(item) for item in keys] if isinstance(keys, list) and keys else []
        )
        if not keys_list:
            return "native_exec", {}
        return "key", {"key": keys_list[-1], "modifiers": keys_list[:-1]}
    if verb == "scroll":
        bound = _bind_verb_args(
            ["element_description", "clicks", "shift"],
            args,
            kwargs,
            {"clicks": 0, "shift": False},
        )
        clicks = bound.get("clicks")
        amount = (
            clicks if isinstance(clicks, int) and not isinstance(clicks, bool) else 0
        )
        if bound.get("shift"):
            return "scroll", {"dx": amount, "dy": 0}
        return "scroll", {"dx": 0, "dy": amount}
    if verb == "drag_and_drop":
        fields = {}
        points = _extract_pyautogui_drag_points(exec_code)
        if points is not None:
            fields["x"], fields["y"], fields["x2"], fields["y2"] = points
        return "drag", fields
    return "native_exec", {}


def _structured_dispatch(
    plan_code: Any,
    exec_code: str,
    *,
    width: int,
    height: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compile one validated grounded Agent-S call to parent-side actions."""

    if not isinstance(plan_code, str):
        raise ValueError("background Agent-S prediction is missing plan_code")
    verb, args, kwargs = parse_background_agent_call(plan_code)

    def point(required: bool = True) -> tuple[int | None, int | None]:
        value = _extract_pyautogui_click_point(exec_code)
        if value is None:
            move = _DRAG_MOVE_RE.search(exec_code)
            value = (int(move.group(1)), int(move.group(2))) if move else None
        if value is None:
            if required:
                raise GroundingError(f"grounded {verb} action has no pixel point")
            return None, None
        x, y = value
        if not 0 <= x < width or not 0 <= y < height:
            raise GroundingError(
                f"grounded {verb} point ({x}, {y}) is outside {width}x{height}"
            )
        return x, y

    raw = plan_code[:500]
    actions: list[dict[str, Any]]
    if verb == "click":
        bound = _bind_verb_args(
            ["element_description", "num_clicks", "button_type", "hold_keys"],
            args,
            kwargs,
            {"num_clicks": 1, "button_type": "left", "hold_keys": []},
        )
        x, y = point()
        clicks = bound.get("num_clicks")
        button = bound.get("button_type")
        modifiers = bound.get("hold_keys") or []
        if (
            not isinstance(clicks, int)
            or isinstance(clicks, bool)
            or not 1 <= clicks <= 3
            or button not in {"left", "right", "middle"}
            or not isinstance(modifiers, list)
            or not all(isinstance(item, str) for item in modifiers)
        ):
            raise ValueError("grounded click parameters are invalid")
        action = _action(
            "click",
            x=x,
            y=y,
            button=button,
            clicks=clicks,
            modifiers=modifiers,
            raw=raw,
        )
        actions = [action]
    elif verb == "type":
        bound = _bind_verb_args(
            ["element_description", "text", "overwrite", "enter"],
            args,
            kwargs,
            {
                "element_description": None,
                "text": "",
                "overwrite": False,
                "enter": False,
            },
        )
        text_value = bound.get("text")
        if not isinstance(text_value, str) or len(text_value) > MAX_INPUT_TEXT_CHARS:
            raise ValueError("grounded type text is invalid")
        x, y = point(required=bound.get("element_description") is not None)
        action = _action(
            "type",
            x=x,
            y=y,
            text=text_value,
            overwrite=bool(bound.get("overwrite")),
            enter=bool(bound.get("enter")),
            raw=raw,
        )
        actions = [action]
    elif verb == "hotkey":
        bound = _bind_verb_args(["keys"], args, kwargs, {"keys": []})
        keys = bound.get("keys")
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(item, str) and item for item in keys)
        ):
            raise ValueError("grounded hotkey keys are invalid")
        action = _action("key", key=keys[-1], modifiers=keys[:-1], raw=raw)
        actions = [action]
    elif verb == "hold_and_press":
        bound = _bind_verb_args(
            ["hold_keys", "press_keys"],
            args,
            kwargs,
            {"hold_keys": [], "press_keys": []},
        )
        hold = bound.get("hold_keys")
        press = bound.get("press_keys")
        if (
            not isinstance(hold, list)
            or not isinstance(press, list)
            or not press
            or not all(isinstance(item, str) and item for item in hold + press)
        ):
            raise ValueError("grounded hold_and_press keys are invalid")
        actions = [
            _action("key", key=key, modifiers=list(hold), raw=raw) for key in press
        ]
        action = actions[0]
    elif verb == "scroll":
        bound = _bind_verb_args(
            ["element_description", "clicks", "shift"],
            args,
            kwargs,
            {"clicks": 0, "shift": False},
        )
        amount = bound.get("clicks")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ValueError("grounded scroll amount is invalid")
        x, y = point()
        action = _action(
            "scroll",
            x=x,
            y=y,
            dx=amount if bound.get("shift") else 0,
            dy=0 if bound.get("shift") else amount,
            raw=raw,
        )
        actions = [action]
    elif verb in {"drag_and_drop", "highlight_text_span"}:
        points = _extract_pyautogui_drag_points(exec_code)
        if points is None:
            raise GroundingError(f"grounded {verb} action has no drag endpoints")
        x, y, x2, y2 = points
        if not all(
            (0 <= x < width, 0 <= x2 < width, 0 <= y < height, 0 <= y2 < height)
        ):
            raise GroundingError("grounded drag endpoint is outside the policy frame")
        button = "left"
        modifiers: list[str] = []
        if verb == "drag_and_drop":
            bound = _bind_verb_args(
                ["starting_description", "ending_description", "hold_keys"],
                args,
                kwargs,
                {"hold_keys": []},
            )
            modifiers = list(bound.get("hold_keys") or [])
        else:
            bound = _bind_verb_args(
                ["starting_phrase", "ending_phrase", "button"],
                args,
                kwargs,
                {"button": "left"},
            )
            button = str(bound.get("button") or "left")
        if button not in {"left", "right", "middle"} or not all(
            isinstance(item, str) for item in modifiers
        ):
            raise ValueError("grounded drag parameters are invalid")
        action = _action(
            "drag",
            x=x,
            y=y,
            x2=x2,
            y2=y2,
            duration=1.0,
            button=button,
            modifiers=modifiers,
            raw=raw,
        )
        actions = [action]
    elif verb == "wait":
        bound = _bind_verb_args(["time"], args, kwargs, {"time": 0.0})
        seconds = float(bound.get("time") or 0.0)
        if not math.isfinite(seconds) or not 0 <= seconds <= MAX_INPUT_ACTION_SECONDS:
            raise ValueError("grounded wait is outside 0..5 seconds")
        action = _action("wait", seconds=seconds, raw=raw)
        actions = [action]
    elif verb == "done":
        action = _action("done", raw=raw)
        actions = [action]
    elif verb == "fail":
        action = _action("fail", reason="", raw=raw)
        actions = [action]
    elif verb == "save_to_knowledge":
        action = _action("wait", seconds=0.0, raw=raw)
        actions = [action]
    else:  # pragma: no cover - parse_background_agent_call owns the allowlist.
        raise ValueError(f"unsupported background Agent-S verb {verb!r}")
    if not 1 <= len(actions) <= MAX_INPUT_BATCH_ACTIONS:
        raise ValueError("structured Agent-S action batch is outside the allowed size")
    return action, actions


def _native_dispatch(code: str, *, plan_code: Any = None) -> dict[str, Any]:
    """Replicate upstream ``cli_app.py``'s done/fail/next/wait/exec branching.

    ``code`` is the pyautogui source ``create_pyautogui_code`` already evaluated
    from the model's plan (e.g. ``agent.done()`` returns the literal string
    ``"DONE"``). Only the final ``else`` branch has a real desktop side effect;
    ``plan_code`` (the model's own, ungrounded ``agent.<verb>(...)`` call) is
    used there purely to pick a friendlier report ``kind`` -- it never changes
    whether/what gets executed.
    """

    lowered = code.lower()
    if "done" in lowered:
        return _action("done", raw=code[:500])
    if "fail" in lowered:
        return _action("fail", reason="", raw=code[:500])
    if "next" in lowered:
        return _action("wait", seconds=0.0, raw=code[:500])
    if "wait" in lowered:
        time.sleep(5.0)
        return _action("wait", seconds=5.0, raw=code[:500])
    time.sleep(1.0)
    _native_exec(code)
    time.sleep(1.0)
    kind, fields = _native_action_fields(plan_code, code)
    return _action(kind, raw=code[:500], **fields)


def _native_exec(code: str) -> None:
    """Run Agent-S-generated pyautogui source with real OS-level side effects.

    This is the one restored native-execution edge; matches upstream
    ``cli_app.py``'s bare ``exec(code[0])`` exactly.
    """

    exec(code)  # noqa: S102 -- intentional native-mode Agent-S execution edge


def _field(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first_field(value: Any, *names: str) -> Any:
    for name in names:
        candidate = _field(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _extract_error_usage(error: Exception) -> Any:
    direct = _field(error, "usage", None)
    if direct is not None:
        return direct
    response = _field(error, "response", None)
    nested = _field(response, "usage", None)
    if nested is not None:
        return nested
    body = _field(error, "body", None)
    return _field(body, "usage", None)


def _usage_audit(
    usage: Any,
    *,
    content: str,
    has_reasoning_content: bool,
    finish_reason: str | None,
    reasoning_expected: bool = False,
    reasoning_disabled: bool = False,
) -> tuple[dict[str, Any], int]:
    """Normalize provider usage while preserving whether each field was present."""

    prompt_raw = _first_field(usage, "prompt_tokens", "input_tokens")
    completion_raw = _first_field(usage, "completion_tokens", "output_tokens")
    total_raw = _field(usage, "total_tokens", None)
    prompt_known = _is_nonnegative_usage_int(prompt_raw)
    completion_known = _is_nonnegative_usage_int(completion_raw)
    total_raw_known = _is_nonnegative_usage_int(total_raw)
    total_known = total_raw_known
    prompt_tokens = _as_nonnegative_int(prompt_raw)
    completion_tokens = _as_nonnegative_int(completion_raw)
    total_tokens = _as_nonnegative_int(total_raw)
    if total_raw_known and prompt_known and completion_known:
        total_known = total_tokens == prompt_tokens + completion_tokens

    completion_details = _field(usage, "completion_tokens_details", None)
    output_details = _field(usage, "output_tokens_details", None)
    prompt_details = _field(usage, "prompt_tokens_details", None)
    input_details = _field(usage, "input_tokens_details", None)
    reasoning_source = "missing"
    reasoning_raw = _first_field(usage, "reasoning_tokens")
    if reasoning_raw is not None:
        reasoning_source = "top_level"
    if reasoning_raw is None:
        reasoning_raw = _first_field(
            completion_details,
            "reasoning_tokens",
        )
        if reasoning_raw is not None:
            reasoning_source = "completion_tokens_details"
    if reasoning_raw is None:
        reasoning_raw = _first_field(output_details, "reasoning_tokens")
        if reasoning_raw is not None:
            reasoning_source = "output_tokens_details"
    reasoning_tokens_value = _as_nonnegative_int(reasoning_raw)
    cached_prompt_tokens = max(
        _as_nonnegative_int(_field(usage, "cached_prompt_tokens", 0)),
        _as_nonnegative_int(_field(prompt_details, "cached_tokens", 0)),
        _as_nonnegative_int(_field(input_details, "cached_tokens", 0)),
        _as_nonnegative_int(_field(prompt_details, "cached_prompt_tokens", 0)),
    )
    finish = str(finish_reason or "").strip().lower()
    reasoning_observed = bool(
        not reasoning_disabled
        and (
            reasoning_expected
            or has_reasoning_content
            or (
                not content.strip()
                and (completion_tokens > 0 or finish in {"length", "max_tokens"})
            )
        )
    )
    reasoning_raw_known = _is_nonnegative_usage_int(reasoning_raw)
    reasoning_detail_status = "exact"
    reasoning_tokens: int | None = reasoning_tokens_value
    if reasoning_raw is None:
        if reasoning_observed:
            reasoning_tokens = None
            reasoning_detail_status = "missing"
        else:
            reasoning_tokens = 0
            reasoning_source = "not_expected"
            reasoning_detail_status = "not_expected"
    elif (
        not reasoning_raw_known
        or reasoning_tokens_value > completion_tokens
        or (has_reasoning_content and reasoning_tokens_value <= 0)
    ):
        reasoning_tokens = None
        reasoning_detail_status = "invalid"
    reasoning_tokens_known = reasoning_tokens is not None
    usage_complete = bool(prompt_known and completion_known and total_known)
    audit = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "prompt_tokens_known": prompt_known,
        "completion_tokens_known": completion_known,
        "total_tokens_known": total_known,
        "reasoning_tokens_known": reasoning_tokens_known,
        "reasoning_expected": reasoning_observed or not reasoning_tokens_known,
        "reasoning_detail_complete": reasoning_tokens_known,
        "reasoning_detail_status": reasoning_detail_status,
        "reasoning_tokens_source": reasoning_source,
        "usage_complete": usage_complete,
    }
    retry_reasoning_tokens = (reasoning_tokens or 0) or int(has_reasoning_content)
    return audit, retry_reasoning_tokens


def _message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _field(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return "" if value is None else str(value)


def _as_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_nonnegative_usage_int(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, label: str) -> int:
    return _bounded_int(value, label, 1, 1_000_000)


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _redact(message: str, config: WorkerConfig | None) -> str:
    if config is None:
        return message
    redacted = message
    for name in {config.api_key_env, config.grounder_api_key_env}:
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[:2_000]


def main() -> None:
    AgentSJsonlServer(sys.stdin, sys.stdout).serve()


if __name__ == "__main__":
    main()


__all__ = [
    "AgentSBindings",
    "AgentSJsonlServer",
    "ChatCompletionsBackend",
    "GroundingError",
    "WorkerConfig",
    "WorkerConfigurationError",
    "build_native_agent_s3",
    "load_agent_s_bindings",
]
