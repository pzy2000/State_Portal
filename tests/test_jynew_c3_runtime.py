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
from warptest.jynew_fuzz import (
    C3_SESSION_VERSION,
    JynewFuzzStateError,
    JynewWarmOutcome,
    JynewWarmSession,
    build_jynew_target,
    project_jynew_fuzz_state,
    sanitize_jynew_fuzz_state,
)
from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs" / "jynew" / "battle-setup.yaml"


def _seed_spec() -> dict:
    return load_spec(SPEC_PATH)


class JynewFuzzStateTests(unittest.TestCase):
    def test_projection_is_limited_to_synthesized_state_fields(self) -> None:
        state = project_jynew_fuzz_state(_seed_spec())
        self.assertEqual(
            set(state), {"player_level", "money", "map_id", "team_ids", "items", "skills", "key_values"}
        )
        self.assertEqual(state["player_level"], 10)
        self.assertEqual(state["team_ids"], [0, 1, 2])
        self.assertNotIn("mod_id", state)
        self.assertNotIn("save_index", state)
        self.assertNotIn("kind", state)

    def test_schema_rejects_escape_fields_and_out_of_bounds_values(self) -> None:
        state = project_jynew_fuzz_state(_seed_spec())
        escaped = {**state, "save_index": 0}
        with self.assertRaises(JynewFuzzStateError):
            sanitize_jynew_fuzz_state(escaped)

        oversized_level = copy.deepcopy(state)
        oversized_level["player_level"] = 10**9
        with self.assertRaises(JynewFuzzStateError):
            sanitize_jynew_fuzz_state(oversized_level)

        too_many_team_ids = copy.deepcopy(state)
        too_many_team_ids["team_ids"] = list(range(100))
        with self.assertRaises(JynewFuzzStateError):
            sanitize_jynew_fuzz_state(too_many_team_ids)

        bad_item_shape = copy.deepcopy(state)
        bad_item_shape["items"] = [{"id": 1, "count": 1, "extra": True}]
        with self.assertRaises(JynewFuzzStateError):
            sanitize_jynew_fuzz_state(bad_item_shape)

    def test_unknown_but_safe_item_id_is_valid_native_input(self) -> None:
        state = project_jynew_fuzz_state(_seed_spec())
        state["items"] = [{"id": 999999, "count": 1}]
        sanitized = sanitize_jynew_fuzz_state(state)
        self.assertEqual(sanitized["items"], [{"id": 999999, "count": 1}])

    def test_build_jynew_target_pins_save_index_and_kind(self) -> None:
        state = project_jynew_fuzz_state(_seed_spec())
        target = build_jynew_target(state, mod_id="JYX2")
        self.assertEqual(target["kind"], "synthesized_state")
        self.assertEqual(target["mod_id"], "JYX2")
        self.assertEqual(target["save_index"], -1)


