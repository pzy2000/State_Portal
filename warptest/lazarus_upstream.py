"""Isolated, bounded upstream-test execution for Lazarus gates and trials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ContextManager

from .c1_sandbox import strict_clone_tree
from .run_context import safe_error_text


PER_TRIAL_P2P_TIMEOUT_S = 300.0
CONSTRUCTION_GATE_TIMEOUT_S = 7200.0
_ORIGINS = frozenset({"upstream", "issue_derived_gui_differential"})
# The ScaleCUA pixel baseline records its own from-start kind so ledger rows
# stay attributable per policy family.
P2P_CHECKPOINT_KINDS = frozenset(
    {"semantic_state", "agent_s_from_start", "scalecua_from_start"}
)
_CHECKPOINT_KINDS = P2P_CHECKPOINT_KINDS
_CONSTRUCTION_FAILURE_CLASSES = frozenset({"version_incompatible"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_RESTORE_RELATIVE_PATH = Path(".warptest/lazarus/checkpoint-restore.json")
_SUBPROCESS_OUTPUT_CAPTURE_SCHEMA = "c1-lazarus-subprocess-output-v1"
_SUBPROCESS_OUTPUT_CAPTURE_VERSION = 1
_SUBPROCESS_OUTPUT_CAPTURE_MAX_BYTES = 16 * 1024
_SUBPROCESS_OUTPUT_CAPTURE_TRUNCATION_MARKER = (
    "[... output truncated; retained tail follows ...]\n"
)
_OUTPUT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<name>\b(?:[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:api[-_]?key|secret|token|password)|authorization)\b)"
    r"(?P<separator>\s*(?:=|:)\s*)(?P<value>[^\s,;]+)"
)


P2PCheckpointRestorer = Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]]
ConstructionRuntimeEnvironmentFactory = Callable[
    [Path], ContextManager[Mapping[str, str]]
]


class LazarusUpstreamError(RuntimeError):
    """Raised for an unsafe or malformed upstream-test declaration."""


@dataclass(frozen=True)
class UpstreamTestDefinition:
    id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_s: float
    origin: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UpstreamTestDefinition":
        argv = value.get("argv")
        if not isinstance(argv, list) or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise LazarusUpstreamError(
                "upstream test argv must be a non-empty string list; shell strings are forbidden"
            )
        definition = cls(
            id=str(value.get("id") or "").strip(),
            argv=tuple(argv),
            cwd=str(value.get("cwd") or "").strip(),
            timeout_s=float(value.get("timeout_s") or 0),
            origin=str(value.get("origin") or "").strip(),
        )
        _validate_definition(definition)
        return definition


@dataclass(frozen=True)
class ConstructionCommandDefinition:
    """Coordinator-owned build/setup command for a construction-only clone."""

    id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_s: float
    failure_class: str | None = None


@dataclass(frozen=True)
class FrozenUpstreamTestSource:
    """One fixed-version upstream test file overlaid into a disposable clone."""

    source_path: str
    destination_path: str
    sha256: str


@dataclass(frozen=True)
class LazarusP2PCheckpointRestore:
    """Coordinator-owned runtime context bound into one isolated P2P clone.

    The live trial runtime restores its task state before this object is made.
    The P2P command runs in a separate CoW clone, so it cannot inherit a live
    process.  Instead, this short, hash-bound receipt is materialized inside
    that clone before the command starts.  It makes the ordering and exact
    checkpoint/run context independently auditable without exposing a hidden
    oracle or a version label to an upstream test command.
    """

    checkpoint_sha256: str
    checkpoint_kind: str
    runtime_restored: bool = True

    def validated(self) -> "LazarusP2PCheckpointRestore":
        if not _SHA256_RE.fullmatch(str(self.checkpoint_sha256)):
            raise LazarusUpstreamError("P2P checkpoint SHA must be a lowercase SHA-256")
        if self.checkpoint_kind not in _CHECKPOINT_KINDS:
            raise LazarusUpstreamError("P2P checkpoint kind is not registered")
        if self.runtime_restored is not True:
            raise LazarusUpstreamError("P2P checkpoint context was not restored")
        return self


@dataclass(frozen=True)
class SubprocessOutputCapture:
    """Bounded, redacted stdout/stderr retained after an isolated clone dies."""

    schema: str
    schema_version: int
    redaction: str
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True)
class UpstreamTestResult:
    id: str
    passed: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: float
    stdout_sha256: str
    stderr_sha256: str
    output_capture: SubprocessOutputCapture


@dataclass(frozen=True)
class UpstreamSubsetResult:
    test_ids: tuple[str, ...]
    complete: bool
    passed: bool
    results: tuple[UpstreamTestResult, ...]
    failures: tuple[str, ...]
    isolation: str
    duration_ms: float
    checkpoint_restore: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FrozenSourceOverlayResult:
    source_path: str
    destination_path: str
    sha256: str


@dataclass(frozen=True)
class ConstructionSubsetResult:
    test_ids: tuple[str, ...]
    complete: bool
    passed: bool
    prepare_results: tuple[UpstreamTestResult, ...]
    results: tuple[UpstreamTestResult, ...]
    overlays: tuple[FrozenSourceOverlayResult, ...]
    failures: tuple[str, ...]
    isolation: str
    duration_ms: float
    terminal_failure_class: str | None = None


def definitions_from_upstream_record(
    record: Mapping[str, Any],
) -> dict[str, UpstreamTestDefinition]:
    """Bind frozen upstream test ids to shell-free executable definitions."""

    raw_tests = record.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise LazarusUpstreamError(
            "upstream record must contain a non-empty tests list"
        )
    definitions: dict[str, UpstreamTestDefinition] = {}
    for index, raw in enumerate(raw_tests):
        if not isinstance(raw, Mapping):
            raise LazarusUpstreamError(
                f"upstream record tests[{index}] must be a mapping"
            )
        definition = UpstreamTestDefinition.from_mapping(raw)
        if definition.id in definitions:
            raise LazarusUpstreamError(
                f"upstream record contains duplicate test id {definition.id!r}"
            )
        definitions[definition.id] = definition
    return definitions


def frozen_sources_from_upstream_record(
    record: Mapping[str, Any],
    test_ids: Sequence[str],
) -> tuple[FrozenUpstreamTestSource, ...]:
    """Project registered F2P ids onto their immutable construction capsules."""

    selected = tuple(str(value).strip() for value in test_ids)
    if not selected or any(not value for value in selected):
        raise LazarusUpstreamError("construction test ids must be non-empty")
    if len(selected) != len(set(selected)):
        raise LazarusUpstreamError("construction test ids must be unique")
    raw_tests = record.get("tests")
    if not isinstance(raw_tests, list):
        raise LazarusUpstreamError("upstream record must contain a tests list")
    by_id = {
        str(value.get("id") or ""): value
        for value in raw_tests
        if isinstance(value, Mapping) and value.get("id")
    }
    missing = [test_id for test_id in selected if test_id not in by_id]
    if missing:
        raise LazarusUpstreamError(
            "missing registered construction test " + ", ".join(missing)
        )
    destinations: dict[str, FrozenUpstreamTestSource] = {}
    for test_id in selected:
        value = by_id[test_id]
        raw_sources = value.get("frozen_test_sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise LazarusUpstreamError(
                f"registered construction test {test_id!r} has a missing frozen test source"
            )
        for raw_source in raw_sources:
            if not isinstance(raw_source, Mapping):
                raise LazarusUpstreamError(
                    f"registered construction test {test_id!r} has an invalid frozen test source"
                )
            source_path = str(raw_source.get("source_path") or "").strip()
            destination_path = str(raw_source.get("destination_path") or "").strip()
            sha256 = str(raw_source.get("sha256") or "").strip()
            if (
                not source_path
                or not destination_path
                or not _SHA256_RE.fullmatch(sha256)
            ):
                raise LazarusUpstreamError(
                    f"registered construction test {test_id!r} has an invalid frozen test source"
                )
            entry = FrozenUpstreamTestSource(
                source_path=source_path,
                destination_path=destination_path,
                sha256=sha256,
            )
            previous = destinations.get(destination_path)
            if previous is not None and previous != entry:
                raise LazarusUpstreamError(
                    f"registered construction tests conflict at {destination_path!r}"
                )
            destinations[destination_path] = entry
    return tuple(destinations[key] for key in sorted(destinations))


def run_upstream_test_subset(
    source_root: str | Path,
    definitions: Mapping[str, UpstreamTestDefinition],
    test_ids: Sequence[str],
    *,
    sandbox_root: str | Path,
    total_timeout_s: float = PER_TRIAL_P2P_TIMEOUT_S,
    harness_root: str | Path | None = None,
    trusted_git_dir: str | Path | None = None,
    checkpoint_restore: LazarusP2PCheckpointRestore | None = None,
    checkpoint_restorer: P2PCheckpointRestorer | None = None,
    environment_overrides: Mapping[str, str] | None = None,
) -> UpstreamSubsetResult:
    """Run a registered subset in one disposable strict CoW clone.

    Commands are passed directly to ``subprocess`` with ``shell=False``.  The
    entire subset and every declared command are capped at 300 seconds.
    """

    started = time.monotonic()
    if total_timeout_s <= 0 or total_timeout_s > PER_TRIAL_P2P_TIMEOUT_S:
        raise LazarusUpstreamError(
            "upstream subset timeout must be in (0, 300] seconds"
        )
    runtime_environment = _validated_environment_overrides(environment_overrides)
    selected = tuple(str(value).strip() for value in test_ids)
    if not selected or any(not value for value in selected):
        return UpstreamSubsetResult(
            test_ids=selected,
            complete=False,
            passed=False,
            results=(),
            failures=("per-trial upstream subset must contain at least one id",),
            isolation="not_executed",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
    if len(selected) != len(set(selected)):
        raise LazarusUpstreamError("upstream subset ids must be unique")
    missing = tuple(test_id for test_id in selected if test_id not in definitions)
    if missing:
        return UpstreamSubsetResult(
            test_ids=selected,
            complete=False,
            passed=False,
            results=(),
            failures=tuple(
                f"missing registered upstream test {test_id}" for test_id in missing
            ),
            isolation="not_executed",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
    selected_definitions = [definitions[test_id] for test_id in selected]
    for definition in selected_definitions:
        _validate_definition(definition)
        if (
            definition.id not in definitions
            or definitions[definition.id] is not definition
        ):
            raise LazarusUpstreamError(
                f"upstream registry key does not bind definition id {definition.id!r}"
            )
    trusted_harness = (
        Path(harness_root).expanduser().resolve() if harness_root is not None else None
    )
    if trusted_harness is not None and not trusted_harness.is_dir():
        raise LazarusUpstreamError(
            f"registered harness root does not exist: {trusted_harness}"
        )
    trusted_git = (
        Path(trusted_git_dir).expanduser().resolve()
        if trusted_git_dir is not None
        else None
    )
    if trusted_git is not None and not trusted_git.is_dir():
        raise LazarusUpstreamError(
            f"trusted Git metadata does not exist: {trusted_git}"
        )

    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise LazarusUpstreamError(f"upstream-test source does not exist: {source}")
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    isolation_root = Path(
        tempfile.mkdtemp(prefix="lazarus-p2p-", dir=sandbox)
    ).resolve()
    clone = isolation_root / "repo"
    results: list[UpstreamTestResult] = []
    failures: list[str] = []
    restore_evidence: Mapping[str, Any] | None = None
    try:
        strict_clone_tree(source, clone)
        if checkpoint_restore is not None:
            receipt = _materialize_checkpoint_restore(
                clone, checkpoint_restore.validated()
            )
            if checkpoint_restorer is None:
                raise LazarusUpstreamError(
                    "P2P checkpoint restore requires an isolated state restorer"
                )
            restore_evidence = _apply_checkpoint_restorer(
                clone,
                receipt,
                checkpoint_restorer,
            )
        deadline = started + float(total_timeout_s)
        for definition in selected_definitions:
            cwd = _resolve_cwd(clone, definition.cwd)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(
                    UpstreamTestResult(
                        id=definition.id,
                        passed=False,
                        exit_code=None,
                        timed_out=True,
                        duration_ms=0.0,
                        stdout_sha256=_sha(b""),
                        stderr_sha256=_sha(b""),
                        output_capture=_capture_subprocess_output(b"", b""),
                    )
                )
                failures.append(f"upstream subset timed out before {definition.id}")
                continue
            command_started = time.monotonic()
            timeout = min(definition.timeout_s, remaining)
            try:
                argv = _registered_test_argv(definition, trusted_harness)
                env = dict(os.environ)
                env.update(runtime_environment)
                if trusted_harness is not None:
                    current = env.get("PYTHONPATH")
                    env["PYTHONPATH"] = str(trusted_harness) + (
                        os.pathsep + current if current else ""
                    )
                if trusted_git is not None:
                    env["WARPTEST_LUANTI_TRUSTED_GIT_DIR"] = str(trusted_git)
                if restore_evidence is not None:
                    restore_path = clone / str(restore_evidence["path"])
                    env["WARPTEST_LAZARUS_CHECKPOINT_RESTORE_PATH"] = str(restore_path)
                    env["WARPTEST_LAZARUS_CHECKPOINT_SHA256"] = str(
                        restore_evidence["checkpoint_sha256"]
                    )
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    timeout=timeout,
                    shell=False,
                    env=env,
                )
                duration_ms = (time.monotonic() - command_started) * 1000.0
                passed = completed.returncode == 0
                results.append(
                    UpstreamTestResult(
                        id=definition.id,
                        passed=passed,
                        exit_code=int(completed.returncode),
                        timed_out=False,
                        duration_ms=duration_ms,
                        stdout_sha256=_sha(completed.stdout),
                        stderr_sha256=_sha(completed.stderr),
                        output_capture=_capture_subprocess_output(
                            completed.stdout,
                            completed.stderr,
                        ),
                    )
                )
                if not passed:
                    failures.append(
                        f"upstream test {definition.id} exited {completed.returncode}"
                    )
            except subprocess.TimeoutExpired as error:
                duration_ms = (time.monotonic() - command_started) * 1000.0
                stdout = _subprocess_output_bytes(error.stdout)
                stderr = _subprocess_output_bytes(error.stderr)
                results.append(
                    UpstreamTestResult(
                        id=definition.id,
                        passed=False,
                        exit_code=None,
                        timed_out=True,
                        duration_ms=duration_ms,
                        stdout_sha256=_sha(stdout),
                        stderr_sha256=_sha(stderr),
                        output_capture=_capture_subprocess_output(stdout, stderr),
                    )
                )
                failures.append(f"upstream test {definition.id} timed out")
    except LazarusUpstreamError:
        raise
    except Exception as error:
        raise LazarusUpstreamError(
            f"cannot execute isolated upstream subset: {type(error).__name__}: {error}"
        ) from error
    finally:
        if isolation_root.exists():
            shutil.rmtree(isolation_root)
    return UpstreamSubsetResult(
        test_ids=selected,
        complete=len(results) == len(selected),
        passed=len(results) == len(selected)
        and all(result.passed for result in results),
        results=tuple(results),
        failures=tuple(failures),
        isolation="apfs_cow_clone",
        duration_ms=(time.monotonic() - started) * 1000.0,
        checkpoint_restore=restore_evidence,
    )


def run_lazarus_construction_test_subset(
    source_root: str | Path,
    definitions: Mapping[str, UpstreamTestDefinition],
    test_ids: Sequence[str],
    *,
    sandbox_root: str | Path,
    frozen_source_root: str | Path | None = None,
    frozen_sources: Sequence[FrozenUpstreamTestSource] = (),
    prepare_commands: Sequence[ConstructionCommandDefinition] = (),
    total_timeout_s: float = CONSTRUCTION_GATE_TIMEOUT_S,
    harness_root: str | Path | None = None,
    trusted_git_dir: str | Path | None = None,
    runtime_environment_factory: ConstructionRuntimeEnvironmentFactory | None = None,
) -> ConstructionSubsetResult:
    """Build and run fixed upstream tests against one version without mutating it.

    Fix commits frequently add or change their regression test.  Inverse-patched
    buggy trees therefore cannot be graded by whatever test source happens to
    remain in that tree.  This construction-only runner overlays the exact
    fixed-version test bytes (bound by SHA-256) into a disposable CoW clone,
    performs separately registered build steps, and then delegates the actual
    upstream test to the unchanged 300-second subset runner.  Per-trial P2P
    never calls this API and cannot receive an overlay or a longer timeout.
    """

    started = time.monotonic()
    if total_timeout_s <= 0 or total_timeout_s > CONSTRUCTION_GATE_TIMEOUT_S:
        raise LazarusUpstreamError(
            "construction subset timeout must be in (0, 7200] seconds"
        )
    selected = tuple(str(value).strip() for value in test_ids)
    if not selected or any(not value for value in selected):
        return _empty_construction_result(
            selected,
            started,
            "construction upstream subset must contain at least one id",
        )
    if len(selected) != len(set(selected)):
        raise LazarusUpstreamError("construction upstream subset ids must be unique")
    missing = tuple(test_id for test_id in selected if test_id not in definitions)
    if missing:
        return _empty_construction_result(
            selected,
            started,
            *(f"missing registered upstream test {test_id}" for test_id in missing),
        )
    for test_id in selected:
        definition = definitions[test_id]
        _validate_definition(definition)
        if definition.id != test_id:
            raise LazarusUpstreamError(
                f"upstream registry key does not bind definition id {definition.id!r}"
            )
    prepared_commands = tuple(prepare_commands)
    for command in prepared_commands:
        _validate_construction_command(command)
    if runtime_environment_factory is not None and not callable(
        runtime_environment_factory
    ):
        raise LazarusUpstreamError(
            "construction runtime environment factory must be callable"
        )

    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise LazarusUpstreamError(f"construction-test source does not exist: {source}")
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    frozen_root = (
        Path(frozen_source_root).expanduser().resolve()
        if frozen_source_root is not None
        else None
    )
    if frozen_sources and (frozen_root is None or not frozen_root.is_dir()):
        raise LazarusUpstreamError(
            "frozen upstream test overlays require an existing source root"
        )
    trusted_harness = (
        Path(harness_root).expanduser().resolve() if harness_root is not None else None
    )
    if trusted_harness is not None and not trusted_harness.is_dir():
        raise LazarusUpstreamError(
            f"registered harness root does not exist: {trusted_harness}"
        )
    trusted_git = (
        Path(trusted_git_dir).expanduser().resolve()
        if trusted_git_dir is not None
        else None
    )
    if trusted_git is not None and not trusted_git.is_dir():
        raise LazarusUpstreamError(
            f"trusted Git metadata does not exist: {trusted_git}"
        )

    isolation_root = Path(
        tempfile.mkdtemp(prefix="lazarus-construction-", dir=sandbox)
    ).resolve()
    prepared = isolation_root / "prepared"
    prepare_results: list[UpstreamTestResult] = []
    failures: list[str] = []
    overlays: tuple[FrozenSourceOverlayResult, ...] = ()
    subset: UpstreamSubsetResult | None = None
    terminal_failure_class: str | None = None
    try:
        strict_clone_tree(source, prepared)
        overlays = _apply_frozen_test_sources(
            prepared,
            frozen_root,
            tuple(frozen_sources),
        )
        deadline = started + float(total_timeout_s)
        for command in prepared_commands:
            result = _run_construction_command(
                prepared,
                command,
                deadline=deadline,
                harness_root=trusted_harness,
                trusted_git_dir=trusted_git,
            )
            prepare_results.append(result)
            if not result.passed:
                reason = (
                    "timed out" if result.timed_out else f"exited {result.exit_code}"
                )
                failures.append(f"construction command {command.id} {reason}")
                if (
                    not result.timed_out
                    and result.exit_code is not None
                    and command.failure_class is not None
                ):
                    terminal_failure_class = command.failure_class
                break
        if not failures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append("construction subset timed out before upstream tests")
            else:
                runtime_context = (
                    runtime_environment_factory(prepared)
                    if runtime_environment_factory is not None
                    else nullcontext({})
                )
                if not hasattr(runtime_context, "__enter__") or not hasattr(
                    runtime_context, "__exit__"
                ):
                    raise LazarusUpstreamError(
                        "construction runtime environment factory did not return a context manager"
                    )
                with runtime_context as environment:
                    runtime_environment = _validated_environment_overrides(environment)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        failures.append(
                            "construction subset timed out while preparing runtime"
                        )
                    else:
                        subset = run_upstream_test_subset(
                            prepared,
                            definitions,
                            selected,
                            sandbox_root=isolation_root,
                            total_timeout_s=min(PER_TRIAL_P2P_TIMEOUT_S, remaining),
                            harness_root=trusted_harness,
                            trusted_git_dir=trusted_git,
                            environment_overrides=runtime_environment,
                        )
                        failures.extend(subset.failures)
    except LazarusUpstreamError:
        raise
    except Exception as error:
        raise LazarusUpstreamError(
            "cannot execute isolated construction subset: "
            f"{type(error).__name__}: {error}"
        ) from error
    finally:
        if isolation_root.exists():
            shutil.rmtree(isolation_root)

    results = subset.results if subset is not None else ()
    complete = (
        not failures
        and len(prepare_results) == len(prepared_commands)
        and subset is not None
        and subset.complete
    )
    return ConstructionSubsetResult(
        test_ids=selected,
        complete=complete,
        passed=complete and bool(subset and subset.passed),
        prepare_results=tuple(prepare_results),
        results=tuple(results),
        overlays=overlays,
        failures=tuple(failures),
        isolation="apfs_cow_clone",
        duration_ms=(time.monotonic() - started) * 1000.0,
        terminal_failure_class=terminal_failure_class,
    )


def run_lazarus_per_trial_p2p(
    *,
    task_id: str,
    version: str,
    checkpoint_sha256: str,
    checkpoint_kind: str,
    source_root: str | Path,
    definitions: Mapping[str, UpstreamTestDefinition],
    test_ids: Sequence[str],
    sandbox_root: str | Path,
    harness_root: str | Path | None = None,
    trusted_git_dir: str | Path | None = None,
    checkpoint_restorer: P2PCheckpointRestorer | None = None,
) -> dict[str, Any]:
    """Run one registered P2P subset and serialize coordinator-gradeable evidence.

    This is deliberately a thin, fixed-timeout wrapper over
    :func:`run_upstream_test_subset`: every trial gets a new strict CoW clone,
    no caller can loosen the 300-second bound, and the result retains enough
    raw subprocess evidence for a later audit without making a trial verdict a
    self-reported claim.
    """

    normalized_task_id = str(task_id).strip()
    if not normalized_task_id:
        raise LazarusUpstreamError("per-trial P2P task id must be non-empty")
    if version not in {"buggy", "fixed"}:
        raise LazarusUpstreamError("per-trial P2P version must be 'buggy' or 'fixed'")
    if checkpoint_restorer is None or not callable(checkpoint_restorer):
        raise LazarusUpstreamError(
            "per-trial P2P requires an isolated checkpoint state restorer"
        )
    restore = LazarusP2PCheckpointRestore(
        checkpoint_sha256=str(checkpoint_sha256),
        checkpoint_kind=str(checkpoint_kind),
    ).validated()
    result = run_upstream_test_subset(
        source_root,
        definitions,
        test_ids,
        sandbox_root=sandbox_root,
        total_timeout_s=PER_TRIAL_P2P_TIMEOUT_S,
        harness_root=harness_root,
        trusted_git_dir=trusted_git_dir,
        checkpoint_restore=restore,
        checkpoint_restorer=checkpoint_restorer,
    )
    return {
        "schema_version": 1,
        "task_id": normalized_task_id,
        "version": version,
        "completed": result.complete,
        "passed": result.passed,
        "timeout_s": PER_TRIAL_P2P_TIMEOUT_S,
        "isolation": result.isolation,
        "duration_ms": result.duration_ms,
        "checkpoint_restore": (
            dict(result.checkpoint_restore)
            if isinstance(result.checkpoint_restore, Mapping)
            else {"schema_version": 1, "applied": False}
        ),
        "failures": list(result.failures),
        "results": [asdict(row) for row in result.results],
    }


def _materialize_checkpoint_restore(
    clone: Path, restore: LazarusP2PCheckpointRestore
) -> dict[str, Any]:
    """Write one trusted restore receipt inside a disposable P2P clone.

    The function intentionally owns the filename and environment projection;
    an upstream command cannot select a different coordinator checkpoint or
    cause a path escape.  The source clone is removed after the subset returns,
    while this compact evidence remains in the trial report.
    """

    destination = (clone / _CHECKPOINT_RESTORE_RELATIVE_PATH).resolve()
    if clone != destination and clone not in destination.parents:
        raise LazarusUpstreamError("P2P checkpoint restore path escapes isolated clone")
    payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": restore.checkpoint_sha256,
        "checkpoint_kind": restore.checkpoint_kind,
        "runtime_restored": True,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "applied": True,
        "checkpoint_sha256": restore.checkpoint_sha256,
        "checkpoint_kind": restore.checkpoint_kind,
        "runtime_restored": True,
        "path": _CHECKPOINT_RESTORE_RELATIVE_PATH.as_posix(),
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _apply_checkpoint_restorer(
    clone: Path,
    receipt: Mapping[str, Any],
    restorer: P2PCheckpointRestorer,
) -> dict[str, Any]:
    """Apply a real backend restore inside the disposable clone.

    A receipt only binds identity.  The supplied trusted adapter must consume
    that receipt and materialize the registered Start in the clone before any
    upstream command is launched.  Its compact outcome is retained with the
    receipt so the later trial grader can reject a no-op adapter.
    """

    relative = receipt.get("path")
    if not isinstance(relative, str) or not relative:
        raise LazarusUpstreamError("P2P restore receipt has no relative path")
    receipt_path = (clone / relative).resolve()
    if clone != receipt_path and clone not in receipt_path.parents:
        raise LazarusUpstreamError("P2P restore receipt path escapes isolated clone")
    if not receipt_path.is_file():
        raise LazarusUpstreamError("P2P restore receipt was not materialized")
    try:
        restored = restorer(clone, receipt_path, receipt)
    except LazarusUpstreamError:
        raise
    except Exception as error:  # noqa: BLE001 - adapter failure is terminal evidence
        raise LazarusUpstreamError(
            f"P2P isolated state restore failed: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(restored, Mapping):
        raise LazarusUpstreamError("P2P isolated state restorer returned no mapping")
    state = dict(restored)
    if (
        state.get("schema_version") != 1
        or state.get("state_restore_applied") is not True
        or not isinstance(state.get("adapter"), str)
        or not str(state["adapter"]).strip()
        or state.get("checkpoint_sha256") != receipt.get("checkpoint_sha256")
        or state.get("checkpoint_kind") != receipt.get("checkpoint_kind")
        or state.get("receipt_sha256") != receipt.get("payload_sha256")
    ):
        raise LazarusUpstreamError(
            "P2P isolated state restorer did not attest the bound checkpoint"
        )
    merged = dict(receipt)
    merged["state_restore"] = state
    return merged


def _empty_construction_result(
    selected: tuple[str, ...],
    started: float,
    *failures: str,
) -> ConstructionSubsetResult:
    return ConstructionSubsetResult(
        test_ids=selected,
        complete=False,
        passed=False,
        prepare_results=(),
        results=(),
        overlays=(),
        failures=tuple(failures),
        isolation="not_executed",
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


def _apply_frozen_test_sources(
    clone: Path,
    frozen_root: Path | None,
    sources: tuple[FrozenUpstreamTestSource, ...],
) -> tuple[FrozenSourceOverlayResult, ...]:
    if not sources:
        return ()
    if frozen_root is None:
        raise LazarusUpstreamError("frozen upstream test source root is missing")
    destinations: set[str] = set()
    results: list[FrozenSourceOverlayResult] = []
    for entry in sources:
        if not _SHA256_RE.fullmatch(str(entry.sha256)):
            raise LazarusUpstreamError("frozen upstream test SHA is invalid")
        source_relative = Path(entry.source_path)
        destination_relative = Path(entry.destination_path)
        if (
            not entry.source_path
            or source_relative.is_absolute()
            or ".." in source_relative.parts
        ):
            raise LazarusUpstreamError(
                "frozen upstream test source path escapes its root"
            )
        if (
            not entry.destination_path
            or destination_relative.is_absolute()
            or ".." in destination_relative.parts
        ):
            raise LazarusUpstreamError(
                "frozen upstream test destination path escapes the clone"
            )
        destination_key = destination_relative.as_posix()
        if destination_key in destinations:
            raise LazarusUpstreamError(
                f"duplicate frozen upstream test destination {destination_key!r}"
            )
        destinations.add(destination_key)
        if _has_symlink_component(frozen_root, source_relative):
            raise LazarusUpstreamError("frozen upstream test source contains a symlink")
        if _has_symlink_component(clone, destination_relative):
            raise LazarusUpstreamError(
                "frozen upstream test destination contains a symlink"
            )
        source = (frozen_root / source_relative).resolve()
        if frozen_root != source and frozen_root not in source.parents:
            raise LazarusUpstreamError(
                "frozen upstream test source path escapes its root"
            )
        if not source.is_file():
            raise LazarusUpstreamError(
                f"frozen upstream test source is missing: {entry.source_path!r}"
            )
        actual_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_sha != entry.sha256:
            raise LazarusUpstreamError(
                f"frozen upstream test content SHA drift: {entry.source_path!r}"
            )
        destination = (clone / destination_relative).resolve()
        if clone != destination and clone not in destination.parents:
            raise LazarusUpstreamError(
                "frozen upstream test destination path escapes the clone"
            )
        if destination.exists() and not destination.is_file():
            raise LazarusUpstreamError(
                f"frozen upstream test destination is not a file: {entry.destination_path!r}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != entry.sha256:
            raise LazarusUpstreamError(
                f"frozen upstream test overlay verification failed: {entry.destination_path!r}"
            )
        results.append(
            FrozenSourceOverlayResult(
                source_path=source_relative.as_posix(),
                destination_path=destination_key,
                sha256=entry.sha256,
            )
        )
    return tuple(results)


def _has_symlink_component(root: Path, relative: Path) -> bool:
    """Detect lexical symlinks before ``resolve()`` can hide their identity."""

    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _validate_construction_command(command: ConstructionCommandDefinition) -> None:
    if not command.id:
        raise LazarusUpstreamError("construction command id must be non-empty")
    if (
        not isinstance(command.argv, tuple)
        or not command.argv
        or not all(isinstance(value, str) and value for value in command.argv)
    ):
        raise LazarusUpstreamError(
            "construction command argv must be a non-empty tuple"
        )
    cwd = Path(command.cwd)
    if not command.cwd or cwd.is_absolute() or ".." in cwd.parts:
        raise LazarusUpstreamError(
            f"construction command cwd escapes the isolated clone: {command.cwd!r}"
        )
    if command.timeout_s <= 0 or command.timeout_s > CONSTRUCTION_GATE_TIMEOUT_S:
        raise LazarusUpstreamError(
            "construction command timeout must be in (0, 7200] seconds"
        )
    if (
        command.failure_class is not None
        and command.failure_class not in _CONSTRUCTION_FAILURE_CLASSES
    ):
        raise LazarusUpstreamError(
            "construction command failure class is not registered"
        )


def _run_construction_command(
    clone: Path,
    command: ConstructionCommandDefinition,
    *,
    deadline: float,
    harness_root: Path | None,
    trusted_git_dir: Path | None,
) -> UpstreamTestResult:
    cwd = _resolve_cwd(clone, command.cwd)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return UpstreamTestResult(
            id=command.id,
            passed=False,
            exit_code=None,
            timed_out=True,
            duration_ms=0.0,
            stdout_sha256=_sha(b""),
            stderr_sha256=_sha(b""),
            output_capture=_capture_subprocess_output(b"", b""),
        )
    argv = _expand_argv(command.argv, harness_root)
    env = dict(os.environ)
    if harness_root is not None:
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(harness_root) + (
            os.pathsep + current if current else ""
        )
    if trusted_git_dir is not None:
        env["WARPTEST_LUANTI_TRUSTED_GIT_DIR"] = str(trusted_git_dir)
    command_started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=min(command.timeout_s, remaining),
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        stdout = _subprocess_output_bytes(error.stdout)
        stderr = _subprocess_output_bytes(error.stderr)
        return UpstreamTestResult(
            id=command.id,
            passed=False,
            exit_code=None,
            timed_out=True,
            duration_ms=(time.monotonic() - command_started) * 1000.0,
            stdout_sha256=_sha(stdout),
            stderr_sha256=_sha(stderr),
            output_capture=_capture_subprocess_output(stdout, stderr),
        )
    return UpstreamTestResult(
        id=command.id,
        passed=completed.returncode == 0,
        exit_code=int(completed.returncode),
        timed_out=False,
        duration_ms=(time.monotonic() - command_started) * 1000.0,
        stdout_sha256=_sha(completed.stdout),
        stderr_sha256=_sha(completed.stderr),
        output_capture=_capture_subprocess_output(
            completed.stdout,
            completed.stderr,
        ),
    )


def _validate_definition(definition: UpstreamTestDefinition) -> None:
    if not definition.id:
        raise LazarusUpstreamError("upstream test id must be non-empty")
    if (
        not isinstance(definition.argv, tuple)
        or not definition.argv
        or not all(isinstance(value, str) and value for value in definition.argv)
    ):
        raise LazarusUpstreamError("upstream test argv must be a non-empty tuple")
    cwd = Path(definition.cwd)
    if not definition.cwd or cwd.is_absolute() or ".." in cwd.parts:
        raise LazarusUpstreamError(
            f"upstream test cwd escapes the isolated clone: {definition.cwd!r}"
        )
    if definition.timeout_s <= 0 or definition.timeout_s > PER_TRIAL_P2P_TIMEOUT_S:
        raise LazarusUpstreamError("upstream test timeout must be in (0, 300] seconds")
    if definition.origin not in _ORIGINS:
        raise LazarusUpstreamError(
            f"unsupported upstream test origin {definition.origin!r}"
        )


def _resolve_cwd(clone: Path, value: str) -> Path:
    path = (clone / value).resolve()
    if clone != path and clone not in path.parents:
        raise LazarusUpstreamError(
            f"upstream test cwd escapes isolated clone: {value!r}"
        )
    if not path.is_dir():
        raise LazarusUpstreamError(f"upstream test cwd does not exist: {value!r}")
    return path


def _expand_argv(argv: tuple[str, ...], harness_root: Path | None) -> list[str]:
    result: list[str] = []
    for value in argv:
        if value == "{python}":
            result.append(sys.executable)
            continue
        if value == "{harness_root}":
            if harness_root is None:
                raise LazarusUpstreamError(
                    "upstream test requires an unregistered harness_root"
                )
            result.append(str(harness_root))
            continue
        prefix = "{harness_root}/"
        if value.startswith(prefix):
            if harness_root is None:
                raise LazarusUpstreamError(
                    "upstream test requires an unregistered harness_root"
                )
            path = (harness_root / value[len(prefix) :]).resolve()
            if harness_root != path and harness_root not in path.parents:
                raise LazarusUpstreamError("upstream test argv escapes harness_root")
            result.append(str(path))
            continue
        if "{harness_root}" in value or "{python}" in value:
            raise LazarusUpstreamError("upstream test uses an invalid harness token")
        result.append(value)
    return result


def _registered_test_argv(
    definition: UpstreamTestDefinition,
    harness_root: Path | None,
) -> list[str]:
    """Expand a registered test command with runner-specific literal titles.

    Galata's upstream records select a single public Playwright title.  Its
    ``--grep`` value is a regular expression rather than a literal filter, so
    a title such as ``Alt + A`` otherwise selects no test at all.  These
    descriptors are title selectors, not arbitrary user-supplied regexes; the
    runner therefore escapes the exact recorded title immediately before the
    shell-free subprocess call.
    """

    argv = _expand_argv(definition.argv, harness_root)
    if definition.cwd != "galata":
        return argv
    indices = [index for index, value in enumerate(argv) if value == "--grep"]
    if not indices:
        return argv
    if len(indices) != 1 or indices[0] + 1 >= len(argv):
        raise LazarusUpstreamError("Galata test selector is malformed")
    argv[indices[0] + 1] = re.escape(argv[indices[0] + 1])
    return argv


def _validated_environment_overrides(
    environment_overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate the small coordinator-owned runtime environment surface."""

    if environment_overrides is None:
        return {}
    if not isinstance(environment_overrides, Mapping):
        raise LazarusUpstreamError(
            "upstream runtime environment overrides must be a string mapping"
        )
    normalized: dict[str, str] = {}
    for key, value in environment_overrides.items():
        if (
            type(key) is not str
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or type(value) is not str
            or not value
            or "\x00" in value
        ):
            raise LazarusUpstreamError(
                "upstream runtime environment override is invalid"
            )
        normalized[key] = value
    return normalized


