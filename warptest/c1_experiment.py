"""Credential loading and explicit online preflight for the formal C1 run.

Nothing in this module performs a network request at import time.  The only
online entry point is :func:`run_c1_preflight`; callers must invoke it
explicitly.  The local ``test_model_batch.py`` credential source is parsed as
syntax and never imported or executed.
"""
from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import hashlib
import io
import json
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import cost
from . import run_context
from .c1_provenance import (
    C1EnvironmentMismatch,
    c1_application_revisions,
    c1_harness_hash,
    validate_c1_environment,
)
from .c1_provider_runtime import (
    C1_PROVIDER_TIMEOUT_S,
    is_temperature_rejection,
    manifest_provider_timeout_s,
)
from .model_request import (
    canonical_profiles_json,
    model_request_profiles_environment,
    resolve_model_request_profile,
)
from .providers import ProviderConfig, resolve_model


DEFAULT_CREDENTIAL_SOURCE = Path("test_model_batch.py")
DECISION_MODELS = (
    "gpt-5.6-terra@relay",
    "gemini-3.5-flash@relay",
    "kimi-k2.6@relay",
)
OBSERVER_MODEL = "claude-sonnet-5@relay"
GROUNDER_MODEL = "claude-sonnet-5@relay"
JUDGE_MODEL = "qwen3-vl-flash@relay"
PREFLIGHT_SCHEMA_VERSION = 4

# Sentinel ``visual_bridge.grounder_model`` value: the Agent-S visual grounder
# follows the per-invocation decision model instead of a single fixed model.
# This lets a multi-model manifest keep one bridge block while still routing
# each trial's grounding through that trial's own policy model.
GROUNDER_MATCH_DECISION_SENTINEL = "match_decision_model"


def grounder_follows_decision_model(configured: Any) -> bool:
    """Return whether the manifest ties grounding to the decision model."""

    return configured == GROUNDER_MATCH_DECISION_SENTINEL


def resolve_grounder_model(
    configured: Any,
    *,
    observer_model: str,
    decision_model: str,
) -> str:
    """Resolve a configured grounder into a concrete provider model string.

    * ``match_decision_model`` -> the per-invocation ``decision_model`` so the
      grounder shares the policy model.
    * empty / ``None`` -> the ``observer_model`` (the historical default).
    * any other value -> that literal value.
    """

    if grounder_follows_decision_model(configured):
        return str(decision_model)
    return str(configured or observer_model)


def calibration_grounder_model(configured: Any, *, observer_model: str) -> str:
    """Return the fixed grounder used by the background-pixel calibration.

    Calibration proves the model-independent input/capture protocol, so when a
    run ties grounding to the decision model the calibration still exercises the
    fixed observer model rather than any single policy model.
    """

    if grounder_follows_decision_model(configured):
        return str(observer_model)
    return str(configured or observer_model)

_CREDENTIAL_NAMES = frozenset({"SECRET", "BASE", "END_USER"})
_REQUIRED_CREDENTIAL_NAMES = frozenset({"SECRET", "BASE"})
_ENV_NAMES = (
    "RELAY_API_KEY",
    "RELAY_BASE_URL",
    "RELAY_END_USER",
)


class C1CredentialError(ValueError):
    """Raised when the local credential source violates the literal contract."""


def _validated_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise C1CredentialError("BASE must be a valid HTTP(S) URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 for char in value)
    ):
        raise C1CredentialError(
            "BASE must be an HTTP(S) URL without credentials, query, or fragment"
        )
    return value.rstrip("/")


@dataclass(frozen=True, repr=False)
class _RelayCredentials:
    api_key: str
    base_url: str
    end_user: str

    def __repr__(self) -> str:
        return (
            "_RelayCredentials(api_key=<redacted>, "
            f"base_url={self.base_url!r}, end_user={self.end_user!r})"
        )


@dataclass(frozen=True)
class RelayEnvironment:
    """Non-secret metadata made available while the temporary env is active."""

    base_url: str
    end_user: str


