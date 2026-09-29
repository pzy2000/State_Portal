"""Minimal current-v2 rendered-client repair for Luanti #14555.

The strict Gate 1--7 epoch already passed and must not be invalidated by a
downstream client problem.  This module therefore does only three things:

* strictly revalidates the existing fixed client with the candidate's trusted
  git metadata;
* rebuilds only the buggy client in a new candidate-specific build directory;
* binds both resulting clients (including the macOS app executables) to typed
  current-v2 receipts.

It never starts Luanti and never writes a formal-freeze admission.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal

from warptest.luanti_runtime import (
    _read_luanti_client_build_provenance,
    _write_luanti_client_build_provenance,
    luanti_client_source_identity,
    luanti_client_source_manifest,
)

from .luanti_native_executor import (
    NativeProcessReceipt,
    ProcessRunner,
    run_bounded_process,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14555_CURRENT_V2_CLIENT_REPAIR"
_CANDIDATE = {"repository_key": "luanti", "change_number": 14555}
_V1 = "1.0"
_V2 = "2.0"
_MAX_OUTPUT = 262_144
_TASK_ID = "lz-luanti-14555-negative-half-pointing"
_TREE_ROOT = Path("private/bounded-runs/luanti")
_TREES = {
    "fixed": _TREE_ROOT / "14555-gate57-fixed",
    "buggy": _TREE_ROOT / "14555-gate57-buggy",
}
_TOOLCHAIN = Path("private/dependencies/luanti-native/3.0/bin")
_CLIENT_BUILD_DIR = "build-client-current-v2-v1"
_CLIENT_MARKERS = (
    b"warptest_background_request_path",
    b"warptest_background_response_path",
    b"warptest_background_session_id",
    b"warptest_background_root",
    b"WARPTEST_C1_BACKGROUND_WINDOW",
)

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14555/source/v2", "candidate_source_packet", _V2
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14555/screening/v2", "candidate_test_patch", _V2
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14555/preannotation/v2", "source_atomicity", _V2
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14555/test_manifest/v2", "candidate_stage", _V2
)
_MATERIALIZATION = ArtifactReference(
    "candidate_stage/luanti/14555/version_materialization/v2", "candidate_stage", _V2
)
_PROFILE = ArtifactReference(
    "candidate_gate57_14555_fresh_profile/luanti/14555/v1",
    "candidate_gate57_14555_fresh_profile",
    _V1,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14555_fresh_aggregate/luanti/14555/v1",
    "candidate_gate57_14555_fresh_aggregate",
    _V1,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14555_fresh_transition/luanti/14555/v1",
    "candidate_gate57_14555_fresh_transition",
    _V1,
)
_LINEAGE = ArtifactReference(
    "candidate_gate57_14555_rendered_lineage_verdict/luanti/14555/v1",
    "candidate_gate57_14555_rendered_lineage_verdict",
    _V1,
)
_INPUTS = (
    _SOURCE,
    _PATCH,
    _ATOMICITY,
    _MANIFEST,
    _MATERIALIZATION,
    _PROFILE,
    _AGGREGATE,
    _TRANSITION,
    _LINEAGE,
)
_RECEIPT_KIND = "candidate_gate813_14555_client_binding"
_RECEIPTS = {
    variant: ArtifactReference(
        f"candidate_gate813_14555_client_binding/luanti/14555/{variant}/v1",
        _RECEIPT_KIND,
        _V1,
    )
    for variant in ("fixed", "buggy")
}
_CLOSURE = ArtifactReference(
    "candidate_gate813_14555_client_closure/luanti/14555/v1",
    "candidate_gate813_14555_client_closure",
    _V1,
)


@dataclass(frozen=True)
class ClientRepairResult:
    closure: StableArtifact
    fixed: StableArtifact
    buggy: StableArtifact
    rebuilt_buggy: bool


def repair_luanti_14555_clients(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    process_runner: ProcessRunner = run_bounded_process,
) -> ClientRepairResult:
    """Revalidate fixed and rebuild buggy without launching either client."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_directory(Path(artifact_root).absolute())
    workspace = _ordinary_directory(Path(workspace_root).absolute())
    artifacts = tuple(_load(root, reference) for reference in _INPUTS)
    _validate_inputs(artifacts)
    input_refs = tuple(item.reference for item in artifacts)

    fixed = _bind_fixed(root, workspace, input_refs, authorization)
    buggy = _bind_or_rebuild_buggy(
        root,
        workspace,
        input_refs,
        authorization,
        process_runner,
    )
    ready = all(
        item.payload.get("state") == "ready"
        and item.payload.get("client_provenance_ready") is True
        for item in (fixed, buggy)
    )
    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "ready" if ready else "downstream_pending",
        "outcome": "client_closure_ready" if ready else "infrastructure_blocked",
        "terminal": False,
        "resumable": not ready,
        "gate_1_7_preserved": True,
        "gate_1_7_qualified": True,
        "no_timeout_or_unknown": True,
        "old_attempts_loaded": False,
        "fixed_binding": fixed.reference.artifact_id,
        "buggy_binding": buggy.reference.artifact_id,
        "fixed_method": fixed.payload.get("method"),
        "buggy_method": buggy.payload.get("method"),
        "fresh_render_attempt_count": 0,
        "client_launched": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "next_action": (
            "run_one_fresh_candidate_specific_real_frame_epoch"
            if ready
            else "resume_only_the_failed_client_binding_step"
        ),
        "complete": True,
    }
    closure = _write(
        root,
        _CLOSURE,
        (fixed.reference, buggy.reference, *input_refs),
        payload,
        authorization,
    )
    return ClientRepairResult(
        closure=closure,
        fixed=fixed,
        buggy=buggy,
        rebuilt_buggy=buggy.payload.get("method") == "candidate_specific_rebuild",
    )


