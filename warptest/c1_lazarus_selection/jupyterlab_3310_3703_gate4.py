"""Static Gate 4 audit for JupyterLab #3310 and #3703.

This lane only reloads the current-v2 source/patch/atomicity/manifest chain and
the already captured fixed source capsules.  It checks the patch partition and
the test expressions against a production-only inverse.  It intentionally does
not materialize a worktree, invoke TypeScript/Node/Karma, access the network,
or schedule paid/formal trials.
"""

from __future__ import annotations

import os
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


@dataclass(frozen=True)
class CandidateSpec:
    number: int
    issue: int
    profile_id: str
    production_paths: tuple[str, ...]
    test_path: str
    raw_patch_path: str
    source_raw_path: str
    capsule_tree: str
    fixed_markers: tuple[str, ...]
    production_markers: tuple[str, ...]
    api_audit: dict[str, Any]

    @property
    def artifact_inputs(self) -> tuple[ArtifactReference, ...]:
        n = self.number
        return (
            ArtifactReference(
                f"candidate_source_packet/jupyterlab/{n}/source/v2",
                "candidate_source_packet",
                "2.0",
            ),
            ArtifactReference(
                f"candidate_test_patch/jupyterlab/{n}/screening/v2",
                "candidate_test_patch",
                "2.0",
            ),
            ArtifactReference(
                f"source_atomicity/jupyterlab/{n}/preannotation/v2",
                "source_atomicity",
                "2.0",
            ),
            ArtifactReference(
                f"candidate_stage/jupyterlab/{n}/test_manifest/v2",
                "candidate_stage",
                "2.0",
            ),
            ArtifactReference(
                f"fixed_source_capsule/jupyterlab/{n}/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            ),
        )

    @property
    def admission_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate4_static_admission/jupyterlab/{self.number}/current-v2/v1",
            "candidate_gate4_static_admission",
            "1.0",
        )


_CACHE = Path(".warptest-cache/c1_lazarus_selection_v1")

_SPECS = {
    3310: CandidateSpec(
        number=3310,
        issue=3309,
        profile_id="jupyterlab-3310-dock-focus-tracker-removal-v1",
        production_paths=("packages/application/src/shell.ts",),
        test_path="test/src/application/shell.spec.ts",
        raw_patch_path=(
            "raw/github/jupyterlab/3310/pull-diff/attempt-1/"
            "2026-08-13T11-50-02.578456Z.response.body"
        ),
        source_raw_path=(
            "raw/github/jupyterlab/3310/source-manifest/attempt-1/"
            "2026-08-13T11-50-20.019844Z.json"
        ),
        capsule_tree="private/fixed-capsules/jupyterlab/3310/captured-merged-tree",
        fixed_markers=(
            "+      widget.parent = null;",
            "+      expect(shell.currentWidget).to.be(null);",
        ),
        production_markers=(
            "-    MessageLoop.installMessageHook(dockPanel, Private.activityClassHook);",
            "+    MessageLoop.installMessageHook(dockPanel, this._dockChildHook);",
            "+  private _dockChildHook =",
        ),
        api_audit={
            "fixed_only_api": False,
            "buggy_compile_expected": True,
            "inverse_restores_existing_api": True,
            "fixed_test_new_expression": "widget.parent = null;",
            "test_visible_symbols": [
                "Widget.parent",
                "ApplicationShell.currentWidget",
                "expect",
                "to.be",
            ],
            "production_added_symbols_not_called_by_test": [
                "ApplicationShell._dockChildHook",
            ],
            "api_compatibility_reason": (
                "The fixed assertion exercises the existing Widget.parent setter "
                "and ApplicationShell.currentWidget getter; the inverse restores "
                "the prior message-hook/tracker implementation and removes no API "
                "used by the test."
            ),
            "buggy_runtime_expected": "regression assertion may fail; runtime deferred",
        },
    ),
    3703: CandidateSpec(
        number=3703,
        issue=3696,
        profile_id="jupyterlab-3703-restart-cancellation-v1",
        production_paths=(
            "packages/apputils/src/clientsession.ts",
            "packages/console-extension/src/index.ts",
            "packages/mainmenu/src/kernel.ts",
            "packages/mainmenu/src/run.ts",
            "packages/notebook-extension/src/index.ts",
        ),
        test_path="tests/test-apputils/src/clientsession.spec.ts",
        raw_patch_path=(
            "raw/github/jupyterlab/3703/pull-diff/attempt-1/"
            "2026-08-13T12-02-54.215173Z.response.body"
        ),
        source_raw_path=(
            "raw/github/jupyterlab/3703/source-manifest/attempt-1/"
            "2026-08-13T12-03-13.972966Z.json"
        ),
        capsule_tree="private/fixed-capsules/jupyterlab/3703/captured-merged-tree",
        fixed_markers=(
            "+          expect(restarted).to.be(true);",
            "+          expect(restarted).to.be(false);",
        ),
        production_markers=(
            "-  restart(): Promise<Kernel.IKernelConnection | null>;",
            "+  restart(): Promise<boolean>;",
            "+  function restartKernel(kernel: Kernel.IKernelConnection): Promise<boolean>",
        ),
        api_audit={
            "fixed_only_api": False,
            "buggy_compile_expected": True,
            "inverse_restores_existing_api": True,
            "fixed_test_new_expressions": [
                "expect(restarted).to.be(true);",
                "expect(restarted).to.be(false);",
            ],
            "test_visible_symbols": ["ClientSession.restart", "expect", "to.be"],
            "production_added_symbols_not_called_by_test": [
                "ClientSession.restart boolean contract",
                "IKernelMenu restartKernel boolean contract",
                "IRunMenu restartAndRunAll boolean contract",
            ],
            "buggy_inverse_signature": "restart(): Promise<Kernel.IKernelConnection | null>",
            "api_compatibility_reason": (
                "The fixed test consumes the restart result only as a value passed "
                "to Chai expect.  The production-only inverse restores the older "
                "kernel-connection-or-null return type, which remains a compilable "
                "value expression; the true/false expectation is a deferred runtime "
                "semantic check, not a fixed-only TypeScript API use."
            ),
            "buggy_runtime_expected": "boolean assertion may fail; runtime deferred",
        },
    ),
}


