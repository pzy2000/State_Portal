from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
import yaml

from warptest.c1_web_runtime import (
    C1WebRestoreResult,
    C1WebSemanticGrade,
    C1WebTransitionResult,
)
from warptest.runner import load_spec
from warptest.state_loop import (
    OracleGoldens,
    StateLoopTask,
    _capture_c1_web_pair,
    _ensure_c1_web_goldens,
    _run_c1_web_task,
    _run_single_task_body,
    _set_state_method_timing,
)
from warptest.task_schema import PublicIssue


ROOT = Path(__file__).resolve().parents[1]


def _write_informative_png(path: Path, offset: int) -> None:
    size = 192
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


ORACLE_SPEC: dict[str, object] = {"kind": "oracle", "target": {"kind": "text_file"}}


class _FakeWebSession:
    def __init__(
        self,
        before: Path,
        after: Path,
        *,
        start_ok: bool = True,
        oracle_start_ok: bool = True,
        goal_ok: bool = True,
    ) -> None:
        self.before = before
        self.after = after
        self.start_ok = start_ok
        self.oracle_start_ok = oracle_start_ok
        self.goal_ok = goal_ok
        self.calls: list[str] = []

    def __enter__(self) -> "_FakeWebSession":
        self.calls.append("start")
        return self

    def __exit__(self, *_args: object) -> None:
        self.calls.append("close")

    def restore_target_only(
        self, _spec: dict[str, object], _spec_path: Path
    ) -> C1WebRestoreResult:
        self.calls.append("restore")
        return C1WebRestoreResult(
            benchmark="JupyterLab",
            phase="target_only",
            app_url="http://127.0.0.1/lab",
            detail="restored",
            artifacts={},
            observation={"live": True},
        )

    def semantic_start(self, spec: dict[str, object]) -> C1WebSemanticGrade:
        oracle = spec.get("kind") == "oracle"
        self.calls.append("semantic_start_oracle" if oracle else "semantic_start")
        ok = self.oracle_start_ok if oracle else self.start_ok
        return C1WebSemanticGrade(
            ok=ok,
            status="success" if ok else "validate_failed",
            source="jupyterlab_contents_kernel_live_v1",
            detail="oracle start" if oracle else "start",
            observation={"content": "before"},
            fingerprint="1" * 64,
        )

    def apply_registered_transition(
        self, _spec: dict[str, object]
    ) -> C1WebTransitionResult:
        self.calls.append("transition")
        return C1WebTransitionResult(
            benchmark="JupyterLab",
            status="success",
            detail="transition",
            artifacts={"surface": "contents_api"},
            observation={"content": "after"},
            action_count=1,
        )

    def semantic_goal(self, _spec: dict[str, object]) -> C1WebSemanticGrade:
        self.calls.append("semantic_goal")
        return C1WebSemanticGrade(
            ok=self.goal_ok,
            status="success" if self.goal_ok else "assert_failed",
            source="jupyterlab_contents_kernel_live_v1",
            detail="goal",
            observation={"content": "after"},
            fingerprint="2" * 64,
        )

    def screenshot(self, name: str) -> str:
        self.calls.append(f"screenshot:{name}")
        return str(self.before if name == "before.png" else self.after)


