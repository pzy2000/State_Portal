"""Source mutation + kill-rate (Stage 3: discrimination validation).

A task is only worth including if its oracle can actually *detect faults* in the
complex region it targets. We validate this by mutating the selected region's
source and measuring the mutation kill rate: a mutant is KILLED if the original
game passes all of the task's execution-grounded oracles but the mutated game
fails at least one. A high kill rate is quantitative evidence that the task
discriminates behaviour in that region (and defends against "trivial/degenerate
task" criticism); a near-zero kill rate flags a task for pruning.

Operators are language-agnostic textual AST-ish mutations restricted to the
selected function's line span, so we only perturb the code the task claims to
exercise (not unrelated code):
  relational swap, arithmetic swap, boolean flip, constant tweak, off-by-one.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Mutant:
    id: str
    operator: str
    line: int  # absolute line number in file
    before: str
    after: str


# Ordered (pattern, replacement, operator-name). Applied one occurrence at a time
# within the target span. Patterns are conservative to avoid corrupting syntax.
_RULES: list[tuple[str, str, str]] = [
    (r"===", "!==", "relational_eq_to_neq"),
    (r"!==", "===", "relational_neq_to_eq"),
    (r"(?<![<>=!])>=", ">", "relational_ge_to_gt"),
    (r"(?<![<>=!])<=", "<", "relational_le_to_lt"),
    (r"(?<![<>=!])>(?!=)", "<", "relational_gt_to_lt"),
    (r"(?<![<>=!])<(?!=)", ">", "relational_lt_to_gt"),
    (r"\+= ", "-= ", "arith_plus_to_minus_assign"),
    (r"\* 2\b", "* 3", "arith_times2_to_times3"),
    (r"\btrue\b", "false", "bool_true_to_false"),
    (r"\bfalse\b", "true", "bool_false_to_true"),
    (r"\b2048\b", "4096", "const_2048_to_4096"),
]


def generate_mutants(source: str, start_line: int, end_line: int, *, max_per_operator: int = 3) -> list[Mutant]:
    """Generate single-point mutants confined to [start_line, end_line] (1-based)."""
    lines = source.splitlines()
    mutants: list[Mutant] = []
    counter = 0
    for pattern, repl, op in _RULES:
        made = 0
        for ln in range(start_line, min(end_line, len(lines)) + 1):
            if made >= max_per_operator:
                break
            text = lines[ln - 1]
            # skip comment lines to reduce no-op mutants
            if text.strip().startswith("//"):
                continue
            m = re.search(pattern, text)
            if not m:
                continue
            new_text = text[: m.start()] + repl + text[m.end():]
            if new_text == text:
                continue
            counter += 1
            made += 1
            mutants.append(
                Mutant(
                    id=f"m{counter:03d}_{op}_L{ln}",
                    operator=op,
                    line=ln,
                    before=text.strip(),
                    after=new_text.strip(),
                )
            )
    return mutants


def _apply_mutant(source: str, mutant: Mutant) -> str:
    lines = source.splitlines(keepends=True)
    idx = mutant.line - 1
    raw = lines[idx]
    newline = "\n" if raw.endswith("\n") else ""
    # re-derive the exact edit on the original (unstripped) line
    for pattern, repl, op in _RULES:
        if op == mutant.operator:
            m = re.search(pattern, raw)
            if m:
                lines[idx] = raw[: m.start()] + repl + raw[m.end():]
                break
    return "".join(lines)


@dataclass
class MutationResult:
    target_file: str
    span: list[int]
    total: int
    killed: int
    survived: int
    errored: int
    kill_rate: float
    details: list[dict] = field(default_factory=list)


def run_mutation_2048(
    *,
    workspace: Path,
    spec_dir: Path | None = None,
    target_file: str = "js/game_manager.js",
    span: tuple[int, int] = (130, 191),  # GameManager.prototype.move
    headless: bool = True,
    max_per_operator: int = 3,
    out_report: Path | None = None,
) -> MutationResult:
    """Mutate the move() region and measure how many mutants the generated
    execution-grounded oracles kill."""
    import json

    import yaml

    from .runner import run_spec

    spec_dir = spec_dir or (workspace / "specs" / "2048" / "generated")
    spec_paths = sorted(spec_dir.glob("*.yaml"))
    if not spec_paths:
        raise FileNotFoundError(f"no generated specs in {spec_dir}")

    orig_repo = (workspace / "games" / "2048").resolve()
    source_path = orig_repo / target_file
    source = source_path.read_text(encoding="utf-8")
    mutants = generate_mutants(source, span[0], span[1], max_per_operator=max_per_operator)

    def _all_pass(repo: Path) -> tuple[bool, list[str]]:
        statuses: list[str] = []
        for sp in spec_paths:
            try:
                report = run_spec(str(sp), repo_arg=str(repo), headless=headless)
                st = (report.to_dict().get("oracle_result") or {}).get("status")
            except Exception as exc:
                st = f"error:{str(exc)[:60]}"
            statuses.append(f"{sp.stem}={st}")
        ok = all(s.endswith("=success") for s in statuses)
        return ok, statuses

    # Baseline: the unmutated game must pass all oracles.
    base_ok, base_status = _all_pass(orig_repo)

    details: list[dict] = []
    killed = survived = errored = 0
    with tempfile.TemporaryDirectory(prefix="wt-mut-2048-") as td:
        mut_repo = Path(td) / "2048"
        for mutant in mutants:
            # fresh copy per mutant (cheap: 2048 is tiny)
            if mut_repo.exists():
                shutil.rmtree(mut_repo)
            shutil.copytree(orig_repo, mut_repo, ignore=shutil.ignore_patterns(".git"))
            (mut_repo / target_file).write_text(_apply_mutant(source, mutant), encoding="utf-8")
            ok, statuses = _all_pass(mut_repo)
            if any(s.startswith(f"{sp.stem}=error") for sp in spec_paths for s in statuses if "=error" in s):
                errored += 1
                verdict = "errored"
            elif not ok:
                killed += 1
                verdict = "killed"
            else:
                survived += 1
                verdict = "survived"
            details.append(
                {"mutant": mutant.id, "operator": mutant.operator, "line": mutant.line,
                 "before": mutant.before, "after": mutant.after, "verdict": verdict, "oracle_status": statuses}
            )

    total = len(mutants)
    denom = killed + survived  # exclude errored (invalid/uncompilable) from the rate
    kill_rate = killed / denom if denom else 0.0
    result = MutationResult(
        target_file=target_file, span=list(span), total=total,
        killed=killed, survived=survived, errored=errored,
        kill_rate=kill_rate, details=details,
    )

    payload = {
        "game": "2048",
        "baseline_all_pass": base_ok,
        "baseline_status": base_status,
        "target_region": {"file": target_file, "span": list(span), "function": "GameManager.prototype.move"},
        "total_mutants": total,
        "killed": killed,
        "survived": survived,
        "errored": errored,
        "kill_rate": round(kill_rate, 4),
        "details": details,
    }
    out_report = out_report or (workspace / "reports" / "mutation" / "2048-move-killrate.json")
    Path(out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(out_report).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return result
