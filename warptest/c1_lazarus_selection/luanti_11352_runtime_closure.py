"""Candidate-bound native Start/replay/oracle closure for Luanti #11352."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_native_executor import run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_11352_BOUNDED_REPLAY_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 11352}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11352/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11352/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11352/preannotation/v2", "source_atomicity", "2.0"
)
_MATERIALIZATION = ArtifactReference(
    "candidate_gate57_fresh_materialization/luanti/11352/cpp17/v6",
    "candidate_gate57_fresh_materialization",
    "1.0",
)
_FIXED_BUILD = ArtifactReference(
    "candidate_gate57_fresh_build/luanti/11352/cpp17/fixed/build/v6",
    "candidate_gate57_fresh_build",
    "1.0",
)
_BUGGY_BUILD = ArtifactReference(
    "candidate_gate57_fresh_build/luanti/11352/cpp17/buggy/build/v6",
    "candidate_gate57_fresh_build",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_fresh_aggregate/luanti/11352/cpp17/v6",
    "candidate_gate57_fresh_aggregate",
    "1.0",
)
_READINESS = ArtifactReference(
    "candidate_downstream_readiness_comparison/luanti/9572-11352/current-v2/v1",
    "candidate_downstream_readiness_comparison",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/11352/lz-luanti-11352-map-settings-hierarchy/v4",
    "lazarus_task_mapping",
    "4.0",
)
_FIXTURE = ArtifactReference(
    "candidate_gate9_11352_runtime_fixture/luanti/11352/bounded-v2",
    "candidate_gate9_11352_runtime_fixture",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_11352_replay_oracle/luanti/11352/bounded-v2",
    "candidate_gate10_11352_replay_oracle",
    "1.0",
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_13_11352_visual_blocker/luanti/11352/current-v2/v1",
    "candidate_gate11_13_11352_visual_blocker",
    "1.0",
)
_PAIR = PurePosixPath("private/fresh-gate57/luanti/11352/cpp17-v6")
_ROOT = PurePosixPath("private/runtime-fixtures/luanti/11352/bounded-v2")
_GAME_ID = "c1_lazarus_11352"
_GAME_CONF = b"name = c1_lazarus_11352\ntitle = C1 Lazarus 11352\n"
_MOD_CONF = b"name = c1_lazarus\n"
_INIT = b'''local before_global = minetest.settings:get("water_level")
local before_mapgen = minetest.get_mapgen_setting("water_level")
local start = tostring(before_global) .. "," .. tostring(before_mapgen)
minetest.log("action", "C1_LAZARUS_11352_START " .. start)
minetest.set_mapgen_setting("water_level", "15", false)
local after_global = minetest.settings:get("water_level")
local after_mapgen = minetest.get_mapgen_setting("water_level")
local class = "unexpected_state"
if before_global == "20" and before_mapgen == "20" then
  if after_global == "20" then
    class = "goal_map_default_isolated"
  elseif after_global == "15" then
    class = "defect_global_setting_mutated"
  end
end
minetest.log("action", "C1_LAZARUS_11352_OBSERVATION " .. class)
minetest.log("action", "C1_LAZARUS_11352_AFTER_MAPGEN " .. tostring(after_mapgen))
minetest.log("action", "C1_LAZARUS_11352_INVARIANT server_responsive")
minetest.request_shutdown("C1_LAZARUS_11352_DONE", false, 0)
'''
_START = re.compile(rb"C1_LAZARUS_11352_START ([^\r\n]+)")
_OBSERVATION = re.compile(rb"C1_LAZARUS_11352_OBSERVATION ([^\r\n]+)")
_INVARIANT = re.compile(rb"C1_LAZARUS_11352_INVARIANT ([^\r\n]+)")


def execute_luanti_11352_bounded_replay(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    """Execute one fresh server replay per side and write Gate 9/10 receipts."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, ref)
        for ref in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MATERIALIZATION,
            _FIXED_BUILD,
            _BUGGY_BUILD,
            _AGGREGATE,
            _READINESS,
        )
    )
    _validate_chain(chain)
    mapping = _write_mapping(root, chain, authorization)
    fixture = _write_fixture(root, mapping, authorization)
    attempts = tuple(
        _run_variant(root, variant, fixture, authorization)
        for variant in ("fixed", "buggy")
    )
    oracle = _write_oracle(root, mapping, fixture, attempts, authorization)
    blocker = _write_visual_blocker(root, mapping, oracle, authorization)
    return mapping, fixture, oracle, blocker


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    source, patch, atomicity, materialization, fixed_build, buggy_build, aggregate, readiness = chain
    if (
        any(item.payload.get("candidate") != _CANDIDATE for item in chain[:-1])
        or source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("primary_issue_number") != 11351
        or materialization.payload.get("state") != "materialized"
        or fixed_build.payload.get("state") != "built"
        or fixed_build.payload.get("returncode") != 0
        or buggy_build.payload.get("state") != "built"
        or buggy_build.payload.get("returncode") != 0
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or readiness.payload.get("selected_candidate") != _CANDIDATE
        or readiness.payload.get("terminal") is not False
    ):
        raise RuntimeError("#11352 native downstream chain drifted")


