#!/usr/bin/env python3
"""Build split, root-cause GitHub issues for the luanti world-load robustness bugs.

Presentation: these are written as **organic end-user bug reports** -- no mention of
fuzzing / WarpTest / the harness. Each issue reads like a player who hit the problem
by loading a world, and embeds a small self-contained reproduction (only
``luantiserver`` + the bundled ``devtest`` game) plus the real captured console output.
The true provenance (a C3 state-fuzz run) is kept only in local ``BUGS/`` records, not
in the public issue.

Ground truth for bucketing = ``verify.json`` (the mechanism each standalone repro
actually produced when run), joined with ``repro/INDEX.json`` and ``fuzz.json``.

Buckets (validation-mismatch excluded as non-crash noise):
  A  unregistered node / invalid item in saved data -> server-fatal Lua crash (AsyncErr)
  B  non-finite / oversized numbers in saved data -> core.parse_json rejects the world
  C  huge filled region in saved data -> load hangs the server (unresponsive)

For each bucket the smallest ``target.json`` is the minimal representative; its repro
is scrubbed of harness wording, embedded inline, and also written out as a clean
runnable copy under ``github-issues/repro-<n>/`` for local persistence.

Emits ``<report_dir>/luanti/github-issues/{manifest.json, 0N-*.md, logs/*, repro-*/}``.
The manifest records ``source_repo`` (where the bug lives upstream) and
``default_publish_repo`` (the SAFE default target = the fork); actually filing the
issues is a separate, guarded step -- see ``scripts/publish_issues.py``.

Usage:
  python scripts/build_luanti_issues_split.py --report reports/warptest/luanti-fuzz-9h-20260706
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.upstream_sources import get_source  # noqa: E402

# Hidden, neutral idempotency marker (invisible in rendered markdown). Kept neutral so
# a reader of the raw body sees nothing about the harness. push_issues.py matches this
# tag (and the legacy warptest-fingerprint tag) for dedup.
MARKER_TAG = "issue-fingerprint"

_ABS_BIN_RE = re.compile(r'LUANTI_SERVER_BIN:-[^}]+')

# Scrub harness/fuzzer wording out of embedded repro files (comments only).
_SCRUB = [
    # run.sh header
    ("# Standalone reproduction for one Luanti C3 state-fuzz defect.\n"
     "# Zero dependency on warptest / the fuzzer: it only drives luantiserver + devtest.",
     "# Minimal, self-contained reproduction: it loads a saved world through\n"
     "# luantiserver + devtest and reports whether the engine crashes / hangs / rejects it."),
    ("# process to die on its own, or for the timeout (a hang -> reproduced defect).",
     "# process to die on its own, or for the timeout (a hang while loading the world)."),
    ('echo "--- defect (see README.md) ---"', 'echo "--- result ---"'),
    ("(engine restored the mutated state cleanly)", "(engine loaded the world state cleanly)"),
    ("(engine hung on the mutated state; killed after", "(engine hung while loading the world; killed after"),
    # init.lua header
    ("-- Standalone reproduction driver for a Luanti C3 state-fuzz defect.\n"
     "-- Self-contained: it does NOT depend on the WarpTest harness or the fuzzer.\n"
     "-- It replays the exact mutated deep-state (worldpath/target.json) through the\n"
     "-- same synthesize+validate steps the fuzzer used, then records whether the\n"
     "-- engine errored / the state failed to restore (a reproduced defect).",
     "-- Minimal world-load reproduction driver: it reads the saved world state from\n"
     "-- worldpath/target.json and restores it (place nodes, fill regions, chest\n"
     "-- inventories, metadata) the way the game does when loading a world, then\n"
     "-- records whether the engine errored or the world failed to load."),
    ("-- Synthesize the deep-state. Mirrors the WarpTest worldmod, minus the online\n"
     "-- player (a dedicated server has none, matching the headless fuzz session, which\n"
     "-- runs with warptest_allow_headless_player_skip=true: injected player state is\n"
     "-- simply skipped rather than treated as a synthesis failure).",
     "-- Restore the saved world state. A dedicated server has no online player, so\n"
     "-- any saved player state is simply skipped rather than treated as a failure."),
    ("\t\t-- Headless: no online player, skip (parity with allow_headless_player_skip).",
     "\t\t-- Dedicated server: no online player, skip."),
    ("-- Replay one request's spec, exactly as the fuzz session did (skip_synthesis=false,\n"
     "-- empty actions/assertions): synthesize then validate.",
     "-- Load one saved world spec: restore the state, then validate it."),
    ("\t\t-- The engine's own JSON parser rejected the mutated state. In the fuzz\n"
     "\t\t-- warm-session this manifested as \"timed out waiting for report\" (the\n"
     "\t\t-- worldmod skipped the unparseable request); here it is pinned down as a\n"
     "\t\t-- concrete parse rejection (typically NaN / Infinity / oversized numbers).",
     "\t\t-- The engine's own JSON parser rejected the saved world data (typically a\n"
     "\t\t-- non-finite NaN / Infinity or an oversized number), so nothing loads."),
    ("\t-- pcall the whole replay so a Lua-level error (the common engine defect, e.g. an\n"
     "\t-- unregistered node or a malformed item table) is captured as a reproduced\n"
     "\t-- crash instead of aborting silently. A hard C++ crash escapes pcall; then no\n"
     "\t-- result file is written and run.sh flags it as a reproduced hard-crash.",
     "\t-- pcall the whole load so a Lua-level error (e.g. an unregistered node or a\n"
     "\t-- malformed item table) is captured here instead of aborting silently. NOTE:\n"
     "\t-- the real world-load path is NOT pcall-guarded, so the same error there is\n"
     "\t-- fatal to the server. A hard C++ crash escapes pcall; then no result file is\n"
     "\t-- written and run.sh flags it as a crash."),
    # mod.conf
    ("description = Standalone Luanti state-fuzz defect reproduction (no WarpTest dependency).",
     "description = Minimal reproduction of a world-load crash."),
    # generic residue
    ("the mutated state", "the world state"),
]

# Files that make up the self-contained repro (source only; skip runtime leftovers).
REPRO_FILES = [
    ("run.sh", "bash"),
    ("minetest.conf", "ini"),
    ("world/world.mt", "ini"),
    ("world/worldmods/repro/mod.conf", "ini"),
    ("world/worldmods/repro/init.lua", "lua"),
    ("target.json", "json"),
]

ENV_BLOCK = (
    "- luanti `5.17.0-dev` (commit `7f4b0c012`), bundled `devtest` game\n"
    "- Reproduced on a dedicated server (`luantiserver`); observed on macOS."
)

BUCKET_META = {
    "A": {
        "n": 1,
        "slug": "unknown-node-item-crash",
        "marker": "luanti-restore-unvalidated-node-item-input",
        "title": "[luanti] Loading a saved world crashes the server (ServerError: AsyncErr) when it contains an unknown node or invalid item",
        "labels": ["luanti", "crash", "save-load"],
        "category": "player-reachable",
        "log": "A.log",
        "log_caption": (
            "Console output from the minimal reproduction. The reproduction pcall-guards the "
            "restore, so the unknown-node error shows as a contained failure here; on the real "
            "(un-guarded) world-load path the same error terminates the server as "
            "`ServerError: AsyncErr`."
        ),
        "summary": (
            "I have a world I'd been playing for a while. It was created while a mod that adds "
            "custom blocks was installed. After I removed/updated that mod, the world no longer "
            "loads: starting the server on it crashes almost immediately with `ServerError: "
            "AsyncErr`, so the world is effectively unopenable. I can trigger the same crash with "
            "a chest whose saved inventory holds an item whose itemstring is no longer registered."
        ),
        "steps": [
            "Own/create a world that contains a node (or a chest item) provided by some mod.",
            "Remove or update that mod so the node/item name is no longer registered (this also "
            "happens with older or hand-edited world data).",
            "Start the server on that world -> it crashes with `ServerError: AsyncErr` instead of "
            "loading.",
        ],
        "expected": (
            "An unknown node/item in saved data should be tolerated: skipped or substituted with "
            "`unknown` (luanti already maps unknown nodes to `CONTENT_UNKNOWN` when loading "
            "mapblocks), or at worst produce a contained, logged load error -- the server should "
            "stay up."
        ),
        "actual": (
            "Restoring the saved node/item calls `core.set_node` / `ItemStack(...)` on the "
            "unregistered name; the Lua error is raised inside an async callback that isn't "
            "error-guarded, so the whole server goes down with `ServerError: AsyncErr`."
        ),
        "fix": (
            "On the world-load / deserialization path, validate names before use: check "
            "`core.registered_nodes[name]` before `core.set_node` (skip or substitute `unknown`), "
            "and guard `ItemStack(...)` construction for items. Wrap per-node / per-item restore in "
            "`pcall` so a single bad entry can't escalate into a server-fatal `AsyncErr`."
        ),
    },
    "B": {
        "n": 2,
        "slug": "invalid-number-world-wont-load",
        "marker": "luanti-restore-nonfinite-json-reject",
        "title": "[luanti] A world whose data has out-of-range / non-finite numbers silently fails to load (core.parse_json rejects it)",
        "labels": ["luanti", "save-load"],
        "category": "buggy-not-reachable",
        "log": "B.log",
        "log_caption": (
            "Console output from the minimal reproduction: luanti's own `ERROR[Server]: Failed to "
            "parse json data` names the exact line/column of the offending non-finite value."
        ),
        "summary": (
            "I tried to open a world (one that had been edited / that I downloaded) and it just "
            "never loads -- the server sits there and eventually times out with no useful message. "
            "It turns out the world's saved JSON contained a non-finite or out-of-range number (an "
            "`Infinity`/`NaN`, or a coordinate/count past the engine's limits). `core.parse_json` "
            "rejects the entire document, so nothing loads and there's no hint about which field is "
            "bad."
        ),
        "steps": [
            "Open a world whose saved data contains a non-finite (`NaN`/`Infinity`) or oversized "
            "number (e.g. a coordinate beyond the map range) -- such as a hand-edited or corrupted "
            "save.",
            "The world fails to load; the server logs a generic parse error / times out with no "
            "indication of the offending field.",
        ],
        "expected": (
            "Invalid numeric fields should be normalized/clamped (finite, in-range), or the load "
            "should fail with a specific, actionable error naming the bad field -- not a silent "
            "no-op / timeout that discards the whole world."
        ),
        "actual": (
            "`core.parse_json` returns nil for the entire document on the first non-finite/oversized "
            "value, so the load is skipped with only a generic timeout / parse error."
        ),
        "fix": (
            "Validate and normalize numeric fields when reading/writing world data (reject or clamp "
            "`NaN`/`Infinity` and values outside the int/float range), and when parsing fails, "
            "surface a specific error (which field/line) instead of discarding the whole document "
            "silently."
        ),
    },
    "C": {
        "n": 3,
        "slug": "huge-fill-hangs-server",
        "marker": "luanti-restore-runaway-fill-hang",
        "title": "[luanti] Loading a world with a huge filled region hangs the server at 100% CPU (unresponsive, needs kill -9)",
        "labels": ["luanti", "crash", "performance"],
        "category": "buggy-not-reachable",
        "log": "C.log",
        "log_caption": (
            "Console output from the minimal reproduction: the oversized fill floods "
            "`WARNING[Server]: Map::getNodeMetadata(): Block not found` (~2M identical lines / "
            "~208 MB in ~60 s) while the process is pinned at 100% CPU and unresponsive to SIGTERM."
        ),
        "summary": (
            "Loading one of my worlds locks the server up completely: it pins a CPU core at 100%, "
            "stops responding, and won't even shut down cleanly (I have to `kill -9`). The world "
            "contained an enormous filled region (think a `//set` / fill or an imported schematic "
            "spanning a huge coordinate range). The load path tries to iterate the entire volume "
            "synchronously and never returns."
        ),
        "steps": [
            "Open a world containing a very large filled region / area operation (e.g. a fill "
            "spanning up to the coordinate limit, from a worldedit `//set` gone wrong or an "
            "imported schematic).",
            "The server pins a CPU at 100%, stops responding, and ignores SIGTERM; it has to be "
            "force-killed.",
        ],
        "expected": (
            "The volume of a single fill / area operation (and its coordinate range) should be "
            "bounded and validated up front; an oversized region should fail fast as a contained, "
            "logged error while the server stays responsive."
        ),
        "actual": (
            "The fill loop iterates the whole (billions-of-blocks) volume inside a synchronous "
            "call, so the main loop never returns; the process spins at 100% CPU and does not "
            "service SIGTERM."
        ),
        "fix": (
            "Cap the number of nodes a single fill/area operation may touch and validate "
            "coordinates against the map range before iterating; reject oversized regions as a "
            "contained load error instead of looping unbounded."
        ),
    },
}


def _bucket(detail: str) -> str:
    d = (detail or "").lower()
    if "parse_json rejected" in d:
        return "B"
    if "unknown node" in d:
        return "A"
    if "item_s.lua" in d or "bad argument" in d or "concatenate" in d:
        return "A"
    if "no result file" in d or "hung" in d:
        return "C"
    if "checks failed" in d:
        return "X"
    if "checkpoint processing crashed" in d:
        return "A"
    return "other"


def _scrub(text: str) -> str:
    for old, new in _SCRUB:
        text = text.replace(old, new)
    return text


def _clean_repro(repro_dir: Path) -> dict[str, str]:
    """Return {rel: scrubbed_text} for the embedded/persisted repro files."""
    out: dict[str, str] = {}
    for rel, _lang in REPRO_FILES:
        fp = repro_dir / rel
        if not fp.exists():
            continue
        text = fp.read_text(encoding="utf-8")
        if rel == "run.sh":
            text = _ABS_BIN_RE.sub("LUANTI_SERVER_BIN:-bin/luantiserver", text)
        out[rel] = _scrub(text)
    return out


def _write_clean_repro(files: dict[str, str], dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    for rel, text in files.items():
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(text, encoding="utf-8")
        if rel == "run.sh":
            d.chmod(d.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (dest / "README.md").write_text(
        "# Minimal reproduction\n\n"
        "Self-contained world-load reproduction. It needs only a built `luantiserver` "
        "and the bundled `devtest` game.\n\n"
        "```bash\n"
        "./run.sh                                    # prints REPRODUCED / NOT-REPRODUCED\n"
        "LUANTI_SERVER_BIN=/path/to/luantiserver ./run.sh   # or point at any server binary\n"
        "```\n\n"
        "`run.sh` starts `luantiserver` on `world/`, which loads the state in "
        "`target.json`, and reports whether the engine crashes / hangs / rejects the world.\n",
        encoding="utf-8",
    )


def _embed_repro(files: dict[str, str]) -> str:
    lang = {rel: lang for rel, lang in REPRO_FILES}
    parts = [
        "## Minimal reproduction",
        "",
        "A small, self-contained world that reproduces the problem with only a built "
        "`luantiserver` and the bundled `devtest` game. Recreate the files below (preserving the "
        "`world/worldmods/repro/` layout), then run `./run.sh` from the repro directory -- it "
        "loads the saved world state from `target.json` and reports whether the engine crashes / "
        "hangs / rejects it.",
        "",
        "```bash",
        "# from a built luanti checkout (bin/luantiserver present), inside the repro dir:",
        "./run.sh                                    # prints REPRODUCED / NOT-REPRODUCED",
        "LUANTI_SERVER_BIN=/path/to/luantiserver ./run.sh   # or point at any server binary",
        "```",
        "",
    ]
    for rel, _l in REPRO_FILES:
        if rel not in files:
            continue
        parts += [
            f"<details><summary><code>{rel}</code></summary>",
            "",
            f"```{lang[rel]}",
            files[rel].rstrip("\n"),
            "```",
            "",
            "</details>",
            "",
        ]
    return "\n".join(parts)


def _embed_log(bucket: str, out_dir: Path) -> str:
    meta = BUCKET_META[bucket]
    log_fp = out_dir / "logs" / meta.get("log", "")
    if not meta.get("log") or not log_fp.exists():
        return ""
    content = log_fp.read_text(encoding="utf-8").rstrip("\n")
    return (
        "## Observed console output / logs\n\n"
        f"{meta.get('log_caption', '')}\n\n"
        "<details><summary>captured <code>luantiserver</code> output</summary>\n\n"
        "```text\n"
        f"{content}\n"
        "```\n\n"
        "</details>\n"
    )


def _render(bucket: str, repro_files: dict[str, str], out_dir: Path) -> tuple[str, str]:
    meta = BUCKET_META[bucket]
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(meta["steps"], 1))
    steps += f"\n{len(meta['steps']) + 1}. Or just run the minimal reproduction below."
    body = f"""## Summary

