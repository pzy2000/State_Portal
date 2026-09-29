"""Executable Luanti bindings for rows that never went through a freeze sprint.

:func:`warptest.lazarus_luanti_states.extract_luanti_lazarus_states` recovers a
frozen row's separating pair from its Gate 1--13 admission closure.  An expansion
row has no admission, so that reader cannot bind it and the row would stay
undispatchable.

The pair is still recoverable from evidence rather than prose.  Publishing an
expansion row requires a passed live construction gate, and that gate is issued
only after the golden capture observed both materialized versions and recorded
what each produced.  Those two recorded observations are this module's source:

* ``defects/<task>/capture/{fixed,buggy}-after.json`` hold the observations;
* ``defects/<task>/formal-v1/gate.json`` is the receipt that graded them.

The pair is therefore prior *observed* evidence, and a trial still re-runs the
replay live and compares against it.  Nothing here reads the oracle spec, so a
spec cannot become its own justification.
"""

from __future__ import annotations

import copy
import json
import platform
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from .c1_sandbox import strict_clone_tree
from .lazarus_luanti_goldens import ensure_version_app_bundle
from .lazarus_luanti_probe import (
    LIVE_SOURCE,
    LuantiLazarusProbeError,
    LuantiProbeBinding,
)
from .lazarus_luanti_states import LuantiLazarusStates
from .lazarus_probes import luanti_pr14396_probe
from .luanti_runtime import LuantiRuntimeAdapter


# Expansion rows whose replay this module can drive.  A change stays out of the
# registry until its replay is genuinely re-runnable.
LUANTI_EXPANSION_CHANGES: dict[int, str] = {
    14396: "lz-luanti-14396-metadata-set-string-nil",
}
_EXPANSION_PROBES: dict[int, Callable[[], dict[str, Any]]] = {
    14396: luanti_pr14396_probe,
}
_EXPANSION_REPLAY = (
    "start_candidate_binary_as_headless_devtest_server",
    "restore_registered_candidate_start",
    "apply_registered_metadata_setter",
    "read_marker_node_name_from_the_candidate_world",
)
# The worldmod reports a node assertion either as a match or as a mismatch, and
# the observed name is the tail of both forms.
_MATCH_DETAIL = re.compile(r"=\s*(?P<node>[a-z0-9_]+:[a-z0-9_]+)\s*$")
_MISMATCH_DETAIL = re.compile(r",\s*got\s+(?P<node>[a-z0-9_]+:[a-z0-9_]+)\s*$")
_VERSIONS = ("fixed", "buggy")
_STATES_LAYOUT = "expansion_capture_receipts"
_PROVENANCE = "live_construction_gate (no freeze admission)"
# The spec names this field in its goal and defect checks.
_AUDIT_FIELD = "marker_node"


class LuantiExpansionStateError(RuntimeError):
    """Raised when an expansion row carries no usable observed pair."""


def observed_marker_node(report: Mapping[str, Any]) -> str:
    """Read the node a Luanti checkpoint report observed at the marker."""

    details = [
        str(check.get("detail") or "")
        for check in _named_checks(report)
        if str(check.get("name") or "").startswith("assertion[luanti_node_is]")
    ]
    if len(details) != 1:
        raise LuantiExpansionStateError(
            "a Luanti observation must record exactly one node assertion"
        )
    detail = details[0]
    for pattern in (_MISMATCH_DETAIL, _MATCH_DETAIL):
        found = pattern.search(detail)
        if found is not None:
            return found.group("node")
    raise LuantiExpansionStateError(
        f"Luanti node assertion detail names no node: {detail!r}"
    )


