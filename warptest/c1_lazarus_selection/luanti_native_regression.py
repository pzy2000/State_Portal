"""Closed, preparation-only profiles for selected Luanti native regressions.

The public planner performs no build and runs no candidate.  It replays the
stable v2 source/patch/test chain, binds a same-root fixed source capsule,
derives the production-only inverse, and accepts only a registered native
toolchain closure.  Missing prerequisites are typed infrastructure blockers;
PATH and the network are never fallback authorities.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from .models import CandidateKey
from .pipeline import StageDecision
from .screening import RawPatchEvidence, resolve_raw_patch_evidence
from .stable_protocol import ArtifactReference, StableArtifact, load_stable_artifact


NATIVE_TOOLCHAIN_DEPENDENCY_ID = "luanti-native-toolchain"
NATIVE_TOOLCHAIN_ROOT = "private/dependencies/luanti-native/3.0"
_TOOLCHAIN_VERSION = "3.0"
_TOOLCHAIN_MANIFEST = "runtime.json"
_TOOL_FIELDS = {
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
}
_PACKAGE_PATHS = {
    "iconv": "packages/iconv",
    "sqlite3": "packages/sqlite3",
    "zlib": "packages/zlib",
    "zstd": "packages/zstd",
}
_SOURCE_PACKET_INPUT = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)


def _require_safe_relative(value: object, label: str) -> PurePosixPath:
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
class NativeRegressionProfile:
    """One exact source, inverse, and F2P/P2P shape."""

    candidate: CandidateKey
    profile_id: str
    source_packet_reference: ArtifactReference
    patch_reference: ArtifactReference
    test_manifest_reference: ArtifactReference
    fixed_capsule_reference: ArtifactReference
    fixed_source_tree_path: str
    production_paths: tuple[str, ...]
    support_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    p2p_test_paths: tuple[str, ...]
    allowed_patch_paths: tuple[str, ...]
    patch_test_markers: tuple[str, ...]
    fixed_test_markers: tuple[str, ...]
    f2p_selector: str
    p2p_selector: str
    repetitions: int
    platform_policy: Literal["portable", "exclude_darwin_arm64"]

    def __post_init__(self) -> None:
        number = self.candidate.change_number
        if (
            self.candidate.repository_key != "luanti"
            or self.repetitions != 3
            or not self.profile_id
            or self.source_packet_reference
            != ArtifactReference(
                f"candidate_source_packet/luanti/{number}/source/v2",
                "candidate_source_packet",
                "2.0",
            )
            or self.patch_reference
            != ArtifactReference(
                f"candidate_test_patch/luanti/{number}/screening/v2",
                "candidate_test_patch",
                "2.0",
            )
            or self.test_manifest_reference
            != ArtifactReference(
                f"candidate_stage/luanti/{number}/test_manifest/v2",
                "candidate_stage",
                "2.0",
            )
            or self.fixed_capsule_reference
            != ArtifactReference(
                f"fixed_source_capsule/luanti/{number}/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            )
        ):
            raise ValueError("native regression profile identity is invalid")
        _require_safe_relative(self.fixed_source_tree_path, "fixed source tree path")

        for path in self.allowed_patch_paths:
            _require_safe_relative(path, "native profile path")
        for path in self.p2p_test_paths:
            _require_safe_relative(path, "native P2P path")
        if (
            not self.production_paths
            or not self.test_paths
            or len(set(self.allowed_patch_paths)) != len(self.allowed_patch_paths)
            or set(self.production_paths) & set(self.support_paths)
            or set(self.production_paths) & set(self.test_paths)
            or set(self.support_paths) & set(self.test_paths)
            or (
                self.candidate != CandidateKey("luanti", 14072)
                and set(self.p2p_test_paths) & set(self.allowed_patch_paths)
            )
            or set(self.production_paths)
            | set(self.support_paths)
            | set(self.test_paths)
            != set(self.allowed_patch_paths)
            or not self.p2p_test_paths
            or not self.patch_test_markers
            or not self.fixed_test_markers
            or not self.f2p_selector
            or not self.p2p_selector
        ):
            raise ValueError("native regression profile closure is invalid")


def _profile(
    number: int,
    *,
    profile_id: str,
    production_paths: tuple[str, ...],
    support_paths: tuple[str, ...],
    test_paths: tuple[str, ...],
    p2p_test_paths: tuple[str, ...],
    patch_test_markers: tuple[str, ...],
    fixed_test_markers: tuple[str, ...],
    f2p_selector: str,
    platform_policy: Literal["portable", "exclude_darwin_arm64"],
    p2p_selector: str = "TestSettings",
) -> NativeRegressionProfile:
    candidate = CandidateKey("luanti", number)
    return NativeRegressionProfile(
        candidate=candidate,
        profile_id=profile_id,
        source_packet_reference=ArtifactReference(
            f"candidate_source_packet/luanti/{number}/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        patch_reference=ArtifactReference(
            f"candidate_test_patch/luanti/{number}/screening/v2",
            "candidate_test_patch",
            "2.0",
        ),
        test_manifest_reference=ArtifactReference(
            f"candidate_stage/luanti/{number}/test_manifest/v2",
            "candidate_stage",
            "2.0",
        ),
        fixed_capsule_reference=ArtifactReference(
            f"fixed_source_capsule/luanti/{number}/fixed/v2",
            "fixed_source_capsule",
            "2.0",
        ),
        fixed_source_tree_path=f"private/fixed-capsules/luanti/{number}/fixed-source",
        production_paths=production_paths,
        support_paths=support_paths,
        test_paths=test_paths,
        p2p_test_paths=p2p_test_paths,
        allowed_patch_paths=production_paths + support_paths + test_paths,
        patch_test_markers=patch_test_markers,
        fixed_test_markers=fixed_test_markers,
        f2p_selector=f2p_selector,
        p2p_selector=p2p_selector,
        repetitions=3,
        platform_policy=platform_policy,
    )


_PROFILES = {
    13234: _profile(
        13234,
        profile_id="luanti-13234-shapeless-hopcroft-karp-v1-native-gate57",
        production_paths=("src/craftdef.cpp", "src/craftdef.h"),
        # This source epoch predates the test file.  The PR's CMake hunk is
        # test registration support, not production behavior, and must remain
        # present on both the fixed and inverse-production trees.
        support_paths=("src/unittest/CMakeLists.txt",),
        test_paths=("src/unittest/test_craft.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            'const char *getName() { return "TestCraft"; }',
            "void TestCraft::testShapeless",
            "crafttest:g1g2",
            "cdef->initHashes(gamedef);",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestCraft"; }',
            "void TestCraft::testShapeless",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestCraft",
        p2p_selector="TestSettings",
        platform_policy="portable",
    ),
    15687: _profile(
        15687,
        profile_id="luanti-15687-matrix-scale-v2-native-gate57",
        production_paths=("irr/include/matrix4.h", "src/client/content_cao.cpp"),
        support_paths=(),
        test_paths=("src/unittest/test_irr_matrix4.cpp",),
        # The registered P2P selector is TestSettings, so the control path has to
        # be the file that defines it.
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            'SECTION("getScale")',
            "correctly gets the length of each row of the 3x3 submatrix",
            "v3f scale = A.getScale();",
            "getLength()",
            "CHECK(scale.equals(v3f(",
        ),
        fixed_test_markers=(
            'TEST_CASE("matrix4")',
            'SECTION("getScale")',
            "correctly gets the length of each row of the 3x3 submatrix",
            'const char *getName() { return "TestSettings"; }',
        ),
        # The upstream regression is a Catch test case.  Luanti's
        # --test-module option forwards unknown module names to Catch, so the
        # executable selector is the exact TEST_CASE name, not a synthetic
        # TestBase method name.
        f2p_selector="matrix4",
        platform_policy="portable",
    ),
    16200: _profile(
        16200,
        profile_id="luanti-16200-rotation-conventions-v1-native-gate57",
        production_paths=(
            "irr/include/matrix4.h",
            "src/script/lua_api/l_object.cpp",
            "src/util/numeric.h",
        ),
        # The merged PR also updates the public API documentation.  Keep it
        # in the strict patch inventory so the inverse only removes the
        # registered production sections below while leaving all test/docs
        # material identical between variants.
        support_paths=("doc/lua_api.md",),
        test_paths=(
            "builtin/common/tests/vector_spec.lua",
            "src/unittest/test_irr_matrix4.cpp",
            "src/unittest/test_irr_rotation.cpp",
        ),
        p2p_test_paths=("src/unittest/test_k_d_tree.cpp",),
        patch_test_markers=(
            'it("rotation order is Z-X-Y", function()',
            'TEST_CASE("matrix4")',
            'SECTION("rotation order is Y*X*Z (matrix notation)")',
        ),
        fixed_test_markers=(
            'it("rotation order is Z-X-Y", function()',
            'TEST_CASE("matrix4")',
            'SECTION("rotation order is Y*X*Z (matrix notation)")',
            'TEST_CASE("k-d-tree")',
        ),
        f2p_selector="matrix4",
        p2p_selector="k-d-tree",
        platform_policy="portable",
    ),
    14464: _profile(
        14464,
        profile_id="luanti-14464-iconv-state-v2-native-gate57",
        production_paths=(
            "src/content_nodemeta.cpp",
            "src/nodemetadata.cpp",
            "src/object_properties.cpp",
            "src/script/common/c_converter.cpp",
            "src/serialization.cpp",
            "src/util/serialize.h",
            "src/util/string.cpp",
        ),
        support_paths=(),
        test_paths=("src/unittest/test_utilities.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "try to check that the conversion function does not accidentally keep",
            'utf8_to_wide("\\xC4");',
            'UASSERT(utf8_to_wide("\\x81") != L"\\u0101");',
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestUtilities"; }',
            'const char *getName() { return "TestSettings"; }',
            "try to check that the conversion function does not accidentally keep",
        ),
        f2p_selector="TestUtilities",
        platform_policy="portable",
    ),
    11352: _profile(
        11352,
        profile_id="luanti-11352-map-settings-hierarchy-v1",
        production_paths=(
            "src/httpfetch.cpp",
            "src/main.cpp",
            "src/map_settings_manager.cpp",
            "src/map_settings_manager.h",
            "src/settings.cpp",
            "src/settings.h",
        ),
        support_paths=(),
        test_paths=("src/unittest/test_map_settings_manager.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "The settings manager MUST leave user settings alone",
            'mgr.setMapSetting("testname", "1", true);',
            'UASSERT(!Settings::getLayer(SL_GLOBAL)->exists("testname"));',
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestMapSettingsManager"; }',
            'Settings::getLayer(SL_GLOBAL)->exists("testname")',
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestMapSettingsManager",
        platform_policy="portable",
    ),
    10661: _profile(
        10661,
        profile_id="luanti-10661-settings-priority-v1-gate4-api",
        production_paths=(
            "src/client/clientlauncher.cpp",
            "src/client/fontengine.cpp",
            "src/client/fontengine.h",
            "src/content/subgames.cpp",
            "src/database/database-files.cpp",
            "src/database/database-files.h",
            "src/defaultsettings.cpp",
            "src/defaultsettings.h",
            "src/gui/guiKeyChangeMenu.cpp",
            "src/main.cpp",
            "src/map.cpp",
            "src/map_settings_manager.cpp",
            "src/map_settings_manager.h",
            "src/remoteplayer.cpp",
            "src/remoteplayer.h",
            "src/script/lua_api/l_mapgen.cpp",
            "src/script/lua_api/l_settings.cpp",
            "src/script/scripting_mainmenu.cpp",
            "src/server.cpp",
            "src/server.h",
            "src/serverenvironment.cpp",
            "src/settings.cpp",
            "src/settings.h",
        ),
        support_paths=(),
        test_paths=(
            "src/unittest/test_map_settings_manager.cpp",
            "src/unittest/test_settings.cpp",
        ),
        # The two changed test files are already part of test_paths.  Keep a
        # separate, untouched module as the P2P control so the profile can
        # prove that the patch inverse never touches an unrelated test file.
        p2p_test_paths=("src/unittest/test_utilities.cpp",),
        patch_test_markers=(
            "void TestMapSettingsManager::makeUserConfig()",
            "MapSettingsManager mgr(test_mapmeta_path);",
            "void TestSettings::testDefaults()",
            "Settings *game = Settings::createLayer(SL_GAME);",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestMapSettingsManager"; }',
            "MapSettingsManager mgr(test_mapmeta_path);",
            'const char *getName() { return "TestSettings"; }',
            "Settings *game = Settings::createLayer(SL_GAME);",
        ),
        f2p_selector="TestMapSettingsManager",
        platform_policy="portable",
    ),
    15519: _profile(
        15519,
        profile_id="luanti-15519-gettext-plural-trim-v2",
        production_paths=("src/gettext_plural_form.cpp", "src/gettext_plural_form.h"),
        support_paths=(),
        test_paths=("src/unittest/test_translations.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            'SECTION("Plural-Forms function for translations")',
            "n%10>=2 && n%10<=4",
            "CHECK((*form)(122) == 1)",
        ),
        fixed_test_markers=(
            'TEST_CASE("test translations")',
            "n%10>=2 && n%10<=4",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="test translations",
        platform_policy="portable",
    ),
    15495: _profile(
        15495,
        profile_id="luanti-15495-biome-boundary-v2",
        production_paths=(
            "src/mapgen/cavegen.cpp",
            "src/mapgen/mapgen.cpp",
            "src/mapgen/mg_biome.cpp",
            "src/mapgen/mg_biome.h",
            "src/script/lua_api/l_mapgen.cpp",
        ),
        support_paths=("src/unittest/CMakeLists.txt",),
        test_paths=("src/unittest/test_mapgen.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            'const char *getName() { return "TestMapgen"; }',
            "UASSERTEQ(auto, biome->name, expected.name)",
            "UASSERTEQ(auto, next_y, expected.next_y)",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestMapgen"; }',
            "UASSERTEQ(auto, biome->name, expected.name)",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestMapgen",
        platform_policy="exclude_darwin_arm64",
    ),
    11866: _profile(
        11866,
        profile_id="luanti-11866-mapgen-limit-v1-native-gate57",
        production_paths=(
            "builtin/game/chat.lua",
            "src/constants.h",
            "src/defaultsettings.cpp",
            "src/map.cpp",
            "src/mapblock.h",
        ),
        support_paths=(
            "builtin/settingtypes.txt",
            "minetest.conf.example",
            "src/unittest/CMakeLists.txt",
        ),
        # test_player.cpp is a deletion-only section in the PR.  It remains in
        # the typed v2 inventory so the inverse parser accounts for the whole
        # diff, but the fixed source capsule legitimately has no such file.
        test_paths=("src/unittest/test_map.cpp", "src/unittest/test_player.cpp"),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "void TestMap::testMaxMapgenLimit()",
            "objectpos_over_limit",
            "blockpos_over_max_limit",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestMap"; }',
            "void TestMap::testMaxMapgenLimit()",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestMap",
        platform_policy="portable",
    ),
    11770: _profile(
        11770,
        profile_id="luanti-11770-find-nodes-out-of-map-v1-native-gate57",
        production_paths=("src/script/lua_api/l_env.cpp",),
        support_paths=(),
        test_paths=("src/unittest/test_voxelarea.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "void TestVoxelArea::test_extent()",
            "v3s16(32493, -32507, 32753)",
            "UASSERTEQ(s32, v1.getVolume(), 1728980655)",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestVoxelArea"; }',
            "void TestVoxelArea::test_extent()",
            "UASSERTEQ(s32, v1.getVolume(), 1728980655)",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestVoxelArea",
        platform_policy="portable",
    ),
    14072: _profile(
        14072,
        profile_id="luanti-14072-oversized-stack-v2-native-gate57",
        production_paths=(
            "src/inventory.cpp",
            "src/unittest/helpers/helper_moveaction.lua",
        ),
        support_paths=(),
        test_paths=("src/unittest/test_moveaction.cpp",),
        p2p_test_paths=("src/unittest/test_moveaction.cpp",),
        patch_test_markers=(
            "void TestMoveAction::testMoveFillStack",
            'apply_action("Move 209 player:p1 main 0 player:p1 main 1",',
            'list->getItem(0).getItemString() == "default:stone 200"',
            'apply_action("Move 200 player:p1 main 0 player:p1 main 1",',
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestMoveAction"; }',
            "void TestMoveAction::testMoveFillStack",
            "void TestMoveAction::testMovePartial",
        ),
        f2p_selector="TestMoveAction.testMoveFillStack",
        platform_policy="portable",
        p2p_selector="TestMoveAction.testMovePartial",
    ),
    13773: _profile(
        13773,
        profile_id="luanti-13773-keycode-v1-native-gate57",
        production_paths=("src/client/keycode.h",),
        support_paths=(),
        test_paths=("src/unittest/test_keycode.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=("KEY_NUMPAD5",),
        fixed_test_markers=(
            'const char *getName() { return "TestKeycode"; }',
            "KEY_NUMPAD5",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestKeycode",
        platform_policy="portable",
        p2p_selector="TestSettings",
    ),
    8019: _profile(
        8019,
        profile_id="luanti-8019-euler-rotation-v1-native-gate57",
        production_paths=(
            "src/client/content_cao.cpp",
            "src/client/content_cao.h",
            "src/irrlichttypes_extrabloated.h",
            "src/network/networkprotocol.h",
            "src/util/numeric.cpp",
            "src/util/numeric.h",
        ),
        # Keep the documentation hunk in the typed patch inventory while
        # restricting the inverse to production paths only.
        support_paths=("doc/lua_api.txt",),
        test_paths=("src/unittest/test_utilities.cpp",),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "void TestUtilities::testEulerConversion()",
            "setPitchYawRollRad(m1, v1);",
            "getPitchYawRollRad(m1)",
        ),
        fixed_test_markers=(
            'const char *getName() { return "TestUtilities"; }',
            "void TestUtilities::testEulerConversion()",
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="TestUtilities",
        platform_policy="portable",
        p2p_selector="TestSettings",
    ),
    14726: _profile(
        14726,
        profile_id="luanti-14726-gettext-plural-client-v1-native-gate57",
        production_paths=(
            "builtin/common/misc_helpers.lua",
            "builtin/mainmenu/tab_content.lua",
            "games/devtest/mods/testtranslations/init.lua",
            "src/client/client.cpp",
            "src/gettext.h",
            "src/gettext_plural_form.cpp",
            "src/gettext_plural_form.h",
            "src/gui/guiEngine.cpp",
            "src/server.cpp",
            "src/translation.cpp",
            "src/translation.h",
            "src/util/string.cpp",
            "src/util/string.h",
        ),
        support_paths=(
            "doc/lua_api.md",
            "games/devtest/mods/testtranslations/locale/testtranslations.fr.po",
            "games/devtest/mods/testtranslations/locale/translation_mo.fr.mo",
            "games/devtest/mods/testtranslations/locale/translation_po.fr.po",
            "games/devtest/mods/testtranslations/locale/translation_tr.fr.tr",
            "games/devtest/mods/testtranslations/mod.conf",
            "games/devtest/mods/testtranslations/test_locale/readme.txt",
            "games/devtest/mods/testtranslations/test_locale/translation_mo.de.mo",
            "games/devtest/mods/testtranslations/test_locale/translation_po.de.po",
            "games/devtest/mods/testtranslations/translation_mo.de.po",
            "games/devtest/mods/testtranslations/translation_mo.fr.po",
            "src/CMakeLists.txt",
            "src/unittest/CMakeLists.txt",
        ),
        test_paths=(
            "src/unittest/test_servermodmanager.cpp",
            "src/unittest/test_translations.cpp",
        ),
        p2p_test_paths=("src/unittest/test_settings.cpp",),
        patch_test_markers=(
            "UASSERTEQ(std::size_t, mods.size(), 34 + 1);",
            'TEST_CASE("test translations")',
            'SECTION("Plural-Forms function for translations")',
        ),
        fixed_test_markers=(
            'UASSERTEQ(std::size_t, mods.size(), 34 + 1);',
            'TEST_CASE("test translations")',
            'const char *getName() { return "TestSettings"; }',
        ),
        f2p_selector="test translations",
        p2p_selector="TestSettings",
        platform_policy="portable",
    ),
}


@dataclass(frozen=True)
class NativeToolchain:
    root: Path
    git: Path
    cmake: Path
    ninja: Path
    c_compiler: Path
    cxx_compiler: Path
    archiver: Path
    ranlib: Path
    linker: Path
    symbol_tool: Path
    strip_tool: Path
    object_inspector: Path
    install_name_tool: Path
    package_roots: Mapping[str, Path]

    def __post_init__(self) -> None:
        if not all(
            path.is_absolute()
            for path in (
                self.root,
                self.git,
                self.cmake,
                self.ninja,
                self.c_compiler,
                self.cxx_compiler,
                self.archiver,
                self.ranlib,
                self.linker,
                self.symbol_tool,
                self.strip_tool,
                self.object_inspector,
                self.install_name_tool,
                *self.package_roots.values(),
            )
        ):
            raise ValueError("native toolchain is invalid")


@dataclass(frozen=True)
class FixedCapsuleBinding:
    reference: ArtifactReference
    source_tree: Path
    test_paths: tuple[str, ...]
    p2p_test_paths: tuple[str, ...]
    support_paths: tuple[str, ...]


@dataclass(frozen=True)
class ProductionInversePlan:
    paths: tuple[str, ...]
    patch: bytes
    git_executable: Path
    buggy_apply_argv: tuple[Path | str, ...]

    def __post_init__(self) -> None:
        if (
            not self.paths
            or not self.patch
            or not self.git_executable.is_absolute()
            or self.buggy_apply_argv
            != (
                self.git_executable,
                "apply",
                "-R",
                "--whitespace=nowarn",
                "-",
            )
        ):
            raise ValueError("native production inverse is invalid")


@dataclass(frozen=True)
class NativeRegressionRun:
    phase: Literal["f2p", "p2p"]
    selector: str
    executable_path: str
    argv: tuple[str, ...]
    repetitions: int
    fixed_expected_outcome: Literal["pass"]
    buggy_expected_outcome: Literal["failure", "pass"]

    def __post_init__(self) -> None:
        _require_safe_relative(self.executable_path, "native executable path")
        if (
            self.phase not in {"f2p", "p2p"}
            or not self.selector
            or self.repetitions != 3
            or self.argv
            != (
                self.executable_path,
                "--run-unittests",
                "--test-module",
                self.selector,
            )
        ):
            raise ValueError("native regression run is invalid")


@dataclass(frozen=True)
class NativeRegressionExecutionPlan:
    profile: NativeRegressionProfile
    toolchain: NativeToolchain
    fixed_capsule: FixedCapsuleBinding
    source_manifest_path: str
    raw_patch_path: str
    production_inverse: ProductionInversePlan
    configure_argv: tuple[Path | str, ...]
    build_argv: tuple[Path | str, ...]
    runs: tuple[NativeRegressionRun, NativeRegressionRun]
    closed_environment: Mapping[str, str]
    # Stable namespace for build/attempt/aggregate receipts.  The default
    # preserves the original v3 executor IDs; candidate-specific dependency
    # retries can opt into a new immutable namespace without loading a prior
    # configure/build/attempt receipt.
    execution_namespace: str = "v3"
    dependency_references: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.fixed_capsule.reference != self.profile.fixed_capsule_reference
            or self.production_inverse.paths != self.profile.production_paths
            or tuple(run.phase for run in self.runs) != ("f2p", "p2p")
            or self.configure_argv[0] != self.toolchain.cmake
            or self.build_argv[0] != self.toolchain.cmake
            or dict(self.closed_environment)
            != {
                "HOME": str(
                    self.toolchain.root.parent.parent.parent
                    / "runtime-home"
                    / "luanti-native"
                ),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": str(self.toolchain.root / "bin"),
            }
            or type(self.execution_namespace) is not str
            or not self.execution_namespace
            or "/" in self.execution_namespace
            or "\\" in self.execution_namespace
            or type(self.dependency_references) is not tuple
            or any(
                type(reference) is not ArtifactReference
                for reference in self.dependency_references
            )
        ):
            raise ValueError("native execution plan is invalid")


def profile_for_candidate(candidate: CandidateKey) -> NativeRegressionProfile:
    """Resolve only the exact two registered native candidates."""

    if type(candidate) is not CandidateKey:
        raise ValueError("native regression candidate is invalid")
    try:
        profile = _PROFILES[candidate.change_number]
    except KeyError as error:
        raise ValueError(
            "candidate has no registered native regression profile"
        ) from error
    if candidate != profile.candidate:
        raise ValueError("candidate has no registered native regression profile")
    return profile


def plan_luanti_native_regression_execution(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> NativeRegressionExecutionPlan | StageDecision:
    """Prepare a closed native plan without building or starting Luanti."""

    profile = profile_for_candidate(candidate)
    root = _safe_artifact_root(artifact_root)
    source = _source_packet_or_block(root, profile)
    if isinstance(source, StageDecision):
        return source
    patch = _patch_or_block(root, profile, source)
    if isinstance(patch, StageDecision):
        return patch
    test_manifest = _test_manifest_or_block(root, profile, patch)
    if isinstance(test_manifest, StageDecision):
        return test_manifest
    platform_blocker = _platform_blocker(profile)
    if platform_blocker is not None:
        return platform_blocker
    toolchain = _toolchain_or_block(root, profile)
    if isinstance(toolchain, StageDecision):
        return toolchain
    capsule = _fixed_capsule_or_block(root, profile, source, patch, test_manifest)
    if isinstance(capsule, StageDecision):
        return capsule
    raw = _raw_evidence_or_block(root, profile, source)
    if isinstance(raw, StageDecision):
        return raw
    inverse = _production_inverse_plan(raw, profile, toolchain.git)
    build_directory = f"build-luanti-{candidate.change_number}"
    configure_argv: tuple[Path | str, ...] = (
        toolchain.cmake,
        "-S",
        ".",
        "-B",
        build_directory,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=TRUE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DRUN_IN_PLACE=TRUE",
        "-DENABLE_CURL=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_LEVELDB=FALSE",
        "-DENABLE_REDIS=FALSE",
        "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_LUAJIT=FALSE",
        "-DENABLE_SYSTEM_GMP=FALSE",
        "-DENABLE_SYSTEM_JSONCPP=FALSE",
        f"-DCMAKE_MAKE_PROGRAM={toolchain.ninja}",
        f"-DCMAKE_C_COMPILER={toolchain.c_compiler}",
        f"-DCMAKE_CXX_COMPILER={toolchain.cxx_compiler}",
        f"-DCMAKE_AR={toolchain.archiver}",
        f"-DCMAKE_RANLIB={toolchain.ranlib}",
        f"-DCMAKE_LINKER={toolchain.linker}",
        f"-DCMAKE_NM={toolchain.symbol_tool}",
        f"-DCMAKE_STRIP={toolchain.strip_tool}",
        f"-DCMAKE_OBJDUMP={toolchain.object_inspector}",
        f"-DCMAKE_INSTALL_NAME_TOOL={toolchain.install_name_tool}",
        "-DENABLE_CURSES=FALSE",
        "-DENABLE_OPENSSL=FALSE",
        f"-DSQLITE3_INCLUDE_DIR={toolchain.package_roots['sqlite3'] / 'include'}",
        f"-DSQLITE3_LIBRARY={toolchain.package_roots['sqlite3'] / 'lib/libsqlite3.a'}",
        f"-DICONV_LIBRARY={toolchain.package_roots['iconv'] / 'lib/libiconv.tbd'}",
        f"-DZLIB_INCLUDE_DIR={toolchain.package_roots['zlib'] / 'include'}",
        f"-DZLIB_LIBRARY={toolchain.package_roots['zlib'] / 'lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={toolchain.package_roots['zstd'] / 'include'}",
        f"-DZSTD_LIBRARY={toolchain.package_roots['zstd'] / 'lib/libzstd.a'}",
    )
    # The exact current-v2 #11866 CMake project is named ``minetest`` and
    # therefore emits ``minetestserver``.  Keep the historical adapter name
    # for the older profiles so their existing plan fixtures remain stable.
    server_target = (
        "minetestserver"
        if candidate.change_number in {8019, 10661, 11866, 13234, 14072, 14726}
        else "luantiserver"
    )
    executable = f"bin/{server_target}"

    def run(
        phase: Literal["f2p", "p2p"],
        selector: str,
        buggy: Literal["failure", "pass"],
    ) -> NativeRegressionRun:
        return NativeRegressionRun(
            phase=phase,
            selector=selector,
            executable_path=executable,
            argv=(
                executable,
                "--run-unittests",
                "--test-module",
                selector,
            ),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome=buggy,
        )

    return NativeRegressionExecutionPlan(
        profile=profile,
        toolchain=toolchain,
        fixed_capsule=capsule,
        source_manifest_path=raw.source_manifest_path,
        raw_patch_path=raw.raw_patch_path,
        production_inverse=inverse,
        configure_argv=configure_argv,
        build_argv=(
            toolchain.cmake,
            "--build",
            build_directory,
            "--target",
            server_target,
            "--parallel",
            "1",
        ),
        runs=(
            run("f2p", profile.f2p_selector, "failure"),
            run("p2p", profile.p2p_selector, "pass"),
        ),
        closed_environment={
            "HOME": str(root / "private" / "runtime-home" / "luanti-native"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(toolchain.root / "bin"),
        },
    )


def _blocker(
    profile: NativeRegressionProfile,
    reason: str,
    details: Mapping[str, object] | None = None,
) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {"profile_id": profile.profile_id, "reason": reason, **(details or {})},
    )


def _host_platform() -> tuple[str, str]:
    return sys.platform, platform.machine().casefold()


def _platform_blocker(profile: NativeRegressionProfile) -> StageDecision | None:
    system, machine = _host_platform()
    if (
        profile.platform_policy == "exclude_darwin_arm64"
        and system == "darwin"
        and machine in {"arm64", "aarch64"}
    ):
        return _blocker(
            profile,
            "registered_native_platform_is_flaky",
            {
                "platform_system": system,
                "platform_machine": machine,
                "diagnostic_issue_number": 15598,
            },
        )
    return None


def _toolchain_or_block(
    root: Path, profile: NativeRegressionProfile
) -> NativeToolchain | StageDecision:
    runtime_root, failure = _registered_directory(root, NATIVE_TOOLCHAIN_ROOT)
    base_details = {
        "dependency_id": NATIVE_TOOLCHAIN_DEPENDENCY_ID,
        "semantic_version": _TOOLCHAIN_VERSION,
        "runtime_root": NATIVE_TOOLCHAIN_ROOT,
    }
    if failure == "missing":
        return _blocker(
            profile, "registered_native_toolchain_unavailable", base_details
        )
    if failure is not None:
        return _blocker(profile, "registered_native_toolchain_unsafe", base_details)
    assert runtime_root is not None
    manifest_path, failure = _safe_regular_file(runtime_root, _TOOLCHAIN_MANIFEST)
    if failure == "missing":
        return _blocker(
            profile,
            "registered_native_toolchain_incomplete",
            {**base_details, "missing_entries": [_TOOLCHAIN_MANIFEST]},
        )
    if failure is not None:
        return _blocker(profile, "registered_native_toolchain_unsafe", base_details)
    assert manifest_path is not None
    try:
        manifest = _strict_json_object(manifest_path, "native toolchain")
        tools, packages, closure = _parse_toolchain_manifest(manifest)
    except RuntimeError:
        return _blocker(profile, "registered_native_toolchain_invalid", base_details)
    actual, unsafe = _runtime_closure(runtime_root)
    if unsafe is not None:
        return _blocker(
            profile,
            "registered_native_toolchain_unsafe",
            {**base_details, "unsafe_entry": unsafe},
        )
    assert actual is not None
    if actual != set(closure):
        missing = sorted(set(closure) - actual)
        return _blocker(
            profile,
            "registered_native_toolchain_incomplete",
            {**base_details, "missing_entries": missing},
        )
    resolved_tools: dict[str, Path] = {}
    for name, relative in tools.items():
        path, failure = _safe_regular_file(runtime_root, relative)
        if failure is not None or path is None or not os.access(path, os.X_OK):
            return _blocker(
                profile,
                "registered_native_toolchain_incomplete",
                {**base_details, "missing_entries": [relative]},
            )
        resolved_tools[name] = path
    package_roots: dict[str, Path] = {}
    for name, relative in packages.items():
        path, failure = _registered_directory(runtime_root, relative)
        if failure is not None or path is None:
            return _blocker(
                profile,
                "registered_native_toolchain_incomplete",
                {**base_details, "missing_entries": [relative]},
            )
        package_roots[name] = path
    return NativeToolchain(
        root=runtime_root,
        git=resolved_tools["git"],
        cmake=resolved_tools["cmake"],
        ninja=resolved_tools["ninja"],
        c_compiler=resolved_tools["c_compiler"],
        cxx_compiler=resolved_tools["cxx_compiler"],
        archiver=resolved_tools["archiver"],
        ranlib=resolved_tools["ranlib"],
        linker=resolved_tools["linker"],
        symbol_tool=resolved_tools["symbol_tool"],
        strip_tool=resolved_tools["strip_tool"],
        object_inspector=resolved_tools["object_inspector"],
        install_name_tool=resolved_tools["install_name_tool"],
        package_roots=package_roots,
    )


def _parse_toolchain_manifest(
    value: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
    if set(value) != {
        "dependency_id",
        "semantic_version",
        *_TOOL_FIELDS,
        "package_roots",
        "closure_files",
    }:
        raise RuntimeError("native toolchain manifest schema is invalid")
    package_roots = value.get("package_roots")
    closure = value.get("closure_files")
    if (
        value.get("dependency_id") != NATIVE_TOOLCHAIN_DEPENDENCY_ID
        or value.get("semantic_version") != _TOOLCHAIN_VERSION
        or type(package_roots) is not dict
        or package_roots != _PACKAGE_PATHS
        or type(closure) is not list
        or not closure
        or any(type(path) is not str for path in closure)
        or len(closure) != len(set(closure))
    ):
        raise RuntimeError("native toolchain manifest is invalid")
    tools: dict[str, str] = {}
    for name, expected in _TOOL_FIELDS.items():
        if value.get(name) != expected:
            raise RuntimeError("native toolchain path is invalid")
        _require_safe_relative(expected, "native toolchain path")
        tools[name] = expected
    for path in (*package_roots.values(), *closure):
        _require_safe_relative(path, "native toolchain closure path")
    expected_closure = set(_TOOL_FIELDS.values()) | {
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
    }
    if set(closure) != expected_closure:
        raise RuntimeError("native toolchain closure is invalid")
    return tools, dict(package_roots), tuple(closure)


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
                if relative != _TOOLCHAIN_MANIFEST:
                    files.add(relative)
            else:
                return None, relative
    return files, None


def _load_or_block(
    root: Path,
    profile: NativeRegressionProfile,
    reference: ArtifactReference,
    reason: str,
) -> StableArtifact | StageDecision:
    try:
        return load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            key = {
                "persisted_source_packet_unavailable": "source_packet_id",
                "persisted_patch_unavailable": "patch_id",
                "persisted_test_manifest_unavailable": "test_manifest_id",
            }[reason]
            return _blocker(profile, reason, {key: reference.artifact_id})
        raise


def _source_packet_or_block(
    root: Path, profile: NativeRegressionProfile
) -> StableArtifact | StageDecision:
    packet = _load_or_block(
        root,
        profile,
        profile.source_packet_reference,
        "persisted_source_packet_unavailable",
    )
    if isinstance(packet, StageDecision):
        return packet
    if (
        packet.inputs != (_SOURCE_PACKET_INPUT,)
        or packet.payload.get("candidate") != profile.candidate.to_value()
        or packet.payload.get("complete") is not True
        or type(packet.payload.get("raw_evidence_path")) is not str
    ):
        raise RuntimeError("native source packet is invalid")
    return packet


def _patch_or_block(
    root: Path,
    profile: NativeRegressionProfile,
    source: StableArtifact,
) -> StableArtifact | StageDecision:
    patch = _load_or_block(
        root,
        profile,
        profile.patch_reference,
        "persisted_patch_unavailable",
    )
    if isinstance(patch, StageDecision):
        return patch
    expected_inputs = (_SOURCE_PACKET_INPUT, profile.source_packet_reference)
    if (
        patch.inputs != expected_inputs
        or source.reference != profile.source_packet_reference
        or patch.payload.get("candidate") != profile.candidate.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(profile.production_paths)
        or patch.payload.get("test_paths") != list(profile.test_paths)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("native test patch is invalid")
    return patch


def _test_manifest_or_block(
    root: Path,
    profile: NativeRegressionProfile,
    patch: StableArtifact,
) -> StableArtifact | StageDecision:
    manifest = _load_or_block(
        root,
        profile,
        profile.test_manifest_reference,
        "persisted_test_manifest_unavailable",
    )
    if isinstance(manifest, StageDecision):
        return manifest
    details = manifest.payload.get("details")
    valid_inputs = {
        (profile.patch_reference,),
        (
            profile.patch_reference,
            ArtifactReference(
                f"source_atomicity/luanti/{profile.candidate.change_number}/preannotation/v2",
                "source_atomicity",
                "2.0",
            ),
        ),
    }
    if (
        manifest.inputs not in valid_inputs
        or patch.reference != profile.patch_reference
        or manifest.payload.get("candidate") != profile.candidate.to_value()
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("outcome") is not None
        or manifest.payload.get("complete") is not True
        or type(details) is not dict
        or details.get("executable_test_paths") != list(profile.test_paths)
        or details.get("test_count") != len(profile.test_paths)
        or details.get("execution_candidate_kind") != "added_assertion"
    ):
        raise RuntimeError("native test manifest is invalid")
    return manifest


def _fixed_capsule_or_block(
    root: Path,
    profile: NativeRegressionProfile,
    source: StableArtifact,
    patch: StableArtifact,
    test_manifest: StableArtifact,
) -> FixedCapsuleBinding | StageDecision:
    try:
        artifact = load_stable_artifact(
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
    expected_payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(profile.test_paths),
        "p2p_test_paths": list(profile.p2p_test_paths),
        "support_paths": list(profile.support_paths),
    }
    if (
        artifact.inputs != (source.reference, patch.reference, test_manifest.reference)
        or artifact.payload != expected_payload
    ):
        raise RuntimeError("native fixed capsule binding is invalid")
    source_tree, failure = _registered_directory(root, profile.fixed_source_tree_path)
    if failure == "missing":
        return _blocker(
            profile,
            "fixed_capsule_source_unavailable",
            {"source_tree_path": profile.fixed_source_tree_path},
        )
    if failure is not None:
        raise RuntimeError("native fixed capsule source tree is unsafe")
    assert source_tree is not None
    for relative in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
        *profile.p2p_test_paths,
    ):
        path, failure = _safe_regular_file(source_tree, relative)
        if failure == "missing":
            if _is_deleted_test_path(profile, relative):
                continue
            return _blocker(
                profile,
                "fixed_capsule_source_unavailable",
                {
                    "source_tree_path": profile.fixed_source_tree_path,
                    "missing_path": relative,
                },
            )
        if failure is not None:
            raise RuntimeError("native fixed capsule source path is unsafe")
        assert path is not None
    combined_tests = "\n".join(
        (source_tree / path).read_text(encoding="utf-8", errors="surrogateescape")
        for path in profile.test_paths + profile.p2p_test_paths
        if (source_tree / path).is_file()
    )
    if not all(marker in combined_tests for marker in profile.fixed_test_markers):
        raise RuntimeError("native fixed capsule test binding is invalid")
    return FixedCapsuleBinding(
        reference=artifact.reference,
        source_tree=source_tree,
        test_paths=profile.test_paths,
        p2p_test_paths=profile.p2p_test_paths,
        support_paths=profile.support_paths,
    )


def _is_deleted_test_path(profile: NativeRegressionProfile, relative: str) -> bool:
    """Allow only the known deletion-only test section in Luanti #11866."""

    return profile.candidate == CandidateKey("luanti", 11866) and relative == (
        "src/unittest/test_player.cpp"
    )


