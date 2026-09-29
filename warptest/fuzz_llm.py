"""LLM-driven evolutionary state fuzzing for the C3 robustness benchmark.

This is a TitanFuzz-style port to *deep-state* fuzzing. Instead of hand-written,
per-benchmark mutations (the old ``fuzzing.mutate_spec``), the fuzzer:

1. Seed generation (generative LLM, TitanFuzz Section 3.1): given the benchmark, a
   known-good target deep state, and a schema hint, an LLM generates several diverse
   boundary/adversarial seed states with a step-by-step prompt.
2. Mutation (infilling LLM, Section 3.2.1/3.2.3): a mutation operator masks part of a
   seed state with a ``"<MASK>"`` sentinel and the LLM infills adversarial values.
3. Operator selection (Section 3.2.2): a Beta-Bernoulli multi-armed bandit with
   Thompson Sampling prioritizes operators that produce injectable, unique mutants.
4. Fitness (Section 3.2.4): a ``D + U - R`` score over the state (nesting depth,
   unique perturbed field paths, minus repeated-value penalty) ranks seeds; the
   evolutionary loop selects seeds via softmax over the top-N.

The oracle (defect / robust / rejected) and unique-defect fingerprinting are reused
from :mod:`warptest.fuzzing`; this module only owns *generation*.

No offline / rule-based fallback exists on purpose: if the LLM is unavailable (no API
key, client construction failure), the fuzzer raises immediately so the experiment
never runs on fake mutations that would mask an LLM-side problem.
"""
from __future__ import annotations

import copy
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cost
from .providers import resolve_model
from .synthesis import (
    DEFAULT_MODELSCOPE_BASE_URL,
    DEFAULT_SYNTHESIS_MODEL,
    OpenAICompatibleChatClient,
    SynthesisError,
)

# Mutation operators. Each is the JSON-state analog of a TitanFuzz code operator.
OP_FIELD_VALUE = "field_value"  # ~ argument-replacement: mask one leaf value
OP_FIELD_INSERT = "field_insert"  # ~ keyword-insertion: add one unexpected key
OP_SUBTREE = "subtree"  # ~ prefix/suffix: regenerate a nested object/array region
OP_TYPE_FLIP = "type_flip"  # ~ method: flip a value's type or swap an enum/id

MUTATION_OPERATORS: tuple[str, ...] = (
    OP_FIELD_VALUE,
    OP_FIELD_INSERT,
    OP_SUBTREE,
    OP_TYPE_FLIP,
)
JYNEW_MUTATION_OPERATORS: tuple[str, ...] = (
    OP_FIELD_VALUE,
    OP_SUBTREE,
    OP_TYPE_FLIP,
)

OPERATOR_NOTES: dict[str, str] = {
    OP_FIELD_VALUE: "field-value replacement (boundary/invalid leaf value)",
    OP_FIELD_INSERT: "unexpected extra key insertion",
    OP_SUBTREE: "nested subtree regeneration",
    OP_TYPE_FLIP: "type flip / invalid enum or id",
}

MASK = "<MASK>"

_SYSTEM_PROMPT = (
    "You are a fuzzing engine that perturbs game deep-state snapshots to find "
    "robustness bugs. You always return exactly one JSON object and nothing else "
    "(no prose, no markdown fences)."
)


# --------------------------------------------------------------------------- #
# JSON structure helpers
# --------------------------------------------------------------------------- #

def _leaf_paths(obj: Any, prefix: tuple = ()) -> list[tuple]:
    """Paths to scalar leaves (and empty containers) in a nested JSON value."""

    paths: list[tuple] = []
    if isinstance(obj, dict):
        if not obj:
            return [prefix]
        for key, value in obj.items():
            paths.extend(_leaf_paths(value, prefix + (key,)))
    elif isinstance(obj, list):
        if not obj:
            return [prefix]
        for index, value in enumerate(obj):
            paths.extend(_leaf_paths(value, prefix + (index,)))
    else:
        paths.append(prefix)
    return paths


