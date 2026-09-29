from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .models import (
    CapabilityReport,
    InspectResult,
    RepoMetadata,
    SemanticEvidence,
    SemanticField,
    SemanticSchemaReport,
    SemanticSurface,
    StoragePolicy,
)


MAX_DISCOVERY_TEXT_FILE_BYTES = 1_000_000


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _git_commit(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _files(repo: Path, suffixes: tuple[str, ...]) -> list[Path]:
    ignored = {
        "node_modules",
        "lib",
        ".git",
        "dist",
        "build",
        "build-app",
        "build-debug",
        "build-release",
        "out",
        ".tmp",
        "reports",
        "tests",
        "test",
        "__tests__",
        "e2e",
        "docs",
        "doc",
        ".github",
        # Unity-generated/project-local caches can contain many package sources
        # and binary-adjacent assets; they are not benchmark source evidence.
        "Library",
        "Temp",
        "Logs",
        "UserSettings",
        "Plugins",
        "obj",
        "bin",
        "Gen",
        "Generated",
    }
    out: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in ignored]
        root_path = Path(root)
        for file_name in files:
            path = root_path / file_name
            if path.suffix in suffixes:
                out.append(path)
    return out


def _text_hits(paths: list[Path], needle: str) -> list[str]:
    hits: list[str] = []
    for path in paths:
        try:
            if _path_contains(path, needle):
                hits.append(str(path))
        except Exception:
            continue
    return hits


