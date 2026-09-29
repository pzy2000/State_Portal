"""JSONL worker for the pinned ScaleCUA native-prompt pixel baseline.

ScaleCUA's released agent is a single vision-language model driven by a fixed
native prompt (``playground/config/prompt_template/ours_ubuntu.json`` in the
vendored checkout) that answers with ``<think>/<operation>/<action>`` blocks
containing pyautogui-style calls.  This worker reproduces that loop exactly:
screenshot in, one provider chat-completions call, parsed low-level actions
out.  Like the Agent-S worker it never owns desktop focus, capture, or input:
the only things crossing the JSONL boundary back to the parent are bounded
structured mouse/keyboard actions in ``frame_pixels_top_left_v1`` plus
fail-closed usage events.

No API key is accepted by this module's public configuration.  Key
*environment variable names* cross the JSONL boundary and the worker resolves
their values from its inherited environment.

Coordinate contract: ScaleCUA/Qwen-style models emit absolute coordinates in
the smart-resized model-input space.  The worker normalizes them against the
upstream ``smart_resize`` dimensions of the real screenshot and then scales
them into the parent-sent logical click space (Retina captures are 2x the
logical points, mirroring the Agent-S worker's grounding/click split).
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, IO, Mapping

from . import cost, run_context
from .agent_s_bridge import (
    BACKGROUND_PIXEL_BACKEND,
    FRAME_PIXEL_COORDINATE_SPACE,
    MAX_INPUT_ACTION_SECONDS,
    MAX_INPUT_BATCH_ACTIONS,
    MAX_INPUT_TEXT_CHARS,
)
from .agent_s_worker import (
    _extract_error_usage,
    _message_content,
    _usage_audit,
)
from .c1_provider_runtime import is_temperature_rejection
from .image_history_backoff import create_with_backoff
from .model_request import resolve_model_request_profile


SCALECUA_PROTOCOL_VERSION = 1
# Vendored checkout: baselines/ScaleCUA, upstream OpenGVLab/ScaleCUA main.
PINNED_SCALECUA_COMMIT = "5d92feea9f1e14b8303ce37da45b286fb1f4d3aa"
SCALECUA_INFERENCE_REVISION = "scalecua-native-ours-ubuntu-v1"
SCALECUA_PROMPT_TEMPLATE_PATH = (
    "playground/config/prompt_template/ours_ubuntu.json"
)
SCALECUA_REQUEST_MAX_TOKENS = 8192
# Upstream's own macOS runs fall back to the ubuntu template: the platform
# wording in the system prompt is cosmetic, the action space is the identity.
SCALECUA_PLATFORM_WORDING = "ubuntu"
# ScaleCUA scroll(clicks) counts wheel clicks (positive = up); the parent
# scroll(dx, dy) moves pixels (positive dy = down).  One wheel click is the
# conventional ~120 px browser notch.
SCALECUA_SCROLL_PIXELS_PER_CLICK = 120
SCALECUA_MAX_PROVIDER_ATTEMPTS_PER_PREDICT = 3

# Qwen-VL smart-resize constants, mirrored from upstream agents/utils.py so
# coordinate normalization matches the grounding space the model was trained
# and evaluated in.
_IMAGE_FACTOR = 28
_MIN_PIXELS = 3136
_MAX_PIXELS = 2109744
_MAX_RATIO = 200


class ScaleCUAWorkerError(RuntimeError):
    """Raised when the worker cannot honour a request."""


class ScaleCUABindings:
    """Verified vendored-checkout identity plus the loaded prompt template."""

    def __init__(self, *, root: Path, commit: str, template: dict[str, str]) -> None:
        self.root = root
        self.commit = commit
        self.template = template


def _round_by_factor(number: float, factor: int) -> int:
    return round(number / factor) * factor


def _ceil_by_factor(number: float, factor: int) -> int:
    return math.ceil(number / factor) * factor


def _floor_by_factor(number: float, factor: int) -> int:
    return math.floor(number / factor) * factor


def smart_resize(
    height: int,
    width: int,
    *,
    factor: int = _IMAGE_FACTOR,
    min_pixels: int = _MIN_PIXELS,
    max_pixels: int = _MAX_PIXELS,
) -> tuple[int, int]:
    """Qwen-VL input resize, mirrored from the vendored upstream utility."""

    if max(height, width) / min(height, width) > _MAX_RATIO:
        raise ValueError(
            f"absolute aspect ratio must be smaller than {_MAX_RATIO}, "
            f"got {max(height, width) / min(height, width)}"
        )
    h_bar = max(factor, _round_by_factor(height, factor))
    w_bar = max(factor, _round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor_by_factor(height / beta, factor)
        w_bar = _floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil_by_factor(height * beta, factor)
        w_bar = _ceil_by_factor(width * beta, factor)
    return h_bar, w_bar


def _git_output(root: Path, *args: str) -> str:
    command = ["git", "-C", str(root), *args]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ScaleCUAWorkerError(
            f"could not inspect vendored ScaleCUA checkout at {root}: {error}"
        ) from error
    return completed.stdout.strip()


def load_scalecua_bindings(root: Path) -> ScaleCUABindings:
    """Verify the pinned, clean, parent-pinned checkout and load its prompt."""

    root = root.resolve()
    if not root.is_dir():
        raise ScaleCUAWorkerError(
            f"ScaleCUA submodule is unavailable at {root}; "
            "run `git submodule update --init baselines/ScaleCUA`"
        )
    head = _git_output(root, "rev-parse", "HEAD")
    if head != PINNED_SCALECUA_COMMIT:
        raise ScaleCUAWorkerError(
            f"ScaleCUA checkout HEAD {head} does not match the pinned commit "
            f"{PINNED_SCALECUA_COMMIT}"
        )
    dirty = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ScaleCUAWorkerError(
            "the pinned ScaleCUA checkout has tracked modifications; refusing use"
        )
    parent_root = root.parent.parent
    gitlink = _git_output(
        parent_root, "ls-files", "-s", "--", str(root.relative_to(parent_root))
    )
    fields = gitlink.split()
    if len(fields) < 4 or fields[0] != "160000" or fields[1] != PINNED_SCALECUA_COMMIT:
        raise ScaleCUAWorkerError(
            "parent repository does not pin baselines/ScaleCUA to the expected "
            f"commit ({PINNED_SCALECUA_COMMIT})"
        )
    template_path = root / SCALECUA_PROMPT_TEMPLATE_PATH
    if not template_path.is_file():
        raise ScaleCUAWorkerError(
            f"ScaleCUA prompt template is missing: {template_path}"
        )
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScaleCUAWorkerError(
            f"could not parse ScaleCUA prompt template: {error}"
        ) from error
    for key in (
        "sys_prompt_planning_cot",
        "sys_prompt_planning_withoutcot",
        "user_prompt_planning",
    ):
        if not isinstance(template.get(key), str) or not template[key].strip():
            raise ScaleCUAWorkerError(
                f"ScaleCUA prompt template is missing {key!r}"
            )
    return ScaleCUABindings(root=root, commit=head, template=template)


class ScaleCUAChatClient:
    """Single-shot OpenAI-compatible transport with auditable usage events."""

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
        kind: str = "scalecua_worker",
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.default_headers = dict(default_headers)
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.usage_sink = usage_sink
        self.kind = kind
        self.omit_temperature_after_rejection = False

    def _get_client(self) -> Any:
        from openai import OpenAI

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ScaleCUAWorkerError(
                f"required API key environment variable {self.api_key_env} is unset"
            )
        return OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout_s,
            max_retries=self.max_retries,
            default_headers=self.default_headers,
        )

    def create(self, **kwargs: Any) -> Any:
        def send() -> Any:
            return create_with_backoff(
                self._get_client().chat.completions.create,
                kwargs,
                extract_error_usage=_extract_error_usage,
                kind=self.kind,
                audit=[],
            )

        model = str(kwargs.get("model") or "")
        request_profile = resolve_model_request_profile(model)
        kwargs.update(request_profile.request_kwargs())
        kwargs.setdefault("max_tokens", SCALECUA_REQUEST_MAX_TOKENS)
        if self.temperature is not None:
            if self.omit_temperature_after_rejection:
                kwargs.pop("temperature", None)
            else:
                kwargs["temperature"] = self.temperature
        messages = cost.sanitize_messages_for_ledger(kwargs.get("messages"))
        requested_at = _utc_now_iso()
        start = time.perf_counter()
        try:
            response = send()
        except Exception as error:
            if "temperature" in kwargs and is_temperature_rejection(error):
                self.omit_temperature_after_rejection = True
                self._record_error(
                    error,
                    model=model,
                    messages=messages,
                    requested_at=requested_at,
                    start=start,
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
                        messages=cost.sanitize_messages_for_ledger(
                            kwargs.get("messages")
                        ),
                        requested_at=requested_at,
                        start=start,
                    )
                    raise
            else:
                self._record_error(
                    error,
                    model=model,
                    messages=messages,
                    requested_at=requested_at,
                    start=start,
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
        usage_audit, _ = _usage_audit(
            usage,
            content=content,
            has_reasoning_content=has_reasoning_content,
            finish_reason=finish_reason,
            reasoning_expected=request_profile.reasoning_expected,
            reasoning_disabled=request_profile.profile == "thinking_disabled",
        )
        response_id = _optional_text(_field(response, "id", None))
        reasoning_disabled = request_profile.profile == "thinking_disabled"
        reasoning_token_anomaly = bool(
            reasoning_disabled and int(usage_audit.get("reasoning_tokens") or 0) > 0
        )
        request_contract_passed = not (reasoning_disabled and has_reasoning_content)
        self.usage_sink.append(
            {
                "model": model,
                "kind": self.kind,
                **usage_audit,
                "latency_ms": latency_ms,
                "finish_reason": finish_reason,
                "endpoint": "chat.completions",
                "provider_reported_model": _optional_text(
                    _field(response, "model", None)
                ),
                "provider_response_id_sha256": (
                    _sha256_text(response_id) if response_id else None
                ),
                "system_fingerprint": _optional_text(
                    _field(response, "system_fingerprint", None)
                ),
                "status": "ok",
                "messages": messages,
                "response": content if request_contract_passed else None,
                "reasoning_content_observed": has_reasoning_content,
                "reasoning_token_anomaly": reasoning_token_anomaly,
                "request_contract_passed": request_contract_passed,
                "requested_at": requested_at,
            }
        )
        return content

    def _record_error(
        self,
        error: Exception,
        *,
        model: str,
        messages: Any,
        requested_at: str,
        start: float,
    ) -> None:
        error_usage = _extract_error_usage(error)
        # Match the shared Agent-S accounting boundary: an explicit rejection
        # before generation is not an unknown paid completion. Preserve usage
        # supplied on errors, and keep ambiguous timeouts fail-closed.
        if error_usage is None and not cost.error_may_have_generated(error):
            return
        profile = resolve_model_request_profile(model)
        usage_audit, _ = _usage_audit(
            error_usage,
            content="",
            has_reasoning_content=False,
            finish_reason=None,
            reasoning_expected=profile.reasoning_expected,
            reasoning_disabled=profile.profile == "thinking_disabled",
        )
        status_code = getattr(error, "status_code", None)
        self.usage_sink.append(
            {
                "model": model,
                "kind": self.kind,
                **usage_audit,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "finish_reason": None,
                "endpoint": "chat.completions",
                "status": "error",
                "error_type": type(error).__name__,
                "status_code": status_code if isinstance(status_code, int) else None,
                "messages": messages,
                "response": None,
                "request_contract_passed": True,
                "requested_at": requested_at,
            }
        )


# --------------------------------------------------------------------------
# Response parsing and action mapping (ScaleCUA native format).
# --------------------------------------------------------------------------

_ACTION_BLOCK = re.compile(r"<action>\s*(.*?)\s*</action>", re.DOTALL)
_OPERATION_BLOCK = re.compile(r"<operation>\s*(.*?)\s*</operation>", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>\s*(.*?)\s*</think>", re.DOTALL)
_VERB_CALL = re.compile(r"(\w+)\((.*)\)\s*$", re.DOTALL)
# Non-native fallback shapes seen from general-purpose decision models that
# were not trained on the ScaleCUA prompt: JSON action arrays and XML key
# tags.  The native pyautogui grammar always takes precedence.
_XML_KEY_TAG = re.compile(
    r"<(press|type|write|click|hotkey|scroll|drag|wait|done|terminate|response)>"
    r"\s*(.*?)\s*</\1>",
    re.DOTALL,
)


def _json_action_records(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse ``[{"name": ..., "args": {...}}]``-style action records."""

    stripped = text.strip()
    records: list[tuple[str, dict[str, Any]]] = []
    candidates: list[str] = []
    block = re.search(r"\[.*\]", stripped, re.DOTALL)
    if block:
        candidates.append(block.group(0))
    if stripped.startswith("{"):
        candidates.append(stripped)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            verb = item.get("name") or item.get("function") or item.get("verb")
            if not isinstance(verb, str) or not verb.strip():
                continue
            args_value = (
                item.get("args")
                if item.get("args") is not None
                else item.get("parameters")
                if item.get("parameters") is not None
                else item.get("arguments")
            )
            args: dict[str, Any] = {}
            if isinstance(args_value, dict):
                args.update(args_value)
            elif args_value is not None:
                args["args"] = args_value if isinstance(args_value, list) else [args_value]
            records.append((verb.strip(), args))
        if records:
            return records
    return records


