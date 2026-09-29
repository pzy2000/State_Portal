"""Deferred native build and fresh Gate 9/10 execution for Luanti #8704.

Registration is static and safe while another candidate owns the native lane.
The build and runtime functions require separate explicit confirmations.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess

from .luanti_native_executor import run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


BUILD_CONFIRMATION = "EXECUTE_LUANTI_8704_GATE8_SERVER_BUILD_V1"
RUNTIME_CONFIRMATION = "EXECUTE_LUANTI_8704_GATE9_10_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 8704}
_PAIR = PurePosixPath("private/materialized/luanti/8704/application-v1")
_SERVER_BUILD = PurePosixPath("private/bounded-builds/luanti/8704/gate8-v1")
_CLIENT_CONFIGURE = PurePosixPath(
    "private/bounded-builds/luanti/8704/gate11-client-skeleton-v1"
)
_DEP = PurePosixPath("private/dependencies/luanti-8704-irrlicht/1.8.4")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_RUN = PurePosixPath("private/bounded-runs/luanti/8704/gate9-v3")
_HOME = PurePosixPath("private/runtime-home/luanti-8704-gate9-v3")
_FIXTURE = PurePosixPath(
    "private/downstream-fixtures/luanti/8704/minimal-craft-result-v1"
)
_MAX_OUTPUT = 262_144

_READINESS = ArtifactReference(
    "candidate_gate8_readiness/luanti/8704/current-v2/v2",
    "candidate_gate8_readiness",
    "2.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_8704_irrlicht184_source/luanti/8704/current-v2/v1",
    "candidate_gate8_8704_irrlicht184_source",
    "1.0",
)
_SOURCE_PAIR = ArtifactReference(
    "candidate_gate8_8704_source_pair/luanti/8704/production-only/v1",
    "candidate_gate8_8704_source_pair",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/8704/lz-luanti-8704-minimal-craft-result/v1",
    "lazarus_task_mapping",
    "1.0",
)
_VISUAL_PLAN = ArtifactReference(
    "candidate_visual_plan/luanti/8704/current-v2/v1",
    "candidate_visual_plan",
    "1.0",
)
_SERVER_CONFIGURES = {
    variant: ArtifactReference(
        f"candidate_gate8_8704_configure/luanti/8704/{variant}/v1",
        "candidate_gate8_8704_configure",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_CLIENT_CONFIGURES = {
    variant: ArtifactReference(
        f"candidate_gate8_8704_client_configure_skeleton/luanti/8704/{variant}/v1",
        "candidate_gate8_8704_client_configure_skeleton",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_BUILD_PLAN = ArtifactReference(
    "candidate_gate8_8704_build_plan/luanti/8704/server-v2",
    "candidate_gate8_8704_build_plan",
    "2.0",
)
_SERVER_BUILDS = {
    variant: ArtifactReference(
        f"candidate_gate8_8704_native_build/luanti/8704/{variant}/v2",
        "candidate_gate8_8704_native_build",
        "2.0",
    )
    for variant in ("fixed", "buggy")
}
_BUILD_AGGREGATE = ArtifactReference(
    "candidate_materialization/luanti/8704/fixed-buggy/v4",
    "candidate_materialization",
    "4.0",
)
_START = ArtifactReference(
    "candidate_runtime_start/luanti/8704/fixed-buggy/v3",
    "candidate_runtime_start",
    "3.0",
)
_REPLAY = ArtifactReference(
    "candidate_replay/luanti/8704/ordered-v3",
    "candidate_replay",
    "3.0",
)
_ORACLE = ArtifactReference(
    "candidate_oracle/luanti/8704/hidden-v3",
    "candidate_oracle",
    "3.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate10_transition/luanti/8704/runtime-v3",
    "candidate_gate10_transition",
    "3.0",
)
_GATE11_BLOCKER = ArtifactReference(
    "candidate_gate11_visual_blocker/luanti/8704/current-v2/v1",
    "candidate_gate11_visual_blocker",
    "1.0",
)
_STATIC = (_READINESS, _DEPENDENCY, _SOURCE_PAIR, _MAPPING, _VISUAL_PLAN)

_OBSERVER_INIT = r'''minetest.log("action", "C1_LAZARUS_8704_START state=common_before")
local input = {
  method = "normal",
  width = 2,
  items = {"", "default:coal_lump", "", "default:stick"},
}
local result = minetest.get_craft_result(input)
local item = result.item
local name = item and item:get_name() or ""
local count = item and item:get_count() or 0
local empty = name == "" or count == 0
local fuel = minetest.get_craft_result({
  method = "fuel", width = 1, items = {"default:coal_lump"},
})
local cooking = minetest.get_craft_result({
  method = "cooking", width = 1, items = {"default:cobble"},
})
local fuel_ok = fuel.time and fuel.time > 0
local cooking_ok = cooking.time and cooking.time > 0 and cooking.item and
  cooking.item:get_name() == "default:stone"
minetest.log("action", "C1_LAZARUS_8704_OBSERVATION item=" .. name ..
  " count=" .. tostring(count) .. " empty=" .. tostring(empty) ..
  " fuel=" .. tostring(fuel_ok) .. " cooking=" .. tostring(cooking_ok))
minetest.after(0.2, function()
  minetest.request_shutdown("C1 Lazarus 8704 observation complete", false, 0)
end)
'''
_OBSERVATION_RE = re.compile(
    rb"C1_LAZARUS_8704_OBSERVATION item=([^ ]*) count=([0-9]+) "
    rb"empty=(true|false) fuel=(true|false) cooking=(true|false)"
)


def register_luanti_8704_execution_skeleton(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Register client configure skeletons and the deferred build plan."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    static = _load_static(root)
    client = tuple(
        _register_client_configure(root, variant, static, authorization)
        for variant in ("fixed", "buggy")
    )
    plan = _register_build_plan(root, static, client, authorization)
    return client[0], client[1], plan


def execute_luanti_8704_server_build(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = BUILD_CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """After lane release, build fixed then buggy with one ``-j1`` lane."""

    _require_auth(authorization)
    if confirmation != BUILD_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {BUILD_CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    static = _load_static(root)
    plan = _load(root, _BUILD_PLAN)
    builds = tuple(
        _build_variant(root, variant, static, plan, authorization)
        for variant in ("fixed", "buggy")
    )
    if any(item.payload.get("state") != "built" for item in builds):
        raise RuntimeError("#8704 server build did not complete")
    aggregate = _register_build_aggregate(root, static, plan, builds, authorization)
    return builds[0], builds[1], aggregate


def execute_luanti_8704_runtime_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = RUNTIME_CONFIRMATION,
) -> tuple[StableArtifact, ...]:
    """Run fresh fixed/buggy semantic observations and the hidden oracle."""

    _require_auth(authorization)
    if confirmation != RUNTIME_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {RUNTIME_CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    static = _load_static(root)
    aggregate = _load(root, _BUILD_AGGREGATE)
    run_root = root / _RUN
    home_root = root / _HOME
    if run_root.exists() or run_root.is_symlink() or home_root.exists() or home_root.is_symlink():
        raise RuntimeError("#8704 Gate9 runtime namespace is not fresh")
    observations = tuple(
        _run_observation(root, variant, static, aggregate, authorization)
        for variant in ("fixed", "buggy")
    )
    start = _register_start(root, static, aggregate, observations, authorization)
    replay = _register_replay(root, static, aggregate, observations, start, authorization)
    oracle = _register_oracle(
        root, static, aggregate, observations, start, replay, authorization
    )
    transition = _register_transition(
        root, static, aggregate, start, replay, oracle, authorization
    )
    return (*observations, start, replay, oracle, transition)


def register_luanti_8704_gate11_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Record the exact client/F12 gap after Gates 9 and 10 pass."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    static = _load_static(root)
    transition = _load(root, _TRANSITION)
    client = tuple(_load(root, _CLIENT_CONFIGURES[variant]) for variant in ("fixed", "buggy"))
    if transition.payload.get("state") != "passed":
        raise RuntimeError("#8704 Gate10 transition is not passed")
    paths = [root / _PAIR / variant / "bin/minetest" for variant in ("fixed", "buggy")]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise RuntimeError("#8704 client state changed; blocker requires re-audit")
    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "reason": "Gates 9 and 10 pass, but candidate-owned Irrlicht 1.8.4 client library and fixed/buggy real clients are not built",
        "passed_gates": [8, 9, 10],
        "client_configure_skeletons": [item.reference.artifact_id for item in client],
        "visual_plan": static[4].reference.artifact_id,
        "missing_required_evidence": [
            "candidate_owned_irrlicht_1_8_4_client_library",
            "candidate_owned_fixed_and_buggy_real_clients",
            "real_common_before_fixed_after_buggy_after_frames",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
        ],
        "new_client_build_started": False,
        "does_not_invalidate_gate_1_7": True,
        "does_not_invalidate_gate_8_10": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "after the native lane is free, build Irrlicht 1.8.4 and both clients sequentially, then execute the registered real-F12 plan",
    }
    inputs = tuple(item.reference for item in static) + (
        transition.reference,
        *(item.reference for item in client),
    )
    return _write_once(root, _GATE11_BLOCKER, inputs, payload, authorization)


def _load_static(root: Path) -> tuple[StableArtifact, ...]:
    values = tuple(_load(root, reference) for reference in _STATIC)
    readiness, dependency, pair, mapping, visual = values
    if (
        readiness.payload.get("candidate") != _CANDIDATE
        or readiness.payload.get("state") != "configured_mapped_waiting_native_lane"
        or readiness.payload.get("terminal") is not False
    ):
        raise RuntimeError("#8704 readiness drifted")
    if dependency.payload.get("candidate_owned") is not True:
        raise RuntimeError("#8704 dependency drifted")
    if pair.payload.get("only_different_paths") != ["src/craftdef.cpp", "src/craftdef.h"]:
        raise RuntimeError("#8704 source pair drifted")
    if mapping.payload.get("state") != "mapping_registered_fixture_frozen":
        raise RuntimeError("#8704 mapping drifted")
    if visual.payload.get("state") != "planned":
        raise RuntimeError("#8704 visual plan drifted")
    return values


def _register_client_configure(
    root: Path,
    variant: str,
    static: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    build = _directory(root / _CLIENT_CONFIGURE / variant, root)
    cache_path = _file(build / "CMakeCache.txt", root)
    ninja_path = _file(build / "build.ninja", root)
    cache = cache_path.read_text(encoding="utf-8", errors="replace")
    ninja = ninja_path.read_text(encoding="utf-8", errors="replace")
    placeholder = (root / _DEP / "link-placeholder/libIrrlicht.a").as_posix()
    required = (
        "BUILD_CLIENT:BOOL=TRUE",
        "BUILD_SERVER:BOOL=FALSE",
        f"IRRLICHT_LIBRARY:FILEPATH={placeholder}",
        (root / _DEP / "source/include").as_posix(),
    )
    if any(marker not in cache + ninja for marker in required):
        raise RuntimeError(f"#8704 {variant} client configure skeleton drifted")
    target = subprocess.run(
        (str(root / _TOOLCHAIN / "bin/ninja"), "-C", str(build), "-n", "minetest"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    steps = sum(1 for line in target.stdout.splitlines() if _step(line))
    if target.returncode != 0 or steps <= 0:
        raise RuntimeError(f"#8704 {variant} client target dry-run failed")
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "configured_skeleton",
        "outcome": "client_target_configured_real_library_build_pending",
        "complete": True,
        "build_tree_path": f"{_CLIENT_CONFIGURE.as_posix()}/{variant}",
        "target": "minetest",
        "planned_steps": steps,
        "candidate_owned_headers": f"{_DEP.as_posix()}/source/include",
        "temporary_server_link_placeholder": f"{_DEP.as_posix()}/link-placeholder/libIrrlicht.a",
        "real_candidate_owned_client_library": "pending_native_lane",
        "final_client_reconfigure_required": True,
        "build_started": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _CLIENT_CONFIGURES[variant],
        tuple(item.reference for item in static),
        payload,
        authorization,
    )


def _register_build_plan(
    root: Path,
    static: tuple[StableArtifact, ...],
    client: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "ready_not_started",
        "outcome": "candidate_bound_build_receipt_skeleton_registered",
        "complete": True,
        "ordered_variants": ["fixed", "buggy"],
        "build_parallelism": 1,
        "target": "minetestserver",
        "planned_receipts": [
            _SERVER_BUILDS[variant].artifact_id for variant in ("fixed", "buggy")
        ],
        "planned_aggregate": _BUILD_AGGREGATE.artifact_id,
        "fresh_runtime_function": "execute_luanti_8704_runtime_closure",
        "fresh_runtime_confirmation": RUNTIME_CONFIRMATION,
        "client_configure_skeletons": [item.reference.artifact_id for item in client],
        "native_lane_required": True,
        "build_started": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + tuple(
        item.reference for item in client
    )
    return _write_once(root, _BUILD_PLAN, inputs, payload, authorization)


def _build_variant(
    root: Path,
    variant: str,
    static: tuple[StableArtifact, ...],
    plan: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, _SERVER_BUILDS[variant])
    if existing is not None:
        return existing
    build = _directory(root / _SERVER_BUILD / variant, root)
    tree = _directory(root / _PAIR / variant, root)
    ninja = _file(root / _TOOLCHAIN / "bin/ninja", root)
    argv = (str(ninja), "-C", str(build), "-j1", "minetestserver")
    process = subprocess.run(
        argv,
        cwd=tree,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=1800,
        env={**os.environ, "LANG": "C", "LC_ALL": "C"},
    )
    binary = tree / "bin/minetestserver"
    version = (
        subprocess.run(
            (str(binary), "--version"),
            cwd=tree,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        if process.returncode == 0 and binary.is_file()
        else None
    )
    pagezero = _pagezero(binary) if binary.is_file() else None
    passed = (
        process.returncode == 0
        and binary.is_file()
        and os.access(binary, os.X_OK)
        and pagezero == 0x100000000
        and version is not None
        and version.returncode == 0
    )
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "built" if passed else "blocked",
        "outcome": "completed" if passed else "build_failed",
        "complete": passed,
        "argv": list(argv),
        "returncode": process.returncode,
        "build_parallelism": 1,
        "binary_path": binary.relative_to(root).as_posix() if passed else None,
        "binary_fingerprint": _fingerprint(binary) if passed else None,
        "pagezero_vmsize": pagezero,
        "version_returncode": version.returncode if version is not None else None,
        "version_stdout": (
            version.stdout.decode("utf-8", errors="replace") if version is not None else ""
        ),
        "stdout_base64": base64.b64encode(process.stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(process.stderr[:_MAX_OUTPUT]).decode("ascii"),
        "stdout_truncated": len(process.stdout) > _MAX_OUTPUT,
        "stderr_truncated": len(process.stderr) > _MAX_OUTPUT,
        "candidate_owned_dependency": static[1].reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
    }
    inputs = tuple(item.reference for item in static) + (plan.reference,)
    return _write_once(root, _SERVER_BUILDS[variant], inputs, payload, authorization)


def _register_build_aggregate(
    root: Path,
    static: tuple[StableArtifact, ...],
    plan: StableArtifact,
    builds: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_fixed_buggy_server_pair_built",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "build_receipts": [item.reference.artifact_id for item in builds],
        "candidate_owned_dependency": static[1].reference.artifact_id,
        "production_only_difference": ["src/craftdef.cpp", "src/craftdef.h"],
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (
        plan.reference,
        *(item.reference for item in builds),
    )
    return _write_once(root, _BUILD_AGGREGATE, inputs, payload, authorization)


def _run_observation(
    root: Path,
    variant: str,
    static: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = _directory(root / _PAIR / variant, root)
    binary = _executable(tree / "bin/minetestserver", root)
    target = root / _RUN / variant
    game = target / "games/c1_lazarus_8704"
    (game / "mods").mkdir(parents=True, mode=0o700)
    default_source = _directory(
        root / _PAIR / "fixed/games/minimal/mods/default", root
    )
    shutil.copytree(default_source, game / "mods/default", symlinks=False)
    observer = game / "mods/c1_observer"
    observer.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text(
        "name = c1_lazarus_8704\ntitle = C1 Lazarus 8704 Replay\n",
        encoding="utf-8",
    )
    (observer / "mod.conf").write_text(
        "name = c1_observer\ndepends = default\n", encoding="utf-8"
    )
    (observer / "init.lua").write_text(_OBSERVER_INIT, encoding="utf-8")
    world = target / "world"
    home = root / _HOME / variant
    world.mkdir(parents=True, mode=0o700)
    home.mkdir(parents=True, mode=0o700)
    (world / "world.mt").write_text(
        "gameid = c1_lazarus_8704\nbackend = dummy\nplayer_backend = dummy\nauth_backend = dummy\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\nenable_ipv6 = false\nbind_address = 127.0.0.1\n",
        encoding="utf-8",
    )
    argv = (
        str(binary),
        "--world",
        str(world),
        "--gameid",
        "c1_lazarus_8704",
        "--config",
        str(config),
        "--logfile",
        "",
    )
    receipt = run_bounded_process(
        argv,
        cwd=tree,
        env={
            **os.environ,
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "MINETEST_SUBGAME_PATH": str(target / "games"),
        },
        timeout_seconds=120,
    )
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in _OBSERVATION_RE.finditer(stream)
    ]
    semantic = None
    if len(matches) == 1:
        match = matches[0]
        semantic = {
            "item_name": match.group(1).decode("utf-8"),
            "item_count": int(match.group(2)),
            "result_empty": match.group(3) == b"true",
            "fuel_ok": match.group(4) == b"true",
            "cooking_ok": match.group(5) == b"true",
        }
    reference = ArtifactReference(
        f"candidate_runtime_observation/luanti/8704/{variant}/v3",
        "candidate_runtime_observation",
        "3.0",
    )
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "fresh_epoch": "luanti-8704-gate9-v3",
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "selected_line_count": len(matches),
        "semantic_observation": semantic,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "oracle_expectation_visible_during_execution": False,
        "complete": receipt.outcome == "completed" and len(matches) == 1,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (aggregate.reference,)
    return _write_once(root, reference, inputs, payload, authorization)


def _register_start(
    root: Path,
    static: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    observations: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = all(item.payload.get("complete") is True for item in observations)
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if passed else "blocked",
        "outcome": "registered_start_completed" if passed else "registered_start_incomplete",
        "complete": passed,
        "variant_observations": [item.reference.artifact_id for item in observations],
        "fresh_process_per_variant": True,
        "no_timeout_or_unknown": passed,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (aggregate.reference,) + tuple(
        item.reference for item in observations
    )
    return _write_once(root, _START, inputs, payload, authorization)


def _register_replay(
    root: Path,
    static: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    observations: tuple[StableArtifact, ...],
    start: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = start.payload.get("state") == "passed"
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if passed else "blocked",
        "outcome": "ordered_replay_completed" if passed else "ordered_replay_incomplete",
        "complete": passed,
        "mapping_order": static[3].payload["ordered_replay"],
        "variant_order": ["fixed", "buggy"],
        "observation_references": [item.reference.artifact_id for item in observations],
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (
        aggregate.reference,
        start.reference,
        *(item.reference for item in observations),
    )
    return _write_once(root, _REPLAY, inputs, payload, authorization)


def _register_oracle(
    root: Path,
    static: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    observations: tuple[StableArtifact, ...],
    start: StableArtifact,
    replay: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    oracle = _json(_file(root / _FIXTURE / "oracle.json", root))
    observed_full = {
        str(item.payload["variant"]): item.payload["semantic_observation"]
        for item in observations
    }
    observed = {
        variant: {
            "item_name": value["item_name"],
            "item_count": value["item_count"],
            "result_empty": value["result_empty"],
        }
        for variant, value in observed_full.items()
        if isinstance(value, dict)
    }
    expected = {
        "fixed": oracle["fixed_expected"],
        "buggy": oracle["buggy_expected"],
    }
    matched = observed == expected
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if matched else "blocked",
        "outcome": "hidden_semantic_oracle_passed" if matched else "hidden_semantic_oracle_failed",
        "complete": matched,
        "oracle_loaded_after_observations": True,
        "expected": expected,
        "observed": observed,
        "supplemental_control_observations": {
            variant: {
                "fuel_ok": value["fuel_ok"],
                "cooking_ok": value["cooking_ok"],
            }
            for variant, value in observed_full.items()
            if isinstance(value, dict)
        },
        "semantic_goal_observed": observed.get("fixed") == expected["fixed"],
        "semantic_defect_observed": observed.get("buggy") == expected["buggy"],
        "control_calls_observed_without_timeout": all(
            isinstance(value, dict)
            and isinstance(value.get("fuel_ok"), bool)
            and isinstance(value.get("cooking_ok"), bool)
            for value in observed_full.values()
        ),
        "no_timeout_or_unknown": all(item.payload.get("complete") is True for item in observations),
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (
        aggregate.reference,
        start.reference,
        replay.reference,
        *(item.reference for item in observations),
    )
    return _write_once(root, _ORACLE, inputs, payload, authorization)


def _register_transition(
    root: Path,
    static: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    start: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = all(item.payload.get("state") == "passed" for item in (start, replay, oracle))
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if passed else "blocked",
        "outcome": "gate9_gate10_passed" if passed else "gate9_gate10_incomplete",
        "complete": passed,
        "terminal": False,
        "resumable": True,
        "gate_status": {
            "gate_1_7": "qualified",
            "gate8_candidate_owned_server_pair": "passed",
            "gate9_registered_start_replay": "passed" if passed else "blocked",
            "gate10_hidden_oracle": "passed" if passed else "blocked",
            "gate11_to_13": "planned",
        },
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in static) + (
        aggregate.reference,
        start.reference,
        replay.reference,
        oracle.reference,
    )
    return _write_once(root, _TRANSITION, inputs, payload, authorization)


def _require_auth(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#8704 execution authorization is inactive")


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
        raise RuntimeError(f"#8704 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#8704 typed reload failed: {reference.artifact_id}")
    return result


def _directory(path: Path, boundary: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(boundary)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe directory: {path}")
    return path


def _file(path: Path, boundary: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(boundary)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"unsafe file: {path}")
    return path


def _executable(path: Path, boundary: Path) -> Path:
    result = _file(path, boundary)
    if not os.access(result, os.X_OK):
        raise RuntimeError(f"file is not executable: {path}")
    return result


def _fingerprint(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _pagezero(path: Path) -> int:
    raw = path.read_bytes()
    if raw[:4] != b"\xcf\xfa\xed\xfe":
        raise RuntimeError("#8704 executable is not arm64 Mach-O64")
    command_count = struct.unpack_from("<I", raw, 16)[0]
    offset = 32
    for _ in range(command_count):
        command, size = struct.unpack_from("<II", raw, offset)
        if command == 0x19:
            segment = struct.unpack_from("<16s", raw, offset + 8)[0].split(b"\0", 1)[0]
            if segment == b"__PAGEZERO":
                return int(struct.unpack_from("<Q", raw, offset + 32)[0])
        offset += size
    raise RuntimeError("#8704 executable lacks __PAGEZERO")


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError(f"JSON fixture is not an object: {path}")
    return value


def _step(line: bytes) -> bool:
    return line.startswith(b"[") and b"/" in line.split(b"]", 1)[0]


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = [
    "BUILD_CONFIRMATION",
    "RUNTIME_CONFIRMATION",
    "execute_luanti_8704_runtime_closure",
    "execute_luanti_8704_server_build",
    "register_luanti_8704_gate11_blocker",
    "register_luanti_8704_execution_skeleton",
]
