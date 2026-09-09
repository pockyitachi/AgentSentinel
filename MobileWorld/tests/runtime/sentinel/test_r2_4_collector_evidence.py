from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonPath,
    JsonValue,
    SpanRole,
)
from mobile_world.offline.g1_history_codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import EvidenceRole, evidence_packet_sha256
from mobile_world.runtime.sentinel.r2_2.evidence import CausalEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import GPT56EvidenceInputV1
from mobile_world.runtime.sentinel.r2_3.contracts import RubricEvidenceRole
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_4 import execution_ledger as execution_ledger_module
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceBundleV1,
    CollectorEvidenceError,
    CollectorEvidenceFactoryV1,
    CollectorEvidenceLimitsV1,
    CollectorRubricOnlyBundleV1,
    build_collector_gpt56_evidence_factory,
    rubric_evidence_snapshot_projection,
    rubric_evidence_snapshot_sha256,
)
from mobile_world.runtime.sentinel.r2_4.execution_ledger import (
    ExecutionLedgerBlobCacheV1,
    ExecutionLedgerError,
    ExecutionSemanticOutcomeV1,
    ExecutorTerminalStatusV1,
    LedgerActionKindV1,
    RepeatFactKindV1,
    ScreenPixelRelationV1,
    build_mobile_execution_ledger,
    execution_ledger_projection,
    snapshot_execution_ledger,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import R24RuntimeCoordinatorV1

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = REPO_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs"


@dataclass(frozen=True, slots=True)
class _Case:
    name: str
    fixture: str
    codec_type: type[QwenFlatProgressHistoryCodec] | type[MaiRawReplayHistoryCodec]
    task_goal: str


CASES = (
    _Case(
        name="qwen",
        fixture="qwen_flat_progress.captured.v1.json",
        codec_type=QwenFlatProgressHistoryCodec,
        task_goal="调整显示亮度。",
    ),
    _Case(
        name="mai",
        fixture="mai_raw_replay.captured.v1.json",
        codec_type=MaiRawReplayHistoryCodec,
        task_goal="在设置中检查显示选项。",
    ),
)


@dataclass(slots=True)
class _RuntimeCase:
    run: RunRecorder
    task: TaskRecorder
    capture: RunnerTaskCapture
    request: dict[str, JsonValue]
    history_ir: HistoryIR
    sentinel_context: SentinelContext
    audit_context: AuditContext


def _load_case(case: _Case) -> tuple[dict[str, JsonValue], HistoryIR]:
    raw = cast(
        dict[str, Any],
        json.loads((FIXTURE_ROOT / case.fixture).read_text(encoding="utf-8")),
    )
    request = cast(dict[str, JsonValue], deepcopy(raw["application_request"]))
    bindings = tuple(
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
        for item in raw["curated_span_bindings"]
    )
    return request, case.codec_type(bindings).extract(cast(JsonValue, request))


def _request_image(request: dict[str, JsonValue]) -> tuple[str, Image.Image]:
    found: list[str] = []

    def visit(value: JsonValue, path: JsonPath = ()) -> None:
        del path
        if type(value) is dict:
            image_url = value.get("image_url")
            if value.get("type") == "image_url" and type(image_url) is dict:
                url = image_url.get("url")
                if type(url) is str:
                    found.append(url)
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(cast(JsonValue, request))
    assert found
    data_url = found[-1]
    image_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(image_bytes)) as opened:
        opened.load()
        image = opened.copy()
    return data_url, image


def _no_history_request(runtime: _RuntimeCase) -> dict[str, JsonValue]:
    request = cast(dict[str, JsonValue], deepcopy(runtime.request))
    messages = cast(list[JsonValue], request["messages"])
    if runtime.history_ir.host_id == "mobileworld.qwen3vl.actor":
        content = cast(list[JsonValue], cast(dict[str, JsonValue], messages[1])["content"])
        text_block = cast(dict[str, JsonValue], content[0])
        text = cast(str, text_block["text"])
        text_block["text"] = text[: text.index("Step 1: ")] + "\n"
    else:
        request["messages"] = [messages[0], messages[1], messages[-1]]
    return request


