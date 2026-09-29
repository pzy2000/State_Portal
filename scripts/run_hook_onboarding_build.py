#!/usr/bin/env python3
"""Run the registered build check for one hook-onboarding workspace."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


UNITY_EXECUTABLES = {
    "jynew": Path("/Applications/Unity/Unity.app/Contents/MacOS/Unity"),
    "pal3": Path("/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity"),
}


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> None:
    print("$", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, timeout=timeout_s, env=env)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def python_sources(root: Path) -> list[str]:
    onboarding = root / "warptest_onboarding"
    if not onboarding.is_dir():
        raise SystemExit("warptest_onboarding directory is missing")
    files = sorted(str(path) for path in onboarding.rglob("*.py"))
    if not files:
        raise SystemExit("warptest_onboarding contains no Python driver")
    return files


def changed_files(repo: Path, suffix: str) -> list[Path]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    files: list[Path] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].split(" -> ")[-1]
        path = repo / rel
        if path.is_file() and path.suffix.lower() == suffix:
            files.append(path)
    return sorted(set(files))


def build(app: str, repo: Path, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="warptest-onboarding-pycache-") as pycache:
        compile_env = os.environ.copy()
        compile_env["PYTHONPYCACHEPREFIX"] = pycache
        run(
            [sys.executable, "-m", "py_compile", *python_sources(repo)],
            cwd=repo,
            timeout_s=120,
            env=compile_env,
        )

    if app == "2048":
        javascript = changed_files(repo, ".js")
        for path in javascript:
            run(["node", "--check", str(path)], cwd=repo, timeout_s=60)
        return

    if app in {"spacerpg", "datingsim"}:
        run(["npm", "run", "build"], cwd=repo, timeout_s=600)
        return

    if app == "openra":
        run(["dotnet", "build", "OpenRA.sln", "--no-restore", "-c", "Debug"], cwd=repo, timeout_s=1_200)
        return

    if app == "libreoffice":
        java_files = sorted((repo / "warptest_onboarding").rglob("*.java"))
        if java_files:
            java_jar = Path(
                "games/LibreOffice/instdir/LibreOfficeDev.app/Contents/Resources/java/libreoffice.jar"
            )
            java_jar = (Path(__file__).resolve().parents[1] / java_jar).resolve()
            classes = report_dir / "libreoffice-java-classes"
            classes.mkdir(parents=True, exist_ok=True)
            run(
                [
                    "javac",
                    "-encoding",
                    "UTF-8",
                    "-cp",
                    str(java_jar),
                    "-d",
                    str(classes),
                    *(str(path) for path in java_files),
                ],
                cwd=repo,
                timeout_s=180,
            )
        return

    if app in {"jupyterlab", "blender"}:
        return

    if app in UNITY_EXECUTABLES:
        executable = UNITY_EXECUTABLES[app]
        if not executable.is_file():
            raise SystemExit(f"Unity executable is unavailable: {executable}")
        project = repo / "jyx2" if app == "jynew" else repo
        log = report_dir / "unity-compile.log"
        run(
            [
                str(executable),
                "-batchmode",
                "-quit",
                "-projectPath",
                str(project),
                "-logFile",
                str(log),
            ],
            cwd=repo,
            timeout_s=1_800,
        )
        return

    if app == "luanti":
        lua = changed_files(repo, ".lua")
        luac = shutil.which("luac") or shutil.which("luac5.1")
        if lua and luac:
            for path in lua:
                run([luac, "-p", str(path)], cwd=repo, timeout_s=60)
        cpp = changed_files(repo, ".cpp") + changed_files(repo, ".cc")
        build_dir_raw = os.environ.get("WARPTEST_ONBOARDING_BUILD_DIR")
        if cpp and build_dir_raw:
            build_dir = Path(build_dir_raw).expanduser().resolve()
            run(["cmake", "--build", str(build_dir), "-j2"], cwd=repo, timeout_s=1_800)
        return

    raise SystemExit(f"unknown onboarding app: {app}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    build(args.app, args.repo.expanduser().resolve(), args.report_dir.expanduser().resolve())


if __name__ == "__main__":
    main()
