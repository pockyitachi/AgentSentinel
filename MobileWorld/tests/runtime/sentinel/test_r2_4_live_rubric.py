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
    RubricTransportAuthority,
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
from mobile_world.runtime.sentinel.r2_4.evidence import CollectorEvidenceFactoryV1
from mobile_world.runtime.sentinel.r2_4.orchestration import R24RuntimeCoordinatorV1
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    CpuFakeRubricProviderPortV1,
    LiveOpenAIRubricBackendV1,
    LiveRubricError,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    ProductionRubricProviderPortV1,
    bind_current_collector_image,
    live_rubric_generate_schema,
    live_rubric_track_schema,
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
                    "char_end": len(task_text),
                    "utf8_byte_start": 0,
                    "utf8_byte_end": len(task_text.encode("utf-8")),
                    "exact_text": task_text,
                    "span_sha256": _sha(task_text),
                }
            ],
            "milestones": [
                {
                    "milestone_id": "task-goal-state",
                    "kind": "HARD_REQUIREMENT",
                    "predicate_kind": "INSTRUCTION_REQUIREMENT",
                    "state_description": task_text,
                    "description_sha256": _sha(task_text),
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
                            "payload_sha256": evidence.payload_sha256,
                            "relation": "SUPPORTS_STATE",
                        }
                    ],
                    "reason_code": "CURRENT_GUI_SUPPORT",
                }
            ],
        }
    )


def test_live_rubric_schemas_are_strict_and_checked_in() -> None:
    for snapshot in (live_rubric_generate_schema(), live_rubric_track_schema()):
        Draft202012Validator.check_schema(snapshot.as_dict())
        assert snapshot.sha256 == hashlib.sha256(snapshot.canonical_bytes).hexdigest()


def test_cpu_fake_generate_once_and_track_current_collector_image(tmp_path: Path) -> None:
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
    assert session.start().status is RubricSessionStatus.ADMITTED
    assert generated.status is RubricSessionStatus.ADMITTED
    assert generated.rubric is not None and generated.state is not None
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


def test_production_port_rejects_arbitrary_or_history_policy_runner() -> None:
    with pytest.raises(LiveRubricError, match="UNTRUSTED_PRODUCTION_RUNNER"):
        ProductionRubricProviderPortV1(runner=cast(Any, object()))


def test_r23_backend_provenance_accepts_only_exact_fake_or_live_pairs() -> None:
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
    live = RubricBackendDescriptorV1(
        **base,
        backend_kind=RubricBackendKind.OPENAI_RESPONSES,
        transport_authority=RubricTransportAuthority.EXPLICIT_OWNER_AUTHORIZATION,
        external_network_attempted=True,
        model_call_attempted=True,
        local_gpu_used=False,
    )
    assert fake.backend_kind is RubricBackendKind.INJECTED_FAKE
    assert live.backend_kind is RubricBackendKind.OPENAI_RESPONSES
    with pytest.raises(R23ContractError, match="INVALID_BACKEND_PROVENANCE"):
        RubricBackendDescriptorV1(
            **base,
            backend_kind=RubricBackendKind.OPENAI_RESPONSES,
            transport_authority=RubricTransportAuthority.EXPLICIT_OWNER_AUTHORIZATION,
            external_network_attempted=False,
            model_call_attempted=True,
        )
    with pytest.raises(R23ContractError, match="INVALID_BACKEND_PROVENANCE"):
        RubricBackendDescriptorV1(
            **base,
            backend_kind=RubricBackendKind.INJECTED_FAKE,
            transport_authority=RubricTransportAuthority.CPU_OFFLINE_FAKE,
            external_network_attempted=True,
            model_call_attempted=True,
        )


def test_r23_fake_projection_stays_stable_and_live_schema_pair_is_exact(tmp_path: Path) -> None:
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

    live_receipt = replace(
        receipt,
        backend_kind="OPENAI_RESPONSES",
        transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
        external_network_attempted=True,
        model_call_attempted=True,
    )
    receipt_schema = json.loads(
        (
            REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3/rubric_receipt.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(receipt_schema)
    validator.validate(fake_projection)
    validator.validate(rubric_receipt_projection(live_receipt))
    with pytest.raises(Exception):
        validator.validate(
            {
                **rubric_receipt_projection(live_receipt),
                "model_call_attempted": False,
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
    live_descriptor = replace(
        generated.rubric.backend,
        backend_kind=RubricBackendKind.OPENAI_RESPONSES,
        transport_authority=RubricTransportAuthority.EXPLICIT_OWNER_AUTHORIZATION,
        external_network_attempted=True,
        model_call_attempted=True,
    )
    live_rubric = replace(generated.rubric, backend=live_descriptor)
    rubric_schema = json.loads(
        (REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3/rubric.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(rubric_schema).validate(multi_path_rubric_projection(live_rubric))
