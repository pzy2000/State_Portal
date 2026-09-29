from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_8704 import (
    LUANTI_8704_PROFILE_ID,
    Luanti8704PreparationDescriptor,
    describe_luanti_8704_minimal_game_preparation,
    inspect_luanti_8704_patch_split,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 8704)
_SOURCE_MANIFEST = (
    "raw/github/luanti/8704/source-manifest/attempt-1/source-manifest.json"
)
_PULL = "raw/github/luanti/8704/pull/attempt-1/pull.json"
_PATCH = "raw/github/luanti/8704/pull-diff/attempt-1/pull.diff"
_MIRROR = "private/repository-enumeration-git/luanti/mirror.git"
_MERGED_TIP = "f" * 40


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/luanti-8704-test/v1",
        scope="screening",
        granted_at="2026-08-13T10:00:00Z",
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


def _patch(*, include_optional_depends: bool = True) -> bytes:
    mod_conf = "+optional_depends = default\n" if include_optional_depends else ""
    return (
        """\
diff --git a/games/minimal/mods/default/init.lua b/games/minimal/mods/default/init.lua
--- a/games/minimal/mods/default/init.lua
+++ b/games/minimal/mods/default/init.lua
@@ -1,7 +1 @@
-local function test_get_craft_result()
-\tlocal output = minetest.get_craft_result(input)
-\tassert(output.item)
-end
-test_get_craft_result()
diff --git a/games/minimal/mods/test/init.lua b/games/minimal/mods/test/init.lua
--- a/games/minimal/mods/test/init.lua
+++ b/games/minimal/mods/test/init.lua
@@ -1 +1,14 @@
+local function test_get_craft_result()
+\tlocal normal = minetest.get_craft_result({ method = "normal" })
+\tlocal fuel = minetest.get_craft_result({ method = "fuel" })
+\tlocal cooking = minetest.get_craft_result({ method = "cooking" })
+\tassert(normal.item:get_name() == "default:torch")
+\tassert(fuel.time > 0)
+\tassert(cooking.item:get_name() == "default:stone")
+end
+test_get_craft_result()
diff --git a/games/minimal/mods/test/mod.conf b/games/minimal/mods/test/mod.conf
--- a/games/minimal/mods/test/mod.conf
+++ b/games/minimal/mods/test/mod.conf
@@ -1,2 +1,3 @@
 name = test
 description = Adds unit tests for the engine
"""
        + mod_conf
        + """\
diff --git a/src/craftdef.cpp b/src/craftdef.cpp
--- a/src/craftdef.cpp
+++ b/src/craftdef.cpp
@@ -1 +1 @@
-old priority initialization
+new priority initialization
diff --git a/src/craftdef.h b/src/craftdef.h
--- a/src/craftdef.h
+++ b/src/craftdef.h
@@ -1 +1 @@
-old declaration
+new declaration
"""
    ).encode("utf-8")


def _write_source_packet(
    root: Path,
    *,
    include_optional_depends: bool = True,
) -> None:
    _write(
        root,
        _PULL,
        json.dumps({"number": 8704, "merged": True, "merge_commit_sha": _MERGED_TIP}),
    )
    _write(root, _PATCH, _patch(include_optional_depends=include_optional_depends))
    _write(
        root,
        _SOURCE_MANIFEST,
        json.dumps(
            {
                "candidate": _CANDIDATE.to_value(),
                "complete": True,
                "raw_paths": [_PULL, _PATCH],
                "patch_raw_path": _PATCH,
            }
        ),
    )
    write_stable_artifact(
        root,
        artifact_id="candidate_source_packet/luanti/8704/source/v2",
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
        created_at="2026-08-13T10:00:01Z",
        inputs=(
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                "2.0",
            ),
        ),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "raw_evidence_path": _SOURCE_MANIFEST,
        },
        authorization=_authorization(),
    )


