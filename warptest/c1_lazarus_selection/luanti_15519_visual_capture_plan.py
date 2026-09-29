"""Typed non-terminal visual/construction plan for Luanti #15519.

The current-v2 source chain and the v4 headless server run already provide
real Start, ordered replay, hidden oracle, and fixed/buggy semantic
observations.  They do not provide a framebuffer.  The registered adapter
uses the Irrlicht ``null`` video driver, whose ``createScreenShot`` returns a
null image, so this module records the exact Gate 11--12 recovery contract
without inventing frames or launching any historical runtime.

This is an audit/plan writer only.  It performs static checks and writes one
resumable non-terminal artifact; it never launches a client, reuses old
attempts, touches protected UE processes, or claims visual closure.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .luanti_15519_headless_adapter import (
    _ATTEMPT_KIND,
    _CONSTRUCTION,
    _LINEAGE_RECEIPT,
    _OBSERVATION,
    _ORACLE,
    _REPLAY,
    _SEMANTIC_GOLDEN,
    _VISUAL_BLOCKER,
    _validate_v4_downstream,
    load_luanti_15519_headless_binding,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_V1 = "1.0"
_V2 = "2.0"
_V4 = "4.0"
_V5 = "5.0"
_PLAN = ArtifactReference(
    "candidate_headless_visual_capture_plan/luanti/15519/v5",
    "candidate_headless_visual_capture_plan",
    _V5,
)
_AUDIT = ArtifactReference(
    "candidate_downstream_recovery_audit/luanti/15519/runtime-visual/v1",
    "candidate_downstream_recovery_audit",
    _V1,
)
_FEASIBILITY = ArtifactReference(
    "native_client_feasibility/luanti/15519/null-video/v1",
    "native_client_feasibility",
    _V1,
)
_NULL_BUILD = ArtifactReference(
    "null_client_build/luanti/15519/fixed-buggy/v2",
    "null_client_build",
    _V2,
)
_CLIENT_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    _V2,
)
_MISSING = (
    "registered_non_null_client_capture_backend",
    "registered_client_start_and_ordered_replay_adapter",
    "common_before_semantic_observation",
    "common_before_visual_frame",
    "fixed_after_visual_frame",
    "buggy_after_visual_frame",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)
_DO_NOT_TOUCH_PIDS = (76485, 89366, 95665, 99762)
_OLD_EVIDENCE = (
    "luanti_runtime_attempt/luanti/15519/fixed/v1",
    "luanti_runtime_attempt/luanti/15519/buggy/v1",
    "luanti_runtime_closure/luanti/15519/fixed-buggy/v1",
)


def write_luanti_15519_visual_capture_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Audit v4 and write a fresh, resumable Gate 11--12 plan.

    All inputs are loaded by stable identity.  The only filesystem reads
    beyond those artifacts are static reads of the two materialized
    ``CNullDriver.cpp`` files; no binary or runtime is executed.
    """

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_15519_headless_binding(root)
    observation = _load(root, _OBSERVATION)
    replay = _load(root, _REPLAY)
    oracle = _load(root, _ORACLE)
    construction = _load(root, _CONSTRUCTION)
    semantic = _load(root, _SEMANTIC_GOLDEN)
    visual = _load(root, _VISUAL_BLOCKER)
    lineage = _load(root, _LINEAGE_RECEIPT)
    attempts = tuple(
        _load(
            root,
            ArtifactReference(
                f"{_ATTEMPT_KIND}/luanti/15519/{variant}/v4",
                _ATTEMPT_KIND,
                _V4,
            ),
        )
        for variant in ("fixed", "buggy")
    )
    _validate_v4_downstream(observation, replay, oracle, construction, attempts)
    _validate_semantic_and_visual_pending(semantic, visual, lineage)
    audit = _load(root, _AUDIT)
    feasibility = _load(root, _FEASIBILITY)
    null_build = _load(root, _NULL_BUILD)
    client_closure = _load(root, _CLIENT_CLOSURE)
    _validate_audit(audit)
    _validate_feasibility(feasibility)
    _validate_null_build(null_build)
    _validate_client_closure(client_closure)
    static = _inspect_null_driver_sources(root, binding.materialization)

    binding_inputs = tuple(item.reference for item in _binding_values(binding))
    inputs = (
        *binding_inputs,
        observation.reference,
        replay.reference,
        oracle.reference,
        construction.reference,
        semantic.reference,
        visual.reference,
        lineage.reference,
        audit.reference,
        feasibility.reference,
        null_build.reference,
        client_closure.reference,
        *(item.reference for item in attempts),
    )
    payload = _plan_payload(
        binding=binding,
        observation=observation,
        replay=replay,
        oracle=oracle,
        construction=construction,
        semantic=semantic,
        visual=visual,
        lineage=lineage,
        audit=audit,
        feasibility=feasibility,
        null_build=null_build,
        client_closure=client_closure,
        attempts=attempts,
        static=static,
    )
    write_stable_artifact(
        root,
        artifact_id=_PLAN.artifact_id,
        artifact_kind=_PLAN.artifact_kind,
        semantic_version=_PLAN.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _PLAN)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("15519 visual capture plan typed reload failed")
    return artifact


