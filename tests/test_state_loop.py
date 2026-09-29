from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import yaml

from warptest.models import PhaseResult
from warptest.state_loop import (
    OracleGoldens,
    StateLoopTask,
    _oracle_visual_guidance,
    _registered_blender_goldens,
    _registered_libreoffice_goldens,
    _run_blender_task,
    _run_libreoffice_task,
    _run_iteration,
    _state_retry_metadata,
    _state_trial_incomplete,
    _state_trial_retryable_incomplete,
    _validate_blender_state_probe,
    _validate_libreoffice_state_probe,
    ensure_oracle_goldens,
)
from warptest.state_store import RestoreOutcome, StateSession
from warptest.task_schema import PublicIssue


class _FakeReport:
    def __init__(
        self,
        *,
        checkpoint: str = "success",
        validation: str = "success",
        smoke: str = "success",
        oracle: str = "success",
    ) -> None:
        self.checkpoint_restoration = PhaseResult(checkpoint, f"checkpoint {checkpoint}")
        self.validation = PhaseResult(validation, f"validation {validation}")
        self.smoke_execution = PhaseResult(smoke, f"smoke {smoke}")
        self.oracle_result = PhaseResult(oracle, f"oracle {oracle}")

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_restoration": {"status": self.checkpoint_restoration.status, "detail": self.checkpoint_restoration.detail},
            "validation": {"status": self.validation.status, "detail": self.validation.detail},
            "smoke_execution": {"status": self.smoke_execution.status, "detail": self.smoke_execution.detail},
            "oracle_result": {"status": self.oracle_result.status, "detail": self.oracle_result.detail},
        }


class _FakePage:
    def wait_for_timeout(self, ms: int) -> None:
        self.last_wait = ms

    def evaluate(self, script: str) -> dict[str, str]:
        return {}

    def close(self) -> None:
        self.closed = True


class _FakeRuntime:
    def __init__(self, screenshot_path: Path) -> None:
        self.screenshot_path = screenshot_path
        self.page = _FakePage()
        self.init_scripts: list[str] = ["oracle-leak"]
        self.console_errors = ["old console"]
        self.page_errors = ["old page"]
        self.network_api_calls = {"blocked": ["old"], "mocked": [], "attempted": ["old"]}
        self.reset_count = 0

    def reset_browser_context(self) -> None:
        self.reset_count += 1
        self.page = _FakePage()
        self.init_scripts = []
        self.console_errors = []
        self.page_errors = []
        self.network_api_calls = {"blocked": [], "mocked": [], "attempted": []}

    def install_storage_init(self, values: dict[str, str], clear_keys: list[str] | None = None) -> None:
        self.init_scripts.append(str(values))

    def screenshot(self, name: str) -> str:
        return str(self.screenshot_path)


class _FakeProfile:
    benchmark = "2048"

    def __init__(self, report: _FakeReport, *, validate: str = "success") -> None:
        self.report = report
        self.validate_status = validate
        self.validated_targets: list[dict[str, object]] = []

    def run(self, spec: dict[str, object], spec_path: Path, runtime: _FakeRuntime, mode: str = "jump_and_validate") -> _FakeReport:
        runtime.install_storage_init({"current": str(spec.get("name", ""))})
        runtime.network_api_calls["attempted"].append("current")
        return self.report

    def _validate(self, runtime: _FakeRuntime, target: dict[str, object]) -> PhaseResult:
        self.validated_targets.append(dict(target))
        return PhaseResult(self.validate_status, f"validate {self.validate_status}")

    def supports_snapshot(self) -> bool:
        return False

    def capture_runtime_state(self, runtime: _FakeRuntime) -> dict[str, object]:
        return {}


def _session(runtime: _FakeRuntime, report: _FakeReport) -> StateSession:
    session = StateSession.__new__(StateSession)
    session._runtime = runtime
    session._profile = _FakeProfile(report)
    session.git_commit = "deadbeef"
    return session


ORACLE_SPEC: dict[str, object] = {"target": {"board": [[2, 2, 0, 0]], "score": 0}}


def _iteration_session(*, validate: str = "success") -> SimpleNamespace:
    """A session whose oracle Start re-validation can be steered by tests."""

    return SimpleNamespace(
        git_commit="deadbeef",
        runtime=SimpleNamespace(),
        _profile=_FakeProfile(_FakeReport(), validate=validate),
    )


def _write_image(path: Path, *, offset: int = 0, blank: bool = False) -> None:
    image = Image.new("RGB", (160, 160))
    pixels = image.load()
    for x in range(160):
        for y in range(160):
            value = 255 if blank else ((x * 37) ^ (y * 91) ^ (offset * 17)) % 256
            pixels[x, y] = (value, value if blank else (value * 2) % 256, value if blank else (value * 3) % 256)
    image.save(path)


