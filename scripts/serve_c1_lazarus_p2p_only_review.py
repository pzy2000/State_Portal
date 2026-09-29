#!/usr/bin/env python3
"""Serve the human review page for the P2P-only C1-Lazarus catalog.

The frozen review page grades rows whose defect is proven by a fail-to-pass test.
These rows have none, so the page is built around the one thing that does separate
the two products here: the published frames.  Every card therefore leads with the
four frames, states the recorded before-equivalence and after-differential, and
says in plain words that the six control cells passed on both products and prove
only that both run.

Run with ``--export`` to write the same page and its images to a directory that
can be opened without a server.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_lazarus_selection.p2p_only_task_text import TASK_TEXT  # noqa: E402
from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402

CATALOG_PATH = REPOSITORY_ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml"
DEFAULT_REVIEW_PATH = (
    REPOSITORY_ROOT / "reports/c1-lazarus-p2p-only-human-reviews.json"
)
DEFAULT_EXPORT_DIR = REPOSITORY_ROOT / "docs/c1_lazarus_p2p_only_REVIEW_HTML"
IMAGE_ROLES = ("before", "buggy-before", "fixed-after", "buggy-after")
ROLE_CAPTIONS = {
    "before": "Fixed before (shared start)",
    "buggy-before": "Buggy before (start the equivalence was measured on)",
    "fixed-after": "Fixed after (goal frame)",
    "buggy-after": "Buggy after (defect frame)",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _resolve(spec_dir: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    path = (spec_dir / relative).resolve()
    return path if path.is_file() else None


def _strings(rubric: dict[str, Any], key: str) -> list[str]:
    value = rubric.get(key) or []
    return [item for item in value if isinstance(item, str)]


def _union(first: list[str], second: list[str]) -> list[str]:
    return first + [item for item in second if item not in first]


def merge_after_rubrics(
    task_id: str, fixed: dict[str, Any], buggy: dict[str, Any]
) -> dict[str, list[str]]:
    """Fold the two per-branch after rubrics into one buggy-or-correct rubric.

    Both branches are authored from the same three parts -- the scene, the
    branch's own frame description, and the readout that names where to look --
    so everything except the frame description is shared.  Keeping the shared
    part once leaves the fixed branch's description as the only condition a
    frame has to meet: a frame that meets it comes from a build without the
    defect, and a frame that does not comes from a build carrying it.  The
    buggy branch's description is not carried over, so the verdict turns on
    what the repaired product does rather than on recognising one known way of
    getting it wrong.
    """

    fixed_must = _strings(fixed, "must")
    buggy_must = _strings(buggy, "must")
    readout = TASK_TEXT[task_id].readout

    correct_if = [item for item in fixed_must if item not in buggy_must]
    buggy_only = [item for item in buggy_must if item not in fixed_must]
    if not correct_if or not buggy_only:
        raise ValueError(
            f"{task_id}: the two after rubrics do not separate the products, so "
            "no single rubric can classify a frame as buggy or correct"
        )
    if readout and not (readout in fixed_must and readout in buggy_must):
        raise ValueError(
            f"{task_id}: the registered readout is missing from an after rubric, "
            "so the published rubrics have drifted from the authored text"
        )

    shared = [
        item for item in fixed_must if item in buggy_must and item != readout
    ]
    return {
        "must": shared,
        "decide_by": [readout] if readout else [],
        "correct_if": correct_if,
        "must_not": _union(_strings(fixed, "must_not"), _strings(buggy, "must_not")),
        "ignore": _union(_strings(fixed, "ignore"), _strings(buggy, "ignore")),
    }


def load_review_tasks(
    catalog_path: Path = CATALOG_PATH,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """Load the catalog and resolve one review card plus four frames per task."""

    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{catalog_path}: catalog must be a mapping")
    if payload.get("p2p_only_population") is not True:
        raise ValueError(f"{catalog_path}: this page only serves the P2P-only catalog")
    _, rows = validate_lazarus_catalog(payload, source_path=catalog_path)

    tasks: list[dict[str, Any]] = []
    images: dict[str, Path] = {}
    for index, row in enumerate(rows, start=1):
        task_id = str(row["id"])
        spec_path = (REPOSITORY_ROOT / str(row["oracle_spec"])).resolve()
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        lazarus = spec["evaluation"]["lazarus"]
        goldens = lazarus["goldens"]
        spec_dir = spec_path.parent

        frames = {
            "before": _resolve(spec_dir, goldens.get("before")),
            "buggy-before": _resolve(spec_dir, goldens.get("buggy_before")),
            "fixed-after": _resolve(spec_dir, goldens["fixed"].get("after")),
            "buggy-after": _resolve(spec_dir, goldens["buggy"].get("after")),
        }
        missing = sorted(role for role, path in frames.items() if path is None)
        if missing:
            raise ValueError(f"{task_id}: missing golden frames {missing}")
        for role, path in frames.items():
            images[f"img/{task_id}/{role}.png"] = path

        gate = json.loads(
            (REPOSITORY_ROOT / str(row["defect"]["gate_report"])).read_text(
                encoding="utf-8"
            )
        )
        metadata = json.loads((spec_dir / f"visual_{task_id}/goldens.meta.json").read_text(encoding="utf-8"))
        control = gate["evidence"]["control_matrix"]
        tasks.append(
            {
                "index": index,
                "task_id": task_id,
                "benchmark": row["benchmark"],
                "horizon_class": row["horizon_class"],
                "defect_locus": row["defect"]["defect_locus"],
                "preconditions": row["preconditions"],
                "reference_steps": row["reference_steps"],
                "state_description": row["state_description"],
                "upstream_pr": row["defect"]["upstream"]["pr"],
                "upstream_issue": row["defect"]["upstream"]["issue"],
                "oracle_spec": str(row["oracle_spec"]),
                "visual_rubric": spec["evaluation"]["visual_oracle"]["rubric"],
                "after_rubric": merge_after_rubrics(
                    task_id, goldens["fixed"]["after_rubric"],
                    goldens["buggy"]["after_rubric"],
                ),
                "fixed_after_description": goldens["fixed"]["after_description"],
                "buggy_after_description": goldens["buggy"]["after_description"],
                "f2p_absence": lazarus["f2p_absence"]["reason"],
                "gate_1_6_status": gate["gate_1_6_status"],
                "defect_differential_source": gate["defect_differential_source"],
                "control_test_path": control["control_test_path"],
                "control_test_relation": control["control_test_relation"],
                "control_cells": control["cells"],
                "before_ratio": metadata["before_equivalence"]["changed_pixel_ratio"],
                "after_ratio": metadata["after_differential"]["changed_pixel_ratio"],
                "identical_starts": (
                    metadata["before_sha256"] == metadata.get("before_buggy_sha256")
                ),
                "production_paths": gate["evidence"]["patch"]["production_paths"],
                "frames": frames,
            }
        )
    return tasks, images


_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.6 -apple-system, "Helvetica Neue", sans-serif;
  background: #f4f5f7; color: #1a1a1a; }
.hero { padding: 28px 32px 18px; background: #12203a; color: #fff; }
.hero h1 { margin: 0 0 8px; font-size: 24px; }
.hero p { margin: 4px 0; max-width: 900px; color: #ccd6e8; }
.banner { margin: 16px 32px 0; padding: 14px 18px; border-radius: 8px;
  background: #fff8e1; border: 1px solid #e0c060; }
.banner strong { color: #7a5300; }
main { padding: 20px 32px 60px; }
.card { background: #fff; border: 1px solid #d8dce3; border-radius: 10px;
  padding: 20px 22px; margin-bottom: 22px; }
.card h2 { margin: 0 0 4px; font-size: 18px; font-family: ui-monospace, monospace; }
.meta { color: #5a6473; font-size: 13px; margin-bottom: 14px; }
.meta span { margin-right: 14px; }
.prompt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 12px 0; }
.prompt-grid .full { grid-column: 1 / -1; }
.prompt-grid h4, .rubric h4 { margin: 0 0 4px; font-size: 12px; letter-spacing: .06em;
  text-transform: uppercase; color: #5a6473; }
pre { white-space: pre-wrap; background: #f7f8fa; border: 1px solid #e3e6eb;
  border-radius: 6px; padding: 10px; margin: 0; font-size: 13px; }
.shot-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px;
  margin-top: 16px; }
.shot { border: 1px solid #d8dce3; border-radius: 8px; overflow: hidden; background: #fbfbfc; }
.shot figcaption { padding: 6px 10px; font-size: 12px; color: #37414f;
  border-bottom: 1px solid #e3e6eb; background: #f2f4f7; }
.shot img { display: block; width: 100%; height: auto; background:
  repeating-conic-gradient(#eee 0% 25%, #fff 0% 50%) 50% / 16px 16px; }
.rubric-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
  gap: 12px; margin-top: 16px; }
.rubric { background: #f7f8fa; border: 1px solid #e3e6eb; border-radius: 6px; padding: 10px; }
.rubric ul { margin: 4px 0 8px 16px; padding: 0; font-size: 13px; }
.after-rubric > h4 { color: #12203a; font-size: 13px; text-transform: none;
  letter-spacing: 0; }
.verdict { border-left: 3px solid #c4cbd6; padding-left: 8px; margin: 8px 0; }
.verdict.correct { border-left-color: #2f7a45; }
.verdict.correct h4 { color: #2f7a45; }
table.gate { border-collapse: collapse; margin-top: 14px; font-size: 13px; width: 100%; }
table.gate th, table.gate td { border: 1px solid #e3e6eb; padding: 5px 8px; text-align: left; }
table.gate th { background: #f2f4f7; font-weight: 600; }
code { font-family: ui-monospace, monospace; background: #eef1f5; padding: 1px 4px;
  border-radius: 4px; }
.metrics { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 12px; font-size: 13px; }
.metric { background: #eef4ff; border: 1px solid #c4d7f5; border-radius: 6px;
  padding: 6px 12px; }
.panel { margin-top: 16px; padding: 12px; border: 1px dashed #c4cbd6; border-radius: 8px; }
.panel label { display: block; font-size: 12px; text-transform: uppercase;
  letter-spacing: .06em; color: #5a6473; margin-bottom: 4px; }
.panel textarea { width: 100%; min-height: 60px; font: inherit; padding: 8px;
  border: 1px solid #c4cbd6; border-radius: 6px; }
details summary { cursor: pointer; margin-top: 14px; color: #12203a; font-size: 13px; }
"""

