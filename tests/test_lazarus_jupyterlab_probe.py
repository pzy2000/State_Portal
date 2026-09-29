from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from warptest import lazarus_jupyterlab_probe as target


def _audit(**overrides: Any) -> dict[str, Any]:
    audit = {
        "schema_version": 1,
        "task_id": "lz-jupyterlab-19227-reactive-toolbar-positions",
        "candidate": {"repository_key": "jupyterlab", "change_number": 19227},
        "variant": "fixed",
        "browser_channel": "msedge",
        "browser_version": "139.0.3405.86",
        "registered_replay": ["load_candidate_local_bundle", "read_order"],
        "observed": {"after_item_names": ["a", "b"]},
        "expected_goal": {"after_item_names": ["a", "b"]},
        "expected_defect": {"after_item_names": ["b", "a"]},
        "frames": {"before": "before.png", "after": "after.png"},
        "probe_errors": [],
    }
    audit.update(overrides)
    return audit


def _binding(**overrides: Any) -> target.JupyterLabProbeBinding:
    def observation(root: Path, variant: str) -> Mapping[str, Any]:
        return {
            "browser_channel": "msedge",
            "browser_version": "139.0.3405.86",
            "frames": {"before": "before.png", "after": "after.png"},
            "order": ["a", "b"] if variant == "fixed" else ["b", "a"],
        }

    fields: dict[str, Any] = {
        "task_id": "lz-jupyterlab-19227-reactive-toolbar-positions",
        "change_number": 19227,
        "replay_steps": ("load_candidate_local_bundle", "read_order"),
        "expected_after": {
            "fixed": {"after_item_names": ["a", "b"]},
            "buggy": {"after_item_names": ["b", "a"]},
        },
        "semantic_state": lambda data: {"after_item_names": list(data["order"])},
        "observation": observation,
    }
    fields.update(overrides)
    return target.JupyterLabProbeBinding(**fields)


def test_audit_separates_goal_from_defect_per_variant() -> None:
    fixed = target.evaluate_jupyterlab_lazarus_audit(_audit())
    assert fixed.complete is True
    assert (fixed.goal_checks_ok, fixed.defect_signature_ok) == (True, False)
    assert fixed.invariants_ok is True

    buggy = target.evaluate_jupyterlab_lazarus_audit(
        _audit(variant="buggy", observed={"after_item_names": ["b", "a"]})
    )
    assert (buggy.goal_checks_ok, buggy.defect_signature_ok) == (False, True)


def test_audit_reports_probe_errors_as_an_invariant_failure() -> None:
    audit = target.evaluate_jupyterlab_lazarus_audit(
        _audit(probe_errors=["TimeoutError: Edge never settled"])
    )
    assert audit.complete is True
    assert audit.invariants_ok is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"task_id": "jupyterlab-19227"},
        {"variant": "candidate"},
        {"browser_channel": "chromium"},
        {"browser_version": ""},
        {"registered_replay": []},
        {"frames": {"after": "after.png"}},
        {"probe_errors": "none"},
        {"observed": {}},
        {"expected_goal": {"same": 1}, "expected_defect": {"same": 1}},
    ],
)
def test_audit_fails_closed_without_evidence(overrides: dict[str, Any]) -> None:
    audit = target.evaluate_jupyterlab_lazarus_audit(_audit(**overrides))
    assert audit.complete is False
    assert audit.goal_checks_ok is None
    assert audit.failures


