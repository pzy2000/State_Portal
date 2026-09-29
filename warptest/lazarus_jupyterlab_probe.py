"""Deterministic JupyterLab lazarus probe and its fail-closed check evaluator.

A JupyterLab lazarus task states its goal and its defect signature as observations
of a real browser surface: the candidate's own production module is bundled with
the candidate-local Rspack and driven in Microsoft Edge through Playwright, and the
probe reports the semantic state that surface reached.

This module supplies the three pieces a task spec needs to be executable:

* :func:`run_jupyterlab_lazarus_probe` replays one materialized version and returns
  an audit mapping plus the frames it captured;
* :func:`evaluate_jupyterlab_lazarus_audit` turns that mapping into goal, defect and
  invariant verdicts, refusing anything it cannot evidence;
* :func:`evaluate_jupyterlab_lazarus_checks` resolves a spec's ``goal_checks``,
  ``defect_signature`` and ``invariant_checks`` entries against the audit.

The probe never calls a model provider: the replay is a fixed sequence and the
oracle is the registered expectation, so it is reproducible offline.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .lazarus_state_checks import evaluate_state_checks, project_state_checks
from .c1_lazarus_selection.jupyterlab_real_edge_closure import (
    RealEdgeClosureSpec,
    run_harness_observation,
    run_variant_observation,
)


AUDIT_SCHEMA_VERSION = 1
DOM_AUDIT_CHECK = "jupyterlab_lazarus_dom_audit"
PROBE_ERROR_CHECK = "no_jupyterlab_probe_errors"
PROBE_ADAPTER = "jupyterlab_lazarus_edge_probe_v1"
PROBE_STRATEGY = "jupyterlab_lazarus_edge_replay_validate"
_VARIANTS = ("fixed", "buggy")
_SHARED_SPEC_CHANGES = (18518, 18579, 18662, 18992, 19121, 19136, 19227)
_BESPOKE_CHANGES = (17700, 18572, 18938)


class JupyterLabLazarusProbeError(RuntimeError):
    """Raised when a JupyterLab lazarus probe cannot produce usable evidence."""


@dataclass(frozen=True)
class JupyterLabLazarusAudit:
    """Verdicts derived from one replayed version of a JupyterLab lazarus task."""

    complete: bool
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class JupyterLabProbeBinding:
    """How one task's registered replay is executed and read."""

    task_id: str
    change_number: int
    replay_steps: tuple[str, ...]
    expected_after: Mapping[str, Mapping[str, Any] | str]
    semantic_state: Callable[[Mapping[str, Any]], Any] = field(repr=False)
    observation: Callable[[Path, str], Mapping[str, Any]] = field(repr=False)

    def expected(self, variant: str) -> Any:
        value = self.expected_after[variant]
        return dict(value) if isinstance(value, Mapping) else value


def _probe_run_root(change_number: int, epoch: str) -> PurePosixPath:
    """Keep probe runs out of the frozen bounded-run directories."""

    return PurePosixPath(
        f"private/lazarus-probe-runs/jupyterlab/{change_number}/{epoch}"
    )


def _shared_binding(task_id: str, change_number: int) -> JupyterLabProbeBinding:
    """Bind a task whose closure specification already describes its replay."""

    module = importlib.import_module(
        f"warptest.c1_lazarus_selection.jupyterlab_{change_number}_downstream"
    )
    spec: RealEdgeClosureSpec = module.SPEC

    def observation(root: Path, variant: str) -> Mapping[str, Any]:
        return run_variant_observation(
            root,
            spec=spec,
            variant=variant,
            run_root=_probe_run_root(change_number, spec.epoch),
        )

    return JupyterLabProbeBinding(
        task_id=task_id,
        change_number=change_number,
        replay_steps=tuple(spec.replay_steps),
        expected_after={variant: spec.expected_after[variant] for variant in _VARIANTS},
        semantic_state=spec.semantic_state,
        observation=observation,
    )