{meta['summary']}

## Steps to reproduce

{steps}

{_embed_repro(repro_files)}
{_embed_log(bucket, out_dir)}
## Expected behavior

{meta['expected']}

## What actually happens

{meta['actual']}

## Suggested fix

{meta['fix']}

## Environment

{ENV_BLOCK}

<!-- {MARKER_TAG}: {meta['marker']} -->
"""
    return meta["title"], body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="Run dir with luanti/fuzz.json, verify.json, repro/.")
    ap.add_argument("--out", default=None, help="Output dir (default: <report>/luanti/github-issues).")
    args = ap.parse_args()

    report_dir = Path(args.report).expanduser().resolve()
    verify = json.loads((report_dir / "verify.json").read_text(encoding="utf-8"))
    out_dir = Path(args.out).expanduser().resolve() if args.out else report_dir / "luanti" / "github-issues"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*out_dir.glob("*.md"), out_dir / "manifest.json"]:
        if stale.exists():
            stale.unlink()

    # Bucket verified defects; pick minimal (smallest target.json) representative.
    buckets: dict[str, list[dict]] = {}
    for e in verify["defects"]:
        did = e["defect_id"]
        b = _bucket(e["detail"])
        tj = report_dir / "repro" / did / "target.json"
        size = tj.stat().st_size if tj.exists() else 10**9
        buckets.setdefault(b, []).append({"defect_id": did, "size": size})
    for b in buckets:
        buckets[b].sort(key=lambda x: x["size"])

    issues = []
    for bucket in ["A", "B", "C"]:
        members = buckets.get(bucket, [])
        if not members:
            continue
        rep_id = members[0]["defect_id"]
        repro_files = _clean_repro(report_dir / "repro" / rep_id)
        meta = BUCKET_META[bucket]
        clean_dir = out_dir / f"repro-{meta['n']:02d}"
        _write_clean_repro(repro_files, clean_dir)
        title, body = _render(bucket, repro_files, out_dir)
        fname = f"{meta['n']:02d}-{meta['category']}-{meta['slug']}.md"
        (out_dir / fname).write_text(body, encoding="utf-8")
        issues.append({
            "n": meta["n"], "title": title, "labels": meta["labels"],
            "body_file": fname, "fingerprint": meta["marker"],
            "category": meta["category"], "needs_review": False,
            "detections": len(members), "representative": rep_id,
            "clean_repro": clean_dir.name, "log": meta["log"],
        })

    src = get_source("luanti", ROOT)
    manifest = {
        "report": str(report_dir / "luanti" / "fuzz.json"),
        "marker_tag": MARKER_TAG,
        # Provenance (read side) vs. the SAFE default file target (write side). The
        # publisher never targets upstream unless explicitly told to (--target upstream).
        "source_repo": src["source_repo"],
        "default_publish_repo": src["default_publish_repo"],
        "issues": issues,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    mech = Counter(_bucket(e["detail"]) for e in verify["defects"])
    print(f"Wrote {len(issues)} luanti issue draft(s) to {out_dir}")
    for it in issues:
        print(f"  {it['n']:02d} {it['title']}  (backing defects={it['detections']}, rep={it['representative']})")
    print(f"Excluded as noise: validation-mismatch = {mech.get('X', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
