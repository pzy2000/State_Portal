from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest
import yaml

from warptest.lazarus_calibration import (
    build_lazarus_calibration,
    lazarus_backend_binding_from_artifact,
)
from warptest.lazarus_expansion_gate import (
    FORMAL_GATE_CHECKS,
    FORMAL_GATE_CHECKS_EXPECTED_FALSE,
)
from warptest.lazarus_formal import (
    FORMAL_OBSERVER_MODEL,
    LazarusCapacityBudget,
    LazarusFormalPreflight,
    build_lazarus_formal_preflight,
    formal_capacity_budget,
    run_lazarus_formal,
    write_lazarus_formal_index,
    write_lazarus_formal_run,
)
from warptest.lazarus_manifest import LazarusManifestContract
from warptest.lazarus_orchestrator import (
    DEFAULT_LAZARUS_MODEL,
    build_lazarus_trial_plan,
)
from warptest.lazarus_roster import FORMAL_TASK_COUNT, FORMAL_TRIAL_COUNT
from warptest.lazarus_versions import LazarusVersionArtifact, LazarusVersionPair


# Mirrors warptest.lazarus_formal._BACKENDS: every registered binary must exist
# in the materialized tree or the calibration binding refuses the row.
BACKENDS = {
    "luanti": (
        "luanti_irrlicht_create_screenshot_v1",
        "luanti_irrlicht_sevent_v1",
        ("build-app/out/luanti.app/Contents/MacOS/luanti",),
    ),
    "openra": (
        "openra_renderer_save_screenshot_sync_v1",
        "openra_default_input_handler_v1",
        (
            "bin/OpenRA.Game.dll",
            "bin/OpenRA.Mods.Common.dll",
            "bin/OpenRA.Test.dll",
        ),
    ),
    "jupyterlab": (
        "playwright_page_screenshot_v1",
        "playwright_page_input_v1",
        ("dev_mode/static/main.js",),
    ),
}
GOLDEN_METADATA_SHA256 = "f" * 64


def _tasks() -> list[dict]:
    """Build a synthetic 30-row population with the registered locus mix."""

    rows: list[dict] = []
    for index in range(FORMAL_TASK_COUNT):
        if index < 14:
            benchmark = "luanti"
        elif index < 29:
            benchmark = "jupyterlab"
        else:
            benchmark = "openra"
        task_id = f"lz-{benchmark}-row-{index:02d}"
        rows.append(
            {
                "id": task_id,
                "benchmark": benchmark,
                "repo": f"games/{benchmark}",
                "preconditions": "PUBLIC start",
                "reference_steps": "PUBLIC transition",
                "defect": {"defect_locus": "on_path" if index % 3 == 0 else "at_goal"},
            }
        )
    return rows


TASKS = _tasks()


PATCHED_FILE = "source.txt"


def _artifact(
    tmp_path: Path,
    *,
    task_id: str,
    benchmark: str,
    version: str,
    ordinal: int,
    patch_sha256: str,
) -> LazarusVersionArtifact:
    root = tmp_path / task_id / version
    for relative in BACKENDS[benchmark][2]:
        binary = root / relative
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(f"{task_id}:{version}:{relative}".encode("utf-8"))
    return LazarusVersionArtifact(
        task_id=task_id,
        version=version,
        root=root,
        git_metadata=tmp_path / f"{task_id}-{version}.git",
        source_revision=f"{ordinal + 1:040x}",
        # Both versions of a pair are built from one inverse patch, so
        # materialization gives them the same digest.
        patch_sha256=patch_sha256,
        tree_sha256=f"{ordinal + 21:064x}",
        changed_files=(PATCHED_FILE,),
        build_identity={"status": "passed"},
        cache_hit=True,
    )


def _pairs(tmp_path: Path) -> dict[str, LazarusVersionPair]:
    pairs: dict[str, LazarusVersionPair] = {}
    for ordinal, task in enumerate(TASKS):
        task_id = str(task["id"])
        benchmark = str(task["benchmark"])
        patch_sha256 = f"{ordinal + 11:064x}"
        pairs[task_id] = LazarusVersionPair(
            buggy=_artifact(
                tmp_path,
                task_id=task_id,
                benchmark=benchmark,
                version="buggy",
                ordinal=ordinal * 10,
                patch_sha256=patch_sha256,
            ),
            fixed=_artifact(
                tmp_path,
                task_id=task_id,
                benchmark=benchmark,
                version="fixed",
                ordinal=ordinal * 10 + 1,
                patch_sha256=patch_sha256,
            ),
            version_switch_ms=12.5,
        )
    return pairs


