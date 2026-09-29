"""Gate 8--13 real Edge closure for JupyterLab #18579.

The authoritative defect (issue #11140) lets a cell marked non-editable be split
or merged, so read-only content is silently rewritten.  The observation splits a
non-editable cell in a real notebook and reads the resulting cell sources.

Both golden frames are built from explicit cell lists in the same browser session,
so neither depends on the candidate's guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 18579
_EPOCH = "real-readonly-split-v1"
_ACTIONS = "packages/notebook/lib/actions.js"
_NOTEBOOK_INDEX = "packages/notebook/lib/index.js"
_CODEMIRROR_INDEX = "packages/codemirror/lib/index.js"
_NOTEBOOK_CSS = "packages/notebook/style/base.css"
_CELLS_CSS = "packages/cells/style/base.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_LUMINO_CSS = "node_modules/@lumino/widgets/style/index.css"
_MARKER = "C1_LAZARUS_18579_EDGE_OBSERVATION"
_BASE_SOURCES = ("alpha = 1", "beta = 2")
_SPLIT_SOURCES = ("alpha", " = 1", "beta = 2")

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_18579_transition/jupyterlab/18579/current-v1",
    "candidate_gate57_18579_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18579/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _cells(
    sources: tuple[str, ...], *, editable: bool = False
) -> list[dict[str, object]]:
    return [
        {
            "cell_type": "code",
            "source": source,
            "metadata": {"editable": editable},
            "outputs": [],
            "execution_count": None,
        }
        for source in sources
    ]


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    notebook = (tree / _NOTEBOOK_INDEX).as_posix()
    codemirror = (tree / _CODEMIRROR_INDEX).as_posix()
    entry = f"""import {{
  Notebook, NotebookActions, NotebookModel, StaticNotebook
}} from {json.dumps(notebook)};
import {{
  CodeMirrorEditorFactory, CodeMirrorMimeTypeService,
  EditorExtensionRegistry, EditorLanguageRegistry
}} from {json.dumps(codemirror)};
import {{ RenderMimeRegistry, standardRendererFactories }} from '@jupyterlab/rendermime';
import {{ Widget }} from '@lumino/widgets';

const host = document.getElementById('notebook-host');
const languages = new EditorLanguageRegistry();
const extensions = new EditorExtensionRegistry();
const editorFactoryService = new CodeMirrorEditorFactory({{ extensions, languages }});
const mimeTypeService = new CodeMirrorMimeTypeService(languages);
const rendermime = new RenderMimeRegistry({{
  initialFactories: standardRendererFactories
}});
const contentFactory = new Notebook.ContentFactory({{
  editorFactory: editorFactoryService.newInlineEditor
}});
const notebookConfig = {{
  ...StaticNotebook.defaultNotebookConfig,
  windowingMode: 'none'
}};

let widget = null;

const build = (cells, activeIndex) => {{
  if (widget) {{
    widget.dispose();
  }}
  widget = new Notebook({{
    rendermime, contentFactory, mimeTypeService, notebookConfig
  }});
  widget.model = new NotebookModel();
  widget.model.fromJSON({{
    cells, metadata: {{}}, nbformat: 4, nbformat_minor: 5
  }});
  Widget.attach(widget, host);
  widget.activeCellIndex = activeIndex ?? 0;
}};

const state = () => ({{
  cell_count: widget.widgets.length,
  sources: widget.widgets.map(cell => cell.model.sharedModel.getSource()),
  editable: widget.widgets.map(cell => cell.model.getMetadata('editable'))
}});

window.__candidateReady = true;
window.__candidateBuild = (cells, activeIndex) => {{ build(cells, activeIndex); }};
window.__candidateSplit = () => {{
  widget.activeCell.editor.setCursorPosition({{ line: 0, column: 5 }});
  NotebookActions.splitCell(widget);
}};
window.__candidateState = () => state();
"""
    config = f"""module.exports = {{
  mode: 'production', target: 'web', devtool: false,
  context: {json.dumps(tree.as_posix())},
  entry: {json.dumps((run / "entry.js").as_posix())},
  output: {{ path: {json.dumps((run / "dist").as_posix())}, filename: 'bundle.js',
    clean: true }},
  resolve: {{
    modules: [{json.dumps((tree / "node_modules").as_posix())}], symlinks: false
  }},
  module: {{ rules: [
    {{ test: /\\.svg$/, type: 'asset/source' }},
    {{ test: /\\.(css|gif|ttf|eot|woff2?)$/, type: 'asset/source' }}
  ] }},
  optimization: {{ minimize: false }}
}};
"""
    html_document = f"""<!doctype html>
