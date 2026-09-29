"""Run provenance and retry accounting for the P5 full-scale experiments.

Every producer report (state-loop, gui-agent, fuzzing, evo suites, model-bench)
should be traceable: which experiment and run produced it, against which git
commit and task manifest, and how many infra / rate-limit retries were spent
before it succeeded. This module centralizes that contract so producers and the
P5 orchestrator stamp identical fields.

The fields land under ``report["run_context"]``:

  experiment_id      logical experiment name (e.g. "state_loop", "c1_state").
  run_id             unique id for one invocation (uuid-based).
  git_commit         workspace ``git rev-parse HEAD`` at run time.
  task_manifest_hash stable hash of the task manifest / arm config used.
  retry_history      list of attempts (orchestrator-level), each recording the
                     attempt number, outcome, error class, and timing.

Retry policy (confirmed for P5):
  * infrastructure failures (network, engine bring-up, display, transient 5xx)
    are retried until the environment is healthy;
  * rate-limit model-call failures are retried multiple times;
  * a model that returns but produces an unusable / non-conformant output is a
    genuine failure and is NOT retried -- it counts in the results.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Substrings that mark a transient / retryable failure (infra or rate limit).
# Matched case-insensitively against ``type(error).__name__: str(error)``.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "429",
    "too many requests",
    "quota",
    "overloaded",
)
_INFRA_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "connectionerror",
    "connreset",
    "econnreset",
    "broken pipe",
    "temporarily unavailable",
    "service unavailable",
    "gateway unavailable",
    "bad gateway",
    "gateway timeout",
    "502",
    "503",
    "504",
    "eof occurred",
    "remote end closed",
    "server disconnected",
    "apiconnectionerror",
    "internalservererror",
    "internal server error",
    "http error 500",
    "returned no choices",
    "no choices",
    "runtime failed",
)

_SECRET_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_TPM_CEILING_RE = re.compile(
    r"(?i)\bTPM\s+limit\s+exceeded\s*:\s*"
    r"(?P<window>\d+)\s*\+\s*(?P<request>\d+)\s*>\s*(?P<limit>\d+)"
    r"(?:\s+for\s+model\s+(?P<model>[A-Za-z0-9._-]+))?"
)


def safe_error_text(error: BaseException | str, *, limit: int = 500) -> str:
    """Return an auditable error label without persisting runtime credentials.

    Provider SDK exceptions occasionally echo request headers or request bodies.
    C1 loads the paid gateway key into the process environment, so retry logs must
    scrub both bearer values and any configured secret-like environment values
    before printing or writing an error.  Classification still uses the original
    exception; this function is only for serialization and user-facing progress.
    """

    prefix = f"{type(error).__name__}: " if isinstance(error, BaseException) else ""
    text = prefix + str(error)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    secrets = {
        value
        for name, value in os.environ.items()
        if value
        and len(value) >= 8
        and any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
    }
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text[: max(0, int(limit))]


def provider_capacity_failure(error: BaseException | str) -> dict[str, Any] | None:
    """Return non-secret proof that one request alone exceeds a TPM ceiling.

    Ordinary rolling-window 429s remain retryable.  This terminal classification
    is intentionally narrow: the provider must report the current window, the
    request token mass, and the limit, and the request by itself must be larger
    than that limit.  Waiting or rotating a key cannot make such a request fit.
    """

    existing = getattr(error, "provider_capacity", None)
    if isinstance(existing, dict):
        request_tokens = existing.get("request_tokens")
        limit_tokens = existing.get("limit_tokens")
        if (
            isinstance(request_tokens, int)
            and not isinstance(request_tokens, bool)
            and isinstance(limit_tokens, int)
            and not isinstance(limit_tokens, bool)
            and request_tokens > limit_tokens > 0
        ):
            return dict(existing)

    status = (
        getattr(error, "status_code", None)
        if isinstance(error, BaseException)
        else 429
    )
    if status is None and isinstance(error, BaseException):
        status = getattr(error, "status", None)
    texts: list[str] = []
    if isinstance(error, BaseException):
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            nested = body.get("error")
            if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                texts.append(nested["message"])
            if isinstance(body.get("message"), str):
                texts.append(body["message"])
        message = getattr(error, "message", None)
        if isinstance(message, str):
            texts.append(message)
    texts.append(str(error))
    for text in texts:
        match = _TPM_CEILING_RE.search(text)
        if match is None:
            continue
        if isinstance(error, BaseException) and status != 429:
            continue
        window_tokens = int(match.group("window"))
        request_tokens = int(match.group("request"))
        limit_tokens = int(match.group("limit"))
        if request_tokens <= limit_tokens or limit_tokens <= 0:
            continue
        return {
            "failure_mode": "provider_capacity",
            "status_code": 429,
            "window_tokens": window_tokens,
            "request_tokens": request_tokens,
            "limit_tokens": limit_tokens,
            "model": match.group("model"),
            "proof": "single_request_tokens_exceed_tpm_limit",
        }
    return None


def git_commit(root: str | Path = ".") -> str | None:
    """Return the workspace ``git rev-parse HEAD`` short of failure noise."""

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    commit = out.stdout.strip()
    return commit or None


def manifest_hash(obj: Any) -> str:
    """Stable 16-hex-char hash of any JSON-serializable manifest/config object."""

    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def error_category(error: BaseException) -> str | None:
    """Classify an exception as ``rate_limit`` / ``infra`` / ``None`` (fatal).

    ``None`` means the failure is NOT transient and must not be retried.
    """

    if provider_capacity_failure(error) is not None:
        return None
    text = f"{type(error).__name__}: {error}".lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in text for marker in _INFRA_MARKERS):
        return "infra"
    return None


def is_retryable_error(error: BaseException) -> bool:
    return error_category(error) is not None


@dataclass
class RetryAttempt:
    attempt: int
    status: str  # "ok" | "retryable_error" | "fatal_error"
    category: str | None = None  # "rate_limit" | "infra" | None
    error: str | None = None
    timestamp_utc: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def call_with_retries(
    fn: Callable[[], Any],
    *,
    label: str = "",
    max_attempts: int = 5,
    base_sleep_s: float = 2.0,
    max_sleep_s: float = 60.0,
    classify: Callable[[BaseException], str | None] = error_category,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[str], None] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Call ``fn`` with retries on transient errors, returning (value, history).

    Transient errors (``classify`` returns a category) are retried with linear
    backoff up to ``max_attempts``. A fatal error (``classify`` returns None) is
    re-raised immediately, but only after its attempt is recorded so the failure
    is still traceable in ``retry_history``.
    """

    history: list[dict[str, Any]] = []
    last_error: BaseException | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        started = time.perf_counter()
        try:
            value = fn()
        except BaseException as error:  # noqa: BLE001 - we re-raise fatal/exhausted
            category = classify(error)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            status = "retryable_error" if category else "fatal_error"
            record = RetryAttempt(
                attempt=attempt,
                status=status,
                category=category,
                error=safe_error_text(error),
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=elapsed_ms,
            ).to_dict()
            history.append(record)
            last_error = error
            if on_event:
                on_event(
                    f"{label}: attempt {attempt} {status} ({category}): "
                    f"{safe_error_text(error)}"
                )
            if not category:
                # Fatal: surface the history on the exception so callers can audit it.
                try:
                    error._p5_retry_history = history  # type: ignore[attr-defined]
                except Exception:
                    pass
                raise
            if attempt >= max_attempts:
                break
            sleep(min(max_sleep_s, base_sleep_s * attempt))
            continue
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        history.append(
            RetryAttempt(
                attempt=attempt,
                status="ok",
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=elapsed_ms,
            ).to_dict()
        )
        if on_event:
            on_event(f"{label}: attempt {attempt} ok")
        return value, history
    assert last_error is not None
    try:
        last_error._p5_retry_history = history  # type: ignore[attr-defined]
    except Exception:
        pass
    raise last_error


