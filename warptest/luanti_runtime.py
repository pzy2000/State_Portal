from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .c1_task_deadline import (
    bounded_task_timeout_s,
    raise_task_timeout_if_expired,
)
from .runtime import json_dumps
from .jynew_runtime import (
    _NoOpPage,
    _live_fallback_image,
    _process_output,
    _screenshot_is_informative,
)


# The WarpTest worldmod is installed as a global mod in the engine checkout and
# enabled per-world via `load_mod_warptest = true` (RUN_IN_PLACE adds mods/).
LUANTI_WORLDMOD_RELATIVE_DIR = Path("mods/warptest")
LUANTI_MACOS_APP_RELATIVE_PATH = Path("build-app/out/luanti.app")
LUANTI_GAME_CPP_RELATIVE_PATH = Path("src/client/game.cpp")
LUANTI_HIDDEN_WINDOW_SOURCE_RELATIVE_PATH = Path("irr/src/CIrrDeviceSDL.cpp")
LUANTI_BUILD_PROVENANCE_SCHEMA_VERSION = 2

_INSTRUMENTATION_DIR = Path(__file__).resolve().parent / "instrumentation" / "luanti"
LUANTI_MOD_TEMPLATE_DIR = _INSTRUMENTATION_DIR / "warptest"
LUANTI_CAPTURE_INC = _INSTRUMENTATION_DIR / "engine" / "warptest_capture.inc"
LUANTI_CALL_INC = _INSTRUMENTATION_DIR / "engine" / "warptest_call.inc"

_CAPTURE_BEGIN = "// WARPTEST-CAPTURE-BEGIN"
_CAPTURE_END = "// WARPTEST-CAPTURE-END"
_CALL_BEGIN = "// WARPTEST-CALL-BEGIN"
_CALL_END = "// WARPTEST-CALL-END"
# Stable anchors in upstream src/client/game.cpp.
_CAPTURE_ANCHOR = "void Game::run()"
_CALL_ANCHOR = "updateProfilerGraphs(&graph);"


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_identity(repo: Path) -> dict[str, str | None]:
    trusted_git_raw = os.environ.get("WARPTEST_LUANTI_TRUSTED_GIT_DIR")
    trusted_git = (
        Path(trusted_git_raw).expanduser().resolve()
        if trusted_git_raw
        else None
    )

    def run(*args: str) -> str | None:
        try:
            command = (
                [
                    "git",
                    f"--git-dir={trusted_git}",
                    f"--work-tree={repo}",
                    *args,
                ]
                if trusted_git is not None and trusted_git.is_dir()
                else ["git", "-C", str(repo), *args]
            )
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=False,
                timeout=15,
            )
        except Exception:
            return None
        return hashlib.sha256(completed.stdout).hexdigest()

    return {
        "head_sha256": run("rev-parse", "HEAD"),
        "tracked_source_diff_sha256": run(
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            "src",
            "irr",
            "CMakeLists.txt",
            "cmake",
        ),
    }


def luanti_client_source_manifest(repo: str | Path) -> dict[str, Any]:
    """Hash every source/template input that identifies the C1 client runtime."""

    resolved = Path(repo).expanduser().resolve()
    inputs = {
        "game_cpp": (
            str(LUANTI_GAME_CPP_RELATIVE_PATH),
            resolved / LUANTI_GAME_CPP_RELATIVE_PATH,
        ),
        "hidden_window_source": (
            str(LUANTI_HIDDEN_WINDOW_SOURCE_RELATIVE_PATH),
            resolved / LUANTI_HIDDEN_WINDOW_SOURCE_RELATIVE_PATH,
        ),
        "capture_template": (
            "warptest/instrumentation/luanti/engine/warptest_capture.inc",
            LUANTI_CAPTURE_INC,
        ),
        "call_template": (
            "warptest/instrumentation/luanti/engine/warptest_call.inc",
            LUANTI_CALL_INC,
        ),
    }
    return {
        "schema_version": LUANTI_BUILD_PROVENANCE_SCHEMA_VERSION,
        "git": _git_identity(resolved),
        "inputs": {
            name: {"path": logical_path, "sha256": _file_sha256(path)}
            for name, (logical_path, path) in inputs.items()
        },
    }


def luanti_client_source_identity(repo: str | Path) -> str:
    manifest = luanti_client_source_manifest(repo)
    encoded = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "luanti-client-v1:" + hashlib.sha256(encoded).hexdigest()


