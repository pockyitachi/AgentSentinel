#!/usr/bin/env python3
"""Freeze an exact GUI-117 task-name catalog as new R2.5 trial-one inputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE,
    R25ArtifactBuildError,
    freeze_gui_only_task_source_from_historical_manifest_v1,
    write_fresh_gui_only_task_source_freeze_receipt_v1,
    write_fresh_gui_only_task_source_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import R25PilotContractError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract only the ordered task names from one exact, hash-bound historical "
            "GUI-117 curated manifest and publish canonical R2.5 task parameters. Trial 1 "
            "is a new R2.5 declaration, not a historical fact. This command is CPU-only and "
            "performs no registry, backend, network, GPU, model, secret, or GUI operation."
        )
    )
    parser.add_argument("--historical-manifest", required=True, type=Path)
    parser.add_argument("--expected-historical-manifest-sha256", required=True)
    parser.add_argument("--expected-historical-manifest-byte-count", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-receipt-output", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--declare-r25-trial-one",
        action="store_true",
        help=(
            "Required acknowledgement that trial=1 is a new R2.5 initialization input and "
            "is not copied, inferred, or derived from the historical runs."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.declare_r25_trial_one:
        sys.stderr.buffer.write(
            canonical_json_bytes(
                {
                    "error_code": "R25_TRIAL_DECLARATION_REQUIRED",
                    "ok": False,
                }
            )
            + b"\n"
        )
        return 2
    try:
        source = freeze_gui_only_task_source_from_historical_manifest_v1(
            arguments.historical_manifest,
            expected_manifest_sha256=arguments.expected_historical_manifest_sha256,
            expected_manifest_byte_count=arguments.expected_historical_manifest_byte_count,
        )
        output = write_fresh_gui_only_task_source_v1(
            source,
            arguments.output,
            repository_root=arguments.repository_root,
        )
        receipt_output = write_fresh_gui_only_task_source_freeze_receipt_v1(
            source,
            arguments.freeze_receipt_output,
            repository_root=arguments.repository_root,
        )
    except (R25ArtifactBuildError, R25PilotContractError) as exc:
        sys.stderr.buffer.write(
            canonical_json_bytes(
                {
                    "error_code": getattr(exc, "code", "GUI_TASK_SOURCE_FREEZE_FAILED"),
                    "ok": False,
                }
            )
            + b"\n"
        )
        return 2
    result: dict[str, JsonValue] = {
        "execution_census": {
            "actor_model_calls": 0,
            "backend_operations": 0,
            "docker_operations": 0,
            "gpu_operations": 0,
            "gui_actions": 0,
            "network_calls": 0,
            "secret_content_reads": 0,
        },
        "historical_manifest_byte_count": source.historical_manifest_byte_count,
        "historical_manifest_path": source.historical_manifest_path,
        "historical_manifest_sha256": source.historical_manifest_sha256,
        "ok": True,
        "output_byte_count": len(source.task_source_bytes),
        "output_path": str(output),
        "output_sha256": source.task_source_sha256,
        "freeze_receipt_path": str(receipt_output),
        "task_count": source.task_count,
        "trial": source.trial,
        "trial_provenance": GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE,
    }
    sys.stdout.buffer.write(canonical_json_bytes(cast(JsonValue, result)) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
