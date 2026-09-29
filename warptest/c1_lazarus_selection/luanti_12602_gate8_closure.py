"""Candidate-owned Gate-8 source/build closure for Luanti #12602.

The earlier Gate 5--7 server pair was compiled with IrrlichtMt headers copied
from candidate #14464.  This module replaces that downstream-only provenance
gap with the exact 1.9.0mt6 tag pinned by #12602's own source tree.  It never
reclassifies Gate 1--7 and it does not run paid actions or formal trials.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 12602}
_FIXED_COMMIT = "5892fcf27837480143353d7fa156d9a9fbfa8c74"
_BUGGY_PRODUCTION_COMMIT = "e8ab538e6b6861c50b006b775b4792b088e05bd0^"
_IRR_TAG = "1.9.0mt6"
_IRR_COMMIT = "128cf1696c2803e12ebbdd3ee034e0c9eea90fae"
_IRR_REMOTE = "https://github.com/minetest/irrlicht.git"
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_PAIR = PurePosixPath("private/materialized/luanti/12602/application-v1")
_IRR = PurePosixPath("private/dependencies/luanti-12602-irrlicht/1.9.0mt6/source")
_BUILD = PurePosixPath("private/bounded-builds/luanti/12602/gate8-mt6-v1")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_FIXTURE = PurePosixPath("private/downstream-fixtures/luanti/12602/media-override-v1")
_BINARY = PurePosixPath("bin/minetestserver")
_MAX_OUTPUT = 262_144

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12602/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12602/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12602/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12602/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/12602/fixed/v2", "fixed_source_capsule", "2.0"
)
_GATE57_AGGREGATE = ArtifactReference(
    "candidate_gate57_12602_selector_reparse_aggregate/luanti/12602/native/v9",
    "candidate_gate57_12602_selector_reparse_aggregate",
    "1.0",
)
_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_12602_selector_reparse_transition/luanti/12602/native/v9",
    "candidate_gate57_12602_selector_reparse_transition",
    "1.0",
)
_TOOLCHAIN_RECEIPT = ArtifactReference(
    "candidate_gate57_12602_cxx17_toolchain/luanti/12602/native/v9",
    "candidate_gate57_12602_cxx17_toolchain",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_12602_irrlichtmt6_source/luanti/12602/current-v2/v1",
    "candidate_gate8_12602_irrlichtmt6_source",
    "1.0",
)
_SOURCE_PAIR = ArtifactReference(
    "candidate_gate8_12602_source_pair/luanti/12602/production-only/v1",
    "candidate_gate8_12602_source_pair",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/12602/fixed-buggy/v3",
    "candidate_materialization",
    "3.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/12602/lz-luanti-12602-media-override/v1",
    "lazarus_task_mapping",
    "1.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/12602/current-v2/v1",
    "candidate_downstream_plan",
    "1.0",
)
_READINESS = ArtifactReference(
    "candidate_gate8_readiness/luanti/12602/current-v2/v1",
    "candidate_gate8_readiness",
    "1.0",
)
_CHAIN = (
    _SOURCE,
    _PATCH,
    _ATOMICITY,
    _MANIFEST,
    _CAPSULE,
    _GATE57_AGGREGATE,
    _GATE57_TRANSITION,
    _TOOLCHAIN_RECEIPT,
)


def register_luanti_12602_gate8_static_closure(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Register exact IrrlichtMt authority and the clean production-only pair."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    dependency = _register_dependency(root, chain, authorization)
    source_pair = _register_source_pair(root, chain, dependency, authorization)
    return dependency, source_pair


def execute_luanti_12602_gate8_build(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, ...]:
    """Build fixed then buggy with one ``ninja -j1`` lane and register Gate 8."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    dependency = _load(root, _DEPENDENCY)
    source_pair = _load(root, _SOURCE_PAIR)
    builds = tuple(
        _build_variant(root, variant, chain, dependency, source_pair, authorization)
        for variant in ("fixed", "buggy")
    )
    if any(item.payload.get("returncode") != 0 for item in builds):
        raise RuntimeError("#12602 candidate-owned Gate-8 build is incomplete")
    materialization = _register_materialization(
        root, chain, dependency, source_pair, builds, authorization
    )
    mapping = _register_mapping(
        root, chain, dependency, source_pair, builds, materialization, authorization
    )
    plan = _register_plan(
        root, chain, dependency, source_pair, builds, materialization, mapping, authorization
    )
    readiness = _register_readiness(
        root,
        chain,
        dependency,
        source_pair,
        builds,
        materialization,
        mapping,
        plan,
        authorization,
    )
    return builds[0], builds[1], materialization, mapping, plan, readiness


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    artifacts = tuple(_load(root, ref) for ref in _CHAIN)
    source, patch, atomicity, manifest, capsule, aggregate, transition, toolchain = artifacts
    if source.payload.get("candidate") != _CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#12602 source packet drifted")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != ["src/server/mods.cpp"]
        or patch.payload.get("test_paths") != ["src/unittest/test_servermodmanager.cpp"]
    ):
        raise RuntimeError("#12602 patch receipt drifted")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 12557
    ):
        raise RuntimeError("#12602 atomicity receipt drifted")
    if manifest.payload.get("state") != "passed" or manifest.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#12602 test manifest drifted")
    if (
        capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("source_revision") != _FIXED_COMMIT
        or capsule.payload.get("complete") is not True
    ):
        raise RuntimeError("#12602 fixed capsule drifted")
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("no_timeout_or_unknown") is not True
    ):
        raise RuntimeError("#12602 Gate 5--7 aggregate drifted")
    if (
        transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("qualified") is not True
        or transition.payload.get("terminal") is not False
    ):
        raise RuntimeError("#12602 Gate 5--7 transition drifted")
    if toolchain.payload.get("candidate") != _CANDIDATE or toolchain.payload.get("source_preserving") is not True:
        raise RuntimeError("#12602 toolchain receipt drifted")
    return artifacts


