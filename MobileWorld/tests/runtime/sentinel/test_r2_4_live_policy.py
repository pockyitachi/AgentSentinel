from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from openai import OpenAI

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
)
from mobile_world.runtime.sentinel import SentinelMode
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56SentinelPolicy,
    OpenAIResponsesTransport,
    ProposalSchemaSnapshotV1,
    ResponsesEnvelopeV1,
    ResponsesRequestV1,
    TransportDescriptorV1,
    build_owner_authorized_openai_responses_transport,
)
from mobile_world.runtime.sentinel.r2_2.metrics import R22PolicyMetrics
from mobile_world.runtime.sentinel.r2_2.sidecar import MemoryR22PolicyReceiptSink
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    R22OwnerAuthorizedLivePolicyAdapter,
    issue_owner_authorized_live_policy_authority,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    OwnerAuthorizationV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceSafetyV1,
    SmokeModeV1,
    SnapshotResourceV1,
    authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
)

MOBILEWORLD_ROOT = Path(__file__).resolve().parents[3]
QWEN_FIXTURE = (
    MOBILEWORLD_ROOT
    / "tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pilot() -> FrozenPilotManifestV1:
    tasks = tuple(
        PilotTaskV1(
            task_id=f"task-{index:02d}",
            task_parameters_sha256=_sha(f"parameters-{index}"),
            reset_seed=1_000 + index,
        )
        for index in range(20)
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="live-policy-unit-cohort",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path="/tmp/r24-live-policy-never-read/tasks.json",
        task_manifest_sha256=_sha("task-manifest"),
        task_manifest_byte_count=1,
        topology_comparison_artifact_path="/tmp/r24-live-policy-never-read/topology.json",
        topology_comparison_artifact_sha256="0" * 64,
        topology_comparison_artifact_byte_count=1,
        cohort_selection_artifact_path=("/tmp/r24-live-policy-never-read/cohort-selection.json"),
        cohort_selection_artifact_sha256="1" * 64,
        cohort_selection_artifact_byte_count=1,
        cohort_selection_sha256="1" * 64,
        task_time_authority=PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY,
        dynamic_wall_clock_tasks_excluded=True,
        tasks=tasks,
        hosts=(PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI),
        arms=(PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL),
        topology=PilotTopologyV1.ISOLATED_HISTORY_FREE,
        seed_policy=PilotSeedPolicyV1.FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS,
        baseline_mode="OFF",
        joint_mode="ACTIVE",
        environment_reset_between_cells=True,
        matched_task_ids=True,
        matched_task_parameters=True,
        official_success_metric_required=True,
        max_steps_per_cell=1,
        per_cell_timeout_seconds=1,
        max_total_wall_time_seconds=80,
        max_total_actor_calls=80,
        max_total_openai_calls=80,
        max_total_cost_usd_micros=100,
    )


def _smoke_plan(host: PilotHostV1) -> HostLiveSmokePlanV1:
    return HostLiveSmokePlanV1(
        host=host,
        cases=tuple(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=(
                    f"/tmp/r24-live-policy-never-read/{host.value.lower()}-{mode.value}.json"
                ),
                request_fixture_sha256=_sha(f"{host.value}-{mode.value}"),
                request_fixture_byte_count=1,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=1,
                max_cost_usd_micros=1,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
            for mode in SmokeModeV1
        ),
    )


def _resource(host: PilotHostV1, port: int) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    return SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=f"/tmp/r24-live-policy-never-read/{host.value}/snapshot",
        snapshot_storage_root=f"/tmp/r24-live-policy-never-read/{host.value}",
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=_sha(f"{host.value}-snapshot"),
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"fixture-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )


def _manifest(
    now: datetime,
    *,
    status: RunAuthorizationStatusV1 = RunAuthorizationStatusV1.OWNER_AUTHORIZED,
    issued_delta: timedelta = -timedelta(hours=1),
    expires_delta: timedelta = timedelta(hours=1),
) -> R24R25RunAuthorityManifestV1:
    smokes = (
        _smoke_plan(PilotHostV1.QWEN3_VL),
        _smoke_plan(PilotHostV1.MAI_UI),
    )
    pilot = _pilot()
    return R24R25RunAuthorityManifestV1(
        schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
        run_id="r24-live-policy-unit-run",
        source_commit="a" * 40,
        authorization=OwnerAuthorizationV1(
            status=status,
            authorization_id="owner-authorized-live-policy-unit",
            authorized_by="owner",
            issued_at_utc=_utc(now + issued_delta),
            expires_at_utc=_utc(now + expires_delta),
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            pilot_gui_actions_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=SequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
                RunStageV1.R25_PILOT,
            ),
            stop_on_failure=True,
            pilot_only_after_both_smokes_pass=True,
            default_dry_run=True,
            arbitrary_commands_forbidden=True,
            secrets_in_logs_forbidden=True,
            repo_external_output_required=True,
        ),
        secret=SecretFileReferenceV1(
            path="/tmp/r24-live-policy-never-read/openai.key",
            environment_key="OPENAI_API_KEY",
            required_mode=0o600,
            content_may_be_read_by_preflight=False,
            persist_value_or_hash=False,
        ),
        openai_stages=(
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.RUBRIC,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=8192,
                timeout_ms=1_000,
                max_attempts=1,
                store=False,
            ),
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.HISTORY_POLICY,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=4096,
                timeout_ms=1_000,
                max_attempts=1,
                store=False,
            ),
        ),
        actor_resources=(
            _resource(PilotHostV1.QWEN3_VL, 18081),
            _resource(PilotHostV1.MAI_UI, 18082),
        ),
        smoke_plans=smokes,
        pilot=pilot,
        topology_comparison_artifact_sha256=pilot.topology_comparison_artifact_sha256,
        output_root="/tmp/r24-live-policy-never-read/output",
        max_resource_preflight_wall_time_seconds=1,
        max_sequence_wall_time_seconds=87,
        max_sequence_openai_calls=92,
        max_sequence_actor_calls=86,
        max_sequence_cost_usd_micros=106,
    )


