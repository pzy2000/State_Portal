"""Mechanical unified-patch inventory for source screening."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from .screening import PatchDecision


_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".rs",
        ".go",
        ".cs",
        ".lua",
        ".glsl",
        ".vert",
        ".frag",
    }
)
_GENERIC_TOKENS = frozenset(
    {"src", "source", "test", "tests", "spec", "main", "unit", "integration"}
)
# The strict track grades from a test assertion, so a stylesheet is not repair
# material there.  The P2P-only track grades from a rendered frame, where a
# stylesheet carries the defect as directly as a source file does.
_STYLE_SUFFIXES = frozenset({".css", ".scss", ".sass", ".less"})
# OpenRA paints chrome from MiniYAML.  Counting those files here, and not in
# `_SOURCE_SUFFIXES`, keeps the strict track from treating a rules.yaml edit as
# a production repair.
_LAYOUT_SUFFIXES = frozenset({".yaml", ".yml"})
_LAYOUT_MARKERS = ("/chrome/", "/widgets/", "/chrome.yaml")


@dataclass
class _Entry:
    old_path: str | None
    new_path: str | None
    binary: bool = False


def classify_unified_patch(content: bytes) -> PatchDecision:
    """Partition changed paths and require a related source/test pair."""

    if type(content) is not bytes or not content:
        return PatchDecision.rejected("patch_missing")
    text = content.decode("utf-8", errors="surrogateescape")
    entries = _entries(text)
    paths = tuple(
        sorted(
            {
                entry.new_path or entry.old_path
                for entry in entries
                if entry.new_path or entry.old_path
            }
        )
    )
    production = tuple(path for path in paths if _is_production(path))
    tests = tuple(path for path in paths if _is_test(path))
    if not production:
        return PatchDecision.rejected("no_production_repair")
    if not tests:
        return PatchDecision.rejected("no_regression_test")
    if not any(_related(source, test) for source in production for test in tests):
        return PatchDecision.rejected("regression_test_not_relevant")
    return PatchDecision.eligible(
        production_paths=production,
        test_paths=tests,
    )


def is_visual_production_path(path: str) -> bool:
    """Return whether one path is repair material for a rendered differential."""

    return _is_production(path) or _is_style(path) or _is_layout(path)


def classify_production_only_patch(content: bytes) -> PatchDecision:
    """Partition changed paths and require a repair that ships no test.

    This is the mechanical half of Gate 5 in the P2P-only track: the change must
    repair production material and must not add a test that could assert the
    defect.  A change that does carry such a test belongs to the strict track, so
    it is rejected here rather than reclassified.
    """

    if type(content) is not bytes or not content:
        return PatchDecision.rejected("patch_missing")
    text = content.decode("utf-8", errors="surrogateescape")
    entries = _entries(text)
    paths = tuple(
        sorted(
            {
                entry.new_path or entry.old_path
                for entry in entries
                if entry.new_path or entry.old_path
            }
        )
    )
    production = tuple(path for path in paths if is_visual_production_path(path))
    tests = tuple(path for path in paths if _is_test(path))
    if not production:
        return PatchDecision.rejected("no_production_repair")
    if tests:
        return PatchDecision.rejected("patch_carries_test_material")
    return PatchDecision.production_only(production_paths=production)


def patch_test_paths(content: bytes) -> tuple[str, ...]:
    """Return the test paths a unified patch touches, for Gate 5 evidence."""

    if type(content) is not bytes or not content:
        return ()
    entries = _entries(content.decode("utf-8", errors="surrogateescape"))
    return tuple(
        sorted(
            {
                path
                for entry in entries
                if (path := entry.new_path or entry.old_path) and _is_test(path)
            }
        )
    )


def _entries(text: str) -> tuple[_Entry, ...]:
    result: list[_Entry] = []
    current: _Entry | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                result.append(current)
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = []
            if len(parts) != 4:
                current = _Entry(None, None)
            else:
                current = _Entry(_path(parts[2]), _path(parts[3]))
            continue
        if current is None:
            continue
        if line.startswith("rename from "):
            current.old_path = _path(line.removeprefix("rename from "))
        elif line.startswith("rename to "):
            current.new_path = _path(line.removeprefix("rename to "))
        elif line.startswith("--- "):
            current.old_path = _path(_header_path(line[4:]))
        elif line.startswith("+++ "):
            current.new_path = _path(_header_path(line[4:]))
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
    if current is not None:
        result.append(current)
    return tuple(result)


def _header_path(value: str) -> str:
    try:
        parts = shlex.split(value)
    except ValueError:
        return value.split("\t", 1)[0]
    return parts[0] if parts else value


def _path(value: str) -> str | None:
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or value != pure.as_posix():
        raise ValueError("patch path is unsafe")
    return value


def _is_test(path: str) -> bool:
    folded = path.casefold()
    parts = PurePosixPath(folded).parts
    name = parts[-1]
    return bool(
        any(part in {"test", "tests", "testing", "spec", "specs"} for part in parts)
        or name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def _is_style(path: str) -> bool:
    pure = PurePosixPath(path.casefold())
    if _is_test(path):
        return False
    if any(
        part in {"docs", "doc", "examples", "assets", "vendor"} for part in pure.parts
    ):
        return False
    return pure.suffix in _STYLE_SUFFIXES


def _is_layout(path: str) -> bool:
    """Return whether a MiniYAML chrome/widget file is painted layout."""

    folded = path.casefold()
    if _is_test(path):
        return False
    pure = PurePosixPath(folded)
    if any(
        part in {"docs", "doc", "examples", "assets", "vendor"} for part in pure.parts
    ):
        return False
    if pure.suffix not in _LAYOUT_SUFFIXES:
        return False
    return any(marker in folded for marker in _LAYOUT_MARKERS)


def _is_production(path: str) -> bool:
    pure = PurePosixPath(path.casefold())
    if _is_test(path):
        return False
    if any(
        part in {"docs", "doc", "examples", "assets", "vendor"} for part in pure.parts
    ):
        return False
    return pure.suffix in _SOURCE_SUFFIXES


def _related(source: str, test: str) -> bool:
    source_tokens = _tokens(source)
    test_tokens = _tokens(test)
    return bool(source_tokens & test_tokens)


def _tokens(path: str) -> frozenset[str]:
    values = {
        value
        for value in re.split(r"[^a-z0-9]+", path.casefold())
        if len(value) >= 3 and value not in _GENERIC_TOKENS
    }
    return frozenset(values)


__all__ = [
    "classify_production_only_patch",
    "classify_unified_patch",
    "is_visual_production_path",
    "patch_test_paths",
]
