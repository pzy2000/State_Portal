from __future__ import annotations

import contextlib
import hashlib
import json
import signal
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from warptest import p5_aggregate
from warptest.runner import load_spec
from warptest.state_loop import (
    StateLoopTask,
    _native_state_benchmark_errors,
    _run_openra_gameplay_task,
    _run_openra_settings_task,
    _run_unity_checkpoint_pair,
    _run_unity_checkpoint_task,
)
from warptest.task_schema import PublicIssue


def _task(task_id: str, benchmark: str) -> StateLoopTask:
    return StateLoopTask(
        issue=PublicIssue(
            task_id,
            benchmark,
            f"games/{benchmark}",
            "perform the registered public transition",
            tags=["c1"],
        ),
        oracle_spec="oracle.yaml",
        state_description="registered start and goal",
    )


def _result(task: StateLoopTask) -> dict[str, object]:
    return {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "seed": 7,
        "result": False,
        "iterations": [],
        "visual_grading_started": False,
    }


def _checkpoint(model_spec: object) -> SimpleNamespace:
    return SimpleNamespace(
        generated_spec={
            "name": "public scaffold that must never be injected",
            "benchmark": "scaffold",
            "target": {},
            "checkpoint": {},
            "actions": [],
            "assertions": [],
        },
        model_generated_spec=model_spec,
        used_public_scaffold=True,
    )


@pytest.mark.parametrize(
    ("benchmark", "task_id", "registered_path", "wrong_path"),
    [
        (
            "OpenRA",
            "openra-target-lines-automatic",
            "specs/openra/c1-target-lines-automatic.yaml",
            "specs/openra/c1-viewport-edge-scroll.yaml",
        ),
        (
            "jynew",
            "jynew-save-restore",
            "specs/jynew/save-restore.yaml",
            "specs/jynew/submap-enter.yaml",
        ),
        (
            "pal3",
            "pal3-scene-restore",
            "specs/pal3/scene-restore.yaml",
            "specs/pal3/scene-transition.yaml",
        ),
    ],
)
def test_native_state_candidate_is_no_longer_pre_rejected_for_differing_from_oracle(
    benchmark: str,
    task_id: str,
    registered_path: str,
    wrong_path: str,
) -> None:
    """A spec that differs from the oracle must be measured, not pre-rejected.

    The old gate required byte-equality with the registered oracle and returned
    before the runtime booted, so such a trial produced no Start datapoint at
    all. The goal is now graded by the oracle's own assertions and the start is
    re-probed against the oracle target on the live session, so divergence is
    decided on evidence instead.
    """

    root = Path(__file__).resolve().parents[1]
    registered = load_spec(root / registered_path)
    wrong = load_spec(root / wrong_path)
    task = _task(task_id, benchmark)

    assert (
        _native_state_benchmark_errors(
            registered, oracle_spec=registered, task=task
        )
        == []
    )
    # Same game, different registered task: allowed through to the Start gate.
    assert (
        _native_state_benchmark_errors(wrong, oracle_spec=registered, task=task)
        == []
    )

    disguised_number = deepcopy(registered)
    if benchmark == "OpenRA":
        disguised_number["target"]["settings"]["Game"][
            "IntroductionPromptVersion"
        ] = 999.0
    elif benchmark == "jynew":
        disguised_number["target"]["player_level"] = 5.0
    else:
        disguised_number["target"]["money"] = 300.0
    assert (
        _native_state_benchmark_errors(
            disguised_number, oracle_spec=registered, task=task
        )
        == []
    )

    # A candidate written for a different game is still rejected outright.
    cross_benchmark = deepcopy(registered)
    cross_benchmark["benchmark"] = "2048"
    assert (
        "generated_spec benchmark does not match the registered task"
        in _native_state_benchmark_errors(
            cross_benchmark, oracle_spec=registered, task=task
        )
    )


