from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from warptest import lazarus_luanti_probe as target
from warptest.lazarus_luanti_states import LuantiLazarusStates


def _states(**overrides: Any) -> LuantiLazarusStates:
    fields: dict[str, Any] = {
        "change_number": 14757,
        "fixed": {"result": "0,-0", "ok": True},
        "buggy": {"result": "1,-1", "ok": False},
        "source_artifact_ids": ("candidate_gate10_server_side_oracle/luanti/14757/v1",),
        "layout": "fixed_observation_pair",
        "fail_to_pass": ("math round()",),
        "pass_to_pass": ("formspec_escape escapes",),
        "admission_id": "candidate_formal_freeze_admission/luanti/14757/gap-closed-v1",
    }
    fields.update(overrides)
    return LuantiLazarusStates(**fields)


def _binding(source: str = target.FROZEN_SOURCE, **overrides: Any) -> target.LuantiProbeBinding:
    states = overrides.pop("states", _states())
    fields: dict[str, Any] = {
        "task_id": "lz-luanti-14757-math-round",
        "change_number": 14757,
        "source": source,
        "replay_steps": ("restore_registered_start", "read_semantic_observation"),
        "states": states,
        "observe": lambda root, version: states.state(version),
    }
    fields.update(overrides)
    return target.LuantiProbeBinding(**fields)


def _audit(**overrides: Any) -> dict[str, Any]:
    audit = {
        "schema_version": 1,
        "task_id": "lz-luanti-14757-math-round",
        "candidate": {"repository_key": "luanti", "change_number": 14757},
        "variant": "fixed",
        "observation_source": target.FROZEN_SOURCE,
        "evidence_layout": "fixed_observation_pair",
        "evidence_artifact_ids": ["candidate_gate10_server_side_oracle/luanti/14757/v1"],
        "registered_replay": ["restore_registered_start"],
        "observed": {"result": "0,-0", "ok": True},
        "expected_goal": {"result": "0,-0", "ok": True},
        "expected_defect": {"result": "1,-1", "ok": False},
        "probe_errors": [],
    }
    audit.update(overrides)
    return audit


def test_probe_reports_each_version_with_its_observation_source(tmp_path: Path) -> None:
    binding = _binding()
    bindings = {binding.task_id: binding}
    fixed = target.run_luanti_lazarus_probe(
        tmp_path, task_id=binding.task_id, version="fixed", bindings=bindings
    )
    assert fixed["observation_source"] == target.FROZEN_SOURCE
    assert fixed["observed"] == {"result": "0,-0", "ok": True}
    assert target.evaluate_luanti_lazarus_audit(fixed).goal_checks_ok is True

    buggy = target.run_luanti_lazarus_probe(
        tmp_path, task_id=binding.task_id, version="buggy", bindings=bindings
    )
    verdict = target.evaluate_luanti_lazarus_audit(buggy)
    assert (verdict.goal_checks_ok, verdict.defect_signature_ok) == (False, True)


def test_live_binding_reports_the_live_adapter() -> None:
    assert _binding(target.LIVE_SOURCE).adapter == target.LIVE_ADAPTER
    assert _binding(target.FROZEN_SOURCE).adapter == target.FROZEN_ADAPTER


def test_failed_observation_is_recorded_not_raised(tmp_path: Path) -> None:
    def broken(root: Path, version: str) -> Any:
        raise RuntimeError("server reached no registered action")

    binding = _binding(observe=broken)
    audit = target.run_luanti_lazarus_probe(
        tmp_path,
        task_id=binding.task_id,
        version="fixed",
        bindings={binding.task_id: binding},
    )
    assert audit["probe_errors"] == ["RuntimeError: server reached no registered action"]
    assert target.evaluate_luanti_lazarus_audit(audit).complete is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"task_id": "luanti-14757"},
        {"variant": "candidate"},
        {"observation_source": "trust_me"},
        {"evidence_artifact_ids": []},
        {"registered_replay": []},
        {"probe_errors": "none"},
        {"observed": {}},
        {"expected_goal": {"same": 1}, "expected_defect": {"same": 1}},
    ],
)
def test_audit_fails_closed_without_evidence(overrides: dict[str, Any]) -> None:
    audit = target.evaluate_luanti_lazarus_audit(_audit(**overrides))
    assert audit.complete is False
    assert audit.goal_checks_ok is None
    assert audit.failures


def test_audit_reports_probe_errors_as_an_invariant_failure() -> None:
    audit = target.evaluate_luanti_lazarus_audit(_audit(probe_errors=["timeout"]))
    assert audit.complete is True
    assert audit.invariants_ok is False


def test_checks_projected_from_states_separate_the_two_versions(tmp_path: Path) -> None:
    binding = _binding()
    goal = target.luanti_lazarus_goal_checks(binding)
    defect = target.luanti_lazarus_defect_signature(binding)
    assert goal != defect
    assert all(check["type"] == "luanti_lazarus_state_audit" for check in goal)
    audit = target.run_luanti_lazarus_probe(
        tmp_path,
        task_id=binding.task_id,
        version="fixed",
        bindings={binding.task_id: binding},
    )
    assert target.evaluate_luanti_lazarus_checks(goal, audit)[0] is True
    assert target.evaluate_luanti_lazarus_checks(defect, audit)[0] is False


def test_probe_rejects_unknown_versions_and_tasks(tmp_path: Path) -> None:
    binding = _binding()
    with pytest.raises(target.LuantiLazarusProbeError):
        target.run_luanti_lazarus_probe(
            tmp_path,
            task_id=binding.task_id,
            version="candidate",
            bindings={binding.task_id: binding},
        )
    with pytest.raises(target.LuantiLazarusProbeError):
        target.run_luanti_lazarus_probe(
            tmp_path, task_id="lz-luanti-0-absent", version="fixed", bindings={}
        )


def test_label_states_project_to_an_observation_check(tmp_path: Path) -> None:
    binding = _binding(
        states=_states(fixed="goal_iconv_state_reset", buggy="defect_iconv_state_regression")
    )
    audit = target.run_luanti_lazarus_probe(
        tmp_path,
        task_id=binding.task_id,
        version="buggy",
        bindings={binding.task_id: binding},
    )
    goal = target.luanti_lazarus_goal_checks(binding)
    defect = target.luanti_lazarus_defect_signature(binding)
    assert goal[0]["field"] == "observation"
    assert target.evaluate_luanti_lazarus_checks(defect, audit)[0] is True
    assert target.evaluate_luanti_lazarus_checks(goal, audit)[0] is False
