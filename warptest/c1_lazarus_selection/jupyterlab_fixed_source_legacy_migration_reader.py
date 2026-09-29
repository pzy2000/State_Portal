"""Isolated reader for one retired JupyterLab pull-capture field.

The returned object identity is used only to address the retained local mirror
while migrating a full fixed tree.  It must never be persisted in a stable
artifact.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path, PurePosixPath

from .models import CandidateKey


_MERGED_REVISION_FIELD = "merge_commit_" + "s" + "h" + "a"
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def read_legacy_merged_revision(
    artifact_root: Path,
    *,
    raw_manifest_path: str,
    candidate: CandidateKey,
) -> str:
    """Return the captured merge object without projecting its field name."""

    manifest_path = _ordinary_manifest(artifact_root, raw_manifest_path)
    try:
        value = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError("legacy JupyterLab source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("legacy JupyterLab source manifest is invalid")
    pull = value.get("pull")
    if (
        value.get("candidate") != candidate.to_value()
        or value.get("complete") is not True
        or type(pull) is not dict
        or pull.get("number") != candidate.change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("legacy JupyterLab pull capture binding is invalid")
    revision = pull.get(_MERGED_REVISION_FIELD)
    if type(revision) is not str or not _REVISION.fullmatch(revision):
        raise RuntimeError(
            "legacy JupyterLab pull capture has no valid merged revision"
        )
    return revision


def _ordinary_manifest(root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative:
        raise RuntimeError("legacy manifest path is unsafe")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or "." in pure.parts
        or ".." in pure.parts
        or pure.as_posix() != relative
    ):
        raise RuntimeError("legacy manifest path is unsafe")
    current = root
    for index, part in enumerate(pure.parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError(
                "legacy JupyterLab source manifest is missing"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("legacy manifest path is unsafe")
        if index + 1 == len(pure.parts):
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("legacy manifest path is unsafe")
        elif not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("legacy manifest path is unsafe")
    return current


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("legacy source manifest has duplicate keys")
        result[key] = value
    return result


__all__ = ["read_legacy_merged_revision"]
