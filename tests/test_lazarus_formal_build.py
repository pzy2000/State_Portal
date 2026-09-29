from __future__ import annotations

import re
from pathlib import Path

import pytest

from warptest.lazarus_formal_build import (
    JUPYTER_FORMAL_BUILD_SCOPES,
    formal_construction_prepare_commands,
    formal_construction_runtime_environment,
    create_formal_prebuild_callback,
)


def test_every_jupyter_candidate_has_a_hash_bound_scoped_build() -> None:
    # Three registered candidates are screening rows outside the formal roster;
    # the rest are the fifteen formal JupyterLab rows.
    assert set(JUPYTER_FORMAL_BUILD_SCOPES) >= {
        "lz-jupyterlab-12863-dialog-getvalue-error",
        "lz-jupyterlab-16943-markdown-regex-replace-all",
        "lz-jupyterlab-17700-statusbar-tab-order",
        "lz-jupyterlab-17369-stream-chunk-merge",
        "lz-jupyterlab-18563-python-comment-fallback",
        "lz-jupyterlab-18572-single-char-code-render",
        "lz-jupyterlab-15634-markdown-focus-render",
        "lz-jupyterlab-13657-replace-selection-command",
        "lz-jupyterlab-16343-shift-tab-tooltip",
        "lz-jupyterlab-17701-empty-cell-execution-state",
        "lz-jupyterlab-17194-cell-metadata-removal",
    }
    for task_id, scopes in JUPYTER_FORMAL_BUILD_SCOPES.items():
        assert scopes, task_id
        callback = create_formal_prebuild_callback(task_id)
        assert re.fullmatch(r"[0-9a-f]{64}", callback.lazarus_build_fingerprint)


def test_formal_build_registry_rejects_unimplemented_task() -> None:
    with pytest.raises(ValueError, match="no registered prebuild"):
        create_formal_prebuild_callback("lz-blender-unimplemented")


def test_formal_construction_rebuilds_scopes_after_test_overlay() -> None:
    commands = formal_construction_prepare_commands(
        "lz-jupyterlab-18572-single-char-code-render",
        test_cwds=("packages/codemirror", "packages/coreutils"),
    )

    assert len(commands) == 3
    assert commands[0].id == "jupyterlab-scoped-build-codemirror"
    assert commands[0].argv == (
        "/opt/miniconda3/bin/jlpm",
        "run",
        "build:packages:scope",
        "--scope",
        "@jupyterlab/codemirror",
    )
    assert commands[0].cwd == "."
    assert commands[1].id == "jupyterlab-test-build-codemirror"
    assert commands[1].argv == (
        "/opt/miniconda3/bin/jlpm",
        "run",
        "build:test",
    )
    assert commands[1].cwd == "packages/codemirror"
    assert commands[1].failure_class == "version_incompatible"
    assert commands[2].id == "jupyterlab-test-build-coreutils"
    assert commands[2].cwd == "packages/coreutils"
    assert commands[2].failure_class == "version_incompatible"


def test_formal_galata_runtime_uses_the_prepared_clone_and_local_jupyter_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeRuntime:
        app_url = "http://127.0.0.1:31888/lab"

        def __init__(
            self,
            repo: Path,
            report_dir: Path,
            *,
            python_executable: str | None = None,
            galata_mode: bool = False,
        ) -> None:
            captured["repo"] = repo
            captured["report_dir"] = report_dir
            captured["python"] = python_executable
            captured["galata_mode"] = galata_mode

        def __enter__(self) -> "FakeRuntime":
            captured["entered"] = True
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        "warptest.lazarus_formal_build.JupyterLabRuntimeAdapter", FakeRuntime
    )
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    factory = formal_construction_runtime_environment(
        "lz-jupyterlab-18563-python-comment-fallback",
        test_cwds=("galata",),
    )

    assert factory is not None
    with factory(prepared) as environment:
        assert environment == {"TARGET_URL": "http://127.0.0.1:31888"}
        assert captured["repo"] == prepared
        assert captured["report_dir"] == prepared / ".warptest/lazarus/galata-runtime"
        assert captured["python"] == "/opt/miniconda3/bin/python"
        # Galata waits on the exposed application object, so the runtime must be
        # started in Galata mode or every test times out before asserting.
        assert captured["galata_mode"] is True
        assert captured["entered"] is True
    assert captured["closed"] is True


def test_non_galata_jupyter_construction_does_not_start_a_runtime() -> None:
    assert (
        formal_construction_runtime_environment(
            "lz-jupyterlab-18572-single-char-code-render",
            test_cwds=("packages/codemirror",),
        )
        is None
    )


def test_luanti_multiplayer_runtime_pins_devtest_in_the_prepared_clone(
    tmp_path: Path,
) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()

    factory = formal_construction_runtime_environment(
        "lz-luanti-14396-metadata-set-string-nil",
        test_cwds=(".",),
    )

    assert factory is not None
    with factory(prepared) as environment:
        assert environment == {"gameid": "devtest"}

    with pytest.raises(ValueError, match="clone root"):
        formal_construction_runtime_environment(
            "lz-luanti-14396-metadata-set-string-nil",
            test_cwds=("packages/notebook",),
        )