_SYNC = """
<script>
(function () {
  const store = {};
  const post = () => fetch('/api/reviews', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(store)
  });
  fetch('/api/reviews').then(r => r.json()).then(saved => {
    Object.assign(store, saved || {});
    document.querySelectorAll('[data-review]').forEach(node => {
      const entry = store[node.dataset.review];
      if (entry) {
        node.querySelector('.score').value = entry.score ?? '';
        node.querySelector('.comment').value = entry.comment ?? '';
      }
    });
  });
  document.addEventListener('change', event => {
    const panel = event.target.closest('[data-review]');
    if (!panel) return;
    store[panel.dataset.review] = {
      score: panel.querySelector('.score').value,
      comment: panel.querySelector('.comment').value
    };
    post();
  });
})();
</script>
"""


def _render_rubric(title: str, rubric: dict[str, Any]) -> str:
    sections = []
    for key in ("must", "must_not", "ignore"):
        items = rubric.get(key) or []
        if not items:
            continue
        entries = "".join(f"<li>{esc(item)}</li>" for item in items)
        sections.append(f"<h4>{esc(key)}</h4><ul>{entries}</ul>")
    return f'<div class="rubric"><h4>{esc(title)}</h4>{"".join(sections)}</div>'


_AFTER_SECTIONS = (
    ("must", "Both after frames must show", ""),
    ("decide_by", "Decide by", ""),
    ("correct_if", "Correct: the build has no defect", "verdict correct"),
    ("must_not", "Frame is invalid if", ""),
    ("ignore", "Ignore", ""),
)


