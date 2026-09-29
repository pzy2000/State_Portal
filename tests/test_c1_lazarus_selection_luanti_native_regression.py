from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_native_regression import (
    NATIVE_TOOLCHAIN_DEPENDENCY_ID,
    NATIVE_TOOLCHAIN_ROOT,
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/luanti-native-regression-test/v1",
        scope="screening",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _patch_for(candidate: CandidateKey) -> bytes:
    profile = profile_for_candidate(candidate)
    sections: list[str] = []
    for path in profile.production_paths:
        sections.append(
            f"""\
diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-old production
+fixed production
"""
        )
    for path in profile.support_paths:
        sections.append(
            f"""\
diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-old support
+fixed support
"""
        )
    test_lines = "\n".join(f"+{marker}" for marker in profile.patch_test_markers)
    for path in profile.test_paths:
        sections.append(
            f"""\
diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,3 @@
+registered test
{test_lines}
"""
        )
    return "".join(sections).encode()


def _write_stable_chain(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    patch_path = (
        f"raw/github/luanti/{candidate.change_number}/pull-diff/attempt-1/response.body"
    )
    manifest_path = (
        f"raw/github/luanti/{candidate.change_number}/source-manifest/attempt-1/"
        "source.json"
    )
    _write(root, patch_path, _patch_for(candidate))
    _write(
        root,
        manifest_path,
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "complete": True,
                "raw_paths": [patch_path],
                "patch_raw_path": patch_path,
            }
        ),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version=profile.source_packet_reference.semantic_version,
        created_at="2026-08-13T12:00:01Z",
        inputs=(
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                "2.0",
            ),
        ),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "raw_evidence_path": manifest_path,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.patch_reference.artifact_id,
        artifact_kind=profile.patch_reference.artifact_kind,
        semantic_version=profile.patch_reference.semantic_version,
        created_at="2026-08-13T12:00:02Z",
        inputs=(
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                "2.0",
            ),
            profile.source_packet_reference,
        ),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.test_paths),
            "source_manifest_path": manifest_path,
            "raw_patch_path": patch_path,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.test_manifest_reference.artifact_id,
        artifact_kind=profile.test_manifest_reference.artifact_kind,
        semantic_version=profile.test_manifest_reference.semantic_version,
        created_at="2026-08-13T12:00:03Z",
        inputs=(profile.patch_reference,),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": list(profile.test_paths),
                "execution_candidate_kind": "added_assertion",
                "test_count": len(profile.test_paths),
            },
            "complete": True,
        },
        authorization=_authorization(),
    )


def _write_fixed_capsule(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    for path in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
        *profile.p2p_test_paths,
    ):
        markers = (
            profile.fixed_test_markers
            if path in profile.test_paths + profile.p2p_test_paths
            else ()
        )
        _write(
            root,
            f"{profile.fixed_source_tree_path}/{path}",
            "fixed source\n" + "\n".join(markers),
        )
    write_stable_artifact(
        root,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version=profile.fixed_capsule_reference.semantic_version,
        created_at="2026-08-13T12:00:04Z",
        inputs=(
            profile.source_packet_reference,
            profile.patch_reference,
            profile.test_manifest_reference,
        ),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "source_tree_path": profile.fixed_source_tree_path,
            "source_manifest_path": (
                f"raw/github/luanti/{candidate.change_number}/"
                "source-manifest/attempt-1/source.json"
            ),
            "test_paths": list(profile.test_paths),
            "p2p_test_paths": list(profile.p2p_test_paths),
            "support_paths": list(profile.support_paths),
        },
        authorization=_authorization(),
    )


