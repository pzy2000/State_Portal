"""Run-scoped checkpoint-synthesis cache shared by the C1 synthesizing arms.

Arm D (`c1_agent_s_synth`) has always cached its self-synthesized start so a
recovery pass reuses the checkpoint it already paid for.  Arm E (`c1_state`) had
no cache and re-rolled the synthesis LLM on every pass, which made the two arms'
Start numbers drift apart across a recovery chain for reasons unrelated to
either method.  Both arms now share this module.

Entries used to be keyed per consumer, so the two arms restored *different*
model-generated starts for the same task, model and seed and their Start rates
were not comparable.  One consumer now serves both: the synthesis prompt, model,
budget and seed are identical by construction, and the accept-or-reject decision
happens once in :mod:`warptest.c1_spec_acceptance` before the entry is written.
Each cached success therefore carries both projections of the same checkpoint --
``generated_spec`` for the arms that restore a spec directly and ``checkpoint``
for the arms that need the full :class:`MethodCheckpoint` -- because an entry
that carried only one of them would be silently unusable by the other arm, which
would then overwrite it and re-roll the model.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .runtime_lock import runtime_lease


SELF_SYNTHESIS_CACHE_SCHEMA_VERSION = 8
SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV = (
    "WARPTEST_C1_SYNTHESIS_CACHE_IDENTITY_ROOT"
)

# One consumer for every C1 arm that self-synthesizes its start. The key is
# deliberately independent of the consuming policy so `c1_state` and
# `c1_agent_s_synth` restore the same start for the same (task, model, seed).
C1_SYNTHESIS_CONSUMER = "c1_self_synth"

# The synthesis request budget both arms must issue. These were a literal inside
# the GUI producer and a keyword default on the state entrypoint; they happened
# to agree, so the shared cache key matched by luck rather than by construction.
C1_SYNTHESIS_MAX_TOKENS = 8192
C1_SYNTHESIS_EXPLORE = True
_GIT_CONTEXT_ENV_VARS = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

CACHEABLE_OUTCOME_STATUSES = frozenset({"success", "fair_failure"})


def canonical_hash(value: Any, *, length: int = 24) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def git_cache_output(repo: Path, *arguments: str) -> str:
    env = dict(os.environ)
    for name in _GIT_CONTEXT_ENV_VARS:
        env.pop(name, None)
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    return completed.stdout.strip()


def cache_identity_root(base: Path, task_repo_for: Callable[[Path], Path]) -> str:
    """Resolve an explicit, fail-closed legacy cache identity alias.

    The default remains the checkout's resolved path.  A continuation running
    from a linked worktree may opt into the main checkout's legacy cache key,
    but only when both parent checkouts share a Git common directory and HEAD,
    and the task-visible child repositories share a clean common directory and
    HEAD.  This preserves an already-produced checkpoint for a same-start
    comparison without silently merging arbitrary worktrees or repositories.
    """

    resolved = base.expanduser().resolve()
    raw_alias = os.environ.get(SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV, "").strip()
    if not raw_alias:
        return str(resolved)
    alias = Path(raw_alias).expanduser().resolve()
    if not alias.is_dir():
        raise ValueError(
            f"{SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV} is not a directory: {alias}"
        )
    if alias == resolved:
        return str(alias)

    try:
        common_dir = Path(
            git_cache_output(
                resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
        ).resolve()
        alias_common_dir = Path(
            git_cache_output(
                alias, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
        ).resolve()
        if common_dir != alias_common_dir:
            raise ValueError("cache identity alias is not in the same Git repository")
        if common_dir.name != ".git" or alias != common_dir.parent:
            raise ValueError("cache identity alias is not the common main checkout")
        if git_cache_output(resolved, "rev-parse", "HEAD") != git_cache_output(
            alias, "rev-parse", "HEAD"
        ):
            raise ValueError("cache identity alias parent HEAD differs")

        task_repo = task_repo_for(resolved).resolve()
        alias_task_repo = task_repo_for(alias).resolve()
        task_common_dir = Path(
            git_cache_output(
                task_repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
        ).resolve()
        alias_task_common_dir = Path(
            git_cache_output(
                alias_task_repo,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        ).resolve()
        if task_common_dir != alias_task_common_dir:
            raise ValueError("cache identity alias task repository differs")
        if git_cache_output(task_repo, "rev-parse", "HEAD") != git_cache_output(
            alias_task_repo, "rev-parse", "HEAD"
        ):
            raise ValueError("cache identity alias task repository HEAD differs")
        for repo in (task_repo, alias_task_repo):
            if git_cache_output(
                repo, "status", "--porcelain=v1", "--untracked-files=all"
            ):
                raise ValueError(
                    f"cache identity alias task repository is dirty: {repo}"
                )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            f"cannot validate {SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV}: {error}"
        ) from error
    return str(alias)


def synthesis_cache_key(
    *,
    consumer: str,
    identity_root: str,
    public_task: dict[str, Any],
    seed: int,
    synth_model: str,
    model_request_profile: Any,
    provider_name: str,
    provider_base_url: str,
    temperature: float,
    max_tokens: int,
    explore: bool,
    max_explore_steps: int,
    ablation_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SELF_SYNTHESIS_CACHE_SCHEMA_VERSION,
        "consumer": str(consumer),
        "root": identity_root,
        "task": public_task,
        "public_task_hash": canonical_hash(public_task),
        "seed": int(seed),
        "synth_model": synth_model,
        "model_request_profile": model_request_profile,
        "provider": provider_name,
        "provider_base_url": provider_base_url,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "explore": bool(explore),
        "max_explore_steps": int(max_explore_steps),
        **({"synthesis_ablation": ablation_identity} if ablation_identity is not None else {}),
    }


def cache_entry_path(cache_root: Path, fingerprint: str) -> Path:
    return Path(cache_root) / fingerprint[:2] / f"{fingerprint}.json"


def synthesis_outcome(
    *,
    status: str,
    synth_status: str,
    generated_spec: dict[str, Any] | None,
    checkpoint: dict[str, Any] | None,
    used_public_scaffold: bool,
    reason: str | None,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the one outcome shape both synthesizing arms read and write.

    ``generated_spec`` is the seeded spec every arm restores; ``checkpoint`` is
    the full ``MethodCheckpoint`` mapping the state arm reconstructs. A success
    must carry both, otherwise the arm that needs the missing projection treats
    the entry as unusable.
    """

    return {
        "status": str(status),
        "synth_status": str(synth_status),
        "generated_spec": generated_spec,
        "checkpoint": checkpoint,
        "used_public_scaffold": bool(used_public_scaffold),
        "reason": reason,
        "artifacts": dict(artifacts or {}),
    }


