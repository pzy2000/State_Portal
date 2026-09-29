"""Record a nonterminal downstream blocker for Luanti current-v2 candidates.

The Luanti source/patch screening chain for changes 13825 and 13773 is
registered and is audited here without starting a build or a candidate
process.  Neither candidate currently has a registered candidate profile,
preparation/downstream plan, or fixed source capsule.  The receipt therefore
keeps both candidates in the pool and makes the exact next action resumable;
it is deliberately not a terminal or a Gate 5--7 result.
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
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)

_SPECS: dict[int, dict[str, Any]] = {
    13825: {
        "issue": 12500,
        "issue_title": "Allow formspecs to open URLs",
        "issue_markers": ("some way of opening URLs", "user interaction"),
        "production_paths": (
            "builtin/mainmenu/tab_about.lua",
            "games/devtest/mods/testformspec/formspec.lua",
            "src/client/game.cpp",
            "src/gui/guiFormSpecMenu.cpp",
            "src/gui/guiFormSpecMenu.h",
            "src/gui/guiHyperText.cpp",
            "src/gui/guiOpenURL.cpp",
            "src/gui/guiOpenURL.h",
            "src/gui/mainmenumanager.h",
            "src/util/colorize.cpp",
            "src/util/colorize.h",
            "src/util/string.cpp",
            "src/util/string.h",
        ),
        "patch_paths": (
            "builtin/mainmenu/tab_about.lua",
            "doc/lua_api.md",
            "games/devtest/mods/testformspec/formspec.lua",
            "src/client/game.cpp",
            "src/gui/CMakeLists.txt",
            "src/gui/guiFormSpecMenu.cpp",
            "src/gui/guiFormSpecMenu.h",
            "src/gui/guiHyperText.cpp",
            "src/gui/guiOpenURL.cpp",
            "src/gui/guiOpenURL.h",
            "src/gui/mainmenumanager.h",
            "src/unittest/test_utilities.cpp",
            "src/util/CMakeLists.txt",
            "src/util/colorize.cpp",
            "src/util/colorize.h",
            "src/util/string.cpp",
            "src/util/string.h",
        ),
        "auxiliary_paths": (
            "doc/lua_api.md",
            "src/gui/CMakeLists.txt",
            "src/util/CMakeLists.txt",
        ),
        "test_paths": ("src/unittest/test_utilities.cpp",),
        "test_marker": "testColorizeURL",
    },
    13773: {
        "issue": 13770,
        "issue_title": "Keypad input doubles as digit input",
        "issue_markers": ("Keypad", "active item"),
        "production_paths": ("src/client/keycode.h",),
        "patch_paths": ("src/client/keycode.h", "src/unittest/test_keycode.cpp"),
        "auxiliary_paths": (),
        "test_paths": ("src/unittest/test_keycode.cpp",),
        "test_marker": "KEY_NUMPAD5",
    },
}


def write_luanti_candidate_chain_blocker(
    artifact_root: str | Path,
    change_number: int,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Audit Gate 1--4 and persist a resumable missing-chain receipt.

    This function only loads typed artifacts and reads the already-captured
    source manifest and patch.  It never fetches, materializes, builds, or
    executes a candidate.  Existing receipts are immutable and are returned
    only when their complete typed payload still agrees with this audit.
    """

    _require_authorization(authorization)
    if change_number not in _SPECS:
        raise ValueError("only Luanti #13825 and #13773 are supported")
    root = Path(artifact_root).absolute().resolve(strict=True)
    spec = _SPECS[change_number]
    chain = _load_chain(root, change_number)
    _enumeration, source, patch, atomicity, manifest, screening, checkpoint = chain
    source_manifest_path = _safe_relative(source.payload.get("raw_evidence_path"))
    patch_path = _safe_relative(patch.payload.get("raw_patch_path"))
    source_manifest = _read_json(root, source_manifest_path)
    source_audit = _audit_source(source_manifest, change_number, spec)
    patch_audit = _audit_patch(root, patch_path, patch.payload, spec)

    missing = {
        name: _artifact_presence(
            root,
            ArtifactReference(artifact_id, artifact_kind, "2.0"),
        )
        for name, artifact_id, artifact_kind in (
            (
                "candidate_profile",
                f"candidate_profile/luanti/{change_number}/preflight/v2",
                "candidate_profile",
            ),
            (
                "candidate_preparation",
                f"candidate_preparation/luanti/{change_number}/preannotation/v2",
                "candidate_preparation",
            ),
            (
                "candidate_downstream_plan",
                f"candidate_downstream_plan/luanti/{change_number}/preannotation/v2",
                "candidate_downstream_plan",
            ),
            (
                "fixed_source_capsule",
                f"fixed_source_capsule/luanti/{change_number}/fixed/v2",
                "fixed_source_capsule",
            ),
        )
    }
    receipt = ArtifactReference(
        f"candidate_reclassification_blocker/luanti/{change_number}/current-v2",
        "candidate_reclassification_blocker",
        "1.0",
    )
    candidate = {"repository_key": "luanti", "change_number": change_number}
    payload: dict[str, Any] = {
        "candidate": candidate,
        "state": "blocked_missing_current_v2_profile_capsule",
        "outcome": "current_v2_downstream_chain_not_registered",
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "pending_candidate_specific_profile_and_capsule",
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_authoritative_issue",
            "gate_3_strict_inverse_patch": "passed_static_production_test_partition",
            "gate_4_fixed_test_api_compatibility": "pending_candidate_profile_capsule",
            "gate_5_fixed_f2p_3x": "not_run_missing_profile_capsule",
            "gate_6_buggy_f2p_3x_failure": "not_run_missing_profile_capsule",
            "gate_7_fixed_buggy_p2p_3x": "not_run_missing_profile_capsule",
        },
        "chain_refs": {
            "source": source.reference.artifact_id,
            "patch": patch.reference.artifact_id,
            "atomicity": atomicity.reference.artifact_id,
            "test_manifest": manifest.reference.artifact_id,
            "screening_checkpoint": screening.reference.artifact_id,
            "preannotation_checkpoint": checkpoint.reference.artifact_id,
        },
        "static_audit": {
            "authority": source_audit,
            "patch": patch_audit,
            "test_manifest_state": manifest.payload.get("state"),
            "source_atomicity_outcome": atomicity.payload.get("outcome"),
            "authoritative_issue_numbers": atomicity.payload.get(
                "authoritative_issue_numbers"
            ),
            "linked_issue_numbers": atomicity.payload.get("linked_issue_numbers"),
        },
        "missing_current_v2_artifacts": missing,
        "next_action": (
            "register a candidate-specific current-v2 profile, preparation, "
            "downstream plan, and fixed source capsule; then repeat this "
            "typed audit and run a fresh 12-cell Gate 5--7 epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(item.reference for item in chain)
    existing = _try_load(root, receipt)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("Luanti candidate blocker receipt conflicts")
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
        raise RuntimeError("Luanti candidate blocker typed reload failed")
    return artifact


def _load_chain(root: Path, change_number: int) -> tuple[StableArtifact, ...]:
    candidate = {"repository_key": "luanti", "change_number": change_number}
    refs = (
        _ENUMERATION,
        ArtifactReference(
            f"candidate_source_packet/luanti/{change_number}/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        ArtifactReference(
            f"candidate_test_patch/luanti/{change_number}/screening/v2",
            "candidate_test_patch",
            "2.0",
        ),
        ArtifactReference(
            f"source_atomicity/luanti/{change_number}/preannotation/v2",
            "source_atomicity",
            "2.0",
        ),
        ArtifactReference(
            f"candidate_stage/luanti/{change_number}/test_manifest/v2",
            "candidate_stage",
            "2.0",
        ),
        ArtifactReference(
            f"screening_checkpoint/luanti/{change_number}/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
        ArtifactReference(
            f"preannotation_checkpoint/luanti/{change_number}/current/v2",
            "preannotation_checkpoint",
            "2.0",
        ),
    )
    artifacts = tuple(_load(root, reference) for reference in refs)
    enumeration, source, patch, atomicity, manifest, screening, checkpoint = artifacts
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("Luanti enumeration repository binding drifted")
    if any(item.payload.get("candidate") != candidate for item in artifacts[1:]):
        raise RuntimeError("Luanti candidate chain binding drifted")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs
        != (_ENUMERATION, source.reference, patch.reference, screening.reference)
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.inputs != (patch.reference,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or screening.inputs != (_ENUMERATION, patch.reference)
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (manifest.reference,)
        or checkpoint.payload.get("state") != "active"
    ):
        raise RuntimeError("Luanti current-v2 Gate 1--4 chain is invalid")
    return (
        enumeration,
        source,
        patch,
        atomicity,
        manifest,
        screening,
        checkpoint,
    )


def _audit_source(
    source_manifest: dict[str, Any], change_number: int, spec: dict[str, Any]
) -> dict[str, Any]:
    candidate = {"repository_key": "luanti", "change_number": change_number}
    authority = source_manifest.get("authority")
    pull = source_manifest.get("pull")
    if (
        source_manifest.get("candidate") != candidate
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or not isinstance(authority, dict)
        or authority.get("repository") != "luanti-org/luanti"
        or not isinstance(pull, dict)
        or pull.get("number") != change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("Luanti source authority evidence is incomplete")
    issue = None
    for row in source_manifest.get("issues", []):
        if isinstance(row, dict) and row.get("number") == spec["issue"]:
            nested = row.get("issue")
            issue = nested if isinstance(nested, dict) else row
            break
    if not isinstance(issue, dict):
        raise RuntimeError("Luanti authoritative issue evidence is missing")
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    if issue.get("title") != spec["issue_title"] or any(
        marker not in body for marker in spec["issue_markers"]
    ):
        raise RuntimeError("Luanti authoritative issue does not match candidate")
    pull_body = pull.get("body") if isinstance(pull.get("body"), str) else ""
    binding = f"#{spec['issue']}"
    if binding not in pull_body:
        raise RuntimeError("Luanti pull does not bind the authoritative issue")
    return {
        "provider": "github",
        "repository": "luanti-org/luanti",
        "pull_number": change_number,
        "merged_pull": True,
        "pull_title": pull.get("title"),
        "primary_issue_number": spec["issue"],
        "issue_title": issue.get("title"),
        "direct_issue_binding": True,
        "single_authoritative_issue": True,
    }


def _audit_patch(
    root: Path,
    relative: str,
    patch_payload: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    content = _read_bytes(root, relative).decode("utf-8", "surrogateescape")
    sections: list[tuple[str, str]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, "".join(current)))
            parts = shlex.split(line.rstrip("\r\n"))
            if (
                len(parts) != 4
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("Luanti patch header is invalid")
            current_path = parts[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, "".join(current)))
    expected = list(spec["patch_paths"])
    paths = [path for path, _ in sections]
    if paths != expected or len(paths) != len(set(paths)):
        raise RuntimeError("Luanti patch path partition drifted")
    if tuple(patch_payload.get("production_paths", ())) != spec["production_paths"]:
        raise RuntimeError("Luanti production path partition drifted")
    if tuple(patch_payload.get("test_paths", ())) != spec["test_paths"]:
        raise RuntimeError("Luanti test path partition drifted")
    test_text = dict(sections)[spec["test_paths"][0]]
    if spec["test_marker"] not in test_text:
        raise RuntimeError("Luanti selected regression marker is unbound")
    production_text = "".join(
        dict(sections)[path] for path in spec["production_paths"]
    )
    if any(path in production_text for path in spec["test_paths"]):
        raise RuntimeError("Luanti inverse scope unexpectedly touches tests")
    return {
        "raw_patch_path": relative,
        "production_paths": list(spec["production_paths"]),
        "auxiliary_paths": list(spec["auxiliary_paths"]),
        "test_paths": list(spec["test_paths"]),
        "selected_test_marker": spec["test_marker"],
        "selected_test_marker_present": True,
        "production_only_inverse_static": True,
        "preserved_test_hunks": True,
    }


def _artifact_presence(root: Path, reference: ArtifactReference) -> str:
    try:
        _load(root, reference)
    except RuntimeError as error:
        if str(error) == "artifact directory is missing":
            return "missing"
        if str(error) == "artifact current head is missing":
            return "directory_without_current_head"
        raise
    return "present"


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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Luanti raw evidence path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError("Luanti raw evidence path is unsafe")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    path = root / PurePosixPath(relative)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("Luanti raw evidence is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Luanti raw evidence path is unsafe")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Luanti raw evidence file is unsafe")
    return path.read_bytes()


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(root, relative).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Luanti source manifest is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("Luanti source manifest is not an object")
    return value


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti candidate blocker authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_candidate_chain_blocker"]
