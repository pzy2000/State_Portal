"""Profile-driven strict Gate 1--7 adapter for modern JupyterLab candidates.

Every candidate keeps its own immutable profile: the authoritative records it must
match, the pinned capture and source fingerprints, the production/test boundary and
the two registered Jest selectors.  The shared runner validates Gate 1--4 from those
records and then executes exactly twelve fresh cells (fixed/buggy x F2P/P2P x three
repetitions) in a per-candidate artifact namespace.

The runner installs nothing, consults no historical attempt, performs no paid work,
runs no formal trial, and constructs no Gate 8--13 artifact.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Mapping

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_JUPYTERLAB_MODERN_FRESH_GATE57_V1"
_VERSION = "1.0"
_EPOCH = "current-v1"
_VERSIONS = {
    "node": "v22.22.0",
    "jest": "29.5.0",
    "typescript": "Version 5.9.3",
}
_MAX_OUTPUT = 262_144
# Jest compiles --testNamePattern as a JavaScript regular expression, so only
# JavaScript metacharacters may be escaped; Python's re.escape would also escape
# spaces and emit identity escapes that JavaScript rejects under unicode mode.
_JS_METACHARACTERS = re.compile(r"([.*+?^${}()|\[\]\\])")

ProcessRunner = Callable[..., NativeProcessReceipt]


def anchored_selector(full_name: str) -> str:
    """Anchor a Jest full name so exactly one registered test can match."""

    escaped = _JS_METACHARACTERS.sub(r"\\\1", full_name)
    return f"^{escaped}$"


@dataclass(frozen=True)
class ModernCandidateProfile:
    """Immutable Gate 1--4 evidence and Gate 5--7 plan for one candidate."""

    change_number: int
    issue_number: int
    fixed_revision: str
    base_revision: str
    package_directory: str
    production_paths: tuple[str, ...]
    test_path: str
    build_outputs: tuple[str, ...]
    f2p_full_name: str
    p2p_full_name: str
    f2p_test_name: str
    p2p_test_name: str
    profile_id: str
    classification: str
    issue_reproducer: str
    production_change: str
    raw_hashes: Mapping[str, str]
    source_hashes: Mapping[str, str]
    test_hash: str
    issue_title_markers: tuple[str, ...]
    issue_body_markers: tuple[str, ...]
    production_patch_markers: tuple[str, ...]
    test_patch_markers: tuple[str, ...]
    fixed_only_markers: tuple[str, ...]
    existing_api_used: tuple[str, ...]
    incidental_pr_numbers: tuple[int, ...] = ()
    defect_locus: Literal["at_goal", "on_path"] = "at_goal"
    pull_request_page_available: bool = True

    def __post_init__(self) -> None:
        if (
            self.change_number <= 0
            or self.issue_number <= 0
            or not self.production_paths
            or not self.fixed_only_markers
            or set(self.source_hashes) != {"fixed", "buggy"}
            or self.f2p_full_name == self.p2p_full_name
        ):
            raise ValueError("modern JupyterLab candidate profile is invalid")

    @property
    def candidate(self) -> dict[str, object]:
        return {"repository_key": "jupyterlab", "change_number": self.change_number}

    @property
    def raw_root(self) -> PurePosixPath:
        return PurePosixPath(f"raw/github/jupyterlab/{self.change_number}/{_EPOCH}")

    @property
    def trees(self) -> PurePosixPath:
        return PurePosixPath(
            f"private/materialized/jupyterlab/{self.change_number}/{_EPOCH}"
        )

    @property
    def runtime_home(self) -> PurePosixPath:
        return PurePosixPath(
            f"private/runtime-home/jupyterlab-{self.change_number}-gate57-v1"
        )

    @property
    def fresh_epoch(self) -> str:
        return f"candidate_gate57_jupyterlab_{self.change_number}_{_EPOCH}"

    @property
    def attempt_kind(self) -> str:
        return f"candidate_gate57_{self.change_number}_attempt_v1"

    @property
    def build_kind(self) -> str:
        return f"candidate_gate57_{self.change_number}_runtime_build_v1"

    def reference(self, kind: str, suffix: str) -> ArtifactReference:
        return ArtifactReference(
            f"{kind}/jupyterlab/{self.change_number}/{suffix}", kind, _VERSION
        )

    @property
    def static_chain(self) -> tuple[ArtifactReference, ...]:
        return (
            self.reference("candidate_source_packet", f"source/{_EPOCH}"),
            self.reference("candidate_test_patch", f"screening/{_EPOCH}"),
            self.reference("source_atomicity", f"preannotation/{_EPOCH}"),
            self.reference("candidate_stage", f"test-manifest/{_EPOCH}"),
            self.reference("fixed_source_capsule", f"fixed/{_EPOCH}"),
            self.reference("candidate_profile", f"gate1-4/{_EPOCH}"),
        )

    @property
    def aggregate(self) -> ArtifactReference:
        kind = f"candidate_gate57_{self.change_number}_aggregate"
        return self.reference(kind, _EPOCH)

    @property
    def transition(self) -> ArtifactReference:
        kind = f"candidate_gate57_{self.change_number}_transition"
        return self.reference(kind, _EPOCH)

    @property
    def pool(self) -> ArtifactReference:
        return self.reference("candidate_gate57_pool_admission", _EPOCH)

    @property
    def blocker(self) -> ArtifactReference:
        kind = f"candidate_gate57_{self.change_number}_blocker"
        return self.reference(kind, _EPOCH)


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    profile: ModernCandidateProfile = field(repr=False)

    @property
    def full_name(self) -> str:
        return (
            self.profile.f2p_full_name
            if self.phase == "f2p"
            else self.profile.p2p_full_name
        )

    @property
    def selector(self) -> str:
        return anchored_selector(self.full_name)

    @property
    def expected(self) -> str:
        if self.phase == "f2p" and self.variant == "buggy":
            return "failure"
        return "pass"


def execute_modern_gate57(
    artifact_root: str | Path,
    *,
    profile: ModernCandidateProfile,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
    process_runner: ProcessRunner | None = None,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Validate Gate 1--4, execute twelve fresh cells, and admit to the pool."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if type(profile) is not ModernCandidateProfile:
        raise ValueError("modern JupyterLab candidate profile is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    _assert_fresh_namespace(root, profile)
    evidence = validate_candidate(root, profile)
    static_chain = _persist_static_chain(root, profile, evidence, authorization)
    runner = process_runner or run_bounded_process
    receipts: list[ArtifactReference] = []
    attempts: list[StableArtifact] = []
    try:
        node = _node_executable()
        for variant in ("fixed", "buggy"):
            _validate_runtime_and_build(
                root,
                profile=profile,
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
                            cell=_Cell(phase, variant, repetition, profile),
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
            profile,
            evidence,
            error,
            static_chain=static_chain,
            receipts=tuple(receipts),
            attempts=attempts,
            authorization=authorization,
        )
        return blocker, blocker, blocker
    aggregate = _write_aggregate(
        root, profile, evidence, static_chain, tuple(receipts), attempts, authorization
    )
    transition = _write_transition(root, profile, aggregate, authorization)
    if transition.payload.get("strict_gate_1_7") is not True:
        return aggregate, transition, transition
    pool = _write_pool_admission(root, profile, transition, authorization)
    return aggregate, transition, pool


def validate_candidate(
    root: Path, profile: ModernCandidateProfile
) -> dict[str, object]:
    """Recompute Gate 1--4 from the pinned captures and materialized trees."""

    raw = root / profile.raw_root
    captures: dict[str, object] = {}
    for name, expected_hash in profile.raw_hashes.items():
        path = _ordinary_file(raw / name, root)
        if _sha256(path) != expected_hash:
            raise RuntimeError(
                f"#{profile.change_number} raw capture hash drifted: {name}"
            )
        if name.endswith(".json"):
            captures[name] = json.loads(path.read_text(encoding="utf-8"))

    commit = captures.get("commit.json")
    issue = captures.get(f"issue-{profile.issue_number}.json")
    timeline = captures.get("issue-timeline.json")
    if not isinstance(commit, dict) or (
        commit.get("sha") != profile.fixed_revision
        or not isinstance(commit.get("parents"), list)
        or [
            item.get("sha") for item in commit["parents"] if isinstance(item, dict)
        ]
        != [profile.base_revision]
    ):
        raise RuntimeError(
            f"#{profile.change_number} authoritative merged commit identity drifted"
        )
    message = str(
        commit.get("commit", {}).get("message", "")
        if isinstance(commit.get("commit"), dict)
        else ""
    )
    references = {int(value) for value in re.findall(r"#([0-9]+)", message)}
    allowed = {
        profile.issue_number,
        profile.change_number,
        *profile.incidental_pr_numbers,
    }
    if references != allowed or not re.search(
        rf"(?i)(?:fixes|closes|resolves)\s+#{profile.issue_number}\b", message
    ):
        raise RuntimeError(f"#{profile.change_number} single-defect mapping drifted")
    files = commit.get("files")
    expected_files = set(profile.production_paths) | {profile.test_path}
    if not isinstance(files, list) or len(files) != len(expected_files):
        raise RuntimeError(f"#{profile.change_number} changed-file set drifted")
    by_name = {
        item.get("filename"): item
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    }
    if set(by_name) != expected_files:
        raise RuntimeError(
            f"#{profile.change_number} production/test patch boundary drifted"
        )
    production_patch = "\n".join(
        str(by_name[name].get("patch", "")) for name in profile.production_paths
    )
    test_patch = str(by_name[profile.test_path].get("patch", ""))
    if any(marker not in production_patch for marker in profile.production_patch_markers):
        raise RuntimeError(f"#{profile.change_number} production patch semantics drifted")
    if any(marker not in test_patch for marker in profile.test_patch_markers):
        raise RuntimeError(f"#{profile.change_number} registered test closure drifted")

    if not isinstance(issue, dict):
        raise RuntimeError(f"#{profile.issue_number} capture is malformed")
    labels = {
        item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)
    }
    title = str(issue.get("title", ""))
    body = str(issue.get("body", ""))
    if (
        issue.get("number") != profile.issue_number
        or issue.get("pull_request") is not None
        or issue.get("state") != "closed"
        or "bug" not in labels
        or any(marker not in title for marker in profile.issue_title_markers)
        or any(marker not in body for marker in profile.issue_body_markers)
    ):
        raise RuntimeError(
            f"#{profile.issue_number} is not the captured authoritative bug"
        )
    if not isinstance(timeline, list) or not any(
        isinstance(item, dict)
        and item.get("event") == "referenced"
        and item.get("commit_id") == profile.fixed_revision
        for item in timeline
    ):
        raise RuntimeError(
            f"#{profile.issue_number} closing timeline does not cite the fix commit"
        )

    hashes: dict[str, dict[str, str]] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_directory(root / profile.trees / variant, root)
        production_text = ""
        for relative in profile.production_paths:
            source = _ordinary_file(tree / PurePosixPath(relative), tree)
            production_text += source.read_text(encoding="utf-8")
        production_hash = _sha256_many(
            tree / PurePosixPath(relative) for relative in profile.production_paths
        )
        test = _ordinary_file(tree / PurePosixPath(profile.test_path), tree)
        test_hash = _sha256(test)
        if (
            production_hash != profile.source_hashes[variant]
            or test_hash != profile.test_hash
        ):
            raise RuntimeError(
                f"#{profile.change_number} {variant} materialized source hash drifted"
            )
        guarded = all(
            marker in production_text for marker in profile.fixed_only_markers
        )
        if guarded is not (variant == "fixed"):
            raise RuntimeError(
                f"#{profile.change_number} {variant} production inverse drifted"
            )
        test_text = test.read_text(encoding="utf-8")
        for test_name in (profile.f2p_test_name, profile.p2p_test_name):
            if test_name not in test_text:
                raise RuntimeError(
                    f"#{profile.change_number} {variant} registered selectors are absent"
                )
        for relative in ("node_modules/jest/bin/jest.js", "node_modules/typescript/bin/tsc"):
            _ordinary_file(tree / PurePosixPath(relative), tree)
        _ordinary_file(tree / f"{profile.package_directory}/package.json", tree)
        hashes[variant] = {"production": production_hash, "test": test_hash}
    if hashes["fixed"]["test"] != hashes["buggy"]["test"]:
        raise RuntimeError(f"#{profile.change_number} inverse changed the candidate test")
    return {
        "pr": profile.change_number,
        "pr_page_available": profile.pull_request_page_available,
        "primary_issue": profile.issue_number,
        "issue_labels": sorted(value for value in labels if isinstance(value, str)),
        "incidental_pr_numbers": list(profile.incidental_pr_numbers),
        "fixed_revision": profile.fixed_revision,
        "base_revision": profile.base_revision,
        "source_hashes": hashes,
        "production_only_inverse": True,
        "identical_test_hash": profile.test_hash,
        "authoritative_bug_count": 1,
    }


def _persist_static_chain(
    root: Path,
    profile: ModernCandidateProfile,
    evidence: dict[str, object],
    authorization: AuthorizationRecord,
) -> tuple[ArtifactReference, ...]:
    source, patch, atomicity, manifest, capsule, gate_profile = profile.static_chain
    _write(
        root,
        source,
        (),
        {
            "candidate": profile.candidate,
            "complete": True,
            "state": "captured",
            "authority": "github:jupyterlab/jupyterlab",
            "authoritative_pr": profile.change_number,
            "authoritative_pr_page_available": profile.pull_request_page_available,
            "primary_bug_issue": profile.issue_number,
            "issue_labels": evidence["issue_labels"],
            "incidental_pr_numbers": list(profile.incidental_pr_numbers),
            "fixed_revision": profile.fixed_revision,
            "base_revision": profile.base_revision,
            "raw_capture_root": profile.raw_root.as_posix(),
            "raw_capture_hashes": dict(profile.raw_hashes),
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
            "candidate": profile.candidate,
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(profile.production_paths),
            "test_paths": [profile.test_path],
            "changed_file_count": len(profile.production_paths) + 1,
            "production_change": profile.production_change,
            "f2p_full_name": profile.f2p_full_name,
            "p2p_full_name": profile.p2p_full_name,
            "identical_fixed_buggy_test_hash": evidence["identical_test_hash"],
            "production_only_inverse": True,
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
            "candidate": profile.candidate,
            "state": "passed",
            "outcome": "accepted",
            "classification": profile.classification,
            "authoritative_bug_count": 1,
            "primary_bug_issue": profile.issue_number,
            "incidental_pr_numbers": list(profile.incidental_pr_numbers),
            "issue_reproducer": profile.issue_reproducer,
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
            "candidate": profile.candidate,
            "complete": True,
            "stage": "test_manifest",
            "state": "passed",
            "details": {
                "executable_test_paths": [profile.test_path],
                "runner": "jest_node22",
                "test_count": 2,
                "f2p_full_name": profile.f2p_full_name,
                "p2p_full_name": profile.p2p_full_name,
                "f2p_test_name": profile.f2p_test_name,
                "p2p_test_name": profile.p2p_test_name,
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
            "candidate": profile.candidate,
            "state": "materialized",
            "source_revision": profile.fixed_revision,
            "fixed_source_tree_path": (profile.trees / "fixed").as_posix(),
            "buggy_source_tree_path": (profile.trees / "buggy").as_posix(),
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
        gate_profile,
        (manifest, capsule),
        {
            "candidate": profile.candidate,
            "state": "prepared",
            "profile_id": profile.profile_id,
            "defect_locus": profile.defect_locus,
            "strict_gate_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
            },
            "buggy_api_surface_unchanged": True,
            "fixed_only_identifiers": [],
            "existing_api_used": list(profile.existing_api_used),
            "required_versions": dict(_VERSIONS),
            "typescript_build_configs": ["tsconfig.json", "tsconfig.test.json"],
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
    return profile.static_chain


def _validate_runtime_and_build(
    root: Path,
    *,
    profile: ModernCandidateProfile,
    variant: str,
    node: str,
    static_chain: tuple[ArtifactReference, ...],
    receipts: list[ArtifactReference],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> None:
    tree = root / profile.trees / variant
    package = tree / profile.package_directory
    jest = tree / "node_modules/jest/bin/jest.js"
    tsc = tree / "node_modules/typescript/bin/tsc"
    commands = (
        ("node-version", (node, "--version"), 30, _VERSIONS["node"], tree),
        ("jest-version", (node, str(jest), "--version"), 60, _VERSIONS["jest"], tree),
        (
            "typescript-version",
            (node, str(tsc), "--version"),
            60,
            _VERSIONS["typescript"],
            tree,
        ),
        (
            "typescript-build",
            (
                node,
                str(tsc),
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
            env=_closed_environment(root, profile, variant, step),
            timeout_seconds=timeout,
        )
        reference = ArtifactReference(
            f"{profile.build_kind}/jupyterlab/{profile.change_number}"
            f"/{variant}/{step}/{_EPOCH}",
            profile.build_kind,
            _VERSION,
        )
        payload = _receipt_payload(receipt)
        payload.update(
            {
                "candidate": profile.candidate,
                "fresh_epoch": profile.fresh_epoch,
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
        build_valid: bool | None = None
        if (
            step == "typescript-build"
            and receipt.outcome == "completed"
            and receipt.returncode == 0
        ):
            compiled = ""
            for relative in profile.build_outputs:
                output = _ordinary_file(tree / PurePosixPath(relative), tree)
                compiled += output.read_text(encoding="utf-8")
            guarded = all(
                marker in compiled for marker in profile.fixed_only_markers
            )
            build_valid = guarded is (variant == "fixed")
            payload.update(
                {
                    "compiled_output_paths": list(profile.build_outputs),
                    "compiled_output_fingerprint": _sha256_many(
                        tree / PurePosixPath(relative)
                        for relative in profile.build_outputs
                    ),
                    "compiled_output_contains_fix": guarded,
                    "compiled_output_valid": build_valid,
                }
            )
        _write(root, reference, static_chain + tuple(receipts), payload, authorization)
        receipts.append(reference)
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#{profile.change_number} {variant} {step} failed")
        if expected_stdout is not None and observed != expected_stdout:
            raise RuntimeError(
                f"#{profile.change_number} {variant} {step} version drift: {observed!r}"
            )
        if step == "typescript-build" and build_valid is not True:
            raise RuntimeError(
                f"#{profile.change_number} {variant} compiled output semantics drifted"
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
    profile = cell.profile
    tree = root / profile.trees / cell.variant
    home = root / profile.runtime_home / cell.phase / cell.variant / str(cell.repetition)
    # Some packages boot a Jupyter Server in their Jest setup and log to stdout,
    # so the machine-readable result is always taken from --outputFile.
    result_path = home / "jest-result.json"
    argv = (
        node,
        str(tree / "node_modules/jest/bin/jest.js"),
        "--config",
        str(tree / profile.package_directory / "jest.config.js"),
        "--runTestsByPath",
        str(tree / profile.test_path),
        "--testNamePattern",
        cell.selector,
        "--runInBand",
        "--no-cache",
        "--cacheDirectory",
        str(home / "jest-cache"),
        "--json",
        "--outputFile",
        str(result_path),
    )
    receipt = runner(
        argv,
        cwd=tree / profile.package_directory,
        env=_closed_environment(
            root, profile, cell.variant, f"{cell.phase}-{cell.repetition}", home=home
        ),
        timeout_seconds=300,
    )
    observed, summary = observed_jest(
        receipt, cell.full_name, result_path=result_path
    )
    total = summary.get("numTotalTests")
    pending = summary.get("numPendingTests")
    executed = total - pending if type(total) is int and type(pending) is int else None
    reference = ArtifactReference(
        f"{profile.attempt_kind}/jupyterlab/{profile.change_number}"
        f"/{cell.phase}/{cell.variant}/{cell.repetition}/{_EPOCH}",
        profile.attempt_kind,
        _VERSION,
    )
    payload = _receipt_payload(receipt)
    payload.update(
        {
            "candidate": profile.candidate,
            "fresh_epoch": profile.fresh_epoch,
            "phase": cell.phase,
            "variant": cell.variant,
            "repetition": cell.repetition,
            "selector": cell.selector,
            "full_name": cell.full_name,
            "test_path": profile.test_path,
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


def observed_jest(
    receipt: NativeProcessReceipt,
    full_name: str,
    *,
    result_path: Path | None = None,
) -> tuple[str, dict[str, object]]:
    """Classify one Jest cell, treating anything unclear as infrastructure."""

    if receipt.outcome == "timed_out":
        return "timed_out", {}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {}
    if receipt.outcome != "completed" or type(receipt.returncode) is not int:
        return "unknown", {}
    encoded = (
        result_path.read_bytes()
        if result_path is not None and result_path.is_file()
        else receipt.stdout
    )
    try:
        value = json.loads(encoded.decode("utf-8"))
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
    profile: ModernCandidateProfile,
    evidence: dict[str, object],
    static_chain: tuple[ArtifactReference, ...],
    receipts: tuple[ArtifactReference, ...],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise RuntimeError(
            f"#{profile.change_number} Gate 5--7 attempt coverage is incomplete"
        )
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (str(attempt.payload["phase"]), str(attempt.payload["variant"]))
        groups.setdefault(key, []).append(attempt)
    cells = (("f2p", "fixed"), ("f2p", "buggy"), ("p2p", "fixed"), ("p2p", "buggy"))
    if set(groups) != set(cells) or any(len(value) != 3 for value in groups.values()):
        raise RuntimeError(
            f"#{profile.change_number} Gate 5--7 repetition coverage is incomplete"
        )
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
    _write(
        root,
        profile.aggregate,
        static_chain + receipts + tuple(item.reference for item in attempts),
        {
            "candidate": profile.candidate,
            "fresh_epoch": profile.fresh_epoch,
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
                        for item in groups[(phase, variant)]
                    ],
                    "expected_outcome": groups[(phase, variant)][0].payload[
                        "expected_outcome"
                    ],
                }
                for phase, variant in cells
            ],
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        authorization,
    )
    return _load(root, profile.aggregate)


def _write_transition(
    root: Path,
    profile: ModernCandidateProfile,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload.get("qualified") is True
    _write(
        root,
        profile.transition,
        (aggregate.reference,),
        {
            "candidate": profile.candidate,
            "fresh_epoch": profile.fresh_epoch,
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
    return _load(root, profile.transition)


def _write_pool_admission(
    root: Path,
    profile: ModernCandidateProfile,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if transition.payload.get("strict_gate_1_7") is not True:
        raise RuntimeError(f"#{profile.change_number} strict Gate 1--7 did not pass")
    _write(
        root,
        profile.pool,
        (transition.reference,),
        {
            "candidate": profile.candidate,
            "fresh_epoch": profile.fresh_epoch,
            "state": "active_qualified",
            "outcome": "strict_gate1_7_pool_admission_passed",
            "complete": True,
            "defect_locus": profile.defect_locus,
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
    return _load(root, profile.pool)


def _write_blocker(
    root: Path,
    profile: ModernCandidateProfile,
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
        profile.blocker,
        static_chain + receipts + tuple(item.reference for item in attempts),
        {
            "candidate": profile.candidate,
            "fresh_epoch": profile.fresh_epoch,
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
    return _load(root, profile.blocker)


def _assert_fresh_namespace(root: Path, profile: ModernCandidateProfile) -> None:
    for reference in (
        profile.aggregate,
        profile.transition,
        profile.pool,
        profile.blocker,
    ):
        if (root / "artifacts" / reference.artifact_id).exists():
            raise RuntimeError(
                f"#{profile.change_number} fresh namespace is not empty: "
                f"{reference.artifact_id}"
            )
    for kind in (profile.attempt_kind, profile.build_kind):
        if (root / "artifacts" / kind).exists():
            raise RuntimeError(
                f"#{profile.change_number} fresh namespace is not empty: {kind}"
            )


def _closed_environment(
    root: Path,
    profile: ModernCandidateProfile,
    variant: str,
    step: str,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    directory = home or root / profile.runtime_home / "steps" / variant / step
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
        "stdout_truncated": receipt.stdout_truncated
        or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated
        or len(receipt.stderr) > _MAX_OUTPUT,
    }


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("authorization is inactive for fresh Gate 5--7 execution")


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(
            f"required directory is missing or unsafe: {path}"
        ) from error
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


def _sha256_many(paths) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(_sha256(Path(path)).encode("ascii"))
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


__all__ = [
    "CONFIRMATION",
    "ModernCandidateProfile",
    "anchored_selector",
    "execute_modern_gate57",
    "observed_jest",
    "validate_candidate",
]
