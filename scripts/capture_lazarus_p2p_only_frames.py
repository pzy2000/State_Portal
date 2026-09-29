#!/usr/bin/env python3
"""Capture and publish the four golden frames for each P2P-only task.

For every task and both products this rebuilds the repaired package so a reverted
source reaches the browser, bundles the task's fixture, drives it in Chromium
through Playwright, and screenshots the registered start and the state the public
reference steps end on.  The four frames are then published through
``register_lazarus_golden_set``, which is what enforces that the two starts are
equivalent and the two ends are not.

Nothing here decides an outcome.  The fixture supplies scaffolding; every line of
repaired code and every stylesheet comes from the materialized tree.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_lazarus_selection.p2p_only_frames import (  # noqa: E402
    CSS_ONLY_STEPS,
    FRAME_SPECS,
    VIEWPORT,
    FrameSpec,
)
from warptest.c1_lazarus_selection.p2p_only_engine_capture import (  # noqa: E402
    capture_engine_variant,
    validate_engine_variant_differential,
)
from warptest.c1_lazarus_selection.p2p_only_gate16 import materialize_pair  # noqa: E402
from warptest.c1_lazarus_selection.p2p_only_profiles import (  # noqa: E402
    load_profile_registry,
)
from warptest.c1_lazarus_selection.p2p_only_task_text import TASK_TEXT  # noqa: E402
from warptest.lazarus_goldens import (  # noqa: E402
    LazarusGoldenError,
    LazarusGoldenFrame,
    register_lazarus_golden_set,
)

RUN_ROOT = Path(".warptest-cache/lazarus-p2p-only/frames")
SPEC_ROOTS = {
    "jupyterlab": Path("specs/jupyterlab/lazarus_p2p_only"),
    "luanti": Path("specs/luanti/lazarus_p2p_only"),
    "openra": Path("specs/openra/lazarus_p2p_only"),
}
SPEC_ROOT = SPEC_ROOTS["jupyterlab"]
TREE_ROOT = Path(".warptest-cache/lazarus-p2p-only/trees")
NODE = shutil.which("node") or "/usr/bin/node"


BROWSERS_PATH = Path.home() / "Library/Caches/ms-playwright"


def run(argv: tuple[str, ...], *, cwd: Path, timeout: int = 1800) -> None:
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        # A private HOME keeps a build from reading or writing a user cache.  The
        # browser cache is named explicitly instead, because it is a fixed
        # dependency rather than state this run may create.
        "HOME": str(cwd),
        "LANG": "C",
        "LC_ALL": "C",
        "NODE_ENV": "production",
        "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_PATH),
    }
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} failed in {cwd}:\n"
            + result.stdout.decode("utf-8", "replace")[-3000:]
            + result.stderr.decode("utf-8", "replace")[-3000:]
        )


def build_packages(tree: Path, spec: FrameSpec) -> None:
    """Recompile so a reverted source is what the browser loads."""

    tsc = tree / "node_modules/typescript/bin/tsc"
    for package in spec.build_packages:
        run(
            (NODE, str(tsc), "--build", "tsconfig.json", "--force"),
            cwd=tree / package,
        )


def inline_styles(tree: Path, spec: FrameSpec) -> str:
    chunks: list[str] = []
    for relative in spec.stylesheets:
        path = tree / PurePosixPath(relative)
        if not path.is_file():
            raise RuntimeError(f"stylesheet is missing from the tree: {relative}")
        chunks.append(f"/* {relative} */\n" + path.read_text(encoding="utf-8"))
    chunks.append("/* fixture */\n" + spec.fixture_css)
    return "\n".join(chunks)


def write_document(run_dir: Path, spec: FrameSpec, styles: str, *, bundled: bool) -> Path:
    script = '<script src="./dist/bundle.js"></script>' if bundled else ""
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{styles}
</style></head>
<body>
{spec.body_html}
{script}
</body></html>
"""
    path = run_dir / "document.html"
    path.write_text(document, encoding="utf-8")
    return path


