from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from warptest.lazarus_mining import backfill_local_frozen_test_capsules


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare_luanti_curated_candidates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "prepare_luanti_curated_candidates", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_uses_only_registered_luanti_evidence_and_prebuild_recipes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()

    assert module.main(["--workspace", str(ROOT), "--dry-run"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert [row["task_id"] for row in payload["results"]] == [
        "lz-luanti-14396-metadata-set-string-nil",
        "lz-luanti-14586-full-crafting-shift-move",
        "lz-luanti-14555-negative-half-pointing",
    ]
    artifacts = {row["task_id"]: row["prebuild_artifact"] for row in payload["results"]}
    assert artifacts == {
        "lz-luanti-14396-metadata-set-string-nil": "bin/luanti",
        "lz-luanti-14586-full-crafting-shift-move": "bin/luanti",
        "lz-luanti-14555-negative-half-pointing": "bin/luanti",
    }
    metadata = payload["results"][0]
    assert (
        "upstream-tests/games/devtest/mods/unittests/metadata.lua"
        in metadata["capsules"]
    )
    for row in payload["results"]:
        assert row["prebuild_mode"] == "formal_registered_build"
        assert all(path.startswith("upstream-tests/") for path in row["capsules"])
        assert row["clone_relative_sources"] is True
    shim_result = next(
        row
        for row in payload["results"]
        if row["task_id"] == "lz-luanti-14586-full-crafting-shift-move"
    )
    assert shim_result["compatibility_shims"] == [
        {
            "base_source_path": "src/unittest/test_moveaction.cpp",
            "destination_path": "src/unittest/test_moveaction.cpp",
            "inserted_line": '#include "servermap.h"',
            "source_path": "upstream-tests/src/unittest/test_moveaction.cpp",
            "test_id": "luanti-pr14586-move-somewhere-partial-f2p",
        }
    ]


def test_capsule_writer_rejects_symlinked_parent_or_conflicting_existing_bytes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (evidence / "upstream-tests").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        module._write_capsule(
            evidence,
            Path("upstream-tests/src/test.cpp"),
            b"frozen test\n",
        )

    clean_evidence = tmp_path / "clean-evidence"
    target = clean_evidence / "upstream-tests/src/test.cpp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong\n")
    with pytest.raises(ValueError, match="conflicts"):
        module._write_capsule(
            clean_evidence,
            Path("upstream-tests/src/test.cpp"),
            b"frozen test\n",
        )


def test_multiplayer_contract_rejects_workspace_command_or_wrong_overlay() -> None:
    module = _load_module()
    task_id = "lz-luanti-14396-metadata-set-string-nil"
    tests = json.loads(
        (ROOT / "benchmarks/p5/lazarus/authoring" / f"{task_id}.tests.json").read_text(
            encoding="utf-8"
        )
    )["tests"]
    bad_command = [dict(test) for test in tests]
    bad_command[0]["argv"] = ["{harness_root}/games/luanti/util/test_multiplayer.sh"]
    with pytest.raises(ValueError, match="clone-local"):
        module._assert_registered_task_contract(task_id, bad_command)

    bad_overlay = [dict(test) for test in tests]
    bad_overlay[0]["frozen_test_sources"] = [
        {
            **bad_overlay[0]["frozen_test_sources"][0],
            "destination_path": "../outside.lua",
        }
    ]
    with pytest.raises(ValueError, match="metadata.lua"):
        module._assert_registered_task_contract(task_id, bad_overlay)


@pytest.mark.parametrize(
    ("task_id", "module_name"),
    (
        ("lz-luanti-14586-full-crafting-shift-move", "TestMoveAction"),
        ("lz-luanti-14555-negative-half-pointing", "TestVoxelAlgorithms"),
    ),
)
def test_cpp_unit_contract_requires_the_clone_root_runtime_binary(
    task_id: str,
    module_name: str,
) -> None:
    module = _load_module()
    tests = json.loads(
        (ROOT / "benchmarks/p5/lazarus/authoring" / f"{task_id}.tests.json").read_text(
            encoding="utf-8"
        )
    )["tests"]

    module._assert_registered_task_contract(task_id, tests)
    f2p = next(test for test in tests if test["kind"] == "fail_to_pass")
    assert f2p["argv"] == [
        "./bin/luanti",
        "--run-unittests",
        "--test-module",
        module_name,
    ]

    staged_binary = [dict(test) for test in tests]
    staged_binary[0]["argv"] = [
        "./build-lazarus-unittests/bin/luanti",
        "--run-unittests",
        "--test-module",
        module_name,
    ]
    with pytest.raises(ValueError, match="clone-local ./bin/luanti"):
        module._assert_registered_task_contract(task_id, staged_binary)

    capsules = {
        Path(source["source_path"]): b"registered frozen source"
        for test in tests
        for source in test.get("frozen_test_sources", [])
    }
    module._assert_clone_relative_sources(tests, capsules)
    workspace_fallback = [dict(test) for test in tests]
    workspace_fallback[0]["command"] = [
        "{harness_root}/games/luanti/bin/luanti",
        "--run-unittests",
        "--test-module",
        module_name,
    ]
    with pytest.raises(ValueError, match="workspace Luanti checkout"):
        module._assert_clone_relative_sources(workspace_fallback, capsules)


def test_14586_pinned_compatibility_shim_is_include_only_and_bound() -> None:
    """The c48 bridge may add one header, never alter the c044 test body."""

    module = _load_module()
    task_id = "lz-luanti-14586-full-crafting-shift-move"
    evidence = ROOT / "benchmarks/p5/lazarus/defects" / task_id
    record = json.loads((evidence / "upstream.json").read_text(encoding="utf-8"))
    authoring = json.loads(
        (ROOT / "benchmarks/p5/lazarus/authoring" / f"{task_id}.tests.json").read_text(
            encoding="utf-8"
        )
    )
    base_tests, base_capsules = backfill_local_frozen_test_capsules(
        benchmark="luanti",
        repo_path=ROOT / "games/luanti",
        fix_commit=record["fix_commit"],
        tests=record["tests"],
    )

    shimmed_tests, shimmed_capsules, receipts = (
        module._apply_registered_luanti_compatibility_shims(
            task_id=task_id,
            record=record,
            declared_tests=authoring["tests"],
            backfilled_tests=base_tests,
            capsules=base_capsules,
        )
    )

    assert shimmed_tests == record["tests"] == authoring["tests"]
    assert receipts == [
        {
            "test_id": "luanti-pr14586-move-somewhere-partial-f2p",
            "source_path": "upstream-tests/src/unittest/test_moveaction.cpp",
            "destination_path": "src/unittest/test_moveaction.cpp",
            "base_source_path": "src/unittest/test_moveaction.cpp",
            "inserted_line": '#include "servermap.h"',
        }
    ]
    source_path = Path("upstream-tests/src/unittest/test_moveaction.cpp")
    base = base_capsules[source_path]
    shimmed = shimmed_capsules[source_path]
    anchor = b'#include "mock_serveractiveobject.h"\n'
    assert shimmed == base.replace(anchor, anchor + b'#include "servermap.h"\n')
    assert shimmed.replace(b'#include "servermap.h"\n', b"") == base

    behavior_mutated_capsules = dict(base_capsules)
    behavior_mutated_capsules[source_path] = base.replace(
        b'"default:stone 110"', b'"default:stone 109"', 1
    )
    with pytest.raises(ValueError, match="does not match c044"):
        module._apply_registered_luanti_compatibility_shims(
            task_id=task_id,
            record=record,
            declared_tests=authoring["tests"],
            backfilled_tests=base_tests,
            capsules=behavior_mutated_capsules,
        )

    tampered = copy.deepcopy(authoring["tests"])
    f2p = next(test for test in tampered if test["kind"] == "fail_to_pass")
    f2p["frozen_test_compatibility_shim"]["inserted_line"] = '#include "inventory.h"'
    with pytest.raises(ValueError, match="include-only"):
        module._apply_registered_luanti_compatibility_shims(
            task_id=task_id,
            record=record,
            declared_tests=tampered,
            backfilled_tests=base_tests,
            capsules=base_capsules,
        )
