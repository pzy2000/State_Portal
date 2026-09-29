"""Publish one ``before``/``fixed after``/``buggy after`` golden set per frozen task.

Every frozen row already owns real captured frames: the Gate 11 evidence in its
formal freeze admission closure names the files that a candidate-owned client or
browser produced.  Adapters from different sprint phases recorded those file names
under different keys, so this module resolves them structurally and then hands the
four frames to :mod:`warptest.lazarus_goldens`, which is the fail-closed publisher
that checks capture quality, start equivalence and the after differential before it
writes ``visual/*.golden.png`` plus ``goldens.meta.json``.

Constructed oracle frames (``*_golden``) are never published: they are hypotheses
built inside the browser, not captured states.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from ..lazarus_goldens import LazarusGoldenFrame, register_lazarus_golden_set
from .c1_freeze_catalog_audit import _closure, _load_admission
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ROLES = ("fixed_before", "fixed_after", "buggy_before", "buggy_after")
_SKIP_FRAGMENTS = ("golden", "diff", "plan")
# Luanti clients paint a volatile debug/HUD band at the top of every frame, while
# the browser frames are deterministic, so the volatile mask is benchmark scoped.
_IGNORE_TOP_FRACTION = {"luanti": 0.25, "jupyterlab": 0.0}
# Task identities are the ones the typed artifacts already carry; only the seven
# candidates this sprint onboarded needed a new identity, and they follow the same
# ``lz-<benchmark>-<change>-<slug>`` shape.
# Task identities are the ones the typed artifacts already carry; only the seven
# candidates this sprint onboarded needed a new identity, and they follow the same
# ``lz-<benchmark>-<change>-<slug>`` shape.
_TASK_IDS = {
    ("jupyterlab", 17369): "lz-jupyterlab-17369-stream-chunk-merge",
    ("jupyterlab", 17700): "lz-jupyterlab-17700-statusbar-tab-order",
    ("jupyterlab", 18518): "lz-jupyterlab-18518-xsrf-duplicate-cookie",
    ("jupyterlab", 18572): "lz-jupyterlab-18572-single-char-code-render",
    ("jupyterlab", 18579): "lz-jupyterlab-18579-read-only-cell-split",
    ("jupyterlab", 18662): "lz-jupyterlab-18662-inline-completer-reserved-lines",
    ("jupyterlab", 18938): "lz-jupyterlab-12863-dialog-getvalue-error",
    ("jupyterlab", 18992): "lz-jupyterlab-18992-collapsed-heading-visibility",
    ("jupyterlab", 19121): "lz-jupyterlab-19121-shortcut-settings-refresh",
    ("jupyterlab", 19136): "lz-jupyterlab-19136-horizontal-activity-bar-restore",
    ("jupyterlab", 19227): "lz-jupyterlab-19227-reactive-toolbar-positions",
    ("luanti", 8704): "lz-luanti-8704-minimal-craft-result",
    ("luanti", 9572): "lz-luanti-9572-vector-rotate",
    ("luanti", 9722): "lz-luanti-9722-reversible-number-serialize",
    ("luanti", 11352): "lz-luanti-11352-map-settings-hierarchy",
    ("luanti", 11866): "lz-luanti-11866-mapgen-boundary",
    ("luanti", 12602): "lz-luanti-12602-media-override",
    ("luanti", 13132): "lz-luanti-13132-empty-separator",
    ("luanti", 14072): "lz-luanti-14072-oversized-stack",
    ("luanti", 14464): "lz-luanti-14464-iconv-state-reset",
    ("luanti", 14555): "lz-luanti-14555-negative-half-pointing",
    ("luanti", 14757): "lz-luanti-14757-math-round",
    ("luanti", 15408): "lz-luanti-15408-collision-acceleration",
    ("luanti", 15519): "lz-luanti-15519-russian-plural-form",
}
_TASK_ID = re.compile(r"^lz-[a-z0-9]+(?:-[a-z0-9]+)*$")
_DEFAULT_AFTER_FLOOR = 0.001
# One frozen task shows its outcome as a short localized string inside a full
# client frame: "яблока" versus "apples" is legible but covers few pixels, so its
# floor is lowered explicitly and recorded in the published policy instead of
# being waived silently.
_RELAXED_AFTER_FLOOR = {
    ("luanti", 15519): (
        0.0005,
        "localized plural string differs inside a full 800x600 client frame",
    ),
}
_PUBLICATION = ArtifactReference(
    "c1_lazarus_golden_publication/lazarus/v2-roster/current-v1",
    "c1_lazarus_golden_publication",
    "1.0",
)


@dataclass(frozen=True)
class CandidateFrames:
    """The four captured frames a golden triplet is published from."""

    repository_key: str
    change_number: int
    task_id: str
    frames: Mapping[str, Path]
    sources: Mapping[str, str]

    def frame(self, role: str) -> Path:
        return self.frames[role]


def resolve_candidate_frames(
    artifact_root: str | Path, *, repository_key: str, change_number: int
) -> CandidateFrames:
    """Find the newest real capture for each of the four required frame roles."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    task_id = _TASK_IDS.get((repository_key, change_number))
    if task_id is None or not _TASK_ID.fullmatch(task_id):
        raise RuntimeError(f"no task id registered for {repository_key}#{change_number}")
    admission = _load_admission(root, repository_key, change_number)
    if admission is None:
        raise RuntimeError(f"{repository_key}#{change_number} has no admission head")
    closure = _closure(root, admission.reference)
    best: dict[str, tuple[str, Path, str]] = {}
    for reference, artifact in sorted(
        closure.items(), key=lambda item: item[1].created_at
    ):
        for role, relative in _classify(artifact):
            path = root / PurePosixPath(relative)
            if not path.is_file():
                continue
            best[role] = (artifact.created_at, path, reference.artifact_id)
    missing = [role for role in _ROLES if role not in best]
    if missing:
        raise RuntimeError(
            f"{repository_key}#{change_number} has no captured frame for {missing}"
        )
    return CandidateFrames(
        repository_key=repository_key,
        change_number=change_number,
        task_id=task_id,
        frames={role: best[role][1] for role in _ROLES},
        sources={role: best[role][2] for role in _ROLES},
    )


