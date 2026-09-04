"""Cancellable, process-isolated OpenAI provider-attempt boundary.

CPU tests use a closed module-owned worker with no request, secret, model, or
network access.  The production runner accepts only the exact sealed
post-preflight factory and case lease.  Its child process, never the actor
process, owns the secret and OpenAI client.  Construction requires the exact
owner-confirmed manifest, sealed preflight, case lease, stage, and pricing
binding; only an explicit call dispatches the child to the provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import re
import signal
import threading
import time
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Final, cast

from mobile_world.runtime.sentinel.r2_4.live_run import OpenAIResponsesStageV1, OpenAIRoleV1
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CaseExecutionLeaseV1,
    CaseExecutionScopeV1,
    ProductionPostPreflightFactoryV1,
    case_execution_lease_sha256,
    openai_stage_sha256,
)

LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-attempt-authority/v1"
)
LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt/v1"
LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt-root/v1"
)
OPENAI_REQUEST_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-openai-provider-request/v1"
HISTORY_POLICY_REQUEST_SCHEMA_VERSION = OPENAI_REQUEST_SCHEMA_VERSION
LIVE_ATTEMPT_PRICING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-live-pricing/v1"
LIVE_ATTEMPT_DEADLINE_BINDING_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-attempt-deadline-binding/v1"
)

# The sealed production runner may first allow one cooperative wait, then waits
# the same grace after TERM, and finally waits at least the KILL-reap interval
# before it may emit TERMINATION_UNCONFIRMED.  Cleanup reservation code imports
# the computed worst-case bound; these timings must not drift as independent
# literals across the attempt and driver layers.
PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1: Final[int] = 1_000
PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1: Final[int] = 5_000
PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1: Final[int] = (
    2 * PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1
    + max(
        PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1,
        PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1,
    )
) * 1_000_000

_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_MODEL_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_FAILURE_CODE: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_CPU_INPUT_TOKENS: Final[int] = 7
_CPU_OUTPUT_TOKENS: Final[int] = 3
_CPU_COST_USD_MICROS: Final[int] = 1
_MAX_DURATION_NS: Final[int] = 7 * 24 * 60 * 60 * 1_000_000_000
_MAX_PROVIDER_REQUEST_BYTES: Final[int] = 8 * 1024 * 1024
_REQUEST_SEAL: Final[object] = object()
_DEADLINE_BINDING_SEAL: Final[object] = object()
_RESPONSES_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        "parallel_tool_calls",
        "reasoning",
        "store",
        "stream",
        "text",
        "tool_choice",
        "tools",
        "truncation",
    }
)
_PROVIDER_SDK_LOGGER_NAMES: Final[tuple[str, ...]] = (
    "openai",
    "openai._base_client",
    "openai._legacy_response",
    "openai._response",
    "openai.lib.parsing",
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_ENV_VALUE_ABSENT: Final[object] = object()


class LiveAttemptError(RuntimeError):
    """Typed failure raised before an attempt can be safely represented."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def production_attempt_termination_upper_bound_ns_v1(
    cancel_grace_ms: int = PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1,
) -> int:
    """Return the sealed cooperative-plus-TERM-plus-KILL observation bound."""

    _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 30_000)
    if cancel_grace_ms == PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1:
        return PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
    cancel_grace_ns = cancel_grace_ms * 1_000_000
    kill_reap_ns = PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1 * 1_000_000
    return 2 * cancel_grace_ns + max(kill_reap_ns, cancel_grace_ns)


class LiveAttemptRoleV1(StrEnum):
    """Closed roles; production dispatch permits RUBRIC and HISTORY_POLICY."""

    RUBRIC = "RUBRIC"
    HISTORY_POLICY = "HISTORY_POLICY"
    ACTOR = "ACTOR"


class LiveAttemptStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    CANCELLED_PRE_DISPATCH = "CANCELLED_PRE_DISPATCH"
    CANCELLED_POST_DISPATCH = "CANCELLED_POST_DISPATCH"
    TERMINATION_UNCONFIRMED = "TERMINATION_UNCONFIRMED"
    FAILED = "FAILED"


class LiveAttemptCostStatusV1(StrEnum):
    EXACT = "EXACT"
    UNKNOWN = "UNKNOWN"


class LiveAttemptTerminationV1(StrEnum):
    NONE = "NONE"
    COOPERATIVE = "COOPERATIVE"
    TERM = "TERM"
    KILL = "KILL"
    UNCONFIRMED = "UNCONFIRMED"


class LiveAttemptExecutionKindV1(StrEnum):
    CPU_FIXED_SUBPROCESS = "CPU_FIXED_SUBPROCESS"
    OPENAI_RESPONSES_CHILD_PROCESS = "OPENAI_RESPONSES_CHILD_PROCESS"


class CpuFixedAttemptScriptV1(StrEnum):
    """Module-owned, data-only child behaviors used by CPU tests."""

    COMPLETE_ONCE = "COMPLETE_ONCE"
    BLOCK_AFTER_DISPATCH = "BLOCK_AFTER_DISPATCH"
    IGNORE_TERM_AFTER_DISPATCH = "IGNORE_TERM_AFTER_DISPATCH"


def _require_id(value: object, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LiveAttemptError("INVALID_ATTEMPT_AUTHORITY", f"{label} is invalid")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LiveAttemptError("INVALID_SHA256", f"{label} is not lowercase SHA-256")
    return value


def _require_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LiveAttemptError("INVALID_INTEGER", f"{label} is outside its bound")
    return value


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveAttemptError("CANONICALIZATION_FAILED", "attempt projection is invalid") from exc
    return hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveAttemptError("CANONICALIZATION_FAILED", "provider request is invalid") from exc


def _disable_provider_sdk_logging_for_child() -> tuple[
    object,
    tuple[tuple[logging.Logger, bool, int], ...],
]:
    """Lock down env-driven SDK/http logging in the dedicated live child."""

    openai_log: object = os.environ.pop("OPENAI_LOG", _ENV_VALUE_ABSENT)
    logger_states: list[tuple[logging.Logger, bool, int]] = []
    for logger_name in _PROVIDER_SDK_LOGGER_NAMES:
        sdk_logger = logging.getLogger(logger_name)
        logger_states.append((sdk_logger, sdk_logger.disabled, sdk_logger.level))
        sdk_logger.disabled = True
        sdk_logger.setLevel(logging.CRITICAL + 1)
    return openai_log, tuple(logger_states)


def _restore_provider_sdk_logging_after_child(
    state: tuple[object, tuple[tuple[logging.Logger, bool, int], ...]],
) -> None:
    """Restore state for direct CPU invocation; spawned children exit anyway."""

    openai_log, logger_states = state
    for sdk_logger, disabled, level in logger_states:
        sdk_logger.disabled = disabled
        sdk_logger.setLevel(level)
    if openai_log is _ENV_VALUE_ABSENT:
        os.environ.pop("OPENAI_LOG", None)
    else:
        os.environ["OPENAI_LOG"] = cast(str, openai_log)


@dataclass(frozen=True, slots=True)
class CanonicalHistoryPolicyRequestV1:
    """In-memory canonical OpenAI request; receipts retain only its hash."""

    canonical_bytes: bytes
    request_sha256: str
    byte_count: int
    schema_version: str = HISTORY_POLICY_REQUEST_SCHEMA_VERSION
    _seal: object = _REQUEST_SEAL

    def __post_init__(self) -> None:
        if self._seal is not _REQUEST_SEAL:
            raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request snapshot is untrusted")
        if self.schema_version != HISTORY_POLICY_REQUEST_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "provider request schema differs")
        if type(self.canonical_bytes) is not bytes:
            raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request bytes are mutable")
        if (
            type(self.byte_count) is not int
            or self.byte_count != len(self.canonical_bytes)
            or not 2 <= self.byte_count <= _MAX_PROVIDER_REQUEST_BYTES
        ):
            raise LiveAttemptError("INVALID_PROVIDER_REQUEST", "request byte count differs")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != _require_sha256(
            self.request_sha256, "request_sha256"
        ):
            raise LiveAttemptError("REQUEST_HASH_DRIFT", "provider request hash differs")
        try:
            parsed = json.loads(self.canonical_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise LiveAttemptError("INVALID_PROVIDER_REQUEST", "request JSON is invalid") from exc
        if type(parsed) is not dict or _canonical_bytes(parsed) != self.canonical_bytes:
            raise LiveAttemptError(
                "NONCANONICAL_PROVIDER_REQUEST", "request bytes are not canonical"
            )


def build_canonical_history_policy_request(
    request_kwargs: dict[str, object],
) -> CanonicalHistoryPolicyRequestV1:
    if type(request_kwargs) is not dict:
        raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request must be an exact dict")
    raw = _canonical_bytes(request_kwargs)
    return CanonicalHistoryPolicyRequestV1(
        canonical_bytes=raw,
        request_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        _seal=_REQUEST_SEAL,
    )


def snapshot_canonical_history_policy_request(
    value: CanonicalHistoryPolicyRequestV1,
) -> CanonicalHistoryPolicyRequestV1:
    if type(value) is not CanonicalHistoryPolicyRequestV1:
        raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request type differs")
    return CanonicalHistoryPolicyRequestV1(
        canonical_bytes=bytes(value.canonical_bytes),
        request_sha256=value.request_sha256,
        byte_count=value.byte_count,
        schema_version=value.schema_version,
        _seal=_REQUEST_SEAL,
    )


def _snapshot_openai_stage(value: OpenAIResponsesStageV1) -> OpenAIResponsesStageV1:
    if type(value) is not OpenAIResponsesStageV1:
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "OpenAI stage type differs")
    try:
        return OpenAIResponsesStageV1(
            role=value.role,
            model=value.model,
            endpoint=value.endpoint,
            transport_kind=value.transport_kind,
            transport_authority=value.transport_authority,
            openai_sdk_version=value.openai_sdk_version,
            sdk_max_retries=value.sdk_max_retries,
            external_network_on_call=value.external_network_on_call,
            model_on_call=value.model_on_call,
            max_output_tokens=value.max_output_tokens,
            timeout_ms=value.timeout_ms,
            max_attempts=value.max_attempts,
            store=value.store,
        )
    except Exception as exc:
        raise LiveAttemptError(
            "PROVIDER_REQUEST_STAGE_MISMATCH", "OpenAI stage validation failed"
        ) from exc


def _require_exact_object_fields(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise LiveAttemptError(
            "PROVIDER_REQUEST_STAGE_MISMATCH", f"{label} fields differ from the sealed request"
        )
    return cast(dict[str, object], value)


def _require_input_content(
    request: dict[str, object],
    *,
    image_required: bool,
) -> None:
    input_value = request.get("input")
    if type(input_value) is not list or len(input_value) != 1:
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider input envelope differs")
    message = _require_exact_object_fields(
        input_value[0], frozenset({"role", "content"}), label="provider input message"
    )
    content = message["content"]
    expected_count = 2 if image_required else 1
    if (
        message["role"] != "user"
        or type(message["role"]) is not str
        or type(content) is not list
        or len(content) != expected_count
    ):
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider input content differs")
    text_part = _require_exact_object_fields(
        content[0], frozenset({"type", "text"}), label="provider input text"
    )
    if (
        text_part["type"] != "input_text"
        or type(text_part["type"]) is not str
        or type(text_part["text"]) is not str
        or not text_part["text"]
    ):
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider input text differs")
    if not image_required:
        return
    image_part = _require_exact_object_fields(
        content[1],
        frozenset({"detail", "image_url", "type"}),
        label="provider input image",
    )
    if (
        image_part["type"] != "input_image"
        or type(image_part["type"]) is not str
        or image_part["detail"] != "high"
        or type(image_part["detail"]) is not str
        or type(image_part["image_url"]) is not str
        or not image_part["image_url"].startswith("data:image/")
    ):
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider input image differs")


def _validate_sealed_provider_request(
    request_bytes: bytes,
    *,
    stage: OpenAIResponsesStageV1,
    role: LiveAttemptRoleV1,
) -> dict[str, object]:
    """Rebuild and validate the exact role/stage request before any provider dispatch."""

    trusted_stage = _snapshot_openai_stage(stage)
    if type(role) is not LiveAttemptRoleV1 or role is LiveAttemptRoleV1.ACTOR:
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider role differs")
    expected_stage_role = (
        OpenAIRoleV1.RUBRIC if role is LiveAttemptRoleV1.RUBRIC else OpenAIRoleV1.HISTORY_POLICY
    )
    if trusted_stage.role is not expected_stage_role:
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider stage role differs")
    if (
        type(request_bytes) is not bytes
        or not 2 <= len(request_bytes) <= _MAX_PROVIDER_REQUEST_BYTES
    ):
        raise LiveAttemptError("PROVIDER_REQUEST_STAGE_MISMATCH", "provider request bytes differ")
    try:
        decoded = json.loads(request_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise LiveAttemptError(
            "PROVIDER_REQUEST_STAGE_MISMATCH", "provider request JSON is invalid"
        ) from exc
    request = _require_exact_object_fields(
        decoded, _RESPONSES_REQUEST_FIELDS, label="provider request"
    )
    try:
        if _canonical_bytes(request) != request_bytes:
            raise LiveAttemptError(
                "PROVIDER_REQUEST_STAGE_MISMATCH", "provider request is not canonical"
            )
        if (
            type(request["model"]) is not str
            or request["model"] != trusted_stage.model
            or type(request["max_output_tokens"]) is not int
            or request["max_output_tokens"] != trusted_stage.max_output_tokens
            or type(request["store"]) is not bool
            or request["store"] is not trusted_stage.store
            or request["store"] is not False
            or type(request["stream"]) is not bool
            or request["stream"] is not False
            or type(request["parallel_tool_calls"]) is not bool
            or request["parallel_tool_calls"] is not False
            or request["tools"] != []
            or type(request["tools"]) is not list
            or request["tool_choice"] != "none"
            or type(request["tool_choice"]) is not str
            or request["truncation"] != "disabled"
            or type(request["truncation"]) is not str
        ):
            raise LiveAttemptError(
                "PROVIDER_REQUEST_STAGE_MISMATCH", "provider request config differs"
            )

        if role is LiveAttemptRoleV1.HISTORY_POLICY:
            from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
                GPT56_OUTPUT_SCHEMA_NAME,
                GPT56_POLICY_INSTRUCTIONS,
                GPT56_REASONING_EFFORT,
                ProposalSchemaSnapshotV1,
            )

            expected_instructions = GPT56_POLICY_INSTRUCTIONS
            expected_reasoning_effort = GPT56_REASONING_EFFORT
            expected_schema_name = GPT56_OUTPUT_SCHEMA_NAME
            expected_schema = ProposalSchemaSnapshotV1.from_checked_in().as_dict()
            image_required = True
        else:
            from mobile_world.runtime.sentinel.r2_4.rubric_live import (
                _GENERATE_INSTRUCTIONS,
                _TRACK_INSTRUCTIONS,
                LIVE_RUBRIC_REASONING_EFFORT,
                live_rubric_generate_schema,
                live_rubric_track_schema,
            )

            instructions = request["instructions"]
            if instructions == _GENERATE_INSTRUCTIONS and type(instructions) is str:
                schema_snapshot = live_rubric_generate_schema()
                image_required = False
            elif instructions == _TRACK_INSTRUCTIONS and type(instructions) is str:
                schema_snapshot = live_rubric_track_schema()
                image_required = True
            else:
                raise LiveAttemptError(
                    "PROVIDER_REQUEST_STAGE_MISMATCH", "rubric instructions differ"
                )
            expected_instructions = instructions
            expected_reasoning_effort = LIVE_RUBRIC_REASONING_EFFORT
            expected_schema_name = schema_snapshot.name
            expected_schema = schema_snapshot.as_dict()

        reasoning = _require_exact_object_fields(
            request["reasoning"], frozenset({"effort"}), label="provider reasoning"
        )
        text = _require_exact_object_fields(
            request["text"], frozenset({"format", "verbosity"}), label="provider text"
        )
        output_format = _require_exact_object_fields(
            text["format"],
            frozenset({"name", "schema", "strict", "type"}),
            label="provider output format",
        )
        if (
            type(request["instructions"]) is not str
            or request["instructions"] != expected_instructions
            or reasoning != {"effort": expected_reasoning_effort}
            or text["verbosity"] != "low"
            or type(text["verbosity"]) is not str
            or output_format["type"] != "json_schema"
            or type(output_format["type"]) is not str
            or output_format["name"] != expected_schema_name
            or type(output_format["name"]) is not str
            or type(output_format["strict"]) is not bool
            or output_format["strict"] is not True
            or type(output_format["schema"]) is not dict
            or _canonical_bytes(output_format["schema"]) != _canonical_bytes(expected_schema)
        ):
            raise LiveAttemptError(
                "PROVIDER_REQUEST_STAGE_MISMATCH", "provider structured output differs"
            )
        _require_input_content(request, image_required=image_required)
    except LiveAttemptError:
        raise
    except Exception as exc:
        raise LiveAttemptError(
            "PROVIDER_REQUEST_STAGE_MISMATCH", "sealed provider request validation failed"
        ) from exc
    return request


CanonicalOpenAIRequestV1 = CanonicalHistoryPolicyRequestV1
build_canonical_openai_request = build_canonical_history_policy_request
snapshot_canonical_openai_request = snapshot_canonical_history_policy_request


@dataclass(frozen=True, slots=True)
class LiveAttemptPricingV1:
    """Explicit operator-pinned token price table used for bounded accounting."""

    pricing_id: str
    model: str
    input_usd_micros_per_million_tokens: int
    cached_input_usd_micros_per_million_tokens: int
    output_usd_micros_per_million_tokens: int
    source_sha256: str
    effective_at_utc: str
    rounding_policy: str = "CEIL_PER_ATTEMPT_USD_MICRO"
    schema_version: str = LIVE_ATTEMPT_PRICING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_PRICING_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "pricing schema differs")
        if self.rounding_policy != "CEIL_PER_ATTEMPT_USD_MICRO":
            raise LiveAttemptError("INVALID_PRICING", "pricing rounding policy differs")
        _require_id(self.pricing_id, "pricing_id")
        _require_id(self.model, "model")
        _require_sha256(self.source_sha256, "source_sha256")
        for value, label in (
            (self.input_usd_micros_per_million_tokens, "input token price"),
            (self.cached_input_usd_micros_per_million_tokens, "cached input token price"),
            (self.output_usd_micros_per_million_tokens, "output token price"),
        ):
            _require_int(value, label, 0, 1_000_000_000_000)
        if type(self.effective_at_utc) is not str:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is invalid")
        try:
            parsed = datetime.strptime(self.effective_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is invalid") from exc
        if parsed.year < 2020:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is implausible")


def live_attempt_pricing_projection(value: LiveAttemptPricingV1) -> dict[str, object]:
    if type(value) is not LiveAttemptPricingV1:
        raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
    trusted = LiveAttemptPricingV1(
        pricing_id=value.pricing_id,
        model=value.model,
        input_usd_micros_per_million_tokens=value.input_usd_micros_per_million_tokens,
        cached_input_usd_micros_per_million_tokens=(
            value.cached_input_usd_micros_per_million_tokens
        ),
        output_usd_micros_per_million_tokens=value.output_usd_micros_per_million_tokens,
        source_sha256=value.source_sha256,
        effective_at_utc=value.effective_at_utc,
        rounding_policy=value.rounding_policy,
        schema_version=value.schema_version,
    )
    return {
        "effective_at_utc": trusted.effective_at_utc,
        "input_usd_micros_per_million_tokens": (trusted.input_usd_micros_per_million_tokens),
        "cached_input_usd_micros_per_million_tokens": (
            trusted.cached_input_usd_micros_per_million_tokens
        ),
        "model": trusted.model,
        "output_usd_micros_per_million_tokens": (trusted.output_usd_micros_per_million_tokens),
        "pricing_id": trusted.pricing_id,
        "rounding_policy": trusted.rounding_policy,
        "schema_version": trusted.schema_version,
        "source_sha256": trusted.source_sha256,
    }


def live_attempt_pricing_sha256(value: LiveAttemptPricingV1) -> str:
    return _canonical_sha256(live_attempt_pricing_projection(value))


def snapshot_live_attempt_pricing(value: LiveAttemptPricingV1) -> LiveAttemptPricingV1:
    if type(value) is not LiveAttemptPricingV1:
        raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
    return LiveAttemptPricingV1(
        pricing_id=value.pricing_id,
        model=value.model,
        input_usd_micros_per_million_tokens=value.input_usd_micros_per_million_tokens,
        cached_input_usd_micros_per_million_tokens=(
            value.cached_input_usd_micros_per_million_tokens
        ),
        output_usd_micros_per_million_tokens=value.output_usd_micros_per_million_tokens,
        source_sha256=value.source_sha256,
        effective_at_utc=value.effective_at_utc,
        rounding_policy=value.rounding_policy,
        schema_version=value.schema_version,
    )


@dataclass(frozen=True, slots=True)
class LiveAttemptAuthorityV1:
    """Data binding for one call; this value alone grants no execution right."""

    attempt_id: str
    role: LiveAttemptRoleV1
    manifest_sha256: str
    preflight_sha256: str
    case_execution_lease_sha256: str
    stage_sha256: str
    case_id: str
    logical_call_id: str
    actor_request_sha256: str
    request_sha256: str
    transport_binding_sha256: str
    pricing_binding_sha256: str
    deadline_monotonic_ns: int
    max_cost_usd_micros: int
    max_output_tokens: int
    schema_version: str = LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "unknown attempt authority schema")
        _require_id(self.attempt_id, "attempt_id")
        if type(self.role) is not LiveAttemptRoleV1:
            raise LiveAttemptError("INVALID_ATTEMPT_AUTHORITY", "role is untrusted")
        for value, label in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.preflight_sha256, "preflight_sha256"),
            (self.case_execution_lease_sha256, "case_execution_lease_sha256"),
            (self.stage_sha256, "stage_sha256"),
            (self.actor_request_sha256, "actor_request_sha256"),
            (self.request_sha256, "request_sha256"),
            (self.transport_binding_sha256, "transport_binding_sha256"),
            (self.pricing_binding_sha256, "pricing_binding_sha256"),
        ):
            _require_sha256(value, label)
        _require_id(self.case_id, "case_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
            1,
            (1 << 63) - 1,
        )
        _require_int(self.max_cost_usd_micros, "max_cost_usd_micros", 0, 100_000_000_000)
        _require_int(self.max_output_tokens, "max_output_tokens", 1, 1_000_000)


