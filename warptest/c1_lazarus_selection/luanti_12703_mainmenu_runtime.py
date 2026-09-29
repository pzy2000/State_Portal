"""Fresh registered main-menu replay and real F12 evidence for Luanti #12703."""

from __future__ import annotations

import ctypes
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import struct
import subprocess
import time
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .luanti_12703_pagezero_adapter import (
    load_luanti_12703_pagezero_materialization,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 12703}
_TASK_ID = "lz-luanti-12703-contentdb-overwrite"
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/12703/lz-luanti-12703-contentdb-overwrite/v3",
    "lazarus_task_mapping",
    "3.0",
)
_PREFLIGHT = ArtifactReference(
    "downstream_preflight/luanti/12703/runtime-visual/v3",
    "downstream_preflight",
    "3.0",
)
_REGISTRATION = ArtifactReference(
    "candidate_12703_mainmenu_runner_registration/luanti/12703/fresh-v16/v1",
    "candidate_12703_mainmenu_runner_registration",
    "1.0",
)
PAIR = ArtifactReference(
    "candidate_12703_mainmenu_f12_pair/luanti/12703/fresh-v16/v1",
    "candidate_12703_mainmenu_f12_pair",
    "1.0",
)
OBSERVATION = ArtifactReference(
    "candidate_12703_mainmenu_observation/luanti/12703/fresh-v16/v1",
    "candidate_12703_mainmenu_observation",
    "1.0",
)
REPLAY = ArtifactReference(
    "candidate_12703_mainmenu_replay/luanti/12703/fresh-v16/v1",
    "candidate_12703_mainmenu_replay",
    "1.0",
)
ORACLE = ArtifactReference(
    "candidate_12703_mainmenu_hidden_oracle/luanti/12703/fresh-v16/v1",
    "candidate_12703_mainmenu_hidden_oracle",
    "1.0",
)
_TIMEOUT = 50.0


