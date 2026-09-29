"""Fail-closed registration of C1-Lazarus visual golden evidence.

The formal catalog keeps both versions' start goldens and both after-state
goldens.  This module does not judge images; it proves that the captured
evidence is usable before it is published into a task artifact:

* both materialized versions reached their expected start state;
* their start screenshots are visually equivalent outside a registered
  volatile header region;
* fixed and buggy after screenshots reached their expected semantic states;
* those after screenshots expose a visible differential outcome.

All output is staged beside the final directory and published only after every
check succeeds.  That keeps a partially captured golden set from looking like
registered provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .visual import screenshot_quality


LAZARUS_GOLDEN_SCHEMA_VERSION = 1
# ``lz-`` is the F2P-graded population; ``lzp-`` is the P2P-only population, whose
# rows carry no fail-to-pass evidence.  The two prefixes keep the populations
# distinguishable from the task id alone, so a report cannot mix them silently.
_TASK_ID = re.compile(r"^lzp?-[a-z0-9]+(?:-[a-z0-9]+)*$")
_LUMA_CHANGE_THRESHOLD = 24


class LazarusGoldenError(RuntimeError):
    """Raised when visual evidence cannot safely enter a Lazarus catalog."""


@dataclass(frozen=True)
class LazarusGoldenFrame:
    """One captured frame plus the semantic run that established its state."""

    image: Path
    report: Path
    expected_semantics_ok: bool


@dataclass(frozen=True)
class LazarusGoldenSet:
    """Published four-frame set and the construction metrics that justify it."""

    task_id: str
    before: Path
    buggy_before: Path
    fixed_after: Path
    buggy_after: Path
    metadata: Path
    before_metrics: dict[str, float]
    after_metrics: dict[str, float]


@dataclass(frozen=True)
class LazarusGoldenVerification:
    """Fail-closed view of a frozen golden-set metadata artifact."""

    task_id: str
    metadata_sha256: str | None
    complete: bool
    fixed_before_visual: bool
    buggy_before_visual: bool
    before_equivalent: bool
    fixed_after_visual: bool
    buggy_after_visual: bool
    failures: tuple[str, ...]


def register_lazarus_golden_set(
    *,
    task_id: str,
    repository_root: str | Path,
    output_dir: str | Path,
    fixed_before: LazarusGoldenFrame,
    buggy_before: LazarusGoldenFrame,
    fixed_after: LazarusGoldenFrame,
    buggy_after: LazarusGoldenFrame,
    ignore_top_fraction: float = 0.25,
    before_max_changed_pixel_ratio: float = 0.01,
    after_min_changed_pixel_ratio: float = 0.001,
) -> LazarusGoldenSet:
    """Validate and atomically publish a Lazarus four-frame golden set.

    ``buggy_after.expected_semantics_ok`` means that the *registered defect
    signature* succeeded, not that the fixed goal did.  That distinction lets
    a construction gate prove both sides of the inverse-patch differential
    without exposing the signature to the model payload.
    """

    normalized_task_id = str(task_id).strip()
    if not _TASK_ID.fullmatch(normalized_task_id):
        raise LazarusGoldenError("task_id must be a lowercase lz-* slug")
    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir():
        raise LazarusGoldenError(f"repository root does not exist: {root}")
    destination = _resolve_output_dir(root, output_dir)
    if destination.exists():
        raise LazarusGoldenError(
            f"Lazarus golden destination already exists: {destination}"
        )
    _validate_thresholds(
        ignore_top_fraction=ignore_top_fraction,
        before_max_changed_pixel_ratio=before_max_changed_pixel_ratio,
        after_min_changed_pixel_ratio=after_min_changed_pixel_ratio,
    )
    frames = {
        "fixed_before": _validate_frame("fixed_before", fixed_before),
        "buggy_before": _validate_frame("buggy_before", buggy_before),
        "fixed_after": _validate_frame("fixed_after", fixed_after),
        "buggy_after": _validate_frame("buggy_after", buggy_after),
    }
    before_metrics = image_change_metrics(
        frames["fixed_before"]["image"],
        frames["buggy_before"]["image"],
        ignore_top_fraction=ignore_top_fraction,
    )
    if before_metrics["changed_pixel_ratio"] > before_max_changed_pixel_ratio:
        raise LazarusGoldenError(
            "before frames are not equivalent: "
            f"changed pixel ratio {before_metrics['changed_pixel_ratio']:.6f} > "
            f"{before_max_changed_pixel_ratio:.6f}"
        )
    after_metrics = image_change_metrics(
        frames["fixed_after"]["image"],
        frames["buggy_after"]["image"],
        ignore_top_fraction=ignore_top_fraction,
    )
    if after_metrics["changed_pixel_ratio"] < after_min_changed_pixel_ratio:
        raise LazarusGoldenError(
            "after frames do not expose a visible differential outcome: "
            f"changed pixel ratio {after_metrics['changed_pixel_ratio']:.6f} < "
            f"{after_min_changed_pixel_ratio:.6f}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = destination.parent
    staging = Path(
        tempfile.mkdtemp(prefix=f".{normalized_task_id}-goldens-", dir=staging_parent)
    ).resolve()
    staged_visual = staging / "visual"
    staged_visual.mkdir()
    staged_paths = {
        "before": staged_visual / "before.golden.png",
        "buggy_before": staged_visual / "buggy.before.golden.png",
        "fixed_after": staged_visual / "fixed.after.golden.png",
        "buggy_after": staged_visual / "buggy.after.golden.png",
    }
    try:
        shutil.copyfile(frames["fixed_before"]["image"], staged_paths["before"])
        shutil.copyfile(frames["buggy_before"]["image"], staged_paths["buggy_before"])
        shutil.copyfile(frames["fixed_after"]["image"], staged_paths["fixed_after"])
        shutil.copyfile(frames["buggy_after"]["image"], staged_paths["buggy_after"])
        metadata = _metadata_payload(
            task_id=normalized_task_id,
            frames=frames,
            published=staged_paths,
            before_metrics=before_metrics,
            after_metrics=after_metrics,
            ignore_top_fraction=ignore_top_fraction,
            before_max_changed_pixel_ratio=before_max_changed_pixel_ratio,
            after_min_changed_pixel_ratio=after_min_changed_pixel_ratio,
        )
        metadata_path = staged_visual / "goldens.meta.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staged_visual, destination)
    except Exception:
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return LazarusGoldenSet(
        task_id=normalized_task_id,
        before=destination / staged_paths["before"].name,
        buggy_before=destination / staged_paths["buggy_before"].name,
        fixed_after=destination / staged_paths["fixed_after"].name,
        buggy_after=destination / staged_paths["buggy_after"].name,
        metadata=destination / "goldens.meta.json",
        before_metrics=before_metrics,
        after_metrics=after_metrics,
    )


def image_change_metrics(
    first: str | Path,
    second: str | Path,
    *,
    ignore_top_fraction: float = 0.0,
) -> dict[str, float]:
    """Return stable image-difference metrics with an optional volatile header mask."""

    if not 0.0 <= ignore_top_fraction < 1.0:
        raise LazarusGoldenError("ignore_top_fraction must be in [0, 1)")
    first_path = Path(first).expanduser().resolve()
    second_path = Path(second).expanduser().resolve()
    try:
        with Image.open(first_path) as first_image, Image.open(second_path) as second_image:
            first_rgb = first_image.convert("RGB")
            second_rgb = second_image.convert("RGB")
            if first_rgb.size != second_rgb.size:
                raise LazarusGoldenError(
                    "golden frame dimensions differ: "
                    f"{first_rgb.size} != {second_rgb.size}"
                )
            width, height = first_rgb.size
            top = int(round(height * ignore_top_fraction))
            crop = (0, top, width, height)
            difference = ImageChops.difference(
                first_rgb.crop(crop), second_rgb.crop(crop)
            )
            mean = ImageStat.Stat(difference).mean
            histogram = difference.convert("L").histogram()
    except LazarusGoldenError:
        raise
    except Exception as error:
        raise LazarusGoldenError(f"cannot compare golden frames: {error}") from error
    total = sum(histogram) or 1
    changed = sum(histogram[_LUMA_CHANGE_THRESHOLD:])
    return {
        "mean_absolute_difference": float(sum(mean) / len(mean)),
        "changed_pixel_ratio": float(changed / total),
        "ignored_top_fraction": float(ignore_top_fraction),
    }


def validate_lazarus_golden_set(
    metadata_path: str | Path,
    *,
    task_id: str,
) -> LazarusGoldenVerification:
    """Verify the published golden set and its frozen construction evidence.

    Both versions publish their start frame so a reviewer can see the pair the
    equivalence metric was computed from.  ``before.golden.png`` stays the fixed
    frame that runtime judging treats as the shared start.
    """

    failures: list[str] = []
    path = Path(metadata_path).expanduser().resolve()
    normalized_task_id = str(task_id).strip()
    if not path.is_file():
        return _verification_failure(normalized_task_id, "golden metadata is missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _verification_failure(
            normalized_task_id, f"golden metadata cannot be read: {error}"
        )
    if not isinstance(raw, dict):
        return _verification_failure(normalized_task_id, "golden metadata must be a mapping")
    metadata_sha = _sha256(path)
    if raw.get("schema") != "c1-lazarus-goldens-v1" or raw.get("schema_version") != LAZARUS_GOLDEN_SCHEMA_VERSION:
        failures.append("golden metadata schema mismatch")
    if raw.get("task_id") != normalized_task_id:
        failures.append("golden metadata task_id mismatch")
    frames = raw.get("frames") if isinstance(raw.get("frames"), dict) else {}
    expected_frames = ("fixed_before", "buggy_before", "fixed_after", "buggy_after")
    if set(frames) != set(expected_frames):
        failures.append("golden metadata frame set mismatch")
    frame_ok: dict[str, bool] = {}
    for name in expected_frames:
        record = frames.get(name)
        if not isinstance(record, dict):
            failures.append(f"{name} frame metadata is missing")
            frame_ok[name] = False
            continue
        image_sha = record.get("image_sha256")
        report_sha = record.get("report_sha256")
        quality = record.get("quality")
        valid = (
            record.get("expected_semantics_ok") is True
            and _is_sha256(image_sha)
            and _is_sha256(report_sha)
            and isinstance(quality, dict)
            and quality.get("informative") is True
        )
        if not valid:
            failures.append(f"{name} frame metadata is incomplete")
        frame_ok[name] = valid

    published = {
        "before": ("before.golden.png", "before_sha256", "fixed_before"),
        "fixed_after": ("fixed.after.golden.png", "fixed_after_sha256", "fixed_after"),
        "buggy_after": ("buggy.after.golden.png", "buggy_after_sha256", "buggy_after"),
    }
    # Phase-0 sets captured before the buggy start frame became public still
    # hold three files.  Validate the fourth wherever it exists so a published
    # start pair cannot be swapped without failing closed.
    if (path.parent / "buggy.before.golden.png").is_file():
        published["buggy_before"] = (
            "buggy.before.golden.png",
            "before_buggy_sha256",
            "buggy_before",
        )
    actual_ok: dict[str, bool] = {}
    for name, (filename, digest_key, frame_name) in published.items():
        image = path.parent / filename
        expected = raw.get(digest_key)
        valid = (
            image.is_file()
            and _is_sha256(expected)
            and _sha256(image) == expected
            and screenshot_quality(image).get("informative") is True
            and frame_ok.get(frame_name, False)
        )
        if not valid:
            failures.append(f"{name} SHA or image quality mismatch")
        actual_ok[name] = valid

    before_equivalent = _metric_meets(
        raw.get("before_equivalence"),
        raw.get("policy"),
        metric="before_max_changed_pixel_ratio",
        direction="max",
    )
    if not before_equivalent:
        failures.append("before equivalence evidence is invalid")
    after_differential = _metric_meets(
        raw.get("after_differential"),
        raw.get("policy"),
        metric="after_min_changed_pixel_ratio",
        direction="min",
    )
    if not after_differential:
        failures.append("after differential evidence is invalid")
    fixed_before_visual = actual_ok.get("before", False)
    buggy_before_visual = actual_ok.get(
        "buggy_before", frame_ok.get("buggy_before", False)
    )
    fixed_after_visual = actual_ok.get("fixed_after", False)
    buggy_after_visual = actual_ok.get("buggy_after", False)
    return LazarusGoldenVerification(
        task_id=normalized_task_id,
        metadata_sha256=metadata_sha,
        complete=not failures,
        fixed_before_visual=fixed_before_visual,
        buggy_before_visual=buggy_before_visual,
        before_equivalent=before_equivalent,
        fixed_after_visual=fixed_after_visual,
        buggy_after_visual=buggy_after_visual,
        failures=tuple(dict.fromkeys(failures)),
    )


def _resolve_output_dir(root: Path, value: str | Path) -> Path:
    requested = Path(value).expanduser()
    destination = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    if destination == root or root not in destination.parents:
        raise LazarusGoldenError("Lazarus golden output escapes repository root")
    return destination


def _validate_thresholds(
    *,
    ignore_top_fraction: float,
    before_max_changed_pixel_ratio: float,
    after_min_changed_pixel_ratio: float,
) -> None:
    if not 0.0 <= float(ignore_top_fraction) < 1.0:
        raise LazarusGoldenError("ignore_top_fraction must be in [0, 1)")
    if not 0.0 <= float(before_max_changed_pixel_ratio) <= 1.0:
        raise LazarusGoldenError("before_max_changed_pixel_ratio must be in [0, 1]")
    if not 0.0 < float(after_min_changed_pixel_ratio) <= 1.0:
        raise LazarusGoldenError("after_min_changed_pixel_ratio must be in (0, 1]")


def _validate_frame(name: str, frame: LazarusGoldenFrame) -> dict[str, Any]:
    if frame.expected_semantics_ok is not True:
        raise LazarusGoldenError(
            f"{name} did not reach its expected semantic state"
        )
    image = Path(frame.image).expanduser().resolve()
    report = Path(frame.report).expanduser().resolve()
    if not image.is_file():
        raise LazarusGoldenError(f"{name} screenshot does not exist: {image}")
    if not report.is_file():
        raise LazarusGoldenError(f"{name} run report does not exist: {report}")
    quality = screenshot_quality(image)
    if quality.get("informative") is not True:
        raise LazarusGoldenError(f"{name} screenshot is not informative")
    return {
        "image": image,
        "report": report,
        "image_sha256": _sha256(image),
        "report_sha256": _sha256(report),
        "quality": quality,
        "expected_semantics_ok": True,
    }


def _metadata_payload(
    *,
    task_id: str,
    frames: dict[str, dict[str, Any]],
    published: dict[str, Path],
    before_metrics: dict[str, float],
    after_metrics: dict[str, float],
    ignore_top_fraction: float,
    before_max_changed_pixel_ratio: float,
    after_min_changed_pixel_ratio: float,
) -> dict[str, Any]:
    frame_metadata = {
        name: {
            "image_sha256": value["image_sha256"],
            "report_sha256": value["report_sha256"],
            "expected_semantics_ok": value["expected_semantics_ok"],
            "quality": value["quality"],
        }
        for name, value in frames.items()
    }
    return {
        "schema": "c1-lazarus-goldens-v1",
        "schema_version": LAZARUS_GOLDEN_SCHEMA_VERSION,
        "task_id": task_id,
        "source": "materialized_fixed_buggy_runtime_capture",
        "frames": frame_metadata,
        "before_sha256": _sha256(published["before"]),
        "before_buggy_sha256": _sha256(published["buggy_before"]),
        "fixed_after_sha256": _sha256(published["fixed_after"]),
        "buggy_after_sha256": _sha256(published["buggy_after"]),
        "before_equivalence": before_metrics,
        "after_differential": after_metrics,
        "policy": {
            "ignore_top_fraction": float(ignore_top_fraction),
            "before_max_changed_pixel_ratio": float(before_max_changed_pixel_ratio),
            "after_min_changed_pixel_ratio": float(after_min_changed_pixel_ratio),
        },
        "published_unix_ns": time.time_ns(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _metric_meets(
    metrics: object,
    policy: object,
    *,
    metric: str,
    direction: str,
) -> bool:
    if not isinstance(metrics, dict) or not isinstance(policy, dict):
        return False
    value = metrics.get("changed_pixel_ratio")
    limit = policy.get(metric)
    ignored = metrics.get("ignored_top_fraction")
    configured = policy.get("ignore_top_fraction")
    if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in (value, limit, ignored, configured)):
        return False
    if not 0.0 <= float(value) <= 1.0 or float(ignored) != float(configured):
        return False
    return float(value) <= float(limit) if direction == "max" else float(value) >= float(limit)


def _verification_failure(task_id: str, failure: str) -> LazarusGoldenVerification:
    return LazarusGoldenVerification(
        task_id=task_id,
        metadata_sha256=None,
        complete=False,
        fixed_before_visual=False,
        buggy_before_visual=False,
        before_equivalent=False,
        fixed_after_visual=False,
        buggy_after_visual=False,
        failures=(failure,),
    )
