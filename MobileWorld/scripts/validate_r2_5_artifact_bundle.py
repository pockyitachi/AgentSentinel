#!/usr/bin/env python3
"""Independently reopen a durable seven-file R2.5 authority bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    R25ArtifactBuildError,
    validate_written_artifact_bundle_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import R25PilotContractError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reopen and validate all seven R2.5 bundle files, their complete checked-in "
            "schemas, exact hashes/byte counts, current task registry, deterministic cohort, "
            "task resolver, authority, and topology. No bytes are repaired or removed."
        )
    )
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        validation = validate_written_artifact_bundle_v1(
            arguments.bundle_dir,
            repository_root=arguments.repository_root,
            expected_source_commit=arguments.source_commit,
        )
    except (R25ArtifactBuildError, R25PilotContractError) as exc:
        sys.stderr.buffer.write(
            canonical_json_bytes(
                {
                    "error_code": getattr(exc, "code", "ARTIFACT_READBACK_FAILED"),
                    "ok": False,
                }
            )
            + b"\n"
        )
        return 2
    result: dict[str, JsonValue] = {
        "artifact_bundle_sha256": validation.artifact_bundle_sha256,
        "artifact_count": validation.artifact_count,
        "bundle_directory": validation.bundle_directory,
        "cohort_size": validation.cohort_size,
        "gui_only_task_source_sha256": validation.gui_only_task_source_sha256,
        "ok": True,
        "registry_sha256": validation.registry_sha256,
        "source_task_count": validation.source_task_count,
    }
    sys.stdout.buffer.write(canonical_json_bytes(cast(JsonValue, result)) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
