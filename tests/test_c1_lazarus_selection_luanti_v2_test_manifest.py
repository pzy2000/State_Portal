from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.luanti_v2_test_manifest import (
    classify_luanti_v2_test_manifest,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection import operations


_THREADING_PATH = "src/unittest/test_threading.cpp"
_WORKSPACE = Path(__file__).resolve().parents[1]


def _cached_luanti_pull_diff(number: int) -> bytes:
    """Read the immutable local capture instead of approximating its Lua diff."""

    root = (
        _WORKSPACE
        / ".warptest-cache"
        / "c1_lazarus_selection_v1"
        / "raw"
        / "github"
        / "luanti"
        / str(number)
        / "pull-diff"
    )
    captures = tuple(sorted(root.glob("attempt-*/*.response.body")))
    if not captures:
        pytest.skip(f"local Luanti #{number} pull-diff capture is unavailable")
    return captures[-1].read_bytes()


def test_luanti_5134_guard_removal_is_only_an_execution_candidate() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
--- a/src/unittest/test_threading.cpp
+++ b/src/unittest/test_threading.cpp
@@ -39,9 +39,7 @@ void TestThreading::runTests()
-#if !(defined(__MACH__) && defined(__APPLE__))
 \tTEST(testStartStopWait);
-#endif
 \tTEST(testThreadKill);
""",
        declared_test_paths=(_THREADING_PATH,),
    )

    assert decision.state == "execution_candidate"
    assert decision.evidence_kind == "enabled_existing_guarded_test"
    assert decision.executable_test_paths == (_THREADING_PATH,)
    assert not hasattr(decision, "qualified")


def test_luanti_v2_added_assertion_is_an_execution_candidate() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
--- a/src/unittest/test_threading.cpp
+++ b/src/unittest/test_threading.cpp
@@ -1 +1,2 @@
 void TestThreading::testStartStopWait()
+\tUASSERT(thread.wait());
""",
        declared_test_paths=(_THREADING_PATH,),
    )

    assert decision.state == "execution_candidate"
    assert decision.evidence_kind == "added_assertion"
    assert decision.executable_test_paths == (_THREADING_PATH,)


def test_luanti_v2_standard_assert_is_an_execution_candidate() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
--- a/src/unittest/test_threading.cpp
+++ b/src/unittest/test_threading.cpp
@@ -1 +1,2 @@
 void TestThreading::testStartStopWait()
+\tassert(thread.wait());
""",
        declared_test_paths=(_THREADING_PATH,),
    )

    assert decision.state == "execution_candidate"
    assert decision.evidence_kind == "added_assertion"


@pytest.mark.parametrize(
    ("number", "test_path"),
    [
        (8704, "games/minimal/mods/test/init.lua"),
        (9722, "builtin/common/tests/serialize_spec.lua"),
        (13132, "builtin/common/tests/misc_helpers_spec.lua"),
    ],
)
def test_real_cached_lua_regression_assertions_are_execution_candidates(
    number: int,
    test_path: str,
) -> None:
    decision = classify_luanti_v2_test_manifest(
        _cached_luanti_pull_diff(number),
        declared_test_paths=(test_path,),
    )

    assert decision.state == "execution_candidate", f"Luanti #{number} was rejected"
    assert decision.evidence_kind == "added_assertion"
    assert decision.executable_test_paths == (test_path,)
    assert not hasattr(decision, "qualified")


def test_lua_assertion_requires_direct_invocation_or_spec_registration() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/games/minimal/mods/test/init.lua b/games/minimal/mods/test/init.lua
--- a/games/minimal/mods/test/init.lua
+++ b/games/minimal/mods/test/init.lua
@@ -1 +1,4 @@
+local function test_get_craft_result()
+\tassert(output.item)
+end
""",
        declared_test_paths=("games/minimal/mods/test/init.lua",),
    )

    assert decision.state == "rejected"
    assert decision.executable_test_paths == ()


def test_lua_commented_assertion_and_spec_registration_only_are_rejected() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/builtin/common/tests/serialize_spec.lua b/builtin/common/tests/serialize_spec.lua
--- a/builtin/common/tests/serialize_spec.lua
+++ b/builtin/common/tests/serialize_spec.lua
@@ -1 +1,5 @@
+it("documents a future regression", function()
+\t-- assert.same(expected, actual)
+\tlocal value = core.serialize({})
+end)
""",
        declared_test_paths=("builtin/common/tests/serialize_spec.lua",),
    )

    assert decision.state == "rejected"
    assert decision.reason == "test_registration_without_regression"
    assert decision.executable_test_paths == ()


def test_real_cached_mod_conf_registration_only_remains_rejected() -> None:
    decision = classify_luanti_v2_test_manifest(
        _cached_luanti_pull_diff(8704),
        declared_test_paths=("games/minimal/mods/test/mod.conf",),
    )

    assert decision.state == "rejected"
    assert decision.reason == "test_registration_without_regression"
    assert decision.executable_test_paths == ()


def test_lua_deletion_only_is_rejected() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/builtin/common/tests/serialize_spec.lua b/builtin/common/tests/serialize_spec.lua
--- a/builtin/common/tests/serialize_spec.lua
+++ b/builtin/common/tests/serialize_spec.lua
@@ -1,5 +1 @@
-it("handles precise numbers", function()
-\tassert.same(expected, actual)
-end)
""",
        declared_test_paths=("builtin/common/tests/serialize_spec.lua",),
    )

    assert decision.state == "rejected"
    assert decision.reason == "deletion_only"
    assert decision.executable_test_paths == ()


