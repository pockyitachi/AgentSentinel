"""Owner-bound live Responses backends for the history-free R2.3 rubric axis.

The provider sees the task instruction at generation and the closed R2.3
tracking packet plus only the current Collector screenshot at tracking.  It
never receives actor history, History IR, a benchmark checker, a future event,
or action authority.  Provider JSON is schema checked and rebuilt into exact
R2.3 dataclasses before the existing session performs semantic admission.

The production port accepts only the module-owned post-preflight attempt
runner for the ``RUBRIC`` role and an exact case execution lease.  The CPU port
is a separate, explicit injected-fake type and has no path to secrets/network.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import threading
from copy import deepcopy
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_2.contracts import PolicyExecutionControlV1
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    ResponsesEnvelopeV1,
    responses_envelope_hash_projection,
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
    RubricPathV1,
    RubricRevisionRequestV1,
    RubricRevisionV1,
    RubricTrackerProposalV1,
    RubricTrackingPacketV1,
    TaskStartRubricRequestV1,
    TrackerProposalStatus,
    rubric_tracking_state_sha256,
    task_start_request_projection,
    tracking_packet_projection,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceBundleV1,
    rubric_evidence_snapshot_projection,
    rubric_evidence_snapshot_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    CanonicalHistoryPolicyRequestV1,
    LiveAttemptCostStatusV1,
    LiveAttemptError,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    ProductionOpenAIAttemptRunnerV1,
    build_canonical_history_policy_request,
    live_attempt_receipt_projection,
    live_attempt_receipt_sha256,
    snapshot_canonical_history_policy_request,
    snapshot_live_attempt_receipt,
)
from mobile_world.runtime.sentinel.r2_4.live_run import OpenAIRoleV1
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CaseExecutionLeaseV1,
    case_execution_lease_sha256,
)

LIVE_RUBRIC_CALL_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-rubric-call-receipt/v1"
)
LIVE_RUBRIC_BACKEND_EXTENSION_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-rubric-backend-extension/v1"
)
LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-rubric-generate-input/v1"
)
LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-rubric-track-input/v1"
)
LIVE_RUBRIC_BACKEND_VERSION = "r2.4-v1"
LIVE_RUBRIC_MODEL = "gpt-5.6-sol"
LIVE_RUBRIC_REASONING_EFFORT = "low"
LIVE_RUBRIC_REQUEST_PROOF_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-rubric-request-proof/v1"
)

_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_DATA_IMAGE: Final[re.Pattern[str]] = re.compile(
    r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})"
)
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_PROVIDER_REQUEST_BYTES = 8 * 1024 * 1024
# Request proofs live only in the owner-restricted audit sink.  They may contain
# the same bounded image more than once (the Collector/image preimage and the
# canonical provider request), so the provider-output limit is not applicable.
_MAX_REQUEST_PROOF_BYTES = 256 * 1024 * 1024
_TRUST_ANCHOR_SEAL: Final[object] = object()

_GENERATE_INSTRUCTIONS = """You are the isolated MobileWorld task-rubric generator. Convert only the exact task instruction into a complete multi-path AND/OR milestone graph. Preserve instruction-bound requirement text byte-for-byte and provide exact Unicode character and UTF-8 byte offsets. Include every hard requirement, constraint, and terminal requirement; model legitimate alternatives explicitly and include exactly one OTHER_UNKNOWN path. Do not infer factual truth, inspect history, recommend actions, emit coordinates/tools, or add requirements. Return only JSON matching the supplied schema."""

_TRACK_INSTRUCTIONS = """You are the isolated MobileWorld rubric tracker. Evaluate every frozen milestone only from the supplied history-free packet and the current screenshot. Actor history, History IR, policy output, future events, benchmark checker results, and replay outcomes are absent and forbidden. Generic transition success, screenshot change, and free-form tool text are weak evidence and cannot alone establish satisfaction or violation. Cite exact evidence IDs and payload hashes. On ambiguity, conflict, or insufficiency use unknown; ABSTAIN requires every milestone unknown. Do not recommend or execute an action. Return only JSON matching the supplied schema."""


def live_rubric_operation_prompt_sha256(operation: LiveRubricOperationV1) -> str:
    """Hash the module-owned prompt bytes for one exact rubric operation."""

    if type(operation) is not LiveRubricOperationV1:
        raise LiveRubricError("UNTRUSTED_OPERATION", "rubric operation type differs")
    prompt = (
        _GENERATE_INSTRUCTIONS
        if operation is LiveRubricOperationV1.GENERATE
        else _TRACK_INSTRUCTIONS
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def live_rubric_prompt_bundle_sha256() -> str:
    """Bind both fixed operation prompts into the compatibility descriptor."""

    return canonical_sha256(
        cast(
            JsonValue,
            {
                "generate_instructions_sha256": live_rubric_operation_prompt_sha256(
                    LiveRubricOperationV1.GENERATE
                ),
                "track_instructions_sha256": live_rubric_operation_prompt_sha256(
                    LiveRubricOperationV1.TRACK
                ),
            },
        )
    )


class LiveRubricError(RuntimeError):
    """Stable failure at the R2.4 live-rubric transport boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class LiveRubricOperationV1(StrEnum):
    GENERATE = "GENERATE"
    TRACK = "TRACK"


class LiveRubricExecutionScopeV1(StrEnum):
    CPU_TEST_LOCAL = "CPU_TEST_LOCAL"
    OWNER_AUTHORIZED_LIVE = "OWNER_AUTHORIZED_LIVE"


class LiveRubricTransportKindV1(StrEnum):
    INJECTED_FAKE = "INJECTED_FAKE"
    OPENAI_RESPONSES = "OPENAI_RESPONSES"


class LiveRubricTransportAuthorityV1(StrEnum):
    CPU_OFFLINE_FAKE = "CPU_OFFLINE_FAKE"
    EXPLICIT_OWNER_AUTHORIZATION = "EXPLICIT_OWNER_AUTHORIZATION"


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LiveRubricError("INVALID_SHA256", f"{name} is not lowercase SHA-256")
    return value


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LiveRubricError("INVALID_ID", f"{name} is invalid")
    return value


