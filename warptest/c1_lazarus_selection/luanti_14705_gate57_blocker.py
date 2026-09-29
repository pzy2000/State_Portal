"""Typed current-v2 quality blocker for Luanti #14705.

The source and single-defect mapping are accepted (#14655), and the raw
patch inventory is stable.  The candidate's own test manifest is terminal
``quality_rejected`` with ``new_api_compile_only``: the Lua spec only removes
old refresh-call mocks/expectations and adds a path stub; it does not add a
behavioral assertion for the refresh lifecycle.  Gate 5--7 therefore cannot
be prepared from this current-v2 contract.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14705}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14705/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14705/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14705/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14705/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14705_quality_blocker/luanti/14705/native/v1",
    "candidate_gate57_14705_quality_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "builtin/mainmenu/content/contentdb.lua",
    "builtin/mainmenu/content/pkgmgr.lua",
    "builtin/mainmenu/content/update_detector.lua",
    "builtin/mainmenu/dlg_delete_content.lua",
    "builtin/mainmenu/dlg_rename_modpack.lua",
    "builtin/mainmenu/settings/dlg_settings.lua",
    "builtin/mainmenu/settings/settingtypes.lua",
    "builtin/mainmenu/tab_content.lua",
)
_TEST_PATHS = ("builtin/mainmenu/content/tests/pkgmgr_spec.lua",)


def write_luanti_14705_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Replay current-v2 and persist the test-contract stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14705 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    raw_patch = _regular_file(
        root / _safe_relative(patch.payload["raw_patch_path"]), "#14705 raw patch"
    ).read_text(encoding="utf-8", errors="replace")
    sections = _patch_sections(raw_patch)
    if set(sections) != set(_PRODUCTION_PATHS + _TEST_PATHS):
        raise RuntimeError("#14705 raw patch path inventory drifted")
    test_section = sections[_TEST_PATHS[0]]
    added_lines = [
        line[1:]
        for line in test_section.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not any("get_texturepath_share" in line for line in added_lines):
        raise RuntimeError("#14705 expected test stub addition is unavailable")
    if any("assert" in line.lower() for line in added_lines):
        raise RuntimeError("#14705 test unexpectedly gained an assertion")
    removed_expectations = sum(
        "assert_calls" in line or "refresh_globals" in line or "update_gamelist" in line
        for line in test_section.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    if removed_expectations < 5:
        raise RuntimeError("#14705 expected old refresh expectations are unavailable")

    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14705",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/14705",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/14705",
        "gate4_api_check": root / "artifacts/candidate_gate4_api_check/luanti/14705",
        "gate57_attempts": root / "artifacts/candidate_gate57_attempt/luanti/14705",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#14705 blocker inputs changed; re-audit exact chain")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14705-content-refresh-v1",
        "state": "blocked",
        "outcome": "test_contract_quality_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "current_v2_manifest_quality_rejected_new_api_compile_only",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed_primary_14655",
            "strict_inverse_patch": "passed_static_inventory",
            "test_api_compatibility": "blocked_before_gate4_behavioral_contract",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "patch_audit": {
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "added_test_lines": added_lines,
            "removed_refresh_expectation_count": removed_expectations,
            "behavioral_assertion_added": False,
            "explanation": (
                "The spec removes old refresh mocks/expectations and adds a core path stub, "
                "but does not assert the new reload behavior."
            ),
        },
        "manifest_quality": {
            "state": manifest.payload["state"],
            "outcome": manifest.payload["outcome"],
            "reason": manifest.payload.get("details", {}).get("reason"),
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "missing_for_fresh_gate5_7": [
            "behavioral_test_assertion_for_unnecessary_refresh_regression",
            "candidate_specific_profile_and_fixed_source_capsule",
            "gate4_api_compatibility_receipt",
            "fresh_12_cell_gate57_runner_and_aggregate",
        ],
        "next_action": (
            "add a behavior-level regression assertion, regenerate the test manifest, then re-audit Gate3/4 before execution"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14705 blocker conflicts with current-v2 state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14705 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#14705 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14705 source packet is incomplete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#14705 patch chain is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 14655
        or atomicity.payload.get("authoritative_issue_numbers") != [14655]
    ):
        raise RuntimeError("#14705 atomicity is not accepted for #14655")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "terminal"
        or manifest.payload.get("outcome") != "quality_rejected"
        or manifest.payload.get("details", {}).get("reason") != "new_api_compile_only"
    ):
        raise RuntimeError("#14705 expected manifest quality rejection is unavailable")


def _patch_sections(raw_patch: str) -> dict[str, str]:
    matches = list(re.finditer(r"^diff --git a/(\S+) b/(\S+)\s*$", raw_patch, re.M))
    return {
        match.group(2): raw_patch[
            match.start() : matches[i + 1].start() if i + 1 < len(matches) else None
        ]
        for i, match in enumerate(matches)
    }


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str):
        raise RuntimeError("#14705 evidence path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("#14705 evidence path escapes artifact root")
    return path


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} is not a regular file")
    return path


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14705_gate57_blocker"]
