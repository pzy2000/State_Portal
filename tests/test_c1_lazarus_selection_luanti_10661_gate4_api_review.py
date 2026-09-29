from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_10661_gate4_api_review import (
    _api_observations,
)


def test_api_observations_classify_fixed_only_constructor_risk() -> None:
    raw = "\n".join(
        [
            "-\tMapSettingsManager mgr(&user_settings, test_mapmeta_path);",
            "+\tMapSettingsManager mgr(test_mapmeta_path);",
            "-MapSettingsManager::MapSettingsManager(Settings *user_settings,",
            "+MapSettingsManager::MapSettingsManager(const std::string &map_meta_path):",
            "-\tMapSettingsManager(Settings *user_settings,",
            "+\tMapSettingsManager(const std::string &map_meta_path);",
        ]
    )
    result = _api_observations(raw)
    assert result["fixed_only_signature_risk"] is True
    assert (
        result["observation_status"] == "static_risk_detected_mechanical_check_not_run"
    )