def _plan_payload(
    *,
    binding: object,
    observation: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
    construction: StableArtifact,
    semantic: StableArtifact,
    visual: StableArtifact,
    lineage: StableArtifact,
    audit: StableArtifact,
    feasibility: StableArtifact,
    null_build: StableArtifact,
    client_closure: StableArtifact,
    attempts: tuple[StableArtifact, StableArtifact],
    static: dict[str, object],
) -> dict[str, object]:
    del binding
    return {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "blocker_class": "no_registered_frame_producing_client_adapter",
        "blocker_reason": (
            "The v4 fixed/buggy server replay is real and semantically complete, "
            "but the registered client path is null-video only.  Irrlicht "
            "CNullDriver::createScreenShot returns no image, so a common-before "
            "frame and fixed/buggy after frames cannot be captured without a "
            "fresh non-null client runner and an isolated framebuffer path."
        ),
        "v4_real_server_receipts": {
            "observation": observation.reference.artifact_id,
            "replay": replay.reference.artifact_id,
            "oracle": oracle.reference.artifact_id,
            "registered_start": observation.payload.get("registered_start"),
            "ordered_replay": replay.payload.get("ordered_replay"),
            "hidden_oracle": oracle.payload.get("hidden_oracle"),
            "real_semantic_observations": replay.payload.get(
                "real_semantic_observations"
            ),
            "fixed_attempt": attempts[0].reference.artifact_id,
            "buggy_attempt": attempts[1].reference.artifact_id,
        },
        "v4_construction_boundary": {
            "construction": construction.reference.artifact_id,
            "semantic_golden": semantic.reference.artifact_id,
            "visual_blocker": visual.reference.artifact_id,
            "lineage": lineage.reference.artifact_id,
            "state": construction.payload.get("state"),
        },
        "candidate_specific_capture_status": {
            "visual_capture_supported": False,
            "capture_performed": False,
            "non_null_backend_registered": False,
            "framebuffer_readback_registered": False,
            "synthetic_frames_written": False,
            "common_before_available": False,
            "fixed_after_available": False,
            "buggy_after_available": False,
        },
        "static_evidence": {
            "audit_reference": audit.reference.artifact_id,
            "feasibility_reference": feasibility.reference.artifact_id,
            "null_build_reference": null_build.reference.artifact_id,
            "client_dependency_reference": client_closure.reference.artifact_id,
            "null_build_client_launched": null_build.payload.get("client_launched"),
            "null_video_screenshot_capable": feasibility.payload.get(
                "null_video_screenshot_capable"
            ),
            "null_video_visual_status": feasibility.payload.get("visual_status"),
            "source_checks": static,
        },
        "minimum_missing_evidence": list(_MISSING),
        "recovery_steps": [
            "register a fresh fixed/buggy current-v2 client runner under a new isolated run root",
            "bind the same source, patch, atomicity, manifest, materialization, and v4 semantic lineage",
            "register Start plus identical ordered replay actions and waits for both variants",
            "capture one real common-before framebuffer and real fixed-after and buggy-after frame",
            "persist frame capture receipts with readiness, dimensions, and source/input provenance",
            "issue before visual equivalence and the complete 2x2 after visual cross-judgment",
            "write a same-lineage construction receipt linking the v4 oracle and all visual goldens",
        ],
        "excluded_evidence": list(_OLD_EVIDENCE),
        "do_not_touch_pids": list(_DO_NOT_TOUCH_PIDS),
        "candidate_execution_performed": False,
        "registered_replay_executed": False,
        "semantic_observations_complete": True,
        "goldens_complete": False,
        "visual_cross_matrix_complete": False,
        "same_lineage_construction_receipt_complete": False,
        "old_attempts_loaded": False,
        "inherit_prior_attempts": False,
        "permanent_terminal_written": False,
        "terminal": False,
        "resumable": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _validate_semantic_and_visual_pending(
    semantic: StableArtifact,
    visual: StableArtifact,
    lineage: StableArtifact,
) -> None:
    if (
        semantic.payload.get("state") != "downstream_pending"
        or semantic.payload.get("source") != "current_v4_real_headless_observations"
        or semantic.payload.get("visual_capture_supported") is not False
        or semantic.payload.get("common_before", {}).get("synthetic") is not False
    ):
        raise RuntimeError("15519 semantic golden is not real after-only evidence")
    if (
        visual.payload.get("state") != "downstream_pending"
        or visual.payload.get("visual_capture_supported") is not False
        or visual.payload.get("null_video_driver_only") is not True
        or visual.payload.get("synthetic_frames_written") is not False
    ):
        raise RuntimeError("15519 visual blocker is not non-terminal")
    if (
        lineage.payload.get("state") != "downstream_pending"
        or lineage.payload.get("construction_lineage_state")
        != "pending_visual_and_common_before"
        or lineage.payload.get("same_lineage_inputs") is None
    ):
        raise RuntimeError("15519 construction lineage is not pending")


def _validate_audit(artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "downstream_pending"
        or payload.get("terminal") is not False
        or payload.get("permanent_terminal_written") is not False
        or payload.get("inherit_prior_attempts") is not False
        or payload.get("do_not_touch_pids") != list(_DO_NOT_TOUCH_PIDS)
    ):
        raise RuntimeError("15519 prior downstream audit is invalid")


def _validate_feasibility(artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("null_video_supported") is not True
        or payload.get("null_video_screenshot_capable") is not False
        or payload.get("visual_status") != "blocked_null_driver_has_no_framebuffer"
        or payload.get("visual_cross_matrix_status")
        != "blocked_until_non_null_frames_exist"
    ):
        raise RuntimeError("15519 null-video feasibility evidence is invalid")


def _validate_null_build(artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "built"
        or payload.get("fixed_build") != "passed"
        or payload.get("buggy_build") != "passed"
        or payload.get("client_launched") is not False
    ):
        raise RuntimeError("15519 null-client build evidence is invalid")


def _validate_client_closure(artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "materialized"
        or payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("15519 client dependency closure is invalid")


def _inspect_null_driver_sources(
    root: Path,
    materialization: StableArtifact,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for variant, key in (("fixed", "fixed_tree_path"), ("buggy", "buggy_tree_path")):
        tree = root / PurePosixPath(str(materialization.payload[key]))
        source = tree / "irr/src/CNullDriver.cpp"
        rendering = tree / "src/client/renderingengine.cpp"
        _ordinary_file(source, tree)
        _ordinary_file(rendering, tree)
        source_text = source.read_text(encoding="utf-8")
        rendering_text = rendering.read_text(encoding="utf-8")
        marker = "IImage *CNullDriver::createScreenShot"
        start = source_text.find(marker)
        body = source_text[start : start + 300] if start >= 0 else ""
        result[variant] = {
            "null_driver_screenshot_symbol_present": start >= 0,
            "null_driver_screenshot_returns_null": start >= 0 and "return 0;" in body,
            "null_driver_selection_registered": '"null"' in rendering_text
            and "EDT_NULL" in rendering_text,
            "capture_receipt_present": False,
        }
        if not (
            result[variant]["null_driver_screenshot_symbol_present"] is True
            and result[variant]["null_driver_screenshot_returns_null"] is True
        ):
            raise RuntimeError(f"15519 {variant} null-driver static proof changed")
    return result


def _binding_values(binding: object) -> tuple[StableArtifact, ...]:
    return (
        binding.source,
        binding.patch,
        binding.atomicity,
        binding.manifest,
        binding.capsule,
        binding.client_aggregate,
        binding.materialization,
        binding.task_mapping,
    )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required source file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required source file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_15519_visual_capture_plan"]