@dataclass(frozen=True)
class PreflightProbe:
    name: str
    model: str
    modality: str
    cost_kind: str

    def __post_init__(self) -> None:
        if self.modality not in {"text", "vision"}:
            raise ValueError(f"Unsupported preflight modality: {self.modality!r}")


DEFAULT_PREFLIGHT_PROBES = (
    *(PreflightProbe(f"decision:{model.split('@', 1)[0]}", model, "vision", "preflight_decision")
      for model in DECISION_MODELS),
    *(PreflightProbe(f"synthesis:{model.split('@', 1)[0]}", model, "text", "preflight_synthesis")
      for model in DECISION_MODELS),
    PreflightProbe("visual_observer", OBSERVER_MODEL, "vision", "preflight_visual_observer"),
    PreflightProbe("agent_s_grounder", GROUNDER_MODEL, "vision", "preflight_agent_s_grounding"),
    PreflightProbe("visual_judge", JUDGE_MODEL, "vision", "preflight_visual_judge"),
)


def preflight_probes_for_manifest(manifest: Mapping[str, Any]) -> tuple[PreflightProbe, ...]:
    """Build the exact paid-route probe matrix registered by a C1 manifest.

    Older fixtures omit the model/visual sections; retain the default six
    probes for those inputs.  Real lightweight or formal manifests, however,
    must preflight their selected decision model rather than an unrelated
    hard-coded model trio.
    """

    raw_models = manifest.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        return DEFAULT_PREFLIGHT_PROBES
    models = [str(model).strip() for model in raw_models]
    if any(not model for model in models):
        raise ValueError("C1 preflight manifest models must be non-empty strings")
    bridge = manifest.get("visual_bridge")
    bridge = bridge if isinstance(bridge, Mapping) else {}
    observer = str(bridge.get("observer_model") or OBSERVER_MODEL)
    configured_grounder = bridge.get("grounder_model")
    judge_cfg = manifest.get("visual_judge")
    judge_cfg = judge_cfg if isinstance(judge_cfg, Mapping) else {}
    judge = str(
        manifest.get("visual_judge_model")
        or judge_cfg.get("model")
        or JUDGE_MODEL
    )
    arms = manifest.get("arms")
    arms = arms if isinstance(arms, Mapping) else {}
    diagnostic = arms.get("c1_state_minimal_agent")
    diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
    source_transition_enabled = (
        diagnostic.get("transition_action_source") == "source_agent_v2"
    )
    return (
        *(
            PreflightProbe(
                f"decision:{model.split('@', 1)[0]}",
                model,
                "vision",
                "preflight_decision",
            )
            for model in models
        ),
        *(
            (
                PreflightProbe(
                    f"source_transition:{model.split('@', 1)[0]}",
                    model,
                    "text",
                    "preflight_source_transition",
                )
                for model in models
            )
            if source_transition_enabled
            else ()
        ),
        *(
            PreflightProbe(
                f"synthesis:{model.split('@', 1)[0]}",
                model,
                "text",
                "preflight_synthesis",
            )
            for model in models
        ),
        PreflightProbe("visual_observer", observer, "vision", "preflight_visual_observer"),
        *grounder_preflight_probes(
            models, observer=observer, configured=configured_grounder
        ),
        PreflightProbe("visual_judge", judge, "vision", "preflight_visual_judge"),
    )


def grounder_preflight_probes(
    models: Sequence[str],
    *,
    observer: str,
    configured: Any,
) -> tuple[PreflightProbe, ...]:
    """Build the grounder route probes registered by a C1 manifest.

    When grounding follows the decision model, each decision model contributes
    its own ``agent_s_grounder:<model>`` route so the paid grounding route for
    every policy model is preflighted.  Otherwise a single fixed grounder route
    is emitted, preserving the historical behavior.
    """

    if grounder_follows_decision_model(configured):
        return tuple(
            PreflightProbe(
                f"agent_s_grounder:{model.split('@', 1)[0]}",
                model,
                "vision",
                "preflight_agent_s_grounding",
            )
            for model in models
        )
    grounder = str(configured or observer)
    return (
        PreflightProbe(
            "agent_s_grounder", grounder, "vision", "preflight_agent_s_grounding"
        ),
    )


