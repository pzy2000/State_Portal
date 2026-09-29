from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from dataclasses import asdict
from pathlib import Path

from .core_experiment import run_core_multimodel_benchmark, run_core_synthesis_benchmark
from .discovery import RepositoryInspector
from .effort import build_effort_report
from .evo import (
    BROWSER_WEB_BENCHMARKS,
    CORE_BENCHMARKS,
    DEFAULT_MEMORY_BASELINE_BACKENDS,
    DEFAULT_PHASE3_SPLIT,
    MEMORY_POLICY_CHOICES,
    MUTATION_POLICY_CHOICES,
    TASK_SCOPE_CHOICES,
    run_evo_synthesis_benchmark,
    run_memory_baseline_suite,
    run_phase1_suite,
    run_phase3_suite,
)
from .memory_backends import MEMORY_BACKEND_CHOICES, MEMORY_SCOPE_CHOICES
from .gui import GenericGuiAppAdapter
from .model_bench import DEFAULT_MODELS, DEFAULT_MODELSCOPE_BASE_URL, load_task_file, run_model_benchmark
from .oracle_policy import ORACLE_POLICY_CHOICES
from .paper_tables import build_paper_tables, write_paper_tables
from .providers import resolve_model
from .reports import new_report_dir, write_inspect_report, write_json
from .runner import compare_benchmark_specs, compare_spec, load_spec, prepare_repo, resolve_repo, run_spec
from .segments import run_segment_bug_discovery
from .fuzzing import FuzzConfig, run_fuzz_benchmark
from .gui_agent import DEFAULT_GUI_AGENT_BASE_URL, DEFAULT_GUI_AGENT_MODEL, run_gui_agent_benchmark
from .p5_scale import ALL_ARMS, DEFAULT_MANIFEST, run_p5_scale
from .state_loop import STATE_FUZZ_TASKS, STATE_LOOP_TASKS, run_state_loop_benchmark
from .transition_explorer import DEFAULT_TRANSITION_MODEL, explore_transitions
from .visual import CASCADE_POLICY_CHOICES, VISUAL_CACHE_CHOICES, cascade_policy_config
from .visual_experiment import run_visual_experiment
from .hybrid_visual_calibration import run_hybrid_visual_phase
from .hook_onboarding import (
    DEFAULT_CREDENTIALS as DEFAULT_ONBOARDING_CREDENTIALS,
    DEFAULT_MANIFEST as DEFAULT_ONBOARDING_MANIFEST,
    load_manifest as load_onboarding_manifest,
    run_hook_onboarding,
    run_provider_preflight as run_onboarding_provider_preflight,
)


def _extract_utility_exit_code(metrics: object) -> int | None:
    if not isinstance(metrics, dict):
        return None
    for key, value in metrics.items():
        if key.endswith("_utility_exit_code") and value is not None:
            return value
    return None


def discover_bench_specs(specs_root: Path = Path("specs")) -> list[Path]:
    return sorted(specs_root.glob("*/*.yaml"))


def discover_bench_repos(spec_paths: list[Path]) -> list[Path]:
    repos: list[Path] = []
    seen: set[Path] = set()
    for spec_path in spec_paths:
        spec = load_spec(spec_path)
        repo = resolve_repo(spec, spec_path.resolve(), None)
        if repo not in seen:
            repos.append(repo)
            seen.add(repo)
    return repos


def split_cli_values(values: list[str] | None, default: list[str]) -> list[str]:
    if not values:
        return default
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def add_oracle_policy_args(parser: argparse.ArgumentParser, *, default_policy: str) -> None:
    parser.add_argument("--oracle-policy", choices=ORACLE_POLICY_CHOICES, default=None, help=f"Second-layer oracle policy. Defaults to {default_policy}.")
    parser.add_argument("--visual-sample-rate", type=float, default=0.0, help="Adaptive visual audit sample rate from 0.0 to 1.0.")
    parser.add_argument("--visual-on-semantic-failure", action="store_true", help="Run visual audit even when the semantic oracle already failed.")
    parser.add_argument("--cascade-policy", choices=CASCADE_POLICY_CHOICES, default=None, help="Visual audit execution policy. Defaults to full-only.")
    parser.add_argument("--visual-frame-sample-count", type=int, default=None, help="Frame-level sample count for selected visual audits.")
    parser.add_argument("--visual-frame-sample-interval-ms", type=int, default=None, help="Delay between visual audit frame samples.")
    parser.add_argument("--visual-cache", choices=VISUAL_CACHE_CHOICES, default=None, help="Visual audit cache mode.")


def resolved_oracle_policy(args: argparse.Namespace, *, default_policy: str) -> str:
    if getattr(args, "oracle_policy", None):
        return args.oracle_policy
    if getattr(args, "visual", False):
        return "visual"
    return default_policy


def resolved_cascade_config(args: argparse.Namespace):
    return cascade_policy_config(
        cascade_policy=getattr(args, "cascade_policy", None),
        sample_count=getattr(args, "visual_frame_sample_count", None),
        sample_interval_ms=getattr(args, "visual_frame_sample_interval_ms", None),
        visual_cache=getattr(args, "visual_cache", None),
    )


