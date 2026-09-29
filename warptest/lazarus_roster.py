"""Single source of truth for the published C1-Lazarus roster's shape.

The formal matrix, its manifest and its aggregation all refuse a roster whose
size they do not recognise.  Those three contracts previously repeated the
frozen roster's size as a literal, so growing the roster meant changing the same
number in several places and hoping none was missed.  They now read it here.

The frozen subset the freeze sprint published is kept here too, because the
expansion appends rows beside it and must never reshape it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml


# Published roster: the frozen 24 rows plus the live-run expansion batch.
FORMAL_TASK_COUNT = 30
FORMAL_LOCUS_MIX = {"at_goal": 20, "on_path": 10}

# One trial per task, version, arm and seed.
FORMAL_VERSIONS = 2
FORMAL_ARMS = 2
FORMAL_REPEATS = 1
FORMAL_SEEDS = 3
FORMAL_TRIAL_COUNT = (
    FORMAL_TASK_COUNT * FORMAL_VERSIONS * FORMAL_ARMS * FORMAL_REPEATS * FORMAL_SEEDS
)

# The paid-run authorization names the scope it was granted for.  Growing the
# roster therefore invalidates an older grant instead of silently extending it
# over the cells the reviewer never saw.
FORMAL_PAID_RUN_KEY = f"formal_{FORMAL_TRIAL_COUNT}_paid_run"
_SUPERSEDED_PAID_RUN_KEYS = ("formal_288_paid_run",)


def superseded_paid_run_keys(authorization: object) -> tuple[str, ...]:
    """Return any narrower paid-run grants a manifest still carries."""

    if not isinstance(authorization, Mapping):
        return ()
    return tuple(
        key
        for key in _SUPERSEDED_PAID_RUN_KEYS
        if key != FORMAL_PAID_RUN_KEY and key in authorization
    )

# The freeze-sprint rows inside the published roster, before the expansion.
FROZEN_SUBSET_TASK_COUNT = 24
FROZEN_SUBSET_LOCUS_MIX = {"at_goal": 16, "on_path": 8}


def unbound_formal_task_ids(
    tasks: Sequence[Mapping[str, Any]], *, repository_root: str | Path
) -> tuple[str, ...]:
    """Return roster rows whose oracle spec declares no executable probe.

    A row can hold a passed construction gate and a published golden triplet and
    still be undispatchable, because scoring a trial needs a runtime binding for
    its probe adapter.  Such a row is legitimate evidence but must not be counted
    as runnable, so the formal matrix is refused while any row is unbound.
    """

    root = Path(repository_root).expanduser().resolve()
    unbound: list[str] = []
    for task in tasks:
        spec_value = task.get("oracle_spec")
        if not isinstance(spec_value, str) or not spec_value.strip():
            continue
        spec = yaml.safe_load((root / spec_value).read_text(encoding="utf-8"))
        setup = spec.get("setup") if isinstance(spec, Mapping) else None
        if not isinstance(setup, Mapping):
            continue
        for key, probe in setup.items():
            if not str(key).endswith("_lazarus_probe") or not isinstance(probe, Mapping):
                continue
            if probe.get("probe_executable") is False:
                unbound.append(str(task.get("id")))
    return tuple(unbound)
