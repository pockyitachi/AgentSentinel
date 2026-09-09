"""History-free rubric backend for ordinary audited ``mw eval`` runs.

This module is intentionally small and task-local.  It binds the current
Collector projection, asks one retry-disabled OpenAI Responses client to
generate/track the R2.3 rubric, validates strict structured output, and returns
the existing R2.3 contract objects.  It has no pilot authority, preflight,
process census, pricing gate, model snapshot, or cleanup lifecycle.

Importing or constructing these classes never calls a provider.  A call is
made only by :class:`RubricTaskSession` during an actor request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openai import OpenAI
from PIL import Image

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    SUPPORTED_OPENAI_SDK_VERSION,
    _project_openai_response,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    GateOperator,
    GateV1,
    GraphRefKind,
    GraphRefV1,
    InstructionSpanRole,
    InstructionSpanV1,
    MilestoneEvidenceRefV1,
    MilestoneEvidenceRelation,
    MilestoneKind,
    MilestonePredicateKind,
    MilestoneReasonCode,
    MilestoneState,
    MilestoneStateRecordV1,
    MilestoneV1,
    MultiPathRubricV1,
    PathKind,
    R23ContractError,
    RevisionKind,
    RevisionReason,
    RubricBackendDescriptorV1,
    RubricBindingV1,
    RubricPathV1,
    RubricRevisionRequestV1,
    RubricRevisionV1,
    RubricTrackerProposalV1,
    RubricTrackingPacketV1,
    TaskStartRubricRequestV1,
    TrackerProposalStatus,
    rubric_tracking_state_sha256,
    snapshot_backend_descriptor,
    task_start_request_projection,
    tracking_packet_projection,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_4.evidence import (
    rubric_evidence_snapshot_sha256,
)
from mobile_world.runtime.sentinel.schemas import read_sentinel_schema_bytes

LEAN_RUBRIC_BACKEND_VERSION = "r2.4-v1"
LEAN_RUBRIC_MODEL = "gpt-5.6-luna"
LEAN_RUBRIC_REASONING_EFFORT = "low"
LEAN_RUBRIC_MAX_OUTPUT_TOKENS = 8192
LEAN_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-lean-rubric-generate-input/v1"
)
LEAN_RUBRIC_TRACK_INPUT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-lean-rubric-track-input/v1"
)

_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_DATA_IMAGE: Final[re.Pattern[str]] = re.compile(
    r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})"
)
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

_GENERATE_INSTRUCTIONS = """You are the isolated MobileWorld task-rubric generator. Convert only the exact task instruction into a complete multi-path AND/OR milestone graph. For each instruction span, copy exact_text byte-for-byte and give only its zero-based Unicode char_start; the runtime derives all end and UTF-8 offsets. Every instruction span must be cited by exactly one instruction-bound milestone whose kind matches the span role, whose predicate_kind is INSTRUCTION_REQUIREMENT, and whose state_description exactly equals the span exact_text. Include every hard requirement, constraint, and terminal requirement without adding requirements. Every graph reference must resolve, every milestone and gate must be reachable, gates must be acyclic with at least two distinct children, and IDs must be unique. Include at least one LEGAL_ALTERNATIVE path with a non-null root and exactly one OTHER_UNKNOWN path with a null root. Use common_root only for requirements shared by every legal alternative. Do not infer factual truth, inspect history, recommend actions, or emit GUI action coordinates/tool calls. Return only JSON matching the supplied schema."""

_TRACK_INSTRUCTIONS = """You are the isolated MobileWorld rubric tracker. Evaluate every frozen milestone only from the supplied history-free packet and the current screenshot. Actor history, History IR, policy output, future events, benchmark checker results, and replay outcomes are absent and forbidden. Generic transition success, screenshot change, and free-form tool text are weak evidence and cannot alone establish satisfaction or violation. An unmet but still feasible hard requirement or terminal requirement is pending, in_progress, or unknown; it is not violated merely because it is incomplete. Use VIOLATED only when strong evidence shows that a requirement or constraint was breached or that its route is impossible, never merely unmet. Return exactly one state for every milestone ID. Cite exact evidence IDs; the runtime binds their payload hashes from the tracking packet. A pending state requires empty evidence_refs and NOT_STARTED. A satisfied state requires SUPPORTS_STATE evidence and a matching support reason; a violated state requires REFUTES_STATE evidence and a matching refutation reason; and an in_progress state requires OBSERVES_PROGRESS evidence and PROGRESS_OBSERVED. On ambiguity, conflict, or insufficiency use unknown with an uncertainty reason; ABSTAIN requires every milestone unknown. Do not recommend or execute an action. Return only JSON matching the supplied schema."""


class LeanRubricError(RuntimeError):
    """Stable failure at the lean rubric boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class LeanRubricOperationV1(StrEnum):
    GENERATE = "GENERATE"
    TRACK = "TRACK"


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LeanRubricError("INVALID_SHA256", f"{name} is not lowercase SHA-256")
    return value


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LeanRubricError("INVALID_ID", f"{name} is invalid")
    return value


