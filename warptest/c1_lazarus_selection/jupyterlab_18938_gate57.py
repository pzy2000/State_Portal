"""Fresh Gate 1--7 adapter for JupyterLab #18938.

The authoritative change is the squash-merged commit for pull request #18938,
which closed issue #12863 ("Dialog hangs if IBodyWidget<T>.getValue() throws an
error").  GitHub no longer serves the pull-request page for #18938, so Gate 1
authority is established from three accessible records: the merged commit on the
default branch, the authoritative issue together with its closing timeline, and
the merged 4.5.x backport pull request #18950 carrying the identical two-file
change.  The recorded 404 response stays in the raw capture so the substitution
is auditable.

A successful invocation validates Gate 1--4 and then executes exactly twelve
fresh Jest cells (fixed/buggy x F2P/P2P x three repetitions) in a new artifact
namespace.  It installs nothing, consults no historical attempt, performs no paid
work, runs no formal trial, and constructs no Gate 8--13 artifact.
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


CONFIRMATION = "EXECUTE_JUPYTERLAB_18938_FRESH_GATE57_V1"
_VERSION = "1.0"
_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 18938}
_EPOCH = "candidate_gate57_jupyterlab_18938_current_v1"
_ISSUE = 12863
_BACKPORT_PULL = 18950
_COMPETING_PULL = 18345
_FIXED_REVISION = "222e68b0a7543b93952e3dd47ecedf994b155b53"
_BASE_REVISION = "39f4120ca0a7af5fbd3760296bd9828d66d2b225"
_RAW = Path("raw/github/jupyterlab/18938/current-v1")
_TREES = Path("private/materialized/jupyterlab/18938/current-v1")
_RUNTIME_HOME = Path("private/runtime-home/jupyterlab-18938-gate57-v1")
_PRODUCTION = Path("packages/apputils/src/dialog.tsx")
_TEST = Path("packages/apputils/test/dialog.spec.tsx")
_BUILD_OUTPUT = Path("packages/apputils/lib/dialog.js")
_PACKAGE_DIRECTORY = Path("packages/apputils")
_PACKAGE = Path("packages/apputils/package.json")
_JEST = Path("node_modules/jest/bin/jest.js")
_TSC = Path("node_modules/typescript/bin/tsc")
_JEST_CONFIG = Path("packages/apputils/jest.config.js")
_TSCONFIG = Path("packages/apputils/tsconfig.json")
_TSCONFIG_TEST = Path("packages/apputils/tsconfig.test.json")

_RAW_HASHES = {
    "commit.json": "a415c71f8cc0cca2ba987fe48f1d817faaf94d20362ca775c8dac4d1722f4555",
    "commit.diff": "617d4295c9ad3631b77801be3a20f7c08d2a0bffa0d1590324230777267927be",
    "issue-12863.json": (
        "799a3b4b5993f0ae189c20ba1f663bd7165a6a1a6208b87480ddc13c729a8cb9"
    ),
    "issue-comments.json": (
        "290fd5bc16022d006f7e51e248db206eaf11746e516f3ba3182fdb0d2f91d278"
    ),
    "issue-timeline.json": (
        "4cca14ddbb3010587ede78089cd1e9cc8358802693b87346141cdb0d564b74da"
    ),
    "backport-pull-18950.json": (
        "0324903989c59f1e1d1fa96a8e3388f59abe3e273339ef1040a5590279c2a3c1"
    ),
    "backport-files.json": (
        "13d0998cc8f00d0c071717e6dc5d23a890cc70038634cd03dadb4cf66df3d2b2"
    ),
    "pull-18938-response.json": (
        "7fe148a00b708ac81b9b6182eb8d263af7730beb8ed1cc56c08698ebaed076a6"
    ),
}
_SOURCE_HASHES = {
    "fixed": "ecbec995500ae81313a6e4ee73c61892c8bd85bcede2b38c47a98c8df1ad6a37",
    "buggy": "04b9258413bc686c51b34222d393893ab3c15e664fb900806a22cabf31308b8b",
}
_TEST_HASH = "bb17af1190498284eb3b0c7e610f5fd88d8a6fe401cae06fc4b2b8d1f9540b2d"
_VERSIONS = {
    "node": "v22.22.0",
    "jest": "29.5.0",
    "typescript": "Version 5.9.3",
}
_F2P_FULL_NAME = (
    "@jupyterlab/apputils showDialog() "
    "should reject the promise when getValue() throws"
)
_P2P_FULL_NAME = "@jupyterlab/apputils showDialog() should give the value from the widget"
_BUILD_KIND = "candidate_gate57_18938_runtime_build_v1"
_ATTEMPT_KIND = "candidate_gate57_18938_attempt_v1"
_STATIC_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/jupyterlab/18938/source/current-v1",
        "candidate_source_packet",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_test_patch/jupyterlab/18938/screening/current-v1",
        "candidate_test_patch",
        _VERSION,
    ),
    ArtifactReference(
        "source_atomicity/jupyterlab/18938/preannotation/current-v1",
        "source_atomicity",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_stage/jupyterlab/18938/test-manifest/current-v1",
        "candidate_stage",
        _VERSION,
    ),
    ArtifactReference(
        "fixed_source_capsule/jupyterlab/18938/fixed/current-v1",
        "fixed_source_capsule",
        _VERSION,
    ),
    ArtifactReference(
        "candidate_profile/jupyterlab/18938/gate1-4/current-v1",
        "candidate_profile",
        _VERSION,
    ),
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_18938_aggregate/jupyterlab/18938/current-v1",
    "candidate_gate57_18938_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_18938_transition/jupyterlab/18938/current-v1",
    "candidate_gate57_18938_transition",
    _VERSION,
)
_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18938/current-v1",
    "candidate_gate57_pool_admission",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_18938_blocker/jupyterlab/18938/current-v1",
    "candidate_gate57_18938_blocker",
    _VERSION,
)
_MAX_OUTPUT = 262_144


ProcessRunner = Callable[..., NativeProcessReceipt]
# Jest compiles --testNamePattern as a JavaScript regular expression, so only
# JavaScript metacharacters may be escaped; Python's re.escape would also escape
# spaces and produce identity escapes that JavaScript rejects under unicode mode.
_JS_METACHARACTERS = re.compile(r"([.*+?^${}()|\[\]\\])")


def _anchored_selector(full_name: str) -> str:
    escaped = _JS_METACHARACTERS.sub(r"\\\1", full_name)
    return f"^{escaped}$"


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int

    @property
    def full_name(self) -> str:
        return _F2P_FULL_NAME if self.phase == "f2p" else _P2P_FULL_NAME

    @property
    def selector(self) -> str:
        return _anchored_selector(self.full_name)

    @property
    def expected(self) -> str:
        if self.phase == "f2p" and self.variant == "buggy":
            return "failure"
        return "pass"


def execute_jupyterlab_18938_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
    process_runner: ProcessRunner | None = None,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Validate Gate 1--4, execute twelve fresh cells, and admit to the pool."""

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
        for variant in ("fixed", "buggy"):
            _validate_runtime_and_build(
                root,
                variant=variant,
                node=node,
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
        return blocker, blocker, blocker
    aggregate = _write_aggregate(
        root, evidence, static_chain, tuple(receipts), attempts, authorization
    )
    transition = _write_transition(root, aggregate, authorization)
    if transition.payload.get("strict_gate_1_7") is not True:
        return aggregate, transition, transition
    pool = _write_pool_admission(root, transition, authorization)
    return aggregate, transition, pool


def _validate_candidate(root: Path) -> dict[str, object]:
    raw = root / _RAW
    captures: dict[str, object] = {}
    for name, expected_hash in _RAW_HASHES.items():
        path = _ordinary_file(raw / name, root)
        if _sha256(path) != expected_hash:
            raise RuntimeError(f"#18938 current-v1 raw capture hash drifted: {name}")
        if name.endswith(".json"):
            captures[name] = _strict_json(path)

    commit = captures["commit.json"]
    issue = captures[f"issue-{_ISSUE}.json"]
    timeline = captures["issue-timeline.json"]
    backport = captures[f"backport-pull-{_BACKPORT_PULL}.json"]
    backport_files = captures["backport-files.json"]
    unavailable = captures["pull-18938-response.json"]

    if not isinstance(commit, dict) or (
        commit.get("sha") != _FIXED_REVISION
        or not isinstance(commit.get("parents"), list)
        or [
            item.get("sha")
            for item in commit["parents"]
            if isinstance(item, dict)
        ]
        != [_BASE_REVISION]
    ):
        raise RuntimeError("#18938 authoritative merged commit identity drifted")
    message = str(
        commit.get("commit", {}).get("message", "")
        if isinstance(commit.get("commit"), dict)
        else ""
    )
    references = {int(value) for value in re.findall(r"#([0-9]+)", message)}
    if references != {_ISSUE, _COMPETING_PULL, 18938} or not re.search(
        rf"(?i)fixes\s+#{_ISSUE}\b", message
    ):
        raise RuntimeError("#18938 single-defect mapping drifted")
    files = commit.get("files")
    if not isinstance(files, list) or len(files) != 2:
        raise RuntimeError("#18938 changed-file set drifted")
    by_name = {
        item.get("filename"): item
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    }
    if set(by_name) != {_PRODUCTION.as_posix(), _TEST.as_posix()}:
        raise RuntimeError("#18938 production/test patch boundary drifted")
    production_patch = str(by_name[_PRODUCTION.as_posix()].get("patch", ""))
    test_patch = str(by_name[_TEST.as_posix()].get("patch", ""))
    if (
        "value = body.getValue();" not in production_patch
        or "catch (err)" not in production_patch
        or "promise.reject(err)" not in production_patch
        or "ThrowingValueWidget" not in test_patch
        or "should reject the promise when getValue() throws" not in test_patch
    ):
        raise RuntimeError("#18938 captured patch semantics drifted")

    if not isinstance(issue, dict):
        raise RuntimeError(f"#{_ISSUE} capture is malformed")
    labels = {
        item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)
    }
    if (
        issue.get("number") != _ISSUE
        or issue.get("pull_request") is not None
        or issue.get("state") != "closed"
        or "bug" not in labels
        or "getValue() throws" not in str(issue.get("title", ""))
        or "the dialog hangs and cannot be resolved or rejected"
        not in str(issue.get("body", ""))
    ):
        raise RuntimeError(f"#{_ISSUE} is not the captured authoritative bug")
    if not isinstance(timeline, list) or not any(
        isinstance(item, dict)
        and item.get("event") == "referenced"
        and item.get("commit_id") == _FIXED_REVISION
        for item in timeline
    ):
        raise RuntimeError(f"#{_ISSUE} closing timeline does not cite the fix commit")
    if not isinstance(backport, dict) or (
        backport.get("number") != _BACKPORT_PULL
        or backport.get("merged") is not True
        or not isinstance(backport.get("base"), dict)
        or backport["base"].get("ref") != "4.5.x"
        or "18938" not in str(backport.get("title", ""))
    ):
        raise RuntimeError(f"#{_BACKPORT_PULL} backport corroboration drifted")
    if not isinstance(backport_files, list) or {
        item.get("filename") for item in backport_files if isinstance(item, dict)
    } != {_PRODUCTION.as_posix(), _TEST.as_posix()}:
        raise RuntimeError(f"#{_BACKPORT_PULL} backport file boundary drifted")
    if not isinstance(unavailable, dict) or unavailable.get("status") != "404":
        raise RuntimeError("#18938 recorded pull-request response is not the 404 capture")

    hashes: dict[str, dict[str, str]] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_directory(root / _TREES / variant, root)
        source = _ordinary_file(tree / _PRODUCTION, tree)
        test = _ordinary_file(tree / _TEST, tree)
        source_hash = _sha256(source)
        test_hash = _sha256(test)
        if source_hash != _SOURCE_HASHES[variant] or test_hash != _TEST_HASH:
            raise RuntimeError(f"#18938 {variant} materialized source hash drifted")
        source_text = source.read_text(encoding="utf-8")
        guarded = "catch (err) {" in source_text and "promise.reject(err);" in source_text
        if variant == "fixed" and not guarded:
            raise RuntimeError("#18938 fixed production source is not the merged fix")
        if variant == "buggy" and guarded:
            raise RuntimeError("#18938 buggy production-only inverse drifted")
        if "value = body.getValue();" not in source_text:
            raise RuntimeError(f"#18938 {variant} production call site is absent")
        test_text = test.read_text(encoding="utf-8")
        if (
            "should reject the promise when getValue() throws" not in test_text
            or "should give the value from the widget" not in test_text
        ):
            raise RuntimeError(f"#18938 {variant} registered selectors are absent")
        package = _strict_json(_ordinary_file(tree / _PACKAGE, tree))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict) or scripts.get("test") != "jest -i":
            raise RuntimeError("#18938 apputils test declaration drifted")
        for relative in (_JEST, _TSC, _JEST_CONFIG, _TSCONFIG, _TSCONFIG_TEST):
            _ordinary_file(tree / relative, tree)
        hashes[variant] = {"production": source_hash, "test": test_hash}
    if hashes["fixed"]["test"] != hashes["buggy"]["test"]:
        raise RuntimeError("#18938 inverse changed the candidate test")
    return {
        "pr": 18938,
        "pr_page_available": False,
        "pr_page_response": "404",
        "authority_evidence": [
            "merged_commit_on_default_branch",
            "authoritative_issue_and_closing_timeline",
            "merged_backport_pull_request",
        ],
        "backport_pr": _BACKPORT_PULL,
        "competing_unmerged_pr": _COMPETING_PULL,
        "primary_issue": _ISSUE,
        "issue_labels": sorted(value for value in labels if isinstance(value, str)),
        "fixed_revision": _FIXED_REVISION,
        "base_revision": _BASE_REVISION,
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
            "authority": "github:jupyterlab/jupyterlab",
            "authoritative_pr": 18938,
            "authoritative_pr_page_available": False,
            "authoritative_pr_page_response": "404",
            "authority_evidence": evidence["authority_evidence"],
            "backport_pr": _BACKPORT_PULL,
            "competing_unmerged_pr": _COMPETING_PULL,
            "primary_bug_issue": _ISSUE,
            "issue_labels": evidence["issue_labels"],
            "fixed_revision": _FIXED_REVISION,
            "base_revision": _BASE_REVISION,
            "raw_capture_root": _RAW.as_posix(),
            "raw_capture_hashes": dict(_RAW_HASHES),
            "authoritative_bug_count": 1,
            "network_read_only": True,
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
            "production_hunks": 1,
            "production_change": (
                "guard body.getValue() so a throwing body disposes the dialog and "
                "rejects the pending promise instead of leaving it unsettled"
            ),
            "f2p_full_name": _F2P_FULL_NAME,
            "p2p_full_name": _P2P_FULL_NAME,
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
            "classification": "real_bug_hang",
            "authoritative_bug_count": 1,
            "primary_bug_issue": _ISSUE,
            "incidental_pr_numbers": [_COMPETING_PULL, _BACKPORT_PULL],
            "issue_reproducer": (
                "a dialog body widget whose getValue() throws leaves the dialog "
                "undisposed and its promise unsettled"
            ),
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
                "runner": "jest_node22",
                "test_count": 2,
                "f2p_full_name": _F2P_FULL_NAME,
                "p2p_full_name": _P2P_FULL_NAME,
                "selector_form": "anchored_escaped_full_name",
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
            "source_revision": _FIXED_REVISION,
            "fixed_source_tree_path": (_TREES / "fixed").as_posix(),
            "buggy_source_tree_path": (_TREES / "buggy").as_posix(),
            "production_only_inverse": True,
            "source_hashes": evidence["source_hashes"],
            "identical_test_hash": evidence["identical_test_hash"],
            "node_modules_snapshot_per_variant": True,
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
            "profile_id": "jupyterlab-18938-node22-jest29-ts5-dialog-getvalue",
            "defect_locus": "at_goal",
            "strict_gate_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
            },
            "buggy_api_surface_unchanged": True,
            "fixed_only_identifiers": [],
            "existing_api_used": ["showDialog", "Dialog", "acceptDialog", "Widget"],
            "required_versions": dict(_VERSIONS),
            "typescript_build_configs": [
                _TSCONFIG.as_posix(),
                _TSCONFIG_TEST.as_posix(),
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
    static_chain: tuple[ArtifactReference, ...],
    receipts: list[ArtifactReference],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> None:
    tree = root / _TREES / variant
    package = tree / _PACKAGE_DIRECTORY
    commands = (
        ("node-version", (node, "--version"), 30, _VERSIONS["node"], tree),
        (
            "jest-version",
            (node, str(tree / _JEST), "--version"),
            60,
            _VERSIONS["jest"],
            tree,
        ),
        (
            "typescript-version",
            (node, str(tree / _TSC), "--version"),
            60,
            _VERSIONS["typescript"],
            tree,
        ),
        (
            "typescript-build",
            (
                node,
                str(tree / _TSC),
                "--build",
                "tsconfig.json",
                "tsconfig.test.json",
                "--force",
                "--pretty",
                "false",
            ),
            900,
            None,
            package,
        ),
    )
    for step, argv, timeout, expected_stdout, cwd in commands:
        receipt = runner(
            argv,
            cwd=cwd,
            env=_closed_environment(root, variant, step),
            timeout_seconds=timeout,
        )
        reference = ArtifactReference(
            f"{_BUILD_KIND}/jupyterlab/18938/{variant}/{step}/current-v1",
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
            output_text = output.read_text(encoding="utf-8")
            guarded = (
                "catch (err)" in output_text and "promise.reject(err)" in output_text
            )
            build_output_valid = guarded is (variant == "fixed")
            payload.update(
                {
                    "compiled_output_path": _BUILD_OUTPUT.as_posix(),
                    "compiled_output_hash": _sha256(output),
                    "compiled_output_guards_getvalue": guarded,
                    "compiled_output_valid": build_output_valid,
                }
            )
        _write(root, reference, static_chain + tuple(receipts), payload, authorization)
        receipts.append(reference)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#18938 {variant} {step} failed")
        if expected_stdout is not None and observed != expected_stdout:
            raise RuntimeError(f"#18938 {variant} {step} version drift: {observed!r}")
        if step == "typescript-build" and build_output_valid is not True:
            raise RuntimeError(
                f"#18938 {variant} TypeScript build output semantics drifted"
            )


def _run_cell(
    root: Path,
    *,
    cell: _Cell,
    node: str,
    static_chain: tuple[ArtifactReference, ...],
    build_references: tuple[ArtifactReference, ...],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / _TREES / cell.variant
    home = root / _RUNTIME_HOME / cell.phase / cell.variant / str(cell.repetition)
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
        "--cacheDirectory",
        str(home / "jest-cache"),
        "--json",
    )
    receipt = runner(
        argv,
        cwd=tree / _PACKAGE_DIRECTORY,
        env=_closed_environment(
            root, cell.variant, f"{cell.phase}-{cell.repetition}", home=home
        ),
        timeout_seconds=300,
    )
    observed, summary = _observed_jest(receipt, cell.full_name)
    total = summary.get("numTotalTests")
    pending = summary.get("numPendingTests")
    executed = (
        total - pending if type(total) is int and type(pending) is int else None
    )
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/jupyterlab/18938/{cell.phase}/{cell.variant}"
        f"/{cell.repetition}/current-v1",
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
            "full_name": cell.full_name,
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
    _write(root, reference, static_chain + build_references, payload, authorization)
    return _load(root, reference)


def _observed_jest(
    receipt: NativeProcessReceipt, full_name: str
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
    if (
        len(active) != 1
        or len(inactive) != pending
        or len(active) + len(inactive) != total
        or active[0].get("fullName") != full_name
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
        raise RuntimeError("#18938 Gate 5--7 attempt coverage is incomplete")
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
    if set(groups) != expected_groups or any(
        len(value) != 3 for value in groups.values()
    ):
        raise RuntimeError("#18938 Gate 5--7 repetition coverage is incomplete")
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
                    item.payload["observed_outcome"] for item in groups[(phase, variant)]
                ],
                "expected_outcome": groups[(phase, variant)][0].payload[
                    "expected_outcome"
                ],
            }
            for phase, variant in (
                ("f2p", "fixed"),
                ("f2p", "buggy"),
                ("p2p", "fixed"),
                ("p2p", "buggy"),
            )
        ],
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate8_13_performed": False,
    }
    _write(
        root,
        _AGGREGATE,
        static_chain + receipts + tuple(item.reference for item in attempts),
        payload,
        authorization,
    )
    return _load(root, _AGGREGATE)


