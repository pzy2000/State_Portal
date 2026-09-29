"""Fresh Gate 1--7 adapter for JupyterLab #18572.

This adapter is intentionally candidate-specific.  Its fresh v2 epoch binds the current-v1
GitHub captures, the production-only inverse already materialized in the
fixed/buggy trees, and the modern JupyterLab Jest toolchain before it can run
any test.  A successful invocation executes exactly twelve fresh cells
(fixed/buggy x F2P/P2P x three repetitions) in a new artifact namespace.

It does not install dependencies, consult historical attempts, perform paid
work, run formal trials, or construct any Gate 8--13 artifact.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Literal

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_JUPYTERLAB_18572_FRESH_GATE57_V2"
_VERSION = "2.0"
_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 18572}
_EPOCH = "candidate_gate57_jupyterlab_18572_current_v2"
_RAW = Path("raw/github/jupyterlab/18572/current-v1")
_TREES = Path("private/materialized/jupyterlab/18572/current-v1")
_PRODUCTION = Path("packages/codemirror/src/language.ts")
_TEST = Path("packages/codemirror/test/language.spec.ts")
_BUILD_OUTPUT = Path("packages/codemirror/lib/language.js")
_PACKAGE = Path("packages/codemirror/package.json")
_YARN = Path("jupyterlab/staging/yarn.js")
_JEST = Path("node_modules/jest/bin/jest.js")
_TSC = Path("node_modules/typescript/bin/tsc")
_JEST_CONFIG = Path("packages/codemirror/jest.config.js")
_TSCONFIG = Path("packages/codemirror/tsconfig.json")
_GENERATED_TEST_OUTPUTS = (
    Path("packages/codemirror/test/foo.js"),
    Path("packages/codemirror/test/foo.terms.js"),
)

_RAW_HASHES = {
    "pull.json": "7aa0db34f840ccb0b949cd35e9e5e8eedff8d4611c1cbc682b8c9884c33b5890",
    "files.json": "f22ebcd359a3b1050fd3d9159b7eafdb386aefb6d77bf120825db78a0d0363ae",
    "issue-17779.json": "633c142a72b27872b4182263f02c27cb93640ecd2ffd74e762a2d862fc8e04d4",
    "pr-18285-as-issue.json": "46f375d038794990d4b3d01d14a806ceb079186bdb5a04c5a01d067313403e52",
    "pull.diff": "7870b05784aa91dcbcba61d026dacacbeaa5bf68cffa61939ce7a44695e6adfc",
}
_SOURCE_HASHES = {
    "fixed": "b06acfbea41c192ec79157efe32c0b1f0df01c017a2f29200348609b5ad7bd18",
    "buggy": "39665f53ffadb270f5b820a7078b6ef7d35faeffd33c2fd0584821c18e040c8d",
}
_TEST_HASH = "06787af632eca9b3aa325d1c85c895474a7dabd86a43e5ad2e308b6fefae8460"
_BUILD_HASHES = {
    "fixed": "9734313fc10d4119a281587cd8bf4f8b03f6b850ec5fd3bfd088d2ec6e4a9589",
    "buggy": "ed67c4cc60eb6d76fe3293381d647b292e53bd953476a492adfaa2b68e552e34",
}
_VERSIONS = {
    "node": "v22.22.0",
    "yarn": "3.5.0",
    "jest": "29.5.0",
    "typescript": "Version 5.9.3",
}
_F2P_SELECTOR = (
    r"^@jupyterlab/codemirror EditorLanguageRegistry #highlight "
    r"should render single-character code blocks$"
)
_P2P_SELECTOR = (
    r"^@jupyterlab/codemirror EditorLanguageRegistry #highlight "
    r"should load a defined spec$"
)
_BUILD_KIND = "candidate_gate57_18572_runtime_build_v2"
_ATTEMPT_KIND = "candidate_gate57_18572_attempt_v2"
_STATIC_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/jupyterlab/18572/source/current-v2",
        "candidate_source_packet",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_test_patch/jupyterlab/18572/screening/current-v2",
        "candidate_test_patch",
        _VERSION,
    ),
    ArtifactReference(
        "source_atomicity/jupyterlab/18572/preannotation/current-v2",
        "source_atomicity",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_stage/jupyterlab/18572/test-manifest/current-v2",
        "candidate_stage",
        _VERSION,
    ),
    ArtifactReference(
        "fixed_source_capsule/jupyterlab/18572/fixed/current-v2",
        "fixed_source_capsule",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_profile/jupyterlab/18572/gate1-4/current-v2",
        "candidate_profile",
        _VERSION,
    ),
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_18572_aggregate/jupyterlab/18572/current-v2",
    "candidate_gate57_18572_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_18572_transition/jupyterlab/18572/current-v2",
    "candidate_gate57_18572_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_18572_blocker/jupyterlab/18572/current-v2",
    "candidate_gate57_18572_blocker",
    _VERSION,
)
_MAX_OUTPUT = 262_144


ProcessRunner = Callable[..., NativeProcessReceipt]


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int

    @property
    def selector(self) -> str:
        return _F2P_SELECTOR if self.phase == "f2p" else _P2P_SELECTOR

    @property
    def expected(self) -> str:
        if self.phase == "f2p" and self.variant == "buggy":
            return "failure"
        return "pass"


def execute_jupyterlab_18572_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
    process_runner: ProcessRunner | None = None,
) -> tuple[StableArtifact, StableArtifact]:
    """Validate Gate 1--4 and execute exactly twelve fresh Jest cells."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    _assert_fresh_namespace(root)
    evidence = _validate_candidate(root)
    static_chain = _persist_static_chain(root, evidence, authorization)
    runner = process_runner or run_bounded_process
    receipts: list[ArtifactReference] = []
    attempts: list[StableArtifact] = []
    try:
        node = _node_executable()
        environment = _closed_environment(root, node)
        for variant in ("fixed", "buggy"):
            _validate_runtime_and_build(
                root,
                variant=variant,
                node=node,
                environment=environment,
                static_chain=static_chain,
                receipts=receipts,
                runner=runner,
                authorization=authorization,
            )
        for phase in ("f2p", "p2p"):
            for variant in ("fixed", "buggy"):
                for repetition in range(1, 4):
                    attempts.append(
                        _run_cell(
                            root,
                            cell=_Cell(phase, variant, repetition),
                            node=node,
                            environment=environment,
                            static_chain=static_chain,
                            build_references=tuple(receipts),
                            runner=runner,
                            authorization=authorization,
                        )
                    )
    except BaseException as error:
        blocker = _write_blocker(
            root,
            evidence,
            error,
            static_chain=static_chain,
            receipts=tuple(receipts),
            attempts=attempts,
            authorization=authorization,
        )
        return blocker, blocker
    aggregate = _write_aggregate(
        root, evidence, static_chain, tuple(receipts), attempts, authorization
    )
    transition = _write_transition(root, aggregate, authorization)
    return aggregate, transition


