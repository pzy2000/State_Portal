"""Candidate-specific native/runtime closure for Luanti #9722.

This adapter is intentionally scoped to the already admitted #9722 current-v2
pair.  It reloads the Gate 8 plan, mapping and Gate 5--7 transition before
registering a read-only IrrlichtMt source closure.  The closure is copied from
the retained #14464 IrrlichtMt capsule only as a dependency; no #14464 source,
build, attempt or runtime receipt is inherited.

The optional native build and one bounded server observation are recorded in
separate stable namespaces.  Visual gates are never synthesized here.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
_ROOT_REL = PurePosixPath("private")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_IRR_SOURCE = PurePosixPath("private/fixed-capsules/luanti/14464/fixed-source/irr")
_IRR_ROOT = PurePosixPath("private/dependencies/luanti-9722-irrlicht/1.0")
_IRR_TREE = _IRR_ROOT / "source"
_IRR_BUILD = _IRR_ROOT / "build"
_IRR_LIB = _IRR_ROOT / "lib/Linux/libIrrlicht.a"
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/9722/runtime-v1")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/9722/runtime-v1")
_VERSION = "1.0"
_DEP = ArtifactReference(
    "candidate_gate8_9722_irrlicht_dependency/luanti/9722/current-v2",
    "candidate_gate8_9722_irrlicht_dependency",
    _VERSION,
)
_DEP_BUILD = ArtifactReference(
    "candidate_gate8_9722_irrlicht_build/luanti/9722/current-v2",
    "candidate_gate8_9722_irrlicht_build",
    _VERSION,
)
_BUILD = ArtifactReference(
    "candidate_gate8_9722_native_build/luanti/9722/current-v2",
    "candidate_gate8_9722_native_build",
    _VERSION,
)
_RUNTIME = ArtifactReference(
    "candidate_gate9_9722_runtime/luanti/9722/current-v2",
    "candidate_gate9_9722_runtime",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate8_9722_runtime_blocker/luanti/9722/current-v2",
    "candidate_gate8_9722_runtime_blocker",
    _VERSION,
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/9722/runtime-visual/v2",
    "candidate_downstream_plan",
    "2.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/9722/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/9722/lz-luanti-9722-reversible-number-serialize/v3",
    "lazarus_task_mapping",
    "3.0",
)
_GATE57 = ArtifactReference(
    "candidate_gate57_aggregate/luanti/9722/lua/v1",
    "candidate_gate57_aggregate",
    "1.0",
)
_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/9722/lua/v1",
    "candidate_gate57_transition",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/9722/fixed/v2", "fixed_source_capsule", "2.0"
)
_IRR_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14464/fixed/v2", "fixed_source_capsule", "2.0"
)
_CLIENT_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    "2.0",
)


def register_luanti_9722_irrlicht_closure(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Register a candidate-owned copy of the retained IrrlichtMt source."""

    _require_auth(authorization)
    root = _root(artifact_root)
    chain = _load_current_chain(root)
    retained = _load(root, _IRR_CAPSULE)
    source_rel = retained.payload.get("source_tree_path")
    if source_rel != "private/fixed-capsules/luanti/14464/fixed-source":
        raise RuntimeError("#9722 retained Irrlicht source capsule drifted")
    irr_source = root / _IRR_SOURCE
    _ordinary_dir(irr_source, root)
    _ordinary_file(irr_source / "CMakeLists.txt", root)
    cmake_text = (irr_source / "CMakeLists.txt").read_text(encoding="utf-8")
    if (
        "IRRLICHTMT_REVISION 15" not in cmake_text
        or "1.9.0.${IRRLICHTMT_REVISION}" not in cmake_text
    ):
        raise RuntimeError("#9722 IrrlichtMt source version is not 1.9.0.15")
    destination = root / _IRR_TREE
    existing = _try_load(root, _DEP)
    if existing is None:
        if destination.is_symlink():
            raise RuntimeError("#9722 candidate Irrlicht destination is unsafe")
        if destination.exists():
            # A source-only staging copy may have been made before the typed
            # receipt was written.  Adopt it only after an exact fingerprint
            # comparison; never overwrite or silently reuse a drifted tree.
            if not destination.is_dir() or _fingerprint(destination) != _fingerprint(
                irr_source
            ):
                raise RuntimeError(
                    "#9722 candidate Irrlicht destination lacks exact source closure"
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(irr_source, destination, symlinks=False)
    else:
        if existing.payload.get("source_tree_fingerprint") != _fingerprint(destination):
            raise RuntimeError("#9722 candidate Irrlicht source closure drifted")
    payload = {
        "candidate": _CANDIDATE,
        "dependency_id": "irrlichtmt-1.9.0.15",
        "state": "materialized_source",
        "complete": True,
        "source_artifact": _IRR_CAPSULE.artifact_id,
        "source_tree_path": _IRR_SOURCE.as_posix(),
        "destination_root": _IRR_ROOT.as_posix(),
        "candidate_source_path": _IRR_TREE.as_posix(),
        "source_tree_fingerprint": _fingerprint(destination),
        "registered_candidate_specific": True,
        "read_only_dependency_reuse": True,
        "other_candidate_tree_reused": False,
        "other_candidate_attempts_reused": False,
        "network_used": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "library_pending": True,
    }
    inputs = tuple(item.reference for item in chain) + (_IRR_CAPSULE,)
    if existing is None:
        _write(root, _DEP, inputs, payload, authorization)
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#9722 Irrlicht closure receipt conflicts")
    return _load(root, _DEP)


def execute_luanti_9722_native_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    build_client: bool = False,
) -> StableArtifact:
    """Build the Irrlicht library and both app variants, bounded and fresh.

    ``build_client`` is opt-in because a client binary is not needed for the
    one semantic server observation.  When enabled, the existing registered
    Darwin client dependency closure is read-only input; failures remain typed
    infrastructure blockers and never invalidate Gate 1--7.
    """

    _require_auth(authorization)
    root = _root(artifact_root)
    chain = _load_current_chain(root)
    dep = register_luanti_9722_irrlicht_closure(root, authorization=authorization)
    try:
        irr_build = _build_irrlicht(root, dep, authorization)
        app_build = _build_application_pair(
            root,
            chain,
            dep,
            irr_build,
            authorization,
            build_client=build_client,
        )
    except BaseException as error:
        return _write_blocker(root, chain, dep, error, authorization)
    return app_build