def _literal_credentials(path: Path) -> _RelayCredentials:
    """Read allowlisted top-level string literals without executing code.

    ``SECRET`` and ``BASE`` are required. ``END_USER`` is optional; when absent
    the C1 environment sets ``RELAY_END_USER`` to the empty string so the
    Relay provider cannot fall back to its historical default header.
    """

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise C1CredentialError(f"Cannot read C1 credential source: {path}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise C1CredentialError(f"Invalid Python syntax in C1 credential source: {path}") from exc

    values: dict[str, str] = {}
    for node in tree.body:
        name: str | None = None
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                name = target.id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value_node = node.value
        if name not in _CREDENTIAL_NAMES:
            continue
        if name in values:
            raise C1CredentialError(f"Duplicate {name} assignment in C1 credential source")
        try:
            value = ast.literal_eval(value_node) if value_node is not None else None
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
            raise C1CredentialError(f"{name} must be a string literal") from exc
        if not isinstance(value, str) or not value.strip():
            raise C1CredentialError(f"{name} must be a non-empty string literal")
        values[name] = value.strip()

    missing = sorted(_REQUIRED_CREDENTIAL_NAMES - values.keys())
    if missing:
        raise C1CredentialError(
            "Missing required literal assignment(s): " + ", ".join(missing)
        )
    return _RelayCredentials(
        api_key=values["SECRET"],
        base_url=_validated_base_url(values["BASE"]),
        end_user=values.get("END_USER", ""),
    )


@contextlib.contextmanager
def relay_environment(
    credential_source: str | Path = DEFAULT_CREDENTIAL_SOURCE,
) -> Iterator[RelayEnvironment]:
    """Temporarily map local literals to the provider environment variables.

    The yielded object intentionally contains no API key.  All three variables
    are restored exactly, including deletion when they were initially absent.
    """

    credentials = _literal_credentials(Path(credential_source))
    previous = {name: os.environ.get(name) for name in _ENV_NAMES}
    os.environ.update(
        {
            "RELAY_API_KEY": credentials.api_key,
            "RELAY_BASE_URL": credentials.base_url,
            "RELAY_END_USER": credentials.end_user,
        }
    )
    try:
        yield RelayEnvironment(
            base_url=credentials.base_url,
            end_user=credentials.end_user,
        )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _probe_image_data_url() -> str:
    """Generate a small deterministic calibration image without writing a file."""

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (128, 96), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 24, 95, 71), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{payload}"


def _messages_for(probe: PreflightProbe, image_url: str) -> list[dict[str, Any]]:
    if probe.modality == "text":
        if probe.name.startswith("source_transition:"):
            return [
                {
                    "role": "user",
                    "content": (
                        "C1 source-transition contract preflight. Return exactly "
                        "this short YAML mapping with non-empty actions, assertions, "
                        "and source_evidence keys: task_id: preflight; benchmark: "
                        "preflight; actions: [{type: wait, ms: 1}]; assertions: "
                        "[{type: no_errors}]; source_evidence: [{source_path: "
                        "preflight.txt, matched_symbol: preflight, rationale: "
                        "connectivity only}]"
                    ),
                }
            ]
        return [
            {
                "role": "user",
                "content": (
                    "C1 connectivity preflight only. Reply with a short non-empty acknowledgement; "
                    "do not solve another task."
                ),
            }
        ]
    prompts = {
        "decision": (
            "C1 Agent-S vision connectivity preflight only. Briefly acknowledge "
            "the visible black rectangle; do not solve another task."
        ),
        "visual_observer": "Briefly describe the visible black rectangle.",
        "agent_s_grounder": "Return the black rectangle bounding box in image pixels.",
        "visual_judge": "State whether the image visibly contains a black rectangle.",
    }
    if probe.name.startswith("decision:"):
        prompt_text = prompts["decision"]
    elif probe.name.startswith("agent_s_grounder"):
        prompt_text = prompts["agent_s_grounder"]
    else:
        prompt_text = prompts.get(probe.name, "Describe this image briefly.")
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt_text,
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]


def _content_is_nonempty(response: Any) -> bool:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return False
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, Sequence):
        for part in content:
            text = part.get("text") if isinstance(part, Mapping) else getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return True
    return False


