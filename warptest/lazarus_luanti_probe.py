"""Resolve a frozen Luanti lazarus task's checks against an observation.

Two observation sources exist, and an audit always says which one it used:

``live_headless_replay``
    The candidate's own binary is started again as a headless server, the
    registered fixture is loaded, and the semantic result is read from its log.
    Only tasks whose closure committed a re-runnable server replay qualify.

``frozen_gate10_evidence``
    The admitted Gate 10 oracle's own observation is replayed from the typed
    artifact.  This resolves the spec's checks without re-executing the client,
    which several candidates cannot do offline because their closure captured a
    windowed client rather than a headless server.

Keeping the source in the audit stops a frozen-evidence run from being read as a
live one, which is the only way the weaker source stays honest.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .lazarus_luanti_states import (
    PROBE_ERROR_CHECK,
    STATE_AUDIT_CHECK,
    LuantiLazarusStates,
    extract_luanti_lazarus_states,
)
from .lazarus_state_checks import evaluate_state_checks, project_state_checks


AUDIT_SCHEMA_VERSION = 1
LIVE_SOURCE = "live_headless_replay"
FROZEN_SOURCE = "frozen_gate10_evidence"
LIVE_ADAPTER = "luanti_live_world_v1"
FROZEN_ADAPTER = "luanti_lazarus_frozen_evidence_v1"
CHECKPOINT_STRATEGY = "luanti_worldmod_state_validate"
_VERSIONS = ("fixed", "buggy")
_SOURCES = (LIVE_SOURCE, FROZEN_SOURCE)


class LuantiLazarusProbeError(RuntimeError):
    """Raised when a Luanti lazarus probe cannot produce usable evidence."""


@dataclass(frozen=True)
class LuantiLazarusAudit:
    """Verdicts derived from one observed version of a Luanti lazarus task."""

    complete: bool
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class LuantiProbeBinding:
    """How one task's registered replay is observed and read."""

    task_id: str
    change_number: int
    source: str
    replay_steps: tuple[str, ...]
    states: LuantiLazarusStates
    observe: Callable[[Path, str], Any] = field(repr=False)

    @property
    def adapter(self) -> str:
        return LIVE_ADAPTER if self.source == LIVE_SOURCE else FROZEN_ADAPTER


_FROZEN_REPLAY = (
    "restore_registered_candidate_start",
    "run_registered_candidate_replay",
    "read_admitted_gate10_semantic_observation",
)
_LIVE_REPLAY_14757 = (
    "start_candidate_binary_as_headless_devtest_server",
    "load_registered_worldmod_fixture",
    "deliver_registered_round_command_from_the_server",
    "read_semantic_result_from_the_candidate_log",
)


def build_luanti_bindings(
    artifact_root: str | Path,
    *,
    task_ids: Mapping[int, str],
    repository_root: str | Path | None = None,
) -> dict[str, LuantiProbeBinding]:
    """Bind every requested Luanti task to its strongest available observation.

    Frozen rows are bound from their admission closure.  An expansion row has no
    admission, so it is bound from its published live evidence instead; that
    needs the repository root, which the caller must supply when it asks for one.
    """

    from .lazarus_luanti_expansion import (
        LUANTI_EXPANSION_CHANGES,
        build_luanti_expansion_binding,
    )

    root = Path(artifact_root).absolute().resolve(strict=True)
    bindings: dict[str, LuantiProbeBinding] = {}
    for change_number, task_id in sorted(task_ids.items()):
        if change_number in LUANTI_EXPANSION_CHANGES:
            if repository_root is None:
                raise LuantiLazarusProbeError(
                    f"luanti#{change_number} is an expansion row and needs a "
                    "repository root to resolve its published evidence"
                )
            bindings[task_id] = build_luanti_expansion_binding(
                repository_root, change_number=change_number
            )
            continue
        states = extract_luanti_lazarus_states(root, change_number=change_number)
        if change_number == 14757:
            bindings[task_id] = LuantiProbeBinding(
                task_id=task_id,
                change_number=change_number,
                source=LIVE_SOURCE,
                replay_steps=_LIVE_REPLAY_14757,
                states=states,
                observe=_live_14757_observation,
            )
            continue
        bindings[task_id] = LuantiProbeBinding(
            task_id=task_id,
            change_number=change_number,
            source=FROZEN_SOURCE,
            replay_steps=_FROZEN_REPLAY,
            states=states,
            observe=_frozen_observation(states),
        )
    return bindings


def _frozen_observation(states: LuantiLazarusStates) -> Callable[[Path, str], Any]:
    def observe(root: Path, version: str) -> Any:
        return states.state(version)

    return observe


def _live_14757_observation(root: Path, version: str) -> Mapping[str, Any]:
    """Re-run #14757's committed headless server replay for one version."""

    module = importlib.import_module(
        "warptest.c1_lazarus_selection.luanti_14757_server_side_replay"
    )
    run = root / "private/lazarus-probe-runs/luanti/14757/server-side"
    run.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = run / version
    if target.exists():
        raise LuantiLazarusProbeError(
            f"#14757 probe run directory already exists: {target}"
        )
    binding = module._load_binding(root)
    spec = module._prepare_variant(
        root,
        run,
        binding,
        version,
        module._WATCHDOG_SECONDS,
        port=_probe_port(version),
    )
    observation = module._run_server(spec)
    if observation.action_command is None:
        raise LuantiLazarusProbeError(
            f"#14757 {version} replay reached no registered action"
        )
    return {
        "command": observation.action_command,
        "count": observation.action_count,
        "ok": observation.action_ok,
        "result": observation.semantic_result,
    }


