"""Fail-closed dual-oracle grading for one C1-Lazarus trial."""

from __future__ import annotations

import re
from dataclasses import dataclass


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VisualJudgement:
    ok: bool | None
    screenshot_sha256: str | None
    oracle_sha256: str | None


@dataclass(frozen=True)
class LazarusTrialEvidence:
    version: str
    before_semantic_ok: bool | None
    before_visual_ok: bool | None
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    fixed_after_visual: VisualJudgement
    buggy_after_visual: VisualJudgement
    per_trial_p2p_ok: bool | None


@dataclass(frozen=True)
class LazarusTrialGrade:
    version: str
    start_success: bool
    goal_success: bool
    defect_exposed: bool
    invariants_ok: bool
    per_trial_p2p_ok: bool
    false_alarm: bool
    complete: bool
    after_statuses: dict[str, str]
    failures: tuple[str, ...]


def grade_lazarus_trial(evidence: LazarusTrialEvidence) -> LazarusTrialGrade:
    """Grade one screenshot against both registered after-state oracles."""

    if evidence.version not in {"buggy", "fixed"}:
        raise ValueError("Lazarus trial version must be 'buggy' or 'fixed'")
    failures: list[str] = []
    before_missing = any(
        type(value) is not bool
        for value in (evidence.before_semantic_ok, evidence.before_visual_ok)
    )
    if before_missing:
        failures.append("before evidence is missing")
    start_success = (
        evidence.before_semantic_ok is True and evidence.before_visual_ok is True
    )
    if not start_success:
        return LazarusTrialGrade(
            version=evidence.version,
            start_success=False,
            goal_success=False,
            defect_exposed=False,
            invariants_ok=evidence.invariants_ok is True,
            per_trial_p2p_ok=evidence.per_trial_p2p_ok is True,
            false_alarm=False,
            complete=not before_missing,
            after_statuses={
                "goal": "not_executed_start_failed",
                "defect": "not_executed_start_failed",
            },
            failures=tuple(failures),
        )

    if any(
        type(value) is not bool
        for value in (
            evidence.goal_checks_ok,
            evidence.defect_signature_ok,
            evidence.invariants_ok,
        )
    ):
        failures.append("semantic evidence is missing")
    if not _visual_complete(evidence.fixed_after_visual) or not _visual_complete(
        evidence.buggy_after_visual
    ):
        failures.append("visual evidence is missing")
    elif (
        evidence.fixed_after_visual.screenshot_sha256
        != evidence.buggy_after_visual.screenshot_sha256
    ):
        failures.append("visual judgements did not use the same screenshot")
    if evidence.per_trial_p2p_ok is None:
        failures.append("P2P evidence is missing")
    elif evidence.per_trial_p2p_ok is not True:
        failures.append("P2P subset failed")

    complete = not failures
    if not complete:
        return LazarusTrialGrade(
            version=evidence.version,
            start_success=True,
            goal_success=False,
            defect_exposed=False,
            invariants_ok=evidence.invariants_ok is True,
            per_trial_p2p_ok=evidence.per_trial_p2p_ok is True,
            false_alarm=False,
            complete=False,
            after_statuses={
                "goal": "not_evaluated_incomplete_evidence",
                "defect": "not_evaluated_incomplete_evidence",
            },
            failures=tuple(dict.fromkeys(failures)),
        )

    goal_success = bool(
        evidence.goal_checks_ok
        and evidence.invariants_ok
        and evidence.fixed_after_visual.ok
    )
    defect_exposed = bool(
        evidence.defect_signature_ok
        and evidence.invariants_ok
        and evidence.buggy_after_visual.ok
    )
    return LazarusTrialGrade(
        version=evidence.version,
        start_success=True,
        goal_success=goal_success,
        defect_exposed=defect_exposed,
        invariants_ok=evidence.invariants_ok is True,
        per_trial_p2p_ok=True,
        false_alarm=evidence.version == "fixed" and defect_exposed,
        complete=True,
        after_statuses={"goal": "evaluated", "defect": "evaluated"},
        failures=(),
    )


def grade_lazarus_p2p_only_trial(
    evidence: LazarusTrialEvidence,
) -> LazarusTrialGrade:
    """Grade an ``lzp-*`` cell without inventing semantic assertions.

    P2P-only rows deliberately have no fail-to-pass or hidden semantic
    differential.  ``before_semantic_ok`` therefore means only that the
    registered live Start was reached, while ``invariants_ok`` means that the
    live transition completed and the runtime remained healthy.  The fixed vs
    buggy outcome is established exclusively by the two after-image oracles.
    """

    if evidence.version not in {"buggy", "fixed"}:
        raise ValueError("Lazarus trial version must be 'buggy' or 'fixed'")
    failures: list[str] = []
    before_missing = any(
        type(value) is not bool
        for value in (evidence.before_semantic_ok, evidence.before_visual_ok)
    )
    if before_missing:
        failures.append("before evidence is missing")
    start_success = (
        evidence.before_semantic_ok is True and evidence.before_visual_ok is True
    )
    if not start_success:
        return LazarusTrialGrade(
            version=evidence.version,
            start_success=False,
            goal_success=False,
            defect_exposed=False,
            invariants_ok=evidence.invariants_ok is True,
            per_trial_p2p_ok=evidence.per_trial_p2p_ok is True,
            false_alarm=False,
            complete=not before_missing,
            after_statuses={
                "goal": "not_executed_start_failed",
                "defect": "not_executed_start_failed",
            },
            failures=tuple(failures),
        )

    if type(evidence.invariants_ok) is not bool:
        failures.append("runtime transition evidence is missing")
    if not _visual_complete(evidence.fixed_after_visual) or not _visual_complete(
        evidence.buggy_after_visual
    ):
        failures.append("visual evidence is missing")
    elif (
        evidence.fixed_after_visual.screenshot_sha256
        != evidence.buggy_after_visual.screenshot_sha256
    ):
        failures.append("visual judgements did not use the same screenshot")
    if evidence.per_trial_p2p_ok is None:
        failures.append("P2P evidence is missing")
    elif evidence.per_trial_p2p_ok is not True:
        failures.append("P2P subset failed")
    if failures:
        return LazarusTrialGrade(
            version=evidence.version,
            start_success=True,
            goal_success=False,
            defect_exposed=False,
            invariants_ok=evidence.invariants_ok is True,
            per_trial_p2p_ok=evidence.per_trial_p2p_ok is True,
            false_alarm=False,
            complete=False,
            after_statuses={
                "goal": "not_evaluated_incomplete_evidence",
                "defect": "not_evaluated_incomplete_evidence",
            },
            failures=tuple(dict.fromkeys(failures)),
        )

    goal_success = bool(
        evidence.invariants_ok and evidence.fixed_after_visual.ok
    )
    defect_exposed = bool(
        evidence.invariants_ok and evidence.buggy_after_visual.ok
    )
    return LazarusTrialGrade(
        version=evidence.version,
        start_success=True,
        goal_success=goal_success,
        defect_exposed=defect_exposed,
        invariants_ok=evidence.invariants_ok,
        per_trial_p2p_ok=True,
        false_alarm=evidence.version == "fixed" and defect_exposed,
        complete=True,
        after_statuses={"goal": "evaluated", "defect": "evaluated"},
        failures=(),
    )


def _visual_complete(value: VisualJudgement) -> bool:
    return (
        type(value.ok) is bool
        and isinstance(value.screenshot_sha256, str)
        and _SHA256.fullmatch(value.screenshot_sha256) is not None
        and isinstance(value.oracle_sha256, str)
        and _SHA256.fullmatch(value.oracle_sha256) is not None
    )
