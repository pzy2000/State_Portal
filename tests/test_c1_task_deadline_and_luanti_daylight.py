from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from warptest import (
    c1_task_resources,
    cost,
    gui_agent,
    p5_scale,
    state_loop,
    synthesis,
)
from warptest.c1_task_deadline import (
    C1TaskTimeout,
    bounded_task_timeout_s,
    c1_task_deadline,
    current_task_deadline,
    raise_if_task_timed_out,
    validate_task_timeout_s,
)
from warptest.luanti_state import (
    LuantiWarmSession,
    luanti_daylight_mode_for_task,
)
from warptest.native_input import native_runtime_lock
from warptest.runtime_lock import (
    RuntimeLockWaitTimeout,
    runtime_lease,
)


def _task(**overrides: str) -> SimpleNamespace:
    values = {
        "state_description": "Place a water source in a stone basin.",
        "preconditions": "A rendered Luanti world is visible.",
        "reference_steps": "Place one water source.",
        "goal_description": "The water source is visible.",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registered_task(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "deadline-task",
        "benchmark": "luanti",
        "repo": ".",
        "issue": SimpleNamespace(repo="."),
        "oracle_spec": "specs/luanti/water-source.yaml",
        "horizon_class": "long",
        "tags": (),
        **vars(_task()),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_task_deadline_caps_blocking_requests_and_raises() -> None:
    with c1_task_deadline("slow-task", 0.03):
        assert 0 < bounded_task_timeout_s(900.0) <= 0.03
        time.sleep(0.04)
        with pytest.raises(C1TaskTimeout, match="slow-task.*0.03s"):
            raise_if_task_timed_out()


def test_synthesis_deadline_timeout_is_recorded_before_unwind() -> None:
    class TimeoutCompletions:
        def create(self, **_kwargs: object) -> object:
            time.sleep(0.02)
            raise TimeoutError("provider transport timed out")

    class TimeoutClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=TimeoutCompletions())

        def with_options(self, **_kwargs: object) -> "TimeoutClient":
            return self

    client = synthesis.OpenAICompatibleChatClient(
        api_key="test-key",
        client=TimeoutClient(),
    )
    with cost.scoped_meter() as meter, pytest.raises(
        synthesis.SynthesisError, match="token usage is unknown"
    ):
        with c1_task_deadline("paid-timeout", 0.01):
            client.converse(
                "gpt-5.6-terra",
                [{"role": "user", "content": "respond"}],
            )

    summary = meter.summary()
    assert summary["llm_calls"] == 1
    assert summary["usage_complete"] is False
    assert summary["unknown_total_token_calls"] == 1


def test_runtime_lease_wait_uses_its_independent_timeout(tmp_path: Path) -> None:
    acquired = threading.Event()
    release = threading.Event()

    def hold_lease() -> None:
        with runtime_lease(tmp_path):
            acquired.set()
            release.wait(2.0)

    holder = threading.Thread(target=hold_lease, daemon=True)
    holder.start()
    assert acquired.wait(1.0)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeLockWaitTimeout, match="service unavailable"):
            with runtime_lease(tmp_path, wait_timeout_s=0.03):
                pass
    finally:
        release.set()
        holder.join(timeout=1.0)
    assert time.monotonic() - started < 0.2


def test_runtime_lease_timeout_does_not_leak_the_thread_lock(
    tmp_path: Path,
) -> None:
    acquired = threading.Event()
    release = threading.Event()

    def hold_lease() -> None:
        with runtime_lease(tmp_path):
            acquired.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold_lease, daemon=True)
    holder.start()
    assert acquired.wait(1.0)
    with pytest.raises(RuntimeLockWaitTimeout):
        with runtime_lease(tmp_path, wait_timeout_s=0.02):
            pass
    release.set()
    holder.join(timeout=1.0)
    with runtime_lease(tmp_path, wait_timeout_s=0.1):
        pass