def _render_after_rubric(rubric: dict[str, list[str]]) -> str:
    sections = []
    for key, label, css_class in _AFTER_SECTIONS:
        items = rubric.get(key) or []
        if not items:
            continue
        entries = "".join(f"<li>{esc(item)}</li>" for item in items)
        opening = f'<div class="{css_class}">' if css_class else "<div>"
        sections.append(f"{opening}<h4>{esc(label)}</h4><ul>{entries}</ul></div>")
    return (
        '<div class="rubric after-rubric"><h4>After rubric &mdash; is this build '
        f'buggy or correct?</h4>{"".join(sections)}</div>'
    )


def _render_shot(task_id: str, role: str, *, static_export: bool) -> str:
    source = (
        f"{task_id}-{role}.png" if static_export else f"/img/{task_id}/{role}.png"
    )
    return (
        f'<figure class="shot"><figcaption>{esc(ROLE_CAPTIONS[role])}</figcaption>'
        f'<img src="{esc(source)}" alt="{esc(role)} frame for {esc(task_id)}"></figure>'
    )


def _render_gate_table(status: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td><code>{esc(name)}</code></td><td>{esc(value)}</td></tr>"
        for name, value in status.items()
    )
    return (
        '<table class="gate"><tr><th>Gate 1--6 field</th><th>Recorded value</th></tr>'
        f"{rows}</table>"
    )


