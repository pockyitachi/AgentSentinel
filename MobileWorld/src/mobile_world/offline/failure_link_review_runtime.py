"""Tamper-evident, read-only Codex runtime for failure-link review units.

This module is intentionally semantics-agnostic.  Callers supply the frozen
prompt renderer, JSON Schema, and public attribution validator.  It provides
only bounded subprocess execution, immutable receipts, and strict resume
verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mobile_world.offline.motivation_review import canonical_json_bytes

RUNTIME_SCHEMA_VERSION = "mobileworld.audit.failure-link-codex-runtime/v4"
MAX_PROMPT_CHARS = 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES = 480 * 1024 * 1024
REQUEST_PAYLOAD_OVERHEAD_BYTES = 8 * 1024 * 1024
REVIEWER_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
)

_UNIT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")


class ReviewRuntimeError(RuntimeError):
    """A deterministic process, receipt, or immutable-artifact check failed."""


class ReviewRetryExhausted(ReviewRuntimeError):
    """One isolated review unit exhausted its finite attempts."""


@dataclass(frozen=True, slots=True)
class ReviewUnit:
    phase: str
    stage: str
    unit_id: str
    task_key: str
    card_sha256: str
    image_paths: tuple[Path, ...] = ()
    image_sha256s: tuple[str, ...] = ()
    image_byte_lengths: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class StageArtifact:
    unit: ReviewUnit
    response: dict[str, Any]
    response_sha256: str
    receipt_sha256: str
    directory: Path
    resumed: bool


Runner = Callable[..., subprocess.CompletedProcess[bytes]]
PromptRenderer = Callable[[str | None], str]
ResponseValidator = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    _reject_symlink_components(path, stop=Path(path.absolute().anchor))
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReviewRuntimeError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def write_once(path: Path, data: bytes) -> None:
    """Atomically create an artifact, or verify an identical frozen value."""

    filesystem_root = Path(path.absolute().anchor)
    _reject_symlink_components(path, stop=filesystem_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path, stop=filesystem_root)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ReviewRuntimeError(f"cannot read frozen artifact {path}: {exc}") from exc
        if existing != data:
            raise ReviewRuntimeError(f"frozen artifact differs from requested bytes: {path}")
        return
    descriptor, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_text)
    try:
        _write_file(temporary, data, exclusive=False)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ReviewRuntimeError(f"frozen artifact differs from requested bytes: {path}")
        else:
            _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def execute_review_unit(
    *,
    unit: ReviewUnit,
    output_root: Path,
    model: str,
    reviewer_id: str,
    codex_bin: str,
    schema: Mapping[str, Any],
    render_prompt: PromptRenderer,
    validate_response: ResponseValidator,
    receipt_binding: Mapping[str, Any],
    max_attempts: int,
    timeout_seconds: int,
    resume: bool,
    runner: Runner = subprocess.run,
) -> StageArtifact:
    """Execute and atomically freeze one singleton review unit."""

    _validate_unit(unit)
    if not model or not reviewer_id or not codex_bin:
        raise ReviewRuntimeError("model, reviewer_id, and codex_bin must be non-empty")
    if max_attempts <= 0 or timeout_seconds <= 0:
        raise ReviewRuntimeError("attempt and timeout limits must be positive")
    if len(set(unit.image_paths)) != len(unit.image_paths):
        raise ReviewRuntimeError(f"duplicate image path in review unit: {unit.unit_id}")
    image_attachments = _verify_image_attachments(unit)

    schema_bytes = canonical_json_bytes(dict(schema))
    base_prompt_bytes = _prompt_bytes(render_prompt(None), unit=unit)
    binding = dict(receipt_binding)
    binding_sha256 = sha256_bytes(canonical_json_bytes(binding))
    expected_receipt = {
        "base_prompt_sha256": sha256_bytes(base_prompt_bytes),
        "card_sha256": unit.card_sha256,
        "codex_bin": codex_bin,
        "max_attempts": max_attempts,
        "model": model,
        "image_attachment_set_sha256": sha256_bytes(canonical_json_bytes(image_attachments)),
        "image_attachment_total_bytes": sum(record["byte_length"] for record in image_attachments),
        "image_attachments": image_attachments,
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "phase": unit.phase,
        "receipt_binding": binding,
        "receipt_binding_sha256": binding_sha256,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "reviewer_id": reviewer_id,
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "request_payload_overhead_bytes": REQUEST_PAYLOAD_OVERHEAD_BYTES,
        "schema_sha256": sha256_bytes(schema_bytes),
        "stage": unit.stage,
        "task_key": unit.task_key,
        "timeout_seconds": timeout_seconds,
        "unit_id": unit.unit_id,
    }
    _reject_symlink_components(output_root, stop=Path(output_root.absolute().anchor))
    target = output_root / "batches" / _stage_directory(unit) / unit.unit_id
    _reject_symlink_components(target, stop=output_root)
    if target.exists():
        if not resume:
            raise ReviewRuntimeError(f"review artifact already exists: {target}")
        return _resume_artifact(
            unit=unit,
            target=target,
            expected_receipt=expected_receipt,
            schema_bytes=schema_bytes,
            render_prompt=render_prompt,
            validate_response=validate_response,
        )

    stage_root = target.parent
    stage_root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(target, stop=output_root)
    attempts: list[dict[str, Any]] = []
    accepted: dict[str, Any] | None = None
    accepted_model_response: dict[str, Any] | None = None
    accepted_prompt_sha256: str | None = None
    validation_feedback: str | None = None
    for attempt in range(1, max_attempts + 1):
        if _verify_image_attachments(unit) != image_attachments:
            raise ReviewRuntimeError(f"review image attachment manifest drift: {unit.unit_id}")
        prompt_bytes = _prompt_bytes(render_prompt(validation_feedback), unit=unit)
        _validate_estimated_request_payload(
            prompt_byte_count=len(prompt_bytes), image_attachments=image_attachments
        )
        prompt_sha256 = sha256_bytes(prompt_bytes)
        feedback_sha256 = (
            sha256_bytes(validation_feedback.encode("utf-8"))
            if validation_feedback is not None
            else None
        )
        stdout = b""
        stderr = b""
        returncode: int | None = None
        error_kind: str | None = None
        error_detail: str | None = None
        model_response_sha256: str | None = None
        response_sha256: str | None = None
        rejected_path: str | None = None
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="failure-link-codex-") as temporary_text:
            temporary = Path(temporary_text)
            schema_path = temporary / "output_schema.json"
            output_path = temporary / "response.json"
            schema_path.write_bytes(schema_bytes)
            argv = _codex_argv(
                codex_bin=codex_bin,
                model=model,
                temporary=temporary,
                schema_path=schema_path,
                output_path=output_path,
                image_paths=unit.image_paths,
            )
            try:
                completed = runner(
                    argv,
                    input=prompt_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=temporary,
                    shell=False,
                    check=False,
                    timeout=timeout_seconds,
                )
                if _verify_image_attachments(unit) != image_attachments:
                    raise ReviewRuntimeError(
                        f"review image attachment changed during attempt: {unit.unit_id}"
                    )
                stdout = completed.stdout or b""
                stderr = completed.stderr or b""
                returncode = completed.returncode
                if returncode != 0:
                    error_kind = "nonzero_exit"
                elif not output_path.is_file():
                    error_kind = "missing_output"
                else:
                    try:
                        candidate = _read_json_object(output_path)
                        model_response_sha256 = sha256_bytes(canonical_json_bytes(candidate))
                        validated = validate_response(candidate)
                        accepted_model_response = candidate
                        accepted = dict(candidate if validated is None else validated)
                        accepted_prompt_sha256 = prompt_sha256
                        response_sha256 = sha256_bytes(canonical_json_bytes(accepted))
                    except (ReviewRuntimeError, ValueError) as exc:
                        error_kind = "invalid_response"
                        error_detail = _validation_feedback(exc)
                        validation_feedback = error_detail
                        try:
                            rejected = _read_json_object(output_path)
                        except ReviewRuntimeError:
                            rejected = None
                        if rejected is not None:
                            rejected_bytes = canonical_json_bytes(rejected)
                            rejected_sha256 = sha256_bytes(rejected_bytes)
                            path = (
                                output_root
                                / "rejected"
                                / _stage_directory(unit)
                                / unit.unit_id
                                / f"attempt-{attempt:02d}-{rejected_sha256}.json"
                            )
                            write_once(path, rejected_bytes)
                            rejected_path = path.relative_to(output_root).as_posix()
                            model_response_sha256 = rejected_sha256
            except subprocess.TimeoutExpired as exc:
                error_kind = "timeout"
                stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
                stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            except OSError:
                error_kind = "process_error"
        attempts.append(
            {
                "attempt": attempt,
                "elapsed_milliseconds": round((time.monotonic() - started) * 1000),
                "error_detail": error_detail,
                "error_kind": error_kind,
                "model_response_sha256": model_response_sha256,
                "prompt_sha256": prompt_sha256,
                "rejected_response_path": rejected_path,
                "response_sha256": response_sha256,
                "returncode": returncode,
                "stderr_byte_count": len(stderr),
                "stderr_sha256": sha256_bytes(stderr),
                "stdout_byte_count": len(stdout),
                "stdout_sha256": sha256_bytes(stdout),
                "validation_feedback_sha256": feedback_sha256,
            }
        )
        if accepted is not None:
            break

    if accepted is None or accepted_model_response is None or accepted_prompt_sha256 is None:
        failure = {
            **expected_receipt,
            "attempts": attempts,
            "failure": "retry_limit_exhausted",
        }
        failure_bytes = canonical_json_bytes(failure)
        failure_sha256 = sha256_bytes(failure_bytes)
        write_once(
            output_root
            / "failures"
            / _stage_directory(unit)
            / unit.unit_id
            / f"{failure_sha256}.json",
            failure_bytes,
        )
        raise ReviewRetryExhausted(f"Codex review unit failed after finite retries: {unit.unit_id}")

    model_response_bytes = canonical_json_bytes(accepted_model_response)
    response_bytes = canonical_json_bytes(accepted)
    receipt = {
        **expected_receipt,
        "accepted_attempt": len(attempts),
        "accepted_prompt_sha256": accepted_prompt_sha256,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "model_response_sha256": sha256_bytes(model_response_bytes),
        "response_sha256": sha256_bytes(response_bytes),
    }
    receipt_bytes = canonical_json_bytes(receipt)
    temporary_target = Path(tempfile.mkdtemp(prefix=f".{unit.unit_id}.tmp-", dir=stage_root))
    try:
        _write_file(temporary_target / "model_response.json", model_response_bytes)
        _write_file(temporary_target / "output_schema.json", schema_bytes)
        _write_file(temporary_target / "response.json", response_bytes)
        _write_file(temporary_target / "receipt.json", receipt_bytes)
        _fsync_directory(temporary_target)
        if target.exists():
            raise ReviewRuntimeError(f"review artifact appeared concurrently: {target}")
        os.rename(temporary_target, target)
        _fsync_directory(stage_root)
    finally:
        if temporary_target.exists():
            shutil.rmtree(temporary_target)
    return StageArtifact(
        unit=unit,
        response=accepted,
        response_sha256=sha256_bytes(response_bytes),
        receipt_sha256=sha256_bytes(receipt_bytes),
        directory=target,
        resumed=False,
    )


def verify_frozen_review_artifact(
    *,
    unit: ReviewUnit,
    artifact_root: Path,
    target: Path,
    schema: Mapping[str, Any],
    render_prompt: PromptRenderer,
    validate_response: ResponseValidator,
    expected_receipt: Mapping[str, Any],
) -> StageArtifact:
    """Read-only verify one explicitly migrated immutable review artifact."""

    _validate_unit(unit)
    _reject_symlink_components(artifact_root, stop=Path(artifact_root.absolute().anchor))
    _reject_symlink_components(target, stop=artifact_root)
    _verify_image_attachments(unit)
    schema_bytes = canonical_json_bytes(dict(schema))
    return _resume_artifact(
        unit=unit,
        target=target,
        expected_receipt=dict(expected_receipt),
        schema_bytes=schema_bytes,
        render_prompt=render_prompt,
        validate_response=validate_response,
    )


def _resume_artifact(
    *,
    unit: ReviewUnit,
    target: Path,
    expected_receipt: Mapping[str, Any],
    schema_bytes: bytes,
    render_prompt: PromptRenderer,
    validate_response: ResponseValidator,
) -> StageArtifact:
    receipt_path = target / "receipt.json"
    model_response_path = target / "model_response.json"
    response_path = target / "response.json"
    schema_path = target / "output_schema.json"
    _reject_symlink_components(target, stop=target)
    for path in (receipt_path, model_response_path, response_path, schema_path):
        _reject_symlink_components(path, stop=target)
    if not (
        receipt_path.is_file()
        and model_response_path.is_file()
        and response_path.is_file()
        and schema_path.is_file()
    ):
        raise ReviewRuntimeError(f"incomplete resumable review artifact: {target}")
    receipt = _read_json_object(receipt_path)
    for key, expected in expected_receipt.items():
        if receipt.get(key) != expected:
            raise ReviewRuntimeError(f"resume receipt mismatch for {unit.unit_id}: {key}")
    if schema_path.read_bytes() != schema_bytes:
        raise ReviewRuntimeError(f"resume schema mismatch for {unit.unit_id}")
    model_response_bytes = model_response_path.read_bytes()
    if sha256_bytes(model_response_bytes) != receipt.get("model_response_sha256"):
        raise ReviewRuntimeError(f"resume model response hash mismatch for {unit.unit_id}")
    model_response = _read_json_object(model_response_path)
    if canonical_json_bytes(model_response) != model_response_bytes:
        raise ReviewRuntimeError(f"resume model response is not canonical for {unit.unit_id}")
    response_bytes = response_path.read_bytes()
    if sha256_bytes(response_bytes) != receipt.get("response_sha256"):
        raise ReviewRuntimeError(f"resume response hash mismatch for {unit.unit_id}")
    response = _read_json_object(response_path)
    if canonical_json_bytes(response) != response_bytes:
        raise ReviewRuntimeError(f"resume response is not canonical for {unit.unit_id}")
    _validate_attempt_chain(receipt, unit=unit, render_prompt=render_prompt)
    validated = validate_response(model_response)
    normalized = dict(response if validated is None else validated)
    if canonical_json_bytes(normalized) != response_bytes:
        raise ReviewRuntimeError(f"resume validator changed response for {unit.unit_id}")
    return StageArtifact(
        unit=unit,
        response=response,
        response_sha256=receipt["response_sha256"],
        receipt_sha256=file_sha256(receipt_path),
        directory=target,
        resumed=True,
    )


def _validate_attempt_chain(
    receipt: Mapping[str, Any], *, unit: ReviewUnit, render_prompt: PromptRenderer
) -> None:
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ReviewRuntimeError(f"resume receipt has no attempts for {unit.unit_id}")
    if receipt.get("attempt_count") != len(attempts):
        raise ReviewRuntimeError(f"resume attempt count mismatch for {unit.unit_id}")
    if receipt.get("accepted_attempt") != len(attempts):
        raise ReviewRuntimeError(f"resume accepted attempt mismatch for {unit.unit_id}")
    feedback: str | None = None
    for index, raw in enumerate(attempts, start=1):
        if not isinstance(raw, Mapping) or raw.get("attempt") != index:
            raise ReviewRuntimeError(f"resume attempt order mismatch for {unit.unit_id}")
        expected_feedback_sha256 = (
            sha256_bytes(feedback.encode("utf-8")) if feedback is not None else None
        )
        if raw.get("validation_feedback_sha256") != expected_feedback_sha256:
            raise ReviewRuntimeError(f"resume feedback mismatch for {unit.unit_id}")
        prompt_bytes = _prompt_bytes(render_prompt(feedback), unit=unit)
        if raw.get("prompt_sha256") != sha256_bytes(prompt_bytes):
            raise ReviewRuntimeError(f"resume prompt mismatch for {unit.unit_id}")
        accepted = index == len(attempts)
        if accepted and raw.get("error_kind") is not None:
            raise ReviewRuntimeError(f"resume accepted attempt has an error for {unit.unit_id}")
        if not accepted and raw.get("error_kind") is None:
            raise ReviewRuntimeError(f"resume failed attempt lacks an error for {unit.unit_id}")
        if raw.get("error_kind") == "invalid_response":
            detail = raw.get("error_detail")
            if not isinstance(detail, str) or not detail:
                raise ReviewRuntimeError(
                    f"resume invalid-response feedback is missing for {unit.unit_id}"
                )
            feedback = detail
    if receipt.get("accepted_prompt_sha256") != attempts[-1].get("prompt_sha256"):
        raise ReviewRuntimeError(f"resume accepted prompt mismatch for {unit.unit_id}")
    if attempts[-1].get("response_sha256") != receipt.get("response_sha256"):
        raise ReviewRuntimeError(f"resume accepted response mismatch for {unit.unit_id}")
    if attempts[-1].get("model_response_sha256") != receipt.get("model_response_sha256"):
        raise ReviewRuntimeError(f"resume accepted model response mismatch for {unit.unit_id}")


def _codex_argv(
    *,
    codex_bin: str,
    model: str,
    temporary: Path,
    schema_path: Path,
    output_path: Path,
    image_paths: Sequence[Path],
) -> list[str]:
    argv = [
        codex_bin,
        "exec",
        "--ephemeral",
        "-s",
        "read-only",
        "-m",
        model,
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--color",
        "never",
        "-C",
        str(temporary),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    for feature in REVIEWER_DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    for image_path in image_paths:
        argv.extend(["--image", str(image_path)])
    argv.append("-")
    return argv


def _stage_directory(unit: ReviewUnit) -> str:
    return f"phase-{unit.phase.lower()}-{unit.stage.lower()}"


def _validate_unit(unit: ReviewUnit) -> None:
    if unit.phase not in {"A", "B"}:
        raise ReviewRuntimeError(f"invalid review phase: {unit.phase}")
    if unit.stage not in {"PRIMARY", "SECONDARY", "ADJUDICATION"}:
        raise ReviewRuntimeError(f"invalid review stage: {unit.stage}")
    if not _UNIT_ID_RE.fullmatch(unit.unit_id):
        raise ReviewRuntimeError(f"unsafe review unit id: {unit.unit_id!r}")
    if not unit.task_key:
        raise ReviewRuntimeError("review unit task_key must be non-empty")
    if not re.fullmatch(r"[0-9a-f]{64}", unit.card_sha256):
        raise ReviewRuntimeError("review unit card_sha256 is invalid")
    if not (len(unit.image_paths) == len(unit.image_sha256s) == len(unit.image_byte_lengths)):
        raise ReviewRuntimeError("review unit image attachment vectors do not align")
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in unit.image_sha256s):
        raise ReviewRuntimeError("review unit image SHA-256 is invalid")
    if any(
        not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length <= 0
        for byte_length in unit.image_byte_lengths
    ):
        raise ReviewRuntimeError("review unit image byte length is invalid")


def _verify_image_attachments(unit: ReviewUnit) -> list[dict[str, Any]]:
    records = []
    for index, (path, expected_sha256, expected_byte_length) in enumerate(
        zip(
            unit.image_paths,
            unit.image_sha256s,
            unit.image_byte_lengths,
            strict=True,
        ),
        start=1,
    ):
        if not path.is_file():
            raise ReviewRuntimeError(f"review image is not a regular file: {path}")
        try:
            byte_length = path.stat().st_size
        except OSError as exc:
            raise ReviewRuntimeError(f"cannot stat review image {path}: {exc}") from exc
        if byte_length != expected_byte_length:
            raise ReviewRuntimeError(
                f"review image byte length mismatch at attachment {index}: {path}"
            )
        actual_sha256 = file_sha256(path)
        if actual_sha256 != expected_sha256:
            raise ReviewRuntimeError(f"review image SHA-256 mismatch at attachment {index}: {path}")
        records.append(
            {
                "attachment_index": index,
                "blob_sha256": expected_sha256,
                "byte_length": expected_byte_length,
            }
        )
    return records


def _prompt_bytes(prompt: str, *, unit: ReviewUnit) -> bytes:
    if not isinstance(prompt, str) or not prompt:
        raise ReviewRuntimeError(f"empty prompt for review unit: {unit.unit_id}")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ReviewRuntimeError(f"prompt exceeds character limit: {unit.unit_id}")
    encoded = prompt.encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ReviewRuntimeError(f"prompt exceeds byte limit: {unit.unit_id}")
    return encoded


def _validate_estimated_request_payload(
    *, prompt_byte_count: int, image_attachments: Sequence[Mapping[str, Any]]
) -> None:
    encoded_image_bytes = sum(
        4 * ((int(record["byte_length"]) + 2) // 3) for record in image_attachments
    )
    estimated = prompt_byte_count + encoded_image_bytes + REQUEST_PAYLOAD_OVERHEAD_BYTES
    if estimated > MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES:
        raise ReviewRuntimeError(
            "estimated Codex request payload exceeds conservative byte limit: "
            f"{estimated} > {MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES}"
        )


def _validation_feedback(exc: BaseException) -> str:
    code = getattr(exc, "code", "review_runtime_validation_error")
    path = getattr(exc, "path", "$")
    return json.dumps(
        {"code": str(code), "message": str(exc)[:800], "path": str(path)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )[:1000]


def _read_json_object(path: Path) -> dict[str, Any]:
    _reject_symlink_components(path, stop=Path(path.absolute().anchor))
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_strict_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewRuntimeError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewRuntimeError(f"JSON artifact must be an object: {path}")
    return value


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _write_file(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    mode = "xb" if exclusive else "wb"
    with path.open(mode) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_symlink_components(path: Path, *, stop: Path) -> None:
    current = path.absolute()
    stop_absolute = stop.absolute()
    while True:
        if current.is_symlink():
            raise ReviewRuntimeError(f"symlink artifact path is forbidden: {current}")
        if current.absolute() == stop_absolute:
            return
        if current.parent == current:
            raise ReviewRuntimeError(f"artifact path escaped its boundary: {path}")
        current = current.parent


__all__ = [
    "MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES",
    "MAX_PROMPT_CHARS",
    "MAX_PROMPT_BYTES",
    "REQUEST_PAYLOAD_OVERHEAD_BYTES",
    "REVIEWER_DISABLED_FEATURES",
    "RUNTIME_SCHEMA_VERSION",
    "ReviewRetryExhausted",
    "ReviewRuntimeError",
    "ReviewUnit",
    "StageArtifact",
    "execute_review_unit",
    "file_sha256",
    "sha256_bytes",
    "verify_frozen_review_artifact",
    "write_once",
]
