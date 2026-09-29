"""Minimal post-Start semantic-action planner for the diagnostic C1 E-prime arm.

The planner is intentionally narrower than checkpoint synthesis.  It receives
only the public task text, the already-accepted Start target/checkpoint, and the
public benchmark action contract.  It never receives the checkpoint's original
actions/assertions, source context, screenshots, or grader data.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import cost, run_context
from .c1_spec_acceptance import generated_spec_acceptance_errors
from .c1_synthesis_cache import atomic_write_json, canonical_hash
from .model_request import model_request_profile_provenance
from .runtime_lock import runtime_lease
from .synthesis import (
    OpenAICompatibleChatClient,
    SynthesisError,
    public_action_contract_for_task,
)


MINIMAL_SMOKE_AGENT_SCHEMA = "minimal-smoke-agent-v1"
MINIMAL_SMOKE_AGENT_SCHEMA_VERSION = 1
MINIMAL_SMOKE_AGENT_MAX_ACTIONS = 8
MINIMAL_SMOKE_AGENT_MAX_TOKENS = 2048
MINIMAL_SMOKE_AGENT_FORMAT_REPAIRS = 1
MINIMAL_SMOKE_AGENT_SYSTEM_PROMPT = (
    "Generate the shortest executable WarpTest semantic action sequence that "
    "completes the public reference_steps from the accepted Start. Return strict "
    "JSON only with exactly one top-level key named actions."
)


class MinimalSmokeAgentError(RuntimeError):
    """Invalid configuration or an unusable planner request."""


@dataclass(frozen=True)
class MinimalSmokePlan:
    status: str
    actions: tuple[dict[str, Any], ...]
    model: str
    provider: str
    request_profile: Mapping[str, Any]
    input_sha256: str
    prompt_sha256: str
    response_sha256: str | None
    actions_sha256: str | None
    repair_used: bool
    cache_hit: bool
    generation_ms: int
    cost: Mapping[str, Any]
    attributed_cost: Mapping[str, Any]
    prompt_path: str | None = None
    response_path: str | None = None
    repair_prompt_path: str | None = None
    repair_response_path: str | None = None
    errors: tuple[str, ...] = ()
    infrastructure_error: bool = False

    @property
    def usable(self) -> bool:
        return self.status in {"generated", "repaired"} and bool(self.actions)

    def to_report(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["actions"] = [copy.deepcopy(action) for action in self.actions]
        payload["action_count"] = len(self.actions)
        payload["cost"] = _compact_cost(payload.get("cost"))
        payload["attributed_cost"] = _compact_cost(
            payload.get("attributed_cost")
        )
        return payload


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _empty_cost() -> dict[str, Any]:
    return cost.CostMeter().summary()


def _compact_cost(value: Any) -> dict[str, Any]:
    payload = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else _empty_cost()
    payload.pop("calls", None)
    return payload


def _cost_since(marker: tuple[cost.CostMeter, int] | None) -> dict[str, Any]:
    if marker is None:
        return _empty_cost()
    meter, start = marker
    return cost.CostMeter(records=list(meter.records[start:])).summary()


def build_minimal_smoke_request(
    task: Any,
    accepted_spec: Mapping[str, Any],
    *,
    max_actions: int = MINIMAL_SMOKE_AGENT_MAX_ACTIONS,
) -> dict[str, Any]:
    """Build the complete model-visible request without private or stale fields."""

    if isinstance(max_actions, bool) or not 1 <= int(max_actions) <= 8:
        raise MinimalSmokeAgentError("minimal smoke max_actions must be in [1, 8]")
    target = accepted_spec.get("target")
    checkpoint = accepted_spec.get("checkpoint")
    if not isinstance(target, Mapping) or not isinstance(checkpoint, Mapping):
        raise MinimalSmokeAgentError("accepted Start target/checkpoint is missing")
    actions = public_action_contract_for_task(task.issue)
    return {
        "schema_version": MINIMAL_SMOKE_AGENT_SCHEMA_VERSION,
        "task_id": str(task.id),
        "benchmark": str(task.benchmark),
        "preconditions": str(task.preconditions),
        "reference_steps": str(task.reference_steps),
        "start": {
            "target": copy.deepcopy(dict(target)),
            "checkpoint": copy.deepcopy(dict(checkpoint)),
        },
        "allowed_action_schema": actions,
        "max_actions": int(max_actions),
        "output_schema": {
            "actions": [{"type": "registered semantic action type"}]
        },
    }


def build_minimal_smoke_prompt(request: Mapping[str, Any]) -> str:
    return json.dumps(request, ensure_ascii=False, indent=2, sort_keys=False)


def build_minimal_smoke_repair_prompt(
    request: Mapping[str, Any], raw_response: str, errors: list[str]
) -> str:
    return json.dumps(
        {
            "task": (
                "Repair the candidate into strict JSON with exactly one top-level "
                "actions key. Preserve action order and values when present; only "
                "repair representation, missing required fields, and listed schema "
                "errors. Use only the same public request."
            ),
            "request": request,
            "candidate": raw_response,
            "errors": errors,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )


def parse_and_validate_minimal_actions(
    raw: str,
    *,
    task: Any,
    accepted_spec: Mapping[str, Any],
    max_actions: int,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    errors: list[str] = []
    try:
        parsed = json.loads(raw)
    except Exception as error:  # strict JSON is part of the measured contract
        return None, [f"response is not strict JSON: {type(error).__name__}: {error}"]
    if not isinstance(parsed, dict):
        return None, ["response must be a JSON object"]
    if set(parsed) != {"actions"}:
        errors.append("response top-level keys must be exactly ['actions']")
    actions = parsed.get("actions")
    if not isinstance(actions, list):
        errors.append("response.actions must be a list")
        return None, errors
    if not 1 <= len(actions) <= int(max_actions):
        errors.append(f"response.actions length must be in [1, {int(max_actions)}]")
    if not all(isinstance(action, dict) and action for action in actions):
        errors.append("every response.actions entry must be a non-empty object")
    if errors:
        return None, errors
    candidate = copy.deepcopy(dict(accepted_spec))
    candidate["actions"] = copy.deepcopy(actions)
    try:
        json.dumps(candidate, sort_keys=True, ensure_ascii=True)
    except Exception as error:
        errors.append(f"planned transition is not JSON serializable: {error}")
    errors.extend(
        generated_spec_acceptance_errors(
            candidate, issue=task.issue, benchmark=task.benchmark
        )
    )
    return (copy.deepcopy(actions), []) if not errors else (None, errors)


class MinimalSmokeActionAgent:
    """One-shot action planner with at most one public-only format repair."""

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        base_url: str,
        api_key_env: str,
        temperature: float,
        max_tokens: int = MINIMAL_SMOKE_AGENT_MAX_TOKENS,
        max_actions: int = MINIMAL_SMOKE_AGENT_MAX_ACTIONS,
        format_repairs: int = MINIMAL_SMOKE_AGENT_FORMAT_REPAIRS,
        cache_root: str | Path | None = None,
        extra_headers: Mapping[str, str] | None = None,
        planning_client: Any | None = None,
        repair_client: Any | None = None,
        client_factory: Callable[..., Any] = OpenAICompatibleChatClient,
    ) -> None:
        if not model:
            raise MinimalSmokeAgentError("minimal smoke planner model is required")
        if isinstance(max_actions, bool) or not 1 <= int(max_actions) <= 8:
            raise MinimalSmokeAgentError("max_actions must be in [1, 8]")
        if int(format_repairs) not in {0, 1}:
            raise MinimalSmokeAgentError("format_repairs must be 0 or 1")
        self.model = str(model)
        self.provider = str(provider)
        self.base_url = str(base_url)
        self.api_key_env = str(api_key_env)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_actions = int(max_actions)
        self.format_repairs = int(format_repairs)
        self.cache_root = (
            Path(cache_root).expanduser().resolve() if cache_root is not None else None
        )
        kwargs = {
            "api_key": os.environ.get(self.api_key_env, ""),
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_headers": dict(extra_headers or {}),
            "minimum_max_tokens": 1,
            "reasoning_retry_max_tokens": None,
        }
        self.planning_client = planning_client or client_factory(
            **kwargs, cost_kind="smoke_action_planning"
        )
        self.repair_client = repair_client or client_factory(
            **kwargs, cost_kind="smoke_action_format_repair"
        )

    def close(self) -> None:
        seen: set[int] = set()
        for client in (self.planning_client, self.repair_client):
            if id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def plan(
        self,
        task: Any,
        accepted_spec: Mapping[str, Any],
        *,
        seed: int,
        artifact_dir: str | Path,
    ) -> MinimalSmokePlan:
        request = build_minimal_smoke_request(
            task, accepted_spec, max_actions=self.max_actions
        )
        prompt = build_minimal_smoke_prompt(request)
        request_profile = model_request_profile_provenance(self.model)
        cache_key = {
            "schema": MINIMAL_SMOKE_AGENT_SCHEMA,
            "request": request,
            "seed": int(seed),
            "prompt_revision": str(getattr(task, "prompt_revision", "") or ""),
            "model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "request_profile": request_profile,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_actions": self.max_actions,
            "format_repairs": self.format_repairs,
        }
        fingerprint = canonical_hash(cache_key, length=64)
        if self.cache_root is None:
            return self._produce(
                task,
                accepted_spec,
                request=request,
                prompt=prompt,
                request_profile=request_profile,
                artifact_dir=Path(artifact_dir),
                cache_hit=False,
            )
        cache_path = self.cache_root / fingerprint[:2] / f"{fingerprint}.json"
        with runtime_lease(cache_path, lock_dir=self.cache_root / "locks"):
            cached = self._read_cache(cache_path, cache_key)
            if cached is not None:
                return self._materialize_cached(
                    cached,
                    artifact_dir=Path(artifact_dir),
                    cache_hit=True,
                )
            plan = self._produce(
                task,
                accepted_spec,
                request=request,
                prompt=prompt,
                request_profile=request_profile,
                artifact_dir=Path(artifact_dir),
                cache_hit=False,
            )
            if plan.status in {"generated", "repaired", "invalid"} and bool(
                plan.cost.get("usage_complete")
            ):
                atomic_write_json(
                    cache_path,
                    {
                        "schema_version": MINIMAL_SMOKE_AGENT_SCHEMA_VERSION,
                        "cache_key": cache_key,
                        "plan": plan.to_report(),
                        "artifacts": self._artifact_texts(plan),
                    },
                )
            return plan

    def _call(self, client: Any, messages: list[dict[str, Any]]) -> str:
        result = client.converse(self.model, messages)
        content = getattr(result, "content", result)
        if not isinstance(content, str):
            raise MinimalSmokeAgentError("planner response content is not text")
        return content

    def _produce(
        self,
        task: Any,
        accepted_spec: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        prompt: str,
        request_profile: Mapping[str, Any],
        artifact_dir: Path,
        cache_hit: bool,
    ) -> MinimalSmokePlan:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = artifact_dir / "prompt.txt"
        response_path = artifact_dir / "response.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        marker = cost.mark_records()
        started = time.perf_counter()
        raw = ""
        errors: list[str] = []
        infrastructure = False
        try:
            raw = self._call(
                self.planning_client,
                [
                    {"role": "system", "content": MINIMAL_SMOKE_AGENT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            response_path.write_text(raw, encoding="utf-8")
            actions, errors = parse_and_validate_minimal_actions(
                raw,
                task=task,
                accepted_spec=accepted_spec,
                max_actions=self.max_actions,
            )
        except Exception as error:  # provider/model outcomes are classified below
            response_path.write_text(raw, encoding="utf-8")
            errors = [f"planning call failed: {type(error).__name__}: {error}"]
            infrastructure = _planner_infrastructure_error(error)
            actions = None
        repair_used = False
        repair_prompt_path: Path | None = None
        repair_response_path: Path | None = None
        if actions is None and not infrastructure and self.format_repairs:
            repair_used = True
            repair_prompt = build_minimal_smoke_repair_prompt(request, raw, errors)
            repair_prompt_path = artifact_dir / "repair-prompt.txt"
            repair_response_path = artifact_dir / "repair-response.txt"
            repair_prompt_path.write_text(repair_prompt, encoding="utf-8")
            try:
                raw = self._call(
                    self.repair_client,
                    [
                        {
                            "role": "system",
                            "content": (
                                "Repair a WarpTest semantic action plan. Return strict "
                                "JSON only with exactly one actions key."
                            ),
                        },
                        {"role": "user", "content": repair_prompt},
                    ],
                )
                repair_response_path.write_text(raw, encoding="utf-8")
                actions, errors = parse_and_validate_minimal_actions(
                    raw,
                    task=task,
                    accepted_spec=accepted_spec,
                    max_actions=self.max_actions,
                )
            except Exception as error:
                repair_response_path.write_text(raw, encoding="utf-8")
                errors = [f"repair call failed: {type(error).__name__}: {error}"]
                infrastructure = _planner_infrastructure_error(error)
                actions = None
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        plan_cost = _cost_since(marker)
        status = (
            "incomplete"
            if infrastructure or plan_cost.get("usage_complete") is not True
            else "repaired"
            if actions is not None and repair_used
            else "generated"
            if actions is not None
            else "invalid"
        )
        action_rows = tuple(copy.deepcopy(actions or []))
        response_sha = _text_sha256(raw) if raw else None
        return MinimalSmokePlan(
            status=status,
            actions=action_rows,
            model=self.model,
            provider=self.provider,
            request_profile=dict(request_profile),
            input_sha256=_json_sha256(request),
            prompt_sha256=_text_sha256(prompt),
            response_sha256=response_sha,
            actions_sha256=_json_sha256(list(action_rows)) if action_rows else None,
            repair_used=repair_used,
            cache_hit=cache_hit,
            generation_ms=elapsed_ms,
            cost=plan_cost,
            attributed_cost=plan_cost,
            prompt_path=str(prompt_path),
            response_path=str(response_path),
            repair_prompt_path=(
                str(repair_prompt_path) if repair_prompt_path is not None else None
            ),
            repair_response_path=(
                str(repair_response_path) if repair_response_path is not None else None
            ),
            errors=tuple(str(error) for error in errors),
            infrastructure_error=(status == "incomplete"),
        )

    @staticmethod
    def _read_cache(path: Path, cache_key: Mapping[str, Any]) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != MINIMAL_SMOKE_AGENT_SCHEMA_VERSION
            or payload.get("cache_key") != cache_key
            or not isinstance(payload.get("plan"), dict)
        ):
            return None
        return payload

    def _materialize_cached(
        self, payload: Mapping[str, Any], *, artifact_dir: Path, cache_hit: bool
    ) -> MinimalSmokePlan:
        plan = dict(payload["plan"])
        artifacts = payload.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, Mapping) else {}
        artifact_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str | None] = {}
        for key, filename in (
            ("prompt", "prompt.txt"),
            ("response", "response.txt"),
            ("repair_prompt", "repair-prompt.txt"),
            ("repair_response", "repair-response.txt"),
        ):
            value = artifacts.get(key)
            if isinstance(value, str):
                path = artifact_dir / filename
                path.write_text(value, encoding="utf-8")
                paths[key] = str(path)
            else:
                paths[key] = None
        attributed = plan.get("attributed_cost")
        attributed = attributed if isinstance(attributed, Mapping) else plan.get("cost")
        return MinimalSmokePlan(
            status=str(plan.get("status") or "invalid"),
            actions=tuple(
                copy.deepcopy(action)
                for action in plan.get("actions", [])
                if isinstance(action, dict)
            ),
            model=self.model,
            provider=self.provider,
            request_profile=dict(plan.get("request_profile") or {}),
            input_sha256=str(plan.get("input_sha256") or ""),
            prompt_sha256=str(plan.get("prompt_sha256") or ""),
            response_sha256=plan.get("response_sha256"),
            actions_sha256=plan.get("actions_sha256"),
            repair_used=bool(plan.get("repair_used")),
            cache_hit=cache_hit,
            generation_ms=0,
            cost=_empty_cost(),
            attributed_cost=dict(attributed or _empty_cost()),
            prompt_path=paths["prompt"],
            response_path=paths["response"],
            repair_prompt_path=paths["repair_prompt"],
            repair_response_path=paths["repair_response"],
            errors=tuple(str(error) for error in plan.get("errors", [])),
            infrastructure_error=False,
        )

    @staticmethod
    def _artifact_texts(plan: MinimalSmokePlan) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in (
            ("prompt", plan.prompt_path),
            ("response", plan.response_path),
            ("repair_prompt", plan.repair_prompt_path),
            ("repair_response", plan.repair_response_path),
        ):
            if isinstance(value, str) and Path(value).is_file():
                result[key] = Path(value).read_text(encoding="utf-8")
        return result


def _planner_infrastructure_error(error: BaseException) -> bool:
    if isinstance(error, cost.UsageAccountingError):
        return True
    if isinstance(error, SynthesisError):
        artifacts = getattr(error, "artifacts", {})
        if isinstance(artifacts, Mapping) and artifacts.get(
            "usage_accounting_complete"
        ) is False:
            return True
    return run_context.error_category(error) in {"infra", "rate_limit"}


__all__ = [
    "MINIMAL_SMOKE_AGENT_FORMAT_REPAIRS",
    "MINIMAL_SMOKE_AGENT_MAX_ACTIONS",
    "MINIMAL_SMOKE_AGENT_MAX_TOKENS",
    "MINIMAL_SMOKE_AGENT_SCHEMA",
    "MinimalSmokeActionAgent",
    "MinimalSmokeAgentError",
    "MinimalSmokePlan",
    "build_minimal_smoke_prompt",
    "build_minimal_smoke_request",
    "build_minimal_smoke_repair_prompt",
    "parse_and_validate_minimal_actions",
]
