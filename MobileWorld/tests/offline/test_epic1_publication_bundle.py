from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import stat
import struct
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from mobile_world.offline.failure_attribution import (
    phase_a_review_schema,
    phase_b_review_schema,
)
from mobile_world.offline.motivation_review import canonical_json_bytes

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REPORT_ROOT = REPOSITORY_ROOT / "motivation study"
REPORT_PATH = REPORT_ROOT / "misleading_history_audit_report.md"
REPORT_PDF_PATH = REPORT_ROOT / "misleading_history_audit_report_20260825.pdf"
REPORT_ASSET_ROOT = REPORT_ROOT / "report_assets"
SCREENSHOT_ROOT = REPORT_ASSET_ROOT / "screenshots"
SCREENSHOT_MANIFEST_PATH = REPORT_ASSET_ROOT / "screenshot_manifest.v1.json"
SCREENSHOT_MANIFEST_SCHEMA_PATH = REPORT_ASSET_ROOT / "screenshot_manifest.v1.schema.json"
RENDERER_PATH = REPOSITORY_ROOT / "MobileWorld/scripts/render_misleading_history_audit_pdf.py"
BUNDLE_ROOT = REPOSITORY_ROOT / "motivation study/epic1_failure_link_audit_v1"
LOCK_PATH = BUNDLE_ROOT / "publication_lock.v2.json"
SUMMARY_PATH = BUNDLE_ROOT / "public_summary.v2.json"
LOCK_SCHEMA_PATH = BUNDLE_ROOT / "schemas/failure_link_publication_lock.v2.schema.json"
SUMMARY_SCHEMA_PATH = BUNDLE_ROOT / "schemas/failure_link_public_summary.v2.schema.json"

EXPECTED_SCREENSHOT_COUNT = 39
EXPECTED_SCREENSHOT_BYTES = 18_315_499
EXPECTED_SCREENSHOT_WIDTH = 1080
EXPECTED_SCREENSHOT_HEIGHT = 2400
EXPECTED_PDF_BYTES = 16_129_148
EXPECTED_PDF_SHA256 = "af670f4c8f539b61ad2199d76a7ec86cfcf7830af4ad8cfa3936ea34f2b0852a"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REPORT_IMAGE_RE = re.compile(
    r"^!\[(?P<alt>.*?)]\("
    r"(?P<path>report_assets/screenshots/(?P<sha256>[0-9a-f]{64})\.png)"
    r"\)$"
)

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
    "public_summary.v2.json",
    "publication_lock.v1.json",
    "publication_lock.v2.json",
    "schemas/failure_link_public_summary.v1.schema.json",
    "schemas/failure_link_public_summary.v2.schema.json",
    "schemas/failure_link_publication_lock.v1.schema.json",
    "schemas/failure_link_publication_lock.v2.schema.json",
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


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = source.read(24)
    assert header[:8] == PNG_SIGNATURE
    assert header[8:12] == struct.pack(">I", 13)
    assert header[12:16] == b"IHDR"
    return struct.unpack(">II", header[16:24])


def _report_image_refs() -> list[dict[str, str]]:
    report = REPORT_PATH.read_text(encoding="utf-8")
    image_lines = [line for line in report.splitlines() if line.startswith("![")]
    matches = [REPORT_IMAGE_RE.fullmatch(line) for line in image_lines]
    assert len(image_lines) == EXPECTED_SCREENSHOT_COUNT
    assert all(match is not None for match in matches)
    return [match.groupdict() for match in matches if match is not None]


def test_current_publication_and_screenshot_manifest_are_schema_valid() -> None:
    lock_schema = _load_json(LOCK_SCHEMA_PATH)
    summary_schema = _load_json(SUMMARY_SCHEMA_PATH)
    screenshot_schema = _load_json(SCREENSHOT_MANIFEST_SCHEMA_PATH)
    Draft202012Validator.check_schema(lock_schema)
    Draft202012Validator.check_schema(summary_schema)
    Draft202012Validator.check_schema(screenshot_schema)
    Draft202012Validator(lock_schema).validate(_load_json(LOCK_PATH))
    Draft202012Validator(summary_schema).validate(_load_json(SUMMARY_PATH))
    Draft202012Validator(screenshot_schema).validate(_load_json(SCREENSHOT_MANIFEST_PATH))


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


