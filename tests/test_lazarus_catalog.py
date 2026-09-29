from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image

from warptest.lazarus_catalog import (
    LAZARUS_BENCHMARK_IDS,
    LAZARUS_FORMAL_BENCHMARK_IDS,
    materialize_lazarus_spec,
    validate_lazarus_catalog,
)
from warptest.lazarus_goldens import LazarusGoldenFrame, register_lazarus_golden_set
from warptest.lazarus_mining import build_upstream_record
from warptest.p5_scale import C1_ARMS, C1_FORMAL_PAPER_ARMS
from warptest.state_loop import load_state_loop_tasks
from warptest.task_descriptions import canonical_state_description


_REVISIONS = {
    "luanti": "c48f698073df253fc6743d16cf056c1489b60dfb",
    "blender": "3d018bcabdaeea66cf4ef4a96ce8913164831f6e",
    "jupyterlab": "4a19f8fff4049a9536e21fa2980fe7e5ee592d01",
    "openra": "7e687f430ee5b194c0ffaa06caaba757dd9155fb",
    "jynew": "be27d95303cb1915bdfbcfe15046fa3f937b7eb7",
    "pal3": "577017d943751a81886604a7744996e9ac968318",
}
_REPOS = {
    "luanti": "games/luanti",
    "blender": "games/blender",
    "jupyterlab": "games/jupyterlab",
    "openra": "games/OpenRA",
    "jynew": "games/jynew",
    "pal3": "games/Pal3.Unity",
}


def _golden_image(path: Path, *, changed: bool = False) -> Path:
    image = Image.new("RGB", (400, 240))
    pixels = image.load()
    for x in range(400):
        for y in range(240):
            pixels[x, y] = ((x * 17 + y * 3) % 255, (x * 7 + y * 11) % 255, (x * 5 + y * 13) % 255)
    if changed:
        for x in range(150, 250):
            for y in range(105, 165):
                pixels[x, y] = (240, 210, 60)
    image.save(path)
    return path


