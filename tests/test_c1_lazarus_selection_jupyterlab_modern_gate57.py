from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import jupyterlab_modern_gate57 as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


_CHANGE = 19227
_ISSUE = 19226
_FIXED_REVISION = "10495e8e976f073fc765ddd5ef517d5a51c594ab"
_BASE_REVISION = "aaaabbbbccccddddeeeeffff0000111122223333"
_PRODUCTION = "packages/ui-components/src/components/toolbar.tsx"
_TEST = "packages/ui-components/test/toolbar.spec.ts"
_BUILD = "packages/ui-components/lib/components/toolbar.js"
_F2P = "@jupyterlab/ui-components ReactiveToolbar #insertBefore() should insert before"
_P2P = "@jupyterlab/ui-components ReactiveToolbar #addItem() should add an item"
_FIXED_SOURCE = """
export class ReactiveToolbar {
  protected onChildRemoved(msg) {
    this._widgetPositions.delete(name);
  }
}
"""
_BUGGY_SOURCE = """
export class ReactiveToolbar {
}
"""
_TEST_SOURCE = """
describe('@jupyterlab/ui-components', () => {
  it('should insert before', () => expect(1).toBe(1));
  it('should add an item', () => expect(2).toBe(2));
});
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_many(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(_sha(path).encode("ascii"))
    return digest.hexdigest()


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="test/jupyterlab-modern-gate57",
        scope="preannotation-execution",
        granted_at="2026-08-15T02:00:00Z",
        local_user_id=os.getuid(),
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _fixture(root: Path) -> target.ModernCandidateProfile:
    raw = root / f"raw/github/jupyterlab/{_CHANGE}/current-v1"
    _write_json(
        raw / "commit.json",
        {
            "sha": _FIXED_REVISION,
            "parents": [{"sha": _BASE_REVISION}],
            "commit": {
                "message": (
                    f"Update the item positions when an item is removed (#{_CHANGE})"
                    f"\n\nFixes #{_ISSUE}"
                )
            },
            "files": [
                {"filename": _PRODUCTION, "patch": "+  protected onChildRemoved(msg)"},
                {"filename": _TEST, "patch": "+ should insert before"},
            ],
        },
    )
    _write_json(
        raw / f"issue-{_ISSUE}.json",
        {
            "number": _ISSUE,
            "state": "closed",
            "pull_request": None,
            "title": "Possible error when using insertRelative() in a reactive toolbar",
            "body": "The stored positions are stale after an item is removed.",
            "labels": [{"name": "bug"}],
        },
    )
    _write_json(
        raw / "issue-timeline.json",
        [{"event": "referenced", "commit_id": _FIXED_REVISION}],
    )

    trees = root / f"private/materialized/jupyterlab/{_CHANGE}/current-v1"
    for variant, source in (("fixed", _FIXED_SOURCE), ("buggy", _BUGGY_SOURCE)):
        values = {
            _PRODUCTION: source,
            _TEST: _TEST_SOURCE,
            _BUILD: source,
            "node_modules/jest/bin/jest.js": "// jest\n",
            "node_modules/typescript/bin/tsc": "// tsc\n",
            "packages/ui-components/package.json": '{"scripts": {"test": "jest"}}\n',
            "packages/ui-components/jest.config.js": "module.exports = {};\n",
        }
        for relative, content in values.items():
            path = trees / variant / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return target.ModernCandidateProfile(
        change_number=_CHANGE,
        issue_number=_ISSUE,
        fixed_revision=_FIXED_REVISION,
        base_revision=_BASE_REVISION,
        package_directory="packages/ui-components",
        production_paths=(_PRODUCTION,),
        test_path=_TEST,
        build_outputs=(_BUILD,),
        f2p_full_name=_F2P,
        p2p_full_name=_P2P,
        f2p_test_name="should insert before",
        p2p_test_name="should add an item",
        profile_id="jupyterlab-19227-node22-reactive-toolbar-v1",
        classification="real_bug_state_desync",
        issue_reproducer="removing a toolbar item leaves stale stored positions",
        production_change="track removals so stored positions stay in sync",
        raw_hashes={
            name: _sha(raw / name)
            for name in ("commit.json", f"issue-{_ISSUE}.json", "issue-timeline.json")
        },
        source_hashes={
            variant: _sha_many([trees / variant / _PRODUCTION])
            for variant in ("fixed", "buggy")
        },
        test_hash=_sha(trees / "fixed" / _TEST),
        issue_title_markers=("reactive toolbar",),
        issue_body_markers=("stale",),
        production_patch_markers=("onChildRemoved",),
        test_patch_markers=("should insert before",),
        fixed_only_markers=("onChildRemoved",),
        existing_api_used=("ReactiveToolbar", "addItem", "insertBefore"),
    )


class _Runner:
    def __init__(self, *, buggy_f2p_passes: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.buggy_f2p_passes = buggy_f2p_passes

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, timeout_seconds))
        if argv == ("/fixture/bin/node", "--version"):
            return NativeProcessReceipt("completed", 0, b"v22.22.0\n", b"")
        if "--version" in argv:
            value = "29.5.0" if argv[1].endswith("jest.js") else "Version 5.9.3"
            return NativeProcessReceipt("completed", 0, f"{value}\n".encode(), b"")
        if "--build" in argv:
            return NativeProcessReceipt("completed", 0, b"", b"")
        selector = argv[argv.index("--testNamePattern") + 1]
        phase = "f2p" if "insert before" in selector else "p2p"
        variant = "fixed" if "/fixed/" in str(cwd) else "buggy"
        full_name = _F2P if phase == "f2p" else _P2P
        failed = phase == "f2p" and variant == "buggy" and not self.buggy_f2p_passes
        summary = {
            "numTotalTests": 2,
            "numPassedTests": 0 if failed else 1,
            "numFailedTests": 1 if failed else 0,
            "numPendingTests": 1,
            "numRuntimeErrorTestSuites": 0,
            "success": not failed,
            "testResults": [
                {
                    "testExecError": None,
                    "assertionResults": [
                        {
                            "fullName": full_name,
                            "status": "failed" if failed else "passed",
                        },
                        {"fullName": "other", "status": "pending"},
                    ],
                }
            ],
        }
        return NativeProcessReceipt(
            "completed", 1 if failed else 0, json.dumps(summary).encode(), b""
        )


def test_profile_driven_runner_writes_twelve_cells_and_pool_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/bin/node")
    runner = _Runner()

    aggregate, transition, pool = target.execute_modern_gate57(
        tmp_path,
        profile=profile,
        authorization=_authorization(),
        process_runner=runner,
    )

    jest_calls = [call for call in runner.calls if "--json" in call[0]]
    assert len(jest_calls) == 12
    assert aggregate.payload["qualified"] is True
    assert aggregate.payload["buggy_f2p_clean_failure"] is True
    assert aggregate.payload["gate1_4_evidence"]["authoritative_bug_count"] == 1
    assert transition.payload["strict_gate_1_7"] is True
    assert pool.payload["state"] == "active_qualified"
    assert pool.payload["defect_locus"] == "at_goal"
    heads = list((tmp_path / "artifacts" / profile.attempt_kind).rglob("head.json"))
    assert len(heads) == 12
    assert all(
        call[0][call[0].index("--testNamePattern") + 1].startswith("^")
        for call in jest_calls
    )


def test_buggy_variant_passing_the_f2p_is_a_semantic_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/bin/node")

    aggregate, transition, third = target.execute_modern_gate57(
        tmp_path,
        profile=profile,
        authorization=_authorization(),
        process_runner=_Runner(buggy_f2p_passes=True),
    )

    assert aggregate.payload["outcome"] == "candidate_semantic_mismatch"
    assert transition.payload["strict_gate_1_7"] is False
    assert third.reference == transition.reference


def test_missing_fix_marker_in_the_fixed_tree_is_rejected(tmp_path: Path) -> None:
    profile = _fixture(tmp_path)
    path = tmp_path / profile.trees / "fixed" / _PRODUCTION
    path.write_text(_BUGGY_SOURCE, encoding="utf-8")
    broken = target.ModernCandidateProfile(
        **{
            **{
                field: getattr(profile, field)
                for field in profile.__dataclass_fields__
                if field != "source_hashes"
            },
            "source_hashes": {
                "fixed": _sha_many([path]),
                "buggy": profile.source_hashes["buggy"],
            },
        }
    )

    with pytest.raises(RuntimeError, match="fixed production inverse drifted"):
        target.validate_candidate(tmp_path, broken)


def test_anchored_selector_escapes_only_javascript_metacharacters() -> None:
    assert target.anchored_selector("a showDialog() b") == (
        r"^a showDialog\(\) b$"
    )
    assert "\\ " not in target.anchored_selector("with spaces")


def test_confirmation_and_authorization_are_required(tmp_path: Path) -> None:
    profile = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        target.execute_modern_gate57(
            tmp_path,
            profile=profile,
            authorization=_authorization(),
            confirmation="NO",
            process_runner=_Runner(),
        )
    revoked = AuthorizationRecord(
        authorization_id="test/jupyterlab-modern-revoked",
        scope="preannotation-execution",
        granted_at="2026-08-15T02:00:00Z",
        local_user_id=os.getuid(),
        revoked=True,
    )
    with pytest.raises(PermissionError, match="authorization is inactive"):
        target.execute_modern_gate57(
            tmp_path,
            profile=profile,
            authorization=revoked,
            process_runner=_Runner(),
        )
