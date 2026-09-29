"""Nonterminal reclassification receipts for JupyterLab candidates.

The current-v2 chain for changes 1846 and 4796 proves a real issue, a merged
pull request, a single authoritative issue, a production-only patch scope,
and an executable test manifest.  Their old preparation state is pending
because screenshots referenced by the historical issue/PR have not been
captured.  That missing attachment closure is recorded as resumable work; it
does not eliminate a source/patch-qualified candidate from the candidate
pool.

Change 2653 is present in the current enumeration but has no typed current-v2
source packet.  It receives a separate nonterminal missing-chain receipt and
is not reclassified on untyped or historical evidence.

This module is read-only with respect to candidate code: it never fetches,
builds, executes, installs dependencies, or starts formal trials.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ENUMERATION = ArtifactReference(
    "repository_enumeration/jupyterlab/current/v2",
    "repository_enumeration",
    "2.0",
)
_CANDIDATES = {
    1846: {
        "issue": 1834,
        "profile_id": "jupyterlab-1846-gfm-latex-delimiter-v2",
        "production_paths": ["src/renderers/latex.ts"],
        "test_paths": ["test/src/renderers/latex.spec.ts"],
        "selected_test_path": "test/src/renderers/latex.spec.ts",
        "selected_test_marker": r"should handle `\\\(` delimiters for math",
        "f2p_selector": "jupyter-ui removeMath() should handle `\\(` delimiters in GFM",
        "p2p_selector": "jupyter-ui removeMath() should handle code spans",
        "bug_title": "\\( in code block breaks markdown syntax highlighting",
        "bug_markers": ["syntax highlighting", "triple backtick", "\\("],
    },
    4796: {
        "issue": 4795,
        "profile_id": "jupyterlab-4796-single-line-csv-v1",
        "production_paths": ["packages/csvviewer/src/model.ts"],
        "test_paths": [
            "tests/test-csvviewer/src/model.spec.ts",
            "tests/test-csvviewer/src/parse-noquotes.spec.ts",
            "tests/test-csvviewer/src/parse.spec.ts",
        ],
        "selected_test_path": "tests/test-csvviewer/src/model.spec.ts",
        "selected_test_marker": "handles having only a header",
        "f2p_selector": "csvviewer/model DSVModel handles having only a header",
        "p2p_selector": "csvviewer/model DSVModel handles not having a header",
        "bug_title": "Table View renders CSV file incorrectly",
        "bug_markers": ["render the csv", "DictWriter", "bad_csv.png"],
    },
}


def write_jupyterlab_candidate_reclassification(
    artifact_root: str | Path,
    change_number: int,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a candidate-pool reclassification or missing-chain receipt.

    ``change_number`` 1846 and 4796 are reclassified only after reloading the
    complete current-v2 chain and its raw authority/patch evidence.  2653 is
    intentionally handled as a blocker because current-v2 source evidence is
    not registered; no old terminal result is imported.
    """

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    if change_number == 2653:
        return _write_missing_chain_receipt(root, authorization=authorization)
    spec = _CANDIDATES.get(change_number)
    if spec is None:
        raise ValueError("only JupyterLab 1846, 4796, or 2653 are supported")

    refs = _refs(change_number)
    chain = tuple(_load(root, reference) for reference in refs)
    _validate_chain(chain, spec)
    (
        _enumeration,
        source,
        patch,
        _screening,
        atomicity,
        manifest,
        _checkpoint,
        capsule,
        profile,
        preparation,
    ) = chain

    source_manifest_path = _relative_path(
        source.payload.get("raw_evidence_path"), "source manifest"
    )
    patch_path = _relative_path(patch.payload.get("raw_patch_path"), "raw patch")
    source_manifest = _read_json(root, source_manifest_path)
    authority_audit = _audit_authority(source_manifest, change_number, spec)
    patch_spec = {
        **spec,
        "selected_test_marker": profile.payload["selected_test_marker"],
    }
    patch_audit = _audit_patch(root, patch_path, patch_spec)
    attachment_audit = _attachment_audit(source_manifest, profile.payload)
    capsule_audit = _capsule_audit(root, capsule.payload, spec)
    runtime_root = _relative_path(profile.payload.get("runtime_root"), "runtime root")
    runtime_present = _ordinary_directory(root / runtime_root)

    profile_gate = profile.payload.get("strict_gate_status")
    if type(profile_gate) is not dict:
        raise RuntimeError("JupyterLab profile gate status is missing")
    fixed_api_status = profile_gate.get("fixed_test_api_compatibility")
    if fixed_api_status not in {"passed_static", "pending_execution"}:
        raise RuntimeError("JupyterLab fixed API status changed")

    blocker_urls = profile.payload.get("source_closure_blockers", [])
    if type(blocker_urls) is not list or any(
        type(value) is not str for value in blocker_urls
    ):
        raise RuntimeError("JupyterLab source-closure blocker list is invalid")

    payload: dict[str, Any] = {
        "candidate": {"repository_key": "jupyterlab", "change_number": change_number},
        "profile_id": spec["profile_id"],
        "state": "candidate_pool_reopened_pending_gate4_7",
        "outcome": "candidate_pool_reclassified",
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "eligible_for_fresh_candidate_specific_gate4_7",
        "reclassification": {
            "from": [
                "source_closure_pending",
                str(preparation.payload.get("outcome")),
            ],
            "to": "candidate_pool_pending",
            "reason": "missing_historical_attachments_is_nonterminal_follow_up",
            "roster_modified": False,
            "old_terminal_conclusion_inherited": False,
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "new_build_started": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed_issue_and_merged_pull",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed_static_production_only",
            "gate_4_fixed_test_api_compatibility": fixed_api_status,
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_fixed_buggy_p2p_3x": "not_run",
        },
        "authority": authority_audit,
        "patch": patch_audit,
        "source_closure": {
            "status": "pending_nonterminal",
            "attachment_urls_declared_by_profile": blocker_urls,
            "attachment_audit": attachment_audit,
        },
        "fixed_capsule": capsule_audit,
        "runtime": {
            "dependency_id": profile.payload.get("runtime_dependency_id"),
            "runtime_root": runtime_root,
            "registered": runtime_present,
            "status": "pending" if not runtime_present else "registered",
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "candidate_specific_runner_created": False,
        },
        "next_action": (
            "retain_candidate_in_pool; complete optional authority attachment capture, "
            "then materialize candidate-specific fixed_buggy pair and run fresh gate4_7"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
            "downstream_8_13": False,
        },
    }
    receipt = _receipt_reference(change_number)
    inputs = refs
    existing = _try_load(root, receipt)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(
                "JupyterLab reclassification receipt conflicts with exact state"
            )
        return existing
    write_stable_artifact(
        root,
        artifact_id=receipt.artifact_id,
        artifact_kind=receipt.artifact_kind,
        semantic_version=receipt.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, receipt)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("JupyterLab reclassification typed reload failed")
    return artifact


