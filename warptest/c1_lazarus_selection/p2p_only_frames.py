"""Four-frame capture for the P2P-only C1-Lazarus track.

Six passing control cells show that both products run.  They cannot show that
either is wrong, because this track has no fail-to-pass test.  The defect
differential therefore rests entirely on these frames, which is why each task
declares its own fixture here rather than reusing a generic page.

Each task's fixture renders the repaired surface twice per product: once at the
registered start, and once after the public reference steps.  The start has to
look the same on both products -- otherwise the after difference could not be
attributed to the steps -- so every fixture keeps the repaired surface out of the
start frame.

A fixture supplies the scaffolding a page needs (a container, a stand-in for a
third-party rule, a stub data model).  It never supplies the repaired code: that
always comes from the materialized tree, either as a compiled module or as the
stylesheet the tree ships.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

VIEWPORT = {"width": 760, "height": 420}
# These pages have no HUD or status band to discount, so the whole frame counts.
IGNORE_TOP_FRACTION = 0.0


@dataclass(frozen=True)
class FrameSpec:
    """One task's capture fixture."""

    task_id: str
    # Packages whose ``lib`` output must be rebuilt so a reverted ``src`` reaches
    # the browser.  A stylesheet-only repair needs none.
    build_packages: tuple[str, ...]
    # Stylesheets read from the materialized tree and inlined into the page.  For
    # a stylesheet-only repair this is where the candidate's decision enters.
    stylesheets: tuple[str, ...]
    # Extra CSS the fixture itself contributes: layout scaffolding, and any
    # stand-in for a third-party rule the real defect depended on.
    fixture_css: str
    body_html: str
    # Returns the bundle entry source, given the variant tree.  ``None`` means the
    # page needs no JavaScript at all.
    entry: Callable[[Path], str] | None = field(default=None, repr=False)
    after_min_changed_pixel_ratio: float = 0.001
    # A few-pixel shift on a small glyph moves too little of a full frame to
    # clear the differential floor.  Comparing a region that is the surface
    # under test keeps the ratio meaningful without distorting the render.
    clip: tuple[int, int, int, int] | None = None
    before_clip: tuple[int, int, int, int] | None = None
    notes: str = ""
    capture_kind: str = "playwright"
    ignore_top_fraction: float = 0.0
    engine_before_mode: str = ""
    engine_after_mode: str = ""
    engine_frame_size: tuple[int, int] = (1280, 720)
    hover_point: tuple[int, int] | None = None
    # Native tasks bind their visual differential to one task-specific runtime
    # observation.  Exact values are optional when the pair validator only needs
    # to prove that the observed ordered content differs across variants.
    engine_variant_state_key: str = ""
    engine_fixed_state_value: str = ""
    engine_buggy_state_value: str = ""

    @property
    def needs_bundle(self) -> bool:
        return self.entry is not None


def _lib(tree: Path, relative: str) -> str:
    return json.dumps((tree / PurePosixPath(relative)).as_posix())


_THEMED_SCAFFOLD = """
  body { margin: 0; background: #fff; font-size: 16px; }
  .page { padding: 24px; }
  .strip {
    display: flex; align-items: center; gap: 8px;
    background: #f0f0f0; border: 1px solid #c0c0c0;
    padding: 6px 8px; width: 320px;
  }
  .readout { margin-top: 10px; font: 13px monospace; color: #202020; }
"""


def _htmlselect_entry(tree: Path) -> str:
    """Mount an overflow panel holding the repaired selector."""

    return f"""import {{ HTMLSelect }} from {_lib(tree, "packages/ui-components/lib/index.js")};
import {{ Widget }} from '@lumino/widgets';
import * as React from 'react';
import {{ createRoot }} from 'react-dom/client';

const host = document.getElementById('panel-host');

window.__step = () => {{
  const panel = document.createElement('div');
  panel.className = 'overflow-panel';
  host.appendChild(panel);
  createRoot(panel).render(
    React.createElement(
      HTMLSelect,
      {{ value: 'code', onChange: () => undefined }},
      React.createElement('option', {{ value: 'code' }}, 'Code')
    )
  );
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}};

window.__state = () => {{
  const select = document.querySelector('.jp-HTMLSelect select');
  const caret = document.querySelector('.jp-HTMLSelect svg');
  if (!select || !caret) {{
    return {{ panel_open: Boolean(document.querySelector('.overflow-panel')) }};
  }}
  const box = select.getBoundingClientRect();
  const glyph = caret.getBoundingClientRect();
  return {{
    panel_open: true,
    caret_top_offset: Math.round(glyph.top - box.top),
    caret_right_inset: Math.round(box.right - glyph.right),
    caret_vertically_centred:
      Math.abs((glyph.top + glyph.bottom) / 2 - (box.top + box.bottom) / 2) <= 2
  }};
}};
window.__ready = true;
"""


