from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from warptest import lazarus_p2p_only_paid_runtime as paid
from warptest import lazarus_p2p_only_synthesis as synthesis
from warptest.lazarus_grading import VisualJudgement
from warptest.lazarus_orchestrator import LazarusTrialKey
from warptest.lazarus_p2p_only_contract import C, D, E, method_fields
from warptest.lazarus_p2p_only_generated_runtime import validate_observed_start
from warptest.lazarus_p2p_only_identity import (
    canonical_sha256,
    file_sha256,
    schedule_sha256,
)
from warptest.lazarus_p2p_only_spec import spec_errors
from warptest.lazarus_trial import LazarusVisualEvidence
from warptest.task_schema import MethodCheckpoint

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = yaml.safe_load(
    (ROOT / "tests/fixtures/lazarus_p2p_only_v3_specs.yaml").read_text()
)
TASKS = {
    t["id"]: t
    for t in yaml.safe_load(
        (ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml").read_text()
    )["tasks"]
}
TASK = "lzp-jupyterlab-17222-toolbar-button-pressed-toggle"


class Session:
    def __init__(self, benchmark):
        self.benchmark = benchmark
        self.state = None
        self.actions = []

    def capture_policy_frame(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (300, 100), "white")
        ImageDraw.Draw(image).text(
            (5, 5), json.dumps([self.state, self.actions], sort_keys=True), fill="black"
        )
        image.save(p)
        return str(p)

    capture_evidence_frame = capture_policy_frame

    def restore_generated_state(self, state):
        self.state = copy.deepcopy(state)
        return self.state

    def export_state(self):
        return copy.deepcopy(self.state)

    def observe_start(self):
        return {
            "state": self.state or {},
            "visible": self.state is not None,
            "runtime_healthy": True,
            "dialog": "none",
            "goal_dialog_open": False,
            "visible_worlds": [{"name": "p2p_world", "mods": ["p2p_world_mod"]}],
            "widgets": [{"id": "TILE_WIDGETS", "visible": False}],
        }

    def execute_semantic(self, action):
        self.actions.append(copy.deepcopy(action))
        return {"accepted": True}


def context(tmp_path, arm=E, version="buggy", task=TASK):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "component.ts").write_text("export const component = 1")
    return SimpleNamespace(
        key=LazarusTrialKey(arm, "test/model", 0, task, version),
        task=TASKS[task],
        agent_source=SimpleNamespace(
            root=source,
            sanitized_tree_sha256="a" * 64,
            forbidden_tokens=("SECRET_ORACLE",),
        ),
        artifact=SimpleNamespace(root=source),
    )


def runner(tmp_path, monkeypatch, spec=None):
    instance = paid.P2POnlyPaidRuntimeRunner(
        workspace=ROOT, runtime_root=tmp_path / "run"
    )
    observations = []
    generated = []
    phase_calls = []

    @contextmanager
    def session(ctx, *, benchmark, output):
        value = Session(benchmark)
        observations.append(value)
        yield value

    @contextmanager
    def hold(*args, **kwargs):
        yield

    def obtain(ctx, **kwargs):
        hit = bool(generated)
        if not hit:
            generated.append(copy.deepcopy(spec or SAMPLES[ctx.key.task_id]["spec"]))
        value = generated[0]
        return {
            "usable": True,
            "generated_spec": value,
            "failure": None,
            "checkpoint_sha256": canonical_sha256(value),
            "duration_ms": 7,
            "usage": {"llm_calls": 3, "total_tokens": 21},
            "synthesis_id": "b" * 64,
            "artifact_dir": str(tmp_path),
        }, hit

    def phase(s, client, ctx, output, name, budget):
        phase_calls.append((ctx.key.arm, name, budget))
        if name == "phase-a":
            s.restore_generated_state(
                SAMPLES[ctx.key.task_id]["spec"]["target"]["state"]
            )
        else:
            s.actions.append({"policy": "pixel"})
        return SimpleNamespace(
            steps=2,
            actions=[
                {
                    "kind": "click",
                    "execution_receipt": {"accepted": True, "event_count": 1},
                }
            ],
            incomplete=False,
        )

    monkeypatch.setattr(instance, "_session", session)
    monkeypatch.setattr(instance, "_client", lambda _: object())
    monkeypatch.setattr(instance, "_phase", phase)
    monkeypatch.setattr(paid, "obtain_shared_synthesis", obtain)
    monkeypatch.setattr(paid, "hold_runtime_state_for_start_grading", hold)
    return instance, observations, generated, phase_calls


