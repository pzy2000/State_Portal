"""Native warm-session support for jynew C3 fuzzing.

jynew's C1 checkpoint adapter (:mod:`warptest.jynew_runtime`) cold-starts a whole
new Unity ``-batchmode`` process per checkpoint request, which is far too slow to
inject dozens of mutated candidates per C3 fuzz task. This module mirrors
:mod:`warptest.openra_fuzz`'s native warm-session pattern: a single Unity batch
process stays alive, and candidates are injected through a small file protocol
(``request(seq) -> report(seq) -> ready(seq)``) implemented by the
``Jyx2.WarptestCheckpoint.RunWarm`` entry point in
``warptest/instrumentation/jynew/WarptestCheckpointCommand.cs``.

Unlike OpenRA (a live simulation whose actor/build state persists across ticks),
jynew's ``SynthesizeState()`` rebuilds a brand-new ``GameRuntimeData`` instance on
every request, so there is no cross-candidate contamination to guard against with
an explicit reset step -- the next request is inherently clean.

The only deep-state fields the LLM fuzzer may perturb are the ones
``WarptestTarget`` already exposes for ``kind: synthesized_state`` specs:
``player_level``, ``money``, ``map_id``, ``team_ids``, ``items``, ``skills``, and
``key_values``. ``mod_id`` and ``save_index`` are fixed by the harness (``save_index``
is pinned to ``-1`` so a candidate can never make the fuzzer read/write a real ES3
save slot).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import run_context
from .jynew_runtime import ensure_jynew_instrumentation, find_jynew_project_path, find_unity_executable


C3_SESSION_VERSION = "jynew-c3-session-v1"

_SAFE_MOD_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Bounds keep the LLM's "boundary/invalid" mutations exploring extreme-but-finite
# values (a genuine robustness stress) instead of degenerate inputs that would
# only measure how slow/large a Python<->JSON<->Unity round-trip can get.
_MAX_PLAYER_LEVEL = 100_000
_MAX_MONEY = 2_000_000_000
_MAX_MAP_ID = 1_000_000
_MAX_ROLE_ID = 1_000_000
_MAX_TEAM_IDS = 16
_MAX_ITEM_ID = 1_000_000
_MAX_ITEM_COUNT = 1_000_000_000
_MAX_ITEMS = 32
_MAX_SKILL_LEVEL = 1_000_000
_MAX_SKILLS = 16
_MAX_KEY_VALUES = 16
_MAX_VALUE_LEN = 256

FUZZ_STATE_FIELDS = frozenset(
    {"player_level", "money", "map_id", "team_ids", "items", "skills", "key_values"}
)


class JynewFuzzStateError(ValueError):
    """A candidate is outside the public C3 fuzz-state schema."""


class JynewWarmSessionError(RuntimeError):
    """The native warm session could not be bootstrapped."""


def _reject_extra(mapping: dict[str, Any], allowed: set[str] | frozenset[str], field_name: str) -> None:
    extras = sorted(str(key) for key in mapping if key not in allowed)
    if extras:
        raise JynewFuzzStateError(f"{field_name} contains unsupported field(s): {', '.join(extras)}")


def _bounded_int(value: Any, field_name: str, *, minimum: int, maximum: int) -> int:
    # ``bool`` is an ``int`` subclass; accepting it here would make an invalid
    # fuzzer shape look like a normal 0/1 value.
    if type(value) is not int or not minimum <= value <= maximum:
        raise JynewFuzzStateError(f"{field_name} must be an integer in [{minimum}, {maximum}]")
    return value


def _safe_key(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
        raise JynewFuzzStateError(
            f"{field_name} must be a safe identifier (1-64 alphanumeric/._:- characters)"
        )
    return value


def _bounded_str(value: Any, field_name: str, *, max_len: int) -> str:
    if not isinstance(value, str) or len(value) > max_len:
        raise JynewFuzzStateError(f"{field_name} must be a string of at most {max_len} characters")
    return value


def sanitize_jynew_fuzz_state(candidate: Any) -> dict[str, Any]:
    """Validate and canonicalize the only deep-state shape jynew C3 may inject.

    Invalid *shape* is deliberately rejected before it ever reaches Unity. A valid
    candidate may still name an unknown item/skill/role id or an out-of-range-but-
    representable stat; those reach ``SynthesizeState`` and produce a normal
    robustness result instead of a harness-level rejection.
    """

    if not isinstance(candidate, dict):
        raise JynewFuzzStateError("fuzz_state must be an object")
    _reject_extra(candidate, FUZZ_STATE_FIELDS, "fuzz_state")
    if set(candidate) != FUZZ_STATE_FIELDS:
        raise JynewFuzzStateError(f"fuzz_state requires exactly {sorted(FUZZ_STATE_FIELDS)}")

    player_level = _bounded_int(candidate["player_level"], "player_level", minimum=-1, maximum=_MAX_PLAYER_LEVEL)
    money = _bounded_int(candidate["money"], "money", minimum=-_MAX_MONEY, maximum=_MAX_MONEY)
    map_id = _bounded_int(candidate["map_id"], "map_id", minimum=-1, maximum=_MAX_MAP_ID)

    team_ids_raw = candidate["team_ids"]
    if not isinstance(team_ids_raw, list) or len(team_ids_raw) > _MAX_TEAM_IDS:
        raise JynewFuzzStateError(f"team_ids must be a list with at most {_MAX_TEAM_IDS} entries")
    team_ids = [
        _bounded_int(value, f"team_ids[{index}]", minimum=-_MAX_ROLE_ID, maximum=_MAX_ROLE_ID)
        for index, value in enumerate(team_ids_raw)
    ]

    items_raw = candidate["items"]
    if not isinstance(items_raw, list) or len(items_raw) > _MAX_ITEMS:
        raise JynewFuzzStateError(f"items must be a list with at most {_MAX_ITEMS} entries")
    items: list[dict[str, int]] = []
    for index, raw in enumerate(items_raw):
        if not isinstance(raw, dict):
            raise JynewFuzzStateError(f"items[{index}] must be an object")
        _reject_extra(raw, {"id", "count"}, f"items[{index}]")
        if set(raw) != {"id", "count"}:
            raise JynewFuzzStateError(f"items[{index}] requires exactly id and count")
        items.append(
            {
                "id": _bounded_int(raw["id"], f"items[{index}].id", minimum=-_MAX_ITEM_ID, maximum=_MAX_ITEM_ID),
                "count": _bounded_int(
                    raw["count"], f"items[{index}].count", minimum=-_MAX_ITEM_COUNT, maximum=_MAX_ITEM_COUNT
                ),
            }
        )

    skills_raw = candidate["skills"]
    if not isinstance(skills_raw, list) or len(skills_raw) > _MAX_SKILLS:
        raise JynewFuzzStateError(f"skills must be a list with at most {_MAX_SKILLS} entries")
    skills: list[dict[str, int]] = []
    for index, raw in enumerate(skills_raw):
        if not isinstance(raw, dict):
            raise JynewFuzzStateError(f"skills[{index}] must be an object")
        _reject_extra(raw, {"id", "level"}, f"skills[{index}]")
        if set(raw) != {"id", "level"}:
            raise JynewFuzzStateError(f"skills[{index}] requires exactly id and level")
        skills.append(
            {
                "id": _bounded_int(raw["id"], f"skills[{index}].id", minimum=-_MAX_ITEM_ID, maximum=_MAX_ITEM_ID),
                "level": _bounded_int(
                    raw["level"], f"skills[{index}].level", minimum=-_MAX_SKILL_LEVEL, maximum=_MAX_SKILL_LEVEL
                ),
            }
        )

    key_values_raw = candidate["key_values"]
    if not isinstance(key_values_raw, list) or len(key_values_raw) > _MAX_KEY_VALUES:
        raise JynewFuzzStateError(f"key_values must be a list with at most {_MAX_KEY_VALUES} entries")
    key_values: list[dict[str, str]] = []
    for index, raw in enumerate(key_values_raw):
        if not isinstance(raw, dict):
            raise JynewFuzzStateError(f"key_values[{index}] must be an object")
        _reject_extra(raw, {"key", "value"}, f"key_values[{index}]")
        if set(raw) != {"key", "value"}:
            raise JynewFuzzStateError(f"key_values[{index}] requires exactly key and value")
        key_values.append(
            {
                "key": _safe_key(raw["key"], f"key_values[{index}].key"),
                "value": _bounded_str(raw["value"], f"key_values[{index}].value", max_len=_MAX_VALUE_LEN),
            }
        )

    return {
        "player_level": player_level,
        "money": money,
        "map_id": map_id,
        "team_ids": team_ids,
        "items": items,
        "skills": skills,
        "key_values": key_values,
    }


def project_jynew_fuzz_state(seed_spec: dict[str, Any]) -> dict[str, Any]:
    """Project a jynew oracle spec into its immutable C3 fuzz-state seed.

    ``mod_id``, ``save_index``, ``validations``, ``actions``, and ``assertions``
    never enter this object: the LLM can only perturb the synthesized-state fields
    jynew already knows how to (dis)allow.
    """

    checkpoint = seed_spec.get("checkpoint")
    strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
    if not isinstance(strategies, list) or "jynew_utility_save_validate" not in strategies:
        raise JynewFuzzStateError("jynew C3 only supports jynew_utility_save_validate specs")

    target = seed_spec.get("target")
    if not isinstance(target, dict) or target.get("kind") != "synthesized_state":
        raise JynewFuzzStateError("jynew C3 only supports target.kind == synthesized_state specs")

    state = {
        "player_level": target.get("player_level", -1),
        "money": target.get("money", 0),
        "map_id": target.get("map_id", -1),
        "team_ids": list(target.get("team_ids") or []),
        "items": [dict(item) for item in (target.get("items") or [])],
        "skills": [dict(skill) for skill in (target.get("skills") or [])],
        "key_values": [dict(kv) for kv in (target.get("key_values") or [])],
    }
    return sanitize_jynew_fuzz_state(state)


def target_mod_id(seed_spec: dict[str, Any]) -> str:
    target = seed_spec.get("target") if isinstance(seed_spec.get("target"), dict) else {}
    mod_id = target.get("mod_id") or "JYX2"
    if not _SAFE_MOD_ID.fullmatch(str(mod_id)):
        raise JynewFuzzStateError("target.mod_id must be a safe identifier")
    return str(mod_id)


def build_jynew_target(fuzz_state: dict[str, Any], *, mod_id: str) -> dict[str, Any]:
    """Merge a sanitized fuzz-state back into a full ``WarptestTarget`` shape.

    ``save_index`` is pinned to ``-1`` (never a real ES3 slot) so C3 always takes
    the ``SynthesizeState`` path, never ``LoadSaveCheckpoint``.
    """

    state = sanitize_jynew_fuzz_state(fuzz_state)
    return {"kind": "synthesized_state", "mod_id": mod_id, "save_index": -1, **state}


@dataclass
class JynewWarmOutcome:
    """One candidate round-trip through the native C3 warm session."""

    sequence: int
    status: str  # robust | rejected | engine_error
    category: str | None = None
    detail: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JynewWarmSession:
    """A single Unity batch-mode jynew process driven by C3 request/report/ready files."""

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        mod_id: str = "JYX2",
        bootstrap_timeout_s: float | None = None,
    ) -> None:
        if not _SAFE_MOD_ID.fullmatch(str(mod_id)):
            raise JynewFuzzStateError("mod_id must be a safe identifier")
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.mod_id = str(mod_id)
        self.bootstrap_timeout_s = bootstrap_timeout_s
        self.benchmark = "jynew"
        self.git_commit = run_context.git_commit(self.repo)
        self.request_path = self.report_dir / "warptest-jynew-c3-request.json"
        self.report_path = self.report_dir / "warptest-jynew-c3-report.json"
        self.ready_path = self.report_dir / "warptest-jynew-c3-ready.json"
        self.stdout_path = self.report_dir / "jynew-warm.stdout.log"
        self.stderr_path = self.report_dir / "jynew-warm.stderr.log"
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._sequence = 0
        self.bootstrap_ms: int | None = None
        self.launch_command: list[str] = []

    def __enter__(self) -> "JynewWarmSession":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "JynewWarmSession":
        if self.is_alive():
            return self
        self.close()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.request_path, self.report_path, self.ready_path):
            path.unlink(missing_ok=True)

        ensure_jynew_instrumentation(self.repo)
        project_path = find_jynew_project_path(self.repo)
        unity_exe = find_unity_executable()

        self._sequence = 0
        self.launch_command = [
            unity_exe,
            "-batchmode",
            "-nographics",
            "-projectPath",
            str(project_path),
            "-executeMethod",
            "Jyx2.WarptestCheckpoint.RunWarm",
            "-logFile",
            str(self.report_dir / "unity-editor-warm.log"),
            "--",
            "--warptest-warm-request",
            str(self.request_path),
            "--warptest-warm-report",
            str(self.report_path),
            "--warptest-warm-ready",
            str(self.ready_path),
        ]
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        started = time.perf_counter()
        try:
            self._proc = subprocess.Popen(
                self.launch_command,
                cwd=self.repo,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                start_new_session=True,
            )
            timeout = self.bootstrap_timeout_s
            if timeout is None:
                # Unity's first batch-mode launch after a checkout can spend a long
                # time importing/reimporting assets; subsequent launches are much
                # faster once the Library/ cache is warm.
                timeout = float(os.environ.get("WARPTEST_JYNEW_WARM_BOOT_S", "300"))
            ready, problem = self._wait_for_ready(0, timeout_s=timeout)
            if ready is None:
                raise JynewWarmSessionError(f"jynew warm session did not become ready: {problem}")
            self.bootstrap_ms = int((time.perf_counter() - started) * 1000)
            return self
        except Exception:
            self.close()
            raise

    def restart(self) -> "JynewWarmSession":
        """Start a clean process after a candidate damaged liveness."""

        self.close()
        return self.start()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    handle.close()
                finally:
                    setattr(self, attr, None)

    def flush_logs(self) -> None:
        """Flush parent-owned log descriptors before evidence copies are taken."""

        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                try:
                    handle.flush()
                except OSError:
                    pass

    def run(self, fuzz_state: dict[str, Any], *, mod_id: str | None = None, timeout_s: float = 60.0) -> JynewWarmOutcome:
        """Inject one already-bounded candidate and wait for its report + ready."""

        try:
            state = sanitize_jynew_fuzz_state(fuzz_state)
        except JynewFuzzStateError as error:
            return JynewWarmOutcome(0, "rejected", "schema", str(error))
        if not self.is_alive():
            return JynewWarmOutcome(
                self._sequence, "engine_error", "process_exit", "jynew warm session is not running"
            )

        self._sequence += 1
        sequence = self._sequence
        target = build_jynew_target(state, mod_id=mod_id or self.mod_id)
        request = {
            "version": C3_SESSION_VERSION,
            "sequence": sequence,
            "spec": {"target": target, "validations": [], "actions": [], "assertions": []},
        }
        started = time.perf_counter()
        try:
            self._atomic_json(self.request_path, request)
        except Exception as error:
            return JynewWarmOutcome(sequence, "rejected", "protocol", f"could not publish request: {error}")

        report, problem = self._wait_for_report(sequence, timeout_s=timeout_s)
        elapsed = int((time.perf_counter() - started) * 1000)
        if report is None:
            category = "process_exit" if problem == "process exited" else "timeout"
            return JynewWarmOutcome(sequence, "engine_error", category, problem or "no report", elapsed_ms=elapsed)

        outcome = self._outcome_from_report(sequence, report, elapsed_ms=elapsed)
        if outcome.status == "rejected" and outcome.category == "protocol":
            return outcome
        ready, ready_problem = self._wait_for_ready(sequence, timeout_s=timeout_s)
        if ready is None:
            category = "reset_process_exit" if ready_problem == "process exited" else "reset_timeout"
            return JynewWarmOutcome(
                sequence,
                "engine_error",
                category,
                f"candidate report was written but the warm session did not signal ready: {ready_problem}",
                checks=outcome.checks,
                elapsed_ms=elapsed,
            )
        return outcome

    def _wait_for_report(self, sequence: int, *, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            report = self._read_json(self.report_path)
            if report is not None and report.get("sequence") == sequence:
                if report.get("version") != C3_SESSION_VERSION:
                    return {
                        "version": report.get("version"),
                        "sequence": sequence,
                        "status": "rejected",
                        "detail": "native report uses an unexpected C3 session version",
                    }, None
                return report, None
            if not self.is_alive():
                return None, "process exited"
            time.sleep(0.05)
        return None, f"timed out waiting for jynew report sequence={sequence}"

    def _wait_for_ready(self, sequence: int, *, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if not self.is_alive():
                return None, "process exited"
            ready = self._read_json(self.ready_path)
            if ready is not None and ready.get("sequence") == sequence:
                if ready.get("version") != C3_SESSION_VERSION or ready.get("status") != "ready":
                    return None, "native ready record has an unexpected protocol shape"
                return ready, None
            time.sleep(0.05)
        return None, f"timed out waiting for jynew ready sequence={sequence}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            # Unity's File.WriteAllText(..., Encoding.UTF8) emits a BOM; utf-8-sig
            # strips it so polling does not spin until timeout on an otherwise valid file.
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        # ``allow_nan=False`` is intentional: non-finite values are a structural
        # rejection, not a malformed protocol payload that can poison the session.
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _outcome_from_report(sequence: int, report: dict[str, Any], *, elapsed_ms: int) -> JynewWarmOutcome:
        raw_status = str(report.get("status") or "").lower()
        detail = str(report.get("detail") or "")[:1000]
        checks = [item for item in report.get("checks", []) if isinstance(item, dict)]

        if raw_status in {"success", "failure"}:
            # ProcessRequest() already turned every expected failure mode it knows
            # about (bad save index, synthesis exception, failed validation/action/
            # assertion check) into a "failure" status with per-check detail. That
            # is jynew handling an adversarial candidate gracefully, not a defect.
            status = "robust"
            category = "success" if raw_status == "success" else "handled_failure"
        elif raw_status == "rejected":
            status = "rejected"
            category = "protocol"
        elif raw_status == "engine_error":
            # An exception that escaped ProcessRequest()'s own try/catch coverage.
            status = "engine_error"
            category = "uncaught_exception"
        else:
            status = "rejected"
            category = "protocol"
            detail = detail or f"unexpected native jynew C3 report status: {raw_status or '<missing>'}"
        return JynewWarmOutcome(sequence=sequence, status=status, category=category, detail=detail, checks=checks, elapsed_ms=elapsed_ms)
