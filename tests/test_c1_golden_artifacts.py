from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import yaml

from warptest import c1_golden_artifacts
from warptest.c1_golden_artifacts import (
    OracleGoldenArtifactValidationCache,
    capture_oracle_golden_artifact_digests,
    validate_oracle_golden_artifacts,
)


_FINGERPRINT = "0123456789abcdef"
_GIT_COMMIT = "a" * 40
_SPEC_SHA256 = "b" * 64
_SEED = 17


def _write_informative_png(path: Path, *, offset: int) -> None:
    """Write a deterministic, high-entropy PNG accepted by screenshot_quality."""

    image = Image.new("RGB", (192, 192))
    pixels = image.load()
    for y in range(192):
        for x in range(192):
            pixels[x, y] = (
                (x * 37 + y * 17 + offset) % 256,
                (x * 11 + y * 43 + offset * 3) % 256,
                (x * y + x * 7 + y * 13 + offset * 5) % 256,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_fixture(root: Path) -> tuple[dict[str, object], Path]:
    directory = root / "goldens"
    before = directory / "golden_before.png"
    after = directory / "golden_after.png"
    _write_informative_png(before, offset=1)
    _write_informative_png(after, offset=2)
    metadata = {
        "schema_version": 2,
        "fingerprint": _FINGERPRINT,
        "git_commit": _GIT_COMMIT,
        "experiment_seed": _SEED,
        "oracle_spec_sha256": _SPEC_SHA256,
        "cacheable": True,
        "images": {
            "before": {"sha256": _sha256(before)},
            "after": {"sha256": _sha256(after)},
        },
    }
    metadata_path = directory / "meta.json"
    metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    report = {
        "before": str(before),
        "after": str(after),
        "fingerprint": _FINGERPRINT,
        "cacheable": True,
        "artifacts": capture_oracle_golden_artifact_digests(before, after),
    }
    return report, metadata_path


class OracleGoldenArtifactValidationTests(unittest.TestCase):
    def test_valid_informative_pair_matches_report_and_expected_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = _valid_fixture(Path(tmp))
            issues = validate_oracle_golden_artifacts(
                report,
                reported_seed=_SEED,
                expected_git_commit=_GIT_COMMIT,
                expected_oracle_spec_sha256=_SPEC_SHA256,
                expected_fingerprint=_FINGERPRINT,
            )
        self.assertEqual(issues, [])

    def test_image_byte_tamper_is_detected_and_invalidates_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = _valid_fixture(Path(tmp))
            cache = OracleGoldenArtifactValidationCache()
            self.assertEqual(
                validate_oracle_golden_artifacts(report, reported_seed=_SEED, cache=cache),
                [],
            )
            _write_informative_png(Path(str(report["before"])), offset=99)
            issues = validate_oracle_golden_artifacts(
                report,
                reported_seed=_SEED,
                cache=cache,
            )
        self.assertIn("oracle_goldens.before: sha256 does not match metadata", issues)
        self.assertIn(
            "oracle_goldens.before: sha256 does not match report artifact digest",
            issues,
        )
        self.assertEqual(cache.misses, 2)

    def test_metadata_report_and_expected_input_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, metadata_path = _valid_fixture(Path(tmp))
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            metadata["fingerprint"] = "f" * 16
            metadata["cacheable"] = False
            metadata["experiment_seed"] = _SEED + 1
            metadata["git_commit"] = "c" * 40
            metadata["oracle_spec_sha256"] = "d" * 64
            metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
            issues = validate_oracle_golden_artifacts(
                report,
                reported_seed=_SEED,
                expected_git_commit=_GIT_COMMIT,
                expected_oracle_spec_sha256=_SPEC_SHA256,
            )
        self.assertIn("oracle_goldens.fingerprint: report value does not match metadata", issues)
        self.assertIn("oracle_goldens.cacheable: report value does not match metadata", issues)
        self.assertIn("oracle_goldens.experiment_seed: report value does not match metadata", issues)
        self.assertIn("oracle_goldens.meta.git_commit: value does not match expected commit", issues)
        self.assertIn(
            "oracle_goldens.meta.oracle_spec_sha256: value does not match expected digest",
            issues,
        )

    def test_coordinated_image_and_metadata_rewrite_still_fails_report_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, metadata_path = _valid_fixture(Path(tmp))
            before = Path(str(report["before"]))
            _write_informative_png(before, offset=88)
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            metadata["images"]["before"]["sha256"] = _sha256(before)
            metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
        self.assertNotIn("oracle_goldens.before: sha256 does not match metadata", issues)
        self.assertIn(
            "oracle_goldens.before: sha256 does not match report artifact digest",
            issues,
        )
        self.assertIn(
            "oracle_goldens.artifacts.metadata: sha256 does not match report artifact digest",
            issues,
        )

    def test_schema_meta_symlink_and_image_symlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, metadata_path = _valid_fixture(root)
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            metadata["schema_version"] = 1
            metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
            self.assertIn("oracle_goldens.meta.schema_version: expected 2", issues)

            real_meta = metadata_path.with_name("real-meta.yaml")
            metadata_path.replace(real_meta)
            metadata_path.symlink_to(real_meta.name)
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
            self.assertIn(
                "oracle_goldens.meta: adjacent meta.json must be an existing regular non-symlink file",
                issues,
            )

            metadata_path.unlink()
            real_meta.replace(metadata_path)
            before = Path(str(report["before"]))
            real_before = before.with_name("real-before.png")
            before.replace(real_before)
            before.symlink_to(real_before.name)
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
        self.assertIn(
            "oracle_goldens.before: must be an existing regular non-symlink file",
            issues,
        )

    def test_malformed_reports_metadata_fields_and_paths_never_raise(self) -> None:
        malformed_reports: list[object] = [None, [], "bad", {"before": [], "after": {}}]
        for report in malformed_reports:
            with self.subTest(report=report):
                issues = validate_oracle_golden_artifacts(report, reported_seed="bad")
                self.assertTrue(issues)

        with tempfile.TemporaryDirectory() as tmp:
            report, metadata_path = _valid_fixture(Path(tmp))
            metadata_path.write_text("{not valid", encoding="utf-8")
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
            self.assertIn("oracle_goldens.meta: metadata could not be parsed", issues)

            metadata_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "2",
                        "fingerprint": [],
                        "cacheable": 1,
                        "experiment_seed": True,
                        "oracle_spec_sha256": {},
                        "images": {"before": None, "after": {"sha256": []}},
                    }
                ),
                encoding="utf-8",
            )
            malformed = dict(report)
            malformed["fingerprint"] = []
            malformed["cacheable"] = "yes"
            malformed["artifacts"] = {"before": None, "after": [], "metadata": "bad"}
            issues = validate_oracle_golden_artifacts(malformed, reported_seed=True)
        self.assertGreaterEqual(len(issues), 8)

    def test_current_screenshot_quality_must_remain_informative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, metadata_path = _valid_fixture(Path(tmp))
            before = Path(str(report["before"]))
            Image.new("RGB", (192, 192), color=(0, 0, 0)).save(before)
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            metadata["images"]["before"]["sha256"] = _sha256(before)
            metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
            issues = validate_oracle_golden_artifacts(report, reported_seed=_SEED)
        self.assertIn("oracle_goldens.before: current screenshot is non-informative", issues)

    def test_repeated_unchanged_pair_uses_dedupe_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = _valid_fixture(Path(tmp))
            cache = OracleGoldenArtifactValidationCache()
            real_quality = c1_golden_artifacts.screenshot_quality
            with patch(
                "warptest.c1_golden_artifacts.screenshot_quality",
                wraps=real_quality,
            ) as quality:
                first = validate_oracle_golden_artifacts(
                    report,
                    reported_seed=_SEED,
                    cache=cache,
                )
                second = validate_oracle_golden_artifacts(
                    report,
                    reported_seed=_SEED,
                    cache=cache,
                )
            self.assertEqual(first, [])
            self.assertEqual(second, [])
            self.assertEqual(quality.call_count, 2)
            self.assertEqual(cache.misses, 1)
            self.assertEqual(cache.hits, 1)

    def test_dedupe_cache_is_prefix_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = _valid_fixture(Path(tmp))
            cache = OracleGoldenArtifactValidationCache()
            report["fingerprint"] = "f" * 16
            first = validate_oracle_golden_artifacts(
                report,
                reported_seed=_SEED,
                cache=cache,
                issue_prefix="trial.one.oracle_goldens",
            )
            second = validate_oracle_golden_artifacts(
                report,
                reported_seed=_SEED,
                cache=cache,
                issue_prefix="trial.two.oracle_goldens",
            )
        self.assertTrue(all(issue.startswith("trial.one.oracle_goldens") for issue in first))
        self.assertTrue(all(issue.startswith("trial.two.oracle_goldens") for issue in second))
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 1)

    def test_concurrent_artifact_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, metadata_path = _valid_fixture(Path(tmp))
            real_quality = c1_golden_artifacts.screenshot_quality

            def mutate_after_quality(path: Path) -> dict[str, object]:
                result = real_quality(path)
                if path == Path(str(report["after"])):
                    metadata_path.write_bytes(metadata_path.read_bytes() + b"\n")
                return result

            with patch(
                "warptest.c1_golden_artifacts.screenshot_quality",
                side_effect=mutate_after_quality,
            ):
                issues = validate_oracle_golden_artifacts(
                    report,
                    reported_seed=_SEED,
                )
        self.assertIn("oracle_goldens: artifacts changed during validation", issues)

    def test_embedded_seed_fallback_and_custom_issue_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = _valid_fixture(Path(tmp))
            report["experiment_seed"] = _SEED
            self.assertEqual(
                validate_oracle_golden_artifacts(report, issue_prefix="trial.goldens"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
