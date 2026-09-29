#!/usr/bin/env python3
"""Resume one stable C1 source-to-patch repository batch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.patches import classify_unified_patch
from warptest.c1_lazarus_selection.providers.gitea import (
    GiteaRepository,
    UrllibGiteaTransport,
    capture_gitea_source,
)
from warptest.c1_lazarus_selection.providers.github import (
    UrllibGitHubTransport,
    capture_github_source,
    github_repository_for,
    validate_github_source_workers,
)
from warptest.c1_lazarus_selection.screening import (
    PatchDecision,
    SourceDecision,
    resolve_raw_patch_evidence,
    run_screening_batch,
)
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--repository",
        required=True,
        choices=("luanti", "blender", "jupyterlab", "openra", "libreoffice"),
    )
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--source-workers", type=int, default=8)
    parser.add_argument(
        "--max-candidates",
        type=_bounded_candidate_limit,
        default=None,
        metavar="N",
        help=(
            "schedule only the first N unresolved candidates in fixed "
            "enumeration order (1..10000)"
        ),
    )
    parser.add_argument(
        "--token-environment",
        default=None,
        help=(
            "environment variable containing the provider token "
            "(defaults to GITEA_TOKEN for Blender and GITHUB_TOKEN otherwise)"
        ),
    )
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        raise RuntimeError("screening execution requires the explicit --execute flag")
    if arguments.repository in {"luanti", "jupyterlab", "openra"}:
        validate_github_source_workers(arguments.source_workers)
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="candidate-screening",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    source_capture, patch_capture = _operations(
        arguments.artifact_root,
        repository_key=arguments.repository,
        authorization=authorization,
        token=os.environ.get(
            arguments.token_environment
            or ("GITEA_TOKEN" if arguments.repository == "blender" else "GITHUB_TOKEN")
        ),
    )
    result = run_screening_batch(
        arguments.artifact_root,
        repository_key=arguments.repository,
        source_capture=source_capture,
        patch_capture=patch_capture,
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=arguments.source_workers,
        patch_workers=4,
        persistence_workers=2,
        reload_workers=2,
        queue_limit=16,
        max_candidates=arguments.max_candidates,
    )
    print(
        json.dumps(
            {
                "repository_key": result.repository_key,
                "total_candidates": result.total_candidates,
                "scheduled_candidates": result.scheduled_candidates,
                "resumed_candidates": result.resumed_candidates,
                "source_rejected": result.source_rejected,
                "patch_rejected": result.patch_rejected,
                "patch_eligible": result.patch_eligible,
                "infrastructure_blocked": result.infrastructure_blocked,
                "remaining_candidates": result.remaining_candidates,
                "paused_for_disk": result.paused_for_disk,
                "max_in_flight": result.max_in_flight,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _operations(
    root: Path,
    *,
    repository_key: str,
    authorization: AuthorizationRecord,
    token: str | None,
):
    if repository_key == "blender":
        repository = GiteaRepository(
            repository_key="blender",
            api_base="https://projects.blender.org/api/v1",
            owner="blender",
            name="blender",
            attachment_hosts=("projects.blender.org",),
        )
        transport = UrllibGiteaTransport(token=token)

        def source(candidate: CandidateKey) -> SourceDecision:
            return capture_gitea_source(
                root,
                candidate=candidate,
                repository=repository,
                transport=transport,
                authorization=authorization,
            )

    elif repository_key in {"luanti", "jupyterlab", "openra"}:
        repository = github_repository_for(repository_key)
        transport = UrllibGitHubTransport(token=token)

        def source(candidate: CandidateKey) -> SourceDecision:
            return capture_github_source(
                root,
                candidate=candidate,
                repository=repository,
                transport=transport,
                authorization=authorization,
            )

    else:

        def source(candidate: CandidateKey) -> SourceDecision:
            raise RuntimeError(
                f"{candidate.repository_key} has no missing source checkpoint; "
                "a fresh provider capture must be configured explicitly"
            )

    def patch(candidate: CandidateKey, decision: SourceDecision) -> PatchDecision:
        if decision.raw_evidence_path is None:
            raise RuntimeError("accepted source decision lacks its raw manifest")
        raw_patch_evidence = resolve_raw_patch_evidence(
            root,
            candidate=candidate,
            source_manifest_path=decision.raw_evidence_path,
        )
        return classify_unified_patch(raw_patch_evidence.patch_path.read_bytes())

    return source, patch


def _bounded_candidate_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be an integer between 1 and 10000"
        ) from error
    if not 1 <= limit <= 10_000:
        raise argparse.ArgumentTypeError("must be between 1 and 10000")
    return limit


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