def _write_mirror(root: Path) -> Path:
    mirror = root / _MIRROR
    (mirror / "objects").mkdir(parents=True)
    (mirror / "refs").mkdir()
    (mirror / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (mirror / "packed-refs").write_text("# pack-refs\n", encoding="utf-8")
    return mirror


def test_8704_descriptor_preserves_the_upstream_lua_test_without_running_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_source_packet(tmp_path)
    mirror = _write_mirror(tmp_path)

    def unexpected_command(*_args: object, **_kwargs: object) -> object:
        pytest.fail("the Luanti 8704 descriptor must not execute a command")

    monkeypatch.setattr(subprocess, "run", unexpected_command)
    revisions_before = tuple(
        sorted(
            path.relative_to(tmp_path).as_posix()
            for path in (tmp_path / "artifacts").rglob("*")
            if path.is_file()
        )
    )

    descriptor = describe_luanti_8704_minimal_game_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    assert isinstance(descriptor, Luanti8704PreparationDescriptor)
    assert descriptor.profile_id == LUANTI_8704_PROFILE_ID
    assert descriptor.merged_tip == _MERGED_TIP
    assert descriptor.mirror == mirror
    assert descriptor.fixed_archive_argv == (
        "git",
        f"--git-dir={mirror}",
        "archive",
        "--format=tar",
        _MERGED_TIP,
    )
    assert descriptor.buggy_apply_argv == (
        "git",
        "apply",
        "-R",
        "--whitespace=nowarn",
        "-",
    )
    assert descriptor.preserved_test_paths == (
        "games/minimal/mods/default/init.lua",
        "games/minimal/mods/test/init.lua",
        "games/minimal/mods/test/mod.conf",
    )
    assert b"games/minimal/mods/" not in descriptor.production_reverse_patch
    assert b"test_get_craft_result" not in descriptor.production_reverse_patch
    assert b"src/craftdef.cpp" in descriptor.production_reverse_patch
    assert b"src/craftdef.h" in descriptor.production_reverse_patch
    prerequisites = descriptor.minimal_game_prerequisites
    assert prerequisites.game_id == "minimal"
    assert prerequisites.server_binary_relative_path == "bin/minetestserver"
    assert prerequisites.required_mod_paths == (
        "games/minimal/mods/default",
        "games/minimal/mods/test",
    )
    assert prerequisites.test_entry_path == "games/minimal/mods/test/init.lua"
    assert prerequisites.test_function == "test_get_craft_result"
    assert prerequisites.required_mod_dependency == "default"
    assert prerequisites.requires_isolated_writable_world is True
    assert prerequisites.requires_external_bounded_supervisor is True
    assert prerequisites.client_interaction_required is False
    assert (
        tuple(
            sorted(
                path.relative_to(tmp_path).as_posix()
                for path in (tmp_path / "artifacts").rglob("*")
                if path.is_file()
            )
        )
        == revisions_before
    )


def test_8704_missing_local_mirror_is_a_typed_infrastructure_decision(
    tmp_path: Path,
) -> None:
    _write_source_packet(tmp_path)

    decision = describe_luanti_8704_minimal_game_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": LUANTI_8704_PROFILE_ID,
        "reason": "local_luanti_mirror_unavailable",
        "mirror_path": _MIRROR,
    }


def test_8704_missing_v2_source_packet_is_a_typed_infrastructure_decision(
    tmp_path: Path,
) -> None:
    decision = describe_luanti_8704_minimal_game_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": LUANTI_8704_PROFILE_ID,
        "reason": "persisted_v2_source_packet_unavailable",
        "source_packet_id": "candidate_source_packet/luanti/8704/source/v2",
    }


def test_8704_missing_minimal_test_dependency_is_a_typed_infrastructure_decision(
    tmp_path: Path,
) -> None:
    _write_source_packet(tmp_path, include_optional_depends=False)
    _write_mirror(tmp_path)

    decision = describe_luanti_8704_minimal_game_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": LUANTI_8704_PROFILE_ID,
        "reason": "minimal_game_test_dependency_unavailable",
        "required_mod_dependency": "default",
        "test_mod_conf_path": "games/minimal/mods/test/mod.conf",
    }


def test_8704_adapter_is_closed_to_its_single_candidate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only registered candidate"):
        describe_luanti_8704_minimal_game_preparation(
            tmp_path,
            candidate=CandidateKey("luanti", 8705),
        )


def test_8704_patch_split_classifies_the_embedded_test_as_support_relocation() -> None:
    patch_path = next(
        (
            Path(__file__).resolve().parents[1]
            / ".warptest-cache"
            / "c1_lazarus_selection_v1"
            / "raw"
            / "github"
            / "luanti"
            / "8704"
            / "pull-diff"
        ).glob("attempt-*/*.response.body")
    )

    split = inspect_luanti_8704_patch_split(patch_path.read_bytes())

    assert split.production_paths == ("src/craftdef.cpp", "src/craftdef.h")
    assert split.support_paths == ("games/minimal/mods/default/init.lua",)
    assert split.test_paths == (
        "games/minimal/mods/test/init.lua",
        "games/minimal/mods/test/mod.conf",
    )
    assert split.relocation_source == "games/minimal/mods/default/init.lua"
    assert split.relocation_destination == "games/minimal/mods/test/init.lua"
    assert split.relocation_marker == "local function test_get_craft_result()"


def test_8704_patch_split_fails_closed_when_relocation_body_changes() -> None:
    patch_path = next(
        (
            Path(__file__).resolve().parents[1]
            / ".warptest-cache"
            / "c1_lazarus_selection_v1"
            / "raw"
            / "github"
            / "luanti"
            / "8704"
            / "pull-diff"
        ).glob("attempt-*/*.response.body")
    )
    altered = patch_path.read_bytes().replace(
        b"assert(output.item:get_count() == 4)",
        b"assert(output.item:get_count() == 5)",
        1,
    )

    with pytest.raises(RuntimeError, match="relocation"):
        inspect_luanti_8704_patch_split(altered)