@pytest.mark.parametrize(
    ("task_benchmark", "spec_benchmark"),
    [
        ("blender", "Blender"),
        ("libreoffice", "LibreOffice"),
        ("openra", "OpenRA"),
        ("pal3.unity", "pal3"),
    ],
)
def test_registered_benchmark_alias_is_not_a_cross_benchmark_spec(
    task_benchmark: str, spec_benchmark: str
) -> None:
    """A case or alias difference must not read as a spec for another game.

    The comparison used the evidence-label helper, which preserves the registered
    display casing and returns unknown ids unchanged, so a spec saying "Blender"
    mismatched a task registered as "blender". The state arm scored a fair zero on
    specs the synthesized GUI arm accepted and graded from the same shared
    checkpoint, which is precisely the cross-arm asymmetry the shared acceptance
    exists to remove.
    """

    task = _task(f"{task_benchmark}-alias", task_benchmark)
    spec = {
        "name": "candidate",
        "benchmark": spec_benchmark,
        "target": {"kind": "scene"},
        "checkpoint": {"allowed_strategies": ["x"]},
        "actions": [{"type": "x"}],
        "assertions": [{"type": "y"}],
    }

    assert (
        _native_state_benchmark_errors(spec, oracle_spec={"target": {}}, task=task)
        == []
    )


@pytest.mark.parametrize(
    "model_spec",
    [
        None,
        {
            "name": "wrong benchmark",
            "benchmark": "jynew",
            "target": {"kind": "synthesized_state"},
            "checkpoint": {
                "allowed_strategies": ["jynew_utility_save_validate"],
                "expected_validity": "save-restorable",
            },
            "actions": [
                {"type": "jynew_save", "save_index": 0},
                {"type": "jynew_load_save", "save_index": 0},
            ],
            "assertions": [{"type": "no_jynew_utility_errors"}],
        },
    ],
)
def test_openra_state_rejects_missing_or_incompatible_model_spec_before_runtime(
    tmp_path: Path,
    model_spec: object,
) -> None:
    task = _task("openra-target-lines", "OpenRA")
    agent = SimpleNamespace(
        synthesize=lambda *_args, **_kwargs: _checkpoint(model_spec)
    )
    result = _result(task)

    with (
        patch(
            "warptest.state_loop._ensure_openra_settings_goldens",
            side_effect=AssertionError("grader must not start"),
        ),
        patch(
            "warptest.state_loop.OpenRASettingsSession",
            side_effect=AssertionError("runtime must not start"),
        ),
    ):
        actual = _run_openra_settings_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            {},
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "task",
            seed=7,
        )

    assert actual["result"] is False
    assert actual["visual_grading_started"] is False
    assert actual["injected_spec_source"] == "none"
    assert actual["used_public_scaffold"] is True
    assert actual["runtime_started"] is False
    assert actual["seed_evidence"]["source"] == "runtime_not_started"
    assert not (tmp_path / "task" / "v1" / "spec.yaml").exists()


def test_openra_gameplay_state_rejects_missing_model_spec_before_runtime(
    tmp_path: Path,
) -> None:
    task = _task("openra-allied-medium-tank-tech-chain", "OpenRA")
    agent = SimpleNamespace(
        synthesize=lambda *_args, **_kwargs: _checkpoint(None)
    )
    result = _result(task)

    with (
        patch(
            "warptest.state_loop._registered_desktop_state_goldens",
            side_effect=AssertionError("grader must not start"),
        ),
        patch(
            "warptest.state_loop.OpenRAGameplaySession",
            side_effect=AssertionError("runtime must not start"),
        ),
    ):
        actual = _run_openra_gameplay_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            {},
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "task",
            seed=7,
        )

    assert actual["status"] == "failure"
    assert actual["result"] is False
    assert actual["visual_grading_started"] is False
    assert actual["runtime_started"] is False
    assert actual["injected_spec_source"] == "none"


