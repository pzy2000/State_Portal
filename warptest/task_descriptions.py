"""Canonical public task-description helpers."""

from __future__ import annotations


def canonical_state_description(
    preconditions: str,
    reference_steps: str,
) -> str:
    """Return the canonical Preconditions/Reference steps description."""

    normalized_preconditions = str(preconditions).strip()
    normalized_reference_steps = str(reference_steps).strip()
    if not normalized_preconditions:
        raise ValueError("preconditions must be non-empty")
    if not normalized_reference_steps:
        raise ValueError("reference_steps must be non-empty")
    return (
        f"Preconditions: {normalized_preconditions}\n"
        f"Reference steps: {normalized_reference_steps}"
    )
