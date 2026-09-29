#!/usr/bin/env python3
"""Verify Luanti replay specs with a five-state verdict (SWE-bench-style regression).

For each replay spec (``specs/luanti/replay/*.yaml``) this:
  1. materializes the triggering deep-state as an isolated fixture world,
  2. obtains a PRE-fix and a POST-fix ``luantiserver`` binary,
  3. runs the fixture on each (fresh world per side; no source mutation),
  4. applies the spec's EXPLICIT defect oracle (Contract 3) to each observation,
  5. emits one of the five verdicts (Contract 3), never two booleans.

Binary resolution:
  * ``--pre-bin`` / ``--post-bin`` override everything (fast, no compile).
  * ``--build`` + ``source.pre_fix_sha`` / ``source.post_fix_sha`` -> the isolated
    builder compiles both commits (bare cache + per-SHA worktree + external build,
    Contract 4). Missing SHAs under ``--build`` yield BUILD_FAILED for that spec.
  * otherwise a single fallback binary (``--bin`` or games/luanti/bin/luantiserver)
    is used for BOTH sides -- a CONTROL run that validates the fixture + oracle +
    classifier plumbing and reports the current engine's honest verdict.

Usage:
    # control run (no compile) against the prebuilt server:
    python scripts/verify_luanti_replay.py
    # true two-commit regression once specs carry real pre/post SHAs:
    python scripts/verify_luanti_replay.py --build
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.luanti_replay import (  # noqa: E402
    LuantiUpstreamBuilder,
    run_replay,
    write_fixture,
)
from warptest.upstream_contracts import (  # noqa: E402
    DefectOracle,
    ReplayObservation,
    classify_replay,
)
from warptest.upstream_sources import get_source  # noqa: E402

_DEFAULT_BIN = ROOT / "games" / "luanti" / "bin" / "luantiserver"


def _load_specs(
    specs_dir: Path, single: str | None
) -> list[tuple[Path, dict[str, Any]]]:
    if single:
        paths = [Path(single) if Path(single).is_absolute() else ROOT / single]
    else:
        paths = sorted(specs_dir.glob("*.yaml"))
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (
            isinstance(spec, dict)
            and spec.get("benchmark") == "luanti"
            and "fixture" in spec
        ):
            out.append((path, spec))
    return out


def _resolve_binaries(
    spec: dict[str, Any],
    *,
    builder: LuantiUpstreamBuilder | None,
    pre_bin: Path | None,
    post_bin: Path | None,
    fallback: Path,
) -> tuple[Path | None, Path | None, str, str]:
    """Return ``(pre_path, post_path, pre_log, post_log)``; None => build failed."""

    src = spec.get("source") or {}
    if pre_bin and post_bin:
        return pre_bin, post_bin, "explicit --pre-bin", "explicit --post-bin"
    if builder is not None:
        pre_sha, post_sha = src.get("pre_fix_sha"), src.get("post_fix_sha")
        if not pre_sha or not post_sha:
            return None, None, "missing pre_fix_sha", "missing post_fix_sha"
        builder.ensure_mirror()
        pre_path, pre_log = builder.build_server(pre_sha)
        post_path, post_log = builder.build_server(post_sha)
        return pre_path, post_path, pre_log, post_log
    # Control mode: same binary both sides.
    return fallback, fallback, "control (fallback bin)", "control (fallback bin)"


def _run_side(
    binary: Path | None,
    target: dict[str, Any],
    oracle: DefectOracle,
    timeout_s: float,
    work: Path,
) -> ReplayObservation:
    if binary is None or not Path(binary).exists():
        return ReplayObservation(build_ok=False, harness_ok=False)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    write_fixture(work, target)
    return run_replay(Path(binary), work, oracle=oracle, timeout_s=timeout_s)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--specs-dir", default="specs/luanti/replay")
    ap.add_argument("--spec", default=None, help="Verify a single spec path.")
    ap.add_argument("--report-dir", default="reports/warptest/luanti-replay")
    ap.add_argument(
        "--build",
        action="store_true",
        help="Compile pre/post commits via the isolated builder.",
    )
    ap.add_argument(
        "--pre-bin", default=None, help="Explicit pre-fix luantiserver (skips build)."
    )
    ap.add_argument(
        "--post-bin", default=None, help="Explicit post-fix luantiserver (skips build)."
    )
    ap.add_argument(
        "--bin",
        default=None,
        help="Fallback single binary for control runs (default: games/luanti/bin/luantiserver).",
    )
    args = ap.parse_args()

    specs = _load_specs((ROOT / args.specs_dir).resolve(), args.spec)
    if not specs:
        print("No replay specs found.")
        return 0

    builder = None
    if args.build:
        src = get_source("luanti", ROOT)
        builder = LuantiUpstreamBuilder(src["upstream_url"])
    pre_bin = Path(args.pre_bin).resolve() if args.pre_bin else None
    post_bin = Path(args.post_bin).resolve() if args.post_bin else None
    fallback = Path(args.bin).resolve() if args.bin else _DEFAULT_BIN

    report_dir = (ROOT / args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    work_root = report_dir / "work"

    results: list[dict[str, Any]] = []
    for path, spec in specs:
        name = path.stem
        print(f"[replay] {name}: resolving binaries...")
        oracle = DefectOracle.from_dict(spec.get("defect_oracle"))
        target = (spec.get("fixture") or {}).get("target") or {}
        timeout_s = float(spec.get("timeout_s", 45))
        pre_path, post_path, pre_log, post_log = _resolve_binaries(
            spec, builder=builder, pre_bin=pre_bin, post_bin=post_bin, fallback=fallback
        )
        print(f"[replay] {name}: pre={pre_log} post={post_log}; running pre...")
        pre_obs = _run_side(
            pre_path, target, oracle, timeout_s, work_root / name / "pre"
        )
        print(f"[replay] {name}: running post...")
        post_obs = _run_side(
            post_path, target, oracle, timeout_s, work_root / name / "post"
        )
        verdict = classify_replay(pre_obs, post_obs, oracle)
        verdict["spec"] = str(path.relative_to(ROOT))
        verdict["name"] = name
        verdict["source"] = spec.get("source")
        verdict["defect_oracle"] = oracle.to_dict()
        results.append(verdict)
        print(f"[replay] {name}: VERDICT = {verdict['verdict']}")

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "build"
        if args.build
        else ("explicit-bins" if (pre_bin and post_bin) else "control"),
        "count": len(results),
        "verdict_counts": {
            v: sum(1 for r in results if r["verdict"] == v)
            for v in {r["verdict"] for r in results}
        },
        "results": results,
    }
    out = report_dir / "verify.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {out.relative_to(ROOT)}")
    print(json.dumps(payload["verdict_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
