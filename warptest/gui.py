from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GenericGuiInspection:
    repo_path: str
    runtime_family: str
    launchable: bool
    launch_command: str | None
    checkpointing_status: str
    adapter_boundary: str
    supported_evidence_channels: list[str]
    unsupported_surfaces: list[str]
    runtime_probe: dict[str, Any]
    evidence: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GenericGuiAppAdapter:
    """Boundary adapter for GUI artifacts beyond the current browser-game path."""

    def inspect(self, repo: str | Path) -> GenericGuiInspection:
        path = Path(repo).expanduser().resolve()
        package_json = _read_json(path / "package.json")
        deps = {
            **(package_json.get("dependencies", {}) if isinstance(package_json.get("dependencies"), dict) else {}),
            **(package_json.get("devDependencies", {}) if isinstance(package_json.get("devDependencies"), dict) else {}),
        }
        scripts = package_json.get("scripts", {}) if isinstance(package_json.get("scripts"), dict) else {}
        evidence: list[dict[str, Any]] = []
        if (path / "index.html").exists() or "vite" in deps or "dev" in scripts:
            evidence.append({"path": "index.html/package.json", "symbol": "browser_runtime"})
            return GenericGuiInspection(
                str(path),
                "browser-web",
                True,
                _browser_launch_command(scripts),
                "supported_by_browser_runtime_adapter",
                "Current evidence supports source-available browser-web GUI artifacts.",
                ["dom", "keyboard", "mouse", "screenshot", "localStorage", "console"],
                ["native_desktop_widgets", "mobile_runtime"],
                {"status": "supported_by_existing_browser_runtime"},
                evidence,
            )
        if "electron" in deps or "electron" in " ".join(scripts.values()).lower():
            evidence.append({"path": "package.json", "symbol": "electron"})
            launch_command = _electron_launch_command(scripts)
            return GenericGuiInspection(
                str(path),
                "electron",
                True,
                launch_command,
                "supported_by_electron_runtime_probe",
                "Current evidence supports source-available Electron GUI artifacts through Playwright Electron smoke probing.",
                ["electron_app", "browser_window_dom", "keyboard", "mouse", "screenshot", "console"],
                ["native_desktop_widgets_outside_browser_window", "mobile_runtime"],
                _probe_electron(path, launch_command),
                evidence,
            )
        if any((path / marker).exists() for marker in ["pyproject.toml", "Cargo.toml", "Package.swift"]):
            evidence.append({"path": str(path), "symbol": "native_project_marker"})
            return GenericGuiInspection(
                str(path),
                "native-desktop",
                False,
                None,
                "explicitly_unsupported_for_v1",
                "Native GUI adapters are future work and must not be used for current claims.",
                [],
                ["native_desktop_widgets", "mobile_runtime"],
                {"status": "not_applicable"},
                evidence,
            )
        return GenericGuiInspection(
            str(path),
            "unknown",
            False,
            None,
            "unsupported",
            "No supported GUI runtime was detected.",
            [],
            ["unknown_runtime"],
            {"status": "not_applicable"},
            evidence,
        )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _browser_launch_command(scripts: dict[str, str]) -> str:
    if "dev" in scripts:
        return "npm run dev -- --host 127.0.0.1 --port {port}"
    if "start" in scripts:
        return "npm run start"
    return "python3 -m http.server {port}"


def _electron_launch_command(scripts: dict[str, str]) -> str:
    if "start" in scripts:
        return "npm run start"
    if "electron" in scripts:
        return "npm run electron"
    return "npx electron ."


def _probe_electron(path: Path, launch_command: str) -> dict[str, Any]:
    electron_binary = path / "node_modules" / ".bin" / "electron"
    if not electron_binary.exists():
        return {
            "status": "skipped_missing_electron_binary",
            "launch_command": launch_command,
            "detail": "Electron runtime detected from package metadata, but node_modules/.bin/electron is not installed.",
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as error:
        return {"status": "skipped_missing_playwright", "launch_command": launch_command, "detail": str(error)}
    try:
        with sync_playwright() as playwright:
            launcher = getattr(playwright, "_electron", None)
            return {
                "status": "available" if launcher is not None else "available_without_public_electron_launcher",
                "launch_command": launch_command,
                "detail": "Playwright is installed and Electron binary is present; Electron smoke probing is available for benchmark runs.",
            }
    except Exception as error:
        return {"status": "probe_error", "launch_command": launch_command, "detail": str(error)}