def bundle(run_dir: Path, tree: Path, spec: FrameSpec) -> None:
    entry_source = spec.entry(tree) if spec.entry is not None else ""
    (run_dir / "entry.js").write_text(entry_source, encoding="utf-8")
    config = f"""module.exports = {{
  mode: 'production', target: 'web', devtool: false,
  context: {json.dumps(tree.as_posix())},
  entry: {json.dumps((run_dir / "entry.js").as_posix())},
  output: {{ path: {json.dumps((run_dir / "dist").as_posix())},
    filename: 'bundle.js', clean: true }},
  resolve: {{ modules: [{json.dumps((tree / "node_modules").as_posix())}],
    symlinks: false }},
  module: {{ rules: [
    {{ test: /\\.svg$/, type: 'asset/source' }},
    {{ test: /\\.(css|gif|ttf|eot|woff2?)$/, type: 'asset/source' }}
  ] }},
  optimization: {{ minimize: false }},
  stats: 'errors-warnings'
}};
"""
    (run_dir / "rspack.config.js").write_text(config, encoding="utf-8")
    run(
        (
            NODE,
            str(tree / "node_modules/@rspack/cli/bin/rspack.js"),
            "build",
            "--config",
            str(run_dir / "rspack.config.js"),
        ),
        cwd=run_dir,
    )


_DRIVER = r"""
const { chromium } = require(process.argv[2]);
const document_url = 'file://' + process.argv[3];
const outputDir = process.argv[4];
const cssStep = process.argv[5] || '';
const cssTarget = process.argv[6] || '';
const width = Number(process.argv[7]);
const height = Number(process.argv[8]);
const clipArgument = process.argv[9] || '';
const clip = clipArgument ? JSON.parse(clipArgument) : undefined;
const shot = path => (clip ? { path, clip } : { path });

const applyCssStep = ({ kind, selector }) => {
  const host = document.querySelector(selector);
  if (!host) {
    throw new Error('css step target is missing: ' + selector);
  }
  if (kind === 'mark_region') {
    host.classList.add('marked');
    return { marked: true, region_color: getComputedStyle(host).backgroundColor };
  }
  if (kind === 'mark_chip') {
    host.classList.add('jp-mod-selected-InputText');
    const style = getComputedStyle(host);
    return { marked: true, chip_color: style.backgroundColor };
  }
  if (kind === 'mark_match') {
    host.classList.add('matched');
    const style = getComputedStyle(host);
    return {
      marked: true,
      text_color: style.color,
      marker_color: style.backgroundColor,
      readable: style.color !== style.backgroundColor
    };
  }
  if (kind === 'append_glyph_button') {
    const button = document.createElement('button');
    button.className = 'glyph-face';
    button.textContent = 'WWWiii';
    host.appendChild(button);
    return {
      placed: true,
      font_family: getComputedStyle(button).fontFamily,
      label_width: Math.round(button.getBoundingClientRect().width)
    };
  }
  throw new Error('unknown css step: ' + kind);
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height } });
  const messages = [];
  page.on('console', message => messages.push(message.type() + ': ' + message.text()));
  page.on('pageerror', error => messages.push('pageerror: ' + error.message));
  await page.goto(document_url, { waitUntil: 'load' });
  if (!cssStep) {
    await page.waitForFunction('window.__ready === true', { timeout: 60000 });
  }
  await page.waitForTimeout(400);

  const readState = async () =>
    cssStep ? {} : await page.evaluate('window.__state()');

  const before = await readState();
  await page.screenshot(shot(outputDir + '/before.png'));

  let stepResult = {};
  if (cssStep) {
    stepResult = await page.evaluate(applyCssStep, {
      kind: cssStep,
      selector: cssTarget
    });
    await page.waitForTimeout(300);
  } else {
    await page.evaluate('window.__step()');
    await page.waitForTimeout(300);
  }

  const after = await readState();
  await page.screenshot(shot(outputDir + '/after.png'));
  await browser.close();

  const report = { before, after, step: stepResult, console: messages };
  require('fs').writeFileSync(
    outputDir + '/report.json',
    JSON.stringify(report, null, 2) + '\n'
  );
  if (messages.some(line => line.startsWith('pageerror'))) {
    console.error(JSON.stringify(messages, null, 2));
    process.exit(3);
  }
})().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(2);
});
"""


