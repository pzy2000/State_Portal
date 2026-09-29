#!/usr/bin/env python3
"""Run the Luanti devtest control suite in one fresh, bounded session."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


PASS_MARKER = "All devtest unit tests passed."
FAIL_MARKER = "There were devtest unit test failures."
SUMMARY_MARKER = "Devtest Unit Test Results:"
APP_BINARY = Path("build-app/out/luanti.app/Contents/MacOS/luanti")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _summary(text: str, *, passed: bool) -> dict[str, object]:
    failed: int | None = None
    total: int | None = None
    marker = text.find(SUMMARY_MARKER)
    if marker >= 0:
        for line in text[marker:].splitlines():
            match = re.search(r"(\d+)\s*/\s*(\d+)\s+failed tests\.", line)
            if match:
                failed = int(match.group(1))
                total = int(match.group(2))
                break
    if failed is None or total is None or total <= 0 or failed > total:
        raise ValueError("Luanti devtest emitted no valid numeric test summary")
    if passed != (failed == 0):
        raise ValueError("Luanti devtest marker contradicts its numeric summary")
    return {
        "schema": "c1-lazarus-p2p-only-luanti-control-v1",
        "verdict": "PASSED" if passed else "FAILED",
        "num_failed_tests": failed,
        "num_total_tests": total,
        "suite": "games/devtest/mods/unittests",
        "source": "fresh_session_log",
    }


def run(source_root: Path, *, timeout_seconds: float, attempt_id: str) -> tuple[int, dict[str, object]]:
    source = source_root.expanduser().resolve(strict=True)
    binary = source / APP_BINARY
    if not binary.is_file():
        return 2, {"error": f"Luanti app binary is missing: {binary}"}
    sessions = source / ".p2p-only-control"
    sessions.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=sessions))
    world = session / "world"
    world.mkdir()
    (world / "world.mt").write_text("gameid = devtest\nbackend = dummy\n", encoding="utf-8")
    config = session / "minetest.conf"
    config.write_text(
        "\n".join(
            (
                "devtest_unittests_autostart = true",
                "enable_sound = false",
                "screen_w = 640",
                "screen_h = 400",
                "name = warptest",
                "",
            )
        ),
        encoding="utf-8",
    )
    log = session / "luanti.log"
    stdout_path = session / "stdout.log"
    stderr_path = session / "stderr.log"
    argv = (
        str(binary),
        "--go",
        "--gameid",
        "devtest",
        "--world",
        str(world),
        "--name",
        "warptest",
        "--config",
        str(config),
        "--logfile",
        str(log),
    )
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=source,
                env=dict(os.environ),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            while time.monotonic() - started < timeout_seconds:
                log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                if PASS_MARKER in log_text:
                    _terminate(process)
                    try:
                        payload = _summary(
                            stdout_path.read_text(encoding="utf-8", errors="replace"),
                            passed=True,
                        )
                    except ValueError as error:
                        return 2, {"error": str(error), "attempt_id": attempt_id}
                    payload.update({"attempt_id": attempt_id, "duration_ms": (time.monotonic() - started) * 1000.0})
                    return 0, payload
                if FAIL_MARKER in log_text:
                    _terminate(process)
                    try:
                        payload = _summary(
                            stdout_path.read_text(encoding="utf-8", errors="replace"),
                            passed=False,
                        )
                    except ValueError as error:
                        return 2, {"error": str(error), "attempt_id": attempt_id}
                    payload.update({"attempt_id": attempt_id, "duration_ms": (time.monotonic() - started) * 1000.0})
                    return 1, payload
                if process.poll() is not None:
                    return 2, {
                        "error": "Luanti exited before the devtest suite emitted a verdict",
                        "returncode": process.returncode,
                        "attempt_id": attempt_id,
                    }
                time.sleep(0.2)
            _terminate(process)
            return 124, {"error": "Luanti devtest control timed out", "attempt_id": attempt_id}
    finally:
        if process is not None:
            _terminate(process)
        shutil.rmtree(session, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--attempt-id", default="control")
    args = parser.parse_args()
    code, payload = run(
        args.source_root,
        timeout_seconds=args.timeout_seconds,
        attempt_id=str(args.attempt_id),
    )
    print(json.dumps(payload, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
