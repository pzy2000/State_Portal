# LibreOffice Benchmark Adaptation Status

> Last updated: 2026-07-06
> Scope: integrate LibreOffice Writer + Calc + Impress as a WarpTest core benchmark.

## Summary

LibreOffice is integrated into WarpTest along the V2 semantic pipeline: discovery / profile / runtime / schema / synthesis / model-bench / core tasks / hidden oracle mappings / state-loop / effort and paper-table coverage.

V2 is positioned as a **deep-state UNO/ODF document benchmark with before/after visual oracles**, not a GUI click replay benchmark. WarpTest does not modify `games/LibreOffice` and does not add private hooks to the nested repo; checkpoints use only public LibreOffice runtime surfaces: the `soffice` process, the UNO component context, ODF artifacts, PDF export filters, and desktop screenshots in visual mode.

The runtime binary is configurable: `WARPTEST_LIBREOFFICE_SOFFICE` takes precedence, then the `instdir` build output of `games/LibreOffice`, and finally `soffice` on PATH. Reports record the actual `soffice` path and version, so the bundled `soffice` available on this machine can be used for smoke runs, but it is not equivalent to a build of the checkout itself.

## Architecture

```text
specs/libreoffice/*.yaml
  -> runner.run_spec
     -> LibreOfficeRuntimeAdapter.run_checkpoint
        -> isolated UserInstallation under report_dir
        -> helper Python imports uno/pyuno
        -> launch soffice --headless for semantic runs, headed for visual runs
        -> synthesize Writer/Calc/Impress target
        -> run actions and assertions
        -> capture screenshot_before/screenshot_after when visual_capture is enabled
        -> write request/report/checks/logs
```

All documents, exported PDFs, and temporary profiles are written under `reports/warptest/<ts>/.../libreoffice-runtime/` and never written back to `games/LibreOffice`.

## Evaluation Set

- `specs/libreoffice/*.yaml`: 10 deep-state specs, covering 3 Writer, 4 Calc, and 3 Impress.
- `benchmarks/core/public/tasks.yaml`: 10 LibreOffice public tasks.
- `benchmarks/core/hidden/oracles.yaml`: 10 oracle mappings pointing to V2 spec files.
- `benchmarks/model/libreoffice-tasks.yaml`: 10 exact executable-spec prompting tasks.
- `benchmarks/state_loop/tasks_libreoffice.yaml`: 10 LibreOffice-only state-loop tasks; the main state-loop manifest samples three representative tasks: Writer board report, Calc quarterly rollup, and Impress status deck.
- `specs/libreoffice/visual/*.png`: every task has a `golden_before` / `golden_after` pair; `capture_goldens.py` can regenerate real goldens on a machine with pyuno and a desktop that supports screenshots.

## Current Run Requirements

`warptest inspect games/LibreOffice` does not require local pyuno. A real `warptest run specs/libreoffice/*.yaml --repo games/LibreOffice` requires `WARPTEST_LIBREOFFICE_PYTHON` to point to a Python that can `import uno`, and `WARPTEST_LIBREOFFICE_SOFFICE`, `URE_BOOTSTRAP`/`PYTHONPATH`, or `WARPTEST_LIBREOFFICE_PROGRAM_DIR` to match that LibreOffice installation. The visual oracle / state-loop additionally require a desktop session with a display and platform screenshot permission. When these are missing, the run reports a dependency-blocked diagnostic.
