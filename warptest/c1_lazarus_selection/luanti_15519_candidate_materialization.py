"""Candidate-specific persistent client materialization for Luanti #15519.

The older ``version_materialization`` receipt deliberately records that the
ephemeral null-client executor discarded its working trees.  This module is a
separate, opt-in boundary for rebuilding those trees *in place*.  It keeps a
single fixed/buggy source root, applies the production-only inverse to the
buggy copy, and (when explicitly requested) runs the registered configure and
build commands in those retained trees.  Every step and the final pair are
typed artifacts.  No terminal is ever written by this module.

The default is source-only preparation.  A real configure/build requires both
``execute_build=True`` and the explicit confirmation token; this makes tests
and the CLI safe to use without accidentally starting a native build.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .luanti_15519_null_client_build import (
    _apply_production_inverse,
    _build_argv,
    _configure_argv,
    _ordinary_file,
    _validate_client_closure,
)
from .luanti_native_executor import (
    NativeProcessReceipt,
    ProcessRunner,
    _validate_tree_links,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
)
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE_MATERIALIZATION_CONFIRMATION = (
    "MATERIALIZE_LUANTI_15519_PERSISTENT_CLIENT_PAIR_V1"
)
_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_VERSION = "1.0"
_BASE_RELATIVE = PurePosixPath("private/materialized/luanti/15519/null-client/v2")
_MATERIALIZATION_REFERENCE = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    _VERSION,
)
_STEP_KIND = "candidate_materialization_step"
_STEP_ROOT = "candidate_materialization_step/luanti/15519"


@dataclass(frozen=True)
class CandidateMaterializationResult:
    """Result of one source/build attempt at the registered pair root."""

    aggregate: StableArtifact
    state: Literal["pending", "built"]
    executed_steps: int
    resumed_steps: int


Planner = Callable[..., NativeRegressionExecutionPlan | StageDecision]
ClosureLoader = Callable[[str | Path], StableArtifact]


def materialize_luanti_15519_persistent_client_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
    execute_build: bool = False,
    native_planner: Planner = plan_luanti_native_regression_execution,
    closure_loader: ClosureLoader | None = None,
    process_runner: ProcessRunner = run_bounded_process,
) -> CandidateMaterializationResult:
    """Retain a fixed/buggy pair and optionally build both clients.

    ``execute_build=False`` only materializes the source pair and records a
    typed pending artifact.  With ``execute_build=True`` the four bounded
    steps are executed in the same retained trees; successful steps may be
    resumed, while failed steps are recorded as pending and never terminal.
    """

    if confirmation != CANDIDATE_MATERIALIZATION_CONFIRMATION:
        raise RuntimeError(
            f"explicit confirmation must be {CANDIDATE_MATERIALIZATION_CONFIRMATION!r}"
        )
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    decision = native_planner(root, candidate=_candidate_key())
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"Luanti 15519 materialization is blocked: {decision.details.get('reason')}"
        )
    plan = decision
    _validate_plan_identity(plan)
    if closure_loader is None:
        from .luanti_client_dependency_materializer import (
            load_luanti_client_dependency_closure,
        )

        closure_loader = load_luanti_client_dependency_closure
    closure = closure_loader(root)
    dependencies = _validate_client_closure(root, closure)
    capsule = _load_capsule(root, plan)
    base = _registered_base(root)

    existing = _try_load(root, _MATERIALIZATION_REFERENCE)
    if existing is not None:
        _validate_existing_aggregate(root, existing, plan, capsule, closure, base)
        state = existing.payload.get("state")
        if state == "built":
            _validate_built_outputs(root, existing)
            return CandidateMaterializationResult(existing, "built", 0, 4)
        _validate_source_pair(root, existing, base)
    elif base.exists() or base.is_symlink():
        raise RuntimeError(
            "registered materialization tree exists without a typed receipt"
        )
    else:
        _materialize_source_pair(plan, root, base)
        pending = _write_aggregate(
            root,
            plan=plan,
            capsule=capsule,
            closure=closure,
            base=base,
            state="pending",
            reason="build_not_requested",
            steps=(),
            authorization=authorization,
            build_requested=False,
        )
        existing = pending

    if not execute_build:
        return CandidateMaterializationResult(existing, "pending", 0, 0)

    receipts: list[StableArtifact] = []
    executed = 0
    resumed = 0
    for variant in ("fixed", "buggy"):
        tree = base / variant
        environment = _closed_environment(plan, base, variant)
        for step, argv, timeout in (
            ("configure", _configure_argv(plan, dependencies), 600),
            ("build", _build_argv(plan), 1_800),
        ):
            reference = _step_reference(variant, step)
            previous = _try_load(root, reference)
            if previous is not None and _step_is_successful(
                previous, plan, closure, variant, step, argv, timeout, base
            ):
                receipts.append(previous)
                resumed += 1
                continue
            receipt = process_runner(
                tuple(str(value) for value in argv),
                cwd=tree,
                env=environment,
                timeout_seconds=timeout,
            )
            artifact = _write_step(
                root,
                plan=plan,
                closure=closure,
                variant=variant,
                step=step,
                argv=argv,
                timeout=timeout,
                receipt=receipt,
                base=base,
                authorization=authorization,
            )
            receipts.append(artifact)
            executed += 1
            if receipt.outcome != "completed" or receipt.returncode != 0:
                aggregate = _write_aggregate(
                    root,
                    plan=plan,
                    capsule=capsule,
                    closure=closure,
                    base=base,
                    state="pending",
                    reason=f"{variant}_{step}_failed",
                    steps=tuple(item.reference for item in receipts),
                    authorization=authorization,
                    build_requested=True,
                )
                return CandidateMaterializationResult(
                    aggregate, "pending", executed, resumed
                )

    _validate_binaries(root, base)
    aggregate = _write_aggregate(
        root,
        plan=plan,
        capsule=capsule,
        closure=closure,
        base=base,
        state="built",
        reason=None,
        steps=tuple(item.reference for item in receipts),
        authorization=authorization,
        build_requested=True,
    )
    return CandidateMaterializationResult(aggregate, "built", executed, resumed)


# Short alias used by candidate-specific callers.
materialize_luanti_15519_client_pair = materialize_luanti_15519_persistent_client_pair


def load_luanti_15519_candidate_materialization(
    artifact_root: str | Path,
    *,
    native_planner: Planner = plan_luanti_native_regression_execution,
    closure_loader: ClosureLoader | None = None,
) -> StableArtifact:
    """Reload and validate the dedicated persistent-pair artifact."""

    root = _ordinary_root(Path(artifact_root).absolute())
    decision = native_planner(root, candidate=_candidate_key())
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"Luanti 15519 materialization is blocked: {decision.details.get('reason')}"
        )
    plan = decision
    _validate_plan_identity(plan)
    if closure_loader is None:
        from .luanti_client_dependency_materializer import (
            load_luanti_client_dependency_closure,
        )

        closure_loader = load_luanti_client_dependency_closure
    closure = closure_loader(root)
    _validate_client_closure(root, closure)
    capsule = _load_capsule(root, plan)
    base = _registered_base(root)
    artifact = _load(root, _MATERIALIZATION_REFERENCE)
    _validate_existing_aggregate(root, artifact, plan, capsule, closure, base)
    if artifact.payload.get("state") == "built":
        _validate_built_outputs(root, artifact)
    else:
        _validate_source_pair(root, artifact, base)
    return artifact


def materialization_reference() -> ArtifactReference:
    return _MATERIALIZATION_REFERENCE


def _candidate_key():
    from .models import CandidateKey

    return CandidateKey("luanti", 15519)


def _validate_plan_identity(plan: NativeRegressionExecutionPlan) -> None:
    if type(plan) is not NativeRegressionExecutionPlan:
        raise ValueError("Luanti 15519 native plan is invalid")
    if plan.profile.candidate != _candidate_key():
        raise ValueError("Luanti 15519 native plan candidate is invalid")
    if plan.profile.fixed_capsule_reference.artifact_id != (
        "fixed_source_capsule/luanti/15519/fixed/v2"
    ):
        raise ValueError("Luanti 15519 native capsule identity is invalid")


def _load_capsule(root: Path, plan: NativeRegressionExecutionPlan) -> StableArtifact:
    reference = plan.profile.fixed_capsule_reference
    capsule = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if (
        capsule.reference != reference
        or capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("source_tree_path")
        != plan.profile.fixed_source_tree_path
    ):
        raise RuntimeError("Luanti 15519 fixed capsule identity is invalid")
    source = Path(plan.fixed_capsule.source_tree)
    if source != root / PurePosixPath(plan.profile.fixed_source_tree_path):
        raise RuntimeError("Luanti 15519 fixed capsule is outside the registered root")
    _ordinary_tree(source, root)
    _validate_tree_links(source)
    return capsule


def _registered_base(root: Path) -> Path:
    parent = root
    for part in _BASE_RELATIVE.parts[:-1]:
        parent = parent / part
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("registered materialization parent is unsafe")
    base = parent / _BASE_RELATIVE.parts[-1]
    if base.is_symlink():
        raise RuntimeError("registered materialization root is unsafe")
    return base


def _materialize_source_pair(
    plan: NativeRegressionExecutionPlan, root: Path, base: Path
) -> None:
    if base.exists() or base.is_symlink():
        raise RuntimeError("registered materialization root already exists")
    parent = base.parent
    staging = Path(tempfile.mkdtemp(prefix=".luanti-15519-materialize-", dir=parent))
    try:
        fixed = staging / "fixed"
        buggy = staging / "buggy"
        source = plan.fixed_capsule.source_tree
        # symlinks=False dereferences the one safe capsule link; the retained
        # pair itself must contain ordinary files/directories only.
        shutil.copytree(source, fixed, symlinks=False)
        shutil.copytree(source, buggy, symlinks=False)
        _validate_no_symlink_tree(fixed)
        _validate_no_symlink_tree(buggy)
        _apply_production_inverse(plan, fixed, buggy)
        _validate_no_symlink_tree(fixed)
        _validate_no_symlink_tree(buggy)
        # Ensure only declared production files differ after the inverse.
        _validate_inverse_pair(plan, fixed, buggy)
        if base.exists() or base.is_symlink():
            raise RuntimeError("registered materialization root already exists")
        os.replace(staging, base)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _validate_source_pair(root: Path, artifact: StableArtifact, base: Path) -> None:
    if base.is_symlink() or not base.is_dir():
        raise RuntimeError("persistent Luanti client pair is missing")
    fixed = base / "fixed"
    buggy = base / "buggy"
    _validate_no_symlink_tree(fixed)
    _validate_no_symlink_tree(buggy)
    for path in (fixed, buggy):
        path.relative_to(root)
    if artifact.payload.get("fixed_tree_path") != (_BASE_RELATIVE / "fixed").as_posix():
        raise RuntimeError("materialization fixed tree path is invalid")
    if artifact.payload.get("buggy_tree_path") != (_BASE_RELATIVE / "buggy").as_posix():
        raise RuntimeError("materialization buggy tree path is invalid")


def _validate_binaries(root: Path, base: Path) -> None:
    for variant in ("fixed", "buggy"):
        binary = base / variant / "bin/luanti"
        _ordinary_file(binary, root)
        if not binary.stat().st_mode & stat.S_IXUSR:
            raise RuntimeError("persistent Luanti client binary is not executable")


def _validate_built_outputs(root: Path, artifact: StableArtifact) -> None:
    base = root / _BASE_RELATIVE
    _validate_source_pair(root, artifact, base)
    _validate_binaries(root, base)
    if artifact.payload.get("state") != "built":
        raise RuntimeError("materialization build state is invalid")
    if artifact.payload.get("missing_required_evidence") != []:
        raise RuntimeError("materialization build evidence is incomplete")


def _tree_inventory(root: Path) -> dict[str, tuple[str, str | None]]:
    inventory: dict[str, tuple[str, str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            inventory[relative] = ("directory", None)
        elif stat.S_ISREG(info.st_mode):
            inventory[relative] = ("file", None)
        elif stat.S_ISLNK(info.st_mode):
            inventory[relative] = ("symlink", os.readlink(path))
        else:
            raise RuntimeError("persistent Luanti client tree entry is unsafe")
    return inventory


def _validate_inverse_pair(
    plan: NativeRegressionExecutionPlan, fixed: Path, buggy: Path
) -> None:
    fixed_inventory = _tree_inventory(fixed)
    buggy_inventory = _tree_inventory(buggy)
    if set(fixed_inventory) != set(buggy_inventory):
        raise RuntimeError("Luanti 15519 source pair inventory diverged")
    production = set(plan.profile.production_paths)
    changed: set[str] = set()
    for relative in fixed_inventory:
        fixed_entry = fixed_inventory[relative]
        buggy_entry = buggy_inventory[relative]
        if fixed_entry[0] != buggy_entry[0]:
            raise RuntimeError("Luanti 15519 inverse changed source type")
        if fixed_entry[0] != "file":
            continue
        if not _files_equal(fixed / relative, buggy / relative):
            changed.add(relative)
    if changed != production:
        raise RuntimeError("Luanti 15519 inverse changed non-production files")


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(65_536)
            right_chunk = right_stream.read(65_536)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _validate_no_symlink_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("persistent Luanti client tree is unsafe")
    for child in path.rglob("*"):
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("persistent Luanti client tree contains a symlink")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError("persistent Luanti client tree entry is unsafe")


def _ordinary_tree(path: Path, root: Path) -> None:
    path.relative_to(root)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("registered source tree is unsafe")


def _validate_existing_aggregate(
    root: Path,
    artifact: StableArtifact,
    plan: NativeRegressionExecutionPlan,
    capsule: StableArtifact,
    closure: StableArtifact,
    base: Path,
) -> None:
    payload = artifact.payload
    if (
        artifact.reference != _MATERIALIZATION_REFERENCE
        or payload.get("candidate") != _CANDIDATE
        or payload.get("profile_id") != plan.profile.profile_id
        or payload.get("source_capsule_reference")
        != _reference_value(capsule.reference)
        or payload.get("client_dependency_reference")
        != _reference_value(closure.reference)
        or payload.get("base_path") != _BASE_RELATIVE.as_posix()
        or payload.get("run_root_path") != _BASE_RELATIVE.as_posix()
        or payload.get("permanent_terminal_written") is not False
        or payload.get("paid_actions_authorized") is not False
        or payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 candidate materialization binding is invalid")
    if payload.get("state") not in {"pending", "built"}:
        raise RuntimeError("Luanti 15519 candidate materialization state is invalid")
    expected_fixed = _BASE_RELATIVE / "fixed"
    expected_buggy = _BASE_RELATIVE / "buggy"
    if payload.get("fixed_tree_path") != expected_fixed.as_posix():
        raise RuntimeError("materialization fixed tree path is invalid")
    if payload.get("buggy_tree_path") != expected_buggy.as_posix():
        raise RuntimeError("materialization buggy tree path is invalid")
    if (
        payload.get("fixed_binary_path")
        != (_BASE_RELATIVE / "fixed/bin/luanti").as_posix()
        or payload.get("buggy_binary_path")
        != (_BASE_RELATIVE / "buggy/bin/luanti").as_posix()
    ):
        raise RuntimeError("materialization binary path is invalid")
    if base != root / _BASE_RELATIVE:
        raise RuntimeError("materialization base root drifted")
    step_values = payload.get("step_receipts")
    if type(step_values) is not list:
        raise RuntimeError("materialization step receipt list is invalid")
    step_references: list[ArtifactReference] = []
    for raw in step_values:
        step_references.append(_reference_from_value(raw))
    expected_inputs = (capsule.reference, closure.reference, *step_references)
    if artifact.inputs != expected_inputs:
        raise RuntimeError("materialization artifact lineage is invalid")
    expected_order = (
        _step_reference("fixed", "configure"),
        _step_reference("fixed", "build"),
        _step_reference("buggy", "configure"),
        _step_reference("buggy", "build"),
    )
    if payload.get("state") == "built" and tuple(step_references) != expected_order:
        raise RuntimeError("materialization step receipt coverage is invalid")
    for reference in step_references:
        step = _load(root, reference)
        _validate_step_identity(step, plan, closure, base)


def _step_reference(variant: str, step: str) -> ArtifactReference:
    if variant not in {"fixed", "buggy"} or step not in {"configure", "build"}:
        raise ValueError("materialization step identity is invalid")
    return ArtifactReference(f"{_STEP_ROOT}/{variant}/{step}/v1", _STEP_KIND, _VERSION)


def _step_is_successful(
    artifact: StableArtifact,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    variant: str,
    step: str,
    argv: tuple[Path | str, ...],
    timeout: int,
    base: Path,
) -> bool:
    payload = artifact.payload
    return (
        artifact.reference == _step_reference(variant, step)
        and artifact.inputs == (plan.profile.fixed_capsule_reference, closure.reference)
        and payload.get("candidate") == _CANDIDATE
        and payload.get("variant") == variant
        and payload.get("step") == step
        and payload.get("argv") == [str(value) for value in argv]
        and payload.get("watchdog_seconds") == timeout
        and payload.get("run_root_path") == _BASE_RELATIVE.as_posix()
        and payload.get("tree_path") == (_BASE_RELATIVE / variant).as_posix()
        and payload.get("binary_path")
        == (_BASE_RELATIVE / variant / "bin/luanti").as_posix()
        and payload.get("process_outcome") == "completed"
        and payload.get("returncode") == 0
        and payload.get("complete") is True
    )


def _validate_step_identity(
    artifact: StableArtifact,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    base: Path,
) -> None:
    parts = artifact.reference.artifact_id.split("/")
    if len(parts) != 6 or parts[:3] != [
        "candidate_materialization_step",
        "luanti",
        "15519",
    ]:
        raise RuntimeError("materialization step identity is invalid")
    variant, step = parts[3], parts[4]
    if variant not in {"fixed", "buggy"} or step not in {"configure", "build"}:
        raise RuntimeError("materialization step identity is invalid")
    if artifact.reference != _step_reference(variant, step):
        raise RuntimeError("materialization step identity is invalid")
    payload = artifact.payload
    if (
        artifact.inputs != (plan.profile.fixed_capsule_reference, closure.reference)
        or payload.get("candidate") != _CANDIDATE
        or payload.get("variant") != variant
        or payload.get("step") != step
        or payload.get("run_root_path") != _BASE_RELATIVE.as_posix()
        or payload.get("tree_path") != (_BASE_RELATIVE / variant).as_posix()
        or payload.get("binary_path")
        != (_BASE_RELATIVE / variant / "bin/luanti").as_posix()
        or payload.get("complete") is not True
    ):
        raise RuntimeError("materialization step binding is invalid")


def _write_step(
    root: Path,
    *,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    variant: str,
    step: str,
    argv: tuple[Path | str, ...],
    timeout: int,
    receipt: NativeProcessReceipt,
    base: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    stdout_truncated = receipt.stdout_truncated or len(receipt.stdout) > 262_144
    stderr_truncated = receipt.stderr_truncated or len(receipt.stderr) > 262_144
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "step": step,
        "argv": [str(value) for value in argv],
        "watchdog_seconds": timeout,
        "run_root_path": _BASE_RELATIVE.as_posix(),
        "tree_path": (_BASE_RELATIVE / variant).as_posix(),
        "binary_path": (_BASE_RELATIVE / variant / "bin/luanti").as_posix(),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": _b64(receipt.stdout),
        "stderr_base64": _b64(receipt.stderr),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_encoding": "base64",
        "complete": True,
    }
    return _write(
        root,
        _step_reference(variant, step),
        inputs=(plan.profile.fixed_capsule_reference, closure.reference),
        payload=payload,
        authorization=authorization,
    )


def _write_aggregate(
    root: Path,
    *,
    plan: NativeRegressionExecutionPlan,
    capsule: StableArtifact,
    closure: StableArtifact,
    base: Path,
    state: Literal["pending", "built"],
    reason: str | None,
    steps: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
    build_requested: bool,
) -> StableArtifact:
    expected_steps = (
        _step_reference("fixed", "configure"),
        _step_reference("fixed", "build"),
        _step_reference("buggy", "configure"),
        _step_reference("buggy", "build"),
    )
    if state == "built" and steps != expected_steps:
        raise RuntimeError("candidate materialization step coverage is incomplete")
    missing = []
    if state != "built":
        missing = [
            "fixed_client_binary",
            "buggy_client_binary",
            "fixed_buggy_build_receipts",
        ]
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": plan.profile.profile_id,
        "state": state,
        "outcome": None,
        "reason": reason,
        "base_path": _BASE_RELATIVE.as_posix(),
        "run_root_path": _BASE_RELATIVE.as_posix(),
        "fixed_tree_path": (_BASE_RELATIVE / "fixed").as_posix(),
        "buggy_tree_path": (_BASE_RELATIVE / "buggy").as_posix(),
        "fixed_binary_path": (_BASE_RELATIVE / "fixed/bin/luanti").as_posix(),
        "buggy_binary_path": (_BASE_RELATIVE / "buggy/bin/luanti").as_posix(),
        "source_capsule_reference": _reference_value(capsule.reference),
        "client_dependency_reference": _reference_value(closure.reference),
        "step_receipts": [_reference_value(reference) for reference in steps],
        "build_requested": build_requested,
        "missing_required_evidence": missing,
        "client_launch_performed": False,
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    inputs = (capsule.reference, closure.reference, *steps)
    return _write(
        root,
        _MATERIALIZATION_REFERENCE,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("candidate materialization typed reload failed")
    return artifact


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


def _reference_value(reference: ArtifactReference) -> dict[str, str]:
    return {
        "artifact_id": reference.artifact_id,
        "artifact_kind": reference.artifact_kind,
        "semantic_version": reference.semantic_version,
    }


def _reference_from_value(value: object) -> ArtifactReference:
    if type(value) is not dict or set(value) != {
        "artifact_id",
        "artifact_kind",
        "semantic_version",
    }:
        raise RuntimeError("materialization step reference is invalid")
    return ArtifactReference(
        value["artifact_id"], value["artifact_kind"], value["semantic_version"]
    )


def _b64(value: bytes) -> str:
    import base64

    return base64.b64encode(value[:262_144]).decode("ascii")


def _closed_environment(
    plan: NativeRegressionExecutionPlan, base: Path, variant: str
) -> dict[str, str]:
    home = base / ".home" / variant
    _mkdir_secure(home, base)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }


def _mkdir_secure(path: Path, root: Path) -> None:
    path.relative_to(root)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("materialization directory is unsafe")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope
        not in {"version-materialization", "preannotation-execution"}
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("candidate materialization authorization is required")


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CANDIDATE_MATERIALIZATION_CONFIRMATION",
    "CandidateMaterializationResult",
    "load_luanti_15519_candidate_materialization",
    "materialization_reference",
    "materialize_luanti_15519_client_pair",
    "materialize_luanti_15519_persistent_client_pair",
]