def _strict_json_object(raw: bytes | str) -> dict[str, JsonValue]:
    if type(raw) is str:
        try:
            payload = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise LiveRubricError("INVALID_PROVIDER_JSON", "output is not UTF-8") from exc
    elif type(raw) is bytes:
        payload = raw
    else:
        raise LiveRubricError("INVALID_PROVIDER_JSON", "output must be bytes or text")
    if not payload or len(payload) > _MAX_OUTPUT_BYTES:
        raise LiveRubricError("INVALID_PROVIDER_JSON", "output byte count is outside bounds")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise LiveRubricError("DUPLICATE_JSON_KEY", "output repeats an object key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=no_duplicates)
    except LiveRubricError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiveRubricError("INVALID_PROVIDER_JSON", "output is not strict JSON") from exc
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise LiveRubricError("INVALID_PROVIDER_JSON", "output root must be an object")
    try:
        canonical_json_bytes(cast(JsonValue, value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveRubricError("INVALID_PROVIDER_JSON", "output is not finite JSON") from exc
    return cast(dict[str, JsonValue], value)


@dataclass(frozen=True, slots=True)
class LiveRubricSchemaSnapshotV1:
    name: str
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        _require_id(self.name, "schema name")
        if type(self.canonical_bytes) is not bytes:
            raise LiveRubricError("UNTRUSTED_SCHEMA", "schema bytes are mutable")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != _require_sha256(
            self.sha256, "schema sha256"
        ):
            raise LiveRubricError("SCHEMA_HASH_DRIFT", "schema hash differs from bytes")
        schema = _strict_json_object(self.canonical_bytes)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise LiveRubricError("INVALID_SCHEMA", "structured output schema is invalid") from exc
        _require_closed_object_schemas(schema)

    @classmethod
    def from_path(cls, *, name: str, path: Path) -> LiveRubricSchemaSnapshotV1:
        if not path.is_absolute():
            raise LiveRubricError("UNTRUSTED_SCHEMA", "schema path must be absolute")
        value = _strict_json_object(path.read_bytes())
        canonical = canonical_json_bytes(cast(JsonValue, value))
        return cls(
            name=name, canonical_bytes=canonical, sha256=hashlib.sha256(canonical).hexdigest()
        )

    def as_dict(self) -> dict[str, JsonValue]:
        return _strict_json_object(bytes(self.canonical_bytes))


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
            raise LiveRubricError(
                "NON_STRICT_SCHEMA", "every object schema must close and require all fields"
            )
    for child in value.values():
        _require_closed_object_schemas(child)


def _schema_path(filename: str) -> Path:
    return (
        Path(__file__).resolve().parents[6]
        / "mobileworld_audit_handoff"
        / "schemas"
        / "r2_4"
        / filename
    )


def live_rubric_generate_schema() -> LiveRubricSchemaSnapshotV1:
    return LiveRubricSchemaSnapshotV1.from_path(
        name="r24_live_rubric_generate_v1",
        path=_schema_path("rubric_generate_output.v1.schema.json"),
    )


def live_rubric_track_schema() -> LiveRubricSchemaSnapshotV1:
    return LiveRubricSchemaSnapshotV1.from_path(
        name="r24_live_rubric_track_v1",
        path=_schema_path("rubric_track_output.v1.schema.json"),
    )


@dataclass(frozen=True, slots=True)
class BoundCollectorCurrentImageV1:
    """Ephemeral current screenshot proven against one Collector evidence bundle."""

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
            raise LiveRubricError("INVALID_IMAGE_BINDING", "source event sequence is invalid")
        _require_sha256(self.content_sha256, "content_sha256")
        _require_sha256(self.stimulus_sha256, "stimulus_sha256")
        if self.media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise LiveRubricError("INVALID_IMAGE_BINDING", "image media type is unsupported")
        if any(
            type(value) is not int or not 1 <= value <= 32768 for value in (self.width, self.height)
        ):
            raise LiveRubricError("INVALID_IMAGE_BINDING", "image dimensions are invalid")
        image_bytes, media_type = _decode_data_image(self.data_url)
        if (
            media_type != self.media_type
            or hashlib.sha256(image_bytes).hexdigest() != self.content_sha256
        ):
            raise LiveRubricError("IMAGE_HASH_DRIFT", "current image bytes differ from evidence")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                size = image.size
        except Exception as exc:
            raise LiveRubricError("INVALID_IMAGE_BYTES", "current image cannot be decoded") from exc
        if size != (self.width, self.height):
            raise LiveRubricError("IMAGE_DIMENSION_DRIFT", "image dimensions differ from evidence")

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
        raise LiveRubricError("INVALID_IMAGE_DATA_URL", "image data URL is invalid")
    match = _DATA_IMAGE.fullmatch(value)
    if match is None:
        raise LiveRubricError("INVALID_IMAGE_DATA_URL", "image data URL is unsupported")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LiveRubricError("INVALID_IMAGE_DATA_URL", "image base64 is malformed") from exc
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise LiveRubricError("INVALID_IMAGE_DATA_URL", "image byte count is outside bounds")
    return raw, match.group(1)


def bind_current_collector_image(
    bundle: CollectorEvidenceBundleV1,
    *,
    logical_call_id: str,
) -> BoundCollectorCurrentImageV1:
    """Extract only the current screenshot bytes from one trusted Collector bundle."""

    if type(bundle) is not CollectorEvidenceBundleV1:
        raise LiveRubricError("UNTRUSTED_COLLECTOR_BUNDLE", "bundle type differs")
    _require_id(logical_call_id, "logical_call_id")
    return bind_current_collector_image_projection(
        stimulus=bundle.r23_snapshot,
        current_image_data_url=bundle.gpt56_input.current_image_data_url,
        current_image_sha256=bundle.gpt56_input.current_image_sha256,
        logical_call_id=logical_call_id,
    )


def bind_current_collector_image_projection(
    *,
    stimulus: RubricEvidenceSnapshotV1,
    current_image_data_url: str,
    current_image_sha256: str,
    logical_call_id: str,
) -> BoundCollectorCurrentImageV1:
    """Bind the history-free projection emitted by the trusted Coordinator."""

    if type(stimulus) is not RubricEvidenceSnapshotV1:
        raise LiveRubricError("UNTRUSTED_COLLECTOR_STIMULUS", "rubric stimulus type differs")
    _require_sha256(current_image_sha256, "current_image_sha256")
    _require_id(logical_call_id, "logical_call_id")
    current = stimulus.current_observation
    matches = tuple(
        item
        for item in stimulus.evidence_index
        if item.evidence_id == current.screenshot_evidence_id
    )
    if len(matches) != 1:
        raise LiveRubricError("CURRENT_IMAGE_NOT_UNIQUE", "current screenshot evidence differs")
    evidence = matches[0]
    projection = evidence.projection
    from mobile_world.runtime.sentinel.r2_3.contracts import ImageEvidenceProjectionV1

    if (
        type(projection) is not ImageEvidenceProjectionV1
        or evidence.source_event_id != current.source_event_id
        or evidence.source_event_seq != current.source_event_seq
        or projection.content_sha256 != current.screenshot_content_sha256
        or current_image_sha256 != projection.content_sha256
    ):
        raise LiveRubricError("CURRENT_IMAGE_BINDING_MISMATCH", "current image evidence drifted")
    return BoundCollectorCurrentImageV1(
        task_run_id=stimulus.task_run_id,
        logical_call_id=logical_call_id,
        source_event_id=current.source_event_id,
        source_event_seq=current.source_event_seq,
        evidence_id=current.screenshot_evidence_id,
        content_sha256=projection.content_sha256,
        media_type=projection.media_type.value,
        width=projection.width,
        height=projection.height,
        data_url=current_image_data_url,
        stimulus_sha256=rubric_evidence_snapshot_sha256(stimulus),
    )


def _snapshot_rubric_stimulus(
    value: RubricEvidenceSnapshotV1,
) -> RubricEvidenceSnapshotV1:
    if type(value) is not RubricEvidenceSnapshotV1:
        raise LiveRubricError("UNTRUSTED_COLLECTOR_STIMULUS", "rubric stimulus type differs")
    try:
        trusted = deepcopy(value)
        if type(trusted) is not RubricEvidenceSnapshotV1 or rubric_evidence_snapshot_sha256(
            trusted
        ) != rubric_evidence_snapshot_sha256(value):
            raise TypeError("stimulus detach changed identity")
    except Exception as exc:
        raise LiveRubricError(
            "UNTRUSTED_COLLECTOR_STIMULUS", "rubric stimulus could not be detached"
        ) from exc
    return trusted


def _snapshot_bound_current_image(
    value: BoundCollectorCurrentImageV1,
) -> BoundCollectorCurrentImageV1:
    if type(value) is not BoundCollectorCurrentImageV1:
        raise LiveRubricError("UNTRUSTED_IMAGE", "current image type differs")
    return BoundCollectorCurrentImageV1(
        task_run_id=value.task_run_id,
        logical_call_id=value.logical_call_id,
        source_event_id=value.source_event_id,
        source_event_seq=value.source_event_seq,
        evidence_id=value.evidence_id,
        content_sha256=value.content_sha256,
        media_type=value.media_type,
        width=value.width,
        height=value.height,
        data_url=value.data_url,
        stimulus_sha256=value.stimulus_sha256,
    )


def _snapshot_responses_envelope(value: ResponsesEnvelopeV1) -> ResponsesEnvelopeV1:
    if type(value) is not ResponsesEnvelopeV1:
        raise LiveRubricError("UNTRUSTED_RESPONSE_ENVELOPE", "response envelope type differs")
    try:
        return ResponsesEnvelopeV1(
            response_id=value.response_id,
            requested_model=value.requested_model,
            returned_model=value.returned_model,
            status=value.status,
            service_tier=value.service_tier,
            output_text=value.output_text,
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            total_tokens=value.total_tokens,
            schema_version=value.schema_version,
        )
    except (TypeError, ValueError) as exc:
        raise LiveRubricError(
            "UNTRUSTED_RESPONSE_ENVELOPE", "response envelope could not be detached"
        ) from exc


def _snapshot_canonical_object(
    value: object,
    *,
    maximum_bytes: int,
    code: str,
    label: str,
) -> dict[str, JsonValue]:
    """Detach a canonical object under a purpose-specific byte budget."""

    if type(value) is not dict:
        raise LiveRubricError(code, f"{label} must be an exact object")
    try:
        payload = canonical_json_bytes(cast(JsonValue, value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveRubricError(code, f"{label} is not finite canonical JSON") from exc
    if not payload or len(payload) > maximum_bytes:
        raise LiveRubricError(code, f"{label} byte count is outside bounds")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiveRubricError(code, f"{label} is not strict JSON") from exc
    if type(decoded) is not dict:
        raise LiveRubricError(code, f"{label} root must be an object")
    return cast(dict[str, JsonValue], decoded)


def _snapshot_provider_input(value: object) -> dict[str, JsonValue]:
    """Detach one exact provider-input projection through canonical bytes."""

    return _snapshot_canonical_object(
        value,
        maximum_bytes=_MAX_REQUEST_PROOF_BYTES,
        code="UNTRUSTED_PROVIDER_INPUT",
        label="provider input",
    )


def _snapshot_request_proof(value: object) -> dict[str, JsonValue]:
    return _snapshot_canonical_object(
        value,
        maximum_bytes=_MAX_REQUEST_PROOF_BYTES,
        code="INVALID_REQUEST_PROOF",
        label="durable rubric request proof",
    )


def _provider_request_object(raw: object) -> dict[str, JsonValue]:
    """Parse an exact canonical provider request without output-parser limits."""

    if type(raw) is not bytes or not 2 <= len(raw) <= _MAX_PROVIDER_REQUEST_BYTES:
        raise LiveRubricError(
            "UNTRUSTED_PROVIDER_REQUEST", "provider request byte count is outside bounds"
        )
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiveRubricError(
            "UNTRUSTED_PROVIDER_REQUEST", "provider request is not strict JSON"
        ) from exc
    if type(decoded) is not dict:
        raise LiveRubricError(
            "UNTRUSTED_PROVIDER_REQUEST", "provider request root must be an object"
        )
    projected = cast(dict[str, JsonValue], decoded)
    if canonical_json_bytes(cast(JsonValue, projected)) != raw:
        raise LiveRubricError(
            "UNTRUSTED_PROVIDER_REQUEST", "provider request is not canonical JSON"
        )
    return projected


def _canonical_json_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare JSON trees by type-sensitive canonical bytes."""

    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _r23_tracking_packet_schema() -> dict[str, JsonValue]:
    path = (
        Path(__file__).resolve().parents[6]
        / "mobileworld_audit_handoff"
        / "schemas"
        / "r2_3"
        / "tracking_packet.v1.schema.json"
    )
    return _strict_json_object(path.read_bytes())


@lru_cache(maxsize=1)
def _request_proof_validator() -> Draft202012Validator:
    schema = _strict_json_object(
        (
            Path(__file__).resolve().parents[6]
            / "mobileworld_audit_handoff"
            / "schemas"
            / "r2_4"
            / "rubric_request_proof.v1.schema.json"
        ).read_bytes()
    )
    tracking_schema = _r23_tracking_packet_schema()
    resolver = RefResolver.from_schema(
        schema,
        store={cast(str, tracking_schema["$id"]): tracking_schema},
    )
    return Draft202012Validator(schema, resolver=resolver)


def _validate_provider_input_stimulus_projection(
    *,
    operation: LiveRubricOperationV1,
    provider_input: dict[str, JsonValue],
    stimulus: dict[str, JsonValue],
    logical_call_id: str,
) -> None:
    """Bind the complete provider semantic input to one Collector projection."""

    expected_outer = {
        "backend_extension_descriptor_sha256",
        "r23_compatibility_descriptor_sha256",
        "schema_version",
        "request" if operation is LiveRubricOperationV1.GENERATE else "packet",
    }
    if operation is LiveRubricOperationV1.TRACK:
        expected_outer.add("current_image_binding_sha256")
    if set(provider_input) != expected_outer:
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH", "rubric provider-input fields differ"
        )
    _require_sha256(
        provider_input["backend_extension_descriptor_sha256"],
        "backend_extension_descriptor_sha256",
    )
    _require_sha256(
        provider_input["r23_compatibility_descriptor_sha256"],
        "r23_compatibility_descriptor_sha256",
    )
    if operation is LiveRubricOperationV1.TRACK:
        _require_sha256(
            provider_input["current_image_binding_sha256"],
            "current_image_binding_sha256",
        )
    expected_schema_version = (
        LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION
        if operation is LiveRubricOperationV1.GENERATE
        else LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION
    )
    if provider_input["schema_version"] != expected_schema_version:
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH", "rubric provider-input version differs"
        )
    if type(stimulus) is not dict or set(stimulus) != {
        "schema_version",
        "task_run_id",
        "step_id",
        "cutoff",
        "task",
        "current_observation",
        "evidence_index",
    }:
        raise LiveRubricError(
            "COLLECTOR_CONTEXT_DRIFT", "Collector stimulus projection fields differ"
        )
    task_run_id = stimulus["task_run_id"]
    step_id = stimulus["step_id"]
    if type(task_run_id) is not str or type(step_id) is not str:
        raise LiveRubricError("COLLECTOR_CONTEXT_DRIFT", "Collector identity fields differ")

    if operation is LiveRubricOperationV1.GENERATE:
        request = provider_input["request"]
        if type(request) is not dict or set(request) != {
            "request_id",
            "task_run_id",
            "task",
            "backend",
        }:
            raise LiveRubricError(
                "PROVIDER_INPUT_BINDING_MISMATCH", "task-start request fields differ"
            )
        if (
            type(request["request_id"]) is not str
            or _ID.fullmatch(request["request_id"]) is None
            or request["task_run_id"] != task_run_id
            or not _canonical_json_equal(request["task"], stimulus["task"])
            or type(request["backend"]) is not dict
        ):
            raise LiveRubricError(
                "PROVIDER_INPUT_BINDING_MISMATCH",
                "task-start request differs from Collector task authority",
            )
        return

    packet = provider_input["packet"]
    if type(packet) is not dict:
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH", "tracking packet is not an exact object"
        )
    errors = tuple(Draft202012Validator(_r23_tracking_packet_schema()).iter_errors(packet))
    if errors:
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH", "tracking packet violates its checked-in schema"
        )
    if (
        packet.get("logical_call_id") != logical_call_id
        or packet.get("task_run_id") != task_run_id
        or packet.get("step_id") != step_id
        or not _canonical_json_equal(packet.get("cutoff"), stimulus["cutoff"])
        or not _canonical_json_equal(packet.get("task"), stimulus["task"])
        or not _canonical_json_equal(
            packet.get("current_observation"), stimulus["current_observation"]
        )
        or not _canonical_json_equal(packet.get("evidence_index"), stimulus["evidence_index"])
    ):
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH",
            "tracking packet differs from the complete Collector stimulus",
        )


def _validate_provider_input_stimulus_binding(
    *,
    operation: LiveRubricOperationV1,
    provider_input: dict[str, JsonValue],
    stimulus: RubricEvidenceSnapshotV1,
    logical_call_id: str,
) -> None:
    _validate_provider_input_stimulus_projection(
        operation=operation,
        provider_input=provider_input,
        stimulus=rubric_evidence_snapshot_projection(stimulus),
        logical_call_id=logical_call_id,
    )


def _live_rubric_responses_kwargs(
    *,
    operation: LiveRubricOperationV1,
    provider_input: dict[str, JsonValue],
    current_image_data_url: str | None,
) -> dict[str, object]:
    """Rebuild the one sealed Responses request from its semantic preimages."""

    if type(operation) is not LiveRubricOperationV1:
        raise LiveRubricError("UNTRUSTED_OPERATION", "rubric operation type differs")
    trusted_input = _snapshot_provider_input(provider_input)
    generate = operation is LiveRubricOperationV1.GENERATE
    if generate != (current_image_data_url is None):
        raise LiveRubricError(
            "CURRENT_IMAGE_BINDING_MISMATCH", "rubric request image census differs"
        )
    prompt = _GENERATE_INSTRUCTIONS if generate else _TRACK_INSTRUCTIONS
    schema = live_rubric_generate_schema() if generate else live_rubric_track_schema()
    input_content: list[dict[str, object]] = [
        {
            "type": "input_text",
            "text": canonical_json_bytes(cast(JsonValue, trusted_input)).decode("utf-8"),
        }
    ]
    if current_image_data_url is not None:
        # Decode now so a persisted proof cannot smuggle an unbound image URL.
        _decode_data_image(current_image_data_url)
        input_content.append(
            {
                "type": "input_image",
                "image_url": current_image_data_url,
                "detail": "high",
            }
        )
    return {
        "model": LIVE_RUBRIC_MODEL,
        "instructions": prompt,
        "input": [{"role": "user", "content": input_content}],
        "reasoning": {"effort": LIVE_RUBRIC_REASONING_EFFORT},
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
        "max_output_tokens": 8192,
    }


def build_live_rubric_provider_request_v1(
    *,
    operation: LiveRubricOperationV1,
    provider_input: dict[str, JsonValue],
    current_image_data_url: str | None,
) -> CanonicalHistoryPolicyRequestV1:
    """Build the exact canonical request used by both dispatch and proof validation."""

    return build_canonical_history_policy_request(
        _live_rubric_responses_kwargs(
            operation=operation,
            provider_input=provider_input,
            current_image_data_url=current_image_data_url,
        )
    )


def _snapshot_live_rubric_request_material(
    *,
    operation: LiveRubricOperationV1,
    task_run_id: str,
    logical_call_id: str,
    collector_stimulus: RubricEvidenceSnapshotV1,
    current_image: BoundCollectorCurrentImageV1 | None,
    provider_input: dict[str, JsonValue],
    provider_request: CanonicalHistoryPolicyRequestV1,
) -> tuple[
    RubricEvidenceSnapshotV1,
    BoundCollectorCurrentImageV1 | None,
    dict[str, JsonValue],
    CanonicalHistoryPolicyRequestV1,
]:
    """Detach and bind the response-independent preimages of one rubric request."""

    if type(operation) is not LiveRubricOperationV1:
        raise LiveRubricError("UNTRUSTED_OPERATION", "rubric operation type differs")
    _require_id(task_run_id, "task_run_id")
    _require_id(logical_call_id, "logical_call_id")
    stimulus = _snapshot_rubric_stimulus(collector_stimulus)
    detached_input = _snapshot_provider_input(provider_input)
    try:
        detached_request = snapshot_canonical_history_policy_request(provider_request)
    except Exception as exc:
        raise LiveRubricError(
            "UNTRUSTED_PROVIDER_REQUEST", "rubric provider request could not be detached"
        ) from exc
    image: BoundCollectorCurrentImageV1 | None = None
    if stimulus.task_run_id != task_run_id:
        raise LiveRubricError("COLLECTOR_CONTEXT_DRIFT", "rubric stimulus belongs to another task")
    if operation is LiveRubricOperationV1.GENERATE:
        if current_image is not None:
            raise LiveRubricError(
                "GENERATION_IMAGE_LEAK", "generation request anchor cannot carry an image"
            )
    else:
        if type(current_image) is not BoundCollectorCurrentImageV1:
            raise LiveRubricError(
                "MISSING_CURRENT_IMAGE", "tracking request anchor needs its current image"
            )
        image = _snapshot_bound_current_image(current_image)
        rebound = bind_current_collector_image_projection(
            stimulus=stimulus,
            current_image_data_url=image.data_url,
            current_image_sha256=image.content_sha256,
            logical_call_id=logical_call_id,
        )
        if rebound != image:
            raise LiveRubricError(
                "CURRENT_IMAGE_BINDING_MISMATCH",
                "tracking image differs from the exact Collector stimulus",
            )
        if detached_input.get("current_image_binding_sha256") != image.binding_sha256:
            raise LiveRubricError(
                "CURRENT_IMAGE_BINDING_MISMATCH",
                "tracking provider input differs from its current-image binding",
            )
    rubric_evidence_snapshot_sha256(stimulus)
    canonical_sha256(cast(JsonValue, detached_input))
    _validate_provider_input_stimulus_binding(
        operation=operation,
        provider_input=detached_input,
        stimulus=stimulus,
        logical_call_id=logical_call_id,
    )
    return stimulus, image, detached_input, detached_request


@dataclass(frozen=True, slots=True)
class LiveRubricAttemptRequestAnchorV1:
    """Module-sealed request preimages registered before one live dispatch."""

    operation: LiveRubricOperationV1
    task_run_id: str
    logical_call_id: str
    attempt_id: str
    attempt_order: int
    collector_stimulus: RubricEvidenceSnapshotV1 = field(repr=False)
    current_image: BoundCollectorCurrentImageV1 | None = field(repr=False)
    provider_input: dict[str, JsonValue] = field(repr=False)
    provider_request: CanonicalHistoryPolicyRequestV1 = field(repr=False)
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _TRUST_ANCHOR_SEAL:
            raise LiveRubricError(
                "UNTRUSTED_REQUEST_ANCHOR",
                "rubric attempt request anchor was not issued by the provider port",
            )
        _require_id(self.attempt_id, "attempt_id")
        if type(self.attempt_order) is not int or not 1 <= self.attempt_order <= 2:
            raise LiveRubricError(
                "INVALID_ATTEMPT_ORDER", "rubric attempt request order is invalid"
            )
        stimulus, image, provider_input, provider_request = _snapshot_live_rubric_request_material(
            operation=self.operation,
            task_run_id=self.task_run_id,
            logical_call_id=self.logical_call_id,
            collector_stimulus=self.collector_stimulus,
            current_image=self.current_image,
            provider_input=self.provider_input,
            provider_request=self.provider_request,
        )
        object.__setattr__(self, "collector_stimulus", stimulus)
        object.__setattr__(self, "current_image", image)
        object.__setattr__(self, "provider_input", provider_input)
        object.__setattr__(self, "provider_request", provider_request)


def _build_live_rubric_attempt_request_anchor(
    *,
    operation: LiveRubricOperationV1,
    task_run_id: str,
    logical_call_id: str,
    attempt_id: str,
    attempt_order: int,
    collector_stimulus: RubricEvidenceSnapshotV1,
    current_image: BoundCollectorCurrentImageV1 | None,
    provider_input: dict[str, JsonValue],
    provider_request: CanonicalHistoryPolicyRequestV1,
) -> LiveRubricAttemptRequestAnchorV1:
    return LiveRubricAttemptRequestAnchorV1(
        operation=operation,
        task_run_id=task_run_id,
        logical_call_id=logical_call_id,
        attempt_id=attempt_id,
        attempt_order=attempt_order,
        collector_stimulus=collector_stimulus,
        current_image=current_image,
        provider_input=provider_input,
        provider_request=provider_request,
        _seal=_TRUST_ANCHOR_SEAL,
    )


def snapshot_live_rubric_attempt_request_anchor(
    value: LiveRubricAttemptRequestAnchorV1,
) -> LiveRubricAttemptRequestAnchorV1:
    if type(value) is not LiveRubricAttemptRequestAnchorV1:
        raise LiveRubricError(
            "UNTRUSTED_REQUEST_ANCHOR", "rubric attempt request anchor type differs"
        )
    return LiveRubricAttemptRequestAnchorV1(
        operation=value.operation,
        task_run_id=value.task_run_id,
        logical_call_id=value.logical_call_id,
        attempt_id=value.attempt_id,
        attempt_order=value.attempt_order,
        collector_stimulus=value.collector_stimulus,
        current_image=value.current_image,
        provider_input=value.provider_input,
        provider_request=value.provider_request,
        _seal=_TRUST_ANCHOR_SEAL,
    )


@dataclass(frozen=True, slots=True)
class LiveRubricCallTrustAnchorV1:
    """Ephemeral preimage proof for one completed live rubric provider call.

    The cross-binding validator uses the exact provider-input/request,
    Collector stimulus/current-image, and admitted Responses envelope
    preimages instead of trusting caller-supplied hash copies.  A bounded
    projection of the request proof is retained only in the owner-only
    production audit; it never enters the hash-only call receipt or ordinary
    logs.
    """

    operation: LiveRubricOperationV1
    task_run_id: str
    logical_call_id: str
    collector_stimulus: RubricEvidenceSnapshotV1 = field(repr=False)
    current_image: BoundCollectorCurrentImageV1 | None = field(repr=False)
    provider_input: dict[str, JsonValue] = field(repr=False)
    provider_request: CanonicalHistoryPolicyRequestV1 = field(repr=False)
    response_envelope: ResponsesEnvelopeV1 = field(repr=False)
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _TRUST_ANCHOR_SEAL:
            raise LiveRubricError(
                "UNTRUSTED_REQUEST_ANCHOR",
                "rubric request anchor was not issued by the provider port",
            )
        stimulus, image, provider_input, provider_request = _snapshot_live_rubric_request_material(
            operation=self.operation,
            task_run_id=self.task_run_id,
            logical_call_id=self.logical_call_id,
            collector_stimulus=self.collector_stimulus,
            current_image=self.current_image,
            provider_input=self.provider_input,
            provider_request=self.provider_request,
        )
        envelope = _snapshot_responses_envelope(self.response_envelope)
        object.__setattr__(self, "collector_stimulus", stimulus)
        object.__setattr__(self, "current_image", image)
        object.__setattr__(self, "provider_input", provider_input)
        object.__setattr__(self, "provider_request", provider_request)
        object.__setattr__(self, "response_envelope", envelope)
        canonical_sha256(cast(JsonValue, responses_envelope_hash_projection(envelope)))


def _build_live_rubric_call_trust_anchor(
    *,
    operation: LiveRubricOperationV1,
    task_run_id: str,
    logical_call_id: str,
    collector_stimulus: RubricEvidenceSnapshotV1,
    current_image: BoundCollectorCurrentImageV1 | None,
    provider_input: dict[str, JsonValue],
    provider_request: CanonicalHistoryPolicyRequestV1,
    response_envelope: ResponsesEnvelopeV1,
) -> LiveRubricCallTrustAnchorV1:
    """Issue the private trust root only from this module's provider port."""

    return LiveRubricCallTrustAnchorV1(
        operation=operation,
        task_run_id=task_run_id,
        logical_call_id=logical_call_id,
        collector_stimulus=collector_stimulus,
        current_image=current_image,
        provider_input=provider_input,
        provider_request=provider_request,
        response_envelope=response_envelope,
        _seal=_TRUST_ANCHOR_SEAL,
    )


def snapshot_live_rubric_call_trust_anchor(
    value: LiveRubricCallTrustAnchorV1,
) -> LiveRubricCallTrustAnchorV1:
    if type(value) is not LiveRubricCallTrustAnchorV1:
        raise LiveRubricError("UNTRUSTED_TRUST_ANCHOR", "rubric trust anchor type differs")
    return LiveRubricCallTrustAnchorV1(
        operation=value.operation,
        task_run_id=value.task_run_id,
        logical_call_id=value.logical_call_id,
        collector_stimulus=_snapshot_rubric_stimulus(value.collector_stimulus),
        current_image=(
            None
            if value.current_image is None
            else _snapshot_bound_current_image(value.current_image)
        ),
        provider_input=_snapshot_provider_input(value.provider_input),
        provider_request=snapshot_canonical_history_policy_request(value.provider_request),
        response_envelope=_snapshot_responses_envelope(value.response_envelope),
        _seal=_TRUST_ANCHOR_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R24RubricBackendExtensionDescriptorV1:
    """Versioned transport provenance layered over the immutable R2.3 v1 descriptor.

    R2.3 remains a CPU/offline/injected-fake contract.  This descriptor is the
    only place where the R2.4 bridge may describe an owner-authorized Responses
    transport; every provider request and R2.4 call receipt binds its hash.
    """

    descriptor_id: str
    descriptor_version: str
    execution_scope: LiveRubricExecutionScopeV1
    transport_kind: LiveRubricTransportKindV1
    transport_authority: LiveRubricTransportAuthorityV1
    r23_compatibility_descriptor_sha256: str
    provider_config_sha256: str
    prompt_sha256: str
    rubric_schema_sha256: str
    tracking_packet_schema_sha256: str
    tracker_schema_sha256: str
    generate_output_schema_sha256: str
    track_output_schema_sha256: str
    configured_model: str
    external_network_attempted: bool
    model_call_attempted: bool
    local_gpu_used: bool = False
    schema_version: str = LIVE_RUBRIC_BACKEND_EXTENSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_RUBRIC_BACKEND_EXTENSION_SCHEMA_VERSION:
            raise LiveRubricError(
                "UNKNOWN_SCHEMA_VERSION", "rubric backend extension schema differs"
            )
        _require_id(self.descriptor_id, "descriptor_id")
        _require_id(self.descriptor_version, "descriptor_version")
        if type(self.execution_scope) is not LiveRubricExecutionScopeV1:
            raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "execution scope type differs")
        if type(self.transport_kind) is not LiveRubricTransportKindV1:
            raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "transport kind type differs")
        if type(self.transport_authority) is not LiveRubricTransportAuthorityV1:
            raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "transport authority type differs")
        for name in (
            "r23_compatibility_descriptor_sha256",
            "provider_config_sha256",
            "prompt_sha256",
            "rubric_schema_sha256",
            "tracking_packet_schema_sha256",
            "tracker_schema_sha256",
            "generate_output_schema_sha256",
            "track_output_schema_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if _require_id(self.configured_model, "configured_model") != LIVE_RUBRIC_MODEL:
            raise LiveRubricError(
                "MODEL_BINDING_MISMATCH", "rubric extension model differs from the pinned model"
            )
        for name in (
            "external_network_attempted",
            "model_call_attempted",
            "local_gpu_used",
        ):
            if type(getattr(self, name)) is not bool:
                raise LiveRubricError("UNTRUSTED_DESCRIPTOR", f"{name} must be an exact bool")
        cpu = (
            self.execution_scope is LiveRubricExecutionScopeV1.CPU_TEST_LOCAL
            and self.transport_kind is LiveRubricTransportKindV1.INJECTED_FAKE
            and self.transport_authority is LiveRubricTransportAuthorityV1.CPU_OFFLINE_FAKE
            and not self.external_network_attempted
            and not self.model_call_attempted
            and not self.local_gpu_used
        )
        live = (
            self.execution_scope is LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE
            and self.transport_kind is LiveRubricTransportKindV1.OPENAI_RESPONSES
            and self.transport_authority
            is LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION
            and self.external_network_attempted
            and self.model_call_attempted
            and not self.local_gpu_used
        )
        if not (cpu or live):
            raise LiveRubricError(
                "INVALID_BACKEND_PROVENANCE",
                "R2.4 transport provenance differs from its execution scope",
            )

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            cast(JsonValue, r24_rubric_backend_extension_descriptor_projection(self))
        )


def r24_rubric_backend_extension_descriptor_projection(
    value: R24RubricBackendExtensionDescriptorV1,
) -> dict[str, JsonValue]:
    if type(value) is not R24RubricBackendExtensionDescriptorV1:
        raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "backend extension type differs")
    return {
        name: cast(JsonValue, item.value if isinstance(item, StrEnum) else item)
        for name in value.__dataclass_fields__
        if (item := getattr(value, name)) is not None
    }


def snapshot_r24_rubric_backend_extension_descriptor(
    value: R24RubricBackendExtensionDescriptorV1,
) -> R24RubricBackendExtensionDescriptorV1:
    if type(value) is not R24RubricBackendExtensionDescriptorV1:
        raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "backend extension type differs")
    return R24RubricBackendExtensionDescriptorV1(
        **{name: getattr(value, name) for name in value.__dataclass_fields__}
    )


def r24_rubric_backend_extension_descriptor_sha256(
    value: R24RubricBackendExtensionDescriptorV1,
) -> str:
    return snapshot_r24_rubric_backend_extension_descriptor(value).sha256


def _checked_in_r23_schema_sha256(filename: str) -> str:
    path = (
        Path(__file__).resolve().parents[6]
        / "mobileworld_audit_handoff"
        / "schemas"
        / "r2_3"
        / filename
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_static_extension_bindings(
    value: R24RubricBackendExtensionDescriptorV1,
) -> None:
    extension = snapshot_r24_rubric_backend_extension_descriptor(value)
    generate_schema = live_rubric_generate_schema()
    track_schema = live_rubric_track_schema()
    if (
        extension.prompt_sha256 != live_rubric_prompt_bundle_sha256()
        or extension.rubric_schema_sha256 != _checked_in_r23_schema_sha256("rubric.v1.schema.json")
        or extension.tracking_packet_schema_sha256
        != _checked_in_r23_schema_sha256("tracking_packet.v1.schema.json")
        or extension.tracker_schema_sha256
        != _checked_in_r23_schema_sha256("tracker_output.v1.schema.json")
        or extension.generate_output_schema_sha256 != generate_schema.sha256
        or extension.track_output_schema_sha256 != track_schema.sha256
    ):
        raise LiveRubricError(
            "BACKEND_EXTENSION_BINDING_MISMATCH",
            "rubric extension differs from the checked-in prompt or schemas",
        )


def _validate_live_rubric_request_material_v1(
    *,
    operation: LiveRubricOperationV1,
    current_image: BoundCollectorCurrentImageV1 | None,
    provider_input: dict[str, JsonValue],
    provider_request: CanonicalHistoryPolicyRequestV1,
    backend_extension: R24RubricBackendExtensionDescriptorV1,
) -> str:
    """Rebuild one sealed request from its detached semantic preimages."""

    extension = snapshot_r24_rubric_backend_extension_descriptor(backend_extension)
    _validate_static_extension_bindings(extension)
    if (
        provider_input["backend_extension_descriptor_sha256"] != extension.sha256
        or provider_input["r23_compatibility_descriptor_sha256"]
        != extension.r23_compatibility_descriptor_sha256
    ):
        raise LiveRubricError(
            "PROVIDER_INPUT_BINDING_MISMATCH",
            "provider input differs from the exact backend extension",
        )
    if operation is LiveRubricOperationV1.GENERATE:
        task_start = provider_input["request"]
        assert type(task_start) is dict  # Checked by the trust-anchor constructor.
        backend = task_start["backend"]
        if (
            type(backend) is not dict
            or canonical_sha256(cast(JsonValue, backend))
            != extension.r23_compatibility_descriptor_sha256
            or backend.get("prompt_sha256") != extension.prompt_sha256
            or backend.get("rubric_schema_sha256") != extension.rubric_schema_sha256
            or backend.get("tracking_packet_schema_sha256")
            != extension.tracking_packet_schema_sha256
            or backend.get("tracker_schema_sha256") != extension.tracker_schema_sha256
        ):
            raise LiveRubricError(
                "PROVIDER_INPUT_BINDING_MISMATCH",
                "task-start backend differs from its compatibility descriptor",
            )
    expected = build_live_rubric_provider_request_v1(
        operation=operation,
        provider_input=provider_input,
        current_image_data_url=(None if current_image is None else current_image.data_url),
    )
    if expected != provider_request:
        raise LiveRubricError(
            "PROVIDER_REQUEST_BINDING_MISMATCH",
            "canonical provider request differs from its exact semantic preimages",
        )
    return expected.request_sha256


def validate_live_rubric_attempt_request_anchor_v1(
    value: LiveRubricAttemptRequestAnchorV1,
    *,
    backend_extension: R24RubricBackendExtensionDescriptorV1,
) -> str:
    """Verify an attempt request anchor without requiring a provider response."""

    trusted = snapshot_live_rubric_attempt_request_anchor(value)
    return _validate_live_rubric_request_material_v1(
        operation=trusted.operation,
        current_image=trusted.current_image,
        provider_input=trusted.provider_input,
        provider_request=trusted.provider_request,
        backend_extension=backend_extension,
    )


def validate_live_rubric_request_anchor_v1(
    value: LiveRubricCallTrustAnchorV1,
    *,
    backend_extension: R24RubricBackendExtensionDescriptorV1,
) -> str:
    """Rebuild and verify a completed call's request independently of receipt hashes."""

    trusted = snapshot_live_rubric_call_trust_anchor(value)
    return _validate_live_rubric_request_material_v1(
        operation=trusted.operation,
        current_image=trusted.current_image,
        provider_input=trusted.provider_input,
        provider_request=trusted.provider_request,
        backend_extension=backend_extension,
    )


def _bound_current_image_proof_projection(
    value: BoundCollectorCurrentImageV1,
) -> dict[str, JsonValue]:
    image = _snapshot_bound_current_image(value)
    return {
        "task_run_id": image.task_run_id,
        "logical_call_id": image.logical_call_id,
        "source_event_id": image.source_event_id,
        "source_event_seq": image.source_event_seq,
        "evidence_id": image.evidence_id,
        "content_sha256": image.content_sha256,
        "media_type": image.media_type,
        "width": image.width,
        "height": image.height,
        "data_url": image.data_url,
        "stimulus_sha256": image.stimulus_sha256,
        "binding_sha256": image.binding_sha256,
    }


def _validate_durable_current_image_projection(
    value: dict[str, JsonValue],
    *,
    stimulus: dict[str, JsonValue],
    stimulus_sha256: str,
    task_run_id: str,
    logical_call_id: str,
) -> str:
    """Rebind a persisted image preimage to the persisted Collector stimulus."""

    try:
        image = BoundCollectorCurrentImageV1(
            task_run_id=cast(str, value["task_run_id"]),
            logical_call_id=cast(str, value["logical_call_id"]),
            source_event_id=cast(str, value["source_event_id"]),
            source_event_seq=cast(int, value["source_event_seq"]),
            evidence_id=cast(str, value["evidence_id"]),
            content_sha256=cast(str, value["content_sha256"]),
            media_type=cast(str, value["media_type"]),
            width=cast(int, value["width"]),
            height=cast(int, value["height"]),
            data_url=cast(str, value["data_url"]),
            stimulus_sha256=cast(str, value["stimulus_sha256"]),
        )
    except (KeyError, TypeError, ValueError, LiveRubricError) as exc:
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "tracking image preimage is invalid"
        ) from exc
    current = stimulus.get("current_observation")
    evidence_index = stimulus.get("evidence_index")
    if type(current) is not dict or type(evidence_index) is not list:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "Collector image authority is absent")
    matches = tuple(
        item
        for item in evidence_index
        if type(item) is dict and item.get("evidence_id") == current.get("screenshot_evidence_id")
    )
    if len(matches) != 1:
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "Collector screenshot evidence is not unique"
        )
    evidence = matches[0]
    projection = evidence.get("projection")
    if type(projection) is not dict:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "Collector screenshot projection is absent")
    authority_projection: JsonValue = {
        "task_run_id": task_run_id,
        "logical_call_id": logical_call_id,
        "source_event_id": current.get("source_event_id"),
        "source_event_seq": current.get("source_event_seq"),
        "evidence_id": current.get("screenshot_evidence_id"),
        "content_sha256": current.get("screenshot_content_sha256"),
        "media_type": projection.get("media_type"),
        "width": projection.get("width"),
        "height": projection.get("height"),
        "stimulus_sha256": stimulus_sha256,
    }
    persisted_projection: JsonValue = {
        key: child for key, child in value.items() if key not in {"data_url", "binding_sha256"}
    }
    if (
        evidence.get("role") != "CURRENT_UI_SCREENSHOT"
        or evidence.get("task_run_id") != task_run_id
        or evidence.get("source_event_id") != current.get("source_event_id")
        or not _canonical_json_equal(
            evidence.get("source_event_seq"), current.get("source_event_seq")
        )
        or evidence.get("source_event_type") != "step_started"
        or evidence.get("caused_by_event_id") is not None
        or evidence.get("observed_by_cutoff") is not True
        or projection.get("kind") != "IMAGE_REFERENCE"
        or projection.get("content_sha256") != current.get("screenshot_content_sha256")
        or not _canonical_json_equal(persisted_projection, authority_projection)
        or value.get("binding_sha256") != image.binding_sha256
    ):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "tracking image differs from Collector authority"
        )
    return image.data_url


