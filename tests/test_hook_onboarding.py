from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from warptest.hook_onboarding import (
    AppSpec,
    CaseSpec,
    CommandSpec,
    OnboardingConfigError,
    build_agent_prompts,
    discover_playwright_chromium,
    find_forbidden_result_keys,
    load_manifest,
    materialize_clean_source,
    projection_mismatches,
    run_driver_case,
    validate_contract,
    write_json,
)
from warptest.hook_onboarding_agent import (
    HOOK_AGENT_TOOLS,
    HookAgentWorkspace,
    HookCodingAgent,
    NamedCheck,
    UsageAccumulator,
    parse_tool_arguments,
    sanitized_subprocess_env,
)
from warptest.hook_onboarding_suites import (
    load_generated_self_tests,
    run_case_suite,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "hook-agent@example.invalid")
    _git(repo, "config", "user.name", "Hook Agent Test")
    (repo / "src").mkdir()
    (repo / "src" / "state.py").write_text(
        "STATE = {'score': 0}\n\ndef save_state():\n    return STATE\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "clean source")
    return repo


def _usage(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cached_tokens: int | None = 2,
    reasoning_tokens: int | None = 3,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=(
            SimpleNamespace(cached_tokens=cached_tokens)
            if cached_tokens is not None
            else None
        ),
        output_tokens_details=(
            SimpleNamespace(reasoning_tokens=reasoning_tokens)
            if reasoning_tokens is not None
            else None
        ),
    )


def _call(call_id: str, name: str, arguments: str | dict):
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=(
            json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, dict)
            else arguments
        ),
    )


def _response(
    response_id: str,
    *,
    calls: list | None = None,
    text: str = "",
    usage=None,
):
    return SimpleNamespace(
        id=response_id,
        status="completed",
        model="gpt-5.6-sol",
        output=list(calls or []),
        output_text=text,
        usage=usage or _usage(),
    )


class _FakeResponses:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected provider request")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = _FakeResponses(responses)


def test_exact_seven_function_tools_are_registered() -> None:
    assert [item["name"] for item in HOOK_AGENT_TOOLS] == [
        "list_dir",
        "grep",
        "read_file",
        "write_file",
        "apply_patch",
        "get_diff",
        "run_check",
    ]
    assert all(item["type"] == "function" for item in HOOK_AGENT_TOOLS)
    assert all(item["strict"] is True for item in HOOK_AGENT_TOOLS)


def test_workspace_read_write_patch_and_diff_tools(clean_repo: Path, tmp_path: Path) -> None:
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")

    assert "src" in workspace.dispatch("list_dir", {"path": "."})
    assert "src/state.py" in workspace.dispatch(
        "grep", {"pattern": "save_state", "glob": "*.py"}
    )
    assert "STATE" in workspace.dispatch(
        "read_file", {"path": "src/state.py", "start": 1, "end": 20}
    )

    written = workspace.dispatch(
        "write_file",
        {
            "path": "warptest_onboarding/driver.py",
            "content": "print('driver')\n",
        },
    )
    assert written.startswith("WROTE warptest_onboarding/driver.py")
    assert (clean_repo / "warptest_onboarding" / "driver.py").is_file()

    patch = """diff --git a/src/state.py b/src/state.py
--- a/src/state.py
+++ b/src/state.py
@@ -1,4 +1,4 @@
-STATE = {'score': 0}
+STATE = {'score': 7}
 
 def save_state():
     return STATE
"""
    assert workspace.dispatch("apply_patch", {"patch": patch}) == "PATCH APPLIED"
    assert "score': 7" in (clean_repo / "src" / "state.py").read_text(encoding="utf-8")

    diff = workspace.dispatch("get_diff", {"path": "."})
    assert "src/state.py" in diff
    assert "warptest_onboarding/driver.py" in diff


