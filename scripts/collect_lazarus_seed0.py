#!/usr/bin/env python3
"""Create an auditable historical collection without contacting providers."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warptest.lazarus_collection import build_collection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", nargs="+", type=Path, required=True)
    parser.add_argument("--api-preflight", nargs="+", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_collection(ROOT, args.source, args.api_preflight)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in (("collection.json", result), ("source-audit.json", result["audit"]),
                          ("usage-summary.json", result["usage"])):
        (args.out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"complete": result["complete"], "cells": result["observed_cells"],
                      "failures": result["audit"]["failures"], "output": str(args.out_dir)}, ensure_ascii=False))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
