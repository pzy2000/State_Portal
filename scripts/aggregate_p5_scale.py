#!/usr/bin/env python3
"""Aggregate a P5 run into ``p5-scale-summary.json`` + Markdown.

Reads a ``p5-run-index.json`` (written by ``warptest p5-scale``) and the P5
manifest, folds the per-arm reports into the C1/C2/C3 summary with bootstrap CIs
and paired tests, and writes the evidence artifacts next to the run index.

Usage:
    python3 scripts/aggregate_p5_scale.py \
        --run-index reports/warptest/<ts>/p5/p5-run-index.json \
        [--manifest benchmarks/p5/manifest.yaml] [--out <dir>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warptest import p5_aggregate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-index", required=True, type=Path, help="Path to p5-run-index.json (or its directory).")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/p5/manifest.yaml"))
    parser.add_argument("--report-root", type=Path, default=Path.cwd(), help="Root for resolving relative report paths.")
    parser.add_argument("--out", type=Path, default=None, help="Output directory. Defaults to the run-index directory.")
    parser.add_argument("--rng-seed", type=int, default=None)
    parser.add_argument("--iters", type=int, default=None)
    parser.add_argument("--ci", type=float, default=None)
    args = parser.parse_args()

    index_path = args.run_index
    if index_path.is_dir():
        index_path = index_path / "p5-run-index.json"
    run_index = json.loads(index_path.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) if args.manifest.exists() else None
    stats_cfg = (manifest or {}).get("stats", {})

    summary = p5_aggregate.aggregate(
        run_index,
        manifest=manifest,
        report_root=args.report_root,
        rng_seed=args.rng_seed if args.rng_seed is not None else int(stats_cfg.get("rng_seed", 1234)),
        iters=args.iters if args.iters is not None else int(stats_cfg.get("bootstrap_iterations", 10000)),
        ci=args.ci if args.ci is not None else float(stats_cfg.get("ci", 0.95)),
    )

    out_dir = args.out or index_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "p5-scale-summary.json"
    md_path = out_dir / "p5-scale-summary.md"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(p5_aggregate.render_markdown(summary), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
