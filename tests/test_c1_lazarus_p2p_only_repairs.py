from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import apply_lazarus_p2p_only_task_text as text_applier
from scripts import mine_lazarus_p2p_only_candidates as candidate_miner
from scripts.capture_lazarus_p2p_only_frames import split_reports
from scripts.publish_lazarus_p2p_only_catalog import (
    LUANTI_CONTROL_RUNNER_ROOT,
    control_test_record,
    require_publishable_goldens,
    runner_tree_sha256,
)
from warptest.c1_lazarus_selection import p2p_only_engine_capture as engine_capture
from warptest.c1_lazarus_selection.p2p_only_engine_capture import (
    _expected_engine_state,
    _terminate_process_group,
    validate_engine_variant_differential,
)
from warptest.c1_lazarus_selection.p2p_only_frames import FRAME_SPECS
from warptest.c1_lazarus_selection.p2p_only_gate16 import (
    _runtime_ready,
    _write_runtime_manifest,
)
from warptest.c1_lazarus_selection.p2p_only_profiles import (
    HOST_UPSTREAM,
    load_profile_registry,
    timeline_fix_linkage,
)
from warptest.c1_lazarus_selection.c1_lazarus_task_text import FRAMES
from warptest.c1_lazarus_selection.p2p_only_task_text import TASK_TEXT
from warptest.lazarus_mining import classify_build_class
from warptest.lazarus_upstream import UpstreamTestDefinition
from warptest.task_descriptions import canonical_state_description


ROOT = Path(__file__).resolve().parents[1]


def _profiles():
    return {item.task_id: item for item in load_profile_registry(ROOT)}


def _write_fake_luanti_binary(root: Path, revision: str) -> Path:
    binary = root / "build-app/out/luanti.app/Contents/MacOS/luanti"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/bin/sh\n"
        f"printf 'Luanti 5.17.0-dev-{revision[:9]} (OSX)\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_every_profile_declares_a_timeline_fix_binding() -> None:
    for profile in _profiles().values():
        assert HOST_UPSTREAM[profile.host]
        assert profile.timeline_cites_fix_commit or profile.timeline_links_fix_change


def test_timeline_fix_binding_accepts_the_exact_fixing_pull_request() -> None:
    direct, change = timeline_fix_linkage(
        [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 42,
                        "repository_url": "https://api.github.com/repos/org/repo",
                        "pull_request": {
                            "url": "https://api.github.com/repos/org/repo/pulls/42"
                        },
                    }
                },
            }
        ],
        upstream_slug="org/repo",
        change_number=42,
        fixed_revision="a" * 40,
    )

    assert direct is False
    assert change is True


def test_timeline_fix_binding_rejects_an_unrelated_pull_request() -> None:
    direct, change = timeline_fix_linkage(
        [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 99,
                        "repository_url": "https://api.github.com/repos/org/repo",
                        "pull_request": {
                            "url": "https://api.github.com/repos/org/repo/pulls/99"
                        },
                    }
                },
            }
        ],
        upstream_slug="org/repo",
        change_number=42,
        fixed_revision="a" * 40,
    )

    assert direct is False
    assert change is False


def test_native_engine_modes_bind_the_registered_starts_and_actions() -> None:
    world = FRAME_SPECS["lzp-luanti-16799-world-mods"]
    tooltip = FRAME_SPECS["lzp-luanti-17152-server-list-search-tooltip"]
    loadgame = FRAME_SPECS["lzp-openra-22476-load-game-faction-filter"]
    editor = FRAME_SPECS["lzp-openra-22283-tileset-template-order"]

    assert (world.engine_before_mode, world.engine_after_mode) == (
        "luanti-local-world",
        "luanti-configure-world",
    )
    assert world.engine_frame_size == (1920, 1080)
    assert world.after_min_changed_pixel_ratio == pytest.approx(0.0005)
    assert tooltip.engine_after_mode == "luanti-online-tooltip"
    assert tooltip.hover_point == (400, 165)
    assert _expected_engine_state(tooltip, variant="fixed", stage="after")[
        "tooltip_visible"
    ] == "1"
    assert (loadgame.engine_before_mode, loadgame.engine_after_mode) == (
        "openra-mainmenu",
        "openra-loadgame-dropdown",
    )
    assert (editor.engine_before_mode, editor.engine_after_mode) == (
        "openra-editor-overlays",
        "openra-editor-tiles",
    )
    assert _expected_engine_state(world, variant="fixed", stage="before")[
        "world_present"
    ] == "1"
    assert _expected_engine_state(world, variant="fixed", stage="after")[
        "dialog_fits"
    ] == "1"
    assert _expected_engine_state(editor, variant="fixed", stage="before")[
        "tile_widgets"
    ] == "0"
    assert _expected_engine_state(loadgame, variant="fixed", stage="after")[
        "faction_labels"
    ] == "Allies,Soviet"
    assert _expected_engine_state(loadgame, variant="buggy", stage="after")[
        "faction_labels"
    ] == "allies,soviet"
    assert _expected_engine_state(editor, variant="fixed", stage="after")[
        "template_ids"
    ].startswith("255,65535,")
    assert _expected_engine_state(editor, variant="buggy", stage="after")[
        "template_ids"
    ].startswith("1,2,3,")