def _transport_result(run: RunRecorder) -> dict[str, Any]:
    request_blob = run.blob_store.put_bytes(b'{"action":"click"}', "application/json")
    response_blob = run.blob_store.put_bytes(b'{"status":"ok"}', "application/json")
    return {
        "kind": "gui_transport",
        "request_endpoint": "http://fixture.invalid/step",
        "request_body_snapshot_blob": request_blob,
        "http_status": 200,
        "response_body_blob": response_blob,
        "response_headers": {"content-type": "application/json"},
        "raw_tool_result_blob": None,
        "agent_visible_tool_result": {"tool": "visible", "ok": True},
        "ask_user_response": "confirmed",
        "exception": None,
    }


def _runtime_case(
    tmp_path: Path,
    case: _Case,
    *,
    mismatch_current_image: bool = False,
    prior_action_count: int = 1,
    prior_action: dict[str, JsonValue] | None = None,
) -> _RuntimeCase:
    request, history_ir = _load_case(case)
    _data_url, current_image = _request_image(request)
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="r2.4-test", worker_id="collector-evidence"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    task_started = capture.start_task(
        task_name=f"R24{case.name}",
        task_goal=case.task_goal,
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": case.name, "model": "fixture", "configuration": {}},
        environment={"backend_id": "cpu-fixture", "device_id": "none"},
        whole_task_attempt_index=1,
    )
    assert task_started is not None
    for prior_index in range(1, prior_action_count + 1):
        prior_step = capture.start_step(
            step_index=prior_index,
            observation={
                "screenshot": Image.new("RGB", current_image.size, "red"),
                "accessibility_tree": {"screen": "prior"},
                "tool_call": None,
                "ask_user_response": None,
            },
        )
        assert prior_step is not None
        action = prior_action or {"action_type": "click", "x": 1, "y": 1}
        decision = capture.record_decision(
            prediction="fixture prior response",
            action=action,
            step=prior_step,
        )
        assert decision is not None
        execution = capture.execution_started(decision=decision)
        assert execution is not None
        transition = capture.transition_completed(
            execution=execution,
            post_observation={
                "screenshot": Image.new("RGB", current_image.size, "green"),
                "accessibility_tree": {"screen": "post", "enabled": True},
                "tool_call": {"tool": "visible", "ok": True},
                "ask_user_response": "confirmed",
            },
            execution_result=_transport_result(run),
            duration_ns=1234,
        )
        assert transition is not None
    bound_image = (
        Image.new("RGB", current_image.size, "black") if mismatch_current_image else current_image
    )
    current_step = capture.start_step(
        step_index=prior_action_count + 1,
        observation={
            "screenshot": bound_image,
            "accessibility_tree": {"screen": "current", "buttons": ["OK"]},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert current_step is not None
    audit_context = AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=current_step.step_id,
        decision_id=current_step.decision_id,
        parent_event_id=current_step.step_started_event_id,
    )
    return _RuntimeCase(
        run=run,
        task=task,
        capture=capture,
        request=request,
        history_ir=history_ir,
        sentinel_context=SentinelContext(
            logical_call_id=f"r24-{case.name}-call",
            host_id=history_ir.host_id,
        ),
        audit_context=audit_context,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda value: value.name)
def test_collector_factory_builds_one_read_r22_and_history_free_r23_bundle(
    tmp_path: Path, case: _Case
) -> None:
    runtime = _runtime_case(tmp_path, case)
    before = runtime.task.path.read_bytes()
    factory = build_collector_gpt56_evidence_factory()
    try:
        with bind_audit_context(runtime.audit_context):
            bundle = factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
    finally:
        runtime.run.close()

    assert type(bundle) is CollectorEvidenceBundleV1
    assert type(bundle.r22_snapshot) is CausalEvidenceSnapshotV1
    assert type(bundle.gpt56_input) is GPT56EvidenceInputV1
    assert type(bundle.r23_snapshot) is RubricEvidenceSnapshotV1
    assert bundle.execution_ledger.cutoff_event_id == runtime.audit_context.parent_event_id
    assert bundle.execution_ledger.cutoff_event_seq == len(
        runtime.task.path.read_text(encoding="utf-8").splitlines()
    )
    assert len(bundle.execution_ledger.attempts) == 1
    prior_attempt = bundle.execution_ledger.attempts[0]
    assert prior_attempt.terminal_status is ExecutorTerminalStatusV1.EXECUTOR_RETURNED
    assert prior_attempt.semantic_outcome is ExecutionSemanticOutcomeV1.UNKNOWN
    assert bundle.gpt56_input.packet == bundle.r22_packet
    assert bundle.r22_packet.logical_call_id == runtime.sentinel_context.logical_call_id
    assert bundle.r22_packet.raw_request_sha256 == runtime.history_ir.raw_request_sha256
    assert bundle.r22_snapshot.cutoff.cutoff_event_seq == (
        bundle.r23_snapshot.cutoff.cutoff_event_seq
    )
    assert bundle.r22_snapshot.task.exact_text == case.task_goal
    assert bundle.r23_snapshot.task.exact_text == case.task_goal
    assert runtime.task.path.read_bytes() == before

    r22_roles = {entry.role for entry in bundle.r22_snapshot.evidence_index}
    assert {
        EvidenceRole.CURRENT_UI_SCREENSHOT,
        EvidenceRole.CURRENT_ACCESSIBILITY,
        EvidenceRole.PRIOR_ACTION_ATTEMPT,
        EvidenceRole.PRIOR_TRANSITION_STATUS,
        EvidenceRole.PRIOR_POST_UI_STATE,
        EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
        EvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
        EvidenceRole.USER_RESPONSE,
    } <= r22_roles
    r23_roles = {entry.role for entry in bundle.r23_snapshot.evidence_index}
    assert r23_roles == {
        RubricEvidenceRole.CURRENT_UI_SCREENSHOT,
        RubricEvidenceRole.CURRENT_ACCESSIBILITY,
        RubricEvidenceRole.COMPLETED_TRANSITION_STATUS,
        RubricEvidenceRole.COMPLETED_POST_UI_STATE,
        RubricEvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
        RubricEvidenceRole.USER_RESPONSE,
    }
    projection_text = json.dumps(
        rubric_evidence_snapshot_projection(bundle.r23_snapshot), sort_keys=True
    )
    for forbidden in (
        runtime.sentinel_context.logical_call_id,
        "raw_request_sha256",
        "history_ir",
        "actor_history",
        '"model"',
        "request_value_sha256",
    ):
        assert forbidden not in projection_text
    assert bundle.r23_snapshot_sha256 == rubric_evidence_snapshot_sha256(bundle.r23_snapshot)


@pytest.mark.parametrize("case", CASES, ids=lambda value: f"{value.name}-no-history")
def test_no_history_factory_builds_only_rubric_projection_from_same_cutoff(
    tmp_path: Path, case: _Case
) -> None:
    runtime = _runtime_case(tmp_path, case)
    request = _no_history_request(runtime)
    before = runtime.task.path.read_bytes()
    try:
        with bind_audit_context(runtime.audit_context):
            bundle = CollectorEvidenceFactoryV1().rubric_only_bundle_for_no_history_call(
                request=cast(JsonValue, request),
                context=runtime.sentinel_context,
            )
    finally:
        runtime.run.close()

    assert type(bundle) is CollectorRubricOnlyBundleV1
    assert type(bundle.r23_snapshot) is RubricEvidenceSnapshotV1
    assert bundle.r23_snapshot.task.exact_text == case.task_goal
    assert bundle.current_image_sha256 == (
        bundle.r23_snapshot.current_observation.screenshot_content_sha256
    )
    assert bundle.current_image_data_url.startswith("data:image/")
    assert bundle.r23_snapshot_sha256 == rubric_evidence_snapshot_sha256(bundle.r23_snapshot)
    assert runtime.task.path.read_bytes() == before
    assert not hasattr(bundle, "gpt56_input")
    assert not hasattr(bundle, "r22_packet")


def test_cutoff_excludes_future_events_and_keeps_stimulus_hash_stable(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path, CASES[0])
    factory = CollectorEvidenceFactoryV1()
    try:
        with bind_audit_context(runtime.audit_context):
            before = factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
        future = runtime.capture.start_step(
            step_index=3,
            observation={
                "screenshot": Image.new("RGB", (1, 1), "yellow"),
                "accessibility_tree": {"future": True},
                "tool_call": {"future": True},
                "ask_user_response": "future",
            },
        )
        assert future is not None
        ended = runtime.capture.end_task(
            runtime_status="completed",
            termination_source="fixture_future_suffix",
            final_step_index=3,
            score=1.0,
            reason="future evaluator result must not enter an earlier cutoff",
        )
        assert ended is not None
        with bind_audit_context(runtime.audit_context):
            after = factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
    finally:
        runtime.run.close()

    assert after.r22_packet == before.r22_packet
    assert after.r23_snapshot == before.r23_snapshot
    assert after.r23_snapshot_sha256 == before.r23_snapshot_sha256
    assert after.execution_ledger == before.execution_ledger
    assert after.execution_ledger.sha256 == before.execution_ledger.sha256
    cutoff = before.r22_snapshot.cutoff.cutoff_event_seq
    assert all(item.source_event_seq <= cutoff for item in after.r22_snapshot.evidence_index)


def test_execution_ledger_reports_only_exact_repeat_and_transport_facts(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path, CASES[0], prior_action_count=2)
    try:
        with bind_audit_context(runtime.audit_context):
            ledger = (
                CollectorEvidenceFactoryV1()
                .bundle_for_call(
                    request=cast(JsonValue, runtime.request),
                    context=runtime.sentinel_context,
                    history_ir=runtime.history_ir,
                )
                .execution_ledger
            )
    finally:
        runtime.run.close()

    assert len(ledger.attempts) == 2
    assert all(
        item.terminal_status is ExecutorTerminalStatusV1.EXECUTOR_RETURNED
        and item.semantic_outcome is ExecutionSemanticOutcomeV1.UNKNOWN
        and item.screen_pixel_relation is ScreenPixelRelationV1.SCREEN_PIXELS_DIFFERENT
        for item in ledger.attempts
    )
    assert len(ledger.repeat_facts) == 1
    repeat = ledger.repeat_facts[0]
    assert repeat.fact_kind is RepeatFactKindV1.EXACT_ACTION_AND_SCREEN_REPEAT
    assert repeat.lower_bound == 2
    projection = json.dumps(execution_ledger_projection(ledger), sort_keys=True)
    for forbidden in ("SUCCESS", "STALLED", "STAGNATION", "NO_PROGRESS", "NEXT_ACTION"):
        assert forbidden not in projection.upper()


def test_rubric_failure_does_not_discard_same_cutoff_history_or_execution_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime_case(tmp_path, CASES[0], prior_action_count=2)
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=lambda _task_run_id, _task: None,
    )

    class _FailingRubricSession:
        @staticmethod
        def start() -> None:
            raise RuntimeError("injected rubric-only failure")

    monkeypatch.setattr(
        coordinator,
        "_session_for",
        lambda _task_run_id, _task: _FailingRubricSession(),
    )
    try:
        with bind_audit_context(runtime.audit_context):
            history_input = coordinator(
                cast(JsonValue, runtime.request),
                runtime.sentinel_context,
                runtime.history_ir,
            )
    finally:
        runtime.run.close()

    logical_call_id = runtime.sentinel_context.logical_call_id
    ledger = coordinator.execution_ledger_for(logical_call_id)
    assert history_input.packet_sha256 == evidence_packet_sha256(history_input.packet)
    assert coordinator.history_evidence_input_for(logical_call_id) == history_input
    assert coordinator.rubric_failure_code_for(logical_call_id) == "RUBRIC_TASK_START_ERROR"
    assert coordinator.rubric_state_changed_for(logical_call_id) is None
    assert ledger is not None
    assert ledger.repeat_facts[0].lower_bound == 2


