"""Typed generated-state adapters on candidate products, not registered task replays."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from .lazarus_p2p_only_spec import action_errors, state_errors
from .lazarus_p2p_only_runtime import JupyterOnlineSession


_DOCUMENT_BUILD_LOCK = threading.Lock()


class GeneratedJupyterSession(JupyterOnlineSession):
    benchmark = "jupyterlab"

    def __init__(self, *, document_cache: Path | None = None, **kwargs):
        super().__init__(**kwargs)
        self.document_cache = document_cache

    def select_runtime_tree(self, destination: Path) -> Path:
        from .c1_lazarus_selection.p2p_only_engine_capture import _cow_copy

        if self.document_cache is None:
            _cow_copy(self.tree, destination)
            return destination
        # Compiled product installations must outlive disposable P2P clones and
        # per-cell workspaces. They contain no generated state or actions.
        cache = self.document_cache
        candidate = cache / "candidate"
        with _DOCUMENT_BUILD_LOCK:
            marker = cache / "candidate-cloned.json"
            if candidate.exists():
                if not marker.is_file():
                    raise RuntimeError("incomplete cached candidate installation")
                return candidate
            cache.mkdir(parents=True, exist_ok=True)
            _cow_copy(self.tree, candidate)
            marker.write_text(json.dumps({"source": str(self.tree)}))
        return candidate

    def prepare_document(self, tree: Path, output: Path) -> None:
        from .lazarus_p2p_only_identity import file_sha256

        cache = self.document_cache
        if cache is None:
            prepare_component_document(tree, output)
            return
        with _DOCUMENT_BUILD_LOCK:
            manifest = cache / "files.json"
            if manifest.is_file():
                files = json.loads(manifest.read_text())["files"]
                if any(
                    not (cache / name).is_file() or file_sha256(cache / name) != sha
                    for name, sha in files.items()
                ):
                    raise RuntimeError("candidate component asset cache drift")
                for name in files:
                    destination = output / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cache / name, destination)
                return
            if not (cache / "candidate-cloned.json").is_file():
                raise RuntimeError("candidate installation was not prepared")
            prepare_component_document(tree, output)
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output / "document.html", cache / "document.html")
            shutil.copytree(output / "dist", cache / "dist")
            files = {
                str(path.relative_to(cache)): file_sha256(path)
                for path in [cache / "document.html", *(cache / "dist").rglob("*")]
                if path.is_file()
            }
            manifest.write_text(
                json.dumps(
                    {"candidate_root": str(tree), "files": files}, sort_keys=True
                )
            )

    def restore_generated_state(self, state: dict) -> dict:
        errors = state_errors(state, "jupyterlab")
        if errors:
            raise ValueError("; ".join(errors))
        if not self.start_complete():
            self.page.evaluate("window.__warptestOpenStart('generated_state')")
        self._task_frame().wait_for_function('typeof window.p2pRestore === "function"')
        return self._task_frame().evaluate("value => window.p2pRestore(value)", state)

    def export_state(self) -> dict:
        if not self.start_complete():
            return {"configured": False}
        return self._task_frame().evaluate("window.p2pSnapshot()")

    def observe_start(self) -> dict:
        if not self.start_complete():
            return {"visible": False, "state": {"configured": False}}
        return self._task_frame().evaluate("window.p2pObserve()")

    def execute_semantic(self, action: dict) -> dict:
        errors = action_errors(action, "jupyterlab")
        if errors:
            raise ValueError("; ".join(errors))
        return self._task_frame().evaluate("action => window.p2pAction(action)", action)

    def capture_evidence_frame(self, path: str | Path) -> str:
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._task_frame().locator("#surface").screenshot(path=str(destination))
        return str(destination)


def prepare_component_document(tree: Path, output: Path) -> None:
    """Bundle generic component capabilities against this candidate's modules."""
    source = Path(__file__).with_name("lazarus_p2p_only_components.js").read_text()
    source = source.replace("__CANDIDATE__", tree.as_posix())
    from scripts.capture_lazarus_p2p_only_frames import build_packages, bundle
    from .c1_lazarus_selection.p2p_only_frames import FrameSpec

    capabilities = FrameSpec(
        task_id="generic-components",
        build_packages=("packages/ui-components", "packages/debugger"),
        stylesheets=(),
        fixture_css="",
        body_html="",
        entry=lambda _: source,
    )
    build_packages(tree, capabilities)
    bundle(output, tree, capabilities)
    themes = {
        name: (tree / f"packages/{package}/style/variables.css").read_text()
        for name, package in (
            ("light", "theme-light-extension"),
            ("dark", "theme-dark-extension"),
            ("high_contrast", "theme-dark-high-contrast-extension"),
        )
    }
    css = "\n".join(
        (tree / f"packages/{name}/style/{file}").read_text()
        for name, file in (
            ("ui-components", "base.css"),
            ("debugger", "base.css"),
            ("shortcuts-extension", "base.css"),
            ("application", "buttons.css"),
        )
    )
    # Static geometry belongs to the public container; all text/model/theme state is explicit.
    layout = """body {margin:0} #surface {width:760px;height:420px;overflow:hidden}
.page {padding:24px} .strip {display:flex;align-items:center;gap:8px;background:#f0f0f0;border:1px solid #c0c0c0;padding:6px 8px;width:320px}
.readout {margin-top:10px;font:13px monospace;color:#202020}
.row {width:420px;padding:22px;background:var(--jp-layout-color1);color:var(--jp-ui-font-color0);font:24px monospace}
.row #line {padding:6px 14px}.panel {width:420px;padding:20px;background:var(--jp-layout-color1);color:var(--jp-ui-font-color0);font:22px monospace}
.marked {background:var(--jp-layout-color3)} .glyph-face {font-family:monospace}
[data-component=themed_button] button {font-size:34px;padding:6px 14px;border:2px solid #202020;background:#fff}
#panel-host {position:relative;width:320px}.overflow-panel {position:absolute;top:6px;left:0;width:300px;padding:22px 12px;background:#fff;border:1px solid #9e9e9e}
#tree-host {width:320px;border:1px solid #c0c0c0;padding:8px}.jp-DebuggerVariables-name {font:22px monospace;margin-right:16px}.jp-DebuggerVariables-detail {font:22px monospace;color:#1a5fb4} jp-tree-item {display:block;padding:4px 0}
#button-host {display:flex;padding:4px;background:#f0f0f0;border:1px solid #c0c0c0;width:120px}#button-host svg {width:40px;height:40px}
[data-component=themed_button] .page {display:flex;gap:16px}
#controls {font:12px sans-serif;padding:4px;background:#eee} textarea {width:600px;height:55px} input {width:100px}"""
    controls = """<section id="controls"><label>Explicit component state JSON <textarea id="state-json"></textarea></label><button id="apply-state">Apply state</button><span id="error"></span><br>
<label>Variable name <input id="variable-name"></label><label>Value <input id="variable-value"></label><button id="add-variable">Add variable</button>
<label>Selector <input id="selector"></label><label>Class <input id="class-name"></label><button id="apply-class">Apply class</button><br>
<label>Option value <input id="option-value"></label><label>Options (comma separated) <input id="options"></label><label>Button text <input id="button-text"></label><label>Button class <input id="button-class"></label><button id="append-button">Append button</button></section>"""
    from .lazarus_p2p_only_spec import public_contract
    import html

    controls += (
        "<details><summary>Component state and action reference</summary><pre>"
        + html.escape(json.dumps(public_contract("jupyterlab"), indent=2))
        + "</pre></details>"
    )
    doc = (
        '<!doctype html><meta charset="utf-8"><style>'
        + css
        + "\n"
        + layout
        + '</style><style id="candidate-theme"></style><main id="surface"></main>'
        + controls
    )
    doc += (
        "<script>window.p2pThemes="
        + json.dumps(themes).replace("</", "<\\/")
        + '</script><script src="/files/dist/bundle.js"></script>'
    )
    (output / "document.html").write_text(doc)


