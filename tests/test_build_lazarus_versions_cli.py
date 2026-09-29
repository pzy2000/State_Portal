from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_lazarus_versions.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("build_lazarus_versions_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registered_luanti_unittest_task_rejects_generic_build_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    monkeypatch.setattr(
        module, "materialize_lazarus_versions", lambda *_args, **_kwargs: {}
    )

    with pytest.raises(ValueError, match="requires --formal-registered-build"):
        module.main(
            [
                "--workspace",
                str(ROOT),
                "--request",
                "benchmarks/p5/lazarus/authoring/"
                "lz-luanti-14586-full-crafting-shift-move.version-request.json",
                "--build-command-json",
                '["not-a-real-build"]',
            ]
        )


def test_registered_luanti_unittest_task_uses_the_registered_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    captured: dict[str, object] = {}

    def fake_materialize(request: object, **kwargs: object) -> dict[str, object]:
        captured["request"] = request
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(module, "materialize_lazarus_versions", fake_materialize)

    assert (
        module.main(
            [
                "--workspace",
                str(ROOT),
                "--request",
                "benchmarks/p5/lazarus/authoring/"
                "lz-luanti-14555-negative-half-pointing.version-request.json",
                "--formal-registered-build",
            ]
        )
        == 0
    )
    build = captured["build"]
    assert callable(build)
    assert len(str(getattr(build, "lazarus_build_fingerprint", ""))) == 64
