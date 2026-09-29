from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from warptest.lazarus_mining import (
    CandidateFacts,
    FORMAL_UPSTREAM_REPOSITORIES,
    UPSTREAM_REPOSITORIES,
    build_upstream_record,
    classify_build_class,
    distinguishing_expected_values,
    parse_fix_references,
    validate_candidate_facts,
    validate_lazarus_trial_provenance,
    validate_upstream_record,
    verify_public_text_has_no_hidden_tokens,
)


def test_lazarus_miner_is_scoped_to_six_authoritative_upstreams() -> None:
    assert UPSTREAM_REPOSITORIES == {
        "luanti": ("github", "luanti-org/luanti"),
        "blender": ("gitea", "blender/blender"),
        "jupyterlab": ("github", "jupyterlab/jupyterlab"),
        "openra": ("github", "OpenRA/OpenRA"),
        "jynew": ("github", "jynew/jynew"),
        "pal3": ("github", "0x7c13/Pal3.Unity"),
    }
    assert FORMAL_UPSTREAM_REPOSITORIES == {
        "luanti": ("github", "luanti-org/luanti"),
        "blender": ("gitea", "blender/blender"),
        "jupyterlab": ("github", "jupyterlab/jupyterlab"),
        "openra": ("github", "OpenRA/OpenRA"),
    }


@pytest.mark.parametrize(
    ("benchmark", "files", "expected"),
    [
        ("luanti", ["builtin/game/falling.lua"], "none"),
        ("luanti", ["src/client/game.cpp"], "incremental_cpp"),
        ("blender", ["scripts/addons/io.py"], "none"),
        ("blender", ["source/blender/editors/object.cc"], "incremental_cpp"),
        ("jupyterlab", ["packages/filebrowser/src/model.ts"], "jlpm"),
        ("jupyterlab", ["jupyterlab/handlers/build_handler.py"], "none"),
        ("openra", ["OpenRA.Mods.Common/Widgets/X.cs"], "incremental_dotnet"),
        ("jynew", ["Assets/Scripts/Battle.cs"], "unity"),
        ("pal3", ["Assets/Scripts/Game.cs"], "unity"),
    ],
)
def test_build_class_is_mechanical(
    benchmark: str, files: list[str], expected: str
) -> None:
    assert classify_build_class(benchmark, files) == expected


def test_parse_fix_references_distinguishes_github_and_blender() -> None:
    assert parse_fix_references("luanti", "Fix #13485 (#15638)") == {
        "issue_numbers": [13485],
        "pr_numbers": [15638],
    }
    assert parse_fix_references("openra", "Merge pull request #21906") == {
        "issue_numbers": [],
        "pr_numbers": [21906],
    }
    assert parse_fix_references("blender", "Fix #124573: Keep active item") == {
        "issue_numbers": [124573],
        "pr_numbers": [],
    }


def test_parse_fix_references_keeps_squash_pr_suffix_when_body_follows() -> None:
    assert parse_fix_references(
        "jupyterlab",
        "Fix toolbar clipping in Safari (#18998)\n\n## References\n\nFixes #18997",
    ) == {
        "issue_numbers": [18997],
        "pr_numbers": [18998],
    }


def test_candidate_gate_rejects_large_or_non_reversible_patch() -> None:
    valid = CandidateFacts(
        benchmark="luanti",
        pinned_revision="a" * 40,
        fix_commit="b" * 40,
        touched_files=("builtin/game/falling.lua",),
        additions=13,
        deletions=10,
        reverse_check_passed=True,
        observable=True,
        has_authoritative_conversation=True,
        has_f2p=True,
        has_p2p=True,
    )
    assert validate_candidate_facts(valid) == []

    too_large = copy.copy(valid)
    object.__setattr__(too_large, "additions", 111)
    broken = copy.copy(valid)
    object.__setattr__(broken, "reverse_check_passed", False)

    assert any("diff_loc" in item for item in validate_candidate_facts(too_large))
    assert any("reverse" in item for item in validate_candidate_facts(broken))

    excluded = copy.copy(valid)
    object.__setattr__(excluded, "benchmark", "jynew")
    assert any(
        "formal four-repository scope" in item
        for item in validate_candidate_facts(excluded)
    )


