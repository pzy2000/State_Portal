"""State fuzzing for the deep-state robustness benchmark (C3).

state -> mutation -> states: take a known-good deep state (the oracle spec's target)
and, with an LLM-driven evolutionary fuzzer (TitanFuzz-style; see
:mod:`warptest.fuzz_llm`), generate diverse boundary/invalid mutated states. Each
mutated state is injected on a warm StateSession (so fuzzing N states is cheap) and
checked against a robustness oracle. A mutation is a DEFECT when injecting it makes
the game crash / render blank / emit severe console errors / soft-lock; it is ROBUST
when the game renders and stays interactive (gracefully clamps or ignores the
perturbation). Mutations the harness cannot even build are reported as REJECTED.

The generation engine is an LLM: an LLM synthesizes seed states and infills masked
fields under multi-armed-bandit-selected mutation operators, guided by a fitness
function over the state's structure. There is deliberately no rule-based fallback --
if the LLM is unavailable the run raises, so experiments never quietly degrade to
fake mutations.

This surfaces deep-state robustness failures that entry-screen / GUI-agent testing
never reaches, and is only tractable because the state-managed session restores each
mutated state without re-paying engine/browser startup.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import cost, run_context
from .fuzz_llm import (
    OPERATOR_NOTES,
    InjectResult,
    LLMStateFuzzer,
    fitness,
    run_evo_fuzz,
)
from .jynew_fuzz import (
    C3_SESSION_VERSION as JYNEW_C3_SESSION_VERSION,
    JynewFuzzStateError,
    JynewWarmOutcome,
    JynewWarmSession,
    build_jynew_target,
    project_jynew_fuzz_state,
    sanitize_jynew_fuzz_state,
    target_mod_id as jynew_target_mod_id,
)
from .jynew_runtime import find_jynew_project_path
from .openra_fuzz import (
    C3_SESSION_VERSION,
    OpenRAFuzzStateError,
    OpenRAWarmOutcome,
    OpenRAWarmSession,
    project_openra_fuzz_state,
    sanitize_openra_fuzz_state,
)
from .reports import new_report_dir, write_json
from .runner import load_spec
from .state_store import StateManagementUnsupported, StateSession
from .synthesis import DEFAULT_SYNTHESIS_MODEL
from .task_schema import resolve_task_repo
from .state_loop import load_state_loop_tasks
from .visual import screenshot_quality

# Browser restore goes through StateSession (console/blank-render/soft-lock
# oracle); the luanti engine path classifies a restore failure as a synthesis
# defect. The two paths surface DIFFERENT defect definitions, so every defect
# record is tagged with its path and aggregation must not silently merge them.
DEFECT_PATH_BROWSER = "browser"
DEFECT_PATH_ENGINE = "engine"
OPENRA_INJECTION_MODE = "native_warm_session"
OPENRA_REACHABILITY = "injection-only"
JYNEW_INJECTION_MODE = "native_warm_session"
# save_index is pinned to -1 for every C3 candidate (see build_jynew_target), so
# a fuzz run never reaches jynew's ES3 save-restorable path.
JYNEW_REACHABILITY = "injection-only"

# SpaceRPG fuzz injection kinds. Family A (load path) writes localStorage + reload +
# drives the game's own loader; Family B (live memory) mutates the running store.
# ``action_replay`` fuzzes a store-action sequence rather than a state snapshot.
SPACERPG_LOAD_KINDS = ("save_restore", "legacy_save", "save_index")
SPACERPG_STATE_KINDS = ("state_overlay", "replace_state")
SPACERPG_ACTION_KIND = "action_replay"
SPACERPG_ALL_KINDS = SPACERPG_LOAD_KINDS + SPACERPG_STATE_KINDS + (SPACERPG_ACTION_KIND,)
# kinds whose candidate is a mutated SaveGameData (share one evo pass + candidate)
SPACERPG_SAVE_KINDS = SPACERPG_LOAD_KINDS + SPACERPG_STATE_KINDS
SPACERPG_KIND_TO_TARGET = {kind: f"fuzz_{kind}" for kind in SPACERPG_ALL_KINDS}
# SaveGameData fields kept in a curated seed (drop bulky/regenerable marketState etc.)
SPACERPG_SEED_KEEP_FIELDS = (
    "version", "savedAt", "screen", "currentSystemId", "currentStationId", "gameClock",
    "player", "activeMissions", "completedMissionIds", "failedMissionIds", "reputation",
    "factionHeat", "knownSystems", "knownPlanetIds", "explorationState", "dialogueState",
    "onboardingState",
)

_VOLATILE_TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|\d+(?:\.\d+)?")
_PATH_TOKEN_RE = re.compile(r"/[^\s:;,'\"]+")


def normalize_detail(detail: str | None) -> str:
    """Collapse run-specific tokens so the same defect dedups across runs/seeds.

    Console errors and frame-quality blobs carry volatile numbers, hex ids, and
    absolute temp paths; left raw they would split one defect into many unique
    fingerprints. We replace paths and numeric/hex tokens with placeholders.
    """

    if not detail:
        return ""
    text = _PATH_TOKEN_RE.sub("<path>", str(detail))
    text = _VOLATILE_TOKEN_RE.sub("<n>", text)
    return re.sub(r"\s+", " ", text).strip().lower()[:300]


def mutated_state_signature(spec: dict[str, Any] | None) -> str:
    """Stable signature of the mutated deep-state (the perturbed target)."""

    if not isinstance(spec, dict):
        return ""
    target = spec.get("target")
    if not isinstance(target, dict):
        return ""
    return json.dumps(target, sort_keys=True, ensure_ascii=False, default=str)[:400]


def _screenshot_hash(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def defect_fingerprint(
    *,
    benchmark: Any,
    task_id: Any,
    mutation: Any,
    defect_type: Any,
    detail: Any,
    mutated_state: dict[str, Any] | None,
    defect_path: str,
    screenshot: str | None = None,
) -> str:
    """Deterministic unique-defect key for the C3 curve.

    Degrades gracefully: any missing field becomes ``""`` instead of raising, so
    the engine path (which produces no screenshot) still dedups. The screenshot
    hash only participates for ``blank_render`` defects, where the frame itself is
    the evidence; for console/soft-lock defects the normalized detail is the
    evidence and a volatile frame must not over-split the bucket.
    """

    screenshot_key = _screenshot_hash(screenshot) if defect_type == "blank_render" else ""
    parts = [
        str(benchmark or ""),
        str(task_id or ""),
        str(mutation or ""),
        str(defect_type or ""),
        normalize_detail(detail),
        mutated_state_signature(mutated_state),
        screenshot_key,
        str(defect_path or ""),
    ]
    blob = "\u0001".join(parts)
    # surrogatepass keeps hashing deterministic even if a lone surrogate reaches here
    # from some path, so a single adversarial candidate never aborts the run.
    return hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def iter_defect_records(results: list[dict[str, Any]]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(benchmark, task_id, mutation_record)`` for every defect verdict."""

    for result in results:
        bench = str(result.get("benchmark"))
        task_id = str(result.get("task_id"))
        for mut in result.get("mutations", []) or []:
            if isinstance(mut, dict) and mut.get("verdict") == "defect":
                yield bench, task_id, mut


def unique_defect_fingerprints(results: list[dict[str, Any]]) -> set[str]:
    """Distinct defect fingerprints across all tasks (the unique-defect set)."""

    fingerprints: set[str] = set()
    for _, _, mut in iter_defect_records(results):
        fingerprint = mut.get("fingerprint")
        if fingerprint:
            fingerprints.add(str(fingerprint))
    return fingerprints


@dataclass
class FuzzConfig:
    """Knobs for the LLM-driven evolutionary state fuzzer (per run)."""

    model: str = DEFAULT_SYNTHESIS_MODEL
    api_key_env: str = "MODELSCOPE_API_KEY"
    num_seeds: int = 4
    samples_per_op: int = 2
    top_n: int = 5
    temperature: float = 0.9
    reasoning_effort: str | None = "medium"  # thinking intensity for reasoning models
    budget_ms: int | None = None  # override per-task budget when set
    seed: int | None = None
    resume: bool = False  # skip tasks already completed in the report-dir's fuzz.json
    # SpaceRPG-only: enabled injection kinds (subset of SPACERPG_ALL_KINDS). None -> all.
    spacerpg_inject: tuple[str, ...] | None = None
    refresh_seeds: bool = False  # SpaceRPG-only: recapture seed saves, ignoring cache
    # Issue-guided structured priors: "none" (control), "auto" (conventional path),
    # or an explicit path. Resolved per-benchmark in _make_fuzzer.
    priors: str = "none"


@dataclass
class FuzzOutcome:
    mutation: str
    verdict: str  # robust | defect | rejected
    defect_type: str | None = None
    detail: str = ""
    screenshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_GRACEFUL_RECOVERY_RE = re.compile(
    r"recovered from an unexpected render error|error boundary you provided",
    re.I,
)


def _is_graceful_recovery_console(console: list) -> bool:
    """True when the captured console noise is fully explained by a render error
    that a React error boundary already caught and recovered from (e.g. SpaceRPG's
    ``GameErrorBoundary.componentDidCatch``), not a genuine uncaught failure.

    React only logs "...using the error boundary you provided, X" when a boundary
    actually catches the error, and a well-behaved boundary additionally logs its
    own recovery ("recovered from an unexpected render error"). When either signal
    is present, console.error noise alone must not fail the mutation -- whether the
    app actually stayed usable is decided by the blank_render/soft_lock checks below,
    which test the ACTUAL post-recovery screen rather than pattern-matching log text.
    """
    if not console:
        return False
    text = "\n".join(str(c) for c in console)
    return bool(_GRACEFUL_RECOVERY_RE.search(text))


def _robustness_verdict(outcome: Any) -> FuzzOutcome:
    """Classify an injected mutated state from the restore outcome + report signals."""
    report = outcome.report if isinstance(getattr(outcome, "report", None), dict) else None
    if report is None:
        # restore raised before producing a report -> the harness could not build it.
        return FuzzOutcome(mutation="", verdict="rejected", detail=outcome.detail or "restore raised")
    console = report.get("console_errors") or []
    if isinstance(console, list) and console and not _is_graceful_recovery_console(console):
        return FuzzOutcome(mutation="", verdict="defect", defect_type="console_error",
                           detail="; ".join(str(c) for c in console[:3]), screenshot=outcome.screenshot)
    # Blank / non-rendered frame is a render failure on the mutated state.
    if outcome.screenshot and Path(outcome.screenshot).exists():
        quality = screenshot_quality(Path(outcome.screenshot))
        if not quality["informative"]:
            return FuzzOutcome(mutation="", verdict="defect", defect_type="blank_render",
                               detail=f"non-informative frame: {quality}", screenshot=outcome.screenshot)
    soft = report.get("soft_lock_signal") if isinstance(report.get("soft_lock_signal"), dict) else {}
    if soft.get("actionable_buttons") == 0 and soft.get("dialogue_open") is not True:
        return FuzzOutcome(mutation="", verdict="defect", defect_type="soft_lock",
                           detail="no actionable UI", screenshot=outcome.screenshot)
    return FuzzOutcome(mutation="", verdict="robust", screenshot=outcome.screenshot)


