from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from warptest.c1_lazarus_selection import c1_lazarus_golden_publication as target
from warptest.c1_lazarus_selection import c1_lazarus_task_goldens_manifest as manifest
from warptest.c1_lazarus_selection.c1_lazarus_task_text import task_text
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CHANGE = 19227
_TASK_ID = "lz-jupyterlab-19227-reactive-toolbar-positions"


def _authorization(scope: str = "infrastructure-audit") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/c1-lazarus-goldens/{scope}",
        scope=scope,
        granted_at="2026-08-15T05:00:00Z",
        local_user_id=os.getuid(),
    )


def _frame(path: Path, label: str, *, shift: int = 0) -> None:
    """Write a frame rich enough to pass the publisher's capture-quality gate."""

    path.parent.mkdir(parents=True, exist_ok=True)
    generator = random.Random(20260815)
    image = Image.new("RGB", (400, 300), "white")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (
                generator.randrange(256),
                generator.randrange(256),
                generator.randrange(256),
            )
    draw = ImageDraw.Draw(image)
    draw.rectangle((10 + shift, 40, 190 + shift, 240), fill="black")
    draw.text((20 + shift, 120), label, fill="white")
    image.save(path)


def _seed(
    root: Path,
    artifact_id: str,
    kind: str,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...] = (),
) -> ArtifactReference:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=kind,
        semantic_version="1.0",
        created_at="2026-08-15T04:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )
    return ArtifactReference(artifact_id, kind, "1.0")


def _fixture(root: Path) -> None:
    observations = []
    for variant, shift in (("fixed", 0), ("buggy", 40)):
        frames = {
            "before": f"private/runs/{variant}/before.png",
            "after": f"private/runs/{variant}/after.png",
            "fixed_golden": f"private/runs/{variant}/fixed-golden.png",
            "buggy_golden": f"private/runs/{variant}/buggy-golden.png",
        }
        _frame(root / frames["before"], "start")
        _frame(root / frames["after"], "after", shift=shift)
        _frame(root / frames["fixed_golden"], "golden")
        _frame(root / frames["buggy_golden"], "golden", shift=90)
        observations.append(
            _seed(
                root,
                f"candidate_gate11_edge_observation/jupyterlab/{_CHANGE}/{variant}/v1",
                "candidate_gate11_edge_observation",
                {
                    "variant": variant,
                    "state": "passed",
                    "complete": True,
                    "frames": frames,
                },
            )
        )
    oracle = _seed(
        root,
        f"candidate_gate10_hidden_oracle/jupyterlab/{_CHANGE}/v1",
        "candidate_gate10_hidden_oracle",
        {
            "state": "passed",
            "complete": True,
            "authoritative_defect": "the toolbar keeps a stale position map",
            "expected_after": {"fixed": "ordered", "buggy": "misordered"},
        },
    )
    _seed(
        root,
        f"candidate_formal_freeze_admission/jupyterlab/{_CHANGE}/v1",
        "candidate_formal_freeze_admission",
        {
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "primary_bug_issue": 19226,
            "f2p_full_name": "toolbar f2p",
            "p2p_full_name": "toolbar p2p",
        },
        inputs=(*observations, oracle),
    )


