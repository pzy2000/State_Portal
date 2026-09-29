from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import jupyterlab_18572_gate57 as target
from warptest.c1_lazarus_selection.luanti_native_executor import (
    NativeProcessReceipt,
)
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


_FIXED_SOURCE = """
export function highlight(code: string): void {
  let pos = 0;
  if (pos === 0 && code.length > 0) {
    document.createTextNode(code);
  } else if (pos < code.length) {
    document.createTextNode(code.slice(pos, code.length));
  }
}
"""
_BUGGY_SOURCE = """
export function highlight(code: string, tree: { length: number }): void {
  let pos = 0;
  if (pos < tree.length - 1) {
    document.createTextNode(code.slice(pos, tree.length));
  }
}
"""
_TEST_SOURCE = """
describe('@jupyterlab/codemirror', () => {
  describe('EditorLanguageRegistry', () => {
    describe('#highlight', () => {
      it('should load a defined spec', () => expect(true).toBe(true));
      it('should render single-character code blocks', () => expect('x').toBe('x'));
    });
  });
});
"""
_FIXED_BUILD = "if (pos === 0 && code.length > 0) { return code; }\n"
_BUGGY_BUILD = "if (pos < tree.length - 1) { return code.slice(pos, tree.length); }\n"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="test/jupyterlab-18572-gate57",
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
    pull = {
        "number": 18572,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": "4e38172c7407dcf900fa7cc4b93a11713fcbb6c5",
        "base": {"sha": "92c8993a883267978d04f443e0b869dbd305898e"},
        "head": {"sha": "ff264f91ce586b74d4dfc8eb0dab95cb7e0b0be6"},
        "body": "Fixes #17779\nCloses #18285 (replaces)",
    }
    files = [
        {
            "filename": target._PRODUCTION.as_posix(),
            "patch": (
                "- if (pos < tree.length - 1)\n"
                "+ if (pos === 0 && code.length > 0)"
            ),
        },
        {
            "filename": target._TEST.as_posix(),
            "patch": "+ should render single-character code blocks",
        },
    ]
    issue = {
        "number": 17779,
        "state": "closed",
        "title": "Code with single variable renders into empty block",
        "body": "A single variable renders as an empty invisible block",
        "pull_request": None,
    }
    replacement = {
        "number": 18285,
        "state": "closed",
        "pull_request": {
            "html_url": "https://github.com/jupyterlab/jupyterlab/pull/18285",
            "merged_at": None,
        },
    }
    for name, value in (
        ("pull.json", pull),
        ("files.json", files),
        ("issue-17779.json", issue),
        ("pr-18285-as-issue.json", replacement),
    ):
        _write_json(raw / name, value)
    (raw / "pull.diff").write_text("production plus test patch\n", encoding="utf-8")

    for variant, source, build in (
        ("fixed", _FIXED_SOURCE, _FIXED_BUILD),
        ("buggy", _BUGGY_SOURCE, _BUGGY_BUILD),
    ):
        tree = root / target._TREES / variant
        values = {
            target._PRODUCTION: source,
            target._TEST: _TEST_SOURCE,
            target._BUILD_OUTPUT: build,
            target._YARN: "// yarn 3.5.0\n",
            target._JEST: "// jest 29.5.0\n",
            target._TSC: "// typescript 5.9.3\n",
            target._JEST_CONFIG: "module.exports = {};\n",
            target._TSCONFIG: "{}\n",
            target._GENERATED_TEST_OUTPUTS[0]: "module.exports = {};\n",
            target._GENERATED_TEST_OUTPUTS[1]: "module.exports = {};\n",
        }
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _write_json(
            tree / target._PACKAGE,
            {
                "devDependencies": {"jest": "^29.2.0", "typescript": "~5.9.3"},
                "scripts": {"test": "jest"},
            },
        )

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
    monkeypatch.setattr(target, "_TEST_HASH", _sha(root / target._TREES / "fixed" / target._TEST))
    monkeypatch.setattr(
        target,
        "_BUILD_HASHES",
        {
            variant: _sha(root / target._TREES / variant / target._BUILD_OUTPUT)
            for variant in ("fixed", "buggy")
        },
    )
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/bin/node")


