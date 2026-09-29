"""Candidate-specific static Gate 4 audit for JupyterLab #2437 and #2469.

#2437's fixed test changes only an assertion over the existing metadata API,
so it remains executable after a production-only inverse.  #2469's fixed test
instantiates ``VDomRenderer<null>``, while the inverse restores the old
``T extends IModel`` constraint, making that test a fixed-only type consumer.
No runtime or candidate build is used here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .jupyterlab_top_profiles import (
    _parse_patch_sections,
    load_top_candidate_chain,
    profile_for_top_candidate,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ADMISSION_2437 = ArtifactReference(
    "candidate_gate4_static_admission/jupyterlab/2437/current-v2/v1",
    "candidate_gate4_static_admission",
    "1.0",
)
_TERMINAL_2469 = ArtifactReference(
    "candidate_gate4_static_terminal/jupyterlab/2469/current-v2/v1",
    "candidate_gate4_static_terminal",
    "1.0",
)


def write_jupyterlab_2437_gate4_admission(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Admit #2437 using its assertion-only fixed test change."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    evidence, inputs = _audit(root, 2437)
    if evidence["fixed_only_api"]:
        raise RuntimeError("JupyterLab #2437 unexpectedly uses a fixed-only API")
    payload: dict[str, Any] = {
        "candidate": {"repository_key": "jupyterlab", "change_number": 2437},
        "profile_id": "jupyterlab-2437-slideshow-metadata-shape-v2",
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
    return _write_or_reload(root, _ADMISSION_2437, inputs, payload, authorization)


def write_jupyterlab_2469_gate4_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Reject #2469's fixed-only nullable generic test contract at Gate 4."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    evidence, inputs = _audit(root, 2469)
    if not evidence["fixed_only_api"]:
        raise RuntimeError("JupyterLab #2469 fixed-only generic mismatch disappeared")
    payload: dict[str, Any] = {
        "candidate": {"repository_key": "jupyterlab", "change_number": 2469},
        "profile_id": "jupyterlab-2469-vdom-null-model-v1",
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "reason": "preserved_test_instantiates_VDomRenderer_null_removed_by_production_inverse",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_production_only_inverse": "passed",
            "fixed_test_buggy_api_compatibility": "failed_fixed_only_generic_constraint",
            "gate_5_7": "not_run_terminal",
        },
        "api_audit": evidence,
        "supersedes_profile_static_status": "candidate_profile/jupyterlab/2469/preflight/v2",
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "network_actions_authorized": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "do_not_schedule_gate5_7_for_this_fixed_test_contract",
        "complete": True,
    }
    return _write_or_reload(root, _TERMINAL_2469, inputs, payload, authorization)


def _audit(root: Path, number: int) -> tuple[dict[str, Any], tuple[ArtifactReference, ...]]:
    candidate = CandidateKey("jupyterlab", number)
    chain = load_top_candidate_chain(root, candidate)
    profile = profile_for_top_candidate(candidate)
    profile_artifact = _load(root, profile.profile_reference)
    preparation = _load(root, profile.preparation_reference)
    capsule = _load(root, profile.capsule_reference)
    if (
        profile_artifact.payload.get("candidate") != candidate.to_value()
        or preparation.payload.get("candidate") != candidate.to_value()
        or capsule.payload.get("candidate") != candidate.to_value()
        or profile_artifact.inputs
        != (
            chain.source.reference,
            chain.patch.reference,
            chain.atomicity.reference,
            chain.manifest.reference,
        )
        or preparation.inputs != (profile_artifact.reference,)
    ):
        raise RuntimeError(f"JupyterLab #{number} typed Gate 4 chain drifted")
    sections = dict(_parse_patch_sections(chain.raw_patch))
    production_inverse = b"".join(sections[path] for path in profile.production_paths)
    if any(path.encode("utf-8") in production_inverse for path in profile.patch_test_paths):
        raise RuntimeError(f"JupyterLab #{number} inverse includes a test path")
    evidence = _audit_test_api(number, sections)
    evidence.update(
        {
            "production_inverse_paths": list(profile.production_paths),
            "preserved_test_paths": list(profile.patch_test_paths),
            "production_inverse_byte_count": len(production_inverse),
            "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        }
    )
    return evidence, (
        chain.source.reference,
        chain.patch.reference,
        chain.atomicity.reference,
        chain.manifest.reference,
        profile_artifact.reference,
        preparation.reference,
        capsule.reference,
    )


def _audit_test_api(number: int, sections: dict[str, bytes]) -> dict[str, Any]:
    if number == 2437:
        production = sections["packages/notebook/src/celltools.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        test = sections["test/src/notebook/celltools.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        assertion = "expect(metadata.get('slideshow')).to.eql({ 'slide_type': 'slide' });"
        if (
            "+      " + assertion not in test
            or "-      expect(metadata.get('slideshow')).to.be('slide');" not in test
            or "getter?: (cell: Cell) => JSONValue;" not in production
            or "setter?: (cell: Cell, value: JSONValue) => void;" not in production
        ):
            raise RuntimeError("JupyterLab #2437 API evidence drifted")
        return {
            "fixed_only_api": False,
            "fixed_test_new_expression": assertion,
            "test_visible_symbols": ["metadata.get", "expect", "to.eql"],
            "production_added_symbols_not_called_by_test": [
                "KeySelector.IOptions.getter",
                "KeySelector.IOptions.setter",
            ],
            "buggy_compile_expected": True,
            "buggy_runtime_expected": "assertion_failure_string_instead_of_object",
        }
    if number == 2469:
        production = sections["packages/apputils/src/vdom.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        test = sections["test/src/apputils/vdom.spec.ts"].decode(
            "utf-8", errors="surrogateescape"
        )
        old_bound = "abstract class VDomRenderer<T extends VDomRenderer.IModel> extends Widget"
        new_bound = "abstract class VDomRenderer<T extends VDomRenderer.IModel | null> extends Widget"
        fixed_use = "class TestWidgetNoModel extends VDomRenderer<null>"
        if (
            "-" + old_bound not in production
            or "+" + new_bound not in production
            or "+" + fixed_use not in test
        ):
            raise RuntimeError("JupyterLab #2469 API evidence drifted")
        return {
            "fixed_only_api": True,
            "fixed_test_type_use": fixed_use,
            "fixed_generic_constraint": "T extends VDomRenderer.IModel | null",
            "buggy_generic_constraint_restored_by_inverse": "T extends VDomRenderer.IModel",
            "buggy_compile_expected": False,
            "expected_type_failure": "null_does_not_satisfy_VDomRenderer.IModel_constraint",
        }
    raise ValueError("only JupyterLab #2437 and #2469 are supported")


def _write_or_reload(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"JupyterLab Gate 4 receipt conflicts: {reference.artifact_id}")
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
        raise RuntimeError(f"JupyterLab Gate 4 typed reload failed: {reference.artifact_id}")
    return artifact


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


__all__ = [
    "write_jupyterlab_2437_gate4_admission",
    "write_jupyterlab_2469_gate4_terminal",
]