def test_upstream_record_freezes_raw_normalized_sources_tests_and_hashes() -> None:
    record = build_upstream_record(
        task_id="lz-luanti-13485-falling-node",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue={
            "number": 13485,
            "url": "https://github.com/luanti-org/luanti/issues/13485",
            "title": "Falling node race",
            "body": "Steps:\r\n1. Place sand.  \n2. Place a node.",
        },
        pull_request={
            "number": 15638,
            "url": "https://github.com/luanti-org/luanti/pull/15638",
            "title": "Fix falling node placement",
            "body": "How to test:\nDrop sand on normal nodes.",
        },
        comments=[
            {
                "source": "issue",
                "url": "https://github.com/luanti-org/luanti/issues/13485#issuecomment-1",
                "author": "reporter",
                "created_at": "2025-01-05T00:00:00Z",
                "body": "Reproduction remains on default:cloud.",
            }
        ],
        reviews=[],
        fix_commit="c" * 40,
        pinned_revision="d" * 40,
        touched_files=["builtin/game/falling.lua"],
        patch_sha256="e" * 64,
        tests=[
            {
                "id": "upstream-manual-f2p",
                "kind": "fail_to_pass",
                "provenance": "pull_request_body",
                "command": ["manual", "drop-sand"],
            },
            {
                "id": "builtin-unit-p2p",
                "kind": "pass_to_pass",
                "provenance": "repository_test",
                "command": ["./bin/luanti", "--run-unittests"],
            },
        ],
        retrieved_at="2026-08-10T00:00:00Z",
    )

    assert record["schema_version"] == 1
    assert record["issue"]["body_raw"].startswith("Steps:\r\n")
    assert record["issue"]["body_normalized"] == (
        "Steps:\n1. Place sand.\n2. Place a node."
    )
    assert len(record["issue"]["body_sha256"]) == 64
    assert len(record["record_sha256"]) == 64
    assert validate_upstream_record(record) == []


def test_upstream_record_fails_closed_on_body_or_test_tampering() -> None:
    record = build_upstream_record(
        task_id="lz-openra-21906-production-tabs",
        benchmark="openra",
        authority="github",
        repository="OpenRA/OpenRA",
        issue=None,
        pull_request={
            "number": 21906,
            "url": "https://github.com/OpenRA/OpenRA/pull/21906",
            "title": "Fix missing tabs",
            "body": "Reproduce by disabling a queue and adding an actor.",
        },
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["OpenRA.Mods.Common/Widgets/ProductionTabsWidget.cs"],
        patch_sha256="3" * 64,
        tests=[
            {
                "id": "issue-derived-gui-f2p",
                "kind": "fail_to_pass",
                "provenance": "issue_derived_gui_differential",
                "command": ["warptest", "openra-production-tabs"],
            },
            {
                "id": "utility-p2p",
                "kind": "pass_to_pass",
                "provenance": "repository_test",
                "command": ["dotnet", "test", "OpenRA.Test"],
            },
        ],
        retrieved_at="2026-08-10T00:00:00Z",
    )
    changed = copy.deepcopy(record)
    changed["pull_request"]["body_normalized"] += " altered"
    removed_test = copy.deepcopy(record)
    removed_test["tests"].pop()

    assert any("body" in item for item in validate_upstream_record(changed))
    assert any("record_sha256" in item for item in validate_upstream_record(removed_test))


