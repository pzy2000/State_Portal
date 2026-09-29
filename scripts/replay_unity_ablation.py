#!/usr/bin/env python3
"""Continue four archived Terra candidates after the scoped Unity admission fix.

No checkpoint synthesis, repair, source exploration, or matrix expansion is
possible here. Original evidence is read-only; new grading has its own ledger.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from unittest.mock import patch

TASK_IDS = (
    "jynew-save-restore", "pal3-world-region-travel-roundtrip",
    "pal3-scene-transition", "pal3-companion-ending-preparation",
)
OLD_HASH = "5c9df34f7db4ebd9599c70151e2954fe60dfcd5542113f79a6fb2a6d45c9b653"
NEW_HASH = "98ca295c86ca673db1ac2bfeab8776f999bcb67b5892989c87e11dd26a17df63"
VARIANT = "no_assertion_prediction"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def forbidden(*args, **kwargs):
    raise RuntimeError("Recovery forbids synthesis, repair, and repository exploration")


def prepare(runtime, original_runtime, source, output):
    from warptest.c1_provenance import c1_harness_hash, validate_c1_environment
    from warptest.c1_spec_acceptance import accept_c1_generated_spec
    from warptest.replay_bench import write_task_subset
    from warptest.state_loop import load_state_loop_tasks, _unity_state_model_spec_errors
    from warptest.synthesis import finalize_formal_c1_model_output, parse_llm_checkpoint_output
    from warptest.synthesis_ablation import ablation_scope
    from warptest.task_schema import resolve_task_repo
    import yaml

    assert c1_harness_hash(original_runtime) == OLD_HASH
    assert c1_harness_hash(runtime) == NEW_HASH
    changed = []
    for old in (original_runtime / "warptest").rglob("*"):
        if not old.is_file() or "__pycache__" in old.parts:
            continue
        rel = old.relative_to(original_runtime)
        if digest(old) != digest(runtime / rel):
            changed.append(str(rel))
    assert changed == ["warptest/c1_spec_acceptance.py"], changed
    manifest = yaml.safe_load((runtime / "benchmarks/p5/manifest.yaml").read_text())
    environment = validate_c1_environment(runtime, manifest=manifest)
    tasks = {t.id: t for t in load_state_loop_tasks(runtime, "benchmarks/p5/c1_tasks.yaml")}
    violations = json.loads((source / "protocol-violations.json").read_text())["items"]
    assert tuple(x["task_id"] for x in violations) == TASK_IDS
    old_audit = json.loads((source / "terra-audit.json").read_text())
    assert old_audit["observed_trials"] == 244 and old_audit["data_integrity_complete"]
    checkpoints, records = {}, []
    for item in violations:
        task_id = item["task_id"]
        task = tasks[task_id]
        path = Path(item["candidate_path"])
        assert digest(path) == item["candidate_sha256"]
        metadata = json.loads((path.parent / "meta.json").read_text())
        synth_meta = json.loads((path.parent.parent / "synthesis-meta.json").read_text())
        assert metadata["response_sha256"] == item["candidate_sha256"]
        assert metadata["succeeded"] and not metadata["remaining_errors"]
        parsed = parse_llm_checkpoint_output(path.read_text())
        assert parsed["generated_spec"]["assertions"] == []
        reads = set(synth_meta["files_read"])
        assert all(e["source_path"].removeprefix("./") in reads for e in parsed["source_evidence"])
        repo = resolve_task_repo(runtime, task.issue)
        located = {"repo": str(repo), "inputs_read": [task.issue.source_path or "", str(repo)] + [str(repo / r) for r in reads]}
        with ablation_scope(VARIANT):
            checkpoint, finalization = finalize_formal_c1_model_output(
                task=task.issue, raw_output=path.read_text(),
                client=type("NoRepair", (), {"converse": staticmethod(forbidden)})(),
                model="gpt-5.6-terra", located=located, root=runtime,
                artifact_dir=None, source_cost_kind="checkpoint_synthesis",
                ablation_variant=VARIANT,
            )
            assert not finalization["repair"]["attempted"]
            assert checkpoint.model_generated_spec == parsed["generated_spec"]
            admission = accept_c1_generated_spec(
                checkpoint.model_generated_spec, issue=task.issue, benchmark=task.benchmark, seed=0,
            )
            assert admission.accepted, admission.errors
            oracle = yaml.safe_load((runtime / task.oracle_spec).read_text())
            errors = _unity_state_model_spec_errors(
                admission.spec, task=task, oracle_spec=oracle, spec_path=Path("candidate.yaml"),
            )
            assert not errors, errors
        old_rows = [r for r in old_audit["rows"] if r["task_id"] == task_id and r["variant"] == VARIANT]
        assert len(old_rows) == 1
        old_row = old_rows[0]
        assert digest(old_row["report"]) == old_row["report_sha256"]
        old_result = json.loads(Path(old_row["report"]).read_text())["results"][0]
        assert old_result["runtime_started"] is False
        assert old_result["reason"].endswith("generated_spec assertions must be a non-empty list")
        assert old_result["synthesis_actual_cost"] == old_result["cost"]
        checkpoints[task_id] = checkpoint
        destination = output / "candidates" / task_id
        destination.mkdir(parents=True, exist_ok=True)
        saved = destination / "response.txt"
        if saved.exists():
            assert digest(saved) == digest(path)
        else:
            shutil.copy2(path, saved)
        write(destination / "checkpoint.json", checkpoint.to_dict())
        records.append({
            **item, "recovery_compatible": True, "public_finalization": finalization,
            "original_report": old_row["report"], "original_report_sha256": old_row["report_sha256"],
            "oracle_spec": task.oracle_spec, "oracle_sha256": digest(runtime / task.oracle_spec),
            "copied_response": str(saved),
            "original_synthesis_cost": old_result["synthesis_actual_cost"],
            "original_synthesis_ms": old_result["checkpoint_synthesis_ms"],
            "original_trial_wall_ms": old_result["trial_wall_clock_ms"],
        })
    subset = write_task_subset(runtime / "benchmarks/p5/c1_tasks.yaml", TASK_IDS, output / "tasks.yaml")
    registration = {
        "schema_version": 1, "recovery": "unity-empty-auxiliary-assertions-v1",
        "runtime": str(runtime), "original_runtime": str(original_runtime),
        "original_harness_sha256": OLD_HASH, "harness_sha256": NEW_HASH,
        "runtime_changed_files": changed, "controller_sha256": digest(__file__),
        "seed": 0, "model": "gpt-5.6-terra@relay", "variant": VARIANT,
        "tasks": list(TASK_IDS), "task_subset": str(subset), "task_subset_sha256": digest(subset),
        "environment": environment, "candidates": records,
        "synthesis_calls_permitted": 0, "source_exploration_permitted": False,
        "visual_judge": manifest["visual_judge_model"], "inner_workers": 1,
        "golden_policy": "same frozen oracle and generator; fresh run-local cache",
        "accounting": "original synthesis once plus new runtime and grader; preserve both raw ledgers",
        "timing": "report additive synthesis plus continuation time, labelled recovered, not uninterrupted wall time",
        "deadline": "subtract original trial wall time from each registered task budget",
    }
    write(output / "recovery-registration.json", registration)
    return manifest, tasks, checkpoints, registration


class SavedCandidateAgent:
    name = "archived-terra-candidate"
    ablation_variant = VARIANT

    def __init__(self, checkpoints):
        self.checkpoints = checkpoints
        self.served = []

    def synthesize(self, task, root=None, artifact_dir=None, *, feedback=None):
        assert feedback is None and task.id in TASK_IDS and task.id not in self.served
        self.served.append(task.id)
        return copy.deepcopy(self.checkpoints[task.id])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--original-runtime", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-source", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    output = args.output.resolve()
    sys.path.insert(0, str(runtime))
    if (output / "execution-started.json").exists():
        raise RuntimeError("Execution already attempted: inspect evidence; automatic repetition is forbidden")
    manifest, tasks, checkpoints, registration = prepare(runtime, args.original_runtime, args.source, output)
    print(json.dumps({"compatible_candidates": len(checkpoints), "synthesis_calls": 0, "harness": NEW_HASH}), flush=True)
    if not args.run:
        return

    from warptest import cost
    from warptest.c1_experiment import relay_environment
    from warptest.c1_provider_runtime import provider_timeout_environment
    from warptest.c1_provenance import c1_harness_hash
    from warptest.c1_sandbox import C1SandboxManager, preflight_c1_sandboxes
    from warptest.model_request import model_request_profiles_environment
    from warptest.p5_scale import _manifest_visual_judge_env, _formal_c1_cache_env, _runtime_lock_dir_env
    from warptest.state_loop import run_state_loop_benchmark, _resolve_task_timeout_s
    from warptest.task_schema import resolve_task_repo

    assert not os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    repos = sorted({resolve_task_repo(runtime, tasks[t].issue) for t in TASK_IDS})
    write(output / "sandbox-preflight.json", preflight_c1_sandboxes(repos))
    with (output / "execution-started.json").open("x") as handle:
        json.dump({"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat(), "tasks": TASK_IDS}, handle)
    agent = SavedCandidateAgent(checkpoints)
    spent_ms = {r["task_id"]: r["original_trial_wall_ms"] for r in registration["candidates"]}

    def remaining_timeout(task, default):
        remaining = _resolve_task_timeout_s(task, default) - spent_ms[task.id] / 1000
        assert remaining > 0
        return remaining

    with (
        relay_environment(args.credential_source),
        _manifest_visual_judge_env(manifest),
        _formal_c1_cache_env(output / "c1-artifact-cache"),
        _runtime_lock_dir_env(output / "runtime-locks", runtime_lock_policy=manifest["runtime_lock"]),
        provider_timeout_environment(manifest["api"]["provider_timeout_s"]),
        model_request_profiles_environment(manifest["model_request_profiles"]),
        patch("warptest.synthesis.OpenAICompatibleChatClient.converse", forbidden),
        patch("warptest.state_loop.build_synthesis_agent", forbidden),
        patch("warptest.repo_tools.RepoSandbox.dispatch", forbidden),
        patch("warptest.state_loop._resolve_task_timeout_s", remaining_timeout),
    ):
        payload = run_state_loop_benchmark(
            runtime, tasks_path=output / "tasks.yaml", agent_kind="llm",
            model="gpt-5.6-terra@relay", temperature=manifest["api"]["temperature"],
            max_tokens=8192, explore=True, max_explore_steps=16,
            shared_synthesis_cache=None, max_iters=1, seeds=1, seed=0,
            headless=False, background_browser_headless=True, report_dir=output / "runtime",
            restore_diagnostics=False, inner_workers=1, task_timeout_s=2400,
            sandbox_manager=C1SandboxManager(run_id=output.name),
            invocation_id="unity-admission-recovery", synthesis_agent_factory=lambda: agent,
            synthesis_ablation=VARIANT, pair_start_cache=None,
            run_meta={"recovery_registration": str(output / "recovery-registration.json"),
                      "c1_harness_hash": NEW_HASH, "source_harness_hash": OLD_HASH},
        )
    assert c1_harness_hash(runtime) == NEW_HASH
    results = payload["results"]
    assert tuple(r["task_id"] for r in results) == TASK_IDS
    assert agent.served == list(TASK_IDS)
    for result in results:
        assert not cost.contains_unknown_usage(result["cost"])
        assert result["synthesis_actual_cost"]["llm_calls"] == 0
        assert all(c["kind"] == "visual_judge" for c in result["cost"].get("calls", []))
    write(output / "execution-completed.json", {
        "at": datetime.now(timezone.utc).isoformat(), "served": agent.served,
        "results": [{k: r.get(k) for k in ["task_id", "status", "start_success", "e2e_success", "incomplete", "reason"]} for r in results],
        "new_synthesis_calls": 0, "raw_report": str(output / "runtime/state-loop.json"),
        "raw_report_sha256": digest(output / "runtime/state-loop.json"),
    })


if __name__ == "__main__":
    main()