def validate_live_rubric_request_proof_projection_v1(
    value: JsonValue,
    *,
    attempt_receipt: LiveAttemptReceiptV1 | dict[str, JsonValue] | None = None,
    expected_attempt_order: int | None = None,
) -> None:
    """Validate a durable request proof and, when supplied, its receipt preimage."""

    proof = _snapshot_request_proof(value)
    if tuple(_request_proof_validator().iter_errors(proof)):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "durable rubric request proof violates its schema"
        )
    expected_fields = {
        "schema_version",
        "operation",
        "task_run_id",
        "logical_call_id",
        "attempt_id",
        "attempt_order",
        "attempt_role",
        "attempt_status",
        "attempt_dispatch_count",
        "attempt_receipt_sha256",
        "backend_extension_descriptor_sha256",
        "r23_compatibility_descriptor_sha256",
        "collector_stimulus",
        "collector_stimulus_sha256",
        "current_image",
        "provider_input",
        "provider_input_sha256",
        "tracking_packet_sha256",
        "provider_request",
        "provider_request_sha256",
        "provider_request_byte_count",
    }
    if set(proof) != expected_fields or proof["schema_version"] != (
        LIVE_RUBRIC_REQUEST_PROOF_SCHEMA_VERSION
    ):
        raise LiveRubricError("INVALID_REQUEST_PROOF", "durable rubric request-proof fields differ")
    try:
        operation = LiveRubricOperationV1(cast(str, proof["operation"]))
    except (TypeError, ValueError) as exc:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof operation differs") from exc
    task_run_id = _require_id(proof["task_run_id"], "task_run_id")
    logical_call_id = _require_id(proof["logical_call_id"], "logical_call_id")
    attempt_id = _require_id(proof["attempt_id"], "attempt_id")
    attempt_order = proof["attempt_order"]
    if type(attempt_order) is not int or not 1 <= attempt_order <= 2:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof attempt order differs")
    if expected_attempt_order is not None and (
        type(expected_attempt_order) is not int or attempt_order != expected_attempt_order
    ):
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof attempt sequence differs")
    if proof["attempt_role"] != LiveAttemptRoleV1.RUBRIC.value:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof attempt role differs")
    try:
        attempt_status = LiveAttemptStatusV1(cast(str, proof["attempt_status"]))
    except (TypeError, ValueError) as exc:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof status differs") from exc
    dispatch_count = proof["attempt_dispatch_count"]
    if type(dispatch_count) is not int or dispatch_count not in {0, 1}:
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof dispatch count differs")
    if (
        attempt_status
        in {
            LiveAttemptStatusV1.COMPLETED,
            LiveAttemptStatusV1.CANCELLED_POST_DISPATCH,
        }
        and dispatch_count != 1
    ) or (attempt_status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH and dispatch_count != 0):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "request-proof status/dispatch state differs"
        )
    attempt_receipt_sha256 = _require_sha256(
        proof["attempt_receipt_sha256"], "attempt_receipt_sha256"
    )
    extension_sha256 = _require_sha256(
        proof["backend_extension_descriptor_sha256"],
        "backend_extension_descriptor_sha256",
    )
    r23_sha256 = _require_sha256(
        proof["r23_compatibility_descriptor_sha256"],
        "r23_compatibility_descriptor_sha256",
    )
    stimulus = _snapshot_provider_input(proof["collector_stimulus"])
    stimulus_sha256 = _require_sha256(
        proof["collector_stimulus_sha256"], "collector_stimulus_sha256"
    )
    if (
        canonical_sha256(cast(JsonValue, stimulus)) != stimulus_sha256
        or stimulus.get("task_run_id") != task_run_id
    ):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "request-proof Collector stimulus hash differs"
        )
    task = stimulus.get("task")
    if (
        type(task) is not dict
        or type(task.get("exact_text")) is not str
        or task.get("text_sha256")
        != hashlib.sha256(cast(str, task["exact_text"]).encode("utf-8")).hexdigest()
    ):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "request-proof task instruction binding differs"
        )
    provider_input = _snapshot_provider_input(proof["provider_input"])
    if (
        canonical_sha256(cast(JsonValue, provider_input))
        != _require_sha256(proof["provider_input_sha256"], "provider_input_sha256")
        or provider_input.get("backend_extension_descriptor_sha256") != extension_sha256
        or provider_input.get("r23_compatibility_descriptor_sha256") != r23_sha256
    ):
        raise LiveRubricError("INVALID_REQUEST_PROOF", "request-proof provider input hash differs")
    _validate_provider_input_stimulus_projection(
        operation=operation,
        provider_input=provider_input,
        stimulus=stimulus,
        logical_call_id=logical_call_id,
    )
    tracking_packet_sha256 = proof["tracking_packet_sha256"]
    if operation is LiveRubricOperationV1.GENERATE:
        if tracking_packet_sha256 is not None:
            raise LiveRubricError(
                "INVALID_REQUEST_PROOF",
                "generation request proof unexpectedly binds a tracking packet",
            )
    else:
        packet = provider_input.get("packet")
        if type(packet) is not dict or _require_sha256(
            tracking_packet_sha256, "tracking_packet_sha256"
        ) != canonical_sha256(cast(JsonValue, packet)):
            raise LiveRubricError(
                "INVALID_REQUEST_PROOF", "tracking packet root differs from its preimage"
            )

    current_image = proof["current_image"]
    current_image_data_url: str | None = None
    if operation is LiveRubricOperationV1.GENERATE:
        if current_image is not None:
            raise LiveRubricError(
                "INVALID_REQUEST_PROOF", "generation proof unexpectedly contains an image"
            )
    else:
        if type(current_image) is not dict or set(current_image) != {
            "task_run_id",
            "logical_call_id",
            "source_event_id",
            "source_event_seq",
            "evidence_id",
            "content_sha256",
            "media_type",
            "width",
            "height",
            "data_url",
            "stimulus_sha256",
            "binding_sha256",
        }:
            raise LiveRubricError("INVALID_REQUEST_PROOF", "tracking image proof fields differ")
        current_image_data_url = _validate_durable_current_image_projection(
            current_image,
            stimulus=stimulus,
            stimulus_sha256=stimulus_sha256,
            task_run_id=task_run_id,
            logical_call_id=logical_call_id,
        )
        if provider_input.get("current_image_binding_sha256") != current_image["binding_sha256"]:
            raise LiveRubricError("INVALID_REQUEST_PROOF", "tracking image proof binding differs")

    request_projection = _snapshot_canonical_object(
        proof["provider_request"],
        maximum_bytes=_MAX_PROVIDER_REQUEST_BYTES,
        code="INVALID_REQUEST_PROOF",
        label="canonical provider request",
    )
    request_bytes = canonical_json_bytes(cast(JsonValue, request_projection))
    expected_request = build_live_rubric_provider_request_v1(
        operation=operation,
        provider_input=provider_input,
        current_image_data_url=current_image_data_url,
    )
    if (
        request_bytes != expected_request.canonical_bytes
        or _require_sha256(proof["provider_request_sha256"], "provider_request_sha256")
        != expected_request.request_sha256
        or type(proof["provider_request_byte_count"]) is not int
        or proof["provider_request_byte_count"] != expected_request.byte_count
    ):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "canonical provider request differs from its reconstruction"
        )

    if attempt_receipt is not None:
        receipt: LiveAttemptReceiptV1
        if type(attempt_receipt) is LiveAttemptReceiptV1:
            receipt = snapshot_live_attempt_receipt(attempt_receipt)
        elif type(attempt_receipt) is dict:
            receipt = _parse_durable_live_attempt_receipt_projection(attempt_receipt)
        else:
            raise LiveRubricError(
                "INVALID_REQUEST_PROOF", "matching attempt receipt has an untrusted type"
            )
        if (
            live_attempt_receipt_sha256(receipt) != attempt_receipt_sha256
            or receipt.attempt_id != attempt_id
            or receipt.role is not LiveAttemptRoleV1.RUBRIC
            or receipt.logical_call_id != logical_call_id
            or receipt.request_sha256 != expected_request.request_sha256
            or receipt.status is not attempt_status
            or receipt.dispatch_count != dispatch_count
        ):
            raise LiveRubricError(
                "INVALID_REQUEST_PROOF", "request proof differs from its attempt receipt"
            )


