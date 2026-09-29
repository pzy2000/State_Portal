from __future__ import annotations

from typing import Any

import pytest

from warptest.lazarus_state_checks import evaluate_state_checks, project_state_checks


AUDIT_CHECK = "example_lazarus_state_audit"
ERROR_CHECK = "no_example_probe_errors"


def _audit(observed: Any, errors: list[str] | None = None) -> dict[str, Any]:
    return {"observed": observed, "probe_errors": list(errors or [])}


def _evaluate(checks: Any, audit: Any) -> tuple[bool, tuple[str, ...]]:
    return evaluate_state_checks(
        checks, audit, audit_check=AUDIT_CHECK, error_check=ERROR_CHECK
    )


def test_mapping_state_resolves_field_by_field() -> None:
    audit = _audit({"result": "0,-0", "ok": True})
    assert _evaluate(
        [
            {"type": AUDIT_CHECK, "field": "result", "expected": "0,-0"},
            {"type": AUDIT_CHECK, "field": "ok", "expected": True},
        ],
        audit,
    ) == (True, ())
    ok, failures = _evaluate(
        [{"type": AUDIT_CHECK, "field": "ok", "expected": False}], audit
    )
    assert ok is False
    assert "ok is True, expected False" in failures[0]


def test_label_state_resolves_through_the_observation_field() -> None:
    audit = _audit("goal_iconv_state_reset")
    assert _evaluate(
        [{"type": AUDIT_CHECK, "field": "observation", "expected": "goal_iconv_state_reset"}],
        audit,
    ) == (True, ())
    ok, failures = _evaluate(
        [{"type": AUDIT_CHECK, "field": "observation", "expected": "defect_iconv_state_regression"}],
        audit,
    )
    assert ok is False
    assert "observation is" in failures[0]
    absent, absent_failures = _evaluate(
        [{"type": AUDIT_CHECK, "field": "result", "expected": 1}], audit
    )
    assert absent is False
    assert "absent" in absent_failures[0]


def test_invariant_check_reads_the_probe_error_list() -> None:
    assert _evaluate([{"type": ERROR_CHECK}], _audit({"a": 1})) == (True, ())
    ok, failures = _evaluate([{"type": ERROR_CHECK}], _audit({"a": 1}, ["boom"]))
    assert ok is False
    assert "boom" in failures[0]
    missing, missing_failures = _evaluate([{"type": ERROR_CHECK}], {"observed": {}})
    assert missing is False
    assert "no probe error list" in missing_failures[0]


@pytest.mark.parametrize(
    "checks",
    [
        [],
        "not-a-list",
        [{"type": "some_unregistered_check"}],
        [{"type": AUDIT_CHECK}],
        [{"type": AUDIT_CHECK, "field": ""}],
        ["not-a-mapping"],
    ],
)
def test_unusable_check_groups_fail_closed(checks: Any) -> None:
    ok, failures = _evaluate(checks, _audit({"a": 1}))
    assert ok is False
    assert failures


def test_non_mapping_audit_fails_closed() -> None:
    ok, failures = _evaluate([{"type": ERROR_CHECK}], "audit")
    assert (ok, failures) == (False, ("audit is not a mapping",))


def test_projection_is_sorted_and_round_trips() -> None:
    checks = project_state_checks({"b": 2, "a": 1}, audit_check=AUDIT_CHECK)
    assert [check["field"] for check in checks] == ["a", "b"]
    assert _evaluate(checks, _audit({"a": 1, "b": 2})) == (True, ())

    label = project_state_checks("goal_label", audit_check=AUDIT_CHECK)
    assert label == [{"type": AUDIT_CHECK, "field": "observation", "expected": "goal_label"}]
    assert _evaluate(label, _audit("goal_label")) == (True, ())


@pytest.mark.parametrize("state", [{}, None, ""])
def test_projection_refuses_an_empty_state(state: Any) -> None:
    with pytest.raises(ValueError):
        project_state_checks(state, audit_check=AUDIT_CHECK)
