"""Candidate-owned IrrlichtMt 1.9.0mt4 Gate-8 repair for Luanti #11866.

The original Gate-5--7 recovery borrowed headers retained below #14464.  This
module does not disturb the already-qualified v5 cells.  It binds the release
pin found in #11866's own fixed source, verifies the authoritative tag archive,
materializes a clean fixed/buggy pair, and configures both variants without
loading any historical attempt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
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


CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
BUILD_CONFIRMATION = "EXECUTE_LUANTI_11866_GATE8_MT4_BUILD_V1"
_KEY = CandidateKey("luanti", 11866)
_HEAD_SHA = "197c40ca86fc1a73c3f1bcdf37f5518ec8c95e39"
_BASE_SHA = "66e8aae9f2a28ee31ffe30694fdb61a8fdceb8d7"
_MERGE_SHA = "f69eead62e71e22d1810e0dedfa363cb9f210268"
_TAG = "1.9.0mt4"
_TAG_COMMIT = "53db262bd224516547fe7e0e05698d9624cf3d61"
_ARCHIVE_SHA256 = "a0e2e5239ebca804adf54400ccaacaf228ec09223cfb2e1daddc9bf2694176e6"
_DEPENDENCY_ROOT = PurePosixPath(
    "private/dependencies/luanti-11866-irrlichtmt/1.9.0mt4"
)
_PAIR_ROOT = PurePosixPath("private/materialized/luanti/11866/gate8-mt4-v1")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/11866/gate8-mt4-v1")

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11866/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11866/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11866/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11866/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/11866/fixed/v2", "fixed_source_capsule", "2.0"
)
_STRICT_AGGREGATE = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_aggregate/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_aggregate",
    "1.0",
)
_STRICT_TRANSITION = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_transition/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_transition",
    "1.0",
)
_OLD_DEPENDENCY = ArtifactReference(
    "candidate_gate57_11866_irrlicht_dependency/luanti/11866/native/v1",
    "candidate_gate57_11866_irrlicht_dependency",
    "1.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_irrlicht_build_blocker/luanti/11866/native/v2",
    "candidate_gate57_11866_irrlicht_build_blocker",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_11866_irrlichtmt_dependency/luanti/11866/mt4-v1",
    "candidate_gate8_11866_irrlichtmt_dependency",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/11866/gate8-mt4-v1",
    "candidate_materialization",
    "1.0",
)
_SUPERSESSION = ArtifactReference(
    "candidate_gate8_11866_dependency_supersession/luanti/11866/mt4-v1",
    "candidate_gate8_11866_dependency_supersession",
    "1.0",
)
_READINESS = ArtifactReference(
    "candidate_gate8_11866_configure_readiness/luanti/11866/mt4-v1",
    "candidate_gate8_11866_configure_readiness",
    "1.0",
)
_PROBE = ArtifactReference(
    "candidate_gate8_11866_compile_probe/luanti/11866/mt4-v1",
    "candidate_gate8_11866_compile_probe",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_gate8_11866_native_build/luanti/11866/mt4-v1",
    "candidate_gate8_11866_native_build",
    "1.0",
)


def prepare_luanti_11866_gate8_mt4(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    """Verify dependency/source closure, materialize, and configure both sides."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, ref)
        for ref in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CAPSULE,
            _STRICT_AGGREGATE,
            _STRICT_TRANSITION,
        )
    )
    _validate_current_chain(chain)
    plan = _plan(root)
    dependency_payload = _dependency_payload(root, chain)
    dependency = _write_once(
        root,
        _DEPENDENCY,
        tuple(item.reference for item in chain),
        dependency_payload,
        authorization,
    )
    materialization_payload = _materialize_pair(root, plan)
    materialization = _write_once(
        root,
        _MATERIALIZATION,
        tuple(item.reference for item in chain),
        materialization_payload,
        authorization,
    )
    old_dependency, old_blocker = (
        _load(root, ref) for ref in (_OLD_DEPENDENCY, _OLD_BLOCKER)
    )
    supersession_payload = {
        "candidate": CANDIDATE,
        "state": "superseded",
        "complete": True,
        "old_dependency": old_dependency.reference.artifact_id,
        "old_source_tree_path": old_dependency.payload["source_tree_path"],
        "old_dependency_version": old_dependency.payload["dependency_version"],
        "old_blocker": old_blocker.reference.artifact_id,
        "replacement_dependency": dependency.reference.artifact_id,
        "replacement_version": _TAG,
        "replacement_tag_commit": _TAG_COMMIT,
        "reason": "old_dependency_was_retained_below_candidate_14464_not_pinned_by_11866",
        "does_not_invalidate_strict_gate_1_7_v5": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    supersession = _write_once(
        root,
        _SUPERSESSION,
        (old_dependency.reference, old_blocker.reference, dependency.reference),
        supersession_payload,
        authorization,
    )
    receipts = tuple(
        _configure_variant(root, plan, dependency, materialization, variant, authorization)
        for variant in ("fixed", "buggy")
    )
    readiness_payload = {
        "candidate": CANDIDATE,
        "state": "ready_for_sequential_build",
        "outcome": "candidate_owned_mt4_fixed_buggy_configure_passed",
        "complete": True,
        "dependency": dependency.reference.artifact_id,
        "materialization": materialization.reference.artifact_id,
        "configure_receipts": [item.reference.artifact_id for item in receipts],
        "strict_gate_1_7_v5": _STRICT_TRANSITION.artifact_id,
        "strict_gate_1_7_preserved": True,
        "build_started": False,
        "native_lane_required": True,
        "next_action": "when_native_lane_is_free_build_fixed_then_buggy_with_parallelism_1",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    readiness = _write_once(
        root,
        _READINESS,
        (
            dependency.reference,
            materialization.reference,
            _STRICT_AGGREGATE,
            _STRICT_TRANSITION,
            *(item.reference for item in receipts),
        ),
        readiness_payload,
        authorization,
    )
    return dependency, materialization, supersession, readiness


def execute_luanti_11866_mt4_compile_probes(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Compile only the old failure locus with ``-fsyntax-only`` on both sides."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    dependency = _load(root, _DEPENDENCY)
    materialization = _load(root, _MATERIALIZATION)
    readiness = _load(root, _READINESS)
    transition = _load(root, _STRICT_TRANSITION)
    if readiness.payload.get("state") != "ready_for_sequential_build":
        raise RuntimeError("#11866 mt4 compile probe is not ready")
    plan = _plan(root)
    receipts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR_ROOT / variant, root)
        build = _ordinary_dir(root / _BUILD_ROOT / variant, root)
        include = root / _DEPENDENCY_ROOT / "source/include"
        includes = (
            build / "src",
            tree / "src",
            plan.toolchain.package_roots["zlib"] / "include",
            plan.toolchain.package_roots["zstd"] / "include",
            plan.toolchain.package_roots["sqlite3"] / "include",
            tree / "lib/lua/src",
            tree / "lib/gmp",
            tree / "lib/jsoncpp",
            tree / "lib/bitop",
            tree / "src/script",
            include,
        )
        argv = (
            str(plan.toolchain.cxx_compiler),
            "-DSERVER",
            "-DUSE_CMAKE_CONFIG_H",
            "-Dminetestserver_EXPORTS",
            *(f"-I{path}" for path in includes),
            "-std=c++11",
            "-DNDEBUG",
            "-Wall",
            "-Wsign-compare",
            "-pipe",
            "-funroll-loops",
            "-Os",
            "-std=gnu++11",
            "-fsyntax-only",
            str(tree / "src/content/content.cpp"),
        )
        receipt = run_bounded_process(
            argv,
            cwd=tree,
            env=dict(plan.closed_environment),
            timeout_seconds=60,
        )
        artifact = _write_process(
            root,
            _step_ref(variant, "compile-probe"),
            (dependency.reference, materialization.reference, readiness.reference),
            variant,
            "compile-probe",
            argv,
            receipt,
            authorization,
        )
        receipts.append(artifact)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#11866 {variant} mt4 compile probe failed")
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "candidate_owned_mt4_cxx11_old_failure_locus_passed",
        "complete": True,
        "source": "src/content/content.cpp",
        "language_standard": "c++11",
        "exact_dependency": dependency.reference.artifact_id,
        "variants": ["fixed", "buggy"],
        "receipts": [item.reference.artifact_id for item in receipts],
        "object_files_written": False,
        "native_build_started": False,
        "old_cxx17_launcher_used": False,
        "old_texturelayer_macro_used": False,
        "strict_gate_1_7_v5_preserved": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _PROBE,
        (
            dependency.reference,
            materialization.reference,
            readiness.reference,
            transition.reference,
            *(item.reference for item in receipts),
        ),
        payload,
        authorization,
    )


def execute_luanti_11866_gate8_mt4_build(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Build fixed then buggy at -j1 after external native-lane coordination."""

    if confirmation != BUILD_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {BUILD_CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    readiness = _load(root, _READINESS)
    dependency = _load(root, _DEPENDENCY)
    materialization = _load(root, _MATERIALIZATION)
    probe = _load(root, _PROBE)
    transition = _load(root, _STRICT_TRANSITION)
    if (
        readiness.payload.get("state") != "ready_for_sequential_build"
        or probe.payload.get("state") != "passed"
        or transition.payload.get("outcome") != "gate57_qualified"
    ):
        raise RuntimeError("#11866 Gate8 build readiness drifted")
    plan = _plan(root)
    receipts: list[StableArtifact] = []
    binaries: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR_ROOT / variant, root)
        build = _ordinary_dir(root / _BUILD_ROOT / variant, root)
        argv = (
            str(plan.toolchain.cmake),
            "--build",
            str(build),
            "--target",
            "minetestserver",
            "--parallel",
            "1",
        )
        receipt = run_bounded_process(
            argv,
            cwd=tree,
            env=dict(plan.closed_environment),
            timeout_seconds=1800,
        )
        artifact = _write_process(
            root,
            _step_ref(variant, "build"),
            (dependency.reference, materialization.reference, readiness.reference),
            variant,
            "build",
            argv,
            receipt,
            authorization,
        )
        receipts.append(artifact)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#11866 {variant} Gate8 build failed")
        binary = _ordinary_executable(tree / "bin/minetestserver", root)
        binaries[variant] = {
            "path": binary.relative_to(root).as_posix(),
            "fingerprint": _sha256(binary),
        }
    payload = {
        "candidate": CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_mt4_fixed_buggy_server_build_passed",
        "complete": True,
        "dependency": dependency.reference.artifact_id,
        "materialization": materialization.reference.artifact_id,
        "binaries": binaries,
        "build_receipts": [item.reference.artifact_id for item in receipts],
        "build_order": ["fixed", "buggy"],
        "build_parallelism": 1,
        "strict_gate_1_7_v5_preserved": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _BUILD,
        (
            dependency.reference,
            materialization.reference,
            readiness.reference,
            probe.reference,
            transition.reference,
            *(item.reference for item in receipts),
        ),
        payload,
        authorization,
    )


def _validate_current_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(item.payload.get("candidate") != CANDIDATE for item in chain):
        raise RuntimeError("#11866 Gate8 chain candidate binding drifted")
    source, patch, atomicity, manifest, capsule, aggregate, transition = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#11866 source packet is incomplete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#11866 patch is not eligible")
    if atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#11866 atomicity is not accepted")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#11866 manifest is not passed")
    if capsule.payload.get("source_tree_path") != "private/fixed-capsules/luanti/11866/fixed-source":
        raise RuntimeError("#11866 capsule path drifted")
    if not (
        aggregate.payload.get("qualified") is True
        and aggregate.payload.get("attempt_count") == 12
        and aggregate.payload.get("stable_repetitions") is True
        and aggregate.payload.get("old_attempts_loaded") is False
        and transition.payload.get("outcome") == "gate57_qualified"
        and transition.payload.get("old_attempts_loaded") is False
    ):
        raise RuntimeError("#11866 strict Gate1-7 v5 is not qualified")


def _dependency_payload(root: Path, chain: tuple[StableArtifact, ...]) -> dict[str, object]:
    source = chain[0]
    capsule = chain[4]
    fixed = _ordinary_dir(root / PurePosixPath(str(capsule.payload["source_tree_path"])), root)
    manifest = _ordinary_file(root / PurePosixPath(str(source.payload["raw_evidence_path"])), root)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    pull = raw.get("pull", {})
    if (
        pull.get("head", {}).get("sha") != _HEAD_SHA
        or pull.get("base", {}).get("sha") != _BASE_SHA
        or pull.get("merge_commit_sha") != _MERGE_SHA
    ):
        raise RuntimeError("#11866 authoritative source pin drifted")
    workflow = _ordinary_file(fixed / ".github/workflows/macos.yml", root).read_text(encoding="utf-8")
    buildwin = _ordinary_file(fixed / "util/buildbot/buildwin64.sh", root).read_text(encoding="utf-8")
    if "IRRLICHT_TAG: 1.9.0mt4" not in workflow or "irrlicht_version=1.9.0mt4" not in buildwin:
        raise RuntimeError("#11866 own-source IrrlichtMt pin drifted")
    dep = _ordinary_dir(root / _DEPENDENCY_ROOT, root)
    archive = _ordinary_file(dep / "irrlicht-1.9.0mt4.tar.gz", root)
    tag_ref = _ordinary_file(dep / "tag-ref.json", root)
    extracted = _ordinary_dir(dep / "source", root)
    tag = json.loads(tag_ref.read_text(encoding="utf-8"))
    if (
        tag.get("ref") != "refs/tags/1.9.0mt4"
        or tag.get("object", {}).get("type") != "commit"
        or tag.get("object", {}).get("sha") != _TAG_COMMIT
        or _sha256(archive) != _ARCHIVE_SHA256
    ):
        raise RuntimeError("#11866 authoritative IrrlichtMt capture drifted")
    archive_files, archive_bytes = _validate_archive(archive, extracted)
    source_cmake = _ordinary_file(extracted / "CMakeLists.txt", root).read_text(encoding="utf-8")
    if "project(Irrlicht" not in source_cmake or "VERSION 1.9.0" not in source_cmake:
        raise RuntimeError("#11866 IrrlichtMt source version drifted")
    smaterial = _ordinary_file(extracted / "include/SMaterial.h", root).read_text(
        encoding="utf-8", errors="replace"
    )
    if "TextureLayer" not in smaterial:
        raise RuntimeError("#11866 mt4 source lacks candidate-era TextureLayer API")
    return {
        "candidate": CANDIDATE,
        "state": "materialized",
        "outcome": "authoritative_candidate_owned_irrlichtmt_mt4_closure",
        "complete": True,
        "candidate_source_pin": {
            "head_revision": _HEAD_SHA,
            "base_revision": _BASE_SHA,
            "merge_revision": _MERGE_SHA,
            "workflow": ".github/workflows/macos.yml",
            "workflow_pin": "IRRLICHT_TAG: 1.9.0mt4",
            "buildbot_pin": "irrlicht_version=1.9.0mt4",
        },
        "dependency_tag": _TAG,
        "dependency_commit": _TAG_COMMIT,
        "archive_url": "https://github.com/minetest/irrlicht/archive/refs/tags/1.9.0mt4.tar.gz",
        "tag_ref_url": "https://api.github.com/repos/minetest/irrlicht/git/ref/tags/1.9.0mt4",
        "archive_path": (_DEPENDENCY_ROOT / "irrlicht-1.9.0mt4.tar.gz").as_posix(),
        "archive_fingerprint": _ARCHIVE_SHA256,
        "tag_ref_path": (_DEPENDENCY_ROOT / "tag-ref.json").as_posix(),
        "tag_ref_fingerprint": _sha256(tag_ref),
        "source_tree_path": (_DEPENDENCY_ROOT / "source").as_posix(),
        "source_tree_fingerprint": _tree_digest(extracted),
        "include_tree_fingerprint": _tree_digest(extracted / "include"),
        "archive_inventory": {"files": archive_files, "bytes": archive_bytes},
        "source_from_other_candidate": False,
        "source_from_14464": False,
        "network_action": "read_only_authoritative_release_capture",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _materialize_pair(root: Path, plan: NativeRegressionExecutionPlan) -> dict[str, object]:
    pair = root / _PAIR_ROOT
    fixed = pair / "fixed"
    buggy = pair / "buggy"
    if not pair.exists():
        pair.mkdir(parents=True, mode=0o700)
        shutil.copytree(plan.fixed_capsule.source_tree, fixed, symlinks=True)
        shutil.copytree(plan.fixed_capsule.source_tree, buggy, symlinks=True)
        environment = dict(os.environ)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
            environment.pop(name, None)
        environment["GIT_CEILING_DIRECTORIES"] = str(pair)
        completed = subprocess.run(
            tuple(str(item) for item in plan.production_inverse.buggy_apply_argv),
            cwd=buggy,
            input=plan.production_inverse.patch,
            capture_output=True,
            timeout=30,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("#11866 Gate8 production inverse failed")
    fixed = _ordinary_dir(fixed, root)
    buggy = _ordinary_dir(buggy, root)
    capsule_fingerprint = _tree_digest(plan.fixed_capsule.source_tree)
    if _tree_digest(fixed) != capsule_fingerprint:
        raise RuntimeError("#11866 fixed Gate8 tree differs from capsule")
    fixed_inventory = _inventory(fixed)
    buggy_inventory = _inventory(buggy)
    all_paths = set(fixed_inventory) | set(buggy_inventory)
    changed = sorted(path for path in all_paths if fixed_inventory.get(path) != buggy_inventory.get(path))
    expected = sorted(plan.profile.production_paths)
    if changed != expected:
        raise RuntimeError(f"#11866 Gate8 inverse scope drifted: {changed}")
    for relative in (
        *plan.profile.test_paths,
        *plan.profile.p2p_test_paths,
        *plan.profile.support_paths,
    ):
        if fixed_inventory.get(relative) != buggy_inventory.get(relative):
            raise RuntimeError("#11866 Gate8 inverse changed test/support bytes")
    return {
        "candidate": CANDIDATE,
        "state": "materialized",
        "outcome": "clean_fixed_buggy_production_inverse_pair",
        "complete": True,
        "fixed_tree_path": (_PAIR_ROOT / "fixed").as_posix(),
        "buggy_tree_path": (_PAIR_ROOT / "buggy").as_posix(),
        "fixed_tree_fingerprint": _tree_digest(fixed),
        "buggy_tree_fingerprint": _tree_digest(buggy),
        "capsule_tree_fingerprint": capsule_fingerprint,
        "production_inverse_fingerprint": hashlib.sha256(plan.production_inverse.patch).hexdigest(),
        "changed_paths": changed,
        "changed_paths_exactly_registered_production": True,
        "test_support_bytes_equal": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _configure_variant(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    dependency: StableArtifact,
    materialization: StableArtifact,
    variant: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = _ordinary_dir(root / _PAIR_ROOT / variant, root)
    build = root / _BUILD_ROOT / variant
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = list(plan.configure_argv)
    raw[raw.index("-S") + 1] = str(tree)
    raw[raw.index("-B") + 1] = str(build)
    include = root / _DEPENDENCY_ROOT / "source/include"
    argv = tuple(str(item) for item in raw) + (f"-DIRRLICHT_INCLUDE_DIR={include}",)
    if any("14464" in item for item in argv):
        raise RuntimeError("#11866 Gate8 configure inherited #14464")
    receipt = run_bounded_process(
        argv,
        cwd=tree,
        env=dict(plan.closed_environment),
        timeout_seconds=900,
    )
    artifact = _write_process(
        root,
        _step_ref(variant, "configure"),
        (dependency.reference, materialization.reference, _STRICT_TRANSITION),
        variant,
        "configure",
        argv,
        receipt,
        authorization,
    )
    if receipt.outcome != "completed" or receipt.returncode != 0:
        raise RuntimeError(f"#11866 {variant} Gate8 configure failed")
    cache = _ordinary_file(build / "CMakeCache.txt", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    ninja = _ordinary_file(build / "build.ninja", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    markers = (
        "BUILD_CLIENT:BOOL=FALSE",
        "BUILD_SERVER:BOOL=TRUE",
        "BUILD_UNITTESTS:BOOL=TRUE",
        "ENABLE_CURSES:BOOL=FALSE",
        f"IRRLICHT_INCLUDE_DIR:PATH={include}",
    )
    if any(marker not in cache for marker in markers):
        raise RuntimeError(f"#11866 {variant} Gate8 configure cache drifted")
    forbidden = ("14464", "TextureLayer=TextureLayers", "cxx17-texturelayer-launcher")
    if any(marker in cache + ninja for marker in forbidden):
        raise RuntimeError(f"#11866 {variant} Gate8 configure inherited old adapter")
    return artifact


def _write_process(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "old_attempts_loaded": False,
        "candidate_execution_performed": step == "build",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, ref, inputs, payload, authorization)


def _step_ref(variant: str, step: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate8_11866_native_step/luanti/11866/{variant}/{step}/mt4-v1",
        "candidate_gate8_11866_native_step",
        "1.0",
    )


def _plan(root: Path) -> NativeRegressionExecutionPlan:
    result = plan_luanti_native_regression_execution(root, candidate=_KEY)
    if isinstance(result, StageDecision):
        raise RuntimeError(f"#11866 native plan blocked: {result.details}")
    return result


def _validate_archive(archive: Path, extracted: Path) -> tuple[int, int]:
    expected: set[str] = set()
    total = 0
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "irrlicht-1.9.0mt4" or ".." in parts:
                raise RuntimeError("#11866 IrrlichtMt archive entry is unsafe")
            if member.isdir():
                continue
            if not member.isfile():
                raise RuntimeError("#11866 IrrlichtMt archive entry type is unsafe")
            relative = PurePosixPath(*parts[1:]).as_posix()
            expected.add(relative)
            content = package.extractfile(member)
            if content is None:
                raise RuntimeError("#11866 IrrlichtMt archive file is unreadable")
            data = content.read()
            total += len(data)
            path = _ordinary_file(extracted / Path(*parts[1:]), extracted.parent)
            if path.read_bytes() != data:
                raise RuntimeError("#11866 IrrlichtMt extraction differs from archive")
    actual = {
        item.relative_to(extracted).as_posix()
        for item in extracted.rglob("*")
        if item.is_file()
    }
    if actual != expected:
        raise RuntimeError("#11866 IrrlichtMt extraction inventory drifted")
    return len(expected), total


def _inventory(root: Path) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            result[relative] = ("dir", "")
        elif stat.S_ISREG(info.st_mode):
            result[relative] = ("file", _sha256(path))
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if os.path.isabs(target):
                raise RuntimeError("#11866 materialized tree has unsafe symlink")
            result[relative] = ("link", target)
        else:
            raise RuntimeError("#11866 materialized tree has unsafe entry")
    return result


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(_inventory(root).items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value[0].encode("ascii"))
        digest.update(b"\0")
        digest.update(value[1].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#11866 Gate8 directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#11866 Gate8 file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#11866 Gate8 executable is unsafe")
    return result


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _write_once(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#11866 artifact conflicts: {ref.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, ref)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#11866 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 Gate8 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "BUILD_CONFIRMATION",
    "execute_luanti_11866_gate8_mt4_build",
    "execute_luanti_11866_mt4_compile_probes",
    "prepare_luanti_11866_gate8_mt4",
]
