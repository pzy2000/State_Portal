"""Candidate-specific native preparation and Gate 5--7 runner for Luanti #13773.

Only the retained Luanti mirror and locally registered toolchain/dependency
inputs are used.  Stable artifacts use candidate/stage identifiers and never
store content or implementation digests.  The runner is intentionally narrow:
it builds the client twice and executes only the registered ``TestKeycode``
and ``TestSettings`` modules in a fresh ``13773-native-v1`` namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeProcessReceipt,
    NativeRegressionExecutionResult,
    _AttemptSpec,
    _apply_production_inverse,
    _copy_full_capsule,
    _new_workspace,
    _ordinary_existing_file,
    _persist_aggregate,
    _persist_attempt,
    _prepare_closed_home,
    _remove_workspace,
    _validate_capsule,
    execute_luanti_native_regression,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    NativeRegressionRun,
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


_CANDIDATE = CandidateKey("luanti", 13773)
_VALUE = _CANDIDATE.to_value()
_ROOT_REL = PurePosixPath("private/fixed-capsules/luanti/13773/fixed-source")
_MIRROR_REL = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_PAIR_REL = PurePosixPath("private/materialized/luanti/13773/gate57-v1")
_CLIENT_DEP_REL = PurePosixPath("private/dependencies/luanti-client/13773/v3")
_PROFILE_ID = "luanti-13773-keycode-v1-native-gate57"
_NATIVE_NAMESPACE = "luanti-13773-native-v6"
_DIAGNOSTIC_NAMESPACE = "luanti-13773-diagnostic-v4"
_DIAGNOSTIC_LOG_REL = PurePosixPath(
    "private/native-regression-diagnostics/luanti/13773/v4"
)
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/13773/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/luanti/13773/preannotation/v2",
    "candidate_preparation",
    "2.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/13773/preannotation/v3",
    "candidate_downstream_plan",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/13773/fixed/v2", "fixed_source_capsule", "2.0"
)
_DEPENDENCY = ArtifactReference(
    "candidate_dependency_closure/luanti/13773/client/v3",
    "candidate_dependency_closure",
    "1.0",
)
_GATE4 = ArtifactReference(
    "candidate_gate4_api_check/luanti/13773/native/v6",
    "candidate_gate4_api_check",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/13773/native/v6",
    "candidate_gate57_blocker",
    "1.0",
)
_DIAGNOSTIC_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/13773/native/v4",
    "candidate_gate57_blocker",
    "1.0",
)
_DIAGNOSTIC_EXACT_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/13773/native/v5",
    "candidate_gate57_blocker",
    "1.0",
)
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/13773/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/13773/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/13773/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/13773/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
)


def prepare_luanti_13773_candidate(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Materialize/register the fixed capsule and nonexecuting downstream plan."""

    _require_scope(authorization, "candidate-screening")
    root = _ordinary_root(Path(artifact_root).absolute())
    source, patch, atomicity, manifest = tuple(_load(root, ref) for ref in _CHAIN)
    source_manifest = _read_json(root, _safe_relative(source.payload["raw_evidence_path"]))
    _validate_chain(source, patch, atomicity, manifest)
    _ensure_fixed_tree(root, source_manifest)
    capsule_payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "source_tree_path": _ROOT_REL.as_posix(),
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": ["src/unittest/test_keycode.cpp"],
        "p2p_test_paths": ["src/unittest/test_settings.cpp"],
        "support_paths": [],
    }
    capsule = _write_or_reload(
        root,
        _CAPSULE,
        (source.reference, patch.reference, manifest.reference),
        capsule_payload,
        authorization,
    )
    profile_payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "primary_issue_number": 13770,
        "authority": "github:luanti-org/luanti",
        "production_paths": ["src/client/keycode.h"],
        "test_paths": ["src/unittest/test_keycode.cpp"],
        "p2p_test_paths": ["src/unittest/test_settings.cpp"],
        "test_markers": ["KEY_NUMPAD5"],
        "f2p_selector": "TestKeycode",
        "p2p_selector": "TestSettings",
        "strict_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed_static_production_only",
            "test_api_compatibility": "pending_gate4_compile",
            "fixed_f2p_3x": "downstream_pending",
            "buggy_f2p_3x": "downstream_pending",
            "fixed_buggy_p2p_3x": "downstream_pending",
        },
        "fixed_capsule_reference": _CAPSULE.artifact_id,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    profile = _write_or_reload(
        root,
        _PROFILE,
        (source.reference, patch.reference, atomicity.reference, manifest.reference, capsule.reference),
        profile_payload,
        authorization,
    )
    preparation_payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "state": "prepared",
        "outcome": "downstream_pending",
        "terminal": False,
        "production_inverse_paths": ["src/client/keycode.h"],
        "preserved_test_paths": ["src/unittest/test_keycode.cpp"],
        "f2p": {
            "selector": "TestKeycode",
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
        },
        "p2p": {
            "selector": "TestSettings",
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
        },
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    preparation = _write_or_reload(
        root, _PREPARATION, (profile.reference,), preparation_payload, authorization
    )
    plan_payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "state": "planned",
        "terminal": False,
        "completed_stage": "test_manifest",
        "next_stage": "candidate_dependency_closure_and_gate4",
        "fixed_capsule_reference": capsule.reference.artifact_id,
        "fixed_source_tree_path": _ROOT_REL.as_posix(),
        "candidate_pair_namespace": _PAIR_REL.as_posix(),
        "dependency_reference": _DEPENDENCY.artifact_id,
        "required_order": [
            "candidate_dependency_closure",
            "gate4_api_compile",
            "fixed_buggy_materialization",
            "fixed_buggy_build",
            "f2p_p2p_repetitions",
        ],
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    plan = _write_or_reload(
        root,
        _PLAN,
        (profile.reference, preparation.reference, capsule.reference),
        plan_payload,
        authorization,
    )
    return profile, preparation, plan


