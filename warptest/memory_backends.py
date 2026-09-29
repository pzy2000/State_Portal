"""Switchable agent-memory backends for EvoPortal checkpoint-recipe transfer.

This module lets the EvoPortal evolution loop retrieve checkpoint-recipe memories
through different, swappable backends so that StatePortal's own recipe memory can be
benchmarked head-to-head against published agent-memory systems:

  * ``stateportal``        - StatePortal's native BM25 + schema-bonus, intra-game
                             retrieval (delegates to :func:`evo.retrieve_memory`).
  * ``amem``               - A-MEM (Xu et al., NeurIPS 2025) agentic memory:
                             atomic notes with keywords/tags/context, Zettelkasten
                             linking, and memory evolution (note consolidation).
  * ``mem0``               - Mem0 (Chhikara et al., ECAI 2025) memory: salient-fact
                             extraction with ADD/UPDATE consolidation + dense search.
  * ``stateportal-anchor`` - StatePortal anchor: A-MEM note/link/evolution extended
                             with recipe abstraction levels, schema-family-gated
                             cross-game transfer, and evolution-driven recipe merging.

All backends operate on ``MemoryEntry`` objects (duck-typed) and a ``PublicIssue``
query, returning a ranked ``list[MemoryEntry]``. They run fully offline with a
deterministic hashing embedding so the comparison pipeline is reproducible without
API keys; the A-MEM and Mem0 backends optionally call the real upstream libraries
when those packages and credentials are available (see ``native`` flag), but the
default path is the faithful local reimplementation of each system's retrieval.

The backend abstraction only changes *which* recipes are surfaced to candidate
generation and repair; it never reads hidden grading material.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime circular import
    from .evo import MemoryEntry
    from .task_schema import PublicIssue

MEMORY_BACKEND_CHOICES = ("stateportal", "amem", "mem0", "stateportal-anchor")
MEMORY_SCOPE_CHOICES = ("intra", "cross", "both")

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_+-]+")
_STOPWORDS = {
    "the", "and", "that", "with", "from", "into", "can", "verify", "check",
    "for", "are", "was", "this", "have", "has", "but", "not", "you", "our",
}
_EMBED_DIM = 256


# ---------------------------------------------------------------------------
# Tokenization + deterministic embeddings (shared by all local backends so the
# comparison isolates memory *organization*, not embedding quality).
# ---------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _TOKEN_RE.findall(str(text).lower()):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        out.append(word)
    return out


def _entry_tokens(entry: "MemoryEntry") -> list[str]:
    terms: list[str] = [
        str(entry.benchmark).lower(),
        str(entry.semantic_surface).lower(),
        str(entry.strategy).lower(),
        str(entry.failure_signature).lower(),
        str(entry.entry_type).lower(),
    ]
    terms.extend(str(t).lower() for t in getattr(entry, "issue_keywords", []) or [])
    terms.extend(str(t).lower() for t in getattr(entry, "source_symbols", []) or [])
    terms.extend(_tokens(getattr(entry, "text_summary", "") or ""))
    recipe = getattr(entry, "checkpoint_recipe", {})
    if isinstance(recipe, dict) and recipe:
        terms.extend(_tokens(_flatten(recipe)))
    return [t for t in terms if t]


def _query_tokens(task: "PublicIssue") -> list[str]:
    terms = _tokens(getattr(task, "issue", "") or "")
    terms.extend(str(t).lower() for t in getattr(task, "tags", []) or [])
    terms.append(str(task.benchmark).lower())
    return [t for t in terms if t]


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


def _hash_embedding(tokens: Sequence[str], dim: int = _EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-words embedding, L2-normalized.

    Uses signed feature hashing so the same token always maps to the same slot,
    making retrieval reproducible across runs and machines without a model.
    """
    vec = [0.0] * dim
    for token in tokens:
        h = _stable_hash(token)
        idx = h % dim
        sign = 1.0 if (h >> 16) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _stable_hash(text: str) -> int:
    h = 1469598103934665603
    for ch in text:
        h ^= ord(ch)
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _filter_policy(entries: Sequence["MemoryEntry"], policy: str) -> list["MemoryEntry"]:
    if policy == "positive-only":
        return [e for e in entries if e.entry_type == "validated_recipe"]
    if policy == "textual-only":
        return [e for e in entries if e.entry_type == "textual_summary"]
    return [e for e in entries if e.entry_type in {"validated_recipe", "negative_contrast"}]