def _variables_entry(tree: Path) -> str:
    """Render the repaired inspector tree over a stubbed variables model."""

    return f"""import {{ VariablesBodyTree }} from {_lib(tree, "packages/debugger/lib/panels/variables/tree.js")};
import {{ VariablesModel }} from {_lib(tree, "packages/debugger/lib/panels/variables/model.js")};
import {{ CommandRegistry }} from '@lumino/commands';
import {{ Widget }} from '@lumino/widgets';

const host = document.getElementById('tree-host');
const model = new VariablesModel();
// A stub stands in for the running session only so the tree can mount; the rows
// it paints come from the model below.
const service = {{
  inspectVariable: async () => [],
  getVariableDetails: async () => [],
  hasRichVariableRendering: false,
  model: {{ hasRichVariableRendering: false, callstack: {{ frame: null }} }}
}};
const entry = (name, value) => ({{
  name, value, type: 'int', variablesReference: 0, evaluateName: name
}});
const locals = variables => [{{ name: 'Locals', variables }}];

const widget = new VariablesBodyTree({{
  model, commands: new CommandRegistry(), service, translator: undefined
}});
Widget.attach(widget, host);
// The widget subscribes to the model in its constructor, so the start scope is
// published after it is attached rather than before.
model.scopes = locals([entry('total', '7')]);

window.__step = () => {{
  model.scopes = locals([entry('total', '7'), entry('count', '0')]);
  widget.update();
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}};

window.__state = () => ({{
  names: Array.from(document.querySelectorAll('.jp-DebuggerVariables-name')).map(
    node => node.textContent
  ),
  details: Array.from(document.querySelectorAll('.jp-DebuggerVariables-detail')).map(
    node => node.textContent
  )
}});
window.__ready = true;
"""


def _toolbar_entry(tree: Path) -> str:
    """Activate the repaired two-state button once."""

    return f"""import {{ ToolbarButton }} from {_lib(tree, "packages/ui-components/lib/index.js")};
import {{ LabIcon }} from {_lib(tree, "packages/ui-components/lib/index.js")};
import {{ Widget }} from '@lumino/widgets';

const svg = (name, body) => new LabIcon({{
  name: 'p2p-only:' + name,
  svgstr:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">' +
    body +
    '</svg>'
}});
const restIcon = svg('rest', '<circle cx="12" cy="12" r="9" fill="#202020"/>');
const engagedIcon = svg(
  'engaged',
  '<rect x="4" y="4" width="16" height="16" fill="none" stroke="#202020" stroke-width="3"/>'
);

const host = document.getElementById('button-host');
const readout = document.getElementById('readout');
const button = new ToolbarButton({{
  icon: restIcon,
  pressedIcon: engagedIcon,
  pressed: false,
  onClick: () => undefined,
  label: ''
}});
Widget.attach(button, host);
const paint = () => {{
  readout.textContent = 'engaged: ' + String(button.pressed);
}};
paint();

window.__step = () => {{
  button.node.querySelector('jp-button').click();
  button.update();
  paint();
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}};

window.__state = () => {{
  const element = button.node.querySelector('jp-button');
  return {{
    pressed: button.pressed,
    aria_pressed: element?.getAttribute('aria-pressed'),
    painted_glyph: element?.querySelector('svg')?.getAttribute('data-icon'),
    readout: readout.textContent
  }};
}};
window.__ready = true;
"""


