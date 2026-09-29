from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .runtime import json_dumps


_SAFE_OPENRA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Regular-world render frames to settle after the gameplay probe completes before the
# post-action screenshot is captured (lets building/unit spawn animations finish).
_GAMEPLAY_SETTLE_FRAMES = 16


class _NoOpPage:
    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class OpenRARuntimeAdapter:
    """Run OpenRA checkpoint specs through the test-gated utility command."""

    def __init__(self, repo: Path, out_dir: Path) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.out_dir = Path(out_dir).expanduser().resolve()
        self.launch_command = (
            "./utility.sh {mod} --warptest-checkpoint {request} {report}; "
            "./launch-game.sh Game.Mod={mod} Launch.Map={map}"
        )
        self.report_dir = self.out_dir
        self.page = _NoOpPage()
        self.app_url = None
        self.logs: list[str] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {"attempted": []}
        self._last_screenshot_path: Path | None = None
        self._last_target: dict[str, str] = {}
        self._last_timeout_seconds = 120.0
        self._gameplay_screenshot_path: Path | None = None

    def __enter__(self) -> "OpenRARuntimeAdapter":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def run_checkpoint(self, spec: dict[str, Any], spec_path: Path) -> dict[str, Any]:
        self._ensure_utility_built()
        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        mod = _safe_openra_id(str(target.get("mod") or spec.get("setup", {}).get("mod") or ""), "target.mod")
        map_id = _safe_openra_id(str(target.get("map") or ""), "target.map") if target.get("map") else None
        if not mod:
            raise RuntimeError("OpenRA spec target.mod is required")
        self._last_target = {key: value for key, value in {"mod": mod, "map": map_id or ""}.items() if value}
        self._last_timeout_seconds = int(spec.get("timeout_ms", 120_000) or 120_000) / 1000

        request_path = self.out_dir / f"{Path(spec_path).stem}-openra-request.json"
        report_path = self.out_dir / f"{Path(spec_path).stem}-openra-result.json"
        stdout_path = self.out_dir / f"{Path(spec_path).stem}-openra-stdout.log"
        stderr_path = self.out_dir / f"{Path(spec_path).stem}-openra-stderr.log"
        screenshot_target = self.out_dir / f"{Path(spec_path).stem}-openra-after.png"
        request = {
            "spec_path": str(Path(spec_path).resolve()),
            "spec": spec,
        }
        request_path.write_text(json_dumps(request), encoding="utf-8")

        # Reset the in-process capture; only the gameplay path (below) fills it in, so a
        # non-gameplay validation spec falls back to the clean cold-launch screenshot.
        self._gameplay_screenshot_path = None

        if _is_openra_gameplay_spec(spec):
            if not map_id:
                raise RuntimeError("OpenRA gameplay production specs require target.map")
            return self._run_gameplay_checkpoint(
                spec, mod, map_id, request_path, report_path, stdout_path, stderr_path, screenshot_target
            )

        cmd = ["./utility.sh", mod, "--warptest-checkpoint", str(request_path), str(report_path)]
        env = {**os.environ, "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major")}
        completed = _run_with_process_group(
            cmd,
            cwd=self.repo,
            timeout=int(spec.get("timeout_ms", 120_000) or 120_000) / 1000,
            env=env,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])

        utility_report: dict[str, Any] = {}
        if report_path.exists():
            try:
                utility_report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as error:
                utility_report = {"status": "failure", "detail": f"Unable to parse OpenRA report JSON: {error}"}

        status = "success" if completed.returncode == 0 and utility_report.get("status") == "success" else "failure"
        return {
            "status": status,
            "exit_code": completed.returncode,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "utility_report": utility_report,
            "checks": utility_report.get("checks", []) if isinstance(utility_report, dict) else [],
            "visual_capture": "openra_gameplay_screenshot",
        }

    def _run_gameplay_checkpoint(
        self,
        spec: dict[str, Any],
        mod: str,
        map_id: str,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        screenshot_target: Path,
    ) -> dict[str, Any]:
        support_dir = self.out_dir / "openra-support"
        support_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_openra_content(mod, support_dir)
        if report_path.exists():
            report_path.unlink()
        screenshot_target.parent.mkdir(parents=True, exist_ok=True)
        if screenshot_target.exists():
            screenshot_target.unlink()

        # Capture the post-action frame in the SAME process that runs the gameplay probe:
        # the engine defers this screenshot until the probe finishes its build actions and
        # recenters the camera on the constructed base, so the frame reflects the real
        # state change (deployed MCV, buildings, produced units) instead of a clean launch.
        cmd = [
            "./launch-game.sh",
            f"Game.Mod={mod}",
            f"Engine.SupportDir={support_dir}",
            f"Launch.Map={map_id}",
            f"Launch.WarptestGameplayRequestPath={request_path.resolve()}",
            f"Launch.WarptestGameplayReportPath={report_path.resolve()}",
            f"Launch.WarptestScreenshotPath={screenshot_target.resolve()}",
            f"Launch.WarptestScreenshotFrame={_GAMEPLAY_SETTLE_FRAMES}",
            "Launch.WarptestExitAfterScreenshot=true",
            "Game.FetchNews=false",
            "Game.EnableDiscordService=false",
            "Sound.Mute=true",
            "Server.AdvertiseOnline=false",
            "Server.AdvertiseOnLocalNetwork=false",
        ]
        env = {**os.environ, "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major")}
        # Prefer this post-action frame for the visual oracle; screenshot() verifies it
        # exists and is informative, otherwise it falls back to the clean cold launch.
        self._gameplay_screenshot_path = screenshot_target
        try:
            completed = _run_with_process_group(
                cmd,
                cwd=self.repo,
                timeout=int(spec.get("timeout_ms", 120_000) or 120_000) / 1000,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            stdout_path.write_text(_process_output_text(error.stdout), encoding="utf-8")
            stderr_path.write_text(_process_output_text(error.stderr), encoding="utf-8")
            self.logs.extend([str(stdout_path), str(stderr_path)])
            return {
                "status": "failure",
                "exit_code": None,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_tail": _process_output_text(error.stdout)[-4000:],
                "stderr_tail": _process_output_text(error.stderr)[-4000:],
                "utility_report": {"status": "failure", "detail": f"OpenRA gameplay probe timed out after {error.timeout} seconds"},
                "checks": [{"name": "probe.timeout", "status": "failure", "detail": f"OpenRA gameplay probe timed out after {error.timeout} seconds"}],
                "visual_capture": "openra_gameplay_probe",
            }

        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])

        gameplay_report: dict[str, Any] = {}
        if report_path.exists():
            try:
                gameplay_report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as error:
                gameplay_report = {"status": "failure", "detail": f"Unable to parse OpenRA gameplay report JSON: {error}"}
        else:
            gameplay_report = {"status": "failure", "detail": "OpenRA gameplay probe did not write a report."}

        status = "success" if completed.returncode == 0 and gameplay_report.get("status") == "success" else "failure"
        return {
            "status": status,
            "exit_code": completed.returncode,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "utility_report": gameplay_report,
            "checks": gameplay_report.get("checks", []) if isinstance(gameplay_report, dict) else [],
            "visual_capture": "openra_gameplay_probe",
        }

    def screenshot(self, name: str = "openra-gameplay.png", full_page: bool = False) -> str:
        mod = self._last_target.get("mod")
        map_id = self._last_target.get("map")
        if not mod or not map_id:
            raise RuntimeError("OpenRA screenshot requested before a map checkpoint was loaded.")
        target = self.out_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)

        # Prefer the post-action frame captured inside the gameplay probe process (real
        # state change). Fall back to a clean cold launch for validation specs, or if the
        # probe crashed/timed out before writing an informative frame.
        source = self._gameplay_screenshot_path
        if source is not None and source.exists() and _screenshot_file_informative(source):
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
            self._last_screenshot_path = target
            return str(target)

        self._capture_gameplay_screenshot(mod, map_id, target)
        self._last_screenshot_path = target
        return str(target)

    def _capture_gameplay_screenshot(self, mod: str, map_id: str, target: Path) -> None:
        self._ensure_utility_built()
        stdout_path = self.out_dir / "openra-gameplay-stdout.log"
        stderr_path = self.out_dir / "openra-gameplay-stderr.log"
        support_dir = self.out_dir / "openra-support"
        support_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_openra_content(mod, support_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()

        cmd = [
            "./launch-game.sh",
            f"Game.Mod={mod}",
            f"Engine.SupportDir={support_dir}",
            f"Launch.Map={map_id}",
            f"Launch.WarptestScreenshotPath={target.resolve()}",
            "Launch.WarptestScreenshotFrame=12",
            "Launch.WarptestExitAfterScreenshot=true",
            "Game.FetchNews=false",
            "Game.EnableDiscordService=false",
            "Sound.Mute=true",
            "Server.AdvertiseOnline=false",
            "Server.AdvertiseOnLocalNetwork=false",
        ]
        env = {**os.environ, "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major")}
        try:
            completed = _run_until_informative_file_with_process_group(
                cmd,
                cwd=self.repo,
                timeout=max(45.0, self._last_timeout_seconds),
                env=env,
                target=target,
            )
        except subprocess.TimeoutExpired as error:
            stdout_path.write_text(_process_output_text(error.stdout), encoding="utf-8")
            stderr_path.write_text(_process_output_text(error.stderr), encoding="utf-8")
            self.logs.extend([str(stdout_path), str(stderr_path)])
            raise RuntimeError(
                f"OpenRA gameplay screenshot launch timed out after {error.timeout} seconds; "
                f"see {stdout_path}, {stderr_path}, and {support_dir / 'Logs'}"
            ) from error
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])

        if completed.returncode != 0:
            raise RuntimeError(
                f"OpenRA gameplay screenshot launch failed with exit code {completed.returncode}; "
                f"see {stdout_path} and {stderr_path}"
            )
        if not target.exists():
            raise RuntimeError(f"OpenRA gameplay screenshot was not written: {target}")
        if not _screenshot_file_informative(target):
            raise RuntimeError(f"OpenRA gameplay screenshot is blank or non-informative: {target}")

    def _ensure_openra_content(self, mod: str, support_dir: Path) -> None:
        download = _load_quick_download(self.repo, mod)
        install_log = self.out_dir / f"openra-{mod}-content-install.log"
        required_targets = [_resolve_openra_content_target(support_dir, target) for target in download["extract"]]
        if required_targets and all(target.exists() and target.stat().st_size > 0 for target in required_targets):
            install_log.write_text(f"OpenRA {mod} quick content already present in {support_dir}\n", encoding="utf-8")
            self.logs.append(str(install_log))
            return

        cache_dir = Path(os.environ.get("WARPTEST_OPENRA_CONTENT_CACHE", Path.home() / ".cache" / "warptest" / "openra-content"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive = _download_openra_content_archive(download, cache_dir, install_log)
        _extract_openra_content_archive(archive, support_dir, download, install_log)
        missing = [target for target in required_targets if not target.exists() or target.stat().st_size == 0]
        if missing:
            raise RuntimeError(
                "OpenRA quick content install did not produce required files: "
                + ", ".join(str(path) for path in missing[:5])
            )
        self.logs.append(str(install_log))

    def _map_file(self, mod: str, map_id: str, file_name: str) -> Path:
        maps_root = (self.repo / "mods" / mod / "maps").resolve()
        path = (maps_root / map_id / file_name).resolve()
        if not str(path).startswith(str(maps_root) + os.sep):
            raise RuntimeError(f"OpenRA map path escapes maps directory: {mod}/{map_id}")
        return path

    def _ensure_utility_built(self) -> None:
        utility_dll = self.repo / "bin" / "OpenRA.Utility.dll"
        game_dll = self.repo / "bin" / "OpenRA.dll"
        command_source = self.repo / "OpenRA.Mods.Common" / "UtilityCommands" / "WarptestCheckpointCommand.cs"
        harness_sources = [
            command_source,
            self.repo / "OpenRA.Platforms.Default" / "Sdl2PlatformWindow.cs",
            self.repo / "OpenRA.Game" / "Game.cs",
            self.repo / "OpenRA.Game" / "Renderer.cs",
            self.repo / "OpenRA.Game" / "Support" / "LaunchArguments.cs",
            self.repo / "OpenRA.Mods.Common" / "LoadScreens" / "BlankLoadScreen.cs",
            self.repo / "OpenRA.Mods.Common" / "Traits" / "World" / "WarptestGameplayProbe.cs",
        ]
        platform_dll = self.repo / "bin" / "OpenRA.Platforms.Default.dll"
        common_dll = self.repo / "bin" / "OpenRA.Mods.Common.dll"
        outputs = [utility_dll, game_dll, platform_dll, common_dll]
        needs_build = any(not output.exists() for output in outputs)
        if not needs_build:
            oldest_output_mtime = min(output.stat().st_mtime for output in outputs)
            needs_build = any(source.exists() and source.stat().st_mtime > oldest_output_mtime for source in harness_sources)
        if not needs_build:
            return

        cmd = ["dotnet", "build", "-c", "Debug", "--nologo", f"-p:TargetPlatform={_target_platform()}"]
        log_path = self.out_dir / "openra-build.log"
        completed = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True, timeout=300)
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        self.logs.append(str(log_path))
        if completed.returncode != 0:
            raise RuntimeError(f"OpenRA build failed; see {log_path}")
        missing_outputs = [output for output in outputs if not output.exists()]
        newest_output_mtime = (
            max(output.stat().st_mtime for output in outputs)
            if not missing_outputs
            else 0
        )
        newest_source_mtime = max(
            (source.stat().st_mtime for source in harness_sources if source.exists()),
            default=0,
        )
        stale_outputs = missing_outputs or (
            outputs if newest_output_mtime < newest_source_mtime else []
        )
        if stale_outputs:
            raise RuntimeError(
                "OpenRA build did not refresh registered C1 runtime outputs: "
                + ", ".join(str(path) for path in stale_outputs)
            )


def _safe_openra_id(value: str, field: str) -> str:
    if not value or not _SAFE_OPENRA_ID.fullmatch(value):
        raise RuntimeError(f"OpenRA {field} must be a simple mod/map id, got: {value!r}")
    if ".." in value:
        raise RuntimeError(f"OpenRA {field} must not contain parent traversal, got: {value!r}")
    return value


def _is_openra_gameplay_spec(spec: dict[str, Any]) -> bool:
    checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
    strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else []
    if isinstance(strategies, list) and "openra_gameplay_production_validate" in strategies:
        return True
    actions = spec.get("actions") if isinstance(spec.get("actions"), list) else []
    return any(
        isinstance(action, dict)
        and action.get("type")
        in {
            "openra_deploy_actor",
            "openra_build_actor",
            "openra_build_group",
            "openra_rally_group",
            "openra_wait_ticks",
        }
        for action in actions
    )


def _screenshot_file_informative(path: Path) -> bool:
    data = path.read_bytes()
    return len(data) >= 2_000 and len(set(data)) >= 32


def _run_with_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        stdout, stderr = _communicate_after_terminate(process)
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from error
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _run_until_informative_file_with_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
    target: Path,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if target.exists():
                try:
                    if _screenshot_file_informative(target):
                        _terminate_process_group(process)
                        stdout, stderr = _communicate_after_terminate(process)
                        return subprocess.CompletedProcess(cmd, 0, stdout, stderr)
                except OSError:
                    pass

            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

            if time.monotonic() >= deadline:
                _terminate_process_group(process)
                stdout, stderr = _communicate_after_terminate(process)
                raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)

            time.sleep(0.25)
    except subprocess.TimeoutExpired:
        raise
    except BaseException:
        _terminate_process_group(process)
        _communicate_after_terminate(process)
        raise


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()


