"""Lean PromptSentinel wiring for ordinary audited ``mw eval`` tasks.

This module deliberately has no pilot manifest, preflight, process factory,
case lease, pricing gate, GPU census, Docker lifecycle, or model snapshot.  It
connects the existing task-local Collector context to the existing rubric and
R2.2 history policy, then lets the common seam enforce exact-span rendering and
Original fallback.

Constructing a factory or task runtime performs no provider call.  Calls occur
only when the returned ``PromptSentinel`` is invoked by an actor request.
"""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import cast

from openai import DefaultHttpxClient, OpenAI, Timeout

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    canonical_json_bytes,
    copy_json,
)
from mobile_world.runtime.sentinel.contracts import (
    SentinelContext,
    SentinelHostConfig,
    SentinelMode,
)
from mobile_world.runtime.sentinel.r2_2.contracts import (
    EvidencePacketV1,
    RuntimeAdmissionBundleV1,
    evidence_packet_projection,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56SentinelPolicy,
    OpenAIResponsesTransport,
    PolicyCallProvenanceV1,
    ProposalSchemaSnapshotV1,
)
from mobile_world.runtime.sentinel.r2_2.metrics import R22PolicyMetrics
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import (
    admission_receipt_projector,
    bind_policy_receipt,
    proposal_admission,
)
from mobile_world.runtime.sentinel.r2_2.sidecar import MemoryR22PolicyReceiptSink
from mobile_world.runtime.sentinel.r2_3.contracts import TaskInstructionV1
from mobile_world.runtime.sentinel.r2_3.session import RubricTaskSession
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.evidence import CollectorEvidenceFactoryV1
from mobile_world.runtime.sentinel.r2_4.lean_policy import LeanActiveRuntimePolicyV1
from mobile_world.runtime.sentinel.r2_4.lean_rubric import (
    DirectOpenAIRubricProviderV1,
    LeanOpenAIRubricBackendV1,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import R24RuntimeCoordinatorV1
from mobile_world.runtime.sentinel.seam import PromptSentinel, SentinelGlobalSwitch
from mobile_world.runtime.sentinel.sidecar import ExternalSentinelReceiptSink

_QWEN_HOST_ID = "mobileworld.qwen3vl.actor"
_MAI_HOST_ID = "mobileworld.mai-ui.actor"


class _AdmissionBridgeV1:
    """Retain exact admission inputs, isolated by logical actor call."""

    def __init__(self, coordinator: R24RuntimeCoordinatorV1) -> None:
        self._coordinator = coordinator
        self._pending: dict[str, tuple[EvidencePacketV1, JsonValue, HistoryIR]] = {}
        self._lock = Lock()

    def evidence(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ):
        evidence = self._coordinator(request, context, history_ir)
        packet = deepcopy(evidence.packet)
        if type(packet) is not EvidencePacketV1:
            raise RuntimeError("Collector evidence returned an untrusted packet type")
        if packet.logical_call_id != context.logical_call_id:
            raise RuntimeError("Collector evidence differs from its logical call")
        with self._lock:
            if context.logical_call_id in self._pending:
                raise RuntimeError("logical call already has pending admission inputs")
            self._pending[context.logical_call_id] = (
                packet,
                copy_json(request),
                deepcopy(history_ir),
            )
        return evidence

    def admit(
        self,
        packet_projection: dict[str, JsonValue],
        proposal_projection: dict[str, JsonValue],
        provenance: PolicyCallProvenanceV1,
    ) -> RuntimeAdmissionBundleV1:
        if type(packet_projection) is not dict:
            raise RuntimeError("policy admission packet projection must be an exact object")
        logical_call_id = packet_projection.get("logical_call_id")
        if type(logical_call_id) is not str:
            raise RuntimeError("policy admission omitted its logical call")
        with self._lock:
            pending = self._pending.pop(logical_call_id, None)
        if pending is None:
            raise RuntimeError("policy admission has no pending inputs for its logical call")
        packet, request, history_ir = pending
        if canonical_json_bytes(cast(JsonValue, packet_projection)) != canonical_json_bytes(
            cast(JsonValue, evidence_packet_projection(packet))
        ):
            raise RuntimeError("policy admission packet differs from Collector evidence")
        return proposal_admission(
            deepcopy(packet),
            proposal_projection,
            provenance,
            source_request=copy_json(request),
            history_ir=deepcopy(history_ir),
        )


class LeanSentinelTaskRuntimeV1:
    """Task-local owner returned to ``runner.py`` for deterministic cleanup."""

    def __init__(
        self,
        *,
        sentinel: PromptSentinel,
        history_transport: OpenAIResponsesTransport,
    ) -> None:
        if type(sentinel) is not PromptSentinel:
            raise TypeError("sentinel must use the exact common seam")
        if type(history_transport) is not OpenAIResponsesTransport:
            raise TypeError("history transport must use the exact Responses adapter")
        self.sentinel = sentinel
        self._history_transport = history_transport
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._history_transport.close()


class LeanSentinelRunFactoryV1:
    """Build one independent Sentinel/rubric state machine per eval task."""

    def __init__(
        self,
        *,
        mode: SentinelMode,
        api_key: str,
        receipt_root: Path,
        repository_root: Path | None = None,
        base_url: str = "https://api.openai.com/v1",
        policy_timeout_seconds: float = 240.0,
        transport_timeout_seconds: float = 220.0,
    ) -> None:
        if mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}:
            raise ValueError("lean Sentinel factory mode must be SHADOW or ACTIVE")
        if type(api_key) is not str or not api_key:
            raise ValueError("lean Sentinel needs a non-empty API key")
        if not isinstance(receipt_root, Path) or not receipt_root.is_absolute():
            raise ValueError("receipt_root must be an absolute Path")
        if repository_root is not None and not isinstance(repository_root, Path):
            raise TypeError("repository_root must be a Path when supplied")
        if type(base_url) is not str or not base_url:
            raise ValueError("base_url must be non-empty text")
        for value, label in (
            (policy_timeout_seconds, "policy_timeout_seconds"),
            (transport_timeout_seconds, "transport_timeout_seconds"),
        ):
            if type(value) not in {int, float} or isinstance(value, bool):
                raise TypeError(f"{label} must be an exact number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be positive and finite")
        if transport_timeout_seconds >= policy_timeout_seconds:
            raise ValueError("transport timeout must be below the policy timeout")
        self._mode = mode
        self._api_key = api_key
        self._receipt_root = receipt_root
        self._repository_root = repository_root
        self._base_url = base_url
        self._policy_timeout_seconds = float(policy_timeout_seconds)
        self._transport_timeout_seconds = float(transport_timeout_seconds)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(mode={self._mode.value!r}, "
            f"receipt_root={str(self._receipt_root)!r}, base_url={self._base_url!r})"
        )

    def __call__(self) -> LeanSentinelTaskRuntimeV1:
        timeout = Timeout(self._transport_timeout_seconds)
        client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=timeout,
            http_client=DefaultHttpxClient(timeout=timeout, trust_env=False),
        )
        history_transport = OpenAIResponsesTransport(
            client,
            seam_policy_deadline_seconds=self._policy_timeout_seconds,
            live_call_authorized=True,
        )
        try:
            rubric_provider = DirectOpenAIRubricProviderV1(
                client=client,
                timeout_seconds=self._transport_timeout_seconds,
            )
            rubric_backend = LeanOpenAIRubricBackendV1(provider=rubric_provider)

            def session_factory(
                task_run_id: str,
                task: TaskInstructionV1,
            ) -> RubricTaskSession:
                return RubricTaskSession(
                    task_run_id=task_run_id,
                    task=task,
                    builder_backend=rubric_backend,
                    tracker_backend=rubric_backend,
                )

            coordinator = R24RuntimeCoordinatorV1(
                collector=CollectorEvidenceFactoryV1(),
                session_factory=session_factory,
                rubric_call_observer=rubric_backend,
            )
            bridge = _AdmissionBridgeV1(coordinator)
            source_policy = GPT56SentinelPolicy(
                transport=history_transport,
                evidence_packet_factory=bridge.evidence,
                proposal_admission=bridge.admit,
                admission_receipt_projector=admission_receipt_projector,
                bind_policy_receipt=bind_policy_receipt,
                receipt_sink=MemoryR22PolicyReceiptSink(),
                metrics=R22PolicyMetrics(),
                output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
                timeout_seconds=self._transport_timeout_seconds,
                seam_policy_deadline_seconds=self._policy_timeout_seconds,
            )
            policy = LeanActiveRuntimePolicyV1(
                cast(GPT56SentinelPolicy[object, object], source_policy),
                coordinator=coordinator,
            )
            host_config = SentinelHostConfig(
                mode=self._mode,
                policy_timeout_ms=round(self._policy_timeout_seconds * 1_000),
            )
            sentinel = PromptSentinel(
                policy=policy,
                codec_registry=build_runtime_history_codec_resolver(),
                host_configs={
                    _QWEN_HOST_ID: host_config,
                    _MAI_HOST_ID: host_config,
                },
                receipt_sink=ExternalSentinelReceiptSink(
                    self._receipt_root,
                    repository_root=self._repository_root,
                ),
                global_switch=SentinelGlobalSwitch(),
            )
            return LeanSentinelTaskRuntimeV1(
                sentinel=sentinel,
                history_transport=history_transport,
            )
        except Exception:
            history_transport.close()
            raise


__all__ = ["LeanSentinelRunFactoryV1", "LeanSentinelTaskRuntimeV1"]