def _aggregate_costs(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-task LLM cost summaries into a run-level ledger.

    Per-task cost is captured with nested :func:`cost.scoped_meter` blocks (records go
    to the innermost meter only), so the run total is aggregated here rather than by a
    wrapping meter.
    """

    total = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "llm_latency_ms": 0,
        "est_usd": 0.0,
        "by_model": {},
    }
    saw_usd = False
    for result in results:
        summary = result.get("cost")
        if not isinstance(summary, dict):
            continue
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens", "llm_latency_ms"):
            total[key] += int(summary.get(key) or 0)
        est = summary.get("est_usd")
        if est is not None:
            saw_usd = True
            total["est_usd"] += float(est)
        for model, bucket in (summary.get("by_model") or {}).items():
            agg = total["by_model"].setdefault(
                model,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0, "est_usd": None},
            )
            for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms"):
                agg[key] += int(bucket.get(key) or 0)
            if bucket.get("est_usd") is not None:
                agg["est_usd"] = round((agg["est_usd"] or 0.0) + float(bucket["est_usd"]), 6)
    total["est_usd"] = round(total["est_usd"], 6) if saw_usd else None
    return total


def _seed_target_of(seed_spec: dict[str, Any]) -> dict[str, Any]:
    target = seed_spec.get("target")
    if not isinstance(target, dict) or not target:
        raise ValueError("oracle spec has no dict 'target' to seed the fuzzer")
    return copy.deepcopy(target)


def _preflight_llm(config: FuzzConfig) -> None:
    """Fail fast if the LLM is unreachable, before doing any task work.

    Deliberately hard-raises (no fallback) so a run never quietly proceeds without a
    real generation engine, which would make experiment results uninterpretable.
    """

    import os

    from .providers import resolve_model
    from .synthesis import DEFAULT_MODELSCOPE_BASE_URL

    _, provider = resolve_model(
        config.model,
        default_base_url=DEFAULT_MODELSCOPE_BASE_URL,
        default_api_key_env=config.api_key_env,
    )
    if not os.environ.get(provider.api_key_env, ""):
        raise RuntimeError(
            f"LLM is required for state fuzzing but no API key is set: expected "
            f"environment variable '{provider.api_key_env}' (provider '{provider.name}', "
            f"model '{config.model}'). Set it and retry -- there is no offline fallback."
        )


def _build_fuzz_payload(
    base: Path,
    tasks_path: str | Path,
    cfg: FuzzConfig,
    results: list[dict[str, Any]],
    run_meta: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": 3,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": "state_fuzzing",
        "task_file": str((base / tasks_path).resolve() if not Path(tasks_path).is_absolute() else tasks_path),
        "generation_engine": "llm_evolutionary",
        "llm_config": {
            "model": cfg.model,
            "api_key_env": cfg.api_key_env,
            "num_seeds": cfg.num_seeds,
            "samples_per_op": cfg.samples_per_op,
            "top_n": cfg.top_n,
            "temperature": cfg.temperature,
            "reasoning_effort": cfg.reasoning_effort,
            "budget_ms": cfg.budget_ms,
            "seed": cfg.seed,
            "priors": cfg.priors,
        },
        "cost": _aggregate_costs(results),
        "robustness_oracle": {
            "browser": (
                "defect = severe console error OR non-informative (blank/crashed) frame OR "
                "soft-lock (no actionable UI and no open dialogue) on the injected mutated state; "
                "rejected = harness could not build the mutated state (no report)."
            ),
            "engine": (
                "defect = engine synthesis/restore error on the injected mutated state "
                "(engine_synthesis_error); robust = engine accepted or clamped it."
            ),
            "native_warm_session": (
                "defect = the native warm session (OpenRA / jynew) reports engine_error "
                "for the injected candidate AFTER a clean-process restart + canonical "
                "control replay confirms it (never on the first, possibly-poisoned, "
                "signal); rejected = malformed candidate or unconfirmed engine signal; "
                "robust = the native session accepts, clamps, or gracefully fails the "
                "candidate (e.g. jynew's own caught synthesis exception)."
            ),
        },
        "results": results,
        "summary": _summarize_fuzz(results),
    }
    run_context.stamp_payload(payload, run_meta=run_meta, experiment_id="state_fuzzing", git_root=base)
    return payload


def _load_completed_results(fuzz_json: Path) -> tuple[list[dict[str, Any]], set[str]]:
    """Load prior successful task results from an existing fuzz.json (for --resume)."""

    if not fuzz_json.exists():
        return [], set()
    try:
        prior = json.loads(fuzz_json.read_text(encoding="utf-8"))
    except Exception:
        return [], set()
    done: list[dict[str, Any]] = []
    done_ids: set[str] = set()
    for record in prior.get("results") or []:
        if isinstance(record, dict) and record.get("status") == "success":
            done.append(record)
            done_ids.add(str(record.get("task_id")))
    return done, done_ids


def run_fuzz_benchmark(
    root: str | Path = Path.cwd(),
    *,
    tasks_path: str | Path,
    headless: bool = False,
    report_dir: Path | None = None,
    run_meta: dict[str, Any] | None = None,
    config: FuzzConfig | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    cfg = config or FuzzConfig()
    _preflight_llm(cfg)  # hard-fail before any work if the LLM is unavailable
    out_dir = Path(report_dir) if report_dir is not None else new_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    fuzz_json = out_dir / "fuzz.json"
    tasks = load_state_loop_tasks(base, tasks_path)
    # Stamp run metadata once so incremental writes share a stable run_context/run_id.
    if run_meta is None:
        run_meta = run_context.run_metadata(experiment_id="state_fuzzing", git_root=base)

    # Resume: carry over already-completed tasks and skip re-running them, so an
    # aborted long run (or a deliberate chunked run) never loses finished work.
    results: list[dict[str, Any]] = []
    done_ids: set[str] = set()
    if cfg.resume:
        results, done_ids = _load_completed_results(fuzz_json)
        if done_ids:
            print(f"[fuzz] resume: {len(done_ids)} task(s) already complete, will skip them", flush=True)

    print(f"[fuzz] report_dir={out_dir} tasks={len(tasks)} model={cfg.model}", flush=True)
    for position, task in enumerate(tasks, start=1):
        if task.id in done_ids:
            print(f"[fuzz] ({position}/{len(tasks)}) {task.benchmark}/{task.id}: skipped (resume)", flush=True)
            continue
        print(f"[fuzz] ({position}/{len(tasks)}) {task.benchmark}/{task.id}", flush=True)
        task_result = _run_fuzz_task(base, task, out_dir=out_dir, headless=headless, config=cfg)
        status = task_result.get("status")
        if status == "success":
            meta = task_result.get("llm_meta") or {}
            print(
                f"[fuzz]   {task.id}: success | injected={meta.get('candidates_injected', 0)} "
                f"generations={meta.get('generations', 0)} "
                f"defects={sum(1 for m in task_result.get('mutations', []) if m.get('verdict') == 'defect')}",
                flush=True,
            )
        else:
            # Never silent: surface why a task did not run (LLM error, unsupported
            # profile, setup failure) directly on the console, not only in fuzz.json.
            print(f"[fuzz]   {task.id}: {status} | reason: {task_result.get('reason', '')}", flush=True)
        results.append(task_result)
        # Persist after every task so an abort/kill keeps all completed work; a
        # later --resume run continues from here instead of starting over.
        try:
            write_json(fuzz_json, _build_fuzz_payload(base, tasks_path, cfg, results, run_meta))
        except Exception as error:  # never let a checkpoint write failure kill the run
            print(f"[fuzz]   warning: incremental fuzz.json write failed: {error}", flush=True)

    payload = _build_fuzz_payload(base, tasks_path, cfg, results, run_meta)
    write_json(fuzz_json, payload)
    return {**payload, "report_dir": str(out_dir)}


def _make_fuzzer(task: Any, config: FuzzConfig) -> LLMStateFuzzer:
    import random

    from .fuzz_priors import resolve_priors_arg

    rng = random.Random(config.seed) if config.seed is not None else random.Random()
    # Single entry point for priors: FuzzConfig -> resolve/normalize -> fuzzer.
    root = Path(__file__).resolve().parents[1]
    try:
        priors = resolve_priors_arg(task.benchmark, config.priors, root=root)
    except Exception as error:
        raise RuntimeError(f"failed to load fuzz priors ({config.priors!r}): {error}") from error
    return LLMStateFuzzer(
        benchmark=task.benchmark,
        model=config.model,
        api_key_env=config.api_key_env,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        rng=rng,
        priors=priors,
    )


def _budget_s(task: Any, config: FuzzConfig) -> float:
    budget_ms = config.budget_ms if config.budget_ms is not None else task.issue.budget_ms
    return max(0.0, float(budget_ms) / 1000.0)


def _run_fuzz_task(base: Path, task: Any, *, out_dir: Path, headless: bool, config: FuzzConfig) -> dict[str, Any]:
    task_dir = out_dir / "tasks" / task.id
    result: dict[str, Any] = {"task_id": task.id, "benchmark": task.benchmark, "mutations": []}
    try:
        repo = resolve_task_repo(base, task.issue)
        seed_spec = load_spec((base / task.oracle_spec).resolve())
        seed_target = _seed_target_of(seed_spec)
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"task setup failed: {error}"
        return result

    if task.benchmark in {"luanti", "minetest"}:
        return _run_fuzz_luanti(base, task, repo, seed_spec, seed_target, result, task_dir, config)

    if task.benchmark == "SpaceRPG":
        return _run_fuzz_spacerpg(base, task, repo, seed_spec, result, task_dir, headless, config)

    if str(task.benchmark).lower() == "openra":
        return _run_fuzz_openra(task, repo, seed_spec, result, task_dir, config)

    if str(task.benchmark).lower() == "jynew":
        return _run_fuzz_jynew(task, repo, seed_spec, result, task_dir, config)

    try:
        session = StateSession(repo, task_dir / "session", headless=headless).start()
    except StateManagementUnsupported as error:
        result["status"] = "unsupported"
        result["reason"] = str(error)
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"session bootstrap failed: {error}"
        return result
    try:
        spec_path = task_dir / "seed.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        import yaml

        def inject_fn(candidate_target: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = candidate_target
            try:
                spec_path.write_text(
                    yaml.safe_dump(cand_spec, sort_keys=False, allow_unicode=False), encoding="utf-8"
                )
                outcome = session.restore(cand_spec, spec_path, actions=[], screenshot_name=screenshot_name)
            except Exception as error:  # unbuildable mutated state -> rejected, not a defect
                return InjectResult(verdict="rejected", detail=f"restore raised: {error}"[:300])
            verdict = _robustness_verdict(outcome)
            return InjectResult(
                verdict=verdict.verdict,
                defect_type=verdict.defect_type,
                detail=verdict.detail,
                screenshot=verdict.screenshot,
            )

        def make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
            record = {
                "mutation": operator_id,
                "verdict": result.verdict,
                "defect_type": result.defect_type,
                "detail": result.detail,
                "screenshot": result.screenshot,
                "note": OPERATOR_NOTES.get(operator_id, provenance),
                "defect_path": DEFECT_PATH_BROWSER,
                "operator_id": operator_id,
                "provenance": provenance,
                "generation": generation,
                "fitness": fitness,
            }
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = target
            _attach_fingerprint(record, benchmark=task.benchmark, task_id=task.id, mutation_spec=cand_spec)
            return record

        _run_evo_into_result(task, seed_target, inject_fn, make_record, result, config)
        return result
    except Exception as error:  # unexpected LLM/generation failure: surface, don't fabricate
        result["status"] = "failed"
        result["reason"] = f"llm fuzz failed: {error}"
        return result
    finally:
        session.close()


def _run_fuzz_luanti(
    base: Path,
    task: Any,
    repo: Path,
    seed_spec: dict[str, Any],
    seed_target: dict[str, Any],
    result: dict[str, Any],
    task_dir: Path,
    config: FuzzConfig,
) -> dict[str, Any]:
    from .luanti_state import LuantiWarmSession

    git_commit = run_context.git_commit(base)
    defects_dir = task_dir / "defects"

    def _dump_luanti_defect(record: dict[str, Any], target: dict[str, Any]) -> None:
        """Persist a self-contained, LLM-independent repro artifact for one defect.

        The exact injected deep-state (the precise mutated ``target``), the engine
        error detail, and provenance are written under ``tasks/<task>/defects/
        <fingerprint>/state.json`` so the defect can be reproduced later by a
        standalone luantiserver harness, without re-running the fuzzer. Keyed by
        fingerprint so identical unique defects share one artifact.
        """

        if record.get("verdict") != "defect":
            return
        fingerprint = record.get("fingerprint") or "unknown"
        dump_dir = defects_dir / str(fingerprint)
        dump_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "benchmark": task.benchmark,
            "task_id": task.id,
            "defect_path": DEFECT_PATH_ENGINE,
            "defect_type": record.get("defect_type"),
            "detail": record.get("detail"),
            "operator_id": record.get("operator_id"),
            "mutation": record.get("mutation"),
            "fingerprint": fingerprint,
            "oracle_spec": task.oracle_spec,
            "git_commit": git_commit,
            "target": target,
        }
        (dump_dir / "state.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    try:
        session = LuantiWarmSession(
            repo, task_dir / "session", render=False, allow_headless_player_skip=True,
            # Non-persistent map backend: an adversarial mutated state (extreme
            # coordinates / huge fill regions) must not balloon map.sqlite and
            # exhaust disk. Defects (unknown node, malformed item, validation
            # mismatch, engine hang/crash) are unaffected by where the map lives.
            map_backend="dummy",
        ).start()
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"luanti warm session failed: {error}"
        return result
    try:
        def inject_fn(candidate_target: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = candidate_target
            # If a prior candidate hard-crashed the engine, bring a fresh server up so
            # THIS candidate is tested against a live engine instead of being falsely
            # blamed for the earlier crash (which would cascade one defect into many).
            if not session.is_alive():
                try:
                    session.restart()
                except Exception as error:
                    return InjectResult(verdict="rejected", detail=f"warm session restart failed: {error}"[:300])
            try:
                # 30s (not 120s): a malformed state that hangs the engine should be
                # bounded quickly so cascading timeouts don't dominate the budget.
                outcome = session.restore(cand_spec, actions=[], assertions=[], timeout_s=30.0)
            except Exception as error:
                # restore() itself raised: a genuine engine/synthesis crash for THIS
                # candidate.
                return InjectResult(verdict="defect", defect_type="engine_synthesis_error", detail=str(error)[:200])
            if outcome.status == "success":
                return InjectResult(verdict="robust", detail=outcome.detail[:200])

            detail = outcome.detail or ""
            detail_low = detail.lower()

            # Request-level malformed JSON is a WarpTest/worldmod protocol rejection,
            # not evidence that a player-reachable Luanti state crashed the engine.
            if "malformed request" in detail_low or "request json missing spec" in detail_low:
                return InjectResult(verdict="rejected", detail=detail[:200])

            # (1) Cascade / liveness: the engine hung or died, so we never obtained a
            # clean verdict for THIS candidate (it is often poisoned by an earlier
            # crash). Grade it "rejected" (no clean judgement), not an engine defect,
            # and restart so the next candidate meets a live engine instead of
            # inheriting the failure.
            if (
                "timed out" in detail_low
                or "warm session is not running" in detail_low
                or "warm session exited" in detail_low
                or not session.is_alive()
            ):
                try:
                    session.restart()
                except Exception:
                    pass
                return InjectResult(verdict="rejected", detail=detail[:200])

            # (2) Assertion / validation mismatch: the engine ACCEPTED the mutated
            # state; the only failing checks are target.validate.* (the mutated state
            # is expected to diverge from the seed's validations). That is engine-robust,
            # not a defect.
            failing = [c for c in outcome.checks if str(c.get("status")) != "success"]
            only_validate = bool(failing) and all(
                str(c.get("name", "")).startswith("target.validate") for c in failing
            )
            if only_validate or (not failing and "one or more checks failed" in detail_low):
                return InjectResult(verdict="robust", detail=detail[:200])

            # (3) A real synthesis/engine crash: "Checkpoint processing crashed",
            # "Checkpoint restoration failed", or a non-validate check (e.g.
            # target.state_synthesized) failed. This is a genuine engine defect.
            return InjectResult(verdict="defect", defect_type="engine_synthesis_error", detail=detail[:200])

        def make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
            record = {
                "mutation": operator_id,
                "verdict": result.verdict,
                "defect_type": result.defect_type,
                "detail": result.detail,
                "note": OPERATOR_NOTES.get(operator_id, provenance),
                "defect_path": DEFECT_PATH_ENGINE,
                "operator_id": operator_id,
                "provenance": provenance,
                "generation": generation,
                "fitness": fitness,
            }
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = target
            _attach_fingerprint(record, benchmark=task.benchmark, task_id=task.id, mutation_spec=cand_spec)
            _dump_luanti_defect(record, target)
            return record

        _run_evo_into_result(task, seed_target, inject_fn, make_record, result, config)
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"llm fuzz failed: {error}"
        return result
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# OpenRA fuzz: fixed-map native warm session + bounded gameplay state
# --------------------------------------------------------------------------- #

def _openra_candidate_key(candidate: Any) -> str:
    """Stable in-memory key used to attach native evidence to evo records."""

    try:
        encoded = json.dumps(candidate, sort_keys=True, ensure_ascii=False, default=str, allow_nan=True)
    except Exception:
        encoded = repr(candidate)
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()


def _openra_outcome_payload(outcome: OpenRAWarmOutcome | None) -> dict[str, Any] | None:
    return outcome.to_dict() if outcome is not None else None


def _openra_defect_type(category: str | None) -> str:
    suffix = re.sub(r"[^a-z0-9_]+", "_", str(category or "error").lower()).strip("_") or "error"
    return f"openra_engine_{suffix[:48]}"


def _copy_openra_artifact_file(
    source: str | Path | None,
    destination: Path,
    *,
    session: OpenRAWarmSession,
) -> bool:
    """Copy an engine artifact if its reported path resolves inside known roots."""

    if not source:
        return False
    source_path = Path(source).expanduser()
    candidates = [source_path] if source_path.is_absolute() else [
        session.report_dir / source_path,
        session.repo / source_path,
    ]
    allowed_roots = (session.report_dir.resolve(), session.repo.resolve())
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            continue
        if not resolved.is_file():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, destination)
            return True
        except OSError:
            return False
    return False


def _write_openra_repro(
    dump_dir: Path,
    *,
    session: OpenRAWarmSession,
    canonical_state: dict[str, Any],
    fuzz_state: dict[str, Any],
) -> None:
    """Write a fuzzer/LLM-independent native C3 replay for one defect.

    This deliberately still uses the OpenRA test injection hook, so the artifact
    is labelled ``injection-only``.  It does not import WarpTest or reproduce an
    LLM mutation run: it starts the pinned map, first proves the canonical
    control/reset sequence, then publishes the exact candidate and reports
    whether the native engine emits ``engine_error`` or fails reset-ready.
    """

    canonical_request = {"version": C3_SESSION_VERSION, "sequence": 1, "fuzz_state": canonical_state}
    request = {"version": C3_SESSION_VERSION, "sequence": 2, "fuzz_state": fuzz_state}
    default_support_dir = Path(getattr(session, "support_dir", session.repo / ".warptest-c3-support"))
    (dump_dir / "canonical-request.json.pending").write_text(
        json.dumps(canonical_request, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (dump_dir / "request.json.pending").write_text(
        json.dumps(request, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    # All interpolated values are passed through the same public schema before a
    # defect is persisted, so mod/map are safe identifiers and the request lives in
    # the artifact directory rather than the run's mutable session directory.
    script = f"""#!/usr/bin/env bash
# Standalone OpenRA C3 injection-only reproduction. No WarpTest Python module or
# LLM is used; it verifies canonical control/reset before replaying the candidate.
# Point WARPTEST_OPENRA_REPO at a built checkout if this artifact was moved.
set -euo pipefail

HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
DEFAULT_REPO={json.dumps(str(session.repo))}
DEFAULT_SUPPORT_DIR={json.dumps(str(default_support_dir))}
OPENRA_REPO="${{WARPTEST_OPENRA_REPO:-$DEFAULT_REPO}}"
SUPPORT_DIR="${{WARPTEST_OPENRA_SUPPORT_DIR:-$DEFAULT_SUPPORT_DIR}}"
REQUEST="$HERE/request.json"
REPORT="$HERE/report.json"
READY="$HERE/ready.json"
SCREENSHOTS="$HERE/screenshots"
LOG="$HERE/openra.stdout.log"
ERR="$HERE/openra.stderr.log"
TIMEOUT_SECONDS="${{WARPTEST_OPENRA_REPRO_TIMEOUT_S:-180}}"
# Keep standalone replay compatible with the runner on hosts where the installed
# SDK is newer than OpenRA's target runtime.
export DOTNET_ROLL_FORWARD="${{DOTNET_ROLL_FORWARD:-Major}}"

rm -f "$REQUEST" "$REPORT" "$READY" "$LOG" "$ERR"
mkdir -p "$SCREENSHOTS"
"$OPENRA_REPO/launch-game.sh" \\
  "Game.Mod={session.mod}" \\
  "Engine.SupportDir=$SUPPORT_DIR" \\
  "Launch.Map={session.map_id}" \\
  "Launch.WarptestFuzzRequestPath=$REQUEST" \\
  "Launch.WarptestFuzzReportPath=$REPORT" \\
  "Launch.WarptestFuzzReadyPath=$READY" \\
  "Launch.WarptestFuzzScreenshotDir=$SCREENSHOTS" \\
  "Game.FetchNews=false" "Game.EnableDiscordService=false" "Sound.Mute=true" \\
  "Server.AdvertiseOnline=false" "Server.AdvertiseOnLocalNetwork=false" \\
  >"$LOG" 2>"$ERR" &
PID=$!
cleanup() {{
  if kill -0 "$PID" 2>/dev/null; then kill "$PID" 2>/dev/null || true; fi
  wait "$PID" 2>/dev/null || true
}}
trap cleanup EXIT

deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if [[ -f "$READY" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*0' "$READY" \\
     && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' "$READY"; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "OpenRA exited before initial ready; see $LOG and $ERR" >&2
    exit 2
  fi
  sleep 0.1
done
if (( SECONDS >= deadline )); then
  echo "Timed out waiting for initial ready; see $LOG and $ERR" >&2
  exit 2
fi

# Require an independent clean-session canonical control before the candidate.
deadline=$((SECONDS + TIMEOUT_SECONDS))
cp "$HERE/canonical-request.json.pending" "$HERE/request.json.tmp"
mv "$HERE/request.json.tmp" "$REQUEST"
CONTROL_REPORT_SEEN=0
while (( SECONDS < deadline )); do
  if [[ -f "$REPORT" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*1' "$REPORT"; then
    CONTROL_REPORT_SEEN=1
    cp "$REPORT" "$HERE/control-replay-report.json" || true
    if ! grep -Eq '"status"[[:space:]]*:[[:space:]]*"(success|robust|accepted|ok)"' "$REPORT"; then
      cat "$REPORT"
      echo "Canonical control did not pass; see $LOG and $ERR" >&2
      exit 2
    fi
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "OpenRA exited before canonical control report; see $LOG and $ERR" >&2
    exit 2
  fi
  sleep 0.1
done
if (( ! CONTROL_REPORT_SEEN )); then
  echo "Timed out waiting for canonical control report; see $LOG and $ERR" >&2
  exit 2
fi

# A canonical report is not sufficient: it must reach reset-ready before the
# exact candidate may be replayed.
CONTROL_READY=0
while (( SECONDS < deadline )); do
  if [[ -f "$READY" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*1' "$READY" \\
     && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' "$READY"; then
    CONTROL_READY=1
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "OpenRA exited before canonical reset-ready; see $LOG and $ERR" >&2
    exit 2
  fi
  sleep 0.1
done
if (( ! CONTROL_READY )); then
  echo "Timed out waiting for canonical reset-ready; see $LOG and $ERR" >&2
  exit 2
fi

# Replay the exact confirmed candidate on the same warm, clean session.
deadline=$((SECONDS + TIMEOUT_SECONDS))
cp "$HERE/request.json.pending" "$HERE/request.json.tmp"
mv "$HERE/request.json.tmp" "$REQUEST"
REPORT_SEEN=0
while (( SECONDS < deadline )); do
  if [[ -f "$REPORT" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*2' "$REPORT"; then
    REPORT_SEEN=1
    cat "$REPORT"
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"engine_error"' "$REPORT"; then
      echo "REPRODUCED: native OpenRA engine_error"
      exit 0
    fi
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "REPRODUCED: OpenRA exited before candidate report; see $LOG and $ERR" >&2
    exit 0
  fi
  sleep 0.1
done
if (( ! REPORT_SEEN )); then
  echo "REPRODUCED: timed out waiting for candidate report; see $LOG and $ERR" >&2
  exit 0
fi

# A candidate report is not a clean-session result by itself: C3 classifies an
# otherwise successful candidate as an engine defect when reset-ready never arrives.
deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if [[ -f "$READY" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*2' "$READY" \\
     && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' "$READY"; then
    echo "NOT REPRODUCED: candidate completed and reset-ready arrived" >&2
    exit 1
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "REPRODUCED: OpenRA exited before candidate reset-ready; see $LOG and $ERR" >&2
    exit 0
  fi
  sleep 0.1
done
echo "REPRODUCED: candidate report arrived but reset-ready did not; see $LOG and $ERR" >&2
exit 0
"""
    repro = dump_dir / "run.sh"
    repro.write_text(script, encoding="utf-8")
    repro.chmod(0o755)


def _dump_openra_defect(
    *,
    task: Any,
    task_dir: Path,
    session: OpenRAWarmSession,
    record: dict[str, Any],
    canonical_state: dict[str, Any],
    fuzz_state: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    """Persist exact C3 candidate + native evidence keyed by unique fingerprint."""

    if record.get("verdict") != "defect":
        return
    fingerprint = str(record.get("fingerprint") or "unknown")
    dump_dir = task_dir / "defects" / fingerprint
    dump_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(session, "flush_logs"):
        session.flush_logs()

    state = {
        "benchmark": task.benchmark,
        "task_id": task.id,
        "defect_path": DEFECT_PATH_ENGINE,
        "defect_type": record.get("defect_type"),
        "detail": record.get("detail"),
        "operator_id": record.get("operator_id"),
        "mutation": record.get("mutation"),
        "fingerprint": fingerprint,
        "oracle_spec": task.oracle_spec,
        "git_commit": session.git_commit,
        "fixed_mod": session.mod,
        "fixed_map": session.map_id,
        "protocol": C3_SESSION_VERSION,
        "reachability": OPENRA_REACHABILITY,
        "canonical_fuzz_state": canonical_state,
        "fuzz_state": fuzz_state,
        "initial_outcome": evidence.get("initial"),
        "control_outcome": evidence.get("control"),
        "replay_outcome": evidence.get("replay"),
    }
    (dump_dir / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )

    replay = evidence.get("replay") or evidence.get("initial")
    if isinstance(replay, dict) and isinstance(replay.get("report"), dict) and replay["report"]:
        (dump_dir / "native-report.json").write_text(
            json.dumps(replay["report"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    else:
        # A hard crash can pre-empt the engine's atomic report write. Keep an
        # explicit client-side record at the conventional artifact path so every
        # accepted defect still carries its observed native-session evidence.
        (dump_dir / "native-report.json").write_text(
            json.dumps(
                {
                    "source": "warptest-client-outcome-no-native-report",
                    "protocol": C3_SESSION_VERSION,
                    "outcome": replay,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    control = evidence.get("control")
    if isinstance(control, dict) and isinstance(control.get("report"), dict):
        (dump_dir / "control-report.json").write_text(
            json.dumps(control["report"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    _copy_openra_artifact_file(session.stdout_path, dump_dir / "openra-warm.stdout.log", session=session)
    _copy_openra_artifact_file(session.stderr_path, dump_dir / "openra-warm.stderr.log", session=session)
    # A hard process exit may prevent the replay candidate from producing a frame.
    # Preserve the most recent candidate/control frame in that case rather than
    # leaving a defect artifact without any visual evidence.
    for screenshot_outcome in (replay, evidence.get("initial"), control):
        if isinstance(screenshot_outcome, dict) and _copy_openra_artifact_file(
            screenshot_outcome.get("screenshot_path"), dump_dir / "screenshot.png", session=session
        ):
            break
    _write_openra_repro(
        dump_dir,
        session=session,
        canonical_state=canonical_state,
        fuzz_state=fuzz_state,
    )


def _run_fuzz_openra(
    task: Any,
    repo: Path,
    seed_spec: dict[str, Any],
    result: dict[str, Any],
    task_dir: Path,
    config: FuzzConfig,
) -> dict[str, Any]:
    """Run C3 over a fixed OpenRA gameplay map without reusing oracle assertions."""

    try:
        target = seed_spec.get("target") if isinstance(seed_spec.get("target"), dict) else {}
        mod = target.get("mod")
        map_id = target.get("map")
        if not isinstance(mod, str) or not isinstance(map_id, str):
            raise OpenRAFuzzStateError("OpenRA C3 gameplay spec requires target.mod and target.map")
        canonical = project_openra_fuzz_state(seed_spec)
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"OpenRA C3 task setup failed: {error}"
        return result

    # Each mutated action has its own native liveness deadline.  It remains bounded
    # by the gameplay spec's normal timeout instead of inheriting the entire LLM
    # generation budget.
    timeout_ms = seed_spec.get("timeout_ms", 90_000)
    try:
        candidate_timeout_s = min(180.0, max(15.0, float(timeout_ms) / 1000.0))
    except (TypeError, ValueError):
        candidate_timeout_s = 90.0

    try:
        session = OpenRAWarmSession(repo, task_dir / "session", mod=mod, map_id=map_id).start()
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"OpenRA warm session bootstrap failed: {error}"
        return result

    evidence_by_candidate: dict[str, dict[str, Any]] = {}
    restart_before_next = [False]

    def _remember(candidate: Any, evidence: dict[str, Any]) -> None:
        evidence_by_candidate[_openra_candidate_key(candidate)] = evidence

    def _as_inject(outcome: OpenRAWarmOutcome) -> InjectResult:
        if outcome.status == "robust":
            return InjectResult(verdict="robust", detail=outcome.detail[:500])
        if outcome.status == "rejected":
            return InjectResult(verdict="rejected", detail=outcome.detail[:500])
        return InjectResult(
            verdict="defect",
            defect_type=_openra_defect_type(outcome.category),
            detail=outcome.detail[:500],
        )

    try:
        # A known-good native request is a hard precondition.  It exercises the
        # initial ready -> request -> reset-ready handshake before LLM work begins.
        control = session.run(canonical, timeout_s=candidate_timeout_s)
        if control.status != "robust":
            result["status"] = "failed"
            result["reason"] = (
                "OpenRA canonical control did not pass: "
                f"{control.category or control.status}: {control.detail}"
            )
            return result

        def inject_fn(candidate: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
            del operator_id, screenshot_name  # The native C3 protocol owns screenshots, if any.
            try:
                fuzz_state = sanitize_openra_fuzz_state(candidate)
            except OpenRAFuzzStateError as error:
                _remember(candidate, {"schema_error": str(error), "fuzz_state": None})
                return InjectResult(verdict="rejected", detail=f"OpenRA fuzz-state rejected: {error}"[:500])

            evidence: dict[str, Any] = {"fuzz_state": fuzz_state}
            if restart_before_next[0] or not session.is_alive():
                try:
                    session.restart()
                    restart_before_next[0] = False
                except Exception as error:
                    evidence["restart_error"] = str(error)
                    _remember(candidate, evidence)
                    return InjectResult(verdict="rejected", detail=f"OpenRA session restart failed: {error}"[:500])

            initial = session.run(fuzz_state, timeout_s=candidate_timeout_s)
            evidence["initial"] = _openra_outcome_payload(initial)
            if initial.status != "engine_error":
                _remember(candidate, evidence)
                return _as_inject(initial)

            # An engine-looking signal is not sufficient evidence: restart to a
            # clean process, prove the canonical action set still works, and only
            # then replay the exact same candidate.  This prevents a broken harness
            # or poisoned previous map from becoming a false OpenRA defect.
            restart_before_next[0] = True
            try:
                session.restart()
                restart_before_next[0] = False
            except Exception as error:
                evidence["restart_error"] = str(error)
                _remember(candidate, evidence)
                return InjectResult(
                    verdict="rejected", detail=f"unconfirmed OpenRA engine signal: restart failed: {error}"[:500]
                )
            control_outcome = session.run(canonical, timeout_s=candidate_timeout_s)
            evidence["control"] = _openra_outcome_payload(control_outcome)
            if control_outcome.status != "robust":
                restart_before_next[0] = True
                _remember(candidate, evidence)
                return InjectResult(
                    verdict="rejected",
                    detail=(
                        "unconfirmed OpenRA engine signal: canonical control did not pass: "
                        f"{control_outcome.detail}"
                    )[:500],
                )
            replay = session.run(fuzz_state, timeout_s=candidate_timeout_s)
            evidence["replay"] = _openra_outcome_payload(replay)
            if replay.status == "engine_error":
                restart_before_next[0] = True
                _remember(candidate, evidence)
                return _as_inject(replay)

            # The original signal was transient or harness-related.  It cannot be
            # called robust (the first run did not have a trustworthy verdict), and
            # it cannot be called a defect because it failed clean confirmation.
            if replay.status != "robust":
                restart_before_next[0] = True
            _remember(candidate, evidence)
            return InjectResult(
                verdict="rejected",
                detail=(
                    "unconfirmed OpenRA engine signal did not reproduce; replay returned "
                    f"{replay.status}: {replay.detail}"
                )[:500],
            )

        def make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
            evidence = evidence_by_candidate.get(_openra_candidate_key(target), {})
            native = evidence.get("replay") or evidence.get("initial")
            native_category = native.get("category") if isinstance(native, dict) else None
            record = {
                "mutation": operator_id,
                "verdict": result.verdict,
                "defect_type": result.defect_type,
                "detail": result.detail,
                "note": OPERATOR_NOTES.get(operator_id, provenance),
                "defect_path": DEFECT_PATH_ENGINE,
                "operator_id": operator_id,
                "provenance": provenance,
                "generation": generation,
                "fitness": fitness,
                "injection_mode": OPENRA_INJECTION_MODE,
                "reachability": OPENRA_REACHABILITY,
                "native_category": native_category,
            }
            cand_spec = {"target": target}
            _attach_fingerprint(record, benchmark=task.benchmark, task_id=task.id, mutation_spec=cand_spec)
            if record.get("verdict") == "defect":
                # Defect results always originated from a sanitized state; retain a
                # defensive copy in case a custom fuzzer/test bypassed that invariant.
                fuzz_state = evidence.get("fuzz_state")
                if isinstance(fuzz_state, dict):
                    _dump_openra_defect(
                        task=task,
                        task_dir=task_dir,
                        session=session,
                        record=record,
                        canonical_state=canonical,
                        fuzz_state=fuzz_state,
                        evidence=evidence,
                    )
            return record

        _run_evo_into_result(task, canonical, inject_fn, make_record, result, config)
        llm_meta = result.get("llm_meta")
        if not isinstance(llm_meta, dict):
            llm_meta = {}
            result["llm_meta"] = llm_meta
        llm_meta["openra_c3"] = {
            "protocol": C3_SESSION_VERSION,
            "fixed_mod": session.mod,
            "fixed_map": session.map_id,
            "bootstrap_ms": session.bootstrap_ms,
            "canonical_control": "passed",
            "per_candidate_baseline": "native_reset_ready",
            "reachability": OPENRA_REACHABILITY,
        }
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"OpenRA C3 fuzz failed: {error}"
        return result
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# jynew fuzz: synthesized-state target + native Unity warm session
# --------------------------------------------------------------------------- #

def _jynew_candidate_key(candidate: Any) -> str:
    """Stable in-memory key used to attach native evidence to evo records."""

    try:
        encoded = json.dumps(candidate, sort_keys=True, ensure_ascii=False, default=str, allow_nan=True)
    except Exception:
        encoded = repr(candidate)
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()


def _jynew_defect_type(category: str | None) -> str:
    suffix = re.sub(r"[^a-z0-9_]+", "_", str(category or "error").lower()).strip("_") or "error"
    return f"jynew_engine_{suffix[:48]}"


def _write_jynew_repro(
    dump_dir: Path,
    *,
    session: JynewWarmSession,
    canonical_state: dict[str, Any],
    fuzz_state: dict[str, Any],
) -> None:
    """Write a fuzzer/LLM-independent native C3 replay for one jynew defect.

    Starts a fresh ``RunWarm`` process, first proves the canonical control
    candidate, then publishes the exact defect candidate and reports whether the
    native warm session emits ``engine_error`` for it.
    """

    def _request(sequence: int, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": JYNEW_C3_SESSION_VERSION,
            "sequence": sequence,
            "spec": {
                "target": build_jynew_target(state, mod_id=session.mod_id),
                "validations": [],
                "actions": [],
                "assertions": [],
            },
        }

    canonical_request = _request(1, canonical_state)
    request = _request(2, fuzz_state)
    project_path = find_jynew_project_path(session.repo)
    (dump_dir / "canonical-request.json.pending").write_text(
        json.dumps(canonical_request, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (dump_dir / "request.json.pending").write_text(
        json.dumps(request, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    script = f"""#!/usr/bin/env bash
# Standalone jynew C3 injection-only reproduction. No WarpTest Python module or
# LLM is used; it verifies the canonical control candidate before replaying the
# exact candidate that produced this defect. Point UNITY_EDITOR_PATH / WARPTEST_
# JYNEW_PROJECT_PATH elsewhere if this artifact was moved off the original checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
DEFAULT_PROJECT={json.dumps(str(project_path))}
PROJECT_PATH="${{WARPTEST_JYNEW_PROJECT_PATH:-$DEFAULT_PROJECT}}"
UNITY_EXE="${{UNITY_EDITOR_PATH:-Unity}}"
REQUEST="$HERE/request.json"
REPORT="$HERE/report.json"
READY="$HERE/ready.json"
LOG="$HERE/jynew-warm.stdout.log"
ERR="$HERE/jynew-warm.stderr.log"
TIMEOUT_SECONDS="${{WARPTEST_JYNEW_REPRO_TIMEOUT_S:-180}}"

rm -f "$REQUEST" "$REPORT" "$READY" "$LOG" "$ERR"
"$UNITY_EXE" -batchmode -nographics \\
  -projectPath "$PROJECT_PATH" \\
  -executeMethod Jyx2.WarptestCheckpoint.RunWarm \\
  -logFile "$HERE/unity-editor-warm.log" \\
  -- \\
  --warptest-warm-request "$REQUEST" \\
  --warptest-warm-report "$REPORT" \\
  --warptest-warm-ready "$READY" \\
  >"$LOG" 2>"$ERR" &
PID=$!
cleanup() {{
  if kill -0 "$PID" 2>/dev/null; then kill "$PID" 2>/dev/null || true; fi
  wait "$PID" 2>/dev/null || true
}}
trap cleanup EXIT

deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if [[ -f "$READY" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*0' "$READY" \\
     && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' "$READY"; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "jynew warm session exited before initial ready; see $LOG and $ERR" >&2
    exit 2
  fi
  sleep 0.2
done
if (( SECONDS >= deadline )); then
  echo "Timed out waiting for initial ready; see $LOG and $ERR" >&2
  exit 2
fi

deadline=$((SECONDS + TIMEOUT_SECONDS))
cp "$HERE/canonical-request.json.pending" "$HERE/request.json.tmp"
mv "$HERE/request.json.tmp" "$REQUEST"
CONTROL_SEEN=0
while (( SECONDS < deadline )); do
  if [[ -f "$REPORT" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*1' "$REPORT"; then
    CONTROL_SEEN=1
    cp "$REPORT" "$HERE/control-replay-report.json" || true
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"(rejected|engine_error)"' "$REPORT"; then
      cat "$REPORT"
      echo "Canonical control did not pass; see $LOG and $ERR" >&2
      exit 2
    fi
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "jynew warm session exited before canonical control report; see $LOG and $ERR" >&2
    exit 2
  fi
  sleep 0.2
done
if (( ! CONTROL_SEEN )); then
  echo "Timed out waiting for canonical control report; see $LOG and $ERR" >&2
  exit 2
fi

deadline=$((SECONDS + TIMEOUT_SECONDS))
cp "$HERE/request.json.pending" "$HERE/request.json.tmp"
mv "$HERE/request.json.tmp" "$REQUEST"
while (( SECONDS < deadline )); do
  if [[ -f "$REPORT" ]] && grep -Eq '"sequence"[[:space:]]*:[[:space:]]*2' "$REPORT"; then
    cat "$REPORT"
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"engine_error"' "$REPORT"; then
      echo "REPRODUCED: native jynew engine_error"
      exit 0
    fi
    echo "NOT REPRODUCED: candidate completed without engine_error" >&2
    exit 1
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "REPRODUCED: jynew warm session exited before candidate report; see $LOG and $ERR" >&2
    exit 0
  fi
  sleep 0.2
done
echo "REPRODUCED: timed out waiting for candidate report; see $LOG and $ERR" >&2
exit 0
"""
    repro = dump_dir / "run.sh"
    repro.write_text(script, encoding="utf-8")
    repro.chmod(0o755)


def _dump_jynew_defect(
    *,
    task: Any,
    task_dir: Path,
    session: JynewWarmSession,
    record: dict[str, Any],
    canonical_state: dict[str, Any],
    fuzz_state: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    """Persist exact C3 candidate + native evidence keyed by unique fingerprint."""

    if record.get("verdict") != "defect":
        return
    fingerprint = str(record.get("fingerprint") or "unknown")
    dump_dir = task_dir / "defects" / fingerprint
    dump_dir.mkdir(parents=True, exist_ok=True)
    session.flush_logs()

    state = {
        "benchmark": task.benchmark,
        "task_id": task.id,
        "defect_path": DEFECT_PATH_ENGINE,
        "defect_type": record.get("defect_type"),
        "detail": record.get("detail"),
        "operator_id": record.get("operator_id"),
        "mutation": record.get("mutation"),
        "fingerprint": fingerprint,
        "oracle_spec": task.oracle_spec,
        "git_commit": session.git_commit,
        "mod_id": session.mod_id,
        "protocol": JYNEW_C3_SESSION_VERSION,
        "reachability": JYNEW_REACHABILITY,
        "canonical_fuzz_state": canonical_state,
        "fuzz_state": fuzz_state,
        "initial_outcome": evidence.get("initial"),
        "control_outcome": evidence.get("control"),
        "replay_outcome": evidence.get("replay"),
    }
    (dump_dir / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    for name, path in (
        ("jynew-warm.stdout.log", session.stdout_path),
        ("jynew-warm.stderr.log", session.stderr_path),
    ):
        try:
            if path.exists():
                shutil.copyfile(path, dump_dir / name)
        except OSError:
            pass
    _write_jynew_repro(dump_dir, session=session, canonical_state=canonical_state, fuzz_state=fuzz_state)


def _run_fuzz_jynew(
    task: Any,
    repo: Path,
    seed_spec: dict[str, Any],
    result: dict[str, Any],
    task_dir: Path,
    config: FuzzConfig,
) -> dict[str, Any]:
    """Run C3 over jynew's synthesized-state target via a native Unity warm session."""

    try:
        mod_id = jynew_target_mod_id(seed_spec)
        canonical = project_jynew_fuzz_state(seed_spec)
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"jynew C3 task setup failed: {error}"
        return result

    # Each mutated candidate has its own native liveness deadline, bounded by the
    # oracle spec's own timeout rather than inheriting the whole LLM generation
    # budget.
    timeout_ms = seed_spec.get("timeout_ms", 120_000)
    try:
        candidate_timeout_s = min(180.0, max(15.0, float(timeout_ms) / 1000.0))
    except (TypeError, ValueError):
        candidate_timeout_s = 60.0

    try:
        session = JynewWarmSession(repo, task_dir / "session", mod_id=mod_id).start()
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"jynew warm session bootstrap failed: {error}"
        return result

    evidence_by_candidate: dict[str, dict[str, Any]] = {}
    restart_before_next = [False]

    def _remember(candidate: Any, evidence: dict[str, Any]) -> None:
        evidence_by_candidate[_jynew_candidate_key(candidate)] = evidence

    def _as_inject(outcome: JynewWarmOutcome) -> InjectResult:
        if outcome.status == "robust":
            return InjectResult(verdict="robust", detail=outcome.detail[:500])
        if outcome.status == "rejected":
            return InjectResult(verdict="rejected", detail=outcome.detail[:500])
        return InjectResult(
            verdict="defect",
            defect_type=_jynew_defect_type(outcome.category),
            detail=outcome.detail[:500],
        )

    try:
        # A known-good native request is a hard precondition: it exercises the
        # initial ready -> request -> ready handshake before LLM work begins.
        control = session.run(canonical, timeout_s=candidate_timeout_s)
        if control.status != "robust":
            result["status"] = "failed"
            result["reason"] = (
                "jynew canonical control did not pass: "
                f"{control.category or control.status}: {control.detail}"
            )
            return result

        def inject_fn(candidate: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
            del operator_id, screenshot_name  # jynew's C3 protocol has no screenshots.
            try:
                fuzz_state = sanitize_jynew_fuzz_state(candidate)
            except JynewFuzzStateError as error:
                _remember(candidate, {"schema_error": str(error), "fuzz_state": None})
                return InjectResult(verdict="rejected", detail=f"jynew fuzz-state rejected: {error}"[:500])

            evidence: dict[str, Any] = {"fuzz_state": fuzz_state}
            if restart_before_next[0] or not session.is_alive():
                try:
                    session.restart()
                    restart_before_next[0] = False
                except Exception as error:
                    evidence["restart_error"] = str(error)
                    _remember(candidate, evidence)
                    return InjectResult(verdict="rejected", detail=f"jynew session restart failed: {error}"[:500])

            initial = session.run(fuzz_state, timeout_s=candidate_timeout_s)
            evidence["initial"] = initial.to_dict()
            if initial.status != "engine_error":
                _remember(candidate, evidence)
                return _as_inject(initial)

            # An engine-looking signal is not sufficient evidence: restart to a
            # clean process, prove the canonical candidate still passes, and only
            # then replay the exact same candidate. This prevents a broken harness
            # or a poisoned prior process from becoming a false jynew defect.
            restart_before_next[0] = True
            try:
                session.restart()
                restart_before_next[0] = False
            except Exception as error:
                evidence["restart_error"] = str(error)
                _remember(candidate, evidence)
                return InjectResult(
                    verdict="rejected", detail=f"unconfirmed jynew engine signal: restart failed: {error}"[:500]
                )
            control_outcome = session.run(canonical, timeout_s=candidate_timeout_s)
            evidence["control"] = control_outcome.to_dict()
            if control_outcome.status != "robust":
                restart_before_next[0] = True
                _remember(candidate, evidence)
                return InjectResult(
                    verdict="rejected",
                    detail=(
                        "unconfirmed jynew engine signal: canonical control did not pass: "
                        f"{control_outcome.detail}"
                    )[:500],
                )
            replay = session.run(fuzz_state, timeout_s=candidate_timeout_s)
            evidence["replay"] = replay.to_dict()
            if replay.status == "engine_error":
                restart_before_next[0] = True
                _remember(candidate, evidence)
                return _as_inject(replay)

            # The original signal was transient or harness-related: it cannot be
            # called robust (the first run had no trustworthy verdict), and it
            # cannot be called a defect because it failed clean confirmation.
            if replay.status != "robust":
                restart_before_next[0] = True
            _remember(candidate, evidence)
            return InjectResult(
                verdict="rejected",
                detail=(
                    "unconfirmed jynew engine signal did not reproduce; replay returned "
                    f"{replay.status}: {replay.detail}"
                )[:500],
            )

        def make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
            evidence = evidence_by_candidate.get(_jynew_candidate_key(target), {})
            native = evidence.get("replay") or evidence.get("initial")
            native_category = native.get("category") if isinstance(native, dict) else None
            record = {
                "mutation": operator_id,
                "verdict": result.verdict,
                "defect_type": result.defect_type,
                "detail": result.detail,
                "note": OPERATOR_NOTES.get(operator_id, provenance),
                "defect_path": DEFECT_PATH_ENGINE,
                "operator_id": operator_id,
                "provenance": provenance,
                "generation": generation,
                "fitness": fitness,
                "injection_mode": JYNEW_INJECTION_MODE,
                "reachability": JYNEW_REACHABILITY,
                "native_category": native_category,
            }
            cand_spec = {"target": target}
            _attach_fingerprint(record, benchmark=task.benchmark, task_id=task.id, mutation_spec=cand_spec)
            if record.get("verdict") == "defect":
                fuzz_state = evidence.get("fuzz_state")
                if isinstance(fuzz_state, dict):
                    _dump_jynew_defect(
                        task=task,
                        task_dir=task_dir,
                        session=session,
                        record=record,
                        canonical_state=canonical,
                        fuzz_state=fuzz_state,
                        evidence=evidence,
                    )
            return record

        _run_evo_into_result(task, canonical, inject_fn, make_record, result, config)
        llm_meta = result.get("llm_meta")
        if not isinstance(llm_meta, dict):
            llm_meta = {}
            result["llm_meta"] = llm_meta
        llm_meta["jynew_c3"] = {
            "protocol": JYNEW_C3_SESSION_VERSION,
            "mod_id": session.mod_id,
            "bootstrap_ms": session.bootstrap_ms,
            "canonical_control": "passed",
            "reachability": JYNEW_REACHABILITY,
        }
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"jynew C3 fuzz failed: {error}"
        return result
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# SpaceRPG fuzz: rich save seed + multi-kind injection (C3 parity with 2048)
# --------------------------------------------------------------------------- #

def _resolve_spacerpg_kinds(config: FuzzConfig) -> tuple[str, ...]:
    """Normalize the configured SpaceRPG inject set to a concrete ordered kind tuple."""

    requested = config.spacerpg_inject
    if not requested:
        return SPACERPG_ALL_KINDS
    expanded: list[str] = []
    for token in requested:
        low = str(token).strip().lower()
        if low in {"all", ""}:
            expanded.extend(SPACERPG_ALL_KINDS)
        elif low == "both":  # back-compat: original two kinds
            expanded.extend(["save_restore", "state_overlay"])
        elif low in SPACERPG_ALL_KINDS:
            expanded.append(low)
        else:
            raise ValueError(f"Unknown SpaceRPG inject kind: {token!r} (choices: {', '.join(SPACERPG_ALL_KINDS)}, all, both)")
    # de-dup preserving canonical order
    return tuple(k for k in SPACERPG_ALL_KINDS if k in set(expanded))


def _curate_spacerpg_save(save: dict[str, Any]) -> dict[str, Any]:
    """Keep gameplay-relevant SaveGameData fields; drop bulky/regenerable ones."""

    return {key: save[key] for key in SPACERPG_SEED_KEEP_FIELDS if key in save}


def _canonical_spacerpg_actions(seed_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """A deterministic canonical store-action sequence to seed action_replay."""

    target = seed_spec.get("target", {}) if isinstance(seed_spec.get("target"), dict) else {}
    station = target.get("station_id", "helion-prime")
    system = target.get("system_id", "helion-reach")
    return [
        {"method": "newGame", "args": []},
        {"method": "dockAt", "args": [station]},
        {"method": "openGalaxyMap", "args": ["gate"]},
        {"method": "jumpToSystem", "args": [system]},
        {"method": "saveGame", "args": ["auto"]},
    ]


def _spacerpg_seed_cache_path(base: Path, git_commit: str | None, oracle_spec: str) -> Path:
    key = hashlib.sha256(f"{git_commit}\u0001{oracle_spec}".encode("utf-8")).hexdigest()[:16]
    cache_dir = base / ".warptest-cache" / "spacerpg-fuzz-seeds"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.json"


def capture_spacerpg_seed(session: StateSession, seed_spec: dict[str, Any], seed_spec_path: Path) -> dict[str, Any]:
    """Reach the spec's good state on a warm session and capture a curated save + actions.

    Drives the game's own ``saveGame('auto')`` then reads the persisted slot JSON,
    so the seed is a real ``SaveGameData`` (the source of truth), not a synthetic
    guess. Raises on failure so a run never fuzzes from a fabricated seed.
    """

    outcome = session.restore(seed_spec, seed_spec_path, actions=[], screenshot_name="spacerpg-seed-capture.png")
    if outcome.status != "success":
        raise RuntimeError(f"SpaceRPG seed capture restore failed: {outcome.detail}")
    raw = session.runtime.page.evaluate(
        """() => {
          const hook = window.__SPACERPG_E2E__;
          if (!hook || typeof hook.getState !== 'function') return null;
          const store = hook.getState();
          store.stopEconomyStream?.();
          store.saveGame?.("auto");
          try { return localStorage.getItem("spacerpg-save-slot:auto"); }
          catch (e) { return null; }
        }"""
    )
    if not raw:
        raise RuntimeError("SpaceRPG seed capture: save slot empty after saveGame('auto').")
    save = json.loads(raw)
    if not isinstance(save, dict) or "player" not in save:
        raise RuntimeError("SpaceRPG seed capture: captured save is not a SaveGameData object.")
    return {"seed_save": _curate_spacerpg_save(save), "seed_actions": _canonical_spacerpg_actions(seed_spec)}


def _load_or_capture_spacerpg_seed(
    base: Path,
    session: StateSession,
    seed_spec: dict[str, Any],
    seed_spec_path: Path,
    oracle_spec: str,
    *,
    refresh: bool,
    log,
) -> dict[str, Any]:
    cache_path = _spacerpg_seed_cache_path(base, session.git_commit, oracle_spec)
    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached.get("seed_save"), dict) and cached["seed_save"]:
                log(f"seed cache hit: {cache_path.name}")
                cached.setdefault("seed_actions", _canonical_spacerpg_actions(seed_spec))
                return cached
        except Exception:
            pass  # corrupt cache -> recapture
    log("capturing seed save from warm session...")
    seed = capture_spacerpg_seed(session, seed_spec, seed_spec_path)
    payload = {
        "git_commit": session.git_commit,
        "oracle_spec": oracle_spec,
        "seed_save": seed["seed_save"],
        "seed_actions": seed["seed_actions"],
    }
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"seed captured and cached: {cache_path.name}")
    return seed


_SPACERPG_REPRO_SCRIPT = r'''#!/usr/bin/env python3
"""Standalone SpaceRPG C3 defect reproduction.

No WarpTest Python module or LLM is used. This script starts the game's own
Vite dev server, replays the exact captured injection kind/target recorded in
the sibling ``state.json``, and independently re-derives the robustness
verdict (console errors / blank render / soft-lock) so the fingerprinted
defect can be confirmed without re-running the fuzzer.

Requirements: `pip install playwright && playwright install chromium`, plus
the SpaceRPG repo's own `npm install` having been run once.

Env overrides:
  WARPTEST_SPACERPG_REPO       point at a different checkout if this artifact was
                            moved away from the repo it was captured against
                            (defaults to the path recorded in state.json).
  WARPTEST_SPACERPG_HEADLESS=1 hide the browser window (default: headed/visible).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Mirrors warptest.fuzzing._GRACEFUL_RECOVERY_RE / _is_graceful_recovery_console: a
# console error fully explained by an ErrorBoundary that already caught and recovered
# from the render error is not, on its own, evidence of a defect (the blank-render /
# soft-lock checks below test the ACTUAL post-recovery screen instead).
GRACEFUL_RECOVERY_RE = re.compile(
    r"recovered from an unexpected render error|error boundary you provided", re.I,
)

HERE = Path(__file__).resolve().parent
STATE = json.loads((HERE / "state.json").read_text(encoding="utf-8"))
TARGET = STATE["target"]
DEFAULT_REPO = STATE.get("repo") or "."
REPO = Path(os.environ.get("WARPTEST_SPACERPG_REPO", DEFAULT_REPO)).expanduser().resolve()

SOFT_LOCK_JS = """() => {
  const s = window.__SPACERPG_E2E__.getState();
  const canvas = document.querySelector("canvas");
  const rect = canvas?.getBoundingClientRect();
  const actionableButtons = [...document.querySelectorAll("button")].filter((button) => !button.disabled).length;
  return {
    screen: s.screen,
    game_over_screen: s.screen === "gameover",
    dialogue_open: !!s.dialogueState?.active,
    canvas_present: !!canvas,
    canvas_size: { width: rect?.width ?? 0, height: rect?.height ?? 0 },
    actionable_buttons: actionableButtons,
    runtime_message: s.runtime?.message ?? null
  };
}"""

REACH_FLIGHT_JS = """() => {
  const store = window.__SPACERPG_E2E__.getState();
  store.newGame?.();
  store.closeDialogue?.();
  store.stopEconomyStream?.();
}"""

REACH_STATION_JS = """(stationId) => {
  const store = window.__SPACERPG_E2E__.getState();
  store.newGame?.();
  store.closeDialogue?.();
  store.stopEconomyStream?.();
  store.dockAt?.(stationId);
  window.__SPACERPG_E2E__.getState().closeDialogue?.();
}"""

LOAD_GAME_JS = """(slot) => {
  const store = window.__SPACERPG_E2E__.getState();
  store.stopEconomyStream?.();
  const ok = store.loadGame ? store.loadGame(slot ?? undefined) : false;
  window.__SPACERPG_E2E__.getState().closeDialogue?.();
  return ok === undefined ? true : ok;
}"""

INJECT_LIVE_STATE_JS = """({ state, replace, persistedKeys }) => {
  const hook = window.__SPACERPG_E2E__;
  const cur = hook.getState();
  cur.stopEconomyStream?.();
  cur.closeDialogue?.();
  if (replace === true) {
    const next = { ...hook.getState() };
    for (const k of persistedKeys) delete next[k];
    Object.assign(next, state);
    hook.setState(next, true);
  } else {
    hook.setState(state, false);
  }
}"""

DRIVE_ACTIONS_JS = """(actions) => {
  const hook = window.__SPACERPG_E2E__;
  let invoked = 0;
  for (const action of actions) {
    if (!action || typeof action !== 'object') continue;
    const store = hook.getState();
    const fn = store[action.method];
    if (typeof fn !== 'function') continue;
    const args = Array.isArray(action.args) ? action.args : [];
    try { fn(...args); invoked += 1; }
    catch (e) { /* adversarial action threw; keep going */ }
  }
  hook.getState().closeDialogue?.();
  return invoked;
}"""

SET_STORAGE_JS = """({ values, clearKeys }) => {
  for (const k of clearKeys) window.localStorage.removeItem(k);
  for (const [k, v] of Object.entries(values)) window.localStorage.setItem(k, v);
}"""

_PERSISTED_SAVE_KEYS = [
    "version", "savedAt", "screen", "currentSystemId", "currentStationId", "gameClock",
    "player", "activeMissions", "completedMissionIds", "failedMissionIds", "reputation",
    "factionHeat", "knownSystems", "knownPlanetIds", "explorationState", "dialogueState",
    "onboardingState",
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"dev server did not respond at {url} within {timeout_s}s: {last_error}")


def start_dev_server(port: int) -> subprocess.Popen:
    vite_js = REPO / "node_modules" / "vite" / "bin" / "vite.js"
    if vite_js.exists():
        cmd = ["node", str(vite_js), "--host", "127.0.0.1", "--port", str(port)]
    else:
        cmd = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)]
    # Redirect to a file instead of PIPE: an unread PIPE fills (~64KB) and
    # blocks the child on write, so Vite can stall before binding the port.
    log_path = HERE / "dev-server.log"
    log = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT, text=True)
    finally:
        log.close()  # child keeps its own fd; parent must not leak the handle
    try:
        _wait_http(f"http://127.0.0.1:{port}/", timeout_s=90.0)
    except Exception as error:
        proc.terminate()
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:].strip()
        except Exception:
            tail = ""
        raise RuntimeError(
            f"{error}. See {log_path}." + (f" Log tail:\n{tail}" if tail else "")
        ) from error
    return proc


def _save_index_for(slot: str, save: dict, *, corrupt: bool) -> dict:
    player = save.get("player") if isinstance(save.get("player"), dict) else {}
    summary = {
        "id": slot,
        "label": slot,
        "exists": True,
        "savedAt": save.get("savedAt") or "2020-01-01T00:00:00.000Z",
        "screen": save.get("screen"),
        "currentSystemId": save.get("currentSystemId"),
        "currentStationId": save.get("currentStationId"),
        "credits": player.get("credits"),
        "gameClock": save.get("gameClock"),
        "version": 3,
    }
    index = {"version": 3, "slots": {slot: summary}, "lastPlayedSlotId": slot}
    if corrupt:
        index["slots"]["__garbage_slot__"] = {"unexpected": True, "exists": "yes"}
        summary["credits"] = "not-a-number"
        summary["gameClock"] = None
        index["extraTopLevelGarbage"] = [1, 2, {"nested": None}]
    return index


def screenshot_informative(path: Path) -> bool:
    data = path.read_bytes()
    if len(data) < 2_000 or len(set(data)) < 32:
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            extrema = image.convert("RGB").getextrema()
            pixel_range = max(high - low for low, high in extrema)
        return pixel_range >= 8
    except Exception:
        return True  # Pillow unavailable: fall back to the byte-diversity heuristic above.


def reach_base(page, base) -> None:
    kind = base.get("kind") if isinstance(base, dict) else None
    if kind == "station":
        page.evaluate(REACH_STATION_JS, base.get("station_id", "helion-prime"))
    else:
        page.evaluate(REACH_FLIGHT_JS)


def main() -> int:
    headless = os.environ.get("WARPTEST_SPACERPG_HEADLESS", "0") == "1"
    port = _find_free_port()
    kind = TARGET.get("kind")
    print(f"[repro] repo={REPO} port={port} kind={kind}")
    server = start_dev_server(port)
    console_errors: list = []
    screenshot_path = HERE / "repro-screenshot.png"
    soft_lock = None
    rejected = False
    detail = ""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                init_values = {"spacerpg-e2e-hook": "enabled", "spacerpg-e2e-disable-economy-backend": "true"}
                context.add_init_script(
                    "(() => { const v = %s; for (const [k, val] of Object.entries(v)) "
                    "window.localStorage.setItem(k, val); })();" % json.dumps(init_values)
                )
                page = context.new_page()
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.goto(f"http://127.0.0.1:{port}/?warptest=1", wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_function("() => !!window.__SPACERPG_E2E__", timeout=30_000)

                if kind in ("fuzz_save_restore", "fuzz_legacy_save", "fuzz_save_index"):
                    save = TARGET["save"]
                    slot = str(TARGET.get("slot", "auto"))
                    serialized = json.dumps(save)
                    preserve = {"spacerpg-e2e-hook": "enabled", "spacerpg-e2e-disable-economy-backend": "true"}
                    if kind == "fuzz_legacy_save":
                        values = {**preserve, "spacerpg-save": serialized}
                        clear = ["spacerpg-save-index"]
                    else:
                        corrupt = kind == "fuzz_save_index"
                        values = {
                            **preserve,
                            f"spacerpg-save-slot:{slot}": serialized,
                            "spacerpg-save-index": json.dumps(_save_index_for(slot, save, corrupt=corrupt)),
                        }
                        clear = []
                    page.evaluate(SET_STORAGE_JS, {"values": values, "clearKeys": clear})
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => typeof window.__SPACERPG_E2E__?.getState === 'function'", timeout=30_000
                    )
                    slot_arg = None if kind in ("fuzz_legacy_save", "fuzz_save_index") else slot
                    loaded = page.evaluate(LOAD_GAME_JS, slot_arg)
                    if not loaded:
                        rejected = True
                        detail = "loadGame() refused the mutated save (normalizeSave/index rejected it)."
                elif kind in ("fuzz_state_overlay", "fuzz_replace_state"):
                    reach_base(page, TARGET.get("base"))
                    persisted_keys = TARGET.get("persisted_keys") or _PERSISTED_SAVE_KEYS
                    page.evaluate(
                        INJECT_LIVE_STATE_JS,
                        {
                            "state": TARGET["save"],
                            "replace": kind == "fuzz_replace_state",
                            "persistedKeys": persisted_keys,
                        },
                    )
                elif kind == "fuzz_action_replay":
                    reach_base(page, TARGET.get("base"))
                    page.evaluate(DRIVE_ACTIONS_JS, TARGET.get("actions") or [])
                else:
                    print(f"[repro] unsupported kind: {kind}", file=sys.stderr)
                    return 2

                if not rejected:
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(screenshot_path))
                    soft_lock = page.evaluate(SOFT_LOCK_JS)
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()

    graceful_recovery = bool(console_errors) and any(
        GRACEFUL_RECOVERY_RE.search(str(c)) for c in console_errors
    )
    if rejected:
        verdict = "rejected"
    elif console_errors and not graceful_recovery:
        verdict = "defect"
        detail = "; ".join(console_errors[:3])
    elif screenshot_path.exists() and not screenshot_informative(screenshot_path):
        verdict = "defect"
        detail = "non-informative (blank/crashed) frame"
    elif isinstance(soft_lock, dict) and soft_lock.get("actionable_buttons") == 0 and soft_lock.get("dialogue_open") is not True:
        verdict = "defect"
        detail = "no actionable UI (soft lock)"
    else:
        verdict = "robust"

    report = {
        "verdict": verdict,
        "detail": detail,
        "console_errors": console_errors,
        "soft_lock_signal": soft_lock,
        "screenshot": str(screenshot_path) if screenshot_path.exists() else None,
        "expected_defect_type": STATE.get("defect_type"),
        "expected_detail": STATE.get("detail"),
    }
    (HERE / "repro-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if verdict == "defect":
        print("REPRODUCED: independent replay confirms the defect")
        return 0
    print(f"NOT REPRODUCED: independent replay verdict = {verdict}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_spacerpg_repro(dump_dir: Path) -> None:
    """Write a standalone Playwright/Python reproduction script for one SpaceRPG defect.

    Reads its target/repo from the sibling ``state.json`` written by
    ``_dump_spacerpg_defect``, so the script itself is a static, reusable template
    that needs no WarpTest import and no LLM to replay the exact injection.
    """

    repro = dump_dir / "repro.py"
    repro.write_text(_SPACERPG_REPRO_SCRIPT, encoding="utf-8")
    repro.chmod(0o755)


def _run_fuzz_spacerpg(
    base: Path,
    task: Any,
    repo: Path,
    seed_spec: dict[str, Any],
    result: dict[str, Any],
    task_dir: Path,
    headless: bool,
    config: FuzzConfig,
) -> dict[str, Any]:
    """SpaceRPG fuzz: rich captured-save seed injected through up to 6 kinds.

    The primary (save-restore) verdict drives the evolutionary loop/MAB; every
    other enabled state-kind re-injects the SAME candidate as a shadow record, and
    ``action_replay`` runs a separate small pass over a store-action sequence. Only
    the primary + action passes consume LLM tokens; shadows only add browser time.
    """

    import yaml

    enabled = _resolve_spacerpg_kinds(config)
    save_kinds = [k for k in SPACERPG_SAVE_KINDS if k in enabled]
    primary = "save_restore" if "save_restore" in save_kinds else (save_kinds[0] if save_kinds else None)
    shadow_kinds = [k for k in save_kinds if k != primary]
    run_action = SPACERPG_ACTION_KIND in enabled
    if primary is None and not run_action:
        result["status"] = "failed"
        result["reason"] = "SpaceRPG fuzz: no injection kinds enabled."
        return result

    try:
        session = StateSession(repo, task_dir / "session", headless=headless).start()
    except StateManagementUnsupported as error:
        result["status"] = "unsupported"
        result["reason"] = str(error)
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"session bootstrap failed: {error}"
        return result

    def _log(message: str) -> None:
        print(f"[fuzz]   {task.id}: {message}", flush=True)

    try:
        spec_path = task_dir / "seed.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        seed_spec_path = task_dir / "spacerpg-seed-oracle.yaml"
        seed_spec_path.write_text(yaml.safe_dump(seed_spec, sort_keys=False, allow_unicode=False), encoding="utf-8")

        seed = _load_or_capture_spacerpg_seed(
            base, session, seed_spec, seed_spec_path, task.oracle_spec, refresh=config.refresh_seeds, log=_log
        )
        seed_save = seed["seed_save"]
        seed_actions = seed.get("seed_actions") or _canonical_spacerpg_actions(seed_spec)
        base_params = seed_spec.get("target", {}) if isinstance(seed_spec.get("target"), dict) else {}
        _log(
            f"enabled kinds={list(enabled)} primary={primary} shadows={shadow_kinds} "
            f"action_replay={'on' if run_action else 'off'}"
        )

        defects_dir = task_dir / "defects"

        def _wrap_save_target(candidate: dict[str, Any], kind: str) -> dict[str, Any]:
            return {
                "kind": SPACERPG_KIND_TO_TARGET[kind],
                "save": candidate,
                "base": base_params,
                "persisted_keys": list(SPACERPG_SEED_KEEP_FIELDS),
            }

        def _dump_spacerpg_defect(
            *,
            screenshot_name: str,
            kind: str,
            operator_id: str,
            inj: InjectResult,
            candidate_target: dict[str, Any],
            outcome: Any,
        ) -> None:
            """Persist a self-contained, LLM-independent repro artifact for one defect.

            The exact injected deep-state (candidate_target, incl. the precise save/
            base/actions), the full console errors, and the captured frame are written
            under ``tasks/<task>/defects/<screenshot-stem>/`` so a defect can be
            replayed later without re-running the fuzzer.
            """

            if inj.verdict != "defect":
                return
            dump_dir = defects_dir / Path(screenshot_name).stem
            dump_dir.mkdir(parents=True, exist_ok=True)
            console: list[Any] = []
            report = getattr(outcome, "report", None)
            if isinstance(report, dict):
                console = report.get("console_errors") or []
            state = {
                "benchmark": task.benchmark,
                "task_id": task.id,
                "injection_mode": kind,
                "operator_id": operator_id,
                "defect_type": inj.defect_type,
                "detail": inj.detail,
                "target": candidate_target,
                "console_errors": console,
                "oracle_spec": task.oracle_spec,
                "git_commit": session.git_commit,
                "repo": str(session.repo),
                "screenshot_name": screenshot_name,
            }
            (dump_dir / "state.json").write_text(
                json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            shot = getattr(outcome, "screenshot", None)
            if shot and Path(shot).exists():
                import shutil

                shutil.copyfile(shot, dump_dir / "screenshot.png")
            _write_spacerpg_repro(dump_dir)

        def _inject_target(candidate_target: dict[str, Any], screenshot_name: str) -> tuple[InjectResult, Any]:
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = candidate_target
            try:
                spec_path.write_text(
                    yaml.safe_dump(cand_spec, sort_keys=False, allow_unicode=False), encoding="utf-8"
                )
                outcome = session.restore(cand_spec, spec_path, actions=[], screenshot_name=screenshot_name)
            except Exception as error:  # unbuildable mutated state -> rejected, not a defect
                return InjectResult(verdict="rejected", detail=f"restore raised: {error}"[:300]), None
            verdict = _robustness_verdict(outcome)
            return (
                InjectResult(
                    verdict=verdict.verdict,
                    defect_type=verdict.defect_type,
                    detail=verdict.detail,
                    screenshot=verdict.screenshot,
                ),
                outcome,
            )

        def _build_record(
            candidate_target: dict[str, Any],
            kind: str,
            inj: InjectResult,
            *,
            operator_id: str,
            provenance: str,
            generation: Any,
            fitness_val: float,
        ) -> dict[str, Any]:
            record = {
                "mutation": operator_id,
                "verdict": inj.verdict,
                "defect_type": inj.defect_type,
                "detail": inj.detail,
                "screenshot": inj.screenshot,
                "note": OPERATOR_NOTES.get(operator_id, provenance),
                "defect_path": DEFECT_PATH_BROWSER,
                "operator_id": operator_id,
                "provenance": provenance,
                "generation": generation,
                "fitness": fitness_val,
                "injection_mode": kind,
            }
            cand_spec = copy.deepcopy(seed_spec)
            cand_spec["target"] = candidate_target
            _attach_fingerprint(record, benchmark=task.benchmark, task_id=task.id, mutation_spec=cand_spec)
            return record

        shadow_records: list[dict[str, Any]] = []
        shadow_counter = [0]

        mutations: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}

        with cost.scoped_meter() as task_meter:
            if primary is not None:
                fuzzer = _make_fuzzer(task, config)

                def primary_inject(candidate: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
                    primary_target = _wrap_save_target(candidate, primary)
                    primary_res, primary_outcome = _inject_target(primary_target, screenshot_name)
                    _dump_spacerpg_defect(
                        screenshot_name=screenshot_name, kind=primary, operator_id=operator_id,
                        inj=primary_res, candidate_target=primary_target, outcome=primary_outcome,
                    )
                    fit = fitness(candidate, seed_save)
                    if primary_res.verdict == "rejected":
                        return primary_res
                    for kind in shadow_kinds:
                        shadow_counter[0] += 1
                        sname = f"fuzz-{kind}-{operator_id}-{shadow_counter[0]}.png"
                        shadow_target = _wrap_save_target(candidate, kind)
                        sres, soutcome = _inject_target(shadow_target, sname)
                        _dump_spacerpg_defect(
                            screenshot_name=sname, kind=kind, operator_id=operator_id,
                            inj=sres, candidate_target=shadow_target, outcome=soutcome,
                        )
                        shadow_records.append(
                            _build_record(
                                shadow_target, kind, sres,
                                operator_id=operator_id, provenance="shadow", generation=None, fitness_val=fit,
                            )
                        )
                    return primary_res

                def primary_make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
                    return _build_record(
                        _wrap_save_target(target, primary), primary, result,
                        operator_id=operator_id, provenance=provenance, generation=generation, fitness_val=fitness,
                    )

                _log(f"primary evo over captured save (fields={list(seed_save)})...")
                primary_records, meta = run_evo_fuzz(
                    benchmark=task.benchmark,
                    seed_target=seed_save,
                    inject_fn=primary_inject,
                    fuzzer=fuzzer,
                    budget_s=_budget_s(task, config),
                    num_seeds=config.num_seeds,
                    samples_per_op=config.samples_per_op,
                    top_n=config.top_n,
                    make_record=primary_make_record,
                    log=lambda m: _log("  " + m),
                )
                mutations.extend(primary_records)
                mutations.extend(shadow_records)

            if run_action:
                action_fuzzer = _make_fuzzer(task, config)
                action_seed = {"actions": seed_actions}
                action_budget = max(10.0, _budget_s(task, config) * 0.4)
                action_num_seeds = min(2, config.num_seeds) if config.num_seeds else 2

                def action_inject(candidate: dict[str, Any], operator_id: str, screenshot_name: str) -> InjectResult:
                    actions = candidate.get("actions") if isinstance(candidate, dict) else None
                    target = {"kind": SPACERPG_KIND_TO_TARGET[SPACERPG_ACTION_KIND], "actions": actions, "base": base_params}
                    inj, outcome = _inject_target(target, screenshot_name)
                    _dump_spacerpg_defect(
                        screenshot_name=screenshot_name, kind=SPACERPG_ACTION_KIND, operator_id=operator_id,
                        inj=inj, candidate_target=target, outcome=outcome,
                    )
                    return inj

                def action_make_record(*, target, operator_id, provenance, generation, fitness, result) -> dict[str, Any]:
                    actions = target.get("actions") if isinstance(target, dict) else None
                    wrapped = {"kind": SPACERPG_KIND_TO_TARGET[SPACERPG_ACTION_KIND], "actions": actions, "base": base_params}
                    return _build_record(
                        wrapped, SPACERPG_ACTION_KIND, result,
                        operator_id=operator_id, provenance=provenance, generation=generation, fitness_val=fitness,
                    )

                _log(f"action_replay pass (budget={action_budget:.0f}s, seeds={action_num_seeds})...")
                action_records, action_meta = run_evo_fuzz(
                    benchmark=task.benchmark,
                    seed_target=action_seed,
                    inject_fn=action_inject,
                    fuzzer=action_fuzzer,
                    budget_s=action_budget,
                    num_seeds=action_num_seeds,
                    samples_per_op=config.samples_per_op,
                    top_n=config.top_n,
                    make_record=action_make_record,
                    log=lambda m: _log("  [action] " + m),
                )
                mutations.extend(action_records)
                if meta:
                    meta = {**meta, "action_replay": action_meta}
                else:
                    meta = {"action_replay": action_meta}

        result["mutations"] = mutations
        result["llm_meta"] = {
            **meta,
            "spacerpg_inject_kinds": list(enabled),
            "spacerpg_primary": primary,
            "spacerpg_shadow_kinds": shadow_kinds,
            "seed_save_fields": list(seed_save),
        }
        result["cost"] = task_meter.summary()
        result["status"] = "success"
        return result
    except Exception as error:  # unexpected LLM/generation/capture failure: surface
        result["status"] = "failed"
        result["reason"] = f"spacerpg fuzz failed: {error}"
        return result
    finally:
        session.close()


def _run_evo_into_result(
    task: Any,
    seed_target: dict[str, Any],
    inject_fn,
    make_record,
    result: dict[str, Any],
    config: FuzzConfig,
) -> None:
    """Drive the LLM evolutionary fuzz for one task, capturing per-task LLM cost."""

    fuzzer = _make_fuzzer(task, config)

    def _log(message: str) -> None:
        print(f"[fuzz]   {task.id}: {message}", flush=True)

    with cost.scoped_meter() as task_meter:
        records, meta = run_evo_fuzz(
            benchmark=task.benchmark,
            seed_target=seed_target,
            inject_fn=inject_fn,
            fuzzer=fuzzer,
            budget_s=_budget_s(task, config),
            num_seeds=config.num_seeds,
            samples_per_op=config.samples_per_op,
            top_n=config.top_n,
            make_record=make_record,
            log=_log,
        )
    result["mutations"] = records
    result["llm_meta"] = meta
    result["cost"] = task_meter.summary()
    result["status"] = "success"


def _attach_fingerprint(
    record: dict[str, Any],
    *,
    benchmark: Any,
    task_id: Any,
    mutation_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Stamp a defect record with its unique-defect fingerprint (defects only)."""

    if record.get("verdict") != "defect":
        return record
    record["fingerprint"] = defect_fingerprint(
        benchmark=benchmark,
        task_id=task_id,
        mutation=record.get("mutation"),
        defect_type=record.get("defect_type"),
        detail=record.get("detail"),
        mutated_state=mutation_spec,
        defect_path=record.get("defect_path") or "",
        screenshot=record.get("screenshot"),
    )
    record["mutated_state_signature"] = mutated_state_signature(mutation_spec)
    return record


def _summarize_fuzz(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize fuzz results.

    The main C3 metric is ``unique_defects`` (deduped by fingerprint across all
    runs/seeds); ``defects`` keeps the raw mutation count for the appendix, and
    ``robust`` / ``rejected`` stay as transparency counters. Defects are also
    split by ``defect_path`` (browser vs engine) because the two paths use
    different defect definitions and must not be silently merged.
    """

    summary: dict[str, Any] = {
        "total_tasks": len(results),
        "mutations_tested": 0,
        "defects": 0,
        "unique_defects": 0,
        "robust": 0,
        "rejected": 0,
        "defect_types": {},
        "unique_defect_types": {},
        "defect_paths": {DEFECT_PATH_BROWSER: 0, DEFECT_PATH_ENGINE: 0},
        "by_benchmark": {},
        # Per-injection-mode split (SpaceRPG's multi-kind fuzz). Each kind uses a
        # different injection path (e.g. save-restore vs replace_state), so their
        # verdicts must not be silently pooled.
        "by_injection_mode": {},
    }
    seen_global: set[str] = set()
    seen_by_type: dict[str, set[str]] = {}
    seen_by_bench: dict[str, set[str]] = {}
    seen_by_mode: dict[str, set[str]] = {}
    for result in results:
        bench = str(result.get("benchmark"))
        bucket = summary["by_benchmark"].setdefault(
            bench, {"mutations": 0, "defects": 0, "unique_defects": 0}
        )
        for mut in result.get("mutations", []) or []:
            summary["mutations_tested"] += 1
            bucket["mutations"] += 1
            mode = mut.get("injection_mode")
            mode_bucket = (
                summary["by_injection_mode"].setdefault(
                    str(mode),
                    {"mutations": 0, "robust": 0, "rejected": 0, "defects": 0, "unique_defects": 0},
                )
                if mode is not None
                else None
            )
            if mode_bucket is not None:
                mode_bucket["mutations"] += 1
            verdict = mut.get("verdict")
            if verdict == "defect":
                summary["defects"] += 1
                bucket["defects"] += 1
                if mode_bucket is not None:
                    mode_bucket["defects"] += 1
                dt = mut.get("defect_type") or "unknown"
                summary["defect_types"][dt] = summary["defect_types"].get(dt, 0) + 1
                path = mut.get("defect_path") or DEFECT_PATH_BROWSER
                summary["defect_paths"][path] = summary["defect_paths"].get(path, 0) + 1
                fingerprint = mut.get("fingerprint")
                if fingerprint and fingerprint not in seen_global:
                    seen_global.add(fingerprint)
                    summary["unique_defects"] += 1
                if fingerprint and fingerprint not in seen_by_bench.setdefault(bench, set()):
                    seen_by_bench[bench].add(fingerprint)
                    bucket["unique_defects"] += 1
                if fingerprint and fingerprint not in seen_by_type.setdefault(dt, set()):
                    seen_by_type[dt].add(fingerprint)
                    summary["unique_defect_types"][dt] = summary["unique_defect_types"].get(dt, 0) + 1
                if mode_bucket is not None and fingerprint and fingerprint not in seen_by_mode.setdefault(str(mode), set()):
                    seen_by_mode[str(mode)].add(fingerprint)
                    mode_bucket["unique_defects"] += 1
            elif verdict == "robust":
                summary["robust"] += 1
                if mode_bucket is not None:
                    mode_bucket["robust"] += 1
            elif verdict == "rejected":
                summary["rejected"] += 1
                if mode_bucket is not None:
                    mode_bucket["rejected"] += 1
    return summary
