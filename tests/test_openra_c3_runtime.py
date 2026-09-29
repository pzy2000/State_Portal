from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from warptest import fuzzing
from warptest.openra_fuzz import (
    C3_SESSION_VERSION,
    OpenRAFuzzStateError,
    OpenRAWarmOutcome,
    OpenRAWarmSession,
    project_openra_fuzz_state,
    sanitize_openra_fuzz_state,
)
from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs" / "openra" / "ra-allies07-soviet-war-factory-produces-heavy-tank.yaml"


def _seed_spec() -> dict:
    return load_spec(SPEC_PATH)


class OpenRAFuzzStateTests(unittest.TestCase):
    def test_projection_is_limited_to_players_and_gameplay_actions(self) -> None:
        state = project_openra_fuzz_state(_seed_spec())
        self.assertEqual(set(state), {"players", "actions"})
        self.assertEqual(len(state["actions"]), 2)
        self.assertEqual(
            set(state["players"][0]), {"id", "cash", "fast_build", "build_anywhere"}
        )
        self.assertNotIn("mod", state)
        self.assertNotIn("map", state)
        self.assertNotIn("assertions", state)

    def test_schema_rejects_escape_fields_and_out_of_bounds_values(self) -> None:
        state = project_openra_fuzz_state(_seed_spec())
        escaped = {**state, "map": "other-map"}
        with self.assertRaises(OpenRAFuzzStateError):
            sanitize_openra_fuzz_state(escaped)

        oversized_cash = copy.deepcopy(state)
        oversized_cash["players"][0]["cash"] = 1_000_001
        with self.assertRaises(OpenRAFuzzStateError):
            sanitize_openra_fuzz_state(oversized_cash)

        too_many_actions = copy.deepcopy(state)
        too_many_actions["actions"] = too_many_actions["actions"] * 5
        with self.assertRaises(OpenRAFuzzStateError):
            sanitize_openra_fuzz_state(too_many_actions)

    def test_unknown_but_safe_gameplay_actor_is_valid_native_input(self) -> None:
        state = project_openra_fuzz_state(_seed_spec())
        state["actions"][-1]["actor"] = "missing_actor"
        self.assertEqual(sanitize_openra_fuzz_state(state)["actions"][-1]["actor"], "missing_actor")


