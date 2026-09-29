from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from warptest.lazarus_mining import (
    backfill_local_frozen_test_capsules,
    freeze_upstream_bundle,
    scan_repository,
    validate_lazarus_trial_provenance,
    validate_upstream_record,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Lazarus Test")
    _git(repo, "config", "user.email", "lazarus@example.invalid")
    target = repo / "builtin/game/falling.lua"
    target.parent.mkdir(parents=True)
    target.write_text("return 'old'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    before = _git(repo, "rev-parse", "HEAD")
    target.write_text("return 'fixed'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix #13485 (#15638)")
    fixed = _git(repo, "rev-parse", "HEAD")
    return repo, before, fixed


def test_scan_repository_reports_exact_reverse_applicability(tmp_path: Path) -> None:
    repo, _before, fixed = _repo(tmp_path)

    rows = scan_repository("luanti", repo, limit=20)

    row = next(item for item in rows if item["fix_commit"] == fixed)
    assert row["reverse_check_passed"] is True
    assert row["touched_files"] == ["builtin/game/falling.lua"]
    assert row["diff_loc"] == 2
    assert row["build_class"] == "none"
    assert row["references"] == {
        "issue_numbers": [13485],
        "pr_numbers": [15638],
    }


def test_freeze_bundle_writes_exact_patch_and_hashed_conversation(tmp_path: Path) -> None:
    repo, before, fixed = _repo(tmp_path)
    destination = tmp_path / "defects" / "lz-luanti-13485-falling-node"
    tests = [
        {
            "id": "upstream-manual-f2p",
            "kind": "fail_to_pass",
            "provenance": "pull_request_body",
            "command": ["warptest", "probe-falling-node"],
        },
        {
            "id": "builtin-p2p",
            "kind": "pass_to_pass",
            "provenance": "repository_test",
            "command": ["./bin/luanti", "--run-unittests"],
        },
    ]

    result = freeze_upstream_bundle(
        task_id="lz-luanti-13485-falling-node",
        benchmark="luanti",
        repo_path=repo,
        patch_base=before,
        patch_head=fixed,
        issue={
            "number": 13485,
            "url": "https://github.com/luanti-org/luanti/issues/13485",
            "title": "Falling node race",
            "body": "steps",
        },
        pull_request={
            "number": 15638,
            "url": "https://github.com/luanti-org/luanti/pull/15638",
            "title": "Fix falling behavior",
            "body": "How to test: drop sand.",
        },
        comments=[],
        reviews=[],
        tests=tests,
        retrieved_at="2026-08-10T00:00:00Z",
        destination=destination,
        observable=True,
    )

    patch = destination / "revert.patch"
    upstream_path = destination / "upstream.json"
    upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
    assert result["patch_path"] == str(patch.resolve())
    assert result["patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert result["touched_files"] == ["builtin/game/falling.lua"]
    assert upstream["fix_commit"] == fixed
    assert upstream["pinned_revision"] == fixed
    assert validate_upstream_record(upstream) == []
    reverse = subprocess.run(
        ["git", "-C", str(repo), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    assert reverse.returncode == 0, reverse.stderr


def test_freeze_bundle_captures_fix_commit_test_sources_for_inverse_patch_gates(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Lazarus Test")
    _git(repo, "config", "user.email", "lazarus@example.invalid")
    production = repo / "packages" / "widget.ts"
    regression = repo / "packages" / "widget.spec.ts"
    production.parent.mkdir(parents=True)
    production.write_text("export const value = 'buggy';\n", encoding="utf-8")
    regression.write_text("test('old', () => true);\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    before = _git(repo, "rev-parse", "HEAD")
    production.write_text("export const value = 'fixed';\n", encoding="utf-8")
    regression.write_text(
        "test('upstream regression', () => expect('fixed').toBe('fixed'));\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix widget regression (#1234)")
    fixed = _git(repo, "rev-parse", "HEAD")
    fixed_test_sha = hashlib.sha256(regression.read_bytes()).hexdigest()
    destination = tmp_path / "defects" / "lz-jupyterlab-widget-regression"
    common = {
        "origin": "upstream",
        "provenance": "repository_test",
        "command": ["jlpm", "test"],
        "argv": ["jlpm", "test"],
        "cwd": ".",
        "timeout_s": 60,
        "upstream_test_path": "packages/widget.spec.ts",
        "upstream_test_sha256": fixed_test_sha,
        "source_url": f"https://github.com/jupyterlab/jupyterlab/blob/{fixed}/packages/widget.spec.ts",
    }

    freeze_upstream_bundle(
        task_id="lz-jupyterlab-widget-regression",
        benchmark="jupyterlab",
        repo_path=repo,
        patch_base=before,
        patch_head=fixed,
        issue={"number": 1233, "body": "The widget shows the wrong value."},
        pull_request={"number": 1234, "body": "Adds an upstream regression test."},
        comments=[],
        reviews=[],
        tests=[
            {**common, "id": "f2p", "kind": "fail_to_pass"},
            {**common, "id": "p2p", "kind": "pass_to_pass"},
        ],
        retrieved_at="2026-08-11T00:00:00Z",
        destination=destination,
        observable=True,
    )

    record = json.loads((destination / "upstream.json").read_text(encoding="utf-8"))
    capsule = destination / "upstream-tests" / "packages" / "widget.spec.ts"
    assert capsule.read_bytes() == regression.read_bytes()
    assert record["tests"][0]["frozen_test_sources"] == [
        {
            "source_path": "upstream-tests/packages/widget.spec.ts",
            "destination_path": "packages/widget.spec.ts",
            "sha256": fixed_test_sha,
        }
    ]
    assert record["tests"][0]["upstream_test_sha256"] == fixed_test_sha
    assert validate_lazarus_trial_provenance(
        record,
        source_root=repo,
        frozen_test_root=destination,
    ) == []

    regression.write_text("later pinned-tree drift\n", encoding="utf-8")
    assert validate_lazarus_trial_provenance(
        record,
        source_root=repo,
        frozen_test_root=destination,
    ) == []
    capsule.write_text("tampered capsule\n", encoding="utf-8")
    assert any(
        "frozen test content SHA drift" in error
        for error in validate_lazarus_trial_provenance(
            record,
            source_root=repo,
            frozen_test_root=destination,
        )
    )


def test_local_capsule_backfill_uses_clean_fix_history_and_clone_relative_argv(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Lazarus Test")
    _git(repo, "config", "user.email", "lazarus@example.invalid")
    test_path = repo / "builtin" / "common" / "tests" / "vector_spec.lua"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("return 'buggy'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    test_path.write_text("return 'fixed regression'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix vector regression (#11919)")
    fix_commit = _git(repo, "rev-parse", "HEAD")
    relative = "builtin/common/tests/vector_spec.lua"
    legacy_source = "{harness_root}/games/luanti/" + relative
    legacy_tests = [
        {
            "id": test_id,
            "kind": kind,
            "origin": "upstream",
            "provenance": "repository_busted_test_suite",
            "upstream_test_path": relative,
            "argv": ["busted", selector, legacy_source],
            "command": ["busted", selector, legacy_source],
        }
        for test_id, kind, selector in (
            ("f2p", "fail_to_pass", "--filter=from_string"),
            ("p2p", "pass_to_pass", "--filter=zero"),
        )
    ]

    updated, capsules = backfill_local_frozen_test_capsules(
        benchmark="luanti",
        repo_path=repo,
        fix_commit=fix_commit,
        tests=legacy_tests,
    )

    expected = test_path.read_bytes()
    expected_sha = hashlib.sha256(expected).hexdigest()
    capsule_path = Path("upstream-tests") / relative
    assert capsules == {capsule_path: expected}
    assert updated[0]["upstream_test_sha256"] == expected_sha
    assert updated[0]["source_url"] == (
        "https://github.com/luanti-org/luanti/blob/"
        f"{fix_commit}/{relative}"
    )
    for test in updated:
        assert test["argv"][-1] == relative
        assert test["command"][-1] == relative
        assert all("{harness_root}/games/luanti/" not in part for part in test["argv"])
        assert test["frozen_test_sources"] == [
            {
                "source_path": capsule_path.as_posix(),
                "destination_path": relative,
                "sha256": expected_sha,
            }
        ]
        assert test["upstream_test_files"] == [
            {
                "path": relative,
                "sha256": expected_sha,
                "source_url": test["source_url"],
            }
        ]

    invalid_path = [dict(legacy_tests[0], upstream_test_path="../escape.lua")]
    with pytest.raises(ValueError, match="escapes the repository"):
        backfill_local_frozen_test_capsules(
            benchmark="luanti",
            repo_path=repo,
            fix_commit=fix_commit,
            tests=invalid_path,
        )

    symlink_path = test_path.with_name("linked.lua")
    symlink_path.symlink_to(test_path.name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add linked test source")
    symlink_commit = _git(repo, "rev-parse", "HEAD")
    symlink_test = [dict(legacy_tests[0], upstream_test_path=relative.replace("vector", "linked"))]
    with pytest.raises(ValueError, match="regular non-symlink"):
        backfill_local_frozen_test_capsules(
            benchmark="luanti",
            repo_path=repo,
            fix_commit=symlink_commit,
            tests=symlink_test,
        )
