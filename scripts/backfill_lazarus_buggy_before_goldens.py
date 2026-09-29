#!/usr/bin/env python3
"""Publish each Lazarus task's buggy start frame as the fourth public golden.

Every registered golden set already records ``before_buggy_sha256``: the capture
always produced a buggy start frame, but only three of the four frames were
copied into the task artifact.  This applier recovers that exact frame from the
local capture caches by digest and publishes it as ``buggy.before.golden.png``,
then registers the path under ``evaluation.lazarus.goldens.buggy_before``.

``goldens.meta.json`` is never rewritten.  ``warptest/lazarus_phase0.py`` compares
the frozen ``metadata_sha256`` in each gate report against a live recomputation,
so the recovered bytes are verified against the digest that is already signed
instead of re-signing the metadata around new bytes.

Every golden set a Lazarus catalog registers must end up with four frames.  A
Phase-0 set that no catalog lists may stay at three if its capture cache is gone;
those sets are reported rather than published.

Usage:
    python scripts/backfill_lazarus_buggy_before_goldens.py [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SPEC_ROOT = REPO_ROOT / "specs"
CATALOG_PATHS = (
    REPO_ROOT / "benchmarks" / "p5" / "c1_lazarus_tasks.yaml",
    REPO_ROOT / "benchmarks" / "p5" / "c1_lazarus_phase0_tasks.yaml",
)
PUBLISHED_NAME = "buggy.before.golden.png"
CACHE_ROOTS = (
    REPO_ROOT / ".warptest-cache" / "c1_lazarus_selection_v1" / "private",
    REPO_ROOT / ".warptest-cache" / "lazarus",
)


class BackfillError(RuntimeError):
    """Raised when a task's buggy start frame cannot be published safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lazarus_goldens(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    evaluation = document.get("evaluation")
    if not isinstance(evaluation, dict):
        return None
    lazarus = evaluation.get("lazarus")
    if not isinstance(lazarus, dict):
        return None
    goldens = lazarus.get("goldens")
    return goldens if isinstance(goldens, dict) else None


def _registered_specs() -> dict[Path, list[Path]]:
    """Group every Lazarus spec file by the golden directory it publishes."""

    grouped: dict[Path, list[Path]] = {}
    for path in sorted(SPEC_ROOT.rglob("*.yaml")):
        goldens = _lazarus_goldens(yaml.safe_load(path.read_text(encoding="utf-8")))
        if goldens is None:
            continue
        before = (path.parent / str(goldens.get("before") or "")).resolve()
        if not before.is_file():
            raise BackfillError(f"{path}: published before golden is missing")
        grouped.setdefault(before.parent, []).append(path)
    return grouped


def register_golden_path(text: str, relative: str) -> str:
    """Insert ``buggy_before`` beside ``before`` without reflowing the document.

    A full YAML re-dump would reformat hand-authored specs and drop their
    comments, so the sibling key is spliced in textually and the result is
    verified by reloading the document.
    """

    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() != "goldens:":
            continue
        goldens_indent = len(line) - len(line.lstrip())
        for offset in range(index + 1, len(lines)):
            candidate = lines[offset]
            if not candidate.strip():
                continue
            indent = len(candidate) - len(candidate.lstrip())
            if indent <= goldens_indent:
                break
            if candidate.strip().startswith("before:"):
                inserted = f"{' ' * indent}buggy_before: {relative}\n"
                return "".join(lines[: offset + 1] + [inserted] + lines[offset + 1 :])
    raise BackfillError("document has no evaluation.lazarus.goldens.before entry")


def _planned_document(path: Path, relative: str) -> str | None:
    original = path.read_text(encoding="utf-8")
    before = yaml.safe_load(original)
    if before["evaluation"]["lazarus"]["goldens"].get("buggy_before") == relative:
        return None
    updated = register_golden_path(original, relative)
    after = yaml.safe_load(updated)
    if after["evaluation"]["lazarus"]["goldens"].pop("buggy_before") != relative:
        raise BackfillError(f"{path}: did not register the new golden")
    if after != before:
        raise BackfillError(f"{path}: changed beyond the new golden")
    return updated