def _parse_durable_live_attempt_receipt_projection(
    value: dict[str, JsonValue],
) -> LiveAttemptReceiptV1:
    """Rebuild every terminal-attempt invariant from owner-only JSON."""

    projection = _snapshot_canonical_object(
        value,
        maximum_bytes=_MAX_REQUEST_PROOF_BYTES,
        code="INVALID_REQUEST_PROOF",
        label="live attempt receipt",
    )
    expected_fields = {
        "actor_request_sha256",
        "attempt_id",
        "authority_sha256",
        "cached_input_tokens",
        "cancellation_requested",
        "case_id",
        "case_execution_lease_sha256",
        "cost_status",
        "cost_usd_micros",
        "dispatch_count",
        "duration_ns",
        "execution_kind",
        "failure_code",
        "input_tokens",
        "late_output_detected",
        "logical_call_id",
        "manifest_sha256",
        "output_tokens",
        "preflight_sha256",
        "pricing_binding_sha256",
        "request_sha256",
        "requested_model",
        "response_envelope_sha256",
        "returned_model",
        "role",
        "schema_version",
        "stage_sha256",
        "status",
        "termination",
        "total_tokens",
        "transport_binding_sha256",
        "worker_exit_code",
        "worker_pid",
        "worker_reaped",
    }
    if set(projection) != expected_fields:
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "live attempt receipt projection fields differ"
        )
    try:
        receipt = LiveAttemptReceiptV1(
            attempt_id=cast(str, projection["attempt_id"]),
            role=LiveAttemptRoleV1(cast(str, projection["role"])),
            authority_sha256=cast(str, projection["authority_sha256"]),
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
            execution_kind=LiveAttemptExecutionKindV1(cast(str, projection["execution_kind"])),
            status=LiveAttemptStatusV1(cast(str, projection["status"])),
            dispatch_count=cast(int, projection["dispatch_count"]),
            response_envelope_sha256=cast(str | None, projection["response_envelope_sha256"]),
            input_tokens=cast(int | None, projection["input_tokens"]),
            cached_input_tokens=cast(int | None, projection["cached_input_tokens"]),
            output_tokens=cast(int | None, projection["output_tokens"]),
            total_tokens=cast(int | None, projection["total_tokens"]),
            cost_status=LiveAttemptCostStatusV1(cast(str, projection["cost_status"])),
            cost_usd_micros=cast(int | None, projection["cost_usd_micros"]),
            cancellation_requested=cast(bool, projection["cancellation_requested"]),
            termination=LiveAttemptTerminationV1(cast(str, projection["termination"])),
            worker_pid=cast(int | None, projection["worker_pid"]),
            worker_exit_code=cast(int | None, projection["worker_exit_code"]),
            worker_reaped=cast(bool, projection["worker_reaped"]),
            late_output_detected=cast(bool, projection["late_output_detected"]),
            duration_ns=cast(int, projection["duration_ns"]),
            failure_code=cast(str | None, projection["failure_code"]),
            requested_model=cast(str | None, projection["requested_model"]),
            returned_model=cast(str | None, projection["returned_model"]),
            schema_version=cast(str, projection["schema_version"]),
        )
    except (KeyError, TypeError, ValueError, LiveAttemptError) as exc:
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "live attempt receipt state is invalid"
        ) from exc
    rebuilt = live_attempt_receipt_projection(receipt)
    if not _canonical_json_equal(
        cast(JsonValue, projection),
        cast(JsonValue, rebuilt),
    ):
        raise LiveRubricError(
            "INVALID_REQUEST_PROOF", "live attempt receipt projection is non-canonical"
        )
    return receipt


