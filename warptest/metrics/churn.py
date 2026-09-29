"""Git-history churn per file and per function line range.

Change frequency is a classic empirical defect predictor. We read it straight
from the game repository's own git history, so it is a fact about the artifact,
not something we assign. Per-function churn uses ``git log -L start,end:file``,
which follows the line range across history.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .static_metrics import FunctionMetric


@dataclass
class FileChurn:
    commits: int
    added: int
    deleted: int


def _run_git(repo: Path, args: list[str], timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout
    except Exception:
        return ""


def file_churn(repo: Path, rel_path: str) -> FileChurn:
    """Total commits + added/deleted lines touching a file across history."""
    out = _run_git(
        repo,
        ["log", "--follow", "--numstat", "--format=%H", "--", rel_path],
    )
    commits = 0
    added = 0
    deleted = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3 and (parts[0].isdigit() or parts[0] == "-"):
            a = int(parts[0]) if parts[0].isdigit() else 0
            d = int(parts[1]) if parts[1].isdigit() else 0
            added += a
            deleted += d
        elif len(line) >= 7 and " " not in line:
            commits += 1
    return FileChurn(commits=commits, added=added, deleted=deleted)


def line_range_commits(repo: Path, rel_path: str, start: int, end: int) -> int:
    """Number of commits that touched a specific line range (git log -L)."""
    out = _run_git(
        repo,
        ["log", "--format=%H", f"-L{start},{end}:{rel_path}"],
        timeout=120,
    )
    return sum(1 for ln in out.splitlines() if ln.strip() and " " not in ln.strip())


def annotate_churn(repo: Path, metrics: list[FunctionMetric], *, per_line: bool = True) -> None:
    """Fill churn_commits / churn_lines on each metric in place.

    File-level added+deleted is used as churn_lines; per-function commit count is
    used as churn_commits when ``per_line`` is set (falls back to file commits).
    """
    file_cache: dict[str, FileChurn] = {}
    for m in metrics:
        if m.file not in file_cache:
            file_cache[m.file] = file_churn(repo, m.file)
        fc = file_cache[m.file]
        m.churn_lines = fc.added + fc.deleted
        if per_line:
            m.churn_commits = line_range_commits(repo, m.file, m.start_line, m.end_line)
        else:
            m.churn_commits = fc.commits
