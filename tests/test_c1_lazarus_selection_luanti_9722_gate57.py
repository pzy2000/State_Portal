from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_9722_gate57 import (
    _attempt_reference,
    _completed_count,
    _observed,
)
from warptest.c1_lazarus_selection.luanti_lua_regression import WatchdogReceipt


def test_9722_attempt_ids_are_fresh_and_candidate_specific() -> None:
    reference = _attempt_reference("f2p", "fixed", 1)
    assert reference.artifact_id == (
        "candidate_gate57_attempt/luanti/9722/lua/f2p/fixed/1/v1"
    )
    assert reference.artifact_kind == "candidate_gate57_attempt"
    assert "lua_regression_attempt" not in reference.artifact_id


def test_9722_busted_summary_accepts_plain_and_ansi_output() -> None:
    for output in (
        b"1 success / 0 failures / 0 errors / 0 pending\n",
        b"\x1b[32m1 success / 0 failures / 0 errors / 0 pending\n",
    ):
        receipt = WatchdogReceipt("completed", 0, output, b"")
        assert _completed_count(receipt) == 1
        assert _observed(receipt, 1) == "pass"


def test_9722_failure_and_timeout_are_typed() -> None:
    failure = WatchdogReceipt(
        "completed", 1, b"1 success / 0 failures / 0 errors / 0 pending\n", b""
    )
    timeout = WatchdogReceipt("timed_out", -9, b"", b"")
    assert _observed(failure, 1) == "failure"
    assert _observed(timeout, None) == "timed_out"
