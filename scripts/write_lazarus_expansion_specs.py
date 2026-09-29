#!/usr/bin/env python3
"""Write the oracle spec and golden sidecar for C1-Lazarus expansion rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_expansion_specs import (  # noqa: E402
    EXPANSION_ROWS,
    EXPANSION_ROWS_BY_ID,
    build_goldens_sidecar,
    build_oracle_spec,
    load_goldens_metadata,
    write_yaml,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_ids", nargs="*", choices=sorted(EXPANSION_ROWS_BY_ID) or None)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    selected = (
        [EXPANSION_ROWS_BY_ID[value] for value in args.task_ids]
        if args.task_ids
        else list(EXPANSION_ROWS)
    )
    written: list[dict[str, str]] = []
    for row in selected:
        if not row.generate_spec:
            continue
        metadata = load_goldens_metadata(args.workspace, row)
        spec_path = write_yaml(
            args.workspace / row.spec_path, build_oracle_spec(row)
        )
        goldens_path = write_yaml(
            args.workspace / row.goldens_path,
            build_goldens_sidecar(row, metadata=metadata),
        )
        written.append(
            {
                "task_id": row.task_id,
                "oracle_spec": str(spec_path.relative_to(args.workspace)),
                "goldens": str(goldens_path.relative_to(args.workspace)),
            }
        )
    print(json.dumps(written, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