def test_openra_gameplay_state_executes_registered_transition_in_live_session(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    oracle_path = root / "specs/openra/c1-allied-medium-tank-tech-chain.yaml"
    oracle_spec = load_spec(oracle_path)
    generated_spec = deepcopy(oracle_spec)
    generated_spec.pop("evaluation", None)
    generated_spec.pop("visual_oracle", None)
    task = _task("openra-allied-medium-tank-tech-chain", "OpenRA")
    task.issue.tags.append("deep-chain")
    agent = SimpleNamespace(
        synthesize=lambda *_args, **_kwargs: _checkpoint(deepcopy(generated_spec))
    )
    result = _result(task)
    calls: list[tuple[object, object]] = []

    class FakeGrade:
        ok = True
        status = "success"
        detail = {"checks": [], "errors": [], "world_tick": 12}

    class FakeRuntime:
        hold_pid = 1234

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeRuntime:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def grade_target(self, spec: object) -> FakeGrade:
            calls.append(("target", spec))
            return FakeGrade()

        def execute_actions_and_grade(
            self, actions: object, assertions: object
        ) -> FakeGrade:
            calls.append((actions, assertions))
            return FakeGrade()

        def screenshot(self, path: Path) -> str:
            return str(path)

        def seed_evidence(self, task_id: str) -> dict[str, object]:
            return {
                "observed": True,
                "process_alive_at_observation": True,
                "seed": 7,
                "task_id": task_id,
                "benchmark": "OpenRA",
                "process_id": 1234,
            }

    goldens = SimpleNamespace(
        before=str(tmp_path / "golden-before.png"),
        after=str(tmp_path / "golden-after.png"),
        after_description="goal",
        after_rubric="rubric",
    )
    with (
        patch(
            "warptest.state_loop._registered_desktop_state_goldens",
            return_value=goldens,
        ),
        patch(
            "warptest.state_loop.oracle_goldens_report_payload",
            return_value={},
        ),
        patch(
            "warptest.state_loop.OpenRAGameplaySession",
            FakeRuntime,
        ),
        patch(
            "warptest.state_loop.runtime_lease",
            return_value=contextlib.nullcontext(),
        ),
        patch(
            "warptest.state_loop.native_runtime_lock",
            return_value=contextlib.nullcontext(),
        ),
        patch(
            "warptest.state_loop._informative_screenshot",
            return_value=True,
        ),
        patch(
            "warptest.state_loop.run_visual_pair_audit",
            return_value={"result": True},
        ),
        # The state arm now suspends the live runtime for the Start judge, so the
        # fake runtime's pid must not reach the real kernel.
        patch("warptest.c1_runtime_hold.os.kill") as kill,
    ):
        actual = _run_openra_gameplay_task(
            root,
            task,
            agent,
            root / "games/OpenRA",
            oracle_spec,
            oracle_path,
            result,
            task_dir=tmp_path / "task",
            seed=7,
        )

    assert calls, actual.get("reason")
    # The arm's own restore is graded first, then the same live match is
    # re-validated against the registered oracle target before the transition.
    assert calls[0][0] == "target"
    assert calls[0][1]["target"] == oracle_spec["target"]
    assert calls[1] == ("target", oracle_spec)
    assert calls[2] == (oracle_spec["actions"], oracle_spec["assertions"])
    assert actual["status"] == "success"
    assert actual["runtime_started"] is True
    assert (
        actual["seed_evidence"]["pair_semantics"]
        == "same_persistent_registered_gameplay_process"
    )
    # Start is graded against a suspended runtime, the same as the GUI arms.
    assert actual["start_grading_runtime_hold"] == {
        "required": True,
        "held": True,
        "released": True,
        "backend": "sigstop",
        "held_pid": FakeRuntime.hold_pid,
    }
    assert [call.args[1] for call in kill.call_args_list] == [
        signal.SIGSTOP,
        signal.SIGCONT,
    ]


@pytest.mark.parametrize(
    "model_spec",
    [
        None,
        {
            "name": "wrong Unity operation",
            "benchmark": "jynew",
            "target": {"kind": "synthesized_state"},
            "checkpoint": {
                "allowed_strategies": ["jynew_utility_save_validate"],
                "expected_validity": "save-restorable",
            },
            "actions": [{"type": "pal3_save", "save_index": 0}],
            "assertions": [{"type": "no_jynew_utility_errors"}],
        },
    ],
)
def test_unity_state_rejects_missing_or_incompatible_model_spec_before_runtime(
    tmp_path: Path,
    model_spec: object,
) -> None:
    task = _task("jynew-save-restore", "jynew")
    agent = SimpleNamespace(
        synthesize=lambda *_args, **_kwargs: _checkpoint(model_spec)
    )
    result = _result(task)

    with (
        patch(
            "warptest.state_loop._ensure_unity_checkpoint_goldens",
            side_effect=AssertionError("grader must not start"),
        ),
        patch(
            "warptest.state_loop._run_unity_checkpoint_pair",
            side_effect=AssertionError("runtime must not start"),
        ),
    ):
        actual = _run_unity_checkpoint_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            deepcopy(model_spec),
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "task",
            headless=False,
        )

    assert actual["result"] is False
    assert actual["visual_grading_started"] is False
    assert actual["injected_spec_source"] == "none"
    assert actual["used_public_scaffold"] is True
    assert actual["runtime_started"] is False
    assert actual["seed_evidence"]["source"] == "runtime_not_started"
    assert not (tmp_path / "task" / "v1" / "spec.yaml").exists()


