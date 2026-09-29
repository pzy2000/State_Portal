"""Structured fuzz priors: distilled mutation recipes + deterministic seeding.

Contract 5 (from the plan): priors must NOT be a natural-language list, and the raw
upstream PR body / diff must NEVER be injected into a prompt. Instead each prior is
a STRUCTURED mutation recipe aligned to the current C3 ``target`` schema, e.g.

    {"target_path": "nodes[].param2", "op": "field_value", "values": [-1, 255, 256]}

``materialize_prior_seeds`` applies recipes DETERMINISTICALLY on top of an existing
oracle target, so a run with the same oracle + priors always produces the same
prior-seed set (independent of the LLM temperature and the Python RNG). Each
generated seed carries its provenance (recipe id + source PR + value).

A short, structured ``priors_hint`` (paths + boundary values + one-line rationale)
is the only thing surfaced to the LLM prompts.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .upstream_contracts import SCHEMA_VERSION, sha256_text

# Cap prior seeds so they seed/steer the loop without dominating a short budget.
DEFAULT_MAX_PRIOR_SEEDS = 12


class NormalizedPriors:
    """A validated, in-memory view of a priors file (recipes + provenance)."""

    def __init__(self, data: dict[str, Any], *, source_path: Path | None = None, raw_text: str | None = None) -> None:
        self.benchmark = str(data.get("benchmark", ""))
        self.recipes = [r for r in (data.get("recipes") or []) if _valid_recipe(r)]
        self.source_cards = data.get("source_cards") or []
        self.source_path = str(source_path) if source_path else None
        self.sha256 = sha256_text(raw_text) if raw_text is not None else None

    def __bool__(self) -> bool:
        return bool(self.recipes)

    @property
    def recipe_ids(self) -> list[str]:
        return [str(r.get("id")) for r in self.recipes]

    def hint(self, limit: int = 16) -> str:
        """Compact, structured prompt hint. No PR body / diff -- paths + values only."""

        if not self.recipes:
            return ""
        lines = [
            "Historically fragile dimensions in this engine (target-schema paths that "
            "past bug fixes touched). Prefer perturbing these when it fits the masked field:",
        ]
        for recipe in self.recipes[:limit]:
            vals = recipe.get("values") or []
            vals_txt = ", ".join(str(v) for v in vals[:6])
            rationale = str(recipe.get("rationale") or "").strip()
            suffix = f" -- {rationale}" if rationale else ""
            lines.append(f"  - {recipe['target_path']} -> [{vals_txt}]{suffix}")
        return "\n".join(lines)


def _valid_recipe(recipe: Any) -> bool:
    return (
        isinstance(recipe, dict)
        and isinstance(recipe.get("target_path"), str)
        and bool(recipe.get("target_path"))
        and isinstance(recipe.get("values"), list)
        and bool(recipe.get("values"))
    )


def load_priors(path: str | Path) -> NormalizedPriors:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    return NormalizedPriors(data, source_path=p, raw_text=raw)


def resolve_priors_arg(benchmark: str, arg: str | None, *, root: Path) -> NormalizedPriors | None:
    """Resolve the ``--priors auto|none|PATH`` argument to normalized priors.

    ``none`` (or empty) -> None (no-prior control). ``auto`` -> the conventional
    ``benchmarks/upstream/<benchmark>/fuzz_priors.json`` if it exists, else None.
    Any other value is treated as an explicit path.
    """

    if not arg or arg == "none":
        return None
    if arg == "auto":
        candidate = root / "benchmarks" / "upstream" / benchmark / "fuzz_priors.json"
        return load_priors(candidate) if candidate.exists() else None
    return load_priors(Path(arg) if Path(arg).is_absolute() else root / arg)


# --------------------------------------------------------------------------- #
# Deterministic path setter (grammar: name, name[], dotted, nested [])
# --------------------------------------------------------------------------- #

def _apply_path(obj: Any, tokens: list[str], value: Any) -> bool:
    """Set ``value`` at the token path in-place. Returns True if anything was set.

    A ``[]`` token applies to every element of a list (so ``nodes[].param2`` sets
    param2 on every node). Only sets where the container already exists -- it never
    fabricates missing structure, so a recipe that doesn't fit the oracle is skipped.
    """

    if not tokens:
        return False
    token = tokens[0]
    rest = tokens[1:]
    if token.endswith("[]"):
        key = token[:-2]
        container = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(container, list) or not container:
            return False
        touched = False
        for item in container:
            if not rest:
                # Terminal list: cannot set a scalar on list elements without a key.
                return False
            if _apply_path(item, rest, value):
                touched = True
        return touched
    if not isinstance(obj, dict):
        return False
    if not rest:
        if token in obj:
            obj[token] = value
            return True
        return False
    child = obj.get(token)
    if child is None:
        return False
    return _apply_path(child, rest, value)


def _tokenize(target_path: str) -> list[str]:
    return [t for t in target_path.split(".") if t]


def materialize_prior_seeds(
    oracle_target: dict[str, Any],
    priors: NormalizedPriors | None,
    *,
    max_seeds: int = DEFAULT_MAX_PRIOR_SEEDS,
) -> list[tuple[dict[str, Any], str]]:
    """Deterministically build prior seeds by applying recipes to the oracle target.

    Returns ``[(seed_target, provenance), ...]``. Recipes that do not fit the oracle
    structure are skipped (no fabricated fields). Order is stable (recipe order,
    then value order), so results are reproducible.
    """

    if not priors:
        return []
    seeds: list[tuple[dict[str, Any], str]] = []
    for recipe in priors.recipes:
        tokens = _tokenize(str(recipe["target_path"]))
        rid = str(recipe.get("id") or recipe["target_path"])
        src_pr = recipe.get("source_pr")
        for value in recipe["values"]:
            if len(seeds) >= max_seeds:
                return seeds
            candidate = copy.deepcopy(oracle_target)
            if _apply_path(candidate, tokens, value):
                prov = f"prior:{rid}:{value}"
                if src_pr:
                    prov += f":pr{src_pr}"
                seeds.append((candidate, prov))
    return seeds