def _finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    value = getattr(choices[0], "finish_reason", None)
    return str(value) if value is not None else None


def _sum_usage(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = {
        name: sum(int(item.get(name, 0) or 0) for item in items)
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "cached_prompt_tokens",
            "total_tokens",
        )
    }
    observed = sum(int(item.get("reasoning_tokens", 0) or 0) for item in items)
    missing = sum(not bool(item.get("reasoning_detail_complete", True)) for item in items)
    unknown_completion = sum(
        int(item.get("completion_tokens", 0) or 0)
        for item in items
        if not bool(item.get("reasoning_detail_complete", True))
    )
    expected_completion = sum(
        int(item.get("completion_tokens", 0) or 0)
        for item in items
        if bool(item.get("reasoning_expected", False))
    )
    fraction = (
        float(unknown_completion) / float(expected_completion)
        if expected_completion > 0
        else 0.0
    )
    result.update(
        reasoning_tokens=None if missing else observed,
        reasoning_tokens_observed=observed,
        reasoning_tokens_lower_bound=observed,
        reasoning_tokens_upper_bound=observed + unknown_completion,
        reasoning_detail_complete=missing == 0,
        reasoning_detail_missing_calls=missing,
        reasoning_detail_unknown_completion_tokens=unknown_completion,
        reasoning_detail_expected_completion_tokens=expected_completion,
        reasoning_detail_unknown_fraction=fraction,
        reasoning_detail_tolerance_fraction=(
            cost.REASONING_DETAIL_TOLERANCE_FRACTION
        ),
        reasoning_detail_within_tolerance=(
            fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION
        ),
    )
    return result


def _usage_with_reasoning_detail(
    usage: Mapping[str, Any], assessment: cost.UsageAssessment
) -> dict[str, Any]:
    """Attach the complete v2 secondary-detail envelope to one attempt."""

    result = dict(usage)
    observed = int(assessment.reasoning_tokens or 0)
    missing = int(not assessment.reasoning_detail_complete)
    completion = int(assessment.completion_tokens or 0)
    unknown_completion = completion if missing else 0
    expected_completion = completion if assessment.reasoning_expected else 0
    fraction = (
        float(unknown_completion) / float(expected_completion)
        if expected_completion > 0
        else 0.0
    )
    result.update(
        reasoning_expected=assessment.reasoning_expected,
        reasoning_detail_complete=assessment.reasoning_detail_complete,
        reasoning_detail_status=assessment.reasoning_detail_status,
        reasoning_tokens_source=assessment.reasoning_tokens_source,
        reasoning_tokens_observed=observed,
        reasoning_tokens_lower_bound=observed,
        reasoning_tokens_upper_bound=observed + unknown_completion,
        reasoning_detail_missing_calls=missing,
        reasoning_detail_unknown_completion_tokens=unknown_completion,
        reasoning_detail_expected_completion_tokens=expected_completion,
        reasoning_detail_unknown_fraction=fraction,
        reasoning_detail_tolerance_fraction=(
            cost.REASONING_DETAIL_TOLERANCE_FRACTION
        ),
        reasoning_detail_within_tolerance=(
            fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION
        ),
    )
    return result


def _client_for(
    provider: ProviderConfig,
    *,
    client_factory: Callable[..., Any],
    timeout_s: float,
) -> Any:
    api_key = os.environ.get(provider.api_key_env, "")
    if not api_key:
        raise C1CredentialError(f"{provider.api_key_env} is not set")
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": provider.base_url,
        "timeout": float(timeout_s),
        "max_retries": 0,
    }
    if provider.extra_headers:
        kwargs["default_headers"] = dict(provider.extra_headers)
    return client_factory(**kwargs)


