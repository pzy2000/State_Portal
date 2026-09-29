from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from warptest import p5_scale
from warptest.lazarus_manifest import (
    validate_lazarus_manifest,
    validate_lazarus_phase0_manifest,
)
from warptest.lazarus_roster import (
    FORMAL_PAID_RUN_KEY,
    FORMAL_TASK_COUNT,
    FORMAL_TRIAL_COUNT,
)


def _manifest(task_path: str) -> dict:
    return {
        "schema_version": 1,
        "experiment_id": "c1_lazarus",
        "task_schema": "c1_lazarus_v1",
        "tasks": task_path,
        "formal_task_count": FORMAL_TASK_COUNT,
        "formal_trial_count": FORMAL_TRIAL_COUNT,
        "formal_benchmarks": ["luanti", "jupyterlab", "openra"],
        "arms": {
            "lazarus_state": {"method": "state_loop_semantic"},
            "lazarus_agent_s": {"method": "agent_s_from_start"},
        },
        "versions": ["buggy", "fixed"],
        "models": ["gpt-5.6-terra"],
        "api": {"provider": "relay"},
        "model_request_profiles": {"gpt-5.6-terra": "thinking_disabled"},
        "observer_model": "claude-sonnet-5@relay",
        "grounder_model": "decision_model",
        "judge_model": "qwen3-vl-flash@relay",
        "provider_timeout_s": 900,
        "decision_step_budget": 100,
        "budget_ms": 2_400_000,
        "per_trial_p2p_timeout_s": 300,
        "seeds": 3,
        "scheduler": {
            "rotation_rule": "buggy_before_fixed_state_v1",
            "ready_scheduler": True,
            "unity_canary": False,
        },
        "calibration": {"schema": "lazarus-calibration-v1"},
        "phase0": {
            "task_ids": ["lz-luanti-dry-00", "lz-openra-dry-00"],
            "seeds": [0],
            "serial": True,
            "trial_count": 8,
            "stop_on_failure": True,
        },
        "long_chain": {
            "excluded_from_formal": True,
            "min_k": 5,
            "task_ids": {
                "luanti": "lz-luanti-dry-00",
                "openra": "lz-openra-dry-00",
                "jupyterlab": "lz-jupyterlab-dry-00",
            },
        },
        "authorization": {
            "phase0_paid_pilot": True,
            FORMAL_PAID_RUN_KEY: True,
        },
        "stats": {
            "bootstrap_iterations": 10_000,
            "ci": 0.95,
            "rng_seed": 1234,
            "mcnemar": "exact",
            "cluster_unit": "task_id",
            "multiple_testing": "holm_h1_h3",
        },
    }


def _phase0_manifest(task_path: str) -> dict:
    value = _manifest(task_path)
    value.update(
        {
            "experiment_id": "c1_lazarus_phase0",
            "phase0_only": True,
            # The pilot registers its own decision model, independent of the
            # formal matrix pin above.
            "models": ["gpt-5.6-luna"],
            "model_request_profiles": {"gpt-5.6-luna": "thinking_disabled"},
            "phase0": {
                "task_ids": [
                    "lz-luanti-11919-vector-from-string",
                    "lz-openra-21453-hotkey-ready-building",
                ],
                "seeds": [0],
                "serial": True,
                "trial_count": 8,
                "stop_on_failure": True,
            },
            "scheduler": {
                "rotation_rule": "buggy_before_fixed_state_v1",
                "ready_scheduler": True,
                "unity_canary": True,
            },
            "long_chain": {"status": "deferred_until_formal_catalog"},
            "authorization": {
                "phase0_paid_pilot": True,
                FORMAL_PAID_RUN_KEY: False,
            },
        }
    )
    return value


def _task_ids() -> list[str]:
    return [
        *(f"lz-luanti-dry-{index:02d}" for index in range(4)),
        *(f"lz-openra-dry-{index:02d}" for index in range(4)),
        *(f"lz-jupyterlab-dry-{index:02d}" for index in range(16)),
        *(f"lz-luanti-extra-{index:02d}" for index in range(FORMAL_TASK_COUNT - 24)),
    ]


