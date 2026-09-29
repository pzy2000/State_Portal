"""Issue naturalization (surface layer of metric-grounded provenance).

The mechanical spec produced by oracle_synth is the primary provenance artifact
and the ground truth. This module renders it into a *natural-language* issue the
way an engineer would write a bug report / regression request, using an LLM as a
pure paraphraser. The LLM never decides what to test or what the answer is; it
only chooses wording, and every rendering is mechanically checked for
information-equivalence against the spec by TokenGuard. A rendering that drops a
grader-critical token or leaks a stripped mechanism token is rejected and
re-sampled. This keeps the issue natural (defensible against "machine-serialized
dict dump" criticism) while keeping the defensibility triangle -- selection,
ground truth, discrimination -- entirely mechanical.

Credentials are read from the environment so the secret is not committed here:
    WARPTEST_NL_API_KEY, WARPTEST_NL_BASE_URL, WARPTEST_NL_MODEL (default gpt-5.5),
    WARPTEST_NL_END_USER (optional X-End-User header).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# ---- token equivalence guard (standalone port of scripts/check_issue_completeness.py)

_TIMING = {"wait_ms", "ms", "timeout_ms", "delay_ms"}
_FORBIDDEN = (
    "recover this exact", "reproduce every field", "in order; reproduce",
    "hidden target", "hidden oracle", "hidden_spec", "specs/", "wait_ms",
    "target: {", "allowed_strategies=", "expected_validity=",
)


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _collect(v: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(v, dict):
        for k, val in v.items():
            if k in _TIMING:
                continue
            _collect(val, out)
    elif isinstance(v, list):
        for val in v:
            _collect(val, out)
    elif v is not None:
        if isinstance(v, bool):
            out.append(("true" if v else "false", "bool"))
        elif isinstance(v, (int, float)):
            f = float(v)
            out.append((str(int(f)) if f.is_integer() else str(f), "num"))
        elif "\n" in str(v):
            out.append((str(v), "multiline"))
        else:
            out.append((str(v), "str"))


def _recoverable(issue: str, raw: str, kind: str) -> bool:
    if kind == "num":
        return re.search(r"(?<![\d.])" + re.escape(raw) + r"(?![\d])", issue) is not None
    if kind == "bool":
        return raw in issue.lower()
    if kind == "multiline":
        words = [w for w in re.split(r"[^a-z0-9]+", raw.lower()) if w]
        ni = _norm(issue)
        return all(w in ni for w in words)
    return _norm(raw) in _norm(issue)


def _mechanism_tokens(spec: dict) -> list[str]:
    """Tokens that the explore variant must HIDE (recoverable from source/contract
    instead of prose): strategy names, validity class, framework action-type names."""
    cp = spec.get("checkpoint") or {}
    toks = list(cp.get("allowed_strategies", []))
    if cp.get("expected_validity"):
        toks.append(cp["expected_validity"])
    for a in spec.get("actions", []) or []:
        if isinstance(a, dict) and a.get("type"):
            toks.append(a["type"])
    return sorted(set(toks))


@dataclass
class GuardResult:
    ok: bool
    missing: list[tuple[str, str]] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    leaked_mechanism: list[str] = field(default_factory=list)


class TokenGuard:
    """Check that a natural issue is information-equivalent to the mechanical spec."""

    def __init__(self, spec: dict):
        self.spec = spec
        req: list[tuple[str, str]] = []
        _collect(spec.get("target"), req)
        _collect(spec.get("actions"), req)
        for s in (spec.get("checkpoint") or {}).get("allowed_strategies", []):
            req.append((s, "str"))
        ev = (spec.get("checkpoint") or {}).get("expected_validity")
        if ev:
            req.append((ev, "str"))
        # de-duplicate
        self.required = sorted(set(req))
        self.mechanism = _mechanism_tokens(spec)

    def check_shallow(self, issue: str) -> GuardResult:
        missing = sorted({(r, k) for (r, k) in self.required if not _recoverable(issue, r, k)})
        forbidden = [f for f in _FORBIDDEN if f in issue.lower()]
        return GuardResult(ok=not missing and not forbidden, missing=missing, forbidden=forbidden)

    def check_explore(self, issue: str) -> GuardResult:
        # Intent still recoverable EXCEPT the deliberately stripped mechanism tokens.
        mech_norm = {_norm(t) for t in self.mechanism}
        missing = sorted(
            {
                (r, k)
                for (r, k) in self.required
                if _norm(r) not in mech_norm and not _recoverable(issue, r, k)
            }
        )
        forbidden = [f for f in _FORBIDDEN if f in issue.lower()]
        leaked = sorted({t for t in self.mechanism if _norm(t) and _norm(t) in _norm(issue)})
        return GuardResult(
            ok=not missing and not forbidden and not leaked,
            missing=missing,
            forbidden=forbidden,
            leaked_mechanism=leaked,
        )

    def check_reverse(
        self,
        public_text: str,
        *,
        extra_forbidden: tuple[str, ...] = (),
        minimum_normalized_length: int = 4,
    ) -> GuardResult:
        """Reject text that makes any guarded token recoverable.

        Lazarus uses the existing information-equivalence guard in the reverse
        direction: target values and fix details are coordinator-only, so a
        public task is valid only when none of those tokens can be recovered.
        Very short strings are ignored because they otherwise match ordinary
        prose accidentally (for example, a one-character enum value).
        """

        leaked = sorted(
            {
                raw
                for raw, kind in self.required
                if len(_norm(raw)) >= minimum_normalized_length
                and _recoverable(public_text, raw, kind)
            }
        )
        forbidden = sorted(
            {
                token
                for token in extra_forbidden
                if token.strip() and token.lower() in public_text.lower()
            }
        )
        return GuardResult(
            ok=not leaked and not forbidden,
            forbidden=forbidden,
            leaked_mechanism=leaked,
        )


# ---- LLM paraphraser (GPT-5.5) ---------------------------------------------

def _client():
    from openai import OpenAI

    api_key = os.environ.get("WARPTEST_NL_API_KEY")
    base_url = os.environ.get("WARPTEST_NL_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError(
            "Set WARPTEST_NL_API_KEY and WARPTEST_NL_BASE_URL for issue naturalization."
        )
    headers = {}
    end_user = os.environ.get("WARPTEST_NL_END_USER")
    if end_user:
        headers["X-End-User"] = end_user
    return OpenAI(base_url=base_url.rstrip("/") + "/", api_key=api_key, default_headers=headers)


def _model() -> str:
    return os.environ.get("WARPTEST_NL_MODEL", "gpt-5.5")


def _create_with_temp_fallback(client, model: str, messages: list):
    """Prefer temperature=0 for determinism; some models (e.g. gpt-5.5) only allow
    the default temperature, so fall back to omitting it. Ground-truth determinism
    is unaffected (it is mechanical); phrasing is guard-verified regardless."""
    try:
        return client.chat.completions.create(model=model, messages=messages, temperature=0)
    except Exception as exc:
        if "temperature" in str(exc).lower():
            return client.chat.completions.create(model=model, messages=messages)
        raise


_SHALLOW_SYS = (
    "You are an engineer writing a concise, natural regression/bug-report style issue "
    "for a game checkpoint test. Write flowing prose, no JSON, no dict dumps, no phrases "
    "like 'recover this exact' or 'reproduce every field'. You MUST mention every concrete "
    "value from the spec (board layout, scores, flags spelled out as true/false, action "
    "type names and arguments, the checkpoint strategy name, and the validity class) so a "
    "reader could reconstruct the test. Do not reference any hidden/answer files."
)

_EXPLORE_SYS = (
    "You are an engineer writing a concise, natural issue for a game checkpoint test, in a "
    "de-leaked 'explore-hard' style. Write flowing prose, no JSON. You MUST keep every concrete "
    "target value and action argument. You MUST NOT name the checkpoint strategy, the validity "
    "class, or the framework action-type identifiers; instead instruct the reader to read the "
    "game's own source to work out the persistence/strategy and choose the matching WarpTest "
    "checkpoint strategy and correct validity class. Do not reference any hidden/answer files."
)


def _spec_brief(spec: dict) -> str:
    import json

    return json.dumps(
        {
            "benchmark": spec.get("benchmark"),
            "target": spec.get("target"),
            "checkpoint": spec.get("checkpoint"),
            "actions": spec.get("actions"),
        },
        ensure_ascii=False,
        indent=2,
    )


def naturalize_issue(
    spec: dict,
    *,
    variant: str = "shallow",
    max_attempts: int = 4,
) -> dict[str, Any]:
    """Return {issue, variant, attempts, guard, raw, model} for a spec.

    Re-samples up to max_attempts, feeding guard feedback back into the prompt.
    Raises if it cannot produce an information-equivalent rendering.
    """
    guard = TokenGuard(spec)
    system = _SHALLOW_SYS if variant == "shallow" else _EXPLORE_SYS
    client = _client()
    model = _model()
    brief = _spec_brief(spec)

    feedback = ""
    last: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        user = (
            f"Mechanical checkpoint spec (ground truth):\n{brief}\n\n"
            "Write the issue as a single natural paragraph."
        )
        if feedback:
            user += f"\n\nYour previous attempt failed the equivalence check: {feedback}\nFix it."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        resp = _create_with_temp_fallback(client, model, messages)
        issue = (resp.choices[0].message.content or "").strip()
        gr = guard.check_shallow(issue) if variant == "shallow" else guard.check_explore(issue)
        last = {
            "issue": issue,
            "variant": variant,
            "attempts": attempt,
            "guard": {
                "ok": gr.ok,
                "missing": gr.missing,
                "forbidden": gr.forbidden,
                "leaked_mechanism": gr.leaked_mechanism,
            },
            "model": model,
        }
        if gr.ok:
            return last
        parts = []
        if gr.missing:
            parts.append(f"missing tokens {gr.missing[:8]}")
        if gr.forbidden:
            parts.append(f"forbidden phrases {gr.forbidden}")
        if gr.leaked_mechanism:
            parts.append(f"must NOT mention {gr.leaked_mechanism}")
        feedback = "; ".join(parts)
    raise RuntimeError(f"naturalization failed guard after {max_attempts} attempts: {feedback}")


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def naturalize_specs(
    spec_paths: list,
    *,
    workspace,
    max_attempts: int = 5,
    out_report=None,
) -> dict:
    """Generate guarded shallow+explore issues for each spec; write a report.

    Each issue records the model, attempt count, guard result, and a content hash
    of the raw rendering so the natural language is auditable and re-checkable.
    """
    import json
    from pathlib import Path

    import yaml

    workspace = Path(workspace)
    tasks: list[dict] = []
    for sp in spec_paths:
        sp = Path(sp)
        spec = yaml.safe_load(sp.read_text(encoding="utf-8"))
        task_id = sp.stem
        entry: dict = {"task_id": task_id, "spec": str(sp.relative_to(workspace)) if _under(sp, workspace) else str(sp)}
        for variant in ("shallow", "explore"):
            r = naturalize_issue(spec, variant=variant, max_attempts=max_attempts)
            entry[variant] = {
                "issue": r["issue"],
                "attempts": r["attempts"],
                "guard_ok": r["guard"]["ok"],
                "model": r["model"],
                "issue_sha": _sha(r["issue"]),
            }
        tasks.append(entry)

    payload = {
        "game": "2048",
        "role": "surface layer: LLM paraphrase gated by TokenGuard (information-equivalent to mechanical spec)",
        "tasks": tasks,
    }
    out_report = out_report or (workspace / "reports" / "oracle_synth" / "2048-issues.json")
    Path(out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(out_report).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    payload["_out_report"] = str(out_report)
    return payload


def _under(path, base) -> bool:
    from pathlib import Path

    try:
        Path(path).relative_to(Path(base))
        return True
    except ValueError:
        return False
