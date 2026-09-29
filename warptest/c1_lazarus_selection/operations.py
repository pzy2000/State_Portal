"""Concrete mechanical gates for the pre-annotation candidate pipeline."""

from __future__ import annotations

import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from .epoch import (
    SELECTION_EPOCH_V1,
    SelectionEpoch,
    require_selection_epoch,
)
from .luanti_v2_test_manifest import classify_luanti_v2_test_manifest
from .models import CandidateKey
from .pipeline import StageDecision, V2PreannotationBatchContext
from .resource_guard import require_write_capacity
from .screening import resolve_raw_patch_evidence
from .source_atomicity import (
    load_source_atomicity_artifact,
    load_source_atomicity_batch_context,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_EXECUTABLE_TEST_SUFFIXES = frozenset(
    {".c", ".cc", ".cpp", ".cs", ".js", ".jsx", ".lua", ".py", ".ts", ".tsx"}
)
_ASSERTION = re.compile(
    r"(?:^|\W)(?:assert|expect|it|test)(?:\.|\s*\()",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ProcessCapture:
    status: Literal["completed", "timeout", "failed_to_start"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    elapsed_ms: float

    def __post_init__(self) -> None:
        if self.status == "completed":
            if type(self.exit_code) is not int:
                raise ValueError("completed process capture lacks an exit code")
        elif self.status in {"timeout", "failed_to_start"}:
            if self.exit_code is not None:
                raise ValueError("incomplete process capture has an exit code")
        else:
            raise ValueError("process capture status is invalid")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise ValueError("process capture output is invalid")
        if type(self.elapsed_ms) is not float or self.elapsed_ms < 0:
            raise ValueError("process capture elapsed time is invalid")


def evaluate_test_manifest(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
    batch_context: V2PreannotationBatchContext | None = None,
) -> StageDecision:
    """Require test evidence sufficient to proceed to execution, not qualification."""

    epoch = require_selection_epoch(selection_epoch)
    root = Path(artifact_root).resolve(strict=True)
    if epoch.is_v2:
        _load_accepted_atomicity_reference(
            root,
            candidate,
            batch_context=batch_context,
        )
    artifact = (
        _load_patch_artifact(root, candidate, epoch)
        if batch_context is None
        else _load_patch_artifact(
            root,
            candidate,
            epoch,
            batch_context=batch_context,
        )
    )
    payload = artifact.payload
    if (
        payload.get("candidate") != candidate.to_value()
        or payload.get("outcome") != "patch_eligible"
        or payload.get("complete") is not True
    ):
        raise RuntimeError("candidate patch is not eligible")
    paths = payload.get("test_paths")
    if (
        type(paths) is not list
        or not paths
        or any(type(path) is not str for path in paths)
    ):
        raise RuntimeError("candidate patch test inventory is invalid")
    patch_path = _safe_raw_path(root, payload.get("raw_patch_path"))
    patch_content = patch_path.read_bytes()
    if epoch.is_v2 and candidate.repository_key == "luanti":
        luanti_decision = classify_luanti_v2_test_manifest(
            patch_content,
            declared_test_paths=tuple(paths),
            candidate_change_number=candidate.change_number,
        )
        if luanti_decision.state == "rejected":
            return StageDecision.terminal(
                "quality_rejected",
                {"reason": luanti_decision.reason},
            )
        return StageDecision.passed(
            {
                "executable_test_paths": list(luanti_decision.executable_test_paths),
                "test_count": len(luanti_decision.executable_test_paths),
                "execution_candidate_kind": luanti_decision.evidence_kind,
            }
        )
    sections = _patch_sections(patch_content)
    executable = tuple(
        sorted(
            path
            for path in paths
            if _is_executable_test(path)
            and _contains_added_assertion(sections.get(path, ()))
        )
    )
    if not executable:
        return StageDecision.terminal(
            "quality_rejected",
            {"reason": "no_executable_regression_assertion"},
        )
    return StageDecision.passed(
        {
            "executable_test_paths": list(executable),
            "test_count": len(executable),
        }
    )


def evaluate_test_repetitions(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
    repetitions: int = 3,
    timeout_s: float = 900.0,
) -> StageDecision:
    """Run or resume the exact fixed/buggy regression-test repetitions."""

    if repetitions != 3:
        raise ValueError("candidate screening requires exactly three repetitions")
    if type(timeout_s) is not float or not 0 < timeout_s <= 900:
        raise ValueError("candidate test timeout is invalid")
    epoch = require_selection_epoch(selection_epoch)
    root = Path(artifact_root).resolve(strict=True)
    manifest = _load_test_manifest(root, candidate, epoch)
    test_paths = tuple(manifest.payload["details"]["executable_test_paths"])
    plans = _test_commands(candidate.repository_key, test_paths)
    if plans is None:
        return StageDecision.terminal(
            "version_incompatible",
            {"reason": "registered_test_profile_is_not_supported_on_this_host"},
        )
    source = _repository_root(Path(workspace_root), candidate.repository_key)
    patch = _load_patch_artifact(root, candidate, epoch)
    production_paths = tuple(patch.payload["production_paths"])
    patch_path = _safe_raw_path(root, patch.payload["raw_patch_path"])
    attempt_ids: list[str] = []
    fixed_results: list[bool] = []
    buggy_results: list[bool] = []

    worktree_parent = root / "private" / "test-worktrees"
    worktree_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"{candidate.repository_key}-{candidate.change_number}-",
        dir=worktree_parent,
    ) as temporary:
        temporary_root = Path(temporary)
        fixed = temporary_root / "fixed"
        buggy = temporary_root / "buggy"
        _clone_repository(source, fixed)
        _clone_repository(source, buggy)
        _apply_buggy_overlay(
            root=buggy,
            patch=patch_path.read_bytes(),
            production_paths=production_paths,
        )
        for repetition in range(1, repetitions + 1):
            for version, version_root, outcomes in (
                ("fixed", fixed, fixed_results),
                ("buggy", buggy, buggy_results),
            ):
                artifact, passed = _load_or_run_test_attempt(
                    root,
                    candidate=candidate,
                    manifest=manifest,
                    version=version,
                    repetition=repetition,
                    version_root=version_root,
                    plans=plans,
                    timeout_s=timeout_s,
                    authorization=authorization,
                    epoch=epoch,
                )
                attempt_ids.append(artifact.reference.artifact_id)
                outcomes.append(passed)

    evidence = [
        {
            "artifact_id": artifact_id,
            "artifact_kind": "candidate_test_attempt",
            "semantic_version": epoch.semantic_version,
        }
        for artifact_id in attempt_ids
    ]
    details = {
        "fixed_passes": sum(fixed_results),
        "buggy_failures": sum(not value for value in buggy_results),
        "attempt_artifact_ids": attempt_ids,
        "evidence_artifacts": evidence,
    }
    if not all(fixed_results):
        if any(fixed_results):
            return StageDecision.terminal("flake_observed", details)
        return StageDecision.terminal("version_incompatible", details)
    if any(buggy_results):
        if not all(buggy_results):
            return StageDecision.terminal("flake_observed", details)
        return StageDecision.terminal("quality_rejected", details)
    return StageDecision.passed(details)


def _load_test_manifest(
    root: Path,
    candidate: CandidateKey,
    epoch: SelectionEpoch,
) -> StableArtifact:
    artifact = load_stable_artifact(
        root,
        artifact_id=(
            f"candidate_stage/{candidate.repository_key}/{candidate.change_number}/"
            f"test_manifest/{epoch.label}"
        ),
        artifact_kind="candidate_stage",
        semantic_version=epoch.semantic_version,
    )
    details = artifact.payload.get("details")
    paths = details.get("executable_test_paths") if type(details) is dict else None
    if (
        artifact.payload.get("candidate") != candidate.to_value()
        or artifact.payload.get("stage") != "test_manifest"
        or artifact.payload.get("state") != "passed"
        or type(paths) is not list
        or not paths
        or any(type(path) is not str for path in paths)
    ):
        raise RuntimeError("candidate test manifest is invalid")
    patch = _load_patch_artifact(root, candidate, epoch)
    expected_prefix = (patch.reference,)
    if epoch.is_v2:
        expected_prefix = (
            patch.reference,
            _load_accepted_atomicity_reference(root, candidate),
        )
    if artifact.inputs[: len(expected_prefix)] != expected_prefix:
        raise RuntimeError("candidate test manifest patch prerequisite is invalid")
    if epoch.is_v2 and any(
        reference.semantic_version != epoch.semantic_version
        for reference in artifact.inputs[len(expected_prefix) :]
    ):
        raise RuntimeError("v2 test manifest evidence is not v2")
    return artifact


def _load_accepted_atomicity_reference(
    root: Path,
    candidate: CandidateKey,
    *,
    batch_context: V2PreannotationBatchContext | None = None,
) -> ArtifactReference:
    atomicity_context = (
        batch_context.source_atomicity_context if batch_context is not None else None
    )
    if atomicity_context is None:
        atomicity_context = load_source_atomicity_batch_context(
            root,
            repository_key=candidate.repository_key,
        )
    atomicity = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
    )
    if atomicity.outcome != "accepted" or atomicity.primary_issue_number is None:
        raise RuntimeError("v2 source atomicity is not accepted")
    return atomicity.artifact.reference


def _load_patch_artifact(
    root: Path,
    candidate: CandidateKey,
    epoch: SelectionEpoch,
    batch_context: V2PreannotationBatchContext | None = None,
) -> StableArtifact:
    artifact = load_stable_artifact(
        root,
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/{epoch.label}"
        ),
        artifact_kind="candidate_test_patch",
        semantic_version=epoch.semantic_version,
    )
    production = artifact.payload.get("production_paths")
    if (
        artifact.payload.get("candidate") != candidate.to_value()
        or artifact.payload.get("outcome") != "patch_eligible"
        or artifact.payload.get("complete") is not True
        or type(production) is not list
        or not production
        or any(type(path) is not str for path in production)
    ):
        raise RuntimeError("candidate test patch is invalid")
    if epoch.is_v2:
        if batch_context is None:
            _validate_v2_patch_root(root, candidate, artifact, epoch)
        else:
            _validate_v2_patch_root_from_context(
                root,
                candidate,
                artifact,
                epoch,
                batch_context,
            )
    return artifact


