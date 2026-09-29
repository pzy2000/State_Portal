#!/usr/bin/env python3
"""Recapture and cache SpaceRPG fuzz seed saves for every spec under ``specs/spacerpg``.

Opens ONE warm StateSession for the SpaceRPG repo and, for each spec, drives the
game's own ``saveGame('auto')`` to capture a real curated ``SaveGameData`` (the
source of truth) plus a canonical action sequence, writing both to
``.warptest-cache/spacerpg-fuzz-seeds/``. ``fuzz-bench`` reuses this cache for speed
and reproducibility (or re-captures on ``--refresh-seeds``).

Usage:
    python scripts/refresh_spacerpg_fuzz_seeds.py [--headed] [--only spec-stem ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.fuzzing import (  # noqa: E402
    _canonical_spacerpg_actions,
    _spacerpg_seed_cache_path,
    capture_spacerpg_seed,
)
from warptest.state_store import StateSession  # noqa: E402

SPEC_DIR = ROOT / "specs" / "spacerpg"
REPO = ROOT / "games" / "SpaceRPG"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless).")
    parser.add_argument("--only", nargs="+", default=None, help="Only refresh these spec stems (e.g. playable-flight).")
    parser.add_argument("--report-dir", default=None, help="Session working dir (default: a temp under .warptest-cache).")
    args = parser.parse_args()

    spec_paths = sorted(SPEC_DIR.glob("*.yaml"))
    if args.only:
        wanted = {stem.strip() for stem in args.only}
        spec_paths = [p for p in spec_paths if p.stem in wanted]
    if not spec_paths:
        raise SystemExit("No matching SpaceRPG specs found.")

    report_dir = Path(args.report_dir) if args.report_dir else (ROOT / ".warptest-cache" / "spacerpg-seed-refresh")
    report_dir.mkdir(parents=True, exist_ok=True)

    session = StateSession(REPO, report_dir / "session", headless=not args.headed).start()
    ok, failed = 0, 0
    try:
        for spec_path in spec_paths:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict) or spec.get("benchmark") != "SpaceRPG":
                print(f"  skip (not a SpaceRPG spec): {spec_path.name}")
                continue
            seed_oracle_path = report_dir / f"{spec_path.stem}-oracle.yaml"
            seed_oracle_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
            oracle_rel = str(spec_path.relative_to(ROOT))
            try:
                seed = capture_spacerpg_seed(session, spec, seed_oracle_path)
            except Exception as error:
                failed += 1
                print(f"  FAIL {spec_path.name}: {error}")
                continue
            seed.setdefault("seed_actions", _canonical_spacerpg_actions(spec))
            cache_path = _spacerpg_seed_cache_path(ROOT, session.git_commit, oracle_rel)
            payload = {
                "git_commit": session.git_commit,
                "oracle_spec": oracle_rel,
                "seed_save": seed["seed_save"],
                "seed_actions": seed["seed_actions"],
            }
            cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            ok += 1
            print(f"  OK  {spec_path.name} -> {cache_path.name} (fields={list(seed['seed_save'])})")
    finally:
        session.close()

    print(f"Done: {ok} captured, {failed} failed, out of {len(spec_paths)} specs.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