def snapshot_live_attempt_authority(value: LiveAttemptAuthorityV1) -> LiveAttemptAuthorityV1:
    if type(value) is not LiveAttemptAuthorityV1:
        raise LiveAttemptError("UNTRUSTED_TYPE", "attempt authority has an untrusted type")
    return LiveAttemptAuthorityV1(
        attempt_id=value.attempt_id,
        role=value.role,
        manifest_sha256=value.manifest_sha256,
        preflight_sha256=value.preflight_sha256,
        case_execution_lease_sha256=value.case_execution_lease_sha256,
        stage_sha256=value.stage_sha256,
        case_id=value.case_id,
        logical_call_id=value.logical_call_id,
        actor_request_sha256=value.actor_request_sha256,
        request_sha256=value.request_sha256,
        transport_binding_sha256=value.transport_binding_sha256,
        pricing_binding_sha256=value.pricing_binding_sha256,
        deadline_monotonic_ns=value.deadline_monotonic_ns,
        max_cost_usd_micros=value.max_cost_usd_micros,
        max_output_tokens=value.max_output_tokens,
        schema_version=value.schema_version,
    )


def live_attempt_authority_projection(value: LiveAttemptAuthorityV1) -> dict[str, object]:
    trusted = snapshot_live_attempt_authority(value)
    return {
        "attempt_id": trusted.attempt_id,
        "actor_request_sha256": trusted.actor_request_sha256,
        "case_id": trusted.case_id,
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "deadline_monotonic_ns": trusted.deadline_monotonic_ns,
        "logical_call_id": trusted.logical_call_id,
        "manifest_sha256": trusted.manifest_sha256,
        "max_cost_usd_micros": trusted.max_cost_usd_micros,
        "max_output_tokens": trusted.max_output_tokens,
        "preflight_sha256": trusted.preflight_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "request_sha256": trusted.request_sha256,
        "role": trusted.role.value,
        "schema_version": trusted.schema_version,
        "stage_sha256": trusted.stage_sha256,
        "transport_binding_sha256": trusted.transport_binding_sha256,
    }


def live_attempt_authority_sha256(value: LiveAttemptAuthorityV1) -> str:
    return _canonical_sha256(live_attempt_authority_projection(value))


def parse_live_attempt_authority_projection(value: object) -> LiveAttemptAuthorityV1:
    """Rebuild an exact authority preimage from owner-only durable JSON."""

    expected_fields = {
        "actor_request_sha256",
        "attempt_id",
        "case_id",
        "case_execution_lease_sha256",
        "deadline_monotonic_ns",
        "logical_call_id",
        "manifest_sha256",
        "max_cost_usd_micros",
        "max_output_tokens",
        "preflight_sha256",
        "pricing_binding_sha256",
        "request_sha256",
        "role",
        "schema_version",
        "stage_sha256",
        "transport_binding_sha256",
    }
    if type(value) is not dict or set(value) != expected_fields:
        raise LiveAttemptError(
            "INVALID_ATTEMPT_AUTHORITY", "attempt authority projection fields differ"
        )
    projection = cast(dict[str, object], value)
    try:
        authority = LiveAttemptAuthorityV1(
            attempt_id=cast(str, projection["attempt_id"]),
            role=LiveAttemptRoleV1(cast(str, projection["role"])),
            manifest_sha256=cast(str, projection["manifest_sha256"]),
            preflight_sha256=cast(str, projection["preflight_sha256"]),
            case_execution_lease_sha256=cast(str, projection["case_execution_lease_sha256"]),
            stage_sha256=cast(str, projection["stage_sha256"]),
            case_id=cast(str, projection["case_id"]),
            logical_call_id=cast(str, projection["logical_call_id"]),
            actor_request_sha256=cast(str, projection["actor_request_sha256"]),
            request_sha256=cast(str, projection["request_sha256"]),
            transport_binding_sha256=cast(str, projection["transport_binding_sha256"]),
            pricing_binding_sha256=cast(str, projection["pricing_binding_sha256"]),
            deadline_monotonic_ns=cast(int, projection["deadline_monotonic_ns"]),
            max_cost_usd_micros=cast(int, projection["max_cost_usd_micros"]),
            max_output_tokens=cast(int, projection["max_output_tokens"]),
            schema_version=cast(str, projection["schema_version"]),
        )
    except (KeyError, TypeError, ValueError, LiveAttemptError) as exc:
        raise LiveAttemptError(
            "INVALID_ATTEMPT_AUTHORITY", "attempt authority projection is invalid"
        ) from exc
    if _canonical_bytes(projection) != _canonical_bytes(
        live_attempt_authority_projection(authority)
    ):
        raise LiveAttemptError(
            "INVALID_ATTEMPT_AUTHORITY", "attempt authority projection is non-canonical"
        )
    return authority


