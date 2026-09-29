import json
from pathlib import Path
from types import SimpleNamespace


from scripts import supervise_lazarus_p2p_only as supervisor


def setup(monkeypatch, tmp_path, outcomes):
    (tmp_path / "manifest.yaml").write_text("{}\n")
    monkeypatch.setattr(
        supervisor.sys,
        "argv",
        [
            "supervise",
            "--root",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "manifest.yaml"),
            "--runtime-canary",
            str(tmp_path / "canary.json"),
            "--api-preflight",
            str(tmp_path / "api.json"),
            "--calibration",
            str(tmp_path / "calibration.json"),
        ],
    )
    commands = []

    class Process:
        pid = 123

        def __init__(self, command, **kwargs):
            commands.append(command)
            run = Path(command[command.index("--run-root") + 1])
            run.mkdir()
            self.outcome = outcomes.pop(0)
            (run / "run.json").write_text(json.dumps(self.outcome))
            (run / "run-index.json").write_text("{}")

        def wait(self):
            return 0 if self.outcome["complete"] else 1

    monkeypatch.setattr(supervisor.subprocess, "Popen", Process)
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    sleeps = []
    monkeypatch.setattr(supervisor.time, "sleep", sleeps.append)
    return commands, sleeps


def test_supervisor_retries_only_after_exit_and_resumes(tmp_path, monkeypatch):
    commands, sleeps = setup(
        monkeypatch,
        tmp_path,
        [
            {
                "complete": False,
                "recovery": {
                    "retryable_groups": [{"task_id": "test"}],
                    "terminal_failures": [],
                },
            },
            {"complete": True},
        ],
    )
    assert supervisor.main() == 0
    assert len(commands) == 2 and sleeps == [30]
    assert "--resume-index" not in commands[0]
    assert commands[1][commands[1].index("--resume-index") + 1].endswith(
        "attempt-001/run-index.json"
    )
    assert (
        json.loads((tmp_path / "supervisor-status.json").read_text())["state"]
        == "complete"
    )
    assert supervisor.main() == 0
    assert len(commands) == 2  # Restarting a completed supervisor makes no paid calls.


def test_supervisor_preserves_terminal_failure(tmp_path, monkeypatch):
    commands, sleeps = setup(
        monkeypatch,
        tmp_path,
        [
            {
                "complete": False,
                "recovery": {
                    "retryable_groups": [],
                    "terminal_failures": ["usage_unknown"],
                },
            }
        ],
    )
    assert supervisor.main() == 1
    assert len(commands) == 1 and sleeps == []
    assert (
        json.loads((tmp_path / "supervisor-status.json").read_text())["state"]
        == "needs_recovery"
    )


def test_frozen_backoff_reaches_initial_and_recovery_attempts(tmp_path, monkeypatch):
    from warptest.image_history_backoff import protocol

    commands, _ = setup(monkeypatch, tmp_path, [
        {"complete": False, "recovery": {"retryable_groups": [{"task_id": "test"}], "terminal_failures": []}},
        {"complete": True},
    ])
    (tmp_path / "manifest.yaml").write_text(json.dumps({"image_history_backoff": protocol()}))
    supervisor.sys.argv.append("--kimi-capacity-history-backoff")
    assert supervisor.main() == 0
    assert len(commands) == 2
    assert all(command.count("--kimi-capacity-history-backoff") == 1 for command in commands)
    assert "--resume-index" in commands[1]


def test_missing_frozen_backoff_flag_refuses_before_dispatch(tmp_path, monkeypatch):
    import pytest
    from warptest.image_history_backoff import protocol

    commands, _ = setup(monkeypatch, tmp_path, [])
    (tmp_path / "manifest.yaml").write_text(json.dumps({"image_history_backoff": protocol()}))
    with pytest.raises(ValueError, match="requires --kimi-capacity-history-backoff"):
        supervisor.main()
    assert commands == []


def test_interrupted_paid_attempt_cannot_silently_replay_from_old_index(tmp_path, monkeypatch):
    commands, _ = setup(monkeypatch, tmp_path, [])
    attempt = tmp_path / "attempts/attempt-001"
    attempt.mkdir(parents=True)
    (attempt / "authorization.json").write_text("{}")
    (attempt / "run-index.json").write_text("{}")
    assert supervisor.main() == 1
    assert commands == []
    status = json.loads((tmp_path / "supervisor-status.json").read_text())
    assert status["state"] == "needs_recovery"
    assert "reconcile provider usage" in status["failures"][0]


def test_quarantine_is_preserved_on_every_attempt(tmp_path, monkeypatch):
    commands, _ = setup(monkeypatch, tmp_path, [
        {"complete": False, "recovery": {"retryable_groups": [{"task_id": "other"}], "terminal_failures": []}},
        {"complete": True},
    ])
    quarantine = tmp_path / "quarantine.json"
    quarantine.write_text('{"groups": []}')
    supervisor.sys.argv.extend(["--quarantine", str(quarantine)])
    assert supervisor.main() == 0
    assert len(commands) == 2
    assert all(command[command.index("--quarantine") + 1] == str(quarantine) for command in commands)


def test_explicit_fresh_runtime_is_preserved_on_recovery(tmp_path, monkeypatch):
    commands, _ = setup(monkeypatch, tmp_path, [
        {"complete": False, "recovery": {"retryable_groups": [{"task_id": "test"}], "terminal_failures": []}},
        {"complete": True},
    ])
    fresh = tmp_path / "fresh-runtime"
    supervisor.sys.argv.extend(["--runtime-root", str(fresh)])
    assert supervisor.main() == 0
    assert len(commands) == 2
    assert all(command[command.index("--runtime-root") + 1] == str(fresh) for command in commands)
