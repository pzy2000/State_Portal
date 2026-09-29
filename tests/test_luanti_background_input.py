import inspect
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from warptest.agent_s_bridge import AgentSAction, InputBatch
from warptest.gui_agent import (
    LUANTI_BEFORE_RUBRIC,
    LuantiGuiRuntime,
    _run_luanti_agent_task,
)
from warptest.luanti_runtime import ensure_luanti_instrumentation
from warptest.luanti_runtime import (
    LuantiRuntimeAdapter,
    _luanti_build_dir,
    _read_luanti_client_build_provenance,
    _write_luanti_client_build_provenance,
    luanti_client_source_identity,
)
from warptest import state_loop


ROOT = Path(__file__).resolve().parents[1]


def test_luanti_background_input_template_defers_receipt_until_consumed() -> None:
    capture = (
        ROOT / "warptest/instrumentation/luanti/engine/warptest_capture.inc"
    ).read_text(encoding="utf-8")
    call = (
        ROOT / "warptest/instrumentation/luanti/engine/warptest_call.inc"
    ).read_text(encoding="utf-8")

    assert 'response["status"] = "pending"' in capture
    assert 'warptest_pending_batch_receipt["consumed"] = true' in capture
    assert '"consumer"] = "luanti_game_loop_v1"' in capture
    assert "warptest_pending_key_releases" in capture
    assert "warptest_release_pending_keys(device);" in capture
    assert "warptest_background_input_release_at" in capture
    assert "seconds * 1000.0" in capture
    assert 'action.isMember("duration") && !action["duration"].isNull()' in capture
    assert 'key == "/" || key == "slash"' in capture
    assert "return 56;" in capture  # SDL physical slash scancode
    assert "device->getCursorControl()->setPosition(x, y)" in capture
    assert "warptest_finish_background_input_frame(device);" in call


def test_luanti_instrumenter_adds_hidden_input_and_camera_gates(tmp_path: Path) -> None:
    (tmp_path / "mods").mkdir()
    (tmp_path / "builtin").mkdir()
    (tmp_path / "builtin/init.lua").write_text("-- builtin\n", encoding="utf-8")
    (tmp_path / "games/devtest").mkdir(parents=True)
    game_cpp = tmp_path / "src/client/game.cpp"
    game_cpp.parent.mkdir(parents=True)
    game_cpp.write_text(
        """void Game::run()
{
\twhile (true) {
\t\tupdateProfilerGraphs(&graph);
\t}
}
void Game::processUserInput(float dtime)
{
\t// Reset input if window not active or some menu is active
\tif (!device->isWindowActive() || isMenuActive() || guienv->hasFocus(gui_chat_console.get())) {
\t}
}
void Game::updateCameraDirection(CameraOrientation *cam, float dtime)
{
\tif ((device->isWindowActive() && device->isWindowFocused()
\t\t\t&& !isMenuActive()) || input->isRandom()) {
\t\tif (m_first_loop_after_window_activation && !g_touchcontrols) {
\t\t}
\t}
}
""",
        encoding="utf-8",
    )

    first = ensure_luanti_instrumentation(tmp_path)
    instrumented = game_cpp.read_text(encoding="utf-8")
    assert first["installed"] is True
    assert "warptest_background_input_is_pending()" in instrumented
    assert "!device->isWindowActive() && !warptest_background_input" in instrumented
    assert "warptest_background_input && !isMenuActive()" in instrumented
    assert "!warptest_background_input)" in instrumented

    second = ensure_luanti_instrumentation(tmp_path)
    assert second["installed"] is False
    assert game_cpp.read_text(encoding="utf-8") == instrumented


def test_luanti_visual_start_rubric_ignores_transient_world_rendering() -> None:
    ignored = " ".join(LUANTI_BEFORE_RUBRIC["ignore"]).lower()
    assert "day/night" in ignored
    assert "server version" in ignored
    assert "chat" in ignored
    assert "wielded-tool" in ignored
    source = inspect.getsource(_run_luanti_agent_task)
    assert source.count("before_rubric=LUANTI_BEFORE_RUBRIC") == 2