def _bespoke_binding(
    task_id: str,
    change_number: int,
    *,
    replay_steps: tuple[str, ...],
    expected_after: Mapping[str, Any],
    semantic_state: Callable[[Mapping[str, Any]], Any],
    production_module: str,
    epoch: str,
) -> JupyterLabProbeBinding:
    """Bind a task whose closure module owns a bespoke candidate-local harness."""

    module = importlib.import_module(
        f"warptest.c1_lazarus_selection.jupyterlab_{change_number}_downstream"
    )
    writer = getattr(module, "_write_harness", None)
    if writer is None:
        raise JupyterLabLazarusProbeError(
            f"{module.__name__} exposes no candidate-local harness"
        )
    marker = _marker_name(module)
    trees = getattr(module, "_TREES", None)
    if trees is None:
        raise JupyterLabLazarusProbeError(
            f"{module.__name__} exposes no materialized tree path"
        )

    def observation(root: Path, variant: str) -> Mapping[str, Any]:
        return run_harness_observation(
            root,
            change_number=change_number,
            variant=variant,
            trees=trees,
            run_root=_probe_run_root(change_number, epoch),
            required_tree_files=(production_module,),
            harness_files=lambda run, tree: writer(run, tree) or {},
            observation_marker=marker,
        )

    return JupyterLabProbeBinding(
        task_id=task_id,
        change_number=change_number,
        replay_steps=replay_steps,
        expected_after=expected_after,
        semantic_state=semantic_state,
        observation=observation,
    )


def _marker_name(module: Any) -> str:
    pattern = getattr(module, "_MARKER", None)
    if pattern is None:
        raise JupyterLabLazarusProbeError(
            f"{module.__name__} exposes no observation marker"
        )
    return pattern.pattern.decode("ascii").split("=")[0]


def evaluate_jupyterlab_lazarus_audit(
    value: Mapping[str, Any],
) -> JupyterLabLazarusAudit:
    """Fail closed unless a real browser replay evidenced the reported state."""

    failures: list[str] = []
    if not isinstance(value, Mapping):
        return _failure(["JupyterLab lazarus audit is not a mapping"])
    if value.get("schema_version") != AUDIT_SCHEMA_VERSION:
        failures.append("JupyterLab lazarus audit has an unknown schema version")
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("lz-jupyterlab-"):
        failures.append("JupyterLab lazarus audit has no lazarus task id")
    variant = value.get("variant")
    if variant not in _VARIANTS:
        failures.append("JupyterLab lazarus audit has no materialized version")
    if value.get("browser_channel") != "msedge":
        failures.append("JupyterLab lazarus audit was not captured in Microsoft Edge")
    version = value.get("browser_version")
    if not isinstance(version, str) or not version:
        failures.append("JupyterLab lazarus audit has no browser version")
    replay = value.get("registered_replay")
    if not isinstance(replay, Sequence) or isinstance(replay, str) or not replay:
        failures.append("JupyterLab lazarus audit has no registered replay")
    frames = value.get("frames")
    if not isinstance(frames, Mapping) or not {"before", "after"} <= set(frames):
        failures.append("JupyterLab lazarus audit has no before/after frames")
    errors = value.get("probe_errors")
    if not isinstance(errors, Sequence) or isinstance(errors, str):
        failures.append("JupyterLab lazarus audit has no probe error list")
        errors = None
    observed = value.get("observed")
    goal = value.get("expected_goal")
    defect = value.get("expected_defect")
    for name, payload in (
        ("observed", observed),
        ("expected_goal", goal),
        ("expected_defect", defect),
    ):
        if payload is None or payload == {}:
            failures.append(f"JupyterLab lazarus audit has no {name} state")
    if goal is not None and defect is not None and goal == defect:
        failures.append("JupyterLab lazarus audit cannot separate goal from defect")
    if failures:
        return _failure(failures)
    invariants_ok = not list(errors or [])
    return JupyterLabLazarusAudit(
        complete=True,
        goal_checks_ok=observed == goal,
        defect_signature_ok=observed == defect,
        invariants_ok=invariants_ok,
    )


