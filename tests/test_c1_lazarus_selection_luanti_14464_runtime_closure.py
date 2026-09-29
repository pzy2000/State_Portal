from warptest.c1_lazarus_selection import luanti_14464_runtime_closure as module


def test_historical_selector_is_module_scoped_but_oracle_is_target_bound() -> None:
    assert b"Module TestUtilities" in module._SUMMARY.pattern
    assert module.CONFIRMATION.endswith("_V4")
    assert module._BUGGY_FAILURES == {
        "testUTF8",
        "testWrapRows",
        "testSanitizeDirName",
    }


def test_runtime_pair_is_candidate_specific() -> None:
    assert module._PAIR.as_posix().startswith("private/materialized/luanti/14464/")
    assert module._PLAN.semantic_version == "4.0"


def test_runtime_chain_excludes_old_gate57_attempts() -> None:
    assert all("attempt" not in ref.artifact_kind for ref in (
        module._BUILD,
        module._READINESS,
        module._ADAPTER,
        module._DEPENDENCY,
        module._MATERIALIZATION,
    ))