def test_every_current_locked_file_matches_hash_and_byte_count() -> None:
    lock = _load_json(LOCK_PATH)
    source_projection = lock["source_projection"]
    source_bindings = source_projection["files"]
    assert len(source_bindings) == source_projection["file_count"] == 8
    assert len({binding["logical_name"] for binding in source_bindings}) == 8
    assert sum(binding["byte_count"] for binding in source_bindings) == 3_018_398

    repository_bindings = lock["repository_bindings"]
    expected_repository_paths = {
        "motivation study/misleading_history_audit_report.md",
        "motivation study/misleading_history_audit_report_20260825.pdf",
        "motivation study/report_assets/screenshot_manifest.v1.json",
        "motivation study/report_assets/screenshot_manifest.v1.schema.json",
        "MobileWorld/scripts/render_misleading_history_audit_pdf.py",
        "motivation study/epic1_failure_link_audit_v1/public_summary.v2.json",
        "motivation study/epic1_failure_link_audit_v1/schemas/failure_link_public_summary.v2.schema.json",
        "motivation study/epic1_failure_link_audit_v1/schemas/failure_link_publication_lock.v2.schema.json",
    }
    assert len(repository_bindings) == len(expected_repository_paths) == 8
    assert {binding["path"] for binding in repository_bindings} == expected_repository_paths
    assert len({binding["logical_name"] for binding in repository_bindings}) == 8

    for binding in [*source_bindings, *repository_bindings]:
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


def test_current_summary_records_the_narrow_owner_publication_boundary() -> None:
    summary = _load_json(SUMMARY_PATH)
    assert summary["report_binding"] == {
        "path": "motivation study/misleading_history_audit_report.md",
        "sha256": _sha256(REPORT_PATH),
        "byte_count": REPORT_PATH.stat().st_size,
    }
    assert summary["pdf_binding"] == {
        "path": "motivation study/misleading_history_audit_report_20260825.pdf",
        "sha256": EXPECTED_PDF_SHA256,
        "byte_count": EXPECTED_PDF_BYTES,
    }
    assert summary["repo_publication_boundary"] == {
        "safe_source_projection_file_count": 8,
        "report_screenshot_bytes_included": True,
        "report_screenshot_count": EXPECTED_SCREENSHOT_COUNT,
        "report_screenshot_byte_count": EXPECTED_SCREENSHOT_BYTES,
        "report_pdf_included": True,
        "raw_trajectory_or_request_text_included": False,
        "review_rationale_or_evaluator_excerpt_included": False,
        "model_response_receipt_rejected_or_migration_artifact_included": False,
        "machine_local_run_manifest_or_log_included": False,
        "other_raw_collection_data_included": False,
        "synthetic_credential_like_ui_content_included": True,
        "actual_credentials_or_secrets_included": False,
        "third_party_rights_independently_verified": False,
        "medical_misinformation_is_synthetic_not_advice": True,
        "public_repository_permanence_acknowledged": True,
        "owner_public_evidence_exception_scope": (
            "exact_39_report_screenshots_and_companion_pdf_only"
        ),
    }


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


def test_report_screenshots_and_pdf_are_self_contained_and_authenticated() -> None:
    manifest = _load_json(SCREENSHOT_MANIFEST_PATH)
    assets = manifest["assets"]
    refs = _report_image_refs()
    report = REPORT_PATH.read_text(encoding="utf-8")

    assert "/shared/" not in report
    assert manifest["asset_count"] == len(assets) == len(refs) == EXPECTED_SCREENSHOT_COUNT
    assert manifest["total_byte_count"] == EXPECTED_SCREENSHOT_BYTES
    assert [asset["ordinal"] for asset in assets] == list(range(1, EXPECTED_SCREENSHOT_COUNT + 1))
    assert len({asset["sha256"] for asset in assets}) == EXPECTED_SCREENSHOT_COUNT

    expected_paths = set()
    observed_total_bytes = 0
    for ordinal, (asset, ref) in enumerate(zip(assets, refs, strict=True), start=1):
        expected_repo_path = f"motivation study/{ref['path']}"
        expected_paths.add(expected_repo_path)
        assert asset == {
            "ordinal": ordinal,
            "path": expected_repo_path,
            "sha256": ref["sha256"],
            "byte_count": asset["byte_count"],
            "media_type": "image/png",
            "width": EXPECTED_SCREENSHOT_WIDTH,
            "height": EXPECTED_SCREENSHOT_HEIGHT,
            "alt_text": ref["alt"],
        }

        path = REPOSITORY_ROOT / asset["path"]
        _assert_regular_repo_file(path)
        assert path.parent == SCREENSHOT_ROOT
        assert path.name == f"{asset['sha256']}.png"
        assert path.stat().st_size == asset["byte_count"]
        assert _sha256(path) == asset["sha256"]
        assert _png_dimensions(path) == (
            EXPECTED_SCREENSHOT_WIDTH,
            EXPECTED_SCREENSHOT_HEIGHT,
        )
        observed_total_bytes += path.stat().st_size

    observed_paths = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in SCREENSHOT_ROOT.iterdir()
        if path.is_file()
    }
    assert observed_paths == expected_paths
    assert observed_total_bytes == EXPECTED_SCREENSHOT_BYTES

    _assert_regular_repo_file(REPORT_PDF_PATH)
    assert REPORT_PDF_PATH.stat().st_size == EXPECTED_PDF_BYTES
    assert _sha256(REPORT_PDF_PATH) == EXPECTED_PDF_SHA256


