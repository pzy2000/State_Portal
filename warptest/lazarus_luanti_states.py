"""Recover each frozen Luanti task's goal and defect state from its own closure.

The 13 frozen Luanti candidates were closed by bespoke Gate 8--13 modules, so their
evidence is spread over several artifact kinds and several payload layouts.  A spec
must still state one separating pair of states, and that pair has to come from the
admitted evidence rather than from prose written after the fact.

Three layouts are recognised, in descending authority:

1. a Gate 10 oracle that names both sides explicitly
   (``fixed_goal_observation`` and ``buggy_defect_observation``);
2. a semantic oracle mapping keyed by version (``semantic_oracle``);
3. per-version attempts whose ``semantic_observation`` is tagged by the version in
   the artifact id.

Extraction fails closed: if no layout yields two different states, the task has no
recoverable pair and the caller must not invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .c1_lazarus_selection.c1_lazarus_task_goldens_manifest import (
    _closure,
    _load_admission,
)


STATE_AUDIT_CHECK = "luanti_lazarus_state_audit"
PROBE_ERROR_CHECK = "no_luanti_mod_errors"
PROBE_ADAPTER = "luanti_lazarus_frozen_evidence_v1"
PROBE_STRATEGY = "luanti_lazarus_replay_validate"
_VERSIONS = ("fixed", "buggy")
# Ordered by authority: a Gate 10 oracle that labels the goal and the defect side
# outranks one that only records the two observations, which in turn outranks a
# version-keyed mapping, which outranks pairing two per-version attempts.
_NAMED_ORACLE_KEYS = (
    ("fixed_goal_observation", "buggy_defect_observation"),
    ("fixed_observation", "buggy_observation"),
)
_VERSION_KEYED_ORACLE_KEYS: tuple[str | None, ...] = (
    "semantic_oracle",
    "raw_semantic_observation_contract",
    "observed",
    None,
)
_TEST_TRUTH = {"fixed": {"f2p": True, "p2p": True}, "buggy": {"f2p": False, "p2p": True}}


class LuantiLazarusStateError(RuntimeError):
    """Raised when a frozen Luanti task carries no separating state pair."""


@dataclass(frozen=True)
class LuantiLazarusStates:
    """One task's recovered pair of states and the evidence it came from."""

    change_number: int
    fixed: Any
    buggy: Any
    source_artifact_ids: tuple[str, ...]
    layout: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    admission_id: str

    def state(self, version: str) -> Any:
        if version not in _VERSIONS:
            raise LuantiLazarusStateError("version must be fixed or buggy")
        return self.fixed if version == "fixed" else self.buggy


