"""Candidate-specific Gate 3/4 static audits for Luanti #9187 and #11866.

This adapter consumes only the current-v2 typed source/patch/atomicity/manifest
chain plus the candidate-specific Gate 2 reclassification receipts.  It never
materializes a source tree, builds, executes a test, or starts a paid/formal
trial.  A local Luanti git mirror is used read-only to make the static API
claim stronger than a patch-text heuristic: the exact merged PR commit and its
parent are checked with ``git cat-file``/``git show``.

#9187 is a hard Gate 4 stop.  The frozen ``test_utilities.cpp`` test calls
``EnrichedString::setDefaultColor``, which is introduced by the PR and absent
from the exact buggy parent.  A production-only inverse therefore cannot
compile that frozen test.

#11866 passes the static Gate 3/4 checks.  Its three non-production sections
(``builtin/settingtypes.txt``, ``minetest.conf.example`` and the unit-test
CMake list) are explicitly accounted for as support paths; the new test uses
only symbols already visible in the buggy parent.  It remains resumable until
a candidate-specific profile/capsule and a fresh Gate 5--7 epoch are
registered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_OUTPUT_KIND = "candidate_gate34_static_audit"
_OUTPUT_VERSION = "1.0"
_MIRROR_RELATIVE = Path("games/luanti")


@dataclass(frozen=True)
class _Spec:
    number: int
    primary_issue: int
    linked_issues: tuple[int, ...]
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    support_paths: tuple[str, ...]
    gate2_reclassification: ArtifactReference
    expected_base: str
    expected_parent: str
    expected_merge: str
    # Test-side symbols which must be visible in the buggy parent.  These are
    # intentionally candidate-specific rather than guessed from identifiers.
    buggy_test_symbols: tuple[str, ...]
    expected_fixed_only_symbols: tuple[str, ...]

    @property
    def candidate(self) -> dict[str, object]:
        return {"repository_key": "luanti", "change_number": self.number}

    @property
    def source(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_source_packet/luanti/{self.number}/source/v2",
            "candidate_source_packet",
            "2.0",
        )

    @property
    def patch(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_test_patch/luanti/{self.number}/screening/v2",
            "candidate_test_patch",
            "2.0",
        )

    @property
    def checkpoint(self) -> ArtifactReference:
        return ArtifactReference(
            f"screening_checkpoint/luanti/{self.number}/current/v2",
            "screening_checkpoint",
            "2.0",
        )

    @property
    def atomicity(self) -> ArtifactReference:
        return ArtifactReference(
            f"source_atomicity/luanti/{self.number}/preannotation/v2",
            "source_atomicity",
            "2.0",
        )

    @property
    def manifest(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_stage/luanti/{self.number}/test_manifest/v2",
            "candidate_stage",
            "2.0",
        )

    @property
    def gate2(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate2_recomputed_admission/luanti/{self.number}/current-v2/v1",
            "candidate_gate2_recomputed_admission",
            "1.0",
        )

    @property
    def output(self) -> ArtifactReference:
        return ArtifactReference(
            f"{_OUTPUT_KIND}/luanti/{self.number}/current-v2/v1",
            _OUTPUT_KIND,
            _OUTPUT_VERSION,
        )


_SPECS = (
    _Spec(
        number=9187,
        primary_issue=9277,
        linked_issues=(9195, 9277),
        production_paths=(
            "src/client/gameui.cpp",
            "src/gui/guiButton.cpp",
            "src/gui/guiFormSpecMenu.cpp",
            "src/irrlicht_changes/static_text.cpp",
            "src/irrlicht_changes/static_text.h",
            "src/util/enriched_string.cpp",
            "src/util/enriched_string.h",
        ),
        test_paths=(
            "games/minimal/mods/test/formspec.lua",
            "src/unittest/test_utilities.cpp",
        ),
        support_paths=(),
        gate2_reclassification=ArtifactReference(
            "candidate_gate2_authority_reclassification/luanti/9187/current-v2/v1",
            "candidate_gate2_authority_reclassification",
            "1.0",
        ),
        expected_base="8e63d22d6472b61477f804dc5b00705650f90409",
        expected_parent="fab3f5f7c8ce0cbfc27e509f065e3f4cfe28c514",
        expected_merge="1892ff3c0db23ccdf7b0f6dc83cb1bdf4579b4ec",
        buggy_test_symbols=("EnrichedString::getBackground",),
        expected_fixed_only_symbols=("EnrichedString::setDefaultColor",),
    ),
    _Spec(
        number=11866,
        primary_issue=11839,
        linked_issues=(11600, 11839),
        production_paths=(
            "builtin/game/chat.lua",
            "src/constants.h",
            "src/defaultsettings.cpp",
            "src/map.cpp",
            "src/mapblock.h",
        ),
        test_paths=("src/unittest/test_map.cpp", "src/unittest/test_player.cpp"),
        support_paths=(
            "builtin/settingtypes.txt",
            "minetest.conf.example",
            "src/unittest/CMakeLists.txt",
        ),
        gate2_reclassification=ArtifactReference(
            "candidate_issue_classification/luanti/11866/preannotation/v1",
            "candidate_issue_classification",
            "1.0",
        ),
        expected_base="66e8aae9f2a28ee31ffe30694fdb61a8fdceb8d7",
        # The captured GitHub diff is the two-commit PR delta 74a -> a9 -> f69;
        # 66e8 is the pull metadata base, but includes unrelated master work in
        # the local mirror and cannot be used as the inverse parent.
        expected_parent="74a384de0acd52410269d160e94f1705cb4de300",
        expected_merge="f69eead62e71e22d1810e0dedfa363cb9f210268",
        buggy_test_symbols=(
            "MAX_MAP_GENERATION_LIMIT",
            "MAP_BLOCKSIZE",
            "BS",
            "objectpos_over_limit",
            "blockpos_over_max_limit",
            "v3f",
            "v3s16",
        ),
        expected_fixed_only_symbols=(),
    ),
)


def audit_luanti_9187_11866_gate34(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, ...]:
    """Audit both candidates and write idempotent typed Gate 3/4 receipts."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    results: list[StableArtifact] = []
    for spec in _SPECS:
        results.append(_audit_one(root, spec, authorization))
    return tuple(results)


