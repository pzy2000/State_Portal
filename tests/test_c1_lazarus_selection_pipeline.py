from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path

from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import (
    PREANNOTATION_STAGES,
    StageDecision,
    run_preannotation_pipeline,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


class Meter:
    def __init__(self) -> None:
        self.current = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.maximum = max(self.maximum, self.current)

    def leave(self) -> None:
        with self.lock:
            self.current -= 1


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-preannotation/run-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _write_patch(root: Path, candidate: CandidateKey) -> None:
    write_stable_artifact(
        root,
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/v1"
        ),
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
        created_at=f"2026-08-13T00:00:{candidate.change_number:02d}Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "complete": True,
        },
        authorization=_authorization(),
    )


def test_candidate_dag_is_serial_bounded_terminal_and_resumable(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "selection"
    candidates = tuple(CandidateKey("luanti", number) for number in range(1, 7))
    for candidate in candidates:
        _write_patch(root, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )
    test_meter = Meter()
    build_meter = Meter()
    replay_meter = Meter()
    calls: dict[int, list[str]] = defaultdict(list)

    def operation(stage: str, meter: Meter):
        def execute(candidate: CandidateKey) -> StageDecision:
            meter.enter()
            try:
                calls[candidate.change_number].append(stage)
                time.sleep(0.005)
                if stage == "test_repetitions" and candidate.change_number == 3:
                    return StageDecision.terminal("flake_observed")
                if stage == "annotation_eligibility":
                    return StageDecision.eligible()
                return StageDecision.passed({"stage": stage})
            finally:
                meter.leave()

        return execute

    operations = {
        "test_manifest": operation("test_manifest", test_meter),
        "test_repetitions": operation("test_repetitions", test_meter),
        "version_materialization": operation("version_materialization", build_meter),
        "frozen_replay": operation("frozen_replay", replay_meter),
        "hidden_oracle": operation("hidden_oracle", replay_meter),
        "construction_gate": operation("construction_gate", replay_meter),
        "annotation_eligibility": operation("annotation_eligibility", replay_meter),
    }

    first = run_preannotation_pipeline(
        root,
        candidates=candidates,
        operations=operations,
        authorization=_authorization(),
        test_workers=3,
        build_workers=1,
        replay_workers=2,
        persistence_workers=2,
        reload_workers=2,
        queue_limit=16,
    )
    second = run_preannotation_pipeline(
        root,
        candidates=candidates,
        operations={
            stage: lambda _candidate: (_ for _ in ()).throw(
                AssertionError("completed DAG stage was rerun")
            )
            for stage in PREANNOTATION_STAGES
        },
        authorization=_authorization(),
    )

    assert first.eligible == 5
    assert first.terminal_counts == {"flake_observed": 1}
    assert first.paused_for_disk == 0
    assert second.resumed == 6
    assert test_meter.maximum <= 3
    assert build_meter.maximum <= 1
    assert replay_meter.maximum <= 2
    assert calls[3] == ["test_manifest", "test_repetitions"]
    expected = list(PREANNOTATION_STAGES)
    for candidate in candidates:
        if candidate.change_number != 3:
            assert calls[candidate.change_number] == expected
    checkpoint = load_stable_artifact(
        root,
        artifact_id="preannotation_checkpoint/luanti/1/current/v1",
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
    )
    assert checkpoint.payload["state"] == "eligible"
    assert checkpoint.payload["completed_stages"] == expected


def test_candidate_dag_pauses_materialization_below_build_watermark(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("openra", 4)
    _write_patch(root, candidate)
    operations = {
        stage: (lambda _candidate: StageDecision.passed({}))
        for stage in PREANNOTATION_STAGES
    }
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": False,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )

    result = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations=operations,
        authorization=_authorization(),
    )

    assert result.paused_for_disk == 1
    assert result.eligible == 0
    checkpoint = load_stable_artifact(
        root,
        artifact_id="preannotation_checkpoint/openra/4/current/v1",
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
    )
    assert checkpoint.payload["completed_stages"] == [
        "test_manifest",
        "test_repetitions",
    ]
    assert checkpoint.payload["state"] == "active"


def test_terminal_stage_recursively_binds_typed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 7)
    _write_patch(root, candidate)
    evidence_id = "candidate_test_attempt/luanti/7/fixed/1/v1"
    write_stable_artifact(
        root,
        artifact_id=evidence_id,
        artifact_kind="candidate_test_attempt",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:08Z",
        inputs=(),
        payload={"candidate": candidate.to_value(), "complete": True},
        authorization=_authorization(),
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )
    evidence = {
        "artifact_id": evidence_id,
        "artifact_kind": "candidate_test_attempt",
        "semantic_version": "1.0",
    }
    operations = {
        stage: lambda _candidate: StageDecision.passed({})
        for stage in PREANNOTATION_STAGES
    }
    operations["test_manifest"] = lambda _candidate: StageDecision.terminal(
        "quality_rejected", {"evidence_artifacts": [evidence]}
    )

    result = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations=operations,
        authorization=_authorization(),
    )

    assert result.terminal_counts == {"quality_rejected": 1}
    patch = load_stable_artifact(
        root,
        artifact_id="candidate_test_patch/luanti/7/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
    )
    attempt = load_stable_artifact(
        root,
        artifact_id=evidence_id,
        artifact_kind="candidate_test_attempt",
        semantic_version="1.0",
    )
    stage = load_stable_artifact(
        root,
        artifact_id="candidate_stage/luanti/7/test_manifest/v1",
        artifact_kind="candidate_stage",
        semantic_version="1.0",
    )
    assert stage.inputs == (patch.reference, attempt.reference)


def test_candidate_dispatch_never_queues_more_than_queue_limit(
    tmp_path: Path, monkeypatch
) -> None:
    """The outer scheduler must not eagerly enqueue a whole repository."""

    root = tmp_path / "selection"
    candidates = tuple(CandidateKey("luanti", number) for number in range(1, 13))
    for candidate in candidates:
        _write_patch(root, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )

    class TrackingExecutor(_ThreadPoolExecutor):
        maximum_outstanding = 0
        outstanding = 0
        lock = threading.Lock()

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            with self.lock:
                type(self).outstanding += 1
                type(self).maximum_outstanding = max(
                    type(self).maximum_outstanding,
                    type(self).outstanding,
                )

            def completed(_future) -> None:
                with self.lock:
                    type(self).outstanding -= 1

            future.add_done_callback(completed)
            return future

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.ThreadPoolExecutor",
        TrackingExecutor,
    )
    release = threading.Event()
    started = threading.Event()
    start_count = 0
    start_lock = threading.Lock()

    def blocked_manifest(_candidate: CandidateKey) -> StageDecision:
        nonlocal start_count
        with start_lock:
            start_count += 1
            if start_count == 3:
                started.set()
        assert release.wait(timeout=10)
        return StageDecision.terminal("quality_rejected")

    operations = {
        stage: (
            blocked_manifest
            if stage == "test_manifest"
            else lambda _candidate: StageDecision.passed({})
        )
        for stage in PREANNOTATION_STAGES
    }
    captured: list[object] = []

    def run() -> None:
        captured.append(
            run_preannotation_pipeline(
                root,
                candidates=candidates,
                operations=operations,
                authorization=_authorization(),
                test_workers=3,
                build_workers=1,
                replay_workers=1,
                persistence_workers=1,
                reload_workers=1,
                queue_limit=3,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=10)
    assert TrackingExecutor.maximum_outstanding <= 3
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert captured[0].terminal_counts == {"quality_rejected": len(candidates)}
