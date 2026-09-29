"""Loader for the fork -> upstream provenance map (benchmarks/upstream/sources.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_REL = Path("benchmarks") / "upstream" / "sources.yaml"


def sources_path(root: Path | None = None) -> Path:
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    return root / _DEFAULT_REL


def load_sources(root: Path | None = None) -> dict[str, Any]:
    path = sources_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError(f"malformed upstream sources map: {path}")
    return data


def get_source(benchmark: str, root: Path | None = None) -> dict[str, Any]:
    """Return the source entry for a benchmark (e.g. 'luanti'), or raise KeyError."""

    sources = load_sources(root)["sources"]
    if benchmark not in sources:
        raise KeyError(f"no upstream source mapping for benchmark {benchmark!r}")
    return sources[benchmark]