def evaluate_jupyterlab_lazarus_checks(
    checks: Sequence[Mapping[str, Any]], audit: Mapping[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    """Resolve one spec check group against an audit, refusing unknown types."""

    return evaluate_state_checks(
        checks, audit, audit_check=DOM_AUDIT_CHECK, error_check=PROBE_ERROR_CHECK
    )


def run_jupyterlab_lazarus_probe(
    artifact_root: str | Path,
    *,
    task_id: str,
    variant: str,
    bindings: Mapping[str, JupyterLabProbeBinding] | None = None,
) -> dict[str, Any]:
    """Replay one materialized version and return its audit mapping."""

    if variant not in _VARIANTS:
        raise JupyterLabLazarusProbeError("probe variant must be fixed or buggy")
    registry = bindings if bindings is not None else probe_bindings()
    binding = registry.get(task_id)
    if binding is None:
        raise JupyterLabLazarusProbeError(f"no probe binding for {task_id}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    probe_errors: list[str] = []
    try:
        data = binding.observation(root, variant)
    except Exception as error:  # noqa: BLE001 - reported as a probe error
        probe_errors.append(f"{type(error).__name__}: {error}")
        data = {}
    observed: Any = None
    if data:
        try:
            observed = binding.semantic_state(data)
        except Exception as error:  # noqa: BLE001 - reported as a probe error
            probe_errors.append(f"semantic read failed: {error}")
    frames = dict(data.get("frame_paths") or data.get("frames") or {})
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "task_id": binding.task_id,
        "candidate": {
            "repository_key": "jupyterlab",
            "change_number": binding.change_number,
        },
        "variant": variant,
        "browser_channel": data.get("browser_channel"),
        "browser_version": data.get("browser_version"),
        "registered_replay": list(binding.replay_steps),
        "observed": observed,
        "expected_goal": binding.expected("fixed"),
        "expected_defect": binding.expected("buggy"),
        "frames": frames,
        "probe_errors": probe_errors,
    }


def probe_bindings() -> dict[str, JupyterLabProbeBinding]:
    """Return every task whose registered replay can be re-executed offline."""

    from .c1_lazarus_selection.c1_lazarus_golden_publication import _TASK_IDS

    bindings: dict[str, JupyterLabProbeBinding] = {}
    for change in _SHARED_SPEC_CHANGES:
        task_id = _TASK_IDS[("jupyterlab", change)]
        bindings[task_id] = _shared_binding(task_id, change)
    for change in _BESPOKE_CHANGES:
        task_id = _TASK_IDS[("jupyterlab", change)]
        bindings[task_id] = _bespoke_binding(
            task_id, change, **_BESPOKE_DETAILS[change]
        )
    task_id = _TASK_IDS[("jupyterlab", 17369)]
    bindings[task_id] = _harness_module_binding(task_id)
    return bindings


def _harness_module_binding(task_id: str) -> JupyterLabProbeBinding:
    """Bind #17369 to the probe-owned harness that restates its frozen contract."""

    from .c1_lazarus_selection import jupyterlab_17369_probe as harness

    def observation(root: Path, variant: str) -> Mapping[str, Any]:
        return run_harness_observation(
            root,
            change_number=harness.CHANGE,
            variant=variant,
            trees=harness.TREES,
            run_root=_probe_run_root(harness.CHANGE, "stream-merge-v1"),
            required_tree_files=(harness.PRODUCTION_MODULE,),
            harness_files=harness.write_harness,
            observation_marker=harness.MARKER,
        )

    return JupyterLabProbeBinding(
        task_id=task_id,
        change_number=harness.CHANGE,
        replay_steps=harness.REPLAY_STEPS,
        expected_after=harness.EXPECTED_AFTER,
        semantic_state=harness.semantic_state,
        observation=observation,
    )


def _statusbar_semantics(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "first_tab_stop": (list(data.get("tab_sequence") or []) or [None])[0],
        "tab_sequence": list(data.get("tab_sequence") or []),
        "rendered_order": list(data.get("visual_order") or []),
    }


def _highlight_semantics(data: Mapping[str, Any]) -> dict[str, Any]:
    return {"rendered_text": data.get("after")}


def _dialog_semantics(data: Mapping[str, Any]) -> dict[str, Any]:
    settled = data.get("after_settled")
    settled = dict(settled) if isinstance(settled, Mapping) else {}
    return {
        "open_dialog_count": data.get("after_dialog_count"),
        "promise_settled": settled.get("settled"),
    }


_BESPOKE_DETAILS: dict[int, dict[str, Any]] = {
    17700: {
        "production_module": "packages/statusbar/lib/statusbar.js",
        "epoch": "real-tab-v1",
        "replay_steps": (
            "load_candidate_local_bundle",
            "register_three_right_aligned_items_by_ascending_rank",
            "press_real_tab_three_times",
            "read_candidate_ordered_focus_targets",
        ),
        "expected_after": {
            "fixed": {
                "first_tab_stop": "line-column",
                "tab_sequence": ["line-column", "kernel-status", "notifications"],
                "rendered_order": ["line-column", "kernel-status", "notifications"],
            },
            "buggy": {
                "first_tab_stop": "notifications",
                "tab_sequence": ["notifications", "kernel-status", "line-column"],
                "rendered_order": ["line-column", "kernel-status", "notifications"],
            },
        },
        "semantic_state": _statusbar_semantics,
    },
    18572: {
        "production_module": "packages/codemirror/lib/language.js",
        "epoch": "current-v3",
        "replay_steps": (
            "load_candidate_local_bundle",
            "restore_empty_registered_start",
            "invoke_editor_language_registry_highlight",
            "read_candidate_mutated_output_text",
        ),
        "expected_after": {
            "fixed": {"rendered_text": "x"},
            "buggy": {"rendered_text": ""},
        },
        "semantic_state": _highlight_semantics,
    },
    18938: {
        "production_module": "packages/apputils/lib/dialog.js",
        "epoch": "real-dialog-v3",
        "replay_steps": (
            "load_candidate_local_bundle",
            "show_dialog_with_a_throwing_body",
            "click_ok_with_real_mouse_input",
            "read_dialog_presence_and_promise_settlement",
        ),
        "expected_after": {
            "fixed": {"open_dialog_count": 0, "promise_settled": "rejected"},
            "buggy": {"open_dialog_count": 1, "promise_settled": None},
        },
        "semantic_state": _dialog_semantics,
    },
}


def jupyterlab_lazarus_goal_checks(
    binding: JupyterLabProbeBinding,
) -> list[dict[str, Any]]:
    """Project the registered goal state into spec ``goal_checks`` entries."""

    return project_state_checks(binding.expected("fixed"), audit_check=DOM_AUDIT_CHECK)


def jupyterlab_lazarus_defect_signature(
    binding: JupyterLabProbeBinding,
) -> list[dict[str, Any]]:
    """Project the registered defect state into spec ``defect_signature`` entries."""

    return project_state_checks(binding.expected("buggy"), audit_check=DOM_AUDIT_CHECK)


def _failure(failures: Sequence[str]) -> JupyterLabLazarusAudit:
    return JupyterLabLazarusAudit(
        complete=False,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        failures=tuple(failures),
    )


def audit_to_json(audit: Mapping[str, Any]) -> str:
    return json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "DOM_AUDIT_CHECK",
    "JupyterLabLazarusAudit",
    "JupyterLabLazarusProbeError",
    "JupyterLabProbeBinding",
    "PROBE_ADAPTER",
    "PROBE_ERROR_CHECK",
    "PROBE_STRATEGY",
    "audit_to_json",
    "evaluate_jupyterlab_lazarus_audit",
    "evaluate_jupyterlab_lazarus_checks",
    "jupyterlab_lazarus_defect_signature",
    "jupyterlab_lazarus_goal_checks",
    "probe_bindings",
    "run_jupyterlab_lazarus_probe",
]