def _write_transition(
    root: Path, aggregate: StableArtifact, authorization: AuthorizationRecord
) -> StableArtifact:
    qualified = aggregate.payload.get("qualified") is True
    _write(
        root,
        _TRANSITION,
        (aggregate.reference,),
        {
            "candidate": _CANDIDATE,
            "fresh_epoch": _EPOCH,
            "state": "passed" if qualified else "blocked",
            "outcome": aggregate.payload["outcome"],
            "complete": qualified,
            "terminal": aggregate.payload["terminal"],
            "resumable": aggregate.payload["resumable"],
            "strict_gate_1_7": qualified,
            "strict_no_timeout_unknown": aggregate.payload[
                "no_timeout_unknown_or_invalid"
            ],
            "lineage_inherited": False,
            "attempt_count": aggregate.payload["attempt_count"],
            "gate_1_7_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
                "fixed_f2p_3x": "passed" if qualified else "blocked",
                "buggy_f2p_3x": "passed_clean_failure" if qualified else "blocked",
                "fixed_buggy_p2p_3x": "passed" if qualified else "blocked",
            },
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    return _load(root, _TRANSITION)


def _write_pool_admission(
    root: Path, transition: StableArtifact, authorization: AuthorizationRecord
) -> StableArtifact:
    if transition.payload.get("strict_gate_1_7") is not True:
        raise RuntimeError("#18938 strict Gate 1--7 did not pass")
    _write(
        root,
        _POOL,
        (transition.reference,),
        {
            "candidate": _CANDIDATE,
            "fresh_epoch": _EPOCH,
            "state": "active_qualified",
            "outcome": "strict_gate1_7_pool_admission_passed",
            "complete": True,
            "defect_locus": "at_goal",
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "catalog_admission_ready": False,
            "gate8_13_performed": False,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return _load(root, _POOL)


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
    _write(
        root,
        _BLOCKER,
        static_chain + receipts + tuple(item.reference for item in attempts),
        {
            "candidate": _CANDIDATE,
            "fresh_epoch": _EPOCH,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "complete": False,
            "terminal": False,
            "resumable": True,
            "reason": f"{type(error).__name__}: {error}"[:512],
            "gate1_4_evidence": evidence,
            "attempt_count": len(attempts),
            "candidate_execution_performed": bool(attempts),
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    return _load(root, _BLOCKER)


def _assert_fresh_namespace(root: Path) -> None:
    for reference in (_AGGREGATE, _TRANSITION, _POOL, _BLOCKER):
        directory = root / "artifacts" / reference.artifact_id
        if directory.exists():
            raise RuntimeError(f"#18938 fresh namespace is not empty: {directory.name}")
    for kind in (_ATTEMPT_KIND, _BUILD_KIND):
        if (root / "artifacts" / kind).exists():
            raise RuntimeError(f"#18938 fresh namespace is not empty: {kind}")


def _closed_environment(
    root: Path, variant: str, step: str, *, home: Path | None = None
) -> dict[str, str]:
    directory = home or root / _RUNTIME_HOME / "steps" / variant / step
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(directory),
        "LANG": "C",
        "LC_ALL": "C",
        "CI": "true",
    }


def _node_executable() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable")
    return str(Path(node).resolve(strict=True))


def _receipt_payload(receipt: NativeProcessReceipt) -> dict[str, object]:
    return {
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
    }


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("authorization is inactive for fresh Gate 5--7 execution")


def _strict_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required directory is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"required directory is missing or unsafe: {path}")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required file is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"required file is missing or unsafe: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> None:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"artifact conflict: {reference.artifact_id}")
        return
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


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_jupyterlab_18938_gate57"]
