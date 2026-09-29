from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from warptest.lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    canonicalize_openra_pr21453_checkpoint_semantics,
    evaluate_openra_pr21453_hotkey_audit,
    openra_pr21453_hotkey_probe,
    prepare_openra_pr21453_runtime_overlay,
    run_openra_pr21453_hotkey_cycle_probe,
    run_openra_pr21453_hotkey_probe,
)
from warptest.spec_schema import validate_spec_schema


ROOT = Path(__file__).resolve().parents[1]


def _overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "openra"
    for relative in (
        "mods/ra/maps/allies-08a/map.yaml",
        "mods/ra/maps/allies-08a/rules.yaml",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / "games/OpenRA" / relative).read_bytes())
    (root / "OpenRA.Mods.Common/Traits/World").mkdir(parents=True)
    return root


def test_openra_hotkey_probe_uses_the_live_ra_gameplay_contract() -> None:
    before = openra_pr21453_hotkey_probe("before")
    after = openra_pr21453_hotkey_probe("after")

    assert OPENRA_HOTKEY_PHASE0_TASK_ID == "lz-openra-21453-hotkey-ready-building"
    assert validate_spec_schema(before, "openra-hotkey-before.yaml") == []
    assert validate_spec_schema(after, "openra-hotkey-after.yaml") == []
    assert before["target"] == {"kind": "openra_map", "mod": "ra", "map": "allies-08a"}
    assert before["setup"] == {
        "openra_gameplay_probe": {
            "all_tech": True,
            "cash": 50000,
            "fast_build": True,
        }
    }
    assert before["actions"] == []
    assert after["actions"] == []
    assert after["assertions"] == [{"type": "no_openra_gameplay_probe_errors"}]


def test_openra_hotkey_overlay_is_exact_and_refuses_a_second_install(
    tmp_path: Path,
) -> None:
    root = _overlay_root(tmp_path)

    identity = prepare_openra_pr21453_runtime_overlay(root)

    audit = root / "OpenRA.Mods.Common/Traits/World/WarptestReadyBuildingAudit.cs"
    rules = root / "mods/ra/maps/allies-08a/rules.yaml"
    map_file = root / "mods/ra/maps/allies-08a/map.yaml"
    assert identity["schema"] == "c1-lazarus-openra-overlay-v1"
    assert identity["task_id"] == OPENRA_HOTKEY_PHASE0_TASK_ID
    assert len(identity["overlay_sha256"]) == 64
    assert audit.is_file()
    audit_source = audit.read_text(encoding="utf-8")
    assert "WarptestReadyBuildingAudit" in audit_source
    assert "WARPTEST_OPENRA_HOTKEY_STATE_PATH" in audit_source
    assert "state_restore_applied" in audit_source
    assert "placement_cancel_observed" in audit_source
    assert "Sync.RunUnsynced" in audit_source
    assert "semantic_start" in audit_source
    assert "ResolveSemanticPlacementTarget" in audit_source
    assert "requestedQueue" in audit_source
    assert rules.read_text(encoding="utf-8").count("WarptestReadyBuildingAudit:") == 1
    assert map_file.read_text(encoding="utf-8").count("WarptestLazarusFactory: fact") == 1

    with pytest.raises(RuntimeError, match="already installed"):
        prepare_openra_pr21453_runtime_overlay(root)


def test_openra_hotkey_audit_requires_the_real_fixed_buggy_differential() -> None:
    fixed = evaluate_openra_pr21453_hotkey_audit(
        _audit(placement_active=True)
    )
    buggy = evaluate_openra_pr21453_hotkey_audit(
        _audit(placement_active=False)
    )

    assert fixed.complete is True
    assert fixed.invariants_ok is True
    assert fixed.goal_checks_ok is True
    assert fixed.defect_signature_ok is False
    assert buggy.complete is True
    assert buggy.invariants_ok is True
    assert buggy.goal_checks_ok is False
    assert buggy.defect_signature_ok is True