def capture_variant(
    *, tree: Path, run_dir: Path, spec: FrameSpec, task_id: str
) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    styles = inline_styles(tree, spec)
    if spec.needs_bundle:
        bundle(run_dir, tree, spec)
    document = write_document(run_dir, spec, styles, bundled=spec.needs_bundle)

    driver = run_dir / "drive.cjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    css_step, css_target = "", ""
    if not spec.needs_bundle:
        target, kind = CSS_ONLY_STEPS[task_id]
        css_step, css_target = kind, target
    run(
        (
            NODE,
            str(driver),
            str(tree / "node_modules/playwright/index.js"),
            str(document),
            str(run_dir),
            css_step,
            css_target,
            str(VIEWPORT["width"]),
            str(VIEWPORT["height"]),
            json.dumps(
                {
                    "x": spec.clip[0],
                    "y": spec.clip[1],
                    "width": spec.clip[2],
                    "height": spec.clip[3],
                }
            )
            if spec.clip is not None
            else "",
        ),
        cwd=run_dir,
        timeout=600,
    )
    return {
        "before": run_dir / "before.png",
        "after": run_dir / "after.png",
        "report": run_dir / "report.json",
    }


def split_reports(run_dir: Path, frames: dict[str, Path]) -> dict[str, Path]:
    """Give each frame its own run report, as the publisher requires."""

    payload = json.loads(frames["report"].read_text(encoding="utf-8"))
    written: dict[str, Path] = {}
    for stage in ("before", "after"):
        stage_payload = (
            payload.get("stages", {}).get(stage)
            if isinstance(payload.get("stages"), dict)
            else None
        )
        if payload.get("schema") == "c1-lazarus-p2p-only-native-capture-v1":
            if not isinstance(stage_payload, dict) or stage_payload.get("matched_expected") is not True:
                raise RuntimeError(f"native {stage} capture did not match its expected state")
            observed = stage_payload
        else:
            observed = payload.get(stage)
        path = run_dir / f"{stage}.report.json"
        path.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "source_schema": payload.get("schema"),
                    "observed": observed,
                    "matched_expected": (
                        stage_payload.get("matched_expected")
                        if isinstance(stage_payload, dict)
                        else True
                    ),
                    "step": payload.get("step") if stage == "after" else {},
                    "console": payload.get("console"),
                    "variant_differential": payload.get("variant_differential"),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        written[stage] = path
    return written


