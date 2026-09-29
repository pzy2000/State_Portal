"""Cover the P2P-only after-rubric audit harness without a paid request.

The harness measures whether one merged after rubric can classify both of a
task's after frames.  These tests pin the two parts that decide what that
measurement means: the prompt that tells a judge how to read the merged rubric,
and the scoring that turns verdicts into an accuracy.  Both run against the
fake judge, so the audit stays verifiable in CI.
"""

from __future__ import annotations

import json

import pytest

from scripts import audit_lazarus_p2p_only_after_rubrics as audit
from scripts.serve_c1_lazarus_p2p_only_review import merge_after_rubrics
from warptest.c1_lazarus_selection.p2p_only_task_text import TASK_TEXT

RUBRIC = {
    "must": ["A client window shows the engine main menu."],
    "decide_by": ["Judge this frame by whether the list draws a worldmods group."],
    "correct_if": ["The list includes a worldmods group."],
    "must_not": ["A blank page or an error overlay is shown."],
    "ignore": ["Window chrome and font antialiasing."],
}


def _task(task_id: str = "task-a", **overrides) -> audit.AuditTask:
    defaults = dict(
        task_id=task_id,
        benchmark="luanti",
        rubric=dict(RUBRIC),
        start_frame=audit.Path("before.png"),
        graded_frames={
            "fixed-after": audit.Path("fixed.png"),
            "buggy-after": audit.Path("buggy.png"),
        },
        before_changed_pixel_ratio=0.0,
        after_changed_pixel_ratio=0.02,
        identical_starts=True,
    )
    defaults.update(overrides)
    return audit.AuditTask(**defaults)


@pytest.fixture
def frames(tmp_path):
    """Three readable frames, since the audit measures what it sends."""

    from PIL import Image

    written = {}
    for name, shade in (("before", 16), ("fixed", 64), ("buggy", 200)):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (32, 24), (shade, shade, shade)).save(path)
        written[name] = path
    return written


def _trial(task_id: str, frame: str, expected: str, label: str) -> audit.Trial:
    return audit.Trial(
        task_id=task_id,
        benchmark="luanti",
        frame=frame,
        expected_label=expected,
        label=label,
        correct=label == expected,
    )


def test_probe_prompt_carries_every_part_of_the_merged_rubric() -> None:
    payload = json.loads(
        audit.build_after_rubric_probe_prompt(rubric=RUBRIC, benchmark="luanti")
    )

    assert payload["rubric"] == RUBRIC
    assert payload["benchmark"] == "luanti"
    assert payload["output_contract"]["label"] == "correct|incorrect|uncertain"
    # The scored decision is whether correct_if holds, so the procedure has to
    # name correct_if rather than leave "same state as the golden" implied.
    procedure = " ".join(payload["decision_procedure"])
    assert "rubric.correct_if" in procedure
    assert "rubric.decide_by" in procedure


def test_probe_prompt_denies_the_start_frame_any_authority() -> None:
    """The transport labels image 1 a golden, and it is not one here.

    A judge that reads the registered start as a target scores "changed since
    the start", which both products satisfy, so the prompt has to say outright
    that image 1 settles nothing.
    """

    payload = json.loads(
        audit.build_after_rubric_probe_prompt(rubric=RUBRIC, benchmark="luanti")
    )
    start, graded = payload["images"]

    assert start["position"] == 1 and graded["position"] == 2
    assert "no information about which build" in start["note"]
    assert "never a target to match" in start["note"]
    procedure = " ".join(payload["decision_procedure"])
    assert "Do not credit image 2 for changing relative to image 1" in procedure


def test_a_task_counts_as_separated_only_when_both_frames_land() -> None:
    tasks = [_task("task-a"), _task("task-b")]
    trials = [
        _trial("task-a", "fixed-after", "correct", "correct"),
        _trial("task-a", "buggy-after", "incorrect", "incorrect"),
        # One right frame is reachable by answering the same way every time.
        _trial("task-b", "fixed-after", "correct", "correct"),
        _trial("task-b", "buggy-after", "incorrect", "correct"),
    ]

    summary = audit.summarize_model(model="m", tasks=tasks, trials=trials)

    assert summary["correct"] == 3
    assert summary["trials"] == 4
    assert summary["accuracy"] == pytest.approx(0.75)
    assert summary["separated_tasks"] == 1
    assert [row["separated"] for row in summary["task_table"]] == [True, False]
    assert [failure["task_id"] for failure in summary["failures"]] == ["task-b"]