def _bind_fixed(
    root: Path,
    workspace: Path,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = _ordinary_directory(root / _TREES["fixed"], root)
    metadata = _metadata(workspace, "fixed")
    with _trusted_git(metadata):
        source_identity = luanti_client_source_identity(tree)
        manifest = luanti_client_source_manifest(tree)
        binary = _ordinary_file(tree / "bin/luanti", tree)
        sidecar = _ordinary_file(
            binary.with_name(binary.name + ".warptest-build.json"), tree
        )
        provenance = _read_luanti_client_build_provenance(tree, binary)
    if provenance is None:
        return _write_binding_blocker(
            root,
            variant="fixed",
            inputs=inputs,
            reason="fixed_existing_sidecar_failed_strict_candidate_metadata_revalidation",
            authorization=authorization,
        )
    if provenance.get("source_manifest") != manifest:
        raise RuntimeError("#14555 fixed provenance manifest drifted after validation")
    app = _bind_app_executable(tree, binary, sidecar)
    return _write_ready_binding(
        root,
        variant="fixed",
        inputs=inputs,
        method="strict_existing_sidecar_revalidation",
        source_identity=source_identity,
        manifest=manifest,
        binary=binary,
        provenance=provenance,
        app=app,
        process_receipts=(),
        authorization=authorization,
    )


def _bind_or_rebuild_buggy(
    root: Path,
    workspace: Path,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
    runner: ProcessRunner,
) -> StableArtifact:
    existing = _try_load(root, _RECEIPTS["buggy"])
    if existing is not None and _binding_still_valid(root, workspace, existing):
        return existing

    tree = _ordinary_directory(root / _TREES["buggy"], root)
    metadata = _metadata(workspace, "buggy")
    build = tree / _CLIENT_BUILD_DIR
    build.mkdir(mode=0o700, exist_ok=True)
    _ordinary_directory(build, tree)
    tool = _ordinary_directory(root / _TOOLCHAIN, root)
    environment = _closed_environment(root, tool, metadata)
    configure = _configure_argv(tool, tree, build)
    compile_argv = (
        str(tool / "cmake"),
        "--build",
        str(build),
        "--target",
        "luanti",
        "--parallel",
        "2",
    )
    with _trusted_git(metadata):
        identity_before = luanti_client_source_identity(tree)
        configure_result = runner(
            configure, cwd=tree, env=environment, timeout_seconds=600
        )
        if not _process_passed(configure_result):
            return _write_process_blocker(
                root,
                inputs,
                "buggy_client_configure_failed",
                configure,
                configure_result,
                authorization,
            )
        build_result = runner(
            compile_argv, cwd=tree, env=environment, timeout_seconds=1_800
        )
        if not _process_passed(build_result):
            return _write_process_blocker(
                root,
                inputs,
                "buggy_client_build_failed",
                compile_argv,
                build_result,
                authorization,
                prior=(configure, configure_result),
            )
        identity_after = luanti_client_source_identity(tree)
        if identity_after != identity_before:
            raise RuntimeError("#14555 buggy client sources changed during rebuild")
        binary = _ordinary_file(tree / "bin/luanti", tree)
        provenance = _write_luanti_client_build_provenance(tree, binary)
        if _read_luanti_client_build_provenance(tree, binary) is None:
            raise RuntimeError("#14555 rebuilt buggy client provenance did not reload")
        manifest = luanti_client_source_manifest(tree)
    sidecar = _ordinary_file(
        binary.with_name(binary.name + ".warptest-build.json"), tree
    )
    app = _bind_app_executable(tree, binary, sidecar)
    return _write_ready_binding(
        root,
        variant="buggy",
        inputs=inputs,
        method="candidate_specific_rebuild",
        source_identity=identity_after,
        manifest=manifest,
        binary=binary,
        provenance=provenance,
        app=app,
        process_receipts=(
            _process_payload(configure, configure_result, 600),
            _process_payload(compile_argv, build_result, 1_800),
        ),
        authorization=authorization,
    )


def _bind_app_executable(tree: Path, binary: Path, sidecar: Path) -> dict[str, object]:
    app_binary = _ordinary_file(
        tree / "build-app/out/luanti.app/Contents/MacOS/luanti", tree
    )
    app_sidecar = app_binary.with_name(app_binary.name + ".warptest-build.json")
    shutil.copy2(binary, app_binary)
    shutil.copy2(sidecar, app_sidecar)
    _ordinary_file(app_sidecar, tree)
    binary_hash = _sha256(binary)
    app_hash = _sha256(app_binary)
    marker_map = {
        marker.decode("ascii"): marker in app_binary.read_bytes()
        for marker in _CLIENT_MARKERS
    }
    if binary_hash != app_hash or not all(marker_map.values()):
        raise RuntimeError("#14555 app executable binding validation failed")
    return {
        "app_binary_path": app_binary.relative_to(tree).as_posix(),
        "app_sidecar_path": app_sidecar.relative_to(tree).as_posix(),
        "app_binary_fingerprint": app_hash,
        "app_equals_root_binary": True,
        "background_markers": marker_map,
        "background_markers_complete": True,
    }


def _write_ready_binding(
    root: Path,
    *,
    variant: Literal["fixed", "buggy"],
    inputs: tuple[ArtifactReference, ...],
    method: str,
    source_identity: str,
    manifest: dict[str, object],
    binary: Path,
    provenance: dict[str, object],
    app: dict[str, object],
    process_receipts: tuple[dict[str, object], ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / _TREES[variant]
    payload = {
        "candidate": dict(_CANDIDATE),
        "variant": variant,
        "state": "ready",
        "outcome": "client_provenance_bound",
        "method": method,
        "tree_path": _TREES[variant].as_posix(),
        "source_identity": source_identity,
        "source_manifest_schema": manifest.get("schema_version"),
        "root_binary_path": binary.relative_to(root).as_posix(),
        "root_binary_fingerprint": _sha256(binary),
        "sidecar_binary_fingerprint": provenance.get("binary_sha256"),
        "client_provenance_ready": True,
        "app_binding": app,
        "process_receipts": list(process_receipts),
        "source_pair_role": variant,
        "old_attempts_loaded": False,
        "client_launched": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    if not tree.is_dir():
        raise RuntimeError("#14555 client tree disappeared before receipt write")
    return _write(root, _RECEIPTS[variant], inputs, payload, authorization)


def _write_binding_blocker(
    root: Path,
    *,
    variant: Literal["fixed", "buggy"],
    inputs: tuple[ArtifactReference, ...],
    reason: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write(
        root,
        _RECEIPTS[variant],
        inputs,
        {
            "candidate": dict(_CANDIDATE),
            "variant": variant,
            "state": "downstream_pending",
            "outcome": "infrastructure_blocked",
            "reason": reason,
            "terminal": False,
            "resumable": True,
            "gate_1_7_preserved": True,
            "client_provenance_ready": False,
            "client_launched": False,
            "formal_trials_authorized": False,
            "paid_actions_authorized": False,
            "complete": True,
        },
        authorization,
    )


def _write_process_blocker(
    root: Path,
    inputs: tuple[ArtifactReference, ...],
    reason: str,
    argv: tuple[str, ...],
    result: NativeProcessReceipt,
    authorization: AuthorizationRecord,
    *,
    prior: tuple[tuple[str, ...], NativeProcessReceipt] | None = None,
) -> StableArtifact:
    receipts = []
    if prior is not None:
        receipts.append(_process_payload(prior[0], prior[1], 600))
    receipts.append(
        _process_payload(argv, result, 1_800 if "--build" in argv else 600)
    )
    payload = {
        "candidate": dict(_CANDIDATE),
        "variant": "buggy",
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": reason,
        "terminal": False,
        "resumable": True,
        "gate_1_7_preserved": True,
        "client_provenance_ready": False,
        "process_receipts": receipts,
        "client_launched": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    return _write(root, _RECEIPTS["buggy"], inputs, payload, authorization)


def _binding_still_valid(root: Path, workspace: Path, artifact: StableArtifact) -> bool:
    if (
        artifact.payload.get("candidate") != _CANDIDATE
        or artifact.payload.get("variant") != "buggy"
        or artifact.payload.get("state") != "ready"
        or artifact.payload.get("client_provenance_ready") is not True
    ):
        return False
    tree = root / _TREES["buggy"]
    binary = tree / "bin/luanti"
    app_binary = tree / "build-app/out/luanti.app/Contents/MacOS/luanti"
    try:
        metadata = _metadata(workspace, "buggy")
        with _trusted_git(metadata):
            provenance = _read_luanti_client_build_provenance(tree, binary)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        provenance is not None
        and _sha256(binary) == artifact.payload.get("root_binary_fingerprint")
        and _sha256(app_binary) == artifact.payload.get("root_binary_fingerprint")
    )


def _validate_inputs(artifacts: tuple[StableArtifact, ...]) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in artifacts):
        raise RuntimeError("#14555 client repair current-v2 candidate drift")
    profile, aggregate, transition, lineage = artifacts[5:]
    if (
        profile.payload.get("build_mode", {}).get("BUILD_CLIENT") is not False
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("no_timeout_or_unknown") is not True
        or aggregate.payload.get("old_attempts_loaded") is not False
        or transition.payload.get("state") != "passed"
        or lineage.payload.get("old_rendered_countable") is not False
        or lineage.payload.get("terminal") is not False
    ):
        raise RuntimeError("#14555 strict Gate 1-7 or lineage premise drifted")


def _configure_argv(tool: Path, tree: Path, build: Path) -> tuple[str, ...]:
    return (
        str(tool / "cmake"),
        "-S",
        str(tree),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DRUN_IN_PLACE=TRUE",
        "-DBUILD_CLIENT=TRUE",
        "-DBUILD_SERVER=FALSE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DENABLE_LEVELDB=OFF",
        "-DENABLE_REDIS=OFF",
        "-DENABLE_POSTGRESQL=OFF",
        "-DCMAKE_FIND_FRAMEWORK=LAST",
        "-DENABLE_GETTEXT=TRUE",
        "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
        "-DCMAKE_EXE_LINKER_FLAGS=-lbz2",
    )


def _closed_environment(root: Path, tool: Path, metadata: Path) -> dict[str, str]:
    home = root / "private/runtime-home/luanti-14555-client-repair"
    home.mkdir(parents=True, exist_ok=True)
    prefixes = (
        "/opt/homebrew",
        "/opt/homebrew/opt/openal-soft",
        "/opt/homebrew/opt/gettext",
        "/opt/homebrew/opt/sqlite",
        "/opt/homebrew/opt/jpeg-turbo",
    )
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": f"{tool}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "CMAKE_PREFIX_PATH": ";".join(prefixes),
        "PKG_CONFIG_PATH": (
            "/opt/homebrew/lib/pkgconfig:/opt/homebrew/share/pkgconfig:"
            "/opt/homebrew/opt/openal-soft/lib/pkgconfig:"
            "/opt/homebrew/opt/gettext/lib/pkgconfig"
        ),
        "WARPTEST_LUANTI_TRUSTED_GIT_DIR": str(metadata),
    }


def _process_payload(
    argv: tuple[str, ...], receipt: NativeProcessReceipt, timeout: int
) -> dict[str, object]:
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    return {
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
    }


def _process_passed(receipt: NativeProcessReceipt) -> bool:
    return receipt.outcome == "completed" and receipt.returncode == 0


def _metadata(workspace: Path, variant: str) -> Path:
    return _ordinary_directory(
        workspace / ".warptest-cache/lazarus/metadata" / _TASK_ID / f"{variant}.git",
        workspace,
    )


@contextmanager
def _trusted_git(metadata: Path) -> Iterator[None]:
    key = "WARPTEST_LUANTI_TRUSTED_GIT_DIR"
    previous = os.environ.get(key)
    os.environ[key] = str(metadata)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _sha256(path: Path) -> str:
    return hashlib.sha256(_ordinary_file(path).read_bytes()).hexdigest()


def _ordinary_directory(path: Path, parent: Path | None = None) -> Path:
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe directory: {path}")
    if parent is not None:
        parent_resolved = parent.resolve(strict=True)
        if resolved != parent_resolved and parent_resolved not in resolved.parents:
            raise RuntimeError(f"directory escapes registered root: {path}")
    return resolved


def _ordinary_file(path: Path, parent: Path | None = None) -> Path:
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"unsafe file: {path}")
    if parent is not None:
        parent_resolved = parent.resolve(strict=True)
        if resolved != parent_resolved and parent_resolved not in resolved.parents:
            raise RuntimeError(f"file escapes registered root: {path}")
    return resolved


def _write(
    root: Path,
    reference: ArtifactReference,
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
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14555 client repair typed reload failed")
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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


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
    "CONFIRMATION",
    "ClientRepairResult",
    "repair_luanti_14555_clients",
]