@dataclass(frozen=True, slots=True)
class LiveAttemptDeadlineBindingV1:
    """Module-issued source proof for a clamped production attempt deadline.

    The R2.2 transport supplies its own requested call deadline.  Production
    orchestration separately registers the enclosing case/owner ceiling before
    that transport can prepare a child.  The runner consumes that registration
    exactly once and records the deterministic minimum used by the authority.
    """

    attempt_id: str
    logical_call_id: str
    case_id: str
    case_execution_lease_sha256: str
    constraint_registered_monotonic_ns: int
    requested_deadline_issued_monotonic_ns: int
    requested_call_deadline_monotonic_ns: int
    request_timeout_ns: int
    begin_observed_monotonic_ns: int
    case_execution_deadline_monotonic_ns: int
    effective_deadline_monotonic_ns: int
    max_cost_usd_micros: int
    schema_version: str = LIVE_ATTEMPT_DEADLINE_BINDING_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _DEADLINE_BINDING_SEAL:
            raise LiveAttemptError(
                "UNTRUSTED_DEADLINE_BINDING",
                "attempt deadline binding was not issued by the production runner",
            )
        if self.schema_version != LIVE_ATTEMPT_DEADLINE_BINDING_SCHEMA_VERSION:
            raise LiveAttemptError(
                "UNKNOWN_SCHEMA_VERSION", "attempt deadline binding schema differs"
            )
        _require_id(self.attempt_id, "attempt_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.case_id, "case_id")
        _require_sha256(self.case_execution_lease_sha256, "case_execution_lease_sha256")
        for name in (
            "requested_deadline_issued_monotonic_ns",
            "requested_call_deadline_monotonic_ns",
            "request_timeout_ns",
            "constraint_registered_monotonic_ns",
            "begin_observed_monotonic_ns",
            "case_execution_deadline_monotonic_ns",
            "effective_deadline_monotonic_ns",
        ):
            _require_int(getattr(self, name), name, 1, (1 << 63) - 1)
        _require_int(
            self.max_cost_usd_micros,
            "max_cost_usd_micros",
            0,
            100_000_000_000,
        )
        requested = self.requested_deadline_issued_monotonic_ns + self.request_timeout_ns
        effective = min(
            requested,
            self.case_execution_deadline_monotonic_ns,
        )
        if (
            requested > (1 << 63) - 1
            or self.requested_call_deadline_monotonic_ns != requested
            or self.effective_deadline_monotonic_ns != effective
            or self.constraint_registered_monotonic_ns > self.requested_deadline_issued_monotonic_ns
            or self.requested_deadline_issued_monotonic_ns > self.begin_observed_monotonic_ns
        ):
            raise LiveAttemptError(
                "INVALID_DEADLINE_BINDING",
                "attempt deadline is not deterministically derived",
            )


def _build_live_attempt_deadline_binding(
    *,
    attempt_id: str,
    logical_call_id: str,
    case_id: str,
    case_execution_lease_sha256: str,
    constraint_registered_monotonic_ns: int,
    requested_call_deadline_monotonic_ns: int,
    request_timeout_ns: int,
    begin_observed_monotonic_ns: int,
    case_execution_deadline_monotonic_ns: int,
    max_cost_usd_micros: int,
) -> LiveAttemptDeadlineBindingV1:
    if type(requested_call_deadline_monotonic_ns) is not int or type(request_timeout_ns) is not int:
        raise LiveAttemptError("INVALID_DEADLINE_BINDING", "requested deadline source is invalid")
    issued_ns = requested_call_deadline_monotonic_ns - request_timeout_ns
    return LiveAttemptDeadlineBindingV1(
        attempt_id=attempt_id,
        logical_call_id=logical_call_id,
        case_id=case_id,
        case_execution_lease_sha256=case_execution_lease_sha256,
        constraint_registered_monotonic_ns=constraint_registered_monotonic_ns,
        requested_deadline_issued_monotonic_ns=issued_ns,
        requested_call_deadline_monotonic_ns=requested_call_deadline_monotonic_ns,
        request_timeout_ns=request_timeout_ns,
        begin_observed_monotonic_ns=begin_observed_monotonic_ns,
        case_execution_deadline_monotonic_ns=case_execution_deadline_monotonic_ns,
        effective_deadline_monotonic_ns=min(
            requested_call_deadline_monotonic_ns,
            case_execution_deadline_monotonic_ns,
        ),
        max_cost_usd_micros=max_cost_usd_micros,
        _seal=_DEADLINE_BINDING_SEAL,
    )


def snapshot_live_attempt_deadline_binding(
    value: LiveAttemptDeadlineBindingV1,
) -> LiveAttemptDeadlineBindingV1:
    if type(value) is not LiveAttemptDeadlineBindingV1:
        raise LiveAttemptError(
            "UNTRUSTED_DEADLINE_BINDING", "attempt deadline binding type differs"
        )
    return LiveAttemptDeadlineBindingV1(
        attempt_id=value.attempt_id,
        logical_call_id=value.logical_call_id,
        case_id=value.case_id,
        case_execution_lease_sha256=value.case_execution_lease_sha256,
        constraint_registered_monotonic_ns=(value.constraint_registered_monotonic_ns),
        requested_deadline_issued_monotonic_ns=(value.requested_deadline_issued_monotonic_ns),
        requested_call_deadline_monotonic_ns=(value.requested_call_deadline_monotonic_ns),
        request_timeout_ns=value.request_timeout_ns,
        begin_observed_monotonic_ns=value.begin_observed_monotonic_ns,
        case_execution_deadline_monotonic_ns=(value.case_execution_deadline_monotonic_ns),
        effective_deadline_monotonic_ns=value.effective_deadline_monotonic_ns,
        max_cost_usd_micros=value.max_cost_usd_micros,
        schema_version=value.schema_version,
        _seal=_DEADLINE_BINDING_SEAL,
    )


def live_attempt_deadline_binding_projection(
    value: LiveAttemptDeadlineBindingV1,
) -> dict[str, object]:
    trusted = snapshot_live_attempt_deadline_binding(value)
    return {
        "attempt_id": trusted.attempt_id,
        "case_execution_deadline_monotonic_ns": (trusted.case_execution_deadline_monotonic_ns),
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "case_id": trusted.case_id,
        "constraint_registered_monotonic_ns": (trusted.constraint_registered_monotonic_ns),
        "begin_observed_monotonic_ns": trusted.begin_observed_monotonic_ns,
        "effective_deadline_monotonic_ns": trusted.effective_deadline_monotonic_ns,
        "logical_call_id": trusted.logical_call_id,
        "max_cost_usd_micros": trusted.max_cost_usd_micros,
        "request_timeout_ns": trusted.request_timeout_ns,
        "requested_call_deadline_monotonic_ns": (trusted.requested_call_deadline_monotonic_ns),
        "requested_deadline_issued_monotonic_ns": (trusted.requested_deadline_issued_monotonic_ns),
        "schema_version": trusted.schema_version,
    }


def parse_live_attempt_deadline_binding_projection(
    value: object,
) -> LiveAttemptDeadlineBindingV1:
    expected_fields = {
        "attempt_id",
        "case_execution_deadline_monotonic_ns",
        "case_execution_lease_sha256",
        "case_id",
        "constraint_registered_monotonic_ns",
        "begin_observed_monotonic_ns",
        "effective_deadline_monotonic_ns",
        "logical_call_id",
        "max_cost_usd_micros",
        "request_timeout_ns",
        "requested_call_deadline_monotonic_ns",
        "requested_deadline_issued_monotonic_ns",
        "schema_version",
    }
    if type(value) is not dict or set(value) != expected_fields:
        raise LiveAttemptError("INVALID_DEADLINE_BINDING", "attempt deadline binding fields differ")
    projection = cast(dict[str, object], value)
    try:
        trusted = LiveAttemptDeadlineBindingV1(
            attempt_id=cast(str, projection["attempt_id"]),
            logical_call_id=cast(str, projection["logical_call_id"]),
            case_id=cast(str, projection["case_id"]),
            case_execution_lease_sha256=cast(str, projection["case_execution_lease_sha256"]),
            constraint_registered_monotonic_ns=cast(
                int, projection["constraint_registered_monotonic_ns"]
            ),
            requested_deadline_issued_monotonic_ns=cast(
                int, projection["requested_deadline_issued_monotonic_ns"]
            ),
            requested_call_deadline_monotonic_ns=cast(
                int, projection["requested_call_deadline_monotonic_ns"]
            ),
            request_timeout_ns=cast(int, projection["request_timeout_ns"]),
            begin_observed_monotonic_ns=cast(int, projection["begin_observed_monotonic_ns"]),
            case_execution_deadline_monotonic_ns=cast(
                int, projection["case_execution_deadline_monotonic_ns"]
            ),
            effective_deadline_monotonic_ns=cast(
                int, projection["effective_deadline_monotonic_ns"]
            ),
            max_cost_usd_micros=cast(int, projection["max_cost_usd_micros"]),
            schema_version=cast(str, projection["schema_version"]),
            _seal=_DEADLINE_BINDING_SEAL,
        )
    except (KeyError, TypeError, ValueError, LiveAttemptError) as exc:
        raise LiveAttemptError(
            "INVALID_DEADLINE_BINDING", "attempt deadline binding is invalid"
        ) from exc
    if _canonical_bytes(projection) != _canonical_bytes(
        live_attempt_deadline_binding_projection(trusted)
    ):
        raise LiveAttemptError(
            "INVALID_DEADLINE_BINDING", "attempt deadline binding is non-canonical"
        )
    return trusted


@dataclass(frozen=True, slots=True)
class LiveAttemptReceiptV1:
    """Terminal, content-free proof for one bounded provider attempt."""

    attempt_id: str
    role: LiveAttemptRoleV1
    authority_sha256: str
    manifest_sha256: str
    preflight_sha256: str
    case_execution_lease_sha256: str
    stage_sha256: str
    case_id: str
    logical_call_id: str
    actor_request_sha256: str
    request_sha256: str
    transport_binding_sha256: str
    pricing_binding_sha256: str
    execution_kind: LiveAttemptExecutionKindV1
    status: LiveAttemptStatusV1
    dispatch_count: int
    response_envelope_sha256: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_status: LiveAttemptCostStatusV1
    cost_usd_micros: int | None
    cancellation_requested: bool
    termination: LiveAttemptTerminationV1
    worker_pid: int | None
    worker_exit_code: int | None
    worker_reaped: bool
    late_output_detected: bool
    duration_ns: int
    failure_code: str | None
    requested_model: str | None = None
    returned_model: str | None = None
    schema_version: str = LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "unknown attempt receipt schema")
        _require_id(self.attempt_id, "attempt_id")
        if type(self.role) is not LiveAttemptRoleV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "receipt role is untrusted")
        for value, label in (
            (self.authority_sha256, "authority_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
            (self.preflight_sha256, "preflight_sha256"),
            (self.case_execution_lease_sha256, "case_execution_lease_sha256"),
            (self.stage_sha256, "stage_sha256"),
            (self.actor_request_sha256, "actor_request_sha256"),
            (self.request_sha256, "request_sha256"),
            (self.transport_binding_sha256, "transport_binding_sha256"),
            (self.pricing_binding_sha256, "pricing_binding_sha256"),
        ):
            _require_sha256(value, label)
        _require_id(self.case_id, "case_id")
        _require_id(self.logical_call_id, "logical_call_id")
        if type(self.execution_kind) is not LiveAttemptExecutionKindV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "execution kind is untrusted")
        if type(self.status) is not LiveAttemptStatusV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "attempt status is untrusted")
        _require_int(self.dispatch_count, "dispatch_count", 0, 1)
        if self.response_envelope_sha256 is not None:
            _require_sha256(self.response_envelope_sha256, "response_envelope_sha256")
        for token_value, label in (
            (self.input_tokens, "input_tokens"),
            (self.cached_input_tokens, "cached_input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if token_value is not None:
                _require_int(token_value, label, 0, 100_000_000)
        if type(self.cost_status) is not LiveAttemptCostStatusV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "cost status is untrusted")
        if self.cost_status is LiveAttemptCostStatusV1.EXACT:
            if self.cost_usd_micros is None:
                raise LiveAttemptError("INCOMPLETE_ACCOUNTING", "exact cost is absent")
            _require_int(self.cost_usd_micros, "cost_usd_micros", 0, 100_000_000_000)
        elif self.cost_usd_micros is not None:
            raise LiveAttemptError("FALSE_ACCOUNTING_CLAIM", "unknown cost cannot carry a value")
        for bool_value, label in (
            (self.cancellation_requested, "cancellation_requested"),
            (self.worker_reaped, "worker_reaped"),
            (self.late_output_detected, "late_output_detected"),
        ):
            if type(bool_value) is not bool:
                raise LiveAttemptError("UNTRUSTED_TYPE", f"{label} is not an exact bool")
        if type(self.termination) is not LiveAttemptTerminationV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "termination kind is untrusted")
        if self.worker_pid is not None:
            _require_int(self.worker_pid, "worker_pid", 1, (1 << 31) - 1)
        if self.worker_exit_code is not None and type(self.worker_exit_code) is not int:
            raise LiveAttemptError("INVALID_INTEGER", "worker_exit_code is invalid")
        _require_int(self.duration_ns, "duration_ns", 0, _MAX_DURATION_NS)
        if self.failure_code is not None and (
            type(self.failure_code) is not str or _FAILURE_CODE.fullmatch(self.failure_code) is None
        ):
            raise LiveAttemptError("INVALID_FAILURE_CODE", "failure_code is invalid")
        for model, label in (
            (self.requested_model, "requested_model"),
            (self.returned_model, "returned_model"),
        ):
            if model is not None and (
                type(model) is not str or _SAFE_MODEL_ID.fullmatch(model) is None
            ):
                raise LiveAttemptError(
                    "INVALID_MODEL_PROVENANCE", f"{label} is not a bounded safe model ID"
                )
        self._validate_state()

    def _validate_state(self) -> None:
        tokens = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.total_tokens,
        )
        complete_response_accounting = (
            self.response_envelope_sha256 is not None
            and all(value is not None for value in tokens)
            and self.cost_status is LiveAttemptCostStatusV1.EXACT
            and self.cost_usd_micros is not None
        )
        observed_response = self.response_envelope_sha256 is not None and (
            all(value is None for value in tokens) or all(value is not None for value in tokens)
        )
        late_response_accounting = complete_response_accounting or (
            observed_response
            and all(value is None for value in tokens)
            and self.cost_status is LiveAttemptCostStatusV1.UNKNOWN
            and self.cost_usd_micros is None
        )
        model_values = (self.requested_model, self.returned_model)
        if self.execution_kind is LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS:
            if any(value is not None for value in model_values):
                raise LiveAttemptError(
                    "INVALID_MODEL_PROVENANCE", "CPU attempts cannot claim provider model IDs"
                )
        elif self.status is LiveAttemptStatusV1.COMPLETED:
            if self.requested_model != "gpt-5.6-sol" or self.returned_model != self.requested_model:
                raise LiveAttemptError(
                    "INVALID_COMPLETED_RECEIPT",
                    "completed production attempt model provenance differs",
                )
        elif (
            self.status is LiveAttemptStatusV1.FAILED
            and self.failure_code == "PROVIDER_RETURNED_MODEL_MISMATCH"
        ):
            if (
                self.requested_model != "gpt-5.6-sol"
                or self.returned_model is None
                or self.returned_model == self.requested_model
                or self.dispatch_count != 1
                or self.cost_status is not LiveAttemptCostStatusV1.UNKNOWN
                or self.cost_usd_micros is not None
                or self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.worker_exit_code != 0
                or self.late_output_detected
                or self.response_envelope_sha256 is not None
                or any(value is not None for value in tokens)
            ):
                raise LiveAttemptError(
                    "INVALID_MODEL_PROVENANCE", "provider model mismatch proof is incomplete"
                )
        elif (
            self.status is LiveAttemptStatusV1.FAILED
            and self.failure_code == "PROVIDER_RETURNED_MODEL_INVALID"
        ):
            if (
                self.requested_model != "gpt-5.6-sol"
                or self.returned_model is not None
                or self.dispatch_count != 1
                or self.cost_status is not LiveAttemptCostStatusV1.UNKNOWN
                or self.cost_usd_micros is not None
                or self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.worker_exit_code != 0
                or self.late_output_detected
                or self.response_envelope_sha256 is not None
                or any(value is not None for value in tokens)
            ):
                raise LiveAttemptError(
                    "INVALID_MODEL_PROVENANCE", "invalid provider model proof is inconsistent"
                )
        elif self.response_envelope_sha256 is not None:
            if (
                self.requested_model != "gpt-5.6-sol"
                or self.returned_model != self.requested_model
                or self.dispatch_count != 1
            ):
                raise LiveAttemptError(
                    "INVALID_MODEL_PROVENANCE",
                    "observed provider response model proof is incomplete",
                )
        elif any(value is not None for value in model_values):
            raise LiveAttemptError(
                "INVALID_MODEL_PROVENANCE", "attempt state cannot carry provider model IDs"
            )
        if self.worker_reaped != (self.worker_exit_code is not None):
            raise LiveAttemptError("INVALID_TERMINATION_PROOF", "reap and exit code disagree")
        if (self.termination is LiveAttemptTerminationV1.UNCONFIRMED) != (
            self.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
        ):
            raise LiveAttemptError(
                "INVALID_TERMINATION_PROOF", "unconfirmed termination status differs"
            )
        if (
            self.status is LiveAttemptStatusV1.FAILED
            and self.worker_pid is not None
            and not self.worker_reaped
        ):
            raise LiveAttemptError(
                "INVALID_TERMINATION_PROOF", "failed worker lacks confirmed termination"
            )
        if self.total_tokens is not None and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise LiveAttemptError("INVALID_TOKEN_CENSUS", "token counts are inconsistent")
        if self.cached_input_tokens is not None and (
            self.input_tokens is None or self.cached_input_tokens > self.input_tokens
        ):
            raise LiveAttemptError("INVALID_TOKEN_CENSUS", "cached input exceeds total input")
        if self.status is LiveAttemptStatusV1.COMPLETED:
            if (
                self.dispatch_count != 1
                or self.response_envelope_sha256 is None
                or any(value is None for value in tokens)
                or self.cost_status is not LiveAttemptCostStatusV1.EXACT
                or self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.worker_exit_code != 0
                or self.late_output_detected
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_COMPLETED_RECEIPT", "completed receipt is incomplete"
                )
            return
        response_allowed = (
            (
                self.status is LiveAttemptStatusV1.FAILED
                and self.failure_code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY"
                and complete_response_accounting
                and self.dispatch_count == 1
                and not self.cancellation_requested
                and self.termination is LiveAttemptTerminationV1.NONE
                and self.worker_reaped
            )
            or (
                self.status is LiveAttemptStatusV1.FAILED
                and self.failure_code == "PROVIDER_USAGE_MISSING"
                and observed_response
                and all(value is None for value in tokens)
                and self.dispatch_count == 1
                and self.cost_status is LiveAttemptCostStatusV1.UNKNOWN
                and not self.cancellation_requested
                and self.termination is LiveAttemptTerminationV1.NONE
                and self.worker_reaped
            )
            or (
                self.status is LiveAttemptStatusV1.FAILED
                and self.failure_code == "PROVIDER_CHILD_DID_NOT_EXIT"
                and observed_response
                and self.dispatch_count == 1
                and self.cost_status is LiveAttemptCostStatusV1.UNKNOWN
                and not self.cancellation_requested
                and self.termination
                in {LiveAttemptTerminationV1.TERM, LiveAttemptTerminationV1.KILL}
                and self.worker_reaped
            )
            or (
                self.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                and self.failure_code == "TERMINATION_UNCONFIRMED"
                and observed_response
                and self.dispatch_count == 1
                and self.cancellation_requested
                and self.termination is LiveAttemptTerminationV1.UNCONFIRMED
                and not self.worker_reaped
                and (
                    self.cost_status is LiveAttemptCostStatusV1.UNKNOWN
                    or complete_response_accounting
                )
            )
            or (
                self.status is LiveAttemptStatusV1.FAILED
                and self.late_output_detected
                and late_response_accounting
                and self.dispatch_count == 1
                and self.worker_reaped
                and not self.cancellation_requested
                and self.termination
                in {
                    LiveAttemptTerminationV1.COOPERATIVE,
                    LiveAttemptTerminationV1.TERM,
                    LiveAttemptTerminationV1.KILL,
                }
            )
            or (
                self.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
                and self.late_output_detected
                and late_response_accounting
                and self.dispatch_count == 1
                and self.cancellation_requested
                and self.worker_reaped
                and self.termination
                in {
                    LiveAttemptTerminationV1.COOPERATIVE,
                    LiveAttemptTerminationV1.TERM,
                    LiveAttemptTerminationV1.KILL,
                }
            )
        )
        if (
            self.response_envelope_sha256 is not None or any(value is not None for value in tokens)
        ) and not response_allowed:
            raise LiveAttemptError(
                "INVALID_NONCOMPLETED_RECEIPT", "failed attempt claims a response"
            )
        if self.status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH:
            if (
                self.dispatch_count != 0
                or self.cost_status is not LiveAttemptCostStatusV1.EXACT
                or self.cost_usd_micros != 0
                or not self.cancellation_requested
                or self.termination is LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_CANCELLED_RECEIPT", "pre-dispatch cancellation is inconsistent"
                )
            return
        if self.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH:
            exact_late_response = self.late_output_detected and complete_response_accounting
            unknown_cost = (
                self.cost_status is LiveAttemptCostStatusV1.UNKNOWN and self.cost_usd_micros is None
            )
            stopped_after_dispatch = self.termination in {
                LiveAttemptTerminationV1.COOPERATIVE,
                LiveAttemptTerminationV1.TERM,
                LiveAttemptTerminationV1.KILL,
            }
            if (
                self.dispatch_count != 1
                or not (unknown_cost or exact_late_response)
                or not self.cancellation_requested
                or not stopped_after_dispatch
                or not self.worker_reaped
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_CANCELLED_RECEIPT", "post-dispatch cancellation is inconsistent"
                )
            return
        if self.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED:
            exact_late_response = self.late_output_detected and complete_response_accounting
            unknown_cost = (
                self.cost_status is LiveAttemptCostStatusV1.UNKNOWN and self.cost_usd_micros is None
            )
            if (
                not (unknown_cost or exact_late_response)
                or not self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.UNCONFIRMED
                or self.worker_reaped
                or self.worker_exit_code is not None
                or self.failure_code != "TERMINATION_UNCONFIRMED"
            ):
                raise LiveAttemptError(
                    "INVALID_TERMINATION_PROOF", "unconfirmed termination is inconsistent"
                )
            return
        if self.failure_code is None:
            raise LiveAttemptError("INVALID_FAILED_RECEIPT", "failed attempt needs a typed code")
        exact_failed_response = (
            self.failure_code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY"
            and complete_response_accounting
        ) or (self.late_output_detected and complete_response_accounting)
        expected_cost = (
            LiveAttemptCostStatusV1.EXACT
            if self.dispatch_count == 0 or exact_failed_response
            else LiveAttemptCostStatusV1.UNKNOWN
        )
        if (
            self.cost_status is not expected_cost
            or (self.dispatch_count == 0 and self.cost_usd_micros != 0)
            or (exact_failed_response and self.dispatch_count != 1)
        ):
            raise LiveAttemptError("FALSE_ACCOUNTING_CLAIM", "failed-attempt cost is inconsistent")

    @property
    def accounting_complete(self) -> bool:
        return self.cost_status is LiveAttemptCostStatusV1.EXACT

    @property
    def passed(self) -> bool:
        return self.status is LiveAttemptStatusV1.COMPLETED and self.accounting_complete


