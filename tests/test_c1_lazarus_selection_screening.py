from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

from warptest.c1_lazarus_selection.screening import (
    PatchDecision,
    ScreeningBatchResult,
    SourceDecision,
    run_screening_batch,
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


def test_screening_loads_enumeration_once_bounds_workers_and_resumes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/run-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id="repository_enumeration/luanti/current/v1",
        artifact_kind="repository_enumeration",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={
            "repository_key": "luanti",
            "candidates": [
                {"repository_key": "luanti", "change_number": number}
                for number in range(1, 21)
            ],
            "complete": True,
        },
        authorization=authorization,
    )
    source_meter = Meter()
    patch_meter = Meter()
    calls: Counter[tuple[str, int]] = Counter()

    def source(candidate):
        calls["source", candidate.change_number] += 1
        source_meter.enter()
        try:
            time.sleep(0.01)
            return SourceDecision.accepted(
                raw_evidence_path=f"raw/source/luanti/{candidate.change_number}.json"
            )
        finally:
            source_meter.leave()

    def patch(candidate, _source):
        calls["patch", candidate.change_number] += 1
        patch_meter.enter()
        try:
            time.sleep(0.01)
            if candidate.change_number % 2:
                return PatchDecision.rejected("no_regression_test")
            return PatchDecision.eligible(
                production_paths=("src/main.cpp",), test_paths=("tests/main.cpp",)
            )
        finally:
            patch_meter.leave()

    first = run_screening_batch(
        root,
        repository_key="luanti",
        source_capture=source,
        patch_capture=patch,
        authorization=authorization,
        source_workers=8,
        patch_workers=4,
        persistence_workers=2,
        reload_workers=2,
        queue_limit=16,
    )
    second = run_screening_batch(
        root,
        repository_key="luanti",
        source_capture=lambda _candidate: (_ for _ in ()).throw(
            AssertionError("completed candidate was recaptured")
        ),
        patch_capture=lambda _candidate, _source: (_ for _ in ()).throw(
            AssertionError("completed candidate was repatched")
        ),
        authorization=authorization,
        source_workers=8,
        patch_workers=4,
        persistence_workers=2,
        reload_workers=2,
        queue_limit=16,
    )

    assert first.total_candidates == 20
    assert first.scheduled_candidates == 20
    assert first.patch_eligible == 10
    assert first.patch_rejected == 10
    assert first.enumeration_loads == 1
    assert first.max_in_flight <= 16
    assert source_meter.maximum <= 8
    assert patch_meter.maximum <= 4
    assert second.scheduled_candidates == 0
    assert second.resumed_candidates == 20
    assert second.enumeration_loads == 1
    assert sum(calls.values()) == 40
    checkpoint = load_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/2/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
    )
    assert checkpoint.payload["state"] == "complete"
    assert checkpoint.payload["outcome"] == "patch_eligible"


def test_screening_batch_limit_runs_first_unresolved_candidates_and_resumes(
    tmp_path: Path,
) -> None:
    """A bounded invocation is a prefix of unresolved work, never a skip."""

    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/bounded-run",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id="repository_enumeration/luanti/current/v1",
        artifact_kind="repository_enumeration",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={
            "repository_key": "luanti",
            "candidates": [
                {"repository_key": "luanti", "change_number": number}
                for number in range(1, 6)
            ],
            "complete": True,
        },
        authorization=authorization,
    )
    captured: list[int] = []

    def source(candidate):
        captured.append(candidate.change_number)
        return SourceDecision.rejected("authoritative_issue_link_missing")

    def run(limit: int | None):
        return run_screening_batch(
            root,
            repository_key="luanti",
            source_capture=source,
            patch_capture=lambda _candidate, _source: (_ for _ in ()).throw(
                AssertionError("rejected sources must not reach patch screening")
            ),
            authorization=authorization,
            source_workers=1,
            patch_workers=1,
            persistence_workers=1,
            reload_workers=1,
            queue_limit=1,
            max_candidates=limit,
        )

    first = run(2)
    assert captured == [1, 2]
    assert first.scheduled_candidates == 2
    assert first.resumed_candidates == 0
    assert first.remaining_candidates == 3
    assert first.paused_for_disk is False

    second = run(2)
    assert captured == [1, 2, 3, 4]
    assert second.scheduled_candidates == 2
    assert second.resumed_candidates == 2
    assert second.remaining_candidates == 1
    assert second.paused_for_disk is False

    final = run(None)
    assert captured == [1, 2, 3, 4, 5]
    assert final.scheduled_candidates == 1
    assert final.resumed_candidates == 4
    assert final.remaining_candidates == 0
    assert final.source_rejected == 5


@pytest.mark.parametrize("limit", (0, 10_001, True, "25"))
def test_screening_batch_limit_rejects_invalid_values(
    tmp_path: Path,
    limit: object,
) -> None:
    authorization = AuthorizationRecord(
        authorization_id="local-screening/invalid-limit",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    with pytest.raises(ValueError, match="max_candidates"):
        run_screening_batch(
            tmp_path / "selection",
            repository_key="luanti",
            source_capture=lambda _candidate: SourceDecision.rejected("unused"),
            patch_capture=lambda _candidate, _source: PatchDecision.rejected("unused"),
            authorization=authorization,
            max_candidates=limit,  # type: ignore[arg-type]
        )


def test_screening_cli_forwards_a_bounded_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_screening_cli()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_operations",
        lambda *_args, **_kwargs: (
            lambda _candidate: SourceDecision.rejected("unused"),
            lambda _candidate, _source: PatchDecision.rejected("unused"),
        ),
    )

    def batch(*_args: object, **kwargs: object) -> ScreeningBatchResult:
        captured.update(kwargs)
        return ScreeningBatchResult(
            repository_key="luanti",
            total_candidates=5,
            scheduled_candidates=2,
            resumed_candidates=0,
            source_rejected=2,
            patch_rejected=0,
            patch_eligible=0,
            infrastructure_blocked=0,
            enumeration_loads=1,
            max_in_flight=1,
            paused_for_disk=False,
            remaining_candidates=3,
        )

    monkeypatch.setattr(module, "run_screening_batch", batch)
    assert (
        module.main(
            [
                "--artifact-root",
                str(tmp_path / "selection"),
                "--repository",
                "luanti",
                "--authorization-id",
                "local-screening/cli-limit",
                "--execute",
                "--max-candidates",
                "2",
            ]
        )
        == 0
    )
    assert captured["max_candidates"] == 2


def _load_screening_cli() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / ("run_c1_lazarus_selection_screening.py")
    )
    specification = importlib.util.spec_from_file_location(
        "c1_lazarus_selection_screening_cli_test", script
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