def _fixture(root: Path) -> tuple[Path, dict, dict]:
    source = root / "benchmarks/p5/c1_lazarus_tasks.yaml"
    defect_dir = root / "benchmarks/p5/lazarus/defects/lz-luanti-example"
    spec_path = root / "specs/luanti/lazarus/lz-luanti-example.yaml"
    defect_dir.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")

    patch = b"diff --git a/builtin/example.lua b/builtin/example.lua\n"
    (defect_dir / "revert.patch").write_bytes(patch)
    upstream = build_upstream_record(
        task_id="lz-luanti-example",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue={
            "url": "https://github.com/luanti-org/luanti/issues/1",
            "title": "Moving a stack leaves the wrong node state",
            "body": "Reproduction steps from the upstream reporter.",
        },
        pull_request=None,
        comments=(),
        reviews=(),
        fix_commit="1" * 40,
        pinned_revision=_REVISIONS["luanti"],
        touched_files=("builtin/example.lua",),
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        tests=(
            {
                "id": "lua.stack-regression",
                "kind": "fail_to_pass",
                "command": ["python", "-V"],
                "provenance": "upstream_test",
            },
            {
                "id": "lua.inventory-smoke",
                "kind": "pass_to_pass",
                "command": ["python", "-V"],
                "provenance": "upstream_test",
            },
        ),
        retrieved_at="2026-08-10T00:00:00Z",
    )
    (defect_dir / "upstream.json").write_text(
        json.dumps(upstream, sort_keys=True), encoding="utf-8"
    )
    (defect_dir / "gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "lz-luanti-example",
                "status": "passed",
                "checks": {
                    "fixed_f2p": True,
                    "buggy_f2p": False,
                    "fixed_p2p": True,
                    "buggy_p2p": True,
                    "before_equivalent": True,
                    "fixed_goal": True,
                    "buggy_signature": True,
                    "fixed_reverse_signature": False,
                    "fixed_invariants": True,
                    "buggy_invariants": True,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    capture_dir = root / "captures"
    capture_dir.mkdir()
    reports = []
    for name in ("fixed-before", "buggy-before", "fixed-after", "buggy-after"):
        report = capture_dir / f"{name}.json"
        report.write_text('{"status":"success"}\n', encoding="utf-8")
        reports.append(report)
    register_lazarus_golden_set(
        task_id="lz-luanti-example",
        repository_root=root,
        output_dir=spec_path.parent / "visual",
        fixed_before=LazarusGoldenFrame(
            _golden_image(capture_dir / "fixed-before.png"), reports[0], True
        ),
        buggy_before=LazarusGoldenFrame(
            _golden_image(capture_dir / "buggy-before.png"), reports[1], True
        ),
        fixed_after=LazarusGoldenFrame(
            _golden_image(capture_dir / "fixed-after.png"), reports[2], True
        ),
        buggy_after=LazarusGoldenFrame(
            _golden_image(capture_dir / "buggy-after.png", changed=True), reports[3], True
        ),
        after_min_changed_pixel_ratio=0.01,
    )

    goal = {
        "type": "luanti_node_is",
        "pos": {"x": 0, "y": 1, "z": 0},
        "expected": "testnodes:light14",
    }
    signature = {
        "type": "luanti_node_is",
        "pos": {"x": 0, "y": 1, "z": 0},
        "expected": "air",
    }
    invariant = {"type": "no_luanti_mod_errors"}
    spec = {
        "name": "Lazarus fixture",
        "benchmark": "luanti",
        "timeout_ms": 2_400_000,
        "target": {
            "kind": "synthesized_world",
            "player": {"pos": {"x": 0, "y": 2, "z": 0}},
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [],
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                "assertions": [],
                "surfaces": {
                    "gui": {
                        "adapter": "luanti_live_world_v1",
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    },
                    "state": {
                        "adapter": "luanti_live_world_v1",
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    },
                },
            },
            "visual_oracle": {
                "description": "The shared starting storage area is visible.",
                "rubric": {
                    "must": ["The world is rendered normally."],
                    "must_not": ["A crash dialog is visible."],
                    "ignore": ["Camera angle."],
                },
                "capture": {"stage": "post_smoke", "wait_ms": 1000, "full_page": False},
            },
            "lazarus": {
                "schema_version": 1,
                "goal_checks": [goal],
                "defect_signature": [signature],
                "invariant_checks": [invariant],
                "upstream_tests": {
                    "fail_to_pass": ["lua.stack-regression"],
                    "pass_to_pass": ["lua.inventory-smoke"],
                    "per_trial_p2p": ["lua.inventory-smoke"],
                },
                "goldens": {
                    "before": "visual/before.golden.png",
                    "buggy_before": "visual/buggy.before.golden.png",
                    "fixed": {
                        "after": "visual/fixed.after.golden.png",
                        "after_description": "The corrected stack transfer is visible.",
                        "after_rubric": {
                            "must": ["The fixed outcome is visible."],
                            "must_not": ["The buggy outcome is visible."],
                            "ignore": ["Camera angle."],
                        },
                    },
                    "buggy": {
                        "after": "visual/buggy.after.golden.png",
                        "after_description": "The historical failure signature is visible.",
                        "after_rubric": {
                            "must": ["The historical outcome is visible."],
                            "must_not": ["A crash dialog is visible."],
                            "ignore": ["Camera angle."],
                        },
                    },
                },
            },
        },
    }
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    preconditions = "A creative devtest world is open at a storage area."
    reference_steps = "Open the storage UI and move its full stack once."
    hosts = {
        benchmark: {
            "display_name": benchmark,
            "repo": _REPOS[benchmark],
            "gitmodules_url": f"https://github.com/example/{benchmark}.git",
            "revision": _REVISIONS[benchmark],
        }
        for benchmark in LAZARUS_BENCHMARK_IDS
    }
    payload = {
        "schema_version": 1,
        "task_schema": "c1_lazarus_v1",
        "benchmark_hosts": hosts,
        "tasks": [
            {
                "id": "lz-luanti-example",
                "benchmark": "luanti",
                "repo": "games/luanti",
                "prompt_revision": "lz-v1",
                "random_control": {
                    "mode": "experiment_seed",
                    "seeds": [0, 1, 2],
                    "algorithm": "luanti_fixed_map_seed",
                },
                "preconditions": preconditions,
                "reference_steps": reference_steps,
                "state_description": canonical_state_description(
                    preconditions, reference_steps
                ),
                "horizon_class": "mid",
                "oracle_spec": "specs/luanti/lazarus/lz-luanti-example.yaml",
                "tags": ["c1", "lazarus", "luanti"],
                "capabilities": ["gui-localization", "state-persistence"],
                "budget_ms": 2_400_000,
                "defect": {
                    "upstream": {
                        "issue": "https://github.com/luanti-org/luanti/issues/1",
                        "pr": "https://github.com/luanti-org/luanti/pull/2",
                    },
                    "fix_commit": "1" * 40,
                    "revert_patch": (
                        "benchmarks/p5/lazarus/defects/"
                        "lz-luanti-example/revert.patch"
                    ),
                    "patch_sha256": hashlib.sha256(patch).hexdigest(),
                    "build_class": "none",
                    "defect_locus": "at_goal",
                    "upstream_record": (
                        "benchmarks/p5/lazarus/defects/"
                        "lz-luanti-example/upstream.json"
                    ),
                    "gate_report": (
                        "benchmarks/p5/lazarus/defects/"
                        "lz-luanti-example/gate.json"
                    ),
                },
            }
        ],
    }
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return source, payload, spec


def test_phase0_catalog_loads_only_public_issue_fields_into_model_tasks() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml"

    hosts, tasks = validate_lazarus_catalog(
        yaml.safe_load(path.read_text(encoding="utf-8")),
        source_path=path,
    )
    loaded = load_state_loop_tasks(root, path)

    assert set(hosts) == set(LAZARUS_BENCHMARK_IDS)
    assert [task["id"] for task in tasks] == [
        "lz-luanti-14757-math-round",
        "lz-openra-21453-hotkey-ready-building",
    ]
    assert [task.id for task in loaded] == [task["id"] for task in tasks]
    assert all(task.issue.tags == ["c1", "lazarus", task.benchmark] for task in loaded)
    assert all("defect" not in task.issue.public_payload() for task in loaded)
    assert loaded[0].coordinator_metadata["defect"]["defect_locus"] == "at_goal"
    assert loaded[1].coordinator_metadata["defect"]["defect_locus"] == "on_path"
    assert "basenodes:sand" in loaded[0].issue.issue
    assert "basenodes:cobble" in loaded[0].issue.issue
    assert "hovering at (0, 3, 6)" in loaded[0].issue.issue
    # The graded outcome nodes stay out of the public task text.
    assert "basenodes:dirt_with_grass" not in loaded[0].issue.issue
    assert "basenodes:desert_sand" not in loaded[0].issue.issue


def test_lazarus_catalog_loads_without_exposing_coordinator_contract(tmp_path: Path) -> None:
    source, payload, _ = _fixture(tmp_path)

    hosts, rows = validate_lazarus_catalog(payload, source_path=source)
    tasks = load_state_loop_tasks(tmp_path, source)

    assert tuple(hosts) == LAZARUS_BENCHMARK_IDS
    assert rows[0]["defect"]["defect_locus"] == "at_goal"
    assert tasks[0].coordinator_metadata["defect"]["fix_commit"] == "1" * 40
    public_payload = tasks[0].issue.public_payload()
    public_json = json.dumps(public_payload, sort_keys=True)
    assert "defect" not in public_json
    assert "goal_checks" not in public_json
    assert "fix_commit" not in public_json
    assert "version" not in public_json


def test_formal_catalog_locks_the_four_upstream_test_repositories(tmp_path: Path) -> None:
    source, payload, _ = _fixture(tmp_path)
    formal = copy.deepcopy(payload)
    formal["formal_population"] = True
    formal["benchmark_hosts"] = {
        benchmark: formal["benchmark_hosts"][benchmark]
        for benchmark in LAZARUS_FORMAL_BENCHMARK_IDS
    }

    hosts, _ = validate_lazarus_catalog(formal, source_path=source)

    assert tuple(hosts) == LAZARUS_FORMAL_BENCHMARK_IDS


def test_formal_catalog_rejects_excluded_unity_hosts(tmp_path: Path) -> None:
    source, payload, _ = _fixture(tmp_path)
    formal = copy.deepcopy(payload)
    formal["formal_population"] = True

    with pytest.raises(ValueError, match="formal benchmark_hosts"):
        validate_lazarus_catalog(formal, source_path=source)


def test_lazarus_catalog_enforces_the_reverse_tokenguard(tmp_path: Path) -> None:
    """A coordinator-only target token cannot reach the public issue surface."""

    source, payload, _ = _fixture(tmp_path)
    invalid = copy.deepcopy(payload)
    row = invalid["tasks"][0]
    row["reference_steps"] += " Do not revert the move afterward."
    row["state_description"] = canonical_state_description(
        row["preconditions"], row["reference_steps"]
    )

    with pytest.raises(ValueError, match="coordinator-only token"):
        validate_lazarus_catalog(invalid, source_path=source)


def test_public_text_may_name_a_value_both_versions_expect(tmp_path: Path) -> None:
    """A start value shared by goal and defect cannot reveal which version runs."""

    source, payload, spec = _fixture(tmp_path)
    spec_path = tmp_path / str(payload["tasks"][0]["oracle_spec"])
    lazarus = spec["evaluation"]["lazarus"]
    shared = {
        "type": "luanti_node_is",
        "pos": {"x": 0, "y": 0, "z": 0},
        "expected": "testnodes:pedestal",
    }
    lazarus["goal_checks"] = [*lazarus["goal_checks"], shared]
    lazarus["defect_signature"] = [*lazarus["defect_signature"], shared]
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    row = copy.deepcopy(payload)["tasks"][0]
    row["preconditions"] += " The stack rests on testnodes:pedestal."
    row["state_description"] = canonical_state_description(
        row["preconditions"], row["reference_steps"]
    )
    allowed = copy.deepcopy(payload)
    allowed["tasks"] = [row]

    _, rows = validate_lazarus_catalog(allowed, source_path=source)

    assert "testnodes:pedestal" in rows[0]["preconditions"]


def test_public_text_still_cannot_name_a_value_only_one_version_expects(
    tmp_path: Path,
) -> None:
    source, payload, _ = _fixture(tmp_path)
    invalid = copy.deepcopy(payload)
    row = invalid["tasks"][0]
    row["preconditions"] += " The lamp reads testnodes:light14."
    row["state_description"] = canonical_state_description(
        row["preconditions"], row["reference_steps"]
    )

    with pytest.raises(ValueError, match="testnodes:light14"):
        validate_lazarus_catalog(invalid, source_path=source)


def test_lazarus_projection_selects_semantics_and_both_after_oracles(tmp_path: Path) -> None:
    _, _, spec = _fixture(tmp_path)

    goal = materialize_lazarus_spec(spec, view="goal")
    defect = materialize_lazarus_spec(spec, view="defect")

    assert [item["type"] for item in goal["assertions"]] == [
        "luanti_node_is",
        "no_luanti_mod_errors",
    ]
    assert goal["assertions"][0]["expected"] == "testnodes:light14"
    assert goal["visual_oracle"]["golden_before"] == "visual/before.golden.png"
    assert goal["visual_oracle"]["golden_after"] == "visual/fixed.after.golden.png"
    assert goal["visual_oracle"]["golden_image"] == "visual/fixed.after.golden.png"

    assert defect["assertions"][0]["expected"] == "air"
    assert defect["assertions"][-1]["type"] == "no_luanti_mod_errors"
    assert defect["visual_oracle"]["golden_after"] == "visual/buggy.after.golden.png"
    assert defect["visual_oracle"]["golden_image"] == "visual/buggy.after.golden.png"
    # Publishing the buggy start frame is a review affordance; both arms are
    # still judged against the one registered start oracle.
    assert defect["visual_oracle"]["golden_before"] == "visual/before.golden.png"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema_version=2), "c1_lazarus_v1"),
        (
            lambda payload: payload["tasks"][0].update(
                oracle_spec="../../outside.yaml"
            ),
            "escapes repository root",
        ),
        (
            lambda payload: payload["tasks"][0]["random_control"].update(
                algorithm="mulberry32"
            ),
            "luanti_fixed_map_seed",
        ),
        (
            lambda payload: payload["tasks"][0].update(
                reference_steps=(
                    "Open the storage UI; the expected value is testnodes:light14."
                ),
                state_description=canonical_state_description(
                    "A creative devtest world is open at a storage area.",
                    "Open the storage UI; the expected value is testnodes:light14.",
                ),
            ),
            "leaks hidden",
        ),
    ],
)
def test_lazarus_catalog_fails_closed_on_schema_paths_randomness_and_leakage(
    tmp_path: Path, mutate, message: str
) -> None:
    source, payload, _ = _fixture(tmp_path)
    invalid = copy.deepcopy(payload)
    mutate(invalid)

    with pytest.raises(ValueError, match=message):
        validate_lazarus_catalog(invalid, source_path=source)


def test_lazarus_does_not_change_the_frozen_c1_arm_contract() -> None:
    # The paper's frozen formal subset stays C/D/E; the ScaleCUA arms extend
    # the registered universe without entering that subset.
    assert C1_FORMAL_PAPER_ARMS == C1_ARMS
    assert tuple(C1_ARMS) == (
        "c1_agent_s",
        "c1_agent_s_synth",
        "c1_state",
        "c1_agent_scalecua",
        "c1_agent_scalecua_synth",
    )