def _catalog_visual_dirs() -> set[Path]:
    """Return the golden directory of every task a Lazarus catalog registers."""

    registered: set[Path] = set()
    for catalog_path in CATALOG_PATHS:
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        for row in catalog.get("tasks") or []:
            spec_path = REPO_ROOT / str(row["oracle_spec"])
            goldens = _lazarus_goldens(
                yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            )
            if goldens is None:
                raise BackfillError(f"{spec_path}: catalog spec has no Lazarus goldens")
            registered.add((spec_path.parent / str(goldens["before"])).resolve().parent)
    return registered


def _collect() -> list[dict[str, Any]]:
    required = _catalog_visual_dirs()
    plans: list[dict[str, Any]] = []
    for visual, specs in _registered_specs().items():
        metadata = json.loads((visual / "goldens.meta.json").read_text(encoding="utf-8"))
        digest = str(metadata.get("before_buggy_sha256") or "")
        if len(digest) != 64:
            raise BackfillError(f"{visual}: golden metadata has no before_buggy_sha256")
        target = visual / PUBLISHED_NAME
        plans.append(
            {
                "name": visual.name,
                "digest": digest,
                "target": target,
                "specs": specs,
                "required": visual in required,
                "published": target.is_file() and _sha256(target) == digest,
            }
        )
    return plans


def _planned_documents(plan: dict[str, Any]) -> dict[Path, str]:
    """Plan the spec edits for a set that already publishes its fourth frame."""

    if not plan["published"]:
        return {}
    documents: dict[Path, str] = {}
    for path in plan["specs"]:
        relative = os.path.relpath(plan["target"], path.parent)
        planned = _planned_document(path, relative)
        if planned is not None:
            documents[path] = planned
    return documents


def _recover_frames(wanted: set[str]) -> dict[str, Path]:
    """Find each missing frame in the capture caches by content digest."""

    found: dict[str, Path] = {}
    for root in CACHE_ROOTS:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*.png"):
            if "node_modules" in candidate.parts:
                continue
            try:
                digest = _sha256(candidate)
            except OSError:
                continue
            if digest in wanted and digest not in found:
                found[digest] = candidate
                if len(found) == len(wanted):
                    return found
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift only")
    args = parser.parse_args(argv)

    plans = _collect()

    if args.check:
        drifted = [
            plan
            for plan in plans
            if (plan["required"] and not plan["published"]) or _planned_documents(plan)
        ]
        for plan in drifted:
            print(f"[buggy-before] drifted: {plan['name']}", flush=True)
        if drifted:
            print(
                "[buggy-before] run scripts/backfill_lazarus_buggy_before_goldens.py",
                flush=True,
            )
            return 1
        print(f"[buggy-before] {len(plans)} golden sets are registered")
        return 0

    missing = {
        plan["digest"]: plan["target"] for plan in plans if not plan["published"]
    }
    if missing:
        print(f"[buggy-before] recovering {len(missing)} frame(s) by digest", flush=True)
        recovered = _recover_frames(set(missing))
        for digest, target in missing.items():
            source = recovered.get(digest)
            if source is None:
                continue
            shutil.copyfile(source, target)
            if _sha256(target) != digest:
                raise BackfillError(f"published {target.name} does not match its digest")
            print(f"[buggy-before] published {target.relative_to(REPO_ROOT)}", flush=True)
        for plan in plans:
            plan["published"] = (
                plan["target"].is_file() and _sha256(plan["target"]) == plan["digest"]
            )

    stranded = sorted(
        plan["name"] for plan in plans if plan["required"] and not plan["published"]
    )
    if stranded:
        raise BackfillError(
            "no cached frame matches the digest of these registered tasks, "
            f"recapture them first: {', '.join(stranded)}"
        )

    for plan in plans:
        for path, content in _planned_documents(plan).items():
            path.write_text(content, encoding="utf-8")
            print(f"[buggy-before] registered {path.relative_to(REPO_ROOT)}", flush=True)
    skipped = sorted(plan["name"] for plan in plans if not plan["published"])
    if skipped:
        print(
            f"[buggy-before] still three frames, no cached capture: {', '.join(skipped)}",
            flush=True,
        )
    print(
        f"[buggy-before] {sum(1 for plan in plans if plan['published'])}"
        f"/{len(plans)} golden sets publish four frames",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
