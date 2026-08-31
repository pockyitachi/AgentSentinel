"""Repo-external, append-only artifact and attempt-event store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import (
    PROVIDER_RESULT_SCHEMA_VERSION,
    JsonValue,
    PortableContractError,
    ProviderResult,
    ProviderResultStatus,
    canonical_json_bytes,
)
from mobile_world.offline.causal_replay.provider import validate_provider_result
from mobile_world.offline.causal_replay_runner.contracts import (
    ATTEMPT_EVENT_SCHEMA_VERSION,
    MAXIMUM_PROVIDER_ATTEMPTS,
    PROTOCOL_VERSION,
    RETRYABLE_FAILURES,
    TERMINAL_ATTEMPT_SCHEMA_VERSION,
    AttemptEvent,
    AttemptEventKind,
    InvocationPlan,
    ReplayRunnerError,
    TerminalStatus,
)
from mobile_world.offline.causal_replay_runner.provider_codec import (
    FAKE_PROVIDER_CODEC_ID,
    FAKE_PROVIDER_ENDPOINT_REVISION,
    PROVIDER_CONTRACT_VERSION,
)
from mobile_world.offline.causal_replay_runner.schedule import (
    validate_invocation_plan_identity,
)

_EVENT_FILE_RE = re.compile(r"^(?P<seq>[0-9]{4})-(?P<sha>[0-9a-f]{64})\.json$")
_RUN_ID_RE = re.compile(r"^g1run-[0-9a-f]{24}$")
_EVENT_KEYS = {
    "schema_version",
    "record_type",
    "protocol_version",
    "event_id",
    "run_id",
    "seq",
    "previous_event_sha256",
    "event_kind",
    "provider_attempt_index",
    "payload",
    "raw_collector_event",
    "generated_action_executed",
}
_TERMINAL_KEYS = {
    "schema_version",
    "record_type",
    "protocol_version",
    "run_id",
    "status",
    "provider_attempt_count",
    "final_event_sha256",
    "provider_result",
    "parser_diagnostics",
    "retry_reason",
    "idempotent_reuse",
    "generated_action_executed",
    "response_fed_to_later_request",
    "scientific_count_eligible",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "/./" in value
        or value.startswith("./")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReplayRunnerError("UNSAFE_OUTPUT_PATH", "derived artifact path is unsafe")
    return path


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:  # type: ignore[redundant-expr]
        raise ReplayRunnerError("RUN_ID_INVALID", "run ID is not canonical")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_artifact_ref(value: object, *, media_type: str = "application/json") -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"relative_path", "sha256", "byte_count", "media_type"}
        and isinstance(value.get("relative_path"), str)
        and re.fullmatch(
            r"objects/sha256/[0-9a-f]{2}/[0-9a-f]{64}",
            cast(str, value["relative_path"]),
        )
        is not None
        and _is_sha256(value.get("sha256"))
        and cast(str, value["relative_path"]).endswith(cast(str, value["sha256"]))
        and type(value.get("byte_count")) is int
        and cast(int, value["byte_count"]) >= 0
        and value.get("media_type") == media_type
    )


def _is_provider_response_ref(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"relative_path", "sha256", "byte_count", "media_type", "schema_version"}
        and value.get("schema_version") is None
        and value.get("media_type") == "application/octet-stream"
        and isinstance(value.get("relative_path"), str)
        and re.fullmatch(
            r"objects/sha256/[0-9a-f]{2}/[0-9a-f]{64}",
            cast(str, value["relative_path"]),
        )
        is not None
        and _is_sha256(value.get("sha256"))
        and cast(str, value["relative_path"]).endswith(cast(str, value["sha256"]))
        and type(value.get("byte_count")) is int
        and cast(int, value["byte_count"]) >= 0
    )


def _is_parser_diagnostics(value: object, *, parsed: bool) -> bool:
    if not isinstance(value, dict):
        return False
    common = (
        value.get("parser_binding_id") == "mobileworld.g1.fixture-json-action-parser/v1"
        and type(value.get("action_count")) is int
    )
    if parsed:
        return (
            common
            and set(value)
            == {"parser_binding_id", "parse_outcome", "action_count", "action_sha256"}
            and value.get("parse_outcome") == "PARSED"
            and value.get("action_count") == 1
            and _is_sha256(value.get("action_sha256"))
        )
    outcome = value.get("parse_outcome")
    error = value.get("error_code")
    return (
        common
        and set(value) == {"parser_binding_id", "parse_outcome", "action_count", "error_code"}
        and value.get("action_count") == 0
        and (
            (outcome == "FAILED" and error in {"MALFORMED_RESPONSE", "PARSER_FAILURE"})
            or (outcome == "REFUSAL" and error == "REFUSAL")
            or (outcome == "EMPTY_RESPONSE" and error == "EMPTY_RESPONSE")
        )
    )


def _validate_event_payload(kind: AttemptEventKind, payload: dict[str, Any]) -> None:
    if kind is AttemptEventKind.PLANNED:
        blocked_keys = {"invocation_plan_sha256", "preflight_outcome"}
        if set(payload) == blocked_keys:
            valid = (
                _is_sha256(payload.get("invocation_plan_sha256"))
                and payload.get("preflight_outcome") == "BLOCKED_BEFORE_FAKE_PROVIDER"
            )
        else:
            required = {
                "invocation_plan_sha256",
                "selected_plan_ref",
                "paired_plan_set_ref",
                "invariance_report_ref",
                "render_result_ref",
                "validation_receipt_ref",
                "final_application_request_ref",
                "target_diff_ref",
                "blinding_commitment",
            }
            commitment = payload.get("blinding_commitment")
            valid = (
                set(payload) == required
                and _is_sha256(payload.get("invocation_plan_sha256"))
                and all(
                    _is_artifact_ref(payload.get(key)) for key in required if key.endswith("_ref")
                )
                and isinstance(commitment, dict)
                and set(commitment)
                == {
                    "blinding_mapping_sha256",
                    "key_commitment_sha256",
                    "mapping_persisted_before_response",
                }
                and _is_sha256(commitment.get("blinding_mapping_sha256"))
                and _is_sha256(commitment.get("key_commitment_sha256"))
                and commitment.get("mapping_persisted_before_response") is True
            )
    elif kind is AttemptEventKind.PREFLIGHT_ALLOWED:
        valid = (
            set(payload)
            == {
                "fake_conformance",
                "external_provider_invocation_allowed",
                "encoded_request_ref",
            }
            and payload.get("fake_conformance") is True
            and payload.get("external_provider_invocation_allowed") is False
            and _is_artifact_ref(payload.get("encoded_request_ref"))
        )
    elif kind is AttemptEventKind.PREFLIGHT_BLOCKED:
        reason = payload.get("reason_code")
        valid = (
            set(payload)
            == {
                "reason_code",
                "provider_invocation_allowed",
                "external_provider_invoked",
                "treatment_response_generation_allowed",
            }
            and isinstance(reason, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", reason) is not None
            and payload.get("provider_invocation_allowed") is False
            and payload.get("external_provider_invoked") is False
            and payload.get("treatment_response_generation_allowed") is False
        )
    elif kind is AttemptEventKind.ATTEMPT_STARTED:
        valid = (
            set(payload) == {"encoded_request_sha256", "simulated", "external_provider_invoked"}
            and _is_sha256(payload.get("encoded_request_sha256"))
            and payload.get("simulated") is True
            and payload.get("external_provider_invoked") is False
        )
    elif kind is AttemptEventKind.CHUNK:
        valid = (
            set(payload) == {"chunk_index", "byte_count", "sha256", "is_final", "content_ref"}
            and type(payload.get("chunk_index")) is int
            and cast(int, payload["chunk_index"]) >= 0
            and type(payload.get("byte_count")) is int
            and cast(int, payload["byte_count"]) >= 0
            and _is_sha256(payload.get("sha256"))
            and type(payload.get("is_final")) is bool
            and _is_artifact_ref(payload.get("content_ref"), media_type="application/octet-stream")
        )
    elif kind is AttemptEventKind.RETURNED:
        valid = (
            set(payload) == {"response_ref", "exchange_ref"}
            and _is_artifact_ref(payload.get("response_ref"), media_type="application/octet-stream")
            and _is_artifact_ref(payload.get("exchange_ref"))
        )
    elif kind is AttemptEventKind.FAILED:
        valid = (
            set(payload) == {"error_code", "retryable", "exchange_ref"}
            and payload.get("error_code") in RETRYABLE_FAILURES
            and payload.get("retryable") is True
            and _is_artifact_ref(payload.get("exchange_ref"))
        )
    elif kind in {AttemptEventKind.PARSED, AttemptEventKind.PARSE_FAILED}:
        valid = (
            set(payload) == {"provider_result_sha256", "parser_diagnostics"}
            and _is_sha256(payload.get("provider_result_sha256"))
            and _is_parser_diagnostics(
                payload.get("parser_diagnostics"),
                parsed=kind is AttemptEventKind.PARSED,
            )
        )
    elif kind is AttemptEventKind.TERMINAL:
        if payload.get("terminal_status") == TerminalStatus.RETRY_EXHAUSTED.value:
            valid = (
                set(payload) == {"terminal_status", "retry_reason", "preceding_event_sha256"}
                and payload.get("retry_reason") in RETRYABLE_FAILURES
                and _is_sha256(payload.get("preceding_event_sha256"))
            )
        else:
            valid = (
                set(payload)
                == {
                    "terminal_status",
                    "retry_reason",
                    "preceding_event_sha256",
                    "generated_action_executed",
                }
                and payload.get("terminal_status")
                in {
                    TerminalStatus.SUCCESS.value,
                    TerminalStatus.PARSE_ERROR.value,
                    TerminalStatus.REFUSAL.value,
                    TerminalStatus.EMPTY_RESPONSE.value,
                    TerminalStatus.NO_OP.value,
                }
                and payload.get("retry_reason") is None
                and _is_sha256(payload.get("preceding_event_sha256"))
                and payload.get("generated_action_executed") is False
            )
    else:
        valid = False
    if not valid:
        raise ReplayRunnerError(
            "ATTEMPT_LEDGER_INVALID", "event payload is not the closed contract shape"
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _event_subject(record: dict[str, Any]) -> dict[str, JsonValue]:
    return {
        "run_id": cast(str, record["run_id"]),
        "seq": cast(int, record["seq"]),
        "previous_event_sha256": cast(str | None, record["previous_event_sha256"]),
        "event_kind": cast(str, record["event_kind"]),
        "provider_attempt_index": cast(int | None, record["provider_attempt_index"]),
        "payload": cast(dict[str, JsonValue], record["payload"]),
    }


def _validate_event_prefix(records: tuple[dict[str, Any], ...]) -> None:
    """Validate the exact state-machine prefix while allowing crash truncation."""

    if not records:
        return
    kinds = [cast(str, item["event_kind"]) for item in records]
    if kinds[0] != AttemptEventKind.PLANNED.value:
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "ledger must begin with PLANNED")
    if len(kinds) == 1:
        return
    if kinds[1] not in {
        AttemptEventKind.PREFLIGHT_ALLOWED.value,
        AttemptEventKind.PREFLIGHT_BLOCKED.value,
    }:
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "preflight event is missing")
    planned_payload = cast(dict[str, Any], records[0]["payload"])
    planned_to_block = "preflight_outcome" in planned_payload
    if (planned_to_block and kinds[1] != AttemptEventKind.PREFLIGHT_BLOCKED.value) or (
        not planned_to_block and kinds[1] != AttemptEventKind.PREFLIGHT_ALLOWED.value
    ):
        raise ReplayRunnerError(
            "ATTEMPT_LEDGER_INVALID",
            "PLANNED payload and preflight decision branch are inconsistent",
        )
    if kinds[1] == AttemptEventKind.PREFLIGHT_BLOCKED.value:
        if len(kinds) != 2:
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "blocked preflight must be terminal")
        return
    index = 2
    expected_attempt = 1
    while index < len(records):
        started = records[index]
        if (
            started["event_kind"] != AttemptEventKind.ATTEMPT_STARTED.value
            or started["provider_attempt_index"] != expected_attempt
        ):
            raise ReplayRunnerError(
                "ATTEMPT_LEDGER_INVALID", "provider attempt start is missing or out of order"
            )
        index += 1
        if index == len(records):
            return
        while index < len(records) and records[index]["event_kind"] == AttemptEventKind.CHUNK.value:
            if records[index]["provider_attempt_index"] != expected_attempt:
                raise ReplayRunnerError(
                    "ATTEMPT_LEDGER_INVALID", "chunk binds another provider attempt"
                )
            index += 1
        if index == len(records):
            return
        outcome = records[index]
        if outcome["provider_attempt_index"] != expected_attempt or outcome["event_kind"] not in {
            AttemptEventKind.RETURNED.value,
            AttemptEventKind.FAILED.value,
        }:
            raise ReplayRunnerError(
                "ATTEMPT_LEDGER_INVALID", "provider attempt lacks RETURNED or FAILED"
            )
        index += 1
        if outcome["event_kind"] == AttemptEventKind.FAILED.value:
            if index == len(records):
                return
            if records[index]["event_kind"] == AttemptEventKind.TERMINAL.value:
                terminal = records[index]
                failed_payload = cast(dict[str, Any], outcome["payload"])
                terminal_payload = cast(dict[str, Any], terminal["payload"])
                if (
                    expected_attempt != MAXIMUM_PROVIDER_ATTEMPTS
                    or terminal["provider_attempt_index"] != expected_attempt
                    or terminal_payload.get("terminal_status")
                    != TerminalStatus.RETRY_EXHAUSTED.value
                    or terminal_payload.get("retry_reason") != failed_payload.get("error_code")
                    or terminal_payload.get("preceding_event_sha256")
                    != _sha256(canonical_json_bytes(outcome))
                    or index + 1 != len(records)
                ):
                    raise ReplayRunnerError(
                        "ATTEMPT_LEDGER_INVALID", "failed terminal is out of order"
                    )
                return
            expected_attempt += 1
            if expected_attempt > 3:
                raise ReplayRunnerError(
                    "ATTEMPT_LEDGER_INVALID", "provider attempt count exceeds three"
                )
            continue
        if index == len(records):
            return
        parsed = records[index]
        if parsed["provider_attempt_index"] != expected_attempt or parsed["event_kind"] not in {
            AttemptEventKind.PARSED.value,
            AttemptEventKind.PARSE_FAILED.value,
        }:
            raise ReplayRunnerError(
                "ATTEMPT_LEDGER_INVALID", "returned response lacks one parser outcome"
            )
        index += 1
        if index == len(records):
            return
        terminal = records[index]
        parsed_payload = cast(dict[str, Any], parsed["payload"])
        terminal_payload = cast(dict[str, Any], terminal["payload"])
        diagnostics = cast(dict[str, Any], parsed_payload["parser_diagnostics"])
        parse_outcome = diagnostics.get("parse_outcome")
        expected_statuses = (
            {TerminalStatus.SUCCESS.value, TerminalStatus.NO_OP.value}
            if parsed["event_kind"] == AttemptEventKind.PARSED.value
            else {
                "FAILED": {TerminalStatus.PARSE_ERROR.value},
                "REFUSAL": {TerminalStatus.REFUSAL.value},
                "EMPTY_RESPONSE": {TerminalStatus.EMPTY_RESPONSE.value},
            }.get(cast(str, parse_outcome), set())
        )
        if (
            terminal["event_kind"] != AttemptEventKind.TERMINAL.value
            or terminal["provider_attempt_index"] != expected_attempt
            or terminal_payload.get("terminal_status") not in expected_statuses
            or terminal_payload.get("preceding_event_sha256")
            != _sha256(canonical_json_bytes(parsed))
            or index + 1 != len(records)
        ):
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "terminal event is out of order")
        return


def _validate_candidate_event(
    value: dict[str, Any],
    *,
    run_id: str,
    seq: int,
    previous: str | None,
    existing: tuple[dict[str, Any], ...],
) -> None:
    """Reject an invalid event before append-only bytes become durable."""

    if (
        set(value) != _EVENT_KEYS
        or value.get("schema_version") != ATTEMPT_EVENT_SCHEMA_VERSION
        or value.get("record_type") != "g1_replay_attempt_event"
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("run_id") != run_id
        or type(value.get("seq")) is not int
        or value.get("seq") != seq
        or value.get("previous_event_sha256") != previous
        or not isinstance(value.get("event_kind"), str)
        or not isinstance(value.get("payload"), dict)
        or type(value.get("raw_collector_event")) is not bool
        or value.get("raw_collector_event") is not False
        or type(value.get("generated_action_executed")) is not bool
        or value.get("generated_action_executed") is not False
    ):
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event envelope is invalid")
    try:
        kind = AttemptEventKind(cast(str, value["event_kind"]))
    except (TypeError, ValueError) as exc:
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event kind is invalid") from exc
    _validate_event_payload(kind, cast(dict[str, Any], value["payload"]))
    provider_index = value.get("provider_attempt_index")
    needs_attempt = kind not in {
        AttemptEventKind.PLANNED,
        AttemptEventKind.PREFLIGHT_ALLOWED,
        AttemptEventKind.PREFLIGHT_BLOCKED,
    }
    if (needs_attempt and (type(provider_index) is not int or not 1 <= provider_index <= 3)) or (
        not needs_attempt and provider_index is not None
    ):
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event attempt index is invalid")
    expected_event_id = (
        f"g1attempt-event-{_sha256(canonical_json_bytes(_event_subject(value)))[:24]}"
    )
    if value.get("event_id") != expected_event_id:
        raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ID is invalid")
    _validate_event_prefix(existing + (value,))


def _provider_result_from_dict(value: dict[str, Any]) -> ProviderResult:
    expected = {
        "schema_version",
        "provider_codec_id",
        "provider_contract_version",
        "endpoint_revision",
        "status",
        "application_request_sha256",
        "encoded_request_sha256",
        "response_sha256",
        "raw_response_ref",
        "normalized_action",
        "normalized_action_sha256",
        "error",
        "model_parameters",
        "model_parameters_sha256",
    }
    if set(value) != expected or value.get("schema_version") != PROVIDER_RESULT_SCHEMA_VERSION:
        raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "provider result shape is invalid")
    try:
        status = ProviderResultStatus(value["status"])
    except (TypeError, ValueError) as exc:
        raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "provider status is invalid") from exc
    result = ProviderResult(
        provider_codec_id=cast(str, value["provider_codec_id"]),
        provider_contract_version=cast(str, value["provider_contract_version"]),
        endpoint_revision=cast(str, value["endpoint_revision"]),
        status=status,
        application_request_sha256=cast(str, value["application_request_sha256"]),
        encoded_request_sha256=cast(str, value["encoded_request_sha256"]),
        response_sha256=cast(str | None, value["response_sha256"]),
        raw_response_ref=cast(dict[str, JsonValue] | None, value["raw_response_ref"]),
        normalized_action=cast(dict[str, JsonValue] | None, value["normalized_action"]),
        normalized_action_sha256=cast(str | None, value["normalized_action_sha256"]),
        error=cast(dict[str, JsonValue] | None, value["error"]),
        model_parameters=cast(dict[str, JsonValue], value["model_parameters"]),
        model_parameters_sha256=cast(str, value["model_parameters_sha256"]),
    )
    try:
        validate_provider_result(result)
    except PortableContractError as exc:
        raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "provider result is invalid") from exc
    return result


class ReplayArtifactStore:
    """Write-once bytes/events store; runner code owns scientific closure proof."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        repo_root: str | os.PathLike[str],
        immutable_roots: tuple[str | os.PathLike[str], ...],
    ) -> None:
        supplied = Path(root)
        repo = Path(repo_root).resolve(strict=True)
        protected: tuple[tuple[Path, str], ...] = ((repo, "OUTPUT_INSIDE_REPOSITORY"),)
        protected += tuple(
            (Path(item).resolve(strict=True), "OUTPUT_OVERLAPS_IMMUTABLE_SOURCE")
            for item in immutable_roots
        )
        if supplied.is_symlink():
            raise ReplayRunnerError("UNSAFE_OUTPUT_ROOT", "derived output root is a symlink")
        candidate = supplied.resolve(strict=False)
        for protected_root, code in protected:
            if _is_within(candidate, protected_root) or _is_within(protected_root, candidate):
                raise ReplayRunnerError(
                    code,
                    "G1 replay artifacts must not overlap repository or immutable sources",
                )
        supplied.mkdir(parents=True, exist_ok=True)
        self.root = supplied.resolve(strict=True)
        for protected_root, code in protected:
            if _is_within(self.root, protected_root) or _is_within(protected_root, self.root):
                raise ReplayRunnerError(
                    code,
                    "G1 replay artifacts must not overlap repository or immutable sources",
                )

    def _open_parent_fd(self, relative: PurePosixPath, *, create: bool) -> tuple[int, str]:
        """Open every output parent component without ever following a symlink."""

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            current_fd = os.open(self.root, directory_flags)
        except OSError as exc:
            raise ReplayRunnerError(
                "UNSAFE_OUTPUT_ROOT", "derived output root is unavailable"
            ) from exc
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise ReplayRunnerError(
                            "OUTPUT_MISSING", "derived artifact parent is missing"
                        ) from None
                    try:
                        os.mkdir(part, 0o755, dir_fd=current_fd)
                        next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                    except OSError as exc:
                        raise ReplayRunnerError(
                            "UNSAFE_OUTPUT_PATH",
                            "derived artifact parent cannot be created safely",
                        ) from exc
                except OSError as exc:
                    raise ReplayRunnerError(
                        "UNSAFE_OUTPUT_PATH",
                        "derived artifact parent is not a no-follow directory",
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, relative.name
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _read_regular_at(parent_fd: int, name: str) -> bytes:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise ReplayRunnerError("OUTPUT_MISSING", "existing output is missing") from exc
        except OSError as exc:
            raise ReplayRunnerError("OUTPUT_UNREADABLE", "existing output cannot be read") from exc
        try:
            metadata_before = os.fstat(fd)
            if not stat.S_ISREG(metadata_before.st_mode):
                raise ReplayRunnerError("OUTPUT_NOT_REGULAR", "existing output is not a file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            metadata_after = os.fstat(fd)
            if (
                len(data) != metadata_before.st_size
                or metadata_before.st_dev != metadata_after.st_dev
                or metadata_before.st_ino != metadata_after.st_ino
                or metadata_before.st_size != metadata_after.st_size
            ):
                raise ReplayRunnerError("OUTPUT_CHANGED", "existing output changed while read")
            return data
        finally:
            os.close(fd)

    def read_logical(self, relative_path: str) -> bytes:
        """Read one exact store-relative regular file through a no-follow path walk."""

        relative = _safe_relative(relative_path)
        parent_fd, name = self._open_parent_fd(relative, create=False)
        try:
            return self._read_regular_at(parent_fd, name)
        finally:
            os.close(parent_fd)

    def _read_regular(self, path: Path) -> bytes:
        if path.is_symlink():
            raise ReplayRunnerError("OUTPUT_NOT_REGULAR", "existing output is a symlink")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ReplayRunnerError("OUTPUT_UNREADABLE", "existing output cannot be read") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReplayRunnerError("OUTPUT_NOT_REGULAR", "existing output is not a file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) != metadata.st_size:
                raise ReplayRunnerError("OUTPUT_CHANGED", "existing output changed while read")
            return data
        finally:
            os.close(fd)

    def write_once(self, relative_path: str, data: bytes) -> bool:
        """Write exact bytes once; return False only for an identical existing file."""

        relative = _safe_relative(relative_path)
        parent_fd, name = self._open_parent_fd(relative, create=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o444, dir_fd=parent_fd)
        except FileExistsError:
            try:
                existing = self._read_regular_at(parent_fd, name)
                if existing != data:
                    raise ReplayRunnerError(
                        "IDEMPOTENCE_COLLISION",
                        "existing logical artifact has different bytes and will not be overwritten",
                    )
                return False
            finally:
                os.close(parent_fd)
        except OSError as exc:
            os.close(parent_fd)
            raise ReplayRunnerError(
                "OUTPUT_WRITE_FAILED", "derived artifact cannot be created"
            ) from exc
        try:
            view = memoryview(data)
            written = 0
            while written < len(data):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise ReplayRunnerError(
                        "OUTPUT_WRITE_FAILED", "short write to derived artifact"
                    )
                written += count
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return True

    def assert_write_compatible(self, relative_path: str, data: bytes) -> None:
        """Reject a conflicting existing target without creating any path."""

        relative = _safe_relative(relative_path)
        try:
            parent_fd, name = self._open_parent_fd(relative, create=False)
        except ReplayRunnerError as exc:
            if exc.code == "OUTPUT_MISSING":
                return
            raise
        try:
            try:
                existing = self._read_regular_at(parent_fd, name)
            except ReplayRunnerError as exc:
                if exc.code == "OUTPUT_MISSING":
                    return
                raise
            if existing != data:
                raise ReplayRunnerError(
                    "IDEMPOTENCE_COLLISION",
                    "existing logical artifact has different bytes and will not be overwritten",
                )
        finally:
            os.close(parent_fd)

    def put_bytes(self, data: bytes, *, media_type: str) -> dict[str, JsonValue]:
        digest = _sha256(data)
        relative = f"objects/sha256/{digest[:2]}/{digest}"
        self.write_once(relative, data)
        return {
            "relative_path": relative,
            "sha256": digest,
            "byte_count": len(data),
            "media_type": media_type,
        }

    def put_json(
        self, value: JsonValue, *, media_type: str = "application/json"
    ) -> dict[str, JsonValue]:
        return self.put_bytes(canonical_json_bytes(value), media_type=media_type)

    def _read_artifact_ref(self, ref: dict[str, Any]) -> bytes:
        if set(ref) not in (
            {"relative_path", "sha256", "byte_count", "media_type"},
            {"relative_path", "sha256", "byte_count", "media_type", "schema_version"},
        ):
            raise ReplayRunnerError("ARTIFACT_REF_INVALID", "artifact ref shape is invalid")
        relative = _safe_relative(cast(str, ref.get("relative_path")))
        try:
            data = self.read_logical(relative.as_posix())
        except ReplayRunnerError as exc:
            raise ReplayRunnerError(
                "ARTIFACT_REF_UNRESOLVED", "artifact bytes are missing or unsafe"
            ) from exc
        if (
            type(ref.get("byte_count")) is not int
            or ref["byte_count"] != len(data)
            or not isinstance(ref.get("sha256"), str)
            or ref["sha256"] != _sha256(data)
            or not isinstance(ref.get("media_type"), str)
            or not ref["media_type"]
        ):
            raise ReplayRunnerError("ARTIFACT_REF_INVALID", "artifact ref binding is invalid")
        return data

    def read_artifact_ref(self, ref: dict[str, JsonValue]) -> bytes:
        """Rehydrate and verify one exact content-addressed artifact reference."""

        return self._read_artifact_ref(cast(dict[str, Any], ref))

    def bind_plan(self, plan: InvocationPlan) -> bool:
        validate_invocation_plan_identity(plan)
        _validate_run_id(plan.run_id)
        return self.write_once(
            f"runs/{plan.run_id}/invocation-plan.json", canonical_json_bytes(plan.to_dict())
        )

    def assert_plan_binding(self, plan: InvocationPlan) -> None:
        """Require a completed run to bind byte-exactly to the current plan."""

        validate_invocation_plan_identity(plan)
        _validate_run_id(plan.run_id)
        path = self.root / "runs" / plan.run_id / "invocation-plan.json"
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise ReplayRunnerError(
                "INVOCATION_PLAN_UNBOUND", "stored invocation plan is missing"
            ) from exc
        if not _is_within(parent, self.root):
            raise ReplayRunnerError(
                "INVOCATION_PLAN_BINDING_MISMATCH", "stored invocation plan escapes output root"
            )
        expected = canonical_json_bytes(plan.to_dict())
        if self._read_regular(parent / path.name) != expected:
            raise ReplayRunnerError(
                "INVOCATION_PLAN_BINDING_MISMATCH",
                "stored invocation plan differs from the current prepared arm",
            )

    def _event_files(self, run_id: str) -> list[Path]:
        _validate_run_id(run_id)
        events = self.root / "runs" / run_id / "events"
        if events.is_symlink():
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ledger is a symlink")
        if not events.exists():
            return []
        if not events.is_dir():
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ledger is not a directory")
        try:
            resolved_events = events.resolve(strict=True)
        except OSError as exc:
            raise ReplayRunnerError(
                "ATTEMPT_LEDGER_INVALID", "event ledger is unavailable"
            ) from exc
        if not _is_within(resolved_events, self.root):
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ledger escapes output root")
        files = sorted(resolved_events.iterdir())
        if any(path.is_symlink() or not path.is_file() for path in files):
            raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ledger has invalid children")
        return files

    def load_events(self, run_id: str) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        previous: str | None = None
        for expected_seq, path in enumerate(self._event_files(run_id)):
            match = _EVENT_FILE_RE.fullmatch(path.name)
            if match is None or int(match.group("seq")) != expected_seq:
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event filename is noncanonical")
            try:
                value = json.loads(self._read_regular(path))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event JSON is invalid") from exc
            if not isinstance(value, dict):
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event must be an object")
            if (
                set(value) != _EVENT_KEYS
                or value.get("schema_version") != ATTEMPT_EVENT_SCHEMA_VERSION
                or value.get("record_type") != "g1_replay_attempt_event"
                or value.get("protocol_version") != PROTOCOL_VERSION
                or type(value.get("seq")) is not int
                or not isinstance(value.get("event_kind"), str)
                or not isinstance(value.get("payload"), dict)
                or type(value.get("raw_collector_event")) is not bool
                or value.get("raw_collector_event") is not False
                or type(value.get("generated_action_executed")) is not bool
                or value.get("generated_action_executed") is not False
            ):
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event envelope is invalid")
            try:
                kind = AttemptEventKind(value["event_kind"])
            except ValueError as exc:
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event kind is invalid") from exc
            _validate_event_payload(kind, cast(dict[str, Any], value["payload"]))
            provider_index = value.get("provider_attempt_index")
            needs_attempt = kind not in {
                AttemptEventKind.PLANNED,
                AttemptEventKind.PREFLIGHT_ALLOWED,
                AttemptEventKind.PREFLIGHT_BLOCKED,
            }
            if (
                needs_attempt and (type(provider_index) is not int or not 1 <= provider_index <= 3)
            ) or (not needs_attempt and provider_index is not None):
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event attempt index is invalid")
            if (
                value.get("run_id") != run_id
                or value.get("seq") != expected_seq
                or value.get("previous_event_sha256") != previous
            ):
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event hash chain is broken")
            data = canonical_json_bytes(cast(JsonValue, value))
            if data != self._read_regular(path):
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event is not canonical JSON")
            digest = _sha256(data)
            if match.group("sha") != digest:
                raise ReplayRunnerError(
                    "ATTEMPT_LEDGER_INVALID", "event filename digest is invalid"
                )
            expected_event_id = (
                f"g1attempt-event-{_sha256(canonical_json_bytes(_event_subject(value)))[:24]}"
            )
            if value.get("event_id") != expected_event_id:
                raise ReplayRunnerError("ATTEMPT_LEDGER_INVALID", "event ID is invalid")
            previous = digest
            records.append(cast(dict[str, Any], value))
        result = tuple(records)
        _validate_event_prefix(result)
        return result

    def append_event(
        self,
        *,
        run_id: str,
        event_kind: AttemptEventKind,
        provider_attempt_index: int | None,
        payload: dict[str, JsonValue],
    ) -> AttemptEvent:
        _validate_run_id(run_id)
        existing = self.load_events(run_id)
        seq = len(existing)
        previous = None if not existing else _sha256(canonical_json_bytes(existing[-1]))
        event_subject: dict[str, JsonValue] = {
            "run_id": run_id,
            "seq": seq,
            "previous_event_sha256": previous,
            "event_kind": event_kind.value,
            "provider_attempt_index": provider_attempt_index,
            "payload": cast(JsonValue, payload),
        }
        event = AttemptEvent(
            event_id=f"g1attempt-event-{_sha256(canonical_json_bytes(event_subject))[:24]}",
            run_id=run_id,
            seq=seq,
            previous_event_sha256=previous,
            event_kind=event_kind,
            provider_attempt_index=provider_attempt_index,
            payload=cast(dict[str, JsonValue], json.loads(canonical_json_bytes(payload))),
        )
        event_value = cast(dict[str, Any], event.to_dict())
        _validate_candidate_event(
            event_value,
            run_id=run_id,
            seq=seq,
            previous=previous,
            existing=existing,
        )
        relative = f"runs/{run_id}/events/{seq:04d}-{event.sha256}.json"
        created = self.write_once(relative, canonical_json_bytes(event_value))
        if not created:
            raise ReplayRunnerError(
                "ATTEMPT_EVENT_DUPLICATE", "append-only event sequence already exists"
            )
        return event

    def ensure_preflight_event(
        self,
        *,
        run_id: str,
        event_kind: AttemptEventKind,
        payload: dict[str, JsonValue],
    ) -> AttemptEvent:
        """Idempotently complete the two-event pre-send ledger prefix."""

        if event_kind not in {
            AttemptEventKind.PLANNED,
            AttemptEventKind.PREFLIGHT_ALLOWED,
            AttemptEventKind.PREFLIGHT_BLOCKED,
        }:
            raise ReplayRunnerError(
                "ATTEMPT_EVENT_INVALID",
                "only pre-send ledger events can be ensured idempotently",
            )
        expected_seq = 0 if event_kind is AttemptEventKind.PLANNED else 1
        existing = self.load_events(run_id)
        if len(existing) <= expected_seq:
            if len(existing) != expected_seq:
                raise ReplayRunnerError(
                    "ATTEMPT_EVENT_PREFIX_INCOMPLETE",
                    "pre-send event cannot skip an earlier ledger entry",
                )
            return self.append_event(
                run_id=run_id,
                event_kind=event_kind,
                provider_attempt_index=None,
                payload=payload,
            )
        current = existing[expected_seq]
        if (
            current.get("event_kind") != event_kind.value
            or current.get("provider_attempt_index") is not None
            or canonical_json_bytes(cast(JsonValue, current.get("payload")))
            != canonical_json_bytes(payload)
        ):
            raise ReplayRunnerError(
                "ATTEMPT_EVENT_COLLISION",
                "existing pre-send event differs from the requested immutable prefix",
            )
        return AttemptEvent(
            event_id=cast(str, current["event_id"]),
            run_id=run_id,
            seq=expected_seq,
            previous_event_sha256=cast(str | None, current["previous_event_sha256"]),
            event_kind=event_kind,
            provider_attempt_index=None,
            payload=cast(
                dict[str, JsonValue],
                json.loads(canonical_json_bytes(cast(JsonValue, current["payload"]))),
            ),
        )

    def _validate_terminal_value(
        self, run_id: str, value: dict[str, Any], events: tuple[dict[str, Any], ...]
    ) -> None:
        if (
            set(value) != _TERMINAL_KEYS
            or value.get("schema_version") != TERMINAL_ATTEMPT_SCHEMA_VERSION
            or value.get("record_type") != "g1_replay_terminal_attempt"
            or value.get("protocol_version") != PROTOCOL_VERSION
            or value.get("run_id") != run_id
            or type(value.get("provider_attempt_count")) is not int
            or type(value.get("idempotent_reuse")) is not bool
            or value.get("idempotent_reuse") is not False
            or type(value.get("generated_action_executed")) is not bool
            or value.get("generated_action_executed") is not False
            or type(value.get("response_fed_to_later_request")) is not bool
            or value.get("response_fed_to_later_request") is not False
            or type(value.get("scientific_count_eligible")) is not bool
            or value.get("scientific_count_eligible") is not False
            or (
                value.get("retry_reason") is not None
                and not isinstance(value.get("retry_reason"), str)
            )
        ):
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "terminal envelope is invalid")
        try:
            status = TerminalStatus(cast(str, value.get("status")))
        except ValueError as exc:
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID", "terminal status is invalid"
            ) from exc
        if not events or events[-1]["event_kind"] != AttemptEventKind.TERMINAL.value:
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "terminal event is missing")
        final_event_sha = _sha256(canonical_json_bytes(cast(JsonValue, events[-1])))
        started = [
            item for item in events if item["event_kind"] == AttemptEventKind.ATTEMPT_STARTED.value
        ]
        if (
            value.get("final_event_sha256") != final_event_sha
            or events[-1]["payload"].get("terminal_status") != status.value
            or events[-1]["payload"].get("preceding_event_sha256")
            != events[-1]["previous_event_sha256"]
            or value.get("provider_attempt_count") != len(started)
            or len(started) < 1
            or len(started) > 3
        ):
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "terminal event binding is invalid")
        provider_value = value.get("provider_result")
        if provider_value is None:
            preceding = events[-2] if len(events) >= 2 else None
            diagnostics = value.get("parser_diagnostics")
            if (
                status is not TerminalStatus.RETRY_EXHAUSTED
                or value.get("provider_attempt_count") != MAXIMUM_PROVIDER_ATTEMPTS
                or value.get("retry_reason") not in RETRYABLE_FAILURES
                or diagnostics != {"parse_outcome": "NOT_RUN"}
                or preceding is None
                or preceding.get("event_kind") != AttemptEventKind.FAILED.value
                or cast(dict[str, Any], preceding.get("payload", {})).get("error_code")
                != value.get("retry_reason")
                or cast(dict[str, Any], events[-1]["payload"]).get("retry_reason")
                != value.get("retry_reason")
            ):
                raise ReplayRunnerError(
                    "TERMINAL_RECORD_INVALID",
                    "retry exhaustion must bind all three attempts and a frozen retry reason",
                )
            return
        if value.get("retry_reason") is not None:
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID", "non-retry terminal cannot carry a retry reason"
            )
        if not isinstance(provider_value, dict):
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "provider result must be an object")
        result = _provider_result_from_dict(provider_value)
        if (
            result.provider_codec_id != FAKE_PROVIDER_CODEC_ID
            or result.provider_contract_version != PROVIDER_CONTRACT_VERSION
            or result.endpoint_revision != FAKE_PROVIDER_ENDPOINT_REVISION
            or result.response_sha256 is None
            or result.raw_response_ref is None
            or not _is_provider_response_ref(result.raw_response_ref)
            or (
                result.status is not ProviderResultStatus.RETURNED
                and (
                    not isinstance(result.error, dict) or result.error.get("retryable") is not False
                )
            )
        ):
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID",
                "terminal provider result is not the sealed network-forbidden fake binding",
            )
        returned_event = events[-3] if len(events) >= 3 else None
        returned_payload = (
            cast(dict[str, Any], returned_event.get("payload", {}))
            if isinstance(returned_event, dict)
            else {}
        )
        response_ref = {
            key: result.raw_response_ref[key]
            for key in ("relative_path", "sha256", "byte_count", "media_type")
        }
        if (
            returned_event is None
            or returned_event.get("event_kind") != AttemptEventKind.RETURNED.value
            or canonical_json_bytes(cast(JsonValue, returned_payload.get("response_ref")))
            != canonical_json_bytes(cast(JsonValue, response_ref))
            or not isinstance(returned_payload.get("exchange_ref"), dict)
        ):
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID",
                "terminal response differs from the final RETURNED event",
            )
        self._read_artifact_ref(cast(dict[str, Any], returned_payload["exchange_ref"]))
        preceding = events[-2] if len(events) >= 2 else None
        diagnostics = value.get("parser_diagnostics")
        expected_parser_kind = (
            AttemptEventKind.PARSED
            if result.status is ProviderResultStatus.RETURNED
            else AttemptEventKind.PARSE_FAILED
        )
        result_error_code = result.error.get("code") if isinstance(result.error, dict) else None
        diagnostics_bind_result = isinstance(diagnostics, dict) and (
            (
                result.status is ProviderResultStatus.RETURNED
                and diagnostics.get("parse_outcome") == "PARSED"
                and diagnostics.get("action_sha256") == result.normalized_action_sha256
            )
            or (
                result.status is ProviderResultStatus.PARSE_ERROR
                and (
                    (
                        result_error_code in {"MALFORMED_RESPONSE", "PARSER_FAILURE"}
                        and diagnostics.get("parse_outcome") == "FAILED"
                        and diagnostics.get("error_code") == result_error_code
                    )
                    or (
                        result_error_code == "EMPTY_RESPONSE"
                        and diagnostics.get("parse_outcome") == "EMPTY_RESPONSE"
                        and diagnostics.get("error_code") == result_error_code
                    )
                )
            )
            or (
                result.status is ProviderResultStatus.PROVIDER_ERROR
                and result_error_code == "REFUSAL"
                and diagnostics.get("parse_outcome") == "REFUSAL"
                and diagnostics.get("error_code") == result_error_code
            )
        )
        if (
            preceding is None
            or preceding.get("event_kind") != expected_parser_kind.value
            or not isinstance(diagnostics, dict)
            or not _is_parser_diagnostics(
                diagnostics,
                parsed=expected_parser_kind is AttemptEventKind.PARSED,
            )
            or cast(dict[str, Any], preceding.get("payload", {})).get("provider_result_sha256")
            != _sha256(canonical_json_bytes(cast(JsonValue, provider_value)))
            or canonical_json_bytes(
                cast(JsonValue, cast(dict[str, Any], preceding["payload"])["parser_diagnostics"])
            )
            != canonical_json_bytes(cast(JsonValue, diagnostics))
            or not diagnostics_bind_result
        ):
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID",
                "terminal parser diagnostics differ from the final parser event",
            )
        response_bytes = self._read_artifact_ref(result.raw_response_ref)
        if _sha256(response_bytes) != result.response_sha256:
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID", "raw response bytes differ from provider result"
            )
        expected_status = TerminalStatus.PROVIDER_ERROR
        if result.status is ProviderResultStatus.RETURNED:
            action = result.normalized_action or {}
            expected_status = (
                TerminalStatus.NO_OP
                if action.get("type") in {"wait", "noop", "no_op"}
                or action.get("action_type") in {"wait", "noop", "no_op"}
                else TerminalStatus.SUCCESS
            )
        elif result.status is ProviderResultStatus.PARSE_ERROR:
            code = None if result.error is None else result.error.get("code")
            expected_status = (
                TerminalStatus.EMPTY_RESPONSE
                if code == "EMPTY_RESPONSE"
                else TerminalStatus.PARSE_ERROR
            )
        elif result.error is not None and result.error.get("code") == "REFUSAL":
            expected_status = TerminalStatus.REFUSAL
        if status is TerminalStatus.PROVIDER_ERROR or status is not expected_status:
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID", "terminal status differs from provider result"
            )

    def _read_structural_terminal(self, run_id: str) -> dict[str, Any] | None:
        """Read only the locally well-formed terminal envelope.

        This package-private primitive is deliberately not a completed-run
        validator.  The runner must additionally rehydrate and cross-bind the
        prepared plan, request, exchange, chunk, response, parser, and terminal
        closure before it may expose or reuse the value.
        """

        _validate_run_id(run_id)
        try:
            data = self.read_logical(f"runs/{run_id}/terminal.json")
        except ReplayRunnerError as exc:
            if exc.code == "OUTPUT_MISSING":
                return None
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID",
                "terminal record is present but unsafe or unreadable",
            ) from exc
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayRunnerError(
                "TERMINAL_RECORD_INVALID", "terminal record is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "terminal record must be an object")
        if canonical_json_bytes(cast(JsonValue, value)) != data:
            raise ReplayRunnerError("TERMINAL_RECORD_INVALID", "terminal record is noncanonical")
        records = self.load_events(run_id)
        typed = cast(dict[str, Any], value)
        self._validate_terminal_value(run_id, typed, records)
        return typed

    def _commit_structural_terminal(self, run_id: str, value: dict[str, JsonValue]) -> bool:
        """Write a locally valid envelope after runner-owned closure proof.

        Callers outside ``runner.py`` must not use this primitive.  Keeping it
        package-private prevents a structural receipt from being mistaken for
        proof of the full derived-artifact closure.
        """

        _validate_run_id(run_id)
        self._validate_terminal_value(run_id, cast(dict[str, Any], value), self.load_events(run_id))
        return self.write_once(f"runs/{run_id}/terminal.json", canonical_json_bytes(value))

    def _assert_no_ambiguous_delivery(self, run_id: str) -> None:
        """Reject a nonterminal ledger that may already have sent a request."""

        _validate_run_id(run_id)
        events = self.load_events(run_id)
        if events and events[-1].get("event_kind") == AttemptEventKind.PREFLIGHT_BLOCKED.value:
            raise ReplayRunnerError(
                "PREFLIGHT_PREVIOUSLY_BLOCKED",
                "a blocked logical run cannot be resumed or sent",
            )
        if any(event.get("event_kind") == "ATTEMPT_STARTED" for event in events):
            raise ReplayRunnerError(
                "AMBIGUOUS_PROVIDER_DELIVERY",
                "an attempt started without a committed terminal receipt; automatic resend is forbidden",
            )