def _render_task(task: dict[str, Any], *, static_export: bool) -> str:
    task_id = task["task_id"]
    starts = (
        "The two start frames are byte-identical."
        if task["identical_starts"]
        else "The two start frames differ only within the recorded tolerance."
    )
    return f"""
<section class="card">
  <h2>{task['index']:02d} &middot; {esc(task_id)}</h2>
  <div class="meta">
    <span>benchmark: {esc(task['benchmark'])}</span>
    <span>locus: {esc(task['defect_locus'])}</span>
    <span>horizon: {esc(task['horizon_class'])}</span>
    <span><a href="{esc(task['upstream_pr'])}">upstream PR</a></span>
    <span><a href="{esc(task['upstream_issue'])}">authoritative issue</a></span>
  </div>

  <div class="metrics">
    <span class="metric">before-equivalence
      {task['before_ratio']:.6f}</span>
    <span class="metric">after-differential
      {task['after_ratio']:.6f}</span>
    <span class="metric">control cells {esc(task['control_cells'])} (all pass)</span>
    <span class="metric">differential source:
      {esc(task['defect_differential_source'])}</span>
  </div>

  <div class="shot-grid">
    {_render_shot(task_id, 'before', static_export=static_export)}
    {_render_shot(task_id, 'buggy-before', static_export=static_export)}
    {_render_shot(task_id, 'fixed-after', static_export=static_export)}
    {_render_shot(task_id, 'buggy-after', static_export=static_export)}
  </div>
  <p class="meta">{esc(starts)}</p>

  <section class="prompt-grid">
    <div><h4>Preconditions</h4><pre>{esc(task['preconditions'])}</pre></div>
    <div><h4>Reference steps</h4><pre>{esc(task['reference_steps'])}</pre></div>
    <div class="full"><h4>State description (what the model receives)</h4>
      <pre>{esc(task['state_description'])}</pre></div>
  </section>

  <div class="rubric-grid">
    {_render_rubric('Start frame rubric', task['visual_rubric'])}
    {_render_after_rubric(task['after_rubric'])}
  </div>

  <div class="panel" data-review="{esc(task_id)}">
    <label for="score-{esc(task_id)}">Your rating (0 = unusable, 1 = usable)</label>
    <input class="score" id="score-{esc(task_id)}" type="number" min="0" max="1"
      step="0.1" style="width:90px">
    <label for="comment-{esc(task_id)}" style="margin-top:8px">Comment</label>
    <textarea class="comment" id="comment-{esc(task_id)}"></textarea>
  </div>

  <details>
    <summary>Why this row carries no fail-to-pass test, and what the control proves</summary>
    <p><strong>Gate 5 declaration.</strong> {esc(task['f2p_absence'])}</p>
    <p><strong>Gate 6 control.</strong> <code>{esc(task['control_test_path'])}</code>
      ({esc(task['control_test_relation'])}) ran three times on each product and
      passed every time. That shows both products build and run; it does not show
      either is wrong.</p>
    <p><strong>Reverted production material.</strong>
      {"".join(f"<code>{esc(path)}</code> " for path in task['production_paths'])}</p>
    <p><strong>Oracle spec.</strong> <code>{esc(task['oracle_spec'])}</code></p>
    {_render_gate_table(task['gate_1_6_status'])}
  </details>
</section>
"""


