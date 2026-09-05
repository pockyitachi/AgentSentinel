from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_3.contracts import (
    MilestoneState,
    R23ContractError,
    RubricBackendDescriptorV1,
    RubricBackendKind,
    RubricTrackingPacketV1,
    RubricTransportAuthority,
    TaskStartRubricRequestV1,
    multi_path_rubric_projection,
)
from mobile_world.runtime.sentinel.r2_3.packet import HistoryFreeTrackingPacketBuilderV1
from mobile_world.runtime.sentinel.r2_3.session import RubricSessionStatus, RubricTaskSession
from mobile_world.runtime.sentinel.r2_3.sidecar import (
    RubricEvaluationStatus,
    RubricReceiptOperation,
    RubricReceiptV1,
    rubric_receipt_projection,
    rubric_receipt_sha256,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import build_runtime_history_codec_resolver
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.evidence import CollectorEvidenceFactoryV1
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24OrchestrationError,
    R24RuntimeCoordinatorV1,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LIVE_RUBRIC_MODEL,
    CpuFakeRubricProviderPortV1,
    LiveOpenAIRubricBackendV1,
    LiveRubricError,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    LiveRubricTransportAuthorityV1,
    LiveRubricTransportKindV1,
    ProductionRubricProviderPortV1,
    bind_current_collector_image,
    live_rubric_call_receipt_projection,
    live_rubric_generate_schema,
    live_rubric_track_schema,
    r24_rubric_backend_extension_descriptor_projection,
    rubric_backend_descriptor_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
QWEN_FIXTURE = (
    REPO_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
    "qwen_flat_progress.captured.v1.json"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_image(request: dict[str, JsonValue]) -> Image.Image:
    urls: list[str] = []

    def visit(value: JsonValue) -> None:
        if type(value) is dict:
            image_url = value.get("image_url")
            if value.get("type") == "image_url" and type(image_url) is dict:
                url = image_url.get("url")
                if type(url) is str:
                    urls.append(url)
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(cast(JsonValue, request))
    assert urls
    raw = base64.b64decode(urls[-1].split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as opened:
        opened.load()
        return cast(Image.Image, opened.copy())


def _collector_bundle(tmp_path: Path):
    fixture = cast(dict[str, Any], json.loads(QWEN_FIXTURE.read_text(encoding="utf-8")))
    request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
    codec = build_runtime_history_codec_resolver().by_id(
        "mobileworld.g1.history-codec.qwen-flat-progress"
    )
    history_ir: HistoryIR = codec.extract(cast(JsonValue, request))
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="r2.4-test", worker_id="live-rubric"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    started = capture.start_task(
        task_name="R24LiveRubric",
        task_goal="调整显示亮度。",
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": "qwen", "model": "fixture", "configuration": {}},
        environment={"backend_id": "cpu-fixture", "device_id": "none"},
        whole_task_attempt_index=1,
    )
    assert started is not None
    current = capture.start_step(
        step_index=1,
        observation={
            "screenshot": _request_image(request),
            "accessibility_tree": {"screen": "display", "slider": "brightness"},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert current is not None
    context = SentinelContext(
        logical_call_id="r24-live-rubric-call-1",
        host_id=history_ir.host_id,
    )
    audit_context = AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=current.step_id,
        decision_id=current.decision_id,
        parent_event_id=current.step_started_event_id,
    )
    try:
        with bind_audit_context(audit_context):
            bundle = CollectorEvidenceFactoryV1().bundle_for_call(
                request=cast(JsonValue, request),
                context=context,
                history_ir=history_ir,
            )
    finally:
        run.close()
    return bundle, context, history_ir


def _generate_output(task_text: str) -> str:
    return json.dumps(
        {
            "instruction_spans": [
                {
                    "span_id": "task-goal",
                    "role": "HARD_REQUIREMENT",
                    "char_start": 0,
                    "exact_text": task_text,
                }
            ],
            "milestones": [
                {
                    "milestone_id": "task-goal-state",
                    "kind": "HARD_REQUIREMENT",
                    "predicate_kind": "INSTRUCTION_REQUIREMENT",
                    "state_description": task_text,
                    "instruction_span_id": "task-goal",
                }
            ],
            "gates": [],
            "common_root": None,
            "paths": [
                {
                    "path_id": "direct-path",
                    "kind": "LEGAL_ALTERNATIVE",
                    "root": {"ref_kind": "MILESTONE", "ref_id": "task-goal-state"},
                },
                {"path_id": "other-unknown", "kind": "OTHER_UNKNOWN", "root": None},
            ],
        },
        ensure_ascii=False,
    )


def _multi_span_generate_output(task_text: str) -> str:
    assert task_text == "调整显示亮度。"
    return json.dumps(
        {
            "instruction_spans": [
                {
                    "span_id": "adjust",
                    "role": "HARD_REQUIREMENT",
                    "char_start": 0,
                    "exact_text": "调整",
                },
                {
                    "span_id": "brightness",
                    "role": "HARD_REQUIREMENT",
                    "char_start": 2,
                    "exact_text": "显示亮度",
                },
            ],
            "milestones": [
                {
                    "milestone_id": "adjust-state",
                    "kind": "HARD_REQUIREMENT",
                    "predicate_kind": "INSTRUCTION_REQUIREMENT",
                    "state_description": "调整",
                    "instruction_span_id": "adjust",
                },
                {
                    "milestone_id": "brightness-state",
                    "kind": "HARD_REQUIREMENT",
                    "predicate_kind": "INSTRUCTION_REQUIREMENT",
                    "state_description": "显示亮度",
                    "instruction_span_id": "brightness",
                },
            ],
            "gates": [
                {
                    "gate_id": "all-requirements",
                    "operator": "AND",
                    "children": [
                        {"ref_kind": "MILESTONE", "ref_id": "adjust-state"},
                        {"ref_kind": "MILESTONE", "ref_id": "brightness-state"},
                    ],
                }
            ],
            "common_root": None,
            "paths": [
                {
                    "path_id": "direct-path",
                    "kind": "LEGAL_ALTERNATIVE",
                    "root": {"ref_kind": "GATE", "ref_id": "all-requirements"},
                },
                {"path_id": "other-unknown", "kind": "OTHER_UNKNOWN", "root": None},
            ],
        },
        ensure_ascii=False,
    )


def _track_output(bundle) -> str:
    evidence_id = bundle.r23_snapshot.current_observation.screenshot_evidence_id
    evidence = next(
        item for item in bundle.r23_snapshot.evidence_index if item.evidence_id == evidence_id
    )
    return json.dumps(
        {
            "proposal_status": "COMPLETE",
            "milestone_states": [
                {
                    "milestone_id": "task-goal-state",
                    "state": "satisfied",
                    "evidence_refs": [
                        {
                            "evidence_id": evidence.evidence_id,
                            "relation": "SUPPORTS_STATE",
                        }
                    ],
                    "reason_code": "CURRENT_GUI_SUPPORT",
                }
            ],
        }
    )


def _bound_tracking_case(
    *,
    bundle: Any,
    context: SentinelContext,
    history_ir: HistoryIR,
    generate_output: str,
    track_outputs: tuple[str, ...],
) -> tuple[LiveOpenAIRubricBackendV1, RubricTaskSession, RubricTrackingPacketV1]:
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(generate_output,),
        track_outputs=track_outputs,
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    session = RubricTaskSession(
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        builder_backend=backend,
        tracker_backend=backend,
    )
    generated = session.start()
    assert generated.status is RubricSessionStatus.ADMITTED
    assert generated.rubric is not None and generated.state is not None
    packet = HistoryFreeTrackingPacketBuilderV1().build(
        packet_id="r24-live-rubric-focused-track",
        logical_call_id=context.logical_call_id,
        rubric=generated.rubric,
        prior_state=generated.state,
        snapshot=bundle.r23_snapshot,
    )
    return backend, session, packet


def test_live_rubric_schemas_are_strict_and_checked_in() -> None:
    for snapshot in (live_rubric_generate_schema(), live_rubric_track_schema()):
        Draft202012Validator.check_schema(snapshot.as_dict())
        assert snapshot.sha256 == hashlib.sha256(snapshot.canonical_bytes).hexdigest()

    generate_schema = live_rubric_generate_schema().as_dict()
    definitions = cast(dict[str, JsonValue], generate_schema["$defs"])
    instruction_span = cast(dict[str, JsonValue], definitions["instructionSpan"])
    milestone = cast(dict[str, JsonValue], definitions["milestone"])
    for derived_field in (
        "char_end",
        "utf8_byte_start",
        "utf8_byte_end",
        "span_sha256",
    ):
        assert derived_field not in cast(dict[str, JsonValue], instruction_span["properties"])
        assert derived_field not in cast(list[str], instruction_span["required"])
    assert "description_sha256" not in cast(dict[str, JsonValue], milestone["properties"])
    assert "description_sha256" not in cast(list[str], milestone["required"])

    valid = cast(dict[str, JsonValue], json.loads(_generate_output("Adjust brightness.")))
    validator = Draft202012Validator(generate_schema)
    assert not tuple(validator.iter_errors(valid))
    for container_name, legacy_field, legacy_value in (
        ("instruction_spans", "char_end", 18),
        ("instruction_spans", "utf8_byte_start", 0),
        ("instruction_spans", "utf8_byte_end", 18),
        ("instruction_spans", "span_sha256", _sha("provider-supplied-derived-field")),
        ("milestones", "description_sha256", _sha("provider-supplied-derived-field")),
    ):
        legacy = deepcopy(valid)
        containers = cast(list[dict[str, JsonValue]], legacy[container_name])
        containers[0][legacy_field] = cast(JsonValue, legacy_value)
        assert tuple(validator.iter_errors(legacy))

    request_proof_schema = cast(
        dict[str, JsonValue],
        json.loads(
            (
                REPO_ROOT
                / "mobileworld_audit_handoff/schemas/r2_4/rubric_request_proof.v1.schema.json"
            ).read_text(encoding="utf-8")
        ),
    )
    request_proof_definitions = cast(dict[str, JsonValue], request_proof_schema["$defs"])
    generate_text = cast(
        dict[str, JsonValue], request_proof_definitions["generateTextConfiguration"]
    )
    generate_text_properties = cast(dict[str, JsonValue], generate_text["properties"])
    generate_format = cast(dict[str, JsonValue], generate_text_properties["format"])
    generate_format_properties = cast(dict[str, JsonValue], generate_format["properties"])
    embedded_schema = cast(dict[str, JsonValue], generate_format_properties["schema"])["const"]
    assert canonical_json_bytes(cast(JsonValue, embedded_schema)) == canonical_json_bytes(
        cast(JsonValue, generate_schema)
    )

    track_schema = live_rubric_track_schema().as_dict()
    track_definitions = cast(dict[str, JsonValue], track_schema["$defs"])
    evidence_ref = cast(dict[str, JsonValue], track_definitions["evidenceRef"])
    assert "sha256" not in track_definitions
    assert "payload_sha256" not in cast(dict[str, JsonValue], evidence_ref["properties"])
    assert "payload_sha256" not in cast(list[str], evidence_ref["required"])
    valid_track = cast(
        dict[str, JsonValue],
        {
            "proposal_status": "COMPLETE",
            "milestone_states": [
                {
                    "milestone_id": "task-goal-state",
                    "state": "satisfied",
                    "evidence_refs": [{"evidence_id": "screen", "relation": "SUPPORTS_STATE"}],
                    "reason_code": "CURRENT_GUI_SUPPORT",
                }
            ],
        },
    )
    track_validator = Draft202012Validator(track_schema)
    assert not tuple(track_validator.iter_errors(valid_track))
    legacy_track = deepcopy(valid_track)
    legacy_states = cast(list[dict[str, JsonValue]], legacy_track["milestone_states"])
    legacy_refs = cast(list[dict[str, JsonValue]], legacy_states[0]["evidence_refs"])
    legacy_refs[0]["payload_sha256"] = _sha("provider-supplied-evidence-payload")
    assert tuple(track_validator.iter_errors(legacy_track))

    track_text = cast(dict[str, JsonValue], request_proof_definitions["trackTextConfiguration"])
    track_text_properties = cast(dict[str, JsonValue], track_text["properties"])
    track_format = cast(dict[str, JsonValue], track_text_properties["format"])
    track_format_properties = cast(dict[str, JsonValue], track_format["properties"])
    embedded_track_schema = cast(dict[str, JsonValue], track_format_properties["schema"])["const"]
    assert canonical_json_bytes(cast(JsonValue, embedded_track_schema)) == canonical_json_bytes(
        cast(JsonValue, track_schema)
    )


def test_cpu_fake_generate_once_and_track_current_collector_image(tmp_path: Path) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    track_output = _track_output(bundle)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(track_output,),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    session = RubricTaskSession(
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        builder_backend=backend,
        tracker_backend=backend,
    )

    generated = session.start()
    assert session.start().status is RubricSessionStatus.ADMITTED
    assert generated.status is RubricSessionStatus.ADMITTED
    assert generated.rubric is not None and generated.state is not None
    assert generated.rubric.instruction_spans[0].span_sha256 == _sha(
        bundle.r23_snapshot.task.exact_text
    )
    assert generated.rubric.instruction_spans[0].char_end == len(
        bundle.r23_snapshot.task.exact_text
    )
    assert generated.rubric.instruction_spans[0].utf8_byte_start == 0
    assert generated.rubric.instruction_spans[0].utf8_byte_end == len(
        bundle.r23_snapshot.task.exact_text.encode("utf-8")
    )
    assert generated.rubric.milestones[0].description_sha256 == _sha(
        generated.rubric.milestones[0].state_description
    )
    assert session.task_start_generation_calls == 1
    packet = HistoryFreeTrackingPacketBuilderV1().build(
        packet_id="r24-live-rubric-packet-1",
        logical_call_id=context.logical_call_id,
        rubric=generated.rubric,
        prior_state=generated.state,
        snapshot=bundle.r23_snapshot,
    )
    tracked = session.track(packet)

    assert tracked.status is RubricSessionStatus.ADMITTED
    assert tracked.state is not None
    assert tracked.state.milestone_states[0].state is MilestoneState.SATISFIED
    cited = tracked.state.milestone_states[0].evidence_refs[0]
    packet_evidence = next(
        item for item in packet.evidence_index if item.evidence_id == cited.evidence_id
    )
    assert cited.payload_sha256 == packet_evidence.payload_sha256
    assert [receipt.operation for receipt in backend.call_receipts] == [
        LiveRubricOperationV1.GENERATE,
        LiveRubricOperationV1.TRACK,
    ]
    assert all(
        receipt.execution_scope is LiveRubricExecutionScopeV1.CPU_TEST_LOCAL
        and receipt.manifest_sha256 is None
        and receipt.attempt_receipt_sha256 is None
        and not receipt.actor_history_included
        and not receipt.history_ir_included
        for receipt in backend.call_receipts
    )
    assert backend.call_receipts[0].current_image_binding_sha256 is None
    assert (
        backend.call_receipts[1].current_image_binding_sha256
        == bind_current_collector_image(
            bundle, logical_call_id=context.logical_call_id
        ).binding_sha256
    )
    rubric_receipts = cast(Any, session.receipt_sink).receipts
    assert all(receipt.backend_kind == "INJECTED_FAKE" for receipt in rubric_receipts)
    assert all(receipt.external_network_attempted is False for receipt in rubric_receipts)
    track_call_receipt = backend.call_receipts[1]
    track_r23_receipt = next(
        receipt for receipt in rubric_receipts if receipt.operation is RubricReceiptOperation.TRACK
    )
    assert track_call_receipt.provider_output_sha256 == _sha(track_output)
    assert track_r23_receipt.raw_backend_output_sha256 is not None
    assert track_r23_receipt.raw_backend_output_sha256 != track_call_receipt.provider_output_sha256


def test_track_normalizes_exact_live_pending_preserve_output(tmp_path: Path) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    generated_value = cast(
        dict[str, JsonValue],
        json.loads(_generate_output(bundle.r23_snapshot.task.exact_text)),
    )
    milestones = cast(list[dict[str, JsonValue]], generated_value["milestones"])
    milestones[0]["milestone_id"] = "m_adjust_brightness"
    paths = cast(list[dict[str, JsonValue]], generated_value["paths"])
    legal_root = cast(dict[str, JsonValue], paths[0]["root"])
    legal_root["ref_id"] = "m_adjust_brightness"
    recovered_output = (
        '{"milestone_states":[{"evidence_refs":[],"milestone_id":'
        '"m_adjust_brightness","reason_code":"PRESERVE_PRIOR_STATE","state":"pending"}],'
        '"proposal_status":"COMPLETE"}'
    )
    recovered_sha256 = "5e612326a4fda3e07507ab9f5f14e3ef7bc3196b26ba9cab2bce95d4698b5011"
    assert len(recovered_output.encode("utf-8")) == 164
    assert _sha(recovered_output) == recovered_sha256
    backend, session, packet = _bound_tracking_case(
        bundle=bundle,
        context=context,
        history_ir=history_ir,
        generate_output=json.dumps(generated_value, ensure_ascii=False),
        track_outputs=(recovered_output,),
    )

    tracked = session.track(packet)

    assert tracked.status is RubricSessionStatus.ADMITTED
    assert tracked.state is not None and tracked.proposal is not None
    state = tracked.state.milestone_states[0]
    assert state.state is MilestoneState.PENDING
    assert state.evidence_refs == ()
    assert state.reason_code.value == "NOT_STARTED"
    call_receipt = backend.call_receipts[-1]
    assert call_receipt.provider_output_sha256 == recovered_sha256
    r23_receipt = next(
        receipt
        for receipt in cast(Any, session.receipt_sink).receipts
        if receipt.operation is RubricReceiptOperation.TRACK
    )
    assert r23_receipt.raw_backend_output_sha256 is not None
    assert r23_receipt.raw_backend_output_sha256 != recovered_sha256


def test_track_keeps_valid_abstain_with_empty_evidence(tmp_path: Path) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    abstain_output = json.dumps(
        {
            "proposal_status": "ABSTAIN",
            "milestone_states": [
                {
                    "milestone_id": "task-goal-state",
                    "state": "unknown",
                    "evidence_refs": [],
                    "reason_code": "INSUFFICIENT_EVIDENCE",
                }
            ],
        }
    )
    _, session, packet = _bound_tracking_case(
        bundle=bundle,
        context=context,
        history_ir=history_ir,
        generate_output=_generate_output(bundle.r23_snapshot.task.exact_text),
        track_outputs=(abstain_output,),
    )

    tracked = session.track(packet)

    assert tracked.status is RubricSessionStatus.ADMITTED
    assert tracked.state is not None
    state = tracked.state.milestone_states[0]
    assert state.state is MilestoneState.UNKNOWN
    assert state.evidence_refs == ()
    assert state.reason_code.value == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize(
    "malformation",
    (
        "unknown_evidence",
        "duplicate_evidence",
        "wrong_relation",
        "wrong_reason",
        "wrong_milestone",
        "preserve_with_evidence",
        "abstain_nonunknown",
    ),
)
def test_track_hydration_preserves_r23_semantic_rejections(
    tmp_path: Path,
    malformation: str,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    output = cast(dict[str, JsonValue], json.loads(_track_output(bundle)))
    states = cast(list[dict[str, JsonValue]], output["milestone_states"])
    state = states[0]
    refs = cast(list[dict[str, JsonValue]], state["evidence_refs"])
    if malformation == "unknown_evidence":
        refs[0]["evidence_id"] = "unknown-evidence"
    elif malformation == "duplicate_evidence":
        refs.append(deepcopy(refs[0]))
    elif malformation == "wrong_relation":
        refs[0]["relation"] = "REFUTES_STATE"
    elif malformation == "wrong_reason":
        state["reason_code"] = "COMPLETED_TRANSITION_SUPPORT"
    elif malformation == "wrong_milestone":
        state["milestone_id"] = "unknown-milestone"
    elif malformation == "preserve_with_evidence":
        state["state"] = "pending"
        state["reason_code"] = "PRESERVE_PRIOR_STATE"
    else:
        output["proposal_status"] = "ABSTAIN"
    backend, session, packet = _bound_tracking_case(
        bundle=bundle,
        context=context,
        history_ir=history_ir,
        generate_output=_generate_output(bundle.r23_snapshot.task.exact_text),
        track_outputs=(json.dumps(output, ensure_ascii=False),),
    )

    if malformation in {
        "unknown_evidence",
        "duplicate_evidence",
        "wrong_relation",
        "preserve_with_evidence",
        "abstain_nonunknown",
    }:
        with pytest.raises(LiveRubricError, match="TRACKER_PROPOSAL_REJECTED"):
            backend.track(packet)
    else:
        tracked = session.track(packet)
        assert tracked.status is RubricSessionStatus.FALLBACK
        assert tracked.state == packet.prior_state
        assert tracked.proposal is None


def test_track_does_not_normalize_pending_preserve_from_nonpending_prior(
    tmp_path: Path,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    first_output = _track_output(bundle)
    pending_preserve = json.dumps(
        {
            "proposal_status": "COMPLETE",
            "milestone_states": [
                {
                    "milestone_id": "task-goal-state",
                    "state": "pending",
                    "evidence_refs": [],
                    "reason_code": "PRESERVE_PRIOR_STATE",
                }
            ],
        }
    )
    backend, session, first_packet = _bound_tracking_case(
        bundle=bundle,
        context=context,
        history_ir=history_ir,
        generate_output=_generate_output(bundle.r23_snapshot.task.exact_text),
        track_outputs=(first_output, pending_preserve),
    )
    first = session.track(first_packet)
    assert first.status is RubricSessionStatus.ADMITTED
    assert first.state is not None and first.rubric is not None
    second_logical_call_id = f"{context.logical_call_id}-second"
    backend.bind_collector_projection(
        stimulus=bundle.r23_snapshot,
        current_image_data_url=bundle.gpt56_input.current_image_data_url,
        current_image_sha256=bundle.gpt56_input.current_image_sha256,
        logical_call_id=second_logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
    )
    second_packet = HistoryFreeTrackingPacketBuilderV1().build(
        packet_id="r24-live-rubric-nonpending-prior",
        logical_call_id=second_logical_call_id,
        rubric=first.rubric,
        prior_state=first.state,
        snapshot=bundle.r23_snapshot,
    )

    second = session.track(second_packet)

    assert second.status is RubricSessionStatus.FALLBACK
    assert second.state == second_packet.prior_state
    assert second.proposal is None


def test_generate_derives_multispan_unicode_and_utf8_coordinates(tmp_path: Path) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    task_text = bundle.r23_snapshot.task.exact_text
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_multi_span_generate_output(task_text),),
        track_outputs=(),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    generated = backend.generate(
        TaskStartRubricRequestV1(
            request_id="r24-live-rubric-multispan",
            task_run_id=bundle.r23_snapshot.task_run_id,
            task=bundle.r23_snapshot.task,
            backend=backend.descriptor,
        )
    )

    assert tuple(
        (
            span.char_start,
            span.char_end,
            span.utf8_byte_start,
            span.utf8_byte_end,
            span.exact_text,
        )
        for span in generated.instruction_spans
    ) == ((0, 2, 0, 6, "调整"), (2, 6, 6, 18, "显示亮度"))


@pytest.mark.parametrize("drift", ("char_start", "out_of_range", "overlap", "text"))
def test_generate_derives_coordinates_and_hashes_but_rejects_instruction_span_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    task_text = bundle.r23_snapshot.task.exact_text
    output = cast(
        dict[str, JsonValue],
        json.loads(
            _multi_span_generate_output(task_text)
            if drift == "overlap"
            else _generate_output(task_text)
        ),
    )
    spans = cast(list[dict[str, JsonValue]], output["instruction_spans"])
    span = spans[0]
    if drift == "char_start":
        span["char_start"] = 1
    elif drift == "out_of_range":
        span["char_start"] = len(task_text) + 1
    elif drift == "overlap":
        span["exact_text"] = "调整显示"
        milestones = cast(list[dict[str, JsonValue]], output["milestones"])
        milestones[0]["state_description"] = "调整显示"
    else:
        span["exact_text"] = task_text[:-1]
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(json.dumps(output, ensure_ascii=False),),
        track_outputs=(),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    request = TaskStartRubricRequestV1(
        request_id=f"r24-live-rubric-{drift}-drift",
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        backend=backend.descriptor,
    )

    with pytest.raises(LiveRubricError, match="GENERATED_RUBRIC_REJECTED"):
        backend.generate(request)


@pytest.mark.parametrize("malformation", ("role", "unknown_graph_reference"))
def test_generate_derivation_does_not_weaken_semantic_graph_validation(
    tmp_path: Path,
    malformation: str,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    output = cast(
        dict[str, JsonValue],
        json.loads(_generate_output(bundle.r23_snapshot.task.exact_text)),
    )
    if malformation == "role":
        spans = cast(list[dict[str, JsonValue]], output["instruction_spans"])
        spans[0]["role"] = "CONSTRAINT"
    else:
        paths = cast(list[dict[str, JsonValue]], output["paths"])
        root = cast(dict[str, JsonValue], paths[0]["root"])
        root["ref_id"] = "unknown-milestone"
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(json.dumps(output, ensure_ascii=False),),
        track_outputs=(),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    request = TaskStartRubricRequestV1(
        request_id=f"r24-live-rubric-{malformation}",
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        backend=backend.descriptor,
    )

    with pytest.raises(LiveRubricError, match="GENERATED_RUBRIC_REJECTED"):
        backend.generate(request)


def test_tracker_rejects_image_pixels_not_bound_to_packet(tmp_path: Path) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(_track_output(bundle),),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    session = RubricTaskSession(
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        builder_backend=backend,
        tracker_backend=backend,
    )
    generated = session.start()
    assert generated.rubric is not None and generated.state is not None
    packet = HistoryFreeTrackingPacketBuilderV1().build(
        packet_id="r24-live-rubric-packet-image-drift",
        logical_call_id=context.logical_call_id,
        rubric=generated.rubric,
        prior_state=generated.state,
        snapshot=bundle.r23_snapshot,
    )
    object.__setattr__(
        backend._provider.context(
            task_run_id=packet.task_run_id, logical_call_id=packet.logical_call_id
        ).image,
        "content_sha256",
        _sha("different image"),
    )

    with pytest.raises(LiveRubricError, match="CURRENT_IMAGE_BINDING_MISMATCH"):
        backend.track(packet)


def test_runtime_coordinator_binds_live_backend_before_generate_and_track(
    tmp_path: Path,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(_track_output(bundle),),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    collector = CollectorEvidenceFactoryV1()
    collector.bundle_for_call = cast(Any, lambda **_kwargs: bundle)
    coordinator = R24RuntimeCoordinatorV1(
        collector=collector,
        session_factory=lambda task_run_id, task: RubricTaskSession(
            task_run_id=task_run_id,
            task=task,
            builder_backend=backend,
            tracker_backend=backend,
        ),
        rubric_call_observer=backend,
    )

    result = coordinator(cast(JsonValue, {}), context, history_ir)

    assert result.packet_sha256 == bundle.gpt56_input.packet_sha256
    assert [receipt.operation for receipt in backend.call_receipts] == [
        LiveRubricOperationV1.GENERATE,
        LiveRubricOperationV1.TRACK,
    ]
    record = coordinator.record_for(context.logical_call_id)
    assert record is not None and record.rubric_result.status is RubricSessionStatus.ADMITTED
    assert (
        coordinator.stimulus_sha256_for_call(context.logical_call_id)
        == record.history_free_stimulus_sha256
    )
    assert (
        coordinator.tracking_packet_sha256_for_call(context.logical_call_id)
        == record.tracking_packet_sha256
    )


def test_runtime_coordinator_retains_pre_dispatch_roots_when_tracking_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(_track_output(bundle),),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)

    def fail_track(_packet: object) -> object:
        raise RuntimeError("injected post-packet tracking failure")

    monkeypatch.setattr(backend, "track", fail_track)
    collector = CollectorEvidenceFactoryV1()
    collector.bundle_for_call = cast(Any, lambda **_kwargs: bundle)
    coordinator = R24RuntimeCoordinatorV1(
        collector=collector,
        session_factory=lambda task_run_id, task: RubricTaskSession(
            task_run_id=task_run_id,
            task=task,
            builder_backend=backend,
            tracker_backend=backend,
        ),
        rubric_call_observer=backend,
    )

    with pytest.raises(R24OrchestrationError, match="RUBRIC_TRACK_FALLBACK"):
        coordinator(cast(JsonValue, {}), context, history_ir)

    record = coordinator.record_for(context.logical_call_id)
    assert record is not None
    assert coordinator.stimulus_sha256_for_call(context.logical_call_id) == (
        record.history_free_stimulus_sha256
    )
    assert coordinator.tracking_packet_sha256_for_call(context.logical_call_id) == (
        record.tracking_packet_sha256
    )


def test_production_port_rejects_arbitrary_or_history_policy_runner() -> None:
    with pytest.raises(LiveRubricError, match="UNTRUSTED_PRODUCTION_RUNNER"):
        ProductionRubricProviderPortV1(runner=cast(Any, object()))


def test_r23_backend_provenance_remains_cpu_offline_fake_only() -> None:
    base = dict(
        backend_id="rubric-backend",
        backend_version="v1",
        prompt_sha256=_sha("prompt"),
        rubric_schema_sha256=_sha("rubric schema"),
        tracking_packet_schema_sha256=_sha("packet schema"),
        tracker_schema_sha256=_sha("tracker schema"),
        config_sha256=_sha("config"),
    )
    fake = RubricBackendDescriptorV1(**base)
    assert fake.backend_kind is RubricBackendKind.INJECTED_FAKE
    assert tuple(item.value for item in RubricBackendKind) == ("INJECTED_FAKE",)
    assert tuple(item.value for item in RubricTransportAuthority) == ("CPU_OFFLINE_FAKE",)
    with pytest.raises(R23ContractError, match="INVALID_ENUM"):
        RubricBackendDescriptorV1(
            **base,
            backend_kind=cast(Any, "OPENAI_RESPONSES"),
            transport_authority=cast(Any, "EXPLICIT_OWNER_AUTHORIZATION"),
            external_network_attempted=True,
            model_call_attempted=True,
        )
    with pytest.raises(R23ContractError, match="UNAUTHORIZED_RESOURCE_USE"):
        RubricBackendDescriptorV1(
            **base,
            backend_kind=RubricBackendKind.INJECTED_FAKE,
            transport_authority=RubricTransportAuthority.CPU_OFFLINE_FAKE,
            external_network_attempted=True,
        )


def test_r23_v1_bytes_stay_frozen_and_schemas_reject_live_provenance(
    tmp_path: Path,
) -> None:
    frozen_sha256s = {
        "MobileWorld/src/mobile_world/runtime/sentinel/r2_3/contracts.py": (
            "faccf4ca02467f88cf5124db63a2187b3b30e69bab9acc679ebadafb9919a0a0"
        ),
        "MobileWorld/src/mobile_world/runtime/sentinel/r2_3/session.py": (
            "8bd78e34570b468ead209a6ee98d6d6426a7f618a92bada592c25e8cf13c8340"
        ),
        "MobileWorld/src/mobile_world/runtime/sentinel/r2_3/sidecar.py": (
            "619e8afd290e6c4e2aee1f9e99e14a604780f234a3e121b603b8fd56fb65a626"
        ),
        "mobileworld_audit_handoff/schemas/r2_3/rubric.v1.schema.json": (
            "098e066db2ec24dade5f2b93a1c0d89d0736c8b0947cd157d5f25a612af50f94"
        ),
        "mobileworld_audit_handoff/schemas/r2_3/rubric_receipt.v1.schema.json": (
            "7d34084a5d65b529ed091064710c256c9d999630e6d5f8249de667412a4baf19"
        ),
    }
    for relative, expected in frozen_sha256s.items():
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == expected

    receipt = RubricReceiptV1(
        receipt_id="rubric-receipt",
        task_run_id="task-run",
        logical_call_id="logical-call",
        operation=RubricReceiptOperation.TRACK,
        topology_kind="ISOLATED_HISTORY_FREE",
        status=RubricEvaluationStatus.ADMITTED,
        fallback_code=None,
        backend_id="backend",
        backend_version="v1",
        prompt_sha256=_sha("prompt"),
        input_schema_sha256=_sha("input"),
        output_schema_sha256=_sha("output"),
        config_sha256=_sha("config"),
        input_sha256=_sha("input value"),
        raw_backend_output_sha256=_sha("raw"),
        parsed_output_sha256=_sha("parsed"),
        admitted_output_sha256=_sha("admitted"),
        rubric_id="rubric",
        rubric_version=1,
        rubric_sha256=_sha("rubric"),
        prior_state_sha256=_sha("prior"),
        final_state_sha256=_sha("final"),
        backend_calls=1,
        task_start_generation_calls=1,
        explicit_revision_calls=0,
        runtime_tracking_calls=1,
        relevance_link_calls=0,
        packet_build_latency_ns=1,
        backend_latency_ns=2,
        admission_latency_ns=1,
        state_update_latency_ns=1,
        total_latency_ns=5,
    )
    fake_projection = rubric_receipt_projection(receipt)
    assert fake_projection["backend_kind"] == "INJECTED_FAKE"
    assert fake_projection["transport_authority"] == "CPU_OFFLINE_FAKE"
    assert fake_projection["external_network_attempted"] is False
    assert (
        rubric_receipt_sha256(receipt)
        == hashlib.sha256(
            json.dumps(
                fake_projection,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )

    receipt_schema = json.loads(
        (
            REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3/rubric_receipt.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(receipt_schema)
    validator.validate(fake_projection)
    with pytest.raises(Exception):
        validator.validate(
            {
                **fake_projection,
                "backend_kind": "OPENAI_RESPONSES",
                "transport_authority": "EXPLICIT_OWNER_AUTHORIZATION",
                "external_network_attempted": True,
                "model_call_attempted": True,
            }
        )

    bundle, context, history_ir = _collector_bundle(tmp_path)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    generated = RubricTaskSession(
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        builder_backend=backend,
        tracker_backend=backend,
    ).start()
    assert generated.rubric is not None
    rubric_schema = json.loads(
        (REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3/rubric.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    rubric_projection = multi_path_rubric_projection(generated.rubric)
    Draft202012Validator(rubric_schema).validate(rubric_projection)
    live_projection = deepcopy(rubric_projection)
    backend_projection = cast(dict[str, JsonValue], live_projection["backend"])
    backend_projection.update(
        {
            "backend_kind": "OPENAI_RESPONSES",
            "transport_authority": "EXPLICIT_OWNER_AUTHORIZATION",
            "external_network_attempted": True,
            "model_call_attempted": True,
        }
    )
    with pytest.raises(Exception):
        Draft202012Validator(rubric_schema).validate(live_projection)


def test_r24_extension_descriptor_and_call_receipt_schemas_bind_live_models(
    tmp_path: Path,
) -> None:
    bundle, context, history_ir = _collector_bundle(tmp_path)
    port = CpuFakeRubricProviderPortV1(
        generate_outputs=(_generate_output(bundle.r23_snapshot.task.exact_text),),
        track_outputs=(),
    )
    backend = LiveOpenAIRubricBackendV1(provider_port=port)
    backend.bind_collector_call(
        bundle=bundle,
        logical_call_id=context.logical_call_id,
        actor_request_sha256=history_ir.raw_request_sha256,
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        max_cost_usd_micros=10,
    )
    generated = RubricTaskSession(
        task_run_id=bundle.r23_snapshot.task_run_id,
        task=bundle.r23_snapshot.task,
        builder_backend=backend,
        tracker_backend=backend,
    ).start()
    assert generated.status is RubricSessionStatus.ADMITTED
    assert backend.descriptor.backend_kind is RubricBackendKind.INJECTED_FAKE
    assert backend.descriptor.transport_authority is RubricTransportAuthority.CPU_OFFLINE_FAKE
    assert backend.descriptor.external_network_attempted is False
    assert backend.descriptor.model_call_attempted is False

    descriptor_schema = Draft202012Validator(
        json.loads(
            (
                REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_4/"
                "rubric_backend_extension.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
    )
    cpu_descriptor = backend.extension_descriptor
    cpu_descriptor_projection = r24_rubric_backend_extension_descriptor_projection(cpu_descriptor)
    descriptor_schema.validate(cpu_descriptor_projection)
    assert cpu_descriptor.r23_compatibility_descriptor_sha256 == rubric_backend_descriptor_sha256(
        backend.descriptor
    )
    live_descriptor = replace(
        cpu_descriptor,
        descriptor_id="r24-openai-rubric",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=(LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION),
        external_network_attempted=True,
        model_call_attempted=True,
    )
    descriptor_schema.validate(r24_rubric_backend_extension_descriptor_projection(live_descriptor))
    with pytest.raises(LiveRubricError, match="MODEL_BINDING_MISMATCH"):
        replace(live_descriptor, configured_model="another-model")

    assert len(backend.call_receipts) == 1
    cpu_receipt = backend.call_receipts[0]
    receipt_schema = Draft202012Validator(
        json.loads(
            (
                REPO_ROOT
                / "mobileworld_audit_handoff/schemas/r2_4/rubric_call_receipt.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
    )
    receipt_schema.validate(live_rubric_call_receipt_projection(cpu_receipt))
    assert cpu_receipt.pricing_binding_sha256 is None
    with pytest.raises(LiveRubricError, match="FALSE_LIVE_CLAIM"):
        replace(cpu_receipt, pricing_binding_sha256=_sha("false CPU pricing"))
    with pytest.raises(Exception):
        receipt_schema.validate(
            {
                **live_rubric_call_receipt_projection(cpu_receipt),
                "pricing_binding_sha256": _sha("false CPU pricing"),
            }
        )
    live_receipt = replace(
        cpu_receipt,
        receipt_id="r24-rubric-live-model-binding",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        backend_extension_descriptor_sha256=live_descriptor.sha256,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=(LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION),
        manifest_sha256=_sha("manifest"),
        preflight_sha256=_sha("preflight"),
        case_execution_lease_sha256=_sha("case lease"),
        stage_sha256=_sha("stage"),
        attempt_authority_sha256=_sha("attempt authority"),
        attempt_receipt_sha256=_sha("attempt receipt"),
        pricing_binding_sha256=_sha("pricing binding"),
        requested_model=LIVE_RUBRIC_MODEL,
        returned_model=LIVE_RUBRIC_MODEL,
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        cost_usd_micros=3,
    )
    live_projection = live_rubric_call_receipt_projection(live_receipt)
    receipt_schema.validate(live_projection)
    assert live_projection["requested_model"] == LIVE_RUBRIC_MODEL
    assert live_projection["returned_model"] == LIVE_RUBRIC_MODEL
    assert live_projection["pricing_binding_sha256"] == _sha("pricing binding")
    with pytest.raises(LiveRubricError, match="INCOMPLETE_LIVE_RECEIPT"):
        replace(live_receipt, pricing_binding_sha256=None)
    without_pricing = dict(live_projection)
    without_pricing.pop("pricing_binding_sha256")
    with pytest.raises(Exception):
        receipt_schema.validate(without_pricing)
    with pytest.raises(LiveRubricError, match="MODEL_OR_TRANSPORT_BINDING_MISMATCH"):
        replace(live_receipt, returned_model="different-model")
    with pytest.raises(Exception):
        receipt_schema.validate({**live_projection, "returned_model": "different-model"})
