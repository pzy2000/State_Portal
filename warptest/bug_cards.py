"""Bug-card schema + dual-axis classification for the issue-guided fuzz pipeline.

A "bug card" is the frozen, provenance-carrying record of one upstream merged
fix PR, mined from ``luanti-org/luanti``. Cards are the shared substrate for both
downstream products: the structured fuzz priors (from ``fuzz_reachable`` cards)
and the SWE-bench-style replay dataset (from ``headless_replayable`` cards).

The schema and the classifier live here (pure, no network) so the miner and the
classifier scripts stay thin and testable. Classification follows Contract 1:
file paths are an INITIAL screening signal only and never yield "high" confidence
on their own; keyword corroboration lifts confidence to "medium"; only a human
``manual_override`` is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .upstream_contracts import (
    AXIS_FUZZ_REACHABLE,
    AXIS_HEADLESS_REPLAYABLE,
    FUZZ_REACHABLE_PATH_HINTS,
    HEADLESS_REPLAYABLE_PATH_HINTS,
    SCHEMA_VERSION,
)

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"

# Keyword corroboration for each axis (case-insensitive; scanned in title+body).
_FUZZ_REACHABLE_KEYWORDS = (
    "itemstack",
    "inventory",
    "nodemeta",
    "node meta",
    "metadata",
    "param2",
    "unknown node",
    "set_node",
    "get_node",
    "itemdef",
    "nodedef",
    "mod_storage",
    "registered node",
    "register_node",
)
_HEADLESS_REPLAYABLE_KEYWORDS = (
    "deserialize",
    "deserialization",
    "serialize",
    "mapblock",
    "map block",
    "world load",
    "load the world",
    "on load",
    "mapgen",
    "map generation",
    "settings",
    "minetest.conf",
    "world.mt",
    "on startup",
    "server start",
    "corrupt",
    "malformed",
)


@dataclass
class AxisVerdict:
    """One reachability-axis classification result for a card."""

    value: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: str = CONFIDENCE_LOW
    manual_override: bool | None = None

    @property
    def effective(self) -> bool:
        return self.manual_override if self.manual_override is not None else self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "effective": self.effective,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "manual_override": self.manual_override,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AxisVerdict":
        data = data or {}
        return cls(
            value=bool(data.get("value", False)),
            reason=str(data.get("reason", "")),
            evidence=list(data.get("evidence") or []),
            confidence=str(data.get("confidence", CONFIDENCE_LOW)),
            manual_override=data.get("manual_override"),
        )


# Ordered fields of a bug card. The miner fills provenance; the classifier fills
# the ``classification`` block. ``body`` is retained for human review + reference
# extraction but MUST NOT be injected verbatim into any LLM prompt (priors are
# distilled to structured recipes instead).
CARD_FIELDS = (
    "schema_version",
    "benchmark",
    "upstream_repo",
    "fork_repo",
    "fetched_at",
    "source_url",
    "pr_number",
    "title",
    "body",
    "labels",
    "matched_labels",
    "closing_issues",  # from GraphQL closingIssuesReferences
    "body_closing_refs",  # from body "fixes/closes #N"
    "related_issues",  # other #N references
    "merged_at",
    "merge_commit",
    "pre_fix_sha",
    "post_fix_sha",
    "base_ref_oid",  # provenance only (Contract 2: never used as pre-fix build)
    "head_ref_oid",  # provenance only
    "changed_files",
    "patch_digest",
    "classification",
    "notes",
)


def new_card(**kwargs: Any) -> dict[str, Any]:
    """Create a bug-card dict with all schema fields present (missing -> defaults)."""

    card: dict[str, Any] = {name: None for name in CARD_FIELDS}
    card["schema_version"] = SCHEMA_VERSION
    card["labels"] = []
    card["matched_labels"] = []
    card["closing_issues"] = []
    card["body_closing_refs"] = []
    card["related_issues"] = []
    card["changed_files"] = []
    card["notes"] = []
    card.update(kwargs)
    return card


def validate_card(card: dict[str, Any]) -> list[str]:
    """Return a list of schema problems (empty => valid enough to persist)."""

    problems: list[str] = []
    if card.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version != {SCHEMA_VERSION}")
    if not card.get("pr_number"):
        problems.append("missing pr_number")
    if not card.get("post_fix_sha"):
        problems.append("missing post_fix_sha")
    if not card.get("pre_fix_sha"):
        problems.append("missing pre_fix_sha")
    return problems


# --------------------------------------------------------------------------- #
# Dual-axis classifier
# --------------------------------------------------------------------------- #


def _path_hits(changed_files: list[str], hints: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for path in changed_files or []:
        low = str(path).lower()
        for hint in hints:
            if hint in low:
                hits.append(str(path))
                break
    return hits


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    low = (text or "").lower()
    return [kw for kw in keywords if kw in low]


def _classify_axis(
    changed_files: list[str],
    text: str,
    path_hints: tuple[str, ...],
    keywords: tuple[str, ...],
) -> AxisVerdict:
    path_hits = _path_hits(changed_files, path_hints)
    kw_hits = _keyword_hits(text, keywords)
    evidence = [f"path:{p}" for p in path_hits] + [f"kw:{k}" for k in kw_hits]
    if not path_hits and not kw_hits:
        return AxisVerdict(
            value=False,
            reason="no path or keyword signal matched",
            evidence=[],
            confidence=CONFIDENCE_LOW,
        )
    # Contract 1: paths alone never exceed low confidence; keyword corroboration
    # (ideally alongside a path hit) lifts to medium. High is reserved for humans.
    if path_hits and kw_hits:
        confidence = CONFIDENCE_MEDIUM
        reason = "path hint corroborated by keyword(s)"
    elif kw_hits:
        confidence = CONFIDENCE_LOW
        reason = "keyword signal only (no path hint) -- weak"
    else:
        confidence = CONFIDENCE_LOW
        reason = "path hint only (initial screening) -- weak"
    return AxisVerdict(
        value=True, reason=reason, evidence=evidence, confidence=confidence
    )


def classify_card(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compute both reachability axes for a card (does not mutate the card)."""

    changed_files = card.get("changed_files") or []
    text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
    fuzz = _classify_axis(
        changed_files, text, FUZZ_REACHABLE_PATH_HINTS, _FUZZ_REACHABLE_KEYWORDS
    )
    headless = _classify_axis(
        changed_files,
        text,
        HEADLESS_REPLAYABLE_PATH_HINTS,
        _HEADLESS_REPLAYABLE_KEYWORDS,
    )
    # Preserve any pre-existing manual overrides so re-running the classifier is
    # idempotent and never clobbers human review.
    existing = card.get("classification") or {}
    for axis_name, verdict in (
        (AXIS_FUZZ_REACHABLE, fuzz),
        (AXIS_HEADLESS_REPLAYABLE, headless),
    ):
        prev = existing.get(axis_name) if isinstance(existing, dict) else None
        if isinstance(prev, dict) and prev.get("manual_override") is not None:
            verdict.manual_override = bool(prev["manual_override"])
    return {
        AXIS_FUZZ_REACHABLE: fuzz.to_dict(),
        AXIS_HEADLESS_REPLAYABLE: headless.to_dict(),
    }


def axis_effective(card: dict[str, Any], axis: str) -> bool:
    """Effective (override-aware) value of one axis for a classified card."""

    classification = card.get("classification") or {}
    verdict = AxisVerdict.from_dict(classification.get(axis))
    return verdict.effective


def axis_eligible_for_downstream(card: dict[str, Any], axis: str) -> bool:
    """Whether an axis verdict is strong enough to drive generated datasets.

    Low-confidence path/keyword matches remain review candidates, but they cannot
    create fuzz priors or replay tasks without an explicit human override.
    """

    classification = card.get("classification") or {}
    verdict = AxisVerdict.from_dict(classification.get(axis))
    if verdict.manual_override is not None:
        return verdict.manual_override
    return verdict.value and verdict.confidence in {CONFIDENCE_MEDIUM, CONFIDENCE_HIGH}