def render_page(tasks: list[dict[str, Any]], *, static_export: bool = False) -> str:
    cards = "".join(_render_task(task, static_export=static_export) for task in tasks)
    sync = "" if static_export else _SYNC
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C1-Lazarus P2P-only catalog review</title>
<style>{_STYLE}</style></head>
<body>
<header class="hero">
  <h1>C1-Lazarus P2P-only catalog review</h1>
  <p>{len(tasks)} tasks. Each is a merged upstream repair for one authoritative
     bug that shipped no test of its own.</p>
  <p>Judge each row on one question: applying the single after rubric to each after
     frame, does it call one build correct and the other buggy, from starts that
     look the same?</p>
</header>
<div class="banner">
  <strong>These rows carry no fail-to-pass evidence.</strong>
  Their defect differential comes from the published frames, not from a test. The
  six control cells per row pass on both products by design. Nothing here may be
  reported as an upstream fail-to-pass result, and this population must not be
  aggregated with the <code>lz-</code> catalog.
</div>
<main>{cards}</main>
{sync}
</body></html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    tasks: list[dict[str, Any]] = []
    images: dict[str, Path] = {}
    review_path: Path = DEFAULT_REVIEW_PATH

    def log_message(self, *args: Any) -> None:  # noqa: A003 - quiet by design
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0].lstrip("/")
        if path in {"", "index.html"}:
            self._send(
                200,
                render_page(self.tasks).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if path == "api/reviews":
            saved = (
                self.review_path.read_bytes()
                if self.review_path.is_file()
                else b"{}"
            )
            self._send(200, saved, "application/json; charset=utf-8")
            return
        image = self.images.get(path)
        if image is not None:
            self._send(200, image.read_bytes(), "image/png")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path.split("?", 1)[0] != "/api/reviews":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, b"invalid json", "text/plain; charset=utf-8")
            return
        if not isinstance(payload, dict):
            self._send(400, b"reviews must be an object", "text/plain; charset=utf-8")
            return
        known = {task["task_id"] for task in self.tasks}
        unknown = sorted(set(payload) - known)
        if unknown:
            self._send(
                400,
                f"unknown task ids: {unknown}".encode(),
                "text/plain; charset=utf-8",
            )
            return
        self.review_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.review_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.review_path)
        self._send(200, b'{"saved":true}', "application/json; charset=utf-8")


def export(tasks: list[dict[str, Any]], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": "c1-lazarus-p2p-only-review-export-v1",
        "task_count": len(tasks),
        "assets": [],
    }
    for task in tasks:
        for role, path in task["frames"].items():
            name = f"{task['task_id']}-{role}.png"
            shutil.copy2(path, destination / name)
            manifest["assets"].append(
                {
                    "asset": name,
                    "task_id": task["task_id"],
                    "role": role,
                    "source": str(path.relative_to(REPOSITORY_ROOT)),
                }
            )
    manifest["screenshot_count"] = len(manifest["assets"])
    (destination / "index.html").write_text(
        render_page(tasks, static_export=True), encoding="utf-8"
    )
    (destination / "asset-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument(
        "--export",
        nargs="?",
        const=DEFAULT_EXPORT_DIR,
        type=Path,
        help="write a standalone page and its images instead of serving",
    )
    args = parser.parse_args()

    tasks, images = load_review_tasks()
    print(f"loaded {len(tasks)} P2P-only tasks and {len(images)} frames")

    if args.export is not None:
        export(tasks, args.export)
        print(f"exported to {args.export}")
        return 0

    ReviewHandler.tasks = tasks
    ReviewHandler.images = images
    ReviewHandler.review_path = args.reviews
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"serving {url} (reviews saved to {args.reviews})")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
