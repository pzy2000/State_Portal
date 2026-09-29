from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any


# Directories that are never useful for source exploration and would otherwise
# flood the agent with build output or dependency code.
IGNORED_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".tmp",
    "reports",
    ".vite",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".warptest-cache",
    "coverage",
}

LAZARUS_IGNORED_DIRS = frozenset(
    {
        *IGNORED_DIRS,
        # Generated application/editor trees can contain stale copies of source
        # files ahead of the authoritative checkout in a bounded grep scan.
        "build-app",
        "Library",
        "Temp",
        "bin",
        "obj",
        "out",
    }
)

# Suffixes treated as searchable text for grep and the repo map.
TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".html", ".css", ".scss", ".md", ".yaml", ".yml", ".txt",
}

# Source languages used by the six Lazarus repositories.  This is deliberately
# opt-in: ordinary/frozen C1 exploration keeps the historical web-text surface,
# while Lazarus can grep the real Lua, native, Python, and Unity sources from
# which its upstream defects are derived.
LAZARUS_SOURCE_TEXT_SUFFIXES = frozenset(
    {
        *TEXT_SUFFIXES,
        ".c",
        ".cc",
        ".cmake",
        ".cpp",
        ".cs",
        ".csproj",
        ".gradle",
        ".h",
        ".hh",
        ".hpp",
        ".ini",
        ".java",
        ".lua",
        ".m",
        ".mm",
        ".py",
        ".pyi",
        ".sh",
        ".sln",
        ".toml",
        ".xml",
    }
)

# Defense-in-depth markers for private grader material. Reads are already
# confined to the game repository, so these can only appear if someone tries to
# escape the sandbox; we refuse them explicitly anyway.
PRIVATE_MARKERS = ("benchmarks/core/hidden", "benchmarks/segments/hidden")
BENCHMARK_ROOT_TASKS = Path("benchmarks/core/public/tasks.yaml")
BENCHMARK_ROOT_GAMES = Path("games")
PRIVATE_ROOT_DIRS = (
    Path("specs"),
    Path("scripts"),
    Path("benchmarks/core/hidden"),
    Path("benchmarks/segments/hidden"),
    Path("benchmarks/state_loop"),
    Path(".warptest-cache"),
)

DEFAULT_MAX_LINES = 400
DEFAULT_MAX_BYTES = 24_000
DEFAULT_GREP_RESULTS = 80
DEFAULT_GREP_FILE_SCAN = 4000
DEFAULT_MAP_DEPTH = 3
DEFAULT_MAP_ENTRIES = 400


