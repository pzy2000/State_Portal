"""Candidate-owned build and fresh Gate 9/10 closure for Luanti #9572."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct

from .luanti_native_executor import run_bounded_process
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE = {"repository_key": "luanti", "change_number": 9572}
_PAIR = PurePosixPath("private/materialized/luanti/9572/current-v2")
_BUILD = PurePosixPath("private/bounded-builds/luanti/9572/gate8-v1")
_RUN = PurePosixPath("private/bounded-runs/luanti/9572/gate9-v2")
_HOME = PurePosixPath("private/runtime-home/luanti-9572-gate9-v2")
_FIXTURE = PurePosixPath("private/downstream-fixtures/luanti/9572/vector-rotate-v2")
_READINESS = ArtifactReference("candidate_gate8_readiness/luanti/9572/current-v2/v2", "candidate_gate8_readiness", "2.0")
_DEPENDENCY = ArtifactReference("candidate_gate8_9572_irrlicht184_source/luanti/9572/current-v2/v1", "candidate_gate8_9572_irrlicht184_source", "1.0")
_PAIR_REF = ArtifactReference("candidate_gate8_9572_source_pair/luanti/9572/production-only/v1", "candidate_gate8_9572_source_pair", "1.0")
_TRANSITION57 = ArtifactReference("candidate_gate57_9572_strict_transition/luanti/9572/lua/v4", "candidate_gate57_9572_strict_transition", "1.0")
_CONFIGURES = {
    variant: ArtifactReference(f"candidate_gate8_9572_configure/luanti/9572/{variant}/v1", "candidate_gate8_9572_configure", "1.0")
    for variant in ("fixed", "buggy")
}
_BUILDS = {
    variant: ArtifactReference(f"candidate_gate8_9572_native_build/luanti/9572/{variant}/v1", "candidate_gate8_9572_native_build", "1.0")
    for variant in ("fixed", "buggy")
}
_MATERIALIZATION = ArtifactReference("candidate_materialization/luanti/9572/fixed-buggy/v3", "candidate_materialization", "3.0")
_MAPPING = ArtifactReference("lazarus_task_mapping/luanti/9572/lz-luanti-9572-vector-rotate/v2", "lazarus_task_mapping", "2.0")
_START = ArtifactReference("candidate_runtime_start/luanti/9572/fixed-buggy/v2", "candidate_runtime_start", "2.0")
_REPLAY = ArtifactReference("candidate_replay/luanti/9572/ordered-v2", "candidate_replay", "2.0")
_ORACLE = ArtifactReference("candidate_oracle/luanti/9572/hidden-v2", "candidate_oracle", "2.0")
_TRANSITION10 = ArtifactReference("candidate_gate10_transition/luanti/9572/runtime-v2", "candidate_gate10_transition", "2.0")
_STATIC = (_READINESS, _DEPENDENCY, _PAIR_REF, _TRANSITION57, _CONFIGURES["fixed"], _CONFIGURES["buggy"])

_MOD_INIT = r'''minetest.log("action", "C1_LAZARUS_9572_START state=common_before")
local rotate_type = type(vector.rotate)
local rotate_ok = false
if rotate_type == "function" then
  local out = vector.rotate({x = 1, y = 0, z = 0}, {x = 0, y = 0, z = math.pi / 2})
  rotate_ok = math.abs(out.x) < 0.000001 and math.abs(out.y + 1) < 0.000001 and math.abs(out.z) < 0.000001
end
local add = vector.add({x = 1, y = 2, z = 3}, {x = 4, y = 5, z = 6})
local add_ok = add.x == 5 and add.y == 7 and add.z == 9
minetest.log("action", "C1_LAZARUS_9572_OBSERVATION rotate_type=" .. rotate_type ..
  " rotate_ok=" .. tostring(rotate_ok) .. " add_ok=" .. tostring(add_ok))
minetest.after(0.2, function()
  minetest.request_shutdown("C1 Lazarus 9572 observation complete", false, 0)
end)
'''
_OBS = re.compile(rb"C1_LAZARUS_9572_OBSERVATION rotate_type=([^ ]+) rotate_ok=(true|false) add_ok=(true|false)")


def register_luanti_9572_builds(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    _auth(authorization); root = Path(artifact_root).absolute().resolve(strict=True)
    static = _static(root)
    builds = tuple(_register_build(root, variant, static, authorization) for variant in ("fixed", "buggy"))
    materialization = _write(root, _MATERIALIZATION, tuple(item.reference for item in static) + tuple(item.reference for item in builds), {
        "candidate": _CANDIDATE, "state": "built", "outcome": "candidate_owned_fixed_buggy_server_pair_built", "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed", "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "build_receipts": [item.reference.artifact_id for item in builds], "production_only_difference": "builtin/common/vector.lua",
        "candidate_owned_dependency": static[1].reference.artifact_id, "old_attempts_loaded": False,
        "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, authorization)
    mapping = _register_mapping(root, static, materialization, authorization)
    return builds[0], builds[1], materialization, mapping


def execute_luanti_9572_runtime(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, ...]:
    _auth(authorization); root = Path(artifact_root).absolute().resolve(strict=True)
    static = _static(root); materialization = _load(root, _MATERIALIZATION); mapping = _load(root, _MAPPING)
    if (root / _RUN).exists() or (root / _HOME).exists(): raise RuntimeError("#9572 runtime namespace is not fresh")
    observations = tuple(_run(root, variant, static, materialization, mapping, authorization) for variant in ("fixed", "buggy"))
    passed = all(item.payload.get("complete") is True for item in observations)
    start = _write(root, _START, tuple(item.reference for item in static) + (materialization.reference, mapping.reference) + tuple(item.reference for item in observations), {
        "candidate": _CANDIDATE, "state": "passed" if passed else "blocked", "outcome": "registered_start_completed" if passed else "registered_start_incomplete", "complete": passed,
        "variant_observations": [item.reference.artifact_id for item in observations], "no_timeout_or_unknown": passed,
        "old_attempts_loaded": False, "candidate_execution_performed": True, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, authorization)
    replay = _write(root, _REPLAY, tuple(item.reference for item in static) + (materialization.reference, mapping.reference, start.reference) + tuple(item.reference for item in observations), {
        "candidate": _CANDIDATE, "state": "passed" if passed else "blocked", "outcome": "ordered_replay_completed" if passed else "ordered_replay_incomplete", "complete": passed,
        "mapping_order": mapping.payload["ordered_replay"], "variant_order": ["fixed", "buggy"],
        "old_attempts_loaded": False, "candidate_execution_performed": True, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, authorization)
    oracle_data = json.loads(_file(root / _FIXTURE / "oracle.json", root).read_text())
    observed = {str(item.payload["variant"]): item.payload["semantic_observation"] for item in observations}
    expected = {"fixed": oracle_data["fixed_expected"], "buggy": oracle_data["buggy_expected"]}
    matched = observed == expected
    oracle = _write(root, _ORACLE, tuple(item.reference for item in static) + (materialization.reference, mapping.reference, start.reference, replay.reference) + tuple(item.reference for item in observations), {
        "candidate": _CANDIDATE, "state": "passed" if matched else "blocked", "outcome": "hidden_semantic_oracle_passed" if matched else "hidden_semantic_oracle_failed", "complete": matched,
        "oracle_loaded_after_observations": True, "expected": expected, "observed": observed,
        "semantic_goal_observed": observed.get("fixed") == expected["fixed"], "semantic_defect_observed": observed.get("buggy") == expected["buggy"],
        "p2p_add_invariant_observed": all(bool(value and value.get("add_ok")) for value in observed.values()),
        "no_timeout_or_unknown": passed, "old_attempts_loaded": False, "candidate_execution_performed": True,
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, authorization)
    done = passed and matched
    transition = _write(root, _TRANSITION10, tuple(item.reference for item in static) + (materialization.reference, mapping.reference, start.reference, replay.reference, oracle.reference), {
        "candidate": _CANDIDATE, "state": "passed" if done else "blocked", "outcome": "gate9_gate10_passed" if done else "gate9_gate10_incomplete", "complete": done, "terminal": False, "resumable": True,
        "gate_status": {"gate_1_7": "qualified_strict_v4", "gate8": "passed", "gate9_registered_start_replay": "passed" if done else "blocked", "gate10_hidden_oracle": "passed" if done else "blocked", "gate11_to_13": "pending"},
        "does_not_invalidate_gate_1_7": True, "old_attempts_loaded": False, "candidate_execution_performed": True,
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, authorization)
    return (*observations, start, replay, oracle, transition)


def _static(root: Path) -> tuple[StableArtifact, ...]:
    values = tuple(_load(root, ref) for ref in _STATIC)
    if values[0].payload.get("state") != "configured_waiting_native_lane" or values[3].payload.get("state") != "passed": raise RuntimeError("#9572 static readiness drifted")
    return values


def _register_build(root: Path, variant: str, static: tuple[StableArtifact, ...], auth: AuthorizationRecord) -> StableArtifact:
    tree = _dir(root / _PAIR / variant, root); binary = _executable(tree / "bin/minetestserver", root)
    version = subprocess_run((str(binary), "--version"), tree)
    pagezero = _pagezero(binary)
    if version[0] != 0 or pagezero != 0x100000000: raise RuntimeError(f"#9572 {variant} binary preflight failed")
    return _write(root, _BUILDS[variant], tuple(item.reference for item in static), {
        "candidate": _CANDIDATE, "variant": variant, "state": "built", "outcome": "completed", "complete": True,
        "argv": ["ninja", "-C", f"{_BUILD.as_posix()}/{variant}", "-j1", "minetestserver"],
        "build_parallelism": 1, "binary_path": binary.relative_to(root).as_posix(), "binary_fingerprint": _fingerprint(binary),
        "pagezero_vmsize": pagezero, "version_returncode": version[0], "version_stdout": version[1],
        "candidate_owned_dependency": static[1].reference.artifact_id, "old_attempts_loaded": False,
        "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, auth)


def _register_mapping(root: Path, static: tuple[StableArtifact, ...], materialization: StableArtifact, auth: AuthorizationRecord) -> StableArtifact:
    fixture = _dir(root / _FIXTURE, root); oracle = json.loads(_file(fixture / "oracle.json", root).read_text()); replay = json.loads(_file(fixture / "replay.json", root).read_text())
    payload = {
        "candidate": _CANDIDATE, "task_id": "lz-luanti-9572-vector-rotate", "profile_id": "luanti-9572-rotation-fresh-v2", "issue_number": 9798,
        "defect_locus": "at_goal", "state": "mapping_registered_fixture_frozen", "complete": True,
        "fixture_root": _FIXTURE.as_posix(), "fixture_files": ["start.json", "replay.json", "oracle.json", "golden-contract.json"],
        "ordered_replay": replay["ordered_actions"], "semantic_oracle": {"goal": oracle["goal"], "defect": oracle["defect"]},
        "candidate_materialization": materialization.reference.artifact_id,
        "golden_contract": {"common_before_fixed_after_buggy_after": True, "pairwise_before_visual_equivalence": True, "two_by_two_after_cross_matrix": True, "same_lineage_required": True},
        "old_runtime_inheritance": False, "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    return _write(root, _MAPPING, tuple(item.reference for item in static) + (materialization.reference,), payload, auth)


def _run(root: Path, variant: str, static: tuple[StableArtifact, ...], materialization: StableArtifact, mapping: StableArtifact, auth: AuthorizationRecord) -> StableArtifact:
    tree = _dir(root / _PAIR / variant, root); binary = _executable(tree / "bin/minetestserver", root)
    target = root / _RUN / variant; game = target / "games/c1_lazarus_9572"; mod = game / "mods/c1_observer"; world = target / "world"; home = root / _HOME / variant
    for path in (mod, world, home): path.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text("name = c1_lazarus_9572\ntitle = C1 Lazarus 9572\n", encoding="utf-8")
    (mod / "mod.conf").write_text("name = c1_observer\n", encoding="utf-8"); (mod / "init.lua").write_text(_MOD_INIT, encoding="utf-8")
    (world / "world.mt").write_text("gameid = c1_lazarus_9572\nbackend = dummy\nplayer_backend = dummy\nauth_backend = dummy\n", encoding="utf-8")
    config = target / "minetest.conf"; config.write_text("server_announce = false\nenable_ipv6 = false\n", encoding="utf-8")
    argv = (str(binary), "--world", str(world), "--gameid", "c1_lazarus_9572", "--config", str(config), "--logfile", "")
    receipt = run_bounded_process(argv, cwd=tree, env={**os.environ, "HOME": str(home), "LANG": "C", "LC_ALL": "C", "MINETEST_SUBGAME_PATH": str(target / "games")}, timeout_seconds=120)
    matches = [match for stream in (receipt.stdout, receipt.stderr) for match in _OBS.finditer(stream)]
    semantic = None
    if len(matches) == 1:
        semantic = {"rotate_type": matches[0].group(1).decode(), "rotate_ok": matches[0].group(2) == b"true", "add_ok": matches[0].group(3) == b"true"}
    ref = ArtifactReference(f"candidate_runtime_observation/luanti/9572/{variant}/v2", "candidate_runtime_observation", "2.0")
    return _write(root, ref, tuple(item.reference for item in static) + (materialization.reference, mapping.reference), {
        "candidate": _CANDIDATE, "variant": variant, "fresh_epoch": "luanti-9572-gate9-v2", "process_outcome": receipt.outcome, "returncode": receipt.returncode,
        "selected_line_count": len(matches), "semantic_observation": semantic, "stdout_base64": base64.b64encode(receipt.stdout).decode(), "stderr_base64": base64.b64encode(receipt.stderr).decode(),
        "stdout_truncated": receipt.stdout_truncated, "stderr_truncated": receipt.stderr_truncated,
        "oracle_expectation_visible_during_execution": False, "complete": receipt.outcome == "completed" and len(matches) == 1,
        "old_attempts_loaded": False, "old_runtime_inheritance": False, "candidate_execution_performed": True,
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, auth)


def subprocess_run(argv: tuple[str, ...], cwd: Path) -> tuple[int, str]:
    import subprocess
    result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False, timeout=15)
    return result.returncode, (result.stdout + result.stderr).decode(errors="replace")


def _pagezero(path: Path) -> int:
    raw = path.read_bytes(); offset = 32
    for _ in range(struct.unpack_from("<I", raw, 16)[0]):
        command, size = struct.unpack_from("<II", raw, offset)
        if command == 0x19 and struct.unpack_from("<16s", raw, offset + 8)[0].split(b"\0", 1)[0] == b"__PAGEZERO": return int(struct.unpack_from("<Q", raw, offset + 32)[0])
        offset += size
    raise RuntimeError("missing PAGEZERO")


def _auth(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope != "preannotation-execution" or value.revoked or value.local_user_id != os.getuid(): raise PermissionError("inactive authorization")


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], auth: AuthorizationRecord) -> StableArtifact:
    try: existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}: raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload: raise RuntimeError(f"artifact conflict {ref.artifact_id}")
        return existing
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=auth)
    return _load(root, ref)


def _dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir(): raise RuntimeError("unsafe directory")
    return resolved


def _file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file(): raise RuntimeError("unsafe file")
    return resolved


def _executable(path: Path, root: Path) -> Path:
    result = _file(path, root)
    if not os.access(result, os.X_OK): raise RuntimeError("not executable")
    return result


def _fingerprint(path: Path) -> str:
    import hashlib
    value = hashlib.sha256(); value.update(path.read_bytes()); return value.hexdigest()


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["execute_luanti_9572_runtime", "register_luanti_9572_builds"]
