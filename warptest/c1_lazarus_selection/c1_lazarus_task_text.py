"""Authored public wording and golden-frame descriptions for C1-Lazarus tasks.

The generated fallbacks in :mod:`c1_lazarus_task_goldens_manifest` describe a
frame as "the goal state of the authoritative fix", which names an intention
rather than anything a visual judge can see, and the public rows describe a
start too loosely for a checkpoint to be synthesized from them. This module is
the single authored source for both texts, so the manifest writer, the formal
catalog writer and the offline text applier all emit the same bytes.

Every registered description states what the corresponding golden frame
actually shows. The evidence is the published frame itself together with the
fixture that rendered it, for example
:mod:`warptest.c1_lazarus_selection.luanti_12602_client_visual` for the green
override wall and :mod:`warptest.c1_lazarus_selection.jupyterlab_17369_downstream`
for the output-area badges.

The three frames are consumed asymmetrically by
:func:`warptest.lazarus_trial.judge_lazarus_visual_evidence`: the registered
``visual_oracle`` description and rubric grade the *before* frame, while each
branch's ``after_description`` and ``after_rubric`` grade that branch's after
frame. :data:`FRAMES` follows that split.

Public wording stays inside the catalog guards in
:mod:`warptest.lazarus_catalog`: it never names a value that separates the
fixed run from the buggy run, never names a touched file, and never uses a
defect word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


FRAMES = ("before", "fixed", "buggy")


@dataclass(frozen=True)
class TaskText:
    """One task's authored public wording and per-frame visual descriptions."""

    preconditions: str
    reference_steps: str
    scene: str
    before: str
    fixed_after: str
    buggy_after: str
    must_not: tuple[str, ...]
    ignore: tuple[str, ...]

    def description(self, frame: str) -> str:
        return {
            "before": self.before,
            "fixed": self.fixed_after,
            "buggy": self.buggy_after,
        }[frame]

    def rubric(self, frame: str) -> dict[str, list[str]]:
        return {
            "must": [self.scene, self.description(frame)],
            "must_not": list(self.must_not),
            "ignore": list(self.ignore),
        }


_LUANTI_MUST_NOT = (
    "A blank frame, a crash dialog, or an unrelated application screen is shown.",
)
_JUPYTERLAB_MUST_NOT = (
    "A blank page, an error overlay, or an unrelated application screen is shown.",
)
_LUANTI_IGNORE = (
    "The hotbar row, the crosshair, the node selection outline, the wielded item "
    "at the lower right, and the server and chat text lines along the top.",
)
_JUPYTERLAB_IGNORE = (
    "Font antialiasing, caret blink, scrollbar rendering, and window chrome.",
)