def test_luanti_runtime_rejects_pre_consumption_success_receipt() -> None:
    runtime = object.__new__(LuantiGuiRuntime)
    runtime.session_id = "session"
    batch = InputBatch(
        session_id="session",
        frame_id=0,
        batch_id="batch",
        actions=(AgentSAction(kind="key", key="f5"),),
    )
    response = {
        "status": "success",
        "accepted": True,
        "event_count": 2,
        "resulting_frame_id": 1,
    }
    runtime.session = SimpleNamespace(background_request=lambda *_args, **_kwargs: response)
    with pytest.raises(RuntimeError, match="input batch rejected"):
        runtime.dispatch_input_batch(batch)

    response.update(consumed=True, consumed_frame_count=1)
    assert runtime.dispatch_input_batch(batch) is response


def _write_minimal_luanti_client_sources(repo: Path) -> None:
    game_cpp = repo / "src/client/game.cpp"
    game_cpp.parent.mkdir(parents=True)
    game_cpp.write_text("// client v1\n", encoding="utf-8")
    hidden_source = repo / "irr/src/CIrrDeviceSDL.cpp"
    hidden_source.parent.mkdir(parents=True)
    hidden_source.write_text(
        "// WARPTEST_C1_BACKGROUND_WINDOW\n", encoding="utf-8"
    )


def test_luanti_client_build_provenance_rejects_stale_source_or_binary(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "luanti"
    _write_minimal_luanti_client_sources(repo)
    binary = repo / "bin/luanti"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"client-v1")

    first_identity = luanti_client_source_identity(repo)
    written = _write_luanti_client_build_provenance(repo, binary)
    assert written["source_identity"] == first_identity
    assert _read_luanti_client_build_provenance(repo, binary) is not None

    (repo / "src/client/game.cpp").write_text("// client v2\n", encoding="utf-8")
    assert luanti_client_source_identity(repo) != first_identity
    assert _read_luanti_client_build_provenance(repo, binary) is None

    _write_luanti_client_build_provenance(repo, binary)
    binary.write_bytes(b"old-client-restored-from-cache")
    assert _read_luanti_client_build_provenance(repo, binary) is None


def test_luanti_client_build_provenance_survives_task_clone_relocation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-luanti"
    _write_minimal_luanti_client_sources(source)
    binary = source / "bin/luanti"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"relocatable-client")
    _write_luanti_client_build_provenance(source, binary)

    clone = tmp_path / "task-sandbox/repo"
    shutil.copytree(source, clone)
    cloned_binary = clone / "bin/luanti"

    assert luanti_client_source_identity(clone) == luanti_client_source_identity(
        source
    )
    assert _read_luanti_client_build_provenance(clone, cloned_binary) is not None


def test_luanti_client_override_fails_closed_without_current_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "luanti"
    _write_minimal_luanti_client_sources(repo)
    binary = tmp_path / "cached-luanti"
    binary.write_bytes(b"unknown-client")
    monkeypatch.setenv("LUANTI_BIN", str(binary))

    adapter = LuantiRuntimeAdapter(repo, tmp_path / "report")
    with pytest.raises(RuntimeError, match="valid WarpTest build provenance"):
        adapter._ensure_built(server=False)


def test_luanti_build_and_golden_cache_keys_use_current_client_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "luanti"
    _write_minimal_luanti_client_sources(repo)
    spec = {"benchmark": "luanti", "experiment_seed": 7, "actions": []}

    assert _luanti_build_dir(repo) != repo / "build"
    first = state_loop._oracle_repo_identity(repo, spec)
    assert first == luanti_client_source_identity(repo)
    first_fingerprint = state_loop._oracle_fingerprint(first, spec)

    (repo / "src/client/game.cpp").write_text("// client v2\n", encoding="utf-8")
    second = state_loop._oracle_repo_identity(repo, spec)
    assert second != first
    assert state_loop._oracle_fingerprint(second, spec) != first_fingerprint
