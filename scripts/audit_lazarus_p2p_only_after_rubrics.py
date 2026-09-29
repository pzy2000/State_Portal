#!/usr/bin/env python3
"""Ask a VLM whether each P2P-only after rubric separates the two after frames.

This track has no fail-to-pass test, so the published frames are the whole
defect oracle and the after rubric is the only instrument that reads them.  A
rubric that a judge cannot apply is therefore not a wording problem, it is a
missing oracle.  This script measures that directly on the goldens, before any
live trial spends a build.

The protocol is the one the human review page uses.
:func:`scripts.serve_c1_lazarus_p2p_only_review.merge_after_rubrics` folds a
task's two branch rubrics into a single rubric whose ``correct_if`` is the fixed
branch's frame description alone.  That one rubric is applied to one after frame
at a time: the fixed frame must be judged ``correct`` and the buggy frame must be
judged ``incorrect``.  Both frames of a task are graded against the same text, so
neither verdict can be reached by recognising which of two rubrics it was handed,
and no request compares an image with itself.

Accuracy is over those 2 x N trials.  ``uncertain`` counts against the rubric:
the judge that cannot decide has not screened the defect out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.serve_c1_lazarus_p2p_only_review import load_review_tasks  # noqa: E402
from warptest import cost  # noqa: E402
from warptest.c1_experiment import (  # noqa: E402
    DEFAULT_CREDENTIAL_SOURCE,
    relay_environment,
)
from warptest.visual import (  # noqa: E402
    VisualJudgeClient,
    visual_judge_image_metadata,
)

DEFAULT_MODELS: tuple[str, ...] = (
    "qwen3-vl-flash@relay",
    "gemini-3.5-flash@relay",
)
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "reports/lazarus/p2p_only_after_rubric_audit"
DEFAULT_TARGET = 0.80
DEFAULT_WORKERS = 4

# The judge re-encodes any frame over its byte ceiling as 4:2:0 JPEG.  At the
# 512 KiB default that fires on lzp-openra-22476's buggy after frame (553 KiB)
# and not on its fixed one (449 KiB), which would grade the two branches through
# different image pipelines.  A 1 MiB ceiling keeps every published after frame
# on the lossless path, so a miss is attributable to the rubric.
JUDGE_MAX_IMAGE_BYTES = 1024 * 1024

# The rubric is applied to one after frame at a time, and the frame's own branch
# fixes the verdict the rubric has to reach on it.
GRADED_FRAMES: tuple[tuple[str, str], ...] = (
    ("fixed-after", "correct"),
    ("buggy-after", "incorrect"),
)


@dataclass(frozen=True)
class AuditTask:
    """One task's merged rubric and the three frames a trial needs."""

    task_id: str
    benchmark: str
    rubric: dict[str, list[str]]
    start_frame: Path
    graded_frames: dict[str, Path]
    before_changed_pixel_ratio: float
    after_changed_pixel_ratio: float
    identical_starts: bool


@dataclass
class Trial:
    """One judged frame, with everything needed to explain the verdict."""

    task_id: str
    benchmark: str
    frame: str
    expected_label: str
    label: str | None = None
    correct: bool = False
    confidence: float | None = None
    observed: str = ""
    rationale: str = ""
    rubric_failures: list[str] = field(default_factory=list)
    judge_model: str | None = None
    latency_ms: int | None = None
    attempts: int | None = None
    error: str | None = None
    start_image: dict[str, Any] = field(default_factory=dict)
    graded_image: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "benchmark": self.benchmark,
            "frame": self.frame,
            "expected_label": self.expected_label,
            "label": self.label,
            "correct": self.correct,
            "confidence": self.confidence,
            "observed": self.observed,
            "rationale": self.rationale,
            "rubric_failures": list(self.rubric_failures),
            "judge_model": self.judge_model,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "error": self.error,
            "start_image": dict(self.start_image),
            "graded_image": dict(self.graded_image),
        }


