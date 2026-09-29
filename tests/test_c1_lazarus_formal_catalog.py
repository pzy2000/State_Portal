from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

from warptest.c1_lazarus_selection import c1_lazarus_formal_catalog as target
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord
from warptest.lazarus_catalog import validate_lazarus_catalog
from warptest.lazarus_expansion_specs import EXPANSION_ROWS_BY_ID
from warptest.lazarus_mining import validate_upstream_record
from warptest.lazarus_roster import (
    FORMAL_LOCUS_MIX,
    FROZEN_SUBSET_LOCUS_MIX,
    FROZEN_SUBSET_TASK_COUNT,
    FORMAL_TASK_COUNT,
)
from warptest.task_descriptions import canonical_state_description


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPOSITORY_ROOT / target.FORMAL_CATALOG_PATH
PHASE0_PATH = REPOSITORY_ROOT / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml"


@pytest.fixture(scope="module")
def catalog() -> dict[str, object]:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(catalog: dict[str, object]) -> list[dict[str, object]]:
    return list(catalog["tasks"])


def _evidence(row: dict[str, object]) -> Path:
    return REPOSITORY_ROOT / str(row["defect"]["revert_patch"])


def _is_expansion(row: dict[str, object]) -> bool:
    """Whether a row was established by a live run rather than the freeze sprint."""

    return str(row["id"]) in EXPANSION_ROWS_BY_ID


def test_formal_catalog_passes_the_strict_validator(catalog: dict[str, object]) -> None:
    hosts, tasks = validate_lazarus_catalog(catalog, source_path=CATALOG_PATH)
    assert len(tasks) == FORMAL_TASK_COUNT
    assert tuple(hosts) == ("luanti", "jupyterlab", "openra")


def test_phase0_catalog_still_validates() -> None:
    payload = yaml.safe_load(PHASE0_PATH.read_text(encoding="utf-8"))
    _, tasks = validate_lazarus_catalog(payload, source_path=PHASE0_PATH)
    assert len(tasks) == 2


