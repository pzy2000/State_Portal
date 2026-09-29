"""Run-scoped, immutable shared D/E synthesis with measured costs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import cost
from .lazarus_p2p_only_contract import METHOD_PROTOCOL, SYNTHESIS
from .lazarus_p2p_only_identity import canonical_sha256, file_sha256
from .lazarus_p2p_only_spec import spec_errors
from .model_request import model_request_profile_provenance
from .providers import resolve_model
from .synthesis import SynthesisError, build_synthesis_agent, must_raise_llm_error
from .task_schema import PublicIssue


def public_issue(context: Any) -> PublicIssue:
    # Only this allowlist crosses the coordinator/model boundary.
    text = "\n\n".join(
        f"{name}: {context.task[name]}"
        for name in ("state_description", "preconditions", "reference_steps")
    )
    from .lazarus_p2p_only_contract import public_start_constraint
    constraint = public_start_constraint(context.key.task_id)
    if constraint:
        text += "\n\n" + constraint
    return PublicIssue(
        id=context.key.task_id,
        benchmark=context.task["benchmark"],
        repo=".",
        issue=text,
        tags=["c1", "lazarus", "p2p-only"],
        budget_ms=2400000,
        prompt_revision="lzp-generated-state-v3",
    )


def obtain_shared_synthesis(
    context: Any, *, cache_root: Path, config: dict | None = None
) -> tuple[dict, bool]:
    settings = dict(SYNTHESIS if config is None else config)
    if settings != SYNTHESIS:
        raise ValueError("shared synthesis configuration drift")
    issue = public_issue(context)
    implementation_root = Path(__file__).parent
    identity = {
        "protocol": METHOD_PROTOCOL,
        "issue": issue.public_payload(),
        "seed": context.key.seed,
        "model": context.key.model,
        "request_profile": model_request_profile_provenance(context.key.model),
        "source_sha256": context.agent_source.sanitized_tree_sha256,
        "provider_base_url": os.environ.get("RELAY_BASE_URL", ""),
        "config": settings,
        "implementation": {
            name: file_sha256(implementation_root / name)
            for name in (
                "synthesis.py",
                "lazarus_p2p_only_spec.py",
                "lazarus_p2p_only_schema.py",
                "lazarus_p2p_only_synthesis.py",
                "lazarus_p2p_only_recovery.py",
                "lazarus_p2p_only_generated_runtime.py",
                "lazarus_p2p_only_components.js",
                "lazarus_p2p_only_native.py",
            )
        },
    }
    fingerprint = canonical_sha256(identity)
    directory = cache_root / fingerprint
    entry = directory / "checkpoint.json"
    if entry.exists():
        value = json.loads(entry.read_text())
        checksum = value.pop("entry_sha256", None)
        if checksum != canonical_sha256(value) or value.get("identity") != identity:
            raise ValueError("shared synthesis cache integrity failure")
        if value["usable"] and spec_errors(value["generated_spec"], issue.benchmark):
            raise ValueError("cached generated spec violates current schema")
        return value, True
    if context.key.version == "fixed":
        raise ValueError("fixed D/E cannot synthesize a missing buggy checkpoint")
    failed_entry = directory / "failed-synthesis.json"
    if directory.exists():
        if not failed_entry.is_file():
            raise ValueError("incomplete synthesis cache has no accounted recovery receipt")
        failed = json.loads(failed_entry.read_text())
        checksum = failed.pop("entry_sha256", None)
        if checksum != canonical_sha256(failed) or failed.get("identity") != identity:
            raise ValueError("failed synthesis receipt integrity drift")
        if failed.get("failure", {}).get("retryable") is not True or failed.get("usage", {}).get("usage_complete") is not True:
            raise ValueError("failed synthesis is not safely retryable")
        directory.rename(directory.with_name(fingerprint + ".failed-" + str(time.time_ns())))
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    checkpoint = None
    failure = None
    with cost.scoped_meter() as meter:
        try:
            clean, provider = resolve_model(
                context.key.model,
                default_base_url=os.environ["RELAY_BASE_URL"],
                default_api_key_env="RELAY_API_KEY",
            )
            agent = build_synthesis_agent(
                "llm",
                model=clean,
                base_url=provider.base_url,
                api_key_env=provider.api_key_env,
                temperature=settings["temperature"],
                max_tokens=settings["max_tokens"],
                explore=settings["explore"],
                max_steps=settings["max_explore_steps"],
                extra_headers=provider.extra_headers,
            )
            checkpoint = agent.synthesize(
                issue, context.agent_source.root, artifact_dir=directory / "artifacts"
            )
        except Exception as error:
            if not isinstance(error, SynthesisError) or must_raise_llm_error(error):
                from .lazarus_p2p_only_recovery import atomic_receipt, classify_failure
                receipt = {"identity": identity, "failure": classify_failure(error),
                    "usage": meter.summary(), "duration_ms": int((time.monotonic() - started) * 1000)}
                if receipt["usage"].get("usage_complete") is False:
                    receipt["failure"] = {"retryable": False, "category": "usage_unknown", "error": "synthesis usage is incomplete"}
                atomic_receipt(directory / "failed-synthesis.json", {**receipt, "entry_sha256": canonical_sha256(receipt)})
                atomic_receipt(directory / "artifacts/cost-ledger.json", {"usage": receipt["usage"]})
                raise
            failure = f"{type(error).__name__}: {error}"
    usage = meter.summary()
    (directory / "artifacts").mkdir(exist_ok=True)
    (directory / "artifacts/cost-ledger.json").write_text(
        json.dumps({"usage": usage}, indent=2)
    )
    if usage.get("usage_complete") is False:
        raise cost.UsageAccountingError("shared synthesis usage is incomplete")
    generated = getattr(checkpoint, "model_generated_spec", None)
    errors = spec_errors(generated, issue.benchmark)
    if getattr(checkpoint, "used_public_scaffold", False):
        errors.append("public scaffold substitution is forbidden")
    if getattr(checkpoint, "hidden_files_read", []):
        errors.append("private input contamination")
    failure = failure or ("; ".join(errors) if errors else None)
    value = {
        "identity": identity,
        "synthesis_id": fingerprint,
        "producer": context.key.invocation_id(),
        "generated_spec": generated,
        "usable": failure is None,
        "failure": failure,
        "checkpoint": checkpoint.to_dict() if checkpoint is not None else None,
        "checkpoint_sha256": canonical_sha256(
            generated if generated is not None else {"failure": failure}
        ),
        "usage": usage,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "artifact_dir": str(directory / "artifacts"),
    }
    temporary = directory / "checkpoint.tmp"
    temporary.write_text(
        json.dumps(
            {**value, "entry_sha256": canonical_sha256(value)}, indent=2, sort_keys=True
        )
        + "\n"
    )
    temporary.replace(entry)
    return value, False