def live_rubric_attempt_request_proof_projection(
    value: LiveRubricAttemptRequestAnchorV1,
    *,
    attempt_receipt: LiveAttemptReceiptV1,
    backend_extension: R24RubricBackendExtensionDescriptorV1,
) -> dict[str, JsonValue]:
    """Project one attempt-bound request preimage for owner-only durable audit."""

    trusted = snapshot_live_rubric_attempt_request_anchor(value)
    receipt = snapshot_live_attempt_receipt(attempt_receipt)
    extension = snapshot_r24_rubric_backend_extension_descriptor(backend_extension)
    request_sha256 = validate_live_rubric_attempt_request_anchor_v1(
        trusted, backend_extension=extension
    )
    if (
        receipt.role is not LiveAttemptRoleV1.RUBRIC
        or receipt.attempt_id != trusted.attempt_id
        or receipt.logical_call_id != trusted.logical_call_id
        or receipt.request_sha256 != request_sha256
    ):
        raise LiveRubricError(
            "REQUEST_ATTEMPT_BINDING_MISMATCH",
            "rubric request anchor differs from its terminal attempt",
        )
    stimulus = rubric_evidence_snapshot_projection(trusted.collector_stimulus)
    provider_input = _snapshot_provider_input(trusted.provider_input)
    proof: dict[str, JsonValue] = {
        "schema_version": LIVE_RUBRIC_REQUEST_PROOF_SCHEMA_VERSION,
        "operation": trusted.operation.value,
        "task_run_id": trusted.task_run_id,
        "logical_call_id": trusted.logical_call_id,
        "attempt_id": trusted.attempt_id,
        "attempt_order": trusted.attempt_order,
        "attempt_role": receipt.role.value,
        "attempt_status": receipt.status.value,
        "attempt_dispatch_count": receipt.dispatch_count,
        "attempt_receipt_sha256": live_attempt_receipt_sha256(receipt),
        "backend_extension_descriptor_sha256": extension.sha256,
        "r23_compatibility_descriptor_sha256": extension.r23_compatibility_descriptor_sha256,
        "collector_stimulus": stimulus,
        "collector_stimulus_sha256": rubric_evidence_snapshot_sha256(trusted.collector_stimulus),
        "current_image": (
            None
            if trusted.current_image is None
            else _bound_current_image_proof_projection(trusted.current_image)
        ),
        "provider_input": provider_input,
        "provider_input_sha256": canonical_sha256(cast(JsonValue, provider_input)),
        "tracking_packet_sha256": (
            None
            if trusted.operation is LiveRubricOperationV1.GENERATE
            else canonical_sha256(provider_input["packet"])
        ),
        "provider_request": _provider_request_object(trusted.provider_request.canonical_bytes),
        "provider_request_sha256": request_sha256,
        "provider_request_byte_count": trusted.provider_request.byte_count,
    }
    validate_live_rubric_request_proof_projection_v1(
        cast(JsonValue, proof),
        attempt_receipt=receipt,
        expected_attempt_order=trusted.attempt_order,
    )
    return proof


