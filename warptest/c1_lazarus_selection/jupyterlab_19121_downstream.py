"""Gate 8--13 real Edge closure for JupyterLab #19121.

The authoritative defect (issue #19120) leaves the shortcuts settings UI stale:
saving user preferences while the UI is open does not refresh the displayed
shortcut list until the tab is closed and reopened, because the component never
subscribes to the settings ``changed`` signal.

Both golden frames come from variant-independent mounts in the same browser
session: mounting with the shortcut already present renders one row in either
variant, and mounting with no shortcuts renders none.  The observed run mounts
empty and then saves the preference externally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 19121
_EPOCH = "real-shortcuts-v1"
_SHORTCUT_UI = "packages/shortcuts-extension/lib/components/ShortcutUI.js"
_COMPONENTS_INDEX = "packages/shortcuts-extension/lib/components/index.js"
_PLUGIN_SCHEMA = "packages/shortcuts-extension/schema/shortcuts.json"
_SHORTCUTS_CSS = "packages/shortcuts-extension/style/base.css"
_UI_CSS = "packages/ui-components/style/index.css"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_MARKER = "C1_LAZARUS_19121_EDGE_OBSERVATION"
_COMMAND = "test:command"
_KEYS = "Ctrl Y"

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_19121_transition/jupyterlab/19121/current-v1",
    "candidate_gate57_19121_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/19121/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    components = (tree / _COMPONENTS_INDEX).as_posix()
    schema = (tree / _PLUGIN_SCHEMA).as_posix()
    entry = f"""import {{ ShortcutUI }} from {json.dumps(components)};
import pluginSchema from {json.dumps(schema)};
import {{ SettingRegistry, Settings }} from '@jupyterlab/settingregistry';
import {{ nullTranslator }} from '@jupyterlab/translation';
import {{ CommandRegistry }} from '@lumino/commands';
import {{ PromiseDelegate }} from '@lumino/coreutils';
import {{ Signal }} from '@lumino/signaling';
import * as React from 'react';
import {{ createRoot }} from 'react-dom/client';

const PLUGIN_ID = '@jupyterlab/shortcuts-extension:shortcuts';
const host = document.getElementById('shortcuts-host');

class DummySettings extends Settings {{
  constructor(options) {{
    super(options);
    this._options = options;
  }}
  get plugin() {{
    return this._options.plugin;
  }}
  async set(key, value) {{
    this._options.plugin.data.user[key] = value;
  }}
}}

let root = null;
let rootElement = null;
let shortcutUI = null;
let settings = null;
let data = null;

const mount = async initialShortcuts => {{
  if (root) {{
    root.unmount();
    rootElement.remove();
  }}
  data = {{
    composite: {{ shortcuts: [...initialShortcuts] }},
    user: {{ shortcuts: [] }}
  }};
  const plugin = {{
    data,
    id: PLUGIN_ID,
    raw: '{{}}',
    schema: pluginSchema,
    version: 'candidate'
  }};
  const connector = {{
    fetch: async () => plugin,
    list: async () => ({{ ids: [PLUGIN_ID], values: [plugin] }}),
    save: async () => undefined,
    remove: async () => undefined
  }};
  settings = new DummySettings({{
    registry: new SettingRegistry({{ connector }}),
    plugin
  }});
  const ready = new PromiseDelegate();
  const element = React.createElement(ShortcutUI, {{
    height: 600,
    width: 900,
    ref: instance => {{
      if (instance) {{
        shortcutUI = instance;
        ready.resolve();
      }}
    }},
    external: {{
      getSettings: async () => settings,
      translator: nullTranslator,
      commandRegistry: new CommandRegistry(),
      actionRequested: new Signal({{}})
    }}
  }});
  rootElement = document.createElement('div');
  host.appendChild(rootElement);
  root = createRoot(rootElement);
  root.render(element);
  await ready.promise;
}};

const state = () => {{
  const rows = Array.from(rootElement.querySelectorAll('.jp-Shortcuts-Row'));
  return {{
    list_length: shortcutUI.state.filteredShortcutList.length,
    first_command: shortcutUI.state.filteredShortcutList.length
      ? shortcutUI.state.filteredShortcutList[0].command
      : null,
    rendered_row_count: rows.length
  }};
}};

