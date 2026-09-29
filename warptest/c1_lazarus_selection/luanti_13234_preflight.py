"""Offline Gate 1--4 and source-capsule preflight for Luanti #13234.

The PR adds a shapeless-craft regression test and replaces the factorial
permutation search with bipartite matching.  The production-only inverse is
limited to ``craftdef.cpp`` and ``craftdef.h``; the added test and its CMake
registration remain in both variants.  This module only materializes the
fixed source from the local mirror and records the static API audit.  It never
configures, builds, executes, contacts a provider, or authorizes paid/formal
work.
"""

from __future__ import annotations

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


_CANDIDATE = {"repository_key": "luanti", "change_number": 13234}
_SOURCE = ArtifactReference("candidate_source_packet/luanti/13234/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/13234/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/13234/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/13234/test_manifest/v2", "candidate_stage", "2.0")
_CAPSULE = ArtifactReference("fixed_source_capsule/luanti/13234/fixed/v1", "fixed_source_capsule", "1.0")
_NATIVE_CAPSULE = ArtifactReference("fixed_source_capsule/luanti/13234/fixed/v2", "fixed_source_capsule", "2.0")
_PROFILE = ArtifactReference("candidate_profile/luanti/13234/preflight/v2", "candidate_profile", "2.0")
_MIRROR = Path("private/repository-enumeration-git/luanti/mirror.git")
_CAPSULE_ROOT = Path("private/fixed-capsules/luanti/13234/fixed-source")
_FIXED = "ccd696c49af9e346c9408859b68ef9731c4a4b02"
_BUGGY = "ae7271b7259437f613f7bca0a3b71ee7ebfa9c51"
_PRODUCTION = ("src/craftdef.cpp", "src/craftdef.h")
_TEST = "src/unittest/test_craft.cpp"
_SUPPORT = "src/unittest/CMakeLists.txt"
_BASELINE_SYMBOLS = (
    "CraftInput", "CraftOutput", "CraftDefinitionShapeless", "CraftReplacements",
    "IWritableCraftDefManager", "IWritableItemDefManager", "getCraftResult",
    "registerCraft", "registerItem", "registerAlias", "initHashes", "clear",
)


