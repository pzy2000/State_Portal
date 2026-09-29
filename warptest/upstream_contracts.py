"""Core contracts for the SWE-bench-style, issue-guided Luanti fuzz pipeline.

This module is the single source of truth for the four design contracts that the
mining / classification / priors / replay code all depend on. They are encoded
here as constants, dataclasses, and pure helper functions (no I/O, no network) so
they can be imported and unit-tested in isolation, and so the rest of the pipeline
cannot silently drift from them.

Contract 1 -- Dual-axis reachability
    The headless C3 fuzz warm session (``LuantiWarmSession(render=False,
    map_backend="dummy")``) can only inject a small subset of deep state and does
    NOT exercise real world deserialization / server-restart load / settings /
    mapgen parsing. Two INDEPENDENT axes must be tracked; a single "state-reachable"
    filter conflates them and produces priors that cannot actually be triggered:
      * ``fuzz_reachable``      -- can the current C3 ``target`` JSON trigger it?
      * ``headless_replayable`` -- can it be reproduced on a headless server via an
                                   independent world/config/mod fixture?

Contract 2 -- pre/post fix SHA
    GitHub's ``baseRefOid`` is NOT the parent of the merge, so it must never be used
    as the "before-fix" build. The only correct commits are derived from the merge
    commit itself.

Contract 3 -- Independent replay defect oracle
    The existing repro worldmod treats ``crashed or status=="failure"`` as
    "reproduced", which counts ordinary validation mismatches as defects. Replay
    must use an EXPLICIT defect oracle (abnormal exit / hang / log signature /
    semantic mismatch) and emit one of five verdict states, never two booleans.

Contract 4 -- Isolated build
    ``LuantiWarmSession.start()`` installs/refreshes the worldmod (and can edit
    ``src/client/game.cpp``) inside the checkout, so historical commits must NEVER
    be checked out in-place in ``games/luanti``. Builds run from a bare git cache +
    per-SHA detached worktree + external build dir, keyed by a reproducible cache key.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# Bumped whenever the on-disk bug-card / recipe / verify schema changes shape.
SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Contract 1: dual-axis reachability
# --------------------------------------------------------------------------- #

AXIS_FUZZ_REACHABLE = "fuzz_reachable"
AXIS_HEADLESS_REPLAYABLE = "headless_replayable"
REACHABILITY_AXES = (AXIS_FUZZ_REACHABLE, AXIS_HEADLESS_REPLAYABLE)

# The ONLY ``target`` sub-fields the headless worldmod actually synthesizes. The
# online ``player`` sub-state is skipped in headless mode (no online player), so it
# is deliberately excluded -- a prior that perturbs ``player`` can never fire on the
# C3 path. Keep in sync with the worldmod ``synthesize`` (instrumentation/luanti).
FUZZ_REACHABLE_TARGET_FIELDS = frozenset(
    {"nodes", "fill", "meta_inventory", "mod_storage"}
)

# Sub-fields that exist in specs but are NOT reachable on the headless fuzz path.
FUZZ_UNREACHABLE_TARGET_FIELDS = frozenset({"player"})

# Engine surfaces that the C3 warm session (running world, dummy map backend) does
# NOT exercise, i.e. things that require a real on-disk world, a server restart, or
# config/mapgen parsing. Only ``headless_replayable`` fixtures (a dedicated server
# loading a crafted world/config) can reach these; the fuzz path cannot.
HEADLESS_ONLY_SURFACES = frozenset(
    {
        "mapblock_deserialization",
        "world_load_on_restart",
        "settings_parsing",
        "mapgen_param_parsing",
        "save_restore_disk",
    }
)

# File-path substrings used ONLY as an initial screening signal for classification.
# Per Contract 1, path matches must never be the final decision -- they set a prior
# with low/medium confidence that a human or a richer signal can override.
FUZZ_REACHABLE_PATH_HINTS = (
    "src/nodedef",
    "src/itemdef",
    "src/inventory",
    "src/content_",
    "src/script/lua_api/l_nodemeta",
    "src/script/lua_api/l_inventory",
    "src/script/lua_api/l_item",
    "src/script/lua_api/l_env",
    "builtin/game/item",
    "builtin/game/register",
)
HEADLESS_REPLAYABLE_PATH_HINTS = (
    "src/map.cpp",
    "src/mapblock",
    "src/mapsector",
    "src/serialization",
    "src/database",
    "src/mapgen/",
    "src/settings",
    "src/content/subgames",
    "src/server.cpp",
    "src/serverenvironment",
)


# --------------------------------------------------------------------------- #
# Contract 2: pre/post fix SHA derivation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MergeShas:
    """The commits that bound a merged fix PR, derived per Contract 2."""

    pre_fix_sha: str
    post_fix_sha: str
    merge_commit: str


def derive_pre_post_sha(merge_commit: dict[str, Any] | None) -> MergeShas:
    """Return ``(pre_fix, post_fix, merge)`` from a GraphQL ``mergeCommit`` node.

    ``pre_fix_sha  = mergeCommit.parents[0].oid`` (mainline before the merge)
    ``post_fix_sha = mergeCommit.oid`` (the merge itself, fix applied)

    ``baseRefOid`` is intentionally ignored -- it is the tip of the base branch at
    PR-open time, not the merge parent, so using it would drag unrelated mainline
    changes into the before/after comparison. Raises ``ValueError`` when the PR has
    no merge commit (e.g. squashed/rebased with missing data) or no first parent, so
    such PRs are surfaced as un-replayable rather than silently mis-bounded.
    """

    if not isinstance(merge_commit, dict):
        raise ValueError("PR has no mergeCommit; cannot derive pre/post fix SHA")
    post = str(merge_commit.get("oid") or "").strip()
    if not post:
        raise ValueError("mergeCommit.oid is empty; cannot derive post-fix SHA")
    parents_node = merge_commit.get("parents") or {}
    parents = parents_node.get("nodes") if isinstance(parents_node, dict) else parents_node
    if not parents:
        raise ValueError("mergeCommit has no parents; cannot derive pre-fix SHA")
    first_parent = parents[0]
    pre = str((first_parent or {}).get("oid") or "").strip()
    if not pre:
        raise ValueError("mergeCommit.parents[0].oid is empty; cannot derive pre-fix SHA")
    return MergeShas(pre_fix_sha=pre, post_fix_sha=post, merge_commit=post)


# --------------------------------------------------------------------------- #
# Body issue-reference parsing (kept separate from GraphQL closingIssuesReferences)
# --------------------------------------------------------------------------- #

# GitHub closing keywords (only these auto-close). Matched case-insensitively.
_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_CLOSING_RE = re.compile(
    r"\b(?:%s)\b\s*:?\s+#(\d+)" % "|".join(_CLOSING_KEYWORDS), re.IGNORECASE
)
_ANY_ISSUE_RE = re.compile(r"#(\d+)")


def parse_body_closing_refs(body: str | None) -> list[int]:
    """Issue numbers referenced by a closing keyword in the PR body."""

    if not body:
        return []
    seen: dict[int, None] = {}
    for match in _CLOSING_RE.finditer(body):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


def parse_body_related_refs(body: str | None, exclude: set[int] | None = None) -> list[int]:
    """All other ``#N`` issue references in the body (related, not auto-closing)."""

    if not body:
        return []
    exclude = exclude or set()
    seen: dict[int, None] = {}
    for match in _ANY_ISSUE_RE.finditer(body):
        num = int(match.group(1))
        if num not in exclude:
            seen.setdefault(num, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# Contract 3: independent replay defect oracle + five-state verdict
# --------------------------------------------------------------------------- #

# The five replay verdict states (never a bare boolean pair).
VERDICT_PRE_REPRODUCED_POST_FIXED = "PRE_REPRODUCED_POST_FIXED"
VERDICT_PRE_NOT_REPRODUCED = "PRE_NOT_REPRODUCED"
VERDICT_POST_STILL_REPRODUCED = "POST_STILL_REPRODUCED"
VERDICT_BUILD_FAILED = "BUILD_FAILED"
VERDICT_HARNESS_INCOMPATIBLE = "HARNESS_INCOMPATIBLE"
REPLAY_VERDICTS = (
    VERDICT_PRE_REPRODUCED_POST_FIXED,
    VERDICT_PRE_NOT_REPRODUCED,
    VERDICT_POST_STILL_REPRODUCED,
    VERDICT_BUILD_FAILED,
    VERDICT_HARNESS_INCOMPATIBLE,
)


@dataclass
class ReplayObservation:
    """One side (pre or post) of a replay run: raw signals + oracle inputs."""

    build_ok: bool = False
    harness_ok: bool = False
    exit_code: int | None = None
    timed_out: bool = False
    log_text: str = ""
    result_json: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_ok": self.build_ok,
            "harness_ok": self.harness_ok,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "result_json": self.result_json,
            "log_excerpt": self.log_text[-2000:] if self.log_text else "",
        }