def _write_toolchain(root: Path) -> None:
    toolchain = root / NATIVE_TOOLCHAIN_ROOT
    entries = (
        "bin/git",
        "bin/cmake",
        "bin/ninja",
        "bin/cc",
        "bin/c++",
        "bin/ar",
        "bin/ranlib",
        "bin/ld",
        "bin/nm",
        "bin/strip",
        "bin/otool",
        "bin/install_name_tool",
        "packages/sqlite3/include/sqlite3.h",
        "packages/sqlite3/lib/libsqlite3.a",
        "packages/zlib/include/zlib.h",
        "packages/zlib/include/zconf.h",
        "packages/zlib/lib/libz.a",
        "packages/zstd/include/zstd.h",
        "packages/zstd/include/zstd_errors.h",
        "packages/zstd/lib/libzstd.a",
        "packages/iconv/include/iconv.h",
        "packages/iconv/lib/libiconv.tbd",
    )
    for relative in entries:
        path = _write(toolchain, relative, "registered\n")
        if relative.startswith("bin/"):
            path.chmod(0o700)
    _write(
        toolchain,
        "runtime.json",
        json.dumps(
            {
                "dependency_id": NATIVE_TOOLCHAIN_DEPENDENCY_ID,
                "semantic_version": "3.0",
                "git": "bin/git",
                "cmake": "bin/cmake",
                "ninja": "bin/ninja",
                "c_compiler": "bin/cc",
                "cxx_compiler": "bin/c++",
                "archiver": "bin/ar",
                "ranlib": "bin/ranlib",
                "linker": "bin/ld",
                "symbol_tool": "bin/nm",
                "strip_tool": "bin/strip",
                "object_inspector": "bin/otool",
                "install_name_tool": "bin/install_name_tool",
                "package_roots": {
                    "sqlite3": "packages/sqlite3",
                    "zlib": "packages/zlib",
                    "zstd": "packages/zstd",
                    "iconv": "packages/iconv",
                },
                "closure_files": list(entries),
            }
        ),
    )


@pytest.mark.parametrize("change_number", [15519, 15495])
def test_native_profiles_are_closed_to_the_two_registered_candidates(
    change_number: int,
) -> None:
    profile = profile_for_candidate(CandidateKey("luanti", change_number))

    assert profile.candidate == CandidateKey("luanti", change_number)
    assert profile.repetitions == 3
    assert profile.fixed_capsule_reference.semantic_version == "2.0"
    assert profile.f2p_selector in {"test translations", "TestMapgen"}
    assert profile.p2p_selector == "TestSettings"
    assert profile.p2p_test_paths == ("src/unittest/test_settings.cpp",)

    with pytest.raises(ValueError, match="no registered native regression profile"):
        profile_for_candidate(CandidateKey("openra", change_number))


def test_missing_registered_toolchain_never_falls_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_stable_chain(tmp_path, CandidateKey("luanti", 15519))
    fallback = _write(tmp_path, "fallback/bin/cmake", "#!/bin/sh\n")
    fallback.chmod(0o700)
    monkeypatch.setenv("PATH", str(fallback.parent))

    decision = plan_luanti_native_regression_execution(
        tmp_path,
        candidate=CandidateKey("luanti", 15519),
    )

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-15519-gettext-plural-trim-v2",
        "reason": "registered_native_toolchain_unavailable",
        "dependency_id": NATIVE_TOOLCHAIN_DEPENDENCY_ID,
        "semantic_version": "3.0",
        "runtime_root": NATIVE_TOOLCHAIN_ROOT,
    }