class JynewWarmProtocolTests(unittest.TestCase):
    def test_request_uses_sequence_and_waits_for_ready(self) -> None:
        state = project_jynew_fuzz_state(_seed_spec())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = JynewWarmSession(root, root / "session", mod_id="JYX2")
            report = {
                "version": C3_SESSION_VERSION,
                "sequence": 1,
                "status": "failure",
                "detail": "State synthesis failed: boom",
                "checks": [{"name": "target.state_synthesized", "status": "failure"}],
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
            self.assertEqual(request["spec"]["target"]["save_index"], -1)
            self.assertEqual(request["spec"]["target"]["mod_id"], "JYX2")
            self.assertEqual(request["spec"]["validations"], [])
            # jynew's own "failure" status (a caught synthesis exception) is
            # graceful handling of an adversarial candidate, not an engine defect.
            self.assertEqual(outcome.status, "robust")
            self.assertEqual(outcome.category, "handled_failure")
            self.assertTrue(wait_report.called)
            self.assertTrue(wait_ready.called)

    def test_report_mapping_classifies_success_failure_rejected_engine_error(self) -> None:
        success = JynewWarmSession._outcome_from_report(
            1, {"status": "success", "detail": "ok"}, elapsed_ms=1
        )
        failure = JynewWarmSession._outcome_from_report(
            2, {"status": "failure", "detail": "handled"}, elapsed_ms=1
        )
        rejected = JynewWarmSession._outcome_from_report(
            3, {"status": "rejected", "detail": "bad protocol"}, elapsed_ms=1
        )
        engine = JynewWarmSession._outcome_from_report(
            4, {"status": "engine_error", "detail": "uncaught exception"}, elapsed_ms=1
        )
        self.assertEqual(
            (success.status, failure.status, rejected.status, engine.status),
            ("robust", "robust", "rejected", "engine_error"),
        )
        self.assertEqual(engine.category, "uncaught_exception")

    def test_read_json_strips_unity_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ready.json"
            path.write_bytes(b'\xef\xbb\xbf{"version":"jynew-c3-session-v1","sequence":0,"status":"ready"}')
            parsed = JynewWarmSession._read_json(path)
            self.assertEqual(parsed and parsed.get("sequence"), 0)

    def test_report_written_before_process_exit_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = JynewWarmSession(root, root / "session", mod_id="JYX2")
            session.report_path.parent.mkdir(parents=True, exist_ok=True)
            session.report_path.write_text(
                json.dumps({"version": C3_SESSION_VERSION, "sequence": 1, "status": "engine_error"}),
                encoding="utf-8",
            )
            with patch.object(session, "is_alive", return_value=False):
                report, problem = session._wait_for_report(1, timeout_s=0.01)

        self.assertIsNone(problem)
        self.assertEqual(report and report.get("status"), "engine_error")

    def test_bad_mod_id_is_rejected_at_construction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(JynewFuzzStateError):
                JynewWarmSession(root, root / "session", mod_id="../escape")


class JynewFuzzRunnerTests(unittest.TestCase):
    def test_dispatches_jynew_without_constructing_browser_state_session(self) -> None:
        task = SimpleNamespace(
            id="jynew-test",
            benchmark="jynew",
            oracle_spec=str(SPEC_PATH.relative_to(ROOT)),
            issue=SimpleNamespace(repo="games/jynew", budget_ms=90_000),
        )
        sentinel = {"status": "success", "dispatched": "jynew"}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fuzzing, "_run_fuzz_jynew", return_value=sentinel) as runner,
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
            def __init__(self, repo: Path, report_dir: Path, *, mod_id: str) -> None:
                self.repo = Path(repo)
                self.report_dir = Path(report_dir)
                self.report_dir.mkdir(parents=True, exist_ok=True)
                self.mod_id = mod_id
                self.git_commit = "jynew-test-commit"
                self.bootstrap_ms = 7
                self.stdout_path = self.report_dir / "jynew-warm.stdout.log"
                self.stderr_path = self.report_dir / "jynew-warm.stderr.log"
                self.stdout_path.write_text("stdout evidence\n", encoding="utf-8")
                self.stderr_path.write_text("stderr evidence\n", encoding="utf-8")
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

            def run(self, fuzz_state: dict, *, timeout_s: float) -> JynewWarmOutcome:
                del timeout_s
                self.calls.append(copy.deepcopy(fuzz_state))
                call = len(self.calls)
                # bootstrap canonical, candidate initial, post-restart canonical,
                # then exact candidate replay.
                if call in {1, 3}:
                    return JynewWarmOutcome(call, "robust", "success", "canonical passed")
                if call == 2:
                    return JynewWarmOutcome(call, "engine_error", "uncaught_exception", "first engine signal")
                return JynewWarmOutcome(
                    call,
                    "engine_error",
                    "uncaught_exception",
                    "replayed engine signal",
                    checks=[{"name": "target.state_synthesized", "status": "failure"}],
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
            candidate["player_level"] = 99_999
            injected = inject_fn(candidate, "field_value", "n/a")
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
                id="jynew-test", benchmark="jynew", oracle_spec=str(SPEC_PATH.relative_to(ROOT))
            )
            with (
                patch.object(fuzzing, "JynewWarmSession", FakeSession),
                patch.object(fuzzing, "_run_evo_into_result", fake_evo),
            ):
                result = fuzzing._run_fuzz_jynew(
                    task,
                    ROOT / "games" / "jynew",
                    seed,
                    {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                    Path(tmp),
                    fuzzing.FuzzConfig(),
                )

            self.assertEqual(result["status"], "success", result.get("reason"))
            mutation = result["mutations"][0]
            self.assertEqual(mutation["verdict"], "defect")
            self.assertEqual(mutation["defect_type"], "jynew_engine_uncaught_exception")
            self.assertEqual(mutation["reachability"], "injection-only")
            session = captured["session"]
            self.assertEqual(session.restarts, 1)
            self.assertEqual(session.calls[0], session.calls[2])  # canonical control after restart
            self.assertEqual(session.calls[1], session.calls[3])  # exact candidate replay
            artifact = Path(tmp) / "defects" / mutation["fingerprint"]
            self.assertTrue((artifact / "state.json").is_file())
            self.assertTrue((artifact / "jynew-warm.stdout.log").is_file())
            self.assertTrue((artifact / "jynew-warm.stderr.log").is_file())
            self.assertTrue((artifact / "canonical-request.json.pending").is_file())
            repro = artifact / "run.sh"
            self.assertTrue(repro.is_file())
            self.assertTrue(os.access(repro, os.X_OK))
            repro_text = repro.read_text(encoding="utf-8")
            self.assertIn("Jyx2.WarptestCheckpoint.RunWarm", repro_text)
            self.assertIn('"sequence"[[:space:]]*:[[:space:]]*2', repro_text)
            state = json.loads((artifact / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["reachability"], "injection-only")
            self.assertEqual(state["canonical_fuzz_state"], session.calls[0])
            self.assertTrue(captured.get("closed"))

    def test_invalid_candidate_is_rejected_without_native_injection(self) -> None:
        seed = _seed_spec()
        calls = {"run": 0}

        class FakeSession:
            mod_id = "JYX2"
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

            def run(self, _state: dict, *, timeout_s: float) -> JynewWarmOutcome:
                del timeout_s
                calls["run"] += 1
                return JynewWarmOutcome(calls["run"], "robust", "success", "ok")

        def fake_evo(
            task: object,
            seed_target: dict,
            inject_fn: object,
            make_record: object,
            result: dict,
            config: object,
        ) -> None:
            del task, config
            candidate = {**seed_target, "save_index": 0}
            injected = inject_fn(candidate, "field_insert", "n/a")
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
                id="jynew-test", benchmark="jynew", oracle_spec=str(SPEC_PATH.relative_to(ROOT))
            )
            with (
                patch.object(fuzzing, "JynewWarmSession", FakeSession),
                patch.object(fuzzing, "_run_evo_into_result", fake_evo),
            ):
                result = fuzzing._run_fuzz_jynew(
                    task,
                    ROOT / "games" / "jynew",
                    seed,
                    {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                    Path(tmp),
                    fuzzing.FuzzConfig(),
                )
        self.assertEqual(result["mutations"][0]["verdict"], "rejected")
        self.assertEqual(calls["run"], 1)  # bootstrap canonical control only

    def test_standalone_repro_requires_control_pass_and_detects_engine_error(self) -> None:
        canonical = project_jynew_fuzz_state(_seed_spec())
        candidate = copy.deepcopy(canonical)
        candidate["player_level"] = 99_999
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "fake-jynew"
            (repo / "jyx2" / "Assets").mkdir(parents=True)
            session = SimpleNamespace(repo=repo, mod_id="JYX2")
            artifact = root / "artifact"
            artifact.mkdir()
            fuzzing._write_jynew_repro(
                artifact,
                session=session,
                canonical_state=canonical,
                fuzz_state=candidate,
            )

            # A fake "Unity" that speaks the RunWarm protocol: ready(0), then for
            # each request echoes success (control) or engine_error (candidate).
            fake_unity = root / "fake-unity.sh"
            fake_unity.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        'REQUEST=""; REPORT=""; READY=""',
                        'while [[ $# -gt 0 ]]; do',
                        '  case "$1" in',
                        '    --warptest-warm-request) REQUEST="$2"; shift 2 ;;',
                        '    --warptest-warm-report) REPORT="$2"; shift 2 ;;',
                        '    --warptest-warm-ready) READY="$2"; shift 2 ;;',
                        '    *) shift ;;',
                        "  esac",
                        "done",
                        'printf \'{"version":"jynew-c3-session-v1","sequence":0,"status":"ready"}\\n\' > "$READY"',
                        "last=0",
                        "while true; do",
                        '  if [[ -f "$REQUEST" ]] && grep -Eq \'"sequence"[[:space:]]*:[[:space:]]*1\' "$REQUEST" && [[ "$last" -lt 1 ]]; then',
                        '    printf \'{"version":"jynew-c3-session-v1","sequence":1,"status":"success","detail":"ok","checks":[]}\\n\' > "$REPORT"',
                        '    printf \'{"version":"jynew-c3-session-v1","sequence":1,"status":"ready"}\\n\' > "$READY"',
                        "    last=1",
                        '  elif [[ -f "$REQUEST" ]] && grep -Eq \'"sequence"[[:space:]]*:[[:space:]]*2\' "$REQUEST" && [[ "$last" -lt 2 ]]; then',
                        '    printf \'{"version":"jynew-c3-session-v1","sequence":2,"status":"engine_error","detail":"boom","checks":[]}\\n\' > "$REPORT"',
                        '    printf \'{"version":"jynew-c3-session-v1","sequence":2,"status":"ready"}\\n\' > "$READY"',
                        "    last=2",
                        "  fi",
                        "  sleep 0.02",
                        "done",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_unity.chmod(0o755)

            env = {
                **os.environ,
                "UNITY_EDITOR_PATH": str(fake_unity),
                "WARPTEST_JYNEW_REPRO_TIMEOUT_S": "5",
            }
            completed = subprocess.run(
                [str(artifact / "run.sh")],
                cwd=artifact,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("REPRODUCED: native jynew engine_error", completed.stdout)


if __name__ == "__main__":
    unittest.main()