def sync_luanti_builtin_assets(repo: str | Path) -> dict[str, Any]:
    """Overlay versioned Lua builtin assets into a disposable macOS app bundle.

    A Lua-only inverse patch does not change the client executable, but macOS
    app bundles embed a copy of ``builtin/``.  Lazarus trial clones must run the
    versioned copy rather than the fixed resources inherited with the binary.
    Callers deliberately invoke this only on a disposable clone.
    """

    root = Path(repo).expanduser().resolve()
    source = root / "builtin"
    destination = (
        root / "build-app/out/luanti.app/Contents/Resources/builtin"
    )
    if not source.is_dir() or not destination.is_dir():
        return {"status": "not_available", "files": 0}
    files = [path for path in source.rglob("*") if path.is_file()]
    for path in files:
        relative = path.relative_to(source)
        target = (destination / relative).resolve()
        if destination != target and destination not in target.parents:
            raise RuntimeError(f"Luanti builtin asset path escapes app bundle: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return {"status": "synced", "files": len(files)}


def _luanti_build_provenance_path(binary: Path) -> Path:
    return binary.with_name(binary.name + ".warptest-build.json")


def _write_luanti_client_build_provenance(repo: Path, binary: Path) -> dict[str, Any]:
    manifest = luanti_client_source_manifest(repo)
    payload = {
        "schema_version": LUANTI_BUILD_PROVENANCE_SCHEMA_VERSION,
        "source_identity": luanti_client_source_identity(repo),
        "source_manifest": manifest,
        # Kept only as human-readable provenance. Validation is intentionally
        # relocation-safe because formal C1 copies this sidecar into a fresh CoW
        # checkout for every task.
        "binary_path": str(binary.resolve()),
        "binary_name": binary.name,
        "binary_sha256": _file_sha256(binary),
        "written_unix_ns": time.time_ns(),
    }
    path = _luanti_build_provenance_path(binary)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return payload


def _read_luanti_client_build_provenance(
    repo: Path, binary: Path
) -> dict[str, Any] | None:
    path = _luanti_build_provenance_path(binary)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != LUANTI_BUILD_PROVENANCE_SCHEMA_VERSION:
        return None
    if payload.get("source_identity") != luanti_client_source_identity(repo):
        return None
    if payload.get("binary_name") != binary.name:
        return None
    if payload.get("binary_sha256") != _file_sha256(binary):
        return None
    return payload


def _luanti_build_dir(repo: Path) -> Path:
    override = os.environ.get("WARPTEST_LUANTI_BUILD_DIR")
    if override:
        return Path(override).expanduser().resolve()
    repo_key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "warptest-luanti-builds" / repo_key


# ---------------------------------------------------------------------------
# Instrumentation install / refresh
# ---------------------------------------------------------------------------


def ensure_luanti_instrumentation(repo: Path) -> dict[str, object]:
    """Install/refresh the bundled Lua worldmod and the C++ screenshot hook.

    Mirrors ensure_jynew_instrumentation/ensure_pal3_instrumentation: additive,
    idempotent, and refuses to clobber non-WarpTest files. The Lua mod drives the
    semantic checkpoint; the C++ hook (config-gated) captures the visual oracle
    frame, analogous to OpenRA's WarptestScreenshot launch harness.
    """

    repo = Path(repo).expanduser().resolve()
    actions: list[str] = []
    mod_action = _install_lua_worldmod(repo)
    actions.append(mod_action)
    cpp_action = _install_cpp_screenshot_hook(repo)
    actions.append(cpp_action)
    input_action = _install_cpp_background_input_integration(repo)
    actions.append(input_action)
    installed = any(action.startswith("installed") or action.startswith("refreshed") for action in actions)
    return {
        "installed": installed,
        "reason": "; ".join(actions),
        "mod_path": str(repo / LUANTI_WORLDMOD_RELATIVE_DIR),
        "game_cpp_path": str(repo / LUANTI_GAME_CPP_RELATIVE_PATH),
    }


def _install_lua_worldmod(repo: Path) -> str:
    target_dir = repo / LUANTI_WORLDMOD_RELATIVE_DIR
    conf_src = LUANTI_MOD_TEMPLATE_DIR / "mod.conf"
    init_src = LUANTI_MOD_TEMPLATE_DIR / "init.lua"
    if not conf_src.exists() or not init_src.exists():
        raise FileNotFoundError(f"Bundled luanti instrumentation template is missing under {LUANTI_MOD_TEMPLATE_DIR}")

    init_target = target_dir / "init.lua"
    if init_target.exists():
        existing = init_target.read_text(encoding="utf-8", errors="ignore")
        if "warptest" not in existing.lower():
            raise RuntimeError(f"Refusing to overwrite non-WarpTest luanti mod file: {init_target}")
        if existing == init_src.read_text(encoding="utf-8"):
            return "existing luanti worldmod present"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "mod.conf").write_text(conf_src.read_text(encoding="utf-8"), encoding="utf-8")
        init_target.write_text(init_src.read_text(encoding="utf-8"), encoding="utf-8")
        return "refreshed luanti worldmod"

    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "mod.conf").write_text(conf_src.read_text(encoding="utf-8"), encoding="utf-8")
    init_target.write_text(init_src.read_text(encoding="utf-8"), encoding="utf-8")
    return "installed luanti worldmod"


