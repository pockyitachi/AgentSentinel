#!/usr/bin/env python3
"""Create one canonical repo-external R2.4 smoke-only DRAFT authority.

The builder hashes only the six declared request fixtures.  It reads no secret,
snapshot, GPU, network, Docker, model, backend, or MobileWorld state.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import (
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveRunContractError,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SmokeModeV1,
    SnapshotResourceV1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
    R24SmokeOwnerAuthorizationV1,
    R24SmokeRunAuthorityManifestV1,
    R24SmokeSequenceSafetyV1,
    SequenceExecutionScopeV1,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAX_FIXTURE_BYTES = 100_000_000


class _BuildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _snapshot_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    parser.add_argument(f"--{prefix}-snapshot-path", required=True, type=Path)
    parser.add_argument(f"--{prefix}-snapshot-storage-root", required=True, type=Path)
    parser.add_argument(f"--{prefix}-snapshot-tree-sha256", required=True)
    parser.add_argument(f"--{prefix}-snapshot-total-bytes", required=True, type=int)
    parser.add_argument(f"--{prefix}-snapshot-file-count", required=True, type=int)
    parser.add_argument(f"--{prefix}-actor-endpoint", required=True)
    parser.add_argument(f"--{prefix}-served-model-id", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one fresh canonical 0600 DRAFT authority for only Qwen then MAI "
            "OFF/SHADOW/ACTIVE smoke. No pilot authority is created."
        )
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--runtime-output-root", required=True, type=Path)
    parser.add_argument(
        "--secret-file",
        required=True,
        type=Path,
        help="Owner-only OPENAI_API_KEY file; metadata only, content is never read.",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--authorized-by", required=True)
    parser.add_argument("--issued-at-utc", required=True)
    parser.add_argument("--expires-at-utc", required=True)
    parser.add_argument("--runtime-config-sha256", required=True)
    _snapshot_arguments(parser, "qwen")
    _snapshot_arguments(parser, "mai")
    for host in ("qwen", "mai"):
        parser.add_argument(f"--{host}-smoke-task-id", required=True)
        for mode in ("off", "shadow", "active"):
            parser.add_argument(f"--{host}-{mode}-fixture", required=True, type=Path)
    parser.add_argument("--openai-timeout-ms", type=int, default=120_000)
    parser.add_argument("--smoke-wall-time-seconds", type=int, default=300)
    parser.add_argument("--smoke-cost-usd-micros", type=int, default=1_000_000)
    parser.add_argument("--resource-preflight-wall-time-seconds", type=int, default=3_600)
    parser.add_argument("--qwen-to-mai-handoff-wall-time-seconds", type=int, default=900)
    parser.add_argument("--resource-cleanup-wall-time-seconds", type=int, default=300)
    return parser


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _external_path(path: Path, repository: Path, *, must_exist: bool) -> Path:
    if not path.is_absolute():
        raise _BuildError("ABSOLUTE_EXTERNAL_PATH_REQUIRED")
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise _BuildError("EXTERNAL_PATH_UNAVAILABLE") from exc
    if _is_within(resolved, repository) or _is_within(repository, resolved):
        raise _BuildError("REPOSITORY_PATH_FORBIDDEN")
    return resolved


def _secret_identity(path: Path, repository: Path) -> tuple[int, int]:
    _external_path(path, repository, must_exist=True)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            getattr(os, "O_PATH", os.O_RDONLY)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        info = os.fstat(descriptor)
    except OSError as exc:
        raise _BuildError("SECRET_METADATA_INVALID") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or not 1 <= info.st_size <= 65_536
    ):
        raise _BuildError("SECRET_METADATA_INVALID")
    return info.st_dev, info.st_ino


def _hash_fixture(path: Path, *, secret_identity: tuple[int, int]) -> tuple[str, int, bytes]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= _MAX_FIXTURE_BYTES
            or (before.st_dev, before.st_ino) == secret_identity
        ):
            raise _BuildError("SMOKE_FIXTURE_INVALID")
        digest = hashlib.sha256()
        total = 0
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, min(1_048_576, _MAX_FIXTURE_BYTES + 1 - total)):
            chunks.append(chunk)
            digest.update(chunk)
            total += len(chunk)
            if total > _MAX_FIXTURE_BYTES:
                raise _BuildError("SMOKE_FIXTURE_INVALID")
        after = os.fstat(descriptor)
        rebound = os.stat(path, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (rebound.st_dev, rebound.st_ino)
            or total != after.st_size
        ):
            raise _BuildError("SMOKE_FIXTURE_CHANGED")
        return digest.hexdigest(), total, b"".join(chunks)
    except _BuildError:
        raise
    except OSError as exc:
        raise _BuildError("SMOKE_FIXTURE_INVALID") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fixture_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        value[key] = item
    return value


def _fixture_constant(_: str) -> object:
    raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")


def _validate_fixture(
    raw: bytes,
    *,
    host: PilotHostV1,
    served_model_id: str,
    validator: Draft202012Validator,
) -> None:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_fixture_pairs,
            parse_constant=_fixture_constant,
        )
        if type(decoded) is not dict or canonical_json_bytes(cast(JsonValue, decoded)) != raw:
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        validator.validate(decoded)
        expected_codec = (
            "mobileworld.g1.history-codec.qwen-flat-progress"
            if host is PilotHostV1.QWEN3_VL
            else "mobileworld.g1.history-codec.mai-raw-replay"
        )
        if (
            decoded.get("schema_version") != "mobileworld.g1.history-codec-captured-fixture/v1"
            or decoded.get("codec_id") != expected_codec
        ):
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        request = decoded.get("application_request")
        request_sha256 = (
            None
            if type(request) is not dict
            else hashlib.sha256(canonical_json_bytes(cast(JsonValue, request))).hexdigest()
        )
        span_bindings = decoded.get("curated_span_bindings")
        if (
            type(request) is not dict
            or request.get("model") != served_model_id
            or decoded.get("fixture_request_sha256") != request_sha256
            or type(span_bindings) is not list
            or any(
                type(binding) is not dict or binding.get("source_request_sha256") != request_sha256
                for binding in span_bindings
            )
        ):
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        messages = request.get("messages")
        if type(messages) is not list:
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        if host is PilotHostV1.MAI_UI:
            if len(messages) < 2 or type(messages[1]) is not dict:
                raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
            task_blocks = messages[1].get("content")
            if (
                type(task_blocks) is not list
                or len(task_blocks) != 1
                or type(task_blocks[0]) is not dict
                or type(task_blocks[0].get("text")) is not str
                or not cast(str, task_blocks[0]["text"]).strip()
            ):
                raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        else:
            task_candidates = [
                line.strip().removeprefix("The user query:").strip()
                for message in messages
                if type(message) is dict
                and message.get("role") == "user"
                and type(message.get("content")) is list
                for block in cast(list[object], message["content"])
                if type(block) is dict and type(block.get("text")) is str
                for line in cast(str, block["text"]).splitlines()
                if line.strip().startswith("The user query:")
                and line.removeprefix("The user query:").strip()
            ]
            if len(task_candidates) != 1:
                raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        image_urls = [
            block.get("image_url", {}).get("url")
            for message in messages
            if type(message) is dict and type(message.get("content")) is list
            for block in cast(list[object], message["content"])
            if type(block) is dict
            and block.get("type") == "image_url"
            and type(block.get("image_url")) is dict
        ]
        if (
            len(image_urls) != 1
            or type(image_urls[0]) is not str
            or not image_urls[0].startswith("data:image/png;base64,")
        ):
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        image = base64.b64decode(
            image_urls[0].removeprefix("data:image/png;base64,"), validate=True
        )
        if not 1 <= len(image) <= 40 * 1024 * 1024:
            raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
        with Image.open(io.BytesIO(image)) as opened:
            if opened.format != "PNG":
                raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED")
            opened.verify()
    except _BuildError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        raise _BuildError("SMOKE_FIXTURE_SCHEMA_REJECTED") from exc


def _resource(arguments: argparse.Namespace, prefix: str, host: PilotHostV1) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    return SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=str(getattr(arguments, f"{prefix}_snapshot_path").absolute()),
        snapshot_storage_root=str(getattr(arguments, f"{prefix}_snapshot_storage_root").absolute()),
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=getattr(arguments, f"{prefix}_snapshot_tree_sha256"),
        snapshot_total_bytes=getattr(arguments, f"{prefix}_snapshot_total_bytes"),
        snapshot_file_count=getattr(arguments, f"{prefix}_snapshot_file_count"),
        actor_endpoint=getattr(arguments, f"{prefix}_actor_endpoint"),
        served_model_id=getattr(arguments, f"{prefix}_served_model_id"),
        host_enabled=True,
        independent_kill_switch=True,
    )


def _plan(
    arguments: argparse.Namespace,
    prefix: str,
    host: PilotHostV1,
    *,
    secret_identity: tuple[int, int],
    validator: Draft202012Validator,
) -> HostLiveSmokePlanV1:
    cases: list[LiveSmokeCaseV1] = []
    served_model_id = cast(str, getattr(arguments, f"{prefix}_served_model_id"))
    for mode in SmokeModeV1:
        path = cast(Path, getattr(arguments, f"{prefix}_{mode.value.lower()}_fixture"))
        digest, size, raw = _hash_fixture(path, secret_identity=secret_identity)
        _validate_fixture(
            raw,
            host=host,
            served_model_id=served_model_id,
            validator=validator,
        )
        cases.append(
            LiveSmokeCaseV1(
                case_id=f"{prefix}-{mode.value.lower()}",
                task_id=getattr(arguments, f"{prefix}_smoke_task_id"),
                mode=mode,
                request_fixture_path=str(path.absolute()),
                request_fixture_sha256=digest,
                request_fixture_byte_count=size,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=arguments.smoke_wall_time_seconds,
                max_cost_usd_micros=arguments.smoke_cost_usd_micros,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
        )
    return HostLiveSmokePlanV1(host=host, cases=tuple(cases))


def _build(arguments: argparse.Namespace) -> R24SmokeRunAuthorityManifestV1:
    repository = arguments.repository_root.resolve(strict=True)
    if repository != REPOSITORY_ROOT.resolve(strict=True):
        raise _BuildError("REPOSITORY_ROOT_MISMATCH")
    _external_path(arguments.runtime_output_root, repository, must_exist=False)
    secret_identity = _secret_identity(arguments.secret_file, repository)
    schema = json.loads(
        (
            repository
            / "mobileworld_audit_handoff"
            / "schemas"
            / "g1_5"
            / "captured_request_fixture.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    plans = (
        _plan(
            arguments,
            "qwen",
            PilotHostV1.QWEN3_VL,
            secret_identity=secret_identity,
            validator=validator,
        ),
        _plan(
            arguments,
            "mai",
            PilotHostV1.MAI_UI,
            secret_identity=secret_identity,
            validator=validator,
        ),
    )
    stages = tuple(
        OpenAIResponsesStageV1(
            role=role,
            model="gpt-5.6-sol",
            endpoint="https://api.openai.com/v1/responses",
            transport_kind="OPENAI_RESPONSES",
            transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
            openai_sdk_version="1.106.1",
            sdk_max_retries=0,
            external_network_on_call=True,
            model_on_call=True,
            max_output_tokens=8192 if role is OpenAIRoleV1.RUBRIC else 4096,
            timeout_ms=arguments.openai_timeout_ms,
            max_attempts=1,
            store=False,
        )
        for role in (OpenAIRoleV1.RUBRIC, OpenAIRoleV1.HISTORY_POLICY)
    )
    cases = tuple(case for plan in plans for case in plan.cases)
    sequence_time = (
        arguments.resource_preflight_wall_time_seconds
        + arguments.qwen_to_mai_handoff_wall_time_seconds
        + arguments.resource_cleanup_wall_time_seconds
        + sum(case.max_wall_time_seconds for case in cases)
    )
    return R24SmokeRunAuthorityManifestV1(
        schema_version=R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
        execution_scope=SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
        run_id=arguments.run_id,
        source_commit=arguments.source_commit,
        authorization=R24SmokeOwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
            authorization_id=arguments.authorization_id,
            authorized_by=arguments.authorized_by,
            issued_at_utc=arguments.issued_at_utc,
            expires_at_utc=arguments.expires_at_utc,
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=R24SmokeSequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
            ),
            stop_on_failure=True,
            pilot_stage_forbidden=True,
            default_dry_run=True,
            arbitrary_commands_forbidden=True,
            secrets_in_logs_forbidden=True,
            repo_external_output_required=True,
        ),
        secret=SecretFileReferenceV1(
            path=str(arguments.secret_file.absolute()),
            environment_key="OPENAI_API_KEY",
            required_mode=0o600,
            content_may_be_read_by_preflight=False,
            persist_value_or_hash=False,
        ),
        openai_stages=stages,
        actor_resources=(
            _resource(arguments, "qwen", PilotHostV1.QWEN3_VL),
            _resource(arguments, "mai", PilotHostV1.MAI_UI),
        ),
        smoke_plans=plans,
        resource_topology="SINGLE_GPU_SEQUENTIAL_SHARED",
        runtime_config_sha256=arguments.runtime_config_sha256,
        output_root=str(arguments.runtime_output_root.absolute()),
        max_resource_preflight_wall_time_seconds=(arguments.resource_preflight_wall_time_seconds),
        max_qwen_to_mai_handoff_wall_time_seconds=(arguments.qwen_to_mai_handoff_wall_time_seconds),
        max_resource_cleanup_wall_time_seconds=arguments.resource_cleanup_wall_time_seconds,
        max_sequence_wall_time_seconds=sequence_time,
        max_sequence_openai_calls=sum(case.max_openai_calls for case in cases),
        max_sequence_actor_calls=sum(case.max_actor_calls for case in cases),
        max_sequence_cost_usd_micros=sum(case.max_cost_usd_micros for case in cases),
    )


def _write_once(path: Path, payload: bytes, repository: Path) -> None:
    target = _external_path(path, repository, must_exist=False)
    if target.exists() or target.is_symlink():
        raise _BuildError("OUTPUT_NOT_FRESH")
    parent = target.parent.resolve(strict=True)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short authority write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(payload):
            raise OSError("authority metadata differs")
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if created:
            try:
                os.unlink(target)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = _build(arguments)
        projection = smoke_authority_manifest_projection(manifest)
        payload = canonical_json_bytes(cast(JsonValue, projection))
        repository = arguments.repository_root.resolve(strict=True)
        _write_once(arguments.output, payload, repository)
        digest = smoke_authority_manifest_sha256(manifest)
    except (LiveRunContractError, OSError, ValueError, SchemaError, _BuildError) as exc:
        code = getattr(exc, "code", "SMOKE_AUTHORITY_BUILD_FAILED")
        print(json.dumps({"error_code": code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "authority_status": "DRAFT_NOT_AUTHORIZED",
                "execution_scope": "R24_LIVE_SMOKE_ONLY",
                "manifest_path": str(arguments.output),
                "manifest_sha256": digest,
                "ok": True,
                "pilot_authorized": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
