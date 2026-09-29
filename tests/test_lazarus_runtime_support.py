from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from warptest import p5_scale
from warptest.lazarus_catalog import LAZARUS_ARMS
from warptest.lazarus_roster import FORMAL_TASK_COUNT
from warptest.lazarus_runtime_support import (
    LAZARUS_PAID_RUNTIME_BINDINGS,
    LazarusRuntimeBinding,
    lazarus_runtime_support_report,
    paid_runtime_binding,
    paid_runtime_support_failures,
    paid_runtime_unbound_task_ids,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml"
MANIFEST = ROOT / "benchmarks/p5/lazarus_manifest.yaml"

# Formal rows with a provider-backed runtime today.  Every other row is
# delivered by the adapter work this gate guards, so this tuple grows one entry
# per delivered adapter.
SUPPORTED_FORMAL_IDS = (
    "lz-luanti-14757-math-round",
    "lz-luanti-14396-metadata-set-string-nil",
    "lz-openra-21453-hotkey-ready-building",
)


def _catalog_rows() -> list[dict]:
    payload = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return list(payload["tasks"])


def _row(task_id: str) -> dict:
    for row in _catalog_rows():
        if row["id"] == task_id:
            return copy.deepcopy(row)
    raise AssertionError(f"{task_id} is not a catalog row")


def test_every_binding_covers_both_arms_and_a_valid_locus() -> None:
    for task_id, binding in LAZARUS_PAID_RUNTIME_BINDINGS.items():
        assert binding.task_id == task_id
        assert binding.arms == LAZARUS_ARMS
        assert binding.defect_locus in {"at_goal", "on_path"}
        assert binding.adapter


def test_binding_rejects_a_single_arm_registration() -> None:
    with pytest.raises(ValueError, match="both arms"):
        LazarusRuntimeBinding(
            task_id="lz-luanti-example",
            benchmark="luanti",
            defect_locus="at_goal",
            adapter="luanti_live_world_v1",
            arms=("lazarus_state",),
        )


def test_the_formal_catalog_reports_only_the_delivered_rows_as_bound() -> None:
    """The coverage ledger is the source of truth for adapter progress."""

    rows = _catalog_rows()
    report = lazarus_runtime_support_report(rows)

    assert report["schema"] == "c1-lazarus-runtime-support-v1"
    assert report["task_count"] == FORMAL_TASK_COUNT
    assert report["complete"] is False
    assert report["provider_dispatch_permitted"] is False
    bound = tuple(row["task_id"] for row in report["tasks"] if row["bound"])
    assert sorted(bound) == sorted(SUPPORTED_FORMAL_IDS)
    assert len(report["unbound_task_ids"]) == FORMAL_TASK_COUNT - len(bound)


def test_the_formal_openra_row_is_bound_without_naming_its_map() -> None:
    """Naming the fixture is a Phase-0 wording contract, not a runtime fact.

    The formal catalog states the same row observationally ("on the registered
    start"), so requiring the map name here would leave a working adapter
    permanently unbound.
    """

    row = _row("lz-openra-21453-hotkey-ready-building")

    assert "allies-08a" not in row["preconditions"].lower()
    assert paid_runtime_support_failures((row,)) == ()


def test_support_detects_benchmark_and_locus_drift() -> None:
    row = _row("lz-luanti-14757-math-round")
    assert paid_runtime_support_failures((row,)) == ()

    drifted_benchmark = copy.deepcopy(row)
    drifted_benchmark["benchmark"] = "jupyterlab"
    assert paid_runtime_support_failures((drifted_benchmark,)) == (
        "lz-luanti-14757-math-round: benchmark drifted from its runtime binding",
    )

    drifted_locus = copy.deepcopy(row)
    drifted_locus["defect"]["defect_locus"] = "on_path"
    assert paid_runtime_support_failures((drifted_locus,)) == (
        "lz-luanti-14757-math-round: defect_locus drifted from its runtime binding",
    )


def test_support_label_shapes_the_message_only() -> None:
    row = _row("lz-luanti-13132-empty-separator")

    assert paid_runtime_support_failures((row,)) == (
        "lz-luanti-13132-empty-separator: no verified paid runtime for luanti",
    )
    assert paid_runtime_support_failures((row,), label="Phase-0") == (
        "lz-luanti-13132-empty-separator: no verified Phase-0 runtime for luanti",
    )


def test_unbound_ids_preserve_catalog_order() -> None:
    rows = _catalog_rows()
    unbound = paid_runtime_unbound_task_ids(rows)

    catalog_order = [row["id"] for row in rows]
    assert list(unbound) == [
        task_id for task_id in catalog_order if task_id not in SUPPORTED_FORMAL_IDS
    ]


def test_paid_runtime_binding_is_absent_for_an_unknown_task() -> None:
    assert paid_runtime_binding("lz-luanti-not-registered") is None
    assert paid_runtime_binding("") is None


def test_dry_run_reports_the_coverage_gap_instead_of_hiding_it() -> None:
    result = p5_scale.run_p5_scale(ROOT, manifest_path=MANIFEST, dry_run=True)

    support = result["runtime_support"]
    assert result["plan"]["trial_count"] == 360
    assert support["complete"] is False
    assert support["bound_count"] == len(SUPPORTED_FORMAL_IDS)
    assert len(support["unbound_task_ids"]) == FORMAL_TASK_COUNT - len(
        SUPPORTED_FORMAL_IDS
    )


def test_paid_dispatch_is_refused_while_any_row_lacks_a_runtime() -> None:
    """The refusal must happen before the orchestrator, not inside it."""

    with pytest.raises(ValueError, match="executable probe or runtime") as error:
        p5_scale.run_p5_scale(ROOT, manifest_path=MANIFEST)

    message = str(error.value)
    for task_id in paid_runtime_unbound_task_ids(_catalog_rows()):
        assert task_id in message
    for task_id in SUPPORTED_FORMAL_IDS:
        assert task_id not in message