def _roster(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "candidates": [
                    {
                        "repository_key": "jupyterlab",
                        "change_number": _CHANGE,
                        "defect_locus": "at_goal",
                        "status": "replacement_gate1_7_passed_downstream_pending",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_resolver_picks_captured_frames_and_skips_constructed_goldens(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)

    resolved = target.resolve_candidate_frames(
        tmp_path, repository_key="jupyterlab", change_number=_CHANGE
    )

    assert resolved.task_id == _TASK_ID
    assert set(resolved.frames) == {
        "fixed_before",
        "fixed_after",
        "buggy_before",
        "buggy_after",
    }
    assert all("golden" not in path.name for path in resolved.frames.values())
    assert resolved.frame("fixed_after").name == "after.png"
    assert "/fixed/" in resolved.sources["fixed_after"]


def test_publication_writes_the_triplet_and_records_metrics(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts-root"
    artifacts.mkdir()
    _fixture(artifacts)
    repo = tmp_path / "repo"
    (repo / "specs/jupyterlab/lazarus").mkdir(parents=True)
    roster = _roster(tmp_path / "roster.yaml")

    record = target.publish_task_goldens(
        artifacts,
        roster_path=roster,
        repository_root=repo,
        authorization=_authorization(),
    )

    assert record.payload["published_task_count"] == 1
    assert record.payload["constructed_oracle_frames_published"] is False
    published = repo / "specs/jupyterlab/lazarus" / f"visual_{_TASK_ID}"
    assert sorted(path.name for path in published.iterdir()) == [
        "before.golden.png",
        "buggy.after.golden.png",
        "buggy.before.golden.png",
        "fixed.after.golden.png",
        "goldens.meta.json",
    ]
    metadata = json.loads((published / "goldens.meta.json").read_text())
    assert metadata["task_id"] == _TASK_ID
    assert metadata["before_equivalence"]["changed_pixel_ratio"] == 0.0
    assert metadata["after_differential"]["changed_pixel_ratio"] > 0.001


def test_manifest_carries_provenance_rubrics_and_runtime_status(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts-root"
    artifacts.mkdir()
    _fixture(artifacts)
    repo = tmp_path / "repo"
    (repo / "specs/jupyterlab/lazarus").mkdir(parents=True)
    roster = _roster(tmp_path / "roster.yaml")
    target.publish_task_goldens(
        artifacts,
        roster_path=roster,
        repository_root=repo,
        authorization=_authorization(),
    )

    record = manifest.write_task_manifests(
        artifacts,
        roster_path=roster,
        repository_root=repo,
        authorization=_authorization(),
    )

    assert record.payload["task_count"] == 1
    path = repo / "specs/jupyterlab/lazarus" / f"{_TASK_ID}.goldens.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["schema"] == manifest.MANIFEST_SCHEMA
    assert document["candidate"]["issue_number"] == 19226
    goldens = document["evaluation"]["lazarus"]["goldens"]
    assert goldens["before"].endswith("/before.golden.png")
    assert goldens["buggy_before"].endswith("/buggy.before.golden.png")
    assert goldens["fixed"]["after"].endswith("fixed.after.golden.png")
    assert goldens["buggy"]["after"].endswith("buggy.after.golden.png")
    # The task is registered in the authored text table, so the writer emits the
    # frame descriptions a visual judge can check instead of a semantic summary.
    authored = task_text(_TASK_ID)
    assert authored is not None
    visual_oracle = document["evaluation"]["visual_oracle"]
    assert visual_oracle["description"] == authored.description("before")
    assert goldens["fixed"]["after_description"] == authored.description("fixed")
    assert goldens["buggy"]["after_description"] == authored.description("buggy")
    assert document["evaluation"]["lazarus"]["upstream_tests"] == {
        "fail_to_pass": ["toolbar f2p"],
        "pass_to_pass": ["toolbar p2p"],
    }
    # jupyterlab has no provider-backed Phase-0 pilot runtime, so a bound task
    # claims only the deterministic probe and still keeps its checks in the spec.
    lazarus = document["evaluation"]["lazarus"]
    assert lazarus["runtime_status"] == "probe_runtime_available"
    assert lazarus["probe"]["adapter"] == "jupyterlab_lazarus_edge_probe_v1"
    assert lazarus["probe"]["provider_calls_required"] is False
    assert lazarus["probe"]["versions_replayed"] == ["fixed", "buggy"]
    assert "goal_checks" not in lazarus


def test_missing_frames_and_wrong_scope_fail_closed(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts-root"
    artifacts.mkdir()
    _fixture(artifacts)
    (artifacts / "private/runs/buggy/after.png").unlink()
    with pytest.raises(RuntimeError, match="no captured frame"):
        target.resolve_candidate_frames(
            artifacts, repository_key="jupyterlab", change_number=_CHANGE
        )
    roster = _roster(tmp_path / "roster.yaml")
    with pytest.raises(PermissionError):
        target.publish_task_goldens(
            artifacts,
            roster_path=roster,
            repository_root=tmp_path,
            authorization=_authorization("preannotation-execution"),
        )
