"""Authored public wording and frame descriptions for the P2P-only track.

The public half of each entry -- ``preconditions`` and ``reference_steps`` -- is
what the model sees.  It states the start and the actions and nothing else: no
value that separates the two products, no repaired path, and no word that hints a
product is at fault.  :func:`warptest.lazarus_catalog.validate_lazarus_catalog`
re-checks all three of those guards, and
:func:`warptest.lazarus_mining.verify_public_text_has_no_hidden_tokens` re-checks
them against the registered oracle.

``readout`` is the other half of that split, and it is not public.  It names the
part of the frame the verdict turns on, which is the judge's business rather than
the model's, so :meth:`P2POnlyTaskText.rubric` carries it as a ``must`` line on
the two after frames instead of leaving it in the reference steps.  The before
rubric deliberately omits it because the target control may not exist until the
reference steps have run.

The remaining fields describe the four published frames.  In this track those
frames are the only defect evidence -- six passing control cells show that both
products run, not that either is wrong -- so each description states what the
frame shows rather than what it is supposed to mean.

Every ``before`` description is shared by both products on purpose: the start has
to be equivalent, or the after differential would not be attributable to the
reference steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .c1_lazarus_task_text import TaskText


@dataclass(frozen=True)
class P2POnlyTaskText(TaskText):
    """One task's wording, with the readout it is judged on kept off the prompt."""

    readout: str = ""
    before_ignore: tuple[str, ...] = ()

    def rubric(self, frame: str) -> dict[str, list[str]]:
        rubric = super().rubric(frame)
        if frame == "before":
            rubric["ignore"].extend(self.before_ignore)
        if frame != "before" and self.readout:
            rubric["must"].append(self.readout)
        return rubric


_MUST_NOT = (
    "A blank page, an error overlay, or an unrelated application screen is shown.",
)
_IGNORE = ("Font antialiasing, caret blink, and sub-pixel text shifts.",)


