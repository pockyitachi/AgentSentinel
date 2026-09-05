"""Runtime glue for the R2.4 two-phase, derived audit-detail channel.

The common seam supplies the trusted pre-provider stages.  The unchanged host
parser later supplies its ordinary provider return and parsed ``JSONAction``.
Provider/parser text is transient: :mod:`audit_detail` retains only hashes and
safe metadata, never chain-of-thought content.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from threading import Lock

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.contracts import SentinelMode
from mobile_world.runtime.sentinel.r2_3.session import RubricSessionStatus
from mobile_world.runtime.sentinel.r2_4.audit_detail import (
    ParserResultStatusV1,
    RuntimeAuditDetailBuilderV1,
    RuntimeAuditDetailSinkV1,
    RuntimeAuditDetailV1,
    RuntimeAuditStageLatenciesV1,
    snapshot_runtime_audit_detail,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    RuntimeHistoryExtractionResultV1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalPolicyOutputV1,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24RuntimeCoordinatorV1,
)
from mobile_world.runtime.sentinel.r2_4.renderer import (
    RuntimeVerticalRenderResultV1,
)


class R24RuntimeAuditError(R24ContractError):
    """Stable failure raised by the runtime audit coordinator."""


class R24RuntimeAuditV1:
    """Join pre-provider Sentinel stages to one later actor parser result.

    This object is a derived sidecar coordinator.  It never calls a provider,
    parser, MobileWorld tool, or action executor.  A configured runtime owns one
    instance and supplies it to the common seam and the two thin host adapters.
    """

    def __init__(
        self,
        *,
        coordinator: R24RuntimeCoordinatorV1,
        topology_comparison_sha256: str,
        sink: RuntimeAuditDetailSinkV1,
        detail_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if type(coordinator) is not R24RuntimeCoordinatorV1:
            raise TypeError("coordinator must use exact R24RuntimeCoordinatorV1")
        if not isinstance(sink, RuntimeAuditDetailSinkV1):
            raise TypeError("sink must implement RuntimeAuditDetailSinkV1")
        if (
            type(topology_comparison_sha256) is not str
            or len(topology_comparison_sha256) != 64
            or any(character not in "0123456789abcdef" for character in topology_comparison_sha256)
        ):
            raise ValueError("topology comparison hash must be lowercase SHA-256")
        if detail_id_factory is not None and not callable(detail_id_factory):
            raise TypeError("detail_id_factory must be callable")
        self._coordinator = coordinator
        self._topology_comparison_sha256 = topology_comparison_sha256
        self._sink_emit = sink.emit
        self._detail_id_factory = detail_id_factory or self._default_detail_id
        self._pending: dict[str, tuple[RuntimeAuditDetailBuilderV1, int]] = {}
        self._lock = Lock()

    @staticmethod
    def _default_detail_id(logical_call_id: str) -> str:
        digest = hashlib.sha256(logical_call_id.encode("utf-8")).hexdigest()
        return f"r24-detail-{digest[:32]}"

    def begin_pre_provider(
        self,
        *,
        logical_call_id: str,
        raw_request: JsonValue,
        extraction: RuntimeHistoryExtractionResultV1,
        policy_output: RuntimeVerticalPolicyOutputV1,
        render_result: RuntimeVerticalRenderResultV1,
        configured_mode: SentinelMode,
        effective_mode: SentinelMode,
        final_request: JsonValue,
        history_extract_ns: int,
        policy_ns: int,
        render_ns: int,
        validator_ns: int,
        pre_provider_total_ns: int,
    ) -> None:
        """Validate and retain detached pre-provider stages for one call."""

        if type(logical_call_id) is not str or not logical_call_id:
            raise R24RuntimeAuditError("INVALID_RUNTIME_ID", "logical call ID is invalid")
        record = self._coordinator.record_for(logical_call_id)
        if (
            record is None
            or record.logical_call_id != logical_call_id
            or record.rubric_result.status is not RubricSessionStatus.ADMITTED
            or record.rubric_result.relevance is None
        ):
            raise R24RuntimeAuditError(
                "RUBRIC_AUDIT_BINDING_UNAVAILABLE",
                "the coordinated isolated rubric result is unavailable",
            )
        if policy_output.admitted_plan.logical_call_id != logical_call_id:
            raise R24RuntimeAuditError(
                "TRACE_LOGICAL_CALL_MISMATCH", "policy and audit logical calls differ"
            )
        values = (
            history_extract_ns,
            policy_ns,
            render_ns,
            validator_ns,
            pre_provider_total_ns,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise R24RuntimeAuditError(
                "INVALID_STAGE_LATENCY", "pre-provider latencies are invalid"
            )
        pre_latencies = RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=record.evidence_snapshot_latency_ns,
            history_extract_ns=history_extract_ns,
            rubric_ns=record.topology_run.total_latency_ns,
            policy_ns=policy_ns,
            render_ns=render_ns,
            validator_ns=validator_ns,
            provider_ns=0,
            parser_ns=0,
            total_ns=max(
                pre_provider_total_ns,
                record.evidence_snapshot_latency_ns,
                record.topology_run.total_latency_ns,
                history_extract_ns,
                policy_ns,
                render_ns,
                validator_ns,
            ),
        )
        detail_id = self._detail_id_factory(logical_call_id)
        if type(detail_id) is not str or not detail_id:
            raise R24RuntimeAuditError("INVALID_RUNTIME_ID", "detail ID factory failed")
        builder = RuntimeAuditDetailBuilderV1.begin_pre_provider(
            detail_id=detail_id,
            raw_request=raw_request,
            extraction=extraction,
            policy_output=policy_output,
            rubric_output=record.rubric_result.relevance,
            render_result=render_result,
            configured_mode=configured_mode,
            effective_mode=effective_mode,
            final_request=final_request,
            topology_comparison_sha256=self._topology_comparison_sha256,
            pre_provider_latencies=pre_latencies,
        )
        with self._lock:
            if logical_call_id in self._pending:
                raise R24RuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "pre-provider stages were already registered"
                )
            self._pending[logical_call_id] = (builder, pre_latencies.total_ns)

    def cancel(self, logical_call_id: str) -> None:
        """Forget a pre-provider stage when the outer receipt cannot commit."""

        if type(logical_call_id) is not str:
            return
        with self._lock:
            self._pending.pop(logical_call_id, None)

    def finalize_actor_output(
        self,
        *,
        logical_call_id: str,
        attempt_id: str,
        raw_provider_response: JsonValue,
        raw_parser_input: JsonValue,
        parsed_action: JsonValue,
        parser_id: str,
        parser_status: ParserResultStatusV1,
        parser_attempt_count: int,
        provider_ns: int,
        parser_ns: int,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> RuntimeAuditDetailV1:
        """Build and emit the completed detail without changing actor output."""

        with self._lock:
            pending = self._pending.pop(logical_call_id, None)
        if pending is None:
            raise R24RuntimeAuditError(
                "AUDIT_PRE_PROVIDER_STAGE_MISSING", "no pre-provider stages exist for this call"
            )
        builder, pre_total_ns = pending
        if any(type(value) is not int or value < 0 for value in (provider_ns, parser_ns)):
            raise R24RuntimeAuditError(
                "INVALID_STAGE_LATENCY", "provider/parser latencies are invalid"
            )
        detail = builder.finalize_actor_output(
            attempt_id=attempt_id,
            raw_provider_response=raw_provider_response,
            raw_parser_input=raw_parser_input,
            parsed_action=parsed_action,
            parser_id=parser_id,
            parser_status=parser_status,
            parser_attempt_count=parser_attempt_count,
            provider_ns=provider_ns,
            parser_ns=parser_ns,
            total_ns=pre_total_ns + provider_ns + parser_ns,
            response_id=response_id,
            model_id=model_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        self._sink_emit(detail)
        return snapshot_runtime_audit_detail(detail)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


__all__ = ["R24RuntimeAuditError", "R24RuntimeAuditV1"]
