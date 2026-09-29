from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _packaging_python() -> str:
    """Return a local interpreter that can build a wheel without network access."""

    if importlib.util.find_spec("setuptools") is not None:
        return sys.executable
    fallback = shutil.which("python3.11")
    if fallback is not None:
        return fallback
    pytest.skip("a local setuptools-enabled Python interpreter is required")


def test_openra_lazarus_overlays_are_present_in_a_built_wheel(tmp_path: Path) -> None:
    """Installed Phase-0 probes resolve their overlays relative to ``__file__``."""

    source = tmp_path / "source"
    package = source / "warptest"
    package.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "warptest" / "__init__.py", package / "__init__.py")
    shutil.copytree(
        ROOT / "warptest" / "instrumentation" / "openra",
        package / "instrumentation" / "openra",
    )
    wheel_dir = tmp_path / "wheels"
    completed = subprocess.run(
        [
            _packaging_python(),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel = next(wheel_dir.glob("warptest-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert {
        "warptest/instrumentation/openra/lazarus/"
        "lz-openra-21453-hotkey-ready-building/WarptestReadyBuildingAudit.cs",
        "warptest/instrumentation/openra/lazarus/"
        "lz-openra-21453-hotkey-ready-building/allies-08a-map.patch",
        "warptest/instrumentation/openra/lazarus/"
        "lz-openra-21453-hotkey-ready-building/allies-08a-rules.patch",
    } <= names
