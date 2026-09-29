"""Declarative C1 evaluation contracts.

Formal C1 oracle YAML stores task evaluation policy below ``evaluation``.  The
helpers in this package keep that hidden contract separate from public task
inputs while offering a read-only legacy projection to existing runtimes.
"""

from .catalog import formal_evaluation_contract, formal_spec, formal_surface_contract
from .contract import (
    EVALUATION_SCHEMA_VERSION,
    evaluation_contract,
    materialize_legacy_spec,
    semantic_assertions,
    surface_contract,
    validate_evaluation_contract,
    validate_lazarus_evaluation_contract,
    visual_oracle,
)
from .engine import EvaluationContext, evaluate_checks

__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "EvaluationContext",
    "evaluate_checks",
    "evaluation_contract",
    "formal_evaluation_contract",
    "formal_spec",
    "formal_surface_contract",
    "materialize_legacy_spec",
    "semantic_assertions",
    "surface_contract",
    "validate_evaluation_contract",
    "validate_lazarus_evaluation_contract",
    "visual_oracle",
]
