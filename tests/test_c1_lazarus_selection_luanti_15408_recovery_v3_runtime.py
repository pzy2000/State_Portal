from pathlib import Path

from warptest.c1_lazarus_selection.luanti_15408_recovery_v3_runtime import (
    _changed_pixel_ratio,
    _parse_summary,
    _render_frame_bytes,
)
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt


def test_native_summary_requires_one_completed_unambiguous_module() -> None:
    passed = NativeProcessReceipt(
        "completed",
        0,
        b"",
        b"Unit Test Results: PASSED\n    0 / 2 failed tests.\n",
    )
    failed = NativeProcessReceipt(
        "completed",
        1,
        b"",
        b"Unit Test Results: FAILED\n    1 / 2 failed tests.\n",
    )
    assert _parse_summary(passed) == {
        "observed": True,
        "summary_count": 1,
        "result": "pass",
        "failed_tests": 0,
        "total_tests": 2,
    }
    assert _parse_summary(failed)["result"] == "failure"
    duplicate = NativeProcessReceipt(
        "completed",
        0,
        passed.stderr,
        passed.stderr,
    )
    assert _parse_summary(duplicate)["result"] == "unknown"


def test_semantic_renderings_are_deterministic_but_not_equal(tmp_path: Path) -> None:
    ready = _render_frame_bytes(
        role="REGISTERED START",
        result="ready",
        details=["selector=TestCollision"],
    )
    assert ready == _render_frame_bytes(
        role="REGISTERED START",
        result="ready",
        details=["selector=TestCollision"],
    )
    fixed = _render_frame_bytes(
        role="FIXED AFTER",
        result="pass",
        details=["returncode=0"],
    )
    buggy = _render_frame_bytes(
        role="BUGGY AFTER",
        result="failure",
        details=["returncode=1"],
    )
    before = tmp_path / "before.png"
    fixed_path = tmp_path / "fixed.png"
    buggy_path = tmp_path / "buggy.png"
    before.write_bytes(ready)
    fixed_path.write_bytes(fixed)
    buggy_path.write_bytes(buggy)
    assert _changed_pixel_ratio(before, before) == 0.0
    assert _changed_pixel_ratio(fixed_path, buggy_path) > 0.01

