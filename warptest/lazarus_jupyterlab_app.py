"""The registered JupyterLab application replay for formal Lazarus rows.

Goldens cannot be captured against a materialized version tree as published.
``dev_mode/static`` is bundled before the fixed/buggy pair is materialized, so
the browser would load the same application for both versions and the after
frames could never expose a differential outcome.  This module therefore
rebuilds the registered package scopes and the ``dev_mode`` bundle inside a
disposable clone of each version, drives one registered replay in a real
browser, and hands the four resulting frames to :mod:`warptest.lazarus_goldens`.

One driver serves every formal JupyterLab row.  A task contributes only its
registered replay: how to reach the common start, which reference steps to
perform, and which observable the hidden oracle reads.  The driver owns the
build recipe, the browser lifecycle and the per-stage evidence, so a new row
cannot quietly acquire a different provenance.

Two consumers drive the same replay, which is why it lives here rather than
inside either of them: :func:`capture_jupyterlab_lazarus_goldens` publishes its
frames, and :mod:`warptest.lazarus_jupyterlab_app_probe` grades a trial from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c1_sandbox import strict_clone_tree
from .jupyterlab_runtime import JupyterLabRuntimeAdapter
from .lazarus_formal_build import JUPYTER_FORMAL_BUILD_SCOPES
from .lazarus_goldens import (
    LazarusGoldenFrame,
    LazarusGoldenSet,
    register_lazarus_golden_set,
)
from .lazarus_versions import tree_fingerprint


_JLPM = "/opt/miniconda3/bin/jlpm"
_JUPYTER_RUNTIME_PYTHON = "/opt/miniconda3/bin/python"
_VERSIONS = ("fixed", "buggy")
_VIEWPORT_SETTLE_MS = 900
# Bumped whenever :func:`prepare_version_clone` changes what it builds, so a
# clone can never outlive the recipe that produced it.
_BUILD_RECIPE = "jupyterlab-scope-and-dev-mode-build-v1"


class JupyterLabAppError(RuntimeError):
    """Raised when a registered JupyterLab golden capture cannot proceed."""


@dataclass(frozen=True)
class JupyterLabAppProfile:
    """One task's registered replay plus the observable its oracle reads.

    ``goal_value`` is the observable the *fixed* product must produce and
    ``defect_value`` the one the *buggy* product must produce.  Both are
    asserted, so a capture cannot publish frames whose semantics drifted from
    the construction truth table.

    ``start_value`` is asserted on *both* versions before the replay runs.  Two
    frames can be pixel-equivalent and still show the wrong notebook, the wrong
    selected cell or the wrong search configuration, so the start state needs an
    observation of its own rather than an inference from image similarity.
    """

    task_id: str
    change_number: int
    # The named steps this replay performs.  The oracle spec publishes the
    # same tuple, so the two cannot describe different sequences.
    registered_replay: tuple[str, ...]
    audit_field: str
    goal_value: str
    defect_value: str
    start_field: str
    start_value: str
    start: Callable[["JupyterLabAppContext"], None]
    replay: Callable[["JupyterLabAppContext"], None]
    read: Callable[["JupyterLabAppContext"], str]
    read_start: Callable[["JupyterLabAppContext"], str]

    @property
    def output_dir(self) -> str:
        return f"specs/jupyterlab/lazarus/visual_{self.task_id}"


@dataclass
class JupyterLabAppContext:
    """Live handles a registered replay is allowed to use."""

    runtime: JupyterLabRuntimeAdapter
    page: Any
    version: str

    def open_notebook(self, path: str, notebook: Mapping[str, Any]) -> None:
        """Publish a notebook through the server API and open it in the UI."""

        self.runtime.put_notebook(path, dict(notebook))
        self.goto(f"{self.runtime.app_url}/tree/{path}")
        self.page.wait_for_selector(".jp-NotebookPanel", timeout=60_000)
        self.page.wait_for_timeout(_VIEWPORT_SETTLE_MS)

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_selector(".jp-LabShell", timeout=60_000)
        self.page.locator("#jupyterlab-splash").wait_for(
            state="detached", timeout=60_000
        )
        # The Lazarus replays read rendered widgets, so wait for the plugin
        # graph rather than a bare DOM ready signal.
        self.page.wait_for_function(
            "() => typeof window.jupyterapp === 'object'", timeout=60_000
        )
        self.page.evaluate("() => window.jupyterapp.started")
        self.page.wait_for_timeout(_VIEWPORT_SETTLE_MS)

    def settle(self, milliseconds: int = _VIEWPORT_SETTLE_MS) -> None:
        self.page.wait_for_timeout(milliseconds)

    def quiesce_chrome(self) -> None:
        """Remove transient shell chrome that would make a frame irreproducible.

        The news prompt and update dialogs appear on their own schedule, so a
        frame that captured one would compare against a frame that did not.
        """

        JupyterLabRuntimeAdapter._dismiss_transient_dialogs(self.page)
        JupyterLabRuntimeAdapter._remove_news_prompt(self.page)
        self.page.wait_for_timeout(200)
        JupyterLabRuntimeAdapter._remove_news_prompt(self.page)


def capture_jupyterlab_lazarus_goldens(
    task_id: str,
    *,
    repository_root: str | Path,
    prebuilt_root: str | Path,
    sandbox_root: str | Path,
    output_dir: str | Path | None = None,
    build_timeout_s: float = 3_600.0,
) -> LazarusGoldenSet:
    """Capture and publish one formal JupyterLab task's golden triplet."""

    profile = JUPYTERLAB_APP_PROFILES.get(str(task_id).strip())
    if profile is None:
        raise JupyterLabAppError(
            f"no registered JupyterLab golden profile for {task_id}"
        )
    root = Path(repository_root).expanduser().resolve()
    prebuilt = Path(prebuilt_root).expanduser().resolve()
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    captures: dict[str, dict[str, Any]] = {}
    for version in _VERSIONS:
        source = prebuilt / profile.task_id / version
        if not source.is_dir():
            raise JupyterLabAppError(
                f"materialized {version} version does not exist: {source}"
            )
        clone = prepared_version_clone(
            profile.task_id,
            version,
            source=source,
            prebuilt_root=prebuilt,
            clone=sandbox / f"{profile.task_id}-{version}",
            timeout_s=build_timeout_s,
        )
        captures[version] = _capture_version(profile, clone, sandbox, version)

    # Each frame is backed by the receipt for its own stage, and its semantic
    # verdict comes from that receipt rather than from the caller.
    frames = {
        f"{version}_{stage}": LazarusGoldenFrame(
            image=Path(captures[version][stage]["image"]),
            report=Path(captures[version][stage]["report"]),
            expected_semantics_ok=captures[version][stage]["semantics_ok"],
        )
        for version in _VERSIONS
        for stage in ("before", "after")
    }
    return register_lazarus_golden_set(
        task_id=profile.task_id,
        repository_root=root,
        output_dir=output_dir if output_dir is not None else root / profile.output_dir,
        fixed_before=frames["fixed_before"],
        buggy_before=frames["buggy_before"],
        fixed_after=frames["fixed_after"],
        buggy_after=frames["buggy_after"],
        # The JupyterLab shell has no volatile header band comparable to the
        # Luanti HUD clock, so the whole frame participates in both metrics.
        ignore_top_fraction=0.0,
    )


