"""Build a lightweight candidate readiness index from stable head.json files.

This deliberately does not execute candidates, inspect raw source trees, or
inherit conclusions. It only summarizes the currently persisted typed heads so
candidate workers can choose a bounded next action without rescanning the
artifact tree independently.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


REPOSITORIES = {"luanti", "jupyterlab", "openra", "blender"}
# The active C1 pool is intentionally explicit.  Pruning at the repository /
# change-number levels avoids walking hundreds of thousands of historical
# heads belonging to unrelated candidates.
POOL_CHANGE_NUMBERS = {
    4346,
    5134,
    6587,
    7279,
    7768,
    8019,
    8383,
    8704,
    8712,
    8742,
    9184,
    9187,
    9284,
    9343,
    9572,
    9588,
    9627,
    9722,
    10085,
    10661,
    10788,
    10879,
    11011,
    11352,
    11427,
    11495,
    11515,
    11641,
    11683,
    11763,
    11770,
    11815,
    11866,
    12015,
    12261,
    12377,
    12445,
    12595,
    12602,
    12703,
    12879,
    13132,
    13234,
    13390,
    13773,
    13825,
    14072,
    14085,
    14123,
    14177,
    14217,
    14288,
    14368,
    14423,
    14464,
    14543,
    14586,
    14555,
    14615,
    14631,
    14685,
    14726,
    14757,
    14878,
    15251,
    15408,
    15438,
    15495,
    15519,
    15521,
    15687,
    15768,
    15919,
    16083,
    16158,
    16179,
    16200,
    16553,
    16634,
    16655,
    16773,
    16929,
    17024,
    17187,
    17231,
    # Known JupyterLab runtime candidates from the current roster/pool.
    444,
    574,
    611,
    1250,
    1846,
    2437,
    2469,
    2752,
    3310,
    3662,
    3663,
    3703,
    3867,
    4796,
}
CORE_KINDS = {
    "candidate_source_packet": "source",
    "candidate_test_patch": "patch",
    "source_atomicity": "atomicity",
    "candidate_stage": "manifest",
}


@dataclass
class CandidateState:
    repository: str
    change_number: int
    core: set[str] = field(default_factory=set)
    capsule: bool = False
    runtime: bool = False
    build: bool = False
    attempt_heads: int = 0
    qualified: bool = False
    terminal: bool = False
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        gate14 = len(self.core) == len(CORE_KINDS)
        if self.qualified:
            lane = "A"
        elif gate14 and self.capsule and self.runtime and self.build:
            lane = "B"
        elif gate14 and self.capsule:
            lane = "C"
        else:
            lane = "D"
        return {
            "candidate": f"{self.repository}#{self.change_number}",
            "repository": self.repository,
            "change_number": self.change_number,
            "gate1_4_chain": gate14,
            "gate1_4_evidence": sorted(self.core),
            "lane": lane,
            "capsule": self.capsule,
            "runtime": self.runtime,
            "build": self.build,
            "attempt_heads": self.attempt_heads,
            "qualified": self.qualified,
            "terminal": self.terminal,
            "blockers": sorted(set(self.blockers))[-3:],
        }


def _walk_heads(root: Path) -> Iterable[Path]:
    # Prune before entering revision directories. The unpruned cache currently
    # contains roughly 200k head.json files, most outside the active pool.
    for directory, dirs, files in os.walk(root):
        relative_parts = Path(directory).relative_to(root).parts
        if len(relative_parts) == 1:
            dirs[:] = [name for name in dirs if name in REPOSITORIES]
        elif len(relative_parts) == 2:
            dirs[:] = [
                name
                for name in dirs
                if name.isdigit() and int(name) in POOL_CHANGE_NUMBERS
            ]
        if "head.json" in files:
            yield Path(directory) / "head.json"


def _candidate_from_parts(parts: tuple[str, ...]) -> tuple[str, int] | None:
    for index, part in enumerate(parts[:-1]):
        if part in REPOSITORIES and parts[index + 1].isdigit():
            return part, int(parts[index + 1])
    return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _load_head(head: Path) -> dict[str, Any] | None:
    """Resolve a stable head pointer to its sibling revision payload."""
    pointer = _load(head)
    if pointer is None:
        return None
    if isinstance(pointer.get("payload"), dict):
        return pointer
    revision = pointer.get("revision")
    if isinstance(revision, str):
        resolved = _load(head.parent / revision)
        if resolved is not None:
            return resolved
    return pointer


def _candidate_from_document(
    document: dict[str, Any], parts: tuple[str, ...]
) -> tuple[str, int] | None:
    artifact_id = document.get("artifact_id")
    if isinstance(artifact_id, str):
        candidate = _candidate_from_parts(tuple(artifact_id.split("/")))
        if candidate is not None:
            return candidate
    payload = document.get("payload")
    if isinstance(payload, dict):
        candidate_payload = payload.get("candidate")
        if isinstance(candidate_payload, dict):
            repository = candidate_payload.get("repository_key")
            number = candidate_payload.get("change_number")
            if isinstance(repository, str) and isinstance(number, int):
                return repository, number
    return _candidate_from_parts(parts)


def _payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()


def build_index(artifact_root: Path) -> list[dict[str, Any]]:
    states: dict[tuple[str, int], CandidateState] = {}
    for head in _walk_heads(artifact_root):
        relative = head.relative_to(artifact_root)
        document = _load_head(head)
        if document is None:
            continue
        candidate = _candidate_from_document(document, relative.parts)
        if candidate is None:
            continue
        repository, change_number = candidate
        state = states.setdefault(candidate, CandidateState(repository, change_number))
        kind = relative.parts[0]
        payload = document.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        text = _payload_text(payload)

        core = CORE_KINDS.get(kind)
        if core == "manifest" and "test_manifest" not in text:
            core = None
        if core is not None:
            state.core.add(core)

        if kind == "fixed_source_capsule":
            state.capsule = True
        if (
            "runtime" in kind
            or "dependency" in kind
            or "toolchain" in kind
            or "preparation" in kind
            or "dependencies" in relative.parts
        ):
            state.runtime = True
        if "build" in kind or "build" in text:
            state.build = True
        if "attempt" in kind:
            state.attempt_heads += 1
        if payload.get("qualified") is True or "gate57_qualified" in text:
            state.qualified = True
        # Only a typed terminal payload is terminal.  Artifact directory names
        # such as ``candidate_gate57_terminal`` are historical namespaces and
        # must not turn a resumable blocker into a terminal candidate.
        if payload.get("terminal") is True:
            state.terminal = True
        if "blocker" in kind or "blocked" in text:
            state.blockers.append(kind)

    return sorted(
        (state.as_dict() for state in states.values()),
        key=lambda item: (item["repository"], item["change_number"]),
    )


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=workspace / ".warptest-cache/c1_lazarus_selection_v1/artifacts",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=workspace
        / ".warptest-cache/c1_lazarus_selection_v1/private/readiness-index/c1-readiness.json",
    )
    args = parser.parse_args()
    rows = build_index(args.artifact_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"indexed={len(rows)} output={args.output}")
    for row in rows:
        print(
            "{candidate} gate1_4={gate1_4_chain} capsule={capsule} runtime={runtime} "
            "build={build} attempts={attempt_heads} qualified={qualified} terminal={terminal}".format(
                **row
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