def _strict_json_object(raw: bytes | str) -> dict[str, JsonValue]:
    if type(raw) is str:
        try:
            payload = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise LeanRubricError("INVALID_PROVIDER_JSON", "output is not UTF-8") from exc
    elif type(raw) is bytes:
        payload = raw
    else:
        raise LeanRubricError("INVALID_PROVIDER_JSON", "output must be bytes or text")
    if not payload or len(payload) > _MAX_OUTPUT_BYTES:
        raise LeanRubricError("INVALID_PROVIDER_JSON", "output byte count is outside bounds")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise LeanRubricError("DUPLICATE_JSON_KEY", "output repeats an object key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=no_duplicates)
    except LeanRubricError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LeanRubricError("INVALID_PROVIDER_JSON", "output is not strict JSON") from exc
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise LeanRubricError("INVALID_PROVIDER_JSON", "output root must be an object")
    try:
        canonical_json_bytes(cast(JsonValue, value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LeanRubricError("INVALID_PROVIDER_JSON", "output is not finite JSON") from exc
    return cast(dict[str, JsonValue], value)


def _require_closed_object_schemas(value: JsonValue) -> None:
    if type(value) is list:
        for item in value:
            _require_closed_object_schemas(item)
        return
    if type(value) is not dict:
        return
    if value.get("type") == "object":
        properties = value.get("properties")
        required = value.get("required")
        if (
            value.get("additionalProperties") is not False
            or type(properties) is not dict
            or type(required) is not list
            or set(properties) != set(required)
        ):
            raise LeanRubricError(
                "NON_STRICT_SCHEMA", "every object schema must close and require all fields"
            )
    for child in value.values():
        _require_closed_object_schemas(child)


@dataclass(frozen=True, slots=True)
class LeanRubricSchemaSnapshotV1:
    name: str
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        _require_id(self.name, "schema name")
        if type(self.canonical_bytes) is not bytes:
            raise LeanRubricError("UNTRUSTED_SCHEMA", "schema bytes are mutable")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != _require_sha256(
            self.sha256, "schema sha256"
        ):
            raise LeanRubricError("SCHEMA_HASH_DRIFT", "schema hash differs from bytes")
        schema = _strict_json_object(self.canonical_bytes)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise LeanRubricError("INVALID_SCHEMA", "structured output schema is invalid") from exc
        _require_closed_object_schemas(schema)

    @classmethod
    def from_bytes(cls, *, name: str, raw: bytes) -> LeanRubricSchemaSnapshotV1:
        if type(raw) is not bytes:
            raise LeanRubricError("UNTRUSTED_SCHEMA", "schema source must use immutable bytes")
        value = _strict_json_object(raw)
        canonical = canonical_json_bytes(cast(JsonValue, value))
        return cls(
            name=name,
            canonical_bytes=canonical,
            sha256=hashlib.sha256(canonical).hexdigest(),
        )

    def as_dict(self) -> dict[str, JsonValue]:
        return _strict_json_object(bytes(self.canonical_bytes))


def lean_rubric_generate_schema() -> LeanRubricSchemaSnapshotV1:
    return LeanRubricSchemaSnapshotV1.from_bytes(
        name="r24_live_rubric_generate_v1",
        raw=read_sentinel_schema_bytes("r2_4", "rubric_generate_output.v1.schema.json"),
    )


def lean_rubric_track_schema() -> LeanRubricSchemaSnapshotV1:
    return LeanRubricSchemaSnapshotV1.from_bytes(
        name="r24_live_rubric_track_v1",
        raw=read_sentinel_schema_bytes("r2_4", "rubric_track_output.v1.schema.json"),
    )


def lean_rubric_prompt_bundle_sha256() -> str:
    return canonical_sha256(
        cast(
            JsonValue,
            {
                "generate_instructions_sha256": hashlib.sha256(
                    _GENERATE_INSTRUCTIONS.encode("utf-8")
                ).hexdigest(),
                "track_instructions_sha256": hashlib.sha256(
                    _TRACK_INSTRUCTIONS.encode("utf-8")
                ).hexdigest(),
            },
        )
    )


@dataclass(frozen=True, slots=True)
class BoundCollectorCurrentImageV1:
    """Current screenshot bound to one history-free Collector stimulus."""

    task_run_id: str
    logical_call_id: str
    source_event_id: str
    source_event_seq: int
    evidence_id: str
    content_sha256: str
    media_type: str
    width: int
    height: int
    data_url: str = field(repr=False)
    stimulus_sha256: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.task_run_id, "task_run_id"),
            (self.logical_call_id, "logical_call_id"),
            (self.source_event_id, "source_event_id"),
            (self.evidence_id, "evidence_id"),
        ):
            _require_id(value, name)
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            raise LeanRubricError("INVALID_IMAGE_BINDING", "source event sequence is invalid")
        _require_sha256(self.content_sha256, "content_sha256")
        _require_sha256(self.stimulus_sha256, "stimulus_sha256")
        if self.media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise LeanRubricError("INVALID_IMAGE_BINDING", "image media type is unsupported")
        if any(
            type(value) is not int or not 1 <= value <= 32768 for value in (self.width, self.height)
        ):
            raise LeanRubricError("INVALID_IMAGE_BINDING", "image dimensions are invalid")
        image_bytes, media_type = _decode_data_image(self.data_url)
        if (
            media_type != self.media_type
            or hashlib.sha256(image_bytes).hexdigest() != self.content_sha256
        ):
            raise LeanRubricError("IMAGE_HASH_DRIFT", "current image bytes differ from evidence")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                size = image.size
        except Exception as exc:
            raise LeanRubricError("INVALID_IMAGE_BYTES", "current image cannot be decoded") from exc
        if size != (self.width, self.height):
            raise LeanRubricError("IMAGE_DIMENSION_DRIFT", "image dimensions differ from evidence")

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(
            cast(
                JsonValue,
                {
                    "content_sha256": self.content_sha256,
                    "evidence_id": self.evidence_id,
                    "height": self.height,
                    "logical_call_id": self.logical_call_id,
                    "media_type": self.media_type,
                    "source_event_id": self.source_event_id,
                    "source_event_seq": self.source_event_seq,
                    "stimulus_sha256": self.stimulus_sha256,
                    "task_run_id": self.task_run_id,
                    "width": self.width,
                },
            )
        )


def _decode_data_image(value: object) -> tuple[bytes, str]:
    if type(value) is not str or len(value) > _MAX_IMAGE_BYTES * 4 // 3 + 128:
        raise LeanRubricError("INVALID_IMAGE_DATA_URL", "image data URL is invalid")
    match = _DATA_IMAGE.fullmatch(value)
    if match is None:
        raise LeanRubricError("INVALID_IMAGE_DATA_URL", "image data URL is unsupported")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LeanRubricError("INVALID_IMAGE_DATA_URL", "image base64 is malformed") from exc
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise LeanRubricError("INVALID_IMAGE_DATA_URL", "image byte count is outside bounds")
    return raw, match.group(1)


def _snapshot_stimulus(value: RubricEvidenceSnapshotV1) -> RubricEvidenceSnapshotV1:
    if type(value) is not RubricEvidenceSnapshotV1:
        raise LeanRubricError("UNTRUSTED_COLLECTOR_STIMULUS", "rubric stimulus type differs")
    try:
        detached = deepcopy(value)
        if type(detached) is not RubricEvidenceSnapshotV1 or rubric_evidence_snapshot_sha256(
            detached
        ) != rubric_evidence_snapshot_sha256(value):
            raise TypeError("stimulus detach changed identity")
    except Exception as exc:
        raise LeanRubricError(
            "UNTRUSTED_COLLECTOR_STIMULUS", "rubric stimulus could not be detached"
        ) from exc
    return detached


def bind_current_collector_image_projection(
    *,
    stimulus: RubricEvidenceSnapshotV1,
    current_image_data_url: str,
    current_image_sha256: str,
    logical_call_id: str,
) -> BoundCollectorCurrentImageV1:
    """Bind only the history-free Coordinator projection and current pixels."""

    trusted_stimulus = _snapshot_stimulus(stimulus)
    _require_sha256(current_image_sha256, "current_image_sha256")
    _require_id(logical_call_id, "logical_call_id")
    current = trusted_stimulus.current_observation
    matches = tuple(
        item
        for item in trusted_stimulus.evidence_index
        if item.evidence_id == current.screenshot_evidence_id
    )
    if len(matches) != 1:
        raise LeanRubricError("CURRENT_IMAGE_NOT_UNIQUE", "current screenshot evidence differs")
    evidence = matches[0]
    from mobile_world.runtime.sentinel.r2_3.contracts import ImageEvidenceProjectionV1

    projection = evidence.projection
    if (
        type(projection) is not ImageEvidenceProjectionV1
        or evidence.source_event_id != current.source_event_id
        or evidence.source_event_seq != current.source_event_seq
        or projection.content_sha256 != current.screenshot_content_sha256
        or current_image_sha256 != projection.content_sha256
    ):
        raise LeanRubricError("CURRENT_IMAGE_BINDING_MISMATCH", "current image evidence drifted")
    return BoundCollectorCurrentImageV1(
        task_run_id=trusted_stimulus.task_run_id,
        logical_call_id=logical_call_id,
        source_event_id=current.source_event_id,
        source_event_seq=current.source_event_seq,
        evidence_id=current.screenshot_evidence_id,
        content_sha256=projection.content_sha256,
        media_type=projection.media_type.value,
        width=projection.width,
        height=projection.height,
        data_url=current_image_data_url,
        stimulus_sha256=rubric_evidence_snapshot_sha256(trusted_stimulus),
    )


@dataclass(frozen=True, slots=True)
class _RubricCallContextV1:
    logical_call_id: str
    task_run_id: str
    actor_request_sha256: str
    deadline_monotonic_ns: int
    stimulus: RubricEvidenceSnapshotV1
    image: BoundCollectorCurrentImageV1

    def __post_init__(self) -> None:
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.task_run_id, "task_run_id")
        _require_sha256(self.actor_request_sha256, "actor_request_sha256")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns < 1:
            raise LeanRubricError("INVALID_DEADLINE", "rubric deadline is invalid")
        trusted_stimulus = _snapshot_stimulus(self.stimulus)
        if (
            type(self.image) is not BoundCollectorCurrentImageV1
            or self.image.logical_call_id != self.logical_call_id
            or self.image.task_run_id != self.task_run_id
            or trusted_stimulus.task_run_id != self.task_run_id
            or self.image.stimulus_sha256 != rubric_evidence_snapshot_sha256(trusted_stimulus)
        ):
            raise LeanRubricError("IMAGE_CONTEXT_DRIFT", "image belongs to another call")