def _keyword_overlap(query: Sequence[str], doc: Sequence[str]) -> float:
    if not query or not doc:
        return 0.0
    qset = set(query)
    dset = set(doc)
    inter = len(qset & dset)
    return inter / math.sqrt(len(qset) * len(dset))


# ---------------------------------------------------------------------------
# Backend base + registry
# ---------------------------------------------------------------------------
class MemoryBackend:
    """Base class for swappable EvoPortal memory backends.

    Each backend declares which retrieval scopes it actually supports via
    ``SUPPORTED_SCOPES`` and its default (``NATIVE_SCOPE``). When a caller requests
    an unsupported scope, the backend does not silently pretend to honor it: it
    falls back to its native scope and records ``scope_honored=False`` plus a
    human-readable ``scope_warning`` so reports never claim a scope that was not
    actually applied. A-MEM and Mem0 are intentionally game-agnostic (they have no
    benchmark/game scope concept), so they support only ``both``.
    """

    name = "base"
    SUPPORTED_SCOPES: frozenset[str] = frozenset(MEMORY_SCOPE_CHOICES)
    NATIVE_SCOPE: str = "intra"

    def __init__(self, *, memory_scope: str = "intra", native: bool = False, link_threshold: int = 2) -> None:
        if memory_scope not in MEMORY_SCOPE_CHOICES:
            raise ValueError(f"memory_scope must be one of {MEMORY_SCOPE_CHOICES}")
        self.requested_scope = memory_scope
        if memory_scope in self.SUPPORTED_SCOPES:
            self.effective_scope = memory_scope
            self.scope_honored = True
            self.scope_warning = ""
        else:
            self.effective_scope = self.NATIVE_SCOPE
            self.scope_honored = False
            self.scope_warning = (
                f"{self.name} does not support memory_scope={memory_scope!r} "
                f"(game-agnostic); using {self.NATIVE_SCOPE!r}."
            )
        # memory_scope is the scope actually applied at retrieval time.
        self.memory_scope = self.effective_scope
        self.native = native
        self.link_threshold = link_threshold
        self.last_stats: dict[str, Any] = {}

    def retrieve(
        self,
        entries: Sequence["MemoryEntry"],
        task: "PublicIssue",
        *,
        limit: int = 5,
        policy: str = "positive-negative",
    ) -> list["MemoryEntry"]:
        raise NotImplementedError

    def inventory(self, entries: Sequence["MemoryEntry"]) -> dict[str, Any]:
        by_bench: dict[str, dict[str, int]] = {}
        validated = negative = textual = 0
        confidences: list[float] = []
        for e in entries:
            bucket = by_bench.setdefault(e.benchmark, {"entries": 0, "validated_recipe": 0, "negative_contrast": 0, "textual_summary": 0})
            bucket["entries"] += 1
            bucket[e.entry_type] = bucket.get(e.entry_type, 0) + 1
            confidences.append(float(getattr(e, "confidence", 0.0) or 0.0))
            if e.entry_type == "validated_recipe":
                validated += 1
            elif e.entry_type == "negative_contrast":
                negative += 1
            elif e.entry_type == "textual_summary":
                textual += 1
        return {
            "backend": self.name,
            "memory_scope": self.effective_scope,
            "requested_scope": self.requested_scope,
            "effective_scope": self.effective_scope,
            "scope_honored": self.scope_honored,
            "total_entries": len(entries),
            "validated_recipe": validated,
            "negative_contrast": negative,
            "textual_summary": textual,
            "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
            "by_benchmark": by_bench,
        }


