"""Oracle specs and catalog rows for the C1-Lazarus roster expansion.

The frozen 24 rows were emitted from freeze-sprint evidence, so their writer is
keyed to that closure.  The expansion rows are established differently: their
truth table comes from a live construction run and their frames from a live
capture.  This module keeps that second provenance explicit and generates both
artifacts from one descriptor, so a row's public text, hidden oracle and golden
paths cannot drift apart.

Public text carries only the registered start and the reference steps.  The
catalog validator additionally refuses any row whose public text hints at a
defect or repeats a distinguishing expected value, so the descriptions here
deliberately name observable surfaces rather than outcomes.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .lazarus_jupyterlab_app import JUPYTERLAB_APP_PROFILES
from .task_descriptions import canonical_state_description


_SHARED_MUST_NOT = (
    "A blank page, an error overlay, or an unrelated application screen is shown.",
)
_SHARED_IGNORE = (
    "Font antialiasing, caret blink, scrollbar rendering, window chrome, and the "
    "file browser's relative modification times.",
)
_LUANTI_MUST_NOT = (
    "A main menu, a crash dialog, or a blank application frame is shown.",
)
_LUANTI_IGNORE = (
    "Server banner text, join messages, the frame counter, and transient chat "
    "lines above the rendered world.",
)


@dataclass(frozen=True)
class ExpansionRow:
    """One expansion row's public contract, hidden oracle and golden frames."""

    task_id: str
    benchmark: str
    change_number: int
    issue_number: int | None
    defect_locus: str
    horizon_class: str
    capabilities: tuple[str, ...]
    fix_commit: str
    pull_request: str
    issue_url: str | None
    build_class: str
    preconditions: str
    reference_steps: str
    audit_check: str
    audit_field: str
    goal_value: str
    defect_value: str
    # The observation both versions must produce before the replay runs.  It is
    # what lets the gate say the two products started from the registered state
    # rather than merely from states that render alike.
    start_field: str
    start_value: str
    invariant_check: str
    registered_replay: tuple[str, ...]
    adapter: str
    checkpoint_strategy: str
    target: dict[str, Any]
    start_frame_description: str
    start_must: tuple[str, ...]
    fixed_after_description: str
    fixed_after_must: tuple[str, ...]
    buggy_after_description: str
    buggy_after_must: tuple[str, ...]
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    per_trial_p2p: tuple[str, ...]
    timeout_ms: int
    must_not: tuple[str, ...] = _SHARED_MUST_NOT
    ignore: tuple[str, ...] = _SHARED_IGNORE
    extra_invariants: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # OpenRA 21453 entered the roster with the Phase-0 spec its registered GUI
    # gate already drives, so this writer only supplies its catalog row.
    generate_spec: bool = True
    # A row is executable only once a runtime binding can drive its replay and
    # score its audit.  Every expansion row currently has construction and
    # golden evidence but no such binding, so the spec must say so rather than
    # let a future runner infer that the probe can be dispatched.
    unbound_reason: str | None = None

    @property
    def probe_executable(self) -> bool:
        return self.unbound_reason is None

    @property
    def visual_dir(self) -> str:
        return f"visual_{self.task_id}"

    @property
    def spec_path(self) -> str:
        return f"specs/{self.benchmark}/lazarus/{self.task_id}.yaml"

    @property
    def goldens_path(self) -> str:
        return f"specs/{self.benchmark}/lazarus/{self.task_id}.goldens.yaml"

    @property
    def defect_dir(self) -> str:
        return f"benchmarks/p5/lazarus/defects/{self.task_id}"


def _rubric(must: tuple[str, ...], row: ExpansionRow) -> dict[str, Any]:
    return {
        "must": list(must),
        "must_not": list(row.must_not),
        "ignore": list(row.ignore),
    }


def _goldens_block(row: ExpansionRow) -> dict[str, Any]:
    return {
        "before": f"{row.visual_dir}/before.golden.png",
        "buggy_before": f"{row.visual_dir}/buggy.before.golden.png",
        "fixed": {
            "after": f"{row.visual_dir}/fixed.after.golden.png",
            "after_description": row.fixed_after_description,
            "after_rubric": _rubric(row.fixed_after_must, row),
        },
        "buggy": {
            "after": f"{row.visual_dir}/buggy.after.golden.png",
            "after_description": row.buggy_after_description,
            "after_rubric": _rubric(row.buggy_after_must, row),
        },
    }


