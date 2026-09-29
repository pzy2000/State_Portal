"""Stable semantic identities for the C1-Lazarus selection protocol."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True, order=True)
class CandidateKey:
    repository_key: str
    change_number: int

    def __post_init__(self) -> None:
        if type(self.repository_key) is not str or not _NAME.fullmatch(
            self.repository_key
        ):
            raise ValueError("candidate repository key is invalid")
        if type(self.change_number) is not int or self.change_number <= 0:
            raise ValueError("candidate change number is invalid")

    @classmethod
    def from_value(cls, value: Any) -> CandidateKey:
        if type(value) is not dict or set(value) != {
            "repository_key",
            "change_number",
        }:
            raise ValueError("candidate identity schema is invalid")
        return cls(
            repository_key=value["repository_key"],
            change_number=value["change_number"],
        )

    def to_value(self) -> dict[str, Any]:
        return {
            "repository_key": self.repository_key,
            "change_number": self.change_number,
        }


__all__ = ["CandidateKey"]
