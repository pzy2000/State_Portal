"""Candidate-specific Gate 4 API compatibility proof for Luanti #10661.

The PR changes the ``MapSettingsManager`` and ``Settings`` APIs while also
changing the frozen tests to use the new APIs.  This runner materializes the
merged PR tree, applies the typed production-only inverse, and compiles the
same fixed-test API expressions against both headers.  It intentionally does
not build or execute Luanti: a header-level compile failure is already a
strict Gate 4 failure and avoids treating an unrelated historical Irrlicht
toolchain mismatch as a candidate semantic result.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
)
from .luanti_native_executor import _apply_production_inverse, _copy_full_capsule
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 10661)
_ROOT_NAMESPACE = PurePosixPath("private/gate4-api/luanti/10661/v1")
_IRRLICHT_INCLUDE = PurePosixPath(
    "private/dependencies/luanti-11866-irrlicht/1.9.0.15/include"
)
_MAX_OUTPUT = 262_144
_CONFIRMATION = "EXECUTE_LUANTI_10661_GATE4_MECHANICAL_V1"
_API_REVIEW = ArtifactReference(
    "candidate_gate4_api_review/luanti/10661/current-v2/v1",
    "candidate_gate4_api_review",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_gate4_api_materialization/luanti/10661/v1",
    "candidate_gate4_api_materialization",
    "1.0",
)
_COMPILE_MAP_FIXED = ArtifactReference(
    "candidate_gate4_api_compile/luanti/10661/fixed/map-settings-manager/v1",
    "candidate_gate4_api_compile",
    "1.0",
)
_COMPILE_MAP_BUGGY = ArtifactReference(
    "candidate_gate4_api_compile/luanti/10661/buggy/map-settings-manager/v1",
    "candidate_gate4_api_compile",
    "1.0",
)
_COMPILE_SETTINGS_FIXED = ArtifactReference(
    "candidate_gate4_api_compile/luanti/10661/fixed/settings-layers/v1",
    "candidate_gate4_api_compile",
    "1.0",
)
_COMPILE_SETTINGS_BUGGY = ArtifactReference(
    "candidate_gate4_api_compile/luanti/10661/buggy/settings-layers/v1",
    "candidate_gate4_api_compile",
    "1.0",
)
_FINAL = ArtifactReference(
    "candidate_gate4_api_compatibility/luanti/10661/current-v3/v1",
    "candidate_gate4_api_compatibility",
    "1.0",
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
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
        raise RuntimeError("#10661 Gate 4 typed reload failed")
    return artifact


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#10661 Gate 4 requires preannotation authorization")


def _root(value: str | Path) -> Path:
    root = Path(value).absolute().resolve(strict=True)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#10661 Gate 4 artifact root is unsafe")
    return root


def _validate_tree(root: Path, plan: NativeRegressionExecutionPlan) -> None:
    fixed = root / _ROOT_NAMESPACE / "fixed"
    buggy = root / _ROOT_NAMESPACE / "buggy"
    for tree in (fixed, buggy):
        if not tree.is_dir() or tree.is_symlink():
            raise RuntimeError("#10661 Gate 4 materialized tree is missing")
        for path in tree.rglob("*"):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                continue
            if not stat.S_ISLNK(info.st_mode) or os.path.isabs(os.readlink(path)):
                raise RuntimeError("#10661 Gate 4 tree contains unsafe entry")
            try:
                (path.parent / os.readlink(path)).resolve(strict=False).relative_to(
                    tree.resolve()
                )
            except ValueError as error:
                raise RuntimeError("#10661 Gate 4 tree link escapes root") from error
    for relative in plan.profile.production_paths:
        if (fixed / relative).read_bytes() == (buggy / relative).read_bytes():
            raise RuntimeError(f"#10661 inverse did not change {relative}")
    for relative in (*plan.profile.test_paths, *plan.profile.p2p_test_paths):
        if (fixed / relative).read_bytes() != (buggy / relative).read_bytes():
            raise RuntimeError(f"#10661 inverse changed non-production {relative}")


def _materialize(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    namespace = root / _ROOT_NAMESPACE
    fixed = namespace / "fixed"
    buggy = namespace / "buggy"
    if not namespace.exists():
        namespace.mkdir(parents=True, mode=0o700)
        _copy_full_capsule(plan, fixed)
        _copy_full_capsule(plan, buggy)
        _apply_production_inverse(plan, fixed, buggy)
    _validate_tree(root, plan)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "fresh_namespace": _ROOT_NAMESPACE.as_posix(),
        "fixed_tree_path": (_ROOT_NAMESPACE / "fixed").as_posix(),
        "buggy_tree_path": (_ROOT_NAMESPACE / "buggy").as_posix(),
        "production_inverse_paths": list(plan.profile.production_paths),
        "frozen_test_paths": list(plan.profile.test_paths),
        "p2p_control_paths": list(plan.profile.p2p_test_paths),
        "fixed_test_hashes": {
            relative: hashlib.sha256((fixed / relative).read_bytes()).hexdigest()
            for relative in (*plan.profile.test_paths, *plan.profile.p2p_test_paths)
        },
        "buggy_test_hashes": {
            relative: hashlib.sha256((buggy / relative).read_bytes()).hexdigest()
            for relative in (*plan.profile.test_paths, *plan.profile.p2p_test_paths)
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "state": "materialized",
    }
    inputs = (_API_REVIEW, plan.profile.fixed_capsule_reference)
    return _write(root, _MATERIALIZATION, inputs, payload, authorization)


def _probe_source(name: str) -> str:
    if name == "map-settings-manager":
        return (
            '#include "map_settings_manager.h"\n'
            'int main() { MapSettingsManager mgr("map_meta.txt"); return 0; }\n'
        )
    if name == "settings-layers":
        return (
            '#include "settings.h"\n'
            "int main() { Settings *g = Settings::createLayer(SL_GLOBAL); "
            "return g ? 0 : 1; }\n"
        )
    raise ValueError("unknown #10661 Gate 4 API probe")


def _compile_probe(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    variant: str,
    name: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / _ROOT_NAMESPACE / variant
    probe = tree / "gate4-probes" / f"{name}.cpp"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(_probe_source(name), encoding="utf-8")
    argv = (
        str(plan.toolchain.cxx_compiler),
        "-std=c++17",
        "-fsyntax-only",
        "-I",
        str(tree / "src"),
        "-I",
        str(root / _IRRLICHT_INCLUDE),
        str(probe),
    )
    environment = {
        "HOME": str(root / "private" / "runtime-home" / "luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }
    try:
        result = subprocess.run(
            argv,
            cwd=tree,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
        outcome = "completed"
        returncode: int | None = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as error:
        outcome = "timed_out"
        returncode = None
        stdout = error.stdout or b""
        stderr = error.stderr or b""
    if len(stdout) > _MAX_OUTPUT:
        stdout = stdout[:_MAX_OUTPUT]
    if len(stderr) > _MAX_OUTPUT:
        stderr = stderr[:_MAX_OUTPUT]
    expected = "pass" if variant == "fixed" else "failure"
    observed = "pass" if outcome == "completed" and returncode == 0 else "failure"
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "variant": variant,
        "probe": name,
        "probe_source_path": probe.relative_to(root).as_posix(),
        "argv": list(argv),
        "process_outcome": outcome,
        "returncode": returncode,
        "observed_outcome": observed,
        "expected_outcome": expected,
        "matched_expected": observed == expected,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "output_encoding": "base64",
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    if name == "map-settings-manager":
        reference = _COMPILE_MAP_FIXED if variant == "fixed" else _COMPILE_MAP_BUGGY
    else:
        reference = (
            _COMPILE_SETTINGS_FIXED
            if variant == "fixed"
            else _COMPILE_SETTINGS_BUGGY
        )
    return _write(root, reference, (_MATERIALIZATION,), payload, authorization)


def run_luanti_10661_gate4_mechanical(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Prove Gate 4 incompatibility and persist a terminal receipt."""

    if confirmation != _CONFIRMATION:
        raise RuntimeError(f"confirmation must be {_CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _root(artifact_root)
    review = _load(root, _API_REVIEW)
    if review.payload.get("candidate") != _CANDIDATE.to_value():
        raise RuntimeError("#10661 API review candidate binding drifted")
    plan = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if not isinstance(plan, NativeRegressionExecutionPlan):
        raise RuntimeError("#10661 Gate 4 planner did not produce a closed plan")
    materialization = _materialize(root, plan, authorization)
    fixed_map = _compile_probe(root, plan, "fixed", "map-settings-manager", authorization)
    buggy_map = _compile_probe(root, plan, "buggy", "map-settings-manager", authorization)
    fixed_settings = _compile_probe(root, plan, "fixed", "settings-layers", authorization)
    buggy_settings = _compile_probe(root, plan, "buggy", "settings-layers", authorization)
    checks = (fixed_map, buggy_map, fixed_settings, buggy_settings)
    if (
        fixed_map.payload.get("observed_outcome") != "pass"
        or buggy_map.payload.get("observed_outcome") != "failure"
        or fixed_settings.payload.get("observed_outcome") != "pass"
        or buggy_settings.payload.get("observed_outcome") != "failure"
    ):
        raise RuntimeError("#10661 Gate 4 mechanical probe truth table is unexpected")
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "state": "terminal_gate4_api_incompatible",
        "outcome": "gate4_api_incompatible",
        "terminal": True,
        "resumable": False,
        "candidate_pool_status": "terminal_rejected",
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "accepted_issue_10555",
            "gate_3_strict_inverse_patch": "passed_candidate_specific",
            "gate_4_test_api_compatibility": "failed_fixed_test_api_against_buggy",
            "gate_5_7": "not_run",
            "gate_8_13": "not_claimed",
        },
        "mechanical_compile_proof": {
            "fixed_map_settings_manager": "pass",
            "buggy_map_settings_manager": "failure_no_one_argument_constructor",
            "fixed_settings_layers": "pass",
            "buggy_settings_layers": "failure_missing_createLayer_and_SL_GLOBAL",
            "fixed_test_files_byte_identical": True,
        },
        "compile_receipts": [item.reference.artifact_id for item in checks],
        "materialization": materialization.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "roster_modified": False,
        "complete": True,
    }
    return _write(
        root,
        _FINAL,
        (_API_REVIEW, materialization.reference, *(item.reference for item in checks)),
        payload,
        authorization,
    )


__all__ = [
    "run_luanti_10661_gate4_mechanical",
]
