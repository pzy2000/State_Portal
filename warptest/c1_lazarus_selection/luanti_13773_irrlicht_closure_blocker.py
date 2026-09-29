"""Typed immutable-source blocker for Luanti #13773's Irrlicht closure.

The current candidate capsule does not contain the historical ``irrUString.h``.
The v3 dependency receipt instead copies current ``irrString.h`` under the old
filename; that content does not define ``core::ustring`` required by
``CGUITTFont.h``.  This audit is read-only except for its resumable receipt and
never builds or executes candidate code.
"""

from __future__ import annotations

import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 13773}
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/13773/preflight/v2", "candidate_profile", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/13773/fixed/v2", "fixed_source_capsule", "2.0"
)
_DEPENDENCY = ArtifactReference(
    "candidate_dependency_closure/luanti/13773/client/v3",
    "candidate_dependency_closure",
    "1.0",
)
_GATE4 = ArtifactReference(
    "candidate_gate4_api_check/luanti/13773/native/v6",
    "candidate_gate4_api_check",
    "1.0",
)
_DIAGNOSTIC = ArtifactReference(
    "candidate_gate57_blocker/luanti/13773/native/v5",
    "candidate_gate57_blocker",
    "1.0",
)
_CURRENT_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/13773/native/v6",
    "candidate_gate57_blocker",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_historical_irrlicht_blocker/luanti/13773/current-v2/v1",
    "candidate_historical_irrlicht_blocker",
    "1.0",
)

_CAPSULE_ROOT = PurePosixPath("private/fixed-capsules/luanti/13773/fixed-source")
_CAPSULE_IRRSTRING = _CAPSULE_ROOT / "lib/irrlichtmt/include/irrString.h"
_CAPSULE_IRRUSTRING = _CAPSULE_ROOT / "lib/irrlichtmt/include/irrUString.h"
_DEPENDENCY_IRRUSTRING = PurePosixPath(
    "private/dependencies/luanti-client/13773/v3/packages/irrlicht/include/irrUString.h"
)
_FONT_HEADER = _CAPSULE_ROOT / "src/irrlicht_changes/CGUITTFont.h"
_FUTURE_NAMESPACES = {
    "dependency_v4": "artifacts/candidate_dependency_closure/luanti/13773/client/v4",
    "immutable_source_receipt": "artifacts/candidate_immutable_source/luanti/13773/irrlicht/v1",
    "dependency_v4_file": (
        "private/dependencies/luanti-client/13773/v4/packages/irrlicht/include/irrUString.h"
    ),
    "immutable_source_file": (
        "private/immutable-sources/luanti/13773/irrlicht/irrUString.h"
    ),
}


