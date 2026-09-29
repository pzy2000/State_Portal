from __future__ import annotations

from pathlib import Path

import pytest

import warptest.c1_lazarus_selection.luanti_14757_visual_capture_plan as plan


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "build-app").mkdir(parents=True)
    (source / "build-app/CMakeCache.txt").write_text(
        "BUILD_CLIENT:BOOL=TRUE\nENABLE_OPENGL:BOOL=ON\nUSE_SDL2:BOOL=OFF\n",
        encoding="utf-8",
    )
    (source / "src/client").mkdir(parents=True)
    (source / "src/client/game.cpp").write_text(
        "wasKeyPressed(KeyType::SCREENSHOT);\nclient->makeScreenshot();\n",
        encoding="utf-8",
    )
    (source / "src/client/client.cpp").write_text(
        "void Client::makeScreenshot() { driver->createScreenShot(); }\n",
        encoding="utf-8",
    )
    (source / "src").mkdir(exist_ok=True)
    (source / "src/defaultsettings.cpp").write_text(
        'settings->setDefault("keymap_screenshot", "KEY_F12");\n',
        encoding="utf-8",
    )
    (source / "irr/src").mkdir(parents=True)
    (source / "irr/src/COpenGLDriver.cpp").write_text(
        "IImage *COpenGLDriver::createScreenShot() { glReadPixels(0, 0, 1, 1, 0, 0, 0); }\n",
        encoding="utf-8",
    )

    app = tmp_path / "fixed-minetest.app"
    binary = app / "Contents/MacOS/minetest"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"Mach-O client")
    binary.chmod(0o700)
    resources = app / "Contents/Resources"
    (resources / "games/devtest").mkdir(parents=True)
    (resources / "games/devtest/game.conf").write_text("name = devtest\n", encoding="utf-8")
    (resources / "client/shaders").mkdir(parents=True)
    (resources / "irr/media").mkdir(parents=True)
    (resources / "client/shaders/Irrlicht").symlink_to("../../irr/media", target_is_directory=True)
    return app, source


def test_static_plan_distinguishes_real_opengl_f12_from_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, source = _fixture(tmp_path)
    monkeypatch.setattr(plan, "_has_opengl_link", lambda _binary: True)

    result = plan._inspect_variant(tmp_path, app, source)

    assert result["build_client_true"] is True
    assert result["opengl_cmake_enabled"] is True
    assert result["opengl_screenshot_readback"] is True
    assert result["builtin_f12_screenshot_path"] is True
    assert result["persistent_warptest_hook_present"] is False
    assert result["binary_contains_warptest_hook"] is False
    assert result["binary_has_opengl_framework_link"] is True


def test_static_plan_rejects_shader_target_outside_disposable_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, source = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = app / "Contents/Resources/client/shaders/Irrlicht"
    link.unlink()
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(plan, "_has_opengl_link", lambda _binary: True)

    with pytest.raises(RuntimeError, match="repaired Irrlicht"):
        plan._inspect_variant(tmp_path, app, source)
