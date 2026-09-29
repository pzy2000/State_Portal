"""Gate 8--13 real-browser closure for JupyterLab #18938.

The adapter consumes only the fresh ``current-v1`` strict Gate 1--7 transition and
pool admission.  It builds one candidate-local Rspack bundle per variant from the
candidate production module ``packages/apputils/lib/dialog.js`` and drives it in
Microsoft Edge through Playwright with real mouse input.  The authoritative defect
is a hang: when a dialog body widget's ``getValue()`` throws, the dialog is never
disposed and its promise never settles, so the user must reload the browser.  The
observation is therefore the dialog's presence after a real click on ``Ok``
together with the settlement state of the returned promise.

Golden frames come from the same production close path: the probe dialog is
dismissed with ``Cancel``, which never calls ``getValue()`` and behaves identically
in both variants.
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


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 18938}
_VERSION = "1.0"
# The v1 epoch left a uniformly blank frame once the fixed dialog closed, which
# carries no usable visual evidence.  This epoch keeps a static page panel so every
# frame stays informative while the dialog presence remains the observable, and it
# owns its own bounded-run directory so no earlier epoch's frames are rewritten.
_EPOCH = "real-dialog-v3"
_TREES = PurePosixPath("private/materialized/jupyterlab/18938/current-v1")
_RUN = PurePosixPath(f"private/bounded-runs/jupyterlab/18938/{_EPOCH}")
_DIALOG = PurePosixPath("packages/apputils/lib/dialog.js")
_DIALOG_CSS = PurePosixPath("packages/apputils/style/dialog.css")
_BASE_CSS = PurePosixPath("packages/apputils/style/base.css")
_UI_CSS = PurePosixPath("packages/ui-components/style/index.css")
_VARIABLES_CSS = PurePosixPath("packages/theme-light-extension/style/variables.css")
_RSPACK = PurePosixPath("node_modules/@rspack/cli/bin/rspack.js")
_PLAYWRIGHT = PurePosixPath("node_modules/playwright/index.js")
_MARKER = re.compile(rb"C1_LAZARUS_18938_EDGE_OBSERVATION=(\{.*\})")

_DIALOG_TITLE = "Rename File"
_DIALOG_BODY = "Rename the notebook"
_THROWN_MESSAGE = "getValue() failed"
_EXPECTED_AFTER = {
    "fixed": {
        "dialog_count": 0,
        "settled": "rejected",
        "error": _THROWN_MESSAGE,
        "uncaught_page_error": False,
    },
    "buggy": {
        "dialog_count": 1,
        "settled": None,
        "error": None,
        "uncaught_page_error": True,
    },
}
_GLOBAL_CROSS_THRESHOLD = 5.0

_TRANSITION = ArtifactReference(
    "candidate_gate57_18938_transition/jupyterlab/18938/current-v1",
    "candidate_gate57_18938_transition",
    _VERSION,
)
_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18938/current-v1",
    "candidate_gate57_pool_admission",
    _VERSION,
)
_GATE8_MATERIALIZATION = ArtifactReference(
    f"candidate_gate8_materialization/jupyterlab/18938/{_EPOCH}",
    "candidate_gate8_materialization",
    _VERSION,
)
_GATE8_RUNTIME = ArtifactReference(
    f"candidate_gate8_runtime/jupyterlab/18938/{_EPOCH}",
    "candidate_gate8_runtime",
    _VERSION,
)
_GATE9_START = ArtifactReference(
    f"candidate_gate9_start/jupyterlab/18938/{_EPOCH}",
    "candidate_gate9_start",
    _VERSION,
)
_GATE9_REPLAY = ArtifactReference(
    f"candidate_gate9_replay/jupyterlab/18938/{_EPOCH}",
    "candidate_gate9_replay",
    _VERSION,
)
_GATE10_ORACLE = ArtifactReference(
    f"candidate_gate10_hidden_oracle/jupyterlab/18938/{_EPOCH}",
    "candidate_gate10_hidden_oracle",
    _VERSION,
)
_GATE12_MATRIX = ArtifactReference(
    f"candidate_gate12_semantic_pixel_matrix/jupyterlab/18938/{_EPOCH}",
    "candidate_gate12_semantic_pixel_matrix",
    _VERSION,
)
_GATE13_LINEAGE = ArtifactReference(
    f"candidate_gate13_same_lineage/jupyterlab/18938/{_EPOCH}",
    "candidate_gate13_same_lineage",
    _VERSION,
)
_ADMISSION = ArtifactReference(
    f"candidate_formal_freeze_admission/jupyterlab/18938/{_EPOCH}",
    "candidate_formal_freeze_admission",
    _VERSION,
)

ProcessRunner = Callable[..., NativeProcessReceipt]


def execute_jupyterlab_18938_downstream(
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
    for common in (
        "dialog_css_fingerprint",
        "base_css_fingerprint",
        "ui_css_fingerprint",
        "variables_css_fingerprint",
    ):
        if fingerprints["fixed"][common] != fingerprints["buggy"][common]:
            raise RuntimeError(f"#18938 common production style drifted: {common}")
    if (
        fingerprints["fixed"]["dialog_fingerprint"]
        == fingerprints["buggy"]["dialog_fingerprint"]
    ):
        raise RuntimeError("#18938 fixed/buggy production build is not an inverse pair")

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
            "production_module": _DIALOG.as_posix(),
            "production_styles": [
                _VARIABLES_CSS.as_posix(),
                _UI_CSS.as_posix(),
                _BASE_CSS.as_posix(),
                _DIALOG_CSS.as_posix(),
            ],
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
        _require_completed(build, f"#18938 {variant} Rspack build")
        bundle = _ordinary_file(run / "dist/bundle.js", run)
        browser_argv = (node, str(run / "edge-observe.cjs"))
        browser = runner(browser_argv, cwd=run, env=environment, timeout_seconds=300)
        _require_completed(browser, f"#18938 {variant} Microsoft Edge observation")
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
            f"candidate_gate11_edge_observation/jupyterlab/18938/{variant}/{_EPOCH}",
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
                    "before_dialog_count": data["before_dialog_count"],
                    "before_settled": data["before_settled"]["settled"],
                    "after_dialog_count": data["after_dialog_count"],
                    "after_settled": data["after_settled"]["settled"],
                    "after_error": data["after_settled"]["error"],
                    "uncaught_page_errors": list(data["page_errors"]),
                    "golden_dialog_count_after_cancel": data["golden_dialog_count"],
                    "frames": {
                        key: (run / value).relative_to(root).as_posix()
                        for key, value in data["frames"].items()
                    },
                    "capture_backend": "playwright_microsoft_edge_png",
                    "browser_channel": "msedge",
                    "real_mouse_click_on_ok": True,
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
    common_start = all(
        item.payload["before_dialog_count"] == 1
        and item.payload["before_settled"] is None
        and item.payload["golden_dialog_count_after_cancel"] == 0
        for item in observations
    )
    start = _write(
        root,
        _GATE9_START,
        (runtime.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if common_start else "blocked",
            "complete": common_start,
            "registered_start": {
                "open_dialog_count": 1,
                "promise_settled": None,
                "dialog_title": _DIALOG_TITLE,
                "dialog_body": _DIALOG_BODY,
                "body_get_value_throws": _THROWN_MESSAGE,
            },
            "common_before_equivalent": common_start,
            "production_cancel_path_closes_in_both_variants": common_start,
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
            "state": "passed" if common_start else "blocked",
            "complete": common_start,
            "ordered_replay": [
                "load_candidate_local_bundle",
                "show_probe_dialog_with_throwing_body",
                "close_probe_dialog_with_real_cancel_click",
                "show_observed_dialog_with_throwing_body",
                "click_ok_with_real_mouse_input",
                "read_dialog_presence_and_promise_settlement",
            ],
            "actual_after_source": "candidate_controlled_dialog_lifecycle",
            "fresh_process_per_variant": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    observed = {
        variant: {
            "dialog_count": by_variant[variant].payload["after_dialog_count"],
            "settled": by_variant[variant].payload["after_settled"],
            "error": by_variant[variant].payload["after_error"],
            "uncaught_page_error": bool(
                by_variant[variant].payload["uncaught_page_errors"]
            ),
        }
        for variant in ("fixed", "buggy")
    }
    oracle_passed = common_start and observed == _EXPECTED_AFTER
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
                "a dialog whose body getValue() throws is never disposed and its "
                "promise never settles, so the dialog hangs until the page reloads"
            ),
            "expected_after": _EXPECTED_AFTER,
            "observed_after": observed,
            "common_before": {"dialog_count": 1, "settled": None},
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
            "gate57_epoch": "current-v1",
            "downstream_epoch": _EPOCH,
            "candidate_local_variant_bundles": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    if not passed:
        raise RuntimeError("#18938 Gate 8--13 semantic/visual closure failed")
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
    if (
        transition.reference != _TRANSITION
        or transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("strict_gate_1_7") is not True
        or transition.payload.get("strict_no_timeout_unknown") is not True
        or transition.payload.get("lineage_inherited") is not False
        or transition.payload.get("old_attempts_loaded") is not False
        or transition.payload.get("gate8_13_performed") is not False
    ):
        raise RuntimeError("#18938 fresh current-v1 Gate 1--7 transition is invalid")
    if (
        pool.reference != _POOL
        or pool.inputs != (transition.reference,)
        or pool.payload.get("candidate") != _CANDIDATE
        or pool.payload.get("state") != "active_qualified"
        or pool.payload.get("defect_locus") != "at_goal"
        or pool.payload.get("strict_gate_1_7") is not True
        or pool.payload.get("strict_no_timeout_unknown") is not True
        or pool.payload.get("lineage_inherited") is not False
        or pool.payload.get("old_attempts_loaded") is not False
        or pool.payload.get("gate8_13_performed") is not False
    ):
        raise RuntimeError("#18938 fresh current-v1 pool admission is invalid")


def _write_harness(run: Path, tree: Path) -> None:
    dialog = (tree / _DIALOG).as_posix()
    entry = f"""import {{ Dialog, showDialog }} from {json.dumps(dialog)};
