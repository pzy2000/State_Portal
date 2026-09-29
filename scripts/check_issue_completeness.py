#!/usr/bin/env python3
"""Verify that public issues expose every non-recoverable task-author fact.

By default this checks the core benchmark.  ``--c1-expansion`` checks one
named C1 expansion set against its C1 oracle specs.  Timing, generic health
checks, adapter operators, source/schema mechanics, and hidden grader
representation names are deliberately excluded.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.c1_evaluation import semantic_assertions

TIMING = {
    "wait_ms",
    "ms",
    "timeout_ms",
    "timeout_s",
    "delay_ms",
    "ticks",
    "timeout_ticks",
    "max_ticks",
    "wait_until_complete",
}
CORE_FORBIDDEN = (
    "recover this exact",
    "reproduce every field",
    "in order; reproduce",
    "hidden target",
    "hidden oracle",
    "hidden_spec",
    "specs/",
    "wait_ms",
    "target: {",
    "allowed_strategies=",
    "expected_validity=",
)
C1_FORBIDDEN = CORE_FORBIDDEN + (
    "[data-",
    "nth-child",
    "registered",
    "configured",
    "the required item",
    "oracle_spec",
    "golden",
    "rubric",
    "timeout",
    "hook_call",
    "hook_action",
    "spacerpg_mine_sortie",
    "luanti_set_",
    "luanti_fill",
    "luanti_save_world",
    "openra_",
    "jupyterlab_",
    "libreoffice_",
    "blender_",
    "jynew_",
    "pal3_",
)

C1_TARGET_SCHEMA_KEYS = {"kind", "role", "screen", "list"}
C1_ACTION_SCHEMA_KEYS = {
    "type",
    "method",
    "action_id",
    "queue",
    "list",
    "selector",
}
C1_SEMANTIC_ALIASES = {
    "station": ("station", "hangar", "market", "docked"),
    "basenodesgravel": ("gravel",),
    "basenodeswatersource": ("water",),
    "basenodesstone": ("stone",),
    "basenodescobble": ("cobble",),
    "basenodestree": ("tree",),
    "basenodesdirtwithgrass": ("grass",),
    "basenodesjunglegrass": ("jungle grass",),
    "basenodesapple": ("apple",),
    "basenodesleaves": ("leaves",),
    "basenodesmossycobble": ("mossy cobble",),
    "basetoolspicksteel": ("steel pick",),
    "stairsstaircobble": ("cobble stair",),
    "stairsslabcobble": ("cobble slab",),
    "stairsslabstone": ("stone slab",),
    "chestchest": ("chest",),
    "testnodeslight14": ("maximum brightness test light",),
    "warptesthomesteadstage": ("homestead stage",),
    "warptestorder": ("order state", "order"),
    "fact": ("construction yard",),
    "powr": ("power plant",),
    "proc": ("refinery",),
    "tent": ("allied barracks",),
    "weap": ("war factory",),
    "barr": ("soviet barracks",),
    "dome": ("radar dome",),
    "e1": ("rifle infantry",),
    "e3": ("rocket infantry",),
    "2tnk": ("medium tank",),
    "rotationeuler": ("euler rotation",),
    "char1": ("Elena",),
    "datecoffee": ("coffee date",),
    "completedelenagalleryarc": ("gallery",),
    "herocube": ("cube", "location and Euler rotation"),
}
NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
}


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def selector_target_names(selector: Any) -> list[str]:
    """Return the named UI targets a DOM selector points at.

    A pixel agent is never handed CSS, so the selector text itself is adapter
    mechanics; the named target inside it (``[data-action-id="date_coffee"]``
    names ``date_coffee``) stays a task-author fact the public steps must
    state.  A positional selector names nothing and keeps its verbatim
    requirement.
    """

    if not isinstance(selector, str):
        return []
    return re.findall(r'"([^"]+)"', selector)


def collect(value: Any, out: list[tuple[str, str]], *, skip_keys: set[str] | None = None) -> None:
    skip = skip_keys or set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in TIMING or key in skip:
                continue
            collect(nested, out, skip_keys=skip)
    elif isinstance(value, list):
        for nested in value:
            collect(nested, out, skip_keys=skip)
    elif value is not None:
        if isinstance(value, bool):
            out.append(("true" if value else "false", "bool"))
        elif isinstance(value, (int, float)):
            numeric = float(value)
            out.append((str(int(numeric)) if numeric.is_integer() else str(numeric), "num"))
        elif "\n" in str(value):
            out.append((str(value), "multiline"))
        else:
            out.append((str(value), "str"))


def collect_assertion_facts(spec: dict[str, Any], out: list[tuple[str, str]]) -> None:
    """Collect semantic result operands without leaking grader class/path syntax."""

    for assertion in semantic_assertions(spec) or []:
        assertion_type = str(assertion.get("type") or "")
        if assertion_type.startswith("no_"):
            continue
        collect(assertion, out, skip_keys={"type", "path", "comparator"})


def recoverable(issue: str, raw: str, kind: str, *, semantic: bool = False) -> bool:
    if kind == "num":
        if re.search(r"(?<![\d.])" + re.escape(raw) + r"(?![\d])", issue) is not None:
            return True
        if semantic:
            number = float(raw)
            if number == 0 and re.search(r"\b(?:zero|no|empty|without|absent|nothing)\b", issue.lower()):
                return True
            if number.is_integer() and int(number) in NUMBER_WORDS:
                return NUMBER_WORDS[int(number)] in issue.lower()
            if number < 0:
                absolute = str(abs(int(number))) if number.is_integer() else str(abs(number))
                return (
                    re.search(r"\b(?:spend|subtract|remove|decrease)[^\n.]*\b" + re.escape(absolute) + r"\b", issue.lower())
                    is not None
                )
        return False
    if kind == "bool":
        if raw in issue.lower():
            return True
        if not semantic:
            return False
        if raw == "true":
            return re.search(r"\b(?:enabled|active|used|playable|required|complete|win|present)\b", issue.lower()) is not None
        return re.search(r"\b(?:disabled|absent|empty|nothing|no|without|false)\b", issue.lower()) is not None
    if kind == "multiline":
        words = [word for word in re.split(r"[^a-z0-9]+", raw.lower()) if word]
        normalized_issue = norm(issue)
        return all(word in normalized_issue for word in words)
    normalized_issue = norm(issue)
    if norm(raw) in normalized_issue:
        return True
    if semantic:
        return any(norm(alias) in normalized_issue for alias in C1_SEMANTIC_ALIASES.get(norm(raw), ()))
    return False


def requirements(
    spec: dict[str, Any],
    *,
    include_assertions: bool,
    semantic: bool = False,
) -> list[tuple[str, str]]:
    required: list[tuple[str, str]] = []
    collect(
        spec.get("target"),
        required,
        skip_keys=C1_TARGET_SCHEMA_KEYS if semantic else {"kind"},
    )
    for action in spec.get("actions") or []:
        if str(action.get("type") or "").endswith("wait_ticks"):
            continue
        if semantic:
            collect(action, required, skip_keys=C1_ACTION_SCHEMA_KEYS)
            action_id = action.get("action_id")
            if action_id:
                required.append((str(action_id), "identifier_words"))
            selector = action.get("selector")
            names = selector_target_names(selector)
            if names:
                required.extend((name, "identifier_words") for name in names)
            elif selector is not None:
                required.append((str(selector), "str"))
        else:
            collect(action, required)
    if not semantic:
        checkpoint = spec.get("checkpoint") or {}
        for strategy in checkpoint.get("allowed_strategies", []):
            required.append((str(strategy), "str"))
        validity = checkpoint.get("expected_validity")
        if validity:
            required.append((str(validity), "str"))
    if include_assertions:
        collect_assertion_facts(spec, required)
    return required


def load_core() -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    tasks = yaml.safe_load((ROOT / "benchmarks/core/public/tasks.yaml").read_text())["tasks"]
    public = {task["id"]: task for task in tasks}
    oracles = yaml.safe_load((ROOT / "benchmarks/core/hidden/oracles.yaml").read_text())["oracles"]
    specs = {oracle["task_id"]: ROOT / oracle["hidden_spec"] for oracle in oracles}
    return public, specs


def load_c1_expansion(name: str) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    manifest = yaml.safe_load((ROOT / "benchmarks/p5/c1_tasks.yaml").read_text())
    expansion = (manifest.get("expansion_sets") or {}).get(name)
    if expansion is None:
        raise SystemExit(f"unknown C1 expansion set: {name}")
    by_id = {task["id"]: task for task in manifest["tasks"]}
    task_ids = expansion["task_ids"]
    public = {task_id: by_id[task_id] for task_id in task_ids}
    specs = {task_id: ROOT / by_id[task_id]["oracle_spec"] for task_id in task_ids}
    return public, specs


def check(
    public: dict[str, dict[str, Any]],
    specs: dict[str, Path],
    ids: list[str],
    *,
    c1: bool,
) -> bool:
    bad_any = False
    forbidden = C1_FORBIDDEN if c1 else CORE_FORBIDDEN
    for task_id in ids:
        if task_id not in public:
            print(f"UNKNOWN {task_id}")
            bad_any = True
            continue
        spec = yaml.safe_load(specs[task_id].read_text())
        if c1:
            preconditions_issue = str(public[task_id]["preconditions"])
            reference_steps_issue = str(public[task_id]["reference_steps"])
            issue = " ".join(
                [
                    preconditions_issue,
                    reference_steps_issue,
                    str(public[task_id]["state_description"]),
                ]
            )
            target_required = requirements(
                {
                    "target": spec.get("target"),
                    "actions": [],
                    "assertions": [],
                },
                include_assertions=False,
                semantic=True,
            )
            reference_steps_required = requirements(
                {
                    "target": {},
                    "actions": spec.get("actions") or [],
                    "assertions": [],
                },
                include_assertions=False,
                semantic=True,
            )
            requirement_groups = (
                (preconditions_issue, target_required),
                (reference_steps_issue, reference_steps_required),
            )
        else:
            issue = str(public[task_id]["issue"])
            requirement_groups = (
                (
                    issue,
                    requirements(
                        spec, include_assertions=False, semantic=False
                    ),
                ),
            )
        missing = sorted(
            {
                (raw, kind)
                for public_text, required in requirement_groups
                for raw, kind in required
                if not (
                    kind == "identifier_words"
                    and all(
                        word in norm(public_text)
                        for word in re.findall(r"[a-z0-9]+", str(raw).lower())
                    )
                )
                and not recoverable(
                    public_text, raw, kind, semantic=c1
                )
            }
        )
        fragments = [fragment for fragment in forbidden if fragment in issue.lower()]
        if missing or fragments:
            bad_any = True
            print(f"REVIEW {task_id}")
            if missing:
                print("   missing:", missing)
            if fragments:
                print("   forbidden:", fragments)
        else:
            print(f"OK     {task_id}")
    return not bad_any


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--c1-expansion", metavar="NAME")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.c1_expansion:
        public, specs = load_c1_expansion(args.c1_expansion)
        ids = args.ids or list(public)
        return 0 if check(public, specs, ids, c1=True) else 1
    public, specs = load_core()
    ids = args.ids or list(public)
    return 0 if check(public, specs, ids, c1=False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
