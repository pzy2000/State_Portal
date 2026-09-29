"""Source-preserving Gate-8 configure repair for Luanti #14464."""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .luanti_native_regression import NativeRegressionExecutionPlan, plan_luanti_native_regression_execution
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = {"repository_key": "luanti", "change_number": 14464}
BUILD_CONFIRMATION = "EXECUTE_LUANTI_14464_GATE8_BUILD_V1"
_KEY = CandidateKey("luanti", 14464)
_HEAD_REVISION = "fa89a63b34005bccc5a25b80ae6c8d4a80ff25a7"
_BASE_REVISION = "d307d01b18b84a117ecd2c427cec85595cf72ea3"
_MERGE_REVISION = "5df60d85f71215b091430a32c7834dd9c90bf79a"
_CURL_PIN = "8.5.0"
_IRRLICHT_VERSION = "1.9.0.15"
_PAIR = PurePosixPath("private/materialized/luanti/14464/gate8-v1")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/14464/gate8-v1")

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14464/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14464/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14464/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14464/test_manifest/v2", "candidate_stage", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/14464/preflight/v2", "candidate_profile", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14464/fixed/v2", "fixed_source_capsule", "2.0"
)
_STRICT_TRANSITION = ArtifactReference(
    "candidate_gate57_14464_curl_retry_transition/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_transition",
    "1.0",
)
_OLD_CURL = ArtifactReference(
    "candidate_gate57_14464_curl_dependency/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_dependency",
    "1.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate8_14464_curl_undef_blocker/luanti/14464/native/v1",
    "candidate_gate8_14464_curl_undef_blocker",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_14464_irrlicht_dependency/luanti/14464/current-v2/v1",
    "candidate_gate8_14464_irrlicht_dependency",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/14464/gate8-v1",
    "candidate_materialization",
    "1.0",
)
_ADAPTER = ArtifactReference(
    "candidate_gate8_14464_generated_config_adapter/luanti/14464/v1",
    "candidate_gate8_14464_generated_config_adapter",
    "1.0",
)
_SUPERSESSION = ArtifactReference(
    "candidate_gate8_14464_curl_supersession/luanti/14464/v1",
    "candidate_gate8_14464_curl_supersession",
    "1.0",
)
_READINESS = ArtifactReference(
    "candidate_gate8_14464_build_readiness/luanti/14464/v1",
    "candidate_gate8_14464_build_readiness",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_gate8_14464_native_build/luanti/14464/v1",
    "candidate_gate8_14464_native_build",
    "1.0",
)
_RUNTIME_PLAN = ArtifactReference(
    "candidate_gate9_10_14464_runtime_plan/luanti/14464/v1",
    "candidate_gate9_10_14464_runtime_plan",
    "1.0",
)
_VISUAL_PLAN = ArtifactReference(
    "candidate_gate11_13_14464_visual_plan/luanti/14464/v1",
    "candidate_gate11_13_14464_visual_plan",
    "1.0",
)