class StatePortalBackend(MemoryBackend):
    """StatePortal native retrieval (BM25 + schema bonus, strict intra-game).

    Delegates to :func:`evo.retrieve_memory` so behavior is identical to the
    pre-adapter pipeline; this is the un-improved StatePortal control. Its scoring
    hard-filters to the same benchmark, so it supports only the ``intra`` scope.
    """

    name = "stateportal"
    SUPPORTED_SCOPES = frozenset({"intra"})
    NATIVE_SCOPE = "intra"

    def retrieve(self, entries, task, *, limit=5, policy="positive-negative"):
        from .evo import retrieve_memory  # lazy import avoids circular dependency

        result = retrieve_memory(entries, task, limit=limit, policy=policy)
        self.last_stats = {"backend": self.name, "candidates": len(entries), "returned": len(result)}
        return result


class AMemBackend(MemoryBackend):
    """A-MEM agentic memory: atomic notes, Zettelkasten linking, evolution.

    Faithful local reimplementation of A-MEM's retrieval path (Xu et al., 2025):
    each memory becomes a note with keywords/tags/context; notes are linked by
    shared keywords; retrieval is dense similarity + keyword match with
    link-based spreading activation; evolution consolidates near-duplicate notes.
    Game-agnostic by design (no hard intra-game filter), so it supports only the
    ``both`` scope; requesting ``intra``/``cross`` is reported as not honored.
    """

    name = "amem"
    SUPPORTED_SCOPES = frozenset({"both"})
    NATIVE_SCOPE = "both"

    def retrieve(self, entries, task, *, limit=5, policy="positive-negative"):
        pool = _filter_policy(entries, policy)
        if self.native:
            native = _try_native_amem(pool, task, limit=limit)
            if native is not None:
                self.last_stats = {"backend": self.name, "mode": "native", "returned": len(native)}
                return native

        notes = _evolve_notes(pool, schema_family_gate=False)
        q_tokens = _query_tokens(task)
        q_embed = _hash_embedding(q_tokens)
        scored: list[tuple[float, Any]] = []
        for note in notes:
            sim = _cosine(q_embed, note["embedding"])
            overlap = _keyword_overlap(q_tokens, note["tokens"])
            base = 0.6 * sim + 0.4 * overlap + 0.05 * float(note["entry"].confidence)
            note["base"] = base
            scored.append((base, note))

        # Zettelkasten spreading activation: a note inherits a fraction of the
        # best base score among its linked neighbors.
        score_by_id = {note["entry"].id: note["base"] for _, note in scored}
        activated: list[tuple[float, "MemoryEntry"]] = []
        for base, note in scored:
            neighbor_boost = 0.0
            for nid in note["links"]:
                neighbor_boost = max(neighbor_boost, 0.25 * score_by_id.get(nid, 0.0))
            total = base + neighbor_boost
            if total > 0:
                entry = note["entry"]
                entry.score = round(total, 6)
                activated.append((total, entry))
        activated.sort(key=lambda item: (-item[0], item[1].entry_type, item[1].id))
        result = [entry for _, entry in activated[:limit]]
        self.last_stats = {
            "backend": self.name,
            "mode": "local",
            "candidates": len(pool),
            "notes_after_evolution": len(notes),
            "returned": len(result),
        }
        return result


