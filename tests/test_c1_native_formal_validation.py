from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from warptest import p5_aggregate
from warptest.c1_evaluation import formal_surface_contract
from warptest.c1_unity_runtime import _save_load_transition_expectation

ROOT = Path(__file__).resolve().parents[1]


def _policy_fields() -> dict[str, object]:
    return {
        "arm": "c1_agent_s",
        "model": "worker/model",
        "seed": 3,
        "policy_action_interface": "pixel_mouse_keyboard_only",
        "policy_received_semantic_probe": False,
        "policy_observation_source": "application_framebuffer_pixels",
        "scripted_transition_executed": False,
        "goal_evaluation_status": "evaluated",
    }


def _openra_detail(*, stage: str, ok: bool = True) -> dict[str, object]:
    expected = (
        [("TargetLines", "Manual"), ("IntroductionPromptVersion", 999)]
        if stage == "before"
        else [("TargetLines", "Automatic")]
    )
    checks = [
        {
            "section": "Game",
            "key": key,
            "expected": value,
            "actual": value,
            "ok": True,
        }
        for key, value in expected
    ]
    if not ok:
        checks[0]["actual"] = "tampered"
        checks[0]["ok"] = False
    mismatches = [
        {
            "section": check["section"],
            "key": check["key"],
            "expected": check["expected"],
            "actual": check["actual"],
        }
        for check in checks
        if check["ok"] is False
    ]
    return {
        "source": "openra_public_settings_yaml",
        "live": True,
        "process_alive": True,
        "settings_path": "/tmp/openra-support/settings.yaml",
        "checks": checks,
        "mismatches": mismatches,
    }


def _openra_snapshot(detail: dict[str, object]) -> dict[str, object]:
    fingerprint: dict[str, dict[str, object]] = {}
    for check in detail["checks"]:
        fingerprint.setdefault(check["section"], {})[check["key"]] = check["actual"]
    return {"semantic_fingerprint": fingerprint}


def test_openra_formal_detail_rejects_wrong_source_and_check_inconsistency() -> None:
    before_detail = _openra_detail(stage="before")
    after_detail = _openra_detail(stage="after")
    trial = {
        **_policy_fields(),
        "task_id": "openra-target-lines-automatic",
        "benchmark": "OpenRA",
        "semantic_probe_source": "openra_public_settings_yaml",
        "before_semantic_ok": True,
        "before_semantic_status": "success",
        "before_semantic_detail": before_detail,
        "after_semantic_ok": True,
        "after_semantic_status": "success",
        "after_semantic_detail": after_detail,
        "semantic_ok": True,
        "semantic_status": "success",
        "semantic_detail": after_detail,
        "seed_evidence": {
            "initial": _openra_snapshot(before_detail),
            "final": _openra_snapshot(after_detail),
        },
    }
    assert p5_aggregate._c1_openra_semantic_trial_issues(trial) == []

    tampered = deepcopy(trial)
    tampered["after_semantic_detail"]["source"] = "requested_settings"
    tampered["after_semantic_detail"]["mismatches"] = [
        {
            "section": "Game",
            "key": "TargetLines",
            "expected": "Automatic",
            "actual": "Manual",
        }
    ]
    issues = p5_aggregate._c1_openra_semantic_trial_issues(tampered)
    assert any("wrong OpenRA source" in issue for issue in issues)
    assert any("mismatches disagrees" in issue for issue in issues)


