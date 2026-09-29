from __future__ import annotations

import copy
import subprocess
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import generate_visual_pairs
from warptest import run_context
from warptest.c1_catalog import (
    CANONICAL_BENCHMARK_IDS,
    canonical_benchmark_id,
    expected_random_control,
    validate_c1_v5_catalog,
)
from warptest.c1_provenance import (
    C1EnvironmentMismatch,
    _gitmodules_urls,
    c1_task_manifest_hash,
    validate_c1_environment,
)
from warptest.p5_scale import write_subset_tasks
from warptest.state_loop import load_state_loop_tasks


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "benchmarks/p5/c1_tasks.yaml"
MANIFEST_PATH = ROOT / "benchmarks/p5/manifest.yaml"
REPRODUCIBILITY_PATH = ROOT / "docs/c1_REPRODUCIBILITY.md"
REGISTERED_CATALOG_PATHS = (
    TASKS_PATH,
    ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml",
    ROOT / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml",
    ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml",
)


EXPECTED_REVISIONS = {
    "2048": "8b5c8a899a4acfd3258ad76e0af8e35a6bf7a3b9",
    "datingsim": "withheld",
    "spacerpg": "withheld",
    "luanti": "ba414e29fddf52337b16d673e17f87165bab3eff",
    "openra": "7e687f430ee5b194c0ffaa06caaba757dd9155fb",
    "jupyterlab": "4a19f8fff4049a9536e21fa2980fe7e5ee592d01",
    "libreoffice": "3f84070274f87588c9ef69e9ea9e2426335392c8",
    "blender": "3d018bcabdaeea66cf4ef4a96ce8913164831f6e",
    "jynew": "be27d95303cb1915bdfbcfe15046fa3f937b7eb7",
    "pal3": "577017d943751a81886604a7744996e9ac968318",
}


