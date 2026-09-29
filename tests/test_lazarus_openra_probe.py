from __future__ import annotations

from pathlib import Path

import pytest

from warptest.lazarus_openra_probe import (
    OPENRA_PHASE0_TASK_ID,
    evaluate_openra_pr21906_audit,
    openra_pr21906_probe,
    prepare_openra_pr21906_runtime_overlay,
)
from warptest.spec_schema import validate_spec_schema


ROOT = Path(__file__).resolve().parents[1]


def _overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "openra"
    rules = root / "mods/cnc/maps/gdi01/rules.yaml"
    rules.parent.mkdir(parents=True)
    source = ROOT / "games/OpenRA/mods/cnc/maps/gdi01/rules.yaml"
    rules.write_bytes(source.read_bytes())
    (root / "OpenRA.Mods.Common/Traits/World").mkdir(parents=True)
    return root


def test_openra_phase0_probe_uses_the_live_gameplay_adapter_contract() -> None:
    before = openra_pr21906_probe("before")
    after = openra_pr21906_probe("after")

    assert OPENRA_PHASE0_TASK_ID == "lz-openra-21906-production-tabs"
    assert validate_spec_schema(before, "openra-before.yaml") == []
    assert validate_spec_schema(after, "openra-after.yaml") == []
    assert before["actions"] == []
    assert after["actions"] == [{"type": "openra_wait_ticks", "ticks": 260}]
    assert after["target"] == {"kind": "openra_map", "mod": "cnc", "map": "gdi01"}
    assert after["assertions"] == [{"type": "no_openra_gameplay_probe_errors"}]


def test_openra_phase0_overlay_is_exact_and_refuses_a_second_install(
    tmp_path: Path,
) -> None:
    root = _overlay_root(tmp_path)

    identity = prepare_openra_pr21906_runtime_overlay(root)

    audit = root / "OpenRA.Mods.Common/Traits/World/WarptestProductionTabsAudit.cs"
    rules = root / "mods/cnc/maps/gdi01/rules.yaml"
    assert identity["schema"] == "c1-lazarus-openra-overlay-v1"
    assert identity["task_id"] == OPENRA_PHASE0_TASK_ID
    assert len(identity["overlay_sha256"]) == 64
    assert audit.is_file()
    assert "WarptestProductionTabsAudit" in audit.read_text(encoding="utf-8")
    rendered_rules = rules.read_text(encoding="utf-8")
    assert rendered_rules.count("WarptestProductionTabsAudit:") == 1
    assert "WARPTABDISABLED:" in rendered_rules
    assert "WARPTABENABLED:" in rendered_rules
    assert "WARPTABGATE:" in rendered_rules

    with pytest.raises(RuntimeError, match="already installed"):
        prepare_openra_pr21906_runtime_overlay(root)


def test_openra_phase0_audit_requires_the_real_fixed_buggy_differential() -> None:
    common = [
        _snapshot(40, tabs=0, enabled=False),
        _snapshot(60, tabs=1, enabled=True),
        _snapshot(100, tabs=1, enabled=True),
        _snapshot(160, tabs=1, enabled=True),
        _snapshot(240, tabs=1, enabled=True),
    ]
    fixed = evaluate_openra_pr21906_audit(
        {
            "localPlayer": "GDI",
            "nonLocalProductionActor": "Nod",
            "snapshots": common,
        }
    )
    buggy = evaluate_openra_pr21906_audit(
        {
            "localPlayer": "GDI",
            "nonLocalProductionActor": "Nod",
            "snapshots": [
                common[0],
                _snapshot(60, tabs=0, enabled=True),
                _snapshot(100, tabs=0, enabled=True),
                _snapshot(160, tabs=0, enabled=True),
                _snapshot(240, tabs=0, enabled=True),
            ],
        }
    )

    assert fixed.complete is True
    assert fixed.invariants_ok is True
    assert fixed.goal_checks_ok is True
    assert fixed.defect_signature_ok is False
    assert buggy.complete is True
    assert buggy.invariants_ok is True
    assert buggy.goal_checks_ok is False
    assert buggy.defect_signature_ok is True


def test_openra_phase0_audit_rejects_a_local_only_actor_sequence() -> None:
    observation = evaluate_openra_pr21906_audit(
        {
            "localPlayer": "GDI",
            "snapshots": [
                _snapshot(40, tabs=0, enabled=False),
                _snapshot(60, tabs=1, enabled=True),
                _snapshot(100, tabs=1, enabled=True),
                _snapshot(160, tabs=1, enabled=True),
                _snapshot(240, tabs=1, enabled=True),
            ],
        }
    )

    assert observation.complete is False
    assert observation.goal_checks_ok is False
    assert observation.defect_signature_ok is False
    assert any("non-local" in failure for failure in observation.failures)


def test_openra_phase0_audit_fails_closed_on_missing_queue_evidence() -> None:
    observation = evaluate_openra_pr21906_audit(
        {"localPlayer": "GDI", "snapshots": [{"worldTick": 240, "vehicleTabs": 0}]}
    )

    assert observation.complete is False
    assert observation.invariants_ok is False
    assert observation.goal_checks_ok is False
    assert observation.defect_signature_ok is False


def _snapshot(tick: int, *, tabs: int, enabled: bool) -> dict[str, object]:
    return {
        "worldTick": tick,
        "vehicleTabs": tabs,
        "warptabTabs": tabs,
        "queues": [
            {
                "Name": "warptabdisabled",
                "Group": "Vehicle",
                "Enabled": enabled,
                "Owner": "GDI",
            }
        ],
    }
