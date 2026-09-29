"""Public CLI for the closed Luanti fixed source capsule v2 migration."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .luanti_fixed_source_capsule import migrate_fixed_source_capsule
from .models import CandidateKey
from .stable_protocol import AuthorizationRecord


def _candidate(value: str) -> CandidateKey:
    allowed = {
        "luanti/13132": CandidateKey("luanti", 13132),
        "luanti/14757": CandidateKey("luanti", 14757),
    }
    try:
        return allowed[value]
    except KeyError as error:
        raise argparse.ArgumentTypeError(
            "candidate is not in the closed allowlist"
        ) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate one registered Luanti fixed source capsule v2 offline."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--candidate", type=_candidate, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="protocol-migration",
        granted_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )
    try:
        capsule = migrate_fixed_source_capsule(
            artifact_root=arguments.artifact_root,
            candidate=arguments.candidate,
            authorization=authorization,
            confirmation=arguments.confirm,
        )
    except (RuntimeError, ValueError, PermissionError) as error:
        parser.error(str(error))
    print(capsule.revision_path)
    return 0


__all__ = ["main"]