def _catalog() -> dict:
    return yaml.safe_load(TASKS_PATH.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_c1_v5_catalog_has_complete_public_and_random_contracts() -> None:
    payload = _catalog()
    hosts, tasks = validate_c1_v5_catalog(payload, source_path=TASKS_PATH)

    assert tuple(hosts) == CANONICAL_BENCHMARK_IDS
    assert len(hosts) == 10
    assert len(tasks) == len({task["id"] for task in tasks}) == 61
    assert {benchmark: host["revision"] for benchmark, host in hosts.items()} == (
        EXPECTED_REVISIONS
    )
    assert Counter(task["benchmark"] for task in tasks) == Counter(
        _manifest()["c1_benchmark_task_counts"]
    )
    assert Counter(task["prompt_revision"] for task in tasks) == {
        "c1-legacy-v1": 16,
        "c1-legacy-self-contained-v1": 7,
        "c1-legacy-v2-playable-story-jump": 1,
        "c1-legacy-v3-locatable-coordinates": 5,
        "c1-deep30-rng-free-v1": 3,
        "c1-deep30-v3": 14,
        "c1-deep30-v7-realistic-credits": 1,
        "c1-deep30-random-control-v1": 3,
        "c1-deep30-v5-ui-save-slots": 1,
        "c1-deep30-v6-player-reachable": 1,
        "c1-deep30-v8-console-syntax": 6,
        "c1-deep30-v9-trimmed-wording": 1,
        "c1-deep30-v10-locatable-coordinates": 2,
    }
    for task in tasks:
        assert task["benchmark"] == canonical_benchmark_id(task["benchmark"])
        assert task["repo"] == hosts[task["benchmark"]]["repo"]
        assert task["random_control"] == expected_random_control(task["benchmark"])
        assert all(
            "registered" not in str(task[field]).lower()
            for field in ("preconditions", "reference_steps", "state_description")
        )
        spec = yaml.safe_load((ROOT / task["oracle_spec"]).read_text(encoding="utf-8"))
        assert spec["benchmark"] == task["benchmark"]


def test_reviewed_c1_task_corrections_remain_bound_to_public_specs() -> None:
    tasks = {task["id"]: task for task in _catalog()["tasks"]}

    spacerpg_task = tasks["spacerpg-contraband-stealth-run"]
    spacerpg_spec = yaml.safe_load(
        (ROOT / spacerpg_task["oracle_spec"]).read_text(encoding="utf-8")
    )
    assert spacerpg_task["prompt_revision"] == "c1-deep30-v7-realistic-credits"
    assert "1725 credits" in spacerpg_task["preconditions"]
    assert spacerpg_task["preconditions"].count("previously acquired") == 2
    assert spacerpg_spec["target"]["credits"] == 1725
    assert spacerpg_spec["target"]["cargo"] == {}
    assert spacerpg_spec["actions"][-1] == {
        "type": "hook_call",
        "method": "setStationTab",
        "args": ["Hangar"],
    }
    assert "open the Hangar tab" in spacerpg_task["reference_steps"]
    credits_goal = next(
        assertion
        for assertion in spacerpg_spec["evaluation"]["semantic"]["assertions"]
        if assertion.get("path") == "player.credits"
    )
    assert credits_goal == {
        "type": "state_gte",
        "path": "player.credits",
        "value": 1726,
    }

    pal3_task = tasks["pal3-scene-transition"]
    pal3_spec = yaml.safe_load(
        (ROOT / pal3_task["oracle_spec"]).read_text(encoding="utf-8")
    )
    assert pal3_task["prompt_revision"] == "c1-legacy-v2-playable-story-jump"
    for command in (
        "ScriptVarSetValue 32768 20200",
        "SceneLoad q02 q02",
        "ActorSetTilePosition -1 70 209",
        "CameraFadeIn",
    ):
        assert command in pal3_task["reference_steps"]
    assert pal3_spec["target"] | {
        "make_playable": True,
        "player_tile_x": 195,
        "player_tile_y": 212,
        "player_nav_layer": 0,
    } == pal3_spec["target"]
    scene_action = next(
        action for action in pal3_spec["actions"] if action["type"] == "pal3_load_scene"
    )
    assert scene_action == {
        "type": "pal3_load_scene",
        "city": "q02",
        "scene": "q02",
        "make_playable": True,
        "player_tile_x": 70,
        "player_tile_y": 209,
        "player_nav_layer": 0,
    }

    luanti_override = generate_visual_pairs.TASK_OVERRIDES[
        "luanti-mine-bridge-outpost-build"
    ]
    assert luanti_override["before_actions"] == [
        generate_visual_pairs.LUANTI_MINE_OUTPOST_VANTAGE
    ]
    assert luanti_override["after_extra_actions"] == [
        generate_visual_pairs.LUANTI_MINE_OUTPOST_VANTAGE
    ]
    assert luanti_override["require_changed_pixel_ratio"] == 0.015


def test_host_registry_matches_gitmodules_and_registered_parent_gitlinks() -> None:
    hosts, _ = validate_c1_v5_catalog(_catalog(), source_path=TASKS_PATH)
    urls = _gitmodules_urls(ROOT)
    registered_revisions: dict[str, set[str]] = {}
    for catalog_path in REGISTERED_CATALOG_PATHS:
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        for registered_host in catalog.get("benchmark_hosts", {}).values():
            registered_revisions.setdefault(registered_host["repo"], set()).add(
                registered_host["revision"]
            )

    for benchmark, host in hosts.items():
        assert urls[host["repo"]] == host["gitmodules_url"], benchmark
        row = subprocess.run(
            ["git", "ls-files", "-s", "--", host["repo"]],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().split()
        assert row[:1] == ["160000"], benchmark
        # Main can serve a different frozen experiment catalog than C1 v5 at a
        # given moment (currently Luanti P2P-only). The checkout must still be
        # one of the revisions declared by a checked-in catalog; formal runtime
        # preflight separately requires the exact selected-catalog revision.
        assert row[1] in registered_revisions[host["repo"]], benchmark


def test_current_catalog_rejects_historical_benchmark_aliases() -> None:
    payload = _catalog()
    payload["tasks"][0]["benchmark"] = "OpenRA"
    with pytest.raises(ValueError, match="non-canonical benchmark"):
        validate_c1_v5_catalog(payload, source_path=TASKS_PATH)
    assert canonical_benchmark_id("OpenRA") == "openra"
    assert canonical_benchmark_id("Pal3.Unity") == "pal3"


def test_reproducibility_document_uses_the_registered_matrix_counts() -> None:
    document = REPRODUCIBILITY_PATH.read_text(encoding="utf-8")

    assert "other 16 unchanged legacy tasks" in document
    assert "contains 61 tasks over 10 applications" in document
    assert "registered matrix contains 3,660" in document
    assert "other 21 unchanged legacy tasks" not in document
    assert "contains 62 tasks over 10 applications" not in document
    assert "registered matrix contains 1,674" not in document


def test_selected_c1_catalog_preserves_global_analysis_registry(
    tmp_path: Path,
) -> None:
    selected_ids = [
        "2048-merge-left",
        "love-office-work-coding",
        "spacerpg-station-save",
        "luanti-set-node",
    ]
    subset_path = write_subset_tasks(
        ROOT,
        TASKS_PATH,
        selected_ids,
        tmp_path / "selected-c1-tasks.yaml",
    )

    tasks = load_state_loop_tasks(ROOT, subset_path)

    assert [task.id for task in tasks] == selected_ids


def test_selected_c1_catalog_cannot_bypass_invalid_source_analysis(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    catalog["expansion_sets"]["deep30"]["task_ids"][0] = "not-a-registered-task"
    source_path = tmp_path / "invalid-c1-tasks.yaml"
    source_path.write_text(
        yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="references unknown task ids"):
        write_subset_tasks(
            ROOT,
            source_path,
            ["2048-merge-left"],
            tmp_path / "selected-c1-tasks.yaml",
        )


@pytest.mark.parametrize("requested", ["LibreOffice", "libreoffice"])
def test_visual_pair_generator_accepts_legacy_and_canonical_c1_ids(
    requested: str,
) -> None:
    selected = generate_visual_pairs.tasks_for_benchmarks(
        [requested],
        ["libreoffice-writer-board-publication"],
        c1_tasks=True,
    )

    assert len(selected) == 1
    assert selected[0][0:2] == (
        "libreoffice-writer-board-publication",
        "libreoffice",
    )
    assert tuple(generate_visual_pairs.BENCHMARKS) == CANONICAL_BENCHMARK_IDS


def test_task_manifest_hash_changes_for_every_reproducibility_surface(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    manifest = _manifest()
    task_path = tmp_path / "benchmarks/p5/c1_tasks.yaml"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state"):
        manifest["arms"][arm]["tasks"] = "benchmarks/p5/c1_tasks.yaml"

    def digest(
        task_payload: dict,
        manifest_payload: dict,
    ) -> str:
        task_path.write_text(
            yaml.safe_dump(task_payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return c1_task_manifest_hash(
            tmp_path,
            manifest=manifest_payload,
            arms=["c1_agent_s", "c1_agent_s_synth", "c1_state"],
            task_ids=["2048-merge-left"],
            manifest_hash=run_context.manifest_hash(manifest_payload),
        )

    baseline = digest(catalog, manifest)
    variants: list[tuple[dict, dict]] = []
    changed_prompt = copy.deepcopy(catalog)
    changed_prompt["tasks"][0]["preconditions"] += " Changed."
    variants.append((changed_prompt, manifest))
    changed_random = copy.deepcopy(catalog)
    changed_random["tasks"][0]["random_control"]["algorithm"] = "changed"
    variants.append((changed_random, manifest))
    changed_host = copy.deepcopy(catalog)
    changed_host["benchmark_hosts"]["2048"]["revision"] = "0" * 40
    variants.append((changed_host, manifest))
    changed_os = copy.deepcopy(manifest)
    changed_os["c1_reproducibility"]["os"] = "macOS changed"
    variants.append((catalog, changed_os))
    changed_screen = copy.deepcopy(manifest)
    changed_screen["c1_reproducibility"]["physical_screen"] = [1, 1]
    variants.append((catalog, changed_screen))

    assert all(digest(tasks, run_manifest) != baseline for tasks, run_manifest in variants)


def test_environment_mismatch_is_structured_and_fail_closed() -> None:
    manifest = _manifest()
    observed = {
        "os": manifest["c1_reproducibility"]["os"],
        "architecture": manifest["c1_reproducibility"]["architecture"],
        "physical_screen": [1, 1],
    }
    with patch(
        "warptest.c1_provenance.observe_c1_environment",
        return_value=observed,
    ):
        with pytest.raises(C1EnvironmentMismatch) as captured:
            validate_c1_environment(ROOT, manifest=manifest)
    assert captured.value.evidence["status"] == "mismatch"
    assert captured.value.evidence["mismatches"] == ["physical_screen"]
    assert len(captured.value.evidence["environment_fingerprint"]) == 64
