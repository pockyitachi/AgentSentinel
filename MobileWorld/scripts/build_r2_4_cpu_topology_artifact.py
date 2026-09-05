#!/usr/bin/env python3
"""Build one real-component, CPU/fake R2.4 topology artifact."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError, canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    r24_cpu_topology_artifact_projection,
)
from mobile_world.runtime.sentinel.r2_4.topology_cpu import produce_cpu_fake_topology_comparison

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real R2.3 session and GPT56 policy against injected CPU fakes, "
            "then write one fresh canonical topology comparison artifact."
        )
    )
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _outside_repository(output: Path, repository_root: Path) -> Path:
    if not output.is_absolute():
        raise R24ContractError("INVALID_OUTPUT_PATH", "output path must be absolute")
    try:
        repository = repository_root.resolve(strict=True)
        parent = output.parent.resolve(strict=True)
        parent.relative_to(repository)
    except ValueError:
        return parent / output.name
    except OSError as exc:
        raise R24ContractError("INVALID_OUTPUT_PATH", "output parent is unavailable") from exc
    raise R24ContractError("REPOSITORY_OUTPUT_FORBIDDEN", "topology artifact must stay outside Git")


def _write_fresh(path: Path, payload: bytes) -> None:
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        parent_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise R24ContractError("TOPOLOGY_ARTIFACT_WRITE_FAILED", "fresh write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        output = _outside_repository(arguments.output, arguments.repository_root)
        if output.exists() or output.is_symlink():
            raise R24ContractError("OUTPUT_NOT_FRESH", "output path already exists")
        result = produce_cpu_fake_topology_comparison(
            repository_root=arguments.repository_root.resolve(strict=True)
        )
        payload = canonical_json_bytes(
            cast(JsonValue, r24_cpu_topology_artifact_projection(result))
        )
        _write_fresh(output, payload)
    except (OSError, R24ContractError, ValueError) as exc:
        sys.stderr.buffer.write(
            canonical_json_bytes(
                {
                    "error_code": getattr(exc, "code", "CPU_TOPOLOGY_BUILD_FAILED"),
                    "ok": False,
                }
            )
            + b"\n"
        )
        return 2
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "artifact_byte_count": len(payload),
                "artifact_path": str(output),
                "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                "isolated_component_calls": {
                    "comparison_provider_dispatches": (
                        result.isolated_components.comparison_provider_dispatches
                    ),
                    "history_policy_admission_adapter": (
                        result.isolated_components.policy_admission_adapter_calls
                    ),
                    "setup_rubric_provider": (
                        result.isolated_components.setup_rubric_provider_calls
                    ),
                },
                "joint_component_calls": {
                    "comparison_provider_dispatches": (
                        result.joint_components.comparison_provider_dispatches
                    ),
                    "history_policy_admission_adapter": (
                        result.joint_components.policy_admission_adapter_calls
                    ),
                    "setup_rubric_provider": result.joint_components.setup_rubric_provider_calls,
                },
                "joint_failure_probe": {
                    "failure_coupled": result.joint_failure_probe.failure_coupled,
                    "history_policy_output_admitted": (
                        result.joint_failure_probe.history_policy_output_admitted
                    ),
                    "provider_dispatches": result.joint_failure_probe.provider_dispatches,
                    "rubric_output_admitted": (result.joint_failure_probe.rubric_output_admitted),
                },
                "ok": True,
                "resource_census": {
                    "actor_actions": 0,
                    "backend_operations": 0,
                    "gpu_operations": 0,
                    "network_calls": 0,
                },
            }
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