def gate(obs, ok=True):
    before = VisualJudgement(ok, file_sha256(obs.before_screenshot), "c" * 64)
    return LazarusVisualEvidence(
        before, VisualJudgement(None, None, None), VisualJudgement(None, None, None), {}
    )


@pytest.mark.parametrize("task", list(SAMPLES))
def test_all_11_explicit_specs_match_schema_and_independent_start(task):
    spec = SAMPLES[task]["spec"]
    assert spec_errors(spec, TASKS[task]["benchmark"]) == []
    observed = {
        "state": spec["target"]["state"],
        "visible": True,
        "dialog": "none",
        "runtime_healthy": True,
        "goal_dialog_open": False,
        "visible_worlds": [{"name": "p2p_world", "mods": ["p2p_world_mod"]}],
        "widgets": [{"id": "TILE_WIDGETS", "visible": False}],
    }
    assert validate_observed_start(TASKS[task], observed)


@pytest.mark.parametrize("task", list(SAMPLES))
def test_66_cells_use_explicit_states_and_shared_actions(tmp_path, monkeypatch, task):
    r, sessions, generated, phases = runner(tmp_path, monkeypatch)
    for version in ("buggy", "fixed"):
        for arm in (C, D, E):
            obs = r.run_cell(context(tmp_path, arm, version, task), start_gate=gate)
            assert obs.before_semantic_ok is True
            assert obs.after_screenshot.is_file()
            if arm == E:
                assert sessions[-1].actions == SAMPLES[task]["spec"]["actions"]
            if arm == D:
                assert sessions[-1].actions == [{"policy": "pixel"}]
    assert len(generated) == 1
    assert all(phase != "phase-a" for arm, phase, budget in phases if arm == D)


def test_shared_four_cells_generate_once_and_fixed_has_zero_calls(
    tmp_path, monkeypatch
):
    r, sessions, generated, phases = runner(tmp_path, monkeypatch)
    gates = []

    def counted(obs):
        gates.append(obs)
        return gate(obs)

    rows = [
        r.run_cell(context(tmp_path, a, v), start_gate=counted)
        for v in ("buggy", "fixed")
        for a in (D, E)
    ]
    assert len(generated) == 1 and len(gates) == 2
    assert len({row.checkpoint_sha256 for row in rows}) == 1
    assert [row.actual_synthesis_calls for row in rows] == [3, 0, 0, 0]
    assert rows[0].trace["costs"]["synthesis"]["usage"]["total_tokens"] == 21
    assert (
        rows[1].trace["costs"]["synthesis"]["attributed"]["usage"]["total_tokens"] == 21
    )


@pytest.mark.parametrize("kind", ["semantic", "visual"])
def test_start_failure_never_executes_phase_b(tmp_path, monkeypatch, kind):
    spec = copy.deepcopy(SAMPLES[TASK]["spec"])
    if kind == "semantic":
        spec["target"]["state"]["props"]["pressed"] = True
    r, sessions, _, phases = runner(tmp_path, monkeypatch, spec)
    obs = r.run_cell(
        context(tmp_path, D), start_gate=lambda o: gate(o, kind != "visual")
    )
    assert obs.after_screenshot is None and not sessions[0].actions and phases == []


def test_empty_generated_actions_are_not_replaced(tmp_path, monkeypatch):
    spec = copy.deepcopy(SAMPLES[TASK]["spec"])
    spec["actions"] = []
    r, sessions, _, _ = runner(tmp_path, monkeypatch, spec)
    from warptest import lazarus_p2p_only_runtime as legacy

    def forbidden(*a, **kw):
        raise AssertionError("registered task implementation executed")

    for name in (
        "_synthesize_registered_transition",
        "_apply_luanti_registered_transition",
        "_apply_openra_registered_transition",
        "_restore_openra_start",
    ):
        monkeypatch.setattr(legacy, name, forbidden)
    obs = r.run_cell(context(tmp_path, E), start_gate=gate)
    assert sessions[0].actions == [] and obs.trace["phase_b"]["steps"] == 0
    assert obs.invariants_ok is False and obs.trace["phase_b"]["executed"] is False


