"""Strict, auditable per-model request profiles for formal C1 calls.

The environment variable is deliberately a tiny model-to-profile JSON mapping,
not an arbitrary request-kwargs escape hatch.  This keeps request behavior
reproducible and prevents configuration from injecting unreviewed provider
parameters into paid experiment calls.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


MODEL_REQUEST_PROFILES_ENV = "WARPTEST_C1_MODEL_REQUEST_PROFILES"
MODEL_REQUEST_PROFILE_SCHEMA_VERSION = 1
PROVIDER_DEFAULT = "provider_default"
THINKING_DISABLED = "thinking_disabled"
REASONING_MEDIUM = "reasoning_medium"
REASONING_HIGH = "reasoning_high"
ALLOWED_MODEL_REQUEST_PROFILES = frozenset(
    {PROVIDER_DEFAULT, THINKING_DISABLED, REASONING_MEDIUM, REASONING_HIGH}
)
_MAX_PROFILE_ENV_CHARS = 16_384
_REASONING_EFFORT_NONE_MODELS = frozenset({"gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"})


class ModelRequestProfileError(ValueError):
    """The formal model-request profile environment is invalid."""


@dataclass(frozen=True)
class ModelRequestProfile:
    """One resolved request profile and its canonical audit identity."""

    model: str
    profile: str

    def request_kwargs(self) -> dict[str, Any]:
        if self.profile == THINKING_DISABLED:
            # Relay exposes provider-native no-thinking controls. The GPT
            # route rejects ``thinking.type=disabled`` but accepts the OpenAI
            # compatible ``reasoning_effort=none`` spelling; Gemini and Kimi
            # accept the former.
            if self.model in _REASONING_EFFORT_NONE_MODELS:
                return {"reasoning_effort": "none"}
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        if self.profile == REASONING_MEDIUM:
            return {"reasoning_effort": "medium"}
        if self.profile == REASONING_HIGH:
            return {"reasoning_effort": "high"}
        return {}

    @property
    def reasoning_expected(self) -> bool:
        """Whether this registered profile explicitly enables reasoning."""

        return self.profile in {REASONING_MEDIUM, REASONING_HIGH}

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_REQUEST_PROFILE_SCHEMA_VERSION,
            "model": self.model,
            "profile": self.profile,
            "request_overrides": self.request_kwargs(),
        }

    @property
    def fingerprint(self) -> str:
        return _canonical_hash(self.canonical_payload())

    def provenance(self) -> dict[str, Any]:
        return {
            **self.canonical_payload(),
            "fingerprint": self.fingerprint,
        }


def parse_model_request_profiles(raw: str | None) -> dict[str, str]:
    """Parse the strict JSON mapping, canonicalizing provider-tagged model keys."""

    if raw is None or not raw.strip():
        return {}
    if len(raw) > _MAX_PROFILE_ENV_CHARS:
        raise ModelRequestProfileError(
            f"{MODEL_REQUEST_PROFILES_ENV} exceeds {_MAX_PROFILE_ENV_CHARS} characters"
        )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except ModelRequestProfileError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ModelRequestProfileError(
            f"{MODEL_REQUEST_PROFILES_ENV} must be a JSON object"
        ) from error
    if not isinstance(value, dict):
        raise ModelRequestProfileError(
            f"{MODEL_REQUEST_PROFILES_ENV} must be a JSON object"
        )
    profiles: dict[str, str] = {}
    for raw_model, profile in value.items():
        if not isinstance(raw_model, str):
            raise ModelRequestProfileError("model request profile keys must be strings")
        model = canonical_model_name(raw_model)
        if model in profiles:
            raise ModelRequestProfileError(
                f"duplicate model request profile after provider-tag normalization: {model}"
            )
        if not isinstance(profile, str) or profile not in ALLOWED_MODEL_REQUEST_PROFILES:
            allowed = ", ".join(sorted(ALLOWED_MODEL_REQUEST_PROFILES))
            raise ModelRequestProfileError(
                f"model request profile for {model!r} must be one of: {allowed}"
            )
        profiles[model] = profile
    return dict(sorted(profiles.items()))


def configured_model_request_profiles() -> dict[str, str]:
    return parse_model_request_profiles(os.environ.get(MODEL_REQUEST_PROFILES_ENV))


def canonical_profiles_json(profiles: Mapping[str, str] | None = None) -> str:
    """Serialize a validated mapping for safe propagation to worker processes."""

    selected = (
        configured_model_request_profiles()
        if profiles is None
        else _validated_profile_mapping(profiles)
    )
    return json.dumps(selected, sort_keys=True, separators=(",", ":"))


def resolve_model_request_profile(model: str) -> ModelRequestProfile:
    canonical_model = canonical_model_name(model)
    profile = configured_model_request_profiles().get(
        canonical_model, PROVIDER_DEFAULT
    )
    return ModelRequestProfile(model=canonical_model, profile=profile)


def model_request_profile_provenance(model: str) -> dict[str, Any]:
    """Canonical cache/provenance payload for the effective model profile."""

    return resolve_model_request_profile(model).provenance()


@contextmanager
def model_request_profiles_environment(
    profiles: Mapping[str, str] | None,
) -> Iterator[None]:
    """Temporarily install a validated profile mapping and restore the env exactly.

    ``None`` intentionally removes the variable for the duration of the context.
    On exit, including exceptional exit, an absent variable remains absent and a
    previously present value is restored byte-for-byte without being normalized.
    """

    canonical = None if profiles is None else canonical_profiles_json(profiles)
    was_present = MODEL_REQUEST_PROFILES_ENV in os.environ
    previous = os.environ.get(MODEL_REQUEST_PROFILES_ENV)
    try:
        if canonical is None:
            os.environ.pop(MODEL_REQUEST_PROFILES_ENV, None)
        else:
            os.environ[MODEL_REQUEST_PROFILES_ENV] = canonical
        yield
    finally:
        if was_present:
            assert previous is not None
            os.environ[MODEL_REQUEST_PROFILES_ENV] = previous
        else:
            os.environ.pop(MODEL_REQUEST_PROFILES_ENV, None)


def canonical_model_name(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ModelRequestProfileError("model request profile model must be non-empty")
    stripped = model.strip()
    if stripped != model:
        raise ModelRequestProfileError(
            "model request profile model must not contain surrounding whitespace"
        )
    canonical = stripped.split("@", 1)[0]
    if not canonical:
        raise ModelRequestProfileError("model request profile model must be non-empty")
    return canonical


def _validated_profile_mapping(profiles: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(profiles, Mapping):
        raise ModelRequestProfileError("model request profiles must be a mapping")
    # Reuse the public parser so canonical serialization has exactly one contract.
    return parse_model_request_profiles(
        json.dumps(dict(profiles), sort_keys=True, separators=(",", ":"))
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ModelRequestProfileError(
                f"duplicate key in {MODEL_REQUEST_PROFILES_ENV}: {key}"
            )
        value[key] = item
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ALLOWED_MODEL_REQUEST_PROFILES",
    "MODEL_REQUEST_PROFILES_ENV",
    "MODEL_REQUEST_PROFILE_SCHEMA_VERSION",
    "ModelRequestProfile",
    "ModelRequestProfileError",
    "PROVIDER_DEFAULT",
    "REASONING_HIGH",
    "REASONING_MEDIUM",
    "THINKING_DISABLED",
    "canonical_model_name",
    "canonical_profiles_json",
    "configured_model_request_profiles",
    "model_request_profile_provenance",
    "model_request_profiles_environment",
    "parse_model_request_profiles",
    "resolve_model_request_profile",
]