class OpenRAWarmProtocolTests(unittest.TestCase):
    def test_request_uses_sequence_and_waits_for_reset_ready(self) -> None:
        state = project_openra_fuzz_state(_seed_spec())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = OpenRAWarmSession(root, root / "session", mod="ra", map_id="allies-07")
            report = {
                "version": C3_SESSION_VERSION,
                "sequence": 1,
                "status": "robust",
                "category": "unknown_actor",
                "detail": "Unknown actor is handled.",
            }
            ready = {"version": C3_SESSION_VERSION, "sequence": 1, "status": "ready"}
            with (
                patch.object(session, "is_alive", return_value=True),
                patch.object(session, "_wait_for_report", return_value=(report, None)) as wait_report,
                patch.object(session, "_wait_for_ready", return_value=(ready, None)) as wait_ready,
            ):
                outcome = session.run(state, timeout_s=1)

            request = json.loads(session.request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["version"], C3_SESSION_VERSION)
            self.assertEqual(request["sequence"], 1)
            self.assertEqual(set(request["fuzz_state"]), {"players", "actions"})
            self.assertNotIn("map", request["fuzz_state"])
            self.assertEqual(outcome.status, "robust")
            self.assertTrue(outcome.reset_ready)
            self.assertTrue(wait_report.called)
            self.assertTrue(wait_ready.called)

    def test_report_mapping_keeps_gameplay_rejections_out_of_engine_defects(self) -> None:
        robust = OpenRAWarmSession._outcome_from_report(
            1,
            {"status": "failure", "category": "queue_rejected", "detail": "queue rejected"},
            elapsed_ms=1,
        )
        rejected = OpenRAWarmSession._outcome_from_report(
            2,
            {"status": "rejected", "category": "protocol", "detail": "bad shape"},
            elapsed_ms=1,
        )
        engine = OpenRAWarmSession._outcome_from_report(
            3,
            {"status": "engine_error", "category": "exception", "detail": "boom"},
            elapsed_ms=1,
        )
        self.assertEqual((robust.status, rejected.status, engine.status), ("robust", "rejected", "engine_error"))

    def test_report_written_before_process_exit_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = OpenRAWarmSession(root, root / "session", mod="ra", map_id="allies-07")
            session.report_path.parent.mkdir(parents=True, exist_ok=True)
            session.report_path.write_text(
                json.dumps(
                    {
                        "version": C3_SESSION_VERSION,
                        "sequence": 1,
                        "status": "engine_error",
                        "category": "exception",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(session, "is_alive", return_value=False):
                report, problem = session._wait_for_report(1, timeout_s=0.01)

        self.assertIsNone(problem)
        self.assertEqual(report and report.get("status"), "engine_error")


class OpenRAFuzzRunnerTests(unittest.TestCase):
    def test_dispatches_openra_without_constructing_browser_state_session(self) -> None:
        task = SimpleNamespace(
            id="openra-test",
            benchmark="OpenRA",
            oracle_spec=str(SPEC_PATH.relative_to(ROOT)),
            issue=SimpleNamespace(repo="games/OpenRA", budget_ms=90_000),
        )
        sentinel = {"status": "success", "dispatched": "openra"}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fuzzing, "_run_fuzz_openra", return_value=sentinel) as runner,
                patch.object(fuzzing, "StateSession", side_effect=AssertionError("browser state session must not run")),
            ):
                result = fuzzing._run_fuzz_task(
                    ROOT, task, out_dir=Path(tmp), headless=True, config=fuzzing.FuzzConfig()
                )
        self.assertEqual(result, sentinel)
        self.assertTrue(runner.called)

    def test_engine_signal_requires_restart_control_and_replay_then_writes_repro(self) -> None:
        seed = _seed_spec()
        captured: dict[str, object] = {}

        class FakeSession:
            def __init__(self, repo: Path, report_dir: Path, *, mod: str, map_id: str) -> None:
                self.repo = Path(repo)
                self.report_dir = Path(report_dir)
                self.report_dir.mkdir(parents=True, exist_ok=True)
                self.mod = mod
                self.map_id = map_id
                self.git_commit = "openra-test-commit"
                self.bootstrap_ms = 7
                self.stdout_path = self.report_dir / "openra-warm.stdout.log"
                self.stderr_path = self.report_dir / "openra-warm.stderr.log"
                self.stdout_path.write_text("stdout evidence\n", encoding="utf-8")
                self.stderr_path.write_text("stderr evidence\n", encoding="utf-8")
                self.screenshot = self.report_dir / "candidate.png"
                self.screenshot.write_bytes(b"fake-png")
                self.calls: list[dict] = []
                self.restarts = 0
                captured["session"] = self

            def start(self) -> "FakeSession":
                return self

            def close(self) -> None:
                captured["closed"] = True

            def flush_logs(self) -> None:
                return None

            def is_alive(self) -> bool:
                return True

            def restart(self) -> "FakeSession":
                self.restarts += 1
                return self

            def run(self, fuzz_state: dict, *, timeout_s: float) -> OpenRAWarmOutcome:
                del timeout_s
                self.calls.append(copy.deepcopy(fuzz_state))
                call = len(self.calls)
                # bootstrap canonical, candidate initial, post-restart canonical,
                # then exact candidate replay.
                if call in {1, 3}:
                    return OpenRAWarmOutcome(call, "robust", "completed", "canonical passed", reset_ready=True)
                if call == 2:
                    return OpenRAWarmOutcome(call, "engine_error", "exception", "first engine signal")
                return OpenRAWarmOutcome(
                    call,
                    "engine_error",
                    "exception",
                    "replayed engine signal",
                    report={"status": "engine_error", "category": "exception", "sequence": call},
                    screenshot_path=str(self.screenshot),
                )

        def fake_evo(
            task: object,
            seed_target: dict,
            inject_fn: object,
            make_record: object,
            result: dict,
            config: object,
        ) -> None:
            del task, config
            candidate = copy.deepcopy(seed_target)
            candidate["actions"][-1]["actor"] = "valid_but_bad_actor"
            injected = inject_fn(candidate, "field_value", "candidate.png")
            record = make_record(
                target=candidate,
                operator_id="field_value",
                provenance="regression",
                generation=1,
                fitness=0.0,
                result=injected,
            )
            result["mutations"] = [record]
            result["llm_meta"] = {"generations": 1}
            result["status"] = "success"

        with tempfile.TemporaryDirectory() as tmp:
            task = SimpleNamespace(
                id="openra-test", benchmark="OpenRA", oracle_spec=str(SPEC_PATH.relative_to(ROOT))
            )
            with (
                patch.object(fuzzing, "OpenRAWarmSession", FakeSession),
                patch.object(fuzzing, "_run_evo_into_result", fake_evo),
            ):
                result = fuzzing._run_fuzz_openra(
                    task,
                    ROOT / "games" / "OpenRA",
                    seed,
                    {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                    Path(tmp),
                    fuzzing.FuzzConfig(),
                )

            self.assertEqual(result["status"], "success", result.get("reason"))
            mutation = result["mutations"][0]
            self.assertEqual(mutation["verdict"], "defect")
            self.assertEqual(mutation["reachability"], "injection-only")
            session = captured["session"]
            self.assertEqual(session.restarts, 1)
            self.assertEqual(session.calls[0], session.calls[2])  # canonical control after restart
            self.assertEqual(session.calls[1], session.calls[3])  # exact candidate replay
            artifact = Path(tmp) / "defects" / mutation["fingerprint"]
            self.assertTrue((artifact / "state.json").is_file())
            self.assertTrue((artifact / "native-report.json").is_file())
            self.assertTrue((artifact / "openra-warm.stdout.log").is_file())
            self.assertTrue((artifact / "openra-warm.stderr.log").is_file())
            self.assertTrue((artifact / "screenshot.png").is_file())
            self.assertTrue((artifact / "canonical-request.json.pending").is_file())
            repro = artifact / "run.sh"
            self.assertTrue(repro.is_file())
            self.assertTrue(os.access(repro, os.X_OK))
            repro_text = repro.read_text(encoding="utf-8")
            self.assertIn('DOTNET_ROLL_FORWARD="${DOTNET_ROLL_FORWARD:-Major}"', repro_text)
            self.assertIn("canonical-request.json.pending", repro_text)
            self.assertIn('"sequence"[[:space:]]*:[[:space:]]*2', repro_text)
            state = json.loads((artifact / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["reachability"], "injection-only")
            self.assertEqual(state["canonical_fuzz_state"], session.calls[0])
            self.assertTrue(captured.get("closed"))

    def test_standalone_repro_requires_control_reset_and_detects_candidate_reset_failure(self) -> None:
        canonical = project_openra_fuzz_state(_seed_spec())
        candidate = copy.deepcopy(canonical)
        candidate["actions"][-1]["actor"] = "missing_actor"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "fake-openra"
            repo.mkdir()
            launcher = repo / "launch-game.sh"
            launcher.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        'for arg in "$@"; do',
                        '  case "$arg" in',
                        '    Launch.WarptestFuzzRequestPath=*) REQUEST="$(echo "$arg" | cut -d= -f2-)" ;;',
                        '    Launch.WarptestFuzzReportPath=*) REPORT="$(echo "$arg" | cut -d= -f2-)" ;;',
                        '    Launch.WarptestFuzzReadyPath=*) READY="$(echo "$arg" | cut -d= -f2-)" ;;',
                        "  esac",
                        "done",
                        'printf \'{"version":"c3-session-v1","status":"ready","sequence":0}\\n\' > "$READY"',
                        "control=0",
                        "candidate=0",
                        "while true; do",
                        '  if [[ "$control" == 0 && -f "$REQUEST" ]] && grep -Eq \'"sequence"[[:space:]]*:[[:space:]]*1\' "$REQUEST"; then',
                        '    printf \'{"version":"c3-session-v1","sequence":1,"status":"success"}\\n\' > "$REPORT"',
                        '    printf \'{"version":"c3-session-v1","status":"ready","sequence":1}\\n\' > "$READY"',
                        "    control=1",
                        '  elif [[ "$control" == 1 && "$candidate" == 0 && -f "$REQUEST" ]] && grep -Eq \'"sequence"[[:space:]]*:[[:space:]]*2\' "$REQUEST"; then',
                        '    printf \'{"version":"c3-session-v1","sequence":2,"status":"success"}\\n\' > "$REPORT"',
                        '    if [[ "$FAKE_RESET_READY" == 1 ]]; then',
                        '      printf \'{"version":"c3-session-v1","status":"ready","sequence":2}\\n\' > "$READY"',
                        "    fi",
                        "    candidate=1",
                        "  fi",
                        "  sleep 0.02",
                        "done",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            session = SimpleNamespace(
                repo=repo,
                support_dir=root / "support",
                mod="ra",
                map_id="allies-07",
            )
            artifact = root / "artifact"
            artifact.mkdir()
            fuzzing._write_openra_repro(
                artifact,
                session=session,
                canonical_state=canonical,
                fuzz_state=candidate,
            )
            env = {**os.environ, "WARPTEST_OPENRA_REPRO_TIMEOUT_S": "2"}
            clean = subprocess.run(
                [str(artifact / "run.sh")],
                cwd=artifact,
                env={**env, "FAKE_RESET_READY": "1"},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            clean_engine_log = (artifact / "openra.stderr.log").read_text(encoding="utf-8")
            reset_failure = subprocess.run(
                [str(artifact / "run.sh")],
                cwd=artifact,
                env={**env, "FAKE_RESET_READY": "0"},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            reset_engine_log = (artifact / "openra.stderr.log").read_text(encoding="utf-8")

        self.assertEqual(clean.returncode, 1, clean.stdout + clean.stderr + clean_engine_log)
        self.assertEqual(reset_failure.returncode, 0, reset_failure.stdout + reset_failure.stderr + reset_engine_log)
        self.assertIn("reset-ready did not", reset_failure.stderr)

    def test_crash_artifact_keeps_client_outcome_when_native_report_is_missing(self) -> None:
        canonical = project_openra_fuzz_state(_seed_spec())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_dir = root / "session"
            report_dir.mkdir()
            stdout = report_dir / "stdout.log"
            stderr = report_dir / "stderr.log"
            stdout.write_text("stdout\n", encoding="utf-8")
            stderr.write_text("stderr\n", encoding="utf-8")
            session = SimpleNamespace(
                repo=root,
                report_dir=report_dir,
                stdout_path=stdout,
                stderr_path=stderr,
                mod="ra",
                map_id="allies-07",
                git_commit="test-commit",
            )
            task = SimpleNamespace(
                id="openra-test",
                benchmark="OpenRA",
                oracle_spec=str(SPEC_PATH.relative_to(ROOT)),
            )
            record = {
                "verdict": "defect",
                "fingerprint": "no-native-report",
                "defect_type": "openra_engine_process_exit",
                "detail": "process exited",
                "operator_id": "field_value",
                "mutation": "field_value",
            }
            outcome = OpenRAWarmOutcome(
                sequence=2,
                status="engine_error",
                category="process_exit",
                detail="OpenRA warm session is not running",
            )
            fuzzing._dump_openra_defect(
                task=task,
                task_dir=root / "task",
                session=session,
                record=record,
                canonical_state=canonical,
                fuzz_state=canonical,
                evidence={"initial": outcome.to_dict()},
            )
            report = json.loads(
                (root / "task" / "defects" / "no-native-report" / "native-report.json").read_text(encoding="utf-8")
            )

        self.assertEqual(report["source"], "warptest-client-outcome-no-native-report")
        self.assertEqual(report["outcome"]["category"], "process_exit")

    def test_invalid_candidate_is_rejected_without_native_injection(self) -> None:
        seed = _seed_spec()
        calls = {"run": 0}

        class FakeSession:
            mod = "ra"
            map_id = "allies-07"
            git_commit = "test"
            bootstrap_ms = 1

            def __init__(self, repo: Path, report_dir: Path, **_kwargs: object) -> None:
                self.repo = Path(repo)
                self.report_dir = Path(report_dir)
                self.stdout_path = self.report_dir / "stdout.log"
                self.stderr_path = self.report_dir / "stderr.log"

            def start(self) -> "FakeSession":
                return self

            def close(self) -> None:
                return None

            def is_alive(self) -> bool:
                return True

            def run(self, _state: dict, *, timeout_s: float) -> OpenRAWarmOutcome:
                del timeout_s
                calls["run"] += 1
                return OpenRAWarmOutcome(calls["run"], "robust", "completed", "ok", reset_ready=True)

        def fake_evo(
            task: object,
            seed_target: dict,
            inject_fn: object,
            make_record: object,
            result: dict,
            config: object,
        ) -> None:
            del task, config
            candidate = {**seed_target, "map": "attempted-escape"}
            injected = inject_fn(candidate, "field_insert", "candidate.png")
            result["mutations"] = [
                make_record(
                    target=candidate,
                    operator_id="field_insert",
                    provenance="regression",
                    generation=1,
                    fitness=0.0,
                    result=injected,
                )
            ]
            result["llm_meta"] = {}
            result["status"] = "success"

        with tempfile.TemporaryDirectory() as tmp:
            task = SimpleNamespace(
                id="openra-test", benchmark="OpenRA", oracle_spec=str(SPEC_PATH.relative_to(ROOT))
            )
            with (
                patch.object(fuzzing, "OpenRAWarmSession", FakeSession),
                patch.object(fuzzing, "_run_evo_into_result", fake_evo),
            ):
                result = fuzzing._run_fuzz_openra(
                    task,
                    ROOT / "games" / "OpenRA",
                    seed,
                    {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                    Path(tmp),
                    fuzzing.FuzzConfig(),
                )
        self.assertEqual(result["mutations"][0]["verdict"], "rejected")
        self.assertEqual(calls["run"], 1)  # bootstrap canonical only


if __name__ == "__main__":
    unittest.main()