def _install_cpp_screenshot_hook(repo: Path) -> str:
    game_cpp = repo / LUANTI_GAME_CPP_RELATIVE_PATH
    if not game_cpp.exists():
        return "skipped C++ screenshot hook (game.cpp not found)"
    capture_block = LUANTI_CAPTURE_INC.read_text(encoding="utf-8").rstrip("\n") + "\n"
    call_block = LUANTI_CALL_INC.read_text(encoding="utf-8").rstrip("\n") + "\n"

    text = game_cpp.read_text(encoding="utf-8")
    original = text
    refreshed = False

    if _CAPTURE_BEGIN in text:
        text, changed = _replace_between(text, _CAPTURE_BEGIN, _CAPTURE_END, capture_block.rstrip("\n"))
        refreshed = refreshed or changed
    else:
        anchor = text.find(_CAPTURE_ANCHOR)
        if anchor == -1:
            return "skipped C++ screenshot hook (run() anchor not found)"
        text = text[:anchor] + capture_block + "\n" + text[anchor:]

    if _CALL_BEGIN in text:
        text, changed = _replace_between(text, _CALL_BEGIN, _CALL_END, call_block.rstrip("\n"))
        refreshed = refreshed or changed
    else:
        idx = text.find(_CALL_ANCHOR)
        if idx == -1:
            return "skipped C++ screenshot call (loop anchor not found)"
        line_end = text.find("\n", idx)
        if line_end == -1:
            line_end = len(text)
        text = text[: line_end + 1] + call_block + text[line_end + 1 :]

    if text == original:
        return "existing C++ screenshot hook present"
    game_cpp.write_text(text, encoding="utf-8")
    return "refreshed C++ screenshot hook" if (_CAPTURE_BEGIN in original) else "installed C++ screenshot hook"


def _install_cpp_background_input_integration(repo: Path) -> str:
    """Keep hidden-window policy input alive through the consuming game frame."""

    game_cpp = repo / LUANTI_GAME_CPP_RELATIVE_PATH
    if not game_cpp.exists():
        return "skipped C++ background input integration (game.cpp not found)"
    text = game_cpp.read_text(encoding="utf-8")
    original = text
    focus_upstream = (
        "\t// Reset input if window not active or some menu is active\n"
        "\tif (!device->isWindowActive() || isMenuActive() || guienv->hasFocus(gui_chat_console.get())) {"
    )
    focus_instrumented = (
        "\t// A hidden C1 policy window is intentionally inactive. Keep its injected input\n"
        "\t// alive until one or more game-loop frames have consumed it; ordinary inactive\n"
        "\t// windows retain the upstream release/clear behavior.\n"
        "\tconst bool warptest_background_input = warptest_background_input_is_pending();\n"
        "\tif ((!device->isWindowActive() && !warptest_background_input) ||\n"
        "\t\t\tisMenuActive() || guienv->hasFocus(gui_chat_console.get())) {"
    )
    camera_upstream = (
        "\tif ((device->isWindowActive() && device->isWindowFocused()\n"
        "\t\t\t&& !isMenuActive()) || input->isRandom()) {"
    )
    camera_instrumented = (
        "\tconst bool warptest_background_input = warptest_background_input_is_pending();\n"
        "\tif ((device->isWindowActive() && device->isWindowFocused()\n"
        "\t\t\t&& !isMenuActive()) || input->isRandom() ||\n"
        "\t\t\t(warptest_background_input && !isMenuActive())) {"
    )
    activation_upstream = (
        "\t\tif (m_first_loop_after_window_activation && !g_touchcontrols) {"
    )
    activation_instrumented = (
        "\t\tif (m_first_loop_after_window_activation && !g_touchcontrols &&\n"
        "\t\t\t\t!warptest_background_input) {"
    )
    for upstream, instrumented in (
        (focus_upstream, focus_instrumented),
        (camera_upstream, camera_instrumented),
        (activation_upstream, activation_instrumented),
    ):
        if instrumented in text:
            continue
        if upstream not in text:
            return "skipped C++ background input integration (input anchor not found)"
        text = text.replace(upstream, instrumented, 1)
    if text == original:
        return "existing C++ background input integration present"
    game_cpp.write_text(text, encoding="utf-8")
    return "installed C++ background input integration"


def _replace_between(text: str, begin: str, end: str, replacement: str) -> tuple[str, bool]:
    marker = text.find(begin)
    if marker == -1:
        return text, False
    stop = text.find(end, marker)
    if stop == -1:
        return text, False
    # Replace whole lines (from the start of the BEGIN line through the END line)
    # so any leading indentation already present is not duplicated.
    line_start = text.rfind("\n", 0, marker) + 1
    stop_line_end = text.find("\n", stop)
    if stop_line_end == -1:
        stop_line_end = len(text)
    new_text = text[:line_start] + replacement + text[stop_line_end:]
    return new_text, new_text != text


# ---------------------------------------------------------------------------
# Runtime adapter
# ---------------------------------------------------------------------------


def _build_utility_spec(spec: dict[str, Any]) -> dict[str, Any]:
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    actions = spec.get("actions") if isinstance(spec.get("actions"), list) else []
    assertions = spec.get("assertions") if isinstance(spec.get("assertions"), list) else []
    validations = target.get("validations") if isinstance(target.get("validations"), list) else []
    utility_spec = {
        "target": target,
        "validations": validations,
        "actions": actions,
        "assertions": assertions,
    }
    # C1 provenance probe: the request contains only which public task-relevant
    # locations/items to inspect.  The worldmod fills the returned snapshot with
    # values read from the live engine; expected values are never sent as evidence.
    if isinstance(spec.get("semantic_probe"), dict):
        utility_spec["semantic_probe"] = copy.deepcopy(spec["semantic_probe"])
    return utility_spec


