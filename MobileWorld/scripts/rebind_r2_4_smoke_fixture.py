#!/usr/bin/env python3
"""Prepare one canonical G1.5 fixture for an R2.4 live actor smoke.

This operator tool is CPU-only and offline.  It reads one secret-free captured
fixture, binds the exact current production system prompt and served-model
identity, recomputes request-derived bindings, and publishes one fresh
owner-only repo-external fixture.  It has no secret, provider, network, GPU,
backend, actor, or action path.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError

from mobile_world.agents.utils.prompts.mai_ui import MAI_MOBILE_SYS_PROMPT_ASK_USER_MCP
from mobile_world.agents.utils.prompts.qwen3vl import MOBILE_QWEN3VL_PROMPT_WITH_ASK_USER
from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    JsonValue,
    OperationKind,
    PlanOperation,
    PortableContractError,
    SpanRole,
    TransformationPlan,
    copy_json,
    stable_id,
)
from mobile_world.offline.causal_replay.core import restore_original, validate_plan
from mobile_world.offline.g1_history_codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = Path("mobileworld_audit_handoff/schemas/g1_5/captured_request_fixture.schema.json")
_MAX_FIXTURE_BYTES = 100_000_000
_MAX_PNG_BYTES = 40 * 1024 * 1024


class _RebindError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _HostSpec:
    codec_id: str
    history_family: str
    runtime_host_id: str
    fixture_id: str
    human_diff_golden: str
    codec_type: type[QwenFlatProgressHistoryCodec] | type[MaiRawReplayHistoryCodec]
    production_system_prompt: str
    system_prompt_shape: str


_HOSTS = {
    "qwen": _HostSpec(
        codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
        history_family="flat_progress",
        runtime_host_id="mobileworld.qwen3vl.actor",
        fixture_id="g15-qwen-flat-progress-captured-redacted-v1",
        human_diff_golden="qwen_flat_progress.expected_diff.v1.txt",
        codec_type=QwenFlatProgressHistoryCodec,
        production_system_prompt=MOBILE_QWEN3VL_PROMPT_WITH_ASK_USER.render(tools=""),
        system_prompt_shape="QWEN_TEXT_BLOCK",
    ),
    "mai": _HostSpec(
        codec_id="mobileworld.g1.history-codec.mai-raw-replay",
        history_family="raw_replay",
        runtime_host_id="mobileworld.mai-ui.actor",
        fixture_id="g15-mai-raw-replay-captured-redacted-v1",
        human_diff_golden="mai_raw_replay.expected_diff.v1.txt",
        codec_type=MaiRawReplayHistoryCodec,
        production_system_prompt=MAI_MOBILE_SYS_PROMPT_ASK_USER_MCP.render(tools=None),
        system_prompt_shape="MAI_STRING",
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline preparation of one canonical secret-free G1.5 captured fixture "
            "with the exact production prompt and R2.4 actor served-model ID."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-host", required=True, choices=tuple(_HOSTS))
    parser.add_argument("--served-model-id", required=True)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    return parser


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise _RebindError("SOURCE_FIXTURE_SCHEMA_REJECTED")
        value[key] = item
    return value


def _constant(_: str) -> object:
    raise _RebindError("SOURCE_FIXTURE_SCHEMA_REJECTED")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_source(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= _MAX_FIXTURE_BYTES:
            raise _RebindError("SOURCE_FIXTURE_INVALID")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                raise _RebindError("SOURCE_FIXTURE_CHANGED")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _RebindError("SOURCE_FIXTURE_CHANGED")
        after = os.fstat(descriptor)
        rebound = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (after.st_dev, after.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise _RebindError("SOURCE_FIXTURE_CHANGED")
        return b"".join(chunks)
    except _RebindError:
        raise
    except OSError as exc:
        raise _RebindError("SOURCE_FIXTURE_INVALID") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_canonical(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict or canonical_json_bytes(cast(JsonValue, value)) != raw:
            raise _RebindError("SOURCE_FIXTURE_NOT_CANONICAL")
        return cast(dict[str, Any], value)
    except _RebindError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise _RebindError("SOURCE_FIXTURE_SCHEMA_REJECTED") from exc


def _request_sha256(data: dict[str, Any]) -> str:
    request = data.get("application_request")
    if type(request) is not dict:
        raise _RebindError("SOURCE_FIXTURE_SCHEMA_REJECTED")
    return hashlib.sha256(canonical_json_bytes(cast(JsonValue, request))).hexdigest()


def _system_prompt(data: dict[str, Any], spec: _HostSpec) -> str:
    try:
        messages = data["application_request"]["messages"]
        first = messages[0]
        if type(messages) is not list or type(first) is not dict or first.get("role") != "system":
            raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID")
        if spec.system_prompt_shape == "QWEN_TEXT_BLOCK":
            content = first.get("content")
            if (
                type(content) is not list
                or len(content) != 1
                or type(content[0]) is not dict
                or set(content[0]) != {"text", "type"}
                or content[0].get("type") != "text"
                or type(content[0].get("text")) is not str
            ):
                raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID")
            return cast(str, content[0]["text"])
        if spec.system_prompt_shape == "MAI_STRING":
            content = first.get("content")
            if type(content) is not str:
                raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID")
            return content
    except (KeyError, IndexError, TypeError) as exc:
        raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID") from exc
    raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID")


def _set_system_prompt(data: dict[str, Any], spec: _HostSpec, prompt: str) -> None:
    # _system_prompt performs the complete host-specific shape check before mutation.
    _system_prompt(data, spec)
    first = data["application_request"]["messages"][0]
    if spec.system_prompt_shape == "QWEN_TEXT_BLOCK":
        first["content"][0]["text"] = prompt
    elif spec.system_prompt_shape == "MAI_STRING":
        first["content"] = prompt
    else:  # pragma: no cover - every closed host specification is defined above.
        raise _RebindError("SOURCE_SYSTEM_PROMPT_INVALID")


def _bindings(data: dict[str, Any]) -> tuple[CuratedSpanBinding, ...]:
    try:
        return tuple(
            CuratedSpanBinding(
                binding_id=item["binding_id"],
                source_request_sha256=item["source_request_sha256"],
                container_path=tuple(item["container_path"]),
                char_start=item["char_start"],
                char_end=item["char_end"],
                utf8_byte_start=item["utf8_byte_start"],
                utf8_byte_end=item["utf8_byte_end"],
                exact_text=item["exact_text"],
                span_sha256=item["span_sha256"],
                span_role=SpanRole(item["span_role"]),
            )
            for item in data["curated_span_bindings"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _RebindError("SOURCE_TARGET_BINDING_INVALID") from exc


def _target_index(ir: Any) -> dict[str, tuple[Any, Any]]:
    found: dict[str, tuple[Any, Any]] = {}
    for record in ir.records:
        binding_ids = record.provenance.get("curated_binding_ids")
        if not isinstance(binding_ids, list) or len(binding_ids) != len(record.editable_spans):
            raise _RebindError("SOURCE_TARGET_BINDING_INVALID")
        for binding_id, span in zip(binding_ids, record.editable_spans, strict=True):
            if not isinstance(binding_id, str) or binding_id in found:
                raise _RebindError("SOURCE_TARGET_BINDING_INVALID")
            found[binding_id] = (record, span)
    return found


def _operation_key(operation: PlanOperation) -> tuple[object, ...]:
    span = operation.target_span
    path_key = tuple(
        (0, token) if isinstance(token, str) else (1, token) for token in span.container_path
    )
    return (
        path_key,
        span.char_start,
        span.char_end,
        span.span_sha256,
        operation.target_record_id,
        operation.operation_id,
    )


def _plan(
    *,
    ir: Any,
    arm: ArmKind,
    binding_ids: tuple[str, ...],
    correction_text: str,
) -> TransformationPlan:
    by_binding = _target_index(ir)
    operations: list[PlanOperation] = []
    for index, binding_id in enumerate(binding_ids):
        located = by_binding.get(binding_id)
        if located is None:
            raise _RebindError("SOURCE_PLAN_TARGET_INVALID")
        record, span = located
        expected_role = (
            SpanRole.BENIGN_SHAM if arm is ArmKind.SHAM_BENIGN_EDIT else SpanRole.EDITABLE_CLAIM
        )
        if span.span_role is not expected_role:
            raise _RebindError("SOURCE_PLAN_TARGET_INVALID")
        operation_id = f"g15-{arm.value.lower()}-{index:02d}-{binding_id}"
        if arm is ArmKind.MASK_CORRECTION:
            if len(record.correction_anchors) != 1:
                raise _RebindError("SOURCE_PLAN_TARGET_INVALID")
            anchor = record.correction_anchors[0]
            rendered_context: JsonValue = {
                "type": "text",
                "text": f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}",
            }
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.REPLACE,
                target_record_id=record.record_id,
                target_span=span,
                replacement_text=correction_text,
                replacement_author="SENTINEL",
                evidence_refs=(
                    EvidenceRef(
                        evidence_id="g15-secret-free-pre-cutoff-evidence",
                        sha256="e" * 64,
                        role="current_observation_pre_cutoff",
                        event_seq=7,
                    ),
                ),
                correction_anchor=anchor,
                rendered_correction_context=rendered_context,
            )
        else:
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.DROP,
                target_record_id=record.record_id,
                target_span=span,
            )
        operations.append(operation)
    operations.sort(key=_operation_key)
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": arm.value,
        "operations": [item.to_dict() for item in operations],
    }
    return TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=arm,
        operations=tuple(operations),
        curated=True,
        deployment_prediction=False,
    )


def _rendered_hashes(data: dict[str, Any], spec: _HostSpec) -> dict[str, str]:
    request = cast(JsonValue, data["application_request"])
    try:
        codec = spec.codec_type(_bindings(data))
        ir = codec.extract(request)
        if (
            ir.host_id != spec.runtime_host_id
            or ir.codec_id != spec.codec_id
            or ir.history_family.value != spec.history_family
            or ir.raw_request_sha256 != _request_sha256(data)
        ):
            raise _RebindError("SOURCE_HOST_MISMATCH")
        targets = data["plan_targets"]
        focal = tuple(targets["mask"])
        correction = tuple(targets["mask_correction"])
        oracle = tuple(targets["oracle_clean"])
        sham = tuple(targets["sham_benign_edit"])
        if focal != correction or len(sham) != 1:
            raise _RebindError("SOURCE_PLAN_TARGET_INVALID")
        by_arm = {
            ArmKind.ORIGINAL: (),
            ArmKind.MASK: focal,
            ArmKind.MASK_CORRECTION: correction,
            ArmKind.ORACLE_CLEAN: oracle,
            ArmKind.SHAM_BENIGN_EDIT: sham,
        }
        results: dict[str, str] = {}
        for arm in ArmKind:
            plan = _plan(
                ir=ir,
                arm=arm,
                binding_ids=by_arm[arm],
                correction_text=data["correction_text"],
            )
            validate_plan(request, ir, plan)
            rendered = codec.render(
                request,
                ir,
                plan,
                execution_mode=ExecutionMode.G1_SCIENTIFIC,
                failure_policy=FailurePolicy.BLOCK,
            )
            if restore_original(rendered) != request:
                raise _RebindError("SOURCE_RENDER_NOT_REVERSIBLE")
            results[arm.value] = rendered.rendered_request_sha256
        return results
    except _RebindError:
        raise
    except PortableContractError as exc:
        raise _RebindError("SOURCE_TARGET_BINDING_INVALID") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise _RebindError("SOURCE_PLAN_TARGET_INVALID") from exc


def _validate_png(data: dict[str, Any]) -> None:
    try:
        messages = data["application_request"]["messages"]
        urls = [
            block["image_url"]["url"]
            for message in messages
            if type(message) is dict and type(message.get("content")) is list
            for block in message["content"]
            if type(block) is dict and block.get("type") == "image_url"
        ]
        prefix = "data:image/png;base64,"
        if len(urls) != 1 or type(urls[0]) is not str or not urls[0].startswith(prefix):
            raise _RebindError("SOURCE_PNG_INVALID")
        raw = base64.b64decode(urls[0].removeprefix(prefix), validate=True)
        if not 1 <= len(raw) <= _MAX_PNG_BYTES:
            raise _RebindError("SOURCE_PNG_INVALID")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (1, 1) or image.n_frames != 1:
                raise _RebindError("SOURCE_PNG_INVALID")
            image.verify()
    except _RebindError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        OSError,
        UnidentifiedImageError,
    ) as exc:
        raise _RebindError("SOURCE_PNG_INVALID") from exc


def _validate_document(
    data: dict[str, Any],
    *,
    spec: _HostSpec,
    validator: Draft202012Validator,
    expected_rendered_hashes: bool,
) -> None:
    try:
        validator.validate(data)
    except ValidationError as exc:
        raise _RebindError("SOURCE_FIXTURE_SCHEMA_REJECTED") from exc
    if (
        data.get("codec_id") != spec.codec_id
        or data.get("history_family") != spec.history_family
        or data.get("fixture_id") != spec.fixture_id
        or data.get("human_diff_golden") != spec.human_diff_golden
    ):
        raise _RebindError("SOURCE_HOST_MISMATCH")
    _system_prompt(data, spec)
    request_sha256 = _request_sha256(data)
    if data.get("fixture_request_sha256") != request_sha256:
        raise _RebindError("SOURCE_REQUEST_BINDING_MISMATCH")
    bindings = data.get("curated_span_bindings")
    if type(bindings) is not list or any(
        type(item) is not dict or item.get("source_request_sha256") != request_sha256
        for item in bindings
    ):
        raise _RebindError("SOURCE_REQUEST_BINDING_MISMATCH")
    _validate_png(data)
    rendered_hashes = _rendered_hashes(data, spec)
    if expected_rendered_hashes and data.get("expected_rendered_request_sha256") != rendered_hashes:
        raise _RebindError("SOURCE_RENDERED_HASH_MISMATCH")


def _rebind(
    source: dict[str, Any],
    *,
    served_model_id: str,
    spec: _HostSpec,
    validator: Draft202012Validator,
) -> tuple[dict[str, Any], str]:
    if (
        type(served_model_id) is not str
        or not served_model_id
        or served_model_id != served_model_id.strip()
        or any(character in served_model_id for character in "\x00\r\n")
    ):
        raise _RebindError("SERVED_MODEL_ID_INVALID")
    rebound = cast(dict[str, Any], copy_json(cast(JsonValue, source)))
    source_system_prompt = _system_prompt(source, spec)
    _set_system_prompt(rebound, spec, spec.production_system_prompt)
    rebound["application_request"]["model"] = served_model_id
    request_sha256 = _request_sha256(rebound)
    rebound["fixture_request_sha256"] = request_sha256
    for binding in rebound["curated_span_bindings"]:
        binding["source_request_sha256"] = request_sha256
    rebound["expected_rendered_request_sha256"] = _rendered_hashes(rebound, spec)
    _validate_document(
        rebound,
        spec=spec,
        validator=validator,
        expected_rendered_hashes=True,
    )

    restored = cast(dict[str, Any], copy_json(cast(JsonValue, rebound)))
    _set_system_prompt(restored, spec, source_system_prompt)
    restored["application_request"]["model"] = source["application_request"]["model"]
    restored["fixture_request_sha256"] = source["fixture_request_sha256"]
    for binding, source_binding in zip(
        restored["curated_span_bindings"], source["curated_span_bindings"], strict=True
    ):
        binding["source_request_sha256"] = source_binding["source_request_sha256"]
    restored["expected_rendered_request_sha256"] = source["expected_rendered_request_sha256"]
    if restored != source:
        raise _RebindError("REBIND_SCOPE_VIOLATION")
    return rebound, request_sha256


def _output_parent(output: Path, repository: Path) -> Path:
    if not output.is_absolute() or os.path.lexists(output):
        raise _RebindError("OUTPUT_NOT_FRESH")
    try:
        parent = output.parent.resolve(strict=True)
        metadata = output.parent.lstat()
    except OSError as exc:
        raise _RebindError("OUTPUT_PARENT_INVALID") from exc
    if (
        output.parent.absolute() != parent
        or output.absolute() != parent / output.name
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
    ):
        raise _RebindError("OUTPUT_PARENT_INVALID")
    if _is_within(parent, repository) or _is_within(repository, parent):
        raise _RebindError("OUTPUT_PATH_NOT_EXTERNAL")
    return parent


def _publish_fresh(output: Path, payload: bytes, *, repository: Path) -> None:
    parent = _output_parent(output, repository)
    destination = parent / output.name
    descriptor = -1
    temporary: str | None = None
    published = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=parent
        )
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short fixture write")
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
            raise OSError("temporary fixture metadata mismatch")
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, destination, follow_symlinks=False)
        published = True
        directory_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
            os.unlink(temporary)
            temporary = None
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        final = os.stat(destination, follow_symlinks=False)
        if (
            not stat.S_ISREG(final.st_mode)
            or stat.S_IMODE(final.st_mode) != 0o600
            or final.st_uid != os.geteuid()
            or final.st_gid != os.getegid()
            or final.st_nlink != 1
            or final.st_size != len(payload)
        ):
            raise OSError("published fixture metadata mismatch")
        with destination.open("rb") as stream:
            if stream.read() != payload:
                raise OSError("published fixture readback mismatch")
    except OSError as exc:
        if published:
            try:
                os.unlink(destination)
            except OSError:
                pass
        raise _RebindError("OUTPUT_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _build(arguments: argparse.Namespace) -> tuple[bytes, str, str, str]:
    try:
        repository = arguments.repository_root.resolve(strict=True)
    except OSError as exc:
        raise _RebindError("REPOSITORY_ROOT_MISMATCH") from exc
    if repository != REPOSITORY_ROOT.resolve(strict=True) or not repository.is_dir():
        raise _RebindError("REPOSITORY_ROOT_MISMATCH")
    try:
        schema = json.loads((repository / _SCHEMA_PATH).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
    except (OSError, ValueError, SchemaError) as exc:
        raise _RebindError("FIXTURE_SCHEMA_UNAVAILABLE") from exc
    raw = _read_source(arguments.input)
    source = _decode_canonical(raw)
    spec = _HOSTS[arguments.expected_host]
    _validate_document(
        source,
        spec=spec,
        validator=validator,
        expected_rendered_hashes=True,
    )
    rebound, request_sha256 = _rebind(
        source,
        served_model_id=arguments.served_model_id,
        spec=spec,
        validator=validator,
    )
    payload = canonical_json_bytes(cast(JsonValue, rebound))
    return (
        payload,
        hashlib.sha256(payload).hexdigest(),
        request_sha256,
        hashlib.sha256(spec.production_system_prompt.encode("utf-8")).hexdigest(),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        payload, artifact_sha256, request_sha256, prompt_sha256 = _build(arguments)
        repository = arguments.repository_root.resolve(strict=True)
        _publish_fresh(arguments.output, payload, repository=repository)
    except _RebindError as exc:
        print(json.dumps({"error_code": exc.code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "application_request_sha256": request_sha256,
                "expected_host": arguments.expected_host,
                "fixture_artifact_byte_count": len(payload),
                "fixture_artifact_sha256": artifact_sha256,
                "ok": True,
                "output": str(arguments.output),
                "production_system_prompt_sha256": prompt_sha256,
                "served_model_id": arguments.served_model_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
