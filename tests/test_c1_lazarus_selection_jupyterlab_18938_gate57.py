from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import jupyterlab_18938_gate57 as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


_FIXED_SOURCE = """
export class Dialog {
  _resolve(): void {
    try {
      value = body.getValue();
    } catch (err) {
      this.dispose();
      promise.reject(err);
      return;
    }
  }
}
"""
_BUGGY_SOURCE = """
export class Dialog {
  _resolve(): void {
    value = body.getValue();
  }
}
"""
_TEST_SOURCE = """
describe('@jupyterlab/apputils', () => {
  describe('showDialog()', () => {
    it('should give the value from the widget', () => expect(true).toBe(true));
    it('should reject the promise when getValue() throws', () => expect(1).toBe(1));
  });
});
"""
_FIXED_BUILD = "catch (err) { this.dispose(); promise.reject(err); return; }\n"
_BUGGY_BUILD = "value = body.getValue();\n"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="test/jupyterlab-18938-gate57",
        scope="preannotation-execution",
        granted_at="2026-08-15T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = root / target._RAW
    commit = {
        "sha": target._FIXED_REVISION,
        "parents": [{"sha": target._BASE_REVISION}],
        "commit": {
            "message": (
                "Prevent dialog from hanging when `getValue()` throws (#18938)\n\n"
                "Fixes #12863\n\nAnother PR targeting this issue: #18345"
            )
        },
        "files": [
            {
                "filename": target._PRODUCTION.as_posix(),
                "patch": (
                    "-      value = body.getValue();\n"
                    "+      try {\n"
                    "+        value = body.getValue();\n"
                    "+      } catch (err) {\n"
                    "+        this.dispose();\n"
                    "+        promise.reject(err);\n"
                    "+        return;\n"
                    "+      }"
                ),
            },
            {
                "filename": target._TEST.as_posix(),
                "patch": (
                    "+class ThrowingValueWidget extends Widget {}\n"
                    "+    it('should reject the promise when getValue() throws',"
                ),
            },
        ],
    }
    issue = {
        "number": target._ISSUE,
        "state": "closed",
        "pull_request": None,
        "title": "Dialog hangs if IBodyWidget<T>.getValue() throws an error",
        "body": (
            "If value = body.getValue() throws an error, then the dialog hangs and "
            "cannot be resolved or rejected. Browser refresh is needed."
        ),
        "labels": [{"name": "bug"}, {"name": "pkg:apputils"}],
    }
    timeline = [
        {"event": "cross-referenced"},
        {"event": "closed"},
        {"event": "referenced", "commit_id": target._FIXED_REVISION},
    ]
    backport = {
        "number": target._BACKPORT_PULL,
        "merged": True,
        "base": {"ref": "4.5.x"},
        "title": "Backport PR #18938 on branch 4.5.x (Prevent dialog from hanging)",
    }
    backport_files = [
        {"filename": target._PRODUCTION.as_posix()},
        {"filename": target._TEST.as_posix()},
    ]
    unavailable = {"message": "Not Found", "status": "404"}
    for name, value in (
        ("commit.json", commit),
        (f"issue-{target._ISSUE}.json", issue),
        ("issue-timeline.json", timeline),
        ("issue-comments.json", []),
        (f"backport-pull-{target._BACKPORT_PULL}.json", backport),
        ("backport-files.json", backport_files),
        ("pull-18938-response.json", unavailable),
    ):
        _write_json(raw / name, value)
    (raw / "commit.diff").write_text("production plus test patch\n", encoding="utf-8")

    for variant, source, build in (
        ("fixed", _FIXED_SOURCE, _FIXED_BUILD),
        ("buggy", _BUGGY_SOURCE, _BUGGY_BUILD),
    ):
        tree = root / target._TREES / variant
        values = {
            target._PRODUCTION: source,
            target._TEST: _TEST_SOURCE,
            target._BUILD_OUTPUT: build,
            target._JEST: "// jest 29.5.0\n",
            target._TSC: "// typescript 5.9.3\n",
            target._JEST_CONFIG: "module.exports = {};\n",
            target._TSCONFIG: "{}\n",
            target._TSCONFIG_TEST: "{}\n",
        }
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _write_json(tree / target._PACKAGE, {"scripts": {"test": "jest -i"}})

    monkeypatch.setattr(
        target,
        "_RAW_HASHES",
        {name: _sha(raw / name) for name in target._RAW_HASHES},
    )
    monkeypatch.setattr(
        target,
        "_SOURCE_HASHES",
        {
            variant: _sha(root / target._TREES / variant / target._PRODUCTION)
            for variant in ("fixed", "buggy")
        },
    )
    monkeypatch.setattr(
        target, "_TEST_HASH", _sha(root / target._TREES / "fixed" / target._TEST)
    )
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/bin/node")


