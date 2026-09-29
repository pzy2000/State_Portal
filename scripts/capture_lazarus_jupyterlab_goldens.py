#!/usr/bin/env python3
"""Capture and publish one formal JupyterLab task's Lazarus golden triplet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_jupyterlab_app import (  # noqa: E402
    JUPYTERLAB_APP_PROFILES,
    capture_jupyterlab_lazarus_goldens,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", choices=sorted(JUPYTERLAB_APP_PROFILES))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--prebuilt-root", type=Path, default=ROOT / ".warptest-cache/lazarus/prebuilt"
    )
    parser.add_argument(
        "--sandbox-root",
        type=Path,
        default=ROOT / ".warptest-cache/lazarus/jupyterlab-goldens",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--build-timeout-s", type=float, default=3_600.0)
    args = parser.parse_args(argv)

    published = capture_jupyterlab_lazarus_goldens(
        args.task_id,
        repository_root=args.workspace,
        prebuilt_root=args.prebuilt_root,
        sandbox_root=args.sandbox_root,
        output_dir=args.output_dir,
        build_timeout_s=args.build_timeout_s,
    )
    print(
        json.dumps(
            {
                "task_id": published.task_id,
                "before": str(published.before),
                "buggy_before": str(published.buggy_before),
                "fixed_after": str(published.fixed_after),
                "buggy_after": str(published.buggy_after),
                "metadata": str(published.metadata),
                "before_metrics": published.before_metrics,
                "after_metrics": published.after_metrics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
