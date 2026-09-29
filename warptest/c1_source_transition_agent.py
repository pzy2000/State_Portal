"""Source-aware post-Start transition planner for C1 E-prime v2.

The agent receives one accepted public Start projection and a sandboxed source
repository.  It has the same read_file/grep/list_dir ReAct surface and budget as
checkpoint synthesis, but it never receives the jointly generated actions,
assertions, source evidence, hidden oracle, screenshots, or grader feedback.
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

import yaml

from . import cost, run_context
from .c1_spec_acceptance import generated_spec_acceptance_errors
from .c1_synthesis_cache import atomic_write_json, canonical_hash
from .model_request import model_request_profile_provenance
from .runtime_lock import runtime_lease
from .synthesis import (
    OpenAICompatibleChatClient,
    RepoSandbox,
    SynthesisError,
    parse_llm_checkpoint_output,
    parse_react_reply,
    public_action_contract_for_task,
    public_assertion_contract_for_task,
    resolve_task_repo,
)


SOURCE_TRANSITION_AGENT_SCHEMA = "source-transition-agent-v2"
SOURCE_TRANSITION_AGENT_SCHEMA_VERSION = 2
SOURCE_TRANSITION_AGENT_PROMPT_REVISION = "c1-source-transition-v2"
SOURCE_TRANSITION_AGENT_MAX_STEPS = 16
SOURCE_TRANSITION_AGENT_MAX_TOKENS = 8192
SOURCE_TRANSITION_AGENT_FORMAT_REPAIRS = 1
_OUTPUT_KEYS = {
    "task_id",
    "benchmark",
    "actions",
    "assertions",
    "source_evidence",
}


class SourceTransitionAgentError(RuntimeError):
    """Invalid configuration or unusable source-transition evidence."""


@dataclass(frozen=True)
class SourceTransitionPlan:
    status: str
    actions: tuple[dict[str, Any], ...]
    assertions: tuple[dict[str, Any], ...]
    source_evidence: tuple[dict[str, Any], ...]
    model: str
    provider: str
    request_profile: Mapping[str, Any]
    input_sha256: str
    prompt_sha256: str
    response_sha256: str | None
    actions_sha256: str | None
    assertions_sha256: str | None
    source_evidence_sha256: str | None
    transcript_sha256: str | None
    tool_call_count: int
    files_read: tuple[str, ...]
    repair_used: bool
    cache_hit: bool
    generation_ms: int
    cost: Mapping[str, Any]
    attributed_cost: Mapping[str, Any]
    prompt_path: str | None = None
    response_path: str | None = None
    transcript_path: str | None = None
    repair_prompt_path: str | None = None
    repair_response_path: str | None = None
    errors: tuple[str, ...] = ()
    infrastructure_error: bool = False

    @property
    def usable(self) -> bool:
        return (
            self.status in {"generated", "repaired"}
            and bool(self.actions)
            and bool(self.assertions)
            and bool(self.source_evidence)
        )

    def to_report(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["actions"] = [copy.deepcopy(row) for row in self.actions]
        payload["assertions"] = [
            copy.deepcopy(row) for row in self.assertions
        ]
        payload["source_evidence"] = [
            copy.deepcopy(row) for row in self.source_evidence
        ]
        payload["action_count"] = len(self.actions)
        payload["assertion_count"] = len(self.assertions)
        payload["assertions_auxiliary_only"] = True
        payload["assertions_ignored_for_authoritative_grading"] = True
        payload["cost"] = _compact_cost(payload.get("cost"))
        payload["attributed_cost"] = _compact_cost(
            payload.get("attributed_cost")
        )
        return payload


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _empty_cost() -> dict[str, Any]:
    return cost.CostMeter().summary()


def _compact_cost(value: Any) -> dict[str, Any]:
    payload = (
        copy.deepcopy(dict(value))
        if isinstance(value, Mapping)
        else _empty_cost()
    )
    payload.pop("calls", None)
    return payload


def _cost_since(marker: tuple[cost.CostMeter, int] | None) -> dict[str, Any]:
    if marker is None:
        return _empty_cost()
    meter, start = marker
    return cost.CostMeter(records=list(meter.records[start:])).summary()


def _accepted_start_projection(accepted_spec: Mapping[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for key in ("name", "benchmark", "setup", "target", "checkpoint"):
        value = accepted_spec.get(key)
        if value is not None:
            projection[key] = copy.deepcopy(value)
    if not isinstance(projection.get("target"), Mapping) or not isinstance(
        projection.get("checkpoint"), Mapping
    ):
        raise SourceTransitionAgentError(
            "accepted Start target/checkpoint is missing"
        )
    return projection


def build_source_transition_request(
    task: Any,
    accepted_spec: Mapping[str, Any],
    *,
    repo: Path,
    repo_map: str,
    max_steps: int = SOURCE_TRANSITION_AGENT_MAX_STEPS,
    max_tokens: int = SOURCE_TRANSITION_AGENT_MAX_TOKENS,
) -> dict[str, Any]:
    """Build the complete public model-visible request for E-prime v2."""

    if isinstance(max_steps, bool) or int(max_steps) < 2:
        raise SourceTransitionAgentError("source transition max_steps must be >= 2")
    if isinstance(max_tokens, bool) or int(max_tokens) < 1:
        raise SourceTransitionAgentError("source transition max_tokens must be positive")
    return {
        "schema_version": SOURCE_TRANSITION_AGENT_SCHEMA_VERSION,
        "protocol_revision": SOURCE_TRANSITION_AGENT_PROMPT_REVISION,
        "task_id": str(task.id),
        "benchmark": str(task.benchmark),
        "preconditions": str(task.preconditions),
        "reference_steps": str(task.reference_steps),
        "accepted_start": _accepted_start_projection(accepted_spec),
        "repository": {"root": str(repo), "map": repo_map},
        "allowed_action_schema": public_action_contract_for_task(task.issue),
        "allowed_assertion_schema": public_assertion_contract_for_task(
            task.issue
        ),
        "budget": {
            "max_source_steps": int(max_steps),
            "max_response_tokens_per_call": int(max_tokens),
            "format_repairs": 1,
            "action_count_upper_bound": None,
        },
        "output_schema": {
            "task_id": str(task.id),
            "benchmark": str(task.benchmark),
            "actions": [{"type": "registered public semantic action"}],
            "assertions": [
                {"type": "registered public auxiliary assertion"}
            ],
            "source_evidence": [
                {
                    "source_path": "path actually observed with a repository tool",
                    "matched_symbol": "public symbol or value",
                    "rationale": "how the source supports the action/assertion",
                }
            ],
        },
    }


def build_source_transition_system_prompt() -> str:
    return "\n".join(
        [
            "You are WarpTest's source-aware post-Start transition agent.",
            "The accepted Start is already frozen. Discover only the public Transition by reading the sandboxed repository.",
            "Private oracle assertions, goldens, screenshots, runtime feedback, grader results, and another arm's actions/assertions are unavailable.",
            "On each turn reply with exactly one ACTION or FINAL block and nothing else.",
            "ACTION: read_file followed by one-line JSON {\"path\":...,\"start\":...,\"end\":...}",
            "ACTION: grep followed by one-line JSON {\"pattern\":...,\"glob\":...}",
            "ACTION: list_dir followed by one-line JSON {\"path\":...}",
            "When ready, return FINAL followed by one fenced YAML mapping.",
            "The FINAL mapping must contain exactly task_id, benchmark, actions, assertions, source_evidence.",
            "actions and assertions must both be non-empty and follow the supplied public schemas.",
            "source_evidence must be non-empty and cite only files actually observed through repository tools.",
            "Assertions are an auxiliary planning target; they do not decide benchmark success.",
        ]
    )


def build_source_transition_prompt(request: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        copy.deepcopy(dict(request)),
        sort_keys=False,
        allow_unicode=False,
    )


def build_source_transition_repair_prompt(
    request: Mapping[str, Any], raw_response: str, errors: list[str]
) -> str:
    payload = {
        "task": (
            "Repair the candidate into one YAML mapping with exactly task_id, "
            "benchmark, actions, assertions, source_evidence. Preserve public "
            "semantics and values; repair only representation and listed contract "
            "errors. Do not add evidence for files absent from files_read."
        ),
        "request": request,
        "candidate": raw_response,
        "errors": list(errors),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)


def parse_and_validate_source_transition(
    raw: str,
    *,
    task: Any,
    accepted_spec: Mapping[str, Any],
    files_read: list[str] | tuple[str, ...],
) -> tuple[
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
    list[str],
]:
    errors: list[str] = []
    try:
        parsed = parse_llm_checkpoint_output(raw)
    except Exception as error:
        return None, None, None, [
            f"response is not valid YAML: {type(error).__name__}: {error}"
        ]
    if set(parsed) != _OUTPUT_KEYS:
        errors.append(
            "response top-level keys must be exactly "
            + repr(sorted(_OUTPUT_KEYS))
        )
    if parsed.get("task_id") != task.id:
        errors.append(f"task_id must equal {task.id!r}")
    if parsed.get("benchmark") != task.benchmark:
        errors.append(f"benchmark must equal {task.benchmark!r}")
    actions = parsed.get("actions")
    assertions = parsed.get("assertions")
    evidence = parsed.get("source_evidence")
    if not isinstance(actions, list) or not actions:
        errors.append("actions must be a non-empty list")
    elif not all(isinstance(row, dict) and row for row in actions):
        errors.append("every action must be a non-empty mapping")
    if not isinstance(assertions, list) or not assertions:
        errors.append("assertions must be a non-empty list")
    elif not all(isinstance(row, dict) and row for row in assertions):
        errors.append("every assertion must be a non-empty mapping")
    normalized_reads = {
        str(path).replace("\\", "/").removeprefix("./") for path in files_read
    }
    if not normalized_reads:
        errors.append("at least one repository tool observation is required")
    if not isinstance(evidence, list) or not evidence:
        errors.append("source_evidence must be a non-empty list")
    else:
        for index, row in enumerate(evidence):
            if not isinstance(row, dict) or set(row) != {
                "source_path",
                "matched_symbol",
                "rationale",
            }:
                errors.append(
                    f"source_evidence[{index}] must contain exactly source_path, matched_symbol, rationale"
                )
                continue
            for field in ("source_path", "matched_symbol", "rationale"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    errors.append(
                        f"source_evidence[{index}].{field} must be non-empty"
                    )
            source_path = (
                str(row.get("source_path") or "")
                .replace("\\", "/")
                .removeprefix("./")
            )
            if source_path and source_path not in normalized_reads:
                errors.append(
                    f"source_evidence[{index}].source_path was not observed: {source_path}"
                )
    if errors:
        return None, None, None, errors
    candidate = copy.deepcopy(dict(accepted_spec))
    candidate["actions"] = copy.deepcopy(actions)
    candidate["assertions"] = copy.deepcopy(assertions)
    try:
        json.dumps(candidate, sort_keys=True, ensure_ascii=True)
    except Exception as error:
        errors.append(f"planned transition is not JSON serializable: {error}")
    errors.extend(
        generated_spec_acceptance_errors(
            candidate, issue=task.issue, benchmark=task.benchmark
        )
    )
    if errors:
        return None, None, None, errors
    return (
        copy.deepcopy(actions),
        copy.deepcopy(assertions),
        copy.deepcopy(evidence),
        [],
    )


class SourceTransitionActionAgent:
    """Source-exploration transition planner with one public-only repair."""

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        base_url: str,
        api_key_env: str,
        temperature: float,
        max_tokens: int = SOURCE_TRANSITION_AGENT_MAX_TOKENS,
        max_steps: int = SOURCE_TRANSITION_AGENT_MAX_STEPS,
        format_repairs: int = SOURCE_TRANSITION_AGENT_FORMAT_REPAIRS,
        cache_root: str | Path | None = None,
        extra_headers: Mapping[str, str] | None = None,
        client: Any | None = None,
        client_factory: Callable[..., Any] = OpenAICompatibleChatClient,
    ) -> None:
        if not model:
            raise SourceTransitionAgentError("source transition model is required")
        if int(max_steps) < 2:
            raise SourceTransitionAgentError("max_steps must be >= 2")
        if int(max_tokens) < 1:
            raise SourceTransitionAgentError("max_tokens must be positive")
        if int(format_repairs) not in {0, 1}:
            raise SourceTransitionAgentError("format_repairs must be 0 or 1")
        self.model = str(model)
        self.provider = str(provider)
        self.base_url = str(base_url)
        self.api_key_env = str(api_key_env)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_steps = int(max_steps)
        self.format_repairs = int(format_repairs)
        self.cache_root = (
            Path(cache_root).expanduser().resolve()
            if cache_root is not None
            else None
        )
        self.client = client or client_factory(
            api_key=os.environ.get(self.api_key_env, ""),
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            extra_headers=dict(extra_headers or {}),
            minimum_max_tokens=1,
            reasoning_retry_max_tokens=None,
            cost_kind="source_action_exploration",
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def plan(
        self,
        task: Any,
        accepted_spec: Mapping[str, Any],
        *,
        base: str | Path,
        seed: int,
        artifact_dir: str | Path,
        pair_start_fingerprint: str | None = None,
    ) -> SourceTransitionPlan:
        repo = resolve_task_repo(base, task.issue)
        sandbox = RepoSandbox(repo)
        request = build_source_transition_request(
            task,
            accepted_spec,
            repo=repo,
            repo_map=sandbox.build_repo_map(),
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
        )
        prompt = build_source_transition_prompt(request)
        request_profile = model_request_profile_provenance(self.model)
        try:
            repo_revision = run_context.git_commit(repo) or "unknown"
        except Exception:
            repo_revision = "unknown"
        cache_key = {
            "schema": SOURCE_TRANSITION_AGENT_SCHEMA,
            "request": request,
            "accepted_start_sha256": _json_sha256(
                request["accepted_start"]
            ),
            "action_contract_sha256": _json_sha256(
                request["allowed_action_schema"]
            ),
            "assertion_contract_sha256": _json_sha256(
                request["allowed_assertion_schema"]
            ),
            "repo_revision": repo_revision,
            "pair_start_fingerprint": pair_start_fingerprint,
            "seed": int(seed),
            "model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "request_profile": request_profile,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_steps": self.max_steps,
            "format_repairs": self.format_repairs,
            "prompt_revision": SOURCE_TRANSITION_AGENT_PROMPT_REVISION,
        }
        fingerprint = canonical_hash(cache_key, length=64)
        artifact_path = Path(artifact_dir)
        if self.cache_root is None:
            return self._produce(
                task,
                accepted_spec,
                request=request,
                prompt=prompt,
                request_profile=request_profile,
                sandbox=sandbox,
                artifact_dir=artifact_path,
                cache_hit=False,
            )
        cache_path = self.cache_root / fingerprint[:2] / f"{fingerprint}.json"
        with runtime_lease(cache_path, lock_dir=self.cache_root / "locks"):
            cached = self._read_cache(cache_path, cache_key)
            if cached is not None:
                return self._materialize_cached(
                    cached, artifact_dir=artifact_path, cache_hit=True
                )
            plan = self._produce(
                task,
                accepted_spec,
                request=request,
                prompt=prompt,
                request_profile=request_profile,
                sandbox=sandbox,
                artifact_dir=artifact_path,
                cache_hit=False,
            )
            if plan.status in {"generated", "repaired", "invalid"} and bool(
                plan.cost.get("usage_complete")
            ):
                atomic_write_json(
                    cache_path,
                    {
                        "schema_version": SOURCE_TRANSITION_AGENT_SCHEMA_VERSION,
                        "cache_key": cache_key,
                        "plan": plan.to_report(),
                        "artifacts": self._artifact_texts(plan),
                    },
                )
            return plan

    def _produce(
        self,
        task: Any,
        accepted_spec: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        prompt: str,
        request_profile: Mapping[str, Any],
        sandbox: RepoSandbox,
        artifact_dir: Path,
        cache_hit: bool,
    ) -> SourceTransitionPlan:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = artifact_dir / "prompt.txt"
        response_path = artifact_dir / "response.txt"
        transcript_path = artifact_dir / "transcript.json"
        full_prompt = build_source_transition_system_prompt() + "\n\n" + prompt
        prompt_path.write_text(full_prompt, encoding="utf-8")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_source_transition_system_prompt()},
            {"role": "user", "content": prompt},
        ]
        transcript = copy.deepcopy(messages)
        marker = cost.mark_records()
        started = time.perf_counter()
        raw = ""
        errors: list[str] = []
        infrastructure = False
        tool_calls = 0
        final_seen = False
        actions: list[dict[str, Any]] | None = None
        assertions: list[dict[str, Any]] | None = None
        evidence: list[dict[str, Any]] | None = None
        try:
            for step in range(1, self.max_steps + 1):
                if step >= self.max_steps - 1:
                    remaining = self.max_steps - step + 1
                    nudge = (
                        f"REMINDER: {remaining} step(s) remain and no repository "
                        "tool has been used. Issue one ACTION now; a zero-tool FINAL "
                        "is invalid."
                        if tool_calls < 1
                        else f"REMINDER: {remaining} step(s) remain. Emit FINAL now "
                        "using the source evidence already observed; do not issue "
                        "another ACTION."
                    )
                    messages.append({"role": "user", "content": nudge})
                    transcript.append(
                        {"role": "user", "content": nudge, "finalize_nudge": True}
                    )
                call_marker = cost.mark_records()
                call = self.client.converse(self.model, messages)
                reply = getattr(call, "content", call)
                if not isinstance(reply, str):
                    raise SourceTransitionAgentError(
                        "source transition response content is not text"
                    )
                messages.append({"role": "assistant", "content": reply})
                transcript.append({"role": "assistant", "content": reply})
                kind, *rest = parse_react_reply(reply)
                if kind == "final":
                    if tool_calls < 1:
                        guidance = (
                            "ERROR: source-transition output requires at least one "
                            "repository ACTION before FINAL. Issue read_file, grep, "
                            "or list_dir now."
                        )
                        messages.append({"role": "user", "content": guidance})
                        transcript.append(
                            {
                                "role": "user",
                                "content": guidance,
                                "zero_tool_final_rejected": True,
                            }
                        )
                        continue
                    final_seen = True
                    raw = rest[0]
                    cost.reclassify_since(
                        call_marker,
                        model=self.model,
                        from_kind="source_action_exploration",
                        to_kind="source_action_synthesis",
                    )
                    (
                        actions,
                        assertions,
                        evidence,
                        errors,
                    ) = parse_and_validate_source_transition(
                        raw,
                        task=task,
                        accepted_spec=accepted_spec,
                        files_read=list(sandbox.reads),
                    )
                    break
                if kind == "action":
                    tool, args = rest[0], rest[1]
                    observation = sandbox.dispatch(tool, args)
                    tool_calls += 1
                    observation_message = f"OBSERVATION:\n{observation}"
                    messages.append(
                        {"role": "user", "content": observation_message}
                    )
                    transcript.append(
                        {
                            "role": "user",
                            "content": observation_message,
                            "tool": tool,
                            "args": args,
                        }
                    )
                    continue
                guidance = (
                    "ERROR: reply was not one ACTION block or one FINAL fenced YAML mapping."
                )
                messages.append({"role": "user", "content": guidance})
                transcript.append(
                    {
                        "role": "user",
                        "content": guidance,
                        "parse_error": rest[0] if rest else "",
                    }
                )
            if not final_seen:
                errors = ["no FINAL transition within max_steps"]
        except Exception as error:
            errors = [f"source transition call failed: {type(error).__name__}: {error}"]
            infrastructure = _planner_infrastructure_error(error)
        response_path.write_text(raw, encoding="utf-8")
        transcript_path.write_text(
            json.dumps(
                {"task_id": task.id, "model": self.model, "messages": transcript},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        repair_used = False
        repair_prompt_path: Path | None = None
        repair_response_path: Path | None = None
        if actions is None and not infrastructure and self.format_repairs:
            repair_used = True
            repair_prompt = build_source_transition_repair_prompt(
                request, raw, errors
            )
            repair_prompt_path = artifact_dir / "repair-prompt.txt"
            repair_response_path = artifact_dir / "repair-response.txt"
            repair_prompt_path.write_text(repair_prompt, encoding="utf-8")
            try:
                repair_marker = cost.mark_records()
                call = self.client.converse(
                    self.model,
                    [
                        {
                            "role": "system",
                            "content": (
                                "Repair a WarpTest source-transition YAML mapping "
                                "without adding private or unobserved task facts."
                            ),
                        },
                        {"role": "user", "content": repair_prompt},
                    ],
                )
                raw = getattr(call, "content", call)
                if not isinstance(raw, str):
                    raise SourceTransitionAgentError(
                        "source transition repair content is not text"
                    )
                cost.reclassify_since(
                    repair_marker,
                    model=self.model,
                    from_kind="source_action_exploration",
                    to_kind="source_action_format_repair",
                )
                repair_response_path.write_text(raw, encoding="utf-8")
                (
                    actions,
                    assertions,
                    evidence,
                    errors,
                ) = parse_and_validate_source_transition(
                    raw,
                    task=task,
                    accepted_spec=accepted_spec,
                    files_read=list(sandbox.reads),
                )
            except Exception as error:
                if repair_response_path is not None:
                    repair_response_path.write_text(raw, encoding="utf-8")
                errors = [f"repair call failed: {type(error).__name__}: {error}"]
                infrastructure = _planner_infrastructure_error(error)
                actions = assertions = evidence = None
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
        assertion_rows = tuple(copy.deepcopy(assertions or []))
        evidence_rows = tuple(copy.deepcopy(evidence or []))
        return SourceTransitionPlan(
            status=status,
            actions=action_rows,
            assertions=assertion_rows,
            source_evidence=evidence_rows,
            model=self.model,
            provider=self.provider,
            request_profile=dict(request_profile),
            input_sha256=_json_sha256(request),
            prompt_sha256=_text_sha256(full_prompt),
            response_sha256=_text_sha256(raw) if raw else None,
            actions_sha256=_json_sha256(list(action_rows)) if action_rows else None,
            assertions_sha256=(
                _json_sha256(list(assertion_rows)) if assertion_rows else None
            ),
            source_evidence_sha256=(
                _json_sha256(list(evidence_rows)) if evidence_rows else None
            ),
            transcript_sha256=_text_sha256(
                transcript_path.read_text(encoding="utf-8")
            ),
            tool_call_count=tool_calls,
            files_read=tuple(str(path) for path in sandbox.reads),
            repair_used=repair_used,
            cache_hit=cache_hit,
            generation_ms=elapsed_ms,
            cost=plan_cost,
            attributed_cost=plan_cost,
            prompt_path=str(prompt_path),
            response_path=str(response_path),
            transcript_path=str(transcript_path),
            repair_prompt_path=(
                str(repair_prompt_path) if repair_prompt_path is not None else None
            ),
            repair_response_path=(
                str(repair_response_path)
                if repair_response_path is not None
                else None
            ),
            errors=tuple(str(error) for error in errors),
            infrastructure_error=(status == "incomplete"),
        )

    @staticmethod
    def _read_cache(
        path: Path, cache_key: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != SOURCE_TRANSITION_AGENT_SCHEMA_VERSION
            or payload.get("cache_key") != cache_key
            or not isinstance(payload.get("plan"), dict)
        ):
            return None
        return payload

    def _materialize_cached(
        self, payload: Mapping[str, Any], *, artifact_dir: Path, cache_hit: bool
    ) -> SourceTransitionPlan:
        plan = dict(payload["plan"])
        artifacts = payload.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, Mapping) else {}
        artifact_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str | None] = {}
        for key, filename in (
            ("prompt", "prompt.txt"),
            ("response", "response.txt"),
            ("transcript", "transcript.json"),
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
        attributed = (
            attributed if isinstance(attributed, Mapping) else plan.get("cost")
        )
        return SourceTransitionPlan(
            status=str(plan.get("status") or "invalid"),
            actions=tuple(
                copy.deepcopy(row)
                for row in plan.get("actions", [])
                if isinstance(row, dict)
            ),
            assertions=tuple(
                copy.deepcopy(row)
                for row in plan.get("assertions", [])
                if isinstance(row, dict)
            ),
            source_evidence=tuple(
                copy.deepcopy(row)
                for row in plan.get("source_evidence", [])
                if isinstance(row, dict)
            ),
            model=self.model,
            provider=self.provider,
            request_profile=dict(plan.get("request_profile") or {}),
            input_sha256=str(plan.get("input_sha256") or ""),
            prompt_sha256=str(plan.get("prompt_sha256") or ""),
            response_sha256=plan.get("response_sha256"),
            actions_sha256=plan.get("actions_sha256"),
            assertions_sha256=plan.get("assertions_sha256"),
            source_evidence_sha256=plan.get("source_evidence_sha256"),
            transcript_sha256=plan.get("transcript_sha256"),
            tool_call_count=int(plan.get("tool_call_count", 0) or 0),
            files_read=tuple(str(path) for path in plan.get("files_read", [])),
            repair_used=bool(plan.get("repair_used")),
            cache_hit=cache_hit,
            generation_ms=0,
            cost=_empty_cost(),
            attributed_cost=dict(attributed or _empty_cost()),
            prompt_path=paths["prompt"],
            response_path=paths["response"],
            transcript_path=paths["transcript"],
            repair_prompt_path=paths["repair_prompt"],
            repair_response_path=paths["repair_response"],
            errors=tuple(str(error) for error in plan.get("errors", [])),
            infrastructure_error=False,
        )

    @staticmethod
    def _artifact_texts(plan: SourceTransitionPlan) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in (
            ("prompt", plan.prompt_path),
            ("response", plan.response_path),
            ("transcript", plan.transcript_path),
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


class DeterministicSourceTransitionActionAgent:
    """Explicit zero-provider planner used only by offline plumbing tests."""

    def __init__(self, **_kwargs: Any) -> None:
        self.model = "deterministic-source-transition-fixture"
        self.provider = "offline"

    def close(self) -> None:
        return None

    def plan(
        self,
        task: Any,
        accepted_spec: Mapping[str, Any],
        *,
        base: str | Path,
        seed: int,
        artifact_dir: str | Path,
        pair_start_fingerprint: str | None = None,
    ) -> SourceTransitionPlan:
        del base, seed, pair_start_fingerprint
        actions = accepted_spec.get("actions")
        assertions = accepted_spec.get("assertions")
        action_rows = tuple(
            copy.deepcopy(row) for row in actions or [] if isinstance(row, dict)
        )
        assertion_rows = tuple(
            copy.deepcopy(row)
            for row in assertions or []
            if isinstance(row, dict)
        )
        evidence_rows = (
            {
                "source_path": "offline-fixture",
                "matched_symbol": "deterministic plumbing",
                "rationale": "No provider call; route coverage only.",
            },
        )
        empty = _empty_cost()
        artifact = Path(artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        transcript = artifact / "transcript.json"
        transcript.write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "offline_fixture": True,
                    "provider_calls": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        status = (
            "generated" if action_rows and assertion_rows else "invalid"
        )
        return SourceTransitionPlan(
            status=status,
            actions=action_rows,
            assertions=assertion_rows,
            source_evidence=evidence_rows,
            model=self.model,
            provider=self.provider,
            request_profile={},
            input_sha256=_json_sha256(
                {"task_id": task.id, "offline_fixture": True}
            ),
            prompt_sha256=_text_sha256("offline fixture"),
            response_sha256=_text_sha256("offline fixture"),
            actions_sha256=(
                _json_sha256(list(action_rows)) if action_rows else None
            ),
            assertions_sha256=(
                _json_sha256(list(assertion_rows)) if assertion_rows else None
            ),
            source_evidence_sha256=_json_sha256(list(evidence_rows)),
            transcript_sha256=_text_sha256(
                transcript.read_text(encoding="utf-8")
            ),
            tool_call_count=0,
            files_read=(),
            repair_used=False,
            cache_hit=False,
            generation_ms=0,
            cost=empty,
            attributed_cost=empty,
            transcript_path=str(transcript),
            errors=(),
            infrastructure_error=False,
        )


__all__ = [
    "SOURCE_TRANSITION_AGENT_FORMAT_REPAIRS",
    "SOURCE_TRANSITION_AGENT_MAX_STEPS",
    "SOURCE_TRANSITION_AGENT_MAX_TOKENS",
    "SOURCE_TRANSITION_AGENT_PROMPT_REVISION",
    "SOURCE_TRANSITION_AGENT_SCHEMA",
    "SOURCE_TRANSITION_AGENT_SCHEMA_VERSION",
    "SourceTransitionActionAgent",
    "SourceTransitionAgentError",
    "SourceTransitionPlan",
    "DeterministicSourceTransitionActionAgent",
    "build_source_transition_prompt",
    "build_source_transition_repair_prompt",
    "build_source_transition_request",
    "parse_and_validate_source_transition",
]