class Mem0Backend(MemoryBackend):
    """Mem0 memory: salient-fact extraction + ADD/UPDATE consolidation + search.

    Faithful local reimplementation of Mem0's retrieval (Chhikara et al., 2025):
    memories are consolidated by salient key (UPDATE keeps the higher-confidence
    fact) and retrieved by dense similarity. No linking and no abstraction layer;
    those are deliberately left as differentiators for the StatePortal anchor.
    Game-agnostic like A-MEM, so it supports only the ``both`` scope.
    """

    name = "mem0"
    SUPPORTED_SCOPES = frozenset({"both"})
    NATIVE_SCOPE = "both"

    def retrieve(self, entries, task, *, limit=5, policy="positive-negative"):
        pool = _filter_policy(entries, policy)
        if self.native:
            native = _try_native_mem0(pool, task, limit=limit)
            if native is not None:
                self.last_stats = {"backend": self.name, "mode": "native", "returned": len(native)}
                return native

        consolidated = _consolidate_facts(pool)
        q_embed = _hash_embedding(_query_tokens(task))
        scored: list[tuple[float, "MemoryEntry"]] = []
        for entry in consolidated:
            embed = _hash_embedding(_entry_tokens(entry))
            sim = _cosine(q_embed, embed) + 0.05 * float(entry.confidence)
            if sim > 0:
                entry.score = round(sim, 6)
                scored.append((sim, entry))
        scored.sort(key=lambda item: (-item[0], item[1].entry_type, item[1].id))
        result = [entry for _, entry in scored[:limit]]
        self.last_stats = {
            "backend": self.name,
            "mode": "local",
            "candidates": len(pool),
            "facts_after_consolidation": len(consolidated),
            "returned": len(result),
        }
        return result


class StatePortalAnchorBackend(MemoryBackend):
    """StatePortal anchor: A-MEM extended for checkpoint-recipe transfer.

    Improvements over the plain A-MEM baseline, all aimed at deep-state recipe
    reuse on held-out tasks:

    1. Recipe abstraction levels - score a high-abstraction "insight" key
       (strategy + semantic surface + failure->fix signature) above raw recipe
       text, because abstract know-how transfers better than literal traces.
    2. Schema-family-gated cross-game transfer - intra-game recipes score at
       full weight; cross-game recipes contribute only when the semantic-surface
       / strategy family is compatible, with a controlled penalty
       (``memory_scope`` selects intra / cross / both).
    3. Evolution-driven recipe merging - validated recipes in the same contract
       family are consolidated, accumulating support_count into a confidence
       boost so robust recipes outrank one-off ones.
    """

    name = "stateportal-anchor"
    SUPPORTED_SCOPES = frozenset({"intra", "cross", "both"})
    NATIVE_SCOPE = "both"

    def __init__(self, *, memory_scope: str = "both", cross_penalty: float = 0.45, native: bool = False, link_threshold: int = 2) -> None:
        super().__init__(memory_scope=memory_scope, native=native, link_threshold=link_threshold)
        self.cross_penalty = cross_penalty

    def retrieve(self, entries, task, *, limit=5, policy="positive-negative"):
        pool = _filter_policy(entries, policy)
        notes = _evolve_notes(pool, schema_family_gate=True)
        q_tokens = _query_tokens(task)
        q_embed = _hash_embedding(q_tokens)
        task_insight = _insight_tokens_for_task(task)

        scored: list[tuple[float, Any]] = []
        cross_used = 0
        for note in notes:
            entry = note["entry"]
            same_game = entry.benchmark == task.benchmark
            if same_game and self.memory_scope == "cross":
                continue
            if not same_game and self.memory_scope == "intra":
                continue
            if not same_game and not _schema_family_compatible(task_insight, note["insight"]):
                continue

            sim = _cosine(q_embed, note["embedding"])
            overlap = _keyword_overlap(q_tokens, note["tokens"])
            insight_match = _keyword_overlap(task_insight, note["insight"])
            support_boost = 0.1 * math.log1p(float(getattr(entry, "support_count", 1) or 1))
            recipe_bonus = 0.2 if entry.entry_type == "validated_recipe" else 0.0
            base = 0.4 * sim + 0.25 * overlap + 0.35 * insight_match + recipe_bonus + support_boost + 0.05 * float(entry.confidence)
            if not same_game:
                base *= self.cross_penalty
                cross_used += 1
            note["base"] = base
            scored.append((base, note))

        score_by_id = {note["entry"].id: note["base"] for _, note in scored}
        activated: list[tuple[float, "MemoryEntry"]] = []
        for base, note in scored:
            neighbor_boost = 0.0
            for nid in note["links"]:
                neighbor_boost = max(neighbor_boost, 0.3 * score_by_id.get(nid, 0.0))
            total = base + neighbor_boost
            if total > 0:
                entry = note["entry"]
                entry.score = round(total, 6)
                activated.append((total, entry))
        activated.sort(key=lambda item: (-item[0], -float(getattr(item[1], "support_count", 1) or 1), item[1].entry_type, item[1].id))
        result = [entry for _, entry in activated[:limit]]
        self.last_stats = {
            "backend": self.name,
            "mode": "local",
            "memory_scope": self.memory_scope,
            "candidates": len(pool),
            "notes_after_evolution": len(notes),
            "cross_game_candidates": cross_used,
            "returned": len(result),
        }
        return result