def validate_subprocess_output_capture(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    """Validate the bounded raw output retained in a durable receipt."""

    expected = {
        "schema",
        "schema_version",
        "redaction",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} output capture schema is invalid")
    if (
        value.get("schema") != _SUBPROCESS_OUTPUT_CAPTURE_SCHEMA
        or value.get("schema_version") != _SUBPROCESS_OUTPUT_CAPTURE_VERSION
        or value.get("redaction") != "environment_and_credential_markers_v1"
    ):
        raise ValueError(f"{label} output capture schema is invalid")
    projected: dict[str, Any] = {
        "schema": _SUBPROCESS_OUTPUT_CAPTURE_SCHEMA,
        "schema_version": _SUBPROCESS_OUTPUT_CAPTURE_VERSION,
        "redaction": "environment_and_credential_markers_v1",
    }
    for stream in ("stdout", "stderr"):
        captured = value.get(stream)
        truncated = value.get(f"{stream}_truncated")
        if (
            type(captured) is not str
            or "\x00" in captured
            or len(captured.encode("utf-8")) > _SUBPROCESS_OUTPUT_CAPTURE_MAX_BYTES
            or type(truncated) is not bool
        ):
            raise ValueError(f"{label} output capture is invalid")
        if truncated and not captured.startswith(
            _SUBPROCESS_OUTPUT_CAPTURE_TRUNCATION_MARKER
        ):
            raise ValueError(f"{label} truncated output capture is invalid")
        projected[stream] = captured
        projected[f"{stream}_truncated"] = truncated
    return projected


def _capture_subprocess_output(
    stdout: bytes | str | None,
    stderr: bytes | str | None,
) -> SubprocessOutputCapture:
    captured_stdout, stdout_truncated = _bounded_output_text(stdout)
    captured_stderr, stderr_truncated = _bounded_output_text(stderr)
    return SubprocessOutputCapture(
        schema=_SUBPROCESS_OUTPUT_CAPTURE_SCHEMA,
        schema_version=_SUBPROCESS_OUTPUT_CAPTURE_VERSION,
        redaction="environment_and_credential_markers_v1",
        stdout=captured_stdout,
        stderr=captured_stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _subprocess_output_bytes(value: bytes | str | None) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def _bounded_output_text(value: bytes | str | None) -> tuple[str, bool]:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = ""
    # JSON can technically serialize NULs, but they make downstream forensic
    # viewers unreliable.  Replace them before applying the shared credential
    # scrubber and retaining a tail of the command output.
    text = text.replace("\x00", "�")
    text = safe_error_text(text, limit=len(text))
    text = _OUTPUT_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match['name']}{match['separator']}<redacted>",
        text,
    )
    encoded = text.encode("utf-8")
    if len(encoded) <= _SUBPROCESS_OUTPUT_CAPTURE_MAX_BYTES:
        return text, False
    marker = _SUBPROCESS_OUTPUT_CAPTURE_TRUNCATION_MARKER.encode("utf-8")
    tail = encoded[-(_SUBPROCESS_OUTPUT_CAPTURE_MAX_BYTES - len(marker)) :]
    # The byte boundary may fall in a multi-byte character.  Ignoring only the
    # incomplete leading sequence keeps the retained receipt valid UTF-8 and
    # still below the declared byte limit.
    return _SUBPROCESS_OUTPUT_CAPTURE_TRUNCATION_MARKER + tail.decode(
        "utf-8", errors="ignore"
    ), True


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
