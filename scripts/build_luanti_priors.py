#!/usr/bin/env python3
"""Distill structured fuzz-mutation recipes from fuzz_reachable bug cards.

Reads classified ``bug_cards.jsonl`` and uses only downstream-eligible
``fuzz_reachable`` cards: corroborated medium/high-confidence verdicts or explicit
human opt-ins. Low-confidence screening hits and replay-only cards never leak into
priors. The output ``fuzz_priors.json`` contains STRUCTURED mutation recipes aligned
to the current C3 ``target`` schema, each tagged with its source PR (Contract 5).

Recipes are derived by matching a card's evidence (changed-file / keyword signals)
to boundary-value templates for the reachable target fields
(``nodes``/``fill``/``meta_inventory``/``mod_storage``). The raw PR body / diff is
never copied into the recipes -- only the structured path + boundary values + a
one-line rationale, plus provenance.

Usage:
    python scripts/build_luanti_priors.py
    python scripts/build_luanti_priors.py --cards ... --out benchmarks/upstream/luanti/fuzz_priors.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.bug_cards import axis_eligible_for_downstream  # noqa: E402
from warptest.upstream_contracts import AXIS_FUZZ_REACHABLE, SCHEMA_VERSION  # noqa: E402

# Boundary-value recipe templates for the reachable target fields. Each template is
# matched against a card's signal text (title + body + changed-file paths); a hit
# emits a recipe for that path, tagged with the card's PR. Values are i32/enum
# boundaries the engine has historically mishandled.
_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "param2",
        "target_path": "nodes[].param2",
        "op": "field_value",
        "values": [-1, 255, 256, 2147483647],
        "rationale": "param2 is a u8; negative / >255 has caused wrap/validation bugs",
        "signals": ["param2"],
    },
    {
        "key": "node-name",
        "target_path": "nodes[].node",
        "op": "type_flip",
        "values": ["", ":", "unknown:node", "ignore", "air "],
        "rationale": "unknown / empty / malformed node names hit content resolution",
        "signals": [
            "nodedef",
            "register_node",
            "unknown node",
            "content_",
            "node name",
        ],
    },
    {
        "key": "node-pos",
        "target_path": "nodes[].pos.x",
        "op": "field_value",
        "values": [-2147483648, 2147483647, 32768, -32768, 31001],
        "rationale": "out-of-range / MAP_GENERATION_LIMIT coordinates",
        "signals": [
            "coordinate",
            "position",
            "out of range",
            "map_generation_limit",
            "set_node",
            "getpos",
        ],
    },
    {
        "key": "item-count",
        "target_path": "meta_inventory[].items[].count",
        "op": "field_value",
        "values": [-1, 0, 65536, 2147483647],
        "rationale": "item stack count boundaries (0 / negative / > stack max)",
        "signals": ["itemstack", "inventory", "count", "stack", "swap"],
    },
    {
        "key": "item-name",
        "target_path": "meta_inventory[].items[].item",
        "op": "type_flip",
        "values": ["", "unknown:item", "default:stone 999999"],
        "rationale": "malformed / unknown item strings reach ItemStack parsing",
        "signals": ["itemstack", "itemdef", "item name", "malformed item"],
    },
    {
        "key": "mod-storage",
        "target_path": "mod_storage[].value",
        "op": "field_value",
        "values": ["", "\u0000", "9" * 40],
        "rationale": "mod-storage values: empty / NUL / oversized strings",
        "signals": ["mod_storage", "metadata", "get_string", "set_string"],
    },
]


def _card_signal_text(card: dict[str, Any]) -> str:
    parts = [str(card.get("title") or ""), str(card.get("body") or "")]
    parts += [str(p) for p in (card.get("changed_files") or [])]
    # Include classifier evidence so path-hint signals also count.
    classification = card.get("classification") or {}
    fr = classification.get(AXIS_FUZZ_REACHABLE) or {}
    parts += [str(e) for e in (fr.get("evidence") or [])]
    return "\n".join(parts).lower()


def build_recipes(
    cards: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reachable = [
        card
        for card in cards
        if axis_eligible_for_downstream(card, AXIS_FUZZ_REACHABLE)
    ]
    # Merge by template key: union of source PRs, first template's values/rationale.
    merged: dict[str, dict[str, Any]] = {}
    source_cards: list[dict[str, Any]] = []
    for card in reachable:
        source_cards.append(
            {
                "pr_number": card.get("pr_number"),
                "source_url": card.get("source_url"),
                "post_fix_sha": card.get("post_fix_sha"),
                "title": card.get("title"),
            }
        )
        text = _card_signal_text(card)
        for tmpl in _TEMPLATES:
            if not any(sig in text for sig in tmpl["signals"]):
                continue
            entry = merged.setdefault(
                tmpl["key"],
                {
                    "id": f"luanti-{tmpl['key']}",
                    "target_path": tmpl["target_path"],
                    "op": tmpl["op"],
                    "values": list(tmpl["values"]),
                    "rationale": tmpl["rationale"],
                    "source_prs": [],
                },
            )
            if card.get("pr_number") not in entry["source_prs"]:
                entry["source_prs"].append(card.get("pr_number"))
    recipes = list(merged.values())
    # Stamp each recipe with a representative source PR (first) for provenance.
    for recipe in recipes:
        recipe["source_pr"] = recipe["source_prs"][0] if recipe["source_prs"] else None
    return recipes, source_cards


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cards", default="benchmarks/upstream/luanti/bug_cards.jsonl")
    ap.add_argument("--out", default="benchmarks/upstream/luanti/fuzz_priors.json")
    args = ap.parse_args()

    cards_path = (ROOT / args.cards).resolve()
    cards = [
        json.loads(line)
        for line in cards_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    recipes, source_cards = build_recipes(cards)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "luanti",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_cards": source_cards,
        "recipes": recipes,
    }
    out = (ROOT / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {len(recipes)} recipe(s) from {len(source_cards)} fuzz_reachable card(s) -> {out.relative_to(ROOT)}"
    )
    for recipe in recipes:
        print(
            f"  {recipe['id']}: {recipe['target_path']} <- {recipe['values']}  (PRs {recipe['source_prs']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
