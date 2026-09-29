from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import native_window


class CaptureFullScreenTests(unittest.TestCase):
    def test_success_invokes_screencapture_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "nested" / "shot.png"

            def fake_run(args, **_kwargs):
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(b"png")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(native_window.subprocess, "run", side_effect=fake_run) as run:
                result = native_window.capture_full_screen(target_path)

        self.assertEqual(result, target_path)
        args, _kwargs = run.call_args
        self.assertEqual(args[0][0], "screencapture")
        self.assertIn("-x", args[0])

    def test_nonzero_exit_raises_native_window_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "shot.png"
            with patch.object(
                native_window.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stderr="permission denied"),
            ):
                with self.assertRaisesRegex(native_window.NativeWindowError, "permission denied"):
                    native_window.capture_full_screen(target_path)

    def test_missing_output_file_raises_native_window_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "shot.png"
            with patch.object(
                native_window.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stderr=""),
            ):
                with self.assertRaises(native_window.NativeWindowError):
                    native_window.capture_full_screen(target_path)


class ScreenSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        native_window._screen_size_cache = None

    def tearDown(self) -> None:
        native_window._screen_size_cache = None

    def test_measures_size_from_a_real_capture(self) -> None:
        fake_image = MagicMock()
        fake_image.width = 2560
        fake_image.height = 1440
        fake_image.__enter__ = MagicMock(return_value=fake_image)
        fake_image.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(native_window, "capture_full_screen", return_value=Path("/tmp/probe.png")),
            patch.object(native_window.Image, "open", return_value=fake_image),
        ):
            self.assertEqual(native_window.screen_size(), (2560, 1440))

    def test_reuses_one_measurement(self) -> None:
        fake_image = MagicMock()
        fake_image.width = 3024
        fake_image.height = 1964
        fake_image.__enter__ = MagicMock(return_value=fake_image)
        fake_image.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(
                native_window,
                "capture_full_screen",
                return_value=Path("/tmp/probe.png"),
            ) as capture,
            patch.object(native_window.Image, "open", return_value=fake_image),
        ):
            self.assertEqual(native_window.screen_size(), (3024, 1964))
            self.assertEqual(native_window.screen_size(), (3024, 1964))

        capture.assert_called_once()

    def test_concurrent_callers_share_one_real_probe(self) -> None:
        capture_count = 0
        capture_count_lock = threading.Lock()
        release_capture = threading.Event()

        def fake_capture(_path: Path) -> Path:
            nonlocal capture_count
            with capture_count_lock:
                capture_count += 1
            release_capture.wait(timeout=2)
            return Path("/tmp/probe.png")

        fake_image = MagicMock()
        fake_image.width = 3024
        fake_image.height = 1964
        fake_image.__enter__ = MagicMock(return_value=fake_image)
        fake_image.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(
                native_window,
                "capture_full_screen",
                side_effect=fake_capture,
            ),
            patch.object(native_window.Image, "open", return_value=fake_image),
            ThreadPoolExecutor(max_workers=16) as pool,
        ):
            futures = [pool.submit(native_window.screen_size) for _ in range(16)]
            release_capture.set()
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(results, [(3024, 1964)] * 16)
        self.assertEqual(capture_count, 1)

    def test_failed_probe_is_not_cached(self) -> None:
        fake_image = MagicMock()
        fake_image.width = 3024
        fake_image.height = 1964
        fake_image.__enter__ = MagicMock(return_value=fake_image)
        fake_image.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(
                native_window,
                "capture_full_screen",
                side_effect=[
                    native_window.NativeWindowError("probe failed"),
                    Path("/tmp/probe.png"),
                ],
            ) as capture,
            patch.object(native_window.Image, "open", return_value=fake_image),
        ):
            with self.assertRaisesRegex(
                native_window.NativeWindowError, "probe failed"
            ):
                native_window.screen_size()
            self.assertEqual(native_window.screen_size(), (3024, 1964))

        self.assertEqual(capture.call_count, 2)


class FocusRuntimeTests(unittest.TestCase):
    def test_luanti_style_runtime_focuses_via_native_quartz_input(self) -> None:
        runtime = SimpleNamespace(native=MagicMock())
        native_window.focus_runtime(runtime)
        runtime.native.focus.assert_called_once_with()

    def test_browser_style_runtime_focuses_via_playwright_page(self) -> None:
        runtime = SimpleNamespace(page=MagicMock())
        native_window.focus_runtime(runtime)
        runtime.page.bring_to_front.assert_called_once_with()

    def test_unrecognized_runtime_raises_native_window_error(self) -> None:
        with self.assertRaises(native_window.NativeWindowError):
            native_window.focus_runtime(object())


if __name__ == "__main__":
    unittest.main()