TASK_TEXT: Mapping[str, P2POnlyTaskText] = {
    "lzp-jupyterlab-18996-htmlselect-caret-offset": P2POnlyTaskText(
        preconditions=(
            "A JupyterLab control strip is restored from the registered start on "
            "the candidate build's replay page: the strip is a single row holding "
            "one labelled trigger at its right-hand end reading \"More\", and no "
            "overflow panel is open below it."
        ),
        reference_steps="Open the overflow panel from that trigger.",
        readout=(
            "Judge this frame by where the small chevron glyph of the overflow "
            "panel's dropdown selector is painted relative to that selector's own "
            "box: its height within the box, and how far its right edge sits from "
            "the box's right edge."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white page "
            "with one grey control strip near the top left whose right end holds a "
            "trigger reading \"More\"."
        ),
        before=(
            "Nothing is drawn below the strip: the area under it is blank white."
        ),
        fixed_after=(
            "A bordered overflow panel is open under the strip, holding one "
            "dropdown selector reading \"Code\". The chevron glyph sits inside the "
            "selector's box, vertically centred against its text and just inside "
            "the box's right border."
        ),
        buggy_after=(
            "A bordered overflow panel is open under the strip, holding one "
            "dropdown selector reading \"Code\". The chevron glyph is lifted above "
            "the selector's text into the panel's own top-right corner, clear of "
            "the selector's box altogether."
        ),
        must_not=_MUST_NOT,
        ignore=_IGNORE,
    ),
    "lzp-jupyterlab-18464-debugger-zero-variable-detail": P2POnlyTaskText(
        preconditions=(
            'A JupyterLab inspector tree is restored from the registered start on '
            "the candidate build's replay page: it lists a single scope row named "
            '"Locals" holding one entry named "total", and that entry\'s row shows '
            'its name followed by its integer readout.'
        ),
        reference_steps=(
            'Load the scope\'s second entry, named "count", whose integer readout is '
            'the additive identity.'
        ),
        scene=(
            'A candidate inspector tree is visible on a white page, belonging to the '
            'Locals scope (a separate scope heading need not be painted).'
        ),
        before=(
            'Exactly one entry row named "total" is shown with a nonzero integer '
            'readout. The second entry named "count" is not present yet.'
        ),
        fixed_after=(
            'Two entry rows are drawn under "Locals". Each reads its name followed '
            'by a blue readout at the same size as the name: "total" then its '
            'initial integer readout, and "count" then "0".'
        ),
        buggy_after=(
            'Two entry rows are drawn under "Locals". The first reads "total" '
            'followed by a blue integer readout at the same size as the name. The '
            'second reads "count" followed by a much smaller "0" in the name\'s own '
            "dark colour, sitting high against the name rather than on the row's "
            'baseline.'
        ),
        must_not=(
            (
                'A blank page, an error overlay, or an unrelated application screen is '
                'shown.'
            ),
        ),
        ignore=(
            (
                'The particular nonzero integer displayed for the initial total row (7 '
                'in the reference is an example, not a required value).'
            ),
            (
                'Whether a separate Locals heading is painted; viewport dimensions, '
                'surrounding blank space, panel position, font antialiasing and '
                'sub-pixel shifts.'
            ),
        ),
        readout=(
            'Judge this frame by how the readout of the entry named "count" is drawn '
            'beside its name, comparing its size and colour with the readout on the '
            'row above.'
        ),
        before_ignore=(),
    ),
    "lzp-jupyterlab-17222-toolbar-button-pressed-toggle": P2POnlyTaskText(
        preconditions=(
            "A JupyterLab control strip is restored from the registered start on "
            "the candidate build's replay page: it holds one two-state button, the "
            "button reports itself as not currently engaged, and it paints a filled "
            "circle glyph."
        ),
        reference_steps="Activate that button once with a left pointer press.",
        readout=(
            "Judge this frame by which glyph the button paints and what engaged "
            "state the button reports for itself."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white page "
            "with one grey control strip near the top left holding a single square "
            "button."
        ),
        before=("The button paints a filled circle glyph."),
        fixed_after=(
            "The button paints a hollow square glyph and reports itself as "
            "engaged, and the strip below it reads \"engaged: true\"."
        ),
        buggy_after=(
            "The button still paints the filled circle glyph and reports itself as "
            "not engaged, and the strip below it reads \"engaged: false\"."
        ),
        must_not=_MUST_NOT,
        ignore=_IGNORE,
    ),
    "lzp-jupyterlab-17153-shortcut-chip-contrast": P2POnlyTaskText(
        preconditions=(
            "A JupyterLab themed container is restored from the registered start "
            "on the candidate build's replay page under the dark palette: it "
            "shows one dark row holding the label \"Ctrl A\" in white, and that "
            "label is not yet drawn as a chip."
        ),
        reference_steps="Draw that label as a selected chip.",
        readout=(
            "Judge this frame by comparing the shade of the chip drawn behind "
            "\"Ctrl A\" with the white of the label drawn on it: whether the chip "
            "is dark enough for the white strokes to read cleanly against it, or "
            "so light that those strokes nearly merge into the chip."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a dark row on a "
            "dark page, holding the short monospaced label \"Ctrl A\" near its "
            "top left."
        ),
        before=(
            "The label reads \"Ctrl A\" in white directly on the dark row, with "
            "no chip drawn behind it."
        ),
        fixed_after=(
            "A medium blue chip is drawn behind \"Ctrl A\". The chip is distinctly "
            "darker than the white label on it, so the label's white strokes read "
            "cleanly against the chip."
        ),
        buggy_after=(
            "A very pale, near-white blue chip is drawn behind \"Ctrl A\". The "
            "chip is almost as light as the white label on it, so the label's "
            "white strokes nearly merge into the chip."
        ),
        must_not=_MUST_NOT,
        ignore=_IGNORE,
    ),
    "lzp-jupyterlab-19091-themed-button-glyph-font": P2POnlyTaskText(
        preconditions=(
            "A JupyterLab themed container is restored from the registered start "
            "on the candidate build's replay page: it holds one plain button "
            "reading \"Plain\" drawn in the interface typeface, and the glyph "
            "button beside it has not been placed yet."
        ),
        reference_steps=(
            "Place the second button, which carries a typeface class of its own and "
            "the label \"WWWiii\", into the same container."
        ),
        readout=(
            "Judge this frame by which typeface the label \"WWWiii\" is painted in "
            "and how wide that label runs."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a white page "
            "holding a themed container with one or two large bordered buttons in a "
            "row near the top left."
        ),
        before=(
            "A single button reading \"Plain\" is drawn in the proportional "
            "interface typeface. No second button is present."
        ),
        fixed_after=(
            "Two buttons are drawn. The second reads \"WWWiii\" in a fixed-width "
            "typeface, so its six characters occupy six equal columns and the label "
            "runs noticeably wider than the same text would in the interface "
            "typeface."
        ),
        buggy_after=(
            "Two buttons are drawn. The second reads \"WWWiii\" in the same "
            "proportional interface typeface as the first, so its three W "
            "characters are much wider than its three i characters and the label "
            "runs narrower overall."
        ),
        must_not=_MUST_NOT,
        ignore=_IGNORE,
    ),
    "lzp-jupyterlab-17618-high-contrast-selection-layer": P2POnlyTaskText(
        preconditions=(
            "A JupyterLab themed container is restored from the registered start "
            "on the candidate build's replay page under the high-contrast dark "
            "palette: it shows one near-black panel holding the single line "
            "\"select me\" in white, and no region of that line is marked."
        ),
        reference_steps="Mark the whole of that line as the current region.",
        readout=(
            "Judge this frame by whether the marked region can be told apart from "
            "the panel behind it, comparing the shade drawn behind the line "
            "\"select me\" with the panel's own shade."
        ),
        scene=(
            "A browser frame rendered by the candidate build shows a near-black "
            "panel on a dark page, holding the white line \"select me\" near its "
            "top left."
        ),
        before=(
            "The line sits directly on the panel: no band of any other shade is "
            "drawn behind it."
        ),
        fixed_after=(
            "A clearly lighter mid-grey band is drawn behind the line \"select "
            "me\", standing out plainly against the near-black panel around it."
        ),
        buggy_after=(
            "The band drawn behind the line \"select me\" is the same near-black "
            "shade as the panel, so no band can be made out and the line looks "
            "unchanged from the start."
        ),
        must_not=_MUST_NOT,
        ignore=_IGNORE,
    ),
    "lzp-luanti-16679-unavailable-settings": P2POnlyTaskText(
        preconditions=(
            'The Luanti main menu is restored from the registered start on the '
            'candidate build: the local-game tab is showing, and no settings dialog '
            'is open.'
        ),
        reference_steps=(
            'Open the settings dialog and go to a page whose requirements are not '
            'currently met.'
        ),
        scene='A Luanti client window shows the engine main menu.',
        before='The local-game tab is showing. No settings dialog is open.',
        fixed_after=(
            'The settings dialog is open on its touchscreen page. Below the '
            '"Touchscreen controls" dropdown the right-hand pane carries a section '
            'heading reading "Unavailable", and under that heading several setting '
            'labels are drawn in a muted grey, packed tightly enough that they '
            'overlap one another.'
        ),
        buggy_after=(
            'The settings dialog is open on its touchscreen page. Below the '
            '"Touchscreen controls" dropdown the right-hand pane is empty: neither '
            'an "Unavailable" heading nor any muted grey setting label is drawn '
            'there.'
        ),
        must_not=(
            (
                'A blank page, an error overlay, or an unrelated application screen is '
                'shown.'
            ),
        ),
        ignore=(
            'Window chrome, font antialiasing, and cursor blink.',
        ),
        readout=(
            "Judge this frame by what the settings dialog's right-hand pane draws "
            'below its "Touchscreen controls" dropdown: whether a section of muted '
            'grey setting labels is drawn there, or that area is left empty.'
        ),
        before_ignore=(
            (
                'The main-menu Settings button, its hover tooltip, cursor position, menu '
                'decoration and background. A Settings button/tooltip is not an open '
                'settings dialog; an actual settings dialog remains forbidden at Start.'
            ),
        ),
    ),
    "lzp-luanti-16799-world-mods": P2POnlyTaskText(
        preconditions=(
            'The Luanti main menu is restored from the registered start on the '
            'candidate build: the local-game tab is showing a world that has mods in '
            'its worldmods directory, and the configure-world dialog is not open.'
        ),
        reference_steps="Open that world's configure-world dialog.",
        scene='A Luanti client window shows the engine main menu.',
        before=(
            'The local-game tab visibly lists at least one world name, and the '
            'configure-world dialog is not open. Worldmods membership is checked '
            'separately against the candidate menu observation.'
        ),
        fixed_after=(
            'The configure-world dialog is open. The select-mods list includes a '
            'worldmods group drawn among the other mod rows.'
        ),
        buggy_after=(
            'The configure-world dialog is open. The select-mods list has no '
            'worldmods group; only global and game mods are drawn.'
        ),
        must_not=(
            (
                'A blank page, an error overlay, or an unrelated application screen is '
                'shown.'
            ),
        ),
        ignore=(
            'Window chrome, font antialiasing, and cursor blink.',
        ),
        readout=(
            'Judge this frame by whether the select-mods list draws a worldmods '
            'group among the listed mods.'
        ),
        before_ignore=(
            (
                'The specific world name (p2p_world in the reference is only an '
                'example), installed-game labels, menu background and decorative '
                'controls. Do not ignore an empty world list or an open dialog.'
            ),
        ),
    ),
    "lzp-luanti-17152-server-list-search-tooltip": P2POnlyTaskText(
        preconditions=(
            "The Luanti main menu is restored from the registered start on the "
            "candidate build: the local-game tab is showing, and the online tab "
            "is not selected."
        ),
        reference_steps=(
            "Open the online tab and hover the server list search field."
        ),
        readout=(
            "Judge this frame by the tooltip painted for the server list search "
            "field: whether the listed filter examples are drawn as complete lines."
        ),
        scene=(
            "A Luanti client window shows the engine main menu."
        ),
        before=(
            "The local-game tab is showing. The online tab is not selected."
        ),
        fixed_after=(
            "The online tab is showing. The search field's tooltip lists the "
            "filter examples as complete separate lines."
        ),
        buggy_after=(
            "The online tab is showing. The search field's tooltip is truncated "
            "or misparsed, so the filter examples are not drawn as complete "
            "separate lines."
        ),
        must_not=_MUST_NOT,
        ignore=("Window chrome, font antialiasing, and cursor blink.",),
    ),
    "lzp-openra-22476-load-game-faction-filter": P2POnlyTaskText(
        preconditions=(
            "The OpenRA main menu is restored from the registered start on the "
            "candidate build: no Load Game dialog is open."
        ),
        reference_steps="Open the Load Game dialog.",
        readout=(
            "Judge this frame by whether each entry of the faction filter dropdown "
            "is the faction's display name or its internal identifier."
        ),
        scene=(
            "An OpenRA client window shows the engine main menu."
        ),
        before=(
            "The main menu is showing. The Load Game dialog is not open."
        ),
        fixed_after=(
            "The Load Game dialog is open. The faction filter dropdown paints "
            "each faction's display name."
        ),
        buggy_after=(
            "The Load Game dialog is open. The faction filter dropdown paints "
            "each faction's internal identifier rather than its display name."
        ),
        must_not=_MUST_NOT,
        ignore=("Window chrome, font antialiasing, and cursor blink.",),
    ),
    "lzp-openra-22325-color-picker-overflow": P2POnlyTaskText(
        preconditions=(
            "The OpenRA lobby is restored from the registered start on the "
            "candidate build: the color picker is closed."
        ),
        reference_steps=(
            "Open the color picker from a player slot near the right edge of the "
            "window."
        ),
        readout=(
            "Judge this frame by whether the picker panel is drawn fully inside the "
            "window or past its right edge."
        ),
        scene=(
            "An OpenRA client window shows a multiplayer lobby with player slots."
        ),
        before=(
            "The lobby is showing. No color picker panel is open."
        ),
        fixed_after=(
            "A color picker panel is open and sits fully inside the window, "
            "shifted left of its parent control so its right edge is on-screen."
        ),
        buggy_after=(
            "A color picker panel is open and extends past the right edge of "
            "the window, so part of the panel is not drawn."
        ),
        must_not=_MUST_NOT,
        ignore=("Window chrome, font antialiasing, and cursor blink.",),
    ),
    "lzp-openra-22283-tileset-template-order": P2POnlyTaskText(
        preconditions=(
            'The OpenRA map editor is restored from the registered start on the '
            'candidate build: the tile template selector is closed or empty of the '
            'listing under study.'
        ),
        reference_steps='Open the tile template selector.',
        scene=(
            'A live OpenRA map editor is visible, not the main menu or a '
            'loading/error screen.'
        ),
        before=(
            'The tile-template palette is not displaying a populated template '
            'listing yet. A different palette, such as overlays, may be open. '
            'Search/Filter controls identifying a populated tile-template listing '
            'are not a valid pre-transition Start.'
        ),
        fixed_after=(
            'The tile template selector is open in the lower right panel, under a '
            '"Search:" field and a filter reading "All". Its grid begins with two '
            'plain pale tiles carrying no water, and the blue speckled water '
            'templates are drawn after them.'
        ),
        buggy_after=(
            'The tile template selector is open in the lower right panel, under a '
            '"Search:" field and a filter reading "All". Its grid begins with the '
            'blue speckled water templates, and the plain pale tiles and the shore '
            'templates are drawn after them.'
        ),
        must_not=(
            (
                'A blank page, an error overlay, or an unrelated application screen is '
                'shown.'
            ),
        ),
        ignore=(
            'Window chrome, font antialiasing, and cursor blink.',
            (
                'Map identity, terrain arrangement, camera position, minimap contents, '
                'resource counts, viewport size and UI scale. These are not specified by '
                'the public Start condition.'
            ),
        ),
        readout=(
            'Judge this frame by what the first cells of the tile template '
            "selector's grid show, read left to right from its top-left cell: "
            'whether the plain pale tiles or the blue speckled water templates are '
            'drawn first.'
        ),
        before_ignore=(
            (
                'Content of the non-tile palette (for example overlay icons) and '
                'unrelated editor toolbar layout. Do not ignore whether the '
                'tile-template listing is already populated.'
            ),
        ),
    ),
}


def raw_initial(task_id: str) -> str:
    """Return the public real-cold-start state owned by the wording registry."""

    if task_id.startswith("lzp-jupyterlab-"):
        return (
            "A real candidate JupyterLab process has cold-started at the Launcher, "
            "and the benchmark task workspace is not open."
        )
    if task_id.startswith("lzp-luanti-"):
        return (
            "Luanti has cold-started at the neutral About tab; the local-game tab "
            "is not selected and no dialog is open."
        )
    if task_id == "lzp-openra-22283-tileset-template-order":
        return (
            "OpenRA has cold-started at the post-boot main menu; the map editor "
            "is not open."
        )
    if task_id == "lzp-openra-22476-load-game-faction-filter":
        return (
            "OpenRA has cold-started at the post-boot main menu and no Load Game "
            "dialog is open."
        )
    raise KeyError(f"P2P-only raw initial is not registered: {task_id}")


def public_contract(task_id: str) -> tuple[str, str] | None:
    """Return ``(preconditions, reference_steps)`` for one registered task."""

    text = TASK_TEXT.get(task_id)
    if text is None:
        return None
    return text.preconditions, text.reference_steps


__all__ = ["P2POnlyTaskText", "TASK_TEXT", "public_contract", "raw_initial"]