@pytest.mark.parametrize(
    ("benchmark", "task_id"),
    [
        ("OpenRA", "openra-target-lines-automatic"),
        ("jynew", "jynew-save-restore"),
    ],
)
def test_native_state_synthesis_exception_keeps_explicit_no_injection_marker(
    tmp_path: Path,
    benchmark: str,
    task_id: str,
) -> None:
    task = _task(task_id, benchmark)

    def fail_synthesis(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("provider exploded")

    agent = SimpleNamespace(synthesize=fail_synthesis)
    result = _result(task)
    if benchmark == "OpenRA":
        actual = _run_openra_settings_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            {},
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "task",
            seed=7,
        )
    else:
        actual = _run_unity_checkpoint_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            {},
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "task",
            headless=False,
        )

    assert actual["injected_spec_source"] == "none"
    assert actual["runtime_started"] is False
    assert actual["seed_evidence"]["source"] == "runtime_not_started"


def test_unity_state_pair_binds_each_runtime_request_to_task_seed_and_stage(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeAdapter:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.instance = len(calls)

        def __enter__(self) -> FakeAdapter:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def run_checkpoint(
            self,
            spec: dict[str, object],
            _spec_path: Path,
            **evidence: object,
        ) -> dict[str, object]:
            calls.append({"spec": deepcopy(spec), "evidence": evidence})
            return {"status": "success"}

        def screenshot(self, name: str) -> str:
            return str(tmp_path / f"{self.instance}-{name}")

    spec = {
        "target": {"kind": "synthesized_state"},
        "actions": [{"type": "jynew_save", "save_index": 0}],
        "assertions": [{"type": "no_jynew_utility_errors"}],
    }
    with patch(
        "warptest.state_loop._unity_checkpoint_adapter",
        return_value=FakeAdapter,
    ):
        pair, *_ = _run_unity_checkpoint_pair(
            tmp_path,
            spec,
            tmp_path / "spec.yaml",
            tmp_path / "report",
            benchmark="jynew",
            headless=False,
            task_id="jynew-save-restore",
            experiment_seed=7,
        )

    assert pair["status"] == "success"
    assert len(calls) == 2
    assert calls[0]["spec"]["actions"] == []
    assert calls[0]["spec"]["assertions"] == []
    assert calls[1]["spec"] == spec
    assert calls[0]["evidence"] == {
        "evidence_task_id": "jynew-save-restore",
        "evidence_seed": 7,
        "evidence_stage": "before",
        "evidence_benchmark": "jynew",
    }
    assert calls[1]["evidence"] == {
        "evidence_task_id": "jynew-save-restore",
        "evidence_seed": 7,
        "evidence_stage": "after",
        "evidence_benchmark": "jynew",
    }


def test_unity_state_start_gate_prevents_after_process(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class FakeAdapter:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeAdapter:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def run_checkpoint(
            self,
            spec: dict[str, object],
            _spec_path: Path,
            **evidence: object,
        ) -> dict[str, object]:
            calls.append({"spec": deepcopy(spec), "evidence": evidence})
            return {"status": "success"}

        def screenshot(self, name: str) -> str:
            return str(tmp_path / name)

    spec = {
        "target": {"kind": "synthesized_state"},
        "actions": [{"type": "jynew_save", "save_index": 0}],
        "assertions": [{"type": "no_jynew_utility_errors"}],
    }
    with patch(
        "warptest.state_loop._unity_checkpoint_adapter",
        return_value=FakeAdapter,
    ):
        pair, _, after, _, after_ms = _run_unity_checkpoint_pair(
            tmp_path,
            spec,
            tmp_path / "spec.yaml",
            tmp_path / "report",
            benchmark="jynew",
            headless=False,
            task_id="jynew-save-restore",
            experiment_seed=7,
            start_gate=lambda *_args: (
                False,
                {"before": {"label": "incorrect"}},
            ),
        )

    assert pair["status"] == "start_gate_failure"
    assert len(calls) == 1
    assert calls[0]["spec"]["actions"] == []
    assert after is None
    assert after_ms == 0


def test_unity_state_runtime_exception_does_not_claim_runtime_never_started(
    tmp_path: Path,
) -> None:
    task = _task("jynew-save-restore", "jynew")
    model_spec = {
        "name": "model-produced save",
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state"},
        "checkpoint": {
            "allowed_strategies": ["jynew_utility_save_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [{"type": "jynew_save", "save_index": 0}],
        "assertions": [{"type": "no_jynew_utility_errors"}],
    }
    agent = SimpleNamespace(
        synthesize=lambda *_args, **_kwargs: _checkpoint(model_spec)
    )
    with (
        patch("warptest.state_loop._ensure_unity_checkpoint_goldens"),
        patch("warptest.state_loop.oracle_goldens_report_payload", return_value={}),
        patch(
            "warptest.state_loop._run_unity_checkpoint_pair",
            side_effect=RuntimeError("candidate process failed"),
        ),
    ):
        actual = _run_unity_checkpoint_task(
            tmp_path,
            task,
            agent,
            tmp_path / "repo",
            deepcopy(model_spec),
            tmp_path / "oracle.yaml",
            _result(task),
            task_dir=tmp_path / "task",
            headless=False,
        )

    assert actual["injected_spec_source"] == "model_generated_spec"
    assert actual["runtime_started"] is True
    assert actual["prng"] is None
    assert (
        actual["seed_evidence"]["source"]
        == "runtime_started_without_complete_pair"
    )


def _state_pair_result(benchmark: str, task_id: str) -> dict[str, object]:
    seed = 7
    normalized = benchmark.lower()
    openra = normalized == "openra"
    source = (
        "openra_state_process_pair"
        if openra
        else "unity_state_checkpoint_process_pair"
    )
    snapshot_source = (
        "openra_state_live_process"
        if openra
        else "unity_state_checkpoint_process"
    )
    algorithm = (
        "not_applicable_public_settings_pair"
        if openra
        else "not_applicable_unity_checkpoint_pair"
    )

    def snapshot(stage: str, process_id: int) -> dict[str, object]:
        if openra:
            observation = {
                "source": "openra_public_settings_yaml",
                "live": True,
                "status": "success",
                "ok": True,
                "checks": [
                    {
                        "section": "Game",
                        "key": "TargetLines",
                        "expected": "Automatic",
                        "actual": "Automatic",
                        "ok": True,
                    }
                ],
                "mismatches": [],
                "settings": {"Game": {"TargetLines": "Automatic"}},
            }
        else:
            observation = {
                "source": f"{normalized}_checkpoint_utility_report_v1",
                "live": True,
                "status": "success",
                "detail": "All checks passed.",
                "checks": [
                    {
                        "name": "fixture.utility",
                        "status": "success",
                        "detail": "fixture utility check passed",
                    }
                ],
                "screenshot_status": "success",
                "screenshot_source": "unity_capture",
            }
        fingerprint = hashlib.sha256(
            json.dumps(
                observation,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        value: dict[str, object] = {
            "schema_version": 1,
            "source": snapshot_source,
            "observed": True,
            "algorithm": algorithm,
            "seed": seed,
            "task_id": task_id,
            "benchmark": "OpenRA" if openra else normalized,
            "stage": stage,
            "process_id": process_id,
            "process_alive_at_observation": True,
            "semantic_observation": observation,
            "semantic_fingerprint": fingerprint,
            "expected_task_id": task_id,
            "expected_seed": seed,
        }
        if not openra:
            value.update(
                protocol_version="warptest-unity-checkpoint-state-v1",
                expected_stage=stage,
                expected_benchmark=normalized,
            )
        return value

    initial = snapshot("before", 111)
    final = snapshot("after", 222)
    evidence = {
        "schema_version": 1,
        "source": source,
        "observed": True,
        "algorithm": algorithm,
        "seed": seed,
        "task_id": task_id,
        "benchmark": "OpenRA" if openra else normalized,
        "pair_semantics": "separate_registered_before_after_processes",
        "initial": initial,
        "final": final,
    }
    return {
        "benchmark": benchmark,
        "task_id": task_id,
        "seed": seed,
        "runtime_started": True,
        "prng": deepcopy(final),
        "seed_evidence": evidence,
    }


def _rehash_snapshot(snapshot: dict[str, object]) -> None:
    observation = snapshot["semantic_observation"]
    snapshot["semantic_fingerprint"] = hashlib.sha256(
        json.dumps(
            observation,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("benchmark", "task_id"),
    [
        ("OpenRA", "openra-target-lines-automatic"),
        ("jynew", "jynew-save-restore"),
        ("pal3", "pal3-scene-restore"),
    ],
)
def test_native_state_pair_allows_distinct_pids_but_rejects_task_and_hash_tamper(
    benchmark: str,
    task_id: str,
) -> None:
    result = _state_pair_result(benchmark, task_id)
    assert (
        p5_aggregate._c1_state_runtime_evidence_issues(
            result,
            benchmark=benchmark,
            seed=7,
            task_id=task_id,
        )
        == []
    )

    wrong_task = deepcopy(result)
    wrong_task["seed_evidence"]["final"]["task_id"] = "wrong-task"
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        wrong_task,
        benchmark=benchmark,
        seed=7,
        task_id=task_id,
    )
    assert any("wrong task" in issue for issue in issues)

    wrong_hash = deepcopy(result)
    wrong_hash["seed_evidence"]["initial"]["semantic_observation"]["live"] = False
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        wrong_hash,
        benchmark=benchmark,
        seed=7,
        task_id=task_id,
    )
    assert any("fingerprint does not match" in issue for issue in issues)

    disguised_types = deepcopy(result)
    disguised_types["seed_evidence"]["schema_version"] = "1"
    disguised_types["seed_evidence"]["initial"]["seed"] = "7"
    disguised_types["seed_evidence"]["initial"]["process_id"] = "111"
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        disguised_types,
        benchmark=benchmark,
        seed=7,
        task_id=task_id,
    )
    assert any("schema_version" in issue for issue in issues)
    assert any("wrong seed" in issue for issue in issues)
    assert any("positive process_id" in issue for issue in issues)

    wrong_benchmark = deepcopy(result)
    wrong_benchmark["seed_evidence"]["benchmark"] = "Open-RA"
    wrong_benchmark["seed_evidence"]["initial"]["benchmark"] = "Open-RA"
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        wrong_benchmark,
        benchmark=benchmark,
        seed=7,
        task_id=task_id,
    )
    assert any("wrong benchmark" in issue for issue in issues)


def test_native_state_semantic_cross_checks_reject_rehashed_inconsistency() -> None:
    openra = _state_pair_result("OpenRA", "openra-target-lines-automatic")
    openra_initial = openra["seed_evidence"]["initial"]
    openra_initial["semantic_observation"]["settings"]["Game"][
        "TargetLines"
    ] = "Manual"
    _rehash_snapshot(openra_initial)
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        openra,
        benchmark="OpenRA",
        seed=7,
        task_id="openra-target-lines-automatic",
    )
    assert any("live settings disagree" in issue for issue in issues)

    unity = _state_pair_result("jynew", "jynew-save-restore")
    unity_initial = unity["seed_evidence"]["initial"]
    unity_initial["semantic_observation"]["detail"] = (
        "One or more checks failed."
    )
    _rehash_snapshot(unity_initial)
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        unity,
        benchmark="jynew",
        seed=7,
        task_id="jynew-save-restore",
    )
    assert any("detail disagrees" in issue for issue in issues)

    screenshot = _state_pair_result("pal3", "pal3-scene-restore")
    screenshot_initial = screenshot["seed_evidence"]["initial"]
    screenshot_initial["semantic_observation"]["screenshot_status"] = "failure"
    screenshot_initial["semantic_observation"]["screenshot_source"] = ""
    _rehash_snapshot(screenshot_initial)
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        screenshot,
        benchmark="pal3",
        seed=7,
        task_id="pal3-scene-restore",
    )
    assert any("screenshot source disagrees" in issue for issue in issues)