def _register_dependency(
    root: Path,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    pair_fixed = _directory(root / _PAIR / "fixed", root)
    source = _directory(root / _IRR, root)
    tag_evidence = {
        "macos": "IRRLICHT_TAG: 1.9.0mt6",
        "windows": "irrlicht_version=1.9.0mt6",
        "linux_ci": "releases/download/1.9.0mt6/ubuntu-bionic.tar.gz",
    }
    evidence_paths = {
        "macos": pair_fixed / ".github/workflows/macos.yml",
        "windows": pair_fixed / "util/buildbot/buildwin64.sh",
        "linux_ci": pair_fixed / "util/ci/common.sh",
    }
    for name, marker in tag_evidence.items():
        if marker not in _file(evidence_paths[name], root).read_text(encoding="utf-8"):
            raise RuntimeError(f"#12602 candidate Irrlicht pin drifted: {name}")
    head = _git(source, "rev-parse", "HEAD")
    tag = _git(source, "rev-parse", f"refs/tags/{_IRR_TAG}^{{}}")
    remote = _git(source, "remote", "get-url", "origin")
    if head != _IRR_COMMIT or tag != _IRR_COMMIT or remote != _IRR_REMOTE:
        raise RuntimeError("#12602 Irrlicht authority is invalid")
    if _git(source, "status", "--porcelain"):
        raise RuntimeError("#12602 Irrlicht source is dirty")
    inventory, fingerprint = _git_inventory(source)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "dependency_family": "IrrlichtMt",
        "authority_remote": remote,
        "authority_tag": _IRR_TAG,
        "authority_commit": head,
        "authority_tree": _git(source, "rev-parse", "HEAD^{tree}"),
        "source_path": _IRR.as_posix(),
        "tracked_file_count": inventory,
        "tracked_inventory_fingerprint": fingerprint,
        "candidate_source_pin_evidence": {
            key: path.relative_to(root).as_posix() for key, path in evidence_paths.items()
        },
        "candidate_owned": True,
        "source_from_other_candidate": False,
        "network_action": "read_only_authoritative_git_fetch",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(root, _DEPENDENCY, tuple(item.reference for item in chain), payload, authorization)


def _register_source_pair(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    mirror = _directory(root / _MIRROR, root)
    fixed = _directory(root / _PAIR / "fixed", root)
    buggy = _directory(root / _PAIR / "buggy", root)
    entries = _git(mirror, "ls-tree", "-r", _FIXED_COMMIT).splitlines()
    differences: list[str] = []
    fixed_digest = hashlib.sha256()
    buggy_digest = hashlib.sha256()
    for entry in entries:
        meta, relative = entry.split("\t", 1)
        mode = meta.split(" ", 1)[0]
        fixed_bytes = _tree_bytes(fixed / PurePosixPath(relative), mode, root)
        buggy_bytes = _tree_bytes(buggy / PurePosixPath(relative), mode, root)
        for digest, content in ((fixed_digest, fixed_bytes), (buggy_digest, buggy_bytes)):
            digest.update(relative.encode("utf-8") + b"\0" + mode.encode("ascii") + b"\0")
            digest.update(hashlib.sha256(content).digest())
        if fixed_bytes != buggy_bytes:
            differences.append(relative)
    if differences != ["src/server/mods.cpp"]:
        raise RuntimeError("#12602 source pair is not production-only")
    fixed_production = _git_bytes(mirror, "show", f"{_FIXED_COMMIT}:src/server/mods.cpp")
    buggy_production = _git_bytes(mirror, "show", f"{_BUGGY_PRODUCTION_COMMIT}:src/server/mods.cpp")
    fixed_test = _git_bytes(mirror, "show", f"{_FIXED_COMMIT}:src/unittest/test_servermodmanager.cpp")
    if _file(fixed / "src/server/mods.cpp", root).read_bytes() != fixed_production:
        raise RuntimeError("#12602 fixed production source is not authoritative")
    if _file(buggy / "src/server/mods.cpp", root).read_bytes() != buggy_production:
        raise RuntimeError("#12602 buggy production source is not authoritative")
    if any(
        _file(tree / "src/unittest/test_servermodmanager.cpp", root).read_bytes() != fixed_test
        for tree in (fixed, buggy)
    ):
        raise RuntimeError("#12602 fixed test bytes were not preserved")
    payload = {
        "candidate": _CANDIDATE,
        "state": "materialized",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "tracked_file_count": len(entries),
        "only_different_paths": differences,
        "fixed_inventory_fingerprint": fixed_digest.hexdigest(),
        "buggy_inventory_fingerprint": buggy_digest.hexdigest(),
        "fixed_production_fingerprint": hashlib.sha256(fixed_production).hexdigest(),
        "buggy_production_fingerprint": hashlib.sha256(buggy_production).hexdigest(),
        "fixed_test_support_bytes_preserved": True,
        "inverse_scope": "production_only",
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "other_candidate_dependency_reused": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (dependency.reference,)
    return _write_once(root, _SOURCE_PAIR, inputs, payload, authorization)


def _build_variant(
    root: Path,
    variant: str,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = ArtifactReference(
        f"candidate_gate8_12602_native_build/luanti/12602/{variant}/mt6-v1",
        "candidate_gate8_12602_native_build",
        "1.0",
    )
    existing = _try_load(root, reference)
    if existing is not None:
        return existing
    build = _directory(root / _BUILD / variant, root)
    source = _directory(root / _PAIR / variant, root)
    cache = _file(build / "CMakeCache.txt", root).read_text(encoding="utf-8", errors="replace")
    required = {
        "BUILD_CLIENT:BOOL=FALSE",
        "BUILD_SERVER:BOOL=TRUE",
        "BUILD_UNITTESTS:BOOL=TRUE",
        f"IRRLICHT_INCLUDE_DIR:PATH={(root / _IRR / 'include').as_posix()}",
    }
    if not required.issubset(set(cache.splitlines())):
        raise RuntimeError(f"#12602 {variant} closed configure cache drifted")
    ninja = _file(root / _TOOLCHAIN / "bin/ninja", root)
    argv = (str(ninja), "-C", str(build), "-j1", "minetestserver")
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(ninja.parent) + os.pathsep + env.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    process = subprocess.run(
        argv,
        cwd=source,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1800,
        check=False,
        env=env,
    )
    binary = source / _BINARY
    binary_record = None
    if process.returncode == 0:
        binary_record = _file_record(_executable(binary, root), root)
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "built" if process.returncode == 0 else "blocked",
        "outcome": "completed" if process.returncode == 0 else "build_failed",
        "complete": process.returncode == 0,
        "argv": list(argv),
        "returncode": process.returncode,
        "build_parallelism": 1,
        "sequential_variant_order": ["fixed", "buggy"],
        "source_tree_path": source.relative_to(root).as_posix(),
        "build_tree_path": build.relative_to(root).as_posix(),
        "binary": binary_record,
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "stdout_base64": base64.b64encode(process.stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(process.stderr[:_MAX_OUTPUT]).decode("ascii"),
        "stdout_truncated": len(process.stdout) > _MAX_OUTPUT,
        "stderr_truncated": len(process.stderr) > _MAX_OUTPUT,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
    }
    inputs = tuple(item.reference for item in chain) + (dependency.reference, source_pair.reference)
    return _write_once(root, reference, inputs, payload, authorization)


def _register_materialization(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    builds: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "gate8_status": "passed_complete_fixed_buggy_materialization",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "fixed_binary_path": f"{_PAIR.as_posix()}/fixed/{_BINARY.as_posix()}",
        "buggy_binary_path": f"{_PAIR.as_posix()}/buggy/{_BINARY.as_posix()}",
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "build_receipts": [item.reference.artifact_id for item in builds],
        "production_only_difference": "src/server/mods.cpp",
        "fixed_test_support_bytes_preserved": True,
        "other_candidate_dependency_reused": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in builds),
    )
    return _write_once(root, _MATERIALIZATION, inputs, payload, authorization)


def _register_mapping(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    builds: tuple[StableArtifact, ...],
    materialization: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    fixture = _directory(root / _FIXTURE, root)
    names = ("start.json", "replay.json", "oracle.json", "golden-contract.json")
    records = {name: _file_record(_file(fixture / name, root), root) for name in names}
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "profile_id": "luanti-12602-media-override-v1",
        "issue_number": 12557,
        "defect_locus": "at_goal",
        "state": "mapping_registered_fixture_frozen",
        "complete": True,
        "fixture_root": _FIXTURE.as_posix(),
        "fixture_files": list(names),
        "fixture_records": records,
        "registered_start": {
            "kind": "native_unit_test_start",
            "binary_relative": _BINARY.as_posix(),
            "argv": ["--run-unittests"],
            "isolated_home_root": "private/runtime-home/luanti-12602-gate9-v1",
            "process_bound_seconds": 300,
        },
        "ordered_replay": [
            {"type": "restore_registered_start"},
            {"type": "launch_fresh_process", "phase": "f2p"},
            {
                "type": "observe_unique_named_test_line",
                "selector": "TestServerModManager.testGetModMediaPaths",
            },
            {"type": "launch_fresh_process", "phase": "p2p"},
            {
                "type": "observe_unique_named_test_line",
                "selector": "TestServerModManager.testGetModNames",
            },
        ],
        "semantic_oracle": {
            "goal": "later-loaded mod media directories precede earlier-loaded mod media directories",
            "defect": "buggy media paths use load order and break override priority",
            "invariants": [
                "fixed and buggy preserve identical test bytes",
                "testGetModNames passes in both variants",
                "no process timeout or unknown result is admissible",
            ],
        },
        "golden_contract": {
            "common_before_fixed_after_buggy_after": True,
            "pairwise_before_visual_equivalence": True,
            "two_by_two_after_cross_matrix": True,
            "same_lineage_required": True,
            "raw_semantic_observations_required": True,
        },
        "candidate_materialization": materialization.reference.artifact_id,
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in builds),
        materialization.reference,
    )
    return _write_once(root, _MAPPING, inputs, payload, authorization)


def _register_plan(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    builds: tuple[StableArtifact, ...],
    materialization: StableArtifact,
    mapping: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "planned",
        "outcome": "gate8_complete_gate9_to_13_pending",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "gate_status": {
            "gate_1_7": "qualified",
            "gate8_materialization": "passed_candidate_owned_provenance",
            "gate8_mapping_fixture": "passed",
            "gate9_registered_start_and_ordered_replay": "pending",
            "gate10_hidden_oracle": "pending",
            "gate11_goldens": "pending",
            "gate12_visual_cross_judgment": "pending",
            "gate13_same_lineage_and_freeze": "pending",
        },
        "execution_order": [
            "registered_start",
            "ordered_replay_fixed_and_buggy",
            "hidden_semantic_oracle",
            "common_before_fixed_after_buggy_after",
            "visual_cross_judgment",
            "same_lineage_construction",
            "formal_admission",
        ],
        "mapping": mapping.reference.artifact_id,
        "fixture_root": _FIXTURE.as_posix(),
        "materialization": materialization.reference.artifact_id,
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in builds),
        materialization.reference,
        mapping.reference,
    )
    return _write_once(root, _PLAN, inputs, payload, authorization)


def _register_readiness(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    builds: tuple[StableArtifact, ...],
    materialization: StableArtifact,
    mapping: StableArtifact,
    plan: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    fixture_present = (root / _FIXTURE).is_dir()
    missing = [
        "candidate_bound_registered_start_launch_receipt",
        "candidate_bound_ordered_replay_with_actions_and_waits",
        "candidate_bound_hidden_oracle",
        "semantic_goal_defect_and_invariant_observations",
        "common_before_fixed_after_and_buggy_after_goldens",
        "pairwise_before_visual_equivalence",
        "two_by_two_after_visual_cross_matrix",
        "same_lineage_construction_receipt",
    ]
    payload = {
        "candidate": _CANDIDATE,
        "state": "gate8_passed_downstream_pending",
        "outcome": "ready_for_registered_start_replay_oracle",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_source_capsule": "passed",
            "gate8_fixed_buggy_pair": "passed_candidate_owned_provenance",
            "gate8_mapping_fixture": "passed",
            "gate8_downstream_plan": "passed",
            "gate9_start_replay": "blocked_missing",
            "gate10_oracle": "blocked_missing",
            "gate11_to_13": "pending",
        },
        "candidate_owned_runtime_closure": {
            "admissible_candidate_owned_runtime_closure": True,
            "dependency": dependency.reference.artifact_id,
            "source_pair": source_pair.reference.artifact_id,
            "materialization": materialization.reference.artifact_id,
            "build_receipts": [item.reference.artifact_id for item in builds],
        },
        "mapping_fixture_inventory": {
            "candidate_bound_lazarus_task_mapping": True,
            "candidate_bound_downstream_fixture": fixture_present,
            "candidate_bound_downstream_plan": True,
            "mapping": mapping.reference.artifact_id,
            "plan": plan.reference.artifact_id,
        },
        "missing_required_evidence": missing,
        "prior_cross_candidate_irrlicht_blocker_superseded": True,
        "does_not_invalidate_gate_1_7": True,
        "new_gate57_execution_performed": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "execute bounded registered Start/replay/oracle from the frozen mapping and fixture",
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in builds),
        materialization.reference,
        mapping.reference,
        plan.reference,
    )
    return _write_once(root, _READINESS, inputs, payload, authorization)


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#12602 Gate-8 authorization is inactive")


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
        raise RuntimeError(f"#12602 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#12602 typed reload failed: {reference.artifact_id}")
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


def _tree_bytes(path: Path, mode: str, boundary: Path) -> bytes:
    path.resolve(strict=True).relative_to(boundary)
    info = path.lstat()
    if mode == "120000":
        if not stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"tracked symlink drifted: {path}")
        return os.readlink(path).encode("utf-8")
    return _file(path, boundary).read_bytes()


def _git(cwd: Path, *args: str) -> str:
    return _git_bytes(cwd, *args).decode("utf-8").strip()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    process = subprocess.run(
        ("git", "-C", str(cwd), *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace")[:500])
    return process.stdout


def _git_inventory(source: Path) -> tuple[int, str]:
    paths = [item.decode("utf-8") for item in _git_bytes(source, "ls-files", "-z").split(b"\0") if item]
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = source / PurePosixPath(relative)
        info = path.lstat()
        content = os.readlink(path).encode("utf-8") if stat.S_ISLNK(info.st_mode) else _file(path, source).read_bytes()
        digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return len(paths), digest.hexdigest()


def _file_record(path: Path, root: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(content),
        "fingerprint": hashlib.sha256(content).hexdigest(),
    }


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = [
    "execute_luanti_12602_gate8_build",
    "register_luanti_12602_gate8_static_closure",
]
