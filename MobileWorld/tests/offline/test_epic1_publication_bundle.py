from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from mobile_world.offline.failure_attribution import (
    phase_a_review_schema,
    phase_b_review_schema,
)
from mobile_world.offline.motivation_review import canonical_json_bytes

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = REPOSITORY_ROOT / "motivation study/epic1_failure_link_audit_v1"
LOCK_PATH = BUNDLE_ROOT / "publication_lock.v1.json"
SUMMARY_PATH = BUNDLE_ROOT / "public_summary.v1.json"
LOCK_SCHEMA_PATH = BUNDLE_ROOT / "schemas/failure_link_publication_lock.v1.schema.json"
SUMMARY_SCHEMA_PATH = BUNDLE_ROOT / "schemas/failure_link_public_summary.v1.schema.json"

EXPECTED_BUNDLE_FILES = {
    "README.md",
    "phase_a/driver_freeze.json",
    "phase_a/input/review_schema.json",
    "phase_a/resolution/manifest.json",
    "phase_b/driver_freeze.json",
    "phase_b/input/manifest.json",
    "phase_b/input/review_schema.json",
    "phase_b/resolution/manifest.json",
    "phase_b/resolution/metrics.json",
    "public_summary.v1.json",
    "publication_lock.v1.json",
    "schemas/failure_link_public_summary.v1.schema.json",
    "schemas/failure_link_publication_lock.v1.schema.json",
}


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_bytes(), parse_constant=_reject_non_json_constant)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_regular_repo_file(path: Path) -> None:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert not path.is_symlink()
    assert metadata.st_nlink == 1
    assert stat.S_IMODE(metadata.st_mode) & 0o002 == 0
    assert stat.S_IMODE(metadata.st_mode) & 0o7000 == 0


def test_publication_lock_and_summary_are_schema_valid() -> None:
    lock_schema = _load_json(LOCK_SCHEMA_PATH)
    summary_schema = _load_json(SUMMARY_SCHEMA_PATH)
    Draft202012Validator.check_schema(lock_schema)
    Draft202012Validator.check_schema(summary_schema)
    Draft202012Validator(lock_schema).validate(_load_json(LOCK_PATH))
    Draft202012Validator(summary_schema).validate(_load_json(SUMMARY_PATH))


def test_publication_bundle_has_exact_safe_file_set() -> None:
    observed = {
        path.relative_to(BUNDLE_ROOT).as_posix()
        for path in BUNDLE_ROOT.rglob("*")
        if path.is_file()
    }
    assert observed == EXPECTED_BUNDLE_FILES
    for relative_path in sorted(observed):
        _assert_regular_repo_file(BUNDLE_ROOT / relative_path)

    forbidden_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".jsonl"}
    assert not any(path.suffix.lower() in forbidden_suffixes for path in BUNDLE_ROOT.rglob("*"))


def test_every_locked_file_matches_hash_and_byte_count() -> None:
    lock = _load_json(LOCK_PATH)
    source_projection = lock["source_projection"]
    source_bindings = source_projection["files"]
    assert len(source_bindings) == source_projection["file_count"] == 8
    assert len({binding["logical_name"] for binding in source_bindings}) == 8
    assert sum(binding["byte_count"] for binding in source_bindings) == 3_018_398

    for binding in [*source_bindings, *lock["repository_bindings"]]:
        path = REPOSITORY_ROOT / binding["path"]
        _assert_regular_repo_file(path)
        assert path.stat().st_size == binding["byte_count"]
        assert _sha256(path) == binding["sha256"]


def test_frozen_review_schemas_equal_the_runtime_emitters() -> None:
    phase_a_bytes = (BUNDLE_ROOT / "phase_a/input/review_schema.json").read_bytes()
    phase_b_bytes = (BUNDLE_ROOT / "phase_b/input/review_schema.json").read_bytes()
    assert phase_a_bytes == canonical_json_bytes(phase_a_review_schema())
    assert phase_b_bytes == canonical_json_bytes(phase_b_review_schema())


def test_public_summary_preserves_both_noncausal_projections() -> None:
    summary = _load_json(SUMMARY_PATH)
    metrics = _load_json(BUNDLE_ROOT / "phase_b/resolution/metrics.json")
    level_counts = metrics["task_counts_by_failure_link_level"]

    assert level_counts == summary["phase_b_internal_observational"][
        "failure_link_level_counts"
    ] | {"NOT_APPLICABLE_SUCCESS_CONTROL": 0}
    assert sum(level_counts.values()) == 108
    assert metrics["plausible_or_strong_observed_contribution"]["task_count"] == 60
    assert summary["phase_b_internal_observational"]["plausible_or_strong_case_count"] == 60

    reader = summary["reader_facing_projection"]
    assert reader["direct_stop_case_count"] + reader["indirect_derailment_case_count"] == 58
    assert (
        reader["linked_case_count"]
        + reader["adapter_parser_boundary_case_count"]
        + reader["no_supported_reader_link_case_count"]
        == reader["denominator_failure_strict_mhr_cases"]
        == 108
    )
    assert metrics["causal_claim_supported"] is False
    assert reader["causal_claim_supported"] is False


def test_safe_projection_has_no_machine_paths_or_secret_patterns() -> None:
    lock = _load_json(LOCK_PATH)
    forbidden = (
        b"/home/",
        b"/shared/",
        b"/tmp/",
        b"authorization:",
        b"bearer ",
        b"api_key",
        b"access_token",
        b"13802138888",
    )
    for binding in lock["source_projection"]["files"]:
        lowered = (REPOSITORY_ROOT / binding["path"]).read_bytes().lower()
        assert all(marker not in lowered for marker in forbidden)


def test_pdf_renderer_is_non_replacing_and_report_images_remain_external() -> None:
    renderer = (
        REPOSITORY_ROOT / "MobileWorld/scripts/render_misleading_history_audit_pdf.py"
    ).read_text(encoding="utf-8")
    report = (REPOSITORY_ROOT / "motivation study/misleading_history_audit_report.md").read_text(
        encoding="utf-8"
    )

    assert "shutil.rmtree" not in renderer
    assert '.open("xb")' in renderer
    assert "build directory must be absent" in renderer
    assert report.count("](/shared/") == 39
    assert not (BUNDLE_ROOT / "misleading_history_audit_report_20260825.pdf").exists()


def test_projection_files_are_canonical_json_with_one_lf() -> None:
    lock = _load_json(LOCK_PATH)
    for binding in lock["source_projection"]["files"]:
        path = REPOSITORY_ROOT / binding["path"]
        raw = path.read_bytes()
        value = _load_json(path)
        assert raw == canonical_json_bytes(value)
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")


def test_publication_does_not_depend_on_external_source_root() -> None:
    lock = _load_json(LOCK_PATH)
    encoded = json.dumps(lock, sort_keys=True).encode("utf-8")
    assert b"/shared/" not in encoded
    assert b"/home/" not in encoded
    assert b"/tmp/" not in encoded
    assert lock["source_archive"]["file_count"] == 2_842
    assert lock["source_archive"]["byte_count"] == 119_555_475
    assert lock["safety"]["raw_collection_data_committed"] is False
    assert os.path.isabs(lock["source_archive"]["source_id"]) is False