class _RubricContextStoreV1:
    def __init__(self) -> None:
        self._by_call: dict[str, _RubricCallContextV1] = {}
        self._current_by_task: dict[str, str] = {}
        self._lock = threading.Lock()

    def bind(self, context: _RubricCallContextV1) -> None:
        if type(context) is not _RubricCallContextV1:
            raise LeanRubricError("UNTRUSTED_CONTEXT", "rubric context type differs")
        with self._lock:
            prior = self._by_call.get(context.logical_call_id)
            if prior is not None and prior != context:
                raise LeanRubricError("LOGICAL_CALL_CONTEXT_DRIFT", "rubric context changed")
            self._by_call[context.logical_call_id] = context
            self._current_by_task[context.task_run_id] = context.logical_call_id

    def resolve(
        self,
        *,
        task_run_id: str,
        logical_call_id: str | None,
    ) -> _RubricCallContextV1:
        with self._lock:
            call_id = logical_call_id or self._current_by_task.get(task_run_id)
            value = None if call_id is None else self._by_call.get(call_id)
        if value is None or value.task_run_id != task_run_id:
            raise LeanRubricError(
                "RUBRIC_CALL_CONTEXT_MISSING", "no bound Collector context exists"
            )
        return value


class DirectOpenAIRubricProviderV1:
    """Direct, retry-disabled Responses transport for one eval task."""

    def __init__(self, *, client: OpenAI, timeout_seconds: float) -> None:
        if type(client) is not OpenAI:
            raise TypeError("client must be the exact supported OpenAI SDK client")
        if type(client.max_retries) is not int or client.max_retries != 0:
            raise ValueError("the dedicated OpenAI client must set max_retries=0")
        if type(timeout_seconds) not in {int, float} or isinstance(timeout_seconds, bool):
            raise TypeError("timeout_seconds must be an exact number")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._client = client
        self._timeout_seconds = float(timeout_seconds)
        self._contexts = _RubricContextStoreV1()
        self._called: set[tuple[str, str, LeanRubricOperationV1]] = set()
        self._call_lock = threading.Lock()

    @property
    def config_projection(self) -> dict[str, JsonValue]:
        return {
            "model": LEAN_RUBRIC_MODEL,
            "openai_sdk_version": SUPPORTED_OPENAI_SDK_VERSION,
            "reasoning_effort": LEAN_RUBRIC_REASONING_EFFORT,
            "sdk_max_retries": 0,
            "timeout_ns": round(self._timeout_seconds * 1_000_000_000),
            "transport": "DIRECT_OPENAI_RESPONSES",
        }

    def bind_collector_projection(
        self,
        *,
        stimulus: RubricEvidenceSnapshotV1,
        current_image_data_url: str,
        current_image_sha256: str,
        logical_call_id: str,
        actor_request_sha256: str,
    ) -> None:
        image = bind_current_collector_image_projection(
            stimulus=stimulus,
            current_image_data_url=current_image_data_url,
            current_image_sha256=current_image_sha256,
            logical_call_id=logical_call_id,
        )
        self._contexts.bind(
            _RubricCallContextV1(
                logical_call_id=logical_call_id,
                task_run_id=image.task_run_id,
                actor_request_sha256=actor_request_sha256,
                deadline_monotonic_ns=(
                    time.monotonic_ns() + round(self._timeout_seconds * 1_000_000_000)
                ),
                stimulus=_snapshot_stimulus(stimulus),
                image=image,
            )
        )

    def context(
        self,
        *,
        task_run_id: str,
        logical_call_id: str | None,
    ) -> _RubricCallContextV1:
        return self._contexts.resolve(
            task_run_id=task_run_id,
            logical_call_id=logical_call_id,
        )

    def invoke(
        self,
        *,
        operation: LeanRubricOperationV1,
        context: _RubricCallContextV1,
        request_kwargs: dict[str, object],
    ) -> str:
        if type(operation) is not LeanRubricOperationV1:
            raise LeanRubricError("UNTRUSTED_OPERATION", "rubric operation type differs")
        if type(context) is not _RubricCallContextV1:
            raise LeanRubricError("UNTRUSTED_CONTEXT", "rubric context type differs")
        key = (context.task_run_id, context.logical_call_id, operation)
        with self._call_lock:
            if key in self._called:
                raise LeanRubricError("DUPLICATE_PROVIDER_CALL", "rubric call repeated")
            self._called.add(key)
        remaining_seconds = (context.deadline_monotonic_ns - time.monotonic_ns()) / 1e9
        if remaining_seconds <= 0:
            raise LeanRubricError("RUBRIC_TIMEOUT", "rubric deadline elapsed")
        raw = self._client.responses.create(
            **request_kwargs,
            timeout=min(self._timeout_seconds, remaining_seconds),
        )
        return _project_openai_response(
            raw,
            requested_model=LEAN_RUBRIC_MODEL,
        ).output_text


