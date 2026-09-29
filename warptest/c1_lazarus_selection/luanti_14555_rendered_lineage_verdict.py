"""Read-only current-v2 lineage verdict for Luanti #14555 rendered evidence.

The native current-v2 Gate 5--7 epoch is independent and qualified, but it is
server/unit-test-only.  This module audits the old rendered receipts against
that fresh epoch and inspects the disposable client trees without starting a
client, a server, or a capture runner.  The old rendered receipts are retained
as explicit inputs for the audit, not as admissible visual evidence.

In particular, a ``same_lineage`` claim in an old construction receipt is not
accepted when the receipt does not bind the current source/patch/atomicity/
manifest and current client materialization.  A later invocation may resume
from this typed non-terminal verdict after a parent-authorized BUILD_CLIENT
receipt and a newly registered rendered adapter exist.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warptest.luanti_runtime import luanti_client_source_identity

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14555}
_V1 = "1.0"
_V2 = "2.0"

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14555/source/v2", "candidate_source_packet", _V2
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14555/screening/v2", "candidate_test_patch", _V2
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14555/preannotation/v2", "source_atomicity", _V2
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14555/test_manifest/v2", "candidate_stage", _V2
)
_MATERIALIZATION = ArtifactReference(
    "candidate_stage/luanti/14555/version_materialization/v2", "candidate_stage", _V2
)
_REGISTRATION = ArtifactReference(
    "candidate_gate57_14555_p2p_registration/luanti/14555/v1",
    "candidate_gate57_14555_p2p_registration",
    _V1,
)
_PROFILE = ArtifactReference(
    "candidate_gate57_14555_fresh_profile/luanti/14555/v1",
    "candidate_gate57_14555_fresh_profile",
    _V1,
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_14555_fresh_preparation/luanti/14555/v1",
    "candidate_gate57_14555_fresh_preparation",
    _V1,
)
_BINDING = ArtifactReference(
    "candidate_gate57_14555_fresh_test_binding/luanti/14555/v1",
    "candidate_gate57_14555_fresh_test_binding",
    _V1,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14555_fresh_aggregate/luanti/14555/v1",
    "candidate_gate57_14555_fresh_aggregate",
    _V1,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14555_fresh_transition/luanti/14555/v1",
    "candidate_gate57_14555_fresh_transition",
    _V1,
)
_BUILD_KIND = "candidate_gate57_14555_build"
_BUILD_FIXED = ArtifactReference(
    "candidate_gate57_14555_build/luanti/14555/fixed/v1", _BUILD_KIND, _V1
)
_BUILD_BUGGY = ArtifactReference(
    "candidate_gate57_14555_build/luanti/14555/buggy/v1", _BUILD_KIND, _V1
)

_OLD_AGGREGATE = ArtifactReference(
    "luanti_14555_rendered_observation/aggregate/v2",
    "luanti_14555_rendered_observation_aggregate",
    _V2,
)
_OLD_ATTEMPT_KIND = "luanti_14555_rendered_observation"
_OLD_ATTEMPTS = tuple(
    ArtifactReference(
        f"luanti_14555_rendered_observation/{variant}/repetition-{repetition}/v2",
        _OLD_ATTEMPT_KIND,
        _V2,
    )
    for variant in ("fixed", "buggy")
    for repetition in (1, 2, 3)
)
_OLD_VERSION = ArtifactReference(
    "candidate_stage/luanti/14555/version_materialization/v2", "candidate_stage", _V2
)
_OLD_REPLAY = ArtifactReference(
    "candidate_stage/luanti/14555/frozen_replay/v2", "candidate_stage", _V2
)
_OLD_ORACLE = ArtifactReference(
    "candidate_stage/luanti/14555/hidden_oracle/v2", "candidate_stage", _V2
)
_OLD_CONSTRUCTION = ArtifactReference(
    "candidate_stage/luanti/14555/construction_gate/v2", "candidate_stage", _V2
)
_OLD_TEST_REPETITIONS = ArtifactReference(
    "candidate_stage/luanti/14555/test_repetitions/v2", "candidate_stage", _V2
)
_OLD_ANNOTATION = ArtifactReference(
    "candidate_stage/luanti/14555/annotation_eligibility/v2", "candidate_stage", _V2
)
_OLD_CHECKPOINT = ArtifactReference(
    "preannotation_checkpoint/luanti/14555/current/v2", "preannotation_checkpoint", _V2
)
_OLD_SPRINT = ArtifactReference(
    "candidate_sprint_terminal/luanti/14555/visual-closure/v2",
    "candidate_sprint_terminal",
    _V2,
)
_OLD_STATUS = ArtifactReference(
    "candidate_downstream_status/luanti/14555/current/v2",
    "candidate_downstream_status",
    _V2,
)
_VERDICT = ArtifactReference(
    "candidate_gate57_14555_rendered_lineage_verdict/luanti/14555/v1",
    "candidate_gate57_14555_rendered_lineage_verdict",
    _V1,
)
_PROTECTED_PIDS = (76485, 89366, 95665, 99762)
_BACKGROUND_MARKERS = (
    b"warptest_background_request_path",
    b"warptest_background_response_path",
    b"warptest_background_session_id",
    b"warptest_background_root",
    b"WARPTEST_C1_BACKGROUND_WINDOW",
)
_SOURCE_MARKERS = {
    "game_capture_hook": ("WARPTEST-CAPTURE-BEGIN", "WARPTEST-CAPTURE-END"),
    "hidden_window_hook": ("WARPTEST_C1_BACKGROUND_WINDOW",),
}
_APP_RESOURCES = (
    "Contents/Info.plist",
    "Contents/Resources",
    "Contents/Resources/builtin",
    "Contents/Resources/games/devtest",
    "Contents/Resources/fonts",
    "Contents/Resources/textures",
    "Contents/Resources/locale",
    "Contents/Frameworks",
)


def write_luanti_14555_rendered_lineage_verdict(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a typed, non-terminal verdict without launching any runtime."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    workspace = Path(workspace_root).absolute().resolve(strict=True)

    current = tuple(
        _load(root, reference)
        for reference in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _MATERIALIZATION,
            _REGISTRATION,
            _PROFILE,
            _PREPARATION,
            _BINDING,
            _AGGREGATE,
            _TRANSITION,
            _BUILD_FIXED,
            _BUILD_BUGGY,
        )
    )
    _validate_current(current)
    (
        source,
        patch,
        atomicity,
        manifest,
        materialization,
        registration,
        profile,
        preparation,
        binding,
        aggregate,
        transition,
        build_fixed,
        build_buggy,
    ) = current

    old = tuple(
        _load(root, reference)
        for reference in (
            _OLD_AGGREGATE,
            *_OLD_ATTEMPTS,
            _OLD_VERSION,
            _OLD_REPLAY,
            _OLD_ORACLE,
            _OLD_CONSTRUCTION,
            _OLD_TEST_REPETITIONS,
            _OLD_ANNOTATION,
            _OLD_CHECKPOINT,
            _OLD_SPRINT,
            _OLD_STATUS,
        )
    )
    (
        old_aggregate,
        *old_attempts,
        old_version,
        old_replay,
        old_oracle,
        old_construction,
        old_test_repetitions,
        old_annotation,
        old_checkpoint,
        old_sprint,
        old_status,
    ) = old

    app_static = _inspect_apps(workspace, profile)
    old_audit = _audit_old_rendered(
        artifact_root=root,
        old_aggregate=old_aggregate,
        old_attempts=tuple(old_attempts),
        old_version=old_version,
        old_replay=old_replay,
        old_oracle=old_oracle,
        old_construction=old_construction,
        old_test_repetitions=old_test_repetitions,
        old_annotation=old_annotation,
        old_checkpoint=old_checkpoint,
        old_sprint=old_sprint,
        old_status=old_status,
    )

    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "blocker_class": "current_v2_client_provenance_not_bound",
        "blocker_reason": (
            "The fresh Gate 5-7 epoch is a qualified native server/unit-test epoch. "
            "Its receipts explicitly set BUILD_CLIENT=false.  The existing app "
            "bundles are not proven current-v2 clients: their executable differs "
            "from the source-tree client, has no client build sidecar, and lacks "
            "the current background request/input markers.  No rendered runtime "
            "was started while producing this verdict."
        ),
        "terminal": False,
        "resumable": True,
        "old_rendered_countable": False,
        "old_rendered_lineage": old_audit,
        "fresh_gate57": {
            "profile_reference": profile.reference.artifact_id,
            "preparation_reference": preparation.reference.artifact_id,
            "test_binding_reference": binding.reference.artifact_id,
            "aggregate_reference": aggregate.reference.artifact_id,
            "transition_reference": transition.reference.artifact_id,
            "attempt_count": aggregate.payload.get("attempt_count"),
            "qualified": aggregate.payload.get("qualified"),
            "no_timeout_or_unknown": aggregate.payload.get("no_timeout_or_unknown"),
            "old_attempts_loaded": aggregate.payload.get("old_attempts_loaded"),
            "inherit_prior_attempts": preparation.payload.get("inherit_prior_attempts"),
            "client_build_covered": False,
            "server_build_receipts": [
                build_fixed.reference.artifact_id,
                build_buggy.reference.artifact_id,
            ],
        },
        "current_v2_chain": [
            source.reference.artifact_id,
            patch.reference.artifact_id,
            atomicity.reference.artifact_id,
            manifest.reference.artifact_id,
            materialization.reference.artifact_id,
            registration.reference.artifact_id,
        ],
        "client_static_audit": app_static,
        "fresh_render_epoch": {
            "candidate_execution_performed": False,
            "old_attempts_loaded": False,
            "inherit_prior_attempts": False,
            "synthetic_frames_written": False,
            "registered_start_adapter": False,
            "ordered_replay_adapter": False,
            "non_null_frame_backend": False,
            "attempt_count": 0,
        },
        "missing_evidence": [
            "fresh BUILD_CLIENT receipt for fixed and buggy",
            "current source identity and client sidecar bound to each app executable",
            "registered client Start and ordered replay adapter",
            "real common-before frame and semantic observation",
            "real fixed-after and buggy-after frames and semantic observations",
            "before equivalence and after 2x2 visual cross-judgment",
            "same-lineage construction receipt bound to current-v2 and fresh frames",
        ],
        "recovery_plan": [
            "Build both disposable clients with BUILD_CLIENT=true under a new candidate namespace.",
            "Require each app executable to carry a matching current source identity sidecar and background capture/input markers.",
            "Register fresh Start plus ordered replay and capture adapters; do not import old attempts.",
            "After explicit parent authorization, run only one bounded fixed/buggy x3 rendered epoch.",
            "Materialize real common-before/fixed-after/buggy-after goldens, visual cross matrix, and same-lineage construction receipt.",
        ],
        "no_launch_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "do_not_touch_pids": list(_PROTECTED_PIDS),
    }
    inputs = tuple(item.reference for item in current) + tuple(
        item.reference for item in old
    )
    write_stable_artifact(
        root,
        artifact_id=_VERDICT.artifact_id,
        artifact_kind=_VERDICT.artifact_kind,
        semantic_version=_VERDICT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _VERDICT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14555 rendered lineage verdict typed reload failed")
    return artifact


def _validate_current(current: tuple[StableArtifact, ...]) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in current):
        raise RuntimeError("#14555 current-v2 rendered verdict candidate drift")
    profile, preparation, binding, aggregate, transition = current[6:11]
    if profile.payload.get("build_mode", {}).get("BUILD_CLIENT") is not False:
        raise RuntimeError("#14555 fresh profile unexpectedly claims a client build")
    if (
        preparation.payload.get("inherit_prior_attempts") is not False
        or preparation.payload.get("old_attempts_loaded") is not False
        or binding.payload.get("old_attempts_loaded") is not False
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("no_timeout_or_unknown") is not True
        or aggregate.payload.get("old_attempts_loaded") is not False
        or transition.payload.get("state") != "passed"
    ):
        raise RuntimeError("#14555 fresh Gate 5-7 receipts are not admissible")


def _audit_old_rendered(
    *,
    artifact_root: Path,
    old_aggregate: StableArtifact,
    old_attempts: tuple[StableArtifact, ...],
    old_version: StableArtifact,
    old_replay: StableArtifact,
    old_oracle: StableArtifact,
    old_construction: StableArtifact,
    old_test_repetitions: StableArtifact,
    old_annotation: StableArtifact,
    old_checkpoint: StableArtifact,
    old_sprint: StableArtifact,
    old_status: StableArtifact,
) -> dict[str, Any]:
    attempt_receipts_have_no_inputs = all(not item.inputs for item in old_attempts)
    construction_inputs = {item.artifact_id for item in old_construction.inputs}
    current_markers = {
        _SOURCE.artifact_id,
        _PATCH.artifact_id,
        _ATOMICITY.artifact_id,
        _MANIFEST.artifact_id,
        _PROFILE.artifact_id,
        _AGGREGATE.artifact_id,
    }
    return {
        "aggregate_reference": old_aggregate.reference.artifact_id,
        "aggregate_qualified_claim": old_aggregate.payload.get("qualified"),
        "aggregate_inputs_bind_current_v2": bool(
            current_markers.intersection(
                {item.artifact_id for item in old_aggregate.inputs}
            )
        ),
        "attempt_count": len(old_attempts),
        "attempt_receipts_have_no_inputs": attempt_receipts_have_no_inputs,
        "attempts_are_admitted": False,
        "old_evidence_run_id": old_version.payload.get("evidence_run_id"),
        "old_replay_state": old_replay.payload.get("state"),
        "old_replay_outcome": old_replay.payload.get("outcome"),
        "old_replay_complete": old_replay.payload.get("complete"),
        "hidden_oracle_state": old_oracle.payload.get("state"),
        "construction_state": old_construction.payload.get("state"),
        "construction_same_lineage_claim": old_construction.payload.get(
            "details", {}
        ).get("same_lineage"),
        "construction_binds_current_v2": current_markers.issubset(construction_inputs),
        "test_repetitions_uses_curated_lineage": any(
            "curated_task_revalidation" in item.artifact_id
            for item in old_test_repetitions.payload.get("details", {}).get(
                "inputs", []
            )
        )
        or "curated" in json.dumps(old_test_repetitions.payload).casefold(),
        "annotation_state": old_annotation.payload.get("state"),
        "checkpoint_state": old_checkpoint.payload.get("state"),
        "sprint_admissible": old_sprint.payload.get("admissible"),
        "downstream_status": old_status.payload.get("state"),
        "frame_receipt_inventory": _old_frame_inventory(artifact_root, old_attempts),
        "exclusion_reasons": [
            "old attempt receipts have empty typed inputs",
            "old aggregate binds only old version materialization and old attempts",
            "old hidden oracle and construction receipts bind the old aggregate, not fresh current-v2 client receipts",
            "old frozen replay current head is terminal render_readiness_timeout",
            "no-inherit policy excludes old curated test/repetition lineage",
        ],
    }


def _inspect_apps(workspace: Path, profile: StableArtifact) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_root": str(workspace),
        "profile_build_client": profile.payload.get("build_mode", {}).get(
            "BUILD_CLIENT"
        ),
        "fixed": _inspect_variant(workspace, profile, "fixed"),
        "buggy": _inspect_variant(workspace, profile, "buggy"),
    }
    result["capture_backend_bound"] = all(
        result[variant].get("capture_backend_bound") is True
        for variant in ("fixed", "buggy")
    )
    return result


def _inspect_variant(
    workspace: Path, profile: StableArtifact, variant: str
) -> dict[str, Any]:
    key = "fixed_tree_path" if variant == "fixed" else "buggy_tree_path"
    tree = workspace / Path(str(profile.payload[key]))
    app = tree / "build-app/out/luanti.app"
    app_binary = app / "Contents/MacOS/luanti"
    root_binary = tree / "bin/luanti"
    root_sidecar = root_binary.with_name(root_binary.name + ".warptest-build.json")
    app_sidecar = app_binary.with_name(app_binary.name + ".warptest-build.json")
    source_game = tree / "src/client/game.cpp"
    source_window = tree / "irr/src/CIrrDeviceSDL.cpp"
    required = {relative: _is_ordinary(app / relative) for relative in _APP_RESOURCES}
    app_bytes = _read_bytes(app_binary)
    root_bytes = _read_bytes(root_binary)
    game_text = _read_text(source_game)
    window_text = _read_text(source_window)
    source_hooks = {
        name: all(marker in text for marker in markers)
        for name, (text, markers) in (
            ("game_capture_hook", (game_text, _SOURCE_MARKERS["game_capture_hook"])),
            (
                "hidden_window_hook",
                (window_text, _SOURCE_MARKERS["hidden_window_hook"]),
            ),
        )
    }
    app_markers = {
        marker.decode("ascii"): marker in app_bytes for marker in _BACKGROUND_MARKERS
    }
    root_markers = {
        marker.decode("ascii"): marker in root_bytes for marker in _BACKGROUND_MARKERS
    }
    current_identity = None
    try:
        current_identity = luanti_client_source_identity(tree)
    except (OSError, RuntimeError, ValueError):
        pass
    sidecar_identity = None
    sidecar_valid_json = False
    try:
        sidecar_payload = json.loads(root_sidecar.read_text(encoding="utf-8"))
        if isinstance(sidecar_payload, dict):
            sidecar_identity = sidecar_payload.get("source_identity")
            sidecar_valid_json = True
    except (OSError, json.JSONDecodeError):
        pass
    root_identity_matches = (
        current_identity is not None
        and sidecar_identity is not None
        and sidecar_identity == current_identity
    )
    return {
        "tree_path": str(tree),
        "bundle_exists": _is_ordinary(app),
        "bundle_resources": required,
        "bundle_complete": all(required.values()) and _is_ordinary(app_binary),
        "bundle_links_resolve": _bundle_links_resolve(app),
        "app_executable_exists": _is_ordinary(app_binary),
        "app_sidecar_present": _is_ordinary(app_sidecar),
        "root_client_exists": _is_ordinary(root_binary),
        "root_client_sidecar_present": _is_ordinary(root_sidecar),
        "root_sidecar_valid_json": sidecar_valid_json,
        "root_sidecar_source_identity_matches_current": root_identity_matches,
        "app_contains_current_background_markers": all(app_markers.values()),
        "root_client_contains_current_background_markers": all(root_markers.values()),
        "app_background_markers": app_markers,
        "root_background_markers": root_markers,
        "source_hooks_present": source_hooks,
        "app_differs_from_root_client": bool(
            app_bytes and root_bytes and app_bytes != root_bytes
        ),
        "capture_backend_bound": False,
        "client_build_receipt_present": False,
        "client_build_provenance_ready": False,
    }


def _old_frame_inventory(
    artifact_root: Path, attempts: tuple[StableArtifact, ...]
) -> dict[str, Any]:
    """Record static path presence for old receipts; never admit frames."""

    rows: list[dict[str, Any]] = []
    present_count = 0
    required_count = 0
    for attempt in attempts:
        variant = str(attempt.payload.get("version", "unknown"))
        repetition = attempt.payload.get("repetition")
        roles = (
            ("before", "aimed", "after")
            if variant == "fixed"
            else (
                "before",
                "after",
            )
        )
        present: dict[str, bool] = {}
        for role in roles:
            required_count += 1
            value = attempt.payload.get(f"{role}_frame_path")
            present[role] = _frame_path_present(artifact_root, value)
            present_count += int(present[role])
        rows.append(
            {
                "variant": variant,
                "repetition": repetition,
                "required_roles": list(roles),
                "present_roles": present,
            }
        )
    return {
        "required_frame_count": required_count,
        "present_frame_count": present_count,
        "all_required_frames_present": present_count == required_count,
        "receipts": rows,
    }


def _frame_path_present(artifact_root: Path, value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = artifact_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(artifact_root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return _is_ordinary(resolved) and resolved.stat().st_size > 0


def _bundle_links_resolve(bundle: Path) -> bool:
    if not _is_ordinary(bundle):
        return False
    root = bundle.resolve(strict=True)
    try:
        for path in root.rglob("*"):
            try:
                info = path.lstat()
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode):
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes() if _is_ordinary(path) else b""
    except OSError:
        return b""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if _is_ordinary(path) else ""
    except (OSError, UnicodeDecodeError):
        return ""


def _is_ordinary(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


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


__all__ = ["write_luanti_14555_rendered_lineage_verdict"]