window.__candidateReady = true;
window.__candidateMount = shortcuts => mount(shortcuts);
window.__candidateSaveExternally = () => {{
  data.composite.shortcuts.push({{
    command: {json.dumps(_COMMAND)},
    keys: [{json.dumps(_KEYS)}],
    selector: 'body'
  }});
  settings.registry.pluginChanged.emit(PLUGIN_ID);
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
    {{ test: /\\.(gif|ttf|eot|woff2?)$/, type: 'asset/source' }},
    {{ test: /\\.css$/, type: 'asset/source' }}
  ] }},
  optimization: {{ minimize: false }}
}};
"""
    html_document = f"""<!doctype html>
<meta charset="utf-8"><title>JupyterLab shortcuts settings refresh replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _UI_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _SHORTCUTS_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
#shortcuts-host{{position:absolute;left:0;top:0;width:900px;height:600px}}
</style>
<div id="shortcuts-host"></div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / "node_modules/playwright/index.js").as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const SHORTCUT = {{
  command: {json.dumps(_COMMAND)}, keys: [{json.dumps(_KEYS)}], selector: 'body'
}};
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 900, height: 600 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);

  // Golden probe one: mounting with the saved shortcut already present.
  await page.evaluate(
    shortcut => window.__candidateMount([shortcut]), SHORTCUT
  );
  await wait(500);
  const golden_refreshed = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: mounting with no shortcuts at all.
  await page.evaluate(() => window.__candidateMount([]));
  await wait(500);
  const golden_stale = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: an open UI with no shortcuts yet.
  await page.evaluate(() => window.__candidateMount([]));
  await wait(500);
  const before = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: save the user preference externally while the UI is open.
  await page.evaluate(() => window.__candidateSaveExternally());
  await wait(800);
  const after = await page.evaluate(() => window.__candidateState());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    golden_refreshed, golden_stale, before, after,
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


_REFRESHED = {"list_length": 1, "first_command": _COMMAND, "rendered_row_count": 1}
_STALE = {"list_length": 0, "first_command": None, "rendered_row_count": 0}


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    after = dict(data.get("after") or {})
    return {
        "after_list_length": after.get("list_length"),
        "after_first_command": after.get("first_command"),
        "after_rendered_row_count": after.get("rendered_row_count"),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "before_state": dict(data.get("before") or {}),
        "after_state": dict(data.get("after") or {}),
        "golden_refreshed_state": dict(data.get("golden_refreshed") or {}),
        "golden_stale_state": dict(data.get("golden_stale") or {}),
        "real_external_preference_save": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        dict(payload.get("before_state") or {}) == _STALE
        and dict(payload.get("golden_refreshed_state") or {}) == _REFRESHED
        and dict(payload.get("golden_stale_state") or {}) == _STALE
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_SHORTCUT_UI,),
    common_assets=(
        _COMPONENTS_INDEX,
        _PLUGIN_SCHEMA,
        _SHORTCUTS_CSS,
        _UI_CSS,
        _VARIABLES_CSS,
    ),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "mounted_shortcuts": [],
        "state": _STALE,
        "external_save": {"command": _COMMAND, "keys": [_KEYS], "selector": "body"},
    },
    replay_steps=(
        "load_candidate_local_bundle",
        "mount_the_shortcuts_settings_ui_with_no_shortcuts",
        "save_the_user_preference_externally_and_emit_plugin_changed",
        "read_the_filtered_shortcut_list_and_the_rendered_rows",
    ),
    authoritative_defect=(
        "the open shortcuts settings UI keeps a stale shortcut list when user "
        "preferences are saved externally"
    ),
    expected_after={
        "fixed": {
            "after_list_length": 1,
            "after_first_command": _COMMAND,
            "after_rendered_row_count": 1,
        },
        "buggy": {
            "after_list_length": 0,
            "after_first_command": None,
            "after_rendered_row_count": 0,
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="variant_independent_mounts_in_the_same_session",
    actual_after_source="candidate_controlled_settings_subscription",
    global_cross_threshold=0.01,
    diagonal_tolerance=0.00005,
)


__all__ = ["SPEC"]
