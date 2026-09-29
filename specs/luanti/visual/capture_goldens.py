"""Capture real Luanti visual-oracle golden images.

WHY THIS IS A SEPARATE SCRIPT (not part of `warptest`):
Luanti's client must create an OpenGL window to render, which requires an
*interactive* macOS desktop session (or Linux + xvfb + software GL). The WarpTest
agent/CI shell cannot create a GL context, so the committed goldens start as
deterministic placeholders. Run THIS script from a real desktop session (a normal
Terminal window while logged in, screen unlocked) to replace them with real frames.

It launches the signed `luanti.app` via LaunchServices (`open -W`), which is the
only reliable way to get a GL window on modern macOS. For each spec that declares a
`visual_oracle`, it synthesizes that spec's scene with the test-gated warptest
worldmod, lets the engine screenshot hook capture a frame, and copies it onto the
spec's golden image.

Usage (in a real desktop Terminal, screen unlocked):
    python3 specs/luanti/visual/capture_goldens.py
    python3 specs/luanti/visual/capture_goldens.py --only set-node player-hp
    LUANTI_APP=/path/to/luanti.app python3 specs/luanti/visual/capture_goldens.py
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from warptest.luanti_runtime import LUANTI_WARPTEST_PLAYER, _build_utility_spec, ensure_luanti_instrumentation  # noqa: E402
from warptest.runner import load_spec  # noqa: E402
from warptest.runtime import json_dumps  # noqa: E402

LUANTI_REPO = REPO / "games" / "luanti"
VISUAL_DIR = REPO / "specs" / "luanti" / "visual"
DEFAULT_APP = LUANTI_REPO / "build-app" / "out" / "luanti.app"
SCREENSHOT_FRAME = os.environ.get("WARPTEST_LUANTI_SCREENSHOT_FRAME", "240")
PER_SPEC_TIMEOUT = int(os.environ.get("WARPTEST_LUANTI_CAPTURE_TIMEOUT", "90"))


def find_app() -> Path:
    override = os.environ.get("LUANTI_APP")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_APP


def ensure_signed(app: Path) -> None:
    # Strip extended attributes (they break codesign) then ad-hoc sign with the
    # luanti debug entitlements so the bundle can open a window on macOS.
    subprocess.run(["xattr", "-cr", str(app)], check=False)
    entitlements = LUANTI_REPO / "misc" / "macos" / "entitlements" / "debug.entitlements"
    args = ["codesign", "--force", "--deep", "-s", "-"]
    if entitlements.exists():
        args += ["--entitlements", str(entitlements)]
    args.append(str(app))
    subprocess.run(args, check=False, capture_output=True, text=True)


def visual_specs(only: list[str] | None) -> list[Path]:
    specs = []
    for path in sorted(VISUAL_DIR.parent.glob("*.yaml")):
        spec = load_spec(path)
        if not isinstance(spec.get("visual_oracle"), dict):
            continue
        if only and path.stem not in only:
            continue
        specs.append(path)
    return specs


def capture_one(app: Path, spec_path: Path, work: Path) -> tuple[bool, str]:
    spec = load_spec(spec_path)
    stem = spec_path.stem
    world = work / f"{stem}-world"
    if world.exists():
        shutil.rmtree(world, ignore_errors=True)
    (world / "worldmods").mkdir(parents=True)
    shutil.copytree(LUANTI_REPO / "mods" / "warptest", world / "worldmods" / "warptest")
    (world / "world.mt").write_text(
        "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\n"
        "auth_backend = sqlite3\nmod_storage_backend = sqlite3\nworld_name = warptest\n"
        "creative_mode = false\nenable_damage = true\nload_mod_warptest = true\n",
        encoding="utf-8",
    )

    request_path = work / f"{stem}-request.json"
    report_path = work / f"{stem}-report.json"
    shot_path = work / f"{stem}-shot.png"
    log_path = work / f"{stem}-luanti.log"
    for stale in (report_path, shot_path):
        stale.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)

    conf = work / f"{stem}.conf"
    conf.write_text(
        f"name = {LUANTI_WARPTEST_PLAYER}\nmg_name = flat\nmgflat_ground_level = 0\n"
        "secure.enable_security = false\nsecure.trusted_mods = warptest\n"
        "enable_sound = false\nscreen_w = 1280\nscreen_h = 720\n"
        f"screenshot_path = {work}\n"
        f"warptest_request_path = {request_path}\n"
        f"warptest_report_path = {report_path}\n"
        f"warptest_screenshot_path = {shot_path}\n"
        f"warptest_screenshot_frame = {SCREENSHOT_FRAME}\n"
        "warptest_exit_after_screenshot = true\n",
        encoding="utf-8",
    )
    request = {
        "spec_path": str(spec_path),
        "screenshot_output_path": str(shot_path),
        "spec": _build_utility_spec(spec),
    }
    request_path.write_text(json_dumps(request), encoding="utf-8")

    cmd = [
        "open", "-n", "-W", str(app), "--args",
        "--go", "--gameid", "devtest", "--world", str(world),
        "--name", LUANTI_WARPTEST_PLAYER, "--config", str(conf),
        "--logfile", str(log_path),
    ]
    try:
        subprocess.run(cmd, check=False, timeout=PER_SPEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "timed out (is the screen unlocked and awake?)"

    # `open -W` can return before the app fully flushes the PNG; give it a moment.
    for _ in range(20):
        if shot_path.exists() and shot_path.stat().st_size > 2000:
            break
        time.sleep(0.5)
    if not (shot_path.exists() and shot_path.stat().st_size > 2000):
        return False, _capture_failure_detail(report_path, log_path)

    golden = VISUAL_DIR / f"{stem}.golden.png"
    shutil.copy2(shot_path, golden)
    return True, str(golden)


def _capture_failure_detail(report_path: Path, log_path: Path) -> str:
    if report_path.exists():
        try:
            text = report_path.read_text(encoding="utf-8", errors="ignore")
            if text.strip():
                return f"no screenshot produced; report: {text.strip()[:500]}"
        except OSError:
            pass
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        signals = [
            line for line in lines
            if "ERROR[" in line
            or "WARNING[" in line and "Irrlicht" in line
            or "[warptest]" in line
            or "Access denied" in line
        ]
        if signals:
            return "no screenshot produced; log: " + " | ".join(signals[-4:])
    return "no screenshot produced (GL context likely unavailable)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture real Luanti golden images (run in a desktop session).")
    parser.add_argument("--only", nargs="*", help="Only capture these spec stems (e.g. set-node player-hp).")
    args = parser.parse_args()

    if platform.system() != "Darwin":
        print("This helper targets macOS desktop sessions. On Linux, run `warptest bench` under xvfb instead.")
        return 2

    app = find_app()
    if not app.exists():
        print(f"luanti.app not found at {app}.\nBuild it first (see README 'Luanti'):")
        print("  cmake -S games/luanti -B games/luanti/build-app -DRUN_IN_PLACE=FALSE -DENABLE_GETTEXT=TRUE \\")
        print("    -DCMAKE_FIND_FRAMEWORK=LAST -DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG -DINSTALL_DEVTEST=TRUE \\")
        print("    -DCMAKE_INSTALL_PREFIX=games/luanti/build-app/out && \\")
        print("  cmake --build games/luanti/build-app -j && cmake --install games/luanti/build-app")
        print("  (or set LUANTI_APP=/path/to/luanti.app)")
        return 2

    ensure_luanti_instrumentation(LUANTI_REPO)
    ensure_signed(app)
    specs = visual_specs(args.only)
    print(f"Capturing {len(specs)} golden(s) using {app}")
    captured, failed = 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for spec_path in specs:
            ok, detail = capture_one(app, spec_path, work)
            status = "OK " if ok else "FAIL"
            print(f"  [{status}] {spec_path.stem:22s} {detail}")
            captured += int(ok)
            failed += int(not ok)
    print(f"== captured {captured}, failed {failed} ==")
    if failed:
        print("If everything failed: ensure you run this from a logged-in desktop session with the screen unlocked.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