def _catalog_header(path: Path, count: int = FORMAL_TASK_COUNT) -> list[str]:
    task_ids = _task_ids()[:count]
    formal_benchmarks = ("luanti", "jupyterlab", "openra")
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "task_schema": "c1_lazarus_v1",
                "formal_population": True,
                "benchmark_hosts": {
                    benchmark: {} for benchmark in formal_benchmarks
                },
                "tasks": [
                    {
                        "id": task_id,
                        "benchmark": next(
                            benchmark
                            for benchmark in formal_benchmarks
                            if task_id.startswith(f"lz-{benchmark}-")
                        ),
                    }
                    for task_id in task_ids
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return task_ids


def test_formal_run_is_refused_while_a_row_has_no_executable_probe(
    tmp_path: Path,
) -> None:
    """Paying for a cell whose probe cannot be dispatched buys no verdict."""

    catalog = tmp_path / "nested" / "catalog.yaml"
    task_ids = _catalog_header(catalog)
    with pytest.raises(ValueError, match="no executable probe"):
        validate_lazarus_manifest(
            _manifest(str(catalog)),
            catalog_task_ids=task_ids,
            unbound_task_ids=("lz-luanti-dry-00",),
        )


def test_lazarus_manifest_locks_models_arms_versions_costs_and_authority() -> None:
    task_ids = tuple(_task_ids())

    contract = validate_lazarus_manifest(
        _manifest("benchmarks/p5/c1_lazarus_tasks.yaml"),
        catalog_task_ids=task_ids,
    )

    assert contract.task_ids == task_ids
    assert contract.model == "gpt-5.6-terra@relay"
    assert contract.arms == ("lazarus_state", "lazarus_agent_s")
    assert contract.versions == ("buggy", "fixed")
    assert contract.seeds == (0, 1, 2)
    assert contract.formal_trial_count == FORMAL_TRIAL_COUNT
    assert contract.phase0_paid_pilot_authorized is True
    assert contract.formal_paid_run_authorized is True
    assert contract.phase0_task_ids == (
        "lz-luanti-dry-00",
        "lz-openra-dry-00",
    )
    assert contract.long_chain_task_ids == (
        "lz-luanti-dry-00",
        "lz-openra-dry-00",
        "lz-jupyterlab-dry-00",
    )


def test_phase0_manifest_is_explicitly_nonformal_but_locks_the_eight_cells() -> None:
    contract = validate_lazarus_phase0_manifest(
        _phase0_manifest("benchmarks/p5/c1_lazarus_phase0_tasks.yaml"),
        catalog_task_ids=(
            "lz-luanti-11919-vector-from-string",
            "lz-openra-21453-hotkey-ready-building",
        ),
    )

    assert contract.task_ids == (
        "lz-luanti-11919-vector-from-string",
        "lz-openra-21453-hotkey-ready-building",
    )
    assert contract.model == "gpt-5.6-luna@relay"
    assert contract.grounder_model == "gpt-5.6-luna@relay"
    assert contract.phase0_trial_count == 8
    assert contract.formal_paid_run_authorized is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["arms"].update(c1_state={}),
            "arms",
        ),
        (
            lambda value: value.update(versions=["fixed", "buggy"]),
            "versions",
        ),
        (
            lambda value: value["model_request_profiles"].update(
                {"gpt-5.6-terra": "high"}
            ),
            "thinking_disabled",
        ),
        (lambda value: value.update(formal_trial_count=FORMAL_TRIAL_COUNT - 1), str(FORMAL_TRIAL_COUNT)),
        (
            lambda value: value["authorization"].update(
                {FORMAL_PAID_RUN_KEY: False}
            ),
            "must be authorized",
        ),
        (
            # A grant scoped to the previous, smaller matrix must not carry
            # over to the cells it never covered.
            lambda value: value["authorization"].update(
                {"formal_288_paid_run": True}
            ),
            "superseded paid-run key",
        ),
        (
            lambda value: value.update(
                formal_benchmarks=["luanti", "jupyterlab", "jynew"]
            ),
            "formal_benchmarks",
        ),
        (
            # Blender is a candidate source, not a formal v1 host.
            lambda value: value.update(
                formal_benchmarks=["luanti", "blender", "jupyterlab", "openra"]
            ),
            "formal_benchmarks",
        ),
        (lambda value: value["phase0"].update(trial_count=9), "Phase 0"),
        (lambda value: value["long_chain"].update(min_k=4), "long-chain"),
        (lambda value: value["stats"].update(bootstrap_iterations=999), "statistics"),
    ],
)
def test_lazarus_manifest_rejects_drift(mutate, message: str) -> None:
    value = copy.deepcopy(_manifest("benchmarks/p5/c1_lazarus_tasks.yaml"))
    mutate(value)

    with pytest.raises(ValueError, match=message):
        validate_lazarus_manifest(
            value,
            catalog_task_ids=tuple(
                _task_ids()
            ),
        )


def test_committed_formal_manifest_plans_the_registered_matrix() -> None:
    """The on-disk manifest and catalog must agree without a synthetic fixture."""

    root = Path(__file__).resolve().parents[1]

    result = p5_scale.run_p5_scale(
        root,
        manifest_path=root / "benchmarks/p5/lazarus_manifest.yaml",
        dry_run=True,
    )

    plan = result["plan"]
    assert result["experiment_id"] == "c1_lazarus"
    assert plan["complete"] is True
    assert plan["formal"] is True
    assert plan["failures"] == []
    assert plan["trial_count"] == FORMAL_TRIAL_COUNT
    assert len(plan["task_ids"]) == FORMAL_TASK_COUNT
    assert plan["arms"] == ["lazarus_state", "lazarus_agent_s"]
    assert plan["versions"] == ["buggy", "fixed"]
    assert plan["seeds"] == [0, 1, 2]
    assert plan["models"] == ["gpt-5.6-terra@relay"]
    assert plan["rotation_rule"] == "buggy_before_fixed_state_v1"