def test_execution_state_preparation_and_history_share_one_collector_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime_case(tmp_path, CASES[0], prior_action_count=2)
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=lambda _task_run_id, _task: None,
    )

    class _FailingRubricSession:
        @staticmethod
        def start() -> None:
            raise RuntimeError("injected rubric-only failure")

    monkeypatch.setattr(
        coordinator,
        "_session_for",
        lambda _task_run_id, _task: _FailingRubricSession(),
    )
    try:
        with bind_audit_context(runtime.audit_context):
            prepared_ledger = coordinator.prepare_execution_state(
                cast(JsonValue, runtime.request),
                runtime.sentinel_context,
                runtime.history_ir,
            )
            prepared_history = coordinator.history_evidence_input_for(
                runtime.sentinel_context.logical_call_id
            )
            history_input = coordinator(
                cast(JsonValue, runtime.request),
                runtime.sentinel_context,
                runtime.history_ir,
            )
    finally:
        runtime.run.close()

    assert coordinator.collector_bundle_calls == 1
    assert prepared_history is not None
    assert history_input == prepared_history
    assert coordinator.execution_ledger_for(runtime.sentinel_context.logical_call_id) == (
        prepared_ledger
    )


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    (
        ("decision_parent", "DECISION_BINDING_MISMATCH"),
        ("decision_action", "ACTION_BINDING_MISMATCH"),
        ("terminal_parent", "TRANSITION_BINDING_MISMATCH"),
        ("terminal_step", "TRANSITION_BINDING_MISMATCH"),
    ),
)
def test_execution_ledger_rejects_broken_decision_execution_causality(
    tmp_path: Path,
    corruption: str,
    expected_code: str,
) -> None:
    runtime = _runtime_case(tmp_path, CASES[0], prior_action_count=2)
    events = cast(
        list[dict[str, JsonValue]],
        [json.loads(line) for line in runtime.task.path.read_text().splitlines()],
    )
    action_event = next(item for item in events if item["event_type"] == "action_execution_started")
    decision_event = next(item for item in events if item["event_type"] == "agent_decision")
    terminal_event = next(item for item in events if item["event_type"] == "transition_completed")
    step_event = next(item for item in events if item["event_type"] == "step_started")
    if corruption == "decision_parent":
        action_event["caused_by_event_id"] = cast(str, step_event["event_id"])
    elif corruption == "decision_action":
        cast(dict[str, JsonValue], decision_event["payload"])["parsed_action"] = {
            "value": {"action_type": "click", "x": 999, "y": 999}
        }
    elif corruption == "terminal_parent":
        terminal_event["caused_by_event_id"] = cast(str, decision_event["event_id"])
    else:
        cast(dict[str, JsonValue], terminal_event["payload"])["step_id"] = "other-step"
    try:
        with pytest.raises(ExecutionLedgerError) as rejected:
            build_mobile_execution_ledger(
                events=tuple(events),
                current_event=events[-1],
                run_id=runtime.run.run_id,
                task_run_id=runtime.task.task_run_id,
                blob_store=runtime.run.blob_store,
            )
    finally:
        runtime.run.close()
    assert rejected.value.code == expected_code


