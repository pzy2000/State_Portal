from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest

from warptest.c1_lazarus_selection.luanti_13773_native_candidate import (
    _run_full_process,
)


def test_diagnostic_process_persists_complete_stdout_and_stderr(tmp_path: Path) -> None:
    result = _run_full_process(
        (
            sys.executable,
            "-c",
            "import sys; print('diagnostic-out'); print('diagnostic-err', file=sys.stderr)",
        ),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        timeout_seconds=10,
        log_relative=PurePosixPath("probe"),
        root=tmp_path,
    )

    assert result.outcome == "completed"
    assert result.returncode == 0
    assert result.stdout_path.read_text(encoding="utf-8") == "diagnostic-out\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "diagnostic-err\n"
    assert result.stdout_bytes == result.stdout_path.stat().st_size
    assert result.stderr_bytes == result.stderr_path.stat().st_size


def test_diagnostic_process_refuses_log_namespace_reuse(tmp_path: Path) -> None:
    kwargs = {
        "argv": (sys.executable, "-c", "print('once')"),
        "cwd": tmp_path,
        "env": {"PATH": os.environ["PATH"]},
        "timeout_seconds": 10,
        "log_relative": PurePosixPath("probe"),
        "root": tmp_path,
    }
    _run_full_process(**kwargs)
    with pytest.raises(RuntimeError, match="namespace already exists"):
        _run_full_process(**kwargs)