def snapshot_live_attempt_receipt(value: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
    if type(value) is not LiveAttemptReceiptV1:
        raise LiveAttemptError("UNTRUSTED_TYPE", "attempt receipt has an untrusted type")
    return LiveAttemptReceiptV1(
        attempt_id=value.attempt_id,
        role=value.role,
        authority_sha256=value.authority_sha256,
        manifest_sha256=value.manifest_sha256,
        preflight_sha256=value.preflight_sha256,
        case_execution_lease_sha256=value.case_execution_lease_sha256,
        stage_sha256=value.stage_sha256,
        case_id=value.case_id,
        logical_call_id=value.logical_call_id,
        actor_request_sha256=value.actor_request_sha256,
        request_sha256=value.request_sha256,
        transport_binding_sha256=value.transport_binding_sha256,
        pricing_binding_sha256=value.pricing_binding_sha256,
        execution_kind=value.execution_kind,
        status=value.status,
        dispatch_count=value.dispatch_count,
        response_envelope_sha256=value.response_envelope_sha256,
        input_tokens=value.input_tokens,
        cached_input_tokens=value.cached_input_tokens,
        output_tokens=value.output_tokens,
        total_tokens=value.total_tokens,
        cost_status=value.cost_status,
        cost_usd_micros=value.cost_usd_micros,
        cancellation_requested=value.cancellation_requested,
        termination=value.termination,
        worker_pid=value.worker_pid,
        worker_exit_code=value.worker_exit_code,
        worker_reaped=value.worker_reaped,
        late_output_detected=value.late_output_detected,
        duration_ns=value.duration_ns,
        failure_code=value.failure_code,
        requested_model=value.requested_model,
        returned_model=value.returned_model,
        schema_version=value.schema_version,
    )


def live_attempt_receipt_projection(value: LiveAttemptReceiptV1) -> dict[str, object]:
    trusted = snapshot_live_attempt_receipt(value)
    return {
        "actor_request_sha256": trusted.actor_request_sha256,
        "attempt_id": trusted.attempt_id,
        "authority_sha256": trusted.authority_sha256,
        "cancellation_requested": trusted.cancellation_requested,
        "case_id": trusted.case_id,
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "cost_status": trusted.cost_status.value,
        "cost_usd_micros": trusted.cost_usd_micros,
        "dispatch_count": trusted.dispatch_count,
        "duration_ns": trusted.duration_ns,
        "execution_kind": trusted.execution_kind.value,
        "failure_code": trusted.failure_code,
        "input_tokens": trusted.input_tokens,
        "cached_input_tokens": trusted.cached_input_tokens,
        "late_output_detected": trusted.late_output_detected,
        "logical_call_id": trusted.logical_call_id,
        "manifest_sha256": trusted.manifest_sha256,
        "output_tokens": trusted.output_tokens,
        "preflight_sha256": trusted.preflight_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "request_sha256": trusted.request_sha256,
        "requested_model": trusted.requested_model,
        "response_envelope_sha256": trusted.response_envelope_sha256,
        "returned_model": trusted.returned_model,
        "role": trusted.role.value,
        "schema_version": trusted.schema_version,
        "stage_sha256": trusted.stage_sha256,
        "status": trusted.status.value,
        "termination": trusted.termination.value,
        "total_tokens": trusted.total_tokens,
        "transport_binding_sha256": trusted.transport_binding_sha256,
        "worker_exit_code": trusted.worker_exit_code,
        "worker_pid": trusted.worker_pid,
        "worker_reaped": trusted.worker_reaped,
    }


def live_attempt_receipt_sha256(value: LiveAttemptReceiptV1) -> str:
    return _canonical_sha256(live_attempt_receipt_projection(value))


def live_attempt_receipt_root_sha256(values: tuple[LiveAttemptReceiptV1, ...]) -> str:
    """Bind an ordered, duplicate-free terminal-attempt census."""

    if type(values) is not tuple or any(
        type(value) is not LiveAttemptReceiptV1 for value in values
    ):
        raise LiveAttemptError("UNTRUSTED_TYPE", "receipt root requires exact receipt values")
    trusted = tuple(snapshot_live_attempt_receipt(value) for value in values)
    attempt_ids = tuple(value.attempt_id for value in trusted)
    if len(set(attempt_ids)) != len(attempt_ids):
        raise LiveAttemptError("DUPLICATE_ATTEMPT_ID", "receipt root repeats an attempt")
    return _canonical_sha256(
        {
            "receipt_sha256s": [live_attempt_receipt_sha256(value) for value in trusted],
            "schema_version": LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION,
        }
    )


class MemoryLiveAttemptReceiptSinkV1:
    """Append-only, in-memory CPU sink with start-before-dispatch admission."""

    def __init__(self) -> None:
        self._started: dict[str, LiveAttemptAuthorityV1] = {}
        self._terminal: dict[str, LiveAttemptReceiptV1] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def _reserve(self, authority: LiveAttemptAuthorityV1) -> None:
        trusted = snapshot_live_attempt_authority(authority)
        with self._lock:
            if trusted.attempt_id in self._started:
                raise LiveAttemptError("DUPLICATE_ATTEMPT_ID", "attempt ID was already admitted")
            self._started[trusted.attempt_id] = trusted
            self._order.append(trusted.attempt_id)

    def _commit(self, receipt: LiveAttemptReceiptV1) -> None:
        trusted = snapshot_live_attempt_receipt(receipt)
        with self._lock:
            authority = self._started.get(trusted.attempt_id)
            if authority is None:
                raise LiveAttemptError("ATTEMPT_NOT_ADMITTED", "attempt has no start admission")
            expected_fields = (
                authority.role,
                authority.manifest_sha256,
                authority.preflight_sha256,
                authority.case_execution_lease_sha256,
                authority.stage_sha256,
                authority.case_id,
                authority.logical_call_id,
                authority.actor_request_sha256,
                authority.request_sha256,
                authority.transport_binding_sha256,
                authority.pricing_binding_sha256,
            )
            receipt_fields = (
                trusted.role,
                trusted.manifest_sha256,
                trusted.preflight_sha256,
                trusted.case_execution_lease_sha256,
                trusted.stage_sha256,
                trusted.case_id,
                trusted.logical_call_id,
                trusted.actor_request_sha256,
                trusted.request_sha256,
                trusted.transport_binding_sha256,
                trusted.pricing_binding_sha256,
            )
            if (
                live_attempt_authority_sha256(authority) != trusted.authority_sha256
                or expected_fields != receipt_fields
            ):
                raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "terminal receipt changed authority")
            if trusted.attempt_id in self._terminal:
                raise LiveAttemptError("DUPLICATE_TERMINAL_RECEIPT", "attempt is already terminal")
            self._terminal[trusted.attempt_id] = trusted

    @property
    def started_count(self) -> int:
        with self._lock:
            return len(self._started)

    @property
    def terminal_count(self) -> int:
        with self._lock:
            return len(self._terminal)

    @property
    def receipts(self) -> tuple[LiveAttemptReceiptV1, ...]:
        with self._lock:
            return tuple(
                snapshot_live_attempt_receipt(self._terminal[attempt_id])
                for attempt_id in self._order
                if attempt_id in self._terminal
            )

    @property
    def receipt_root_sha256(self) -> str:
        return live_attempt_receipt_root_sha256(self.receipts)

    def receipt_for(self, attempt_id: str) -> LiveAttemptReceiptV1 | None:
        _require_id(attempt_id, "attempt_id")
        with self._lock:
            value = self._terminal.get(attempt_id)
            return None if value is None else snapshot_live_attempt_receipt(value)

    def authority_for(self, attempt_id: str) -> LiveAttemptAuthorityV1 | None:
        """Return a detached admitted authority without exposing mutable sink state."""

        _require_id(attempt_id, "attempt_id")
        with self._lock:
            value = self._started.get(attempt_id)
            return None if value is None else snapshot_live_attempt_authority(value)


def _cpu_response_sha256(authority_sha256: str, request_sha256: str) -> str:
    return _canonical_sha256(
        {
            "authority_sha256": authority_sha256,
            "fixed_cpu_output": "ok",
            "request_sha256": request_sha256,
        }
    )


def _cpu_fixed_attempt_worker(
    connection: Connection,
    script_value: str,
    authority_sha256: str,
    request_sha256: str,
) -> None:
    """Run one closed CPU behavior; arguments cannot carry code, argv, or secrets."""

    try:
        script = CpuFixedAttemptScriptV1(script_value)
        if script is CpuFixedAttemptScriptV1.IGNORE_TERM_AFTER_DISPATCH:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        connection.send(("READY", authority_sha256))
        command = connection.recv()
        if command != ("DISPATCH", authority_sha256):
            return
        connection.send(("DISPATCHED", authority_sha256))
        if script is CpuFixedAttemptScriptV1.COMPLETE_ONCE:
            connection.send(
                (
                    "COMPLETED",
                    authority_sha256,
                    _cpu_response_sha256(authority_sha256, request_sha256),
                    _CPU_INPUT_TOKENS,
                    _CPU_OUTPUT_TOKENS,
                    _CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                    _CPU_COST_USD_MICROS,
                )
            )
            return
        while True:
            signal.pause()
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class _StopResult:
    termination: LiveAttemptTerminationV1
    worker_reaped: bool
    worker_exit_code: int | None


@dataclass(frozen=True, slots=True)
class _ObservedCompletedResponseV1:
    """Validated, detached provider evidence observed before terminalization."""

    response_envelope_sha256: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_status: LiveAttemptCostStatusV1
    cost_usd_micros: int | None
    requested_model: str
    returned_model: str


class CpuFixedLiveAttemptHandleV1:
    """One CPU attempt handle with a concurrent-safe cancellation entry point."""

    def __init__(
        self,
        *,
        authority: LiveAttemptAuthorityV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        process: BaseProcess | None,
        connection: Connection | None,
        started_ns: int,
        cancel_grace_seconds: float,
        terminal_receipt: LiveAttemptReceiptV1 | None = None,
    ) -> None:
        self._authority = snapshot_live_attempt_authority(authority)
        self._authority_sha256 = live_attempt_authority_sha256(self._authority)
        self._sink = sink
        self._process = process
        self._connection = connection
        self._started_ns = started_ns
        self._cancel_grace_seconds = cancel_grace_seconds
        self._terminal_receipt = terminal_receipt
        self._dispatch_count = 0
        self._dispatch_command_sent = False
        self._execute_started = False
        self._cancel_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._finalize_lock = threading.Lock()

    @property
    def dispatch_count(self) -> int:
        with self._state_lock:
            return self._dispatch_count

    @property
    def worker_alive(self) -> bool:
        with self._state_lock:
            process = self._process
            terminal = self._terminal_receipt
        return terminal is None and process is not None and process.is_alive()

    @property
    def terminal_receipt(self) -> LiveAttemptReceiptV1 | None:
        with self._state_lock:
            value = self._terminal_receipt
        return None if value is None else snapshot_live_attempt_receipt(value)

    def _receive(self, timeout_seconds: float) -> tuple[object, ...] | None:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return None
            try:
                if not connection.poll(max(0.0, timeout_seconds)):
                    return None
                value = connection.recv()
            except (EOFError, OSError):
                return None
        return value if type(value) is tuple else ("INVALID",)

    def _send(self, value: tuple[str, str]) -> bool:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.send(value)
            except (BrokenPipeError, EOFError, OSError):
                return False
        return True

    def _drain(self) -> tuple[tuple[object, ...], ...]:
        messages: list[tuple[object, ...]] = []
        while True:
            value = self._receive(0.0)
            if value is None:
                return tuple(messages)
            messages.append(value)

    def _observe_dispatch(self, messages: tuple[tuple[object, ...], ...]) -> None:
        expected = self._authority_sha256
        if any(message == ("DISPATCHED", expected) for message in messages):
            with self._state_lock:
                self._dispatch_count = 1

    def _stop_worker(self, *, cooperative: bool) -> _StopResult:
        process = self._process
        if process is None:
            return _StopResult(LiveAttemptTerminationV1.COOPERATIVE, True, 0)
        if not process.is_alive():
            process.join(0)
            return _StopResult(
                LiveAttemptTerminationV1.COOPERATIVE,
                True,
                process.exitcode,
            )
        if cooperative:
            process.join(self._cancel_grace_seconds)
            if not process.is_alive():
                return _StopResult(
                    LiveAttemptTerminationV1.COOPERATIVE,
                    True,
                    process.exitcode,
                )
        process.terminate()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.TERM, True, process.exitcode)
        process.kill()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.KILL, True, process.exitcode)
        return _StopResult(LiveAttemptTerminationV1.UNCONFIRMED, False, None)

    def _make_receipt(
        self,
        *,
        status: LiveAttemptStatusV1,
        dispatch_count: int,
        response_envelope_sha256: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost_status: LiveAttemptCostStatusV1,
        cost_usd_micros: int | None,
        cancellation_requested: bool,
        stop: _StopResult,
        late_output_detected: bool = False,
        failure_code: str | None = None,
    ) -> LiveAttemptReceiptV1:
        authority = self._authority
        process = self._process
        return LiveAttemptReceiptV1(
            attempt_id=authority.attempt_id,
            role=authority.role,
            authority_sha256=self._authority_sha256,
            manifest_sha256=authority.manifest_sha256,
            preflight_sha256=authority.preflight_sha256,
            case_execution_lease_sha256=authority.case_execution_lease_sha256,
            stage_sha256=authority.stage_sha256,
            case_id=authority.case_id,
            logical_call_id=authority.logical_call_id,
            actor_request_sha256=authority.actor_request_sha256,
            request_sha256=authority.request_sha256,
            transport_binding_sha256=authority.transport_binding_sha256,
            pricing_binding_sha256=authority.pricing_binding_sha256,
            execution_kind=LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS,
            status=status,
            dispatch_count=dispatch_count,
            response_envelope_sha256=response_envelope_sha256,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_status=cost_status,
            cost_usd_micros=cost_usd_micros,
            cancellation_requested=cancellation_requested,
            termination=stop.termination,
            worker_pid=None if process is None else process.pid,
            worker_exit_code=stop.worker_exit_code,
            worker_reaped=stop.worker_reaped,
            late_output_detected=late_output_detected,
            duration_ns=min(_MAX_DURATION_NS, max(0, time.monotonic_ns() - self._started_ns)),
            failure_code=failure_code,
        )

    def _publish(self, receipt: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
        trusted = snapshot_live_attempt_receipt(receipt)
        self._sink._commit(trusted)
        with self._state_lock:
            self._terminal_receipt = trusted
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.close()
        return snapshot_live_attempt_receipt(trusted)

    def cancel_and_join(self) -> LiveAttemptReceiptV1:
        """Cancel once and return only after a terminal process observation."""

        self._cancel_requested.set()
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            with self._state_lock:
                command_sent = self._dispatch_command_sent
            self._send(("CANCEL", self._authority_sha256))
            stop = self._stop_worker(cooperative=not command_sent)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            late_output = any(
                len(message) > 0 and message[0] == "COMPLETED" for message in messages
            )
            if not stop.worker_reaped:
                return self._publish(
                    self._make_receipt(
                        status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
                        dispatch_count=dispatch_count,
                        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                        cost_usd_micros=None,
                        cancellation_requested=True,
                        stop=stop,
                        late_output_detected=late_output,
                        failure_code="TERMINATION_UNCONFIRMED",
                    )
                )
            status = (
                LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
                if dispatch_count == 0
                else LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
            )
            cost_status = (
                LiveAttemptCostStatusV1.EXACT
                if dispatch_count == 0
                else LiveAttemptCostStatusV1.UNKNOWN
            )
            return self._publish(
                self._make_receipt(
                    status=status,
                    dispatch_count=dispatch_count,
                    cost_status=cost_status,
                    cost_usd_micros=0 if dispatch_count == 0 else None,
                    cancellation_requested=True,
                    stop=stop,
                    late_output_detected=late_output,
                )
            )

    def _failed(self, code: str) -> LiveAttemptReceiptV1:
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            stop = self._stop_worker(cooperative=False)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            termination_unconfirmed = not stop.worker_reaped
            return self._publish(
                self._make_receipt(
                    status=(
                        LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                        if termination_unconfirmed
                        else LiveAttemptStatusV1.FAILED
                    ),
                    dispatch_count=dispatch_count,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if dispatch_count == 0 and not termination_unconfirmed
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=(
                        0 if dispatch_count == 0 and not termination_unconfirmed else None
                    ),
                    cancellation_requested=termination_unconfirmed,
                    stop=stop,
                    late_output_detected=any(
                        len(message) > 0 and message[0] == "COMPLETED" for message in messages
                    ),
                    failure_code="TERMINATION_UNCONFIRMED" if termination_unconfirmed else code,
                )
            )

    def execute(self) -> LiveAttemptReceiptV1:
        """Dispatch the fixed worker once, enforcing the authority deadline."""

        with self._state_lock:
            if self._terminal_receipt is not None:
                return snapshot_live_attempt_receipt(self._terminal_receipt)
            if self._execute_started:
                raise LiveAttemptError("DUPLICATE_EXECUTION", "attempt execute was called twice")
            self._execute_started = True
            if self._cancel_requested.is_set():
                should_cancel = True
            else:
                should_cancel = False
                self._dispatch_command_sent = True
        if should_cancel:
            return self.cancel_and_join()
        if time.monotonic_ns() >= self._authority.deadline_monotonic_ns:
            return self.cancel_and_join()
        if not self._send(("DISPATCH", self._authority_sha256)):
            return self._failed("CPU_WORKER_DISPATCH_FAILED")

        expected_authority = self._authority_sha256
        expected_response = _cpu_response_sha256(expected_authority, self._authority.request_sha256)
        while True:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            if self._cancel_requested.is_set():
                return self.cancel_and_join()
            remaining_ns = self._authority.deadline_monotonic_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return self.cancel_and_join()
            message = self._receive(min(0.01, remaining_ns / 1_000_000_000))
            if message is None:
                process = self._process
                if process is not None and not process.is_alive():
                    return self._failed("CPU_WORKER_EXITED_WITHOUT_OUTPUT")
                continue
            if message == ("DISPATCHED", expected_authority):
                self._observe_dispatch((message,))
                continue
            if (
                len(message) == 7
                and message[0] == "COMPLETED"
                and message[1] == expected_authority
                and message[2] == expected_response
                and message[3:]
                == (
                    _CPU_INPUT_TOKENS,
                    _CPU_OUTPUT_TOKENS,
                    _CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                    _CPU_COST_USD_MICROS,
                )
            ):
                self._observe_dispatch((("DISPATCHED", expected_authority),))
                with self._finalize_lock:
                    terminal = self.terminal_receipt
                    if terminal is not None:
                        return terminal
                    process = self._process
                    if process is not None:
                        process.join(self._cancel_grace_seconds)
                    if process is not None and process.is_alive():
                        stop = self._stop_worker(cooperative=False)
                        return self._publish(
                            self._make_receipt(
                                status=(
                                    LiveAttemptStatusV1.FAILED
                                    if stop.worker_reaped
                                    else LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                                ),
                                dispatch_count=1,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=not stop.worker_reaped,
                                stop=stop,
                                failure_code=(
                                    "CPU_WORKER_DID_NOT_EXIT"
                                    if stop.worker_reaped
                                    else "TERMINATION_UNCONFIRMED"
                                ),
                            )
                        )
                    stop = _StopResult(
                        termination=LiveAttemptTerminationV1.NONE,
                        worker_reaped=True,
                        worker_exit_code=0 if process is None else process.exitcode,
                    )
                    if (
                        self._authority.max_output_tokens < _CPU_OUTPUT_TOKENS
                        or self._authority.max_cost_usd_micros < _CPU_COST_USD_MICROS
                    ):
                        return self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                stop=stop,
                                failure_code="CPU_FIXED_RESULT_EXCEEDS_AUTHORITY",
                            )
                        )
                    return self._publish(
                        self._make_receipt(
                            status=LiveAttemptStatusV1.COMPLETED,
                            dispatch_count=1,
                            response_envelope_sha256=expected_response,
                            input_tokens=_CPU_INPUT_TOKENS,
                            cached_input_tokens=0,
                            output_tokens=_CPU_OUTPUT_TOKENS,
                            total_tokens=_CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                            cost_status=LiveAttemptCostStatusV1.EXACT,
                            cost_usd_micros=_CPU_COST_USD_MICROS,
                            cancellation_requested=False,
                            stop=stop,
                        )
                    )
            return self._failed("CPU_WORKER_PROTOCOL_VIOLATION")


