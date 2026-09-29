"""Closed offline preparation profiles for registered Luanti Lua regressions.

This module intentionally prepares no source tree and executes no candidate.
It binds a future fixed source capsule, derives a production-only inverse
patch, and verifies that a registered local Busted runtime is complete.  A
future runner may use the returned plan for bounded repetitions once the
registered runtime has been supplied.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from .models import CandidateKey
from .pipeline import StageDecision
from .screening import RawPatchEvidence, resolve_raw_patch_evidence
from .stable_protocol import ArtifactReference, StableArtifact, load_stable_artifact


OFFLINE_BUSTED_DEPENDENCY_ID = "luanti-busted-runtime"
OFFLINE_BUSTED_RUNTIME_ROOT = "private/dependencies/luanti-lua-busted/1.0"
OFFLINE_BUSTED_COMPONENTS = (
    "busted",
    "cliargs",
    "dkjson",
    "luafilesystem",
    "lua_term",
    "luassert",
    "luasystem",
    "mediator_lua",
    "penlight",
    "say",
)
OFFLINE_BUSTED_MODULE_VERSIONS = {
    "busted": "2.2.0-1",
    "lua_cliargs": "3.0-2",
    "luasystem": "0.7.1-1",
    "dkjson": "2.11-1",
    "say": "1.4.1-3",
    "luassert": "1.9.0-1",
    "lua-term": "0.8-1",
    "penlight": "1.15.0-1",
    "luafilesystem": "1.9.0-1",
    "mediator_lua": "1.1.2-0",
}
_OFFLINE_RUNTIME_VERSION = "1.0"
_OFFLINE_RUNTIME_MANIFEST = "runtime.json"
_OFFLINE_RUNTIME_RUNNER = "bin/busted"
_OFFLINE_RUNTIME_INTERPRETER = "bin/luajit"
_BUSTED_VERSION = re.compile(r"[1-9][0-9]*(?:\.[0-9]+){1,2}(?:-[1-9][0-9]*)?")
_OFFLINE_RUNTIME_LUA_PATHS = (
    "share/lua/5.1/?.lua",
    "share/lua/5.1/?/init.lua",
)
_OFFLINE_RUNTIME_LUA_CPATHS = ("lib/lua/5.1/?.so",)
_SOURCE_PACKET_INPUT = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)


def _require_safe_relative(value: str, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path


@dataclass(frozen=True)
class LuaRegressionProfile:
    """One closed source/test shape for a registered Luanti Lua regression."""

    candidate: CandidateKey
    profile_id: str
    source_packet_reference: ArtifactReference
    fixed_capsule_reference: ArtifactReference
    fixed_source_tree_path: str
    production_paths: tuple[str, ...]
    support_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    allowed_patch_paths: tuple[str, ...]
    patch_test_markers: tuple[str, ...]
    fixed_test_markers: tuple[str, ...]
    f2p_selector: str
    p2p_selector: str
    f2p_buggy_outcome: Literal["failure", "timed_out"]
    repetitions: int

    def __post_init__(self) -> None:
        if self.candidate.repository_key != "luanti":
            raise ValueError("Lua regression profile repository is invalid")
        if not self.profile_id or self.repetitions != 3:
            raise ValueError("Lua regression profile execution policy is invalid")
        if self.source_packet_reference != ArtifactReference(
            (
                f"candidate_source_packet/luanti/"
                f"{self.candidate.change_number}/source/v2"
            ),
            "candidate_source_packet",
            "2.0",
        ):
            raise ValueError("Lua regression profile source packet is invalid")
        if self.fixed_capsule_reference != ArtifactReference(
            (f"fixed_source_capsule/luanti/{self.candidate.change_number}/fixed/v2"),
            "fixed_source_capsule",
            "2.0",
        ):
            raise ValueError("Lua regression profile fixed capsule is invalid")
        _require_safe_relative(self.fixed_source_tree_path, "fixed source tree path")
        for path in (*self.production_paths, *self.support_paths, *self.test_paths):
            _require_safe_relative(path, "profile path")
        if (
            not self.production_paths
            or not self.support_paths
            or not self.test_paths
            or len(set((*self.production_paths, *self.support_paths, *self.test_paths)))
            != len((*self.production_paths, *self.support_paths, *self.test_paths))
            or len(set(self.allowed_patch_paths)) != len(self.allowed_patch_paths)
            or set(self.production_paths) | set(self.test_paths)
            > set(self.allowed_patch_paths)
        ):
            raise ValueError("Lua regression profile patch scope is invalid")
        if not all(
            isinstance(value, str) and value
            for value in (
                *self.patch_test_markers,
                *self.fixed_test_markers,
                self.f2p_selector,
                self.p2p_selector,
            )
        ):
            raise ValueError("Lua regression profile test binding is invalid")


def _profile(
    number: int,
    *,
    profile_id: str,
    allowed_patch_paths: tuple[str, ...],
    patch_test_markers: tuple[str, ...],
    fixed_test_markers: tuple[str, ...],
    f2p_selector: str,
    p2p_selector: str,
    f2p_buggy_outcome: Literal["failure", "timed_out"],
) -> LuaRegressionProfile:
    candidate = CandidateKey("luanti", number)
    return LuaRegressionProfile(
        candidate=candidate,
        profile_id=profile_id,
        source_packet_reference=ArtifactReference(
            f"candidate_source_packet/luanti/{number}/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        fixed_capsule_reference=ArtifactReference(
            f"fixed_source_capsule/luanti/{number}/fixed/v2",
            "fixed_source_capsule",
            "2.0",
        ),
        fixed_source_tree_path=(
            f"private/fixed-capsules/luanti/{number}/fixed-source-v2"
        ),
        production_paths=("builtin/common/misc_helpers.lua",),
        support_paths=("builtin/common/vector.lua",),
        test_paths=("builtin/common/tests/misc_helpers_spec.lua",),
        allowed_patch_paths=allowed_patch_paths,
        patch_test_markers=patch_test_markers,
        fixed_test_markers=fixed_test_markers,
        f2p_selector=f2p_selector,
        p2p_selector=p2p_selector,
        f2p_buggy_outcome=f2p_buggy_outcome,
        repetitions=3,
    )


_PROFILES = {
    12703: LuaRegressionProfile(
        candidate=CandidateKey("luanti", 12703),
        profile_id="luanti-12703-contentdb-overwrite-v1",
        source_packet_reference=ArtifactReference(
            "candidate_source_packet/luanti/12703/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        fixed_capsule_reference=ArtifactReference(
            "fixed_source_capsule/luanti/12703/fixed/v2",
            "fixed_source_capsule",
            "2.0",
        ),
        fixed_source_tree_path="private/fixed-capsules/luanti/12703/fixed-source-v2",
        production_paths=("builtin/mainmenu/pkgmgr.lua",),
        support_paths=("builtin/common/misc_helpers.lua",),
        test_paths=("builtin/mainmenu/tests/pkgmgr_spec.lua",),
        allowed_patch_paths=(
            "builtin/mainmenu/pkgmgr.lua",
            "builtin/mainmenu/tests/pkgmgr_spec.lua",
        ),
        patch_test_markers=(
            'it("installs mod to target (update)"',
            '{ "delete_dir", "/tmp/alt-target" }',
        ),
        fixed_test_markers=(
            'it("installs mod to target (update)"',
            'it("prevents installing other as texture pack"',
            'setfenv(loadfile("builtin/common/misc_helpers.lua"), env)()',
        ),
        f2p_selector=r"^install_dir installs mod to target %(update%)$",
        p2p_selector=r"^install_dir prevents installing other as texture pack$",
        f2p_buggy_outcome="failure",
        repetitions=3,
    ),
    13132: _profile(
        13132,
        profile_id="luanti-13132-empty-separator-v1",
        allowed_patch_paths=(
            "builtin/common/misc_helpers.lua",
            "builtin/common/tests/misc_helpers_spec.lua",
            "doc/lua_api.txt",
        ),
        patch_test_markers=(
            'it("rejects empty separator"',
            'string.split("", "")',
        ),
        fixed_test_markers=(
            'it("rejects empty separator"',
            'string.split("", "")',
            'it("removes empty"',
        ),
        f2p_selector="string split() rejects empty separator",
        p2p_selector="string split() removes empty",
        f2p_buggy_outcome="timed_out",
    ),
    14757: _profile(
        14757,
        profile_id="luanti-14757-math-round-v1",
        allowed_patch_paths=(
            "builtin/common/misc_helpers.lua",
            "builtin/common/tests/misc_helpers_spec.lua",
        ),
        patch_test_markers=(
            'it("round()"',
            "math.round(0.49999999999999994)",
        ),
        fixed_test_markers=(
            'it("round()"',
            "math.round(0.49999999999999994)",
            'it("escapes"',
        ),
        f2p_selector="math round()",
        p2p_selector="formspec_escape escapes",
        f2p_buggy_outcome="failure",
    ),
    9722: LuaRegressionProfile(
        candidate=CandidateKey("luanti", 9722),
        profile_id="luanti-9722-reversible-number-serialize-v1",
        source_packet_reference=ArtifactReference(
            "candidate_source_packet/luanti/9722/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        fixed_capsule_reference=ArtifactReference(
            "fixed_source_capsule/luanti/9722/fixed/v2",
            "fixed_source_capsule",
            "2.0",
        ),
        fixed_source_tree_path="private/fixed-capsules/luanti/9722/fixed-source-v2",
        production_paths=("builtin/common/serialize.lua",),
        support_paths=("builtin/common/vector.lua",),
        test_paths=("builtin/common/tests/serialize_spec.lua",),
        allowed_patch_paths=(
            "builtin/common/serialize.lua",
            "builtin/common/tests/serialize_spec.lua",
        ),
        patch_test_markers=(
            'it("handles precise numbers"',
            "0.2695949158945771",
            'it("handles big integers"',
            "269594915894577",
        ),
        fixed_test_markers=(
            'it("handles precise numbers"',
            "0.2695949158945771",
            'it("handles big integers"',
            "269594915894577",
            'it("handles characters"',
        ),
        f2p_selector="serialize handles precise numbers",
        p2p_selector="serialize handles characters",
        f2p_buggy_outcome="failure",
        repetitions=3,
    ),
}


@dataclass(frozen=True)
class OfflineBustedRuntime:
    """A complete registered local Busted runtime with no path fallback."""

    dependency_id: str
    semantic_version: str
    busted_version: str
    root: Path
    runner: Path
    interpreter: Path
    modules: Mapping[str, Path]
    module_versions: Mapping[str, str]
    lua_paths: tuple[Path, ...]
    lua_cpaths: tuple[Path, ...]
    closure_files: tuple[Path, ...]

    def __post_init__(self) -> None:
        if (
            self.dependency_id != OFFLINE_BUSTED_DEPENDENCY_ID
            or self.semantic_version != _OFFLINE_RUNTIME_VERSION
            or not self.busted_version
            or not self.lua_paths
            or not self.lua_cpaths
            or not self.closure_files
        ):
            raise ValueError("offline Busted runtime identity is invalid")
        if not self.root.is_absolute():
            raise ValueError("offline Busted runtime root is invalid")
        if set(self.modules) != set(OFFLINE_BUSTED_COMPONENTS):
            raise ValueError("offline Busted runtime closure is invalid")
        if dict(self.module_versions) != OFFLINE_BUSTED_MODULE_VERSIONS:
            raise ValueError("offline Busted runtime module versions are invalid")


@dataclass(frozen=True)
class FixedCapsuleBinding:
    """The same fixed source capsule supplies tests to fixed and buggy runs."""

    reference: ArtifactReference
    source_tree: Path
    support_paths: tuple[str, ...]
    test_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.reference.artifact_kind != "fixed_source_capsule"
            or self.reference.semantic_version != "2.0"
            or not self.source_tree.is_absolute()
            or not self.support_paths
            or not self.test_paths
        ):
            raise ValueError("fixed capsule binding is invalid")
        if set(self.support_paths) & set(self.test_paths):
            raise ValueError("fixed capsule binding is invalid")
        for path in (*self.support_paths, *self.test_paths):
            _require_safe_relative(path, "fixed capsule test path")


@dataclass(frozen=True)
class ProductionInversePlan:
    """Reverse only production hunks; upstream test/doc hunks stay fixed."""

    paths: tuple[str, ...]
    patch: bytes
    buggy_apply_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.paths
            or not self.patch
            or self.buggy_apply_argv
            != ("git", "apply", "-R", "--whitespace=nowarn", "-")
        ):
            raise ValueError("production inverse plan is invalid")
        for path in self.paths:
            _require_safe_relative(path, "production inverse path")


@dataclass(frozen=True)
class LuaRegressionRun:
    """One future fixed/buggy test invocation repeated under the watchdog."""

    phase: Literal["f2p", "p2p"]
    selector: str
    test_path: str
    argv: tuple[str, ...]
    repetitions: int
    fixed_expected_outcome: Literal["pass"]
    buggy_expected_outcome: Literal["failure", "pass", "timed_out"]

    def __post_init__(self) -> None:
        if (
            self.phase not in {"f2p", "p2p"}
            or not self.selector
            or self.repetitions != 3
            or self.fixed_expected_outcome != "pass"
            or not self.argv
        ):
            raise ValueError("Lua regression run is invalid")
        _require_safe_relative(self.test_path, "Lua regression test path")


@dataclass(frozen=True)
class WatchdogReceipt:
    """A non-persisted process result with no content-derived fields."""

    outcome: Literal["completed", "launch_failed", "timed_out"]
    returncode: int | None
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        if (
            self.outcome not in {"completed", "launch_failed", "timed_out"}
            or self.returncode is not None
            and type(self.returncode) is not int
            or type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
        ):
            raise ValueError("watchdog receipt is invalid")


@dataclass(frozen=True)
class PythonSubprocessWatchdog:
    """Process-group watchdog for a later local-only Lua invocation."""

    timeout_seconds: int

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("watchdog timeout is invalid")

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> WatchdogReceipt:
        """Run one later local process, bounding a hanging buggy regression.

        Preparation functions in this module never call this method.  It is
        deliberately a narrow interface so an execution stage cannot fall
        back to a shell, a PATH lookup, or an unbounded child process.
        """

        if not argv or any(type(value) is not str or not value for value in argv):
            raise ValueError("watchdog argv is invalid")
        if not cwd.is_absolute():
            raise ValueError("watchdog working directory is invalid")
        if env is not None and (
            type(env) is not dict
            or any(
                type(key) is not str or type(value) is not str
                for key, value in env.items()
            )
        ):
            raise ValueError("watchdog environment is invalid")
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return WatchdogReceipt(
                outcome="launch_failed",
                returncode=None,
                stdout=b"",
                stderr=str(error).encode("utf-8", errors="replace"),
            )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return WatchdogReceipt(
                outcome="timed_out",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return WatchdogReceipt(
            outcome="completed",
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


@dataclass(frozen=True)
class LuaRegressionExecutionPlan:
    """A future-only fixed/buggy execution plan for exactly three runs each."""

    profile: LuaRegressionProfile
    runtime: OfflineBustedRuntime
    fixed_capsule: FixedCapsuleBinding
    source_manifest_path: str
    raw_patch_path: str
    production_inverse: ProductionInversePlan
    runs: tuple[LuaRegressionRun, LuaRegressionRun]
    watchdog: PythonSubprocessWatchdog

    def __post_init__(self) -> None:
        if (
            len(self.runs) != 2
            or tuple(run.phase for run in self.runs) != ("f2p", "p2p")
            or self.fixed_capsule.reference != self.profile.fixed_capsule_reference
            or self.fixed_capsule.support_paths != self.profile.support_paths
            or self.fixed_capsule.test_paths != self.profile.test_paths
            or self.production_inverse.paths != self.profile.production_paths
        ):
            raise ValueError("Lua regression execution plan is invalid")
        _require_safe_relative(self.source_manifest_path, "source manifest path")
        _require_safe_relative(self.raw_patch_path, "raw patch path")


def profile_for_candidate(candidate: CandidateKey) -> LuaRegressionProfile:
    """Return one explicitly registered offline Lua profile."""

    if type(candidate) is not CandidateKey:
        raise ValueError("Lua regression candidate is invalid")
    try:
        profile = _PROFILES[candidate.change_number]
    except KeyError as error:
        raise ValueError(
            "candidate has no registered Lua regression profile"
        ) from error
    if candidate != profile.candidate:
        raise ValueError("candidate has no registered Lua regression profile")
    return profile


def plan_luanti_lua_regression_execution(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> LuaRegressionExecutionPlan | StageDecision:
    """Prepare, but never execute, a closed offline Lua regression plan.

    The local Busted runtime is checked first.  Its registered root is the
    only allowed runtime source; the absence of that root is a typed terminal,
    not permission to use PATH, Homebrew, LuaRocks, or the network.
    """

    profile = profile_for_candidate(candidate)
    root = _safe_artifact_root(artifact_root)
    runtime = _offline_runtime_or_block(root, profile)
    if isinstance(runtime, StageDecision):
        return runtime
    source_packet = _source_packet_or_block(root, profile)
    if isinstance(source_packet, StageDecision):
        return source_packet
    fixed_capsule = _fixed_capsule_or_block(root, profile)
    if isinstance(fixed_capsule, StageDecision):
        return fixed_capsule
    raw_evidence = _raw_evidence_or_block(root, profile, source_packet)
    if isinstance(raw_evidence, StageDecision):
        return raw_evidence
    inverse = _production_inverse_plan(raw_evidence, profile)
    runs = _planned_runs(profile, runtime)
    return LuaRegressionExecutionPlan(
        profile=profile,
        runtime=runtime,
        fixed_capsule=fixed_capsule,
        source_manifest_path=raw_evidence.source_manifest_path,
        raw_patch_path=raw_evidence.raw_patch_path,
        production_inverse=inverse,
        runs=runs,
        watchdog=PythonSubprocessWatchdog(timeout_seconds=10),
    )


def _offline_runtime_or_block(
    root: Path,
    profile: LuaRegressionProfile,
) -> OfflineBustedRuntime | StageDecision:
    runtime_root, root_failure = _registered_directory(
        root, OFFLINE_BUSTED_RUNTIME_ROOT
    )
    expected_entries = [
        _OFFLINE_RUNTIME_MANIFEST,
        _OFFLINE_RUNTIME_RUNNER,
        _OFFLINE_RUNTIME_INTERPRETER,
        "share/lua/5.1",
        "lib/lua/5.1",
    ]
    if root_failure == "missing":
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_unavailable",
            {"missing_entries": expected_entries},
        )
    if root_failure is not None:
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_unsafe",
            {"unsafe_entry": "."},
        )
    assert runtime_root is not None
    manifest_path, failure = _safe_regular_file(runtime_root, _OFFLINE_RUNTIME_MANIFEST)
    if failure == "missing":
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_incomplete",
            {"missing_entries": [_OFFLINE_RUNTIME_MANIFEST]},
        )
    if failure is not None:
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_unsafe",
            {"unsafe_entry": _OFFLINE_RUNTIME_MANIFEST},
        )
    assert manifest_path is not None
    try:
        manifest = _strict_json_object(manifest_path, "offline Busted runtime")
    except RuntimeError:
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_invalid",
            {"invalid_entry": _OFFLINE_RUNTIME_MANIFEST},
        )
    parsed = _parse_runtime_manifest(manifest)
    if parsed is None:
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_invalid",
            {"invalid_entry": _OFFLINE_RUNTIME_MANIFEST},
        )
    (
        runner_relative,
        interpreter_relative,
        busted_version,
        modules,
        module_versions,
        lua_paths,
        lua_cpaths,
        closure_files,
    ) = parsed
    rock_manifests = [
        f"lib/luarocks/rocks-5.1/{rock}/{version}/rock_manifest"
        for rock, version in module_versions.items()
    ]
    required_entries = [
        runner_relative,
        interpreter_relative,
        *modules.values(),
        *rock_manifests,
    ]
    for relative in required_entries:
        path, failure = _safe_regular_file(runtime_root, relative)
        if failure == "missing":
            return _runtime_blocker(
                profile,
                "offline_busted_runtime_incomplete",
                {"missing_entries": [relative]},
            )
        if failure is not None:
            return _runtime_blocker(
                profile,
                "offline_busted_runtime_unsafe",
                {"unsafe_entry": relative},
            )
        assert path is not None
    actual_closure, unsafe_entry = _runtime_closure(runtime_root)
    if unsafe_entry is not None:
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_unsafe",
            {"unsafe_entry": unsafe_entry},
        )
    if actual_closure != set(closure_files):
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_invalid",
            {"invalid_entry": _OFFLINE_RUNTIME_MANIFEST},
        )
    runner = runtime_root / runner_relative
    interpreter = runtime_root / interpreter_relative
    if not os.access(runner, os.X_OK):
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_incomplete",
            {"missing_entries": [runner_relative]},
        )
    if not os.access(interpreter, os.X_OK):
        return _runtime_blocker(
            profile,
            "offline_busted_runtime_incomplete",
            {"missing_entries": [interpreter_relative]},
        )
    return OfflineBustedRuntime(
        dependency_id=OFFLINE_BUSTED_DEPENDENCY_ID,
        semantic_version=_OFFLINE_RUNTIME_VERSION,
        busted_version=busted_version,
        root=runtime_root,
        runner=runner,
        interpreter=interpreter,
        modules={name: runtime_root / relative for name, relative in modules.items()},
        module_versions=module_versions,
        lua_paths=tuple(runtime_root / relative for relative in lua_paths),
        lua_cpaths=tuple(runtime_root / relative for relative in lua_cpaths),
        closure_files=tuple(runtime_root / relative for relative in closure_files),
    )


def _runtime_blocker(
    profile: LuaRegressionProfile,
    reason: str,
    details: Mapping[str, object],
) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {
            "profile_id": profile.profile_id,
            "reason": reason,
            "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
            "semantic_version": _OFFLINE_RUNTIME_VERSION,
            "runtime_root": OFFLINE_BUSTED_RUNTIME_ROOT,
            **details,
        },
    )


def _parse_runtime_manifest(
    value: Mapping[str, object],
) -> (
    tuple[
        str,
        str,
        str,
        dict[str, str],
        dict[str, str],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]
    | None
):
    if set(value) != {
        "dependency_id",
        "semantic_version",
        "busted_version",
        "lua_abi",
        "runner",
        "interpreter",
        "modules",
        "module_versions",
        "lua_paths",
        "lua_cpaths",
        "closure_files",
    }:
        return None
    if (
        value.get("dependency_id") != OFFLINE_BUSTED_DEPENDENCY_ID
        or value.get("semantic_version") != _OFFLINE_RUNTIME_VERSION
        or value.get("lua_abi") != "5.1"
        or not isinstance(value.get("busted_version"), str)
        or not _BUSTED_VERSION.fullmatch(value["busted_version"])
        or not isinstance(value.get("runner"), str)
        or not isinstance(value.get("interpreter"), str)
        or not isinstance(value.get("modules"), dict)
        or not isinstance(value.get("module_versions"), dict)
        or not isinstance(value.get("lua_paths"), list)
        or not isinstance(value.get("lua_cpaths"), list)
        or not isinstance(value.get("closure_files"), list)
    ):
        return None
    runner = value["runner"]
    interpreter = value["interpreter"]
    modules = value["modules"]
    module_versions = value["module_versions"]
    lua_paths = value["lua_paths"]
    lua_cpaths = value["lua_cpaths"]
    closure_files = value["closure_files"]
    if (
        runner != _OFFLINE_RUNTIME_RUNNER
        or interpreter != _OFFLINE_RUNTIME_INTERPRETER
        or set(modules) != set(OFFLINE_BUSTED_COMPONENTS)
        or any(type(path) is not str for path in modules.values())
        or len(set(modules.values())) != len(modules)
        or module_versions != OFFLINE_BUSTED_MODULE_VERSIONS
        or tuple(lua_paths) != _OFFLINE_RUNTIME_LUA_PATHS
        or tuple(lua_cpaths) != _OFFLINE_RUNTIME_LUA_CPATHS
        or any(type(path) is not str for path in closure_files)
        or closure_files != sorted(closure_files)
        or len(set(closure_files)) != len(closure_files)
        or runner not in closure_files
        or interpreter not in closure_files
        or not set(modules.values()).issubset(closure_files)
    ):
        return None
    try:
        _require_safe_relative(runner, "offline Busted runner")
        _require_safe_relative(interpreter, "offline Lua interpreter")
        for path in modules.values():
            _require_safe_relative(path, "offline Busted module")
        for path in (*lua_paths, *lua_cpaths):
            _require_safe_relative(path, "offline Lua search path")
        for path in closure_files:
            _require_safe_relative(path, "offline Busted closure file")
    except RuntimeError:
        return None
    return (
        runner,
        interpreter,
        value["busted_version"],
        dict(modules),
        dict(module_versions),
        tuple(lua_paths),
        tuple(lua_cpaths),
        tuple(closure_files),
    )


def _runtime_closure(root: Path) -> tuple[set[str] | None, str | None]:
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            relative = entry.relative_to(root).as_posix()
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None, relative
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                if relative != _OFFLINE_RUNTIME_MANIFEST:
                    files.add(relative)
            else:
                return None, relative
    return files, None


def _source_packet_or_block(
    root: Path,
    profile: LuaRegressionProfile,
) -> StableArtifact | StageDecision:
    try:
        packet = load_stable_artifact(
            root,
            artifact_id=profile.source_packet_reference.artifact_id,
            artifact_kind=profile.source_packet_reference.artifact_kind,
            semantic_version=profile.source_packet_reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return _blocker(
                profile,
                "persisted_source_packet_unavailable",
                {"source_packet_id": profile.source_packet_reference.artifact_id},
            )
        raise
    if (
        packet.inputs != (_SOURCE_PACKET_INPUT,)
        or packet.payload.get("candidate") != profile.candidate.to_value()
        or packet.payload.get("complete") is not True
        or type(packet.payload.get("raw_evidence_path")) is not str
    ):
        raise RuntimeError("Lua regression source packet is invalid")
    return packet


def _fixed_capsule_or_block(
    root: Path,
    profile: LuaRegressionProfile,
) -> FixedCapsuleBinding | StageDecision:
    try:
        capsule = load_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return _blocker(
                profile,
                "fixed_capsule_unavailable",
                {"fixed_capsule_id": profile.fixed_capsule_reference.artifact_id},
            )
        raise
    declared_source_tree = capsule.payload.get("source_tree_path")
    _require_safe_relative(declared_source_tree, "fixed capsule source tree path")
    expected_payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "support_paths": list(profile.support_paths),
        "test_paths": list(profile.test_paths),
    }
    if (
        capsule.inputs != (profile.source_packet_reference,)
        or capsule.payload != expected_payload
    ):
        raise RuntimeError("Lua regression fixed capsule binding is invalid")
    source_tree, source_tree_failure = _registered_directory(
        root, profile.fixed_source_tree_path
    )
    if source_tree_failure == "missing":
        return _blocker(
            profile,
            "fixed_capsule_source_unavailable",
            {"source_tree_path": profile.fixed_source_tree_path},
        )
    if source_tree_failure is not None:
        raise RuntimeError("Lua regression fixed capsule source tree is unsafe")
    assert source_tree is not None
    for relative in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
    ):
        path, failure = _safe_regular_file(source_tree, relative)
        if failure == "missing":
            return _blocker(
                profile,
                "fixed_capsule_source_unavailable",
                {
                    "source_tree_path": profile.fixed_source_tree_path,
                    "missing_path": relative,
                },
            )
        if failure is not None:
            raise RuntimeError("Lua regression fixed capsule source path is unsafe")
        assert path is not None
    for test_path in profile.test_paths:
        content = (source_tree / test_path).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        if not all(marker in content for marker in profile.fixed_test_markers):
            raise RuntimeError("Lua regression fixed capsule test binding is invalid")
    return FixedCapsuleBinding(
        reference=profile.fixed_capsule_reference,
        source_tree=source_tree,
        support_paths=profile.support_paths,
        test_paths=profile.test_paths,
    )


def _raw_evidence_or_block(
    root: Path,
    profile: LuaRegressionProfile,
    source_packet: StableArtifact,
) -> RawPatchEvidence | StageDecision:
    try:
        return resolve_raw_patch_evidence(
            root,
            candidate=profile.candidate,
            source_manifest_path=source_packet.payload["raw_evidence_path"],
        )
    except RuntimeError as error:
        if str(error) in {
            "source manifest file is missing",
            "source patch file is missing",
        }:
            return _blocker(
                profile,
                "persisted_source_evidence_unavailable",
                {"source_packet_id": profile.source_packet_reference.artifact_id},
            )
        raise


def _production_inverse_plan(
    raw_evidence: RawPatchEvidence,
    profile: LuaRegressionProfile,
) -> ProductionInversePlan:
    sections = _patch_sections(raw_evidence.patch_path.read_bytes())
    paths = tuple(section.path for section in sections)
    if len(set(paths)) != len(paths) or set(paths) != set(profile.allowed_patch_paths):
        raise RuntimeError("Lua regression raw patch is outside the registered profile")
    by_path = {section.path: section for section in sections}
    test_content = "".join("".join(by_path[path].lines) for path in profile.test_paths)
    if not all(marker in test_content for marker in profile.patch_test_markers):
        raise RuntimeError("Lua regression raw patch does not bind the regression test")
    production = tuple(by_path[path] for path in profile.production_paths)
    return ProductionInversePlan(
        paths=profile.production_paths,
        patch="".join("".join(section.lines) for section in production).encode(
            "utf-8", errors="surrogateescape"
        ),
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
    )


@dataclass(frozen=True)
class _PatchSection:
    path: str
    lines: tuple[str, ...]


def _patch_sections(content: bytes) -> tuple[_PatchSection, ...]:
    if not content:
        raise RuntimeError("Lua regression raw patch is empty")
    sections: list[_PatchSection] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append(_PatchSection(current_path, tuple(current_lines)))
            current_path = _patch_path_from_header(line)
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        sections.append(_PatchSection(current_path, tuple(current_lines)))
    if not sections:
        raise RuntimeError("Lua regression raw patch is invalid")
    return tuple(sections)


def _patch_path_from_header(line: str) -> str:
    try:
        parts = shlex.split(line.rstrip("\r\n"))
    except ValueError as error:
        raise RuntimeError("Lua regression raw patch header is invalid") from error
    if (
        len(parts) != 4
        or parts[:2] != ["diff", "--git"]
        or not parts[2].startswith("a/")
        or not parts[3].startswith("b/")
        or parts[2][2:] != parts[3][2:]
    ):
        raise RuntimeError("Lua regression raw patch header is invalid")
    path = parts[2][2:]
    _require_safe_relative(path, "Lua regression raw patch path")
    return path


def _planned_runs(
    profile: LuaRegressionProfile,
    runtime: OfflineBustedRuntime,
) -> tuple[LuaRegressionRun, LuaRegressionRun]:
    test_path = profile.test_paths[0]

    def make_run(
        phase: Literal["f2p", "p2p"],
        selector: str,
        buggy_expected_outcome: Literal["failure", "pass", "timed_out"],
    ) -> LuaRegressionRun:
        return LuaRegressionRun(
            phase=phase,
            selector=selector,
            test_path=test_path,
            argv=(
                str(runtime.runner),
                test_path,
                f"--name={selector}",
            ),
            repetitions=profile.repetitions,
            fixed_expected_outcome="pass",
            buggy_expected_outcome=buggy_expected_outcome,
        )

    return (
        make_run("f2p", profile.f2p_selector, profile.f2p_buggy_outcome),
        make_run("p2p", profile.p2p_selector, "pass"),
    )


def _blocker(
    profile: LuaRegressionProfile,
    reason: str,
    details: Mapping[str, object],
) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {"profile_id": profile.profile_id, "reason": reason, **details},
    )


def _safe_artifact_root(artifact_root: str | Path) -> Path:
    root = Path(artifact_root).absolute()
    try:
        info = root.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return root.resolve(strict=True)


def _registered_directory(
    root: Path, relative_path: str
) -> tuple[Path | None, str | None]:
    try:
        relative = _require_safe_relative(relative_path, "registered directory path")
    except RuntimeError:
        return None, "unsafe"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None, "missing"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None, "unsafe"
    return current.resolve(strict=True), None


def _safe_regular_file(
    root: Path, relative_path: str
) -> tuple[Path | None, str | None]:
    try:
        relative = _require_safe_relative(relative_path, "registered file path")
    except RuntimeError:
        return None, "unsafe"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None, "missing"
        if stat.S_ISLNK(info.st_mode):
            return None, "unsafe"
    if not stat.S_ISREG(current.lstat().st_mode):
        return None, "unsafe"
    return current, None


def _strict_json_object(path: Path, label: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"{label} has duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"{label} has an invalid number")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} is invalid")
    return value


__all__ = [
    "OFFLINE_BUSTED_COMPONENTS",
    "OFFLINE_BUSTED_DEPENDENCY_ID",
    "OFFLINE_BUSTED_MODULE_VERSIONS",
    "OFFLINE_BUSTED_RUNTIME_ROOT",
    "FixedCapsuleBinding",
    "LuaRegressionExecutionPlan",
    "LuaRegressionProfile",
    "OfflineBustedRuntime",
    "ProductionInversePlan",
    "PythonSubprocessWatchdog",
    "WatchdogReceipt",
    "plan_luanti_lua_regression_execution",
    "profile_for_candidate",
]
