"""CLI for offline JupyterLab historical runtime audit and packaging."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .jupyterlab_historical_runtime import (
    audit_local_host,
    audit_runtime_source,
    package_historical_runtime,
    write_runtime_preparation,
)
from .jupyterlab_regression import profile_for_candidate
from .models import CandidateKey
from .stable_protocol import AuthorizationRecord


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or package one local historical JupyterLab runtime."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        type=int,
        choices=(444, 574, 611, 1250, 2437, 2752, 3310, 3662),
        required=True,
    )
    parser.add_argument("--source-tree", type=Path)
    parser.add_argument("--package", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--authorization-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.package and (
        arguments.source_tree is None or arguments.confirm is None
    ):
        parser.error("--package requires --source-tree and --confirm")
    profile = profile_for_candidate(CandidateKey("jupyterlab", arguments.candidate))
    audit = (
        audit_local_host(profile)
        if arguments.source_tree is None
        else audit_runtime_source(profile, arguments.source_tree)
    )
    if arguments.package:
        if not audit.ready:
            parser.error("historical JupyterLab runtime source is not exact")
        package_historical_runtime(
            artifact_root=arguments.artifact_root,
            profile=profile,
            source_tree=arguments.source_tree,
            confirmation=arguments.confirm,
        )
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="infrastructure-audit",
        granted_at=now,
        local_user_id=os.getuid(),
    )
    artifact = write_runtime_preparation(
        artifact_root=arguments.artifact_root,
        profile=profile,
        audit=audit,
        authorization=authorization,
        created_at=now,
    )
    print(artifact.revision_path)
    return 0


__all__ = ["main"]