def _policy(
    *,
    live: bool,
    base_url: str = "https://api.openai.com/v1",
) -> tuple[GPT56SentinelPolicy[Any, Any], OpenAI | None, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    if live:

        def no_network(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            raise AssertionError("focused authority tests must not dispatch HTTP")

        http_client = httpx.Client(transport=httpx.MockTransport(no_network))
        client = OpenAI(
            api_key="unit-test-placeholder",
            max_retries=0,
            timeout=httpx.Timeout(0.25),
            http_client=http_client,
            base_url=base_url,
        )
        transport: object = OpenAIResponsesTransport(
            client,
            seam_policy_deadline_seconds=1.0,
            live_call_authorized=True,
        )
    else:
        client = None
        transport = _NoCallFakeTransport()
    policy = GPT56SentinelPolicy(
        transport=cast(Any, transport),
        evidence_packet_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("evidence factory must not run")
        ),
        proposal_admission=lambda *_args: (_ for _ in ()).throw(
            AssertionError("admission must not run")
        ),
        admission_receipt_projector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("receipt projection must not run")
        ),
        bind_policy_receipt=lambda *_args: (_ for _ in ()).throw(
            AssertionError("receipt binding must not run")
        ),
        receipt_sink=MemoryR22PolicyReceiptSink(),
        metrics=R22PolicyMetrics(),
        output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
        timeout_seconds=0.05,
        seam_policy_deadline_seconds=1.0,
    )
    return policy, client, requests


class _NoCallFakeTransport:
    @property
    def descriptor(self) -> TransportDescriptorV1:
        return TransportDescriptorV1.cpu_fake()

    def create(
        self,
        request: ResponsesRequestV1,
        *,
        call_role: object,
        timeout_seconds: float,
    ) -> ResponsesEnvelopeV1:
        del request, call_role, timeout_seconds
        raise AssertionError("fake descriptor must be rejected before transport")


class _NoCallControl:
    def run_transport[T](self, call: Callable[[], T]) -> T:
        del call
        raise AssertionError("source transport must not be authorized")

    def publish_receipt(self, publish: Callable[[], None]) -> None:
        del publish
        raise AssertionError("source receipt must not be authorized")


class _ArbitraryLiveDescriptorTransport:
    @property
    def descriptor(self) -> TransportDescriptorV1:
        return TransportDescriptorV1(
            transport_kind="OPENAI_RESPONSES",
            transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
            openai_sdk_version="1.106.1",
            sdk_max_retries=0,
            external_network_on_call=True,
            model_on_call=True,
        )

    def create(
        self,
        request: ResponsesRequestV1,
        *,
        call_role: object,
        timeout_seconds: float,
    ) -> ResponsesEnvelopeV1:
        del request, call_role, timeout_seconds
        raise AssertionError("arbitrary transport must be rejected before dispatch")


def _no_history_qwen_request() -> tuple[dict[str, JsonValue], str, str]:
    fixture = cast(dict[str, Any], json.loads(QWEN_FIXTURE.read_text(encoding="utf-8")))
    request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
    messages = cast(list[JsonValue], request["messages"])
    user = cast(dict[str, JsonValue], messages[1])
    content = cast(list[JsonValue], user["content"])
    text_block = cast(dict[str, JsonValue], content[0])
    text = cast(str, text_block["text"])
    text_block["text"] = f"{text[: text.index('Step 1: ')]}\n"
    codec_id = cast(str, fixture["codec_id"])
    return request, codec_id, "mobileworld.qwen3vl.actor"