def _raw_evidence_or_block(
    root: Path,
    profile: NativeRegressionProfile,
    source: StableArtifact,
) -> RawPatchEvidence | StageDecision:
    try:
        return resolve_raw_patch_evidence(
            root,
            candidate=profile.candidate,
            source_manifest_path=source.payload["raw_evidence_path"],
        )
    except RuntimeError as error:
        if str(error) in {
            "source manifest file is missing",
            "source patch file is missing",
        }:
            return _blocker(
                profile,
                "persisted_source_evidence_unavailable",
                {"source_packet_id": source.reference.artifact_id},
            )
        raise


def _production_inverse_plan(
    raw: RawPatchEvidence,
    profile: NativeRegressionProfile,
    git_executable: Path,
) -> ProductionInversePlan:
    sections = _patch_sections(raw.patch_path.read_bytes())
    paths = tuple(section.path for section in sections)
    if len(set(paths)) != len(paths) or set(paths) != set(profile.allowed_patch_paths):
        raise RuntimeError("native raw patch is outside the registered profile")
    by_path = {section.path: section for section in sections}
    tests = "".join("".join(by_path[path].lines) for path in profile.test_paths)
    if not all(marker in tests for marker in profile.patch_test_markers):
        raise RuntimeError("native raw patch does not bind the regression test")
    production = tuple(by_path[path] for path in profile.production_paths)
    return ProductionInversePlan(
        paths=profile.production_paths,
        patch="".join("".join(section.lines) for section in production).encode(
            "utf-8", errors="surrogateescape"
        ),
        git_executable=git_executable,
        buggy_apply_argv=(
            git_executable,
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ),
    )