class _Runner:
    def __init__(
        self,
        *,
        zero_test: tuple[str, str] | None = None,
        node_version: str = "v22.22.0",
        wrong_full_name: bool = False,
        fixed_f2p_fails: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.zero_test = zero_test
        self.node_version = node_version
        self.wrong_full_name = wrong_full_name
        self.fixed_f2p_fails = fixed_f2p_fails

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, timeout_seconds))
        assert env["CI"] == "true"
        if argv == ("/fixture/bin/node", "--version"):
            return NativeProcessReceipt(
                "completed", 0, f"{self.node_version}\n".encode(), b""
            )
        if "--version" in argv:
            value = "29.5.0" if argv[1].endswith("jest.js") else "Version 5.9.3"
            return NativeProcessReceipt("completed", 0, f"{value}\n".encode(), b"")
        if "--build" in argv:
            return NativeProcessReceipt("completed", 0, b"", b"")
        assert "--json" in argv
        selector = argv[argv.index("--testNamePattern") + 1]
        phase = "f2p" if "reject the promise" in selector else "p2p"
        variant = "fixed" if "/fixed/" in str(cwd) else "buggy"
        if self.zero_test == (phase, variant):
            summary = {
                "numTotalTests": 0,
                "numPassedTests": 0,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "numRuntimeErrorTestSuites": 0,
                "success": False,
                "testResults": [],
            }
            return NativeProcessReceipt(
                "completed", 1, json.dumps(summary).encode(), b"No tests found"
            )
        failed = phase == "f2p" and (
            variant == "buggy" or (variant == "fixed" and self.fixed_f2p_fails)
        )
        full_name = target._F2P_FULL_NAME if phase == "f2p" else target._P2P_FULL_NAME
        selected = "wrong selected test" if self.wrong_full_name else full_name
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
                            "fullName": selected,
                            "status": "failed" if failed else "passed",
                        },
                        {"fullName": "unselected test", "status": "pending"},
                    ],
                }
            ],
        }
        return NativeProcessReceipt(
            "completed",
            1 if failed else 0,
            json.dumps(summary).encode(),
            b"FAIL\n" if failed else b"PASS\n",
        )


