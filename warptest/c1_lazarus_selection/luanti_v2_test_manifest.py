"""Conservative Luanti v2 test-manifest classification.

This module is intentionally pure: it only decides whether a patch contains
enough test-side evidence to proceed to execution.  An execution candidate is
not a qualified benchmark task; replay and the remaining gates still decide
that separately.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal


_EXECUTABLE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".lua"})
_ASSERTION = re.compile(
    r"\b(?:assert|U?ASSERT(?:[A-Z_]+)?|EXPECT_[A-Z_]+|CHECK|TESTEQUAL|TESTNOTNULL|"
    r"TESTNULL)\s*\("
)
_TEST_REGISTRATION = re.compile(r"\b(?:TEST|TEST_CASE|REGISTER_TEST)\s*\(")
_LUA_ASSERTION = re.compile(r"\bassert(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*\(")
_LUA_SPEC_REGISTRATION = re.compile(r"\bit\s*\(")
_LUA_FUNCTION_DEFINITION = re.compile(
    r"^\s*(?:local\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)"
)
_LUA_DIRECT_INVOCATION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*;?\s*$")
_CONDITIONAL_GUARD = re.compile(r"^\s*#\s*(?:if|ifdef|ifndef)\b")
_INCLUDE = re.compile(r"^\s*#\s*include\b")
_COMMENT = re.compile(r"^\s*(?://|/\*|\*|\*/|--)")
_BRACES_ONLY = re.compile(r"^[\s{};]+$")


@dataclass(frozen=True)
class LuantiV2TestManifestDecision:
    """A conservative admission decision for the execution stage only."""

    state: Literal["execution_candidate", "rejected"]
    reason: str | None
    evidence_kind: (
        Literal[
            "added_assertion",
            "enabled_existing_guarded_test",
            "expanded_regression_corpus",
        ]
        | None
    )
    executable_test_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state == "execution_candidate":
            if self.reason is not None or self.evidence_kind is None:
                raise ValueError("execution candidate decision is incomplete")
            if not self.executable_test_paths:
                raise ValueError("execution candidate has no executable test path")
            return
        if self.state == "rejected":
            if self.reason is None or self.evidence_kind is not None:
                raise ValueError("rejected decision is incomplete")
            if self.executable_test_paths:
                raise ValueError("rejected decision carries executable tests")
            return
        raise ValueError("Luanti v2 test-manifest decision state is invalid")

    @classmethod
    def execution_candidate(
        cls,
        *,
        evidence_kind: Literal[
            "added_assertion",
            "enabled_existing_guarded_test",
            "expanded_regression_corpus",
        ],
        executable_test_paths: tuple[str, ...],
    ) -> LuantiV2TestManifestDecision:
        return cls(
            "execution_candidate",
            None,
            evidence_kind,
            executable_test_paths,
        )

    @classmethod
    def rejected(cls, reason: str) -> LuantiV2TestManifestDecision:
        return cls("rejected", reason, None, ())


@dataclass(frozen=True)
class _Section:
    path: str
    additions: tuple[str, ...]
    removals: tuple[str, ...]
    context: tuple[str, ...]


def classify_luanti_v2_test_manifest(
    content: bytes,
    *,
    declared_test_paths: tuple[str, ...],
    candidate_change_number: int | None = None,
) -> LuantiV2TestManifestDecision:
    """Find test evidence worth executing for a Luanti v2 candidate.

    The positive cases are deliberately narrow: an added assertion, or the
    removal of a conditional guard around an already registered test.  All
    other test-side source changes are rejected before build/materialization.
    """

    if type(content) is not bytes or not content:
        return LuantiV2TestManifestDecision.rejected("patch_missing")
    test_paths = _declared_paths(declared_test_paths)
    if test_paths is None:
        return LuantiV2TestManifestDecision.rejected("test_inventory_invalid")
    sections = _sections(content)
    relevant = tuple(section for section in sections if section.path in test_paths)
    if not relevant:
        return LuantiV2TestManifestDecision.rejected("declared_test_changes_missing")

    executable_paths = tuple(
        sorted(
            section.path
            for section in relevant
            if _is_executable_test_path(section.path)
        )
    )
    additions = tuple(line for section in relevant for line in section.additions)
    if executable_paths and any(
        _has_added_non_lua_assertion(section) or _has_lua_execution_regression(section)
        for section in relevant
    ):
        return LuantiV2TestManifestDecision.execution_candidate(
            evidence_kind="added_assertion",
            executable_test_paths=executable_paths,
        )
    if executable_paths and any(_enables_guarded_test(section) for section in relevant):
        return LuantiV2TestManifestDecision.execution_candidate(
            evidence_kind="enabled_existing_guarded_test",
            executable_test_paths=executable_paths,
        )
    if (
        candidate_change_number == 14555
        and executable_paths
        and _is_14555_half_boundary_regression(sections, test_paths)
    ):
        return LuantiV2TestManifestDecision.execution_candidate(
            evidence_kind="expanded_regression_corpus",
            executable_test_paths=executable_paths,
        )

    substantive_additions = tuple(line for line in additions if _is_substantive(line))
    if not substantive_additions:
        return LuantiV2TestManifestDecision.rejected("deletion_only")
    if all(_INCLUDE.match(line) for line in substantive_additions):
        return LuantiV2TestManifestDecision.rejected("include_only")
    if _is_formatting_only(relevant):
        return LuantiV2TestManifestDecision.rejected("formatting_only")
    if _is_test_registration_without_regression(relevant, substantive_additions):
        return LuantiV2TestManifestDecision.rejected(
            "test_registration_without_regression"
        )
    return LuantiV2TestManifestDecision.rejected("new_api_compile_only")


def _is_14555_half_boundary_regression(
    sections: tuple[_Section, ...],
    declared_test_paths: frozenset[str],
) -> bool:
    """Recognize only PR 14555's boundary-value expansion of an existing test.

    The regression adds no assertion because ``testVoxelLineIterator`` already
    asserts the iterator's end position and node count for every line in its
    corpus.  The new half-grid loops add the exact previously-excluded boundary
    values to that corpus.  Admission is tied to the two-file production/test
    patch shape so a generic source refactor cannot inherit this exception.
    """

    test_path = "src/unittest/test_voxelalgorithms.cpp"
    production_path = "src/voxelalgorithms.cpp"
    if declared_test_paths != frozenset({test_path}):
        return False
    by_path = {section.path: section for section in sections}
    if set(by_path) != {test_path, production_path}:
        return False
    test = by_path[test_path]
    production = by_path[production_path]
    required_test_additions = (
        "for (f32 x = -3.0f; x < 3.1f; x += 0.5f)",
        "for (f32 y = -3.0f; y < 3.1f; y += 0.5f)",
        "for (f32 z = -3.0f; z < 3.1f; z += 0.5f) {",
        "lines.emplace_back(-x, -y, -z, x, y, z);",
    )
    additions = tuple(line.strip() for line in test.additions)
    if any(value not in additions for value in required_test_additions):
        return False
    executed_test_body = tuple(
        line.strip() for line in (*test.additions, *test.context)
    )
    if not all(
        any(token in line for line in executed_test_body)
        for token in (
            "VoxelLineIterator iterator(l.start, l.getVector())",
            "UASSERTEQ(f32, (new_voxel - old_voxel).getLengthSQ(), 1)",
            "UASSERT(box.intersectsWithLine(l))",
        )
    ):
        return False
    production_additions = tuple(line.strip() for line in production.additions)
    production_removals = tuple(line.strip() for line in production.removals)
    for axis in "XYZ":
        if not all(
            any(token in line for line in production_additions)
            for token in (
                f"m_next_intersection_multi.{axis} = (m_current_node_pos.{axis} + 0.5f",
                f"m_next_intersection_multi.{axis} = (m_current_node_pos.{axis} - 0.5f",
            )
        ):
            return False
        if (
            sum(
                f"floorf(m_start_position.{axis} - 0.5)" in line
                for line in production_removals
            )
            != 2
        ):
            return False
    return True


def _declared_paths(value: tuple[str, ...]) -> frozenset[str] | None:
    if type(value) is not tuple or not value:
        return None
    result: set[str] = set()
    for path in value:
        if type(path) is not str or not path:
            return None
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
            return None
        result.add(path)
    return frozenset(result)


def _sections(content: bytes) -> tuple[_Section, ...]:
    text = content.decode("utf-8", errors="surrogateescape")
    result: list[_Section] = []
    path: str | None = None
    additions: list[str] = []
    removals: list[str] = []
    context: list[str] = []

    def finish() -> None:
        if path is not None:
            result.append(
                _Section(path, tuple(additions), tuple(removals), tuple(context))
            )

    for line in text.splitlines():
        if line.startswith("diff --git "):
            finish()
            path = _diff_new_path(line)
            additions = []
            removals = []
            context = []
            continue
        if path is None or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            additions.append(line[1:])
        elif line.startswith("-"):
            removals.append(line[1:])
        elif line.startswith(" "):
            context.append(line[1:])
    finish()
    return tuple(result)


def _diff_new_path(line: str) -> str | None:
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    value = parts[3]
    if not value.startswith("b/"):
        return None
    path = value[2:]
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
        return None
    return path


def _is_executable_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.casefold() in _EXECUTABLE_SUFFIXES and not any(
        "snapshot" in part.casefold() for part in pure.parts
    )


def _is_added_assertion(line: str) -> bool:
    return _is_substantive(line) and _ASSERTION.search(line) is not None


def _has_added_non_lua_assertion(section: _Section) -> bool:
    return not _is_lua_test_path(section.path) and any(
        _is_added_assertion(line) for line in section.additions
    )


def _has_lua_execution_regression(section: _Section) -> bool:
    """Admit only Lua assertions that the changed test code actually executes.

    Lua test files are not all automatically run.  A patch therefore needs an
    added executable assertion plus either a Busted ``it(...)`` registration or
    a direct call to a function declared in the same added hunk.  This is a
    manifest admission check, not a qualification verdict.
    """

    if not _is_lua_test_path(section.path):
        return False
    additions = tuple(line for line in section.additions if _is_lua_substantive(line))
    if not any(_is_lua_assertion(line) for line in additions):
        return False
    return _has_lua_spec_registration(additions) or _has_lua_direct_invocation(
        additions
    )


def _is_lua_test_path(path: str) -> bool:
    return PurePosixPath(path).suffix.casefold() == ".lua"


def _lua_code(line: str) -> str:
    """Strip the Lua line-comment tail before scanning executable syntax."""

    return line.split("--", 1)[0]


def _is_lua_substantive(line: str) -> bool:
    return _is_substantive(_lua_code(line))


def _is_lua_assertion(line: str) -> bool:
    code = _lua_code(line)
    return _is_lua_substantive(line) and _LUA_ASSERTION.search(code) is not None


def _has_lua_spec_registration(lines: tuple[str, ...]) -> bool:
    return any(
        _LUA_SPEC_REGISTRATION.search(_lua_code(line)) is not None for line in lines
    )


def _has_lua_direct_invocation(lines: tuple[str, ...]) -> bool:
    definitions = {
        match.group(1)
        for line in lines
        if (match := _LUA_FUNCTION_DEFINITION.match(_lua_code(line))) is not None
    }
    if not definitions:
        return False
    return any(
        (match := _LUA_DIRECT_INVOCATION.match(_lua_code(line))) is not None
        and match.group(1) in definitions
        for line in lines
    )


def _enables_guarded_test(section: _Section) -> bool:
    if not any(_CONDITIONAL_GUARD.match(line) for line in section.removals):
        return False
    return any(
        _TEST_REGISTRATION.search(line) is not None
        for line in (*section.additions, *section.context)
        if _is_substantive(line)
    )


def _is_substantive(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped and _COMMENT.match(line) is None and _BRACES_ONLY.match(line) is None
    )


def _is_formatting_only(sections: tuple[_Section, ...]) -> bool:
    additions = tuple(
        line
        for section in sections
        for line in section.additions
        if _is_substantive(line) and _INCLUDE.match(line) is None
    )
    removals = tuple(
        line
        for section in sections
        for line in section.removals
        if _is_substantive(line) and _INCLUDE.match(line) is None
    )
    if not additions or not removals:
        return False
    return _formatting_form(additions) == _formatting_form(removals)


def _formatting_form(lines: tuple[str, ...]) -> str:
    return re.sub(r"[\s{};]", "", "".join(lines))


def _is_test_registration_without_regression(
    sections: tuple[_Section, ...],
    additions: tuple[str, ...],
) -> bool:
    if any(
        _TEST_REGISTRATION.search(line) is not None
        or _LUA_SPEC_REGISTRATION.search(_lua_code(line)) is not None
        for line in additions
        if _is_substantive(line)
    ):
        return True
    return any(
        PurePosixPath(section.path).suffix.casefold() in {".conf", ".cmake"}
        and any(part.casefold() == "test" for part in PurePosixPath(section.path).parts)
        for section in sections
    )


__all__ = ["LuantiV2TestManifestDecision", "classify_luanti_v2_test_manifest"]