def test_luanti_online_after_rubric_is_not_self_contradictory() -> None:
    rubric = TASK_TEXT["lzp-luanti-17152-server-list-search-tooltip"].rubric(
        "fixed"
    )
    assert any("online tab" in item for item in rubric["must"])
    assert all("local-game tab" not in item for item in rubric["must"])


def test_reference_steps_state_actions_and_keep_the_readout_private() -> None:
    """The model is told what to do; what the verdict turns on stays hidden."""

    readout_clause = re.compile(r",\s*then read\b", re.IGNORECASE)
    for task_id, text in TASK_TEXT.items():
        public = f"{text.preconditions}\n{text.reference_steps}"
        assert not readout_clause.search(text.reference_steps), task_id
        assert text.readout, task_id
        assert text.readout not in public, task_id


def test_only_after_frame_rubrics_carry_the_registered_readout() -> None:
    for task_id, text in TASK_TEXT.items():
        for frame in FRAMES:
            rubric = text.rubric(frame)
            expected = [text.scene, text.description(frame)]
            if frame != "before":
                expected.append(text.readout)
            assert rubric["must"] == expected, (task_id, frame)


def test_catalog_rows_carry_the_registered_public_wording() -> None:
    catalog = yaml.safe_load(text_applier.CATALOG.read_text(encoding="utf-8"))
    rows = {str(row["id"]): row for row in catalog["tasks"]}
    # The registry also holds candidates the catalog does not admit, so it may
    # cover more rows than the catalog carries, but never fewer.
    assert set(rows) <= set(TASK_TEXT)
    for task_id, row in rows.items():
        text = TASK_TEXT[task_id]
        assert row["preconditions"] == text.preconditions, task_id
        assert row["reference_steps"] == text.reference_steps, task_id
        assert row["state_description"] == canonical_state_description(
            text.preconditions, text.reference_steps
        ), task_id


def test_published_files_match_the_wording_registry() -> None:
    """The offline applier is the only writer of this text between publishes."""

    drifted = sorted(
        str(path.relative_to(ROOT))
        for path, content in text_applier.planned_documents().items()
        if path.read_text(encoding="utf-8") != content
    )
    assert drifted == []


