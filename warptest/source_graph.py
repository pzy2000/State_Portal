from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import InspectResult


def build_source_state_graph(inspect_result: InspectResult) -> dict[str, Any]:
    """Build a compact source path to semantic state/action graph."""

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for surface in inspect_result.semantic_schema.surfaces:
        evidence = surface.evidence[0] if surface.evidence else None
        node_id = f"surface:{surface.name}"
        nodes.append(
            {
                "id": node_id,
                "type": "surface",
                "name": surface.name,
                "kind": surface.kind,
                "access": surface.access,
                "source_path": evidence.source_path if evidence else surface.source_path,
                "matched_symbol": evidence.matched_symbol if evidence else None,
                "confidence": evidence.confidence if evidence else None,
            }
        )
    for field in inspect_result.semantic_schema.fields:
        evidence = field.evidence[0] if field.evidence else None
        node_id = f"field:{field.name}"
        nodes.append(
            {
                "id": node_id,
                "type": "field",
                "name": field.name,
                "source": field.source,
                "readable_via": field.readable_via,
                "writable_via": field.writable_via,
                "source_path": evidence.source_path if evidence else None,
                "matched_symbol": evidence.matched_symbol if evidence else None,
                "confidence": evidence.confidence if evidence else None,
            }
        )
        for access in field.readable_via + field.writable_via:
            edges.append({"from": access, "to": node_id, "kind": "semantic-access"})
    return {
        "repo": inspect_result.metadata.repo_path,
        "profile": inspect_result.profile,
        "nodes": nodes,
        "edges": edges,
        "limitations": inspect_result.semantic_schema.limitations,
    }


def locate_issue_evidence(issue: str, graph: dict[str, Any], *, max_items: int = 8) -> list[dict[str, Any]]:
    tokens = _tokens(issue)
    scored: list[tuple[int, dict[str, Any]]] = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        haystack = " ".join(str(node.get(key, "")) for key in ["name", "kind", "source", "matched_symbol", "source_path"]).lower()
        score = sum(1 for token in tokens if token and token in haystack)
        if score == 0 and node.get("type") == "surface":
            score = 1
        if score:
            scored.append((score, node))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("source_path", "")), str(item[1].get("name", ""))))
    evidence: list[dict[str, Any]] = []
    for score, node in scored[:max_items]:
        evidence.append(
            {
                "source_path": node.get("source_path") or "<unknown>",
                "matched_symbol": node.get("matched_symbol") or node.get("name"),
                "confidence": min(0.99, 0.55 + score * 0.1),
                "rationale": f"Matched public issue terms against {node.get('type')} {node.get('name')}.",
            }
        )
    return evidence


def validate_source_evidence(root: str | Path, evidence: list[dict[str, Any]]) -> list[str]:
    base = Path(root).expanduser().resolve()
    errors: list[str] = []
    for item in evidence:
        source_path = item.get("source_path")
        if not isinstance(source_path, str) or not source_path or source_path == "<unknown>":
            errors.append("source_evidence item is missing source_path")
            continue
        candidate = Path(source_path)
        if not candidate.is_absolute():
            candidate = base / source_path
        if "benchmarks/core/hidden" in str(candidate):
            errors.append(f"source_evidence leaks hidden path: {source_path}")
        if not candidate.exists():
            # Some inspector evidence is repo-relative. Try resolving against the current repo root.
            repo = Path(evidence[0].get("repo", "")) if evidence and evidence[0].get("repo") else None
            if not repo or not (repo / source_path).exists():
                errors.append(f"source_evidence path does not exist: {source_path}")
    return errors


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9_]+", text.lower()) if len(token) >= 3}
