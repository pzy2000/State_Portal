from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from .adapters import LocalStorageSaveAdapter, TestHookAdapter, VanillaDOMAdapter, ZustandHookAdapter
from .discovery import RepositoryInspector
from .models import PhaseResult
from .profiles import SpaceRPGProfile, Game2048Profile, DatingSimProfile, ProfileError, profile_for
from .reports import new_report_dir, write_json
from .runner import load_spec, resolve_repo
from .runtime import BrowserRuntimeAdapter, json_dumps
from .model_bench import DEFAULT_MODELSCOPE_BASE_URL
from .synthesis import ModelCallResult, OpenAICompatibleChatClient, SOURCE_VOCABULARY, SynthesisError


class TransitionExplorerError(RuntimeError):
    pass


SUPPORTED_PROFILE_TYPES = (Game2048Profile, SpaceRPGProfile, DatingSimProfile)
DEFAULT_TRANSITION_MODEL = "deepseek-ai/DeepSeek-V4-Flash:DashScope"


def state_fingerprint(state: Any) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def llm_candidate_actions_for_spec(
    spec: dict[str, Any],
    benchmark: str,
    semantic_schema: dict[str, Any],
    *,
    max_actions: int,
    model: str = DEFAULT_TRANSITION_MODEL,
    base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.0,
    max_tokens: int = 2048,
    client: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_actions < 1:
        raise ValueError("--max-actions must be at least 1.")
    prompt = build_transition_action_prompt(spec, benchmark, semantic_schema, max_actions=max_actions)
    try:
        llm = client or OpenAICompatibleChatClient(
            api_key=os.environ.get(api_key_env, ""),
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except SynthesisError as error:
        raise TransitionExplorerError(f"{api_key_env} is required for LLM transition action proposal.") from error
    call: ModelCallResult = llm.converse(
        model,
        [
            {
                "role": "system",
                "content": (
                    "You propose WarpTest runtime actions for transition exploration. "
                    "Return only JSON, with no Markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_attempts=2,
    )
    actions, invalid = parse_llm_action_output(call.content, benchmark=benchmark, max_actions=max_actions)
    if not actions:
        raise TransitionExplorerError("LLM did not return any valid transition actions.")
    plan = {
        "source": "llm",
        "model": model,
        "base_url": base_url,
        "latency_ms": call.latency_ms,
        "prompt": prompt,
        "raw_output": call.content,
        "invalid_actions": invalid,
    }
    return actions, plan


def build_transition_action_prompt(spec: dict[str, Any], benchmark: str, semantic_schema: dict[str, Any], *, max_actions: int) -> str:
    action_schema = _action_schema_for_benchmark(benchmark)
    payload = {
        "task": "Propose candidate actions to run after restoring this WarpTest checkpoint.",
        "requirements": [
            f"Return at most {max_actions} actions.",
            "Each action must be executable by the existing WarpTest profile action runner.",
            "Prefer actions that test meaningful state transitions from the restored checkpoint.",
            "Use the spec's existing actions only as examples; do not blindly copy them unless they are useful.",
            "Do not invent new action types outside allowed_action_schema.",
        ],
        "output_schema": {
            "actions": [
                {
                    "type": "action type from allowed_action_schema",
                    "additional_fields": "fields required by that action type",
                }
            ],
            "notes": "short optional rationale",
        },
        "benchmark": benchmark,
        "allowed_action_schema": action_schema,
        "source_vocabulary": SOURCE_VOCABULARY.get(benchmark) or SOURCE_VOCABULARY.get(_canonical_benchmark(benchmark), {}),
        "semantic_schema": semantic_schema,
        "spec": spec,
    }
    return "\n".join(
        [
            "Return JSON only. The JSON must parse to a mapping with an actions list.",
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        ]
    )


def parse_llm_action_output(text: str, *, benchmark: str, max_actions: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body = text.strip()
    fence = re.search(r"```(?:json|yaml|yml)?\s*(.*?)```", body, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    try:
        parsed = yaml.safe_load(body)
    except Exception as error:
        raise TransitionExplorerError(f"LLM action output did not parse as JSON/YAML: {error}") from error
    if not isinstance(parsed, dict):
        raise TransitionExplorerError("LLM action output must be a mapping.")
    raw_actions = parsed.get("actions")
    if not isinstance(raw_actions, list):
        raise TransitionExplorerError("LLM action output must contain an actions list.")
    actions: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            invalid.append({"action": raw, "reason": "not a mapping"})
            continue
        action = copy.deepcopy(raw)
        reason = _invalid_action_reason(action, benchmark)
        if reason:
            invalid.append({"action": action, "reason": reason})
            continue
        actions.append(action)
        if len(actions) >= max_actions:
            break
    return _dedupe_actions(actions)[:max_actions], invalid


def candidate_actions_for_spec(spec: dict[str, Any], benchmark: str, *, max_actions: int) -> list[dict[str, Any]]:
    raise TransitionExplorerError(
        "Rule-based transition action generation has been removed. "
        "Use llm_candidate_actions_for_spec() through explore-transitions instead."
    )


def _action_schema_for_benchmark(benchmark: str) -> dict[str, Any]:
    canonical = _canonical_benchmark(benchmark)
    if canonical == "2048":
        return {
            "move": {"type": "move", "direction": "left|right|up|down", "wait_ms": "optional integer milliseconds"},
            "restart": {"type": "restart"},
        }
    if canonical == "SpaceRPG":
        return {
            "key": {"type": "key", "key": "Playwright keyboard key, e.g. ArrowUp or Escape", "wait_ms": "optional integer milliseconds"},
            "hook_call": {"type": "hook_call", "method": "existing SpaceRPG store method", "args": "optional list"},
            "wait": {"type": "wait", "ms": "integer milliseconds"},
        }
    if canonical == "DatingSim":
        return {
            "hook_action": {"type": "hook_action", "action_id": "existing deterministic action id", "wait_ms": "optional integer milliseconds"},
            "hook_call": {"type": "hook_call", "method": "existing __WARPTEST_LOVE__ hook method", "args": "optional list"},
            "click": {"type": "click", "selector": "CSS selector"},
            "wait": {"type": "wait", "ms": "integer milliseconds"},
        }
    return {}


def _invalid_action_reason(action: dict[str, Any], benchmark: str) -> str | None:
    canonical = _canonical_benchmark(benchmark)
    action_type = action.get("type")
    if canonical == "2048":
        if action_type == "move":
            if action.get("direction") not in {"left", "right", "up", "down"}:
                return "2048 move requires direction left/right/up/down."
            return None
        if action_type == "restart":
            return None
        return f"Unsupported 2048 action type: {action_type}"
    if canonical == "SpaceRPG":
        if action_type == "key":
            return None if isinstance(action.get("key"), str) and action["key"] else "SpaceRPG key action requires key."
        if action_type == "hook_call":
            return None if isinstance(action.get("method"), str) and action["method"] else "SpaceRPG hook_call requires method."
        if action_type == "wait":
            return None
        return f"Unsupported SpaceRPG action type: {action_type}"
    if canonical == "DatingSim":
        if action_type == "hook_action":
            return None if isinstance(action.get("action_id"), str) and action["action_id"] else "Love hook_action requires action_id."
        if action_type == "hook_call":
            return None if isinstance(action.get("method"), str) and action["method"] else "Love hook_call requires method."
        if action_type == "click":
            return None if isinstance(action.get("selector"), str) and action["selector"] else "Love click requires selector."
        if action_type == "wait":
            return None
        return f"Unsupported DatingSim action type: {action_type}"
    return f"Unsupported benchmark for LLM transition actions: {benchmark}"


def _canonical_benchmark(benchmark: str) -> str:
    if benchmark in {"spacerpg", "SpaceRPG"}:
        return "SpaceRPG"
    if benchmark in {"love", "DatingSim"}:
        return "DatingSim"
    return benchmark


def explore_transitions(
    spec_path: str | Path,
    *,
    repo_arg: str | None = None,
    max_actions: int = 8,
    horizon: int = 1,
    report_dir: Path | None = None,
    headless: bool = True,
    model: str = DEFAULT_TRANSITION_MODEL,
    base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.0,
    max_tokens: int = 2048,
    action_client: Any | None = None,
) -> dict[str, Any]:
    if horizon != 1:
        raise TransitionExplorerError("Only --horizon 1 is currently supported.")
    if max_actions < 1:
        raise ValueError("--max-actions must be at least 1.")

    spec_file = Path(spec_path).expanduser().resolve()
    spec = load_spec(spec_file)
    repo = resolve_repo(spec, spec_file, repo_arg)
    inspect_result = RepositoryInspector().inspect(repo)
    profile = profile_for(inspect_result)
    benchmark = str(spec.get("benchmark") or getattr(profile, "benchmark", "unknown"))
    if not isinstance(profile, SUPPORTED_PROFILE_TYPES):
        raise TransitionExplorerError(f"Transition exploration currently supports browser-web profiles only; got {profile.name}.")

    out_dir = report_dir or new_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    actions, action_plan = llm_candidate_actions_for_spec(
        spec,
        benchmark,
        asdict(inspect_result.semantic_schema),
        max_actions=max_actions,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
        client=action_client,
    )
    prompt = str(action_plan.pop("prompt", ""))
    raw_output = str(action_plan.pop("raw_output", ""))
    prompt_path = out_dir / "llm-action-prompt.txt"
    raw_path = out_dir / "llm-action-output.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    raw_path.write_text(raw_output, encoding="utf-8")
    action_plan.update(
        {
            "prompt_path": str(prompt_path),
            "raw_output_path": str(raw_path),
            "actions": actions,
            "api_key_env": api_key_env,
        }
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "spec_path": str(spec_file),
        "repo_path": str(repo),
        "benchmark": benchmark,
        "profile": profile.name,
        "action_plan": action_plan,
        "horizon": horizon,
        "max_actions": max_actions,
        "report_dir": str(out_dir),
        "nodes": [],
        "edges": [],
        "summary": {},
    }

    node_index: dict[str, dict[str, Any]] = {}
    with BrowserRuntimeAdapter(repo, inspect_result.metadata.launch_command, out_dir, headless=headless) as runtime:
        baseline_restore = _restore_checkpoint(profile, spec, runtime)
        if baseline_restore.status != "success":
            result["summary"] = {
                "status": "failed",
                "reason": baseline_restore.detail,
                "actions_considered": len(actions),
                "nodes": 0,
                "edges": 0,
                "valid_transitions": 0,
                "meaningful_transitions": 0,
            }
            result["baseline_restore"] = asdict(baseline_restore)
            result["runtime"] = _runtime_summary(runtime)
            write_json(out_dir / "transition-graph.json", result)
            return result

        baseline_state = _snapshot_state(profile, runtime)
        baseline_id = _node_id(baseline_state)
        _upsert_node(node_index, baseline_id, baseline_state, label="checkpoint")

        for index, action in enumerate(actions):
            restore = _restore_checkpoint(profile, spec, runtime)
            edge = {
                "index": index,
                "action": action,
                "from": baseline_id,
                "restore": asdict(restore),
            }
            if restore.status != "success":
                edge.update({"status": "failure", "detail": restore.detail, "changed": False, "to": baseline_id})
                result["edges"].append(edge)
                continue

            before = _snapshot_state(profile, runtime)
            before_id = _node_id(before)
            _upsert_node(node_index, before_id, before, label="checkpoint")
            start = time.perf_counter()
            phase = _run_one_action(profile, runtime, action)
            duration_ms = int((time.perf_counter() - start) * 1000)
            after = _snapshot_state(profile, runtime)
            after_id = _node_id(after)
            _upsert_node(node_index, after_id, after, label=f"after action {index}")
            edge.update(
                {
                    "from": before_id,
                    "to": after_id,
                    "status": phase.status,
                    "detail": phase.detail,
                    "phase": asdict(phase),
                    "duration_ms": duration_ms,
                    "changed": before_id != after_id,
                }
            )
            result["edges"].append(edge)

        result["runtime"] = _runtime_summary(runtime)

    result["nodes"] = list(node_index.values())
    result["summary"] = _summarize(result["nodes"], result["edges"], len(actions))
    write_json(out_dir / "transition-graph.json", result)
    return result


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        key = json.dumps(_action_signature(action), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _action_signature(action: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in action.items() if key not in {"wait_ms", "ms"}}


def _restore_checkpoint(profile: Any, spec: dict[str, Any], runtime: BrowserRuntimeAdapter) -> PhaseResult:
    target = spec.get("target", {})
    if isinstance(profile, Game2048Profile):
        board = target.get("board")
        if not board:
            return PhaseResult("failure", "2048 spec target.board is required")
        state = profile._serialized_state(
            board,
            int(target.get("score", 0)),
            bool(target.get("won", False)),
            bool(target.get("over", False)),
            bool(target.get("keepPlaying", False)),
        )
        storage_values = {"gameState": json_dumps(state)}
        if "bestScore" in target:
            storage_values["bestScore"] = str(target["bestScore"])
        if runtime.page is None:
            profile._open_with_hook(runtime, "/?warptest=1", timeout_ms=10_000)
        LocalStorageSaveAdapter(runtime).write_many(storage_values, [], profile.inspect_result.storage_policy)
        runtime.page.reload(wait_until="domcontentloaded")
        runtime.wait_for_hook(profile.hook_name, timeout_ms=10_000)
        VanillaDOMAdapter(runtime).wait_for_tiles(timeout_ms=10_000)
        return profile._validate(runtime, target)

    if isinstance(profile, SpaceRPGProfile):
        storage_values = {}
        if spec.get("setup", {}).get("disable_economy_backend", True):
            storage_values["spacerpg-e2e-disable-economy-backend"] = "true"
        storage_values["spacerpg-e2e-hook"] = "enabled"
        LocalStorageSaveAdapter(runtime).write_many(storage_values, [], profile.inspect_result.storage_policy)
        _close_page(runtime)
        profile._open_with_hook(runtime, "/?warptest=1", timeout_ms=30_000)
        restored = profile._restore_target(runtime, target)
        if restored.status != "success":
            return restored
        return profile._validate(runtime, target)

    if isinstance(profile, DatingSimProfile):
        storage = {"warptest-love-ai-mode": spec.get("setup", {}).get("ai_mode", "mocked")}
        LocalStorageSaveAdapter(runtime).write_many(storage, ["love_emulator_save_v1"], profile.inspect_result.storage_policy)
        runtime.block_ai_network("mocked")
        _close_page(runtime)
        profile._open_with_hook(runtime, "/?warptest=1&ai=mocked", timeout_ms=20_000)
        target = spec.get("target", {})
        zustand = ZustandHookAdapter(TestHookAdapter(runtime, profile.hook_name))
        zustand.initialize({"seed": target.get("seed", 1), "name": target.get("player_name", "WarpTest"), "gender": target.get("gender", "male")})
        if target.get("patch"):
            zustand.patch_state(target["patch"])
        if target.get("save"):
            zustand.save()
        return profile._validate(runtime, target)

    raise TransitionExplorerError(f"Unsupported profile for transition exploration: {profile.name}.")


def _run_one_action(profile: Any, runtime: BrowserRuntimeAdapter, action: dict[str, Any]) -> PhaseResult:
    try:
        return profile._run_actions(runtime, [copy.deepcopy(action)])
    except ProfileError as error:
        return PhaseResult("failure", str(error))
    except Exception as error:
        return PhaseResult("failure", f"Action execution failed: {error}")


def _snapshot_state(profile: Any, runtime: BrowserRuntimeAdapter) -> dict[str, Any]:
    if isinstance(profile, Game2048Profile):
        return runtime.page.evaluate(
            """() => {
              const state = window.__WARPTEST_2048__?.getState?.() ?? JSON.parse(localStorage.getItem("gameState"));
              const cells = state?.grid?.cells || [];
              const board = Array.from({ length: 4 }, (_, y) =>
                Array.from({ length: 4 }, (_, x) => cells?.[x]?.[y]?.value ?? 0)
              );
              return {
                board,
                score: state?.score ?? null,
                won: !!state?.won,
                over: !!state?.over,
                keepPlaying: !!state?.keepPlaying
              };
            }"""
        )
    if isinstance(profile, SpaceRPGProfile):
        return runtime.page.evaluate(
            """() => {
              const s = window.__SPACERPG_E2E__.getState();
              return {
                screen: s.screen,
                currentSystemId: s.currentSystemId,
                currentStationId: s.currentStationId,
                stationTab: s.stationTab,
                galaxyMapMode: s.galaxyMapMode,
                activeMissionIds: (s.activeMissions || []).map((m) => m.id),
                completedMissionIds: s.completedMissionIds || [],
                knownSystems: s.knownSystems || [],
                credits: s.player?.credits,
                cargo: s.player?.cargo,
                equipment: s.player?.equipment || [],
                equipmentInventory: s.player?.equipmentInventory || {},
                activeSaveSlotId: s.activeSaveSlotId,
                hasSave: s.hasSave,
                saveSlotCount: s.saveSlots?.filter((slot) => slot.exists).length ?? 0
              };
            }"""
        )
    if isinstance(profile, DatingSimProfile):
        return runtime.page.evaluate(
            """() => {
              const s = window.__WARPTEST_LOVE__.getState();
              return {
                isSetupComplete: !!s.isSetupComplete,
                player: {
                  name: s.player?.name,
                  title: s.player?.title,
                  resources: s.player?.resources,
                  relationships: s.player?.relationships
                },
                world: {
                  location: s.world?.location,
                  day: s.world?.day,
                  globalFlags: s.world?.globalFlags
                },
                activeDialogueOpen: !!s.activeDialogue?.isOpen,
                currentEventId: s.currentEvent?.id ?? null,
                isGeneratingEvent: !!s.isGeneratingEvent
              };
            }"""
        )
    raise TransitionExplorerError(f"Unsupported profile for state snapshot: {profile.name}.")


def _node_id(state: Any) -> str:
    return f"state:{state_fingerprint(state)}"


def _upsert_node(node_index: dict[str, dict[str, Any]], node_id: str, state: Any, *, label: str) -> None:
    if node_id not in node_index:
        node_index[node_id] = {"id": node_id, "label": label, "state": state}


def _close_page(runtime: BrowserRuntimeAdapter) -> None:
    page = getattr(runtime, "page", None)
    if page is None:
        return
    try:
        page.close()
    except Exception:
        pass
    runtime.page = None


def _runtime_summary(runtime: BrowserRuntimeAdapter) -> dict[str, Any]:
    return {
        "app_url": runtime.app_url,
        "launch_command": runtime.launch_command,
        "server_log": str(runtime.server_log),
        "console_errors": runtime.console_errors,
        "page_errors": runtime.page_errors,
        "network_api_calls": runtime.network_api_calls,
    }


def _summarize(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], action_count: int) -> dict[str, Any]:
    valid = [edge for edge in edges if edge.get("status") == "success"]
    meaningful = [edge for edge in valid if edge.get("changed")]
    failed = [edge for edge in edges if edge.get("status") != "success"]
    return {
        "status": "success" if not failed else "partial",
        "actions_considered": action_count,
        "nodes": len(nodes),
        "edges": len(edges),
        "valid_transitions": len(valid),
        "meaningful_transitions": len(meaningful),
        "failed_transitions": len(failed),
    }