def _binary_name() -> str:
    return "luanti.exe" if platform.system() == "Windows" else "luanti"


def _server_binary_name() -> str:
    return "luantiserver.exe" if platform.system() == "Windows" else "luantiserver"


# Spec features that require an online player, which only exists in a rendered
# single-player client session. Everything else (voxel world, node-meta
# inventories, mod storage, gametime) can run on the headless dedicated server.
_PLAYER_ACTION_TYPES = {
    "luanti_set_player_pos",
    "luanti_set_player_hp",
    "luanti_give_item",
    "luanti_set_player_inventory",
}
_PLAYER_ASSERT_TYPES = {"luanti_player_pos_near", "luanti_player_hp_gte", "luanti_inventory_contains"}
LUANTI_WARPTEST_PLAYER = "warptest"


def _spec_needs_client(spec: dict[str, Any]) -> bool:
    """True when the spec touches online-player state (needs the rendered client)."""
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    if isinstance(target.get("player"), dict):
        return True
    for action in spec.get("actions") or []:
        if isinstance(action, dict) and action.get("type") in _PLAYER_ACTION_TYPES:
            return True
    for assertion in spec.get("assertions") or []:
        if isinstance(assertion, dict) and assertion.get("type") in _PLAYER_ASSERT_TYPES:
            return True
    return False


