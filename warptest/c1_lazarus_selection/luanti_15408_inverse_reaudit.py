"""Fresh namespace inverse re-audit for Luanti #15408.

The previous downstream materialization was blocked after observing an empty
changed-path set.  This module deliberately uses a new pair namespace and a
new typed receipt.  It only copies the candidate's fixed capsule and applies
the registered production hunk in reverse; it never loads or resumes the
previous pair or any native attempt.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_15408_downstream_closure import (
    _CAPSULE_REL,
    _CLOSURE,
    _PATCH,
    _CAPSULE,
    _CANDIDATE,
    _MATERIALIZATION_BLOCKER,
    _PROFILE_ID,
    _tree_manifest,
    _manifest_fingerprint,
    _patch_sections,
    _upstream,
    _load,
    _try_load,
    _copy_capsule,
    _require_auth,
    _root,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    write_stable_artifact,
)


_PAIR_REL = PurePosixPath("private/materialized/luanti/15408/application-recovery-v3")
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15408/fixed-buggy/recovery-v3",
    "candidate_materialization",
    "3.0",
)
_PREFLIGHT = ArtifactReference(
    "candidate_gate8_preflight/luanti/15408/recovery-v3",
    "candidate_gate8_preflight",
    "1.0",
)
_PREFLIGHT_CONFIGURE = ArtifactReference(
    "candidate_gate8_preflight/luanti/15408/recovery-v3-configure",
    "candidate_gate8_preflight",
    "1.0",
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_or_check(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#15408 {reference.artifact_id} conflicts")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#15408 {reference.artifact_id} typed reload failed")
    return artifact


def _pair_delta(fixed: Path, buggy: Path) -> tuple[str, ...]:
    fixed_entries = _tree_manifest(fixed)
    buggy_entries = _tree_manifest(buggy)
    fixed_map = {item["path"]: item for item in fixed_entries}
    buggy_map = {item["path"]: item for item in buggy_entries}
    if set(fixed_map) != set(buggy_map):
        raise RuntimeError("#15408 recovery-v3 changed tree membership")
    return tuple(path for path in sorted(fixed_map) if fixed_map[path] != buggy_map[path])


def materialize_reaudit(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Materialize a fresh fixed/buggy pair and prove the inverse delta."""

    _require_auth(authorization, "protocol-migration")
    root = _root(artifact_root)
    chain = _upstream(root)
    closure = _load(root, _CLOSURE)
    if closure.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#15408 closure is not candidate-bound")

    base = root / _PAIR_REL
    existing = _try_load(root, _MATERIALIZATION)
    if existing is not None:
        _validate_pair(root, existing)
        return existing
    reused_pair = False
    if base.exists() or base.is_symlink():
        if base.is_symlink() or not (base / "fixed").is_dir() or not (base / "buggy").is_dir():
            raise RuntimeError("#15408 recovery-v3 pair exists without typed receipt")
        fixed = base / "fixed"
        buggy = base / "buggy"
        if _pair_delta(fixed, buggy) != ("src/collision.cpp",):
            raise RuntimeError("#15408 recovery-v3 existing pair has an invalid inverse delta")
        before = _sha256(fixed / "src/collision.cpp")
        after = _sha256(buggy / "src/collision.cpp")
        if before == after:
            raise RuntimeError("#15408 recovery-v3 existing pair has no production delta")
        reused_pair = True

    capsule = root / _CAPSULE_REL
    if not capsule.is_dir() or capsule.is_symlink():
        raise RuntimeError("#15408 candidate fixed capsule is unavailable")
    raw_patch_path = chain["patch"].payload.get("raw_patch_path")
    if not isinstance(raw_patch_path, str):
        raise RuntimeError("#15408 registered raw patch path is invalid")
    sections = _patch_sections((root / PurePosixPath(raw_patch_path)).read_bytes())
    if tuple(path for path, _ in sections) != (
        "src/collision.cpp",
        "src/unittest/test_collision.cpp",
    ):
        raise RuntimeError("#15408 registered patch scope drifted")

    if not reused_pair:
        staging = Path(tempfile.mkdtemp(prefix=".luanti-15408-recovery-v3-", dir=root / "private"))
        try:
            fixed = staging / "fixed"
            buggy = staging / "buggy"
            _copy_capsule(capsule, fixed)
            _copy_capsule(capsule, buggy)
            production = fixed / "src/collision.cpp"
            before = _sha256(production)
            # The artifact root lives below the parent StatePortal Git worktree.
            # Without a ceiling, git apply silently reports "Skipped patch" for
            # the untracked nested pair and returns 0, which caused the old
            # changed=() observation.  Force patch path resolution to this
            # candidate namespace rather than the parent repository.
            git_env = os.environ.copy()
            git_env["GIT_CEILING_DIRECTORIES"] = str(root)
            applied = subprocess.run(
                ("git", "apply", "-R", "--whitespace=nowarn", "-"),
                cwd=buggy,
                input=sections[0][1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
                env=git_env,
            )
            if applied.returncode != 0:
                raise RuntimeError(
                    "#15408 recovery-v3 inverse failed: "
                    + applied.stderr.decode("utf-8", errors="replace")[-2000:]
                )
            changed = _pair_delta(fixed, buggy)
            expected = ("src/collision.cpp",)
            if changed != expected:
                raise RuntimeError(
                    f"#15408 recovery-v3 inverse delta is {changed!r}, expected {expected!r}"
                )
            after = _sha256(buggy / "src/collision.cpp")
            if before == after:
                raise RuntimeError("#15408 recovery-v3 inverse did not alter production")
            base.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(staging, base)
            fixed = base / "fixed"
            buggy = base / "buggy"
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "state": "materialized",
        "outcome": "fresh_candidate_owned_fixed_buggy_pair",
        "complete": True,
        "namespace": "application-recovery-v3",
        "fixed_tree_path": (_PAIR_REL / "fixed").as_posix(),
        "buggy_tree_path": (_PAIR_REL / "buggy").as_posix(),
        "fixed_tree_fingerprint": _manifest_fingerprint(_tree_manifest(fixed)),
        "buggy_tree_fingerprint": _manifest_fingerprint(_tree_manifest(buggy)),
        "production_inverse": {
            "operation": "git apply -R --whitespace=nowarn",
            "paths": ["src/collision.cpp"],
            "tests_preserved": True,
            "support_paths_touched": False,
            "fixed_production_fingerprint": before,
            "buggy_production_fingerprint": after,
            "observed_changed_paths": ["src/collision.cpp"],
        },
        "capsule_owned_irrlicht_closure": closure.reference.artifact_id,
        "candidate_execution_performed": False,
        "build_started": False,
        "start_replay_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        chain["source"].reference,
        chain["patch"].reference,
        chain["atomicity"].reference,
        chain["manifest"].reference,
        chain["capsule"].reference,
        chain["transition"].reference,
        chain["aggregate"].reference,
        closure.reference,
    )
    return _write_or_check(
        root,
        _MATERIALIZATION,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )


