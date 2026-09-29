#!/usr/bin/env python3
"""Annotate mined bug cards with the two independent reachability axes.

Reads ``bug_cards.jsonl`` (from ``mine_luanti_issues.py``), computes both
``fuzz_reachable`` and ``headless_replayable`` per Contract 1 (file paths are only
an initial screening signal; keyword corroboration lifts confidence to medium;
"high" is reserved for a human ``manual_override``), and writes the classified
cards back (idempotent: existing manual overrides are preserved).

An optional ``--overrides`` JSON lets a human pin a card's axis:
    {"16502": {"headless_replayable": true}, "17330": {"fuzz_reachable": false}}

Usage:
    python scripts/classify_luanti_cards.py
    python scripts/classify_luanti_cards.py --overrides benchmarks/upstream/luanti/overrides.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.bug_cards import (  # noqa: E402
    CONFIDENCE_HIGH,
    axis_effective,
    classify_card,
)
from warptest.upstream_contracts import (  # noqa: E402
    AXIS_FUZZ_REACHABLE,
    AXIS_HEADLESS_REPLAYABLE,
)


def _load_cards(path: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cards.append(json.loads(line))
    return cards


def _apply_overrides(card: dict[str, Any], override: dict[str, Any]) -> None:
    classification = card.setdefault("classification", {})
    for axis in (AXIS_FUZZ_REACHABLE, AXIS_HEADLESS_REPLAYABLE):
        if axis in override:
            verdict = classification.setdefault(axis, {})
            verdict["manual_override"] = bool(override[axis])
            verdict["confidence"] = CONFIDENCE_HIGH
            verdict["effective"] = bool(override[axis])
            reason = verdict.get("reason", "")
            verdict["reason"] = (reason + " | manual override").strip(" |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cards", default="benchmarks/upstream/luanti/bug_cards.jsonl")
    ap.add_argument("--out", default=None, help="Output path (defaults to in-place on --cards).")
    ap.add_argument("--overrides", default=None, help="Optional JSON of {pr_number: {axis: bool}} manual overrides.")
    args = ap.parse_args()

    cards_path = (ROOT / args.cards).resolve()
    out_path = (ROOT / args.out).resolve() if args.out else cards_path
    overrides: dict[str, Any] = {}
    if args.overrides:
        overrides = json.loads((ROOT / args.overrides).read_text(encoding="utf-8"))

    cards = _load_cards(cards_path)
    counts: Counter[str] = Counter()
    for card in cards:
        card["classification"] = classify_card(card)
        ov = overrides.get(str(card.get("pr_number")))
        if ov:
            _apply_overrides(card, ov)
        if axis_effective(card, AXIS_FUZZ_REACHABLE):
            counts["fuzz_reachable"] += 1
        if axis_effective(card, AXIS_HEADLESS_REPLAYABLE):
            counts["headless_replayable"] += 1
        if axis_effective(card, AXIS_FUZZ_REACHABLE) and axis_effective(card, AXIS_HEADLESS_REPLAYABLE):
            counts["both"] += 1
        if not axis_effective(card, AXIS_FUZZ_REACHABLE) and not axis_effective(card, AXIS_HEADLESS_REPLAYABLE):
            counts["neither"] += 1

    with out_path.open("w", encoding="utf-8") as fh:
        for card in cards:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")

    print(f"Classified {len(cards)} cards -> {out_path.relative_to(ROOT)}")
    print(
        f"  fuzz_reachable={counts['fuzz_reachable']}  "
        f"headless_replayable={counts['headless_replayable']}  "
        f"both={counts['both']}  neither={counts['neither']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
