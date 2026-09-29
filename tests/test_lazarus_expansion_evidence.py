"""Contracts the C1-Lazarus expansion rows must satisfy before publication.

Every check here exists because the published artifact could otherwise assert
something no run established: a record bound to a patch the row does not
publish, a control the graded run never executes, a start state nobody observed,
or a paid run authorized for a smaller matrix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from warptest.lazarus_catalog import validate_lazarus_catalog
from warptest.lazarus_expansion_gate import (
    ExpansionGateError,
    _capture_invariants,
    _capture_start_ok,
    _semantic_start_ok,
    derive_formal_upstream_record,
)
from warptest.lazarus_expansion_specs import EXPANSION_ROWS_BY_ID
from warptest.lazarus_jupyterlab_app import JUPYTERLAB_APP_PROFILES
from warptest.lazarus_jupyterlab_app_probe import (
    PROBE_ADAPTER as APP_PROBE_ADAPTER,
    app_probe_bindings,
    evaluate_jupyterlab_app_lazarus_audit,
    evaluate_jupyterlab_app_lazarus_checks,
    jupyterlab_app_defect_signature,
    jupyterlab_app_goal_checks,
)
from warptest.lazarus_luanti_expansion import (
    LUANTI_EXPANSION_CHANGES,
    build_luanti_expansion_binding,
    expansion_luanti_states,
)
from warptest.lazarus_luanti_probe import (
    LIVE_ADAPTER as LUANTI_LIVE_ADAPTER,
    luanti_lazarus_defect_signature,
    luanti_lazarus_goal_checks,
)
from warptest.lazarus_roster import (
    FORMAL_PAID_RUN_KEY,
    superseded_paid_run_keys,
    unbound_formal_task_ids,
)


OPENRA_ADAPTER = "openra_live_gameplay_probe_v1"
ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml"


@pytest.fixture(scope="module")
def rows() -> list[dict[str, object]]:
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    _, tasks = validate_lazarus_catalog(payload, source_path=CATALOG_PATH)
    return tasks


def _spec(row: dict[str, object]) -> dict[str, object]:
    return yaml.safe_load((ROOT / str(row["oracle_spec"])).read_text(encoding="utf-8"))


def test_every_formal_record_is_bound_to_the_patch_its_row_publishes(
    rows: list[dict[str, object]],
) -> None:
    """A record that names some other patch cannot vouch for this row."""

    for row in rows:
        defect = row["defect"]
        patch = ROOT / str(defect["revert_patch"])
        record = json.loads(
            (ROOT / str(defect["upstream_record"])).read_text(encoding="utf-8")
        )
        published = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert record["patch_sha256"] == published, row["id"]
        assert defect["patch_sha256"] == published, row["id"]


def test_formal_records_only_register_construction_tests(
    rows: list[dict[str, object]],
) -> None:
    """The construction matrix runs these two kinds and nothing else."""

    for row in rows:
        record = json.loads(
            (ROOT / str(row["defect"]["upstream_record"])).read_text(encoding="utf-8")
        )
        kinds = {str(test["kind"]) for test in record["tests"]}
        assert kinds <= {"fail_to_pass", "pass_to_pass"}, (row["id"], sorted(kinds))


def test_every_row_reruns_a_control_the_gate_established(
    rows: list[dict[str, object]],
) -> None:
    """Per-trial ids must be pass-to-pass ids, so each trial re-runs proven evidence."""

    for row in rows:
        upstream_tests = _spec(row)["evaluation"]["lazarus"]["upstream_tests"]
        per_trial = set(upstream_tests["per_trial_p2p"])
        assert per_trial, row["id"]
        assert per_trial <= set(upstream_tests["pass_to_pass"]), row["id"]
        expansion = EXPANSION_ROWS_BY_ID.get(str(row["id"]))
        if expansion is not None:
            # The generator must publish the registered control rather than
            # silently substituting the construction P2P for it.
            assert per_trial == set(expansion.per_trial_p2p), row["id"]


def test_every_executable_spec_names_an_adapter_with_a_binding(
    rows: list[dict[str, object]],
) -> None:
    """A spec may only claim to be executable if something can dispatch it."""

    bound_adapters = {
        APP_PROBE_ADAPTER: set(JUPYTERLAB_APP_PROFILES),
        LUANTI_LIVE_ADAPTER: {
            task_id for task_id in LUANTI_EXPANSION_CHANGES.values()
        }
        | {"lz-luanti-11919-vector-from-string", "lz-luanti-13485-falling-node"},
        OPENRA_ADAPTER: {"lz-openra-21453-hotkey-ready-building"},
    }
    for row in rows:
        expansion = EXPANSION_ROWS_BY_ID.get(str(row["id"]))
        if expansion is None:
            continue
        spec = _spec(row)
        # Every spec names its adapter on the semantic surfaces, which is what a
        # trial dispatches on; the probe block is host specific.
        adapter = spec["evaluation"]["semantic"]["surfaces"]["gui"]["adapter"]
        registered = bound_adapters.get(str(adapter))
        assert registered is not None, (row["id"], adapter)
        assert str(row["id"]) in registered, (row["id"], adapter)
        probes = [
            value
            for key, value in spec["setup"].items()
            if key.endswith("_lazarus_probe")
        ]
        for probe in probes:
            assert probe["probe_executable"] is True, row["id"]
            assert "unbound_reason" not in probe, row["id"]


def test_no_roster_row_is_left_undispatchable(rows: list[dict[str, object]]) -> None:
    """The guard reads the specs, so this fails the moment a binding is dropped."""

    assert unbound_formal_task_ids(rows, repository_root=ROOT) == ()


def test_specs_and_probes_describe_the_same_replay(
    rows: list[dict[str, object]],
) -> None:
    """A published replay must be the one the probe actually performs."""

    for row in rows:
        profile = JUPYTERLAB_APP_PROFILES.get(str(row["id"]))
        if profile is None:
            continue
        setup = _spec(row)["setup"]["jupyterlab_lazarus_probe"]
        assert tuple(setup["registered_replay"]) == profile.registered_replay
        assert setup["adapter"] == APP_PROBE_ADAPTER


def test_luanti_expansion_states_come_from_recorded_observations() -> None:
    """The pair must be read from capture receipts, never from the oracle spec."""

    states = expansion_luanti_states(ROOT, change_number=14396)
    assert states.fixed != states.buggy
    assert states.layout == "expansion_capture_receipts"
    assert all(
        "capture/" in source or "formal-v1/gate.json" in source
        for source in states.source_artifact_ids
    )
    # No admission exists for an expansion row, and the record must say so
    # rather than borrow the freeze sprint's vocabulary.
    assert "no freeze admission" in states.admission_id


def test_app_probe_audit_refuses_an_unverified_start() -> None:
    audit = {
        "schema_version": 1,
        "task_id": "lz-jupyterlab-16343-shift-tab-tooltip",
        "variant": "fixed",
        "runtime": "materialized_dev_mode_bundle",
        "browser_channel": "chromium",
        "registered_replay": ["load_materialized_dev_mode_bundle"],
        "probe_errors": [],
        "start": {"field": "tooltip_start_state", "observed": "unexpected",
                  "expected": "name_typed_no_tooltip"},
        "observed": {"tooltip_state": "tooltip_shown"},
        "expected_goal": {"tooltip_state": "tooltip_shown"},
        "expected_defect": {"tooltip_state": "tooltip_absent"},
    }
    verdict = evaluate_jupyterlab_app_lazarus_audit(audit)
    assert verdict.complete is False
    assert any("registered start" in failure for failure in verdict.failures)


def test_app_probe_projects_the_checks_its_spec_publishes(
    rows: list[dict[str, object]],
) -> None:
    """The dispatcher must resolve the very checks the row publishes.

    A binding that projected a different field or value would score a trial
    against an oracle nobody published, so the projection is compared with the
    spec instead of being trusted.
    """

    bindings = app_probe_bindings(
        prebuilt_root=ROOT / ".warptest-cache/lazarus/prebuilt",
        sandbox_root=ROOT / ".warptest-cache/lazarus/probe-contract",
    )
    for row in rows:
        binding = bindings.get(str(row["id"]))
        if binding is None:
            continue
        lazarus = _spec(row)["evaluation"]["lazarus"]
        assert jupyterlab_app_goal_checks(binding) == lazarus["goal_checks"]
        assert jupyterlab_app_defect_signature(binding) == lazarus["defect_signature"]


def test_luanti_expansion_projects_the_checks_its_spec_publishes() -> None:
    binding = build_luanti_expansion_binding(ROOT, change_number=14396)
    spec_path = ROOT / EXPANSION_ROWS_BY_ID[binding.task_id].spec_path
    lazarus = yaml.safe_load(spec_path.read_text(encoding="utf-8"))["evaluation"][
        "lazarus"
    ]
    assert luanti_lazarus_goal_checks(binding) == lazarus["goal_checks"]
    assert luanti_lazarus_defect_signature(binding) == lazarus["defect_signature"]


def test_app_probe_checks_refuse_an_unknown_check_type() -> None:
    audit = {"observed": {"tooltip_state": "tooltip_shown"}, "probe_errors": []}
    ok, failures = evaluate_jupyterlab_app_lazarus_checks(
        [{"type": "some_other_audit", "field": "tooltip_state", "expected": "x"}],
        audit,
    )
    assert ok is False
    assert any("unsupported type" in failure for failure in failures)


def test_a_narrower_paid_run_grant_does_not_carry_over() -> None:
    assert superseded_paid_run_keys({"formal_288_paid_run": True}) == (
        "formal_288_paid_run",
    )
    assert superseded_paid_run_keys({FORMAL_PAID_RUN_KEY: True}) == ()


def test_derived_record_refuses_a_test_the_matrix_never_ran() -> None:
    mined = {
        "task_id": "lz-demo-1-example",
        "tests": [
            {"id": "demo-f2p", "kind": "fail_to_pass"},
            {"id": "demo-trial", "kind": "per_trial_p2p"},
        ],
        "record_sha256": "0" * 64,
    }
    with pytest.raises(ExpansionGateError, match="does not register as construction"):
        derive_formal_upstream_record(
            mined, patch_sha256="a" * 64, declared_tests=("demo-f2p", "demo-trial")
        )


def test_capture_evidence_fails_closed_when_a_stage_is_missing() -> None:
    only_after = {"fixed": {"after": {"audit": {}, "invariants": {"ok": True}}}}
    with pytest.raises(ExpansionGateError, match="no before stage"):
        _capture_start_ok(only_after, "fixed")
    with pytest.raises(ExpansionGateError, match="no before stage"):
        _capture_invariants(only_after, "fixed")


def test_capture_evidence_fails_closed_without_invariant_records() -> None:
    without_invariants = {
        "fixed": {
            "before": {"audit": {"observed": "a", "expected": "a"}},
            "after": {"audit": {"observed": "b", "expected": "b"}},
        }
    }
    with pytest.raises(ExpansionGateError, match="no invariant evidence"):
        _capture_invariants(without_invariants, "fixed")


def test_live_gate_start_verdict_must_have_been_recorded() -> None:
    with pytest.raises(ExpansionGateError, match="start_ok"):
        _semantic_start_ok({"evidence": {"semantic": {"fixed_start": {}}}}, "fixed_start")
    assert _semantic_start_ok(
        {"evidence": {"semantic": {"fixed_start": {"start_ok": True}}}}, "fixed_start"
    )