def _audit_one(
    root: Path, spec: _Spec, authorization: AuthorizationRecord
) -> StableArtifact:
    chain = tuple(
        _load(root, reference)
        for reference in (
            spec.source,
            spec.patch,
            spec.checkpoint,
            spec.atomicity,
            spec.manifest,
            spec.gate2,
        )
    )
    source, patch, checkpoint, atomicity, manifest, gate2 = chain
    _validate_chain(spec, source, patch, checkpoint, atomicity, manifest, gate2)

    source_manifest_path = _safe_relative(
        source.payload.get("raw_evidence_path"), f"Luanti #{spec.number} source path"
    )
    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), f"Luanti #{spec.number} patch path"
    )
    source_manifest = _read_json(root / source_manifest_path)
    raw_patch = _regular_file(root / raw_patch_path).read_bytes()
    _validate_manifest_identity(spec, source_manifest)
    sections = _parse_patch(raw_patch, spec.number)
    declared = set(spec.production_paths + spec.test_paths + spec.support_paths)
    raw_paths = tuple(sorted(sections))
    if set(raw_paths) != declared:
        raise RuntimeError(
            f"Luanti #{spec.number} raw path inventory is not exactly accounted for"
        )
    if tuple(patch.payload.get("production_paths", ())) != spec.production_paths:
        raise RuntimeError(f"Luanti #{spec.number} production inventory drifted")
    if tuple(patch.payload.get("test_paths", ())) != spec.test_paths:
        raise RuntimeError(f"Luanti #{spec.number} test inventory drifted")

    # The typed artifact root is ``<workspace>/.warptest-cache/<epoch>``;
    # resolve the read-only repository mirror beside that cache, never inside
    # an artifact namespace.
    mirror = (root.parent.parent / _MIRROR_RELATIVE).resolve(strict=True)
    merge, base = _git_revisions(
        mirror, spec.expected_parent, spec.expected_merge, spec.number
    )
    if merge != spec.expected_merge or base != spec.expected_parent:
        raise RuntimeError(f"Luanti #{spec.number} merged revision drifted")
    git_paths = _git_diff_paths(mirror, base, merge)
    if git_paths != raw_paths:
        raise RuntimeError(f"Luanti #{spec.number} mirror/raw path inventory drifted")

    inverse = _git_diff(mirror, merge, base, spec.production_paths)
    _validate_inverse(spec, inverse)
    test_additions = {
        path: tuple(
            line[1:]
            for line in sections[path]
            if line.startswith("+") and not line.startswith("+++")
        )
        for path in spec.test_paths
    }
    fixed_only = _fixed_only_api_evidence(spec, mirror, base, merge, test_additions)
    if spec.number == 9187 and not fixed_only:
        raise RuntimeError("Luanti #9187 expected fixed-only API evidence vanished")
    if spec.number == 11866 and fixed_only:
        raise RuntimeError("Luanti #11866 unexpectedly gained fixed-only API evidence")

    inverse_hash = hashlib.sha256(inverse).hexdigest()
    buggy_symbol_evidence = _buggy_symbol_evidence(spec, mirror, base, test_additions)
    terminal = spec.number == 9187
    payload: dict[str, Any] = {
        "candidate": spec.candidate,
        "profile_id": f"luanti-{spec.number}-gate34-static-v1",
        "state": "terminal_gate4_static" if terminal else "gate34_static_passed",
        "outcome": (
            "gate4_fixed_only_api_terminal"
            if terminal
            else "gate3_4_static_passed_pending_gate57"
        ),
        "terminal": terminal,
        "resumable": not terminal,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_2": True,
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at for artifact in chain
        },
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed_candidate_specific",
            "strict_inverse_patch": "passed_static_production_only",
            "test_api_compatibility": (
                "blocked_fixed_only_api" if terminal else "passed_static_buggy_parent"
            ),
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
            "gate_8_13": "not_claimed",
        },
        "authority": {
            "primary_issue_number": spec.primary_issue,
            "authoritative_issue_numbers": [spec.primary_issue],
            "linked_issue_numbers": list(spec.linked_issues),
        },
        "patch_audit": {
            "raw_patch_paths": list(raw_paths),
            "production_paths": list(spec.production_paths),
            "support_paths": list(spec.support_paths),
            "preserved_test_paths": list(spec.test_paths),
            "unaccounted_paths": [],
            "inverse_scope_strict": True,
            "production_inverse_paths": list(spec.production_paths),
            "production_inverse_fingerprint": inverse_hash,
            "production_inverse_bytes": len(inverse),
            "production_inverse_patch_contains_test_paths": False,
            "production_inverse_patch_contains_support_paths": False,
        },
        "git_evidence": {
            "mirror": _MIRROR_RELATIVE.as_posix(),
            "pull_base_revision": spec.expected_base,
            "merge_parent_revision": base,
            "merged_revision": merge,
            "raw_patch_fingerprint": hashlib.sha256(raw_patch).hexdigest(),
            "mirror_changed_paths": list(git_paths),
            "read_only_git_commands": ["cat-file", "rev-list", "diff", "show"],
        },
        "test_api_audit": {
            "fixed_only_symbols": fixed_only,
            "buggy_parent_symbol_evidence": buggy_symbol_evidence,
            "test_addition_line_counts": {
                path: len(lines) for path, lines in test_additions.items()
            },
            "buggy_parent_compile_claim": (
                "rejected_fixed_only_symbol_absent"
                if terminal
                else "static_visibility_passed_no_new_api"
            ),
        },
        "required_next": (
            [
                "new_candidate_identity_with_buggy_visible_test_contract",
                "do_not_run_current_fixed_only_test_on_buggy_source",
            ]
            if terminal
            else [
                "register_candidate_specific_profile_and_selector",
                "materialize_exact_merged_fixed_capsule",
                "derive_production_only_buggy_inverse_from_this_receipt",
                "run_fresh_12_cell_gate57_epoch_without_old_attempt_inheritance",
            ]
        ),
        "roster_modified": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "complete": True,
    }
    inputs = tuple(artifact.reference for artifact in chain)
    existing = _try_load(root, spec.output)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError(f"Luanti #{spec.number} Gate 3/4 receipt drifted")
        return existing
    write_stable_artifact(
        root,
        artifact_id=spec.output.artifact_id,
        artifact_kind=spec.output.artifact_kind,
        semantic_version=spec.output.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, spec.output)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError(f"Luanti #{spec.number} Gate 3/4 typed reload failed")
    return artifact