def _write_mapping(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-11352-map-settings-hierarchy",
        "profile_id": "luanti-11352-map-settings-hierarchy-v1",
        "issue_number": 11351,
        "defect_locus": "on_path",
        "complete": True,
        "candidate_execution_performed": False,
        "fixture_root": _ROOT.as_posix(),
        "registered_start": {
            "global_water_level": "20",
            "mapgen_water_level": "20",
        },
        "ordered_replay": [
            {"type": "restore_registered_start"},
            {
                "type": "set_low_priority_mapgen_default",
                "name": "water_level",
                "value": "15",
                "override_meta": False,
            },
        ],
        "semantic_oracle": {
            "goal": "a low-priority mapgen-default call does not mutate the global user setting 20",
            "defect": "the call leaks into the global Settings layer and changes water_level from 20 to 15",
            "invariants": [
                "fixed and buggy begin with the same global and mapgen water level",
                "both native servers remain responsive after the replay",
            ],
        },
        "golden_contract": {
            "common_before_fixed_after_buggy_after": True,
            "pairwise_before_visual_equivalence": True,
            "two_by_two_after_cross_matrix": True,
            "raw_frames_required": True,
            "semantic_observations_required": True,
            "same_lineage_required": True,
        },
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, _MAPPING, tuple(item.reference for item in chain), payload, authorization)


def _write_fixture(
    root: Path, mapping: StableArtifact, authorization: AuthorizationRecord
) -> StableArtifact:
    game = root / _ROOT / "games" / _GAME_ID
    files = {
        game / "game.conf": _GAME_CONF,
        game / "mods/c1_lazarus/mod.conf": _MOD_CONF,
        game / "mods/c1_lazarus/init.lua": _INIT,
    }
    for path, content in files.items():
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != content:
                raise RuntimeError("#11352 runtime fixture drifted")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(content)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "fixture_root": _ROOT.as_posix(),
        "game_id": _GAME_ID,
        "registered_start": mapping.payload["registered_start"],
        "ordered_replay": mapping.payload["ordered_replay"],
        "file_fingerprints": {
            path.relative_to(root / _ROOT).as_posix(): hashlib.sha256(content).hexdigest()
            for path, content in files.items()
        },
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, _FIXTURE, (mapping.reference,), payload, authorization)


def _run_variant(
    root: Path,
    variant: str,
    fixture: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _attempt_reference(variant)
    if _try_load(root, reference) is not None:
        raise RuntimeError("#11352 fresh observation already exists")
    binary = _ordinary_file(root / _PAIR / variant / "bin/minetestserver", root)
    tree = _ordinary_dir(root / _PAIR / variant, root)
    world = root / _ROOT / "worlds" / variant
    if world.exists() or world.is_symlink():
        raise RuntimeError("#11352 fresh runtime world already exists without receipt")
    world.mkdir(parents=True, mode=0o700)
    config = root / _ROOT / f"{variant}.conf"
    config.write_text(
        "name = c1\nmg_name = singlenode\nwater_level = 20\n"
        "server_announce = false\n"
        f"port = {31352 if variant == 'fixed' else 31353}\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(root / _ROOT / "home" / variant),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_SUBGAME_PATH": str(root / _ROOT / "games"),
        "PATH": "/usr/bin:/bin",
    }
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = run_bounded_process(
        (
            str(binary),
            "--world",
            str(world),
            "--gameid",
            _GAME_ID,
            "--config",
            str(config),
        ),
        cwd=tree,
        env=env,
        timeout_seconds=30,
    )
    merged = receipt.stdout + b"\n" + receipt.stderr
    start = _one(_START, merged, "start")
    observation = _one(_OBSERVATION, merged, "observation")
    invariant = _one(_INVARIANT, merged, "invariant")
    expected = "goal_map_default_isolated" if variant == "fixed" else "defect_global_setting_mutated"
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "observed",
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "start_marker": start,
        "semantic_observation": observation,
        "expected_observation": expected,
        "matched_expected": observation == expected,
        "invariant_marker": invariant,
        "start_equivalent_value": start == "20,20",
        "invariant_preserved": invariant == "server_responsive",
        "no_timeout_or_unknown": receipt.outcome == "completed",
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "candidate_execution_performed": True,
        "fresh_receipt": True,
        "old_attempt_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    if not (
        receipt.outcome == "completed"
        and receipt.returncode == 0
        and payload["matched_expected"]
        and payload["start_equivalent_value"]
        and payload["invariant_preserved"]
    ):
        raise RuntimeError(f"#11352 {variant} bounded replay did not satisfy oracle: {payload}")
    return _write_once(root, reference, (fixture.reference,), payload, authorization)


