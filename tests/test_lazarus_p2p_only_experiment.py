from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from pathlib import Path

import yaml
import pytest

from warptest.lazarus_catalog import validate_lazarus_catalog
from warptest.lazarus_p2p_only_experiment import (
    EXPECTED_CELL_COUNT,
    EXPECTED_GROUP_COUNT,
    EXPECTED_MODELS,
    aggregate_p2p_only_reports,
    build_p2p_only_plan,
    preflight_p2p_only,
    run_p2p_only_groups,
    validate_p2p_only_manifest,
    write_run_index,
)
from warptest.lazarus_phase0 import AgentVisibleSource
from warptest.lazarus_p2p_only_executor import (
    _runtime_definitions,
    _validate_p2p_only_definitions,
)
from warptest.lazarus_upstream import definitions_from_upstream_record
from warptest.lazarus_versions import (
    LazarusVersionArtifact,
    LazarusVersionPair,
    tree_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/p5/lazarus_p2p_only_manifest.yaml"
CATALOG = ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _tasks() -> list[dict]:
    payload = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    _, tasks = validate_lazarus_catalog(payload, source_path=CATALOG)
    return tasks


def test_manifest_freezes_the_approved_990_cell_schedule() -> None:
    tasks = _tasks()
    contract = validate_p2p_only_manifest(
        _manifest(),
        catalog_path=CATALOG,
        task_ids=[task["id"] for task in tasks],
    )
    assert contract.models == EXPECTED_MODELS
    assert contract.seeds == (0, 1, 2)
    assert contract.expected_cells == EXPECTED_CELL_COUNT
    assert contract.paid_run_authorized is True


def test_current_formal_schedule_requires_eight_lanes_without_changing_cells() -> None:
    path = ROOT / "benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    task_ids = [task["id"] for task in _tasks()]
    contract = validate_p2p_only_manifest(manifest, catalog_path=CATALOG, task_ids=task_ids)
    assert contract.outer_workers == 8
    assert contract.expected_cells == 440
    for value in (1, 2, 4):
        changed = {**manifest, "outer_workers": value}
        with pytest.raises(ValueError, match="outer_workers must be 8"):
            validate_p2p_only_manifest(changed, catalog_path=CATALOG, task_ids=task_ids)


def test_manifest_rejects_an_incomplete_seed_set_or_provider_default_profile() -> None:
    tasks = _tasks()
    task_ids = [task["id"] for task in tasks]
    second_seed = _manifest()
    second_seed["seeds"] = [0, 1]
    try:
        validate_p2p_only_manifest(second_seed, catalog_path=CATALOG, task_ids=task_ids)
    except ValueError as error:
        assert "frozen five-arm schedule" in str(error)
    else:
        raise AssertionError("second seed was accepted")

    provider_default = _manifest()
    provider_default["model_request_profiles"]["kimi-k2.6"] = "provider_default"
    try:
        validate_p2p_only_manifest(
            provider_default, catalog_path=CATALOG, task_ids=task_ids
        )
    except ValueError as error:
        assert "thinking_disabled" in str(error)
    else:
        raise AssertionError("provider-default thinking was accepted")


def test_manifest_requires_the_real_cold_start_protocol() -> None:
    tasks = _tasks()
    legacy = _manifest()
    legacy["schema_version"] = 1
    legacy.pop("start_protocol", None)
    try:
        validate_p2p_only_manifest(
            legacy,
            catalog_path=CATALOG,
            task_ids=[task["id"] for task in tasks],
        )
    except ValueError as error:
        assert "schema_version must be 3" in str(error)
    else:
        raise AssertionError("legacy prepositioned-Start manifest was accepted")


def test_readiness_uses_scoped_identity_without_presentation_fields() -> None:
    readiness = preflight_p2p_only(
        ROOT,
        manifest_path=MANIFEST,
        require_runtime_canaries=False,
        materialize=False,
    )
    report = readiness.to_report()
    identity = report["identity"]
    assert identity["identity_version"] == "scoped-v1"
    assert set(identity["runtime_sha256"]) == {"jupyterlab", "luanti", "openra"}
    assert "provider_profile_sha256" in identity
    assert "grading_sha256" in identity
    assert "orchestrator_sha256" in identity
    assert "materialization_sha256" in identity
    assert "viewer_sha256" not in identity
    assert report["presentation_identity"] == {}


def test_planner_has_594_unique_cells_and_model_scoped_groups() -> None:
    task_ids = [task["id"] for task in _tasks()]
    plan = build_p2p_only_plan(task_ids)
    assert plan.complete is True
    assert plan.trial_count == EXPECTED_CELL_COUNT
    assert len(set(plan.keys)) == EXPECTED_CELL_COUNT
    assert len(plan.groups) == EXPECTED_GROUP_COUNT
    assert all(len(group) == 10 for group in plan.groups)
    assert {
        (group[0].task_id, group[0].model, group[0].seed) for group in plan.groups
    } == {(task_id, model, seed) for task_id in task_ids for model in EXPECTED_MODELS for seed in (0, 1, 2)}
    for group in plan.groups:
        state = [key for key in group if key.arm == "lazarus_state"]
        assert [key.version for key in state] == ["buggy", "fixed"]


def test_formal_planner_still_rejects_lzp_tasks() -> None:
    from warptest.lazarus_orchestrator import build_lazarus_trial_plan

    tasks = [task["id"] for task in _tasks()]
    plan = build_lazarus_trial_plan(tasks, formal=True)
    assert plan.complete is False
    assert "formal Lazarus matrix permits only lz-* tasks" in plan.failures


def _complete_reports() -> tuple[object, list[dict]]:
    plan = build_p2p_only_plan([task["id"] for task in _tasks()])
    rows = []
    for key in plan.keys:
        rows.append(
            {
                "task_id": key.task_id,
                "model": key.model,
                "seed": key.seed,
                "arm": key.arm,
                "version": key.version,
                "complete": True,
                "semantic_evidence": "not_applicable_p2p_only",
                "defect_evidence_source": "dual_after_visual_goldens",
                "start_protocol": "real_cold_start_v1",
                "start_evaluated": True,
                "start_success": True,
                "goal_success": key.version == "fixed",
                "defect_exposed": key.version == "buggy",
                "false_alarm": False,
                "checkpoint_reuse_success": (
                    key.arm == "lazarus_state" and key.version == "fixed"
                ),
                "synthesis_cache_hit": (
                    key.arm == "lazarus_state" and key.version == "fixed"
                ),
                "actual_synthesis_calls": (
                    0 if key.arm == "lazarus_state" and key.version == "fixed" else 1
                ),
            }
        )
    return plan, rows


def test_aggregate_is_descriptive_and_never_pools_models() -> None:
    plan, rows = _complete_reports()
    result = aggregate_p2p_only_reports(rows, plan=plan)
    assert result["complete"] is True
    assert result["formal"] is False
    assert result["confirmatory"] is False
    assert result["paper_ready"] is False
    assert result["statistics"] == {
        "mode": "exploratory_descriptive_by_model",
        "pooled_across_models": False,
        "h2": "not_applicable_all_tasks_at_goal",
    }
    assert set(result["models"]) == set(EXPECTED_MODELS)
    assert all(value["observed_cells"] == 330 for value in result["models"].values())


def test_run_index_keeps_model_in_group_identity(tmp_path: Path) -> None:
    plan, rows = _complete_reports()
    readiness = preflight_p2p_only(
        ROOT,
        manifest_path=MANIFEST,
        require_runtime_canaries=False,
        materialize=False,
    )
    # Preflight intentionally remains incomplete until paid API/calibration
    # artifacts exist, but its immutable identity can still bind a test index.
    path = write_run_index(
        rows,
        plan=plan,
        readiness=readiness,
        path=tmp_path / "run-index.json",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["group_identity"] == ["task_id", "model", "seed"]
    assert len(payload["groups"]) == EXPECTED_GROUP_COUNT
    assert all(len(group["cells"]) == 10 for group in payload["groups"])
    assert len({group["model"] for group in payload["groups"]}) == 3


@pytest.mark.parametrize("worker_count", [2, 8])
def test_group_runner_is_model_scoped_and_resume_imports_only_atomic_groups(
    tmp_path: Path, worker_count: int,
) -> None:
    readiness = preflight_p2p_only(
        ROOT, manifest_path=MANIFEST, require_runtime_canaries=False, materialize=False
    )
    pairs = {}
    sources = {}
    for task in readiness.tasks:
        task_id = task["id"]
        roots = {}
        for version in ("buggy", "fixed"):
            root = tmp_path / task_id / version
            root.mkdir(parents=True)
            (root / "source.txt").write_text(version, encoding="utf-8")
            git = tmp_path / task_id / f"{version}.git"
            git.mkdir()
            roots[version] = LazarusVersionArtifact(
                task_id=task_id,
                version=version,
                root=root,
                git_metadata=git,
                source_revision="a" * 40,
                patch_sha256="b" * 64,
                tree_sha256=tree_fingerprint(root),
                changed_files=(),
                build_identity={},
                cache_hit=True,
            )
        pairs[task_id] = LazarusVersionPair(
            buggy=roots["buggy"], fixed=roots["fixed"], version_switch_ms=0
        )
        source = tmp_path / task_id / "agent-source"
        source.mkdir()
        trusted = tmp_path / task_id / "agent.git"
        trusted.mkdir()
        sources[task_id] = AgentVisibleSource(
            task_id=task_id,
            root=source,
            trusted_git_dir=trusted,
            source_revision="a" * 40,
            source_tree_sha256="c" * 64,
            sanitized_tree_sha256="c" * 64,
            forbidden_tokens=(),
            version_identity_hidden=True,
        )
    ready = replace(
        readiness,
        contract=replace(readiness.contract, outer_workers=worker_count),
        complete=True,
        failures=(),
        pairs=pairs,
        agent_sources=sources,
    )

    generated = set()

    def executor(context):
        key = context.key
        group = (key.task_id, key.model, key.seed)
        synth_arm = key.arm in {
            "lazarus_state",
            "lazarus_agent_s_synth",
            "lazarus_scalecua_synth",
        }
        new = synth_arm and group not in generated
        if synth_arm:
            generated.add(group)
        checkpoint = hashlib.sha256(
            f"{key.task_id}|{key.model}|{key.seed}".encode()
        ).hexdigest()
        fixed_state = synth_arm and not new
        return {
            "task_id": key.task_id,
            "model": key.model,
            "seed": key.seed,
            "arm": key.arm,
            "version": key.version,
            "complete": True,
            "semantic_evidence": "not_applicable_p2p_only",
            "defect_evidence_source": "dual_after_visual_goldens",
            "start_protocol": "real_cold_start_v1",
            "start_evaluated": True,
            "start_construction_mode": (
                "shared_generated_state_from_cold_start"
                if synth_arm
                else (
                    "scalecua_phase_a_from_cold_start"
                    if key.arm == "lazarus_scalecua"
                    else "agent_s_phase_a_from_cold_start"
                )
            ),
            "runtime": {
                "initial_screenshot_sha256": "a" * 64,
                "trace": {
                    "method_protocol": "p2p-only-generated-state-cdefg-v4",
                    "start_gate": {"passed": True},
                    "phase_b": {"executed": True},
                    "costs": {
                        phase: {
                            "duration_ms": 0,
                            "usage": {
                                "total_tokens": 0,
                                "llm_calls": 0,
                                "usage_complete": True,
                            },
                        }
                        for phase in (
                            "synthesis",
                            "restore",
                            "execution",
                            "bootstrap",
                            "grading",
                        )
                    },
                },
            },
            "provenance_sha256": context.artifact.tree_sha256,
            "per_trial_p2p_ok": True,
            "start_success": True,
            "goal_success": key.version == "fixed",
            "defect_exposed": key.version == "buggy",
            "false_alarm": False,
            "checkpoint_sha256": checkpoint,
            "actual_synthesis_calls": 1 if new else 0,
            "synthesis_cache_hit": True if fixed_state else False,
            "checkpoint_reuse_success": True if fixed_state else False,
        }

    from contextlib import contextmanager
    from warptest.c1_task_deadline import current_task_deadline

    @contextmanager
    def resource_scope(context):
        assert current_task_deadline() is None
        yield

    executor.resource_scope = resource_scope
    import threading
    barrier = threading.Barrier(worker_count)
    lock = threading.Lock()
    active = peak = started = 0

    def counted_executor(context):
        nonlocal active, peak, started
        with lock:
            ordinal = started
            started += 1
            active += 1
            peak = max(peak, active)
        try:
            if ordinal < worker_count:
                barrier.wait(timeout=10)
            return executor(context)
        finally:
            with lock:
                active -= 1

    counted_executor.resource_scope = resource_scope
    index_path = tmp_path / "run-index.json"
    first = run_p2p_only_groups(
        ready,
        executor=counted_executor,
        progress_index_path=index_path,
    )
    assert first.complete is True
    assert peak == worker_count
    with pytest.raises(ValueError, match=f"outer_workers={worker_count}"):
        run_p2p_only_groups(ready, executor=executor, outer_workers=1)
    assert len(first.groups) == 99
    assert len(first.reports) == 990
    resume = json.loads(index_path.read_text(encoding="utf-8"))
    second = run_p2p_only_groups(ready, executor=executor, resume_index=resume)
    assert second.complete is True
    assert len(second.imported_groups) == 99
    assert all(group.imported for group in second.groups)

    import copy
    partial = copy.deepcopy(resume)
    partial["groups"][0]["complete"] = False
    partial["groups"][0]["cells"] = partial["groups"][0]["cells"][:2]
    dispatched = []
    def remaining_executor(context):
        dispatched.append(context.key)
        return executor(context)
    restored = run_p2p_only_groups(ready, executor=remaining_executor, resume_index=partial)
    assert restored.complete and len(restored.reports) == 990
    assert dispatched == list(ready.plan.groups[0][2:])
    assert len(restored.imported_groups) == 98
    reordered = copy.deepcopy(partial)
    reordered["groups"][0]["cells"].reverse()
    with pytest.raises(ValueError, match="ordered complete prefix"):
        run_p2p_only_groups(ready, executor=remaining_executor, resume_index=reordered)
    corrupted = copy.deepcopy(partial)
    corrupted["groups"][0]["cells"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA drift"):
        run_p2p_only_groups(ready, executor=remaining_executor, resume_index=corrupted)

    swapped_seed = copy.deepcopy(resume)
    swapped_seed["groups"][0]["seed"] = 2
    with pytest.raises(ValueError, match="not atomic"):
        run_p2p_only_groups(ready, executor=executor, resume_index=swapped_seed)

    failed_calls = []
    def broken_executor(context):
        failed_calls.append(context.key)
        raise RuntimeError("provider authentication rejected")
    stopped = run_p2p_only_groups(ready, executor=broken_executor)
    assert not stopped.complete
    assert 1 <= len(failed_calls) <= worker_count
    assert len(stopped.groups) <= worker_count

    from warptest.synthesis import SynthesisError
    transient_calls = []
    def intermittent_executor(context):
        transient_calls.append(context.key)
        if context.key.model == EXPECTED_MODELS[0]:
            raise SynthesisError("gateway unavailable", artifacts={"failure_mode": "gateway_outage"})
        return executor(context)
    recovered_others = run_p2p_only_groups(ready, executor=intermittent_executor)
    assert not recovered_others.complete
    assert any(group.retryable for group in recovered_others.groups)
    assert sum(group.complete for group in recovered_others.groups) == 66
    assert sum(key.model == EXPECTED_MODELS[0] for key in transient_calls) <= worker_count

    quarantine_key = (ready.plan.groups[0][0].task_id, ready.plan.groups[0][0].model, ready.plan.groups[0][0].seed)
    remaining_calls = []
    def known_executor(context):
        remaining_calls.append((context.key.task_id, context.key.model, context.key.seed))
        return executor(context)
    quarantined = run_p2p_only_groups(ready, executor=known_executor, quarantined_groups=(quarantine_key,))
    assert not quarantined.complete
    assert len(quarantined.reports) == 980
    assert quarantine_key not in remaining_calls
    assert ready.plan.trial_count == 990

    from warptest.c1_task_deadline import C1TaskTimeout
    timeout_group = (ready.plan.groups[0][0].task_id, ready.plan.groups[0][0].model, ready.plan.groups[0][0].seed)
    def deadline_executor(context):
        if (context.key.task_id, context.key.model, context.key.seed) == timeout_group:
            error = C1TaskTimeout("accounted timeout")
            error.recovery = {"retryable":True,"category":"task_deadline_with_known_usage"}
            raise error
        return executor(context)
    timed = run_p2p_only_groups(ready, executor=deadline_executor)
    assert not timed.complete and len(timed.reports) == 980
    assert sum(group.complete for group in timed.groups) == 98
    assert sum(group.retryable for group in timed.groups) == 1

    legacy = dict(resume)
    legacy["schema"] = "c1-lazarus-p2p-only-run-index-v1"
    try:
        run_p2p_only_groups(ready, executor=executor, resume_index=legacy)
    except ValueError as error:
        assert "resume index schema is invalid" in str(error)
    else:
        raise AssertionError("legacy v1 groups were imported into a v2 run")


def test_p2p_only_executor_accepts_control_path_without_f2p_source_capsule() -> None:
    task = _tasks()[0]
    upstream = json.loads(
        (ROOT / task["defect"]["upstream_record"]).read_text(encoding="utf-8")
    )
    definitions = definitions_from_upstream_record(upstream)
    spec = yaml.safe_load((ROOT / task["oracle_spec"]).read_text(encoding="utf-8"))
    ids = tuple(spec["evaluation"]["lazarus"]["upstream_tests"]["per_trial_p2p"])

    class Context:
        key = type("Key", (), {"task_id": task["id"]})()
        per_trial_p2p_ids = ids

    _validate_p2p_only_definitions(
        Context(),
        definitions,
        source_root=ROOT / task["repo"],
        upstream=upstream,
    )
    runtime = _runtime_definitions(definitions, task=task)
    definition = runtime[ids[0]]
    assert definition.cwd.startswith("packages/")
    assert definition.argv[1] == "../../node_modules/jest/bin/jest.js"


def test_three_seed_pairing_preserves_different_outcomes():
    plan, rows = _complete_reports()
    for row in rows:
        if row["seed"] != 0:
            row["defect_exposed"] = False
    aggregate = aggregate_p2p_only_reports(rows, plan=plan)
    for model in aggregate["models"].values():
        assert model["expected_cells"] == 330
        assert set(model["seeds"]) == {"0", "1", "2"}
        for arm in model["arms"].values():
            assert arm["discrimination_pairs"] == 33
            assert arm["discrimination_successes"] == 11
        for seed, summary in model["seeds"].items():
            assert summary["observed_cells"] == 110
            for arm in summary["arms"].values():
                assert arm["discrimination_pairs"] == 11
                assert arm["discrimination_successes"] == (11 if seed == "0" else 0)


def test_disk_capacity_uses_c1_macos_policy_and_fails_closed_without_capacity(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from warptest import lazarus_p2p_only_experiment as experiment
    gib = 1024**3
    minimum = experiment.MIN_FREE_BYTES
    # Reclaimable capacity admits a run that ordinary free bytes would reject.
    monkeypatch.setattr(experiment.shutil, 'disk_usage', lambda _: SimpleNamespace(free=minimum - 10*gib))
    monkeypatch.setattr(experiment, '_important_available_bytes', lambda _: minimum + 30*gib)
    result = experiment._p2p_disk_capacity(tmp_path)
    assert result['capacity_ok'] is True
    assert result['free_bytes'] == minimum - 10*gib
    assert result['eligible_free_bytes'] == minimum + 30*gib
    assert result['minimum_free_bytes'] == minimum
    assert result['capacity_policy'] == 'macos_important_usage_v1'
    monkeypatch.setattr(experiment, '_important_available_bytes', lambda _: None)
    result = experiment._p2p_disk_capacity(tmp_path)
    assert result['capacity_ok'] is False
    assert result['capacity_policy'] == 'ordinary_free_bytes_v1'
    # Reclaimable capacity also governs when it is the scarcer of the two.
    monkeypatch.setattr(experiment.shutil, 'disk_usage', lambda _: SimpleNamespace(free=minimum + 30*gib))
    monkeypatch.setattr(experiment, '_important_available_bytes', lambda _: minimum - 1)
    assert experiment._p2p_disk_capacity(tmp_path)['capacity_ok'] is False
