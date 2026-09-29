"""Launch a WarpTest browser spec in a headed browser and hand it to a human.

Usage:
    python3 scripts/play_spec.py specs/2048/near-loss.yaml

Starts the game's dev server, opens a real (headed) Chromium window, restores the
spec's target state through the profile's normal checkpoint path, then keeps the
window alive so a person can play. It exits (and shuts the server down) when the
browser window is closed or Ctrl-C is pressed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from warptest.adapters import LocalStorageSaveAdapter, VanillaDOMAdapter
from warptest.discovery import RepositoryInspector
from warptest.profiles import Game2048Profile, profile_for
from warptest.reports import new_report_dir
from warptest.runner import load_spec, resolve_repo
from warptest.runtime import BrowserRuntimeAdapter, json_dumps


def restore_2048(profile: Game2048Profile, runtime: BrowserRuntimeAdapter, spec: dict) -> None:
    target = spec["target"]
    board = target["board"]
    score = int(target.get("score", 0))
    state = profile._serialized_state(
        board,
        score,
        bool(target.get("won", False)),
        bool(target.get("over", False)),
        bool(target.get("keepPlaying", False)),
    )
    storage_values = {"gameState": json_dumps(state)}
    if "bestScore" in target:
        storage_values["bestScore"] = str(target["bestScore"])

    profile._open_with_hook(runtime, "/?warptest=1", timeout_ms=10_000)
    LocalStorageSaveAdapter(runtime).write_many(storage_values, [], profile.inspect_result.storage_policy)
    runtime.page.reload(wait_until="domcontentloaded")
    VanillaDOMAdapter(runtime).wait_for_tiles(timeout_ms=10_000)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 scripts/play_spec.py <spec.yaml>", file=sys.stderr)
        return 2

    spec_path = Path(sys.argv[1]).expanduser().resolve()
    spec = load_spec(spec_path)
    repo = resolve_repo(spec, spec_path, None)
    inspect_result = RepositoryInspector().inspect(repo)
    profile = profile_for(inspect_result)
    out_dir = new_report_dir()

    runtime = BrowserRuntimeAdapter(repo, inspect_result.metadata.launch_command, out_dir, headless=False)
    runtime.start_server()
    runtime.start_browser()
    try:
        if isinstance(profile, Game2048Profile):
            restore_2048(profile, runtime, spec)
        else:
            raise SystemExit(f"play_spec.py only supports the 2048 profile so far, got {type(profile).__name__}")

        print(f"READY url={runtime.app_url}", flush=True)
        print("Play in the opened window. Close the window (or press Ctrl-C here) to stop.", flush=True)

        while runtime.browser.is_connected() and len(runtime.context.pages) > 0:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
