from pathlib import Path

from warptest.c1_lazarus_selection.curated import load_curated_catalog
from warptest.c1_lazarus_selection.luanti_14757_curated_recovery import (
    _LUA_TRANSITION,
    _load,
    _load_source_attempts,
)


def test_catalog_replaces_terminal_14586_with_14757() -> None:
    root = Path.cwd()
    tasks = load_curated_catalog(
        root / "benchmarks/p5/lazarus/curated.yaml",
        workspace_root=root,
    )
    assert len(tasks) == 24
    assert sum(task.defect_locus == "at_goal" for task in tasks) == 16
    assert sum(task.defect_locus == "on_path" for task in tasks) == 8
    assert sum(task.repository_key == "luanti" for task in tasks) == 4
    assert any(task.change_number == 14757 for task in tasks)
    assert not any(task.change_number == 14586 for task in tasks)


def test_existing_fresh_epoch_has_three_distinct_truth_table_repetitions() -> None:
    root = Path(".warptest-cache/c1_lazarus_selection_v1").resolve(strict=True)
    transition = _load(root, _LUA_TRANSITION)
    repetitions = _load_source_attempts(root, transition)
    assert set(repetitions) == {1, 2, 3}
    assert all(len(attempts) == 4 for attempts in repetitions.values())
    assert (
        len(
            {
                attempt.reference.artifact_id
                for attempts in repetitions.values()
                for attempt in attempts
            }
        )
        == 12
    )
