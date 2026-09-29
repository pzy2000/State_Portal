from warptest.c1_lazarus_selection import luanti_14685_gate14_terminal as module


def test_14685_terminal_binds_fixed_only_test_asset() -> None:
    assert module._TEST_PATH == "src/unittest/test_irr_gltf_mesh_loader.cpp"
    assert module._TEST_ASSET.endswith("gltf_simple_skin.gltf")
    assert not hasattr(module, "run_candidate")
