"""Fixed visual bridge used by the formal C1 GUI baselines.

The three decision models in the experiment are intentionally treated as text-only:
one fixed VLM observes every screenshot and emits a compact, coordinate-grounded
description.  This controls the visual front-end across models and also makes routes
that reject ``image_url`` usable without changing the policy under test.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import cost
from .providers import resolve_model
from .model_request import resolve_model_request_profile
from .visual import image_data_url
from .c1_task_deadline import (
    bounded_task_timeout_s,
    raise_task_timeout_if_expired,
)


DEFAULT_OBSERVER_MODEL = "claude-sonnet-5@relay"
DEFAULT_GROUNDER_MODEL = "claude-sonnet-5@relay"


def _usage_fields(usage: Any) -> dict[str, int]:
    """Normalize OpenAI/gateway token usage without retaining reasoning content."""
    return cost.usage_tokens(usage)


def _message_content(response: Any) -> tuple[str, str | None]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "", None
    choice = choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    # Deliberately ignore ``reasoning_content``: it may be sensitive, is not part of
    # the action contract, and the plan only permits metering its token count.
    return str(content or ""), getattr(choice, "finish_reason", None)


class FixedVisualBridge:
    """Screenshot -> structured textual observation with pixel bounding boxes."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_OBSERVER_MODEL,
        base_url: str,
        api_key_env: str,
        max_tokens: int = 8192,
        provider_timeout_s: float | None = None,
    ) -> None:
        from openai import OpenAI

        clean_model, provider = resolve_model(
            model,
            default_base_url=base_url,
            default_api_key_env=api_key_env,
        )
        api_key = os.environ.get(provider.api_key_env, "")
        if not api_key:
            raise RuntimeError(f"{provider.api_key_env} is required for the C1 visual observer.")
        timeout = (
            float(provider_timeout_s)
            if provider_timeout_s is not None
            else float(os.environ.get("WARPTEST_VISUAL_BRIDGE_TIMEOUT", "300"))
        )
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": provider.base_url,
            "timeout": timeout,
            # SDK-internal retries are invisible to the cost ledger.  Explicit
            # C1 attempt/reasoning retries sit above this client and account for
            # every provider request, so the transport itself must be single-shot.
            "max_retries": int(os.environ.get("WARPTEST_VISUAL_BRIDGE_MAX_RETRIES", "0")),
        }
        if provider.extra_headers:
            kwargs["default_headers"] = dict(provider.extra_headers)
        self.client = OpenAI(**kwargs)
        self.model = clean_model
        self.provider_name = provider.name
        self.provider_timeout_s = timeout
        # Formal C1 uses a fixed 8k first response budget.  Keep that floor even
        # if an older caller still passes the historical 4k/64-token value.
        self.max_tokens = max(8192, int(max_tokens))

    def observe(
        self,
        screenshot: Path,
        *,
        instruction: str,
        width: int,
        height: int,
        kind: str = "visual_observer",
    ) -> str:
        prompt = {
            "task": instruction,
            "screenshot_pixels": {"width": int(width), "height": int(height)},
            "request": (
                "Describe the current GUI as strict JSON. Include screen/state summary; "
                "all visible interactive controls and game objects relevant to the task; "
                "for each element include label, role, state, and bbox_px=[x1,y1,x2,y2] in "
                "the exact screenshot pixel coordinate system. Mention blockers, overlays, "
                "selected/focused state, and any visible success evidence. Do not propose an action."
            ),
        }
        messages = [
            {
                "role": "system",
                "content": "You are a deterministic GUI observer. Return JSON only; never infer hidden state.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_data_url(screenshot)}},
                ],
            },
        ]
        max_tokens = self.max_tokens
        for attempt in range(2):
            content, finish_reason, usage = _recorded_completion(
                client=self.client,
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                kind=kind,
                provider_timeout_s=self.provider_timeout_s,
            )
            if content.strip():
                return content.strip()
            if attempt == 0 and str(finish_reason or "").lower() in {
                "length",
                "max_tokens",
            }:
                max_tokens = 16384
                continue
            break
        raise RuntimeError("visual observer returned empty content")


