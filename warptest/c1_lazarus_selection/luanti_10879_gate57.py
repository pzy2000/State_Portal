"""Fresh Gates 5--7 execution for the current-v2 Luanti #10879 chain.

The PR is an old Minetest tree whose unit-test driver predates ``--test-module``.
This adapter therefore runs the complete native suite and binds each cell to the
unique ``TestConnection``/``TestSettings`` module summary.  The fixed capsule,
production-only inverse, compiler launcher, builds, and all twelve cells live in
new #10879 namespaces; no #12602 receipt or tree is loaded.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from . import luanti_12602_gate57 as _base
from .models import CandidateKey
from .luanti_native_regression import _patch_sections
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_10879_GATE57_NATIVE_V1"
_CANDIDATE = CandidateKey("luanti", 10879)
_VALUE = _CANDIDATE.to_value()
_EPOCH = "candidate_gate57_10879_native_v1"
_PROFILE = "luanti-10879-utf16-network-v1-native-gate57"
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_CAPSULE_TREE = PurePosixPath("private/fixed-capsules/luanti/10879/fixed-source")
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/10879/gate57-v1")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/10879/gate57-v1")
_TOOLS_ROOT = PurePosixPath("private/bounded-tools/luanti/10879/gate57-v1")
_CXX_LAUNCHER = _TOOLS_ROOT / "cxx17-launcher"
_IRR_SOURCE = PurePosixPath("private/fixed-capsules/luanti/14464/fixed-source/irr")
_IRR_ROOT = PurePosixPath("private/dependencies/luanti-10879-irrlicht/1.9.0.15")
_IRR_INCLUDE = _IRR_ROOT / "include"
_BINARY = "bin/minetestserver"
_SOURCE_REV = "5ac220b100d3793e692eecf0d6aa8ee45c0e013d"
_PRODUCTION = (
    "src/chat.cpp",
    "src/client/client.cpp",
    "src/client/keycode.cpp",
    "src/gui/guiConfirmRegistration.cpp",
    "src/network/networkpacket.cpp",
    "src/network/networkpacket.h",
    "src/network/serverpackethandler.cpp",
    "src/script/lua_api/l_server.cpp",
    "src/server.cpp",
    "src/server.h",
    "src/util/string.cpp",
    "src/util/string.h",
    "util/wireshark/minetest.lua",
)
_TESTS = ("src/unittest/test_connection.cpp", "src/unittest/test_utilities.cpp")
_P2P_TESTS = ("src/unittest/test_settings.cpp",)
_F2P_SELECTOR = "TestConnection.testNetworkPacketSerialize"
_P2P_SELECTOR = "TestSettings"
_VERSION = "1.0"
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/10879/fixed/v2", "fixed_source_capsule", "2.0"
)
_IRR_DEP = ArtifactReference(
    "candidate_gate57_10879_irrlicht_dependency/luanti/10879/native/v1",
    "candidate_gate57_10879_irrlicht_dependency",
    _VERSION,
)
_TOOLCHAIN_ARTIFACT = ArtifactReference(
    "candidate_gate57_10879_cxx17_toolchain/luanti/10879/native/v1",
    "candidate_gate57_10879_cxx17_toolchain",
    _VERSION,
)
_BUILD_KIND = "candidate_gate57_10879_build_v1"
_ATTEMPT_KIND = "candidate_gate57_10879_attempt_v1"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_10879_aggregate/luanti/10879/native/v1",
    "candidate_gate57_10879_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_10879_transition/luanti/10879/native/v1",
    "candidate_gate57_10879_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_10879_blocker/luanti/10879/native/v1",
    "candidate_gate57_10879_blocker",
    _VERSION,
)
_GATE34 = ArtifactReference(
    "candidate_gate34_audit/luanti/10879/current/v1",
    "candidate_gate34_audit",
    _VERSION,
)
_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([A-Za-z0-9_.-]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\)"
)


def _refs() -> tuple[ArtifactReference, ...]:
    return (
        ArtifactReference("candidate_source_packet/luanti/10879/source/v2", "candidate_source_packet", "2.0"),
        ArtifactReference("candidate_test_patch/luanti/10879/screening/v2", "candidate_test_patch", "2.0"),
        ArtifactReference("screening_checkpoint/luanti/10879/current/v2", "screening_checkpoint", "2.0"),
        ArtifactReference("source_atomicity/luanti/10879/preannotation/v2", "source_atomicity", "2.0"),
        ArtifactReference("candidate_stage/luanti/10879/test_manifest/v2", "candidate_stage", "2.0"),
    )


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, Any], auth: AuthorizationRecord) -> StableArtifact:
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=auth)
    return _load(root, ref)


def prepare_10879(artifact_root: str | Path, *, authorization: AuthorizationRecord) -> dict[str, str]:
    """Close the mechanical Gates 2--4 and register the minimal continuation profile."""
    root = Path(artifact_root).absolute().resolve(strict=True)
    terminal_ref = ArtifactReference("candidate_gate4_terminal/luanti/10879/current/v1", "candidate_gate4_terminal", _VERSION)
    try:
        terminal = _load(root, terminal_ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if terminal.payload.get("outcome") == "gate4_incompatible":
            raise RuntimeError("#10879 Gate 4 terminal already recorded; no further retry is permitted")
    source, patch, checkpoint, atomicity, manifest = tuple(_load(root, ref) for ref in _refs())
    if source.payload.get("candidate") != _VALUE or source.payload.get("complete") is not True:
        raise RuntimeError("#10879 source packet is not current-v2 complete")
    if patch.payload.get("candidate") != _VALUE or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#10879 patch packet is not current-v2 eligible")
    if checkpoint.payload.get("outcome") != "patch_eligible" or atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#10879 checkpoint/atomicity is not accepted")
    if atomicity.payload.get("primary_issue_number") != 7643 or atomicity.payload.get("authoritative_issue_numbers") != [7643]:
        raise RuntimeError("#10879 primary issue binding drifted")
    if manifest.payload.get("state") != "passed" or manifest.payload.get("details", {}).get("test_count") != 2:
        raise RuntimeError("#10879 test manifest is not the exact two-file current-v2 manifest")
    source_manifest = json.loads((root / PurePosixPath(source.payload["raw_evidence_path"])).read_text(encoding="utf-8"))
    issue = next(row["issue"] for row in source_manifest["issues"] if row["number"] == 7643)
    if "Bug" not in {item["name"] for item in issue["labels"]} or "send_chat_message truncates" not in issue["title"]:
        raise RuntimeError("#10879 Gate 2 bug evidence drifted")
    patch_paths = tuple(patch.payload["production_paths"] + patch.payload["test_paths"])
    if patch_paths != _PRODUCTION + _TESTS:
        raise RuntimeError("#10879 patch inventory drifted")
    raw = (root / PurePosixPath(patch.payload["raw_patch_path"])).read_bytes()
    sections = _patch_sections(raw)
    if tuple(sorted(section.path for section in sections)) != tuple(sorted(patch_paths)):
        raise RuntimeError("#10879 raw patch section order drifted")
    test_text = "".join("".join(section.lines) for section in sections if section.path in _TESTS)
    required = ("testNetworkPacketSerialize", "putRawPacket", "utf8_to_wide(\"¤\")", "wide_to_utf8")
    if not all(marker in test_text for marker in required):
        raise RuntimeError("#10879 fixed test markers are incomplete")
    profile_ref = ArtifactReference("candidate_profile/luanti/10879/preflight/v2", "candidate_profile", "2.0")
    prep_ref = ArtifactReference("candidate_preparation/luanti/10879/preannotation/v2", "candidate_preparation", "2.0")
    downstream_ref = ArtifactReference("candidate_downstream_plan/luanti/10879/preannotation/v2", "candidate_downstream_plan", "2.0")
    profile_payload = {
        "candidate": _VALUE, "profile_id": _PROFILE, "tier": "on_path_candidate",
        "primary_issue_number": 7643, "production_paths": list(_PRODUCTION), "test_paths": list(_TESTS),
        "p2p_test_paths": list(_P2P_TESTS), "f2p_selector": _F2P_SELECTOR, "p2p_selector": _P2P_SELECTOR,
        "fixed_source_tree_path": _CAPSULE_TREE.as_posix(), "runtime_dependency_id": "luanti-native-toolchain-v3",
        "strict_gate_status": {"source_authority": "passed", "single_defect_mapping": "passed", "strict_inverse_patch": "passed", "test_api_compatibility": "passed"},
        "fixed_source_tree_status": "pending", "runtime_status": "registered", "downstream_status": "planned",
        "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    profile = _write(root, profile_ref, (source.reference, patch.reference, atomicity.reference, manifest.reference), profile_payload, authorization)
    prep_payload = {
        "candidate": _VALUE, "profile_id": _PROFILE, "state": "prepared", "outcome": "downstream_pending",
        "profile_reference": profile.reference.artifact_id, "fixed_source_tree_path": _CAPSULE_TREE.as_posix(),
        "production_inverse_paths": list(_PRODUCTION), "preserved_test_paths": list(_TESTS),
        "f2p": {"selector": _F2P_SELECTOR, "repetitions": 3, "fixed_expected": "pass", "buggy_expected": "failure"},
        "p2p": {"selector": _P2P_SELECTOR, "repetitions": 3, "fixed_expected": "pass", "buggy_expected": "pass"},
        "strict_gate_status": profile_payload["strict_gate_status"], "runtime_status": "registered", "downstream_status": "planned",
        "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    preparation = _write(root, prep_ref, (profile.reference,), prep_payload, authorization)
    downstream_payload = {"candidate": _VALUE, "profile_id": _PROFILE, "state": "planned", "outcome": "gate8_13_deferred", "preparation_reference": preparation.reference.artifact_id, "gate57_required_cells": 12, "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False}
    _write(root, downstream_ref, (preparation.reference,), downstream_payload, authorization)
    audit_payload = {
        "candidate": _VALUE, "state": "passed", "outcome": "gate1_4_passed", "terminal": False, "resumable": True,
        "gate_status": {"gate_1_source_authority": "passed", "gate_2_single_defect_mapping": "passed_issue_7643_bug", "gate_3_strict_inverse_patch": "passed_production_only_13_paths", "gate_4_test_api_compatibility": "passed_static_existing_api_surface", "gate_5_7": "registered_pending"},
        "gate2_evidence": {"primary_issue": 7643, "primary_issue_labels": ["Bug", "@ Network", "@ Client Script API"], "linked_issue_numbers": [2574, 6938, 7643], "authoritative_issue_count": 1},
        "gate3_evidence": {"production_paths": list(_PRODUCTION), "test_paths_preserved": list(_TESTS), "raw_patch_paths_exact": True, "inverse_scope": "production_only"},
        "gate4_evidence": {"fixed_tests_preserved_in_inverse": True, "existing_buggy_api_markers": list(required), "fixed_only_api_detected": False},
        "candidate_execution_performed": False, "old_attempts_loaded": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    _write(root, _GATE34, (source.reference, patch.reference, checkpoint.reference, atomicity.reference, manifest.reference), audit_payload, authorization)
    return {"profile": profile.reference.artifact_id, "preparation": preparation.reference.artifact_id, "gate34": _GATE34.artifact_id}


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    refs = _refs()[:2] + _refs()[3:] + (
        ArtifactReference("candidate_profile/luanti/10879/preflight/v2", "candidate_profile", "2.0"),
        ArtifactReference("candidate_preparation/luanti/10879/preannotation/v2", "candidate_preparation", "2.0"),
        ArtifactReference("candidate_downstream_plan/luanti/10879/preannotation/v2", "candidate_downstream_plan", "2.0"),
    )
    chain = tuple(_load(root, ref) for ref in refs)
    if any(item.payload.get("candidate") != _VALUE for item in chain) or chain[0].payload.get("complete") is not True or chain[1].payload.get("outcome") != "patch_eligible" or chain[2].payload.get("outcome") != "accepted" or chain[3].payload.get("state") != "passed" or chain[4].payload.get("strict_gate_status", {}).get("test_api_compatibility") != "passed" or chain[5].payload.get("state") != "prepared" or chain[6].payload.get("state") != "planned":
        raise RuntimeError("#10879 current-v2 Gate 1--4 prerequisites are not passed")
    return chain


def _assert_fresh_namespace(root: Path) -> None:
    for ref in (_AGGREGATE, _TRANSITION, _BLOCKER):
        try:
            _load(root, ref)
        except RuntimeError as error:
            if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
                raise
        else:
            raise RuntimeError(f"#10879 fresh namespace already exists: {ref.artifact_id}")
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        directory = root / "artifacts" / kind / "luanti" / "10879"
        if directory.exists() and any(directory.rglob("*.json")):
            raise RuntimeError(f"#10879 fresh namespace is not empty: {kind}")


def _materialize_capsule(root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord) -> StableArtifact:
    manifest = json.loads((root / PurePosixPath(chain[0].payload["raw_evidence_path"])).read_text(encoding="utf-8"))
    if manifest.get("pull", {}).get("head", {}).get("sha") != _SOURCE_REV:
        raise RuntimeError("#10879 source head does not match retained mirror revision")
    mirror, git = root / _MIRROR, root / _TOOLCHAIN / "bin/git"
    if not mirror.is_dir() or not git.is_file():
        raise RuntimeError("#10879 retained mirror or registered git is unavailable")
    check = subprocess.run((str(git), f"--git-dir={mirror}", "rev-parse", "refs/c1/pull/10879/head"), capture_output=True, env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"}, check=False)
    if check.returncode != 0 or check.stdout.decode().strip() != _SOURCE_REV:
        raise RuntimeError("#10879 retained mirror head revision drifted")
    destination = root / _CAPSULE_TREE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="lz10879-capsule-", dir=destination.parent) as temp:
        staging = Path(temp) / "tree"
        staging.mkdir(mode=0o700)
        proc = subprocess.Popen((str(git), f"--git-dir={mirror}", "archive", "--format=tar", _SOURCE_REV), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"})
        assert proc.stdout is not None and proc.stderr is not None
        with tarfile.open(fileobj=proc.stdout, mode="r|") as stream:
            for member in stream:
                _base._safe_archive_member(member)
                target = staging / PurePosixPath(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700); target.symlink_to(member.linkname)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source_file = stream.extractfile(member)
                    if source_file is None: raise RuntimeError("#10879 source archive member is unreadable")
                    with source_file, target.open("wb") as output: shutil.copyfileobj(source_file, output)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
        stderr = proc.stderr.read(); code = proc.wait(timeout=60)
        if code != 0: raise RuntimeError("#10879 source archive failed: " + stderr.decode(errors="replace")[:300])
        _base._validate_tree(staging); os.replace(staging, destination)
    payload = {"candidate": _VALUE, "profile_id": _PROFILE, "source_tree_path": _CAPSULE_TREE.as_posix(), "source_revision": _SOURCE_REV, "mirror_ref": "refs/c1/pull/10879/head", "source_manifest_path": chain[0].payload["raw_evidence_path"], "tree_fingerprint": _base._tree_sha256(destination), "source_materialization": "git_archive_from_retained_mirror", "network_used": False, "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False, "state": "materialized", "complete": True}
    return _write(root, _CAPSULE, tuple(item.reference for item in chain[:5]), payload, authorization)


def _materialize_pair(root: Path, chain: tuple[StableArtifact, ...], capsule: StableArtifact, irr: StableArtifact) -> dict[str, Path]:
    base = root / _RUN_ROOT; fixed, buggy = base / "fixed", base / "buggy"; base.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(root / _CAPSULE_TREE, fixed, symlinks=True); shutil.copytree(fixed, buggy, symlinks=True)
    raw = (root / PurePosixPath(chain[1].payload["raw_patch_path"])).read_bytes(); sections = _patch_sections(raw); production = b"".join("".join(section.lines).encode("utf-8", "surrogateescape") for section in sections if section.path in _PRODUCTION)
    git = root / _TOOLCHAIN / "bin/git"; result = subprocess.run((str(git), "apply", "-R", "--whitespace=nowarn", "-"), cwd=buggy, input=production, capture_output=True, env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C", "GIT_DIR": "/dev/null"}, timeout=60, check=False)
    if result.returncode != 0: raise RuntimeError("#10879 candidate-specific production inverse failed: " + result.stderr.decode(errors="replace")[:500])
    _base._validate_tree(fixed); _base._validate_tree(buggy)
    for left in fixed.rglob("*"):
        rel = left.relative_to(fixed); right = buggy / rel
        if rel.as_posix() in _PRODUCTION:
            if _base._same_file(left, right): raise RuntimeError("#10879 production inverse did not change production")
        elif not _base._same_file(left, right):
            raise RuntimeError(f"#10879 production inverse changed non-production path {rel}")
    return {"fixed": fixed, "buggy": buggy}


def _build_variant(root: Path, chain: tuple[StableArtifact, ...], capsule: StableArtifact, irr: StableArtifact, launcher: StableArtifact, variant: str, tree: Path, authorization: AuthorizationRecord) -> StableArtifact:
    tc = root / _TOOLCHAIN; build = root / _BUILD_ROOT / variant; build.mkdir(parents=True, exist_ok=True, mode=0o700); packages = {name: tc / "packages" / name for name in ("sqlite3", "iconv", "zlib", "zstd")}; cmake = tc / "bin/cmake"
    configure = (str(cmake), "-S", str(tree), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_CLIENT=FALSE", "-DBUILD_SERVER=TRUE", "-DBUILD_UNITTESTS=TRUE", "-DBUILD_BENCHMARKS=FALSE", "-DBUILD_DOCUMENTATION=FALSE", "-DRUN_IN_PLACE=TRUE", "-DENABLE_LTO=FALSE", "-DENABLE_CURL=FALSE", "-DENABLE_GETTEXT=FALSE", "-DENABLE_POSTGRESQL=FALSE", "-DENABLE_LEVELDB=FALSE", "-DENABLE_REDIS=FALSE", "-DENABLE_SPATIAL=FALSE", "-DENABLE_OPENSSL=FALSE", "-DENABLE_CURSES=FALSE", "-DENABLE_LUAJIT=FALSE", "-DENABLE_SYSTEM_GMP=FALSE", "-DENABLE_SYSTEM_JSONCPP=FALSE", f"-DCMAKE_MAKE_PROGRAM={tc / 'bin/ninja'}", f"-DCMAKE_C_COMPILER={tc / 'bin/cc'}", f"-DCMAKE_CXX_COMPILER={tc / 'bin/c++'}", f"-DCMAKE_AR={tc / 'bin/ar'}", f"-DCMAKE_RANLIB={tc / 'bin/ranlib'}", f"-DCMAKE_LINKER={tc / 'bin/ld'}", f"-DCMAKE_NM={tc / 'bin/nm'}", f"-DCMAKE_STRIP={tc / 'bin/strip'}", f"-DCMAKE_OBJDUMP={tc / 'bin/otool'}", f"-DCMAKE_INSTALL_NAME_TOOL={tc / 'bin/install_name_tool'}", f"-DSQLITE3_INCLUDE_DIR={packages['sqlite3'] / 'include'}", f"-DSQLITE3_LIBRARY={packages['sqlite3'] / 'lib/libsqlite3.a'}", f"-DICONV_LIBRARY={packages['iconv'] / 'lib/libiconv.tbd'}", f"-DZLIB_INCLUDE_DIR={packages['zlib'] / 'include'}", f"-DZLIB_LIBRARY={packages['zlib'] / 'lib/libz.a'}", f"-DZSTD_INCLUDE_DIR={packages['zstd'] / 'include'}", f"-DZSTD_LIBRARY={packages['zstd'] / 'lib/libzstd.a'}", f"-DIRRLICHT_INCLUDE_DIR={root / _IRR_INCLUDE}", "-DCMAKE_CXX_STANDARD=17", "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE", "-DCMAKE_CXX_EXTENSIONS=OFF", "-DCMAKE_CXX_FLAGS=-w", f"-DCMAKE_CXX_COMPILER_LAUNCHER={root / _CXX_LAUNCHER}")
    build_argv = (str(cmake), "--build", str(build), "--target", "minetestserver", "--parallel", "1"); env = {"HOME": str(root / "private/runtime-home/luanti-10879-gate57"), "LANG": "C", "LC_ALL": "C", "PATH": str(tc / "bin"), "GIT_DIR": "/dev/null"}; last: StableArtifact | None = None
    for step, argv, timeout in (("configure", configure, 600), ("build", build_argv, 1800)):
        receipt = _base.run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=timeout); ref = ArtifactReference(f"{_BUILD_KIND}/luanti/10879/{variant}/{step}/v1", _BUILD_KIND, _VERSION); payload = {"candidate": _VALUE, "profile_id": _PROFILE, "fresh_epoch": _EPOCH, "variant": variant, "step": step, "tree_path": tree.relative_to(root).as_posix(), "binary_path": (tree / _BINARY).relative_to(root).as_posix(), "irrlicht_dependency": _IRR_DEP.artifact_id, "cxx17_toolchain": launcher.reference.artifact_id, "old_attempts_loaded": False, "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False, **_base._process_payload(variant, step, argv, receipt)}; last = _write(root, ref, tuple(item.reference for item in chain) + (capsule.reference, irr.reference, launcher.reference), payload, authorization)
        if receipt.outcome != "completed" or receipt.returncode != 0: raise RuntimeError(f"#10879 {variant} {step} failed")
    assert last is not None
    if not (tree / _BINARY).is_file(): raise RuntimeError(f"#10879 {variant} server binary is missing")
    return last


def _run_cell(root: Path, chain: tuple[StableArtifact, ...], capsule: StableArtifact, irr: StableArtifact, launcher: StableArtifact, build: StableArtifact, run: Any, variant: str, repetition: int, tree: Path, authorization: AuthorizationRecord) -> StableArtifact:
    phase = run.phase; selector = _F2P_SELECTOR if phase == "f2p" else _P2P_SELECTOR; expected = "pass" if variant == "fixed" or phase == "p2p" else "failure"; binary = tree / _BINARY; env = {"HOME": str(root / "private/runtime-home/luanti-10879-gate57"), "LANG": "C", "LC_ALL": "C", "PATH": str(root / _TOOLCHAIN / "bin"), "GIT_DIR": "/dev/null"}; receipt = _base.run_bounded_process((str(binary), "--run-unittests"), cwd=tree, env=env, timeout_seconds=300); module = selector.split(".", 1)[0]; matches = [match for stream in (receipt.stdout, receipt.stderr) for match in _MODULE_SUMMARY.finditer(stream) if match.group(1).decode() == module]; observed = "timed_out" if receipt.outcome == "timed_out" else "launch_failed" if receipt.outcome == "launch_failed" else "invalid_test_count" if len(matches) != 1 else ("pass" if matches[0].group(2) == b"passed" else "failure"); count = int(matches[0].group(4)) if len(matches) == 1 else 0; ref = ArtifactReference(f"{_ATTEMPT_KIND}/luanti/10879/{phase}/{variant}/{repetition}/v1", _ATTEMPT_KIND, _VERSION); stdout, st = _base._bounded_output(receipt.stdout); stderr, et = _base._bounded_output(receipt.stderr); payload = {"candidate": _VALUE, "profile_id": _PROFILE, "fresh_epoch": _EPOCH, "phase": phase, "variant": variant, "repetition": repetition, "selector": selector, "expected_outcome": expected, "observed_outcome": observed, "process_outcome": receipt.outcome, "returncode": receipt.returncode, "completed_test_count": count, "selector_mode": "module_summary", "selected_module": module, "selected_module_summary_count": len(matches), "selected_module_failures": int(matches[0].group(3)) if len(matches) == 1 else None, "selected_module_tests": int(matches[0].group(4)) if len(matches) == 1 else None, "argv": [str(binary), "--run-unittests"], "stdout_base64": base64.b64encode(stdout).decode(), "stderr_base64": base64.b64encode(stderr).decode(), "stdout_truncated": receipt.stdout_truncated or st, "stderr_truncated": receipt.stderr_truncated or et, "output_encoding": "base64", "matched_expected": observed == expected, "tree_path": tree.relative_to(root).as_posix(), "build_reference": build.reference.artifact_id, "irrlicht_dependency": _IRR_DEP.artifact_id, "cxx17_toolchain": launcher.reference.artifact_id, "old_attempts_loaded": False, "candidate_execution_performed": True, "paid_actions_authorized": False, "formal_trials_authorized": False}; return _write(root, ref, tuple(item.reference for item in chain) + (capsule.reference, irr.reference, launcher.reference, build.reference), payload, authorization)


def execute_luanti_10879_gate57(artifact_root: str | Path, *, authorization: AuthorizationRecord, confirmation: str = CONFIRMATION) -> tuple[StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION or authorization.scope != "preannotation-execution" or authorization.revoked or authorization.local_user_id != os.getuid(): raise PermissionError("#10879 Gate 5--7 authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True); chain = _load_chain(root); _assert_fresh_namespace(root)
    try:
        capsule = _base._load_or_materialize(root, _CAPSULE, _CAPSULE_TREE, _materialize_capsule, chain, authorization); irr = _base._load_or_materialize(root, _IRR_DEP, _IRR_ROOT, _base._materialize_irrlicht_dependency, chain, authorization); launcher = _base._ensure_cxx17_launcher(root, chain, capsule, irr, authorization); trees = _materialize_pair(root, chain, capsule, irr); builds = {variant: _build_variant(root, chain, capsule, irr, launcher, variant, trees[variant], authorization) for variant in ("fixed", "buggy")}; attempts = []
        for phase in ("f2p", "p2p"):
            run = type("Run", (), {"phase": phase})()
            for variant in ("fixed", "buggy"):
                for repetition in (1, 2, 3): attempts.append(_run_cell(root, chain, capsule, irr, launcher, builds[variant], run, variant, repetition, trees[variant], authorization))
    except BaseException as error:
        payload = {"candidate": _VALUE, "profile_id": _PROFILE, "fresh_epoch": _EPOCH, "state": "blocked", "outcome": "infrastructure_blocked", "terminal": False, "resumable": True, "reason": "candidate_specific_native_build_or_execution_failed", "error_type": type(error).__name__, "error": str(error), "candidate_materialization_performed": True, "candidate_execution_performed": False, "old_attempts_loaded": False, "next_action": "inspect #10879 fresh receipts; do not reuse #12602 attempts/tree", "paid_actions_authorized": False, "formal_trials_authorized": False}; blocker = _write(root, _BLOCKER, tuple(item.reference for item in chain), payload, authorization); return blocker, blocker
    aggregate = _base._write_aggregate(root, chain, capsule, irr, launcher, builds, attempts, authorization); transition = _base._write_transition(root, chain, capsule, irr, launcher, aggregate, attempts, authorization); return aggregate, transition


def _install_overrides() -> None:
    for name, value in {"_CANDIDATE": _CANDIDATE, "_VALUE": _VALUE, "_EPOCH": _EPOCH, "_PROFILE": _PROFILE, "_MIRROR": _MIRROR, "_TOOLCHAIN": _TOOLCHAIN, "_CAPSULE_TREE": _CAPSULE_TREE, "_RUN_ROOT": _RUN_ROOT, "_BUILD_ROOT": _BUILD_ROOT, "_TOOLS_ROOT": _TOOLS_ROOT, "_CXX_LAUNCHER": _CXX_LAUNCHER, "_IRR_SOURCE": _IRR_SOURCE, "_IRR_ROOT": _IRR_ROOT, "_IRR_INCLUDE": _IRR_INCLUDE, "_BINARY": _BINARY, "_CAPSULE": _CAPSULE, "_IRR_DEP": _IRR_DEP, "_TOOLCHAIN_ARTIFACT": _TOOLCHAIN_ARTIFACT, "_BUILD_KIND": _BUILD_KIND, "_ATTEMPT_KIND": _ATTEMPT_KIND, "_AGGREGATE": _AGGREGATE, "_TRANSITION": _TRANSITION, "_BLOCKER": _BLOCKER}.items(): setattr(_base, name, value)


_install_overrides()

__all__ = ["CONFIRMATION", "prepare_10879", "execute_luanti_10879_gate57"]