def write_jupyterlab_3310_3703_gate4_admissions(
    artifact_root: str | Path = _CACHE,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact]:
    """Write and typed-reload the two static Gate 4 admissions."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    receipts = tuple(_write_admission(root, _SPECS[n], authorization) for n in (3310, 3703))
    return receipts  # type: ignore[return-value]


def _write_admission(
    root: Path, spec: CandidateSpec, authorization: AuthorizationRecord
) -> StableArtifact:
    evidence = _audit_candidate(root, spec)
    if evidence["fixed_only_api"] or not evidence["buggy_compile_expected"]:
        raise RuntimeError(f"JupyterLab #{spec.number} failed static Gate 4")
    payload: dict[str, Any] = {
        "candidate": {"repository_key": "jupyterlab", "change_number": spec.number},
        "profile_id": spec.profile_id,
        "primary_issue_number": spec.issue,
        "state": "admitted",
        "outcome": "gate4_static_passed",
        "terminal": False,
        "resumable": True,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_production_only_inverse": "passed",
            "fixed_test_buggy_api_compatibility": "passed_static",
            "gate_5_7": "downstream_pending",
        },
        "api_audit": evidence,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "network_actions_authorized": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "retain_as_gate1_4_candidate_pending_candidate_owned_runtime",
        "complete": True,
    }
    reference = spec.admission_reference
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != spec.artifact_inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"JupyterLab Gate 4 receipt conflicts: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=spec.artifact_inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.inputs != spec.artifact_inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"JupyterLab #{spec.number} Gate 4 typed reload failed")
    return artifact


def _audit_candidate(root: Path, spec: CandidateSpec) -> dict[str, Any]:
    source, patch, atomicity, manifest, capsule = (
        _load(root, ref) for ref in spec.artifact_inputs
    )
    candidate = {"repository_key": "jupyterlab", "change_number": spec.number}
    if source.payload.get("candidate") != candidate or not source.payload.get("complete"):
        raise RuntimeError(f"JupyterLab #{spec.number} source chain is incomplete")
    if patch.payload.get("candidate") != candidate or not patch.payload.get("complete"):
        raise RuntimeError(f"JupyterLab #{spec.number} patch chain is incomplete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError(f"JupyterLab #{spec.number} patch is not eligible")
    if patch.payload.get("production_paths") != list(spec.production_paths):
        raise RuntimeError(f"JupyterLab #{spec.number} production path drift")
    if patch.payload.get("test_paths") != [spec.test_path]:
        raise RuntimeError(f"JupyterLab #{spec.number} test path drift")
    if patch.payload.get("raw_patch_path") != spec.raw_patch_path:
        raise RuntimeError(f"JupyterLab #{spec.number} raw patch binding drift")
    if source.payload.get("raw_evidence_path") != spec.source_raw_path:
        raise RuntimeError(f"JupyterLab #{spec.number} source binding drift")
    if (
        atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != spec.issue
    ):
        raise RuntimeError(f"JupyterLab #{spec.number} atomicity chain is not accepted")
    if (
        manifest.payload.get("candidate") != candidate
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths") != [spec.test_path]
    ):
        raise RuntimeError(f"JupyterLab #{spec.number} test manifest is not executable")
    if (
        capsule.payload.get("candidate") != candidate
        or capsule.payload.get("profile_id") != spec.profile_id
        or capsule.payload.get("source_tree_path") != spec.capsule_tree
        or capsule.payload.get("production_paths") != list(spec.production_paths)
        or capsule.payload.get("test_paths") != [spec.test_path]
    ):
        raise RuntimeError(f"JupyterLab #{spec.number} fixed capsule binding drift")

    raw_patch = _regular(root / spec.raw_patch_path, "raw patch")
    sections = _parse_patch_sections(raw_patch)
    expected_paths = (*spec.production_paths, spec.test_path)
    if tuple(path for path, _section in sections) != expected_paths:
        raise RuntimeError(f"JupyterLab #{spec.number} patch partition drift")
    by_path = dict(sections)
    for marker in spec.fixed_markers:
        if marker not in by_path[spec.test_path].decode("utf-8", errors="surrogateescape"):
            raise RuntimeError(f"JupyterLab #{spec.number} fixed test marker missing")
    production_text = "".join(
        by_path[path].decode("utf-8", errors="surrogateescape") for path in spec.production_paths
    )
    for marker in spec.production_markers:
        if marker not in production_text:
            raise RuntimeError(f"JupyterLab #{spec.number} production marker missing")

    tree = root / spec.capsule_tree
    for path in (*spec.production_paths, spec.test_path):
        _regular(tree / _safe_relative(path), f"capsule file {path}")
    inverse = b"".join(by_path[path] for path in spec.production_paths)
    evidence = dict(spec.api_audit)
    evidence.update(
        {
            "candidate": candidate,
            "partition_exact": True,
            "production_inverse_paths": list(spec.production_paths),
            "preserved_test_paths": [spec.test_path],
            "production_inverse_byte_count": len(inverse),
            "fixed_capsule_tree": spec.capsule_tree,
            "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            "static_only": True,
        }
    )
    return evidence


def _parse_patch_sections(content: bytes) -> tuple[tuple[str, bytes], ...]:
    text = content.decode("utf-8", errors="surrogateescape")
    sections: list[tuple[str, list[str]]] = []
    path: str | None = None
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                sections.append((path, lines))
            parts = line.rstrip("\r\n").split(" ")
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise RuntimeError("raw patch header is invalid")
            if parts[2][2:] != parts[3][2:]:
                raise RuntimeError("raw patch rename is outside static audit")
            path = parts[3][2:]
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        sections.append((path, lines))
    if not sections:
        raise RuntimeError("raw patch has no sections")
    return tuple((path, "".join(lines).encode("utf-8", errors="surrogateescape")) for path, lines in sections)


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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _regular(path: Path, label: str) -> bytes:
    try:
        if not path.is_file():
            raise RuntimeError(f"{label} is missing")
        return path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"{label} is unavailable") from error


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise RuntimeError("unsafe relative path")
    return path


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("JupyterLab Gate 4 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    auth = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-3310-3703-gate4-static",
        scope="candidate-screening",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    for artifact in write_jupyterlab_3310_3703_gate4_admissions(authorization=auth):
        print(artifact.reference.artifact_id)
        print(artifact.revision_path)


__all__ = ["write_jupyterlab_3310_3703_gate4_admissions"]
