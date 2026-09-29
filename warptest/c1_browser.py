"""Browser-presentation policy for formal background C1 runs."""

from __future__ import annotations


_C1_BROWSER_BENCHMARKS = frozenset(
    {
        "2048",
        "spacerpg",
        "jupyterlab",
        "datingsim",
    }
)


def is_c1_browser_benchmark(benchmark: str) -> bool:
    """Return whether a registered C1 benchmark is driven through Playwright."""

    normalized = str(benchmark).strip().lower().replace("-", "").replace("_", "")
    return normalized in _C1_BROWSER_BENCHMARKS


def c1_task_headless(
    benchmark: str,
    *,
    requested_headless: bool,
    background_browser_headless: bool,
) -> bool:
    """Keep browser pixels off-screen without changing native GUI presentation."""

    if background_browser_headless and is_c1_browser_benchmark(benchmark):
        return True
    return bool(requested_headless)


__all__ = ["c1_task_headless", "is_c1_browser_benchmark"]
