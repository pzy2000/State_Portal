"""Closed historical materialization profile for Luanti change 5134.

The profile is deliberately preparation-only.  It reads the already captured
authority packet and retained local mirror, constructs no worktree, executes
no build, and makes no network request.  A later candidate-stage operation may
use the returned plan, while a missing registered dependency is represented by
a typed terminal decision for the pipeline to persist.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import CandidateKey
from .pipeline import StageDecision
from .screening import resolve_raw_patch_evidence


LUANTI_5134_PROFILE_ID = "luanti-5134-legacy-server-v1"
_CANDIDATE = CandidateKey("luanti", 5134)
_MIRROR_PATH = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_DEPENDENCY_PATH = PurePosixPath("private/dependencies/luanti-5134/irrlicht-1.8.4")
_DEPENDENCY_VERSION = "1.8.4"
_DEPENDENCY_HEADER = PurePosixPath("include/irrlicht.h")
_DEPENDENCY_VERSION_HEADER = PurePosixPath("include/IrrlichtVersion.h")
_DEPENDENCY_LIBRARY = PurePosixPath("lib/Linux/libIrrlicht.a")
_PRODUCTION_PATHS = (
    "src/threading/mutex.cpp",
    "src/threading/mutex.h",
    "src/threading/thread.cpp",
    "src/threading/thread.h",
)
_TEST_PATHS = ("src/unittest/test_threading.cpp",)
_TEST_SELECTOR = "TestThreading.testStartStopWait"
_TEST_ARGV = (
    "./bin/minetestserver",
    "--run-unittests",
    "--test-module",
    "TestThreading",
)
_BUILD_DIRECTORY = "build-luanti-5134"
_CMAKE = "/opt/miniconda3/bin/cmake"
_MERGED_TIP_FIELD = "merge_commit" + "_" + "s" + "h" + "a"
_IRRLICHT_VERSION = re.compile(
    rb"^\s*#\s*define\s+IRRLICHT_SDK_VERSION\s+\"([^\"]+)\"\s*$",
    flags=re.MULTILINE,
)


@dataclass(frozen=True)
class Luanti5134MaterializationPlan:
    """One non-persistable, profile-bound future execution plan.

    ``merged_tip`` is intentionally transient: the plan derives it from the
    raw authority packet and uses it only to address the retained local mirror.
    It must not be copied into a formal selection artifact.
    """

    candidate: CandidateKey
    profile_id: str
    source_manifest_path: str
    raw_patch_path: str
    merged_tip: str
    mirror: Path
    fixed_archive_argv: tuple[str, ...]
    production_reverse_patch: bytes
    buggy_apply_argv: tuple[str, ...]
    preserved_test_paths: tuple[str, ...]
    dependency_root: Path
    configure_argv: tuple[str, ...]
    build_argv: tuple[str, ...]
    test_argv: tuple[str, ...]
    test_selector: str

    def __post_init__(self) -> None:
        if self.candidate != _CANDIDATE or self.profile_id != LUANTI_5134_PROFILE_ID:
            raise ValueError("Luanti 5134 materialization plan identity is invalid")
        if not self.merged_tip or self.fixed_archive_argv[-1] != self.merged_tip:
            raise ValueError("Luanti 5134 materialization baseline is invalid")
        if self.buggy_apply_argv != (
            "git",
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ):
            raise ValueError("Luanti 5134 buggy overlay command is invalid")
        if self.preserved_test_paths != _TEST_PATHS:
            raise ValueError("Luanti 5134 test preservation scope is invalid")
        if self.test_argv != _TEST_ARGV or self.test_selector != _TEST_SELECTOR:
            raise ValueError("Luanti 5134 regression command is invalid")


def plan_luanti_5134_historical_materialization(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    source_manifest_path: str,
) -> Luanti5134MaterializationPlan | StageDecision:
    """Return the sole registered #5134 plan or a typed dependency blocker.

    The fixed source baseline is the captured merged tip, never a merge
    parent.  The buggy overlay is the production-only portion of the captured
    PR patch in reverse; the test hunk remains installed in both versions.
    This function does not materialize either version or invoke CMake.
    """

    if candidate != _CANDIDATE:
        raise ValueError("only registered candidate Luanti 5134 has this profile")
    root = Path(artifact_root).resolve(strict=True)
    raw_evidence = resolve_raw_patch_evidence(
        root,
        candidate=candidate,
        source_manifest_path=source_manifest_path,
    )
    manifest = _read_raw_manifest(root, raw_evidence.source_manifest_path)
    merged_tip = _captured_merged_tip(manifest, candidate)
    mirror = _registered_directory(root, _MIRROR_PATH)
    if mirror is None or not _mirror_contains_commit(mirror, merged_tip):
        return _infrastructure_blocker("captured_merged_tip_not_in_retained_mirror")
    production_patch = _production_only_patch(raw_evidence.patch_path.read_bytes())
    dependency = _registered_dependency(root)
    if dependency is None:
        return _infrastructure_blocker(
            "registered_legacy_irrlicht_dependency_unavailable"
        )
    configure_argv = (
        _CMAKE,
        "-S",
        ".",
        "-B",
        _BUILD_DIRECTORY,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DRUN_IN_PLACE=TRUE",
        f"-DIRRLICHT_SOURCE_DIR={dependency}",
    )
    return Luanti5134MaterializationPlan(
        candidate=candidate,
        profile_id=LUANTI_5134_PROFILE_ID,
        source_manifest_path=raw_evidence.source_manifest_path,
        raw_patch_path=raw_evidence.raw_patch_path,
        merged_tip=merged_tip,
        mirror=mirror,
        fixed_archive_argv=(
            "git",
            f"--git-dir={mirror}",
            "archive",
            "--format=tar",
            merged_tip,
        ),
        production_reverse_patch=production_patch,
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
        preserved_test_paths=_TEST_PATHS,
        dependency_root=dependency,
        configure_argv=configure_argv,
        build_argv=(
            _CMAKE,
            "--build",
            _BUILD_DIRECTORY,
            "--target",
            "minetestserver",
            "--parallel",
            "1",
        ),
        test_argv=_TEST_ARGV,
        test_selector=_TEST_SELECTOR,
    )


def _infrastructure_blocker(reason: str) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {
            "profile_id": LUANTI_5134_PROFILE_ID,
            "reason": reason,
            "dependency_root": _DEPENDENCY_PATH.as_posix(),
            "required_version": _DEPENDENCY_VERSION,
        },
    )


def _read_raw_manifest(root: Path, relative_path: str) -> dict[str, object]:
    path = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Luanti 5134 raw source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("Luanti 5134 raw source manifest is invalid")
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("raw source manifest has duplicate keys")
        result[key] = value
    return result


def _captured_merged_tip(manifest: dict[str, object], candidate: CandidateKey) -> str:
    pull = manifest.get("pull")
    if (
        manifest.get("candidate") != candidate.to_value()
        or manifest.get("complete") is not True
        or type(pull) is not dict
        or pull.get("number") != candidate.change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("Luanti 5134 raw pull binding is invalid")
    value = pull.get(_MERGED_TIP_FIELD)
    if type(value) is not str or not value:
        raise RuntimeError("Luanti 5134 raw pull has no captured merged tip")
    return value


def _registered_directory(root: Path, relative: PurePosixPath) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
    return current


def _registered_file(root: Path, relative: PurePosixPath) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    if not stat.S_ISREG(current.lstat().st_mode):
        return None
    return current


def _registered_dependency(root: Path) -> Path | None:
    dependency = _registered_directory(root, _DEPENDENCY_PATH)
    if dependency is None:
        return None
    header = _registered_file(dependency, _DEPENDENCY_HEADER)
    version_header = _registered_file(dependency, _DEPENDENCY_VERSION_HEADER)
    library = _registered_file(dependency, _DEPENDENCY_LIBRARY)
    if header is None or version_header is None or library is None:
        return None
    match = _IRRLICHT_VERSION.search(version_header.read_bytes())
    if (
        match is None
        or match.group(1).decode("ascii", errors="replace") != _DEPENDENCY_VERSION
    ):
        return None
    return dependency


def _mirror_contains_commit(mirror: Path, merged_tip: str) -> bool:
    try:
        completed = subprocess.run(
            (
                "git",
                f"--git-dir={mirror}",
                "cat-file",
                "-e",
                f"{merged_tip}^{{commit}}",
            ),
            check=False,
            capture_output=True,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _production_only_patch(content: bytes) -> bytes:
    if not content:
        raise RuntimeError("Luanti 5134 raw patch is empty")
    text = content.decode("utf-8", errors="surrogateescape")
    sections: list[tuple[str, list[str]]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, current))
            parts = line.rstrip("\r\n").split(" ")
            if (
                len(parts) != 4
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("Luanti 5134 raw patch header is invalid")
            current_path = parts[3][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, current))
    paths = tuple(path for path, _chunk in sections)
    expected = _PRODUCTION_PATHS + _TEST_PATHS
    if paths != expected or len(set(paths)) != len(paths):
        raise RuntimeError("Luanti 5134 raw patch is outside the registered profile")
    return "".join(
        line for path, chunk in sections if path in _PRODUCTION_PATHS for line in chunk
    ).encode("utf-8", errors="surrogateescape")


__all__ = [
    "LUANTI_5134_PROFILE_ID",
    "Luanti5134MaterializationPlan",
    "plan_luanti_5134_historical_materialization",
]
