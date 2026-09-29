"""Executable bindings for JupyterLab Lazarus rows driven as a whole application.

:mod:`warptest.lazarus_jupyterlab_probe` binds the frozen rows, whose replay runs
against a candidate-local Rspack bundle in Microsoft Edge.  The expansion rows
are observed differently: their replay drives the *materialized application* --
the version's own ``dev_mode`` bundle served by its own JupyterLab -- in
Chromium.  That surface needs its own binding, audit and check dispatcher, which
is what this module supplies:

* :func:`run_jupyterlab_app_lazarus_probe` replays one materialized version and
  returns an audit mapping;
* :func:`evaluate_jupyterlab_app_lazarus_audit` turns that mapping into goal,
  defect and invariant verdicts, refusing anything it cannot evidence;
* :func:`evaluate_jupyterlab_app_lazarus_checks` resolves a spec's check groups
  against the audit.

The registered replay itself lives in :mod:`warptest.lazarus_jupyterlab_app`, so
a trial and a golden capture cannot drift apart: both drive the same steps and
read the same observable.

The probe never calls a model provider.  The replay is a fixed sequence and the
oracle is the registered expectation, so it is reproducible offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .lazarus_jupyterlab_app import (
    JUPYTERLAB_APP_PROFILES,
    JupyterLabAppProfile,
    drive_registered_replay,
    prepared_version_clone,
)
from .lazarus_state_checks import evaluate_state_checks, project_state_checks


AUDIT_SCHEMA_VERSION = 1
APP_AUDIT_CHECK = "jupyterlab_lazarus_app_audit"
PROBE_ERROR_CHECK = "no_jupyterlab_probe_errors"
PROBE_ADAPTER = "jupyterlab_lazarus_app_probe_v1"
PROBE_STRATEGY = "jupyterlab_lazarus_app_replay_validate"
TARGET_KIND = "jupyterlab_lazarus_application"
_RUNTIME = "materialized_dev_mode_bundle"
_BROWSER_CHANNEL = "chromium"
_VERSIONS = ("fixed", "buggy")


class JupyterLabAppProbeError(RuntimeError):
    """Raised when the app probe cannot produce usable evidence."""


@dataclass(frozen=True)
class JupyterLabAppAudit:
    """Verdicts derived from one replayed version of a JupyterLab app task."""

    complete: bool
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class JupyterLabAppBinding:
    """How one task's registered replay is prepared, executed and read."""

    task_id: str
    change_number: int
    audit_field: str
    replay_steps: tuple[str, ...]
    expected_after: Mapping[str, Mapping[str, Any]]
    expected_start: Mapping[str, Any]
    observation: Callable[[str], Mapping[str, Any]] = field(repr=False)

    def expected(self, version: str) -> dict[str, Any]:
        return dict(self.expected_after[version])


def build_jupyterlab_app_binding(
    profile: JupyterLabAppProfile,
    *,
    prebuilt_root: str | Path,
    sandbox_root: str | Path,
    build_timeout_s: float = 3_600.0,
) -> JupyterLabAppBinding:
    """Bind one task to a live replay of its own materialized application."""

    prebuilt = Path(prebuilt_root).expanduser().resolve()
    sandbox = Path(sandbox_root).expanduser().resolve()

    def observation(version: str) -> Mapping[str, Any]:
        source = prebuilt / profile.task_id / version
        if not source.is_dir():
            raise JupyterLabAppProbeError(
                f"{profile.task_id} {version} is not materialized: {source}"
            )
        # The served bundle is built before the pair is materialized, so each
        # version must be rebuilt or the browser would load the same
        # application for both.
        clone = prepared_version_clone(
            profile.task_id,
            version,
            source=source,
            prebuilt_root=prebuilt,
            clone=sandbox / "versions" / f"{profile.task_id}-{version}",
            timeout_s=build_timeout_s,
        )
        return drive_registered_replay(
            profile,
            clone,
            sandbox / "runs" / f"{profile.task_id}-{version}",
            version,
        )

    return JupyterLabAppBinding(
        task_id=profile.task_id,
        change_number=profile.change_number,
        audit_field=profile.audit_field,
        replay_steps=tuple(profile.registered_replay),
        expected_after={
            "fixed": {profile.audit_field: profile.goal_value},
            "buggy": {profile.audit_field: profile.defect_value},
        },
        expected_start={profile.start_field: profile.start_value},
        observation=observation,
    )


def app_probe_bindings(
    *,
    prebuilt_root: str | Path,
    sandbox_root: str | Path,
    build_timeout_s: float = 3_600.0,
) -> dict[str, JupyterLabAppBinding]:
    """Return every JupyterLab row whose application replay can be re-executed."""

    return {
        task_id: build_jupyterlab_app_binding(
            profile,
            prebuilt_root=prebuilt_root,
            sandbox_root=sandbox_root,
            build_timeout_s=build_timeout_s,
        )
        for task_id, profile in JUPYTERLAB_APP_PROFILES.items()
    }