# ---------------------------------------------------------------------------
# Shared note construction, linking, and evolution helpers
# ---------------------------------------------------------------------------
def _insight_key(entry: "MemoryEntry") -> tuple[str, str, str]:
    return (
        str(entry.strategy).lower(),
        _surface_family(str(entry.semantic_surface)),
        str(entry.failure_signature).lower(),
    )


def _surface_family(surface: str) -> str:
    base = re.split(r"[._:-]", surface.lower(), maxsplit=1)[0]
    return base or surface.lower()


def _insight_tokens(entry: "MemoryEntry") -> list[str]:
    strat, family, failure = _insight_key(entry)
    return [t for t in (strat, family, failure) if t]


def _insight_tokens_for_task(task: "PublicIssue") -> list[str]:
    tokens = [str(t).lower() for t in getattr(task, "tags", []) or []]
    tokens.extend(_tokens(getattr(task, "issue", "") or ""))
    tokens.append(_surface_family(task.benchmark))
    return tokens


def _schema_family_compatible(task_insight: Sequence[str], note_insight: Sequence[str]) -> bool:
    return len(set(task_insight) & set(note_insight)) >= 1


def _consolidate_facts(entries: Sequence["MemoryEntry"]) -> list["MemoryEntry"]:
    """Mem0-style ADD/UPDATE: keep the highest-confidence fact per salient key."""
    best: dict[tuple[str, str, str], "MemoryEntry"] = {}
    for entry in entries:
        key = (str(entry.benchmark), str(entry.semantic_surface).lower(), str(entry.strategy).lower())
        current = best.get(key)
        if current is None or float(entry.confidence) > float(current.confidence):
            best[key] = entry
    return list(best.values())


def _evolve_notes(entries: Sequence["MemoryEntry"], *, schema_family_gate: bool) -> list[dict[str, Any]]:
    """Build A-MEM notes with evolution (consolidation) and Zettelkasten links.

    Returns a list of note dicts: {entry, tokens, embedding, insight, links}.
    Evolution merges entries sharing a recipe signature, accumulating
    support_count and nudging confidence up; the surviving representative keeps
    the merged ids as link anchors.
    """
    from .evo import MemoryEntry  # lazy import to avoid circular dependency

    # --- evolution: merge entries with identical recipe signature ---
    clusters: dict[tuple, list["MemoryEntry"]] = {}
    for entry in entries:
        sig = (
            str(entry.benchmark),
            _surface_family(str(entry.semantic_surface)),
            str(entry.strategy).lower(),
            str(entry.entry_type),
        )
        clusters.setdefault(sig, []).append(entry)

    representatives: list["MemoryEntry"] = []
    for members in clusters.values():
        members.sort(key=lambda e: (e.entry_type != "validated_recipe", -float(e.confidence)))
        rep = members[0]
        if len(members) > 1:
            merged_support = sum(int(getattr(m, "support_count", 1) or 1) for m in members)
            merged_conf = min(0.99, float(rep.confidence) + 0.03 * (len(members) - 1))
            rep = MemoryEntry(
                id=rep.id,
                scope=rep.scope,
                benchmark=rep.benchmark,
                semantic_surface=rep.semantic_surface,
                issue_keywords=list(dict.fromkeys([k for m in members for k in m.issue_keywords])),
                strategy=rep.strategy,
                failure_signature=rep.failure_signature,
                checkpoint_recipe=rep.checkpoint_recipe,
                public_validation_summary=rep.public_validation_summary,
                source_symbols=list(dict.fromkeys([s for m in members for s in m.source_symbols])),
                repair_history=[h for m in members for h in m.repair_history],
                confidence=merged_conf,
                support_count=merged_support,
                entry_type=rep.entry_type,
                score=0.0,
                text_summary=rep.text_summary,
            )
        representatives.append(rep)

    notes: list[dict[str, Any]] = []
    for entry in representatives:
        tokens = _entry_tokens(entry)
        notes.append(
            {
                "entry": entry,
                "tokens": tokens,
                "embedding": _hash_embedding(tokens),
                "insight": _insight_tokens(entry),
                "links": [],
            }
        )

    # --- Zettelkasten linking: connect notes sharing keywords (and, when gated,
    #     a compatible schema family) ---
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            a, b = notes[i], notes[j]
            shared = len(set(a["tokens"]) & set(b["tokens"]))
            if shared < 2:
                continue
            if schema_family_gate and not _schema_family_compatible(a["insight"], b["insight"]):
                continue
            a["links"].append(b["entry"].id)
            b["links"].append(a["entry"].id)
    return notes