class LeanOpenAIRubricBackendV1:
    """R2.3 builder/tracker backend backed by the direct eval provider."""

    def __init__(self, *, provider: DirectOpenAIRubricProviderV1) -> None:
        if type(provider) is not DirectOpenAIRubricProviderV1:
            raise TypeError("provider must be the exact direct rubric provider")
        self._provider = provider
        self._generate_schema = lean_rubric_generate_schema()
        self._track_schema = lean_rubric_track_schema()
        rubric_schema_sha256 = hashlib.sha256(
            read_sentinel_schema_bytes("r2_3", "rubric.v1.schema.json")
        ).hexdigest()
        tracking_packet_schema_sha256 = hashlib.sha256(
            read_sentinel_schema_bytes("r2_3", "tracking_packet.v1.schema.json")
        ).hexdigest()
        tracker_schema_sha256 = hashlib.sha256(
            read_sentinel_schema_bytes("r2_3", "tracker_output.v1.schema.json")
        ).hexdigest()
        self._descriptor = RubricBackendDescriptorV1(
            backend_id="r24-r23-lean-rubric-admission-bridge",
            backend_version=LEAN_RUBRIC_BACKEND_VERSION,
            prompt_sha256=lean_rubric_prompt_bundle_sha256(),
            rubric_schema_sha256=rubric_schema_sha256,
            tracking_packet_schema_sha256=tracking_packet_schema_sha256,
            tracker_schema_sha256=tracker_schema_sha256,
            config_sha256=canonical_sha256(
                cast(
                    JsonValue,
                    {
                        "generate_output_schema_sha256": self._generate_schema.sha256,
                        "provider": provider.config_projection,
                        "track_output_schema_sha256": self._track_schema.sha256,
                    },
                )
            ),
        )

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        return snapshot_backend_descriptor(self._descriptor)

    def bind_collector_projection(
        self,
        *,
        stimulus: RubricEvidenceSnapshotV1,
        current_image_data_url: str,
        current_image_sha256: str,
        logical_call_id: str,
        actor_request_sha256: str,
    ) -> None:
        self._provider.bind_collector_projection(
            stimulus=stimulus,
            current_image_data_url=current_image_data_url,
            current_image_sha256=current_image_sha256,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
        )

    @staticmethod
    def _responses_kwargs(
        *,
        prompt: str,
        input_content: list[dict[str, object]],
        schema: LeanRubricSchemaSnapshotV1,
    ) -> dict[str, object]:
        return {
            "model": LEAN_RUBRIC_MODEL,
            "instructions": prompt,
            "input": [{"role": "user", "content": input_content}],
            "reasoning": {"effort": LEAN_RUBRIC_REASONING_EFFORT},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.name,
                    "strict": True,
                    "schema": schema.as_dict(),
                },
                "verbosity": "low",
            },
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
            "store": False,
            "stream": False,
            "truncation": "disabled",
            "max_output_tokens": LEAN_RUBRIC_MAX_OUTPUT_TOKENS,
        }

    def _invoke(
        self,
        *,
        operation: LeanRubricOperationV1,
        context: _RubricCallContextV1,
        provider_input: dict[str, JsonValue],
        prompt: str,
        output_schema: LeanRubricSchemaSnapshotV1,
        include_image: bool,
    ) -> dict[str, JsonValue]:
        input_content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": canonical_json_bytes(cast(JsonValue, provider_input)).decode("utf-8"),
            }
        ]
        if include_image:
            input_content.append(
                {
                    "type": "input_image",
                    "image_url": context.image.data_url,
                    "detail": "high",
                }
            )
        output = self._provider.invoke(
            operation=operation,
            context=context,
            request_kwargs=self._responses_kwargs(
                prompt=prompt,
                input_content=input_content,
                schema=output_schema,
            ),
        )
        parsed = _strict_json_object(output)
        if tuple(Draft202012Validator(output_schema.as_dict()).iter_errors(parsed)):
            raise LeanRubricError("PROVIDER_SCHEMA_REJECTED", "provider output violates schema")
        return parsed

    def generate(self, request: TaskStartRubricRequestV1) -> MultiPathRubricV1:
        if type(request) is not TaskStartRubricRequestV1 or request.backend != self._descriptor:
            raise LeanRubricError("TASK_START_BINDING_MISMATCH", "task-start request differs")
        context = self._provider.context(
            task_run_id=request.task_run_id,
            logical_call_id=None,
        )
        if request.task != context.stimulus.task:
            raise LeanRubricError(
                "TASK_START_BINDING_MISMATCH", "task-start request differs from Collector"
            )
        input_value = cast(
            dict[str, JsonValue],
            {
                "request": task_start_request_projection(request),
                "schema_version": LEAN_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
            },
        )
        parsed = self._invoke(
            operation=LeanRubricOperationV1.GENERATE,
            context=context,
            provider_input=input_value,
            prompt=_GENERATE_INSTRUCTIONS,
            output_schema=self._generate_schema,
            include_image=False,
        )
        return _parse_generated_rubric(
            parsed,
            request=request,
            descriptor=self._descriptor,
        )

    def revise(self, request: RubricRevisionRequestV1) -> MultiPathRubricV1:
        del request
        raise R23ContractError(
            "LEAN_REVISION_UNSUPPORTED",
            "ordinary eval generates one rubric and does not revise it mid-task",
        )

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1:
        if type(packet) is not RubricTrackingPacketV1:
            raise LeanRubricError("UNTRUSTED_TRACKING_PACKET", "tracking packet type differs")
        context = self._provider.context(
            task_run_id=packet.task_run_id,
            logical_call_id=packet.logical_call_id,
        )
        image = context.image
        if (
            packet.current_observation.source_event_id != image.source_event_id
            or packet.current_observation.source_event_seq != image.source_event_seq
            or packet.current_observation.screenshot_evidence_id != image.evidence_id
            or packet.current_observation.screenshot_content_sha256 != image.content_sha256
        ):
            raise LeanRubricError("CURRENT_IMAGE_BINDING_MISMATCH", "tracking packet image differs")
        input_value = cast(
            dict[str, JsonValue],
            {
                "current_image_binding_sha256": image.binding_sha256,
                "packet": tracking_packet_projection(packet),
                "schema_version": LEAN_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
            },
        )
        parsed = self._invoke(
            operation=LeanRubricOperationV1.TRACK,
            context=context,
            provider_input=input_value,
            prompt=_TRACK_INSTRUCTIONS,
            output_schema=self._track_schema,
            include_image=True,
        )
        return _parse_tracker_proposal(parsed, packet=packet)