def run_luanti_12703_mainmenu_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Execute exactly one fresh fixed-then-buggy registered replay pair."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    if _try_load(root, PAIR) is not None:
        raise RuntimeError("#12703 fresh-v16 pair already has a terminal receipt")
    materialization = load_luanti_12703_pagezero_materialization(root)
    mapping = _load(root, _MAPPING)
    preflight = _load(root, _PREFLIGHT)
    _validate_inputs(mapping, preflight)
    native = _native_input_preflight()
    registration = _write_once(
        root,
        _REGISTRATION,
        (materialization.reference, mapping.reference, preflight.reference),
        {
            "candidate": dict(_CANDIDATE),
            "task_id": _TASK_ID,
            "state": "registered" if all(native.values()) else "blocked",
            "runner": "candidate_specific_mainmenu_escape_replay_f12_v16_plain_key_binding",
            "native_input_preflight": native,
            "ordered_variants": ["fixed", "buggy"],
            "ordered_replay": [
                "wait_registered_common_before",
                "capture_engine_f12_before",
                "post_escape_to_trigger_install_overwrite",
                "wait_registered_after_and_semantic_observation",
                "capture_engine_f12_after",
                "post_escape_to_close",
            ],
            "isolated_home_user_screenshots": True,
            "isolated_world": "not_applicable_mainmenu_surface",
            "isolated_port": "not_opened_mainmenu_surface",
            "prior_attempts_loaded": False,
            "prior_frames_inherited": False,
            "network_allowed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    if not all(native.values()):
        return _write_pair(
            root,
            registration,
            (),
            "blocked",
            "native_input_preflight_failed",
            None,
            authorization,
        )

    run_id = _safe_timestamp()
    run_root = root / "private/runs/luanti/12703/mainmenu-f12/fresh-v16" / run_id
    run_root.mkdir(parents=True, mode=0o700)
    attempts: list[StableArtifact] = []
    results: list[dict[str, Any]] = []
    for variant in ("fixed", "buggy"):
        try:
            result = _execute_variant(root, run_root, materialization, variant)
        except Exception as error:
            result = {
                "candidate": dict(_CANDIDATE),
                "variant": variant,
                "state": "blocked",
                "reason": f"{type(error).__name__}: {error}",
                "candidate_process_started": False,
                "process_exit_code": None,
                "frames": {},
                "semantic_observation": {},
                "prior_attempts_loaded": False,
                "prior_frames_inherited": False,
                "complete": True,
            }
        attempt = _write_once(
            root,
            ArtifactReference(
                f"candidate_12703_mainmenu_f12_attempt/luanti/12703/fresh-v16/{run_id}/{variant}",
                "candidate_12703_mainmenu_f12_attempt",
                "1.0",
            ),
            (registration.reference,),
            result,
            authorization,
        )
        attempts.append(attempt)
        results.append(result)
    state = "passed" if all(item.get("state") == "passed" for item in results) else "blocked"
    reason = None if state == "passed" else "one_or_more_fresh_variants_failed"
    matrix = _visual_matrix(root, results) if state == "passed" else None
    pair = _write_pair(
        root,
        registration,
        tuple(attempts),
        state,
        reason,
        matrix,
        authorization,
    )
    if state == "passed":
        _write_semantic_chain(root, pair, tuple(attempts), matrix, authorization)
    return pair


def load_luanti_12703_runtime_chain(
    artifact_root: str | Path,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    pair = _load(root, PAIR)
    observation = _load(root, OBSERVATION)
    replay = _load(root, REPLAY)
    oracle = _load(root, ORACLE)
    attempts = tuple(_load(root, ref) for ref in pair.inputs[1:])
    if not (
        pair.payload.get("candidate") == _CANDIDATE
        and pair.payload.get("state") == "passed"
        and pair.payload.get("attempt_count") == 2
        and pair.payload.get("ordered_variants") == ["fixed", "buggy"]
        and pair.payload.get("prior_attempts_loaded") is False
        and pair.payload.get("prior_frames_inherited") is False
        and len(attempts) == 2
    ):
        raise RuntimeError("#12703 runtime pair is invalid")
    for variant, attempt in zip(("fixed", "buggy"), attempts, strict=True):
        semantic = attempt.payload.get("semantic_observation", {})
        if not (
            attempt.payload.get("variant") == variant
            and attempt.payload.get("state") == "passed"
            and attempt.payload.get("candidate_process_started") is True
            and attempt.payload.get("process_exit_code") == 0
            and attempt.payload.get("registered_common_before") is True
            and attempt.payload.get("registered_after") is True
            and semantic.get("matched_expected") is True
            and attempt.payload.get("prior_attempts_loaded") is False
            and attempt.payload.get("prior_frames_inherited") is False
        ):
            raise RuntimeError("#12703 runtime attempt is invalid")
        for frame in attempt.payload["frames"].values():
            path = _ordinary_file(root / PurePosixPath(frame["path"]), root)
            raw = path.read_bytes()
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or (image.width, image.height) != (800, 600):
                    raise RuntimeError("#12703 runtime frame dimensions are invalid")
            if hashlib.sha256(raw).hexdigest() != frame.get("fingerprint"):
                raise RuntimeError("#12703 runtime frame changed")
    if not (
        observation.inputs == (pair.reference, *(item.reference for item in attempts))
        and replay.inputs == (observation.reference, pair.reference)
        and oracle.inputs == (replay.reference, observation.reference, pair.reference)
        and observation.payload.get("state") == "passed"
        and replay.payload.get("state") == "passed"
        and oracle.payload.get("state") == "passed"
        and oracle.payload.get("fixed_goal_observed") is True
        and oracle.payload.get("buggy_defect_observed") is True
    ):
        raise RuntimeError("#12703 semantic runtime chain is invalid")
    return pair, observation, replay, oracle


def _execute_variant(
    root: Path,
    run_root: Path,
    materialization: StableArtifact,
    variant: str,
    *,
    sign_run_bundle: bool = True,
) -> dict[str, Any]:
    source_app = root / PurePosixPath(str(materialization.payload[f"{variant}_app_path"]))
    variant_root = run_root / variant
    app = variant_root / "minetest.app"
    shutil.copytree(source_app, app, symlinks=True)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    source_binary = _ordinary_file(source_app / "Contents/MacOS/minetest", source_app)
    init = _ordinary_file(app / "Contents/Resources/builtin/mainmenu/init.lua", app)
    source_executable_fingerprint = _sha256(source_binary)
    copied_executable_fingerprint = _sha256(binary)
    if copied_executable_fingerprint != source_executable_fingerprint:
        raise RuntimeError("copied app executable differs from candidate build")
    signature_before = _signing_facts(app, binary)
    original_init = init.read_text(encoding="utf-8")
    injection_point = "\tcheck_new_version()\n\ttv_main:show()\n\tui.update()"
    if original_init.count(injection_point) != 1:
        raise RuntimeError("main-menu harness injection point is not unique")
    init.write_text(
        original_init.replace(injection_point, _MAINMENU_HARNESS.strip("\n"), 1),
        encoding="utf-8",
    )
    harness_fingerprint = hashlib.sha256(_MAINMENU_HARNESS.encode("utf-8")).hexdigest()
    if _sha256(binary) != copied_executable_fingerprint:
        raise RuntimeError("test-support injection changed executable before signing")
    if sign_run_bundle:
        subprocess.run(
            (
                "codesign",
                "--force",
                "--deep",
                "--sign",
                "-",
                "--timestamp=none",
                str(app),
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ("codesign", "--verify", "--deep", "--strict", str(app)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    signature_after = _signing_facts(app, binary)
    if sign_run_bundle and not (
        signature_after["code_resources_fingerprint"]
        and signature_after["cdhash"]
        and signature_before["executable_text_fingerprint"]
        == signature_after["executable_text_fingerprint"]
    ):
        raise RuntimeError("ad-hoc re-sign changed executable code or lacks signature evidence")

    runtime = variant_root / "runtime"
    home = runtime / "home"
    application_support = home / "Library/Application Support"
    user = application_support / "minetest"
    screenshots = runtime / "screenshots"
    temporary = runtime / "tmp"
    for path in (application_support, user, screenshots, temporary):
        path.mkdir(parents=True, mode=0o700)
    config = runtime / "minetest.conf"
    config.write_text(
        "video_driver = opengl\n"
        "screen_w = 800\nscreen_h = 600\nfullscreen = false\nvsync = false\n"
        "enable_sound = false\nserver_announce = false\nenable_console = true\n"
        "enable_update_checker = false\nlanguage = en\n"
        "debug_log_level = action\n"
        f"screenshot_path = {screenshots}\n"
        "screenshot_format = png\nkeymap_screenshot = KEY_KEY_P\n",
        encoding="utf-8",
    )
    log = runtime / "minetest.log"
    argv = (str(binary), "--config", str(config), "--logfile", str(log))
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_USER_PATH": str(user),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(temporary),
    }
    before_files = tuple(screenshots.glob("*.png"))
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=variant_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    frames: dict[str, dict[str, Any]] = {}
    try:
        _wait_marker(process, log, "C1_12703_COMMON_BEFORE_READY", started)
        _activate_process(process.pid)
        _post_key(process.pid, 35)
        before = _wait_new_png(process, screenshots, before_files, started)
        frames["common_before"] = _frame(before, root)
        time.sleep(1.1)
        after_before = tuple(screenshots.glob("*.png"))
        _post_key(process.pid, 53)
        _wait_marker(process, log, "C1_12703_AFTER_READY", started)
        time.sleep(0.5)
        _post_key(process.pid, 35)
        after = _wait_new_png(process, screenshots, after_before, started)
        frames["after"] = _frame(after, root)
        _post_key(process.pid, 53)
        stdout, stderr = process.communicate(
            timeout=max(1.0, _TIMEOUT - (time.monotonic() - started))
        )
    except Exception:
        _terminate(process)
        raise
    text = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")
    if log.is_file():
        text += "\n" + log.read_text(encoding="utf-8", errors="replace")
    semantic = _semantic_observation(text, variant, user)
    if process.returncode != 0 or not semantic["matched_expected"]:
        raise RuntimeError(
            f"client/semantic mismatch rc={process.returncode} semantic={semantic}"
        )
    return {
        "candidate": dict(_CANDIDATE),
        "variant": variant,
        "state": "passed",
        "reason": None,
        "candidate_process_started": True,
        "process_id": process.pid,
        "process_exit_code": process.returncode,
        "argv": list(argv),
        "run_root": variant_root.relative_to(root).as_posix(),
        "frames": frames,
        "registered_common_before": True,
        "registered_after": True,
        "ordered_replay": [
            "C1_12703_COMMON_BEFORE_READY",
            "KEY_ESCAPE_INSTALL_OVERWRITE",
            "C1_12703_AFTER_READY",
        ],
        "semantic_observation": semantic,
        "isolated_home": home.relative_to(root).as_posix(),
        "isolated_user": user.relative_to(root).as_posix(),
        "isolated_screenshots": screenshots.relative_to(root).as_posix(),
        "network_allowed": False,
        "prior_attempts_loaded": False,
        "prior_frames_inherited": False,
        "synthetic_frames_written": False,
        "run_bundle_adhoc_signed": sign_run_bundle,
        "run_bundle_signing": {
            "before": signature_before,
            "after": signature_after,
            "source_executable_fingerprint": source_executable_fingerprint,
            "copied_executable_fingerprint": copied_executable_fingerprint,
            "app_executable_text_hash_unchanged": (
                signature_before["executable_text_fingerprint"]
                == signature_after["executable_text_fingerprint"]
            ),
            "whole_executable_hash_changed_only_by_signing_metadata": (
                signature_before["executable_fingerprint"]
                != signature_after["executable_fingerprint"]
                and signature_before["executable_text_fingerprint"]
                == signature_after["executable_text_fingerprint"]
            ),
            "bundle_resign_scope": "adhoc_signature_metadata_only",
        },
        "candidate_specific_test_support": {
            "path": init.relative_to(app).as_posix(),
            "harness_fingerprint": harness_fingerprint,
            "production_patch_member": False,
            "classification": "runtime_test_support_outside_production_only_delta",
        },
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _semantic_observation(text: str, variant: str, user: Path) -> dict[str, Any]:
    marker = next(
        (line.split("C1_12703_AFTER_READY", 1)[1].strip() for line in text.splitlines() if "C1_12703_AFTER_READY" in line),
        None,
    )
    target = user / "mods/c1_target/init.lua"
    content = target.read_text(encoding="utf-8").strip() if target.is_file() else None
    expected = "return 'new-content'" if variant == "fixed" else "return 'old-content'"
    return {
        "marker": marker,
        "target_content": content,
        "expected_target_content": expected,
        "overwrite_succeeded": content == "return 'new-content'",
        "bug_signature_observed": content == "return 'old-content'",
        "mainmenu_remained_responsive": marker is not None,
        "matched_expected": marker is not None and content == expected,
    }


def _write_pair(
    root: Path,
    registration: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    state: str,
    reason: str | None,
    matrix: dict[str, Any] | None,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": dict(_CANDIDATE),
        "task_id": _TASK_ID,
        "state": state,
        "reason": reason,
        "attempt_count": len(attempts),
        "ordered_variants": [item.payload.get("variant") for item in attempts],
        "registered_common_before": state == "passed",
        "ordered_replay": state == "passed",
        "real_semantic_observations": state == "passed",
        "real_engine_f12_frames": state == "passed",
        "visual_matrix": matrix,
        "prior_attempts_loaded": False,
        "prior_frames_inherited": False,
        "synthetic_frames_written": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(
        root,
        PAIR,
        (registration.reference, *(item.reference for item in attempts)),
        payload,
        authorization,
    )


def _write_semantic_chain(
    root: Path,
    pair: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    matrix: dict[str, Any] | None,
    authorization: AuthorizationRecord,
) -> None:
    fixed = attempts[0].payload["semantic_observation"]
    buggy = attempts[1].payload["semantic_observation"]
    observation = _write_once(
        root,
        OBSERVATION,
        (pair.reference, *(item.reference for item in attempts)),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed",
            "registered_start": True,
            "start_surface": "mainmenu_contentdb_overwrite",
            "raw_fixed_and_buggy_start_observations": True,
            "common_before_frames": [
                attempts[0].payload["frames"]["common_before"],
                attempts[1].payload["frames"]["common_before"],
            ],
            "fixed_after": attempts[0].payload["frames"]["after"],
            "buggy_after": attempts[1].payload["frames"]["after"],
            "fixed_semantic": fixed,
            "buggy_semantic": buggy,
            "complete": True,
        },
        authorization,
    )
    replay = _write_once(
        root,
        REPLAY,
        (observation.reference, pair.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed",
            "ordered_replay": True,
            "actions": [
                "restore_registered_mainmenu_start",
                "capture_common_before",
                "escape_trigger_pkgmgr_install_dir_overwrite",
                "wait_for_after_semantic_marker",
                "capture_after",
            ],
            "action_count": 1,
            "explicit_waits": True,
            "real_semantic_observations": True,
            "complete": True,
        },
        authorization,
    )
    _write_once(
        root,
        ORACLE,
        (replay.reference, observation.reference, pair.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed",
            "hidden_oracle": True,
            "goal": "fixed overwrite replaces old package content with new package content",
            "defect": "buggy overwrite leaves old package content",
            "invariants": [
                "mainmenu remains responsive through registered after marker",
                "source and target remain inside the disposable user profile",
                "one overwrite action is executed",
            ],
            "fixed_goal_observed": fixed["overwrite_succeeded"] is True,
            "buggy_defect_observed": buggy["bug_signature_observed"] is True,
            "invariants_observed": (
                fixed["mainmenu_remained_responsive"] is True
                and buggy["mainmenu_remained_responsive"] is True
            ),
            "before_visual_equivalent": bool(matrix and matrix["before_equivalent"]),
            "after_cross_difference_visible": bool(matrix and matrix["after_cross_rms"] > 1.0),
            "complete": True,
        },
        authorization,
    )


def _visual_matrix(root: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    fixed = results[0]["frames"]
    buggy = results[1]["frames"]
    paths = {
        "fixed_before": root / PurePosixPath(fixed["common_before"]["path"]),
        "buggy_before": root / PurePosixPath(buggy["common_before"]["path"]),
        "fixed_after": root / PurePosixPath(fixed["after"]["path"]),
        "buggy_after": root / PurePosixPath(buggy["after"]["path"]),
    }
    before = _rms(paths["fixed_before"], paths["buggy_before"])
    after = _rms(paths["fixed_after"], paths["buggy_after"])
    return {
        "before_pair_rms": before,
        "before_equivalent": before <= 1.0,
        "after_fixed_to_fixed_rms": 0.0,
        "after_fixed_to_buggy_rms": after,
        "after_buggy_to_fixed_rms": after,
        "after_buggy_to_buggy_rms": 0.0,
        "after_cross_rms": after,
        "two_by_two_complete": after > 1.0,
    }


def _rms(left: Path, right: Path) -> float:
    with Image.open(left) as a, Image.open(right) as b:
        a.load()
        b.load()
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        values = ImageStat.Stat(diff).rms
    return float(sum(value * value for value in values) / len(values)) ** 0.5


def _signing_facts(app: Path, binary: Path) -> dict[str, Any]:
    resources = app / "Contents/_CodeSignature/CodeResources"
    result = subprocess.run(
        ("codesign", "-d", "--verbose=4", str(app)),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    code_offset, code_size, code_hash = _macho_code_region(binary)
    text_offset, text_size, text_hash = _macho_text_region(binary)
    return {
        "codesign_inspection_exit_code": result.returncode,
        "cdhash": values.get("CDHash"),
        "candidate_cdhash_full": values.get("CandidateCDHashFull sha256"),
        "signature_kind": values.get("Signature"),
        "code_resources_fingerprint": _sha256(resources) if resources.is_file() else None,
        "executable_fingerprint": _sha256(binary),
        "executable_code_region_fingerprint": code_hash,
        "executable_code_signature_offset": code_offset,
        "executable_code_signature_size": code_size,
        "executable_text_fingerprint": text_hash,
        "executable_text_offset": text_offset,
        "executable_text_size": text_size,
    }


def _macho_code_region(path: Path) -> tuple[int, int, str]:
    raw = path.read_bytes()
    if raw[:4] != b"\xcf\xfa\xed\xfe":
        raise RuntimeError("#12703 executable is not a little-endian 64-bit Mach-O")
    command_count = struct.unpack_from("<I", raw, 16)[0]
    offset = 32
    for _ in range(command_count):
        command, command_size = struct.unpack_from("<II", raw, offset)
        if command_size < 8 or offset + command_size > len(raw):
            raise RuntimeError("#12703 Mach-O load commands are invalid")
        if command == 0x1D:
            signature_offset, signature_size = struct.unpack_from("<II", raw, offset + 8)
            if signature_offset + signature_size > len(raw):
                raise RuntimeError("#12703 Mach-O code signature is invalid")
            return (
                signature_offset,
                signature_size,
                hashlib.sha256(raw[:signature_offset]).hexdigest(),
            )
        offset += command_size
    raise RuntimeError("#12703 Mach-O lacks LC_CODE_SIGNATURE")


def _macho_text_region(path: Path) -> tuple[int, int, str]:
    raw = path.read_bytes()
    if raw[:4] != b"\xcf\xfa\xed\xfe":
        raise RuntimeError("#12703 executable is not a little-endian 64-bit Mach-O")
    command_count = struct.unpack_from("<I", raw, 16)[0]
    command_offset = 32
    for _ in range(command_count):
        command, command_size = struct.unpack_from("<II", raw, command_offset)
        if command_size < 8 or command_offset + command_size > len(raw):
            raise RuntimeError("#12703 Mach-O load commands are invalid")
        if command == 0x19 and command_size >= 72:
            section_count = struct.unpack_from("<I", raw, command_offset + 64)[0]
            section_offset = command_offset + 72
            if section_offset + section_count * 80 > command_offset + command_size:
                raise RuntimeError("#12703 Mach-O sections are invalid")
            for index in range(section_count):
                current = section_offset + index * 80
                section_name = raw[current : current + 16].split(b"\0", 1)[0]
                segment_name = raw[current + 16 : current + 32].split(b"\0", 1)[0]
                if section_name == b"__text" and segment_name == b"__TEXT":
                    section_size = struct.unpack_from("<Q", raw, current + 40)[0]
                    file_offset = struct.unpack_from("<I", raw, current + 48)[0]
                    if file_offset + section_size > len(raw):
                        raise RuntimeError("#12703 Mach-O __text section is invalid")
                    return (
                        file_offset,
                        section_size,
                        hashlib.sha256(raw[file_offset : file_offset + section_size]).hexdigest(),
                    )
        command_offset += command_size
    raise RuntimeError("#12703 Mach-O lacks __TEXT,__text")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frame(path: Path, root: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    with Image.open(path) as image:
        image.load()
        if image.format != "PNG" or (image.width, image.height) != (800, 600):
            raise RuntimeError("#12703 F12 frame is not an 800x600 PNG")
    return {
        "path": path.relative_to(root).as_posix(),
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "width": 800,
        "height": 600,
        "ordinary_file": True,
        "synthetic": False,
    }


def _native_input_preflight() -> dict[str, bool]:
    app = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    app.AXIsProcessTrusted.restype = ctypes.c_bool
    cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
    return {
        "accessibility_trusted": bool(app.AXIsProcessTrusted()),
        "post_event_access": bool(cg.CGPreflightPostEventAccess()),
        "quartz_console_present": Path("/dev/console").exists(),
    }


def _post_key(pid: int, keycode: int) -> None:
    if pid <= 0:
        raise RuntimeError("invalid candidate process ID")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    for down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(None, keycode, down)
        if not event:
            raise RuntimeError("failed to create keyboard event")
        cg.CGEventPost(0, event)
        cf.CFRelease(event)
        time.sleep(0.08)


def _activate_process(pid: int) -> None:
    class ProcessSerialNumber(ctypes.Structure):
        _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

    app = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    app.GetProcessForPID.argtypes = [ctypes.c_int32, ctypes.POINTER(ProcessSerialNumber)]
    app.GetProcessForPID.restype = ctypes.c_int32
    app.SetFrontProcess.argtypes = [ctypes.POINTER(ProcessSerialNumber)]
    app.SetFrontProcess.restype = ctypes.c_int32
    serial = ProcessSerialNumber()
    if app.GetProcessForPID(pid, ctypes.byref(serial)) != 0:
        raise RuntimeError("failed to resolve candidate process serial number")
    if app.SetFrontProcess(ctypes.byref(serial)) != 0:
        raise RuntimeError("failed to activate candidate process")
    time.sleep(0.5)


def _wait_marker(
    process: subprocess.Popen[bytes], log: Path, marker: str, started: float
) -> None:
    while time.monotonic() - started < _TIMEOUT:
        if log.is_file() and marker in log.read_text(encoding="utf-8", errors="replace"):
            time.sleep(0.35)
            return
        if process.poll() is not None:
            raise RuntimeError(f"client exited before {marker}")
        time.sleep(0.1)
    raise TimeoutError(f"timeout waiting for {marker}")


def _wait_new_png(
    process: subprocess.Popen[bytes],
    directory: Path,
    before: tuple[Path, ...],
    started: float,
) -> Path:
    known = {path.resolve() for path in before}
    while time.monotonic() - started < _TIMEOUT:
        new = [path for path in directory.glob("*.png") if path.resolve() not in known]
        if len(new) == 1:
            size = new[0].stat().st_size
            time.sleep(0.15)
            if new[0].stat().st_size == size and size > 0:
                return new[0]
        if len(new) > 1:
            raise RuntimeError("more than one new F12 frame appeared")
        if process.poll() is not None:
            raise RuntimeError("client exited before F12 frame was written")
        time.sleep(0.1)
    raise TimeoutError("timeout waiting for F12 frame")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _validate_inputs(mapping: StableArtifact, preflight: StableArtifact) -> None:
    if not (
        mapping.payload.get("candidate") == _CANDIDATE
        and mapping.payload.get("task_id") == _TASK_ID
        and mapping.payload.get("defect_locus") == "at_goal"
        and preflight.payload.get("candidate") == _CANDIDATE
        and preflight.payload.get("admission_state") == "active_qualified"
        and preflight.payload.get("application_pair_status") == "complete_launch_unverified"
        and preflight.payload.get("candidate_execution_performed") is False
        and preflight.payload.get("paid_actions_authorized") is False
    ):
        raise RuntimeError("#12703 downstream inputs are invalid")


def _ordinary_file(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#12703 runtime file is unsafe")
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


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError(f"#12703 artifact drifted: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _safe_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z").replace(":", "-").replace(".", "-").lower()


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if not (
        type(authorization) is AuthorizationRecord
        and authorization.scope == "preannotation-execution"
        and not authorization.revoked
        and authorization.local_user_id == os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


_MAINMENU_HARNESS = r'''

-- C1 Lazarus #12703 candidate-specific main-menu replay harness.
do
	local c1_state = "before"
	local c1_user = core.get_user_path()
	local c1_mods = c1_user .. DIR_DELIM .. "mods"
	local c1_source = c1_mods .. DIR_DELIM .. "c1_source"
	local c1_target = c1_mods .. DIR_DELIM .. "c1_target"
	core.create_dir(c1_mods)
	core.delete_dir(c1_source)
	core.delete_dir(c1_target)
	assert(core.create_dir(c1_source))
	assert(core.create_dir(c1_target))
	assert(core.safe_file_write(c1_source .. DIR_DELIM .. "mod.conf", "name = c1_target\n"))
	assert(core.safe_file_write(c1_source .. DIR_DELIM .. "init.lua", "return 'new-content'\n"))
	assert(core.safe_file_write(c1_target .. DIR_DELIM .. "mod.conf", "name = c1_target\n"))
	assert(core.safe_file_write(c1_target .. DIR_DELIM .. "init.lua", "return 'old-content'\n"))

	local function read_target()
		local stream = io.open(c1_target .. DIR_DELIM .. "init.lua", "r")
		if not stream then return "missing" end
		local value = stream:read("*all") or ""
		stream:close()
		return value:gsub("%s+$", "")
	end

	local c1_dialog
	local function formspec()
		local title = c1_state == "before" and "C1 COMMON BEFORE" or
			(c1_state == "fixed" and "C1 OVERWRITE PASSED" or "C1 OVERWRITE DEFECT")
		return "formspec_version[4]size[20,14]bgcolor[#202020;true]" ..
			"box[0,0;20,14;#202020]label[7.1,6.0;" .. title .. "]" ..
			"label[5.8,7.0;ESC executes one registered overwrite action]"
	end

	local function execute_once()
		if c1_state ~= "before" then return end
		local ok, path, message = pcall(pkgmgr.install_dir, "mod", c1_source, "c1_target", nil)
		local target = read_target()
		c1_state = (ok and target == "return 'new-content'") and "fixed" or "buggy"
		core.log("error", "C1_12703_AFTER_READY target=" .. target ..
			" call_ok=" .. tostring(ok) .. " path=" .. tostring(path) ..
			" message=" .. tostring(message))
		ui.update()
	end

	c1_dialog = dialog_create(
		"c1_12703_registered_replay",
		formspec,
		function(this, fields)
			if fields.c1_action then execute_once() return true end
		end,
		function(event)
			if event ~= "MenuQuit" then return true end
			if c1_state == "before" then
				execute_once()
				return true
			end
		core.log("error", "C1_12703_REPLAY_COMPLETE")
			core.close()
			return true
		end
	)
	local parent = ui.find_by_name("maintab")
	if parent then parent:hide() c1_dialog:set_parent(parent) end
	c1_dialog:show()
	ui.update()
	core.log("error", "C1_12703_COMMON_BEFORE_READY target=" .. read_target())
end
'''


__all__ = [
    "OBSERVATION",
    "ORACLE",
    "PAIR",
    "REPLAY",
    "load_luanti_12703_runtime_chain",
    "run_luanti_12703_mainmenu_pair",
]
