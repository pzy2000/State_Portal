from __future__ import annotations

import hashlib
from itertools import combinations
from pathlib import Path

import pytest
import yaml

from scripts import apply_c1_lazarus_task_text as applier
from warptest.c1_lazarus_selection import c1_lazarus_formal_catalog as catalog_writer
from warptest.c1_lazarus_selection.c1_lazarus_task_text import FRAMES, TASK_TEXT
from warptest.lazarus_catalog import validate_lazarus_catalog
from warptest.lazarus_expansion_specs import EXPANSION_ROWS_BY_ID
from warptest.lazarus_roster import FORMAL_TASK_COUNT
from warptest.task_descriptions import canonical_state_description


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml"
# The wording these tasks used to carry named an intention rather than anything
# a visual judge can see in the frame.
_ABSTRACT_PHRASES = (
    "the goal state of the authoritative fix",
    "the registered defect signature",
    "is visible.",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def rows() -> dict[str, dict[str, object]]:
    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    return {str(row["id"]): row for row in catalog["tasks"]}


def _specs(task_id: str, rows: dict[str, dict[str, object]]) -> list[Path]:
    spec_path = ROOT / str(rows[task_id]["oracle_spec"])
    return [spec_path, spec_path.with_name(f"{task_id}.goldens.yaml")]


def test_every_frozen_task_carries_authored_wording(
    rows: dict[str, dict[str, object]],
) -> None:
    # TASK_TEXT is the freeze sprint's wording registry, so it covers exactly
    # the frozen rows.  The expansion rows carry their wording in their own
    # registry, and together the two must account for every published row.
    assert set(TASK_TEXT) == set(catalog_writer._PUBLIC)
    assert set(TASK_TEXT).isdisjoint(EXPANSION_ROWS_BY_ID)
    assert set(TASK_TEXT) | set(EXPANSION_ROWS_BY_ID) == set(rows)


def test_registered_specs_describe_frames_instead_of_intentions(
    rows: dict[str, dict[str, object]],
) -> None:
    for task_id, text in TASK_TEXT.items():
        for path in _specs(task_id, rows):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            evaluation = document["evaluation"]
            visual = evaluation["visual_oracle"]
            goldens = evaluation["lazarus"]["goldens"]

            # The registered visual oracle grades the before frame.
            assert visual["description"] == text.description("before")
            assert visual["rubric"] == text.rubric("before")
            for frame in ("fixed", "buggy"):
                branch = goldens[frame]
                assert branch["after_description"] == text.description(frame)
                assert branch["after_rubric"] == text.rubric(frame)

            rendered = path.read_text(encoding="utf-8")
            for phrase in _ABSTRACT_PHRASES:
                assert phrase not in rendered, (task_id, path.name, phrase)


def test_each_frame_gets_its_own_must_line() -> None:
    for task_id, text in TASK_TEXT.items():
        for frame in FRAMES:
            description = text.description(frame)
            assert text.rubric(frame)["must"] == [text.scene, description], (
                task_id,
                frame,
            )
            assert len(description.split()) >= 6, (task_id, frame)
            assert text.rubric(frame)["must_not"], (task_id, frame)
            assert text.rubric(frame)["ignore"], (task_id, frame)


def test_descriptions_match_exactly_when_the_frames_are_identical(
    rows: dict[str, dict[str, object]],
) -> None:
    """Two frames may share wording only when they are the same picture."""

    for task_id, text in TASK_TEXT.items():
        visual = (ROOT / str(rows[task_id]["oracle_spec"])).parent / f"visual_{task_id}"
        digests = {
            "before": _sha256(visual / "before.golden.png"),
            "fixed": _sha256(visual / "fixed.after.golden.png"),
            "buggy": _sha256(visual / "buggy.after.golden.png"),
        }
        assert digests["fixed"] != digests["buggy"], task_id
        for first, second in combinations(FRAMES, 2):
            same_frame = digests[first] == digests[second]
            same_text = text.description(first) == text.description(second)
            assert same_frame == same_text, (task_id, first, second)


def test_public_rows_carry_the_authored_wording(
    rows: dict[str, dict[str, object]],
) -> None:
    for task_id, text in TASK_TEXT.items():
        row = rows[task_id]
        assert row["preconditions"] == text.preconditions
        assert row["reference_steps"] == text.reference_steps
        assert row["state_description"] == canonical_state_description(
            text.preconditions, text.reference_steps
        )


def test_registered_visual_oracle_grades_the_before_frame(
    rows: dict[str, dict[str, object]],
) -> None:
    """judge_lazarus_visual_evidence reads visual_oracle as the before rubric."""

    for task_id, text in TASK_TEXT.items():
        for path in _specs(task_id, rows):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            visual = document["evaluation"]["visual_oracle"]
            goldens = document["evaluation"]["lazarus"]["goldens"]
            before_line = text.description("before")

            assert visual["description"] == before_line, (task_id, path.name)
            assert visual["rubric"]["must"][-1] == before_line, (task_id, path.name)
            for frame in ("fixed", "buggy"):
                if text.description(frame) != before_line:
                    assert (
                        goldens[frame]["after_description"] != visual["description"]
                    ), (task_id, path.name, frame)


def test_catalog_still_passes_the_strict_validator() -> None:
    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    _, tasks = validate_lazarus_catalog(catalog, source_path=CATALOG_PATH)

    assert len(tasks) == FORMAL_TASK_COUNT


def test_tracked_files_match_the_registry() -> None:
    assert applier.main(["--check"]) == 0
