"""Registered prebuild callbacks for the formal Lazarus population."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .jupyterlab_runtime import JupyterLabRuntimeAdapter
from .lazarus_build import create_prebuild_callback
from .lazarus_upstream import (
    ConstructionCommandDefinition,
    ConstructionRuntimeEnvironmentFactory,
)


_JLPM = "/opt/miniconda3/bin/jlpm"
_JUPYTER_RUNTIME_PYTHON = "/opt/miniconda3/bin/python"
_LUANTI_CMAKE = "/opt/miniconda3/bin/cmake"
_LUANTI_UNITTEST_BUILD_DIR = "build-lazarus-unittests"

# These candidates execute their authoritative F2P test through the Luanti
# client binary, as ``bin/luantiserver --run-unittests --test-module <module>``.
# They must therefore use this registered configure + build recipe rather than a
# caller-provided generic command.  The recipe is self-contained in the
# disposable version clone: it does not fetch sources, reuse a developer build
# directory, or refer to an arbitrary binary on PATH.
LUANTI_FORMAL_UNITTEST_TASK_IDS = frozenset(
    {
        "lz-luanti-14586-full-crafting-shift-move",
        "lz-luanti-14555-negative-half-pointing",
        "lz-luanti-11352-map-settings-hierarchy",
        "lz-luanti-11866-mapgen-boundary",
        "lz-luanti-12602-media-override",
        "lz-luanti-14072-oversized-stack",
        "lz-luanti-14464-iconv-state-reset",
        "lz-luanti-15408-collision-acceleration",
    }
)
# This historical regression is exercised by the upstream multiplayer/devtest
# script rather than ``--run-unittests``.  Its materialized versions start at
# the pinned Luanti checkout, whose script resolves the in-place ``luanti``
# binary.  Do not route it through the C++ unit-test recipe.
LUANTI_FORMAL_MULTIPLAYER_TASK_IDS = frozenset(
    {"lz-luanti-14396-metadata-set-string-nil"}
)
LUANTI_FORMAL_TASK_IDS = (
    LUANTI_FORMAL_UNITTEST_TASK_IDS | LUANTI_FORMAL_MULTIPLAYER_TASK_IDS
)
_LUANTI_UNITTEST_CONFIGURE = (
    _LUANTI_CMAKE,
    "-S",
    ".",
    "-B",
    _LUANTI_UNITTEST_BUILD_DIR,
    "-G",
    "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_CLIENT=TRUE",
    "-DBUILD_SERVER=FALSE",
    "-DBUILD_UNITTESTS=TRUE",
    "-DBUILD_DOCUMENTATION=FALSE",
    "-DRUN_IN_PLACE=TRUE",
    "-DENABLE_LTO=FALSE",
    "-DBUILD_WITH_TRACY=FALSE",
    "-DENABLE_SOUND=FALSE",
    "-DENABLE_GETTEXT=FALSE",
    "-DENABLE_CURL=FALSE",
    "-DENABLE_CURSES=FALSE",
    "-DENABLE_POSTGRESQL=FALSE",
    "-DENABLE_LEVELDB=FALSE",
    "-DENABLE_REDIS=FALSE",
)
_LUANTI_UNITTEST_BUILD = (
    _LUANTI_CMAKE,
    "--build",
    _LUANTI_UNITTEST_BUILD_DIR,
    "--target",
    "luanti",
    "--parallel",
    "2",
)
_LUANTI_UNITTEST_REMOVE_STALE_CACHE = (
    _LUANTI_CMAKE,
    "-E",
    "rm",
    "-f",
    f"{_LUANTI_UNITTEST_BUILD_DIR}/CMakeCache.txt",
)
_LUANTI_UNITTEST_REMOVE_STALE_CMAKE_FILES = (
    _LUANTI_CMAKE,
    "-E",
    "remove_directory",
    f"{_LUANTI_UNITTEST_BUILD_DIR}/CMakeFiles",
)
_LUANTI_MULTIPLAYER_BUILD_DIR = "build-lazarus-multiplayer"
_LUANTI_MULTIPLAYER_CONFIGURE = (
    _LUANTI_CMAKE,
    "-S",
    ".",
    "-B",
    _LUANTI_MULTIPLAYER_BUILD_DIR,
    "-G",
    "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_CLIENT=TRUE",
    # The upstream CI workflow for this exact test used a client-only binary:
    # that binary provides ``--server`` as well as the null-video client mode.
    "-DBUILD_SERVER=FALSE",
    "-DBUILD_UNITTESTS=TRUE",
    "-DBUILD_DOCUMENTATION=FALSE",
    "-DRUN_IN_PLACE=TRUE",
    "-DENABLE_LTO=FALSE",
    "-DBUILD_WITH_TRACY=FALSE",
    "-DENABLE_SOUND=FALSE",
    "-DENABLE_GETTEXT=FALSE",
    "-DENABLE_CURL=FALSE",
    "-DENABLE_CURSES=FALSE",
    "-DENABLE_POSTGRESQL=FALSE",
    "-DENABLE_LEVELDB=FALSE",
    "-DENABLE_REDIS=FALSE",
)
_LUANTI_MULTIPLAYER_BUILD = (
    _LUANTI_CMAKE,
    "--build",
    _LUANTI_MULTIPLAYER_BUILD_DIR,
    "--target",
    "luanti",
    "--parallel",
    "2",
)
JUPYTER_FORMAL_BUILD_SCOPES: Mapping[str, tuple[str, ...]] = {
    "lz-jupyterlab-12863-dialog-getvalue-error": ("apputils",),
    "lz-jupyterlab-16943-markdown-regex-replace-all": ("cells", "notebook"),
    "lz-jupyterlab-17700-statusbar-tab-order": ("statusbar",),
    "lz-jupyterlab-17369-stream-chunk-merge": ("outputarea",),
    "lz-jupyterlab-18563-python-comment-fallback": ("codemirror-extension",),
    "lz-jupyterlab-18572-single-char-code-render": ("codemirror",),
    "lz-jupyterlab-15634-markdown-focus-render": ("notebook",),
    "lz-jupyterlab-13657-replace-selection-command": ("codemirror",),
    "lz-jupyterlab-16343-shift-tab-tooltip": ("codemirror",),
    "lz-jupyterlab-17701-empty-cell-execution-state": ("cells",),
    "lz-jupyterlab-17194-cell-metadata-removal": ("notebook-extension",),
    # The remaining formal rows.  A row must rebuild at least the packages its
    # inverse patch touches, and may name more when the replay drives a package
    # that only depends on them.
    "lz-jupyterlab-18518-xsrf-duplicate-cookie": ("services",),
    "lz-jupyterlab-18579-read-only-cell-split": ("notebook", "notebook-extension"),
    "lz-jupyterlab-18662-inline-completer-reserved-lines": ("completer",),
    "lz-jupyterlab-18992-collapsed-heading-visibility": ("notebook",),
    "lz-jupyterlab-19121-shortcut-settings-refresh": ("shortcuts-extension",),
    "lz-jupyterlab-19136-horizontal-activity-bar-restore": ("application",),
    "lz-jupyterlab-19227-reactive-toolbar-positions": ("ui-components",),
}

# These commands are intentionally a small coordinator-owned allow-list rather
# than a discovery pass over arbitrary package.json files.  The frozen test
# overlays can change TypeScript or generated parser inputs after the scoped
# package build has completed, so their test build must happen inside the
# isolated construction clone as well.
JUPYTER_FORMAL_TEST_BUILD_COMMANDS: Mapping[str, tuple[str, ...]] = {
    "packages/apputils": (_JLPM, "run", "build:test"),
    "packages/cells": (_JLPM, "run", "build:test"),
    "packages/codemirror": (_JLPM, "run", "build:test"),
    "packages/coreutils": (_JLPM, "run", "build:test"),
    "packages/notebook": (_JLPM, "run", "build:test"),
    "packages/outputarea": (_JLPM, "run", "build:test"),
    "packages/statusbar": (_JLPM, "run", "build:test"),
    "galata": (_JLPM, "run", "build"),
}

# Galata drives a browser against the ``dev_mode`` bundle, not the per-package
# ``lib`` output.  A scoped package build alone leaves that bundle byte
# identical between the fixed and buggy versions, so the browser would execute
# the same application in both and the truth table could never discriminate.
_JUPYTER_DEV_MODE_BUNDLE = ConstructionCommandDefinition(
    id="jupyterlab-dev-mode-bundle",
    argv=(_JLPM, "run", "build"),
    cwd="dev_mode",
    timeout_s=7_200.0,
)


def create_formal_prebuild_callback(
    task_id: str, *, timeout_s: float = 7_200.0
) -> Callable[[str, Path], Mapping[str, Any]]:
    """Return the immutable build recipe registered for one formal task."""

    normalized = str(task_id).strip()
    if normalized in LUANTI_FORMAL_UNITTEST_TASK_IDS:
        return create_prebuild_callback(
            _LUANTI_UNITTEST_BUILD,
            pre_commands=(_LUANTI_UNITTEST_CONFIGURE,),
            # RUN_IN_PLACE binaries locate the runtime tree at ``../builtin``.
            # Preserve the in-place clone-relative binary so that lookup stays
            # inside the materialized version rather than a staged build dir.
            post_commands=(),
            artifact_paths=("bin/luanti",),
            timeout_s=timeout_s,
        )
    if normalized in LUANTI_FORMAL_MULTIPLAYER_TASK_IDS:
        # ``util/test_multiplayer.sh`` resolves this exact path relative to
        # its disposable construction clone.  The historical CMake project
        # writes it in-place; staging or pointing at a developer binary would
        # silently bypass that isolation boundary.
        return create_prebuild_callback(
            _LUANTI_MULTIPLAYER_BUILD,
            pre_commands=(_LUANTI_MULTIPLAYER_CONFIGURE,),
            artifact_paths=("bin/luanti",),
            timeout_s=timeout_s,
        )
    scopes = JUPYTER_FORMAL_BUILD_SCOPES.get(normalized)
    if scopes is None:
        raise ValueError(f"formal task has no registered prebuild callback: {task_id}")
    commands = tuple(_jupyter_scope_command(scope) for scope in scopes)
    return create_prebuild_callback(
        commands[-1],
        pre_commands=commands[:-1],
        artifact_paths=tuple(f"packages/{scope}/lib" for scope in scopes),
        timeout_s=timeout_s,
    )


def formal_construction_prepare_commands(
    task_id: str,
    *,
    test_cwds: Sequence[str] = (),
    timeout_s: float = 7_200.0,
) -> tuple[ConstructionCommandDefinition, ...]:
    """Return scoped builds that must run after frozen tests are overlaid.

    JupyterLab regression tests can introduce generated parser inputs or import
    package output that was produced before the fixed test bytes were copied
    into the disposable construction clone.  Reusing only the prebuilt tree is
    therefore insufficient: rebuild the registered package scopes inside the
    clone after the overlay and before either F2P or P2P is collected.
    """

    normalized = str(task_id).strip()
    if type(timeout_s) is not float or not 0 < timeout_s <= 7_200:
        raise ValueError("formal construction build timeout is invalid")
    normalized_cwds = tuple(dict.fromkeys(str(value).strip() for value in test_cwds))
    if normalized in LUANTI_FORMAL_UNITTEST_TASK_IDS:
        if normalized_cwds not in {(), (".",)}:
            raise ValueError("formal Luanti unit-test build must run from clone root")
        # The C++ F2P source is overlaid *after* the cached version pair is
        # materialized.  A strict clone inherits its CMakeCache from the
        # materialization staging path, which no longer exists by the time the
        # construction clone runs.  Remove only that cache and its generated
        # CMakeFiles directory, then reconfigure and rebuild the registered
        # target in every disposable construction clone so fixed and buggy
        # both execute the same frozen source against their own product bytes.
        return (
            ConstructionCommandDefinition(
                id="luanti-unit-test-remove-stale-cache",
                argv=_LUANTI_UNITTEST_REMOVE_STALE_CACHE,
                cwd=".",
                timeout_s=timeout_s,
            ),
            ConstructionCommandDefinition(
                id="luanti-unit-test-remove-stale-cmake-files",
                argv=_LUANTI_UNITTEST_REMOVE_STALE_CMAKE_FILES,
                cwd=".",
                timeout_s=timeout_s,
            ),
            ConstructionCommandDefinition(
                id="luanti-unit-test-reconfigure",
                argv=_LUANTI_UNITTEST_CONFIGURE,
                cwd=".",
                timeout_s=timeout_s,
            ),
            ConstructionCommandDefinition(
                id="luanti-unit-test-rebuild",
                argv=_LUANTI_UNITTEST_BUILD,
                cwd=".",
                timeout_s=timeout_s,
            ),
        )
    scopes = JUPYTER_FORMAL_BUILD_SCOPES.get(normalized)
    if scopes is None:
        if normalized.startswith("lz-jupyterlab-"):
            raise ValueError(
                f"formal JupyterLab task has no construction build registry: {task_id}"
            )
        return ()
    scope_commands = tuple(
        ConstructionCommandDefinition(
            id=f"jupyterlab-scoped-build-{scope}",
            argv=_jupyter_scope_command(scope),
            cwd=".",
            timeout_s=timeout_s,
        )
        for scope in scopes
    )
    test_commands: list[ConstructionCommandDefinition] = []
    for cwd in normalized_cwds:
        if not cwd:
            raise ValueError("formal construction test build path is invalid")
        command = JUPYTER_FORMAL_TEST_BUILD_COMMANDS.get(cwd)
        if command is None:
            raise ValueError(
                f"formal JupyterLab test build is not registered for: {cwd}"
            )
        command_id = cwd.removeprefix("packages/").replace("/", "-")
        test_commands.append(
            ConstructionCommandDefinition(
                id=f"jupyterlab-test-build-{command_id}",
                argv=command,
                cwd=cwd,
                timeout_s=timeout_s,
                # This command compiles the fixed frozen regression source
                # inside the exact historical product tree.  A bounded
                # non-zero exit is therefore evidence that this candidate's
                # regression cannot be constructed against that version,
                # rather than a generic infrastructure outage.
                failure_class="version_incompatible",
            )
        )
    bundle_commands: tuple[ConstructionCommandDefinition, ...] = ()
    if "galata" in normalized_cwds:
        bundle_commands = (
            ConstructionCommandDefinition(
                id=_JUPYTER_DEV_MODE_BUNDLE.id,
                argv=_JUPYTER_DEV_MODE_BUNDLE.argv,
                cwd=_JUPYTER_DEV_MODE_BUNDLE.cwd,
                timeout_s=timeout_s,
            ),
        )
    return scope_commands + bundle_commands + tuple(test_commands)


def formal_construction_runtime_environment(
    task_id: str,
    *,
    test_cwds: Sequence[str] = (),
) -> ConstructionRuntimeEnvironmentFactory | None:
    """Return the registered local service lifecycle required by a test suite.

    Galata is not a standalone browser test: its fixtures connect to a live
    JupyterLab HTTP server.  The service must point at the already-prepared
    construction clone, be available only while the subset runs, and never
    fall back to a developer workstation's default ``localhost:8888``.
    """

    normalized = str(task_id).strip()
    if normalized in LUANTI_FORMAL_MULTIPLAYER_TASK_IDS:
        normalized_cwds = tuple(
            dict.fromkeys(str(value).strip() for value in test_cwds)
        )
        if normalized_cwds != (".",):
            raise ValueError(
                "formal Luanti multiplayer test must run from its clone root"
            )

        @contextmanager
        def _multiplayer_environment(prepared: Path) -> Iterator[Mapping[str, str]]:
            if not prepared.is_dir() or prepared.is_symlink():
                raise ValueError("formal Luanti multiplayer clone does not exist")
            # The upstream script accepts a lower-case ``gameid`` override.
            # Pin it rather than inheriting an arbitrary host value that could
            # select a different game from the construction clone.
            yield {"gameid": "devtest"}

        return _multiplayer_environment
    scopes = JUPYTER_FORMAL_BUILD_SCOPES.get(normalized)
    if scopes is None:
        if normalized.startswith("lz-jupyterlab-"):
            raise ValueError(
                f"formal JupyterLab task has no construction build registry: {task_id}"
            )
        return None
    normalized_cwds = tuple(dict.fromkeys(str(value).strip() for value in test_cwds))
    if any(not value for value in normalized_cwds):
        raise ValueError("formal construction runtime path is invalid")
    for cwd in normalized_cwds:
        if cwd not in JUPYTER_FORMAL_TEST_BUILD_COMMANDS:
            raise ValueError(
                f"formal JupyterLab test build is not registered for: {cwd}"
            )
    if "galata" not in normalized_cwds:
        return None

    @contextmanager
    def _runtime_environment(prepared: Path) -> Iterator[Mapping[str, str]]:
        if not prepared.is_dir():
            raise ValueError("formal Galata runtime clone does not exist")
        report_dir = prepared / ".warptest/lazarus/galata-runtime"
        with JupyterLabRuntimeAdapter(
            prepared,
            report_dir,
            python_executable=_JUPYTER_RUNTIME_PYTHON,
            galata_mode=True,
        ) as runtime:
            target_url = runtime.app_url.removesuffix("/lab")
            if target_url == runtime.app_url:
                raise RuntimeError("registered JupyterLab runtime has no /lab endpoint")
            yield {"TARGET_URL": target_url}

    return _runtime_environment


def formal_luanti_prebuild_artifact_path(task_id: str) -> str:
    """Return the clone-relative executable promised by a Luanti recipe."""

    normalized = str(task_id).strip()
    if normalized in LUANTI_FORMAL_UNITTEST_TASK_IDS:
        return "bin/luanti"
    if normalized in LUANTI_FORMAL_MULTIPLAYER_TASK_IDS:
        return "bin/luanti"
    raise ValueError(f"no registered Luanti prebuild recipe: {task_id}")


def _jupyter_scope_command(scope: str) -> tuple[str, ...]:
    return (
        _JLPM,
        "run",
        "build:packages:scope",
        "--scope",
        f"@jupyterlab/{scope}",
    )
