"""Bounded null-video client builds for Luanti candidate 15519."""

from __future__ import annotations

import base64
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .luanti_client_dependency_materializer import (
    load_luanti_client_dependency_closure,
)
from .luanti_native_executor import (
    NativeProcessReceipt,
    ProcessRunner,
    _apply_production_inverse,
    _copy_full_capsule,
    _validate_capsule,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


BUILD_CONFIRMATION = "EXECUTE_LUANTI_15519_NULL_CLIENT_BUILD"
_CANDIDATE = CandidateKey("luanti", 15519)
_VERSION = "2.0"
_MAX_OUTPUT = 262_144


@dataclass(frozen=True)
class NullClientBuildResult:
    aggregate: StableArtifact
    state: str
    executed_steps: int
    resumed_steps: int


Planner = Callable[..., NativeRegressionExecutionPlan | StageDecision]
ClosureLoader = Callable[[str | Path], StableArtifact]


def execute_luanti_15519_null_client_build(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    native_planner: Planner = plan_luanti_native_regression_execution,
    closure_loader: ClosureLoader = load_luanti_client_dependency_closure,
    process_runner: ProcessRunner = run_bounded_process,
) -> NullClientBuildResult:
    """Configure/build both null-client variants without launching either one."""

    if confirmation != BUILD_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {BUILD_CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    requested = Path(run_root).absolute()
    reserved = _reserve_run_root(root, requested)
    decision = native_planner(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"null-client build is blocked: {decision.details.get('reason')}"
        )
    plan = decision
    if plan.profile.candidate != _CANDIDATE:
        raise RuntimeError("null-client native plan identity is invalid")
    _validate_capsule(root, plan)
    try:
        closure = closure_loader(root)
    except RuntimeError as error:
        raise RuntimeError(
            "registered client dependency closure is unavailable"
        ) from error
    dependencies = _validate_client_closure(root, closure)
    trees = {variant: reserved / variant for variant in ("fixed", "buggy")}
    executed = 0
    resumed = 0
    receipts: list[StableArtifact] = []
    try:
        _copy_full_capsule(plan, trees["fixed"])
        _copy_full_capsule(plan, trees["buggy"])
        _apply_production_inverse(plan, trees["fixed"], trees["buggy"])
        for variant in ("fixed", "buggy"):
            environment = _closed_environment(plan, reserved, variant)
            for step, argv, timeout in (
                ("configure", _configure_argv(plan, dependencies), 600),
                ("build", _build_argv(plan), 1_800),
            ):
                reference = _step_reference(variant, step)
                previous = _try_load(root, reference)
                if previous is not None:
                    _validate_successful_step(
                        previous, plan, closure, variant, step, argv, timeout
                    )
                    receipts.append(previous)
                    resumed += 1
                    continue
                receipt = process_runner(
                    argv,
                    cwd=trees[variant],
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
                    authorization=authorization,
                )
                receipts.append(artifact)
                executed += 1
                if receipt.outcome != "completed" or receipt.returncode != 0:
                    aggregate = _write_aggregate(
                        root,
                        evidence=tuple(item.reference for item in receipts),
                        state="pending",
                        reason=f"{variant}_{step}_failed",
                        authorization=authorization,
                    )
                    return NullClientBuildResult(
                        aggregate, "pending", executed, resumed
                    )
        aggregate = _write_aggregate(
            root,
            evidence=tuple(item.reference for item in receipts),
            state="built",
            reason=None,
            authorization=authorization,
        )
        return NullClientBuildResult(aggregate, "built", executed, resumed)
    finally:
        for tree in trees.values():
            _remove_variant_tree(tree, reserved)


def _validate_client_closure(root: Path, closure: StableArtifact) -> dict[str, Path]:
    if (
        closure.reference
        != ArtifactReference(
            "client_dependency_closure/luanti/15519/darwin-arm64/v2",
            "client_dependency_closure",
            "2.0",
        )
        or closure.payload.get("candidate") != _CANDIDATE.to_value()
        or closure.payload.get("destination_root")
        != "private/dependencies/luanti-client/2.0"
        or closure.payload.get("state") != "materialized"
        or closure.payload.get("complete") is not True
    ):
        raise RuntimeError("registered client dependency closure is invalid")
    destination = root / "private/dependencies/luanti-client/2.0"
    values = {
        "freetype_include": destination / "packages/freetype/include/freetype2",
        "freetype_library": destination / "packages/freetype/lib/libfreetype.6.dylib",
        "jpeg_include": destination / "packages/jpeg/include",
        "jpeg_library": destination / "packages/jpeg/lib/libjpeg.8.dylib",
        "png_include": destination / "packages/png/include/libpng16",
        "png_library": destination / "packages/png/lib/libpng16.16.dylib",
        "opengl_include": destination / "packages/opengl/include",
        "opengl_library": destination / "packages/opengl/lib/OpenGL.tbd",
    }
    for name, path in values.items():
        if name.endswith("include"):
            _ordinary_directory(path, root)
        else:
            _ordinary_file(path, root)
    return values


def _configure_argv(
    plan: NativeRegressionExecutionPlan, dependencies: dict[str, Path]
) -> tuple[str, ...]:
    values = [str(value) for value in plan.configure_argv]
    values = [
        value
        for value in values
        if not value.startswith("-DBUILD_CLIENT=")
        and not value.startswith("-DBUILD_SERVER=")
    ]
    values.extend(
        (
            "-DBUILD_CLIENT=TRUE",
            "-DBUILD_SERVER=FALSE",
            f"-DFREETYPE_INCLUDE_DIRS={dependencies['freetype_include']}",
            f"-DFREETYPE_LIBRARY={dependencies['freetype_library']}",
            f"-DJPEG_INCLUDE_DIR={dependencies['jpeg_include']}",
            f"-DJPEG_LIBRARY={dependencies['jpeg_library']}",
            f"-DPNG_PNG_INCLUDE_DIR={dependencies['png_include']}",
            f"-DPNG_LIBRARY={dependencies['png_library']}",
            f"-DOPENGL_INCLUDE_DIR={dependencies['opengl_include']}",
            f"-DOPENGL_gl_LIBRARY={dependencies['opengl_library']}",
            "-DENABLE_SOUND=FALSE",
            "-DENABLE_CURSES=FALSE",
            "-DENABLE_OPENSSL=FALSE",
        )
    )
    return tuple(values)


def _build_argv(plan: NativeRegressionExecutionPlan) -> tuple[str, ...]:
    values = [str(value) for value in plan.build_argv]
    target = values.index("--target") + 1
    values[target] = "luanti"
    return tuple(values)


def _closed_environment(
    plan: NativeRegressionExecutionPlan, run_root: Path, variant: str
) -> dict[str, str]:
    home = run_root / "home" / variant
    home.mkdir(parents=True, mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }


def _step_reference(variant: str, step: str) -> ArtifactReference:
    return ArtifactReference(
        f"null_client_build_step/luanti/15519/{variant}/{step}/v2",
        "null_client_build_step",
        _VERSION,
    )


def _write_step(
    root: Path,
    *,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    timeout: int,
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "watchdog_seconds": timeout,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated
        or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated
        or len(receipt.stderr) > _MAX_OUTPUT,
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


def _validate_successful_step(
    artifact: StableArtifact,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    timeout: int,
) -> None:
    payload = artifact.payload
    if (
        artifact.inputs != (plan.profile.fixed_capsule_reference, closure.reference)
        or payload.get("candidate") != _CANDIDATE.to_value()
        or payload.get("variant") != variant
        or payload.get("step") != step
        or payload.get("argv") != list(argv)
        or payload.get("watchdog_seconds") != timeout
        or payload.get("process_outcome") != "completed"
        or payload.get("returncode") != 0
        or payload.get("complete") is not True
    ):
        raise RuntimeError("null-client build resume receipt is invalid")


def _write_aggregate(
    root: Path,
    *,
    evidence: tuple[ArtifactReference, ...],
    state: str,
    reason: str | None,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if state == "built" and len(evidence) != 4:
        raise RuntimeError("null-client build evidence coverage is incomplete")
    return _write(
        root,
        ArtifactReference(
            "null_client_build/luanti/15519/fixed-buggy/v2",
            "null_client_build",
            _VERSION,
        ),
        inputs=evidence,
        payload={
            "candidate": _CANDIDATE.to_value(),
            "state": state,
            "fixed_build": "passed" if state == "built" else "pending",
            "buggy_build": "passed" if state == "built" else "pending",
            "reason": reason,
            "downstream_state": "pending",
            "client_launched": False,
            "permanent_terminal_written": False,
            "paid_actions_authorized": False,
            "complete": True,
        },
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
        raise RuntimeError("null-client build typed reload failed")
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


def _reserve_run_root(root: Path, requested: Path) -> Path:
    expected_parent = root / "private/null-client-runs/luanti/15519"
    try:
        requested.relative_to(expected_parent)
    except ValueError as error:
        raise RuntimeError(
            "null-client run root is outside the registered root"
        ) from error
    if requested.parent != expected_parent or not requested.name:
        raise RuntimeError("null-client run root is invalid")
    _make_directory(expected_parent, root)
    try:
        requested.mkdir(mode=0o700)
    except FileExistsError as error:
        raise RuntimeError("null-client run root already exists") from error
    _ordinary_directory(requested, expected_parent)
    return requested


def _remove_variant_tree(tree: Path, run_root: Path) -> None:
    if tree.parent != run_root or tree.name not in {"fixed", "buggy"}:
        raise RuntimeError("refusing to remove an unsafe null-client tree")
    if not tree.exists() and not tree.is_symlink():
        return
    info = tree.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("null-client variant tree became unsafe")
    shutil.rmtree(tree)


def _make_directory(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("null-client directory is unsafe")


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(path: Path, root: Path) -> None:
    path.relative_to(root)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("registered client dependency directory is unsafe")


def _ordinary_file(path: Path, root: Path) -> None:
    path.relative_to(root)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("registered client dependency file is unsafe")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "BUILD_CONFIRMATION",
    "NullClientBuildResult",
    "execute_luanti_15519_null_client_build",
]