def _validate_candidate(root: Path) -> dict[str, object]:
    raw = root / _RAW
    captures: dict[str, object] = {}
    for name, expected_hash in _RAW_HASHES.items():
        path = _ordinary_file(raw / name, root)
        if _sha256(path) != expected_hash:
            raise RuntimeError(f"#18572 current-v1 raw capture hash drifted: {name}")
        if name.endswith(".json"):
            captures[name] = _strict_json(path)

    pull = captures["pull.json"]
    issue = captures["issue-17779.json"]
    replacement = captures["pr-18285-as-issue.json"]
    files = captures["files.json"]
    if not isinstance(pull, dict) or not isinstance(issue, dict):
        raise RuntimeError("#18572 PR/issue captures are malformed")
    if (
        pull.get("number") != 18572
        or pull.get("state") != "closed"
        or pull.get("merged") is not True
        or pull.get("merge_commit_sha")
        != "4e38172c7407dcf900fa7cc4b93a11713fcbb6c5"
        or not isinstance(pull.get("base"), dict)
        or pull["base"].get("sha") != "92c8993a883267978d04f443e0b869dbd305898e"
        or not isinstance(pull.get("head"), dict)
        or pull["head"].get("sha") != "ff264f91ce586b74d4dfc8eb0dab95cb7e0b0be6"
    ):
        raise RuntimeError("#18572 authoritative PR identity drifted")
    if (
        issue.get("number") != 17779
        or issue.get("pull_request") is not None
        or issue.get("state") != "closed"
        or "single variable" not in str(issue.get("title", "")).lower()
        or "empty" not in str(issue.get("body", "")).lower()
    ):
        raise RuntimeError("#17779 is not the captured authoritative bug")
    if not isinstance(replacement, dict):
        raise RuntimeError("#18285 replacement capture is malformed")
    replacement_pr = replacement.get("pull_request")
    if (
        replacement.get("number") != 18285
        or replacement.get("state") != "closed"
        or not isinstance(replacement_pr, dict)
        or replacement_pr.get("merged_at") is not None
        or replacement_pr.get("html_url")
        != "https://github.com/jupyterlab/jupyterlab/pull/18285"
    ):
        raise RuntimeError("#18285 is not the captured unmerged superseded PR")
    body = str(pull.get("body", ""))
    references = {int(value) for value in re.findall(r"#([0-9]+)", body)}
    if (
        references != {17779, 18285}
        or not re.search(r"(?i)fixes\s+#17779\b", body)
        or not re.search(r"(?i)closes\s+#18285\s*\(replaces\)", body)
    ):
        raise RuntimeError("#18572 single-defect mapping drifted")

    if not isinstance(files, list) or len(files) != 2:
        raise RuntimeError("#18572 changed-file set drifted")
    by_name = {
        item.get("filename"): item
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    }
    if set(by_name) != {_PRODUCTION.as_posix(), _TEST.as_posix()}:
        raise RuntimeError("#18572 production/test patch boundary drifted")
    production_patch = str(by_name[_PRODUCTION.as_posix()].get("patch", ""))
    test_patch = str(by_name[_TEST.as_posix()].get("patch", ""))
    if (
        "pos < tree.length - 1" not in production_patch
        or "pos === 0 && code.length > 0" not in production_patch
        or "should render single-character code blocks" not in test_patch
    ):
        raise RuntimeError("#18572 captured patch semantics drifted")

    hashes: dict[str, dict[str, str]] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_directory(root / _TREES / variant, root)
        source = _ordinary_file(tree / _PRODUCTION, tree)
        test = _ordinary_file(tree / _TEST, tree)
        source_hash = _sha256(source)
        test_hash = _sha256(test)
        if source_hash != _SOURCE_HASHES[variant] or test_hash != _TEST_HASH:
            raise RuntimeError(f"#18572 {variant} materialized source hash drifted")
        source_text = source.read_text(encoding="utf-8")
        if variant == "fixed" and (
            "pos === 0 && code.length > 0" not in source_text
            or "pos < tree.length - 1" in source_text
        ):
            raise RuntimeError("#18572 fixed production source is not the PR head")
        if variant == "buggy" and (
            "pos < tree.length - 1" not in source_text
            or "pos === 0 && code.length > 0" in source_text
        ):
            raise RuntimeError("#18572 buggy production-only inverse drifted")
        if "should render single-character code blocks" not in test.read_text(
            encoding="utf-8"
        ):
            raise RuntimeError("#18572 candidate test is absent")
        package = _strict_json(_ordinary_file(tree / _PACKAGE, tree))
        dev = package.get("devDependencies") if isinstance(package, dict) else None
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if (
            not isinstance(dev, dict)
            or dev.get("jest") != "^29.2.0"
            or dev.get("typescript") != "~5.9.3"
            or not isinstance(scripts, dict)
            or scripts.get("test") != "jest"
        ):
            raise RuntimeError("#18572 codemirror test API/runtime declaration drifted")
        for relative in (_YARN, _JEST, _TSC, _JEST_CONFIG, _TSCONFIG):
            _ordinary_file(tree / relative, tree)
        for relative in _GENERATED_TEST_OUTPUTS:
            _ordinary_file(tree / relative, tree)
        hashes[variant] = {"production": source_hash, "test": test_hash}
    if hashes["fixed"]["test"] != hashes["buggy"]["test"]:
        raise RuntimeError("#18572 inverse changed the candidate test")
    return {
        "pr": 18572,
        "primary_issue": 17779,
        "superseded_unmerged_pr": 18285,
        "merge_commit_oid": pull["merge_commit_sha"],
        "base_oid": pull["base"]["sha"],
        "head_oid": pull["head"]["sha"],
        "source_hashes": hashes,
        "production_only_inverse": True,
        "identical_test_hash": _TEST_HASH,
        "authoritative_bug_count": 1,
    }