def _probe_port(version: str) -> int:
    return 39457 if version == "fixed" else 39458


def run_luanti_lazarus_probe(
    artifact_root: str | Path,
    *,
    task_id: str,
    version: str,
    bindings: Mapping[str, LuantiProbeBinding],
) -> dict[str, Any]:
    """Observe one version of a Luanti lazarus task and return its audit."""

    if version not in _VERSIONS:
        raise LuantiLazarusProbeError("probe version must be fixed or buggy")
    binding = bindings.get(task_id)
    if binding is None:
        raise LuantiLazarusProbeError(f"no probe binding for {task_id}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    probe_errors: list[str] = []
    observed: Any = None
    try:
        observed = binding.observe(root, version)
    except Exception as error:  # noqa: BLE001 - reported as a probe error
        probe_errors.append(f"{type(error).__name__}: {error}")
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "task_id": binding.task_id,
        "candidate": {
            "repository_key": "luanti",
            "change_number": binding.change_number,
        },
        "variant": version,
        "observation_source": binding.source,
        "evidence_layout": binding.states.layout,
        "evidence_artifact_ids": list(binding.states.source_artifact_ids),
        "registered_replay": list(binding.replay_steps),
        "observed": observed,
        "expected_goal": binding.states.fixed,
        "expected_defect": binding.states.buggy,
        "probe_errors": probe_errors,
    }


def evaluate_luanti_lazarus_audit(value: Mapping[str, Any]) -> LuantiLazarusAudit:
    """Fail closed unless the audit names a known source and a separating pair."""

    failures: list[str] = []
    if not isinstance(value, Mapping):
        return _failure(["Luanti lazarus audit is not a mapping"])
    if value.get("schema_version") != AUDIT_SCHEMA_VERSION:
        failures.append("Luanti lazarus audit has an unknown schema version")
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("lz-luanti-"):
        failures.append("Luanti lazarus audit has no lazarus task id")
    if value.get("variant") not in _VERSIONS:
        failures.append("Luanti lazarus audit has no materialized version")
    if value.get("observation_source") not in _SOURCES:
        failures.append("Luanti lazarus audit has no recognised observation source")
    evidence = value.get("evidence_artifact_ids")
    if not isinstance(evidence, Sequence) or isinstance(evidence, str) or not evidence:
        failures.append("Luanti lazarus audit names no evidence artifact")
    replay = value.get("registered_replay")
    if not isinstance(replay, Sequence) or isinstance(replay, str) or not replay:
        failures.append("Luanti lazarus audit has no registered replay")
    errors = value.get("probe_errors")
    if not isinstance(errors, Sequence) or isinstance(errors, str):
        failures.append("Luanti lazarus audit has no probe error list")
        errors = None
    observed = value.get("observed")
    goal = value.get("expected_goal")
    defect = value.get("expected_defect")
    for name, payload in (
        ("observed", observed),
        ("expected_goal", goal),
        ("expected_defect", defect),
    ):
        if payload in (None, "", {}):
            failures.append(f"Luanti lazarus audit has no {name} state")
    if goal is not None and defect is not None and goal == defect:
        failures.append("Luanti lazarus audit cannot separate goal from defect")
    if failures:
        return _failure(failures)
    return LuantiLazarusAudit(
        complete=True,
        goal_checks_ok=observed == goal,
        defect_signature_ok=observed == defect,
        invariants_ok=not list(errors or []),
    )


def evaluate_luanti_lazarus_checks(
    checks: Sequence[Mapping[str, Any]], audit: Mapping[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    """Resolve one spec check group against a Luanti audit."""

    return evaluate_state_checks(
        checks, audit, audit_check=STATE_AUDIT_CHECK, error_check=PROBE_ERROR_CHECK
    )


def luanti_lazarus_goal_checks(binding: LuantiProbeBinding) -> list[dict[str, Any]]:
    """Project the admitted goal state into spec ``goal_checks`` entries."""

    return project_state_checks(binding.states.fixed, audit_check=STATE_AUDIT_CHECK)


def luanti_lazarus_defect_signature(
    binding: LuantiProbeBinding,
) -> list[dict[str, Any]]:
    """Project the admitted defect state into spec ``defect_signature`` entries."""

    return project_state_checks(binding.states.buggy, audit_check=STATE_AUDIT_CHECK)


def _failure(failures: Sequence[str]) -> LuantiLazarusAudit:
    return LuantiLazarusAudit(
        complete=False,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        failures=tuple(failures),
    )


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "CHECKPOINT_STRATEGY",
    "FROZEN_ADAPTER",
    "FROZEN_SOURCE",
    "LIVE_ADAPTER",
    "LIVE_SOURCE",
    "LuantiLazarusAudit",
    "LuantiLazarusProbeError",
    "LuantiProbeBinding",
    "build_luanti_bindings",
    "evaluate_luanti_lazarus_audit",
    "evaluate_luanti_lazarus_checks",
    "luanti_lazarus_defect_signature",
    "luanti_lazarus_goal_checks",
    "run_luanti_lazarus_probe",
]
