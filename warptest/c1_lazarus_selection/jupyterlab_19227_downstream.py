"""Gate 8--13 real Edge closure for JupyterLab #19227.

The authoritative defect (issue #19226) is a state desynchronisation: when a
reactive toolbar item is removed, its entry stays in the internal position map, so
a later ``insertBefore`` places the new item in the wrong slot.  The rendered
toolbar therefore shows the factory items after the permanent close button instead
of before it.

Both golden frames are produced by variant-independent production calls in the same
browser session: ``insertBefore`` into a freshly built toolbar for the correct
order, and plain ``addItem`` in the defective order for the wrong one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 19227
_EPOCH = "real-toolbar-v1"
_TOOLBAR = "packages/ui-components/lib/components/toolbar.js"
_TOOLBAR_CSS = "packages/ui-components/style/toolbar.css"
_UI_CSS = "packages/ui-components/style/index.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_MARKER = "C1_LAZARUS_19227_EDGE_OBSERVATION"
_CORRECT_ORDER = ("spacer", "f1", "f2", "close", "toolbar-popup-opener")
_DEFECTIVE_ORDER = ("spacer", "close", "f2", "f1", "toolbar-popup-opener")

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_19227_transition/jupyterlab/19227/current-v1",
    "candidate_gate57_19227_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/19227/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    toolbar = (tree / _TOOLBAR).as_posix()
    entry = f"""import {{ ReactiveToolbar }} from {json.dumps(toolbar)};
import {{ Widget }} from '@lumino/widgets';

const label = name => {{
  const widget = new Widget();
  widget.node.textContent = name;
  widget.node.style.padding = '0 10px';
  return widget;
}};

const host = document.getElementById('toolbar-host');
let toolbar = null;
const factoryItems = {{}};

const build = plan => {{
  if (toolbar) {{
    toolbar.dispose();
  }}
  toolbar = new ReactiveToolbar();
  Widget.attach(toolbar, host);
  if (plan === 'insert-before') {{
    toolbar.addItem('spacer', label('spacer'));
    toolbar.addItem('close', label('close'));
    factoryItems.f1 = label('f1');
    factoryItems.f2 = label('f2');
    toolbar.insertBefore('close', 'f1', factoryItems.f1);
    toolbar.insertBefore('close', 'f2', factoryItems.f2);
  }} else {{
    toolbar.addItem('spacer', label('spacer'));
    toolbar.addItem('close', label('close'));
    toolbar.addItem('f2', label('f2'));
    toolbar.addItem('f1', label('f1'));
  }}
}};

window.__candidateReady = true;
window.__candidateBuild = plan => {{ build(plan); }};
window.__candidateRefreshFactoryItems = () => {{
  factoryItems.f1.dispose();
  factoryItems.f2.dispose();
  factoryItems.f1 = label('f1');
  factoryItems.f2 = label('f2');
  toolbar.insertBefore('close', 'f1', factoryItems.f1);
  toolbar.insertBefore('close', 'f2', factoryItems.f2);
}};
window.__candidateNames = () => Array.from(toolbar.names());
window.__candidateRenderedOrder = () =>
  Array.from(toolbar.node.querySelectorAll('[data-jp-item-name]'))
    .map(node => ({{
      name: node.dataset.jpItemName,
      x: node.getBoundingClientRect().x
    }}))
    .sort((left, right) => left.x - right.x)
    .map(entry => entry.name);
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
<meta charset="utf-8"><title>JupyterLab reactive toolbar order replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _UI_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _TOOLBAR_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#toolbar-host{{position:absolute;left:0;right:0;top:0}}
</style>
<div id="toolbar-host"></div>
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
    viewport: {{ width: 900, height: 200 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  // Golden probe one: the correct order built with insertBefore.
  await page.evaluate(() => window.__candidateBuild('insert-before'));
  await wait(400);
  const golden_correct_order = await page.evaluate(
    () => window.__candidateRenderedOrder()
  );
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: the defective order built with plain addItem calls.
  await page.evaluate(() => window.__candidateBuild('append'));
  await wait(400);
  const golden_defective_order = await page.evaluate(
    () => window.__candidateRenderedOrder()
  );
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Real observation: rebuild, then refresh the factory items in place.
  await page.evaluate(() => window.__candidateBuild('insert-before'));
  await wait(400);
  const before_names = await page.evaluate(() => window.__candidateNames());
  const before_order = await page.evaluate(() => window.__candidateRenderedOrder());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});
  await page.evaluate(() => window.__candidateRefreshFactoryItems());
  await wait(400);
  const after_names = await page.evaluate(() => window.__candidateNames());
  const after_order = await page.evaluate(() => window.__candidateRenderedOrder());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    golden_correct_order, golden_defective_order,
    before_names, before_order, after_names, after_order,
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
    return {
        "after_item_names": list(data.get("after_names") or ()),
        "after_rendered_order": list(data.get("after_order") or ()),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "before_item_names": list(data.get("before_names") or ()),
        "before_rendered_order": list(data.get("before_order") or ()),
        "after_item_names": list(data.get("after_names") or ()),
        "after_rendered_order": list(data.get("after_order") or ()),
        "golden_correct_order": list(data.get("golden_correct_order") or ()),
        "golden_defective_order": list(data.get("golden_defective_order") or ()),
        "real_widget_lifecycle": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        list(payload.get("before_item_names") or ()) == list(_CORRECT_ORDER)
        and list(payload.get("before_rendered_order") or ()) == list(_CORRECT_ORDER)
        and list(payload.get("golden_correct_order") or ()) == list(_CORRECT_ORDER)
        and list(payload.get("golden_defective_order") or ()) == list(_DEFECTIVE_ORDER)
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_TOOLBAR,),
    common_assets=(_TOOLBAR_CSS, _UI_CSS, _VARIABLES_CSS),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "item_names": list(_CORRECT_ORDER),
        "rendered_order": list(_CORRECT_ORDER),
        "factory_items": ["f1", "f2"],
        "permanent_items": ["spacer", "close"],
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "build_reactive_toolbar_with_permanent_items",
        "insert_factory_items_before_the_close_button",
        "dispose_the_factory_items",
        "insert_replacement_factory_items_before_the_close_button",
        "read_item_names_and_rendered_left_to_right_order",
    ),
    authoritative_defect=(
        "stale reactive toolbar positions place refreshed factory items after the "
        "permanent close button instead of before it"
    ),
    expected_after={
        "fixed": {
            "after_item_names": list(_CORRECT_ORDER),
            "after_rendered_order": list(_CORRECT_ORDER),
        },
        "buggy": {
            "after_item_names": list(_DEFECTIVE_ORDER),
            "after_rendered_order": list(_DEFECTIVE_ORDER),
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_production_calls_in_the_same_session",
    actual_after_source="candidate_controlled_toolbar_layout",
    global_cross_threshold=0.05,
)


__all__ = ["SPEC"]