@pytest.mark.parametrize("change_number", [15519, 15495])
def test_ready_plan_replays_stable_chain_and_only_reverses_production(
    tmp_path: Path,
    change_number: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = CandidateKey("luanti", change_number)
    profile = profile_for_candidate(candidate)
    _write_toolchain(tmp_path)
    _write_stable_chain(tmp_path, candidate)
    _write_fixed_capsule(tmp_path, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_native_regression._host_platform",
        lambda: ("linux", "x86_64"),
    )

    plan = plan_luanti_native_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(plan, NativeRegressionExecutionPlan)
    assert plan.profile == profile
    assert plan.production_inverse.paths == profile.production_paths
    assert plan.production_inverse.buggy_apply_argv[0].is_absolute()
    assert all(
        path.encode() in plan.production_inverse.patch
        for path in profile.production_paths
    )
    assert all(
        path.encode() not in plan.production_inverse.patch
        for path in profile.test_paths
    )
    assert all(
        path.encode() not in plan.production_inverse.patch
        for path in profile.support_paths
    )
    assert all(
        path.encode() not in plan.production_inverse.patch
        for path in profile.p2p_test_paths
    )
    assert tuple(run.phase for run in plan.runs) == ("f2p", "p2p")
    assert tuple(run.selector for run in plan.runs) == (
        profile.f2p_selector,
        profile.p2p_selector,
    )
    assert all(run.repetitions == 3 for run in plan.runs)
    assert all(run.executable_path == "bin/luantiserver" for run in plan.runs)
    assert all(run.argv[0] == "bin/luantiserver" for run in plan.runs)
    assert all(not Path(run.executable_path).is_absolute() for run in plan.runs)
    assert plan.configure_argv[0].is_absolute()
    assert f"-DCMAKE_AR={plan.toolchain.archiver}" in plan.configure_argv
    assert f"-DCMAKE_RANLIB={plan.toolchain.ranlib}" in plan.configure_argv
    assert f"-DCMAKE_LINKER={plan.toolchain.linker}" in plan.configure_argv
    assert f"-DCMAKE_NM={plan.toolchain.symbol_tool}" in plan.configure_argv
    assert f"-DCMAKE_STRIP={plan.toolchain.strip_tool}" in plan.configure_argv
    assert f"-DCMAKE_OBJDUMP={plan.toolchain.object_inspector}" in plan.configure_argv
    assert (
        f"-DCMAKE_INSTALL_NAME_TOOL={plan.toolchain.install_name_tool}"
        in plan.configure_argv
    )
    assert "-DENABLE_CURSES=FALSE" in plan.configure_argv
    assert "-DENABLE_OPENSSL=FALSE" in plan.configure_argv
    assert "-DBUILD_DOCUMENTATION=FALSE" in plan.configure_argv
    configure_values = tuple(str(value) for value in plan.configure_argv)
    assert not any(value.startswith("-DSQLite3_") for value in configure_values)
    assert any(value.startswith("-DSQLITE3_INCLUDE_DIR=") for value in configure_values)
    assert any(value.startswith("-DSQLITE3_LIBRARY=") for value in configure_values)
    assert plan.build_argv[0].is_absolute()
    assert plan.closed_environment == {
        "HOME": str(tmp_path / "private/runtime-home/luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(tmp_path / NATIVE_TOOLCHAIN_ROOT / "bin"),
    }


def test_15495_known_darwin_arm64_flake_is_a_typed_platform_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = CandidateKey("luanti", 15495)
    _write_stable_chain(tmp_path, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_native_regression._host_platform",
        lambda: ("darwin", "arm64"),
    )

    decision = plan_luanti_native_regression_execution(
        tmp_path,
        candidate=candidate,
    )

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-15495-biome-boundary-v2",
        "reason": "registered_native_platform_is_flaky",
        "platform_system": "darwin",
        "platform_machine": "arm64",
        "diagnostic_issue_number": 15598,
    }


def test_platform_blocker_does_not_mask_missing_persisted_source_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_native_regression._host_platform",
        lambda: ("darwin", "arm64"),
    )

    decision = plan_luanti_native_regression_execution(
        tmp_path,
        candidate=CandidateKey("luanti", 15495),
    )

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-15495-biome-boundary-v2",
        "reason": "persisted_source_packet_unavailable",
        "source_packet_id": "candidate_source_packet/luanti/15495/source/v2",
    }


def test_missing_fixed_capsule_is_a_typed_infrastructure_blocker(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 15519)
    _write_toolchain(tmp_path)
    _write_stable_chain(tmp_path, candidate)

    decision = plan_luanti_native_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details["reason"] == "fixed_capsule_unavailable"
    assert decision.details["fixed_capsule_id"] == (
        "fixed_source_capsule/luanti/15519/fixed/v2"
    )
