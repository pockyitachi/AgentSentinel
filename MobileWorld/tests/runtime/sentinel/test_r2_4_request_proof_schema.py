from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_3.contracts import (
    CurrentObservationBindingV1,
    EvidenceMediaType,
    ImageEvidenceProjectionV1,
    RubricBackendDescriptorV1,
    RubricCutoffV1,
    RubricEvidenceRole,
    RubricEvidenceV1,
    RubricSourceEventType,
    TaskInstructionV1,
    TaskStartRubricRequestV1,
    task_start_request_projection,
)
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_4 import rubric_live as rubric_live_module
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptAuthorityV1,
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptPricingV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    live_attempt_authority_sha256,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_run import OpenAIResponsesStageV1, OpenAIRoleV1
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    openai_stage_set_sha256,
    openai_stage_sha256,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
    LIVE_RUBRIC_MODEL,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    LiveRubricTransportAuthorityV1,
    LiveRubricTransportKindV1,
    R24RubricBackendExtensionDescriptorV1,
    build_live_rubric_attempt_constraint_binding_v1,
    build_live_rubric_provider_request_v1,
    live_rubric_attempt_request_proof_projection,
    live_rubric_generate_schema,
    live_rubric_operation_prompt_sha256,
    live_rubric_prompt_bundle_sha256,
    live_rubric_track_schema,
    rubric_backend_descriptor_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
R24_SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_4"
R22_SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_2"
R23_TRACKING_SCHEMA_PATH = (
    REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3/tracking_packet.v1.schema.json"
)


