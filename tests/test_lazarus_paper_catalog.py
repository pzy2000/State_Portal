from __future__ import annotations

from pathlib import Path

import yaml

from warptest.lazarus_paper_catalog import (
    PAPER_BENCHMARK_COUNTS,
    PAPER_CATALOG_PATH,
    PAPER_CATALOG_SHA256,
    PAPER_TASK_COUNT,
    PAPER_TASK_IDS,
    validate_paper_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmarks/p5/lazarus_p2p_only_manifest.yaml"


def _yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_paper_catalog_is_exactly_the_ordered_11_task_p2p_only_population() -> None:
    catalog_path = ROOT / PAPER_CATALOG_PATH
    hosts, tasks = validate_paper_catalog(
        _yaml(catalog_path),
        repository_root=ROOT,
        source_path=catalog_path,
    )
    assert len(tasks) == PAPER_TASK_COUNT == 11
    assert tuple(task["id"] for task in tasks) == PAPER_TASK_IDS
    assert {
        benchmark: sum(task["benchmark"] == benchmark for task in tasks)
        for benchmark in hosts
    } == PAPER_BENCHMARK_COUNTS


def test_paper_manifest_can_only_point_to_the_11_task_authority() -> None:
    manifest = _yaml(MANIFEST_PATH)
    assert manifest["tasks"] == PAPER_CATALOG_PATH.as_posix()
    assert manifest["task_count"] == PAPER_TASK_COUNT
    assert manifest["catalog_sha256"] == PAPER_CATALOG_SHA256
    assert manifest["formal"] is False
    assert manifest["confirmatory"] is False
    assert manifest["paper_ready"] is False

