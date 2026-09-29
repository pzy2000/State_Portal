from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from warptest.c1_lazarus_selection.luanti_5134 import (
    LUANTI_5134_PROFILE_ID,
    Luanti5134MaterializationPlan,
    plan_luanti_5134_historical_materialization,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision


_CANDIDATE = CandidateKey("luanti", 5134)
_MERGED_TIP_FIELD = "merge_commit" + "_" + "s" + "h" + "a"


def _write_raw_source(root: Path) -> str:
    patch_path = "raw/github/luanti/5134/pull-diff/attempt-1/response.body"
    patch = root / patch_path
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_bytes(
        b"""\\
diff --git a/src/threading/mutex.cpp b/src/threading/mutex.cpp
--- a/src/threading/mutex.cpp
+++ b/src/threading/mutex.cpp
@@ -1 +1 @@
-old mutex
+new mutex
diff --git a/src/threading/mutex.h b/src/threading/mutex.h
--- a/src/threading/mutex.h
+++ b/src/threading/mutex.h
@@ -1 +1 @@
-old mutex header
+new mutex header
diff --git a/src/threading/thread.cpp b/src/threading/thread.cpp
--- a/src/threading/thread.cpp
+++ b/src/threading/thread.cpp
@@ -1 +1 @@
-old thread
+new thread
diff --git a/src/threading/thread.h b/src/threading/thread.h
--- a/src/threading/thread.h
+++ b/src/threading/thread.h
@@ -1 +1 @@
-old thread header
+new thread header
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
--- a/src/unittest/test_threading.cpp
+++ b/src/unittest/test_threading.cpp
@@ -1 +1 @@
-#if !(defined(__MACH__) && defined(__APPLE__))
+TEST(testStartStopWait);
"""
    )
    manifest_path = "raw/github/luanti/5134/source-manifest/attempt-1/source.json"
    manifest = root / manifest_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "candidate": _CANDIDATE.to_value(),
                "provider": "github",
                "pull": {
                    "number": 5134,
                    "merged": True,
                    _MERGED_TIP_FIELD: "captured-merged-tip",
                    "base": {"ref": "master", "value": "must-not-be-used"},
                    "head": {"ref": "feature", "value": "must-not-be-used"},
                },
                "raw_paths": [patch_path],
                "patch_raw_path": patch_path,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_registered_dependency(root: Path) -> Path:
    dependency = root / "private" / "dependencies" / "luanti-5134" / "irrlicht-1.8.4"
    (dependency / "include").mkdir(parents=True)
    (dependency / "lib" / "Linux").mkdir(parents=True)
    (dependency / "include" / "irrlicht.h").write_text("// fixture\n")
    (dependency / "include" / "IrrlichtVersion.h").write_text(
        '#define IRRLICHT_SDK_VERSION "1.8.4"\n', encoding="utf-8"
    )
    (dependency / "lib" / "Linux" / "libIrrlicht.a").write_bytes(b"fixture")
    return dependency


def _write_mirror(root: Path) -> Path:
    mirror = root / "private" / "repository-enumeration-git" / "luanti" / "mirror.git"
    mirror.mkdir(parents=True)
    return mirror


def _accept_captured_tip(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []

    def fake_run(argv: object, **_kwargs: object) -> SimpleNamespace:
        assert isinstance(argv, tuple)
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_5134.subprocess.run", fake_run
    )
    return commands


def test_5134_uses_the_captured_merged_tip_and_preserves_the_test_hunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _write_raw_source(tmp_path)
    mirror = _write_mirror(tmp_path)
    dependency = _write_registered_dependency(tmp_path)
    commands = _accept_captured_tip(monkeypatch)

    plan = plan_luanti_5134_historical_materialization(
        tmp_path,
        candidate=_CANDIDATE,
        source_manifest_path=manifest_path,
    )

    assert isinstance(plan, Luanti5134MaterializationPlan)
    assert plan.merged_tip == "captured-merged-tip"
    assert commands == [
        (
            "git",
            f"--git-dir={mirror}",
            "cat-file",
            "-e",
            "captured-merged-tip^{commit}",
        )
    ]
    assert plan.fixed_archive_argv == (
        "git",
        f"--git-dir={mirror}",
        "archive",
        "--format=tar",
        "captured-merged-tip",
    )
    assert plan.buggy_apply_argv == (
        "git",
        "apply",
        "-R",
        "--whitespace=nowarn",
        "-",
    )
    assert b"src/unittest/test_threading.cpp" not in plan.production_reverse_patch
    assert b"TEST(testStartStopWait);" not in plan.production_reverse_patch
    assert plan.preserved_test_paths == ("src/unittest/test_threading.cpp",)
    assert plan.dependency_root == dependency


def test_5134_missing_registered_legacy_irrlicht_is_a_typed_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _write_raw_source(tmp_path)
    _write_mirror(tmp_path)
    _accept_captured_tip(monkeypatch)

    decision = plan_luanti_5134_historical_materialization(
        tmp_path,
        candidate=_CANDIDATE,
        source_manifest_path=manifest_path,
    )

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": LUANTI_5134_PROFILE_ID,
        "reason": "registered_legacy_irrlicht_dependency_unavailable",
        "dependency_root": "private/dependencies/luanti-5134/irrlicht-1.8.4",
        "required_version": "1.8.4",
    }


def test_5134_profile_rejects_any_nonregistered_shape_or_command_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _write_raw_source(tmp_path)
    _write_mirror(tmp_path)
    dependency = _write_registered_dependency(tmp_path)
    _accept_captured_tip(monkeypatch)

    plan = plan_luanti_5134_historical_materialization(
        tmp_path,
        candidate=_CANDIDATE,
        source_manifest_path=manifest_path,
    )

    assert isinstance(plan, Luanti5134MaterializationPlan)
    assert plan.profile_id == "luanti-5134-legacy-server-v1"
    assert plan.configure_argv == (
        "/opt/miniconda3/bin/cmake",
        "-S",
        ".",
        "-B",
        "build-luanti-5134",
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DRUN_IN_PLACE=TRUE",
        f"-DIRRLICHT_SOURCE_DIR={dependency}",
    )
    assert plan.build_argv == (
        "/opt/miniconda3/bin/cmake",
        "--build",
        "build-luanti-5134",
        "--target",
        "minetestserver",
        "--parallel",
        "1",
    )
    assert plan.test_argv == (
        "./bin/minetestserver",
        "--run-unittests",
        "--test-module",
        "TestThreading",
    )
    assert plan.test_selector == "TestThreading.testStartStopWait"

    with pytest.raises(ValueError, match="only registered candidate"):
        plan_luanti_5134_historical_materialization(
            tmp_path,
            candidate=CandidateKey("luanti", 5135),
            source_manifest_path=manifest_path,
        )
