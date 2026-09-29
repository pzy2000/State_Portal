"""Frozen, pre-registered configuration for metric-grounded task selection.

Reviewer-facing rationale: selection must be a *fixed* function decided before
looking at any result, otherwise the weights themselves become a cherry-picking
knob. We therefore freeze the composite weights and normalisation method here,
version them, and expose a content hash so a paper/report can cite the exact
configuration used to produce a candidate ranking.

If a weight ever changes, bump CONFIG_VERSION; the hash changes with it and any
downstream candidate file records which config produced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

CONFIG_VERSION = "v1.0-preregistered-2026-07"

# Per-signal weights for the composite selection score. Each signal is first
# converted to a within-corpus percentile rank in [0, 1], then combined by these
# weights. Weights sum to 1.0. Chosen before inspecting any ranking:
#   - cyclomatic complexity dominates: it is the classic defect-proneness proxy.
#   - nesting depth and coupling capture structural difficulty complexity misses.
#   - churn captures "high-value regression point" (complex AND frequently edited).
COMPOSITE_WEIGHTS: dict[str, float] = {
    "cyclomatic": 0.35,
    "nesting": 0.20,
    "fan_in": 0.10,
    "fan_out": 0.10,
    "churn": 0.25,
}

# Reachability is reported and used at the *candidate target* granularity (its
# natural level): it is the from-start unreachability that justifies checkpoint
# necessity. It multiplies the static composite into the final task value rather
# than being averaged into the per-function selection score.
REACHABILITY_BLEND = 0.5  # final = composite * (REACHABILITY_BLEND + (1-blend)*unreach)

# Minimum cyclomatic complexity for a function to be a selection candidate at
# all: trivial getters/setters are not interesting test targets.
MIN_CYCLOMATIC = 3


@dataclass(frozen=True)
class MetricConfig:
    version: str = CONFIG_VERSION
    weights: dict[str, float] = field(default_factory=lambda: dict(COMPOSITE_WEIGHTS))
    reachability_blend: float = REACHABILITY_BLEND
    min_cyclomatic: int = MIN_CYCLOMATIC
    normalization: str = "within-corpus percentile rank"

    def config_hash(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "weights": self.weights,
                "reachability_blend": self.reachability_blend,
                "min_cyclomatic": self.min_cyclomatic,
                "normalization": self.normalization,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "weights": self.weights,
            "reachability_blend": self.reachability_blend,
            "min_cyclomatic": self.min_cyclomatic,
            "normalization": self.normalization,
            "config_hash": self.config_hash(),
        }


def default_config() -> MetricConfig:
    return MetricConfig()
