from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from warptest.lazarus_goldens import (
    LazarusGoldenError,
    LazarusGoldenFrame,
    register_lazarus_golden_set,
    validate_lazarus_golden_set,
)


def _image(path: Path, *, changed: bool = False, top_only: bool = False) -> Path:
    image = Image.new("RGB", (400, 240), (45, 75, 105))
    pixels = image.load()
    for x in range(400):
        for y in range(240):
            pixels[x, y] = ((x * 17 + y * 3) % 255, (x * 7 + y * 11) % 255, (x * 5 + y * 13) % 255)
    for x in range(60, 340):
        for y in range(60, 195):
            pixels[x, y] = ((x * 3) % 255, (y * 5) % 255, (x + y) % 255)
    if changed:
        for x in range(150, 250):
            for y in range(105, 165):
                pixels[x, y] = (240, 210, 60)
    if top_only:
        for x in range(400):
            for y in range(30):
                pixels[x, y] = (220, 30, 90)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _report(path: Path, *, status: str = "success") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status}) + "\n", encoding="utf-8")
    return path


def _frame(image: Path, report: Path, *, semantic_ok: bool = True) -> LazarusGoldenFrame:
    return LazarusGoldenFrame(
        image=image,
        report=report,
        expected_semantics_ok=semantic_ok,
    )


def test_registers_a_complete_lazarus_golden_set_atomically(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    captures = tmp_path / "captures"
    fixed_before = _image(captures / "fixed-before.png")
    # Volatile server text belongs above the masked visual region, so it cannot
    # turn two identical starting worlds into different public starts.
    buggy_before = _image(captures / "buggy-before.png", top_only=True)
    fixed_after = _image(captures / "fixed-after.png")
    buggy_after = _image(captures / "buggy-after.png", changed=True)
    reports = [_report(captures / f"{name}.json") for name in ("fb", "bb", "fa", "ba")]

    result = register_lazarus_golden_set(
        task_id="lz-luanti-real-provenance",
        repository_root=repo,
        output_dir=repo / "specs/luanti/lazarus/visual",
        fixed_before=_frame(fixed_before, reports[0]),
        buggy_before=_frame(buggy_before, reports[1]),
        fixed_after=_frame(fixed_after, reports[2]),
        buggy_after=_frame(buggy_after, reports[3]),
        ignore_top_fraction=0.2,
        before_max_changed_pixel_ratio=0.001,
        after_min_changed_pixel_ratio=0.01,
    )

    assert result.before.name == "before.golden.png"
    assert result.buggy_before.name == "buggy.before.golden.png"
    assert result.fixed_after.name == "fixed.after.golden.png"
    assert result.buggy_after.name == "buggy.after.golden.png"
    assert sorted(path.name for path in result.before.parent.iterdir()) == [
        "before.golden.png",
        "buggy.after.golden.png",
        "buggy.before.golden.png",
        "fixed.after.golden.png",
        "goldens.meta.json",
    ]
    assert result.before_metrics["changed_pixel_ratio"] == 0.0
    assert result.after_metrics["changed_pixel_ratio"] > 0.01
    assert result.metadata.is_file()
    assert not (repo / "specs/luanti/lazarus/.lazarus-golden-").exists()

    metadata = json.loads(result.metadata.read_text(encoding="utf-8"))
    assert metadata["schema"] == "c1-lazarus-goldens-v1"
    assert metadata["task_id"] == "lz-luanti-real-provenance"
    assert metadata["before_buggy_sha256"] != metadata["before_sha256"]
    # The fourth public file must be the buggy start frame the equivalence
    # metric was measured on, not a second copy of the fixed start.
    assert (
        hashlib.sha256(result.buggy_before.read_bytes()).hexdigest()
        == metadata["before_buggy_sha256"]
    )
    assert result.buggy_before.read_bytes() == buggy_before.read_bytes()
    assert metadata["fixed_after_sha256"] != metadata["buggy_after_sha256"]
    assert metadata["frames"]["fixed_after"]["expected_semantics_ok"] is True
    verified = validate_lazarus_golden_set(
        result.metadata,
        task_id="lz-luanti-real-provenance",
    )
    assert verified.complete is True
    assert verified.fixed_before_visual is True
    assert verified.buggy_before_visual is True
    assert verified.before_equivalent is True
    assert verified.fixed_after_visual is True
    assert verified.buggy_after_visual is True

    original = result.buggy_before.read_bytes()
    result.buggy_before.write_bytes(_image(tmp_path / "swap.png", changed=True).read_bytes())
    swapped = validate_lazarus_golden_set(
        result.metadata,
        task_id="lz-luanti-real-provenance",
    )
    assert swapped.complete is False
    assert swapped.buggy_before_visual is False
    assert any("buggy_before SHA" in failure for failure in swapped.failures)
    result.buggy_before.write_bytes(original)

    result.fixed_after.write_bytes(b"tampered")
    tampered = validate_lazarus_golden_set(
        result.metadata,
        task_id="lz-luanti-real-provenance",
    )
    assert tampered.complete is False
    assert any("fixed_after SHA" in failure for failure in tampered.failures)


def test_golden_registration_rejects_non_equivalent_start_or_missing_semantics(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    captures = tmp_path / "captures"
    before = _image(captures / "before.png")
    different_before = _image(captures / "different-before.png", changed=True)
    after = _image(captures / "after.png")
    changed_after = _image(captures / "changed-after.png", changed=True)
    reports = [_report(captures / f"{index}.json") for index in range(4)]

    with pytest.raises(LazarusGoldenError, match="before frames are not equivalent"):
        register_lazarus_golden_set(
            task_id="lz-luanti-real-provenance",
            repository_root=repo,
            output_dir=repo / "visual-a",
            fixed_before=_frame(before, reports[0]),
            buggy_before=_frame(different_before, reports[1]),
            fixed_after=_frame(after, reports[2]),
            buggy_after=_frame(changed_after, reports[3]),
            after_min_changed_pixel_ratio=0.01,
        )

    with pytest.raises(LazarusGoldenError, match="expected semantic state"):
        register_lazarus_golden_set(
            task_id="lz-luanti-real-provenance",
            repository_root=repo,
            output_dir=repo / "visual-b",
            fixed_before=_frame(before, reports[0], semantic_ok=False),
            buggy_before=_frame(before, reports[1]),
            fixed_after=_frame(after, reports[2]),
            buggy_after=_frame(changed_after, reports[3]),
        )


def test_golden_registration_rejects_invisible_after_pair_and_path_escape(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    captures = tmp_path / "captures"
    before = _image(captures / "before.png")
    after = _image(captures / "after.png")
    reports = [_report(captures / f"{index}.json") for index in range(4)]

    with pytest.raises(LazarusGoldenError, match="after frames do not expose"):
        register_lazarus_golden_set(
            task_id="lz-luanti-real-provenance",
            repository_root=repo,
            output_dir=repo / "visual-a",
            fixed_before=_frame(before, reports[0]),
            buggy_before=_frame(before, reports[1]),
            fixed_after=_frame(after, reports[2]),
            buggy_after=_frame(after, reports[3]),
        )
    with pytest.raises(LazarusGoldenError, match="escapes repository root"):
        register_lazarus_golden_set(
            task_id="lz-luanti-real-provenance",
            repository_root=repo,
            output_dir=tmp_path / "outside",
            fixed_before=_frame(before, reports[0]),
            buggy_before=_frame(before, reports[1]),
            fixed_after=_frame(after, reports[2]),
            buggy_after=_frame(_image(captures / "changed.png", changed=True), reports[3]),
        )
