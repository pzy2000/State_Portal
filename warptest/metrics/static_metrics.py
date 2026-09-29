"""Static per-function metrics via lizard (one tool, many languages).

lizard gives McCabe cyclomatic complexity, NLOC, token count and parameter
count directly; with the ``nd`` extension it also gives maximum nesting depth.
We deliberately use a single, well-known, off-the-shelf analyzer so the numbers
are not "our own definition of complexity" that a reviewer can dispute.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import lizard

# Enable the nesting-depth extension so ``max_nesting_depth`` is populated.
_ANALYZER = lizard.FileAnalyzer(lizard.get_extensions(["nd"]))

# File suffix -> coarse language label (for reporting only).
_LANG = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".lua": "lua",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".py": "python",
}


@dataclass
class FunctionMetric:
    file: str  # repo-relative path
    name: str  # unqualified callable name (or "(anonymous)")
    long_name: str
    language: str
    start_line: int
    end_line: int
    cyclomatic: int
    nesting: int
    nloc: int
    tokens: int
    params: int
    # coupling filled in by coupling.py; churn by churn.py (per line range)
    fan_in: int = 0
    fan_out: int = 0
    churn_commits: int = 0
    churn_lines: int = 0

    @property
    def key(self) -> str:
        return f"{self.file}:{self.name}:{self.start_line}"

    @property
    def short_name(self) -> str:
        """Bare callable name (last dotted segment) used for call-coupling.

        lizard reports qualified names for JS methods (e.g.
        ``GameManager.prototype.move``); the token that appears at call sites is
        the last segment (``self.move(...)``)."""
        return self.name.split(".")[-1] if self.name else self.name

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_file(repo: Path, path: Path) -> list[FunctionMetric]:
    lang = _LANG.get(path.suffix.lower())
    if lang is None:
        return []
    try:
        info = _ANALYZER(str(path))
    except Exception:
        return []
    rel = str(path.relative_to(repo)) if _is_relative(path, repo) else str(path)
    out: list[FunctionMetric] = []
    for f in info.function_list:
        out.append(
            FunctionMetric(
                file=rel,
                name=getattr(f, "unqualified_name", None) or f.name,
                long_name=f.long_name,
                language=lang,
                start_line=f.start_line,
                end_line=f.end_line,
                cyclomatic=f.cyclomatic_complexity,
                nesting=int(getattr(f, "max_nesting_depth", 0) or 0),
                nloc=f.nloc,
                tokens=f.token_count,
                params=f.parameter_count,
            )
        )
    return out


def analyze_paths(repo: Path, paths: list[Path]) -> list[FunctionMetric]:
    metrics: list[FunctionMetric] = []
    for path in paths:
        metrics.extend(analyze_file(repo, path))
    return metrics


def _is_relative(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
