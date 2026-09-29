"""Candidate-owned static Gate-8 repair for Luanti #8704.

The qualified Gate 5--7 run used an Irrlicht receipt whose files were under
the #9722 namespace.  This module registers the exact Irrlicht 1.8.4 release
pinned by #8704, validates a clean production-only source pair, and records
two successful closed configures without starting a native build.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import zipfile

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 8704}
_FIXED_COMMIT = "fe6c666da26e08b726bd21f34d25558cf456bd1f"
_ARCHIVE_FINGERPRINT = "f42b280bc608e545b820206fe2a999c55f290de5c7509a02bdbeeccc1bf9e433"
_ARCHIVE_REMOTE = "https://downloads.sourceforge.net/irrlicht/irrlicht-1.8.4.zip"
_PAIR = PurePosixPath("private/materialized/luanti/8704/application-v1")
_DEP = PurePosixPath("private/dependencies/luanti-8704-irrlicht/1.8.4")
_BUILD = PurePosixPath("private/bounded-builds/luanti/8704/gate8-v1")
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/8704/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/8704/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/8704/preannotation/v2", "source_atomicity", "2.0"
)
_TRANSFORM = ArtifactReference(
    "candidate_patch_transform/luanti/8704/screening/v2",
    "candidate_patch_transform",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/8704/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/8704/fixed/v2", "fixed_source_capsule", "2.0"
)
_GATE57_AGGREGATE = ArtifactReference(
    "candidate_gate57_8704_aggregate/luanti/8704/native/v2",
    "candidate_gate57_8704_aggregate",
    "1.0",
)
_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_8704_transition/luanti/8704/native/v2",
    "candidate_gate57_8704_transition",
    "1.0",
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
_CONFIGURES = {
    variant: ArtifactReference(
        f"candidate_gate8_8704_configure/luanti/8704/{variant}/v1",
        "candidate_gate8_8704_configure",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_READINESS = ArtifactReference(
    "candidate_gate8_readiness/luanti/8704/current-v2/v1",
    "candidate_gate8_readiness",
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
_DOWNSTREAM_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/8704/current-v2/v1",
    "candidate_downstream_plan",
    "1.0",
)
_PROBES = {
    variant: ArtifactReference(
        f"candidate_gate8_8704_target_probe/luanti/8704/{variant}/v1",
        "candidate_gate8_8704_target_probe",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_READINESS_V2 = ArtifactReference(
    "candidate_gate8_readiness/luanti/8704/current-v2/v2",
    "candidate_gate8_readiness",
    "2.0",
)
_FIXTURE = PurePosixPath("private/downstream-fixtures/luanti/8704/minimal-craft-result-v1")
_CHAIN = (
    _SOURCE,
    _PATCH,
    _ATOMICITY,
    _TRANSFORM,
    _MANIFEST,
    _CAPSULE,
    _GATE57_AGGREGATE,
    _GATE57_TRANSITION,
)


def register_luanti_8704_gate8_static_repair(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, ...]:
    """Register the candidate-owned static closure and build-wait readiness."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    dependency = _register_dependency(root, chain, authorization)
    source_pair = _register_source_pair(root, chain, dependency, authorization)
    configures = tuple(
        _register_configure(root, variant, chain, dependency, source_pair, authorization)
        for variant in ("fixed", "buggy")
    )
    readiness = _register_readiness(
        root, chain, dependency, source_pair, configures, authorization
    )
    probes = tuple(
        _register_target_probe(
            root, variant, chain, dependency, source_pair, configures, authorization
        )
        for variant in ("fixed", "buggy")
    )
    mapping = _register_mapping(
        root, chain, dependency, source_pair, configures, probes, authorization
    )
    visual = _register_visual_plan(
        root, chain, dependency, source_pair, configures, probes, mapping, authorization
    )
    plan = _register_downstream_plan(
        root,
        chain,
        dependency,
        source_pair,
        configures,
        probes,
        mapping,
        visual,
        authorization,
    )
    readiness_v2 = _register_readiness_v2(
        root,
        chain,
        dependency,
        source_pair,
        configures,
        probes,
        mapping,
        visual,
        plan,
        authorization,
    )
    return (
        dependency,
        source_pair,
        *configures,
        readiness,
        *probes,
        mapping,
        visual,
        plan,
        readiness_v2,
    )


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    values = tuple(_load(root, reference) for reference in _CHAIN)
    source, patch, atomicity, transform, manifest, capsule, aggregate, transition = values
    if source.payload.get("candidate") != _CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#8704 source packet drifted")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != ["src/craftdef.cpp", "src/craftdef.h"]
    ):
        raise RuntimeError("#8704 patch receipt drifted")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 8607
    ):
        raise RuntimeError("#8704 atomicity receipt drifted")
    if transform.payload.get("transform") != "support_test_relocation":
        raise RuntimeError("#8704 test transform drifted")
    if manifest.payload.get("candidate") != _CANDIDATE or manifest.payload.get("state") != "passed":
        raise RuntimeError("#8704 test manifest drifted")
    if (
        capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("merged_tip") != _FIXED_COMMIT
        or capsule.payload.get("complete") is not True
    ):
        raise RuntimeError("#8704 capsule drifted")
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("stable_repetitions") is not True
    ):
        raise RuntimeError("#8704 Gate 5--7 aggregate drifted")
    if (
        transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "gate57_passed"
        or transition.payload.get("terminal") is not False
    ):
        raise RuntimeError("#8704 Gate 5--7 transition drifted")
    return values