TASK_TEXT: Mapping[str, TaskText] = {
    "lz-jupyterlab-17700-statusbar-tab-order": TaskText(
        preconditions=(
            "A JupyterLab status bar is loaded from the candidate build at the "
            "bottom of an otherwise empty page: three items are registered in the "
            "right-hand group in ascending rank and render from left to right as "
            "\"Ln 1, Col 1\", \"Kernel Idle\" and \"Bell\", and no item holds "
            "keyboard focus yet."
        ),
        reference_steps=(
            "Press Tab three times and read which right-hand item takes the focus "
            "outline on each press, then compare that focus order with the order "
            "the three items are rendered in."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a blank white "
            "page above a grey JupyterLab status bar, whose right end holds three "
            "items reading \"Ln 1, Col 1\", \"Kernel Idle\" and \"Bell\" from left "
            "to right."
        ),
        before="No status bar item carries a keyboard-focus outline.",
        fixed_after=(
            "A black focus rectangle surrounds \"Ln 1, Col 1\", the leftmost and "
            "innermost of the three right-hand items, while \"Kernel Idle\" and "
            "\"Bell\" carry no outline."
        ),
        buggy_after=(
            "A black focus rectangle surrounds \"Bell\", the rightmost item at the "
            "screen edge, while \"Ln 1, Col 1\" and \"Kernel Idle\" carry no "
            "outline."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=(
            "Font antialiasing, sub-pixel shifts in the status bar background, and "
            "the empty white area above the status bar.",
        ),
    ),
    "lz-jupyterlab-18518-xsrf-duplicate-cookie": TaskText(
        preconditions=(
            "A JupyterLab services connection is loaded from the candidate build "
            "on a routed origin whose real cookie jar already holds two "
            "same-named security cookies stored at two different paths, one on the "
            "origin root and one on the routed sub-path. The page shows only the "
            "grey heading \"XSRF token received by the server\" with no token "
            "printed under it."
        ),
        reference_steps=(
            "Let the connection build one authenticated POST request, then read "
            "the security header value the endpoint echoes back under that "
            "heading and compare it with the cookie the server itself treats as "
            "current."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white page "
            "whose only heading is the small grey line \"XSRF token received by "
            "the server\" near the top left."
        ),
        before=(
            "Nothing is printed under the heading: the rest of the page is blank "
            "white."
        ),
        fixed_after=(
            "One large monospaced line under the heading reads "
            "\"server-truth-token\"."
        ),
        buggy_after=(
            "One large monospaced line under the heading reads "
            "\"stale-path-token\"."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-18572-single-char-code-render": TaskText(
        preconditions=(
            "A JupyterLab editor language registry is loaded from the candidate "
            "build on a page holding one card titled \"CodeMirror language "
            "highlight\": its left \"Input code\" column holds a code block of one "
            "single character, the capital letter X, and its right \"Rendered "
            "preview\" column is an empty rendering target showing the italic grey "
            "placeholder \"Not rendered yet\"."
        ),
        reference_steps=(
            "Ask the registry to highlight that one-character code block into the "
            "preview target, then read the text the preview column holds "
            "afterwards."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows one white card "
            "titled \"CodeMirror language highlight\", split into a left \"Input "
            "code\" column holding a single large letter X and a right \"Rendered "
            "preview\" column."
        ),
        before=(
            "The right column holds only the italic grey placeholder \"Not "
            "rendered yet\"."
        ),
        fixed_after=(
            "The right column holds one large letter X drawn in the same style as "
            "the left column, and the placeholder is gone."
        ),
        buggy_after=(
            "The right column is empty: the placeholder is gone and no letter "
            "appears in its place, so only the \"Rendered preview\" label remains."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-18579-read-only-cell-split": TaskText(
        preconditions=(
            "A JupyterLab notebook is built from the candidate build with exactly "
            "two code cells, each holding one single-line assignment, and every "
            "cell marked non-editable. The upper cell is active and the text "
            "cursor sits inside it, five characters into its only line."
        ),
        reference_steps=(
            "Request a cell split at that cursor through the notebook actions, "
            "then read how many cells the notebook holds afterwards and what each "
            "one contains."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white "
            "notebook page of stacked code cells, each drawn as a grey input box "
            "behind an \"[ ]:\" prompt."
        ),
        before=(
            "The notebook holds exactly two code cells, reading \"alpha = 1\" and "
            "\"beta = 2\" from top to bottom, and a blue bar marks the first cell "
            "as active."
        ),
        fixed_after=(
            "The notebook holds exactly two code cells, reading \"alpha = 1\" and "
            "\"beta = 2\" from top to bottom, and a blue bar marks the first cell "
            "as active."
        ),
        buggy_after=(
            "The notebook holds three code cells, reading \"alpha\", \"alpha = 1\" "
            "and \"beta = 2\" from top to bottom, and the middle one is active "
            "with both a blue bar and a blue input border."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-18662-inline-completer-reserved-lines": TaskText(
        preconditions=(
            "A JupyterLab editor is built from the candidate build with an inline "
            "completer active: the plain white surface holds three black "
            "monospaced document lines reading \"alpha\", \"beta\" and \"gamma\", "
            "and the completer's italic grey ghost suggestion spans fewer lines "
            "than the reserved minimum it is configured with."
        ),
        reference_steps=(
            "Configure the reserved line count twice, once with a bounded maximum "
            "and once with the shipped unlimited maximum, then read the ghost "
            "suggestion together with how many lines the editor keeps reserved "
            "for it."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a plain white "
            "editor surface holding a few short monospaced lines at its top left."
        ),
        before=(
            "The italic grey ghost line \"short\" sits alone on the first line, a "
            "blank line separates it from the document text, and the black words "
            "\"alpha\", \"beta\" and \"gamma\" follow on three lines of their own."
        ),
        fixed_after=(
            "The italic grey ghost line \"short\" sits alone on the first line, a "
            "blank line separates it from the document text, and the black words "
            "\"alpha\", \"beta\" and \"gamma\" follow on three lines of their own."
        ),
        buggy_after=(
            "The italic grey ghost word \"short\" and the black word \"alpha\" "
            "share the first line with no gap between them, only \"beta\" and "
            "\"gamma\" follow underneath, and no blank line is reserved below the "
            "ghost text."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-12863-dialog-getvalue-error": TaskText(
        preconditions=(
            "A JupyterLab dialog is shown from the candidate build on a page "
            "headed \"Rename dialog replay\": the page behind it is dimmed grey, a "
            "centred white modal titled \"Rename File\" reads \"Rename the "
            "notebook\" above a \"Cancel\" button and a blue-outlined \"Ok\" "
            "button, and the modal body's value accessor raises an error when it "
            "is read."
        ),
        reference_steps=(
            "Accept the modal by clicking \"Ok\" with real mouse input, then read "
            "whether the modal is still on screen and how the promise the dialog "
            "returned settles."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a page headed "
            "\"Rename dialog replay\" above the grey line \"A dialog body whose "
            "getValue() throws is accepted with Ok.\"."
        ),
        before=(
            "The page below the heading is dimmed grey and a white modal titled "
            "\"Rename File\" is centred on it, reading \"Rename the notebook\" "
            "above a \"Cancel\" button and a blue-outlined \"Ok\" button."
        ),
        fixed_after=(
            "No modal is on screen and the page below the two heading lines is "
            "undimmed white."
        ),
        buggy_after=(
            "The page below the heading is dimmed grey and a white modal titled "
            "\"Rename File\" is centred on it, reading \"Rename the notebook\" "
            "above a \"Cancel\" button and a blue-outlined \"Ok\" button."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-18992-collapsed-heading-visibility": TaskText(
        preconditions=(
            "A JupyterLab notebook is built from the candidate build with the "
            "top-level heading \"# Title\" above two heading sections, \"## First "
            "section\" and \"## Second section\". The first section holds the two "
            "code cells \"first = 1\" and \"second = 2\", and its heading is "
            "collapsed, so a small grey pill reading \"+ 2 cells hidden\" stands "
            "in their place."
        ),
        reference_steps=(
            "Move the collapsed \"## First section\" heading down past \"## Second "
            "section\", then read every cell's source together with whether each "
            "cell is rendered visible or stays hidden."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white "
            "notebook page of rendered markdown headings, each ending in a blue "
            "pilcrow, with a blue active bar left of the top heading \"Title\"."
        ),
        before=(
            "A small grey pill reading \"+ 2 cells hidden\" sits between the "
            "headings \"First section\" and \"Second section\", and no code cell "
            "is visible anywhere on the page."
        ),
        fixed_after=(
            "Two code cells reading \"first = 1\" and \"second = 2\" are visible "
            "directly under \"Title\", the headings \"First section\" and "
            "\"Second section\" follow below them, and no hidden-cells pill is on "
            "the page."
        ),
        buggy_after=(
            "Only the three headings \"Title\", \"First section\" and \"Second "
            "section\" are on the page: no code cell is visible and no "
            "hidden-cells pill marks the missing ones."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-19121-shortcut-settings-refresh": TaskText(
        preconditions=(
            "The JupyterLab shortcut settings interface is mounted from the "
            "candidate build with no keybindings registered: a dark search field "
            "sits beside \"Show Selectors\", \"Reset All\" and a blue plus button, "
            "the column headers \"Category\", \"Command\", \"Shortcut\" and "
            "\"Source\" are drawn under them, and the bordered list area below is "
            "empty."
        ),
        reference_steps=(
            "Save a user preference for one keybinding outside this interface and "
            "emit the plugin changed signal, then read the filtered shortcut list "
            "together with how many rows the interface renders."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows the JupyterLab "
            "shortcut settings page: a dark search field with \"Show Selectors\", "
            "\"Reset All\" and a blue plus button, above the column headers "
            "\"Category\", \"Command\", \"Shortcut\" and \"Source\" and a bordered "
            "list area."
        ),
        before=(
            "The bordered list area under the headers is entirely empty: no "
            "shortcut row is drawn."
        ),
        fixed_after=(
            "One shortcut row is drawn under the headers, reading \"test\" in the "
            "Category column, \"^ + Y\" in the Shortcut column and \"Default\" in "
            "the Source column."
        ),
        buggy_after=(
            "The bordered list area under the headers is entirely empty: no "
            "shortcut row is drawn."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-19136-horizontal-activity-bar-restore": TaskText(
        preconditions=(
            "A JupyterLab shell is built from the candidate build with its "
            "activity bar in the horizontal position and one widget docked in the "
            "right area, so the frame shows a main dock area bounded on the right "
            "by a vertical border, and a side area beyond it carrying a grey "
            "\"Property Inspector\" tab at the bottom right. That layout is then "
            "saved."
        ),
        reference_steps=(
            "Restore a second shell from that saved layout with an explicit "
            "not-collapsed flag, then measure whether the restored right area is "
            "visible and whether its width has become zero."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a mostly white "
            "JupyterLab shell with a thin horizontal rule near the top and a small "
            "blue dot at the top left corner."
        ),
        before=(
            "A vertical border about three quarters across the frame bounds the "
            "main dock area, and the side area beyond it carries a grey "
            "\"Property Inspector\" tab at the bottom right."
        ),
        fixed_after=(
            "A vertical border about three quarters across the frame bounds the "
            "main dock area, and the side area beyond it carries a grey "
            "\"Property Inspector\" tab at the bottom right."
        ),
        buggy_after=(
            "No vertical border and no side area are drawn: the horizontal rule "
            "runs the full frame width and the right-hand side is empty white "
            "with no \"Property Inspector\" tab."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-jupyterlab-19227-reactive-toolbar-positions": TaskText(
        preconditions=(
            "A reactive JupyterLab toolbar is built from the candidate build with "
            "its permanent items in place, a \"spacer\" at the left and a "
            "\"close\" button followed by the overflow opener at the right, and "
            "two factory items named \"f1\" and \"f2\" inserted immediately before "
            "\"close\", so the strip reads spacer, f1, f2, close and then the "
            "overflow button from left to right."
        ),
        reference_steps=(
            "Dispose both factory items, insert their replacements before the same "
            "\"close\" button, then read the item names in the order the toolbar "
            "renders them from left to right."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows one small white "
            "toolbar strip in the top-left corner of an otherwise blank page, "
            "holding five items in a single row that ends with a horizontal "
            "ellipsis overflow button."
        ),
        before=(
            "The row reads \"spacer\", \"f1\", \"f2\", \"close\" and then the "
            "overflow button, from left to right."
        ),
        fixed_after=(
            "The row reads \"spacer\", \"f1\", \"f2\", \"close\" and then the "
            "overflow button, from left to right."
        ),
        buggy_after=(
            "The row reads \"spacer\", \"close\", \"f2\", \"f1\" and then the "
            "overflow button, from left to right, so both factory items sit after "
            "the close button and in reverse order."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=_JUPYTERLAB_IGNORE,
    ),
    "lz-luanti-8704-minimal-craft-result": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with the minimal "
            "game's test mod loaded: a centred formspec panel titled \"C1 LAZARUS "
            "#8704 CRAFT RESULT REPLAY\" reports under \"COMMON START\" that one "
            "recipe combining coal and a stick is registered for a single output "
            "item, names the output item and count it expects, and waits for the "
            "registered craft query."
        ),
        reference_steps=(
            "Run the registered replay once so the server resolves that recipe, "
            "then read the item name and item count the panel reports as the craft "
            "result."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a dark "
            "room behind a centred formspec panel with a coloured title bar, above "
            "an eight-slot hotbar."
        ),
        before=(
            "The title bar is dark blue-grey and reads \"C1 LAZARUS #8704 CRAFT "
            "RESULT REPLAY\", and the body reads \"COMMON START\", \"Registered "
            "coal plus stick recipe is ready.\", \"Expected semantic result: "
            "default:torch x4\" and \"Waiting for minetest.get_craft_result ...\"."
        ),
        fixed_after=(
            "The title bar is green and reads \"C1 LAZARUS #8704 ACTUAL CRAFT "
            "RESULT\", and the body shows \"GOAL: TORCH RECIPE FOUND\" with a "
            "yellow torch icon beside \"item name: default:torch\", \"item count: "
            "4\" and \"semantic state: goal_torch_x4\"."
        ),
        buggy_after=(
            "The title bar is dark red and reads \"C1 LAZARUS #8704 ACTUAL CRAFT "
            "RESULT\", and the body shows \"DEFECT: EMPTY CRAFT RESULT\" with no "
            "item icon, an \"item name:\" line with nothing after it, \"item "
            "count: 0\" and \"semantic state: defect_empty\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-9572-vector-rotate": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with a helper that "
            "rotates one direction around an axis: a centred formspec panel titled "
            "\"C1 LAZARUS #9572 VECTOR ROTATE REPLAY\" states under \"COMMON "
            "START\" that the replay rotates (1,0,0) around Z by pi/2, that the "
            "expected result is approximately (0,-1,0), and that vector addition "
            "stays the invariant."
        ),
        reference_steps=(
            "Run the registered replay once, then read the rotated direction the "
            "panel reports together with whether the rotation helper was callable "
            "at all."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a dark "
            "room behind a centred formspec panel with a coloured title bar, above "
            "an eight-slot hotbar."
        ),
        before=(
            "The title bar is dark blue-grey and reads \"C1 LAZARUS #9572 VECTOR "
            "ROTATE REPLAY\", and the body reads \"COMMON START\", \"Rotate "
            "(1,0,0) around Z by pi/2.\", \"Expected result: approximately "
            "(0,-1,0)\" and \"vector.add remains the invariant.\"."
        ),
        fixed_after=(
            "The title bar is green and reads \"C1 LAZARUS #9572 ACTUAL VECTOR "
            "RESULT\", and the body shows \"GOAL: VECTOR ROTATE WORKS\" above "
            "\"rotate type: function\", \"rotation correct: true\" and "
            "\"vector.add invariant: true\"."
        ),
        buggy_after=(
            "The title bar is dark red and reads \"C1 LAZARUS #9572 ACTUAL VECTOR "
            "RESULT\", and the body shows \"DEFECT: VECTOR ROTATE MISSING\" above "
            "\"rotate type: nil\", \"rotation correct: false\" and \"vector.add "
            "invariant: true\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-9722-reversible-number-serialize": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start at night with two "
            "high-precision Lua numbers registered in one table: a centred "
            "formspec panel titled \"C1 LAZARUS #9722 REVERSIBLE NUMBER REPLAY\" "
            "lists them under \"COMMON START\" as 0.2695949158945771 and "
            "269594915894577 and waits for the ordered replay."
        ),
        reference_steps=(
            "Run the registered replay once so that table is written out and read "
            "back, then read whether each of the two numbers still matches the "
            "value the panel listed."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a centred "
            "formspec panel with a coloured title bar over the world, above an "
            "eight-slot hotbar."
        ),
        before=(
            "The world behind is nearly black, the title bar is dark blue-grey and "
            "reads \"C1 LAZARUS #9722 REVERSIBLE NUMBER REPLAY\", and the body "
            "lists \"COMMON START\", the two numbers 0.2695949158945771 and "
            "269594915894577, and \"Waiting for the ordered replay ...\"."
        ),
        fixed_after=(
            "The world behind is daylit blue sky, the title bar is green and reads "
            "\"C1 LAZARUS #9722 REPLAY RESULT\", and the body shows \"GOAL: BOTH "
            "NUMBERS PRESERVED\" above \"value 1 exact: true\", \"value 2 exact: "
            "true\" and \"semantic class: goal_reversible_numbers\"."
        ),
        buggy_after=(
            "The world behind is daylit blue sky, the title bar is dark red and "
            "reads \"C1 LAZARUS #9722 REPLAY RESULT\", and the body shows "
            "\"DEFECT: NUMBER CHANGED\" above \"value 1 exact: false\", \"value 2 "
            "exact: true\" and \"semantic class: defect_number_changed\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-11352-map-settings-hierarchy": TaskText(
        preconditions=(
            "A Luanti Development Test main menu is open on the registered start: "
            "a grey panel headed \"REGISTERED START\" reports \"Global "
            "water_level: 20\" and \"Mapgen water_level: 20\", so one map value is "
            "present both in the global configuration and in the world's own "
            "mapgen configuration, and a \"Run registered replay\" button waits "
            "below the panel."
        ),
        reference_steps=(
            "Press \"Run registered replay\" once so the world's own mapgen "
            "configuration is applied, then read both the global water_level and "
            "the mapgen water_level the panel reports afterwards."
        ),
        scene=(
            "A Luanti main-menu frame rendered by the candidate build shows the "
            "\"Development Test\" title over a green checkered background, the "
            "line \"C1 Lazarus #11352 - Map Settings Hierarchy\", a centred panel "
            "of two water_level readings, and a \"Run registered replay\" button."
        ),
        before=(
            "The panel is translucent grey, headed \"REGISTERED START\", and reads "
            "\"Global water_level: 20\" above \"Mapgen water_level: 20\"."
        ),
        fixed_after=(
            "The panel is solid green, headed \"FIXED: MAP DEFAULT ISOLATED\", and "
            "reads \"Global water_level: 20\" above \"Mapgen water_level: 15\", so "
            "only the mapgen value moved."
        ),
        buggy_after=(
            "The panel is solid red-brown, headed \"BUGGY: GLOBAL SETTING "
            "MUTATED\", and reads \"Global water_level: 15\" above \"Mapgen "
            "water_level: 15\", so both values moved together."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "The checkered menu background, the \"Development Test\" title "
            "graphic, and font antialiasing.",
        ),
    ),
    "lz-luanti-11866-mapgen-boundary": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with the map "
            "generation limit set close to the edge of the addressable map: a "
            "centred formspec panel titled \"C1 LAZARUS #11866 MAPGEN BOUNDARY\" "
            "states under \"COMMON START\" that the replay reads the engine "
            "default mapgen_limit, that MAP_BLOCKSIZE is 16, and that the limit is "
            "expected to leave remainder 15, the last node in a mapblock."
        ),
        reference_steps=(
            "Run the registered replay once, then read the mapgen_limit the panel "
            "reports together with the remainder that limit leaves against the "
            "block size."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a dark "
            "room behind a centred formspec panel with a coloured title bar, above "
            "an eight-slot hotbar."
        ),
        before=(
            "The title bar is dark blue-grey and reads \"C1 LAZARUS #11866 MAPGEN "
            "BOUNDARY\", and the body reads \"COMMON START\", \"Read the engine "
            "default mapgen_limit.\", \"MAP_BLOCKSIZE = 16\" and \"Goal: limit % "
            "16 equals 15 (the last node in a mapblock).\"."
        ),
        fixed_after=(
            "The title bar is green and reads \"C1 LAZARUS #11866 REPLAY RESULT\", "
            "and the body shows \"GOAL: MAPGEN LIMIT ALIGNED\" above "
            "\"mapgen_limit = 31007\", \"31007 % 16 = 15\" and \"expected boundary "
            "remainder = 15\", so the reported remainder matches the expected one."
        ),
        buggy_after=(
            "The title bar is dark red and reads \"C1 LAZARUS #11866 REPLAY "
            "RESULT\", and the body shows \"DEFECT: MAPGEN LIMIT MISALIGNED\" "
            "above \"mapgen_limit = 31000\", \"31000 % 16 = 8\" and \"expected "
            "boundary remainder = 15\", so the two remainders disagree."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-13132-empty-separator": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with a string helper "
            "that splits text on a caller-supplied separator: the client camera is "
            "locked on a free-standing wall panel of grey cobblestone blocks "
            "standing on a flat grey stone plain under a blue sky, and the "
            "registered replay rewrites every panel block to the block that the "
            "split outcome selects."
        ),
        reference_steps=(
            "Run the registered replay once with an empty separator, then read the "
            "outcome the server reports together with the block the wall panel "
            "renders afterwards."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a flat "
            "grey stone plain under a blue sky, with a free-standing wall panel of "
            "blocks centred in view and a hotbar of tools along the bottom."
        ),
        before="Every block of the wall panel is grey cobblestone.",
        fixed_after=(
            "Every block of the wall panel is dirt with a grass top, so the panel "
            "reads as brown blocks banded with green grass edges and no pale "
            "pink-tan sand block remains in it."
        ),
        buggy_after=(
            "Every block of the wall panel is pale pink-tan desert sand, and no "
            "green grass edge remains in it."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-14072-oversized-stack": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start showing an overlay "
            "titled \"C1 Lazarus #14072 - Oversized Stack Move\": a SOURCE slot "
            "holds 209 items of one kind, a count above that item's stack maximum, "
            "a DESTINATION slot already holds 90 of the same item, the status line "
            "reads \"REGISTERED START\" over \"counts=209/90\", and a \"Run Move "
            "209\" button waits below."
        ),
        reference_steps=(
            "Press \"Run Move 209\" once to move the oversized stack into the "
            "occupied slot, then read the item counts both slots hold afterwards."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a flat "
            "blue-grey background behind a centred overlay titled \"C1 Lazarus "
            "#14072 - Oversized Stack Move\", with a SOURCE and a DESTINATION item "
            "icon side by side above a status line and a \"Run Move 209\" button."
        ),
        before=(
            "The SOURCE icon carries the count 209 and the DESTINATION icon the "
            "count 90, and the status line reads \"REGISTERED START\" over "
            "\"counts=209/90\"."
        ),
        fixed_after=(
            "The SOURCE icon carries the count 99 and the DESTINATION icon the "
            "count 200, and the status line reads "
            "\"goal_fixed_oversized_stack_preserved\" over \"counts=99/200\", so "
            "the two counts still add up to the 299 items that were present."
        ),
        buggy_after=(
            "The SOURCE icon carries the count 99 and the DESTINATION icon the "
            "count 101, and the status line reads "
            "\"defect_buggy_oversized_stack_loss\" over \"counts=99/101\", so "
            "items have gone missing."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-14464-iconv-state-reset": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start in a fresh process "
            "with the 28-test utilities module registered: a centred formspec "
            "panel titled \"C1 LAZARUS #14464 ICONV STATE REPLAY\" states under "
            "\"COMMON START\" that the node metadata carries the byte C4 followed "
            "by an isolated 81, outside the ASCII range, and that the text "
            "converter must not carry state across invocations."
        ),
        reference_steps=(
            "Run the registered replay once so that metadata is stored and read "
            "back, then read how many of the 28 utility checks the panel reports "
            "as passing and which named checks do not pass."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a dark "
            "room behind a centred formspec panel with a coloured title bar, above "
            "an eight-slot hotbar."
        ),
        before=(
            "The title bar is dark blue-grey and reads \"C1 LAZARUS #14464 ICONV "
            "STATE REPLAY\", and the body reads \"COMMON START\", \"Fresh process, "
            "TestUtilities 28-test module registered.\", \"Input bytes: C4 then "
            "isolated 81\" and \"Waiting for the ordered replay ...\"."
        ),
        fixed_after=(
            "The title bar is green and reads \"C1 LAZARUS #14464 REPLAY RESULT\", "
            "and the body shows \"GOAL: 28 / 28 UTILITIES PASS\" above "
            "\"testUTF8: PASS\", \"testWrapRows: PASS\" and \"testSanitizeDirName: "
            "PASS\"."
        ),
        buggy_after=(
            "The title bar is dark red and reads \"C1 LAZARUS #14464 REPLAY "
            "RESULT\", and the body shows \"DEFECT: EXACT 3 / 28 FAIL\" above "
            "\"testUTF8: FAIL\", \"testWrapRows: FAIL\" and \"testSanitizeDirName: "
            "FAIL\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    "lz-luanti-14555-negative-half-pointing": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with the player "
            "pointing at a node whose coordinates lie on the negative side of the "
            "origin: a full-screen overlay titled \"C1 Lazarus #14555 - "
            "Negative-half Pointing\" reads \"REGISTERED START\" over a dark "
            "blue-grey background, with the pickaxe selected in the hotbar."
        ),
        reference_steps=(
            "Run the registered replay once to place a cobble node against the "
            "pointed face, then read whether that node landed at the negative-half "
            "target the replay aimed at."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build is covered by a "
            "full-screen tinted overlay carrying the title \"C1 Lazarus #14555 - "
            "Negative-half Pointing\", with the hotbar visible along the bottom."
        ),
        before=(
            "The overlay is dark blue-grey and its only status line reads "
            "\"REGISTERED START\"."
        ),
        fixed_after=(
            "The overlay is green and reads \"GOAL\" above \"fixed: cobble "
            "placement present at negative-half target\"."
        ),
        buggy_after=(
            "The overlay is dark red and reads \"DEFECT\" above \"buggy: expected "
            "cobble placement absent\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "The hotbar row, the crosshair, the wielded item, and font "
            "antialiasing on the overlay text.",
        ),
    ),
    "lz-luanti-15408-collision-acceleration": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with a white-and-grey "
            "cube entity resting on a small stone platform that floats in a pale "
            "blue sky: a tall cobblestone pillar stands on the same platform, a "
            "small purple marker lies on the platform between them, and a "
            "horizontal acceleration is applied to the entity toward the pillar."
        ),
        reference_steps=(
            "Run the registered replay once, then read the entity's resulting "
            "speed and position and whether the pillar stopped it."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a small "
            "stone platform floating in a pale blue sky, carrying a tall "
            "cobblestone pillar and one white-and-grey cube entity."
        ),
        before=(
            "The cube rests to the right of the pillar with a clear gap between "
            "them, and a small purple marker is visible on the platform inside "
            "that gap."
        ),
        fixed_after=(
            "The cube has moved left and now rests directly against the right side "
            "of the pillar, covering the purple marker, and no part of the cube is "
            "on the left side of the pillar."
        ),
        buggy_after=(
            "The cube has passed through the pillar and now rests on its left "
            "side, overlapping the pillar's base, while the purple marker is "
            "visible again on the empty right side."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "The hotbar row, the crosshair, the coloured cloud band on the right, "
            "and the exact sky gradient.",
        ),
    ),
    "lz-luanti-15519-russian-plural-form": TaskText(
        preconditions=(
            "A Luanti world is open on the registered start with a translation "
            "catalogue whose plural rule text is supplied by the catalogue itself: "
            "the client shows a near-black screen carrying the single centred line "
            "\"C1 COMMON BEFORE\" and nothing else."
        ),
        reference_steps=(
            "Run the registered replay once to translate one plural message, then "
            "read the single line of text the client renders in place of that "
            "start line."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a "
            "near-black screen carrying one short centred line of light text and "
            "nothing else."
        ),
        before="The centred line reads \"C1 COMMON BEFORE\".",
        fixed_after=(
            "The centred line reads the Cyrillic word \"\u044f\u0431\u043b\u043e"
            "\u043a\u0430\", so the message was translated into Russian."
        ),
        buggy_after=(
            "The centred line reads the untranslated English word \"apples\"."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "Font antialiasing, the exact position of the line, and sub-pixel "
            "differences in the near-black background.",
        ),
    ),
    "lz-luanti-12602-media-override": TaskText(
        preconditions=(
            "A Luanti devtest world is open on the registered start: the player "
            "stands at the registered spawn inside a closed room whose walls, "
            "floor and ceiling are one display node, looking straight ahead at "
            "the far wall, and the game loads two ordered mods, a base mod and a "
            "later override mod, that both ship a texture file under the same "
            "name. A centred dialog titled \"C1 LAZARUS  #12602  MEDIA OVERRIDE "
            "REPLAY\" states that the later override mod must win."
        ),
        reference_steps=(
            "Run the registered replay once from that start, then read the "
            "texture the room surfaces render and the media paths the server's "
            "mod manager reports for that shared texture name."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows the "
            "player inside a closed room of display nodes, looking straight "
            "ahead at the far wall."
        ),
        before=(
            "No tiled texture covers the room yet: the view is a flat dark "
            "blue-grey background behind a centred dialog titled \"C1 LAZARUS  "
            "#12602  MEDIA OVERRIDE REPLAY\" whose body reads \"COMMON START\", "
            "\"Two ordered mods provide the same texture filename.\", \"The later "
            "override mod must win.\" and \"Waiting for the registered replay "
            "...\"."
        ),
        fixed_after=(
            "The dialog is gone and every wall, floor and ceiling surface in "
            "view is tiled with the override mod's green texture, a bright green "
            "grid on a dark green background, with no red tiling on those "
            "surfaces."
        ),
        buggy_after=(
            "The dialog is gone and every wall, floor and ceiling surface in "
            "view is tiled with the base mod's red texture, a bright red grid on "
            "a dark red background, with no green tiling on those surfaces."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "The green slab at the lower right, the hotbar row, the crosshair, "
            "the node selection outline, and the server and chat text lines "
            "along the top.",
        ),
    ),
    "lz-jupyterlab-17369-stream-chunk-merge": TaskText(
        preconditions=(
            "A JupyterLab output area is restored from the registered start on "
            "the candidate build's replay page: it holds exactly one "
            "standard-output stream, the stream renders as two monospaced lines "
            "inside a single output card, and its second line stops mid-sentence "
            "without a trailing newline."
        ),
        reference_steps=(
            "Append one further standard-output chunk to that same stream, then "
            "read the consolidated body the output area renders, counting its "
            "lines and reading how the appended chunk joins the line that "
            "carried no trailing newline."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows the "
            "output-area replay page: a white top bar reading \"JupyterLab \u00b7 "
            "OutputAreaModel replay\" above one white \"stdout\" card on a light "
            "grey page."
        ),
        before=(
            "The card carries a grey left border and a grey \"COMMON START\" "
            "badge, and its monospaced body holds exactly two lines, \"The "
            "meaning of life is....\" and \"Some people...\"."
        ),
        fixed_after=(
            "The card carries a green left border and a green \"CORRECT MERGE\" "
            "badge, and its monospaced body holds exactly two lines, \"The "
            "meaning of life is....\" and \"Some people... More text.\"."
        ),
        buggy_after=(
            "The card carries a red left border and a red \"CORRUPT MERGE\" "
            "badge, and its monospaced body holds exactly two lines, \"The "
            "meaning of life is....\" and \"More text.e...\", so the second "
            "starting line is gone."
        ),
        must_not=_JUPYTERLAB_MUST_NOT,
        ignore=(
            "Font antialiasing, caret blink, scrollbar rendering, window chrome, "
            "and the grey footnote line under the card.",
        ),
    ),
    "lz-luanti-14757-math-round": TaskText(
        preconditions=(
            "A Luanti devtest world is open on the registered start as a "
            "headless server with the registered world mod loaded: the client "
            "camera sits at a locked first-person pose in a flat desert facing "
            "a free-standing wall panel of grey cobblestone blocks, and the world "
            "mod registers the chat command /c1_lazarus_round, which rounds one "
            "registered pair of numbers and then rewrites every panel block to "
            "the block that the rounded pair selects."
        ),
        reference_steps=(
            "Deliver /c1_lazarus_round once from the server side, then read the "
            "rounded pair the command reports together with the block the wall "
            "panel renders afterwards."
        ),
        scene=(
            "A Luanti client frame rendered by the candidate build shows a flat "
            "desert from a fixed first-person camera, with a free-standing wall "
            "panel of blocks centred in view above a stone-slab base."
        ),
        before=("Every block of the wall panel is grey cobblestone."),
        fixed_after=(
            "Every block of the wall panel is dirt with a grass top, so the "
            "panel reads as brown blocks banded with green grass edges and no "
            "pale pink-tan sand block remains in it."
        ),
        buggy_after=(
            "Every block of the wall panel is pale pink-tan desert sand, and no "
            "green grass edge remains in it."
        ),
        must_not=_LUANTI_MUST_NOT,
        ignore=(
            "The hotbar row, the wielded pickaxe, the crosshair, the node "
            "selection outline, the sky gradient, and the surrounding desert "
            "terrain.",
        ),
    ),
}


def task_text(task_id: str) -> TaskText | None:
    """Return the authored text for one task, or ``None`` when unregistered."""

    return TASK_TEXT.get(task_id)


def public_contract(task_id: str) -> tuple[str, str] | None:
    """Return ``(preconditions, reference_steps)`` for one registered task."""

    text = TASK_TEXT.get(task_id)
    if text is None:
        return None
    return text.preconditions, text.reference_steps


def frame_description(task_id: str, frame: str) -> str | None:
    """Return the authored description of one registered golden frame."""

    if frame not in FRAMES:
        raise ValueError(f"unknown Lazarus golden frame: {frame!r}")
    text = TASK_TEXT.get(task_id)
    return None if text is None else text.description(frame)


def frame_rubric(task_id: str, frame: str) -> dict[str, list[str]] | None:
    """Return the authored rubric for one registered golden frame."""

    if frame not in FRAMES:
        raise ValueError(f"unknown Lazarus golden frame: {frame!r}")
    text = TASK_TEXT.get(task_id)
    return None if text is None else text.rubric(frame)


__all__ = [
    "FRAMES",
    "TASK_TEXT",
    "TaskText",
    "frame_description",
    "frame_rubric",
    "public_contract",
    "task_text",
]