def _validate_v2_patch_root_from_context(
    root: Path,
    candidate: CandidateKey,
    patch: StableArtifact,
    epoch: SelectionEpoch,
    batch_context: V2PreannotationBatchContext,
) -> None:
    if type(batch_context) is not V2PreannotationBatchContext:
        raise TypeError("v2 pre-annotation batch context is not typed")
    if batch_context.artifact_root != root:
        raise RuntimeError("v2 pre-annotation batch root is inconsistent")
    if batch_context.repository_key != candidate.repository_key:
        raise RuntimeError("v2 pre-annotation batch repository is inconsistent")
    if candidate.change_number not in batch_context.candidate_numbers:
        raise RuntimeError("v2 candidate is outside the batch enumeration")
    enumeration_reference = epoch.enumeration_reference(candidate.repository_key)
    if batch_context.enumeration_reference != enumeration_reference:
        raise RuntimeError("v2 pre-annotation batch enumeration is inconsistent")
    _validate_v2_patch_dependencies(
        root,
        candidate,
        patch,
        epoch,
        enumeration_reference,
    )


def _validate_v2_patch_root(
    root: Path,
    candidate: CandidateKey,
    patch: StableArtifact,
    epoch: SelectionEpoch,
) -> None:
    """Require the exact v2 enumeration/source/patch/screening closure."""

    enumeration_reference = epoch.enumeration_reference(candidate.repository_key)
    enumeration = load_stable_artifact(
        root,
        artifact_id=enumeration_reference.artifact_id,
        artifact_kind=enumeration_reference.artifact_kind,
        semantic_version=enumeration_reference.semantic_version,
    )
    candidates = enumeration.payload.get("candidates")
    if (
        enumeration.payload.get("repository_key") != candidate.repository_key
        or enumeration.payload.get("complete") is not True
        or type(candidates) is not list
    ):
        raise RuntimeError("v2 repository enumeration is invalid")
    try:
        enumerated = tuple(CandidateKey.from_value(value) for value in candidates)
    except ValueError as error:
        raise RuntimeError(
            "v2 repository enumeration candidates are invalid"
        ) from error
    if candidate not in enumerated:
        raise RuntimeError("v2 candidate is outside the deep-screen population")

    _validate_v2_patch_dependencies(
        root,
        candidate,
        patch,
        epoch,
        enumeration_reference,
    )


