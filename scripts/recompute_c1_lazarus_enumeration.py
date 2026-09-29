#!/usr/bin/env python3
"""Print a read-only proposed C1 repository-enumeration v2 transition."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from warptest.c1_lazarus_selection.enumeration import (
    load_repository_enumeration,
    recompute_repository_enumeration_from_github_captures,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--pinned-ref", required=True)
    parser.add_argument("--expected-repository-name", required=True)
    arguments = parser.parse_args()

    baseline = load_repository_enumeration(
        arguments.artifact_root,
        repository_key=arguments.repository,
    )
    result = recompute_repository_enumeration_from_github_captures(
        baseline,
        capture_root=arguments.capture_root,
        mirror_path=arguments.mirror,
        pinned_ref=arguments.pinned_ref,
        expected_repository_name=arguments.expected_repository_name,
    )
    print(json.dumps(result.dry_run_value(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