class RepoSandbox:
    """Read-only, repo-confined file access for an exploration agent.

    Every public method returns a plain-text observation and never raises on bad
    agent input: malformed paths, escape attempts, and missing files are reported
    back to the model as ``ERROR: ...`` strings so the ReAct loop can recover.
    Paths that are actually opened are recorded in :attr:`reads` for provenance.
    """

    def __init__(
        self,
        repo_root: str | Path,
        *,
        text_suffixes: Iterable[str] | None = None,
        ignored_dirs: Iterable[str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        selected_suffixes = TEXT_SUFFIXES if text_suffixes is None else text_suffixes
        self._text_suffixes = frozenset(
            str(value).lower()
            for value in selected_suffixes
            if isinstance(value, str) and value.startswith(".")
        )
        self._ignored_dirs = frozenset(
            str(value)
            for value in (IGNORED_DIRS if ignored_dirs is None else ignored_dirs)
            if isinstance(value, str) and value
        )
        benchmark_root = _discover_benchmark_root(self.repo_root)
        self._benchmark_root = benchmark_root
        self._private_roots = tuple(
            private
            for private in _private_roots_for(benchmark_root)
            if not (
                private.name == ".warptest-cache"
                and private in self.repo_root.parents
            )
        )
        self.reads: list[str] = []
        self._read_set: set[str] = set()

    # -- internal helpers -------------------------------------------------

    def _record(self, rel: str) -> None:
        if rel not in self._read_set:
            self._read_set.add(rel)
            self.reads.append(rel)

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _is_private_path(self, path: Path) -> bool:
        benchmark_root = self._benchmark_root
        if not self._private_roots or benchmark_root is None:
            return False
        resolved = _safe_resolve(path)
        if any(
            resolved == private or private in resolved.parents
            for private in self._private_roots
        ):
            return True
        try:
            parts = resolved.relative_to(benchmark_root).parts
        except ValueError:
            return False
        return bool(
            parts
            and parts[0] == "benchmarks"
            and "hidden" in parts[1:]
        )

    def _resolve(self, raw_path: str) -> tuple[Path | None, str | None]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None, "path is required and must be a non-empty string"
        text = raw_path.strip()
        normalized = text.replace("\\", "/")
        for marker in PRIVATE_MARKERS:
            if marker in normalized:
                return None, "access to private grader data is not allowed"
        candidate = Path(text)
        if candidate.is_absolute():
            if self._is_private_path(candidate):
                return None, "access to private grader data is not allowed"
            return None, "absolute paths are not allowed"
        if ".." in Path(normalized).parts:
            return None, "parent traversal is not allowed"
        lexical = self.repo_root / candidate
        if _contains_symlink(self.repo_root, lexical):
            return None, "symlink paths are not allowed"
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        resolved = _safe_resolve(candidate)
        if resolved != self.repo_root and self.repo_root not in resolved.parents:
            return None, "path is outside the repository sandbox"
        if self._is_private_path(resolved):
            return None, "access to private grader data is not allowed"
        parts = set(resolved.relative_to(self.repo_root).parts) if resolved != self.repo_root else set()
        ignored_hit = parts & self._ignored_dirs
        if ignored_hit:
            return None, f"path is inside an ignored directory ({', '.join(sorted(ignored_hit))})"
        return resolved, None

    # -- tools ------------------------------------------------------------

    def read_file(self, path: str, start: Any = None, end: Any = None) -> str:
        resolved, error = self._resolve(path)
        if error is not None or resolved is None:
            return f"ERROR: {error}"
        if not resolved.exists():
            return f"ERROR: file does not exist: {self._rel(resolved)}"
        if not resolved.is_file():
            return f"ERROR: not a file: {self._rel(resolved)}"
        try:
            text = resolved.read_text(encoding="utf-8", errors="ignore")
        except Exception as read_error:  # pragma: no cover - unexpected fs error
            return f"ERROR: could not read file: {read_error}"

        lines = text.splitlines()
        total = len(lines)
        start_idx = _coerce_int(start)
        end_idx = _coerce_int(end)
        if start_idx is None:
            start_idx = 1
        start_idx = max(1, start_idx)
        if end_idx is None:
            end_idx = start_idx + DEFAULT_MAX_LINES - 1
        end_idx = min(total, max(start_idx, end_idx), start_idx + DEFAULT_MAX_LINES - 1)

        selected = lines[start_idx - 1:end_idx]
        rendered: list[str] = []
        byte_budget = DEFAULT_MAX_BYTES
        last_line = start_idx - 1
        for offset, line in enumerate(selected):
            number = start_idx + offset
            entry = f"{number:6d}|{line}"
            byte_budget -= len(entry.encode("utf-8")) + 1
            if byte_budget < 0:
                break
            rendered.append(entry)
            last_line = number

        rel = self._rel(resolved)
        self._record(rel)
        header = f"FILE {rel} (lines {start_idx}-{last_line} of {total}):"
        body = "\n".join(rendered) if rendered else "(no lines in selected range)"
        truncated = "\n... (truncated; request a narrower line range to read more)" if last_line < end_idx else ""
        return f"{header}\n{body}{truncated}"

    def grep(self, pattern: str, glob: str | None = None, max_results: int = DEFAULT_GREP_RESULTS) -> str:
        if not isinstance(pattern, str) or not pattern:
            return "ERROR: pattern is required and must be a non-empty string"
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return f"ERROR: invalid regex: {error}"

        results: list[str] = []
        scanned = 0
        matched_files: list[str] = []
        for file_path in self._iter_text_files():
            if scanned >= DEFAULT_GREP_FILE_SCAN:
                break
            scanned += 1
            rel = self._rel(file_path)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(file_path.name, glob)):
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            file_had_match = False
            for lineno, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    file_had_match = True
                    if len(results) >= max_results:
                        break
            if file_had_match:
                matched_files.append(rel)
            if len(results) >= max_results:
                break

        for rel in matched_files:
            self._record(rel)
        if not results:
            return f"No matches for pattern {pattern!r}" + (f" in {glob}" if glob else "")
        capped = "\n... (more matches truncated; refine the pattern or glob)" if len(results) >= max_results else ""
        return "\n".join(results) + capped

    def list_dir(self, path: str = ".") -> str:
        resolved, error = self._resolve(path or ".")
        if error is not None or resolved is None:
            return f"ERROR: {error}"
        if not resolved.exists():
            return f"ERROR: directory does not exist: {self._rel(resolved)}"
        if not resolved.is_dir():
            return f"ERROR: not a directory: {self._rel(resolved)}"
        entries: list[str] = []
        for child in sorted(resolved.iterdir(), key=lambda item: (item.is_file(), item.name)):
            if child.is_symlink():
                continue
            if self._is_private_path(child):
                continue
            if child.is_dir():
                marker = "/ (skipped)" if child.name in self._ignored_dirs else "/"
                entries.append(child.name + marker)
            else:
                entries.append(child.name)
        rel = self._rel(resolved)
        listing = "\n".join(entries) if entries else "(empty directory)"
        return f"DIR {rel}:\n{listing}"

    def build_repo_map(self, max_depth: int = DEFAULT_MAP_DEPTH, max_entries: int = DEFAULT_MAP_ENTRIES) -> str:
        lines: list[str] = [self.repo_root.name + "/"]
        count = 0

        def walk(directory: Path, depth: int) -> None:
            nonlocal count
            if depth > max_depth or count >= max_entries:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name))
            except Exception:
                return
            for child in children:
                if count >= max_entries:
                    return
                if child.is_symlink():
                    continue
                if self._is_private_path(child):
                    continue
                if child.is_dir() and child.name in self._ignored_dirs:
                    continue
                indent = "  " * depth
                if child.is_dir():
                    lines.append(f"{indent}{child.name}/")
                    count += 1
                    walk(child, depth + 1)
                else:
                    lines.append(f"{indent}{child.name}")
                    count += 1

        walk(self.repo_root, 1)
        if count >= max_entries:
            lines.append("... (repo map truncated; use list_dir to explore further)")
        return "\n".join(lines)

    def dispatch(self, tool: str, args: dict[str, Any]) -> str:
        if not isinstance(args, dict):
            args = {}
        if tool == "read_file":
            return self.read_file(args.get("path", ""), args.get("start"), args.get("end"))
        if tool == "grep":
            return self.grep(args.get("pattern", ""), args.get("glob"))
        if tool == "list_dir":
            return self.list_dir(args.get("path", "."))
        return f"ERROR: unknown tool {tool!r}; use read_file, grep, or list_dir"

    # -- iteration --------------------------------------------------------

    def _iter_text_files(self):
        for root, dirs, files in os.walk(self.repo_root):
            root_path = Path(root)
            dirs[:] = [
                name
                for name in dirs
                if name not in self._ignored_dirs
                and not (root_path / name).is_symlink()
                and not self._is_private_path(root_path / name)
            ]
            for file_name in sorted(files):
                path = root_path / file_name
                if path.is_symlink():
                    continue
                if self._is_private_path(path):
                    continue
                if path.suffix.lower() in self._text_suffixes:
                    yield path


def _discover_benchmark_root(repo_root: Path) -> Path | None:
    for candidate in (repo_root, *repo_root.parents):
        if (candidate / BENCHMARK_ROOT_TASKS).is_file() and (candidate / BENCHMARK_ROOT_GAMES).is_dir():
            return candidate.resolve()
    return None


def _private_roots_for(benchmark_root: Path | None) -> tuple[Path, ...]:
    if benchmark_root is None:
        return ()
    return tuple((benchmark_root / rel).resolve() for rel in PRIVATE_ROOT_DIRS)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _contains_symlink(repo_root: Path, candidate: Path) -> bool:
    """Reject any symlink component before resolution can erase its identity."""

    try:
        relative = candidate.relative_to(repo_root)
    except ValueError:
        return True
    current = repo_root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
