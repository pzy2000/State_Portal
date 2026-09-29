"""Shared C1 Start-checkpoint semantic grading.

Every C1 arm has to answer the same question at the Start checkpoint: does the
live runtime satisfy the registered oracle target?  The GUI arms answered it
with :func:`semantic_start_grade` while the state-managed arm only asked "did my
own restore report success", which made the two Start numbers incomparable.
This module holds the one predicate both arms now call.

The module deliberately carries no runtime imports beyond the profile registry
so producers can import it without pulling in a browser or desktop stack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .c1_public_semantics import grade_public_start


# A backend that cannot expose oracle-side evidence must be visible in the
# report rather than silently counted as a passed Start.
UNSUPPORTED_STATUS = "oracle_target_unsupported"

SemanticGrade = tuple[bool, str, dict[str, Any]]


def oracle_target(oracle_spec: Any) -> dict[str, Any]:
    """Return the registered Start target mapping of an oracle spec."""

    if not isinstance(oracle_spec, dict):
        return {}
    target = oracle_spec.get("target")
    return target if isinstance(target, dict) else {}


def semantic_start_grade(
    profile: Any,
    runtime: Any,
    oracle_spec: dict[str, Any],
    *,
    prefer_public_surface: bool = True,
) -> SemanticGrade:
    """Validate the live runtime against the oracle Start target.

    ``prefer_public_surface`` selects the production-surface grader for the
    benchmarks that have one.  The GUI arms need it because they run without
    state hooks; the state-managed arm runs with hooks and uses the profile
    validator directly, so it passes ``False``.  Both paths grade the same
    ``oracle_spec["target"]``.
    """

    if profile is None:
        return False, "no_profile", {}
    if prefer_public_surface:
        public = grade_public_start(profile, runtime, oracle_spec)
        if public is not None:
            return public
    validate = getattr(profile, "_validate", None)
    if validate is None:
        return False, UNSUPPORTED_STATUS, {}
    try:
        verdict = validate(runtime, oracle_target(oracle_spec))
    except Exception as error:  # noqa: BLE001 - graded as a fair failure
        return False, "validate_error", {"error": str(error)}
    ok = getattr(verdict, "status", None) == "success"
    return bool(ok), ("success" if ok else "validate_failed"), {
        "status": getattr(verdict, "status", None),
        "detail": getattr(verdict, "detail", None),
    }


def merge_start_semantic(
    *,
    restore_ok: bool,
    restore_status: str | None,
    restore_detail: Any = None,
    oracle_grade: SemanticGrade | None,
) -> SemanticGrade:
    """Conjoin an arm's own start construction with the oracle re-validation.

    ``oracle_grade`` of ``None`` means the backend produced no oracle-side
    evidence; that fails closed under :data:`UNSUPPORTED_STATUS` so the report
    can separate "wrong start" from "ungradable start".
    """

    detail: dict[str, Any] = {"restore_status": restore_status}
    if restore_detail is not None:
        detail["restore_detail"] = restore_detail
    if not restore_ok:
        return False, str(restore_status or "failure"), detail

    if oracle_grade is None:
        detail["oracle_target"] = {"status": UNSUPPORTED_STATUS}
        return False, UNSUPPORTED_STATUS, detail

    ok, status, oracle_detail = oracle_grade
    entry: dict[str, Any] = {"status": status}
    if oracle_detail:
        entry["detail"] = oracle_detail
    detail["oracle_target"] = entry
    if ok:
        return True, "success", detail
    return False, str(status or "validate_failed"), detail


def profile_for_repo(repo: str | Path) -> Any | None:
    """Build the benchmark profile for a checkout, or ``None`` if unavailable."""

    # Imported lazily: the profile registry pulls in the discovery stack, which
    # several lightweight report-side consumers of this module do not need.
    from .discovery import RepositoryInspector
    from .profiles import profile_for

    try:
        return profile_for(RepositoryInspector().inspect(Path(repo)))
    except Exception:  # noqa: BLE001 - absence is graded, not raised
        return None