def test_fresh_runner_writes_strict_twelve_cell_pool_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    runner = _Runner()

    aggregate, transition, pool = target.execute_jupyterlab_18938_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=runner,
    )

    assert len(runner.calls) == 20
    jest_calls = [call for call in runner.calls if "--json" in call[0]]
    assert len(jest_calls) == 12
    assert all(call[1].name == "apputils" for call in jest_calls)
    assert aggregate.payload["qualified"] is True
    assert aggregate.payload["attempt_count"] == 12
    assert aggregate.payload["buggy_f2p_clean_failure"] is True
    assert aggregate.payload["no_timeout_unknown_or_invalid"] is True
    assert aggregate.payload["old_attempts_loaded"] is False
    assert aggregate.payload["gate1_4_evidence"]["authoritative_bug_count"] == 1
    assert aggregate.payload["gate1_4_evidence"]["pr_page_available"] is False
    assert aggregate.payload["gate1_4_evidence"]["authority_evidence"] == [
        "merged_commit_on_default_branch",
        "authoritative_issue_and_closing_timeline",
        "merged_backport_pull_request",
    ]
    assert transition.payload["strict_gate_1_7"] is True
    assert transition.payload["lineage_inherited"] is False
    assert transition.payload["gate_1_7_status"]["buggy_f2p_3x"] == (
        "passed_clean_failure"
    )
    assert pool.payload["state"] == "active_qualified"
    assert pool.payload["defect_locus"] == "at_goal"
    assert pool.payload["gate8_13_performed"] is False

    selectors = {call[0][call[0].index("--testNamePattern") + 1] for call in jest_calls}
    assert selectors == {
        r"^@jupyterlab/apputils showDialog\(\) "
        r"should reject the promise when getValue\(\) throws$",
        r"^@jupyterlab/apputils showDialog\(\) "
        r"should give the value from the widget$",
    }
    heads = list(
        (tmp_path / "artifacts" / target._ATTEMPT_KIND).rglob("head.json")
    )
    assert len(heads) == 12


def test_zero_tests_is_infrastructure_not_a_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    aggregate, transition, _ = target.execute_jupyterlab_18938_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(zero_test=("f2p", "buggy")),
    )

    assert aggregate.payload["qualified"] is False
    assert aggregate.payload["outcome"] == "infrastructure_blocked"
    assert aggregate.payload["terminal"] is False
    assert transition.payload["strict_gate_1_7"] is False


def test_selected_assertion_full_name_must_match_exact_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    aggregate, _, _ = target.execute_jupyterlab_18938_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(wrong_full_name=True),
    )

    assert aggregate.payload["qualified"] is False
    assert aggregate.payload["outcome"] == "infrastructure_blocked"


def test_failing_fixed_f2p_is_a_semantic_mismatch_without_pool_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    aggregate, transition, _ = target.execute_jupyterlab_18938_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(fixed_f2p_fails=True),
    )

    assert aggregate.payload["outcome"] == "candidate_semantic_mismatch"
    assert aggregate.payload["terminal"] is True
    assert transition.payload["strict_gate_1_7"] is False
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        target._load(tmp_path, target._POOL)


def test_runtime_version_drift_writes_resumable_typed_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    blocker, repeated, also = target.execute_jupyterlab_18938_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(node_version="v20.0.0"),
    )

    assert repeated.reference == blocker.reference == also.reference
    assert blocker.payload["outcome"] == "infrastructure_blocked"
    assert blocker.payload["resumable"] is True
    assert blocker.payload["attempt_count"] == 0
    assert blocker.payload["candidate_execution_performed"] is False


def test_buggy_tree_must_not_contain_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    path = tmp_path / target._TREES / "buggy" / target._PRODUCTION
    path.write_text(_FIXED_SOURCE, encoding="utf-8")
    monkeypatch.setattr(
        target,
        "_SOURCE_HASHES",
        {**target._SOURCE_HASHES, "buggy": _sha(path)},
    )

    with pytest.raises(RuntimeError, match="buggy production-only inverse drifted"):
        target._validate_candidate(tmp_path)


def test_confirmation_and_authorization_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        target.execute_jupyterlab_18938_gate57(
            tmp_path,
            authorization=_authorization(),
            confirmation="NO",
            process_runner=_Runner(),
        )
    revoked = AuthorizationRecord(
        authorization_id="test/jupyterlab-18938-revoked",
        scope="preannotation-execution",
        granted_at="2026-08-15T00:00:00Z",
        local_user_id=os.getuid(),
        revoked=True,
    )
    with pytest.raises(PermissionError, match="authorization is inactive"):
        target.execute_jupyterlab_18938_gate57(
            tmp_path, authorization=revoked, process_runner=_Runner()
        )