def test_named_check_pass_is_invalidated_by_later_source_edit(
    clean_repo: Path, tmp_path: Path
) -> None:
    workspace = HookAgentWorkspace(
        clean_repo,
        tmp_path / "reports",
        checks={
            "self_tests": NamedCheck(
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_s=30,
            )
        },
    )
    assert workspace.check_is_current("self_tests") is False
    assert "exit=0" in workspace.dispatch("run_check", {"name": "self_tests"})
    assert workspace.check_is_current("self_tests") is True
    workspace.dispatch(
        "write_file",
        {"path": "warptest_onboarding/driver.py", "content": "print('changed')\n"},
    )
    assert workspace.check_is_current("self_tests") is False
    assert workspace.check_attempts == {"self_tests": 1}


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": "../secret", "start": 1, "end": 2}),
        ("write_file", {"path": "/tmp/escape", "content": "x"}),
        ("write_file", {"path": ".git/config", "content": "x"}),
        ("get_diff", {"path": "../"}),
    ],
)
def test_workspace_rejects_path_escape(
    clean_repo: Path, tmp_path: Path, tool: str, args: dict
) -> None:
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")
    assert workspace.dispatch(tool, args).startswith("ERROR:")


def test_workspace_rejects_symlink_escape(clean_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (clean_repo / "linked").symlink_to(outside, target_is_directory=True)
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")
    result = workspace.dispatch(
        "write_file", {"path": "linked/escape.txt", "content": "no"}
    )
    assert result.startswith("ERROR:")
    assert not (outside / "escape.txt").exists()


def test_apply_patch_rejects_unsafe_path(clean_repo: Path, tmp_path: Path) -> None:
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")
    patch = """diff --git a/../escape.txt b/../escape.txt
--- a/../escape.txt
+++ b/../escape.txt
@@ -0,0 +1 @@
+no
"""
    result = workspace.dispatch("apply_patch", {"patch": patch})
    assert result.startswith("ERROR:")
    assert not (tmp_path / "escape.txt").exists()


def test_run_check_uses_registered_argv_and_strips_secrets(
    clean_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = clean_repo / "check_env.py"
    script.write_text(
        """import os
assert 'RELAY_API_KEY' not in os.environ
assert 'EXTRA_TOKEN' not in os.environ
assert os.environ['VISIBLE_SETTING'] == 'yes'
print('check-ok')
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("RELAY_API_KEY", "never-show-this-key")
    monkeypatch.setenv("EXTRA_TOKEN", "never-show-this-token")
    workspace = HookAgentWorkspace(
        clean_repo,
        tmp_path / "reports",
        checks={
            "build": NamedCheck(
                argv=(sys.executable, "check_env.py"),
                env={
                    "VISIBLE_SETTING": "yes",
                    "IGNORED_API_KEY": "also-secret",
                },
            )
        },
    )
    output = workspace.dispatch("run_check", {"name": "build"})
    assert "exit=0" in output
    assert "check-ok" in output
    assert "never-show-this" not in output
    assert workspace.dispatch("run_check", {"name": "anything"}).startswith("ERROR:")


def test_sanitized_subprocess_env_removes_secret_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMAL_VALUE", "kept")
    monkeypatch.setenv("MY_PASSWORD", "removed")
    env = sanitized_subprocess_env(
        {"VISIBLE": "yes", "ANOTHER_SECRET": "removed-too"}
    )
    assert env["NORMAL_VALUE"] == "kept"
    assert env["VISIBLE"] == "yes"
    assert "MY_PASSWORD" not in env
    assert "ANOTHER_SECRET" not in env


def test_responses_agent_executes_multiple_tools_and_continues(
    clean_repo: Path, tmp_path: Path
) -> None:
    first = _response(
        "resp-1",
        calls=[
            _call("call-1", "list_dir", {"path": "."}),
            _call(
                "call-2",
                "write_file",
                {
                    "path": "warptest_onboarding/contract.yaml",
                    "content": "schema_version: 1\n",
                },
            ),
        ],
        usage=_usage(20, 8, 4, 5),
    )
    second = _response(
        "resp-2",
        text="Implemented the hook contract.",
        usage=_usage(30, 10, 6, 7),
    )
    client = _FakeClient([first, second])
    transcript = tmp_path / "reports" / "public-transcript.jsonl"
    agent = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
    )

    result = agent.run(
        system_prompt="system",
        user_prompt="user",
        transcript_path=transcript,
    )

    assert result.status == "success"
    assert result.rounds == 2
    assert result.tool_calls == 2
    assert result.tool_counts == {"list_dir": 1, "write_file": 1}
    assert result.final_text == "Implemented the hook contract."
    assert result.usage == {
        "input_tokens": 50,
        "output_tokens": 18,
        "total_tokens": 68,
        "cached_input_tokens": 10,
        "reasoning_tokens": 12,
        "responses": 2,
    }
    calls = client.responses.calls
    assert calls[0]["model"] == "gpt-5.6-sol"
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert calls[0]["max_output_tokens"] == 16_384
    assert "previous_response_id" not in calls[0]
    assert calls[1]["previous_response_id"] == "resp-1"
    assert [item["call_id"] for item in calls[1]["input"]] == ["call-1", "call-2"]
    assert all(item["type"] == "function_call_output" for item in calls[1]["input"])
    transcript_text = transcript.read_text(encoding="utf-8")
    assert "list_dir" in transcript_text
    assert "Implemented the hook contract" in transcript_text


def test_agent_returns_tool_argument_error_as_observation(
    clean_repo: Path, tmp_path: Path
) -> None:
    first = _response(
        "resp-1",
        calls=[_call("call-1", "read_file", "{not-json")],
    )
    second = _response("resp-2", text="Recovered.")
    client = _FakeClient([first, second])
    agent = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
    )
    result = agent.run(system_prompt="s", user_prompt="u")
    assert result.status == "success"
    assert "invalid tool JSON" in client.responses.calls[1]["input"][0]["output"]


def test_agent_reports_empty_provider_response(clean_repo: Path, tmp_path: Path) -> None:
    client = _FakeClient([_response("resp-empty")])
    result = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
    ).run(system_prompt="s", user_prompt="u")
    assert result.status == "empty_agent_response"
    assert result.error is not None


def test_agent_reports_provider_failure_without_leaking_secret(
    clean_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "provider-secret-value"
    monkeypatch.setenv("RELAY_API_KEY", secret)
    client = _FakeClient([RuntimeError(f"request with {secret} failed")])
    result = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
    ).run(system_prompt="s", user_prompt="u")
    assert result.status == "provider_failure"
    assert result.error is not None
    assert secret not in result.error
    assert "<redacted>" in result.error


def test_agent_stops_before_exceeding_tool_call_limit(
    clean_repo: Path, tmp_path: Path
) -> None:
    client = _FakeClient(
        [
            _response(
                "resp-1",
                calls=[
                    _call("call-1", "list_dir", {"path": "."}),
                    _call("call-2", "list_dir", {"path": "src"}),
                ],
            )
        ]
    )
    result = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
        max_tool_calls=1,
    ).run(system_prompt="s", user_prompt="u")
    assert result.status == "tool_call_limit"
    assert result.tool_calls == 0


def test_agent_stops_at_round_limit(clean_repo: Path, tmp_path: Path) -> None:
    client = _FakeClient(
        [_response("resp-1", calls=[_call("call-1", "list_dir", {"path": "."})])]
    )
    result = HookCodingAgent(
        client,
        HookAgentWorkspace(clean_repo, tmp_path / "reports"),
        max_rounds=1,
    ).run(system_prompt="s", user_prompt="u")
    assert result.status == "round_limit"
    assert result.rounds == 1
    assert result.tool_calls == 1


def test_usage_accumulator_marks_omitted_fields_unknown() -> None:
    accumulator = UsageAccumulator()
    accumulator.add(_usage(10, 5, cached_tokens=None, reasoning_tokens=None))
    assert accumulator.to_dict() == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "responses": 1,
    }


@pytest.mark.parametrize(
    "raw,expected_error",
    [
        ('{"path":"."}', None),
        ("[]", "tool arguments must decode to an object"),
        ("{", "invalid tool JSON"),
        (None, "tool arguments must be a JSON object"),
    ],
)
def test_parse_tool_arguments(raw, expected_error: str | None) -> None:
    value, error = parse_tool_arguments(raw)
    if expected_error is None:
        assert value == {"path": "."}
        assert error is None
    else:
        assert value == {}
        assert expected_error in str(error)


def _case(case_id: str, value: int) -> dict:
    return {
        "id": case_id,
        "target": {"value": value},
        "roundtrip": "save_reload",
        "expected": {"value": value},
        "timeout_s": 30,
    }


def _app_mapping(index: int) -> dict:
    return {
        "id": f"app-{index}",
        "family": "test",
        "remote": f"https://example.invalid/app-{index}.git",
        "clean_commit": f"{index + 1:040x}",
        "capability_brief": "Write, save, reload, and read one value.",
        "runtime_notes": "test runtime",
        "prepare": [],
        "checks": {},
        "development_smoke": _case("development", index),
        "held_out": [_case("held-a", index + 10), _case("held-b", index + 20)],
        "reference": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]},
        "driver_env": {},
    }


def _write_manifest(root: Path, apps: list[dict]) -> Path:
    prompt = root / "prompt.md"
    prompt.write_text("You are HookCodingAgent.\n", encoding="utf-8")
    path = root / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "pilot_id": "pilot-v1",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
                "prompt": "prompt.md",
                "apps": apps,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_loads_ten_apps_and_two_held_out_cases(tmp_path: Path) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, [_app_mapping(i) for i in range(10)]))
    assert manifest.pilot_id == "pilot-v1"
    assert manifest.model == "gpt-5.6-sol"
    assert manifest.reasoning_effort == "medium"
    assert len(manifest.apps) == 10
    assert all(len(app.held_out) == 2 for app in manifest.apps)


def test_hook_overlay_preserves_base_and_adds_visible_suites() -> None:
    base = load_manifest("benchmarks/onboarding/manifest.yaml")
    hook = load_manifest("benchmarks/onboarding/manifest_hook.yaml")

    assert base.pilot_id == "pilot-v1"
    assert base.require_agent_self_tests is False
    assert all(len(app.development_cases) == 1 for app in base.apps)
    assert hook.pilot_id == "hook-onboarding-uniform"
    assert hook.model == "gpt-5.6-sol"
    assert hook.reasoning_effort == "medium"
    assert hook.require_agent_self_tests is True
    assert hook.self_test_min_cases == 5
    assert hook.self_test_max_cases == 10
    assert hook.agent_max_rounds == 120
    assert hook.agent_max_tool_calls == 280
    assert hook.agent_wall_time_s == 10_800
    assert all(len(app.development_cases) == 3 for app in hook.apps)
    assert [app.id for app in hook.apps] == [app.id for app in base.apps]
    for old, new in zip(base.apps, hook.apps, strict=True):
        assert new.clean_commit == old.clean_commit
        assert new.held_out == old.held_out
    libreoffice = next(app for app in hook.apps if app.id == "libreoffice")
    assert set(libreoffice.driver_env) == {
        "WARPTEST_ONBOARDING_SOFFICE",
        "WARPTEST_ONBOARDING_LIBREOFFICE_JAR",
    }


def test_hook_manifest_freezes_protocol_and_runtime_preparation() -> None:
    manifest = load_manifest("benchmarks/onboarding/manifest_hook.yaml")
    assert manifest.prompt_path.name == "prompt_hook.md"
    assert "--request <request.json> --report <report.json>" in manifest.prompt_path.read_text()
    assert len(manifest.apps[-1].prepare) == 1
    assert "prepare_hook_onboarding_luanti.py" in manifest.apps[-1].prepare[0].argv[1]


def test_onboarding_build_does_not_write_pycache_into_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    driver = repo / "warptest_onboarding" / "driver.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("VALUE = 1\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/run_hook_onboarding_build.py").resolve()),
            "--app",
            "jupyterlab",
            "--repo",
            str(repo),
            "--report-dir",
            str(report_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert not list(repo.rglob("*.pyc"))
    assert not list(repo.rglob("__pycache__"))


def test_playwright_browser_discovery_is_read_only() -> None:
    value = discover_playwright_chromium()
    assert value is None or Path(value).is_file()


def test_hook_visible_suites_cover_held_out_top_level_schema_without_duplicate_cases() -> None:
    manifest = load_manifest("benchmarks/onboarding/manifest_hook.yaml")
    for app in manifest.apps:
        visible_keys = {
            str(key)
            for case in app.development_cases
            for key in case.target
        }
        hidden_keys = {str(key) for case in app.held_out for key in case.target}
        assert hidden_keys <= visible_keys, app.id
        visible_identities = {
            json.dumps(
                {"target": case.target, "roundtrip": case.roundtrip},
                sort_keys=True,
            )
            for case in app.development_cases
        }
        hidden_identities = {
            json.dumps(
                {"target": case.target, "roundtrip": case.roundtrip},
                sort_keys=True,
            )
            for case in app.held_out
        }
        assert not visible_identities & hidden_identities, app.id
        assert {case.roundtrip for case in app.development_cases} == {"live", "save_reload"}


def test_overlay_manifest_requires_every_app_override(tmp_path: Path) -> None:
    base = _write_manifest(tmp_path, [_app_mapping(i) for i in range(10)])
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base_manifest": base.name,
                "app_overrides": {"app-0": {"development_suite": [_case("a", 1)] * 3}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OnboardingConfigError, match="override every app"):
        load_manifest(overlay)


def test_manifest_rejects_build_artifact_app(tmp_path: Path) -> None:
    apps = [_app_mapping(i) for i in range(10)]
    apps[-1]["id"] = "build_darwin_lite"
    with pytest.raises(OnboardingConfigError, match="build artifact"):
        load_manifest(_write_manifest(tmp_path, apps))


def test_manifest_rejects_non_https_remote_and_short_commit(tmp_path: Path) -> None:
    apps = [_app_mapping(i) for i in range(10)]
    apps[0]["remote"] = "git@example.invalid:repo.git"
    with pytest.raises(OnboardingConfigError, match="HTTPS"):
        load_manifest(_write_manifest(tmp_path, apps))
    apps[0]["remote"] = "https://example.invalid/repo.git"
    apps[0]["clean_commit"] = "abc"
    with pytest.raises(OnboardingConfigError, match="40-character"):
        load_manifest(_write_manifest(tmp_path, apps))


def test_case_id_rejects_path_traversal() -> None:
    with pytest.raises(OnboardingConfigError, match="only letters"):
        CaseSpec.from_mapping(
            {
                "id": "../escape",
                "target": {"value": 1},
                "roundtrip": "live",
                "expected": {"value": 1},
            },
            label="case",
        )


def test_materialize_exact_local_commit_and_remove_remote(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source")
    (source / "README.md").write_text("clean\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "clean")
    commit = _git(source, "rev-parse", "HEAD").strip()
    case = CaseSpec("case", {"value": 1}, "live", {"value": 1})
    app = AppSpec(
        id="local",
        family="test",
        remote=str(source),
        clean_commit=commit,
        capability_brief="test",
        runtime_notes="",
        prepare=(),
        checks={},
        development_smoke=case,
        held_out=(case, case),
        reference=None,
        driver_env={},
    )
    destination = tmp_path / "materialized"
    repo = materialize_clean_source(app, destination, tmp_path / "source.log")
    assert _git(repo, "rev-parse", "HEAD").strip() == commit
    assert _git(repo, "remote").strip() == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "clean\n"


def test_build_agent_prompt_includes_smoke_but_not_held_out(
    clean_repo: Path, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system prompt\n", encoding="utf-8")
    case = CaseSpec("development", {"visible": 1}, "live", {"visible": 1})
    held_a = CaseSpec("secret-a", {"hidden": 11}, "save_reload", {"hidden": 11})
    held_b = CaseSpec("secret-b", {"hidden": 22}, "save_reload", {"hidden": 22})
    app = AppSpec(
        id="app",
        family="web",
        remote="https://example.invalid/app.git",
        clean_commit="1" * 40,
        capability_brief="state binding",
        runtime_notes="runtime",
        prepare=(),
        checks={},
        development_smoke=case,
        held_out=(held_a, held_b),
        reference=None,
        driver_env={},
    )
    manifest = SimpleNamespace(prompt_path=prompt, pilot_id="pilot-v1")
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")
    system, user = build_agent_prompts(manifest, app, workspace)
    assert system == "system prompt\n"
    assert "visible: 1" in user
    assert "secret-a" not in user
    assert "hidden: 11" not in user
    assert "hidden: 22" not in user


def test_hook_prompt_includes_self_tests_and_full_visible_suite_but_not_held_out(
    clean_repo: Path, tmp_path: Path
) -> None:
    manifest = load_manifest("benchmarks/onboarding/manifest_hook.yaml")
    app = manifest.apps[0]
    workspace = HookAgentWorkspace(clean_repo, tmp_path / "reports")
    system, user = build_agent_prompts(manifest, app, workspace)

    assert "warptest_onboarding/self_tests/cases.yaml" in system
    assert "self_tests" in user
    assert "development_suite" in user
    assert "development-live-midgame" in user
    assert "development-reload-varied" in user
    assert "heldout-a" not in user
    assert "1024" not in user


def _write_generated_self_tests(repo: Path, *, duplicate_development: bool = False) -> None:
    root = repo / "warptest_onboarding" / "self_tests"
    root.mkdir(parents=True, exist_ok=True)
    cases = []
    for index in range(5):
        value = 1 if duplicate_development and index == 0 else index + 10
        cases.append(
            {
                "id": f"self-case-{index}",
                "rationale": f"probe value {value}",
                "coverage": ["value", "reload" if index else "live"],
                "roundtrip": "live" if index == 0 else "save_reload",
                "timeout_s": 30,
                "target": {"value": value},
                "expected": {"value": value},
            }
        )
    (root / "cases.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "app_id": "sample", "cases": cases}, sort_keys=False),
        encoding="utf-8",
    )


def test_generated_self_tests_validate_diversity_and_metadata(clean_repo: Path) -> None:
    _write_generated_self_tests(clean_repo)
    development = [CaseSpec("development", {"value": 1}, "live", {"value": 1})]
    cases, metadata = load_generated_self_tests(
        clean_repo,
        expected_app_id="sample",
        development_cases=development,
        min_cases=5,
        max_cases=10,
    )

    assert len(cases) == 5
    assert metadata["case_count"] == 5
    assert metadata["roundtrips"] == ["live", "save_reload"]
    assert "value" in metadata["declared_coverage"]


def test_generated_self_tests_reject_development_duplicate(clean_repo: Path) -> None:
    _write_generated_self_tests(clean_repo, duplicate_development=True)
    development = [CaseSpec("development", {"value": 1}, "live", {"value": 1})]
    with pytest.raises(OnboardingConfigError, match="must not duplicate"):
        load_generated_self_tests(
            clean_repo,
            expected_app_id="sample",
            development_cases=development,
            min_cases=5,
            max_cases=10,
        )


def test_case_suite_runs_driver_and_preserves_source(clean_repo: Path, tmp_path: Path) -> None:
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    (onboarding / "driver.py").write_text(
        """import argparse, json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--report');a=p.parse_args()
r=json.loads(Path(a.request).read_text()); artifacts=[]
if r['roundtrip']=='save_reload':
 f=Path(r['artifact_dir'])/'save.json';f.write_text(json.dumps(r['target']));artifacts=[str(f)]
Path(a.report).write_text(json.dumps({'status':'success','observed':r['target'],'app_started':True,'roundtrip':r['roundtrip'],'gate_enabled':True,'artifacts':artifacts,'detail':''}))
""",
        encoding="utf-8",
    )
    cases = [
        CaseSpec("self-live", {"value": 7}, "live", {"value": 7}, timeout_s=30),
        CaseSpec("self-reload", {"value": 8}, "save_reload", {"value": 8}, timeout_s=30),
    ]
    result = run_case_suite(clean_repo, cases, tmp_path / "suite")

    assert result["status"] == "pass"
    assert result["passes"] == 2
    assert result["source_mutation"] is False


def test_validate_contract_and_driver(clean_repo: Path) -> None:
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    (onboarding / "driver.py").write_text("print('ok')\n", encoding="utf-8")
    (onboarding / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "app_id": "sample",
                "hook_kind": "existing_api",
                "gate": {"mode": "public", "normal_mode_absent": None},
                "driver": "warptest_onboarding/driver.py",
                "capabilities": ["write", "read", "save_reload"],
                "product_files_changed": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    contract, errors = validate_contract(clean_repo, "sample")
    assert errors == []
    assert contract is not None
    assert contract["hook_kind"] == "existing_api"


def test_validate_contract_rejects_forbidden_hash_key(clean_repo: Path) -> None:
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    (onboarding / "driver.py").write_text("print('ok')\n", encoding="utf-8")
    (onboarding / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "app_id": "sample",
                "hook_kind": "existing_api",
                "gate": {"mode": "public", "normal_mode_absent": None},
                "driver": "warptest_onboarding/driver.py",
                "capabilities": ["write", "read", "save_reload"],
                "product_files_changed": [],
                "manifest_hash": "not-allowed",
            }
        ),
        encoding="utf-8",
    )
    _, errors = validate_contract(clean_repo, "sample")
    assert any("forbidden" in error for error in errors)


def test_driver_case_validates_projection_and_save_reload_artifact(
    clean_repo: Path, tmp_path: Path
) -> None:
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    driver = onboarding / "driver.py"
    driver.write_text(
        """from __future__ import annotations
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--request'); p.add_argument('--report'); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
artifact=Path(req['artifact_dir'])/'native-save.json'; artifact.write_text(json.dumps(req['target']))
Path(a.report).write_text(json.dumps({'status':'success','observed':req['target'],'app_started':True,'roundtrip':req['roundtrip'],'gate_enabled':True,'artifacts':[str(artifact)],'detail':'reloaded'}))
""",
        encoding="utf-8",
    )
    case = CaseSpec(
        "held-a",
        {"state": {"score": 7}},
        "save_reload",
        {"state": {"score": 7}},
        timeout_s=30,
    )
    result = run_driver_case(clean_repo, case, tmp_path / "reports")
    assert result["status"] == "success"
    assert result["errors"] == []


def test_driver_case_rejects_projection_mismatch_and_missing_artifact(
    clean_repo: Path, tmp_path: Path
) -> None:
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    (onboarding / "driver.py").write_text(
        """import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--request'); p.add_argument('--report'); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
Path(a.report).write_text(json.dumps({'status':'success','observed':{'value':0},'app_started':True,'roundtrip':req['roundtrip'],'gate_enabled':True,'artifacts':[],'detail':''}))
""",
        encoding="utf-8",
    )
    case = CaseSpec("held", {"value": 7}, "save_reload", {"value": 7}, timeout_s=30)
    result = run_driver_case(clean_repo, case, tmp_path / "reports")
    assert result["status"] == "failure"
    assert any("expected 7" in item for item in result["errors"])
    assert any("artifact" in item for item in result["errors"])


def test_driver_case_rejects_artifact_outside_case_directory(
    clean_repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    onboarding = clean_repo / "warptest_onboarding"
    onboarding.mkdir()
    (onboarding / "driver.py").write_text(
        f"""import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--request'); p.add_argument('--report'); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
Path(a.report).write_text(json.dumps({{'status':'success','observed':req['target'],'app_started':True,'roundtrip':req['roundtrip'],'gate_enabled':True,'artifacts':[{str(outside)!r}],'detail':''}}))
""",
        encoding="utf-8",
    )
    case = CaseSpec("held", {"value": 7}, "save_reload", {"value": 7}, timeout_s=30)
    result = run_driver_case(clean_repo, case, tmp_path / "reports")
    assert result["status"] == "failure"
    assert any("outside artifact_dir" in item for item in result["errors"])


def test_projection_mismatches_uses_expected_as_subset() -> None:
    assert projection_mismatches(
        {"player": {"money": 7, "energy": 9}, "extra": True},
        {"player": {"money": 7}},
    ) == []
    assert projection_mismatches({"player": {}}, {"player": {"money": 7}}) == [
        "observed.player.money is missing"
    ]


def test_write_json_rejects_hash_and_fingerprint_fields(tmp_path: Path) -> None:
    assert find_forbidden_result_keys({"nested": {"prompt_hash": "x"}}) == [
        "nested.prompt_hash"
    ]
    with pytest.raises(ValueError, match="forbidden"):
        write_json(tmp_path / "bad.json", {"source_fingerprint": "x"})
    write_json(tmp_path / "good.json", {"clean_commit": "1" * 40})
    assert json.loads((tmp_path / "good.json").read_text())["clean_commit"] == "1" * 40