def synthesis_outcome_usable(outcome: dict[str, Any]) -> bool:
    """Accept an entry only when every arm can consume it.

    The strict executable contract already ran at produce time, so the reader
    only re-checks that both projections of a success survived serialization.
    """

    if outcome.get("status") != "success":
        return True
    generated_spec = outcome.get("generated_spec")
    checkpoint = outcome.get("checkpoint")
    return bool(
        isinstance(generated_spec, dict)
        and generated_spec
        and isinstance(checkpoint, dict)
        and checkpoint
        and isinstance(checkpoint.get("model_generated_spec"), dict)
    )


def read_synthesis_cache(
    path: Path,
    *,
    cache_key: dict[str, Any],
    fingerprint: str,
    outcome_usable: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    """Return a cache entry only when every field still binds to this request."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SELF_SYNTHESIS_CACHE_SCHEMA_VERSION:
        return None
    if payload.get("cache_key_fingerprint") != fingerprint:
        return None
    if payload.get("cache_key") != cache_key:
        return None
    outcome = payload.get("outcome")
    if not isinstance(outcome, dict):
        return None
    if outcome.get("status") not in CACHEABLE_OUTCOME_STATUSES:
        return None
    if not outcome_usable(outcome):
        return None
    if not isinstance(payload.get("synthesis_cost"), dict):
        return None
    if payload["synthesis_cost"].get("usage_complete") is not True:
        return None
    if not isinstance(payload.get("checkpoint_synthesis_ms"), int):
        return None
    return payload


def is_cacheable_payload(payload: dict[str, Any]) -> bool:
    """Only a usage-complete, non-infrastructure, readable outcome is persisted.

    The readability check matters because both arms write to the same path: an
    entry one arm cannot consume would be overwritten on every trial, so the
    cache would report a miss forever instead of sharing one checkpoint.
    """

    outcome = payload.get("outcome")
    return bool(
        isinstance(outcome, dict)
        and outcome.get("status") in CACHEABLE_OUTCOME_STATUSES
        and payload.get("synthesis_cost", {}).get("usage_complete") is True
        and synthesis_outcome_usable(outcome)
    )


def obtain_synthesis(
    *,
    cache_key: dict[str, Any],
    fingerprint: str,
    shared_cache_root: Path | None,
    produce: Callable[[], dict[str, Any]],
    materialize: Callable[..., dict[str, Any]],
    outcome_usable: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Read-or-produce one synthesis payload under a run-scoped lease.

    The produce step runs inside the lease so a concurrent identical trial
    blocks instead of paying for the same checkpoint twice.
    """

    if shared_cache_root is None:
        return materialize(produce(), cache_hit=False, cache_path=None)

    cache_root = Path(shared_cache_root).expanduser().resolve()
    cache_path = cache_entry_path(cache_root, fingerprint)
    with runtime_lease(cache_path, lock_dir=cache_root / "locks"):
        cached = read_synthesis_cache(
            cache_path,
            cache_key=cache_key,
            fingerprint=fingerprint,
            outcome_usable=outcome_usable,
        )
        if cached is not None:
            return materialize(cached, cache_hit=True, cache_path=cache_path)
        payload = produce()
        if is_cacheable_payload(payload):
            atomic_write_json(cache_path, payload)
        return materialize(payload, cache_hit=False, cache_path=cache_path)
