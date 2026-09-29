from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .probes import resolve_probe


@dataclass(frozen=True)
class EvaluationContext:
    spec: Mapping[str, Any]
    current: Mapping[str, Any]
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    trace: Mapping[str, Any] | None = None
    runtime: Any = None
    extras: Mapping[str, Any] = field(default_factory=dict)


def evaluate_checks(
    declarations: list[dict[str, Any]], context: EvaluationContext
) -> list[dict[str, Any]]:
    """Evaluate an already schema-validated list in declared order."""

    rows: list[dict[str, Any]] = []
    for declaration in declarations:
        name = str(declaration["name"])
        metric = str(declaration["metric"])
        actual = _resolve_operand(declaration["actual"], context)
        expected = _resolve_operand(declaration["expected"], context)
        if metric == "equals":
            rows.append(
                {
                    "name": name,
                    "ok": actual == expected,
                    "actual": actual,
                    "expected": expected,
                }
            )
        elif metric == "greater_or_equal":
            ok = (
                isinstance(actual, (int, float))
                and isinstance(expected, (int, float))
                and actual >= expected
            )
            rows.append(
                {
                    "name": name,
                    "ok": ok,
                    "actual": actual,
                    "expected_min": expected,
                }
            )
        elif metric == "greater_than":
            ok = (
                isinstance(actual, (int, float))
                and isinstance(expected, (int, float))
                and actual > expected
            )
            rows.append(
                {
                    "name": name,
                    "ok": ok,
                    "actual": actual,
                    "baseline": expected,
                }
            )
        elif metric == "contains":
            ok = isinstance(actual, str) and isinstance(expected, str) and expected in actual
            rows.append(
                {
                    "name": name,
                    "ok": ok,
                    "actual": actual,
                    "expected": expected,
                }
            )
        else:  # validation should make this unreachable; stay fail closed.
            raise ValueError(f"unsupported C1 evaluation metric: {metric}")
    return rows


def _resolve_operand(value: Mapping[str, Any], context: EvaluationContext) -> Any:
    if "value" in value:
        return value.get("value")
    return resolve_probe(str(value.get("probe") or ""), value, context)