def _persist_static_chain(
    root: Path,
    evidence: dict[str, object],
    authorization: AuthorizationRecord,
) -> tuple[ArtifactReference, ...]:
    source, patch, atomicity, manifest, capsule, profile = _STATIC_CHAIN
    _write(
        root,
        source,
        (),
        {
            "candidate": _CANDIDATE,
            "complete": True,
            "state": "captured",
            "authoritative_pr": 18572,
            "primary_bug_issue": 17779,
            "superseded_unmerged_pr": 18285,
            "merge_commit_oid": evidence["merge_commit_oid"],
            "base_oid": evidence["base_oid"],
            "head_oid": evidence["head_oid"],
            "raw_capture_root": _RAW.as_posix(),
            "raw_capture_hashes": dict(_RAW_HASHES),
            "authoritative_bug_count": 1,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    _write(
        root,
        patch,
        (source,),
        {
            "candidate": _CANDIDATE,
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": [_PRODUCTION.as_posix()],
            "test_paths": [_TEST.as_posix()],
            "changed_file_count": 2,
            "f2p_selector": _F2P_SELECTOR,
            "p2p_selector": _P2P_SELECTOR,
            "identical_fixed_buggy_test_hash": evidence["identical_test_hash"],
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    _write(
        root,
        atomicity,
        (source, patch),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "outcome": "accepted",
            "authoritative_bug_count": 1,
            "primary_bug_issue": 17779,
            "superseded_unmerged_pr": 18285,
            "production_only_inverse": True,
            "source_hashes": evidence["source_hashes"],
            "test_hash_identical": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    _write(
        root,
        manifest,
        (patch, atomicity),
        {
            "candidate": _CANDIDATE,
            "complete": True,
            "stage": "test_manifest",
            "state": "passed",
            "details": {
                "executable_test_paths": [_TEST.as_posix()],
                "test_count": 1,
                "f2p_selector": _F2P_SELECTOR,
                "p2p_selector": _P2P_SELECTOR,
                "zero_tests_is_failure": True,
            },
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    _write(
        root,
        capsule,
        (source, patch, atomicity),
        {
            "candidate": _CANDIDATE,
            "state": "materialized",
            "fixed_source_tree_path": (_TREES / "fixed").as_posix(),
            "buggy_source_tree_path": (_TREES / "buggy").as_posix(),
            "production_only_inverse": True,
            "source_hashes": evidence["source_hashes"],
            "identical_test_hash": evidence["identical_test_hash"],
            "required_generated_test_outputs": [
                value.as_posix() for value in _GENERATED_TEST_OUTPUTS
            ],
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    _write(
        root,
        profile,
        (manifest, capsule),
        {
            "candidate": _CANDIDATE,
            "state": "prepared",
            "profile_id": "jupyterlab-18572-node22-yarn3-jest29-ts5",
            "strict_gate_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
            },
            "required_versions": dict(_VERSIONS),
            "typescript_build_config": _TSCONFIG.as_posix(),
            "generated_test_outputs": [
                value.as_posix() for value in _GENERATED_TEST_OUTPUTS
            ],
            "test_runner": "jest-json-run-in-band-no-cache",
            "repetitions_per_cell": 3,
            "expected_attempt_count": 12,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    return _STATIC_CHAIN


def _validate_runtime_and_build(
    root: Path,
    *,
    variant: str,
    node: str,
    environment: dict[str, str],
    static_chain: tuple[ArtifactReference, ...],
    receipts: list[ArtifactReference],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> None:
    tree = root / _TREES / variant
    commands = (
        ("node-version", (node, "--version"), 30, _VERSIONS["node"]),
        ("yarn-version", (node, str(tree / _YARN), "--version"), 30, _VERSIONS["yarn"]),
        ("jest-version", (node, str(tree / _JEST), "--version"), 30, _VERSIONS["jest"]),
        ("typescript-version", (node, str(tree / _TSC), "--version"), 30, _VERSIONS["typescript"]),
        (
            "typescript-build",
            (
                node,
                str(tree / _TSC),
                "--build",
                str(tree / _TSCONFIG),
                "--pretty",
                "false",
            ),
            900,
            None,
        ),
    )
    for step, argv, timeout, expected_stdout in commands:
        receipt = runner(
            argv, cwd=tree, env=dict(environment), timeout_seconds=timeout
        )
        reference = ArtifactReference(
            f"{_BUILD_KIND}/jupyterlab/18572/{variant}/{step}/current-v2",
            _BUILD_KIND,
            _VERSION,
        )
        payload = _receipt_payload(receipt)
        payload.update(
            {
                "candidate": _CANDIDATE,
                "fresh_epoch": _EPOCH,
                "variant": variant,
                "step": step,
                "argv": list(argv),
                "expected_stdout": expected_stdout,
                "old_attempts_loaded": False,
                "candidate_execution_performed": False,
                "paid_actions_authorized": False,
                "formal_trials_authorized": False,
                "gate8_13_performed": False,
            }
        )
        observed = receipt.stdout.decode("utf-8", "replace").strip()
        build_output_valid: bool | None = None
        if (
            step == "typescript-build"
            and receipt.outcome == "completed"
            and receipt.returncode == 0
        ):
            output = _ordinary_file(tree / _BUILD_OUTPUT, tree)
            output_hash = _sha256(output)
            output_text = output.read_text(encoding="utf-8")
            marker = (
                "pos === 0 && code.length > 0"
                if variant == "fixed"
                else "pos < tree.length - 1"
            )
            build_output_valid = (
                output_hash == _BUILD_HASHES[variant] and marker in output_text
            )
            payload.update(
                {
                    "compiled_output_path": _BUILD_OUTPUT.as_posix(),
                    "compiled_output_hash": output_hash,
                    "compiled_output_valid": build_output_valid,
                }
            )
        _write(
            root,
            reference,
            static_chain + tuple(receipts),
            payload,
            authorization,
        )
        receipts.append(reference)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#18572 {variant} {step} failed")
        if expected_stdout is not None and observed != expected_stdout:
            raise RuntimeError(
                f"#18572 {variant} {step} version drift: {observed!r}"
            )
        if step == "typescript-build" and build_output_valid is not True:
            raise RuntimeError(
                f"#18572 {variant} TypeScript build output hash or semantics drifted"
            )


def _run_cell(
    root: Path,
    *,
    cell: _Cell,
    node: str,
    environment: dict[str, str],
    static_chain: tuple[ArtifactReference, ...],
    build_references: tuple[ArtifactReference, ...],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / _TREES / cell.variant
    argv = (
        node,
        str(tree / _JEST),
        "--config",
        str(tree / _JEST_CONFIG),
        "--runTestsByPath",
        str(tree / _TEST),
        "--testNamePattern",
        cell.selector,
        "--runInBand",
        "--no-cache",
        "--json",
    )
    receipt = runner(
        argv,
        cwd=tree / "packages/codemirror",
        env=dict(environment),
        timeout_seconds=300,
    )
    observed, summary = _observed_jest(receipt, cell.selector)
    total = summary.get("numTotalTests")
    pending = summary.get("numPendingTests")
    executed = (
        total - pending
        if type(total) is int and type(pending) is int
        else None
    )
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/jupyterlab/18572/{cell.phase}/{cell.variant}/{cell.repetition}/current-v2",
        _ATTEMPT_KIND,
        _VERSION,
    )
    payload = _receipt_payload(receipt)
    payload.update(
        {
            "candidate": _CANDIDATE,
            "fresh_epoch": _EPOCH,
            "phase": cell.phase,
            "variant": cell.variant,
            "repetition": cell.repetition,
            "selector": cell.selector,
            "test_path": _TEST.as_posix(),
            "argv": list(argv),
            "expected_outcome": cell.expected,
            "observed_outcome": observed,
            "discovered_test_count": total,
            "pending_test_count": pending,
            "completed_test_count": executed,
            "passed_test_count": summary.get("numPassedTests"),
            "failed_test_count": summary.get("numFailedTests"),
            "matched_expected": observed == cell.expected,
            "clean_expected_failure": (
                cell.expected == "failure"
                and observed == "failure"
                and receipt.outcome == "completed"
                and receipt.returncode == 1
                and executed == 1
                and summary.get("numFailedTests") == 1
            ),
            "old_attempts_loaded": False,
            "fresh_receipt": True,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        }
    )
    _write(
        root,
        reference,
        static_chain + build_references,
        payload,
        authorization,
    )
    return _load(root, reference)


def _observed_jest(
    receipt: NativeProcessReceipt, selector: str
) -> tuple[str, dict[str, object]]:
    if receipt.outcome == "timed_out":
        return "timed_out", {}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {}
    if receipt.outcome != "completed" or type(receipt.returncode) is not int:
        return "unknown", {}
    try:
        value = json.loads(receipt.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_jest_result", {}
    if not isinstance(value, dict):
        return "invalid_jest_result", {}
    keys = (
        "numTotalTests",
        "numPassedTests",
        "numFailedTests",
        "numPendingTests",
        "numRuntimeErrorTestSuites",
    )
    if any(type(value.get(key)) is not int for key in keys):
        return "invalid_jest_result", value
    total = value["numTotalTests"]
    passed = value["numPassedTests"]
    failed = value["numFailedTests"]
    pending = value["numPendingTests"]
    runtime_errors = value["numRuntimeErrorTestSuites"]
    executed = total - pending
    if (
        total <= 0
        or pending < 0
        or executed != 1
        or passed + failed != executed
        or runtime_errors != 0
    ):
        return "invalid_test_count", value
    test_results = value.get("testResults")
    if not isinstance(test_results, list):
        return "invalid_jest_result", value
    assertions: list[dict[str, object]] = []
    for suite in test_results:
        if not isinstance(suite, dict) or not isinstance(
            suite.get("assertionResults"), list
        ):
            return "invalid_jest_result", value
        if suite.get("testExecError") not in (None, {}):
            return "invalid_jest_result", value
        for assertion in suite["assertionResults"]:
            if not isinstance(assertion, dict):
                return "invalid_jest_result", value
            assertions.append(assertion)
    if len(assertions) != total:
        return "invalid_jest_result", value
    active = [
        assertion
        for assertion in assertions
        if assertion.get("status") in {"passed", "failed"}
    ]
    inactive = [
        assertion
        for assertion in assertions
        if assertion.get("status") in {"pending", "skipped", "todo", "disabled"}
    ]
    expected_full_name = selector[1:-1]
    if (
        not selector.startswith("^")
        or not selector.endswith("$")
        or len(active) != 1
        or len(inactive) != pending
        or len(active) + len(inactive) != total
        or active[0].get("fullName") != expected_full_name
    ):
        return "invalid_jest_result", value
    status = active[0].get("status")
    if (
        status == "passed"
        and receipt.returncode == 0
        and passed == 1
        and failed == 0
        and value.get("success") is True
    ):
        return "pass", value
    if (
        status == "failed"
        and receipt.returncode == 1
        and passed == 0
        and failed == 1
        and value.get("success") is False
    ):
        return "failure", value
    return "invalid_jest_result", value


def _write_aggregate(
    root: Path,
    evidence: dict[str, object],
    static_chain: tuple[ArtifactReference, ...],
    receipts: tuple[ArtifactReference, ...],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise RuntimeError("#18572 Gate 5--7 attempt coverage is incomplete")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (str(attempt.payload["phase"]), str(attempt.payload["variant"]))
        groups.setdefault(key, []).append(attempt)
    expected_groups = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if set(groups) != expected_groups or any(len(value) != 3 for value in groups.values()):
        raise RuntimeError("#18572 Gate 5--7 repetition coverage is incomplete")
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    clean_buggy = all(
        item.payload["clean_expected_failure"] is True
        for item in groups[("f2p", "buggy")]
    )
    infrastructure_values = {
        "timed_out",
        "launch_failed",
        "unknown",
        "invalid_test_count",
        "invalid_jest_result",
    }
    infrastructure = any(
        item.payload["observed_outcome"] in infrastructure_values
        or item.payload["process_outcome"] != "completed"
        for item in attempts
    )
    qualified = stable and all_expected and clean_buggy and not infrastructure
    outcome = (
        "qualified"
        if qualified
        else "infrastructure_blocked"
        if infrastructure
        else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _CANDIDATE,
        "fresh_epoch": _EPOCH,
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "qualified": qualified,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome == "infrastructure_blocked",
        "gate1_4_evidence": evidence,
        "runtime_versions": dict(_VERSIONS),
        "attempt_count": 12,
        "expected_attempt_count": 12,
        "repetitions_per_cell": 3,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "buggy_f2p_clean_failure": clean_buggy,
        "no_timeout_unknown_or_invalid": not infrastructure,
        "old_attempts_loaded": False,
        "inherited_attempt_count": 0,
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    item.payload["observed_outcome"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: value.payload["repetition"],
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate8_13_performed": False,
    }
    inputs = static_chain + receipts + tuple(item.reference for item in attempts)
    return _write(root, _AGGREGATE, inputs, payload, authorization)


def _write_transition(
    root: Path,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload = {
        "candidate": _CANDIDATE,
        "fresh_epoch": _EPOCH,
        "state": "passed" if qualified else "blocked",
        "outcome": aggregate.payload["outcome"],
        "aggregate_reference": aggregate.reference.artifact_id,
        "strict_gate_1_7": qualified,
        "strict_no_timeout_unknown": aggregate.payload[
            "no_timeout_unknown_or_invalid"
        ],
        "lineage_inherited": False,
        "old_attempts_loaded": False,
        "gate_1_7_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_production_only_inverse": "passed",
            "test_api_runtime_build": "passed",
            "fixed_f2p_3x": "passed" if qualified else "failed",
            "buggy_f2p_3x": "passed_clean_failure" if qualified else "failed",
            "fixed_p2p_3x": "passed" if qualified else "failed",
            "buggy_p2p_3x": "passed" if qualified else "failed",
        },
        "deferred_until_after_gate1_7": [
            "gate8_full_versions",
            "gate9_start_replay_oracle_observations",
            "gate10_goldens",
            "gate11_visual_cross_judgment",
            "gate12_same_lineage_receipt",
            "gate13_freeze",
        ],
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate8_13_performed": False,
    }
    return _write(root, _TRANSITION, (aggregate.reference,), payload, authorization)


def _write_blocker(
    root: Path,
    evidence: dict[str, object],
    error: BaseException,
    *,
    static_chain: tuple[ArtifactReference, ...],
    receipts: tuple[ArtifactReference, ...],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "fresh_epoch": _EPOCH,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "candidate_specific_runtime_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "gate1_4_evidence": evidence,
        "attempt_count": len(attempts),
        "expected_attempt_count": 12,
        "old_attempts_loaded": False,
        "inherited_attempt_count": 0,
        "candidate_execution_performed": bool(attempts),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate8_13_performed": False,
        "next_action": "inspect fresh runtime/build receipts; never reuse partial attempts",
    }
    inputs = static_chain + receipts + tuple(item.reference for item in attempts)
    return _write(root, _BLOCKER, inputs, payload, authorization)


def _assert_fresh_namespace(root: Path) -> None:
    references = _STATIC_CHAIN + (_AGGREGATE, _TRANSITION, _BLOCKER)
    for reference in references:
        directory = root / "artifacts" / Path(reference.artifact_id) / _VERSION
        if directory.exists():
            raise RuntimeError(f"#18572 fresh artifact already exists: {reference.artifact_id}")
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        directory = root / "artifacts" / kind / "jupyterlab" / "18572"
        if directory.exists() and any(directory.rglob("*.json")):
            raise RuntimeError(f"#18572 fresh namespace is not empty: {kind}")


def _closed_environment(root: Path, node: str) -> dict[str, str]:
    home = root / "private/runtime-home/jupyterlab-18572-current-v2"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "CI": "1",
        "PATH": os.pathsep.join((str(Path(node).parent), "/usr/bin", "/bin")),
    }


def _node_executable() -> str:
    value = shutil.which("node")
    if value is None:
        raise RuntimeError("#18572 Node executable is unavailable")
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("#18572 Node executable is unsafe")
    return str(path)


def _receipt_payload(receipt: NativeProcessReceipt) -> dict[str, object]:
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    return {
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
    }


def _strict_json(path: Path) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid JSON capture: {path.name}") from error


def _ordinary_directory(path: Path, boundary: Path) -> Path:
    resolved_boundary = boundary.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != resolved_boundary and resolved_boundary not in resolved.parents:
        raise RuntimeError(f"path escapes candidate boundary: {path}")
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"candidate directory is missing or unsafe: {path}")
    return path


def _ordinary_file(path: Path, boundary: Path) -> Path:
    resolved_boundary = boundary.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved_boundary not in resolved.parents:
        raise RuntimeError(f"path escapes candidate boundary: {path}")
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError(f"candidate file is missing or unsafe: {path}")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#18572 Gate 1--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_jupyterlab_18572_gate57"]
