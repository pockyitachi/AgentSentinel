#!/usr/bin/env python3
"""Run the bounded, action-free R2.5 post-run Collector integrity gate."""

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
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    parse_production_runtime_config,
    production_runtime_config_sha256,
)
from mobile_world.runtime.sentinel.r2_5 import integrity_gate as integrity_gate_module
from mobile_world.runtime.sentinel.r2_5.integrity_gate import (
    PostRunIntegrityAuthorityV1,
    R25PostRunIntegrityError,
    reopen_validate_post_run_integrity_artifact_v1,
    run_post_run_integrity_gate_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    frozen_pilot_manifest_sha256,
    resolve_pilot_task_inputs_v1,
    resolved_pilot_task_inputs_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "After the complete R2.4/R2.5 sequence has cleaned its resources and "
            "published terminal.json, run the official Collector v1 checker over "
            "six smoke runs and every pilot cell, then publish one owner-only acceptance artifact."
        )
    )
    parser.add_argument("--authority-manifest", required=True, type=Path)
    parser.add_argument("--sequence-output-root", required=True, type=Path)
    parser.add_argument("--confirm-sequence-deadline-monotonic-ns", required=True, type=int)
    parser.add_argument("--confirm-sequence-started-monotonic-ns", required=True, type=int)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--confirm-preflight-report-sha256", required=True)
    parser.add_argument("--confirm-runtime-config-sha256", required=True)
    parser.add_argument("--confirm-pricing-sha256", required=True)
    parser.add_argument("--confirm-sentinel-config-sha256", required=True)
    parser.add_argument("--confirm-factory-binding-sha256", required=True)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--production-audit-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = load_owner_authorized_authority_manifest_v2(
            arguments.authority_manifest,
            confirmed_manifest_sha256=arguments.confirm_manifest_sha256,
        )
        manifest_sha256 = authority_manifest_sha256(manifest)
        if arguments.confirm_manifest_sha256 != manifest_sha256:
            raise R25PostRunIntegrityError(
                "MANIFEST_CONFIRMATION_MISMATCH", "owner manifest pin differs"
            )
        runtime_raw, _ = integrity_gate_module._read_owner_file(
            arguments.runtime_config,
            maximum_bytes=1_048_576,
            code="INVALID_RUNTIME_CONFIG",
        )
        runtime_value = integrity_gate_module._decode_canonical_json(
            runtime_raw, code="INVALID_RUNTIME_CONFIG"
        )
        runtime_config = parse_production_runtime_config(runtime_value)
        if (
            manifest.output_root != str(arguments.sequence_output_root)
            or manifest.runtime_config_sha256 != arguments.confirm_runtime_config_sha256
            or production_runtime_config_sha256(runtime_config)
            != arguments.confirm_runtime_config_sha256
            or manifest.pricing_sha256 != arguments.confirm_pricing_sha256
            or manifest.sentinel_config_sha256 != arguments.confirm_sentinel_config_sha256
            or type(manifest.max_post_run_integrity_wall_time_seconds) is not int
        ):
            raise R25PostRunIntegrityError(
                "GATE_AUTHORITY_BINDING_MISMATCH",
                "manifest runtime, pricing, config, output, or wall binding differs",
            )
        resolved_inputs_sha256 = resolved_pilot_task_inputs_sha256(
            resolve_pilot_task_inputs_v1(
                manifest.pilot,
                authorized_input_root=runtime_config.authorized_pilot_input_root,
                repository_root=REPOSITORY_ROOT,
            )
        )
        authority = PostRunIntegrityAuthorityV1(
            run_id=manifest.run_id,
            authority_manifest_sha256=manifest_sha256,
            preflight_report_sha256=arguments.confirm_preflight_report_sha256,
            runtime_config_sha256=arguments.confirm_runtime_config_sha256,
            pricing_sha256=arguments.confirm_pricing_sha256,
            sentinel_config_sha256=arguments.confirm_sentinel_config_sha256,
            factory_binding_sha256=arguments.confirm_factory_binding_sha256,
            run_manifest=manifest,
            source_commit=manifest.source_commit,
            pilot_manifest=manifest.pilot,
            pilot_manifest_sha256=frozen_pilot_manifest_sha256(manifest.pilot),
            resolved_pilot_inputs_sha256=resolved_inputs_sha256,
            backend_endpoint=f"http://127.0.0.1:{runtime_config.backend_port}",
            expected_cell_count=len(manifest.pilot.cells),
            max_sequence_wall_time_seconds=manifest.max_sequence_wall_time_seconds,
            max_wall_time_seconds=manifest.max_post_run_integrity_wall_time_seconds,
            production_audit_root=(
                None
                if arguments.production_audit_root is None
                else str(arguments.production_audit_root)
            ),
        )
        _, artifact_sha256, artifact_path = run_post_run_integrity_gate_v1(
            sequence_output_root=arguments.sequence_output_root,
            repository_root=REPOSITORY_ROOT,
            authority=authority,
            sequence_started_monotonic_ns=(arguments.confirm_sequence_started_monotonic_ns),
            sequence_deadline_monotonic_ns=(arguments.confirm_sequence_deadline_monotonic_ns),
        )
        _, reopened_sha256 = reopen_validate_post_run_integrity_artifact_v1(
            artifact_path,
            repository_root=REPOSITORY_ROOT,
            authority=authority,
            rerun_official_checker=False,
        )
        if reopened_sha256 != artifact_sha256:
            raise R25PostRunIntegrityError(
                "INTEGRITY_ACCEPTANCE_PUBLICATION_FAILED", "acceptance hash differs"
            )
    except (LiveRunContractError, R25PostRunIntegrityError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "R25_POST_RUN_INTEGRITY_FAILED")
        print(json.dumps({"error_code": code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact": str(artifact_path),
                "artifact_sha256": artifact_sha256,
                "collector_run_count": len(manifest.pilot.cells) + 6,
                "pilot_collector_run_count": len(manifest.pilot.cells),
                "smoke_collector_run_count": 6,
                "ok": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
