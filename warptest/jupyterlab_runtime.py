from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .c1_evaluation import evaluation_contract, semantic_assertions, surface_contract
from .reports import write_json
from .runtime import RuntimeErrorWithLog, find_free_port


_LEGACY_JUPYTER_MEANINGFUL_ASSERTIONS = (
    "jupyterlab_file_text_equals",
    "jupyterlab_notebook_output_contains",
)


def _jupyter_changed_from_start_requirement(
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Read the formal transition requirement without inspecting actions.

    Root-format generated/legacy specs retain the historical requirement as an
    in-memory compatibility default.  Evaluation-v1 specs fail closed if their
    declared surface omits or duplicates it.
    """

    for surface_name in ("gui", "state"):
        surface = surface_contract(spec, surface_name)
        goal = surface.get("goal") if isinstance(surface, dict) else None
        requirements = goal.get("requirements") if isinstance(goal, dict) else None
        matches = [
            item
            for item in (requirements if isinstance(requirements, list) else [])
            if isinstance(item, dict)
            and item.get("name") == "transition.changed_from_start"
        ]
        if len(matches) == 1:
            return matches[0]
    if evaluation_contract(spec) is not None:
        raise ValueError(
            "JupyterLab evaluation contract lacks transition.changed_from_start"
        )
    return {
        "name": "transition.changed_from_start",
        "probe": "jupyter.changed_from_start",
        "metric": "equals",
        "expected": True,
        "meaningful_assertions": list(_LEGACY_JUPYTER_MEANINGFUL_ASSERTIONS),
    }


class JupyterLabRuntimeAdapter:
    """Isolated JupyterLab server runtime for API-backed GUI checkpoints."""

    def __init__(
        self,
        repo: Path,
        report_dir: Path,
        headless: bool = True,
        *,
        python_executable: str | None = None,
        galata_mode: bool = False,
    ) -> None:
        self.repo = repo.resolve()
        self.report_dir = report_dir.resolve()
        self.headless = headless
        self.galata_mode = bool(galata_mode)
        self.port = find_free_port()
        self.app_url = f"http://127.0.0.1:{self.port}/lab"
        self.api_url = f"http://127.0.0.1:{self.port}/api"
        self.lab_api_url = f"http://127.0.0.1:{self.port}/lab/api"
        if python_executable is not None and (
            type(python_executable) is not str or not python_executable.strip()
        ):
            raise ValueError("JupyterLab runtime Python executable is invalid")
        self.python = (
            python_executable.strip()
            if python_executable is not None
            else os.environ.get("WARPTEST_JUPYTERLAB_PYTHON", sys.executable)
        )
        self.server: subprocess.Popen[str] | None = None
        self.server_log = self.report_dir / "jupyterlab-server.log"
        self._server_log_handle = None
        self.runtime_root = self.report_dir / "jupyterlab-runtime"
        self.root_dir = self.runtime_root / "root"
        self.config_dir = self.runtime_root / "config"
        self.data_dir = self.runtime_root / "data"
        self.runtime_dir = self.runtime_root / "runtime"
        self.workspaces_dir = self.runtime_root / "workspaces"
        self.app_dir = self._resolve_app_dir()
        self.launch_command = self._format_launch_command()
        self.sessions: list[str] = []
        self.logs: list[str] = []
        self._sessions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {
            "attempted": [],
            "source": "jupyterlab_browser_external_request_audit",
        }
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._active_workspace: str | None = None
        self._last_opened_paths: list[str] = []
        self._last_screenshot_metadata: dict[str, Any] = {
            "live_source": "jupyterlab_browser_capture"
        }
        self._c1_spec: dict[str, Any] | None = None
        self._c1_start_target_fingerprint: str | None = None

    def __enter__(self) -> "JupyterLabRuntimeAdapter":
        self.start_server()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _galata_launch_args(self) -> list[str]:
        """Server options Galata's own ``configure_jupyter_server`` requires.

        Galata waits on ``window.jupyterapp``, which the JupyterLab bootstrap
        only exposes under ``expose_app_in_browser``, and its fixtures drive the
        test-helper labextension shipped inside the checkout.  Without both,
        every Galata test times out before it reaches its own assertions.
        """

        if not self.galata_mode:
            return []
        args = ["--LabApp.expose_app_in_browser=True"]
        helpers = self.repo / "jupyterlab" / "galata"
        if helpers.is_dir():
            args.append(f"--LabServerApp.extra_labextensions_path={helpers}")
        return args

    def _format_launch_command(self) -> str:
        return (
            f"{self.python} -m jupyterlab --no-browser "
            f"--ServerApp.ip=127.0.0.1 --ServerApp.port={self.port} "
            "--ServerApp.port_retries=0 --ServerApp.token= --ServerApp.password= "
            "--ServerApp.disable_check_xsrf=True --ServerApp.open_browser=False "
            f"--ServerApp.root_dir={self.root_dir} --LabApp.workspaces_dir={self.workspaces_dir}"
            + (f" --LabApp.app_dir={self.app_dir}" if self.app_dir else "")
            + "".join(f" {value}" for value in self._galata_launch_args())
        )

    def _resolve_app_dir(self) -> Path | None:
        configured = os.environ.get("WARPTEST_JUPYTERLAB_APP_DIR")
        candidates = [Path(configured)] if configured else [self.repo / "dev_mode"]
        for candidate in candidates:
            if (candidate / "static" / "index.html").exists():
                return candidate.resolve()
        return None

    def start_server(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self._write_runtime_config()
        self._ensure_python_dependency()
        log = self.server_log.open("w", encoding="utf-8")
        self._server_log_handle = log
        env = os.environ.copy()
        env.update(
            {
                "JUPYTER_CONFIG_DIR": str(self.config_dir),
                "JUPYTER_DATA_DIR": str(self.data_dir),
                "JUPYTER_RUNTIME_DIR": str(self.runtime_dir),
                "JUPYTERLAB_GALATA_ROOT_DIR": str(self.root_dir),
                "PYTHONUNBUFFERED": "1",
            }
        )
        if str(self.repo) not in env.get("PYTHONPATH", "").split(os.pathsep):
            env["PYTHONPATH"] = str(self.repo) + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
        args = [
            self.python,
            "-m",
            "jupyterlab",
            "--no-browser",
            "--ServerApp.ip=127.0.0.1",
            f"--ServerApp.port={self.port}",
            "--ServerApp.port_retries=0",
            "--ServerApp.token=",
            "--ServerApp.password=",
            "--ServerApp.disable_check_xsrf=True",
            "--ServerApp.open_browser=False",
            f"--ServerApp.root_dir={self.root_dir}",
            f"--LabApp.workspaces_dir={self.workspaces_dir}",
        ]
        if self.app_dir is not None:
            args.append(f"--LabApp.app_dir={self.app_dir}")
        args.extend(self._galata_launch_args())
        self.server = subprocess.Popen(
            args,
            cwd=str(self.repo),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        self._wait_for_server()
        self.logs.append(str(self.server_log))

    def _write_runtime_config(self) -> None:
        config = self.config_dir / "jupyter_lab_config.py"
        config.write_text("c.LabApp.news_url = None\n", encoding="utf-8")

    def _ensure_python_dependency(self) -> None:
        code = "import jupyterlab, jupyter_server, jupyter_client"
        env = os.environ.copy()
        if str(self.repo) not in env.get("PYTHONPATH", "").split(os.pathsep):
            env["PYTHONPATH"] = str(self.repo) + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
        result = subprocess.run(
            [self.python, "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(self.repo),
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeErrorWithLog(
                "JupyterLab runtime dependencies are not installed for "
                f"{self.python}. Install the JupyterLab checkout/dependencies or set "
                f"WARPTEST_JUPYTERLAB_PYTHON to an environment with jupyterlab, "
                f"jupyter_server, and jupyter_client. Import error: {detail}"
            )

    def _wait_for_server(self) -> None:
        deadline = time.time() + 60
        last_error = ""
        while time.time() < deadline:
            if self.server and self.server.poll() is not None:
                returncode = self.server.returncode
                self._close_log()
                raise RuntimeErrorWithLog(
                    f"JupyterLab launch exited early with code {returncode}: {self.launch_command}. "
                    f"See {self.server_log}. Log tail:\n{self._log_tail()}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/status", timeout=1.5
                ) as response:
                    if response.status < 500:
                        return
            except Exception as error:
                last_error = str(error)
                time.sleep(0.4)
        self._stop_server()
        raise RuntimeErrorWithLog(
            f"Timed out waiting for JupyterLab at {self.app_url}: {last_error}. "
            f"Launch command: {self.launch_command}. See {self.server_log}. Log tail:\n{self._log_tail()}"
        )

    def run_checkpoint(self, spec: dict[str, Any], spec_path: Path) -> dict[str, Any]:
        out_dir = self.report_dir / "jupyterlab"
        out_dir.mkdir(parents=True, exist_ok=True)
        request_path = out_dir / "request.json"
        report_path = out_dir / "report.json"
        write_json(request_path, {"spec_path": str(spec_path), "spec": spec})

        checks: list[dict[str, Any]] = []
        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        self._run_check(
            checks, "target.state_synthesized", lambda: self._restore_target(target)
        )
        self._run_check(
            checks, "target.validate.kind", lambda: self._validate_target(target)
        )
        for index, action in enumerate(spec.get("actions", [])):
            self._run_check(
                checks,
                f"action[{action.get('type', 'unknown')}]@{index}",
                lambda action=action: self._run_action(action),
            )
        for index, assertion in enumerate(semantic_assertions(spec) or []):
            self._run_check(
                checks,
                f"assertion[{assertion.get('type', 'unknown')}]@{index}",
                lambda assertion=assertion: self._run_assertion(assertion),
            )

        status = (
            "success"
            if all(check.get("status") == "success" for check in checks)
            else "failure"
        )
        result = {
            "status": status,
            "checks": checks,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "root_dir": str(self.root_dir),
            "workspaces_dir": str(self.workspaces_dir),
            "app_url": self.app_url,
        }
        write_json(report_path, result)
        return result

    def _run_check(self, checks: list[dict[str, Any]], name: str, func) -> None:
        start = time.perf_counter()
        try:
            detail, data = func()
            checks.append(
                {
                    "name": name,
                    "status": "success",
                    "detail": detail,
                    "data": data,
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                }
            )
        except Exception as error:
            checks.append(
                {
                    "name": name,
                    "status": "failure",
                    "detail": str(error),
                    "data": {},
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                }
            )

    def _restore_target(self, target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = target.get("kind")
        if kind == "text_file":
            path = self._target_path(target)
            self.put_file(path, str(target.get("content", "")))
            return "Created target text file through Jupyter Contents API.", {
                "path": path
            }
        if kind == "notebook":
            path = self._target_path(target)
            self.put_notebook(path, self._notebook_model(target))
            return "Created target notebook through Jupyter Contents API.", {
                "path": path
            }
        if kind == "workspace_layout":
            workspace = str(target.get("workspace", "warptest"))
            paths = [
                str(path) for path in target.get("paths", []) if isinstance(path, str)
            ]
            for path in paths:
                if path.endswith(".ipynb"):
                    self.put_notebook(path, self._notebook_model({"cells": []}))
                else:
                    self.put_file(path, "")
            self.save_workspace(workspace, paths)
            return "Prepared workspace layout target.", {
                "workspace": workspace,
                "paths": paths,
            }
        if kind == "terminal_session":
            return "Prepared terminal/session smoke target.", {"kind": kind}
        raise ValueError(f"Unsupported JupyterLab target.kind: {kind}")

    def _validate_target(self, target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = target.get("kind")
        if kind in {"text_file", "notebook"}:
            path = self._target_path(target)
            model = self.get_content(path)
            if model.get("path") != path:
                raise AssertionError(
                    f"Content API returned unexpected path for {path}: {model.get('path')}"
                )
            return "Target content exists in JupyterLab root.", {
                "path": path,
                "type": model.get("type"),
            }
        if kind == "workspace_layout":
            workspace = str(target.get("workspace", "warptest"))
            expected = set(
                str(path) for path in target.get("paths", []) if isinstance(path, str)
            )
            actual = self.workspace_paths(workspace)
            missing = sorted(expected - actual)
            if missing:
                raise AssertionError(f"Workspace target is missing paths: {missing}")
            return "Workspace target paths are persisted.", {
                "workspace": workspace,
                "paths": sorted(actual),
            }
        if kind == "terminal_session":
            return "Terminal/session target is ready.", {"kind": kind}
        raise ValueError(f"Unsupported JupyterLab target.kind: {kind}")

    def _run_action(self, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        action_type = action.get("type")
        if action_type == "jupyterlab_create_text_file":
            path = self._action_path(action)
            self.put_file(path, str(action.get("content", "")))
            return "Created text file.", {"path": path}
        if action_type == "jupyterlab_open_file":
            path = self._action_path(action)
            self.get_content(path, content=False)
            workspace = str(action.get("workspace", "warptest"))
            paths = sorted(self.workspace_paths(workspace) | {path})
            self.save_workspace(workspace, paths)
            self._remember_opened_path(path, workspace=workspace)
            return "Opened file through JupyterLab workspace metadata.", {
                "path": path,
                "workspace": workspace,
            }
        if action_type == "jupyterlab_edit_text_file":
            path = self._action_path(action)
            self.put_file(path, str(action.get("content", "")))
            self._remember_opened_path(path)
            return "Edited text file.", {"path": path}
        if action_type == "jupyterlab_create_notebook":
            path = self._action_path(action)
            self.put_notebook(path, self._notebook_model(action))
            self._remember_opened_path(path)
            return "Created notebook.", {"path": path}
        if action_type == "jupyterlab_edit_notebook_cell":
            path = self._action_path(action)
            cell_index = int(action.get("cell_index", 0))
            model = self.get_content(path)
            notebook = model.get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            if not isinstance(cells, list) or not 0 <= cell_index < len(cells):
                raise IndexError(
                    f"Notebook cell index out of range for {path}: {cell_index}"
                )
            cells[cell_index]["source"] = str(action.get("source", ""))
            cells[cell_index]["outputs"] = []
            cells[cell_index]["execution_count"] = None
            self.put_notebook(path, notebook)
            self._remember_opened_path(path)
            return "Edited an allowlisted notebook cell.", {
                "path": path,
                "cell_index": cell_index,
            }
        if action_type == "jupyterlab_run_notebook_cell":
            path = self._action_path(action)
            cell_index = int(action.get("cell_index", 0))
            output = self.run_notebook_cell(path, cell_index)
            self._remember_opened_path(path)
            return "Ran notebook cell with a Jupyter kernel.", {
                "path": path,
                "cell_index": cell_index,
                "output": output,
            }
        if action_type == "jupyterlab_run_notebook":
            path = self._action_path(action)
            model = self.get_content(path)
            notebook = model.get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            if not isinstance(cells, list):
                raise ValueError(f"Notebook has no cells: {path}")
            outputs = []
            for cell_index, cell in enumerate(cells):
                if isinstance(cell, dict) and cell.get("cell_type") == "code":
                    outputs.append(self.run_notebook_cell(path, cell_index))
            self._remember_opened_path(path)
            return "Ran all notebook code cells in order.", {
                "path": path,
                "outputs": outputs,
            }
        if action_type == "jupyterlab_export_notebook_html":
            path = self._action_path(action)
            output_path = self._safe_relative_path(str(action.get("output_path", "")))
            source = self.root_dir / path
            destination = self.root_dir / output_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    self.python,
                    "-m",
                    "jupyter",
                    "nbconvert",
                    "--to",
                    "html",
                    "--output",
                    destination.stem,
                    "--output-dir",
                    str(destination.parent),
                    str(source),
                ],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0 or not destination.exists():
                raise RuntimeError(
                    "Notebook HTML export failed: "
                    + (completed.stderr or completed.stdout or "no output").strip()
                )
            self._remember_opened_path(output_path)
            return "Exported notebook to HTML.", {
                "path": path,
                "output_path": output_path,
            }
        if action_type == "jupyterlab_set_workspace_layout":
            workspace = str(action.get("workspace", "warptest"))
            paths = [
                str(path) for path in action.get("paths", []) if isinstance(path, str)
            ]
            self.save_workspace(workspace, paths)
            return "Saved workspace layout through the Jupyter workspace API.", {
                "workspace": workspace,
                "paths": paths,
            }
        if action_type == "jupyterlab_start_session":
            path = self._action_path(action)
            session = self.start_session(path, str(action.get("kernel", "python3")))
            self._remember_opened_path(path)
            return "Started Jupyter kernel session.", {
                "path": path,
                "session_id": session.get("id"),
            }
        if action_type == "wait":
            time.sleep(float(action.get("ms", 250)) / 1000.0)
            return "Waited.", {"ms": int(action.get("ms", 250))}
        raise ValueError(f"Unsupported JupyterLab action: {action}")

    def _run_assertion(self, assertion: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        assertion_type = assertion.get("type")
        if assertion_type == "jupyterlab_file_exists":
            path = self._assertion_path(assertion)
            model = self.get_content(path, content=False)
            return "File exists.", {"path": path, "type": model.get("type")}
        if assertion_type == "jupyterlab_file_text_equals":
            path = self._assertion_path(assertion)
            actual = self.get_file_text(path)
            expected = str(assertion.get("content", ""))
            if actual != expected:
                raise AssertionError(
                    f"Text content mismatch for {path}: expected {expected!r}, got {actual!r}"
                )
            return "Text file content matched.", {"path": path}
        if assertion_type == "jupyterlab_notebook_output_contains":
            path = self._assertion_path(assertion)
            needle = str(assertion.get("text", ""))
            outputs = self.notebook_outputs(path)
            if needle not in "\n".join(outputs):
                raise AssertionError(
                    f"Notebook output did not contain {needle!r}: {outputs}"
                )
            return "Notebook output contained expected text.", {
                "path": path,
                "text": needle,
            }
        if assertion_type == "jupyterlab_notebook_cell_source_equals":
            path = self._assertion_path(assertion)
            cell_index = int(assertion.get("cell_index", 0))
            notebook = self.get_content(path).get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            if not isinstance(cells, list) or not 0 <= cell_index < len(cells):
                raise AssertionError(
                    f"Notebook cell index out of range for {path}: {cell_index}"
                )
            actual = str(cells[cell_index].get("source", ""))
            expected = str(assertion.get("source", ""))
            if actual != expected:
                raise AssertionError(
                    f"Notebook cell source mismatch at {path}[{cell_index}]"
                )
            return "Notebook cell source matched.", {
                "path": path,
                "cell_index": cell_index,
            }
        if assertion_type == "jupyterlab_notebook_all_code_cells_executed":
            path = self._assertion_path(assertion)
            notebook = self.get_content(path).get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            pending = [
                index
                for index, cell in enumerate(cells or [])
                if isinstance(cell, dict)
                and cell.get("cell_type") == "code"
                and cell.get("execution_count") is None
            ]
            if pending:
                raise AssertionError(f"Notebook has unexecuted code cells: {pending}")
            return "All notebook code cells were executed.", {"path": path}
        if assertion_type == "jupyterlab_notebook_has_mime":
            path = self._assertion_path(assertion)
            mime = str(assertion.get("mime", ""))
            notebook = self.get_content(path).get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            found = any(
                mime in output.get("data", {})
                for cell in cells or []
                if isinstance(cell, dict)
                for output in cell.get("outputs", [])
                if isinstance(output, dict)
            )
            if not found:
                raise AssertionError(f"Notebook output did not contain MIME {mime!r}")
            return "Notebook output MIME was present.", {"path": path, "mime": mime}
        if assertion_type == "jupyterlab_notebook_has_no_error_outputs":
            path = self._assertion_path(assertion)
            notebook = self.get_content(path).get("content")
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            errors = [
                output
                for cell in cells or []
                if isinstance(cell, dict)
                for output in cell.get("outputs", [])
                if isinstance(output, dict) and output.get("output_type") == "error"
            ]
            if errors:
                raise AssertionError(f"Notebook contains error outputs: {errors}")
            return "Notebook contains no error outputs.", {"path": path}
        if assertion_type == "jupyterlab_workspace_contains":
            workspace = str(assertion.get("workspace", "warptest"))
            path = self._assertion_path(assertion)
            persisted = self.workspace_paths(workspace)
            if path not in persisted:
                raise AssertionError(f"Workspace {workspace} does not contain {path}.")
            return "Workspace contains expected path.", {
                "workspace": workspace,
                "path": path,
            }
        if assertion_type == "jupyterlab_session_started":
            if not self.sessions:
                raise AssertionError("No Jupyter session was started.")
            return "Jupyter session started.", {"sessions": list(self.sessions)}
        if assertion_type == "no_jupyterlab_server_errors":
            tail = self._log_tail()
            severe = [
                line
                for line in tail.splitlines()
                if "[E " in line or "Traceback" in line
            ]
            if severe:
                raise AssertionError(
                    "JupyterLab server logged severe errors: " + "\n".join(severe[-5:])
                )
            return "No severe JupyterLab server errors were found.", {}
        raise ValueError(f"Unsupported JupyterLab assertion: {assertion}")

    def _target_path(self, target: dict[str, Any]) -> str:
        path = target.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("JupyterLab target.path must be a non-empty string")
        return path

    def _action_path(self, action: dict[str, Any]) -> str:
        path = action.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"{action.get('type')} requires a non-empty path")
        return path

    def _assertion_path(self, assertion: dict[str, Any]) -> str:
        path = assertion.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"{assertion.get('type')} requires a non-empty path")
        return path

    def _safe_relative_path(self, value: str) -> str:
        text = value.strip().replace("\\", "/")
        path = Path(text)
        if not text or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Path must stay inside the task sandbox: {value!r}")
        return path.as_posix()

    def _notebook_model(self, source: dict[str, Any]) -> dict[str, Any]:
        cells = source.get("cells", [])
        if not isinstance(cells, list):
            cells = []
        normalized = []
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            cell_type = str(cell.get("cell_type", "code"))
            normalized.append(
                {
                    "cell_type": cell_type,
                    "metadata": copy.deepcopy(cell.get("metadata", {}))
                    if isinstance(cell.get("metadata"), dict)
                    else {},
                    "source": str(cell.get("source", "")),
                    **(
                        {
                            "execution_count": None,
                            "outputs": copy.deepcopy(cell.get("outputs", []))
                            if isinstance(cell.get("outputs"), list)
                            else [],
                        }
                        if cell_type == "code"
                        else {}
                    ),
                }
            )
        return {
            "cells": normalized,
            "metadata": copy.deepcopy(source.get("metadata", {}))
            if isinstance(source.get("metadata"), dict)
            else {},
            "nbformat": int(source.get("nbformat", 4)),
            "nbformat_minor": int(source.get("nbformat_minor", 5)),
        }

    def put_file(self, path: str, content: str) -> dict[str, Any]:
        self._ensure_parent_dir(path)
        self._remember_opened_path(path)
        return self._request_json(
            "PUT",
            f"/contents/{self._quote_path(path)}",
            {"type": "file", "format": "text", "content": content},
        )

    def put_notebook(self, path: str, notebook: dict[str, Any]) -> dict[str, Any]:
        self._ensure_parent_dir(path)
        self._remember_opened_path(path)
        return self._request_json(
            "PUT",
            f"/contents/{self._quote_path(path)}",
            {"type": "notebook", "format": "json", "content": notebook},
        )

    def _ensure_parent_dir(self, path: str) -> None:
        target = (self.root_dir / path.strip("/")).resolve()
        root = self.root_dir.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"JupyterLab path escapes runtime root: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)

    def get_content(self, path: str, *, content: bool = True) -> dict[str, Any]:
        query = "?content=1" if content else "?content=0"
        return self._request_json("GET", f"/contents/{self._quote_path(path)}{query}")

    def get_file_text(self, path: str) -> str:
        model = self.get_content(path)
        content = model.get("content")
        return content if isinstance(content, str) else ""

    def notebook_outputs(self, path: str) -> list[str]:
        model = self.get_content(path)
        content = model.get("content") if isinstance(model.get("content"), dict) else {}
        outputs: list[str] = []
        for cell in content.get("cells", []) if isinstance(content, dict) else []:
            if not isinstance(cell, dict):
                continue
            for output in (
                cell.get("outputs", []) if isinstance(cell.get("outputs"), list) else []
            ):
                if not isinstance(output, dict):
                    continue
                text = output.get("text")
                if isinstance(text, list):
                    outputs.append("".join(str(item) for item in text))
                elif isinstance(text, str):
                    outputs.append(text)
                data = output.get("data")
                if isinstance(data, dict):
                    plain = data.get("text/plain")
                    if isinstance(plain, list):
                        outputs.append("".join(str(item) for item in plain))
                    elif isinstance(plain, str):
                        outputs.append(plain)
        return outputs

    def save_workspace(self, workspace: str, paths: list[str]) -> dict[str, Any]:
        payload = self._workspace_model(workspace, paths)
        self._request_workspace_json(
            "PUT", f"/workspaces/{self._quote_path(workspace)}", payload
        )
        self._active_workspace = workspace
        for path in paths:
            self._remember_opened_path(path, workspace=workspace)
        return payload

    def get_workspace(self, workspace: str) -> dict[str, Any]:
        try:
            model = self._request_workspace_json(
                "GET", f"/workspaces/{self._quote_path(workspace)}"
            )
        except RuntimeError as error:
            if " 404:" not in str(error) and " 500:" not in str(error):
                raise
            return {"data": {}, "metadata": {"id": workspace}}
        if not isinstance(model.get("data"), dict):
            model["data"] = {}
        if not isinstance(model.get("metadata"), dict):
            model["metadata"] = {"id": workspace}
        return model

    def workspace_paths(self, workspace: str) -> set[str]:
        model = self.get_workspace(workspace)
        data = model.get("data") if isinstance(model.get("data"), dict) else {}
        paths = data.get("warptest:opened")
        found = (
            {str(path) for path in paths if isinstance(path, str)}
            if isinstance(paths, list)
            else set()
        )
        found.update(
            str(value)
            for value in self._find_workspace_paths(data)
            if isinstance(value, str)
        )
        return found

    def _workspace_model(self, workspace: str, paths: list[str]) -> dict[str, Any]:
        current = paths[0] if paths else None
        widgets = [self._workspace_widget_name(path) for path in paths]
        data: dict[str, Any] = {
            "layout-restorer:data": {
                "main": {
                    "dock": {"type": "tab-area", "currentIndex": 0, "widgets": widgets},
                    "current": self._workspace_widget_name(current)
                    if current
                    else None,
                },
                "down": {"size": 0, "widgets": []},
                "left": {
                    "collapsed": False,
                    "visible": True,
                    "current": "filebrowser",
                    "widgets": ["filebrowser"],
                },
                "right": {"collapsed": True, "visible": True, "widgets": []},
                "relativeSizes": [0.3, 0.7, 0],
                "top": {"simpleVisibility": True},
            },
            "warptest:opened": list(paths),
        }
        for path in paths:
            factory = "Notebook" if path.endswith(".ipynb") else "Editor"
            data[self._workspace_widget_name(path)] = {
                "data": {"path": path, "factory": factory}
            }
        return {"data": data, "metadata": {"id": workspace}}

    @staticmethod
    def _workspace_widget_name(path: str | None) -> str:
        if not path:
            return ""
        return ("notebook:" if path.endswith(".ipynb") else "editor:") + path

    def _remember_opened_path(self, path: str, *, workspace: str | None = None) -> None:
        normalized = path.strip("/")
        if not normalized:
            return
        if normalized not in self._last_opened_paths:
            self._last_opened_paths.append(normalized)
        if workspace:
            self._active_workspace = workspace

    def _find_workspace_paths(self, value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str):
                found.append(path)
            for child in value.values():
                found.extend(self._find_workspace_paths(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(self._find_workspace_paths(child))
        return found

    def run_notebook_cell(self, path: str, cell_index: int) -> str:
        model = self.get_content(path)
        notebook = (
            model.get("content") if isinstance(model.get("content"), dict) else {}
        )
        cells = notebook.get("cells") if isinstance(notebook.get("cells"), list) else []
        if cell_index < 0 or cell_index >= len(cells):
            raise IndexError(f"Notebook cell_index out of range: {cell_index}")
        cell = cells[cell_index]
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            raise ValueError(f"Notebook cell {cell_index} is not a code cell.")
        outputs, output_text, execution_count = self.execute_kernel_code(
            path, str(cell.get("source", ""))
        )
        cell["execution_count"] = execution_count
        cell["outputs"] = outputs
        self.put_notebook(path, notebook)
        return output_text

    def execute_kernel_code(
        self, path: str, code: str, kernel: str = "python3"
    ) -> tuple[list[dict[str, Any]], str, int]:
        session = self.start_session(path, kernel)
        session_id = str(session.get("id", ""))
        kernel_model = (
            session.get("kernel") if isinstance(session.get("kernel"), dict) else {}
        )
        kernel_id = kernel_model.get("id") if isinstance(kernel_model, dict) else None
        if not isinstance(kernel_id, str) or not kernel_id:
            raise RuntimeError(
                f"Jupyter session for {path} did not include a kernel id."
            )
        msg_id = uuid.uuid4().hex
        ws_url = (
            f"ws://127.0.0.1:{self.port}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels"
            f"?session_id={urllib.parse.quote(session_id, safe='')}"
        )
        timeout = float(os.environ.get("WARPTEST_JUPYTERLAB_CELL_TIMEOUT", "30"))
        outputs, output_text, execution_count = self._execute_kernel_code_via_websocket(
            ws_url,
            session_id,
            msg_id,
            code,
            timeout,
        )
        return outputs, output_text, execution_count

    def _execute_kernel_code_via_websocket(
        self,
        ws_url: str,
        session_id: str,
        msg_id: str,
        code: str,
        timeout: float,
    ) -> tuple[list[dict[str, Any]], str, int]:
        socket = self._open_kernel_websocket(ws_url, timeout)
        try:
            socket.send(
                json.dumps(self._kernel_execute_request(session_id, msg_id, code))
            )
            deadline = time.time() + timeout
            outputs: list[dict[str, Any]] = []
            text_fragments: list[str] = []
            execution_count = 0
            shell_done = False
            iopub_idle = False
            error_output: dict[str, Any] | None = None
            while time.time() < deadline and not (shell_done and iopub_idle):
                raw = socket.recv()
                message = (
                    json.loads(raw)
                    if isinstance(raw, str)
                    else json.loads(raw.decode("utf-8"))
                )
                parent = (
                    message.get("parent_header")
                    if isinstance(message.get("parent_header"), dict)
                    else {}
                )
                if parent.get("msg_id") != msg_id:
                    continue
                channel = message.get("channel")
                msg_type = message.get("msg_type") or (message.get("header") or {}).get(
                    "msg_type"
                )
                content = (
                    message.get("content")
                    if isinstance(message.get("content"), dict)
                    else {}
                )
                if channel == "shell" and msg_type == "execute_reply":
                    shell_done = True
                    if isinstance(content.get("execution_count"), int):
                        execution_count = content["execution_count"]
                    if content.get("status") == "error" and error_output is None:
                        error_output = self._kernel_error_output(content)
                    continue
                if channel != "iopub":
                    continue
                if msg_type == "status" and content.get("execution_state") == "idle":
                    iopub_idle = True
                elif msg_type == "execute_input" and isinstance(
                    content.get("execution_count"), int
                ):
                    execution_count = content["execution_count"]
                elif msg_type == "stream":
                    text = content.get("text", "")
                    text_fragments.append(
                        "".join(str(item) for item in text)
                        if isinstance(text, list)
                        else str(text)
                    )
                    outputs.append(
                        {
                            "output_type": "stream",
                            "name": str(content.get("name", "stdout")),
                            "text": text,
                        }
                    )
                elif msg_type == "execute_result":
                    output = {
                        "output_type": "execute_result",
                        "execution_count": content.get("execution_count"),
                        "data": copy.deepcopy(content.get("data", {}))
                        if isinstance(content.get("data"), dict)
                        else {},
                        "metadata": copy.deepcopy(content.get("metadata", {}))
                        if isinstance(content.get("metadata"), dict)
                        else {},
                    }
                    outputs.append(output)
                    text_fragments.extend(self._plain_text_from_output(output))
                elif msg_type == "display_data":
                    output = {
                        "output_type": "display_data",
                        "data": copy.deepcopy(content.get("data", {}))
                        if isinstance(content.get("data"), dict)
                        else {},
                        "metadata": copy.deepcopy(content.get("metadata", {}))
                        if isinstance(content.get("metadata"), dict)
                        else {},
                    }
                    outputs.append(output)
                    text_fragments.extend(self._plain_text_from_output(output))
                elif msg_type == "error":
                    error_output = self._kernel_error_output(content)
                    outputs.append(error_output)
            if not (shell_done and iopub_idle):
                raise RuntimeError(
                    "Timed out waiting for Jupyter kernel execute_reply/idle messages."
                )
            if error_output is not None:
                raise RuntimeError(
                    f"Jupyter kernel execution failed: {error_output.get('ename')}: {error_output.get('evalue')}"
                )
            return outputs, "".join(text_fragments), execution_count or 1
        finally:
            socket.close()

    def _open_kernel_websocket(self, ws_url: str, timeout: float):
        try:
            import websocket
        except ImportError as error:
            raise RuntimeError(
                "websocket-client is required for Jupyter kernel execution. "
                "Install the WarpTest package dependencies before running JupyterLab notebook checkpoints."
            ) from error
        return websocket.create_connection(ws_url, timeout=timeout)

    @staticmethod
    def _kernel_execute_request(
        session_id: str, msg_id: str, code: str
    ) -> dict[str, Any]:
        return {
            "header": {
                "msg_id": msg_id,
                "username": "warptest",
                "session": session_id,
                "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }

    @staticmethod
    def _plain_text_from_output(output: dict[str, Any]) -> list[str]:
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        plain = data.get("text/plain")
        if isinstance(plain, list):
            return ["".join(str(item) for item in plain)]
        if isinstance(plain, str):
            return [plain]
        return []

    @staticmethod
    def _kernel_error_output(content: dict[str, Any]) -> dict[str, Any]:
        return {
            "output_type": "error",
            "ename": str(content.get("ename", "")),
            "evalue": str(content.get("evalue", "")),
            "traceback": copy.deepcopy(content.get("traceback", []))
            if isinstance(content.get("traceback"), list)
            else [],
        }

    def start_session(self, path: str, kernel: str) -> dict[str, Any]:
        existing = self._sessions_by_key.get((path, kernel))
        if existing:
            return existing
        payload = {
            "path": path,
            "type": "notebook",
            "name": Path(path).name,
            "kernel": {"name": kernel},
        }
        session = self._request_json("POST", "/sessions", payload)
        session_id = session.get("id")
        if isinstance(session_id, str):
            self.sessions.append(session_id)
            self._sessions_by_key[(path, kernel)] = session
            self._remember_opened_path(path)
        return session

    # C1 deliberately keeps target restoration separate from Phase-B actions.  These
    # methods never call run_checkpoint(), and therefore never execute spec.actions.
    def start_c1_session(
        self, *, require_headed: bool = True
    ) -> "JupyterLabRuntimeAdapter":
        if require_headed and self.headless:
            raise RuntimeError(
                "Formal JupyterLab C1 requires a headed Playwright browser."
            )
        if self.server is None:
            self.start_server()
        elif self.server.poll() is not None:
            raise RuntimeErrorWithLog(
                f"JupyterLab C1 server exited with code {self.server.returncode}. "
                f"See {self.server_log}. Log tail:\n{self._log_tail()}"
            )
        return self

    def reset_c1_state(self) -> dict[str, Any]:
        """Reset public Jupyter state before restoring a C1 target.

        Browser-created kernels are included by querying /api/sessions instead of
        relying only on sessions that this adapter started itself.  Any cleanup
        failure aborts the restore rather than leaking a prior candidate's state.
        """

        self.start_c1_session(require_headed=False)
        self._close_browser()

        session_models = self._request_json("GET", "/sessions")
        if not isinstance(session_models, list):
            raise RuntimeError(
                "Jupyter /api/sessions did not return a list during C1 reset"
            )
        for session in session_models:
            session_id = session.get("id") if isinstance(session, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError(
                    "Jupyter C1 reset observed a session without a valid id"
                )
            self._request_json(
                "DELETE", f"/sessions/{urllib.parse.quote(session_id, safe='')}"
            )

        root_model = self.get_content("")
        entries = root_model.get("content") if isinstance(root_model, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError(
                "Jupyter Contents API did not return a root listing during C1 reset"
            )
        removed: list[str] = []
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path:
                raise RuntimeError(
                    "Jupyter C1 reset observed a root entry without a valid path"
                )
            self._request_json("DELETE", f"/contents/{self._quote_path(path)}")
            removed.append(path)

        remaining_model = self.get_content("")
        remaining = (
            remaining_model.get("content")
            if isinstance(remaining_model, dict)
            else None
        )
        if not isinstance(remaining, list) or remaining:
            raise RuntimeError(
                f"Jupyter C1 reset left residual root contents: {remaining!r}"
            )

        if self.workspaces_dir.exists():
            for child in self.workspaces_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        self.sessions.clear()
        self._sessions_by_key.clear()
        self._active_workspace = None
        self._last_opened_paths.clear()
        self.console_errors.clear()
        self.page_errors.clear()
        self._c1_spec = None
        self._c1_start_target_fingerprint = None
        return {"removed_paths": removed, "remaining_paths": []}

    def clean_c1_entry(self, *, require_headed: bool = True) -> dict[str, Any]:
        self.start_c1_session(require_headed=require_headed)
        reset = self.reset_c1_state()
        page = self._open_c1_page(self.app_url, target_kind=None)
        observation = {
            "root_paths": [],
            "ui": self._c1_ui_observation(page),
        }
        return {
            "ok": observation["ui"].get("lab_shell") is True,
            "detail": "JupyterLab clean entry is open with an empty isolated root.",
            "observation": observation,
            "artifacts": {"root_dir": str(self.root_dir), "reset": reset},
            "actions_executed": False,
        }

    def bind_c1_live_target(
        self,
        spec: dict[str, Any],
        spec_path: Path | None = None,
        *,
        require_headed: bool = True,
    ) -> dict[str, Any]:
        """Bind an Agent-S-produced live start without restoring any content.

        This is grader bookkeeping for the from-start arm.  It deliberately reads
        only the currently open Lab shell and records the hidden target for the
        subsequent semantic probe; file/notebook creation remains entirely the
        policy's responsibility.
        """

        self.start_c1_session(require_headed=require_headed)
        if spec.get("benchmark") not in {None, "JupyterLab", "jupyterlab"}:
            raise ValueError(
                f"JupyterLab C1 received benchmark={spec.get('benchmark')!r}"
            )
        target = spec.get("target")
        if not isinstance(target, dict) or target.get("kind") not in {
            "text_file",
            "notebook",
        }:
            raise ValueError(
                "JupyterLab live binding requires target.kind text_file or notebook"
            )
        page = self.page
        if page is None or page.__class__.__name__ == "_NoOpPage":
            raise RuntimeError("JupyterLab live binding has no browser page")
        observation = {"ui": self._c1_ui_observation(page)}
        if observation["ui"].get("lab_shell") is not True:
            raise RuntimeError("JupyterLab live binding did not observe the Lab shell")
        self._c1_spec = copy.deepcopy(spec)
        return {
            "ok": True,
            "detail": (
                "Agent-S-produced JupyterLab state is bound for read-only semantic "
                "grading; no target content was restored."
            ),
            "observation": observation,
            "artifacts": {
                "root_dir": str(self.root_dir),
                "spec_path": str(spec_path) if spec_path is not None else None,
            },
            "actions_executed": False,
            "app_state_mutated": False,
        }

    def restore_c1_target(
        self,
        spec: dict[str, Any],
        spec_path: Path | None = None,
        *,
        require_headed: bool = True,
    ) -> dict[str, Any]:
        if spec.get("benchmark") not in {None, "JupyterLab", "jupyterlab"}:
            raise ValueError(
                f"JupyterLab C1 received benchmark={spec.get('benchmark')!r}"
            )
        target = spec.get("target")
        if not isinstance(target, dict):
            raise ValueError("JupyterLab C1 spec.target must be a mapping")
        if target.get("kind") not in {"text_file", "notebook"}:
            raise ValueError(
                "JupyterLab C1 currently supports target.kind text_file or notebook only"
            )

        self.start_c1_session(require_headed=require_headed)
        reset = self.reset_c1_state()
        restore_detail, restore_data = self._restore_target(copy.deepcopy(target))
        validate_detail, validate_data = self._validate_target(target)
        url = self._screenshot_url()
        page = self._open_c1_page(url, target_kind=str(target.get("kind")))
        self._c1_spec = copy.deepcopy(spec)
        observation = self._c1_target_observation(target, page=page)
        return {
            "ok": True,
            "detail": "JupyterLab target restored and opened without executing Phase-B actions.",
            "observation": observation,
            "artifacts": {
                "root_dir": str(self.root_dir),
                "spec_path": str(spec_path) if spec_path is not None else None,
                "reset": reset,
                "restore": {"detail": restore_detail, "data": restore_data},
                "validate": {"detail": validate_detail, "data": validate_data},
                "ignored_scripted_action_count": len(spec.get("actions", []))
                if isinstance(spec.get("actions"), list)
                else 0,
            },
            "actions_executed": False,
        }

    def _open_c1_page(self, url: str, *, target_kind: str | None):
        page = self._ensure_browser_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        page.wait_for_selector(".jp-LabShell", timeout=20_000)
        self._dismiss_transient_dialogs(page)
        self._remove_news_prompt(page)
        if target_kind == "text_file":
            page.wait_for_selector(".jp-FileEditor", timeout=20_000)
        elif target_kind == "notebook":
            page.wait_for_selector(".jp-Notebook", timeout=20_000)
        page.wait_for_timeout(500)
        self._dismiss_transient_dialogs(page)
        self._remove_news_prompt(page)
        if not self.headless:
            page.bring_to_front()
        return page

    def _c1_ui_observation(self, page=None) -> dict[str, Any]:
        page = page or self.page
        if page is None:
            raise RuntimeError("JupyterLab C1 has no live browser page")
        value = page.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                lab_shell: Boolean(document.querySelector('.jp-LabShell')),
                file_editor: Boolean(document.querySelector('.jp-FileEditor')),
                notebook: Boolean(document.querySelector('.jp-Notebook')),
                dialog: Boolean(document.querySelector('.jp-Dialog'))
            })"""
        )
        if not isinstance(value, dict):
            raise RuntimeError("JupyterLab C1 browser observation was not a mapping")
        return value

    @staticmethod
    def _c1_source_text(value: Any) -> str:
        if isinstance(value, list):
            return "".join(str(item) for item in value)
        return value if isinstance(value, str) else str(value or "")

    @classmethod
    def _c1_start_cell_source(cls, value: Any) -> str:
        """Canonicalize only the notebook serializer's terminal line ending.

        Jupyter may round-trip a cell source with or without one final newline.
        That difference does not change the declared Start, but indentation,
        interior newlines, and every other character remain significant.
        """

        text = cls._c1_source_text(value)
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith(("\n", "\r")):
            return text[:-1]
        return text

    def _c1_target_observation(
        self, target: dict[str, Any], *, page=None
    ) -> dict[str, Any]:
        kind = str(target.get("kind") or "")
        path = self._target_path(target)
        model = self.get_content(path)
        observed: dict[str, Any] = {
            "kind": kind,
            "path": model.get("path"),
            "content_type": model.get("type"),
        }
        if kind == "text_file":
            observed["content"] = (
                model.get("content") if isinstance(model.get("content"), str) else ""
            )
        elif kind == "notebook":
            notebook = (
                model.get("content") if isinstance(model.get("content"), dict) else {}
            )
            cells: list[dict[str, Any]] = []
            for cell in notebook.get("cells", []) if isinstance(notebook, dict) else []:
                if not isinstance(cell, dict):
                    continue
                cells.append(
                    {
                        "cell_type": str(cell.get("cell_type") or ""),
                        "source": self._c1_start_cell_source(cell.get("source")),
                        "execution_count": cell.get("execution_count"),
                        "outputs": copy.deepcopy(cell.get("outputs", []))
                        if isinstance(cell.get("outputs"), list)
                        else [],
                    }
                )
            observed["cells"] = cells
            observed["output_text"] = self.notebook_outputs(path)
        observed["ui"] = self._c1_ui_observation(page)
        return observed

    @staticmethod
    def _c1_fingerprint(value: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()

    def grade_c1_start(self, spec: dict[str, Any]) -> dict[str, Any]:
        if self._c1_spec is None:
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "JupyterLab target-only restore has not run.",
                "observation": {},
            }
        target = spec.get("target")
        if not isinstance(target, dict):
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "spec.target is missing",
                "observation": {},
            }
        try:
            observation = self._c1_target_observation(target)
            ui = (
                observation.get("ui") if isinstance(observation.get("ui"), dict) else {}
            )
            kind = target.get("kind")
            if kind == "text_file":
                semantic_ok = observation.get("content") == str(
                    target.get("content", "")
                )
                editor_ok = ui.get("file_editor") is True
            elif kind == "notebook":
                expected_model = self._notebook_model(target)
                expected_cells = [
                    {
                        "cell_type": str(cell.get("cell_type") or ""),
                        "source": self._c1_start_cell_source(cell.get("source")),
                        "execution_count": cell.get("execution_count"),
                        "outputs": cell.get("outputs", []),
                    }
                    for cell in expected_model.get("cells", [])
                    if isinstance(cell, dict)
                ]
                semantic_ok = observation.get("cells") == expected_cells
                editor_ok = ui.get("notebook") is True
            else:
                raise ValueError(f"unsupported JupyterLab C1 target kind: {kind!r}")
            ok = bool(semantic_ok and editor_ok and ui.get("lab_shell") is True)
            if ok:
                target_observation = {
                    key: value for key, value in observation.items() if key != "ui"
                }
                self._c1_start_target_fingerprint = self._c1_fingerprint(
                    target_observation
                )
            return {
                "ok": ok,
                "status": "success" if ok else "validate_failed",
                "detail": "Live Jupyter target matches the requested start state."
                if ok
                else "Live Jupyter target or editor does not match the requested start state.",
                "observation": observation,
            }
        except Exception as error:
            try:
                ui = self._c1_ui_observation()
            except Exception:
                ui = None
            if isinstance(ui, dict) and ui.get("lab_shell") is True:
                return {
                    "ok": False,
                    "status": "validate_failed",
                    "detail": (
                        "Live JupyterLab remained available, but the requested "
                        f"start target could not be observed: {error}"
                    ),
                    "observation": {
                        "kind": target.get("kind"),
                        "path": target.get("path"),
                        "probe_error": str(error),
                        "ui": ui,
                    },
                }
            return {
                "ok": False,
                "status": "validate_error",
                "detail": str(error),
                "observation": {},
            }

    def grade_c1_goal(self, spec: dict[str, Any]) -> dict[str, Any]:
        assertions = semantic_assertions(spec)
        if not isinstance(assertions, list):
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "spec.assertions must be a list",
                "observation": {},
            }
        try:
            transition_requirement = _jupyter_changed_from_start_requirement(spec)
        except ValueError as error:
            return {
                "ok": False,
                "status": "validate_error",
                "detail": str(error),
                "observation": {},
            }
        meaningful_types = transition_requirement.get("meaningful_assertions")
        if not isinstance(meaningful_types, list):
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "JupyterLab changed-from-start requirement is malformed.",
                "observation": {},
            }
        meaningful = [
            assertion
            for assertion in assertions
            if isinstance(assertion, dict) and assertion.get("type") in meaningful_types
        ]
        if not meaningful:
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "JupyterLab C1 goal needs a live content or notebook-output assertion.",
                "observation": {},
            }
        if self._c1_start_target_fingerprint is None:
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "JupyterLab start semantics were not proven before goal grading.",
                "observation": {},
            }
        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        c1 = spec.get("c1") if isinstance(spec.get("c1"), dict) else {}
        timeout_ms = c1.get("semantic_timeout_ms", 20_000)
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms <= 0
        ):
            return {
                "ok": False,
                "status": "validate_error",
                "detail": "c1.semantic_timeout_ms must be a positive integer",
                "observation": {},
            }

        deadline = time.monotonic() + timeout_ms / 1000.0
        last_observation: dict[str, Any] = {}
        last_checks: list[dict[str, Any]] = []
        while True:
            checks: list[dict[str, Any]] = []
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    checks.append(
                        {"status": "failure", "detail": "assertion is not a mapping"}
                    )
                    continue
                try:
                    detail, data = self._run_assertion(assertion)
                    checks.append(
                        {
                            "type": assertion.get("type"),
                            "status": "success",
                            "detail": detail,
                            "data": data,
                        }
                    )
                except Exception as error:
                    checks.append(
                        {
                            "type": assertion.get("type"),
                            "status": "failure",
                            "detail": str(error),
                            "data": {},
                        }
                    )
            try:
                target_observation = self._c1_target_observation(target)
                semantic_target = {
                    key: value
                    for key, value in target_observation.items()
                    if key != "ui"
                }
                changed = (
                    self._c1_fingerprint(semantic_target)
                    != self._c1_start_target_fingerprint
                )
                changed_ok = changed == transition_requirement.get("expected")
                last_observation = {
                    "target": target_observation,
                    "checks": checks,
                    "changed_from_start": changed,
                }
            except Exception as error:
                return {
                    "ok": False,
                    "status": "validate_error",
                    "detail": str(error),
                    "observation": {},
                }
            last_checks = checks
            if changed_ok and all(check.get("status") == "success" for check in checks):
                return {
                    "ok": True,
                    "status": "success",
                    "detail": "Live Jupyter contents/kernel state satisfies the goal assertions.",
                    "observation": last_observation,
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        failed = [
            str(check.get("detail") or check.get("type"))
            for check in last_checks
            if check.get("status") != "success"
        ]
        return {
            "ok": False,
            "status": "assert_failed",
            "detail": "JupyterLab live goal was not reached: "
            + ("; ".join(failed) if failed else "state did not change from start"),
            "observation": last_observation,
        }

    def apply_c1_state_transition(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Apply the small registered State-arm transition for a restored target.

        This is deliberately narrower than ``_run_action``.  Arm E may execute
        only an explicitly registered program against the restored target.
        Arbitrary action programs are rejected, and the live editor is reloaded
        before returning so visual and semantic evidence describe the same state.
        """

        if self._c1_spec is None:
            raise RuntimeError(
                "JupyterLab target-only restore must run before the State transition"
            )
        target = spec.get("target")
        restored_target = self._c1_spec.get("target")
        if not isinstance(target, dict) or target != restored_target:
            raise ValueError(
                "JupyterLab State transition target does not match the restored target"
            )
        actions = spec.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(
                "JupyterLab State transition requires a non-empty actions list"
            )
        if any(not isinstance(action, dict) for action in actions):
            raise ValueError("JupyterLab State transition actions must be mappings")

        kind = str(target.get("kind") or "")
        path = self._target_path(target)
        action_types = [str(action.get("type") or "") for action in actions]
        if kind == "text_file":
            allowed_programs = [
                ["jupyterlab_edit_text_file"],
                ["jupyterlab_open_file", "jupyterlab_edit_text_file"],
            ]
        elif kind == "notebook":
            allowed_programs = [
                ["jupyterlab_run_notebook_cell"],
                ["jupyterlab_open_file", "jupyterlab_run_notebook_cell"],
                [
                    "jupyterlab_open_file",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_edit_text_file",
                    "jupyterlab_set_workspace_layout",
                ],
                [
                    "jupyterlab_open_file",
                    "jupyterlab_edit_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_export_notebook_html",
                ],
                [
                    "jupyterlab_open_file",
                    "jupyterlab_edit_text_file",
                    "jupyterlab_edit_text_file",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_run_notebook_cell",
                    "jupyterlab_set_workspace_layout",
                ],
            ]
        else:
            raise ValueError(
                "JupyterLab State transition supports text_file or notebook targets only"
            )
        if action_types not in allowed_programs:
            raise ValueError(
                f"Unregistered JupyterLab State transition program: {action_types!r}"
            )

        target_action_types = {
            "jupyterlab_open_file",
            "jupyterlab_edit_notebook_cell",
            "jupyterlab_run_notebook_cell",
            "jupyterlab_export_notebook_html",
        }
        for action in actions:
            if (
                action.get("type") in target_action_types
                and self._action_path(action) != path
            ):
                raise ValueError(
                    f"JupyterLab State transition action path must remain {path!r}"
                )

        deep_program = len(actions) > 2
        if deep_program:
            target_cells = target.get("cells")
            if not isinstance(target_cells, list):
                raise ValueError("Registered notebook transition requires target.cells")
            expected_code_cells = [
                index
                for index, cell in enumerate(target_cells)
                if isinstance(cell, dict) and cell.get("cell_type", "code") == "code"
            ]
            run_cells = [
                int(action.get("cell_index", -1))
                for action in actions
                if action.get("type") == "jupyterlab_run_notebook_cell"
            ]
            if run_cells != expected_code_cells:
                raise ValueError(
                    "Registered notebook transition must run every code cell once in order"
                )

            edited_paths = {
                self._safe_relative_path(self._action_path(action))
                for action in actions
                if action.get("type") == "jupyterlab_edit_text_file"
            }
            workspace_actions = [
                action
                for action in actions
                if action.get("type") == "jupyterlab_set_workspace_layout"
            ]
            if workspace_actions:
                workspace_action = workspace_actions[0]
                workspace = workspace_action.get("workspace")
                raw_paths = workspace_action.get("paths")
                if not isinstance(workspace, str) or not workspace.strip():
                    raise ValueError(
                        "Registered workspace transition requires a workspace name"
                    )
                if not isinstance(raw_paths, list) or any(
                    not isinstance(item, str) for item in raw_paths
                ):
                    raise ValueError(
                        "Registered workspace transition requires string paths"
                    )
                workspace_paths = {self._safe_relative_path(item) for item in raw_paths}
                if self._safe_relative_path(path) not in workspace_paths:
                    raise ValueError(
                        "Registered workspace transition must include the notebook target"
                    )
                if not edited_paths.issubset(workspace_paths):
                    raise ValueError(
                        "Registered workspace transition must include every edited text file"
                    )

            export_actions = [
                action
                for action in actions
                if action.get("type") == "jupyterlab_export_notebook_html"
            ]
            if export_actions:
                output_path = self._safe_relative_path(
                    str(export_actions[0].get("output_path") or "")
                )
                if not output_path.endswith(".html"):
                    raise ValueError(
                        "Registered notebook export must write an HTML file"
                    )
                asserted_paths = {
                    self._safe_relative_path(self._assertion_path(assertion))
                    for assertion in semantic_assertions(spec) or []
                    if isinstance(assertion, dict)
                    and assertion.get("type") == "jupyterlab_file_exists"
                }
                if output_path not in asserted_paths:
                    raise ValueError(
                        "Registered notebook export output must have a file-exists assertion"
                    )

        executed: list[dict[str, Any]] = []
        for action in actions:
            detail, data = self._run_action(copy.deepcopy(action))
            executed.append(
                {"type": str(action.get("type") or ""), "detail": detail, "data": data}
            )

        page = self._open_c1_page(
            self._screenshot_url(preferred_path=path),
            target_kind=kind,
        )
        observation = self._c1_target_observation(target, page=page)
        return {
            "ok": True,
            "status": "success",
            "detail": "Registered JupyterLab State transition completed and the live editor was synchronized.",
            "observation": observation,
            "artifacts": {"executed_actions": executed},
            "actions_executed": True,
            "action_count": len(executed),
        }

    def screenshot_c1_live(self, name: str, full_page: bool = False) -> str:
        page = self.page
        if page is None:
            raise RuntimeError("JupyterLab C1 screenshot requires an open live page")
        if not self.headless:
            page.bring_to_front()
        output = self.report_dir / name
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=full_page)
        self._last_screenshot_metadata = {
            "live_source": "jupyterlab_c1_live_page",
            "app_url": page.url,
            "workspace": self._active_workspace,
            "opened_paths": list(self._last_opened_paths),
            "full_page": full_page,
        }
        return str(output)

    def screenshot(self, name: str, full_page: bool = False) -> str:
        """Capture the real JupyterLab UI after the API-backed checkpoint run."""

        page = self._ensure_browser_page()
        url = self._screenshot_url()
        output = self.report_dir / name
        output.parent.mkdir(parents=True, exist_ok=True)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        try:
            page.wait_for_selector(".jp-LabShell", timeout=15_000)
        except Exception as error:
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=1_000)
            except Exception:
                pass
            raise RuntimeError(
                f"JupyterLab UI did not load at {url}: {body[:500]}"
            ) from error
        self._dismiss_transient_dialogs(page)
        page.wait_for_timeout(750)
        self._dismiss_transient_dialogs(page)
        self._remove_news_prompt(page)
        page.wait_for_timeout(250)
        self._remove_news_prompt(page)
        page.screenshot(path=str(output), full_page=full_page)
        self._last_screenshot_metadata = {
            "live_source": "jupyterlab_browser_capture",
            "app_url": url,
            "workspace": self._active_workspace,
            "opened_paths": list(self._last_opened_paths),
        }
        return str(output)

    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)

    def _screenshot_url(self, *, preferred_path: str | None = None) -> str:
        preferred = (
            urllib.parse.quote(preferred_path.strip("/"), safe="/")
            if isinstance(preferred_path, str) and preferred_path.strip("/")
            else None
        )
        if self._active_workspace:
            workspace_url = (
                f"{self.app_url}/workspaces/"
                f"{urllib.parse.quote(self._active_workspace, safe='')}"
            )
            return f"{workspace_url}/tree/{preferred}" if preferred else workspace_url
        if preferred:
            return f"{self.app_url}/tree/{preferred}"
        if self._last_opened_paths:
            path = urllib.parse.quote(self._last_opened_paths[-1], safe="/")
            return f"{self.app_url}/tree/{path}"
        return self.app_url

    def _ensure_browser_page(self):
        if self.page is not None:
            return self.page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "playwright is required for JupyterLab visual screenshots. "
                "Install WarpTest dependencies before generating JupyterLab visual pairs."
            ) from error
        self._playwright = sync_playwright().start()
        executable = os.environ.get(
            "WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""
        ).strip()
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if executable:
            executable_path = Path(executable).expanduser().resolve()
            if not executable_path.is_file():
                raise RuntimeError(
                    "WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE is not a file: "
                    f"{executable_path}"
                )
            launch_kwargs["executable_path"] = str(executable_path)
        self.browser = self._playwright.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
        )
        self.page = self.context.new_page()
        self.page.on("request", self._record_browser_request)
        self.page.on("console", self._record_browser_console)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        return self.page

    def _record_browser_request(self, request: Any) -> None:
        """Record non-loopback browser requests made by the live Jupyter UI."""

        url = str(getattr(request, "url", ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in {"data", "blob", "file", "about"}:
            return
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return
        self.network_api_calls["attempted"].append(url)

    @staticmethod
    def _dismiss_transient_dialogs(page) -> None:
        selectors = [
            '.jp-Dialog button:has-text("No Kernel")',
            'button:has-text("No Kernel")',
            '.jp-Notification button:has-text("No")',
            'button:has-text("No")',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    locator.first.click(timeout=1_000)
                    page.wait_for_timeout(200)
            except Exception:
                pass

    @staticmethod
    def _remove_news_prompt(page) -> None:
        try:
            page.evaluate(
                """() => {
                    const needle = 'Would you like to get notified about official Jupyter news?';
                    for (const element of Array.from(document.querySelectorAll('body *'))) {
                        if (!element.textContent || !element.textContent.includes(needle)) {
                            continue;
                        }
                        let target = element;
                        while (
                            target.parentElement &&
                            target.parentElement !== document.body &&
                            !String(target.className || '').includes('jp-Notification')
                        ) {
                            target = target.parentElement;
                        }
                        target.remove();
                    }
                }"""
            )
        except Exception:
            pass

    def _record_browser_console(self, message) -> None:
        if message.type in {"error", "warning"}:
            self.console_errors.append(message.text)

    def _request_json(
        self, method: str, api_path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request_json_base(
            self.api_url, "Jupyter API", method, api_path, payload
        )

    def _request_workspace_json(
        self, method: str, api_path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return self._request_json_base(
                self.lab_api_url, "JupyterLab API", method, api_path, payload
            )
        except RuntimeError as error:
            # Older JupyterLab/Jupyter Server combinations exposed workspace
            # handlers directly below /api; prefer the Lab route but keep this
            # fallback for existing checkouts.
            if " 404:" not in str(error):
                raise
            return self._request_json_base(
                self.api_url, "Jupyter API", method, api_path, payload
            )

    def _request_json_base(
        self,
        base_url: str,
        label: str,
        method: str,
        api_path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = base_url + api_path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"{label} {method} {api_path} failed with {error.code}: {detail}"
            ) from error

    @staticmethod
    def _quote_path(path: str) -> str:
        return urllib.parse.quote(path.strip("/"), safe="/")

    def close(self) -> None:
        for session_id in list(self.sessions):
            try:
                self._request_json("DELETE", f"/sessions/{session_id}")
            except Exception:
                pass
        self.sessions.clear()
        self._sessions_by_key.clear()
        self._close_browser()
        self._stop_server()
        self._close_log()

    def _close_browser(self) -> None:
        for attr in ("page", "context", "browser"):
            obj = getattr(self, attr)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass
            setattr(self, attr, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _stop_server(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        self.server = None

    def _close_log(self) -> None:
        if self._server_log_handle:
            self._server_log_handle.close()
            self._server_log_handle = None

    def _log_tail(self) -> str:
        try:
            text = self.server_log.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        return "\n".join(text.splitlines()[-80:])

    def cleanup_runtime_root(self) -> None:
        if self.runtime_root.exists():
            shutil.rmtree(self.runtime_root)