def _validate_v2_patch_dependencies(
    root: Path,
    candidate: CandidateKey,
    patch: StableArtifact,
    epoch: SelectionEpoch,
    enumeration_reference: ArtifactReference,
) -> None:
    source_reference = ArtifactReference(
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/{epoch.label}"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version=epoch.semantic_version,
    )
    source = load_stable_artifact(
        root,
        artifact_id=source_reference.artifact_id,
        artifact_kind=source_reference.artifact_kind,
        semantic_version=source_reference.semantic_version,
    )
    if source.inputs != (enumeration_reference,):
        raise RuntimeError("v2 source packet enumeration input is invalid")
    if (
        source.payload.get("candidate") != candidate.to_value()
        or type(source.payload.get("raw_evidence_path")) is not str
        or not source.payload["raw_evidence_path"]
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 source packet binding is invalid")
    raw_patch_evidence = resolve_raw_patch_evidence(
        root,
        candidate=candidate,
        source_manifest_path=source.payload["raw_evidence_path"],
    )
    if patch.inputs != (enumeration_reference, source_reference):
        raise RuntimeError("v2 test patch source prerequisite is invalid")
    test_paths = patch.payload.get("test_paths")
    if (
        type(test_paths) is not list
        or not test_paths
        or any(type(path) is not str or not path for path in test_paths)
    ):
        raise RuntimeError("v2 test patch inventory is invalid")
    if (
        patch.payload.get("source_manifest_path")
        != raw_patch_evidence.source_manifest_path
        or patch.payload.get("raw_patch_path") != raw_patch_evidence.raw_patch_path
    ):
        raise RuntimeError("v2 test patch raw evidence binding is invalid")
    checkpoint = load_stable_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/{epoch.label}"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version=epoch.semantic_version,
    )
    if checkpoint.inputs != (enumeration_reference, patch.reference):
        raise RuntimeError("v2 screening checkpoint patch prerequisite is invalid")
    if (
        checkpoint.payload.get("candidate") != candidate.to_value()
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != patch.reference.artifact_id
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")


def _test_commands(
    repository_key: str,
    paths: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    if repository_key != "jupyterlab":
        raise RuntimeError("candidate repository has no registered test profile")
    if any(path.startswith("galata/") for path in paths):
        return None
    packages: dict[str, list[str]] = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[0] != "packages":
            raise RuntimeError("JupyterLab test path has no registered package runner")
        package = parts[1]
        packages.setdefault(package, []).append("/".join(parts[2:]))
    return tuple(
        (
            f"packages/{package}",
            ("../../node_modules/.bin/jest", *sorted(values), "--runInBand"),
        )
        for package, values in sorted(packages.items())
    )


def _repository_root(workspace_root: Path, repository_key: str) -> Path:
    candidate = workspace_root.resolve(strict=True) / "games" / repository_key
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("candidate repository root is unsafe")
    return candidate.resolve(strict=True)


def _clone_repository(source: Path, destination: Path) -> None:
    completed = subprocess.run(
        ("/bin/cp", "-cR", f"{source}/.", str(destination)),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("copy-on-write candidate test clone failed")
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("candidate test clone root is unsafe")


def _apply_buggy_overlay(
    *,
    root: Path,
    patch: bytes,
    production_paths: tuple[str, ...],
) -> None:
    production_patch = _select_patch_files(patch, frozenset(production_paths))
    completed = subprocess.run(
        ("git", "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=root,
        input=production_patch,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("candidate buggy overlay cannot be materialized exactly")


def _load_or_run_test_attempt(
    root: Path,
    *,
    candidate: CandidateKey,
    manifest: StableArtifact,
    version: str,
    repetition: int,
    version_root: Path,
    plans: tuple[tuple[str, tuple[str, ...]], ...],
    timeout_s: float,
    authorization: AuthorizationRecord,
    epoch: SelectionEpoch,
) -> tuple[StableArtifact, bool]:
    artifact_id = (
        f"candidate_test_attempt/{candidate.repository_key}/{candidate.change_number}/"
        f"{version}/{repetition}/{epoch.label}"
    )
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind="candidate_test_attempt",
            semantic_version=epoch.semantic_version,
        )
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        return artifact, _validate_test_attempt(
            artifact,
            candidate=candidate,
            manifest=manifest,
            version=version,
            repetition=repetition,
            plans=plans,
            epoch=epoch,
        )

    capture = _execute_test_plan(
        version=version,
        root=version_root,
        plans=plans,
        timeout_s=timeout_s,
    )
    raw_base = PurePosixPath(
        "raw",
        "tests",
        candidate.repository_key,
        str(candidate.change_number),
        epoch.label,
        version,
        str(repetition),
    )
    stdout_path = raw_base / "stdout.bin"
    stderr_path = raw_base / "stderr.bin"
    _write_raw_once(root, stdout_path, capture.stdout)
    _write_raw_once(root, stderr_path, capture.stderr)
    passed = capture.status == "completed" and capture.exit_code == 0
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="candidate_test_attempt",
        semantic_version=epoch.semantic_version,
        created_at=_now(),
        inputs=(manifest.reference,),
        payload={
            "candidate": candidate.to_value(),
            "version": version,
            "repetition": repetition,
            "commands": [{"cwd": cwd, "argv": list(argv)} for cwd, argv in plans],
            "status": capture.status,
            "exit_code": capture.exit_code,
            "elapsed_ms": capture.elapsed_ms,
            "stdout_path": stdout_path.as_posix(),
            "stderr_path": stderr_path.as_posix(),
            "passed": passed,
            "complete": True,
        },
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="candidate_test_attempt",
        semantic_version=epoch.semantic_version,
    )
    return artifact, _validate_test_attempt(
        artifact,
        candidate=candidate,
        manifest=manifest,
        version=version,
        repetition=repetition,
        plans=plans,
        epoch=epoch,
    )


def _execute_test_plan(
    *,
    version: str,
    root: Path,
    plans: tuple[tuple[str, tuple[str, ...]], ...],
    timeout_s: float,
) -> ProcessCapture:
    outputs: list[ProcessCapture] = []
    for cwd, argv in plans:
        capture = _execute_process(
            version=version,
            root=root,
            argv=argv,
            cwd=cwd,
            timeout_s=timeout_s,
        )
        outputs.append(capture)
        if capture.status != "completed" or capture.exit_code != 0:
            break
    status = next(
        (capture.status for capture in outputs if capture.status != "completed"),
        "completed",
    )
    exit_code = (
        None
        if status != "completed"
        else next(
            (capture.exit_code for capture in outputs if capture.exit_code != 0),
            0,
        )
    )
    return ProcessCapture(
        status=status,
        exit_code=exit_code,
        stdout=b"\n".join(capture.stdout for capture in outputs),
        stderr=b"\n".join(capture.stderr for capture in outputs),
        elapsed_ms=float(sum(capture.elapsed_ms for capture in outputs)),
    )


def _execute_process(
    *,
    version: str,
    root: Path,
    argv: tuple[str, ...],
    cwd: str,
    timeout_s: float,
) -> ProcessCapture:
    del version
    started = time.monotonic()
    working_directory = (root / cwd).resolve(strict=True)
    if root.resolve(strict=True) not in working_directory.parents:
        raise RuntimeError("candidate test working directory escapes its clone")
    try:
        completed = subprocess.run(
            argv,
            cwd=working_directory,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return ProcessCapture(
            status="timeout",
            exit_code=None,
            stdout=error.stdout or b"",
            stderr=error.stderr or b"",
            elapsed_ms=float((time.monotonic() - started) * 1000),
        )
    except OSError as error:
        return ProcessCapture(
            status="failed_to_start",
            exit_code=None,
            stdout=b"",
            stderr=str(error).encode("utf-8", errors="replace"),
            elapsed_ms=float((time.monotonic() - started) * 1000),
        )
    return ProcessCapture(
        status="completed",
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_ms=float((time.monotonic() - started) * 1000),
    )


def _validate_test_attempt(
    artifact: StableArtifact,
    *,
    candidate: CandidateKey,
    manifest: StableArtifact,
    version: str,
    repetition: int,
    plans: tuple[tuple[str, tuple[str, ...]], ...],
    epoch: SelectionEpoch,
) -> bool:
    payload = artifact.payload
    expected_commands = [{"cwd": cwd, "argv": list(argv)} for cwd, argv in plans]
    if (
        artifact.reference.semantic_version != epoch.semantic_version
        or artifact.inputs != (manifest.reference,)
        or payload.get("candidate") != candidate.to_value()
        or payload.get("version") != version
        or payload.get("repetition") != repetition
        or payload.get("commands") != expected_commands
        or payload.get("complete") is not True
        or type(payload.get("passed")) is not bool
    ):
        raise RuntimeError("candidate test attempt binding is invalid")
    expected_pass = (
        payload.get("status") == "completed" and payload.get("exit_code") == 0
    )
    if payload["passed"] is not expected_pass:
        raise RuntimeError("candidate test attempt outcome is invalid")
    return payload["passed"]


def _select_patch_files(content: bytes, paths: frozenset[str]) -> bytes:
    text = content.decode("utf-8", errors="surrogateescape")
    chunks: list[str] = []
    current: list[str] = []
    current_path: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path in paths:
                chunks.extend(current)
            parts = line.rstrip("\r\n").split(" ")
            if len(parts) != 4 or not parts[3].startswith("b/"):
                raise RuntimeError("candidate patch file header is invalid")
            current_path = parts[3][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path in paths:
        chunks.extend(current)
    if not chunks:
        raise RuntimeError("candidate production patch is empty")
    return "".join(chunks).encode("utf-8", errors="surrogateescape")


def _write_raw_once(root: Path, relative: PurePosixPath, content: bytes) -> None:
    require_write_capacity(root)
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.read_bytes() != content:
            raise RuntimeError(
                "candidate test raw evidence conflicts with current data"
            )
        return
    descriptor = path.open("xb")
    try:
        descriptor.write(content)
        descriptor.flush()
    finally:
        descriptor.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_raw_path(root: Path, value: object) -> Path:
    if type(value) is not str or not value:
        raise RuntimeError("candidate patch raw path is missing")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("candidate patch raw path is unsafe")
    candidate = root.joinpath(*relative.parts)
    if candidate.parent.resolve(strict=True) != candidate.parent:
        raise RuntimeError("candidate patch raw parent is unsafe")
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("candidate patch raw file is unsafe")
    return candidate


def _patch_sections(content: bytes) -> dict[str, tuple[str, ...]]:
    if type(content) is not bytes or not content:
        raise RuntimeError("candidate patch is empty")
    text = content.decode("utf-8", errors="surrogateescape")
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) != 4 or not parts[3].startswith("b/"):
                raise RuntimeError("candidate patch file header is invalid")
            current = parts[3][2:]
            pure = PurePosixPath(current)
            if pure.is_absolute() or ".." in pure.parts:
                raise RuntimeError("candidate patch file path is unsafe")
            result.setdefault(current, [])
        elif (
            current is not None and line.startswith("+") and not line.startswith("+++")
        ):
            result[current].append(line[1:])
    return {path: tuple(lines) for path, lines in result.items()}


def _is_executable_test(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError("candidate test path is unsafe")
    folded_parts = tuple(part.casefold() for part in pure.parts)
    return pure.suffix.casefold() in _EXECUTABLE_TEST_SUFFIXES and not any(
        "snapshot" in part for part in folded_parts
    )


def _contains_added_assertion(lines: tuple[str, ...]) -> bool:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*", "*/")):
            continue
        if _ASSERTION.search(stripped):
            return True
    return False


__all__ = [
    "ProcessCapture",
    "V2PreannotationBatchContext",
    "evaluate_test_manifest",
    "evaluate_test_repetitions",
]