def _write_oracle(
    root: Path,
    mapping: StableArtifact,
    fixture: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    by_variant = {str(item.payload["variant"]): item for item in attempts}
    fixed = by_variant["fixed"].payload
    buggy = by_variant["buggy"].payload
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "outcome": "native_semantic_replay_oracle_passed",
        "complete": True,
        "gate9_registered_start": "passed_semantic_start",
        "gate10_replay_and_hidden_oracle": "passed",
        "registered_start_equal": fixed["start_marker"] == buggy["start_marker"],
        "ordered_replay_executed": True,
        "fixed_goal_observation": fixed["semantic_observation"],
        "buggy_defect_observation": buggy["semantic_observation"],
        "fixed_invariant_preserved": fixed["invariant_preserved"],
        "buggy_invariant_preserved": buggy["invariant_preserved"],
        "no_timeout_or_unknown": fixed["no_timeout_or_unknown"] and buggy["no_timeout_or_unknown"],
        "same_lineage": {
            "materialization": _MATERIALIZATION.artifact_id,
            "fixed_build": _FIXED_BUILD.artifact_id,
            "buggy_build": _BUGGY_BUILD.artifact_id,
            "mapping": mapping.reference.artifact_id,
            "fixture": fixture.reference.artifact_id,
        },
        "gate11_to_13": "pending_real_client_frames",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _ORACLE,
        (mapping.reference, fixture.reference, *(item.reference for item in attempts)),
        payload,
        authorization,
    )


def _write_visual_blocker(
    root: Path,
    mapping: StableArtifact,
    oracle: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    clients = tuple(
        root / _PAIR / variant / "bin" / name
        for variant in ("fixed", "buggy")
        for name in ("minetest", "luanti")
    )
    if any(path.exists() or path.is_symlink() for path in clients):
        raise RuntimeError("#11352 client appeared; visual blocker requires re-audit")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "does_not_invalidate_gate_1_7": True,
        "completed_downstream": {
            "gate8_fixed_buggy_materialization_and_server_build": True,
            "gate9_registered_semantic_start": True,
            "gate10_ordered_replay_hidden_oracle_semantic_observations": True,
        },
        "pending_downstream": {
            "gate11_raw_common_before_fixed_after_buggy_after": True,
            "gate12_before_equivalence_and_after_2x2_visual_cross_judgment": True,
            "gate13_same_lineage_visual_construction_receipt": True,
            "formal_freeze_admission": True,
        },
        "reason": "candidate-owned builds are server-only; the mapping requires real client frames",
        "synthetic_frames_allowed": False,
        "formal_admission_written": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "build candidate-owned fixed and buggy clients when the native lane is free, then capture registered-Start frames from this same mapping and fixture lineage",
    }
    return _write_once(
        root,
        _VISUAL_BLOCKER,
        (mapping.reference, oracle.reference),
        payload,
        authorization,
    )


def _attempt_reference(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate9_11352_runtime_observation/luanti/11352/{variant}/bounded-v2",
        "candidate_gate9_11352_runtime_observation",
        "1.0",
    )


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is None:
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
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError(f"#11352 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#11352 typed reload failed: {reference.artifact_id}")
    return result


def _one(pattern: re.Pattern[bytes], value: bytes, label: str) -> str:
    matches = pattern.findall(value)
    if len(matches) != 1:
        raise RuntimeError(f"#11352 runtime {label} marker count is invalid")
    return matches[0].decode("utf-8")


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#11352 runtime directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("#11352 runtime binary is unsafe")
    return resolved


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


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11352 runtime authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_11352_bounded_replay"]