def test_execution_ledger_redacts_nested_action_values_and_detaches_snapshots(
    tmp_path: Path,
) -> None:
    canary = "never-persist-this-action-value"
    runtime = _runtime_case(
        tmp_path,
        CASES[0],
        prior_action_count=2,
        prior_action={
            "action_type": "mcp",
            "action_json": {"query": canary, "items": [canary]},
        },
    )
    try:
        with bind_audit_context(runtime.audit_context):
            ledger = (
                CollectorEvidenceFactoryV1()
                .bundle_for_call(
                    request=cast(JsonValue, runtime.request),
                    context=runtime.sentinel_context,
                    history_ir=runtime.history_ir,
                )
                .execution_ledger
            )
    finally:
        runtime.run.close()

    projection = json.dumps(execution_ledger_projection(ledger), sort_keys=True)
    assert canary not in projection
    assert "action_json" not in projection
    assert all(
        item.action_projection.action_kind is LedgerActionKindV1.MCP
        and item.action_projection.sensitive_value_present
        for item in ledger.attempts
    )
    detached = snapshot_execution_ledger(ledger)
    object.__setattr__(detached.attempts[0].pre_observation, "mode", "mutated")
    object.__setattr__(detached.repeat_facts[0], "lower_bound", 63)
    assert ledger.attempts[0].pre_observation.mode == "RGB"
    assert ledger.repeat_facts[0].lower_bound == 2
    with pytest.raises(ExecutionLedgerError):
        snapshot_execution_ledger(detached)