def prepare_luanti_13234(artifact_root: str | Path, *, authorization: AuthorizationRecord) -> tuple[StableArtifact, StableArtifact]:
    """Materialize the fixed source and persist a static no-build preflight."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST))
    source, patch, atomicity, manifest = chain
    _validate_chain(source, patch, atomicity, manifest)
    mirror = root / _MIRROR
    if not _has_commit(mirror, _FIXED) or not _has_commit(mirror, _BUGGY):
        raise RuntimeError("#13234 fixed or buggy source revision is absent from local mirror")
    raw_patch = root / patch.payload["raw_patch_path"]
    patch_text = raw_patch.read_text(encoding="utf-8", errors="surrogateescape")
    _validate_raw_patch(patch_text)
    source_symbols = _baseline_symbol_probe(mirror)
    if not all(source_symbols.values()):
        missing = ", ".join(name for name, present in source_symbols.items() if not present)
        raise RuntimeError(f"#13234 buggy parent lacks preserved test API: {missing}")

    capsule_path = root / _CAPSULE_ROOT
    _materialize_capsule(mirror, capsule_path)
    capsule_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-13234-shapeless-hopcroft-karp-v1",
        "source_tree_path": _CAPSULE_ROOT.as_posix(),
        "source_tree_present": capsule_path.is_dir(),
        "fixed_revision": _FIXED,
        "buggy_parent_revision": _BUGGY,
        "fixed_tree": _git_text(mirror, "rev-parse", f"{_FIXED}^{{tree}}"),
        "preserved_test_paths": [_TEST],
        "support_paths": [_SUPPORT],
        "production_inverse_paths": list(_PRODUCTION),
        "candidate_materialization_performed": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    capsule = _write_idempotent(root, _CAPSULE, chain, capsule_payload, authorization)
    profile_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-13234-shapeless-hopcroft-karp-v1",
        "state": "prepared",
        "outcome": "gate1_4_passed_pending_gate57",
        "terminal": False,
        "resumable": True,
        "primary_issue_number": 11224,
        "linked_issue_numbers": [4901, 11224],
        "strict_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed_production_only",
            "test_api_compatibility": "passed_static_buggy_parent",
            "version_materialization": "fixed_capsule_ready_buggy_mirror_ready",
            "fixed_f2p_3x": "downstream_pending",
            "buggy_f2p_3x": "downstream_pending",
            "fixed_buggy_p2p_3x": "downstream_pending",
        },
        "fixed_f2p_selector": "TestCraft.testShapeless",
        "p2p_selector": "requires_registered_control_selector_after_test_module_listing",
        "production_inverse_paths": list(_PRODUCTION),
        "preserved_test_paths": [_TEST],
        "support_paths": [_SUPPORT],
        "buggy_parent_api_symbol_results": [
            {"symbol_name": name, "present": present}
            for name, present in source_symbols.items()
        ],
        "inverse_rule": "reverse_only_src/craftdef.cpp_and_src/craftdef.h; retain_test_craft_and_CMake_registration",
        "candidate_materialization_performed": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "after native slot is free, register an exact P2P control selector and run an independent fresh 12-cell epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False, "native_build_started": False},
    }
    profile = _write_idempotent(root, _PROFILE, chain + (capsule.reference,), profile_payload, authorization)
    return capsule, profile


def write_luanti_13234_native_capsule(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Bind the existing materialized tree to the native executor's v2 schema."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    capsule_path = root / _CAPSULE_ROOT
    if not capsule_path.is_dir() or not (capsule_path / _TEST).is_file():
        raise RuntimeError("#13234 materialized fixed capsule is unavailable")
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-13234-shapeless-hopcroft-karp-v1-native-gate57",
        "source_tree_path": _CAPSULE_ROOT.as_posix(),
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": [_TEST],
        "p2p_test_paths": ["src/unittest/test_settings.cpp"],
        "support_paths": [_SUPPORT],
    }
    return _write_idempotent(
        root,
        _NATIVE_CAPSULE,
        (source, patch, manifest),
        payload,
        authorization,
    )


def _validate_chain(source: StableArtifact, patch: StableArtifact, atomicity: StableArtifact, manifest: StableArtifact) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in (source, patch, atomicity, manifest)):
        raise RuntimeError("#13234 current-v2 candidate binding drifted")
    if source.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#13234 source or patch receipt is not eligible")
    if tuple(patch.payload.get("production_paths", ())) != _PRODUCTION or tuple(patch.payload.get("test_paths", ())) != (_TEST,):
        raise RuntimeError("#13234 typed patch inventory drifted")
    if (atomicity.payload.get("outcome") != "accepted" or atomicity.payload.get("authoritative_issue_count") != 1 or atomicity.payload.get("primary_issue_number") != 11224):
        raise RuntimeError("#13234 accepted atomicity mapping drifted")
    details = manifest.payload.get("details")
    if manifest.payload.get("state") != "passed" or not isinstance(details, dict) or details.get("executable_test_paths") != [_TEST] or details.get("test_count") != 1:
        raise RuntimeError("#13234 executable test manifest drifted")


def _validate_raw_patch(text: str) -> None:
    paths = tuple(line.split(" b/", 1)[1] for line in text.splitlines() if line.startswith("diff --git a/"))
    if set(paths) != {*_PRODUCTION, _TEST, _SUPPORT}:
        raise RuntimeError("#13234 raw patch has unaccounted paths")
    if "${CMAKE_CURRENT_SOURCE_DIR}/test_craft.cpp" not in text:
        raise RuntimeError("#13234 test registration support hunk is missing")
    if "CraftDefinitionShapeless::check" not in text or "hopcroft_karp_can_match_all" not in text:
        raise RuntimeError("#13234 production inverse is not the expected algorithm replacement")


def _baseline_symbol_probe(mirror: Path) -> dict[str, bool]:
    return {
        symbol: subprocess.run(
            ["git", f"--git-dir={mirror}", "grep", "-q", symbol, _BUGGY, "--", "src"],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        ).returncode == 0
        for symbol in _BASELINE_SYMBOLS
    }


def _materialize_capsule(mirror: Path, target: Path) -> None:
    if target.exists():
        if not target.is_dir():
            raise RuntimeError("#13234 fixed capsule target is unsafe")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{os.getpid()}"
    if staging.exists():
        raise RuntimeError("#13234 fixed capsule staging directory already exists")
    staging.mkdir(mode=0o700)
    try:
        archive = subprocess.Popen(["git", f"--git-dir={mirror}", "archive", "--format=tar", _FIXED], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        unpack = subprocess.run(["tar", "-xf", "-", "-C", str(staging)], stdin=archive.stdout, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if archive.stdout is not None:
            archive.stdout.close()
        _, archive_stderr = archive.communicate(timeout=30)
        if archive.returncode != 0 or unpack.returncode != 0:
            raise RuntimeError("#13234 fixed source archive extraction failed")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _has_commit(mirror: Path, revision: str) -> bool:
    return subprocess.run(["git", f"--git-dir={mirror}", "cat-file", "-e", f"{revision}^{{commit}}"], check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0


def _git_text(mirror: Path, *arguments: str) -> str:
    return subprocess.run(["git", f"--git-dir={mirror}", *arguments], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20).stdout.strip()


def _write_idempotent(root: Path, reference: ArtifactReference, inputs: tuple[StableArtifact, ...] | tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    typed_inputs = tuple(item.reference if isinstance(item, StableArtifact) else item for item in inputs)
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != typed_inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#13234 {reference.artifact_kind} conflicts with current state")
        return existing
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=typed_inputs, payload=payload, authorization=authorization)
    artifact = _load(root, reference)
    if artifact.inputs != typed_inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#13234 {reference.artifact_kind} typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != "preannotation-execution" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#13234 preflight authorization is inactive")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["prepare_luanti_13234", "write_luanti_13234_native_capsule"]
