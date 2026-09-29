from __future__ import annotations
import os
import json
import multiprocessing
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import c1_task_resources
from warptest import gui_agent
from warptest import runner
from warptest import p5_scale
from warptest import state_loop
from warptest.runtime_lock import (
    RuntimeResourceBusy,
    runtime_lease,
    runtime_lock_path,
)


def _lease_process(
    repo: str,
    lock_dir: str,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    queue: multiprocessing.queues.Queue,
) -> None:
    with runtime_lease(repo, lock_dir=lock_dir):
        queue.put(time.monotonic())
        acquired.set()
        release.wait(timeout=5)


class RuntimeLeaseTests(unittest.TestCase):
    def test_nonblocking_busy_precedes_deadline_and_policy_reset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            task = SimpleNamespace(
                id="busy-a",
                benchmark="luanti",
                issue=object(),
            )
            policy = SimpleNamespace(reset=MagicMock())

            @contextmanager
            def busy_resources(*_args, **_kwargs):
                raise RuntimeResourceBusy("native-backend", "busy")
                yield  # pragma: no cover

            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=repo),
                patch.object(
                    gui_agent,
                    "c1_task_resource_locks",
                    side_effect=busy_resources,
                ),
                patch.object(gui_agent, "c1_task_deadline") as deadline,
            ):
                with self.assertRaises(RuntimeResourceBusy):
                    gui_agent._run_agent_task(
                        root,
                        task,
                        model="model",
                        base_url="https://example.invalid/v1",
                        api_key="key",
                        api_key_env="KEY",
                        synth_model="model",
                        max_explore_steps=1,
                        temperature=0.2,
                        out_dir=root / "out",
                        max_steps=1,
                        headless=True,
                        policy_kind="agent_s",
                        policy_client=policy,
                        _nonblocking_resources=True,
                    )

            deadline.assert_not_called()
            policy.reset.assert_not_called()

    def test_nonblocking_runtime_lease_reports_busy_without_waiting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            lock_dir = root / "locks"
            acquired = threading.Event()
            release = threading.Event()

            def owner() -> None:
                with runtime_lease(repo, lock_dir=lock_dir):
                    acquired.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=owner)
            thread.start()
            self.assertTrue(acquired.wait(timeout=1))
            started = time.monotonic()
            try:
                with self.assertRaises(RuntimeResourceBusy):
                    with runtime_lease(
                        repo,
                        lock_dir=lock_dir,
                        blocking=False,
                    ):
                        self.fail("busy lease must not be admitted")
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                release.set()
                thread.join(timeout=2)

    def test_p5_lock_directory_is_shared_across_report_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first" / "runtime-locks"
            second = root / "second" / "runtime-locks"
            with patch.dict(os.environ, {}, clear=True):
                with p5_scale._runtime_lock_dir_env(first):
                    first_dir = os.environ["WARPTEST_RUNTIME_LOCK_DIR"]
                    first_wait = os.environ[
                        "WARPTEST_RUNTIME_LOCK_WAIT_TIMEOUT_S"
                    ]
                with p5_scale._runtime_lock_dir_env(second):
                    second_dir = os.environ["WARPTEST_RUNTIME_LOCK_DIR"]
                    second_wait = os.environ[
                        "WARPTEST_RUNTIME_LOCK_WAIT_TIMEOUT_S"
                    ]

            expected = str(Path("/tmp/warptest-runtime-locks").resolve())
            self.assertEqual(first_dir, expected)
            self.assertEqual(second_dir, expected)
            self.assertEqual(first_wait, "10800")
            self.assertEqual(second_wait, "10800")
            self.assertNotEqual(first_dir, str(first.resolve()))
            self.assertNotEqual(second_dir, str(second.resolve()))
            self.assertEqual(
                json.loads((first.parent / "runtime-locks.json").read_text(encoding="utf-8"))["lock_dir"],
                expected,
            )
            lock_audit = json.loads(
                (first.parent / "runtime-locks.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(lock_audit["wait_timeout_s"], 10800.0)
            self.assertEqual(
                lock_audit["task_clock_policy"],
                "starts_after_resource_acquisition",
            )

    def test_same_repository_is_serialized_across_threads(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            lock_dir = root / "locks"
            active = 0
            peak = 0
            guard = threading.Lock()

            def worker() -> None:
                nonlocal active, peak
                with runtime_lease(repo, lock_dir=lock_dir):
                    with guard:
                        active += 1
                        peak = max(peak, active)
                    time.sleep(0.03)
                    with guard:
                        active -= 1

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(peak, 1)
            self.assertTrue(runtime_lock_path(repo, lock_dir=lock_dir).exists())

    def test_same_thread_can_reenter_the_same_repository_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            lock_dir = root / "locks"
            with runtime_lease(repo, lock_dir=lock_dir) as outer:
                with runtime_lease(repo, lock_dir=lock_dir) as inner:
                    self.assertEqual(inner, outer)

    def test_same_repository_is_serialized_across_processes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = multiprocessing.get_context("spawn")
            acquired_one = context.Event()
            acquired_two = context.Event()
            release = context.Event()
            queue = context.Queue()
            first = context.Process(
                target=_lease_process,
                args=(str(root / "game"), str(root / "locks"), acquired_one, release, queue),
            )
            second = context.Process(
                target=_lease_process,
                args=(str(root / "game"), str(root / "locks"), acquired_two, release, queue),
            )
            first.start()
            self.assertTrue(acquired_one.wait(timeout=3))
            second.start()
            self.assertFalse(acquired_two.wait(timeout=0.15))
            release.set()
            self.assertTrue(acquired_two.wait(timeout=3))
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len([queue.get(timeout=1), queue.get(timeout=1)]), 2)

    def test_distinct_repositories_do_not_share_a_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            barrier = threading.Barrier(2)
            passed: list[str] = []

            def worker(name: str) -> None:
                with runtime_lease(root / name, lock_dir=root / "locks"):
                    barrier.wait(timeout=1)
                    passed.append(name)

            threads = [threading.Thread(target=worker, args=(name,)) for name in ("game-a", "game-b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(set(passed), {"game-a", "game-b"})

    def test_run_spec_leases_the_resolved_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_file = root / "spec.yaml"
            spec_file.write_text("name: x\n", encoding="utf-8")
            repo = root / "repo"
            sentinel = object()
            with (
                patch("warptest.runner.load_spec", return_value={}),
                patch("warptest.runner.resolve_repo", return_value=repo),
                patch("warptest.runner._run_spec_unlocked", return_value=sentinel) as unlocked,
                patch("warptest.runner.runtime_lease") as lease,
            ):
                lease.return_value.__enter__.return_value = root / "locks" / "repo.lock"
                self.assertIs(runner.run_spec(spec_file), sentinel)
            lease.assert_called_once_with(repo)
            unlocked.assert_called_once()

    def test_c1_gui_dispatch_leases_the_resolved_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            task = SimpleNamespace(benchmark="luanti", issue=object())
            lease = MagicMock()
            lease.return_value.__enter__.return_value = root / "locks" / "game.lock"
            expected = {"status": "sentinel"}
            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=repo),
                patch.object(c1_task_resources, "runtime_lease", lease),
                patch.object(
                    c1_task_resources,
                    "native_runtime_lock",
                    return_value=nullcontext(),
                ),
                patch.object(
                    gui_agent,
                    "_run_luanti_agent_task",
                    return_value=expected,
                ),
            ):
                result = gui_agent._run_agent_task(
                    root,
                    task,
                    model="model",
                    base_url="https://example.invalid/v1",
                    api_key="key",
                    api_key_env="KEY",
                    synth_model="model",
                    max_explore_steps=1,
                    temperature=0.2,
                    out_dir=root / "out",
                    max_steps=1,
                    headless=True,
                )

            self.assertIs(result, expected)
            lease.assert_called_once_with(repo)

    def test_c1_state_task_leases_the_resolved_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            task = SimpleNamespace(id="runtime-lease-test", issue=object())
            lease = MagicMock()
            lease.return_value.__enter__.return_value = root / "locks" / "game.lock"
            with (
                patch.object(state_loop, "resolve_task_repo", return_value=repo),
                patch.object(c1_task_resources, "runtime_lease", lease),
                patch.object(
                    state_loop,
                    "_run_single_task_body",
                    return_value={"status": "failure"},
                ),
            ):
                result = state_loop._run_single_task(
                    root,
                    task,
                    object(),
                    out_dir=root / "out",
                    max_iters=1,
                    headless=True,
                )

            self.assertEqual(result["status"], "failure")
            lease.assert_called_once_with(repo)

    def test_c1_gui_and_runner_share_one_repository_critical_section(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            spec_file = root / "spec.yaml"
            spec_file.write_text("name: x\n", encoding="utf-8")
            task = SimpleNamespace(benchmark="luanti", issue=object())
            gui_entered = threading.Event()
            release_gui = threading.Event()
            runner_entered = threading.Event()
            failures: list[BaseException] = []

            def fake_gui(*_args, **_kwargs):
                gui_entered.set()
                release_gui.wait(timeout=3)
                return {"status": "done"}

            def run_gui() -> None:
                try:
                    with (
                        patch.object(
                            gui_agent, "resolve_task_repo", return_value=repo
                        ),
                        patch.object(
                            gui_agent,
                            "_run_luanti_agent_task",
                            side_effect=fake_gui,
                        ),
                    ):
                        gui_agent._run_agent_task(
                            root,
                            task,
                            model="model",
                            base_url="https://example.invalid/v1",
                            api_key="key",
                            api_key_env="KEY",
                            synth_model="model",
                            max_explore_steps=1,
                            temperature=0.2,
                            out_dir=root / "out",
                            max_steps=1,
                            headless=True,
                        )
                except BaseException as error:  # surfaced in the parent assertion
                    failures.append(error)

            def run_runner() -> None:
                try:
                    with (
                        patch.object(runner, "load_spec", return_value={}),
                        patch.object(runner, "resolve_repo", return_value=repo),
                        patch.object(
                            runner,
                            "_run_spec_unlocked",
                            side_effect=lambda *_args, **_kwargs: runner_entered.set(),
                        ),
                    ):
                        runner.run_spec(spec_file)
                except BaseException as error:  # surfaced in the parent assertion
                    failures.append(error)

            with patch.dict(
                os.environ,
                {"WARPTEST_RUNTIME_LOCK_DIR": str(root / "locks")},
                clear=False,
            ):
                gui_thread = threading.Thread(target=run_gui)
                runner_thread = threading.Thread(target=run_runner)
                gui_thread.start()
                self.assertTrue(gui_entered.wait(timeout=2))
                runner_thread.start()
                self.assertFalse(runner_entered.wait(timeout=0.15))
                release_gui.set()
                self.assertTrue(runner_entered.wait(timeout=2))
                gui_thread.join(timeout=2)
                runner_thread.join(timeout=2)

            self.assertFalse(failures)
            self.assertFalse(gui_thread.is_alive())
            self.assertFalse(runner_thread.is_alive())