class StateLoopTests(unittest.TestCase):
    def test_minimal_agent_plans_only_after_start_gate_and_replaces_old_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            executed: list[dict[str, object]] = []
            spec = {
                "name": "fake",
                "benchmark": "2048",
                "target": {},
                "checkpoint": {},
                "actions": [{"type": "move", "direction": "left"}],
                "assertions": [],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> object:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            class Planner:
                def plan(self, *_args: object, **_kwargs: object) -> object:
                    events.append("plan")
                    return SimpleNamespace(
                        status="generated",
                        usable=True,
                        actions=({"type": "move", "direction": "right"},),
                        generation_ms=7,
                        to_report=lambda: {
                            "schema_version": 1,
                            "status": "generated",
                            "actions": [
                                {"type": "move", "direction": "right"}
                            ],
                            "cost": {},
                            "attributed_cost": {},
                        },
                    )

            class Store:
                def restore_before(self, *_args: object, **_kwargs: object):
                    return RestoreOutcome(
                        "success", 1, screenshot=str(root / "before.png")
                    ), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, _spec, _path, *, actions, **_kwargs):
                    events.append("execute")
                    executed.extend(actions)
                    return RestoreOutcome(
                        "success", 2, screenshot=str(root / "after.png")
                    ), SimpleNamespace(to_dict=lambda: {})

            def visual(**kwargs: object) -> dict[str, object]:
                if kwargs.get("evaluate_after") is False:
                    events.append("start_grade")
                    return {"before": {"label": "correct"}}
                return {
                    "before": {"label": "correct"},
                    "after": {"label": "correct"},
                }

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
                preconditions="two tiles are ready",
                reference_steps="move right once",
            )
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch("warptest.state_loop._informative_screenshot", return_value=True),
                patch("warptest.state_loop.run_visual_pair_audit", side_effect=visual),
            ):
                result = _run_iteration(
                    root,
                    task,
                    Agent(),
                    session=_iteration_session(),
                    store=Store(),
                    goldens=goldens,
                    description="move right once",
                    task_dir=root / "task",
                    iteration=1,
                    feedback=None,
                    oracle_spec=ORACLE_SPEC,
                    minimal_planner=Planner(),
                    transition_action_source="minimal_agent_v1",
                )
            self.assertEqual(events[:3], ["start_grade", "plan", "execute"])
            self.assertEqual(
                executed, [{"type": "move", "direction": "right"}]
            )
            self.assertEqual(result["smoke_action_generation_ms"], 7)
            self.assertEqual(result["original_generated_actions_ignored"], True)
            self.assertIs(result["planner_called"], True)
            self.assertIs(result["planner_called_after_start"], True)
            self.assertIs(result["action_execution_started"], True)

    def test_minimal_agent_is_not_called_when_start_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {
                "name": "fake",
                "benchmark": "2048",
                "target": {},
                "checkpoint": {},
                "actions": [{"type": "move", "direction": "left"}],
                "assertions": [],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> object:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            class Planner:
                def plan(self, *_args: object, **_kwargs: object) -> object:
                    raise AssertionError("planner must not run before Start passes")

            class Store:
                def restore_before(self, *_args: object, **_kwargs: object):
                    return RestoreOutcome(
                        "success", 1, screenshot=str(root / "before.png")
                    ), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, *_args: object, **_kwargs: object):
                    raise AssertionError("transition must not execute")

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
                preconditions="start",
                reference_steps="move",
            )
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch("warptest.state_loop._informative_screenshot", return_value=True),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={"before": {"label": "incorrect"}},
                ),
            ):
                result = _run_iteration(
                    root,
                    task,
                    Agent(),
                    session=_iteration_session(),
                    store=Store(),
                    goldens=goldens,
                    description="move",
                    task_dir=root / "task",
                    iteration=1,
                    feedback=None,
                    oracle_spec=ORACLE_SPEC,
                    minimal_planner=Planner(),
                    transition_action_source="minimal_agent_v1",
                )
            self.assertEqual(result["start_gate_status"], "visual_failed")
            self.assertEqual(
                result["smoke_action_verdict"], "not_evaluated_start_failed"
            )
            self.assertIs(result["planner_called"], False)
            self.assertIsNone(result["planner_called_after_start"])
            self.assertIs(result["action_execution_started"], False)

    def test_restore_resets_context_before_each_profile_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            _write_image(shot)
            runtime = _FakeRuntime(shot)
            session = _session(runtime, _FakeReport())

            first = session.restore({"name": "first"}, Path(tmp) / "spec.yaml", actions=[])
            self.assertEqual(first.status, "success")
            self.assertEqual(runtime.reset_count, 1)
            self.assertEqual(runtime.init_scripts, ["{'current': 'first'}"])
            self.assertEqual(runtime.network_api_calls["attempted"], ["current"])

            second = session.restore({"name": "second"}, Path(tmp) / "spec.yaml", actions=[])
            self.assertEqual(second.status, "success")
            self.assertEqual(runtime.reset_count, 2)
            self.assertEqual(runtime.init_scripts, ["{'current': 'second'}"])
            self.assertEqual(runtime.network_api_calls["attempted"], ["current"])

    def test_after_restore_fails_closed_on_smoke_or_oracle_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            _write_image(shot)

            smoke = _session(_FakeRuntime(shot), _FakeReport(smoke="failure"))
            smoke_outcome = smoke.restore({}, Path(tmp) / "spec.yaml", actions=[], require_after=True)
            self.assertEqual(smoke_outcome.status, "failure")
            self.assertEqual(smoke_outcome.failure_phase, "smoke_execution")

            oracle = _session(_FakeRuntime(shot), _FakeReport(oracle="failure"))
            oracle_outcome = oracle.restore({}, Path(tmp) / "spec.yaml", actions=[], require_after=True)
            self.assertEqual(oracle_outcome.status, "failure")
            self.assertEqual(oracle_outcome.failure_phase, "oracle_result")

    def test_before_restore_ignores_post_smoke_oracle_but_requires_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            _write_image(shot)

            before = _session(_FakeRuntime(shot), _FakeReport(oracle="failure"))
            before_outcome = before.restore({}, Path(tmp) / "spec.yaml", actions=[])
            self.assertEqual(before_outcome.status, "success")

            validation = _session(_FakeRuntime(shot), _FakeReport(validation="failure"))
            validation_outcome = validation.restore({}, Path(tmp) / "spec.yaml", actions=[])
            self.assertEqual(validation_outcome.status, "failure")
            self.assertEqual(validation_outcome.failure_phase, "validation")

    def test_oracle_goldens_reject_non_informative_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blank = root / "blank.png"
            _write_image(blank, blank=True)

            session = SimpleNamespace(
                git_commit="deadbeef",
                benchmark="2048",
                report_dir=root / "report",
                restore=lambda *args, **kwargs: RestoreOutcome("success", 1, screenshot=str(blank)),
            )
            with self.assertRaisesRegex(RuntimeError, "non-informative"):
                ensure_oracle_goldens(
                    session,
                    {"actions": []},
                    root / "oracle.yaml",
                    cache_root=root / "global",
                    cross_validate_captures=1,
                )

    def test_nondeterministic_before_goldens_are_report_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before1 = root / "before1.png"
            before2 = root / "before2.png"
            after = root / "after.png"
            _write_image(before1, offset=1)
            _write_image(before2, offset=2)
            _write_image(after, offset=3)
            screenshots = [str(before1), str(before2), str(after)]

            def restore(*args: object, **kwargs: object) -> RestoreOutcome:
                return RestoreOutcome("success", 1, screenshot=screenshots.pop(0))

            session = SimpleNamespace(
                git_commit="deadbeef",
                benchmark="2048",
                report_dir=root / "report",
                restore=restore,
            )
            goldens = ensure_oracle_goldens(
                session,
                {"actions": [{"type": "wait"}]},
                root / "oracle.yaml",
                cache_root=root / "global",
                cross_validate_captures=2,
            )

            self.assertFalse(goldens.cacheable)
            self.assertIn(str(root / "report"), goldens.before)
            self.assertFalse((root / "global" / "state-loop-golden").exists())

    def test_run_iteration_passes_feedback_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "task"
            feedback = {"before": {"label": "incorrect", "rationale": "wrong state"}}
            calls: list[dict[str, object] | None] = []

            class Agent:
                def synthesize(self, issue: PublicIssue, base: Path, artifact_dir: Path, *, feedback: dict[str, object] | None = None) -> object:
                    calls.append(feedback)
                    spec = {
                        "name": "fake",
                        "benchmark": "2048",
                        "target": {},
                        "checkpoint": {},
                        "actions": [],
                        "assertions": [],
                    }
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            class Store:
                def restore_before(self, spec: dict[str, object], spec_path: Path, *, screenshot_name: str, cold_restore_ms: int | None = None) -> tuple[RestoreOutcome, object]:
                    return RestoreOutcome("success", 1, screenshot=str(root / "before.png")), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, spec: dict[str, object], spec_path: Path, *, actions: list[dict[str, object]], assertions: list[dict[str, object]] | None = None, screenshot_name: str) -> tuple[RestoreOutcome, object]:
                    return (
                        RestoreOutcome("success", 1, screenshot=str(root / "after.png")),
                        SimpleNamespace(to_dict=lambda: {}),
                    )

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            session = _iteration_session()
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch(
                    "warptest.state_loop._informative_screenshot",
                    return_value=True,
                ),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={"result": True},
                ),
            ):
                result = _run_iteration(
                    root,
                    task,
                    Agent(),
                    session=session,
                    store=Store(),
                    goldens=goldens,
                    description="issue",
                    task_dir=config_dir,
                    iteration=2,
                    feedback=feedback,
                    oracle_spec=ORACLE_SPEC,
                )
            self.assertTrue(result["result"])
            self.assertEqual(calls, [feedback])

    def test_state_stage_gate_precedes_transition_and_goal_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            spec = {
                "name": "fake",
                "benchmark": "2048",
                "target": {},
                "checkpoint": {},
                "actions": [{"type": "move", "direction": "left"}],
                "assertions": [],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> object:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            class Store:
                def restore_before(self, *_args: object, **_kwargs: object) -> tuple[RestoreOutcome, object]:
                    return RestoreOutcome(
                        "success", 1, screenshot=str(root / "before.png")
                    ), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, *_args: object, **_kwargs: object) -> tuple[RestoreOutcome, object]:
                    events.append("transition")
                    return RestoreOutcome(
                        "success", 1, screenshot=str(root / "after.png")
                    ), SimpleNamespace(to_dict=lambda: {})

            def staged_visual(**kwargs: object) -> dict[str, object]:
                if kwargs.get("evaluate_after") is False:
                    events.append("start_grade")
                    return {
                        "result": False,
                        "before": {"label": "correct"},
                    }
                events.append("goal_grade")
                self.assertEqual(kwargs.get("prior_before"), {"label": "correct"})
                return {
                    "result": False,
                    "before": {"label": "correct"},
                    "after": {"label": "incorrect"},
                }

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch("warptest.state_loop._informative_screenshot", return_value=True),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    side_effect=staged_visual,
                ),
            ):
                result = _run_iteration(
                    root,
                    task,
                    Agent(),
                    session=_iteration_session(),
                    store=Store(),
                    goldens=goldens,
                    description="issue",
                    task_dir=root / "task",
                    iteration=1,
                    feedback=None,
                    oracle_spec=ORACLE_SPEC,
                )

            self.assertEqual(events, ["start_grade", "transition", "goal_grade"])
            self.assertIs(result["start_success"], True)
            self.assertIs(result["goal_success"], False)
            self.assertIs(result["e2e_success"], False)
            self.assertIs(result["result"], False)

    def test_failed_state_start_gate_prevents_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transition_called = False
            spec = {
                "name": "fake",
                "benchmark": "2048",
                "target": {},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> object:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            class Store:
                def restore_before(self, *_args: object, **_kwargs: object) -> tuple[RestoreOutcome, object]:
                    return RestoreOutcome(
                        "success", 1, screenshot=str(root / "before.png")
                    ), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, *_args: object, **_kwargs: object) -> tuple[RestoreOutcome, object]:
                    nonlocal transition_called
                    transition_called = True
                    raise AssertionError("transition must not run after failed Start")

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch("warptest.state_loop._informative_screenshot", return_value=True),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={
                        "result": False,
                        "before": {"label": "incorrect"},
                    },
                ),
            ):
                result = _run_iteration(
                    root,
                    task,
                    Agent(),
                    session=_iteration_session(),
                    store=Store(),
                    goldens=goldens,
                    description="issue",
                    task_dir=root / "task",
                    iteration=1,
                    feedback=None,
                    oracle_spec=ORACLE_SPEC,
                )

            self.assertFalse(transition_called)
            self.assertIs(result["start_success"], False)
            self.assertEqual(
                result["goal_evaluation_status"], "not_evaluated_start_failed"
            )
            self.assertIs(result["e2e_success"], False)

    def test_missing_state_screenshot_preserves_semantic_stage_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {
                "name": "fake",
                "benchmark": "2048",
                "target": {},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> object:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                    )

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )

            for missing_stage in ("before", "after"):
                with self.subTest(missing_stage=missing_stage):

                    class Store:
                        def restore_before(
                            self, *_args: object, **_kwargs: object
                        ) -> tuple[RestoreOutcome, object]:
                            screenshot = (
                                None
                                if missing_stage == "before"
                                else str(root / "before.png")
                            )
                            return RestoreOutcome(
                                "success", 1, screenshot=screenshot
                            ), SimpleNamespace(to_dict=lambda: {})

                        def restore_after(
                            self, *_args: object, **_kwargs: object
                        ) -> tuple[RestoreOutcome, object]:
                            return RestoreOutcome(
                                "success", 1, screenshot=None
                            ), SimpleNamespace(to_dict=lambda: {})

                    with (
                        patch(
                            "warptest.state_loop._informative_screenshot",
                            return_value=True,
                        ),
                        patch(
                            "warptest.state_loop.run_visual_pair_audit",
                            return_value={"result": True},
                        ),
                    ):
                        result = _run_iteration(
                            root,
                            task,
                            Agent(),
                            session=_iteration_session(),
                            store=Store(),
                            goldens=goldens,
                            description="issue",
                            task_dir=root / f"task-{missing_stage}",
                            iteration=1,
                            feedback=None,
                            oracle_spec=ORACLE_SPEC,
                        )

                    self.assertIs(result["before_semantic_ok"], True)
                    if missing_stage == "before":
                        self.assertEqual(result["before_visual_status"], "incomplete_capture")
                        self.assertEqual(
                            result["goal_evaluation_status"],
                            "not_evaluated_start_failed",
                        )
                        self.assertEqual(result["status"], "restore_failure")
                    else:
                        self.assertIs(result["start_success"], True)
                        self.assertIs(result["after_semantic_ok"], True)
                        self.assertEqual(result["after_visual_status"], "incomplete_capture")
                        self.assertEqual(result["goal_evaluation_status"], "incomplete")
                        self.assertEqual(result["status"], "smoke_failure")

    def test_visual_judge_transport_failure_is_retryable_after_grading_started(
        self,
    ) -> None:
        result = {
            "incomplete": True,
            "visual_grading_started": True,
            "pair_verdict": {
                "before": {
                    "label": "uncertain",
                    "judge_failure": True,
                    "judge_error": "APIConnectionError: Connection error.",
                },
                "after": {"label": "not_evaluated"},
            },
        }

        self.assertTrue(_state_trial_retryable_incomplete(result))
        result["retryable_incomplete"] = True
        category, reason = _state_retry_metadata(result)
        self.assertEqual(category, "infra")
        self.assertIn("visual judge", reason or "")
        self.assertIn("Connection error", reason or "")

    def test_malformed_visual_judge_verdict_remains_nonretryable(self) -> None:
        result = {
            "incomplete": True,
            "visual_grading_started": True,
            "pair_verdict": {
                "after": {
                    "label": "uncertain",
                    "judge_failure": True,
                    "judge_error": (
                        "Visual judge label=correct requires an empty "
                        "rubric_failures list."
                    ),
                }
            },
        }

        self.assertFalse(_state_trial_retryable_incomplete(result))

    def test_run_iteration_injects_model_spec_not_canonicalized_scaffold(self) -> None:
        """Regression: ``_run_iteration`` must inject ``checkpoint.model_generated_spec``
        (the method's own synthesis), not ``checkpoint.generated_spec`` (which may have
        been silently canonicalized to the hidden public scaffold), so the method arm
        actually measures its own checkpoint quality.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "task"
            model_spec = {"name": "model-own", "benchmark": "2048", "target": {}, "checkpoint": {}, "actions": [], "assertions": []}
            scaffold_spec = {"name": "scaffold", "benchmark": "2048", "target": {"board": "different"}, "checkpoint": {}, "actions": [], "assertions": []}

            class Agent:
                def synthesize(self, issue, base, artifact_dir, *, feedback=None):
                    return SimpleNamespace(
                        generated_spec=scaffold_spec,
                        model_generated_spec=model_spec,
                        used_public_scaffold=True,
                    )

            restore_before_specs: list[dict] = []

            class Store:
                def restore_before(self, spec, spec_path, *, screenshot_name, cold_restore_ms=None):
                    restore_before_specs.append(spec)
                    return RestoreOutcome("success", 1, screenshot=str(root / "before.png")), SimpleNamespace(to_dict=lambda: {})

                def restore_after(self, spec, spec_path, *, actions, assertions=None, screenshot_name):
                    return RestoreOutcome("success", 1, screenshot=str(root / "after.png")), SimpleNamespace(to_dict=lambda: {})

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            session = _iteration_session()
            goldens = OracleGoldens(before=str(root / "b.png"), after=str(root / "a.png"), fingerprint="fp", cache_hit=False, cacheable=True)
            with patch("warptest.state_loop.run_visual_pair_audit", return_value={"result": True}):
                result = _run_iteration(
                    root, task, Agent(), session=session, store=Store(), goldens=goldens,
                    description="issue", task_dir=config_dir, iteration=1, feedback=None,
                    oracle_spec=ORACLE_SPEC,
                )
            self.assertEqual(restore_before_specs, [model_spec])
            self.assertEqual(result["injected_spec_source"], "model_generated_spec")
            self.assertTrue(result["used_public_scaffold"])

    def test_run_iteration_with_no_usable_model_spec_is_synthesis_failure(self) -> None:
        """Regression: when the model produced no usable spec (e.g. an untemplated
        public task fell back to the scaffold-fallback checkpoint), the iteration must
        fail rather than silently injecting the hidden public scaffold.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "task"

            class Agent:
                def synthesize(self, issue, base, artifact_dir, *, feedback=None):
                    return SimpleNamespace(
                        generated_spec={},
                        model_generated_spec=None,
                        used_public_scaffold=False,
                    )

            class Store:
                def restore_before(self, *args, **kwargs):
                    raise AssertionError("must not attempt to restore when there is no usable model spec")

                def restore_after(self, *args, **kwargs):
                    raise AssertionError("must not reach restore_after")

            task = StateLoopTask(
                issue=PublicIssue("task", "2048", "games/2048", "issue"),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
            session = _iteration_session()
            goldens = OracleGoldens(before=str(root / "b.png"), after=str(root / "a.png"), fingerprint="fp", cache_hit=False, cacheable=True)
            result = _run_iteration(
                root, task, Agent(), session=session, store=Store(), goldens=goldens,
                description="issue", task_dir=config_dir, iteration=1, feedback=None,
                oracle_spec=ORACLE_SPEC,
            )
            self.assertEqual(result["status"], "synthesis_failure")
            self.assertFalse(result["result"])


class OracleVisualGuidanceTests(unittest.TestCase):
    def test_prefers_after_specific_guidance(self) -> None:
        spec = {
            "visual_oracle": {
                "description": "generic start state",
                "rubric": {"must": ["start"], "must_not": [], "ignore": []},
                "after_description": "war factory and heavy tank built",
                "after_rubric": {
                    "must": ["war factory visible", "heavy tank present"],
                    "must_not": ["only the initial MCV"],
                    "ignore": ["camera framing"],
                },
            }
        }
        description, rubric = _oracle_visual_guidance(spec)
        self.assertEqual(description, "war factory and heavy tank built")
        self.assertEqual(rubric["must"], ["war factory visible", "heavy tank present"])
        self.assertEqual(rubric["must_not"], ["only the initial MCV"])

    def test_falls_back_to_generic_guidance(self) -> None:
        spec = {
            "visual_oracle": {
                "description": "generic state",
                "rubric": {"must": ["something"], "must_not": [], "ignore": []},
            }
        }
        description, rubric = _oracle_visual_guidance(spec)
        self.assertEqual(description, "generic state")
        self.assertEqual(rubric["must"], ["something"])


class BlenderStateLoopTests(unittest.TestCase):
    def test_blender_transition_semantic_failure_is_valid_treatment_evidence(self) -> None:
        report = {
            "status": "failure",
            "source": "blender_live_bpy_probe_v1",
            "live": True,
            "passed": False,
            "checks": [
                {"name": "action[blender_add_modifier]@0", "ok": False},
                {"name": "transition.actions_applied", "ok": False},
            ],
            "errors": [],
            "state_action_checks": [
                {"name": "action[blender_add_modifier]@0", "ok": False},
                {"name": "transition.actions_applied", "ok": False},
            ],
            "state_transition_backend": "blender_live_bpy_state_transition_v1",
            "state_transition_stage": "actions",
        }

        self.assertFalse(
            _validate_blender_state_probe(
                report, label="goal", require_transition=True
            )
        )

    def test_blender_state_uses_live_transition_render_proofs_and_seed_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            golden_before = root / "golden-before.png"
            golden_after = root / "golden-after.png"
            for path, offset in [(before, 0), (after, 24), (golden_before, 0), (golden_after, 24)]:
                _write_informative_png(path, offset)

            task = StateLoopTask(
                issue=PublicIssue("blender-scene-cube-material", "Blender", "games/blender", "issue"),
                oracle_spec="specs/blender/scene-cube-material.yaml",
                state_description="issue",
            )
            generated_spec = {
                "benchmark": "Blender",
                "target": {
                    "kind": "scene",
                    "objects": [{"name": "WarpCube", "primitive": "cube"}],
                },
                "checkpoint": {},
                "actions": [
                    {
                        "type": "blender_set_object_location",
                        "object": "WarpCube",
                        "location": [1, 2, 3],
                    }
                ],
                "assertions": [
                    {
                        "type": "blender_object_location",
                        "object": "WarpCube",
                        "location": [1, 2, 3],
                    }
                ],
            }
            registered_assertions = [
                {
                    "type": "blender_mesh_geometry",
                    "object": "WarpCube",
                    "vertex_count": 56,
                }
            ]
            oracle_spec = {
                "target": {"kind": "scene"},
                "evaluation": {
                    "schema_version": 1,
                    "semantic": {"assertions": registered_assertions},
                },
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=generated_spec,
                        model_generated_spec=generated_spec,
                        used_public_scaffold=False,
                    )

            sessions: list[FakeBlenderSession] = []
            trusted_source_repo = root / "games" / "blender-source"
            trusted_source_repo.mkdir(parents=True)
            trusted_executable = root / "build" / "Blender"

            class FakeBlenderSession:
                benchmark = "Blender"
                semantic_source = "blender_live_bpy_probe_v1"
                seed_evidence_source = "blender_native_desktop_session_v1"

                def __init__(
                    self,
                    repo: Path,
                    report_dir: Path,
                    *,
                    executable: Path | None = None,
                ) -> None:
                    self.repo = repo
                    self.report_dir = report_dir
                    self.executable = executable
                    self.restore_calls: list[dict[str, object]] = []
                    self.transition_calls: list[dict[str, object]] = []
                    self.render_calls: list[dict[str, object]] = []
                    sessions.append(self)

                def __enter__(self) -> "FakeBlenderSession":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def probe_liveness(self) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "bridge.ready", "ok": True}],
                        "errors": [],
                    }

                def restore_target(
                    self, spec: dict[str, object]
                ) -> dict[str, object]:
                    self.restore_calls.append(spec)
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "checks": [
                            {"name": "restore.target_only", "ok": True}
                        ],
                    }

                def probe_start(
                    self, _spec: dict[str, object]
                ) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "target.object[WarpCube]", "ok": True}],
                        "errors": [],
                    }

                def execute_state_transition(
                    self, spec: dict[str, object]
                ) -> dict[str, object]:
                    self.transition_calls.append(spec)
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [
                            {"name": "assertion[location]", "ok": True}
                        ],
                        "errors": [],
                        "state_action_checks": [
                            {"name": "action[location]@0", "ok": True},
                            {"name": "transition.actions_applied", "ok": True},
                        ],
                        "state_transition_backend": (
                            "blender_live_bpy_state_transition_v1"
                        ),
                    }

                def probe_goal(self, _spec: dict[str, object]) -> dict[str, object]:
                    raise AssertionError(
                        "execute_state_transition already returns the final goal probe"
                    )

                def screenshot(self, path: Path) -> str:
                    return str(before if "before" in path.name else after)

                def render_proof(
                    self,
                    path: Path,
                    *,
                    canonical_spec: dict[str, object],
                ) -> str:
                    self.render_calls.append(canonical_spec)
                    return str(before if "before" in path.name else after)

                def seed_evidence(
                    self,
                    seed: int,
                    probe: dict[str, object],
                    task_id: str,
                    stage: str,
                ) -> dict[str, object]:
                    return {
                        "schema_version": 1,
                        "source": self.seed_evidence_source,
                        "observed": True,
                        "algorithm": "not_applicable_native_gui",
                        "seed": seed,
                        "benchmark": self.benchmark,
                        "process_id": 4343,
                        "process_alive_at_observation": True,
                        "task_id": task_id,
                        "semantic_stage": stage,
                        "semantic_source": self.semantic_source,
                        "semantic_observed": True,
                        "semantic_observation": probe,
                    }

            goldens = OracleGoldens(
                before=str(golden_before),
                after=str(golden_after),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
                quality={},
            )
            with (
                patch(
                    "warptest.state_loop._registered_blender_goldens",
                    return_value=goldens,
                ),
                patch(
                    "warptest.state_loop.BlenderC1DesktopSession",
                    FakeBlenderSession,
                ),
                patch(
                    "warptest.state_loop.find_blender_executable",
                    return_value=trusted_executable,
                ) as find_executable,
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={"result": True, "status": "success"},
                ),
            ):
                result = _run_blender_task(
                    root,
                    task,
                    Agent(),
                    root / "games" / "blender",
                    oracle_spec,
                    root / "oracle.yaml",
                    {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "state_description": task.state_description,
                        "oracle_spec": task.oracle_spec,
                        "seed": 11,
                        "result": False,
                        "iterations": [],
                    },
                    task_dir=root / "task",
                    headless=False,
                    source_repo=trusted_source_repo,
                )

        self.assertTrue(result["result"])
        self.assertEqual(result["screenshot_before"], str(before))
        self.assertEqual(result["screenshot_after"], str(after))
        self.assertEqual(result["iterations"][0]["screenshot_before"], str(before))
        self.assertEqual(result["iterations"][0]["screenshot_after"], str(after))
        self.assertEqual(result["iterations"][0]["pair_verdict"]["status"], "success")
        self.assertTrue(result["visual_grading_started"])
        self.assertEqual(result["injected_spec_source"], "model_generated_spec")
        self.assertEqual(len(sessions), 1)
        find_executable.assert_called_once_with(trusted_source_repo.resolve())
        self.assertEqual(sessions[0].executable, trusted_executable)
        self.assertEqual(
            result["trusted_source_repo"], str(trusted_source_repo.resolve())
        )
        self.assertEqual(result["runtime_executable"], str(trusted_executable))
        seeded_spec = {**generated_spec, "experiment_seed": 11}
        self.assertEqual(sessions[0].restore_calls, [seeded_spec])
        self.assertEqual(
            sessions[0].transition_calls,
            [{**seeded_spec, "assertions": registered_assertions}],
        )
        self.assertEqual(sessions[0].render_calls, [seeded_spec, seeded_spec])
        self.assertEqual(
            result["state_transition_backend"],
            "blender_live_bpy_state_transition_v1",
        )
        evidence = result["seed_evidence"]
        self.assertEqual(evidence["source"], "blender_native_desktop_session_v1")
        self.assertEqual(evidence["seed"], 11)
        self.assertEqual(evidence["initial"]["task_id"], task.id)
        self.assertEqual(evidence["initial"]["semantic_stage"], "restored")
        self.assertEqual(evidence["final"]["semantic_stage"], "goal")
        self.assertEqual(evidence["initial"]["process_id"], 4343)
        self.assertEqual(evidence["final"]["process_id"], 4343)
        self.assertEqual(
            evidence["initial"]["semantic_observation"],
            result["before_semantic_detail"],
        )
        self.assertEqual(
            evidence["final"]["semantic_observation"],
            result["after_semantic_detail"],
        )
        self.assertEqual(result["prng"], evidence["final"])

    def test_blender_state_rejects_public_scaffold_fallback_before_grading(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = StateLoopTask(
                issue=PublicIssue(
                    "blender-scene-cube-material",
                    "Blender",
                    "games/blender",
                    "issue",
                ),
                oracle_spec="specs/blender/scene-cube-material.yaml",
                state_description="issue",
            )
            scaffold = {
                "benchmark": "Blender",
                "target": {"kind": "scene", "objects": []},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def __init__(self, model_spec: object) -> None:
                    self.model_spec = model_spec

                def synthesize(
                    self, *_args: object, **_kwargs: object
                ) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=scaffold,
                        model_generated_spec=self.model_spec,
                        used_public_scaffold=True,
                    )

            incompatible = {**scaffold, "benchmark": "LibreOffice"}
            for model_spec in (None, {"target": {}}, incompatible):
                with self.subTest(model_spec=model_spec):
                    with (
                        patch(
                            "warptest.state_loop._registered_blender_goldens"
                        ) as goldens,
                        patch(
                            "warptest.state_loop.BlenderC1DesktopSession"
                        ) as runtime,
                        patch(
                            "warptest.state_loop.run_visual_pair_audit"
                        ) as visual,
                    ):
                        result = _run_blender_task(
                            root,
                            task,
                            Agent(model_spec),
                            root / "games" / "blender",
                            {"target": {"kind": "scene"}},
                            root / "oracle.yaml",
                            {
                                "task_id": task.id,
                                "benchmark": task.benchmark,
                                "seed": 2,
                                "result": False,
                                "iterations": [],
                                "visual_grading_started": False,
                            },
                            task_dir=root / "task",
                            headless=False,
                        )

                    self.assertEqual(result["status"], "failure")
                    self.assertFalse(result["result"])
                    self.assertTrue(result["used_public_scaffold"])
                    self.assertEqual(result["injected_spec_source"], "none")
                    self.assertNotIn("config_path", result)
                    self.assertNotIn("oracle_goldens", result)
                    self.assertIs(result["runtime_started"], False)
                    self.assertEqual(
                        result["seed_evidence"],
                        {
                            "schema_version": 1,
                            "source": "runtime_not_started",
                            "observed": False,
                            "algorithm": "not_applicable_native_gui",
                            "seed": 2,
                            "initial": None,
                            "final": None,
                        },
                    )
                    self.assertNotIn("prng", result)
                    goldens.assert_not_called()
                    runtime.assert_not_called()
                    visual.assert_not_called()

    def test_blender_semantic_restore_failure_is_treatment_and_preserves_entry_seed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = StateLoopTask(
                issue=PublicIssue(
                    "blender-scene-cube-material",
                    "Blender",
                    "games/blender",
                    "issue",
                ),
                oracle_spec="specs/blender/scene-cube-material.yaml",
                state_description="issue",
            )
            generated_spec = {
                "benchmark": "Blender",
                "target": {"kind": "scene", "objects": []},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def synthesize(
                    self, *_args: object, **_kwargs: object
                ) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=generated_spec,
                        model_generated_spec=generated_spec,
                        used_public_scaffold=False,
                    )

            class FailingBlenderSession:
                benchmark = "Blender"
                semantic_source = "blender_live_bpy_probe_v1"
                seed_evidence_source = "blender_native_desktop_session_v1"

                def __init__(self, _repo: Path, _report_dir: Path) -> None:
                    pass

                def __enter__(self) -> "FailingBlenderSession":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def probe_liveness(self) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "bridge.ready", "ok": True}],
                        "errors": [],
                    }

                def seed_evidence(
                    self,
                    seed: int,
                    probe: dict[str, object],
                    task_id: str,
                    stage: str,
                ) -> dict[str, object]:
                    return {
                        "schema_version": 1,
                        "source": self.seed_evidence_source,
                        "observed": True,
                        "algorithm": "not_applicable_native_gui",
                        "seed": seed,
                        "benchmark": self.benchmark,
                        "process_id": 5151,
                        "process_alive_at_observation": True,
                        "task_id": task_id,
                        "semantic_stage": stage,
                        "semantic_source": self.semantic_source,
                        "semantic_observed": True,
                        "semantic_observation": probe,
                    }

                def restore_target(
                    self, _spec: dict[str, object]
                ) -> dict[str, object]:
                    return {
                        "status": "failure",
                        "source": self.semantic_source,
                        "live": True,
                        "checks": [
                            {
                                "name": "restore.target_only",
                                "ok": False,
                                "detail": (
                                    "Blender artifact path must be relative: "
                                    "/tmp/model-output.blend"
                                ),
                            },
                        ],
                        "errors": [],
                    }

            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch(
                    "warptest.state_loop._registered_blender_goldens",
                    return_value=goldens,
                ),
                patch(
                    "warptest.state_loop.BlenderC1DesktopSession",
                    FailingBlenderSession,
                ),
            ):
                result = _run_blender_task(
                    root,
                    task,
                    Agent(),
                    root / "games" / "blender",
                    {"target": {"kind": "scene"}},
                    root / "oracle.yaml",
                    {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "seed": 13,
                        "result": False,
                        "iterations": [],
                    },
                    task_dir=root / "task",
                    headless=False,
                )

        self.assertEqual(result["status"], "failure")
        self.assertIs(result["runtime_started"], True)
        self.assertTrue(result["treatment_failure"])
        self.assertEqual(result["model_failure_stage"], "restore")
        self.assertEqual(result["start_construction_status"], "restore_failed")
        self.assertFalse(_state_trial_incomplete(result))
        evidence = result["seed_evidence"]
        self.assertTrue(evidence["observed"])
        self.assertEqual(evidence["source"], "blender_native_desktop_session_v1")
        self.assertEqual(evidence["seed"], 13)
        self.assertEqual(evidence["initial"]["task_id"], task.id)
        self.assertEqual(evidence["initial"]["semantic_stage"], "entry")
        self.assertEqual(evidence["initial"]["process_id"], 5151)
        self.assertEqual(
            evidence["initial"]["semantic_observation"]["checks"],
            [{"name": "bridge.ready", "ok": True}],
        )
        self.assertIsNone(evidence["final"])
        self.assertEqual(result["prng"], evidence["initial"])

    def test_blender_goldens_use_the_committed_registered_render_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "specs" / "blender"
            visual_dir = spec_dir / "visual"
            before = visual_dir / "registered.before.golden.png"
            after = visual_dir / "registered.after.golden.png"
            _write_informative_png(before, 9)
            _write_informative_png(after, 39)
            spec_path = spec_dir / "registered.yaml"
            spec = {
                "benchmark": "Blender",
                "target": {"kind": "scene", "objects": []},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
                "experiment_seed": 5,
                "visual_oracle": {
                    "golden_before": "visual/registered.before.golden.png",
                    "golden_after": "visual/registered.after.golden.png",
                },
            }
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("benchmark: Blender\n", encoding="utf-8")
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "warptest@example.invalid"],
                ["git", "config", "user.name", "WarpTest"],
                ["git", "add", "specs"],
                ["git", "commit", "-qm", "register Blender render pair"],
            ):
                subprocess.run(command, cwd=root, check=True)

            with patch.dict(
                "os.environ", {"WARPTEST_C1_CACHE_ROOT": str(root / "cache")}
            ):
                goldens = _registered_blender_goldens(
                    root,
                    spec,
                    spec_path,
                    root / "report",
                    cache_root=root / "cache",
                )
                self.assertEqual(
                    goldens.consistency["source"],
                    "committed_visual_oracle_pair_v1",
                )
                self.assertTrue(Path(goldens.before).is_file())
                self.assertTrue(Path(goldens.after).is_file())


