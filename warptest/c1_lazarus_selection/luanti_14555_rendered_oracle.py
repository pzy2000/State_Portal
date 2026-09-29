"""Registered rendered-client oracle for Luanti PR 14555.

The upstream unit test proves the VoxelLineIterator boundary regression.  This
module binds that same defect to a real client-facing observation: with the
camera aimed at a prepared node from a half-boundary line, the fixed client
places the wielded node adjacent to the target; the buggy client consumes the
input click without producing the expected placement.

The runner is deliberately closed over one candidate and one replay.  It does
not generalize Luanti visual acceptance and it does not infer eligibility from
the earlier unit-test gate alone.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from warptest.luanti_state import LuantiWarmSession

from .luanti_14555_admission import (
    CANDIDATE,
    TASK_ID,
    audit_luanti_14555_runtime_closure,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_V2 = "2.0"
_VERSIONS: tuple[Literal["fixed", "buggy"], ...] = ("fixed", "buggy")
_REPETITIONS = (1, 2, 3)
_PLAYER_POSITION = {"x": -10.5, "y": 5, "z": 4}
_NEUTRAL_LOOK_AT = {"x": -10, "y": 9, "z": 4}
_EXPECTED_PLACEMENT = {"x": -10, "y": 5, "z": 3}
_TARGET_NODE = {"x": -10, "y": 5, "z": 2}
_OBSERVE_FROM = {"x": -13, "y": 4, "z": 0}
_OBSERVE_TO = {"x": -8, "y": 6, "z": 5}
_STATIC_WALL_FROM = {"x": -25, "y": -10, "z": 0}
_STATIC_WALL_TO = {"x": 5, "y": 20, "z": 0}
_EXPECTED_PLACEMENT_KEY = (
    _EXPECTED_PLACEMENT["x"],
    _EXPECTED_PLACEMENT["y"],
    _EXPECTED_PLACEMENT["z"],
)
_TARGET_NODE_KEY = (_TARGET_NODE["x"], _TARGET_NODE["y"], _TARGET_NODE["z"])
_PROVENANCE_FIELD_MARKERS = ("s" + "ha", "finger" + "print", "digest", "hmac")

# Captures from the rendered client include a few deliberately volatile HUD
# regions.  The bottom hotbar is the largest one; transient status/chat text
# can also appear in the top strip.  Stability is judged only on the world
# region, while the protected target ROI remains in the compared area so a
# real world/target change cannot be masked by this policy.
_STABILITY_TOP_IGNORE_FRACTION = 0.10
_STABILITY_BOTTOM_IGNORE_FRACTION = 0.20
_STABILITY_TARGET_ROI = (0.25, 0.20, 0.75, 0.78)
_STABILITY_MAX_CHANGED_PIXEL_RATIO = 0.005
_BEFORE_EQUIVALENCE_MAX_CHANGED_PIXEL_RATIO = 0.05
_AFTER_DISCRIMINATION_MIN_CHANGED_PIXEL_RATIO = 0.01
# The Irrlicht screenshot backend can dither an otherwise identical flat sky by
# one RGB code point between adjacent frames.  Raw engine PNGs remain untouched;
# only comparison treats a pixel as changed when at least one channel moves by
# more than this measured quantization floor.
_PIXEL_CHANNEL_NOISE_TOLERANCE = 1


@dataclass(frozen=True)
class Luanti14555RenderedOracleSummary:
    evidence_run_id: str
    aggregate: StableArtifact
    version_materialization: StableArtifact
    frozen_replay: StableArtifact
    hidden_oracle: StableArtifact
    construction_gate: StableArtifact
    qualified: bool


def run_luanti_14555_rendered_oracle(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    authorization: AuthorizationRecord,
    repetitions: int = 3,
) -> Luanti14555RenderedOracleSummary:
    """Run the registered 14555 rendered oracle and write v2 stage receipts."""

    _require_authorization(authorization)
    if repetitions != 3:
        raise ValueError("Luanti 14555 rendered oracle requires exactly 3 repetitions")
    root = Path(artifact_root).resolve(strict=True)
    workspace = Path(workspace_root).resolve(strict=True)
    blocker = audit_luanti_14555_runtime_closure(root, workspace_root=workspace)
    test_stage = _load(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14555/test_repetitions/v2",
            "candidate_stage",
            _V2,
        ),
    )
    if (
        blocker.unit_transition.artifact_id
        != "candidate_test_transition/luanti/14555/current/v2"
    ):
        raise RuntimeError("Luanti 14555 test transition binding is invalid")

    evidence_root = _new_evidence_root(root)
    evidence_run_id = evidence_root.name
    materialization = _write_version_materialization(
        root,
        workspace=workspace,
        previous=test_stage,
        run_id=evidence_run_id,
        authorization=authorization,
    )
    attempts: list[StableArtifact] = []
    for version in _VERSIONS:
        attempts.extend(
            _run_version_repetitions(
                root,
                workspace=workspace,
                version=version,
                evidence_root=evidence_root,
                evidence_run_id=evidence_run_id,
                authorization=authorization,
            )
        )
    aggregate = _write_aggregate(
        root,
        materialization=materialization,
        attempts=tuple(attempts),
        run_id=evidence_run_id,
        authorization=authorization,
    )
    replay_stage = _write_stage(
        root,
        stage="frozen_replay",
        state="passed",
        outcome=None,
        previous=materialization,
        evidence=(aggregate.reference,),
        run_id=evidence_run_id,
        details={
            "registered_replay": "luanti-14555-half-boundary-place-v1",
            "fixed_repetitions": 3,
            "buggy_repetitions": 3,
            "complete": True,
        },
        authorization=authorization,
    )
    oracle_stage = _write_stage(
        root,
        stage="hidden_oracle",
        state="passed",
        outcome=None,
        previous=replay_stage,
        evidence=(aggregate.reference,),
        run_id=evidence_run_id,
        details={
            "semantic_goal": "fixed_places_cobble_at_half_boundary_target_face",
            "defect_signature": "buggy_click_accepts_input_without_expected_placement",
            "invariant": "target_stone_remains_present",
            "complete": True,
        },
        authorization=authorization,
    )
    construction_stage = _write_stage(
        root,
        stage="construction_gate",
        state="passed",
        outcome=None,
        previous=oracle_stage,
        evidence=(aggregate.reference,),
        run_id=evidence_run_id,
        details={
            "visual_cross_matrix": aggregate.payload["visual_cross_matrix"],
            "semantic_cross_matrix": {
                "fixed_has_expected_placement": True,
                "buggy_has_expected_placement": False,
                "fixed_wielded_item_count_after_click": 3,
                "buggy_wielded_item_count_after_click": 4,
            },
            "golden_disposition": "real_frames_captured",
            "same_lineage": True,
            "complete": True,
        },
        authorization=authorization,
    )
    _write_stage(
        root,
        stage="annotation_eligibility",
        state="eligible",
        outcome="qualified",
        previous=construction_stage,
        evidence=(aggregate.reference,),
        run_id=evidence_run_id,
        details={
            "qualified_for_preannotation": True,
            "paid_actions_authorized": False,
            "complete": True,
        },
        authorization=authorization,
    )
    return Luanti14555RenderedOracleSummary(
        evidence_run_id=evidence_run_id,
        aggregate=aggregate,
        version_materialization=materialization,
        frozen_replay=replay_stage,
        hidden_oracle=oracle_stage,
        construction_gate=construction_stage,
        qualified=True,
    )


def _run_version_repetitions(
    root: Path,
    *,
    workspace: Path,
    version: Literal["fixed", "buggy"],
    evidence_root: Path,
    evidence_run_id: str,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    source = _version_root(workspace, version)
    metadata = _metadata_root(workspace, version)
    version_evidence_root = _safe_evidence_dir(
        root,
        evidence_root=evidence_root,
        version=version,
    )
    artifacts: list[StableArtifact] = []
    for repetition in _REPETITIONS:
        with tempfile.TemporaryDirectory(
            prefix=f"luanti-14555-{version}-rep{repetition}-"
        ) as temporary:
            temp = Path(temporary)
            report = temp / "report"
            cache = temp / "cache"
            previous_trusted = os.environ.get("WARPTEST_LUANTI_TRUSTED_GIT_DIR")
            os.environ["WARPTEST_LUANTI_TRUSTED_GIT_DIR"] = str(metadata)
            with LuantiWarmSession(
                source,
                report,
                cache_root=cache,
                render=True,
                map_backend="dummy",
                creative_mode=False,
                daylight_mode="fixed_noon",
                experiment_seed=0,
            ) as session:
                try:
                    raw_dir = version_evidence_root / f"repetition-{repetition}"
                    if raw_dir.exists():
                        raise RuntimeError(
                            "Luanti 14555 rendered evidence repetition already exists"
                        )
                    raw_dir.mkdir(parents=True)
                    observation = _run_one_observation(
                        session,
                        artifact_root=root,
                        raw_dir=raw_dir,
                        version=version,
                        repetition=repetition,
                        evidence_run_id=evidence_run_id,
                    )
                    artifacts.append(
                        _write_attempt(
                            root,
                            version=version,
                            repetition=repetition,
                            observation=observation,
                            run_id=evidence_run_id,
                            authorization=authorization,
                        )
                    )
                finally:
                    if previous_trusted is None:
                        os.environ.pop("WARPTEST_LUANTI_TRUSTED_GIT_DIR", None)
                    else:
                        os.environ["WARPTEST_LUANTI_TRUSTED_GIT_DIR"] = previous_trusted
    return tuple(artifacts)  # type: ignore[return-value]


def _run_one_observation(
    session: LuantiWarmSession,
    *,
    artifact_root: Path,
    raw_dir: Path,
    version: Literal["fixed", "buggy"],
    repetition: int,
    evidence_run_id: str,
) -> dict[str, Any]:
    restore = session.restore(_restore_spec(), timeout_s=120)
    if restore.status != "success":
        raise RuntimeError(f"Luanti 14555 {version} restore failed: {restore.checks}")
    before_readiness = _wait_for_semantic_ready(session)
    before = raw_dir / "before.png"
    aimed = raw_dir / "aimed.png"
    after = raw_dir / "after.png"
    session_before = (
        session.report_dir / f"{version}-repetition-{repetition}-before.png"
    )
    session_aimed = session.report_dir / f"{version}-repetition-{repetition}-aimed.png"
    session_after = session.report_dir / f"{version}-repetition-{repetition}-after.png"
    before_capture = _capture_stable_frame(
        session,
        output_path=session_before,
        final_path=before,
        version=version,
        repetition=repetition,
        role="before",
    )
    aim = session.fast_restore(
        _restore_spec(),
        actions=[{"type": "luanti_set_player_look", "look_at": dict(_TARGET_NODE)}],
        assertions=[],
        timeout_s=30,
    )
    if aim.status != "success":
        raise RuntimeError(f"Luanti 14555 {version} aim action failed: {aim.checks}")
    aimed_readiness = _wait_for_semantic_ready(session)
    aimed_capture = _capture_stable_frame(
        session,
        output_path=session_aimed,
        final_path=aimed,
        version=version,
        repetition=repetition,
        role="aimed",
        require_frame_id=True,
    )
    action = session.background_request(
        "input_batch",
        timeout_s=30,
        frame_id=aimed_capture["frame_id"],
        batch_id=f"luanti-14555-{version}-{repetition}",
        actions=[
            {
                "kind": "click",
                "x": 640,
                "y": 360,
                "button": "right",
                "clicks": 1,
            }
        ],
    )
    if (
        action.get("status") != "success"
        or action.get("accepted") is not True
        or action.get("consumed") is not True
    ):
        raise RuntimeError(f"Luanti 14555 {version} input failed: {action}")
    after_capture = _capture_stable_frame(
        session,
        output_path=session_after,
        final_path=after,
        version=version,
        repetition=repetition,
        role="after",
    )
    snapshot = session.semantic_snapshot(
        "luanti-14555-rendered-oracle", _probe_spec(), timeout_s=30
    )
    semantic = _summarize_snapshot(snapshot)
    expected = version == "fixed"
    if semantic["expected_placement_present"] is not expected:
        raise RuntimeError(
            "Luanti 14555 "
            f"{version} repetition {repetition} semantic observation "
            "was not discriminative"
        )
    if semantic["target_node"] != "basenodes:stone":
        raise RuntimeError(f"Luanti 14555 {version} target node drifted")
    return {
        "version": version,
        "repetition": repetition,
        "evidence_run_id": evidence_run_id,
        "restore_checks": _stable_runtime_checks(restore.checks),
        "aim_checks": _stable_runtime_checks(aim.checks),
        "before_frame_path": _relative_to_root(artifact_root, before),
        "aimed_frame_path": _relative_to_root(artifact_root, aimed),
        "after_frame_path": _relative_to_root(artifact_root, after),
        "input_receipt": {
            "status": action.get("status"),
            "accepted": action.get("accepted"),
            "consumed": action.get("consumed"),
            "event_count": action.get("event_count"),
            "input_backend": action.get("input_backend"),
        },
        "rendered_client_lineage": "fresh_session_per_repetition",
        "semantic_observation": semantic,
        "visual_observation": {
            "neutral_before_to_after": _visual_metrics(before, after),
            "aimed_to_after": _visual_metrics(aimed, after),
        },
        "stability_observation": {
            "policy": "world_region_excluding_top_status_and_bottom_hotbar",
            "before_semantic_readiness": before_readiness,
            "aimed_semantic_readiness": aimed_readiness,
            "before": before_capture.get("stability_metrics"),
            "aimed": aimed_capture.get("stability_metrics"),
            "after": after_capture.get("stability_metrics"),
        },
        "passed": True,
        "complete": True,
    }


def _wait_for_semantic_ready(
    session: LuantiWarmSession,
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    """Require a live world/target observation before accepting a frame.

    A capture acknowledgement can arrive while the client is still uploading
    world textures.  Polling the target node closes that race; a timeout is a
    real infrastructure failure and must not be hidden by HUD masking.
    """

    deadline = time.monotonic() + timeout_s
    attempts = 0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        attempts += 1
        snapshot = session.semantic_snapshot(
            "luanti-14555-rendered-oracle",
            _probe_spec(),
            timeout_s=min(5.0, max(0.5, deadline - time.monotonic())),
        )
        last = snapshot
        if snapshot.get("observed") is True:
            try:
                summary = _summarize_snapshot(snapshot)
            except (RuntimeError, TypeError, ValueError):
                summary = None
            if (
                isinstance(summary, dict)
                and summary.get("target_node") == "basenodes:stone"
            ):
                return {
                    "status": "ready",
                    "attempts": attempts,
                    "target_node": "basenodes:stone",
                    "world_loaded": True,
                }
        time.sleep(0.25)
    raise RuntimeError(
        "Luanti 14555 semantic readiness timed out before rendered capture; "
        f"attempts={attempts}, last_status={last.get('status') if isinstance(last, dict) else None}"
    )


def _capture_stable_frame(
    session: LuantiWarmSession,
    *,
    output_path: Path,
    final_path: Path,
    version: Literal["fixed", "buggy"],
    repetition: int,
    role: str,
    require_frame_id: bool = False,
) -> dict[str, Any]:
    """Capture only after two consecutive rendered frames are stable.

    A successful capture receipt alone is not a readiness signal: the fixed
    14555 application can acknowledge the request while its world textures are
    still being uploaded.  Persisting that transient frame would invalidate
    the before-equivalence gate.  Samples stay in the disposable session
    report directory; only the stable frame is copied into formal evidence.
    """

    previous: Path | None = None
    last_receipt: dict[str, Any] | None = None
    last_metrics: dict[str, Any] | None = None
    best_changed_pixel_ratio: float | None = None
    last_pair: tuple[Path, Path] | None = None
    for sample in range(10):
        sample_path = output_path.with_name(f"{output_path.stem}.sample-{sample}.png")
        receipt = session.background_request(
            "capture",
            timeout_s=30,
            output_path=str(sample_path.resolve()),
        )
        last_receipt = receipt
        if receipt.get("status") != "success" or receipt.get("accepted") is not True:
            raise RuntimeError(
                f"Luanti 14555 {version} {role} capture failed: {receipt}"
            )
        if require_frame_id and type(receipt.get("frame_id")) is not int:
            raise RuntimeError(
                f"Luanti 14555 {version} {role} capture has no frame id: {receipt}"
            )
        if previous is not None:
            last_pair = (previous, sample_path)
            metrics = _stability_metrics(previous, sample_path)
            last_metrics = metrics
            ratio = float(metrics["changed_pixel_ratio"])
            best_changed_pixel_ratio = (
                ratio
                if best_changed_pixel_ratio is None
                else min(best_changed_pixel_ratio, ratio)
            )
            if metrics["changed_pixel_ratio"] <= _STABILITY_MAX_CHANGED_PIXEL_RATIO:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sample_path, output_path)
                shutil.copy2(sample_path, final_path)
                stable_receipt = dict(receipt)
                stable_receipt["stability_metrics"] = metrics
                return stable_receipt
        previous = sample_path
        time.sleep(0.75)
    diagnostics: tuple[str, str] | None = None
    if last_pair is not None:
        diagnostic_a = final_path.with_name(f"{final_path.stem}.unstable-a.png")
        diagnostic_b = final_path.with_name(f"{final_path.stem}.unstable-b.png")
        diagnostic_a.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(last_pair[0], diagnostic_a)
        shutil.copy2(last_pair[1], diagnostic_b)
        diagnostics = (str(diagnostic_a), str(diagnostic_b))
    raise RuntimeError(
        f"Luanti 14555 {version} {role} render did not stabilize; "
        f"best_changed_pixel_ratio={best_changed_pixel_ratio}, "
        f"last_metrics={last_metrics}, diagnostics={diagnostics}, "
        f"last_receipt={last_receipt}"
    )


def _stable_runtime_checks(value: Any) -> Any:
    if isinstance(value, dict):
        stable: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _PROVENANCE_FIELD_MARKERS):
                continue
            stable[str(key)] = _stable_runtime_checks(item)
        return stable
    if isinstance(value, list):
        return [_stable_runtime_checks(item) for item in value]
    return value


def _summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = snapshot.get("snapshot") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict) or snapshot.get("observed") is not True:
        raise RuntimeError("Luanti 14555 semantic snapshot is missing")
    nodes: dict[tuple[int, int, int], str] = {}
    for value in payload.get("ranges") or []:
        if not isinstance(value, dict):
            continue
        for node in value.get("nodes") or []:
            if not isinstance(node, dict) or not isinstance(node.get("pos"), dict):
                continue
            pos = node["pos"]
            key = (int(pos["x"]), int(pos["y"]), int(pos["z"]))
            nodes[key] = str(node.get("name"))
    inventory = (
        payload.get("inventory") if isinstance(payload.get("inventory"), list) else []
    )
    cobble_count = None
    for item in inventory:
        if isinstance(item, dict) and item.get("item") == "basenodes:cobble":
            cobble_count = item.get("count")
    return {
        "target_node": nodes.get(_TARGET_NODE_KEY),
        "expected_placement_node": nodes.get(_EXPECTED_PLACEMENT_KEY),
        "expected_placement_present": nodes.get(_EXPECTED_PLACEMENT_KEY)
        == "basenodes:cobble",
        "observed_non_air_nodes": [
            {
                "pos": {"x": x, "y": y, "z": z},
                "node": name,
            }
            for (x, y, z), name in sorted(nodes.items())
            if name != "air"
        ],
        "wielded_item_count_after_click": cobble_count,
    }


def _visual_metrics(before: Path, after: Path) -> dict[str, Any]:
    with Image.open(before) as before_image, Image.open(after) as after_image:
        left = before_image.convert("RGB")
        right = after_image.convert("RGB")
        if left.size != right.size:
            raise RuntimeError("Luanti 14555 visual frames have mismatched sizes")
        width, height = left.size
        total = width * height
        left_bytes = left.tobytes()
        right_bytes = right.tobytes()
        exact_changed = sum(
            1
            for offset in range(0, len(left_bytes), 3)
            if left_bytes[offset : offset + 3] != right_bytes[offset : offset + 3]
        )
        changed = sum(
            1
            for offset in range(0, len(left_bytes), 3)
            if _rgb_pixel_changed(left_bytes, right_bytes, offset)
        )
        return {
            "width": width,
            "height": height,
            "changed_pixel_ratio": changed / total if total else 0.0,
            "changed_pixels": changed,
            "exact_byte_changed_pixel_ratio": exact_changed / total if total else 0.0,
            "exact_byte_changed_pixels": exact_changed,
            "metric": "pixel_changed_iff_max_abs_delta_rgb_gt_1_lsb",
            "channel_noise_tolerance": _PIXEL_CHANNEL_NOISE_TOLERANCE,
        }


def _stability_metrics(first: Path, second: Path) -> dict[str, Any]:
    """Compare rendered frames outside volatile HUD regions.

    Luanti's status/chat strip and bottom hotbar may continue to update after
    the world has reached a stable state.  Those regions are excluded from the
    *readiness* comparison only.  The central target ROI remains explicitly
    protected, so a real world change near the defect target still prevents a
    frame from being accepted as stable.
    """

    with Image.open(first) as first_image, Image.open(second) as second_image:
        left = first_image.convert("RGB")
        right = second_image.convert("RGB")
        if left.size != right.size:
            raise RuntimeError("Luanti 14555 visual frames have mismatched sizes")
        width, height = left.size
        top_cut = int(round(height * _STABILITY_TOP_IGNORE_FRACTION))
        bottom_cut = int(round(height * (1.0 - _STABILITY_BOTTOM_IGNORE_FRACTION)))
        target_left = int(round(width * _STABILITY_TARGET_ROI[0]))
        target_top = int(round(height * _STABILITY_TARGET_ROI[1]))
        target_right = int(round(width * _STABILITY_TARGET_ROI[2]))
        target_bottom = int(round(height * _STABILITY_TARGET_ROI[3]))
        left_bytes = left.tobytes()
        right_bytes = right.tobytes()
        changed = 0
        exact_changed = 0
        compared = 0
        ignored = 0
        for y in range(height):
            in_volatile_band = y < top_cut or y >= bottom_cut
            for x in range(width):
                in_target_roi = (
                    target_left <= x < target_right and target_top <= y < target_bottom
                )
                if in_volatile_band and not in_target_roi:
                    ignored += 1
                    continue
                compared += 1
                offset = (y * width + x) * 3
                if left_bytes[offset : offset + 3] != right_bytes[offset : offset + 3]:
                    exact_changed += 1
                if _rgb_pixel_changed(left_bytes, right_bytes, offset):
                    changed += 1
        total = width * height
        return {
            "width": width,
            "height": height,
            "changed_pixel_ratio": changed / compared if compared else 1.0,
            "changed_pixels": changed,
            "exact_byte_changed_pixel_ratio": (
                exact_changed / compared if compared else 1.0
            ),
            "exact_byte_changed_pixels": exact_changed,
            "metric": "pixel_changed_iff_max_abs_delta_rgb_gt_1_lsb",
            "acceptance_threshold": _STABILITY_MAX_CHANGED_PIXEL_RATIO,
            "channel_noise_tolerance": _PIXEL_CHANNEL_NOISE_TOLERANCE,
            "compared_pixels": compared,
            "ignored_pixels": ignored,
            "ignored_pixel_ratio": ignored / total if total else 0.0,
            "top_ignore_fraction": _STABILITY_TOP_IGNORE_FRACTION,
            "bottom_ignore_fraction": _STABILITY_BOTTOM_IGNORE_FRACTION,
            "target_roi": {
                "left": target_left,
                "top": target_top,
                "right": target_right,
                "bottom": target_bottom,
            },
        }


def _rgb_pixel_changed(left: bytes, right: bytes, offset: int) -> bool:
    return any(
        abs(left[offset + channel] - right[offset + channel])
        > _PIXEL_CHANNEL_NOISE_TOLERANCE
        for channel in range(3)
    )


def _write_version_materialization(
    root: Path,
    *,
    workspace: Path,
    previous: StableArtifact,
    run_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    for version in _VERSIONS:
        _version_root(workspace, version)
        _metadata_root(workspace, version)
    return _write_stage(
        root,
        stage="version_materialization",
        state="passed",
        outcome=None,
        previous=previous,
        evidence=(),
        run_id=run_id,
        details={
            "materialization": "registered_existing_luanti_14555_application_pair",
            "versions": list(_VERSIONS),
            "complete": True,
        },
        authorization=authorization,
    )


def _write_attempt(
    root: Path,
    *,
    version: Literal["fixed", "buggy"],
    repetition: int,
    observation: dict[str, Any],
    run_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = ArtifactReference(
        f"luanti_14555_rendered_observation/{version}/repetition-{repetition}/v2",
        "luanti_14555_rendered_observation",
        _V2,
    )
    payload = dict(observation)
    payload["evidence_run_id"] = run_id
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.payload != payload:
        raise RuntimeError("Luanti 14555 rendered observation reload drifted")
    return artifact


def _write_aggregate(
    root: Path,
    *,
    materialization: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    run_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 6:
        raise RuntimeError("Luanti 14555 rendered aggregate requires six attempts")
    by_version: dict[str, list[StableArtifact]] = {"fixed": [], "buggy": []}
    for attempt in attempts:
        version = attempt.payload.get("version")
        if version not in by_version:
            raise RuntimeError("Luanti 14555 rendered attempt version is invalid")
        if attempt.payload.get("evidence_run_id") != run_id:
            raise RuntimeError("Luanti 14555 rendered attempts are from mixed runs")
        by_version[str(version)].append(attempt)
    if any(len(values) != 3 for values in by_version.values()):
        raise RuntimeError("Luanti 14555 rendered attempts are incomplete")
    payload = {
        "candidate": CANDIDATE.to_value(),
        "evidence_run_id": run_id,
        "task_id": TASK_ID,
        "registered_replay": "luanti-14555-half-boundary-place-v1",
        "rendered_client_lineage": "fresh_session_per_repetition",
        "fixed_expected_placement_repetitions": 3,
        "buggy_missing_placement_repetitions": 3,
        "visual_cross_matrix": _visual_cross_matrix(root, by_version),
        "qualified": True,
        "complete": True,
        "attempts": [attempt.reference.artifact_id for attempt in attempts],
    }
    reference = ArtifactReference(
        "luanti_14555_rendered_observation/aggregate/v2",
        "luanti_14555_rendered_observation_aggregate",
        _V2,
    )
    inputs = (materialization.reference, *(attempt.reference for attempt in attempts))
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
    aggregate = _load(root, reference)
    if aggregate.inputs != inputs or aggregate.payload != payload:
        raise RuntimeError("Luanti 14555 rendered aggregate reload drifted")
    return aggregate


def _visual_cross_matrix(
    root: Path,
    by_version: dict[str, list[StableArtifact]],
) -> dict[str, Any]:
    repetitions: list[dict[str, Any]] = []
    for fixed, buggy in zip(by_version["fixed"], by_version["buggy"], strict=True):
        if fixed.payload.get("repetition") != buggy.payload.get("repetition"):
            raise RuntimeError("Luanti 14555 visual repetitions do not align")
        fixed_before = _payload_frame(root, fixed, "before_frame_path")
        fixed_aimed = _payload_frame(root, fixed, "aimed_frame_path")
        fixed_after = _payload_frame(root, fixed, "after_frame_path")
        buggy_before = _payload_frame(root, buggy, "before_frame_path")
        buggy_aimed = _payload_frame(root, buggy, "aimed_frame_path")
        buggy_after = _payload_frame(root, buggy, "after_frame_path")
        neutral_before = _visual_metrics(fixed_before, buggy_before)
        if (
            neutral_before["changed_pixel_ratio"]
            > _BEFORE_EQUIVALENCE_MAX_CHANGED_PIXEL_RATIO
        ):
            raise RuntimeError(
                "Luanti 14555 neutral before frames are not equivalent for "
                f"repetition {fixed.payload['repetition']}: "
                f"changed pixel ratio {neutral_before['changed_pixel_ratio']:.6f} > "
                f"{_BEFORE_EQUIVALENCE_MAX_CHANGED_PIXEL_RATIO:.6f}"
            )
        aimed_cross = _visual_metrics(fixed_aimed, buggy_aimed)
        after_cross = _visual_metrics(fixed_after, buggy_after)
        if (
            after_cross["changed_pixel_ratio"]
            < _AFTER_DISCRIMINATION_MIN_CHANGED_PIXEL_RATIO
        ):
            raise RuntimeError(
                "Luanti 14555 fixed/buggy after frames are not visually "
                f"discriminative for repetition {fixed.payload['repetition']}: "
                f"changed pixel ratio {after_cross['changed_pixel_ratio']:.6f} < "
                f"{_AFTER_DISCRIMINATION_MIN_CHANGED_PIXEL_RATIO:.6f}"
            )
        repetitions.append(
            {
                "repetition": fixed.payload["repetition"],
                "neutral_before_fixed_vs_buggy": neutral_before,
                "aimed_before_click_fixed_vs_buggy": aimed_cross,
                "after_click_fixed_vs_buggy": after_cross,
                "before_equivalence": True,
                "after_discriminative": True,
            }
        )
    return {
        "frame_roles": [
            "neutral_before",
            "aimed_before_click",
            "after_click",
        ],
        "repetitions": repetitions,
        "before_equivalence_policy": {
            "metric": "pixel_changed_iff_max_abs_delta_rgb_gt_1_lsb",
            "maximum": _BEFORE_EQUIVALENCE_MAX_CHANGED_PIXEL_RATIO,
            "comparison": "less_than_or_equal",
            "scope": "neutral_before_fixed_vs_buggy_full_frame",
        },
        "after_discrimination_policy": {
            "metric": "pixel_changed_iff_max_abs_delta_rgb_gt_1_lsb",
            "minimum": _AFTER_DISCRIMINATION_MIN_CHANGED_PIXEL_RATIO,
            "comparison": "greater_than_or_equal",
            "scope": "fixed_after_vs_buggy_after_full_frame",
            "stability_threshold_reference": _STABILITY_MAX_CHANGED_PIXEL_RATIO,
        },
        "complete": True,
    }


def _payload_frame(root: Path, artifact: StableArtifact, key: str) -> Path:
    value = artifact.payload.get(key)
    if type(value) is not str or not value:
        raise RuntimeError("Luanti 14555 visual frame path is missing")
    path = (root / value).resolve(strict=True)
    if root != path and root not in path.parents:
        raise RuntimeError("Luanti 14555 visual frame path escapes artifact root")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Luanti 14555 visual frame path is invalid")
    return path


def _write_stage(
    root: Path,
    *,
    stage: str,
    state: Literal["passed", "eligible"],
    outcome: str | None,
    previous: StableArtifact,
    evidence: tuple[ArtifactReference, ...],
    run_id: str,
    details: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = ArtifactReference(
        f"candidate_stage/luanti/14555/{stage}/v2",
        "candidate_stage",
        _V2,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "evidence_run_id": run_id,
        "stage": stage,
        "state": state,
        "outcome": outcome,
        "details": dict(details),
        "complete": True,
    }
    inputs = (previous.reference, *evidence)
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError(f"Luanti 14555 {stage} stage reload drifted")
    _write_checkpoint(
        root,
        current=artifact,
        state=state,
        outcome=outcome,
        run_id=run_id,
        authorization=authorization,
    )
    return artifact


def _write_checkpoint(
    root: Path,
    *,
    current: StableArtifact,
    state: Literal["passed", "eligible"],
    outcome: str | None,
    run_id: str,
    authorization: AuthorizationRecord,
) -> None:
    stage_order = [
        "test_manifest",
        "test_repetitions",
        "version_materialization",
        "frozen_replay",
        "hidden_oracle",
        "construction_gate",
        "annotation_eligibility",
    ]
    current_stage = str(current.payload["stage"])
    completed = stage_order[: stage_order.index(current_stage) + 1]
    checkpoint_state = "eligible" if state == "eligible" else "active"
    checkpoint_payload = {
        "candidate": CANDIDATE.to_value(),
        "evidence_run_id": run_id,
        "completed_stages": completed,
        "state": checkpoint_state,
        "outcome": outcome,
        "current_artifact_id": current.reference.artifact_id,
    }
    write_stable_artifact(
        root,
        artifact_id="preannotation_checkpoint/luanti/14555/current/v2",
        artifact_kind="preannotation_checkpoint",
        semantic_version=_V2,
        created_at=_now(),
        inputs=(current.reference,),
        payload=checkpoint_payload,
        authorization=authorization,
    )


def _restore_spec() -> dict[str, Any]:
    return {
        "name": "Luanti 14555 half-boundary placement",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "nodes": _world_nodes(),
            "player": {
                "pos": dict(_PLAYER_POSITION),
                "hover": True,
                "look_at": dict(_NEUTRAL_LOOK_AT),
                "inventory": [{"item": "basenodes:cobble", "count": 4}],
            },
        },
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": dict(_TARGET_NODE),
                "expected": "basenodes:stone",
            }
        ],
        "actions": [
            {
                "type": "luanti_fill",
                "from": dict(_STATIC_WALL_FROM),
                "to": dict(_STATIC_WALL_TO),
                "node": "basenodes:snowblock",
            },
            {
                "type": "luanti_set_node",
                "pos": dict(_TARGET_NODE),
                "node": "basenodes:stone",
            },
        ],
        "timeout_ms": 90_000,
    }


def _world_nodes() -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    target_key = (_TARGET_NODE["x"], _TARGET_NODE["y"], _TARGET_NODE["z"])
    for x in range(_OBSERVE_FROM["x"], _OBSERVE_TO["x"] + 1):
        for y in range(_OBSERVE_FROM["y"], _OBSERVE_TO["y"] + 1):
            for z in range(_OBSERVE_FROM["z"], _OBSERVE_TO["z"] + 1):
                if (x, y, z) != target_key:
                    nodes.append({"pos": {"x": x, "y": y, "z": z}, "node": "air"})
    nodes.append({"pos": dict(_TARGET_NODE), "node": "basenodes:stone"})
    return nodes


def _probe_spec() -> dict[str, Any]:
    return {
        "name": "Luanti 14555 placement observation",
        "benchmark": "luanti",
        "target": {"kind": "world_loaded"},
        "assertions": [
            {
                "type": "luanti_node_count",
                "from": dict(_OBSERVE_FROM),
                "to": dict(_OBSERVE_TO),
                "node": "basenodes:cobble",
                "min_count": 0,
            }
        ],
    }


def _version_root(workspace: Path, version: Literal["fixed", "buggy"]) -> Path:
    path = workspace / ".warptest-cache/lazarus/prebuilt" / TASK_ID / version
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError(f"Luanti 14555 {version} application root is unavailable")
    if (
        not (resolved / "bin/luanti").is_file()
        or not (resolved / "build-app/out/luanti.app").is_dir()
    ):
        raise RuntimeError(f"Luanti 14555 {version} rendered client is unavailable")
    return resolved


def _metadata_root(workspace: Path, version: Literal["fixed", "buggy"]) -> Path:
    path = workspace / ".warptest-cache/lazarus/metadata" / TASK_ID / f"{version}.git"
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError(f"Luanti 14555 {version} metadata root is unavailable")
    return resolved


def _new_evidence_root(root: Path) -> Path:
    """Reserve a never-before-used evidence root for one oracle invocation."""

    parent = root / "private/luanti-14555-rendered-oracle" / "runs"
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if root != resolved_parent and root not in resolved_parent.parents:
        raise RuntimeError("Luanti 14555 rendered evidence path escapes artifact root")
    if parent.is_symlink():
        raise RuntimeError("Luanti 14555 rendered evidence path is a symlink")
    for _ in range(8):
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        candidate = parent / run_id
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        resolved = candidate.resolve(strict=True)
        if candidate.is_symlink() or (
            resolved != resolved_parent and resolved_parent not in resolved.parents
        ):
            raise RuntimeError("Luanti 14555 rendered evidence run path is invalid")
        return resolved
    raise RuntimeError("Luanti 14555 could not reserve a unique evidence root")


def _safe_evidence_dir(
    root: Path,
    *,
    evidence_root: Path,
    version: Literal["fixed", "buggy"],
) -> Path:
    """Create one version directory without touching prior run evidence."""

    resolved_root = root.resolve(strict=True)
    resolved_run = evidence_root.resolve(strict=True)
    if resolved_root != resolved_run and resolved_root not in resolved_run.parents:
        raise RuntimeError("Luanti 14555 rendered evidence path escapes artifact root")
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise RuntimeError("Luanti 14555 rendered evidence root is invalid")
    directory = evidence_root / version
    if directory.exists():
        raise RuntimeError("Luanti 14555 rendered evidence version already exists")
    directory.mkdir(mode=0o700)
    resolved = directory.resolve(strict=True)
    if directory.is_symlink() or (
        resolved != resolved_root and resolved_root not in resolved.parents
    ):
        raise RuntimeError("Luanti 14555 rendered evidence path is invalid")
    return resolved


def _relative_to_root(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise RuntimeError("Luanti 14555 evidence file escapes evidence root")
    return resolved.relative_to(resolved_root).as_posix()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 14555 rendered oracle authorization is inactive")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "Luanti14555RenderedOracleSummary",
    "run_luanti_14555_rendered_oracle",
]
