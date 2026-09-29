"""Offline fixed/buggy source materialization for JupyterLab candidates."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .jupyterlab_3703 import JupyterLab3703Preparation
from .jupyterlab_regression import JupyterLabSourceMaterializationPlan
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "MATERIALIZE_JUPYTERLAB_FIXED_BUGGY_V1"
_SUPPORTED = frozenset({444, 574, 611, 1250, 2437, 2469, 2752, 3310, 3662, 3703, 3867})


@dataclass(frozen=True)
class CandidateSourcePlan:
    candidate: CandidateKey
    profile_id: str
    fixed_capsule_reference: ArtifactReference
    fixed_source_tree: Path
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    reverse_patch: bytes
    apply_argv: tuple[str, ...]
    runtime_dependency_id: str
    runtime_root: str

    def __post_init__(self) -> None:
        if (
            self.candidate.repository_key != "jupyterlab"
            or self.candidate.change_number not in _SUPPORTED
            or not self.profile_id
            or not self.production_paths
            or not self.test_paths
            or not self.reverse_patch
            or self.apply_argv
            != (
                "/usr/bin/git",
                "apply",
                "-R",
                "--whitespace=nowarn",
                "-",
            )
            or not self.runtime_dependency_id
            or not self.runtime_root
        ):
            raise ValueError("JupyterLab candidate source plan is invalid")
        for relative in (*self.production_paths, *self.test_paths):
            _safe_relative(relative)


def source_plan_from_regression(
    plan: JupyterLabSourceMaterializationPlan,
) -> CandidateSourcePlan:
    if type(plan) is not JupyterLabSourceMaterializationPlan:
        raise ValueError("JupyterLab regression materialization plan is invalid")
    return CandidateSourcePlan(
        candidate=plan.profile.candidate,
        profile_id=plan.profile.profile_id,
        fixed_capsule_reference=plan.profile.fixed_capsule_reference,
        fixed_source_tree=plan.fixed_capsule.source_tree,
        production_paths=plan.profile.production_paths,
        test_paths=plan.profile.patch_test_paths,
        reverse_patch=plan.production_inverse.patch,
        apply_argv=tuple(str(value) for value in plan.production_inverse.apply_argv),
        runtime_dependency_id=plan.profile.runtime_dependency_id,
        runtime_root=plan.profile.runtime_root,
    )


def source_plan_from_3703(plan: JupyterLab3703Preparation) -> CandidateSourcePlan:
    if type(plan) is not JupyterLab3703Preparation:
        raise ValueError("JupyterLab 3703 materialization plan is invalid")
    from .jupyterlab_3703 import PROFILE

    return CandidateSourcePlan(
        candidate=plan.candidate,
        profile_id=plan.profile_id,
        fixed_capsule_reference=PROFILE.fixed_capsule_reference,
        fixed_source_tree=plan.fixed_source_tree,
        production_paths=PROFILE.production_paths,
        test_paths=PROFILE.patch_test_paths,
        reverse_patch=plan.production_reverse_patch,
        apply_argv=(
            "/usr/bin/git",
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ),
        runtime_dependency_id=PROFILE.runtime_dependency_id,
        runtime_root=PROFILE.runtime_root,
    )


def source_plan_from_top(
    plan: object, *, artifact_root: str | Path
) -> CandidateSourcePlan:
    """Adapt a validated top-profile preparation without importing it globally."""

    from .jupyterlab_top_profiles import TopPreparation

    if type(plan) is not TopPreparation:
        raise ValueError("JupyterLab top materialization plan is invalid")
    profile = plan.profile
    return CandidateSourcePlan(
        candidate=profile.candidate,
        profile_id=profile.profile_id,
        fixed_capsule_reference=profile.capsule_reference,
        fixed_source_tree=(
            Path(artifact_root).absolute() / profile.fixed_source_tree_path
        ),
        production_paths=profile.production_paths,
        test_paths=profile.patch_test_paths,
        reverse_patch=plan.inverse_patch,
        apply_argv=(
            "/usr/bin/git",
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ),
        runtime_dependency_id=profile.runtime_dependency_id,
        runtime_root=profile.runtime_root,
    )


def materialization_reference(candidate: CandidateKey) -> ArtifactReference:
    _require_candidate(candidate)
    return ArtifactReference(
        f"candidate_materialization/jupyterlab/{candidate.change_number}/fixed-buggy/v1",
        "candidate_materialization",
        "1.0",
    )


def downstream_plan_reference(candidate: CandidateKey) -> ArtifactReference:
    _require_candidate(candidate)
    return ArtifactReference(
        f"candidate_downstream_plan/jupyterlab/{candidate.change_number}/preannotation/v1",
        "candidate_downstream_plan",
        "1.0",
    )


def materialize_fixed_and_buggy(
    *,
    artifact_root: str | Path,
    plan: CandidateSourcePlan,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Create two complete source trees and retain the patched tests in both."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if type(plan) is not CandidateSourcePlan:
        raise ValueError("JupyterLab candidate source plan is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("JupyterLab materialization authorization is invalid")
    if authorization.scope != "version-materialization":
        raise PermissionError(
            "JupyterLab materialization authorization scope is invalid"
        )
    root = _ordinary_root(Path(artifact_root).absolute())
    capsule = load_stable_artifact(
        root,
        artifact_id=plan.fixed_capsule_reference.artifact_id,
        artifact_kind=plan.fixed_capsule_reference.artifact_kind,
        semantic_version=plan.fixed_capsule_reference.semantic_version,
    )
    if capsule.payload.get("candidate") != plan.candidate.to_value():
        raise RuntimeError("JupyterLab fixed capsule identity is invalid")
    source = _ordinary_tree(plan.fixed_source_tree)
    source.relative_to(root)
    before = _tree_inventory(source)
    for relative in (*plan.production_paths, *plan.test_paths):
        if relative not in before:
            raise RuntimeError("JupyterLab materialization source is incomplete")

    base_relative = PurePosixPath(
        f"private/materialized/jupyterlab/{plan.candidate.change_number}/v1"
    )
    parent = _ordinary_parent(root, PurePosixPath(*base_relative.parts[:-1]))
    destination = parent / base_relative.name
    if destination.exists() or destination.is_symlink():
        return _resume(root, plan, capsule, destination)

    staging = Path(tempfile.mkdtemp(prefix=".jupyterlab-materialize-", dir=parent))
    try:
        fixed = staging / "fixed"
        buggy = staging / "buggy"
        shutil.copytree(source, fixed, symlinks=False)
        shutil.copytree(source, buggy, symlinks=False)
        if _tree_inventory(fixed) != before or _tree_inventory(buggy) != before:
            raise RuntimeError("JupyterLab source copy is incomplete")
        _apply_inverse(buggy, plan)
        after = _tree_inventory(buggy)
        changed = tuple(
            sorted(
                path
                for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
        )
        if changed != tuple(sorted(plan.production_paths)):
            raise RuntimeError("JupyterLab inverse patch changed non-production files")
        for test_path in plan.test_paths:
            if after.get(test_path) != before[test_path]:
                raise RuntimeError("JupyterLab inverse patch changed fixed tests")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            _remove_tree(staging)

    payload = _payload(plan, base_relative)
    reference = materialization_reference(plan.candidate)
    created_at = _created_after(capsule.created_at, authorization.granted_at)
    try:
        write_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
            created_at=created_at,
            inputs=(capsule.reference,),
            payload=payload,
            authorization=authorization,
        )
        artifact = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except BaseException:
        _remove_tree(destination)
        raise
    if artifact.inputs != (capsule.reference,) or artifact.payload != payload:
        _remove_tree(destination)
        raise RuntimeError("JupyterLab materialization typed reload is invalid")
    return artifact


def write_active_downstream_plan(
    *,
    artifact_root: str | Path,
    plan: CandidateSourcePlan,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist runtime wait as active work, never as candidate rejection."""

    if type(plan) is not CandidateSourcePlan:
        raise ValueError("JupyterLab candidate source plan is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("JupyterLab downstream authorization is invalid")
    if authorization.scope != "preannotation-planning":
        raise PermissionError("JupyterLab downstream authorization scope is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    materialization_ref = materialization_reference(plan.candidate)
    materialization = load_stable_artifact(
        root,
        artifact_id=materialization_ref.artifact_id,
        artifact_kind=materialization_ref.artifact_kind,
        semantic_version=materialization_ref.semantic_version,
    )
    runtime_ref = ArtifactReference(
        f"dependency_preparation/{plan.runtime_dependency_id}/current/v1",
        "dependency_preparation",
        "1.0",
    )
    runtime = load_stable_artifact(
        root,
        artifact_id=runtime_ref.artifact_id,
        artifact_kind=runtime_ref.artifact_kind,
        semantic_version=runtime_ref.semantic_version,
    )
    if (
        materialization.payload.get("candidate") != plan.candidate.to_value()
        or materialization.payload.get("candidate_lifecycle") != "active"
        or runtime.payload.get("complete") is not True
        or runtime.payload.get("dependency_id") != plan.runtime_dependency_id
        or runtime.payload.get("runtime_root") != plan.runtime_root
        or runtime.payload.get("outcome")
        not in {"ready", "pending", "infrastructure_blocked"}
    ):
        raise RuntimeError("JupyterLab downstream inputs are invalid")
    ready = runtime.payload["outcome"] == "ready"
    payload = {
        "candidate": plan.candidate.to_value(),
        "profile_id": plan.profile_id,
        "complete": True,
        "terminal": False,
        "candidate_lifecycle": "active",
        "readiness_state": "ready" if ready else "runtime_dependency_pending",
        "completed_stage": "version_materialization",
        "next_stage": "test_repetitions",
        "runtime_dependency_id": plan.runtime_dependency_id,
        "runtime_root": plan.runtime_root,
        "pending_components": []
        if ready
        else list(runtime.payload.get("missing_components", [])),
        "mismatched_components": []
        if ready
        else list(runtime.payload.get("mismatched_components", [])),
        "pending_action": None
        if ready
        else "construct_registered_historical_runtime_closure",
    }
    reference = downstream_plan_reference(plan.candidate)
    created_at = _created_after(
        materialization.created_at,
        runtime.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=(materialization.reference, runtime.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if (
        artifact.inputs != (materialization.reference, runtime.reference)
        or artifact.payload != payload
    ):
        raise RuntimeError("JupyterLab downstream plan typed reload is invalid")
    return artifact


def _resume(
    root: Path,
    plan: CandidateSourcePlan,
    capsule: StableArtifact,
    destination: Path,
) -> StableArtifact:
    reference = materialization_reference(plan.candidate)
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    base = PurePosixPath(
        f"private/materialized/jupyterlab/{plan.candidate.change_number}/v1"
    )
    if (
        artifact.inputs != (capsule.reference,)
        or artifact.payload != _payload(plan, base)
        or _tree_inventory(destination / "fixed")
        != _tree_inventory(plan.fixed_source_tree)
    ):
        raise RuntimeError("JupyterLab materialization resume is invalid")
    buggy = _tree_inventory(destination / "buggy")
    fixed = _tree_inventory(destination / "fixed")
    changed = tuple(
        sorted(
            path
            for path in set(fixed) | set(buggy)
            if fixed.get(path) != buggy.get(path)
        )
    )
    if changed != tuple(sorted(plan.production_paths)):
        raise RuntimeError("JupyterLab materialization resume is invalid")
    return artifact


def _payload(plan: CandidateSourcePlan, base: PurePosixPath) -> dict[str, object]:
    return {
        "candidate": plan.candidate.to_value(),
        "profile_id": plan.profile_id,
        "complete": True,
        "candidate_lifecycle": "active",
        "downstream_state": "runtime_dependency_pending",
        "fixed_tree_path": f"{base.as_posix()}/fixed",
        "buggy_tree_path": f"{base.as_posix()}/buggy",
        "production_paths": list(plan.production_paths),
        "preserved_test_paths": list(plan.test_paths),
        "runtime_dependency_id": plan.runtime_dependency_id,
        "runtime_root": plan.runtime_root,
        "next_stage": "test_repetitions",
    }


def _apply_inverse(tree: Path, plan: CandidateSourcePlan) -> None:
    environment = {
        "HOME": str(tree / ".closed-home"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tree.parent),
    }
    try:
        completed = subprocess.run(
            plan.apply_argv,
            cwd=tree,
            input=plan.reverse_patch,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("JupyterLab production inverse failed") from error
    if completed.returncode != 0:
        raise RuntimeError("JupyterLab production inverse failed")


def _tree_inventory(root: Path) -> dict[str, tuple[bytes, int]]:
    root = _ordinary_tree(root)
    result: dict[str, tuple[bytes, int]] = {}
    for path in root.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise RuntimeError("JupyterLab source tree contains an unsafe entry")
        if stat.S_ISREG(info.st_mode):
            result[relative] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
    return result


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError("JupyterLab materialization path is unsafe")
    return path


def _ordinary_root(root: Path) -> Path:
    return _ordinary_tree(root)


def _ordinary_tree(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("JupyterLab materialization tree is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("JupyterLab materialization tree is unsafe")
    return resolved


def _ordinary_parent(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("JupyterLab materialization parent is unsafe")
    return current.resolve(strict=True)


def _remove_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


def _created_after(*values: str) -> str:
    latest = max(
        datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values
    )
    return (
        (latest + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_candidate(candidate: CandidateKey) -> None:
    if (
        type(candidate) is not CandidateKey
        or candidate.repository_key != "jupyterlab"
        or candidate.change_number not in _SUPPORTED
    ):
        raise ValueError("candidate has no JupyterLab materialization profile")


__all__ = [
    "CONFIRMATION",
    "CandidateSourcePlan",
    "downstream_plan_reference",
    "materialization_reference",
    "materialize_fixed_and_buggy",
    "source_plan_from_3703",
    "source_plan_from_regression",
    "source_plan_from_top",
    "write_active_downstream_plan",
]