def _mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise LeanRubricError("PROVIDER_SCHEMA_REJECTED", f"{name} must be an object")
    return value


def _sequence(value: JsonValue, name: str) -> list[JsonValue]:
    if type(value) is not list:
        raise LeanRubricError("PROVIDER_SCHEMA_REJECTED", f"{name} must be an array")
    return value


def _graph_ref(value: JsonValue) -> GraphRefV1:
    item = _mapping(value, "graph reference")
    return GraphRefV1(
        ref_kind=GraphRefKind(cast(str, item["ref_kind"])),
        ref_id=cast(str, item["ref_id"]),
    )


def _parse_instruction_span(value: JsonValue, *, task_text: str) -> InstructionSpanV1:
    item = _mapping(value, "instruction span")
    char_start = cast(int, item["char_start"])
    exact_text = cast(str, item["exact_text"])
    utf8_byte_start = len(task_text[:char_start].encode("utf-8"))
    return InstructionSpanV1(
        span_id=cast(str, item["span_id"]),
        role=InstructionSpanRole(cast(str, item["role"])),
        char_start=char_start,
        char_end=char_start + len(exact_text),
        utf8_byte_start=utf8_byte_start,
        utf8_byte_end=utf8_byte_start + len(exact_text.encode("utf-8")),
        exact_text=exact_text,
        span_sha256=hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
    )


