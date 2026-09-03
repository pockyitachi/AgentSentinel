#!/usr/bin/env python3
"""Build deterministic CPU-only R2.4/R2.5 authority artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import LiveRunContractError
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    AuthorityArtifactInputsV1,
    R25ArtifactBuildError,
    SnapshotDeclarationV1,
    artifact_bundle_output,
    build_authority_artifact_bundle,
    current_registry_metadata,
    verify_current_source_commit,
    write_artifact_bundle,
)
from mobile_world.runtime.sentinel.r2_5.pilot import R25PilotContractError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _snapshot_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    label = prefix.replace("-", " ").title()
    parser.add_argument(f"--{prefix}-snapshot-path", required=True, type=Path)
    parser.add_argument(f"--{prefix}-snapshot-storage-root", required=True, type=Path)
    parser.add_argument(f"--{prefix}-snapshot-tree-sha256", required=True)
    parser.add_argument(f"--{prefix}-snapshot-total-bytes", required=True, type=int)
    parser.add_argument(f"--{prefix}-snapshot-file-count", required=True, type=int)
    parser.add_argument(f"--{prefix}-actor-endpoint", required=True)
    parser.add_argument(f"--{prefix}-served-model-id", required=True, help=f"{label} served ID")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select 20 local GUI-only tasks and build a DRAFT R2.4/R2.5 authority bundle. "
            "This command never reads the secret or uses network, GPU, Docker, or MobileWorld."
        )
    )
    parser.add_argument("--source-task-jsonl", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--bundle-dir",
        required=True,
        type=Path,
        help="Planned fresh repo-external artifact directory; created only with --write.",
    )
    parser.add_argument("--runtime-output-root", required=True, type=Path)
    parser.add_argument(
        "--secret-file",
        required=True,
        type=Path,
        help="Repo-external 0600 key-file reference. Its contents and metadata are not read.",
    )
    parser.add_argument(
        "--topology-comparison-artifact",
        required=True,
        type=Path,
        help=(
            "Canonical CPU/fake topology comparison JSON; strictly parsed, recomputed, "
            "and copied into the bundle. A bare hash is never accepted."
        ),
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--verify-current-source-commit",
        action="store_true",
        help="Require source_commit == local HEAD and a clean worktree (CPU/read-only).",
    )
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--frozen-at-utc", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--authorized-by", required=True)
    parser.add_argument("--issued-at-utc", required=True)
    parser.add_argument("--expires-at-utc", required=True)
    parser.add_argument("--qwen-smoke-fixture", required=True, type=Path)
    parser.add_argument("--mai-smoke-fixture", required=True, type=Path)
    parser.add_argument("--qwen-smoke-task-id", required=True)
    parser.add_argument("--mai-smoke-task-id", required=True)
    _snapshot_arguments(parser, "qwen")
    _snapshot_arguments(parser, "mai")
    parser.add_argument("--max-steps-per-cell", type=int, default=8)
    parser.add_argument("--per-cell-timeout-seconds", type=int, default=900)
    parser.add_argument("--max-total-wall-time-seconds", type=int, default=72_000)
    parser.add_argument("--max-total-cost-usd-micros", type=int, default=100_000_000)
    parser.add_argument("--smoke-wall-time-seconds", type=int, default=300)
    parser.add_argument("--smoke-cost-usd-micros", type=int, default=1_000_000)
    parser.add_argument("--resource-preflight-wall-time-seconds", type=int, default=3_600)
    parser.add_argument("--openai-timeout-ms", type=int, default=120_000)
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Create bundle-dir once and write six canonical JSON artifacts plus the exact "
            "bound GUI-only JSONL source, all with mode 0600."
        ),
    )
    return parser


def _snapshot(arguments: argparse.Namespace, prefix: str) -> SnapshotDeclarationV1:
    return SnapshotDeclarationV1(
        snapshot_path=str(getattr(arguments, f"{prefix}_snapshot_path")),
        snapshot_storage_root=str(getattr(arguments, f"{prefix}_snapshot_storage_root")),
        snapshot_tree_sha256=getattr(arguments, f"{prefix}_snapshot_tree_sha256"),
        snapshot_total_bytes=getattr(arguments, f"{prefix}_snapshot_total_bytes"),
        snapshot_file_count=getattr(arguments, f"{prefix}_snapshot_file_count"),
        actor_endpoint=getattr(arguments, f"{prefix}_actor_endpoint"),
        served_model_id=getattr(arguments, f"{prefix}_served_model_id"),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.verify_current_source_commit:
            verify_current_source_commit(arguments.repository_root, arguments.source_commit)
        bundle = build_authority_artifact_bundle(
            AuthorityArtifactInputsV1(
                source_task_jsonl=arguments.source_task_jsonl,
                repository_root=arguments.repository_root,
                bundle_directory=arguments.bundle_dir,
                runtime_output_root=arguments.runtime_output_root,
                secret_file=arguments.secret_file,
                topology_comparison_artifact=arguments.topology_comparison_artifact,
                qwen_snapshot=_snapshot(arguments, "qwen"),
                mai_snapshot=_snapshot(arguments, "mai"),
                qwen_smoke_fixture=arguments.qwen_smoke_fixture,
                mai_smoke_fixture=arguments.mai_smoke_fixture,
                qwen_smoke_task_id=arguments.qwen_smoke_task_id,
                mai_smoke_task_id=arguments.mai_smoke_task_id,
                source_commit=arguments.source_commit,
                cohort_id=arguments.cohort_id,
                run_id=arguments.run_id,
                frozen_at_utc=arguments.frozen_at_utc,
                authorization_id=arguments.authorization_id,
                authorized_by=arguments.authorized_by,
                issued_at_utc=arguments.issued_at_utc,
                expires_at_utc=arguments.expires_at_utc,
                max_steps_per_cell=arguments.max_steps_per_cell,
                per_cell_timeout_seconds=arguments.per_cell_timeout_seconds,
                max_total_wall_time_seconds=arguments.max_total_wall_time_seconds,
                max_total_cost_usd_micros=arguments.max_total_cost_usd_micros,
                smoke_wall_time_seconds=arguments.smoke_wall_time_seconds,
                smoke_cost_usd_micros=arguments.smoke_cost_usd_micros,
                resource_preflight_wall_time_seconds=(
                    arguments.resource_preflight_wall_time_seconds
                ),
                openai_timeout_ms=arguments.openai_timeout_ms,
            ),
            current_registry_metadata(),
        )
        if arguments.write:
            write_artifact_bundle(bundle, repository_root=arguments.repository_root)
        output = artifact_bundle_output(bundle)
    except (R25ArtifactBuildError, R25PilotContractError, LiveRunContractError) as exc:
        error_code = getattr(exc, "code", "ARTIFACT_BUILD_FAILED")
        sys.stderr.buffer.write(
            canonical_json_bytes({"error_code": error_code, "ok": False}) + b"\n"
        )
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(cast(JsonValue, output)) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
