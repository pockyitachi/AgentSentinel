#!/usr/bin/env python3
"""Publish a strict denominator-complete analysis of one completed R2.5 pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mobile_world.runtime.sentinel.r2_4.live_run import (
    LiveRunContractError,
    authority_manifest_sha256,
    load_owner_authorized_authority_manifest_v2,
)
from mobile_world.runtime.sentinel.r2_5.analysis import (
    R25AnalysisContractError,
    pilot_analysis_sha256,
)
from mobile_world.runtime.sentinel.r2_5.analysis_artifact import (
    R25AnalysisArtifactError,
    analyze_pilot_artifacts_v1,
    write_pilot_analysis_artifact_v1,
)
from mobile_world.runtime.sentinel.r2_5.integrity_gate import (
    PostRunIntegrityAuthorityV1,
    R25PostRunIntegrityError,
)
from mobile_world.runtime.sentinel.r2_5.pilot import frozen_pilot_manifest_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a completed R2.5 pilot stage and every referenced owner-only "
            "runtime-audit detail, then write one fresh canonical 0600 analysis artifact."
        )
    )
    parser.add_argument("--authority-manifest", required=True, type=Path)
    parser.add_argument("--pilot-stage-artifact", required=True, type=Path)
    parser.add_argument("--production-audit-root", required=True, type=Path)
    parser.add_argument("--post-run-integrity-artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--confirm-post-run-integrity-sha256", required=True)
    parser.add_argument("--confirm-preflight-report-sha256", required=True)
    parser.add_argument("--confirm-factory-binding-sha256", required=True)
    parser.add_argument("--confirm-resolved-pilot-inputs-sha256", required=True)
    parser.add_argument("--backend-endpoint", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = load_owner_authorized_authority_manifest_v2(
            arguments.authority_manifest,
            confirmed_manifest_sha256=arguments.confirm_manifest_sha256,
        )
        manifest_hash = authority_manifest_sha256(manifest)
        if arguments.confirm_manifest_sha256 != manifest_hash:
            raise R25AnalysisArtifactError(
                "MANIFEST_CONFIRMATION_MISMATCH", "owner manifest pin differs"
            )
        if (
            type(manifest.runtime_config_sha256) is not str
            or type(manifest.pricing_sha256) is not str
            or type(manifest.sentinel_config_sha256) is not str
            or type(manifest.max_post_run_integrity_wall_time_seconds) is not int
        ):
            raise R25AnalysisArtifactError(
                "R25_AUTHORITY_V2_REQUIRED",
                "production analysis requires the complete v2 run authority",
            )
        integrity_authority = PostRunIntegrityAuthorityV1(
            run_id=manifest.run_id,
            authority_manifest_sha256=manifest_hash,
            preflight_report_sha256=arguments.confirm_preflight_report_sha256,
            runtime_config_sha256=manifest.runtime_config_sha256,
            pricing_sha256=manifest.pricing_sha256,
            sentinel_config_sha256=manifest.sentinel_config_sha256,
            factory_binding_sha256=arguments.confirm_factory_binding_sha256,
            run_manifest=manifest,
            source_commit=manifest.source_commit,
            pilot_manifest=manifest.pilot,
            pilot_manifest_sha256=frozen_pilot_manifest_sha256(manifest.pilot),
            resolved_pilot_inputs_sha256=(arguments.confirm_resolved_pilot_inputs_sha256),
            backend_endpoint=arguments.backend_endpoint,
            expected_cell_count=len(manifest.pilot.cells),
            max_sequence_wall_time_seconds=manifest.max_sequence_wall_time_seconds,
            max_wall_time_seconds=(manifest.max_post_run_integrity_wall_time_seconds),
            production_audit_root=str(arguments.production_audit_root),
        )
        analysis = analyze_pilot_artifacts_v1(
            manifest.pilot,
            run_manifest_sha256=manifest_hash,
            run_id=manifest.run_id,
            pilot_stage_artifact=arguments.pilot_stage_artifact,
            production_audit_root=arguments.production_audit_root,
            post_run_integrity_artifact=arguments.post_run_integrity_artifact,
            confirmed_post_run_integrity_sha256=(arguments.confirm_post_run_integrity_sha256),
            post_run_integrity_authority=integrity_authority,
        )
        written_hash = write_pilot_analysis_artifact_v1(
            analysis,
            arguments.output,
            repository_root=REPOSITORY_ROOT,
        )
        if written_hash != pilot_analysis_sha256(analysis):
            raise R25AnalysisArtifactError(
                "ANALYSIS_ARTIFACT_HASH_MISMATCH", "published analysis hash differs"
            )
    except (
        LiveRunContractError,
        R25AnalysisArtifactError,
        R25AnalysisContractError,
        R25PostRunIntegrityError,
        OSError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "R25_ANALYSIS_FAILED")
        print(json.dumps({"error_code": code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "analysis_sha256": written_hash,
                "cell_count": len(analysis.cells),
                "matched_pair_count": len(analysis.matched_pairs),
                "ok": True,
                "output": str(arguments.output),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