def test_openra_hotkey_audit_fails_closed_on_visible_or_unready_target() -> None:
    visible = evaluate_openra_pr21453_hotkey_audit(
        _audit(placement_active=True, target_hidden=False)
    )
    unready = evaluate_openra_pr21453_hotkey_audit(
        _audit(placement_active=True, ready=False)
    )

    assert visible.complete is False
    assert visible.invariants_ok is False
    assert unready.complete is False
    assert unready.invariants_ok is False


def test_openra_hotkey_probe_uses_a_real_background_e_key_and_classifies_both_versions(
    tmp_path: Path,
) -> None:
    fixed_root = tmp_path / "fixed"
    buggy_root = tmp_path / "buggy"
    fixed_root.mkdir()
    buggy_root.mkdir()
    fixed = run_openra_pr21453_hotkey_probe(
        fixed_root,
        tmp_path / "fixed-output",
        session_factory=_fake_session_factory(placement_after_key=True),
    )
    buggy = run_openra_pr21453_hotkey_probe(
        buggy_root,
        tmp_path / "buggy-output",
        session_factory=_fake_session_factory(placement_after_key=False),
    )

    assert fixed["passed"] is True
    assert fixed["before"]["ready"] is True
    assert fixed["after"]["goal_checks_ok"] is True
    assert fixed["after"]["defect_signature_ok"] is False
    assert fixed["input"]["accepted"] is True
    assert fixed["input"]["event_count"] == 2
    assert buggy["passed"] is False
    assert buggy["before"]["ready"] is True
    assert buggy["after"]["goal_checks_ok"] is False
    assert buggy["after"]["defect_signature_ok"] is True


def test_openra_hotkey_cycle_restores_start_without_preconstructing_it_for_agent_s(
    tmp_path: Path,
) -> None:
    """The H2 path uses a true state start, then replays the real UI defect."""

    (tmp_path / "fixed").mkdir()
    (tmp_path / "buggy").mkdir()
    fixed = run_openra_pr21453_hotkey_cycle_probe(
        tmp_path / "fixed",
        tmp_path / "fixed-output",
        checkpoint_sha256="a" * 64,
        session_factory=_cycle_session_factory(placement_after_key=True),
    )
    buggy = run_openra_pr21453_hotkey_cycle_probe(
        tmp_path / "buggy",
        tmp_path / "buggy-output",
        checkpoint_sha256="a" * 64,
        session_factory=_cycle_session_factory(placement_after_key=False),
    )

    assert fixed["passed"] is True
    assert fixed["before"]["ready"] is True
    assert fixed["before"]["state_restore_applied"] is True
    assert fixed["after"]["goal_checks_ok"] is True
    assert fixed["after"]["defect_signature_ok"] is False
    assert fixed["after"]["placement_cancel_observed"] is True
    assert fixed["inputs"] == [
        {"kind": "click", "button": "right", "accepted": True},
        {"kind": "key", "key": "e", "accepted": True},
    ]

    assert buggy["passed"] is False
    assert buggy["before"]["ready"] is True
    assert buggy["before"]["state_restore_applied"] is True
    assert buggy["after"]["goal_checks_ok"] is False
    assert buggy["after"]["defect_signature_ok"] is True
    assert buggy["after"]["placement_cancel_observed"] is True