def test_checks_resolve_fields_and_reject_unknown_types() -> None:
    audit = _audit()
    ok, failures = target.evaluate_jupyterlab_lazarus_checks(
        [
            {"type": target.DOM_AUDIT_CHECK, "field": "after_item_names", "expected": ["a", "b"]},
            {"type": target.PROBE_ERROR_CHECK},
        ],
        audit,
    )
    assert (ok, failures) == (True, ())

    mismatch, mismatch_failures = target.evaluate_jupyterlab_lazarus_checks(
        [{"type": target.DOM_AUDIT_CHECK, "field": "after_item_names", "expected": ["b", "a"]}],
        audit,
    )
    assert mismatch is False
    assert "after_item_names" in mismatch_failures[0]

    unknown, unknown_failures = target.evaluate_jupyterlab_lazarus_checks(
        [{"type": "jupyterlab_lazarus_trust_me"}], audit
    )
    assert unknown is False
    assert "unsupported type" in unknown_failures[0]

    absent, absent_failures = target.evaluate_jupyterlab_lazarus_checks(
        [{"type": target.DOM_AUDIT_CHECK, "field": "missing", "expected": 1}], audit
    )
    assert absent is False
    assert "absent" in absent_failures[0]

    empty, empty_failures = target.evaluate_jupyterlab_lazarus_checks([], audit)
    assert empty is False
    assert empty_failures


def test_probe_reports_both_versions_from_the_registered_replay(tmp_path: Path) -> None:
    binding = _binding()
    bindings = {binding.task_id: binding}
    fixed = target.run_jupyterlab_lazarus_probe(
        tmp_path, task_id=binding.task_id, variant="fixed", bindings=bindings
    )
    assert fixed["observed"] == {"after_item_names": ["a", "b"]}
    assert fixed["probe_errors"] == []
    assert target.evaluate_jupyterlab_lazarus_audit(fixed).goal_checks_ok is True

    buggy = target.run_jupyterlab_lazarus_probe(
        tmp_path, task_id=binding.task_id, variant="buggy", bindings=bindings
    )
    assert target.evaluate_jupyterlab_lazarus_audit(buggy).defect_signature_ok is True


def test_probe_records_a_failed_replay_instead_of_raising(tmp_path: Path) -> None:
    def broken(root: Path, variant: str) -> Mapping[str, Any]:
        raise RuntimeError("Rspack build did not complete")

    binding = _binding(observation=broken)
    audit = target.run_jupyterlab_lazarus_probe(
        tmp_path,
        task_id=binding.task_id,
        variant="fixed",
        bindings={binding.task_id: binding},
    )
    assert audit["probe_errors"] == ["RuntimeError: Rspack build did not complete"]
    verdict = target.evaluate_jupyterlab_lazarus_audit(audit)
    assert verdict.complete is False


def test_probe_rejects_unknown_variants_and_tasks(tmp_path: Path) -> None:
    binding = _binding()
    with pytest.raises(target.JupyterLabLazarusProbeError):
        target.run_jupyterlab_lazarus_probe(
            tmp_path,
            task_id=binding.task_id,
            variant="candidate",
            bindings={binding.task_id: binding},
        )
    with pytest.raises(target.JupyterLabLazarusProbeError):
        target.run_jupyterlab_lazarus_probe(
            tmp_path, task_id="lz-jupyterlab-0-absent", variant="fixed", bindings={}
        )


def test_check_projection_round_trips_through_the_evaluator(tmp_path: Path) -> None:
    binding = _binding()
    audit = target.run_jupyterlab_lazarus_probe(
        tmp_path,
        task_id=binding.task_id,
        variant="fixed",
        bindings={binding.task_id: binding},
    )
    goal_ok, _ = target.evaluate_jupyterlab_lazarus_checks(
        target.jupyterlab_lazarus_goal_checks(binding), audit
    )
    defect_ok, _ = target.evaluate_jupyterlab_lazarus_checks(
        target.jupyterlab_lazarus_defect_signature(binding), audit
    )
    assert (goal_ok, defect_ok) == (True, False)


def test_registered_bindings_cover_every_replayable_task() -> None:
    bindings = target.probe_bindings()
    assert len(bindings) == 11
    for task_id, binding in bindings.items():
        assert task_id.startswith("lz-jupyterlab-")
        assert binding.replay_steps
        assert binding.expected("fixed") != binding.expected("buggy")
        goal = target.jupyterlab_lazarus_goal_checks(binding)
        assert goal and all(check["type"] == target.DOM_AUDIT_CHECK for check in goal)
