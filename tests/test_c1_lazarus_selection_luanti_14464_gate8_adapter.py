from warptest.c1_lazarus_selection import luanti_14464_gate8_adapter as module


def test_dependency_pins_belong_to_14464_source() -> None:
    assert module._CURL_PIN == "8.5.0"
    assert module._IRRLICHT_VERSION == "1.9.0.15"
    assert module._HEAD_REVISION == "fa89a63b34005bccc5a25b80ae6c8d4a80ff25a7"


def test_adapter_is_not_forbidden_compiler_undefinition() -> None:
    source = open(module.__file__, encoding="utf-8").read()
    assert '"forbidden_flag_used": False' in source
    assert '"generated_build_header_only": True' in source


def test_gate8_paths_are_candidate_specific() -> None:
    assert module._PAIR.as_posix().startswith("private/materialized/luanti/14464/")
    assert module._BUILD_ROOT.as_posix().startswith("private/bounded-builds/luanti/14464/")


def test_build_and_downstream_plans_are_separately_gated() -> None:
    assert module.BUILD_CONFIRMATION == "EXECUTE_LUANTI_14464_GATE8_BUILD_V1"
    assert module._RUNTIME_PLAN.artifact_kind == "candidate_gate9_10_14464_runtime_plan"
    assert module._VISUAL_PLAN.artifact_kind == "candidate_gate11_13_14464_visual_plan"
