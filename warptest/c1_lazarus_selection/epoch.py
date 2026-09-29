"""Explicit selection epochs for stable C1 screening artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .stable_protocol import ArtifactReference


@dataclass(frozen=True)
class SelectionEpoch:
    """A protocol generation with its isolated stable-ID namespace."""

    label: Literal["v1", "v2"]
    semantic_version: Literal["1.0", "2.0"]

    def __post_init__(self) -> None:
        if (self.label, self.semantic_version) not in {
            ("v1", "1.0"),
            ("v2", "2.0"),
        }:
            raise ValueError("selection epoch/version pairing is invalid")

    @property
    def is_v2(self) -> bool:
        return self.label == "v2"

    def enumeration_reference(self, repository_key: str) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=(
                f"repository_enumeration/{repository_key}/current/{self.label}"
            ),
            artifact_kind="repository_enumeration",
            semantic_version=self.semantic_version,
        )


SELECTION_EPOCH_V1 = SelectionEpoch("v1", "1.0")
SELECTION_EPOCH_V2 = SelectionEpoch("v2", "2.0")


def require_selection_epoch(value: SelectionEpoch) -> SelectionEpoch:
    """Reject untyped epoch names before they can select an artifact namespace."""

    if type(value) is not SelectionEpoch:
        raise TypeError("selection epoch must be typed")
    return value


__all__ = [
    "SELECTION_EPOCH_V1",
    "SELECTION_EPOCH_V2",
    "SelectionEpoch",
    "require_selection_epoch",
]