def write_luanti_13773_irrlicht_closure_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Bind the invalid relabel and absence of a historical immutable source."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#13773 Irrlicht audit authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    profile, capsule, dependency, gate4, diagnostic, current = (
        _load(root, reference)
        for reference in (
            _PROFILE,
            _CAPSULE,
            _DEPENDENCY,
            _GATE4,
            _DIAGNOSTIC,
            _CURRENT_BLOCKER,
        )
    )
    _validate_artifacts(profile, capsule, dependency, gate4, diagnostic, current)

    capsule_irrstring = _ordinary_file(root / _CAPSULE_IRRSTRING, root)
    dependency_irrust = _ordinary_file(root / _DEPENDENCY_IRRUSTRING, root)
    font_header = _ordinary_file(root / _FONT_HEADER, root)
    if (root / _CAPSULE_IRRUSTRING).exists():
        raise RuntimeError("#13773 candidate capsule gained irrUString.h; re-audit required")
    source_bytes = capsule_irrstring.read_bytes()
    dependency_bytes = dependency_irrust.read_bytes()
    if source_bytes != dependency_bytes:
        raise RuntimeError("#13773 v3 relabeled dependency content drifted")
    dependency_text = dependency_bytes.decode("utf-8", errors="surrogateescape")
    font_text = font_header.read_text(encoding="utf-8", errors="surrogateescape")
    if "core::ustring" in dependency_text or "ustring" in dependency_text:
        raise RuntimeError("#13773 dependency now contains historical ustring API")
    if "#include <irrUString.h>" not in font_text or "core::ustring" not in font_text:
        raise RuntimeError("#13773 fixed source no longer requires historical irrUString API")

    future_present = {
        name: (root / PurePosixPath(relative)).exists()
        for name, relative in _FUTURE_NAMESPACES.items()
    }
    if any(future_present.values()):
        raise RuntimeError("#13773 new immutable Irrlicht evidence appeared; re-audit required")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "infrastructure_blocked",
        "outcome": "historical_irrlicht_immutable_source_unavailable",
        "terminal": False,
        "resumable": True,
        "reason": "v3_dependency_relabels_current_irrString_as_irrUString_without_core_ustring_API",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed_static_production_only",
            "test_api_compatibility": "passed_candidate_specific_compile_fixture",
            "gate_5_7": "blocked_before_fresh_epoch",
        },
        "closure_audit": {
            "capsule_irrUString_path": _CAPSULE_IRRUSTRING.as_posix(),
            "capsule_irrUString_present": False,
            "relabel_source_path": _CAPSULE_IRRSTRING.as_posix(),
            "relabel_destination_path": _DEPENDENCY_IRRUSTRING.as_posix(),
            "relabel_bytes_equal": True,
            "relabel_byte_count": len(dependency_bytes),
            "relabel_contains_ustring_API": False,
            "consumer_path": _FONT_HEADER.as_posix(),
            "consumer_requires_include": "irrUString.h",
            "consumer_requires_type": "core::ustring",
            "legal_historical_immutable_source_registered": False,
        },
        "future_namespace_probe": {
            name: {"path": _FUTURE_NAMESPACES[name], "present": present}
            for name, present in future_present.items()
        },
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "network_actions_authorized": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "register_a_provenance_bound_historical_irrUString_header_and_matching_irrlicht_closure_then_use_a_fresh_namespace",
        "complete": True,
    }
    inputs = (_PROFILE, _CAPSULE, _DEPENDENCY, _GATE4, _DIAGNOSTIC, _CURRENT_BLOCKER)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13773 historical Irrlicht blocker conflicts with current state")
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
    blocker = _load(root, _BLOCKER)
    if blocker.inputs != inputs or dict(blocker.payload) != payload:
        raise RuntimeError("#13773 historical Irrlicht blocker typed reload failed")
    return blocker


def _validate_artifacts(
    profile: StableArtifact,
    capsule: StableArtifact,
    dependency: StableArtifact,
    gate4: StableArtifact,
    diagnostic: StableArtifact,
    current: StableArtifact,
) -> None:
    if any(
        artifact.payload.get("candidate") != _CANDIDATE
        for artifact in (profile, capsule, dependency, gate4, diagnostic, current)
    ):
        raise RuntimeError("#13773 typed candidate binding drifted")
    inventory = dependency.payload.get("inventory")
    if type(inventory) is not list:
        raise RuntimeError("#13773 dependency inventory is invalid")
    irr = [
        item
        for item in inventory
        if type(item) is dict
        and item.get("destination") == "packages/irrlicht/include/irrUString.h"
    ]
    if len(irr) != 1 or not str(irr[0].get("source", "")).endswith(
        "/lib/irrlichtmt/include/irrString.h"
    ):
        raise RuntimeError("#13773 v3 dependency provenance drifted")
    if (
        gate4.payload.get("state") != "passed"
        or gate4.payload.get("fixed_returncode") != 0
        or gate4.payload.get("buggy_returncode") != 0
        or diagnostic.payload.get("reason") != "missing_irrlicht_header"
        or "irrUString.h" not in str(diagnostic.payload.get("compiler_diagnostic"))
        or current.payload.get("state") != "infrastructure_blocked"
    ):
        raise RuntimeError("#13773 current blocker chain drifted")


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("#13773 exact evidence file is missing or unsafe") from error
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#13773 exact evidence file is missing or unsafe")
    return resolved


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


__all__ = ["write_luanti_13773_irrlicht_closure_blocker"]
