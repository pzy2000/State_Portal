from warptest.c1_lazarus_selection import luanti_13132_irrlichtmt9_source as module


def test_authoritative_upstream_identity_is_pinned() -> None:
    assert module.REMOTE_URL == "https://github.com/minetest/irrlicht.git"
    assert module.TAG == "1.9.0mt9"
    assert module.COMMIT == "ce0d29df933fd03c5de218e159976629113257b9"
    assert module.TREE == "7a85087dab624a147ee8b047678fed3637d1d548"


def test_source_namespace_is_candidate_specific() -> None:
    assert module._SOURCE_TREE.as_posix().startswith(
        "private/dependencies/luanti-13132-irrlicht/"
    )
    assert "9722" not in module._SOURCE_TREE.as_posix()
