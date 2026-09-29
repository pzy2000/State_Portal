#!/usr/bin/env python3
"""Run an archived study controller against an unchanged runtime worktree."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    root = args.root.resolve()
    controller = args.controller.resolve()
    sys.path.insert(0, str(root))
    import warptest
    if Path(warptest.__file__).resolve().parent != root / "warptest":
        raise RuntimeError("Runtime package was not imported from the frozen worktree")
    print(json.dumps({"runtime_root": str(root), "controller": str(controller),
                      "controller_sha256": hashlib.sha256(controller.read_bytes()).hexdigest()}), flush=True)
    name = "warptest._archived_ablation_controller"
    spec = importlib.util.spec_from_file_location(name, controller)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load archived study controller")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    sys.argv = [str(controller), "--root", str(root), *remaining]
    module.main()


if __name__ == "__main__":
    main()
