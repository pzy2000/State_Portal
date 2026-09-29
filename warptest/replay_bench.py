"""C4: checkpoint replay determinism and regression cost.

C1 measures what it costs to *create* a checkpoint. This module measures what it
costs to *re-run* one, which is the property the paper actually claims when it
calls a checkpoint a versionable, re-runnable regression artifact: synthesis is
paid once, and every later run is restore + transition + assert with no provider
calls at all.

The experiment reruns already-synthesized checkpoints through the unmodified C1
execution and grading path. Nothing about how a checkpoint is restored, executed
or graded changes; only the source of the checkpoint changes, from a synthesis
agent to a frozen artifact on disk. Two properties follow from that choice:

* the reference verdict and the replay verdict are produced by the same grader,
  so disagreement is a real property of the artifact rather than of two
  differently-configured harnesses; and
* the population can include checkpoints that originally *failed*. The claim
  under test is verdict reproduction, not pass rate, so a checkpoint that failed
  in C1 and keeps failing is reproducing correctly. Restricting the population
  to the successes would measure something weaker and self-selected.

Provider calls are forbidden structurally rather than by convention: the whole
replay runs inside :func:`warptest.cost.no_llm_guard`, which counts and rejects
any ``record_call`` regardless of which cost meter happens to be innermost. The
visual channel is likewise pinned to deterministic normalized-image equality via
``WARPTEST_DETERMINISTIC_VISUAL_ONLY``, so a drifting frame is recorded as
rendering drift instead of escalating to the VLM judge. Pixel identity is a
strictly stronger requirement than the C1 rubric, which deliberately ignores
benign nondeterminism such as 2048's random tile spawns, so the visual number is
reported as its own diagnostic and never as "the C1 visual verdict reproduced".
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from . import cost
from .task_schema import MethodCheckpoint

REPLAY_SCHEMA_VERSION = 1
REPLAY_POPULATION_SCHEMA = "c4-replay-population-v1"
REPLAY_TRIAL_SCHEMA = "c4-replay-trial-v1"
REPLAY_SUMMARY_SCHEMA = "c4-replay-summary-v1"
REPLAY_AGENT_NAME = "frozen_checkpoint_replay_v1"

DEFAULT_SOURCE_ARM = "c1_state"
GOLDEN_CACHE_SUBDIR = "state-loop-golden"

# Verdict flags copied from the reference trial and recomputed on every replay.
# ``e2e_success`` is the headline accept decision; the four channel flags are
# kept so a disagreement can be attributed to a stage rather than just observed.
VERDICT_FIELDS = (
    "start_success",
    "goal_success",
    "e2e_success",
    "before_semantic_ok",
    "after_semantic_ok",
    "before_visual_ok",
    "after_visual_ok",
)

# The programmatic channel is the primary verdict. Visual identity is graded
# separately because pixel equality is stricter than the C1 rubric.
PROGRAMMATIC_FIELDS = ("before_semantic_ok", "after_semantic_ok")
VISUAL_FIELDS = ("before_visual_ok", "after_visual_ok")

TIMING_FIELDS = (
    "checkpoint_restore_ms",
    "time_to_start_ms",
    "time_start_to_goal_ms",
    "time_e2e_excluding_synthesis_ms",
    "grader_ms",
    "runtime_bootstrap_ms",
    "session_bootstrap_ms",
)

# A replay that never reached a verdict is an infrastructure observation, not a
# flaky checkpoint. Keeping these separate is the whole reason the flake number
# is trustworthy, so the classes are named explicitly rather than inferred.
OUTCOME_REPRODUCED = "reproduced"
OUTCOME_DIVERGED = "diverged"
OUTCOME_INFRASTRUCTURE_BLOCKED = "infrastructure_blocked"
# A checkpoint that the harness itself can no longer load, because the action
# or spec contract was tightened after the reference run graded it. This is a
# property of the harness's schema stability rather than of the checkpoint's
# determinism, so it is counted and reported but kept out of both the
# reproduction and the flake denominators.
OUTCOME_CONTRACT_DRIFT = "spec_contract_drift"

# The harness refusing to load or execute a checkpoint it once accepted. These
# are schema and registry decisions taken before the application runs, so they
# are distinguishable from both application nondeterminism and a flaky host.
SPEC_REJECTION_MARKERS = (
    "config synthesis failed",
    "Unregistered JupyterLab State transition program",
    "is not a registered",
    "only the registered",
)

INFRASTRUCTURE_REASON_MARKERS = (
    "session bootstrap failed",
    "runtime lock",
    "timed out",
    "timeout",
    "unsupported",
    "license",
    "display",
    "port",
    "address already in use",
    "connection refused",
)


class ReplayPopulationError(RuntimeError):
    """The frozen population could not be built or loaded."""


# --------------------------------------------------------------------------
# Population
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayCandidate:
    """One frozen checkpoint plus the verdict it was originally graded with."""

    checkpoint_id: str
    task_id: str
    benchmark: str
    model: str
    seed: int
    spec_relpath: str
    spec_sha256: str
    oracle_spec: str | None
    validity_class: str | None
    source_report: str
    source_report_sha256: str
    source_run: str
    reference_verdict: dict[str, Any]
    reference_timing: dict[str, Any]
    reference_synthesis_cost: dict[str, Any]
    reference_oracle_goldens: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReplayCandidate":
        known = {key: data.get(key) for key in cls.__dataclass_fields__}
        known["seed"] = int(known.get("seed") or 0)
        known["reference_verdict"] = dict(known.get("reference_verdict") or {})
        known["reference_timing"] = dict(known.get("reference_timing") or {})
        known["reference_synthesis_cost"] = dict(
            known.get("reference_synthesis_cost") or {}
        )
        known["reference_oracle_goldens"] = dict(
            known.get("reference_oracle_goldens") or {}
        )
        return cls(**known)  # type: ignore[arg-type]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _checkpoint_id(task_id: str, model: str, seed: int, spec_sha256: str) -> str:
    payload = f"{task_id}|{model}|{seed}|{spec_sha256}"
    return _sha256_text(payload)[:16]


def _reference_verdict(result: Mapping[str, Any]) -> dict[str, Any]:
    verdict = {field: result.get(field) for field in VERDICT_FIELDS}
    verdict["status"] = result.get("status")
    verdict["start_gate_status"] = result.get("start_gate_status")
    verdict["goal_evaluation_status"] = result.get("goal_evaluation_status")
    return verdict


def _reference_timing(result: Mapping[str, Any]) -> dict[str, Any]:
    return {field: result.get(field) for field in TIMING_FIELDS}


def _spec_path_for_report(
    report_path: Path, task_id: str, payload: Mapping[str, Any] | None = None
) -> Path | None:
    """Locate the synthesized spec belonging to a trial report.

    Atomic trial snapshots under ``c1-trial-evidence`` carry the report only;
    the ``tasks/`` tree stays with the shared invocation that produced it, so
    fall back to the snapshot's own back-link rather than dropping the trial.
    """

    direct = report_path.parent / "tasks" / task_id / "v1" / "spec.yaml"
    if direct.is_file():
        return direct
    evidence = (payload or {}).get("atomic_trial_evidence")
    source = (evidence or {}).get("source_invocation_report") if isinstance(evidence, dict) else None
    if isinstance(source, str) and source:
        linked = Path(source).expanduser().parent / "tasks" / task_id / "v1" / "spec.yaml"
        if linked.is_file():
            return linked
    return None


def harvest_candidates(
    index_paths: Sequence[str | Path],
    *,
    arm: str = DEFAULT_SOURCE_ARM,
    require_complete: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect graded checkpoints from prior C1 run indexes.

    Later indexes win on collision, so callers should pass indexes oldest-first.
    Returns the candidate rows plus a rejection ledger, because a population
    built by silently dropping rows is not auditable.
    """

    collected: dict[tuple[str, str, int], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    scanned = 0

    for index_path in index_paths:
        path = Path(index_path).expanduser().resolve()
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            rejected.append(
                {"index": str(path), "reason": f"unreadable index: {error}"}
            )
            continue
        rows = (index.get("arms") or {}).get(arm)
        if not isinstance(rows, list):
            rejected.append({"index": str(path), "reason": f"no {arm} rows"})
            continue
        run_id = str(index.get("run_id") or path.parent.name)
        for row in rows:
            scanned += 1
            status = row.get("status")
            report = row.get("report")
            task_id = str(row.get("task_id") or "")
            if status != "ok" or not report:
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": f"trial status {status!r} with report={bool(report)}",
                    }
                )
                continue
            report_path = Path(str(report)).expanduser()
            if not report_path.is_file():
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": "trial report file is missing",
                    }
                )
                continue
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as error:
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": f"unreadable trial report: {error}",
                    }
                )
                continue
            results = payload.get("results")
            if not isinstance(results, list) or not results:
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": "trial report has no results",
                    }
                )
                continue
            result = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict) and item.get("task_id") == task_id
                ),
                None,
            )
            if result is None:
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": "trial report does not contain the indexed task",
                    }
                )
                continue
            if require_complete and result.get("incomplete"):
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": "trial is marked incomplete",
                    }
                )
                continue
            spec_path = _spec_path_for_report(report_path, task_id, payload)
            if spec_path is None:
                rejected.append(
                    {
                        "index": str(path),
                        "task_id": task_id,
                        "reason": "synthesized spec.yaml could not be located",
                    }
                )
                continue
            model = str(row.get("model") or result.get("model") or "unknown")
            seed = int(row.get("seed") or result.get("seed") or 0)
            spec_sha = _sha256_file(spec_path)
            candidate = {
                "checkpoint_id": _checkpoint_id(task_id, model, seed, spec_sha),
                "task_id": task_id,
                "benchmark": str(result.get("benchmark") or ""),
                "model": model,
                "seed": seed,
                "spec_source_path": str(spec_path),
                "spec_sha256": spec_sha,
                "oracle_spec": result.get("oracle_spec"),
                "validity_class": result.get("validity_class"),
                "source_report": str(report_path),
                "source_report_sha256": str(row.get("report_sha256") or ""),
                "source_run": run_id,
                "reference_verdict": _reference_verdict(result),
                "reference_timing": _reference_timing(result),
                "reference_synthesis_cost": dict(
                    result.get("synthesis_actual_cost") or result.get("cost") or {}
                ),
                "reference_oracle_goldens": dict(result.get("oracle_goldens") or {}),
            }
            collected[(task_id, model, seed)] = candidate

    ledger = {
        "scanned_rows": scanned,
        "accepted": len(collected),
        "rejected": len(rejected),
        "rejections": rejected,
    }
    return list(collected.values()), ledger


