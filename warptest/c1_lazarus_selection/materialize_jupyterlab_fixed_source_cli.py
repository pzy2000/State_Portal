"""CLI for offline JupyterLab fixed source materialization."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .jupyterlab_fixed_source_capsule import materialize_jupyterlab_fixed_source
from .models import CandidateKey
from .stable_protocol import AuthorizationRecord


def _candidate(value: str) -> CandidateKey:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("candidate must be a change number") from error
    if number not in {444, 574, 611, 1250, 2437, 2752, 3310, 3662}:
        raise argparse.ArgumentTypeError("candidate is not in the closed allowlist")
    return CandidateKey("jupyterlab", number)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize one captured JupyterLab fixed tree offline."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--candidate", type=_candidate, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="protocol-migration",
        granted_at=now,
        local_user_id=os.getuid(),
    )
    try:
        artifact = materialize_jupyterlab_fixed_source(
            artifact_root=arguments.artifact_root,
            candidate=arguments.candidate,
            authorization=authorization,
            confirmation=arguments.confirm,
        )
    except (PermissionError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(artifact.revision_path)
    return 0


__all__ = ["main"]