def _refs(change_number: int) -> tuple[ArtifactReference, ...]:
    prefix = f"jupyterlab/{change_number}"
    return (
        _ENUMERATION,
        ArtifactReference(
            f"candidate_source_packet/{prefix}/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        ArtifactReference(
            f"candidate_test_patch/{prefix}/screening/v2", "candidate_test_patch", "2.0"
        ),
        ArtifactReference(
            f"screening_checkpoint/{prefix}/current/v2", "screening_checkpoint", "2.0"
        ),
        ArtifactReference(
            f"source_atomicity/{prefix}/preannotation/v2", "source_atomicity", "2.0"
        ),
        ArtifactReference(
            f"candidate_stage/{prefix}/test_manifest/v2", "candidate_stage", "2.0"
        ),
        ArtifactReference(
            f"preannotation_checkpoint/{prefix}/current/v2",
            "preannotation_checkpoint",
            "2.0",
        ),
        ArtifactReference(
            f"fixed_source_capsule/{prefix}/fixed/v2", "fixed_source_capsule", "2.0"
        ),
        ArtifactReference(
            f"candidate_profile/{prefix}/preflight/v2", "candidate_profile", "2.0"
        ),
        ArtifactReference(
            f"candidate_preparation/{prefix}/preannotation/v2",
            "candidate_preparation",
            "2.0",
        ),
    )


def _receipt_reference(change_number: int) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_reclassification/jupyterlab/{change_number}/current-v2",
        "candidate_reclassification",
        "1.0",
    )


