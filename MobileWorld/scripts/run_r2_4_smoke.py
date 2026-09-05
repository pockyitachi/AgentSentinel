#!/usr/bin/env python3
"""Preflight by default, or execute, an exact owner-authorized R2.4 smoke only.

This entrypoint has no R2.5 stage or pilot adapter.  ``--execute`` requires four
exact hash confirmations and a recent reproducible preflight timestamp.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.authority_promotion import (
    AuthorityPromotionError,
    load_owner_authorized_smoke_authority_v1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LIVE_ATTEMPT_PRICING_SCHEMA_VERSION,
    LiveAttemptPricingV1,
    live_attempt_pricing_projection,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    _production_cleanup_bound_seconds,
    build_production_r24_smoke_executor_v1,
    r24_smoke_sequence_result_projection,
)
from mobile_world.runtime.sentinel.r2_4.live_run import LiveRunContractError
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
    ProductionDriverError,
    ProductionResourceTopologyV1,
    build_production_case_authority_broker_provider_v1,
    build_production_driver_v1,
    build_production_resource_lifecycle_adapter_v1,
    parse_production_runtime_config,
    production_runtime_config_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    r24_smoke_production_preflight_report_projection,
    r24_smoke_production_preflight_report_sha256,
    require_production_post_preflight_factory_v1,
    run_r24_smoke_production_preflight_v1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
    smoke_authority_manifest_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAX_INPUT_BYTES = 1_048_576
_MAX_PREFLIGHT_AGE_SECONDS = 300


class _CliError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SecretMetadataPin:
    __slots__ = ("descriptor", "identity", "path")

    def __init__(self, descriptor: int, path: Path, identity: tuple[int, ...]) -> None:
        self.descriptor = descriptor
        self.path = path
        self.identity = identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "R2.4 Qwen then MAI OFF/SHADOW/ACTIVE smoke-only preflight/runner. "
            "Dry-run preflight is the default; no R2.5 action is reachable."
        )
    )
    parser.add_argument("--authority-manifest", required=True, type=Path)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--confirm-runtime-config-sha256", required=True)
    parser.add_argument("--pricing", required=True, type=Path)
    parser.add_argument("--confirm-pricing-sha256", required=True)
    parser.add_argument("--preflight-checked-at-utc", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-preflight-report-sha256")
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


def _input_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_input_metadata(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
        or info.st_nlink != 1
        or not 1 <= info.st_size <= _MAX_INPUT_BYTES
    ):
        raise _CliError("INVALID_EXECUTION_INPUT")


def _pin_secret_metadata(path: Path) -> _SecretMetadataPin:
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        path_only = getattr(os, "O_PATH", None)
        if type(no_follow) is not int or type(path_only) is not int or path.is_symlink():
            raise _CliError("INVALID_SECRET_METADATA")
        descriptor = os.open(
            path,
            path_only | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
            or info.st_nlink != 1
            or not 1 <= info.st_size <= 65_536
        ):
            raise _CliError("INVALID_SECRET_METADATA")
        declared = os.lstat(path)
        resolved = os.stat(path.resolve(strict=True), follow_symlinks=False)
        identity = _input_identity(info)
        if (
            stat.S_ISLNK(declared.st_mode)
            or _input_identity(declared) != identity
            or _input_identity(resolved) != identity
        ):
            raise _CliError("INVALID_SECRET_METADATA")
        return _SecretMetadataPin(descriptor=descriptor, path=path, identity=identity)
    except _CliError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise _CliError("INVALID_SECRET_METADATA") from exc


def _require_current_secret_pin(pin: _SecretMetadataPin) -> None:
    path_descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        path_only = getattr(os, "O_PATH", None)
        if type(no_follow) is not int or type(path_only) is not int:
            raise _CliError("SECRET_METADATA_DRIFT")
        held = os.fstat(pin.descriptor)
        declared = os.lstat(pin.path)
        if stat.S_ISLNK(declared.st_mode):
            raise _CliError("SECRET_METADATA_DRIFT")
        path_descriptor = os.open(
            pin.path,
            path_only | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        rebound = os.fstat(path_descriptor)
        resolved = os.stat(pin.path.resolve(strict=True), follow_symlinks=False)
        if any(
            _input_identity(value) != pin.identity for value in (held, declared, rebound, resolved)
        ):
            raise _CliError("SECRET_METADATA_DRIFT")
    except _CliError:
        raise
    except OSError as exc:
        raise _CliError("SECRET_METADATA_DRIFT") from exc
    finally:
        if path_descriptor >= 0:
            os.close(path_descriptor)


def _load_canonical_input(
    path: Path,
    *,
    forbidden_identity: tuple[int, int] | None = None,
    secret_pin: _SecretMetadataPin | None = None,
) -> JsonValue:
    descriptor = -1
    path_descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        path_only = getattr(os, "O_PATH", None)
        if type(no_follow) is not int or type(path_only) is not int or path.is_symlink():
            raise _CliError("INVALID_EXECUTION_INPUT")
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        _require_input_metadata(before)
        if forbidden_identity is not None and (before.st_dev, before.st_ino) == forbidden_identity:
            raise _CliError("EXECUTION_INPUT_ALIASES_SECRET")
        if secret_pin is not None:
            _require_current_secret_pin(secret_pin)
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65_536, _MAX_INPUT_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_INPUT_BYTES:
                raise _CliError("INVALID_EXECUTION_INPUT")
        after = os.fstat(descriptor)
        _require_input_metadata(after)
        if _input_identity(before) != _input_identity(after) or total != after.st_size:
            raise _CliError("INVALID_EXECUTION_INPUT")
        declared = os.lstat(path)
        if stat.S_ISLNK(declared.st_mode) or _input_identity(after) != _input_identity(declared):
            raise _CliError("INVALID_EXECUTION_INPUT")
        resolved = path.resolve(strict=True)
        resolved_info = os.stat(resolved, follow_symlinks=False)
        if _input_identity(after) != _input_identity(resolved_info):
            raise _CliError("INVALID_EXECUTION_INPUT")
        path_descriptor = os.open(
            path,
            path_only | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        rebound = os.fstat(path_descriptor)
        _require_input_metadata(rebound)
        if _input_identity(after) != _input_identity(rebound):
            raise _CliError("INVALID_EXECUTION_INPUT")
        raw = b"".join(chunks)
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        if type(decoded) is not dict or canonical_json_bytes(cast(JsonValue, decoded)) != raw:
            raise _CliError("INVALID_EXECUTION_INPUT")
        return cast(JsonValue, decoded)
    except _CliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _CliError("INVALID_EXECUTION_INPUT") from exc
    finally:
        if path_descriptor >= 0:
            os.close(path_descriptor)
        if descriptor >= 0:
            os.close(descriptor)


def _parse_pricing(value: JsonValue) -> LiveAttemptPricingV1:
    if type(value) is not dict or set(value) != {
        "cached_input_usd_micros_per_million_tokens",
        "effective_at_utc",
        "input_usd_micros_per_million_tokens",
        "model",
        "output_usd_micros_per_million_tokens",
        "pricing_id",
        "rounding_policy",
        "schema_version",
        "source_sha256",
    }:
        raise _CliError("INVALID_PRICING")
    if value.get("schema_version") != LIVE_ATTEMPT_PRICING_SCHEMA_VERSION:
        raise _CliError("INVALID_PRICING")
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
        raise _CliError("INVALID_PRICING") from exc


def _utc_second(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise _CliError("INVALID_PREFLIGHT_TIMESTAMP") from exc


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if type(code) is str and code else "SMOKE_PRODUCTION_SETUP_FAILED"


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    secret_pin: _SecretMetadataPin | None = None
    try:
        manifest = load_owner_authorized_smoke_authority_v1(
            arguments.authority_manifest,
            repository_root=REPOSITORY_ROOT,
        )
        manifest_sha256 = smoke_authority_manifest_sha256(manifest)
        if arguments.confirm_manifest_sha256 != manifest_sha256:
            raise _CliError("MANIFEST_CONFIRMATION_MISMATCH")
        secret_pin = _pin_secret_metadata(Path(manifest.secret.path))
        secret_identity = cast(tuple[int, int], secret_pin.identity[:2])

        runtime = parse_production_runtime_config(
            _load_canonical_input(
                arguments.runtime_config,
                forbidden_identity=secret_identity,
                secret_pin=secret_pin,
            )
        )
        runtime_sha256 = production_runtime_config_sha256(runtime)
        if (
            arguments.confirm_runtime_config_sha256 != runtime_sha256
            or runtime_sha256 != manifest.runtime_config_sha256
        ):
            raise _CliError("RUNTIME_CONFIG_CONFIRMATION_MISMATCH")
        if (
            runtime.resource_topology
            is not ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
            or runtime.qwen_gpu_index != runtime.mai_gpu_index
        ):
            raise _CliError("SMOKE_SHARED_RESOURCE_CONFIG_REQUIRED")

        pricing_value = _load_canonical_input(
            arguments.pricing,
            forbidden_identity=secret_identity,
            secret_pin=secret_pin,
        )
        pricing = _parse_pricing(pricing_value)
        pricing_sha256 = live_attempt_pricing_sha256(pricing)
        if arguments.confirm_pricing_sha256 != pricing_sha256:
            raise _CliError("PRICING_CONFIRMATION_MISMATCH")
        if canonical_json_bytes(
            cast(JsonValue, live_attempt_pricing_projection(pricing))
        ) != canonical_json_bytes(pricing_value):
            raise _CliError("INVALID_PRICING")

        checked_at = _utc_second(arguments.preflight_checked_at_utc)
        report = run_r24_smoke_production_preflight_v1(
            manifest,
            confirmed_manifest_sha256=manifest_sha256,
            confirmed_runtime_config_sha256=runtime_sha256,
            repository_root=REPOSITORY_ROOT,
            now=checked_at,
        )
        report_sha256 = r24_smoke_production_preflight_report_sha256(report)
        report_projection = r24_smoke_production_preflight_report_projection(report)
        if not report.eligible_for_post_preflight_factory:
            raise _CliError("SMOKE_PRODUCTION_PREFLIGHT_FAILED")

        if not arguments.execute:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "execution_scope": "R24_LIVE_SMOKE_ONLY",
                        "manifest_sha256": manifest_sha256,
                        "ok": True,
                        "pilot_reachable": False,
                        "preflight": report_projection,
                        "preflight_report_sha256": report_sha256,
                        "runtime_config_sha256": runtime_sha256,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0

        if (
            arguments.confirm_preflight_report_sha256 is None
            or arguments.production_audit_root is None
        ):
            raise _CliError("EXECUTE_ARGUMENTS_REQUIRED")
        if arguments.confirm_preflight_report_sha256 != report_sha256:
            raise _CliError("PREFLIGHT_REPORT_CONFIRMATION_MISMATCH")
        if abs((datetime.now(UTC) - checked_at).total_seconds()) > _MAX_PREFLIGHT_AGE_SECONDS:
            raise _CliError("PREFLIGHT_TIMESTAMP_NOT_CURRENT")

        factory = require_production_post_preflight_factory_v1(
            manifest,
            report,
            confirmed_manifest_sha256=manifest_sha256,
            confirmed_preflight_report_sha256=report_sha256,
            confirmed_pricing_sha256=pricing_sha256,
        )
        resource = build_production_resource_lifecycle_adapter_v1(
            runtime,
            confirmed_config_sha256=runtime_sha256,
        )
        cleanup_upper_bound_seconds, _, _ = _production_cleanup_bound_seconds(
            resource,
            expected_schema_version=PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
            runtime_config_sha256=runtime_sha256,
        )
        if manifest.max_resource_cleanup_wall_time_seconds < max(
            R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
            cleanup_upper_bound_seconds,
        ):
            raise _CliError("INSUFFICIENT_RESOURCE_CLEANUP_RESERVE")
        audit_sink = ExternalProductionRuntimeAuditSinkV1(
            arguments.production_audit_root,
            repository_root=REPOSITORY_ROOT,
        )
        drivers = build_production_driver_v1(
            factory=factory,
            runtime_config=runtime,
            confirmed_runtime_config_sha256=runtime_sha256,
            pricing=pricing,
            confirmed_pricing_sha256=pricing_sha256,
            production_audit_sink=audit_sink,
            resource_lifecycle=resource,
        )
        broker = build_production_case_authority_broker_provider_v1(factory)
        executor = build_production_r24_smoke_executor_v1(
            manifest,
            confirmed_manifest_sha256=manifest_sha256,
            confirmed_runtime_config_sha256=runtime_sha256,
            repository_root=REPOSITORY_ROOT,
            post_preflight_factory=factory,
            resource_adapter=resource,
            driver_adapters=drivers,
            case_authority_broker_provider=broker,
        )
        result = executor.execute(manifest)
        projection = r24_smoke_sequence_result_projection(result)
        ok = result.status.value == "COMPLETE"
        print(
            json.dumps(
                {
                    "dry_run": False,
                    "execution_scope": "R24_LIVE_SMOKE_ONLY",
                    "manifest_sha256": manifest_sha256,
                    "ok": ok,
                    "pilot_reachable": False,
                    "preflight_report_sha256": report_sha256,
                    "result": projection,
                    "runtime_config_sha256": runtime_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if ok else 3
    except (AuthorityPromotionError, LiveRunContractError, ProductionDriverError, _CliError) as exc:
        print(
            json.dumps({"error_code": _error_code(exc), "ok": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"error_code": "SMOKE_PRODUCTION_SETUP_FAILED", "ok": False}, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if secret_pin is not None:
            os.close(secret_pin.descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