class _Runner:
    def __init__(
        self,
        *,
        zero_test: tuple[str, str] | None = None,
        node_version: str = "v22.22.0",
        wrong_full_name: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.zero_test = zero_test
        self.node_version = node_version
        self.wrong_full_name = wrong_full_name

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, timeout_seconds))
        assert env["CI"] == "1"
        if argv == ("/fixture/bin/node", "--version"):
            return NativeProcessReceipt("completed", 0, f"{self.node_version}\n".encode(), b"")
        if "--version" in argv:
            executable = argv[1]
            if executable.endswith("yarn.js"):
                value = "3.5.0"
            elif executable.endswith("jest.js"):
                value = "29.5.0"
            else:
                value = "Version 5.9.3"
            return NativeProcessReceipt("completed", 0, f"{value}\n".encode(), b"")
        if "--build" in argv:
            return NativeProcessReceipt("completed", 0, b"", b"")
        assert "--json" in argv
        selector = argv[argv.index("--testNamePattern") + 1]
        phase = "f2p" if "single-character" in selector else "p2p"
        variant = cwd.parents[1].name
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
            return NativeProcessReceipt("completed", 1, json.dumps(summary).encode(), b"No tests found")
        failed = phase == "f2p" and variant == "buggy"
        selected = "wrong selected test" if self.wrong_full_name else selector[1:-1]
        summary = {
            "numTotalTests": 6,
            "numPassedTests": 0 if failed else 1,
            "numFailedTests": 1 if failed else 0,
            "numPendingTests": 5,
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
                        *[
                            {"fullName": f"unselected test {index}", "status": "pending"}
                            for index in range(5)
                        ],
                    ],
                }
            ],
        }
        stderr = b"FAIL assertion\n" if failed else b"PASS\n"
        return NativeProcessReceipt(
            "completed", 1 if failed else 0, json.dumps(summary).encode(), stderr
        )


def test_fresh_runner_writes_strict_twelve_cell_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    runner = _Runner()

    aggregate, transition = target.execute_jupyterlab_18572_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=runner,
    )

    assert len(runner.calls) == 22
    jest_calls = [call for call in runner.calls if "--json" in call[0]]
    assert all(call[1].name == "codemirror" for call in jest_calls)
    assert all(call[1].parents[1].name in {"fixed", "buggy"} for call in jest_calls)
    assert aggregate.payload["qualified"] is True
    assert aggregate.payload["attempt_count"] == 12
    assert aggregate.payload["old_attempts_loaded"] is False
    assert aggregate.payload["inherited_attempt_count"] == 0
    assert aggregate.payload["buggy_f2p_clean_failure"] is True
    assert aggregate.payload["no_timeout_unknown_or_invalid"] is True
    assert aggregate.payload["runtime_versions"] == target._VERSIONS
    assert aggregate.payload["gate1_4_evidence"]["authoritative_bug_count"] == 1
    assert transition.payload["strict_gate_1_7"] is True
    assert transition.payload["lineage_inherited"] is False
    assert transition.payload["gate8_13_performed"] is False
    assert transition.payload["gate_1_7_status"]["buggy_f2p_3x"] == "passed_clean_failure"

    reachable = set()
    pending = [transition.reference]
    while pending:
        reference = pending.pop()
        if reference in reachable:
            continue
        reachable.add(reference)
        pending.extend(target._load(tmp_path, reference).inputs)
    assert set(target._STATIC_CHAIN) < reachable
    profile = target._load(tmp_path, target._STATIC_CHAIN[-1])
    assert profile.payload["strict_gate_status"] == {
        "source_authority": "passed",
        "single_defect_mapping": "passed",
        "strict_inverse_patch": "passed",
        "test_api_compatibility": "passed",
    }
    assert profile.payload["typescript_build_config"].endswith("/tsconfig.json")

    heads = list(
        (
            tmp_path
            / "artifacts"
            / target._ATTEMPT_KIND
            / "jupyterlab"
            / "18572"
        ).rglob("head.json")
    )
    assert len(heads) == 12
    selectors = [call[0][call[0].index("--testNamePattern") + 1] for call in runner.calls if "--json" in call[0]]
    assert set(selectors) == {target._F2P_SELECTOR, target._P2P_SELECTOR}
    assert all(value.startswith("^") and value.endswith("$") for value in selectors)