def test_changed_generated_actions_change_executed_trace(tmp_path, monkeypatch):
    spec = copy.deepcopy(SAMPLES[TASK]["spec"])
    spec["actions"] = [{"type": "click", "selector": "#different-button"}]
    r, sessions, _, _ = runner(tmp_path, monkeypatch, spec)
    r.run_cell(context(tmp_path, E), start_gate=gate)
    assert sessions[0].actions == spec["actions"]


def test_local_restore_drift_is_incomplete(tmp_path, monkeypatch):
    r, _, generated, _ = runner(tmp_path, monkeypatch)
    r.run_cell(context(tmp_path, D), start_gate=gate)
    generated[0]["target"]["state"]["props"]["pressed"] = True
    with pytest.raises(RuntimeError, match="drift"):
        r.run_cell(context(tmp_path, E), start_gate=gate)


def test_runtime_refuses_missing_gate(tmp_path):
    with pytest.raises(RuntimeError, match="start_gate"):
        paid.P2POnlyPaidRuntimeRunner(workspace=ROOT, runtime_root=tmp_path)(None)


def test_method_identity_includes_all_method_fields():
    config = method_fields()
    before = schedule_sha256(config)
    config["methods"][D]["temperature"] = 0.8
    assert schedule_sha256(config) != before
    with pytest.raises(ValueError, match="methods"):
        paid.P2POnlyPaidRuntimeRunner(
            workspace=ROOT, runtime_root=ROOT, method_config=config
        )


def test_disk_cache_restart_corruption_and_fixed_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_BASE_URL", "http://localhost:1/v1")
    ctx = context(tmp_path)
    calls = []

    class Agent:
        def synthesize(self, issue, root, artifact_dir):
            calls.append(issue)
            assert (
                "oracle_spec" not in issue.public_payload()
                and root == ctx.agent_source.root
            )
            spec = copy.deepcopy(SAMPLES[TASK]["spec"])
            return MethodCheckpoint(
                issue.id,
                issue.benchmark,
                spec,
                [{"source_path": "component.ts"}],
                {"kind": "state_injection"},
                "injection-only",
                {"status": "not_run"},
                [],
                model_generated_spec=spec,
            )

    monkeypatch.setattr(synthesis, "build_synthesis_agent", lambda *a, **kw: Agent())
    monkeypatch.setattr(
        synthesis,
        "resolve_model",
        lambda *a, **kw: (
            "test",
            SimpleNamespace(
                base_url="http://localhost:1", api_key_env="TEST_KEY", extra_headers={}
            ),
        ),
    )
    a, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "cache")
    assert not hit and a["usable"]
    ctx.key = replace(ctx.key, arm=D, version="fixed")
    b, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "cache")
    assert hit and a == b and len(calls) == 1
    with pytest.raises(ValueError, match="missing buggy"):
        synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "absent")
    ctx.key = replace(ctx.key, version="buggy", seed=1)
    c, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "cache")
    assert not hit and c["synthesis_id"] != a["synthesis_id"] and len(calls) == 2
    ctx.key = replace(ctx.key, seed=2)
    d, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "cache")
    assert not hit and d["synthesis_id"] not in {a["synthesis_id"], c["synthesis_id"]}
    assert len(calls) == 3
    ctx.key = replace(ctx.key, seed=0, version="fixed")
    entry = tmp_path / "cache" / a["synthesis_id"] / "checkpoint.json"
    data = json.loads(entry.read_text())
    data["generated_spec"]["actions"] = []
    entry.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        synthesis.obtain_shared_synthesis(ctx, cache_root=tmp_path / "cache")