def _named_checks(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            if isinstance(node.get("name"), str) and "status" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(report)
    return found


def expansion_luanti_states(
    repository_root: str | Path, *, change_number: int
) -> LuantiLazarusStates:
    """Recover an expansion row's separating pair from its published evidence."""

    task_id = LUANTI_EXPANSION_CHANGES.get(change_number)
    if task_id is None:
        raise LuantiExpansionStateError(
            f"luanti#{change_number} is not a registered expansion row"
        )
    root = Path(repository_root).expanduser().resolve()
    defects = root / "benchmarks/p5/lazarus/defects" / task_id
    gate_path = defects / "formal-v1/gate.json"
    if not gate_path.is_file():
        raise LuantiExpansionStateError(f"{task_id} has no published gate report")
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if gate.get("status") != "passed":
        raise LuantiExpansionStateError(f"{task_id} gate report did not pass")

    states: dict[str, str] = {}
    sources: list[str] = []
    for version in _VERSIONS:
        receipt = defects / "capture" / f"{version}-after.json"
        if not receipt.is_file():
            raise LuantiExpansionStateError(
                f"{task_id} has no {version} capture receipt; the pair must come "
                "from a recorded observation"
            )
        states[version] = observed_marker_node(
            json.loads(receipt.read_text(encoding="utf-8"))
        )
        sources.append(str(receipt.relative_to(root)))
    if states["fixed"] == states["buggy"]:
        raise LuantiExpansionStateError(
            f"{task_id} capture receipts do not separate the two versions"
        )

    record = json.loads((defects / "formal-v1/upstream.json").read_text(encoding="utf-8"))
    tests = record.get("tests", [])
    return LuantiLazarusStates(
        change_number=change_number,
        fixed={_AUDIT_FIELD: states["fixed"]},
        buggy={_AUDIT_FIELD: states["buggy"]},
        source_artifact_ids=(*sources, str(gate_path.relative_to(root))),
        layout=_STATES_LAYOUT,
        fail_to_pass=tuple(
            str(test["id"]) for test in tests if test.get("kind") == "fail_to_pass"
        ),
        pass_to_pass=tuple(
            str(test["id"]) for test in tests if test.get("kind") == "pass_to_pass"
        ),
        admission_id=_PROVENANCE,
    )


def build_luanti_expansion_binding(
    repository_root: str | Path,
    *,
    change_number: int,
    prebuilt_root: str | Path | None = None,
    sandbox_root: str | Path | None = None,
) -> LuantiProbeBinding:
    """Bind one expansion row to a live headless replay of its own client."""

    task_id = LUANTI_EXPANSION_CHANGES.get(change_number)
    if task_id is None:
        raise LuantiExpansionStateError(
            f"luanti#{change_number} is not a registered expansion row"
        )
    root = Path(repository_root).expanduser().resolve()
    prebuilt = Path(
        prebuilt_root
        if prebuilt_root is not None
        else root / ".warptest-cache/lazarus/prebuilt"
    ).expanduser().resolve()
    sandbox = Path(
        sandbox_root
        if sandbox_root is not None
        else root / ".warptest-cache/lazarus/expansion-probe"
    ).expanduser().resolve()
    states = expansion_luanti_states(root, change_number=change_number)
    probe = _EXPANSION_PROBES[change_number]

    def observe(_artifact_root: Path, version: str) -> dict[str, str]:
        return _run_headless_replay(
            task_id=task_id,
            version=version,
            spec=probe(),
            prebuilt=prebuilt,
            sandbox=sandbox,
        )

    return LuantiProbeBinding(
        task_id=task_id,
        change_number=change_number,
        source=LIVE_SOURCE,
        replay_steps=_EXPANSION_REPLAY,
        states=states,
        observe=observe,
    )


def _run_headless_replay(
    *,
    task_id: str,
    version: str,
    spec: dict[str, Any],
    prebuilt: Path,
    sandbox: Path,
) -> dict[str, str]:
    """Replay one materialized version headlessly and read its marker node."""

    if version not in _VERSIONS:
        raise LuantiExpansionStateError("probe version must be fixed or buggy")
    source = prebuilt / task_id / version
    if not source.is_dir():
        raise LuantiLazarusProbeError(
            f"{task_id} {version} is not materialized: {source}"
        )
    sandbox.mkdir(parents=True, exist_ok=True)
    trial_root = sandbox / f"{task_id}-{version}-trial"
    output = sandbox / f"{task_id}-{version}-output"
    # The clone only supplies sources and the built client, and the engine
    # builds its world under the output directory, so reusing it across runs
    # keeps a repeat replay from rebuilding the client from scratch.
    if not trial_root.exists():
        strict_clone_tree(source, trial_root)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    if platform.system() == "Darwin":
        # A macOS client run resolves the app bundle, and the bundle a version
        # inherits was built before the pair existed.  Replace it with one built
        # from this version's own sources or the replay would observe the same
        # engine for both.
        bundle = ensure_version_app_bundle(
            source,
            task_id=task_id,
            version=version,
            cache_root=sandbox.parent / "app-bundles",
        )
        destination = trial_root / "build-app/out/luanti.app"
        shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(bundle, destination)
    with LuantiRuntimeAdapter(
        trial_root,
        output,
        headless=True,
        # No frame is needed, but the replay must still run the client: the
        # formal Luanti recipe rebuilds only ``bin/luanti``, so the dedicated
        # server binary is shared by both versions and would observe the same
        # state for each.
        visual_capture=False,
        require_client=True,
        sync_builtin_from_repo=True,
        daylight_mode="fixed_noon",
        experiment_seed=0,
    ) as runtime:
        result = runtime.run_checkpoint(copy.deepcopy(spec), output / f"{version}.yaml")
    # The returned value is a summary; the per-check details the marker is read
    # from are written to the report the run names.
    report_path = Path(str(result.get("report_path") or ""))
    if not report_path.is_file():
        raise LuantiLazarusProbeError(
            f"{task_id} {version} replay produced no checkpoint report"
        )
    node = observed_marker_node(json.loads(report_path.read_text(encoding="utf-8")))
    return {_AUDIT_FIELD: node}


__all__ = [
    "LUANTI_EXPANSION_CHANGES",
    "LuantiExpansionStateError",
    "build_luanti_expansion_binding",
    "expansion_luanti_states",
    "observed_marker_node",
]
