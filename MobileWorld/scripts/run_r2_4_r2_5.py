#!/usr/bin/env python3
"""Preflight or execute the owner-authorized treatment-only R2.5 batches.

Dry-run is the default and performs no network, GPU, Docker, model, backend,
secret-read, or actor-action operation. ``--execute`` is reachable only after
the operator supplies four exact hash confirmations and a recent, reproducible
deep-preflight timestamp. It is an alias for ``--execute-with-tool-batches``:
fresh Qwen treatment suffix, cleanup, then all fresh MAI treatment cells.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.audit.integrity import check_run_integrity
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LIVE_ATTEMPT_PRICING_SCHEMA_VERSION,
    LiveAttemptPricingV1,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import StageAdapterContextV1
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    build_production_live_budget_ledger_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    LiveRunContractError,
    R24R25RunAuthorityManifestV1,
    authority_manifest_sha256,
    inspect_local_resources,
    load_authority_manifest,
    load_owner_authorized_authority_manifest_v2,
    preflight_report_projection,
    production_sentinel_config_sha256_v1,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionCaseAuthorityBrokerProviderV1,
    ProductionDriverError,
    ProductionResourceLifecycleAdapterV1,
    ProductionRuntimeConfigV1,
    WithToolJointBatchEvidenceV1,
    build_production_case_authority_broker_provider_v1,
    build_production_driver_v1,
    build_production_pilot_switch_authority_v1,
    build_production_resource_lifecycle_adapter_v1,
    parse_production_runtime_config,
    production_runtime_config_sha256,
    with_tool_joint_batch_evidence_projection,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    ProductionPostPreflightFactoryV1,
    production_preflight_report_projection,
    production_preflight_report_sha256,
    require_production_post_preflight_factory_v1,
    run_production_preflight_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    PilotHostV1,
    resolve_pilot_task_inputs_v1,
    resolved_pilot_task_inputs_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAX_CONFIG_BYTES = 1_048_576
_MAX_PREFLIGHT_AGE_SECONDS = 300
_WITH_TOOL_MAX_STEPS = 50


class _CliContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _WithToolExecutionSetup:
    factory: ProductionPostPreflightFactoryV1
    production_preflight: dict[str, JsonValue]
    runtime_config: ProductionRuntimeConfigV1
    runtime_config_sha256: str
    pricing: LiveAttemptPricingV1
    pricing_sha256: str
    audit_sink: ExternalProductionRuntimeAuditSinkV1
    audit_root: Path
    broker_provider: ProductionCaseAuthorityBrokerProviderV1
    first_resource_adapter: ProductionResourceLifecycleAdapterV1
    cleanup_seconds: int
    cleanup_sha256: str


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
        "--execute-with-tool-batches",
        "--execute-joint-batches",
        dest="execute_with_tool_batches",
        action="store_true",
        help=(
            "Run the historical-control treatment path: fresh Qwen backend/model for the "
            "selected ACTIVE 50-step suffix, cleanup, then fresh MAI backend/model for all "
            "20 cells. No smoke or fresh baseline is run. --execute is an alias for this mode."
        ),
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
    parser.add_argument(
        "--qwen-start-task-ordinal",
        type=int,
        default=1,
        help=(
            "One-based frozen-cohort task ordinal for the fresh Qwen batch; "
            "MAI always runs the complete cohort. Defaults to 1."
        ),
    )
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


def _build_execution_setup(
    arguments: argparse.Namespace,
    manifest: R24R25RunAuthorityManifestV1,
    *,
    manifest_sha256: str,
    preflight_now: datetime,
) -> _WithToolExecutionSetup:
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
    audit_root = cast(Path, arguments.production_audit_root)
    expected_audit_root = Path(runtime_config.process_log_root).parent / "audit"
    if not audit_root.is_absolute() or audit_root != expected_audit_root:
        raise _CliContractError("PRODUCTION_AUDIT_ROOT_BINDING_MISMATCH")
    audit_sink = ExternalProductionRuntimeAuditSinkV1(
        audit_root,
        repository_root=REPOSITORY_ROOT,
    )
    return _WithToolExecutionSetup(
        factory=factory,
        production_preflight=production_preflight_report_projection(report),
        runtime_config=runtime_config,
        runtime_config_sha256=runtime_sha256,
        pricing=pricing,
        pricing_sha256=pricing_sha256,
        audit_sink=audit_sink,
        audit_root=audit_root,
        broker_provider=build_production_case_authority_broker_provider_v1(factory),
        first_resource_adapter=resource_adapter,
        cleanup_seconds=cleanup_seconds,
        cleanup_sha256=cleanup_sha256,
    )


def _with_tool_collector_integrity_checks(
    projection: dict[str, JsonValue],
) -> list[dict[str, JsonValue]]:
    raw_cells = [
        *cast(list[JsonValue], projection["cells"]),
        *cast(list[JsonValue], projection.get("failed_cells", [])),
    ]
    checks: list[dict[str, JsonValue]] = []
    for raw_cell in raw_cells:
        cell = cast(dict[str, JsonValue], raw_cell)
        raw_locator = cell.get("collector_run_locator")
        if type(raw_locator) is not dict:
            continue
        locator = raw_locator
        collector_root = cast(str, locator["collector_run_root"])
        try:
            report = check_run_integrity(Path(collector_root), write_report=False)
        except Exception as error:  # noqa: BLE001 - preserve failed-cell evidence
            report = {
                "valid": False,
                "errors": [f"integrity_check_failed:{type(error).__name__}"],
                "warnings": [],
            }
        checks.append(
            {
                "collector_run_id": locator["collector_run_id"],
                "collector_run_root": collector_root,
                "report": cast(JsonValue, report),
                "sequence_index": cell["sequence_index"],
                "task_id": cell["task_id"],
            }
        )
    return checks


def _execute_with_tool_batches(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    manifest_sha256: str,
    setup: _WithToolExecutionSetup,
    qwen_start_task_ordinal: int = 1,
) -> dict[str, JsonValue]:
    task_count = len(manifest.pilot.tasks)
    if (
        task_count != 20
        or manifest.pilot.max_steps_per_cell != _WITH_TOOL_MAX_STEPS
        or type(qwen_start_task_ordinal) is not int
        or not 1 <= qwen_start_task_ordinal <= task_count
    ):
        raise _CliContractError("WITH_TOOL_BATCH_SHAPE_MISMATCH")
    resolved_pilot_inputs_sha256 = resolved_pilot_task_inputs_sha256(
        resolve_pilot_task_inputs_v1(
            manifest.pilot,
            authorized_input_root=setup.runtime_config.authorized_pilot_input_root,
            repository_root=REPOSITORY_ROOT,
        )
    )
    qwen_task_count = task_count - qwen_start_task_ordinal + 1
    batch_task_counts = (qwen_task_count, task_count)
    new_cell_count = sum(batch_task_counts)
    treatment_actor_cap = new_cell_count * _WITH_TOOL_MAX_STEPS
    treatment_openai_cap = 2 * treatment_actor_cap
    treatment_wall_time_cap_ms = new_cell_count * manifest.pilot.per_cell_timeout_seconds * 1_000

    started_ns = time.monotonic_ns()
    integrity_seconds = manifest.max_post_run_integrity_wall_time_seconds
    if type(integrity_seconds) is not int:
        raise _CliContractError("V2_WITH_TOOL_AUTHORITY_BINDING_MISSING")
    batch_work_seconds = tuple(
        manifest.max_resource_preflight_wall_time_seconds
        + count * manifest.pilot.per_cell_timeout_seconds
        for count in batch_task_counts
    )
    required_seconds = sum(batch_work_seconds) + 2 * setup.cleanup_seconds + integrity_seconds
    expires_at = datetime.strptime(
        manifest.authorization.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    if required_seconds > min(
        manifest.max_sequence_wall_time_seconds,
        int((expires_at - datetime.now(UTC)).total_seconds()),
    ):
        raise _CliContractError("WITH_TOOL_SEQUENCE_BUDGET_EXCEEDED")
    cursor_ns = started_ns
    deadlines: list[tuple[int, int, int]] = []
    for index, work_seconds in enumerate(batch_work_seconds):
        work_deadline_ns = cursor_ns + work_seconds * 1_000_000_000
        cleanup_deadline_ns = work_deadline_ns + setup.cleanup_seconds * 1_000_000_000
        integrity_share = integrity_seconds // 2 + (integrity_seconds % 2 if index else 0)
        integrity_deadline_ns = cleanup_deadline_ns + integrity_share * 1_000_000_000
        deadlines.append((work_deadline_ns, cleanup_deadline_ns, integrity_deadline_ns))
        cursor_ns = integrity_deadline_ns
    shared_budget_ledger = build_production_live_budget_ledger_v1(setup.factory)
    batches: list[dict[str, JsonValue]] = []
    batch_evidences: list[WithToolJointBatchEvidenceV1] = []
    backend_container_ids: list[str] = []
    failed_cell_count = 0
    attempted_cell_count = 0
    collector_integrity_all_valid = True

    for batch_index, host in enumerate((PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI)):
        work_deadline_ns, cleanup_deadline_ns, integrity_deadline_ns = deadlines[batch_index]
        batch_task_count = batch_task_counts[batch_index]
        task_ordinal_start = qwen_start_task_ordinal if batch_index == 0 else 1
        lifecycle = (
            setup.first_resource_adapter
            if batch_index == 0
            else build_production_resource_lifecycle_adapter_v1(
                setup.runtime_config,
                confirmed_config_sha256=setup.runtime_config_sha256,
            )
        )
        driver = build_production_driver_v1(
            factory=setup.factory,
            runtime_config=setup.runtime_config,
            confirmed_runtime_config_sha256=setup.runtime_config_sha256,
            pricing=setup.pricing,
            confirmed_pricing_sha256=setup.pricing_sha256,
            production_audit_sink=setup.audit_sink,
            resource_lifecycle=lifecycle,
            shared_budget_ledger=shared_budget_ledger,
        )
        switch_authority = build_production_pilot_switch_authority_v1(
            factory=setup.factory,
            resource_lifecycle=lifecycle,
            confirmed_runtime_config_sha256=setup.runtime_config_sha256,
            confirmed_cleanup_upper_bound_sha256=setup.cleanup_sha256,
        )
        batch_started_ns = time.monotonic_ns()
        context = StageAdapterContextV1(
            manifest_sha256=manifest_sha256,
            sequence_execution_scope="R24_R25_FULL",
            sequence_scope_authority_sha256=manifest_sha256,
            run_id=manifest.run_id,
            source_commit=manifest.source_commit,
            remaining_actor_calls=batch_task_count * _WITH_TOOL_MAX_STEPS,
            remaining_openai_calls=2 * batch_task_count * _WITH_TOOL_MAX_STEPS,
            remaining_cost_usd_micros=manifest.pilot.max_total_cost_usd_micros,
            remaining_wall_time_ms=(work_deadline_ns - batch_started_ns) // 1_000_000,
            authority_deadline_monotonic_ns=work_deadline_ns,
        )
        prepare_invoked = False
        try:
            prepare_invoked = True
            resource_result = lifecycle.prepare(
                manifest.actor_resources,
                context,
                pilot_switch_authority=switch_authority,
                with_tool_initial_host=host,
            )
            broker = setup.broker_provider.acquire(
                manifest.secret,
                manifest_sha256=manifest_sha256,
            )
            try:
                batch_result = driver.pilot.run_with_tool_joint_batch(
                    manifest.pilot,
                    manifest.actor_resources,
                    manifest.openai_stages,
                    context,
                    broker,
                    host=host,
                    start_task_ordinal=task_ordinal_start,
                )
            finally:
                broker.close()
            batch_evidence = driver.pilot.evidence
            resource_evidence = lifecycle.evidence
            if (
                type(batch_evidence) is not WithToolJointBatchEvidenceV1
                or resource_evidence is None
            ):
                raise _CliContractError("WITH_TOOL_BATCH_EVIDENCE_MISSING")
        finally:
            if prepare_invoked:
                lifecycle.cleanup(
                    replace(
                        context,
                        remaining_actor_calls=0,
                        remaining_openai_calls=0,
                        remaining_cost_usd_micros=0,
                        remaining_wall_time_ms=max(
                            0, (cleanup_deadline_ns - time.monotonic_ns()) // 1_000_000
                        ),
                        authority_deadline_monotonic_ns=cleanup_deadline_ns,
                    ),
                )
                cleanup_evidence = lifecycle.cleanup_success_evidence_preimage()
                if type(cleanup_evidence) is not bytes:
                    raise _CliContractError("WITH_TOOL_BATCH_CLEANUP_PROOF_MISSING")
                cleanup_sha256 = hashlib.sha256(cleanup_evidence).hexdigest()
        evidence_projection = with_tool_joint_batch_evidence_projection(batch_evidence)
        integrity_checks = _with_tool_collector_integrity_checks(evidence_projection)
        successful_cells = cast(list[JsonValue], evidence_projection["cells"])
        failed_cells = cast(list[JsonValue], evidence_projection.get("failed_cells", []))
        batch_attempted_count = len(successful_cells) + len(failed_cells)
        batch_failed_count = len(failed_cells)
        batch_integrity_valid = len(integrity_checks) == batch_attempted_count and all(
            type(check.get("report")) is dict
            and cast(dict[str, JsonValue], check["report"]).get("valid") is True
            and cast(dict[str, JsonValue], check["report"]).get("errors") == []
            and cast(dict[str, JsonValue], check["report"]).get("warnings") == []
            for check in integrity_checks
        )
        attempted_cell_count += batch_attempted_count
        failed_cell_count += batch_failed_count
        collector_integrity_all_valid = collector_integrity_all_valid and batch_integrity_valid
        if time.monotonic_ns() >= integrity_deadline_ns:
            raise _CliContractError("WITH_TOOL_COLLECTOR_INTEGRITY_TIMEOUT")
        batch_evidences.append(batch_evidence)
        backend_container_ids.append(resource_evidence.backend_container_id)
        batch_output: dict[str, JsonValue] = {
            "census": evidence_projection["census"],
            "cleanup_evidence_sha256": cleanup_sha256,
            "collector_integrity_checks": cast(JsonValue, integrity_checks),
            "evidence_sha256": batch_result.evidence_sha256,
            "evidence_projection": cast(JsonValue, evidence_projection),
            "host": host.value,
            "resource_evidence_sha256": resource_result.evidence_sha256,
            "wall_time_ms": (time.monotonic_ns() - batch_started_ns + 999_999) // 1_000_000,
        }
        if batch_failed_count or not batch_integrity_valid:
            batch_output.update(
                {
                    "attempted_cell_count": batch_attempted_count,
                    "collector_integrity_all_valid": batch_integrity_valid,
                    "failed_cell_count": batch_failed_count,
                    "successful_cell_count": len(successful_cells),
                }
            )
        if qwen_start_task_ordinal != 1:
            batch_output.update(
                {
                    "task_ordinal_end": task_count,
                    "task_ordinal_start": task_ordinal_start,
                    "with_tool_cell_count": batch_task_count,
                }
            )
        batches.append(batch_output)

    actor_calls = sum(item.census.actor_calls for item in batch_evidences)
    openai_calls = sum(item.census.openai_calls for item in batch_evidences)
    actor_actions = sum(item.census.actor_actions for item in batch_evidences)
    offline_rubric_evaluations = sum(
        item.census.offline_rubric_evaluations for item in batch_evidences
    )
    rubric_openai_calls = sum(item.census.rubric_openai_calls for item in batch_evidences)
    history_policy_openai_calls = sum(
        item.census.history_policy_openai_calls for item in batch_evidences
    )
    cost_usd_micros = sum(item.census.cost_usd_micros for item in batch_evidences)
    census_wall_time_ms = sum(item.census.wall_time_ms for item in batch_evidences)
    integrity_count = sum(
        len(cast(list[JsonValue], batch["collector_integrity_checks"])) for batch in batches
    )
    if (
        len(set(backend_container_ids)) != 2
        or attempted_cell_count != new_cell_count
        or integrity_count > attempted_cell_count
        or actor_calls > treatment_actor_cap
        or actor_calls > manifest.pilot.max_total_actor_calls
        or openai_calls > treatment_openai_cap
        or openai_calls > manifest.pilot.max_total_openai_calls
        or cost_usd_micros > manifest.pilot.max_total_cost_usd_micros
        or census_wall_time_ms > treatment_wall_time_cap_ms
        or census_wall_time_ms > manifest.pilot.max_total_wall_time_seconds * 1_000
    ):
        raise _CliContractError("WITH_TOOL_FRESH_BATCH_EVIDENCE_MISMATCH")
    total_wall_time_ms = (time.monotonic_ns() - started_ns + 999_999) // 1_000_000
    if total_wall_time_ms > manifest.max_sequence_wall_time_seconds * 1_000:
        raise _CliContractError("WITH_TOOL_SEQUENCE_BUDGET_EXCEEDED")
    output: dict[str, JsonValue] = {
        "audit_root": str(setup.audit_root),
        "batches": cast(JsonValue, batches),
        "census": {
            "actor_actions": actor_actions,
            "actor_calls": actor_calls,
            "cost_usd_micros": cost_usd_micros,
            "history_policy_openai_calls": history_policy_openai_calls,
            "offline_rubric_evaluations": offline_rubric_evaluations,
            "openai_calls": openai_calls,
            "rubric_openai_calls": rubric_openai_calls,
            "wall_time_ms": census_wall_time_ms,
        },
        "collector_integrity": {
            "all_valid": True,
            "run_count": integrity_count,
        },
        "comparison_design": "WITH_TOOL_VS_HISTORICAL_NONPAIRED",
        "factory_binding_sha256": setup.factory.factory_binding_sha256,
        "fresh_backend_container_count": len(set(backend_container_ids)),
        "fresh_baseline_cell_count": 0,
        "manifest_sha256": manifest_sha256,
        "preflight": cast(JsonValue, setup.production_preflight),
        "preflight_report_sha256": setup.factory.preflight_report_sha256,
        "pricing_sha256": setup.pricing_sha256,
        "resolved_pilot_inputs_sha256": resolved_pilot_inputs_sha256,
        "run_id": manifest.run_id,
        "runtime_config_sha256": setup.runtime_config_sha256,
        "schema_version": "mobileworld.runtime.sentinel.r2.5-with-tool-two-batch-run/v1",
        "sentinel_config_sha256": manifest.sentinel_config_sha256,
        "strict_matched_pilot_compatible": False,
        "total_wall_time_ms": total_wall_time_ms,
        "with_tool_cell_count": new_cell_count,
    }
    if qwen_start_task_ordinal != 1:
        output.update(
            {
                "continuation": {
                    "qwen_prior_task_count_not_reexecuted": qwen_start_task_ordinal - 1,
                    "qwen_start_task_ordinal": qwen_start_task_ordinal,
                },
                "schema_version": "mobileworld.runtime.sentinel.r2.5-with-tool-two-batch-run/v2",
            }
        )
    if failed_cell_count or not collector_integrity_all_valid:
        output.update(
            {
                "attempted_cell_count": attempted_cell_count,
                "collector_integrity": {
                    "all_valid": collector_integrity_all_valid,
                    "missing_run_count": attempted_cell_count - integrity_count,
                    "run_count": integrity_count,
                },
                "failed_cell_count": failed_cell_count,
                "schema_version": "mobileworld.runtime.sentinel.r2.5-with-tool-two-batch-run/v3",
                "successful_cell_count": attempted_cell_count - failed_cell_count,
            }
        )
    return output


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if type(code) is str and code else "PRODUCTION_SETUP_FAILED"


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        execution_requested = arguments.execute_with_tool_batches
        manifest = _load_cli_authority(
            arguments.authority_manifest,
            production_confirmation_requested=(
                execution_requested or arguments.preflight_checked_at_utc is not None
            ),
            confirmed_manifest_sha256=arguments.confirm_manifest_sha256,
        )
        manifest_hash = authority_manifest_sha256(manifest)
        if execution_requested:
            if not 1 <= arguments.qwen_start_task_ordinal <= len(manifest.pilot.tasks):
                raise _CliContractError("WITH_TOOL_BATCH_SHAPE_MISMATCH")
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
            setup = _build_execution_setup(
                arguments,
                manifest,
                manifest_sha256=manifest_hash,
                preflight_now=preflight_now,
            )
            batch_output = _execute_with_tool_batches(
                manifest,
                manifest_sha256=manifest_hash,
                setup=setup,
                qwen_start_task_ordinal=arguments.qwen_start_task_ordinal,
            )
            print(
                json.dumps(
                    {
                        "dry_run": False,
                        "execution_scope": "R25_WITH_TOOL_BATCHES",
                        "ok": True,
                        "result": batch_output,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0

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
