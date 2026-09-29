"""Fresh registered-Start and semantic replay closure for Luanti #11866."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
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


CONFIRMATION = "EXECUTE_LUANTI_11866_BOUNDED_REPLAY_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
_BUILD = ArtifactReference(
    "candidate_gate8_11866_native_build/luanti/11866/mt4-v1",
    "candidate_gate8_11866_native_build",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_11866_irrlichtmt_dependency/luanti/11866/mt4-v1",
    "candidate_gate8_11866_irrlichtmt_dependency",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/11866/gate8-mt4-v1",
    "candidate_materialization",
    "1.0",
)
_STRICT_AGGREGATE = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_aggregate/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_aggregate",
    "1.0",
)
_STRICT_TRANSITION = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_transition/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_transition",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/11866/lz-luanti-11866-mapgen-boundary/v3",
    "lazarus_task_mapping",
    "3.0",
)
_FIXTURE = ArtifactReference(
    "candidate_gate9_11866_runtime_fixture/luanti/11866/bounded-v3",
    "candidate_gate9_11866_runtime_fixture",
    "3.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_11866_replay_oracle/luanti/11866/bounded-v3",
    "candidate_gate10_11866_replay_oracle",
    "3.0",
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_13_11866_visual_blocker/luanti/11866/mt4-v3",
    "candidate_gate11_13_11866_visual_blocker",
    "3.0",
)
_PAIR = PurePosixPath("private/materialized/luanti/11866/gate8-mt4-v1")
_ROOT = PurePosixPath("private/runtime-fixtures/luanti/11866/bounded-v3")
_APP_ROOT = PurePosixPath("private/runtime-apps/luanti/11866/bounded-v3")
_GAME_ID = "c1_lazarus_11866"
_GAME_CONF = b"name = c1_lazarus_11866\ntitle = C1 Lazarus 11866\n"
_MOD_CONF = b"name = c1_lazarus\n"
_INIT = b'''local storage = minetest.get_mod_storage()
local function execute_replay()
  storage:set_string("registered_start", "c1-11866-ready")
  local before = storage:get_string("registered_start")
  minetest.log("action", "C1_LAZARUS_11866_START marker=" .. before)
  local raw = minetest.settings:get("mapgen_limit")
  local limit = tonumber(raw)
  local remainder = limit and (limit % 16) or -1
  local class = remainder == 15 and
    "goal_mapgen_limit_aligned" or "defect_mapgen_limit_misaligned"
  minetest.log("action", "C1_LAZARUS_11866_REPLAY limit=" .. tostring(limit) ..
    " blocksize=16 remainder=" .. tostring(remainder))
  minetest.log("action", "C1_LAZARUS_11866_OBSERVATION " .. class)
  minetest.log("action", "C1_LAZARUS_11866_INVARIANT marker=" .. storage:get_string("registered_start"))
  minetest.request_shutdown("C1_LAZARUS_11866_DONE", false, 0)
end
minetest.after(0, execute_replay)
'''
_START = re.compile(rb"C1_LAZARUS_11866_START marker=([^\r\n]+)")
_REPLAY = re.compile(
    rb"C1_LAZARUS_11866_REPLAY limit=([0-9]+) blocksize=16 remainder=([0-9]+)"
)
_OBSERVATION = re.compile(rb"C1_LAZARUS_11866_OBSERVATION ([^\r\n]+)")
_INVARIANT = re.compile(rb"C1_LAZARUS_11866_INVARIANT marker=([^\r\n]+)")


def execute_luanti_11866_bounded_replay(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    build, dependency, materialization, aggregate, transition = (
        _load(root, ref)
        for ref in (
            _BUILD,
            _DEPENDENCY,
            _MATERIALIZATION,
            _STRICT_AGGREGATE,
            _STRICT_TRANSITION,
        )
    )
    _validate_chain(build, dependency, materialization, aggregate, transition)
    mapping = _write_mapping(
        root,
        build,
        dependency,
        materialization,
        aggregate,
        transition,
        authorization,
    )
    fixture = _write_fixture(root, mapping, authorization)
    attempts = tuple(
        _run_variant(root, variant, build, fixture, authorization)
        for variant in ("fixed", "buggy")
    )
    oracle = _write_oracle(root, mapping, fixture, attempts, authorization)
    blocker = _write_visual_blocker(root, mapping, oracle, authorization)
    return mapping, fixture, oracle, blocker


def _validate_chain(
    build: StableArtifact,
    dependency: StableArtifact,
    materialization: StableArtifact,
    aggregate: StableArtifact,
    transition: StableArtifact,
) -> None:
    for item in (build, dependency, materialization, aggregate, transition):
        if item.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#11866 runtime chain candidate binding drifted")
    if not (
        build.payload.get("state") == "built"
        and build.payload.get("outcome") == "candidate_owned_mt4_fixed_buggy_server_build_passed"
        and dependency.payload.get("dependency_tag") == "1.9.0mt4"
        and dependency.payload.get("source_from_14464") is False
        and materialization.payload.get("state") == "materialized"
        and aggregate.payload.get("qualified") is True
        and aggregate.payload.get("attempt_count") == 12
        and transition.payload.get("outcome") == "gate57_qualified"
    ):
        raise RuntimeError("#11866 runtime chain is incomplete")


def _write_mapping(
    root: Path,
    build: StableArtifact,
    dependency: StableArtifact,
    materialization: StableArtifact,
    aggregate: StableArtifact,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-11866-mapgen-boundary",
        "profile_id": "luanti-11866-mapgen-limit-v1",
        "defect_locus": "at_goal",
        "issue_number": 11839,
        "complete": True,
        "strict_gate_1_7": transition.reference.artifact_id,
        "strict_gate_1_7_aggregate": aggregate.reference.artifact_id,
        "native_build": build.reference.artifact_id,
        "candidate_owned_dependency": dependency.reference.artifact_id,
        "materialization": materialization.reference.artifact_id,
        "fixture_root": _ROOT.as_posix(),
        "registered_start": {
            "mod_storage_marker": {"key": "registered_start", "value": "c1-11866-ready"},
            "setting": "mapgen_limit",
            "map_blocksize": 16,
        },
        "ordered_replay": [
            {"type": "restore_registered_start"},
            {"type": "read_engine_default", "key": "mapgen_limit"},
            {"type": "compute_boundary_remainder", "divisor": 16},
            {"type": "classify_goal_or_defect"},
            {"type": "verify_world_marker_invariant"},
        ],
        "semantic_oracle": {
            "goal": "mapgen_limit modulo MAP_BLOCKSIZE equals MAP_BLOCKSIZE minus one",
            "defect": "mapgen_limit is not aligned to the final node of a mapblock boundary",
            "fixed_expected": {"limit": 31007, "remainder": 15},
            "buggy_expected": {"limit": 31000, "remainder": 8},
            "invariants": ["world remains responsive", "registered marker is unchanged"],
        },
        "golden_contract": {
            "common_before_fixed_after_buggy_after": True,
            "pairwise_before_visual_equivalence": True,
            "two_by_two_after_cross_matrix": True,
            "same_lineage_required": True,
            "raw_frames_required": True,
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _MAPPING,
        (
            build.reference,
            dependency.reference,
            materialization.reference,
            aggregate.reference,
            transition.reference,
        ),
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
                raise RuntimeError("#11866 runtime fixture drifted")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(content)
    runtime_apps = _prepare_runtime_apps(root)
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
        "runtime_resource_adapter": runtime_apps,
        "runtime_adapter_scope": "disposable_candidate_same_variant_builtin_copy_only",
        "production_source_modified": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, _FIXTURE, (mapping.reference,), payload, authorization)


def _prepare_runtime_apps(root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        source = _ordinary_dir(root / _PAIR / variant, root)
        app = root / _APP_ROOT / variant
        binary = app / "bin/minetestserver"
        builtin = app / "builtin"
        if not app.exists():
            (app / "bin").mkdir(parents=True, mode=0o700)
            shutil.copy2(source / "bin/minetestserver", binary)
            shutil.copytree(source / "builtin", builtin, symlinks=False)
            misc = builtin / "game/misc.lua"
            text = misc.read_text(encoding="utf-8")
            opening = "core.set_http_api_lua(function(httpenv)"
            closing = "core.set_http_api_lua = nil"
            if text.count(opening) != 1 or text.count(closing) != 1:
                raise RuntimeError("#11866 runtime HTTP adapter locus drifted")
            text = text.replace(opening, "if core.set_http_api_lua then\n" + opening, 1)
            text = text.replace(closing, closing + "\nend", 1)
            misc.write_text(text, encoding="utf-8")
        app = _ordinary_dir(app, root)
        binary = _ordinary_executable(binary, root)
        misc = _ordinary_file(builtin / "game/misc.lua", root)
        text = misc.read_text(encoding="utf-8")
        if "if core.set_http_api_lua then\ncore.set_http_api_lua(function(httpenv)" not in text:
            raise RuntimeError("#11866 runtime HTTP adapter is unavailable")
        result[variant] = {
            "application_root": app.relative_to(root).as_posix(),
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "builtin_adapter_fingerprint": _tree_fingerprint(builtin),
            "http_adapter": "guard_set_http_api_lua_when_curl_is_disabled",
        }
    return result


def _run_variant(
    root: Path,
    variant: str,
    build: StableArtifact,
    fixture: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _attempt_ref(variant)
    if _try_load(root, reference) is not None:
        raise RuntimeError("#11866 fresh runtime observation already exists")
    app_value = fixture.payload.get("runtime_resource_adapter", {}).get(variant, {}).get("application_root")
    tree = _ordinary_dir(root / PurePosixPath(str(app_value)), root)
    binary = _ordinary_executable(tree / "bin/minetestserver", root)
    world = root / _ROOT / "worlds" / variant
    if world.exists() or world.is_symlink():
        raise RuntimeError("#11866 fresh runtime world already exists")
    world.mkdir(parents=True, mode=0o700)
    config = root / _ROOT / f"{variant}.conf"
    config.write_text(
        "name = c1\nmg_name = singlenode\nserver_announce = false\n"
        f"port = {31866 if variant == 'fixed' else 31867}\n",
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
    replay = _one_match(_REPLAY, merged, "replay")
    observation = _one(_OBSERVATION, merged, "observation")
    invariant = _one(_INVARIANT, merged, "invariant")
    limit = int(replay.group(1))
    remainder = int(replay.group(2))
    expected = (
        (31007, 15, "goal_mapgen_limit_aligned")
        if variant == "fixed"
        else (31000, 8, "defect_mapgen_limit_misaligned")
    )
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "observed",
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "start_marker": start,
        "mapgen_limit": limit,
        "map_blocksize": 16,
        "boundary_remainder": remainder,
        "semantic_observation": observation,
        "expected_observation": expected[2],
        "matched_expected": (limit, remainder, observation) == expected,
        "invariant_marker": invariant,
        "start_equal_to_invariant": start == invariant == "c1-11866-ready",
        "no_timeout_or_unknown": receipt.outcome == "completed",
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "candidate_execution_performed": True,
        "fresh_receipt": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    if not (
        receipt.outcome == "completed"
        and receipt.returncode == 0
        and payload["matched_expected"]
        and payload["start_equal_to_invariant"]
    ):
        raise RuntimeError(f"#11866 {variant} runtime oracle failed: {payload}")
    return _write_once(
        root,
        reference,
        (build.reference, fixture.reference),
        payload,
        authorization,
    )


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
        "outcome": "native_mapgen_boundary_replay_oracle_passed",
        "complete": True,
        "gate9_registered_start": "passed",
        "gate10_ordered_replay_hidden_oracle": "passed",
        "registered_start_equal": fixed["start_marker"] == buggy["start_marker"],
        "ordered_replay_executed": True,
        "fixed_goal_observation": fixed["semantic_observation"],
        "buggy_defect_observation": buggy["semantic_observation"],
        "fixed_limit": fixed["mapgen_limit"],
        "buggy_limit": buggy["mapgen_limit"],
        "fixed_boundary_remainder": fixed["boundary_remainder"],
        "buggy_boundary_remainder": buggy["boundary_remainder"],
        "fixed_invariant_preserved": fixed["start_equal_to_invariant"],
        "buggy_invariant_preserved": buggy["start_equal_to_invariant"],
        "no_timeout_or_unknown": fixed["no_timeout_or_unknown"] and buggy["no_timeout_or_unknown"],
        "same_lineage": {
            "build": _BUILD.artifact_id,
            "mapping": mapping.reference.artifact_id,
            "fixture": fixture.reference.artifact_id,
        },
        "gate11_to_13": "pending_real_client_frames",
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
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
    client_paths = tuple(root / _PAIR / variant / "bin/minetest" for variant in ("fixed", "buggy"))
    if any(path.exists() or path.is_symlink() for path in client_paths):
        raise RuntimeError("#11866 client appeared; visual blocker requires re-audit")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "does_not_invalidate_gate_1_7": True,
        "completed_downstream": {
            "gate8_candidate_owned_fixed_buggy_server_build": True,
            "gate9_registered_start": True,
            "gate10_ordered_replay_hidden_oracle_semantic_observations": True,
        },
        "pending_downstream": {
            "gate11_real_common_before_fixed_after_buggy_after": True,
            "gate12_before_equivalence_and_after_2x2": True,
            "gate13_same_lineage_visual_construction": True,
            "formal_freeze_admission": True,
        },
        "reason": "candidate-owned mt4 builds are server-only; real client frames are required",
        "synthetic_frames_allowed": False,
        "formal_admission_written": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "build candidate-owned fixed and buggy clients against IrrlichtMt 1.9.0mt4, then capture same-lineage common-before and goal-versus-defect frames",
    }
    return _write_once(
        root,
        _VISUAL_BLOCKER,
        (mapping.reference, oracle.reference),
        payload,
        authorization,
    )


def _attempt_ref(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate9_11866_runtime_observation/luanti/11866/{variant}/bounded-v3",
        "candidate_gate9_11866_runtime_observation",
        "3.0",
    )


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _one(pattern: re.Pattern[bytes], value: bytes, label: str) -> str:
    return _one_match(pattern, value, label).group(1).decode("utf-8")


def _one_match(pattern: re.Pattern[bytes], value: bytes, label: str) -> re.Match[bytes]:
    matches = list(pattern.finditer(value))
    if len(matches) != 1:
        raise RuntimeError(f"#11866 runtime {label} marker count is invalid")
    return matches[0]


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#11866 runtime directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#11866 runtime file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#11866 runtime binary is unsafe")
    return result


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _try_load(root: Path, ref: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, ref)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _write_once(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, ref)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=ref.artifact_id,
            artifact_kind=ref.artifact_kind,
            semantic_version=ref.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError(f"#11866 artifact conflicts: {ref.artifact_id}")
    result = _load(root, ref)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#11866 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 runtime authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_11866_bounded_replay"]