def _calibration(pairs: dict[str, LazarusVersionPair]) -> dict:
    bindings = []
    for task in TASKS:
        task_id = str(task["id"])
        benchmark = str(task["benchmark"])
        capture, inputs, binaries = BACKENDS[benchmark]
        for artifact in (pairs[task_id].buggy, pairs[task_id].fixed):
            bindings.append(
                lazarus_backend_binding_from_artifact(
                    artifact,
                    benchmark=benchmark,
                    capture_backend=capture,
                    input_backend=inputs,
                    binary_paths=binaries,
                    canary_passed=True,
                )
            )
    return build_lazarus_calibration(
        bindings,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        observer_model=FORMAL_OBSERVER_MODEL,
        grounder_model=DEFAULT_LAZARUS_MODEL,
        judge_model="qwen3-vl-flash@relay",
    )


def _gate_checks() -> dict[str, bool]:
    """Publish the canonical vocabulary, so the fixture cannot drift from it."""

    return {
        name: name not in FORMAL_GATE_CHECKS_EXPECTED_FALSE
        for name in sorted(FORMAL_GATE_CHECKS)
    }


def _gate(
    task_id: str,
    pair: LazarusVersionPair,
    *,
    wrapper_s: float = 2.0,
    frozen_provenance: bool = True,
) -> dict:
    """Build one passed formal gate in either published provenance shape."""

    if frozen_provenance:
        provenance: dict[str, object] = {
            "versions": {
                "patch_sha256": pair.buggy.patch_sha256,
                "changed_files": [PATCHED_FILE],
                "patch_source": "materialized_tree_difference",
                "reverse_apply_reproduces_frozen_buggy_tree": True,
            }
        }
    else:
        # A live receipt records the same one patch under its own key.
        provenance = {
            "patch": {
                "sha256": pair.buggy.patch_sha256,
                "production_paths": [PATCHED_FILE],
                "source": "materialized_tree_difference",
            }
        }
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "passed",
        "complete": True,
        "derived_from_frozen_artifacts": frozen_provenance,
        "checks": _gate_checks(),
        "evidence": {
            "goldens": {"metadata_sha256": GOLDEN_METADATA_SHA256},
            **provenance,
            "upstream": {
                version: {
                    "complete": True,
                    "passed": True,
                    "isolation": "apfs_cow_clone",
                    "duration_ms": wrapper_s * 1000,
                    "results": [
                        {
                            "id": f"{task_id}-p2p",
                            "passed": True,
                            "timed_out": False,
                            "duration_ms": wrapper_s * 1000,
                        }
                    ],
                }
                for version in ("buggy", "fixed")
            },
        },
    }


def _agent_views(tmp_path: Path) -> dict:
    from warptest.lazarus_phase0 import AgentVisibleSource

    return {
        str(task["id"]): AgentVisibleSource(
            task_id=str(task["id"]),
            root=tmp_path / "agent" / str(task["id"]),
            trusted_git_dir=tmp_path / "trusted" / str(task["id"]),
            source_revision="d" * 40,
            source_tree_sha256="e" * 64,
            sanitized_tree_sha256="f" * 64,
            forbidden_tokens=(),
            version_identity_hidden=True,
        )
        for task in TASKS
    }


def _contract() -> LazarusManifestContract:
    return LazarusManifestContract(
        task_source="benchmarks/p5/c1_lazarus_tasks.yaml",
        task_ids=tuple(str(task["id"]) for task in TASKS),
        formal_benchmarks=("luanti", "jupyterlab", "openra"),
        arms=("lazarus_state", "lazarus_agent_s"),
        versions=("buggy", "fixed"),
        model=DEFAULT_LAZARUS_MODEL,
        seeds=(0, 1, 2),
        observer_model=FORMAL_OBSERVER_MODEL,
        grounder_model=DEFAULT_LAZARUS_MODEL,
        judge_model="qwen3-vl-flash@relay",
        provider_timeout_s=900.0,
        per_trial_p2p_timeout_s=300.0,
        formal_trial_count=FORMAL_TRIAL_COUNT,
        phase0_task_ids=(str(TASKS[0]["id"]), str(TASKS[-1]["id"])),
        long_chain_task_ids=(
            str(TASKS[0]["id"]),
            str(TASKS[-1]["id"]),
            str(TASKS[14]["id"]),
        ),
        phase0_paid_pilot_authorized=True,
        formal_paid_run_authorized=True,
    )