def extract_luanti_lazarus_states(
    artifact_root: str | Path, *, change_number: int
) -> LuantiLazarusStates:
    """Recover one frozen Luanti task's separating state pair, or fail closed."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    admission = _load_admission(root, "luanti", change_number)
    if admission is None:
        raise LuantiLazarusStateError(f"luanti#{change_number} has no admission head")
    # A closure can reach artifacts shared with other candidates, and those carry
    # other candidates' selectors and observations.  Scope the evidence to this
    # candidate's own path so a neighbour's test ids can never leak into a spec.
    scope = f"/{change_number}/"
    closure = tuple(
        artifact
        for artifact in sorted(
            _closure(root, admission.reference).values(), key=lambda a: a.created_at
        )
        if scope in artifact.reference.artifact_id
    )
    if not closure:
        raise LuantiLazarusStateError(
            f"luanti#{change_number} closure carries no candidate-scoped evidence"
        )
    tests = _upstream_tests(closure)
    for reader in (_explicit_oracle, _semantic_oracle, _version_tagged_attempts):
        recovered = reader(closure)
        if recovered is None:
            continue
        fixed, buggy, sources, layout = recovered
        if fixed == buggy or fixed in (None, "", {}) or buggy in (None, "", {}):
            continue
        return LuantiLazarusStates(
            change_number=change_number,
            fixed=fixed,
            buggy=buggy,
            source_artifact_ids=sources,
            layout=layout,
            fail_to_pass=tests[0],
            pass_to_pass=tests[1],
            admission_id=admission.reference.artifact_id,
        )
    raise LuantiLazarusStateError(
        f"luanti#{change_number} closure carries no separating state pair"
    )


def _explicit_oracle(
    closure: Sequence[Any],
) -> tuple[Any, Any, tuple[str, ...], str] | None:
    """Read an oracle that names the goal and the defect side in one payload."""

    for goal_key, defect_key in _NAMED_ORACLE_KEYS:
        for artifact in reversed(closure):
            payload = artifact.payload
            if not isinstance(payload, Mapping):
                continue
            fixed = payload.get(goal_key)
            buggy = payload.get(defect_key)
            if fixed is not None and buggy is not None:
                return (
                    fixed,
                    buggy,
                    (artifact.reference.artifact_id,),
                    f"{goal_key}_pair",
                )
    return None


def _semantic_oracle(
    closure: Sequence[Any],
) -> tuple[Any, Any, tuple[str, ...], str] | None:
    """Read an oracle whose two sides are keyed by the materialized version."""

    for key in _VERSION_KEYED_ORACLE_KEYS:
        for artifact in reversed(closure):
            payload = artifact.payload
            if not isinstance(payload, Mapping):
                continue
            value = payload if key is None else payload.get(key)
            if not isinstance(value, Mapping) or not {"fixed", "buggy"} <= set(value):
                continue
            return (
                value["fixed"],
                value["buggy"],
                (artifact.reference.artifact_id,),
                f"{key or 'payload'}_version_keyed",
            )
    return None


def _version_tagged_attempts(
    closure: Sequence[Any],
) -> tuple[Any, Any, tuple[str, ...], str] | None:
    """Pair the newest per-version attempt observations of the same kind."""

    grouped: dict[str, dict[str, tuple[str, Any]]] = {}
    for artifact in closure:
        payload = artifact.payload
        if not isinstance(payload, Mapping):
            continue
        observation = payload.get("semantic_observation")
        if observation in (None, "", {}):
            continue
        artifact_id = artifact.reference.artifact_id
        version = _version_of(artifact_id)
        if version is None:
            continue
        kind = artifact.reference.artifact_kind
        grouped.setdefault(kind, {})[version] = (artifact_id, observation)
    for kind, versions in grouped.items():
        if set(versions) != set(_VERSIONS):
            continue
        fixed_id, fixed = versions["fixed"]
        buggy_id, buggy = versions["buggy"]
        if fixed == buggy:
            continue
        return fixed, buggy, (fixed_id, buggy_id), f"{kind}_version_tagged"
    return None


def _version_of(artifact_id: str) -> str | None:
    parts = [part.lower() for part in artifact_id.split("/")]
    found = [version for version in _VERSIONS if version in parts]
    return found[0] if len(found) == 1 else None


def _upstream_tests(
    closure: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Recover named upstream ids from a fixed/buggy truth table when present."""

    for artifact in reversed(closure):
        payload = artifact.payload
        if not isinstance(payload, Mapping):
            continue
        for key in ("fixed_tests", "truth_table"):
            table = payload.get(key)
            if key == "truth_table" and isinstance(table, Mapping):
                table = table.get("fixed")
            if not isinstance(table, Mapping):
                continue
            named = [str(name) for name in table]
            fail = [name for name in named if name.endswith("-f2p")]
            passing = [name for name in named if name.endswith("-p2p")]
            if fail and passing:
                return tuple(fail), tuple(passing)
    return _selector_tests(closure)


def _selector_tests(
    closure: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fall back to the Gate 5--7 selectors the closure recorded, if any."""

    fail: set[str] = set()
    passing: set[str] = set()
    for artifact in closure:
        payload = artifact.payload
        if not isinstance(payload, Mapping):
            continue
        parts = artifact.reference.artifact_id.split("/")
        selector = payload.get("selector")
        if isinstance(selector, str) and selector:
            # A Gate 5--7 attempt records which role it ran under in its own id.
            if "f2p" in parts:
                fail.add(selector)
            elif "p2p" in parts:
                passing.add(selector)
        sources = [payload]
        details = payload.get("details")
        if isinstance(details, Mapping):
            sources.append(details)
        for source in sources:
            for key in ("f2p_full_name", "f2p_selector"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    fail.add(value)
            for key in ("p2p_full_name", "p2p_selector"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    passing.add(value)
    return tuple(sorted(fail - passing)), tuple(sorted(passing - fail))


def luanti_truth_table() -> dict[str, dict[str, bool]]:
    """Return the fixed/buggy upstream truth table every frozen task satisfies."""

    return {version: dict(row) for version, row in _TEST_TRUTH.items()}


__all__ = [
    "PROBE_ADAPTER",
    "PROBE_ERROR_CHECK",
    "PROBE_STRATEGY",
    "STATE_AUDIT_CHECK",
    "LuantiLazarusStateError",
    "LuantiLazarusStates",
    "extract_luanti_lazarus_states",
    "luanti_truth_table",
]