def test_full_exploration_finalization_accepts_generated_schema(tmp_path):
    from warptest.synthesis import (
        ExploratoryLLMCheckpointSynthesisAgent,
        ModelCallResult,
    )

    ctx = context(tmp_path)
    spec = copy.deepcopy(SAMPLES[TASK]["spec"])
    payload = {
        "task_id": TASK,
        "benchmark": "jupyterlab",
        "generated_spec": spec,
        "source_evidence": [
            {
                "source_path": "component.ts",
                "matched_symbol": "component",
                "rationale": "candidate API",
            }
        ],
        "payload": {"kind": "state_injection"},
        "validity_class": "injection-only",
        "runtime_validation": {"status": "not_run", "detail": "await runtime"},
    }
    replies = iter(
        [
            'ACTION: read_file\n{"path":"component.ts"}',
            "FINAL\n```yaml\n" + yaml.safe_dump(payload) + "```",
        ]
    )

    class Client:
        def converse(self, model, messages, **kwargs):
            return ModelCallResult(next(replies), 0)

    agent = ExploratoryLLMCheckpointSynthesisAgent(
        model="test", client=Client(), max_steps=16, max_tokens=8192
    )
    result = agent.synthesize(
        synthesis.public_issue(ctx),
        ctx.agent_source.root,
        artifact_dir=tmp_path / "artifacts",
    )
    assert result.model_generated_spec == spec and not result.used_public_scaffold
    prompt = (tmp_path / "artifacts/prompt.txt").read_text()
    assert (
        "p2p_only_typed_restore_v3" in prompt and "restore_public_start" not in prompt
    )
    assert "SECRET_ORACLE" not in prompt


