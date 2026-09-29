from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image

from warptest.lazarus_goldens import LazarusGoldenFrame
from warptest.lazarus_luanti_goldens import capture_luanti_pr11919_goldens
from warptest.lazarus_versions import (
    LazarusVersionArtifact,
    LazarusVersionPair,
)


def _artifact(tmp_path: Path, version: str) -> LazarusVersionArtifact:
    root = tmp_path / version
    root.mkdir()
    metadata = tmp_path / f"{version}.git"
    metadata.mkdir()
    return LazarusVersionArtifact(
        task_id="lz-luanti-11919-vector-from-string",
        version=version,
        root=root,
        git_metadata=metadata,
        source_revision="a" * 40,
        patch_sha256="b" * 64,
        tree_sha256=("c" if version == "fixed" else "d") * 64,
        changed_files=() if version == "fixed" else ("builtin/common/vector.lua",),
        build_identity={"status": "not_required"},
        cache_hit=True,
    )


def _image(path: Path, *, changed: bool = False) -> Path:
    image = Image.new("RGB", (400, 240))
    pixels = image.load()
    for x in range(400):
        for y in range(240):
            pixels[x, y] = ((x * 17 + y * 3) % 255, (x * 7 + y * 11) % 255, (x * 5 + y * 13) % 255)
    if changed:
        for x in range(150, 250):
            for y in range(105, 165):
                pixels[x, y] = (240, 210, 60)
    image.save(path)
    return path


def test_luanti_golden_capture_uses_start_goal_and_signature_projections(
    tmp_path: Path,
) -> None:
    pair = LazarusVersionPair(
        buggy=_artifact(tmp_path, "buggy"),
        fixed=_artifact(tmp_path, "fixed"),
        version_switch_ms=1.0,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[str, str, dict]] = []
    observed_capture_frames: list[str | None] = []

    def runner(artifact, stage: str, spec: dict, work_dir: Path) -> LazarusGoldenFrame:
        calls.append((artifact.version, stage, spec))
        observed_capture_frames.append(os.environ.get("WARPTEST_LUANTI_SCREENSHOT_FRAME"))
        image = _image(
            work_dir / f"{artifact.version}-{stage}.png",
            changed=artifact.version == "buggy" and stage == "after",
        )
        report = work_dir / f"{artifact.version}-{stage}.json"
        report.write_text(json.dumps({"status": "success"}) + "\n", encoding="utf-8")
        return LazarusGoldenFrame(
            image=image,
            report=report,
            expected_semantics_ok=True,
        )

    result = capture_luanti_pr11919_goldens(
        pair,
        repository_root=repo,
        output_dir=repo / "specs/luanti/lazarus/visual",
        sandbox_root=tmp_path / "sandbox",
        frame_runner=runner,
    )

    assert [(version, stage) for version, stage, _ in calls] == [
        ("fixed", "before"),
        ("buggy", "before"),
        ("fixed", "after"),
        ("buggy", "after"),
    ]
    assert calls[0][2]["actions"] == []
    assert calls[0][2]["target"]["player"] == {
        "pos": {"x": 0, "y": 3, "z": 6},
        "hover": True,
        "look_at": {"x": 0, "y": 1, "z": 0},
    }
    assert calls[1][2]["assertions"] == [
        {
            "type": "luanti_player_view_matches",
            "pos": {"x": 0, "y": 3, "z": 6},
            "look_at": {"x": 0, "y": 1, "z": 0},
            "radius": 0.1,
            "angle_tolerance_degrees": 1.0,
        },
        # The start run records the mod-error invariant so the gate can grade it
        # on the start frame instead of assuming it held.
        {"type": "no_luanti_mod_errors"},
    ]
    assert calls[2][2]["actions"][-1]["type"] == "luanti_set_player_pos"
    assert calls[3][2]["actions"][-1]["look_at"] == {"x": 0, "y": 1, "z": 0}
    assert calls[2][2]["assertions"][0]["expected"] == "basenodes:dirt_with_grass"
    assert calls[3][2]["assertions"][0]["expected"] == "basenodes:desert_sand"
    assert observed_capture_frames == ["1000", "1000", "1000", "1000"]
    assert os.environ.get("WARPTEST_LUANTI_SCREENSHOT_FRAME") is None
    assert result.before.is_file()
    assert result.fixed_after.is_file()
    assert result.buggy_after.is_file()
