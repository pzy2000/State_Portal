from __future__ import annotations

from pathlib import Path

from PIL import Image

from warptest.c1_background_compatibility import background_pixel_roundtrip


class _Runtime:
    session_id = "session-compat"
    capture_backend = "fixture_framebuffer_v1"
    input_backend = "fixture_input_v1"
    width = 1280
    height = 720

    def __init__(self) -> None:
        self.last_policy_frame_id = 0
        self.batches = []

    def capture_policy_frame(self, path: Path) -> str:
        self.last_policy_frame_id += 1
        Image.new("RGB", (self.width, self.height), (20, 40, 80)).save(path)
        return str(path)

    def dispatch_input_batch(self, batch):
        self.batches.append(batch)
        return {
            "batch_id": batch.batch_id,
            "accepted": True,
            "event_count": 0,
            "resulting_frame_id": batch.frame_id,
            "error": "",
        }


def test_zero_model_roundtrip_binds_session_frame_and_receipt(tmp_path: Path) -> None:
    runtime = _Runtime()
    result = background_pixel_roundtrip(
        runtime,
        tmp_path,
        image_evidence=lambda value: {"path": str(value), "informative": True},
    )

    assert result["passed"] is True
    assert result["execution_backend"] == "background_pixel_v1"
    assert result["foreground_required"] is False
    assert result["frame_id"] == 1
    assert result["resulting_frame_id"] == 2
    assert result["receipt"]["event_count"] == 0
    assert len(runtime.batches) == 1