def _notebook_entry(tree: Path) -> str:
    """Select text in the first cell, then activate the second."""

    return f"""import {{
  Notebook, NotebookModel, StaticNotebook
}} from {_lib(tree, "packages/notebook/lib/index.js")};
import {{
  CodeMirrorEditorFactory, CodeMirrorMimeTypeService,
  EditorExtensionRegistry, EditorLanguageRegistry
}} from {_lib(tree, "packages/codemirror/lib/index.js")};
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
  ...StaticNotebook.defaultNotebookConfig, windowingMode: 'none'
}};

const widget = new Notebook({{
  rendermime, contentFactory, mimeTypeService, notebookConfig
}});
widget.model = new NotebookModel();
widget.model.fromJSON({{
  cells: [
    {{ cell_type: 'code', source: 'alpha = 1', metadata: {{}}, outputs: [], execution_count: null }},
    {{ cell_type: 'code', source: 'beta = 2', metadata: {{}}, outputs: [], execution_count: null }}
  ],
  metadata: {{}}, nbformat: 4, nbformat_minor: 5
}});
Widget.attach(widget, host);
widget.activeCellIndex = 0;

window.__step = () => {{
  const first = widget.widgets[0];
  const editor = first.editor;
  editor.focus();
  editor.setSelection({{
    start: {{ line: 0, column: 0 }},
    end: {{ line: 0, column: 9 }}
  }});
  widget.activeCellIndex = 1;
  widget.update();
  return new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  );
}};

window.__state = () => ({{
  active_cell_index: widget.activeCellIndex,
  selection_ranges: widget.widgets.map(cell => {{
    const uuid = cell.editor?.uuid;
    const selections = uuid ? cell.model.selections.get(uuid) : undefined;
    return selections ? selections.length : 0;
  }}),
  painted_selection_layers: widget.widgets.map(
    cell => cell.node.querySelectorAll('.cm-selectionBackground').length
  )
}});
window.__ready = true;
"""