def _runtime_support(*, permitted: bool = True) -> dict:
    return {
        "schema": "c1-lazarus-runtime-support-v1",
        "schema_version": 1,
        "complete": permitted,
        "provider_dispatch_permitted": permitted,
        "task_count": len(TASKS),
        "bound_count": len(TASKS) if permitted else 0,
        "unbound_task_ids": [] if permitted else [str(TASKS[0]["id"])],
        "tasks": [],
        "failures": [],
    }


def _built_preflight(tmp_path: Path, **overrides) -> LazarusFormalPreflight:
    pairs = _pairs(tmp_path)
    kwargs = {
        "tasks": TASKS,
        "pairs": pairs,
        "calibration": _calibration(pairs),
        "manifest_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "provider_identity_sha256": "c" * 64,
        "gate_reports": {
            str(task["id"]): _gate(str(task["id"]), pairs[str(task["id"])])
            for task in TASKS
        },
        "per_trial_p2p_ids": {
            str(task["id"]): (f"{task['id']}-p2p",) for task in TASKS
        },
        "agent_sources": _agent_views(tmp_path),
        "task_evidence": {
            str(task["id"]): {
                "oracle_spec_sha256": "1" * 64,
                "upstream_evidence_sha256": "2" * 64,
                "golden_metadata_sha256": GOLDEN_METADATA_SHA256,
            }
            for task in TASKS
        },
        "environment": {
            "apfs": True,
            "submodules_clean": True,
            "capacity_ok": True,
            "visual_judge_fake": False,
        },
        "runtime_support": _runtime_support(),
        "capacity": LazarusCapacityBudget(
            outer_workers=4,
            clones_per_cell=2,
            largest_host_bytes=1_000,
            headroom_bytes=10_000,
        ),
        "contract": _contract(),
    }
    kwargs.update(overrides)
    return build_lazarus_formal_preflight(**kwargs)


