from __future__ import annotations

from unittest.mock import patch

import pytest

from warptest import run_context


def test_retry_history_and_progress_redact_runtime_credentials() -> None:
    events: list[str] = []
    secret = "c1-super-secret-provider-token"

    def failing() -> None:
        raise TimeoutError(f"request Bearer {secret} timed out api_key={secret}")

    with patch.dict("os.environ", {"RELAY_API_KEY": secret}, clear=False):
        with pytest.raises(TimeoutError) as raised:
            run_context.call_with_retries(
                failing,
                max_attempts=1,
                sleep=lambda _seconds: None,
                on_event=events.append,
            )

    history = getattr(raised.value, "_p5_retry_history")
    serialized = repr(history) + repr(events)
    assert secret not in serialized
    assert "<redacted>" in serialized
