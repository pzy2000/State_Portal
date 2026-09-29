"""Typed Gate-2 terminal for multi-defect JupyterLab PR #16943."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 16943}
_FIX_REVISION = "141904e62406c407434f5fd56c95d657e366eb3f"
_BASE_REVISION = "55213d8bca9d133f89e4b6fa989f372b7c3da9a9"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/jupyterlab/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/jupyterlab/16943/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/jupyterlab/16943/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/jupyterlab/16943/preannotation/v2", "source_atomicity", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_sprint_terminal/jupyterlab/16943/multiple-authoritative-issues/v2",
    "candidate_sprint_terminal",
    "2.0",
)
_UPSTREAM = Path(
    "benchmarks/p5/lazarus/defects/lz-jupyterlab-16943-markdown-regex-replace-all/upstream.json"
)
_REVERT = Path(
    "benchmarks/p5/lazarus/defects/lz-jupyterlab-16943-markdown-regex-replace-all/revert.patch"
)
_ISSUE_11124 = Path(
    ".warptest-cache/c1_lazarus_selection_v1/raw/github/jupyterlab/16943/onboard-v2/issue-11124.normalized.json"
)
_RAW_ROOT = Path("raw/github/jupyterlab/16943/onboard-v2")


def write_jupyterlab_16943_gate2_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    workspace = root.parents[1]
    enumeration = _load(root, _ENUMERATION)
    if enumeration.payload.get("repository_key") != "jupyterlab":
        raise RuntimeError("JupyterLab enumeration drifted")
    upstream_source = _regular_file(workspace / _UPSTREAM, workspace)
    revert_source = _regular_file(workspace / _REVERT, workspace)
    issue11124_source = _regular_file(workspace / _ISSUE_11124, workspace)
    raw_root = root / _RAW_ROOT
    raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    captures = {
        "upstream.json": upstream_source,
        "revert.patch": revert_source,
        "issue-11124.normalized.json": issue11124_source,
    }
    for name, source in captures.items():
        target = raw_root / name
        if target.exists():
            if target.is_symlink() or target.read_bytes() != source.read_bytes():
                raise RuntimeError(f"#16943 raw capture drifted: {name}")
        else:
            shutil.copy2(source, target)
    upstream = json.loads((raw_root / "upstream.json").read_text(encoding="utf-8"))
    issue11124 = json.loads(
        (raw_root / "issue-11124.normalized.json").read_text(encoding="utf-8")
    )
    issue16925 = upstream.get("issue", {})
    pull = upstream.get("pull_request", {})
    comments = upstream.get("comments", [])
    if not (
        upstream.get("repository") == "jupyterlab/jupyterlab"
        and upstream.get("fix_commit") == _FIX_REVISION
        and upstream.get("pinned_revision")
        == "4a19f8fff4049a9536e21fa2980fe7e5ee592d01"
        and issue11124.get("number") == 11124
        and "bug" in issue11124.get("labels", [])
        and issue16925.get("number") == 16925
        and pull.get("number") == 16943
        and "Fixes https://github.com/jupyterlab/jupyterlab/issues/11124"
        in pull.get("body_raw", "")
        and "issues/16925" in pull.get("body_raw", "")
        and any("this PR does two things" in item.get("body_raw", "") for item in comments)
    ):
        raise RuntimeError("#16943 authoritative multi-defect evidence drifted")
    repo = workspace / "games/jupyterlab"
    base = subprocess.run(
        ("git", "rev-parse", f"{_FIX_REVISION}^"),
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
    )
    names = subprocess.run(
        ("git", "diff", "--name-only", _BASE_REVISION, _FIX_REVISION),
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
    )
    changed = [line for line in names.stdout.splitlines() if line]
    expected = [
        "packages/cells/src/searchprovider.ts",
        "packages/cells/src/widget.ts",
        "packages/notebook/test/searchprovider.spec.ts",
    ]
    if base.returncode != 0 or base.stdout.strip() != _BASE_REVISION or names.returncode != 0 or changed != expected:
        raise RuntimeError("#16943 merge commit inventory drifted")
    source_payload = {
        "candidate": _CANDIDATE,
        "complete": True,
        "repository": "jupyterlab/jupyterlab",
        "pull_number": 16943,
        "pull_title": pull["title"],
        "fix_revision": _FIX_REVISION,
        "base_revision": _BASE_REVISION,
        "raw_evidence_path": (_RAW_ROOT / "upstream.json").as_posix(),
        "raw_patch_path": (_RAW_ROOT / "revert.patch").as_posix(),
        "authoritative_issue_paths": [
            (_RAW_ROOT / "issue-11124.normalized.json").as_posix(),
            (_RAW_ROOT / "upstream.json").as_posix(),
        ],
        "authoritative_issue_numbers": [11124, 16925],
        "changed_files": changed,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    source = _write_once(
        root, _SOURCE, (enumeration.reference,), source_payload, authorization
    )
    patch_bytes = (raw_root / "revert.patch").read_bytes()
    patch_payload = {
        "candidate": _CANDIDATE,
        "complete": False,
        "outcome": "patch_scope_ambiguous_multi_defect",
        "patch_fingerprint": hashlib.sha256(patch_bytes).hexdigest(),
        "production_paths": [
            "packages/cells/src/searchprovider.ts",
            "packages/cells/src/widget.ts",
        ],
        "test_paths": ["packages/notebook/test/searchprovider.spec.ts"],
        "primary_benchmark_intent": "markdown regex replace-all",
        "independent_production_fix_also_present": "read-only markdown paste",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    patch = _write_once(
        root, _PATCH, (enumeration.reference, source.reference), patch_payload, authorization
    )
    atomicity_payload = {
        "candidate": _CANDIDATE,
        "complete": True,
        "outcome": "rejected",
        "reason": "multiple_authoritative_issues",
        "primary_issue_number": None,
        "authoritative_issue_numbers": [11124, 16925],
        "authoritative_issue_count": 2,
        "issue_classification": {
            "11124": "bug_read_only_markdown_paste",
            "16925": "bug_markdown_regex_replace_all_groups",
        },
        "production_hunk_mapping": {
            "packages/cells/src/widget.ts": 11124,
            "packages/cells/src/searchprovider.ts": 16925,
        },
        "review_evidence": "reviewer explicitly stated this PR does two things",
        "incidental_cross_reference": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    atomicity = _write_once(
        root, _ATOMICITY, (source.reference, patch.reference), atomicity_payload, authorization
    )
    terminal_payload = {
        "candidate": _CANDIDATE,
        "state": "terminal",
        "outcome": "rejected",
        "terminal": True,
        "resumable": False,
        "reason": "multiple_authoritative_issues_and_independent_production_fixes",
        "gate_status": {
            "gate_1_source_authority": "passed_complete_pr_and_two_issue_closure",
            "gate_2_single_defect_mapping": "failed_multiple_authoritative_issues",
            "gate_3_production_only_inverse": "not_executed_after_gate2_failure",
            "gate_4_fixed_test_api_compatibility": "not_executed_after_gate2_failure",
            "gate_5_7": "not_executed",
            "gate_8_13": "not_applicable",
        },
        "tests_or_builds_started": False,
        "node_runtime_started": False,
        "roster_modified": False,
        "replacement_proposal": None,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "select_a_different_modern_single_defect_jupyterlab_candidate",
    }
    terminal = _write_once(
        root,
        _TERMINAL,
        (source.reference, patch.reference, atomicity.reference),
        terminal_payload,
        authorization,
    )
    return source, patch, atomicity, terminal


def _regular_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#16943 source evidence file is unsafe")
    return resolved


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _write_once(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#16943 artifact conflicts: {ref.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, ref)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#16943 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#16943 Gate2 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_jupyterlab_16943_gate2_terminal"]