def report_semantics_ok(path: Path) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("matched_expected") is True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="restrict to one task id")
    parser.add_argument(
        "--republish",
        action="store_true",
        help="remove an existing golden directory before publishing",
    )
    args = parser.parse_args()

    wanted = set(args.task or ())
    profiles = [
        profile
        for profile in load_profile_registry(REPOSITORY_ROOT)
        if (
            profile.task_id in wanted
            if wanted
            else profile.catalog_admission
        )
    ]
    if not profiles:
        print("no registered profile matched", file=sys.stderr)
        return 1

    results: list[dict[str, object]] = []
    for profile in profiles:
        task_id = profile.task_id
        spec = FRAME_SPECS.get(task_id)
        if spec is None:
            print(f"== {task_id} ==\n  no capture fixture registered")
            continue
        print(f"== {task_id} ==")
        try:
            fixed, buggy, _ = materialize_pair(REPOSITORY_ROOT, profile)
            captured: dict[str, dict[str, Path]] = {}
            for variant, tree in (("fixed", fixed), ("buggy", buggy)):
                print(f"  building and driving {variant}")
                build_packages(tree, spec)
                run_dir = REPOSITORY_ROOT / RUN_ROOT / task_id / variant
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                if spec.capture_kind != "playwright":
                    frames = capture_engine_variant(
                        host=profile.host,
                        tree=tree,
                        run_dir=run_dir,
                        spec=spec,
                        variant=variant,
                    )
                else:
                    frames = capture_variant(
                        tree=tree, run_dir=run_dir, spec=spec, task_id=task_id
                    )
                captured[variant] = frames

            if spec.capture_kind != "playwright":
                validate_engine_variant_differential(spec=spec, captured=captured)
            for variant in ("fixed", "buggy"):
                run_dir = REPOSITORY_ROOT / RUN_ROOT / task_id / variant
                reports = split_reports(run_dir, captured[variant])
                captured[variant].update(
                    {f"{key}_report": value for key, value in reports.items()}
                )

            spec_root = SPEC_ROOTS.get(profile.host, SPEC_ROOT)
            output_dir = spec_root / f"visual_{task_id}"
            absolute_output = REPOSITORY_ROOT / output_dir
            if args.republish and absolute_output.exists():
                shutil.rmtree(absolute_output)
            published = register_lazarus_golden_set(
                task_id=task_id,
                repository_root=REPOSITORY_ROOT,
                output_dir=output_dir,
                fixed_before=LazarusGoldenFrame(
                    image=captured["fixed"]["before"],
                    report=captured["fixed"]["before_report"],
                    expected_semantics_ok=report_semantics_ok(
                        captured["fixed"]["before_report"]
                    ),
                ),
                buggy_before=LazarusGoldenFrame(
                    image=captured["buggy"]["before"],
                    report=captured["buggy"]["before_report"],
                    expected_semantics_ok=report_semantics_ok(
                        captured["buggy"]["before_report"]
                    ),
                ),
                fixed_after=LazarusGoldenFrame(
                    image=captured["fixed"]["after"],
                    report=captured["fixed"]["after_report"],
                    expected_semantics_ok=report_semantics_ok(
                        captured["fixed"]["after_report"]
                    ),
                ),
                buggy_after=LazarusGoldenFrame(
                    image=captured["buggy"]["after"],
                    report=captured["buggy"]["after_report"],
                    expected_semantics_ok=report_semantics_ok(
                        captured["buggy"]["after_report"]
                    ),
                ),
                ignore_top_fraction=spec.ignore_top_fraction,
                after_min_changed_pixel_ratio=spec.after_min_changed_pixel_ratio,
            )
        except (RuntimeError, LazarusGoldenError, OSError) as error:
            print(f"  BLOCKED {str(error)[:1500]}")
            results.append({"task_id": task_id, "published": False, "error": str(error)})
            continue
        print(
            "  published before-equivalence "
            f"{published.before_metrics['changed_pixel_ratio']:.6f}, "
            f"after-differential {published.after_metrics['changed_pixel_ratio']:.6f}"
        )
        results.append(
            {
                "task_id": task_id,
                "published": True,
                "before_changed_pixel_ratio": published.before_metrics[
                    "changed_pixel_ratio"
                ],
                "after_changed_pixel_ratio": published.after_metrics[
                    "changed_pixel_ratio"
                ],
                "goldens": str(published.metadata.relative_to(REPOSITORY_ROOT)),
                "has_authored_text": task_id in TASK_TEXT,
            }
        )

    report_path = REPOSITORY_ROOT / "reports/lazarus-p2p-only-frames.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema": "c1-lazarus-p2p-only-frames-v1",
                "schema_version": 1,
                "captured_at": datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "published": sum(1 for item in results if item.get("published")),
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    published = sum(1 for item in results if item.get("published"))
    print(f"\npublished {published}/{len(results)} four-frame sets")
    return 0 if published == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
