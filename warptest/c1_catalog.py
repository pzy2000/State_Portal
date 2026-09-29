"""Typed catalog contract and benchmark-id compatibility for formal C1."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CANONICAL_BENCHMARK_IDS = (
    "2048",
    "datingsim",
    "spacerpg",
    "luanti",
    "openra",
    "jupyterlab",
    "libreoffice",
    "blender",
    "jynew",
    "pal3",
)

_BENCHMARK_ALIASES = {
    "2048": "2048",
    "datingsim": "datingsim",
    "love": "datingsim",
    "spacerpg": "spacerpg",
    "spacerpg": "spacerpg",
    "spacerpg": "spacerpg",
    "luanti": "luanti",
    "minetest": "luanti",
    "openra": "openra",
    "jupyterlab": "jupyterlab",
    "libreoffice": "libreoffice",
    "blender": "blender",
    "jynew": "jynew",
    "pal3": "pal3",
    "pal3unity": "pal3",
    "pal3.unity": "pal3",
}

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOWER_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def canonical_benchmark_id(value: Any) -> str:
    """Return the canonical lower-case id, accepting aliases for old reports."""

    raw = str(value or "").strip()
    token = raw.lower().replace("-", "_")
    canonical = _BENCHMARK_ALIASES.get(token)
    if canonical is None:
        compact = re.sub(r"[^a-z0-9]", "", raw.lower())
        canonical = _BENCHMARK_ALIASES.get(compact)
    if canonical is None:
        raise ValueError(f"unknown C1 benchmark id: {raw!r}")
    return canonical


def expected_random_control(benchmark: str) -> dict[str, Any]:
    canonical = canonical_benchmark_id(benchmark)
    if canonical in {"2048", "datingsim", "spacerpg"}:
        return {
            "mode": "experiment_seed",
            "seeds": [0, 1, 2],
            "algorithm": "mulberry32",
        }
    if canonical == "luanti":
        return {
            "mode": "experiment_seed",
            "seeds": [0, 1, 2],
            "algorithm": "luanti_fixed_map_seed",
        }
    return {
        "mode": "not_applicable",
        "seed_role": "trial_pairing_only",
    }


def validate_c1_v5_catalog(
    payload: Mapping[str, Any],
    *,
    source_path: str | Path,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Validate and return the v5 host registry and task rows.

    Aliases are deliberately not accepted here.  They remain available through
    :func:`canonical_benchmark_id` only for historical artifact readers.
    """

    source = Path(source_path)
    if payload.get("schema_version") != 5 or payload.get("task_schema") != "c1_v5":
        raise ValueError(f"{source}: formal C1 catalog must be schema c1_v5/version 5")

    raw_hosts = payload.get("benchmark_hosts")
    if not isinstance(raw_hosts, Mapping):
        raise ValueError(f"{source}: c1_v5 requires benchmark_hosts")
    if tuple(raw_hosts) != CANONICAL_BENCHMARK_IDS:
        raise ValueError(
            f"{source}: benchmark_hosts must list the ten canonical ids in order"
        )

    hosts: dict[str, dict[str, str]] = {}
    repos: set[str] = set()
    revisions: set[str] = set()
    for benchmark, raw_host in raw_hosts.items():
        if not _LOWER_SLUG.fullmatch(str(benchmark)):
            raise ValueError(f"{source}: non-canonical benchmark id {benchmark!r}")
        if not isinstance(raw_host, Mapping):
            raise ValueError(f"{source}: benchmark host {benchmark} must be a mapping")
        host = {
            key: str(raw_host.get(key) or "").strip()
            for key in ("display_name", "repo", "gitmodules_url", "revision")
        }
        missing = [key for key, value in host.items() if not value]
        if missing:
            raise ValueError(
                f"{source}: benchmark host {benchmark} is missing {', '.join(missing)}"
            )
        if not _FULL_SHA.fullmatch(host["revision"]):
            raise ValueError(
                f"{source}: benchmark host {benchmark} revision must be a full SHA"
            )
        if host["repo"] in repos or host["revision"] in revisions:
            raise ValueError(f"{source}: benchmark host repos and revisions must be unique")
        repos.add(host["repo"])
        revisions.add(host["revision"])
        hosts[str(benchmark)] = host

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not all(
        isinstance(task, Mapping) for task in raw_tasks
    ):
        raise ValueError(f"{source}: formal C1 catalog must contain a tasks list")
    tasks = [dict(task) for task in raw_tasks]
    task_ids: set[str] = set()
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        benchmark = str(task.get("benchmark") or "").strip()
        if not task_id or task_id in task_ids:
            raise ValueError(f"{source}: C1 task ids must be non-empty and unique")
        task_ids.add(task_id)
        if benchmark not in hosts or canonical_benchmark_id(benchmark) != benchmark:
            raise ValueError(
                f"{source}: task {task_id} uses non-canonical benchmark {benchmark!r}"
            )
        if str(task.get("repo") or "").strip() != hosts[benchmark]["repo"]:
            raise ValueError(
                f"{source}: task {task_id} repo does not match benchmark_hosts"
            )
        if not str(task.get("prompt_revision") or "").strip():
            raise ValueError(f"{source}: task {task_id} is missing prompt_revision")
        public_fields = ("preconditions", "reference_steps", "state_description")
        for field in public_fields:
            text = str(task.get(field) or "").strip()
            if not text:
                raise ValueError(f"{source}: task {task_id} is missing {field}")
            if "registered" in text.lower():
                raise ValueError(
                    f"{source}: task {task_id} public {field} references registered state"
                )
        random_control = task.get("random_control")
        expected = expected_random_control(benchmark)
        if random_control != expected:
            raise ValueError(
                f"{source}: task {task_id} random_control must equal {expected!r}"
            )
    return hosts, tasks

