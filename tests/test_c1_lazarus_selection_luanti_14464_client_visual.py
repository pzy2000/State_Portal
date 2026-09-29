from warptest.c1_lazarus_selection import luanti_14464_client_visual as module


def test_panels_are_bound_to_fresh_gate10_counts() -> None:
    fixed = module._variant_mod("fixed")
    buggy = module._variant_mod("buggy")
    assert "28 / 28" in fixed and "goal_iconv_state_reset" in fixed
    assert "EXACT 3 / 28" in buggy and "defect_iconv_state_regression" in buggy
    for name in ("testUTF8", "testWrapRows", "testSanitizeDirName"):
        assert name in fixed and name in buggy


def test_client_closure_is_candidate_owned() -> None:
    assert module._CURL_ROOT.as_posix().startswith("private/dependencies/luanti-14464-curl/8.5.0")
    assert "14464" in module._PAIR.as_posix()


def test_admission_confirmation_is_separate() -> None:
    assert module.ADMISSION_CONFIRMATION != module.CONFIRMATION
