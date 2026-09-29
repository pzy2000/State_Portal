from __future__ import annotations

from dataclasses import replace

import pytest

from warptest.lazarus_grading import (
    LazarusTrialEvidence,
    VisualJudgement,
    grade_lazarus_p2p_only_trial,
    grade_lazarus_trial,
)


def _visual(ok: bool | None, oracle: str) -> VisualJudgement:
    return VisualJudgement(
        ok=ok,
        screenshot_sha256="a" * 64,
        oracle_sha256=oracle,
    )


def _evidence(**overrides) -> LazarusTrialEvidence:
    value = LazarusTrialEvidence(
        version="fixed",
        before_semantic_ok=True,
        before_visual_ok=True,
        goal_checks_ok=True,
        defect_signature_ok=False,
        invariants_ok=True,
        fixed_after_visual=_visual(True, "f" * 64),
        buggy_after_visual=_visual(False, "b" * 64),
        per_trial_p2p_ok=True,
    )
    return replace(value, **overrides)


def test_fixed_trial_uses_goal_semantics_and_both_visual_oracles() -> None:
    result = grade_lazarus_trial(_evidence())

    assert result.start_success is True
    assert result.goal_success is True
    assert result.defect_exposed is False
    assert result.false_alarm is False
    assert result.complete is True
    assert result.after_statuses == {"goal": "evaluated", "defect": "evaluated"}


def test_buggy_trial_requires_specific_signature_invariants_and_buggy_visual() -> None:
    result = grade_lazarus_trial(
        _evidence(
            version="buggy",
            goal_checks_ok=False,
            defect_signature_ok=True,
            fixed_after_visual=_visual(False, "f" * 64),
            buggy_after_visual=_visual(True, "b" * 64),
        )
    )

    assert result.start_success is True
    assert result.goal_success is False
    assert result.defect_exposed is True
    assert result.false_alarm is False
    assert result.complete is True


def test_fixed_defect_match_is_recorded_as_false_alarm() -> None:
    result = grade_lazarus_trial(
        _evidence(
            defect_signature_ok=True,
            buggy_after_visual=_visual(True, "b" * 64),
        )
    )

    assert result.goal_success is True
    assert result.defect_exposed is True
    assert result.false_alarm is True


def test_start_failure_short_circuits_both_after_verdicts() -> None:
    result = grade_lazarus_trial(_evidence(before_semantic_ok=False))

    assert result.start_success is False
    assert result.goal_success is False
    assert result.defect_exposed is False
    assert result.after_statuses == {
        "goal": "not_executed_start_failed",
        "defect": "not_executed_start_failed",
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"before_visual_ok": None}, "before evidence is missing"),
        ({"goal_checks_ok": None}, "semantic evidence is missing"),
        ({"defect_signature_ok": None}, "semantic evidence is missing"),
        ({"invariants_ok": None}, "semantic evidence is missing"),
        ({"fixed_after_visual": _visual(None, "f" * 64)}, "visual evidence is missing"),
        ({"buggy_after_visual": _visual(None, "b" * 64)}, "visual evidence is missing"),
        ({"per_trial_p2p_ok": None}, "P2P evidence is missing"),
        ({"per_trial_p2p_ok": False}, "P2P subset failed"),
    ],
)
def test_trial_grading_fails_closed_on_missing_or_failed_evidence(
    overrides: dict, reason: str
) -> None:
    result = grade_lazarus_trial(_evidence(**overrides))

    assert result.complete is False
    assert result.goal_success is False
    assert result.defect_exposed is False
    assert reason in result.failures


def test_two_visual_judgements_must_use_the_exact_same_screenshot() -> None:
    mismatched = VisualJudgement(
        ok=True,
        screenshot_sha256="c" * 64,
        oracle_sha256="b" * 64,
    )
    result = grade_lazarus_trial(_evidence(buggy_after_visual=mismatched))

    assert result.complete is False
    assert result.goal_success is False
    assert result.defect_exposed is False
    assert "visual judgements did not use the same screenshot" in result.failures


def test_p2p_only_grading_uses_visuals_without_semantic_assertions() -> None:
    result = grade_lazarus_p2p_only_trial(
        _evidence(
            version="buggy",
            goal_checks_ok=None,
            defect_signature_ok=None,
            fixed_after_visual=_visual(False, "f" * 64),
            buggy_after_visual=_visual(True, "b" * 64),
        )
    )
    assert result.complete is True
    assert result.goal_success is False
    assert result.defect_exposed is True
    assert result.failures == ()


def test_p2p_only_grading_fails_closed_on_runtime_or_p2p_gap() -> None:
    runtime_missing = grade_lazarus_p2p_only_trial(
        _evidence(goal_checks_ok=None, defect_signature_ok=None, invariants_ok=None)
    )
    assert runtime_missing.complete is False
    assert "runtime transition evidence is missing" in runtime_missing.failures

    p2p_failed = grade_lazarus_p2p_only_trial(
        _evidence(
            goal_checks_ok=None,
            defect_signature_ok=None,
            per_trial_p2p_ok=False,
        )
    )
    assert p2p_failed.complete is False
    assert "P2P subset failed" in p2p_failed.failures