def freeze_population(
    candidates: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    *,
    ledger: Mapping[str, Any] | None = None,
    source_indexes: Sequence[str | Path] = (),
) -> Path:
    """Copy each candidate spec into ``out_dir`` and write the frozen manifest.

    The population is content-addressed and copied rather than referenced so a
    later replay cannot silently pick up an edited or garbage-collected spec.
    """

    root = Path(out_dir).expanduser().resolve()
    specs_dir = root / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    frozen: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates, key=lambda item: (str(item["benchmark"]), str(item["task_id"]))
    ):
        source = Path(str(candidate["spec_source_path"]))
        relpath = f"specs/{candidate['checkpoint_id']}.yaml"
        destination = root / relpath
        shutil.copyfile(source, destination)
        copied_sha = _sha256_file(destination)
        if copied_sha != candidate["spec_sha256"]:
            raise ReplayPopulationError(
                f"frozen spec digest changed while copying {source}"
            )
        row = {
            key: value
            for key, value in candidate.items()
            if key != "spec_source_path"
        }
        row["spec_relpath"] = relpath
        row["spec_source_path"] = str(source)
        frozen.append(row)

    manifest = {
        "schema": REPLAY_POPULATION_SCHEMA,
        "schema_version": REPLAY_SCHEMA_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_indexes": [str(item) for item in source_indexes],
        "harvest_ledger": dict(ledger or {}),
        "count": len(frozen),
        "candidates": frozen,
    }
    manifest_path = root / "population.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest_path


