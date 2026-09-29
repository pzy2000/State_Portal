from __future__ import annotations

from typing import Any


def auxiliary_positioning(component: str) -> dict[str, Any]:
    return {
        "component": component,
        "paper_role": "auxiliary_appendix",
        "primary_claim": False,
        "note": (
            "Auxiliary evaluation for spec authoring or visual plausibility. "
            "The core WarpTest claim is semantic checkpoint synthesis plus hybrid jump-and-validate "
            "using source/runtime state signals."
        ),
    }