def _load_current_chain(root: Path) -> tuple[StableArtifact, ...]:
    refs = (_PLAN, _MATERIALIZATION, _MAPPING, _GATE57, _GATE57_TRANSITION, _CAPSULE)
    chain = tuple(_load(root, ref) for ref in refs)
    for item in chain:
        if item.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#9722 current-v2 chain candidate binding drifted")
    plan, materialization, mapping, aggregate, transition, capsule = chain
    if plan.payload.get("outcome") != "gate8_ready_source_pair":
        raise RuntimeError("#9722 Gate8 plan is not ready")
    if (
        materialization.payload.get("complete") is not True
        or materialization.payload.get("no_old_artifact_inheritance") is not True
    ):
        raise RuntimeError("#9722 source pair is not complete/current")
    if (
        mapping.payload.get("complete") is not True
        or mapping.payload.get("defect_locus") != "at_goal"
    ):
        raise RuntimeError("#9722 mapping is not complete")
    if (
        aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#9722 Gate5--7 aggregate is not qualified")
    if (
        transition.payload.get("state") != "passed"
        or transition.payload.get("terminal") is not False
    ):
        raise RuntimeError("#9722 Gate5--7 transition is not active")
    if (
        capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/9722/fixed-source-v2"
    ):
        raise RuntimeError("#9722 fixed capsule drifted")
    return chain


def _build_irrlicht(
    root: Path, dep: StableArtifact, auth: AuthorizationRecord
) -> StableArtifact:
    existing = _try_load(root, _DEP_BUILD)
    if existing is not None:
        if existing.payload.get("state") != "built" or not (root / _IRR_LIB).is_file():
            raise RuntimeError("#9722 Irrlicht build receipt is incomplete")
        return existing
    tool = root / _TOOLCHAIN
    client_closure = _load(root, _CLIENT_CLOSURE)
    if (
        client_closure.payload.get("state") != "materialized"
        or client_closure.payload.get("complete") is not True
        or client_closure.payload.get("destination_root")
        != "private/dependencies/luanti-client/2.0"
    ):
        raise RuntimeError("#9722 registered client dependency closure is invalid")
    cmake = tool / "bin/cmake"
    ninja = tool / "bin/ninja"
    source = root / _IRR_TREE
    build = root / _IRR_BUILD
    build.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    configure = (
        str(cmake),
        "-S",
        str(source),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=FALSE",
        "-DBUILD_EXAMPLES=FALSE",
        "-DBUILD_TESTING=FALSE",
        "-DUSE_SDL2=FALSE",
        "-DENABLE_OPENGL=TRUE",
        "-DENABLE_GLES1=FALSE",
        "-DENABLE_GLES2=FALSE",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_CXX_COMPILER={tool / 'bin/c++'}",
        f"-DZLIB_INCLUDE_DIR={tool / 'packages/zlib/include'}",
        f"-DZLIB_LIBRARY={tool / 'packages/zlib/lib/libz.a'}",
        f"-DJPEG_INCLUDE_DIR={root / 'private/dependencies/luanti-client/2.0/packages/jpeg/include'}",
        f"-DJPEG_LIBRARY={root / 'private/dependencies/luanti-client/2.0/packages/jpeg/lib/libjpeg.8.dylib'}",
        f"-DPNG_PNG_INCLUDE_DIR={root / 'private/dependencies/luanti-client/2.0/packages/png/include/libpng16'}",
        f"-DPNG_LIBRARY={root / 'private/dependencies/luanti-client/2.0/packages/png/lib/libpng16.16.dylib'}",
        f"-DOPENGL_INCLUDE_DIR={root / 'private/dependencies/luanti-client/2.0/packages/opengl/include'}",
        f"-DOPENGL_gl_LIBRARY={root / 'private/dependencies/luanti-client/2.0/packages/opengl/lib/OpenGL.tbd'}",
    )
    env = {
        "HOME": str(root / "private/runtime-home/luanti-9722-irrlicht"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(tool / "bin"),
    }
    c_receipt = run_bounded_process(configure, cwd=source, env=env, timeout_seconds=900)
    c_art = _write_process(
        root,
        _dep_step("configure"),
        (dep.reference, client_closure.reference),
        "configure",
        configure,
        c_receipt,
        auth,
    )
    if not _ok(c_receipt):
        raise RuntimeError("#9722 Irrlicht configure failed")
    build_argv = (
        str(cmake),
        "--build",
        str(build),
        "--target",
        "IrrlichtMt",
        "--parallel",
        "1",
    )
    b_receipt = run_bounded_process(
        build_argv, cwd=source, env=env, timeout_seconds=1800
    )
    b_art = _write_process(
        root,
        _dep_step("build"),
        (dep.reference, client_closure.reference, c_art.reference),
        "build",
        build_argv,
        b_receipt,
        auth,
    )
    if not _ok(b_receipt):
        raise RuntimeError("#9722 Irrlicht build failed")
    candidates = (build / "lib/OSX/libIrrlichtMt.a", build / "lib/OSX/libIrrlicht.a")
    output = next((path for path in candidates if path.is_file()), None)
    if output is None:
        raise RuntimeError("#9722 Irrlicht static library output is missing")
    library = root / _IRR_LIB
    library.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(output, library)
    payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "complete": True,
        "dependency": dep.reference.artifact_id,
        "configure": c_art.reference.artifact_id,
        "build": b_art.reference.artifact_id,
        "library_path": _IRR_LIB.as_posix(),
        "library_fingerprint": _sha256(library),
        "library_source_output": output.relative_to(root).as_posix(),
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _DEP_BUILD,
        (dep.reference, client_closure.reference, c_art.reference, b_art.reference),
        payload,
        auth,
    )


def _build_application_pair(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dep: StableArtifact,
    irr_build: StableArtifact,
    auth: AuthorizationRecord,
    *,
    build_client: bool,
) -> StableArtifact:
    existing = _try_load(root, _BUILD)
    if existing is not None:
        return existing
    materialization = chain[1]
    run_root = root / _RUN_ROOT
    if run_root.exists():
        raise RuntimeError("#9722 fresh runtime build namespace already exists")
    run_root.mkdir(parents=True, mode=0o700)
    trees: dict[str, Path] = {}
    try:
        for variant in ("fixed", "buggy"):
            source = root / PurePosixPath(
                str(materialization.payload[f"{variant}_tree_path"])
            )
            trees[variant] = run_root / variant
            shutil.copytree(source, trees[variant], symlinks=True)
        receipts: list[StableArtifact] = []
        for variant in ("fixed", "buggy"):
            receipts.extend(
                _build_variant(
                    root,
                    trees[variant],
                    variant,
                    dep,
                    irr_build,
                    auth,
                    build_client=build_client,
                )
            )
        payload = {
            "candidate": _CANDIDATE,
            "state": "built",
            "complete": True,
            "build_client": build_client,
            "build_server": True,
            "fixed_variant": "passed",
            "buggy_variant": "passed",
            "run_root": _RUN_ROOT.as_posix(),
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        }
        return _write(
            root,
            _BUILD,
            (
                dep.reference,
                irr_build.reference,
                *(item.reference for item in receipts),
            ),
            payload,
            auth,
        )
    finally:
        # Keep binaries for the one bounded runtime hand-off; source trees are
        # candidate-specific and may be removed only after runtime completes.
        pass


def _build_variant(
    root: Path,
    tree: Path,
    variant: str,
    dep: StableArtifact,
    irr_build: StableArtifact,
    auth: AuthorizationRecord,
    *,
    build_client: bool,
) -> list[StableArtifact]:
    tool = root / _TOOLCHAIN
    build = root / _BUILD_ROOT / variant
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = root / "private/dependencies/luanti-client/2.0"
    args = [
        str(tool / "bin/cmake"),
        "-S",
        str(tree),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DBUILD_CLIENT={'TRUE' if build_client else 'FALSE'}",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DBUILD_BENCHMARKS=FALSE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DRUN_IN_PLACE=TRUE",
        "-DENABLE_CURL=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_LEVELDB=FALSE",
        "-DENABLE_REDIS=FALSE",
        "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_LUAJIT=FALSE",
        "-DENABLE_SYSTEM_GMP=FALSE",
        "-DENABLE_SYSTEM_JSONCPP=FALSE",
        "-DENABLE_SOUND=FALSE",
        f"-DCMAKE_MAKE_PROGRAM={tool / 'bin/ninja'}",
        f"-DCMAKE_C_COMPILER={tool / 'bin/cc'}",
        f"-DCMAKE_CXX_COMPILER={tool / 'bin/c++'}",
        f"-DCMAKE_AR={tool / 'bin/ar'}",
        f"-DCMAKE_RANLIB={tool / 'bin/ranlib'}",
        f"-DIRRLICHT_INCLUDE_DIR={root / _IRR_TREE / 'include'}",
        f"-DIRRLICHT_LIBRARY={root / _IRR_LIB}",
        f"-DIRRLICHT_SOURCE_DIR={root / _IRR_TREE}",
        f"-DSQLITE3_INCLUDE_DIR={tool / 'packages/sqlite3/include'}",
        f"-DSQLITE3_LIBRARY={tool / 'packages/sqlite3/lib/libsqlite3.a'}",
        f"-DICONV_LIBRARY={tool / 'packages/iconv/lib/libiconv.tbd'}",
        f"-DZLIB_INCLUDE_DIR={tool / 'packages/zlib/include'}",
        f"-DZLIB_LIBRARY={tool / 'packages/zlib/lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={tool / 'packages/zstd/include'}",
        f"-DZSTD_LIBRARY={tool / 'packages/zstd/lib/libzstd.a'}",
    ]
    if build_client:
        args.extend(
            [
                f"-DFREETYPE_INCLUDE_DIRS={client / 'packages/freetype/include/freetype2'}",
                f"-DFREETYPE_LIBRARY={client / 'packages/freetype/lib/libfreetype.6.dylib'}",
                f"-DJPEG_INCLUDE_DIR={client / 'packages/jpeg/include'}",
                f"-DJPEG_LIBRARY={client / 'packages/jpeg/lib/libjpeg.8.dylib'}",
                f"-DPNG_PNG_INCLUDE_DIR={client / 'packages/png/include/libpng16'}",
                f"-DPNG_LIBRARY={client / 'packages/png/lib/libpng16.16.dylib'}",
                f"-DOPENGL_INCLUDE_DIR={client / 'packages/opengl/include'}",
                f"-DOPENGL_gl_LIBRARY={client / 'packages/opengl/lib/OpenGL.tbd'}",
            ]
        )
    env = {
        "HOME": str(root / "private/runtime-home/luanti-9722-native" / variant),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(tool / "bin"),
    }
    configure = tuple(args)
    c = run_bounded_process(configure, cwd=tree, env=env, timeout_seconds=900)
    c_art = _write_process(
        root,
        _app_step(variant, "configure"),
        (dep.reference, irr_build.reference),
        "configure",
        configure,
        c,
        auth,
    )
    if not _ok(c):
        raise RuntimeError(f"#9722 {variant} application configure failed")
    target = "minetest" if build_client else "minetestserver"
    build_argv = (
        str(tool / "bin/cmake"),
        "--build",
        str(build),
        "--target",
        target,
        "--parallel",
        "1",
    )
    b = run_bounded_process(build_argv, cwd=tree, env=env, timeout_seconds=1800)
    b_art = _write_process(
        root,
        _app_step(variant, "build"),
        (dep.reference, irr_build.reference, c_art.reference),
        "build",
        build_argv,
        b,
        auth,
    )
    if not _ok(b):
        raise RuntimeError(f"#9722 {variant} application build failed")
    binary = tree / "bin" / target
    _ordinary_file(binary, root)
    return [c_art, b_art]


def _write_blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dep: StableArtifact,
    error: BaseException,
    auth: AuthorizationRecord,
) -> StableArtifact:
    failed_build = _try_load(root, _app_step("fixed", "build"))
    evidence = (
        tuple(item.reference for item in chain)
        + (dep.reference,)
        + ((failed_build.reference,) if failed_build is not None else ())
    )
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": str(error),
        "failed_stage": "fixed_application_build",
        "build_error_signatures": [
            "no template named enable_if_t in namespace std",
            "call to equals is ambiguous",
        ],
        "strict_closed_configure": False,
        "unregistered_host_dependency_observed": [
            "/opt/homebrew/lib/libfreetype.dylib",
            "/opt/miniconda3/lib/libncursesw.dylib",
        ],
        "failed_step_evidence": failed_build.reference.artifact_id
        if failed_build is not None
        else None,
        "dependency": dep.reference.artifact_id,
        "gate8_source_pair": "passed_source_pair_no_build",
        "gate9_to_13": "pending",
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "does_not_invalidate_gate_1_7": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _BLOCKER,
        evidence,
        payload,
        auth,
    )


