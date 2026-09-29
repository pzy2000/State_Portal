"""CLI for the opt-in Luanti #15519 persistent client pair boundary."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .luanti_15519_candidate_materialization import (
    materialize_luanti_15519_persistent_client_pair,
)
from .stable_protocol import AuthorizationRecord


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize Luanti 15519 fixed/buggy client trees. "
            "Build execution is opt-in."
        )
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument(
        "--execute-build",
        action="store_true",
        help="run the registered 600/1800-second configure/build steps",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope=(
            "preannotation-execution"
            if arguments.execute_build
            else "version-materialization"
        ),
        granted_at=now,
        local_user_id=os.getuid(),
    )
    try:
        result = materialize_luanti_15519_persistent_client_pair(
            arguments.artifact_root,
            authorization=authorization,
            confirmation=arguments.confirm,
            execute_build=arguments.execute_build,
        )
    except (PermissionError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(result.aggregate.revision_path)
    print(
        f"state={result.state} executed_steps={result.executed_steps} resumed_steps={result.resumed_steps}"
    )
    return 0


__all__ = ["main"]