def _visual_oracle(row: ExpansionRow) -> dict[str, Any]:
    return {
        "golden_image": f"{row.visual_dir}/fixed.after.golden.png",
        "expected_label": "correct",
        "description": row.start_frame_description,
        "rubric": _rubric(row.start_must, row),
        "capture": {"stage": "post_smoke", "wait_ms": 1000, "full_page": False},
    }


def _surface(row: ExpansionRow) -> dict[str, Any]:
    return {
        "adapter": row.adapter,
        "start": {"validate_target": True, "checks": []},
        "goal": {"use_assertions": True, "checks": []},
    }


def build_oracle_spec(row: ExpansionRow) -> dict[str, Any]:
    """Return the private oracle spec for one expansion row."""

    invariants: list[dict[str, Any]] = [{"type": row.invariant_check}]
    invariants.extend(copy.deepcopy(list(row.extra_invariants)))
    probe: dict[str, Any] = {
        "task_id": row.task_id,
        "change_number": row.change_number,
        "issue_number": row.issue_number,
        "adapter": row.adapter,
        "observation_source": "live_materialized_replay",
        "registered_replay": list(row.registered_replay),
        "probe_executable": row.probe_executable,
    }
    if row.unbound_reason is not None:
        probe["unbound_reason"] = row.unbound_reason
    spec: dict[str, Any] = {
        "name": f"C1-Lazarus {row.benchmark.capitalize()} "
        f"{row.change_number}-{row.task_id.split('-', 3)[-1]}",
        "benchmark": row.benchmark,
        "timeout_ms": row.timeout_ms,
        "tags": ["c1", "lazarus", row.benchmark, "production"],
        "setup": {f"{row.benchmark}_lazarus_probe": probe},
        "target": copy.deepcopy(row.target),
        "checkpoint": {
            "allowed_strategies": [row.checkpoint_strategy],
            "expected_validity": "replay-reachable",
        },
        "actions": [],
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                "assertions": [],
                "surfaces": {"gui": _surface(row), "state": _surface(row)},
            },
            "visual_oracle": _visual_oracle(row),
            "lazarus": {
                "schema_version": 1,
                "goal_checks": [
                    {
                        "type": row.audit_check,
                        "field": row.audit_field,
                        "expected": row.goal_value,
                    }
                ],
                "defect_signature": [
                    {
                        "type": row.audit_check,
                        "field": row.audit_field,
                        "expected": row.defect_value,
                    }
                ],
                "invariant_checks": invariants,
                "upstream_tests": {
                    "fail_to_pass": list(row.fail_to_pass),
                    "pass_to_pass": list(row.pass_to_pass),
                    # The registered per-trial control, which the contract
                    # requires to be one of the pass_to_pass ids so the gate
                    # already established it on both versions.
                    "per_trial_p2p": list(row.per_trial_p2p),
                },
                "goldens": _goldens_block(row),
            },
        },
    }
    return spec


