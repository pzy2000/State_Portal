"""Gate 8--13 real Edge closure for JupyterLab #18992.

The authoritative defect (issue #18991) makes notebook cells disappear: moving a
collapsed heading section down past a following section leaves the moved cells
hidden, because the notebook never recomputes visibility from the collapsed
heading structure after the move.

Both golden frames are produced on separate notebook instances with the
variant-independent Lumino visibility API, so the observed run is the only place
where the candidate decides which cells stay hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 18992
_EPOCH = "real-notebook-v1"
_NOTEBOOK_WIDGET = "packages/notebook/lib/widget.js"
_NOTEBOOK_INDEX = "packages/notebook/lib/index.js"
_CODEMIRROR_INDEX = "packages/codemirror/lib/index.js"
_NOTEBOOK_CSS = "packages/notebook/style/base.css"
_CELLS_CSS = "packages/cells/style/base.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
# Lumino ships the display rule for hidden widgets; without it a hidden cell
# still paints and the visual difference disappears.
_LUMINO_CSS = "node_modules/@lumino/widgets/style/index.css"
_MARKER = "C1_LAZARUS_18992_EDGE_OBSERVATION"
_MOVED_SOURCES = (
    "# Title",
    "first = 1",
    "second = 2",
    "## First section",
    "## Second section",
)
_ALL_VISIBLE = (False, False, False, False, False)
_DEFECTIVE_HIDDEN = (False, True, True, False, False)

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_18992_transition/jupyterlab/18992/current-v1",
    "candidate_gate57_18992_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18992/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


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
import {{ MarkdownCell }} from '@jupyterlab/cells';
import {{ RenderMimeRegistry, standardRendererFactories }} from '@jupyterlab/rendermime';
import {{ Widget }} from '@lumino/widgets';

const host = document.getElementById('notebook-host');

const headingOf = content => {{
  let level = 0;
  while (level < content.length && level < 6 && content[level] === '#') {{
    level += 1;
  }}
  if (level === 0 || ![' ', '\\t'].includes(content[level])) {{
    return null;
  }}
  return {{ level, text: content.slice(level).trim() }};
}};
const markdownParser = {{
  render: async content => {{
    const heading = headingOf(content);
    return heading
      ? `<h${{heading.level}}>${{heading.text}}</h${{heading.level}}>`
      : content;
  }},
  getHeadingTokens: async content =>
    content
      .split('\\n')
      .map((raw, line) => ({{ line, raw }}))
      .filter(token => headingOf(token.raw) !== null)
}};

const languages = new EditorLanguageRegistry();
const extensions = new EditorExtensionRegistry();
const editorFactoryService = new CodeMirrorEditorFactory({{ extensions, languages }});
const mimeTypeService = new CodeMirrorMimeTypeService(languages);
const rendermime = new RenderMimeRegistry({{
  initialFactories: standardRendererFactories,
  markdownParser
}});
const contentFactory = new Notebook.ContentFactory({{
  editorFactory: editorFactoryService.newInlineEditor
}});
const notebookConfig = {{
  ...StaticNotebook.defaultNotebookConfig,
  windowingMode: 'none'
}};

const content = {{
  cells: [
    {{ cell_type: 'markdown', source: '# Title', metadata: {{}} }},
    {{ cell_type: 'markdown', source: '## First section', metadata: {{}} }},
    {{ cell_type: 'code', source: 'first = 1', metadata: {{}}, outputs: [],
      execution_count: null }},
    {{ cell_type: 'code', source: 'second = 2', metadata: {{}}, outputs: [],
      execution_count: null }},
    {{ cell_type: 'markdown', source: '## Second section', metadata: {{}} }}
  ],
  metadata: {{}},
  nbformat: 4,
  nbformat_minor: 5
}};

let widget = null;

const build = async () => {{
  if (widget) {{
    widget.dispose();
  }}
  widget = new Notebook({{
    rendermime, contentFactory, mimeTypeService, notebookConfig
  }});
  widget.model = new NotebookModel();
  widget.model.fromJSON(content);
  Widget.attach(widget, host);
  await Promise.all(
    widget.widgets
      .filter(cell => cell instanceof MarkdownCell)
      .map(cell => cell.getHeadings())
  );
  NotebookActions.setHeadingCollapse(widget.widgets[1], true, widget);
}};

const state = () => ({{
  sources: widget.widgets.map(cell => cell.model.sharedModel.getSource()),
  hidden: widget.widgets.map(cell => cell.isHidden),
  heading_collapsed: widget.widgets.map(
    cell => (cell instanceof MarkdownCell ? cell.headingCollapsed : null)
  )
}});

window.__candidateReady = true;
window.__candidateBuild = () => build();
window.__candidateMove = () => {{ widget.moveCell(1, 3); }};
window.__candidateForceVisibility = hidden => {{
  widget.widgets.forEach((cell, index) => cell.setHidden(hidden[index]));
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
<meta charset="utf-8"><title>JupyterLab collapsed heading move replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _LUMINO_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _CELLS_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _NOTEBOOK_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#notebook-host{{position:absolute;left:0;top:0;width:700px;height:600px}}
</style>
<div id="notebook-host"></div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / "node_modules/playwright/index.js").as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const ALL_VISIBLE = {json.dumps(list(_ALL_VISIBLE))};
const DEFECTIVE_HIDDEN = {json.dumps(list(_DEFECTIVE_HIDDEN))};
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 700, height: 600 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  // Golden probe one: the moved notebook with every cell forced visible.
  await page.evaluate(() => window.__candidateBuild());
  await wait(500);
  await page.evaluate(() => window.__candidateMove());
  await page.evaluate(
    hidden => window.__candidateForceVisibility(hidden), ALL_VISIBLE
  );
  await wait(400);
  const golden_visible = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: the moved notebook with the defective cells forced hidden.
  await page.evaluate(() => window.__candidateBuild());
  await wait(500);
  await page.evaluate(() => window.__candidateMove());
  await page.evaluate(
    hidden => window.__candidateForceVisibility(hidden), DEFECTIVE_HIDDEN
  );
  await wait(400);
  const golden_hidden = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: the collapsed section before the move.
  await page.evaluate(() => window.__candidateBuild());
  await wait(500);
  const before = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: the candidate decides visibility after the move.
  await page.evaluate(() => window.__candidateMove());
  await wait(400);
  const after = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    golden_visible, golden_hidden, before, after,
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


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    after = dict(data.get("after") or {})
    return {
        "after_sources": list(after.get("sources") or ()),
        "after_hidden": list(after.get("hidden") or ()),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "before_state": dict(data.get("before") or {}),
        "after_state": dict(data.get("after") or {}),
        "golden_visible_state": dict(data.get("golden_visible") or {}),
        "golden_hidden_state": dict(data.get("golden_hidden") or {}),
        "real_notebook_cell_move": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    before = dict(payload.get("before_state") or {})
    return (
        list(before.get("hidden") or ()) == [False, False, True, True, False]
        and list(payload.get("golden_visible_state", {}).get("hidden") or ())
        == list(_ALL_VISIBLE)
        and list(payload.get("golden_hidden_state", {}).get("hidden") or ())
        == list(_DEFECTIVE_HIDDEN)
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_NOTEBOOK_WIDGET,),
    common_assets=(
        _CODEMIRROR_INDEX,
        _NOTEBOOK_CSS,
        _CELLS_CSS,
        _VARIABLES_CSS,
        _LUMINO_CSS,
    ),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "cells": [
            "# Title",
            "## First section",
            "first = 1",
            "second = 2",
            "## Second section",
        ],
        "collapsed_heading_index": 1,
        "hidden": [False, False, True, True, False],
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "build_a_notebook_with_two_heading_sections",
        "collapse_the_first_section_heading",
        "move_the_collapsed_heading_down_past_the_following_section",
        "read_cell_sources_and_rendered_visibility",
    ),
    authoritative_defect=(
        "moving a collapsed heading section down leaves the moved cells hidden, so "
        "they disappear from the notebook"
    ),
    expected_after={
        "fixed": {
            "after_sources": list(_MOVED_SOURCES),
            "after_hidden": list(_ALL_VISIBLE),
        },
        "buggy": {
            "after_sources": list(_MOVED_SOURCES),
            "after_hidden": list(_DEFECTIVE_HIDDEN),
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_visibility_api_on_separate_notebooks",
    actual_after_source="candidate_controlled_cell_visibility",
    global_cross_threshold=0.05,
)


__all__ = ["SPEC"]
