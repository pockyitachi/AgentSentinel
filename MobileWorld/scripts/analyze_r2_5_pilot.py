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
    load_authority_manifest,
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = load_authority_manifest(arguments.authority_manifest)
        manifest_hash = authority_manifest_sha256(manifest)
        if arguments.confirm_manifest_sha256 != manifest_hash:
            raise R25AnalysisArtifactError(
                "MANIFEST_CONFIRMATION_MISMATCH", "owner manifest pin differs"
            )
        analysis = analyze_pilot_artifacts_v1(
            manifest.pilot,
            run_manifest_sha256=manifest_hash,
            run_id=manifest.run_id,
            pilot_stage_artifact=arguments.pilot_stage_artifact,
            production_audit_root=arguments.production_audit_root,
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