def test_uncertain_and_judge_errors_both_count_against_the_rubric() -> None:
    """A judge that will not decide has not screened the defect out."""

    task = _task()
    unsure = _trial(task.task_id, "fixed-after", "correct", "uncertain")
    broken = audit.Trial(
        task_id=task.task_id,
        benchmark="luanti",
        frame="buggy-after",
        expected_label="incorrect",
        error="VisualOracleError: judge failed",
    )

    summary = audit.summarize_model(model="m", tasks=[task], trials=[unsure, broken])

    assert summary["accuracy"] == 0.0
    assert summary["separated_tasks"] == 0
    assert summary["errors"] == 1
    assert summary["label_counts"]["uncertain"] == 1


def test_fake_judge_scores_a_constant_answer_at_one_frame_in_two(
    monkeypatch: pytest.MonkeyPatch, frames
) -> None:
    """The protocol denies a fixed answer any better than chance.

    Both frames of a task are graded against the same text, so a judge that
    always answers ``correct`` earns the fixed frame and loses the buggy one.
    """

    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_FAKE", "correct")
    graded = {
        "fixed-after": frames["fixed"],
        "buggy-after": frames["buggy"],
    }
    tasks = [
        _task(task_id, start_frame=frames["before"], graded_frames=graded)
        for task_id in ("task-a", "task-b")
    ]

    summary = audit.audit_model(model="fake", tasks=tasks, workers=1)

    assert summary["trials"] == 4
    assert summary["accuracy"] == pytest.approx(0.5)
    assert summary["separated_tasks"] == 0
    assert {
        (record["task_id"], record["frame"]) for record in summary["trial_records"]
    } == {
        ("task-a", "fixed-after"),
        ("task-a", "buggy-after"),
        ("task-b", "fixed-after"),
        ("task-b", "buggy-after"),
    }


def test_published_rubrics_all_merge_and_name_a_decidable_readout() -> None:
    """Every catalog row must reach the audit with a usable merged rubric.

    ``merge_after_rubrics`` is what refuses a task whose two after rubrics have
    stopped separating the products, and ``load_audit_tasks`` runs it, so a
    registry edit that collapses the two branches fails here.
    """

    tasks = audit.load_audit_tasks()

    assert len(tasks) == 11
    for task in tasks:
        assert task.rubric["decide_by"], task.task_id
        assert task.rubric["correct_if"], task.task_id
        assert task.rubric["decide_by"] == [TASK_TEXT[task.task_id].readout]
        assert task.graded_frames["fixed-after"].is_file()
        assert task.graded_frames["buggy-after"].is_file()
        assert task.start_frame.is_file()


def test_merged_rubric_keeps_the_buggy_description_off_the_prompt() -> None:
    """A frame is judged against the repaired product, not one known defect.

    The buggy branch's own description must not reach the judge, or a rubric
    could be satisfied by recognising one particular way of getting it wrong.
    """

    for task in audit.load_audit_tasks():
        text = TASK_TEXT[task.task_id]
        payload = json.loads(
            audit.build_after_rubric_probe_prompt(
                rubric=task.rubric, benchmark=task.benchmark
            )
        )
        carried = [
            line for lines in payload["rubric"].values() for line in lines
        ]
        assert text.fixed_after in carried, task.task_id
        assert text.buggy_after not in carried, task.task_id


def test_merge_refuses_two_rubrics_that_no_longer_separate() -> None:
    task_id = "lzp-luanti-16799-world-mods"
    shared = {
        "must": [
            "A Luanti client window shows the engine main menu.",
            "The configure-world dialog is open.",
            TASK_TEXT[task_id].readout,
        ],
        "must_not": [],
        "ignore": [],
    }

    with pytest.raises(ValueError, match="do not separate the products"):
        merge_after_rubrics(task_id, shared, dict(shared))
