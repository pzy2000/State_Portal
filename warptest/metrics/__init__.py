"""Metric-grounded task-provenance package.

This package answers the "where does a benchmark task come from?" question with a
reproducible pipeline instead of hand-picked authoring:

  static_metrics -> per-function McCabe cyclomatic complexity, nesting depth,
                    NLOC, token count (via lizard, one tool across JS/TS/C#/C++/
                    Python/Lua).
  coupling       -> transparent text-level fan-in / fan-out over the analysed
                    file set (documented approximation, no call-graph tooling).
  churn          -> git-history change frequency per file and per line range.
  reachability   -> bounded from-start reach rate for a candidate deep state,
                    the signal that justifies why a checkpoint is needed.
  score          -> percentile-normalised composite with PRE-REGISTERED weights,
                    intersected with the discovery semantic surfaces so the
                    selected region is actually observable/controllable.
  select         -> orchestration entry used by the CLI; emits a ranked,
                    re-derivable candidate list to reports/metrics/.

The composite selection score is a deterministic function of
(source@commit, git history, frozen weights); anyone can re-run it and obtain
the same ranking, which is the property that defends the benchmark against
cherry-picking claims.
"""

from .config import COMPOSITE_WEIGHTS, MetricConfig, default_config
from .static_metrics import FunctionMetric, analyze_paths
from .score import Candidate, rank_candidates

__all__ = [
    "COMPOSITE_WEIGHTS",
    "MetricConfig",
    "default_config",
    "FunctionMetric",
    "analyze_paths",
    "Candidate",
    "rank_candidates",
]