def test_concurrent_c_and_d_luanti_trials_get_full_post_lock_task_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second arm may queue, but its 1200-second analogue starts afterward."""

    task = _registered_task(id="luanti-concurrent-deadline")
    repo = tmp_path / "luanti"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []
    results: dict[str, dict[str, object]] = {}
    remaining_by_arm: dict[str, float] = {}

    def task_native_lock():
        return native_runtime_lock(tmp_path / "native.lock")

    def fake_luanti(*_args: object, **kwargs: object) -> dict[str, object]:
        arm = str(kwargs.get("start_mode"))
        deadline = current_task_deadline()
        assert deadline is not None
        remaining_by_arm[arm] = deadline.remaining_s()
        if arm == "from_start":
            first_entered.set()
            assert release_first.wait(1.0)
        else:
            second_entered.set()
        return {"status": "failure", "incomplete": False}

    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_args: repo)
    monkeypatch.setattr(
        c1_task_resources, "native_runtime_lock", task_native_lock
    )
    monkeypatch.setattr(gui_agent, "_run_luanti_agent_task", fake_luanti)

    def run_arm(name: str, start_mode: str) -> None:
        try:
            results[name] = gui_agent._run_agent_task(
                tmp_path,
                task,
                model="test-model",
                base_url="https://example.invalid/v1",
                api_key="unused",
                api_key_env="TEST_KEY",
                synth_model="test-model",
                max_explore_steps=1,
                temperature=1.0,
                out_dir=tmp_path / name,
                max_steps=1,
                headless=True,
                start_mode=start_mode,
                policy_kind="agent_s",
                seed=0,
                _task_timeout_s=0.25,
            )
        except BaseException as error:
            failures.append(error)

    c_thread = threading.Thread(
        target=run_arm, args=("C", "from_start"), daemon=True
    )
    d_thread = threading.Thread(
        target=run_arm, args=("D", "synthesized"), daemon=True
    )
    c_thread.start()
    assert first_entered.wait(1.0)
    d_thread.start()
    time.sleep(0.08)
    assert not second_entered.is_set()
    release_first.set()
    c_thread.join(timeout=1.0)
    d_thread.join(timeout=1.0)

    assert not failures
    assert not c_thread.is_alive()
    assert not d_thread.is_alive()
    assert second_entered.is_set()
    assert remaining_by_arm["from_start"] > 0.20
    assert remaining_by_arm["synthesized"] > 0.20
    assert int(results["D"]["runtime_lock_wait_ms"]) >= 60
    assert results["D"]["runtime_lock_wait_timed_out"] is False


def test_runtime_lock_wait_timeout_is_pre_policy_infrastructure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task = _registered_task(id="luanti-lock-infrastructure")
    repo = tmp_path / "luanti"
    acquired = threading.Event()
    release = threading.Event()
    body_called = threading.Event()

    def hold_repo() -> None:
        with runtime_lease(repo, wait_timeout_s=1.0):
            acquired.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold_repo, daemon=True)
    holder.start()
    assert acquired.wait(1.0)
    monkeypatch.setenv("WARPTEST_RUNTIME_LOCK_WAIT_TIMEOUT_S", "0.03")
    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_args: repo)

    def unexpected_body(*_args: object, **_kwargs: object) -> dict[str, object]:
        body_called.set()
        return {"status": "failure"}

    monkeypatch.setattr(
        gui_agent, "_run_luanti_agent_task", unexpected_body
    )
    try:
        result = gui_agent._run_agent_task(
            tmp_path,
            task,
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="unused",
            api_key_env="TEST_KEY",
            synth_model="test-model",
            max_explore_steps=1,
            temperature=1.0,
            out_dir=tmp_path / "out",
            max_steps=1,
            headless=True,
            policy_kind="agent_s",
            _task_timeout_s=0.25,
        )
    finally:
        release.set()
        holder.join(timeout=1.0)

    assert not body_called.is_set()
    assert result["incomplete"] is True
    assert result["infrastructure_error"] is True
    assert result["runtime_lock_wait_timed_out"] is True
    assert result["timed_out"] is False
    assert result["policy_phase_started"] is False
    assert result["runtime_lock_wait_ms"] >= 20
    assert gui_agent._gui_trial_retryable_incomplete(result) is True


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_task_deadline_rejects_non_positive_or_non_finite_values(value: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        validate_task_timeout_s(value)


def test_state_task_timeout_persists_incomplete_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = _registered_task()

    def overrun(*_args: object, **_kwargs: object) -> dict[str, object]:
        while True:
            raise_if_task_timed_out()
            time.sleep(0.002)

    monkeypatch.setattr(state_loop, "_run_single_task_body", overrun)
    result = state_loop._run_single_task.__wrapped__(
        tmp_path,
        task,
        object(),
        out_dir=tmp_path / "out",
        max_iters=1,
        headless=True,
        task_timeout_s=0.02,
    )

    assert result["status"] == "incomplete"
    assert result["timed_out"] is True
    assert result["task_timeout_s"] == pytest.approx(0.02)
    assert "wall-clock limit" in result["reason"]


def test_gui_task_timeout_persists_incomplete_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = _registered_task()

    class Policy:
        metadata: dict[str, object] = {}

        def close(self) -> None:
            return None

    def overrun(*_args: object, **_kwargs: object) -> dict[str, object]:
        while True:
            raise_if_task_timed_out()
            time.sleep(0.002)

    monkeypatch.setenv("MODELSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(gui_agent, "load_state_loop_tasks", lambda *_args, **_kwargs: [task])
    monkeypatch.setattr(gui_agent, "FixedVisualBridge", lambda **_kwargs: object())
    monkeypatch.setattr(gui_agent, "_build_policy_client", lambda **_kwargs: Policy())
    monkeypatch.setattr(gui_agent, "_run_luanti_agent_task", overrun)

    report = gui_agent.run_gui_agent_benchmark(
        tmp_path,
        tasks_path=tmp_path / "tasks.yaml",
        model="test-model",
        report_dir=tmp_path / "report",
        task_timeout_s=0.02,
    )
    result = report["results"][0]

    assert result["status"] == "incomplete"
    assert result["timed_out"] is True
    assert result["task_timeout_s"] == pytest.approx(0.02)
    assert report["task_timeout_s"] == pytest.approx(0.02)


def test_p5_dry_run_exposes_manifest_bound_task_timeout() -> None:
    result = p5_scale.plan(
        {
            "api": {"provider_timeout_s": 900.0},
            "c1_task_count": 5,
            "c1_task_timeout_s": 1200.0,
        },
        models=["model"],
        seeds=1,
        arms=["c1_agent_s"],
    )

    assert result["task_timeout_s"] == pytest.approx(1200.0)
    assert result["total_c1_trials"] == 5


def test_luanti_daylight_policy_keeps_time_sensitive_tasks_natural() -> None:
    assert luanti_daylight_mode_for_task(
        _task(
            reference_steps="Wait until night and verify the moon is visible.",
            goal_description="INDEPENDENT_SENTINEL_GOAL",
        ),
        {},
    ) == "natural"
    assert luanti_daylight_mode_for_task(
        _task(goal_description="Wait until night and verify the moon is visible."),
        {},
    ) == "fixed_noon"
    assert luanti_daylight_mode_for_task(
        _task(), {"actions": [{"type": "luanti_set_timeofday", "value": 0.8}]}
    ) == "natural"
    assert luanti_daylight_mode_for_task(
        _task(), {"c1_daylight_mode": "natural"}
    ) == "natural"


def test_luanti_daylight_policy_does_not_confuse_gametime_with_time_of_day() -> None:
    spec = {
        "assertions": [{"type": "luanti_gametime_gte", "value": 1}],
    }
    assert luanti_daylight_mode_for_task(_task(), spec) == "fixed_noon"


def test_every_registered_c1_luanti_task_is_currently_safe_to_lock_at_noon() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load(
        (root / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
    )
    luanti_tasks = [
        item for item in payload["tasks"] if str(item.get("benchmark", "")).lower() == "luanti"
    ]
    assert luanti_tasks
    for item in luanti_tasks:
        spec = yaml.safe_load((root / item["oracle_spec"]).read_text(encoding="utf-8"))
        assert luanti_daylight_mode_for_task(SimpleNamespace(**item), spec) == "fixed_noon"


def test_fixed_noon_writes_time_speed_and_task_local_worldmod(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    report = tmp_path / "report"
    report.mkdir()
    world = tmp_path / "world"
    world.mkdir()
    (world / "world.mt").write_text("gameid = devtest\n", encoding="utf-8")
    session = LuantiWarmSession(
        repo,
        report,
        cache_root=tmp_path / "cache",
        render=True,
        daylight_mode="fixed_noon",
    )
    session._adapter._prepare_world = lambda _stem: world  # type: ignore[method-assign]

    prepared = session._prepare_world("warm")
    conf = session._write_persistent_conf(prepared).read_text(encoding="utf-8")
    world_mt = (world / "world.mt").read_text(encoding="utf-8")
    daylight_lua = (
        world / "worldmods/warptest_c1_daylight/init.lua"
    ).read_text(encoding="utf-8")

    assert "time_speed = 0" in conf
    assert "load_mod_warptest_c1_daylight = true" in world_mt
    assert "core.set_timeofday(0.5)" in daylight_lua


def test_deterministic_render_disables_volatile_client_effects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    report = tmp_path / "report"
    report.mkdir()
    world = tmp_path / "world"
    world.mkdir()
    session = LuantiWarmSession(
        repo,
        report,
        cache_root=tmp_path / "cache",
        render=True,
        daylight_mode="fixed_noon",
        deterministic_render=True,
    )

    conf = session._write_persistent_conf(world).read_text(encoding="utf-8")

    for setting in (
        "enable_clouds = false",
        "enable_fog = false",
        "enable_particles = false",
        "enable_dynamic_shadows = false",
        "enable_auto_exposure = false",
        "show_hud = false",
        "show_chat = false",
        "view_bobbing_amount = 0",
        "fall_bobbing_amount = 0",
        "camera_smoothing = 0",
    ):
        assert setting in conf


def test_natural_daylight_does_not_install_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    report = tmp_path / "report"
    report.mkdir()
    world = tmp_path / "world"
    world.mkdir()
    (world / "world.mt").write_text("gameid = devtest\n", encoding="utf-8")
    session = LuantiWarmSession(
        repo,
        report,
        cache_root=tmp_path / "cache",
        render=True,
        daylight_mode="natural",
    )
    session._adapter._prepare_world = lambda _stem: world  # type: ignore[method-assign]

    prepared = session._prepare_world("warm")
    conf = session._write_persistent_conf(prepared).read_text(encoding="utf-8")

    assert "time_speed = 0" not in conf
    assert not (world / "worldmods/warptest_c1_daylight").exists()