@dataclass(frozen=True, slots=True)
class LiveRubricCallReceiptV1:
    receipt_id: str
    operation: LiveRubricOperationV1
    execution_scope: LiveRubricExecutionScopeV1
    task_run_id: str
    logical_call_id: str
    backend_extension_descriptor_sha256: str
    r23_compatibility_descriptor_sha256: str
    transport_kind: LiveRubricTransportKindV1
    transport_authority: LiveRubricTransportAuthorityV1
    prompt_sha256: str
    provider_input_schema_version: str
    provider_output_schema_sha256: str
    provider_request_sha256: str
    provider_output_sha256: str
    transport_binding_sha256: str
    pricing_binding_sha256: str | None
    current_image_binding_sha256: str | None
    manifest_sha256: str | None
    preflight_sha256: str | None
    case_execution_lease_sha256: str | None
    stage_sha256: str | None
    attempt_authority_sha256: str | None
    attempt_receipt_sha256: str | None
    requested_model: str | None
    returned_model: str | None
    dispatch_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd_micros: int | None
    raw_task_or_image_persisted: bool = False
    provider_output_persisted: bool = False
    actor_history_included: bool = False
    history_ir_included: bool = False
    action_or_tool_authority: bool = False
    schema_version: str = LIVE_RUBRIC_CALL_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_RUBRIC_CALL_RECEIPT_SCHEMA_VERSION:
            raise LiveRubricError("UNKNOWN_SCHEMA_VERSION", "rubric call receipt schema differs")
        _require_id(self.receipt_id, "receipt_id")
        if type(self.operation) is not LiveRubricOperationV1:
            raise LiveRubricError("UNTRUSTED_RECEIPT", "operation type differs")
        if type(self.execution_scope) is not LiveRubricExecutionScopeV1:
            raise LiveRubricError("UNTRUSTED_RECEIPT", "execution scope differs")
        _require_id(self.task_run_id, "task_run_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.provider_input_schema_version, "provider_input_schema_version")
        for name in (
            "backend_extension_descriptor_sha256",
            "r23_compatibility_descriptor_sha256",
            "prompt_sha256",
            "provider_output_schema_sha256",
            "provider_request_sha256",
            "provider_output_sha256",
            "transport_binding_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if self.current_image_binding_sha256 is not None:
            _require_sha256(self.current_image_binding_sha256, "current_image_binding_sha256")
        for name in (
            "pricing_binding_sha256",
            "manifest_sha256",
            "preflight_sha256",
            "case_execution_lease_sha256",
            "stage_sha256",
            "attempt_authority_sha256",
            "attempt_receipt_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        if type(self.dispatch_count) is not int or self.dispatch_count != 1:
            raise LiveRubricError("INVALID_CALL_CENSUS", "completed rubric call dispatch differs")
        if type(self.transport_kind) is not LiveRubricTransportKindV1:
            raise LiveRubricError("UNTRUSTED_RECEIPT", "transport kind type differs")
        if type(self.transport_authority) is not LiveRubricTransportAuthorityV1:
            raise LiveRubricError("UNTRUSTED_RECEIPT", "transport authority type differs")
        usage = (self.input_tokens, self.output_tokens, self.total_tokens, self.cost_usd_micros)
        for name, maximum in (
            ("input_tokens", 100_000_000),
            ("output_tokens", 100_000_000),
            ("total_tokens", 100_000_000),
            ("cost_usd_micros", 100_000_000_000),
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                raise LiveRubricError("INVALID_RECEIPT_ACCOUNTING", f"{name} is outside bounds")
        if self.total_tokens is not None and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise LiveRubricError(
                "INVALID_RECEIPT_ACCOUNTING", "rubric token census is inconsistent"
            )
        if self.execution_scope is LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE:
            if any(
                getattr(self, name) is None
                for name in (
                    "manifest_sha256",
                    "preflight_sha256",
                    "case_execution_lease_sha256",
                    "stage_sha256",
                    "attempt_authority_sha256",
                    "attempt_receipt_sha256",
                    "pricing_binding_sha256",
                )
            ) or any(value is None for value in usage):
                raise LiveRubricError("INCOMPLETE_LIVE_RECEIPT", "live attempt proof is incomplete")
            if (
                self.transport_kind is not LiveRubricTransportKindV1.OPENAI_RESPONSES
                or self.transport_authority
                is not LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION
                or self.requested_model != LIVE_RUBRIC_MODEL
                or self.returned_model != self.requested_model
            ):
                raise LiveRubricError(
                    "MODEL_OR_TRANSPORT_BINDING_MISMATCH",
                    "live receipt model or transport provenance differs",
                )
        elif any(
            getattr(self, name) is not None
            for name in (
                "manifest_sha256",
                "preflight_sha256",
                "case_execution_lease_sha256",
                "stage_sha256",
                "attempt_authority_sha256",
                "attempt_receipt_sha256",
                "pricing_binding_sha256",
            )
        ) or any(value is not None for value in usage):
            raise LiveRubricError("FALSE_LIVE_CLAIM", "CPU receipt carries live attempt proof")
        elif (
            self.transport_kind is not LiveRubricTransportKindV1.INJECTED_FAKE
            or self.transport_authority is not LiveRubricTransportAuthorityV1.CPU_OFFLINE_FAKE
            or self.requested_model is not None
            or self.returned_model is not None
        ):
            raise LiveRubricError(
                "FALSE_LIVE_CLAIM", "CPU receipt carries live transport or model provenance"
            )
        if self.operation is LiveRubricOperationV1.TRACK:
            if self.current_image_binding_sha256 is None:
                raise LiveRubricError(
                    "MISSING_CURRENT_IMAGE", "tracking receipt needs current image"
                )
        elif self.current_image_binding_sha256 is not None:
            raise LiveRubricError("GENERATION_IMAGE_LEAK", "generation cannot consume a screenshot")
        for name in (
            "raw_task_or_image_persisted",
            "provider_output_persisted",
            "actor_history_included",
            "history_ir_included",
            "action_or_tool_authority",
        ):
            if type(getattr(self, name)) is not bool or getattr(self, name):
                raise LiveRubricError("FORBIDDEN_RECEIPT_CLAIM", f"{name} must remain false")

    @property
    def sha256(self) -> str:
        return canonical_sha256(cast(JsonValue, live_rubric_call_receipt_projection(self)))


def live_rubric_call_receipt_projection(value: LiveRubricCallReceiptV1) -> dict[str, JsonValue]:
    if type(value) is not LiveRubricCallReceiptV1:
        raise LiveRubricError("UNTRUSTED_RECEIPT", "receipt type differs")
    trusted = snapshot_live_rubric_call_receipt(value)
    result: dict[str, JsonValue] = {}
    for name in trusted.__dataclass_fields__:
        item = getattr(trusted, name)
        if item is None:
            continue
        result[name] = cast(JsonValue, item.value if isinstance(item, StrEnum) else item)
    return result


def snapshot_live_rubric_call_receipt(
    value: LiveRubricCallReceiptV1,
) -> LiveRubricCallReceiptV1:
    if type(value) is not LiveRubricCallReceiptV1:
        raise LiveRubricError("UNTRUSTED_RECEIPT", "receipt type differs")
    return LiveRubricCallReceiptV1(
        **{name: getattr(value, name) for name in value.__dataclass_fields__}
    )


def live_rubric_call_receipt_sha256(value: LiveRubricCallReceiptV1) -> str:
    return snapshot_live_rubric_call_receipt(value).sha256


def rubric_backend_descriptor_projection(
    value: RubricBackendDescriptorV1,
) -> dict[str, JsonValue]:
    if type(value) is not RubricBackendDescriptorV1:
        raise LiveRubricError("UNTRUSTED_DESCRIPTOR", "backend descriptor type differs")
    return {
        "backend_id": value.backend_id,
        "backend_version": value.backend_version,
        "prompt_sha256": value.prompt_sha256,
        "rubric_schema_sha256": value.rubric_schema_sha256,
        "tracking_packet_schema_sha256": value.tracking_packet_schema_sha256,
        "tracker_schema_sha256": value.tracker_schema_sha256,
        "config_sha256": value.config_sha256,
        "backend_kind": value.backend_kind.value,
        "transport_authority": value.transport_authority.value,
        "external_network_attempted": value.external_network_attempted,
        "model_call_attempted": value.model_call_attempted,
        "local_gpu_used": value.local_gpu_used,
    }


def rubric_backend_descriptor_sha256(value: RubricBackendDescriptorV1) -> str:
    return canonical_sha256(cast(JsonValue, rubric_backend_descriptor_projection(value)))


@dataclass(frozen=True, slots=True)
class _RubricCallContextV1:
    logical_call_id: str
    task_run_id: str
    actor_request_sha256: str
    deadline_monotonic_ns: int
    max_cost_usd_micros: int
    stimulus: RubricEvidenceSnapshotV1
    image: BoundCollectorCurrentImageV1
    case_lease: CaseExecutionLeaseV1 | None
    execution_control: PolicyExecutionControlV1 | None

    def __post_init__(self) -> None:
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.task_run_id, "task_run_id")
        _require_sha256(self.actor_request_sha256, "actor_request_sha256")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns < 1:
            raise LiveRubricError("INVALID_DEADLINE", "rubric deadline is invalid")
        if (
            type(self.max_cost_usd_micros) is not int
            or not 0 <= self.max_cost_usd_micros <= 100_000_000_000
        ):
            raise LiveRubricError("INVALID_COST_BOUND", "rubric cost bound is invalid")
        if type(self.image) is not BoundCollectorCurrentImageV1:
            raise LiveRubricError("UNTRUSTED_IMAGE", "current image type differs")
        stimulus = _snapshot_rubric_stimulus(self.stimulus)
        if (
            self.image.logical_call_id != self.logical_call_id
            or self.image.task_run_id != self.task_run_id
            or stimulus.task_run_id != self.task_run_id
            or self.image.stimulus_sha256 != rubric_evidence_snapshot_sha256(stimulus)
        ):
            raise LiveRubricError("IMAGE_CONTEXT_DRIFT", "image belongs to another call")


@dataclass(frozen=True, slots=True)
class _ProductionCaseAuthorityV1:
    logical_call_id: str
    actor_request_sha256: str
    deadline_monotonic_ns: int
    max_cost_usd_micros: int
    case_lease: CaseExecutionLeaseV1
    execution_control: PolicyExecutionControlV1

    def __post_init__(self) -> None:
        _require_id(self.logical_call_id, "logical_call_id")
        _require_sha256(self.actor_request_sha256, "actor_request_sha256")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns < 1:
            raise LiveRubricError("INVALID_DEADLINE", "rubric deadline is invalid")
        if (
            type(self.max_cost_usd_micros) is not int
            or not 0 <= self.max_cost_usd_micros <= 100_000_000_000
        ):
            raise LiveRubricError("INVALID_COST_BOUND", "rubric cost bound is invalid")
        if type(self.case_lease) is not CaseExecutionLeaseV1:
            raise LiveRubricError("CASE_LEASE_REQUIRED", "production authority lacks a lease")
        if not isinstance(self.execution_control, PolicyExecutionControlV1):
            raise LiveRubricError(
                "EXECUTION_CONTROL_REQUIRED", "production authority lacks the seam fence"
            )


class _RubricContextStoreV1:
    def __init__(self) -> None:
        self._by_call: dict[str, _RubricCallContextV1] = {}
        self._current_by_task: dict[str, str] = {}
        self._lock = threading.Lock()

    def bind(self, context: _RubricCallContextV1) -> None:
        if type(context) is not _RubricCallContextV1:
            raise LiveRubricError("UNTRUSTED_CONTEXT", "rubric context type differs")
        with self._lock:
            prior = self._by_call.get(context.logical_call_id)
            if prior is not None and prior != context:
                raise LiveRubricError("LOGICAL_CALL_CONTEXT_DRIFT", "rubric context changed")
            self._by_call[context.logical_call_id] = context
            self._current_by_task[context.task_run_id] = context.logical_call_id

    def resolve(self, *, task_run_id: str, logical_call_id: str | None) -> _RubricCallContextV1:
        with self._lock:
            call_id = logical_call_id or self._current_by_task.get(task_run_id)
            value = None if call_id is None else self._by_call.get(call_id)
        if value is None or value.task_run_id != task_run_id:
            raise LiveRubricError("RUBRIC_CASE_CONTEXT_MISSING", "no bound case context exists")
        return value


class _BaseRubricProviderPortV1:
    def __init__(self) -> None:
        self._contexts = _RubricContextStoreV1()
        self._receipts: list[LiveRubricCallReceiptV1] = []
        self._trust_anchors: list[LiveRubricCallTrustAnchorV1] = []
        self._attempt_request_anchors: list[LiveRubricAttemptRequestAnchorV1] = []
        self._receipt_lock = threading.Lock()
        self._call_keys: set[tuple[str, str, LiveRubricOperationV1]] = set()

    @property
    def execution_scope(self) -> LiveRubricExecutionScopeV1:
        raise NotImplementedError

    @property
    def config_projection(self) -> dict[str, JsonValue]:
        raise NotImplementedError

    @property
    def receipts(self) -> tuple[LiveRubricCallReceiptV1, ...]:
        with self._receipt_lock:
            return tuple(snapshot_live_rubric_call_receipt(value) for value in self._receipts)

    @property
    def trust_anchors(self) -> tuple[LiveRubricCallTrustAnchorV1, ...]:
        with self._receipt_lock:
            return tuple(
                snapshot_live_rubric_call_trust_anchor(value) for value in self._trust_anchors
            )

    @property
    def attempt_request_anchors(self) -> tuple[LiveRubricAttemptRequestAnchorV1, ...]:
        with self._receipt_lock:
            return tuple(
                snapshot_live_rubric_attempt_request_anchor(value)
                for value in self._attempt_request_anchors
            )

    def _reserve(self, key: tuple[str, str, LiveRubricOperationV1]) -> None:
        with self._receipt_lock:
            if key in self._call_keys:
                raise LiveRubricError("DUPLICATE_PROVIDER_CALL", "rubric provider call repeated")
            self._call_keys.add(key)

    def _register_attempt_request_anchor(
        self,
        *,
        operation: LiveRubricOperationV1,
        task_run_id: str,
        logical_call_id: str,
        attempt_id: str,
        collector_stimulus: RubricEvidenceSnapshotV1,
        current_image: BoundCollectorCurrentImageV1 | None,
        provider_input: dict[str, JsonValue],
        provider_request: CanonicalHistoryPolicyRequestV1,
    ) -> None:
        """Linearize one request proof before transport or for a confirmed begin-TU."""

        with self._receipt_lock:
            if any(value.attempt_id == attempt_id for value in self._attempt_request_anchors):
                raise LiveRubricError(
                    "DUPLICATE_ATTEMPT_ID", "rubric attempt request anchor repeated"
                )
            attempt_order = 1 + sum(
                value.logical_call_id == logical_call_id for value in self._attempt_request_anchors
            )
            anchor = _build_live_rubric_attempt_request_anchor(
                operation=operation,
                task_run_id=task_run_id,
                logical_call_id=logical_call_id,
                attempt_id=attempt_id,
                attempt_order=attempt_order,
                collector_stimulus=collector_stimulus,
                current_image=current_image,
                provider_input=provider_input,
                provider_request=provider_request,
            )
            self._attempt_request_anchors.append(anchor)

    def _publish(
        self,
        value: LiveRubricCallReceiptV1,
        *,
        trust_anchor: LiveRubricCallTrustAnchorV1 | None = None,
    ) -> None:
        if type(value) is not LiveRubricCallReceiptV1:
            raise LiveRubricError("UNTRUSTED_RECEIPT", "rubric call receipt type differs")
        trusted_receipt = snapshot_live_rubric_call_receipt(value)
        trusted_anchor = (
            None if trust_anchor is None else snapshot_live_rubric_call_trust_anchor(trust_anchor)
        )
        if (trusted_anchor is None) != (
            trusted_receipt.execution_scope is LiveRubricExecutionScopeV1.CPU_TEST_LOCAL
        ):
            raise LiveRubricError(
                "TRUST_ANCHOR_CENSUS_MISMATCH",
                "only a completed live rubric call may publish a trust anchor",
            )
        if trusted_anchor is not None and (
            trusted_anchor.operation is not trusted_receipt.operation
            or trusted_anchor.task_run_id != trusted_receipt.task_run_id
            or trusted_anchor.logical_call_id != trusted_receipt.logical_call_id
        ):
            raise LiveRubricError(
                "TRUST_ANCHOR_BINDING_MISMATCH", "rubric receipt and trust anchor differ"
            )
        with self._receipt_lock:
            self._receipts.append(trusted_receipt)
            if trusted_anchor is not None:
                self._trust_anchors.append(trusted_anchor)

    def _attest_extension(
        self,
        value: R24RubricBackendExtensionDescriptorV1,
    ) -> R24RubricBackendExtensionDescriptorV1:
        trusted = snapshot_r24_rubric_backend_extension_descriptor(value)
        if (
            trusted.execution_scope is not self.execution_scope
            or trusted.provider_config_sha256
            != canonical_sha256(cast(JsonValue, self.config_projection))
        ):
            raise LiveRubricError(
                "BACKEND_EXTENSION_BINDING_MISMATCH",
                "backend extension differs from the provider port",
            )
        return trusted

    def context(self, *, task_run_id: str, logical_call_id: str | None) -> _RubricCallContextV1:
        return self._contexts.resolve(task_run_id=task_run_id, logical_call_id=logical_call_id)


class CpuFakeRubricProviderPortV1(_BaseRubricProviderPortV1):
    """Data-only injected fake; it cannot open a provider or acquire a secret."""

    def __init__(
        self, *, generate_outputs: tuple[str, ...], track_outputs: tuple[str, ...]
    ) -> None:
        super().__init__()
        if type(generate_outputs) is not tuple or type(track_outputs) is not tuple:
            raise LiveRubricError("INVALID_FAKE_SCRIPT", "fake outputs must be exact tuples")
        if any(type(item) is not str or not item for item in (*generate_outputs, *track_outputs)):
            raise LiveRubricError("INVALID_FAKE_SCRIPT", "fake output must be nonempty text")
        self._outputs = {
            LiveRubricOperationV1.GENERATE: list(generate_outputs),
            LiveRubricOperationV1.TRACK: list(track_outputs),
        }
        self._script_sha256 = canonical_sha256(
            cast(
                JsonValue,
                {
                    "generate_output_sha256s": [
                        hashlib.sha256(value.encode("utf-8")).hexdigest()
                        for value in generate_outputs
                    ],
                    "track_output_sha256s": [
                        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in track_outputs
                    ],
                },
            )
        )

    @property
    def execution_scope(self) -> LiveRubricExecutionScopeV1:
        return LiveRubricExecutionScopeV1.CPU_TEST_LOCAL

    @property
    def config_projection(self) -> dict[str, JsonValue]:
        return {"execution_scope": self.execution_scope.value, "script_sha256": self._script_sha256}

    def bind_collector_call(
        self,
        *,
        bundle: CollectorEvidenceBundleV1,
        logical_call_id: str,
        actor_request_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        case_lease: CaseExecutionLeaseV1 | None,
        execution_control: PolicyExecutionControlV1 | None = None,
    ) -> None:
        if case_lease is not None:
            raise LiveRubricError("CPU_LIVE_AUTHORITY_FORBIDDEN", "CPU fake rejects case leases")
        image = bind_current_collector_image(bundle, logical_call_id=logical_call_id)
        self._contexts.bind(
            _RubricCallContextV1(
                logical_call_id=logical_call_id,
                task_run_id=image.task_run_id,
                actor_request_sha256=actor_request_sha256,
                deadline_monotonic_ns=deadline_monotonic_ns,
                max_cost_usd_micros=max_cost_usd_micros,
                stimulus=_snapshot_rubric_stimulus(bundle.r23_snapshot),
                image=image,
                case_lease=None,
                execution_control=None,
            )
        )

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
                deadline_monotonic_ns=(1 << 63) - 1,
                max_cost_usd_micros=0,
                stimulus=_snapshot_rubric_stimulus(stimulus),
                image=image,
                case_lease=None,
                execution_control=None,
            )
        )

    def invoke(
        self,
        *,
        operation: LiveRubricOperationV1,
        context: _RubricCallContextV1,
        provider_input: dict[str, JsonValue],
        request: CanonicalHistoryPolicyRequestV1,
        prompt_sha256: str,
        input_schema_version: str,
        output_schema_sha256: str,
        backend_extension: R24RubricBackendExtensionDescriptorV1,
        current_image_binding_sha256: str | None,
    ) -> str:
        if context.case_lease is not None:
            raise LiveRubricError("CPU_LIVE_AUTHORITY_FORBIDDEN", "CPU context carries a lease")
        key = (context.task_run_id, context.logical_call_id, operation)
        self._reserve(key)
        extension = self._attest_extension(backend_extension)
        outputs = self._outputs[operation]
        if not outputs:
            raise LiveRubricError("FAKE_OUTPUT_EXHAUSTED", "injected fake output is absent")
        output = outputs.pop(0)
        output_sha256 = hashlib.sha256(output.encode("utf-8")).hexdigest()
        transport_sha256 = canonical_sha256(
            cast(
                JsonValue,
                {
                    "backend_extension_descriptor_sha256": extension.sha256,
                    "execution_scope": self.execution_scope.value,
                    "operation": operation.value,
                    "provider_request_sha256": request.request_sha256,
                    "script_sha256": self._script_sha256,
                },
            )
        )
        self._publish(
            LiveRubricCallReceiptV1(
                receipt_id=f"r24-rubric-cpu-{hashlib.sha256((context.logical_call_id + operation.value).encode()).hexdigest()[:32]}",
                operation=operation,
                execution_scope=self.execution_scope,
                task_run_id=context.task_run_id,
                logical_call_id=context.logical_call_id,
                backend_extension_descriptor_sha256=extension.sha256,
                r23_compatibility_descriptor_sha256=(extension.r23_compatibility_descriptor_sha256),
                transport_kind=extension.transport_kind,
                transport_authority=extension.transport_authority,
                prompt_sha256=prompt_sha256,
                provider_input_schema_version=input_schema_version,
                provider_output_schema_sha256=output_schema_sha256,
                provider_request_sha256=request.request_sha256,
                provider_output_sha256=output_sha256,
                transport_binding_sha256=transport_sha256,
                pricing_binding_sha256=None,
                current_image_binding_sha256=current_image_binding_sha256,
                manifest_sha256=None,
                preflight_sha256=None,
                case_execution_lease_sha256=None,
                stage_sha256=None,
                attempt_authority_sha256=None,
                attempt_receipt_sha256=None,
                requested_model=None,
                returned_model=None,
                dispatch_count=1,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost_usd_micros=None,
            )
        )
        return output


class ProductionRubricProviderPortV1(_BaseRubricProviderPortV1):
    """Exact case-bound bridge to the cancellable RUBRIC attempt runner."""

    def __init__(self, *, runner: ProductionOpenAIAttemptRunnerV1) -> None:
        super().__init__()
        if type(runner) is not ProductionOpenAIAttemptRunnerV1:
            raise LiveRubricError("UNTRUSTED_PRODUCTION_RUNNER", "runner type differs")
        if (
            runner.role is not LiveAttemptRoleV1.RUBRIC
            or runner.openai_stage.role is not OpenAIRoleV1.RUBRIC
            or runner.openai_stage.model != LIVE_RUBRIC_MODEL
        ):
            raise LiveRubricError("RUBRIC_ROLE_REQUIRED", "runner is not the rubric stage")
        self._runner = runner
        self._authorities: dict[str, _ProductionCaseAuthorityV1] = {}
        self._authority_lock = threading.Lock()

    @property
    def execution_scope(self) -> LiveRubricExecutionScopeV1:
        return LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE

    @property
    def config_projection(self) -> dict[str, JsonValue]:
        stage = self._runner.openai_stage
        return {
            "execution_scope": self.execution_scope.value,
            "factory_binding_sha256": self._runner.factory_binding_sha256,
            "manifest_sha256": self._runner.manifest_sha256,
            "model": stage.model,
            "preflight_sha256": self._runner.preflight_report_sha256,
            "pricing_binding_sha256": self._runner.pricing_binding_sha256,
            "role": self._runner.role.value,
            "stage_sha256": self._runner.openai_stage_sha256,
        }

    def _register_begin_termination_unconfirmed_request_anchor(
        self,
        *,
        operation: LiveRubricOperationV1,
        context: _RubricCallContextV1,
        attempt_id: str,
        transport_binding_sha256: str,
        provider_input: dict[str, JsonValue],
        provider_request: CanonicalHistoryPolicyRequestV1,
    ) -> bool:
        """Retain a request only when ``begin`` published an exact TU terminal.

        Ordinary pre-admission failures have no callable provider attempt and
        remain outside the request-anchor census.  An unreaped START/READY
        child is different: the exact sink terminal proves that a worker may
        still exist, so its already sealed request must survive fallback audit.
        """

        receipt = self._runner.terminal_receipt_for_attempt(attempt_id)
        if receipt is None or receipt.status is not LiveAttemptStatusV1.TERMINATION_UNCONFIRMED:
            return False
        if (
            receipt.attempt_id != attempt_id
            or receipt.role is not LiveAttemptRoleV1.RUBRIC
            or receipt.manifest_sha256 != self._runner.manifest_sha256
            or receipt.preflight_sha256 != self._runner.preflight_report_sha256
            or receipt.case_execution_lease_sha256
            != case_execution_lease_sha256(cast(CaseExecutionLeaseV1, context.case_lease))
            or receipt.stage_sha256 != self._runner.openai_stage_sha256
            or receipt.logical_call_id != context.logical_call_id
            or receipt.actor_request_sha256 != context.actor_request_sha256
            or receipt.request_sha256 != provider_request.request_sha256
            or receipt.transport_binding_sha256 != transport_binding_sha256
            or receipt.pricing_binding_sha256 != self._runner.pricing_binding_sha256
            or receipt.response_envelope_sha256 is not None
            or receipt.requested_model is not None
            or receipt.returned_model is not None
            or receipt.worker_reaped
            or receipt.termination is not LiveAttemptTerminationV1.UNCONFIRMED
            or receipt.failure_code != "TERMINATION_UNCONFIRMED"
        ):
            raise LiveRubricError(
                "ATTEMPT_RECEIPT_DRIFT",
                "begin failure terminal differs from its rubric request",
            )
        self._register_attempt_request_anchor(
            operation=operation,
            task_run_id=context.task_run_id,
            logical_call_id=context.logical_call_id,
            attempt_id=attempt_id,
            collector_stimulus=context.stimulus,
            current_image=(None if operation is LiveRubricOperationV1.GENERATE else context.image),
            provider_input=provider_input,
            provider_request=provider_request,
        )
        return True

    def bind_collector_call(
        self,
        *,
        bundle: CollectorEvidenceBundleV1,
        logical_call_id: str,
        actor_request_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        case_lease: CaseExecutionLeaseV1 | None,
        execution_control: PolicyExecutionControlV1 | None = None,
    ) -> None:
        if type(case_lease) is not CaseExecutionLeaseV1:
            raise LiveRubricError("CASE_LEASE_REQUIRED", "production rubric needs a case lease")
        if not isinstance(execution_control, PolicyExecutionControlV1):
            raise LiveRubricError(
                "EXECUTION_CONTROL_REQUIRED", "production rubric needs the seam fence"
            )
        self.bind_case_authority(
            case_lease=case_lease,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            deadline_monotonic_ns=deadline_monotonic_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            execution_control=execution_control,
        )
        self.bind_collector_projection(
            stimulus=bundle.r23_snapshot,
            current_image_data_url=bundle.gpt56_input.current_image_data_url,
            current_image_sha256=bundle.gpt56_input.current_image_sha256,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
        )

    def bind_case_authority(
        self,
        *,
        case_lease: CaseExecutionLeaseV1,
        logical_call_id: str,
        actor_request_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        execution_control: PolicyExecutionControlV1,
    ) -> None:
        """Pre-register the driver's exact lease before Collector is read."""

        if type(case_lease) is not CaseExecutionLeaseV1:
            raise LiveRubricError("CASE_LEASE_REQUIRED", "production rubric needs a case lease")
        try:
            trusted_lease = self._runner.attest_case_execution_lease(case_lease)
        except Exception as exc:
            raise LiveRubricError("CASE_LEASE_REJECTED", "case lease failed attestation") from exc
        if trusted_lease.request_sha256 != _require_sha256(
            actor_request_sha256, "actor_request_sha256"
        ):
            raise LiveRubricError("CASE_LEASE_BINDING_MISMATCH", "case lease binds another request")
        authority = _ProductionCaseAuthorityV1(
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            deadline_monotonic_ns=deadline_monotonic_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            case_lease=trusted_lease,
            execution_control=execution_control,
        )
        with self._authority_lock:
            prior = self._authorities.get(logical_call_id)
            if prior is not None and prior != authority:
                raise LiveRubricError("LOGICAL_CALL_AUTHORITY_DRIFT", "case authority changed")
            self._authorities[logical_call_id] = authority

    def bind_collector_projection(
        self,
        *,
        stimulus: RubricEvidenceSnapshotV1,
        current_image_data_url: str,
        current_image_sha256: str,
        logical_call_id: str,
        actor_request_sha256: str,
    ) -> None:
        """Complete a pre-registered authority with only current Collector pixels."""

        with self._authority_lock:
            authority = self._authorities.get(logical_call_id)
        if authority is None:
            raise LiveRubricError(
                "CASE_AUTHORITY_NOT_REGISTERED", "driver did not pre-register case authority"
            )
        if authority.actor_request_sha256 != actor_request_sha256:
            raise LiveRubricError(
                "CASE_LEASE_BINDING_MISMATCH", "Collector call binds another actor request"
            )
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
                actor_request_sha256=authority.actor_request_sha256,
                deadline_monotonic_ns=authority.deadline_monotonic_ns,
                max_cost_usd_micros=authority.max_cost_usd_micros,
                stimulus=_snapshot_rubric_stimulus(stimulus),
                image=image,
                case_lease=authority.case_lease,
                execution_control=authority.execution_control,
            )
        )

    def invoke(
        self,
        *,
        operation: LiveRubricOperationV1,
        context: _RubricCallContextV1,
        provider_input: dict[str, JsonValue],
        request: CanonicalHistoryPolicyRequestV1,
        prompt_sha256: str,
        input_schema_version: str,
        output_schema_sha256: str,
        backend_extension: R24RubricBackendExtensionDescriptorV1,
        current_image_binding_sha256: str | None,
    ) -> str:
        if type(context.case_lease) is not CaseExecutionLeaseV1:
            raise LiveRubricError("CASE_LEASE_REQUIRED", "production context lacks a case lease")
        if not isinstance(context.execution_control, PolicyExecutionControlV1):
            raise LiveRubricError(
                "EXECUTION_CONTROL_REQUIRED", "production context lacks the seam fence"
            )
        extension = self._attest_extension(backend_extension)
        key = (context.task_run_id, context.logical_call_id, operation)
        self._reserve(key)
        transport_sha256 = canonical_sha256(
            cast(
                JsonValue,
                {
                    **self.config_projection,
                    "backend_extension_descriptor_sha256": extension.sha256,
                    "input_schema_version": input_schema_version,
                    "operation": operation.value,
                    "output_schema_sha256": output_schema_sha256,
                    "prompt_sha256": prompt_sha256,
                },
            )
        )
        attempt_id = (
            f"r24-rubric-{operation.value.lower()}-"
            f"{hashlib.sha256((context.logical_call_id + request.request_sha256).encode()).hexdigest()[:32]}"
        )
        try:
            call = self._runner.begin(
                case_lease=context.case_lease,
                attempt_id=attempt_id,
                logical_call_id=context.logical_call_id,
                request=request,
                transport_binding_sha256=transport_sha256,
                deadline_monotonic_ns=context.deadline_monotonic_ns,
                max_cost_usd_micros=context.max_cost_usd_micros,
            )
        except Exception as exc:
            # ``begin`` can publish TERMINATION_UNCONFIRMED before returning a
            # callable (for example when a START/READY child cannot be reaped).
            # Preserve that exact request after confirming its sink terminal;
            # ordinary, proved-reaped admission failures remain unanchored.
            try:
                self._register_begin_termination_unconfirmed_request_anchor(
                    operation=operation,
                    context=context,
                    attempt_id=attempt_id,
                    transport_binding_sha256=transport_sha256,
                    provider_input=provider_input,
                    provider_request=request,
                )
            except Exception as anchor_exc:
                raise LiveRubricError(
                    "RUBRIC_REQUEST_ANCHOR_FAILED",
                    "begin failure request proof could not be registered",
                ) from anchor_exc
            raise LiveRubricError(
                "RUBRIC_ATTEMPT_FAILED", "rubric provider attempt failed during admission"
            ) from exc
        try:
            self._register_attempt_request_anchor(
                operation=operation,
                task_run_id=context.task_run_id,
                logical_call_id=context.logical_call_id,
                attempt_id=attempt_id,
                collector_stimulus=context.stimulus,
                current_image=(
                    None if operation is LiveRubricOperationV1.GENERATE else context.image
                ),
                provider_input=provider_input,
                provider_request=request,
            )
        except Exception as exc:
            # A prepared worker must not outlive failure to publish its request
            # proof.  Its terminal receipt, if any, remains in the attempt sink.
            try:
                call.cancel_and_join()
            except Exception:
                pass
            raise LiveRubricError(
                "RUBRIC_REQUEST_ANCHOR_FAILED", "rubric request proof could not be registered"
            ) from exc
        try:
            result = context.execution_control.run_transport(call)
        except Exception as exc:
            # The execution-control deadline can reject before invoking the
            # prepared callable.  Always drive the admitted attempt to a typed
            # terminal receipt; the request anchor remains available for
            # CANCELLED_PRE_DISPATCH or TERMINATION_UNCONFIRMED audit.
            try:
                call.cancel_and_join()
            except Exception:
                pass
            raise LiveRubricError(
                "RUBRIC_ATTEMPT_FAILED", "rubric provider attempt failed"
            ) from exc
        receipt = call.terminal_receipt
        if type(result) is not ResponsesEnvelopeV1 or type(receipt) is not LiveAttemptReceiptV1:
            raise LiveRubricError("INCOMPLETE_ATTEMPT_PROOF", "attempt omitted envelope or receipt")
        if (
            receipt.status is not LiveAttemptStatusV1.COMPLETED
            or receipt.role is not LiveAttemptRoleV1.RUBRIC
            or receipt.manifest_sha256 != self._runner.manifest_sha256
            or receipt.preflight_sha256 != self._runner.preflight_report_sha256
            or receipt.case_execution_lease_sha256
            != case_execution_lease_sha256(context.case_lease)
            or receipt.stage_sha256 != self._runner.openai_stage_sha256
            or receipt.logical_call_id != context.logical_call_id
            or receipt.actor_request_sha256 != context.actor_request_sha256
            or receipt.request_sha256 != request.request_sha256
            or receipt.transport_binding_sha256 != transport_sha256
            or receipt.response_envelope_sha256 != result.sha256
            or result.requested_model != self._runner.openai_stage.model
            or result.returned_model != result.requested_model
            or not receipt.passed
        ):
            raise LiveRubricError("ATTEMPT_RECEIPT_DRIFT", "attempt receipt bindings differ")
        call_receipt = LiveRubricCallReceiptV1(
            receipt_id=f"r24-rubric-live-{hashlib.sha256((attempt_id + receipt.authority_sha256).encode()).hexdigest()[:32]}",
            operation=operation,
            execution_scope=self.execution_scope,
            task_run_id=context.task_run_id,
            logical_call_id=context.logical_call_id,
            backend_extension_descriptor_sha256=extension.sha256,
            r23_compatibility_descriptor_sha256=(extension.r23_compatibility_descriptor_sha256),
            transport_kind=extension.transport_kind,
            transport_authority=extension.transport_authority,
            prompt_sha256=prompt_sha256,
            provider_input_schema_version=input_schema_version,
            provider_output_schema_sha256=output_schema_sha256,
            provider_request_sha256=request.request_sha256,
            provider_output_sha256=result.output_text_sha256,
            transport_binding_sha256=transport_sha256,
            pricing_binding_sha256=receipt.pricing_binding_sha256,
            current_image_binding_sha256=current_image_binding_sha256,
            manifest_sha256=receipt.manifest_sha256,
            preflight_sha256=receipt.preflight_sha256,
            case_execution_lease_sha256=receipt.case_execution_lease_sha256,
            stage_sha256=receipt.stage_sha256,
            attempt_authority_sha256=receipt.authority_sha256,
            attempt_receipt_sha256=live_attempt_receipt_sha256(receipt),
            requested_model=result.requested_model,
            returned_model=result.returned_model,
            dispatch_count=receipt.dispatch_count,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            total_tokens=receipt.total_tokens,
            cost_usd_micros=receipt.cost_usd_micros,
        )
        self._publish(
            call_receipt,
            trust_anchor=_build_live_rubric_call_trust_anchor(
                operation=operation,
                task_run_id=context.task_run_id,
                logical_call_id=context.logical_call_id,
                collector_stimulus=context.stimulus,
                current_image=(
                    None if operation is LiveRubricOperationV1.GENERATE else context.image
                ),
                provider_input=provider_input,
                provider_request=request,
                response_envelope=result,
            ),
        )
        return result.output_text


