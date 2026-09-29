"""Manifest-bound provider transport settings for formal C1 runs."""

from __future__ import annotations

import contextlib
import math
import os
from collections.abc import Iterator, Mapping
from typing import Any


C1_PROVIDER_TIMEOUT_S = 900.0
C1_PROVIDER_TIMEOUT_ENV_VARS = (
    "WARPTEST_GUI_AGENT_TIMEOUT",
    "WARPTEST_SYNTHESIS_TIMEOUT",
    "WARPTEST_VISUAL_BRIDGE_TIMEOUT",
    "WARPTEST_VISUAL_JUDGE_TIMEOUT",
)


def is_temperature_rejection(error: Exception) -> bool:
    """Return true only for an explicit provider HTTP 400 about temperature."""

    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    # Omitting an experiment parameter changes the effective method, so local
    # TypeError/transport failures that merely mention the word must fail closed.
    if status != 400:
        return False
    text = str(getattr(error, "message", "") or "") + " " + str(error)
    return "temperature" in text.lower()


def manifest_provider_timeout_s(manifest: Mapping[str, Any]) -> float:
    """Return the positive finite C1 provider timeout registered by the manifest."""

    api = manifest.get("api")
    if not isinstance(api, Mapping):
        raise ValueError("Formal C1 manifest must define an api mapping")
    raw = api.get("provider_timeout_s")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("Formal C1 api.provider_timeout_s must be numeric")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Formal C1 api.provider_timeout_s must be positive and finite")
    if value != C1_PROVIDER_TIMEOUT_S:
        raise ValueError(
            f"Formal C1 api.provider_timeout_s must equal {C1_PROVIDER_TIMEOUT_S:.1f}"
        )
    return value


@contextlib.contextmanager
def provider_timeout_environment(timeout_s: float) -> Iterator[None]:
    """Force every formal C1 provider transport to one registered timeout."""

    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError("provider timeout must be numeric")
    value = float(timeout_s)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("provider timeout must be positive and finite")
    rendered = format(value, ".15g")
    previous = {name: os.environ.get(name) for name in C1_PROVIDER_TIMEOUT_ENV_VARS}
    try:
        for name in C1_PROVIDER_TIMEOUT_ENV_VARS:
            os.environ[name] = rendered
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


__all__ = [
    "C1_PROVIDER_TIMEOUT_ENV_VARS",
    "C1_PROVIDER_TIMEOUT_S",
    "is_temperature_rejection",
    "manifest_provider_timeout_s",
    "provider_timeout_environment",
]