def _communicate_after_terminate(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            process.kill()
        return process.communicate(timeout=5)


def _process_output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _load_quick_download(repo: Path, mod: str) -> dict[str, Any]:
    mod_yaml = repo / "mods" / f"{mod}-content" / "mod.yaml"
    downloads_yaml = repo / "mods" / f"{mod}-content" / "installer" / "downloads.yaml"
    if not mod_yaml.exists() or not downloads_yaml.exists():
        raise RuntimeError(f"OpenRA quick content metadata is missing for mod {mod!r}")

    quick_download = None
    for line in mod_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("QuickDownload:"):
            quick_download = stripped.split(":", 1)[1].strip()
            break
    if not quick_download:
        raise RuntimeError(f"OpenRA mod {mod!r} does not declare ModContent.QuickDownload")

    return _parse_download_definition(downloads_yaml, quick_download)


def _parse_download_definition(path: Path, key: str) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{key}:")), None)
    if start is None:
        raise RuntimeError(f"OpenRA quick download {key!r} was not found in {path}")

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        block.append(line)

    download: dict[str, Any] = {"key": key, "extract": {}}
    in_extract = False
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Extract:":
            in_extract = True
            continue
        if in_extract:
            if line[:1].isspace() and ":" in stripped:
                target, source = stripped.split(":", 1)
                download["extract"][target.strip()] = source.strip()
                continue
            in_extract = False
        if ":" in stripped:
            field, value = stripped.split(":", 1)
            if field in {"Type", "SHA1", "URL", "MirrorList"}:
                download[field.lower()] = value.strip()

    if download.get("type") != "ZipFile":
        raise RuntimeError(f"OpenRA quick download {key!r} must be a ZipFile package")
    if not download["extract"]:
        raise RuntimeError(f"OpenRA quick download {key!r} has no Extract entries")
    if not (download.get("url") or download.get("mirrorlist")):
        raise RuntimeError(f"OpenRA quick download {key!r} has no URL or MirrorList")
    return download


def _download_openra_content_archive(download: dict[str, Any], cache_dir: Path, log_path: Path) -> Path:
    sha1 = str(download.get("sha1") or "")
    archive = cache_dir / f"{download['key']}-{sha1 or 'unknown'}.zip"
    log_lines = [f"Preparing OpenRA quick content package {download['key']}"]
    if archive.exists() and (not sha1 or _sha1_file(archive) == sha1):
        log_lines.append(f"Using cached archive {archive}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return archive

    urls = _openra_content_urls(download)
    errors: list[str] = []
    for url in urls:
        try:
            log_lines.append(f"Downloading {url}")
            with urllib.request.urlopen(url, timeout=60) as response:
                if getattr(response, "status", 200) != 200:
                    errors.append(f"{url}: HTTP {getattr(response, 'status', 'unknown')}")
                    continue
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    temp_path = Path(tmp.name)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp.write(chunk)
            if sha1 and _sha1_file(temp_path) != sha1:
                errors.append(f"{url}: SHA1 mismatch")
                temp_path.unlink(missing_ok=True)
                continue
            temp_path.replace(archive)
            log_lines.append(f"Cached archive {archive}")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return archive
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            errors.append(f"{url}: {error}")

    log_path.write_text("\n".join(log_lines + errors) + "\n", encoding="utf-8")
    raise RuntimeError("Unable to download OpenRA quick content package; see " + str(log_path))


def _openra_content_urls(download: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    if download.get("url"):
        urls.append(str(download["url"]))
    if download.get("mirrorlist"):
        with urllib.request.urlopen(str(download["mirrorlist"]), timeout=30) as response:
            mirrors = response.read().decode("utf-8", errors="replace").splitlines()
        urls.extend(line.strip() for line in mirrors if line.strip() and not line.strip().startswith("#"))
    if not urls:
        raise RuntimeError(f"OpenRA quick download {download['key']!r} has no usable download URLs")
    return urls


def _extract_openra_content_archive(archive: Path, support_dir: Path, download: dict[str, Any], log_path: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        with log_path.open("a", encoding="utf-8") as log:
            for target, source in download["extract"].items():
                if source not in names:
                    log.write(f"Skipping missing archive entry {source}\n")
                    continue
                target_path = _resolve_openra_content_target(support_dir, target)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with package.open(source) as src, target_path.open("wb") as dst:
                    dst.write(src.read())
                log.write(f"Extracted {source} -> {target_path}\n")


def _resolve_openra_content_target(support_dir: Path, target: str) -> Path:
    if not target.startswith("^SupportDir|"):
        raise RuntimeError(f"OpenRA quick content target must be under ^SupportDir: {target}")
    resolved = (support_dir / target.split("|", 1)[1]).resolve()
    support_root = support_dir.resolve()
    if resolved != support_root and support_root not in resolved.parents:
        raise RuntimeError(f"OpenRA quick content target escapes support dir: {target}")
    return resolved


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _target_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "osx-arm64" if machine in {"arm64", "aarch64"} else "osx-x64"
    if sys.platform.startswith("linux"):
        return "linux-arm64" if machine in {"arm64", "aarch64"} else "linux-x64"
    if sys.platform.startswith("win"):
        return "win-x64"
    return "unix-generic"