class LuantiRuntimeAdapter:
    """Run Luanti checkpoint specs through the test-gated WarpTest worldmod.

    The semantic checkpoint runs server-side (embedded server of a single-player
    `--go` session) so player + voxel-world + node-meta-inventory + mod-storage
    state are all mutable; the same rendered session captures the visual oracle
    frame under xvfb via the engine screenshot hook.
    """

    def __init__(
        self,
        repo: Path,
        out_dir: Path,
        *,
        headless: bool = True,
        visual_capture: bool = False,
        map_backend: str = "sqlite3",
        sync_builtin_from_repo: bool = False,
        daylight_mode: str = "natural",
        experiment_seed: int | None = None,
        require_client: bool = False,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.out_dir = Path(out_dir).expanduser().resolve()
        self._visual_capture = visual_capture
        self._sync_builtin_from_repo = sync_builtin_from_repo
        # A headless run normally prefers the dedicated server.  A Lazarus row
        # whose build recipe only rebuilds ``bin/luanti`` must not: the server
        # binary would be shared by both materialized versions and the replay
        # could not tell them apart.
        self._require_client = require_client
        if daylight_mode not in {"natural", "fixed_noon"}:
            raise ValueError("daylight_mode must be 'natural' or 'fixed_noon'")
        self.daylight_mode = daylight_mode
        if experiment_seed is not None and (
            isinstance(experiment_seed, bool) or not isinstance(experiment_seed, int)
        ):
            raise TypeError("experiment_seed must be an integer")
        self.experiment_seed = experiment_seed
        self.headless = headless
        # Map storage backend for the synthesized world. Defaults to on-disk sqlite3;
        # the C3 fuzz warm session uses "dummy" (non-persistent, in-memory) so an
        # adversarial mutated state (extreme coordinates / huge fill regions) cannot
        # balloon map.sqlite and exhaust disk -- the defect surface is unchanged.
        self.map_backend = map_backend
        if platform.system() == "Darwin":
            self.launch_command = (
                "open -n -W ./build-app/out/luanti.app --args --go --gameid devtest "
                f"--world {{world}} --name {LUANTI_WARPTEST_PLAYER} --config {{config}}"
            )
        else:
            self.launch_command = (
                "xvfb-run -a ./bin/luanti --go --gameid devtest --world {world} "
                f"--name {LUANTI_WARPTEST_PLAYER} --config {{config}}"
            )
        self.report_dir = self.out_dir
        self.page = _NoOpPage()
        self.app_url = None
        self.logs: list[str] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {"attempted": []}
        self._last_screenshot_path: str | None = None
        self._last_screenshot_metadata: dict[str, Any] = {
            "screenshot_status": "skipped",
            "screenshot_source": "",
            "screenshot_detail": "",
            "live_source": "capture_failure",
        }

    def __enter__(self) -> "LuantiRuntimeAdapter":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        install = ensure_luanti_instrumentation(self.repo)
        self.logs.append(f"luanti_instrumentation={install['reason']}")
        if self._sync_builtin_from_repo:
            sync = sync_luanti_builtin_assets(self.repo)
            self.logs.append(
                f"luanti_builtin_assets={sync['status']}:{sync['files']}"
            )
            if platform.system() == "Darwin" and sync["status"] != "synced":
                raise RuntimeError(
                    "Lazarus Luanti trial needs a macOS app bundle for versioned builtin assets"
                )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    @property
    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)

    # -- build -------------------------------------------------------------

    def _ensure_built(self, *, server: bool = False) -> Path:
        override = os.environ.get("LUANTI_SERVER_BIN" if server else "LUANTI_BIN")
        if override and Path(override).exists():
            override_binary = Path(override).expanduser().resolve()
            if not server and _read_luanti_client_build_provenance(
                self.repo, override_binary
            ) is None:
                raise RuntimeError(
                    "LUANTI_BIN does not have a valid WarpTest build provenance "
                    "for the current Luanti sources; rebuild it with WarpTest or "
                    "unset LUANTI_BIN"
                )
            return override_binary
        binary = self.repo / "bin" / (_server_binary_name() if server else _binary_name())
        needs_build = not binary.exists()
        # A timestamp cannot prove that a binary contains the current dirty-tree
        # instrumentation (nor distinguish a binary restored from an old cache).
        # Rendered clients therefore require a content-addressed build sidecar.
        if not server and not needs_build:
            needs_build = (
                _read_luanti_client_build_provenance(self.repo, binary) is None
            )
        if not needs_build:
            return binary
        if shutil.which("cmake") is None:
            raise RuntimeError("cmake is required to build Luanti; install cmake or set LUANTI_BIN/LUANTI_SERVER_BIN to a prebuilt binary")

        # Do not reuse Luanti's historical in-tree CMake cache.  Besides being easy
        # to restore accidentally, it can retain a different source/worktree path.
        build_dir = _luanti_build_dir(self.repo)
        build_dir.mkdir(parents=True, exist_ok=True)
        client_source_identity_before = (
            luanti_client_source_identity(self.repo) if not server else None
        )
        timeout = float(os.environ.get("WARPTEST_LUANTI_BUILD_TIMEOUT", "1800"))
        configure = [
            "cmake", "-S", str(self.repo), "-B", str(build_dir),
            "-DCMAKE_BUILD_TYPE=Release", "-DRUN_IN_PLACE=TRUE",
            "-DBUILD_CLIENT=TRUE", "-DBUILD_SERVER=TRUE", "-DBUILD_UNITTESTS=FALSE",
            "-DENABLE_LEVELDB=OFF", "-DENABLE_REDIS=OFF", "-DENABLE_POSTGRESQL=OFF",
        ]
        env = dict(os.environ)
        if platform.system() == "Darwin":
            # On macOS prefer Homebrew libs over system frameworks and build the
            # gettext locale support the engine expects (matches doc/compiling/macos.md).
            # -DLIBICONV_PLUG makes any GNU libiconv header that shadows the SDK's
            # <iconv.h> (e.g. from a conda/macports/brew prefix on the include path)
            # emit the plain iconv_* symbols the macOS SDK libiconv actually exports,
            # instead of the GNU-prefixed libiconv_* ones that fail to link.
            # -lbz2 matches the link flags in util/ci/build_macos.sh.
            configure += [
                "-DCMAKE_FIND_FRAMEWORK=LAST",
                "-DENABLE_GETTEXT=TRUE",
                "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
                "-DCMAKE_EXE_LINKER_FLAGS=-lbz2",
            ]
            env = self._with_homebrew_prefix(env)
        build = [
            "cmake",
            "--build",
            str(build_dir),
            "--target",
            "luantiserver" if server else "luanti",
            "--parallel",
            str(os.cpu_count() or 2),
        ]
        log_path = self.out_dir / "luanti-build.log"
        with log_path.open("w", encoding="utf-8") as log:
            for cmd in (configure, build):
                command_timeout = bounded_task_timeout_s(timeout)
                try:
                    completed = subprocess.run(
                        cmd,
                        cwd=str(self.repo),
                        capture_output=True,
                        text=True,
                        timeout=command_timeout,
                        env=env,
                    )
                except subprocess.TimeoutExpired as error:
                    raise_task_timeout_if_expired(error)
                    raise
                log.write(f"$ {' '.join(cmd)}\n{completed.stdout}\n{completed.stderr}\n")
                if completed.returncode != 0:
                    self.logs.append(str(log_path))
                    raise RuntimeError(f"Luanti build failed; see {log_path}")
        self.logs.append(str(log_path))
        if not binary.exists():
            raise RuntimeError(f"Luanti build did not produce {binary}")
        if not server:
            client_source_identity_after = luanti_client_source_identity(self.repo)
            if client_source_identity_after != client_source_identity_before:
                raise RuntimeError(
                    "Luanti client sources changed while the client was building; "
                    "rerun the build to obtain an exact provenance match"
                )
            _write_luanti_client_build_provenance(self.repo, binary)
            if _read_luanti_client_build_provenance(self.repo, binary) is None:
                raise RuntimeError(
                    "Luanti client build provenance did not validate after build"
                )
        return binary

    @staticmethod
    def _with_homebrew_prefix(env: dict[str, str]) -> dict[str, str]:
        """Help CMake find Homebrew libraries, including keg-only ones (openal-soft,
        gettext), which are not symlinked into the main prefix on macOS."""
        brew = shutil.which("brew")
        if not brew:
            return env
        try:
            prefix = subprocess.run([brew, "--prefix"], capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            prefix = "/opt/homebrew"
        if not prefix:
            prefix = "/opt/homebrew"
        paths = [prefix]
        for keg in ("openal-soft", "gettext", "sqlite", "jpeg-turbo"):
            opt = Path(prefix) / "opt" / keg
            if opt.exists():
                paths.append(str(opt))
        existing = env.get("CMAKE_PREFIX_PATH", "")
        env = dict(env)
        env["CMAKE_PREFIX_PATH"] = ";".join(paths + ([existing] if existing else []))
        return env

    # -- world / config ----------------------------------------------------

    def _prepare_world(self, stem: str) -> Path:
        world = self.out_dir / f"{stem}-world"
        if world.exists():
            shutil.rmtree(world, ignore_errors=True)
        world.mkdir(parents=True, exist_ok=True)
        world_mt = "\n".join(
            [
                "gameid = devtest",
                f"backend = {self.map_backend}",
                "player_backend = sqlite3",
                "auth_backend = sqlite3",
                "mod_storage_backend = sqlite3",
                "world_name = warptest",
                "creative_mode = false",
                "enable_damage = true",
                "load_mod_warptest = true",
                "",
            ]
        )
        (world / "world.mt").write_text(world_mt, encoding="utf-8")
        source_mod = self.repo / LUANTI_WORLDMOD_RELATIVE_DIR
        if source_mod.exists():
            worldmod = world / "worldmods" / "warptest"
            worldmod.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_mod, worldmod)
        if self.daylight_mode == "fixed_noon":
            daylight_mod = world / "worldmods" / "warptest_lazarus_daylight"
            daylight_mod.mkdir(parents=True, exist_ok=True)
            (daylight_mod / "init.lua").write_text(
                "\n".join(
                    [
                        "core.set_timeofday(0.5)",
                        "core.register_on_joinplayer(function()",
                        "  core.after(0, function() core.set_timeofday(0.5) end)",
                        "end)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with (world / "world.mt").open("a", encoding="utf-8") as handle:
                handle.write("load_mod_warptest_lazarus_daylight = true\n")
        return world

    def _write_conf(self, stem: str, world: Path, request_path: Path, report_path: Path, screenshot_path: Path | None) -> Path:
        conf_path = self.out_dir / f"{stem}-minetest.conf"
        lines = [
            f"name = {LUANTI_WARPTEST_PLAYER}",
            "mg_name = flat",
            "mgflat_ground_level = 0",
            "enable_damage = true",
            "creative_mode = false",
            "give_initial_stuff = false",
            "enable_sound = false",
            "sound_volume = 0",
            "show_debug = false",
            "enable_client_modding = false",
            # The warptest worldmod reads its request JSON and writes its report JSON
            # via io.open/safe_file_write; Luanti's mod-security sandbox blocks reads
            # outside the mod/world dir, so disable it for this ephemeral harness world.
            "secure.enable_security = false",
            "secure.trusted_mods = warptest",
            "screen_w = 1280",
            "screen_h = 720",
            "fullscreen = false",
            "window_maximized = false",
            "viewing_range = 80",
            f"screenshot_path = {self.out_dir}",
            "screenshot_format = png",
            f"warptest_request_path = {request_path}",
            f"warptest_report_path = {report_path}",
        ]
        if self.daylight_mode == "fixed_noon":
            lines.append("time_speed = 0")
        if self.experiment_seed is not None:
            lines.append(f"fixed_map_seed = {self.experiment_seed}")
        if screenshot_path is not None:
            lines.extend(
                [
                    f"warptest_screenshot_path = {screenshot_path}",
                    f"warptest_screenshot_frame = {os.environ.get('WARPTEST_LUANTI_SCREENSHOT_FRAME', '200')}",
                    "warptest_exit_after_screenshot = true",
                ]
            )
        lines.append("")
        conf_path.write_text("\n".join(lines), encoding="utf-8")
        return conf_path

    # -- engine invocation -------------------------------------------------

    def _engine_command(self, binary: Path, world: Path, conf_path: Path, use_client: bool) -> list[str]:
        if not use_client:
            # Headless dedicated server: no window/GL needed. The worldmod runs its
            # checkpoint from the on_mods_loaded fallback and requests shutdown.
            return [
                str(binary), "--config", str(conf_path),
                "--world", str(world), "--gameid", "devtest",
                "--logfile", str(self.out_dir / "luanti.log"),
            ]
        if platform.system() == "Darwin":
            app = self._macos_client_app()
            if app is not None:
                return [
                    "open", "-n", "-W", str(app), "--args",
                    "--go", "--gameid", "devtest",
                    "--world", str(world), "--name", LUANTI_WARPTEST_PLAYER,
                    "--config", str(conf_path),
                    "--logfile", str(self.out_dir / "luanti.log"),
                ]
        base = [
            str(binary), "--go", "--gameid", "devtest",
            "--world", str(world), "--name", LUANTI_WARPTEST_PLAYER,
            "--config", str(conf_path),
            "--logfile", str(self.out_dir / "luanti.log"),
        ]
        if shutil.which("xvfb-run") is not None:
            return ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24", *base]
        return base

    def _macos_client_app(self) -> Path | None:
        override = os.environ.get("LUANTI_APP")
        app = Path(override).expanduser().resolve() if override else self.repo / LUANTI_MACOS_APP_RELATIVE_PATH
        binary = app / "Contents" / "MacOS" / "luanti"
        resources = app / "Contents" / "Resources"
        if binary.exists() and (resources / "fonts" / "Arimo-Regular.ttf").exists() and (resources / "games" / "devtest").exists():
            return app
        return None

    def _run_engine(
        self,
        request: dict[str, Any],
        world: Path,
        conf_path: Path,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: float,
        use_client: bool = True,
    ) -> tuple[dict[str, Any], int | None, str, str]:
        request_path.write_text(json_dumps(request), encoding="utf-8")
        if report_path.exists():
            report_path.unlink()
        binary = self._ensure_built(server=not use_client)
        cmd = self._engine_command(binary, world, conf_path, use_client)
        try:
            completed = subprocess.run(cmd, cwd=str(self.repo), capture_output=True, text=True, timeout=timeout_seconds)
            stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, exit_code = _process_output(error.stdout), _process_output(error.stderr), None
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])
        engine_log = self.out_dir / "luanti.log"
        if engine_log.exists():
            self.logs.append(str(engine_log))

        report: dict[str, Any] = {}
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as error:
                report = {"status": "failure", "detail": f"Unable to parse luanti report JSON: {error}", "checks": []}
        else:
            report = {
                "status": "failure",
                "detail": "Luanti WarpTest mod did not write a report (engine likely failed to launch or render).",
                "checks": [{"name": "probe.no_report", "status": "failure", "detail": stderr[-2000:] or "no report written"}],
            }
        return report, exit_code, stdout, stderr

    # -- public API --------------------------------------------------------

    def run_checkpoint(self, spec: dict[str, Any], spec_path: Path) -> dict[str, Any]:
        stem = Path(spec_path).stem
        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        is_save_restore = target.get("kind") == "save_restore"

        # Only capture a screenshot when the oracle policy will actually consume it
        # (self._visual_capture). A spec merely declaring a visual_oracle must not
        # force the rendered client under the default semantic policy.
        screenshot_output: Path | None = None
        if self._visual_capture:
            screenshot_output = self.out_dir / "luanti-visual-post-smoke.png"
            if screenshot_output.exists():
                screenshot_output.unlink()

        # Use the rendered single-player client only when the spec needs an online
        # player or a visual capture; otherwise drive the headless dedicated server
        # (no window/GL/display required), which works in CI and over SSH.
        use_client = (
            screenshot_output is not None
            or self._require_client
            or _spec_needs_client(spec)
        )

        request_path = self.out_dir / f"{stem}-luanti-request.json"
        report_path = self.out_dir / f"{stem}-luanti-result.json"
        stdout_path = self.out_dir / f"{stem}-luanti-stdout.log"
        stderr_path = self.out_dir / f"{stem}-luanti-stderr.log"
        timeout_seconds = int(spec.get("timeout_ms", 180_000) or 180_000) / 1000

        try:
            world = self._prepare_world(stem)
            conf_path = self._write_conf(stem, world, request_path, report_path, screenshot_output)
        except Exception as error:  # pragma: no cover - filesystem failure
            return self._failure_payload(request_path, report_path, stdout_path, stderr_path, str(error), "probe.world_setup_failed")

        try:
            if is_save_restore:
                report, exit_code = self._run_save_restore(spec, world, conf_path, request_path, report_path, stdout_path, stderr_path, timeout_seconds, use_client)
            else:
                request = {
                    "spec_path": str(Path(spec_path).resolve()),
                    "screenshot_output_path": str(screenshot_output or ""),
                    "spec": _build_utility_spec(spec),
                }
                report, exit_code, _, _ = self._run_engine(request, world, conf_path, request_path, report_path, stdout_path, stderr_path, timeout_seconds, use_client)
        except subprocess.TimeoutExpired as error:
            return self._failure_payload(request_path, report_path, stdout_path, stderr_path, f"Luanti build/run timed out: {error}", "probe.luanti_timeout")
        except RuntimeError as error:
            return self._failure_payload(request_path, report_path, stdout_path, stderr_path, str(error), "probe.luanti_build_failed")
        except FileNotFoundError as error:
            return self._failure_payload(request_path, report_path, stdout_path, stderr_path, str(error), "probe.luanti_not_found")
        except OSError as error:
            return self._failure_payload(request_path, report_path, stdout_path, stderr_path, str(error), "probe.luanti_os_error")

        self._resolve_screenshot(report, screenshot_output, spec, spec_path)

        checks = report.get("checks", []) if isinstance(report.get("checks"), list) else []
        status = "success" if exit_code == 0 and report.get("status") == "success" else "failure"
        return {
            "status": status,
            "exit_code": exit_code,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": stdout_path.read_text(encoding="utf-8")[-4000:] if stdout_path.exists() else "",
            "stderr_tail": stderr_path.read_text(encoding="utf-8")[-4000:] if stderr_path.exists() else "",
            "utility_report": report,
            "checks": checks,
            "screenshot_status": self._last_screenshot_metadata.get("screenshot_status"),
            "screenshot_source": self._last_screenshot_metadata.get("screenshot_source"),
            "screenshot_detail": self._last_screenshot_metadata.get("screenshot_detail"),
            "live_source": self._last_screenshot_metadata.get("live_source"),
        }

    def _run_save_restore(
        self,
        spec: dict[str, Any],
        world: Path,
        conf_path: Path,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: float,
        use_client: bool = True,
    ) -> tuple[dict[str, Any], int | None]:
        # Pass 1: synthesize + actions, engine persists world on exit.
        target = copy.deepcopy(spec.get("target") or {})
        pass1_spec = {"target": target, "actions": spec.get("actions", []), "assertions": []}
        pass1_request = {"spec_path": "", "screenshot_output_path": "", "spec": _build_utility_spec(pass1_spec)}
        report1, exit1, _, _ = self._run_engine(
            pass1_request, world, conf_path, request_path, report_path,
            self.out_dir / "pass1-stdout.log", self.out_dir / "pass1-stderr.log", timeout_seconds, use_client,
        )
        synthesis_checks = [
            check for check in (report1.get("checks", []) if isinstance(report1.get("checks"), list) else [])
            if str(check.get("name", "")).startswith("target.state_synthesized") or str(check.get("name", "")).startswith("target.world_loaded")
        ]

        # Pass 2: reopen the persisted world, validate + assert only.
        pass2_target = {"kind": "world_loaded", "validations": target.get("validations", [])}
        pass2_spec = {"target": pass2_target, "actions": [], "assertions": spec.get("assertions", [])}
        pass2_request = {"spec_path": "", "screenshot_output_path": "", "spec": _build_utility_spec(pass2_spec)}
        report2, exit2, _, _ = self._run_engine(
            pass2_request, world, conf_path, request_path, report_path, stdout_path, stderr_path, timeout_seconds, use_client
        )
        pass2_checks = report2.get("checks", []) if isinstance(report2.get("checks"), list) else []
        # Drop pass2's vacuous world_loaded synthesis check; use pass1's restoration check.
        pass2_checks = [c for c in pass2_checks if not str(c.get("name", "")).startswith("target.world_loaded")]
        merged_checks = synthesis_checks + pass2_checks
        status = "success" if all(c.get("status") == "success" for c in merged_checks) and exit1 == 0 and exit2 == 0 else "failure"
        merged = {"status": status, "detail": "Save/restore validated across two engine sessions.", "checks": merged_checks}
        report_path.write_text(json_dumps(merged), encoding="utf-8")
        return merged, (exit2 if exit2 is not None else exit1)

    def _resolve_screenshot(self, report: dict[str, Any], screenshot_output: Path | None, spec: dict[str, Any], spec_path: Path) -> None:
        self._last_screenshot_path = None
        if screenshot_output is None:
            self._last_screenshot_metadata = {
                "screenshot_status": "skipped",
                "screenshot_source": "",
                "screenshot_detail": "",
                "live_source": "capture_failure",
            }
            return
        if screenshot_output.exists() and _screenshot_is_informative(screenshot_output):
            self._last_screenshot_path = str(screenshot_output)
            self._last_screenshot_metadata = {
                "screenshot_status": "success",
                "screenshot_source": "luanti_capture",
                "screenshot_detail": "Engine screenshot hook captured an informative frame.",
                "screenshot_path": str(screenshot_output),
                "live_source": "luanti_capture",
            }
            return
        detail = "Engine did not produce an informative screenshot (headless rendering unavailable?)."
        self._last_screenshot_metadata = {
            "screenshot_status": "failure",
            "screenshot_source": "capture_failure",
            "screenshot_detail": detail,
            "live_source": "capture_failure",
        }
        self.logs.append(f"luanti_visual_capture_failure={detail}")
        fallback = _live_fallback_image(spec, spec_path)
        if fallback is not None:
            diagnostic = screenshot_output.with_name("luanti-visual-fallback.png")
            shutil.copy2(fallback, diagnostic)
            self._last_screenshot_metadata["fallback_image"] = str(diagnostic)

    def screenshot(self, name: str = "luanti-screenshot.png", full_page: bool = False) -> str:
        if self._last_screenshot_path and Path(self._last_screenshot_path).exists():
            target = self.out_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(target) != self._last_screenshot_path:
                shutil.copy2(self._last_screenshot_path, target)
            return str(target)
        detail = self._last_screenshot_metadata.get("screenshot_detail") or "Luanti did not produce an informative image."
        raise RuntimeError(f"Luanti capture failure: {detail}")

    def _failure_payload(
        self,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        detail: str,
        check_name: str,
    ) -> dict[str, Any]:
        return {
            "status": "failure",
            "exit_code": None,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": "",
            "stderr_tail": detail,
            "utility_report": {"status": "failure", "detail": detail},
            "checks": [{"name": check_name, "status": "failure", "detail": detail}],
            "screenshot_status": "skipped",
            "screenshot_source": "",
            "screenshot_detail": "",
            "live_source": "capture_failure",
        }
