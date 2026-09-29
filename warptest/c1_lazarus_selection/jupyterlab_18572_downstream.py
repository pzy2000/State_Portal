"""Gate 8--13 real-browser closure for JupyterLab #18572.

The adapter consumes only the fresh ``current-v2`` Gate 1--7 transition and
pool admission.  It builds one candidate-local Rspack bundle per variant and
executes that bundle in Microsoft Edge through Playwright.  The HTML is only a
display/control surface: the observed after DOM is populated exclusively by
``EditorLanguageRegistry.highlight`` from the selected candidate tree.
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


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 18572}
_VERSION = "2.0"
_TREES = PurePosixPath("private/materialized/jupyterlab/18572/current-v1")
# The v1 capture rendered the single character at 22px in a 1280x720 frame, so
# the differential covered too few pixels to read as visual evidence.  This
# epoch keeps the same candidate behaviour in a frame where the glyph is legible.
_RUN = PurePosixPath("private/bounded-runs/jupyterlab/18572/real-edge-v3")
_LANGUAGE = PurePosixPath("packages/codemirror/lib/language.js")
_PARSER = PurePosixPath("packages/codemirror/test/foo.js")
_CODEMIRROR_LANGUAGE = PurePosixPath("node_modules/@codemirror/language/dist/index.js")
_RSPACK = PurePosixPath("node_modules/@rspack/cli/bin/rspack.js")
_PLAYWRIGHT = PurePosixPath("node_modules/playwright/index.js")
_MARKER = re.compile(rb"C1_LAZARUS_18572_EDGE_OBSERVATION=(\{.*\})")

_TRANSITION = ArtifactReference(
    "candidate_gate57_18572_transition/jupyterlab/18572/current-v2",
    "candidate_gate57_18572_transition",
    _VERSION,
)
_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18572/current-v2",
    "candidate_gate57_pool_admission",
    _VERSION,
)
_GATE8_MATERIALIZATION = ArtifactReference(
    "candidate_gate8_materialization/jupyterlab/18572/current-v3",
    "candidate_gate8_materialization",
    _VERSION,
)
_GATE8_RUNTIME = ArtifactReference(
    "candidate_gate8_runtime/jupyterlab/18572/current-v3",
    "candidate_gate8_runtime",
    _VERSION,
)
_GATE9_START = ArtifactReference(
    "candidate_gate9_start/jupyterlab/18572/current-v3",
    "candidate_gate9_start",
    _VERSION,
)
_GATE9_REPLAY = ArtifactReference(
    "candidate_gate9_replay/jupyterlab/18572/current-v3",
    "candidate_gate9_replay",
    _VERSION,
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_gate10_hidden_oracle/jupyterlab/18572/current-v3",
    "candidate_gate10_hidden_oracle",
    _VERSION,
)
_GATE12_MATRIX = ArtifactReference(
    "candidate_gate12_semantic_pixel_matrix/jupyterlab/18572/current-v3",
    "candidate_gate12_semantic_pixel_matrix",
    _VERSION,
)
_GATE13_LINEAGE = ArtifactReference(
    "candidate_gate13_same_lineage/jupyterlab/18572/current-v3",
    "candidate_gate13_same_lineage",
    _VERSION,
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/jupyterlab/18572/current-v3",
    "candidate_formal_freeze_admission",
    _VERSION,
)

ProcessRunner = Callable[..., NativeProcessReceipt]


def execute_jupyterlab_18572_downstream(
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
    _validate_fresh_gate57_inputs(transition, pool)
    trees = {variant: _tree(root, variant) for variant in ("fixed", "buggy")}
    fingerprints = {variant: _fingerprints(tree) for variant, tree in trees.items()}
    if (
        fingerprints["fixed"]["parser_fingerprint"]
        != fingerprints["buggy"]["parser_fingerprint"]
    ):
        raise RuntimeError("#18572 fixed/buggy parser fixture drifted")
    if (
        fingerprints["fixed"]["language_fingerprint"]
        == fingerprints["buggy"]["language_fingerprint"]
    ):
        raise RuntimeError("#18572 fixed/buggy production build is not an inverse pair")

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
            "production_module": _LANGUAGE.as_posix(),
            "parser_module": _PARSER.as_posix(),
            "codemirror_language_module": _CODEMIRROR_LANGUAGE.as_posix(),
            "candidate_local_direct_imports": True,
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
        build = runner(
            (
                node,
                str(tree / _RSPACK),
                "build",
                "--config",
                str(run / "rspack.config.cjs"),
            ),
            cwd=run,
            env=environment,
            timeout_seconds=600,
        )
        _require_completed(build, f"#18572 {variant} Rspack build")
        bundle = _ordinary_file(run / "dist/bundle.js", run)
        browser = runner(
            (node, str(run / "edge-observe.cjs")),
            cwd=run,
            env=environment,
            timeout_seconds=180,
        )
        _require_completed(browser, f"#18572 {variant} Microsoft Edge observation")
        data = _parse_observation(browser)
        _validate_observation(data, variant, run)
        runtime_receipts[variant] = {
            "rspack_argv": list(
                (
                    node,
                    str(tree / _RSPACK),
                    "build",
                    "--config",
                    str(run / "rspack.config.cjs"),
                )
            ),
            "rspack_stdout_base64": base64.b64encode(build.stdout).decode("ascii"),
            "rspack_stderr_base64": base64.b64encode(build.stderr).decode("ascii"),
            "bundle_fingerprint": _sha256(bundle),
            "browser_argv": [node, str(run / "edge-observe.cjs")],
            "browser_version": data["browser_version"],
            "browser_channel": data["browser_channel"],
        }
        observation_ref = ArtifactReference(
            f"candidate_gate11_edge_observation/jupyterlab/18572/{variant}/current-v3",
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
                    "before": data["before"],
                    "after": data["after"],
                    "frames": {
                        key: (run / value).relative_to(root).as_posix()
                        for key, value in data["frames"].items()
                    },
                    "capture_backend": "playwright_microsoft_edge_png",
                    "browser_channel": "msedge",
                    "after_dom_candidate_controlled": True,
                    "candidate_appended_dom_nodes": variant == "fixed",
                    "html_prefilled_oracle_text": False,
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
    common_before = (
        by_variant["fixed"].payload["before"]
        == by_variant["buggy"].payload["before"]
        == ""
    )
    start = _write(
        root,
        _GATE9_START,
        (runtime.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if common_before else "blocked",
            "complete": common_before,
            "registered_start": {
                "output_text": "",
                "input_code": "x",
                "language": "foo",
            },
            "common_before_equivalent": common_before,
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
            "state": "passed" if common_before else "blocked",
            "complete": common_before,
            "ordered_replay": [
                "load_candidate_local_bundle",
                "restore_empty_registered_start",
                "invoke_EditorLanguageRegistry.highlight('x')",
                "read_candidate_mutated_output_text",
            ],
            "actual_after_source": "candidate_code_controlled_dom_target",
            "fresh_process_per_variant": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    observed = {
        variant: str(by_variant[variant].payload["after"])
        for variant in ("fixed", "buggy")
    }
    expected = {"fixed": "x", "buggy": ""}
    oracle_passed = common_before and observed == expected
    oracle = _write(
        root,
        _GATE10_ORACLE,
        (replay.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if oracle_passed else "blocked",
            "complete": oracle_passed,
            "hidden_until_after_candidate_execution": True,
            "expected_after": expected,
            "observed_after": observed,
            "common_before": "",
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    matrix_payload = _matrix(root, by_variant, observed, expected)
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
            "gate57_epoch": "current-v2",
            "downstream_epoch": "current-v3",
            "candidate_local_variant_bundles": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    if not passed:
        raise RuntimeError("#18572 Gate 8--13 semantic/visual closure failed")
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


def _validate_fresh_gate57_inputs(
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
        raise RuntimeError("#18572 fresh current-v2 Gate 1--7 transition is invalid")
    if (
        pool.reference != _POOL
        or pool.inputs != (transition.reference,)
        or pool.payload.get("candidate") != _CANDIDATE
        or pool.payload.get("state") != "active_qualified"
        or pool.payload.get("strict_gate_1_7") is not True
        or pool.payload.get("strict_no_timeout_unknown") is not True
        or pool.payload.get("lineage_inherited") is not False
        or pool.payload.get("old_attempts_loaded") is not False
        or pool.payload.get("gate8_13_performed") is not False
    ):
        raise RuntimeError("#18572 fresh current-v2 pool admission is invalid")


def _write_harness(run: Path, tree: Path) -> None:
    language = (tree / _LANGUAGE).as_posix()
    parser = (tree / _PARSER).as_posix()
    codemirror = (tree / _CODEMIRROR_LANGUAGE).as_posix()
    entry = f"""import {{ EditorLanguageRegistry }} from {json.dumps(language)};