def test_openra_formal_detail_rejects_wrong_task_shape_and_seed_snapshot() -> None:
    before_detail = _openra_detail(stage="before")
    after_detail = _openra_detail(stage="after")
    trial = {
        **_policy_fields(),
        "task_id": "openra-target-lines-automatic",
        "benchmark": "OpenRA",
        "semantic_probe_source": "openra_public_settings_yaml",
        "before_semantic_ok": True,
        "before_semantic_status": "success",
        "before_semantic_detail": before_detail,
        "after_semantic_ok": True,
        "after_semantic_status": "success",
        "after_semantic_detail": after_detail,
        "seed_evidence": {
            "initial": _openra_snapshot(before_detail),
            "final": _openra_snapshot(after_detail),
        },
    }

    wrong_shape = deepcopy(trial)
    wrong_shape["after_semantic_detail"]["checks"][0].update(
        key="ViewportEdgeScroll",
        expected=False,
        actual=False,
    )
    wrong_shape["seed_evidence"]["final"] = _openra_snapshot(
        wrong_shape["after_semantic_detail"]
    )
    issues = p5_aggregate._c1_openra_semantic_trial_issues(wrong_shape)
    assert any("do not match the registered task" in issue for issue in issues)

    wrong_ok = deepcopy(trial)
    wrong_ok["after_semantic_detail"]["checks"][0]["ok"] = False
    issues = p5_aggregate._c1_openra_semantic_trial_issues(wrong_ok)
    assert any("ok disagrees with actual/expected" in issue for issue in issues)

    wrong_snapshot = deepcopy(trial)
    wrong_snapshot["seed_evidence"]["final"]["semantic_fingerprint"]["Game"][
        "TargetLines"
    ] = "Manual"
    issues = p5_aggregate._c1_openra_semantic_trial_issues(wrong_snapshot)
    assert any("disagrees with live seed snapshot" in issue for issue in issues)

    disguised_integer = deepcopy(trial)
    intro = disguised_integer["before_semantic_detail"]["checks"][1]
    intro["expected"] = 999.0
    intro["actual"] = 999.0
    disguised_integer["seed_evidence"]["initial"] = _openra_snapshot(
        disguised_integer["before_semantic_detail"]
    )
    issues = p5_aggregate._c1_openra_semantic_trial_issues(disguised_integer)
    assert any("do not match the registered task" in issue for issue in issues)


def _unity_evidence(*, stage: str, complete: bool) -> dict[str, object]:
    return {
        "required": True,
        "kind": "save_then_load",
        "slot": 0,
        "source": "jynew_public_ui_save_load_witness_v1",
        "armed_sequence": 4,
        "observed_sequence": 4 if stage == "before" else 5,
        "save_observed": complete,
        "load_observed": complete,
        "save_frame": 100 if complete else -1,
        "load_frame": 120 if complete else -1,
        "ordered": complete,
    }


