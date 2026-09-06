#!/usr/bin/env python3
"""Preflight or execute the owner-authorized R2.4/R2.5 sequence.

Dry-run is the default and performs no network, GPU, Docker, model, backend,
secret-read, or actor-action operation. ``--execute`` is reachable only after
the operator supplies four exact hash confirmations and a recent, reproducible
deep-preflight timestamp. It then constructs only the checked-in sealed
production adapters; the CLI exposes no callback, command, or client injection.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LIVE_ATTEMPT_PRICING_SCHEMA_VERSION,
    LiveAttemptPricingV1,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    ProductionR24R25ExecutorV1,
    build_production_executor_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    LiveRunContractError,
    R24R25RunAuthorityManifestV1,
    SequenceRunResultV1,
    authority_manifest_sha256,
    inspect_local_resources,
    load_authority_manifest,
    load_owner_authorized_authority_manifest_v2,
    preflight_report_projection,
    production_sentinel_config_sha256_v1,
    run_authorized_sequence_with_executor,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionDriverError,
    ProductionResourceLifecycleAdapterV1,
    ProductionRuntimeConfigV1,
    build_production_case_authority_broker_provider_v1,
    build_production_driver_v1,
    build_production_resource_lifecycle_adapter_v1,
    parse_production_runtime_config,
    production_runtime_config_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    production_preflight_report_projection,
    production_preflight_report_sha256,
    require_production_post_preflight_factory_v1,
    run_production_preflight_v1,
)
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
_MAX_CONFIG_BYTES = 1_048_576
_MAX_PREFLIGHT_AGE_SECONDS = 300


class _CliContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "R2.4-live/R2.5-pilot preflight and owner-authorized production runner. "
            "The default is zero-I/O dry-run beyond declared local file metadata/content."
        )
    )
    parser.add_argument("--authority-manifest", required=True, type=Path)
    parser.add_argument(
        "--deep-snapshot-hash",
        action="store_true",
        help="Hash declared model files on CPU; never loads a model or accesses the secret.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only; this is also the behavior when neither mode flag is supplied.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Run only through the exact sealed production executor and all owner pins.",
    )
    parser.add_argument("--confirm-manifest-sha256")
    parser.add_argument(
        "--preflight-checked-at-utc",
        help=(
            "Exact UTC second used for reproducible production preflight, for example "
            "2026-09-03T04:00:00Z. Required for --execute."
        ),
    )
    parser.add_argument("--confirm-preflight-report-sha256")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--confirm-runtime-config-sha256")
    parser.add_argument("--pricing", type=Path)
    parser.add_argument("--confirm-pricing-sha256")
    parser.add_argument("--production-audit-root", type=Path)
    return parser


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _load_small_json(path: Path, *, require_owner_canonical: bool = False) -> JsonValue:
    descriptor = -1
    directory_descriptors: list[tuple[int, str, int, os.stat_result]] = []
    try:
        if require_owner_canonical:
            if not path.is_absolute() or path.anchor != "/" or path.name in {"", ".", ".."}:
                raise ValueError("owner input path is not one canonical absolute path")
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            parent_fd = os.open("/", directory_flags)
            directory_descriptors.append((-1, "/", parent_fd, os.fstat(parent_fd)))
            for component in path.parts[1:-1]:
                if component in {"", ".", ".."}:
                    raise ValueError("owner input path has an invalid component")
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                child_metadata = os.fstat(child_fd)
                if not stat.S_ISDIR(child_metadata.st_mode):
                    raise ValueError("owner input ancestor is not a directory")
                directory_descriptors.append((parent_fd, component, child_fd, child_metadata))
                parent_fd = child_fd
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        else:
            before = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(before.st_mode):
                raise ValueError("JSON input is not a bounded regular file")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not 0 < opened.st_size <= _MAX_CONFIG_BYTES
            or (before.st_dev, before.st_ino, before.st_uid)
            != (opened.st_dev, opened.st_ino, opened.st_uid)
            or (
                require_owner_canonical
                and (
                    stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_uid != os.geteuid()
                    or opened.st_gid != os.getegid()
                    or opened.st_nlink != 1
                )
            )
        ):
            raise ValueError("JSON input identity differs")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("JSON input was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("JSON input grew while being read")
        after = os.fstat(descriptor)
        if require_owner_canonical:
            parent_fd = directory_descriptors[-1][2]
            rebound = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            for ancestor_fd, component, child_fd, original in directory_descriptors[1:]:
                current = os.stat(component, dir_fd=ancestor_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise ValueError("owner input ancestor changed while being read")
                if (os.fstat(child_fd).st_dev, os.fstat(child_fd).st_ino) != (
                    original.st_dev,
                    original.st_ino,
                ):
                    raise ValueError("owner input ancestor descriptor changed")
        else:
            rebound = os.lstat(path)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        ) or (after.st_dev, after.st_ino, after.st_uid, after.st_size, after.st_mtime_ns) != (
            rebound.st_dev,
            rebound.st_ino,
            rebound.st_uid,
            rebound.st_size,
            rebound.st_mtime_ns,
        ):
            raise ValueError("JSON input changed while being read")
        raw = b"".join(chunks)
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        if require_owner_canonical and canonical_json_bytes(cast(JsonValue, decoded)) != raw:
            raise ValueError("owner input is not exact canonical JSON")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _CliContractError("INVALID_EXECUTION_INPUT") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for _, _, directory_fd, _ in reversed(directory_descriptors):
            os.close(directory_fd)
    if type(decoded) is not dict:
        raise _CliContractError("INVALID_EXECUTION_INPUT")
    return cast(JsonValue, decoded)


def _load_cli_authority(
    path: Path,
    *,
    production_confirmation_requested: bool,
    confirmed_manifest_sha256: str | None,
) -> R24R25RunAuthorityManifestV1:
    if not production_confirmation_requested:
        return load_authority_manifest(path)
    if type(confirmed_manifest_sha256) is not str:
        raise _CliContractError("MANIFEST_CONFIRMATION_MISMATCH")
    return load_owner_authorized_authority_manifest_v2(
        path,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
    )


def _parse_utc_second(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise _CliContractError("INVALID_PREFLIGHT_TIMESTAMP") from exc


def _parse_pricing(value: JsonValue) -> LiveAttemptPricingV1:
    if type(value) is not dict:
        raise _CliContractError("INVALID_PRICING")
    expected = {
        "cached_input_usd_micros_per_million_tokens",
        "effective_at_utc",
        "input_usd_micros_per_million_tokens",
        "model",
        "output_usd_micros_per_million_tokens",
        "pricing_id",
        "rounding_policy",
        "schema_version",
        "source_sha256",
    }
    if set(value) != expected or value.get("schema_version") != LIVE_ATTEMPT_PRICING_SCHEMA_VERSION:
        raise _CliContractError("INVALID_PRICING")
    try:
        return LiveAttemptPricingV1(
            pricing_id=cast(str, value["pricing_id"]),
            model=cast(str, value["model"]),
            input_usd_micros_per_million_tokens=cast(
                int, value["input_usd_micros_per_million_tokens"]
            ),
            cached_input_usd_micros_per_million_tokens=cast(
                int, value["cached_input_usd_micros_per_million_tokens"]
            ),
            output_usd_micros_per_million_tokens=cast(
                int, value["output_usd_micros_per_million_tokens"]
            ),
            source_sha256=cast(str, value["source_sha256"]),
            effective_at_utc=cast(str, value["effective_at_utc"]),
            rounding_policy=cast(str, value["rounding_policy"]),
            schema_version=cast(str, value["schema_version"]),
        )
    except (TypeError, ValueError) as exc:
        raise _CliContractError("INVALID_PRICING") from exc


def _require_execute_arguments(arguments: argparse.Namespace) -> None:
    required = (
        arguments.confirm_manifest_sha256,
        arguments.preflight_checked_at_utc,
        arguments.confirm_preflight_report_sha256,
        arguments.runtime_config,
        arguments.confirm_runtime_config_sha256,
        arguments.pricing,
        arguments.confirm_pricing_sha256,
        arguments.production_audit_root,
    )
    if any(value is None for value in required):
        raise _CliContractError("EXECUTE_ARGUMENTS_REQUIRED")


def _load_confirmed_runtime(
    arguments: argparse.Namespace,
    manifest: R24R25RunAuthorityManifestV1,
) -> tuple[
    ProductionRuntimeConfigV1,
    str,
    ProductionResourceLifecycleAdapterV1,
    int,
    str,
]:
    if arguments.runtime_config is None or arguments.confirm_runtime_config_sha256 is None:
        raise _CliContractError("RUNTIME_CONFIRMATION_REQUIRED")
    runtime_config = parse_production_runtime_config(
        _load_small_json(cast(Path, arguments.runtime_config), require_owner_canonical=True)
    )
    if Path(runtime_config.repository_root).resolve(strict=True) != REPOSITORY_ROOT.resolve(
        strict=True
    ):
        raise _CliContractError("RUNTIME_REPOSITORY_MISMATCH")
    runtime_sha256 = production_runtime_config_sha256(runtime_config)
    if arguments.confirm_runtime_config_sha256 != runtime_sha256:
        raise _CliContractError("RUNTIME_CONFIG_CONFIRMATION_MISMATCH")
    resource_adapter = build_production_resource_lifecycle_adapter_v1(
        runtime_config,
        confirmed_config_sha256=runtime_sha256,
    )
    cleanup_seconds = resource_adapter.full_cleanup_upper_bound_seconds
    cleanup_sha256 = resource_adapter.full_cleanup_upper_bound_sha256
    if (
        runtime_sha256 != manifest.runtime_config_sha256
        or runtime_config.resource_topology.value != manifest.resource_topology
        or cleanup_seconds != manifest.max_resource_cleanup_wall_time_seconds
        or cleanup_sha256 != manifest.resource_cleanup_upper_bound_sha256
    ):
        raise _CliContractError("RUNTIME_AUTHORITY_BINDING_MISMATCH")
    return runtime_config, runtime_sha256, resource_adapter, cleanup_seconds, cleanup_sha256


def _load_confirmed_pricing(
    arguments: argparse.Namespace,
    manifest: R24R25RunAuthorityManifestV1,
) -> tuple[LiveAttemptPricingV1, str]:
    if arguments.pricing is None or arguments.confirm_pricing_sha256 is None:
        raise _CliContractError("PRICING_CONFIRMATION_REQUIRED")
    pricing = _parse_pricing(
        _load_small_json(cast(Path, arguments.pricing), require_owner_canonical=True)
    )
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    if (
        arguments.confirm_pricing_sha256 != pricing_sha256
        or pricing_sha256 != manifest.pricing_sha256
    ):
        raise _CliContractError("PRICING_CONFIRMATION_MISMATCH")
    return pricing, pricing_sha256


def _build_production_executor(
    arguments: argparse.Namespace,
    manifest: R24R25RunAuthorityManifestV1,
    *,
    manifest_sha256: str,
    preflight_now: datetime,
) -> tuple[
    ProductionR24R25ExecutorV1,
    dict[str, JsonValue],
    ProductionRuntimeConfigV1,
    Path,
]:
    (
        runtime_config,
        runtime_sha256,
        resource_adapter,
        cleanup_seconds,
        cleanup_sha256,
    ) = _load_confirmed_runtime(arguments, manifest)
    pricing, pricing_sha256 = _load_confirmed_pricing(arguments, manifest)
    sentinel_config_sha256 = production_sentinel_config_sha256_v1()
    if sentinel_config_sha256 != manifest.sentinel_config_sha256:
        raise _CliContractError("SENTINEL_CONFIG_CONFIRMATION_MISMATCH")
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        repository_root=REPOSITORY_ROOT,
        now=preflight_now,
        confirmed_runtime_config_sha256=runtime_sha256,
        confirmed_pricing_sha256=pricing_sha256,
        confirmed_sentinel_config_sha256=sentinel_config_sha256,
        confirmed_resource_topology=runtime_config.resource_topology.value,
        confirmed_resource_cleanup_upper_bound_seconds=cleanup_seconds,
        confirmed_resource_cleanup_upper_bound_sha256=cleanup_sha256,
    )
    report_sha256 = production_preflight_report_sha256(report)
    if arguments.confirm_preflight_report_sha256 != report_sha256:
        raise _CliContractError("PREFLIGHT_REPORT_CONFIRMATION_MISMATCH")

    factory = require_production_post_preflight_factory_v1(
        manifest,
        report,
        confirmed_manifest_sha256=manifest_sha256,
        confirmed_preflight_report_sha256=report_sha256,
        confirmed_pricing_sha256=pricing_sha256,
    )
    production_audit_root = cast(Path, arguments.production_audit_root)
    expected_audit_root = Path(runtime_config.process_log_root).parent / "audit"
    if not production_audit_root.is_absolute() or production_audit_root != expected_audit_root:
        raise _CliContractError("PRODUCTION_AUDIT_ROOT_BINDING_MISMATCH")
    audit_sink = ExternalProductionRuntimeAuditSinkV1(
        production_audit_root, repository_root=REPOSITORY_ROOT
    )
    driver_adapters = build_production_driver_v1(
        factory=factory,
        runtime_config=runtime_config,
        confirmed_runtime_config_sha256=runtime_sha256,
        pricing=pricing,
        confirmed_pricing_sha256=pricing_sha256,
        production_audit_sink=audit_sink,
        resource_lifecycle=resource_adapter,
    )
    broker_provider = build_production_case_authority_broker_provider_v1(factory)
    executor = build_production_executor_v1(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        factory=factory,
        confirmed_runtime_config_sha256=runtime_sha256,
        repository_root=REPOSITORY_ROOT,
        resource_adapter=resource_adapter,
        driver_adapters=driver_adapters,
        case_authority_broker_provider=broker_provider,
    )
    return (
        executor,
        production_preflight_report_projection(report),
        runtime_config,
        production_audit_root,
    )


def _sequence_projection(value: SequenceRunResultV1) -> dict[str, JsonValue]:
    return {
        "failed_stage": None if value.failed_stage is None else value.failed_stage.value,
        "failure_code": value.failure_code,
        "manifest_sha256": value.manifest_sha256,
        "receipts": [
            {
                "actor_actions": receipt.actor_actions,
                "actor_calls": receipt.actor_calls,
                "completed_units": list(receipt.completed_units),
                "cost_usd_micros": receipt.cost_usd_micros,
                "evidence_sha256": receipt.evidence_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "openai_calls": receipt.openai_calls,
                "passed": receipt.passed,
                "provider_final_request_proven": receipt.provider_final_request_proven,
                "stage": receipt.stage.value,
                "wall_time_ms": receipt.wall_time_ms,
            }
            for receipt in value.receipts
        ],
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "status": value.status.value,
    }


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if type(code) is str and code else "PRODUCTION_SETUP_FAILED"


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = _load_cli_authority(
            arguments.authority_manifest,
            production_confirmation_requested=(
                arguments.execute or arguments.preflight_checked_at_utc is not None
            ),
            confirmed_manifest_sha256=arguments.confirm_manifest_sha256,
        )
        manifest_hash = authority_manifest_sha256(manifest)
        if arguments.execute:
            _require_execute_arguments(arguments)
            if arguments.confirm_manifest_sha256 != manifest_hash:
                raise _CliContractError("MANIFEST_CONFIRMATION_MISMATCH")
            preflight_now = _parse_utc_second(arguments.preflight_checked_at_utc)
            assert preflight_now is not None
            if (
                abs((datetime.now(UTC) - preflight_now).total_seconds())
                > _MAX_PREFLIGHT_AGE_SECONDS
            ):
                raise _CliContractError("PREFLIGHT_TIMESTAMP_NOT_CURRENT")
            (
                executor,
                production_preflight,
                runtime_config,
                production_audit_root,
            ) = _build_production_executor(
                arguments,
                manifest,
                manifest_sha256=manifest_hash,
                preflight_now=preflight_now,
            )
            resolved_pilot_inputs_sha256 = resolved_pilot_task_inputs_sha256(
                resolve_pilot_task_inputs_v1(
                    manifest.pilot,
                    authorized_input_root=runtime_config.authorized_pilot_input_root,
                    repository_root=REPOSITORY_ROOT,
                )
            )
            result = run_authorized_sequence_with_executor(
                manifest,
                executor,
                confirmed_manifest_sha256=manifest_hash,
            )
            integrity_output: dict[str, JsonValue] | None = None
            if result.status.value == "COMPLETE":
                pricing_sha256 = manifest.pricing_sha256
                sentinel_config_sha256 = manifest.sentinel_config_sha256
                max_integrity_wall_time_seconds = manifest.max_post_run_integrity_wall_time_seconds
                if (
                    type(pricing_sha256) is not str
                    or type(sentinel_config_sha256) is not str
                    or type(max_integrity_wall_time_seconds) is not int
                ):
                    raise _CliContractError("V2_INTEGRITY_AUTHORITY_BINDING_MISSING")
                gate_authority = PostRunIntegrityAuthorityV1(
                    run_id=manifest.run_id,
                    source_commit=manifest.source_commit,
                    authority_manifest_sha256=manifest_hash,
                    preflight_report_sha256=executor.preflight_report_sha256,
                    runtime_config_sha256=executor.runtime_config_sha256,
                    pricing_sha256=pricing_sha256,
                    sentinel_config_sha256=sentinel_config_sha256,
                    factory_binding_sha256=executor.factory_binding_sha256,
                    run_manifest=manifest,
                    pilot_manifest=manifest.pilot,
                    pilot_manifest_sha256=frozen_pilot_manifest_sha256(manifest.pilot),
                    resolved_pilot_inputs_sha256=resolved_pilot_inputs_sha256,
                    backend_endpoint=f"http://127.0.0.1:{runtime_config.backend_port}",
                    expected_cell_count=len(manifest.pilot.cells),
                    max_sequence_wall_time_seconds=(manifest.max_sequence_wall_time_seconds),
                    max_wall_time_seconds=max_integrity_wall_time_seconds,
                    production_audit_root=str(production_audit_root),
                )
                integrity_artifact, integrity_sha256, integrity_path = (
                    run_post_run_integrity_gate_v1(
                        sequence_output_root=executor.post_run_integrity_output_root,
                        repository_root=REPOSITORY_ROOT,
                        authority=gate_authority,
                        sequence_started_monotonic_ns=(executor.sequence_started_monotonic_ns),
                        sequence_deadline_monotonic_ns=(
                            executor.post_run_integrity_deadline_monotonic_ns
                        ),
                    )
                )
                reopened, reopened_sha256 = reopen_validate_post_run_integrity_artifact_v1(
                    integrity_path,
                    repository_root=REPOSITORY_ROOT,
                    authority=gate_authority,
                    rerun_official_checker=False,
                )
                actual_sequence_wall_time_ms = (
                    time.monotonic_ns() - executor.sequence_started_monotonic_ns + 999_999
                ) // 1_000_000
                if (
                    reopened != integrity_artifact
                    or reopened_sha256 != integrity_sha256
                    or time.monotonic_ns() >= executor.post_run_integrity_deadline_monotonic_ns
                    or actual_sequence_wall_time_ms > manifest.max_sequence_wall_time_seconds * 1000
                ):
                    raise _CliContractError("POST_RUN_INTEGRITY_REOPEN_MISMATCH")
                integrity_output = {
                    "artifact_path": str(integrity_path),
                    "artifact_sha256": integrity_sha256,
                    "collector_run_count": cast(int, integrity_artifact["collector_run_count"]),
                    "ordered_collector_integrity_root_sha256": cast(
                        str, integrity_artifact["ordered_collector_integrity_root_sha256"]
                    ),
                    "ordered_pilot_collector_integrity_root_sha256": cast(
                        str,
                        integrity_artifact["ordered_pilot_collector_integrity_root_sha256"],
                    ),
                    "ordered_smoke_collector_integrity_root_sha256": cast(
                        str,
                        integrity_artifact["ordered_smoke_collector_integrity_root_sha256"],
                    ),
                    "pilot_collector_run_count": cast(
                        int, integrity_artifact["pilot_collector_run_count"]
                    ),
                    "smoke_collector_run_count": cast(
                        int, integrity_artifact["smoke_collector_run_count"]
                    ),
                    "status": cast(str, integrity_artifact["status"]),
                    "total_sequence_wall_time_ms_at_cli_reopen": (actual_sequence_wall_time_ms),
                }
            ok = result.status.value == "COMPLETE" and integrity_output is not None
            print(
                json.dumps(
                    {
                        "dry_run": False,
                        "integrity": integrity_output,
                        "factory_binding_sha256": executor.factory_binding_sha256,
                        "manifest_sha256": manifest_hash,
                        "ok": ok,
                        "preflight": production_preflight,
                        "preflight_report_sha256": arguments.confirm_preflight_report_sha256,
                        "pricing_sha256": manifest.pricing_sha256,
                        "resolved_pilot_inputs_sha256": resolved_pilot_inputs_sha256,
                        "result": _sequence_projection(result),
                        "runtime_config_sha256": executor.runtime_config_sha256,
                        "sentinel_config_sha256": manifest.sentinel_config_sha256,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0 if ok else 3

        base_report = inspect_local_resources(
            manifest,
            repo_root=REPOSITORY_ROOT,
            deep_snapshot_hashes=arguments.deep_snapshot_hash,
        )
        output: dict[str, Any] = {
            "dry_run": True,
            "manifest_sha256": manifest_hash,
            "ok": True,
            "preflight": preflight_report_projection(base_report),
        }
        preflight_now = _parse_utc_second(arguments.preflight_checked_at_utc)
        if preflight_now is not None:
            if arguments.confirm_manifest_sha256 != manifest_hash:
                raise _CliContractError("MANIFEST_CONFIRMATION_MISMATCH")
            (
                runtime_config,
                runtime_sha256,
                _resource_adapter,
                cleanup_seconds,
                cleanup_sha256,
            ) = _load_confirmed_runtime(arguments, manifest)
            _pricing, pricing_sha256 = _load_confirmed_pricing(arguments, manifest)
            sentinel_config_sha256 = production_sentinel_config_sha256_v1()
            if sentinel_config_sha256 != manifest.sentinel_config_sha256:
                raise _CliContractError("SENTINEL_CONFIG_CONFIRMATION_MISMATCH")
            report = run_production_preflight_v1(
                manifest,
                confirmed_manifest_sha256=manifest_hash,
                repository_root=REPOSITORY_ROOT,
                now=preflight_now,
                confirmed_runtime_config_sha256=runtime_sha256,
                confirmed_pricing_sha256=pricing_sha256,
                confirmed_sentinel_config_sha256=sentinel_config_sha256,
                confirmed_resource_topology=runtime_config.resource_topology.value,
                confirmed_resource_cleanup_upper_bound_seconds=cleanup_seconds,
                confirmed_resource_cleanup_upper_bound_sha256=cleanup_sha256,
            )
            output["production_preflight"] = production_preflight_report_projection(report)
            output["production_preflight_report_sha256"] = production_preflight_report_sha256(
                report
            )
            output["runtime_config_sha256"] = runtime_sha256
            output["pricing_sha256"] = pricing_sha256
            output["sentinel_config_sha256"] = sentinel_config_sha256
        print(
            json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (
        LiveRunContractError,
        ProductionDriverError,
        R25PostRunIntegrityError,
        _CliContractError,
    ) as exc:
        # Never dump an environment, manifest, Authorization header, secret or path.
        print(
            json.dumps({"error_code": _error_code(exc), "ok": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        # Unexpected setup failures remain fully redacted on the public channel.
        print(
            json.dumps({"error_code": "PRODUCTION_SETUP_FAILED", "ok": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