FRAME_SPECS: Mapping[str, FrameSpec] = {
    "lzp-jupyterlab-18996-htmlselect-caret-offset": FrameSpec(
        task_id="lzp-jupyterlab-18996-htmlselect-caret-offset",
        build_packages=("packages/ui-components",),
        stylesheets=(
            "packages/theme-light-extension/style/variables.css",
            "packages/ui-components/style/base.css",
        ),
        fixture_css=_THEMED_SCAFFOLD
        + """
  /* The fixture supplies the positioned ancestor a toolbar popup provides, so
     the candidate decides where the caret anchors. */
  #panel-host { position: relative; width: 320px; }
  .overflow-panel {
    position: absolute; top: 6px; left: 0;
    width: 300px; padding: 22px 12px;
    background: #fff; border: 1px solid #9e9e9e;
  }
""",
        body_html=(
            '<div class="page jp-ThemedContainer">'
            '<div class="strip"><span>Cell type</span><button>More</button></div>'
            '<div id="panel-host"></div>'
            "</div>"
        ),
        entry=_htmlselect_entry,
        clip=(16, 16, 340, 104),
        notes=(
            "The fixture provides the positioned popup ancestor; the caret offset "
            "and the stylesheet that anchors it both come from the tree."
        ),
    ),
    "lzp-jupyterlab-18464-debugger-zero-variable-detail": FrameSpec(
        task_id="lzp-jupyterlab-18464-debugger-zero-variable-detail",
        build_packages=("packages/debugger",),
        stylesheets=(
            "packages/theme-light-extension/style/variables.css",
            "packages/debugger/style/base.css",
        ),
        fixture_css=_THEMED_SCAFFOLD
        + """
  #tree-host { width: 320px; border: 1px solid #c0c0c0; padding: 8px; }
  .jp-DebuggerVariables-name { font: 22px monospace; margin-right: 16px; }
  .jp-DebuggerVariables-detail { font: 22px monospace; color: #1a5fb4; }
  jp-tree-item { display: block; padding: 4px 0; }
""",
        body_html='<div class="page jp-ThemedContainer"><div id="tree-host"></div></div>',
        entry=_variables_entry,
        clip=(16, 16, 344, 132),
        notes="The variables model is a fixture; the row markup comes from the tree.",
    ),
    "lzp-jupyterlab-17222-toolbar-button-pressed-toggle": FrameSpec(
        task_id="lzp-jupyterlab-17222-toolbar-button-pressed-toggle",
        build_packages=("packages/ui-components",),
        stylesheets=(
            "packages/theme-light-extension/style/variables.css",
            "packages/ui-components/style/base.css",
        ),
        fixture_css=_THEMED_SCAFFOLD
        + """
  #button-host { display: flex; padding: 4px; background: #f0f0f0;
    border: 1px solid #c0c0c0; width: 120px; }
  #button-host svg { width: 40px; height: 40px; }
""",
        body_html=(
            '<div class="page jp-ThemedContainer">'
            '<div id="button-host"></div>'
            '<div class="readout" id="readout"></div>'
            "</div>"
        ),
        entry=_toolbar_entry,
        notes=(
            "The two glyphs are fixture icons so the swap is legible; which glyph "
            "is painted after activation is the candidate's decision."
        ),
    ),
    "lzp-jupyterlab-17153-shortcut-chip-contrast": FrameSpec(
        task_id="lzp-jupyterlab-17153-shortcut-chip-contrast",
        build_packages=(),
        stylesheets=(
            "packages/theme-dark-extension/style/variables.css",
            "packages/shortcuts-extension/style/base.css",
        ),
        fixture_css="""
  body { margin: 0; background: #000; }
  .page { padding: 24px; }
  .row {
    width: 420px; padding: 22px;
    background: var(--jp-layout-color1);
    color: var(--jp-ui-font-color0);
    font: 24px monospace;
  }
  /* The chip's own rule comes from the tree; the fixture only gives it room and
     keeps it on screen, which the real panel does on row hover. */
  #line { padding: 6px 14px; }
""",
        body_html=(
            '<div class="page jp-ThemedContainer">'
            '<div class="row"><span id="line">Ctrl A</span></div>'
            "</div>"
        ),
        entry=None,
        after_min_changed_pixel_ratio=0.002,
        notes=(
            "The chip colour is read from the tree's palette and its stylesheet; "
            "the fixture only decides which label becomes a chip."
        ),
    ),
    "lzp-jupyterlab-19091-themed-button-glyph-font": FrameSpec(
        task_id="lzp-jupyterlab-19091-themed-button-glyph-font",
        build_packages=(),
        stylesheets=(
            "packages/theme-light-extension/style/variables.css",
            "packages/application/style/buttons.css",
        ),
        fixture_css="""
  body { margin: 0; background: #fff; }
  .page { padding: 24px; display: flex; gap: 16px; }
  button { font-size: 34px; padding: 6px 14px; border: 2px solid #202020;
    background: #fff; }
  /* Stand-in for the third-party glyph rule the repair had to stop overriding.
     Its specificity is (0,1,0), exactly as the real one, so the candidate's
     stylesheet decides which family wins. */
  .glyph-face { font-family: monospace; }
""",
        body_html=(
            '<div class="page jp-ThemedContainer" id="host">'
            "<button>Plain</button>"
            "</div>"
        ),
        entry=None,
        # A whole label repainted in another family moves a large share of pixels.
        after_min_changed_pixel_ratio=0.002,
        notes=(
            "The fixture contributes a (0,1,0) class rule in place of Font "
            "Awesome's; the container rule that may override it comes from the "
            "tree's own stylesheet."
        ),
    ),
    "lzp-jupyterlab-17618-high-contrast-selection-layer": FrameSpec(
        task_id="lzp-jupyterlab-17618-high-contrast-selection-layer",
        build_packages=(),
        stylesheets=("packages/theme-dark-high-contrast-extension/style/variables.css",),
        fixture_css="""
  body { margin: 0; background: #000; }
  .page { padding: 24px; }
  .panel {
    width: 420px; padding: 20px;
    background: var(--jp-layout-color1);
    color: var(--jp-ui-font-color0);
    font: 22px monospace;
  }
  /* The marked region is painted with the palette's own region colour, so the
     candidate's stylesheet decides whether it can be told apart. */
  .marked { background: var(--jp-layout-color3); }
""",
        body_html=(
            '<div class="page jp-ThemedContainer">'
            '<div class="panel"><span id="line">select me</span></div>'
            "</div>"
        ),
        entry=None,
        after_min_changed_pixel_ratio=0.002,
        notes=(
            "The region colour is read from the tree's palette; the fixture only "
            "decides where a region is drawn."
        ),
    ),
    "lzp-luanti-16679-unavailable-settings": FrameSpec(
        task_id="lzp-luanti-16679-unavailable-settings",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="luanti_engine",
        ignore_top_fraction=0.0,
        engine_before_mode="luanti-local",
        engine_after_mode="luanti-settings",
        after_min_changed_pixel_ratio=0.001,
        notes="After launch opens the settings dialog on a page with unmet requirements.",
    ),
    "lzp-luanti-16799-world-mods": FrameSpec(
        task_id="lzp-luanti-16799-world-mods",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="luanti_engine",
        ignore_top_fraction=0.0,
        engine_frame_size=(1920, 1080),
        engine_before_mode="luanti-local-world",
        engine_after_mode="luanti-configure-world",
        # The larger frame is required to show the fixed-size dialog at Retina
        # density.  Its extra canvas dilutes the same one-row text differential,
        # so bind the threshold to the full 1920x1080 frame rather than the old
        # clipped 1280x720 capture.
        after_min_changed_pixel_ratio=0.0005,
        notes="After launch shows the complete configure-world mods dialog of a world that has worldmods.",
    ),
    "lzp-luanti-17152-server-list-search-tooltip": FrameSpec(
        task_id="lzp-luanti-17152-server-list-search-tooltip",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="luanti_engine",
        ignore_top_fraction=0.0,
        engine_before_mode="luanti-local",
        engine_after_mode="luanti-online-tooltip",
        hover_point=(400, 165),
        after_min_changed_pixel_ratio=0.001,
        notes="After launch opens the online tab and paints the search-filter examples in a dropdown so escaping is visible without hover.",
    ),
    "lzp-openra-22476-load-game-faction-filter": FrameSpec(
        task_id="lzp-openra-22476-load-game-faction-filter",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="openra_engine",
        ignore_top_fraction=0.0,
        after_min_changed_pixel_ratio=0.0001,
        engine_before_mode="openra-mainmenu",
        engine_after_mode="openra-loadgame-dropdown",
        engine_variant_state_key="faction_labels",
        engine_fixed_state_value="Allies,Soviet",
        engine_buggy_state_value="allies,soviet",
        notes="After launch opens the Load Game dialog so the faction filter strings are painted.",
    ),
    "lzp-openra-22325-color-picker-overflow": FrameSpec(
        task_id="lzp-openra-22325-color-picker-overflow",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="openra_engine",
        ignore_top_fraction=0.0,
        after_min_changed_pixel_ratio=0.001,
        engine_before_mode="openra-colorpicker-closed",
        engine_after_mode="openra-colorpicker",
        notes="After launch opens a color picker whose parent sits near the right edge.",
    ),
    "lzp-openra-22283-tileset-template-order": FrameSpec(
        task_id="lzp-openra-22283-tileset-template-order",
        build_packages=(),
        stylesheets=(),
        fixture_css="",
        body_html="",
        capture_kind="openra_engine",
        ignore_top_fraction=0.0,
        after_min_changed_pixel_ratio=0.001,
        engine_before_mode="openra-editor-overlays",
        engine_after_mode="openra-editor-tiles",
        engine_variant_state_key="template_ids",
        engine_fixed_state_value="255,65535,1,2,3,4,5,6,7,8,9,10",
        engine_buggy_state_value="1,2,3,4,5,6,7,8,9,10,11,12",
        notes="After launch opens the map editor tile selector.",
    ),
}

# Tasks with no JavaScript still need a step.  The driver applies this class to
# the declared element to move the page from its start to its after state.
CSS_ONLY_STEPS: Mapping[str, tuple[str, str]] = {
    "lzp-jupyterlab-19091-themed-button-glyph-font": (
        "#host",
        "append_glyph_button",
    ),
    "lzp-jupyterlab-17618-high-contrast-selection-layer": ("#line", "mark_region"),
    "lzp-jupyterlab-17153-shortcut-chip-contrast": ("#line", "mark_chip"),
}


__all__ = [
    "CSS_ONLY_STEPS",
    "FRAME_SPECS",
    "IGNORE_TOP_FRACTION",
    "VIEWPORT",
    "FrameSpec",
]
