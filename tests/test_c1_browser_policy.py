from warptest.c1_browser import c1_task_headless, is_c1_browser_benchmark


def test_formal_background_policy_forces_only_playwright_tasks_headless() -> None:
    for benchmark in ("2048", "DatingSim", "SpaceRPG", "JupyterLab"):
        assert is_c1_browser_benchmark(benchmark)
        assert c1_task_headless(
            benchmark,
            requested_headless=False,
            background_browser_headless=True,
        )

    for benchmark in ("Blender", "LibreOffice", "OpenRA", "jynew", "pal3", "luanti"):
        assert not is_c1_browser_benchmark(benchmark)
        assert not c1_task_headless(
            benchmark,
            requested_headless=False,
            background_browser_headless=True,
        )


def test_nonformal_runs_preserve_requested_browser_presentation() -> None:
    assert not c1_task_headless(
        "SpaceRPG",
        requested_headless=False,
        background_browser_headless=False,
    )
    assert c1_task_headless(
        "SpaceRPG",
        requested_headless=True,
        background_browser_headless=False,
    )