def load_population(manifest_path: str | Path) -> tuple[Path, list[ReplayCandidate]]:
    path = Path(manifest_path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != REPLAY_POPULATION_SCHEMA:
        raise ReplayPopulationError(
            f"{path}: expected {REPLAY_POPULATION_SCHEMA}, got {data.get('schema')!r}"
        )
    root = path.parent
    candidates: list[ReplayCandidate] = []
    for row in data.get("candidates") or []:
        candidate = ReplayCandidate.from_dict(row)
        spec_path = root / candidate.spec_relpath
        if not spec_path.is_file():
            raise ReplayPopulationError(f"frozen spec is missing: {spec_path}")
        if _sha256_file(spec_path) != candidate.spec_sha256:
            raise ReplayPopulationError(
                f"frozen spec digest mismatch, population was edited: {spec_path}"
            )
        candidates.append(candidate)
    return root, candidates


def freeze_goldens(
    candidates: Sequence[ReplayCandidate], cache_root: str | Path
) -> dict[str, Any]:
    """Copy each candidate's reference goldens into a private C1 cache root.

    Replays then compare against exactly the frames the reference verdict was
    graded on. Letting the harness regenerate goldens per run would compare two
    freshly-rendered frames, which measures a different and weaker thing.
    """

    root = Path(cache_root).expanduser().resolve()
    copied = 0
    missing: list[str] = []
    for candidate in candidates:
        goldens = candidate.reference_oracle_goldens or {}
        fingerprint = goldens.get("fingerprint")
        before = goldens.get("before")
        after = goldens.get("after")
        if not (fingerprint and before and after):
            missing.append(candidate.checkpoint_id)
            continue
        source_dir = Path(str(before)).parent
        target_dir = root / GOLDEN_CACHE_SUBDIR / str(fingerprint)
        if target_dir.is_dir():
            copied += 1
            continue
        if not source_dir.is_dir():
            missing.append(candidate.checkpoint_id)
            continue
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir)
        copied += 1
    return {
        "cache_root": str(root),
        "frozen_golden_dirs": copied,
        "candidates_without_goldens": missing,
    }


# --------------------------------------------------------------------------
# Replay agent
# --------------------------------------------------------------------------


def _payload_for_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    from .synthesis import _payload_for_spec as build_payload

    return build_payload(dict(spec))


class FrozenCheckpointReplayAgent:
    """Serves frozen checkpoints through the synthesis-agent interface.

    This is deliberately not a synthesizer. It performs no repository
    inspection, consults no model, and ignores feedback, so the C1 task path
    downstream of ``synthesize`` runs exactly as it does in a paid trial while
    the checkpoint itself is held fixed.
    """

    name = REPLAY_AGENT_NAME

    def __init__(self, specs_by_task_id: Mapping[str, Mapping[str, Any]]) -> None:
        self._specs = {
            str(task_id): copy.deepcopy(dict(spec))
            for task_id, spec in specs_by_task_id.items()
        }
        self.served: list[str] = []

    def synthesize(
        self,
        task: Any,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: Mapping[str, Any] | None = None,
    ) -> MethodCheckpoint:
        del feedback  # Replay is fixed; iteration feedback cannot change it.
        task_id = str(getattr(task, "id", ""))
        spec = self._specs.get(task_id)
        if spec is None:
            raise ReplayPopulationError(
                f"no frozen checkpoint for task {task_id!r}"
            )
        self.served.append(task_id)
        generated_spec = copy.deepcopy(spec)
        checkpoint = generated_spec.get("checkpoint") or {}
        validity = checkpoint.get("expected_validity", "unknown")
        if artifact_dir is not None:
            artifact_dir = Path(artifact_dir)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "replayed-spec.yaml").write_text(
                yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
            )
        return MethodCheckpoint(
            task_id=task_id,
            benchmark=getattr(task, "benchmark", generated_spec.get("benchmark", "")),
            generated_spec=generated_spec,
            source_evidence=[
                {
                    "repo": str(root),
                    "source_path": f"frozen-checkpoint:{task_id}",
                    "matched_symbol": task_id,
                    "confidence": 1.0,
                    "rationale": (
                        "Replayed from a frozen checkpoint artifact; no source "
                        "exploration was performed."
                    ),
                }
            ],
            payload=_payload_for_spec(generated_spec),
            validity_class=validity,
            runtime_validation={
                "status": "pending",
                "detail": "Runtime validation has not been executed yet.",
                "required": True,
            },
            inputs_read=[],
            hidden_files_read=[],
            generated_by=REPLAY_AGENT_NAME,
            model_generated_spec=copy.deepcopy(generated_spec),
            used_public_scaffold=False,
        )


# --------------------------------------------------------------------------
# Replay execution
# --------------------------------------------------------------------------