def _xml_key_tag_records(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse ``<press>Left</press>``-style single-key XML action tags."""

    records: list[tuple[str, dict[str, Any]]] = []
    for match in _XML_KEY_TAG.finditer(text or ""):
        verb = match.group(1)
        body = match.group(2).strip()
        args: dict[str, Any] = {}
        if verb == "press":
            args = {"keys": body} if body else {}
        elif verb in ("type", "write"):
            args = {"message": body}
        elif verb in ("done", "response"):
            args = {"answer": body}
        elif verb == "terminate":
            args = {"status": body or "success"}
        elif verb == "wait":
            args = {"seconds": _coerce_scalar(body) if body else 3}
        elif verb in ("click", "scroll", "drag", "hotkey"):
            parsed = parse_verb_call(f"{verb}({body})")
            if parsed is None:
                args = {"args": [body]} if body else {}
            else:
                records.append(parsed)
                continue
        records.append((verb, args))
    return records


def parse_scalecua_response(response: str) -> tuple[str | None, str | None, list[str]]:
    """Split a native response into (think, operation, action-line list)."""

    action_lines: list[str] = []
    for match in _ACTION_BLOCK.finditer(response or ""):
        for line in match.group(1).split("\n"):
            line = line.strip()
            if line:
                action_lines.append(line)
    operation_match = _OPERATION_BLOCK.search(response or "")
    operation = operation_match.group(1).strip() if operation_match else None
    think_match = _THINK_BLOCK.search(response or "")
    think = think_match.group(1).strip() if think_match else None
    return think, operation, action_lines


def parse_verb_call(line: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one ``verb(key=value, ...)`` call into a structured record.

    Mirrors the vendored ``NativeAgent.parse_action`` argument grammar
    (keyword lists, quoted strings, numbers, booleans) without inheriting its
    whole-batch ``None`` quirk: an unparsable line is reported per line.
    """

    match = _VERB_CALL.match(line.strip())
    if not match:
        return None
    verb = match.group(1)
    args_str = match.group(2).strip()
    args: dict[str, Any] = {}
    if "=" in args_str:
        for arg in re.finditer(r"(\w+)=\[([^\]]+)\]", args_str):
            param = arg.group(1)
            list_items = []
            for item in re.finditer(r"'([^']*)'|\"([^\"]*)\"|([^,\]]+)", arg.group(2)):
                value = (item.group(1) or item.group(2) or item.group(3)).strip()
                if value:
                    list_items.append(value.strip("\"'"))
            args[param] = list_items
        for arg in re.finditer(r"(\w+)=([^,)]+)", args_str):
            param = arg.group(1)
            if param in args:
                continue
            args[param] = _coerce_scalar(arg.group(2).strip())
    else:
        args_list = []
        for arg in re.finditer(r"'([^']*)'|\"([^\"]*)\"|([^,]+)", args_str):
            value = (arg.group(1) or arg.group(2) or arg.group(3)).strip()
            if value:
                args_list.append(value.strip("\"'"))
        if args_list:
            args["args"] = args_list
    return verb, args


def _coerce_scalar(value_str: str) -> Any:
    if value_str.isdigit():
        return int(value_str)
    if value_str.replace(".", "", 1).isdigit():
        return float(value_str)
    if value_str.lower() in ("true", "false"):
        return value_str.lower() == "true"
    return value_str.strip("\"'")


class ScaleCUAActionMapper:
    """Map parsed ScaleCUA verbs onto the bounded parent-executable vocabulary.

    The parent owns the pointer, so click-without-coordinates and ``dragTo``
    are resolved against a worker-tracked cursor position, mirroring the
    persistent-cursor semantics of the pyautogui calls the model was trained
    to emit.
    """

    def __init__(self, *, width: int, height: int) -> None:
        self.width = int(width)
        self.height = int(height)
        self.cursor: tuple[int, int] | None = None

    def map(
        self, verb: str, args: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | list[dict[str, Any]] | None, str | None]:
        """Return (action dict, action list, or None; skip reason or None)."""

        def coord_x(value: Any) -> int:
            return self._to_logical_x(value)

        def coord_y(value: Any) -> int:
            return self._to_logical_y(value)

        if verb in ("click", "doubleClick", "rightClick"):
            x, y = self._resolve_point(args)
            if x is None:
                return None, f"{verb} without coordinates and unknown cursor"
            clicks = int(args.get("clicks", 2 if verb == "doubleClick" else 1))
            button = str(args.get("button", "right" if verb == "rightClick" else "left"))
            self.cursor = (x, y)
            return {
                "kind": "click",
                "x": x,
                "y": y,
                "clicks": max(1, min(3, clicks)),
                "button": button,
                "raw": f"{verb}({args})",
            }, None
        if verb == "moveTo":
            x = coord_x(args.get("x"))
            y = coord_y(args.get("y"))
            self.cursor = (x, y)
            return None, "moveTo is cursor-only under parent-owned execution"
        if verb == "dragTo":
            x2 = coord_x(args.get("x"))
            y2 = coord_y(args.get("y"))
            if self.cursor is None:
                return None, "dragTo without coordinates and unknown cursor"
            x1, y1 = self.cursor
            self.cursor = (x2, y2)
            return {
                "kind": "drag",
                "x": x1,
                "y": y1,
                "x2": x2,
                "y2": y2,
                "button": str(args.get("button", "left")),
                "raw": f"dragTo({args})",
            }, None
        if verb == "scroll":
            clicks = float(args.get("clicks", 0) or 0)
            amount = int(round(clicks * SCALECUA_SCROLL_PIXELS_PER_CLICK))
            if args.get("x") is not None and args.get("y") is not None:
                self.cursor = (coord_x(args.get("x")), coord_y(args.get("y")))
            # Upstream convention: positive clicks scroll up.
            return {"kind": "scroll", "dx": 0, "dy": -amount, "raw": f"scroll({args})"}, None
        if verb == "press":
            keys = args.get("keys", args.get("args"))
            presses = int(args.get("presses", 1) or 1)
            if isinstance(keys, str):
                keys = [keys]
            if not keys:
                return None, "press without keys"
            actions: list[dict[str, Any]] = []
            for _ in range(max(1, presses)):
                for single in keys:
                    actions.append(
                        {"kind": "key", "key": str(single), "raw": f"press({args})"}
                    )
            return actions, None
        if verb == "hotkey":
            keys = args.get("args")
            if not keys or not isinstance(keys, list):
                return None, "hotkey without keys"
            return {
                "kind": "key",
                "key": str(keys[-1]),
                "modifiers": [str(part) for part in keys[:-1]],
                "raw": f"hotkey({args})",
            }, None
        if verb in ("keyDown", "keyUp"):
            return None, f"{verb} is not representable in the bounded vocabulary"
        if verb == "write":
            message = args.get("message")
            if message is None and isinstance(args.get("args"), list):
                message = " ".join(str(part) for part in args["args"])
            if message is None:
                return None, "write without message"
            text = str(message)[:MAX_INPUT_TEXT_CHARS]
            return {"kind": "type", "text": text, "raw": f"write({args})"}, None
        if verb == "wait":
            seconds = float(args.get("seconds", 3) or 0)
            seconds = max(0.0, min(MAX_INPUT_ACTION_SECONDS, seconds))
            return {"kind": "wait", "seconds": seconds, "raw": f"wait({args})"}, None
        if verb == "response":
            answer = args.get("answer")
            if answer is None and isinstance(args.get("args"), list) and args["args"]:
                answer = args["args"][0]
            return {
                "kind": "done",
                "reason": str(answer or "")[:500],
                "raw": f"response({args})",
            }, None
        if verb == "terminate":
            status = str(args.get("status", "success"))
            info = args.get("info")
            kind = "done" if status.lower() == "success" else "fail"
            return {
                "kind": kind,
                "reason": str(info or status)[:500],
                "raw": f"terminate({args})",
            }, None
        if verb == "call_user":
            return {"kind": "fail", "reason": "call_user", "raw": "call_user()"}, None
        return None, f"unsupported ScaleCUA verb {verb!r}"

    def _resolve_point(self, args: Mapping[str, Any]) -> tuple[int | None, int | None]:
        x = args.get("x")
        y = args.get("y")
        if x is None or y is None:
            if self.cursor is None:
                return None, None
            return self.cursor
        return self._to_logical_x(x), self._to_logical_y(y)

    def _to_logical_x(self, value: Any) -> int:
        return self._to_logical(value, self.width)

    def _to_logical_y(self, value: Any) -> int:
        return self._to_logical(value, self.height)

    @staticmethod
    def _to_logical(value: Any, extent: int) -> int:
        if isinstance(value, bool) or value is None:
            raise ScaleCUAWorkerError(f"invalid coordinate {value!r}")
        return max(0, min(extent - 1, int(float(value))))


def build_scalecua_actions(
    response: str,
    *,
    screenshot_bytes: bytes,
    width: int,
    height: int,
) -> tuple[str | None, str | None, list[dict[str, Any]], list[str]]:
    """Parse a native response into bounded action dicts in click space.

    Returns (thought, operation, actions, skipped-notes).  Coordinates are
    normalized in the upstream smart-resize space and scaled into the
    parent-sent logical frame.
    """

    from PIL import Image

    think, operation, action_lines = parse_scalecua_response(response)
    if not action_lines:
        # Non-native fallbacks for decision models that were not trained on
        # the ScaleCUA prompt: JSON action arrays and XML key tags.
        fallback = _json_action_records(response or "") or _xml_key_tag_records(
            response or ""
        )
        if fallback:
            action_lines = [f"__structured__{index}" for index in range(len(fallback))]
        else:
            return think, operation, [], ["response carried no <action> block"]
    image = Image.open(io.BytesIO(screenshot_bytes))
    pixel_width, pixel_height = image.size
    resize_height, resize_width = smart_resize(pixel_height, pixel_width)
    mapper = ScaleCUAActionMapper(width=int(width), height=int(height))
    actions: list[dict[str, Any]] = []
    skipped: list[str] = []
    structured = (
        _json_action_records(response or "") or _xml_key_tag_records(response or "")
        if action_lines and action_lines[0].startswith("__structured__")
        else None
    )
    for index, line in enumerate(action_lines):
        if structured is not None:
            parsed = structured[index] if index < len(structured) else None
        else:
            parsed = parse_verb_call(line)
        if parsed is None:
            skipped.append(f"unparsable action line: {line[:200]}")
            continue
        verb, args = parsed
        # Normalize absolute coordinates from the model-input space into the
        # logical click space before mapping (the mapper scales nothing).
        args = _normalize_coord_args(verb, args, resize_width, resize_height,
                                     int(width), int(height))
        mapped, skip_reason = mapper.map(verb, args)
        if skip_reason is not None:
            skipped.append(f"{verb}: {skip_reason}")
        if mapped is None:
            continue
        if isinstance(mapped, list):
            actions.extend(mapped)
        else:
            actions.append(mapped)
    if len(actions) > MAX_INPUT_BATCH_ACTIONS:
        actions = actions[:MAX_INPUT_BATCH_ACTIONS]
        skipped.append("action batch truncated at the 64-action protocol bound")
    return think, operation, actions, skipped


def _normalize_coord_args(
    verb: str,
    args: dict[str, Any],
    resize_width: int,
    resize_height: int,
    frame_width: int,
    frame_height: int,
) -> dict[str, Any]:
    """Scale model-space x/y (and list-valued hotkey args untouched)."""

    normalized = dict(args)

    def scale(value: Any, resized: int, extent: int) -> Any:
        if value is None or isinstance(value, (list, dict)):
            return value
        try:
            return float(value) / resized * extent
        except (TypeError, ValueError):
            return value

    if verb in ("click", "doubleClick", "rightClick", "moveTo", "dragTo", "scroll"):
        normalized["x"] = scale(args.get("x"), resize_width, frame_width)
        normalized["y"] = scale(args.get("y"), resize_height, frame_height)
    return normalized


# --------------------------------------------------------------------------
# Worker configuration and JSONL server.
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


class WorkerConfig:
    """Non-secret worker configuration for one ScaleCUA process."""

    def __init__(self, mapping: Mapping[str, Any]) -> None:
        self.model = _nonempty_text(mapping.get("model"), "model")
        self.base_url = _nonempty_text(mapping.get("base_url"), "base_url")
        self.api_key_env = _env_name(mapping.get("api_key_env"), "api_key_env")
        headers = mapping.get("default_headers") or {}
        if not isinstance(headers, Mapping):
            raise ValueError("default_headers must be a mapping")
        self.default_headers = {
            str(k): str(v)
            for k, v in headers.items()
            if str(k).lower() in {"x-end-user", "x-custom-source"}
        }
        temperature = mapping.get("temperature", 0.2)
        if temperature is not None:
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                raise ValueError("temperature must be a number or None")
        self.temperature = None if temperature is None else float(temperature)
        self.timeout_s = float(mapping.get("timeout_s") or 900.0)
        self.max_retries = int(mapping.get("max_retries", 0))
        self.scalecua_root = Path(str(mapping.get("scalecua_root") or "")).expanduser()
        self.native_screen_width = int(mapping.get("native_screen_width") or 0)
        self.native_screen_height = int(mapping.get("native_screen_height") or 0)
        self.execution_backend = str(
            mapping.get("execution_backend") or BACKGROUND_PIXEL_BACKEND
        )
        self.coordinate_space = str(
            mapping.get("coordinate_space") or FRAME_PIXEL_COORDINATE_SPACE
        )
        self.platform = str(mapping.get("platform") or "darwin")
        if self.execution_backend != BACKGROUND_PIXEL_BACKEND:
            raise ValueError(
                "ScaleCUA worker supports only background_pixel_v1 execution"
            )
        if self.coordinate_space != FRAME_PIXEL_COORDINATE_SPACE:
            raise ValueError(
                f"coordinate_space must be {FRAME_PIXEL_COORDINATE_SPACE!r}"
            )
        if not self.scalecua_root.is_dir():
            raise ValueError("scalecua_root must be an existing directory")
        if self.native_screen_width <= 0 or self.native_screen_height <= 0:
            raise ValueError("native screen dimensions must be positive")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "WorkerConfig":
        return cls(mapping)


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _env_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{label} must name an environment variable")
    return value


def _redact(message: str, config: WorkerConfig | None) -> str:
    if config is None:
        return message[:2_000]
    redacted = message
    secret = os.environ.get(config.api_key_env)
    if secret:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[:2_000]


class ScaleCUAJsonlServer:
    """Stateful init/reset/configure_surface/predict/close JSONL server."""

    def __init__(
        self,
        in_stream: IO[str],
        out_stream: IO[str],
        *,
        client_factory: Callable[..., ScaleCUAChatClient] = ScaleCUAChatClient,
        bindings_loader: Callable[[Path], ScaleCUABindings] = load_scalecua_bindings,
    ) -> None:
        self.in_stream = in_stream
        self.out_stream = out_stream
        self.client_factory = client_factory
        self.bindings_loader = bindings_loader
        self.config: WorkerConfig | None = None
        self.bindings: ScaleCUABindings | None = None
        self.client: ScaleCUAChatClient | None = None
        self.usage_events: list[dict[str, Any]] = []
        self.current_task: str | None = None
        self.last_frame_by_session: dict[str, tuple[int, str]] = {}
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
                    f"[scalecua-worker] {type(error).__name__}: {message}",
                    file=sys.stderr,
                    flush=True,
                )
                error_payload: dict[str, Any] = {
                    "type": type(error).__name__,
                    "message": message,
                }
                status_code = getattr(error, "status_code", None)
                if isinstance(status_code, int):
                    error_payload["status_code"] = status_code
                provider_capacity = run_context.provider_capacity_failure(error)
                if provider_capacity is not None:
                    error_payload["provider_capacity"] = provider_capacity
                if (
                    isinstance(request, dict)
                    and request.get("op") == "predict"
                    and self.usage_events
                ):
                    error_payload["usage_events"] = [
                        dict(event) for event in self.usage_events
                    ]
                response = {"id": request_id, "ok": False, "error": error_payload}
            self.out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            self.out_stream.flush()
            if self.closed:
                break

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "init":
            return self._handle_init(request)
        if op == "reset":
            return self._handle_reset(request)
        if op == "configure_surface":
            return self._handle_configure_surface(request)
        if op == "predict":
            return self._handle_predict(request)
        if op == "close":
            self.closed = True
            return {"status": "closed"}
        raise ValueError(f"unsupported op {op!r}")

    def _handle_init(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self.config is not None:
            raise RuntimeError("worker is already initialized")
        config = WorkerConfig.from_mapping(request.get("config") or {})
        bindings = self.bindings_loader(config.scalecua_root)
        self.config = config
        self.bindings = bindings
        self.usage_events = []
        self.client = self.client_factory(
            model=config.model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            default_headers=config.default_headers,
            temperature=config.temperature,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            usage_sink=self.usage_events,
        )
        return {
            "protocol_version": SCALECUA_PROTOCOL_VERSION,
            "scalecua_commit": bindings.commit,
            "scalecua_inference_revision": SCALECUA_INFERENCE_REVISION,
            "runtime_mode": "background_pixel",
            "execution_backend": config.execution_backend,
            "coordinate_space": config.coordinate_space,
            "raw_screenshot_observation": True,
            "capture_backend": "runtime_policy_frame",
            "action_execution_owner": "parent_runtime",
            "foreground_required": False,
            "native_exec_enabled": False,
            "local_code_enabled": False,
            "native_screen_width": config.native_screen_width,
            "native_screen_height": config.native_screen_height,
            "native_click_width": config.native_screen_width,
            "native_click_height": config.native_screen_height,
            "provider_timeout_s": float(config.timeout_s),
            "max_provider_attempts_per_predict": (
                SCALECUA_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
            ),
            "model_request_profiles": {
                "worker": resolve_model_request_profile(config.model).provenance(),
            },
            "temperature_contract": {
                "requested": config.temperature,
                "omit_after_rejection": False,
            },
            "worker_observation_mode": "screenshot",
        }

    def _handle_reset(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._require_ready()
        task = request.get("task")
        if task is not None and not isinstance(task, str):
            raise ValueError("task must be a string or null")
        self.current_task = task
        self.last_frame_by_session.clear()
        return {"status": "reset", "task": task}

    def _handle_configure_surface(self, request: Mapping[str, Any]) -> dict[str, Any]:
        config = self._require_ready()
        width = request.get("width")
        height = request.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise ValueError("surface width and height must be integers")
        if width <= 0 or height <= 0:
            raise ValueError("surface width and height must be positive")
        if width != config.native_screen_width or height != config.native_screen_height:
            config.native_screen_width = width
            config.native_screen_height = height
        return {
            "native_screen_width": config.native_screen_width,
            "native_screen_height": config.native_screen_height,
            "native_click_width": config.native_screen_width,
            "native_click_height": config.native_screen_height,
        }

    def _handle_predict(self, request: Mapping[str, Any]) -> dict[str, Any]:
        config = self._require_ready()
        assert self.client is not None and self.bindings is not None
        instruction = request.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be a non-empty string")
        screenshot_path = Path(str(request.get("screenshot") or "")).expanduser()
        if not screenshot_path.is_file():
            raise ValueError(f"screenshot is missing: {screenshot_path}")
        width = request.get("width")
        height = request.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise ValueError("predict width and height must be integers")
        if width <= 0 or height <= 0:
            raise ValueError("predict width and height must be positive")
        policy_frame = request.get("policy_frame")
        if not isinstance(policy_frame, dict):
            raise ValueError("background prediction requires a policy frame")
        session_id = str(policy_frame.get("session_id") or "")
        frame_id = policy_frame.get("frame_id")
        frame_sha = str(policy_frame.get("sha256") or "")
        if not session_id or not isinstance(frame_id, int) or frame_id < 0:
            raise ValueError("policy frame identity is invalid")
        actual_sha = _sha256_file(screenshot_path)
        if actual_sha != frame_sha:
            raise ValueError("policy frame sha256 does not match the screenshot")
        last = self.last_frame_by_session.get(session_id)
        if last is not None and frame_id <= last[0]:
            raise ValueError("policy frame ids must increase within a session")
        self.last_frame_by_session[session_id] = (frame_id, frame_sha)
        screenshot_bytes = screenshot_path.read_bytes()
        history = request.get("history")
        if not isinstance(history, list):
            history = []
        history_lines = [str(item) for item in history if str(item).strip()]
        user_prompt = self.bindings.template["user_prompt_planning"].format(
            instruction=instruction,
            actions="\n".join(history_lines) if history_lines else None,
        )
        # Upstream truncates the planner transcript to the system prompt plus
        # the newest user turn: one stateless decision per screenshot.
        messages = [
            {
                "role": "system",
                "content": self.bindings.template["sys_prompt_planning_withoutcot"],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + _b64_png(screenshot_bytes)
                        },
                    },
                ],
            },
        ]
        del config, request  # only client/bindings/frames participate in prediction
        self.usage_events.clear()
        # Keep the child's stdout a pure JSONL channel: any third-party print
        # triggered inside the provider call is diverted to stderr.
        import contextlib

        with contextlib.redirect_stdout(sys.stderr):
            response_text = self.client.create(
                model=self.client.model, messages=messages
            )
        usage_accounting_complete = bool(self.usage_events) and all(
            event.get("usage_complete") is not False
            for event in self.usage_events
            if event.get("status") == "ok"
        )
        think, operation, actions, skipped = build_scalecua_actions(
            response_text,
            screenshot_bytes=screenshot_bytes,
            width=width,
            height=height,
        )
        if not actions:
            raise ScaleCUAWorkerError(
                "ScaleCUA response produced no executable action: "
                + "; ".join(skipped or ["empty action list"])[:500]
            )
        primary = actions[0]
        return {
            "action": primary,
            "actions": actions,
            "usage_events": [dict(event) for event in self.usage_events],
            "usage_accounting_complete": usage_accounting_complete,
            "info": {
                "turn": len(history_lines) + 1,
                "thought": think,
                "operation": operation,
                "skipped_notes": skipped,
                "inference_revision": SCALECUA_INFERENCE_REVISION,
            },
        }

    def _require_ready(self) -> WorkerConfig:
        if self.config is None or self.bindings is None or self.client is None:
            raise RuntimeError("worker is not initialized")
        return self.config


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _b64_png(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def main() -> None:
    server = ScaleCUAJsonlServer(sys.stdin, sys.stdout)
    server.serve()


if __name__ == "__main__":
    main()
