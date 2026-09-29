from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import Any

import yaml

from .contract import (
    evaluation_contract,
    materialize_legacy_spec,
    surface_contract,
    validate_evaluation_contract,
)


DEFAULT_ROOT = Path(__file__).resolve().parents[2]
FORMAL_TASKS = Path("benchmarks/p5/c1_tasks.yaml")


@functools.lru_cache(maxsize=8)
def _formal_specs(root_text: str) -> dict[str, dict[str, Any]]:
    root = Path(root_text).expanduser().resolve()
    catalog_path = root / FORMAL_TASKS
    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        raise ValueError(f"formal C1 catalog has no tasks list: {catalog_path}")
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        spec_value = task.get("oracle_spec")
        if not task_id or not isinstance(spec_value, str):
            raise ValueError(f"malformed formal C1 task row in {catalog_path}")
        spec_path = (root / spec_value).resolve()
        if root != spec_path and root not in spec_path.parents:
            raise ValueError(f"formal C1 oracle escapes benchmark root: {spec_path}")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError(f"formal C1 oracle is not a mapping: {spec_path}")
        if evaluation_contract(spec) is None:
            raise ValueError(
                f"formal C1 oracle lacks evaluation-v1 contract: {spec_path}"
            )
        errors = validate_evaluation_contract(spec, spec_path)
        if errors:
            raise ValueError(f"invalid formal C1 evaluation contract {spec_path}: {errors}")
        if task_id in result:
            raise ValueError(f"duplicate formal C1 task id {task_id!r} in {catalog_path}")
        result[task_id] = spec
    return result


def formal_evaluation_contract(
    task_id: str, *, root: str | Path = DEFAULT_ROOT
) -> dict[str, Any] | None:
    spec = _formal_specs(str(Path(root).expanduser().resolve())).get(str(task_id))
    contract = evaluation_contract(spec) if isinstance(spec, dict) else None
    return copy.deepcopy(contract) if contract is not None else None


def formal_surface_contract(
    task_id: str,
    surface: str = "gui",
    *,
    root: str | Path = DEFAULT_ROOT,
) -> dict[str, Any] | None:
    spec = _formal_specs(str(Path(root).expanduser().resolve())).get(str(task_id))
    contract = surface_contract(spec, surface) if isinstance(spec, dict) else None
    return copy.deepcopy(contract) if contract is not None else None


def formal_spec(
    task_id: str,
    *,
    root: str | Path = DEFAULT_ROOT,
    materialized: bool = False,
) -> dict[str, Any] | None:
    """Return an isolated trusted-parent spec view for evaluator consumers."""

    spec = _formal_specs(str(Path(root).expanduser().resolve())).get(str(task_id))
    if not isinstance(spec, dict):
        return None
    return materialize_legacy_spec(spec) if materialized else copy.deepcopy(spec)