def prepare_version_clone(
    task_id: str, clone: Path, *, timeout_s: float
) -> None:
    """Rebuild the registered scopes and the served bundle inside one clone."""

    scopes = JUPYTER_FORMAL_BUILD_SCOPES.get(task_id)
    if not scopes:
        raise JupyterLabAppError(
            f"formal JupyterLab task has no registered build scope: {task_id}"
        )
    commands: list[tuple[tuple[str, ...], str]] = [
        (
            (
                _JLPM,
                "run",
                "build:packages:scope",
                "--scope",
                f"@jupyterlab/{scope}",
            ),
            ".",
        )
        for scope in scopes
    ]
    commands.append(((_JLPM, "run", "build"), "dev_mode"))
    for argv, cwd in commands:
        completed = subprocess.run(
            list(argv),
            cwd=str(clone / cwd),
            capture_output=True,
            timeout=timeout_s,
            shell=False,
            env=dict(os.environ),
        )
        if completed.returncode != 0:
            detail = (
                completed.stderr.decode(errors="replace")
                or completed.stdout.decode(errors="replace")
            ).strip()
            raise JupyterLabAppError(
                f"golden capture build failed in {cwd}: {' '.join(argv)}: "
                f"{detail[-800:]}"
            )


def version_clone_fingerprint(
    task_id: str, version: str, *, source: Path, prebuilt_root: Path
) -> str:
    """Identify every input a prepared clone of one version is derived from."""

    manifest_path = prebuilt_root.parent / "metadata" / task_id / "manifest.json"
    if manifest_path.is_file():
        # The materialization manifest already pins the source revision, the
        # revert patch and each version's tree hash.
        source_identity: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        source_identity = {"tree_sha256": tree_fingerprint(source)}
    payload = {
        "build_recipe": _BUILD_RECIPE,
        "build_scopes": list(JUPYTER_FORMAL_BUILD_SCOPES.get(task_id, ())),
        "source_identity": source_identity,
        "task_id": task_id,
        "version": version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def prepared_version_clone(
    task_id: str,
    version: str,
    *,
    source: Path,
    prebuilt_root: Path,
    clone: Path,
    timeout_s: float,
) -> Path:
    """Return a built clone of one version, rebuilt whenever its inputs moved.

    Rebuilding the bundle costs an hour, so clones are cached, but a cache that
    cannot expire would serve an audit the product of a superseded source tree,
    revert patch or build recipe.  The stamp is written only once the build has
    succeeded, so an interrupted preparation is retried rather than trusted.
    """

    fingerprint = version_clone_fingerprint(
        task_id, version, source=source, prebuilt_root=prebuilt_root
    )
    stamp = clone.parent / f"{clone.name}.fingerprint"
    if clone.exists():
        recorded = (
            stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
        )
        if recorded == fingerprint:
            return clone
        shutil.rmtree(clone)
        stamp.unlink(missing_ok=True)
    clone.parent.mkdir(parents=True, exist_ok=True)
    strict_clone_tree(source, clone)
    prepare_version_clone(task_id, clone, timeout_s=timeout_s)
    stamp.write_text(fingerprint + "\n", encoding="utf-8")
    return clone


def drive_registered_replay(
    profile: JupyterLabAppProfile,
    clone: Path,
    report_dir: Path,
    version: str,
    *,
    on_stage: Callable[[str, Any, Path], Path | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run one version's registered replay and return its per-stage observations.

    Both stages record the runtime's browser error channels, because the spec's
    ``no_jupyterlab_probe_errors`` invariant is graded from them and would
    otherwise be asserted from nothing.

    ``on_stage`` is called once the stage has settled, before its observable is
    read, so a caller that also needs a frame can take one from the same run
    rather than replaying the task a second time.
    """

    report_dir.mkdir(parents=True, exist_ok=True)
    stages: dict[str, dict[str, Any]] = {}
    with JupyterLabRuntimeAdapter(
        clone,
        report_dir,
        python_executable=_JUPYTER_RUNTIME_PYTHON,
        galata_mode=True,
    ) as runtime:
        page = runtime._ensure_browser_page()
        context = JupyterLabAppContext(runtime=runtime, page=page, version=version)
        for stage, drive, read, field, expected in (
            (
                "before",
                profile.start,
                profile.read_start,
                profile.start_field,
                profile.start_value,
            ),
            (
                "after",
                profile.replay,
                profile.read,
                profile.audit_field,
                profile.goal_value if version == "fixed" else profile.defect_value,
            ),
        ):
            drive(context)
            context.quiesce_chrome()
            artifact = on_stage(stage, page, report_dir) if on_stage else None
            stages[stage] = {
                "image": artifact,
                "field": field,
                "observed": read(context),
                "expected": expected,
                "errors": _drain_errors(runtime),
            }
        stages["browser_version"] = getattr(runtime.browser, "version", None)
    return stages


def _capture_version(
    profile: JupyterLabAppProfile,
    clone: Path,
    sandbox: Path,
    version: str,
) -> dict[str, Any]:
    """Drive one registered replay and record a golden receipt for each stage."""

    report_dir = sandbox / "reports" / f"{profile.task_id}-{version}"

    def screenshot(stage: str, page: Any, directory: Path) -> Path:
        image = directory / f"{stage}.png"
        page.screenshot(path=str(image))
        return image

    stages = drive_registered_replay(
        profile, clone, report_dir, version, on_stage=screenshot
    )
    stages.pop("browser_version", None)
    captured: dict[str, Any] = {}
    for stage, values in stages.items():
        errors = values["errors"]
        invariants_ok = not errors["console_errors"] and not errors["page_errors"]
        audit_ok = values["observed"] == values["expected"]
        if not audit_ok:
            raise JupyterLabAppError(
                f"{profile.task_id} {version} {stage} observed "
                f"{values['field']}={values['observed']!r}, "
                f"expected {values['expected']!r}"
            )
        if not invariants_ok:
            raise JupyterLabAppError(
                f"{profile.task_id} {version} {stage} recorded browser errors: "
                f"{errors}"
            )
        report = {
            "schema": "c1-lazarus-jupyterlab-golden-capture-v1",
            "schema_version": 2,
            "task_id": profile.task_id,
            "change_number": profile.change_number,
            "version": version,
            "stage": stage,
            "audit": {
                "field": values["field"],
                "observed": values["observed"],
                "expected": values["expected"],
                "ok": audit_ok,
            },
            "invariants": {
                "check": "no_jupyterlab_probe_errors",
                "console_errors": errors["console_errors"],
                "page_errors": errors["page_errors"],
                "ok": invariants_ok,
            },
            "live_source": "jupyterlab_browser_capture",
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        report_path = report_dir / f"{stage}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        captured[stage] = {
            "image": str(values["image"]),
            "report": str(report_path),
            "observed": values["observed"],
            "semantics_ok": audit_ok and invariants_ok,
        }
    return captured


def _drain_errors(runtime: JupyterLabRuntimeAdapter) -> dict[str, list[str]]:
    """Take and clear the browser error channels recorded for one stage."""

    errors = {
        "console_errors": list(runtime.console_errors),
        "page_errors": list(runtime.page_errors),
    }
    runtime.console_errors.clear()
    runtime.page_errors.clear()
    return errors


def _code_notebook(source: str) -> dict[str, Any]:
    return {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source,
            }
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def _markdown_notebook(cells: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {
        "cells": [
            {
                "cell_type": kind,
                "metadata": {},
                "source": source,
                **({} if kind == "markdown" else {"execution_count": None, "outputs": []}),
            }
            for kind, source in cells
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def _activate_property_inspector(context: JupyterLabAppContext) -> None:
    tools = context.page.locator(".jp-NotebookTools")
    if tools.count() > 0 and tools.is_visible():
        return
    context.page.locator(".lm-TabBar-tab[title='Property Inspector']").click()
    context.page.wait_for_selector(".jp-NotebookTools", timeout=30_000)
    context.settle()


def _expand_notebook_tools_section(
    context: JupyterLabAppContext, label: str
) -> Any:
    section = context.page.locator(".jp-NotebookTools .jp-Collapse", has_text=label)
    if section.locator(".jp-Collapse-contents.lm-mod-hidden").count() > 0:
        section.click()
        context.settle()
    return section


_METADATA_NOTEBOOK = "lazarus-17194/code_notebook.ipynb"


def _start_17194(context: JupyterLabAppContext) -> None:
    context.open_notebook(
        _METADATA_NOTEBOOK, _code_notebook("a = 1\nb = 2\nc = a + b\n")
    )
    context.page.locator(".jp-Cell").first.click()
    _activate_property_inspector(context)
    _expand_notebook_tools_section(context, "Advanced Tools")
    context.page.wait_for_selector(
        ".jp-CellMetadataEditor .cm-content", timeout=30_000
    )
    context.settle()


def _start_state_17194(context: JupyterLabAppContext) -> str:
    text = context.page.locator(".jp-CellMetadataEditor .cm-content").inner_text()
    return "trusted_entry_present" if "trusted" in text else "trusted_entry_absent"


def _replay_17194(context: JupyterLabAppContext) -> None:
    editor = context.page.locator(".jp-CellMetadataEditor .cm-content")
    trusted = editor.locator(".cm-line").filter(
        has=context.page.locator("span", has_text="trusted")
    )
    trusted.first.evaluate("element => element.remove()")
    context.page.locator(
        ".jp-CellMetadataEditor .jp-JSONEditor-header "
        "[title='Commit changes to data']"
    ).click()
    context.settle()
    context.page.locator(".lm-TabBar-tab[title='Property Inspector']").click()
    context.settle(400)
    _activate_property_inspector(context)
    _expand_notebook_tools_section(context, "Advanced Tools")
    context.settle()


def _read_17194(context: JupyterLabAppContext) -> str:
    text = context.page.locator(
        ".jp-CellMetadataEditor .cm-content"
    ).inner_text()
    return "empty" if "trusted" not in text else "retains_trusted"


_TOOLTIP_NOTEBOOK = "lazarus-16343/tooltip_notebook.ipynb"


def _start_16343(context: JupyterLabAppContext) -> None:
    context.open_notebook(_TOOLTIP_NOTEBOOK, _code_notebook("len\n"))
    context.page.locator(".jp-Cell .cm-content").first.click()
    context.settle()


def _start_state_16343(context: JupyterLabAppContext) -> str:
    editor = context.page.locator(".jp-Cell .cm-content").first.inner_text().strip()
    tooltip = context.page.locator(".jp-Tooltip").count()
    return "name_typed_no_tooltip" if editor == "len" and tooltip == 0 else "unexpected"


def _replay_16343(context: JupyterLabAppContext) -> None:
    context.page.keyboard.press("End")
    context.page.keyboard.press("Shift+Tab")
    context.settle(1_500)


def _read_16343(context: JupyterLabAppContext) -> str:
    visible = context.page.locator(".jp-Tooltip").count() > 0
    return "tooltip_shown" if visible else "tooltip_absent"


_MARKDOWN_NOTEBOOK = "lazarus-15634/markdown_notebook.ipynb"


def _start_15634(context: JupyterLabAppContext) -> None:
    context.open_notebook(
        _MARKDOWN_NOTEBOOK,
        _markdown_notebook(
            (
                ("markdown", "# Lazarus rendered heading\n"),
                ("code", "value = 1\n"),
            )
        ),
    )
    # Select the rendered markdown cell in command mode: the registered start
    # is the markdown cell active and rendered, not being edited.
    context.page.locator(".jp-MarkdownCell.jp-mod-rendered").first.wait_for(
        timeout=30_000
    )
    context.page.locator(".jp-MarkdownCell .jp-MarkdownOutput").first.click()
    context.settle()


def _start_state_15634(context: JupyterLabAppContext) -> str:
    rendered = context.page.locator(".jp-MarkdownCell.jp-mod-rendered").count()
    code_cells = context.page.locator(".jp-CodeCell").count()
    return (
        "markdown_rendered_beside_code"
        if rendered >= 1 and code_cells >= 1
        else "unexpected"
    )


def _replay_15634(context: JupyterLabAppContext) -> None:
    # Move focus into the *other* cell's editor, which switches the notebook to
    # edit mode while the markdown cell is the previously active one.
    context.page.locator(".jp-CodeCell .cm-content").first.click()
    context.settle(1_200)


def _read_15634(context: JupyterLabAppContext) -> str:
    rendered = context.page.locator(".jp-MarkdownCell.jp-mod-rendered").count()
    return "previous_rendered" if rendered >= 1 else "previous_unrendered"


_SEARCH_NOTEBOOK = "lazarus-16943/search_notebook.ipynb"
_SEARCH_VALUES = (
    ("alpha", "123"),
    ("beta", "234"),
    ("gamma", "345"),
    ("delta", "456"),
    ("epsilon", "567"),
    ("zeta", "678"),
    ("eta", "789"),
    ("theta", "890"),
)
_SEARCH_MARKDOWN = "## Lazarus replace-all fixture\n\n" + "".join(
    f"- {name}={digits}\n" for name, digits in _SEARCH_VALUES
)


def _start_16943(context: JupyterLabAppContext) -> None:
    # The reverted provider drops its replace options only for markdown cells,
    # so the registered start must carry the searched text in a markdown cell.
    context.open_notebook(
        _SEARCH_NOTEBOOK,
        _markdown_notebook(
            (
                ("markdown", _SEARCH_MARKDOWN),
                ("code", "kappa=901\n"),
            )
        ),
    )
    context.page.locator(".jp-MarkdownCell.jp-mod-rendered").first.wait_for(
        timeout=30_000
    )
    context.page.locator(".jp-Cell").first.click()
    # The keyboard accelerator is platform dependent; the registered command is
    # the same surface the Find dialog binds to.
    context.page.evaluate(
        "() => window.jupyterapp.commands.execute"
        "('documentsearch:startWithReplace')"
    )
    overlay = context.page.locator(".jp-DocumentSearch-overlay")
    overlay.wait_for(timeout=30_000)
    overlay.locator("button[title='Use Regular Expression']").click()
    overlay.locator("textarea[title='Find']").fill(r"(\d+)")
    overlay.locator("textarea[title='Replace']").fill("$1+1")
    context.settle(1_500)


def _start_state_16943(context: JupyterLabAppContext) -> str:
    markdown = context.page.locator(".jp-MarkdownCell").first.inner_text()
    overlay = context.page.locator(".jp-DocumentSearch-overlay").count()
    present = all(
        f"{name}={digits}" in markdown for name, digits in _SEARCH_VALUES
    )
    return (
        "digit_values_present"
        if overlay == 1 and present and "$1" not in markdown
        else "unexpected"
    )


def _replay_16943(context: JupyterLabAppContext) -> None:
    # Once a query matches, the replace controls drop their ``title`` and are
    # identified only by their label, so assert the label before clicking.
    buttons = context.page.locator(
        ".jp-DocumentSearch-overlay .jp-DocumentSearch-replace-button-wrapper"
    )
    replace_all = buttons.nth(1)
    label = replace_all.inner_text().strip()
    if label != "Replace All":
        raise JupyterLabAppError(
            f"expected the Replace All control, found {label!r}"
        )
    replace_all.click()
    context.settle(1_500)


def _read_16943(context: JupyterLabAppContext) -> str:
    # The goal requires every fixture value to carry its own expanded match, so
    # a query that matched nothing, a replacement that never landed or a cleared
    # cell reads as neither the goal nor the defect.
    markdown = context.page.locator(".jp-MarkdownCell").first.inner_text()
    if "$1" in markdown:
        return "backreference_literal"
    if all(f"{name}={digits}+1" in markdown for name, digits in _SEARCH_VALUES):
        return "backreference_expanded"
    return "unexpected"


JUPYTERLAB_APP_PROFILES: Mapping[str, JupyterLabAppProfile] = {
    profile.task_id: profile
    for profile in (
        JupyterLabAppProfile(
            task_id="lz-jupyterlab-17194-cell-metadata-removal",
            registered_replay=(
                "load_materialized_dev_mode_bundle",
                "restore_registered_metadata_start",
                "remove_metadata_entry_and_commit",
                "read_cell_metadata_editor_text",
            ),
            change_number=17194,
            audit_field="cell_metadata_state",
            goal_value="empty",
            defect_value="retains_trusted",
            start_field="cell_metadata_start_state",
            start_value="trusted_entry_present",
            start=_start_17194,
            replay=_replay_17194,
            read=_read_17194,
            read_start=_start_state_17194,
        ),
        JupyterLabAppProfile(
            task_id="lz-jupyterlab-16343-shift-tab-tooltip",
            registered_replay=(
                "load_materialized_dev_mode_bundle",
                "restore_registered_code_cell_start",
                "press_shift_tab_once",
                "read_inspector_tooltip_state",
            ),
            change_number=16343,
            audit_field="tooltip_state",
            goal_value="tooltip_shown",
            defect_value="tooltip_absent",
            start_field="tooltip_start_state",
            start_value="name_typed_no_tooltip",
            start=_start_16343,
            replay=_replay_16343,
            read=_read_16343,
            read_start=_start_state_16343,
        ),
        JupyterLabAppProfile(
            task_id="lz-jupyterlab-15634-markdown-focus-render",
            registered_replay=(
                "load_materialized_dev_mode_bundle",
                "restore_registered_rendered_markdown_start",
                "focus_other_cell_editor",
                "read_markdown_render_state",
            ),
            change_number=15634,
            audit_field="markdown_render_state",
            goal_value="previous_rendered",
            defect_value="previous_unrendered",
            start_field="markdown_start_state",
            start_value="markdown_rendered_beside_code",
            start=_start_15634,
            replay=_replay_15634,
            read=_read_15634,
            read_start=_start_state_15634,
        ),
        JupyterLabAppProfile(
            task_id="lz-jupyterlab-16943-markdown-regex-replace-all",
            registered_replay=(
                "load_materialized_dev_mode_bundle",
                "restore_registered_search_start",
                "apply_registered_replace_all",
                "read_markdown_cell_text",
            ),
            change_number=16943,
            audit_field="regex_replace_state",
            goal_value="backreference_expanded",
            defect_value="backreference_literal",
            start_field="regex_replace_start_state",
            start_value="digit_values_present",
            start=_start_16943,
            replay=_replay_16943,
            read=_read_16943,
            read_start=_start_state_16943,
        ),
    )
}