class CpuFixedLiveAttemptRunnerV1:
    """Launch only the module-owned, no-I/O CPU worker above."""

    def __init__(
        self,
        *,
        sink: MemoryLiveAttemptReceiptSinkV1,
        startup_timeout_ms: int = 1_000,
        cancel_grace_ms: int = 50,
    ) -> None:
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "CPU runner requires the exact memory sink")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 10_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 10_000)
        if os.name != "posix":
            raise LiveAttemptError(
                "CPU_PROCESS_CONTROL_UNAVAILABLE", "POSIX process control required"
            )
        self._sink = sink
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000

    def begin(
        self,
        authority: LiveAttemptAuthorityV1,
        *,
        confirmed_authority_sha256: str,
        script: CpuFixedAttemptScriptV1,
    ) -> CpuFixedLiveAttemptHandleV1:
        """Admit and start one fixed worker without authorizing dispatch yet."""

        trusted = snapshot_live_attempt_authority(authority)
        confirmed = _require_sha256(confirmed_authority_sha256, "confirmed_authority_sha256")
        actual = live_attempt_authority_sha256(trusted)
        if confirmed != actual:
            raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "confirmed authority hash differs")
        if type(script) is not CpuFixedAttemptScriptV1:
            raise LiveAttemptError("UNTRUSTED_CPU_SCRIPT", "CPU script must use the closed enum")
        self._sink._reserve(trusted)
        started_ns = time.monotonic_ns()
        if started_ns >= trusted.deadline_monotonic_ns:
            handle = CpuFixedLiveAttemptHandleV1(
                authority=trusted,
                sink=self._sink,
                process=None,
                connection=None,
                started_ns=started_ns,
                cancel_grace_seconds=self._cancel_grace_seconds,
            )
            handle.cancel_and_join()
            return handle
        context = multiprocessing.get_context("fork")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_cpu_fixed_attempt_worker,
                args=(child_connection, script.value, actual, trusted.request_sha256),
                name="mobileworld-r24-fixed-live-attempt",
                daemon=False,
            ),
        )
        try:
            process.start()
            child_connection.close()
        except Exception as exc:
            parent_connection.close()
            child_connection.close()
            failed = CpuFixedLiveAttemptHandleV1(
                authority=trusted,
                sink=self._sink,
                process=None,
                connection=None,
                started_ns=started_ns,
                cancel_grace_seconds=self._cancel_grace_seconds,
            )
            failed._failed("CPU_WORKER_START_FAILED")
            raise LiveAttemptError(
                "CPU_WORKER_START_FAILED", "fixed worker failed to start"
            ) from exc
        handle = CpuFixedLiveAttemptHandleV1(
            authority=trusted,
            sink=self._sink,
            process=process,
            connection=parent_connection,
            started_ns=started_ns,
            cancel_grace_seconds=self._cancel_grace_seconds,
        )
        ready_deadline_ns = min(
            trusted.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        remaining_ns = max(0, ready_deadline_ns - time.monotonic_ns())
        ready = handle._receive(remaining_ns / 1_000_000_000)
        if ready != ("READY", actual):
            handle._failed("CPU_WORKER_READY_FAILED")
            raise LiveAttemptError("CPU_WORKER_READY_FAILED", "fixed worker did not become ready")
        return handle


def _production_openai_attempt_worker(
    connection: Connection,
    factory: ProductionPostPreflightFactoryV1,
    case_lease: CaseExecutionLeaseV1,
    request_bytes: bytes,
    authority_sha256: str,
    expected_stage_sha256: str,
    role_value: str,
) -> None:
    """Own the secret, SDK client, and one provider call inside the child."""

    sdk_logging_state = _disable_provider_sdk_logging_for_child()
    secret_lease = None
    client = None
    http_client = None
    try:
        connection.send(("READY", authority_sha256))
        if connection.recv() != ("DISPATCH", authority_sha256):
            return
        role = OpenAIRoleV1(role_value)
        stage = factory.openai_stage(role)
        if factory.openai_stage_sha256(role) != _require_sha256(
            expected_stage_sha256, "expected_stage_sha256"
        ):
            raise LiveAttemptError(
                "PROVIDER_REQUEST_STAGE_MISMATCH", "child OpenAI stage binding differs"
            )
        attempt_role = (
            LiveAttemptRoleV1.RUBRIC
            if role is OpenAIRoleV1.RUBRIC
            else LiveAttemptRoleV1.HISTORY_POLICY
        )
        request_kwargs = _validate_sealed_provider_request(
            request_bytes,
            stage=stage,
            role=attempt_role,
        )
        secret_lease, api_key = factory._acquire_openai_secret_for_child_process(case_lease)

        from openai import DefaultHttpxClient, OpenAI, Timeout
        from openai.types.responses.response_usage import InputTokensDetails, ResponseUsage

        from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
            _project_openai_response,
        )

        timeout_seconds = stage.timeout_ms / 1_000
        timeout = Timeout(timeout_seconds)
        http_client = DefaultHttpxClient(timeout=timeout, trust_env=False)
        client = OpenAI(
            api_key=api_key,
            base_url=stage.endpoint.removesuffix("/responses"),
            timeout=timeout,
            max_retries=stage.sdk_max_retries,
            http_client=http_client,
        )
        # Linearize the potentially billable provider attempt immediately
        # before entering the SDK.  Secret/config/request/client failures that
        # happen above this point are exact zero-dispatch failures.
        connection.send(("DISPATCHED", authority_sha256))
        raw = cast(Any, client.responses.create)(**request_kwargs, timeout=timeout_seconds)
        returned_model = getattr(raw, "model", None)
        if type(returned_model) is not str or _SAFE_MODEL_ID.fullmatch(returned_model) is None:
            # Never serialize an unbounded, non-string, or otherwise unsafe
            # provider-controlled value across the process boundary.
            connection.send(("PROVIDER_RETURNED_MODEL_INVALID", authority_sha256))
            return
        if returned_model != stage.model:
            # The requested model is deliberately omitted: the parent derives
            # it only from its own private, authority-bound stage snapshot.
            connection.send(("PROVIDER_RETURNED_MODEL_MISMATCH", authority_sha256, returned_model))
            return
        envelope = _project_openai_response(raw, requested_model=stage.model)
        usage = getattr(raw, "usage", None)
        if type(usage) is not ResponseUsage or type(usage.input_tokens_details) is not (
            InputTokensDetails
        ):
            raise TypeError("provider response omitted exact cached-token usage")
        cached_input_tokens = usage.input_tokens_details.cached_tokens
        if (
            type(cached_input_tokens) is not int
            or cached_input_tokens < 0
            or cached_input_tokens > usage.input_tokens
        ):
            raise ValueError("provider cached-token usage differs")
        connection.send(("COMPLETED", authority_sha256, envelope, cached_input_tokens))
    except BaseException as exc:
        failure_code = (
            "PROVIDER_REQUEST_STAGE_MISMATCH"
            if isinstance(exc, LiveAttemptError) and exc.code == "PROVIDER_REQUEST_STAGE_MISMATCH"
            else "PROVIDER_CHILD_FAILED"
        )
        try:
            connection.send(("FAILED", authority_sha256, failure_code))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        elif http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass
        if secret_lease is not None:
            secret_lease.close()
        connection.close()
        _restore_provider_sdk_logging_after_child(sdk_logging_state)