def _sha(value: str | bytes) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validator() -> Draft202012Validator:
    schema = json.loads(
        (R24_SCHEMA_ROOT / "rubric_request_proof.v1.schema.json").read_text(encoding="utf-8")
    )
    tracking_schema = json.loads(R23_TRACKING_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    resolver = RefResolver.from_schema(
        schema,
        store={tracking_schema["$id"]: tracking_schema},
    )
    return Draft202012Validator(schema, resolver=resolver)


def _history_validator() -> Draft202012Validator:
    schema = json.loads(
        (R24_SCHEMA_ROOT / "history_policy_request_proof.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    rubric_schema = json.loads(
        (R24_SCHEMA_ROOT / "rubric_request_proof.v1.schema.json").read_text(encoding="utf-8")
    )
    receipt_schema = json.loads(
        (R22_SCHEMA_ROOT / "policy_receipt.v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    resolver = RefResolver.from_schema(
        schema,
        store={
            rubric_schema["$id"]: rubric_schema,
            receipt_schema["$id"]: receipt_schema,
        },
    )
    return Draft202012Validator(schema, resolver=resolver)


def _stimulus() -> dict[str, JsonValue]:
    screenshot_sha256 = _sha(b"schema-test-image")
    task_text = "Adjust display brightness."
    return {
        "schema_version": "mobileworld.runtime.sentinel-r2.4-rubric-stimulus/v1",
        "task_run_id": "task-run-1",
        "step_id": "step-1",
        "cutoff": {
            "run_id": "run-1",
            "task_run_id": "task-run-1",
            "step_id": "step-1",
            "current_observation_event_id": "event-step-1",
            "cutoff_event_seq": 2,
        },
        "task": {
            "source_event_id": "event-task-1",
            "source_event_seq": 1,
            "exact_text": task_text,
            "text_sha256": _sha(task_text),
            "source_event_type": "task_started",
        },
        "current_observation": {
            "source_event_id": "event-step-1",
            "source_event_seq": 2,
            "screenshot_evidence_id": "evidence-screen-1",
            "screenshot_content_sha256": screenshot_sha256,
            "accessibility_evidence_ids": [],
        },
        "evidence_index": [
            {
                "evidence_id": "evidence-screen-1",
                "role": "CURRENT_UI_SCREENSHOT",
                "source_event_id": "event-step-1",
                "source_event_type": "step_started",
                "source_event_seq": 2,
                "task_run_id": "task-run-1",
                "caused_by_event_id": None,
                "payload_sha256": _sha("screen-payload"),
                "projection": {
                    "content_sha256": screenshot_sha256,
                    "media_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "kind": "IMAGE_REFERENCE",
                },
                "observed_by_cutoff": True,
            }
        ],
    }


def _backend() -> dict[str, JsonValue]:
    return {
        "backend_id": "rubric-backend-v1",
        "backend_version": "r2.4-v1",
        "prompt_sha256": _sha("prompt"),
        "rubric_schema_sha256": _sha("rubric-schema"),
        "tracking_packet_schema_sha256": _sha("tracking-schema"),
        "tracker_schema_sha256": _sha("tracker-schema"),
        "config_sha256": _sha("config"),
        "backend_kind": "INJECTED_FAKE",
        "transport_authority": "CPU_OFFLINE_FAKE",
        "external_network_attempted": False,
        "model_call_attempted": False,
        "local_gpu_used": False,
    }


def _generate_provider_input(stimulus: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "backend_extension_descriptor_sha256": _sha("extension"),
        "r23_compatibility_descriptor_sha256": _sha("r23 descriptor"),
        "request": {
            "request_id": "request-generate-1",
            "task_run_id": stimulus["task_run_id"],
            "task": stimulus["task"],
            "backend": _backend(),
        },
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-rubric-generate-input/v1",
    }


def _tracking_packet(stimulus: dict[str, JsonValue]) -> dict[str, JsonValue]:
    rubric_binding: dict[str, JsonValue] = {
        "rubric_id": "rubric-1",
        "rubric_version": 1,
        "rubric_sha256": _sha("rubric"),
    }
    return {
        "packet_id": "packet-1",
        "logical_call_id": "logical-call-1",
        "task_run_id": stimulus["task_run_id"],
        "step_id": stimulus["step_id"],
        "rubric_binding": rubric_binding,
        "prior_state": {
            "state_id": "state-0",
            "rubric_binding": rubric_binding,
            "state_version": 0,
            "source_packet_id": None,
            "logical_call_id": None,
            "prior_state_sha256": None,
            "milestone_states": [
                {
                    "milestone_id": "milestone-1",
                    "state": "pending",
                    "evidence_refs": [],
                    "reason_code": "NOT_STARTED",
                }
            ],
            "path_states": [
                {"path_id": "path-legal", "state": "viable"},
                {"path_id": "path-other", "state": "unknown"},
            ],
            "frontier": [{"path_id": "path-legal", "milestone_id": "milestone-1"}],
            "topology": {
                "kind": "ISOLATED_HISTORY_FREE",
                "independent_grounding_claim_eligible": True,
            },
            "actor_visible": {
                "enabled": False,
                "exact_text": None,
                "text_sha256": None,
                "content_kind": "DETERMINISTIC_STATUS_ONLY",
                "independently_configured": True,
                "actor_request_injected": False,
                "history_filtering_controlled": False,
            },
            "authority": {
                "factual_truth_authority": False,
                "history_edit_authority": False,
                "action_or_tool_authority": False,
                "archive_execution_authority": False,
            },
            "output_kind": "TRACKING_STATE",
            "schema_version": "mobileworld.runtime.rubric-tracker-output/v1",
        },
        "cutoff": stimulus["cutoff"],
        "task": stimulus["task"],
        "current_observation": stimulus["current_observation"],
        "evidence_index": stimulus["evidence_index"],
        "input_exclusions": {
            "natural_language_actor_history_included": False,
            "history_ir_included": False,
            "history_policy_output_used_as_truth": False,
            "future_event_included": False,
            "task_outcome_included": False,
            "benchmark_checker_included": False,
            "replay_result_included": False,
            "collector_raw_mutated": False,
        },
        "topology": {
            "kind": "ISOLATED_HISTORY_FREE",
            "independent_grounding_claim_eligible": True,
        },
        "schema_version": "mobileworld.runtime.rubric-tracking-packet/v1",
    }


def _proof(operation: LiveRubricOperationV1) -> dict[str, JsonValue]:
    stimulus = _stimulus()
    if operation is LiveRubricOperationV1.GENERATE:
        provider_input = _generate_provider_input(stimulus)
        data_url = None
        current_image: JsonValue = None
    else:
        image_bytes = b"schema-test-image"
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        image_binding_sha256 = _sha("image-binding")
        provider_input = {
            "backend_extension_descriptor_sha256": _sha("extension"),
            "r23_compatibility_descriptor_sha256": _sha("r23 descriptor"),
            "current_image_binding_sha256": image_binding_sha256,
            "packet": _tracking_packet(stimulus),
            "schema_version": "mobileworld.runtime.sentinel-r2.4-live-rubric-track-input/v1",
        }
        current_image = {
            "task_run_id": "task-run-1",
            "logical_call_id": "logical-call-1",
            "source_event_id": "event-step-1",
            "source_event_seq": 2,
            "evidence_id": "evidence-screen-1",
            "content_sha256": _sha(image_bytes),
            "media_type": "image/png",
            "width": 1,
            "height": 1,
            "data_url": data_url,
            "stimulus_sha256": canonical_sha256(cast(JsonValue, stimulus)),
            "binding_sha256": image_binding_sha256,
        }
    request = build_live_rubric_provider_request_v1(
        operation=operation,
        provider_input=provider_input,
        current_image_data_url=data_url,
    )
    stage: dict[str, JsonValue] = {
        "endpoint": "https://api.openai.com/v1/responses",
        "external_network_on_call": True,
        "max_attempts": 1,
        "max_output_tokens": 8192,
        "model": LIVE_RUBRIC_MODEL,
        "model_on_call": True,
        "openai_sdk_version": "1.106.1",
        "role": "RUBRIC",
        "sdk_max_retries": 0,
        "store": False,
        "timeout_ms": 60_000,
        "transport_authority": "EXPLICIT_OWNER_AUTHORIZATION",
        "transport_kind": "OPENAI_RESPONSES",
    }
    pricing: dict[str, JsonValue] = {
        "cached_input_usd_micros_per_million_tokens": 0,
        "effective_at_utc": "2026-09-01T00:00:00Z",
        "input_usd_micros_per_million_tokens": 0,
        "model": LIVE_RUBRIC_MODEL,
        "output_usd_micros_per_million_tokens": 0,
        "pricing_id": "schema-test-pricing",
        "rounding_policy": "CEIL_PER_ATTEMPT_USD_MICRO",
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-pricing/v1",
        "source_sha256": _sha("pricing-source"),
    }
    lease: dict[str, JsonValue] = {
        "actor_call_index": 1,
        "case_id": "case-1",
        "execution_scope": "OWNER_AUTHORIZED_LIVE",
        "expires_at_utc": "2026-09-04T01:00:00Z",
        "factory_binding_sha256": _sha("factory"),
        "host": "QWEN3_VL",
        "issued_at_utc": "2026-09-04T00:00:00Z",
        "manifest_sha256": _sha("manifest"),
        "mode": "SHADOW",
        "openai_stage_set_sha256": _sha("stage-set"),
        "preflight_report_sha256": _sha("preflight"),
        "pricing_binding_sha256": canonical_sha256(cast(JsonValue, pricing)),
        "request_sha256": _sha("actor-request"),
        "reset_seed": None,
        "schema_version": "mobileworld.runtime.sentinel-r2.4-case-execution-lease/v1",
        "stage": "QWEN_LIVE_SMOKE",
        "task_id": "task-1",
        "task_parameters_sha256": None,
    }
    transport: dict[str, JsonValue] = {
        "backend_extension_descriptor_sha256": _sha("extension"),
        "execution_scope": "OWNER_AUTHORIZED_LIVE",
        "factory_binding_sha256": lease["factory_binding_sha256"],
        "input_schema_version": cast(str, provider_input["schema_version"]),
        "manifest_sha256": lease["manifest_sha256"],
        "model": LIVE_RUBRIC_MODEL,
        "operation": operation.value,
        "output_schema_sha256": _sha(f"{operation.value}-output-schema"),
        "preflight_sha256": lease["preflight_report_sha256"],
        "pricing_binding_sha256": lease["pricing_binding_sha256"],
        "prompt_sha256": _sha(f"{operation.value}-prompt"),
        "role": "RUBRIC",
        "stage_sha256": canonical_sha256(cast(JsonValue, stage)),
    }
    authority: dict[str, JsonValue] = {
        "attempt_id": f"attempt-{operation.value.lower()}-1",
        "actor_request_sha256": lease["request_sha256"],
        "case_id": lease["case_id"],
        "case_execution_lease_sha256": canonical_sha256(cast(JsonValue, lease)),
        "deadline_monotonic_ns": 1_000_010_000_000_000,
        "logical_call_id": "logical-call-1",
        "manifest_sha256": lease["manifest_sha256"],
        "max_cost_usd_micros": 1_000_000,
        "max_output_tokens": 8192,
        "preflight_sha256": lease["preflight_report_sha256"],
        "pricing_binding_sha256": lease["pricing_binding_sha256"],
        "request_sha256": request.request_sha256,
        "role": "RUBRIC",
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-attempt-authority/v1",
        "stage_sha256": transport["stage_sha256"],
        "transport_binding_sha256": canonical_sha256(cast(JsonValue, transport)),
    }
    history_stage: dict[str, JsonValue] = {
        **stage,
        "max_output_tokens": 4096,
        "role": "HISTORY_POLICY",
    }
    constraint: dict[str, JsonValue] = {
        "attempt_max_cost_usd_micros": 1_000_000,
        "case_execution_deadline_monotonic_ns": 1_000_010_000_000_000,
        "case_host": lease["host"],
        "case_id": lease["case_id"],
        "case_max_cost_usd_micros": 3_000_000,
        "case_mode": lease["mode"],
        "case_stage": lease["stage"],
        "effective_deadline_monotonic_ns": 1_000_010_000_000_000,
        "history_stage": history_stage,
        "issued_monotonic_ns": 1_000_000_000_000_000,
        "max_actor_calls": 1,
        "max_openai_calls": 3,
        "max_wall_time_seconds": 60,
        "requested_call_deadline_monotonic_ns": 1_000_060_000_000_000,
        "reset_seed": lease["reset_seed"],
        "rubric_stage_sha256": canonical_sha256(cast(JsonValue, stage)),
        "rubric_stage_timeout_ms": 60_000,
        "schema_version": ("mobileworld.runtime.sentinel-r2.4-live-rubric-attempt-constraint/v1"),
        "task_id": lease["task_id"],
        "task_parameters_sha256": lease["task_parameters_sha256"],
    }
    return {
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-rubric-request-proof/v1",
        "operation": operation.value,
        "task_run_id": "task-run-1",
        "logical_call_id": "logical-call-1",
        "attempt_id": f"attempt-{operation.value.lower()}-1",
        "attempt_order": 1 if operation is LiveRubricOperationV1.GENERATE else 2,
        "attempt_role": "RUBRIC",
        "attempt_status": "COMPLETED",
        "attempt_dispatch_count": 1,
        "attempt_receipt_sha256": _sha(f"attempt-{operation.value}"),
        "attempt_authority": authority,
        "attempt_authority_sha256": canonical_sha256(cast(JsonValue, authority)),
        "attempt_constraint_binding": constraint,
        "case_execution_lease": lease,
        "openai_stage": stage,
        "pricing": pricing,
        "transport_binding": transport,
        "backend_extension_descriptor_sha256": _sha("extension"),
        "r23_compatibility_descriptor_sha256": _sha("r23 descriptor"),
        "collector_stimulus": stimulus,
        "collector_stimulus_sha256": canonical_sha256(cast(JsonValue, stimulus)),
        "tracking_packet_sha256": (
            None
            if operation is LiveRubricOperationV1.GENERATE
            else canonical_sha256(cast(JsonValue, provider_input["packet"]))
        ),
        "current_image": current_image,
        "provider_input": provider_input,
        "provider_input_sha256": canonical_sha256(cast(JsonValue, provider_input)),
        "provider_request": cast(JsonValue, json.loads(request.canonical_bytes)),
        "provider_request_sha256": request.request_sha256,
        "provider_request_byte_count": request.byte_count,
    }


def _real_generate_projection() -> dict[str, JsonValue]:
    task_text = "Adjust display brightness."
    task = TaskInstructionV1(
        source_event_id="event-task-1",
        source_event_seq=1,
        exact_text=task_text,
        text_sha256=_sha(task_text),
    )
    screenshot_sha256 = _sha(b"schema-test-image")
    stimulus = RubricEvidenceSnapshotV1(
        task_run_id="task-run-1",
        step_id="step-1",
        cutoff=RubricCutoffV1(
            run_id="run-1",
            task_run_id="task-run-1",
            step_id="step-1",
            current_observation_event_id="event-step-1",
            cutoff_event_seq=2,
        ),
        task=task,
        current_observation=CurrentObservationBindingV1(
            source_event_id="event-step-1",
            source_event_seq=2,
            screenshot_evidence_id="evidence-screen-1",
            screenshot_content_sha256=screenshot_sha256,
            accessibility_evidence_ids=(),
        ),
        evidence_index=(
            RubricEvidenceV1(
                evidence_id="evidence-screen-1",
                role=RubricEvidenceRole.CURRENT_UI_SCREENSHOT,
                source_event_id="event-step-1",
                source_event_type=RubricSourceEventType.STEP_STARTED,
                source_event_seq=2,
                task_run_id="task-run-1",
                caused_by_event_id=None,
                payload_sha256=_sha("screen-payload"),
                projection=ImageEvidenceProjectionV1(
                    content_sha256=screenshot_sha256,
                    media_type=EvidenceMediaType.PNG,
                    width=1,
                    height=1,
                ),
            ),
        ),
    )
    r23_schema_root = REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3"
    descriptor = RubricBackendDescriptorV1(
        backend_id="rubric-backend-v1",
        backend_version="r2.4-v1",
        prompt_sha256=live_rubric_prompt_bundle_sha256(),
        rubric_schema_sha256=_file_sha256(r23_schema_root / "rubric.v1.schema.json"),
        tracking_packet_schema_sha256=_file_sha256(
            r23_schema_root / "tracking_packet.v1.schema.json"
        ),
        tracker_schema_sha256=_file_sha256(r23_schema_root / "tracker_output.v1.schema.json"),
        config_sha256=_sha("r23-config"),
    )
    extension = R24RubricBackendExtensionDescriptorV1(
        descriptor_id="rubric-live-extension-v1",
        descriptor_version="r2.4-v1",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION,
        r23_compatibility_descriptor_sha256=rubric_backend_descriptor_sha256(descriptor),
        provider_config_sha256=_sha("provider-config"),
        prompt_sha256=descriptor.prompt_sha256,
        rubric_schema_sha256=descriptor.rubric_schema_sha256,
        tracking_packet_schema_sha256=descriptor.tracking_packet_schema_sha256,
        tracker_schema_sha256=descriptor.tracker_schema_sha256,
        generate_output_schema_sha256=live_rubric_generate_schema().sha256,
        track_output_schema_sha256=live_rubric_track_schema().sha256,
        configured_model=LIVE_RUBRIC_MODEL,
        external_network_attempted=True,
        model_call_attempted=True,
    )
    task_start = TaskStartRubricRequestV1(
        request_id="request-generate-1",
        task_run_id=stimulus.task_run_id,
        task=task,
        backend=descriptor,
    )
    provider_input: dict[str, JsonValue] = {
        "backend_extension_descriptor_sha256": extension.sha256,
        "r23_compatibility_descriptor_sha256": extension.r23_compatibility_descriptor_sha256,
        "request": cast(JsonValue, task_start_request_projection(task_start)),
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-rubric-generate-input/v1",
    }
    provider_request = build_live_rubric_provider_request_v1(
        operation=LiveRubricOperationV1.GENERATE,
        provider_input=provider_input,
        current_image_data_url=None,
    )
    stage = OpenAIResponsesStageV1(
        role=OpenAIRoleV1.RUBRIC,
        model=LIVE_RUBRIC_MODEL,
        endpoint="https://api.openai.com/v1/responses",
        transport_kind="OPENAI_RESPONSES",
        transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
        openai_sdk_version="1.106.1",
        sdk_max_retries=0,
        external_network_on_call=True,
        model_on_call=True,
        max_output_tokens=8192,
        timeout_ms=60_000,
        max_attempts=1,
        store=False,
    )
    pricing = LiveAttemptPricingV1(
        pricing_id="schema-real-pricing",
        model=LIVE_RUBRIC_MODEL,
        input_usd_micros_per_million_tokens=0,
        cached_input_usd_micros_per_million_tokens=0,
        output_usd_micros_per_million_tokens=0,
        source_sha256=_sha("schema-real-pricing-source"),
        effective_at_utc="2026-09-01T00:00:00Z",
    )
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    lease: dict[str, JsonValue] = {
        "actor_call_index": 1,
        "case_id": "case-1",
        "execution_scope": "OWNER_AUTHORIZED_LIVE",
        "expires_at_utc": "2026-09-04T01:00:00Z",
        "factory_binding_sha256": _sha("factory"),
        "host": "QWEN3_VL",
        "issued_at_utc": "2026-09-04T00:00:00Z",
        "manifest_sha256": _sha("manifest"),
        "mode": "SHADOW",
        "openai_stage_set_sha256": _sha("stage-set"),
        "preflight_report_sha256": _sha("preflight"),
        "pricing_binding_sha256": pricing_sha256,
        "request_sha256": _sha("actor-request"),
        "reset_seed": None,
        "schema_version": "mobileworld.runtime.sentinel-r2.4-case-execution-lease/v1",
        "stage": "QWEN_LIVE_SMOKE",
        "task_id": "task-1",
        "task_parameters_sha256": None,
    }
    transport: dict[str, JsonValue] = {
        "execution_scope": "OWNER_AUTHORIZED_LIVE",
        "factory_binding_sha256": lease["factory_binding_sha256"],
        "manifest_sha256": lease["manifest_sha256"],
        "model": stage.model,
        "preflight_sha256": lease["preflight_report_sha256"],
        "pricing_binding_sha256": pricing_sha256,
        "role": "RUBRIC",
        "stage_sha256": openai_stage_sha256(stage),
        "backend_extension_descriptor_sha256": extension.sha256,
        "input_schema_version": LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
        "operation": "GENERATE",
        "output_schema_sha256": extension.generate_output_schema_sha256,
        "prompt_sha256": live_rubric_operation_prompt_sha256(LiveRubricOperationV1.GENERATE),
    }
    history_stage = OpenAIResponsesStageV1(
        role=OpenAIRoleV1.HISTORY_POLICY,
        model=LIVE_RUBRIC_MODEL,
        endpoint="https://api.openai.com/v1/responses",
        transport_kind="OPENAI_RESPONSES",
        transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
        openai_sdk_version="1.106.1",
        sdk_max_retries=0,
        external_network_on_call=True,
        model_on_call=True,
        max_output_tokens=4096,
        timeout_ms=60_000,
        max_attempts=1,
        store=False,
    )
    lease["openai_stage_set_sha256"] = openai_stage_set_sha256((stage, history_stage))
    constraint = build_live_rubric_attempt_constraint_binding_v1(
        issued_monotonic_ns=1_000_000_000_000_000,
        case_execution_deadline_monotonic_ns=1_000_010_000_000_000,
        history_stage=history_stage,
        rubric_stage=stage,
        case_stage=cast(str, lease["stage"]),
        case_host=cast(str, lease["host"]),
        case_mode=cast(str, lease["mode"]),
        case_id=cast(str, lease["case_id"]),
        task_id=cast(str, lease["task_id"]),
        task_parameters_sha256=cast(str | None, lease["task_parameters_sha256"]),
        reset_seed=cast(int | None, lease["reset_seed"]),
        max_actor_calls=1,
        max_openai_calls=3,
        max_wall_time_seconds=60,
        case_max_cost_usd_micros=3_000_000,
    )
    authority = LiveAttemptAuthorityV1(
        attempt_id="attempt-generate-1",
        role=LiveAttemptRoleV1.RUBRIC,
        manifest_sha256=cast(str, lease["manifest_sha256"]),
        preflight_sha256=cast(str, lease["preflight_report_sha256"]),
        case_execution_lease_sha256=canonical_sha256(cast(JsonValue, lease)),
        stage_sha256=openai_stage_sha256(stage),
        case_id="case-1",
        logical_call_id="logical-call-1",
        actor_request_sha256=cast(str, lease["request_sha256"]),
        request_sha256=provider_request.request_sha256,
        transport_binding_sha256=canonical_sha256(cast(JsonValue, transport)),
        pricing_binding_sha256=pricing_sha256,
        deadline_monotonic_ns=constraint.effective_deadline_monotonic_ns,
        max_cost_usd_micros=constraint.attempt_max_cost_usd_micros,
        max_output_tokens=8192,
    )
    anchor = rubric_live_module._build_live_rubric_attempt_request_anchor(
        operation=LiveRubricOperationV1.GENERATE,
        task_run_id=stimulus.task_run_id,
        logical_call_id="logical-call-1",
        attempt_id="attempt-generate-1",
        attempt_order=1,
        attempt_authority=authority,
        constraint_binding=constraint,
        case_execution_lease=lease,
        openai_stage=stage,
        pricing=pricing,
        transport_binding=transport,
        collector_stimulus=stimulus,
        current_image=None,
        provider_input=provider_input,
        provider_request=provider_request,
    )
    receipt = LiveAttemptReceiptV1(
        attempt_id="attempt-generate-1",
        role=LiveAttemptRoleV1.RUBRIC,
        authority_sha256=live_attempt_authority_sha256(authority),
        manifest_sha256=authority.manifest_sha256,
        preflight_sha256=authority.preflight_sha256,
        case_execution_lease_sha256=authority.case_execution_lease_sha256,
        stage_sha256=authority.stage_sha256,
        case_id="case-1",
        logical_call_id="logical-call-1",
        actor_request_sha256=authority.actor_request_sha256,
        request_sha256=provider_request.request_sha256,
        transport_binding_sha256=authority.transport_binding_sha256,
        pricing_binding_sha256=authority.pricing_binding_sha256,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.COMPLETED,
        dispatch_count=1,
        response_envelope_sha256=_sha("response-envelope"),
        requested_model=LIVE_RUBRIC_MODEL,
        returned_model=LIVE_RUBRIC_MODEL,
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        total_tokens=2,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=0,
        cancellation_requested=False,
        termination=LiveAttemptTerminationV1.NONE,
        worker_pid=1234,
        worker_exit_code=0,
        worker_reaped=True,
        late_output_detected=False,
        duration_ns=1,
        failure_code=None,
    )
    return live_rubric_attempt_request_proof_projection(
        anchor,
        attempt_receipt=receipt,
        backend_extension=extension,
    )


@pytest.mark.parametrize("operation", [LiveRubricOperationV1.GENERATE, LiveRubricOperationV1.TRACK])
def test_checked_in_request_proof_schema_accepts_exact_runtime_envelopes(
    operation: LiveRubricOperationV1,
) -> None:
    _validator().validate(_proof(operation))


def test_checked_in_request_proof_schema_matches_real_projection() -> None:
    projection = _real_generate_projection()

    _validator().validate(projection)
    assert projection["tracking_packet_sha256"] is None


def test_checked_in_history_request_proof_schema_is_closed_and_type_exact() -> None:
    validator = _history_validator()
    schema = cast(dict[str, Any], validator.schema)
    assert schema["additionalProperties"] is False
    authority_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": schema["$defs"],
        "$ref": "#/$defs/attemptAuthority",
    }
    authority = cast(dict[str, Any], deepcopy(_real_generate_projection()["attempt_authority"]))
    authority["role"] = "HISTORY_POLICY"
    authority["max_output_tokens"] = 4096
    Draft202012Validator(authority_schema).validate(authority)
    bool_confused = deepcopy(authority)
    bool_confused["deadline_monotonic_ns"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(authority_schema).validate(bool_confused)


@pytest.mark.parametrize(
    ("status", "dispatch_count"),
    (
        ("FAILED", 1),
        ("CANCELLED_PRE_DISPATCH", 0),
        ("CANCELLED_POST_DISPATCH", 1),
        ("TERMINATION_UNCONFIRMED", 1),
    ),
)
def test_request_proof_schema_allows_noncompleted_attempts_without_call_receipts(
    status: str,
    dispatch_count: int,
) -> None:
    proof = _proof(LiveRubricOperationV1.TRACK)
    proof["attempt_status"] = status
    proof["attempt_dispatch_count"] = dispatch_count
    _validator().validate(proof)


def test_checked_in_request_proof_schema_rejects_open_or_type_confused_proofs() -> None:
    validator = _validator()
    valid = _proof(LiveRubricOperationV1.GENERATE)

    with_extra = cast(dict[str, Any], deepcopy(valid))
    with_extra["untrusted_extension"] = True
    with pytest.raises(ValidationError):
        validator.validate(with_extra)

    bool_byte_count = cast(dict[str, Any], deepcopy(valid))
    bool_byte_count["provider_request_byte_count"] = True
    with pytest.raises(ValidationError):
        validator.validate(bool_byte_count)

    bool_attempt_order = cast(dict[str, Any], deepcopy(valid))
    bool_attempt_order["attempt_order"] = True
    with pytest.raises(ValidationError):
        validator.validate(bool_attempt_order)

    bool_dispatch_count = cast(dict[str, Any], deepcopy(valid))
    bool_dispatch_count["attempt_dispatch_count"] = False
    with pytest.raises(ValidationError):
        validator.validate(bool_dispatch_count)

    invalid_status = cast(dict[str, Any], deepcopy(valid))
    invalid_status["attempt_status"] = "SUCCEEDED"
    with pytest.raises(ValidationError):
        validator.validate(invalid_status)

    completed_without_dispatch = cast(dict[str, Any], deepcopy(valid))
    completed_without_dispatch["attempt_dispatch_count"] = 0
    with pytest.raises(ValidationError):
        validator.validate(completed_without_dispatch)

    pre_dispatch_cancel_after_dispatch = cast(dict[str, Any], deepcopy(valid))
    pre_dispatch_cancel_after_dispatch["attempt_status"] = "CANCELLED_PRE_DISPATCH"
    pre_dispatch_cancel_after_dispatch["attempt_dispatch_count"] = 1
    with pytest.raises(ValidationError):
        validator.validate(pre_dispatch_cancel_after_dispatch)

    operation_drift = cast(dict[str, Any], deepcopy(valid))
    operation_drift["operation"] = "TRACK"
    with pytest.raises(ValidationError):
        validator.validate(operation_drift)