def test_pdf_renderer_accepts_only_authenticated_report_local_images(tmp_path: Path) -> None:
    renderer = RENDERER_PATH.read_text(encoding="utf-8")
    namespace = runpy.run_path(str(RENDERER_PATH), run_name="epic1_pdf_renderer_test")
    resolve_report_image = namespace["resolve_report_image"]
    markdown_to_tex = namespace["markdown_to_tex"]
    refs = _report_image_refs()

    assert "shutil.rmtree" not in renderer
    assert '.open("xb")' in renderer
    assert "build directory must be absent" in renderer
    assert "report_assets/screenshots/<lowercase-sha256>.png" in renderer

    source, digest = resolve_report_image(REPORT_PATH, refs[0]["path"])
    assert source == REPORT_ROOT / refs[0]["path"]
    assert digest == refs[0]["sha256"]

    rejected_paths = (
        "/shared/report_assets/screenshots/" + "0" * 64 + ".png",
        "https://example.test/" + "0" * 64 + ".png",
        "file:///tmp/" + "0" * 64 + ".png",
        "../report_assets/screenshots/" + "0" * 64 + ".png",
        "report_assets/../screenshots/" + "0" * 64 + ".png",
        "report_assets/screenshots/" + "0" * 64 + ".jpg",
    )
    for rejected_path in rejected_paths:
        with pytest.raises(ValueError, match="report images must use"):
            resolve_report_image(REPORT_PATH, rejected_path)

    invalid_figure_dir = tmp_path / "invalid-figures"
    invalid_figure_dir.mkdir()
    with pytest.raises(ValueError, match="unsupported report image syntax"):
        markdown_to_tex(
            "![remote](https://example.test/image(with-parentheses).png)",
            invalid_figure_dir,
            REPORT_PATH,
        )

    symlink_report_root = tmp_path / "symlink-report"
    symlink_screenshot_root = symlink_report_root / "report_assets/screenshots"
    symlink_screenshot_root.mkdir(parents=True)
    symlink_report = symlink_report_root / "report.md"
    symlink_report.write_text("# report\n", encoding="utf-8")
    symlink_path = symlink_report_root / refs[0]["path"]
    symlink_path.symlink_to(source)
    with pytest.raises(RuntimeError, match="must not contain a symlink"):
        resolve_report_image(symlink_report, refs[0]["path"])

    mismatch_report_root = tmp_path / "mismatch-report"
    mismatch_screenshot_root = mismatch_report_root / "report_assets/screenshots"
    mismatch_screenshot_root.mkdir(parents=True)
    mismatch_report = mismatch_report_root / "report.md"
    mismatch_report.write_text("# report\n", encoding="utf-8")
    mismatch_path = mismatch_screenshot_root / ("0" * 64 + ".png")
    mismatch_path.write_bytes(PNG_SIGNATURE)
    with pytest.raises(RuntimeError, match="digest/filename mismatch"):
        resolve_report_image(mismatch_report, f"report_assets/screenshots/{'0' * 64}.png")

    figure_dir = tmp_path / "figures"
    figure_dir.mkdir()
    tex = markdown_to_tex(REPORT_PATH.read_text(encoding="utf-8"), figure_dir, REPORT_PATH)
    assert "\\end{document}" in tex
    assert len(list(figure_dir.iterdir())) == EXPECTED_SCREENSHOT_COUNT
    assert all(path.is_symlink() for path in figure_dir.iterdir())


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
    assert os.path.isabs(lock["source_archive"]["source_id"]) is False
    for binding in [*lock["source_projection"]["files"], *lock["repository_bindings"]]:
        assert os.path.isabs(binding["path"]) is False
        assert ".." not in Path(binding["path"]).parts