def _unity_check(name: str, status: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": f"{name}: {status}"}


def _unity_trial(*, complete: bool) -> dict[str, object]:
    before_checks = [
        _unity_check("c1.transition.armed", "success"),
        _unity_check("c1.target.player_level", "success"),
        _unity_check("c1.target.money", "success"),
        _unity_check("c1.target.team.role_0", "success"),
        _unity_check("c1.target.team.role_1", "success"),
    ]
    after_checks = [
        _unity_check("assertion[jynew_role_attr].Level", "success"),
        _unity_check("assertion[jynew_money_gte]", "success"),
        _unity_check("assertion[jynew_team_contains].role_1", "success"),
        _unity_check("assertion[no_jynew_utility_errors]", "success"),
        _unity_check("c1.transition.expectation", "success"),
        _unity_check("c1.transition.save", "success" if complete else "failure"),
        _unity_check("c1.transition.load", "success" if complete else "failure"),
    ]
    after_ok = all(check["status"] == "success" for check in after_checks)
    before_detail = {
        "source": "jynew_live_semantic_probe_v1",
        "live": True,
        "detail": "C1 live operation succeeded.",
        "checks": before_checks,
        "transition_evidence": _unity_evidence(stage="before", complete=False),
        "transition_evidence_valid": True,
    }
    after_detail = {
        "source": "jynew_live_semantic_probe_v1",
        "live": True,
        "detail": (
            "C1 live operation succeeded."
            if after_ok
            else "One or more C1 live checks failed."
        ),
        "checks": after_checks,
        "transition_evidence": _unity_evidence(stage="after", complete=complete),
        "transition_evidence_valid": True,
    }
    trial = {
        **_policy_fields(),
        "task_id": "jynew-save-restore",
        "benchmark": "jynew",
        "semantic_probe_source": "jynew_live_semantic_probe_v1",
        "before_semantic_ok": True,
        "before_semantic_status": "success",
        "before_semantic_detail": before_detail,
        "after_semantic_ok": after_ok,
        "after_semantic_status": "success" if after_ok else "failure",
        "after_semantic_detail": after_detail,
        "semantic_ok": after_ok,
        "semantic_status": "success" if after_ok else "failure",
        "semantic_detail": after_detail,
    }
    trial["seed_evidence"] = {
        "initial": _unity_snapshot(before_detail, status="success", ok=True),
        "final": _unity_snapshot(
            after_detail,
            status="success" if after_ok else "failure",
            ok=after_ok,
        ),
    }
    return trial


def _unity_snapshot(
    detail: dict[str, object],
    *,
    status: str,
    ok: bool,
) -> dict[str, object]:
    evidence = detail["transition_evidence"]
    return {
        "sequence": evidence["observed_sequence"],
        "semantic_fingerprint": {
            "source": detail["source"],
            "status": status,
            "ok": ok,
            "checks": deepcopy(detail["checks"]),
            "transition_evidence": deepcopy(evidence),
            "transition_evidence_valid": detail["transition_evidence_valid"],
        },
    }


def test_unity_immediate_done_is_valid_failure_but_tampered_success_is_rejected() -> (
    None
):
    immediate_done = _unity_trial(complete=False)
    assert p5_aggregate._c1_unity_semantic_trial_issues(immediate_done) == []

    tampered = deepcopy(immediate_done)
    for check in tampered["after_semantic_detail"]["checks"]:
        check["status"] = "success"
    tampered["after_semantic_ok"] = True
    tampered["after_semantic_status"] = "success"
    tampered["semantic_ok"] = True
    tampered["semantic_status"] = "success"
    issues = p5_aggregate._c1_unity_semantic_trial_issues(tampered)
    assert any("goal save check disagrees" in issue for issue in issues)
    assert any("goal load check disagrees" in issue for issue in issues)


def test_unity_formal_detail_rejects_wrong_task_shape_snapshot_and_types() -> None:
    trial = _unity_trial(complete=True)
    assert p5_aggregate._c1_unity_semantic_trial_issues(trial) == []

    wrong_shape = deepcopy(trial)
    wrong_shape["after_semantic_detail"]["checks"][0]["name"] = (
        "assertion[jynew_map_id]"
    )
    wrong_shape["seed_evidence"]["final"] = _unity_snapshot(
        wrong_shape["after_semantic_detail"],
        status="success",
        ok=True,
    )
    issues = p5_aggregate._c1_unity_semantic_trial_issues(wrong_shape)
    assert any("do not match the registered task" in issue for issue in issues)

    wrong_snapshot = deepcopy(trial)
    wrong_snapshot["seed_evidence"]["final"]["semantic_fingerprint"]["ok"] = False
    issues = p5_aggregate._c1_unity_semantic_trial_issues(wrong_snapshot)
    assert any("disagrees with live Unity seed snapshot" in issue for issue in issues)

    disguised_sequence = deepcopy(trial)
    disguised_sequence["after_semantic_detail"]["transition_evidence"][
        "observed_sequence"
    ] = "5"
    issues = p5_aggregate._c1_unity_semantic_trial_issues(disguised_sequence)
    assert any("sequence evidence is malformed" in issue for issue in issues)

    extra_evidence_field = deepcopy(trial)
    extra_evidence_field["after_semantic_detail"]["transition_evidence"][
        "unregistered"
    ] = True
    issues = p5_aggregate._c1_unity_semantic_trial_issues(extra_evidence_field)
    assert any("transition evidence has invalid fields" in issue for issue in issues)


def test_unity_formal_detail_accepts_registered_failure_renderings() -> None:
    assertion_failure = _unity_trial(complete=True)
    team_check = assertion_failure["after_semantic_detail"]["checks"][2]
    team_check["name"] = "assertion[jynew_team_contains]"
    team_check["status"] = "failure"
    assertion_failure["after_semantic_ok"] = False
    assertion_failure["after_semantic_status"] = "failure"
    assertion_failure["semantic_ok"] = False
    assertion_failure["semantic_status"] = "failure"
    assertion_failure["seed_evidence"]["final"] = _unity_snapshot(
        assertion_failure["after_semantic_detail"],
        status="failure",
        ok=False,
    )
    assert p5_aggregate._c1_unity_semantic_trial_issues(assertion_failure) == []

    live_runtime_failure = _unity_trial(complete=True)
    live_runtime_failure["goal_evaluation_status"] = "not_evaluated"
    live_runtime_failure["before_semantic_ok"] = False
    live_runtime_failure["before_semantic_status"] = "failure"
    live_runtime_failure["before_semantic_detail"]["checks"] = [
        _unity_check("c1.transition.armed", "success"),
        _unity_check("c1.target.runtime", "failure"),
    ]
    live_runtime_failure["seed_evidence"]["initial"] = _unity_snapshot(
        live_runtime_failure["before_semantic_detail"],
        status="failure",
        ok=False,
    )
    assert p5_aggregate._c1_unity_semantic_trial_issues(live_runtime_failure) == []


def test_unity_non_save_task_rejects_inactive_witness_tampering() -> None:
    def detail(stage: str, sequence: int) -> dict[str, object]:
        return {
            "source": "pal3_live_semantic_probe_v1",
            "live": True,
            "detail": "C1 live operation succeeded.",
            "checks": (
                [_unity_check("c1.target.scene", "success")]
                if stage == "before"
                else [
                    _unity_check("assertion[pal3_scene_is]", "success"),
                    _unity_check("assertion[no_pal3_utility_errors]", "success"),
                ]
            ),
            "transition_evidence": {
                "required": False,
                "kind": "",
                "slot": -1,
                "source": "pal3_public_ui_save_load_witness_v1",
                "armed_sequence": -1,
                "observed_sequence": sequence,
                "save_observed": False,
                "reset_observed": False,
                "scene_load_observed": False,
                "load_observed": False,
                "save_frame": -1,
                "reset_frame": -1,
                "scene_load_frame": -1,
                "load_frame": -1,
                "ordered": False,
            },
            "transition_evidence_valid": True,
        }

    before_detail = detail("before", 4)
    after_detail = detail("after", 5)
    trial = {
        **_policy_fields(),
        "task_id": "pal3-scene-transition",
        "benchmark": "pal3",
        "semantic_probe_source": "pal3_live_semantic_probe_v1",
        "before_semantic_ok": True,
        "before_semantic_status": "success",
        "before_semantic_detail": before_detail,
        "after_semantic_ok": True,
        "after_semantic_status": "success",
        "after_semantic_detail": after_detail,
        "seed_evidence": {
            "initial": _unity_snapshot(before_detail, status="success", ok=True),
            "final": _unity_snapshot(after_detail, status="success", ok=True),
        },
    }
    assert p5_aggregate._c1_unity_semantic_trial_issues(trial) == []

    tampered = deepcopy(trial)
    evidence = tampered["after_semantic_detail"]["transition_evidence"]
    evidence["save_observed"] = True
    evidence["save_frame"] = 90
    tampered["after_semantic_detail"]["checks"].append(
        _unity_check("assertion[pal3_scene_is]", "success")
    )
    issues = p5_aggregate._c1_unity_semantic_trial_issues(tampered)
    assert any("contains transition events" in issue for issue in issues)
    assert any("duplicate names" in issue for issue in issues)


def _native_snapshot(*, source: str, task_id: str) -> dict[str, object]:
    openra = source == "openra_live_settings_process"
    return {
        "schema_version": 1,
        "source": source,
        "observed": True,
        "algorithm": (
            "not_applicable_public_settings" if openra else "not_applicable_unity_gui"
        ),
        "seed": 3,
        "process_id": 91,
        "process_alive_at_observation": True,
        "benchmark": "OpenRA" if openra else "jynew",
        "task_id": task_id,
        **(
            {}
            if openra
            else {"protocol_version": "warptest-c1-unity-v3", "sequence": 4}
        ),
        "semantic_fingerprint": (
            {"Game": {"TargetLines": "Manual"}}
            if openra
            else {
                "source": "jynew_live_semantic_probe_v1",
                "status": "success",
                "ok": True,
                "checks": [{"name": "live", "status": "success"}],
            }
        ),
    }


def test_unity_task_witness_arming_matches_the_registered_save_load_slots() -> None:
    """Runtime and aggregator consume the same task-owned YAML requirement."""

    tasks = yaml.safe_load((ROOT / "benchmarks/p5/c1_tasks.yaml").read_text())["tasks"]
    for task in tasks:
        if task.get("benchmark") not in {"jynew", "pal3"}:
            continue
        task_id = task["id"]
        spec = yaml.safe_load((ROOT / task["oracle_spec"]).read_text())
        surface = formal_surface_contract(task_id, "gui", root=ROOT)
        goal = surface["goal"]
        requirements = goal.get("requirements", [])
        declared = next(
            (
                requirement
                for requirement in requirements
                if requirement.get("name") == "c1.public_save_load_witness"
            ),
            None,
        )
        expectation = _save_load_transition_expectation(spec)
        expected = (
            {
                "kind": declared["kind"],
                "slot": declared["slot"],
                "ordered_events": declared["ordered_events"],
            }
            if declared is not None
            else None
        )
        assert expectation == expected, task_id


def test_unity_public_save_slot_ranges_match_the_bundled_game_ui() -> None:
    jynew_const = (
        ROOT / "games/jynew/jyx2/Assets/Scripts/GameCore/GameConst.cs"
    ).read_text(encoding="utf-8")
    jynew_panel = (
        ROOT / "games/jynew/jyx2/Assets/Scripts/UI/SavePanel.cs"
    ).read_text(encoding="utf-8")
    pal3_menu = (
        ROOT / "games/Pal3.Unity/Assets/Scripts/Pal3.Game/Dev/MainMenu.cs"
    ).read_text(encoding="utf-8")

    assert "GameSettings.GetInt(nameof(SAVE_COUNT), 3)" in jynew_const
    assert "i < GameConst.SAVE_COUNT" in jynew_panel
    assert "private const int SAVE_SLOT_COUNT = 5;" in pal3_menu
    assert "for (int i = 1; i <= SAVE_SLOT_COUNT; i++)" in pal3_menu
    tasks = yaml.safe_load((ROOT / "benchmarks/p5/c1_tasks.yaml").read_text())["tasks"]
    slots = {"jynew": set(), "pal3": set()}
    for task in tasks:
        benchmark = task.get("benchmark")
        if benchmark not in slots:
            continue
        surface = formal_surface_contract(task["id"], "gui", root=ROOT)
        for requirement in surface["goal"].get("requirements", []):
            if requirement.get("name") == "c1.public_save_load_witness":
                slots[benchmark].add(requirement["slot"])
    assert slots["jynew"] <= {0, 1, 2}
    assert slots["pal3"] <= {1, 2, 3, 4, 5}


def test_unity_check_contracts_declare_transition_checks_iff_slot_registered() -> None:
    tasks = yaml.safe_load((ROOT / "benchmarks/p5/c1_tasks.yaml").read_text())["tasks"]
    for task in tasks:
        if task.get("benchmark") not in {"jynew", "pal3"}:
            continue
        task_id = task["id"]
        contract = formal_surface_contract(task_id, "gui", root=ROOT)
        before = {
            name
            for group in contract["start"].get("expected_checks", [])
            for name in group["any_of"]
        }
        after = {
            name
            for group in contract["goal"].get("expected_checks", [])
            for name in group["any_of"]
        }
        expects_witness = bool(contract["goal"].get("requirements"))
        assert ("c1.transition.armed" in before) is expects_witness, task_id
        for name in ("c1.transition.expectation", "c1.transition.save", "c1.transition.load"):
            assert (name in after) is expects_witness, (task_id, name)


def test_openra_and_unity_seed_evidence_reject_cross_task_replay() -> None:
    for benchmark, source, algorithm in (
        ("OpenRA", "openra_live_settings_process", "not_applicable_public_settings"),
        ("jynew", "unity_live_process", "not_applicable_unity_gui"),
    ):
        snapshot = _native_snapshot(source=source, task_id="wrong-task")
        evidence = {
            "schema_version": 1,
            "source": source,
            "observed": True,
            "algorithm": algorithm,
            "seed": 3,
            "task_id": "wrong-task",
            "initial": snapshot,
            "final": deepcopy(snapshot),
        }
        issues = p5_aggregate._runtime_seed_evidence_issues(
            evidence,
            benchmark=benchmark,
            seed=3,
            task_id="registered-task",
            expected_semantic_stage="entry",
            require_final=True,
        )
        assert any("wrong task" in issue for issue in issues), (benchmark, issues)
