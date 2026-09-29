"""CLI for the explicitly authorized Luanti 14757 bounded launch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

from .luanti_14757_bounded_launch import (
    EXECUTION_CONFIRMATION,
    execute_luanti_14757_bounded_launch,
)
from .stable_protocol import AuthorizationRecord


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fixed/buggy Luanti 14757 downstream smoke once each."
    )
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--watchdog-seconds", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    authorization = AuthorizationRecord(
        authorization_id=args.authorization_id,
        scope="preannotation-execution",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    result = execute_luanti_14757_bounded_launch(
        args.artifact_root,
        run_root=args.run_root,
        authorization=authorization,
        confirmation=args.confirmation,
        watchdog_seconds=args.watchdog_seconds,
    )
    print(
        json.dumps(
            {
                "confirmation": EXECUTION_CONFIRMATION,
                "attempt_artifact_ids": [
                    item.reference.artifact_id for item in result.attempts
                ],
                "aggregate_artifact_id": result.aggregate.reference.artifact_id,
                "semantic_pair_observed": result.aggregate.payload[
                    "semantic_pair_observed"
                ],
                "state": result.aggregate.payload["state"],
            },
            sort_keys=True,
        )
    )
    return 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
