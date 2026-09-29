from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_server_unit_plan import (
    write_server_unit_plan,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


ROOT = Path(".warptest-cache/c1_lazarus_selection_v1")


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/luanti-14586-server-plan-test/v1",
        scope="candidate-screening",
        granted_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )


def test_14586_server_unit_plan_is_typed_and_downstream_pending() -> None:
    if not ROOT.is_dir():
        pytest.skip("local v2 artifact cache is unavailable")
    artifact = write_server_unit_plan(
        ROOT,
        CandidateKey("luanti", 14586),
        authorization=_authorization(),
    )
    assert artifact.payload["candidate"] == {
        "repository_key": "luanti",
        "change_number": 14586,
    }
    assert artifact.payload["terminal"] is False
    assert artifact.payload["downstream_status"] == "downstream_pending"
    inverse = artifact.payload["strict_inverse"]
    assert inverse["production_paths"] == ["src/inventory.cpp"]
    assert inverse["preserved_test_paths"] == ["src/unittest/test_moveaction.cpp"]
    runner = artifact.payload["runner_contract"]
    assert runner["kind"] == "luanti_native_server_unit"
    assert runner["f2p"] == {
        "module": "TestMoveAction",
        "selector": "TestMoveAction",
        "repetitions": 3,
        "fixed_expected": "pass",
        "buggy_expected": "failure",
    }
    assert runner["p2p"] == {
        "module": "TestSettings",
        "selector": "TestSettings",
        "repetitions": 3,
        "fixed_expected": "pass",
        "buggy_expected": "pass",
    }
    receipts = runner["receipt_contract"]
    assert len(receipts) == 12
    assert receipts[0] == {
        "phase": "f2p",
        "variant": "fixed",
        "repetition": 1,
        "selector": "TestMoveAction",
        "expected": "pass",
    }
    assert receipts[-1] == {
        "phase": "p2p",
        "variant": "buggy",
        "repetition": 3,
        "selector": "TestSettings",
        "expected": "pass",
    }
    assert artifact.payload["resource_policy"]["build_started"] is False


def test_server_unit_plan_rejects_other_candidate() -> None:
    with pytest.raises(ValueError, match="only registered"):
        write_server_unit_plan(
            ROOT,
            CandidateKey("luanti", 12602),
            authorization=_authorization(),
        )