RubricProviderPortV1 = CpuFakeRubricProviderPortV1 | ProductionRubricProviderPortV1


class LiveOpenAIRubricBackendV1:
    """R2.3 builder/tracker backend backed by one explicit R2.4 provider port."""

    def __init__(self, *, provider_port: RubricProviderPortV1) -> None:
        if type(provider_port) not in {
            CpuFakeRubricProviderPortV1,
            ProductionRubricProviderPortV1,
        }:
            raise LiveRubricError("UNTRUSTED_PROVIDER_PORT", "provider port type differs")
        self._provider = provider_port
        self._generate_schema = live_rubric_generate_schema()
        self._track_schema = live_rubric_track_schema()
        prompt_sha256 = live_rubric_prompt_bundle_sha256()
        root = Path(__file__).resolve().parents[6]
        rubric_schema_sha256 = hashlib.sha256(
            (root / "mobileworld_audit_handoff/schemas/r2_3/rubric.v1.schema.json").read_bytes()
        ).hexdigest()
        tracking_packet_schema_sha256 = hashlib.sha256(
            (
                root / "mobileworld_audit_handoff/schemas/r2_3/tracking_packet.v1.schema.json"
            ).read_bytes()
        ).hexdigest()
        tracker_schema_sha256 = hashlib.sha256(
            (
                root / "mobileworld_audit_handoff/schemas/r2_3/tracker_output.v1.schema.json"
            ).read_bytes()
        ).hexdigest()
        compatibility_config_sha256 = canonical_sha256(
            cast(
                JsonValue,
                {
                    "admission_layer": "R23_CPU_OFFLINE_COMPATIBILITY",
                    "generate_output_schema_sha256": self._generate_schema.sha256,
                    "reasoning_effort": LIVE_RUBRIC_REASONING_EFFORT,
                    "r24_extension_schema_version": (LIVE_RUBRIC_BACKEND_EXTENSION_SCHEMA_VERSION),
                    "track_output_schema_sha256": self._track_schema.sha256,
                },
            )
        )
        live = provider_port.execution_scope is LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE
        self._descriptor = RubricBackendDescriptorV1(
            backend_id="r24-r23-rubric-admission-bridge",
            backend_version=LIVE_RUBRIC_BACKEND_VERSION,
            prompt_sha256=prompt_sha256,
            rubric_schema_sha256=rubric_schema_sha256,
            tracking_packet_schema_sha256=tracking_packet_schema_sha256,
            tracker_schema_sha256=tracker_schema_sha256,
            config_sha256=compatibility_config_sha256,
        )
        self._extension_descriptor = R24RubricBackendExtensionDescriptorV1(
            descriptor_id=("r24-openai-rubric" if live else "r24-cpu-fake-rubric"),
            descriptor_version=LIVE_RUBRIC_BACKEND_VERSION,
            execution_scope=provider_port.execution_scope,
            transport_kind=(
                LiveRubricTransportKindV1.OPENAI_RESPONSES
                if live
                else LiveRubricTransportKindV1.INJECTED_FAKE
            ),
            transport_authority=(
                LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION
                if live
                else LiveRubricTransportAuthorityV1.CPU_OFFLINE_FAKE
            ),
            r23_compatibility_descriptor_sha256=rubric_backend_descriptor_sha256(self._descriptor),
            provider_config_sha256=canonical_sha256(
                cast(JsonValue, provider_port.config_projection)
            ),
            prompt_sha256=prompt_sha256,
            rubric_schema_sha256=rubric_schema_sha256,
            tracking_packet_schema_sha256=tracking_packet_schema_sha256,
            tracker_schema_sha256=tracker_schema_sha256,
            generate_output_schema_sha256=self._generate_schema.sha256,
            track_output_schema_sha256=self._track_schema.sha256,
            configured_model=LIVE_RUBRIC_MODEL,
            external_network_attempted=live,
            model_call_attempted=live,
        )

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        value = self._descriptor
        return RubricBackendDescriptorV1(
            **{name: getattr(value, name) for name in value.__dataclass_fields__}
        )

    @property
    def extension_descriptor(self) -> R24RubricBackendExtensionDescriptorV1:
        return snapshot_r24_rubric_backend_extension_descriptor(self._extension_descriptor)

    @property
    def call_receipts(self) -> tuple[LiveRubricCallReceiptV1, ...]:
        return self._provider.receipts

    def call_receipts_for_call(self, logical_call_id: str) -> tuple[LiveRubricCallReceiptV1, ...]:
        _require_id(logical_call_id, "logical_call_id")
        return tuple(
            value for value in self._provider.receipts if value.logical_call_id == logical_call_id
        )

    def call_trust_anchors_for_call(
        self,
        logical_call_id: str,
    ) -> tuple[LiveRubricCallTrustAnchorV1, ...]:
        """Return detached ephemeral preimages for completed live rubric calls."""

        _require_id(logical_call_id, "logical_call_id")
        return tuple(
            value
            for value in self._provider.trust_anchors
            if value.logical_call_id == logical_call_id
        )

    def attempt_request_anchors_for_call(
        self,
        logical_call_id: str,
    ) -> tuple[LiveRubricAttemptRequestAnchorV1, ...]:
        """Return request preimages registered before transport authorization."""

        _require_id(logical_call_id, "logical_call_id")
        return tuple(
            value
            for value in self._provider.attempt_request_anchors
            if value.logical_call_id == logical_call_id
        )

    def bind_collector_call(
        self,
        *,
        bundle: CollectorEvidenceBundleV1,
        logical_call_id: str,
        actor_request_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        case_lease: CaseExecutionLeaseV1 | None = None,
        execution_control: PolicyExecutionControlV1 | None = None,
    ) -> None:
        self._provider.bind_collector_call(
            bundle=bundle,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            deadline_monotonic_ns=deadline_monotonic_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            case_lease=case_lease,
            execution_control=execution_control,
        )

    def bind_case_authority(
        self,
        *,
        case_lease: CaseExecutionLeaseV1,
        logical_call_id: str,
        actor_request_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
        execution_control: PolicyExecutionControlV1,
    ) -> None:
        """Register production lease authority without exposing it to Coordinator."""

        if type(self._provider) is not ProductionRubricProviderPortV1:
            raise LiveRubricError(
                "LIVE_AUTHORITY_FOR_CPU_FORBIDDEN", "CPU fake backend rejects case authority"
            )
        self._provider.bind_case_authority(
            case_lease=case_lease,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            deadline_monotonic_ns=deadline_monotonic_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            execution_control=execution_control,
        )

    def bind_collector_projection(
        self,
        *,
        stimulus: RubricEvidenceSnapshotV1,
        current_image_data_url: str,
        current_image_sha256: str,
        logical_call_id: str,
        actor_request_sha256: str,
    ) -> None:
        """Coordinator hook carrying only history-free stimulus/current pixels."""

        self._provider.bind_collector_projection(
            stimulus=stimulus,
            current_image_data_url=current_image_data_url,
            current_image_sha256=current_image_sha256,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
        )

    def _invoke(
        self,
        *,
        operation: LiveRubricOperationV1,
        context: _RubricCallContextV1,
        provider_input: dict[str, JsonValue],
        request_kwargs: dict[str, object],
        prompt: str,
        input_schema_version: str,
        output_schema: LiveRubricSchemaSnapshotV1,
        current_image_binding_sha256: str | None,
    ) -> dict[str, JsonValue]:
        request = build_canonical_history_policy_request(request_kwargs)
        expected_request = build_live_rubric_provider_request_v1(
            operation=operation,
            provider_input=provider_input,
            current_image_data_url=(
                None if operation is LiveRubricOperationV1.GENERATE else context.image.data_url
            ),
        )
        if request != expected_request:
            raise LiveRubricError(
                "PROVIDER_REQUEST_BINDING_MISMATCH",
                "dispatch request differs from its module-owned reconstruction",
            )
        request = expected_request
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt_sha256 != live_rubric_operation_prompt_sha256(operation):
            raise LiveRubricError(
                "PROMPT_BINDING_MISMATCH", "rubric prompt differs from module-owned bytes"
            )
        output = self._provider.invoke(
            operation=operation,
            context=context,
            provider_input=provider_input,
            request=request,
            prompt_sha256=prompt_sha256,
            input_schema_version=input_schema_version,
            output_schema_sha256=output_schema.sha256,
            backend_extension=self._extension_descriptor,
            current_image_binding_sha256=current_image_binding_sha256,
        )
        parsed = _strict_json_object(output)
        errors = tuple(Draft202012Validator(output_schema.as_dict()).iter_errors(parsed))
        if errors:
            raise LiveRubricError("PROVIDER_SCHEMA_REJECTED", "provider output violates schema")
        return parsed

    @staticmethod
    def _responses_kwargs(
        *,
        prompt: str,
        input_content: list[dict[str, object]],
        schema: LiveRubricSchemaSnapshotV1,
        max_output_tokens: int,
    ) -> dict[str, object]:
        return {
            "model": LIVE_RUBRIC_MODEL,
            "instructions": prompt,
            "input": [{"role": "user", "content": input_content}],
            "reasoning": {"effort": LIVE_RUBRIC_REASONING_EFFORT},
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
            "max_output_tokens": max_output_tokens,
        }

    def generate(self, request: TaskStartRubricRequestV1) -> MultiPathRubricV1:
        if type(request) is not TaskStartRubricRequestV1 or request.backend != self._descriptor:
            raise LiveRubricError("TASK_START_BINDING_MISMATCH", "task-start request differs")
        context = self._provider.context(task_run_id=request.task_run_id, logical_call_id=None)
        input_value = {
            "backend_extension_descriptor_sha256": self._extension_descriptor.sha256,
            "r23_compatibility_descriptor_sha256": (
                self._extension_descriptor.r23_compatibility_descriptor_sha256
            ),
            "request": task_start_request_projection(request),
            "schema_version": LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
        }
        kwargs = self._responses_kwargs(
            prompt=_GENERATE_INSTRUCTIONS,
            input_content=[
                {
                    "type": "input_text",
                    "text": canonical_json_bytes(cast(JsonValue, input_value)).decode("utf-8"),
                }
            ],
            schema=self._generate_schema,
            max_output_tokens=8192,
        )
        parsed = self._invoke(
            operation=LiveRubricOperationV1.GENERATE,
            context=context,
            provider_input=cast(dict[str, JsonValue], input_value),
            request_kwargs=kwargs,
            prompt=_GENERATE_INSTRUCTIONS,
            input_schema_version=LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
            output_schema=self._generate_schema,
            current_image_binding_sha256=None,
        )
        return _parse_generated_rubric(parsed, request=request, descriptor=self._descriptor)

    def revise(self, request: RubricRevisionRequestV1) -> MultiPathRubricV1:
        del request
        raise R23ContractError(
            "LIVE_REVISION_NOT_AUTHORIZED",
            "R2.4 pilot permits generate-once and does not authorize live rubric revision",
        )

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1:
        if type(packet) is not RubricTrackingPacketV1:
            raise LiveRubricError("UNTRUSTED_TRACKING_PACKET", "tracking packet type differs")
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
            raise LiveRubricError("CURRENT_IMAGE_BINDING_MISMATCH", "packet image binding differs")
        input_value = {
            "backend_extension_descriptor_sha256": self._extension_descriptor.sha256,
            "r23_compatibility_descriptor_sha256": (
                self._extension_descriptor.r23_compatibility_descriptor_sha256
            ),
            "current_image_binding_sha256": image.binding_sha256,
            "packet": tracking_packet_projection(packet),
            "schema_version": LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
        }
        kwargs = self._responses_kwargs(
            prompt=_TRACK_INSTRUCTIONS,
            input_content=[
                {
                    "type": "input_text",
                    "text": canonical_json_bytes(cast(JsonValue, input_value)).decode("utf-8"),
                },
                {
                    "type": "input_image",
                    "image_url": image.data_url,
                    "detail": "high",
                },
            ],
            schema=self._track_schema,
            max_output_tokens=8192,
        )
        parsed = self._invoke(
            operation=LiveRubricOperationV1.TRACK,
            context=context,
            provider_input=cast(dict[str, JsonValue], input_value),
            request_kwargs=kwargs,
            prompt=_TRACK_INSTRUCTIONS,
            input_schema_version=LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
            output_schema=self._track_schema,
            current_image_binding_sha256=image.binding_sha256,
        )
        return _parse_tracker_proposal(parsed, packet=packet)


