#!/usr/bin/env python3
"""Swap a freshly captured Lazarus golden set in and re-freeze its gate evidence.

Some Phase-0 captures predate the public buggy start frame and their raw frames
are no longer on disk, so the only way to publish a fourth golden for them is to
capture the set again.  A recapture changes all four frames, which invalidates
every digest a gate report copied out of ``goldens.meta.json``.

This applier keeps the two artifacts consistent: it verifies the new set, swaps
it into the published directory, and rewrites the golden evidence of every gate
report that referenced the previous metadata digest.  Nothing else in a gate
report is touched, because nothing else describes the goldens.

Usage:
    python scripts/install_recaptured_lazarus_goldens.py \
        --task-id lz-luanti-14396-metadata-set-string-nil \
        --source .warptest-cache/lazarus/recapture-14396 \
        --published specs/luanti/lazarus/visual_lz-luanti-14396-metadata-set-string-nil
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warptest.lazarus_goldens import validate_lazarus_golden_set  # noqa: E402

GATE_ROOT = REPO_ROOT / "benchmarks" / "p5" / "lazarus"
EXPECTED_MEMBERS = (
    "before.golden.png",
    "buggy.before.golden.png",
    "fixed.after.golden.png",
    "buggy.after.golden.png",
    "goldens.meta.json",
)


class InstallError(RuntimeError):
    """Raised when a recaptured golden set cannot replace a published one."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify(directory: Path, task_id: str) -> None:
    missing = [name for name in EXPECTED_MEMBERS if not (directory / name).is_file()]
    if missing:
        raise InstallError(f"{directory}: capture is missing {', '.join(missing)}")
    verification = validate_lazarus_golden_set(
        directory / "goldens.meta.json", task_id=task_id
    )
    if not verification.complete:
        raise InstallError(
            f"{directory}: capture is not publishable: "
            + "; ".join(verification.failures)
        )


def _swap(source: Path, published: Path) -> None:
    staging = published.with_name(f".{published.name}-incoming")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    shutil.rmtree(published)
    staging.rename(published)


def _refreeze(old_digest: str, published: Path) -> list[Path]:
    """Rewrite the golden evidence of every gate report built on the old set."""

    metadata = json.loads((published / "goldens.meta.json").read_text(encoding="utf-8"))
    verification = validate_lazarus_golden_set(
        published / "goldens.meta.json", task_id=str(metadata["task_id"])
    )
    replacements: dict[str, Any] = {
        "metadata_sha256": verification.metadata_sha256,
        "before_sha256": metadata["before_sha256"],
        "fixed_after_sha256": metadata["fixed_after_sha256"],
        "buggy_after_sha256": metadata["buggy_after_sha256"],
        "before_equivalence": metadata["before_equivalence"],
        "after_differential": metadata["after_differential"],
        "complete": verification.complete,
        "failures": list(verification.failures),
        "before_equivalent": verification.before_equivalent,
        "fixed_before_visual": verification.fixed_before_visual,
        "buggy_before_visual": verification.buggy_before_visual,
        "fixed_after_visual": verification.fixed_after_visual,
        "buggy_after_visual": verification.buggy_after_visual,
    }

    updated: list[Path] = []
    for path in sorted(GATE_ROOT.rglob("gate.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        goldens = (report.get("evidence") or {}).get("goldens")
        if not isinstance(goldens, dict):
            continue
        if goldens.get("metadata_sha256") != old_digest:
            continue
        # Only keys the frozen report already carries are refreshed, so both the
        # live-gate and the verification-dump shapes stay intact.
        for key, value in replacements.items():
            if key in goldens:
                goldens[key] = value
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        updated.append(path)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--published", type=Path, required=True)
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    published = args.published.expanduser().resolve()
    if not published.is_dir():
        raise InstallError(f"{published}: published golden set does not exist")
    _verify(source, args.task_id)
    old_digest = _sha256(published / "goldens.meta.json")

    _swap(source, published)
    updated = _refreeze(old_digest, published)
    for path in updated:
        print(f"[recapture] refroze {path.relative_to(REPO_ROOT)}", flush=True)
    print(
        f"[recapture] installed {args.task_id} into "
        f"{published.relative_to(REPO_ROOT)} and refroze {len(updated)} gate report(s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
