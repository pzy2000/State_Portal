from __future__ import annotations

import pytest

from warptest.c1_lazarus_selection.luanti_native_server_closure import (
    classify_server_observation,
)


@pytest.mark.parametrize(
    ("variant", "log", "expected_classification"),
    [
        (
            "fixed",
            b"C1_SERVER_OBSERVER_STARTED\nC1_SERVER_OBSERVER_RESULT apple\n",
            "fixed_accepts_registered_russian_plural_form",
        ),
        (
            "buggy",
            b"C1_SERVER_OBSERVER_STARTED\nInvalid Plural-Forms line: russian\n"
            b"C1_SERVER_OBSERVER_RESULT apple\n",
            "buggy_rejects_registered_russian_plural_form",
        ),
    ],
)
def test_classifies_exact_fixed_buggy_semantics(
    variant: str, log: bytes, expected_classification: str
) -> None:
    result = classify_server_observation(variant, log, b"")

    assert result["semantic_match"] is True
    assert result["semantic_classification"] == expected_classification


def test_requires_both_markers_and_expected_invalid_plural_disposition() -> None:
    fixed = classify_server_observation(
        "fixed",
        b"C1_SERVER_OBSERVER_STARTED\nInvalid Plural-Forms line: russian\n",
        b"",
    )
    buggy = classify_server_observation(
        "buggy", b"C1_SERVER_OBSERVER_STARTED\nC1_SERVER_OBSERVER_RESULT apple\n", b""
    )

    assert fixed["semantic_match"] is False
    assert buggy["semantic_match"] is False


def test_rejects_nil_or_failed_translation_observer_result() -> None:
    nil_result = classify_server_observation(
        "fixed",
        b"C1_SERVER_OBSERVER_STARTED\nC1_SERVER_OBSERVER_RESULT <nil>\n",
        b"",
    )
    call_error = classify_server_observation(
        "buggy",
        b"C1_SERVER_OBSERVER_STARTED\n"
        b"C1_SERVER_OBSERVER_ERROR failed\n"
        b"Invalid Plural-Forms line: russian\n"
        b"C1_SERVER_OBSERVER_RESULT <error>\n",
        b"",
    )

    assert nil_result["semantic_match"] is False
    assert nil_result["nil_result_observed"] is True
    assert call_error["semantic_match"] is False
    assert call_error["observer_call_error_observed"] is True
