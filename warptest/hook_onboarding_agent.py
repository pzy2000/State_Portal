"""Responses-based coding agent for automatic state-hook onboarding.

This module deliberately contains only the model/tool loop and a repository-
confined workspace.  Experiment orchestration, clean source materialization,
reference runs, and held-out grading live in :mod:`warptest.hook_onboarding`.

The agent never receives an arbitrary shell tool.  Build and smoke commands are
registered by the experiment manifest and exposed only by symbolic name through
``run_check``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .repo_tools import LAZARUS_IGNORED_DIRS, LAZARUS_SOURCE_TEXT_SUFFIXES, RepoSandbox


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_MAX_ROUNDS = 40
DEFAULT_MAX_TOOL_CALLS = 100
DEFAULT_WALL_TIME_S = 3_600.0
MAX_WRITE_BYTES = 256 * 1024
MAX_DIFF_CHARS = 40_000
MAX_CHECK_OUTPUT_CHARS = 30_000

_SECRET_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
_PATCH_PATH_RE = re.compile(
    r"^(?:diff --git a/(.+?) b/(.+?)|--- (.+?)|\+\+\+ (.+?))$",
    flags=re.MULTILINE,
)


HOOK_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_dir",
        "description": "List one directory inside the clean source repository.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "grep",
        "description": "Search repository text files with a regular expression.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": ["string", "null"]},
            },
            "required": ["pattern", "glob"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a bounded line range from one repository text file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": ["integer", "null"]},
                "end": {"type": ["integer", "null"]},
            },
            "required": ["path", "start", "end"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Create or replace one text file inside the repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "apply_patch",
        "description": "Apply a standard unified Git patch inside the repository.",
        "parameters": {
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_diff",
        "description": "Inspect current Git status, diff stat, and a bounded text diff.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "run_check",
        "description": "Run one manifest-registered build, test, smoke, or gate check by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass(frozen=True)
class NamedCheck:
    """One orchestrator-owned command exposed to the model by symbolic name."""

    argv: tuple[str, ...]
    cwd: str = "."
    timeout_s: float = 300.0
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NamedCheck":
        argv = value.get("argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
            raise ValueError("named check argv must be a list of strings")
        normalized = tuple(str(item) for item in argv)
        if not normalized or any(not item for item in normalized):
            raise ValueError("named check argv must contain non-empty strings")
        cwd = str(value.get("cwd") or ".")
        timeout_s = float(value.get("timeout_s") or 300.0)
        if timeout_s <= 0:
            raise ValueError("named check timeout_s must be positive")
        raw_env = value.get("env") or {}
        if not isinstance(raw_env, Mapping):
            raise ValueError("named check env must be a mapping")
        return cls(
            argv=normalized,
            cwd=cwd,
            timeout_s=timeout_s,
            env={str(key): str(item) for key, item in raw_env.items()},
        )


@dataclass
class UsageAccumulator:
    """Sum provider usage without inventing fields a response omitted."""

    _totals: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        }
    )
    _complete: dict[str, bool] = field(
        default_factory=lambda: {
            "input_tokens": True,
            "output_tokens": True,
            "total_tokens": True,
            "cached_input_tokens": True,
            "reasoning_tokens": True,
        }
    )
    responses: int = 0

    def add(self, usage: Any) -> dict[str, int | None]:
        values = usage_values(usage)
        self.responses += 1
        for key, value in values.items():
            if value is None:
                self._complete[key] = False
            else:
                self._totals[key] += int(value)
        return values

    def to_dict(self) -> dict[str, int | None]:
        return {
            **{
                key: self._totals[key] if self._complete[key] else None
                for key in self._totals
            },
            "responses": self.responses,
        }


@dataclass
class HookAgentResult:
    status: str
    final_text: str
    rounds: int
    tool_calls: int
    tool_counts: dict[str, int]
    wall_time_ms: int
    usage: dict[str, int | None]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "final_text": self.final_text,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "wall_time_ms": self.wall_time_ms,
            "usage": self.usage,
            "error": self.error,
        }


class HookAgentWorkspace:
    """Repository-confined implementation of the seven coding-agent tools."""

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        checks: Mapping[str, NamedCheck | Mapping[str, Any]] | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if not self.repo.is_dir():
            raise ValueError(f"onboarding repo does not exist: {self.repo}")
        self.sandbox = RepoSandbox(
            self.repo,
            text_suffixes=LAZARUS_SOURCE_TEXT_SUFFIXES,
            ignored_dirs=LAZARUS_IGNORED_DIRS,
        )
        self.checks: dict[str, NamedCheck] = {}
        self.mutation_epoch = 0
        self.check_attempts: Counter[str] = Counter()
        self.check_pass_epochs: dict[str, int] = {}
        for name, check in (checks or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError("named check names must be non-empty strings")
            self.checks[name] = (
                check if isinstance(check, NamedCheck) else NamedCheck.from_mapping(check)
            )

    def repo_map(self) -> str:
        return self.sandbox.build_repo_map()

    def dispatch(self, tool: str, args: Mapping[str, Any] | None) -> str:
        payload = dict(args or {})
        try:
            if tool == "list_dir":
                return self.sandbox.list_dir(str(payload.get("path") or "."))
            if tool == "grep":
                glob = payload.get("glob")
                return self.sandbox.grep(
                    str(payload.get("pattern") or ""),
                    str(glob) if isinstance(glob, str) else None,
                )
            if tool == "read_file":
                return self.sandbox.read_file(
                    str(payload.get("path") or ""),
                    payload.get("start"),
                    payload.get("end"),
                )
            if tool == "write_file":
                return self.write_file(payload.get("path"), payload.get("content"))
            if tool == "apply_patch":
                return self.apply_patch(payload.get("patch"))
            if tool == "get_diff":
                return self.get_diff(payload.get("path"))
            if tool == "run_check":
                return self.run_check(payload.get("name"))
        except Exception as error:  # tool errors are observations, not agent crashes
            return f"ERROR: {type(error).__name__}: {safe_error_text(error)}"
        return f"ERROR: unknown tool {tool!r}"

    def write_file(self, raw_path: Any, content: Any) -> str:
        if not isinstance(content, str):
            return "ERROR: content must be a string"
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            return f"ERROR: content exceeds {MAX_WRITE_BYTES} bytes"
        if "\x00" in content:
            return "ERROR: binary content is not allowed"
        path, error = self._resolve_mutable(raw_path)
        if error is not None or path is None:
            return f"ERROR: {error}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.mutation_epoch += 1
        return f"WROTE {path.relative_to(self.repo).as_posix()} ({len(encoded)} bytes)"

    def apply_patch(self, patch: Any) -> str:
        if not isinstance(patch, str) or not patch.strip():
            return "ERROR: patch must be a non-empty string"
        if len(patch.encode("utf-8")) > MAX_WRITE_BYTES:
            return f"ERROR: patch exceeds {MAX_WRITE_BYTES} bytes"
        error = self._validate_patch_paths(patch)
        if error is not None:
            return f"ERROR: {error}"
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=self.repo,
            input=patch,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "git apply failed").strip()
            return "ERROR: " + detail[-MAX_CHECK_OUTPUT_CHARS:]
        self.mutation_epoch += 1
        return "PATCH APPLIED"

    def get_diff(self, raw_path: Any = ".") -> str:
        path, error = self._resolve_existing(raw_path or ".")
        if error is not None or path is None:
            return f"ERROR: {error}"
        rel = path.relative_to(self.repo).as_posix() if path != self.repo else "."
        status = self._git(["status", "--short", "--", rel])
        stat = self._git(["diff", "--stat", "--", rel])
        diff = self._git(["diff", "--no-ext-diff", "--", rel])
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "--", rel]
        )
        rendered = "\n".join(
            [
                "STATUS:",
                status.strip() or "(clean tracked state)",
                "UNTRACKED FILES:",
                untracked.strip() or "(none)",
                "DIFF STAT:",
                stat.strip() or "(no tracked diff)",
                "DIFF:",
                diff[:MAX_DIFF_CHARS].rstrip() or "(no tracked diff)",
            ]
        )
        if len(diff) > MAX_DIFF_CHARS:
            rendered += "\n... (diff truncated)"
        return rendered

    def run_check(self, raw_name: Any) -> str:
        if not isinstance(raw_name, str) or not raw_name:
            return "ERROR: check name must be a non-empty string"
        check = self.checks.get(raw_name)
        if check is None:
            available = ", ".join(sorted(self.checks)) or "(none)"
            return f"ERROR: unknown check {raw_name!r}; available: {available}"
        self.check_attempts[raw_name] += 1
        cwd, error = self._resolve_check_cwd(check.cwd)
        if error is not None or cwd is None or not cwd.is_dir():
            return f"ERROR: invalid check cwd: {error or check.cwd}"
        log_path = self.report_dir / f"check-{safe_file_name(raw_name)}.log"
        env = sanitized_subprocess_env(check.env)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(check.argv),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=check.timeout_s,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            combined = "".join(
                [
                    f"$ {' '.join(check.argv)}\n",
                    completed.stdout or "",
                    "\n[stderr]\n",
                    completed.stderr or "",
                ]
            )
            log_path.write_text(combined, encoding="utf-8")
            if completed.returncode == 0:
                self.check_pass_epochs[raw_name] = self.mutation_epoch
            tail = combined[-MAX_CHECK_OUTPUT_CHARS:]
            return (
                f"CHECK {raw_name} exit={completed.returncode} elapsed_ms={elapsed_ms}\n"
                f"LOG {log_path}\n{tail}"
            )
        except subprocess.TimeoutExpired as error:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            output = text_output(error.stdout) + "\n[stderr]\n" + text_output(error.stderr)
            log_path.write_text(output, encoding="utf-8")
            return (
                f"ERROR: CHECK {raw_name} timed out after {check.timeout_s:g}s "
                f"elapsed_ms={elapsed_ms}\nLOG {log_path}\n"
                + output[-MAX_CHECK_OUTPUT_CHARS:]
            )
        except OSError as error:
            return f"ERROR: CHECK {raw_name} could not start: {safe_error_text(error)}"

    def check_is_current(self, name: str) -> bool:
        """Return whether a named check passed after the most recent source edit."""

        return self.check_pass_epochs.get(name) == self.mutation_epoch

    def _resolve_existing(self, raw_path: Any) -> tuple[Path | None, str | None]:
        path, error = self._resolve_mutable(raw_path)
        if error is not None or path is None:
            return path, error
        if not path.exists():
            return None, f"path does not exist: {path.relative_to(self.repo)}"
        return path, None

    def _resolve_check_cwd(self, raw_path: str) -> tuple[Path | None, str | None]:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved != self.repo and self.repo not in resolved.parents:
                return None, "check cwd is outside the repository"
            if not resolved.exists():
                return None, f"path does not exist: {resolved}"
            return resolved, None
        return self._resolve_existing(raw_path)

    def _resolve_mutable(self, raw_path: Any) -> tuple[Path | None, str | None]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None, "path must be a non-empty string"
        candidate = Path(raw_path.strip())
        if candidate.is_absolute():
            return None, "absolute paths are not allowed"
        if ".." in candidate.parts:
            return None, "parent traversal is not allowed"
        if ".git" in candidate.parts:
            return None, ".git may not be modified"
        current = self.repo
        for part in candidate.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                return None, "symlink paths are not allowed"
        resolved = (self.repo / candidate).resolve()
        if resolved != self.repo and self.repo not in resolved.parents:
            return None, "path is outside the repository"
        return resolved, None

    def _validate_patch_paths(self, patch: str) -> str | None:
        paths: list[str] = []
        for match in _PATCH_PATH_RE.finditer(patch):
            paths.extend(value for value in match.groups() if value and value != "/dev/null")
        if not paths:
            return "patch contains no recognized file paths"
        for raw in paths:
            normalized = raw.strip().split("\t", 1)[0]
            if normalized.startswith("a/") or normalized.startswith("b/"):
                normalized = normalized[2:]
            _, error = self._resolve_mutable(normalized)
            if error is not None:
                return f"unsafe patch path {raw!r}: {error}"
        return None

    def _git(self, args: list[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return f"ERROR: {(completed.stderr or completed.stdout).strip()}"
        return completed.stdout


class HookCodingAgent:
    """A small native function-calling coding agent over the Responses API."""

    def __init__(
        self,
        client: Any,
        workspace: HookAgentWorkspace,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        wall_time_s: float = DEFAULT_WALL_TIME_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("model must be non-empty")
        if not reasoning_effort:
            raise ValueError("reasoning_effort must be non-empty")
        if max_output_tokens <= 0 or max_rounds <= 0 or max_tool_calls <= 0 or wall_time_s <= 0:
            raise ValueError("agent limits must be positive")
        self.client = client
        self.workspace = workspace
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = int(max_output_tokens)
        self.max_rounds = int(max_rounds)
        self.max_tool_calls = int(max_tool_calls)
        self.wall_time_s = float(wall_time_s)
        self.clock = clock

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        transcript_path: str | Path | None = None,
    ) -> HookAgentResult:
        started = self.clock()
        usage = UsageAccumulator()
        tool_counts: Counter[str] = Counter()
        tool_calls = 0
        rounds = 0
        previous_response_id: str | None = None
        pending_input: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        transcript = Path(transcript_path).expanduser().resolve() if transcript_path else None
        if transcript is not None:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("", encoding="utf-8")
            self._event(transcript, {"type": "system_prompt", "content": system_prompt})
            self._event(transcript, {"type": "user_prompt", "content": user_prompt})

        while rounds < self.max_rounds:
            if self.clock() - started > self.wall_time_s:
                return self._result(
                    "wall_time_limit",
                    "",
                    rounds,
                    tool_calls,
                    tool_counts,
                    started,
                    usage,
                    f"agent exceeded {self.wall_time_s:g}s wall-time limit",
                )
            rounds += 1
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": pending_input,
                "tools": HOOK_AGENT_TOOLS,
                "reasoning": {"effort": self.reasoning_effort},
                "max_output_tokens": self.max_output_tokens,
            }
            if previous_response_id is not None:
                kwargs["previous_response_id"] = previous_response_id
            try:
                response = self.client.responses.create(**kwargs)
            except Exception as error:
                return self._result(
                    "provider_failure",
                    "",
                    rounds,
                    tool_calls,
                    tool_counts,
                    started,
                    usage,
                    safe_error_text(error),
                )

            response_usage = usage.add(getattr(response, "usage", None))
            response_id = str(getattr(response, "id", "") or "")
            provider_model = str(getattr(response, "model", "") or "")
            output_text = str(getattr(response, "output_text", "") or "")
            output_items = list(getattr(response, "output", None) or [])
            calls = [item for item in output_items if item_type(item) == "function_call"]
            if transcript is not None:
                self._event(
                    transcript,
                    {
                        "type": "response",
                        "round": rounds,
                        "response_id": response_id,
                        "status": getattr(response, "status", None),
                        "provider_model": provider_model,
                        "output_text": output_text,
                        "function_calls": len(calls),
                        "usage": response_usage,
                    },
                )

            if calls:
                if tool_calls + len(calls) > self.max_tool_calls:
                    return self._result(
                        "tool_call_limit",
                        output_text,
                        rounds,
                        tool_calls,
                        tool_counts,
                        started,
                        usage,
                        f"agent would exceed {self.max_tool_calls} tool calls",
                    )
                outputs: list[dict[str, Any]] = []
                for call in calls:
                    name = str(item_value(call, "name") or "")
                    call_id = str(item_value(call, "call_id") or "")
                    raw_arguments = item_value(call, "arguments")
                    args, parse_error = parse_tool_arguments(raw_arguments)
                    observation = (
                        f"ERROR: {parse_error}"
                        if parse_error is not None
                        else self.workspace.dispatch(name, args)
                    )
                    tool_calls += 1
                    tool_counts[name] += 1
                    outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": observation,
                        }
                    )
                    if transcript is not None:
                        self._event(
                            transcript,
                            {
                                "type": "tool",
                                "round": rounds,
                                "call_id": call_id,
                                "name": name,
                                "arguments": args if parse_error is None else None,
                                "argument_error": parse_error,
                                "output": observation,
                            },
                        )
                if not response_id:
                    return self._result(
                        "provider_failure",
                        output_text,
                        rounds,
                        tool_calls,
                        tool_counts,
                        started,
                        usage,
                        "tool-calling response did not include a response id",
                    )
                previous_response_id = response_id
                pending_input = outputs
                continue

            if output_text.strip():
                return self._result(
                    "success",
                    output_text.strip(),
                    rounds,
                    tool_calls,
                    tool_counts,
                    started,
                    usage,
                    None,
                )
            return self._result(
                "empty_agent_response",
                "",
                rounds,
                tool_calls,
                tool_counts,
                started,
                usage,
                "provider response contained neither function calls nor public output text",
            )

        return self._result(
            "round_limit",
            "",
            rounds,
            tool_calls,
            tool_counts,
            started,
            usage,
            f"agent reached {self.max_rounds} response rounds",
        )

    def _result(
        self,
        status: str,
        final_text: str,
        rounds: int,
        tool_calls: int,
        tool_counts: Counter[str],
        started: float,
        usage: UsageAccumulator,
        error: str | None,
    ) -> HookAgentResult:
        return HookAgentResult(
            status=status,
            final_text=final_text,
            rounds=rounds,
            tool_calls=tool_calls,
            tool_counts=dict(tool_counts),
            wall_time_ms=max(0, int((self.clock() - started) * 1000)),
            usage=usage.to_dict(),
            error=error,
        )

    @staticmethod
    def _event(path: Path, payload: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n")


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, Mapping):
        return dict(raw), None
    if not isinstance(raw, str):
        return {}, "tool arguments must be a JSON object"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        return {}, f"invalid tool JSON: {error.msg}"
    if not isinstance(value, dict):
        return {}, "tool arguments must decode to an object"
    return value, None


def usage_values(usage: Any) -> dict[str, int | None]:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": optional_int(getattr(usage, "input_tokens", None)),
        "output_tokens": optional_int(getattr(usage, "output_tokens", None)),
        "total_tokens": optional_int(getattr(usage, "total_tokens", None)),
        "cached_input_tokens": optional_int(
            getattr(input_details, "cached_tokens", None) if input_details is not None else None
        ),
        "reasoning_tokens": optional_int(
            getattr(output_details, "reasoning_tokens", None) if output_details is not None else None
        ),
    }


def optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def item_type(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def item_value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def sanitized_subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }
    for key, value in (extra or {}).items():
        if any(marker in str(key).upper() for marker in _SECRET_ENV_MARKERS):
            continue
        env[str(key)] = str(value)
    return env


def safe_error_text(error: BaseException, limit: int = 2_000) -> str:
    text = str(error).replace("\x00", "")
    for key, value in os.environ.items():
        if (
            any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
            and isinstance(value, str)
            and len(value) >= 4
        ):
            text = text.replace(value, "<redacted>")
    return text[:limit]


def safe_file_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return normalized or "check"


def text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_WALL_TIME_S",
    "HOOK_AGENT_TOOLS",
    "HookAgentResult",
    "HookAgentWorkspace",
    "HookCodingAgent",
    "NamedCheck",
    "UsageAccumulator",
    "parse_tool_arguments",
    "sanitized_subprocess_env",
    "usage_values",
]
