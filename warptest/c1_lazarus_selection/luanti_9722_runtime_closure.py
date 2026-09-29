"""Fresh native Start/replay/oracle closure for Luanti #9722."""

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


CONFIRMATION = "EXECUTE_LUANTI_9722_BOUNDED_REPLAY_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
_BUILD = ArtifactReference(
    "candidate_gate8_9722_native_build/luanti/9722/irrlicht184-v1",
    "candidate_gate8_9722_native_build",
    "1.0",
)
_STRICT_AGGREGATE = ArtifactReference(
    "candidate_gate57_aggregate/luanti/9722/lua/v1",
    "candidate_gate57_aggregate",
    "1.0",
)
_STRICT_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/9722/lua/v1",
    "candidate_gate57_transition",
    "1.0",
)
_OLD_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/9722/lz-luanti-9722-reversible-number-serialize/v3",
    "lazarus_task_mapping",
    "3.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/9722/lz-luanti-9722-reversible-number-serialize/v7",
    "lazarus_task_mapping",
    "7.0",
)
_FIXTURE = ArtifactReference(
    "candidate_gate9_9722_runtime_fixture/luanti/9722/bounded-v4",
    "candidate_gate9_9722_runtime_fixture",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_9722_replay_oracle/luanti/9722/bounded-v4",
    "candidate_gate10_9722_replay_oracle",
    "1.0",
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_13_9722_visual_blocker/luanti/9722/current-v2/v1",
    "candidate_gate11_13_9722_visual_blocker",
    "1.0",
)
_PAIR = PurePosixPath("private/materialized/luanti/9722/application-v2")
_ROOT = PurePosixPath("private/runtime-fixtures/luanti/9722/bounded-v4")
_GAME_ID = "c1_lazarus_9722"
_GAME_CONF = b"name = c1_lazarus_9722\ntitle = C1 Lazarus 9722\n"
_MOD_CONF = b"name = c1_lazarus\n"
_INIT = b'''minetest.register_node(":basenodes:stone", {description = "stone"})
minetest.register_node(":basenodes:cobble", {description = "cobble"})
local p0 = {x = 0, y = 0, z = 0}
local p1 = {x = 0, y = 1, z = 0}
local function execute_replay()
  local vm = minetest.get_voxel_manip()
  vm:read_from_map(p0, p1)
  minetest.set_node(p0, {name = "basenodes:stone"})
  minetest.set_node(p1, {name = "basenodes:cobble"})
  local before = minetest.get_node(p0).name .. "," .. minetest.get_node(p1).name
  minetest.log("action", "C1_LAZARUS_9722_START " .. before)
  local values = {0.2695949158945771, 269594915894577}
  local encoded = minetest.serialize(values)
  local decoded = minetest.deserialize(encoded)
  local exact1 = decoded[1] == values[1]
  local exact2 = decoded[2] == values[2]
  local class = (exact1 and exact2) and "goal_reversible_numbers" or "defect_number_changed"
  local after = minetest.get_node(p0).name .. "," .. minetest.get_node(p1).name
  minetest.log("action", "C1_LAZARUS_9722_SERIALIZED " .. encoded:gsub("[\\r\\n]", " "))
  minetest.log("action", "C1_LAZARUS_9722_OBSERVATION " .. class)
  minetest.log("action", "C1_LAZARUS_9722_INVARIANT " .. after)
  minetest.request_shutdown("C1_LAZARUS_9722_DONE", false, 0)
end
minetest.after(0, execute_replay)
'''
_START = re.compile(rb"C1_LAZARUS_9722_START ([^\r\n]+)")
_OBSERVATION = re.compile(rb"C1_LAZARUS_9722_OBSERVATION ([^\r\n]+)")
_INVARIANT = re.compile(rb"C1_LAZARUS_9722_INVARIANT ([^\r\n]+)")