import {{ parser }} from {json.dumps(parser)};
import {{ LanguageSupport, LRLanguage }} from {json.dumps(codemirror)};

const registry = new EditorLanguageRegistry();
const support = new LanguageSupport(LRLanguage.define({{ parser }}));
const language = {{ name: 'foo', mime: 'text/x-foo', support }};
window.__candidateReady = true;
window.__runCandidate = async () => {{
  const output = document.getElementById('candidate-output');
  if (output.textContent !== '') throw new Error('registered start is not empty');
  await registry.highlight('x', language, output);
  window.__candidateAfter = output.textContent;
  return window.__candidateAfter;
}};
"""
    config = f"""module.exports = {{
  mode: 'production', target: 'web', devtool: false,
  context: {json.dumps(tree.as_posix())},
  entry: {json.dumps((run / "entry.js").as_posix())},
  output: {{ path: {json.dumps((run / "dist").as_posix())}, filename: 'bundle.js', clean: true }},
  resolve: {{
    modules: [{json.dumps((tree / "node_modules").as_posix())}], symlinks: false,
    alias: {{
      '@jupyterlab/codeeditor$': {json.dumps((run / "stubs/codeeditor.js").as_posix())},
      '@jupyterlab/coreutils$': {json.dumps((run / "stubs/coreutils.js").as_posix())},
      '@jupyterlab/translation$': {json.dumps((run / "stubs/translation.js").as_posix())}
    }}
  }},
  optimization: {{ minimize: false }}
}};
"""
    html_document = """<!doctype html>