def test_openra_hotkey_cycle_rejects_a_generated_action_that_cannot_construct_start(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixed"
    root.mkdir()
    generated = {
        "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
        "checkpoint": {
            "allowed_strategies": ["openra_gameplay_production_validate"]
        },
        "actions": [{"type": "openra_wait_ticks", "ticks": 1}],
    }
    with pytest.raises(ValueError, match="generated semantic start action"):
        canonicalize_openra_pr21453_checkpoint_semantics(generated)


def _cycle_session_factory(*, placement_after_key: bool):
    @contextmanager
    def create(_repo: Path, out_dir: Path, _spec: dict[str, object]):
        yield _FakeHotkeyCycleSession(
            out_dir,
            placement_after_key=placement_after_key,
        )

    return create


def _fake_session_factory(*, placement_after_key: bool):
    @contextmanager
    def create(_repo: Path, out_dir: Path, _spec: dict[str, object]):
        yield _FakeHotkeySession(out_dir, placement_after_key=placement_after_key)

    return create


class _FakeHotkeySession:
    session_id = "fake-openra-session"
    width = 1280
    height = 720

    def __init__(self, out_dir: Path, *, placement_after_key: bool) -> None:
        self.out_dir = out_dir
        self.placement_after_key = placement_after_key
        self.last_policy_frame_id = -1
        self._world_tick = 47

    def start(self) -> "_FakeHotkeySession":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._write_audit(placement_active=False)
        return self

    def capture_policy_frame(self, path: Path) -> str:
        self.last_policy_frame_id += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-openra-policy-frame")
        return str(path)

    def dispatch_input_batch(self, batch: object) -> dict[str, object]:
        actions = getattr(batch, "actions")
        assert len(actions) == 1
        assert actions[0].kind == "key"
        assert actions[0].key == "e"
        self._write_audit(placement_active=self.placement_after_key)
        return {
            "accepted": True,
            "batch_id": getattr(batch, "batch_id"),
            "event_count": 2,
            "resulting_frame_id": getattr(batch, "frame_id") + 1,
        }

    def _write_audit(self, *, placement_active: bool) -> None:
        import os

        self._world_tick += 1
        target = Path(os.environ["WARPTEST_OPENRA_HOTKEY_AUDIT_PATH"])
        target.write_text(
            json.dumps(
                _audit(
                    placement_active=placement_active,
                    world_tick=self._world_tick,
                )
            ),
            encoding="utf-8",
        )


class _FakeHotkeyCycleSession(_FakeHotkeySession):
    def __init__(self, out_dir: Path, *, placement_after_key: bool) -> None:
        super().__init__(out_dir, placement_after_key=placement_after_key)
        self._placement_active = False
        self._placement_cancel_observed = False

    def start(self) -> "_FakeHotkeyCycleSession":
        import os

        state_path = Path(os.environ["WARPTEST_OPENRA_HOTKEY_STATE_PATH"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["checkpoint_sha256"] == "a" * 64
        semantic = state["semantic_start"]
        assert semantic == {
            "schema": "c1-lazarus-openra-generated-start-v1",
            "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
            "action": {
                "type": "openra_build_actor",
                "queue": "Building",
                "selector": "completed_building_placement",
            },
        }
        self._state_restore_spec_sha256 = state["semantic_start_sha256"]
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._placement_active = True
        self._write_cycle_audit()
        return self

    def dispatch_input_batch(self, batch: object) -> dict[str, object]:
        actions = getattr(batch, "actions")
        assert len(actions) == 1
        action = actions[0]
        if action.kind == "click":
            assert action.button == "right"
            self._placement_active = False
            self._placement_cancel_observed = True
            event_count = 3
        else:
            assert action.kind == "key"
            assert action.key == "e"
            self._placement_active = self.placement_after_key
            event_count = 2
        self._write_cycle_audit()
        return {
            "accepted": True,
            "batch_id": getattr(batch, "batch_id"),
            "event_count": event_count,
            "resulting_frame_id": getattr(batch, "frame_id") + 1,
        }

    def _write_cycle_audit(self) -> None:
        import os

        self._world_tick += 1
        target = Path(os.environ["WARPTEST_OPENRA_HOTKEY_AUDIT_PATH"])
        payload = _audit(
            placement_active=self._placement_active,
            world_tick=self._world_tick,
        )
        payload["state_restore_requested"] = True
        payload["state_restore_applied"] = True
        payload["state_checkpoint_sha256"] = "a" * 64
        payload["state_restore_spec_sha256"] = self._state_restore_spec_sha256
        payload["placement_cancel_observed"] = self._placement_cancel_observed
        target.write_text(json.dumps(payload), encoding="utf-8")


def _audit(
    *,
    placement_active: bool,
    target_hidden: bool = True,
    ready: bool = True,
    world_tick: int = 48,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "world_tick": world_tick,
        "player": "Greece",
        "queue_type": "Building",
        "target": "warpready",
        "queue_item": "warpready",
        "queue_ready": ready,
        "target_hidden": target_hidden,
        "palette_items": 41,
        "placement_active": placement_active,
        "placement_cancel_observed": False,
        "state_restore_error": "",
        "probe_errors": [],
    }