def live_attempt_cost_usd_micros(
    pricing: LiveAttemptPricingV1,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> int:
    trusted = snapshot_live_attempt_pricing(pricing)
    for value, label in (
        (input_tokens, "input_tokens"),
        (cached_input_tokens, "cached_input_tokens"),
        (output_tokens, "output_tokens"),
    ):
        _require_int(value, label, 0, 100_000_000)
    if not 0 <= cached_input_tokens <= input_tokens:
        raise LiveAttemptError("INVALID_TOKEN_CENSUS", "cached input exceeds total input")
    numerator = (
        (input_tokens - cached_input_tokens) * trusted.input_usd_micros_per_million_tokens
        + cached_input_tokens * trusted.cached_input_usd_micros_per_million_tokens
        + output_tokens * trusted.output_usd_micros_per_million_tokens
    )
    return (numerator + 999_999) // 1_000_000


def live_attempt_worst_case_cost_usd_micros(
    pricing: LiveAttemptPricingV1,
    *,
    request_byte_count: int,
    max_output_tokens: int,
) -> int:
    """Return a conservative pre-dispatch reservation for one request.

    OpenAI tokenization operates on the UTF-8 byte stream, so the canonical
    request byte count is an upper bound on input-token count.  Treat every
    input byte as uncached (or at the higher cached rate, if configured) and
    reserve the full authorized output-token ceiling.  This is deliberately a
    cost admission bound, not a prediction of provider usage.
    """

    trusted = snapshot_live_attempt_pricing(pricing)
    _require_int(
        request_byte_count,
        "request_byte_count",
        2,
        _MAX_PROVIDER_REQUEST_BYTES,
    )
    _require_int(max_output_tokens, "max_output_tokens", 1, 1_000_000)
    input_rate = max(
        trusted.input_usd_micros_per_million_tokens,
        trusted.cached_input_usd_micros_per_million_tokens,
    )
    numerator = (
        request_byte_count * input_rate
        + max_output_tokens * trusted.output_usd_micros_per_million_tokens
    )
    return (numerator + 999_999) // 1_000_000


class ProductionOpenAIAttemptCallV1:
    """Exact callable recognized by the seam's cancel-and-join fence."""

    def __init__(
        self,
        *,
        authority: LiveAttemptAuthorityV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        pricing: LiveAttemptPricingV1,
        process: BaseProcess,
        connection: Connection,
        started_ns: int,
        cancel_grace_seconds: float,
        execution_kind: LiveAttemptExecutionKindV1,
        request: CanonicalHistoryPolicyRequestV1 | None = None,
        stage: OpenAIResponsesStageV1 | None = None,
    ) -> None:
        self._authority = snapshot_live_attempt_authority(authority)
        self._authority_sha256 = live_attempt_authority_sha256(self._authority)
        self._sink = sink
        self._pricing = snapshot_live_attempt_pricing(pricing)
        self._process = process
        self._connection: Connection | None = connection
        self._started_ns = started_ns
        self._cancel_grace_seconds = cancel_grace_seconds
        if type(execution_kind) is not LiveAttemptExecutionKindV1:
            raise LiveAttemptError("UNTRUSTED_EXECUTION_KIND", "execution kind differs")
        self._execution_kind = execution_kind
        self._request: CanonicalHistoryPolicyRequestV1 | None
        self._stage: OpenAIResponsesStageV1 | None
        if execution_kind is LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS:
            if (
                type(request) is not CanonicalHistoryPolicyRequestV1
                or type(stage) is not OpenAIResponsesStageV1
            ):
                raise LiveAttemptError(
                    "PROVIDER_REQUEST_STAGE_MISMATCH",
                    "production call lacks its sealed request and stage",
                )
            self._request = snapshot_canonical_history_policy_request(request)
            self._stage = _snapshot_openai_stage(stage)
            if (
                self._request.request_sha256 != self._authority.request_sha256
                or openai_stage_sha256(self._stage) != self._authority.stage_sha256
                or self._stage.max_output_tokens != self._authority.max_output_tokens
            ):
                raise LiveAttemptError(
                    "PROVIDER_REQUEST_STAGE_MISMATCH",
                    "production call request or stage authority differs",
                )
        else:
            if request is not None or stage is not None:
                raise LiveAttemptError(
                    "PROVIDER_REQUEST_STAGE_MISMATCH",
                    "CPU call cannot retain a production request or stage",
                )
            self._request = None
            self._stage = None
        self._dispatch_count = 0
        self._dispatch_command_sent = False
        self._execute_started = False
        self._cancel_requested = threading.Event()
        self._terminal_receipt: LiveAttemptReceiptV1 | None = None
        self._result: object | None = None
        self._completed_message_detected = False
        self._completed_message_count = 0
        self._observed_completed_response: _ObservedCompletedResponseV1 | None = None
        self._state_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._finalize_lock = threading.Lock()

    @property
    def authority_sha256(self) -> str:
        return self._authority_sha256

    @property
    def authority(self) -> LiveAttemptAuthorityV1:
        return snapshot_live_attempt_authority(self._authority)

    @property
    def dispatch_count(self) -> int:
        with self._state_lock:
            return self._dispatch_count

    @property
    def terminal_receipt(self) -> LiveAttemptReceiptV1 | None:
        with self._state_lock:
            value = self._terminal_receipt
        return None if value is None else snapshot_live_attempt_receipt(value)

    @property
    def terminal_receipt_sha256(self) -> str | None:
        receipt = self.terminal_receipt
        return None if receipt is None else live_attempt_receipt_sha256(receipt)

    @property
    def receipt_root_sha256(self) -> str:
        return self._sink.receipt_root_sha256

    def _receive(self, timeout_seconds: float) -> tuple[object, ...] | None:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return None
            try:
                if not connection.poll(max(0.0, timeout_seconds)):
                    return None
                value = connection.recv()
            except (EOFError, OSError):
                return None
            message = value if type(value) is tuple else ("INVALID",)
            if message and type(message[0]) is str and message[0] == "COMPLETED":
                # Keep recv + validated observation atomic against drain and
                # terminalization in a competing cancellation thread.
                self._observe_completed_message(message)
            return message

    def _observe_completed_message(self, message: tuple[object, ...]) -> None:
        """Retain only a unique, validated COMPLETED envelope as evidence.

        This runs immediately after ``recv`` so a cancellation thread cannot
        terminalize the attempt after another thread removed the envelope from
        the pipe but before that thread acquired the finalization lock.
        Malformed, conflicting, or duplicate payloads are never retained.
        """

        matched_authority = (
            len(message) >= 2 and type(message[1]) is str and message[1] == self._authority_sha256
        )
        with self._state_lock:
            self._completed_message_detected = True
            if matched_authority:
                self._dispatch_count = 1
                self._completed_message_count += 1
                message_count = self._completed_message_count
                if message_count > 1:
                    self._observed_completed_response = None
                    self._result = None
            else:
                message_count = 0
        if not matched_authority or message_count != 1 or len(message) != 4:
            return

        from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
            ResponsesEnvelopeV1,
            _detach_envelope,
        )

        try:
            raw_envelope = message[2]
            cached_input_tokens = message[3]
            stage = self._stage
            if type(raw_envelope) is not ResponsesEnvelopeV1:
                raise TypeError("provider child envelope type differs")
            if type(cached_input_tokens) is not int or cached_input_tokens < 0:
                raise TypeError("provider child cached-token usage differs")
            if type(stage) is not OpenAIResponsesStageV1:
                raise TypeError("provider child stage snapshot is absent")
            envelope = _detach_envelope(raw_envelope)
            if envelope.requested_model != stage.model or envelope.returned_model != stage.model:
                raise ValueError("provider child envelope model differs")
            usage = (
                envelope.input_tokens,
                envelope.output_tokens,
                envelope.total_tokens,
            )
            if all(value is None for value in usage):
                retained_cached_input_tokens: int | None = None
                cost_status = LiveAttemptCostStatusV1.UNKNOWN
                cost_usd_micros: int | None = None
            elif all(value is not None for value in usage):
                retained_cached_input_tokens = cached_input_tokens
                cost_usd_micros = live_attempt_cost_usd_micros(
                    self._pricing,
                    input_tokens=cast(int, envelope.input_tokens),
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=cast(int, envelope.output_tokens),
                )
                cost_status = LiveAttemptCostStatusV1.EXACT
            else:
                raise ValueError("provider child usage is incomplete")
            observed = _ObservedCompletedResponseV1(
                response_envelope_sha256=envelope.sha256,
                input_tokens=envelope.input_tokens,
                cached_input_tokens=retained_cached_input_tokens,
                output_tokens=envelope.output_tokens,
                total_tokens=envelope.total_tokens,
                cost_status=cost_status,
                cost_usd_micros=cost_usd_micros,
                requested_model=stage.model,
                returned_model=envelope.returned_model,
            )
        except Exception:
            return
        with self._state_lock:
            if self._completed_message_count != 1:
                return
            self._observed_completed_response = observed
            self._result = _detach_envelope(envelope)

    def _late_completed_response(
        self,
    ) -> tuple[bool, _ObservedCompletedResponseV1 | None]:
        with self._state_lock:
            return self._completed_message_detected, self._observed_completed_response

    def _send(self, value: tuple[str, str]) -> bool:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.send(value)
            except (BrokenPipeError, EOFError, OSError):
                return False
        return True

    def _drain(self) -> tuple[tuple[object, ...], ...]:
        messages: list[tuple[object, ...]] = []
        while True:
            message = self._receive(0.0)
            if message is None:
                return tuple(messages)
            messages.append(message)

    def _observe_dispatch(self, messages: tuple[tuple[object, ...], ...]) -> None:
        if any(message == ("DISPATCHED", self._authority_sha256) for message in messages):
            with self._state_lock:
                self._dispatch_count = 1

    def _stop_worker(self, *, cooperative: bool) -> _StopResult:
        process = self._process
        if not process.is_alive():
            process.join(0)
            return _StopResult(LiveAttemptTerminationV1.COOPERATIVE, True, process.exitcode)
        if cooperative:
            process.join(self._cancel_grace_seconds)
            if not process.is_alive():
                return _StopResult(
                    LiveAttemptTerminationV1.COOPERATIVE,
                    True,
                    process.exitcode,
                )
        process.terminate()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.TERM, True, process.exitcode)
        process.kill()
        process.join(
            max(
                PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1 / 1_000,
                self._cancel_grace_seconds,
            )
        )
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.KILL, True, process.exitcode)
        return _StopResult(LiveAttemptTerminationV1.UNCONFIRMED, False, None)

    def _make_receipt(
        self,
        *,
        status: LiveAttemptStatusV1,
        dispatch_count: int,
        stop: _StopResult,
        cost_status: LiveAttemptCostStatusV1,
        cost_usd_micros: int | None,
        cancellation_requested: bool,
        response_envelope_sha256: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        late_output_detected: bool = False,
        failure_code: str | None = None,
        requested_model: str | None = None,
        returned_model: str | None = None,
    ) -> LiveAttemptReceiptV1:
        authority = self._authority
        return LiveAttemptReceiptV1(
            attempt_id=authority.attempt_id,
            role=authority.role,
            authority_sha256=self._authority_sha256,
            manifest_sha256=authority.manifest_sha256,
            preflight_sha256=authority.preflight_sha256,
            case_execution_lease_sha256=authority.case_execution_lease_sha256,
            stage_sha256=authority.stage_sha256,
            case_id=authority.case_id,
            logical_call_id=authority.logical_call_id,
            actor_request_sha256=authority.actor_request_sha256,
            request_sha256=authority.request_sha256,
            transport_binding_sha256=authority.transport_binding_sha256,
            pricing_binding_sha256=authority.pricing_binding_sha256,
            execution_kind=self._execution_kind,
            status=status,
            dispatch_count=dispatch_count,
            response_envelope_sha256=response_envelope_sha256,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_status=cost_status,
            cost_usd_micros=cost_usd_micros,
            cancellation_requested=cancellation_requested,
            termination=stop.termination,
            worker_pid=self._process.pid,
            worker_exit_code=stop.worker_exit_code,
            worker_reaped=stop.worker_reaped,
            late_output_detected=late_output_detected,
            duration_ns=min(_MAX_DURATION_NS, max(0, time.monotonic_ns() - self._started_ns)),
            failure_code=failure_code,
            requested_model=requested_model,
            returned_model=returned_model,
        )

    def _publish(self, receipt: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
        trusted = snapshot_live_attempt_receipt(receipt)
        self._sink._commit(trusted)
        with self._state_lock:
            self._terminal_receipt = trusted
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.close()
        return snapshot_live_attempt_receipt(trusted)

    def _failed(self, code: str) -> LiveAttemptReceiptV1:
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            stop = self._stop_worker(cooperative=False)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            termination_unconfirmed = not stop.worker_reaped
            late_output, observed = self._late_completed_response()
            exact_observed_cost = (
                observed is not None and observed.cost_status is LiveAttemptCostStatusV1.EXACT
            )
            return self._publish(
                self._make_receipt(
                    status=(
                        LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                        if termination_unconfirmed
                        else LiveAttemptStatusV1.FAILED
                    ),
                    dispatch_count=dispatch_count,
                    stop=stop,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if (
                            (dispatch_count == 0 and not termination_unconfirmed)
                            or exact_observed_cost
                        )
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=(
                        observed.cost_usd_micros
                        if exact_observed_cost and observed is not None
                        else 0
                        if dispatch_count == 0 and not termination_unconfirmed
                        else None
                    ),
                    cancellation_requested=termination_unconfirmed,
                    response_envelope_sha256=(
                        None if observed is None else observed.response_envelope_sha256
                    ),
                    input_tokens=None if observed is None else observed.input_tokens,
                    cached_input_tokens=(
                        None if observed is None else observed.cached_input_tokens
                    ),
                    output_tokens=None if observed is None else observed.output_tokens,
                    total_tokens=None if observed is None else observed.total_tokens,
                    late_output_detected=late_output,
                    failure_code="TERMINATION_UNCONFIRMED" if termination_unconfirmed else code,
                    requested_model=None if observed is None else observed.requested_model,
                    returned_model=None if observed is None else observed.returned_model,
                )
            )

    def _provider_model_failure(
        self,
        *,
        failure_code: str,
        returned_model: str | None,
    ) -> LiveAttemptReceiptV1:
        """Reap a model-provenance failure and bind only parent-trusted request data."""

        stage = self._stage
        if type(stage) is not OpenAIResponsesStageV1:
            raise LiveAttemptError(
                "PROVIDER_CHILD_PROTOCOL_VIOLATION", "production stage snapshot is absent"
            )
        if failure_code == "PROVIDER_RETURNED_MODEL_MISMATCH":
            if (
                type(returned_model) is not str
                or _SAFE_MODEL_ID.fullmatch(returned_model) is None
                or returned_model == stage.model
            ):
                raise LiveAttemptError(
                    "PROVIDER_CHILD_PROTOCOL_VIOLATION", "model mismatch IPC is malformed"
                )
        elif failure_code == "PROVIDER_RETURNED_MODEL_INVALID":
            if returned_model is not None:
                raise LiveAttemptError(
                    "PROVIDER_CHILD_PROTOCOL_VIOLATION", "invalid-model IPC exposed a value"
                )
        else:
            raise LiveAttemptError(
                "PROVIDER_CHILD_PROTOCOL_VIOLATION", "model failure IPC code is unknown"
            )

        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            self._process.join(self._cancel_grace_seconds)
            if self._process.is_alive():
                stop = self._stop_worker(cooperative=False)
                terminal_code = (
                    "PROVIDER_CHILD_DID_NOT_EXIT"
                    if stop.worker_reaped
                    else "TERMINATION_UNCONFIRMED"
                )
                return self._publish(
                    self._make_receipt(
                        status=(
                            LiveAttemptStatusV1.FAILED
                            if stop.worker_reaped
                            else LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                        ),
                        dispatch_count=1,
                        stop=stop,
                        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                        cost_usd_micros=None,
                        cancellation_requested=not stop.worker_reaped,
                        failure_code=terminal_code,
                    )
                )
            stop = _StopResult(
                LiveAttemptTerminationV1.NONE,
                True,
                self._process.exitcode,
            )
            if stop.worker_exit_code != 0:
                return self._publish(
                    self._make_receipt(
                        status=LiveAttemptStatusV1.FAILED,
                        dispatch_count=1,
                        stop=stop,
                        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                        cost_usd_micros=None,
                        cancellation_requested=False,
                        failure_code="PROVIDER_CHILD_FAILED",
                    )
                )
            return self._publish(
                self._make_receipt(
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=1,
                    stop=stop,
                    cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                    cost_usd_micros=None,
                    cancellation_requested=False,
                    failure_code=failure_code,
                    requested_model=stage.model,
                    returned_model=returned_model,
                )
            )

    def _cancel_and_join_locked(self) -> LiveAttemptReceiptV1:
        """Terminalize cancellation while the caller owns ``_finalize_lock``."""

        terminal = self.terminal_receipt
        if terminal is not None:
            return terminal
        # The cancel flag and every terminal publication share one
        # linearization lock.  It therefore cannot flip after a successful
        # response's final check but before that response is published.
        self._cancel_requested.set()
        with self._state_lock:
            command_sent = self._dispatch_command_sent
        self._send(("CANCEL", self._authority_sha256))
        stop = self._stop_worker(cooperative=not command_sent)
        messages = self._drain()
        self._observe_dispatch(messages)
        dispatch_count = self.dispatch_count
        late_output, observed = self._late_completed_response()
        exact_observed_cost = (
            observed is not None and observed.cost_status is LiveAttemptCostStatusV1.EXACT
        )
        if not stop.worker_reaped:
            return self._publish(
                self._make_receipt(
                    status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
                    dispatch_count=dispatch_count,
                    stop=stop,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if exact_observed_cost
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=(
                        observed.cost_usd_micros
                        if exact_observed_cost and observed is not None
                        else None
                    ),
                    cancellation_requested=True,
                    response_envelope_sha256=(
                        None if observed is None else observed.response_envelope_sha256
                    ),
                    input_tokens=None if observed is None else observed.input_tokens,
                    cached_input_tokens=(
                        None if observed is None else observed.cached_input_tokens
                    ),
                    output_tokens=None if observed is None else observed.output_tokens,
                    total_tokens=None if observed is None else observed.total_tokens,
                    late_output_detected=late_output,
                    failure_code="TERMINATION_UNCONFIRMED",
                    requested_model=None if observed is None else observed.requested_model,
                    returned_model=None if observed is None else observed.returned_model,
                )
            )
        pre_dispatch = dispatch_count == 0
        return self._publish(
            self._make_receipt(
                status=(
                    LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
                    if pre_dispatch
                    else LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
                ),
                dispatch_count=dispatch_count,
                stop=stop,
                cost_status=(
                    LiveAttemptCostStatusV1.EXACT
                    if pre_dispatch or exact_observed_cost
                    else LiveAttemptCostStatusV1.UNKNOWN
                ),
                cost_usd_micros=(
                    0
                    if pre_dispatch
                    else observed.cost_usd_micros
                    if exact_observed_cost and observed is not None
                    else None
                ),
                cancellation_requested=True,
                response_envelope_sha256=(
                    None if observed is None else observed.response_envelope_sha256
                ),
                input_tokens=None if observed is None else observed.input_tokens,
                cached_input_tokens=(None if observed is None else observed.cached_input_tokens),
                output_tokens=None if observed is None else observed.output_tokens,
                total_tokens=None if observed is None else observed.total_tokens,
                late_output_detected=late_output,
                requested_model=None if observed is None else observed.requested_model,
                returned_model=None if observed is None else observed.returned_model,
            )
        )

    def cancel_and_join(self) -> LiveAttemptReceiptV1:
        """TERM, then KILL if needed, and publish only after waitpid observation."""

        with self._finalize_lock:
            return self._cancel_and_join_locked()

    def __call__(self) -> object:
        with self._state_lock:
            if self._execute_started:
                raise LiveAttemptError("DUPLICATE_EXECUTION", "attempt was called twice")
            self._execute_started = True
            cancelled = self._cancel_requested.is_set()
        if cancelled or time.monotonic_ns() >= self._authority.deadline_monotonic_ns:
            self.cancel_and_join()
            raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt was cancelled")
        if self._execution_kind is LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS:
            request = self._request
            stage = self._stage
            try:
                if (
                    type(request) is not CanonicalHistoryPolicyRequestV1
                    or type(stage) is not OpenAIResponsesStageV1
                ):
                    raise LiveAttemptError(
                        "PROVIDER_REQUEST_STAGE_MISMATCH", "sealed provider request is absent"
                    )
                trusted_request = snapshot_canonical_history_policy_request(request)
                if (
                    trusted_request.request_sha256 != self._authority.request_sha256
                    or openai_stage_sha256(_snapshot_openai_stage(stage))
                    != self._authority.stage_sha256
                ):
                    raise LiveAttemptError(
                        "PROVIDER_REQUEST_STAGE_MISMATCH", "sealed request authority drifted"
                    )
                _validate_sealed_provider_request(
                    trusted_request.canonical_bytes,
                    stage=stage,
                    role=self._authority.role,
                )
            except LiveAttemptError as exc:
                self._failed("PROVIDER_REQUEST_STAGE_MISMATCH")
                raise LiveAttemptError(
                    "PROVIDER_REQUEST_STAGE_MISMATCH",
                    "provider request differs from its sealed stage",
                ) from exc
        with self._state_lock:
            if self._cancel_requested.is_set():
                cancelled = True
            else:
                cancelled = False
                self._dispatch_command_sent = True
        if cancelled:
            self.cancel_and_join()
            raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt was cancelled")
        if not self._send(("DISPATCH", self._authority_sha256)):
            self._failed("PROVIDER_CHILD_DISPATCH_FAILED")
            raise LiveAttemptError("PROVIDER_CHILD_DISPATCH_FAILED", "child dispatch failed")

        while True:
            if self._cancel_requested.is_set():
                self.cancel_and_join()
                raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt was cancelled")
            remaining_ns = self._authority.deadline_monotonic_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                self.cancel_and_join()
                raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt deadline elapsed")
            message = self._receive(min(0.01, remaining_ns / 1_000_000_000))
            if message is None:
                if not self._process.is_alive():
                    self._failed("PROVIDER_CHILD_EXITED_WITHOUT_OUTPUT")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_EXITED_WITHOUT_OUTPUT", "child exited without output"
                    )
                continue
            if message == ("DISPATCHED", self._authority_sha256):
                self._observe_dispatch((message,))
                continue
            if message and message[0] in {
                "PROVIDER_RETURNED_MODEL_INVALID",
                "PROVIDER_RETURNED_MODEL_MISMATCH",
            }:
                # Only the returned safe ID may cross from the child.  The
                # requested ID always comes from this parent's sealed stage.
                self._observe_dispatch((("DISPATCHED", self._authority_sha256),))
                try:
                    if message == (
                        "PROVIDER_RETURNED_MODEL_INVALID",
                        self._authority_sha256,
                    ):
                        failure_code = "PROVIDER_RETURNED_MODEL_INVALID"
                        returned_model: str | None = None
                    elif (
                        len(message) == 3
                        and message[0] == "PROVIDER_RETURNED_MODEL_MISMATCH"
                        and message[1] == self._authority_sha256
                    ):
                        failure_code = "PROVIDER_RETURNED_MODEL_MISMATCH"
                        returned_model = cast(str, message[2])
                    else:
                        raise LiveAttemptError(
                            "PROVIDER_CHILD_PROTOCOL_VIOLATION",
                            "provider model IPC differs",
                        )
                    receipt = self._provider_model_failure(
                        failure_code=failure_code,
                        returned_model=returned_model,
                    )
                except LiveAttemptError as exc:
                    if self.terminal_receipt is None:
                        self._failed("PROVIDER_CHILD_PROTOCOL_VIOLATION")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_PROTOCOL_VIOLATION", "provider model IPC differs"
                    ) from exc
                terminal_code = receipt.failure_code
                if terminal_code is None:
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_PROTOCOL_VIOLATION",
                        "provider model failure receipt lacks a code",
                    )
                raise LiveAttemptError(terminal_code, "provider returned model differs")
            if (
                len(message) == 3
                and message[0] == "FAILED"
                and message[1] == self._authority_sha256
                and message[2] in {"PROVIDER_CHILD_FAILED", "PROVIDER_REQUEST_STAGE_MISMATCH"}
            ):
                failure_code = cast(str, message[2])
                self._failed(failure_code)
                raise LiveAttemptError(failure_code, "provider child failed")
            if (
                len(message) == 4
                and message[0] == "COMPLETED"
                and message[1] == self._authority_sha256
            ):
                self._observe_dispatch((("DISPATCHED", self._authority_sha256),))
                from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
                    ResponsesEnvelopeV1,
                    _detach_envelope,
                )

                try:
                    raw_envelope = message[2]
                    cached_input_tokens = message[3]
                    if type(raw_envelope) is not ResponsesEnvelopeV1:
                        raise TypeError("provider child envelope type differs")
                    if type(cached_input_tokens) is not int or cached_input_tokens < 0:
                        raise TypeError("provider child cached-token usage differs")
                    envelope = _detach_envelope(raw_envelope)
                except Exception as exc:
                    self._failed("PROVIDER_CHILD_RESPONSE_INVALID")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_RESPONSE_INVALID", "child response differs"
                    ) from exc
                completion_stage = self._stage
                if type(completion_stage) is not OpenAIResponsesStageV1:
                    self._failed("PROVIDER_CHILD_PROTOCOL_VIOLATION")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_PROTOCOL_VIOLATION",
                        "completed provider result lacks its sealed stage",
                    )
                with self._finalize_lock:
                    terminal = self.terminal_receipt
                    if terminal is not None:
                        raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt is terminal")
                    if (
                        self._cancel_requested.is_set()
                        or time.monotonic_ns() >= self._authority.deadline_monotonic_ns
                    ):
                        self._cancel_and_join_locked()
                        raise LiveAttemptError(
                            "LIVE_ATTEMPT_CANCELLED", "attempt cancellation won finalization"
                        )
                    with self._state_lock:
                        self._result = envelope
                    self._process.join(self._cancel_grace_seconds)
                    if (
                        self._cancel_requested.is_set()
                        or time.monotonic_ns() >= self._authority.deadline_monotonic_ns
                    ):
                        self._cancel_and_join_locked()
                        raise LiveAttemptError(
                            "LIVE_ATTEMPT_CANCELLED", "attempt cancellation won finalization"
                        )
                    if self._process.is_alive():
                        stop = self._stop_worker(cooperative=False)
                        failure_code = (
                            "PROVIDER_CHILD_DID_NOT_EXIT"
                            if stop.worker_reaped
                            else "TERMINATION_UNCONFIRMED"
                        )
                        self._publish(
                            self._make_receipt(
                                status=(
                                    LiveAttemptStatusV1.FAILED
                                    if stop.worker_reaped
                                    else LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
                                ),
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=not stop.worker_reaped,
                                response_envelope_sha256=envelope.sha256,
                                input_tokens=envelope.input_tokens,
                                cached_input_tokens=(
                                    cached_input_tokens
                                    if envelope.input_tokens is not None
                                    else None
                                ),
                                output_tokens=envelope.output_tokens,
                                total_tokens=envelope.total_tokens,
                                failure_code=failure_code,
                                requested_model=completion_stage.model,
                                returned_model=envelope.returned_model,
                            )
                        )
                        raise LiveAttemptError(failure_code, "child remained alive")
                    stop = _StopResult(
                        LiveAttemptTerminationV1.NONE,
                        True,
                        self._process.exitcode,
                    )
                    if (
                        envelope.input_tokens is None
                        or envelope.output_tokens is None
                        or envelope.total_tokens is None
                    ):
                        self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                response_envelope_sha256=envelope.sha256,
                                failure_code="PROVIDER_USAGE_MISSING",
                                requested_model=completion_stage.model,
                                returned_model=envelope.returned_model,
                            )
                        )
                        raise LiveAttemptError(
                            "PROVIDER_USAGE_MISSING", "provider omitted usage accounting"
                        )
                    cost = live_attempt_cost_usd_micros(
                        self._pricing,
                        input_tokens=envelope.input_tokens,
                        cached_input_tokens=cached_input_tokens,
                        output_tokens=envelope.output_tokens,
                    )
                    if (
                        envelope.output_tokens > self._authority.max_output_tokens
                        or cost > self._authority.max_cost_usd_micros
                        or self._process.exitcode != 0
                    ):
                        self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.EXACT,
                                cost_usd_micros=cost,
                                cancellation_requested=False,
                                response_envelope_sha256=envelope.sha256,
                                input_tokens=envelope.input_tokens,
                                cached_input_tokens=cached_input_tokens,
                                output_tokens=envelope.output_tokens,
                                total_tokens=envelope.total_tokens,
                                failure_code="PROVIDER_RESULT_EXCEEDS_AUTHORITY",
                                requested_model=completion_stage.model,
                                returned_model=envelope.returned_model,
                            )
                        )
                        raise LiveAttemptError(
                            "PROVIDER_RESULT_EXCEEDS_AUTHORITY", "provider result exceeds authority"
                        )
                    if (
                        self._cancel_requested.is_set()
                        or time.monotonic_ns() >= self._authority.deadline_monotonic_ns
                    ):
                        self._cancel_and_join_locked()
                        raise LiveAttemptError(
                            "LIVE_ATTEMPT_CANCELLED", "attempt cancellation won finalization"
                        )
                    self._publish(
                        self._make_receipt(
                            status=LiveAttemptStatusV1.COMPLETED,
                            dispatch_count=1,
                            stop=stop,
                            cost_status=LiveAttemptCostStatusV1.EXACT,
                            cost_usd_micros=cost,
                            cancellation_requested=False,
                            response_envelope_sha256=envelope.sha256,
                            input_tokens=envelope.input_tokens,
                            cached_input_tokens=cached_input_tokens,
                            output_tokens=envelope.output_tokens,
                            total_tokens=envelope.total_tokens,
                            requested_model=completion_stage.model,
                            returned_model=envelope.returned_model,
                        )
                    )
                    return _detach_envelope(envelope)
            self._failed("PROVIDER_CHILD_PROTOCOL_VIOLATION")
            raise LiveAttemptError("PROVIDER_CHILD_PROTOCOL_VIOLATION", "child protocol differs")


