from __future__ import annotations

import random

from warptest import p5_stats
from warptest.p5_aggregate import lazarus_from_reports


MODEL = "gpt-5.6-terra@relay"


def _reports(task_count: int = 6) -> tuple[list[dict], dict[tuple[str, str], str]]:
    reports: list[dict] = []
    expected_sha: dict[tuple[str, str], str] = {}
    for index in range(task_count):
        task_id = f"lz-openra-task-{index:02d}"
        for version in ("buggy", "fixed"):
            sha = ("a" if version == "buggy" else "f") * 64
            expected_sha[(task_id, version)] = sha
            for arm in ("lazarus_state", "lazarus_agent_s"):
                state = arm == "lazarus_state"
                buggy = version == "buggy"
                start = True if state or not buggy else False
                reports.append(
                    {
                        "arm": arm,
                        "model": MODEL,
                        "seed": 0,
                        "task_id": task_id,
                        "version": version,
                        "defect_locus": "on_path",
                        "complete": True,
                        "provenance_valid": True,
                        "provenance_sha256": sha,
                        "per_trial_p2p_ok": True,
                        "start_success": start,
                        "goal_success": (not buggy),
                        "defect_exposed": bool(buggy and state),
                        "false_alarm": False,
                        "checkpoint_reuse_success": bool(state and not buggy),
                        "synthesis_cache_hit": bool(state and not buggy),
                        "actual_synthesis_calls": (
                            0 if state and not buggy else (1 if state else 2)
                        ),
                        "treatment_tokens": (
                            0 if state and not buggy else (40 if state else 100)
                        ),
                        "treatment_time_ms": (
                            10 if state and not buggy else (40 if state else 100)
                        ),
                    }
                )
    return reports, expected_sha


def test_lazarus_aggregate_pairs_versions_arms_and_preregistered_statistics() -> None:
    reports, expected_sha = _reports()
    task_ids = sorted({report["task_id"] for report in reports})

    result = lazarus_from_reports(
        reports,
        expected_task_ids=task_ids,
        expected_seeds=(0,),
        expected_model=MODEL,
        expected_provenance=expected_sha,
        formal=False,
        rng=random.Random(7),
        iters=500,
        ci=0.95,
    )

    assert result["complete"] is True
    assert result["expected_cells"] == 24
    assert result["observed_cells"] == 24
    assert result["metrics"]["defect_discrimination"]["lazarus_state"] == {
        "successes": 6,
        "n": 6,
        "rate": 1.0,
    }
    assert result["metrics"]["defect_discrimination"]["lazarus_agent_s"][
        "rate"
    ] == 0.0
    h1 = result["statistics"]["h1_discrimination_mcnemar"]
    assert (h1["b"], h1["c"], h1["discordant"]) == (6, 0, 6)
    assert h1["p_value"] == 0.03125
    h2 = result["statistics"]["h2_on_path_buggy_start_mcnemar"]
    assert (h2["b"], h2["c"]) == (6, 0)
    h2_version_gap = result["statistics"][
        "h2_on_path_agent_s_fixed_vs_buggy_start_mcnemar"
    ]
    assert (h2_version_gap["b"], h2_version_gap["c"]) == (6, 0)
    assert result["statistics"]["h2_intersection_union_p_value"] == 0.03125
    assert result["metrics"]["h2_start_success"]["lazarus_state"][
        "buggy"
    ] == 1.0
    assert result["metrics"]["h2_start_success"]["lazarus_agent_s"][
        "buggy"
    ] == 0.0
    h3 = result["metrics"]["h3_second_version"]
    assert h3["checkpoint_reuse_success"] == {"successes": 6, "n": 6}
    assert h3["state_zero_actual_synthesis_calls"] is True
    assert h3["tokens_state_minus_agent_s"]["mean_diff"] == -100.0
    raw = result["statistics"]["raw_p_values"]
    assert result["statistics"]["holm_adjusted_p_values"] == p5_stats.holm_adjust(raw)


def test_lazarus_aggregate_fails_closed_on_missing_cell_false_alarm_and_sha_drift() -> None:
    reports, expected_sha = _reports(task_count=2)
    task_ids = sorted({report["task_id"] for report in reports})
    common = {
        "expected_task_ids": task_ids,
        "expected_seeds": (0,),
        "expected_model": MODEL,
        "expected_provenance": expected_sha,
        "formal": False,
        "rng": random.Random(3),
        "iters": 50,
        "ci": 0.95,
    }

    missing = lazarus_from_reports(reports[:-1], **common)
    false_alarm_reports = [dict(report) for report in reports]
    fixed = next(
        report
        for report in false_alarm_reports
        if report["version"] == "fixed"
    )
    fixed["defect_exposed"] = True
    fixed["false_alarm"] = True
    false_alarm = lazarus_from_reports(false_alarm_reports, **common)
    drift_reports = [dict(report) for report in reports]
    drift_reports[0]["provenance_sha256"] = "0" * 64
    drift = lazarus_from_reports(drift_reports, **common)

    assert missing["complete"] is False
    assert any("missing cell" in issue for issue in missing["issues"])
    assert false_alarm["complete"] is False
    assert any("false alarm" in issue for issue in false_alarm["issues"])
    assert drift["complete"] is False
    assert any("provenance" in issue for issue in drift["issues"])


def test_lazarus_aggregate_holm_and_mcnemar_known_examples_are_deterministic() -> None:
    reports, expected_sha = _reports(task_count=6)
    result = lazarus_from_reports(
        reports,
        expected_task_ids=sorted({report["task_id"] for report in reports}),
        expected_seeds=(0,),
        expected_model=MODEL,
        expected_provenance=expected_sha,
        formal=False,
        rng=random.Random(11),
        iters=100,
        ci=0.95,
    )

    raw = result["statistics"]["raw_p_values"]
    adjusted = result["statistics"]["holm_adjusted_p_values"]
    assert raw["H1"] == 0.03125
    assert raw["H2"] == 0.03125
    assert raw["H3"] == 0.03125
    assert adjusted == {"H1": 0.09375, "H2": 0.09375, "H3": 0.09375}