class FixedVisualGrounder(FixedVisualBridge):
    """Locate one requested target in raw screenshot pixel coordinates."""

    def ground(
        self,
        screenshot: Path,
        *,
        instruction: str,
        width: int,
        height: int,
        observation: str | None = None,
    ) -> dict[str, Any]:
        prompt = {
            "task": instruction,
            "screenshot_pixels": {"width": int(width), "height": int(height)},
            "observer_caption": observation,
            "output_contract": {
                "x": "integer screenshot pixel x at the center of the target",
                "y": "integer screenshot pixel y at the center of the target",
                "bbox_px": "[x1,y1,x2,y2] tightly enclosing the target",
                "confidence": "0..1",
            },
            "instructions": "Return strict JSON only. Use the raw image; never scale coordinates to 0..1 or 0..1000.",
        }
        messages = [
            {
                "role": "system",
                "content": "You are a GUI grounding model. Return one target location as JSON only.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_data_url(screenshot)}},
                ],
            },
        ]
        max_tokens = max(8192, self.max_tokens)
        for attempt in range(2):
            content, finish_reason, usage = _recorded_completion(
                client=self.client,
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                kind="agent_s_grounding",
                provider_timeout_s=self.provider_timeout_s,
            )
            if content.strip():
                data = _strict_json_object(content)
                x = int(round(float(data["x"])))
                y = int(round(float(data["y"])))
                bbox = data.get("bbox_px")
                if not (0 <= x < int(width) and 0 <= y < int(height)):
                    raise RuntimeError(f"grounder coordinate out of bounds: ({x}, {y})")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise RuntimeError("grounder response is missing bbox_px")
                return {"x": x, "y": y, "bbox_px": [int(round(float(v))) for v in bbox], "confidence": data.get("confidence")}
            if attempt == 0 and str(finish_reason or "").lower() in {
                "length",
                "max_tokens",
            }:
                max_tokens = 16384
                continue
            break
        raise RuntimeError("visual grounder returned no usable content")


def _recorded_completion(
    *,
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    kind: str,
    provider_timeout_s: float,
) -> tuple[str, str | None, dict[str, int]]:
    requested_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        timeout_s = bounded_task_timeout_s(provider_timeout_s)
        request_client = (
            client.with_options(timeout=timeout_s)
            if timeout_s is not None and hasattr(client, "with_options")
            else client
        )
        response = request_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
    except Exception as error:
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            cost.record_error_usage_call(
                model,
                error=error,
                latency_ms=latency_ms,
                kind=kind,
                messages=messages,
                requested_at=requested_at,
            )
        except cost.UsageAccountingError as accounting_error:
            raise accounting_error from error
        raise_task_timeout_if_expired(error)
        raise
    latency_ms = int((time.perf_counter() - started) * 1000)
    content, finish_reason = _message_content(response)
    usage = cost.record_usage_call(
        model,
        usage=getattr(response, "usage", None),
        content=content,
        finish_reason=finish_reason,
        reasoning_observed=cost.response_has_reasoning_content(response),
        reasoning_expected=(
            resolve_model_request_profile(model).reasoning_expected
        ),
        reasoning_disabled=(
            resolve_model_request_profile(model).profile == "thinking_disabled"
        ),
        latency_ms=latency_ms,
        kind=kind,
        messages=messages,
        response=content,
        requested_at=requested_at,
        endpoint="chat.completions",
        provider_reported_model=getattr(response, "model", None),
        provider_response_id=getattr(response, "id", None),
        system_fingerprint=getattr(response, "system_fingerprint", None),
    )
    return content, finish_reason, usage


def _strict_json_object(content: str) -> dict[str, Any]:
    body = content.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("model did not return strict JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("model JSON response is not an object")
    return value