import {{ Widget }} from '@lumino/widgets';

class ThrowingValueWidget extends Widget {{
  constructor() {{
    super();
    this.node.textContent = {json.dumps(_DIALOG_BODY)};
  }}
  getValue() {{
    throw new Error({json.dumps(_THROWN_MESSAGE)});
  }}
}}

const state = {{ settled: null, error: null }};
const open = () => {{
  showDialog({{
    title: {json.dumps(_DIALOG_TITLE)},
    body: new ThrowingValueWidget(),
    buttons: [Dialog.cancelButton(), Dialog.okButton()]
  }}).then(
    () => {{ state.settled = 'resolved'; }},
    error => {{
      state.settled = 'rejected';
      state.error = String(error && error.message);
    }}
  );
}};

window.__candidateReady = true;
window.__candidateOpen = () => {{ open(); }};
window.__candidateDialogCount = () =>
  document.querySelectorAll('.jp-Dialog').length;
window.__candidateSettled = () => ({{ ...state }});
window.__candidateResetSettled = () => {{ state.settled = null; state.error = null; }};
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
<meta charset="utf-8"><title>JupyterLab dialog getValue replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _UI_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _BASE_CSS).as_uri()}">
<link rel="stylesheet" href="{(tree / _DIALOG_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:var(--jp-layout-color0,#fff)}}
.panel{{padding:36px;font:16px var(--jp-ui-font-family,system-ui);color:#202124}}
.panel h1{{margin:0 0 10px;font-size:20px}}
.panel p{{margin:0;color:#6a737d}}
</style>
<div class="panel">
  <h1>Rename dialog replay</h1>
  <p>A dialog body whose getValue() throws is accepted with Ok.</p>
</div>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / _PLAYWRIGHT).as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{
    viewport: {{ width: 900, height: 600 }}, deviceScaleFactor: 1
  }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);
  const page_errors = [];
  page.on('pageerror', error => page_errors.push(String(error.message).slice(0, 200)));

  await page.evaluate(() => window.__candidateOpen());
  await page.waitForSelector('.jp-Dialog', {{ timeout: 15000 }});
  await wait(300);
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});
  await page.getByRole('button', {{ name: 'Cancel' }}).click({{ timeout: 15000 }});
  await wait(300);
  const golden_dialog_count = await page.evaluate(
    () => window.__candidateDialogCount()
  );
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});
  await page.evaluate(() => window.__candidateResetSettled());

  await page.evaluate(() => window.__candidateOpen());
  await page.waitForSelector('.jp-Dialog', {{ timeout: 15000 }});
  await wait(300);
  const before_dialog_count = await page.evaluate(
    () => window.__candidateDialogCount()
  );
  const before_settled = await page.evaluate(() => window.__candidateSettled());
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});
  await page.getByRole('button', {{ name: 'Ok' }}).click({{ timeout: 15000 }});
  await wait(2000);
  const after_dialog_count = await page.evaluate(
    () => window.__candidateDialogCount()
  );
  const after_settled = await page.evaluate(() => window.__candidateSettled());
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('C1_LAZARUS_18938_EDGE_OBSERVATION=' + JSON.stringify({{
    before_dialog_count, after_dialog_count, golden_dialog_count,
    before_settled, after_settled, page_errors,
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
    observed: dict[str, dict[str, object]],
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
        raise RuntimeError("#18938 Edge frame dimensions drifted")
    semantic = {
        variant: {
            golden: observed[variant] == _EXPECTED_AFTER[golden]
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
    )
    return {
        "candidate": _CANDIDATE,
        "state": "passed" if passed else "blocked",
        "complete": passed,
        "common_before_pixel_difference_mean": before_difference,
        "common_before_equivalent": before_difference == 0.0,
        "semantic_two_by_two": semantic,
        "pixel_difference_mean_two_by_two": pixel,
        "cross_rejection_thresholds": {"global_mean_gt": _GLOBAL_CROSS_THRESHOLD},
        "diagonal_matches_and_cross_rejects": passed,
        "synthetic_observation_frames": False,
        "golden_frame_source": "production_cancel_path_in_the_same_browser_session",
        "actual_after_source": "candidate_controlled_dialog_lifecycle",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _tree(root: Path, variant: str) -> Path:
    tree = _ordinary_directory(root / _TREES / variant, root)
    for relative in (
        _DIALOG,
        _DIALOG_CSS,
        _BASE_CSS,
        _UI_CSS,
        _VARIABLES_CSS,
        _RSPACK,
        _PLAYWRIGHT,
    ):
        _ordinary_file(tree / relative, tree)
    return tree


def _fingerprints(tree: Path) -> dict[str, str]:
    return {
        "dialog_fingerprint": _sha256(tree / _DIALOG),
        "dialog_css_fingerprint": _sha256(tree / _DIALOG_CSS),
        "base_css_fingerprint": _sha256(tree / _BASE_CSS),
        "ui_css_fingerprint": _sha256(tree / _UI_CSS),
        "variables_css_fingerprint": _sha256(tree / _VARIABLES_CSS),
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
        raise RuntimeError("#18938 Edge observation marker is missing or ambiguous")
    value = json.loads(matches[0].group(1))
    if not isinstance(value, dict):
        raise RuntimeError("#18938 Edge observation marker is malformed")
    return value


def _validate_observation(data: dict[str, object], variant: str, run: Path) -> None:
    frames = data.get("frames")
    before = data.get("before_settled")
    after = data.get("after_settled")
    errors = data.get("page_errors")
    expected = _EXPECTED_AFTER[variant]
    if (
        data.get("before_dialog_count") != 1
        or data.get("golden_dialog_count") != 0
        or not isinstance(before, dict)
        or before.get("settled") is not None
        or not isinstance(after, dict)
        or after.get("settled") != expected["settled"]
        or after.get("error") != expected["error"]
        or data.get("after_dialog_count") != expected["dialog_count"]
        or not isinstance(errors, list)
        or bool(errors) is not expected["uncaught_page_error"]
        or (errors and errors != [_THROWN_MESSAGE])
        or data.get("browser_channel") != "msedge"
        or not isinstance(data.get("browser_version"), str)
        or not data["browser_version"]
        or not isinstance(frames, dict)
        or set(frames) != {"before", "after", "fixed_golden", "buggy_golden"}
    ):
        raise RuntimeError(f"#18938 {variant} Edge semantic observation failed")
    for name in frames.values():
        if not isinstance(name, str):
            raise RuntimeError("#18938 Edge frame path is malformed")
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


__all__ = ["execute_jupyterlab_18938_downstream"]
