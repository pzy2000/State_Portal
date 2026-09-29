"""Build and type-check the isolated Luanti #14757 repackage-v3 receipt.

This module only records a repackage already created in a fresh namespace.  It
does not launch either application and never reads or rewrites the old Gate 9
attempts.  The receipt closes the otherwise missing lineage edge for the
``games/devtest`` resources before a separately authorised bounded run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
_V1 = "1.0"
_GATE = ArtifactReference(
    "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
    "lua_regression_aggregate",
    "3.0",
)
_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    "2.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    "4.0",
)
_V2_REPACKAGE = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v2/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14757/source/v2",
    "candidate_source_packet",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14757/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_RECEIPT = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)
_PROTECTED_PIDS = (20946, 21627, 30865, 70361, 60839)
_HEAD_SHA = "d693a07716f72c8e940e5dace1b11313af2cbdeb"
_DEVTEST_TREE_SHA = "b6b82dff8686372f0f6409a9607a8876692bb19e"
_UUID_RE = re.compile(r"uuid\s+([0-9A-Fa-f-]+)")


def write_luanti_14757_repackage_v3_receipt(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Verify v3 lineage/static closure and persist one typed receipt."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    gate = _load(root, _GATE)
    transition = _load(root, _TRANSITION)
    mapping = _load(root, _MAPPING)
    v2 = _load(root, _V2_REPACKAGE)
    source = _load(root, _SOURCE)
    capsule = _load(root, _CAPSULE)
    _validate_admission(gate, transition, mapping)
    source_pull = _validate_source(root, source, capsule)
    if v2.payload.get("outcome") != "static_packaging_qualified":
        raise RuntimeError("repackage-v2 is not a qualified source pair")

    formal = {
        variant: root
        / PurePosixPath(f"private/luanti-14757-formal-materialization/{variant}")
        for variant in ("fixed", "buggy")
    }
    v2_apps = {
        variant: root
        / PurePosixPath(
            f"private/candidate_downstream/luanti/14757/repackage-v2/{variant}-minetest.app"
        )
        for variant in ("fixed", "buggy")
    }
    v3_apps = {
        variant: root
        / PurePosixPath(
            f"private/candidate_downstream/luanti/14757/repackage-v3/{variant}-minetest.app"
        )
        for variant in ("fixed", "buggy")
    }
    for path in (*formal.values(), *v2_apps.values(), *v3_apps.values()):
        _ordinary_directory(path, root)

    formal_heads = {
        variant: _git(formal[variant], "rev-parse", "HEAD")
        for variant in ("fixed", "buggy")
    }
    formal_trees = {
        variant: _git(formal[variant], "rev-parse", "HEAD^{tree}")
        for variant in ("fixed", "buggy")
    }
    devtest_git_trees = {
        variant: _git(formal[variant], "ls-tree", "HEAD", "games/devtest").split()[2]
        for variant in ("fixed", "buggy")
    }
    if any(head != _HEAD_SHA for head in formal_heads.values()):
        raise RuntimeError("formal pair is not the registered PR head")
    if any(tree != _DEVTEST_TREE_SHA for tree in devtest_git_trees.values()):
        raise RuntimeError("formal devtest tree drifted")

    binary_lineage: dict[str, dict[str, object]] = {}
    resource_lineage: dict[str, dict[str, object]] = {}
    static_checks: dict[str, dict[str, object]] = {}
    for variant in ("fixed", "buggy"):
        formal_app = formal[variant] / "build-app/out/minetest.app"
        formal_binary = formal_app / "Contents/MacOS/minetest"
        v2_binary = v2_apps[variant] / "Contents/MacOS/minetest"
        v3_binary = v3_apps[variant] / "Contents/MacOS/minetest"
        formal_uuid = _uuid(formal_binary)
        v2_uuid = _uuid(v2_binary)
        v3_uuid = _uuid(v3_binary)
        if not (formal_uuid == v2_uuid == v3_uuid):
            raise RuntimeError(f"{variant} binary UUID lineage mismatch")
        binary_lineage[variant] = {
            "formal_build_binary_uuid": formal_uuid,
            "repackage_v2_binary_uuid": v2_uuid,
            "repackage_v3_binary_uuid": v3_uuid,
            "uuid_match": True,
        }

        formal_misc = formal_app / "Contents/Resources/builtin/common/misc_helpers.lua"
        v2_misc = (
            v2_apps[variant] / "Contents/Resources/builtin/common/misc_helpers.lua"
        )
        v3_misc = (
            v3_apps[variant] / "Contents/Resources/builtin/common/misc_helpers.lua"
        )
        misc_hashes = {
            "formal": _sha256(formal_misc),
            "repackage_v2": _sha256(v2_misc),
            "repackage_v3": _sha256(v3_misc),
        }
        if len(set(misc_hashes.values())) != 1:
            raise RuntimeError(f"{variant} production resource lineage mismatch")

        source_devtest = formal[variant] / "games/devtest"
        v3_devtest = v3_apps[variant] / "Contents/Resources/games/devtest"
        _ordinary_directory(source_devtest, root)
        _ordinary_directory(v3_devtest, root)
        src_tree = _canonical_tree_hash(source_devtest)
        dst_tree = _canonical_tree_hash(v3_devtest)
        file_count = sum(1 for path in source_devtest.rglob("*") if path.is_file())
        if src_tree != dst_tree or file_count != 536:
            raise RuntimeError(f"{variant} devtest resource copy drifted")
        resource_lineage[variant] = {
            "formal_git_tree": devtest_git_trees[variant],
            "formal_canonical_tree": src_tree,
            "repackage_v3_canonical_tree": dst_tree,
            "file_count": file_count,
            "byte_count": sum(
                path.stat().st_size
                for path in source_devtest.rglob("*")
                if path.is_file()
            ),
            "misc_helpers_hash": misc_hashes,
        }
        static_checks[variant] = _static_checks(v3_apps[variant])

    payload: dict[str, object] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "static_packaging_qualified",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "admission_gate_1_7_status": "qualified",
        "candidate_execution_performed": False,
        "registered_replay_executed": False,
        "semantic_observations_complete": False,
        "goldens_complete": False,
        "visual_cross_matrix_complete": False,
        "same_lineage_construction_receipt_complete": True,
        "source_pair_status": "complete_launch_unverified",
        "fresh_copy_status": "succeeded",
        "fresh_namespace": "private/candidate_downstream/luanti/14757/repackage-v3",
        "binding_application_fixed_path": "private/candidate_downstream/luanti/14757/repackage-v3/fixed-minetest.app",
        "binding_application_buggy_path": "private/candidate_downstream/luanti/14757/repackage-v3/buggy-minetest.app",
        "original_pair_modified": False,
        "old_attempts_loaded": False,
        "inherit_prior_attempts": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "do_not_touch_pids": list(_PROTECTED_PIDS),
        "downstream_start_admission": {
            "static_packaging_closed": True,
            "start_executed": False,
            "gate_9_claimed": False,
            "registered_replay_executed": False,
            "requires_separate_fresh_start_namespace": True,
            "status": "eligible_for_new_start_construction",
        },
        "lineage": {
            "pr_head": _HEAD_SHA,
            "pr_base": source_pull["base"]["sha"],
            "pr_merge_commit": source_pull["merge_commit_sha"],
            "formal_git_heads": formal_heads,
            "formal_git_trees": formal_trees,
            "devtest_git_trees": devtest_git_trees,
            "binary": binary_lineage,
            "resources": resource_lineage,
        },
        "static_checks": static_checks,
        "missing_required_evidence": [
            "registered_start_and_ordered_replay",
            "hidden_oracle_and_real_semantic_observations",
            "common_before_fixed_after_and_buggy_after_goldens",
            "before_visual_equivalence_and_after_2x2_cross_judgment",
        ],
    }
    inputs = (
        gate.reference,
        transition.reference,
        mapping.reference,
        v2.reference,
        source.reference,
        capsule.reference,
    )
    write_stable_artifact(
        root,
        artifact_id=_RECEIPT.artifact_id,
        artifact_kind=_RECEIPT.artifact_kind,
        semantic_version=_RECEIPT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _RECEIPT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("repackage-v3 receipt typed reload failed")
    return artifact


def _validate_admission(
    gate: StableArtifact,
    transition: StableArtifact,
    mapping: StableArtifact,
) -> None:
    if (
        gate.payload.get("candidate") != _CANDIDATE
        or gate.payload.get("qualified") is not True
    ):
        raise RuntimeError("current Gate 1-7 aggregate is not qualified")
    if (
        transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("qualified") is not True
    ):
        raise RuntimeError("current transition is not qualified")
    if (
        mapping.payload.get("candidate") != _CANDIDATE
        or mapping.payload.get("complete") is not True
    ):
        raise RuntimeError("current mapping is not complete")


def _validate_source(
    root: Path, source: StableArtifact, capsule: StableArtifact
) -> dict[str, object]:
    raw_path = source.payload.get("raw_evidence_path")
    if not isinstance(raw_path, str):
        raise RuntimeError("candidate source manifest path is not registered")
    source_manifest = root / PurePosixPath(raw_path)
    try:
        manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("candidate source manifest is unavailable") from error
    pull = manifest.get("pull")
    if (
        source.payload.get("candidate") != _CANDIDATE
        or not isinstance(pull, dict)
        or pull.get("head", {}).get("sha") != _HEAD_SHA
        or capsule.inputs != (source.reference,)
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/14757/fixed-source-v2"
    ):
        raise RuntimeError("candidate source/capsule lineage is not registered")
    return pull


def _static_checks(app: Path) -> dict[str, object]:
    verify = subprocess.run(
        ("codesign", "--verify", "--strict", "--deep", "--verbose=4", str(app)),
        capture_output=True,
        text=True,
        check=False,
    )
    display = subprocess.run(
        ("codesign", "--display", "--verbose=4", str(app)),
        capture_output=True,
        text=True,
        check=False,
    )
    otool = subprocess.run(
        ("otool", "-L", str(app / "Contents/MacOS/minetest")),
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode or display.returncode or otool.returncode:
        raise RuntimeError(f"static checks failed for {app.name}")
    return {
        "codesign_verify_strict_deep": {
            "returncode": verify.returncode,
            "status": "passed",
        },
        "codesign_display_verbose": {
            "returncode": display.returncode,
            "status": "passed",
            "candidate_cdhash": _line_value(display.stdout, "CandidateCDHash sha256="),
            "sealed_resource_files": _line_value(
                display.stdout, "Sealed Resources version=2 rules=13 files="
            ),
        },
        "otool_dependency_listing": {
            "returncode": otool.returncode,
            "status": "passed",
            "dependency_count": max(0, len(otool.stdout.splitlines()) - 1),
        },
    }


def _line_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        if prefix in line:
            return line.split(prefix, 1)[1].strip()
    return None


def _canonical_tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uuid(path: Path) -> str:
    result = subprocess.run(
        ("otool", "-l", str(path)), capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"cannot inspect UUID for {path}")
    match = _UUID_RE.search(result.stdout)
    if match is None:
        raise RuntimeError(f"missing UUID for {path}")
    return match.group(1).upper()


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(cwd), *argv),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git lineage probe failed: {' '.join(argv)}")
    return result.stdout.strip()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_root(path: Path) -> Path:
    return _ordinary_directory(path, path)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation-execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14757_repackage_v3_receipt"]
