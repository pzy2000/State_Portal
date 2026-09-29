"""Gate 8--13 real Edge closure for JupyterLab #18662.

The authoritative defect (issue #18660) is a falsy-default bug: the inline
completer ships ``maxLines: 0`` to mean "unlimited", but the ghost text widget
treated the zero as "no limit configured" and skipped the ``minLines``
reservation, so the settings that reserve space for inline completions had no
visible effect.

Both golden frames come from variant-independent configurations in the same
browser session: a non-zero ``maxLines`` reserves lines in either variant, and
``minLines: 0`` reserves nothing in either variant.  The observed run is the
shipped default combination that carries the defect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 18662
_EPOCH = "real-ghost-v1"
_GHOST = "packages/completer/lib/ghost.js"
_COMPLETER_INDEX = "packages/completer/lib/index.js"
_TESTUTILS = "packages/completer/lib/testutils.js"
_COMPLETER_CSS = "packages/completer/style/base.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_MARKER = "C1_LAZARUS_18662_EDGE_OBSERVATION"
_INSERT_TEXT = "short"
_RESERVED_TEXT = "short\n\n"

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_18662_transition/jupyterlab/18662/current-v1",
    "candidate_gate57_18662_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18662/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    completer = (tree / _COMPLETER_INDEX).as_posix()
    testutils = (tree / _TESTUTILS).as_posix()
    entry = f"""import {{ InlineCompleter }} from {json.dumps(completer)};
import {{ createEditorWidget }} from {json.dumps(testutils)};
import {{ Widget }} from '@lumino/widgets';
import {{ Signal }} from '@lumino/signaling';

const host = document.getElementById('editor-host');
const provider = {{
  name: 'An inline provider',
  identifier: 'inline-provider',
  fetch: async () => ({{ items: [] }})
}};

let editorWidget = null;
let completer = null;
let model = null;

const build = settings => {{
  if (completer) {{
    completer.dispose();
  }}
  if (editorWidget) {{
    editorWidget.dispose();
  }}
  editorWidget = createEditorWidget();
  editorWidget.model.sharedModel.setSource('alpha\\nbeta\\ngamma');
  model = new InlineCompleter.Model();
  model.cursor = {{ line: 0, column: 0 }};
  completer = new InlineCompleter({{ editor: editorWidget.editor, model }});
  Widget.attach(editorWidget, host);
  Widget.attach(completer, host);
  completer.configure({{ ...InlineCompleter.defaultSettings, ...settings }});
  model.setCompletions({{
    items: [
      {{
        insertText: {json.dumps(_INSERT_TEXT)},
        streaming: false,
        provider,
        stream: new Signal({{}})
      }}
    ]
  }});
}};

const ghost = () => editorWidget.editor.host.querySelector('.jp-GhostText');

window.__candidateReady = true;
window.__candidateBuild = settings => {{ build(settings); }};
window.__candidateGhost = () => {{
  const node = ghost();
  if (!node) {{
    return {{ present: false, text: null, editor_height: null }};
  }}
  const content = editorWidget.editor.host.querySelector('.cm-content');
  return {{
    present: true,
    text: node.textContent,
    editor_height: Math.round(content.getBoundingClientRect().height)
  }};
}};
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
<meta charset="utf-8"><title>JupyterLab inline completer reserved lines replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _COMPLETER_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#editor-host{{position:absolute;left:0;top:0;width:520px}}
</style>
<div id="editor-host"></div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / "node_modules/playwright/index.js").as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 560, height: 240 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  // Golden probe one: a non-zero maxLines reserves lines in either variant.
  await page.evaluate(() => window.__candidateBuild({{ minLines: 3, maxLines: 5 }}));
  await wait(400);
  const golden_reserved = await page.evaluate(() => window.__candidateGhost());
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: minLines zero reserves nothing in either variant.
  await page.evaluate(() => window.__candidateBuild({{ minLines: 0, maxLines: 5 }}));
  await wait(400);
  const golden_unreserved = await page.evaluate(() => window.__candidateGhost());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: the reserved configuration rebuilt once more.
  await page.evaluate(() => window.__candidateBuild({{ minLines: 3, maxLines: 5 }}));
  await wait(400);
  const before = await page.evaluate(() => window.__candidateGhost());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: the shipped unlimited maxLines default.
  await page.evaluate(() => window.__candidateBuild({{ minLines: 3, maxLines: 0 }}));
  await wait(400);
  const after = await page.evaluate(() => window.__candidateGhost());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    golden_reserved, golden_unreserved, before, after,
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


# The editor content box grows by the two reserved ghost lines.
_RESERVED = {"present": True, "text": _RESERVED_TEXT, "editor_height": 99}
_UNRESERVED = {"present": True, "text": _INSERT_TEXT, "editor_height": 63}


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    after = dict(data.get("after") or {})
    return {
        "ghost_present": after.get("present"),
        "ghost_text": after.get("text"),
        "editor_content_height": after.get("editor_height"),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "before_ghost": dict(data.get("before") or {}),
        "after_ghost": dict(data.get("after") or {}),
        "golden_reserved_ghost": dict(data.get("golden_reserved") or {}),
        "golden_unreserved_ghost": dict(data.get("golden_unreserved") or {}),
        "real_inline_completion_render": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        dict(payload.get("before_ghost") or {}) == _RESERVED
        and dict(payload.get("golden_reserved_ghost") or {}) == _RESERVED
        and dict(payload.get("golden_unreserved_ghost") or {}) == _UNRESERVED
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_GHOST,),
    common_assets=(_COMPLETER_INDEX, _TESTUTILS, _COMPLETER_CSS, _VARIABLES_CSS),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "settings": {"minLines": 3, "maxLines": 5},
        "insert_text": _INSERT_TEXT,
        "ghost": _RESERVED,
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "build_an_editor_with_an_inline_completer",
        "configure_reserved_lines_with_a_bounded_max_lines",
        "configure_reserved_lines_with_the_shipped_unlimited_max_lines",
        "read_ghost_text_and_rendered_line_count",
    ),
    authoritative_defect=(
        "the shipped unlimited maxLines default disables the minLines reservation, "
        "so no space is reserved for inline completions"
    ),
    expected_after={
        "fixed": {
            "ghost_present": True,
            "ghost_text": _RESERVED_TEXT,
            "editor_content_height": 99,
        },
        "buggy": {
            "ghost_present": True,
            "ghost_text": _INSERT_TEXT,
            "editor_content_height": 63,
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_settings_in_the_same_session",
    actual_after_source="candidate_controlled_ghost_text_layout",
    global_cross_threshold=0.01,
)


__all__ = ["SPEC"]