def test_lua_rename_only_is_rejected() -> None:
    decision = classify_luanti_v2_test_manifest(
        b"""\
diff --git a/builtin/common/tests/old_spec.lua b/builtin/common/tests/new_spec.lua
similarity index 100%
rename from builtin/common/tests/old_spec.lua
rename to builtin/common/tests/new_spec.lua
""",
        declared_test_paths=("builtin/common/tests/new_spec.lua",),
    )

    assert decision.state == "rejected"
    assert decision.executable_test_paths == ()


@pytest.mark.parametrize(
    ("number", "raw", "test_path", "expected_reason"),
    [
        (
            5475,
            b"""\
diff --git a/src/unittest/test_player.cpp b/src/unittest/test_player.cpp
--- a/src/unittest/test_player.cpp
+++ b/src/unittest/test_player.cpp
@@ -31,12 +31,4 @@ class TestPlayer : public TestBase
-\tvoid testSave(IGameDef *gamedef);
-\tTEST(testSave, gamedef);
-\tUASSERT(fs::PathExists("testplayer_save"));
""",
            "src/unittest/test_player.cpp",
            "deletion_only",
        ),
        (
            5645,
            b"""\
diff --git a/src/unittest/test_mapnode.cpp b/src/unittest/test_mapnode.cpp
--- a/src/unittest/test_mapnode.cpp
+++ b/src/unittest/test_mapnode.cpp
@@ -23,7 +23,8 @@ with this program
-class TestMapNode : public TestBase {
+class TestMapNode : public TestBase
+{
 public:
-\t//verbosestream << "Test";
+\t// verbosestream << "Test";
""",
            "src/unittest/test_mapnode.cpp",
            "formatting_only",
        ),
        (
            6243,
            b"""\
diff --git a/games/minimal/mods/test/mod.conf b/games/minimal/mods/test/mod.conf
new file mode 100644
--- /dev/null
+++ b/games/minimal/mods/test/mod.conf
@@ -0,0 +1,2 @@
+name = test
+description = Adds unit tests for the engine
""",
            "games/minimal/mods/test/mod.conf",
            "test_registration_without_regression",
        ),
        (
            6346,
            b"""\
diff --git a/src/unittest/test.cpp b/src/unittest/test.cpp
--- a/src/unittest/test.cpp
+++ b/src/unittest/test.cpp
@@ -228,8 +228,6 @@ bool run_tests()
-\tDSTACK(FUNCTION_NAME);
""",
            "src/unittest/test.cpp",
            "deletion_only",
        ),
        (
            6599,
            b"""\
diff --git a/src/unittest/test_map_settings_manager.cpp b/src/unittest/test_map_settings_manager.cpp
--- a/src/unittest/test_map_settings_manager.cpp
+++ b/src/unittest/test_map_settings_manager.cpp
@@ -21,7 +21,7 @@ with this program
-#include "mapgen_v5.h"
+#include "mapgen/mapgen_v5.h"
""",
            "src/unittest/test_map_settings_manager.cpp",
            "include_only",
        ),
        (
            7353,
            b"""\
diff --git a/src/unittest/test.cpp b/src/unittest/test.cpp
--- a/src/unittest/test.cpp
+++ b/src/unittest/test.cpp
@@ -26,6 +26,7 @@ with this program
+#include "porting.h"
@@ -323,7 +324,7 @@ std::string TestBase::getTestTempDirectory()
-\tsnprintf(buf, sizeof(buf), "%08X", myrand());
+\tporting::mt_snprintf(buf, sizeof(buf), "%08X", myrand());
""",
            "src/unittest/test.cpp",
            "new_api_compile_only",
        ),
        (
            7539,
            b"""\
diff --git a/src/unittest/CMakeLists.txt b/src/unittest/CMakeLists.txt
--- a/src/unittest/CMakeLists.txt
+++ b/src/unittest/CMakeLists.txt
@@ -23,7 +23,6 @@ set (UNITTEST_SRCS
-\t${CMAKE_CURRENT_SOURCE_DIR}/test_serveractiveobjectmap.cpp
""",
            "src/unittest/CMakeLists.txt",
            "deletion_only",
        ),
    ],
)
def test_current_luanti_negative_examples_do_not_admit_execution(
    number: int,
    raw: bytes,
    test_path: str,
    expected_reason: str,
) -> None:
    decision = classify_luanti_v2_test_manifest(
        raw,
        declared_test_paths=(test_path,),
    )

    assert decision.state == "rejected", f"Luanti #{number} was admitted"
    assert decision.reason == expected_reason
    assert decision.executable_test_paths == ()