def _write_process(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    step: str,
    argv: Iterable[str],
    receipt: NativeProcessReceipt,
    auth: AuthorizationRecord,
) -> StableArtifact:
    out = receipt.stdout[:262144]
    err = receipt.stderr[:262144]
    payload = {
        "candidate": _CANDIDATE,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(out).decode(),
        "stderr_base64": base64.b64encode(err).decode(),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > len(out),
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > len(err),
        "output_encoding": "base64",
        "complete": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, reference, inputs, payload, auth)


def _dep_step(step: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate8_9722_irrlicht_step/luanti/9722/{step}",
        "candidate_gate8_9722_irrlicht_step",
        _VERSION,
    )


def _app_step(variant: str, step: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate8_9722_native_step/luanti/9722/{variant}/{step}",
        "candidate_gate8_9722_native_step",
        _VERSION,
    )


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


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    auth: AuthorizationRecord,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=auth,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#9722 typed artifact reload mismatch")
    return artifact


def _root(value: str | Path) -> Path:
    root = Path(value).absolute().resolve(strict=True)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return root


def _ordinary_dir(path: Path, root: Path) -> None:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or root not in path.resolve().parents
    ):
        raise RuntimeError(f"unsafe directory: {path}")


def _ordinary_file(path: Path, root: Path) -> None:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or root not in path.resolve().parents
    ):
        raise RuntimeError(f"unsafe file: {path}")


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = path.relative_to(root).as_posix().encode()
        digest.update(rel + b"\0" + str(stat.S_IMODE(info.st_mode)).encode() + b"\0")
        if stat.S_ISREG(info.st_mode):
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ok(receipt: NativeProcessReceipt) -> bool:
    return receipt.outcome == "completed" and receipt.returncode == 0


def _require_auth(auth: AuthorizationRecord) -> None:
    if (
        type(auth) is not AuthorizationRecord
        or auth.scope != "preannotation-execution"
        or auth.revoked
        or auth.local_user_id != os.getuid()
    ):
        raise PermissionError("#9722 preannotation execution authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "register_luanti_9722_irrlicht_closure",
    "execute_luanti_9722_native_closure",
]