def run_jupyterlab_app_lazarus_probe(
    *,
    task_id: str,
    version: str,
    bindings: Mapping[str, JupyterLabAppBinding],
) -> dict[str, Any]:
    """Replay one materialized version and return its audit mapping."""

    if version not in _VERSIONS:
        raise JupyterLabAppProbeError("probe version must be fixed or buggy")
    binding = bindings.get(task_id)
    if binding is None:
        raise JupyterLabAppProbeError(f"no probe binding for {task_id}")
    probe_errors: list[str] = []
    stages: Mapping[str, Any] = {}
    try:
        stages = binding.observation(version)
    except Exception as error:  # noqa: BLE001 - reported as a probe error
        probe_errors.append(f"{type(error).__name__}: {error}")
    observed: dict[str, Any] | None = None
    start: dict[str, Any] | None = None
    browser_version = None
    if stages:
        browser_version = stages.get("browser_version")
        after = stages.get("after") or {}
        before = stages.get("before") or {}
        if after:
            observed = {binding.audit_field: after.get("observed")}
        if before:
            start = {
                "field": before.get("field"),
                "observed": before.get("observed"),
                "expected": before.get("expected"),
            }
        for stage, values in (("before", before), ("after", after)):
            errors = values.get("errors") or {}
            for channel in ("console_errors", "page_errors"):
                for message in errors.get(channel, ()):
                    probe_errors.append(f"{stage} {channel}: {message}")
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "task_id": binding.task_id,
        "candidate": {
            "repository_key": "jupyterlab",
            "change_number": binding.change_number,
        },
        "variant": version,
        "runtime": _RUNTIME,
        "browser_channel": _BROWSER_CHANNEL,
        "browser_version": browser_version,
        "registered_replay": list(binding.replay_steps),
        "start": start,
        "expected_start": dict(binding.expected_start),
        "observed": observed,
        "expected_goal": binding.expected("fixed"),
        "expected_defect": binding.expected("buggy"),
        "probe_errors": probe_errors,
    }


def evaluate_jupyterlab_app_lazarus_audit(
    value: Mapping[str, Any],
) -> JupyterLabAppAudit:
    """Fail closed unless a real application replay evidenced the reported state."""

    failures: list[str] = []
    if not isinstance(value, Mapping):
        return _failure(["JupyterLab app audit is not a mapping"])
    if value.get("schema_version") != AUDIT_SCHEMA_VERSION:
        failures.append("JupyterLab app audit has an unknown schema version")
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("lz-jupyterlab-"):
        failures.append("JupyterLab app audit has no lazarus task id")
    if value.get("variant") not in _VERSIONS:
        failures.append("JupyterLab app audit has no materialized version")
    if value.get("runtime") != _RUNTIME:
        failures.append("JupyterLab app audit did not drive the materialized bundle")
    if value.get("browser_channel") != _BROWSER_CHANNEL:
        failures.append("JupyterLab app audit was not captured in Chromium")
    if not _nonempty_sequence(value.get("registered_replay")):
        failures.append("JupyterLab app audit has no registered replay")
    errors = value.get("probe_errors")
    if not isinstance(errors, Sequence) or isinstance(errors, str):
        failures.append("JupyterLab app audit has no probe error list")
        errors = None
    # An observation taken from an unverified start is not evidence about the
    # replay, so the start is graded before the outcome is trusted.
    start = value.get("start")
    if not isinstance(start, Mapping) or "observed" not in start:
        failures.append("JupyterLab app audit recorded no start observation")
    elif start.get("observed") != start.get("expected"):
        failures.append(
            "JupyterLab app audit did not reach the registered start: "
            f"{start.get('observed')!r} is not {start.get('expected')!r}"
        )
    observed = value.get("observed")
    goal = value.get("expected_goal")
    defect = value.get("expected_defect")
    for name, payload in (
        ("observed", observed),
        ("expected_goal", goal),
        ("expected_defect", defect),
    ):
        if payload in (None, "", {}):
            failures.append(f"JupyterLab app audit has no {name} state")
    if goal is not None and defect is not None and goal == defect:
        failures.append("JupyterLab app audit cannot separate goal from defect")
    if failures:
        return _failure(failures)
    return JupyterLabAppAudit(
        complete=True,
        goal_checks_ok=observed == goal,
        defect_signature_ok=observed == defect,
        invariants_ok=not list(errors or []),
    )


def evaluate_jupyterlab_app_lazarus_checks(
    checks: Sequence[Mapping[str, Any]], audit: Mapping[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    """Resolve one spec check group against an audit, refusing unknown types."""

    return evaluate_state_checks(
        checks, audit, audit_check=APP_AUDIT_CHECK, error_check=PROBE_ERROR_CHECK
    )


def jupyterlab_app_goal_checks(
    binding: JupyterLabAppBinding,
) -> list[dict[str, Any]]:
    """Project the registered goal state into spec ``goal_checks`` entries."""

    return project_state_checks(binding.expected("fixed"), audit_check=APP_AUDIT_CHECK)


def jupyterlab_app_defect_signature(
    binding: JupyterLabAppBinding,
) -> list[dict[str, Any]]:
    """Project the registered defect state into spec ``defect_signature`` entries."""

    return project_state_checks(binding.expected("buggy"), audit_check=APP_AUDIT_CHECK)


def _nonempty_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str) and bool(value)


def _failure(failures: Sequence[str]) -> JupyterLabAppAudit:
    return JupyterLabAppAudit(
        complete=False,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        failures=tuple(failures),
    )


__all__ = [
    "APP_AUDIT_CHECK",
    "AUDIT_SCHEMA_VERSION",
    "JupyterLabAppAudit",
    "JupyterLabAppBinding",
    "JupyterLabAppProbeError",
    "PROBE_ADAPTER",
    "PROBE_ERROR_CHECK",
    "PROBE_STRATEGY",
    "TARGET_KIND",
    "app_probe_bindings",
    "build_jupyterlab_app_binding",
    "evaluate_jupyterlab_app_lazarus_audit",
    "evaluate_jupyterlab_app_lazarus_checks",
    "jupyterlab_app_defect_signature",
    "jupyterlab_app_goal_checks",
    "run_jupyterlab_app_lazarus_probe",
]
