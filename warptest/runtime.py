from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import stat
import time
import urllib.request
from pathlib import Path
from typing import Any

from .c1_task_deadline import bounded_task_timeout_s
from .run_context import safe_error_text


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RuntimeErrorWithLog(RuntimeError):
    pass


class BrowserRuntimeAdapter:
    """Reusable browser runtime for local web games."""

    def __init__(
        self,
        repo: Path,
        launch_command_template: str,
        report_dir: Path,
        headless: bool = True,
        *,
        experiment_seed: int | None = None,
    ) -> None:
        self.repo = repo
        self.port = find_free_port()
        self.app_url = f"http://127.0.0.1:{self.port}"
        self.launch_command = launch_command_template.format(port=self.port)
        self.report_dir = report_dir
        self.headless = headless
        self.experiment_seed = int(experiment_seed) if experiment_seed is not None else None
        self.server: subprocess.Popen[str] | None = None
        self.server_log = report_dir / "server.log"
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {"blocked": [], "mocked": [], "attempted": []}
        self._server_log_handle = None
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self) -> "BrowserRuntimeAdapter":
        self.start_server()
        self.start_browser()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start_server(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        log = self.server_log.open("w", encoding="utf-8")
        self._server_log_handle = log
        if self._should_use_static_bundle():
            static_root = self._prepare_static_bundle(log)
            command_args = [
                "python3",
                "-m",
                "http.server",
                str(self.port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(static_root),
            ]
            self.launch_command = " ".join(shlex.quote(arg) for arg in command_args)
            self.server = self._start_process(command_args, cwd=static_root, log=log)
            self._wait_for_server()
            return

        command_args = shlex.split(self.launch_command)
        vite_js = self.repo / "node_modules" / "vite" / "bin" / "vite.js"
        if "npm run dev" in self.launch_command and vite_js.exists():
            self.launch_command = f"node {vite_js} --host 127.0.0.1 --port {self.port}"
            command_args = ["node", str(vite_js), "--host", "127.0.0.1", "--port", str(self.port)]
        self.server = self._start_process(command_args, cwd=self.repo, log=log)
        self._wait_for_server()
        if vite_js.exists() and self._should_probe_vite_entry():
            self._wait_for_vite_entry()

    def _start_process(self, command_args: list[str], *, cwd: Path, log) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command_args,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._server_env(),
        )

    def _wait_for_server(self) -> None:
        deadline = time.time() + 45
        last_error = ""
        while time.time() < deadline:
            if self.server.poll() is not None:
                returncode = self.server.returncode
                self._close_server_log()
                tail = self._server_log_tail()
                raise RuntimeErrorWithLog(
                    f"Launch command exited early with code {returncode}: {self.launch_command}. "
                    f"See {self.server_log}. Log tail:\n{tail}"
                )
            try:
                with urllib.request.urlopen(self.app_url, timeout=1.5) as response:
                    if response.status < 500:
                        return
            except Exception as error:
                last_error = str(error)
                time.sleep(0.4)
        tail = self._server_log_tail()
        self._stop_server()
        raise RuntimeErrorWithLog(
            f"Timed out waiting for {self.app_url}: {last_error}. "
            f"Launch command: {self.launch_command}. See {self.server_log}. Log tail:\n{tail}"
        )

    def _should_use_static_bundle(self) -> bool:
        mode = os.environ.get("WARPTEST_BROWSER_STATIC_BUNDLE", "off").strip().lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        return False

    def _should_probe_vite_entry(self) -> bool:
        mode = os.environ.get("WARPTEST_BROWSER_PROBE_VITE_ENTRY", "auto").strip().lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False
        return self.repo.name in {"SpaceRPG", "DatingSim"} and (self.repo / "src" / "main.tsx").exists()

    def _wait_for_vite_entry(self) -> None:
        entry_url = self.app_url + "/src/main.tsx"
        deadline = time.time() + 8
        last_error = ""
        while time.time() < deadline:
            if self.server and self.server.poll() is not None:
                returncode = self.server.returncode
                self._close_server_log()
                tail = self._server_log_tail()
                raise RuntimeErrorWithLog(
                    f"Launch command exited while probing Vite entry with code {returncode}: {self.launch_command}. "
                    f"See {self.server_log}. Log tail:\n{tail}"
                )
            try:
                with urllib.request.urlopen(entry_url, timeout=1.5) as response:
                    if response.status < 500:
                        return
            except Exception as error:
                last_error = str(error)
                time.sleep(0.4)
        tail = self._server_log_tail()
        self._stop_server()
        raise RuntimeErrorWithLog(
            f"Vite served {self.app_url} but timed out transforming {entry_url}: {last_error}. "
            f"This is a frontend toolchain blocker before browser/LLM scoring. "
            f"Launch command: {self.launch_command}. See {self.server_log}. Log tail:\n{tail}"
        )

    def _prepare_static_bundle(self, log) -> Path:
        index_html = self.repo / "index.html"
        entry = self.repo / "src" / "main.tsx"
        if not index_html.exists() or not entry.exists():
            raise RuntimeErrorWithLog(f"Static bundle fallback requires index.html and src/main.tsx in {self.repo}")

        static_root = self.report_dir / "static-app"
        if static_root.exists():
            shutil.rmtree(static_root)
        static_root.mkdir(parents=True, exist_ok=True)
        public_dir = self.repo / "public"
        if public_dir.exists():
            shutil.copytree(public_dir, static_root, dirs_exist_ok=True)

        esbuild_bin = self._copy_esbuild_binary()
        if not esbuild_bin:
            raise RuntimeErrorWithLog(f"Static bundle fallback requires an esbuild binary in {self.repo}/node_modules")

        metafile = static_root / "esbuild-metafile.json"
        command = [
            str(esbuild_bin),
            str(entry),
            "--bundle",
            "--format=esm",
            "--splitting",
            f"--outdir={static_root}",
            "--entry-names=assets/[name]-[hash]",
            "--chunk-names=assets/[name]-[hash]",
            "--asset-names=assets/[name]-[hash]",
            f"--metafile={metafile}",
            "--loader:.png=file",
            "--loader:.jpg=file",
            "--loader:.jpeg=file",
            "--loader:.gif=file",
            "--loader:.svg=file",
            "--loader:.webp=file",
            "--loader:.glb=file",
            "--loader:.gltf=file",
            "--loader:.ogg=file",
            "--loader:.mp3=file",
            "--loader:.wasm=file",
            "--define:import.meta.env.DEV=true",
            "--define:import.meta.env.PROD=false",
            "--define:import.meta.env.MODE=\"development\"",
            "--define:import.meta.env.BASE_URL=\"/\"",
            "--define:import.meta.env.VITE_ECONOMY_STATIC_FALLBACK=\"true\"",
            "--define:import.meta.env.VITE_MULTIPLAYER_STATIC_DISABLED=\"true\"",
            "--define:import.meta.env.VITE_ECONOMY_API_URL=undefined",
            "--define:import.meta.env.VITE_MULTIPLAYER_API_URL=undefined",
            "--define:import.meta.env.VITE_MULTIPLAYER_ICE_SERVERS=undefined",
        ]
        completed = subprocess.run(
            command,
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=self._server_env(),
            timeout=120,
        )
        log.write("[WarpTest] Static bundle command: " + " ".join(shlex.quote(arg) for arg in command) + "\n")
        if completed.stdout:
            log.write(completed.stdout + "\n")
        if completed.stderr:
            log.write(completed.stderr + "\n")
        log.flush()
        if completed.returncode != 0:
            raise RuntimeErrorWithLog(
                f"Static bundle fallback failed with code {completed.returncode}. See {self.server_log}. "
                f"Log tail:\n{self._server_log_tail()}"
            )

        entry_script, css_files = self._bundle_outputs(metafile, static_root)
        rewritten = self._rewrite_index_html(index_html.read_text(encoding="utf-8"), entry_script, css_files)
        (static_root / "index.html").write_text(rewritten, encoding="utf-8")
        log.write(f"[WarpTest] Serving static browser bundle from {static_root}\n")
        log.flush()
        return static_root

    def _bundle_outputs(self, metafile: Path, static_root: Path) -> tuple[str, list[str]]:
        meta = json.loads(metafile.read_text(encoding="utf-8"))
        outputs = meta.get("outputs", {})
        entry_script: str | None = None
        css_files: list[str] = []
        for output_path, output_meta in outputs.items():
            rel = self._static_output_relpath(output_path, static_root)
            if output_meta.get("entryPoint", "").endswith("src/main.tsx") and rel.endswith(".js"):
                entry_script = rel
                css_bundle = output_meta.get("cssBundle")
                if isinstance(css_bundle, str):
                    css_files.append(self._static_output_relpath(css_bundle, static_root))
            elif rel.endswith(".css"):
                css_files.append(rel)
        if not entry_script:
            scripts = sorted(self._static_output_relpath(path, static_root) for path in outputs if path.endswith(".js"))
            if not scripts:
                raise RuntimeErrorWithLog(f"Static bundle fallback produced no JavaScript outputs: {metafile}")
            entry_script = scripts[0]
        return entry_script, sorted(dict.fromkeys(css_files))

    def _static_output_relpath(self, output_path: str, static_root: Path) -> str:
        path = Path(output_path)
        if not path.is_absolute():
            path = (self.repo / path).resolve()
        try:
            rel = path.relative_to(static_root.resolve())
        except ValueError:
            rel = path.relative_to(self.repo.resolve())
        return rel.as_posix()

    @staticmethod
    def _rewrite_index_html(html: str, entry_script: str, css_files: list[str]) -> str:
        html = re.sub(
            r'\s*<script\s+type=["\']module["\']\s+src=["\']/src/main\.tsx["\']\s*>\s*</script>',
            "",
            html,
            flags=re.IGNORECASE,
        )
        if css_files:
            css_tags = "\n".join(f'    <link rel="stylesheet" href="/{path}" />' for path in css_files)
            html = html.replace("</head>", f"{css_tags}\n  </head>", 1)
        script_tag = f'    <script type="module" src="/{entry_script}"></script>'
        if "</body>" in html:
            return html.replace("</body>", f"{script_tag}\n  </body>", 1)
        return html.rstrip() + "\n" + script_tag + "\n"

    def start_browser(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as error:  # pragma: no cover - environment dependent
            raise RuntimeError("Python Playwright is required. Install with `pip install playwright`.") from error
        self._playwright = sync_playwright().start()
        executable = os.environ.get(
            "WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""
        ).strip()
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": ["--disable-gpu"],
        }
        if executable:
            executable_path = Path(executable).expanduser().resolve()
            if not executable_path.is_file():
                raise RuntimeError(
                    "WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE is not a file: "
                    f"{executable_path}"
                )
            launch_kwargs["executable_path"] = str(executable_path)
        self.browser = self._playwright.chromium.launch(**launch_kwargs)
        self._new_context()

    def _new_context(self) -> None:
        # C1 GUI coordinates are expressed in screenshot pixels.  Pinning both the
        # CSS viewport and DPR keeps screenshot pixels, policy coordinates, and
        # Playwright mouse coordinates in a 1:1 mapping on Retina and non-Retina
        # hosts alike.
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
        )
        if self.experiment_seed is not None:
            self.context.add_init_script(script=self._deterministic_random_script(self.experiment_seed))
        self.page = None

    @staticmethod
    def _deterministic_random_script(seed: int) -> str:
        """Return a pre-navigation deterministic ``Math.random`` initializer.

        The implementation is Mulberry32 with an unsigned 32-bit seed.  The
        metadata is deliberately exposed read-only-ish on ``window`` so the C1
        harness can record which PRNG was installed without reading hidden game
        state.  ``add_init_script`` installs this before application scripts run.
        """

        unsigned = int(seed) & 0xFFFFFFFF
        return f"""
        (() => {{
          let state = {unsigned} >>> 0;
          let calls = 0;
          const random = () => {{
            state = (state + 0x6D2B79F5) >>> 0;
            let t = state;
            t = Math.imul(t ^ (t >>> 15), t | 1);
            t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
            calls += 1;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
          }};
          Math.random = random;
          Object.defineProperty(window, '__WARPTEST_EXPERIMENT_PRNG__', {{
            configurable: false,
            enumerable: false,
            value: {{algorithm: 'mulberry32', seed: {unsigned}, calls: () => calls}}
          }});
        }})();
        """

    def prng_metadata(self) -> dict[str, Any]:
        """Read seed/viewport evidence from the live page, never from constructor defaults.

        C1 uses this as experimental provenance.  Returning the requested seed or the
        configured viewport when no page exists would let the harness attest to its own
        intent even if the init script never ran.  The ``observed`` bit is therefore
        true only when the values are read back from the navigated renderer.
        """

        metadata: dict[str, Any] = {
            "source": "browser_page_runtime",
            "observed": False,
            "algorithm": None,
            "seed": None,
            "calls": None,
            "viewport": None,
            "device_scale_factor": None,
        }
        page = self.page
        if page is None:
            return metadata
        try:
            observed = page.evaluate(
                """() => {
                  const prng = window.__WARPTEST_EXPERIMENT_PRNG__;
                  return {
                    algorithm: prng?.algorithm ?? null,
                    seed: prng?.seed ?? null,
                    calls: prng?.calls?.() ?? null,
                    viewport: {width: window.innerWidth, height: window.innerHeight},
                    device_scale_factor: window.devicePixelRatio,
                  };
                }"""
            )
        except Exception:
            return metadata
        if not isinstance(observed, dict):
            return metadata
        viewport = observed.get("viewport")
        valid = bool(
            isinstance(observed.get("algorithm"), str)
            and observed.get("algorithm")
            and isinstance(observed.get("seed"), int)
            and not isinstance(observed.get("seed"), bool)
            and isinstance(observed.get("calls"), int)
            and not isinstance(observed.get("calls"), bool)
            and observed.get("calls") >= 0
            and isinstance(viewport, dict)
            and isinstance(viewport.get("width"), int)
            and isinstance(viewport.get("height"), int)
            and isinstance(observed.get("device_scale_factor"), (int, float))
            and not isinstance(observed.get("device_scale_factor"), bool)
        )
        metadata.update(observed)
        metadata["source"] = "browser_page_runtime"
        metadata["observed"] = valid
        return metadata

    def reset_browser_context(self) -> None:
        """Clear page/context-scoped state while keeping the dev server and browser warm."""

        page = getattr(self, "page", None)
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.console_errors = []
        self.page_errors = []
        self.network_api_calls = {"blocked": [], "mocked": [], "attempted": []}
        self._new_context()

    def _server_env(self) -> dict[str, str]:
        env = os.environ.copy()
        preload = Path(__file__).resolve().parent / "node_preloads" / "no_fsevents.cjs"
        if preload.exists():
            existing_options = env.get("NODE_OPTIONS", "").strip()
            preload_option = f"--require={preload}"
            if preload_option not in existing_options:
                env["NODE_OPTIONS"] = f"{existing_options} {preload_option}".strip()
        env.setdefault("CHOKIDAR_USEPOLLING", "1")
        env.setdefault("CHOKIDAR_INTERVAL", "100")
        esbuild_bin = self._copy_esbuild_binary()
        if esbuild_bin and "ESBUILD_BINARY_PATH" not in env:
            env["ESBUILD_BINARY_PATH"] = str(esbuild_bin)
        return env

    def _copy_esbuild_binary(self) -> Path | None:
        source = self.repo / "node_modules" / "@esbuild" / "darwin-arm64" / "bin" / "esbuild"
        if not source.exists():
            return None
        target_dir = self.report_dir / "runtime-bin"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "esbuild"
        if not target.exists():
            shutil.copyfile(source, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return target.resolve()

    def install_storage_init(self, values: dict[str, str], clear_keys: list[str] | None = None) -> list[str]:
        touched = list(clear_keys or []) + list(values)
        payload = json.dumps({"clearKeys": clear_keys or [], "values": values})
        script = f"""
        (() => {{
          const payload = {payload};
          for (const key of payload.clearKeys) window.localStorage.removeItem(key);
          for (const [key, value] of Object.entries(payload.values)) window.localStorage.setItem(key, value);
        }})();
        """
        self.context.add_init_script(script=script)
        return touched

    def block_ai_network(self, mode: str = "mocked") -> None:
        patterns = ["**/api/proxy/**", "**/v1/chat/completions", "**/ai.yaml", "**/api.openai.com/**"]

        def handler(route, request) -> None:
            url = request.url
            if url.endswith("/ai.yaml"):
                self.network_api_calls[mode].append(url)
                route.fulfill(
                    status=200,
                    content_type="text/yaml",
                    body="llm:\n  service: warptest\n  model1: warptest-mock\n  model2: warptest-mock\n  model3: warptest-mock\n  openai_api_key: warptest\n  openai_base_url: http://127.0.0.1\n",
                )
                return
            self.network_api_calls["blocked"].append(url)
            route.abort()

        for pattern in patterns:
            self.context.route(pattern, handler)

    def open(self, path: str = "/", wait_until: str = "domcontentloaded") -> None:
        self.page = self.context.new_page()
        self.page.on("request", self._on_request)
        self.page.on("console", self._on_console)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        url = self.app_url + path
        self.page.goto(url, wait_until=wait_until, timeout=45_000)

    def _on_console(self, message) -> None:
        if message.type in {"error"}:
            self.console_errors.append(message.text)

    def _on_request(self, request) -> None:
        """Audit real AI endpoint attempts independently of route outcomes."""

        url = str(request.url)
        lowered = url.lower()
        if any(
            marker in lowered
            for marker in ("/api/proxy/", "/v1/chat/completions", "api.openai.com")
        ):
            self.network_api_calls["attempted"].append(url)

    def wait_for_hook(self, hook_name: str, timeout_ms: int = 20_000) -> None:
        self.page.wait_for_function(f"() => !!window.{hook_name}", timeout=timeout_ms)

    def wait_for_canvas_ready(self, timeout_ms: int = 20_000) -> dict[str, Any]:
        self.page.wait_for_function(
            """() => {
              const canvas = document.querySelector('canvas');
              if (!canvas) return false;
              const rect = canvas.getBoundingClientRect();
              return rect.width > 32 && rect.height > 32;
            }""",
            timeout=timeout_ms,
        )
        return self.page.evaluate(
            """() => {
              const canvas = document.querySelector('canvas');
              const rect = canvas?.getBoundingClientRect();
              return { present: !!canvas, width: rect?.width ?? 0, height: rect?.height ?? 0 };
            }"""
        )

    def screenshot(self, name: str, full_page: bool = False) -> str:
        path = self.report_dir / name
        # Busy WebGL scenes can finish compositor capture well after fonts are
        # ready. Keep the screenshot strict while respecting the trial deadline.
        timeout_ms = float(bounded_task_timeout_s(60.0)) * 1000
        try:
            self.page.screenshot(path=str(path), full_page=full_page, timeout=timeout_ms)
        except Exception as error:
            # StateSession's optional-capture path may return None; preserve the
            # underlying failure so golden-generation errors remain diagnosable.
            try:
                self.report_dir.mkdir(parents=True, exist_ok=True)
                with (self.report_dir / "screenshot-errors.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "schema": "warptest-browser-screenshot-error-v1",
                        "name": name,
                        "timeout_ms": timeout_ms,
                        "error_type": type(error).__name__,
                        "error": safe_error_text(error),
                    }) + "\n")
            except Exception:
                pass
            raise
        return str(path)

    def close(self) -> None:
        # Playwright may already have closed its transport after a long-running
        # headed trial.  Teardown is best-effort and must not overwrite an otherwise
        # complete trial with ``Browser.close: Connection closed``.  Null each handle
        # before invoking it so repeated cleanup remains idempotent, and always stop
        # the local game server even when one Playwright layer is already gone.
        context, self.context = self.context, None
        browser, self.browser = self.browser, None
        playwright, self._playwright = self._playwright, None
        for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception:
                pass
        try:
            self._stop_server()
        except Exception:
            # Cleanup failures are not test outcomes.  A live interaction failure is
            # still surfaced at the operation that encountered it; this only avoids
            # turning teardown transport races into incomplete C1 samples.
            pass

    def _server_log_tail(self, max_chars: int = 4000) -> str:
        if self._server_log_handle:
            self._server_log_handle.flush()
        if not self.server_log.exists():
            return ""
        try:
            content = self.server_log.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return content[-max_chars:].strip()

    def _close_server_log(self) -> None:
        if self._server_log_handle:
            self._server_log_handle.close()
            self._server_log_handle = None

    def _stop_server(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        self._close_server_log()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
