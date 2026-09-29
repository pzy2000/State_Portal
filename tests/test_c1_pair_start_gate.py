from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from warptest.state_loop import (
    _freeze_pair_start_semantic_failure,
    _run_state_start_visual_gate,
    _stable_pair_start_id,
    _state_trial_retryable_incomplete,
    load_state_loop_tasks,
)
from warptest.synthesis import synthesize_spec_from_public_issue


ROOT = Path(__file__).resolve().parents[1]


def test_pair_start_id_is_plain_run_scoped_identity() -> None:
    value = _stable_pair_start_id(
        "p5-run", "gpt-5.6-terra", 0, "2048-merge-left"
    )
    assert value == "p5-run:gpt-5.6-terra:0:2048-merge-left"


def task_and_spec():
    task = next(
        task
        for task in load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
        if task.id == "2048-merge-left"
    )
    return task, synthesize_spec_from_public_issue(task.issue)


def informative(path: Path, offset: int) -> None:
    image = Image.new("RGB", (128, 128))
    pixels = image.load()
    for y in range(128):
        for x in range(128):
            pixels[x, y] = (
                (x * 7 + offset) % 256,
                (y * 11 + offset) % 256,
                (x * y + offset) % 256,
            )
    image.save(path)


def result(cache: Path, *, semantic: bool = True) -> dict:
    return {
        "task_id": "2048-merge-left",
        "benchmark": "2048",
        "seed": 0,
        "generated_spec_hash": "generated",
        "synthesis_cache_fingerprint": "synthesis",
        "before_semantic_ok": semantic,
        "before_semantic_status": "success" if semantic else "failure",
        "grader_ms": 0,
        "_pair_start_cache": str(cache),
    }


def verdict(label: str) -> dict:
    return {
        "result": label == "correct",
        "status": "success" if label == "correct" else "failure",
        "before": {
            "stage": "before",
            "label": label,
            "confidence": 1.0,
            "verdict_source": "fake_pair_judge",
            "judge_model": "fake",
            "rubric_failures": [] if label == "correct" else ["wrong start"],
        },
        "after": {
            "stage": "after",
            "label": "not_evaluated",
            "evaluation_status": "pending_phase_b",
        },
        "after_evaluated": False,
    }


def test_pair_start_gate_is_written_once_and_reused_without_second_judge(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    cache = tmp_path / "pair-cache"
    golden_before = tmp_path / "golden-before.png"
    golden_after = tmp_path / "golden-after.png"
    live_e = tmp_path / "e-before.png"
    live_ep = tmp_path / "ep-before.png"
    for index, path in enumerate(
        (golden_before, golden_after, live_e, live_ep)
    ):
        informative(path, index)
    goldens = SimpleNamespace(
        before=str(golden_before),
        after=str(golden_after),
        after_description="goal",
        after_rubric={"must": [], "must_not": [], "ignore": []},
    )
    first = result(cache)
    with patch(
        "warptest.state_loop.run_visual_pair_audit",
        return_value=verdict("correct"),
    ) as judge:
        first_ok, first_verdict = _run_state_start_visual_gate(
            first,
            live_before=str(live_e),
            goldens=goldens,
            task=task,
            accepted_spec=spec,
        )
    assert first_ok is True
    assert first_verdict["before"]["label"] == "correct"
    assert judge.call_count == 1
    pair_files = list(cache.glob("*/*.json"))
    assert len(pair_files) == 1
    pair_payload = json.loads(pair_files[0].read_text())
    assert pair_payload["written_before_transition"] is True

    second = result(cache)
    with patch(
        "warptest.state_loop.run_visual_pair_audit",
        side_effect=AssertionError("pair cache hit must not call the judge"),
    ):
        second_ok, second_verdict = _run_state_start_visual_gate(
            second,
            live_before=str(live_ep),
            goldens=goldens,
            task=task,
            accepted_spec=spec,
        )
    assert second_ok is True
    assert second_verdict == first_verdict
    assert first["pair_start_fingerprint"] == second["pair_start_fingerprint"]
    assert first["pair_start_gate_cache_hit"] is False
    assert second["pair_start_gate_cache_hit"] is True
    assert second["local_start_attestation"]["screenshot_informative"] is True


def test_pair_start_local_semantic_drift_is_retryable_infrastructure(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    cache = tmp_path / "pair-cache"
    golden_before = tmp_path / "golden-before.png"
    golden_after = tmp_path / "golden-after.png"
    live = tmp_path / "before.png"
    for index, path in enumerate((golden_before, golden_after, live)):
        informative(path, index)
    goldens = SimpleNamespace(
        before=str(golden_before),
        after=str(golden_after),
        after_description="goal",
        after_rubric={"must": [], "must_not": [], "ignore": []},
    )
    with patch(
        "warptest.state_loop.run_visual_pair_audit",
        return_value=verdict("correct"),
    ):
        assert _run_state_start_visual_gate(
            result(cache),
            live_before=str(live),
            goldens=goldens,
            task=task,
            accepted_spec=spec,
        )[0]

    drifted = result(cache, semantic=False)
    ok, _ = _run_state_start_visual_gate(
        drifted,
        live_before=str(live),
        goldens=goldens,
        task=task,
        accepted_spec=spec,
    )
    assert ok is False
    assert drifted["incomplete"] is True
    assert drifted["infrastructure_error"] is True
    assert drifted["start_gate_status"] == "pair_start_attestation_incomplete"
    assert _state_trial_retryable_incomplete(drifted) is True


def test_pair_semantic_failure_is_shared_without_visual_judge(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    cache = tmp_path / "pair-cache"
    first = result(cache, semantic=False)
    agent = SimpleNamespace(current_cache_fingerprint="synthesis")
    _freeze_pair_start_semantic_failure(
        first,
        task=task,
        accepted_spec=spec,
        synthesis_agent=agent,
    )
    assert first["start_success"] is False
    assert first["start_gate_status"] == "semantic_failed"
    assert first["visual_grading_started"] is False
    assert first["pair_start_gate_cache_hit"] is False

    second = result(cache, semantic=False)
    _freeze_pair_start_semantic_failure(
        second,
        task=task,
        accepted_spec=spec,
        synthesis_agent=agent,
    )
    assert second["pair_start_fingerprint"] == first["pair_start_fingerprint"]
    assert second["pair_start_gate_cache_hit"] is True
    assert second["pair_verdict"] == first["pair_verdict"]