class LibreOfficeStateLoopTests(unittest.TestCase):
    def test_libreoffice_mutation_semantic_failure_is_valid_treatment_evidence(
        self,
    ) -> None:
        report = {
            "status": "failure",
            "source": "libreoffice_live_uno_probe_v1",
            "live": True,
            "passed": False,
            "checks": [
                {"name": "action[libreoffice_set_cell]@0", "ok": False},
                {"name": "transition.actions_applied", "ok": False},
            ],
            "errors": [],
            "state_action_checks": [
                {"name": "action[libreoffice_set_cell]@0", "ok": False},
                {"name": "transition.actions_applied", "ok": False},
            ],
            "state_transition_backend": "uno_mutation_native_ui_save_reopen_v1",
            "state_transition_stage": "actions",
            "document_identity": "uno-document-start",
            "session_identity": "office-session-1",
            "document_url": "",
            "document_modified": True,
        }

        self.assertFalse(
            _validate_libreoffice_state_probe(
                report,
                label="goal",
                require_persistence=True,
                start_probe={
                    "document_identity": "uno-document-start",
                    "session_identity": "office-session-1",
                },
            )
        )

    def test_libreoffice_state_uses_live_desktop_transition_and_seed_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            golden_before = root / "golden-before.png"
            golden_after = root / "golden-after.png"
            saved = root / "persisted.ods"
            saved.write_bytes(b"persisted-libreoffice-document")
            for path, offset in [
                (before, 3),
                (after, 27),
                (golden_before, 3),
                (golden_after, 27),
            ]:
                _write_informative_png(path, offset)

            task = StateLoopTask(
                issue=PublicIssue(
                    "libreoffice-calc-budget-rollup",
                    "LibreOffice",
                    "games/LibreOffice",
                    "issue",
                ),
                oracle_spec="specs/libreoffice/calc-quarterly-budget-rollup.yaml",
                state_description="issue",
            )
            generated_spec = {
                "benchmark": "LibreOffice",
                "target": {"kind": "calc_spreadsheet", "sheets": {}},
                "checkpoint": {},
                "actions": [
                    {"type": "libreoffice_set_cell"},
                    {"type": "libreoffice_save"},
                    {"type": "libreoffice_reopen"},
                ],
                "assertions": [{"type": "no_libreoffice_errors"}],
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=generated_spec,
                        model_generated_spec=generated_spec,
                        used_public_scaffold=False,
                    )

            sessions: list[FakeLibreOfficeSession] = []

            class FakeLibreOfficeSession:
                benchmark = "LibreOffice"
                semantic_source = "libreoffice_live_uno_probe_v1"
                seed_evidence_source = "libreoffice_native_desktop_session_v1"

                def __init__(self, repo: Path, report_dir: Path) -> None:
                    self.repo = repo
                    self.report_dir = report_dir
                    self.restore_calls: list[dict[str, object]] = []
                    self.transition_calls: list[dict[str, object]] = []
                    self.visual_prepare_calls: list[
                        tuple[dict[str, object], str]
                    ] = []
                    sessions.append(self)

                def __enter__(self) -> "FakeLibreOfficeSession":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def probe_liveness(self) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "bridge.ready", "ok": True}],
                        "errors": [],
                    }

                def restore_target(
                    self, spec: dict[str, object]
                ) -> dict[str, object]:
                    self.restore_calls.append(spec)
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "checks": [
                            {"name": "restore.target_only", "ok": True}
                        ],
                    }

                def probe_start(
                    self, _spec: dict[str, object]
                ) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "target.sheet_count", "ok": True}],
                        "errors": [],
                        "document_identity": "uno-document-start",
                        "session_identity": "office-session-1",
                        "document_url": "",
                        "document_modified": True,
                    }

                def execute_state_transition(
                    self, spec: dict[str, object]
                ) -> dict[str, object]:
                    self.transition_calls.append(spec)
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [
                            {"name": "assert.summary", "ok": True},
                            {"name": "transition.session_same", "ok": True},
                            {"name": "transition.saved_url", "ok": True},
                            {"name": "transition.persisted_file", "ok": True},
                            {"name": "transition.not_modified", "ok": True},
                            {
                                "name": "transition.document_reopened",
                                "ok": True,
                            },
                        ],
                        "errors": [],
                        "state_action_checks": [
                            {"name": "transition.actions_applied", "ok": True}
                        ],
                        "state_transition_backend": (
                            "uno_mutation_native_ui_save_reopen_v1"
                        ),
                        "state_transition_stage": "goal",
                        "document_identity": "uno-document-reopened",
                        "session_identity": "office-session-1",
                        "document_url": saved.as_uri(),
                        "document_path": str(saved),
                        "document_modified": False,
                    }

                def probe_goal(self, _spec: dict[str, object]) -> dict[str, object]:
                    raise AssertionError(
                        "execute_state_transition already returns the final goal probe"
                    )

                def prepare_visual_capture(
                    self, spec: dict[str, object], label: str
                ) -> dict[str, object]:
                    self.visual_prepare_calls.append((spec, label))
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "passed": True,
                        "document_identity": (
                            "uno-document-start"
                            if label == "before"
                            else "uno-document-reopened"
                        ),
                    }

                def screenshot(self, path: Path) -> str:
                    return str(before if "before" in path.name else after)

                def seed_evidence(
                    self,
                    seed: int,
                    probe: dict[str, object],
                    task_id: str,
                    stage: str,
                ) -> dict[str, object]:
                    return {
                        "schema_version": 1,
                        "source": self.seed_evidence_source,
                        "observed": True,
                        "algorithm": "not_applicable_native_gui",
                        "seed": seed,
                        "benchmark": self.benchmark,
                        "process_id": 4242,
                        "process_alive_at_observation": True,
                        "task_id": task_id,
                        "semantic_stage": stage,
                        "semantic_source": self.semantic_source,
                        "semantic_observed": True,
                        "semantic_observation": probe,
                    }

            goldens = OracleGoldens(
                before=str(golden_before),
                after=str(golden_after),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
                quality={},
            )
            with (
                patch(
                    "warptest.state_loop._registered_libreoffice_goldens",
                    return_value=goldens,
                ),
                patch(
                    "warptest.state_loop.LibreOfficeC1DesktopSession",
                    FakeLibreOfficeSession,
                ),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={"result": True, "status": "success"},
                ),
            ):
                result = _run_libreoffice_task(
                    root,
                    task,
                    Agent(),
                    root / "games" / "LibreOffice",
                    {
                        "target": {"kind": "calc_spreadsheet"},
                        "assertions": [{"type": "libreoffice_cell_equals"}],
                    },
                    root / "oracle.yaml",
                    {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "state_description": task.state_description,
                        "oracle_spec": task.oracle_spec,
                        "seed": 7,
                        "result": False,
                        "iterations": [],
                    },
                    task_dir=root / "task",
                    headless=False,
                )

        self.assertTrue(result["result"])
        self.assertEqual(result["screenshot_before"], str(before))
        self.assertEqual(result["screenshot_after"], str(after))
        self.assertEqual(result["iterations"][0]["pair_verdict"]["status"], "success")
        self.assertTrue(result["visual_grading_started"])
        self.assertEqual(len(sessions), 1)
        seeded_generated_spec = {**generated_spec, "experiment_seed": 7}
        self.assertEqual(sessions[0].restore_calls, [seeded_generated_spec])
        self.assertEqual(
            sessions[0].visual_prepare_calls,
            [
                (
                    {
                        "target": {"kind": "calc_spreadsheet"},
                        "assertions": [{"type": "libreoffice_cell_equals"}],
                    },
                    "before",
                ),
                (
                    {
                        "target": {"kind": "calc_spreadsheet"},
                        "assertions": [{"type": "libreoffice_cell_equals"}],
                    },
                    "after",
                ),
            ],
        )
        # The transition executes the candidate's own actions but is graded by
        # the registered oracle's assertions: the candidate's own
        # ``no_libreoffice_errors`` must not survive into the grader spec.
        self.assertEqual(
            sessions[0].transition_calls,
            [
                {
                    **seeded_generated_spec,
                    "assertions": [{"type": "libreoffice_cell_equals"}],
                }
            ],
        )
        self.assertEqual(
            result["state_transition_backend"],
            "uno_mutation_native_ui_save_reopen_v1",
        )
        evidence = result["seed_evidence"]
        self.assertEqual(evidence["source"], "libreoffice_native_desktop_session_v1")
        self.assertEqual(evidence["seed"], 7)
        self.assertEqual(evidence["initial"]["task_id"], task.id)
        self.assertEqual(evidence["initial"]["semantic_stage"], "restored")
        self.assertEqual(evidence["final"]["semantic_stage"], "goal")
        self.assertEqual(evidence["initial"]["process_id"], 4242)
        self.assertEqual(evidence["final"]["process_id"], 4242)
        self.assertEqual(
            evidence["initial"]["semantic_observation"],
            result["before_semantic_detail"],
        )
        self.assertEqual(
            evidence["final"]["semantic_observation"],
            result["after_semantic_detail"],
        )
        self.assertEqual(result["prng"], evidence["final"])
        self.assertEqual(result["injected_spec_source"], "model_generated_spec")

    def test_libreoffice_state_rejects_public_scaffold_fallback_before_grading(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = StateLoopTask(
                issue=PublicIssue(
                    "libreoffice-calc-budget-rollup",
                    "LibreOffice",
                    "games/LibreOffice",
                    "issue",
                ),
                oracle_spec="specs/libreoffice/calc-quarterly-budget-rollup.yaml",
                state_description="issue",
            )
            scaffold = {
                "benchmark": "LibreOffice",
                "target": {"kind": "calc_spreadsheet", "sheets": {}},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def __init__(self, model_spec: object) -> None:
                    self.model_spec = model_spec

                def synthesize(
                    self, *_args: object, **_kwargs: object
                ) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=scaffold,
                        model_generated_spec=self.model_spec,
                        used_public_scaffold=True,
                    )

            # A well-formed spec whose target is simply the wrong one is no
            # longer pre-rejected: it is restored and then measured against the
            # oracle target at the Start gate, like the GUI arms. Only an
            # unusable spec still fails before grading.
            for model_spec in (None, {"target": {}}):
                with self.subTest(model_spec=model_spec):
                    with (
                        patch(
                            "warptest.state_loop._registered_libreoffice_goldens"
                        ) as goldens,
                        patch(
                            "warptest.state_loop.LibreOfficeC1DesktopSession"
                        ) as runtime,
                        patch(
                            "warptest.state_loop.run_visual_pair_audit"
                        ) as visual,
                    ):
                        result = _run_libreoffice_task(
                            root,
                            task,
                            Agent(model_spec),
                            root / "games" / "LibreOffice",
                            {"target": {"kind": "calc_spreadsheet"}},
                            root / "oracle.yaml",
                            {
                                "task_id": task.id,
                                "benchmark": task.benchmark,
                                "seed": 2,
                                "result": False,
                                "iterations": [],
                                "visual_grading_started": False,
                            },
                            task_dir=root / "task",
                            headless=False,
                        )

                    self.assertEqual(result["status"], "failure")
                    self.assertFalse(result["result"])
                    self.assertTrue(result["used_public_scaffold"])
                    self.assertEqual(result["injected_spec_source"], "none")
                    self.assertNotIn("config_path", result)
                    self.assertNotIn("oracle_goldens", result)
                    self.assertIs(result["runtime_started"], False)
                    self.assertEqual(
                        result["seed_evidence"],
                        {
                            "schema_version": 1,
                            "source": "runtime_not_started",
                            "observed": False,
                            "algorithm": "not_applicable_native_gui",
                            "seed": 2,
                            "initial": None,
                            "final": None,
                        },
                    )
                    self.assertNotIn("prng", result)
                    goldens.assert_not_called()
                    runtime.assert_not_called()
                    visual.assert_not_called()

    def test_libreoffice_semantic_restore_failure_is_treatment_and_preserves_entry_seed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = StateLoopTask(
                issue=PublicIssue(
                    "libreoffice-calc-budget-rollup",
                    "LibreOffice",
                    "games/LibreOffice",
                    "issue",
                ),
                oracle_spec="specs/libreoffice/calc-quarterly-budget-rollup.yaml",
                state_description="issue",
            )
            generated_spec = {
                "benchmark": "LibreOffice",
                "target": {"kind": "calc_spreadsheet", "sheets": {}},
                "checkpoint": {},
                "actions": [],
                "assertions": [],
            }

            class Agent:
                def synthesize(
                    self, *_args: object, **_kwargs: object
                ) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=generated_spec,
                        model_generated_spec=generated_spec,
                        used_public_scaffold=False,
                    )

            class FailingLibreOfficeSession:
                benchmark = "LibreOffice"
                semantic_source = "libreoffice_live_uno_probe_v1"
                seed_evidence_source = "libreoffice_native_desktop_session_v1"

                def __init__(self, _repo: Path, _report_dir: Path) -> None:
                    pass

                def __enter__(self) -> "FailingLibreOfficeSession":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def probe_liveness(self) -> dict[str, object]:
                    return {
                        "status": "success",
                        "source": self.semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": [{"name": "bridge.ready", "ok": True}],
                        "errors": [],
                    }

                def seed_evidence(
                    self,
                    seed: int,
                    probe: dict[str, object],
                    task_id: str,
                    stage: str,
                ) -> dict[str, object]:
                    return {
                        "schema_version": 1,
                        "source": self.seed_evidence_source,
                        "observed": True,
                        "algorithm": "not_applicable_native_gui",
                        "seed": seed,
                        "benchmark": self.benchmark,
                        "process_id": 5252,
                        "process_alive_at_observation": True,
                        "task_id": task_id,
                        "semantic_stage": stage,
                        "semantic_source": self.semantic_source,
                        "semantic_observed": True,
                        "semantic_observation": probe,
                    }

                def restore_target(
                    self, _spec: dict[str, object]
                ) -> dict[str, object]:
                    return {
                        "status": "failure",
                        "source": self.semantic_source,
                        "live": True,
                        "checks": [
                            {"name": "restore.target_only", "ok": True},
                            {"name": "target.cell[Summary.B5]", "ok": False},
                        ],
                        "errors": [],
                    }

            goldens = OracleGoldens(
                before=str(root / "golden-before.png"),
                after=str(root / "golden-after.png"),
                fingerprint="fp",
                cache_hit=False,
                cacheable=True,
            )
            with (
                patch(
                    "warptest.state_loop._registered_libreoffice_goldens",
                    return_value=goldens,
                ),
                patch(
                    "warptest.state_loop.LibreOfficeC1DesktopSession",
                    FailingLibreOfficeSession,
                ),
            ):
                result = _run_libreoffice_task(
                    root,
                    task,
                    Agent(),
                    root / "games" / "LibreOffice",
                    {"target": {"kind": "calc_spreadsheet"}},
                    root / "oracle.yaml",
                    {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "seed": 17,
                        "result": False,
                        "iterations": [],
                    },
                    task_dir=root / "task",
                    headless=False,
                )

        self.assertEqual(result["status"], "failure")
        self.assertIs(result["runtime_started"], True)
        self.assertTrue(result["treatment_failure"])
        self.assertEqual(result["model_failure_stage"], "restore")
        self.assertEqual(result["start_construction_status"], "restore_failed")
        self.assertFalse(_state_trial_incomplete(result))
        evidence = result["seed_evidence"]
        self.assertTrue(evidence["observed"])
        self.assertEqual(
            evidence["source"], "libreoffice_native_desktop_session_v1"
        )
        self.assertEqual(evidence["seed"], 17)
        self.assertEqual(evidence["initial"]["task_id"], task.id)
        self.assertEqual(evidence["initial"]["semantic_stage"], "entry")
        self.assertEqual(evidence["initial"]["process_id"], 5252)
        self.assertEqual(
            evidence["initial"]["semantic_observation"]["checks"],
            [{"name": "bridge.ready", "ok": True}],
        )
        self.assertIsNone(evidence["final"])
        self.assertEqual(result["prng"], evidence["initial"])

    def test_libreoffice_goldens_are_loaded_only_from_committed_registered_pair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "specs" / "libreoffice"
            visual_dir = spec_dir / "visual"
            before = visual_dir / "registered.before.golden.png"
            after = visual_dir / "registered.after.golden.png"
            _write_informative_png(before, 5)
            _write_informative_png(after, 31)
            spec_path = spec_dir / "registered.yaml"
            spec = {
                "benchmark": "LibreOffice",
                "target": {"kind": "calc_spreadsheet", "sheets": {}},
                "actions": [],
                "assertions": [],
                "experiment_seed": 3,
                "visual_oracle": {
                    "golden_before": "visual/registered.before.golden.png",
                    "golden_after": "visual/registered.after.golden.png",
                },
            }
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("benchmark: LibreOffice\n", encoding="utf-8")
            commands = [
                ["git", "init", "-q"],
                ["git", "config", "user.email", "warptest@example.invalid"],
                ["git", "config", "user.name", "WarpTest"],
                ["git", "add", "specs"],
                ["git", "commit", "-qm", "register LibreOffice visual pair"],
            ]
            for command in commands:
                subprocess.run(command, cwd=root, check=True)

            with patch.dict(
                "os.environ",
                {"WARPTEST_C1_CACHE_ROOT": str(root / "cache")},
            ):
                goldens = _registered_libreoffice_goldens(
                    root,
                    spec,
                    spec_path,
                    root / "report",
                    cache_root=root / "cache",
                )
                self.assertEqual(
                    goldens.consistency["source"],
                    "committed_visual_oracle_pair_v1",
                )
                self.assertTrue(Path(goldens.before).is_file())
                self.assertTrue(Path(goldens.after).is_file())
                metadata_path = Path(goldens.before).parent / "meta.json"
                self.assertTrue(metadata_path.is_file())

                legacy = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                legacy["consistency"]["source"] = "libreoffice_desktop_screenshot"
                metadata_path.write_text(
                    yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8"
                )
                repaired = _registered_libreoffice_goldens(
                    root,
                    spec,
                    spec_path,
                    root / "report",
                    cache_root=root / "cache",
                )
                self.assertFalse(repaired.cache_hit)
                repaired_metadata = yaml.safe_load(
                    metadata_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    repaired_metadata["consistency"]["source"],
                    "committed_visual_oracle_pair_v1",
                )
                verified = _registered_libreoffice_goldens(
                    root,
                    spec,
                    spec_path,
                    root / "report",
                    cache_root=root / "cache",
                )
                self.assertTrue(verified.cache_hit)
                self.assertEqual(verified.consistency["source"], "verified_cache")
                self.assertEqual(
                    verified.consistency["registered_source"],
                    "committed_visual_oracle_pair_v1",
                )

                _write_informative_png(before, 77)
                with self.assertRaisesRegex(
                    RuntimeError, "registered golden_before differs from HEAD"
                ):
                    _registered_libreoffice_goldens(
                        root,
                        spec,
                        spec_path,
                        root / "report",
                        cache_root=root / "cache",
                    )


def _write_informative_png(path: Path, offset: int) -> None:
    size = 256
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (
                (x * 37 + y * 17 + offset) % 256,
                (x * 11 + y * 43 + offset) % 256,
                (x * y + x * 7 + y * 13 + offset) % 256,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


if __name__ == "__main__":
    unittest.main()
