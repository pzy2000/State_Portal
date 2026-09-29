from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from warptest import cost, state_loop
from warptest.state_loop import StateLoopTask
from warptest.state_store import RestoreOutcome, StateStore
from warptest.task_schema import PublicIssue


def _write_informative_png(path: Path, offset: int = 0) -> None:
    image = Image.new("RGB", (192, 192))
    pixels = image.load()
    for y in range(192):
        for x in range(192):
            pixels[x, y] = (
                (x * 37 + y * 17 + offset) % 256,
                (x * 11 + y * 43 + offset) % 256,
                (x * y + x * 7 + y * 13 + offset) % 256,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _task(task_id: str) -> StateLoopTask:
    return StateLoopTask(
        issue=PublicIssue(task_id, "2048", "games/2048", "issue"),
        oracle_spec=f"specs/{task_id}.yaml",
        state_description="state",
    )


class OracleGoldenPrelookupTests(unittest.TestCase):
    def test_prelookup_requires_seed_commit_metadata_digest_and_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            _write_informative_png(before, 1)
            _write_informative_png(after, 2)
            screenshots = [str(before), str(before), str(after)]

            def restore(*_args: object, **_kwargs: object) -> RestoreOutcome:
                return RestoreOutcome("success", 1, screenshot=screenshots.pop(0))

            session = SimpleNamespace(
                git_commit="commit-a",
                benchmark="2048",
                report_dir=root / "report",
                restore=restore,
            )
            spec = {
                "benchmark": "2048",
                "experiment_seed": 7,
                "actions": [{"type": "key", "key": "ArrowLeft"}],
            }
            generated = state_loop.ensure_oracle_goldens(
                session,
                spec,
                root / "oracle.yaml",
                cache_root=root / "cache",
                cross_validate_captures=2,
            )
            self.assertTrue(generated.cacheable)
            self.assertEqual(
                set(generated.artifacts),
                {"before", "after", "metadata"},
            )
            self.assertTrue(
                all(
                    len(generated.artifacts[name]["sha256"]) == 64
                    for name in ("before", "after", "metadata")
                )
            )

            with patch("warptest.state_loop._repo_git_commit", return_value="commit-a"):
                hit = state_loop.lookup_cached_oracle_goldens(
                    root / "repo", spec, cache_root=root / "cache"
                )
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertTrue(hit.cache_hit)
            self.assertEqual(hit.consistency["source"], "verified_cache")
            self.assertEqual(hit.artifacts, generated.artifacts)

            def unexpected_restore(*_args: object, **_kwargs: object) -> RestoreOutcome:
                raise AssertionError("cache hit must not restore the hidden oracle")

            cached_session = SimpleNamespace(
                git_commit="commit-a",
                benchmark="2048",
                report_dir=root / "second-report",
                restore=unexpected_restore,
            )
            reused = state_loop.ensure_oracle_goldens(
                cached_session,
                spec,
                root / "oracle.yaml",
                cache_root=root / "cache",
            )
            self.assertTrue(reused.cache_hit)

            with patch("warptest.state_loop._repo_git_commit", return_value="commit-b"):
                self.assertIsNone(
                    state_loop.lookup_cached_oracle_goldens(
                        root / "repo", spec, cache_root=root / "cache"
                    )
                )

            other_seed = {**spec, "experiment_seed": 8}
            with patch("warptest.state_loop._repo_git_commit", return_value="commit-a"):
                self.assertIsNone(
                    state_loop.lookup_cached_oracle_goldens(
                        root / "repo", other_seed, cache_root=root / "cache"
                    )
                )

            # A still-informative replacement image must not pass the metadata digest.
            _write_informative_png(Path(generated.before), 99)
            with patch("warptest.state_loop._repo_git_commit", return_value="commit-a"):
                self.assertIsNone(
                    state_loop.lookup_cached_oracle_goldens(
                        root / "repo", spec, cache_root=root / "cache"
                    )
                )

    def test_prelookup_rejects_legacy_images_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {"benchmark": "2048", "experiment_seed": 3, "actions": []}
            fingerprint = state_loop._oracle_fingerprint("commit-a", spec)
            directory = root / "cache" / "state-loop-golden" / fingerprint
            _write_informative_png(directory / "golden_before.png", 1)
            _write_informative_png(directory / "golden_after.png", 2)
            with patch("warptest.state_loop._repo_git_commit", return_value="commit-a"):
                hit = state_loop.lookup_cached_oracle_goldens(
                    root / "repo", spec, cache_root=root / "cache"
                )
            self.assertIsNone(hit)


class RestoreDiagnosticRoutingTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        *,
        restore_diagnostics: bool,
        task_ids: list[str] | None = None,
    ) -> tuple[dict[str, object], list[tuple[str, bool]]]:
        tasks = [_task("task-a"), _task("task-b")]
        calls: list[tuple[str, bool]] = []

        def fake_run(
            _base: Path,
            task: StateLoopTask,
            _agent: object,
            **kwargs: object,
        ) -> dict[str, object]:
            calls.append((task.id, bool(kwargs["restore_diagnostics"])))
            return {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "status": "failure",
                "result": False,
                "iterations": [],
                "cost": cost.CostMeter().summary(),
            }

        with (
            patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
            patch("warptest.state_loop.build_synthesis_agent", return_value=object()),
            patch("warptest.state_loop._run_single_task", side_effect=fake_run),
            patch("warptest.state_loop.run_context.stamp_payload"),
        ):
            report = state_loop.run_state_loop_benchmark(
                root,
                tasks_path="tasks.yaml",
                agent_kind="deterministic",
                report_dir=root / ("all" if task_ids is None else "subset"),
                restore_diagnostics=restore_diagnostics,
                restore_diagnostic_task_ids=task_ids,
            )
        return report, calls

    def test_formal_caller_can_disable_all_restore_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, calls = self._run(Path(tmp), restore_diagnostics=False)
        self.assertEqual(calls, [("task-a", False), ("task-b", False)])
        self.assertEqual(
            report["restore_diagnostics"], {"enabled": False, "task_ids": []}
        )

    def test_explicit_diagnostic_sample_only_enables_selected_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, calls = self._run(
                Path(tmp),
                restore_diagnostics=True,
                task_ids=["task-b"],
            )
        self.assertEqual(calls, [("task-a", False), ("task-b", True)])
        self.assertEqual(
            report["restore_diagnostics"],
            {"enabled": True, "task_ids": ["task-b"]},
        )

    def test_task_selection_requires_explicit_diagnostic_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "requires restore_diagnostics=True"):
                self._run(
                    Path(tmp),
                    restore_diagnostics=False,
                    task_ids=["task-a"],
                )