def _parse_generated_rubric(
    value: dict[str, JsonValue],
    *,
    request: TaskStartRubricRequestV1,
    descriptor: RubricBackendDescriptorV1,
) -> MultiPathRubricV1:
    try:
        spans = tuple(
            _parse_instruction_span(raw, task_text=request.task.exact_text)
            for raw in _sequence(value["instruction_spans"], "instruction_spans")
        )
        milestones = tuple(
            MilestoneV1(
                milestone_id=cast(str, item["milestone_id"]),
                kind=MilestoneKind(cast(str, item["kind"])),
                predicate_kind=MilestonePredicateKind(cast(str, item["predicate_kind"])),
                state_description=cast(str, item["state_description"]),
                description_sha256=hashlib.sha256(
                    cast(str, item["state_description"]).encode("utf-8")
                ).hexdigest(),
                instruction_span_id=cast(str | None, item["instruction_span_id"]),
            )
            for item in (
                _mapping(raw, "milestone") for raw in _sequence(value["milestones"], "milestones")
            )
        )
        gates = tuple(
            GateV1(
                gate_id=cast(str, item["gate_id"]),
                operator=GateOperator(cast(str, item["operator"])),
                children=tuple(
                    _graph_ref(child) for child in _sequence(item["children"], "gate children")
                ),
            )
            for item in (_mapping(raw, "gate") for raw in _sequence(value["gates"], "gates"))
        )
        paths = tuple(
            RubricPathV1(
                path_id=cast(str, item["path_id"]),
                kind=PathKind(cast(str, item["kind"])),
                root=None if item["root"] is None else _graph_ref(item["root"]),
            )
            for item in (_mapping(raw, "path") for raw in _sequence(value["paths"], "paths"))
        )
        output_sha256 = canonical_sha256(cast(JsonValue, value))
        rubric_id = (
            "r24-rubric-"
            + hashlib.sha256((request.task_run_id + output_sha256).encode("utf-8")).hexdigest()[:32]
        )
        return MultiPathRubricV1(
            rubric_id=rubric_id,
            task_run_id=request.task_run_id,
            rubric_version=1,
            task=request.task,
            revision=RubricRevisionV1(
                revision_id=f"r24-revision-{output_sha256[:32]}",
                revision_event_id=request.task.source_event_id,
                kind=RevisionKind.INITIAL,
                reason=RevisionReason.TASK_START,
                previous_rubric_version=None,
                previous_rubric_sha256=None,
                hard_requirement_deltas=(),
                changed_node_ids=(),
            ),
            instruction_spans=spans,
            milestones=milestones,
            gates=gates,
            common_root=(
                None if value["common_root"] is None else _graph_ref(value["common_root"])
            ),
            paths=paths,
            backend=descriptor,
        )
    except (KeyError, TypeError, ValueError, R23ContractError) as exc:
        raise LeanRubricError(
            "GENERATED_RUBRIC_REJECTED", "generated graph is not admissible"
        ) from exc


