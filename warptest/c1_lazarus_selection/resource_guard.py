"""Disk watermarks for bounded C1-Lazarus execution."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


GIB = 1024**3


@dataclass(frozen=True)
class DiskCapacity:
    free_bytes: int
    may_start_build: bool
    may_start_candidate: bool
    may_write: bool


def disk_capacity(path: str | Path) -> DiskCapacity:
    target = Path(path).absolute()
    while not target.exists():
        parent = target.parent
        if parent == target:
            raise RuntimeError("C1-Lazarus disk guard has no existing ancestor")
        target = parent
    free = shutil.disk_usage(target).free
    return DiskCapacity(
        free_bytes=free,
        may_start_build=free >= 50 * GIB,
        may_start_candidate=free >= 40 * GIB,
        may_write=free >= 30 * GIB,
    )


def require_write_capacity(path: str | Path) -> DiskCapacity:
    capacity = disk_capacity(path)
    if not capacity.may_write:
        raise RuntimeError("C1-Lazarus disk guard prohibits new writes")
    return capacity


__all__ = ["DiskCapacity", "disk_capacity", "require_write_capacity"]