class StateStoreDiagnosticTests(unittest.TestCase):
    def test_disabling_probe_keeps_operational_fast_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fast_calls: list[list[dict[str, object]] | None] = []

            class Session:
                git_commit = "commit-a"
                bootstrap_ms = 12

                def restore(self, *_args: object, **_kwargs: object) -> RestoreOutcome:
                    return RestoreOutcome(
                        "success",
                        20,
                        screenshot="full.png",
                        runtime_state={"save": "snapshot"},
                    )

                def supports_snapshot(self) -> bool:
                    return True

                def fast_restore(
                    self,
                    _spec: dict[str, object],
                    _spec_path: Path,
                    _snapshot: dict[str, object],
                    *,
                    actions: list[dict[str, object]] | None,
                    assertions: list[dict[str, object]] | None = None,
                    screenshot_name: str,
                ) -> RestoreOutcome:
                    fast_calls.append(actions)
                    return RestoreOutcome("success", 3, screenshot=screenshot_name)

            store = StateStore(Session(), cache_root=Path(tmp) / "cache")
            spec = {
                "benchmark": "2048",
                "target": {"score": 10},
                "checkpoint": {"strategy": "test"},
            }
            before, before_timing = store.restore_before(
                spec,
                Path(tmp) / "spec.yaml",
                probe_fast_vs_full=False,
            )
            self.assertEqual(before.screenshot, "full.png")
            self.assertFalse(before_timing.fast_restore_used)
            self.assertIsNone(before_timing.fast_restore_ms)
            self.assertEqual(fast_calls, [])

            after, after_timing = store.restore_after(
                spec,
                Path(tmp) / "spec.yaml",
                actions=[{"type": "key", "key": "ArrowLeft"}],
            )
            self.assertEqual(after.elapsed_ms, 3)
            self.assertTrue(after_timing.fast_restore_used)
            self.assertEqual(len(fast_calls), 1)


if __name__ == "__main__":
    unittest.main()