def _parse_milestone_evidence_ref(
    value: JsonValue,
    *,
    evidence_hashes: dict[str, str],
) -> MilestoneEvidenceRefV1:
    reference = _mapping(value, "evidence reference")
    evidence_id = cast(str, reference["evidence_id"])
    return MilestoneEvidenceRefV1(
        evidence_id=evidence_id,
        payload_sha256=evidence_hashes[evidence_id],
        relation=MilestoneEvidenceRelation(cast(str, reference["relation"])),
    )


def _parse_milestone_state(
    value: JsonValue,
    *,
    evidence_hashes: dict[str, str],
    prior_states: dict[str, MilestoneStateRecordV1],
) -> MilestoneStateRecordV1:
    item = _mapping(value, "milestone state")
    milestone_id = cast(str, item["milestone_id"])
    state = MilestoneState(cast(str, item["state"]))
    raw_evidence_refs = _sequence(item["evidence_refs"], "evidence_refs")
    reason_code = MilestoneReasonCode(cast(str, item["reason_code"]))
    prior = prior_states.get(milestone_id)
    if prior is not None and prior.state is not MilestoneState.PENDING:
        raw_ref_keys = tuple(
            (
                cast(str, _mapping(raw, "evidence reference")["evidence_id"]),
                cast(str, _mapping(raw, "evidence reference")["relation"]),
            )
            for raw in raw_evidence_refs
        )
        prior_ref_keys = tuple(
            (reference.evidence_id, reference.relation.value) for reference in prior.evidence_refs
        )
        if (
            state is prior.state
            and reason_code in {prior.reason_code, MilestoneReasonCode.PRESERVE_PRIOR_STATE}
            and raw_ref_keys == prior_ref_keys
        ):
            return MilestoneStateRecordV1(
                milestone_id=milestone_id,
                state=prior.state,
                evidence_refs=tuple(
                    MilestoneEvidenceRefV1(
                        evidence_id=reference.evidence_id,
                        payload_sha256=reference.payload_sha256,
                        relation=reference.relation,
                    )
                    for reference in prior.evidence_refs
                ),
                reason_code=MilestoneReasonCode.PRESERVE_PRIOR_STATE,
            )
    evidence_refs = tuple(
        _parse_milestone_evidence_ref(raw, evidence_hashes=evidence_hashes)
        for raw in raw_evidence_refs
    )
    if (
        state is MilestoneState.PENDING
        and not evidence_refs
        and reason_code is MilestoneReasonCode.PRESERVE_PRIOR_STATE
        and prior is not None
        and prior.state is MilestoneState.PENDING
        and not prior.evidence_refs
        and prior.reason_code is MilestoneReasonCode.NOT_STARTED
    ):
        reason_code = MilestoneReasonCode.NOT_STARTED
    return MilestoneStateRecordV1(
        milestone_id=milestone_id,
        state=state,
        evidence_refs=evidence_refs,
        reason_code=reason_code,
    )


