"""Offline replay of JupyterLab #17369's registered stream-merge observation.

The frozen Gate 11 frames for #17369 were captured in an in-app browser session
whose harness was never committed, so the task's checks had no way to be resolved
again.  This module restates the same registered contract as a candidate-local
Edge harness: the output area model starts with the registered first stdout chunk,
one more chunk is added, and the consolidated text is read back.

The expectations are the frozen Gate 10 oracle's own strings, so a re-run either
reproduces the admitted observation or the probe reports a mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


CHANGE = 17369
TREES = PurePosixPath("private/materialized/jupyterlab/17369/current-v1")
PRODUCTION_MODULE = "packages/outputarea/lib/model.js"
MARKER = "C1_LAZARUS_17369_PROBE_OBSERVATION"
FIRST_CHUNK = "The meaning of life is....\nSome people... "
SECOND_CHUNK = "More text."
EXPECTED_AFTER = {
    "fixed": {"consolidated_text": "The meaning of life is....\nSome people... More text."},
    "buggy": {"consolidated_text": "The meaning of life is....\nMore text.e... "},
}
REPLAY_STEPS = (
    "load_candidate_local_bundle",
    "restore_registered_output_area_start",
    "add_stdout_stream_chunk",
    "read_consolidated_output_text",
)


def write_harness(run: Path, tree: Path) -> Mapping[str, str]:
    """Return the candidate-local harness that replays the registered sequence."""

    model = (tree / PRODUCTION_MODULE).as_posix()
    entry = f"""import {{ OutputAreaModel }} from {json.dumps(model)};

const host = document.getElementById('output-host');
const streamChunk = text => ({{
  output_type: 'stream', name: 'stdout', text
}});
let model = null;

const consolidated = () => {{
  const outputs = model.toJSON();
  return outputs.length === 0 ? '' : String(outputs[0].text ?? '');
}};

const render = () => {{
  host.textContent = consolidated();
}};

window.__candidateReady = true;
window.__candidateStart = () => {{
  if (model) {{
    model.dispose();
  }}
  model = new OutputAreaModel();
  model.add(streamChunk({json.dumps(FIRST_CHUNK)}));
  render();
}};
window.__candidateAddChunk = () => {{
  model.add(streamChunk({json.dumps(SECOND_CHUNK)}));
  render();
}};
window.__candidateText = () => consolidated();
window.__candidateOutputCount = () => model.length;
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
    html_document = """<!doctype html>
<meta charset="utf-8"><title>JupyterLab output area stream merge replay</title>
<style>
html,body{margin:0;width:100%;height:100%;background:#fff}
#output-host{position:absolute;left:0;top:0;right:0;padding:8px;
  font:14px/1.4 "SFMono-Regular",Menlo,monospace;white-space:pre-wrap;color:#212121}
</style>
<div id="output-host"></div>
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
    viewport: {{ width: 640, height: 160 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  await page.evaluate(() => window.__candidateStart());
  await wait(300);
  const before_text = await page.evaluate(() => window.__candidateText());
  const before_output_count = await page.evaluate(
    () => window.__candidateOutputCount()
  );
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  await page.evaluate(() => window.__candidateAddChunk());
  await wait(300);
  const after_text = await page.evaluate(() => window.__candidateText());
  const after_output_count = await page.evaluate(
    () => window.__candidateOutputCount()
  );
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{MARKER}=' + JSON.stringify({{
    before_text, before_output_count, after_text, after_output_count,
    browser_channel: 'msedge', browser_version: browser.version(),
    frames: {{ before: 'before.png', after: 'after.png' }}
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


def semantic_state(data: Mapping[str, Any]) -> dict[str, Any]:
    """Read the one observable the registered oracle names."""

    return {"consolidated_text": data.get("after_text")}


__all__ = [
    "CHANGE",
    "EXPECTED_AFTER",
    "FIRST_CHUNK",
    "MARKER",
    "PRODUCTION_MODULE",
    "REPLAY_STEPS",
    "SECOND_CHUNK",
    "TREES",
    "semantic_state",
    "write_harness",
]
