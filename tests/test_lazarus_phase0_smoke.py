from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from warptest import lazarus_phase0_smoke as target
from warptest.lazarus_orchestrator import PHASE0_DECISION_MODEL


TASK_ID = "lz-luanti-14757-math-round"


@dataclass
class _Observation:
    complete: bool = True
    runtime_restored: bool = True
    before_semantic_ok: bool = True
    goal_checks_ok: bool | None = None
    defect_signature_ok: bool | None = None
    invariants_ok: bool = True
    synthesis_cache_hit: bool = False
    actual_synthesis_calls: int = 1
    checkpoint_reuse_success: bool | None = None
    treatment_tokens: int = 128
    treatment_time_ms: float = 1200.0


@dataclass
class _Artifact:
    version: str


@dataclass
class _Pair:
    buggy: _Artifact
    fixed: _Artifact
    version_switch_ms: float = 1.0


@dataclass
class _Preflight:
    complete: bool
    tasks: list[dict[str, Any]]
    pairs: dict[str, _Pair]
    agent_sources: dict[str, object]
    per_trial_p2p_ids: dict[str, tuple[str, ...]]
    failures: tuple[str, ...] = ()


def _preflight(complete: bool = True) -> _Preflight:
    return _Preflight(
        complete=complete,
        tasks=[
            {
                "id": TASK_ID,
                "benchmark": "luanti",
                "defect": {"defect_locus": "at_goal"},
            }
        ],
        pairs={TASK_ID: _Pair(buggy=_Artifact("buggy"), fixed=_Artifact("fixed"))},
        agent_sources={TASK_ID: object()},
        per_trial_p2p_ids={TASK_ID: ("luanti-pr14757-formspec-escape-p2p",)},
    )


def _install(monkeypatch: pytest.MonkeyPatch, preflight: _Preflight) -> None:
    monkeypatch.setattr(target, "preflight_lazarus_phase0", lambda root: preflight)


def _separating_runner(calls: list[Any]) -> Any:
    def run(context: Any) -> _Observation:
        calls.append(context)
        if context.key.version == "fixed":
            return _Observation(
                goal_checks_ok=True,
                defect_signature_ok=False,
                synthesis_cache_hit=True,
                actual_synthesis_calls=0,
                checkpoint_reuse_success=True,
            )
        return _Observation(goal_checks_ok=False, defect_signature_ok=True)

    return run


def test_smoke_runs_buggy_then_fixed_and_records_the_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _preflight())
    calls: list[Any] = []
    report = tmp_path / "reports/smoke.json"

    smoke = target.run_lazarus_phase0_smoke(
        tmp_path,
        task_id=TASK_ID,
        arm="lazarus_state",
        report_path=report,
        runtime_factory=_separating_runner(calls),
    )

    assert [context.key.version for context in calls] == ["buggy", "fixed"]
    assert [context.key.model for context in calls] == [PHASE0_DECISION_MODEL] * 2
    assert smoke.complete is True
    assert smoke.separates_goal_from_defect is True

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema"] == target.SMOKE_SCHEMA
    assert payload["is_phase0_run"] is False
    assert payload["authorized_population"] == "none"
    assert payload["provider_calls_performed"] is True
    assert payload["separates_goal_from_defect"] is True
    assert [cell["version"] for cell in payload["cells"]] == ["buggy", "fixed"]
    assert payload["cells"][1]["synthesis_cache_hit"] is True
    assert payload["cells"][1]["actual_synthesis_calls"] == 0


def test_smoke_reports_a_non_separating_pair_without_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _preflight())

    def run(context: Any) -> _Observation:
        return _Observation(goal_checks_ok=True, defect_signature_ok=False)

    smoke = target.run_lazarus_phase0_smoke(
        tmp_path,
        task_id=TASK_ID,
        report_path=tmp_path / "smoke.json",
        runtime_factory=run,
    )
    assert smoke.complete is True
    assert smoke.separates_goal_from_defect is False


def test_smoke_refuses_an_incomplete_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _preflight(complete=False)
    preflight.failures = ("gate report is not passed",)
    _install(monkeypatch, preflight)
    with pytest.raises(target.LazarusSmokeError, match="complete preflight"):
        target.run_lazarus_phase0_smoke(
            tmp_path,
            task_id=TASK_ID,
            report_path=tmp_path / "smoke.json",
            runtime_factory=lambda context: _Observation(),
        )


def test_smoke_refuses_a_task_absent_from_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _preflight())
    with pytest.raises(target.LazarusSmokeError, match="absent from the verified"):
        target.run_lazarus_phase0_smoke(
            tmp_path,
            task_id="lz-luanti-11919-vector-from-string",
            report_path=tmp_path / "smoke.json",
            runtime_factory=lambda context: _Observation(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"arm": "c1_state"},
        {"seed": -1},
        {"seed": True},
    ],
)
def test_smoke_refuses_unregistered_arms_and_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    _install(monkeypatch, _preflight())
    with pytest.raises(target.LazarusSmokeError):
        target.run_lazarus_phase0_smoke(
            tmp_path,
            task_id=TASK_ID,
            report_path=tmp_path / "smoke.json",
            runtime_factory=lambda context: _Observation(),
            **overrides,
        )


def test_smoke_refuses_a_credential_source_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _preflight())
    with pytest.raises(target.LazarusSmokeError, match="escapes the workspace"):
        target.run_lazarus_phase0_smoke(
            tmp_path,
            task_id=TASK_ID,
            report_path=tmp_path / "smoke.json",
            credential_source="/etc/hosts",
            runtime_factory=lambda context: _Observation(),
        )
