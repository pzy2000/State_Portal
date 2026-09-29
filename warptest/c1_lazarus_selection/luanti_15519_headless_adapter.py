"""Current-v2 headless/server replay adapter for Luanti #15519.

The client Gate 5--7 receipts for #15519 are useful admission evidence, but
they are not a runtime Start or replay.  This module is the narrow downstream
adapter for that boundary.  It binds the current source/patch/atomicity/
manifest/capsule chain, the fresh client aggregate, and the persistent fixed /
buggy materialization pair before it can launch anything.

The optional probe copies each materialized tree into a *new* run root and
launches the copied server with an isolated world and a fixture that requests
shutdown after emitting a semantic marker.  Thus no old runtime root, UE
process, or production tree is touched.  A successful probe writes raw and
typed Start/replay/semantic observations; visual/golden and construction
receipts remain explicitly pending.  A missing marker or any infrastructure
failure writes a resumable, non-terminal blocker instead of guessing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


HEADLESS_CONFIRMATION = "EXECUTE_LUANTI_15519_HEADLESS_REPLAY_V4"
_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_V2 = "2.0"
_V1 = "1.0"
_OUTPUT_VERSION = "4.0"
_GAME_ID = "c1_plural"
_WATCHDOG_SECONDS = 45
_MAX_OUTPUT = 262_144

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15519/source/v2", "candidate_source_packet", _V2
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15519/screening/v2", "candidate_test_patch", _V2
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15519/preannotation/v2", "source_atomicity", _V2
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15519/test_manifest/v2", "candidate_stage", _V2
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", _V2
)
_CLIENT_AGGREGATE = ArtifactReference(
    "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
    "candidate_gate57_15519_client_aggregate",
    _V1,
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    _V1,
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    _V2,
)

_ATTEMPT_KIND = "candidate_headless_server_attempt"
_OBSERVATION = ArtifactReference(
    "candidate_headless_server_observation/luanti/15519/fixed-buggy/v4",
    "candidate_headless_server_observation",
    _OUTPUT_VERSION,
)
_REPLAY = ArtifactReference(
    "candidate_headless_server_replay/luanti/15519/v4",
    "candidate_headless_server_replay",
    _OUTPUT_VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_headless_server_blocker/luanti/15519/v4",
    "candidate_headless_server_blocker",
    _OUTPUT_VERSION,
)
_CONSTRUCTION = ArtifactReference(
    "candidate_headless_server_construction/luanti/15519/v4",
    "candidate_headless_server_construction",
    _OUTPUT_VERSION,
)
_ORACLE = ArtifactReference(
    "candidate_headless_server_oracle/luanti/15519/v4",
    "candidate_headless_server_oracle",
    _OUTPUT_VERSION,
)
_SEMANTIC_GOLDEN = ArtifactReference(
    "candidate_headless_semantic_golden/luanti/15519/v4",
    "candidate_headless_semantic_golden",
    _OUTPUT_VERSION,
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_headless_visual_golden_blocker/luanti/15519/v4",
    "candidate_headless_visual_golden_blocker",
    _OUTPUT_VERSION,
)
_LINEAGE_RECEIPT = ArtifactReference(
    "candidate_headless_construction_lineage/luanti/15519/v4",
    "candidate_headless_construction_lineage",
    _OUTPUT_VERSION,
)
_PROBE_BASE = PurePosixPath("private/candidate-downstream/luanti/15519/headless-v4")

_MISSING_AFTER_PROBE = (
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)


@dataclass(frozen=True)
class HeadlessProbeBinding:
    source: StableArtifact
    patch: StableArtifact
    atomicity: StableArtifact
    manifest: StableArtifact
    capsule: StableArtifact
    client_aggregate: StableArtifact
    materialization: StableArtifact
    task_mapping: StableArtifact
    fixed_tree: Path
    buggy_tree: Path
    fixed_binary: Path
    buggy_binary: Path


@dataclass(frozen=True)
class HeadlessProbeResult:
    observation: StableArtifact
    replay: StableArtifact
    oracle: StableArtifact
    blocker: StableArtifact
    construction: StableArtifact
    state: Literal["passed", "downstream_pending"]
    executed_attempts: int


@dataclass(frozen=True)
class HeadlessGoldenResult:
    semantic_golden: StableArtifact
    visual_blocker: StableArtifact
    construction_lineage: StableArtifact
    state: Literal["downstream_pending"]


Runner = Callable[..., NativeProcessReceipt]


def load_luanti_15519_headless_binding(
    artifact_root: str | Path,
) -> HeadlessProbeBinding:
    """Load and validate the complete current-v2 downstream input closure."""

    root = _ordinary_root(Path(artifact_root).absolute())
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    capsule = _load(root, _CAPSULE)
    client = _load(root, _CLIENT_AGGREGATE)
    materialization = _load(root, _MATERIALIZATION)
    mapping = _load(root, _TASK_MAPPING)
    _validate_source_chain(source, patch, atomicity, manifest, capsule)
    _validate_client_gate57(client)
    fixed_tree, buggy_tree, fixed_binary, buggy_binary = _validate_materialization(
        root, materialization
    )
    _validate_task_mapping(root, mapping)
    return HeadlessProbeBinding(
        source,
        patch,
        atomicity,
        manifest,
        capsule,
        client,
        materialization,
        mapping,
        fixed_tree,
        buggy_tree,
        fixed_binary,
        buggy_binary,
    )


def write_luanti_15519_semantic_golden_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> HeadlessGoldenResult:
    """Project v4 observations into semantic goldens and a visual blocker.

    The v4 server run supplies real fixed/buggy *after* observations, but it
    did not capture a pre-action frame and the null video driver cannot produce
    any visual frame.  This function therefore records only the observed
    semantic after-state snapshots and a typed nonterminal visual blocker.  It
    never writes PNGs, invents a common-before state, or changes the v4 runtime
    receipts.
    """

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_15519_headless_binding(root)
    observation = _load(root, _OBSERVATION)
    replay = _load(root, _REPLAY)
    oracle = _load(root, _ORACLE)
    construction = _load(root, _CONSTRUCTION)
    attempts = tuple(
        _load(
            root,
            ArtifactReference(
                f"{_ATTEMPT_KIND}/luanti/15519/{variant}/v4",
                _ATTEMPT_KIND,
                _OUTPUT_VERSION,
            ),
        )
        for variant in ("fixed", "buggy")
    )
    _validate_v4_downstream(
        observation, replay, oracle, construction, attempts
    )
    semantic_payload = _semantic_golden_payload(attempts)
    semantic = _write(
        root,
        _SEMANTIC_GOLDEN,
        inputs=(
            observation.reference,
            replay.reference,
            oracle.reference,
            *(attempt.reference for attempt in attempts),
        ),
        payload=semantic_payload,
        authorization=authorization,
    )
    visual_payload = {
        "candidate": _CANDIDATE,
        "state": "downstream_pending",
        "outcome": "visual_capture_unavailable",
        "visual_capture_supported": False,
        "null_video_driver_only": True,
        "synthetic_frames_written": False,
        "golden_disposition": "required_no_not_applicable",
        "frame_policy": {
            "common_before": "required_missing_no_registered_before_observation",
            "fixed_after": "required_missing_no_video_frame",
            "buggy_after": "required_missing_no_video_frame",
            "before_visual_equivalence": "required_pending",
            "after_2x2_cross_matrix": "required_pending",
        },
        "semantic_after_source": semantic.reference.artifact_id,
        "missing_required_evidence": [
            "common_before_semantic_observation",
            "common_before_visual_frame",
            "fixed_after_visual_frame",
            "buggy_after_visual_frame",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
        ],
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    visual = _write(
        root,
        _VISUAL_BLOCKER,
        inputs=(semantic.reference, observation.reference, replay.reference, oracle.reference),
        payload=visual_payload,
        authorization=authorization,
    )
    lineage_payload = {
        "candidate": _CANDIDATE,
        "state": "downstream_pending",
        "runtime_replay_oracle_state": "passed",
        "semantic_golden_state": semantic_payload["state"],
        "visual_golden_state": "pending_visual_capture",
        "construction_lineage_state": "pending_visual_and_common_before",
        "same_lineage_inputs": [item.artifact_id for item in _binding_inputs(binding)],
        "semantic_golden_reference": semantic.reference.artifact_id,
        "visual_blocker_reference": visual.reference.artifact_id,
        "prior_construction_reference": construction.reference.artifact_id,
        "missing_required_evidence": list(visual_payload["missing_required_evidence"])
        + ["same_lineage_construction_receipt"],
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    lineage = _write(
        root,
        _LINEAGE_RECEIPT,
        inputs=(
            semantic.reference,
            visual.reference,
            construction.reference,
            *_binding_inputs(binding),
        ),
        payload=lineage_payload,
        authorization=authorization,
    )
    return HeadlessGoldenResult(semantic, visual, lineage, "downstream_pending")


def write_luanti_15519_headless_pending(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a readiness receipt without launching a process."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_15519_headless_binding(root)
    payload = _blocker_payload(
        state="downstream_pending",
        outcome="probe_not_requested",
        reason="headless_probe_not_requested",
        executed_attempts=0,
        missing=(
            "registered_start_and_ordered_replay",
            "raw_fixed_and_buggy_semantic_observations",
            *_MISSING_AFTER_PROBE,
        ),
    )
    return _write(
        root,
        _BLOCKER,
        inputs=_binding_inputs(binding),
        payload=payload,
        authorization=authorization,
    )


def execute_luanti_15519_headless_probe(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    process_runner: Runner = run_bounded_process,
) -> HeadlessProbeResult:
    """Run one isolated fixed/buggy server replay, then persist typed receipts.

    The two variants are one bounded probe.  If either process cannot emit the
    registered Start, semantic, and shutdown markers, no positive replay claim
    is made; a non-terminal blocker is persisted instead.
    """

    if confirmation != HEADLESS_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {HEADLESS_CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_15519_headless_binding(root)
    run = _reserve_run_root(root, Path(run_root).absolute())
    attempts: list[StableArtifact] = []
    executed = 0
    try:
        for variant, tree, binary in (
            ("fixed", binding.fixed_tree, binding.fixed_binary),
            ("buggy", binding.buggy_tree, binding.buggy_binary),
        ):
            spec = _prepare_variant(root, run, binding, variant, tree, binary)
            receipt = process_runner(
                spec.argv,
                cwd=spec.cwd,
                env=spec.environment,
                timeout_seconds=_WATCHDOG_SECONDS,
            )
            attempt = _write_attempt(
                root,
                binding=binding,
                spec=spec,
                receipt=receipt,
                authorization=authorization,
            )
            attempts.append(attempt)
            executed += 1
            if not _attempt_semantically_complete(variant, attempt.payload):
                return _persist_downstream(
                    root,
                    binding=binding,
                    attempts=tuple(attempts),
                    state="downstream_pending",
                    outcome="semantic_observation_incomplete",
                    reason=f"{variant}_headless_marker_or_oracle_missing",
                    authorization=authorization,
                )
        return _persist_downstream(
            root,
            binding=binding,
            attempts=tuple(attempts),
            state="passed",
            outcome=None,
            reason=None,
            authorization=authorization,
        )
    finally:
        # Only the fresh probe root is cleaned.  Materialized source trees and
        # all historical runtime roots are outside this path and untouched.
        if run.exists() or run.is_symlink():
            _remove_run_root(run, root)


@dataclass(frozen=True)
class _RunSpec:
    variant: str
    cwd: Path
    runtime: Path
    argv: tuple[str, ...]
    environment: dict[str, str]
    expected_defect: bool


def _prepare_variant(
    root: Path,
    run: Path,
    binding: HeadlessProbeBinding,
    variant: str,
    source_tree: Path,
    source_binary: Path,
) -> _RunSpec:
    if variant not in {"fixed", "buggy"}:
        raise ValueError("headless variant is invalid")
    tree = run / variant / "tree"
    shutil.copytree(source_tree, tree, symlinks=False)
    _validate_no_symlink_tree(tree)
    fixture = root / PurePosixPath(str(binding.task_mapping.payload["fixture_root"]))
    target_game = tree / "games" / _GAME_ID
    if target_game.exists() or target_game.is_symlink():
        raise RuntimeError("headless fixture path conflicts with materialized tree")
    shutil.copytree(fixture, target_game, symlinks=False)
    _validate_no_symlink_tree(target_game)
    # The fixture is a probe-only copy.  Use the registered translator API and
    # wait until mods are loaded; the frozen authoring fixture intentionally
    # only specifies the public task and its direct ``get_translated_string``
    # call is not guaranteed to have a value during mod initialisation.  The
    # shutdown request makes the server deterministic and avoids relying on a
    # watchdog kill.  This is an adapter-only runtime wrapper; the frozen
    # task-mapping file and production trees are never edited.
    init = target_game / "mods/c1_plural/init.lua"
    init.write_text(
        'local S, NS = minetest.get_translator("c1_plural")\n'
        "minetest.register_on_mods_loaded(function()\n"
        "  minetest.after(0.1, function()\n"
        '    local encoded = NS("apple", "apples", 122)\n'
        '    local ok, translated = pcall(minetest.get_translated_string, "ru", encoded)\n'
        '    if not ok then translated = "<error>" end\n'
        '    if translated == nil then translated = "<nil>" end\n'
        '    minetest.log("action", "C1 plural fixture loaded: " .. tostring(translated))\n'
        '    minetest.log("action", "C1_HEADLESS_REPLAY_STEP=semantic_observation")\n'
        '    minetest.request_shutdown("C1_HEADLESS_DONE", false, 0)\n'
        "  end)\n"
        "end)\n",
        encoding="utf-8",
    )
    runtime = run / variant / "runtime"
    world = runtime / "world"
    world.mkdir(parents=True, mode=0o700)
    (world / "world.mt").write_text(
        "gameid = c1_plural\nbackend = sqlite3\n"
        "player_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = runtime / "minetest.conf"
    config.write_text(
        "video_driver = null\n"
        "enable_sound = false\n"
        "server_announce = false\n"
        "enable_console = true\n"
        "language = ru\n"
        "name = warptest\n"
        "time_speed = 0\n",
        encoding="utf-8",
    )
    log = runtime / "luanti.log"
    home = runtime / "home"
    tmp = runtime / "tmp"
    home.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str((root / "private/dependencies/luanti-native/3.0/bin")),
        "TMPDIR": str(tmp),
    }
    _ordinary_file(source_binary, source_tree)
    # Use the copied executable, never the persistent materialization path.
    copied_binary = tree / "bin/luanti"
    _ordinary_file(copied_binary, tree)
    argv = (
        str(copied_binary),
        "--server",
        "--gameid",
        _GAME_ID,
        "--world",
        str(world),
        "--config",
        str(config),
        "--logfile",
        str(log),
    )
    return _RunSpec(variant, tree, runtime, argv, env, variant == "buggy")


def _write_attempt(
    root: Path,
    *,
    binding: HeadlessProbeBinding,
    spec: _RunSpec,
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    text = (receipt.stdout[:_MAX_OUTPUT] + b"\n" + receipt.stderr[:_MAX_OUTPUT]).decode(
        "utf-8", errors="replace"
    )
    log = spec.runtime / "luanti.log"
    if log.exists() and log.is_file():
        text += "\n" + log.read_text(encoding="utf-8", errors="replace")[:_MAX_OUTPUT]
    start = "C1 plural fixture loaded" in text
    ordered = [
        marker
        for marker in (
            "C1 plural fixture loaded",
            "C1_HEADLESS_REPLAY_STEP=semantic_observation",
            "C1_HEADLESS_DONE",
            "Server: Shutting down",
        )
        if marker in text
    ]
    invalid = "Invalid Plural-Forms" in text
    # Russian plural selection may legitimately produce яблоко/яблока/яблок;
    # the semantic distinction is the presence of a real Cyrillic translation,
    # not one hard-coded cardinality.
    translated = "ябл" in text
    payload: dict[str, object] = {
        "candidate": _CANDIDATE,
        "variant": spec.variant,
        "argv": list(spec.argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "watchdog_seconds": _WATCHDOG_SECONDS,
        "startup_marker_observed": start,
        "registered_start": start,
        "ordered_replay": ordered,
        "semantic_observation": {
            "fixture_loaded": start,
            "invalid_plural_forms_observed": invalid,
            "translated_russian_observed": translated,
            "expected_invalid_plural_forms": spec.expected_defect,
            "goal_oracle": translated and not invalid
            if not spec.expected_defect
            else False,
            "defect_oracle": invalid if spec.expected_defect else False,
            "invariant_oracle": start,
        },
        "process_group_reaped": receipt.outcome == "completed",
        # request_shutdown's reason is not emitted by every Luanti build.  A
        # clean process exit with the native shutdown action is an equivalent
        # observed marker; keep the actual text in ``ordered_replay`` rather
        # than inventing C1_HEADLESS_DONE.
        "shutdown_marker_observed": (
            "C1_HEADLESS_DONE" in text
            or (
                "Server: Shutting down" in text
                and receipt.outcome == "completed"
                and receipt.returncode == 0
                and start
            )
        ),
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode(),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode(),
        "output_encoding": "base64",
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "complete": True,
    }
    ref = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/15519/{spec.variant}/v4",
        _ATTEMPT_KIND,
        _OUTPUT_VERSION,
    )
    return _write(
        root,
        ref,
        inputs=_binding_inputs(binding),
        payload=payload,
        authorization=authorization,
    )


def _attempt_semantically_complete(variant: str, payload: object) -> bool:
    if type(payload) is not dict:
        return False
    obs = payload.get("semantic_observation")
    ordered = payload.get("ordered_replay")
    if type(obs) is not dict or type(ordered) is not list:
        return False
    return (
        payload.get("process_outcome") == "completed"
        and payload.get("registered_start") is True
        and payload.get("shutdown_marker_observed") is True
        and payload.get("process_group_reaped") is True
        and ordered[:2]
        == [
            "C1 plural fixture loaded",
            "C1_HEADLESS_REPLAY_STEP=semantic_observation",
        ]
        and ordered[-1:] in (["C1_HEADLESS_DONE"], ["Server: Shutting down"])
        and obs.get("fixture_loaded") is True
        and obs.get("invalid_plural_forms_observed") is (variant == "buggy")
        and obs.get("translated_russian_observed") is (variant == "fixed")
        and obs.get("invariant_oracle") is True
    )


def _persist_downstream(
    root: Path,
    *,
    binding: HeadlessProbeBinding,
    attempts: tuple[StableArtifact, ...],
    state: Literal["passed", "downstream_pending"],
    outcome: str | None,
    reason: str | None,
    authorization: AuthorizationRecord,
) -> HeadlessProbeResult:
    attempt_inputs = tuple(item.reference for item in attempts)
    obs_payload = {
        "candidate": _CANDIDATE,
        "state": state,
        "outcome": outcome,
        "attempt_count": len(attempts),
        "fixed_buggy_pair": len(attempts) == 2
        and all(
            _attempt_semantically_complete(v, a.payload)
            for v, a in zip(("fixed", "buggy"), attempts, strict=True)
        ),
        "registered_start": len(attempts) == 2,
        "ordered_replay": len(attempts) == 2,
        "semantic_observations": len(attempts) == 2,
        "raw_observation_receipts": [a.reference.artifact_id for a in attempts],
        "complete": True,
    }
    observation = _write(
        root,
        _OBSERVATION,
        inputs=(*_binding_inputs(binding), *attempt_inputs),
        payload=obs_payload,
        authorization=authorization,
    )
    pair = obs_payload["fixed_buggy_pair"] is True
    replay = _write(
        root,
        _REPLAY,
        inputs=(observation.reference, *attempt_inputs),
        payload={
            "candidate": _CANDIDATE,
            "state": "passed" if pair else "downstream_pending",
            "outcome": None if pair else outcome,
            "registered_start": pair,
            "ordered_replay": pair,
            "hidden_oracle": pair,
            "real_semantic_observations": pair,
            "complete": True,
        },
        authorization=authorization,
    )
    oracle = _write(
        root,
        _ORACLE,
        inputs=(observation.reference, replay.reference, *attempt_inputs),
        payload={
            "candidate": _CANDIDATE,
            "state": "passed" if pair else "downstream_pending",
            "outcome": None if pair else outcome,
            "hidden_oracle": pair,
            "fixed_goal_observed": pair,
            "buggy_defect_observed": pair,
            "invariant_observed": pair,
            "semantic_observation_receipts": [
                a.reference.artifact_id for a in attempts
            ],
            "complete": True,
        },
        authorization=authorization,
    )
    missing = (
        _MISSING_AFTER_PROBE
        if pair
        else (
            "registered_start_and_ordered_replay",
            "raw_fixed_and_buggy_semantic_observations",
            "hidden_semantic_oracle",
            *_MISSING_AFTER_PROBE,
        )
    )
    blocker = _write(
        root,
        _BLOCKER,
        inputs=(
            *_binding_inputs(binding),
            observation.reference,
            replay.reference,
            oracle.reference,
        ),
        payload=_blocker_payload(
            state="passed" if pair else "downstream_pending",
            outcome=outcome,
            reason=reason,
            executed_attempts=len(attempts),
            missing=missing,
        ),
        authorization=authorization,
    )
    construction = _write(
        root,
        _CONSTRUCTION,
        inputs=(
            observation.reference,
            replay.reference,
            oracle.reference,
            blocker.reference,
        ),
        payload={
            "candidate": _CANDIDATE,
            "state": "downstream_pending",
            "runtime_state": "passed" if pair else "pending",
            "oracle_state": "passed" if pair else "pending",
            "golden_state": "pending_null_video_frame_capture",
            "visual_capture_supported": False,
            "construction_lineage_state": "pending",
            "missing_required_evidence": list(_MISSING_AFTER_PROBE),
            "permanent_terminal_written": False,
            "paid_actions_authorized": False,
            "complete": True,
        },
        authorization=authorization,
    )
    return HeadlessProbeResult(
        observation,
        replay,
        oracle,
        blocker,
        construction,
        "passed" if pair else "downstream_pending",
        len(attempts),
    )


def _validate_v4_downstream(
    observation: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
    construction: StableArtifact,
    attempts: tuple[StableArtifact, ...],
) -> None:
    attempt_refs = tuple(item.reference for item in attempts)
    if (
        len(attempts) != 2
        or observation.inputs[-2:] != attempt_refs
        or replay.inputs != (observation.reference, *attempt_refs)
        or oracle.inputs != (observation.reference, replay.reference, *attempt_refs)
        or construction.inputs[:3] != (
            observation.reference,
            replay.reference,
            oracle.reference,
        )
    ):
        raise RuntimeError("15519 v4 downstream lineage is invalid")
    if (
        observation.reference != _OBSERVATION
        or observation.payload.get("state") != "passed"
        or observation.payload.get("fixed_buggy_pair") is not True
        or observation.payload.get("registered_start") is not True
        or observation.payload.get("ordered_replay") is not True
        or observation.payload.get("semantic_observations") is not True
    ):
        raise RuntimeError("15519 v4 observation is not complete")
    if (
        replay.reference != _REPLAY
        or replay.payload.get("state") != "passed"
        or replay.payload.get("ordered_replay") is not True
        or replay.payload.get("hidden_oracle") is not True
        or replay.payload.get("real_semantic_observations") is not True
    ):
        raise RuntimeError("15519 v4 replay is not complete")
    if (
        oracle.reference != _ORACLE
        or oracle.payload.get("state") != "passed"
        or oracle.payload.get("hidden_oracle") is not True
        or oracle.payload.get("fixed_goal_observed") is not True
        or oracle.payload.get("buggy_defect_observed") is not True
        or oracle.payload.get("invariant_observed") is not True
    ):
        raise RuntimeError("15519 v4 oracle is not complete")
    if (
        construction.reference != _CONSTRUCTION
        or construction.payload.get("runtime_state") != "passed"
        or construction.payload.get("oracle_state") != "passed"
        or construction.payload.get("visual_capture_supported") is not False
    ):
        raise RuntimeError("15519 v4 construction boundary is invalid")
    if len(attempts) != 2 or tuple(item.payload.get("variant") for item in attempts) != (
        "fixed",
        "buggy",
    ):
        raise RuntimeError("15519 v4 attempt pair is invalid")
    for variant, attempt in zip(("fixed", "buggy"), attempts, strict=True):
        if not _attempt_semantically_complete(variant, attempt.payload):
            raise RuntimeError(f"15519 v4 {variant} semantic attempt is incomplete")


def _semantic_golden_payload(
    attempts: tuple[StableArtifact, StableArtifact],
) -> dict[str, object]:
    fixed, buggy = attempts
    return {
        "candidate": _CANDIDATE,
        "state": "downstream_pending",
        "outcome": "semantic_after_only",
        "source": "current_v4_real_headless_observations",
        "common_before": {
            "state": "missing",
            "reason": "v4_replay_registered_after_start_only",
            "synthetic": False,
        },
        "fixed_after": _semantic_snapshot(fixed),
        "buggy_after": _semantic_snapshot(buggy),
        "visual_capture_supported": False,
        "visual_golden_registration": "blocked_by_null_video_and_missing_before",
        "complete": True,
    }


def _semantic_snapshot(artifact: StableArtifact) -> dict[str, object]:
    observation = artifact.payload.get("semantic_observation")
    if type(observation) is not dict:
        raise RuntimeError("15519 semantic observation payload is invalid")
    encoded = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "state": "observed",
        "attempt_reference": artifact.reference.artifact_id,
        "ordered_replay": artifact.payload.get("ordered_replay"),
        "process_outcome": artifact.payload.get("process_outcome"),
        "returncode": artifact.payload.get("returncode"),
        "observation": dict(observation),
        "semantic_fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "synthetic": False,
    }


def _blocker_payload(
    *,
    state: str,
    outcome: str | None,
    reason: str | None,
    executed_attempts: int,
    missing: tuple[str, ...],
) -> dict[str, object]:
    return {
        "candidate": _CANDIDATE,
        "state": state,
        "outcome": outcome,
        "reason": reason,
        "executed_attempts": executed_attempts,
        "missing_required_evidence": list(missing),
        "golden_disposition": "required_no_not_applicable",
        "visual_capture_supported": False,
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _binding_inputs(binding: HeadlessProbeBinding) -> tuple[ArtifactReference, ...]:
    return (
        binding.source.reference,
        binding.patch.reference,
        binding.atomicity.reference,
        binding.manifest.reference,
        binding.capsule.reference,
        binding.client_aggregate.reference,
        binding.materialization.reference,
        binding.task_mapping.reference,
    )


def _validate_source_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    capsule: StableArtifact,
) -> None:
    if (
        source.inputs
        != (
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                _V2,
            ),
        )
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("15519 current-v2 source packet is invalid")
    if (
        patch.inputs != (source.inputs[0], source.reference)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("complete") is not True
    ):
        raise RuntimeError("15519 current-v2 patch is invalid")
    if (
        atomicity.inputs
        != (
            source.inputs[0],
            source.reference,
            patch.reference,
            ArtifactReference(
                "screening_checkpoint/luanti/15519/current/v2",
                "screening_checkpoint",
                _V2,
            ),
        )
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 15518
        or atomicity.payload.get("complete") is not True
    ):
        raise RuntimeError("15519 current-v2 source atomicity is invalid")
    if (
        manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("complete") is not True
    ):
        raise RuntimeError("15519 current-v2 test manifest is invalid")
    # The fixed-source capsule schema intentionally has no ``complete`` field;
    # its source paths and profile identity are the closure proof.
    if (
        capsule.inputs != (source.reference, patch.reference, manifest.reference)
        or capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("profile_id") != "luanti-15519-gettext-plural-trim-v2"
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/15519/fixed-source"
        or capsule.payload.get("test_paths") != ["src/unittest/test_translations.cpp"]
        or capsule.payload.get("p2p_test_paths") != ["src/unittest/test_settings.cpp"]
    ):
        raise RuntimeError("15519 current-v2 fixed capsule is invalid")


def _validate_client_gate57(artifact: StableArtifact) -> None:
    payload = artifact.payload
    status = payload.get("gate_status")
    if (
        artifact.inputs.__len__() != 12
        or payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "qualified"
        or payload.get("outcome") != "qualified"
        or payload.get("qualified") is not True
        or payload.get("attempt_count") != 12
        or payload.get("all_expected") is not True
        or payload.get("stable_repetitions") is not True
        or payload.get("old_attempts_loaded") is not False
        or payload.get("candidate_execution_performed") is not True
        or payload.get("terminal") is not False
        or type(status) is not dict
        or any(
            status.get(key) != "passed"
            for key in ("fixed_f2p_3x", "buggy_f2p_3x", "fixed_p2p_3x", "buggy_p2p_3x")
        )
    ):
        raise RuntimeError("15519 fresh client Gate 5-7 aggregate is invalid")


def _validate_materialization(
    root: Path, artifact: StableArtifact
) -> tuple[Path, Path, Path, Path]:
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "built"
        or payload.get("complete") is not True
        or payload.get("missing_required_evidence") != []
        or payload.get("client_launch_performed") is not False
    ):
        raise RuntimeError("15519 persistent materialization is not complete")
    paths = tuple(
        root / PurePosixPath(str(payload[key]))
        for key in (
            "fixed_tree_path",
            "buggy_tree_path",
            "fixed_binary_path",
            "buggy_binary_path",
        )
    )
    fixed_tree, buggy_tree, fixed_binary, buggy_binary = paths
    _ordinary_directory(fixed_tree, root)
    _ordinary_directory(buggy_tree, root)
    _ordinary_file(fixed_binary, fixed_tree)
    _ordinary_file(buggy_binary, buggy_tree)
    if not os.access(fixed_binary, os.X_OK) or not os.access(buggy_binary, os.X_OK):
        raise RuntimeError("15519 materialized binaries are not executable")
    return fixed_tree, buggy_tree, fixed_binary, buggy_binary


def _validate_task_mapping(root: Path, artifact: StableArtifact) -> None:
    payload = artifact.payload
    if payload.get("candidate") != _CANDIDATE or payload.get("complete") is not True:
        raise RuntimeError("15519 task mapping is invalid")
    fixture_value = payload.get("fixture_root")
    fixture_paths = payload.get("fixture_paths")
    if type(fixture_value) is not str or type(fixture_paths) is not list:
        raise RuntimeError("15519 task mapping fixture is invalid")
    fixture = root / PurePosixPath(fixture_value)
    _ordinary_directory(fixture, root)
    for relative in fixture_paths:
        if type(relative) is not str:
            raise RuntimeError("15519 task mapping fixture path is invalid")
        _ordinary_file(fixture / PurePosixPath(relative), fixture)


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
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
        raise RuntimeError("15519 headless receipt typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _reserve_run_root(root: Path, requested: Path) -> Path:
    allowed = root / _PROBE_BASE
    _make_directory(allowed, root)
    try:
        requested.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError("15519 headless run root escapes registered root") from error
    if (
        requested.parent != allowed
        or not requested.name
        or requested.exists()
        or requested.is_symlink()
    ):
        raise RuntimeError("15519 headless run root is invalid or already exists")
    requested.mkdir(mode=0o700)
    return requested


def _remove_run_root(run: Path, root: Path) -> None:
    try:
        run.relative_to(root / _PROBE_BASE)
    except ValueError as error:
        raise RuntimeError(
            "15519 headless cleanup root escapes registered root"
        ) from error
    if run.is_symlink() or not run.is_dir():
        raise RuntimeError("15519 headless cleanup root is unsafe")
    shutil.rmtree(run)


def _make_directory(path: Path, root: Path) -> None:
    path.relative_to(root)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("15519 headless directory is unsafe")
        else:
            current.mkdir(mode=0o700)


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _validate_no_symlink_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("headless copied tree is unsafe")
    for item in path.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise RuntimeError("headless copied tree contains an unsafe entry")


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


__all__ = [
    "HEADLESS_CONFIRMATION",
    "HeadlessGoldenResult",
    "HeadlessProbeBinding",
    "HeadlessProbeResult",
    "execute_luanti_15519_headless_probe",
    "load_luanti_15519_headless_binding",
    "write_luanti_15519_headless_pending",
    "write_luanti_15519_semantic_golden_blocker",
]