def _relative(repo: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _first_path_containing(repo: Path, paths: list[Path], needle: str) -> Path | None:
    for path in paths:
        try:
            if _path_contains(path, needle):
                return path
        except Exception:
            continue
    return None


def _path_contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size > MAX_DISCOVERY_TEXT_FILE_BYTES:
        return False
    return needle in path.read_text(encoding="utf-8", errors="ignore")


def _read_text_sample(path: Path, max_chars: int = 80_000) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_DISCOVERY_TEXT_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def _evidence(repo: Path, path: Path | None, symbol: str, confidence: float, detail: str = "") -> list[SemanticEvidence]:
    source_path = _relative(repo, path) or "<unknown>"
    return [SemanticEvidence(source_path=source_path, matched_symbol=symbol, confidence=confidence, detail=detail)]


def _field(
    repo: Path,
    name: str,
    source: str,
    path: Path | None,
    symbol: str,
    readable_via: list[str],
    writable_via: list[str],
    confidence: float = 0.85,
) -> SemanticField:
    return SemanticField(
        name=name,
        source=source,
        readable_via=readable_via,
        writable_via=writable_via,
        evidence=_evidence(repo, path, symbol, confidence),
    )


class RepositoryInspector:
    """Read-only capability detection for source-available game repositories."""

    def inspect(self, repo_path: str | Path) -> InspectResult:
        repo = Path(repo_path).expanduser().resolve()
        if not repo.exists() or not repo.is_dir():
            raise FileNotFoundError(f"Repository path does not exist or is not a directory: {repo}")

        package_json = _read_json(repo / "package.json")
        scripts = package_json.get("scripts", {}) if isinstance(package_json.get("scripts"), dict) else {}
        deps = {
            **(package_json.get("dependencies", {}) if isinstance(package_json.get("dependencies"), dict) else {}),
            **(package_json.get("devDependencies", {}) if isinstance(package_json.get("devDependencies"), dict) else {}),
        }
        ts_js_paths = _files(repo, (".ts", ".tsx", ".js", ".jsx", ".html"))
        source_paths = _files(repo, (".ts", ".tsx", ".js", ".jsx", ".html", ".cs", ".cc", ".cpp", ".h", ".hh", ".py", ".yaml", ".yml", ".lua"))
        all_source = "\n".join(_read_text_sample(path) for path in source_paths[:350])

        package_manager = None
        if (repo / "package-lock.json").exists():
            package_manager = "npm"
        elif (repo / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (repo / "yarn.lock").exists():
            package_manager = "yarn"

        profile = self._profile_for(repo)
        hook_name = self._hook_name(profile)
        is_openra = profile == "OpenRAProfile"
        is_jupyterlab = profile == "JupyterLabProfile"
        is_libreoffice = profile == "LibreOfficeProfile"
        is_blender = profile == "BlenderProfile"
        is_jynew = profile == "JynewProfile"
        is_pal3 = profile == "Pal3Profile"
        is_luanti = profile == "LuantiProfile"
        is_native = is_openra or is_libreoffice or is_blender or is_jynew or is_pal3 or is_luanti
        hook_available = bool(hook_name and hook_name in all_source) or (
            is_openra and (repo / "OpenRA.Mods.Common" / "UtilityCommands" / "WarptestCheckpointCommand.cs").exists()
        ) or (
            is_jupyterlab
            and (repo / "jupyterlab" / "galata" / "__init__.py").exists()
            and (repo / "jupyterlab" / "labapp.py").exists()
        ) or (
            is_libreoffice
            and (repo / "desktop" / "source" / "app" / "app.cxx").exists()
            and (repo / "include" / "LibreOfficeKit" / "LibreOfficeKit.h").exists()
        ) or (
            is_blender
            and (repo / "source" / "creator" / "creator_args.cc").exists()
            and (repo / "scripts").is_dir()
        ) or (
            is_jynew and any(
                (repo / sub / "Assets" / "Scripts" / "GameCore" / "WarptestCheckpointCommand.cs").exists()
                for sub in ["jyx2", "."]
            )
        ) or (
            is_pal3 and any(
                (repo / sub / "Assets" / "Scripts" / "Pal3.Game" / "Command" / "WarptestCheckpointCommand.cs").exists()
                for sub in [".", "Pal3.Unity"]
            )
        ) or (
            is_luanti and (
                (repo / "mods" / "warptest" / "init.lua").exists()
                or (repo / "src" / "client" / "game.cpp").exists()
            )
        )

        zustand_paths = [
            str(path.relative_to(repo))
            for path in ts_js_paths
            if path.suffix in {".ts", ".tsx", ".js", ".jsx"}
            and _path_contains(path, "zustand")
        ]
        has_zustand = "zustand" in deps or bool(zustand_paths)
        has_runtime_zustand = hook_available and profile in {"SpaceRPGProfile", "DatingSimProfile"}

        capabilities = CapabilityReport(
            browser_runtime=False if is_native else (repo / "index.html").exists() or bool(package_json),
            vite=(repo / "vite.config.ts").exists() or "vite" in deps or "vite" in scripts.get("dev", ""),
            static_html=(repo / "index.html").exists() and not package_json,
            react="react" in deps or "React" in all_source,
            vanilla_dom=profile == "Game2048Profile",
            three="three" in deps,
            react_three_fiber="@react-three/fiber" in deps,
            webgl_or_canvas=("canvas" in all_source.lower() or "webgl" in all_source.lower() or "three" in deps),
            local_storage="localStorage" in all_source,
            indexed_db="indexedDB" in all_source,
            playwright="@playwright/test" in deps or (repo / "playwright.config.ts").exists(),
            vitest="vitest" in deps or (repo / "vitest.config.ts").exists(),
            backend_or_external_api=True if is_jupyterlab or is_libreoffice else (False if is_native else ("openai" in deps or "fetch(" in all_source or "WebSocket" in all_source)),
            deterministic_seed=is_native or "seed" in all_source.lower() or profile == "DatingSimProfile",
            hook_available=hook_available,
            hook_name=hook_name,
            zustand_detected=has_zustand,
            zustand_source_paths=zustand_paths,
            zustand_runtime_access_available=has_runtime_zustand,
            zustand_runtime_access_method="test_hook" if has_runtime_zustand else "unsupported",
        )

        launch_command = self._launch_command(repo, package_json, profile)
        metadata = RepoMetadata(
            repo_path=str(repo),
            repo_name=repo.name,
            git_commit=_git_commit(repo),
            package_manager=package_manager,
            launch_command=launch_command,
            app_url=None,
            build_command=self._script_command(package_manager, "build", scripts),
            test_command=self._script_command(package_manager, "test", scripts),
            scripts=scripts,
        )
        storage_policy = self.storage_policy_for(profile)
        adapter_layers = self.adapter_layers_for(profile, capabilities)
        instrumentation_mode = (
            "existing_hook"
            if profile == "SpaceRPGProfile" and hook_available
            else "in_place_test_gated"
            if profile in {"Game2048Profile", "DatingSimProfile", "OpenRAProfile", "JynewProfile", "Pal3Profile", "LuantiProfile"}
            else "none"
        )
        semantic_schema = self.semantic_schema_for(repo, profile, source_paths, capabilities, storage_policy)

        return InspectResult(metadata, profile, adapter_layers, capabilities, storage_policy, instrumentation_mode, semantic_schema)

    @staticmethod
    def _profile_for(repo: Path) -> str:
        name = repo.name.lower()
        if (repo / "OpenRA.sln").exists() and (repo / "OpenRA.Game").is_dir() and (repo / "mods").is_dir():
            return "OpenRAProfile"
        if (
            (repo / "pyproject.toml").exists()
            and (repo / "jupyterlab" / "labapp.py").exists()
        ):
            return "JupyterLabProfile"
        # C1 sandboxes use the neutral leaf name ``repo``.  Native profile
        # detection therefore has to rely on tracked project structure rather
        # than the checkout directory name, just like the web/Unity profiles
        # below.  Requiring the original name here made reference-replay audits
        # misclassify sandboxed LibreOffice and Blender checkouts as generic web
        # repositories and fail before the model was ever called.
        if (repo / "configure.ac").exists() and (
            repo / "desktop" / "source" / "app"
        ).is_dir():
            return "LibreOfficeProfile"
        if (
            (repo / "source" / "creator" / "creator_args.cc").exists()
            and (repo / "scripts").is_dir()
        ):
            return "BlenderProfile"
        # C1 task sandboxes intentionally use the neutral leaf directory
        # ``repo``.  Keep name-based detection for normal checkouts, but also
        # recognize SpaceRPG from its stable tracked source layout so cloned
        # checkouts do not silently fall back to GenericWebProfile.
        if "spacerpg" in name or (
            (repo / "src" / "state" / "gameStore.ts").is_file()
            and (repo / "src" / "data" / "systems.ts").is_file()
            and (repo / "src" / "components" / "FlightScene.tsx").is_file()
        ):
            return "SpaceRPGProfile"
        if name == "2048" or (repo / "js" / "game_manager.js").exists():
            return "Game2048Profile"
        if "datingsim" in name or (repo / "src" / "store" / "useGameStore.ts").exists():
            return "DatingSimProfile"
        if "jynew" in name or "jyx2" in name or (repo / "jyx2" / "Assets" / "Scripts" / "GameSave").is_dir():
            return "JynewProfile"
        if "pal3" in name or (repo / "Assets" / "Scripts" / "Pal3.Game").is_dir():
            return "Pal3Profile"
        if "luanti" in name or "minetest" in name or (
            (repo / "builtin" / "init.lua").exists()
            and (repo / "games" / "devtest").is_dir()
            and (repo / "src" / "client" / "game.cpp").exists()
        ):
            return "LuantiProfile"
        return "GenericWebProfile"

    @staticmethod
    def _hook_name(profile: str) -> str | None:
        return {
            "SpaceRPGProfile": "__SPACERPG_E2E__",
            "Game2048Profile": "__WARPTEST_2048__",
            "DatingSimProfile": "__WARPTEST_LOVE__",
            "OpenRAProfile": "--warptest-checkpoint",
            "JupyterLabProfile": "jupyter server contents/session api",
            "LibreOfficeProfile": "uno/odf document api",
            "BlenderProfile": "bpy/operator api",
            "JynewProfile": "--warptest-checkpoint",
            "Pal3Profile": "--warptest-checkpoint",
            "LuantiProfile": "warptest",
        }.get(profile)

    @staticmethod
    def _script_command(package_manager: str | None, script: str, scripts: dict[str, str]) -> str | None:
        if script not in scripts or not package_manager:
            return None
        return f"{package_manager} run {script}"

    @staticmethod
    def _launch_command(repo: Path, package_json: dict[str, Any], profile: str) -> str:
        if profile == "OpenRAProfile":
            return "./utility.sh {mod} --warptest-checkpoint {request} {report}"
        if profile == "JupyterLabProfile":
            return "python -m jupyterlab --no-browser --ServerApp.ip=127.0.0.1 --ServerApp.port={port} --ServerApp.token= --ServerApp.password= --ServerApp.disable_check_xsrf=True"
        if profile == "LibreOfficeProfile":
            return "soffice --headless --accept=pipe,name={pipe};urp;"
        if profile == "BlenderProfile":
            return "Blender --factory-startup --python warptest/blender_driver.py -- {request} {report}"
        if profile == "JynewProfile":
            return "unity -batchmode -nographics -projectPath {project} -executeMethod Jyx2.WarptestCheckpoint.Run -- --warptest-request {request} --warptest-report {report}"
        if profile == "Pal3Profile":
            return "unity -batchmode -nographics -projectPath {project} -executeMethod Pal3.Game.Command.WarptestCheckpoint.Run -- --warptest-request {request} --warptest-report {report}"
        if profile == "LuantiProfile":
            return "xvfb-run -a ./bin/luanti --go --gameid devtest --world {world} --name singleplayer --config {config}"
        scripts = package_json.get("scripts", {}) if isinstance(package_json.get("scripts"), dict) else {}
        if "dev" in scripts:
            return "npm run dev -- --host 127.0.0.1 --port {port}"
        if "preview" in scripts:
            return "npm run preview -- --host 127.0.0.1 --port {port}"
        if (repo / "index.html").exists():
            return "python3 -m http.server {port} --bind 127.0.0.1"
        raise RuntimeError(f"No supported browser launch command found for {repo}")

    @staticmethod
    def storage_policy_for(profile: str) -> StoragePolicy:
        if profile == "Game2048Profile":
            return StoragePolicy(
                required_keys=["gameState"],
                safe_to_clear=["gameState", "bestScore"],
                safe_to_overwrite=["gameState", "bestScore"],
            )
        if profile == "SpaceRPGProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=["spacerpg-save", "spacerpg-save-index"],
                safe_to_overwrite=[
                    "spacerpg-e2e-hook",
                    "spacerpg-e2e-disable-economy-backend",
                    "spacerpg-save",
                    "spacerpg-save-index",
                    "spacerpg-save-slot:auto",
                    "spacerpg-save-slot:manual-1",
                    "spacerpg-save-slot:manual-2",
                    "spacerpg-save-slot:manual-3",
                ],
                preserve_keys=["spacerpg-e2e-hook", "spacerpg-e2e-disable-economy-backend"],
                namespace_prefixes=["spacerpg-save-slot:"],
            )
        if profile == "DatingSimProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=["love_emulator_save_v1"],
                safe_to_overwrite=["love_emulator_save_v1", "warptest-love-ai-mode"],
                preserve_keys=["warptest-love-ai-mode"],
            )
        if profile == "JupyterLabProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["jupyterlab-runtime/", "workspaces/", "root/"],
            )
        if profile == "LibreOfficeProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["libreoffice-runtime/", "artifacts/", "user/"],
            )
        if profile == "BlenderProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["blender/", "artifacts/", "user/"],
            )
        if profile == "JynewProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["jyx2/archive_"],
            )
        if profile == "Pal3Profile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["Saves/slot_"],
            )
        if profile == "LuantiProfile":
            return StoragePolicy(
                required_keys=[],
                safe_to_clear=[],
                safe_to_overwrite=[],
                namespace_prefixes=["worldmods/warptest", "mod_storage"],
            )
        return StoragePolicy()

    @staticmethod
    def adapter_layers_for(profile: str, capabilities: CapabilityReport) -> list[str]:
        if profile == "OpenRAProfile":
            return ["OpenRARuntimeAdapter", "OpenRAUtilityCheckpointAdapter"]
        if profile == "JupyterLabProfile":
            return ["JupyterLabRuntimeAdapter", "JupyterServerContentsAPI", "JupyterKernelSessionAdapter", "BrowserRuntimeAdapter"]
        if profile == "LibreOfficeProfile":
            return ["LibreOfficeRuntimeAdapter", "LibreOfficeUNOAdapter", "ODFDocumentArtifactAdapter"]
        if profile == "BlenderProfile":
            return ["BlenderRuntimeAdapter", "BlenderPythonSceneAdapter", "BlenderGUIAppBundle"]
        if profile == "JynewProfile":
            return ["JynewRuntimeAdapter", "JynewUtilityCheckpointAdapter"]
        if profile == "Pal3Profile":
            return ["Pal3RuntimeAdapter", "Pal3UtilityCheckpointAdapter"]
        if profile == "LuantiProfile":
            return ["LuantiRuntimeAdapter", "LuantiWorldmodCheckpointAdapter"]
        layers = ["BrowserRuntimeAdapter"]
        if capabilities.react:
            layers.append("ReactAdapter")
        if capabilities.zustand_detected:
            layers.append("ZustandAdapter")
        if capabilities.three or capabilities.react_three_fiber:
            layers.append("ThreeAdapter")
        if capabilities.vanilla_dom:
            layers.append("VanillaDOMAdapter")
        if capabilities.local_storage:
            layers.append("LocalStorageSaveAdapter")
        if capabilities.hook_name:
            layers.append("TestHookAdapter")
        return layers

    def semantic_schema_for(
        self,
        repo: Path,
        profile: str,
        source_paths: list[Path],
        capabilities: CapabilityReport,
        storage_policy: StoragePolicy,
    ) -> SemanticSchemaReport:
        if profile == "Game2048Profile":
            return self._semantic_schema_2048(repo, source_paths)
        if profile == "SpaceRPGProfile":
            return self._semantic_schema_spacerpg(repo, source_paths, capabilities, storage_policy)
        if profile == "DatingSimProfile":
            return self._semantic_schema_love(repo, source_paths, capabilities, storage_policy)
        if profile == "OpenRAProfile":
            return self._semantic_schema_openra(repo, source_paths)
        if profile == "JupyterLabProfile":
            return self._semantic_schema_jupyterlab(repo, source_paths)
        if profile == "LibreOfficeProfile":
            return self._semantic_schema_libreoffice(repo, source_paths)
        if profile == "BlenderProfile":
            return self._semantic_schema_blender(repo, source_paths)
        if profile == "JynewProfile":
            return self._semantic_schema_jynew(repo, source_paths)
        if profile == "Pal3Profile":
            return self._semantic_schema_pal3(repo, source_paths)
        if profile == "LuantiProfile":
            return self._semantic_schema_luanti(repo, source_paths)
        return SemanticSchemaReport(
            surfaces=[],
            fields=[],
            limitations=[
                "Generic repositories are reported with raw capabilities only until a benchmark profile declares semantic surfaces.",
            ],
        )

    def _semantic_schema_2048(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        manager = repo / "js" / "game_manager.js"
        storage = repo / "js" / "local_storage_manager.js"
        hook = _first_path_containing(repo, source_paths, "__WARPTEST_2048__")
        actuator = repo / "js" / "html_actuator.js"
        surfaces = [
            SemanticSurface(
                kind="vanilla_dom_manager",
                name="GameManager",
                access=["dom-read", "keyboard-action", "localStorage-restore"],
                source_path=_relative(repo, manager),
                evidence=_evidence(repo, manager, "function GameManager", 0.95),
            ),
            SemanticSurface(
                kind="localStorage_key",
                name="gameState",
                access=["read", "write", "clear"],
                source_path=_relative(repo, storage),
                evidence=_evidence(repo, storage, "gameStateKey", 0.95),
            ),
            SemanticSurface(
                kind="test_hook",
                name="__WARPTEST_2048__",
                access=["read-state", "invoke-action"],
                source_path=_relative(repo, hook),
                evidence=_evidence(repo, hook, "__WARPTEST_2048__", 0.9),
            ),
        ]
        fields = [
            _field(repo, "board", "GameManager.serialize().grid", manager, "grid", ["test_hook", "localStorage"], ["localStorage"], 0.9),
            _field(repo, "score", "GameManager.score / HTMLActuator", manager, "score", ["test_hook", "localStorage", "dom"], ["localStorage"], 0.9),
            _field(repo, "bestScore", "LocalStorageManager.bestScoreKey", storage, "bestScoreKey", ["localStorage", "dom"], ["localStorage"], 0.88),
            _field(repo, "won", "GameManager.won", manager, "won", ["test_hook", "localStorage", "dom"], ["localStorage"], 0.9),
            _field(repo, "over", "GameManager.over", manager, "over", ["test_hook", "localStorage", "dom"], ["localStorage"], 0.9),
            _field(repo, "keepPlaying", "GameManager.keepPlaying", manager, "keepPlaying", ["test_hook", "localStorage"], ["localStorage"], 0.9),
            _field(repo, "tileDom", "HTMLActuator tile container", actuator, ".tile", ["dom"], [], 0.75),
        ]
        return SemanticSchemaReport(surfaces=surfaces, fields=fields)

    def _semantic_schema_spacerpg(
        self,
        repo: Path,
        source_paths: list[Path],
        capabilities: CapabilityReport,
        storage_policy: StoragePolicy,
    ) -> SemanticSchemaReport:
        store = repo / "src" / "state" / "gameStore.ts"
        store_types = repo / "src" / "state" / "gameStoreTypes.ts"
        hook = _first_path_containing(repo, source_paths, "__SPACERPG_E2E__")
        flight_scene = _first_path_containing(repo, source_paths, "FlightScene")
        save_source = _first_path_containing(repo, source_paths, "spacerpg-save-slot:")
        surfaces = [
            SemanticSurface(
                kind="zustand_store",
                name="useGameStore",
                access=["read-state", "patch-state", "invoke-store-action"],
                source_path=_relative(repo, store),
                evidence=_evidence(repo, store, "useGameStore", 0.95),
            ),
            SemanticSurface(
                kind="test_hook",
                name="__SPACERPG_E2E__",
                access=["dev-gated-read", "dev-gated-write", "scenario-apply"],
                source_path=_relative(repo, hook),
                evidence=_evidence(repo, hook, "__SPACERPG_E2E__", 0.95, "Exposed only behind the SpaceRPG E2E gate."),
            ),
            SemanticSurface(
                kind="localStorage_namespace",
                name="spacerpg-save-slot:",
                access=["read", "write", "clear-by-policy"],
                source_path=_relative(repo, save_source),
                evidence=_evidence(repo, save_source, "spacerpg-save-slot:", 0.85),
            ),
            SemanticSurface(
                kind="canvas_webgl_readiness",
                name="FlightScene canvas",
                access=["canvas-presence", "canvas-size", "render-heartbeat"],
                source_path=_relative(repo, flight_scene),
                evidence=_evidence(repo, flight_scene, "FlightScene", 0.75),
            ),
        ]
        fields = [
            _field(repo, "screen", "GameStore.screen", store_types, "screen", ["test_hook", "zustand"], ["test_hook"], 0.88),
            _field(repo, "currentSystemId", "GameStore.currentSystemId", store_types, "currentSystemId", ["test_hook", "zustand"], ["test_hook"], 0.92),
            _field(repo, "currentStationId", "GameStore.currentStationId", store_types, "currentStationId", ["test_hook", "zustand"], ["test_hook"], 0.92),
            _field(repo, "stationTab", "GameStore.stationTab", store_types, "stationTab", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "galaxyMapMode", "GameStore.galaxyMapMode", store_types, "galaxyMapMode", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "player.cargo", "GameStore.player.cargo", store_types, "cargo", ["test_hook", "zustand"], ["test_hook"], 0.85),
            _field(repo, "player.equipment", "GameStore.player.equipment", store_types, "equipment", ["test_hook", "zustand"], ["test_hook"], 0.85),
            _field(repo, "activeMissionIds", "GameStore.activeMissionIds", store_types, "activeMissionIds", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "completedMissionIds", "GameStore.completedMissionIds", store_types, "completedMissionIds", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "knownSystems", "GameStore.knownSystems", store_types, "knownSystems", ["test_hook", "zustand"], ["test_hook"], 0.88),
            _field(repo, "activeSaveSlotId", "GameStore.activeSaveSlotId", store_types, "activeSaveSlotId", ["test_hook", "zustand", "localStorage"], ["test_hook", "localStorage"], 0.85),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "SpaceRPG replay-from-start is intentionally excluded from deterministic baseline support.",
                "Canvas/WebGL readiness is coarse presence and heartbeat evidence, not scene graph introspection.",
                "Semantic fields are discovered from store types and test-gated source surfaces.",
            ],
        )

    def _semantic_schema_love(
        self,
        repo: Path,
        source_paths: list[Path],
        capabilities: CapabilityReport,
        storage_policy: StoragePolicy,
    ) -> SemanticSchemaReport:
        store = repo / "src" / "store" / "useGameStore.ts"
        engine_types = repo / "src" / "engine" / "types.ts"
        persistence = repo / "src" / "engine" / "PersistenceManager.ts"
        hook = _first_path_containing(repo, source_paths, "__WARPTEST_LOVE__")
        ai_mode = _first_path_containing(repo, source_paths, "warptest-love-ai-mode")
        surfaces = [
            SemanticSurface(
                kind="zustand_store",
                name="useGameStore",
                access=["read-state", "patch-state", "invoke-store-action", "save-load"],
                source_path=_relative(repo, store),
                evidence=_evidence(repo, store, "useGameStore", 0.95),
            ),
            SemanticSurface(
                kind="test_hook",
                name="__WARPTEST_LOVE__",
                access=["test-gated-read", "test-gated-write", "perform-action", "dialogue-control"],
                source_path=_relative(repo, hook),
                evidence=_evidence(repo, hook, "__WARPTEST_LOVE__", 0.95),
            ),
            SemanticSurface(
                kind="localStorage_key",
                name="love_emulator_save_v1",
                access=["read", "write", "clear-by-policy"],
                source_path=_relative(repo, persistence),
                evidence=_evidence(repo, persistence, "love_emulator_save_v1", 0.9),
            ),
            SemanticSurface(
                kind="mocked_ai_gate",
                name="warptest-love-ai-mode",
                access=["set-mock-mode", "block-real-api"],
                source_path=_relative(repo, ai_mode),
                evidence=_evidence(repo, ai_mode, "warptest-love-ai-mode", 0.9),
            ),
        ]
        fields = [
            _field(repo, "player.resources", "GameStore.player.resources", engine_types, "resources", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "player.relationships", "GameStore.player.relationships", engine_types, "relationships", ["test_hook", "zustand"], ["test_hook"], 0.9),
            _field(repo, "player.stats", "GameStore.player.stats", engine_types, "stats", ["test_hook", "zustand"], ["test_hook"], 0.85),
            _field(repo, "world.location", "GameStore.world.location", engine_types, "location", ["test_hook", "zustand", "dom"], ["test_hook"], 0.9),
            _field(repo, "world.week", "GameStore.world.week", engine_types, "week", ["test_hook", "zustand"], ["test_hook"], 0.82),
            _field(repo, "world.day", "GameStore.world.day", engine_types, "day", ["test_hook", "zustand"], ["test_hook"], 0.82),
            _field(repo, "world.timeSlot", "GameStore.world.timeSlot", engine_types, "timeSlot", ["test_hook", "zustand"], ["test_hook"], 0.82),
            _field(repo, "activeDialogue", "GameStore.activeDialogue", engine_types, "activeDialogue", ["test_hook", "zustand", "dom"], ["test_hook"], 0.9),
            _field(repo, "currentEvent", "GameStore.currentEvent", engine_types, "currentEvent", ["test_hook", "zustand", "dom"], ["test_hook"], 0.86),
            _field(repo, "world.globalFlags", "GameStore.world.globalFlags", engine_types, "globalFlags", ["test_hook", "zustand"], ["test_hook"], 0.86),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "AI dialogue content is mocked for repeatable smoke tests; semantic validation targets store state and deterministic UI effects.",
                "The schema records writable test-gated surfaces, not unrestricted runtime mutation.",
            ],
        )

    def _semantic_schema_openra(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        utility_command = _first_path_containing(repo, source_paths, "--warptest-checkpoint")
        cnc_mod = repo / "mods" / "cnc" / "mod.yaml"
        ra_mod = repo / "mods" / "ra" / "mod.yaml"
        gdi01 = repo / "mods" / "cnc" / "maps" / "gdi01" / "map.yaml"
        ra_drop_zone = repo / "mods" / "ra" / "maps" / "drop-zone" / "map.yaml"
        game_cs = repo / "OpenRA.Game" / "Game.cs"
        surfaces = [
            SemanticSurface(
                kind="native_utility_command",
                name="--warptest-checkpoint",
                access=["cli-validate-map", "json-report"],
                source_path=_relative(repo, utility_command),
                evidence=_evidence(repo, utility_command, "--warptest-checkpoint", 0.95, "Explicit utility command used only by WarpTest specs."),
            ),
            SemanticSurface(
                kind="openra_mod_manifest",
                name="mods/cnc/mod.yaml",
                access=["read-mod-id", "load-mod-data"],
                source_path=_relative(repo, cnc_mod),
                evidence=_evidence(repo, cnc_mod, "Manifest", 0.85),
            ),
            SemanticSurface(
                kind="openra_mod_manifest",
                name="mods/ra/mod.yaml",
                access=["read-mod-id", "load-mod-data"],
                source_path=_relative(repo, ra_mod),
                evidence=_evidence(repo, ra_mod, "Manifest", 0.85),
            ),
            SemanticSurface(
                kind="openra_map_yaml",
                name="mods/*/maps/*/map.yaml",
                access=["read-map-metadata", "read-player-references", "read-actors"],
                source_path=_relative(repo, gdi01),
                evidence=_evidence(repo, gdi01, "Players", 0.9),
            ),
            SemanticSurface(
                kind="openra_lobby_map_yaml",
                name="mods/ra/maps/drop-zone/map.yaml",
                access=["read-lobby-visibility", "read-spawns", "read-drop-zone-actors"],
                source_path=_relative(repo, ra_drop_zone),
                evidence=_evidence(repo, ra_drop_zone, "Visibility: Lobby", 0.9),
            ),
            SemanticSurface(
                kind="openra_gameplay_capture",
                name="Launch.WarptestScreenshotPath",
                access=["launch-map", "capture-live-gameplay-screenshot"],
                source_path=_relative(repo, game_cs),
                evidence=_evidence(repo, game_cs, "WarptestScreenshotPath", 0.9, "WarpTest launch harness captures a rendered in-game OpenRA screen."),
            ),
        ]
        fields = [
            _field(repo, "target.mod", "OpenRA mod id", cnc_mod, "RequiresMod", ["utility_json", "mod.yaml"], [], 0.9),
            _field(repo, "target.map", "OpenRA map directory id", gdi01, "map.yaml", ["utility_json", "map.yaml"], [], 0.9),
            _field(repo, "target.title", "OpenRA map title", gdi01, "Title", ["utility_json", "map.yaml"], [], 0.88),
            _field(repo, "target.visibility", "OpenRA map visibility", gdi01, "Visibility", ["utility_json", "map.yaml"], [], 0.85),
            _field(repo, "target.players", "PlayerReference entries", gdi01, "PlayerReference@", ["utility_json", "map.yaml"], [], 0.85),
            _field(repo, "target.actors", "Actors section entries", gdi01, "Actors", ["utility_json", "map.yaml"], [], 0.85),
            _field(repo, "target.objectives", "Lua AddPrimaryObjective/AddSecondaryObjective ids", repo / "mods" / "cnc" / "maps" / "gdi01" / "gdi01.lua", "AddPrimaryObjective", ["utility_json", "lua"], [], 0.82),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "OpenRAProfile validates source/runtime map checkpoints through OpenRA.Utility and visual parity through native clean-launch gameplay screenshots, not SDL menu automation.",
                "Validity is replay-reachable for clean-launch map initial state, with exact grader answer data kept outside public tasks.",
            ],
        )

    def _semantic_schema_jupyterlab(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        labapp = repo / "jupyterlab" / "labapp.py"
        galata_config = repo / "jupyterlab" / "galata" / "__init__.py"
        galata_routes = repo / "galata" / "src" / "galata.ts"
        contents_helper = repo / "galata" / "src" / "contents.ts"
        notebook_helper = repo / "galata" / "src" / "helpers" / "notebook.ts"
        filebrowser_helper = repo / "galata" / "src" / "helpers" / "filebrowser.ts"
        pytest_plugin = repo / "jupyterlab" / "pytest_plugin.py"
        if not contents_helper.exists():
            contents_helper = _first_path_containing(repo, source_paths, "Contents REST API")
        if not notebook_helper.exists():
            notebook_helper = _first_path_containing(repo, source_paths, "Notebook")
        if not filebrowser_helper.exists():
            filebrowser_helper = _first_path_containing(repo, source_paths, "filebrowser")

        surfaces = [
            SemanticSurface(
                kind="jupyter_lab_server",
                name="LabApp / Jupyter Server",
                access=["launch-isolated-server", "serve-lab-ui", "disable-token-for-test"],
                source_path=_relative(repo, labapp),
                evidence=_evidence(repo, labapp, "LabApp", 0.95, "JupyterLab server entry point and app configuration."),
            ),
            SemanticSurface(
                kind="jupyter_contents_api",
                name="/api/contents",
                access=["create-file", "read-file", "save-notebook", "save-text"],
                source_path=_relative(repo, galata_routes),
                evidence=_evidence(repo, galata_routes, "Routes.contents", 0.95, "Galata mirrors the public Jupyter Server Contents REST API."),
            ),
            SemanticSurface(
                kind="jupyter_sessions_api",
                name="/api/sessions",
                access=["start-kernel-session", "wait-for-session", "close-session"],
                source_path=_relative(repo, galata_routes),
                evidence=_evidence(repo, galata_routes, "Routes.sessions", 0.9, "Galata exposes the public Sessions API route used by notebook/kernel tests."),
            ),
            SemanticSurface(
                kind="jupyter_workspace_metadata",
                name="/api/workspaces",
                access=["set-layout", "verify-open-tabs", "restore-workspace"],
                source_path=_relative(repo, galata_routes),
                evidence=_evidence(repo, galata_routes, "Routes.workspaces", 0.9, "JupyterLab workspaces store UI layout and open document metadata."),
            ),
            SemanticSurface(
                kind="galata_browser_helpers",
                name="Galata-compatible browser UI helpers",
                access=["open-notebook", "open-text-editor", "wait-ui-ready"],
                source_path=_relative(repo, galata_config),
                evidence=_evidence(repo, galata_config, "configure_jupyter_server", 0.9, "Upstream test config disables token/password and isolates the root dir for browser tests."),
            ),
        ]
        fields = [
            _field(repo, "target.kind", "JupyterLab target kind", galata_routes, "contents", ["spec_yaml", "contents_api"], [], 0.95),
            _field(repo, "target.path", "Contents API path relative to server root", contents_helper, "ContentsHelper", ["contents_api"], ["contents_api"], 0.95),
            _field(repo, "target.content", "Text file body stored by Contents API", contents_helper, "save", ["contents_api"], ["contents_api"], 0.9),
            _field(repo, "target.cells", "Notebook cell models stored in ipynb JSON", notebook_helper, "cell", ["contents_api", "kernel_session"], ["contents_api"], 0.9),
            _field(repo, "workspace.name", "JupyterLab workspace id", galata_routes, "Routes.workspaces", ["workspace_api"], ["workspace_api"], 0.88),
            _field(repo, "workspace.paths", "Open document paths/tabs in workspace metadata", filebrowser_helper, "open", ["workspace_api", "galata_ui"], ["workspace_api"], 0.85),
            _field(repo, "session.kernel", "Jupyter kernel/session name", galata_routes, "Routes.sessions", ["sessions_api"], ["sessions_api"], 0.86),
            _field(repo, "server.root_dir", "Isolated Jupyter server root directory", galata_config, "JUPYTERLAB_GALATA_ROOT_DIR", ["server_config"], ["server_config"], 0.9),
            _field(repo, "workspaces.dir", "Isolated JupyterLab workspaces directory", pytest_plugin, "workspaces_dir", ["server_config"], ["server_config"], 0.85),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "JupyterLab V1 uses upstream public Jupyter Server Contents, Sessions, kernel, workspace, and Galata-compatible browser surfaces; no private hooks are added to the nested checkout.",
                "Runtime checkpoints run inside a temporary root/config/data/runtime/workspaces tree under the WarpTest report directory and do not write back to games/jupyterlab.",
                "Visual UI oracles are optional and require a locally installable/built JupyterLab UI plus browser dependencies.",
            ],
        )

    def _semantic_schema_libreoffice(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        desktop_app = repo / "desktop" / "source" / "app" / "app.cxx"
        pyuno_helper = repo / "pyuno" / "source" / "officehelper.py"
        pyuno_module = repo / "pyuno" / "source" / "module" / "uno.py"
        uitest_connection = repo / "uitest" / "libreoffice" / "connection.py"
        writer_module = repo / "sw" / "README.md"
        calc_module = repo / "sc" / "README.md"
        impress_module = repo / "sd" / "README.md"
        lok_header = repo / "include" / "LibreOfficeKit" / "LibreOfficeKit.h"
        filter_config = repo / "filter" / "source" / "config" / "fragments" / "filters"
        surfaces = [
            SemanticSurface(
                kind="libreoffice_desktop_process",
                name="soffice",
                access=["launch-headless-office", "use-isolated-user-profile", "connect-uno-pipe"],
                source_path=_relative(repo, desktop_app),
                evidence=_evidence(repo, desktop_app, "Desktop::Main", 0.9, "LibreOffice desktop process entry point used by soffice."),
            ),
            SemanticSurface(
                kind="uno_component_context",
                name="StarOffice.ComponentContext",
                access=["bootstrap-uno", "load-component", "dispatch-document-model"],
                source_path=_relative(repo, pyuno_helper),
                evidence=_evidence(repo, pyuno_helper, "officehelper.bootstrap", 0.95, "Public pyuno helper launches and connects to soffice over UNO."),
            ),
            SemanticSurface(
                kind="pyuno_bridge",
                name="uno.py / pyuno",
                access=["import-uno", "system-path-to-file-url", "property-values"],
                source_path=_relative(repo, pyuno_module),
                evidence=_evidence(repo, pyuno_module, "uno.py", 0.9),
            ),
            SemanticSurface(
                kind="libreoffice_uitest_connection",
                name="OfficeConnection",
                access=["headless-soffice-with-userdir", "connect-pipe", "terminate-desktop"],
                source_path=_relative(repo, uitest_connection),
                evidence=_evidence(repo, uitest_connection, "--accept=", 0.9, "Upstream test harness uses the same isolated user profile and UNO pipe shape."),
            ),
            SemanticSurface(
                kind="writer_document_model",
                name="Writer / sw",
                access=["create-text-document", "replace-text", "paragraph-style", "text-table", "bookmark", "save-odt", "export-pdf"],
                source_path=_relative(repo, writer_module),
                evidence=_evidence(repo, writer_module, "Writer", 0.85),
            ),
            SemanticSurface(
                kind="calc_document_model",
                name="Calc / sc",
                access=["create-spreadsheet", "set-cell", "set-formula", "recalculate", "named-range", "chart", "save-ods"],
                source_path=_relative(repo, calc_module),
                evidence=_evidence(repo, calc_module, "Calc", 0.85),
            ),
            SemanticSurface(
                kind="impress_presentation_model",
                name="Impress / sd",
                access=["create-presentation", "set-slide-title", "set-slide-bullets", "add-shape", "save-odp", "export-pdf"],
                source_path=_relative(repo, impress_module),
                evidence=_evidence(repo, impress_module, "Impress", 0.85),
            ),
            SemanticSurface(
                kind="libreofficekit_public_api",
                name="LibreOfficeKit",
                access=["document-rendering-api", "public-office-embedding-boundary"],
                source_path=_relative(repo, lok_header),
                evidence=_evidence(repo, lok_header, "LibreOfficeKit", 0.85),
            ),
        ]
        fields = [
            _field(repo, "target.kind", "LibreOffice spec target kind", pyuno_helper, "loadComponentFromURL", ["spec_yaml", "uno"], [], 0.9),
            _field(repo, "target.path", "ODF artifact path under report_dir/libreoffice-runtime/artifacts", uitest_connection, "UserInstallation", ["odf_artifact"], ["odf_artifact"], 0.9),
            _field(repo, "writer.text", "XText document body", writer_module, "Writer", ["uno", "odf"], ["uno"], 0.88),
            _field(repo, "writer.paragraph_style", "Paragraph ParaStyleName", writer_module, "Writer", ["uno", "odf"], ["uno"], 0.86),
            _field(repo, "writer.table", "Writer TextTables collection and cell contents", writer_module, "Writer", ["uno", "odf"], ["uno"], 0.84),
            _field(repo, "writer.bookmark", "Writer Bookmarks collection or persisted ODF marker fallback", writer_module, "Writer", ["uno", "odf"], ["uno"], 0.82),
            _field(repo, "calc.sheet", "Spreadsheet Sheets collection", calc_module, "Calc", ["uno", "odf"], ["uno"], 0.88),
            _field(repo, "calc.cell", "Cell value/string", calc_module, "Calc", ["uno", "odf"], ["uno"], 0.88),
            _field(repo, "calc.formula", "Cell Formula and calculateAll", calc_module, "Calc", ["uno", "odf"], ["uno"], 0.88),
            _field(repo, "calc.named_range", "NamedRanges collection and ODF-persisted range marker", calc_module, "Calc", ["uno", "odf"], ["uno"], 0.84),
            _field(repo, "calc.chart", "Spreadsheet TableCharts collection and exported document state", calc_module, "Calc", ["uno", "odf"], ["uno"], 0.82),
            _field(repo, "impress.slide", "DrawPages slide collection and text shapes", impress_module, "Impress", ["uno", "odf"], ["uno"], 0.86),
            _field(repo, "impress.shape_text", "Impress TextShape string content", impress_module, "Impress", ["uno", "odf"], ["uno"], 0.84),
            _field(repo, "pdf.export", "writer_pdf_Export / calc_pdf_Export / impress_pdf_Export filter output", filter_config, "pdf_Export", ["file_artifact"], ["uno"], 0.82),
            _field(repo, "visual.before_after", "Desktop screenshot pair captured around UNO checkpoint actions", desktop_app, "Desktop::Main", ["desktop_screenshot"], ["desktop_screenshot"], 0.75),
            _field(repo, "runtime.user_profile", "Isolated -env:UserInstallation profile", uitest_connection, "UserInstallation", ["runtime_config"], ["runtime_config"], 0.9),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "LibreOffice V2 covers Writer, Calc, and Impress document semantics; Base/Draw are out of scope.",
                "WarpTest does not modify games/LibreOffice or add private hooks; it uses public UNO/ODF/PDF surfaces.",
                "Visual before/after oracles require a desktop-capable soffice process and platform screenshot permission.",
                "Runtime artifacts and the LibreOffice user profile are isolated under the WarpTest report directory.",
                "The actual soffice binary is configurable and recorded in run metrics; it may differ from the source checkout if WARPTEST_LIBREOFFICE_SOFFICE is not pointed at an instdir build.",
            ],
        )

    def _semantic_schema_blender(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        creator_args = repo / "source" / "creator" / "creator_args.cc"
        bpy_module = repo / "source" / "creator" / "CMakeLists.txt"
        docs_script = repo / "tools" / "utils_doc" / "code_layout_diagram.py"
        if not docs_script.exists():
            docs_script = _first_path_containing(repo, source_paths, "bpy.ops.wm.save_as_mainfile")
        python_api = repo / "scripts" / "startup" / "bl_operators" / "object.py"
        if not python_api.exists():
            python_api = _first_path_containing(repo, source_paths, "bpy.ops.object")
        surfaces = [
            SemanticSurface(
                kind="blender_gui_app",
                name="Blender.app / creator arguments",
                access=["launch-visible-gui", "factory-startup", "python-driver"],
                source_path=_relative(repo, creator_args),
                evidence=_evidence(repo, creator_args, "--python", 0.95, "Creator arguments expose visible GUI launch plus Python script execution."),
            ),
            SemanticSurface(
                kind="bpy_python_api",
                name="bpy scene data API",
                access=["create-object", "assign-material", "insert-keyframe", "save-blend"],
                source_path=_relative(repo, docs_script),
                evidence=_evidence(repo, docs_script, "import bpy", 0.9, "Repo tools demonstrate scene creation and saving through bpy."),
            ),
            SemanticSurface(
                kind="blend_file_persistence",
                name=".blend mainfile",
                access=["save-as-mainfile", "open-mainfile", "validate-persisted-scene"],
                source_path=_relative(repo, docs_script),
                evidence=_evidence(repo, docs_script, "save_as_mainfile", 0.9),
            ),
            SemanticSurface(
                kind="modifier_stack",
                name="Object.modifiers",
                access=["add-modifier", "apply-modifier", "validate-mesh-data"],
                source_path=_relative(repo, python_api),
                evidence=_evidence(repo, python_api, "modifier", 0.8),
            ),
            SemanticSurface(
                kind="animation_data",
                name="Object animation_data/action/fcurves",
                access=["insert-keyframe", "read-fcurves", "validate-frames"],
                source_path=_relative(repo, docs_script),
                evidence=_evidence(repo, docs_script, "bpy.context.scene", 0.8),
            ),
        ]
        fields = [
            _field(repo, "scene.objects", "bpy.context.scene.objects", docs_script, "scene.objects", ["bpy"], ["bpy"], 0.9),
            _field(repo, "object.name", "bpy.data.objects[name]", docs_script, "bpy.data.objects", ["bpy"], ["bpy"], 0.9),
            _field(repo, "object.location", "Object.location", docs_script, "location", ["bpy"], ["bpy"], 0.9),
            _field(repo, "object.material.diffuse_color", "Material.diffuse_color", docs_script, "diffuse_color", ["bpy"], ["bpy"], 0.85),
            _field(repo, "object.modifiers", "Object.modifiers", python_api, "modifier", ["bpy"], ["bpy"], 0.85),
            _field(repo, "animation.keyframes", "Object.animation_data.action.fcurves", docs_script, "keyframe", ["bpy"], ["bpy"], 0.8),
            _field(repo, "blend.filepath", "bpy.ops.wm.save_as_mainfile/open_mainfile", docs_script, "save_as_mainfile", ["bpy"], ["bpy"], 0.9),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "Blender V1 keeps the GUI visible but drives checkpoints through bpy/operator APIs, not pixel click replay.",
                "The adapter writes temporary .blend files under the WarpTest report directory and does not modify games/blender.",
                "Visual screenshot oracles are deferred; V1 validates scene graph, persistence, modifiers, and animation semantics.",
            ],
        )

    def _semantic_schema_jynew(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        game_save_dir = repo / "jyx2" / "Assets" / "Scripts" / "GameSave"
        runtime_data = game_save_dir / "GameRuntimeData.cs" if game_save_dir.is_dir() else None
        role_instance = game_save_dir / "RoleInstance.cs" if game_save_dir.is_dir() else None
        warptest_cmd = repo / "jyx2" / "Assets" / "Scripts" / "GameCore" / "WarptestCheckpointCommand.cs"
        if not warptest_cmd.exists():
            warptest_cmd = _first_path_containing(repo, source_paths, "WarptestCheckpoint")
        save_panel = repo / "jyx2" / "Assets" / "Scripts" / "GameSave" / "SavePanel.cs"
        if not save_panel.exists():
            save_panel = _first_path_containing(repo, source_paths, "SavePanel")
        surfaces = [
            SemanticSurface(
                kind="unity_batch_command",
                name="WarptestCheckpoint.Run",
                access=["cli-synthesize-state", "cli-validate-checkpoint", "json-report"],
                source_path=_relative(repo, warptest_cmd),
                evidence=_evidence(repo, warptest_cmd, "WarptestCheckpoint", 0.95, "Unity batch-mode WarpTest entry point."),
            ),
            SemanticSurface(
                kind="es3_save_system",
                name="GameRuntimeData",
                access=["load-archive", "save-archive", "create-new"],
                source_path=_relative(repo, runtime_data),
                evidence=_evidence(repo, runtime_data, "GameRuntimeData", 0.95),
            ),
            SemanticSurface(
                kind="role_instance",
                name="RoleInstance",
                access=["read-attrs", "level-up", "learn-skill", "use-item"],
                source_path=_relative(repo, role_instance),
                evidence=_evidence(repo, role_instance, "RoleInstance", 0.95),
            ),
            SemanticSurface(
                kind="save_panel",
                name="SavePanel",
                access=["save-slot-ui", "load-slot-ui"],
                source_path=_relative(repo, save_panel),
                evidence=_evidence(repo, save_panel, "SavePanel", 0.85),
            ),
        ]
        fields = [
            _field(repo, "player.Level", "RoleInstance.Level", role_instance, "Level", ["utility_json"], [], 0.95),
            _field(repo, "player.Hp", "RoleInstance.Hp", role_instance, "Hp", ["utility_json"], [], 0.95),
            _field(repo, "player.MaxHp", "RoleInstance.MaxHp", role_instance, "MaxHp", ["utility_json"], [], 0.95),
            _field(repo, "player.Attack", "RoleInstance.Attack", role_instance, "Attack", ["utility_json"], [], 0.9),
            _field(repo, "player.Defence", "RoleInstance.Defence", role_instance, "Defence", ["utility_json"], [], 0.9),
            _field(repo, "team.count", "GameRuntimeData.GetTeamMembersCount", runtime_data, "TeamId", ["utility_json"], [], 0.9),
            _field(repo, "money", "GameRuntimeData.GetMoney via MONEY_ID", runtime_data, "GetMoney", ["utility_json"], [], 0.9),
            _field(repo, "items", "GameRuntimeData.Items dict", runtime_data, "Items", ["utility_json"], [], 0.85),
            _field(repo, "skills", "RoleInstance.Wugongs list", role_instance, "Wugongs", ["utility_json"], [], 0.85),
            _field(repo, "submap.id", "SubMapSaveData.MapId", runtime_data, "SubMapData", ["utility_json"], [], 0.85),
            _field(repo, "key_values", "GameRuntimeData.KeyValues", runtime_data, "KeyValues", ["utility_json"], [], 0.85),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "JynewProfile validates state through Unity batch-mode checkpoint command, not browser runtime.",
                "ES3 binary save format is opaque; state synthesis and validation go through GameRuntimeData APIs.",
                "Visual oracle requires Unity rendering which may not be available in headless CI.",
            ],
        )

    def _semantic_schema_pal3(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        warptest_cmd = _first_path_containing(repo, source_paths, "WarptestCheckpoint")
        pal3_cs = _first_path_containing(repo, source_paths, "public void Execute(ICommand")
        save_manager = _first_path_containing(repo, source_paths, "ConvertCurrentGameStateToCommands")
        script_constants = _first_path_containing(repo, source_paths, "MainStoryVariableId")
        team_manager = _first_path_containing(repo, source_paths, "GetActorsInTeam")
        inventory_manager = _first_path_containing(repo, source_paths, "GetTotalMoney")
        scene_info = _first_path_containing(repo, source_paths, "ScnSceneInfo")
        world_map_manager = _first_path_containing(repo, source_paths, "GetRegionEnablementInfo")
        favor_manager = _first_path_containing(repo, source_paths, "GetFavorByActor")
        surfaces = [
            SemanticSurface(
                kind="unity_batch_command",
                name="WarptestCheckpoint.Run",
                access=["cli-synthesize-state", "cli-load-save", "cli-validate-checkpoint", "json-report"],
                source_path=_relative(repo, warptest_cmd),
                evidence=_evidence(repo, warptest_cmd, "WarptestCheckpoint", 0.95, "Unity batch-mode WarpTest entry point."),
            ),
            SemanticSurface(
                kind="command_pattern",
                name="Pal3.Execute(ICommand)",
                access=["execute-command", "console-command"],
                source_path=_relative(repo, pal3_cs),
                evidence=_evidence(repo, pal3_cs, "Execute(ICommand", 0.92, "All mutable state flows through ICommand dispatch."),
            ),
            SemanticSurface(
                kind="text_save_system",
                name="SaveManager",
                access=["save-slot", "load-slot", "commands-from-state"],
                source_path=_relative(repo, save_manager),
                evidence=_evidence(repo, save_manager, "ConvertCurrentGameStateToCommands", 0.93, "Saves are newline-separated console-command text files."),
            ),
            SemanticSurface(
                kind="service_locator_managers",
                name="ServiceLocator",
                access=["read-managers"],
                source_path=_relative(repo, team_manager),
                evidence=_evidence(repo, team_manager, "GetActorsInTeam", 0.9, "Runtime state lives in ServiceLocator-registered managers."),
            ),
        ]
        fields = [
            _field(repo, "story_var.32768", "UserVariableManager main story variable (ScriptConstants.MainStoryVariableId=0x8000)", script_constants, "MainStoryVariableId", ["utility_json"], ["ScriptVarSetValueCommand"], 0.95),
            _field(repo, "scene.city", "ScnSceneInfo.CityName via SceneManager.GetCurrentScene", scene_info, "CityName", ["utility_json"], ["SceneLoadCommand"], 0.92),
            _field(repo, "scene.name", "ScnSceneInfo.SceneName via SceneManager.GetCurrentScene", scene_info, "SceneName", ["utility_json"], ["SceneLoadCommand"], 0.92),
            _field(repo, "team.count", "TeamManager.GetActorsInTeam().Count", team_manager, "GetActorsInTeam", ["utility_json"], ["TeamAddOrRemoveActorCommand"], 0.9),
            _field(repo, "money", "InventoryManager.GetTotalMoney", inventory_manager, "GetTotalMoney", ["utility_json"], ["InventoryAddMoneyCommand"], 0.9),
            _field(repo, "items", "InventoryManager.GetAllItems", inventory_manager, "GetAllItems", ["utility_json"], ["InventoryAddItemCommand"], 0.85),
            _field(repo, "region", "WorldMapManager.GetRegionEnablementInfo", world_map_manager, "GetRegionEnablementInfo", ["utility_json"], ["WorldMapEnableRegionCommand"], 0.85),
            _field(repo, "favor", "FavorManager.GetFavorByActor", favor_manager, "GetFavorByActor", ["utility_json"], ["FavorAddCommand"], 0.85),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "Pal3Profile validates state through Unity batch-mode checkpoint command in play mode, not browser runtime.",
                "Checkpoints are synthesized/restored via the game's own console commands (the same text format used by SaveManager).",
                "Running the game requires the original PAL3 data (CPK); without it the harness reports target.game_ready failure.",
                "Visual oracle requires Unity rendering which may not be available in headless CI.",
            ],
        )

    def _semantic_schema_luanti(self, repo: Path, source_paths: list[Path]) -> SemanticSchemaReport:
        lua_api = repo / "doc" / "lua_api.md"
        worldmod = _first_path_containing(repo, source_paths, "warptest_request_path")
        basenodes = repo / "games" / "devtest" / "mods" / "basenodes" / "init.lua"
        chest = _first_path_containing(repo, source_paths, "chest") or (repo / "games" / "devtest" / "mods" / "chest" / "chest.lua")
        game_cpp = repo / "src" / "client" / "game.cpp"
        env_api = _first_path_containing(repo, source_paths, "register_on_joinplayer") or lua_api
        surfaces = [
            SemanticSurface(
                kind="lua_worldmod_command",
                name="warptest",
                access=["read-request", "synthesize-state", "validate", "write-json-report"],
                source_path=_relative(repo, worldmod),
                evidence=_evidence(repo, worldmod, "warptest_request_path", 0.95, "Test-gated WarpTest worldmod driving the checkpoint."),
            ),
            SemanticSurface(
                kind="voxel_map",
                name="core.set_node/get_node",
                access=["read-node", "write-node", "find-nodes-in-area"],
                source_path=_relative(repo, lua_api),
                evidence=_evidence(repo, lua_api, "set_node", 0.95),
            ),
            SemanticSurface(
                kind="registered_nodes",
                name="basenodes",
                access=["read-node-definitions"],
                source_path=_relative(repo, basenodes),
                evidence=_evidence(repo, basenodes, "basenodes:stone", 0.9, "devtest base node ids used as voxel checkpoint content."),
            ),
            SemanticSurface(
                kind="node_meta_inventory",
                name="core.get_meta():get_inventory()",
                access=["read-list", "write-list", "add-item"],
                source_path=_relative(repo, chest),
                evidence=_evidence(repo, chest, "get_inventory", 0.85, "Chest node metadata inventory (list 'main')."),
            ),
            SemanticSurface(
                kind="player_object",
                name="core.get_player_by_name",
                access=["read-pos", "set-pos", "read-hp", "set-hp", "player-inventory"],
                source_path=_relative(repo, env_api),
                evidence=_evidence(repo, env_api, "register_on_joinplayer", 0.85, "Online player object available in single-player --go session."),
            ),
            SemanticSurface(
                kind="mod_storage",
                name="core.get_mod_storage",
                access=["get-string", "set-string"],
                source_path=_relative(repo, lua_api),
                evidence=_evidence(repo, lua_api, "get_mod_storage", 0.85),
            ),
            SemanticSurface(
                kind="engine_screenshot_capture",
                name="warptest_screenshot_path",
                access=["render-frame", "write-png", "exit-after-capture"],
                source_path=_relative(repo, game_cpp),
                evidence=_evidence(repo, game_cpp, "makeScreenshot", 0.85, "Engine render-loop screenshot hook for the visual oracle."),
            ),
        ]
        fields = [
            _field(repo, "node@pos", "core.get_node(pos).name", lua_api, "get_node", ["utility_json", "lua"], ["lua"], 0.92),
            _field(repo, "node_count", "core.find_nodes_in_area", lua_api, "find_nodes_in_area", ["utility_json", "lua"], [], 0.88),
            _field(repo, "player.pos", "ObjectRef:get_pos/set_pos", env_api, "set_pos", ["utility_json", "lua"], ["lua"], 0.88),
            _field(repo, "player.hp", "ObjectRef:get_hp/set_hp", env_api, "set_hp", ["utility_json", "lua"], ["lua"], 0.88),
            _field(repo, "player.inventory", "Player InvRef list 'main'", env_api, "get_inventory", ["utility_json", "lua"], ["lua"], 0.85),
            _field(repo, "meta_inventory", "NodeMetaRef inventory list", chest, "get_inventory", ["utility_json", "lua"], ["lua"], 0.82),
            _field(repo, "mod_storage", "StorageRef get_string/set_string", lua_api, "get_mod_storage", ["utility_json", "lua"], ["lua"], 0.85),
            _field(repo, "gametime", "core.get_gametime", lua_api, "get_gametime", ["utility_json", "lua"], [], 0.85),
        ]
        return SemanticSchemaReport(
            surfaces=surfaces,
            fields=fields,
            limitations=[
                "LuantiProfile drives state through a test-gated Lua worldmod inside an embedded single-player session, not a dedicated headless server, so online-player state is mutable.",
                "Online player, voxel world, node-meta inventories, and mod storage are persisted through the engine's own world format (map.sqlite/players.sqlite/mod_storage.sqlite); save-restorable.",
                "Visual oracle requires a rendered client (xvfb + software GL on headless hosts); a non-rendering server cannot produce screenshots.",
            ],
        )
