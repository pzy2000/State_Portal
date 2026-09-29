"""Write one golden manifest per frozen task, mirroring the OpenRA task blocks.

The manifest carries exactly the evidence that exists: the published
``before``/``fixed after``/``buggy after`` triplet, the construction metrics from
``goldens.meta.json``, the typed formal-freeze admission the frames came from, the
registered upstream tests, and the semantic expectation each after-frame shows.

Fields that need a benchmark Phase-0 lazarus runtime (target synthesis, action
sequence, ``goal_checks``) are *not* invented: every manifest states its
``runtime_status`` so a reader can tell a visual-evidence contract from an
executable task.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from functools import lru_cache
from typing import Any, Mapping, Sequence

import yaml

from .c1_freeze_catalog_audit import _closure, _load_admission
from .c1_lazarus_golden_publication import resolve_candidate_frames
from .c1_lazarus_task_text import task_text
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


MANIFEST_SCHEMA = "c1-lazarus-task-goldens-v1"
MANIFEST_SCHEMA_VERSION = 1
_PROBE_PENDING = "pending_phase0_lazarus_runtime"
# Only these two pilot tasks are accepted by the provider-backed Phase-0 runtime
# in warptest.lazarus_runtime; every other frozen task reports the weaker claim its
# own evidence supports.
_PHASE0_PILOT_TASK_IDS = frozenset(
    {"lz-luanti-11919-vector-from-string", "lz-openra-21453-hotkey-ready-building"}
)
_RUBRIC = {
    "jupyterlab": {
        "must": [
            "A live JupyterLab surface rendered by the candidate build is visible.",
        ],
        "must_not": [
            "A blank frame, a crash overlay, or an unrelated application screen "
            "is shown.",
        ],
        "ignore": [
            "Font antialiasing, caret blink, scrollbar rendering, and window chrome.",
        ],
    },
    "luanti": {
        "must": [
            "A live Luanti client frame rendered by the candidate build is visible.",
        ],
        "must_not": [
            "A blank frame, a crash dialog, or an unrelated application screen "
            "is shown.",
        ],
        "ignore": [
            "Debug and HUD text, lighting, timing, and the exact camera angle.",
        ],
    },
}
_MANIFEST = ArtifactReference(
    "c1_lazarus_task_goldens_manifest/lazarus/v2-roster/current-v1",
    "c1_lazarus_task_goldens_manifest",
    "1.0",
)
_SEMANTIC_KEYS = (
    "authoritative_defect",
    "expected_after",
    "semantic_observation",
    "f2p_selector",
    "p2p_selector",
    "f2p_full_name",
    "p2p_full_name",
    "issue_number",
    "primary_bug_issue",
    "primary_issue",
)


@dataclass(frozen=True)
class TaskManifest:
    task_id: str
    repository_key: str
    change_number: int
    path: Path
    runtime_status: str


def _runtime_binding(
    artifact_root: Path, repository_key: str, task_id: str
) -> tuple[str, dict[str, object] | None]:
    """Report the strongest runtime claim this one task can support.

    The claim is per task, not per benchmark: ``phase0_runtime_available`` belongs
    to the two pilot tasks that the provider-backed Phase-0 runtime accepts,
    ``probe_runtime_available`` to a task whose registered replay can be
    re-executed offline, and ``frozen_evidence_available`` to a task whose checks
    can only be resolved against its admitted Gate 10 observation.
    """

    if task_id in _PHASE0_PILOT_TASK_IDS:
        return "phase0_runtime_available", None
    if repository_key == "jupyterlab":
        from ..lazarus_jupyterlab_probe import PROBE_ADAPTER, probe_bindings

        binding = probe_bindings().get(task_id)
        if binding is None:
            return _PROBE_PENDING, None
        return "probe_runtime_available", _probe_block(
            PROBE_ADAPTER, task_id, binding.replay_steps, live=True
        )
    if repository_key == "luanti":
        from ..lazarus_luanti_probe import LIVE_SOURCE

        binding = _luanti_bindings(artifact_root).get(task_id)
        if binding is None:
            return _PROBE_PENDING, None
        live = binding.source == LIVE_SOURCE
        status = "probe_runtime_available" if live else "frozen_evidence_available"
        return status, _probe_block(
            binding.adapter, task_id, binding.replay_steps, live=live
        )
    return _PROBE_PENDING, None


def _probe_block(
    adapter: str, task_id: str, replay: Sequence[str], *, live: bool
) -> dict[str, object]:
    return {
        "adapter": adapter,
        "spec": f"{task_id}.yaml",
        "registered_replay": list(replay),
        "versions_replayed": ["fixed", "buggy"],
        "re_executed_offline": live,
        "provider_calls_required": False,
    }


@lru_cache(maxsize=4)
def _luanti_bindings(artifact_root: Path) -> Mapping[str, Any]:
    """Cache the Luanti bindings: recovering them walks every task's closure."""

    from ..lazarus_luanti_probe import build_luanti_bindings
    from .c1_lazarus_luanti_specs import luanti_task_ids

    return build_luanti_bindings(artifact_root, task_ids=luanti_task_ids())