def test_v2_luanti_manifest_uses_the_classifier_without_claiming_qualification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_patch = tmp_path / "5134.diff"
    raw_patch.write_bytes(
        b"""\
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
--- a/src/unittest/test_threading.cpp
+++ b/src/unittest/test_threading.cpp
@@ -39,9 +39,7 @@ void TestThreading::runTests()
-#if !(defined(__MACH__) && defined(__APPLE__))
 \tTEST(testStartStopWait);
-#endif
"""
    )
    candidate = CandidateKey("luanti", 5134)
    artifact = SimpleNamespace(
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "complete": True,
            "test_paths": [_THREADING_PATH],
            "raw_patch_path": raw_patch.name,
        }
    )
    monkeypatch.setattr(operations, "_load_patch_artifact", lambda *_args: artifact)
    monkeypatch.setattr(
        operations,
        "_load_accepted_atomicity_reference",
        lambda *_args, **_kwargs: object(),
    )

    decision = operations.evaluate_test_manifest(
        tmp_path,
        candidate,
        selection_epoch=SELECTION_EPOCH_V2,
    )

    assert decision.state == "passed"
    assert decision.details == {
        "executable_test_paths": [_THREADING_PATH],
        "test_count": 1,
        "execution_candidate_kind": "enabled_existing_guarded_test",
    }


def test_14555_half_boundary_corpus_is_an_execution_candidate() -> None:
    """The added half-grid corpus executes existing assertions; it is not an API probe."""

    raw = b"""\
diff --git a/src/unittest/test_voxelalgorithms.cpp b/src/unittest/test_voxelalgorithms.cpp
--- a/src/unittest/test_voxelalgorithms.cpp
+++ b/src/unittest/test_voxelalgorithms.cpp
@@ -49,6 +49,13 @@ void TestVoxelAlgorithms::testVoxelLineIterator()
 \t\tlines.emplace_back(-x, -y, -z, x, y, z);
 \t}
+\tfor (f32 x = -3.0f; x < 3.1f; x += 0.5f)
+\tfor (f32 y = -3.0f; y < 3.1f; y += 0.5f)
+\tfor (f32 z = -3.0f; z < 3.1f; z += 0.5f) {
+\t\tlines.emplace_back(-x, -y, -z, x, y, z);
+\t}
 \tfor (auto l : lines) {
 \t\tvoxalgo::VoxelLineIterator iterator(l.start, l.getVector());
 \t\twhile (iterator.hasNext()) {
 \t\t\titerator.next();
 \t\t\tUASSERTEQ(f32, (new_voxel - old_voxel).getLengthSQ(), 1);
 \t\t\tUASSERT(box.intersectsWithLine(l));
 \t\t}
 \t\tUASSERT(iterator.m_current_node_pos == end_voxel);
 \t\tUASSERTEQ(int, actual_nodecount, nodecount);
 \t}
diff --git a/src/voxelalgorithms.cpp b/src/voxelalgorithms.cpp
--- a/src/voxelalgorithms.cpp
+++ b/src/voxelalgorithms.cpp
@@ -1246,18 +1246,18 @@ VoxelLineIterator::VoxelLineIterator
-\t\tm_next_intersection_multi.X = (floorf(m_start_position.X - 0.5) + 1.5
+\t\tm_next_intersection_multi.X = (m_current_node_pos.X + 0.5f
 \t\t\t- m_start_position.X) / m_line_vector.X;
-\t\tm_next_intersection_multi.X = (floorf(m_start_position.X - 0.5)
-\t\t\t- m_start_position.X + 0.5) / m_line_vector.X;
+\t\tm_next_intersection_multi.X = (m_current_node_pos.X - 0.5f
+\t\t\t- m_start_position.X) / m_line_vector.X;
-\t\tm_next_intersection_multi.Y = (floorf(m_start_position.Y - 0.5) + 1.5
+\t\tm_next_intersection_multi.Y = (m_current_node_pos.Y + 0.5f
 \t\t\t- m_start_position.Y) / m_line_vector.Y;
-\t\tm_next_intersection_multi.Y = (floorf(m_start_position.Y - 0.5)
-\t\t\t- m_start_position.Y + 0.5) / m_line_vector.Y;
+\t\tm_next_intersection_multi.Y = (m_current_node_pos.Y - 0.5f
+\t\t\t- m_start_position.Y) / m_line_vector.Y;
-\t\tm_next_intersection_multi.Z = (floorf(m_start_position.Z - 0.5) + 1.5
+\t\tm_next_intersection_multi.Z = (m_current_node_pos.Z + 0.5f
 \t\t\t- m_start_position.Z) / m_line_vector.Z;
-\t\tm_next_intersection_multi.Z = (floorf(m_start_position.Z - 0.5)
-\t\t\t- m_start_position.Z + 0.5) / m_line_vector.Z;
+\t\tm_next_intersection_multi.Z = (m_current_node_pos.Z - 0.5f
+\t\t\t- m_start_position.Z) / m_line_vector.Z;
"""

    decision = classify_luanti_v2_test_manifest(
        raw,
        declared_test_paths=("src/unittest/test_voxelalgorithms.cpp",),
        candidate_change_number=14555,
    )

    assert decision.state == "execution_candidate"
    assert decision.reason is None
    assert decision.evidence_kind == "expanded_regression_corpus"
    assert decision.executable_test_paths == ("src/unittest/test_voxelalgorithms.cpp",)