def _cell_report(context, *, checkpoint_sha256: str = "a" * 64, **overrides) -> dict:
    key = context.key
    semantic_goal = key.version == "fixed"
    semantic_defect = key.version == "buggy"
    checkpoint_kind = (
        "semantic_state" if key.arm == "lazarus_state" else "agent_s_from_start"
    )
    checkpoint_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": checkpoint_kind,
        "runtime_restored": True,
    }
    receipt_sha256 = hashlib.sha256(
        (
            json.dumps(
                checkpoint_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    grading_result = {
        "version": key.version,
        "start_success": True,
        "goal_success": semantic_goal,
        "defect_exposed": semantic_defect,
        "invariants_ok": True,
        "per_trial_p2p_ok": True,
        "false_alarm": False,
        "complete": True,
        "after_statuses": {"goal": "evaluated", "defect": "evaluated"},
        "failures": [],
    }
    report = {
        "task_id": key.task_id,
        "arm": key.arm,
        "model": key.model,
        "seed": key.seed,
        "version": key.version,
        "defect_locus": context.defect_locus,
        "start_success": True,
        "goal_success": semantic_goal,
        "defect_exposed": semantic_defect,
        "invariants_ok": True,
        "per_trial_p2p_ok": True,
        "false_alarm": False,
        "complete": True,
        "provenance_valid": True,
        "provenance_sha256": context.artifact.tree_sha256,
        "treatment_tokens": 7,
        "treatment_time_ms": 4,
        "version_switch_ms": context.version_switch_ms,
        "checkpoint_sha256": checkpoint_sha256,
        "actual_synthesis_calls": (
            1 if key.arm == "lazarus_state" and key.version == "buggy" else 0
        ),
        "synthesis_cache_hit": key.arm == "lazarus_state" and key.version == "fixed",
        "checkpoint_reuse_success": (
            key.arm == "lazarus_state" and key.version == "fixed"
        ),
        "runtime": {
            "observed": True,
            "trace": (
                {
                    "observer": {
                        "model": FORMAL_OBSERVER_MODEL,
                        "usage": "registered_not_consumed_native_screenshot",
                        "llm_calls": 0,
                    }
                }
                if key.arm == "lazarus_agent_s"
                else {}
            ),
        },
        "grading": {
            "schema_version": 1,
            "evidence": {
                "version": key.version,
                "before_semantic_ok": True,
                "before_visual_ok": True,
                "goal_checks_ok": semantic_goal,
                "defect_signature_ok": semantic_defect,
                "invariants_ok": True,
                "fixed_after_visual": {
                    "ok": semantic_goal,
                    "screenshot_sha256": "b" * 64,
                    "oracle_sha256": "c" * 64,
                },
                "buggy_after_visual": {
                    "ok": semantic_defect,
                    "screenshot_sha256": "b" * 64,
                    "oracle_sha256": "d" * 64,
                },
                "per_trial_p2p_ok": True,
            },
            "result": grading_result,
        },
        "per_trial_p2p_evidence": {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300,
            "duration_ms": 2000,
            "checkpoint_restore": {
                "schema_version": 1,
                "applied": True,
                "checkpoint_sha256": checkpoint_sha256,
                "checkpoint_kind": checkpoint_kind,
                "runtime_restored": True,
                "path": ".warptest/lazarus/checkpoint-restore.json",
                "payload_sha256": receipt_sha256,
                "state_restore": {
                    "schema_version": 1,
                    "state_restore_applied": True,
                    "adapter": "test-isolated-checkpoint-restorer-v1",
                    "checkpoint_sha256": checkpoint_sha256,
                    "checkpoint_kind": checkpoint_kind,
                    "receipt_sha256": receipt_sha256,
                },
            },
            "results": [
                {
                    "id": f"{key.task_id}-p2p",
                    "passed": True,
                    "timed_out": False,
                    "duration_ms": 1000,
                }
            ],
            "failures": [],
        },
    }
    report.update(overrides)
    return report


def _run(preflight: LazarusFormalPreflight, executor, **kwargs):
    return run_lazarus_formal(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=64,
        lease_resources=False,
        **kwargs,
    )


def test_preflight_accepts_a_complete_formal_population(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)

    assert preflight.failures == ()
    assert preflight.complete is True
    assert preflight.plan.trial_count == FORMAL_TRIAL_COUNT
    assert preflight.plan.formal is True
    assert preflight.p2p_p95_s == pytest.approx(2.0)


def test_preflight_accepts_a_live_receipt_gate(tmp_path: Path) -> None:
    """Expansion rows publish a patch receipt instead of per-version trees."""

    pairs = _pairs(tmp_path)
    preflight = _built_preflight(
        tmp_path,
        pairs=pairs,
        gate_reports={
            str(task["id"]): _gate(
                str(task["id"]),
                pairs[str(task["id"])],
                frozen_provenance=False,
            )
            for task in TASKS
        },
    )

    assert preflight.failures == ()
    assert preflight.complete is True


def test_every_committed_gate_report_scores_against_one_vocabulary() -> None:
    """Both published provenances must satisfy the same formal gate contract."""

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    seen = {True: 0, False: 0}
    for row in catalog["tasks"]:
        gate = json.loads(
            (root / row["defect"]["gate_report"]).read_text(encoding="utf-8")
        )
        assert gate["schema_version"] == 1
        assert gate["status"] == "passed"
        assert gate["complete"] is True
        assert set(gate["checks"]) == set(FORMAL_GATE_CHECKS)
        for name, value in gate["checks"].items():
            assert bool(value) is (name not in FORMAL_GATE_CHECKS_EXPECTED_FALSE), (
                row["id"],
                name,
            )
        frozen = bool(gate["derived_from_frozen_artifacts"])
        seen[frozen] += 1
        # Exactly one provenance binding per report, never both and never none.
        assert ("versions" in gate["evidence"]) is frozen
        assert ("patch" in gate["evidence"]) is not frozen
    assert seen[True] and seen[False]
    assert seen[True] + seen[False] == FORMAL_TASK_COUNT


def test_the_materialization_request_comes_from_the_registered_catalog() -> None:
    """The pair must be built from the patch the published gate graded.

    A row's authoring version-request names the full inverse patch, which also
    reverts the fix's own regression test.  The gate graded the production-only
    ``formal-v1`` patch, so materializing from the authoring file would build a
    buggy tree no gate ever scored.
    """

    from warptest.lazarus_formal import _formal_version_request, _patch_touched_files

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    hosts = catalog["benchmark_hosts"]
    checked = 0
    for row in catalog["tasks"]:
        revision = hosts[row["benchmark"]]["revision"]
        request = _formal_version_request(root, row, fixed_revision=revision)
        gate = json.loads(
            (root / row["defect"]["gate_report"]).read_text(encoding="utf-8")
        )
        evidence = gate["evidence"]
        recorded = (
            evidence["versions"]["patch_sha256"]
            if "versions" in evidence
            else evidence["patch"]["sha256"]
        )

        assert request.patch_sha256 == row["defect"]["patch_sha256"]
        assert request.patch_sha256 == recorded, row["id"]
        # The fixed tree is the pinned revision only for a current-base row; a
        # row whose defect predates it registers its own verified base.
        sidecar = (
            root
            / "benchmarks/p5/lazarus/authoring"
            / f"{row['id']}.materialization.json"
        )
        if sidecar.is_file():
            base = json.loads(sidecar.read_text(encoding="utf-8"))
            assert base["patch_sha256"] == row["defect"]["patch_sha256"], row["id"]
            assert request.fixed_revision == base["fixed_revision"], row["id"]
        else:
            assert request.fixed_revision == revision, row["id"]
        assert request.build_class == row["defect"]["build_class"]
        # declared_files is read back out of the patch, never restated.
        assert request.declared_files == _patch_touched_files(
            root / row["defect"]["revert_patch"]
        )
        assert request.declared_files
        checked += 1
    assert checked == FORMAL_TASK_COUNT


def test_frozen_test_sources_resolve_beside_their_upstream_record() -> None:
    """A row's frozen test bytes are published under its own defect directory.

    The validator needs that root to confirm the bytes exist; without it every
    row declaring frozen sources is refused for a reason that is about the
    caller, not about the row.
    """

    from warptest.lazarus_mining import validate_lazarus_trial_provenance

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    declaring = 0
    for row in catalog["tasks"]:
        record_path = root / row["defect"]["upstream_record"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        sources = [
            source
            for test in record.get("tests", [])
            for source in (test.get("frozen_test_sources") or [])
        ]
        if not sources:
            continue
        declaring += 1
        for source in sources:
            assert (
                record_path.parent.parent / source["source_path"]
            ).is_file(), row["id"]
        errors = validate_lazarus_trial_provenance(
            record,
            source_root=root / row["repo"],
            harness_root=root,
            frozen_test_root=record_path.parent.parent,
        )
        assert not any("frozen test source" in error for error in errors), row["id"]
    assert declaring


def test_every_formal_row_has_a_registered_build_recipe() -> None:
    """A row whose build_class is not ``none`` must name how it is built.

    Treating an unregistered row as "no build" would surface later as a
    confusing materialization error rather than naming the missing recipe.
    """

    from warptest.lazarus_formal import _formal_prebuild

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    # These two rows declare a C++ build but run their test through a path that
    # is absent from the pinned tree, so neither the unittest nor the
    # multiplayer recipe describes them.  They are the same two rows the
    # provenance backfill reports as needing frozen test bytes; both gaps close
    # together, and this list shrinks as they do.
    awaiting_a_recipe = {
        "lz-luanti-8704-minimal-craft-result",
        "lz-luanti-15519-russian-plural-form",
    }
    unregistered: set[str] = set()
    for row in catalog["tasks"]:
        build_class = row["defect"]["build_class"]
        if build_class == "none":
            assert _formal_prebuild(row["id"], build_class) is None, row["id"]
            continue
        try:
            assert _formal_prebuild(row["id"], build_class) is not None, row["id"]
        except ValueError:
            unregistered.add(row["id"])
    assert unregistered == awaiting_a_recipe


def test_jupyterlab_recipes_rebuild_at_least_the_patched_packages() -> None:
    """A scope list that omits a patched package would build stale output."""

    from warptest.lazarus_formal import _patch_touched_files
    from warptest.lazarus_formal_build import JUPYTER_FORMAL_BUILD_SCOPES

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    checked = 0
    for row in catalog["tasks"]:
        if row["benchmark"] != "jupyterlab":
            continue
        scopes = JUPYTER_FORMAL_BUILD_SCOPES.get(row["id"])
        assert scopes, row["id"]
        patched = {
            value.split("/")[1]
            for value in _patch_touched_files(root / row["defect"]["revert_patch"])
            if value.startswith("packages/")
        }
        assert patched <= set(scopes), (row["id"], sorted(patched), scopes)
        checked += 1
    assert checked == 15


def test_registered_materialization_bases_accept_their_own_patch() -> None:
    """Every registered base must still reverse-apply the patch it was verified against.

    The revision is reviewable evidence rather than something a paid run
    recomputes, so it has to be re-checkable here: a patch that was regenerated
    without re-registering its base would otherwise materialize a tree nobody
    verified.
    """

    from scripts.register_lazarus_materialization_base import (
        SCHEMA,
        sidecar_path,
        verify_revision,
    )

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )
    historical = current = 0
    for row in catalog["tasks"]:
        path = sidecar_path(root, str(row["id"]))
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == SCHEMA
        assert payload["task_id"] == row["id"]
        assert payload["patch_sha256"] == row["defect"]["patch_sha256"]
        assert payload["source"] in {"pinned_host_revision", "patch_post_image_blob"}
        if payload["fixed_revision"] == payload["pinned_host_revision"]:
            assert payload["source"] == "pinned_host_revision"
            current += 1
        else:
            historical += 1
        assert verify_revision(
            root / row["repo"],
            root / row["defect"]["revert_patch"],
            payload["fixed_revision"],
        ), row["id"]
    # Both kinds must be represented, or this guard is not exercising the case
    # that motivated the sidecar.
    assert current and historical


def test_preflight_refuses_a_population_without_runtime_coverage(
    tmp_path: Path,
) -> None:
    preflight = _built_preflight(tmp_path, runtime_support=_runtime_support(permitted=False))

    assert preflight.complete is False
    assert any("no provider-backed runtime" in failure for failure in preflight.failures)


def test_preflight_refuses_a_failed_capacity_check(tmp_path: Path) -> None:
    preflight = _built_preflight(
        tmp_path,
        environment={
            "apfs": True,
            "submodules_clean": True,
            "capacity_ok": False,
            "visual_judge_fake": False,
        },
    )

    assert preflight.complete is False
    assert "formal environment check failed: capacity_ok" in preflight.failures


def test_the_full_matrix_runs_and_aggregates(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)
    seen: list[tuple[str, int, str, str]] = []

    def executor(context):
        key = context.key
        seen.append((key.task_id, key.seed, key.arm, key.version))
        return _cell_report(context)

    run = _run(preflight, executor)

    assert run.stop_reason is None
    assert run.complete is True
    assert len(seen) == FORMAL_TRIAL_COUNT
    assert len(run.reports) == FORMAL_TRIAL_COUNT
    assert run.aggregate is not None
    assert run.aggregate["complete"] is True
    assert run.observed_p2p_p95_s == pytest.approx(2.0)


def test_state_buggy_always_precedes_fixed_inside_a_group(tmp_path: Path) -> None:
    """Checkpoint reuse is only measurable if the buggy cell runs first."""

    preflight = _built_preflight(tmp_path)
    order: dict[tuple[str, int], list[tuple[str, str]]] = {}

    def executor(context):
        key = context.key
        order.setdefault((key.task_id, key.seed), []).append((key.arm, key.version))
        return _cell_report(context)

    _run(preflight, executor, outer_workers=1)

    assert len(order) == FORMAL_TASK_COUNT * 3
    for cells in order.values():
        state_versions = [
            version for arm, version in cells if arm == "lazarus_state"
        ]
        assert state_versions == ["buggy", "fixed"]


def test_a_fixed_state_cell_reusing_another_checkpoint_fails_its_group(
    tmp_path: Path,
) -> None:
    preflight = _built_preflight(tmp_path)
    target = str(TASKS[0]["id"])

    def executor(context):
        key = context.key
        if (
            key.task_id == target
            and key.arm == "lazarus_state"
            and key.version == "fixed"
        ):
            return _cell_report(context, checkpoint_sha256="9" * 64)
        return _cell_report(context)

    run = _run(preflight, executor, outer_workers=1)

    assert run.complete is False
    assert run.aggregate is None
    failed = [group for group in run.groups if not group.complete]
    assert {group.task_id for group in failed} == {target}
    assert any("must reuse the same checkpoint" in f for g in failed for f in g.failures)
    # Only the three seeds of the offending row are lost; every other group
    # still produced its four cells, so one bad row does not void the run.
    assert len(failed) == 3
    assert len(run.reports) < FORMAL_TRIAL_COUNT
    for group in run.groups:
        if group.task_id != target:
            assert group.complete is True
            assert len(group.reports) == 4


def test_provenance_drift_stops_the_whole_run(tmp_path: Path) -> None:
    """A drifted artifact invalidates every group, not just its own."""

    preflight = _built_preflight(tmp_path)
    calls: list[str] = []

    def executor(context):
        calls.append(context.key.invocation_id())
        return _cell_report(context, provenance_sha256="0" * 64)

    run = _run(preflight, executor, outer_workers=1)

    assert run.stopped_early is True
    assert run.complete is False
    assert "provenance SHA drift" in str(run.stop_reason)
    assert run.aggregate is None
    assert len(calls) == 1


def test_completed_groups_are_checkpointed_before_the_next_group_starts(
    tmp_path: Path,
) -> None:
    preflight = _built_preflight(tmp_path)
    progress = tmp_path / "reports" / "formal-run-index.json"
    calls: list[str] = []
    snapshots: list[dict] = []

    def executor(context):
        if len(calls) == 4:
            snapshots.append(json.loads(progress.read_text(encoding="utf-8")))
        calls.append(context.key.invocation_id())
        return _cell_report(context)

    run = _run(
        preflight,
        executor,
        outer_workers=1,
        progress_index_path=progress,
    )

    assert run.complete is True
    assert len(snapshots) == 1
    assert snapshots[0]["complete"] is False
    assert snapshots[0]["observed_cells"] == 4
    assert len(snapshots[0]["groups"]) == 1
    assert snapshots[0]["groups"][0]["status"] == "ok"
    assert len(snapshots[0]["groups"][0]["cells"]) == 4


def test_an_incomplete_preflight_never_reaches_the_executor(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path, runtime_support=_runtime_support(permitted=False))

    run = _run(preflight, lambda _context: pytest.fail("executor must not run"))

    assert run.stopped_early is True
    assert run.stop_reason == "formal preflight is incomplete"


def test_a_fake_visual_judge_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_FAKE", "1")
    preflight = _built_preflight(tmp_path)

    run = _run(preflight, lambda _context: pytest.fail("executor must not run"))

    assert run.stopped_early is True
    assert "fake visual judge" in str(run.stop_reason)


def test_an_unauthorized_manifest_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    preflight = _built_preflight(tmp_path)
    unauthorized = replace(
        preflight,
        contract=replace(preflight.contract, formal_paid_run_authorized=False),
    )

    run = _run(unauthorized, lambda _context: pytest.fail("executor must not run"))

    assert run.stop_reason == "formal paid run is not authorized"


def test_a_missing_cell_leaves_the_matrix_incomplete(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)
    target = str(TASKS[3]["id"])

    def executor(context):
        if context.key.task_id == target and context.key.seed == 1:
            raise RuntimeError("runtime exploded")
        return _cell_report(context)

    run = _run(preflight, executor, outer_workers=2)

    assert run.complete is False
    assert run.aggregate is None
    assert "did not complete" in str(run.stop_reason)
    assert any("runtime exploded" in f for g in run.groups for f in g.failures)


def test_resume_imports_only_groups_that_finished_in_full(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)
    dropped = str(TASKS[5]["id"])

    def first_executor(context):
        if context.key.task_id == dropped and context.key.arm == "lazarus_agent_s":
            raise RuntimeError("interrupted")
        return _cell_report(context)

    first = _run(preflight, first_executor, outer_workers=2)
    assert first.complete is False

    index_path = write_lazarus_formal_index(
        first, preflight, tmp_path / "reports" / "formal-run-index.json"
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    ok_groups = [row for row in index["groups"] if row["status"] == "ok"]
    assert len(ok_groups) == FORMAL_TASK_COUNT * 3 - 3
    assert all(row["task_id"] != dropped for row in ok_groups)

    replayed: set[tuple[str, int]] = set()

    def second_executor(context):
        replayed.add((context.key.task_id, context.key.seed))
        return _cell_report(context)

    second = _run(preflight, second_executor, outer_workers=2, resume_index=index)

    assert second.complete is True
    # Exactly the three interrupted groups are re-run, and each is re-run whole
    # because a partially finished group has no checkpoint left to reuse.
    assert replayed == {(dropped, seed) for seed in (0, 1, 2)}
    assert len(second.reports) == FORMAL_TRIAL_COUNT


def test_resume_rejects_an_index_from_another_harness(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)

    def executor(context):
        return _cell_report(context)

    run = _run(preflight, executor)
    index_path = write_lazarus_formal_index(
        run, preflight, tmp_path / "reports" / "formal-run-index.json"
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["harness_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="another harness"):
        _run(preflight, executor, resume_index=index)


def test_resume_rejects_a_snapshot_whose_bytes_changed(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)

    def executor(context):
        return _cell_report(context)

    run = _run(preflight, executor)
    index_path = write_lazarus_formal_index(
        run, preflight, tmp_path / "reports" / "formal-run-index.json"
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    tampered = Path(index["groups"][0]["cells"][0]["path"])
    payload = json.loads(tampered.read_text(encoding="utf-8"))
    payload["goal_success"] = not payload["goal_success"]
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match its recorded digest"):
        _run(preflight, executor, resume_index=index)


def test_the_run_report_records_group_outcomes_without_task_text(
    tmp_path: Path,
) -> None:
    preflight = _built_preflight(tmp_path)

    run = _run(preflight, lambda context: _cell_report(context))
    destination = write_lazarus_formal_run(
        run, preflight, tmp_path / "reports" / "formal-run.json"
    )
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["schema"] == "c1-lazarus-formal-run-v1"
    assert payload["complete"] is True
    assert payload["expected_cells"] == FORMAL_TRIAL_COUNT
    assert payload["observed_cells"] == FORMAL_TRIAL_COUNT
    assert len(payload["groups"]) == FORMAL_TASK_COUNT * 3
    serialized = json.dumps(payload)
    assert "PUBLIC start" not in serialized
    assert "PUBLIC transition" not in serialized


def test_preflight_report_carries_no_task_wording(tmp_path: Path) -> None:
    preflight = _built_preflight(tmp_path)
    payload = preflight.to_report()

    assert payload["schema"] == "c1-lazarus-formal-preflight-v1"
    assert payload["expected_cells"] == FORMAL_TRIAL_COUNT
    assert payload["capacity"]["required_free_bytes"] == 4 * 2 * 1_000 + 10_000
    serialized = json.dumps(payload)
    assert "PUBLIC start" not in serialized


def test_capacity_budget_scales_with_concurrency(tmp_path: Path) -> None:
    repo = tmp_path / "games" / "luanti"
    repo.mkdir(parents=True)
    (repo / "payload.bin").write_bytes(b"x" * 4_096)
    hosts = {"luanti": {"repo": "games/luanti"}}

    single = formal_capacity_budget(
        tmp_path, outer_workers=1, hosts=hosts, headroom_bytes=0
    )
    wide = formal_capacity_budget(
        tmp_path, outer_workers=8, hosts=hosts, headroom_bytes=0
    )

    assert single.largest_host_bytes == 4_096
    assert single.required_free_bytes == 2 * 4_096
    assert wide.required_free_bytes == 8 * single.required_free_bytes


def test_p5_scale_delegates_a_paid_run_once_coverage_is_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``p5-scale`` must hand off, never run the matrix through the C1 scheduler."""

    from warptest import lazarus_formal, p5_scale

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(p5_scale, "paid_runtime_unbound_task_ids", lambda _tasks: ())
    monkeypatch.setattr(
        p5_scale, "unbound_formal_task_ids", lambda _tasks, repository_root: ()
    )
    calls: list[dict] = []

    def fake_launch(base, **kwargs):
        calls.append({"base": base, **kwargs})
        preflight = _built_preflight(tmp_path)
        run = _run(preflight, lambda context: _cell_report(context))
        return lazarus_formal.LazarusFormalLaunch(
            preflight=preflight,
            run=run,
            report_path=tmp_path / "formal-run.json",
            index_path=tmp_path / "formal-run-index.json",
        )

    monkeypatch.setattr(lazarus_formal, "launch_lazarus_formal", fake_launch)

    result = p5_scale.run_p5_scale(
        root,
        manifest_path=root / "benchmarks/p5/lazarus_manifest.yaml",
        report_dir=tmp_path / "run",
        c1_outer_worker=3,
    )

    assert len(calls) == 1
    assert calls[0]["outer_workers"] == 3
    assert result["experiment_id"] == "c1_lazarus"
    assert result["complete"] is True
    assert result["expected_cells"] == FORMAL_TRIAL_COUNT
    assert result["observed_cells"] == FORMAL_TRIAL_COUNT


def test_p5_scale_refuses_a_paid_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subset is a planning aid; paying for one would not be the registered matrix."""

    from warptest import p5_scale

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(p5_scale, "paid_runtime_unbound_task_ids", lambda _tasks: ())
    monkeypatch.setattr(
        p5_scale, "unbound_formal_task_ids", lambda _tasks, repository_root: ()
    )

    with pytest.raises(ValueError, match="whole registered matrix"):
        p5_scale.run_p5_scale(
            root,
            manifest_path=root / "benchmarks/p5/lazarus_manifest.yaml",
            seeds=1,
        )


def test_the_planned_matrix_is_grouped_four_cells_per_task_and_seed() -> None:
    plan = build_lazarus_trial_plan(
        [str(task["id"]) for task in TASKS],
        seeds=(0, 1, 2),
        model=DEFAULT_LAZARUS_MODEL,
        formal=True,
    )

    groups: dict[tuple[str, int], int] = {}
    for key in plan.keys:
        groups[(key.task_id, key.seed)] = groups.get((key.task_id, key.seed), 0) + 1

    assert plan.complete is True
    assert len(groups) == FORMAL_TASK_COUNT * 3
    assert set(groups.values()) == {4}
