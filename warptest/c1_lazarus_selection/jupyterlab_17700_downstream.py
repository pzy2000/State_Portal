"""Gate 8--13 real-browser closure for JupyterLab #17700.

The adapter consumes only the strict ``current-v1`` Gate 1--7 transition and pool
admission.  It builds one candidate-local Rspack bundle per variant from the
candidate production module ``packages/statusbar/lib/statusbar.js`` and executes
that bundle in Microsoft Edge through Playwright.  The authoritative defect is an
accessibility regression: right-aligned status items are inserted in the wrong
DOM order, so real keyboard ``Tab`` navigation reaches the edge-most item before
the middle-most one.  The candidate fix keeps the rendered left-to-right order
identical, therefore the observation is the focus ring position after real key
presses, not the item layout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable

from PIL import Image, ImageChops, ImageStat

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 17700}
_VERSION = "1.0"
_EPOCH = "real-tab-v1"
_TREES = PurePosixPath("private/materialized/jupyterlab/17700/current-v1")
_RUN = PurePosixPath("private/bounded-runs/jupyterlab/17700/real-tab-v1")
_STATUSBAR = PurePosixPath("packages/statusbar/lib/statusbar.js")
_BASE_CSS = PurePosixPath("packages/statusbar/style/base.css")
_VARIABLES_CSS = PurePosixPath("packages/theme-light-extension/style/variables.css")
_LUMINO_WIDGETS = PurePosixPath("node_modules/@lumino/widgets/package.json")
_RSPACK = PurePosixPath("node_modules/@rspack/cli/bin/rspack.js")
_PLAYWRIGHT = PurePosixPath("node_modules/playwright/index.js")
_MARKER = re.compile(rb"C1_LAZARUS_17700_EDGE_OBSERVATION=(\{.*\})")

# Registered right-aligned items, ordered by ascending rank as extensions do.
_ITEMS = (
    {"id": "notifications", "rank": 0, "label": "Bell"},
    {"id": "kernel-status", "rank": 1, "label": "Kernel Idle"},
    {"id": "line-column", "rank": 2, "label": "Ln 1, Col 1"},
)
# Higher rank must render closer to the middle in both variants.
_VISUAL_ORDER = ("line-column", "kernel-status", "notifications")
_EXPECTED_TAB_ORDER = {
    "fixed": ("line-column", "kernel-status", "notifications"),
    "buggy": ("notifications", "kernel-status", "line-column"),
}
_GLOBAL_CROSS_THRESHOLD = 0.05
_ROI_CROSS_THRESHOLD = 2.0

_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/jupyterlab/17700/node22/current-v1",
    "candidate_gate57_transition",
    _VERSION,
)
_POOL = ArtifactReference(
    "candidate_pool_admission/jupyterlab/17700/strict/current-v1",
    "candidate_pool_admission",
    _VERSION,
)
_GATE8_MATERIALIZATION = ArtifactReference(
    f"candidate_gate8_materialization/jupyterlab/17700/{_EPOCH}",
    "candidate_gate8_materialization",
    _VERSION,
)
_GATE8_RUNTIME = ArtifactReference(
    f"candidate_gate8_runtime/jupyterlab/17700/{_EPOCH}",
    "candidate_gate8_runtime",
    _VERSION,
)
_GATE9_START = ArtifactReference(
    f"candidate_gate9_start/jupyterlab/17700/{_EPOCH}",
    "candidate_gate9_start",
    _VERSION,
)
_GATE9_REPLAY = ArtifactReference(
    f"candidate_gate9_replay/jupyterlab/17700/{_EPOCH}",
    "candidate_gate9_replay",
    _VERSION,
)
_GATE10_ORACLE = ArtifactReference(
    f"candidate_gate10_hidden_oracle/jupyterlab/17700/{_EPOCH}",
    "candidate_gate10_hidden_oracle",
    _VERSION,
)
_GATE12_MATRIX = ArtifactReference(
    f"candidate_gate12_semantic_pixel_matrix/jupyterlab/17700/{_EPOCH}",
    "candidate_gate12_semantic_pixel_matrix",
    _VERSION,
)
_GATE13_LINEAGE = ArtifactReference(
    f"candidate_gate13_same_lineage/jupyterlab/17700/{_EPOCH}",
    "candidate_gate13_same_lineage",
    _VERSION,
)
_ADMISSION = ArtifactReference(
    f"candidate_formal_freeze_admission/jupyterlab/17700/{_EPOCH}",
    "candidate_formal_freeze_admission",
    _VERSION,
)

ProcessRunner = Callable[..., NativeProcessReceipt]


def execute_jupyterlab_17700_downstream(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    process_runner: ProcessRunner | None = None,
) -> tuple[StableArtifact, ...]:
    """Execute fresh Gate 8--13 construction without any paid/formal trial."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    transition = _load(root, _TRANSITION)
    pool = _load(root, _POOL)
    _validate_strict_gate57_inputs(transition, pool)
    trees = {variant: _tree(root, variant) for variant in ("fixed", "buggy")}
    fingerprints = {variant: _fingerprints(tree) for variant, tree in trees.items()}
    for common in ("base_css_fingerprint", "variables_css_fingerprint"):
        if fingerprints["fixed"][common] != fingerprints["buggy"][common]:
            raise RuntimeError(f"#17700 common production style drifted: {common}")
    if (
        fingerprints["fixed"]["statusbar_fingerprint"]
        == fingerprints["buggy"]["statusbar_fingerprint"]
    ):
        raise RuntimeError("#17700 fixed/buggy production build is not an inverse pair")

    materialization = _write(
        root,
        _GATE8_MATERIALIZATION,
        (transition.reference, pool.reference),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "variants": {
                variant: {
                    "tree": tree.relative_to(root).as_posix(),
                    **fingerprints[variant],
                }
                for variant, tree in trees.items()
            },
            "production_module": _STATUSBAR.as_posix(),
            "production_style": _BASE_CSS.as_posix(),
            "production_theme_variables": _VARIABLES_CSS.as_posix(),
            "candidate_local_direct_imports": True,
            "identical_production_style_across_variants": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    runner = process_runner or run_bounded_process
    node = _node_executable()
    observations: list[StableArtifact] = []
    runtime_receipts: dict[str, dict[str, object]] = {}
    for variant, tree in trees.items():
        run = _ordinary_run_directory(root / _RUN / variant, root)
        _write_harness(run, tree)
        environment = _environment(run)
        build_argv = (
            node,
            str(tree / _RSPACK),
            "build",
            "--config",
            str(run / "rspack.config.cjs"),
        )
        build = runner(build_argv, cwd=run, env=environment, timeout_seconds=900)
        _require_completed(build, f"#17700 {variant} Rspack build")
        bundle = _ordinary_file(run / "dist/bundle.js", run)
        browser_argv = (node, str(run / "edge-observe.cjs"))
        browser = runner(browser_argv, cwd=run, env=environment, timeout_seconds=300)
        _require_completed(browser, f"#17700 {variant} Microsoft Edge observation")
        data = _parse_observation(browser)
        _validate_observation(data, variant, run)
        runtime_receipts[variant] = {
            "rspack_argv": list(build_argv),
            "rspack_stdout_base64": base64.b64encode(build.stdout).decode("ascii"),
            "rspack_stderr_base64": base64.b64encode(build.stderr).decode("ascii"),
            "bundle_fingerprint": _sha256(bundle),
            "browser_argv": list(browser_argv),
            "browser_version": data["browser_version"],
            "browser_channel": data["browser_channel"],
        }
        observation_ref = ArtifactReference(
            f"candidate_gate11_edge_observation/jupyterlab/17700/{variant}/{_EPOCH}",
            "candidate_gate11_edge_observation",
            _VERSION,
        )
        observations.append(
            _write(
                root,
                observation_ref,
                (materialization.reference,),
                {
                    "candidate": _CANDIDATE,
                    "variant": variant,
                    "state": "passed",
                    "complete": True,
                    "before_focused_item": data["before_focused_item"],
                    "dom_order": list(data["dom_order"]),
                    "visual_order": list(data["visual_order"]),
                    "tab_sequence": list(data["tab_sequence"]),
                    "first_tab_stop": data["tab_sequence"][0],
                    "right_group_box": dict(data["right_group_box"]),
                    "frames": {
                        key: (run / value).relative_to(root).as_posix()
                        for key, value in data["frames"].items()
                    },
                    "capture_backend": "playwright_microsoft_edge_png",
                    "browser_channel": "msedge",
                    "real_keyboard_tab_presses": True,
                    "after_dom_candidate_controlled": True,
                    "candidate_local_bundle": bundle.relative_to(root).as_posix(),
                    "old_attempts_loaded": False,
                    "paid_actions_authorized": False,
                    "formal_trials_authorized": False,
                },
                authorization,
            )
        )

    runtime = _write(
        root,
        _GATE8_RUNTIME,
        (materialization.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "bundler": "candidate_local_rspack",
            "browser": "Microsoft Edge via Playwright channel msedge",
            "variant_receipts": runtime_receipts,
            "fresh_process_per_variant": True,
            "candidate_execution_performed": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    by_variant = {str(item.payload["variant"]): item for item in observations}
    unfocused_start = all(
        item.payload["before_focused_item"] is None for item in observations
    )
    common_visual_order = (
        tuple(by_variant["fixed"].payload["visual_order"])
        == tuple(by_variant["buggy"].payload["visual_order"])
        == _VISUAL_ORDER
    )
    start_passed = unfocused_start and common_visual_order
    start = _write(
        root,
        _GATE9_START,
        (runtime.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if start_passed else "blocked",
            "complete": start_passed,
            "registered_start": {
                "focused_item": None,
                "registered_right_items": [dict(item) for item in _ITEMS],
                "rendered_left_to_right_order": list(_VISUAL_ORDER),
            },
            "common_before_equivalent": unfocused_start,
            "common_rendered_order_equivalent": common_visual_order,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    replay = _write(
        root,
        _GATE9_REPLAY,
        (start.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if start_passed else "blocked",
            "complete": start_passed,
            "ordered_replay": [
                "load_candidate_local_bundle",
                "register_three_right_aligned_items_by_ascending_rank",
                "attach_status_bar_and_restore_unfocused_start",
                "press_real_tab_three_times",
                "read_candidate_ordered_focus_targets",
            ],
            "actual_after_source": "candidate_controlled_dom_focus_order",
            "fresh_process_per_variant": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    observed = {
        variant: tuple(by_variant[variant].payload["tab_sequence"])
        for variant in ("fixed", "buggy")
    }
    oracle_passed = start_passed and observed == _EXPECTED_TAB_ORDER
    oracle = _write(
        root,
        _GATE10_ORACLE,
        (replay.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if oracle_passed else "blocked",
            "complete": oracle_passed,
            "hidden_until_after_candidate_execution": True,
            "authoritative_defect": (
                "right status items receive keyboard focus from the screen edge "
                "inward instead of from the middle outward"
            ),
            "expected_tab_order": {
                variant: list(value) for variant, value in _EXPECTED_TAB_ORDER.items()
            },
            "observed_tab_order": {
                variant: list(value) for variant, value in observed.items()
            },
            "common_rendered_order": list(_VISUAL_ORDER),
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    matrix_payload = _matrix(root, by_variant, observed)
    matrix = _write(
        root,
        _GATE12_MATRIX,
        (oracle.reference,) + tuple(item.reference for item in observations),
        matrix_payload,
        authorization,
    )
    passed = (
        start.payload["state"] == "passed"
        and replay.payload["state"] == "passed"
        and oracle.payload["state"] == "passed"
        and matrix.payload["state"] == "passed"
    )
    lineage = _write(
        root,
        _GATE13_LINEAGE,
        (
            transition.reference,
            pool.reference,
            materialization.reference,
            runtime.reference,
            start.reference,
            replay.reference,
            oracle.reference,
            *(item.reference for item in observations),
            matrix.reference,
        ),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if passed else "blocked",
            "complete": passed,
            "same_lineage": passed,
            "gate57_epoch": "node22/current-v1",
            "downstream_epoch": _EPOCH,
            "candidate_local_variant_bundles": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    if not passed:
        raise RuntimeError("#17700 Gate 8--13 semantic/visual closure failed")
    admission = _write(
        root,
        _ADMISSION,
        (lineage.reference, matrix.reference, oracle.reference),
        {
            "candidate": _CANDIDATE,
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "gate8_materialization_runtime": "passed",
            "gate9_registered_start_replay": "passed",
            "gate10_hidden_oracle": "passed",
            "gate11_real_edge_observations": "passed",
            "gate12_semantic_pixel_2x2": "passed",
            "gate13_same_lineage": "passed",
            "formal_freeze_ready": True,
            "no_timeout_unknown_or_inheritance": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return (
        materialization,
        runtime,
        start,
        replay,
        oracle,
        *observations,
        matrix,
        lineage,
        admission,
    )


def _validate_strict_gate57_inputs(
    transition: StableArtifact, pool: StableArtifact
) -> None:
    gate_status = transition.payload.get("gate_status")
    if (
        transition.reference != _TRANSITION
        or transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("outcome") != "qualified"
        or transition.payload.get("complete") is not True
        or transition.payload.get("terminal") is not False
        or transition.payload.get("gate8_13_performed") is not False
        or transition.payload.get("old_attempts_loaded") is not False
        or type(gate_status) is not dict
        or set(gate_status.values()) != {"passed"}
    ):
        raise RuntimeError("#17700 strict current-v1 Gate 1--7 transition is invalid")
    if (
        pool.reference != _POOL
        or pool.payload.get("candidate") != _CANDIDATE
        or pool.payload.get("state") != "admitted"
        or pool.payload.get("defect_locus") != "at_goal"
        or pool.payload.get("strict_gate_1_7") is not True
        or pool.payload.get("no_timeout_unknown_or_inheritance") is not True
        or pool.payload.get("old_attempts_loaded") is not False
        or pool.payload.get("gate8_to_13") != "pending_post_admission"
    ):
        raise RuntimeError("#17700 strict current-v1 pool admission is invalid")


def _write_harness(run: Path, tree: Path) -> None:
    statusbar = (tree / _STATUSBAR).as_posix()
    entry = f"""import {{ StatusBar }} from {json.dumps(statusbar)};
import {{ Widget }} from '@lumino/widgets';

const items = {json.dumps(list(_ITEMS))};
const statusBar = new StatusBar();
const nodes = {{}};
for (const spec of items) {{
  const widget = new Widget();
  widget.node.textContent = spec.label;
  widget.node.tabIndex = 0;
  widget.node.dataset.itemId = spec.id;
  statusBar.registerStatusItem(spec.id, {{
    item: widget, align: 'right', rank: spec.rank
  }});
  nodes[spec.id] = widget.node;
}}
Widget.attach(statusBar, document.getElementById('statusbar-host'));

const rightItems = () => Array.from(
  statusBar.node.querySelectorAll('.jp-StatusBar-Right [data-item-id]')
);
window.__candidateReady = true;
window.__candidateDomOrder = () => rightItems().map(node => node.dataset.itemId);
window.__candidateVisualOrder = () => rightItems()
  .map(node => ({{ id: node.dataset.itemId, x: node.getBoundingClientRect().x }}))
  .sort((left, right) => left.x - right.x)
  .map(entry => entry.id);
window.__candidateFocused = () => {{
  const node = document.activeElement;
  return (node && node.dataset && node.dataset.itemId) || null;
}};
window.__candidateRightBox = () => {{
  const box = statusBar.node
    .querySelector('.jp-StatusBar-Right')
    .getBoundingClientRect();
  return {{ x: box.x, y: box.y, width: box.width, height: box.height }};
}};
window.__candidateFocusItem = id => nodes[id].focus();
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
  optimization: {{ minimize: false }}
}};
"""
    html_document = f"""<!doctype html>
<meta charset="utf-8"><title>JupyterLab status bar keyboard order replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _BASE_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:#fff}}
#statusbar-host{{position:absolute;left:0;right:0;bottom:0}}
</style>
<div id="statusbar-host"></div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / _PLAYWRIGHT).as_posix()
    presses = len(_ITEMS)
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 1280, height: 200 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);
  const dom_order = await page.evaluate(() => window.__candidateDomOrder());
  const visual_order = await page.evaluate(() => window.__candidateVisualOrder());
  const right_group_box = await page.evaluate(() => window.__candidateRightBox());
  const before_focused_item = await page.evaluate(() => window.__candidateFocused());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});
  const tab_sequence = [];
  for (let index = 0; index < {presses}; index += 1) {{
    await page.keyboard.press('Tab');
    tab_sequence.push(await page.evaluate(() => window.__candidateFocused()));
    if (index === 0) {{
      await page.screenshot({{ path: path.join(__dirname, 'after.png') }});
    }}
  }}
  await page.evaluate(() => window.__candidateFocusItem({json.dumps(_VISUAL_ORDER[0])}));
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});
  await page.evaluate(() => window.__candidateFocusItem({json.dumps(_VISUAL_ORDER[-1])}));
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});
  console.log('C1_LAZARUS_17700_EDGE_OBSERVATION=' + JSON.stringify({{
    dom_order, visual_order, right_group_box, before_focused_item, tab_sequence,
    browser_channel: 'msedge', browser_version: browser.version(),
    frames: {{ before: 'before.png', after: 'after.png',
      fixed_golden: 'fixed-golden.png', buggy_golden: 'buggy-golden.png' }}
  }}));
  await browser.close();
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    for name, text in (
        ("entry.js", entry),
        ("rspack.config.cjs", config),
        ("index.html", html_document),
        ("edge-observe.cjs", browser),
    ):
        target = run / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _matrix(
    root: Path,
    observations: dict[str, StableArtifact],
    observed: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    paths: dict[str, dict[str, Path]] = {}
    for variant, artifact in observations.items():
        paths[variant] = {
            key: _ordinary_file(root / PurePosixPath(value), root)
            for key, value in artifact.payload["frames"].items()
        }
    before_difference = _pixel_difference(
        paths["fixed"]["before"], paths["buggy"]["before"]
    )
    pixel = {
        variant: {
            golden: _pixel_difference(
                paths[variant]["after"], paths[variant][f"{golden}_golden"]
            )
            for golden in ("fixed", "buggy")
        }
        for variant in ("fixed", "buggy")
    }
    sizes = {
        variant: _image_size(paths[variant]["after"]) for variant in ("fixed", "buggy")
    }
    if len(set(sizes.values())) != 1:
        raise RuntimeError("#17700 Edge frame dimensions drifted")
    boxes = {
        variant: dict(artifact.payload["right_group_box"])
        for variant, artifact in observations.items()
    }
    if boxes["fixed"] != boxes["buggy"]:
        raise RuntimeError("#17700 right status group geometry drifted between variants")
    roi_bbox = _right_group_roi(boxes["fixed"], sizes["fixed"])
    pixel_roi = {
        variant: {
            golden: _pixel_difference(
                paths[variant]["after"],
                paths[variant][f"{golden}_golden"],
                crop=roi_bbox,
            )
            for golden in ("fixed", "buggy")
        }
        for variant in ("fixed", "buggy")
    }
    semantic = {
        variant: {
            golden: observed[variant] == _EXPECTED_TAB_ORDER[golden]
            for golden in ("fixed", "buggy")
        }
        for variant in ("fixed", "buggy")
    }
    passed = (
        before_difference == 0.0
        and semantic
        == {
            "fixed": {"fixed": True, "buggy": False},
            "buggy": {"fixed": False, "buggy": True},
        }
        and pixel["fixed"]["fixed"] == 0.0
        and pixel["fixed"]["buggy"] > _GLOBAL_CROSS_THRESHOLD
        and pixel["buggy"]["fixed"] > _GLOBAL_CROSS_THRESHOLD
        and pixel["buggy"]["buggy"] == 0.0
        and pixel_roi["fixed"]["fixed"] == 0.0
        and pixel_roi["fixed"]["buggy"] > _ROI_CROSS_THRESHOLD
        and pixel_roi["buggy"]["fixed"] > _ROI_CROSS_THRESHOLD
        and pixel_roi["buggy"]["buggy"] == 0.0
    )
    return {
        "candidate": _CANDIDATE,
        "state": "passed" if passed else "blocked",
        "complete": passed,
        "common_before_pixel_difference_mean": before_difference,
        "common_before_equivalent": before_difference == 0.0,
        "semantic_two_by_two": semantic,
        "pixel_difference_mean_two_by_two": pixel,
        "right_group_roi_bbox": list(roi_bbox),
        "right_group_roi_difference_mean_two_by_two": pixel_roi,
        "cross_rejection_thresholds": {
            "global_mean_gt": _GLOBAL_CROSS_THRESHOLD,
            "right_group_roi_mean_gt": _ROI_CROSS_THRESHOLD,
        },
        "diagonal_matches_and_cross_rejects": passed,
        "unchanged_rendered_order_across_variants": True,
        "synthetic_observation_frames": False,
        "golden_frame_source": "browser_constructed_hidden_semantic_oracle",
        "actual_after_source": "candidate_controlled_dom_focus_order",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _right_group_roi(
    box: dict[str, object], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    width, height = size
    margin = 6
    left = max(int(float(str(box["x"]))) - margin, 0)
    top = max(int(float(str(box["y"]))) - margin, 0)
    right = min(
        int(float(str(box["x"])) + float(str(box["width"]))) + margin,
        width,
    )
    bottom = min(
        int(float(str(box["y"])) + float(str(box["height"]))) + margin,
        height,
    )
    if right - left < 1 or bottom - top < 1:
        raise RuntimeError("#17700 right status group region of interest is degenerate")
    return (left, top, right, bottom)


def _tree(root: Path, variant: str) -> Path:
    tree = _ordinary_directory(root / _TREES / variant, root)
    for relative in (
        _STATUSBAR,
        _BASE_CSS,
        _VARIABLES_CSS,
        _LUMINO_WIDGETS,
        _RSPACK,
        _PLAYWRIGHT,
    ):
        _ordinary_file(tree / relative, tree)
    return tree


def _fingerprints(tree: Path) -> dict[str, str]:
    return {
        "statusbar_fingerprint": _sha256(tree / _STATUSBAR),
        "base_css_fingerprint": _sha256(tree / _BASE_CSS),
        "variables_css_fingerprint": _sha256(tree / _VARIABLES_CSS),
        "lumino_widgets_fingerprint": _sha256(tree / _LUMINO_WIDGETS),
        "rspack_cli_fingerprint": _sha256(tree / _RSPACK),
        "playwright_fingerprint": _sha256(tree / _PLAYWRIGHT),
    }


def _parse_observation(receipt: NativeProcessReceipt) -> dict[str, object]:
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in _MARKER.finditer(stream)
    ]
    if len(matches) != 1:
        raise RuntimeError("#17700 Edge observation marker is missing or ambiguous")
    value = json.loads(matches[0].group(1))
    if not isinstance(value, dict):
        raise RuntimeError("#17700 Edge observation marker is malformed")
    return value


def _validate_observation(data: dict[str, object], variant: str, run: Path) -> None:
    frames = data.get("frames")
    box = data.get("right_group_box")
    expected_dom_order = _EXPECTED_TAB_ORDER[variant]
    if (
        data.get("before_focused_item") is not None
        or tuple(data.get("dom_order") or ()) != expected_dom_order
        or tuple(data.get("visual_order") or ()) != _VISUAL_ORDER
        or tuple(data.get("tab_sequence") or ()) != expected_dom_order
        or data.get("browser_channel") != "msedge"
        or not isinstance(data.get("browser_version"), str)
        or not data["browser_version"]
        or not isinstance(box, dict)
        or set(box) != {"x", "y", "width", "height"}
        or not all(isinstance(value, (int, float)) for value in box.values())
        or not isinstance(frames, dict)
        or set(frames) != {"before", "after", "fixed_golden", "buggy_golden"}
    ):
        raise RuntimeError(f"#17700 {variant} Edge semantic observation failed")
    for name in frames.values():
        if not isinstance(name, str):
            raise RuntimeError("#17700 Edge frame path is malformed")
        _ordinary_file(run / PurePosixPath(name), run)


def _require_completed(receipt: NativeProcessReceipt, label: str) -> None:
    if receipt.outcome != "completed" or receipt.returncode != 0:
        raise RuntimeError(f"{label} failed: {receipt.outcome}/{receipt.returncode}")


def _environment(run: Path) -> dict[str, str]:
    home = run / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return {**os.environ, "HOME": str(home), "LANG": "C", "LC_ALL": "C", "CI": "1"}


def _node_executable() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable")
    return str(Path(node).resolve(strict=True))


def _pixel_difference(
    left_path: Path, right_path: Path, *, crop: tuple[int, int, int, int] | None = None
) -> float:
    with (
        Image.open(left_path).convert("RGB") as left,
        Image.open(right_path).convert("RGB") as right,
    ):
        if left.size != right.size:
            return float("inf")
        if crop is not None:
            left = left.crop(crop)
            right = right.crop(crop)
        return float(sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3)


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _ordinary_run_directory(path: Path, root: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return _ordinary_directory(path, root)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "active preannotation-execution authorization is required"
        )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"artifact conflict: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["execute_jupyterlab_17700_downstream"]