# ---------------------------------------------------------------------------
# Optional native upstream hooks (used only when native=True and deps present).
# These keep the fast offline path as default but allow real A-MEM / Mem0 runs.
# ---------------------------------------------------------------------------
def _try_native_amem(entries, task, *, limit):  # pragma: no cover - requires optional deps
    try:
        from agentic_memory.memory_system import AgenticMemorySystem  # type: ignore
    except Exception:
        return None
    try:
        system = AgenticMemorySystem()
        id_to_entry: dict[str, Any] = {}
        for entry in entries:
            mem_id = system.add_note(
                content=_flatten(getattr(entry, "checkpoint_recipe", {})) + " " + getattr(entry, "text_summary", ""),
                tags=[entry.benchmark, entry.strategy, entry.entry_type],
            )
            id_to_entry[mem_id] = entry
        hits = system.search_agentic(" ".join(_query_tokens(task)), k=limit)
        ordered = [id_to_entry[h["id"]] for h in hits if h.get("id") in id_to_entry]
        return ordered[:limit]
    except Exception:
        return None


def _try_native_mem0(entries, task, *, limit):  # pragma: no cover - requires optional deps
    try:
        from mem0 import Memory  # type: ignore
    except Exception:
        return None
    try:
        memory = Memory()
        for entry in entries:
            memory.add(
                _flatten(getattr(entry, "checkpoint_recipe", {})) + " " + getattr(entry, "text_summary", ""),
                user_id="stateportal",
                metadata={"id": entry.id, "benchmark": entry.benchmark},
            )
        hits = memory.search(" ".join(_query_tokens(task)), user_id="stateportal", limit=limit)
        by_id = {entry.id: entry for entry in entries}
        results = hits.get("results", hits) if isinstance(hits, dict) else hits
        ordered = []
        for hit in results or []:
            meta = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            entry = by_id.get(meta.get("id"))
            if entry is not None:
                ordered.append(entry)
        return ordered[:limit] or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_memory_backend(name: str, *, memory_scope: str = "intra", native: bool = False) -> MemoryBackend:
    """Construct a memory backend, passing the requested scope through unchanged.

    Each backend resolves the requested scope against its ``SUPPORTED_SCOPES`` in
    ``__init__`` and records whether the request was honored, so callers always get
    a truthful ``effective_scope``/``scope_honored`` instead of a silently coerced
    one.
    """
    if name not in MEMORY_BACKEND_CHOICES:
        raise ValueError(f"memory_backend must be one of {MEMORY_BACKEND_CHOICES}; got {name!r}")
    if name == "stateportal":
        return StatePortalBackend(memory_scope=memory_scope, native=native)
    if name == "amem":
        return AMemBackend(memory_scope=memory_scope, native=native)
    if name == "mem0":
        return Mem0Backend(memory_scope=memory_scope, native=native)
    return StatePortalAnchorBackend(memory_scope=memory_scope, native=native)