def test_native_report_must_match_before_it_can_be_split(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "c1-lazarus-p2p-only-native-capture-v1",
                "stages": {
                    "before": {"matched_expected": False},
                    "after": {"matched_expected": True},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="before capture"):
        split_reports(tmp_path, {"report": report})


def test_native_variant_differential_binds_observed_content(tmp_path: Path) -> None:
    spec = FRAME_SPECS["lzp-openra-22476-load-game-faction-filter"]
    captured: dict[str, dict[str, Path]] = {}
    for variant, labels in (("fixed", "Allies,Soviet"), ("buggy", "allies,soviet")):
        report = tmp_path / f"{variant}.json"
        report.write_text(
            json.dumps(
                {
                    "stages": {
                        "after": {
                            "observed_state": {"faction_labels": labels},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        captured[variant] = {"report": report}

    validate_engine_variant_differential(spec=spec, captured=captured)
    payload = json.loads(captured["fixed"]["report"].read_text(encoding="utf-8"))
    assert payload["variant_differential"] == {
        "state_key": "faction_labels",
        "fixed": "Allies,Soviet",
        "buggy": "allies,soviet",
        "matched_expected": True,
    }


def test_native_variant_differential_rejects_identical_order(tmp_path: Path) -> None:
    spec = replace(
        FRAME_SPECS["lzp-openra-22283-tileset-template-order"],
        engine_fixed_state_value="",
        engine_buggy_state_value="",
    )
    captured: dict[str, dict[str, Path]] = {}
    for variant in ("fixed", "buggy"):
        report = tmp_path / f"{variant}.json"
        report.write_text(
            json.dumps(
                {
                    "stages": {
                        "after": {"observed_state": {"template_ids": "1,2,3"}}
                    }
                }
            ),
            encoding="utf-8",
        )
        captured[variant] = {"report": report}
    with pytest.raises(RuntimeError, match="did not distinguish"):
        validate_engine_variant_differential(spec=spec, captured=captured)


def test_openra_catalog_build_class_is_mechanical() -> None:
    for profile in _profiles().values():
        if profile.host == "openra":
            assert (
                classify_build_class(profile.host, profile.production_paths)
                == "incremental_dotnet"
            )


def test_published_luanti_control_is_a_hash_bound_executable_definition() -> None:
    profile = _profiles()["lzp-luanti-16679-unavailable-settings"]
    record = control_test_record(profile)
    definition = UpstreamTestDefinition.from_mapping(record)
    assert definition.timeout_s == 120
    assert definition.argv == tuple(record["command"])
    assert record["upstream_runner_root"] == LUANTI_CONTROL_RUNNER_ROOT.as_posix()
    assert record["upstream_runner_tree_sha256"] == runner_tree_sha256(
        ROOT / LUANTI_CONTROL_RUNNER_ROOT
    )


def test_runtime_cache_manifest_is_bound_to_the_profile_pin(tmp_path: Path) -> None:
    profile = _profiles()["lzp-luanti-16679-unavailable-settings"]
    binary = _write_fake_luanti_binary(tmp_path, profile.pinned_revision)
    _write_runtime_manifest(profile, tmp_path)
    assert _runtime_ready(profile, tmp_path)
    binary.write_text("#!/bin/sh\nprintf 'Luanti stale runtime\\n'\n", encoding="utf-8")
    assert not _runtime_ready(profile, tmp_path)
    _write_fake_luanti_binary(tmp_path, profile.pinned_revision)
    other = replace(profile, pinned_revision="0" * 40)
    assert not _runtime_ready(other, tmp_path)


def test_catalog_publisher_only_skips_explicit_exclusions() -> None:
    profiles = _profiles()
    excluded = profiles["lzp-openra-22325-color-picker-overflow"]
    admitted = profiles["lzp-openra-22476-load-game-faction-filter"]
    invalid = SimpleNamespace(complete=False, failures=("metadata_missing",))
    assert require_publishable_goldens(excluded, invalid) is False
    with pytest.raises(RuntimeError, match="admitted goldens are invalid"):
        require_publishable_goldens(admitted, invalid)


def test_openra_association_failure_marks_mining_incomplete(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("persistent GraphQL outage")

    monkeypatch.setattr(candidate_miner, "gh_graphql", fail)
    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        candidate_miner.associated_pull_request_numbers(
            candidate_miner.HOSTS["openra"], ("0" * 40,), batch_size=1
        )


def test_openra_pull_fetch_failure_marks_mining_incomplete(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("persistent GraphQL outage")

    monkeypatch.setattr(candidate_miner, "gh_graphql", fail)
    with pytest.raises(RuntimeError, match="pull-request coverage is incomplete"):
        candidate_miner.fetch_pull_requests(
            candidate_miner.HOSTS["openra"], (22476,), batch_size=1
        )


def test_openra_association_requires_merge_commit_in_pinned_history() -> None:
    merge_commit = "b" * 40
    node = {
        "number": 22476,
        "title": "Render faction display names",
        "merged": True,
        "mergedAt": "2026-05-24T00:00:00Z",
        "mergeCommit": {"oid": merge_commit},
        "closingIssuesReferences": {
            "nodes": [
                {
                    "number": 22472,
                    "title": "Faction filter uses internal names",
                    "labels": {"nodes": [{"name": "Bug"}]},
                }
            ]
        },
        "files": {
            "nodes": [
                {
                    "path": (
                        "OpenRA.Mods.Common/Widgets/Logic/"
                        "LoadGameBrowserLogic.cs"
                    )
                }
            ]
        },
    }
    candidate = candidate_miner.screen_candidate(
        candidate_miner.HOSTS["openra"],
        node,
        inventory=(),
        issue_cache={},
        pinned_merge_commits=frozenset({"a" * 40}),
    )
    assert candidate is not None
    assert "gate1_merge_commit_not_in_pinned_history" in candidate.rejections

    reachable = candidate_miner.screen_candidate(
        candidate_miner.HOSTS["openra"],
        node,
        inventory=(),
        issue_cache={},
        pinned_merge_commits=frozenset({merge_commit}),
    )
    assert reachable is not None
    assert "gate1_merge_commit_not_in_pinned_history" not in reachable.rejections


def test_luanti_tooltip_waits_for_f12_notice_before_final_capture(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    captures = iter((tmp_path / "baseline.png", tmp_path / "captured.png"))

    def take_screenshot(**kwargs):
        events.append("screenshot")
        return next(captures)

    class Native:
        def move(self, x: int, y: int) -> None:
            events.append(("move", x, y))

    monkeypatch.setattr(engine_capture, "_take_luanti_screenshot", take_screenshot)
    monkeypatch.setattr(
        engine_capture.time,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )
    monkeypatch.setattr(engine_capture, "_image_change_ratio", lambda *_: 0.25)

    captured, ratio, interaction = engine_capture._capture_luanti_tooltip_frame(
        process=object(),
        screenshots=tmp_path,
        native=Native(),
        hover_point=(400, 165),
    )

    assert captured == tmp_path / "captured.png"
    assert ratio == pytest.approx(0.25)
    assert events == [
        "screenshot",
        ("sleep", engine_capture._LUANTI_SCREENSHOT_NOTICE_SETTLE_SECONDS),
        ("move", 400, 165),
        ("sleep", engine_capture._LUANTI_TOOLTIP_HOVER_SETTLE_SECONDS),
        "screenshot",
    ]
    assert interaction["baseline_notice_settle_seconds"] >= 3.0


def test_openra_capture_terminates_the_whole_process_group() -> None:
    process = subprocess.Popen(
        ("/bin/sh", "-c", "sleep 30; :"), start_new_session=True
    )
    _terminate_process_group(process)
    assert process.returncode is not None
    groups = subprocess.run(
        ("ps", "-axo", "pgid="), capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert str(process.pid) not in {value.strip() for value in groups}


def test_luanti_control_runner_ignores_a_stale_verdict(tmp_path: Path) -> None:
    binary = tmp_path / "build-app/out/luanti.app/Contents/MacOS/luanti"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys,time\n"
        "log=pathlib.Path(sys.argv[sys.argv.index('--logfile')+1])\n"
        "time.sleep(0.35)\n"
        "print('Devtest Unit Test Results: PASSED\\n    0 / 66 failed tests.', flush=True)\n"
        "log.write_text('All devtest unit tests passed.\\n', encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    stale = tmp_path / ".p2p-only-control/control-stale"
    stale.mkdir(parents=True)
    (stale / "luanti.log").write_text(
        "All devtest unit tests passed.\n", encoding="utf-8"
    )
    runner = ROOT / LUANTI_CONTROL_RUNNER_ROOT / "run.py"
    started = time.monotonic()
    completed = subprocess.run(
        (
            str(runner),
            "--source-root",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--attempt-id",
            "control",
        ),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert time.monotonic() - started >= 0.3
    payload = json.loads(completed.stdout)
    assert payload["verdict"] == "PASSED"
    assert payload["num_total_tests"] == 66
    assert (stale / "luanti.log").is_file()


def test_luanti_control_runner_rejects_marker_without_numeric_summary(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "build-app/out/luanti.app/Contents/MacOS/luanti"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys,time\n"
        "log=pathlib.Path(sys.argv[sys.argv.index('--logfile')+1])\n"
        "log.write_text('All devtest unit tests passed.\\n', encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    runner = ROOT / LUANTI_CONTROL_RUNNER_ROOT / "run.py"
    completed = subprocess.run(
        (
            str(runner),
            "--source-root",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--attempt-id",
            "missing-summary",
        ),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=10,
    )
    assert completed.returncode == 2
    assert "no valid numeric test summary" in json.loads(completed.stdout)["error"]
