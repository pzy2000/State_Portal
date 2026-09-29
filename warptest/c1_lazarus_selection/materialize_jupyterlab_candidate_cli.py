"""CLI for runtime-independent JupyterLab fixed/buggy materialization."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .jupyterlab_3703 import describe_jupyterlab_3703_preparation
from .jupyterlab_candidate_materialization import (
    materialize_fixed_and_buggy,
    source_plan_from_3703,
    source_plan_from_regression,
    source_plan_from_top,
    write_active_downstream_plan,
)
from .jupyterlab_regression import plan_jupyterlab_source_materialization
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import AuthorizationRecord


_CANDIDATES = (444, 574, 611, 1250, 2437, 2752, 3310, 3662, 3703, 3867)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize one JupyterLab fixed/buggy source pair offline."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--candidate", type=int, choices=_CANDIDATES, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    candidate = CandidateKey("jupyterlab", arguments.candidate)
    if arguments.candidate == 3867:
        from .jupyterlab_top_profiles import prepare_top_candidate

        source_plan = source_plan_from_top(
            prepare_top_candidate(arguments.artifact_root, candidate),
            artifact_root=arguments.artifact_root,
        )
    elif arguments.candidate == 3703:
        source_plan = source_plan_from_3703(
            describe_jupyterlab_3703_preparation(
                arguments.artifact_root, candidate=candidate
            )
        )
    else:
        preparation = plan_jupyterlab_source_materialization(
            arguments.artifact_root, candidate=candidate
        )
        if isinstance(preparation, StageDecision):
            parser.error(
                f"materialization prerequisite unavailable: {preparation.details}"
            )
        source_plan = source_plan_from_regression(preparation)
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="version-materialization",
        granted_at=now,
        local_user_id=os.getuid(),
    )
    try:
        artifact = materialize_fixed_and_buggy(
            artifact_root=arguments.artifact_root,
            plan=source_plan,
            authorization=authorization,
            confirmation=arguments.confirm,
        )
        downstream = write_active_downstream_plan(
            artifact_root=arguments.artifact_root,
            plan=source_plan,
            authorization=AuthorizationRecord(
                authorization_id=arguments.authorization_id,
                scope="preannotation-planning",
                granted_at=now,
                local_user_id=os.getuid(),
            ),
        )
    except (PermissionError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(artifact.revision_path)
    print(downstream.revision_path)
    return 0


__all__ = ["main"]