class C1WebPairTests(unittest.TestCase):
    def test_pair_proves_semantics_around_registered_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 1)
            _write_informative_png(after, 31)
            fake = _FakeWebSession(before, after)
            with patch(
                "warptest.state_loop.create_c1_web_session", return_value=fake
            ) as factory:
                pair = _capture_c1_web_pair(
                    root,
                    {"target": {"kind": "text_file"}},
                    root / "spec.yaml",
                    root / "report",
                    benchmark="JupyterLab",
                    headless=False,
                    oracle_spec=ORACLE_SPEC,
                )

        self.assertEqual(pair["status"], "success")
        self.assertEqual(
            fake.calls,
            [
                "start",
                "restore",
                "semantic_start",
                "semantic_start_oracle",
                "screenshot:before.png",
                "transition",
                "semantic_goal",
                "screenshot:after.png",
                "close",
            ],
        )
        self.assertTrue(pair["transition"]["actions_executed"])
        factory.assert_called_once()
        self.assertTrue(factory.call_args.kwargs["require_headed"])

    def test_pair_fails_closed_before_transition_when_start_semantics_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 2)
            _write_informative_png(after, 32)
            fake = _FakeWebSession(before, after, start_ok=False)
            with patch(
                "warptest.state_loop.create_c1_web_session", return_value=fake
            ):
                pair = _capture_c1_web_pair(
                    root,
                    {"target": {"kind": "text_file"}},
                    root / "spec.yaml",
                    root / "report",
                    benchmark="JupyterLab",
                    headless=True,
                    oracle_spec=ORACLE_SPEC,
                )

        self.assertEqual(pair["status"], "failure")
        self.assertEqual(pair["failure_phase"], "semantic_start")
        self.assertNotIn("transition", fake.calls)

    def test_pair_fails_closed_when_start_misses_the_oracle_target(self) -> None:
        """A start that restores cleanly but misses the oracle target is not a Start."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 3)
            _write_informative_png(after, 33)
            fake = _FakeWebSession(before, after, oracle_start_ok=False)
            with patch(
                "warptest.state_loop.create_c1_web_session", return_value=fake
            ):
                pair = _capture_c1_web_pair(
                    root,
                    {"target": {"kind": "text_file"}},
                    root / "spec.yaml",
                    root / "report",
                    benchmark="JupyterLab",
                    headless=True,
                    oracle_spec=ORACLE_SPEC,
                )

        self.assertEqual(pair["status"], "failure")
        self.assertEqual(pair["failure_phase"], "semantic_start")
        self.assertTrue(pair["semantic_start"]["ok"])
        self.assertFalse(pair["semantic_start_merged"]["ok"])
        self.assertEqual(
            pair["semantic_start_merged"]["status"], "validate_failed"
        )
        self.assertNotIn("transition", fake.calls)

    def test_pair_fails_closed_when_no_oracle_evidence_is_available(self) -> None:
        """An ungradable Start must be visible, never silently counted as passed."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 4)
            _write_informative_png(after, 34)
            fake = _FakeWebSession(before, after)
            with patch(
                "warptest.state_loop.create_c1_web_session", return_value=fake
            ):
                pair = _capture_c1_web_pair(
                    root,
                    {"target": {"kind": "text_file"}},
                    root / "spec.yaml",
                    root / "report",
                    benchmark="JupyterLab",
                    headless=True,
                )

        self.assertEqual(pair["failure_phase"], "semantic_start")
        self.assertEqual(
            pair["semantic_start_merged"]["status"], "oracle_target_unsupported"
        )
        self.assertNotIn("transition", fake.calls)

    def test_pair_fails_closed_before_transition_when_start_visual_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 2)
            _write_informative_png(after, 32)
            fake = _FakeWebSession(before, after)
            gate_calls: list[str] = []

            def start_gate(
                live_before: str,
                merged_start: dict[str, object],
                runtime: object = None,
            ) -> tuple[bool, dict[str, object]]:
                gate_calls.append(live_before)
                self.assertTrue(merged_start["ok"])
                self.assertEqual(merged_start["status"], "success")
                return False, {"before": {"label": "incorrect"}}

            with patch(
                "warptest.state_loop.create_c1_web_session", return_value=fake
            ):
                pair = _capture_c1_web_pair(
                    root,
                    {"target": {"kind": "text_file"}},
                    root / "spec.yaml",
                    root / "report",
                    benchmark="JupyterLab",
                    headless=True,
                    oracle_spec=ORACLE_SPEC,
                    start_gate=start_gate,
                )

        self.assertEqual(gate_calls, [str(before)])
        self.assertEqual(pair["failure_phase"], "start_visual")
        self.assertNotIn("transition", fake.calls)

    def test_only_semantically_verified_web_cache_is_reused(self) -> None:
        cached = OracleGoldens(
            before="before.png",
            after="after.png",
            fingerprint="fp",
            cache_hit=True,
            cacheable=True,
            consistency={"source": "verified_cache"},
            quality={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp)
            metadata = cache_root / "state-loop-golden" / "fp" / "meta.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(
                yaml.safe_dump(
                    {
                        "consistency": {
                            "source": "c1_web_live_semantic_pair_v1",
                            "semantic_start_ok": True,
                            "semantic_goal_ok": True,
                            "transition_actions_executed": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("warptest.state_loop._repo_git_commit", return_value="abc"),
                patch("warptest.state_loop._cached_oracle_goldens", return_value=cached),
                patch(
                    "warptest.state_loop._capture_c1_web_pair",
                    side_effect=AssertionError("verified cache should avoid a runtime"),
                ),
            ):
                observed = _ensure_c1_web_goldens(
                    Path("/repo"),
                    {"benchmark": "JupyterLab"},
                    Path("/spec.yaml"),
                    Path("/report"),
                    benchmark="JupyterLab",
                    headless=True,
                    cache_root=cache_root,
                )
        self.assertIs(observed, cached)


class C1WebStateTaskTests(unittest.TestCase):
    def test_state_task_emits_common_pair_timing_and_seed_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            golden_before = root / "golden-before.png"
            golden_after = root / "golden-after.png"
            for path, offset in (
                (before, 3),
                (after, 33),
                (golden_before, 3),
                (golden_after, 33),
            ):
                _write_informative_png(path, offset)
            spec = load_spec(ROOT / "specs/jupyterlab/text-file-edit.yaml")
            task = StateLoopTask(
                issue=PublicIssue(
                    "jupyterlab-text-file-edit",
                    "JupyterLab",
                    "games/jupyterlab",
                    "edit text",
                ),
                oracle_spec="specs/jupyterlab/text-file-edit.yaml",
                state_description="edit text",
                preconditions="initial text",
                goal_description="edited text",
            )
            goldens = OracleGoldens(
                before=str(golden_before),
                after=str(golden_after),
                fingerprint="golden-fp",
                cache_hit=False,
                cacheable=True,
                consistency={"source": "c1_web_live_semantic_pair_v1"},
                quality={},
            )
            pair = {
                "status": "success",
                "failure_phase": None,
                "start_ms": 125,
                "goal_ms": 240,
                "screenshot_before": str(before),
                "screenshot_after": str(after),
                "semantic_start": {
                    "ok": True,
                    "status": "success",
                    "source": "jupyterlab_contents_kernel_live_v1",
                    "detail": "start",
                    "observation": {"content": "WarpTest initial\n"},
                    "fingerprint": "1" * 64,
                },
                "transition": {
                    "actions_executed": True,
                    "action_count": 2,
                    "status": "success",
                },
                "semantic_goal": {
                    "ok": True,
                    "status": "success",
                    "source": "jupyterlab_contents_kernel_live_v1",
                    "detail": "goal",
                    "observation": {"content": "WarpTest edited\n"},
                    "fingerprint": "2" * 64,
                },
            }

            class Agent:
                def synthesize(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                    return SimpleNamespace(
                        generated_spec=spec,
                        model_generated_spec=spec,
                        used_public_scaffold=False,
                        payload={},
                        runtime_validation={},
                    )

            base_result = {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "state_description": task.state_description,
                "oracle_spec": task.oracle_spec,
                "seed": 0,
                "result": False,
                "iterations": [],
                "visual_grading_started": False,
            }

            def capture_with_start_gate(
                *_args: object, start_gate: object = None, **_kwargs: object
            ) -> dict[str, object]:
                self.assertTrue(callable(start_gate))
                start_ok, start_verdict = start_gate(
                    str(before), pair["semantic_start"], None
                )
                self.assertTrue(start_ok)
                return {**pair, "start_verdict": start_verdict}

            with (
                patch("warptest.state_loop._ensure_c1_web_goldens", return_value=goldens),
                patch(
                    "warptest.state_loop._capture_c1_web_pair",
                    side_effect=capture_with_start_gate,
                ),
                patch("warptest.state_loop._repo_git_commit", return_value="abc"),
                patch(
                    "warptest.state_loop.run_visual_pair_audit",
                    return_value={"result": True, "status": "success"},
                ),
            ):
                result = _run_c1_web_task(
                    root,
                    task,
                    Agent(),
                    root / "games/jupyterlab",
                    spec,
                    ROOT / "specs/jupyterlab/text-file-edit.yaml",
                    base_result,
                    task_dir=root / "task",
                    headless=True,
                )
            _set_state_method_timing(result)

        self.assertTrue(result["result"])
        self.assertTrue(result["visual_grading_started"])
        self.assertEqual(result["time_to_start_ms"], 125)
        self.assertEqual(result["time_start_to_goal_ms"], 240)
        self.assertEqual(result["screenshot_before"], str(before))
        self.assertEqual(result["screenshot_after"], str(after))
        self.assertTrue(result["seed_evidence"]["semantic_observed"])
        self.assertEqual(
            result["seed_evidence"]["initial_semantic_source"],
            "jupyterlab_contents_kernel_live_v1",
        )
        self.assertTrue(result["registered_transition"]["actions_executed"])

    def test_dispatch_precedes_generic_browser_state_session(self) -> None:
        task = StateLoopTask(
            issue=PublicIssue(
                "jupyterlab-text-file-edit",
                "JupyterLab",
                "games/jupyterlab",
                "edit text",
            ),
            oracle_spec="specs/jupyterlab/text-file-edit.yaml",
            state_description="edit text",
        )
        sentinel = {"task_id": task.id, "dispatch": "c1_web"}
        oracle = load_spec(ROOT / task.oracle_spec)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("warptest.state_loop.resolve_task_repo", return_value=root / "repo"),
                patch("warptest.state_loop.load_spec", return_value=oracle),
                patch(
                    "warptest.state_loop.audit_scripted_reference_replay",
                    return_value={"reference_reachable": False, "status": "unsupported"},
                ),
                patch("warptest.state_loop._run_c1_web_task", return_value=sentinel) as web,
                patch(
                    "warptest.state_loop.StateSession",
                    side_effect=AssertionError("generic StateSession must not start"),
                ),
            ):
                observed = _run_single_task_body(
                    root,
                    task,
                    Mock(),
                    out_dir=root / "out",
                    max_iters=1,
                    headless=True,
                )

        self.assertIs(observed, sentinel)
        web.assert_called_once()


if __name__ == "__main__":
    unittest.main()
