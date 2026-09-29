"""Two-cell runtime smoke for one registered Luanti Phase-0 task.

This is deliberately **not** a Phase-0 run.  The authorized pilot is an
eight-cell population whose coordinator writes a ``c1-lazarus-phase0-run-v1``
report; nothing here may impersonate that.  What this module does instead is
exercise one arm of one task end to end -- real materialized pair, real agent
source view, real provider calls -- so a newly registered task can be validated
before any authorized population is dispatched.

The receipt is written under its own schema and records that it is a smoke run,
how many provider calls it made, and the semantic verdicts the runtime observed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .c1_experiment import relay_environment
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S, provider_timeout_environment
from .lazarus_orchestrator import PHASE0_DECISION_MODEL, LazarusTrialKey
from .lazarus_phase0 import LazarusPhase0CellContext, preflight_lazarus_phase0
from .model_request import model_request_profiles_environment


SMOKE_SCHEMA = "c1-lazarus-phase0-runtime-smoke-v1"
_ARMS = ("lazarus_state", "lazarus_agent_s")
_VERSIONS = ("buggy", "fixed")


class LazarusSmokeError(RuntimeError):
    """Raised when a smoke run cannot be prepared from verified evidence."""


@dataclass(frozen=True)
class LazarusPhase0Smoke:
    """One task/arm smoke outcome and the receipt it was written to."""

    task_id: str
    arm: str
    complete: bool
    cells: tuple[dict[str, Any], ...]
    report_path: Path

    @property
    def separates_goal_from_defect(self) -> bool:
        by_version = {cell["version"]: cell for cell in self.cells}
        if set(by_version) != set(_VERSIONS):
            return False
        buggy, fixed = by_version["buggy"], by_version["fixed"]
        return bool(
            fixed.get("goal_checks_ok") is True
            and fixed.get("defect_signature_ok") is not True
            and buggy.get("defect_signature_ok") is True
            and buggy.get("goal_checks_ok") is not True
        )


def run_lazarus_phase0_smoke(
    root: str | Path,
    *,
    task_id: str,
    arm: str = "lazarus_state",
    seed: int = 0,
    report_path: str | Path,
    credential_source: str | Path | None = None,
    runtime_factory: Any | None = None,
    provider_timeout_s: float = C1_PROVIDER_TIMEOUT_S,
) -> LazarusPhase0Smoke:
    """Run the buggy and fixed cells of one task/arm and record the outcome.

    Preflight still runs in full: the pair, the agent-visible view and the gate
    evidence a cell depends on must be verified even though only two of the eight
    registered cells are dispatched.  ``runtime_factory`` exists for tests; the
    default path builds the same provider-backed runner the pilot uses.
    """

    if arm not in _ARMS:
        raise LazarusSmokeError(f"unknown Lazarus arm {arm!r}")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise LazarusSmokeError("Lazarus smoke seed must be a non-negative integer")
    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise LazarusSmokeError("Lazarus smoke workspace does not exist")
    destination = Path(report_path).expanduser()
    destination = (
        destination.resolve()
        if destination.is_absolute()
        else (workspace / destination).resolve()
    )

    credentials = None
    if credential_source is not None:
        candidate = Path(credential_source).expanduser()
        credentials = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace / candidate).resolve()
        )
        if workspace != credentials and workspace not in credentials.parents:
            raise LazarusSmokeError("Lazarus smoke credential source escapes the workspace")

    with relay_environment(credentials) if credentials else _nullcontext():
        preflight = preflight_lazarus_phase0(workspace)
        if not preflight.complete:
            raise LazarusSmokeError(
                "Lazarus smoke requires a complete preflight: "
                + "; ".join(preflight.failures[:4])
            )
        task = next(
            (item for item in preflight.tasks if str(item.get("id")) == task_id), None
        )
        pair = preflight.pairs.get(task_id)
        view = preflight.agent_sources.get(task_id)
        if task is None or pair is None or view is None:
            raise LazarusSmokeError(f"{task_id} is absent from the verified preflight")
        runner = runtime_factory
        if runner is None:
            from .lazarus_runtime import build_lazarus_phase0_runtime_runner

            # Runtime evidence is written with a no-overwrite guard, so each smoke
            # gets its own root instead of colliding with an earlier run.
            runner = build_lazarus_phase0_runtime_runner(
                workspace,
                runtime_root=workspace
                / ".warptest-cache"
                / "lazarus"
                / "phase0-smoke-runtime"
                / f"{task_id}-{arm}-s{seed}-{time.time_ns()}",
                provider_timeout_s=provider_timeout_s,
            )
        cells: list[dict[str, Any]] = []
        with model_request_profiles_environment(
            {PHASE0_DECISION_MODEL: "thinking_disabled"}
        ):
            for version in _VERSIONS:
                key = LazarusTrialKey(
                    arm=arm,
                    model=PHASE0_DECISION_MODEL,
                    seed=seed,
                    task_id=task_id,
                    version=version,
                )
                context = LazarusPhase0CellContext(
                    key=key,
                    task=task,
                    artifact=pair.buggy if version == "buggy" else pair.fixed,
                    agent_source=view,
                    defect_locus=str((task.get("defect") or {}).get("defect_locus") or ""),
                    per_trial_p2p_ids=preflight.per_trial_p2p_ids.get(task_id, ()),
                    version_switch_ms=pair.version_switch_ms,
                )
                started = time.monotonic()
                with provider_timeout_environment(provider_timeout_s):
                    observation = runner(context)
                cells.append(
                    _cell_payload(
                        key=key,
                        observation=observation,
                        elapsed_ms=(time.monotonic() - started) * 1000.0,
                    )
                )
    smoke = LazarusPhase0Smoke(
        task_id=task_id,
        arm=arm,
        complete=all(cell["complete"] for cell in cells),
        cells=tuple(cells),
        report_path=destination,
    )
    _write_receipt(smoke, preflight_model=PHASE0_DECISION_MODEL, seed=seed)
    return smoke


def _cell_payload(
    *, key: LazarusTrialKey, observation: Any, elapsed_ms: float
) -> dict[str, Any]:
    return {
        "invocation_id": key.invocation_id(),
        "arm": key.arm,
        "model": key.model,
        "seed": key.seed,
        "task_id": key.task_id,
        "version": key.version,
        "complete": bool(getattr(observation, "complete", False)),
        "runtime_restored": getattr(observation, "runtime_restored", None),
        "before_semantic_ok": getattr(observation, "before_semantic_ok", None),
        "goal_checks_ok": getattr(observation, "goal_checks_ok", None),
        "defect_signature_ok": getattr(observation, "defect_signature_ok", None),
        "invariants_ok": getattr(observation, "invariants_ok", None),
        "synthesis_cache_hit": getattr(observation, "synthesis_cache_hit", None),
        "actual_synthesis_calls": getattr(observation, "actual_synthesis_calls", None),
        "checkpoint_reuse_success": getattr(
            observation, "checkpoint_reuse_success", None
        ),
        "treatment_tokens": getattr(observation, "treatment_tokens", None),
        "treatment_time_ms": getattr(observation, "treatment_time_ms", None),
        "elapsed_ms": elapsed_ms,
    }


def _write_receipt(
    smoke: LazarusPhase0Smoke, *, preflight_model: str, seed: int
) -> None:
    payload = {
        "schema": SMOKE_SCHEMA,
        "schema_version": 1,
        "authorized_population": "none",
        "is_phase0_run": False,
        "task_id": smoke.task_id,
        "arm": smoke.arm,
        "model": preflight_model,
        "seed": seed,
        "versions": list(_VERSIONS),
        "complete": smoke.complete,
        "separates_goal_from_defect": smoke.separates_goal_from_defect,
        "provider_calls_performed": True,
        "cells": [dict(cell) for cell in smoke.cells],
    }
    smoke.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = smoke.report_path.with_name(
        f".{smoke.report_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, smoke.report_path)
    finally:
        temporary.unlink(missing_ok=True)


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool:
        return False


__all__ = [
    "SMOKE_SCHEMA",
    "LazarusPhase0Smoke",
    "LazarusSmokeError",
    "run_lazarus_phase0_smoke",
]
