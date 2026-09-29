from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from warptest.lazarus_build import create_prebuild_callback


def test_prebuild_callback_runs_argv_and_binds_declared_artifacts(
    tmp_path: Path,
) -> None:
    callback = create_prebuild_callback(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('build/app.bin').parent.mkdir(); "
            "Path('build/app.bin').write_bytes(b'fixed-binary')",
        ],
        artifact_paths=("build/app.bin",),
        timeout_s=30,
    )

    result = callback("fixed", tmp_path)

    expected = hashlib.sha256(b"fixed-binary").hexdigest()
    assert result["status"] == "passed"
    assert result["version"] == "fixed"
    assert result["artifacts"] == {
        "build/app.bin": {"kind": "file", "sha256": expected, "size": 12}
    }
    assert len(result["stdout_sha256"]) == 64
    assert len(result["stderr_sha256"]) == 64


def test_prebuild_callback_rejects_missing_or_escaping_artifact(tmp_path: Path) -> None:
    missing = create_prebuild_callback(
        [sys.executable, "-c", "pass"],
        artifact_paths=("build/missing.bin",),
        timeout_s=30,
    )
    with pytest.raises(RuntimeError, match="missing"):
        missing("buggy", tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        create_prebuild_callback(
            [sys.executable, "-c", "pass"],
            artifact_paths=("../outside",),
            timeout_s=30,
        )


def test_prebuild_callback_fails_closed_on_command_failure(tmp_path: Path) -> None:
    callback = create_prebuild_callback(
        [sys.executable, "-c", "raise SystemExit(7)"],
        artifact_paths=(),
        timeout_s=30,
    )
    with pytest.raises(RuntimeError, match="exit 7"):
        callback("fixed", tmp_path)


def test_prebuild_callback_runs_registered_clean_step_before_build(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "build" / "stale.bin"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")
    callback = create_prebuild_callback(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "assert not Path('build/stale.bin').exists(); "
            "Path('build/app.bin').write_bytes(b'fresh-binary')",
        ],
        pre_commands=(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('build/stale.bin').unlink()",
            ),
        ),
        artifact_paths=("build/app.bin",),
        timeout_s=30,
    )

    result = callback("buggy", tmp_path)

    assert result["pre_commands"] == [
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('build/stale.bin').unlink()",
        ]
    ]
    assert (
        result["artifacts"]["build/app.bin"]["sha256"]
        == hashlib.sha256(b"fresh-binary").hexdigest()
    )


def test_prebuild_callback_runs_registered_post_step_before_collecting_artifacts(
    tmp_path: Path,
) -> None:
    callback = create_prebuild_callback(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('bin').mkdir(); "
            "Path('bin/app.bin').write_bytes(b'fresh-binary')",
        ],
        post_commands=(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; src=Path('bin/app.bin'); "
                "dst=Path('build/bin/app.bin'); dst.parent.mkdir(parents=True); "
                "dst.write_bytes(src.read_bytes())",
            ),
        ),
        artifact_paths=("build/bin/app.bin",),
        timeout_s=30,
    )

    result = callback("fixed", tmp_path)

    assert result["post_commands"] == [
        [
            sys.executable,
            "-c",
            "from pathlib import Path; src=Path('bin/app.bin'); "
            "dst=Path('build/bin/app.bin'); dst.parent.mkdir(parents=True); "
            "dst.write_bytes(src.read_bytes())",
        ]
    ]
    assert (
        result["artifacts"]["build/bin/app.bin"]["sha256"]
        == hashlib.sha256(b"fresh-binary").hexdigest()
    )
