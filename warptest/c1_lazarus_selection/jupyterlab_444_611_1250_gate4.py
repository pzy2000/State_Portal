"""Static Gate 4 admissions for JupyterLab #444, #611, and #1250.

Each fixed test uses an API already visible on the buggy side.  The strict
inverse contains production paths only and preserves every test/support hunk.
This module performs no package installation, build, browser, or test run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import jupyterlab_regression as regression
from .jupyterlab_top_profiles import _parse_patch_sections
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_NUMBERS = (444, 611, 1250)


def write_jupyterlab_gate4_admission(
    artifact_root: str | Path,
    *,
    change_number: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one candidate-specific current-v2 Gate 4 admission."""

    if change_number not in _NUMBERS:
        raise ValueError("unsupported JupyterLab Gate 4 candidate")
    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    profile = regression.profile_for_candidate(
        CandidateKey("jupyterlab", change_number)
    )
    source = _require_artifact(regression._source(root, profile), "source")
    patch = _require_artifact(regression._patch(root, profile, source), "patch")
    atomicity = _require_artifact(
        regression._atomicity(root, profile, source, patch), "atomicity"
    )
    manifest = _require_artifact(
        regression._test_manifest(root, profile, patch, atomicity), "manifest"
    )
    raw_patch = regression._raw_patch(root, profile, source, patch)
    sections = dict(_parse_patch_sections(raw_patch))
    expected = profile.production_paths + profile.patch_test_paths
    if set(sections) != set(expected) or len(sections) != len(expected):
        raise RuntimeError(f"JupyterLab #{change_number} patch partition drifted")
    inverse = b"".join(sections[path] for path in profile.production_paths)
    if any(path.encode("utf-8") in inverse for path in profile.patch_test_paths):
        raise RuntimeError(f"JupyterLab #{change_number} inverse touches a test")
    evidence = _audit_fixed_test_api(change_number, sections)
    if evidence["fixed_only_api"]:
        raise RuntimeError(f"JupyterLab #{change_number} unexpectedly uses fixed-only API")
    evidence.update(
        {
            "production_inverse_paths": list(profile.production_paths),
            "preserved_test_paths": list(profile.patch_test_paths),
            "production_inverse_byte_count": len(inverse),
            "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        }
    )
    payload: dict[str, Any] = {
        "candidate": {"repository_key": "jupyterlab", "change_number": change_number},
        "profile_id": profile.profile_id,
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
    reference = _reference(change_number)
    inputs = (source.reference, patch.reference, atomicity.reference, manifest.reference)
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"JupyterLab #{change_number} Gate 4 receipt drifted")
        return existing
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"JupyterLab #{change_number} Gate 4 typed reload failed")
    return artifact


def _audit_fixed_test_api(number: int, sections: dict[str, bytes]) -> dict[str, Any]:
    if number == 444:
        actions = sections["test/src/notebook/notebook/actions.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        model = sections["test/src/notebook/notebook/model.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        widget = sections["test/src/notebook/notebook/widget.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        markers = (
            "NotebookActions.deleteCells(widget);",
            "widget.select(widget.childAt(widget.childCount() - 1));",
            "model.cells.clear();",
            "widget.model.cells.clear();",
        )
        if markers[0] not in actions or markers[1] not in actions or markers[2] not in model or markers[3] not in widget:
            raise RuntimeError("JupyterLab #444 fixed test evidence drifted")
        return {
            "fixed_only_api": False,
            "buggy_visible_test_symbols": list(markers),
            "buggy_compile_expected": True,
            "buggy_runtime_expected": "selection_and_empty_notebook_assertion_failures",
        }
    if number == 611:
        support = sections["test/src/docmanager/mockcontext.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        test = sections["test/src/notebook/notebook/default-toolbar.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        call = "panel.context.changeKernel(void 0)"
        if call not in test or "changeKernel(options: IKernel.IModel)" not in support or "this._kernel = null;" not in support:
            raise RuntimeError("JupyterLab #611 fixed test/support evidence drifted")
        return {
            "fixed_only_api": False,
            "buggy_visible_test_symbols": [
                "ToolbarItems.createKernelStatusItem",
                "IDocumentContext.changeKernel",
                "Widget.hasClass",
            ],
            "preserved_test_support": "MockContext.changeKernel handles undefined as null",
            "declared_typescript_version": "1.8.10",
            "strict_null_checks_available": False,
            "buggy_compile_expected": True,
            "buggy_runtime_expected": "old_kernelChanged_handler_dereferences_null",
        }
    if number == 1250:
        test = sections["test/src/renderers/renderers.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        production = sections["src/renderers/index.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        call = "expect(t.isSanitizable('image/svg+xml')).to.be(false);"
        if call not in test or "isSanitizable(mimetype: string): boolean" not in production or "-    return this.mimetypes.indexOf(mimetype) !== -1;" not in production:
            raise RuntimeError("JupyterLab #1250 fixed test evidence drifted")
        return {
            "fixed_only_api": False,
            "buggy_visible_test_symbols": ["SVGRenderer", "SVGRenderer.isSanitizable"],
            "buggy_compile_expected": True,
            "buggy_runtime_expected": "isSanitizable_returns_true_assertion_failure",
        }
    raise ValueError("unsupported JupyterLab Gate 4 candidate")


def _reference(number: int) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate4_static_admission/jupyterlab/{number}/current-v2/v1",
        "candidate_gate4_static_admission",
        "1.0",
    )


def _require_artifact(value: StableArtifact | StageDecision, label: str) -> StableArtifact:
    if isinstance(value, StageDecision):
        raise RuntimeError(f"JupyterLab Gate 4 {label} is blocked: {value.reason}")
    return value


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


__all__ = ["write_jupyterlab_gate4_admission"]
