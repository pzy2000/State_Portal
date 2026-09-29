"""Fail-closed integrity checks for C1 oracle-golden artifacts.

The C1 producer reports the paths and lightweight provenance for a shared
before/after golden pair.  Formal aggregation must not trust those strings on
their own: this module binds the report to the adjacent schema-v2 metadata and
to the image bytes that are present at aggregation time.

The public validator deliberately returns issue strings instead of raising.
Reports are experiment artifacts and therefore untrusted input; a malformed
path, metadata document, or field must invalidate that trial without taking
down aggregation of the remaining trials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
from threading import Lock
from typing import Any

import yaml

from .visual import screenshot_quality


ORACLE_GOLDEN_ARTIFACT_SCHEMA_VERSION = 2
_MISSING = object()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE_ISSUE_PREFIX = "oracle_goldens"


@dataclass
class OracleGoldenArtifactValidationCache:
    """Deduplicate validation of an unchanged reported golden pair.

    Cache keys include ``lstat`` identity, size, and nanosecond timestamps for
    both images and their adjacent metadata.  Repeated trials sharing the same
    immutable golden pair therefore pay hashing and image decoding once, while
    an ordinary on-disk edit invalidates the entry automatically.
    """

    hits: int = 0
    misses: int = 0
    _entries: dict[tuple[Any, ...], tuple[str, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def clear(self) -> None:
        """Discard cached validations and reset observability counters."""

        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0

    def _lookup(self, key: tuple[Any, ...]) -> tuple[str, ...] | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self.misses += 1
                return None
            self.hits += 1
            return value

    def _store(self, key: tuple[Any, ...], issues: list[str]) -> None:
        with self._lock:
            self._entries[key] = tuple(issues)


def validate_oracle_golden_artifacts(
    oracle_goldens: Any,
    *,
    reported_seed: Any = _MISSING,
    expected_fingerprint: str | None = None,
    expected_git_commit: str | None = None,
    expected_oracle_spec_sha256: str | None = None,
    cache: OracleGoldenArtifactValidationCache | None = None,
    issue_prefix: str = "oracle_goldens",
) -> list[str]:
    """Validate one reported C1 oracle-golden pair, returning all issues.

    ``reported_seed`` is normally the enclosing trial's seed.  For callers
    whose report embeds it in ``oracle_goldens``, omitting the argument falls
    back to ``experiment_seed`` and then ``seed``.  The optional expected git
    commit and oracle-spec digest bind the metadata to pre-registered inputs.

    The function never intentionally raises for malformed report or artifact
    input.  A cache is optional and affects performance only, never validity.
    """

    prefix = issue_prefix if isinstance(issue_prefix, str) and issue_prefix else "oracle_goldens"
    try:
        seed = _reported_seed(oracle_goldens, reported_seed)
        initial_key = _validation_cache_key(
            oracle_goldens,
            reported_seed=seed,
            expected_fingerprint=expected_fingerprint,
            expected_git_commit=expected_git_commit,
            expected_oracle_spec_sha256=expected_oracle_spec_sha256,
        )
        if cache is not None:
            cached = cache._lookup(initial_key)
            if cached is not None:
                return _with_issue_prefix(cached, prefix)

        issues = _validate_oracle_golden_artifacts(
            oracle_goldens,
            reported_seed=seed,
            expected_fingerprint=expected_fingerprint,
            expected_git_commit=expected_git_commit,
            expected_oracle_spec_sha256=expected_oracle_spec_sha256,
            issue_prefix=_CACHE_ISSUE_PREFIX,
        )

        # Fail closed, and do not cache, across a concurrent artifact mutation.
        final_key = _validation_cache_key(
            oracle_goldens,
            reported_seed=seed,
            expected_fingerprint=expected_fingerprint,
            expected_git_commit=expected_git_commit,
            expected_oracle_spec_sha256=expected_oracle_spec_sha256,
        )
        if final_key == initial_key:
            if cache is not None:
                cache._store(final_key, issues)
        else:
            issues.append(
                f"{_CACHE_ISSUE_PREFIX}: artifacts changed during validation"
            )
        return _with_issue_prefix(issues, prefix)
    except Exception as exc:  # pragma: no cover - final fail-closed boundary
        return [f"{prefix}: artifact validation failed safely ({type(exc).__name__})"]


def _validate_oracle_golden_artifacts(
    oracle_goldens: Any,
    *,
    reported_seed: Any,
    expected_fingerprint: str | None,
    expected_git_commit: str | None,
    expected_oracle_spec_sha256: str | None,
    issue_prefix: str,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(oracle_goldens, dict):
        return [f"{issue_prefix}: report value must be an object"]

    before = _reported_path(
        oracle_goldens.get("before"),
        label=f"{issue_prefix}.before",
        issues=issues,
    )
    after = _reported_path(
        oracle_goldens.get("after"),
        label=f"{issue_prefix}.after",
        issues=issues,
    )

    fingerprint = oracle_goldens.get("fingerprint", _MISSING)
    if not _valid_fingerprint(fingerprint):
        issues.append(f"{issue_prefix}.fingerprint: report value is missing or invalid")
    cacheable = oracle_goldens.get("cacheable", _MISSING)
    if type(cacheable) is not bool:
        issues.append(f"{issue_prefix}.cacheable: report value must be boolean")
    if type(reported_seed) is not int:
        issues.append(f"{issue_prefix}.experiment_seed: reported value must be an integer")

    reported_artifacts = oracle_goldens.get("artifacts")
    if not isinstance(reported_artifacts, dict):
        issues.append(f"{issue_prefix}.artifacts: report value must be an object")
        reported_artifacts = {}
    report_digests = {
        name: _reported_artifact_digest(
            reported_artifacts.get(name),
            name=name,
            issue_prefix=issue_prefix,
            issues=issues,
        )
        for name in ("before", "after", "metadata")
    }

    if before is None or after is None:
        return issues
    if before.parent != after.parent:
        issues.append(f"{issue_prefix}: before and after images must share one metadata directory")
        return issues

    metadata_path = before.parent / "meta.json"
    if not _regular_non_symlink(metadata_path):
        issues.append(
            f"{issue_prefix}.meta: adjacent meta.json must be an existing regular non-symlink file"
        )
        return issues

    _compare_report_digest(
        metadata_path,
        reported_digest=report_digests["metadata"],
        label=f"{issue_prefix}.artifacts.metadata",
        issues=issues,
    )

    metadata = _load_metadata(metadata_path, issue_prefix=issue_prefix, issues=issues)
    if metadata is None:
        return issues

    schema_version = metadata.get("schema_version", _MISSING)
    if type(schema_version) is not int or schema_version != ORACLE_GOLDEN_ARTIFACT_SCHEMA_VERSION:
        issues.append(
            f"{issue_prefix}.meta.schema_version: expected {ORACLE_GOLDEN_ARTIFACT_SCHEMA_VERSION}"
        )

    meta_fingerprint = metadata.get("fingerprint", _MISSING)
    if not _valid_fingerprint(meta_fingerprint):
        issues.append(f"{issue_prefix}.meta.fingerprint: value is missing or invalid")
    elif _valid_fingerprint(fingerprint) and fingerprint != meta_fingerprint:
        issues.append(f"{issue_prefix}.fingerprint: report value does not match metadata")
    if expected_fingerprint is not None:
        if not _valid_fingerprint(expected_fingerprint):
            issues.append(f"{issue_prefix}: expected fingerprint is invalid")
        elif fingerprint != expected_fingerprint:
            issues.append(f"{issue_prefix}.fingerprint: report value does not match expected value")

    meta_cacheable = metadata.get("cacheable", _MISSING)
    if type(meta_cacheable) is not bool:
        issues.append(f"{issue_prefix}.meta.cacheable: value must be boolean")
    elif type(cacheable) is bool and cacheable != meta_cacheable:
        issues.append(f"{issue_prefix}.cacheable: report value does not match metadata")

    meta_seed = metadata.get("experiment_seed", _MISSING)
    if type(meta_seed) is not int:
        issues.append(f"{issue_prefix}.meta.experiment_seed: value must be an integer")
    elif type(reported_seed) is int and reported_seed != meta_seed:
        issues.append(f"{issue_prefix}.experiment_seed: report value does not match metadata")

    meta_git_commit = metadata.get("git_commit", _MISSING)
    if meta_cacheable is True and not _nonempty_string(meta_git_commit):
        issues.append(f"{issue_prefix}.meta.git_commit: cacheable artifacts require a commit")
    if expected_git_commit is not None:
        if not _nonempty_string(expected_git_commit):
            issues.append(f"{issue_prefix}: expected git commit is invalid")
        elif meta_git_commit != expected_git_commit:
            issues.append(f"{issue_prefix}.meta.git_commit: value does not match expected commit")

    meta_spec_sha256 = metadata.get("oracle_spec_sha256", _MISSING)
    if not _valid_sha256(meta_spec_sha256):
        issues.append(f"{issue_prefix}.meta.oracle_spec_sha256: value is missing or invalid")
    if expected_oracle_spec_sha256 is not None:
        if not _valid_sha256(expected_oracle_spec_sha256):
            issues.append(f"{issue_prefix}: expected oracle spec sha256 is invalid")
        elif meta_spec_sha256 != expected_oracle_spec_sha256:
            issues.append(
                f"{issue_prefix}.meta.oracle_spec_sha256: value does not match expected digest"
            )

    images = metadata.get("images")
    if not isinstance(images, dict):
        issues.append(f"{issue_prefix}.meta.images: value must be an object")
        images = {}
    for stage, path in (("before", before), ("after", after)):
        _validate_image(
            path,
            stage=stage,
            image_metadata=images.get(stage),
            reported_digest=report_digests[stage],
            issue_prefix=issue_prefix,
            issues=issues,
        )
    return issues


def _validate_image(
    path: Path,
    *,
    stage: str,
    image_metadata: Any,
    reported_digest: str | None,
    issue_prefix: str,
    issues: list[str],
) -> None:
    expected_digest: Any = _MISSING
    if not isinstance(image_metadata, dict):
        issues.append(f"{issue_prefix}.meta.images.{stage}: value must be an object")
    else:
        expected_digest = image_metadata.get("sha256", _MISSING)
        if not _valid_sha256(expected_digest):
            issues.append(
                f"{issue_prefix}.meta.images.{stage}.sha256: value is missing or invalid"
            )

    try:
        actual_digest = _sha256_file(path)
    except Exception:
        issues.append(f"{issue_prefix}.{stage}: image bytes could not be hashed")
    else:
        if reported_digest is not None and actual_digest != reported_digest:
            issues.append(f"{issue_prefix}.{stage}: sha256 does not match report artifact digest")
        if _valid_sha256(expected_digest) and actual_digest != expected_digest:
            issues.append(f"{issue_prefix}.{stage}: sha256 does not match metadata")

    try:
        quality = screenshot_quality(path)
    except Exception:
        issues.append(f"{issue_prefix}.{stage}: screenshot quality could not be evaluated")
    else:
        if not isinstance(quality, dict) or quality.get("informative") is not True:
            issues.append(f"{issue_prefix}.{stage}: current screenshot is non-informative")


def _reported_path(value: Any, *, label: str, issues: list[str]) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or isinstance(value, bytes):
        issues.append(f"{label}: path is missing or invalid")
        return None
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise ValueError("invalid path")
        path = Path(raw).expanduser().absolute()
    except Exception:
        issues.append(f"{label}: path is missing or invalid")
        return None
    if not _regular_non_symlink(path):
        issues.append(f"{label}: must be an existing regular non-symlink file")
        return None
    return path


def _regular_non_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except Exception:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _load_metadata(
    path: Path,
    *,
    issue_prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        issues.append(f"{issue_prefix}.meta: metadata could not be parsed")
        return None
    if not isinstance(value, dict):
        issues.append(f"{issue_prefix}.meta: metadata must be an object")
        return None
    return value


def _reported_seed(oracle_goldens: Any, explicit: Any) -> Any:
    if explicit is not _MISSING:
        return explicit
    if not isinstance(oracle_goldens, dict):
        return _MISSING
    if "experiment_seed" in oracle_goldens:
        return oracle_goldens.get("experiment_seed")
    return oracle_goldens.get("seed", _MISSING)


def _validation_cache_key(
    oracle_goldens: Any,
    *,
    reported_seed: Any,
    expected_fingerprint: str | None,
    expected_git_commit: str | None,
    expected_oracle_spec_sha256: str | None,
) -> tuple[Any, ...]:
    report = oracle_goldens if isinstance(oracle_goldens, dict) else {}
    before = report.get("before", _MISSING)
    after = report.get("after", _MISSING)
    return (
        _stable_scalar(type(oracle_goldens).__name__),
        _stable_scalar(report.get("fingerprint", _MISSING)),
        _stable_scalar(report.get("cacheable", _MISSING)),
        _stable_scalar(report.get("artifacts", _MISSING)),
        _stable_scalar(reported_seed),
        _stable_scalar(expected_fingerprint),
        _stable_scalar(expected_git_commit),
        _stable_scalar(expected_oracle_spec_sha256),
        _artifact_signature(before),
        _artifact_signature(after),
        _adjacent_metadata_signature(before),
        _adjacent_metadata_signature(after),
    )


def _artifact_signature(value: Any) -> tuple[Any, ...]:
    path = _path_for_signature(value)
    if path is None:
        return ("invalid", _stable_scalar(value))
    try:
        metadata = path.lstat()
    except Exception as exc:
        return ("unavailable", str(path), type(exc).__name__)
    return (
        "stat",
        str(path),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _adjacent_metadata_signature(value: Any) -> tuple[Any, ...]:
    path = _path_for_signature(value)
    if path is None:
        return ("invalid-meta",)
    return _artifact_signature(path.parent / "meta.json")


def _path_for_signature(value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or isinstance(value, bytes):
        return None
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            return None
        return Path(raw).expanduser().absolute()
    except Exception:
        return None


def _stable_scalar(value: Any) -> tuple[str, Any]:
    if value is _MISSING:
        return ("missing", None)
    if value is None or type(value) in {str, int, bool, float}:
        return (type(value).__name__, value)
    try:
        rendered = repr(value)
    except Exception:
        rendered = "<unrepresentable>"
    return (type(value).__name__, rendered)


def _with_issue_prefix(issues: Any, issue_prefix: str) -> list[str]:
    if issue_prefix == _CACHE_ISSUE_PREFIX:
        return list(issues)
    return [
        f"{issue_prefix}{issue[len(_CACHE_ISSUE_PREFIX):]}"
        if isinstance(issue, str) and issue.startswith(_CACHE_ISSUE_PREFIX)
        else str(issue)
        for issue in issues
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_oracle_golden_artifact_digests(
    before: str | os.PathLike[str],
    after: str | os.PathLike[str],
) -> dict[str, dict[str, str]]:
    """Capture producer-time digests for a schema-v2 golden pair.

    Unlike the fail-closed validator, this producer helper raises when asked to
    publish an incomplete or symlinked artifact set.  A producer must never emit
    a formal report whose immutable artifact binding could not be captured.
    """

    before_path = Path(before).expanduser().absolute()
    after_path = Path(after).expanduser().absolute()
    if before_path.parent != after_path.parent:
        raise ValueError("oracle golden images do not share a metadata directory")
    metadata_path = before_path.parent / "meta.json"
    paths = {
        "before": before_path,
        "after": after_path,
        "metadata": metadata_path,
    }
    for label, path in paths.items():
        if not _regular_non_symlink(path):
            raise ValueError(f"oracle golden {label} is not a regular non-symlink file")
    return {label: {"sha256": _sha256_file(path)} for label, path in paths.items()}


def _reported_artifact_digest(
    value: Any,
    *,
    name: str,
    issue_prefix: str,
    issues: list[str],
) -> str | None:
    if not isinstance(value, dict) or not _valid_sha256(value.get("sha256")):
        issues.append(
            f"{issue_prefix}.artifacts.{name}.sha256: report value is missing or invalid"
        )
        return None
    return value["sha256"]


def _compare_report_digest(
    path: Path,
    *,
    reported_digest: str | None,
    label: str,
    issues: list[str],
) -> None:
    try:
        actual_digest = _sha256_file(path)
    except Exception:
        issues.append(f"{label}: artifact bytes could not be hashed")
        return
    if reported_digest is not None and actual_digest != reported_digest:
        issues.append(f"{label}: sha256 does not match report artifact digest")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 16
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "ORACLE_GOLDEN_ARTIFACT_SCHEMA_VERSION",
    "OracleGoldenArtifactValidationCache",
    "capture_oracle_golden_artifact_digests",
    "validate_oracle_golden_artifacts",
]
