"""The state arm's Goal must be graded by the registered oracle, not the model.

Before this contract the candidate supplied its own ``assertions``, so a weak
spec could grade itself as a success. On the native backends that hole was
plugged by rejecting any spec that was not byte-identical to the oracle, which
also destroyed the Start datapoint. These tests pin the replacement: the oracle
grades the goal, and a divergent spec is measured rather than pre-rejected.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from warptest import state_loop
from warptest.models import PhaseResult
from warptest.state_store import RestoreOutcome, StateSession, StateStore


ORACLE_ASSERTION = {"type": "oracle_check", "expected": "42"}
MODEL_ASSERTION = {"type": "trivially_true"}
ORACLE_SPEC = {
    "benchmark": "2048",
    "target": {"score": 42},
    "assertions": [ORACLE_ASSERTION],
}


def _task() -> state_loop.StateLoopTask:
    from warptest.task_schema import PublicIssue

    return state_loop.StateLoopTask(
        issue=PublicIssue("task-1", "2048", "games/2048", "issue"),
        oracle_spec="oracle.yaml",
        state_description="issue",
    )


# --- the pre-restore gate no longer pre-rejects a divergent spec ----------


def test_benchmark_gate_accepts_a_spec_that_diverges_from_the_oracle() -> None:
    spec = {
        "benchmark": "2048",
        "target": {"score": 999},
        "actions": [{"type": "move"}],
        "assertions": [MODEL_ASSERTION],
        "checkpoint": {},
    }

    assert (
        state_loop._native_state_benchmark_errors(
            spec, oracle_spec=ORACLE_SPEC, task=_task()
        )
        == []
    )


def test_benchmark_gate_still_rejects_another_game() -> None:
    spec = {"benchmark": "luanti", "target": {}, "actions": [], "assertions": []}

    assert state_loop._native_state_benchmark_errors(
        spec, oracle_spec=ORACLE_SPEC, task=_task()
    ) == ["generated_spec benchmark does not match the registered task"]


def test_usable_native_spec_no_longer_requires_the_oracle_target_kind() -> None:
    """A wrong-but-well-formed target is measured at Start, not pre-rejected."""

    spec = {
        "name": "candidate",
        "benchmark": "2048",
        "target": {"kind": "impress_presentation"},
        "checkpoint": {},
        "actions": [],
        "assertions": [],
    }

    assert state_loop._usable_native_state_model_spec(
        spec, task=_task(), oracle_spec={"target": {"kind": "calc_spreadsheet"}}
    )


# --- the web goal path grades with the oracle and keeps the snapshot ------


class _FakeProfile:
    """Minimal profile exposing the surface fast_restore depends on."""

    def __init__(self, *, oracle_ok: bool) -> None:
        self.oracle_ok = oracle_ok
        self.seen_assertions: list[list[dict[str, object]]] = []

    def supports_snapshot(self) -> bool:
        return True

    def inject_runtime_state(self, runtime: object, snapshot: object) -> None:
        return None

    def _validate(self, runtime: object, target: dict[str, object]) -> PhaseResult:
        return PhaseResult("success", "target restored")

    def _run_actions(self, runtime: object, actions: list[dict[str, object]]) -> PhaseResult:
        return PhaseResult("success", f"ran {len(actions)}")

    def _assert_oracles(
        self, runtime: object, assertions: list[dict[str, object]]
    ) -> PhaseResult:
        self.seen_assertions.append(list(assertions))
        if self.oracle_ok:
            return PhaseResult("success", "oracle satisfied")
        return PhaseResult("failure", "oracle assertions failed")

    def capture_runtime_state(self, runtime: object) -> dict[str, object]:
        return {"snapshot": True}


def _session(profile: _FakeProfile) -> StateSession:
    session = StateSession.__new__(StateSession)
    session._profile = profile
    session._runtime = SimpleNamespace(reset_browser_context=lambda: None)
    session.git_commit = "deadbeef"
    session.bootstrap_ms = 0
    return session


def _store(profile: _FakeProfile, tmp_path: Path) -> StateStore:
    store = StateStore(_session(profile), cache_root=tmp_path)
    store._settle = lambda *_a, **_k: None  # type: ignore[method-assign]
    return store


def _patch_session_io(session: StateSession) -> None:
    session._settle = lambda *_a, **_k: None  # type: ignore[method-assign]
    session._dump_localstorage = lambda *_a, **_k: {}  # type: ignore[method-assign]
    session._safe_screenshot = lambda *_a, **_k: "shot.png"  # type: ignore[method-assign]


def test_web_goal_grades_the_oracle_assertions_on_the_snapshot_path(
    tmp_path: Path,
) -> None:
    """The fast path must evaluate the oracle, not report success on nothing."""

    profile = _FakeProfile(oracle_ok=True)
    store = _store(profile, tmp_path)
    _patch_session_io(store.session)
    spec = {"benchmark": "2048", "target": {"score": 42}, "assertions": [MODEL_ASSERTION]}
    store.save_snapshot(store.fingerprint(spec), spec, {"state": 1})

    outcome, timing = store.restore_after(
        spec,
        tmp_path / "spec.yaml",
        actions=[{"type": "move"}],
        assertions=[ORACLE_ASSERTION],
    )

    assert outcome.status == "success"
    # The snapshot identity is unchanged, so the fast path still applies.
    assert timing.cache_hit is True
    assert timing.fast_restore_used is True
    # The oracle's assertion was evaluated; the model's was discarded.
    assert profile.seen_assertions == [[ORACLE_ASSERTION]]
    assert outcome.phase_statuses["oracle_result"] == "success"


def test_web_goal_fails_when_the_oracle_assertions_fail(tmp_path: Path) -> None:
    profile = _FakeProfile(oracle_ok=False)
    store = _store(profile, tmp_path)
    _patch_session_io(store.session)
    spec = {"benchmark": "2048", "target": {"score": 42}, "assertions": []}
    store.save_snapshot(store.fingerprint(spec), spec, {"state": 1})

    outcome, timing = store.restore_after(
        spec,
        tmp_path / "spec.yaml",
        actions=[],
        assertions=[ORACLE_ASSERTION],
    )

    assert outcome.status == "failure"
    assert outcome.failure_phase == "oracle_result"
    # A failed goal is a verdict, not a snapshot miss: no pointless full replay.
    assert timing.fast_restore_used is True
    assert timing.fast_restore_fallback is False


def test_snapshot_path_reports_success_without_assertions_only_when_none_given(
    tmp_path: Path,
) -> None:
    """Callers that pass no assertions keep the old restore-only semantics."""

    profile = _FakeProfile(oracle_ok=False)
    store = _store(profile, tmp_path)
    _patch_session_io(store.session)
    spec = {"benchmark": "2048", "target": {"score": 42}, "assertions": []}
    store.save_snapshot(store.fingerprint(spec), spec, {"state": 1})

    outcome, _ = store.restore_after(spec, tmp_path / "spec.yaml", actions=[])

    assert outcome.status == "success"
    assert outcome.phase_statuses["oracle_result"] == "skipped"
    assert profile.seen_assertions == []


# --- Unity goal is read from the assertion checks, not the aggregate ------


def _unity_pair(after_checks: list[dict[str, object]], status: str = "success") -> dict:
    return {
        "status": status,
        "before": {"status": "success", "checks": []},
        "after": {"status": status, "checks": after_checks},
    }


def test_unity_goal_fails_when_an_oracle_assertion_check_fails() -> None:
    result: dict[str, object] = {}
    ok = state_loop._mark_unity_goal_semantic(
        result,
        _unity_pair(
            [
                {"name": "c1.target.runtime", "status": "success"},
                {"name": "assertion[pal3_money_gte]", "status": "failure"},
            ]
        ),
    )

    assert ok is False
    assert result["after_semantic_ok"] is False
    assert result["after_semantic_status"] == "assert_failed"


def test_unity_goal_passes_only_with_successful_assertion_checks() -> None:
    result: dict[str, object] = {}
    ok = state_loop._mark_unity_goal_semantic(
        result,
        _unity_pair(
            [
                {"name": "c1.target.runtime", "status": "success"},
                {"name": "assertion[pal3_money_gte]", "status": "success"},
            ]
        ),
    )

    assert ok is True
    assert result["after_semantic_status"] == "success"


def test_unity_goal_without_any_assertion_check_is_not_a_silent_pass() -> None:
    """An after pass carrying no oracle assertion must not count as a goal."""

    result: dict[str, object] = {}
    ok = state_loop._mark_unity_goal_semantic(
        result, _unity_pair([{"name": "c1.target.runtime", "status": "success"}])
    )

    assert ok is False
    assert result["after_semantic_status"] == state_loop.ORACLE_TARGET_UNSUPPORTED
