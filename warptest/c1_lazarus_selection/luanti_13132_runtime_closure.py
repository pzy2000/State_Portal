"""Fresh bounded Start/replay/oracle closure for native Luanti #13132."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_13132_BOUNDED_REPLAY_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 13132}
_BUILD = ArtifactReference(
    "candidate_gate8_13132_native_build/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_native_build",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/13132/lz-luanti-13132-empty-separator/v3",
    "lazarus_task_mapping",
    "3.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_transition/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_transition",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_13132_irrlichtmt9_source/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_irrlichtmt9_source",
    "1.0",
)
_FIXTURE = ArtifactReference(
    "candidate_gate9_13132_runtime_fixture/luanti/13132/bounded-v6",
    "candidate_gate9_13132_runtime_fixture",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_13132_replay_oracle/luanti/13132/bounded-v6",
    "candidate_gate10_13132_replay_oracle",
    "1.0",
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_13_13132_visual_blocker/luanti/13132/current-v2/v1",
    "candidate_gate11_13_13132_visual_blocker",
    "1.0",
)
_ROOT = PurePosixPath("private/runtime-fixtures/luanti/13132/bounded-v6")
_GAME_ID = "c1_lazarus_13132"
_GAME_CONF = b"name = c1_lazarus_13132\ntitle = C1 Lazarus 13132\n"
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
  minetest.log("action", "C1_LAZARUS_13132_START " .. before)
  local ticks = 0
  debug.sethook(function()
    ticks = ticks + 1
    if ticks >= 1000 then error("C1_BOUND instruction limit", 0) end
  end, "", 10000)
  local ok, value = pcall(function() return string.split("", "") end)
  debug.sethook()
  local text = tostring(value)
  local class
  if ok then
    class = "unexpected_success"
  elseif text:find("separator is empty", 1, true) then
    class = "goal_fixed_argument_error"
  elseif text:find("C1_BOUND instruction limit", 1, true) then
    class = "defect_buggy_nontermination_bounded"
  else
    class = "unexpected_error"
  end
  local after = minetest.get_node(p0).name .. "," .. minetest.get_node(p1).name
  minetest.log("action", "C1_LAZARUS_13132_OBSERVATION " .. class)
  minetest.log("action", "C1_LAZARUS_13132_INVARIANT " .. after)
  minetest.request_shutdown("C1_LAZARUS_13132_DONE", false, 0)
end
minetest.after(0, execute_replay)
'''
_START = re.compile(rb"C1_LAZARUS_13132_START ([^\r\n]+)")
_OBSERVATION = re.compile(rb"C1_LAZARUS_13132_OBSERVATION ([^\r\n]+)")
_INVARIANT = re.compile(rb"C1_LAZARUS_13132_INVARIANT ([^\r\n]+)")


def execute_luanti_13132_bounded_replay(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Run one fresh ordered replay per side and persist its hidden oracle."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    build, mapping, transition, dependency = (
        _load(root, ref) for ref in (_BUILD, _MAPPING, _TRANSITION, _DEPENDENCY)
    )
    _validate_chain(root, build, mapping, transition, dependency)
    fixture = _register_fixture(root, build, mapping, transition, dependency, authorization)
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        existing = _try_load(root, _attempt_reference(variant))
        if existing is not None:
            attempts.append(existing)
            continue
        attempts.append(
            _run_variant(root, variant, build, fixture, authorization)
        )
    oracle = _write_oracle(root, build, mapping, fixture, attempts, authorization)
    blocker = _write_visual_blocker(root, build, mapping, oracle, authorization)
    return fixture, oracle, blocker


def _validate_chain(
    root: Path,
    build: StableArtifact,
    mapping: StableArtifact,
    transition: StableArtifact,
    dependency: StableArtifact,
) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in (build, mapping, transition, dependency)):
        raise RuntimeError("#13132 runtime chain candidate drifted")
    if (
        build.payload.get("state") != "built"
        or build.payload.get("single_threaded") is not True
        or mapping.payload.get("complete") is not True
        or mapping.payload.get("defect_locus") != "at_goal"
        or transition.payload.get("qualified") is not True
        or dependency.payload.get("tag") != "1.9.0mt9"
    ):
        raise RuntimeError("#13132 runtime chain is not admitted")
    for key in ("fixed_binary", "buggy_binary"):
        path = root / PurePosixPath(str(build.payload[key]))
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            raise RuntimeError("#13132 runtime binary is unavailable")


def _register_fixture(
    root: Path,
    build: StableArtifact,
    mapping: StableArtifact,
    transition: StableArtifact,
    dependency: StableArtifact,
    authorization: AuthorizationRecord,
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
                raise RuntimeError("#13132 runtime fixture drifted")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(content)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "fixture_root": _ROOT.as_posix(),
        "game_id": _GAME_ID,
        "registered_start": {
            "world_nodes": ["basenodes:stone@0,0,0", "basenodes:cobble@0,1,0"],
            "expected_marker": "basenodes:stone,basenodes:cobble",
        },
        "ordered_replay": ["restore_registered_start", "string.split('', '')"],
        "bounded_adapter": {
            "kind": "disposable_lua_instruction_hook",
            "instruction_interval": 10000,
            "max_ticks": 1000,
            "production_or_test_modified": False,
        },
        "file_fingerprints": {
            path.relative_to(root / _ROOT).as_posix(): hashlib.sha256(content).hexdigest()
            for path, content in files.items()
        },
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = (build.reference, mapping.reference, transition.reference, dependency.reference)
    existing = _try_load(root, _FIXTURE)
    if existing is None:
        _write(root, _FIXTURE, inputs, payload, authorization)
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#13132 runtime fixture receipt conflicts")
    return _load(root, _FIXTURE)


def _run_variant(
    root: Path,
    variant: str,
    build: StableArtifact,
    fixture: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    binary = root / PurePosixPath(str(build.payload[f"{variant}_binary"]))
    tree = binary.parent.parent
    world = root / _ROOT / "worlds" / variant
    if world.exists() or world.is_symlink():
        raise RuntimeError("#13132 fresh runtime world already exists without receipt")
    world.mkdir(parents=True, mode=0o700)
    config = root / _ROOT / f"{variant}.conf"
    config.write_text(
        "name = c1\nmg_name = singlenode\nserver_announce = false\n"
        f"port = {31032 if variant == 'fixed' else 31033}\n",
        encoding="utf-8",
    )
    argv = (
        str(binary),
        "--world",
        str(world),
        "--gameid",
        _GAME_ID,
        "--config",
        str(config),
    )
    env = {
        "HOME": str(root / _ROOT / "home" / variant),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_SUBGAME_PATH": str(root / _ROOT / "games"),
        "PATH": "/usr/bin:/bin",
    }
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=30)
    merged = receipt.stdout + b"\n" + receipt.stderr
    start = _one(_START, merged, "start")
    observation = _one(_OBSERVATION, merged, "observation")
    invariant = _one(_INVARIANT, merged, "invariant")
    expected = (
        "goal_fixed_argument_error"
        if variant == "fixed"
        else "defect_buggy_nontermination_bounded"
    )
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "observed",
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "start_marker": start,
        "semantic_observation": observation,
        "invariant_marker": invariant,
        "expected_observation": expected,
        "matched_expected": observation == expected,
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
        raise RuntimeError(f"#13132 {variant} bounded replay did not satisfy oracle")
    reference = _attempt_reference(variant)
    _write(root, reference, (build.reference, fixture.reference), payload, authorization)
    return _load(root, reference)


def _write_oracle(
    root: Path,
    build: StableArtifact,
    mapping: StableArtifact,
    fixture: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    by_variant = {str(item.payload["variant"]): item for item in attempts}
    if set(by_variant) != {"fixed", "buggy"}:
        raise RuntimeError("#13132 replay variant coverage is incomplete")
    fixed = by_variant["fixed"].payload
    buggy = by_variant["buggy"].payload
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "outcome": "bounded_semantic_replay_oracle_passed",
        "complete": True,
        "registered_start_equal": fixed["start_marker"] == buggy["start_marker"],
        "ordered_replay_executed": True,
        "fixed_goal_observation": fixed["semantic_observation"],
        "buggy_defect_observation": buggy["semantic_observation"],
        "fixed_invariant_preserved": fixed["invariant_preserved"],
        "buggy_invariant_preserved": buggy["invariant_preserved"],
        "no_timeout_or_unknown": fixed["no_timeout_or_unknown"] and buggy["no_timeout_or_unknown"],
        "same_lineage": {
            "build": build.reference.artifact_id,
            "fixture": fixture.reference.artifact_id,
            "dependency": _DEPENDENCY.artifact_id,
        },
        "gate9_registered_start": "passed_semantic_start",
        "gate10_replay_and_hidden_oracle": "passed",
        "gate11_to_13": "pending_real_client_frames",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = (build.reference, mapping.reference, fixture.reference, *(item.reference for item in attempts))
    existing = _try_load(root, _ORACLE)
    if existing is None:
        _write(root, _ORACLE, inputs, payload, authorization)
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#13132 replay oracle conflicts")
    return _load(root, _ORACLE)


def _write_visual_blocker(
    root: Path,
    build: StableArtifact,
    mapping: StableArtifact,
    oracle: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    contract = mapping.payload.get("golden_contract", {})
    if contract.get("raw_frames_required") is not True:
        raise RuntimeError("#13132 visual contract no longer requires raw frames")
    fixed_tree = (root / PurePosixPath(str(build.payload["fixed_binary"]))).parent.parent
    buggy_tree = (root / PurePosixPath(str(build.payload["buggy_binary"]))).parent.parent
    client_paths = [fixed_tree / "bin/minetest", fixed_tree / "bin/luanti", buggy_tree / "bin/minetest", buggy_tree / "bin/luanti"]
    if any(path.exists() for path in client_paths):
        raise RuntimeError("#13132 client binary appeared; visual blocker must be re-audited")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "does_not_invalidate_gate_1_7": True,
        "completed_downstream": {
            "gate8_fixed_buggy_materialization_and_build": True,
            "gate9_registered_semantic_start": True,
            "gate10_ordered_replay_hidden_oracle_semantic_observations": True,
        },
        "pending_downstream": {
            "gate11_common_before_fixed_after_buggy_after_raw_goldens": True,
            "gate12_before_equivalence_and_after_2x2_visual_cross_judgment": True,
            "gate13_same_lineage_visual_construction_receipt": True,
            "candidate_formal_freeze_admission": True,
        },
        "reason": "current candidate-owned build is server-only; required real client frames do not exist",
        "missing": [
            "candidate-owned fixed and buggy client binaries built against IrrlichtMt 1.9.0mt9",
            "raw common-before fixed-after buggy-after frames from registered player Start",
            "pairwise before equivalence and two-by-two after visual cross matrix",
            "same-lineage visual construction receipt",
        ],
        "synthetic_frames_allowed": False,
        "formal_admission_written": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "build fixed/buggy client in candidate-owned namespace and capture real registered-Start frames",
    }
    inputs = (build.reference, mapping.reference, oracle.reference)
    existing = _try_load(root, _VISUAL_BLOCKER)
    if existing is None:
        _write(root, _VISUAL_BLOCKER, inputs, payload, authorization)
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#13132 visual blocker conflicts")
    return _load(root, _VISUAL_BLOCKER)


def _attempt_reference(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate9_13132_runtime_observation/luanti/13132/{variant}/bounded-v6",
        "candidate_gate9_13132_runtime_observation",
        "1.0",
    )


def _one(pattern: re.Pattern[bytes], value: bytes, label: str) -> str:
    matches = pattern.findall(value)
    if len(matches) != 1:
        raise RuntimeError(f"#13132 runtime {label} marker count is invalid")
    return matches[0].decode("utf-8")


def _write(root: Path, reference: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> None:
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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_auth(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != "preannotation-execution" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#13132 runtime authorization is inactive")


def _now() -> str:
    return ((datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"))


__all__ = ["CONFIRMATION", "execute_luanti_13132_bounded_replay"]
