#!/usr/bin/env python3
"""Derive the per-task execution-channel column for the C1 task universe.

The paper separates two channels that are easy to conflate:

* the *observation* channel -- how a verdict reads the software (real process
  state plus real screenshots, identical for every arm); and
* the *execution* channel -- how the bounded target action is applied to the
  software after the checkpoint restores the Start state.

Only the execution channel varies, and it is what bounds the claim "we do not
replace pixel input". This script makes that channel an auditable artifact
instead of prose: it reads each C1 task's registered oracle spec, reads the
``actions[].type`` list, and assigns one of three channels.

Scope of this column: it describes how the registered *transition spec* reaches
the software, so it applies to the arms that execute that spec. The pixel-input
arms ignore the registered actions and ground their own clicks, so their channel
is a property of the arm rather than of the task and does not appear here. All
arms run all tasks, so these counts are task properties, not an allocation of
tasks across arms.

The action-type vocabulary is not restated here. It is extracted from
``warptest/spec_schema.py`` so that a schema change cannot silently desync the
reported column: any type observed in a spec but absent from the channel table
fails the run.

Output is content-addressed and free of timestamps, so a rerun on an unchanged
tree is byte-identical (the Gate A property claimed for the other provenance
artifacts).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASK_FILE = ROOT / "benchmarks/p5/c1_tasks.yaml"
SCHEMA_FILE = ROOT / "warptest/spec_schema.py"
DEFAULT_OUTPUT = ROOT / "reports/metrics/c1-execution-channel.json"

SCHEMA_VERSION = 1

# Channels, ordered from "no bypass" to "full bypass". A task inherits the
# most-bypassing channel among its non-neutral actions, because the reviewer's
# objection is bounded by the weakest link, not the average.
CHANNEL_NATIVE_INPUT = "native_input"
CHANNEL_UI_EVENT = "ui_event"
CHANNEL_SOURCE_API = "source_api"
CHANNEL_NEUTRAL = "neutral"

CHANNEL_ORDER = (CHANNEL_NATIVE_INPUT, CHANNEL_UI_EVENT, CHANNEL_SOURCE_API)

CHANNEL_DEFINITIONS: dict[str, str] = {
    CHANNEL_NATIVE_INPUT: (
        "Real input events delivered through the application's own input "
        "pipeline (keyboard/mouse events dispatched to the running app). The "
        "input path is not bypassed; only pixel grounding is."
    ),
    CHANNEL_UI_EVENT: (
        "Real UI event handlers reached through a structural selector rather "
        "than screen coordinates. The widget's handler runs, but hit-testing "
        "and occlusion are bypassed."
    ),
    CHANNEL_SOURCE_API: (
        "Source-level call into the application (test hook, engine API, UNO, "
        "bpy, worldmod, registered probe). The UI input path is bypassed "
        "entirely; only the observation channel remains pixel-faithful."
    ),
    CHANNEL_NEUTRAL: (
        "Lifecycle or pacing step that applies no target mutation (wait, "
        "restart). Excluded from a task's channel unless it is the only action."
    ),
}

# Explicit channel assignment. Exact types first, then prefix families. Every
# type observed in a registered spec must resolve here or the run fails.
EXACT_CHANNELS: dict[str, str] = {
    "move": CHANNEL_NATIVE_INPUT,
    "key": CHANNEL_NATIVE_INPUT,
    "click": CHANNEL_UI_EVENT,
    "hook_call": CHANNEL_SOURCE_API,
    "hook_action": CHANNEL_SOURCE_API,
    # Writes a raw payload straight into the persistence layer, so the UI input
    # path is bypassed even though no application code is called.
    "local_storage_set": CHANNEL_SOURCE_API,
    "wait": CHANNEL_NEUTRAL,
    "restart": CHANNEL_NEUTRAL,
    # Waits for the app's pending event queue to drain; applies no mutation.
    "settle_events": CHANNEL_NEUTRAL,
}

PREFIX_CHANNELS: tuple[tuple[str, str], ...] = (
    ("spacerpg_", CHANNEL_SOURCE_API),
    ("openra_", CHANNEL_SOURCE_API),
    ("jynew_", CHANNEL_SOURCE_API),
    ("pal3_", CHANNEL_SOURCE_API),
    ("luanti_", CHANNEL_SOURCE_API),
    ("blender_", CHANNEL_SOURCE_API),
    ("jupyterlab_", CHANNEL_SOURCE_API),
    ("libreoffice_", CHANNEL_SOURCE_API),
)

# Tasks whose spec carries no ``actions`` list express the transition as a named
# replay program instead. They are reported separately rather than folded into
# any channel, since the channel table cannot speak to them.
CHANNEL_REGISTERED_REPLAY = "registered_replay"
CHANNEL_EMPTY = "empty"


class DeriveError(RuntimeError):
    pass


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def channel_for(action_type: str) -> str | None:
    if action_type in EXACT_CHANNELS:
        return EXACT_CHANNELS[action_type]
    for prefix, channel in PREFIX_CHANNELS:
        if action_type.startswith(prefix):
            return channel
    return None


def schema_vocabulary(schema_file: Path) -> set[str]:
    """Extract the action-type vocabulary from ``_validate_action``.

    Only literal comparisons against the ``action_type`` local are collected
    (``== "x"`` and ``in {"x", "y"}``). The Blender family is dispatched through
    a module-level contract instead of literals, so it is imported.
    """

    tree = ast.parse(schema_file.read_text(encoding="utf-8"), filename=str(schema_file))
    targets = {"_validate_action", "_validate_blender_action"}
    vocab: set[str] = set()
    found: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in targets:
            continue
        found.add(node.name)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Compare):
                continue
            if not (isinstance(sub.left, ast.Name) and sub.left.id == "action_type"):
                continue
            for op, comparator in zip(sub.ops, sub.comparators):
                if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                    if isinstance(comparator.value, str):
                        vocab.add(comparator.value)
                elif isinstance(op, ast.In) and isinstance(
                    comparator, (ast.Set, ast.Tuple, ast.List)
                ):
                    for elt in comparator.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            vocab.add(elt.value)

    if "_validate_action" not in found:
        raise DeriveError(
            f"{schema_file}: _validate_action not found; the vocabulary source moved"
        )

    from warptest.spec_schema import BLENDER_PUBLIC_ACTION_CONTRACT  # noqa: E402

    for row in BLENDER_PUBLIC_ACTION_CONTRACT:
        action_type = row.get("type")
        if isinstance(action_type, str):
            vocab.add(action_type)

    if not vocab:
        raise DeriveError(f"{schema_file}: extracted an empty action vocabulary")
    return vocab


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def registered_replay_steps(spec: dict[str, Any]) -> list[str]:
    setup = spec.get("setup")
    if not isinstance(setup, dict):
        return []
    steps: list[str] = []
    for probe in setup.values():
        if not isinstance(probe, dict):
            continue
        replay = probe.get("registered_replay")
        if isinstance(replay, list):
            steps.extend(str(step) for step in replay)
    return steps


def classify_task(task: dict[str, Any], vocab: set[str]) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    spec_rel = task.get("oracle_spec")
    if not isinstance(spec_rel, str) or not spec_rel:
        raise DeriveError(f"{task_id}: missing oracle_spec")
    spec_path = ROOT / spec_rel
    if not spec_path.is_file():
        raise DeriveError(f"{task_id}: oracle_spec not found at {spec_rel}")

    spec = load_yaml(spec_path)
    if not isinstance(spec, dict):
        raise DeriveError(f"{spec_rel}: spec is not a mapping")

    raw_actions = spec.get("actions")
    if raw_actions is None:
        raw_actions = []
    if not isinstance(raw_actions, list):
        raise DeriveError(f"{spec_rel}: actions must be a list")

    action_types: list[str] = []
    for index, action in enumerate(raw_actions):
        if not isinstance(action, dict):
            raise DeriveError(f"{spec_rel}: actions[{index}] is not a mapping")
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type:
            raise DeriveError(f"{spec_rel}: actions[{index}].type is missing")
        action_types.append(action_type)

    unmapped = sorted({t for t in action_types if channel_for(t) is None})
    if unmapped:
        raise DeriveError(
            f"{spec_rel}: action types absent from the channel table: {unmapped}. "
            "Classify them in EXACT_CHANNELS/PREFIX_CHANNELS before reporting "
            "the column."
        )

    off_schema = sorted({t for t in action_types if t not in vocab})
    channels = [channel_for(t) for t in action_types]
    effective = [c for c in channels if c != CHANNEL_NEUTRAL]

    replay = registered_replay_steps(spec)
    if not action_types:
        execution_channel = CHANNEL_REGISTERED_REPLAY if replay else CHANNEL_EMPTY
    elif not effective:
        execution_channel = CHANNEL_NEUTRAL
    else:
        execution_channel = max(effective, key=CHANNEL_ORDER.index)

    return {
        "task_id": task_id,
        "benchmark": str(task.get("benchmark") or ""),
        "horizon_class": str(task.get("horizon_class") or ""),
        "oracle_spec": spec_rel,
        "oracle_spec_sha256": sha256_path(spec_path),
        "action_types": action_types,
        "action_channels": channels,
        "channels_present": sorted(set(effective), key=CHANNEL_ORDER.index),
        "execution_channel": execution_channel,
        "registered_replay_steps": replay,
        "off_schema_action_types": off_schema,
    }


def build_report(task_file: Path, schema_file: Path) -> dict[str, Any]:
    vocab = schema_vocabulary(schema_file)
    catalog = load_yaml(task_file)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tasks"), list):
        raise DeriveError(f"{task_file}: expected a mapping with a 'tasks' list")

    rows = [classify_task(task, vocab) for task in catalog["tasks"]]
    rows.sort(key=lambda row: row["task_id"])

    by_benchmark: dict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()
    for row in rows:
        by_benchmark[row["benchmark"]][row["execution_channel"]] += 1
        totals[row["execution_channel"]] += 1

    channel_table = {
        "exact": EXACT_CHANNELS,
        "prefix": [list(pair) for pair in PREFIX_CHANNELS],
        "order": list(CHANNEL_ORDER),
    }

    off_schema = sorted({t for row in rows for t in row["off_schema_action_types"]})

    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": (
            "Per-task execution-channel column for the C1 task universe. The "
            "observation channel is identical across channels and across arms; "
            "only the execution channel of the bounded target action varies."
        ),
        "scope": (
            "Describes how the registered transition spec reaches the software. "
            "The pixel-input arms ignore the registered actions and ground their "
            "own clicks, so their channel is a property of the arm and is absent "
            "here. All arms run all tasks, so these counts are task properties, "
            "not an allocation of tasks across arms."
        ),
        "task_file": str(task_file.relative_to(ROOT)),
        "task_file_sha256": sha256_path(task_file),
        "schema_source": str(schema_file.relative_to(ROOT)),
        "schema_source_sha256": sha256_path(schema_file),
        "channel_table": channel_table,
        "channel_table_sha256": sha256_obj(channel_table),
        "channel_definitions": CHANNEL_DEFINITIONS,
        "task_count": len(rows),
        "totals": dict(sorted(totals.items())),
        "by_benchmark": {
            benchmark: dict(sorted(counts.items()))
            for benchmark, counts in sorted(by_benchmark.items())
        },
        "off_schema_action_types": off_schema,
        "tasks": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    order = list(CHANNEL_ORDER) + [
        CHANNEL_NEUTRAL,
        CHANNEL_REGISTERED_REPLAY,
        CHANNEL_EMPTY,
    ]
    present = [key for key in order if key in report["totals"]]

    lines.append("Execution channel by benchmark (task counts)")
    lines.append("")
    lines.append("| Benchmark | " + " | ".join(present) + " | total |")
    lines.append("|---" * (len(present) + 2) + "|")
    for benchmark, counts in report["by_benchmark"].items():
        cells = [str(counts.get(key, 0)) for key in present]
        lines.append(
            f"| {benchmark} | " + " | ".join(cells) + f" | {sum(counts.values())} |"
        )
    total_cells = [str(report["totals"].get(key, 0)) for key in present]
    lines.append(
        "| **all** | " + " | ".join(total_cells) + f" | {report['task_count']} |"
    )
    lines.append("")
    lines.append("Per-task assignment")
    lines.append("")
    lines.append("| Task | Execution channel | Action types |")
    lines.append("|---|---|---|")
    for row in report["tasks"]:
        types = ", ".join(row["action_types"]) or "(none)"
        lines.append(f"| {row['task_id']} | {row['execution_channel']} | {types} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", type=Path, default=TASK_FILE)
    parser.add_argument("--schema-file", type=Path, default=SCHEMA_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print the paper-ready summary tables to stdout",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="derive and compare against the existing output without writing",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(args.task_file.resolve(), args.schema_file.resolve())
    except DeriveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    if args.check:
        if not args.output.is_file():
            print(f"error: {args.output} does not exist", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != payload:
            print(f"error: {args.output} is stale; rerun without --check", file=sys.stderr)
            return 1
        print(f"ok: {args.output} matches the derived report")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote {args.output.relative_to(ROOT)} ({report['task_count']} tasks)")

    if report["off_schema_action_types"]:
        print(
            "warning: action types present in specs but not in the schema "
            f"vocabulary: {report['off_schema_action_types']}",
            file=sys.stderr,
        )

    if args.markdown:
        print()
        print(render_markdown(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
