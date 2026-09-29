from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from warptest.lazarus_goldens import LazarusGoldenFrame
from warptest.lazarus_openra_hotkey_goldens import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    OpenRAHotkeyGoldenCapture,
    capture_openra_pr21453_goldens,
)
from warptest.lazarus_versions import LazarusVersionArtifact, LazarusVersionPair


def _artifact(tmp_path: Path, version: str) -> LazarusVersionArtifact:
    root = tmp_path / version
    root.mkdir()
    metadata = tmp_path / f"{version}.git"
    metadata.mkdir()
    return LazarusVersionArtifact(
        task_id=OPENRA_HOTKEY_PHASE0_TASK_ID,
        version=version,
        root=root,
        git_metadata=metadata,
        source_revision="a" * 40,
        patch_sha256="b" * 64,
        tree_sha256=("c" if version == "fixed" else "d") * 64,
        changed_files=("OpenRA.Mods.Common/Widgets/ProductionPaletteWidget.cs",),
        build_identity={"status": "success"},
        cache_hit=True,
    )


def _image(path: Path, *, changed: bool = False) -> Path:
    image = Image.new("RGB", (400, 240))
    for x in range(400):
        for y in range(240):
            image.putpixel(
                (x, y),
                ((x * 17 + y * 3) % 255, (x * 7 + y * 11) % 255, (x * 5 + y * 13) % 255),
            )
    if changed:
        for x in range(120, 280):
            for y in range(80, 180):
                image.putpixel((x, y), (240, 210, 60))
    image.save(path)
    return path


def test_openra_golden_capture_binds_one_real_before_and_dual_after_states(
    tmp_path: Path,
) -> None:
    pair = LazarusVersionPair(
        buggy=_artifact(tmp_path, "buggy"),
        fixed=_artifact(tmp_path, "fixed"),
        version_switch_ms=1.0,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[str] = []

    def runner(artifact: LazarusVersionArtifact, work_dir: Path) -> OpenRAHotkeyGoldenCapture:
        calls.append(artifact.version)
        before = _image(work_dir / f"{artifact.version}-before.png")
        after = _image(
            work_dir / f"{artifact.version}-after.png",
            changed=artifact.version == "buggy",
        )
        report = work_dir / f"{artifact.version}.json"
        report.write_text(json.dumps({"version": artifact.version}) + "\n", encoding="utf-8")
        return OpenRAHotkeyGoldenCapture(
            before=LazarusGoldenFrame(before, report, True),
            after=LazarusGoldenFrame(after, report, True),
        )

    result = capture_openra_pr21453_goldens(
        pair,
        repository_root=repo,
        output_dir=repo / "specs/openra/lazarus/visual",
        sandbox_root=tmp_path / "sandbox",
        frame_runner=runner,
    )

    assert calls == ["fixed", "buggy"]
    assert result.before.is_file()
    assert result.fixed_after.is_file()
    assert result.buggy_after.is_file()
    assert result.before_metrics["changed_pixel_ratio"] == 0.0
    assert result.after_metrics["changed_pixel_ratio"] > 0.1
