"""Real-runtime golden capture for the OpenRA PR #21453 Phase-0 candidate."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c1_sandbox import strict_clone_tree
from .lazarus_goldens import (
    LazarusGoldenFrame,
    LazarusGoldenSet,
    register_lazarus_golden_set,
)
from .lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    OPENRA_HOTKEY_REGISTERED_START_SHA256,
    run_openra_pr21453_hotkey_cycle_probe,
)
from .lazarus_versions import LazarusVersionArtifact, LazarusVersionPair


OPENRA_HOTKEY_GOLDEN_SANDBOX_ROOT = Path("/private/tmp/warptest-lazarus-goldens")


@dataclass(frozen=True)
class OpenRAHotkeyGoldenCapture:
    """The two frames and coordinator-side evidence from one real game session."""

    before: LazarusGoldenFrame
    after: LazarusGoldenFrame


OpenRAHotkeyFrameRunner = Callable[
    [LazarusVersionArtifact, Path], OpenRAHotkeyGoldenCapture
]


def capture_openra_pr21453_goldens(
    pair: LazarusVersionPair,
    *,
    repository_root: str | Path,
    output_dir: str | Path,
    sandbox_root: str | Path = OPENRA_HOTKEY_GOLDEN_SANDBOX_ROOT,
    frame_runner: OpenRAHotkeyFrameRunner | None = None,
) -> LazarusGoldenSet:
    """Capture the public before and version-specific after states in CoW clones.

    Each version is restored into the same registered placement Start, then
    cancelled and run once through the real `E` production hotkey.  The fixed
    after frame is admitted only when the hidden goal group succeeds; the buggy
    after frame is admitted only when its hidden defect-signature group succeeds.
    Neither group is sent to an agent.
    """

    _validate_pair(pair)
    runner = frame_runner or _capture_openra_hotkey_frames
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openra-pr21453-", dir=sandbox) as temp_dir:
        work_dir = Path(temp_dir).resolve()
        fixed = runner(pair.fixed, work_dir)
        buggy = runner(pair.buggy, work_dir)
        return register_lazarus_golden_set(
            task_id=OPENRA_HOTKEY_PHASE0_TASK_ID,
            repository_root=repository_root,
            output_dir=output_dir,
            fixed_before=fixed.before,
            buggy_before=buggy.before,
            fixed_after=fixed.after,
            buggy_after=buggy.after,
            # OpenRA's full capture is stable between the two real versions;
            # retaining the header makes sidebar-page movement part of evidence.
            ignore_top_fraction=0.0,
            before_max_changed_pixel_ratio=0.01,
            after_min_changed_pixel_ratio=0.001,
        )


def _capture_openra_hotkey_frames(
    artifact: LazarusVersionArtifact,
    work_dir: Path,
) -> OpenRAHotkeyGoldenCapture:
    trial_root = work_dir / f"{artifact.version}-trial"
    strict_clone_tree(artifact.root.expanduser().resolve(), trial_root)
    output = work_dir / f"{artifact.version}-output"
    output.mkdir(parents=True, exist_ok=False)
    summary = run_openra_pr21453_hotkey_cycle_probe(
        trial_root,
        output,
        checkpoint_sha256=OPENRA_HOTKEY_REGISTERED_START_SHA256,
    )
    report = output / "openra-pr21453-hotkey-golden-report.json"
    _atomic_json(report, summary)
    before = summary.get("before")
    after = summary.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise RuntimeError(f"OpenRA {artifact.version} golden capture has no complete probe report")
    before_ok = (
        before.get("executed") is True
        and before.get("ready") is True
        and before.get("state_restore_applied") is True
    )
    if artifact.version == "fixed":
        after_ok = (
            after.get("executed") is True
            and after.get("goal_checks_ok") is True
            and after.get("invariants_ok") is True
        )
    else:
        after_ok = (
            after.get("executed") is True
            and after.get("defect_signature_ok") is True
            and after.get("invariants_ok") is True
        )
    before_image = output / "openra-pr21453-hotkey-cycle-before.png"
    after_image = output / "openra-pr21453-hotkey-cycle-after.png"
    if not before_image.is_file() or not after_image.is_file():
        raise RuntimeError(f"OpenRA {artifact.version} golden capture has no screenshots")
    return OpenRAHotkeyGoldenCapture(
        before=LazarusGoldenFrame(before_image, report, before_ok),
        after=LazarusGoldenFrame(after_image, report, after_ok),
    )


def _validate_pair(pair: LazarusVersionPair) -> None:
    if (
        pair.fixed.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID
        or pair.buggy.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID
        or pair.fixed.version != "fixed"
        or pair.buggy.version != "buggy"
        or pair.fixed.patch_sha256 != pair.buggy.patch_sha256
    ):
        raise ValueError("OpenRA golden capture requires its matched Phase-0 pair")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
