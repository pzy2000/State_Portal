"""Composite scoring + intersection with the discovery semantic surfaces.

A high-complexity function is only a usable benchmark target if the state it
touches is actually *observable and controllable* by the harness. We therefore
intersect the metric ranking with the semantic fields/surfaces that
``warptest.discovery.RepositoryInspector`` already extracts from source, and
keep only candidates that sit on a known semantic surface. This is what turns
"complex code" into "a testable checkpoint target".
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import MetricConfig, default_config
from .static_metrics import FunctionMetric
from .reachability import unreachability, ReachResult


def percentile_ranks(values: list[float]) -> list[float]:
    """Return within-corpus percentile rank in [0,1] for each value.

    Ties share the average rank. Robust and scale-free, so no single raw metric
    with a huge range dominates the composite.
    """
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # average 1-based rank for the tie block, normalised to [0,1]
        avg = (i + j) / 2.0
        pr = avg / (n - 1) if n > 1 else 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = pr
        i = j + 1
    return ranks


@dataclass
class SemanticMatch:
    on_surface: bool
    matched_fields: list[str] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    writable_via: list[str] = field(default_factory=list)
    readable_via: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    metric: FunctionMetric
    signals: dict[str, float]  # percentile-ranked signals used in composite
    composite_static: float
    reachability: dict
    final_score: float
    semantic: SemanticMatch

    def to_dict(self) -> dict:
        return {
            "key": self.metric.key,
            "file": self.metric.file,
            "name": self.metric.name,
            "long_name": self.metric.long_name,
            "language": self.metric.language,
            "lines": [self.metric.start_line, self.metric.end_line],
            "raw": {
                "cyclomatic": self.metric.cyclomatic,
                "nesting": self.metric.nesting,
                "fan_in": self.metric.fan_in,
                "fan_out": self.metric.fan_out,
                "churn_commits": self.metric.churn_commits,
                "churn_lines": self.metric.churn_lines,
                "nloc": self.metric.nloc,
                "tokens": self.metric.tokens,
            },
            "signals": self.signals,
            "composite_static": round(self.composite_static, 6),
            "reachability": self.reachability,
            "final_score": round(self.final_score, 6),
            "semantic": asdict(self.semantic),
        }


def build_semantic_anchors(repo: Path) -> list[dict]:
    """Extract (source_path, symbol, field/surface, access) anchors from the
    discovery semantic schema. Returns [] on failure (metrics still run, but no
    surface can be matched)."""
    try:
        from ..discovery import RepositoryInspector
    except Exception:
        return []
    try:
        result = RepositoryInspector().inspect(repo)
    except Exception:
        return []
    schema = getattr(result, "semantic_schema", None)
    if schema is None:
        return []
    anchors: list[dict] = []
    for fld in getattr(schema, "fields", []) or []:
        for ev in getattr(fld, "evidence", []) or []:
            anchors.append(
                {
                    "source_path": (ev.source_path or "").lstrip("./"),
                    "symbol": ev.matched_symbol,
                    "name": fld.name,
                    "kind": "field",
                    "readable_via": list(getattr(fld, "readable_via", []) or []),
                    "writable_via": list(getattr(fld, "writable_via", []) or []),
                }
            )
    for surf in getattr(schema, "surfaces", []) or []:
        for ev in getattr(surf, "evidence", []) or []:
            anchors.append(
                {
                    "source_path": (ev.source_path or "").lstrip("./"),
                    "symbol": ev.matched_symbol,
                    "name": surf.name,
                    "kind": "surface",
                    "readable_via": list(getattr(surf, "access", []) or []),
                    "writable_via": list(getattr(surf, "access", []) or []),
                }
            )
    return anchors


def _match_semantics(repo: Path, m: FunctionMetric, anchors: list[dict]) -> SemanticMatch:
    if not anchors:
        return SemanticMatch(on_surface=False)
    try:
        body = "\n".join(
            (repo / m.file).read_text(encoding="utf-8", errors="ignore").splitlines()[
                max(0, m.start_line - 1) : m.end_line
            ]
        )
    except Exception:
        body = ""
    norm_file = m.file.lstrip("./")
    fields: list[str] = []
    symbols: list[str] = []
    writable: set[str] = set()
    readable: set[str] = set()
    for a in anchors:
        ap = a["source_path"]
        same_file = ap and (norm_file == ap or norm_file.endswith("/" + ap) or ap.endswith("/" + norm_file))
        if not same_file:
            continue
        sym = a["symbol"] or ""
        if sym and sym in body:
            fields.append(a["name"])
            symbols.append(sym)
            writable.update(a["writable_via"])
            readable.update(a["readable_via"])
    fields = sorted(set(fields))
    return SemanticMatch(
        on_surface=bool(fields),
        matched_fields=fields,
        matched_symbols=sorted(set(symbols)),
        writable_via=sorted(writable),
        readable_via=sorted(readable),
    )


def rank_candidates(
    repo: Path,
    metrics: list[FunctionMetric],
    *,
    game: str,
    cfg: MetricConfig | None = None,
    anchors: list[dict] | None = None,
    reach_descriptor: dict | None = None,
    require_surface: bool = True,
    reach_trials: int = 200,
) -> list[Candidate]:
    cfg = cfg or default_config()
    if anchors is None:
        anchors = build_semantic_anchors(repo)

    # Filter trivial functions and anonymous noise before ranking.
    pool = [m for m in metrics if m.cyclomatic >= cfg.min_cyclomatic and m.name != "(anonymous)"]
    if not pool:
        return []

    signal_specs = {
        "cyclomatic": [float(m.cyclomatic) for m in pool],
        "nesting": [float(m.nesting) for m in pool],
        "fan_in": [float(m.fan_in) for m in pool],
        "fan_out": [float(m.fan_out) for m in pool],
        "churn": [float(m.churn_commits + 0.01 * m.churn_lines) for m in pool],
    }
    ranked_signals = {name: percentile_ranks(vals) for name, vals in signal_specs.items()}

    # Reachability descriptor is per-game; computed once and shared.
    reach: ReachResult | None = None
    if reach_descriptor is not None:
        reach = unreachability(game, reach_descriptor, trials=reach_trials)

    candidates: list[Candidate] = []
    for idx, m in enumerate(pool):
        signals = {name: ranked_signals[name][idx] for name in signal_specs}
        composite = sum(cfg.weights[name] * signals[name] for name in cfg.weights)
        if reach is not None:
            unreach = reach.unreachability
            final = composite * (cfg.reachability_blend + (1 - cfg.reachability_blend) * unreach)
            reach_dict = {
                "descriptor": reach.descriptor,
                "reach_rate": round(reach.reach_rate, 6),
                "unreachability": round(reach.unreachability, 6),
                "method": reach.method,
            }
        else:
            final = composite
            reach_dict = {"unreachability": None, "method": "not-estimated"}
        sem = _match_semantics(repo, m, anchors)
        candidates.append(
            Candidate(
                metric=m,
                signals={k: round(v, 6) for k, v in signals.items()},
                composite_static=composite,
                reachability=reach_dict,
                final_score=final,
                semantic=sem,
            )
        )

    if require_surface:
        candidates = [c for c in candidates if c.semantic.on_surface]

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates
