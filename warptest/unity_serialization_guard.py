"""Conservative cleanup for deterministic Unity editor serialization noise.

Unity may normalize a handful of floating-point literals while opening the
jynew project.  C1 provenance must still fail closed for every other edit, so
the guard captures the exact pre-launch bytes and restores them only when the
post-launch file is byte-for-byte equal to the known normalized form.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


_JYNEW_SERIALIZATION_NOISE = {
    "Assets/3DScene/Model/BigMapNew/Materials/water-Jyx2.mat": (
        (b"0.050976038", b"0.050976016"),
        (b"0.05489698", b"0.05489696"),
        (b"0.20390114", b"0.20390111"),
    ),
    "Assets/3DScene/Model/Common/Water/Materials/JYX2_water_1.mat": (
        (b"0.411482", b"0.41148198"),
        (b"0.3969766", b"0.39697656"),
    ),
}


def capture_jynew_serialization_guard(
    project_path: str | Path,
) -> dict[Path, tuple[bytes, bytes]]:
    """Capture original and exactly-known normalized bytes before Unity starts."""

    project = Path(project_path).expanduser().resolve()
    guard: dict[Path, tuple[bytes, bytes]] = {}
    for relative, replacements in _JYNEW_SERIALIZATION_NOISE.items():
        path = (project / relative).resolve()
        if not path.is_file():
            continue
        original = path.read_bytes()
        normalized = original
        for before, after in replacements:
            normalized = normalized.replace(before, after)
        guard[path] = (original, normalized)
    return guard


def restore_jynew_serialization_guard(
    guard: dict[Path, tuple[bytes, bytes]],
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Restore only an exact known normalization; preserve all other drift."""

    emit = log or (lambda _message: None)
    for path, (original, normalized) in guard.items():
        if not path.is_file():
            emit(f"serialization_guard_missing={path}")
            continue
        current = path.read_bytes()
        if current == original:
            continue
        if current != normalized:
            emit(f"serialization_guard_unknown_drift={path}")
            continue
        temporary = path.with_suffix(path.suffix + ".warptest-restore")
        temporary.write_bytes(original)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
        emit(f"serialization_guard_restored={path}")