def test_execution_ledger_reuses_verified_immutable_pixel_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime_case(tmp_path, CASES[0], prior_action_count=2)
    factory = CollectorEvidenceFactoryV1()
    read_count = 0
    original_read_bytes = BlobStore.read_bytes

    def counted_read_bytes(self: BlobStore, reference: Any) -> bytes:
        nonlocal read_count
        read_count += 1
        return original_read_bytes(self, reference)

    monkeypatch.setattr(BlobStore, "read_bytes", counted_read_bytes)
    try:
        with bind_audit_context(runtime.audit_context):
            factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
        first_read_count = read_count
        with bind_audit_context(runtime.audit_context):
            factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
        second_read_count = read_count - first_read_count
    finally:
        runtime.run.close()

    assert first_read_count >= 3
    # The ordinary current-image binding is deliberately re-read. Historical
    # canonical pixel blobs are verified once and then guarded by file identity.
    assert second_read_count == 1


def test_execution_ledger_rejects_png_header_drift_before_pixel_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BlobStore(tmp_path / "blob-root")
    reference = store.put_bytes(b"bounded-fixture", "image/png")

    class _Opened:
        format = "PNG"
        size = (50_000, 50_000)
        mode = "RGB"
        loaded = False

        def __enter__(self) -> _Opened:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def load(self) -> None:
            self.loaded = True
            raise AssertionError("unbounded pixels must not be decoded")

    opened = _Opened()
    monkeypatch.setattr(execution_ledger_module.Image, "open", lambda _stream: opened)
    with pytest.raises(ExecutionLedgerError) as rejected:
        ExecutionLedgerBlobCacheV1().verified_pixel_identity(
            blob_store=store,
            reference=reference,
            width=1,
            height=1,
            mode="RGB",
            max_image_bytes=1024,
            max_image_pixels=1024,
        )
    assert rejected.value.code == "PIXEL_METADATA_DRIFT"
    assert not opened.loaded


