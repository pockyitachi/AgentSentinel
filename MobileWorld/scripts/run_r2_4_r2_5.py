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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import JsonValue
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
    preflight_report_projection,
    run_authorized_sequence_with_executor,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionDriverError,
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


def _load_small_json(path: Path) -> JsonValue:
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or not 0 < info.st_size <= _MAX_CONFIG_BYTES:
            raise ValueError("JSON input is not a bounded regular file")
        raw = path.read_bytes()
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _CliContractError("INVALID_EXECUTION_INPUT") from exc
    if type(decoded) is not dict:
        raise _CliContractError("INVALID_EXECUTION_INPUT")
    return cast(JsonValue, decoded)


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


def _build_production_executor(
    arguments: argparse.Namespace,
    manifest: R24R25RunAuthorityManifestV1,
    *,
    manifest_sha256: str,
    preflight_now: datetime,
) -> tuple[ProductionR24R25ExecutorV1, dict[str, JsonValue]]:
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        repository_root=REPOSITORY_ROOT,
        now=preflight_now,
    )
    report_sha256 = production_preflight_report_sha256(report)
    if arguments.confirm_preflight_report_sha256 != report_sha256:
        raise _CliContractError("PREFLIGHT_REPORT_CONFIRMATION_MISMATCH")

    runtime_config = parse_production_runtime_config(
        _load_small_json(cast(Path, arguments.runtime_config))
    )
    if Path(runtime_config.repository_root).resolve(strict=True) != REPOSITORY_ROOT.resolve(
        strict=True
    ):
        raise _CliContractError("RUNTIME_REPOSITORY_MISMATCH")
    runtime_sha256 = production_runtime_config_sha256(runtime_config)
    if arguments.confirm_runtime_config_sha256 != runtime_sha256:
        raise _CliContractError("RUNTIME_CONFIG_CONFIRMATION_MISMATCH")

    pricing = _parse_pricing(_load_small_json(cast(Path, arguments.pricing)))
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    if arguments.confirm_pricing_sha256 != pricing_sha256:
        raise _CliContractError("PRICING_CONFIRMATION_MISMATCH")

    factory = require_production_post_preflight_factory_v1(
        manifest,
        report,
        confirmed_manifest_sha256=manifest_sha256,
        confirmed_preflight_report_sha256=report_sha256,
        confirmed_pricing_sha256=pricing_sha256,
    )
    resource_adapter = build_production_resource_lifecycle_adapter_v1(
        runtime_config,
        confirmed_config_sha256=runtime_sha256,
    )
    audit_sink = ExternalProductionRuntimeAuditSinkV1(
        cast(Path, arguments.production_audit_root), repository_root=REPOSITORY_ROOT
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
        repository_root=REPOSITORY_ROOT,
        resource_adapter=resource_adapter,
        driver_adapters=driver_adapters,
        case_authority_broker_provider=broker_provider,
    )
    return executor, production_preflight_report_projection(report)


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
        manifest = load_authority_manifest(arguments.authority_manifest)
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
            executor, production_preflight = _build_production_executor(
                arguments,
                manifest,
                manifest_sha256=manifest_hash,
                preflight_now=preflight_now,
            )
            result = run_authorized_sequence_with_executor(
                manifest,
                executor,
                confirmed_manifest_sha256=manifest_hash,
            )
            ok = result.status.value == "COMPLETE"
            print(
                json.dumps(
                    {
                        "dry_run": False,
                        "manifest_sha256": manifest_hash,
                        "ok": ok,
                        "preflight": production_preflight,
                        "preflight_report_sha256": arguments.confirm_preflight_report_sha256,
                        "result": _sequence_projection(result),
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
            report = run_production_preflight_v1(
                manifest,
                confirmed_manifest_sha256=manifest_hash,
                repository_root=REPOSITORY_ROOT,
                now=preflight_now,
            )
            output["production_preflight"] = production_preflight_report_projection(report)
            output["production_preflight_report_sha256"] = production_preflight_report_sha256(
                report
            )
        print(
            json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (LiveRunContractError, ProductionDriverError, _CliContractError) as exc:
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