def _validate_chain(
    spec: _Spec,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    gate2: StableArtifact,
) -> None:
    candidate = spec.candidate
    if source.inputs != (_ENUMERATION,) or source.payload.get("candidate") != candidate:
        raise RuntimeError(f"Luanti #{spec.number} source chain drifted")
    if source.payload.get("complete") is not True:
        raise RuntimeError(f"Luanti #{spec.number} source packet incomplete")
    if (
        patch.inputs != (_ENUMERATION, spec.source)
        or patch.payload.get("candidate") != candidate
    ):
        raise RuntimeError(f"Luanti #{spec.number} patch chain drifted")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError(f"Luanti #{spec.number} patch is not eligible")
    if (
        checkpoint.inputs != (_ENUMERATION, spec.patch)
        or checkpoint.payload.get("candidate") != candidate
    ):
        raise RuntimeError(f"Luanti #{spec.number} checkpoint chain drifted")
    if (
        checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError(f"Luanti #{spec.number} checkpoint incomplete")
    if atomicity.inputs != (_ENUMERATION, spec.source, spec.patch, spec.checkpoint):
        raise RuntimeError(f"Luanti #{spec.number} atomicity chain drifted")
    if (
        atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("complete") is not True
    ):
        raise RuntimeError(f"Luanti #{spec.number} atomicity candidate drifted")
    if (
        manifest.inputs != (spec.patch,)
        or manifest.payload.get("candidate") != candidate
    ):
        raise RuntimeError(f"Luanti #{spec.number} manifest chain drifted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(spec.test_paths)
    ):
        raise RuntimeError(f"Luanti #{spec.number} test manifest drifted")
    if (
        gate2.inputs[-1] != spec.gate2_reclassification
        or gate2.payload.get("candidate") != candidate
    ):
        raise RuntimeError(
            f"Luanti #{spec.number} Gate 2 reclassification chain drifted"
        )
    if (
        gate2.payload.get("state") != "gate2_passed_candidate_specific"
        or gate2.payload.get("terminal") is not False
    ):
        raise RuntimeError(f"Luanti #{spec.number} Gate 2 admission is not resumable")
    if gate2.payload.get("primary_issue_number") != spec.primary_issue:
        raise RuntimeError(f"Luanti #{spec.number} Gate 2 primary issue drifted")


def _validate_manifest_identity(spec: _Spec, manifest: dict[str, Any]) -> None:
    if (
        manifest.get("candidate") != spec.candidate
        or manifest.get("complete") is not True
        or manifest.get("pull", {}).get("number") != spec.number
        or manifest.get("pull", {}).get("merged") is not True
        or manifest.get("issue_numbers") != list(spec.linked_issues)
    ):
        raise RuntimeError(f"Luanti #{spec.number} source manifest identity drifted")
    merge = manifest.get("pull", {}).get("merge_commit_sha")
    if merge != spec.expected_merge:
        raise RuntimeError(f"Luanti #{spec.number} merge commit is not authoritative")


def _validate_inverse(spec: _Spec, inverse: bytes) -> None:
    if not inverse.startswith(b"diff --git ") or not inverse.strip():
        raise RuntimeError(f"Luanti #{spec.number} production inverse is empty")
    sections = _parse_patch(inverse, spec.number)
    if tuple(sorted(sections)) != tuple(sorted(spec.production_paths)):
        raise RuntimeError(
            f"Luanti #{spec.number} inverse scope is not production-only"
        )
    for path, lines in sections.items():
        if not any(line.startswith("--- ") for line in lines) or not any(
            line.startswith("+++ ") for line in lines
        ):
            raise RuntimeError(
                f"Luanti #{spec.number} inverse section is malformed: {path}"
            )


def _fixed_only_api_evidence(
    spec: _Spec,
    mirror: Path,
    base: str,
    merge: str,
    test_additions: dict[str, tuple[str, ...]],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for symbol in spec.expected_fixed_only_symbols:
        token = symbol.rsplit("::", 1)[-1]
        fixed_lines = _git_grep(mirror, merge, token)
        buggy_lines = _git_grep(mirror, base, token)
        test_lines = [
            {"path": path, "line": line}
            for path, lines in test_additions.items()
            for line in lines
            if token in line
        ]
        if not fixed_lines or buggy_lines or not test_lines:
            raise RuntimeError(
                f"Luanti #{spec.number} fixed-only API proof drifted for {symbol}"
            )
        evidence.append(
            {
                "symbol": symbol,
                "fixed_parent_matches": fixed_lines,
                "buggy_parent_matches": buggy_lines,
                "fixed_test_calls": test_lines,
            }
        )
    return evidence


def _buggy_symbol_evidence(
    spec: _Spec,
    mirror: Path,
    base: str,
    test_additions: dict[str, tuple[str, ...]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for symbol in spec.buggy_test_symbols:
        token = symbol.rsplit("::", 1)[-1]
        matches = _git_grep(mirror, base, token)
        test_lines = [
            {"path": path, "line": line}
            for path, lines in test_additions.items()
            for line in lines
            if token in line
        ]
        if not matches or not test_lines:
            raise RuntimeError(
                f"Luanti #{spec.number} buggy-parent API visibility is not proven for {symbol}"
            )
        result.append(
            {
                "symbol": symbol,
                "buggy_parent_matches": matches,
                "fixed_test_calls": test_lines,
            }
        )
    return result


def _git_revisions(
    mirror: Path, expected_parent: str, expected_merge: str, number: int
) -> tuple[str, str]:
    _git(mirror, "cat-file", "-e", f"{expected_parent}^{{commit}}", number=number)
    _git(mirror, "cat-file", "-e", f"{expected_merge}^{{commit}}", number=number)
    merge_base = (
        _git(
            mirror,
            "merge-base",
            expected_parent,
            expected_merge,
            number=number,
        )
        .decode()
        .strip()
    )
    if merge_base != expected_parent:
        raise RuntimeError(
            f"Luanti #{number} captured PR base is not an ancestor of merged tip"
        )
    return expected_merge, expected_parent


def _git_diff_paths(mirror: Path, base: str, merge: str) -> tuple[str, ...]:
    output = _git(mirror, "diff", "--name-only", "--no-ext-diff", base, merge)
    return tuple(sorted(line for line in output.decode().splitlines() if line))


def _git_diff(mirror: Path, left: str, right: str, paths: tuple[str, ...]) -> bytes:
    return _git(mirror, "diff", "--no-ext-diff", "--binary", left, right, "--", *paths)


def _git_grep(mirror: Path, revision: str, token: str) -> list[str]:
    completed = subprocess.run(
        ("git", "-C", str(mirror), "grep", "-n", "-F", token, revision, "--"),
        capture_output=True,
        check=False,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise RuntimeError("Luanti mirror git grep failed")
    return completed.stdout.decode("utf-8", errors="surrogateescape").splitlines()


def _git(mirror: Path, *arguments: str, number: int = 0) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(mirror), *arguments),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Luanti #{number} read-only git command failed: "
            f"{shlex.join(arguments)}: {completed.stderr.decode(errors='replace').strip()}"
        )
    return completed.stdout


def _parse_patch(raw: bytes, number: int) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError(f"Luanti #{number} patch header is malformed")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError(f"Luanti #{number} patch path header is malformed")
            current = _safe_relative(
                left[2:], f"Luanti #{number} patch path"
            ).as_posix()
            if current in sections:
                raise RuntimeError(f"Luanti #{number} patch repeats {current}")
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError(f"Luanti #{number} patch contains no sections")
    return {path: tuple(lines) for path, lines in sections.items()}


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path


def _regular_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"evidence file is missing: {path}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(_regular_file(path).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("source manifest is not an object")
    return value


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "Luanti Gate 3/4 static audit requires candidate-screening authorization"
        )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["audit_luanti_9187_11866_gate34"]