def execute_luanti_9722_bounded_replay(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    build, aggregate, transition, old_mapping = (
        _load(root, ref)
        for ref in (_BUILD, _STRICT_AGGREGATE, _STRICT_TRANSITION, _OLD_MAPPING)
    )
    if (
        build.payload.get("candidate") != _CANDIDATE
        or build.payload.get("state") != "built"
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or transition.payload.get("state") != "passed"
        or old_mapping.payload.get("candidate") != _CANDIDATE
        or old_mapping.payload.get("golden_contract", {}).get("raw_frames_required") is not True
    ):
        raise RuntimeError("#9722 runtime chain drifted")
    mapping = _write_mapping(root, build, aggregate, transition, old_mapping, authorization)
    fixture = _write_fixture(root, mapping, authorization)
    attempts = tuple(
        _run_variant(root, variant, build, fixture, authorization)
        for variant in ("fixed", "buggy")
    )
    oracle = _write_oracle(root, mapping, fixture, attempts, authorization)
    blocker = _write_visual_blocker(root, mapping, oracle, authorization)
    return mapping, fixture, oracle, blocker


def _write_mapping(
    root: Path,
    build: StableArtifact,
    aggregate: StableArtifact,
    transition: StableArtifact,
    old_mapping: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = dict(old_mapping.payload)
    payload.update(
        {
            "fixture_root": _ROOT.as_posix(),
            "native_build": build.reference.artifact_id,
            "strict_gate_1_7": transition.reference.artifact_id,
            "mapping_rebind": "candidate_owned_irrlicht_1_8_4_native_server_pair",
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        }
    )
    return _write_once(
        root,
        _MAPPING,
        (old_mapping.reference, aggregate.reference, transition.reference, build.reference),
        payload,
        authorization,
    )


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
                raise RuntimeError("#9722 runtime fixture drifted")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(content)
    builtin_adapters = {
        variant: _ordinary_dir(root / _PAIR / variant / "bin/builtin", root)
        for variant in ("fixed", "buggy")
    }
    for variant, adapter in builtin_adapters.items():
        source_init = root / _PAIR / variant / "builtin/init.lua"
        adapter_init = adapter / "init.lua"
        if source_init.read_bytes() != adapter_init.read_bytes():
            raise RuntimeError(f"#9722 {variant} builtin runtime adapter drifted")
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
        "runtime_resource_adapter": {
            variant: adapter.relative_to(root).as_posix()
            for variant, adapter in builtin_adapters.items()
        },
        "runtime_resource_origin": "same_candidate_same_variant_builtin_tree",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, _FIXTURE, (mapping.reference,), payload, authorization)


def _run_variant(
    root: Path,
    variant: str,
    build: StableArtifact,
    fixture: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _attempt_reference(variant)
    if _try_load(root, reference) is not None:
        raise RuntimeError("#9722 fresh observation already exists")
    binary = _ordinary_executable(root / PurePosixPath(str(build.payload[f"{variant}_binary"])), root)
    tree = _ordinary_dir(root / _PAIR / variant, root)
    world = root / _ROOT / "worlds" / variant
    if world.exists() or world.is_symlink():
        raise RuntimeError("#9722 fresh world already exists without receipt")
    world.mkdir(parents=True, mode=0o700)
    config = root / _ROOT / f"{variant}.conf"
    config.write_text(
        "name = c1\nmg_name = singlenode\nserver_announce = false\n"
        f"port = {39722 if variant == 'fixed' else 39723}\n",
        encoding="utf-8",
    )
    home = root / _ROOT / "home" / variant
    (home / "Library/Application Support").mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_SUBGAME_PATH": str(root / _ROOT / "games"),
        "PATH": "/usr/bin:/bin",
    }
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
    expected = "goal_reversible_numbers" if variant == "fixed" else "defect_number_changed"
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
        "start_equivalent_value": start == "basenodes:stone,basenodes:cobble",
        "invariant_preserved": invariant == start,
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
        raise RuntimeError(f"#9722 {variant} replay did not satisfy oracle: {payload}")
    return _write_once(root, reference, (build.reference, fixture.reference), payload, authorization)


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
            "build": _BUILD.artifact_id,
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
        raise RuntimeError("#9722 client appeared; visual blocker requires re-audit")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "does_not_invalidate_gate_1_7": True,
        "completed_downstream": {
            "gate8_fixed_buggy_native_server_build": True,
            "gate9_registered_semantic_start": True,
            "gate10_ordered_replay_hidden_oracle_semantic_observations": True,
        },
        "pending_downstream": {
            "gate11_raw_common_before_fixed_after_buggy_after": True,
            "gate12_before_equivalence_and_after_2x2_visual_cross_judgment": True,
            "gate13_same_lineage_visual_construction_receipt": True,
            "formal_freeze_admission": True,
        },
        "reason": "candidate-owned builds are server-only; mapping requires real client frames",
        "synthetic_frames_allowed": False,
        "formal_admission_written": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "build candidate-owned fixed and buggy clients against Irrlicht 1.8.4 when the native lane is free, then capture same-lineage registered-Start frames",
    }
    return _write_once(root, _VISUAL_BLOCKER, (mapping.reference, oracle.reference), payload, authorization)


def _attempt_reference(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate9_9722_runtime_observation/luanti/9722/{variant}/bounded-v4",
        "candidate_gate9_9722_runtime_observation",
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
        raise RuntimeError(f"#9722 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#9722 typed reload failed: {reference.artifact_id}")
    return result


def _one(pattern: re.Pattern[bytes], value: bytes, label: str) -> str:
    matches = pattern.findall(value)
    if len(matches) != 1:
        raise RuntimeError(f"#9722 runtime {label} marker count is invalid")
    return matches[0].decode("utf-8")


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#9722 runtime directory is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("#9722 runtime binary is unsafe")
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
        raise PermissionError("#9722 runtime authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_9722_bounded_replay"]