<meta charset="utf-8"><title>JupyterLab single-character highlight replay</title>
<style>
html,body{margin:0;width:100%;height:100%;background:#f4f6f8;color:#202124;font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{padding:24px 6%}.card{background:white;border:1px solid #cfd4da;border-radius:8px;box-shadow:0 2px 8px #0001}
.header{padding:14px 18px;border-bottom:1px solid #e3e6e8;font-weight:600}.row{display:grid;grid-template-columns:1fr 1fr;min-height:150px}
.pane{padding:28px 34px}.pane+.pane{border-left:1px solid #e3e6e8}.label{color:#6a737d;margin-bottom:18px}.code,.candidate-output{margin:0;white-space:pre-wrap;font:64px ui-monospace,SFMono-Regular,Menlo,monospace}.preview:not([data-attempted])::after{content:'Not rendered yet';color:#8a929a;font-style:italic}.candidate-output:empty{display:inline}
</style><main class="shell"><section class="card"><div class="header">CodeMirror language highlight</div><div class="row"><div class="pane"><div class="label">Input code</div><pre class="code">x</pre></div><div id="preview" class="pane preview"><div class="label">Rendered preview</div><pre id="candidate-output" class="candidate-output"></pre></div></div></section></main>
<script src="dist/bundle.js"></script>
"""
    playwright = (tree / _PLAYWRIGHT).as_posix()
    browser = f"""const path = require('node:path');
const {{ pathToFileURL }} = require('node:url');
const {{ chromium }} = require({json.dumps(playwright)});
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const page = await browser.newPage({{ viewport: {{ width: 720, height: 320 }}, deviceScaleFactor: 1 }});
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
  await page.waitForFunction(() => window.__candidateReady === true);
  const output = page.locator('#candidate-output');
  const before = await output.textContent();
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});
  const after = await page.evaluate(() => window.__runCandidate());
  await page.evaluate(() => document.getElementById('preview').dataset.attempted = 'true');
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});
  await page.evaluate(() => document.getElementById('candidate-output').replaceChildren(document.createTextNode('x')));
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});
  await page.evaluate(() => document.getElementById('candidate-output').replaceChildren());
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});
  console.log('C1_LAZARUS_18572_EDGE_OBSERVATION=' + JSON.stringify({{
    before, after, browser_channel: 'msedge', browser_version: browser.version(),
    after_dom_candidate_controlled: true,
    candidate_appended_dom_nodes: after !== '',
    frames: {{ before: 'before.png', after: 'after.png', fixed_golden: 'fixed-golden.png', buggy_golden: 'buggy-golden.png' }}
  }}));
  await browser.close();
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    stubs = {
        "stubs/codeeditor.js": (
            "export const IEditorMimeTypeService = { defaultMimeType: 'text/plain' };\n"
        ),
        "stubs/coreutils.js": (
            "export const PathExt = { basename: value => "
            "String(value).replace(/\\\\/g, '/').split('/').pop() };\n"
        ),
        "stubs/translation.js": (
            "export const nullTranslator = { load: () => ({ __: value => value }) };\n"
        ),
    }
    for name, text in (
        ("entry.js", entry),
        ("rspack.config.cjs", config),
        ("index.html", html_document),
        ("edge-observe.cjs", browser),
        *stubs.items(),
    ):
        target = run / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _matrix(
    root: Path,
    observations: dict[str, StableArtifact],
    observed: dict[str, str],
    expected: dict[str, str],
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
        raise RuntimeError("#18572 Edge frame dimensions drifted")
    width, height = sizes["fixed"]
    roi_bbox = (width // 2, int(height * 0.29), int(width * 0.875), int(height * 0.60))
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
            golden: observed[variant] == expected[golden]
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
        and pixel["fixed"]["buggy"] > 0.005
        and pixel["buggy"]["fixed"] > 0.005
        and pixel["buggy"]["buggy"] == 0.0
        and pixel_roi["fixed"]["fixed"] == 0.0
        and pixel_roi["fixed"]["buggy"] > 0.05
        and pixel_roi["buggy"]["fixed"] > 0.05
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
        "preview_roi_bbox": list(roi_bbox),
        "preview_roi_difference_mean_two_by_two": pixel_roi,
        "cross_rejection_thresholds": {
            "global_mean_gt": 0.005,
            "preview_roi_mean_gt": 0.05,
        },
        "diagonal_matches_and_cross_rejects": passed,
        "synthetic_observation_frames": False,
        "golden_frame_source": "browser_constructed_hidden_semantic_oracle",
        "actual_after_source": "candidate_code_controlled_dom_target",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _tree(root: Path, variant: str) -> Path:
    tree = _ordinary_directory(root / _TREES / variant, root)
    for relative in (_LANGUAGE, _PARSER, _CODEMIRROR_LANGUAGE, _RSPACK, _PLAYWRIGHT):
        _ordinary_file(tree / relative, tree)
    return tree


def _fingerprints(tree: Path) -> dict[str, str]:
    return {
        "language_fingerprint": _sha256(tree / _LANGUAGE),
        "parser_fingerprint": _sha256(tree / _PARSER),
        "codemirror_language_fingerprint": _sha256(tree / _CODEMIRROR_LANGUAGE),
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
        raise RuntimeError("#18572 Edge observation marker is missing or ambiguous")
    value = json.loads(matches[0].group(1))
    if not isinstance(value, dict):
        raise RuntimeError("#18572 Edge observation marker is malformed")
    return value


def _validate_observation(data: dict[str, object], variant: str, run: Path) -> None:
    expected = "x" if variant == "fixed" else ""
    frames = data.get("frames")
    if (
        data.get("before") != ""
        or data.get("after") != expected
        or data.get("browser_channel") != "msedge"
        or not isinstance(data.get("browser_version"), str)
        or not data["browser_version"]
        or data.get("after_dom_candidate_controlled") is not True
        or data.get("candidate_appended_dom_nodes") is not (variant == "fixed")
        or not isinstance(frames, dict)
        or set(frames) != {"before", "after", "fixed_golden", "buggy_golden"}
    ):
        raise RuntimeError(f"#18572 {variant} Edge semantic observation failed")
    for name in frames.values():
        if not isinstance(name, str):
            raise RuntimeError("#18572 Edge frame path is malformed")
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


__all__ = ["execute_jupyterlab_18572_downstream"]