def test_trial_provenance_rejects_manual_pr_steps_claimed_as_upstream_tests() -> None:
    record = build_upstream_record(
        task_id="lz-luanti-example",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue={"body": "A real issue discussion."},
        pull_request={"body": "How to test this manually."},
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["builtin/example.lua"],
        patch_sha256="3" * 64,
        tests=[
            {
                "id": "manual-f2p",
                "kind": "fail_to_pass",
                "origin": "upstream",
                "provenance": "pull_request_body_how_to_test",
                "command": ["manual", "reproduce"],
            },
            {
                "id": "suite-p2p",
                "kind": "pass_to_pass",
                "origin": "upstream",
                "provenance": "repository_test",
                "command": ["luanti", "--run-unittests"],
                "upstream_test_path": "tests/test_example.lua",
                "upstream_test_sha256": "4" * 64,
                "source_url": "https://github.com/luanti-org/luanti/blob/master/tests/test_example.lua",
            },
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )

    errors = validate_lazarus_trial_provenance(record)

    assert any("manual conversation steps" in error for error in errors)


def test_trial_provenance_allows_only_the_registered_openra_gui_f2p_exception() -> None:
    record = build_upstream_record(
        task_id="lz-openra-example",
        benchmark="openra",
        authority="github",
        repository="OpenRA/OpenRA",
        issue={"body": "A real issue discussion."},
        pull_request={"body": "A real pull request discussion."},
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["OpenRA.Mods.Common/Widgets/Example.cs"],
        patch_sha256="3" * 64,
        tests=[
            {
                "id": "gui-f2p",
                "kind": "fail_to_pass",
                "origin": "issue_derived_gui_differential",
                "provenance": "issue_and_pull_request_gui_differential",
                "command": ["warptest", "probe"],
            },
            {
                "id": "suite-p2p",
                "kind": "pass_to_pass",
                "origin": "upstream",
                "provenance": "OpenRA.Test_upstream_regression_suite",
                "command": ["dotnet", "test"],
                "upstream_test_path": "OpenRA.Test/OpenRA.Game/ActionQueueTest.cs",
                "upstream_test_sha256": "4" * 64,
                "source_url": "https://github.com/OpenRA/OpenRA/blob/bleed/OpenRA.Test/OpenRA.Game/ActionQueueTest.cs",
            },
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )

    assert validate_lazarus_trial_provenance(record) == []


def test_trial_provenance_binds_an_upstream_test_file_hash_and_rejects_harness_wrappers(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    test_path = source / "tests" / "regression.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_regression():\n    assert True\n", encoding="utf-8")
    digest = hashlib.sha256(test_path.read_bytes()).hexdigest()
    record = build_upstream_record(
        task_id="lz-jupyterlab-example",
        benchmark="jupyterlab",
        authority="github",
        repository="jupyterlab/jupyterlab",
        issue={"body": "A real issue."},
        pull_request={"body": "A real fix."},
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["jupyterlab/example.py"],
        patch_sha256="3" * 64,
        tests=[
            {
                "id": "f2p",
                "kind": "fail_to_pass",
                "origin": "upstream",
                "provenance": "repository_test",
                "command": ["pytest", "tests/regression.py"],
                "argv": ["pytest", "tests/regression.py"],
                "upstream_test_path": "tests/regression.py",
                "upstream_test_sha256": digest,
                "source_url": "https://github.com/jupyterlab/jupyterlab/blob/main/tests/regression.py",
            },
            {
                "id": "p2p",
                "kind": "pass_to_pass",
                "origin": "upstream",
                "provenance": "repository_test",
                "command": ["pytest", "tests/regression.py"],
                "argv": ["pytest", "tests/regression.py"],
                "upstream_test_path": "tests/regression.py",
                "upstream_test_sha256": digest,
                "source_url": "https://github.com/jupyterlab/jupyterlab/blob/main/tests/regression.py",
            },
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )

    assert validate_lazarus_trial_provenance(record, source_root=source) == []
    test_path.write_text("tampered\n", encoding="utf-8")
    assert any(
        "content SHA drift" in error
        for error in validate_lazarus_trial_provenance(record, source_root=source)
    )


def test_real_luanti_phase0_candidate_uses_hashed_repository_tests() -> None:
    """The paid pilot may admit Luanti only through committed upstream tests."""

    workspace = Path(__file__).resolve().parents[1]
    task_id = "lz-luanti-11919-vector-from-string"
    record_path = (
        workspace
        / "benchmarks"
        / "p5"
        / "lazarus"
        / "defects"
        / task_id
        / "upstream.json"
    )
    authoring_path = (
        workspace
        / "benchmarks"
        / "p5"
        / "lazarus"
        / "authoring"
        / f"{task_id}.tests.json"
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    authoring = json.loads(authoring_path.read_text(encoding="utf-8"))
    evidence_root = record_path.parent
    fixed_test = subprocess.run(
        [
            "git",
            "-C",
            str(workspace / "games" / "luanti"),
            "show",
            "196562870552b156265feb83043da2bc21bd6246:"
            "builtin/common/tests/vector_spec.lua",
        ],
        check=True,
        capture_output=True,
    ).stdout
    capsule_relative = Path("upstream-tests/builtin/common/tests/vector_spec.lua")
    capsule = evidence_root / capsule_relative
    expected_sha = hashlib.sha256(fixed_test).hexdigest()

    assert record["fix_commit"] == "196562870552b156265feb83043da2bc21bd6246"
    assert {test["kind"] for test in record["tests"]} == {
        "fail_to_pass",
        "pass_to_pass",
    }
    assert record["tests"] == authoring["tests"]
    assert capsule.read_bytes() == fixed_test
    current = evidence_root
    for part in capsule_relative.parts:
        current = current / part
        assert not current.is_symlink()
    for test in record["tests"]:
        assert test["upstream_test_path"] == "builtin/common/tests/vector_spec.lua"
        assert test["upstream_test_sha256"] == expected_sha
        assert test["source_url"] == (
            "https://github.com/luanti-org/luanti/blob/"
            "196562870552b156265feb83043da2bc21bd6246/"
            "builtin/common/tests/vector_spec.lua"
        )
        assert test["argv"] == test["command"]
        assert test["argv"][-1] == "builtin/common/tests/vector_spec.lua"
        assert all("{harness_root}/games/luanti/" not in value for value in test["argv"])
        assert test["upstream_test_files"] == [
            {
                "path": "builtin/common/tests/vector_spec.lua",
                "sha256": expected_sha,
                "source_url": test["source_url"],
            }
        ]
        assert test["frozen_test_sources"] == [
            {
                "source_path": capsule_relative.as_posix(),
                "destination_path": "builtin/common/tests/vector_spec.lua",
                "sha256": expected_sha,
            }
        ]
    assert validate_lazarus_trial_provenance(
        record,
        source_root=workspace / "games" / "luanti",
        harness_root=workspace,
        frozen_test_root=evidence_root,
    ) == []


def test_external_upstream_runner_is_bound_to_a_hashed_tool_tree(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    test_file = source / "test.lua"
    test_file.write_text("return true\n", encoding="utf-8")
    runner_root = tmp_path / "tools" / "runner"
    runner_root.mkdir(parents=True)
    runner = runner_root / "bin" / "runner"
    runner.parent.mkdir()
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    record = build_upstream_record(
        task_id="lz-luanti-runner-lock",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue=None,
        pull_request={"number": 1, "body": "test"},
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["builtin/common/vector.lua"],
        patch_sha256="3" * 64,
        tests=[
            {
                "id": "f2p",
                "kind": "fail_to_pass",
                "origin": "upstream",
                "provenance": "repository_test",
                "argv": ["{harness_root}/tools/runner/bin/runner", "test.lua"],
                "command": ["{harness_root}/tools/runner/bin/runner", "test.lua"],
                "cwd": ".",
                "timeout_s": 60,
                "upstream_test_path": "test.lua",
                "upstream_test_sha256": hashlib.sha256(test_file.read_bytes()).hexdigest(),
                "source_url": "https://example.invalid/test.lua",
                "upstream_runner_root": "tools/runner",
                "upstream_runner_tree_sha256": "0" * 64,
            },
            {
                "id": "p2p",
                "kind": "pass_to_pass",
                "origin": "upstream",
                "provenance": "repository_test_suite",
                "argv": ["{harness_root}/tools/runner/bin/runner", "test.lua"],
                "command": ["{harness_root}/tools/runner/bin/runner", "test.lua"],
                "cwd": ".",
                "timeout_s": 60,
                "upstream_test_path": "test.lua",
                "upstream_test_sha256": hashlib.sha256(test_file.read_bytes()).hexdigest(),
                "source_url": "https://example.invalid/test.lua",
                "upstream_runner_root": "tools/runner",
                "upstream_runner_tree_sha256": "0" * 64,
            },
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )

    errors = validate_lazarus_trial_provenance(
        record, source_root=source, harness_root=tmp_path
    )

    assert any("runner tool tree SHA drift" in error for error in errors)


def test_absolute_upstream_runner_binds_executable_and_version(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    test_file = source / "test.py"
    test_file.write_text("pass\n", encoding="utf-8")
    executable = Path(sys.executable).resolve()
    version = subprocess.run(
        [str(executable), "--version"], capture_output=True, check=True
    )
    common = {
        "origin": "upstream",
        "provenance": "repository_test_suite",
        "argv": [str(executable), "--version"],
        "command": [str(executable), "--version"],
        "cwd": ".",
        "timeout_s": 60,
        "upstream_test_path": "test.py",
        "upstream_test_sha256": hashlib.sha256(test_file.read_bytes()).hexdigest(),
        "source_url": "https://example.invalid/test.py",
        "upstream_runner_executable_sha256": "0" * 64,
        "upstream_runner_version_argv": [str(executable), "--version"],
        "upstream_runner_version_stdout_sha256": hashlib.sha256(
            version.stdout
        ).hexdigest(),
        "upstream_runner_version_stderr_sha256": hashlib.sha256(
            version.stderr
        ).hexdigest(),
    }
    record = build_upstream_record(
        task_id="lz-luanti-absolute-runner",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue=None,
        pull_request={"number": 1, "body": "test"},
        comments=[],
        reviews=[],
        fix_commit="1" * 40,
        pinned_revision="2" * 40,
        touched_files=["builtin/common/vector.lua"],
        patch_sha256="3" * 64,
        tests=[
            {**common, "id": "f2p", "kind": "fail_to_pass"},
            {**common, "id": "p2p", "kind": "pass_to_pass"},
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )

    errors = validate_lazarus_trial_provenance(record, source_root=source)

    assert any("runner executable SHA drift" in error for error in errors)


def test_reverse_token_guard_rejects_hidden_values_locations_and_version_hints() -> None:
    spec = {
        "evaluation": {
            "lazarus": {
                "goal_checks": [{"type": "node_is", "expected": "default:stone"}],
                "defect_signature": [
                    {"type": "node_is", "expected": "default:sand"}
                ],
                "invariant_checks": [{"type": "player_is", "expected": "singleplayer"}],
            }
        }
    }

    verify_public_text_has_no_hidden_tokens(
        "A falling test block is above a clear landing cell.\n"
        "Place a solid marker in the landing cell just before it settles.",
        spec=spec,
        touched_files=["builtin/game/falling.lua"],
    )
    with pytest.raises(ValueError, match="default:stone"):
        verify_public_text_has_no_hidden_tokens(
            "Place default:stone before it settles.",
            spec=spec,
            touched_files=["builtin/game/falling.lua"],
        )
    with pytest.raises(ValueError, match="falling.lua"):
        verify_public_text_has_no_hidden_tokens(
            "Exercise the behavior implemented by falling.lua.",
            spec=spec,
            touched_files=["builtin/game/falling.lua"],
        )
    with pytest.raises(ValueError, match="version"):
        verify_public_text_has_no_hidden_tokens(
            "On the buggy version, place a marker.",
            spec=spec,
            touched_files=["builtin/game/falling.lua"],
        )


def test_reverse_token_guard_allows_values_both_versions_expect() -> None:
    """Only values that separate the two versions stay coordinator-only."""

    spec = {
        "evaluation": {
            "lazarus": {
                "goal_checks": [
                    {"type": "node_is", "expected": "default:stone"},
                    {"type": "stands_on", "expected": "default:pedestal"},
                ],
                "defect_signature": [
                    {"type": "node_is", "expected": "default:sand"},
                    {"type": "stands_on", "expected": "default:pedestal"},
                ],
                "invariant_checks": [{"type": "player_is", "expected": "singleplayer"}],
            }
        }
    }

    assert distinguishing_expected_values(spec) == {
        "default:stone",
        "default:sand",
        "singleplayer",
    }
    verify_public_text_has_no_hidden_tokens(
        "A falling test block rests on default:pedestal.\n"
        "Place a solid marker in the landing cell just before it settles.",
        spec=spec,
        touched_files=["builtin/game/falling.lua"],
    )
    with pytest.raises(ValueError, match="default:sand"):
        verify_public_text_has_no_hidden_tokens(
            "The block on default:pedestal turns into default:sand.",
            spec=spec,
            touched_files=["builtin/game/falling.lua"],
        )
