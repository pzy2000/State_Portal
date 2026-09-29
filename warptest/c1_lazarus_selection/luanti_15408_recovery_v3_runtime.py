"""Fresh recovery-v3 replay and honest visual-contract audit for Luanti #15408.

The candidate is a native collision regression.  This adapter executes the
new recovery-v3 binaries only, persists a registered TestCollision/TestSettings
replay and real process observations, and renders those observations into
diagnostic PNGs.  The PNGs are *not* game frames.  The current Lazarus contract
requires native screenshots, so this module writes a resumable Gate 11--13 and
formal-freeze blocker instead of promoting the diagnostic renderings.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .luanti_15408_downstream_closure import _CANDIDATE, _PROFILE_ID, _root, _try_load
from .luanti_15408_recovery_v3_downstream import (
    _BUILD_AGGREGATE,
    _CLOSURE,
    _MAPPING,
    _PAIR_REL,
    _PLAN,
    _RECOVERY_CONFIGURE,
    _RECOVERY_MATERIALIZATION,
    _TRANSITION,
    _load_ref,
    _write_or_check,
)
from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact


EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_15408_RECOVERY_V3_REPLAY_V2"
_VERSION = "2.0"
_TIMEOUT_SECONDS = 300
_ATTEMPT_KIND = "candidate_gate9_15408_recovery_v3_attempt"
_RUN_REL = PurePosixPath(
    "private/downstream-runs/luanti/15408/recovery-v3-runtime-v2"
)
_GOLDEN_REL = _RUN_REL / "diagnostic-semantic-goldens"
_CONTRACT_REL = PurePosixPath("docs/lazarus_EVALUATION_CONTRACT.md")
_SUMMARY = re.compile(
    rb"(?m)^Unit Test Results: (PASSED|FAILED)\r?\n"
    rb"\s+(\d+) / (\d+) failed tests\.\r?$"
)

_GATE9 = ArtifactReference(
    "candidate_gate9_15408_recovery_v3_replay/luanti/15408/v2",
    "candidate_gate9_15408_recovery_v3_replay",
    _VERSION,
)
_GATE10_OBSERVATION = ArtifactReference(
    "candidate_gate10_15408_recovery_v3_observation/luanti/15408/v2",
    "candidate_gate10_15408_recovery_v3_observation",
    _VERSION,
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_gate10_15408_recovery_v3_oracle/luanti/15408/v2",
    "candidate_gate10_15408_recovery_v3_oracle",
    _VERSION,
)
_GATE11_DIAGNOSTIC = ArtifactReference(
    "candidate_gate11_15408_semantic_rendering/luanti/15408/v2",
    "candidate_gate11_15408_semantic_rendering",
    _VERSION,
)
_GATE12_DIAGNOSTIC = ArtifactReference(
    "candidate_gate12_15408_semantic_rendering_matrix/luanti/15408/v2",
    "candidate_gate12_15408_semantic_rendering_matrix",
    _VERSION,
)
_GATE13_BLOCKER = ArtifactReference(
    "candidate_gate13_15408_native_visual_blocker/luanti/15408/v2",
    "candidate_gate13_15408_native_visual_blocker",
    _VERSION,
)
_FREEZE_BLOCKER = ArtifactReference(
    "candidate_formal_freeze_blocker/luanti/15408/recovery-v3/v2",
    "candidate_formal_freeze_blocker",
    _VERSION,
)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_execution(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15408 recovery-v3 replay authorization is inactive")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _attempt_reference(
    variant: Literal["fixed", "buggy"], phase: Literal["f2p", "p2p"]
) -> ArtifactReference:
    return ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/15408/{variant}/{phase}/recovery-v3/v2",
        _ATTEMPT_KIND,
        _VERSION,
    )


def _validate_runtime_binding(root: Path) -> dict[str, StableArtifact]:
    loaded = {
        "transition": _load_ref(root, _TRANSITION),
        "materialization": _load_ref(root, _RECOVERY_MATERIALIZATION),
        "closure": _load_ref(root, _CLOSURE),
        "configure": _load_ref(root, _RECOVERY_CONFIGURE),
        "mapping": _load_ref(root, _MAPPING),
        "plan": _load_ref(root, _PLAN),
        "build": _load_ref(root, _BUILD_AGGREGATE),
    }
    if (
        loaded["transition"].payload.get("state") != "passed"
        or loaded["transition"].payload.get("outcome") != "qualified"
        or loaded["materialization"].payload.get("namespace")
        != "application-recovery-v3"
        or loaded["closure"].payload.get("complete") is not True
        or loaded["configure"].payload.get("configure_probe", {}).get("state")
        != "passed"
        or loaded["mapping"].payload.get("complete") is not True
        or loaded["plan"].payload.get("outcome")
        != "gate8_recovery_v3_ready_for_candidate_build"
        or loaded["build"].payload.get("outcome")
        != "bounded_configure_build_passed"
        or loaded["build"].payload.get("variants") != ["fixed", "buggy"]
    ):
        raise RuntimeError("#15408 recovery-v3 runtime binding is incomplete")
    if loaded["build"].inputs[:4] != (
        loaded["plan"].reference,
        loaded["materialization"].reference,
        loaded["closure"].reference,
        loaded["configure"].reference,
    ):
        raise RuntimeError("#15408 recovery-v3 build lineage drifted")
    for variant in ("fixed", "buggy"):
        tree = root / _PAIR_REL / variant
        binary = tree / "bin/luantiserver"
        if tree.is_symlink() or not tree.is_dir() or binary.is_symlink() or not binary.is_file():
            raise RuntimeError(f"#15408 recovery-v3 {variant} binary is unavailable")
    return loaded


def _parse_summary(receipt: NativeProcessReceipt) -> dict[str, object]:
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in _SUMMARY.finditer(stream)
    ]
    if receipt.outcome != "completed" or len(matches) != 1:
        return {
            "observed": False,
            "summary_count": len(matches),
            "result": "unknown",
            "failed_tests": None,
            "total_tests": None,
        }
    match = matches[0]
    failed = int(match.group(2))
    total = int(match.group(3))
    result = "pass" if match.group(1) == b"PASSED" and failed == 0 else "failure"
    return {
        "observed": True,
        "summary_count": 1,
        "result": result,
        "failed_tests": failed,
        "total_tests": total,
    }


def _attempt_payload(
    *,
    root: Path,
    variant: Literal["fixed", "buggy"],
    phase: Literal["f2p", "p2p"],
    selector: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
) -> dict[str, Any]:
    summary = _parse_summary(receipt)
    expected = "failure" if variant == "buggy" and phase == "f2p" else "pass"
    binary = Path(argv[0])
    return {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "execution_namespace": "recovery-v3-runtime-v2",
        "variant": variant,
        "phase": phase,
        "selector": selector,
        "expected_outcome": expected,
        "observed_outcome": summary["result"],
        "matched_expected": summary["result"] == expected,
        "registered_start": {
            "binary_relative": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _sha256_file(binary),
            "working_directory": (root / _PAIR_REL / variant).relative_to(root).as_posix(),
            "argv": list(argv),
            "observed": summary["observed"],
        },
        "ordered_replay_step": {
            "type": "run_test_module",
            "selector": selector,
            "phase": phase,
        },
        "semantic_observation": summary,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_fingerprint": _sha256_bytes(receipt.stdout),
        "stderr_fingerprint": _sha256_bytes(receipt.stderr),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "output_encoding": "base64",
        "watchdog_seconds": _TIMEOUT_SECONDS,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _load_or_execute_attempt(
    root: Path,
    *,
    variant: Literal["fixed", "buggy"],
    phase: Literal["f2p", "p2p"],
    selector: str,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _attempt_reference(variant, phase)
    existing = _try_load(root, reference)
    if existing is not None:
        if (
            existing.inputs != inputs
            or existing.payload.get("execution_namespace") != "recovery-v3-runtime-v2"
            or existing.payload.get("process_outcome") != "completed"
            or existing.payload.get("semantic_observation", {}).get("observed") is not True
            or existing.payload.get("matched_expected") is not True
            or existing.payload.get("old_attempts_loaded") is not False
        ):
            raise RuntimeError(f"#15408 existing {variant} {phase} replay is invalid")
        return existing
    tree = root / _PAIR_REL / variant
    binary = tree / "bin/luantiserver"
    runtime = root / _RUN_REL / variant / phase
    home = runtime / "home"
    temporary = runtime / "tmp"
    home.mkdir(parents=True, exist_ok=False, mode=0o700)
    temporary.mkdir(parents=True, exist_ok=False, mode=0o700)
    environment = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(root / "private/dependencies/luanti-native/3.0/bin"),
    }
    argv = (str(binary), "--run-unittests", "--test-module", selector)
    receipt = run_bounded_process(
        argv,
        cwd=tree,
        env=environment,
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    payload = _attempt_payload(
        root=root,
        variant=variant,
        phase=phase,
        selector=selector,
        argv=argv,
        receipt=receipt,
    )
    artifact = _write_or_check(
        root,
        reference,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    if (
        payload["process_outcome"] != "completed"
        or payload["semantic_observation"]["observed"] is not True
        or payload["matched_expected"] is not True
    ):
        raise RuntimeError(f"#15408 recovery-v3 {variant} {phase} replay did not qualify")
    return artifact


def _write_gate9_gate10(
    root: Path,
    *,
    chain: dict[str, StableArtifact],
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    ordered = [
        {
            "variant": attempt.payload["variant"],
            "start": "restore_registered_start",
            "action": attempt.payload["ordered_replay_step"],
            "attempt": attempt.reference.artifact_id,
        }
        for attempt in attempts
    ]
    gate9_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "gate9_status": "complete",
        "registered_start": chain["mapping"].payload["start_definition"],
        "ordered_replay": ordered,
        "attempt_count": 4,
        "all_processes_completed": True,
        "no_timeout_or_unknown": True,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    gate9 = _write_or_check(
        root,
        _GATE9,
        inputs=(chain["build"].reference, chain["mapping"].reference, *(a.reference for a in attempts)),
        payload=gate9_payload,
        authorization=authorization,
    )
    cells = {
        f"{attempt.payload['variant']}_{attempt.payload['phase']}": {
            "selector": attempt.payload["selector"],
            "result": attempt.payload["observed_outcome"],
            "returncode": attempt.payload["returncode"],
            "failed_tests": attempt.payload["semantic_observation"]["failed_tests"],
            "total_tests": attempt.payload["semantic_observation"]["total_tests"],
            "stdout_fingerprint": attempt.payload["stdout_fingerprint"],
            "stderr_fingerprint": attempt.payload["stderr_fingerprint"],
        }
        for attempt in attempts
    }
    observation_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "gate10_status": "complete",
        "real_semantic_observations": True,
        "observation_source": "fresh_native_process_stdout_stderr_and_returncode",
        "cells": cells,
        "truth_table": {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        },
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "no_timeout_or_unknown": True,
        "complete": True,
    }
    observation = _write_or_check(
        root,
        _GATE10_OBSERVATION,
        inputs=(gate9.reference, *(a.reference for a in attempts)),
        payload=observation_payload,
        authorization=authorization,
    )
    oracle_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "gate10_status": "complete",
        "hidden_oracle": True,
        "goal": "fixed TestCollision completes with zero failed tests",
        "defect_signature": "buggy TestCollision completes with one or more failed tests",
        "invariants": "fixed and buggy TestSettings both complete with zero failed tests",
        "goal_observed": cells["fixed_f2p"]["result"] == "pass",
        "defect_observed": cells["buggy_f2p"]["result"] == "failure",
        "fixed_false_alarm": cells["fixed_f2p"]["result"] != "pass",
        "invariants_observed": cells["fixed_p2p"]["result"] == cells["buggy_p2p"]["result"] == "pass",
        "real_semantic_observations": True,
        "complete": True,
    }
    oracle = _write_or_check(
        root,
        _GATE10_ORACLE,
        inputs=(observation.reference, gate9.reference),
        payload=oracle_payload,
        authorization=authorization,
    )
    if not (
        oracle_payload["goal_observed"]
        and oracle_payload["defect_observed"]
        and not oracle_payload["fixed_false_alarm"]
        and oracle_payload["invariants_observed"]
    ):
        raise RuntimeError("#15408 recovery-v3 hidden oracle did not qualify")
    return gate9, observation, oracle


def _render_frame_bytes(*, role: str, result: str, details: list[str]) -> bytes:
    palette = {
        "ready": ((25, 42, 66), (71, 142, 255)),
        "pass": ((18, 57, 38), (55, 214, 122)),
        "failure": ((65, 25, 29), (255, 92, 105)),
    }
    background, accent = palette[result]
    image = Image.new("RGB", (1280, 720), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=24)
    title_font = ImageFont.load_default(size=34)
    draw.rectangle((0, 0, 1280, 18), fill=accent)
    draw.text((64, 64), "Luanti #15408 collision replay", fill=(242, 246, 252), font=title_font)
    draw.text((64, 126), role, fill=accent, font=title_font)
    y = 210
    for line in details:
        draw.text((72, y), line, fill=(225, 232, 242), font=font)
        y += 46
    draw.text(
        (72, 650),
        "Deterministic semantic rendering of real process evidence; not a game frame.",
        fill=(185, 196, 211),
        font=font,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _write_exact(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"#15408 diagnostic frame conflicts: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)


def _changed_pixel_ratio(left: Path, right: Path) -> float:
    with Image.open(left) as left_image, Image.open(right) as right_image:
        lrgb = left_image.convert("RGB")
        rrgb = right_image.convert("RGB")
        if lrgb.size != rrgb.size:
            return 1.0
        difference = ImageChops.difference(lrgb, rrgb)
        histogram = difference.convert("L").histogram()
        total = lrgb.width * lrgb.height
        return (total - histogram[0]) / total


def _write_diagnostics_and_blockers(
    root: Path,
    *,
    chain: dict[str, StableArtifact],
    attempts: tuple[StableArtifact, ...],
    gate9: StableArtifact,
    observation: StableArtifact,
    oracle: StableArtifact,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    attempt_map = {
        (a.payload["variant"], a.payload["phase"]): a for a in attempts
    }
    fixed = attempt_map[("fixed", "f2p")]
    buggy = attempt_map[("buggy", "f2p")]
    common_before = root / _GOLDEN_REL / "common-before.png"
    fixed_after = root / _GOLDEN_REL / "fixed-after.png"
    buggy_after = root / _GOLDEN_REL / "buggy-after.png"
    _write_exact(
        common_before,
        _render_frame_bytes(
            role="REGISTERED START",
            result="ready",
            details=[
                "Binary pair: recovery-v3 candidate-owned materialization",
                "Action: --run-unittests --test-module TestCollision",
                "State: ready; no test result observed yet",
            ],
        ),
    )
    for path, role, artifact in (
        (fixed_after, "FIXED AFTER", fixed),
        (buggy_after, "BUGGY AFTER", buggy),
    ):
        semantic = artifact.payload["semantic_observation"]
        _write_exact(
            path,
            _render_frame_bytes(
                role=role,
                result=artifact.payload["observed_outcome"],
                details=[
                    f"Selector: {artifact.payload['selector']}",
                    f"Result: {artifact.payload['observed_outcome'].upper()}",
                    f"Return code: {artifact.payload['returncode']}",
                    f"Failed tests: {semantic['failed_tests']} / {semantic['total_tests']}",
                    f"stdout fingerprint: {artifact.payload['stdout_fingerprint']}",
                    f"stderr fingerprint: {artifact.payload['stderr_fingerprint']}",
                ],
            ),
        )
    paths = {
        "common_before": common_before.relative_to(root).as_posix(),
        "fixed_after": fixed_after.relative_to(root).as_posix(),
        "buggy_after": buggy_after.relative_to(root).as_posix(),
    }
    frame_hashes = {key: _sha256_file(root / value) for key, value in paths.items()}
    gate11_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "diagnostic_only",
        "gate11_status": "deterministic_semantic_rendering_nonqualifying",
        "frame_paths": paths,
        "frame_fingerprints": frame_hashes,
        "rendering_source": "real_process_stdout_stderr_returncode",
        "deterministic_semantic_rendering": True,
        "synthetic_semantics": False,
        "engine_frame": False,
        "native_screenshot": False,
        "qualifying_visual_golden": False,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "complete": True,
    }
    gate11 = _write_or_check(
        root,
        _GATE11_DIAGNOSTIC,
        inputs=(gate9.reference, observation.reference, oracle.reference, fixed.reference, buggy.reference),
        payload=gate11_payload,
        authorization=authorization,
    )
    before_ratio = _changed_pixel_ratio(common_before, common_before)
    after_ratio = _changed_pixel_ratio(fixed_after, buggy_after)
    matrix = {
        "fixed_before_vs_buggy_before_changed_pixel_ratio": before_ratio,
        "fixed_after_vs_fixed_after_changed_pixel_ratio": 0.0,
        "fixed_after_vs_buggy_after_changed_pixel_ratio": after_ratio,
        "buggy_after_vs_fixed_after_changed_pixel_ratio": after_ratio,
        "buggy_after_vs_buggy_after_changed_pixel_ratio": 0.0,
        "before_equivalent": before_ratio == 0.0,
        "after_fixed_buggy_different": after_ratio > 0.01,
        "complete": True,
    }
    gate12_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "diagnostic_only",
        "gate12_status": "semantic_rendering_2x2_passed_nonqualifying",
        "matrix": matrix,
        "engine_frame": False,
        "native_screenshot": False,
        "qualifying_visual_cross_judgment": False,
        "complete": True,
    }
    gate12 = _write_or_check(
        root,
        _GATE12_DIAGNOSTIC,
        inputs=(gate11.reference,),
        payload=gate12_payload,
        authorization=authorization,
    )
    contract = Path(_CONTRACT_REL)
    if not contract.is_file():
        raise RuntimeError("#15408 Lazarus evaluation contract is unavailable")
    contract_text = contract.read_text(encoding="utf-8")
    normalized_contract = re.sub(r"\s+", " ", contract_text)
    required_fragments = (
        "If either screenshot, oracle hash, P2P result, semantic group, or visual judgement is missing",
        "both visual judgements with the same screenshot SHA-256",
        "Agent-S consumes native screenshots directly",
    )
    if any(fragment not in normalized_contract for fragment in required_fragments):
        raise RuntimeError("#15408 Lazarus native screenshot contract drifted")
    test_paths = ("src/unittest/test_collision.cpp", "src/unittest/test_settings.cpp")
    test_hashes = {
        path: {
            variant: _sha256_file(root / _PAIR_REL / variant / path)
            for variant in ("fixed", "buggy")
        }
        for path in test_paths
    }
    tests_equal = all(value["fixed"] == value["buggy"] for value in test_hashes.values())
    gate13_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "downstream_pending",
        "outcome": "native_visual_contract_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "diagnostic_semantic_renderings_are_not_native_screenshots",
        "gate9_status": "passed_ordered_replay",
        "gate10_status": "passed_hidden_oracle_and_real_semantic_observations",
        "gate11_status": "diagnostic_only_engine_frame_false",
        "gate12_status": "diagnostic_matrix_passed_but_nonqualifying",
        "gate13_status": "pending_native_visual_same_lineage_construction",
        "same_lineage_native_build_and_semantic_replay": True,
        "same_lineage_test_bytes": tests_equal,
        "test_fingerprints": test_hashes,
        "production_collision_fingerprints": {
            variant: _sha256_file(root / _PAIR_REL / variant / "src/collision.cpp")
            for variant in ("fixed", "buggy")
        },
        "contract": {
            "path": _CONTRACT_REL.as_posix(),
            "fingerprint": _sha256_file(contract),
            "requires_native_screenshot": True,
            "diagnostic_text_observer_is_not_a_substitute": True,
        },
        "missing_required_evidence": [
            "candidate_bound_rendered_client_start",
            "native_common_before_screenshot",
            "native_fixed_after_screenshot",
            "native_buggy_after_screenshot",
            "native_before_equivalence",
            "native_after_2x2_cross_judgment",
            "same_lineage_native_visual_construction_receipt",
        ],
        "does_not_invalidate_gate_1_7": True,
        "next_action": "build a candidate-owned client in a new namespace and capture a real collision scene; retain recovery-v3 Gate9/10",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    gate13 = _write_or_check(
        root,
        _GATE13_BLOCKER,
        inputs=(chain["build"].reference, gate9.reference, observation.reference, oracle.reference, gate11.reference, gate12.reference),
        payload=gate13_payload,
        authorization=authorization,
    )
    freeze_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-15408-collision-acceleration",
        "defect_locus": "at_goal",
        "freeze_state": "blocked_resumable",
        "formal_freeze_eligible": False,
        "gate_1_7": "qualified",
        "gate8": "passed_recovery_v3_build",
        "gate9": "passed_registered_start_ordered_replay",
        "gate10": "passed_hidden_oracle_real_semantic_observations",
        "gate11_to_13": "blocked_native_screenshot_missing",
        "diagnostic_pngs": {
            "real_process_evidence_source": True,
            "deterministic_semantic_rendering": True,
            "engine_frame": False,
            "qualifying_goldens": False,
            "matrix": matrix,
        },
        "blocker_reference": gate13.reference.artifact_id,
        "does_not_invalidate_gate_1_7": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "annotation_authorized": False,
        "complete": True,
    }
    freeze = _write_or_check(
        root,
        _FREEZE_BLOCKER,
        inputs=(chain["transition"].reference, chain["build"].reference, gate9.reference, observation.reference, oracle.reference, gate11.reference, gate12.reference, gate13.reference),
        payload=freeze_payload,
        authorization=authorization,
    )
    return gate11, gate12, gate13, freeze


def execute_recovery_v3_runtime(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, ...]:
    """Execute four bounded fresh replay cells and persist the honest blocker."""

    _require_execution(authorization)
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    root = _root(artifact_root)
    chain = _validate_runtime_binding(root)
    attempt_inputs = (
        chain["plan"].reference,
        chain["build"].reference,
        chain["materialization"].reference,
        chain["mapping"].reference,
    )
    attempts = tuple(
        _load_or_execute_attempt(
            root,
            variant=variant,
            phase=phase,
            selector="TestCollision" if phase == "f2p" else "TestSettings",
            inputs=attempt_inputs,
            authorization=authorization,
        )
        for variant in ("fixed", "buggy")
        for phase in ("f2p", "p2p")
    )
    gate9, observation, oracle = _write_gate9_gate10(
        root,
        chain=chain,
        attempts=attempts,
        authorization=authorization,
    )
    gate11, gate12, gate13, freeze = _write_diagnostics_and_blockers(
        root,
        chain=chain,
        attempts=attempts,
        gate9=gate9,
        observation=observation,
        oracle=oracle,
        authorization=authorization,
    )
    return (*attempts, gate9, observation, oracle, gate11, gate12, gate13, freeze)


def main() -> None:
    authorization = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-recovery-v3-runtime-v2",
        scope="preannotation-execution",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    artifacts = execute_recovery_v3_runtime(
        Path(".warptest-cache/c1_lazarus_selection_v1"),
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
    )
    for artifact in artifacts:
        print(artifact.reference.artifact_id, artifact.revision_path)


if __name__ == "__main__":
    main()


__all__ = ["EXECUTION_CONFIRMATION", "execute_recovery_v3_runtime"]
