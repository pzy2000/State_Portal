"""CLI for packaging the registered Luanti native toolchain."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .package_luanti_native_toolchain import package_luanti_native_toolchain


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package the audited Darwin/arm64 Luanti native toolchain offline."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        runtime = package_luanti_native_toolchain(
            artifact_root=arguments.artifact_root,
            confirmation=arguments.confirm,
        )
    except (RuntimeError, ValueError, PermissionError) as error:
        parser.error(str(error))
    print(runtime)
    return 0


__all__ = ["main"]
