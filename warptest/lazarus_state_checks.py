"""Shared resolution of lazarus check groups against a probe audit.

A lazarus spec states its goal and its defect signature as check lists.  Every
benchmark that observes a semantic state mapping resolves those lists the same
way, so the field lookup, the unknown-type refusal and the invariant check live
here instead of once per benchmark.

Two check shapes are supported:

``<benchmark>_lazarus_state_audit``
    ``field`` names one key of the audit's observed state and ``expected`` is the
    value that key must carry.

``no_<benchmark>_probe_errors``
    The probe reported no error while replaying the registered sequence.

Anything else is refused: a spec cannot smuggle in a check the evaluator does not
understand and have it silently pass.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def evaluate_state_checks(
    checks: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
    *,
    audit_check: str,
    error_check: str,
) -> tuple[bool, tuple[str, ...]]:
    """Resolve one check group, returning the verdict and every failure reason."""

    if not isinstance(checks, Sequence) or isinstance(checks, str) or not checks:
        return False, ("check group is empty",)
    if not isinstance(audit, Mapping):
        return False, ("audit is not a mapping",)
    observed = audit.get("observed")
    failures: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, Mapping):
            failures.append(f"check[{index}] is not a mapping")
            continue
        kind = check.get("type")
        if kind == error_check:
            failures.extend(_probe_errors(index, audit))
            continue
        if kind != audit_check:
            failures.append(f"check[{index}] has an unsupported type: {kind!r}")
            continue
        failures.extend(_field(index, check, observed))
    return not failures, tuple(failures)


def project_state_checks(state: Any, *, audit_check: str) -> list[dict[str, Any]]:
    """Project a registered semantic state into a spec check list."""

    if isinstance(state, Mapping):
        if not state:
            raise ValueError("a registered state cannot be empty")
        return [
            {"type": audit_check, "field": str(key), "expected": value}
            for key, value in sorted(state.items(), key=lambda item: str(item[0]))
        ]
    if state is None or state == "":
        raise ValueError("a registered state cannot be empty")
    return [{"type": audit_check, "field": "observation", "expected": state}]


def _probe_errors(index: int, audit: Mapping[str, Any]) -> list[str]:
    errors = audit.get("probe_errors")
    if not isinstance(errors, Sequence) or isinstance(errors, str):
        return [f"check[{index}] has no probe error list"]
    if list(errors):
        return [f"check[{index}] observed probe errors: {list(errors)}"]
    return []


def _field(index: int, check: Mapping[str, Any], observed: Any) -> list[str]:
    name = check.get("field")
    if not isinstance(name, str) or not name:
        return [f"check[{index}] has no audit field"]
    if isinstance(observed, Mapping):
        if name not in observed:
            return [f"check[{index}] field is absent: {name}"]
        value = observed[name]
    elif name == "observation":
        value = observed
    else:
        return [f"check[{index}] field is absent: {name}"]
    if value != check.get("expected"):
        return [
            f"check[{index}] {name} is {value!r}, expected {check.get('expected')!r}"
        ]
    return []


__all__ = ["evaluate_state_checks", "project_state_checks"]