def test_caller_injected_openai_mock_is_not_a_production_live_transport() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=now,
    )
    policy, client, requests = _policy(live=True)
    try:
        with pytest.raises(R24ContractError, match="LIVE_PRODUCTION_TRANSPORT_REQUIRED"):
            R22OwnerAuthorizedLivePolicyAdapter(policy, authority=authority)
        assert policy.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


@pytest.mark.parametrize("failure", ["draft", "expired", "hash-mismatch"])
def test_draft_expired_and_hash_mismatch_fail_before_policy_call(failure: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    if failure == "draft":
        manifest = _manifest(now, status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED)
        expected = "OWNER_AUTHORITY_REQUIRED"
        confirmation = authority_manifest_sha256(manifest)
    elif failure == "expired":
        manifest = _manifest(
            now,
            issued_delta=-timedelta(hours=2),
            expires_delta=-timedelta(hours=1),
        )
        expected = "OWNER_AUTHORITY_EXPIRED"
        confirmation = authority_manifest_sha256(manifest)
    else:
        manifest = _manifest(now)
        expected = "MANIFEST_CONFIRMATION_MISMATCH"
        confirmation = "f" * 64
    policy, client, requests = _policy(live=True)
    try:
        with pytest.raises(R24ContractError, match=expected):
            issue_owner_authorized_live_policy_authority(
                manifest,
                confirmed_manifest_sha256=confirmation,
                now=now,
            )
        assert policy.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


def test_fake_descriptor_is_rejected_before_source_evaluation() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=now,
    )
    policy, _client, requests = _policy(live=False)
    with pytest.raises(R24ContractError, match="LIVE_SOURCE_ATTESTATION_REQUIRED"):
        R22OwnerAuthorizedLivePolicyAdapter(policy, authority=authority)
    assert policy.evaluate_count == 0
    assert requests == []


def test_manifest_hash_and_key_callback_cannot_build_a_production_transport() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    key_reads: list[str] = []

    def key_provider() -> str:
        key_reads.append("read")
        return "unit-test-placeholder"

    with pytest.raises(TypeError, match="unexpected keyword"):
        cast(Any, build_owner_authorized_openai_responses_transport)(
            api_key_provider=key_provider,
            authority_manifest_sha256=authority_manifest_sha256(manifest),
            seam_policy_deadline_seconds=1.0,
            client_timeout_seconds=0.25,
        )
    assert key_reads == []


@pytest.mark.parametrize("mode", [SentinelMode.SHADOW, SentinelMode.ACTIVE])
def test_no_history_cannot_upgrade_a_caller_client_to_live_authority(
    mode: SentinelMode,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    manifest_sha256 = authority_manifest_sha256(manifest)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        now=now,
    )
    source, client, requests = _policy(live=True)
    try:
        with pytest.raises(R24ContractError, match="LIVE_PRODUCTION_TRANSPORT_REQUIRED"):
            R22OwnerAuthorizedLivePolicyAdapter(source, authority=authority)
        assert source.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


def test_live_adapter_rejects_cpu_fake_runtime_audit_at_sentinel_construction() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=now,
    )
    source, client, requests = _policy(live=True)
    try:
        with pytest.raises(R24ContractError, match="LIVE_PRODUCTION_TRANSPORT_REQUIRED"):
            R22OwnerAuthorizedLivePolicyAdapter(source, authority=authority)
        assert source.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


def test_non_manifest_endpoint_is_rejected_before_transport() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=now,
    )
    policy, client, requests = _policy(live=True, base_url="https://example.com/v1")
    try:
        with pytest.raises(R24ContractError, match="LIVE_TRANSPORT_MANIFEST_MISMATCH"):
            R22OwnerAuthorizedLivePolicyAdapter(policy, authority=authority)
        assert policy.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


def test_production_client_base_url_drift_is_rejected_before_transport() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    manifest_sha256 = authority_manifest_sha256(manifest)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        now=now,
    )
    policy, client, requests = _policy(live=True)
    try:
        assert client is not None
        client.base_url = "https://example.com/v1/"
        with pytest.raises(R24ContractError, match="LIVE_TRANSPORT_ATTESTATION_REQUIRED"):
            R22OwnerAuthorizedLivePolicyAdapter(policy, authority=authority)
        assert policy.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()


def test_policy_transport_replacement_is_rejected_before_transport() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(now)
    manifest_sha256 = authority_manifest_sha256(manifest)
    authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=manifest_sha256,
        now=now,
    )
    policy, client, requests = _policy(live=True)
    try:
        object.__setattr__(policy, "_transport", _ArbitraryLiveDescriptorTransport())
        with pytest.raises(R24ContractError, match="LIVE_TRANSPORT_ATTESTATION_REQUIRED"):
            R22OwnerAuthorizedLivePolicyAdapter(policy, authority=authority)
        assert policy.evaluate_count == 0
        assert requests == []
    finally:
        if client is not None:
            client.close()