def run_metadata(
    *,
    experiment_id: str,
    run_id: str | None = None,
    manifest: Any | None = None,
    task_manifest_hash: str | None = None,
    git_root: str | Path = ".",
    retry_history: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical ``run_context`` block stamped into report payloads."""

    meta: dict[str, Any] = {
        "experiment_id": experiment_id,
        "run_id": run_id or new_run_id(),
        "git_commit": git_commit(git_root),
        "task_manifest_hash": (
            task_manifest_hash
            if task_manifest_hash is not None
            else (manifest_hash(manifest) if manifest is not None else None)
        ),
        "retry_history": list(retry_history or []),
    }
    if extra:
        meta.update(extra)
    return meta


def stamp_payload(
    payload: dict[str, Any],
    *,
    run_meta: dict[str, Any] | None = None,
    experiment_id: str | None = None,
    git_root: str | Path = ".",
) -> dict[str, Any]:
    """Attach a ``run_context`` block to a report payload in place.

    If ``run_meta`` is supplied (the orchestrator path) it is used as-is; the
    direct-run path self-generates a minimal block keyed by ``experiment_id`` (or
    the payload's ``experiment`` field) so every report is traceable.
    """

    if run_meta is None:
        eid = experiment_id or str(payload.get("experiment") or "warptest")
        run_meta = run_metadata(experiment_id=eid, git_root=git_root)
    payload["run_context"] = run_meta
    return payload
