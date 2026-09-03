#!/usr/bin/env python3
"""Build canonical production runtime-config and pricing inputs without execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptPricingV1,
    live_attempt_pricing_projection,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionDriverError,
    ProductionRuntimeConfigV1,
    production_runtime_config_projection,
    production_runtime_config_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _InputBuildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create fresh canonical 0600 runtime-config and pricing files. This command "
            "does not inspect Docker/GPU state and never reads a secret file's content."
        )
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--authorized-pilot-input-root", required=True, type=Path)
    parser.add_argument("--process-log-root", required=True, type=Path)
    parser.add_argument("--backend-port", required=True, type=int)
    parser.add_argument("--backend-device", required=True)
    parser.add_argument("--backend-image-id-sha256", required=True)
    parser.add_argument("--backend-environment-file", required=True, type=Path)
    parser.add_argument("--qwen-gpu-index", required=True, type=int)
    parser.add_argument("--mai-gpu-index", required=True, type=int)
    parser.add_argument("--vllm-python-executable", required=True, type=Path)
    parser.add_argument("--vllm-version", required=True)
    parser.add_argument("--startup-timeout-seconds", type=int, default=900)
    parser.add_argument("--shutdown-grace-seconds", type=int, default=10)
    parser.add_argument("--health-poll-interval-ms", type=int, default=250)
    parser.add_argument("--pricing-id", required=True)
    parser.add_argument("--pricing-model", default="gpt-5.6-sol")
    parser.add_argument("--input-price-usd-micros-per-million", required=True, type=int)
    parser.add_argument("--cached-input-price-usd-micros-per-million", required=True, type=int)
    parser.add_argument("--output-price-usd-micros-per-million", required=True, type=int)
    parser.add_argument("--pricing-source-sha256", required=True)
    parser.add_argument("--pricing-effective-at-utc", required=True)
    return parser


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _hash_regular_file(path: Path, *, maximum_bytes: int) -> tuple[Path, str, int]:
    try:
        logical = path.absolute()
        real = path.resolve(strict=True)
        metadata = real.stat()
    except OSError as exc:
        raise _InputBuildError("VLLM_EXECUTABLE_UNAVAILABLE") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(real, os.X_OK)
        or not 0 < metadata.st_size <= maximum_bytes
    ):
        raise _InputBuildError("VLLM_EXECUTABLE_INVALID")
    digest = hashlib.sha256()
    try:
        with real.open("rb") as stream:
            while chunk := stream.read(1_048_576):
                digest.update(chunk)
    except OSError as exc:
        raise _InputBuildError("VLLM_EXECUTABLE_UNAVAILABLE") from exc
    return logical, digest.hexdigest(), metadata.st_size


def _backend_environment_metadata(path: Path) -> os.stat_result:
    try:
        if path.is_symlink():
            raise _InputBuildError("BACKEND_ENVIRONMENT_FILE_INVALID")
        metadata = path.lstat()
    except OSError as exc:
        raise _InputBuildError("BACKEND_ENVIRONMENT_FILE_UNAVAILABLE") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= 1_048_576
    ):
        raise _InputBuildError("BACKEND_ENVIRONMENT_FILE_INVALID")
    return metadata


def _write_once(path: Path, payload: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short artifact write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
        ):
            raise OSError("artifact metadata differs")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _build(arguments: argparse.Namespace) -> tuple[bytes, str, bytes, str]:
    repository = arguments.repository_root.resolve(strict=True)
    if not repository.is_dir() or repository != REPOSITORY_ROOT.resolve(strict=True):
        raise _InputBuildError("REPOSITORY_ROOT_MISMATCH")
    executable, executable_sha, executable_bytes = _hash_regular_file(
        arguments.vllm_python_executable, maximum_bytes=1_000_000_000
    )
    environment_metadata = _backend_environment_metadata(arguments.backend_environment_file)
    runtime = ProductionRuntimeConfigV1(
        backend_port=arguments.backend_port,
        backend_device=arguments.backend_device,
        qwen_gpu_index=arguments.qwen_gpu_index,
        mai_gpu_index=arguments.mai_gpu_index,
        process_log_root=str(arguments.process_log_root.absolute()),
        authorized_pilot_input_root=str(arguments.authorized_pilot_input_root.absolute()),
        repository_root=str(repository),
        mobileworld_source_root=str(repository / "MobileWorld" / "src"),
        vllm_python_executable=str(executable),
        vllm_python_realpath=str(arguments.vllm_python_executable.resolve(strict=True)),
        vllm_python_sha256=executable_sha,
        vllm_python_byte_count=executable_bytes,
        vllm_version=arguments.vllm_version,
        backend_image_id_sha256=arguments.backend_image_id_sha256,
        backend_environment_file=str(arguments.backend_environment_file.absolute()),
        backend_environment_file_device=environment_metadata.st_dev,
        backend_environment_file_inode=environment_metadata.st_ino,
        backend_environment_file_mode=stat.S_IMODE(environment_metadata.st_mode),
        backend_environment_file_uid=environment_metadata.st_uid,
        backend_environment_file_byte_count=environment_metadata.st_size,
        backend_environment_file_mtime_ns=environment_metadata.st_mtime_ns,
        startup_timeout_seconds=arguments.startup_timeout_seconds,
        shutdown_grace_seconds=arguments.shutdown_grace_seconds,
        health_poll_interval_ms=arguments.health_poll_interval_ms,
    )
    pricing = LiveAttemptPricingV1(
        pricing_id=arguments.pricing_id,
        model=arguments.pricing_model,
        input_usd_micros_per_million_tokens=(arguments.input_price_usd_micros_per_million),
        cached_input_usd_micros_per_million_tokens=(
            arguments.cached_input_price_usd_micros_per_million
        ),
        output_usd_micros_per_million_tokens=(arguments.output_price_usd_micros_per_million),
        source_sha256=arguments.pricing_source_sha256,
        effective_at_utc=arguments.pricing_effective_at_utc,
    )
    runtime_bytes = canonical_json_bytes(
        cast(JsonValue, production_runtime_config_projection(runtime))
    )
    pricing_bytes = canonical_json_bytes(cast(JsonValue, live_attempt_pricing_projection(pricing)))
    return (
        runtime_bytes,
        production_runtime_config_sha256(runtime),
        pricing_bytes,
        live_attempt_pricing_sha256(pricing),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    created_directory = False
    try:
        output = arguments.output_dir
        if not output.is_absolute() or output.exists() or output.is_symlink():
            raise _InputBuildError("OUTPUT_DIRECTORY_NOT_FRESH")
        parent = output.parent.resolve(strict=True)
        repository = arguments.repository_root.resolve(strict=True)
        resolved = parent / output.name
        if _is_within(resolved, repository) or _is_within(repository, resolved):
            raise _InputBuildError("REPOSITORY_OUTPUT_FORBIDDEN")
        runtime_bytes, runtime_hash, pricing_bytes, pricing_hash = _build(arguments)
        os.mkdir(resolved, mode=0o700)
        created_directory = True
        os.chmod(resolved, 0o700)
        _write_once(resolved / "runtime-config.json", runtime_bytes)
        _write_once(resolved / "pricing.json", pricing_bytes)
        directory_fd = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except (OSError, ProductionDriverError, ValueError, _InputBuildError) as exc:
        if created_directory:
            try:
                shutil.rmtree(arguments.output_dir)
            except OSError:
                pass
        code = getattr(exc, "code", "PRODUCTION_INPUT_BUILD_FAILED")
        print(json.dumps({"error_code": code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "pricing_path": str(resolved / "pricing.json"),
                "pricing_sha256": pricing_hash,
                "runtime_config_path": str(resolved / "runtime-config.json"),
                "runtime_config_sha256": runtime_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
