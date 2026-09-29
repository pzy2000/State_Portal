#!/usr/bin/env python3
"""Run bounded real API probes for every P2P-only model route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest import cost  # noqa: E402
from warptest.lazarus_p2p_only_recovery import classify_failure  # noqa: E402
from warptest.run_context import safe_error_text  # noqa: E402
from warptest.c1_experiment import relay_environment  # noqa: E402
from warptest.lazarus_calibration import lazarus_provider_identity_sha256  # noqa: E402
from warptest.model_request import (  # noqa: E402
    model_request_profiles_environment,
    resolve_model_request_profile,
)
from warptest.providers import resolve_model  # noqa: E402
from warptest.synthesis import OpenAICompatibleChatClient  # noqa: E402
from warptest.lazarus_p2p_only_identity import (  # noqa: E402
    file_sha256,
    provider_profile_sha256,
)



def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml")
    parser.add_argument("--credentials", type=Path, default=ROOT / "test_model_batch.py")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/api-preflight.json",
    )
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--adopt-current-complete", action="store_true")
    parser.add_argument("--kimi-capacity-history-backoff", action="store_true")
    return parser.parse_args()


def _probe(model_spec: str, *, roles: list[str]) -> dict[str, object]:
    clean_model, provider = resolve_model(
        model_spec,
        default_base_url=os.environ["RELAY_BASE_URL"],
        default_api_key_env="RELAY_API_KEY",
    )
    client = OpenAICompatibleChatClient(
        api_key=os.environ[provider.api_key_env],
        base_url=provider.base_url,
        temperature=0.2,
        max_tokens=4096 if "observer" in roles else 256,
        minimum_max_tokens=4096 if "observer" in roles else 256,
        reasoning_retry_max_tokens=None,
        extra_headers=dict(provider.extra_headers),
        cost_kind="lazarus_p2p_only_preflight",
    )
    result = client.converse(
        clean_model,
        [
            {
                "role": "system",
                "content": "Return exactly the ASCII word OK and nothing else.",
            },
            {"role": "user", "content": "API route readiness probe."},
        ],
        empty_retries=1,
        outage_budget_s=120.0,
    )
    profile = resolve_model_request_profile(clean_model)
    reasoning_content_present = bool(result.reasoning_content)
    sentinel_anomaly = (
        model_spec == "kimi-k2.6@relay"
        and result.reasoning_tokens == 1
        and not reasoning_content_present
        and profile.profile == "thinking_disabled"
    )
    thinking_disabled_verified = bool(
        profile.profile == "thinking_disabled"
        and not reasoning_content_present
        and (result.reasoning_tokens in {0, None} or sentinel_anomaly)
    )
    primary_complete = (
        result.prompt_tokens >= 0
        and result.completion_tokens >= 0
        and result.total_tokens == result.prompt_tokens + result.completion_tokens
        and result.total_tokens > 0
    )
    return {
        "model": model_spec,
        "resolved_model": clean_model,
        "provider": provider.name,
        "roles": roles,
        "complete": bool(result.content.strip()) and primary_complete,
        "response_sha256": hashlib.sha256(result.content.encode("utf-8")).hexdigest(),
        "latency_ms": result.latency_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "reasoning_content_present": reasoning_content_present,
        "thinking_disabled_verified": thinking_disabled_verified,
        "reasoning_accounting_anomaly": (
            "provider_reports_one_reasoning_token_without_reasoning_content"
            if sentinel_anomaly
            else None
        ),
        "request_profile": profile.profile,
        "request_overrides": profile.request_kwargs(),
        "request_profile_fingerprint": profile.fingerprint,
    }


def _measured_probe(model_spec: str, *, roles: list[str]) -> dict[str, object]:
    with cost.scoped_meter() as meter:
        try:
            row = _probe(model_spec, roles=roles)
        except Exception as error:
            error.probe_usage = meter.summary()
            raise
    row["actual_usage"] = meter.summary()
    return row


def main() -> int:
    args = _parse_args()
    from warptest.image_history_backoff import POLICY_ENV, REVISION
    os.environ.pop(POLICY_ENV, None)
    if args.kimi_capacity_history_backoff:
        os.environ[POLICY_ENV] = REVISION
    manifest_path = args.manifest.resolve()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    from warptest.image_history_backoff import validate_manifest_policy
    validate_manifest_policy(manifest, requested=args.kimi_capacity_history_backoff)
    profile_identity = provider_profile_sha256(ROOT, manifest)
    if args.adopt_current_complete:
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") != "c1-lazarus-p2p-only-api-preflight-v1"
            or existing.get("complete") is not True
        ):
            raise SystemExit("only a complete API preflight can be adopted")
        source_sha256 = file_sha256(args.output)
        existing["provider_profile_sha256"] = profile_identity
        existing["identity_migration"] = {
            "kind": "scoped_provider_identity_adoption_v1",
            "source_artifact_sha256": source_sha256,
            "api_calls_reused": True,
        }
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.output)
        print(f"adopted scoped provider identity in {args.output}")
        return 0
    rows: list[dict[str, object]] = []
    route_rows: list[dict[str, object]] = []
    failures: list[str] = []
    resumed_models: dict[str, dict[str, object]] = {}
    resumed_routes: dict[str, dict[str, object]] = {}
    if args.resume is not None:
        previous = json.loads(args.resume.read_text(encoding="utf-8"))
        if previous.get("provider_profile_sha256") == profile_identity:
            for row in previous.get("models") or []:
                if isinstance(row, dict) and _valid_decision_row(row):
                    resumed_models[str(row.get("model"))] = row
            for row in previous.get("fixed_routes") or []:
                if not isinstance(row, dict) or row.get("complete") is not True:
                    continue
                for role in row.get("roles") or []:
                    resumed_routes[str(role)] = row
    provider_identity = ""
    with relay_environment(args.credentials), model_request_profiles_environment(manifest["model_request_profiles"]):
        provider_identity = lazarus_provider_identity_sha256(manifest)
        for model in manifest["models"]:
            if model in resumed_models:
                rows.append({**resumed_models[model], "imported": True})
                continue
            try:
                row = _measured_probe(model, roles=["decision", "synthesis", "matched_grounder"])
                rows.append(row)
                if row["complete"] is not True or row["thinking_disabled_verified"] is not True:
                    failures.append(f"{model}: no-thinking probe is incomplete")
            except Exception as error:  # noqa: BLE001 - persist route failure safely
                failures.append(f"{model}: {safe_error_text(error)}")
                rows.append({"model": model, "complete": False, "failure": classify_failure(error), "actual_usage": getattr(error, "probe_usage", None)})
        for role, model in (
            ("observer", "claude-sonnet-5@relay"),
            ("judge", "qwen3-vl-flash@relay"),
        ):
            if role in resumed_routes:
                route_rows.append({**resumed_routes[role], "imported": True})
                continue
            try:
                row = _measured_probe(model, roles=[role])
                route_rows.append(row)
                if row["complete"] is not True:
                    failures.append(f"{role}: route probe is incomplete")
            except Exception as error:  # noqa: BLE001
                failures.append(f"{role}: {safe_error_text(error)}")
                route_rows.append({"model": model, "roles": [role], "complete": False, "failure": classify_failure(error), "actual_usage": getattr(error, "probe_usage", None)})
    payload = {
        "schema": "c1-lazarus-p2p-only-api-preflight-v1",
        "schema_version": 1,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        "complete": not failures,
        "failures": failures,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "provider_identity_sha256": provider_identity,
        "provider_profile_sha256": profile_identity,
        "models": rows,
        "fixed_routes": route_rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "complete": payload["complete"],
                "models": [
                    {"model": row.get("model"), "complete": row.get("complete")}
                    for row in rows
                ],
                "fixed_routes": [
                    {"roles": row.get("roles"), "complete": row.get("complete")}
                    for row in route_rows
                ],
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if payload["complete"] else 1


def _valid_decision_row(row: dict[str, object]) -> bool:
    model = str(row.get("model") or "")
    if row.get("complete") is not True or row.get("thinking_disabled_verified") is not True:
        return False
    from warptest.model_request import ModelRequestProfile, THINKING_DISABLED, canonical_model_name
    expected = ModelRequestProfile(canonical_model_name(model), THINKING_DISABLED).request_kwargs()
    return row.get("request_overrides") == expected


if __name__ == "__main__":
    raise SystemExit(main())
