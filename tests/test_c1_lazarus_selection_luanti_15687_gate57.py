from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_native_fixed_source_capsule import (
    CONFIRMATION_15687,
)
from warptest.c1_lazarus_selection.luanti_native_regression import (
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey


def test_15687_native_profile_binds_exact_catch_and_inverse_scope() -> None:
    profile = profile_for_candidate(CandidateKey("luanti", 15687))

    assert profile.profile_id == "luanti-15687-matrix-scale-v2-native-gate57"
    assert profile.production_paths == (
        "irr/include/matrix4.h",
        "src/client/content_cao.cpp",
    )
    assert profile.test_paths == ("src/unittest/test_irr_matrix4.cpp",)
    assert profile.p2p_test_paths == ("src/unittest/test_settings.cpp",)
    assert profile.f2p_selector == "matrix4"
    assert profile.p2p_selector == "TestSettings"
    assert CONFIRMATION_15687 == "MIGRATE_LUANTI_15687_NATIVE_FIXED_SOURCE_CAPSULE"