def _register_dependency(
    root: Path,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    fixed = _directory(root / _PAIR / "fixed", root)
    evidence = {
        "win64": fixed / "util/buildbot/buildwin64.sh",
        "win32": fixed / "util/buildbot/buildwin32.sh",
    }
    for path in evidence.values():
        if "irrlicht_version=1.8.4" not in _file(path, root).read_text(encoding="utf-8"):
            raise RuntimeError("#8704 Irrlicht version pin drifted")
    archive = _file(root / _DEP / "irrlicht-1.8.4.zip", root)
    if _fingerprint(archive) != _ARCHIVE_FINGERPRINT:
        raise RuntimeError("#8704 authoritative Irrlicht archive drifted")
    source = _directory(root / _DEP / "source", root)
    inventory, release_fingerprint = _archive_inventory(archive, source)
    placeholder = _file(root / _DEP / "link-placeholder/libIrrlicht.a", root)
    empty_source = _file(root / _DEP / "link-placeholder/empty.c", root)
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "complete": True,
        "dependency_family": "Irrlicht",
        "release": "1.8.4",
        "authority_remote": _ARCHIVE_REMOTE,
        "archive_path": f"{_DEP.as_posix()}/irrlicht-1.8.4.zip",
        "archive_fingerprint": _ARCHIVE_FINGERPRINT,
        "source_path": f"{_DEP.as_posix()}/source",
        "release_tree_fingerprint": release_fingerprint,
        "inventory": inventory,
        "version_evidence": {
            name: path.relative_to(root).as_posix() for name, path in evidence.items()
        },
        "server_link_placeholder": placeholder.relative_to(root).as_posix(),
        "server_link_placeholder_fingerprint": _fingerprint(placeholder),
        "server_link_placeholder_source": empty_source.relative_to(root).as_posix(),
        "candidate_owned": True,
        "source_from_other_candidate": False,
        "prior_cross_candidate_path": "private/dependencies/luanti-9722-irrlicht/1.0",
        "prior_cross_candidate_receipt_superseded": True,
        "network_action": "read_only_authoritative_release_download",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root, _DEPENDENCY, tuple(item.reference for item in chain), payload, authorization
    )


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
    fixed_fingerprint = hashlib.sha256()
    buggy_fingerprint = hashlib.sha256()
    for entry in entries:
        meta, relative = entry.split("\t", 1)
        mode = meta.split(" ", 1)[0]
        left = _tree_bytes(fixed / PurePosixPath(relative), mode, root)
        right = _tree_bytes(buggy / PurePosixPath(relative), mode, root)
        for value, content in ((fixed_fingerprint, left), (buggy_fingerprint, right)):
            value.update(relative.encode("utf-8") + b"\0" + mode.encode("ascii") + b"\0")
            value.update(hashlib.sha256(content).digest())
        if left != right:
            differences.append(relative)
    if differences != ["src/craftdef.cpp", "src/craftdef.h"]:
        raise RuntimeError("#8704 source pair is not production-only")
    preserved = (
        "games/minimal/mods/default/init.lua",
        "games/minimal/mods/test/init.lua",
        "games/minimal/mods/test/mod.conf",
    )
    for relative in preserved:
        left = _file(fixed / PurePosixPath(relative), root).read_bytes()
        right = _file(buggy / PurePosixPath(relative), root).read_bytes()
        if left != right:
            raise RuntimeError("#8704 fixed test/support bytes changed")
    payload = {
        "candidate": _CANDIDATE,
        "state": "materialized",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "tracked_file_count": len(entries),
        "only_different_paths": differences,
        "fixed_inventory_fingerprint": fixed_fingerprint.hexdigest(),
        "buggy_inventory_fingerprint": buggy_fingerprint.hexdigest(),
        "preserved_test_support_paths": list(preserved),
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


def _register_configure(
    root: Path,
    variant: str,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    build = _directory(root / _BUILD / variant, root)
    cache_path = _file(build / "CMakeCache.txt", root)
    ninja_path = _file(build / "build.ninja", root)
    cache = cache_path.read_text(encoding="utf-8", errors="replace")
    ninja = ninja_path.read_text(encoding="utf-8", errors="replace")
    include_path = (root / _DEP / "source/include").as_posix()
    library_path = (root / _DEP / "link-placeholder/libIrrlicht.a").as_posix()
    required = (
        "BUILD_CLIENT:BOOL=FALSE",
        "BUILD_SERVER:BOOL=TRUE",
        f"IRRLICHT_INCLUDE_DIR:PATH={include_path}",
        f"IRRLICHT_LIBRARY:FILEPATH={library_path}",
    )
    if any(marker not in cache for marker in required):
        raise RuntimeError(f"#8704 {variant} configure cache drifted")
    if include_path not in ninja:
        raise RuntimeError(f"#8704 {variant} compile graph lacks candidate headers")
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "configured",
        "outcome": "closed_configure_passed",
        "complete": True,
        "source_tree_path": f"{_PAIR.as_posix()}/{variant}",
        "build_tree_path": f"{_BUILD.as_posix()}/{variant}",
        "cache_path": cache_path.relative_to(root).as_posix(),
        "cache_fingerprint": _fingerprint(cache_path),
        "build_graph_fingerprint": _fingerprint(ninja_path),
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "build_started": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (dependency.reference, source_pair.reference)
    return _write_once(root, _CONFIGURES[variant], inputs, payload, authorization)


def _register_readiness(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "configured_waiting_native_lane",
        "outcome": "gate8_static_closure_complete_build_pending",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_candidate_owned_dependency": "passed",
            "gate8_clean_fixed_buggy_pair": "passed",
            "gate8_fixed_buggy_configure": "passed",
            "gate8_fixed_buggy_build": "pending_native_lane",
            "gate9_to_13": "pending",
        },
        "candidate_owned_dependency": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "configure_receipts": [item.reference.artifact_id for item in configures],
        "prior_cross_candidate_receipt_superseded": True,
        "other_candidate_dependency_reused": False,
        "new_build_started": False,
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "when the single native lane is free, build fixed then buggy sequentially and register candidate-owned receipts",
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
    )
    return _write_once(root, _READINESS, inputs, payload, authorization)


def _register_target_probe(
    root: Path,
    variant: str,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    build = _directory(root / _BUILD / variant, root)
    ninja = _file(root / "private/dependencies/luanti-native/3.0/bin/ninja", root)
    targets = subprocess.run(
        (str(ninja), "-C", str(build), "-t", "targets", "all"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    dry = subprocess.run(
        (str(ninja), "-C", str(build), "-n", "minetestserver"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    target_present = b"minetestserver: phony" in targets.stdout
    planned_steps = sum(
        1 for line in dry.stdout.splitlines() if re_match_step(line)
    )
    if targets.returncode != 0 or dry.returncode != 0 or not target_present or planned_steps <= 0:
        raise RuntimeError(f"#8704 {variant} target probe failed")
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "passed",
        "outcome": "minetestserver_target_and_dry_run_passed",
        "complete": True,
        "target": "minetestserver",
        "target_present": True,
        "dry_run_returncode": dry.returncode,
        "planned_steps": planned_steps,
        "build_started": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
    )
    return _write_once(root, _PROBES[variant], inputs, payload, authorization)


def _register_mapping(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    probes: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    fixture = _directory(root / _FIXTURE, root)
    names = (
        "start.json",
        "replay.json",
        "oracle.json",
        "golden-contract.json",
        "visual-plan.json",
    )
    records = {
        name: {
            "path": (_FIXTURE / name).as_posix(),
            "bytes": _file(fixture / name, root).stat().st_size,
            "fingerprint": _fingerprint(fixture / name),
        }
        for name in names
    }
    oracle = _json(_file(fixture / "oracle.json", root))
    replay = _json(_file(fixture / "replay.json", root))
    if oracle.get("fixed_expected", {}).get("item_name") != "default:torch":
        raise RuntimeError("#8704 fixture oracle drifted")
    if len(replay.get("ordered_actions", [])) != 5:
        raise RuntimeError("#8704 fixture replay drifted")
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-8704-minimal-craft-result",
        "profile_id": "luanti-8704-minimal-craft-result-v2-native-gate57",
        "issue_number": 8607,
        "defect_locus": "at_goal",
        "state": "mapping_registered_fixture_frozen",
        "complete": True,
        "fixture_root": _FIXTURE.as_posix(),
        "fixture_files": list(names),
        "fixture_records": records,
        "registered_start": {
            "kind": "native_minimal_game_start",
            "binary_relative": "bin/minetestserver",
            "isolated_home_root": "private/runtime-home/luanti-8704-gate9-v1",
            "process_bound_seconds": 120,
        },
        "ordered_replay": replay["ordered_actions"],
        "semantic_oracle": {
            "goal": oracle["goal"],
            "defect": oracle["defect"],
            "invariants": oracle["invariants"],
        },
        "golden_contract": {
            "common_before_fixed_after_buggy_after": True,
            "pairwise_before_visual_equivalence": True,
            "two_by_two_after_cross_matrix": True,
            "same_lineage_required": True,
            "raw_semantic_observations_required": True,
        },
        "candidate_owned_irrlicht": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "target_probes": [item.reference.artifact_id for item in probes],
        "old_runtime_inheritance": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
        *(item.reference for item in probes),
    )
    return _write_once(root, _MAPPING, inputs, payload, authorization)


def _register_visual_plan(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    probes: tuple[StableArtifact, ...],
    mapping: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    visual = _json(_file(root / _FIXTURE / "visual-plan.json", root))
    if visual.get("capture_backend") != "minetest_builtin_F12_OpenGL_PNG":
        raise RuntimeError("#8704 visual plan drifted")
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-8704-minimal-craft-result",
        "state": "planned",
        "outcome": "real_client_visual_plan_registered",
        "complete": True,
        "capture_backend": visual["capture_backend"],
        "common_before": visual["common_before"],
        "fixed_after": visual["fixed_after"],
        "buggy_after": visual["buggy_after"],
        "semantic_source": visual["after_source"],
        "classification": visual["classification"],
        "source_or_test_modification": False,
        "real_frames_required": True,
        "synthetic_frames_forbidden": True,
        "same_lineage_required": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
        *(item.reference for item in probes),
        mapping.reference,
    )
    return _write_once(root, _VISUAL_PLAN, inputs, payload, authorization)


def _register_downstream_plan(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    probes: tuple[StableArtifact, ...],
    mapping: StableArtifact,
    visual: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-8704-minimal-craft-result",
        "state": "planned",
        "outcome": "mapping_fixture_visual_plan_complete_build_pending",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "gate_status": {
            "gate_1_7": "qualified",
            "gate8_static_dependency_pair_configure": "passed",
            "gate8_server_build": "pending_native_lane",
            "gate9_registered_start_replay": "pending",
            "gate10_hidden_oracle": "pending",
            "gate11_to_13_real_visual_and_admission": "planned",
        },
        "mapping": mapping.reference.artifact_id,
        "visual_plan": visual.reference.artifact_id,
        "execution_order": [
            "fixed_server_build",
            "buggy_server_build",
            "registered_start",
            "ordered_replay",
            "hidden_oracle",
            "fixed_buggy_real_client_build",
            "real_f12_capture",
            "visual_cross_judgment",
            "same_lineage_admission",
        ],
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
        *(item.reference for item in probes),
        mapping.reference,
        visual.reference,
    )
    return _write_once(root, _DOWNSTREAM_PLAN, inputs, payload, authorization)


def _register_readiness_v2(
    root: Path,
    chain: tuple[StableArtifact, ...],
    dependency: StableArtifact,
    source_pair: StableArtifact,
    configures: tuple[StableArtifact, ...],
    probes: tuple[StableArtifact, ...],
    mapping: StableArtifact,
    visual: StableArtifact,
    plan: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "state": "configured_mapped_waiting_native_lane",
        "outcome": "gate8_static_and_downstream_planning_complete_build_pending",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "current_gate_status": {
            "gate_1_7": "qualified",
            "gate8_candidate_owned_dependency": "passed",
            "gate8_clean_fixed_buggy_pair": "passed",
            "gate8_fixed_buggy_configure": "passed",
            "gate8_target_dry_run": "passed",
            "gate8_mapping_fixture_visual_plan": "passed",
            "gate8_fixed_buggy_build": "pending_native_lane",
            "gate9_to_13": "planned",
        },
        "candidate_owned_dependency": dependency.reference.artifact_id,
        "source_pair": source_pair.reference.artifact_id,
        "configure_receipts": [item.reference.artifact_id for item in configures],
        "target_probes": [item.reference.artifact_id for item in probes],
        "mapping": mapping.reference.artifact_id,
        "visual_plan": visual.reference.artifact_id,
        "downstream_plan": plan.reference.artifact_id,
        "prior_cross_candidate_receipt_superseded": True,
        "other_candidate_dependency_reused": False,
        "new_build_started": False,
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "after native-lane release, build fixed then buggy sequentially",
    }
    inputs = tuple(item.reference for item in chain) + (
        dependency.reference,
        source_pair.reference,
        *(item.reference for item in configures),
        *(item.reference for item in probes),
        mapping.reference,
        visual.reference,
        plan.reference,
    )
    return _write_once(root, _READINESS_V2, inputs, payload, authorization)


def re_match_step(line: bytes) -> bool:
    return line.startswith(b"[") and b"/" in line.split(b"]", 1)[0]


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError(f"JSON fixture is not an object: {path}")
    return value


def _archive_inventory(archive: Path, source: Path) -> tuple[dict[str, int], str]:
    value = hashlib.sha256()
    files = directories = total_bytes = 0
    with zipfile.ZipFile(archive) as package:
        for info in sorted(package.infolist(), key=lambda item: item.filename):
            if not info.filename.startswith("irrlicht-1.8.4/") or ".." in PurePosixPath(info.filename).parts:
                raise RuntimeError("#8704 Irrlicht archive inventory is unsafe")
            relative = PurePosixPath(info.filename).relative_to("irrlicht-1.8.4")
            if info.is_dir():
                directories += 1
                continue
            content = package.read(info)
            extracted = _file(source / Path(*relative.parts), source.parent)
            if extracted.read_bytes() != content:
                raise RuntimeError("#8704 extracted Irrlicht release differs from archive")
            files += 1
            total_bytes += len(content)
            value.update(relative.as_posix().encode("utf-8") + b"\0")
            value.update(hashlib.sha256(content).digest())
    return {"files": files, "directories": directories, "bytes": total_bytes}, value.hexdigest()


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#8704 Gate8 authorization is inactive")


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


def _tree_bytes(path: Path, mode: str, boundary: Path) -> bytes:
    path.resolve(strict=True).relative_to(boundary)
    info = path.lstat()
    if mode == "120000":
        if not stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"tracked symlink drifted: {path}")
        return os.readlink(path).encode("utf-8")
    return _file(path, boundary).read_bytes()


def _fingerprint(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ("git", "-C", str(cwd), *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace")[:500])
    return process.stdout.decode("utf-8").strip()


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = ["register_luanti_8704_gate8_static_repair"]