@pytest.mark.parametrize("change_number", [None, 14554, 14556])
def test_14555_half_boundary_profile_is_bound_to_candidate_identity(
    change_number: int | None,
) -> None:
    raw = b"""\
diff --git a/src/unittest/test_voxelalgorithms.cpp b/src/unittest/test_voxelalgorithms.cpp
--- a/src/unittest/test_voxelalgorithms.cpp
+++ b/src/unittest/test_voxelalgorithms.cpp
@@ -49,6 +49,13 @@ void TestVoxelAlgorithms::testVoxelLineIterator()
+\tfor (f32 x = -3.0f; x < 3.1f; x += 0.5f)
+\tfor (f32 y = -3.0f; y < 3.1f; y += 0.5f)
+\tfor (f32 z = -3.0f; z < 3.1f; z += 0.5f) {
+\t\tlines.emplace_back(-x, -y, -z, x, y, z);
+\t}
 \tfor (auto l : lines) {
 \t\tvoxalgo::VoxelLineIterator iterator(l.start, l.getVector());
 \t\twhile (iterator.hasNext()) { iterator.next(); }
 \t\tUASSERT(iterator.m_current_node_pos == end_voxel);
 \t\tUASSERTEQ(int, actual_nodecount, nodecount);
 \t}
diff --git a/src/voxelalgorithms.cpp b/src/voxelalgorithms.cpp
--- a/src/voxelalgorithms.cpp
+++ b/src/voxelalgorithms.cpp
@@ -1246,6 +1246,6 @@ VoxelLineIterator::VoxelLineIterator
- old X floorf(m_start_position.X - 0.5)
+ m_current_node_pos.X + 0.5f m_current_node_pos.X - 0.5f
- old Y floorf(m_start_position.Y - 0.5)
+ m_current_node_pos.Y + 0.5f m_current_node_pos.Y - 0.5f
- old Z floorf(m_start_position.Z - 0.5)
+ m_current_node_pos.Z + 0.5f m_current_node_pos.Z - 0.5f
"""

    decision = classify_luanti_v2_test_manifest(
        raw,
        declared_test_paths=("src/unittest/test_voxelalgorithms.cpp",),
        candidate_change_number=change_number,
    )

    assert decision.state == "rejected"
    assert decision.reason == "new_api_compile_only"


def test_v2_jupyter_manifest_does_not_invoke_the_luanti_classifier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_patch = tmp_path / "jupyter.diff"
    raw_patch.write_bytes(
        b"""\
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1,2 @@
 test('old', () => expect(value).toBe(1));
+test('new', () => expect(value).toBe(2));
"""
    )
    candidate = CandidateKey("jupyterlab", 9001)
    artifact = SimpleNamespace(
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "complete": True,
            "test_paths": ["packages/example/test/index.spec.ts"],
            "raw_patch_path": raw_patch.name,
        }
    )
    monkeypatch.setattr(operations, "_load_patch_artifact", lambda *_args: artifact)
    monkeypatch.setattr(
        operations,
        "_load_accepted_atomicity_reference",
        lambda *_args, **_kwargs: object(),
    )

    def unexpected_luanti_call(*_args: object, **_kwargs: object) -> object:
        pytest.fail("JupyterLab must not use the Luanti v2 classifier")

    monkeypatch.setattr(
        operations,
        "classify_luanti_v2_test_manifest",
        unexpected_luanti_call,
    )

    decision = operations.evaluate_test_manifest(
        tmp_path,
        candidate,
        selection_epoch=SELECTION_EPOCH_V2,
    )

    assert decision.state == "passed"
    assert decision.details == {
        "executable_test_paths": ["packages/example/test/index.spec.ts"],
        "test_count": 1,
    }