def _parse_tracker_proposal(
    value: dict[str, JsonValue],
    *,
    packet: RubricTrackingPacketV1,
) -> RubricTrackerProposalV1:
    try:
        evidence_hashes = {item.evidence_id: item.payload_sha256 for item in packet.evidence_index}
        prior_states = {item.milestone_id: item for item in packet.prior_state.milestone_states}
        milestone_states = tuple(
            _parse_milestone_state(
                raw,
                evidence_hashes=evidence_hashes,
                prior_states=prior_states,
            )
            for raw in _sequence(value["milestone_states"], "milestone_states")
        )
        packet_sha256 = tracking_packet_sha256(packet)
        output_sha256 = canonical_sha256(cast(JsonValue, value))
        return RubricTrackerProposalV1(
            proposal_id=(
                "r24-proposal-"
                + hashlib.sha256((packet_sha256 + output_sha256).encode("utf-8")).hexdigest()[:32]
            ),
            packet_id=packet.packet_id,
            packet_sha256=packet_sha256,
            rubric_binding=RubricBindingV1(
                rubric_id=packet.rubric_binding.rubric_id,
                rubric_version=packet.rubric_binding.rubric_version,
                rubric_sha256=packet.rubric_binding.rubric_sha256,
            ),
            prior_state_sha256=rubric_tracking_state_sha256(packet.prior_state),
            proposal_status=TrackerProposalStatus(cast(str, value["proposal_status"])),
            milestone_states=milestone_states,
        )
    except (KeyError, TypeError, ValueError, R23ContractError) as exc:
        raise LeanRubricError(
            "TRACKER_PROPOSAL_REJECTED", "tracker proposal is not admissible"
        ) from exc


__all__ = [
    "BoundCollectorCurrentImageV1",
    "DirectOpenAIRubricProviderV1",
    "LEAN_RUBRIC_BACKEND_VERSION",
    "LEAN_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION",
    "LEAN_RUBRIC_MODEL",
    "LEAN_RUBRIC_TRACK_INPUT_SCHEMA_VERSION",
    "LeanOpenAIRubricBackendV1",
    "LeanRubricError",
    "LeanRubricOperationV1",
    "LeanRubricSchemaSnapshotV1",
    "bind_current_collector_image_projection",
    "lean_rubric_generate_schema",
    "lean_rubric_prompt_bundle_sha256",
    "lean_rubric_track_schema",
]