@pytest.mark.parametrize(
    ("benchmark", "task_id", "reported_benchmark"),
    [
        ("OpenRA", "openra-target-lines-automatic", "OpenRA"),
        ("jynew", "jynew-save-restore", "jynew"),
        ("pal3", "pal3-scene-restore", "pal3"),
    ],
)
def test_native_state_runtime_not_started_is_strictly_bound(
    benchmark: str,
    task_id: str,
    reported_benchmark: str,
) -> None:
    result = {
        "benchmark": benchmark,
        "task_id": task_id,
        "seed": 7,
        "runtime_started": False,
        "prng": None,
        "seed_evidence": {
            "schema_version": 1,
            "source": "runtime_not_started",
            "observed": False,
            "algorithm": "not_applicable",
            "seed": 7,
            "task_id": task_id,
            "benchmark": reported_benchmark,
        },
    }
    assert (
        p5_aggregate._c1_state_runtime_evidence_issues(
            result,
            benchmark=benchmark,
            seed=7,
            task_id=task_id,
        )
        == []
    )

    tampered = deepcopy(result)
    tampered["seed_evidence"].update(
        schema_version="1",
        algorithm="bad",
        seed="7",
        task_id="wrong",
        benchmark="wrong",
        initial={"claimed": "runtime"},
    )
    issues = p5_aggregate._c1_state_runtime_evidence_issues(
        tampered,
        benchmark=benchmark,
        seed=7,
        task_id=task_id,
    )
    assert any("invalid fields" in issue for issue in issues)
    assert any("schema_version" in issue for issue in issues)
    assert any("invalid algorithm" in issue for issue in issues)
    assert any("wrong seed" in issue for issue in issues)
    assert any("wrong task" in issue for issue in issues)
    assert any("wrong benchmark" in issue for issue in issues)