def test_population_matches_the_frozen_roster(rows: list[dict[str, object]]) -> None:
    frozen = [row for row in rows if not _is_expansion(row)]
    roster = yaml.safe_load(
        (REPOSITORY_ROOT / "benchmarks/p5/lazarus/c1_freeze_sprint_v2_roster.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert roster["selection_status"] == "frozen"
    locus: dict[str, int] = {}
    benchmarks: dict[str, int] = {}
    for row in frozen:
        locus[row["defect"]["defect_locus"]] = locus.get(row["defect"]["defect_locus"], 0) + 1
        benchmarks[row["benchmark"]] = benchmarks.get(row["benchmark"], 0) + 1
    # The frozen population is an invariant: the expansion appends rows beside
    # it and must never restate or reshape it.
    assert locus == FROZEN_SUBSET_LOCUS_MIX
    assert benchmarks == {"luanti": 13, "jupyterlab": 11}
    assert len({row["id"] for row in frozen}) == FROZEN_SUBSET_TASK_COUNT
    full_locus: dict[str, int] = {}
    for row in rows:
        key = str(row["defect"]["defect_locus"])
        full_locus[key] = full_locus.get(key, 0) + 1
    assert full_locus == FORMAL_LOCUS_MIX
    assert len({row["id"] for row in rows}) == FORMAL_TASK_COUNT


def test_public_rows_state_the_canonical_description(rows: list[dict[str, object]]) -> None:
    for row in rows:
        assert row["budget_ms"] == 2_400_000
        assert row["prompt_revision"] == target.PROMPT_REVISION
        assert row["horizon_class"] in {"short", "mid", "long"}
        assert "goal_description" not in row
        assert row["state_description"] == canonical_state_description(
            str(row["preconditions"]), str(row["reference_steps"])
        )
        assert {"c1", "lazarus", row["benchmark"]} <= set(row["tags"])
        assert row["capabilities"]
        assert (REPOSITORY_ROOT / str(row["oracle_spec"])).is_file()


def test_declared_patch_hash_matches_the_written_patch(rows: list[dict[str, object]]) -> None:
    for row in rows:
        patch = _evidence(row)
        assert patch.is_file()
        assert row["defect"]["patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()


def test_every_upstream_record_is_valid_and_covers_the_oracle(
    rows: list[dict[str, object]],
) -> None:
    for row in rows:
        record = json.loads(
            (REPOSITORY_ROOT / str(row["defect"]["upstream_record"])).read_text(
                encoding="utf-8"
            )
        )
        assert validate_upstream_record(record) == []
        spec = yaml.safe_load(
            (REPOSITORY_ROOT / str(row["oracle_spec"])).read_text(encoding="utf-8")
        )
        declared = {
            value
            for values in spec["evaluation"]["lazarus"]["upstream_tests"].values()
            for value in values
        }
        known = {str(entry["id"]) for entry in record["tests"]}
        assert declared <= known
        kinds = {str(entry["kind"]) for entry in record["tests"]}
        assert {"fail_to_pass", "pass_to_pass"} <= kinds


def test_gate_reports_are_passed_and_declare_their_provenance(
    rows: list[dict[str, object]],
) -> None:
    """Every row must pass its gate and say how that gate was established.

    The two provenances are checked separately and each is forbidden the other's
    claim: a frozen row must be a projection of the freeze sprint's verdicts, and
    an expansion row must be a live run that never borrows an admission id.
    """

    for row in rows:
        report = json.loads(
            (REPOSITORY_ROOT / str(row["defect"]["gate_report"])).read_text(encoding="utf-8")
        )
        assert report["schema_version"] == 1
        assert report["task_id"] == row["id"]
        assert report["status"] == "passed"
        assert report["failures"] == []
        assert report["checks"]["buggy_f2p"] is False
        assert report["checks"]["fixed_f2p"] is True
        assert report["paid_actions_authorized"] is False
        assert report["formal_trials_authorized"] is False
        if _is_expansion(row):
            assert report["derived_from_frozen_artifacts"] is False
            assert report["projection"]["fresh_gate_run"] is True
            assert report["projection"]["admission"] is None
            assert "source_artifacts" not in report["evidence"]
            assert report["evidence"]["upstream"]["receipt"]
            assert report["evidence"]["goldens"]["metadata_sha256"]
        else:
            assert report["derived_from_frozen_artifacts"] is True
            assert report["projection"]["fresh_gate_run"] is False
            assert report["projection"]["admission"]
            assert report["evidence"]["source_artifacts"]["strict_gate_1_7"]


def test_inverse_patches_are_production_only(rows: list[dict[str, object]]) -> None:
    reused = set(target._REUSED_PATCHES)
    for row in rows:
        if row["id"] in reused:
            continue
        patch = _evidence(row).read_text(encoding="utf-8", errors="replace")
        paths = [
            line[len("+++ b/") :]
            for line in patch.splitlines()
            if line.startswith("+++ b/")
        ]
        assert paths
        assert not [
            value for value in paths if target._is_test_path(PurePosixPath(value))
        ]


def test_pilot_evidence_directories_are_untouched(rows: list[dict[str, object]]) -> None:
    for row in rows:
        assert PurePosixPath(str(row["defect"]["revert_patch"])).parent.name == (
            target.EVIDENCE_EPOCH
        )


def test_reused_pilot_patch_still_reverse_applies() -> None:
    for task_id, relative in target._REUSED_PATCHES.items():
        patch = (REPOSITORY_ROOT / relative).read_bytes()
        result = subprocess.run(
            ("git", "apply", "--reverse", "--check", "-"),
            cwd=REPOSITORY_ROOT / "games/luanti",
            input=patch,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (task_id, result.stderr.decode()[:200])


def test_public_contracts_cover_every_frozen_task() -> None:
    rows = target.frozen_task_rows()
    assert len(rows) == 24
    assert {task_id for task_id, _, _ in rows} == set(target._PUBLIC)


def test_test_path_heuristics_separate_production_from_tests() -> None:
    assert target._is_test_path(PurePosixPath("src/unittest/test_map.cpp")) is True
    assert target._is_test_path(PurePosixPath("packages/a/test/b.spec.ts")) is True
    assert target._is_test_path(PurePosixPath("builtin/common/tests/x_spec.lua")) is True
    assert target._is_test_path(PurePosixPath("src/collision.cpp")) is False
    assert target._is_test_path(PurePosixPath("packages/a/src/toolbar.tsx")) is False


def test_native_module_and_snake_round_trip() -> None:
    assert target._native_module("TestMoveAction.testMoveFillStack", "x.cpp") == (
        "TestMoveAction"
    )
    assert target._native_module("test translations", "src/unittest/test_translations.cpp") == (
        "TestTranslations"
    )
    assert target._snake("TestMapSettingsManager") == "map_settings_manager"


def test_writer_requires_an_infrastructure_audit_authorization(tmp_path: Path) -> None:
    authorization = AuthorizationRecord(
        authorization_id="test/formal-catalog/wrong-scope",
        scope="preannotation-execution",
        granted_at="2026-08-15T10:00:00Z",
        local_user_id=os.getuid(),
    )
    with pytest.raises(PermissionError):
        target.write_formal_catalog(
            tmp_path,
            repository_root=REPOSITORY_ROOT,
            roster_path=REPOSITORY_ROOT
            / "benchmarks/p5/lazarus/c1_freeze_sprint_v2_roster.yaml",
            authorization=authorization,
        )
