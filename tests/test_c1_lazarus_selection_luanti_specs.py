from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from warptest.c1_lazarus_selection import c1_lazarus_luanti_specs as target
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord
from warptest.lazarus_luanti_probe import (
    FROZEN_ADAPTER,
    FROZEN_SOURCE,
    LIVE_ADAPTER,
    LIVE_SOURCE,
    LuantiProbeBinding,
)
from warptest.lazarus_luanti_states import (
    PROBE_ERROR_CHECK,
    STATE_AUDIT_CHECK,
    LuantiLazarusStates,
)
from warptest.spec_schema import ensure_valid_spec_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPOSITORY_ROOT / "specs/luanti/lazarus"
TASK_IDS = target.luanti_task_ids()


def _authorization(scope: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/luanti-lazarus-specs/{scope}",
        scope=scope,
        granted_at="2026-08-15T06:00:00Z",
        local_user_id=os.getuid(),
    )


def _binding(source: str = FROZEN_SOURCE) -> LuantiProbeBinding:
    states = LuantiLazarusStates(
        change_number=14757,
        fixed={"result": "0,-0"},
        buggy={"result": "1,-1"},
        source_artifact_ids=("candidate_gate10_server_side_oracle/luanti/14757/v1",),
        layout="fixed_observation_pair",
        fail_to_pass=("math round()",),
        pass_to_pass=("formspec_escape escapes",),
        admission_id="candidate_formal_freeze_admission/luanti/14757/gap-closed-v1",
    )
    return LuantiProbeBinding(
        task_id="lz-luanti-14757-math-round",
        change_number=14757,
        source=source,
        replay_steps=("start_headless_server", "read_semantic_result"),
        states=states,
        observe=lambda root, version: states.state(version),
    )


def _manifest(task_id: str) -> Mapping[str, Any]:
    return yaml.safe_load((SPEC_DIR / f"{task_id}.goldens.yaml").read_text(encoding="utf-8"))


def _written_specs() -> list[Path]:
    return sorted(SPEC_DIR / f"{task_id}.yaml" for task_id in TASK_IDS.values())


def test_every_frozen_luanti_row_has_a_schema_valid_spec() -> None:
    paths = _written_specs()
    assert len(paths) == 13
    for path in paths:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        ensure_valid_spec_schema(spec, path, allow_lazarus=True)


def test_written_specs_state_separating_checks_from_their_own_evidence() -> None:
    live = 0
    for path in _written_specs():
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        probe = spec["setup"]["luanti_lazarus_probe"]
        lazarus = spec["evaluation"]["lazarus"]
        assert probe["observation_source"] in {LIVE_SOURCE, FROZEN_SOURCE}
        assert probe["adapter"] in {LIVE_ADAPTER, FROZEN_ADAPTER}
        assert probe["evidence_artifact_ids"]
        assert f"/{probe['change_number']}/" in probe["evidence_artifact_ids"][0]
        assert probe["formal_freeze_admission"].startswith(
            "candidate_formal_freeze_admission/luanti/"
        )
        assert lazarus["goal_checks"] != lazarus["defect_signature"]
        for check in (*lazarus["goal_checks"], *lazarus["defect_signature"]):
            assert check["type"] == STATE_AUDIT_CHECK
            assert check["field"]
        assert lazarus["invariant_checks"] == [{"type": PROBE_ERROR_CHECK}]
        upstream = lazarus["upstream_tests"]
        assert set(upstream) == {"fail_to_pass", "pass_to_pass", "per_trial_p2p"}
        assert upstream["fail_to_pass"] and upstream["pass_to_pass"]
        assert not set(upstream["fail_to_pass"]) & set(upstream["pass_to_pass"])
        if probe["observation_source"] == LIVE_SOURCE:
            live += 1
    assert live == 1


def test_written_specs_reuse_their_manifest_oracle_and_goldens() -> None:
    for path in _written_specs():
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest = _manifest(path.stem)
        assert spec["evaluation"]["visual_oracle"] == manifest["evaluation"]["visual_oracle"]
        assert (
            spec["evaluation"]["lazarus"]["goldens"]
            == manifest["evaluation"]["lazarus"]["goldens"]
        )
        for name in ("before", "buggy_before", "fixed", "buggy"):
            golden = spec["evaluation"]["lazarus"]["goldens"][name]
            relative = golden if isinstance(golden, str) else golden["after"]
            assert (SPEC_DIR / relative).is_file()


def test_document_carries_the_luanti_target_and_checkpoint_contract() -> None:
    document = target._spec_document(
        binding=_binding(LIVE_SOURCE),
        manifest=_manifest("lz-luanti-14757-math-round"),
    )
    assert document["benchmark"] == "luanti"
    assert document["target"]["kind"] == "synthesized_world"
    assert document["checkpoint"] == {
        "allowed_strategies": ["luanti_worldmod_state_validate"],
        "expected_validity": "save-restorable",
    }
    probe = document["setup"]["luanti_lazarus_probe"]
    assert probe["upstream_truth_table"] == {
        "fixed": {"f2p": True, "p2p": True},
        "buggy": {"f2p": False, "p2p": True},
    }
    for surface in document["evaluation"]["semantic"]["surfaces"].values():
        assert surface["adapter"] == LIVE_ADAPTER


def test_frozen_binding_declares_the_frozen_evidence_surface() -> None:
    document = target._spec_document(
        binding=_binding(FROZEN_SOURCE),
        manifest=_manifest("lz-luanti-14757-math-round"),
    )
    for surface in document["evaluation"]["semantic"]["surfaces"].values():
        assert surface["adapter"] == FROZEN_ADAPTER
    assert document["setup"]["luanti_lazarus_probe"]["observation_source"] == FROZEN_SOURCE


def test_document_refuses_a_manifest_without_goldens() -> None:
    with pytest.raises(ValueError):
        target._spec_document(binding=_binding(), manifest={"evaluation": {}})


@pytest.mark.parametrize("scope", ["preannotation-execution", "unrelated-scope"])
def test_writer_requires_an_infrastructure_audit_authorization(
    tmp_path: Path, scope: str
) -> None:
    with pytest.raises(PermissionError):
        target.write_luanti_task_specs(
            tmp_path,
            repository_root=REPOSITORY_ROOT,
            authorization=_authorization(scope),
        )


def test_verifier_requires_a_preannotation_authorization(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        target.verify_luanti_task_specs(
            tmp_path,
            repository_root=REPOSITORY_ROOT,
            authorization=_authorization("infrastructure-audit"),
        )