def _classify(artifact: StableArtifact) -> Iterable[tuple[str, str]]:
    """Yield ``(role, relative_path)`` for every captured frame in one payload."""

    kind = artifact.reference.artifact_kind
    if "gate11" not in kind and "observation" not in kind and "attempt" not in kind:
        return ()
    variant = artifact.payload.get("variant")
    found: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, str):
            if not value.endswith(".png"):
                return
            key = prefix.lower()
            if any(fragment in key for fragment in _SKIP_FRAGMENTS):
                return
            phase = (
                "after"
                if "after" in key
                else "before"
                if "before" in key
                else None
            )
            if phase is None:
                return
            side = (
                "fixed"
                if "fixed" in key
                else "buggy"
                if "buggy" in key
                else str(variant) if variant in {"fixed", "buggy"} else None
            )
            if side is None:
                if phase == "before" and "common" in key:
                    found.append(("fixed_before", value))
                    found.append(("buggy_before", value))
                return
            found.append((f"{side}_{phase}", value))
            if phase == "before" and "common" in key:
                other = "buggy" if side == "fixed" else "fixed"
                found.append((f"{other}_before", value))
            return
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(f"{prefix}.{index}" if prefix else str(index), item)

    walk("", dict(artifact.payload))
    return found


def publish_task_goldens(
    artifact_root: str | Path,
    *,
    roster_path: str | Path,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Publish every roster row's golden triplet and record the result."""

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
    reports = root / "private/lazarus-golden-reports"
    reports.mkdir(parents=True, mode=0o700, exist_ok=True)
    published: list[dict[str, Any]] = []
    for row in rows:
        repository_key = str(row["repository_key"])
        change_number = int(row["change_number"])
        resolved = resolve_candidate_frames(
            root, repository_key=repository_key, change_number=change_number
        )
        destination = (
            repo / "specs" / repository_key / "lazarus" / f"visual_{resolved.task_id}"
        )
        if destination.exists():
            published.append(
                {
                    "task_id": resolved.task_id,
                    "repository_key": repository_key,
                    "change_number": change_number,
                    "state": "already_published",
                    "directory": str(destination.relative_to(repo)),
                }
            )
            continue
        frames = {
            role: LazarusGoldenFrame(
                image=resolved.frame(role),
                report=_write_report(reports, resolved, role),
                expected_semantics_ok=True,
            )
            for role in _ROLES
        }
        floor, relaxation = _RELAXED_AFTER_FLOOR.get(
            (repository_key, change_number), (_DEFAULT_AFTER_FLOOR, None)
        )
        result = register_lazarus_golden_set(
            task_id=resolved.task_id,
            repository_root=repo,
            output_dir=destination,
            fixed_before=frames["fixed_before"],
            buggy_before=frames["buggy_before"],
            fixed_after=frames["fixed_after"],
            buggy_after=frames["buggy_after"],
            ignore_top_fraction=_IGNORE_TOP_FRACTION.get(repository_key, 0.0),
            after_min_changed_pixel_ratio=floor,
        )
        published.append(
            {
                "task_id": resolved.task_id,
                "repository_key": repository_key,
                "change_number": change_number,
                "state": "published",
                "directory": str(destination.relative_to(repo)),
                "frame_sources": dict(resolved.sources),
                "before_metrics": result.before_metrics,
                "after_metrics": result.after_metrics,
                "after_min_changed_pixel_ratio": floor,
                "relaxed_after_floor_reason": relaxation,
            }
        )
    payload = {
        "roster_path": str(Path(roster_path)),
        "state": "passed",
        "complete": True,
        "task_count": len(published),
        "published_task_count": sum(
            1 for item in published if item["state"] == "published"
        ),
        "tasks": published,
        "published_frames": ["before", "fixed.after", "buggy.after"],
        "default_after_min_changed_pixel_ratio": _DEFAULT_AFTER_FLOOR,
        "relaxed_after_floor_tasks": [
            {
                "repository_key": key[0],
                "change_number": key[1],
                "after_min_changed_pixel_ratio": value[0],
                "reason": value[1],
            }
            for key, value in _RELAXED_AFTER_FLOOR.items()
        ],
        "constructed_oracle_frames_published": False,
        "volatile_top_mask_by_repository": dict(_IGNORE_TOP_FRACTION),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PUBLICATION.artifact_id,
        artifact_kind=_PUBLICATION.artifact_kind,
        semantic_version=_PUBLICATION.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_PUBLICATION.artifact_id,
        artifact_kind=_PUBLICATION.artifact_kind,
        semantic_version=_PUBLICATION.semantic_version,
    )


def _write_report(reports: Path, resolved: CandidateFrames, role: str) -> Path:
    """Persist the typed source of one frame as its capture report."""

    directory = reports / resolved.task_id
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / f"{role}.report.json"
    path.write_text(
        json.dumps(
            {
                "task_id": resolved.task_id,
                "repository_key": resolved.repository_key,
                "change_number": resolved.change_number,
                "role": role,
                "frame_path": str(resolved.frame(role)),
                "source_artifact_id": resolved.sources[role],
                "expected_semantics_ok": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CandidateFrames",
    "publish_task_goldens",
    "resolve_candidate_frames",
]