def test_p2p_restorer_binds_actual_snapshot_and_rejects_receipt_drift(
    tmp_path, monkeypatch
):
    r, sessions, _, _ = runner(tmp_path, monkeypatch)
    obs = r.run_cell(context(tmp_path, C), start_gate=gate)
    data = {
        "checkpoint_sha256": obs.checkpoint_sha256,
        "checkpoint_kind": "agent_s_from_start",
        "runtime_restored": True,
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(data))
    data["payload_sha256"] = file_sha256(receipt)
    report = obs.p2p_restore_adapter(tmp_path / "clone", receipt, data)
    assert report["state_restore_applied"] is True
    assert report["start_state_sha256"] == report["restored_state_sha256"]
    data["checkpoint_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="drift"):
        obs.p2p_restore_adapter(tmp_path / "clone", receipt, data)


def test_c_and_d_share_policy_config_and_phase_b_prompt():
    from warptest.lazarus_p2p_only_contract import METHODS

    for field in (
        "policy_kind",
        "temperature",
        "observer_model",
        "grounder_model",
        "phase_b_prompt_revision",
        "execution_backend",
        "foreground_required",
        "coordinate_space",
    ):
        assert METHODS[C][field] == METHODS[D][field]


def test_c_phase_a_spends_shared_budget_but_d_gets_full_phase_b(tmp_path, monkeypatch):
    r, _, _, phases = runner(tmp_path, monkeypatch)
    r.run_cell(context(tmp_path, C), start_gate=gate)
    r.run_cell(context(tmp_path, D), start_gate=gate)
    assert (C, "phase-b", 98) in phases and (D, "phase-b", 100) in phases


def test_shared_cost_total_counts_generation_once(tmp_path, monkeypatch):
    from warptest.lazarus_p2p_only_experiment import summarize_costs

    r, _, _, _ = runner(tmp_path, monkeypatch)
    observations = [
        r.run_cell(context(tmp_path, a, v), start_gate=gate)
        for v in ("buggy", "fixed")
        for a in (D, E)
    ]
    rows = [{"runtime": {"trace": o.trace}} for o in observations]
    summary = summarize_costs(rows, attributed=False)
    assert (
        summary["synthesis_events"] == 1
        and summary["phases"]["synthesis"]["total_tokens"] == 21
    )
    for indexes in ((0, 2), (1, 3)):
        assert (
            summarize_costs([rows[i] for i in indexes], attributed=True)["phases"][
                "synthesis"
            ]["total_tokens"]
            == 21
        )


def test_paid_cli_refuses_before_creating_run_or_reading_credentials(tmp_path):
    import subprocess
    import sys

    from warptest.lazarus_p2p_only_contract import AUTHORIZATION_ID
    manifest = yaml.safe_load((ROOT / "benchmarks/p5/lazarus_p2p_only_manifest.yaml").read_text())
    manifest["authorization"][AUTHORIZATION_ID] = False
    manifest_path = tmp_path / "benchmarks/p5/lazarus_p2p_only_manifest.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(yaml.safe_dump(manifest))
    output = tmp_path / "should-not-create-v3-unapproved"
    assert not output.exists()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_lazarus_p2p_only.py"),
            "--manifest",
            str(manifest_path),
            "--workspace",
            str(tmp_path),
            "--run-root",
            str(output),
            "--credentials",
            str(tmp_path / "must-not-read.py"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "not authorized" in result.stderr
    assert not output.exists()


def test_legacy_lz_trial_keys_do_not_accept_d():
    with pytest.raises(ValueError, match="requires an lzp-\* task id"):
        LazarusTrialKey(D, "test", 0, "lz-example", "buggy")
    with pytest.raises(ValueError, match="requires an lzp-\* task id"):
        LazarusTrialKey("lazarus_scalecua", "test", 0, "lz-example", "buggy")


def test_synthesis_failure_retains_cost_and_skips_restore(tmp_path, monkeypatch):
    r, sessions, _, phases = runner(tmp_path, monkeypatch)

    def failed(ctx):
        return {
            "usable": False,
            "generated_spec": None,
            "failure": "invalid_generated_spec",
            "checkpoint_sha256": "a" * 64,
            "duration_ms": 12,
            "usage": {"llm_calls": 2, "total_tokens": 18},
            "synthesis_id": "b" * 64,
            "artifact_dir": str(tmp_path),
        }, False

    monkeypatch.setattr(r, "_synthesis", failed)
    obs = r.run_cell(context(tmp_path, D), start_gate=lambda o: gate(o, False))
    assert obs.before_semantic_ok is False and obs.treatment_tokens == 18
    assert sessions[0].state is None and phases == [] and obs.after_screenshot is None


def test_restore_attestation_cannot_accept_an_ignored_parameter(tmp_path, monkeypatch):
    r, _, _, _ = runner(tmp_path, monkeypatch)

    def ignore(self, state):
        self.state = {"component": "wrong"}

    monkeypatch.setattr(Session, "restore_generated_state", ignore)
    with pytest.raises(RuntimeError, match="attestation drift"):
        r.run_cell(context(tmp_path, E), start_gate=gate)


def test_canary_rejects_relabeling_legacy_evidence(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_lazarus_p2p_only_runtime_canary.py"),
            "--adopt-current-complete",
            "--run-root",
            str(tmp_path / "unused"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "fresh execution" in result.stderr
    assert not (tmp_path / "unused").exists()


def test_same_state_different_png_metadata_does_not_change_shared_verdict(
    tmp_path, monkeypatch
):
    from PIL import Image, PngImagePlugin

    r, _, _, _ = runner(tmp_path, monkeypatch)
    first = r.run_cell(context(tmp_path, D), start_gate=gate)
    original = Session.capture_policy_frame

    def capture(self, path):
        result = original(self, path)
        with Image.open(result) as frame:
            meta = PngImagePlugin.PngInfo()
            meta.add_text("capture", "another session")
            frame.save(result, pnginfo=meta)
        return result

    monkeypatch.setattr(Session, "capture_policy_frame", capture)
    monkeypatch.setattr(Session, "capture_evidence_frame", capture)
    second = r.run_cell(
        context(tmp_path, E),
        start_gate=lambda _: pytest.fail("shared gate was repeated"),
    )
    assert file_sha256(first.before_screenshot) != file_sha256(second.before_screenshot)
    assert second.trace["start_gate"]["passed"] and second.trace["start_gate"]["shared"]


def test_start_predicates_do_not_require_unpublished_fixture_integer():
    task = "lzp-jupyterlab-18464-debugger-zero-variable-detail"
    state = copy.deepcopy(SAMPLES[task]["spec"]["target"]["state"])
    state["props"]["variables"][0]["value"] = "42"
    assert validate_observed_start(TASKS[task], {"state": state, "visible": True})


def test_shared_start_verdict_survives_runner_restart(tmp_path, monkeypatch):
    r, _, _, _ = runner(tmp_path, monkeypatch)
    r.run_cell(context(tmp_path, D), start_gate=gate)
    second, _, _, _ = runner(tmp_path, monkeypatch)
    result = second.run_cell(
        context(tmp_path, E),
        start_gate=lambda _: pytest.fail("immutable gate was resampled"),
    )
    assert result.trace["start_gate"]["shared"]


def test_candidate_asset_installation_outlives_first_workspace(tmp_path, monkeypatch):
    import shutil
    from warptest import lazarus_p2p_only_generated_runtime as generated
    from warptest.c1_lazarus_selection import p2p_only_engine_capture as engine

    source = tmp_path / "source"
    source.mkdir()
    (source / "program.js").write_text("candidate code")
    monkeypatch.setattr(engine, "_cow_copy", lambda a, b: shutil.copytree(a, b))

    def build(tree, output):
        (output / "dist").mkdir()
        (output / "dist/bundle.js").write_text("generic components")
        (output / "document.html").write_text("blank public workbench")

    monkeypatch.setattr(generated, "prepare_component_document", build)
    first = tmp_path / "first"
    first.mkdir()
    session = generated.GeneratedJupyterSession(
        tree=source, output_dir=first, task_id=TASK, document_cache=tmp_path / "assets"
    )
    candidate = session.select_runtime_tree(first / "candidate")
    session.prepare_document(candidate, first)
    shutil.rmtree(first)
    second = tmp_path / "second"
    second.mkdir()
    next_session = generated.GeneratedJupyterSession(
        tree=source, output_dir=second, task_id=TASK, document_cache=tmp_path / "assets"
    )
    assert next_session.select_runtime_tree(second / "candidate") == candidate
    next_session.prepare_document(candidate, second)
    assert (candidate / "program.js").read_text() == "candidate code"
    assert (second / "document.html").read_text() == "blank public workbench"


def test_done_without_input_is_not_a_transition(tmp_path, monkeypatch):
    r, _, _, _ = runner(tmp_path, monkeypatch)
    monkeypatch.setattr(
        r,
        "_phase",
        lambda *args: SimpleNamespace(
            steps=1, actions=[{"kind": "done"}], incomplete=False
        ),
    )
    result = r.run_cell(context(tmp_path, D), start_gate=gate)
    assert result.before_semantic_ok and result.after_screenshot.is_file()
    assert result.invariants_ok is False


def test_retryable_synthesis_archives_paid_attempt_before_recovery(tmp_path, monkeypatch):
    from warptest import cost
    from warptest.synthesis import SynthesisError
    monkeypatch.setenv('RELAY_BASE_URL', 'http://localhost:1/v1')
    ctx = context(tmp_path)
    calls = []
    class Agent:
        def synthesize(self, issue, root, artifact_dir):
            calls.append(issue)
            cost.record_call('test', prompt_tokens=2, completion_tokens=3, total_tokens=5)
            if len(calls) == 1:
                raise SynthesisError('gateway outage', artifacts={'failure_mode':'gateway_outage'})
            spec = copy.deepcopy(SAMPLES[TASK]['spec'])
            return MethodCheckpoint(issue.id, issue.benchmark, spec,
                [{'source_path':'component.ts'}], {'kind':'state_injection'},
                'injection-only', {'status':'not_run'}, [], model_generated_spec=spec)
    monkeypatch.setattr(synthesis, 'build_synthesis_agent', lambda *a, **kw: Agent())
    monkeypatch.setattr(synthesis, 'resolve_model', lambda *a, **kw: ('test', SimpleNamespace(base_url='http://localhost:1', api_key_env='TEST_KEY', extra_headers={})))
    cache = tmp_path/'cache'
    with pytest.raises(SynthesisError):
        synthesis.obtain_shared_synthesis(ctx, cache_root=cache)
    failed_path = next(cache.glob('*/failed-synthesis.json'))
    receipt = json.loads(failed_path.read_text())
    assert receipt['usage']['total_tokens'] == 5 and receipt['failure']['retryable']
    result, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=cache)
    assert not hit and result['usable'] and result['usage']['total_tokens'] == 5
    archived = list(cache.glob('*.failed-*/failed-synthesis.json'))
    assert len(archived) == 1 and json.loads(archived[0].read_text()) == receipt
    ctx.key = replace(ctx.key, arm=D, version='fixed')
    reused, hit = synthesis.obtain_shared_synthesis(ctx, cache_root=cache)
    assert hit and reused == result and len(calls) == 2


def test_luanti_refocuses_before_each_global_f12_capture(tmp_path, monkeypatch):
    from warptest.lazarus_p2p_only_runtime import LuantiOnlineSession
    from warptest.c1_lazarus_selection import p2p_only_engine_capture as engine
    from warptest import lazarus_p2p_only_runtime as runtime
    calls = []
    session = object.__new__(LuantiOnlineSession)
    session.process = SimpleNamespace(poll=lambda: None)
    session.native = SimpleNamespace(focus=lambda: calls.append("focus"), _press_key=lambda code: calls.append(("key", code)))
    session._screenshots = tmp_path / "screenshots"
    session.last_policy_frame_id = 0
    captured = tmp_path / "engine.png"
    captured.write_bytes(b"engine screenshot")
    def take(**kwargs):
        calls.append("F12")
        kwargs["request_key"]()
        return captured
    monkeypatch.setattr(engine, "_take_luanti_screenshot", take)
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    session.capture_policy_frame(tmp_path / "first.png")
    session.capture_policy_frame(tmp_path / "second.png")
    assert calls == ["F12", "focus", ("key", 111), "F12", "focus", ("key", 111)]
    assert session.last_policy_frame_id == 2


def test_runtime_records_normal_policy_exit_without_reusing_a_frame(tmp_path, monkeypatch):
    r, _, _, _ = runner(tmp_path, monkeypatch)
    ctx = context(tmp_path, arm=C, task="lzp-luanti-16799-world-mods")
    @contextmanager
    def session(*args, **kwargs):
        value = Session("luanti")
        value.exit_code = None
        value.process = SimpleNamespace(pid=123, poll=lambda: value.exit_code)
        yield value
    def phase(value, *args):
        value.exit_code = 0
        return SimpleNamespace(steps=1, incomplete=True, screenshot="diagnostic-only.png",
            actions=[{"kind":"key", "key":"escape", "modifiers":[], "step":1,
                "execution_receipt":{"accepted":True,"event_count":1}}])
    monkeypatch.setattr(r,"_session",session)
    monkeypatch.setattr(r,"_phase",phase)
    def negative_gate(obs):
        assert obs.before_screenshot is None
        return LazarusVisualEvidence(VisualJudgement(False,None,None),VisualJudgement(None,None,None),VisualJudgement(None,None,None),{})
    result = r.run_cell(ctx, start_gate=negative_gate)
    assert result.complete and result.before_semantic_ok is False
    assert result.before_screenshot is None and result.after_screenshot is None
    assert result.trace["policy_exit"]["exit_code"] == 0
    assert result.trace["phase_b"]["executed"] is False


def test_closed_jupyter_task_is_a_start_failure_with_current_policy_frame(tmp_path, monkeypatch):
    r, _, _, _ = runner(tmp_path, monkeypatch)
    class ClosedTask(Session, paid.GeneratedJupyterSession):
        closed = False
        def observe_start(self):
            if self.closed:
                raise RuntimeError("Jupyter benchmark extension task frame is not open")
            return super().observe_start()
        def capture_evidence_frame(self, path):
            raise RuntimeError("Jupyter benchmark extension task frame is not open")
    @contextmanager
    def session(*args, **kwargs):
        yield ClosedTask("jupyterlab")
    def phase(value, *args):
        value.closed = True
        return SimpleNamespace(steps=1, incomplete=False, actions=[{"kind": "click"}])
    monkeypatch.setattr(r, "_session", session)
    monkeypatch.setattr(r, "_phase", phase)
    def gate(obs):
        assert obs.before_semantic_ok is False
        assert obs.before_screenshot.is_file()
        return LazarusVisualEvidence(VisualJudgement(False,None,None),VisualJudgement(None,None,None),VisualJudgement(None,None,None),{})
    result = r.run_cell(context(tmp_path, arm=C), start_gate=gate)
    assert result.complete and result.before_semantic_ok is False
    assert result.trace["phase_b"]["not_run_reason"] == "start_failed"
    assert result.p2p_restore_adapter is None


def test_unrelated_jupyter_runtime_error_remains_infrastructure_failure():
    value = object.__new__(paid.GeneratedJupyterSession)
    def broken():
        raise RuntimeError("browser transport disconnected")
    value.observe_start = broken
    with pytest.raises(RuntimeError, match="transport disconnected"):
        paid.P2POnlyPaidRuntimeRunner._observe(value)
