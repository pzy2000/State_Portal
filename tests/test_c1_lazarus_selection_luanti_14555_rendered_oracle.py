from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import warptest.c1_lazarus_selection.luanti_14555_rendered_oracle as oracle


def _frame(
    path: Path, *, center_changed: bool = False, volatile_changed: bool = False
) -> Path:
    image = Image.new("RGB", (100, 100), (20, 40, 60))
    pixels = image.load()
    if volatile_changed:
        for x in range(100):
            for y in list(range(10)) + list(range(80, 100)):
                pixels[x, y] = (220, 30, 90)
    if center_changed:
        for x in range(40, 60):
            for y in range(40, 60):
                pixels[x, y] = (240, 210, 60)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def test_each_run_gets_a_unique_evidence_root_without_touching_old_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    old = root / "private/luanti-14555-rendered-oracle/legacy/fixed/after.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old-rendered-evidence")

    first = oracle._new_evidence_root(root)
    second = oracle._new_evidence_root(root)

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
    assert old.read_bytes() == b"old-rendered-evidence"


def test_existing_version_evidence_is_never_deleted_or_reused(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    run_root = oracle._new_evidence_root(root)
    fixed = oracle._safe_evidence_dir(
        root,
        evidence_root=run_root,
        version="fixed",
    )
    sentinel = fixed / "repetition-1" / "after.png"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"prior-frame")

    with pytest.raises(RuntimeError, match="version already exists"):
        oracle._safe_evidence_dir(
            root,
            evidence_root=run_root,
            version="fixed",
        )

    assert sentinel.read_bytes() == b"prior-frame"


def test_stability_metrics_ignores_hud_and_bottom_hot_zone_but_keeps_target_roi(
    tmp_path: Path,
) -> None:
    first = _frame(tmp_path / "first.png")
    volatile = _frame(tmp_path / "volatile.png", volatile_changed=True)
    target = _frame(tmp_path / "target.png", center_changed=True)

    volatile_metrics = oracle._stability_metrics(first, volatile)
    target_metrics = oracle._stability_metrics(first, target)

    assert volatile_metrics["changed_pixel_ratio"] == 0.0
    assert volatile_metrics["ignored_pixels"] > 0
    assert target_metrics["changed_pixel_ratio"] > 0.0
    assert target_metrics["compared_pixels"] > 0


def test_render_metrics_ignore_only_one_lsb_backend_quantization(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    one_lsb = tmp_path / "one-lsb.png"
    two_lsb = tmp_path / "two-lsb.png"
    Image.new("RGB", (20, 20), (100, 100, 100)).save(first)
    Image.new("RGB", (20, 20), (101, 100, 100)).save(one_lsb)
    Image.new("RGB", (20, 20), (102, 100, 100)).save(two_lsb)

    assert oracle._visual_metrics(first, one_lsb)["changed_pixel_ratio"] == 0.0
    assert oracle._visual_metrics(first, two_lsb)["changed_pixel_ratio"] == 1.0
    assert oracle._stability_metrics(first, one_lsb)["changed_pixel_ratio"] == 0.0
    assert oracle._stability_metrics(first, two_lsb)["changed_pixel_ratio"] == 1.0


def test_stable_capture_records_masked_readiness_metrics_and_keeps_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _frame(tmp_path / "sample-1.png")
    second = _frame(tmp_path / "sample-2.png", volatile_changed=True)
    output = tmp_path / "report" / "before.png"
    final = tmp_path / "evidence" / "before.png"

    class FakeSession:
        report_dir = output.parent

        def __init__(self) -> None:
            self.calls = 0

        def background_request(self, kind: str, **kwargs: object) -> dict[str, object]:
            assert kind == "capture"
            destination = Path(str(kwargs["output_path"]))
            source = first if self.calls == 0 else second
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            self.calls += 1
            return {"status": "success", "accepted": True, "frame_id": self.calls}

    monkeypatch.setattr(oracle.time, "sleep", lambda _seconds: None)
    receipt = oracle._capture_stable_frame(
        FakeSession(),
        output_path=output,
        final_path=final,
        version="fixed",
        repetition=1,
        role="before",
    )

    assert output.read_bytes() == second.read_bytes()
    assert final.read_bytes() == second.read_bytes()
    assert receipt["stability_metrics"]["changed_pixel_ratio"] == 0.0


def test_semantic_readiness_polls_target_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        def semantic_snapshot(
            self, task_id: str, spec: dict[str, object], **kwargs: object
        ) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {"observed": False, "status": "failure"}
            return {
                "observed": True,
                "status": "success",
                "snapshot": {
                    "ranges": [
                        {
                            "nodes": [
                                {
                                    "pos": dict(oracle._TARGET_NODE),
                                    "name": "basenodes:stone",
                                }
                            ]
                        }
                    ]
                },
            }

    monkeypatch.setattr(oracle.time, "sleep", lambda _seconds: None)
    result = oracle._wait_for_semantic_ready(FakeSession(), timeout_s=1.0)

    assert result["status"] == "ready"
    assert result["attempts"] == 2


def test_visual_cross_matrix_rejects_non_equivalent_neutral_before_frames(
    tmp_path: Path,
) -> None:
    fixed_before = _frame(tmp_path / "fixed-before.png")
    buggy_before = _frame(
        tmp_path / "buggy-before.png",
        center_changed=True,
        volatile_changed=True,
    )
    fixed_aimed = _frame(tmp_path / "fixed-aimed.png")
    buggy_aimed = _frame(tmp_path / "buggy-aimed.png")
    fixed_after = _frame(tmp_path / "fixed-after.png")
    buggy_after = _frame(tmp_path / "buggy-after.png")

    def attempt(
        repetition: int,
        *,
        before: Path,
        aimed: Path,
        after: Path,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            payload={
                "repetition": repetition,
                "before_frame_path": str(before.relative_to(tmp_path)),
                "aimed_frame_path": str(aimed.relative_to(tmp_path)),
                "after_frame_path": str(after.relative_to(tmp_path)),
            }
        )

    with pytest.raises(RuntimeError, match="neutral before frames are not equivalent"):
        oracle._visual_cross_matrix(
            tmp_path,
            {
                "fixed": [
                    attempt(
                        1, before=fixed_before, aimed=fixed_aimed, after=fixed_after
                    )
                ],
                "buggy": [
                    attempt(
                        1, before=buggy_before, aimed=buggy_aimed, after=buggy_after
                    )
                ],
            },
        )