def _mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise LiveRubricError("PROVIDER_SCHEMA_REJECTED", f"{name} must be an object")
    return value


def _sequence(value: JsonValue, name: str) -> list[JsonValue]:
    if type(value) is not list:
        raise LiveRubricError("PROVIDER_SCHEMA_REJECTED", f"{name} must be an array")
    return value


def _graph_ref(value: JsonValue) -> GraphRefV1:
    item = _mapping(value, "graph reference")
    return GraphRefV1(
        ref_kind=GraphRefKind(cast(str, item["ref_kind"])),
        ref_id=cast(str, item["ref_id"]),
    )


def _parse_generated_rubric(
    value: dict[str, JsonValue],
    *,
    request: TaskStartRubricRequestV1,
    descriptor: RubricBackendDescriptorV1,
) -> MultiPathRubricV1:
    try:
        spans = tuple(
            InstructionSpanV1(
                span_id=cast(str, item["span_id"]),
                role=InstructionSpanRole(cast(str, item["role"])),
                char_start=cast(int, item["char_start"]),
                char_end=cast(int, item["char_end"]),
                utf8_byte_start=cast(int, item["utf8_byte_start"]),
                utf8_byte_end=cast(int, item["utf8_byte_end"]),
                exact_text=cast(str, item["exact_text"]),
                span_sha256=cast(str, item["span_sha256"]),
            )
            for item in (
                _mapping(raw, "instruction span")
                for raw in _sequence(value["instruction_spans"], "instruction_spans")
            )
        )
        milestones = tuple(
            MilestoneV1(
                milestone_id=cast(str, item["milestone_id"]),
                kind=MilestoneKind(cast(str, item["kind"])),
                predicate_kind=MilestonePredicateKind(cast(str, item["predicate_kind"])),
                state_description=cast(str, item["state_description"]),
                description_sha256=cast(str, item["description_sha256"]),
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
                root=(None if item["root"] is None else _graph_ref(item["root"])),
            )
            for item in (_mapping(raw, "path") for raw in _sequence(value["paths"], "paths"))
        )
        output_sha256 = canonical_sha256(cast(JsonValue, value))
        rubric_id = f"r24-rubric-{hashlib.sha256((request.task_run_id + output_sha256).encode()).hexdigest()[:32]}"
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
        raise LiveRubricError(
            "GENERATED_RUBRIC_REJECTED", "generated graph is not admissible"
        ) from exc


def _parse_tracker_proposal(
    value: dict[str, JsonValue],
    *,
    packet: RubricTrackingPacketV1,
) -> RubricTrackerProposalV1:
    try:
        milestone_states = tuple(
            MilestoneStateRecordV1(
                milestone_id=cast(str, item["milestone_id"]),
                state=MilestoneState(cast(str, item["state"])),
                evidence_refs=tuple(
                    MilestoneEvidenceRefV1(
                        evidence_id=cast(str, reference["evidence_id"]),
                        payload_sha256=cast(str, reference["payload_sha256"]),
                        relation=MilestoneEvidenceRelation(cast(str, reference["relation"])),
                    )
                    for reference in (
                        _mapping(raw, "evidence reference")
                        for raw in _sequence(item["evidence_refs"], "evidence_refs")
                    )
                ),
                reason_code=MilestoneReasonCode(cast(str, item["reason_code"])),
            )
            for item in (
                _mapping(raw, "milestone state")
                for raw in _sequence(value["milestone_states"], "milestone_states")
            )
        )
        packet_sha256 = tracking_packet_sha256(packet)
        output_sha256 = canonical_sha256(cast(JsonValue, value))
        return RubricTrackerProposalV1(
            proposal_id=f"r24-proposal-{hashlib.sha256((packet_sha256 + output_sha256).encode()).hexdigest()[:32]}",
            packet_id=packet.packet_id,
            packet_sha256=packet_sha256,
            rubric_binding=rubric_binding_from_packet(packet),
            prior_state_sha256=rubric_tracking_state_sha256(packet.prior_state),
            proposal_status=TrackerProposalStatus(cast(str, value["proposal_status"])),
            milestone_states=milestone_states,
        )
    except (KeyError, TypeError, ValueError, R23ContractError) as exc:
        raise LiveRubricError(
            "TRACKER_PROPOSAL_REJECTED", "tracker proposal is not admissible"
        ) from exc


def rubric_binding_from_packet(packet: RubricTrackingPacketV1):
    """Return the already frozen binding without exposing mutable aliases."""

    # RubricBindingV1 is frozen and recursively primitive, but reconstructing
    # through its public helper would require the full rubric.  Its exact type
    # was validated by RubricTrackingPacketV1.
    from mobile_world.runtime.sentinel.r2_3.contracts import RubricBindingV1

    binding = packet.rubric_binding
    return RubricBindingV1(
        rubric_id=binding.rubric_id,
        rubric_version=binding.rubric_version,
        rubric_sha256=binding.rubric_sha256,
    )


__all__ = [
    "BoundCollectorCurrentImageV1",
    "CpuFakeRubricProviderPortV1",
    "LIVE_RUBRIC_BACKEND_EXTENSION_SCHEMA_VERSION",
    "LIVE_RUBRIC_BACKEND_VERSION",
    "LIVE_RUBRIC_CALL_RECEIPT_SCHEMA_VERSION",
    "LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION",
    "LIVE_RUBRIC_MODEL",
    "LIVE_RUBRIC_REQUEST_PROOF_SCHEMA_VERSION",
    "LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION",
    "LiveOpenAIRubricBackendV1",
    "LiveRubricAttemptRequestAnchorV1",
    "LiveRubricCallReceiptV1",
    "LiveRubricCallTrustAnchorV1",
    "LiveRubricError",
    "LiveRubricExecutionScopeV1",
    "LiveRubricOperationV1",
    "LiveRubricSchemaSnapshotV1",
    "LiveRubricTransportAuthorityV1",
    "LiveRubricTransportKindV1",
    "ProductionRubricProviderPortV1",
    "R24RubricBackendExtensionDescriptorV1",
    "bind_current_collector_image",
    "build_live_rubric_provider_request_v1",
    "live_rubric_attempt_request_proof_projection",
    "live_rubric_call_receipt_projection",
    "live_rubric_call_receipt_sha256",
    "live_rubric_operation_prompt_sha256",
    "live_rubric_prompt_bundle_sha256",
    "live_rubric_generate_schema",
    "live_rubric_track_schema",
    "r24_rubric_backend_extension_descriptor_projection",
    "r24_rubric_backend_extension_descriptor_sha256",
    "rubric_backend_descriptor_projection",
    "snapshot_live_rubric_call_trust_anchor",
    "snapshot_live_rubric_attempt_request_anchor",
    "rubric_backend_descriptor_sha256",
    "snapshot_live_rubric_call_receipt",
    "snapshot_r24_rubric_backend_extension_descriptor",
    "validate_live_rubric_request_anchor_v1",
    "validate_live_rubric_attempt_request_anchor_v1",
    "validate_live_rubric_request_proof_projection_v1",
]