def test_zero_tests_is_infrastructure_not_a_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    aggregate, transition = target.execute_jupyterlab_18572_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(zero_test=("f2p", "buggy")),
    )

    assert aggregate.payload["qualified"] is False
    assert aggregate.payload["outcome"] == "infrastructure_blocked"
    assert aggregate.payload["terminal"] is False
    assert aggregate.payload["buggy_f2p_clean_failure"] is False
    assert aggregate.payload["no_timeout_unknown_or_invalid"] is False
    assert transition.payload["strict_gate_1_7"] is False


def test_selected_assertion_full_name_must_match_exact_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    aggregate, _ = target.execute_jupyterlab_18572_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(wrong_full_name=True),
    )

    assert aggregate.payload["qualified"] is False
    assert aggregate.payload["outcome"] == "infrastructure_blocked"
    assert aggregate.payload["no_timeout_unknown_or_invalid"] is False


def test_runtime_version_drift_writes_resumable_typed_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)

    blocker, repeated = target.execute_jupyterlab_18572_gate57(
        tmp_path,
        authorization=_authorization(),
        process_runner=_Runner(node_version="v20.0.0"),
    )

    assert repeated.reference == blocker.reference
    assert blocker.payload["outcome"] == "infrastructure_blocked"
    assert blocker.payload["resumable"] is True
    assert blocker.payload["attempt_count"] == 0
    assert blocker.payload["old_attempts_loaded"] is False
    assert blocker.payload["candidate_execution_performed"] is False


def test_superseded_pr_must_remain_unmerged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    path = tmp_path / target._RAW / "pr-18285-as-issue.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["pull_request"]["merged_at"] = "2026-01-01T00:00:00Z"
    _write_json(path, value)
    raw_hashes = dict(target._RAW_HASHES)
    raw_hashes[path.name] = _sha(path)
    monkeypatch.setattr(target, "_RAW_HASHES", raw_hashes)

    with pytest.raises(RuntimeError, match="unmerged superseded PR"):
        target._validate_candidate(tmp_path)


def test_existing_fresh_attempt_namespace_is_never_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    old = (
        tmp_path
        / "artifacts"
        / target._ATTEMPT_KIND
        / "jupyterlab"
        / "18572"
        / "f2p"
        / "fixed"
    )
    old.mkdir(parents=True)
    (old / "head.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="fresh namespace is not empty"):
        target.execute_jupyterlab_18572_gate57(
            tmp_path,
            authorization=_authorization(),
            process_runner=_Runner(),
        )


def test_confirmation_and_authorization_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        target.execute_jupyterlab_18572_gate57(
            tmp_path,
            authorization=_authorization(),
            confirmation="NO",
            process_runner=_Runner(),
        )
    revoked = AuthorizationRecord(
        authorization_id="test/jupyterlab-18572-revoked",
        scope="preannotation-execution",
        granted_at="2026-08-15T00:00:00Z",
        local_user_id=os.getuid(),
        revoked=True,
    )
    with pytest.raises(PermissionError, match="authorization is inactive"):
        target.execute_jupyterlab_18572_gate57(
            tmp_path, authorization=revoked, process_runner=_Runner()
        )
