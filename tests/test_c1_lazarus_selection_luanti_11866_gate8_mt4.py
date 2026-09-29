from warptest.c1_lazarus_selection import luanti_11866_gate8_mt4 as module


def test_dependency_is_candidate_owned_exact_mt4_pin() -> None:
    assert module._TAG == "1.9.0mt4"
    assert module._TAG_COMMIT == "53db262bd224516547fe7e0e05698d9624cf3d61"
    assert module._DEPENDENCY_ROOT.as_posix().startswith(
        "private/dependencies/luanti-11866-irrlichtmt/"
    )
    assert "14464" not in module._DEPENDENCY_ROOT.as_posix()


def test_build_requires_separate_explicit_confirmation() -> None:
    assert module.BUILD_CONFIRMATION == "EXECUTE_LUANTI_11866_GATE8_MT4_BUILD_V1"


def test_gate8_chain_keeps_strict_v5_transition() -> None:
    assert module._STRICT_TRANSITION.artifact_id.endswith("/native/v5")
    assert module._READINESS.artifact_kind == "candidate_gate8_11866_configure_readiness"
