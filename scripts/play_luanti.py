"""Launch a Luanti WarpTest scene by id and hand it to a human to play.

Usage:
    python3 scripts/play_luanti.py luanti-mine-bridge-outpost

Takes a single scene id (e.g. ``luanti-mine-bridge-outpost``), synthesizes the
matching spec's world state through the normal WarpTest checkpoint path so the
world is persisted to disk, then reopens that world in a plain Luanti client --
without the ``warptest_request_path`` config, so the worldmod stays inert (it
does not re-synthesize or auto-exit) and you can just play.

It blocks until you close the game window (or press Ctrl-C here).
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from warptest.luanti_runtime import LUANTI_WARPTEST_PLAYER, LuantiRuntimeAdapter
from warptest.runner import load_spec, resolve_repo

SPECS_DIR = _REPO_ROOT / "specs" / "luanti"
_ID_PREFIXES = ("luanti-", "minetest-")


def resolve_spec(scene_id: str) -> Path:
    """Resolve a scene id like ``luanti-mine-bridge-outpost`` to a spec path."""
    direct = Path(scene_id).expanduser()
    if direct.suffix in {".yaml", ".yml"} and direct.exists():
        return direct.resolve()

    stem = scene_id
    for prefix in _ID_PREFIXES:
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break

    for candidate in (SPECS_DIR / f"{stem}.yaml", SPECS_DIR / f"{scene_id}.yaml"):
        if candidate.exists():
            return candidate.resolve()

    available = sorted(f"luanti-{path.stem}" for path in SPECS_DIR.glob("*.yaml"))
    listing = "\n  ".join(available) if available else "(none found)"
    raise SystemExit(
        f"Could not resolve scene id {scene_id!r} to a spec under {SPECS_DIR}/.\n"
        f"Available scene ids:\n  {listing}"
    )


def build_play_cmd(repo: Path, world: Path, binary: Path, app: Path | None, player_name: str) -> list[str]:
    """Build a plain client launch command (no warptest config -> worldmod inert)."""
    if platform.system() == "Darwin" and app is not None:
        return [
            "open", "-n", "-W", str(app), "--args",
            "--go", "--gameid", "devtest",
            "--world", str(world), "--name", player_name,
        ]
    return [
        str(binary), "--go", "--gameid", "devtest",
        "--world", str(world), "--name", player_name,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="play_luanti.py",
        description="Synthesize a Luanti WarpTest scene by id and play it in a real client.",
    )
    parser.add_argument("scene_id", help="Scene id, e.g. luanti-mine-bridge-outpost.")
    parser.add_argument(
        "--player-name",
        default=LUANTI_WARPTEST_PLAYER,
        help=f"Player name to synthesize and play as. Defaults to {LUANTI_WARPTEST_PLAYER}.",
    )
    parser.add_argument(
        "--world-dir",
        default=None,
        help="Override the world output directory. Defaults to reports/play/<stem>.",
    )
    args = parser.parse_args()

    spec_path = resolve_spec(args.scene_id)
    spec = load_spec(spec_path)
    repo = resolve_repo(spec, spec_path, None)

    out = (
        Path(args.world_dir).expanduser()
        if args.world_dir
        else _REPO_ROOT / "reports" / "play" / spec_path.stem
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"scene    = {args.scene_id}", flush=True)
    print(f"spec     = {spec_path}", flush=True)
    print(f"repo     = {repo}", flush=True)
    print("Synthesizing and persisting world (a client window may flash and close -- this is expected)...", flush=True)

    with LuantiRuntimeAdapter(repo, out) as rt:
        result = rt.run_checkpoint(spec, spec_path)
        world = out / f"{spec_path.stem}-world"
        if not world.exists():
            print(f"ERROR: world was not persisted at {world}", file=sys.stderr, flush=True)
            print(f"checkpoint status: {result.get('status')}", file=sys.stderr, flush=True)
            print(result.get("stderr_tail", ""), file=sys.stderr, flush=True)
            return 1
        if result.get("status") != "success":
            # Synthesis of the world can still succeed enough to play even when
            # some assertions fail, so warn but continue.
            print(f"WARNING: checkpoint status={result.get('status')} (continuing anyway).", file=sys.stderr, flush=True)

        binary = rt._ensure_built()
        app = rt._macos_client_app()
        cmd = build_play_cmd(repo, world, binary, app, args.player_name)

        print(f"world    = {world}", flush=True)
        print(f"READY: launching client as {args.player_name!r}. Close the game window (or Ctrl-C here) to stop.", flush=True)
        try:
            subprocess.run(cmd, cwd=str(repo))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
