"""Independent trial planning and checkpoint-reuse invariants for Lazarus."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .lazarus_catalog import LAZARUS_ARMS, LAZARUS_VERSIONS
from .lazarus_p2p_only_contract import ARMS as P2P_ONLY_ARMS
from .lazarus_roster import FORMAL_TASK_COUNT


DEFAULT_LAZARUS_MODEL = "gpt-5.6-terra@relay"
# The Phase-0 pilot registers its own decision model so a pilot model change
# cannot silently re-point the formal 24-task matrix.
PHASE0_DECISION_MODEL = "gpt-5.6-luna@relay"
DEFAULT_LAZARUS_SEEDS = (0, 1, 2)
LAZARUS_ROTATION_RULE = "buggy_before_fixed_state_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^lzp?-[a-z0-9]+(?:-[a-z0-9]+)*$")
_FORMAL_TASK_ID = re.compile(r"^lz-[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class LazarusTrialKey:
    arm: str
    model: str
    seed: int
    task_id: str
    version: str

    def __post_init__(self) -> None:
        known_arms = frozenset(LAZARUS_ARMS) | frozenset(P2P_ONLY_ARMS)
        if self.arm not in known_arms and not (
            self.arm == "lazarus_agent_s_synth" and self.task_id.startswith("lzp-")
        ):
            raise ValueError(f"unknown Lazarus arm {self.arm!r}")
        if (
            self.arm in P2P_ONLY_ARMS
            and self.arm not in LAZARUS_ARMS
            and not self.task_id.startswith("lzp-")
        ):
            raise ValueError(f"P2P-only arm {self.arm!r} requires an lzp-* task id")
        if self.version not in LAZARUS_VERSIONS:
            raise ValueError(f"unknown Lazarus version {self.version!r}")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("Lazarus seed must be a non-negative integer")
        if not _TASK_ID.fullmatch(self.task_id):
            raise ValueError("Lazarus task_id must be an lz-* or lzp-* slug")
        if not self.model.strip():
            raise ValueError("Lazarus model must be non-empty")

    def identity(self) -> tuple[str, str, int, str, str]:
        return (self.arm, self.model, self.seed, self.task_id, self.version)

    def invocation_id(self) -> str:
        model = re.sub(r"[^a-zA-Z0-9_.-]+", "_", self.model)
        return (
            f"{self.task_id}__{self.arm}__{model}__s{self.seed}__{self.version}"
        )


@dataclass(frozen=True)
class LazarusTrialPlan:
    keys: tuple[LazarusTrialKey, ...]
    formal: bool
    complete: bool
    failures: tuple[str, ...]
    rotation_rule: str = LAZARUS_ROTATION_RULE

    @property
    def trial_count(self) -> int:
        return len(self.keys)


@dataclass(frozen=True)
class LazarusReuseResult:
    checkpoint_reuse_success: bool
    checkpoint_sha256: str
    actual_synthesis_calls: int
    synthesis_cache_hit: bool


def build_lazarus_trial_plan(
    task_ids: Sequence[str],
    *,
    seeds: Sequence[int] = DEFAULT_LAZARUS_SEEDS,
    model: str = DEFAULT_LAZARUS_MODEL,
    formal: bool = True,
) -> LazarusTrialPlan:
    """Build deterministic arm/version cells without touching ``C1_ARMS``."""

    tasks = tuple(str(task_id).strip() for task_id in task_ids)
    normalized_seeds = tuple(seeds)
    failures: list[str] = []
    if not tasks or len(tasks) != len(set(tasks)):
        failures.append("task ids must be non-empty and unique")
    if any(not _TASK_ID.fullmatch(task_id) for task_id in tasks):
        failures.append("all task ids must be lz-* slugs")
    if not normalized_seeds or len(normalized_seeds) != len(set(normalized_seeds)):
        failures.append("seeds must be non-empty and unique")
    if any(
        not isinstance(seed, int) or isinstance(seed, bool) or seed < 0
        for seed in normalized_seeds
    ):
        failures.append("seeds must be non-negative integers")
    if not str(model).strip():
        failures.append("model must be non-empty")
    if formal:
        if any(not _FORMAL_TASK_ID.fullmatch(task_id) for task_id in tasks):
            failures.append("formal Lazarus matrix permits only lz-* tasks")
        if len(tasks) != FORMAL_TASK_COUNT:
            failures.append(
                f"formal Lazarus matrix requires exactly {FORMAL_TASK_COUNT} tasks"
            )
        if normalized_seeds != DEFAULT_LAZARUS_SEEDS:
            failures.append("formal Lazarus matrix requires seeds 0, 1, 2")
        if model != DEFAULT_LAZARUS_MODEL:
            failures.append(
                f"formal Lazarus matrix requires model {DEFAULT_LAZARUS_MODEL}"
            )
    if failures:
        return LazarusTrialPlan(
            keys=(), formal=formal, complete=False, failures=tuple(failures)
        )

    keys: list[LazarusTrialKey] = []
    orders = (
        (
            ("lazarus_state", "buggy"),
            ("lazarus_agent_s", "buggy"),
            ("lazarus_agent_s", "fixed"),
            ("lazarus_state", "fixed"),
        ),
        (
            ("lazarus_agent_s", "buggy"),
            ("lazarus_state", "buggy"),
            ("lazarus_state", "fixed"),
            ("lazarus_agent_s", "fixed"),
        ),
    )
    for task_index, task_id in enumerate(tasks):
        for seed_index, seed in enumerate(normalized_seeds):
            order = orders[(task_index + seed_index) % len(orders)]
            for arm, version in order:
                keys.append(
                    LazarusTrialKey(
                        arm=arm,
                        model=model,
                        seed=seed,
                        task_id=task_id,
                        version=version,
                    )
                )
    expected = len(tasks) * len(normalized_seeds) * 4
    if len(keys) != expected or len(set(keys)) != expected:
        failures.append("trial planner did not produce a complete unique matrix")
    return LazarusTrialPlan(
        keys=tuple(keys),
        formal=formal,
        complete=not failures,
        failures=tuple(failures),
    )


class LazarusStateReuseTracker:
    """Fail closed unless fixed reuses the exact buggy state checkpoint."""

    def __init__(self) -> None:
        self._buggy: dict[tuple[str, str, int], tuple[str, int]] = {}

    def record_buggy(
        self,
        key: LazarusTrialKey,
        *,
        checkpoint_sha256: str,
        actual_synthesis_calls: int,
    ) -> None:
        _require_state_key(key, version="buggy")
        _require_checkpoint_sha(checkpoint_sha256)
        if actual_synthesis_calls < 1:
            raise ValueError("buggy state cell must produce the checkpoint via synthesis")
        identity = _reuse_identity(key)
        if identity in self._buggy:
            raise ValueError("buggy state checkpoint is already recorded")
        self._buggy[identity] = (checkpoint_sha256, actual_synthesis_calls)

    def record_fixed(
        self,
        key: LazarusTrialKey,
        *,
        checkpoint_sha256: str,
        synthesis_cache_hit: bool,
        actual_synthesis_calls: int,
    ) -> LazarusReuseResult:
        _require_state_key(key, version="fixed")
        _require_checkpoint_sha(checkpoint_sha256)
        identity = _reuse_identity(key)
        previous = self._buggy.get(identity)
        if previous is None:
            raise ValueError("fixed state cell cannot run before its buggy checkpoint")
        if checkpoint_sha256 != previous[0]:
            raise ValueError("fixed state cell must reuse the same checkpoint")
        if synthesis_cache_hit is not True:
            raise ValueError("fixed state cell must record a synthesis cache hit")
        if actual_synthesis_calls != 0:
            raise ValueError("fixed state cell must use zero actual synthesis calls")
        return LazarusReuseResult(
            checkpoint_reuse_success=True,
            checkpoint_sha256=checkpoint_sha256,
            actual_synthesis_calls=actual_synthesis_calls,
            synthesis_cache_hit=True,
        )


def _require_state_key(key: LazarusTrialKey, *, version: str) -> None:
    if key.arm != "lazarus_state":
        raise ValueError("checkpoint reuse applies only to lazarus_state")
    if key.version != version:
        raise ValueError(f"expected {version} Lazarus state key")


def _require_checkpoint_sha(value: str) -> None:
    if not _SHA256.fullmatch(str(value)):
        raise ValueError("checkpoint_sha256 must be a full lowercase SHA-256")


def _reuse_identity(key: LazarusTrialKey) -> tuple[str, str, int]:
    return (key.task_id, key.model, key.seed)