@dataclass
class DefectOracle:
    """Explicit, per-replay defect definition (Contract 3).

    A replay counts as a defect ONLY when at least one enabled signal fires. This
    deliberately does not treat a plain ``status == "failure"`` (e.g. a validation
    mismatch) as a defect -- that must be expressed via an explicit
    ``semantic_assertion`` if it is genuinely part of the bug.
    """

    # Non-zero / signal exit is a defect (crash / abort).
    abnormal_exit: bool = True
    # A hang past the watchdog timeout is a defect.
    timeout: bool = True
    # Substrings/regexes that, if present in captured stdout+log, mark a defect
    # (e.g. "AsyncErr", "ServerError", "Segmentation fault", a stack signature).
    log_signatures: list[str] = field(default_factory=list)
    # Optional explicit result-json field expectations. Each entry is
    # {"path": "status", "equals": "failure"} or {"path": ..., "contains": ...};
    # a MISMATCH against `equals`/`contains` (i.e. the buggy output) is a defect.
    semantic_assertions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "abnormal_exit": self.abnormal_exit,
            "timeout": self.timeout,
            "log_signatures": list(self.log_signatures),
            "semantic_assertions": list(self.semantic_assertions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DefectOracle":
        data = data or {}
        return cls(
            abnormal_exit=bool(data.get("abnormal_exit", True)),
            timeout=bool(data.get("timeout", True)),
            log_signatures=list(data.get("log_signatures") or []),
            semantic_assertions=list(data.get("semantic_assertions") or []),
        )

    def evaluate(self, obs: ReplayObservation) -> tuple[bool, str]:
        """Return ``(is_defect, reason)`` for one observation."""

        if self.timeout and obs.timed_out:
            return True, "hang: watchdog timeout"
        if self.abnormal_exit and obs.exit_code is not None and obs.exit_code != 0:
            return True, f"abnormal process exit (code={obs.exit_code})"
        if self.log_signatures and obs.log_text:
            for sig in self.log_signatures:
                try:
                    if re.search(sig, obs.log_text):
                        return True, f"log signature matched: {sig!r}"
                except re.error:
                    if sig in obs.log_text:
                        return True, f"log substring matched: {sig!r}"
        for assertion in self.semantic_assertions:
            hit, reason = _semantic_mismatch(assertion, obs.result_json)
            if hit:
                return True, reason
        return False, "no defect signal fired"


def _semantic_mismatch(assertion: dict[str, Any], result: dict[str, Any] | None) -> tuple[bool, str]:
    """A semantic assertion describes the BUGGY output; matching it => defect."""

    path = str(assertion.get("path", ""))
    actual = _dig(result, path) if result is not None else None
    if "equals" in assertion:
        if actual == assertion["equals"]:
            return True, f"semantic: {path} == {assertion['equals']!r} (buggy output)"
    if "contains" in assertion:
        needle = str(assertion["contains"])
        if isinstance(actual, str) and needle in actual:
            return True, f"semantic: {path} contains {needle!r} (buggy output)"
    return False, ""


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    if not dotted:
        return cur
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def classify_replay(pre: ReplayObservation, post: ReplayObservation, oracle: DefectOracle) -> dict[str, Any]:
    """Combine pre/post observations into one of the five verdict states.

    Precedence: build failure and harness incompatibility dominate (the experiment
    is invalid), then the pre/post defect flip decides the scientific verdict.
    """

    if not pre.build_ok or not post.build_ok:
        return {
            "verdict": VERDICT_BUILD_FAILED,
            "reason": f"build_ok pre={pre.build_ok} post={post.build_ok}",
            "pre": pre.to_dict(),
            "post": post.to_dict(),
        }
    if not pre.harness_ok or not post.harness_ok:
        return {
            "verdict": VERDICT_HARNESS_INCOMPATIBLE,
            "reason": f"harness_ok pre={pre.harness_ok} post={post.harness_ok}",
            "pre": pre.to_dict(),
            "post": post.to_dict(),
        }
    pre_defect, pre_reason = oracle.evaluate(pre)
    post_defect, post_reason = oracle.evaluate(post)
    if not pre_defect:
        verdict = VERDICT_PRE_NOT_REPRODUCED
    elif post_defect:
        verdict = VERDICT_POST_STILL_REPRODUCED
    else:
        verdict = VERDICT_PRE_REPRODUCED_POST_FIXED
    return {
        "verdict": verdict,
        "pre_defect": pre_defect,
        "pre_reason": pre_reason,
        "post_defect": post_defect,
        "post_reason": post_reason,
        "pre": pre.to_dict(),
        "post": post.to_dict(),
    }


# --------------------------------------------------------------------------- #
# Contract 4: isolated build cache key
# --------------------------------------------------------------------------- #

def build_cache_key(
    *,
    commit: str,
    platform: str,
    compiler: str,
    cmake_args: list[str] | tuple[str, ...],
    harness_digest: str,
) -> str:
    """Reproducible cache key for a per-SHA isolated server build.

    Any change to the commit, platform, compiler, CMake configure args, or the
    harness (worldmod/fixture) digest yields a new key, so a stale binary is never
    reused across incompatible inputs.
    """

    material = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "commit": commit,
            "platform": platform,
            "compiler": compiler,
            "cmake_args": list(cmake_args),
            "harness_digest": harness_digest,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