def _task_rows(tasks_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    data = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ReplayPopulationError(f"task file has no tasks list: {tasks_path}")
    rows = {
        str(row.get("id")): row
        for row in data["tasks"]
        if isinstance(row, dict) and row.get("id")
    }
    return data, rows


def write_task_subset(
    tasks_path: str | Path,
    task_ids: Sequence[str],
    destination: str | Path,
) -> Path:
    """Write a task file containing only ``task_ids``, in the order given.

    Emitted in the catalog's own derived-subset form (``selected_task_ids``
    plus ``source_task_file``) so it passes the same validation a formal C1
    subset does, rather than sidestepping it. Order is preserved on purpose:
    the position of a checkpoint inside an invocation is a measured factor.
    """

    source = Path(tasks_path).expanduser().resolve()
    data, rows = _task_rows(source)
    missing = [task_id for task_id in task_ids if task_id not in rows]
    if missing:
        raise ReplayPopulationError(
            "task file is missing replay tasks: " + ", ".join(sorted(missing))
        )
    ordered = list(task_ids)
    subset = copy.deepcopy(data)
    subset["tasks"] = [copy.deepcopy(rows[task_id]) for task_id in ordered]
    subset["selected_task_ids"] = ordered
    subset["source_task_file"] = str(source)
    out = Path(destination).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(subset, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return out


def _rejected_by_spec_contract(result: Mapping[str, Any]) -> bool:
    reason = str(result.get("reason") or "")
    return any(marker in reason for marker in SPEC_REJECTION_MARKERS)


def _reference_was_graded(reference: Mapping[str, Any]) -> bool:
    """True when the reference run actually loaded and graded the checkpoint.

    Used to tell contract drift apart from a faithful reproduction of an
    already-broken checkpoint: if the reference run also failed to load the
    spec, a replay that fails the same way is reproducing correctly.
    """

    return reference.get("before_semantic_ok") is True or str(
        reference.get("goal_evaluation_status") or ""
    ) == "evaluated"


def _classify_infrastructure(result: Mapping[str, Any]) -> bool:
    if result.get("incomplete"):
        return True
    status = str(result.get("status") or "").lower()
    if status in {"unsupported", "error", "blocked"}:
        return True
    if str(result.get("start_gate_status") or "") == "infrastructure_blocked":
        return True
    reason = str(result.get("reason") or "").lower()
    return any(marker in reason for marker in INFRASTRUCTURE_REASON_MARKERS)


def _observed_verdict(result: Mapping[str, Any]) -> dict[str, Any]:
    return _reference_verdict(result)


def _compare_verdicts(
    reference: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """Attribute a replay outcome to the channel that moved.

    Only the programmatic channel decides ``reproduced`` versus ``diverged``.
    Visual identity is reported alongside it but never flips the outcome,
    because normalized pixel equality is a stricter test than the rubric the
    reference verdict was graded with.

    The goal stage is compared only when the reference run actually reached it.
    A reference trial whose Start gate failed records ``after_semantic_ok`` as
    False meaning "never evaluated", so comparing it against a replay that did
    evaluate the goal would report a policy difference as nondeterminism.
    """

    goal_comparable = (
        str(reference.get("goal_evaluation_status") or "") == "evaluated"
    )
    compared_fields = ["before_semantic_ok"] + (
        ["after_semantic_ok"] if goal_comparable else []
    )
    programmatic_fields = [
        field
        for field in compared_fields
        if reference.get(field) != observed.get(field)
    ]
    visual_fields = [
        field for field in VISUAL_FIELDS if reference.get(field) != observed.get(field)
    ]
    return {
        "programmatic_reproduced": not programmatic_fields,
        "programmatic_compared_fields": compared_fields,
        "programmatic_divergent_fields": programmatic_fields,
        "goal_stage_comparable": goal_comparable,
        "visual_identity_reproduced": not visual_fields,
        "visual_divergent_fields": visual_fields,
        "reference_e2e_success": reference.get("e2e_success"),
        "observed_programmatic_accept": (
            observed.get("before_semantic_ok") is True
            and observed.get("after_semantic_ok") is True
        ),
    }


@dataclass
class ReplayTrial:
    checkpoint_id: str
    task_id: str
    benchmark: str
    repetition: int
    environment: str
    seed: int
    position: int
    outcome: str
    comparison: dict[str, Any]
    reference_verdict: dict[str, Any]
    observed_verdict: dict[str, Any]
    timing: dict[str, Any]
    no_llm_attestation: dict[str, Any]
    cost_summary: dict[str, Any]
    wall_clock_ms: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = REPLAY_TRIAL_SCHEMA
        payload["schema_version"] = REPLAY_SCHEMA_VERSION
        return payload


def replay_batch(
    base: Path,
    candidates: Sequence[ReplayCandidate],
    population_root: Path,
    *,
    tasks_path: str | Path,
    report_dir: Path,
    repetition: int,
    environment: str,
    seed: int | None = None,
    headless: bool = True,
    task_timeout_s: float | None = None,
    sandbox_manager: Any = None,
) -> list[ReplayTrial]:
    """Replay one ordered batch of checkpoints in a single invocation.

    Provider calls are forbidden for the whole batch rather than per task, so a
    leak anywhere in the invocation, including golden regeneration and grading,
    is caught.
    """

    from .state_loop import run_state_loop_benchmark

    if not candidates:
        return []

    # The experiment seed is part of a checkpoint's execution contract, not a
    # nuisance factor: on the browser profiles it keys the injected PRNG, so
    # replaying at a different seed would change the run itself and measure
    # seed robustness rather than reproduction. Replays therefore inherit the
    # seed their reference verdict was graded at.
    reference_seeds = {candidate.seed for candidate in candidates}
    if seed is None:
        if len(reference_seeds) != 1:
            raise ReplayPopulationError(
                "a replay batch must share one reference seed; got "
                + ", ".join(str(value) for value in sorted(reference_seeds))
            )
        seed = next(iter(reference_seeds))

    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    specs_by_task: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        spec_path = population_root / candidate.spec_relpath
        specs_by_task[candidate.task_id] = yaml.safe_load(
            spec_path.read_text(encoding="utf-8")
        )

    ordered_ids = [candidate.task_id for candidate in candidates]
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ReplayPopulationError(
            "a replay batch may hold each task at most once; batch by "
            "(benchmark, model, seed) before calling replay_batch"
        )
    subset_path = write_task_subset(
        tasks_path, ordered_ids, report_dir / "replay-tasks.yaml"
    )

    agent = FrozenCheckpointReplayAgent(specs_by_task)
    started = time.perf_counter()
    payload: dict[str, Any]
    guard_error: str | None = None
    with cost.scoped_meter() as meter:
        try:
            with cost.no_llm_guard() as attestation:
                payload = run_state_loop_benchmark(
                    base,
                    tasks_path=subset_path,
                    agent_kind="replay",
                    model=REPLAY_AGENT_NAME,
                    seeds=1,
                    seed=0 if seed is None else int(seed),
                    headless=headless,
                    report_dir=report_dir,
                    restore_diagnostics=False,
                    arm="c1_state",
                    transition_action_source="generated_spec",
                    sandbox_manager=sandbox_manager,
                    invocation_id=f"replay-{environment}-r{repetition}",
                    synthesis_agent_factory=lambda: agent,
                    # A shared start verdict cache would let a replay "reproduce"
                    # by reading a stored verdict instead of re-grading.
                    pair_start_cache=None,
                    **(
                        {"task_timeout_s": task_timeout_s}
                        if task_timeout_s is not None
                        else {}
                    ),
                )
        except cost.LLMCallForbiddenError as error:
            guard_error = str(error)
            payload = {"results": []}
            attestation = cost.NoLLMAttestation(observed_calls=1)
        except Exception as error:  # noqa: BLE001 - recorded as infrastructure
            guard_error = f"replay invocation failed: {error}"
            payload = {"results": []}
            attestation = cost.NoLLMAttestation()
    wall_clock_ms = int((time.perf_counter() - started) * 1000)

    by_task = {
        str(item.get("task_id")): item
        for item in (payload.get("results") or [])
        if isinstance(item, dict)
    }
    attestation_payload = attestation.to_dict()
    cost_summary = meter.summary()

    trials: list[ReplayTrial] = []
    for position, candidate in enumerate(candidates):
        result = by_task.get(candidate.task_id)
        if result is None:
            trials.append(
                ReplayTrial(
                    checkpoint_id=candidate.checkpoint_id,
                    task_id=candidate.task_id,
                    benchmark=candidate.benchmark,
                    repetition=repetition,
                    environment=environment,
                    seed=int(seed or 0),
                    position=position,
                    outcome=OUTCOME_INFRASTRUCTURE_BLOCKED,
                    comparison={},
                    reference_verdict=dict(candidate.reference_verdict),
                    observed_verdict={},
                    timing={},
                    no_llm_attestation=attestation_payload,
                    cost_summary=cost_summary,
                    wall_clock_ms=wall_clock_ms,
                    reason=guard_error or "task produced no result record",
                )
            )
            continue
        observed = _observed_verdict(result)
        comparison = _compare_verdicts(candidate.reference_verdict, observed)
        # Order matters. A harness refusal is never an infrastructure problem:
        # if the reference run had already graded this checkpoint the contract
        # moved underneath it, and if the reference run also failed to load it
        # then refusing again is a faithful reproduction.
        if _rejected_by_spec_contract(result):
            outcome = (
                OUTCOME_CONTRACT_DRIFT
                if _reference_was_graded(candidate.reference_verdict)
                else OUTCOME_REPRODUCED
                if comparison["programmatic_reproduced"]
                else OUTCOME_DIVERGED
            )
        elif _classify_infrastructure(result):
            outcome = OUTCOME_INFRASTRUCTURE_BLOCKED
        elif comparison["programmatic_reproduced"]:
            outcome = OUTCOME_REPRODUCED
        else:
            outcome = OUTCOME_DIVERGED
        trials.append(
            ReplayTrial(
                checkpoint_id=candidate.checkpoint_id,
                task_id=candidate.task_id,
                benchmark=candidate.benchmark,
                repetition=repetition,
                environment=environment,
                seed=int(seed or 0),
                position=position,
                outcome=outcome,
                comparison=comparison,
                reference_verdict=dict(candidate.reference_verdict),
                observed_verdict=observed,
                timing=_reference_timing(result),
                no_llm_attestation=attestation_payload,
                cost_summary=cost_summary,
                wall_clock_ms=wall_clock_ms,
                reason=result.get("reason"),
            )
        )
    return trials


DEFAULT_MANIFEST = Path("benchmarks/p5/replay_manifest.yaml")


def load_replay_manifest(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ReplayPopulationError(f"replay manifest must be a mapping: {path}")
    return data


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    return _sha256_text(payload)[:16]


def _profile_policy(manifest: Mapping[str, Any], benchmark: str) -> dict[str, Any]:
    profiles = manifest.get("profiles") or {}
    default = dict(manifest.get("default_profile") or {})
    default.update(dict(profiles.get(benchmark) or {}))
    return default


def select_population(
    candidates: Sequence[ReplayCandidate], manifest: Mapping[str, Any]
) -> list[ReplayCandidate]:
    """Apply the manifest's per-profile sampling rules deterministically.

    Sampling is by sorted checkpoint id rather than by outcome, so an expensive
    profile's subset cannot be quietly biased toward checkpoints that pass.
    """

    by_benchmark: dict[str, list[ReplayCandidate]] = {}
    for candidate in candidates:
        by_benchmark.setdefault(candidate.benchmark, []).append(candidate)

    selected: list[ReplayCandidate] = []
    for benchmark, rows in sorted(by_benchmark.items()):
        policy = _profile_policy(manifest, benchmark)
        if policy.get("enabled") is False:
            continue
        limit = policy.get("max_checkpoints")
        ordered = sorted(rows, key=lambda item: item.checkpoint_id)
        selected.extend(ordered if limit is None else ordered[: int(limit)])
    return selected


def run_replay_bench(
    base: str | Path,
    *,
    manifest: Mapping[str, Any],
    population_manifest: str | Path,
    report_dir: str | Path,
    environment: str,
    tasks_path: str | Path,
    headless: bool = True,
    repetitions: int | None = None,
    benchmarks: Sequence[str] | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Run every selected checkpoint ``K`` times and persist the trials.

    Each repetition of a benchmark runs as one invocation with the checkpoints
    in a rotated order, so a checkpoint occupies a different position on each
    pass. That turns invocation position into a measured factor instead of a
    hidden constant, which matters because the profiles reuse a live engine
    process across the restore and the transition.
    """

    root = Path(base).expanduser().resolve()
    population_root, all_candidates = load_population(population_manifest)
    candidates = select_population(all_candidates, manifest)
    if benchmarks:
        wanted = {str(item) for item in benchmarks}
        candidates = [item for item in candidates if item.benchmark in wanted]
    if not candidates:
        raise ReplayPopulationError("no checkpoints selected for replay")

    out_dir = Path(report_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_root = out_dir / "frozen-goldens"
    golden_report = freeze_goldens(candidates, cache_root)

    # Frozen goldens plus a disabled visual cache plus deterministic-only visual
    # grading is what makes the visual number a rendering-identity measurement
    # rather than a re-judgement.
    os.environ["WARPTEST_C1_CACHE_ROOT"] = str(cache_root)
    os.environ["WARPTEST_C1_DISABLE_VISUAL_CACHE"] = "1"
    os.environ["WARPTEST_DETERMINISTIC_VISUAL_ONLY"] = "1"
    # The strict visual channel must not gate Phase B; see the comment on the
    # gate itself in state_loop.
    os.environ["WARPTEST_REPLAY_PROGRAMMATIC_START_GATE"] = "1"

    # One invocation may hold each task at most once, and the population keeps
    # one checkpoint per (task, model, seed), so group by the model and seed
    # that produced them before batching.
    groups: dict[tuple[str, str, int], list[ReplayCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(
            (candidate.benchmark, candidate.model, candidate.seed), []
        ).append(candidate)

    trials_path = out_dir / "replay-trials.json"
    existing: list[dict[str, Any]] = []
    if resume and trials_path.is_file():
        existing = json.loads(trials_path.read_text(encoding="utf-8"))
    done = {
        (row.get("checkpoint_id"), row.get("repetition"), row.get("environment"))
        for row in existing
    }
    collected = [ReplayTrialRecord(row) for row in existing]

    for benchmark, model, group_seed in sorted(groups):
        policy = _profile_policy(manifest, benchmark)
        passes = int(repetitions or policy.get("repetitions") or 1)
        rows = sorted(
            groups[(benchmark, model, group_seed)],
            key=lambda item: item.checkpoint_id,
        )
        group_label = f"{_sha256_text(model)[:8]}-seed{group_seed}"
        batch_size = int(policy.get("batch_size") or len(rows))
        timeout = policy.get("task_timeout_s")
        for repetition in range(passes):
            # Rotate so each pass gives a checkpoint a different position.
            offset = repetition % max(1, len(rows))
            ordered = rows[offset:] + rows[:offset]
            for start in range(0, len(ordered), max(1, batch_size)):
                batch = ordered[start : start + max(1, batch_size)]
                pending = [
                    item
                    for item in batch
                    if (item.checkpoint_id, repetition, environment) not in done
                ]
                if not pending:
                    continue
                batch_dir = (
                    out_dir
                    / "invocations"
                    / environment
                    / f"r{repetition:02d}"
                    / benchmark
                    / group_label
                    / f"b{start // max(1, batch_size):02d}"
                )
                print(
                    f"[replay-bench] {benchmark} {group_label} "
                    f"rep {repetition + 1}/{passes} ({len(pending)} checkpoints)",
                    flush=True,
                )
                trials = replay_batch(
                    root,
                    pending,
                    population_root,
                    tasks_path=tasks_path,
                    report_dir=batch_dir,
                    repetition=repetition,
                    environment=environment,
                    headless=headless,
                    task_timeout_s=float(timeout) if timeout is not None else None,
                )
                collected.extend(ReplayTrialRecord(trial.to_dict()) for trial in trials)
                write_trials_records(collected, trials_path)

    rows = [record.data for record in collected]
    summary = aggregate_trials(
        rows,
        population=candidates,
        bootstrap_iterations=int(
            (manifest.get("stats") or {}).get("bootstrap_iterations") or 10000
        ),
        rng_seed=int((manifest.get("stats") or {}).get("rng_seed") or 1234),
    )
    summary["manifest_hash"] = manifest_hash(manifest)
    summary["environment"] = environment
    summary["frozen_goldens"] = golden_report
    summary["population_manifest"] = str(Path(population_manifest).resolve())

    (out_dir / "replay-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "replay-summary.md").write_text(
        render_markdown(summary) + "\n", encoding="utf-8"
    )
    return summary


class ReplayTrialRecord:
    """Thin wrapper so persisted and freshly-produced trials share one path."""

    __slots__ = ("data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = dict(data)


def write_trials_records(
    records: Sequence[ReplayTrialRecord], destination: Path
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(
        json.dumps([record.data for record in records], indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, destination)
    return destination


def write_trials(trials: Iterable[ReplayTrial], destination: Path) -> Path:
    """Persist trials atomically so an interrupted run leaves no partial file."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = [trial.to_dict() for trial in trials]
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, destination)
    return destination


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def _timing_stats(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for field_name in TIMING_FIELDS:
        values = [
            float(trial["timing"][field_name])
            for trial in trials
            if isinstance(trial.get("timing"), dict)
            and isinstance(trial["timing"].get(field_name), (int, float))
        ]
        if not values:
            continue
        stats[field_name] = {
            "n": len(values),
            "median": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "min": min(values),
            "max": max(values),
        }
    return stats


def aggregate_trials(
    trials: Sequence[Mapping[str, Any]],
    *,
    population: Sequence[ReplayCandidate] = (),
    bootstrap_iterations: int = 10000,
    rng_seed: int = 1234,
) -> dict[str, Any]:
    """Summarize replay outcomes.

    Reproduction and flake are computed per checkpoint over graded replays only.
    Infrastructure-blocked replays are counted and reported but excluded from
    both denominators: folding them in would let an unavailable display or a
    busy port masquerade as a nondeterministic checkpoint.
    """

    from .p5_stats import clustered_mean_ci

    graded = [
        trial
        for trial in trials
        if trial.get("outcome") in {OUTCOME_REPRODUCED, OUTCOME_DIVERGED}
    ]
    blocked = [
        trial for trial in trials if trial.get("outcome") == OUTCOME_INFRASTRUCTURE_BLOCKED
    ]
    drifted = [
        trial for trial in trials if trial.get("outcome") == OUTCOME_CONTRACT_DRIFT
    ]
    drifted_checkpoints = sorted(
        {
            (str(trial.get("benchmark")), str(trial.get("task_id")))
            for trial in drifted
        }
    )

    by_checkpoint: dict[str, list[Mapping[str, Any]]] = {}
    for trial in graded:
        by_checkpoint.setdefault(str(trial.get("checkpoint_id")), []).append(trial)

    # Two distinct properties, deliberately not merged. Self-consistency asks
    # whether repeated replays of one checkpoint agree with *each other*, which
    # is determinism. Reproduction asks whether they agree with the reference
    # verdict, which additionally requires the harness and host to have not
    # moved since. A checkpoint that disagrees with its reference the same way
    # on every replay is perfectly deterministic and must not be called flaky.
    unanimous = 0
    self_consistent = 0
    divergent_checkpoints: list[dict[str, Any]] = []
    inconsistent_checkpoints: list[dict[str, Any]] = []
    for checkpoint_id, rows in by_checkpoint.items():
        outcomes = {row.get("outcome") for row in rows}
        observed = {
            tuple(
                (row.get("observed_verdict") or {}).get(field)
                for field in PROGRAMMATIC_FIELDS
            )
            for row in rows
        }
        entry = {
            "checkpoint_id": checkpoint_id,
            "task_id": rows[0].get("task_id"),
            "benchmark": rows[0].get("benchmark"),
            "replays": len(rows),
            "reproduced": sum(
                1 for row in rows if row.get("outcome") == OUTCOME_REPRODUCED
            ),
            "distinct_observed_verdicts": len(observed),
            "divergent_fields": sorted(
                {
                    field
                    for row in rows
                    for field in (row.get("comparison") or {}).get(
                        "programmatic_divergent_fields", []
                    )
                }
            ),
        }
        if len(observed) == 1:
            self_consistent += 1
        else:
            inconsistent_checkpoints.append(entry)
        if outcomes == {OUTCOME_REPRODUCED}:
            unanimous += 1
        else:
            divergent_checkpoints.append(entry)

    n_checkpoints = len(by_checkpoint)
    # Cluster by checkpoint: repeated replays of one checkpoint are not
    # independent observations of determinism.
    clustered: dict[tuple[Any, ...], float] = {
        (
            str(trial.get("checkpoint_id")),
            str(trial.get("environment")),
            int(trial.get("repetition") or 0),
            int(trial.get("position") or 0),
        ): (1.0 if trial.get("outcome") == OUTCOME_REPRODUCED else 0.0)
        for trial in graded
    }
    reproduction_ci = (
        clustered_mean_ci(
            clustered,
            iterations=bootstrap_iterations,
            rng=random.Random(rng_seed),
        )
        if clustered
        else None
    )

    visual_graded = [
        trial
        for trial in graded
        if isinstance(trial.get("comparison"), dict)
        and "visual_identity_reproduced" in trial["comparison"]
    ]
    visual_identical = sum(
        1
        for trial in visual_graded
        if trial["comparison"].get("visual_identity_reproduced")
    )

    total_llm_calls = sum(
        int((trial.get("cost_summary") or {}).get("llm_calls") or 0) for trial in trials
    )
    attestations_clean = all(
        bool((trial.get("no_llm_attestation") or {}).get("clean", False))
        for trial in trials
    )

    def _breakdown(key: Any) -> dict[str, dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for trial in graded:
            name = str(key(trial))
            bucket = buckets.setdefault(
                name, {"replays": 0, "reproduced": 0, "checkpoints": set()}
            )
            bucket["replays"] += 1
            bucket["reproduced"] += int(trial.get("outcome") == OUTCOME_REPRODUCED)
            bucket["checkpoints"].add(str(trial.get("checkpoint_id")))
        for bucket in buckets.values():
            bucket["checkpoints"] = len(bucket["checkpoints"])
            bucket["reproduction_rate"] = (
                bucket["reproduced"] / bucket["replays"] if bucket["replays"] else None
            )
        return buckets

    by_benchmark = _breakdown(lambda trial: trial.get("benchmark") or "unknown")
    by_environment = _breakdown(lambda trial: trial.get("environment") or "unknown")
    # Reported separately because the two strata answer different questions: an
    # originally-accepted checkpoint is the regression artifact the paper
    # claims, while an originally-rejected one tests that a verdict stays put
    # in both directions.
    by_reference_verdict = _breakdown(
        lambda trial: (
            "reference_accepted"
            if (trial.get("reference_verdict") or {}).get("e2e_success")
            else "reference_rejected"
        )
    )

    return {
        "schema": REPLAY_SUMMARY_SCHEMA,
        "schema_version": REPLAY_SCHEMA_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "population_size": len(population) or n_checkpoints,
        "checkpoints_replayed": n_checkpoints,
        "replays_total": len(trials),
        "replays_graded": len(graded),
        "replays_infrastructure_blocked": len(blocked),
        "replays_spec_contract_drift": len(drifted),
        "spec_contract_drift_checkpoints": [
            {"benchmark": benchmark, "task_id": task_id}
            for benchmark, task_id in drifted_checkpoints
        ],
        "spec_contract_drift_note": (
            "These checkpoints were graded by the reference run but are no "
            "longer loadable at the current commit, because the executable "
            "action contract was tightened afterwards. This bounds artifact "
            "durability against harness schema evolution and is reported "
            "separately from checkpoint nondeterminism."
        ),
        "verdict_reproduction_rate": (
            sum(1 for trial in graded if trial.get("outcome") == OUTCOME_REPRODUCED)
            / len(graded)
            if graded
            else None
        ),
        "verdict_reproduction_ci": reproduction_ci,
        "unanimous_checkpoints": unanimous,
        "unanimous_reproduction_rate": (
            unanimous / n_checkpoints if n_checkpoints else None
        ),
        "self_consistent_checkpoints": self_consistent,
        "self_consistency_rate": (
            self_consistent / n_checkpoints if n_checkpoints else None
        ),
        # Determinism proper: a checkpoint whose repeated replays disagree with
        # one another. A checkpoint that consistently disagrees with its
        # reference is counted under divergence, not here.
        "flake_rate": (
            (n_checkpoints - self_consistent) / n_checkpoints
            if n_checkpoints
            else None
        ),
        "inconsistent_checkpoints": inconsistent_checkpoints,
        "divergent_checkpoints": divergent_checkpoints,
        "visual_identity_rate": (
            visual_identical / len(visual_graded) if visual_graded else None
        ),
        "visual_identity_note": (
            "Normalized pixel equality against the frozen reference frame. This "
            "is stricter than the C1 rubric, which ignores benign rendering "
            "nondeterminism, so a mismatch here is rendering drift and not a "
            "failed visual verdict."
        ),
        "llm_calls_observed": total_llm_calls,
        "no_llm_attestations_clean": attestations_clean,
        "timing": _timing_stats(graded),
        "by_benchmark": by_benchmark,
        "by_environment": by_environment,
        "by_reference_verdict": by_reference_verdict,
    }


C1_METHOD_LABELS = {
    "state_managed": "c1_state",
    "agent_s_from_start": "c1_agent_s",
    "agent_s_synthesized": "c1_agent_s_synth",
    "state_managed_minimal_action_agent": "c1_state_minimal_agent",
}


def parse_c1_token_costs(summary_markdown: str | Path) -> dict[str, dict[str, float]]:
    """Read mean per-trial token costs per (model, method) from a C1 summary.

    The markdown summary is parsed rather than the JSON one because the latter
    carries every trial record and runs to hundreds of megabytes, while the
    numbers wanted here are exactly the ones the paper already reports.
    """

    text = Path(summary_markdown).expanduser().resolve().read_text(encoding="utf-8")
    costs: dict[str, dict[str, float]] = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("| Method | Model | Mean total tokens"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                if costs:
                    break
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 3 or cells[0] in {"---", ""} or set(cells[0]) <= {"-", ":"}:
                continue
            method = C1_METHOD_LABELS.get(cells[0], cells[0])
            model = cells[1]
            value = cells[2].split("[")[0].strip()
            try:
                costs.setdefault(model, {})[method] = float(value)
            except ValueError:
                continue
    if not costs:
        raise ReplayPopulationError(
            f"no C1 token-cost table found in {summary_markdown}"
        )
    return costs


def amortization_curve(
    summary: Mapping[str, Any],
    *,
    c1_token_costs: Mapping[str, Mapping[str, float]],
    checkpoint_arm: str = "c1_state",
    max_runs: int = 20,
) -> dict[str, Any]:
    """Cumulative regression cost as a function of how often a test is re-run.

    The baselines re-derive the start state on every run and so pay their full
    per-trial cost each time. The checkpoint arm pays synthesis once and then
    the measured replay cost, which the no-LLM guard pins at zero tokens. Costs
    are kept per model because they differ by roughly an order of magnitude
    across the three decision models, and an average over them would describe
    no actual configuration.
    """

    replay_tokens_per_run = float(summary.get("llm_calls_observed") or 0)
    runs = list(range(0, max_runs + 1))
    per_model: dict[str, Any] = {}

    for model, methods in c1_token_costs.items():
        synthesis = float(methods.get(checkpoint_arm) or 0.0)
        baselines = {
            name: float(value)
            for name, value in methods.items()
            if name != checkpoint_arm
        }
        series = {name: [value * run for run in runs] for name, value in baselines.items()}
        series["checkpoint_replay"] = [
            synthesis + replay_tokens_per_run * run for run in runs
        ]
        break_even = {
            name: next(
                (
                    run
                    for run in runs
                    if series["checkpoint_replay"][run] <= series[name][run]
                ),
                None,
            )
            for name in baselines
        }
        per_model[model] = {
            "synthesis_tokens_once": synthesis,
            "baseline_tokens_per_run": baselines,
            "cumulative_tokens": series,
            "break_even_run": break_even,
        }

    return {
        "runs": runs,
        "replay_tokens_per_run": replay_tokens_per_run,
        "checkpoint_arm": checkpoint_arm,
        "per_model": per_model,
    }


def render_markdown(summary: Mapping[str, Any], amortization: Mapping[str, Any] | None = None) -> str:
    lines: list[str] = []
    lines.append("# C4 Checkpoint Replay Determinism")
    lines.append("")
    lines.append(f"- Checkpoints replayed: {summary.get('checkpoints_replayed')}")
    lines.append(
        f"- Replays graded: {summary.get('replays_graded')} "
        f"(infrastructure blocked: {summary.get('replays_infrastructure_blocked')}, "
        f"spec contract drift: {summary.get('replays_spec_contract_drift')})"
    )
    rate = summary.get("verdict_reproduction_rate")
    lines.append(
        "- Verdict reproduction rate: "
        + ("n/a" if rate is None else f"{rate:.4f}")
    )
    consistency = summary.get("self_consistency_rate")
    lines.append(
        "- Replay self-consistency (repeats agree with each other): "
        + ("n/a" if consistency is None else f"{consistency:.4f}")
    )
    flake = summary.get("flake_rate")
    lines.append("- Flake rate: " + ("n/a" if flake is None else f"{flake:.4f}"))
    unanimous = summary.get("unanimous_reproduction_rate")
    lines.append(
        "- Checkpoints reproducing on every replay: "
        + ("n/a" if unanimous is None else f"{unanimous:.4f}")
    )
    visual = summary.get("visual_identity_rate")
    lines.append(
        "- Visual identity rate (stricter than the C1 rubric): "
        + ("n/a" if visual is None else f"{visual:.4f}")
    )
    lines.append(f"- Provider calls observed during replay: {summary.get('llm_calls_observed')}")
    lines.append(
        f"- No-LLM attestations clean: {summary.get('no_llm_attestations_clean')}"
    )
    lines.append("")

    timing = summary.get("timing") or {}
    if timing:
        lines.append("## Per-replay wall clock (ms)")
        lines.append("")
        for name, stats in timing.items():
            lines.append(
                f"- {name}: median {stats['median']:.0f}, p95 "
                f"{stats['p95']:.0f} over n={stats['n']}"
            )
        lines.append("")

    for title, key in (
        ("Per benchmark", "by_benchmark"),
        ("Per environment", "by_environment"),
        ("By reference verdict", "by_reference_verdict"),
    ):
        buckets = summary.get(key) or {}
        if not buckets:
            continue
        lines.append(f"## {title}")
        lines.append("")
        for name in sorted(buckets):
            bucket = buckets[name]
            value = bucket.get("reproduction_rate")
            lines.append(
                f"- {name}: {bucket['checkpoints']} checkpoints, "
                f"{bucket['replays']} replays, reproduction "
                + ("n/a" if value is None else f"{value:.4f}")
            )
        lines.append("")

    inconsistent = summary.get("inconsistent_checkpoints") or []
    if inconsistent:
        lines.append("## Checkpoints whose repeated replays disagreed with each other")
        lines.append("")
        for row in inconsistent:
            lines.append(
                f"- {row['task_id']} ({row['benchmark']}): "
                f"{row['distinct_observed_verdicts']} distinct verdicts over "
                f"{row['replays']} replays"
            )
        lines.append("")

    divergent = summary.get("divergent_checkpoints") or []
    if divergent:
        lines.append("## Checkpoints that did not reproduce the reference verdict")
        lines.append("")
        for row in divergent:
            deterministic = (
                "deterministically"
                if row["distinct_observed_verdicts"] == 1
                else "intermittently"
            )
            lines.append(
                f"- {row['task_id']} ({row['benchmark']}): "
                f"{row['reproduced']}/{row['replays']} reproduced, diverged "
                f"{deterministic}, fields "
                f"{', '.join(row['divergent_fields']) or 'n/a'}"
            )
        lines.append("")

    if amortization:
        lines.append("## Regression cost amortization")
        lines.append("")
        lines.append(
            f"- Replay cost per run: {amortization['replay_tokens_per_run']:.0f} tokens"
        )
        for model in sorted(amortization.get("per_model") or {}):
            entry = amortization["per_model"][model]
            lines.append(f"- {model}")
            lines.append(
                f"    - checkpoint synthesis paid once: "
                f"{entry['synthesis_tokens_once']:.0f} tokens"
            )
            for name, per_run in sorted(entry["baseline_tokens_per_run"].items()):
                run = entry["break_even_run"].get(name)
                lines.append(
                    f"    - vs {name}: {per_run:.0f} tokens per run, break-even at "
                    + (
                        "no run in the plotted range"
                        if run is None
                        else f"run {run}"
                    )
                )
        lines.append("")

    return "\n".join(lines)