def run_c1_preflight(
    credential_source: str | Path = DEFAULT_CREDENTIAL_SOURCE,
    *,
    probes: Sequence[PreflightProbe] | None = None,
    decision_models: Sequence[str] | None = None,
    initial_max_tokens: int = 8192,
    retry_max_tokens: int = 16384,
    timeout_s: float | None = None,
    client_factory: Callable[..., Any] | None = None,
    root: str | Path | None = None,
    manifest_path: str | Path = "benchmarks/p5/manifest.yaml",
) -> dict[str, Any]:
    """Explicitly check all C1 model routes and return a content-free report.

    A call is retried once at ``retry_max_tokens`` only when its public content
    is empty and ``finish_reason`` explicitly indicates output-limit exhaustion.
    Both attempts remain in token accounting.  Model output and
    ``reasoning_content`` are never copied into the report.
    """

    if initial_max_tokens <= 0 or retry_max_tokens < initial_max_tokens:
        raise ValueError("retry_max_tokens must be >= initial_max_tokens > 0")
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI

    started = time.perf_counter()
    formal_root = Path(root).expanduser().resolve() if root is not None else None
    manifest: dict[str, Any] | None = None
    configured_profiles: dict[str, str] | None = None
    configured_temperature: float | None = None
    frozen_harness_hash: str | None = None
    application_revisions: dict[str, dict[str, str]] | None = None
    environment_evidence: dict[str, Any] | None = None
    effective_timeout_s = (
        C1_PROVIDER_TIMEOUT_S if timeout_s is None else float(timeout_s)
    )
    if formal_root is not None:
        manifest_file = Path(manifest_path).expanduser()
        if not manifest_file.is_absolute():
            manifest_file = formal_root / manifest_file
        loaded = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("C1 preflight manifest must be a mapping")
        manifest = loaded
        registered_timeout_s = manifest_provider_timeout_s(manifest)
        if timeout_s is not None and float(timeout_s) != registered_timeout_s:
            raise ValueError(
                "C1 preflight timeout must equal manifest api.provider_timeout_s"
            )
        effective_timeout_s = registered_timeout_s
        raw_profiles = manifest.get("model_request_profiles")
        if not isinstance(raw_profiles, dict):
            raise ValueError("Formal C1 manifest must define model_request_profiles")
        configured_profiles = {
            str(model): str(profile) for model, profile in raw_profiles.items()
        }
        api_cfg = manifest.get("api")
        raw_temperature = api_cfg.get("temperature") if isinstance(api_cfg, dict) else None
        if raw_temperature is not None:
            if isinstance(raw_temperature, bool) or not isinstance(
                raw_temperature, (int, float)
            ):
                raise ValueError("Formal C1 api.temperature must be numeric or null")
            configured_temperature = float(raw_temperature)
            if not math.isfinite(configured_temperature):
                raise ValueError("Formal C1 api.temperature must be finite")
        frozen_harness_hash = c1_harness_hash(formal_root)
        application_revisions = c1_application_revisions(formal_root)
        try:
            environment_evidence = validate_c1_environment(
                formal_root,
                manifest=manifest,
            )
        except C1EnvironmentMismatch as error:
            return {
                "schema_version": PREFLIGHT_SCHEMA_VERSION,
                "complete": False,
                "probe_count": 0,
                "initial_max_tokens": int(initial_max_tokens),
                "retry_max_tokens": int(retry_max_tokens),
                "retry_rule": (
                    "one_retry_only_if_empty_content_and_explicit_output_limit"
                ),
                "provider_timeout_s": effective_timeout_s,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "git_commit": run_context.git_commit(formal_root),
                "manifest_hash": run_context.manifest_hash(manifest),
                "harness_hash": frozen_harness_hash,
                "c1_application_revisions": application_revisions,
                "c1_environment": error.evidence,
                "failure_stage": "environment_preflight",
                "results": [],
            }
    if probes is not None and decision_models is not None:
        raise ValueError("probes and decision_models are mutually exclusive")
    probe_manifest = manifest
    if decision_models is not None:
        if not decision_models or any(not str(model).strip() for model in decision_models):
            raise ValueError("decision_models must contain non-empty model ids")
        probe_manifest = dict(manifest or {})
        probe_manifest["models"] = [str(model).strip() for model in decision_models]
    selected_probes = tuple(
        probes
        if probes is not None
        else preflight_probes_for_manifest(probe_manifest)
        if probe_manifest is not None
        else DEFAULT_PREFLIGHT_PROBES
    )
    results: list[dict[str, Any]] = []
    image_url = _probe_image_data_url()
    profiles_context = (
        model_request_profiles_environment(configured_profiles)
        if formal_root is not None
        else contextlib.nullcontext()
    )
    with profiles_context, relay_environment(credential_source) as environment:
        clients: dict[tuple[str, str, tuple[tuple[str, str], ...]], Any] = {}
        for probe in selected_probes:
            probe_started = time.perf_counter()
            attempts = 0
            attempt_usage: list[dict[str, int]] = []
            attempt_records: list[dict[str, Any]] = []
            finish_reason: str | None = None
            clean_model, provider = resolve_model(
                probe.model,
                default_base_url=os.environ["RELAY_BASE_URL"],
                default_api_key_env="RELAY_API_KEY",
            )
            request_profile = resolve_model_request_profile(clean_model)
            cache_key = (
                provider.base_url,
                provider.api_key_env,
                tuple(sorted(provider.extra_headers.items())),
            )
            try:
                if cache_key not in clients:
                    clients[cache_key] = _client_for(
                        provider,
                        client_factory=client_factory,
                        timeout_s=effective_timeout_s,
                    )
                client = clients[cache_key]
                messages = _messages_for(probe, image_url)
                content_nonempty = False
                request_contract_passed = False
                reasoning_content_observed_any = False
                probe_temperature = (
                    configured_temperature
                    if probe.cost_kind == "preflight_decision"
                    else None
                )
                omit_temperature_after_rejection = False
                temperature_fallback_triggered = False
                for max_tokens in (initial_max_tokens, retry_max_tokens):
                    while True:
                        attempts += 1
                        requested_at = datetime.now(timezone.utc).isoformat()
                        call_started = time.perf_counter()
                        request = {
                            "model": clean_model,
                            "messages": messages,
                            "max_tokens": int(max_tokens),
                        }
                        request.update(request_profile.request_kwargs())
                        if (
                            probe_temperature is not None
                            and not omit_temperature_after_rejection
                        ):
                            request["temperature"] = probe_temperature
                        requested_temperature = request.get("temperature")
                        try:
                            response = client.chat.completions.create(**request)
                        except Exception as request_error:
                            usage = cost.record_error_usage_call(
                                clean_model,
                                error=request_error,
                                latency_ms=int(
                                    (time.perf_counter() - call_started) * 1000
                                ),
                                kind=probe.cost_kind,
                                requested_at=requested_at,
                            )
                            if usage is not None:
                                attempt_usage.append(usage)
                            attempt_record: dict[str, Any] = {
                                "ordinal": attempts,
                                "max_tokens": int(max_tokens),
                                "request_succeeded": False,
                                "error_type": type(request_error).__name__,
                                "status_code": getattr(
                                    request_error, "status_code", None
                                ),
                                "temperature_configured": probe_temperature,
                                "temperature_requested": requested_temperature,
                                "temperature_fallback_triggered": False,
                            }
                            if usage is not None:
                                attempt_record["usage"] = usage
                            if (
                                "temperature" in request
                                and is_temperature_rejection(request_error)
                            ):
                                omit_temperature_after_rejection = True
                                temperature_fallback_triggered = True
                                attempt_record[
                                    "temperature_fallback_triggered"
                                ] = True
                                attempt_records.append(attempt_record)
                                continue
                            attempt_records.append(attempt_record)
                            raise
                        break
                    elapsed_ms = int((time.perf_counter() - call_started) * 1000)
                    content_nonempty = _content_is_nonempty(response)
                    finish_reason = _finish_reason(response)
                    reasoning_content_observed = cost.response_has_reasoning_content(
                        response
                    )
                    reasoning_content_observed_any = bool(
                        reasoning_content_observed_any or reasoning_content_observed
                    )
                    reasoning_expected = (
                        request_profile.reasoning_expected
                        or reasoning_content_observed
                    )
                    usage_assessment = cost.assess_usage(
                        getattr(response, "usage", None),
                        content="present" if content_nonempty else "",
                        finish_reason=finish_reason,
                        reasoning_observed=reasoning_content_observed,
                        reasoning_expected=reasoning_expected,
                        reasoning_disabled=(
                            request_profile.profile == "thinking_disabled"
                        ),
                    )
                    usage = cost.record_usage_call(
                        clean_model,
                        usage=getattr(response, "usage", None),
                        # Only presence is needed for usage validation. Never copy
                        # model output or reasoning text into the preflight report.
                        content="present" if content_nonempty else "",
                        finish_reason=finish_reason,
                        reasoning_observed=reasoning_content_observed,
                        reasoning_expected=reasoning_expected,
                        reasoning_disabled=(
                            request_profile.profile == "thinking_disabled"
                        ),
                        latency_ms=elapsed_ms,
                        kind=probe.cost_kind,
                        requested_at=requested_at,
                        endpoint="chat.completions",
                        provider_reported_model=getattr(response, "model", None),
                        provider_response_id=getattr(response, "id", None),
                        system_fingerprint=getattr(
                            response, "system_fingerprint", None
                        ),
                    )
                    usage = _usage_with_reasoning_detail(
                        usage, usage_assessment
                    )
                    attempt_usage.append(usage)
                    reasoning_token_anomaly = bool(
                        request_profile.profile == "thinking_disabled"
                        and int(usage.get("reasoning_tokens_observed", 0) or 0) > 0
                    )
                    request_contract_passed = bool(
                        content_nonempty
                        and not (
                            request_profile.profile == "thinking_disabled"
                            and reasoning_content_observed
                        )
                    )
                    reasoning_exhausted = (
                        not content_nonempty
                        and str(finish_reason or "").lower()
                        in {"length", "max_tokens"}
                    )
                    attempt_records.append(
                        {
                            "ordinal": attempts,
                            "max_tokens": int(max_tokens),
                            "request_succeeded": True,
                            "content_nonempty": content_nonempty,
                            "finish_reason": finish_reason,
                            "reasoning_observed": bool(
                                reasoning_content_observed
                                or (usage.get("reasoning_tokens") or 0) > 0
                            ),
                            "reasoning_content_observed": bool(
                                reasoning_content_observed
                            ),
                            "reasoning_token_anomaly": reasoning_token_anomaly,
                            "request_contract_passed": request_contract_passed,
                            "reasoning_exhausted": reasoning_exhausted,
                            "temperature_configured": probe_temperature,
                            "temperature_requested": requested_temperature,
                            "temperature_fallback_triggered": (
                                temperature_fallback_triggered
                            ),
                            "temperature_omitted_after_rejection": (
                                omit_temperature_after_rejection
                            ),
                            "usage": usage,
                        }
                    )
                    if content_nonempty or not reasoning_exhausted or max_tokens == retry_max_tokens:
                        break
                aggregate_usage = _sum_usage(attempt_usage)
                request_contract_passed = bool(
                    content_nonempty
                    and not (
                        request_profile.profile == "thinking_disabled"
                        and reasoning_content_observed_any
                    )
                )
                reasoning_token_anomaly = bool(
                    request_profile.profile == "thinking_disabled"
                    and int(
                        aggregate_usage.get("reasoning_tokens_observed", 0) or 0
                    )
                    > 0
                )
                results.append(
                    {
                        "name": probe.name,
                        "model": clean_model,
                        "provider": provider.name,
                        "modality": probe.modality,
                        "success": request_contract_passed,
                        "request_contract_passed": request_contract_passed,
                        "reasoning_content_observed": (
                            reasoning_content_observed_any
                        ),
                        "reasoning_token_anomaly": reasoning_token_anomaly,
                        "attempts": attempts,
                        "elapsed_ms": int((time.perf_counter() - probe_started) * 1000),
                        "finish_reason": finish_reason,
                        "request_profile": request_profile.provenance(),
                        "temperature_configured": probe_temperature,
                        "temperature_fallback_triggered": (
                            temperature_fallback_triggered
                        ),
                        "temperature_omitted_after_rejection": (
                            omit_temperature_after_rejection
                        ),
                        "attempt_records": attempt_records,
                        "usage": aggregate_usage,
                    }
                )
            except Exception as exc:  # continue so one unavailable route does not hide the others
                results.append(
                    {
                        "name": probe.name,
                        "model": clean_model,
                        "provider": provider.name,
                        "modality": probe.modality,
                        "success": False,
                        "attempts": attempts,
                        "elapsed_ms": int((time.perf_counter() - probe_started) * 1000),
                        "finish_reason": finish_reason,
                        "request_profile": request_profile.provenance(),
                        "attempt_records": attempt_records,
                        # Preserve all earlier completed attempts when a later
                        # retry fails before returning a provider response.
                        "usage": _sum_usage(attempt_usage),
                        # Never serialize exception text: gateways may echo request headers.
                        "error_type": type(exc).__name__,
                        "status_code": getattr(exc, "status_code", None),
                    }
                )
    if formal_root is not None:
        if c1_harness_hash(formal_root) != frozen_harness_hash:
            raise RuntimeError("C1 harness changed while preflight was running")
        current_environment = validate_c1_environment(
            formal_root,
            manifest=manifest,
        )
        if current_environment != environment_evidence:
            raise RuntimeError("C1 environment changed while preflight was running")
    report: dict[str, Any] = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION if formal_root is not None else 1,
        "complete": len(results) == len(selected_probes) and all(item["success"] for item in results),
        "probe_count": len(results),
        "initial_max_tokens": int(initial_max_tokens),
        "retry_max_tokens": int(retry_max_tokens),
        "retry_rule": "one_retry_only_if_empty_content_and_explicit_output_limit",
        "temperature_contract": {
            "configured": configured_temperature,
            "applies_to": "decision_probes_only",
            "fallback": "omit_only_after_explicit_http_400_temperature_rejection",
        },
        "provider_timeout_s": effective_timeout_s,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "results": results,
    }
    if formal_root is not None and manifest is not None and environment is not None:
        api_cfg = manifest.get("api") if isinstance(manifest.get("api"), dict) else {}
        canonical_profiles = canonical_profiles_json(configured_profiles)
        report.update(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "git_commit": run_context.git_commit(formal_root),
                "manifest_hash": run_context.manifest_hash(manifest),
                "harness_hash": frozen_harness_hash,
                "c1_application_revisions": application_revisions,
                "c1_environment": environment_evidence,
                "usage_accounting": manifest.get("usage_accounting"),
                "model_request_profiles": json.loads(canonical_profiles),
                "model_request_profiles_sha256": hashlib.sha256(
                    canonical_profiles.encode("utf-8")
                ).hexdigest(),
                "api_provider": {
                    "name": api_cfg.get("provider"),
                    "base_url": environment.base_url.rstrip("/"),
                    "end_user_sha256": (
                        hashlib.sha256(environment.end_user.encode("utf-8")).hexdigest()
                        if environment.end_user
                        else None
                    ),
                },
            }
        )
    return report


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIAL_SOURCE))
    parser.add_argument("--manifest", default="benchmarks/p5/manifest.yaml")
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Preflight an execution-time model override instead of the manifest defaults.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Must match manifest api.provider_timeout_s; omitted uses the manifest.",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    credentials = Path(args.credentials).expanduser()
    if not credentials.is_absolute():
        credentials = root / credentials
    report = run_c1_preflight(
        credentials,
        timeout_s=args.timeout,
        root=root,
        manifest_path=args.manifest,
        decision_models=args.models,
    )
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    _atomic_json(output, report)
    print(
        json.dumps(
            {
                "artifact": str(output.resolve()),
                "complete": report["complete"],
                "probe_count": report["probe_count"],
            }
        )
    )
    return 0 if report["complete"] else 1


__all__ = [
    "RelayEnvironment",
    "C1CredentialError",
    "DECISION_MODELS",
    "DEFAULT_CREDENTIAL_SOURCE",
    "DEFAULT_PREFLIGHT_PROBES",
    "GROUNDER_MODEL",
    "JUDGE_MODEL",
    "OBSERVER_MODEL",
    "PREFLIGHT_SCHEMA_VERSION",
    "PreflightProbe",
    "relay_environment",
    "run_c1_preflight",
]


if __name__ == "__main__":
    raise SystemExit(main())