def test_committed_formal_manifest_registers_three_benchmarks() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load(
        (root / "benchmarks/p5/lazarus_manifest.yaml").read_text(encoding="utf-8")
    )
    catalog = yaml.safe_load(
        (root / "benchmarks/p5/c1_lazarus_tasks.yaml").read_text(encoding="utf-8")
    )

    expected = ["luanti", "jupyterlab", "openra"]
    assert manifest["formal_benchmarks"] == expected
    assert list(catalog["benchmark_hosts"]) == expected
    assert {row["benchmark"] for row in catalog["tasks"]} == set(expected)
    # Every long-chain row must be a real catalog row, one per formal host.
    catalog_ids = {row["id"] for row in catalog["tasks"]}
    long_chain = manifest["long_chain"]["task_ids"]
    assert list(long_chain) == ["luanti", "openra", "jupyterlab"]
    assert set(long_chain.values()) <= catalog_ids
    assert len(set(long_chain.values())) == 3
    for benchmark, task_id in long_chain.items():
        assert task_id.startswith(f"lz-{benchmark}-")


def test_p5_scale_recognizes_lazarus_manifest_and_dry_runs_the_formal_matrix(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "benchmarks/p5/c1_lazarus_tasks.yaml"
    task_ids = _catalog_header(tasks_path)
    manifest_path = tmp_path / "benchmarks/p5/lazarus_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            _manifest("benchmarks/p5/c1_lazarus_tasks.yaml"), sort_keys=False
        ),
        encoding="utf-8",
    )

    result = p5_scale.run_p5_scale(
        tmp_path,
        manifest_path=manifest_path,
        dry_run=True,
    )

    assert result["experiment_id"] == "c1_lazarus"
    assert result["dry_run"] is True
    assert result["plan"]["complete"] is True
    assert result["plan"]["formal"] is True
    assert result["plan"]["trial_count"] == FORMAL_TRIAL_COUNT
    assert result["plan"]["task_ids"] == task_ids
    assert result["plan"]["arms"] == ["lazarus_state", "lazarus_agent_s"]
    assert p5_scale.C1_FORMAL_PAPER_ARMS == p5_scale.C1_ARMS
    assert tuple(p5_scale.C1_ARMS) == (
        "c1_agent_s",
        "c1_agent_s_synth",
        "c1_state",
        "c1_agent_scalecua",
        "c1_agent_scalecua_synth",
    )


def test_p5_scale_lazarus_pilot_selection_dry_runs_eight_cells(tmp_path: Path) -> None:
    tasks_path = tmp_path / "benchmarks/p5/c1_lazarus_tasks.yaml"
    task_ids = _catalog_header(tasks_path)
    manifest_path = tmp_path / "benchmarks/p5/lazarus_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            _manifest("benchmarks/p5/c1_lazarus_tasks.yaml"), sort_keys=False
        ),
        encoding="utf-8",
    )

    result = p5_scale.run_p5_scale(
        tmp_path,
        manifest_path=manifest_path,
        task_ids=task_ids[:2],
        seeds=1,
        dry_run=True,
    )

    assert result["plan"]["formal"] is False
    assert result["plan"]["trial_count"] == 8


def test_p5_scale_rejects_a_nonformal_catalog_before_dry_run(tmp_path: Path) -> None:
    tasks_path = tmp_path / "benchmarks/p5/c1_lazarus_tasks.yaml"
    _catalog_header(tasks_path)
    payload = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    payload.pop("formal_population")
    tasks_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "benchmarks/p5/lazarus_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            _manifest("benchmarks/p5/c1_lazarus_tasks.yaml"), sort_keys=False
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="formal_population"):
        p5_scale.run_p5_scale(
            tmp_path,
            manifest_path=manifest_path,
            dry_run=True,
        )


def test_p5_scale_phase0_manifest_cannot_expand_into_the_formal_matrix() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "benchmarks/p5/lazarus_phase0_manifest.yaml"

    result = p5_scale.run_p5_scale(
        root,
        manifest_path=manifest_path,
        dry_run=True,
    )

    assert result["experiment_id"] == "c1_lazarus_phase0"
    assert result["plan"]["phase0_only"] is True
    assert result["plan"]["trial_count"] == 8
    assert result["scheduler"]["maximum_task_concurrency"] == 1
