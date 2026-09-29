"""Provider routing for OpenAI-compatible model gateways.

The benchmark talks to several OpenAI-compatible gateways. Historically this was
a single ``base_url`` + ``api_key_env`` per run (ModelScope by default), with the
``:DashScope`` suffix being ModelScope-internal routing text carried inside the
model id. This module adds a lightweight per-model routing layer so a single run
can mix providers, selected by an ``@<provider>`` tag appended to a model id
(e.g. ``gpt-5.4@gateway``). Untagged model ids keep using the run-level default
provider, so existing commands are unchanged.

All providers here are OpenAI-compatible chat-completions endpoints; the gateway
exposes ``/v1/chat/completions`` for OpenAI, Anthropic, and Gemini models
alike, so no protocol-specific clients are required.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from .model_bench import DEFAULT_MODELSCOPE_BASE_URL

DEFAULT_GATEWAY_BASE_URL = "http://gateway.example/v1"

# Tag separator. ``:`` is reserved by ModelScope routing (e.g. ``:DashScope``),
# so ``@`` is used to avoid collisions; no current model id contains ``@``.
PROVIDER_SEPARATOR = "@"


class ProviderError(ValueError):
    """Raised when a model spec references an unknown provider tag."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str
    extra_headers: dict[str, str] = field(default_factory=dict)


def _modelscope_provider() -> ProviderConfig:
    return ProviderConfig(
        name="modelscope",
        base_url=os.environ.get("MODELSCOPE_BASE_URL", DEFAULT_MODELSCOPE_BASE_URL),
        api_key_env="MODELSCOPE_API_KEY",
    )


def _gateway_provider() -> ProviderConfig:
    headers: dict[str, str] = {}
    end_user = os.environ.get("GATEWAY_END_USER")
    custom_source = os.environ.get("GATEWAY_CUSTOM_SOURCE")
    if end_user:
        headers["X-End-User"] = end_user
    if custom_source:
        headers["X-Custom-Source"] = custom_source
    return ProviderConfig(
        name="gateway",
        base_url=os.environ.get("GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE_URL),
        api_key_env="GATEWAY_API_KEY",
        extra_headers=headers,
    )


def _relay_provider() -> ProviderConfig:
    headers: dict[str, str] = {}
    end_user = os.environ.get("RELAY_END_USER")
    if end_user:
        headers["X-End-User"] = end_user
    return ProviderConfig(
        name="relay",
        base_url=os.environ.get("RELAY_BASE_URL", "https://relay.example/v1"),
        api_key_env="RELAY_API_KEY",
        extra_headers=headers,
    )


PROVIDER_BUILDERS: dict[str, Callable[[], ProviderConfig]] = {
    "modelscope": _modelscope_provider,
    "gateway": _gateway_provider,
    "relay": _relay_provider,
}


def available_providers() -> list[str]:
    return sorted(PROVIDER_BUILDERS)


def resolve_model(
    spec: str,
    *,
    default_base_url: str,
    default_api_key_env: str,
) -> tuple[str, ProviderConfig]:
    """Split a ``model[@provider]`` spec into a clean model id and its provider.

    Untagged specs resolve to a ``default`` provider that reuses the run-level
    ``default_base_url``/``default_api_key_env`` (preserving ``:DashScope`` and
    other ModelScope routing carried inside the model id). A tag matching a known
    provider returns that provider's config and strips the tag from the model id.
    """

    spec = spec.strip()
    if PROVIDER_SEPARATOR in spec:
        clean_model, _, tag = spec.rpartition(PROVIDER_SEPARATOR)
        clean_model = clean_model.strip()
        tag = tag.strip()
        if not clean_model:
            raise ProviderError(
                f"Model spec '{spec}' is missing a model id before '{PROVIDER_SEPARATOR}'."
            )
        builder = PROVIDER_BUILDERS.get(tag.lower())
        if builder is None:
            raise ProviderError(
                f"Unknown provider '{tag}' in model spec '{spec}'. "
                f"Known providers: {', '.join(available_providers())}."
            )
        return clean_model, builder()
    return spec, ProviderConfig(
        name="default",
        base_url=default_base_url,
        api_key_env=default_api_key_env,
    )