def build_after_rubric_probe_prompt(
    *, rubric: dict[str, list[str]], benchmark: str
) -> str:
    """Prompt for applying one merged after rubric to one after frame.

    The judge is handed the registered start frame first and the frame under
    judgement second.  The start frame is published once per task and both
    products reproduce it, so it says what the reference steps changed without
    saying which product ran them.  The prompt has to say so outright: the
    transport labels its first image as a golden oracle, and a judge that reads
    it as a target would score "differs from the start" instead of the condition
    the rubric actually states.
    """

    must = list(rubric.get("must") or [])
    decide_by = list(rubric.get("decide_by") or [])
    correct_if = list(rubric.get("correct_if") or [])
    must_not = list(rubric.get("must_not") or [])
    ignore = list(rubric.get("ignore") or [])
    payload = {
        "task": (
            "Two builds of the same application were driven through identical "
            "reference steps from an identical start. One build renders the "
            "end state correctly and the other carries a rendering defect. "
            "Decide which of the two produced the second screenshot."
        ),
        "benchmark": benchmark,
        "images": [
            {
                "position": 1,
                "role": "registered start frame",
                "note": (
                    "The published start state, before the reference steps ran. "
                    "Both builds reproduce this frame identically, so it carries "
                    "no information about which build produced image 2. It is "
                    "context for what the reference steps changed, never a target "
                    "to match, and differing from it is not evidence of anything."
                ),
            },
            {
                "position": 2,
                "role": "frame under judgement",
                "note": (
                    "The end state after the reference steps ran on one of the "
                    "two builds. This is the only frame you are grading."
                ),
            },
        ],
        "rubric": {
            "must": must,
            "decide_by": decide_by,
            "correct_if": correct_if,
            "must_not": must_not,
            "ignore": ignore,
        },
        "decision_procedure": [
            "Locate in image 2 the part of the frame named by rubric.decide_by.",
            (
                "Read that part and compare what it shows against every line of "
                "rubric.correct_if."
            ),
            (
                "Return label=correct when every rubric.must line holds in image 2 "
                "and every rubric.correct_if line holds at that part."
            ),
            (
                "Return label=incorrect when any rubric.correct_if line does not "
                "hold there, or any rubric.must line is unmet, or any "
                "rubric.must_not line is visible. That is the defective build."
            ),
            (
                "rubric.correct_if is the entire difference between the two builds. "
                "Do not credit image 2 for changing relative to image 1, for looking "
                "plausible, or for showing no error."
            ),
            "Treat every line of rubric.ignore as carrying no information.",
        ],
        "output_contract": {
            "label": "correct|incorrect|uncertain",
            "confidence": "number from 0.0 to 1.0",
            "observed": (
                "non-empty short description of what image 2 actually shows at the "
                "part named by rubric.decide_by"
            ),
            "rationale": (
                "non-empty short reason tying that observation to a rubric line"
            ),
            "rubric_failures": (
                "non-empty list of the unmet correct_if/must/must_not lines when "
                "label=incorrect; empty list when label=correct"
            ),
        },
        "instructions": [
            (
                "You receive exactly two images: first the registered start frame, "
                "then the frame under judgement."
            ),
            "Base every observation only on visible pixels in the two images.",
            (
                "Do not invent dialogs, logs, values or screens that are not drawn, "
                "and do not reason about source code or version numbers."
            ),
            (
                "Label uncertain only when the part named by rubric.decide_by cannot "
                "be resolved in image 2 at all."
            ),
            (
                "observed and rationale must both be non-empty and must support the "
                "returned label. Do not state a different final label inside either "
                "field."
            ),
            "Return strict JSON only. Do not wrap it in Markdown.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def load_audit_tasks(task_ids: Sequence[str] | None = None) -> list[AuditTask]:
    """Load the catalog rows and their merged after rubrics.

    ``load_review_tasks`` is the review page's loader.  It revalidates the
    catalog against the Lazarus contract, resolves all four frames, and folds the
    two branch rubrics through ``merge_after_rubrics``, which refuses a task
    whose rubrics have stopped separating the products.  Reusing it keeps this
    audit and the human page on one definition of the rubric under test.
    """

    rows, _ = load_review_tasks()
    selected = set(task_ids) if task_ids else None
    tasks: list[AuditTask] = []
    for row in rows:
        task_id = str(row["task_id"])
        if selected is not None and task_id not in selected:
            continue
        frames = row["frames"]
        tasks.append(
            AuditTask(
                task_id=task_id,
                benchmark=str(row["benchmark"]),
                rubric=row["after_rubric"],
                start_frame=Path(frames["before"]),
                graded_frames={
                    "fixed-after": Path(frames["fixed-after"]),
                    "buggy-after": Path(frames["buggy-after"]),
                },
                before_changed_pixel_ratio=float(row["before_ratio"]),
                after_changed_pixel_ratio=float(row["after_ratio"]),
                identical_starts=bool(row["identical_starts"]),
            )
        )
    if selected is not None:
        unknown = sorted(selected - {task.task_id for task in tasks})
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
    return tasks


@contextmanager
def _judge_environment(model: str) -> Iterator[None]:
    """Bind the shared visual judge to one model and one image ceiling."""

    overrides = {
        "WARPTEST_VISUAL_JUDGE_MODEL": model,
        "WARPTEST_VISUAL_JUDGE_MAX_IMAGE_BYTES": str(JUDGE_MAX_IMAGE_BYTES),
    }
    previous = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _build_judge() -> Any:
    """Resolve the judge from the environment, then keep it in this process.

    ``VisualJudgeClient.from_env`` owns the model/provider/key resolution, so it
    stays the single source of routing.  Its default transport spawns one child
    process per request, which is isolation this audit does not need and cannot
    afford 44 times per iteration, so the resolved identity is rebuilt around an
    explicit client.  ``FakeVisualJudgeClient`` has no such identity and is
    returned untouched.
    """

    resolved = VisualJudgeClient.from_env()
    if not isinstance(resolved, VisualJudgeClient):
        return resolved
    from openai import OpenAI

    kwargs: dict[str, Any] = {
        "api_key": resolved.api_key,
        "timeout": resolved.timeout_s,
        "max_retries": resolved.max_retries,
    }
    if resolved.base_url:
        kwargs["base_url"] = resolved.base_url
    if resolved.extra_headers:
        kwargs["default_headers"] = dict(resolved.extra_headers)
    return VisualJudgeClient(
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        model=resolved.model,
        extra_headers=resolved.extra_headers,
        client=OpenAI(**kwargs),
    )


def _run_trial(
    *, judge: Any, task: AuditTask, frame: str, expected_label: str, meter: Any
) -> Trial:
    graded = task.graded_frames[frame]
    trial = Trial(
        task_id=task.task_id,
        benchmark=task.benchmark,
        frame=frame,
        expected_label=expected_label,
    )
    prompt = build_after_rubric_probe_prompt(
        rubric=task.rubric, benchmark=task.benchmark
    )
    try:
        trial.start_image = visual_judge_image_metadata(task.start_frame)
        trial.graded_image = visual_judge_image_metadata(graded)
        # The meter is a contextvar, so a worker thread starts without it.
        with cost.scoped_meter(meter):
            verdict = judge.judge(
                prompt=prompt, live_screenshot=graded, golden_image=task.start_frame
            )
    except Exception as error:  # a judge that cannot answer has not screened
        trial.error = f"{type(error).__name__}: {error}"
        return trial
    trial.label = str(verdict["label"])
    trial.correct = trial.label == expected_label
    trial.confidence = verdict.get("confidence")
    trial.observed = str(verdict.get("observed", ""))
    trial.rationale = str(verdict.get("rationale", ""))
    trial.rubric_failures = list(verdict.get("rubric_failures") or [])
    trial.judge_model = verdict.get("judge_model")
    trial.latency_ms = verdict.get("latency_ms")
    trial.attempts = verdict.get("attempts")
    return trial


def audit_model(
    *, model: str, tasks: Sequence[AuditTask], workers: int = DEFAULT_WORKERS
) -> dict[str, Any]:
    """Grade every task's two after frames with one model."""

    meter = cost.CostMeter()
    with _judge_environment(model):
        judge = _build_judge()
        plan = [
            (task, frame, expected)
            for task in tasks
            for frame, expected in GRADED_FRAMES
        ]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            trials = list(
                pool.map(
                    lambda item: _run_trial(
                        judge=judge,
                        task=item[0],
                        frame=item[1],
                        expected_label=item[2],
                        meter=meter,
                    ),
                    plan,
                )
            )
    return summarize_model(model=model, tasks=tasks, trials=trials, meter=meter)


def summarize_model(
    *,
    model: str,
    tasks: Sequence[AuditTask],
    trials: Sequence[Trial],
    meter: Any | None = None,
) -> dict[str, Any]:
    """Score the trials and lay out the per-task separation table."""

    by_task: dict[str, list[Trial]] = {}
    for trial in trials:
        by_task.setdefault(trial.task_id, []).append(trial)
    task_rows: list[dict[str, Any]] = []
    for task in tasks:
        own = by_task.get(task.task_id, [])
        labels = {trial.frame: trial.label for trial in own}
        task_rows.append(
            {
                "task_id": task.task_id,
                "benchmark": task.benchmark,
                # A task is only usable when the one rubric reaches both
                # verdicts; one right frame can be reached by a fixed answer.
                "separated": bool(own) and all(trial.correct for trial in own),
                "fixed_after_label": labels.get("fixed-after"),
                "buggy_after_label": labels.get("buggy-after"),
                "after_changed_pixel_ratio": task.after_changed_pixel_ratio,
                "before_changed_pixel_ratio": task.before_changed_pixel_ratio,
                "identical_starts": task.identical_starts,
            }
        )
    graded = len(trials)
    correct = sum(1 for trial in trials if trial.correct)
    summary: dict[str, Any] = {
        "model": model,
        "trials": graded,
        "correct": correct,
        "accuracy": (correct / graded) if graded else 0.0,
        "tasks": len(tasks),
        "separated_tasks": sum(1 for row in task_rows if row["separated"]),
        "label_counts": {
            label: sum(1 for trial in trials if trial.label == label)
            for label in ("correct", "incorrect", "uncertain")
        },
        "errors": sum(1 for trial in trials if trial.error),
        "failures": [
            {
                "task_id": trial.task_id,
                "frame": trial.frame,
                "expected_label": trial.expected_label,
                "label": trial.label,
                "confidence": trial.confidence,
                "observed": trial.observed,
                "rationale": trial.rationale,
                "rubric_failures": list(trial.rubric_failures),
                "error": trial.error,
            }
            for trial in trials
            if not trial.correct
        ],
        "task_table": task_rows,
        "trial_records": [trial.as_dict() for trial in trials],
    }
    if meter is not None and meter.records:
        summary["usage"] = meter.summary()
    return summary


def format_digest(summaries: Sequence[dict[str, Any]], *, target: float) -> str:
    """Render the per-model score and the failing frames as plain text."""

    lines: list[str] = []
    for summary in summaries:
        lines.append(
            f"{summary['model']}: {summary['correct']}/{summary['trials']} "
            f"({summary['accuracy']:.1%}) "
            f"separated {summary['separated_tasks']}/{summary['tasks']} tasks"
            + (f", {summary['errors']} judge error(s)" if summary["errors"] else "")
        )
        for failure in summary["failures"]:
            detail = failure["error"] or (
                f"said {failure['label']}, expected {failure['expected_label']}"
            )
            lines.append(f"  - {failure['task_id']} [{failure['frame']}] {detail}")
            if failure["observed"]:
                lines.append(f"      observed: {failure['observed']}")
            if failure["rubric_failures"]:
                lines.append(
                    "      named: " + "; ".join(failure["rubric_failures"])
                )
    best = max((summary["accuracy"] for summary in summaries), default=0.0)
    verdict = "MET" if best >= target else "NOT MET"
    lines.append(f"best accuracy {best:.1%} vs target {target:.0%}: {verdict}")
    return "\n".join(lines)


def write_report(
    summaries: Sequence[dict[str, Any]],
    *,
    output_dir: Path,
    target: float,
    tasks: Sequence[AuditTask],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    for summary in summaries:
        name = str(summary["model"]).replace("/", "_").replace("@", "-at-")
        (output_dir / f"{name}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    best = max((summary["accuracy"] for summary in summaries), default=0.0)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "protocol": "merged_after_rubric_single_frame_v1",
                "target": target,
                "target_met": best >= target,
                "best_accuracy": best,
                "task_ids": [task.task_id for task in tasks],
                "models": [
                    {
                        "model": summary["model"],
                        "accuracy": summary["accuracy"],
                        "correct": summary["correct"],
                        "trials": summary["trials"],
                        "separated_tasks": summary["separated_tasks"],
                        "errors": summary["errors"],
                    }
                    for summary in summaries
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="judge model spec; repeatable (default: both registered VLMs)",
    )
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="report directory (default: a timestamped directory under reports/)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=REPOSITORY_ROOT / DEFAULT_CREDENTIAL_SOURCE,
        help="literal source mapped onto the Relay environment",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    models = list(args.models or DEFAULT_MODELS)
    tasks = load_audit_tasks(args.task_ids)
    output_dir = args.output or (
        DEFAULT_OUTPUT_ROOT
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    print(
        f"[after-rubric-audit] {len(tasks)} task(s) x {len(GRADED_FRAMES)} frames "
        f"x {len(models)} model(s) = {len(tasks) * len(GRADED_FRAMES) * len(models)} "
        "judge request(s)",
        flush=True,
    )

    fake = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    credentials = (
        None
        if fake or os.environ.get("RELAY_API_KEY")
        else args.credentials
    )

    def _run() -> list[dict[str, Any]]:
        summaries = []
        for model in models:
            print(f"[after-rubric-audit] judging with {model}", flush=True)
            summaries.append(
                audit_model(model=model, tasks=tasks, workers=args.workers)
            )
        return summaries

    if credentials is None:
        summaries = _run()
    else:
        with relay_environment(credentials):
            summaries = _run()

    summary_path = write_report(
        summaries, output_dir=output_dir, target=args.target, tasks=tasks
    )
    print(format_digest(summaries, target=args.target), flush=True)
    print(f"[after-rubric-audit] wrote {summary_path}", flush=True)
    best = max((summary["accuracy"] for summary in summaries), default=0.0)
    return 0 if best >= args.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
