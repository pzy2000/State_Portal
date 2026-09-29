from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_lazarus_phase0.py"


def _module():
    spec = importlib.util.spec_from_file_location("lazarus_phase0_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase0_cli_rejects_a_non_preregistered_bootstrap_count(
    tmp_path: Path,
) -> None:
    module = _module()

    with pytest.raises(ValueError, match="10000"):
        module.main(
            [
                "--workspace",
                str(tmp_path),
                "--bootstrap-iterations",
                "1",
            ]
        )


def test_phase0_cli_uses_only_the_dedicated_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    report = tmp_path / "reports" / "phase0.json"
    calls: list[tuple[Path, Path, Path, int]] = []

    def fake_launch(
        workspace: Path,
        *,
        report_path: Path,
        credential_source: Path,
        bootstrap_iterations: int,
    ):
        calls.append((workspace, report_path, credential_source, bootstrap_iterations))
        return SimpleNamespace(
            report_path=report_path,
            preflight=SimpleNamespace(plan=SimpleNamespace(trial_count=8)),
            run=SimpleNamespace(
                complete=True,
                stopped_early=False,
                stop_reason=None,
                reports=(),
            ),
        )

    monkeypatch.setattr(module, "launch_lazarus_phase0", fake_launch)
    assert module.main(["--workspace", str(tmp_path), "--output", str(report)]) == 0

    assert calls == [
        (
            tmp_path.resolve(),
            report.resolve(),
            (tmp_path / "test_model_batch.py").resolve(),
            10_000,
        )
    ]
    assert '"complete": true' in capsys.readouterr().out