@dataclass(frozen=True)
class _PatchSection:
    path: str
    lines: tuple[str, ...]


def _patch_sections(content: bytes) -> tuple[_PatchSection, ...]:
    if not content:
        raise RuntimeError("native raw patch is empty")
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
        raise RuntimeError("native raw patch is invalid")
    return tuple(sections)


def _patch_path_from_header(line: str) -> str:
    try:
        parts = shlex.split(line.rstrip("\r\n"))
    except ValueError as error:
        raise RuntimeError("native raw patch header is invalid") from error
    if (
        len(parts) != 4
        or parts[:2] != ["diff", "--git"]
        or not parts[2].startswith("a/")
        or not parts[3].startswith("b/")
        or parts[2][2:] != parts[3][2:]
    ):
        raise RuntimeError("native raw patch header is invalid")
    path = parts[2][2:]
    _require_safe_relative(path, "native raw patch path")
    return path


def _safe_artifact_root(value: str | Path) -> Path:
    root = Path(value).absolute()
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
                RuntimeError(f"{label} contains a non-finite number")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} is invalid")
    return value


__all__ = [
    "NATIVE_TOOLCHAIN_DEPENDENCY_ID",
    "NATIVE_TOOLCHAIN_ROOT",
    "FixedCapsuleBinding",
    "NativeRegressionExecutionPlan",
    "NativeRegressionProfile",
    "NativeRegressionRun",
    "NativeToolchain",
    "ProductionInversePlan",
    "plan_luanti_native_regression_execution",
    "profile_for_candidate",
]