def test_missing_context_and_resource_bound_fail_with_typed_codes(tmp_path: Path) -> None:
    request, history_ir = _load_case(CASES[0])
    factory = CollectorEvidenceFactoryV1()
    with pytest.raises(CollectorEvidenceError, match="NO_AUDIT_CONTEXT") as missing:
        factory(
            cast(JsonValue, request),
            SentinelContext(logical_call_id="r24-no-context", host_id=history_ir.host_id),
            history_ir,
        )
    assert missing.value.code == "NO_AUDIT_CONTEXT"

    runtime = _runtime_case(tmp_path, CASES[0])
    bounded = CollectorEvidenceFactoryV1(
        limits=CollectorEvidenceLimitsV1(
            max_stream_bytes=1,
            max_event_line_bytes=1,
        )
    )
    try:
        with bind_audit_context(runtime.audit_context):
            with pytest.raises(
                CollectorEvidenceError, match="TASK_STREAM_SIZE_REJECTED"
            ) as oversized:
                bounded(
                    cast(JsonValue, runtime.request),
                    runtime.sentinel_context,
                    runtime.history_ir,
                )
    finally:
        runtime.run.close()
    assert oversized.value.code == "TASK_STREAM_SIZE_REJECTED"


def test_request_and_collector_current_image_drift_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path, CASES[1], mismatch_current_image=True)
    try:
        with bind_audit_context(runtime.audit_context):
            with pytest.raises(CollectorEvidenceError, match="CURRENT_IMAGE_PIXEL_DRIFT") as drift:
                CollectorEvidenceFactoryV1()(
                    cast(JsonValue, runtime.request),
                    runtime.sentinel_context,
                    runtime.history_ir,
                )
    finally:
        runtime.run.close()
    assert drift.value.code == "CURRENT_IMAGE_PIXEL_DRIFT"


def test_public_results_are_rebuilt_after_hostile_result_mutation(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path, CASES[0])
    factory = CollectorEvidenceFactoryV1()
    try:
        with bind_audit_context(runtime.audit_context):
            first = factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
            object.__setattr__(first.r23_snapshot.task, "exact_text", "mutated")
            second = factory.bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=runtime.sentinel_context,
                history_ir=runtime.history_ir,
            )
    finally:
        runtime.run.close()
    assert second.r23_snapshot.task.exact_text == CASES[0].task_goal
    assert first.r23_snapshot is not second.r23_snapshot
    assert first.r22_packet is not second.r22_packet
