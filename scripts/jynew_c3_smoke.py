#!/usr/bin/env python3
"""Manual jynew C3 warm-session smoke test (no LLM).

Starts JynewWarmSession, injects canonical + two candidates, and prints per-
candidate latency so we can confirm the session is genuinely warm (each inject
should be far faster than bootstrap).
"""
from __future__ import annotations

import copy
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.jynew_fuzz import JynewWarmSession, project_jynew_fuzz_state, sanitize_jynew_fuzz_state
from warptest.runner import load_spec


def main() -> int:
    spec_path = ROOT / "specs/jynew/battle-setup.yaml"
    repo = Path(os.environ.get("WARPTEST_JYNEW_SMOKE_REPO", str(ROOT / "games/jynew")))
    out = Path(os.environ.get("WARPTEST_JYNEW_SMOKE_OUT", "/tmp/jynew-c3-smoke/session"))
    seed_spec = load_spec(spec_path)
    canonical = project_jynew_fuzz_state(seed_spec)
    mod_id = seed_spec["target"]["mod_id"]

    print(f"[smoke] repo={repo} out={out}", flush=True)
    session = JynewWarmSession(repo, out, mod_id=mod_id)
    t0 = time.perf_counter()
    session.start()
    bootstrap_s = time.perf_counter() - t0
    print(f"[smoke] bootstrap_ms={session.bootstrap_ms} wall_s={bootstrap_s:.1f}", flush=True)

    cases: list[tuple[str, dict]] = [
        ("canonical", canonical),
        ("valid_boundary", {**canonical, "player_level": 99999}),
        ("invalid_schema", {**canonical, "save_index": 0}),  # rejected by Python sanitize
    ]

    inject_times: list[float] = []
    for label, candidate in cases:
        if label == "invalid_schema":
            try:
                sanitize_jynew_fuzz_state(candidate)
                print(f"[smoke] {label}: UNEXPECTED sanitize pass", flush=True)
                continue
            except Exception as error:
                print(f"[smoke] {label}: rejected at sanitize ({error})", flush=True)
                continue

        started = time.perf_counter()
        outcome = session.run(candidate, timeout_s=120.0)
        elapsed = time.perf_counter() - started
        inject_times.append(elapsed)
        print(
            f"[smoke] {label}: status={outcome.status} category={outcome.category} "
            f"elapsed_s={elapsed:.2f} detail={outcome.detail[:120]}",
            flush=True,
        )

    # Second canonical on same warm process — should still be fast.
    started = time.perf_counter()
    outcome = session.run(canonical, timeout_s=120.0)
    elapsed = time.perf_counter() - started
    inject_times.append(elapsed)
    print(
        f"[smoke] second_canonical: status={outcome.status} elapsed_s={elapsed:.2f} alive={session.is_alive()}",
        flush=True,
    )

    session.close()
    if inject_times:
        avg = sum(inject_times) / len(inject_times)
        print(
            f"[smoke] inject avg={avg:.2f}s min={min(inject_times):.2f}s "
            f"bootstrap={bootstrap_s:.1f}s ratio={bootstrap_s / max(min(inject_times), 0.01):.1f}x",
            flush=True,
        )
        if bootstrap_s > 5 * min(inject_times):
            print("[smoke] PASS: inject latency << bootstrap (genuinely warm)", flush=True)
        else:
            print("[smoke] WARN: inject latency close to bootstrap — may be pseudo-warm", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
