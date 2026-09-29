"""Single source of truth for the paper's C1-Lazarus population.

The paper studies bugs for which the registered upstream tests remain P2P on
both the fixed and buggy trees.  The broader ``lz-*`` catalogs are retained
for audit and artifact replay only; they are not paper populations.
This module freezes the one current authority so a caller cannot silently use
an old count, an old catalog path, or a merely same-sized replacement set.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path
from typing import Any

import yaml

from .lazarus_catalog import validate_lazarus_catalog


PAPER_POPULATION_ID = "c1-lazarus-p2p-only-11-v1"
PAPER_CATALOG_PATH = Path("benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml")
PAPER_CATALOG_SHA256 = "a04f414d3b65aac441aaa7f6743030b81d1c92fe1f48dc5133fd18c6100ffd5b"
PAPER_TASK_IDS = (
    "lzp-jupyterlab-17153-shortcut-chip-contrast",
    "lzp-jupyterlab-17222-toolbar-button-pressed-toggle",
    "lzp-jupyterlab-17618-high-contrast-selection-layer",
    "lzp-jupyterlab-18464-debugger-zero-variable-detail",
    "lzp-jupyterlab-18996-htmlselect-caret-offset",
    "lzp-jupyterlab-19091-themed-button-glyph-font",
    "lzp-luanti-16679-unavailable-settings",
    "lzp-luanti-16799-world-mods",
    "lzp-luanti-17152-server-list-search-tooltip",
    "lzp-openra-22283-tileset-template-order",
    "lzp-openra-22476-load-game-faction-filter",
)
PAPER_TASK_COUNT = len(PAPER_TASK_IDS)
PAPER_BENCHMARK_COUNTS = {"jupyterlab": 6, "luanti": 3, "openra": 2}

LEGACY_CATALOG_PATH = Path("benchmarks/p5/c1_lazarus_tasks.yaml")
LEGACY_TASK_COUNTS = (24, 30)


def validate_paper_catalog(
    payload: Mapping[str, Any],
    *,
    repository_root: str | Path,
    source_path: str | Path = PAPER_CATALOG_PATH,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Validate the exact 11-row P2P-only paper catalog and its hidden specs."""

    root = Path(repository_root).expanduser().resolve()
    source = Path(source_path).expanduser()
    source = source.resolve() if source.is_absolute() else (root / source).resolve()
    if source != (root / PAPER_CATALOG_PATH).resolve():
        raise ValueError(
            "C1-Lazarus paper catalog must be " f"{PAPER_CATALOG_PATH.as_posix()}"
        )
    if payload.get("p2p_only_population") is not True:
        raise ValueError("C1-Lazarus paper catalog must declare p2p_only_population")
    if payload.get("formal_population") is True:
        raise ValueError("legacy formal_population cannot be the paper population")
    observed_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if observed_sha256 != PAPER_CATALOG_SHA256:
        raise ValueError("C1-Lazarus paper catalog SHA-256 drift")

    hosts, tasks = validate_lazarus_catalog(payload, source_path=source)
    task_ids = tuple(str(task.get("id") or "") for task in tasks)
    if task_ids != PAPER_TASK_IDS:
        raise ValueError(
            "C1-Lazarus paper catalog ordered task IDs drifted from the frozen "
            f"{PAPER_TASK_COUNT}-task population"
        )
    observed_counts = Counter(str(task.get("benchmark") or "") for task in tasks)
    if dict(observed_counts) != PAPER_BENCHMARK_COUNTS:
        raise ValueError(
            "C1-Lazarus paper benchmark counts must be "
            f"{PAPER_BENCHMARK_COUNTS}"
        )

    for task in tasks:
        task_id = str(task["id"])
        tags = tuple(str(value) for value in task.get("tags") or ())
        if "p2p-only" not in tags:
            raise ValueError(f"{task_id}: paper task must carry p2p-only tag")
        defect = task.get("defect")
        if not isinstance(defect, Mapping) or defect.get("defect_locus") != "at_goal":
            raise ValueError(f"{task_id}: paper task must be at_goal")
        spec_value = task.get("oracle_spec")
        if not isinstance(spec_value, str) or not spec_value.strip():
            raise ValueError(f"{task_id}: oracle spec is missing")
        spec_path = (root / spec_value).resolve()
        if root != spec_path and root not in spec_path.parents:
            raise ValueError(f"{task_id}: oracle spec escapes repository")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        evaluation = spec.get("evaluation") if isinstance(spec, Mapping) else None
        lazarus = evaluation.get("lazarus") if isinstance(evaluation, Mapping) else None
        if not isinstance(lazarus, Mapping):
            raise ValueError(f"{task_id}: Lazarus oracle contract is missing")
        upstream = lazarus.get("upstream_tests")
        if not isinstance(upstream, Mapping):
            raise ValueError(f"{task_id}: upstream test contract is missing")
        if upstream.get("fail_to_pass") != []:
            raise ValueError(f"{task_id}: paper task must have no F2P test")
        for field in ("pass_to_pass", "per_trial_p2p"):
            values = upstream.get(field)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
                raise ValueError(f"{task_id}: {field} must be non-empty")
        if not isinstance(lazarus.get("f2p_absence"), Mapping):
            raise ValueError(f"{task_id}: F2P absence evidence is missing")
        for field in ("goal_checks", "defect_signature", "invariant_checks"):
            values = lazarus.get(field)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
                raise ValueError(f"{task_id}: {field} must be non-empty")

    return hosts, tasks


__all__ = [
    "LEGACY_CATALOG_PATH",
    "LEGACY_TASK_COUNTS",
    "PAPER_BENCHMARK_COUNTS",
    "PAPER_CATALOG_PATH",
    "PAPER_CATALOG_SHA256",
    "PAPER_POPULATION_ID",
    "PAPER_TASK_COUNT",
    "PAPER_TASK_IDS",
    "validate_paper_catalog",
]