@dataclass(frozen=True, slots=True)
class _PendingHistoryAttemptConstraintV1:
    attempt_id: str
    logical_call_id: str
    case_id: str
    case_execution_lease_sha256: str
    constraint_registered_monotonic_ns: int
    case_execution_deadline_monotonic_ns: int
    request_timeout_ns: int
    max_cost_usd_micros: int


class ProductionOpenAIAttemptRunnerV1:
    """Role-bound live runner for exact RUBRIC or HISTORY_POLICY attempts."""

    def __init__(
        self,
        *,
        factory: ProductionPostPreflightFactoryV1,
        role: LiveAttemptRoleV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        pricing: LiveAttemptPricingV1,
        confirmed_pricing_sha256: str,
        startup_timeout_ms: int = 5_000,
        cancel_grace_ms: int = PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1,
    ) -> None:
        if type(factory) is not ProductionPostPreflightFactoryV1:
            raise LiveAttemptError("UNTRUSTED_PRODUCTION_FACTORY", "factory type differs")
        if (
            role not in {LiveAttemptRoleV1.RUBRIC, LiveAttemptRoleV1.HISTORY_POLICY}
            or type(role) is not LiveAttemptRoleV1
        ):
            raise LiveAttemptError("UNSUPPORTED_LIVE_ROLE", "live attempt role differs")
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "sink type differs")
        if type(pricing) is not LiveAttemptPricingV1:
            raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
        pricing_sha256 = live_attempt_pricing_sha256(pricing)
        if _require_sha256(confirmed_pricing_sha256, "confirmed_pricing_sha256") != pricing_sha256:
            raise LiveAttemptError("PRICING_HASH_DRIFT", "confirmed pricing hash differs")
        if factory.pricing_binding_sha256 != pricing_sha256:
            raise LiveAttemptError(
                "PRICING_AUTHORITY_MISMATCH",
                "pricing differs from the post-preflight factory",
            )
        stage_role = (
            OpenAIRoleV1.RUBRIC if role is LiveAttemptRoleV1.RUBRIC else OpenAIRoleV1.HISTORY_POLICY
        )
        stage = factory.openai_stage(stage_role)
        if stage.model != pricing.model:
            raise LiveAttemptError("PRICING_MODEL_DRIFT", "pricing model differs from stage")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 30_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 30_000)
        if os.name != "posix":
            raise LiveAttemptError(
                "PRODUCTION_PROCESS_CONTROL_UNAVAILABLE", "POSIX process control required"
            )
        self._factory = factory
        self._role = role
        self._stage = stage
        self._sink = sink
        self._pricing = snapshot_live_attempt_pricing(pricing)
        self._pricing_sha256 = pricing_sha256
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000
        self._constraint_lock = threading.Lock()
        self._pending_history_constraints: dict[str, _PendingHistoryAttemptConstraintV1] = {}
        self._known_history_constraint_attempt_ids: set[str] = set()
        self._attempt_deadline_bindings: dict[str, LiveAttemptDeadlineBindingV1] = {}
        self._attempt_requests: dict[str, CanonicalHistoryPolicyRequestV1] = {}
        self._attempt_calls: dict[str, ProductionOpenAIAttemptCallV1] = {}

    @property
    def factory_binding_sha256(self) -> str:
        return _require_sha256(self._factory.factory_binding_sha256, "factory binding")

    @property
    def pricing_binding_sha256(self) -> str:
        return self._pricing_sha256

    @property
    def pricing(self) -> LiveAttemptPricingV1:
        return snapshot_live_attempt_pricing(self._pricing)

    @property
    def manifest_sha256(self) -> str:
        return _require_sha256(self._factory.manifest_sha256, "manifest binding")

    @property
    def preflight_report_sha256(self) -> str:
        return _require_sha256(self._factory.preflight_report_sha256, "preflight binding")

    @property
    def role(self) -> LiveAttemptRoleV1:
        return self._role

    @property
    def openai_stage(self) -> OpenAIResponsesStageV1:
        return OpenAIResponsesStageV1(
            role=self._stage.role,
            model=self._stage.model,
            endpoint=self._stage.endpoint,
            transport_kind=self._stage.transport_kind,
            transport_authority=self._stage.transport_authority,
            openai_sdk_version=self._stage.openai_sdk_version,
            sdk_max_retries=self._stage.sdk_max_retries,
            external_network_on_call=self._stage.external_network_on_call,
            model_on_call=self._stage.model_on_call,
            max_output_tokens=self._stage.max_output_tokens,
            timeout_ms=self._stage.timeout_ms,
            max_attempts=self._stage.max_attempts,
            store=self._stage.store,
        )

    @property
    def openai_stage_sha256(self) -> str:
        return _require_sha256(
            self._factory.openai_stage_sha256(self._stage.role), "OpenAI stage binding"
        )

    def attest_case_execution_lease(self, case_lease: CaseExecutionLeaseV1) -> CaseExecutionLeaseV1:
        return self._factory.validate_case_execution_lease(case_lease)

    def terminal_receipt_for_attempt(self, attempt_id: str) -> LiveAttemptReceiptV1 | None:
        """Return a detached terminal emitted while ``begin`` was in progress.

        A child that cannot be proven reaped during START/READY admission can
        make ``begin`` raise before it returns a callable attempt.  The request
        owner still needs the sink-confirmed terminal in order to retain the
        exact request proof and trip the run-fatal audit path.
        """

        return self._sink.receipt_for(attempt_id)

    def attempt_authority_for_attempt(self, attempt_id: str) -> LiveAttemptAuthorityV1 | None:
        """Return the detached start authority paired with a sink attempt ID."""

        return self._sink.authority_for(attempt_id)

    def register_history_attempt_constraint(
        self,
        *,
        case_lease: CaseExecutionLeaseV1,
        attempt_id: str,
        logical_call_id: str,
        case_execution_deadline_monotonic_ns: int,
        request_timeout_ns: int,
        max_cost_usd_micros: int,
    ) -> None:
        """Register one independently issued case ceiling before R2.2 prepares.

        Registrations are attempt-ID bound and one-shot.  Releasing an unused
        registration does not make its ID reusable, preventing a later logical
        call from inheriting stale authority.
        """

        if self._role is not LiveAttemptRoleV1.HISTORY_POLICY:
            raise LiveAttemptError(
                "HISTORY_CONSTRAINT_ROLE_MISMATCH",
                "only the history-policy runner accepts this registration",
            )
        lease = self._factory.validate_case_execution_lease(case_lease)
        _require_id(attempt_id, "attempt_id")
        _require_id(logical_call_id, "logical_call_id")
        _require_int(
            case_execution_deadline_monotonic_ns,
            "case_execution_deadline_monotonic_ns",
            1,
            (1 << 63) - 1,
        )
        _require_int(request_timeout_ns, "request_timeout_ns", 1, (1 << 63) - 1)
        _require_int(
            max_cost_usd_micros,
            "max_cost_usd_micros",
            0,
            100_000_000_000,
        )
        registered_ns = time.monotonic_ns()
        if case_execution_deadline_monotonic_ns <= registered_ns:
            raise LiveAttemptError(
                "ATTEMPT_DEADLINE_ELAPSED", "case attempt deadline already elapsed"
            )
        pending = _PendingHistoryAttemptConstraintV1(
            attempt_id=attempt_id,
            logical_call_id=logical_call_id,
            case_id=lease.case_id,
            case_execution_lease_sha256=case_execution_lease_sha256(lease),
            constraint_registered_monotonic_ns=registered_ns,
            case_execution_deadline_monotonic_ns=(case_execution_deadline_monotonic_ns),
            request_timeout_ns=request_timeout_ns,
            max_cost_usd_micros=max_cost_usd_micros,
        )
        with self._constraint_lock:
            if attempt_id in self._known_history_constraint_attempt_ids:
                raise LiveAttemptError(
                    "DUPLICATE_ATTEMPT_ID", "history constraint attempt ID was already used"
                )
            self._known_history_constraint_attempt_ids.add(attempt_id)
            self._pending_history_constraints[attempt_id] = pending

    def discard_unformed_history_attempt_constraint(
        self, *, attempt_id: str, logical_call_id: str
    ) -> bool:
        """Discard an unused registration without permitting ID reuse."""

        _require_id(attempt_id, "attempt_id")
        _require_id(logical_call_id, "logical_call_id")
        with self._constraint_lock:
            pending = self._pending_history_constraints.get(attempt_id)
            if pending is None:
                return False
            if pending.logical_call_id != logical_call_id:
                raise LiveAttemptError(
                    "HISTORY_CONSTRAINT_BINDING_MISMATCH",
                    "unused history constraint belongs to another logical call",
                )
            del self._pending_history_constraints[attempt_id]
            return True

    def attempt_deadline_binding_for_attempt(
        self, attempt_id: str
    ) -> LiveAttemptDeadlineBindingV1 | None:
        _require_id(attempt_id, "attempt_id")
        with self._constraint_lock:
            value = self._attempt_deadline_bindings.get(attempt_id)
        return None if value is None else snapshot_live_attempt_deadline_binding(value)

    def canonical_request_for_attempt(
        self, attempt_id: str
    ) -> CanonicalHistoryPolicyRequestV1 | None:
        _require_id(attempt_id, "attempt_id")
        with self._constraint_lock:
            value = self._attempt_requests.get(attempt_id)
        return None if value is None else snapshot_canonical_history_policy_request(value)

    def response_envelope_for_attempt(self, attempt_id: str) -> object | None:
        """Return only an observed envelope bound by the terminal receipt."""

        _require_id(attempt_id, "attempt_id")
        with self._constraint_lock:
            call = self._attempt_calls.get(attempt_id)
        if call is None:
            return None
        with call._state_lock:
            result = call._result
            terminal = call._terminal_receipt
        if result is None:
            return None
        if terminal is None or terminal.response_envelope_sha256 is None:
            return None
        from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
            ResponsesEnvelopeV1,
        )

        if type(result) is not ResponsesEnvelopeV1:
            raise LiveAttemptError(
                "PROVIDER_CHILD_PROTOCOL_VIOLATION",
                "retained provider response envelope type differs",
            )
        if result.sha256 != terminal.response_envelope_sha256:
            raise LiveAttemptError(
                "PROVIDER_CHILD_PROTOCOL_VIOLATION",
                "retained provider response differs from terminal receipt",
            )
        return ResponsesEnvelopeV1(
            response_id=result.response_id,
            requested_model=result.requested_model,
            returned_model=result.returned_model,
            status=result.status,
            service_tier=result.service_tier,
            output_text=result.output_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            schema_version=result.schema_version,
        )

    def _consume_history_attempt_constraint(
        self,
        *,
        lease: CaseExecutionLeaseV1,
        attempt_id: str,
        logical_call_id: str,
        requested_call_deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        begin_observed_monotonic_ns: int,
    ) -> LiveAttemptDeadlineBindingV1:
        with self._constraint_lock:
            pending = self._pending_history_constraints.pop(attempt_id, None)
        if pending is None:
            raise LiveAttemptError(
                "HISTORY_ATTEMPT_CONSTRAINT_REQUIRED",
                "history attempt has no one-shot case constraint",
            )
        if (
            pending.logical_call_id != logical_call_id
            or pending.case_id != lease.case_id
            or pending.case_execution_lease_sha256 != case_execution_lease_sha256(lease)
            or pending.max_cost_usd_micros != max_cost_usd_micros
        ):
            raise LiveAttemptError(
                "HISTORY_CONSTRAINT_BINDING_MISMATCH",
                "history attempt differs from its one-shot case constraint",
            )
        return _build_live_attempt_deadline_binding(
            attempt_id=attempt_id,
            logical_call_id=logical_call_id,
            case_id=lease.case_id,
            case_execution_lease_sha256=pending.case_execution_lease_sha256,
            constraint_registered_monotonic_ns=(pending.constraint_registered_monotonic_ns),
            requested_call_deadline_monotonic_ns=(requested_call_deadline_monotonic_ns),
            request_timeout_ns=pending.request_timeout_ns,
            begin_observed_monotonic_ns=begin_observed_monotonic_ns,
            case_execution_deadline_monotonic_ns=(pending.case_execution_deadline_monotonic_ns),
            max_cost_usd_micros=pending.max_cost_usd_micros,
        )

    def begin(
        self,
        *,
        case_lease: CaseExecutionLeaseV1,
        attempt_id: str,
        logical_call_id: str,
        request: CanonicalHistoryPolicyRequestV1,
        transport_binding_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
    ) -> ProductionOpenAIAttemptCallV1:
        lease = self._factory.validate_case_execution_lease(case_lease)
        if lease.execution_scope is not CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE:
            raise LiveAttemptError("LIVE_SCOPE_REQUIRED", "case lease is not live")
        trusted_request = snapshot_canonical_history_policy_request(request)
        begin_observed_ns = time.monotonic_ns()
        deadline_binding: LiveAttemptDeadlineBindingV1 | None = None
        effective_deadline_ns = deadline_monotonic_ns
        if self._role is LiveAttemptRoleV1.HISTORY_POLICY:
            deadline_binding = self._consume_history_attempt_constraint(
                lease=lease,
                attempt_id=attempt_id,
                logical_call_id=logical_call_id,
                requested_call_deadline_monotonic_ns=deadline_monotonic_ns,
                max_cost_usd_micros=max_cost_usd_micros,
                begin_observed_monotonic_ns=begin_observed_ns,
            )
            effective_deadline_ns = deadline_binding.effective_deadline_monotonic_ns
        authority = LiveAttemptAuthorityV1(
            attempt_id=attempt_id,
            role=self._role,
            manifest_sha256=lease.manifest_sha256,
            preflight_sha256=lease.preflight_report_sha256,
            case_execution_lease_sha256=case_execution_lease_sha256(lease),
            stage_sha256=self.openai_stage_sha256,
            case_id=lease.case_id,
            logical_call_id=logical_call_id,
            actor_request_sha256=lease.request_sha256,
            request_sha256=trusted_request.request_sha256,
            transport_binding_sha256=_require_sha256(
                transport_binding_sha256, "transport_binding_sha256"
            ),
            pricing_binding_sha256=lease.pricing_binding_sha256,
            deadline_monotonic_ns=effective_deadline_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            max_output_tokens=self._stage.max_output_tokens,
        )
        started_ns = time.monotonic_ns()
        reserved_cost = live_attempt_worst_case_cost_usd_micros(
            self._pricing,
            request_byte_count=trusted_request.byte_count,
            max_output_tokens=authority.max_output_tokens,
        )
        self._sink._reserve(authority)
        if deadline_binding is not None:
            with self._constraint_lock:
                if (
                    attempt_id in self._attempt_deadline_bindings
                    or attempt_id in self._attempt_requests
                ):
                    raise LiveAttemptError(
                        "DUPLICATE_ATTEMPT_ID",
                        "history attempt proof was already retained",
                    )
                self._attempt_deadline_bindings[attempt_id] = (
                    snapshot_live_attempt_deadline_binding(deadline_binding)
                )
                self._attempt_requests[attempt_id] = snapshot_canonical_history_policy_request(
                    trusted_request
                )
        if started_ns >= authority.deadline_monotonic_ns:
            self._sink._commit(
                LiveAttemptReceiptV1(
                    attempt_id=authority.attempt_id,
                    role=authority.role,
                    authority_sha256=live_attempt_authority_sha256(authority),
                    manifest_sha256=authority.manifest_sha256,
                    preflight_sha256=authority.preflight_sha256,
                    case_execution_lease_sha256=authority.case_execution_lease_sha256,
                    stage_sha256=authority.stage_sha256,
                    case_id=authority.case_id,
                    logical_call_id=authority.logical_call_id,
                    actor_request_sha256=authority.actor_request_sha256,
                    request_sha256=authority.request_sha256,
                    transport_binding_sha256=authority.transport_binding_sha256,
                    pricing_binding_sha256=authority.pricing_binding_sha256,
                    execution_kind=(LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS),
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=0,
                    response_envelope_sha256=None,
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_status=LiveAttemptCostStatusV1.EXACT,
                    cost_usd_micros=0,
                    cancellation_requested=False,
                    termination=LiveAttemptTerminationV1.NONE,
                    worker_pid=None,
                    worker_exit_code=None,
                    worker_reaped=False,
                    late_output_detected=False,
                    duration_ns=min(
                        _MAX_DURATION_NS,
                        max(0, time.monotonic_ns() - started_ns),
                    ),
                    failure_code="ATTEMPT_DEADLINE_ELAPSED",
                )
            )
            raise LiveAttemptError("ATTEMPT_DEADLINE_ELAPSED", "attempt deadline elapsed")
        if reserved_cost > authority.max_cost_usd_micros:
            self._sink._commit(
                LiveAttemptReceiptV1(
                    attempt_id=authority.attempt_id,
                    role=authority.role,
                    authority_sha256=live_attempt_authority_sha256(authority),
                    manifest_sha256=authority.manifest_sha256,
                    preflight_sha256=authority.preflight_sha256,
                    case_execution_lease_sha256=authority.case_execution_lease_sha256,
                    stage_sha256=authority.stage_sha256,
                    case_id=authority.case_id,
                    logical_call_id=authority.logical_call_id,
                    actor_request_sha256=authority.actor_request_sha256,
                    request_sha256=authority.request_sha256,
                    transport_binding_sha256=authority.transport_binding_sha256,
                    pricing_binding_sha256=authority.pricing_binding_sha256,
                    execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=0,
                    response_envelope_sha256=None,
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_status=LiveAttemptCostStatusV1.EXACT,
                    cost_usd_micros=0,
                    cancellation_requested=False,
                    termination=LiveAttemptTerminationV1.NONE,
                    worker_pid=None,
                    worker_exit_code=None,
                    worker_reaped=False,
                    late_output_detected=False,
                    duration_ns=min(
                        _MAX_DURATION_NS,
                        max(0, time.monotonic_ns() - started_ns),
                    ),
                    failure_code="ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY",
                )
            )
            raise LiveAttemptError(
                "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY",
                "worst-case request cost exceeds attempt authority",
            )
        # Production attempts may be prepared from the Sentinel worker thread.
        # A clean spawned interpreter avoids inheriting unrelated thread locks,
        # clients, loggers, or allocator state as POSIX ``fork`` would.
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_production_openai_attempt_worker,
                args=(
                    child_connection,
                    self._factory,
                    lease,
                    trusted_request.canonical_bytes,
                    live_attempt_authority_sha256(authority),
                    authority.stage_sha256,
                    self._role.value,
                ),
                name="mobileworld-r24-openai-attempt",
                daemon=False,
            ),
        )
        try:
            process.start()
            child_connection.close()
        except Exception as exc:
            parent_connection.close()
            child_connection.close()
            self._sink._commit(
                LiveAttemptReceiptV1(
                    attempt_id=authority.attempt_id,
                    role=authority.role,
                    authority_sha256=live_attempt_authority_sha256(authority),
                    manifest_sha256=authority.manifest_sha256,
                    preflight_sha256=authority.preflight_sha256,
                    case_execution_lease_sha256=authority.case_execution_lease_sha256,
                    stage_sha256=authority.stage_sha256,
                    case_id=authority.case_id,
                    logical_call_id=authority.logical_call_id,
                    actor_request_sha256=authority.actor_request_sha256,
                    request_sha256=authority.request_sha256,
                    transport_binding_sha256=authority.transport_binding_sha256,
                    pricing_binding_sha256=authority.pricing_binding_sha256,
                    execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=0,
                    response_envelope_sha256=None,
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_status=LiveAttemptCostStatusV1.EXACT,
                    cost_usd_micros=0,
                    cancellation_requested=False,
                    termination=LiveAttemptTerminationV1.COOPERATIVE,
                    worker_pid=None,
                    worker_exit_code=0,
                    worker_reaped=True,
                    late_output_detected=False,
                    duration_ns=min(
                        _MAX_DURATION_NS,
                        max(0, time.monotonic_ns() - started_ns),
                    ),
                    failure_code="PROVIDER_CHILD_START_FAILED",
                )
            )
            raise LiveAttemptError(
                "PROVIDER_CHILD_START_FAILED", "provider child failed to start"
            ) from exc
        call = ProductionOpenAIAttemptCallV1(
            authority=authority,
            sink=self._sink,
            pricing=self._pricing,
            process=process,
            connection=parent_connection,
            started_ns=started_ns,
            cancel_grace_seconds=self._cancel_grace_seconds,
            execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
            request=trusted_request,
            stage=self._stage,
        )
        with self._constraint_lock:
            self._attempt_calls[attempt_id] = call
        ready_deadline_ns = min(
            authority.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        ready = call._receive(max(0, ready_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if ready != ("READY", live_attempt_authority_sha256(authority)):
            call._failed("PROVIDER_CHILD_READY_FAILED")
            raise LiveAttemptError("PROVIDER_CHILD_READY_FAILED", "provider child is not ready")
        return call


# Backward-compatible semantic names for the only currently seam-wired role.
# They are aliases, not a second implementation or activation gate.
ProductionHistoryPolicyAttemptCallV1 = ProductionOpenAIAttemptCallV1
ProductionHistoryPolicyAttemptRunnerV1 = ProductionOpenAIAttemptRunnerV1


class CpuFixedCancellableAttemptRunnerV1:
    """CPU-only constructor for exercising the exact seam cancellation call."""

    def __init__(
        self,
        *,
        sink: MemoryLiveAttemptReceiptSinkV1,
        startup_timeout_ms: int = 1_000,
        cancel_grace_ms: int = 100,
    ) -> None:
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "CPU runner requires the exact sink")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 10_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 10_000)
        self._sink = sink
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000

    def begin(
        self,
        authority: LiveAttemptAuthorityV1,
        *,
        confirmed_authority_sha256: str,
        script: CpuFixedAttemptScriptV1,
    ) -> ProductionOpenAIAttemptCallV1:
        trusted = snapshot_live_attempt_authority(authority)
        actual = live_attempt_authority_sha256(trusted)
        if _require_sha256(confirmed_authority_sha256, "confirmed_authority_sha256") != actual:
            raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "confirmed authority hash differs")
        if (
            script
            not in {
                CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH,
                CpuFixedAttemptScriptV1.IGNORE_TERM_AFTER_DISPATCH,
            }
            or type(script) is not CpuFixedAttemptScriptV1
        ):
            raise LiveAttemptError(
                "UNTRUSTED_CPU_SCRIPT", "cancellable CPU call requires a blocking script"
            )
        self._sink._reserve(trusted)
        context = multiprocessing.get_context("fork")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_cpu_fixed_attempt_worker,
                args=(child_connection, script.value, actual, trusted.request_sha256),
                name="mobileworld-r24-fixed-cancellable-attempt",
                daemon=False,
            ),
        )
        process.start()
        child_connection.close()
        pricing = LiveAttemptPricingV1(
            pricing_id="cpu-fixed-test-price",
            model="gpt-5.6-sol",
            input_usd_micros_per_million_tokens=0,
            cached_input_usd_micros_per_million_tokens=0,
            output_usd_micros_per_million_tokens=0,
            source_sha256=_canonical_sha256({"cpu_fixed": True}),
            effective_at_utc="2026-01-01T00:00:00Z",
        )
        call = ProductionOpenAIAttemptCallV1(
            authority=trusted,
            sink=self._sink,
            pricing=pricing,
            process=process,
            connection=parent_connection,
            started_ns=time.monotonic_ns(),
            cancel_grace_seconds=self._cancel_grace_seconds,
            execution_kind=LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS,
        )
        ready_deadline_ns = min(
            trusted.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        ready = call._receive(max(0, ready_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if ready != ("READY", actual):
            call._failed("CPU_WORKER_READY_FAILED")
            raise LiveAttemptError("CPU_WORKER_READY_FAILED", "CPU worker did not become ready")
        return call


def production_live_attempt_runner_available_v1() -> bool:
    from mobile_world.runtime.sentinel.r2_4.production_preflight import (
        production_activation_available_v1,
    )

    available = production_activation_available_v1()
    if type(available) is not bool:
        raise LiveAttemptError("INVALID_ACTIVATION_STATE", "activation state is untrusted")
    return available


__all__ = [
    "CpuFixedAttemptScriptV1",
    "CpuFixedLiveAttemptHandleV1",
    "CpuFixedLiveAttemptRunnerV1",
    "CpuFixedCancellableAttemptRunnerV1",
    "CanonicalHistoryPolicyRequestV1",
    "CanonicalOpenAIRequestV1",
    "HISTORY_POLICY_REQUEST_SCHEMA_VERSION",
    "OPENAI_REQUEST_SCHEMA_VERSION",
    "PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1",
    "PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1",
    "PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1",
    "LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION",
    "LIVE_ATTEMPT_DEADLINE_BINDING_SCHEMA_VERSION",
    "LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION",
    "LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "LIVE_ATTEMPT_PRICING_SCHEMA_VERSION",
    "LiveAttemptAuthorityV1",
    "LiveAttemptCostStatusV1",
    "LiveAttemptDeadlineBindingV1",
    "LiveAttemptError",
    "LiveAttemptExecutionKindV1",
    "LiveAttemptPricingV1",
    "LiveAttemptReceiptV1",
    "LiveAttemptRoleV1",
    "LiveAttemptStatusV1",
    "LiveAttemptTerminationV1",
    "MemoryLiveAttemptReceiptSinkV1",
    "ProductionHistoryPolicyAttemptCallV1",
    "ProductionHistoryPolicyAttemptRunnerV1",
    "ProductionOpenAIAttemptCallV1",
    "ProductionOpenAIAttemptRunnerV1",
    "live_attempt_authority_projection",
    "live_attempt_authority_sha256",
    "live_attempt_cost_usd_micros",
    "live_attempt_deadline_binding_projection",
    "live_attempt_worst_case_cost_usd_micros",
    "live_attempt_pricing_projection",
    "live_attempt_pricing_sha256",
    "live_attempt_receipt_projection",
    "live_attempt_receipt_root_sha256",
    "live_attempt_receipt_sha256",
    "production_live_attempt_runner_available_v1",
    "production_attempt_termination_upper_bound_ns_v1",
    "parse_live_attempt_authority_projection",
    "parse_live_attempt_deadline_binding_projection",
    "build_canonical_history_policy_request",
    "build_canonical_openai_request",
    "snapshot_canonical_openai_request",
    "snapshot_canonical_history_policy_request",
    "snapshot_live_attempt_authority",
    "snapshot_live_attempt_deadline_binding",
    "snapshot_live_attempt_pricing",
    "snapshot_live_attempt_receipt",
]
