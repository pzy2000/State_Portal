"""Gate 8--13 real Edge closure for JupyterLab #19136.

The authoritative defect (issue #19114) appears when the activity bar sits at the
bottom: the shell dehydrates the right side area as collapsed even though the user
never collapsed it, so restoring the layout leaves the right sidebar with width
zero and its widgets unreachable.

The observation restores a second shell from the layout the candidate itself saved.
Both golden frames come from the same variant-independent ``rehydrate`` call with
an explicit collapsed flag, so the only variant-dependent input is the saved flag
that carries the defect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 19136
_EPOCH = "real-shell-v1"
_SHELL = "packages/application/lib/shell.js"
_APPLICATION_CSS = "packages/application/style/base.css"
_UI_CSS = "packages/ui-components/style/index.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_MARKER = "C1_LAZARUS_19136_EDGE_OBSERVATION"

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_19136_transition/jupyterlab/19136/current-v1",
    "candidate_gate57_19136_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/19136/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    shell = (tree / _SHELL).as_posix()
    entry = f"""import {{ LabShell }} from {json.dumps(shell)};
import {{ Widget }} from '@lumino/widgets';

const host = document.getElementById('shell-host');
const panel = id => {{
  const widget = new Widget();
  widget.id = id;
  widget.title.label = 'Property Inspector';
  widget.title.caption = 'Property Inspector';
  widget.node.textContent = 'Property Inspector';
  widget.node.style.padding = '8px';
  return widget;
}};

let saved = null;
let restored = null;

const buildSource = () => {{
  const shell = new LabShell({{ waitForRestore: false }});
  Widget.attach(shell, host);
  shell.updateConfig({{ activityBarPosition: 'bottom' }});
  shell.add(panel('property-inspector'), 'right');
  saved = shell.saveLayout().rightArea;
  shell.dispose();
}};

const restore = area => {{
  if (restored) {{
    restored.dispose();
  }}
  restored = new LabShell({{ waitForRestore: false }});
  Widget.attach(restored, host);
  restored.updateConfig({{ activityBarPosition: 'bottom' }});
  const widget = panel('property-inspector');
  restored.add(widget, 'right');
  restored._rightHandler.rehydrate({{ ...area, widgets: [widget] }});
}};

const measure = () => {{
  const handler = restored._rightHandler;
  const area = handler.area.node.getBoundingClientRect();
  return {{
    right_collapsed: restored.rightCollapsed,
    area_hidden: handler.area.isHidden,
    side_bar_hidden: handler.sideBar.isHidden,
    area_width_is_zero: area.width === 0
  }};
}};

window.__candidateReady = true;
window.__candidateBuildSource = () => {{ buildSource(); }};
window.__candidateSavedCollapsed = () => saved.collapsed;
window.__candidateRestoreExplicit = collapsed => {{
  restore({{ ...saved, collapsed }});
}};
window.__candidateRestoreSaved = () => {{ restore(saved); }};
window.__candidateMeasure = () => measure();
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
<meta charset="utf-8"><title>JupyterLab shell layout restore replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _UI_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _APPLICATION_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#shell-host{{position:absolute;inset:0}}
</style>
<div id="shell-host"></div>
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
    viewport: {{ width: 1000, height: 600 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  await page.evaluate(() => window.__candidateBuildSource());
  const saved_collapsed = await page.evaluate(
    () => window.__candidateSavedCollapsed()
  );

  // Golden probe one: restore with an explicit not-collapsed flag.
  await page.evaluate(() => window.__candidateRestoreExplicit(false));
  await wait(400);
  const golden_visible = await page.evaluate(() => window.__candidateMeasure());
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: restore with an explicit collapsed flag.
  await page.evaluate(() => window.__candidateRestoreExplicit(true));
  await wait(400);
  const golden_collapsed = await page.evaluate(() => window.__candidateMeasure());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: the restored shell before the candidate layout is applied.
  await page.evaluate(() => window.__candidateRestoreExplicit(false));
  await wait(400);
  const before = await page.evaluate(() => window.__candidateMeasure());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: restore from the layout the candidate itself saved.
  await page.evaluate(() => window.__candidateRestoreSaved());
  await wait(400);
  const after = await page.evaluate(() => window.__candidateMeasure());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    saved_collapsed, golden_visible, golden_collapsed, before, after,
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


# A restored side area reports ``rightCollapsed`` from the absent current title in
# both variants; the defect shows up as a hidden, zero-width area.
_VISIBLE = {
    "right_collapsed": True,
    "area_hidden": False,
    "side_bar_hidden": False,
    "area_width_is_zero": False,
}
_COLLAPSED = {
    "right_collapsed": True,
    "area_hidden": True,
    "side_bar_hidden": False,
    "area_width_is_zero": True,
}


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    after = dict(data.get("after") or {})
    return {
        "saved_collapsed": data.get("saved_collapsed"),
        "restored_right_collapsed": after.get("right_collapsed"),
        "restored_area_hidden": after.get("area_hidden"),
        "restored_side_bar_hidden": after.get("side_bar_hidden"),
        "restored_area_width_is_zero": after.get("area_width_is_zero"),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "saved_collapsed": data.get("saved_collapsed"),
        "before_state": dict(data.get("before") or {}),
        "after_state": dict(data.get("after") or {}),
        "golden_visible_state": dict(data.get("golden_visible") or {}),
        "golden_collapsed_state": dict(data.get("golden_collapsed") or {}),
        "real_layout_save_and_restore": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        dict(payload.get("before_state") or {}) == _VISIBLE
        and dict(payload.get("golden_visible_state") or {}) == _VISIBLE
        and dict(payload.get("golden_collapsed_state") or {}) == _COLLAPSED
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_SHELL,),
    common_assets=(_APPLICATION_CSS, _UI_CSS, _VARIABLES_CSS),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "activity_bar_position": "bottom",
        "right_area_widget": "property-inspector",
        "restored_state": _VISIBLE,
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "build_a_shell_with_a_horizontal_activity_bar_and_a_right_widget",
        "save_the_layout_with_the_candidate_shell",
        "restore_a_second_shell_with_an_explicit_not_collapsed_flag",
        "restore_a_second_shell_from_the_candidate_saved_layout",
        "measure_right_area_visibility_and_width",
    ),
    authoritative_defect=(
        "a horizontal activity bar makes the shell save the right side area as "
        "collapsed, so the restored right sidebar has width zero"
    ),
    expected_after={
        "fixed": {
            "saved_collapsed": False,
            "restored_right_collapsed": True,
            "restored_area_hidden": False,
            "restored_side_bar_hidden": False,
            "restored_area_width_is_zero": False,
        },
        "buggy": {
            "saved_collapsed": True,
            "restored_right_collapsed": True,
            "restored_area_hidden": True,
            "restored_side_bar_hidden": False,
            "restored_area_width_is_zero": True,
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_explicit_rehydrate_in_the_same_session",
    actual_after_source="candidate_saved_layout_restored_in_a_second_shell",
    global_cross_threshold=0.05,
)


__all__ = ["SPEC"]