def _run_replay_bench_command(args: argparse.Namespace) -> dict[str, object]:
    from . import replay_bench

    if args.replay_command == "harvest":
        if args.index:
            indexes = [Path(item) for item in args.index]
        else:
            root = Path("reports/warptest")
            indexes = sorted(root.glob("*/p5-run-index.json")) + sorted(
                root.glob("*/p5/p5-run-index.json")
            )
        candidates, ledger = replay_bench.harvest_candidates(indexes, arm=args.arm)
        if not candidates:
            raise SystemExit(
                "no graded checkpoints were found; pass --index explicitly"
            )
        manifest_path = replay_bench.freeze_population(
            candidates, args.out, ledger=ledger, source_indexes=indexes
        )
        return {
            "population": str(manifest_path),
            "candidates": len(candidates),
            "scanned_rows": ledger["scanned_rows"],
            "rejected": ledger["rejected"],
        }

    if args.replay_command == "run":
        manifest = replay_bench.load_replay_manifest(args.manifest)
        report_dir = (
            Path(args.report_dir)
            if args.report_dir
            else new_report_dir() / "c4-replay"
        )
        summary = replay_bench.run_replay_bench(
            Path.cwd(),
            manifest=manifest,
            population_manifest=args.population,
            report_dir=report_dir,
            environment=args.environment,
            tasks_path=manifest.get("tasks", "benchmarks/p5/c1_tasks.yaml"),
            headless=not args.headed,
            repetitions=args.repetitions,
            benchmarks=args.benchmarks,
            resume=not args.no_resume,
        )
        return {
            "report_dir": str(report_dir),
            "checkpoints_replayed": summary["checkpoints_replayed"],
            "verdict_reproduction_rate": summary["verdict_reproduction_rate"],
            "flake_rate": summary["flake_rate"],
            "llm_calls_observed": summary["llm_calls_observed"],
        }

    manifest = replay_bench.load_replay_manifest(args.manifest)
    report_dirs = [Path(item).expanduser().resolve() for item in args.report_dir]
    report_dir = report_dirs[0]
    trials: list[dict] = []
    for directory in report_dirs:
        trials.extend(
            json.loads((directory / "replay-trials.json").read_text(encoding="utf-8"))
        )
    stats = manifest.get("stats") or {}
    summary = replay_bench.aggregate_trials(
        trials,
        bootstrap_iterations=int(stats.get("bootstrap_iterations") or 10000),
        rng_seed=int(stats.get("rng_seed") or 1234),
    )
    summary["manifest_hash"] = replay_bench.manifest_hash(manifest)
    summary["pooled_report_dirs"] = [str(item) for item in report_dirs]
    amortization = None
    if args.c1_summary:
        amortization = replay_bench.amortization_curve(
            summary,
            c1_token_costs=replay_bench.parse_c1_token_costs(args.c1_summary),
            max_runs=int((manifest.get("amortization") or {}).get("max_runs") or 20),
        )
        summary["amortization"] = amortization
        summary["amortization_source"] = str(Path(args.c1_summary).resolve())
    (report_dir / "replay-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (report_dir / "replay-summary.md").write_text(
        replay_bench.render_markdown(summary, amortization) + "\n", encoding="utf-8"
    )
    return {
        "report_dir": str(report_dir),
        "verdict_reproduction_rate": summary["verdict_reproduction_rate"],
        "flake_rate": summary["flake_rate"],
        "visual_identity_rate": summary["visual_identity_rate"],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="warptest", description="Semantic checkpoint smoke testing for source-available web games.")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect", help="Read-only repository capability inspection.")
    inspect_p.add_argument("repo")
    inspect_p.add_argument("--json", action="store_true", help="Print JSON to stdout.")

    run_p = sub.add_parser("run", help="Run a smoke spec against an explicit repository.")
    run_p.add_argument("spec")
    run_p.add_argument("--repo", help="Target repository path. Required unless spec declares repo or benchmark.")
    run_p.add_argument("--headed", action="store_true", help="Run browser headed.")
    add_oracle_policy_args(run_p, default_policy="semantic")

    explore_p = sub.add_parser("explore-transitions", help="Probe action-conditioned state transitions from a restored checkpoint.")
    explore_p.add_argument("spec")
    explore_p.add_argument("--repo", help="Target repository path. Required unless spec declares repo or benchmark.")
    explore_p.add_argument("--max-actions", type=int, default=8, help="Maximum candidate actions to probe. Defaults to 8.")
    explore_p.add_argument("--horizon", type=int, default=1, help="Transition depth to probe. Currently only horizon 1 is supported.")
    explore_p.add_argument("--model", default=DEFAULT_TRANSITION_MODEL, help="Model id for LLM action proposal.")
    explore_p.add_argument("--base-url", default=DEFAULT_MODELSCOPE_BASE_URL, help="OpenAI-compatible base URL for LLM action proposal.")
    explore_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY", help="Environment variable containing the LLM API key.")
    explore_p.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature for LLM action proposal.")
    explore_p.add_argument("--max-tokens", type=int, default=2048, help="Max tokens for the LLM action proposal.")
    explore_p.add_argument("--headed", action="store_true", help="Show browser windows while exploring transitions.")
    explore_p.add_argument("--json", action="store_true", help="Print the full transition graph JSON instead of a compact summary.")

    bench_p = sub.add_parser("bench", help="Run bundled benchmark specs.")
    bench_p.add_argument("--prepare", action="store_true", help="Prepare benchmark repos and record actions.")
    bench_p.add_argument("--install-deps", action="store_true", help="With --prepare, run npm ci/npm install when needed.")
    bench_p.add_argument("--headed", action="store_true", help="Show browser windows while bundled benchmark specs run.")
    bench_p.add_argument("--compare", action="store_true", help="Run ablation modes for every bundled benchmark spec and summarize them.")
    add_oracle_policy_args(bench_p, default_policy="visual")

    core_p = sub.add_parser("core-bench", help="Run public-issue/hidden-oracle core checkpoint synthesis benchmark.")
    core_p.add_argument("--tasks", default="benchmarks/core/public/tasks.yaml")
    core_p.add_argument("--hidden", default="benchmarks/core/hidden/oracles.yaml")
    core_p.add_argument("--agent", choices=["llm", "deterministic"], default="llm", help="Checkpoint synthesis agent. Defaults to llm.")
    core_p.add_argument("--model", default=None, help="Model id for --agent llm (required unless --models is given; no default). Append @<provider> (e.g. gpt-5.4@gateway) to route to a known provider; untagged ids use --base-url/--api-key-env.")
    core_p.add_argument("--models", nargs="+", help="Run the strict multi-model leaderboard. Accepts space or comma separated model ids. Each id may carry an @<provider> tag (e.g. 'deepseek-ai/DeepSeek-V4-Flash:DashScope gpt-5.4@gateway') to mix providers in one run.")
    core_p.add_argument("--base-url", default="https://api-inference.modelscope.cn/v1", help="OpenAI-compatible base URL for the default provider (untagged models).")
    core_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY", help="Environment variable with the API key for the default provider (untagged models).")
    core_p.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature for --agent llm.")
    core_p.add_argument("--max-tokens", type=int, default=8192, help="Max tokens per synthesis call (raise for reasoning models).")
    core_p.add_argument("--execute", action="store_true", help="Execute generated method checkpoints instead of static grading only.")
    core_p.add_argument("--visual", action="store_true", help="When used with --execute, attach hidden canonical visual oracles for execution-time grading.")
    core_p.add_argument("--headed", action="store_true", help="Show browser windows while executing generated checkpoints.")
    add_oracle_policy_args(core_p, default_policy="semantic")
    core_p.add_argument("--explore", action=argparse.BooleanOptionalAction, default=True, help="Autonomous source-exploration mode (default on): the agent reads/greps the repo via a ReAct loop instead of a pre-digested source context. Use --no-explore for the legacy single-shot protocol.")
    core_p.add_argument("--max-explore-steps", type=int, default=12, help="Max ReAct tool-call steps per task in exploration mode.")
    core_p.add_argument("--grading", choices=["strict", "explore-execute"], default="strict", help="Headline grading mode. 'strict' (default) grades the model's proposed spec structurally. 'explore-execute' grades combined success = target intent recovered AND checkpoint executes AND hidden visual oracle passes; it forces --execute + a visual oracle policy and, unless --tasks is overridden, uses the de-leaked browser subset benchmarks/core/public/tasks_explore.yaml.")

    evo_p = sub.add_parser("evo-bench", help="[experimental; not used in the paper] Run test-guided checkpoint evolution on public core tasks.")
    evo_p.add_argument("--tasks", default="benchmarks/core/public/tasks.yaml")
    evo_p.add_argument("--hidden", default="benchmarks/core/hidden/oracles.yaml")
    evo_p.add_argument("--task-scope", choices=TASK_SCOPE_CHOICES, default="browser-web", help="Task universe for default benchmark selection. Defaults to Phase 1 browser/web smoke.")
    evo_p.add_argument("--split", default=str(DEFAULT_PHASE3_SPLIT), help="Train/held-out split manifest for --phase3-suite.")
    evo_p.add_argument("--benchmarks", nargs="+", default=None, help="Benchmark filters. Accepts space or comma separated values; defaults are determined by --task-scope.")
    evo_p.add_argument("--task-limit", type=int, default=None, help="Optional cap after benchmark filtering, useful for smoke runs.")
    evo_p.add_argument("--iterations", type=int, default=2)
    evo_p.add_argument("--population", type=int, default=2)
    evo_p.add_argument("--memory-mode", choices=["none", "episode", "repository"], default="episode")
    evo_p.add_argument("--memory-backend", choices=MEMORY_BACKEND_CHOICES, default="stateportal", help="Memory system used for recipe retrieval: StatePortal native, A-MEM/Mem0 baselines, or the StatePortal anchor.")
    evo_p.add_argument("--memory-scope", choices=MEMORY_SCOPE_CHOICES, default="intra", help="Retrieval scope for cross-game transfer: intra (same game), cross (other games), or both.")
    evo_p.add_argument("--memory-baseline-suite", action="store_true", help="Run the StatePortal anchor vs A-MEM vs Mem0 held-out transfer comparison and write memory-baseline-suite.json/md.")
    evo_p.add_argument("--memory-baseline-backends", nargs="+", default=None, help="Backends to compare in --memory-baseline-suite. Accepts space/comma separated values. Defaults to amem, mem0, stateportal-anchor.")
    evo_p.add_argument("--memory-policy", choices=MEMORY_POLICY_CHOICES, default="positive-negative")
    evo_p.add_argument("--mutation-policy", choices=MUTATION_POLICY_CHOICES, default="source-constrained")
    evo_p.add_argument("--diagnosis", action=argparse.BooleanOptionalAction, default=True, help="Use detailed public failure diagnosis. Disable for ablations.")
    evo_p.add_argument("--memory-in", help="Optional EvoPortal memory JSON to preload. Not used unless supplied.")
    evo_p.add_argument("--memory-out", help="Optional path to write sanitized EvoPortal memory JSON.")
    evo_p.add_argument("--phase1-suite", action="store_true", help="Run the Phase 1 browser/web ablation suite and write phase1-browser-web-live.json/md.")
    evo_p.add_argument("--phase3-suite", action="store_true", help="Run the Phase 3 all-core train/held-out ablation suite and write phase3-core-suite.json/md.")
    evo_p.add_argument("--include-repository-memory", action="store_true", help="Include repository-memory variant in --phase1-suite.")
    evo_p.add_argument("--provider-retries", type=int, default=2, help="Retry provider-empty synthesis fallbacks before accepting a scaffold fallback.")
    evo_p.add_argument("--repair", action=argparse.BooleanOptionalAction, default=True, help="Use public-report-driven LLM repair between evolution rounds.")
    evo_p.add_argument("--repair-max-attempts", type=int, default=2, help="Attempts per repair LLM call.")
    evo_p.add_argument("--agent", choices=["llm", "deterministic"], default="llm", help="Seed checkpoint synthesis agent. Defaults to llm.")
    evo_p.add_argument("--model", default=None, help="Model id for --agent llm (required for --agent llm; no default). Append @<provider> (e.g. gpt-5.4@gateway) to route to a known provider; untagged ids use --base-url/--api-key-env.")
    evo_p.add_argument("--base-url", default="https://api-inference.modelscope.cn/v1", help="OpenAI-compatible base URL for the default provider (untagged models).")
    evo_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY", help="Environment variable with the API key for the default provider (untagged models).")
    evo_p.add_argument("--temperature", type=float, default=0.0)
    evo_p.add_argument("--max-tokens", type=int, default=8192)
    evo_p.add_argument("--headed", action="store_true", help="Show browser windows while executing candidates.")
    add_oracle_policy_args(evo_p, default_policy="semantic")
    evo_p.add_argument("--explore", action=argparse.BooleanOptionalAction, default=True, help="Use autonomous source exploration for LLM seed synthesis.")
    evo_p.add_argument("--max-explore-steps", type=int, default=12)

    state_loop_p = sub.add_parser(
        "state-loop",
        help="Run the State Loop Gen+Test benchmark: config synthesis + warm state restore + before/after screenshot grading.",
    )
    state_loop_p.add_argument("--tasks", default=str(STATE_LOOP_TASKS))
    state_loop_p.add_argument("--agent", choices=["llm", "deterministic"], default="llm", help="Config synthesis agent. Use deterministic for an offline run without an API key.")
    state_loop_p.add_argument("--model", default=None, help="Model id for --agent llm (required for --agent llm; no default). Append @<provider> (e.g. gpt-5.4@gateway) to route to a known provider; untagged ids use --base-url/--api-key-env.")
    state_loop_p.add_argument("--base-url", default="https://api-inference.modelscope.cn/v1", help="OpenAI-compatible base URL for the default provider (untagged models).")
    state_loop_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY", help="Environment variable with the API key for the default provider (untagged models).")
    state_loop_p.add_argument("--temperature", type=float, default=0.2)
    state_loop_p.add_argument("--max-tokens", type=int, default=8192)
    state_loop_p.add_argument("--max-iters", type=int, default=1, help="Max Gen+Test iterations per task (feedback regeneration on failure).")
    state_loop_p.add_argument("--seeds", type=int, default=1, help="Repeat each task N times for pass@1/pass@k and cost variance.")
    state_loop_p.add_argument("--headed", action="store_true", help="Show browser windows while running the loop.")
    state_loop_p.add_argument("--report-dir", default=None, help="Optional explicit report directory.")
    state_loop_p.add_argument("--explore", action=argparse.BooleanOptionalAction, default=True, help="Autonomous source-exploration mode for --agent llm (default on).")
    state_loop_p.add_argument("--max-explore-steps", type=int, default=12, help="Max ReAct tool-call steps per task in exploration mode.")
    state_loop_p.add_argument(
        "--transition-action-source",
        choices=["generated_spec", "minimal_agent_v1"],
        default="generated_spec",
        help=(
            "generated_spec keeps formal arm E unchanged; minimal_agent_v1 runs "
            "the non-formal E-prime post-Start semantic action planner."
        ),
    )
    state_loop_p.add_argument("--minimal-action-max-actions", type=int, default=8)
    state_loop_p.add_argument("--minimal-action-max-tokens", type=int, default=2048)
    state_loop_p.add_argument("--minimal-action-format-repairs", type=int, choices=[0, 1], default=1)
    state_loop_p.add_argument("--minimal-action-cache", default=None)

    gui_agent_p = sub.add_parser(
        "gui-agent-bench",
        help="Run the real vision GUI-agent baseline (pixel actions) for the C1 cost-accuracy / same-start comparison.",
    )
    gui_agent_p.add_argument("--tasks", default=str(STATE_LOOP_TASKS))
    gui_agent_p.add_argument("--model", default=DEFAULT_GUI_AGENT_MODEL, help="Multimodal computer-use model id.")
    gui_agent_p.add_argument("--base-url", default=DEFAULT_GUI_AGENT_BASE_URL)
    gui_agent_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY")
    gui_agent_p.add_argument("--max-steps", type=int, default=15, help="Max pixel actions per phase (reach-start / do-transition).")
    gui_agent_p.add_argument(
        "--start-mode", choices=["from_start", "synthesized", "restored"], default="from_start",
        help="from_start: play from the entry screen (Phase A+B). synthesized: the GUI arm synthesizes its OWN deep start (same stack/model as the method), injects it, then runs only the transition phase (fair same-start comparison). 'restored' is a legacy alias for 'synthesized'.",
    )
    gui_agent_p.add_argument("--synth-model", default=None, help="Model for self-synthesizing the start checkpoint in --start-mode synthesized (defaults to --model).")
    gui_agent_p.add_argument("--max-explore-steps", type=int, default=12, help="Max ReAct tool-call steps for self-synthesizing the start (synthesized mode).")
    gui_agent_p.add_argument("--headed", action="store_true")
    gui_agent_p.add_argument("--report-dir", default=None)

    fuzz_p = sub.add_parser(
        "fuzz-bench",
        help="[experimental; not used in the paper] State fuzzing: LLM-driven evolutionary mutation of seed deep states, checked against a robustness oracle (C3) on a warm session.",
    )
    fuzz_p.add_argument("--tasks", default=str(STATE_FUZZ_TASKS))
    fuzz_p.add_argument("--headless", action="store_true", help="Run the browser without a visible window (default: headed / visible).")
    fuzz_p.add_argument("--headed", action="store_true", help="(default) Show the browser window; kept for explicitness/back-compat.")
    fuzz_p.add_argument("--report-dir", default=None)
    fuzz_p.add_argument("--model", default=None, help="LLM used as the generation engine (required; no default; supports @provider tags).")
    fuzz_p.add_argument("--api-key-env", default="MODELSCOPE_API_KEY", help="Env var holding the API key for the default provider.")
    fuzz_p.add_argument("--budget-ms", type=int, default=None, help="Per-task mutation-loop budget in ms (overrides the task's budget_ms).")
    fuzz_p.add_argument("--num-seeds", type=int, default=4, help="Number of LLM-generated seed states per task.")
    fuzz_p.add_argument("--samples", type=int, default=2, help="Infill samples generated per mutation round.")
    fuzz_p.add_argument("--top-n", type=int, default=5, help="Top-N seeds (by fitness) sampled for mutation via softmax.")
    fuzz_p.add_argument("--temperature", type=float, default=0.9, help="LLM sampling temperature for seed/mutation generation.")
    fuzz_p.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "none"],
        default="medium",
        help="Thinking intensity for reasoning models (default: medium). 'none' omits the parameter entirely.",
    )
    fuzz_p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible operator/seed selection.")
    fuzz_p.add_argument("--resume", action="store_true", help="Skip tasks already completed in the --report-dir's fuzz.json (continue an aborted/chunked run).")
    fuzz_p.add_argument("--spacerpg-inject", nargs="+", default=None, help="SpaceRPG-only: enabled injection kinds (space/comma separated), or 'all'/'both'. Choices: save_restore, legacy_save, save_index, state_overlay, replace_state, action_replay. Default: all.")
    fuzz_p.add_argument("--refresh-seeds", action="store_true", help="SpaceRPG-only: recapture seed saves from the warm session, ignoring the seed cache.")
    fuzz_p.add_argument(
        "--priors",
        default="none",
        help="Issue-guided structured priors: 'none' (no-prior control), 'auto' "
        "(benchmarks/upstream/<benchmark>/fuzz_priors.json if present), or an explicit path.",
    )

    p5_p = sub.add_parser(
        "p5-scale",
        help="Run the P5 full-scale C1/C2/C3 experiment from benchmarks/p5/manifest.yaml (cost-accuracy Pareto, memory transfer, robustness).",
    )
    p5_p.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="P5 experiment manifest path.")
    p5_p.add_argument("--arms", nargs="+", default=None, help=f"Subset of normal C1/C2/C3 arms. Choices: {', '.join(ALL_ARMS)}. Lazarus manifests validate their own arms. Accepts space/comma separated values.")
    p5_p.add_argument("--models", nargs="+", default=None, help="Override the manifest model list. Accepts space/comma separated values.")
    p5_p.add_argument("--seeds", type=int, default=None, help="Override the manifest seed count.")
    p5_p.add_argument("--memory-workers", type=int, default=1, help="Maximum concurrent model jobs for C2 memory-baseline runs (default: 1).")
    p5_p.add_argument("--c1-outer_worker", type=int, default=None, help="Maximum concurrent C1 (arm, model, seed) invocations (default: 8).")
    p5_p.add_argument("--c1-inner_worker", type=int, default=4, help="Maximum task lanes inside each C1 invocation (default: 4).")
    p5_p.add_argument("--c1-workers", type=int, default=None, help="Deprecated alias for --c1-outer_worker.")
    p5_p.add_argument("--smoke", action="store_true", help="Offline plumbing run: deterministic agent + fake visual judge, 1 seed.")
    p5_p.add_argument("--dry-run", action="store_true", help="Print the run plan and exit without running any arm.")
    p5_p.add_argument("--aggregate", action="store_true", help="Aggregate produced reports into p5-scale-summary.json/.md after the run.")
    p5_p.add_argument("--headed", action="store_true", help="Show browser windows while arms run.")
    p5_p.add_argument("--report-dir", default=None, help="Optional explicit run directory.")
    p5_p.add_argument("--resume", action="store_true", help="Resume an interrupted --report-dir; C2 also continues from atomic per-task phase checkpoints.")
    p5_p.add_argument(
        "--preflight-artifact",
        default=None,
        help=(
            "Complete manifest-derived online preflight JSON required before "
            "formal C1 model calls."
        ),
    )
    p5_p.add_argument("--calibration-artifact", default=None, help="Passed browser + Luanti calibration JSON required before formal C-D model calls.")
    p5_p.add_argument("--task-ids", action="append", default=None, help="Optional comma-separated C1 task ids; repeat the flag to build a run-local shakeout subset.")
    p5_p.add_argument(
        "--c1-source-index",
        default=None,
        help=(
            "Trial-atomic C1 recovery only: import every completed tuple from a "
            "compatible prior p5-run-index.json and run its registered noncompleted tuples."
        ),
    )
    p5_p.add_argument(
        "--c1-source-contract-migration",
        default=None,
        help=(
            "Audited JSON contract required when a recovery source differs only "
            "by explicitly registered controller/provenance migrations."
        ),
    )

    replay_p = sub.add_parser(
        "replay-bench",
        help=(
            "Run the C4 replay-determinism experiment: rerun already-synthesized "
            "checkpoints with zero provider calls and measure verdict "
            "reproduction, flake rate and per-replay wall clock."
        ),
    )
    replay_sub = replay_p.add_subparsers(dest="replay_command", required=True)

    replay_harvest = replay_sub.add_parser(
        "harvest",
        help="Freeze a checkpoint population from prior C1 run indexes.",
    )
    replay_harvest.add_argument(
        "--index",
        action="append",
        default=None,
        help=(
            "A prior p5-run-index.json. Repeat the flag; later indexes win on "
            "collision, so pass them oldest-first. Defaults to every index under "
            "reports/warptest."
        ),
    )
    replay_harvest.add_argument("--arm", default="c1_state", help="Source arm (default: c1_state).")
    replay_harvest.add_argument(
        "--out",
        default="reports/warptest/c4-replay-population",
        help="Directory to write the frozen population into.",
    )

    replay_run = replay_sub.add_parser(
        "run", help="Replay a frozen population and write trials plus a summary."
    )
    replay_run.add_argument(
        "--population",
        default="reports/warptest/c4-replay-population/population.json",
        help="Frozen population manifest produced by `replay-bench harvest`.",
    )
    replay_run.add_argument(
        "--manifest",
        default="benchmarks/p5/replay_manifest.yaml",
        help="Replay experiment manifest.",
    )
    replay_run.add_argument(
        "--environment",
        required=True,
        help=(
            "Label for this execution environment, e.g. mac-a, mac-b or "
            "ci-linux. Recorded on every trial and reported per environment."
        ),
    )
    replay_run.add_argument("--report-dir", default=None, help="Optional explicit run directory.")
    replay_run.add_argument(
        "--repetitions",
        type=int,
        default=None,
        help="Override the per-profile repetition count.",
    )
    replay_run.add_argument(
        "--benchmarks",
        nargs="+",
        default=None,
        help="Restrict the run to these benchmarks.",
    )
    replay_run.add_argument("--headed", action="store_true", help="Show application windows.")
    replay_run.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore existing trials in the report directory and replay everything.",
    )

    replay_agg = replay_sub.add_parser(
        "aggregate", help="Re-aggregate persisted trials and rebuild the summary."
    )
    replay_agg.add_argument(
        "--report-dir",
        action="append",
        required=True,
        help=(
            "Directory holding replay-trials.json. Repeat the flag to pool "
            "several environments into one summary; the first directory "
            "receives the combined output."
        ),
    )
    replay_agg.add_argument(
        "--manifest",
        default="benchmarks/p5/replay_manifest.yaml",
        help="Replay experiment manifest.",
    )
    replay_agg.add_argument(
        "--c1-summary",
        default=None,
        help=(
            "Optional p5-scale-summary.md used to derive the regression-cost "
            "amortization curve from measured C1 arm costs."
        ),
    )

    segment_p = sub.add_parser("segment-bench", help="[experimental; not used in the paper] Run public segment-bank active bug discovery benchmark.")
    segment_p.add_argument("--bank", default="benchmarks/segments/public/segments.yaml")
    segment_p.add_argument("--bugs", default="benchmarks/segments/hidden/bugs.yaml")
    segment_p.add_argument("--agents", action="append", help="Comma-separated agent names. Defaults to segment_probe.")
    segment_p.add_argument("--parallel", action="store_true", help="Run segment-agent jobs in parallel.")

    gui_p = sub.add_parser("gui-inspect", help="Inspect generic GUI runtime support boundary for a repository.")
    gui_p.add_argument("repo")
    gui_p.add_argument("--json", action="store_true")

    compare_p = sub.add_parser("compare", help="Run ablation modes for a spec.")
    compare_p.add_argument("spec")
    compare_p.add_argument("--repo", help="Target repository path. Required unless spec declares repo or benchmark.")
    compare_p.add_argument("--headed", action="store_true")
    compare_p.add_argument("--visual", action="store_true", help="Run visual oracle validation for every ablation mode.")
    add_oracle_policy_args(compare_p, default_policy="semantic")

    effort_p = sub.add_parser("effort", help="Estimate reusable adapter/profile/spec/instrumentation effort.")
    effort_p.add_argument("--json", action="store_true", help="Print full effort report JSON without writing a report file.")

    paper_p = sub.add_parser("paper-tables", help="Generate JSON/Markdown tables for paper reporting.")
    paper_p.add_argument("--json", action="store_true", help="Print full paper-tables JSON without writing files.")
    paper_p.add_argument("--generate-missing", action="store_true", help="Run missing benchmark ablations before generating tables.")

    model_p = sub.add_parser("model-bench", help="[experimental; not used in the paper] Run ModelScope model-generated spec benchmark.")
    model_p.add_argument("--models", nargs="+", help="ModelScope model ids. Accepts space or comma separated values.")
    model_p.add_argument("--repeats", type=int, default=1, help="Number of generations per model/task.")
    model_p.add_argument("--tasks", help="Optional YAML/JSON task file. Defaults to built-in five tasks.")
    model_p.add_argument("--headed", action="store_true", help="Show browser windows while generated specs are executed.")
    model_p.add_argument("--quiet", action="store_true", help="Suppress per-model/task progress logs.")
    model_p.add_argument("--temperature", type=float, default=0.2)
    model_p.add_argument("--max-tokens", type=int, default=4096)
    add_oracle_policy_args(model_p, default_policy="visual")

    visual_p = sub.add_parser("visual-experiment", help="Run headed human validation for visual judge accuracy.")
    visual_p.add_argument("--sample-count", type=int, default=50)
    visual_p.add_argument("--headed", action="store_true", help="Show game browsers while samples run.")
    visual_p.add_argument("--benchmark", help="Restrict the visual experiment to one benchmark, for example OpenRA.")
    visual_p.add_argument(
        "--protocol",
        choices=["legacy", "hybrid-v1", "hybrid-v2", "hybrid-v3"],
        default="legacy",
        help="Use the legacy interactive experiment or the staged paper-grade hybrid-v1 protocol.",
    )
    visual_p.add_argument(
        "--phase",
        choices=["prepare", "judge", "annotate", "adjudicate", "aggregate"],
        help="hybrid-v1 stage; omitted for the legacy experiment.",
    )
    visual_p.add_argument("--dataset-dir", type=Path, help="Hybrid dataset/output directory.")
    visual_p.add_argument("--split", choices=["pilot", "formal"], default="formal")
    visual_p.add_argument(
        "--pilot-cohort",
        choices=["development", "validation", "confirmation"],
        default="development",
        help="Versioned non-formal task cohort used by hybrid-v2 pilot prepare.",
    )
    visual_p.add_argument(
        "--task-file",
        type=Path,
        default=Path("benchmarks/p5/c1_tasks.yaml"),
        help="Frozen C1 task catalog used by hybrid-v1 prepare.",
    )
    visual_p.add_argument("--annotator", help="annotator-a, annotator-b, or adjudicator.")
    visual_p.add_argument(
        "--annotation-mode",
        choices=["human", "vlm"],
        default="human",
        help=(
            "Annotation source for hybrid-v1 annotate. Human opens the blind UI; "
            "VLM performs resumable paid pair judging with explicit model receipts."
        ),
    )
    visual_p.add_argument(
        "--annotator-model",
        help=(
            "VLM model id for --annotation-mode vlm. Defaults to the frozen "
            "annotator-a/annotator-b mapping; untagged ids use @relay."
        ),
    )
    visual_p.add_argument(
        "--p5-manifest",
        type=Path,
        default=Path("benchmarks/p5/manifest.yaml"),
        help="Manifest that freezes the formal visual-judge route.",
    )
    visual_p.add_argument(
        "--credentials-file",
        type=Path,
        default=Path("test_model_batch.py"),
        help="Literal-only Relay credential source; secrets are never persisted.",
    )

    onboarding_p = sub.add_parser(
        "hook-onboarding",
        help="Build and evaluate reusable application state hooks from clean source revisions.",
    )
    onboarding_p.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_ONBOARDING_MANIFEST,
    )
    onboarding_p.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_ONBOARDING_CREDENTIALS,
    )
    onboarding_p.add_argument("--model", default=None)
    onboarding_p.add_argument("--reasoning-effort", default=None)
    onboarding_p.add_argument("--app", action="append", dest="apps")
    onboarding_p.add_argument("--report-dir", type=Path, default=None)
    onboarding_p.add_argument("--reference-only", action="store_true")
    onboarding_p.add_argument("--skip-reference", action="store_true")
    onboarding_p.add_argument("--provider-preflight", action="store_true")

    prov_p = sub.add_parser(
        "provenance",
        help="Metric-grounded task provenance: metric-select | oracle-synth | naturalize | mutation.",
    )
    prov_p.add_argument("stage", choices=["metric-select", "oracle-synth", "naturalize", "mutation"])
    prov_p.add_argument("--game", default="2048", help="Game to run the stage for (default 2048).")
    prov_p.add_argument("--top", type=int, default=25, help="metric-select: candidates to emit.")
    prov_p.add_argument("--reach-trials", type=int, default=200, help="metric-select: reachability Monte-Carlo trials.")
    prov_p.add_argument("--headed", action="store_true", help="oracle-synth/mutation: show the game browser.")
    prov_p.add_argument("--no-verify", action="store_true", help="oracle-synth: skip Gate-B re-run verification.")
    prov_p.add_argument("--max-per-operator", type=int, default=2, help="mutation: mutants per operator.")

    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            result = RepositoryInspector().inspect(args.repo)
            if args.json:
                print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
            else:
                report_path = write_inspect_report(result)
                print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
                print(f"inspect_report={report_path}")
        elif args.command == "run":
            out_dir = new_report_dir()
            report = run_spec(
                args.spec,
                repo_arg=args.repo,
                headless=not args.headed,
                report_dir=out_dir,
                oracle_policy=resolved_oracle_policy(args, default_policy="semantic"),
                visual_sample_rate=args.visual_sample_rate,
                visual_on_semantic_failure=args.visual_on_semantic_failure,
                cascade_config=resolved_cascade_config(args),
            )
            result: dict[str, object] = {
                "status": report.overall_status,
                "benchmark": report.benchmark,
                "app_url": report.app_url,
                "report_dir": str(out_dir),
            }
            if report.overall_status != "success":
                failed_phases = {}
                for phase_name in ("checkpoint_restoration", "validation", "smoke_execution", "oracle_result"):
                    phase: object = getattr(report, phase_name, None)
                    if phase is not None and getattr(phase, "status", None) == "failure":
                        failed_phases[phase_name] = getattr(phase, "detail", "")
                if failed_phases:
                    result["failure_phases"] = failed_phases
                exit_code = _extract_utility_exit_code(report.metrics)
                if exit_code is not None:
                    result["exit_code"] = exit_code
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "explore-transitions":
            out_dir = new_report_dir()
            result = explore_transitions(
                args.spec,
                repo_arg=args.repo,
                max_actions=args.max_actions,
                horizon=args.horizon,
                report_dir=out_dir,
                headless=not args.headed,
                model=args.model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            )
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(
                    json.dumps(
                        {
                            "report_dir": result["report_dir"],
                            "benchmark": result["benchmark"],
                            "summary": result["summary"],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
        elif args.command == "bench":
            out_dir = new_report_dir()
            bench_specs = discover_bench_specs()
            if args.prepare:
                prep = [prepare_repo(repo, install_deps=args.install_deps) for repo in discover_bench_repos(bench_specs)]
                write_json(out_dir / "prepare.json", {"actions": prep})
                print(f"prepare_report={out_dir / 'prepare.json'}")
            if args.compare:
                result = compare_benchmark_specs(
                    bench_specs,
                    headless=not args.headed,
                    report_dir=out_dir,
                    oracle_policy=resolved_oracle_policy(args, default_policy="visual"),
                    visual_sample_rate=args.visual_sample_rate,
                    visual_on_semantic_failure=args.visual_on_semantic_failure,
                    cascade_config=resolved_cascade_config(args),
                )
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
                return
            results = []
            for spec in bench_specs:
                result = {"spec": str(spec)}
                try:
                    report_dir = out_dir / spec.parent.name / spec.stem
                    report = run_spec(
                        spec,
                        repo_arg=None,
                        report_dir=report_dir,
                        headless=not args.headed,
                        oracle_policy=resolved_oracle_policy(args, default_policy="visual"),
                        visual_sample_rate=args.visual_sample_rate,
                        visual_on_semantic_failure=args.visual_on_semantic_failure,
                        cascade_config=resolved_cascade_config(args),
                    )
                    result.update({"repo": report.repo_path, "status": report.overall_status, "visual": report.visual_oracle_result.status})
                except Exception as error:
                    result.update({"status": "failed", "reason": str(error)})
                results.append(result)
            write_json(out_dir / "bench.json", {"results": results})
            print(json.dumps({"report_dir": str(out_dir), "results": results}, indent=2))
        elif args.command == "core-bench":
            grading_mode = "explore_execute" if args.grading == "explore-execute" else "strict"
            tasks_path = args.tasks
            default_tasks = "benchmarks/core/public/tasks.yaml"
            if grading_mode == "explore_execute" and tasks_path == default_tasks:
                tasks_path = "benchmarks/core/public/tasks_explore.yaml"
                print(f"[core-bench] explore-execute grading: defaulting --tasks to {tasks_path}", flush=True)
            if args.models:
                if grading_mode == "explore_execute":
                    raise SystemExit("core-bench --grading explore-execute is not supported with the --models leaderboard (it requires per-task execution). Run a single --model instead.")
                result = run_core_multimodel_benchmark(
                    Path.cwd(),
                    models=split_cli_values(args.models, []),
                    tasks_path=tasks_path,
                    oracle_path=args.hidden,
                    base_url=args.base_url,
                    api_key_env=args.api_key_env,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    explore=args.explore,
                    max_explore_steps=args.max_explore_steps,
                )
                print(json.dumps({"report_dir": result["report_dir"], "leaderboard": result["leaderboard"]}, indent=2, ensure_ascii=False))
            else:
                if args.agent == "llm" and not args.model:
                    raise SystemExit("core-bench --agent llm requires --model (or use --models for the leaderboard); there is no default model.")
                # In explore-execute mode the benchmark forces execution + a visual
                # oracle policy internally. Pass the raw flag through so an unset policy
                # defaults to visual, while an explicit `--oracle-policy semantic` is
                # honoured as a deliberate semantic-only run (visual skipped) for
                # runtimes whose goldens can't be reproduced on this host (e.g. luanti).
                if grading_mode == "explore_execute":
                    core_oracle_policy = args.oracle_policy
                else:
                    core_oracle_policy = resolved_oracle_policy(args, default_policy="semantic")
                result = run_core_synthesis_benchmark(
                    Path.cwd(),
                    tasks_path=tasks_path,
                    oracle_path=args.hidden,
                    agent_kind=args.agent,
                    model=args.model,
                    base_url=args.base_url,
                    api_key_env=args.api_key_env,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    execute=args.execute,
                    oracle_policy=core_oracle_policy,
                    visual_sample_rate=args.visual_sample_rate,
                    visual_on_semantic_failure=args.visual_on_semantic_failure,
                    cascade_config=resolved_cascade_config(args),
                    headless=not args.headed,
                    explore=args.explore,
                    max_explore_steps=args.max_explore_steps,
                    grading_mode=grading_mode,
                )
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "evo-bench":
            if args.agent == "llm" and not args.model:
                raise SystemExit("evo-bench --agent llm requires --model; there is no default model.")
            default_benchmarks = list(CORE_BENCHMARKS if args.task_scope == "all-core" or args.phase3_suite else BROWSER_WEB_BENCHMARKS)
            common_kwargs = {
                "tasks_path": args.tasks,
                "oracle_path": args.hidden,
                "agent_kind": args.agent,
                "model": args.model,
                "base_url": args.base_url,
                "api_key_env": args.api_key_env,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
                "iterations": args.iterations,
                "population": args.population,
                "task_scope": "all-core" if (args.phase3_suite or args.memory_baseline_suite) else args.task_scope,
                "memory_mode": args.memory_mode,
                "memory_backend": args.memory_backend,
                "memory_scope": args.memory_scope,
                "memory_policy": args.memory_policy,
                "mutation_policy": args.mutation_policy,
                "diagnosis_enabled": args.diagnosis,
                "memory_in": args.memory_in,
                "memory_out": args.memory_out,
                "benchmarks": split_cli_values(args.benchmarks, default_benchmarks),
                "task_limit": args.task_limit,
                "oracle_policy": resolved_oracle_policy(args, default_policy="semantic"),
                "visual_sample_rate": args.visual_sample_rate,
                "visual_on_semantic_failure": args.visual_on_semantic_failure,
                "cascade_config": resolved_cascade_config(args),
                "headless": not args.headed,
                "explore": args.explore,
                "max_explore_steps": args.max_explore_steps,
                "provider_retries": args.provider_retries,
                "repair": args.repair,
                "repair_max_attempts": args.repair_max_attempts,
            }
            if args.memory_baseline_suite:
                suite_kwargs = {k: v for k, v in common_kwargs.items() if k not in {"memory_backend", "memory_scope"}}
                backends = split_cli_values(args.memory_baseline_backends, list(DEFAULT_MEMORY_BASELINE_BACKENDS))
                result = run_memory_baseline_suite(Path.cwd(), split_path=args.split, backends=backends, **suite_kwargs)
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
            elif args.phase3_suite:
                result = run_phase3_suite(Path.cwd(), split_path=args.split, **common_kwargs)
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
            elif args.phase1_suite:
                result = run_phase1_suite(Path.cwd(), include_repository_memory=args.include_repository_memory, **common_kwargs)
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
            else:
                result = run_evo_synthesis_benchmark(Path.cwd(), **common_kwargs)
                print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "state-loop":
            if args.agent == "llm":
                if not args.model:
                    raise SystemExit("state-loop --agent llm requires --model; there is no default model. Use --agent deterministic for an offline run.")
                _clean_model, _provider = resolve_model(
                    args.model, default_base_url=args.base_url, default_api_key_env=args.api_key_env
                )
                if not os.environ.get(_provider.api_key_env):
                    raise SystemExit(
                        f"{_provider.api_key_env} is required for state-loop --agent llm. "
                        f"Use --agent deterministic for an offline run."
                    )
            result = run_state_loop_benchmark(
                Path.cwd(),
                tasks_path=args.tasks,
                agent_kind=args.agent,
                model=args.model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                explore=args.explore,
                max_explore_steps=args.max_explore_steps,
                max_iters=args.max_iters,
                seeds=args.seeds,
                headless=not args.headed,
                report_dir=Path(args.report_dir) if args.report_dir else None,
                arm=(
                    "c1_state_minimal_agent"
                    if args.transition_action_source == "minimal_agent_v1"
                    else "c1_state"
                ),
                transition_action_source=args.transition_action_source,
                minimal_action_max_actions=args.minimal_action_max_actions,
                minimal_action_max_tokens=args.minimal_action_max_tokens,
                minimal_action_format_repairs=args.minimal_action_format_repairs,
                minimal_action_cache=(
                    Path(args.minimal_action_cache)
                    if args.minimal_action_cache
                    else None
                ),
            )
            print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "gui-agent-bench":
            result = run_gui_agent_benchmark(
                Path.cwd(),
                tasks_path=args.tasks,
                model=args.model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                max_steps=args.max_steps,
                start_mode=args.start_mode,
                synth_model=args.synth_model,
                max_explore_steps=args.max_explore_steps,
                headless=not args.headed,
                report_dir=Path(args.report_dir) if args.report_dir else None,
            )
            print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "fuzz-bench":
            if not args.model:
                raise SystemExit("fuzz-bench requires --model (the LLM generation engine); there is no default model.")
            result = run_fuzz_benchmark(
                Path.cwd(),
                tasks_path=args.tasks,
                headless=args.headless,
                report_dir=Path(args.report_dir) if args.report_dir else None,
                config=FuzzConfig(
                    model=args.model,
                    api_key_env=args.api_key_env,
                    num_seeds=args.num_seeds,
                    samples_per_op=args.samples,
                    top_n=args.top_n,
                    temperature=args.temperature,
                    reasoning_effort=None if args.reasoning_effort == "none" else args.reasoning_effort,
                    budget_ms=args.budget_ms,
                    seed=args.seed,
                    resume=args.resume,
                    spacerpg_inject=tuple(split_cli_values(args.spacerpg_inject, [])) or None,
                    refresh_seeds=args.refresh_seeds,
                    priors=args.priors,
                ),
            )
            print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "p5-scale":
            if args.c1_outer_worker is not None and args.c1_workers is not None:
                parser.error("--c1-outer_worker and deprecated --c1-workers cannot be used together")
            c1_outer_worker = args.c1_outer_worker
            if c1_outer_worker is None and args.c1_workers is not None:
                warnings.warn(
                    "--c1-workers is deprecated; use --c1-outer_worker",
                    DeprecationWarning,
                    stacklevel=2,
                )
                c1_outer_worker = args.c1_workers
            if c1_outer_worker is None:
                c1_outer_worker = 8
            # Lazarus arm labels are intentionally not in ``ALL_ARMS``: their
            # manifest-specific dispatcher validates them after loading the
            # selected experiment contract.
            arms = split_cli_values(args.arms, []) if args.arms else None
            result = run_p5_scale(
                Path.cwd(),
                manifest_path=args.manifest,
                models=split_cli_values(args.models, []) or None if args.models else None,
                seeds=args.seeds,
                arms=arms,
                smoke=args.smoke,
                dry_run=args.dry_run,
                aggregate=args.aggregate,
                headless=not args.headed,
                report_dir=Path(args.report_dir) if args.report_dir else None,
                memory_workers=args.memory_workers,
                c1_outer_worker=c1_outer_worker,
                c1_inner_worker=args.c1_inner_worker,
                resume=args.resume,
                preflight_artifact=Path(args.preflight_artifact) if args.preflight_artifact else None,
                calibration_artifact=Path(args.calibration_artifact) if args.calibration_artifact else None,
                task_ids=split_cli_values(args.task_ids, []) if args.task_ids else None,
                c1_source_index=(
                    Path(args.c1_source_index) if args.c1_source_index else None
                ),
                c1_source_contract_migration=(
                    Path(args.c1_source_contract_migration)
                    if args.c1_source_contract_migration
                    else None
                ),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "replay-bench":
            result = _run_replay_bench_command(args)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "segment-bench":
            result = run_segment_bug_discovery(
                Path.cwd(),
                bank_path=args.bank,
                bugs_path=args.bugs,
                agents=split_cli_values(args.agents, ["segment_probe"]),
                parallel=args.parallel,
            )
            print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "gui-inspect":
            result = GenericGuiAppAdapter().inspect(args.repo).to_dict()
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(json.dumps({"runtime_family": result["runtime_family"], "checkpointing_status": result["checkpointing_status"]}, indent=2, ensure_ascii=False))
        elif args.command == "compare":
            result = compare_spec(
                args.spec,
                repo_arg=args.repo,
                headless=not args.headed,
                oracle_policy=resolved_oracle_policy(args, default_policy="semantic"),
                visual_sample_rate=args.visual_sample_rate,
                visual_on_semantic_failure=args.visual_on_semantic_failure,
                cascade_config=resolved_cascade_config(args),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "effort":
            result = build_effort_report(Path.cwd())
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                out_dir = new_report_dir()
                write_json(out_dir / "effort.json", result)
                print(json.dumps({"report_dir": str(out_dir), "loc": result["loc"], "ratios": result["ratios"]}, indent=2, ensure_ascii=False))
        elif args.command == "paper-tables":
            out_dir = None if args.json else new_report_dir()
            result = build_paper_tables(Path.cwd(), generate_missing=args.generate_missing, report_dir=out_dir)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                json_path, md_path = write_paper_tables(out_dir, result)
                print(json.dumps({"report_dir": str(out_dir), "json": str(json_path), "markdown": str(md_path)}, indent=2, ensure_ascii=False))
        elif args.command == "model-bench":
            tasks = load_task_file(args.tasks) if args.tasks else None
            result = run_model_benchmark(
                models=split_cli_values(args.models, DEFAULT_MODELS),
                tasks=tasks,
                repeats=args.repeats,
                headless=not args.headed,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                quiet=args.quiet,
                oracle_policy=resolved_oracle_policy(args, default_policy="visual"),
                visual_sample_rate=args.visual_sample_rate,
                visual_on_semantic_failure=args.visual_on_semantic_failure,
                cascade_config=resolved_cascade_config(args),
            )
            print(json.dumps({"report_dir": result["report_dir"], "summary": result["summary"]}, indent=2, ensure_ascii=False))
        elif args.command == "visual-experiment":
            if args.protocol in {"hybrid-v1", "hybrid-v2", "hybrid-v3"}:
                if args.phase is None:
                    parser.error("visual-experiment --protocol hybrid-v1 requires --phase")
                out_dir = run_hybrid_visual_phase(
                    phase=args.phase,
                    dataset_dir=args.dataset_dir,
                    task_file=args.task_file,
                    split=args.split,
                    headed=args.headed,
                    pilot_cohort=args.pilot_cohort,
                    annotator_id=args.annotator,
                    annotation_mode=args.annotation_mode,
                    annotator_model=args.annotator_model,
                    p5_manifest=args.p5_manifest,
                    credentials_file=args.credentials_file,
                )
            else:
                if (
                    args.phase is not None
                    or args.dataset_dir is not None
                    or args.annotator is not None
                    or args.annotation_mode != "human"
                    or args.annotator_model is not None
                ):
                    parser.error("hybrid phase/dataset/annotator options require --protocol hybrid-v1")
                out_dir = run_visual_experiment(sample_count=args.sample_count, headed=args.headed, benchmark=args.benchmark)
            print(json.dumps({"report_dir": str(out_dir)}, indent=2, ensure_ascii=False))
        elif args.command == "hook-onboarding":
            if args.reference_only and args.skip_reference:
                parser.error("--reference-only and --skip-reference cannot be combined")
            onboarding_manifest = load_onboarding_manifest(args.manifest)
            onboarding_model = args.model or onboarding_manifest.model
            onboarding_effort = (
                args.reasoning_effort or onboarding_manifest.reasoning_effort
            )
            if args.provider_preflight:
                onboarding_report = (
                    args.report_dir
                    or Path("reports/warptest/onboarding-provider-preflight")
                )
                result = run_onboarding_provider_preflight(
                    args.credentials,
                    model=onboarding_model,
                    reasoning_effort=onboarding_effort,
                    report_dir=onboarding_report,
                )
            else:
                result = run_hook_onboarding(
                    manifest_path=args.manifest,
                    credentials=args.credentials,
                    model=onboarding_model,
                    reasoning_effort=onboarding_effort,
                    app_ids=args.apps,
                    report_dir=args.report_dir,
                    reference_only=args.reference_only,
                    skip_reference=args.skip_reference,
                )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "provenance":
            workspace = Path.cwd()
            if args.stage == "metric-select":
                from .metrics.select import run_game

                payload = run_game(args.game, workspace=workspace, top=args.top, reach_trials=args.reach_trials)
                summary = {
                    "game": payload["game"],
                    "config_hash": payload["config"]["config_hash"],
                    "git_commit": payload["git_commit"],
                    "n_candidates": payload["n_candidates"],
                    "out": payload["_out_path"],
                    "top3": [
                        {"name": c["name"], "final_score": c["final_score"], "raw": c["raw"]}
                        for c in payload["candidates"][:3]
                    ],
                }
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            elif args.stage == "oracle-synth":
                if args.game != "2048":
                    raise SystemExit("oracle-synth PoC currently supports --game 2048")
                from .oracle_synth import synthesize_2048

                payload = synthesize_2048(workspace=workspace, headless=not args.headed, verify=not args.no_verify)
                print(json.dumps({"out_report": payload["_out_report"],
                                  "tasks": [{"id": t["task_id"], "verify": t.get("verification", {}).get("status")}
                                            for t in payload["tasks"]]}, indent=2, ensure_ascii=False))
            elif args.stage == "naturalize":
                from .issue_naturalize import naturalize_specs

                spec_dir = workspace / "specs" / args.game / "generated"
                specs = sorted(spec_dir.glob("*.yaml"))
                if not specs:
                    raise SystemExit(f"no generated specs in {spec_dir}; run oracle-synth first")
                payload = naturalize_specs(specs, workspace=workspace)
                print(json.dumps({"out_report": payload["_out_report"],
                                  "tasks": [{"id": t["task_id"],
                                             "shallow_ok": t["shallow"]["guard_ok"],
                                             "explore_ok": t["explore"]["guard_ok"]}
                                            for t in payload["tasks"]]}, indent=2, ensure_ascii=False))
            elif args.stage == "mutation":
                if args.game != "2048":
                    raise SystemExit("mutation PoC currently supports --game 2048")
                from .mutation import run_mutation_2048

                r = run_mutation_2048(workspace=workspace, headless=not args.headed, max_per_operator=args.max_per_operator)
                print(json.dumps({"total": r.total, "killed": r.killed, "survived": r.survived,
                                  "errored": r.errored, "kill_rate": round(r.kill_rate, 4)}, indent=2))
    except Exception as error:
        print(f"warptest: error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