def build_goldens_sidecar(
    row: ExpansionRow, *, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Return the published golden-provenance sidecar for one expansion row."""

    return {
        "schema": "c1-lazarus-task-goldens-v1",
        "schema_version": 1,
        "task_id": row.task_id,
        "benchmark": row.benchmark,
        "candidate": {
            "repository_key": row.benchmark,
            "change_number": row.change_number,
            "issue_number": row.issue_number,
            "defect_locus": row.defect_locus,
        },
        "authoritative_defect": None,
        "provenance": {
            # An expansion row has no freeze admission: its frames were captured
            # from the materialized pair rather than republished from Gate 11.
            "formal_freeze_admission": None,
            "frame_sources": {
                name: "live_materialized_capture"
                for name in ("fixed_before", "fixed_after", "buggy_before", "buggy_after")
            },
            "goldens_metadata": f"{row.visual_dir}/goldens.meta.json",
            "before_equivalence": metadata["before_equivalence"],
            "after_differential": metadata["after_differential"],
            "golden_policy": metadata["policy"],
            "captured_by": metadata["source"],
        },
        "evaluation": {
            "visual_oracle": _visual_oracle(row),
            "lazarus": {
                "schema_version": 1,
                "runtime_status": "probe_runtime_available",
                "probe": {
                    "adapter": row.adapter,
                    "spec": f"{row.task_id}.yaml",
                    "registered_replay": list(row.registered_replay),
                    "versions_replayed": ["fixed", "buggy"],
                    "re_executed_offline": True,
                    "provider_calls_required": False,
                },
                "upstream_tests": {
                    "fail_to_pass": list(row.fail_to_pass),
                    "pass_to_pass": list(row.pass_to_pass),
                },
                "goldens": _goldens_block(row),
            },
        },
    }


def build_catalog_row(row: ExpansionRow, *, patch_sha256: str) -> dict[str, Any]:
    """Return the public catalog row for one expansion row."""

    from .c1_catalog import expected_random_control

    upstream: dict[str, Any] = {"pr": row.pull_request}
    if row.issue_url:
        upstream["issue"] = row.issue_url
    return {
        "id": row.task_id,
        "benchmark": row.benchmark,
        "repo": _REPOS[row.benchmark],
        "prompt_revision": "lz-v1",
        "random_control": expected_random_control(row.benchmark),
        "preconditions": row.preconditions,
        "reference_steps": row.reference_steps,
        "state_description": canonical_state_description(
            row.preconditions, row.reference_steps
        ),
        "horizon_class": row.horizon_class,
        "oracle_spec": row.spec_path,
        "tags": ["c1", "lazarus", row.benchmark],
        "capabilities": list(row.capabilities),
        "budget_ms": 2_400_000,
        "defect": {
            "upstream": upstream,
            "fix_commit": row.fix_commit,
            "revert_patch": f"{row.defect_dir}/formal-v1/revert.patch",
            "patch_sha256": patch_sha256,
            "build_class": row.build_class,
            "defect_locus": row.defect_locus,
            "upstream_record": f"{row.defect_dir}/formal-v1/upstream.json",
            "gate_report": f"{row.defect_dir}/formal-v1/gate.json",
        },
    }


_REPOS = {
    "luanti": "games/luanti",
    "blender": "games/blender",
    "jupyterlab": "games/jupyterlab",
    "openra": "games/OpenRA",
}


def write_yaml(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=88),
        encoding="utf-8",
    )
    return destination


def load_goldens_metadata(repository_root: str | Path, row: ExpansionRow) -> dict[str, Any]:
    path = (
        Path(repository_root)
        / "specs"
        / row.benchmark
        / "lazarus"
        / row.visual_dir
        / "goldens.meta.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


_JUPYTERLAB_TARGET = {
    "kind": "jupyterlab_lazarus_application",
    "variants": ["fixed", "buggy"],
    "bundler": "materialized_dev_mode_bundle",
    "browser": "chromium_via_playwright",
}


EXPANSION_ROWS: tuple[ExpansionRow, ...] = (
    ExpansionRow(
        task_id="lz-jupyterlab-16943-markdown-regex-replace-all",
        benchmark="jupyterlab",
        change_number=16943,
        issue_number=16925,
        defect_locus="at_goal",
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        fix_commit="141904e62406c407434f5fd56c95d657e366eb3f",
        pull_request="https://github.com/jupyterlab/jupyterlab/pull/16943",
        issue_url="https://github.com/jupyterlab/jupyterlab/issues/16925",
        build_class="jlpm",
        preconditions=(
            "A JupyterLab notebook is open on the registered start with two cells: a "
            "markdown cell carrying a heading above eight bullet items of the form "
            "name=number, and one code cell holding a single "
            "name=number line. The document search overlay is open with replace shown, "
            "its regular-expression option enabled, its find field holding the pattern "
            "that captures a run of digits, and its replace field holding the "
            "substitution that refers back to that capture."
        ),
        reference_steps=(
            "Apply the overlay's replace-all control once, then read the eight bullet "
            "items the markdown cell now shows, comparing each item's text after the "
            "equals sign with the substitution the replace field named."
        ),
        audit_check="jupyterlab_lazarus_app_audit",
        audit_field="regex_replace_state",
        start_field="regex_replace_start_state",
        start_value="digit_values_present",
        goal_value="backreference_expanded",
        defect_value="backreference_literal",
        invariant_check="no_jupyterlab_probe_errors",
        registered_replay=JUPYTERLAB_APP_PROFILES[
            "lz-jupyterlab-16943-markdown-regex-replace-all"
        ].registered_replay,
        adapter="jupyterlab_lazarus_app_probe_v1",
        checkpoint_strategy="jupyterlab_lazarus_app_replay_validate",
        target=_JUPYTERLAB_TARGET | {"change_number": 16943},
        start_frame_description=(
            "The markdown cell lists eight bullet items whose values are plain digit "
            "runs, and the search overlay shows the digit-capturing pattern and its "
            "substitution."
        ),
        start_must=(
            "A JupyterLab notebook window shows a markdown cell headed \"Lazarus "
            "replace-all fixture\" above one code cell, with the document search "
            "overlay open at the top right.",
            "Each of the eight bullet items reads name= followed by a three-digit "
            "number and nothing else.",
        ),
        fixed_after_description=(
            "Every bullet item reads name= followed by its original three-digit number, "
            "then a plus sign and a one, so each captured run of digits was carried "
            "into the substitution."
        ),
        fixed_after_must=(
            "A JupyterLab notebook window shows the markdown cell's eight bullet items "
            "with the document search overlay still open.",
            "Every bullet item reads name= followed by its own three-digit number, then "
            "+1.",
        ),
        buggy_after_description=(
            "Every bullet item reads name= followed by a dollar sign, a one, a plus "
            "sign and a one, so the substitution's back-reference was written out "
            "literally instead of carrying the captured digits."
        ),
        buggy_after_must=(
            "A JupyterLab notebook window shows the markdown cell's eight bullet items "
            "with the document search overlay still open.",
            "Every bullet item reads name= followed by $1+1, and no three-digit number "
            "remains in any item.",
        ),
        fail_to_pass=("jupyterlab-16943-regex-replace-all-f2p",),
        pass_to_pass=(
            "jupyterlab-notebook-replace-all-construction-p2p",
            "jupyterlab-coreutils-path-trial-p2p",
        ),
        per_trial_p2p=("jupyterlab-coreutils-path-trial-p2p",),
        timeout_ms=900_000,
    ),
    ExpansionRow(
        task_id="lz-jupyterlab-17194-cell-metadata-removal",
        benchmark="jupyterlab",
        change_number=17194,
        issue_number=17164,
        defect_locus="at_goal",
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        fix_commit="7041277fe5d5a547f6d8795c61565e8424895819",
        pull_request="https://github.com/jupyterlab/jupyterlab/pull/17194",
        issue_url="https://github.com/jupyterlab/jupyterlab/issues/17164",
        build_class="jlpm",
        preconditions=(
            "A JupyterLab notebook is open on the registered start with one code cell "
            "selected, and the Property Inspector's Advanced Tools section is expanded "
            "so its Cell metadata editor shows a three-line JSON object whose single "
            "entry is the trusted flag."
        ),
        reference_steps=(
            "Delete the trusted entry's line from the Cell metadata editor, commit the "
            "edit with the editor's commit control, then collapse and reopen the "
            "Property Inspector and read how many lines the Cell metadata editor now "
            "shows and what they contain."
        ),
        audit_check="jupyterlab_lazarus_app_audit",
        audit_field="cell_metadata_state",
        start_field="cell_metadata_start_state",
        start_value="trusted_entry_present",
        goal_value="empty",
        defect_value="retains_trusted",
        invariant_check="no_jupyterlab_probe_errors",
        registered_replay=JUPYTERLAB_APP_PROFILES[
            "lz-jupyterlab-17194-cell-metadata-removal"
        ].registered_replay,
        adapter="jupyterlab_lazarus_app_probe_v1",
        checkpoint_strategy="jupyterlab_lazarus_app_replay_validate",
        target=_JUPYTERLAB_TARGET | {"change_number": 17194},
        start_frame_description=(
            "The Cell metadata editor shows a three-line JSON object holding the "
            "trusted entry, beside the notebook's single code cell."
        ),
        start_must=(
            "A JupyterLab notebook window shows one code cell and the Property "
            "Inspector open on the right with an Advanced Tools section.",
            "The Cell metadata editor shows a JSON object spanning three numbered "
            "lines whose middle line names the trusted entry.",
        ),
        fixed_after_description=(
            "The Cell metadata editor shows a single line holding an empty JSON object, "
            "so the committed removal survived reopening the Property Inspector."
        ),
        fixed_after_must=(
            "A JupyterLab notebook window shows one code cell and the Property "
            "Inspector open on the right with an Advanced Tools section.",
            "The Cell metadata editor shows exactly one numbered line containing an "
            "empty pair of braces.",
        ),
        buggy_after_description=(
            "The Cell metadata editor again shows the three-line JSON object naming the "
            "trusted entry, so reopening the Property Inspector restored the entry the "
            "commit had removed."
        ),
        buggy_after_must=(
            "A JupyterLab notebook window shows one code cell and the Property "
            "Inspector open on the right with an Advanced Tools section.",
            "The Cell metadata editor shows a JSON object spanning three numbered "
            "lines whose middle line names the trusted entry.",
        ),
        fail_to_pass=("jupyterlab-17194-remove-cell-metadata-f2p",),
        pass_to_pass=(
            "jupyterlab-metadata-empty-removal-construction-p2p",
            "jupyterlab-coreutils-path-trial-p2p",
        ),
        per_trial_p2p=("jupyterlab-coreutils-path-trial-p2p",),
        timeout_ms=900_000,
    ),
    ExpansionRow(
        task_id="lz-jupyterlab-15634-markdown-focus-render",
        benchmark="jupyterlab",
        change_number=15634,
        issue_number=15595,
        defect_locus="on_path",
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        fix_commit="83b2ae958f86b8a6925b9e0c36adb7b897bff62b",
        pull_request="https://github.com/jupyterlab/jupyterlab/pull/15634",
        issue_url="https://github.com/jupyterlab/jupyterlab/issues/15595",
        build_class="jlpm",
        preconditions=(
            "A JupyterLab notebook is open on the registered start with two cells: a "
            "markdown cell displaying the rendered heading \"Lazarus rendered heading\" "
            "and, below it, one code cell holding a single assignment line. The "
            "markdown cell is the selected cell and the notebook is in command mode, so "
            "no editor is open."
        ),
        reference_steps=(
            "Click into the code cell's editor so the notebook enters edit mode there, "
            "then read whether the markdown cell above still displays its heading as "
            "rendered text or now shows its own editor."
        ),
        audit_check="jupyterlab_lazarus_app_audit",
        audit_field="markdown_render_state",
        start_field="markdown_start_state",
        start_value="markdown_rendered_beside_code",
        goal_value="previous_rendered",
        defect_value="previous_unrendered",
        invariant_check="no_jupyterlab_probe_errors",
        registered_replay=JUPYTERLAB_APP_PROFILES[
            "lz-jupyterlab-15634-markdown-focus-render"
        ].registered_replay,
        adapter="jupyterlab_lazarus_app_probe_v1",
        checkpoint_strategy="jupyterlab_lazarus_app_replay_validate",
        target=_JUPYTERLAB_TARGET | {"change_number": 15634},
        start_frame_description=(
            "The markdown cell displays its heading as rendered text above the code "
            "cell, and no cell editor is focused."
        ),
        start_must=(
            "A JupyterLab notebook window shows a rendered markdown heading reading "
            "\"Lazarus rendered heading\" above one code cell.",
            "The markdown cell shows rendered heading text rather than markdown source "
            "in an editor.",
        ),
        fixed_after_description=(
            "The markdown cell still displays its heading as rendered text while the "
            "code cell below now has a focused editor."
        ),
        fixed_after_must=(
            "A JupyterLab notebook window shows the code cell's editor focused with a "
            "caret.",
            "The markdown cell above still shows the rendered heading text \"Lazarus "
            "rendered heading\".",
        ),
        buggy_after_description=(
            "The markdown cell now shows its own editor containing the heading's "
            "markdown source, so it stopped displaying rendered text when the code "
            "cell's editor took focus."
        ),
        buggy_after_must=(
            "A JupyterLab notebook window shows the code cell's editor focused with a "
            "caret.",
            "The markdown cell above shows an editor containing markdown source "
            "beginning with a hash character.",
        ),
        fail_to_pass=("jupyterlab-15634-preserve-rendered-markdown-f2p",),
        pass_to_pass=(
            "jupyterlab-cell-focus-edit-construction-p2p",
            "jupyterlab-coreutils-path-trial-p2p",
        ),
        per_trial_p2p=("jupyterlab-coreutils-path-trial-p2p",),
        timeout_ms=900_000,
    ),
    ExpansionRow(
        task_id="lz-jupyterlab-16343-shift-tab-tooltip",
        benchmark="jupyterlab",
        change_number=16343,
        issue_number=16319,
        defect_locus="at_goal",
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        fix_commit="4e0cbc0fd6703aee740ad15f601a78499b6b3a01",
        pull_request="https://github.com/jupyterlab/jupyterlab/pull/16343",
        issue_url="https://github.com/jupyterlab/jupyterlab/issues/16319",
        build_class="jlpm",
        preconditions=(
            "A JupyterLab notebook is open on the registered start with a single code "
            "cell whose editor holds the three characters of a built-in function name "
            "and nothing else, with a Python 3 kernel idle and the cell's editor "
            "focused."
        ),
        reference_steps=(
            "Move the caret to the end of the line and press Shift and Tab together "
            "once, then read whether an inspector panel appears over the notebook and "
            "what it reports about the name under the caret."
        ),
        audit_check="jupyterlab_lazarus_app_audit",
        audit_field="tooltip_state",
        start_field="tooltip_start_state",
        start_value="name_typed_no_tooltip",
        goal_value="tooltip_shown",
        defect_value="tooltip_absent",
        invariant_check="no_jupyterlab_probe_errors",
        registered_replay=JUPYTERLAB_APP_PROFILES[
            "lz-jupyterlab-16343-shift-tab-tooltip"
        ].registered_replay,
        adapter="jupyterlab_lazarus_app_probe_v1",
        checkpoint_strategy="jupyterlab_lazarus_app_replay_validate",
        target=_JUPYTERLAB_TARGET | {"change_number": 16343},
        start_frame_description=(
            "The notebook shows one code cell whose editor holds a three-character "
            "built-in name, with no panel over the notebook area."
        ),
        start_must=(
            "A JupyterLab notebook window shows exactly one code cell whose editor "
            "holds a short lowercase name.",
            "No panel or overlay covers the notebook area below the toolbar.",
        ),
        fixed_after_description=(
            "A panel appears directly under the caret reporting the name's signature, "
            "its documentation line, and its type."
        ),
        fixed_after_must=(
            "A JupyterLab notebook window shows one code cell whose editor holds a "
            "short lowercase name.",
            "A panel over the notebook area lists a signature line, a documentation "
            "line, and a type line for that name.",
        ),
        buggy_after_description=(
            "No panel appears over the notebook area, so the key combination produced "
            "no inspector output for the name under the caret."
        ),
        buggy_after_must=(
            "A JupyterLab notebook window shows one code cell whose editor holds a "
            "short lowercase name.",
            "No panel or overlay covers the notebook area below the toolbar.",
        ),
        fail_to_pass=("jupyterlab-16343-shift-tab-tooltip-f2p",),
        pass_to_pass=(
            "jupyterlab-docmanager-activate-construction-p2p",
            "jupyterlab-coreutils-path-trial-p2p",
        ),
        per_trial_p2p=("jupyterlab-coreutils-path-trial-p2p",),
        timeout_ms=900_000,
    ),
    ExpansionRow(
        task_id="lz-luanti-14396-metadata-set-string-nil",
        benchmark="luanti",
        change_number=14396,
        issue_number=14391,
        defect_locus="at_goal",
        horizon_class="short",
        capabilities=("resource-management", "structured-artifact-editing"),
        fix_commit="fa1d80b53b12beedac2e8c1045ffe86e07b89423",
        pull_request="https://github.com/luanti-org/luanti/pull/14396",
        issue_url="https://github.com/luanti-org/luanti/issues/14391",
        build_class="incremental_cpp",
        preconditions=(
            "A Luanti world is open on the registered start with the devtest game "
            "loaded: a sand block sits at the origin with a cobble marker block "
            "directly above it, and the player hovers six nodes away looking at that "
            "marker. The registered metadata query has not run yet."
        ),
        reference_steps=(
            "Run the registered replay once so the server stores a string field on a "
            "stack's metadata and then calls the same setter for that field with no "
            "value, then read which block the marker position now shows."
        ),
        audit_check="luanti_lazarus_state_audit",
        audit_field="marker_node",
        start_field="world_start_state",
        start_value="start_validated",
        goal_value="basenodes:dirt_with_grass",
        defect_value="basenodes:desert_sand",
        invariant_check="no_luanti_mod_errors",
        registered_replay=(
            "restore_registered_candidate_start",
            "apply_registered_metadata_setter",
            "read_marker_node_name",
        ),
        adapter="luanti_live_world_v1",
        checkpoint_strategy="luanti_worldmod_state_validate",
        target={
            "kind": "synthesized_world",
            "candidate_versions": ["fixed", "buggy"],
            "game": "devtest",
            "fixture": "registered_candidate_worldmod",
        },
        start_frame_description=(
            "The rendered world shows a cobble marker block above a sand block, seen "
            "from the hovering player's vantage."
        ),
        start_must=(
            "A live Luanti world is rendered from a hovering vantage with the normal "
            "heads-up display present.",
            "A grey cobble block sits directly above a sand block at the centre of the "
            "view.",
        ),
        fixed_after_description=(
            "The marker position now shows a grass-topped dirt block, so the setter "
            "call with no value completed and cleared the stored field."
        ),
        fixed_after_must=(
            "A live Luanti world is rendered from a hovering vantage with the normal "
            "heads-up display present.",
            "A grass-topped dirt block sits at the marker position above the sand "
            "block.",
        ),
        buggy_after_description=(
            "The marker position now shows a desert sand block, so the setter call with "
            "no value did not complete and the stored field was left in place."
        ),
        buggy_after_must=(
            "A live Luanti world is rendered from a hovering vantage with the normal "
            "heads-up display present.",
            "A pale desert sand block sits at the marker position above the sand block.",
        ),
        fail_to_pass=("luanti-pr14396-metadata-compat-f2p",),
        pass_to_pass=("luanti-vector-zero-construction-p2p",),
        per_trial_p2p=("luanti-vector-zero-construction-p2p",),
        timeout_ms=2_400_000,
        must_not=_LUANTI_MUST_NOT,
        ignore=_LUANTI_IGNORE,
    ),
    ExpansionRow(
        task_id="lz-openra-21453-hotkey-ready-building",
        benchmark="openra",
        change_number=21453,
        issue_number=21197,
        defect_locus="on_path",
        horizon_class="short",
        capabilities=("resource-management", "gui-localization"),
        fix_commit="387e594f4a7ceaaf17f5c00002bfa98829574031",
        pull_request="https://github.com/OpenRA/OpenRA/pull/21453",
        issue_url="https://github.com/OpenRA/OpenRA/issues/21197",
        build_class="incremental_dotnet",
        preconditions=(
            "A live Red Alert skirmish is open on the registered start with the Allies "
            "construction yard placed and the sidebar's Buildings production palette "
            "visible. One structure has finished producing and its palette page is not "
            "the page the sidebar currently shows."
        ),
        reference_steps=(
            "Right-click once to cancel the pending placement, then press the Buildings "
            "category hotkey once and read whether the sidebar returns to the completed "
            "structure's page with placement ready."
        ),
        audit_check="openra_lazarus_hotkey_audit",
        audit_field="placement_active",
        start_field="production_start_state",
        start_value="palette_ready",
        goal_value="true",
        defect_value="false",
        invariant_check="no_openra_gameplay_probe_errors",
        registered_replay=(
            "restore_registered_production_start",
            "cancel_pending_placement",
            "press_buildings_category_hotkey",
        ),
        adapter="openra_live_gameplay_probe_v1",
        checkpoint_strategy="openra_gameplay_production_validate",
        target={"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
        start_frame_description="",
        start_must=(),
        fixed_after_description="",
        fixed_after_must=(),
        buggy_after_description="",
        buggy_after_must=(),
        fail_to_pass=("openra-pr21453-hidden-ready-building-f2p",),
        pass_to_pass=("openra-action-queue-p2p",),
        per_trial_p2p=("openra-action-queue-p2p",),
        timeout_ms=2_400_000,
        generate_spec=False,
    ),
)


EXPANSION_ROWS_BY_ID = {row.task_id: row for row in EXPANSION_ROWS}