def materialize_luanti_13773_client_dependencies(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Copy only the explicitly registered local freetype/JPEG/PNG inputs."""

    _require_scope(authorization, "candidate-screening")
    root = _ordinary_root(Path(artifact_root).absolute())
    destination = root / _CLIENT_DEP_REL
    inventory = _dependency_inventory(root)
    existing = _try_load(root, _DEPENDENCY)
    if existing is not None:
        _validate_dependency_destination(destination, inventory, existing)
        return existing
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#13773 dependency destination exists without receipt")
    destination.mkdir(parents=True, mode=0o700)
    copied: list[dict[str, str]] = []
    try:
        for source, relative in inventory:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() or target.is_symlink():
                raise RuntimeError("#13773 dependency destination collision")
            if source.is_dir():
                shutil.copytree(source, target, symlinks=False)
            else:
                shutil.copy2(source, target, follow_symlinks=False)
            valid_target = target.is_dir() if source.is_dir() else target.is_file()
            if target.is_symlink() or not valid_target:
                raise RuntimeError("#13773 dependency copy is unsafe")
            copied.append({"source": str(source), "destination": relative.as_posix()})
        _validate_dependency_files(destination, inventory)
    except Exception:
        raise
    payload = {
        "candidate": _VALUE,
        "state": "materialized",
        "destination_root": _CLIENT_DEP_REL.as_posix(),
        "inventory": copied,
        "copied_file_count": len(copied),
        "network_used": False,
        "candidate_execution_performed": False,
        "complete": True,
    }
    return _write_or_reload(root, _DEPENDENCY, (_CAPSULE,), payload, authorization)


def gate4_api_compile_check(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Compile a tiny fixed/buggy API fixture without running a candidate."""

    _require_scope(authorization, "candidate-screening")
    root = _ordinary_root(Path(artifact_root).absolute())
    profile, _preparation, _plan = _load_registered_plan(root)
    dependency = _load(root, _DEPENDENCY)
    tree = root / _ROOT_REL
    irr_include = tree / "lib/irrlichtmt/include"
    cxx = root / "private/dependencies/luanti-native/3.0/bin/c++"
    _ordinary_file(cxx, root)
    fixture = (
        '#include "client/keycode.h"\n'
        "int main() {\n"
        '  KeyPress key("5");\n'
        '  return key == KeyPress("KEY_NUMPAD5");\n'
        "}\n"
    )
    temp = Path(tempfile.mkdtemp(prefix="luanti-13773-gate4-", dir=root / "private"))
    try:
        source = temp / "api_check.cpp"
        source.write_text(fixture, encoding="utf-8")
        fixed_argv = (
            str(cxx),
            "-std=c++17",
            "-fsyntax-only",
            "-I",
            str(tree / "src"),
            "-I",
            str(irr_include),
            str(source),
        )
        fixed_result = subprocess.run(
            fixed_argv,
            cwd=tree,
            env={"PATH": str((root / "private/dependencies/luanti-native/3.0/bin"))},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        patch = _load(root, _CHAIN[1])
        raw_patch = _read_bytes(root, _safe_relative(patch.payload["raw_patch_path"]))
        buggy_root = temp / "buggy"
        (buggy_root / "src/client").mkdir(parents=True, mode=0o700)
        fixed_header = tree / "src/client/keycode.h"
        (buggy_root / "src/client/keycode.h").write_bytes(fixed_header.read_bytes())
        production_section = _diff_section(raw_patch, "src/client/keycode.h")
        inverse = subprocess.run(
            [str(root / "private/dependencies/luanti-native/3.0/bin/git"), "apply", "-R", "--whitespace=nowarn", "-"],
            cwd=buggy_root,
            input=production_section,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        buggy_argv = tuple(
            value if value != str(tree / "src") else str(buggy_root / "src")
            for value in fixed_argv
        )
        buggy_argv = buggy_argv[: buggy_argv.index(str(buggy_root / "src")) + 1] + (
            "-I",
            str(tree / "src"),
        ) + buggy_argv[buggy_argv.index(str(buggy_root / "src")) + 1 :]
        buggy_result = (
            subprocess.run(
                buggy_argv,
                cwd=buggy_root,
                env={"PATH": str((root / "private/dependencies/luanti-native/3.0/bin"))},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
                check=False,
            )
            if inverse.returncode == 0
            else None
        )
        fixed_passed = fixed_result.returncode == 0
        buggy_passed = buggy_result is not None and buggy_result.returncode == 0
        payload = {
            "candidate": _VALUE,
            "profile_id": profile.payload["profile_id"],
            "state": "passed" if fixed_passed and buggy_passed else "blocked",
            "compile_scope": "fixed_and_inverse_buggy_api_fixture",
            "reversed_production_only": inverse.returncode == 0,
            "fixed_source_tree_path": _ROOT_REL.as_posix(),
            "compiler_path": str(cxx.relative_to(root)),
            "fixed_returncode": fixed_result.returncode,
            "buggy_returncode": None if buggy_result is None else buggy_result.returncode,
            "fixed_stdout": fixed_result.stdout.decode("utf-8", "replace")[-16000:],
            "fixed_stderr": fixed_result.stderr.decode("utf-8", "replace")[-16000:],
            "buggy_stdout": "" if buggy_result is None else buggy_result.stdout.decode("utf-8", "replace")[-16000:],
            "buggy_stderr": inverse.stderr.decode("utf-8", "replace")[-4000:] if buggy_result is None else buggy_result.stderr.decode("utf-8", "replace")[-16000:],
            "dependency_reference": dependency.reference.artifact_id,
            "candidate_execution_performed": False,
            "complete": fixed_passed and buggy_passed,
        }
    finally:
        shutil.rmtree(temp)
    return _write_or_reload(root, _GATE4, (profile.reference, dependency.reference), payload, authorization)


def execute_luanti_13773_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = EXECUTION_CONFIRMATION,
) -> NativeRegressionExecutionResult | StableArtifact:
    """Run one fresh fixed/buggy client build and 12-cell native epoch."""

    _require_scope(authorization, "preannotation-execution")
    root = _ordinary_root(Path(artifact_root).absolute())
    _load_registered_plan(root)
    gate4 = _load(root, _GATE4)
    if gate4.payload.get("state") != "passed":
        return _write_blocker(
            root,
            (gate4.reference,),
            "gate4_api_compile_not_passed",
            authorization,
        )
    dependency = _load(root, _DEPENDENCY)
    try:
        return execute_luanti_native_regression(
            root,
            candidate=_CANDIDATE,
            authorization=authorization,
            confirmation=confirmation,
            planner=plan_luanti_13773_native_execution,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return _write_blocker(
            root,
            (gate4.reference, dependency.reference),
            type(error).__name__,
            authorization,
        )


@dataclass(frozen=True)
class _FullProcessResult:
    """A bounded process result whose complete streams are durable on disk."""

    outcome: str
    returncode: int | None
    stdout_path: Path
    stderr_path: Path
    stdout_bytes: int
    stderr_bytes: int


def execute_luanti_13773_diagnostic_v4(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = EXECUTION_CONFIRMATION,
) -> NativeRegressionExecutionResult | StableArtifact:
    """Run one fresh full-log diagnostic for #13773.

    This deliberately uses a new namespace and never loads v3 build or
    attempt receipts.  Configure/build output is streamed to ordinary files
    before its typed receipt is written, so a compiler diagnostic cannot be
    hidden by the normal bounded capture.  The fixed build is a hard gate:
    buggy build and the twelve test cells are attempted only after it passes.
    """

    _require_scope(authorization, "preannotation-execution")
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    root = _ordinary_root(Path(artifact_root).absolute())
    _load_registered_plan(root)
    gate4 = _load(root, _GATE4)
    dependency = _load(root, _DEPENDENCY)
    if gate4.payload.get("state") != "passed":
        return _write_diagnostic_blocker(
            root,
            (gate4.reference, dependency.reference),
            stage="gate4",
            reason="gate4_api_compile_not_passed",
            authorization=authorization,
        )
    decision = plan_luanti_13773_native_execution(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        return _write_diagnostic_blocker(
            root,
            (gate4.reference, dependency.reference),
            stage="plan",
            reason=str(decision.details.get("reason", "native_plan_blocked")),
            authorization=authorization,
        )
    plan = replace(decision, execution_namespace=_DIAGNOSTIC_NAMESPACE)
    _validate_capsule(root, plan)
    _prepare_closed_home(root, Path(plan.closed_environment["HOME"]))
    workspace = _new_workspace(root, _CANDIDATE)
    fixed_build: ArtifactReference | None = None
    buggy_build: ArtifactReference | None = None
    try:
        fixed_tree = workspace / "fixed"
        _copy_full_capsule(plan, fixed_tree)
        fixed_build = _diagnostic_build_variant(
            root,
            plan,
            variant="fixed",
            tree=fixed_tree,
            authorization=authorization,
        )
        if fixed_build is None:
            receipt = _diagnostic_receipt_for_variant(root, plan, "fixed")
            inputs = (gate4.reference, dependency.reference)
            if receipt is not None:
                inputs += (receipt,)
            return _write_diagnostic_blocker(
                root,
                inputs,
                stage="fixed_build",
                reason="fixed_build_failed",
                authorization=authorization,
            )

        buggy_tree = workspace / "buggy"
        _copy_full_capsule(plan, buggy_tree)
        _apply_production_inverse(plan, fixed_tree, buggy_tree)
        buggy_build = _diagnostic_build_variant(
            root,
            plan,
            variant="buggy",
            tree=buggy_tree,
            authorization=authorization,
        )
        if buggy_build is None:
            receipt = _diagnostic_receipt_for_variant(root, plan, "buggy")
            inputs = (gate4.reference, dependency.reference, fixed_build)
            if receipt is not None:
                inputs += (receipt,)
            return _write_diagnostic_blocker(
                root,
                inputs,
                stage="buggy_build",
                reason="buggy_build_failed",
                authorization=authorization,
            )

        attempts: list[StableArtifact] = []
        for run in plan.runs:
            for variant, tree, build_reference in (
                ("fixed", fixed_tree, fixed_build),
                ("buggy", buggy_tree, buggy_build),
            ):
                for repetition in range(1, run.repetitions + 1):
                    spec = _AttemptSpec(run, variant, repetition)
                    result = _run_full_process(
                        tuple(str(value) for value in (tree / run.executable_path, *run.argv[1:])),
                        cwd=tree,
                        env=dict(plan.closed_environment),
                        timeout_seconds=300,
                        log_relative=PurePosixPath(
                            "attempt"
                        ) / run.phase / variant / str(repetition),
                        root=root,
                    )
                    receipt = NativeProcessReceipt(
                        result.outcome, result.returncode,
                        result.stdout_path.read_bytes(),
                        result.stderr_path.read_bytes(),
                    )
                    attempts.append(
                        _persist_attempt(
                            root,
                            plan,
                            spec,
                            build_reference,
                            receipt,
                            authorization,
                        )
                    )
        aggregate = _persist_aggregate(root, plan, attempts, authorization)
        return NativeRegressionExecutionResult(
            candidate=_CANDIDATE,
            aggregate=aggregate,
            executed_build_steps=4,
            executed_attempts=len(attempts),
            resumed_attempts=0,
            qualified=aggregate.payload["qualified"] is True,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        inputs: tuple[ArtifactReference, ...] = (gate4.reference, dependency.reference)
        if fixed_build is not None:
            inputs += (fixed_build,)
        if buggy_build is not None:
            inputs += (buggy_build,)
        return _write_diagnostic_blocker(
            root,
            inputs,
            stage="test_cells",
            reason=type(error).__name__,
            authorization=authorization,
        )
    finally:
        _remove_workspace(workspace, root)


def record_luanti_13773_diagnostic_v4_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind the complete v4 compiler diagnostic as a precise blocker."""

    _require_scope(authorization, "preannotation-execution")
    root = _ordinary_root(Path(artifact_root).absolute())
    gate4 = _load(root, _GATE4)
    dependency = _load(root, _DEPENDENCY)
    decision = plan_luanti_13773_native_execution(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError("#13773 diagnostic v4 plan is unavailable")
    build = _load(
        root,
        _diagnostic_build_reference(
            replace(
                decision,
                execution_namespace=_DIAGNOSTIC_NAMESPACE,
            ),
            "fixed",
            "build",
        ),
    )
    stdout_path = root / PurePosixPath(build.payload["stdout_path"])
    stderr_path = root / PurePosixPath(build.payload["stderr_path"])
    _ordinary_file(stdout_path, root)
    _ordinary_file(stderr_path, root)
    diagnostic = stdout_path.read_text(encoding="utf-8", errors="replace")
    marker = "fatal error: 'irrUString.h' file not found"
    if build.payload.get("returncode") != 1 or marker not in diagnostic:
        raise RuntimeError("#13773 diagnostic v4 exact blocker evidence is unavailable")
    payload = {
        "candidate": _VALUE,
        "state": "infrastructure_blocked",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "diagnostic_namespace": _DIAGNOSTIC_NAMESPACE,
        "stage": "fixed_build",
        "reason": "missing_irrlicht_header",
        "compiler_diagnostic": (
            "src/client/fontengine.cpp -> src/irrlicht_changes/CGUITTFont.h:38:10: "
            "fatal error: 'irrUString.h' file not found"
        ),
        "build_receipt": build.reference.artifact_id,
        "stdout_path": build.payload["stdout_path"],
        "stderr_path": build.payload["stderr_path"],
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": (
            "repair the registered fixed capsule/Irrlicht header closure, then use a new diagnostic namespace"
        ),
    }
    return _write_or_reload(
        root,
        _DIAGNOSTIC_EXACT_BLOCKER,
        (gate4.reference, dependency.reference, build.reference),
        payload,
        authorization,
    )


def _diagnostic_build_variant(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    variant: str,
    tree: Path,
    authorization: AuthorizationRecord,
) -> ArtifactReference | None:
    """Run configure/build with complete stream files for one variant."""

    last_reference: ArtifactReference | None = None
    for step, raw_argv, timeout_seconds in (
        ("configure", plan.configure_argv, 600),
        ("build", plan.build_argv, 1800),
    ):
        result = _run_full_process(
            tuple(str(value) for value in raw_argv),
            cwd=tree,
            env=dict(plan.closed_environment),
            timeout_seconds=timeout_seconds,
            log_relative=PurePosixPath(step) / variant,
            root=root,
        )
        reference = _write_diagnostic_build_receipt(
            root,
            plan,
            variant=variant,
            step=step,
            argv=tuple(str(value) for value in raw_argv),
            result=result,
            authorization=authorization,
        )
        last_reference = reference
        if result.outcome != "completed" or result.returncode != 0:
            return None
    _ordinary_existing_file(tree / plan.runs[0].executable_path, tree)
    return last_reference


def _run_full_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    log_relative: PurePosixPath,
    root: Path,
) -> _FullProcessResult:
    if not argv or timeout_seconds <= 0:
        raise ValueError("#13773 diagnostic process invocation is invalid")
    log_root = root / _DIAGNOSTIC_LOG_REL / log_relative
    _ensure_fresh_directory(log_root, root)
    stdout_path = log_root / "stdout.log"
    stderr_path = log_root / "stderr.log"
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(str(error).encode("utf-8", "replace"))
        return _FullProcessResult(
            "launch_failed", None, stdout_path, stderr_path, 0, stderr_path.stat().st_size
        )

    def drain(stream: object, destination: Path) -> None:
        assert hasattr(stream, "read")
        with destination.open("xb") as handle:
            while chunk := stream.read(65_536):
                handle.write(chunk)
                handle.flush()

    threads = (
        threading.Thread(target=drain, args=(process.stdout, stdout_path)),
        threading.Thread(target=drain, args=(process.stderr, stderr_path)),
    )
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
        outcome = "completed"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait()
        returncode = None
        outcome = "timed_out"
    for thread in threads:
        thread.join()
    assert stdout_path.is_file() and stderr_path.is_file()
    return _FullProcessResult(
        outcome,
        returncode,
        stdout_path,
        stderr_path,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )


def _diagnostic_build_reference(
    plan: NativeRegressionExecutionPlan, variant: str, step: str
) -> ArtifactReference:
    return ArtifactReference(
        f"native_regression_build/luanti/13773/{variant}/{step}/{_DIAGNOSTIC_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )


def _diagnostic_receipt_for_variant(
    root: Path, plan: NativeRegressionExecutionPlan, variant: str
) -> ArtifactReference | None:
    for step in ("build", "configure"):
        reference = _diagnostic_build_reference(plan, variant, step)
        if _try_load(root, reference) is not None:
            return reference
    return None


def _write_diagnostic_build_receipt(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    result: _FullProcessResult,
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    reference = _diagnostic_build_reference(plan, variant, step)
    payload = {
        "candidate": _VALUE,
        "profile_id": plan.profile.profile_id,
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": result.outcome,
        "returncode": result.returncode,
        "stdout_path": result.stdout_path.relative_to(root).as_posix(),
        "stderr_path": result.stderr_path.relative_to(root).as_posix(),
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "output_complete": True,
        "output_encoding": "raw",
        "diagnostic_namespace": _DIAGNOSTIC_NAMESPACE,
    }
    _write_or_reload(
        root,
        reference,
        (plan.profile.fixed_capsule_reference, *plan.dependency_references),
        payload,
        authorization,
    )
    return reference


def _write_diagnostic_blocker(
    root: Path,
    inputs: tuple[ArtifactReference, ...],
    *,
    stage: str,
    reason: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _VALUE,
        "state": "infrastructure_blocked",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "diagnostic_namespace": _DIAGNOSTIC_NAMESPACE,
        "stage": stage,
        "reason": reason,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "inspect the complete diagnostic stream files and repair only the typed blocker",
    }
    return _write_or_reload(root, _DIAGNOSTIC_BLOCKER, inputs, payload, authorization)


def _ensure_fresh_directory(path: Path, root: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"#13773 diagnostic log namespace already exists: {path}")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ordinary_root(root)
    path.mkdir(mode=0o700)


def plan_luanti_13773_native_execution(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> NativeRegressionExecutionPlan | StageDecision:
    """Adapt the shared closed planner to the client-only TestKeycode build."""

    if candidate != _CANDIDATE:
        raise ValueError("#13773 planner received the wrong candidate")
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = plan_luanti_native_regression_execution(root, candidate=candidate)
    if isinstance(plan, StageDecision):
        return plan
    dependency = _load(root, _DEPENDENCY)
    configure = tuple(
        "-DBUILD_CLIENT=TRUE" if value == "-DBUILD_CLIENT=FALSE" else
        "-DBUILD_SERVER=FALSE" if value == "-DBUILD_SERVER=TRUE" else value
        for value in plan.configure_argv
    )
    configure = configure + (
        "-DENABLE_SOUND=FALSE",
        f"-DCMAKE_PREFIX_PATH={root / _CLIENT_DEP_REL}",
        f"-DFREETYPE_INCLUDE_DIRS={root / _CLIENT_DEP_REL / 'packages/freetype/include/freetype2'}",
        f"-DFREETYPE_LIBRARY={root / _CLIENT_DEP_REL / 'packages/freetype/lib/libfreetype.dylib'}",
        f"-DJPEG_INCLUDE_DIR={root / _CLIENT_DEP_REL / 'packages/jpeg/include'}",
        f"-DJPEG_LIBRARY={root / _CLIENT_DEP_REL / 'packages/jpeg/lib/libjpeg.dylib'}",
        f"-DPNG_PNG_INCLUDE_DIR={root / _CLIENT_DEP_REL / 'packages/png/include'}",
        f"-DPNG_LIBRARY={root / _CLIENT_DEP_REL / 'packages/png/lib/libpng16.dylib'}",
        "-DCMAKE_OSX_SYSROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
        "-DOPENGL_INCLUDE_DIR=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/OpenGL.framework/Headers",
        "-DOPENGL_gl_LIBRARY=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/OpenGL.framework/OpenGL.tbd",
        "-DCMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH=FALSE",
        f"-DCMAKE_CXX_FLAGS=-I{root / _CLIENT_DEP_REL / 'packages/irrlicht/include'}",
    )
    executable = "bin/minetest"
    runs = (
        NativeRegressionRun(
            phase="f2p",
            selector="TestKeycode",
            executable_path=executable,
            argv=(executable, "--run-unittests", "--test-module", "TestKeycode"),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="failure",
        ),
        NativeRegressionRun(
            phase="p2p",
            selector="TestSettings",
            executable_path=executable,
            argv=(executable, "--run-unittests", "--test-module", "TestSettings"),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="pass",
        ),
    )
    return replace(
        plan,
        configure_argv=configure,
        build_argv=(
            plan.toolchain.cmake,
            "--build",
            "build-luanti-13773",
            "--target",
            "minetest",
            "--parallel",
            "1",
        ),
        runs=runs,
        execution_namespace=_NATIVE_NAMESPACE,
        dependency_references=(dependency.reference,),
    )


def _load_registered_plan(root: Path) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    plan = _load(root, _PLAN)
    if (
        profile.payload.get("candidate") != _VALUE
        or preparation.payload.get("candidate") != _VALUE
        or plan.payload.get("candidate") != _VALUE
        or profile.payload.get("candidate_execution_performed") is not False
        or preparation.payload.get("candidate_execution_performed") is not False
        or plan.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("#13773 registered profile/preparation/plan drifted")
    return profile, preparation, plan


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if (
        source.payload.get("candidate") != _VALUE
        or patch.payload.get("candidate") != _VALUE
        or atomicity.payload.get("candidate") != _VALUE
        or manifest.payload.get("candidate") != _VALUE
        or source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [13770]
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != ["src/unittest/test_keycode.cpp"]
    ):
        raise RuntimeError("#13773 current-v2 source/patch chain is invalid")


def _ensure_fixed_tree(root: Path, source_manifest: dict[str, Any]) -> None:
    destination = root / _ROOT_REL
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("#13773 fixed capsule tree is unsafe")
        _validate_fixed_tree(destination)
        return
    pull = source_manifest.get("pull")
    merge_revision = pull.get("merge_commit_sha") if isinstance(pull, dict) else None
    if not isinstance(merge_revision, str) or not merge_revision:
        raise RuntimeError("#13773 merged revision is unavailable")
    mirror = root / _MIRROR_REL
    if mirror.is_symlink() or not mirror.is_dir():
        raise RuntimeError("#13773 retained Luanti mirror is unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    archive = subprocess.run(
        ["git", "--git-dir", str(mirror), "archive", "--format=tar", merge_revision],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if archive.returncode != 0 or not archive.stdout:
        raise RuntimeError("#13773 fixed capsule archive failed")
    import tarfile
    from io import BytesIO

    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as stream:
        for member in stream.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError("#13773 fixed capsule archive escaped")
        stream.extractall(destination)
    _validate_fixed_tree(destination)


def _validate_fixed_tree(tree: Path) -> None:
    for relative in (
        "CMakeLists.txt",
        "src/client/keycode.h",
        "src/unittest/test_keycode.cpp",
        "src/unittest/test_settings.cpp",
        "lib/irrlichtmt/CMakeLists.txt",
        "lib/irrlichtmt/include/irrlicht.h",
    ):
        _ordinary_file(tree / relative, tree)


def _dependency_inventory(root: Path) -> tuple[tuple[Path, PurePosixPath], ...]:
    home = Path("/opt/homebrew")
    # Package complete include directories. JPEG's public header includes
    # jmorecfg.h (and related companion headers), so copying only jpeglib.h
    # leaves a syntactically valid but unusable dependency closure.
    png_include = (home / "include/png.h").resolve(strict=True).parent
    jpeg_include = (home / "include/jpeglib.h").resolve(strict=True).parent
    paths = (
        (home / "include/freetype2", PurePosixPath("packages/freetype/include/freetype2")),
        (png_include, PurePosixPath("packages/png/include")),
        (jpeg_include, PurePosixPath("packages/jpeg/include")),
        (
            root / _ROOT_REL / "lib/irrlichtmt/include/irrString.h",
            PurePosixPath("packages/irrlicht/include/irrUString.h"),
        ),
        (home / "lib/libfreetype.dylib", PurePosixPath("packages/freetype/lib/libfreetype.dylib")),
        (home / "lib/libpng16.dylib", PurePosixPath("packages/png/lib/libpng16.dylib")),
        (home / "lib/libjpeg.dylib", PurePosixPath("packages/jpeg/lib/libjpeg.dylib")),
    )
    resolved: list[tuple[Path, PurePosixPath]] = []
    for source, relative in paths:
        if not source.exists():
            raise RuntimeError(f"#13773 local client dependency is missing: {source}")
        actual = source.resolve(strict=True)
        if not actual.is_file() and not actual.is_dir():
            raise RuntimeError(f"#13773 local client dependency is unsafe: {source}")
        resolved.append((actual, relative))
    return tuple(resolved)


def _validate_dependency_destination(
    destination: Path,
    inventory: tuple[tuple[Path, PurePosixPath], ...],
    artifact: StableArtifact,
) -> None:
    _validate_dependency_files(destination, inventory)
    if artifact.inputs != (_CAPSULE,) or artifact.payload.get("state") != "materialized":
        raise RuntimeError("#13773 client dependency receipt is invalid")


def _validate_dependency_files(
    destination: Path, inventory: tuple[tuple[Path, PurePosixPath], ...]
) -> None:
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError("#13773 dependency destination is unsafe")
    for source, relative in inventory:
        target = destination / relative
        if source.is_dir():
            if target.is_symlink() or not target.is_dir():
                raise RuntimeError("#13773 dependency directory is invalid")
            for item in target.rglob("*"):
                if item.is_symlink():
                    raise RuntimeError("#13773 dependency contains a symlink")
        elif target.is_symlink() or not target.is_file():
            raise RuntimeError("#13773 dependency file is invalid")


def _write_blocker(
    root: Path,
    inputs: tuple[ArtifactReference, ...],
    reason: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _VALUE,
        "state": "infrastructure_blocked",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "reason": reason,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "repair the typed dependency/toolchain gap, then resume the fresh namespace",
    }
    return _write_or_reload(root, _BLOCKER, inputs, payload, authorization)


def _write_or_reload(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#13773 artifact conflicts: {reference.artifact_id}")
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
        raise RuntimeError(f"#13773 typed reload mismatch: {reference.artifact_id}")
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


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#13773 artifact root is unsafe")
    return path.resolve(strict=True)


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"#13773 required file is missing: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"#13773 required file is unsafe: {path}")
    return path


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("#13773 source path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise RuntimeError("#13773 source path is unsafe")
    return value


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / PurePosixPath(relative)
    _ordinary_file(path, root)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#13773 source manifest is invalid")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    path = root / PurePosixPath(relative)
    _ordinary_file(path, root)
    return path.read_bytes()


def _diff_section(raw_patch: bytes, path: str) -> bytes:
    lines = raw_patch.splitlines(keepends=True)
    sections: list[tuple[str, list[bytes]]] = []
    current: list[bytes] = []
    current_path: str | None = None
    for line in lines:
        if line.startswith(b"diff --git "):
            if current_path is not None:
                sections.append((current_path, current))
            fields = line.decode("utf-8", "surrogateescape").split()
            if len(fields) != 4 or not fields[2].startswith("a/"):
                raise RuntimeError("#13773 raw patch header is invalid")
            current_path = fields[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, current))
    for section_path, section_lines in sections:
        if section_path == path:
            return b"".join(section_lines)
    raise RuntimeError(f"#13773 raw patch section missing: {path}")


def _require_scope(authorization: AuthorizationRecord, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"#13773 {scope} authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "execute_luanti_13773_gate57",
    "gate4_api_compile_check",
    "materialize_luanti_13773_client_dependencies",
    "plan_luanti_13773_native_execution",
    "prepare_luanti_13773_candidate",
]