<meta charset="utf-8"><title>JupyterLab read-only split replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _LUMINO_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _CELLS_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _NOTEBOOK_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#notebook-host{{position:absolute;left:0;top:0;width:700px;height:420px}}
</style>
<div id="notebook-host"></div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / "node_modules/playwright/index.js").as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const BASE_CELLS = {json.dumps(_cells(_BASE_SOURCES))};
const EDITABLE_CELLS = {json.dumps(_cells(_BASE_SOURCES, editable=True))};
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 700, height: 420 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  // Golden probe one: the untouched read-only notebook.
  await page.evaluate(cells => window.__candidateBuild(cells), BASE_CELLS);
  await wait(500);
  const golden_intact = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: the same split performed on an editable notebook, which
  // both variants allow, so the frame never depends on the candidate guard.
  await page.evaluate(cells => window.__candidateBuild(cells), EDITABLE_CELLS);
  await wait(500);
  await page.evaluate(() => window.__candidateSplit());
  await wait(500);
  const golden_split = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: the read-only notebook before the split request.
  await page.evaluate(cells => window.__candidateBuild(cells), BASE_CELLS);
  await wait(500);
  const before = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: request a split on the non-editable active cell.
  await page.evaluate(() => window.__candidateSplit());
  await wait(500);
  const after = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    golden_intact, golden_split, before, after,
    browser_channel: 'msedge', browser_version: browser.version(),
    frames: {{ before: 'before.png', after: 'after.png',
      fixed_golden: 'fixed-golden.png', buggy_golden: 'buggy-golden.png' }}
  }}));
  await browser.close();
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    return {
        "entry.js": entry,
        "rspack.config.cjs": config,
        "index.html": html_document,
        "edge-observe.cjs": browser,
    }


_INTACT = {
    "cell_count": 2,
    "sources": list(_BASE_SOURCES),
    "editable": [False, False],
}
_SPLIT = {
    "cell_count": 3,
    "sources": list(_SPLIT_SOURCES),
    "editable": [True, True, True],
}


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    after = dict(data.get("after") or {})
    return {
        "after_cell_count": after.get("cell_count"),
        "after_sources": list(after.get("sources") or ()),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "before_state": dict(data.get("before") or {}),
        "after_state": dict(data.get("after") or {}),
        "golden_intact_state": dict(data.get("golden_intact") or {}),
        "golden_split_state": dict(data.get("golden_split") or {}),
        "real_split_request_on_read_only_cell": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        dict(payload.get("before_state") or {}) == _INTACT
        and dict(payload.get("golden_intact_state") or {}) == _INTACT
        and dict(payload.get("golden_split_state") or {}) == _SPLIT
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_ACTIONS,),
    common_assets=(
        _NOTEBOOK_INDEX,
        _CODEMIRROR_INDEX,
        _NOTEBOOK_CSS,
        _CELLS_CSS,
        _VARIABLES_CSS,
        _LUMINO_CSS,
    ),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "cells": list(_BASE_SOURCES),
        "editable": False,
        "cursor": {"line": 0, "column": 5},
        "state": _INTACT,
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "build_a_notebook_whose_cells_are_marked_non_editable",
        "place_the_cursor_inside_the_active_cell",
        "request_a_cell_split_through_notebook_actions",
        "read_the_cell_count_and_sources",
    ),
    authoritative_defect=(
        "a non-editable cell can still be split, so read-only content is rewritten"
    ),
    expected_after={
        "fixed": {
            "after_cell_count": 2,
            "after_sources": list(_BASE_SOURCES),
        },
        "buggy": {
            "after_cell_count": 3,
            "after_sources": list(_SPLIT_SOURCES),
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_explicit_cell_lists_in_the_same_session",
    actual_after_source="candidate_controlled_split_guard",
    global_cross_threshold=0.05,
)


__all__ = ["SPEC"]