def _validate_chain(chain: tuple[StableArtifact, ...], spec: dict[str, Any]) -> None:
    if len(chain) != 10:
        raise RuntimeError("JupyterLab current-v2 chain is incomplete")
    if any(
        artifact.payload.get("candidate")
        != {
            "repository_key": "jupyterlab",
            "change_number": chain[1].payload.get("candidate", {}).get("change_number"),
        }
        for artifact in chain[1:]
    ):
        raise RuntimeError("JupyterLab current-v2 candidate binding is invalid")
    (
        enumeration,
        source,
        patch,
        screening,
        atomicity,
        manifest,
        checkpoint,
        capsule,
        profile,
        preparation,
    ) = chain
    number = int(source.payload["candidate"]["change_number"])
    candidate = {"repository_key": "jupyterlab", "change_number": number}
    if number not in _CANDIDATES or spec is not _CANDIDATES[number]:
        raise RuntimeError("JupyterLab candidate profile is not registered")
    if (
        enumeration.payload.get("repository_key") != "jupyterlab"
        or enumeration.payload.get("complete") is not True
    ):
        raise RuntimeError("JupyterLab enumeration is not complete")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != candidate
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("JupyterLab source packet is not current-v2 bound")
    if (
        patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("candidate") != candidate
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("JupyterLab patch receipt is not eligible")
    if (
        screening.inputs != (_ENUMERATION, patch.reference)
        or screening.payload.get("candidate") != candidate
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("JupyterLab screening checkpoint is not current")
    if (
        atomicity.inputs
        != (_ENUMERATION, source.reference, patch.reference, screening.reference)
        or atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [spec["issue"]]
        or atomicity.payload.get("linked_issue_numbers") != [spec["issue"]]
    ):
        raise RuntimeError("JupyterLab source atomicity is not accepted")
    if (
        manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("candidate") != candidate
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("complete") is not True
    ):
        raise RuntimeError("JupyterLab test manifest is not current")
    if (
        checkpoint.inputs != (manifest.reference,)
        or checkpoint.payload.get("candidate") != candidate
        or checkpoint.payload.get("state") != "active"
    ):
        raise RuntimeError("JupyterLab preannotation checkpoint is not current")
    if (
        capsule.inputs
        != (source.reference, patch.reference, atomicity.reference, manifest.reference)
        or capsule.payload.get("candidate") != candidate
        or capsule.payload.get("source_origin") != "captured_merged_revision"
        or capsule.payload.get("profile_id") != spec["profile_id"]
    ):
        raise RuntimeError("JupyterLab fixed capsule is not current-v2 bound")
    if (
        profile.inputs
        != (source.reference, patch.reference, atomicity.reference, manifest.reference)
        or profile.payload.get("candidate") != candidate
        or profile.payload.get("profile_id") != spec["profile_id"]
        or profile.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("JupyterLab candidate profile is not current-v2 bound")
    if (
        preparation.inputs != (profile.reference,)
        or preparation.payload.get("candidate") != candidate
        or preparation.payload.get("terminal") is not False
        or preparation.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("JupyterLab preparation is not resumable")


def _audit_authority(
    source_manifest: dict[str, Any], number: int, spec: dict[str, Any]
) -> dict[str, Any]:
    candidate = {"repository_key": "jupyterlab", "change_number": number}
    authority = source_manifest.get("authority")
    issue_numbers = source_manifest.get("issue_numbers")
    pull = source_manifest.get("pull")
    if (
        source_manifest.get("candidate") != candidate
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or issue_numbers != [spec["issue"]]
        or type(authority) is not dict
        or authority.get("repository") != "jupyterlab/jupyterlab"
        or type(pull) is not dict
        or pull.get("number") != number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("JupyterLab authority evidence drifted")
    issues = source_manifest.get("issues")
    if type(issues) is not list:
        raise RuntimeError("JupyterLab issue evidence is missing")
    issue = None
    for row in issues:
        if type(row) is not dict or row.get("number") != spec["issue"]:
            continue
        nested = row.get("issue")
        issue = nested if type(nested) is dict else row
        break
    if type(issue) is not dict or issue.get("title") != spec["bug_title"]:
        raise RuntimeError("JupyterLab issue does not match registered real bug")
    body = issue.get("body") if type(issue.get("body")) is str else ""
    if any(marker not in body for marker in spec["bug_markers"]):
        raise RuntimeError("JupyterLab issue bug markers are incomplete")
    pull_body = pull.get("body") if type(pull.get("body")) is str else ""
    if f"#{spec['issue']}" not in pull_body:
        raise RuntimeError("JupyterLab pull does not bind the authoritative issue")
    return {
        "provider": "github",
        "repository": "jupyterlab/jupyterlab",
        "pull_number": number,
        "merged_pull": True,
        "primary_issue_number": spec["issue"],
        "issue_title": spec["bug_title"],
        "real_bug_evidence": True,
        "single_authoritative_issue": True,
    }


def _audit_patch(root: Path, relative: str, spec: dict[str, Any]) -> dict[str, Any]:
    content = _read_bytes(root, relative)
    sections: list[tuple[str, bytes]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append(
                    (current_path, "".join(current).encode("utf-8", "surrogateescape"))
                )
            parts = shlex.split(line.rstrip("\r\n"))
            if (
                len(parts) != 4
                or parts[2][0:2] != "a/"
                or parts[3][0:2] != "b/"
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("JupyterLab patch header is invalid")
            current_path = parts[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append(
            (current_path, "".join(current).encode("utf-8", "surrogateescape"))
        )
    expected = spec["production_paths"] + spec["test_paths"]
    paths = [path for path, _ in sections]
    if paths != expected or len(set(paths)) != len(paths):
        raise RuntimeError("JupyterLab patch path partition drifted")
    selected = dict(sections)[spec["selected_test_path"]].decode(
        "utf-8", "surrogateescape"
    )
    if spec["selected_test_marker"] not in selected:
        raise RuntimeError("JupyterLab selected regression marker is unbound")
    production = b"".join(dict(sections)[path] for path in spec["production_paths"])
    if any(path.encode("utf-8") in production for path in spec["test_paths"]):
        raise RuntimeError("JupyterLab static inverse scope touches a test")
    return {
        "raw_patch_path": relative,
        "production_paths": list(spec["production_paths"]),
        "test_paths": list(spec["test_paths"]),
        "selected_test_path": spec["selected_test_path"],
        "selected_test_marker_present": True,
        "production_only_inverse_static": True,
        "preserved_test_hunks": True,
    }


def _attachment_audit(
    source_manifest: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    expected = profile.get("source_closure_blockers", [])
    captured = source_manifest.get("attachment_paths", [])
    if type(captured) is not list:
        raise RuntimeError("JupyterLab attachment path list is invalid")
    missing = [url for url in expected if url not in captured]
    return {
        "expected_url_count": len(expected),
        "captured_attachment_count": len(captured),
        "missing_urls": missing,
        "status": "pending_missing_captured_attachments" if missing else "complete",
        "is_nonterminal": True,
    }


def _capsule_audit(
    root: Path, capsule: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    relative = _relative_path(capsule.get("source_tree_path"), "fixed capsule tree")
    path = root / relative
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("JupyterLab fixed capsule tree is unsafe")
    return {
        "source_origin": "captured_merged_revision",
        "source_tree_path": relative,
        "present": True,
        "ordinary_directory": True,
        "profile_id": spec["profile_id"],
    }


def _write_missing_chain_receipt(
    root: Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    enumeration = _load(root, _ENUMERATION)
    candidates = enumeration.payload.get("candidates")
    if (
        type(candidates) is not list
        or {"repository_key": "jupyterlab", "change_number": 2653} not in candidates
    ):
        raise RuntimeError("JupyterLab #2653 is not in current-v2 enumeration")
    expected = {
        "source": "candidate_source_packet/jupyterlab/2653/source/v2",
        "patch": "candidate_test_patch/jupyterlab/2653/screening/v2",
        "atomicity": "source_atomicity/jupyterlab/2653/preannotation/v2",
        "manifest": "candidate_stage/jupyterlab/2653/test_manifest/v2",
    }
    present = {
        name: _artifact_directory_exists(root, artifact_id)
        for name, artifact_id in expected.items()
    }
    receipt = ArtifactReference(
        "candidate_reclassification_blocker/jupyterlab/2653/current-v2",
        "candidate_reclassification_blocker",
        "1.0",
    )
    payload = {
        "candidate": {"repository_key": "jupyterlab", "change_number": 2653},
        "state": "blocked_missing_current_v2_source_chain",
        "outcome": "current_v2_chain_not_registered",
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "not_reclassified_pending_source_packet",
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "current_v2_artifacts_present": present,
        "reason": "enumerated_candidate_has_no_typed_source_patch_atomicity_manifest_chain",
        "next_action": "register_fresh_current_v2_source_packet_then_repeat_candidate_audit",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_ENUMERATION,)
    existing = _try_load(root, receipt)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("JupyterLab #2653 missing-chain receipt conflicts")
        return existing
    write_stable_artifact(
        root,
        artifact_id=receipt.artifact_id,
        artifact_kind=receipt.artifact_kind,
        semantic_version=receipt.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, receipt)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("JupyterLab #2653 missing-chain typed reload failed")
    return artifact


def _artifact_directory_exists(root: Path, artifact_id: str) -> bool:
    path = root / "artifacts" / PurePosixPath(artifact_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("JupyterLab reclassification authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _relative_path(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError(f"{label} path is unsafe")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    path = root / PurePosixPath(relative)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("JupyterLab raw evidence is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("JupyterLab raw evidence path is unsafe")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("JupyterLab raw evidence file is unsafe")
    return path.read_bytes()


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    content = _read_bytes(root, relative)

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError("JupyterLab source manifest has duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("JupyterLab source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("JupyterLab source manifest is not an object")
    return value


def _ordinary_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_jupyterlab_candidate_reclassification"]
