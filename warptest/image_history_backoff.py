"""Opt-in, capacity-triggered Kimi screenshot history backoff.

This is an explicitly authorized experimental protocol variation. Every new
logical request starts with its original history; only a proven pre-generation
single-request ceiling rejection permits removing an older image. Text and the
latest image are never removed. Receipts contain hashes, not image payloads.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from .run_context import provider_capacity_failure


REVISION = "kimi-capacity-old-screenshots-v1"
POLICY_ENV = "WARPTEST_IMAGE_HISTORY_BACKOFF"
AUDIT_ENV = "WARPTEST_IMAGE_HISTORY_BACKOFF_AUDIT_ROOT"


def enabled() -> bool:
    return os.environ.get(POLICY_ENV) == REVISION


def protocol() -> dict[str, Any]:
    return {
        "revision": REVISION,
        "model": "kimi-k2.6",
        "scope": "native Agent-S worker and grounding requests",
        "trigger": "explicit single-request TPM ceiling rejection without usage",
        "action": "remove one oldest image per rejected retry",
        "preserve": ["latest image", "all text", "image pixels and resolution"],
        "sticky": False,
        "authorization": "user explicitly approved conditional old-screenshot reduction",
    }


def validate_manifest_policy(manifest: dict, *, requested: bool) -> None:
    """Require explicit CLI agreement with a frozen transport variation."""
    frozen = manifest.get("image_history_backoff")
    if frozen is not None and frozen != protocol():
        raise ValueError("Unrecognized frozen image history backoff protocol")
    if frozen is not None and not requested:
        raise ValueError("Frozen manifest requires --kimi-capacity-history-backoff")


def _images(messages: list[dict]) -> list[tuple[int, int, dict]]:
    return [
        (mi, pi, part)
        for mi, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for pi, part in enumerate(message["content"])
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _image_hash(part: dict) -> str:
    url = part.get("image_url", {}).get("url", "")
    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
        return hashlib.sha256(base64.b64decode(url.split(",", 1)[1], validate=True)).hexdigest()
    return _hash(part)


def create_with_backoff(
    create: Callable[..., Any],
    kwargs: dict[str, Any],
    *,
    extract_error_usage: Callable[[Exception], Any],
    kind: str,
    audit: list[dict],
) -> Any:
    """Update only the caller's private kwargs, never upstream history objects."""
    if not enabled() or kwargs.get("model") != "kimi-k2.6":
        return create(**kwargs)
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        return create(**kwargs)
    original_images = _images(messages)
    if len(original_images) < 2:
        return create(**kwargs)
    # Enabling the variation requires durable evidence even if the worker dies
    # during the retry. Fail before provider access if the audit root is absent.
    root = os.environ.get(AUDIT_ENV)
    if not root or not Path(root).is_dir():
        raise ValueError("capacity history backoff requires an existing audit directory")
    path = Path(root) / f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}.json"
    receipt = {
        **protocol(), "kind": kind, "created_at_ns": time.time_ns(),
        "original_image_count": len(original_images),
        "sent_image_count": len(original_images),
        "latest_image_sha256": _image_hash(original_images[-1][2]),
        "original_messages_sha256": _hash(messages),
        "all_text_sha256": _hash([
            m["content"] if isinstance(m.get("content"), str)
            else [p for p in m.get("content", []) if not isinstance(p, dict) or p.get("type") != "image_url"]
            for m in messages
        ]),
        "rejections": [], "status": "original_request", "receipt_path": str(path),
    }

    def persist() -> None:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(receipt, indent=2) + "\n")
        temp.replace(path)

    while True:
        try:
            response = create(**kwargs)
        except Exception as error:
            proof = provider_capacity_failure(error)
            images = _images(kwargs["messages"])
            if proof is None or extract_error_usage(error) is not None or len(images) <= 1:
                if receipt["rejections"]:
                    receipt.update(status="failed_after_backoff", error_type=type(error).__name__)
                    persist()
                raise
            if not receipt["rejections"]:
                kwargs["messages"] = copy.deepcopy(messages)
                audit.append(receipt)
            mi, pi, part = images[0]
            receipt["rejections"].append({
                "provider_capacity": proof, "rejected_image_count": len(images),
                "dropped_image_sha256": _image_hash(part),
                "original_message_index": mi, "part_index": pi,
            })
            del kwargs["messages"][mi]["content"][pi]
            # Preserve the message and role even when it had no text at all.
            if not kwargs["messages"][mi]["content"]:
                kwargs["messages"][mi]["content"] = ""
            receipt.update(status="retry_pending", sent_image_count=len(images) - 1,
                           sent_messages_sha256=_hash(kwargs["messages"]))
            persist()
        else:
            if receipt["rejections"]:
                receipt["status"] = "response_received"
                persist()
            return response