def collect_task_facts(
    artifact_root: str | Path, *, repository_key: str, change_number: int
) -> dict[str, Any]:
    """Read the typed semantics, selectors and admission id for one candidate."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    admission = _load_admission(root, repository_key, change_number)
    if admission is None:
        raise RuntimeError(f"{repository_key}#{change_number} has no admission head")
    facts: dict[str, Any] = {"admission_id": admission.reference.artifact_id}
    for _, artifact in sorted(
        _closure(root, admission.reference).items(),
        key=lambda item: item[1].created_at,
    ):
        payload = artifact.payload
        for key in _SEMANTIC_KEYS:
            value = payload.get(key)
            if value not in (None, "", {}, []):
                facts[key] = value
        details = payload.get("details")
        if isinstance(details, dict):
            for key in ("f2p_selector", "p2p_selector", "f2p_full_name", "p2p_full_name"):
                if details.get(key):
                    facts[key] = details[key]
    return facts


def write_task_manifests(
    artifact_root: str | Path,
    *,
    roster_path: str | Path,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Emit every frozen task's golden manifest and record the publication."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("an active infrastructure-audit authorization is required")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    rows = yaml.safe_load(Path(roster_path).read_text(encoding="utf-8"))["candidates"]
    written: list[TaskManifest] = []
    for row in rows:
        repository_key = str(row["repository_key"])
        change_number = int(row["change_number"])
        resolved = resolve_candidate_frames(
            root, repository_key=repository_key, change_number=change_number
        )
        lazarus_dir = repo / "specs" / repository_key / "lazarus"
        visual = lazarus_dir / f"visual_{resolved.task_id}"
        metadata_path = visual / "goldens.meta.json"
        if not metadata_path.is_file():
            raise RuntimeError(f"{resolved.task_id} has no published golden metadata")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        facts = collect_task_facts(
            root, repository_key=repository_key, change_number=change_number
        )
        document = _manifest_document(
            artifact_root=root,
            task_id=resolved.task_id,
            repository_key=repository_key,
            change_number=change_number,
            defect_locus=str(row["defect_locus"]),
            visual_dir=visual.name,
            metadata=metadata,
            facts=facts,
            frame_sources=dict(resolved.sources),
        )
        path = lazarus_dir / f"{resolved.task_id}.goldens.yaml"
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        written.append(
            TaskManifest(
                task_id=resolved.task_id,
                repository_key=repository_key,
                change_number=change_number,
                path=path,
                runtime_status=document["evaluation"]["lazarus"]["runtime_status"],
            )
        )
    payload = {
        "roster_path": str(Path(roster_path)),
        "state": "passed",
        "complete": True,
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "task_count": len(written),
        "manifests": [
            {
                "task_id": item.task_id,
                "repository_key": item.repository_key,
                "change_number": item.change_number,
                "path": str(item.path.relative_to(repo)),
                "runtime_status": item.runtime_status,
            }
            for item in written
        ],
        "phase0_pilot_task_ids": sorted(_PHASE0_PILOT_TASK_IDS),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_MANIFEST.artifact_id,
        artifact_kind=_MANIFEST.artifact_kind,
        semantic_version=_MANIFEST.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_MANIFEST.artifact_id,
        artifact_kind=_MANIFEST.artifact_kind,
        semantic_version=_MANIFEST.semantic_version,
    )


def _manifest_document(
    *,
    artifact_root: Path,
    task_id: str,
    repository_key: str,
    change_number: int,
    defect_locus: str,
    visual_dir: str,
    metadata: Mapping[str, Any],
    facts: Mapping[str, Any],
    frame_sources: Mapping[str, str],
) -> dict[str, Any]:
    rubric = _RUBRIC.get(repository_key, _RUBRIC["jupyterlab"])
    issue = (
        facts.get("primary_bug_issue")
        or facts.get("issue_number")
        or facts.get("primary_issue")
    )
    defect = facts.get("authoritative_defect")
    fixed_state, buggy_state = _states(facts)
    goal_line = (
        f"The registered replay reaches the goal state: {fixed_state}."
        if fixed_state
        else "The registered replay reaches the goal state of the authoritative fix."
    )
    defect_line = (
        f"The registered replay reaches the defect signature: {buggy_state}."
        if buggy_state
        else "The registered replay reaches the registered defect signature."
    )
    # The registered visual oracle grades the *before* frame in
    # warptest.lazarus_trial, so an authored task describes that frame here and
    # each branch's own after frame below.
    authored = task_text(task_id)
    before_description = (
        authored.description("before")
        if authored is not None
        else (defect or f"The candidate surface for {repository_key}#{change_number}.")
    )
    before_rubric = (
        authored.rubric("before")
        if authored is not None
        else {
            "must": [*rubric["must"], goal_line],
            "must_not": list(rubric["must_not"]),
            "ignore": list(rubric["ignore"]),
        }
    )
    if authored is not None:
        goal_line = authored.description("fixed")
        defect_line = authored.description("buggy")
    fixed_rubric = (
        authored.rubric("fixed")
        if authored is not None
        else {
            "must": [*rubric["must"], goal_line],
            "must_not": list(rubric["must_not"]),
            "ignore": list(rubric["ignore"]),
        }
    )
    buggy_rubric = (
        authored.rubric("buggy")
        if authored is not None
        else {
            "must": [*rubric["must"], defect_line],
            "must_not": list(rubric["must_not"]),
            "ignore": list(rubric["ignore"]),
        }
    )
    runtime_status, probe = _runtime_binding(
        artifact_root, repository_key, task_id
    )
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "task_id": task_id,
        "benchmark": repository_key,
        "candidate": {
            "repository_key": repository_key,
            "change_number": change_number,
            "issue_number": issue,
            "defect_locus": defect_locus,
        },
        "authoritative_defect": defect,
        "provenance": {
            "formal_freeze_admission": facts["admission_id"],
            "frame_sources": frame_sources,
            "goldens_metadata": f"{visual_dir}/goldens.meta.json",
            "before_equivalence": metadata.get("before_equivalence"),
            "after_differential": metadata.get("after_differential"),
            "golden_policy": metadata.get("policy"),
            "captured_by": metadata.get("source"),
        },
        "evaluation": {
            "visual_oracle": {
                "golden_image": f"{visual_dir}/fixed.after.golden.png",
                "expected_label": "correct",
                "description": before_description,
                "rubric": before_rubric,
                "capture": {"stage": "post_smoke", "wait_ms": 1000, "full_page": False},
            },
            "lazarus": {
                "schema_version": 1,
                "runtime_status": runtime_status,
                **({"probe": probe} if probe is not None else {}),
                "upstream_tests": {
                    "fail_to_pass": _selectors(facts, "f2p"),
                    "pass_to_pass": _selectors(facts, "p2p"),
                },
                "goldens": {
                    "before": f"{visual_dir}/before.golden.png",
                    "buggy_before": f"{visual_dir}/buggy.before.golden.png",
                    "fixed": {
                        "after": f"{visual_dir}/fixed.after.golden.png",
                        "after_description": goal_line,
                        "after_rubric": fixed_rubric,
                    },
                    "buggy": {
                        "after": f"{visual_dir}/buggy.after.golden.png",
                        "after_description": defect_line,
                        "after_rubric": buggy_rubric,
                    },
                },
            },
        },
    }


def _states(facts: Mapping[str, Any]) -> tuple[str | None, str | None]:
    expected = facts.get("expected_after")
    if isinstance(expected, dict) and {"fixed", "buggy"} <= set(expected):
        return _render(expected["fixed"]), _render(expected["buggy"])
    observation = facts.get("semantic_observation")
    if observation not in (None, "", {}):
        return None, _render(observation)
    return None, None


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value or "an empty result"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _selectors(facts: Mapping[str, Any], phase: str) -> list[str]:
    values = [
        facts.get(f"{phase}_full_name"),
        facts.get(f"{phase}_selector"),
    ]
    return [value for value in values if isinstance(value, str) and value]


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "MANIFEST_SCHEMA",
    "MANIFEST_SCHEMA_VERSION",
    "TaskManifest",
    "collect_task_facts",
    "write_task_manifests",
]