def prepare_luanti_14464_gate8(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, ref)
        for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _PROFILE, _CAPSULE)
    )
    transition = _load(root, _STRICT_TRANSITION)
    _validate_chain(chain, transition)
    plan = _plan(root)
    dependency = _write_once(
        root,
        _DEPENDENCY,
        tuple(item.reference for item in chain),
        _dependency_payload(root, chain),
        authorization,
    )
    materialization = _write_once(
        root,
        _MATERIALIZATION,
        tuple(item.reference for item in chain),
        _materialize_pair(root, plan),
        authorization,
    )
    configure_receipts: list[StableArtifact] = []
    adapter_variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        configure, adapter_info = _configure_and_adapt(
            root, plan, dependency, materialization, variant, authorization
        )
        configure_receipts.append(configure)
        adapter_variants[variant] = adapter_info
    adapter_payload = {
        "candidate": CANDIDATE,
        "state": "registered",
        "outcome": "generated_config_use_curl_zero_normalized_to_undefined",
        "complete": True,
        "intent": "honor_ENABLE_CURL_FALSE_for_both_numeric_and_ifdef_call_sites",
        "variants": adapter_variants,
        "compiler_undefinition_flag_used": False,
        "requested_forbidden_flag": "-UUSE_CURL",
        "forbidden_flag_used": False,
        "production_source_bytes_modified": False,
        "test_source_bytes_modified": False,
        "generated_build_header_only": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    adapter = _write_once(
        root,
        _ADAPTER,
        (
            dependency.reference,
            materialization.reference,
            *(item.reference for item in configure_receipts),
        ),
        adapter_payload,
        authorization,
    )
    old_curl, old_blocker = (_load(root, ref) for ref in (_OLD_CURL, _OLD_BLOCKER))
    supersession = _write_once(
        root,
        _SUPERSESSION,
        (old_curl.reference, old_blocker.reference, adapter.reference),
        {
            "candidate": CANDIDATE,
            "state": "superseded",
            "complete": True,
            "old_curl_dependency": old_curl.reference.artifact_id,
            "old_registration_root": old_curl.payload["source_provenance"]["registration_root"],
            "old_blocker": old_blocker.reference.artifact_id,
            "replacement_adapter": adapter.reference.artifact_id,
            "replacement_requires_curl_library": False,
            "reason": "ENABLE_CURL_FALSE_is_now_honored_without_external_or_system_curl",
            "does_not_invalidate_gate_1_7": True,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    readiness = _write_once(
        root,
        _READINESS,
        (
            dependency.reference,
            materialization.reference,
            adapter.reference,
            *(item.reference for item in configure_receipts),
        ),
        {
            "candidate": CANDIDATE,
            "state": "ready_for_sequential_build",
            "outcome": "candidate_owned_irrlicht_and_source_preserving_curl_adapter_ready",
            "complete": True,
            "dependency": dependency.reference.artifact_id,
            "materialization": materialization.reference.artifact_id,
            "adapter": adapter.reference.artifact_id,
            "configure_receipts": [item.reference.artifact_id for item in configure_receipts],
            "strict_gate_1_7_status_reference": transition.reference.artifact_id,
            "strict_gate_1_7_not_execution_input": True,
            "native_build_started": False,
            "next_action": "when_native_lane_is_free_build_fixed_then_buggy_at_parallelism_1",
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return dependency, materialization, supersession, readiness


def write_luanti_14464_downstream_plans(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Register no-launch Gate9/10 and real-client visual plans."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    readiness = _load(root, _READINESS)
    dependency = _load(root, _DEPENDENCY)
    materialization = _load(root, _MATERIALIZATION)
    adapter = _load(root, _ADAPTER)
    if readiness.payload.get("state") != "ready_for_sequential_build":
        raise RuntimeError("#14464 downstream plan is not build-ready")
    runtime_payload = {
        "candidate": CANDIDATE,
        "state": "planned",
        "outcome": "candidate_bound_iconv_state_replay_planned",
        "complete": True,
        "task_id": "lz-luanti-14464-iconv-state-reset",
        "fixture_root": "private/runtime-fixtures/luanti/14464/iconv-v1",
        "registered_start": {
            "binary_pair": "candidate_gate8_14464_native_build/luanti/14464/v1",
            "generated_config_adapter": adapter.reference.artifact_id,
            "selector": "TestUtilities.testUTF8",
            "input_sequence_hex": ["c4", "81"],
            "initial_iconv_state": "fresh_process",
        },
        "ordered_replay": [
            {"type": "start_fresh_candidate_binary"},
            {"type": "invoke_utf8_to_wide", "input_hex": "c4"},
            {"type": "invoke_utf8_to_wide", "input_hex": "81"},
            {"type": "parse_unique_TestUtilities_summary"},
            {"type": "classify_state_reset_semantics"},
        ],
        "execution_command": [
            "bin/minetestserver",
            "--run-unittests",
            "--test-module",
            "TestUtilities.testUTF8",
        ],
        "semantic_oracle": {
            "goal": "second isolated continuation byte is not combined with prior incomplete lead byte",
            "defect": "conversion state leaks or invalid-byte handling violates the fixed test",
            "fixed_expected": "unique TestUtilities summary passes",
            "buggy_expected": "unique TestUtilities summary fails at iconv state regression",
            "p2p_invariant": "TestSettings remains passing",
        },
        "fresh_attempt_namespace": "candidate_gate9_14464_iconv_v1",
        "old_gate57_attempts_are_not_inputs": True,
        "build_required_before_execution": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    runtime = _write_once(
        root,
        _RUNTIME_PLAN,
        (readiness.reference, dependency.reference, materialization.reference, adapter.reference),
        runtime_payload,
        authorization,
    )
    source_evidence: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR / variant, root)
        game = _ordinary_file(tree / "src/client/game.cpp", root).read_text(
            encoding="utf-8", errors="replace"
        )
        formspec = _ordinary_file(tree / "src/gui/guiFormSpecMenu.cpp", root).read_text(
            encoding="utf-8", errors="replace"
        )
        defaults = _ordinary_file(tree / "src/defaultsettings.cpp", root).read_text(
            encoding="utf-8", errors="replace"
        )
        if not (
            "makeScreenshot" in game
            and "SCREENSHOT" in game
            and "utf8_to_wide" in formspec
            and 'keymap_screenshot", "KEY_F12"' in defaults
        ):
            raise RuntimeError(f"#14464 {variant} real-client visual source path drifted")
        source_evidence[variant] = {
            "game_fingerprint": hashlib.sha256(game.encode()).hexdigest(),
            "formspec_fingerprint": hashlib.sha256(formspec.encode()).hexdigest(),
            "defaults_fingerprint": hashlib.sha256(defaults.encode()).hexdigest(),
            "f12_engine_png": True,
            "formspec_uses_utf8_to_wide": True,
        }
    visual_payload = {
        "candidate": CANDIDATE,
        "state": "planned",
        "outcome": "real_client_iconv_visual_path_statically_feasible",
        "complete": True,
        "runtime_plan": runtime.reference.artifact_id,
        "source_evidence": source_evidence,
        "capture_backend": "minetest_builtin_F12_OpenGL_PNG",
        "panel_contract": {
            "common_before": "ICONV STATE REPLAY READY; bytes C4 then 81",
            "fixed_after": "GOAL: conversion state reset; isolated continuation is not U+0101",
            "buggy_after": "DEFECT: iconv state regression reproduced",
            "semantic_source": "fresh Gate9/10 selector observation, not a synthetic guess",
        },
        "golden_contract": {
            "common_before_fixed_after_buggy_after": True,
            "before_equivalence": True,
            "after_two_by_two": True,
            "same_lineage": True,
            "raw_frames": True,
        },
        "client_pair_status": "pending_candidate_owned_build",
        "real_frames_captured": False,
        "synthetic_frames_allowed": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "after server build and fresh Gate9/10, build candidate-owned clients with the same generated-config adapter and capture four real F12 PNGs",
    }
    visual = _write_once(
        root,
        _VISUAL_PLAN,
        (runtime.reference, dependency.reference, materialization.reference, adapter.reference),
        visual_payload,
        authorization,
    )
    return runtime, visual


def execute_luanti_14464_gate8_build(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Build fixed then buggy at -j1 after explicit lane handoff."""

    if confirmation != BUILD_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {BUILD_CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    readiness = _load(root, _READINESS)
    dependency = _load(root, _DEPENDENCY)
    materialization = _load(root, _MATERIALIZATION)
    adapter = _load(root, _ADAPTER)
    if readiness.payload.get("state") != "ready_for_sequential_build":
        raise RuntimeError("#14464 build readiness drifted")
    plan = _plan(root)
    receipts: list[StableArtifact] = []
    binaries: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR / variant, root)
        build = _ordinary_dir(root / _BUILD_ROOT / variant, root)
        generated = _ordinary_file(build / "src/cmake_config.h", root)
        if "#define USE_CURL" in generated.read_text(encoding="utf-8"):
            raise RuntimeError("#14464 generated curl adapter drifted before build")
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
            argv, cwd=tree, env=dict(plan.closed_environment), timeout_seconds=1800
        )
        step = _write_build_process(
            root,
            variant,
            (dependency.reference, materialization.reference, adapter.reference, readiness.reference),
            argv,
            receipt,
            authorization,
        )
        receipts.append(step)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#14464 {variant} Gate8 build failed")
        if "#define USE_CURL" in generated.read_text(encoding="utf-8"):
            raise RuntimeError("#14464 CMake regenerated USE_CURL during build")
        binary = _ordinary_file(tree / "bin/minetestserver", root)
        if not os.access(binary, os.X_OK):
            raise RuntimeError("#14464 built server is not executable")
        binaries[variant] = {
            "path": binary.relative_to(root).as_posix(),
            "fingerprint": _sha256(binary),
        }
    return _write_once(
        root,
        _BUILD,
        (
            dependency.reference,
            materialization.reference,
            adapter.reference,
            readiness.reference,
            *(item.reference for item in receipts),
        ),
        {
            "candidate": CANDIDATE,
            "state": "built",
            "outcome": "fixed_buggy_server_build_passed_with_generated_config_adapter",
            "complete": True,
            "build_order": ["fixed", "buggy"],
            "build_parallelism": 1,
            "binaries": binaries,
            "build_receipts": [item.reference.artifact_id for item in receipts],
            "forbidden_flag_used": False,
            "old_curl_dependency_used": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )


def _write_build_process(
    root: Path,
    variant: str,
    inputs: tuple[ArtifactReference, ...],
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    ref = ArtifactReference(
        f"candidate_gate8_14464_native_step/luanti/14464/{variant}/build/v1",
        "candidate_gate8_14464_native_step",
        "1.0",
    )
    return _write_once(
        root,
        ref,
        inputs,
        {
            "candidate": CANDIDATE,
            "variant": variant,
            "step": "build",
            "argv": list(argv),
            "process_outcome": receipt.outcome,
            "returncode": receipt.returncode,
            "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
            "stdout_truncated": receipt.stdout_truncated,
            "stderr_truncated": receipt.stderr_truncated,
            "forbidden_flag_used": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )


def _validate_chain(chain: tuple[StableArtifact, ...], transition: StableArtifact) -> None:
    if any(item.payload.get("candidate") != CANDIDATE for item in chain):
        raise RuntimeError("#14464 Gate8 chain candidate binding drifted")
    source, patch, atomicity, manifest, profile, capsule = chain
    if not (
        source.payload.get("complete") is True
        and patch.payload.get("outcome") == "patch_eligible"
        and atomicity.payload.get("outcome") == "accepted"
        and manifest.payload.get("state") == "passed"
        and profile.payload.get("strict_gate_status", {}).get("source_authority") == "passed"
        and capsule.payload.get("source_tree_path") == "private/fixed-capsules/luanti/14464/fixed-source"
        and transition.payload.get("candidate") == CANDIDATE
        and transition.payload.get("state") == "passed"
        and transition.payload.get("outcome") == "qualified"
        and transition.payload.get("attempt_count") == 12
        and transition.payload.get("old_attempts_loaded") is False
    ):
        raise RuntimeError("#14464 current chain is incomplete")


def _dependency_payload(root: Path, chain: tuple[StableArtifact, ...]) -> dict[str, object]:
    source = chain[0]
    capsule = chain[5]
    fixed = _ordinary_dir(root / PurePosixPath(str(capsule.payload["source_tree_path"])), root)
    manifest = _ordinary_file(root / PurePosixPath(str(source.payload["raw_evidence_path"])), root)
    import json

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    pull = raw.get("pull", {})
    if (
        pull.get("head", {}).get("sha") != _HEAD_REVISION
        or pull.get("base", {}).get("sha") != _BASE_REVISION
        or pull.get("merge_commit_sha") != _MERGE_REVISION
    ):
        raise RuntimeError("#14464 authoritative source pin drifted")
    common = _ordinary_file(fixed / "util/buildbot/common.sh", root).read_text(encoding="utf-8")
    if "curl_version=8.5.0" not in common:
        raise RuntimeError("#14464 curl pin drifted")
    irr = _ordinary_dir(fixed / "irr", root)
    irr_cmake = _ordinary_file(irr / "CMakeLists.txt", root).read_text(encoding="utf-8")
    if "set(IRRLICHTMT_REVISION 15)" not in irr_cmake or "VERSION 1.9.0.${IRRLICHTMT_REVISION}" not in irr_cmake:
        raise RuntimeError("#14464 candidate-owned Irrlicht pin drifted")
    return {
        "candidate": CANDIDATE,
        "state": "registered",
        "outcome": "candidate_owned_irrlicht_and_disabled_curl_source_pins_registered",
        "complete": True,
        "candidate_source_pin": {
            "head_revision": _HEAD_REVISION,
            "base_revision": _BASE_REVISION,
            "merge_revision": _MERGE_REVISION,
        },
        "irrlicht_source_path": "private/fixed-capsules/luanti/14464/fixed-source/irr",
        "irrlicht_version": _IRRLICHT_VERSION,
        "irrlicht_tree_fingerprint": _tree_digest(irr),
        "curl_source_pin": _CURL_PIN,
        "curl_mode": "disabled_by_ENABLE_CURL_FALSE",
        "curl_library_required": False,
        "source_from_other_candidate": False,
        "system_dependency_closure_used": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _materialize_pair(root: Path, plan: NativeRegressionExecutionPlan) -> dict[str, object]:
    pair = root / _PAIR
    fixed = pair / "fixed"
    buggy = pair / "buggy"
    if not pair.exists():
        pair.mkdir(parents=True, mode=0o700)
        shutil.copytree(plan.fixed_capsule.source_tree, fixed, symlinks=True)
        shutil.copytree(plan.fixed_capsule.source_tree, buggy, symlinks=True)
        env = dict(os.environ)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
            env.pop(name, None)
        env["GIT_CEILING_DIRECTORIES"] = str(pair)
        result = subprocess.run(
            tuple(str(item) for item in plan.production_inverse.buggy_apply_argv),
            cwd=buggy,
            input=plan.production_inverse.patch,
            capture_output=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError("#14464 production inverse failed")
    fixed = _ordinary_dir(fixed, root)
    buggy = _ordinary_dir(buggy, root)
    capsule_fingerprint = _tree_digest(plan.fixed_capsule.source_tree)
    if _tree_digest(fixed) != capsule_fingerprint:
        raise RuntimeError("#14464 fixed tree differs from capsule")
    fi = _inventory(fixed)
    bi = _inventory(buggy)
    changed = sorted(path for path in set(fi) | set(bi) if fi.get(path) != bi.get(path))
    if changed != sorted(plan.profile.production_paths):
        raise RuntimeError(f"#14464 inverse scope drifted: {changed}")
    for relative in (*plan.profile.test_paths, *plan.profile.p2p_test_paths, *plan.profile.support_paths):
        if fi.get(relative) != bi.get(relative):
            raise RuntimeError("#14464 inverse changed test/support bytes")
    if fi.get("irr/CMakeLists.txt") != bi.get("irr/CMakeLists.txt"):
        raise RuntimeError("#14464 inverse changed Irrlicht dependency")
    return {
        "candidate": CANDIDATE,
        "state": "materialized",
        "outcome": "clean_fixed_buggy_production_inverse_pair",
        "complete": True,
        "fixed_tree_path": (_PAIR / "fixed").as_posix(),
        "buggy_tree_path": (_PAIR / "buggy").as_posix(),
        "fixed_tree_fingerprint": _tree_digest(fixed),
        "buggy_tree_fingerprint": _tree_digest(buggy),
        "changed_paths": changed,
        "changed_paths_exactly_registered_production": True,
        "test_support_bytes_equal": True,
        "irrlicht_dependency_bytes_equal": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _configure_and_adapt(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    dependency: StableArtifact,
    materialization: StableArtifact,
    variant: str,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, dict[str, object]]:
    tree = _ordinary_dir(root / _PAIR / variant, root)
    build = root / _BUILD_ROOT / variant
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = list(plan.configure_argv)
    raw[raw.index("-S") + 1] = str(tree)
    raw[raw.index("-B") + 1] = str(build)
    argv = tuple(str(item) for item in raw)
    if any("-UUSE_CURL" in item for item in argv):
        raise RuntimeError("#14464 forbidden curl undefinition flag appeared")
    receipt = run_bounded_process(
        argv, cwd=tree, env=dict(plan.closed_environment), timeout_seconds=900
    )
    configure = _write_process(
        root,
        _step_ref(variant),
        (dependency.reference, materialization.reference),
        variant,
        argv,
        receipt,
        authorization,
    )
    if receipt.outcome != "completed" or receipt.returncode != 0:
        raise RuntimeError(f"#14464 {variant} configure failed")
    generated = _ordinary_file(build / "src/cmake_config.h", root)
    before = generated.read_text(encoding="utf-8")
    if "#define USE_CURL 0" in before:
        if before.count("#define USE_CURL 0") != 1:
            raise RuntimeError("#14464 generated USE_CURL field is ambiguous")
        generated.write_text(
            before.replace("#define USE_CURL 0", "/* #undef USE_CURL */", 1),
            encoding="utf-8",
        )
    after = generated.read_text(encoding="utf-8")
    if "#define USE_CURL" in after or "/* #undef USE_CURL */" not in after:
        raise RuntimeError("#14464 generated config adapter did not disable curl")
    cache = _ordinary_file(build / "CMakeCache.txt", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    ninja = _ordinary_file(build / "build.ninja", root).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    if "ENABLE_CURL:BOOL=FALSE" not in cache or "-UUSE_CURL" in ninja:
        raise RuntimeError("#14464 configure is not source-preserving curl-disabled")
    probe = _preprocess_probe(root, plan, tree, build)
    return configure, {
        "generated_header": generated.relative_to(root).as_posix(),
        "before_fingerprint": hashlib.sha256(before.encode()).hexdigest(),
        "after_fingerprint": hashlib.sha256(after.encode()).hexdigest(),
        "use_curl_defined_after_adapter": False,
        "enable_curl_cache": False,
        "preprocess_probe": probe,
    }


def _preprocess_probe(
    root: Path, plan: NativeRegressionExecutionPlan, tree: Path, build: Path
) -> dict[str, object]:
    commands = subprocess.run(
        (str(plan.toolchain.ninja), "-C", str(build), "-t", "commands"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if commands.returncode != 0:
        raise RuntimeError("#14464 could not inspect generated compile command")
    line = next(
        (value for value in commands.stdout.splitlines() if "unittest/test_utilities.cpp" in value),
        None,
    )
    if line is None:
        raise RuntimeError("#14464 TestUtilities compile command is missing")
    tokens = shlex.split(line)
    source = str(tree / "src/unittest/test_utilities.cpp")
    filtered: list[str] = []
    skip = 0
    for token in tokens:
        if skip:
            skip -= 1
            continue
        if token in {"-MD", "-c"}:
            continue
        if token in {"-MT", "-MF", "-o"}:
            skip = 1
            continue
        if token == source:
            continue
        filtered.append(token)
    preprocess = subprocess.run(
        (*filtered, "-E", "-P", source),
        cwd=tree,
        capture_output=True,
        timeout=60,
    )
    if preprocess.returncode != 0:
        raise RuntimeError("#14464 generated-config preprocess probe failed")
    text = preprocess.stdout.decode("utf-8", errors="replace")
    forbidden = ("curl_url(", "curl_url_set(", "curl_url_get(", "curl_url_cleanup(", "curl_free(")
    if any(marker in text for marker in forbidden):
        raise RuntimeError("#14464 curl ifdef branch remained after adapter")
    syntax = subprocess.run(
        (*filtered, "-fsyntax-only", source),
        cwd=tree,
        capture_output=True,
        timeout=60,
    )
    if syntax.returncode != 0:
        raise RuntimeError("#14464 generated-config syntax probe failed")
    return {
        "source": "src/unittest/test_utilities.cpp",
        "preprocess_returncode": preprocess.returncode,
        "syntax_returncode": syntax.returncode,
        "forbidden_curl_calls_present": False,
        "preprocessed_fingerprint": hashlib.sha256(preprocess.stdout).hexdigest(),
        "object_files_written": False,
    }


def _write_process(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    variant: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write_once(
        root,
        ref,
        inputs,
        {
            "candidate": CANDIDATE,
            "variant": variant,
            "step": "configure",
            "argv": list(argv),
            "process_outcome": receipt.outcome,
            "returncode": receipt.returncode,
            "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
            "stdout_truncated": receipt.stdout_truncated,
            "stderr_truncated": receipt.stderr_truncated,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )


def _step_ref(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate8_14464_native_step/luanti/14464/{variant}/configure/v1",
        "candidate_gate8_14464_native_step",
        "1.0",
    )


def _plan(root: Path) -> NativeRegressionExecutionPlan:
    result = plan_luanti_native_regression_execution(root, candidate=_KEY)
    if isinstance(result, StageDecision):
        raise RuntimeError(f"#14464 native plan blocked: {result.details}")
    return result


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
                raise RuntimeError("#14464 materialized tree has unsafe symlink")
            result[relative] = ("link", target)
        else:
            raise RuntimeError("#14464 materialized tree has unsafe entry")
    return result


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(_inventory(root).items()):
        digest.update(path.encode() + b"\0" + value[0].encode() + b"\0" + value[1].encode() + b"\n")
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
        raise RuntimeError("#14464 Gate8 directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#14464 Gate8 file is unsafe")
    return resolved


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
            raise RuntimeError(f"#14464 artifact conflicts: {ref.artifact_id}")
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
        raise RuntimeError(f"#14464 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14464 Gate8 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "BUILD_CONFIRMATION",
    "execute_luanti_14464_gate8_build",
    "prepare_luanti_14464_gate8",
    "write_luanti_14464_downstream_plans",
]
