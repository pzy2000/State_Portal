#!/usr/bin/env python3
"""Seed scoped materialized-pair receipts from a complete readiness artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_p2p_only_experiment import (  # noqa: E402
    adopt_materialized_pair_receipts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--readiness", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    readiness = json.loads(args.readiness.read_text(encoding="utf-8"))
    written = adopt_materialized_pair_receipts(workspace, readiness)
    print(json.dumps({"complete": True, "receipts_written": written}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
