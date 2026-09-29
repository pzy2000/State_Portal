from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from warptest.c1_lazarus_selection import c1_lazarus_jupyterlab_specs as target
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord
from warptest.lazarus_jupyterlab_probe import (
    DOM_AUDIT_CHECK,
    PROBE_ADAPTER,
    PROBE_ERROR_CHECK,
    JupyterLabProbeBinding,
)
from warptest.c1_lazarus_selection.c1_lazarus_golden_publication import _TASK_IDS
from warptest.spec_schema import ensure_valid_spec_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPOSITORY_ROOT / "specs/jupyterlab/lazarus"


def _authorization(scope: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-lazarus-specs/{scope}",
        scope=scope,
        granted_at="2026-08-15T05:00:00Z",
        local_user_id=os.getuid(),
    )


def _binding(task_id: str, change_number: int) -> JupyterLabProbeBinding:
    return JupyterLabProbeBinding(
        task_id=task_id,
        change_number=change_number,
        replay_steps=("load_candidate_local_bundle", "read_order"),
        expected_after={
            "fixed": {"after_item_names": ["a", "b"]},
            "buggy": {"after_item_names": ["b", "a"]},
        },
        semantic_state=lambda data: dict(data),
        observation=lambda root, variant: {},
    )


def _manifest(task_id: str) -> Mapping[str, Any]:
    path = SPEC_DIR / f"{task_id}.goldens.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _written_specs() -> list[Path]:
    """Return only the specs this writer owns.

    The directory also holds expansion-row specs, which are written by
    ``warptest.lazarus_expansion_specs`` and drive a different probe adapter.
    """

    return sorted(
        SPEC_DIR / f"{task_id}.yaml"
        for (repository_key, _), task_id in _TASK_IDS.items()
        if repository_key == "jupyterlab"
    )


def test_every_written_spec_satisfies_the_lazarus_schema() -> None:
    paths = _written_specs()
    assert len(paths) == 11
    for path in paths:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        ensure_valid_spec_schema(spec, path, allow_lazarus=True)


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


def test_executable_specs_state_a_separating_goal_and_defect() -> None:
    executable = 0
    for path in _written_specs():
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        probe = spec["setup"]["jupyterlab_lazarus_probe"]
        lazarus = spec["evaluation"]["lazarus"]
        assert probe["adapter"] == PROBE_ADAPTER
        assert lazarus["invariant_checks"] == [{"type": PROBE_ERROR_CHECK}]
        if not probe["probe_executable"]:
            assert probe["unbound_reason"]
            assert not probe["registered_replay"]
            continue
        executable += 1
        assert probe["registered_replay"]
        assert lazarus["goal_checks"] != lazarus["defect_signature"]
        for check in (*lazarus["goal_checks"], *lazarus["defect_signature"]):
            assert check["type"] == DOM_AUDIT_CHECK
            assert check["field"]
        for surface in spec["evaluation"]["semantic"]["surfaces"].values():
            assert surface["adapter"] == PROBE_ADAPTER
    assert executable == 11


def test_document_projects_the_binding_and_keeps_the_manifest_prose() -> None:
    task_id = "lz-jupyterlab-19227-reactive-toolbar-positions"
    manifest = _manifest(task_id)
    document = target._spec_document(
        task_id=task_id,
        change_number=19227,
        manifest=manifest,
        binding=_binding(task_id, 19227),
    )
    lazarus = document["evaluation"]["lazarus"]
    assert lazarus["goal_checks"] == [
        {"type": DOM_AUDIT_CHECK, "field": "after_item_names", "expected": ["a", "b"]}
    ]
    assert lazarus["defect_signature"] == [
        {"type": DOM_AUDIT_CHECK, "field": "after_item_names", "expected": ["b", "a"]}
    ]
    assert set(lazarus["upstream_tests"]) == {
        "fail_to_pass",
        "pass_to_pass",
        "per_trial_p2p",
    }
    assert lazarus["upstream_tests"]["per_trial_p2p"] == (
        lazarus["upstream_tests"]["pass_to_pass"]
    )
    assert document["evaluation"]["visual_oracle"] == manifest["evaluation"]["visual_oracle"]


def test_document_without_a_binding_records_why_it_cannot_run() -> None:
    """Every task is bound today; an unbound one must still say so, not pretend."""

    task_id = "lz-jupyterlab-17369-stream-chunk-merge"
    document = target._spec_document(
        task_id=task_id,
        change_number=17369,
        manifest=_manifest(task_id),
        binding=None,
    )
    probe = document["setup"]["jupyterlab_lazarus_probe"]
    assert probe["probe_executable"] is False
    assert probe["unbound_reason"] == "no offline probe binding is registered"
    assert probe["registered_replay"] == []
    assert document["evaluation"]["lazarus"]["goal_checks"] == [
        {"type": PROBE_ERROR_CHECK}
    ]


def test_document_refuses_a_manifest_without_goldens() -> None:
    with pytest.raises(ValueError):
        target._spec_document(
            task_id="lz-jupyterlab-19227-reactive-toolbar-positions",
            change_number=19227,
            manifest={"evaluation": {"visual_oracle": {}}},
            binding=None,
        )


@pytest.mark.parametrize(
    "scope",
    ["preannotation-execution", "unrelated-scope"],
)
def test_writer_requires_an_infrastructure_audit_authorization(
    tmp_path: Path, scope: str
) -> None:
    with pytest.raises(PermissionError):
        target.write_jupyterlab_task_specs(
            tmp_path,
            repository_root=REPOSITORY_ROOT,
            authorization=_authorization(scope),
        )


def test_verifier_requires_a_preannotation_authorization(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        target.verify_jupyterlab_task_specs(
            tmp_path,
            repository_root=REPOSITORY_ROOT,
            authorization=_authorization("infrastructure-audit"),
        )
