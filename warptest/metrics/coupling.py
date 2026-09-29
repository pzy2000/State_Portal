"""Text-level fan-in / fan-out coupling over the analysed file set.

This is an explicit, transparent approximation of call coupling that works
uniformly across languages without a per-language call-graph tool:

  fan_out(f) = number of *distinct other* known function short-names that appear
               as a call ``name(`` inside f's source span.
  fan_in(f)  = number of *distinct other* functions whose span contains a call
               to f's short-name.

Limitations (stated so the paper can disclose them): name-based, so it cannot
disambiguate overloads/same-named methods on different objects, and it only sees
callees that are themselves in the analysed set. It is a coupling *proxy*, not a
sound call graph. It is deterministic and re-derivable, which is what we need.
"""

from __future__ import annotations

import re
from pathlib import Path

from .static_metrics import FunctionMetric

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _span_text(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[max(0, start - 1) : end])


def annotate_coupling(repo: Path, metrics: list[FunctionMetric]) -> None:
    """Fill fan_in / fan_out on each metric in place."""
    # Group by file and cache file text.
    file_lines: dict[str, list[str]] = {}
    for m in metrics:
        if m.file not in file_lines:
            try:
                file_lines[m.file] = (repo / m.file).read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
            except Exception:
                file_lines[m.file] = []

    # Set of callable short-names that are real functions (skip anonymous and
    # single-char names that create noise).
    named = [m for m in metrics if m.short_name and m.name != "(anonymous)" and len(m.short_name) > 1]
    name_set = {m.short_name for m in named}

    # For each function, the set of callee short-names it references (excluding self).
    callees: dict[str, set[str]] = {}
    for m in named:
        body = _span_text(file_lines.get(m.file, []), m.start_line, m.end_line)
        called: set[str] = set()
        for match in re.finditer(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", body):
            token = match.group(1)
            if token in name_set and token != m.short_name:
                called.add(token)
        callees[m.key] = called

    # fan_out = |callees|; fan_in = how many functions call me.
    incoming: dict[str, int] = {name: 0 for name in name_set}
    for m in named:
        for callee in callees[m.key]:
            incoming[callee] = incoming.get(callee, 0) + 1

    for m in metrics:
        if m.key in callees:
            m.fan_out = len(callees[m.key])
            m.fan_in = incoming.get(m.short_name, 0)
        else:
            m.fan_out = 0
            m.fan_in = 0