def _validate_pair(root: Path, artifact: StableArtifact) -> None:
    if artifact.payload.get("state") != "materialized" or artifact.payload.get("complete") is not True:
        raise RuntimeError("#15408 recovery-v3 materialization is invalid")
    if artifact.payload.get("production_inverse", {}).get("observed_changed_paths") != ["src/collision.cpp"]:
        raise RuntimeError("#15408 recovery-v3 inverse receipt is invalid")
    for variant in ("fixed", "buggy"):
        value = artifact.payload.get(f"{variant}_tree_path")
        if not isinstance(value, str):
            raise RuntimeError("#15408 recovery-v3 tree path is invalid")
        tree = root / PurePosixPath(value)
        if not tree.is_dir() or tree.is_symlink():
            raise RuntimeError("#15408 recovery-v3 pair tree is unavailable")
        if any(path.is_symlink() for path in tree.rglob("*")):
            raise RuntimeError("#15408 recovery-v3 pair contains symlink")


def write_preflight(
    artifact_root: str | Path,
    *,
    materialization: StableArtifact,
    authorization: AuthorizationRecord,
    configure: dict[str, Any] | None = None,
    reference: ArtifactReference = _PREFLIGHT,
) -> StableArtifact:
    """Record Gate8 pair/readiness; configure is optional and never a build."""

    _require_auth(authorization, "infrastructure-audit")
    root = _root(artifact_root)
    _validate_pair(root, materialization)
    closure = _load(root, _CLOSURE)
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "state": "preflight_ready",
        "outcome": "candidate_owned_pair_and_irrlicht_closure_ready",
        "complete": True,
        "candidate_materialization": materialization.reference.artifact_id,
        "capsule_owned_irrlicht_closure": closure.reference.artifact_id,
        "gate8": {
            "fixed_buggy_pair": "passed",
            "production_inverse": "passed_src_collision_cpp_only",
            "candidate_owned_dependency": "passed_capsule_irrlicht",
            "mapping_fixture": "pending",
            "downstream_plan": "pending",
        },
        "configure_probe": configure or {"state": "not_run", "build_started": False},
        "gate9_to_13": "not_claimed",
        "candidate_execution_performed": False,
        "start_replay_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (materialization.reference, closure.reference)
    return _write_or_check(
        root, reference, inputs=inputs, payload=payload, authorization=authorization
    )


__all__ = ["materialize_reaudit", "write_preflight"]