def _container_paths(obj: Any, prefix: tuple = ()) -> list[tuple]:
    """Paths to non-empty dict/list nodes (excluding the root)."""

    paths: list[tuple] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)) and value:
                paths.append(prefix + (key,))
                paths.extend(_container_paths(value, prefix + (key,)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            if isinstance(value, (dict, list)) and value:
                paths.append(prefix + (index,))
                paths.extend(_container_paths(value, prefix + (index,)))
    return paths


def _get_path(obj: Any, path: tuple) -> Any:
    cur = obj
    for key in path:
        cur = cur[key]
    return cur


def _set_path(obj: Any, path: tuple, value: Any) -> None:
    if not path:
        return
    cur = obj
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value


def _depth(obj: Any) -> int:
    if isinstance(obj, dict):
        return 1 + max((_depth(v) for v in obj.values()), default=0)
    if isinstance(obj, list):
        return 1 + max((_depth(v) for v in obj), default=0)
    return 0


def state_signature(target: Any) -> str:
    """Stable canonical signature of a deep-state target for dedup."""

    return json.dumps(target, sort_keys=True, ensure_ascii=False, default=str)


def _describe_structure(obj: Any, prefix: str = "") -> list[str]:
    """A compact schema hint: ``path: type`` lines for the known-good state."""

    lines: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                lines.append(f"{path}: {type(value).__name__}")
                lines.extend(_describe_structure(value, path))
            else:
                lines.append(f"{path}: {type(value).__name__} = {value!r}")
    elif isinstance(obj, list):
        path = f"{prefix}[]"
        if obj:
            lines.append(f"{path}: list[{len(obj)}]")
            lines.extend(_describe_structure(obj[0], path))
    return lines[:60]


# --------------------------------------------------------------------------- #
# Fitness (TitanFuzz Section 3.2.4: D + U - R)
# --------------------------------------------------------------------------- #

def fitness(target: Any, seed_target: Any | None = None) -> float:
    """Score a state by structural richness relative to the known-good seed.

    * ``D`` -- max nesting depth of the state (deeper == more chained structure).
    * ``U`` -- number of unique leaf field paths that differ from the seed (or all
      leaves when no seed is given); more perturbed dimensions == more diverse.
    * ``R`` -- repeated-value penalty: leaves minus distinct leaf values, so states
      padded with identical repeated values are de-prioritized.
    """

    depth = _depth(target)
    leaves = _leaf_paths(target)
    leaf_values = [state_signature(_get_path(target, p)) for p in leaves]
    distinct = len(set(leaf_values))
    repeated = len(leaf_values) - distinct

    if seed_target is None:
        unique = len(leaves)
    else:
        unique = 0
        for path in leaves:
            try:
                seed_value = _get_path(seed_target, path)
            except (KeyError, IndexError, TypeError):
                unique += 1
                continue
            if state_signature(_get_path(target, path)) != state_signature(seed_value):
                unique += 1
    return float(depth + unique - repeated)


# --------------------------------------------------------------------------- #
# Multi-armed bandit operator selection (Thompson Sampling over Beta-Bernoulli)
# --------------------------------------------------------------------------- #

@dataclass
class MABState:
    """Per-operator Beta(S, F) posterior, re-initialized per task."""

    rng: random.Random
    operators: tuple[str, ...] = MUTATION_OPERATORS
    success: dict[str, int] = field(default_factory=dict)
    failure: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for op in self.operators:
            self.success.setdefault(op, 1)
            self.failure.setdefault(op, 1)

    def select(self) -> str:
        best_op = self.operators[0]
        best_theta = -1.0
        for op in self.operators:
            theta = self.rng.betavariate(self.success[op], self.failure[op])
            if theta > best_theta:
                best_theta = theta
                best_op = op
        return best_op

    def update(self, op: str, n_valid: int, n_invalid: int) -> None:
        self.success[op] = self.success.get(op, 1) + max(0, int(n_valid))
        self.failure[op] = self.failure.get(op, 1) + max(0, int(n_invalid))

    def stats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for op in self.operators:
            s = self.success[op]
            f = self.failure[op]
            out[op] = {
                "success": s - 1,
                "failure": f - 1,
                "posterior_mean": round(s / (s + f), 4),
            }
        return out


# --------------------------------------------------------------------------- #
# Seed bank
# --------------------------------------------------------------------------- #

@dataclass
class SeedEntry:
    target: dict[str, Any]
    fitness: float
    signature: str
    provenance: str  # oracle_seed | llm_seed | mutant
    generation: int


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def _benchmark_prompt_guidance(benchmark: str) -> str:
    if benchmark.lower() != "jynew":
        return ""
    return (
        "\n\nJynew-specific guidance: keep the exact C3 fuzz-state schema whenever "
        "possible so the candidate reaches the Unity warm session rather than being "
        "filtered by the Python schema guard. The exact top-level keys are "
        "player_level:int, money:int, map_id:int, team_ids:list[int], "
        "items:list[{id:int,count:int}], skills:list[{id:int,level:int}], and "
        "key_values:list[{key:safe-string,value:string}]. Prefer player-visible or "
        "gameplay-impacting perturbations: extreme but finite levels/money, negative "
        "or unknown role/item/skill/map ids, duplicate or empty party/items/skills, "
        "large item counts, contradictory map/team/item/skill combinations, and odd "
        "quest key/value combinations. Avoid unsupported extra keys, non-finite "
        "numbers, markdown, comments, or prose unless the current operator explicitly "
        "asks for an inserted key."
    )


def _priors_block(priors_hint: str) -> str:
    """Structured historical-bug guidance for prompts. Never the raw PR body/diff."""

    return f"\n\n{priors_hint}\n" if priors_hint else ""


def build_seed_prompt(
    benchmark: str,
    seed_target: dict[str, Any],
    schema_hint: str,
    priors_hint: str = "",
) -> str:
    good = json.dumps(seed_target, ensure_ascii=False, indent=2)
    guidance = _benchmark_prompt_guidance(benchmark)
    return (
        f"We fuzz the deep-state robustness of the '{benchmark}' game by injecting "
        f"perturbed state snapshots and checking whether the game crashes, renders "
        f"blank, emits console errors, or soft-locks.\n\n"
        f"Here is a known-good target deep state (the game handles it correctly):\n"
        f"```json\n{good}\n```\n\n"
        f"State field schema (path: type):\n{schema_hint}\n\n"
        f"{guidance}"
        f"{_priors_block(priors_hint)}\n\n"
        f"Task 1: Understand the state schema above.\n"
        f"Task 2: Pick ONE boundary or invalid dimension likely to stress robustness "
        f"(e.g. extreme/overflow numbers, negative where non-negative is expected, "
        f"wrong types, contradictory flags, unknown enum/id values, oversized arrays).\n"
        f"Task 3: Output a SINGLE JSON object with the SAME top-level shape as the "
        f"known-good state but with your chosen perturbation applied. Keep it a "
        f"plausible-but-adversarial snapshot (not random garbage). Return ONLY the JSON."
    )


def build_mutation_prompt(
    benchmark: str,
    masked_target: dict[str, Any],
    op: str,
    *,
    insert_hint: bool = False,
    priors_hint: str = "",
) -> str:
    masked = json.dumps(masked_target, ensure_ascii=False, indent=2)
    guidance = _benchmark_prompt_guidance(benchmark)
    if op == OP_FIELD_INSERT and insert_hint:
        instruction = (
            "Add exactly ONE new, unexpected top-level key with an adversarial value "
            "(a key the game does not expect). Keep all existing fields unchanged."
        )
    elif op == OP_TYPE_FLIP:
        instruction = (
            f'Replace every "{MASK}" with a value of a DIFFERENT type than the field '
            f"normally holds, or an invalid enum/id string. Keep all other fields."
        )
    elif op == OP_SUBTREE:
        instruction = (
            f'Replace every "{MASK}" with a regenerated nested structure that is a '
            f"boundary/adversarial variant (e.g. oversized, malformed, or extreme). "
            f"Keep all other fields."
        )
    else:  # OP_FIELD_VALUE
        instruction = (
            f'Replace every "{MASK}" with a boundary or invalid value (extreme/overflow '
            f"number, negative, wrong-magnitude, or otherwise adversarial). Keep all "
            f"other fields unchanged."
        )
    return (
        f"We fuzz the deep-state robustness of the '{benchmark}' game. Below is a state "
        f'snapshot where some values are masked with "{MASK}".\n'
        f"```json\n{masked}\n```\n\n"
        f"{guidance}"
        f"{_priors_block(priors_hint)}\n\n"
        f"{instruction}\n"
        f"Output the SINGLE complete JSON object with all masks filled in. Return ONLY "
        f"the JSON, no prose, no markdown fences."
    )


# --------------------------------------------------------------------------- #
# Robust JSON extraction from model output
# --------------------------------------------------------------------------- #

def _sanitize_surrogates(value: Any) -> Any:
    """Recursively drop lone UTF-16 surrogates from strings/keys in a parsed value.

    The generation LLM occasionally emits invalid escapes (e.g. ``"\\ud800"``) that
    become lone surrogates in the parsed object; those cannot be UTF-8 encoded, so
    left in place they crash serialization/fingerprinting and would abort the whole
    fuzz task on a single adversarial candidate. Replacing them keeps every candidate
    JSON/UTF-8 safe while still preserving its (weird, adversarial) shape.
    """

    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {_sanitize_surrogates(k): _sanitize_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_surrogates(item) for item in value]
    return value


# i32-range sentinel used to clamp non-finite numbers (see _sanitize_non_finite).
# Matches the coordinate/count magnitude most game engines already treat as an
# extreme-but-representable boundary value, so clamping still exercises the
# same "huge number" adversarial intent the fuzzer was going for.
_FINITE_CLAMP = 2_147_483_647.0


def _sanitize_non_finite(value: Any) -> Any:
    """Recursively clamp non-finite floats (``Infinity``/``-Infinity``/``NaN``) to
    finite sentinels.

    ``json.loads`` is a Python-only extension that accepts the bare literals
    ``Infinity``/``-Infinity``/``NaN`` even though they are not valid JSON (a
    generation LLM emits them fairly often when asked for "extreme/overflow"
    values, since that mirrors common JS/Python idioms). Left in place, a single
    non-finite leaf makes the *entire* candidate unparseable once it is
    re-serialized for a strict downstream JSON consumer (e.g. the game engine's
    own parser rejects the whole document, not just the bad field) -- which
    manifests as an opaque whole-payload rejection or a wasted round-trip
    timeout instead of a targeted boundary-value test. Clamping to a large
    finite sentinel preserves the adversarial intent (still an extreme value)
    while guaranteeing every candidate is valid, round-trippable JSON.
    """

    if isinstance(value, float):
        if math.isnan(value):
            return 0.0
        if math.isinf(value):
            return _FINITE_CLAMP if value > 0 else -_FINITE_CLAMP
        return value
    if isinstance(value, dict):
        return {k: _sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(v) for v in value]
    return value


def parse_json(text: str) -> dict[str, Any] | None:
    """Extract the first top-level JSON object from a model response, or None."""

    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop a leading ```json / ``` fence and any trailing fence.
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = cleaned[start : index + 1]
                try:
                    parsed = json.loads(blob)
                except (ValueError, TypeError):
                    return None
                if not isinstance(parsed, dict):
                    return None
                return _sanitize_non_finite(_sanitize_surrogates(parsed))
    return None


# --------------------------------------------------------------------------- #
# Fuzzer
# --------------------------------------------------------------------------- #

@dataclass
class InjectResult:
    """Verdict of injecting one candidate state (returned by the caller's inject_fn)."""

    verdict: str  # robust | defect | rejected
    defect_type: str | None = None
    detail: str = ""
    screenshot: str | None = None


class LLMStateFuzzer:
    """Owns the LLM client, MAB, seed bank, and generation logic for one task.

    Construction fails loudly if the LLM cannot be reached (missing key / client
    construction error), so a run never silently degrades to non-LLM behavior.
    """

    def __init__(
        self,
        *,
        benchmark: str,
        model: str = DEFAULT_SYNTHESIS_MODEL,
        api_key_env: str = "MODELSCOPE_API_KEY",
        default_base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
        temperature: float = 0.9,
        max_tokens: int = 2048,
        reasoning_effort: str | None = "medium",
        rng: random.Random | None = None,
        client: Any | None = None,
        priors: Any | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.rng = rng or random.Random()
        # Structured, issue-guided priors (NormalizedPriors) or None. The single
        # entry point (FuzzConfig -> normalize -> here) keeps generate_seeds /
        # run_evo_fuzz free of their own priors parameters.
        self.priors = priors
        self.priors_hint = priors.hint() if priors else ""
        clean_model, provider = resolve_model(
            model, default_base_url=default_base_url, default_api_key_env=api_key_env
        )
        self.model = clean_model
        self.provider_name = provider.name
        self.reasoning_effort = reasoning_effort or None
        import os

        api_key = os.environ.get(provider.api_key_env, "")
        # No fallback: OpenAICompatibleChatClient raises SynthesisError on empty key.
        self.client = OpenAICompatibleChatClient(
            api_key=api_key,
            base_url=provider.base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=provider.extra_headers or None,
            reasoning_effort=self.reasoning_effort,
            client=client,
        )
        operators = JYNEW_MUTATION_OPERATORS if benchmark.lower() == "jynew" else MUTATION_OPERATORS
        self.mab = MABState(rng=self.rng, operators=operators)
        self.seen: set[str] = set()
        self.seed_target: dict[str, Any] | None = None

    # -- LLM calls --------------------------------------------------------- #

    def _chat(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        result = self.client.converse(self.model, messages)
        return result.content

    # -- seed generation (Section 3.1) ------------------------------------- #

    def generate_seeds(
        self,
        seed_target: dict[str, Any],
        n: int,
        *,
        log: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        self.seed_target = copy.deepcopy(seed_target)
        schema_hint = "\n".join(_describe_structure(seed_target))
        prompt = build_seed_prompt(self.benchmark, seed_target, schema_hint, self.priors_hint)
        seeds: list[dict[str, Any]] = []
        total = max(0, n)
        for index in range(total):
            if log:
                log(f"seed {index + 1}/{total}: calling LLM...")
            parsed = parse_json(self._chat(prompt))
            ok = isinstance(parsed, dict) and bool(parsed)
            if ok:
                seeds.append(parsed)
            if log:
                log(f"seed {index + 1}/{total}: {'parsed' if ok else 'unparseable (skipped)'}")
        return seeds

    # -- mutation (Section 3.2.1 / 3.2.3) ---------------------------------- #

    def mask(self, target: dict[str, Any], op: str) -> tuple[dict[str, Any], bool]:
        """Produce a masked copy of ``target`` for operator ``op``.

        Returns ``(masked_target, insert_hint)``; ``insert_hint`` signals the
        field-insert operator (no in-place mask, the LLM adds a new key).
        """

        masked = copy.deepcopy(target)
        if op == OP_FIELD_INSERT:
            return masked, True
        if op == OP_SUBTREE:
            containers = _container_paths(masked)
            if containers:
                path = self.rng.choice(containers)
                _set_path(masked, path, MASK)
                return masked, False
            # Fall back to leaf masking if the state is flat.
        # OP_FIELD_VALUE / OP_TYPE_FLIP / flat subtree: mask one leaf value.
        leaves = [p for p in _leaf_paths(masked) if p]
        if leaves:
            path = self.rng.choice(leaves)
            _set_path(masked, path, MASK)
        return masked, False

    def prior_seeds(self, seed_target: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        """Deterministic, provenance-tagged seeds distilled from historical bug fixes."""

        from .fuzz_priors import materialize_prior_seeds

        return materialize_prior_seeds(seed_target, self.priors)

    def infill(self, masked: dict[str, Any], op: str, n: int, *, insert_hint: bool) -> list[dict[str, Any]]:
        prompt = build_mutation_prompt(
            self.benchmark, masked, op, insert_hint=insert_hint, priors_hint=self.priors_hint
        )
        out: list[dict[str, Any]] = []
        for _ in range(max(1, n)):
            parsed = parse_json(self._chat(prompt))
            if isinstance(parsed, dict) and parsed:
                out.append(parsed)
        return out

    # -- seed selection (Section 3.2, softmax over top-N) ------------------ #

    def select_seed(self, bank: list[SeedEntry], top_n: int) -> SeedEntry:
        ranked = sorted(bank, key=lambda e: e.fitness, reverse=True)[: max(1, top_n)]
        scores = [e.fitness for e in ranked]
        max_score = max(scores)
        weights = [math.exp(s - max_score) for s in scores]
        total = sum(weights) or 1.0
        pick = self.rng.random() * total
        acc = 0.0
        for entry, weight in zip(ranked, weights):
            acc += weight
            if pick <= acc:
                return entry
        return ranked[-1]


# --------------------------------------------------------------------------- #
# Evolutionary fuzzing loop (TitanFuzz Algorithm 1)
# --------------------------------------------------------------------------- #

def run_evo_fuzz(
    *,
    benchmark: str,
    seed_target: dict[str, Any],
    inject_fn: Callable[[dict[str, Any], str, str], InjectResult],
    fuzzer: LLMStateFuzzer,
    budget_s: float,
    num_seeds: int,
    samples_per_op: int,
    top_n: int,
    make_record: Callable[..., dict[str, Any]],
    log: Callable[[str], None] | None = None,
    heartbeat_s: float = 15.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the LLM-driven evolutionary state fuzz for one task.

    ``inject_fn(candidate_target, operator_id, screenshot_name)`` injects a candidate
    deep state and returns an :class:`InjectResult`. ``make_record`` builds the final
    mutation record dict (with fingerprint) from the candidate + verdict fields; it is
    supplied by the caller so browser and engine paths stay in :mod:`warptest.fuzzing`.

    Returns ``(records, meta)`` where ``meta`` carries MAB stats and generation counts.
    """

    records: list[dict[str, Any]] = []
    _log = log or (lambda _msg: None)
    defect_count = [0]

    def _consider(
        target: dict[str, Any],
        *,
        operator_id: str,
        provenance: str,
        generation: int,
        counter: list[int],
    ) -> InjectResult | None:
        sig = state_signature(target)
        if sig in fuzzer.seen:
            return None  # not unique -> counts as a failure for the operator
        fuzzer.seen.add(sig)
        idx = counter[0]
        counter[0] += 1
        screenshot_name = f"fuzz-{operator_id}-{generation}-{idx}.png"
        result = inject_fn(target, operator_id, screenshot_name)
        record = make_record(
            target=target,
            operator_id=operator_id,
            provenance=provenance,
            generation=generation,
            fitness=fitness(target, seed_target),
            result=result,
        )
        records.append(record)
        if result.verdict == "defect":
            defect_count[0] += 1
        return result

    counter = [0]

    # Generation 0: evaluate the oracle seed and the LLM-generated seeds; keep the
    # injectable ones in the bank for later mutation.
    bank: list[SeedEntry] = []

    def _admit(target: dict[str, Any], provenance: str, result: InjectResult | None) -> None:
        if result is None or result.verdict == "rejected":
            return
        sig = state_signature(target)
        bank.append(
            SeedEntry(
                target=copy.deepcopy(target),
                fitness=fitness(target, seed_target),
                signature=sig,
                provenance=provenance,
                generation=0,
            )
        )

    _log("evaluating oracle seed...")
    oracle_result = _consider(
        seed_target, operator_id="oracle_seed", provenance="oracle_seed", generation=0, counter=counter
    )
    _admit(seed_target, "oracle_seed", oracle_result)

    # Prior seeds come AFTER the oracle seed and BEFORE the LLM seeds (fixed order:
    # oracle -> prior -> llm) so the bank always anchors on the known-good state,
    # then the structured, issue-guided perturbations, then the LLM's exploration.
    prior_seed_defs = fuzzer.prior_seeds(seed_target) if getattr(fuzzer, "priors", None) else []
    if prior_seed_defs:
        _log(f"injecting {len(prior_seed_defs)} structured prior seed(s) from historical bug fixes...")
    num_prior_seeds_admitted = 0
    for prior_target, provenance in prior_seed_defs:
        prior_result = _consider(
            prior_target, operator_id="prior_seed", provenance=provenance, generation=0, counter=counter
        )
        if prior_result is not None and prior_result.verdict != "rejected":
            num_prior_seeds_admitted += 1
        _admit(prior_target, provenance, prior_result)

    _log(f"generating {num_seeds} LLM seed states (setup, not counted against budget)...")
    llm_seeds = fuzzer.generate_seeds(seed_target, num_seeds, log=lambda m: _log("  " + m))
    for seed in llm_seeds:
        seed_result = _consider(
            seed, operator_id="llm_seed", provenance="llm_seed", generation=0, counter=counter
        )
        _admit(seed, "llm_seed", seed_result)
    _log(f"seed phase done: bank={len(bank)}, injected={counter[0]}, defects={defect_count[0]}")

    # The oracle seed always anchors the bank so the loop can proceed even if every
    # LLM seed was a duplicate/rejected (still deep-mutated from the good state).
    if not bank:
        bank.append(
            SeedEntry(
                target=copy.deepcopy(seed_target),
                fitness=fitness(seed_target, seed_target),
                signature=state_signature(seed_target),
                provenance="oracle_seed",
                generation=0,
            )
        )

    # The budget governs the mutation rounds; seed generation above is amortized
    # setup so a short budget still runs the evolutionary loop at least once.
    _log(f"mutation loop starting (budget {budget_s:.0f}s)...")
    generation = 1
    start = time.perf_counter()
    last_beat = start
    while time.perf_counter() - start < budget_s:
        entry = fuzzer.select_seed(bank, top_n)
        op = fuzzer.mab.select()
        masked, insert_hint = fuzzer.mask(entry.target, op)
        candidates = fuzzer.infill(masked, op, samples_per_op, insert_hint=insert_hint)

        n_valid = 0
        n_invalid = 0
        if not candidates:
            # The LLM produced no parseable candidate this round: pure operator failure.
            n_invalid += max(1, samples_per_op)
        for candidate in candidates:
            result = _consider(
                candidate,
                operator_id=op,
                provenance="mutant",
                generation=generation,
                counter=counter,
            )
            if result is None:
                n_invalid += 1  # duplicate
                continue
            if result.verdict in {"robust", "defect"}:
                n_valid += 1
                bank.append(
                    SeedEntry(
                        target=copy.deepcopy(candidate),
                        fitness=fitness(candidate, seed_target),
                        signature=state_signature(candidate),
                        provenance="mutant",
                        generation=generation,
                    )
                )
            else:  # rejected
                n_invalid += 1

        fuzzer.mab.update(op, n_valid, n_invalid)
        # Per-round heartbeat, throttled so fast (cached/duplicate) rounds do not
        # flood the console but a slow round still shows the loop is alive.
        now = time.perf_counter()
        if log and (now - last_beat >= heartbeat_s or n_valid or n_invalid == 0):
            last_beat = now
            _log(
                f"gen {generation} op={op} +{n_valid}valid/{n_invalid}invalid | "
                f"injected={counter[0]} defects={defect_count[0]} bank={len(bank)} "
                f"elapsed={now - start:.0f}/{budget_s:.0f}s"
            )
        generation += 1

    _log(
        f"mutation loop done: generations={generation - 1} injected={counter[0]} "
        f"defects={defect_count[0]}"
    )

    meta = {
        "model": fuzzer.model,
        "provider": fuzzer.provider_name,
        "num_oracle_seeds": 1,
        "num_prior_seeds": len(prior_seed_defs),
        "num_prior_seeds_admitted": num_prior_seeds_admitted,
        "num_llm_seeds_generated": len(llm_seeds),
        "num_seeds_requested": num_seeds,
        "generations": generation - 1,
        "candidates_injected": counter[0],
        "seed_bank_size": len(bank),
        "operator_mab": fuzzer.mab.stats(),
        "budget_s": budget_s,
    }
    priors = getattr(fuzzer, "priors", None)
    if priors:
        meta["priors"] = {
            "source_path": getattr(priors, "source_path", None),
            "sha256": getattr(priors, "sha256", None),
            "recipe_ids": getattr(priors, "recipe_ids", []),
        }
    return records, meta
