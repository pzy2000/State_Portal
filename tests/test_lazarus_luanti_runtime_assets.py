from __future__ import annotations

from pathlib import Path

from warptest.luanti_runtime import LuantiRuntimeAdapter, sync_luanti_builtin_assets


def test_lazarus_golden_runtime_freezes_noon_in_its_disposable_world(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "version"
    (repo / "mods/warptest").mkdir(parents=True)
    (repo / "mods/warptest/init.lua").write_text("return true\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    adapter = LuantiRuntimeAdapter(
        repo, output, daylight_mode="fixed_noon", experiment_seed=0
    )

    world = adapter._prepare_world("golden")
    conf = adapter._write_conf(
        "golden",
        world,
        output / "request.json",
        output / "report.json",
        output / "frame.png",
    )

    assert "time_speed = 0" in conf.read_text(encoding="utf-8")
    assert "fixed_map_seed = 0" in conf.read_text(encoding="utf-8")
    assert (world / "worldmods/warptest_lazarus_daylight/init.lua").is_file()
    assert "load_mod_warptest_lazarus_daylight = true" in (
        world / "world.mt"
    ).read_text(encoding="utf-8")


def test_lazarus_runtime_syncs_builtin_lua_into_disposable_macos_app(tmp_path: Path) -> None:
    repo = tmp_path / "version"
    source = repo / "builtin/game/falling.lua"
    destination = (
        repo
        / "build-app/out/luanti.app/Contents/Resources/builtin/game/falling.lua"
    )
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_text("return 'buggy'\n", encoding="utf-8")
    destination.write_text("return 'fixed'\n", encoding="utf-8")

    result = sync_luanti_builtin_assets(repo)

    assert result["status"] == "synced"
    assert result["files"] == 1
    assert destination.read_text(encoding="utf-8") == "return 'buggy'\n"


def test_lazarus_runtime_asset_sync_fails_closed_without_an_app_bundle(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "version"
    (repo / "builtin").mkdir(parents=True)

    result = sync_luanti_builtin_assets(repo)

    assert result == {"status": "not_available", "files": 0}