def validate_observed_start(task: dict, observation: dict) -> bool:
    """Coordinator-only semantic predicates from public preconditions; never a restorer."""
    state = observation.get("state", {})
    task_id = task["id"]
    if task["benchmark"] == "jupyterlab":
        # These predicates describe the public preconditions, not the test fixture's
        # exact parameter choices. Rendering remains independently graded.
        if state_errors(state, "jupyterlab"):
            return False
        expected = {
            "lzp-jupyterlab-17222-toolbar-button-pressed-toggle": (
                "toolbar_button",
                "light",
            ),
            "lzp-jupyterlab-18464-debugger-zero-variable-detail": (
                "variables",
                "light",
            ),
            "lzp-jupyterlab-18996-htmlselect-caret-offset": ("html_select", "light"),
            "lzp-jupyterlab-17153-shortcut-chip-contrast": ("shortcut_chip", "dark"),
            "lzp-jupyterlab-19091-themed-button-glyph-font": ("themed_button", "light"),
            "lzp-jupyterlab-17618-high-contrast-selection-layer": (
                "selection",
                "high_contrast",
            ),
        }
        if (state["component"], state["theme"]) != expected[task_id]:
            return False
        props = state["props"]
        if state["component"] == "variables":
            values = props["variables"]
            if (
                props["scope"] != "Locals"
                or len(values) != 1
                or values[0]["name"] != "total"
            ):
                return False
            try:
                if int(values[0]["value"]) == 0:
                    return False
            except (ValueError, TypeError):
                return False
        elif state["component"] == "toolbar_button":
            if props["pressed"] is not False:
                return False
        elif state["component"] == "html_select":
            if props["label"] != "More":
                return False
        else:
            expected_text = {
                "shortcut_chip": "Ctrl A",
                "themed_button": "Plain",
                "selection": "select me",
            }
            if props["text"] != expected_text[state["component"]]:
                return False
            if set(props["class_name"].split()) & {
                "jp-mod-selected-InputText",
                "marked",
                "glyph-face",
            }:
                return False
        return (
            observation.get("visible") is True
            and not observation.get("panel_open")
            and not observation.get("selected")
        )
    if task["benchmark"] == "luanti":
        ready = (
            state.get("tab") == "local"
            and observation.get("dialog") == "none"
            and observation.get("runtime_healthy") is True
        )
        if task_id == "lzp-luanti-16799-world-mods":
            # Persisted world.mt files are not proof that the candidate UI can
            # see a world (the menu filters by installed/selected game).
            visible = observation.get("visible_worlds")
            return ready and isinstance(visible, list) and any(
                isinstance(world, dict)
                and bool(world.get("name"))
                and isinstance(world.get("mods"), list)
                and bool(world["mods"])
                for world in visible
            )
        return ready
    if task["benchmark"] == "openra":
        screen = (
            "editor"
            if task_id == "lzp-openra-22283-tileset-template-order"
            else "mainmenu"
        )
        ready = (
            state.get("screen") == screen
            and observation.get("runtime_healthy") is True
            and observation.get("goal_dialog_open") is False
        )
        if task_id == "lzp-openra-22283-tileset-template-order":
            widgets = observation.get("widgets")
            if not ready or not isinstance(widgets, list):
                return False
            tiles = [w for w in widgets if isinstance(w, dict) and w.get("id") == "TILE_WIDGETS"]
            # Require an observed closed tile palette, or a visible but empty
            # one, as specified in the public preconditions. A populated tile
            # list is already the transition target and cannot be Start.
            return bool(tiles) and all(type(w.get("visible")) is bool for w in tiles) and (
                not any(w["visible"] for w in tiles)
                or not any(
                    isinstance(w, dict) and w.get("id") == "TILE_PREVIEW"
                    and w.get("visible") is True for w in widgets
                )
            )
        return ready
    return False
