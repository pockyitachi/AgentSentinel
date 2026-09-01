"""R2.1 provider-free Prompt Sentinel runtime seam."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any

from mobile_world.offline.causal_replay.contracts import (
    ExecutionMode,
    FailurePolicy,
    FallbackState,
    HistoryCodecResolver,
    HistoryIR,
    JsonValue,
    OperationKind,
    PortableContractError,
    RenderResult,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    restore_original,
    validate_history_ir,
    validate_plan,
)
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.sentinel.contracts import (
    SentinelBypassReason,
    SentinelCallRole,
    SentinelContext,
    SentinelContractError,
    SentinelDecisionKind,
    SentinelFallbackReason,
    SentinelHostConfig,
    SentinelMode,
    SentinelPolicy,
    SentinelPolicyOutput,
    SentinelReceipt,
    SentinelReceiptSink,
    SentinelResult,
    SentinelValidationStatus,
)

_EMPTY_DIFF_SHA256 = canonical_sha256({"diffs": [], "list_insertions": []})
_EMPTY_POLICY_OUTPUT_SHA256 = canonical_sha256({"decisions": [], "transformation_plan": None})
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CHECK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CURRENT_LOGICAL_CALL: ContextVar[SentinelLogicalCall | None] = ContextVar(
    "mobileworld_prompt_sentinel_logical_call", default=None
)


class SentinelGlobalSwitch:
    """Process-wide, thread-safe emergency kill switch."""

    def __init__(self, *, active: bool = False) -> None:
        self._active = bool(active)
        self._lock = Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def set_active(self, active: bool) -> None:
        if type(active) is not bool:
            raise TypeError("kill switch state must be bool")
        with self._lock:
            self._active = active


GLOBAL_SENTINEL_KILL_SWITCH = SentinelGlobalSwitch()


def set_global_sentinel_kill_switch(active: bool) -> None:
    GLOBAL_SENTINEL_KILL_SWITCH.set_active(active)


def global_sentinel_kill_switch_active() -> bool:
    return GLOBAL_SENTINEL_KILL_SWITCH.active


def current_sentinel_logical_call() -> SentinelLogicalCall | None:
    return _CURRENT_LOGICAL_CALL.get()


@contextmanager
def bind_sentinel_logical_call(call: SentinelLogicalCall):
    if not isinstance(call, SentinelLogicalCall):
        raise TypeError("call must be SentinelLogicalCall")
    token: Token[SentinelLogicalCall | None] = _CURRENT_LOGICAL_CALL.set(call)
    try:
        yield call
    finally:
        _CURRENT_LOGICAL_CALL.reset(token)


@dataclass(frozen=True)
class _EvaluationFailure(Exception):
    reason: SentinelFallbackReason
    check: str


class PromptSentinel:
    """Single pre-provider hook with typed Original fallback semantics."""

    def __init__(
        self,
        *,
        policy: SentinelPolicy,
        codec_registry: HistoryCodecResolver,
        host_configs: dict[str, SentinelHostConfig] | None = None,
        default_host_config: SentinelHostConfig | None = None,
        receipt_sink: SentinelReceiptSink | None = None,
        global_switch: SentinelGlobalSwitch = GLOBAL_SENTINEL_KILL_SWITCH,
        logical_call_id_factory: Any = new_ulid,
        clock_ns: Any = time.monotonic_ns,
    ) -> None:
        if not isinstance(policy, SentinelPolicy):
            raise TypeError("policy must implement SentinelPolicy")
        if not isinstance(codec_registry, HistoryCodecResolver):
            raise TypeError("codec_registry must implement HistoryCodecResolver")
        if receipt_sink is not None and not isinstance(receipt_sink, SentinelReceiptSink):
            raise TypeError("receipt_sink must implement SentinelReceiptSink")
        if not isinstance(global_switch, SentinelGlobalSwitch):
            raise TypeError("global_switch must be SentinelGlobalSwitch")
        if not callable(logical_call_id_factory) or not callable(clock_ns):
            raise TypeError("ID factory and clock must be callable")
        configs = dict(host_configs or {})
        if any(
            not key or not isinstance(value, SentinelHostConfig) for key, value in configs.items()
        ):
            raise TypeError("host_configs must map non-empty host IDs to SentinelHostConfig")
        policy_id = policy.policy_id
        if not isinstance(policy_id, str) or _SEMANTIC_ID.fullmatch(policy_id) is None:
            raise TypeError("policy.policy_id must be a bounded safe identifier")
        self._policy = policy
        self._policy_id = policy_id
        self._codec_registry = codec_registry
        self._host_configs = configs
        self._default_host_config = default_host_config or SentinelHostConfig()
        self._receipt_sink = receipt_sink
        self._global_switch = global_switch
        self._logical_call_id_factory = logical_call_id_factory
        self._clock_ns = clock_ns

    @property
    def policy(self) -> SentinelPolicy:
        return self._policy

    @property
    def kill_switch_active(self) -> bool:
        return self._global_switch.active

    def host_config(self, host_id: str) -> SentinelHostConfig:
        return self._host_configs.get(host_id, self._default_host_config)

    def logical_call(
        self,
        *,
        host_id: str,
        history_codec_id: str | None,
        call_role: SentinelCallRole = SentinelCallRole.ACTOR,
        attributes: dict[str, JsonValue] | None = None,
    ) -> SentinelLogicalCall:
        if not host_id:
            raise ValueError("host_id is required")
        if history_codec_id is not None and (
            not isinstance(history_codec_id, str)
            or _SEMANTIC_ID.fullmatch(history_codec_id) is None
        ):
            raise ValueError("history_codec_id must be a bounded safe identifier")
        logical_call_id = self._logical_call_id_factory()
        if not isinstance(logical_call_id, str) or not logical_call_id:
            raise SentinelContractError("logical-call ID factory returned an invalid value")
        context = SentinelContext(
            logical_call_id=logical_call_id,
            host_id=host_id,
            attributes={} if attributes is None else attributes,
        )
        return SentinelLogicalCall(
            sentinel=self,
            context=context,
            history_codec_id=history_codec_id,
            call_role=call_role,
        )

    def before_model_call(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole | str,
    ) -> SentinelResult:
        """Evaluate one actor request and return an immutable separate final object."""

        started = self._clock_ns()
        policy_output_sha256 = _EMPTY_POLICY_OUTPUT_SHA256
        try:
            role = SentinelCallRole(call_role)
        except ValueError as exc:
            raise SentinelContractError("call_role must be actor or sentinel") from exc
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        raw_sha256 = canonical_sha256(raw)
        config = self.host_config(context.host_id)
        kill_switch_active = self._global_switch.active

        if role is SentinelCallRole.SENTINEL:
            return self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.CALL_ROLE_SENTINEL,
                kill_switch_active=kill_switch_active,
                started=started,
            )
        if kill_switch_active:
            return self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.GLOBAL_KILL_SWITCH,
                kill_switch_active=True,
                started=started,
            )
        if config.mode is SentinelMode.OFF:
            return self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.MODE_OFF,
                kill_switch_active=False,
                started=started,
            )
        if self._receipt_sink is None:
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.SIDECAR_FAILURE,
                check="semantic_mode_requires_receipt_sink",
                started=started,
                emit=False,
                policy_evaluated=False,
            )

        try:
            self._validate_request_schema(raw)
            if not history_codec_id:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "history_codec_id_missing",
                )
            try:
                codec = self._codec_registry.by_id(
                    history_codec_id, config.history_codec_contract_version
                )
            except PortableContractError as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_resolution_failed",
                ) from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_resolution_exception",
                ) from exc
            if not isinstance(codec, HistoryCodec):
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_has_no_runtime_renderer",
                )
            try:
                ir = codec.extract(copy_json(raw))
            except PortableContractError as exc:
                reason = (
                    SentinelFallbackReason.AMBIGUOUS_HISTORY_SPAN
                    if exc.code
                    in {
                        "TARGET_BINDING_AMBIGUOUS",
                        "OVERLAPPING_HISTORY_SPAN",
                    }
                    else SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
                )
                raise _EvaluationFailure(reason, "history_extract_failed") from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_extract_exception",
                ) from exc
            try:
                self._validate_extracted_ir(
                    raw=raw,
                    context=context,
                    history_codec_id=history_codec_id,
                    config=config,
                    codec=codec,
                    ir=ir,
                )
            except PortableContractError as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_ir_validation_failed",
                ) from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_ir_validation_exception",
                ) from exc

            output = self._evaluate_policy_with_timeout(
                request=raw,
                context=context,
                history_ir=ir,
                timeout_ms=config.policy_timeout_ms,
            )
            try:
                self._validate_policy_output(output)
                if output.transformation_plan is None:
                    self._validate_no_plan_decisions(output)
                else:
                    self._validate_decision_plan_binding(output)
                    validate_plan(raw, ir, output.transformation_plan)
                policy_output_sha256 = canonical_sha256(output.to_dict())
            except _EvaluationFailure:
                raise
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                    "policy_output_validation_exception",
                ) from exc

            candidate = copy_json(raw)
            render_result: RenderResult | None = None
            checks = ["request_schema", "codec_extract", "policy_output_schema"]
            if output.transformation_plan is not None:
                try:
                    render_result = codec.render(
                        copy_json(raw),
                        ir,
                        output.transformation_plan,
                        execution_mode=ExecutionMode.RUNTIME,
                        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
                    )
                except PortableContractError as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.RENDERER_FAILURE,
                        "render_failed",
                    ) from exc
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.RENDERER_FAILURE,
                        "render_exception",
                    ) from exc
                try:
                    self._validate_render_result(raw, ir, output, render_result)
                except (PortableContractError, SentinelContractError) as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVARIANT_FAILURE,
                        "invariant_validation_failed",
                    ) from exc
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVARIANT_FAILURE,
                        "invariant_validation_exception",
                    ) from exc
                candidate = copy_json(render_result.rendered_request)
                checks.extend(
                    (
                        "g1_2_exact_span_render",
                        "independent_render_recomputed",
                        "reversible_source_mapping",
                        "caller_input_immutable",
                    )
                )
            else:
                checks.append("no_transform_proposed")

            if canonical_sha256(request) != raw_sha256:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVARIANT_FAILURE,
                    "caller_input_mutated",
                )
            if self._global_switch.active:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVARIANT_FAILURE,
                    "global_kill_switch_activated_during_evaluation",
                )
            candidate_json = canonical_json_bytes(candidate)
            candidate_sha256 = canonical_sha256(candidate)
            would_edit = candidate_sha256 != raw_sha256
            edit_applied = config.mode is SentinelMode.ACTIVE and would_edit
            final = candidate if edit_applied else raw
            final_json = canonical_json_bytes(final)
            diff_sha256 = self._diff_sha256(render_result)
            receipt = SentinelReceipt(
                logical_call_id=context.logical_call_id,
                host_id=context.host_id,
                call_role=role,
                configured_mode=config.mode,
                effective_mode=config.mode,
                bypass_reason=None,
                global_kill_switch_active=False,
                history_codec_id=history_codec_id,
                history_codec_contract_version=config.history_codec_contract_version,
                policy_id=self._policy_id,
                policy_output_sha256=policy_output_sha256,
                raw_request_sha256=raw_sha256,
                candidate_request_sha256=candidate_sha256,
                final_request_sha256=canonical_sha256(final),
                exact_diff_sha256=diff_sha256,
                decision_kinds=tuple(item.kind for item in output.decisions),
                policy_evaluated=True,
                would_edit=would_edit,
                edit_applied=edit_applied,
                fallback_reason=None,
                validation_status=SentinelValidationStatus.PASSED,
                validation_checks=tuple(checks),
                latency_ns=self._elapsed(started),
            )
            return self._finalize(
                receipt=receipt,
                raw_json=raw_json,
                candidate_json=candidate_json,
                final_json=final_json,
                fallback_context=(raw, context, config, role, history_codec_id, started),
            )
        except _EvaluationFailure as failure:
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=failure.reason,
                check=failure.check,
                started=started,
                policy_output_sha256=policy_output_sha256,
            )

    def _evaluate_policy_with_timeout(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: Any,
        timeout_ms: int,
    ) -> SentinelPolicyOutput:
        """Run the replaceable backend behind a real, bounded daemon wait."""

        finished = Event()
        outcome: list[tuple[bool, Any]] = []
        policy_context = copy_context()

        def evaluate() -> None:
            try:
                value = self._policy.evaluate(
                    request=copy_json(request),
                    context=context,
                    history_ir=history_ir,
                )
            except BaseException as error:
                outcome.append((False, error))
            else:
                outcome.append((True, value))
            finally:
                finished.set()

        worker = Thread(
            target=policy_context.run,
            args=(evaluate,),
            name="mobileworld-prompt-sentinel-policy",
            daemon=True,
        )
        policy_started = self._clock_ns()
        worker.start()
        if not finished.wait(timeout_ms / 1000):
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_TIMEOUT,
                "policy_deadline_exceeded",
            )
        policy_elapsed = self._clock_ns() - policy_started
        if policy_elapsed > timeout_ms * 1_000_000:
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_TIMEOUT,
                "policy_latency_budget_exceeded",
            )
        if not outcome:
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_EXCEPTION,
                "policy_worker_terminated_without_result",
            )
        succeeded, value = outcome[0]
        if not succeeded:
            if isinstance(value, TimeoutError):
                raise _EvaluationFailure(
                    SentinelFallbackReason.POLICY_TIMEOUT,
                    "policy_timeout_exception",
                )
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_EXCEPTION,
                "policy_exception",
            )
        return value

    @staticmethod
    def _validate_extracted_ir(
        *,
        raw: JsonValue,
        context: SentinelContext,
        history_codec_id: str,
        config: SentinelHostConfig,
        codec: HistoryCodec,
        ir: Any,
    ) -> None:
        if not isinstance(ir, HistoryIR):
            raise PortableContractError(
                "HISTORY_IR_TYPE_MISMATCH",
                "codec extraction must return the canonical HistoryIR type",
            )
        validate_history_ir(raw, ir)
        if (
            ir.host_id != context.host_id
            or ir.codec_id != history_codec_id
            or ir.codec_contract_version != config.history_codec_contract_version
            or ir.history_family is not codec.history_family
            or ir.capabilities != codec.capabilities
        ):
            raise PortableContractError(
                "HISTORY_IR_BINDING_MISMATCH",
                "extracted IR differs from the selected host or codec declaration",
            )

    @staticmethod
    def _validate_request_schema(request: JsonValue) -> None:
        if not isinstance(request, dict):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_root_not_object"
            )
        if not isinstance(request.get("model"), str) or not request["model"]:
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_model_missing"
            )
        if not isinstance(request.get("messages"), list):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_messages_missing"
            )

    @staticmethod
    def _validate_policy_output(output: Any) -> None:
        if not isinstance(output, SentinelPolicyOutput):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT, "policy_output_type"
            )
        if len({item.decision_id for item in output.decisions}) != len(output.decisions):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT, "duplicate_decision_id"
            )

    @staticmethod
    def _validate_no_plan_decisions(output: SentinelPolicyOutput) -> None:
        if any(
            item.kind not in {SentinelDecisionKind.KEEP, SentinelDecisionKind.KEEP_UNCERTAIN}
            or item.operation_id is not None
            for item in output.decisions
        ):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "no_plan_decision_requires_keep_or_abstain",
            )

    @staticmethod
    def _validate_decision_plan_binding(output: SentinelPolicyOutput) -> None:
        plan = output.transformation_plan
        assert plan is not None
        if any(item.kind is OperationKind.REPLACE for item in plan.operations):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "r2_1_replace_requires_later_runtime_plan_overlay",
            )
        if any(
            item.operation_id is None
            and item.kind
            not in {
                SentinelDecisionKind.KEEP,
                SentinelDecisionKind.KEEP_UNCERTAIN,
            }
            for item in output.decisions
        ):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "material_decision_missing_operation_id",
            )
        operations = {item.operation_id: item for item in plan.operations}
        decision_operations = {
            item.operation_id: item for item in output.decisions if item.operation_id is not None
        }
        bound_decision_count = sum(item.operation_id is not None for item in output.decisions)
        if len(decision_operations) != bound_decision_count:
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "duplicate_decision_operation_binding",
            )
        if set(operations) != set(decision_operations):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "decision_operation_census_mismatch",
            )
        kind_map = {
            OperationKind.KEEP: SentinelDecisionKind.KEEP,
            OperationKind.DROP: SentinelDecisionKind.DROP,
            OperationKind.REPLACE: SentinelDecisionKind.REPLACE,
            OperationKind.KEEP_UNCERTAIN: SentinelDecisionKind.KEEP_UNCERTAIN,
        }
        for operation_id, operation in operations.items():
            expected = kind_map.get(operation.kind)
            decision = decision_operations[operation_id]
            if expected is None or decision.kind is not expected:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                    "decision_operation_kind_mismatch",
                )
            if decision.record_id != operation.target_record_id:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                    "decision_operation_record_mismatch",
                )

    @staticmethod
    def _validate_render_result(
        raw: JsonValue,
        ir: Any,
        output: SentinelPolicyOutput,
        result: RenderResult,
    ) -> None:
        plan = output.transformation_plan
        assert plan is not None
        if result.list_insertions:
            raise SentinelContractError(
                "R2.1 history-only runtime cannot insert current-observation blocks"
            )
        if result.fallback_state is not FallbackState.NOT_NEEDED:
            raise SentinelContractError("renderer returned a fallback instead of a candidate")
        expected = render_request(
            copy_json(raw),
            ir,
            plan,
            execution_mode=ExecutionMode.RUNTIME,
            failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
        )
        if canonical_sha256(result.to_dict()) != canonical_sha256(expected.to_dict()):
            raise SentinelContractError("renderer result differs from G1.2 recomputation")
        if result.original_request != raw or result.source_request_sha256 != canonical_sha256(raw):
            raise SentinelContractError("renderer source binding differs from raw request")
        if result.rendered_request_sha256 != canonical_sha256(result.rendered_request):
            raise SentinelContractError("renderer final hash is invalid")
        if restore_original(result) != raw:
            raise SentinelContractError("renderer mapping cannot restore Original")

    @staticmethod
    def _diff_sha256(result: RenderResult | None) -> str:
        if result is None:
            return _EMPTY_DIFF_SHA256
        return canonical_sha256(
            {
                "diffs": [item.to_dict() for item in result.diffs],
                "list_insertions": [item.to_dict() for item in result.list_insertions],
            }
        )

    def _bypass_result(
        self,
        *,
        raw: JsonValue,
        raw_json: bytes,
        raw_sha256: str,
        context: SentinelContext,
        config: SentinelHostConfig,
        role: SentinelCallRole,
        history_codec_id: str | None,
        bypass_reason: SentinelBypassReason,
        kill_switch_active: bool,
        started: int,
        emit: bool = True,
    ) -> SentinelResult:
        receipt = SentinelReceipt(
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            call_role=role,
            configured_mode=config.mode,
            effective_mode=SentinelMode.OFF,
            bypass_reason=bypass_reason,
            global_kill_switch_active=kill_switch_active,
            history_codec_id=history_codec_id,
            history_codec_contract_version=(
                None if history_codec_id is None else config.history_codec_contract_version
            ),
            policy_id=None,
            policy_output_sha256=_EMPTY_POLICY_OUTPUT_SHA256,
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=_EMPTY_DIFF_SHA256,
            decision_kinds=(),
            policy_evaluated=False,
            would_edit=False,
            edit_applied=False,
            fallback_reason=None,
            validation_status=SentinelValidationStatus.BYPASSED,
            validation_checks=(bypass_reason.value,),
            latency_ns=self._elapsed(started),
        )
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=raw_json,
            _final_request_json=raw_json,
        )
        if not emit:
            return result
        return self._finalize(
            receipt=receipt,
            raw_json=raw_json,
            candidate_json=raw_json,
            final_json=raw_json,
            fallback_context=(raw, context, config, role, history_codec_id, started),
        )

    def bypass_reuse(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
        prior_receipt: SentinelReceipt,
    ) -> SentinelResult:
        """Bind the current Original without semantic work after a cached bypass."""

        if prior_receipt.bypass_reason is None:
            raise SentinelContractError("bypass reuse requires a prior bypass receipt")
        started = self._clock_ns()
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        return self._bypass_result(
            raw=raw,
            raw_json=raw_json,
            raw_sha256=canonical_sha256(raw),
            context=context,
            config=self.host_config(context.host_id),
            role=call_role,
            history_codec_id=history_codec_id,
            bypass_reason=prior_receipt.bypass_reason,
            kill_switch_active=prior_receipt.global_kill_switch_active,
            started=started,
            emit=False,
        )

    def _fallback_result(
        self,
        *,
        raw: JsonValue,
        raw_json: bytes,
        raw_sha256: str,
        context: SentinelContext,
        config: SentinelHostConfig,
        role: SentinelCallRole,
        history_codec_id: str | None,
        reason: SentinelFallbackReason,
        check: str,
        started: int,
        emit: bool = True,
        policy_evaluated: bool | None = None,
        policy_output_sha256: str = _EMPTY_POLICY_OUTPUT_SHA256,
    ) -> SentinelResult:
        if policy_evaluated is None:
            policy_evaluated = reason not in {
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA,
                SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                SentinelFallbackReason.AMBIGUOUS_HISTORY_SPAN,
                SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                SentinelFallbackReason.REQUEST_DRIFT,
            }
        receipt = SentinelReceipt(
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            call_role=role,
            configured_mode=config.mode,
            effective_mode=SentinelMode.OFF,
            bypass_reason=None,
            global_kill_switch_active=self._global_switch.active,
            history_codec_id=history_codec_id,
            history_codec_contract_version=(
                None if history_codec_id is None else config.history_codec_contract_version
            ),
            policy_id=self._policy_id,
            policy_output_sha256=policy_output_sha256,
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=_EMPTY_DIFF_SHA256,
            decision_kinds=(),
            policy_evaluated=policy_evaluated,
            would_edit=False,
            edit_applied=False,
            fallback_reason=reason,
            validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
            validation_checks=(self._safe_check_code(check),),
            latency_ns=self._elapsed(started),
        )
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=raw_json,
            _final_request_json=raw_json,
        )
        if emit and self._receipt_sink is not None:
            try:
                self._receipt_sink.emit(receipt)
            except Exception:
                return self._fallback_result(
                    raw=raw,
                    raw_json=raw_json,
                    raw_sha256=raw_sha256,
                    context=context,
                    config=config,
                    role=role,
                    history_codec_id=history_codec_id,
                    reason=SentinelFallbackReason.SIDECAR_FAILURE,
                    check="sidecar_emit_failed",
                    started=started,
                    emit=False,
                    policy_evaluated=receipt.policy_evaluated,
                    policy_output_sha256=receipt.policy_output_sha256,
                )
        return result

    def _finalize(
        self,
        *,
        receipt: SentinelReceipt,
        raw_json: bytes,
        candidate_json: bytes,
        final_json: bytes,
        fallback_context: tuple[
            JsonValue,
            SentinelContext,
            SentinelHostConfig,
            SentinelCallRole,
            str | None,
            int,
        ],
    ) -> SentinelResult:
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=candidate_json,
            _final_request_json=final_json,
        )
        if self._receipt_sink is None:
            return result
        try:
            self._receipt_sink.emit(receipt)
        except Exception:
            if receipt.validation_status is SentinelValidationStatus.BYPASSED:
                return result
            raw, context, config, role, history_codec_id, started = fallback_context
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=receipt.raw_request_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.SIDECAR_FAILURE,
                check="sidecar_emit_failed",
                started=started,
                emit=False,
                policy_evaluated=receipt.policy_evaluated,
                policy_output_sha256=receipt.policy_output_sha256,
            )
        return result

    def request_drift_fallback(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
        policy_evaluated: bool,
        policy_output_sha256: str,
    ) -> SentinelResult:
        started = self._clock_ns()
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        return self._fallback_result(
            raw=raw,
            raw_json=raw_json,
            raw_sha256=canonical_sha256(raw),
            context=context,
            config=self.host_config(context.host_id),
            role=call_role,
            history_codec_id=history_codec_id,
            reason=SentinelFallbackReason.REQUEST_DRIFT,
            check="cached_raw_request_sha256_mismatch",
            started=started,
            emit=False,
            policy_evaluated=policy_evaluated,
            policy_output_sha256=policy_output_sha256,
        )

    def _elapsed(self, started: int) -> int:
        value = self._clock_ns() - started
        return max(0, int(value))

    @staticmethod
    def _safe_check_code(check: Any) -> str:
        if isinstance(check, str) and _CHECK_CODE.fullmatch(check) is not None:
            return check
        return "unsafe_external_check_code_redacted"


class SentinelLogicalCall:
    """Short-lived evaluate-once cache for one already assembled actor request."""

    def __init__(
        self,
        *,
        sentinel: PromptSentinel,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
    ) -> None:
        if not isinstance(call_role, SentinelCallRole):
            raise TypeError("call_role must be SentinelCallRole")
        self._sentinel = sentinel
        self._context = context
        self._history_codec_id = history_codec_id
        self._call_role = call_role
        self._result: SentinelResult | None = None
        self._lock = Lock()

    @property
    def sentinel(self) -> PromptSentinel:
        return self._sentinel

    @property
    def context(self) -> SentinelContext:
        return self._context

    @property
    def history_codec_id(self) -> str | None:
        return self._history_codec_id

    @property
    def call_role(self) -> SentinelCallRole:
        return self._call_role

    @property
    def result(self) -> SentinelResult | None:
        with self._lock:
            return self._result

    def before_model_call(self, request: JsonValue) -> SentinelResult:
        request_sha256 = canonical_sha256(request)
        with self._lock:
            if self._result is not None:
                if self._result.receipt.raw_request_sha256 == request_sha256:
                    return self._result
                if self._result.receipt.validation_status is SentinelValidationStatus.BYPASSED:
                    return self._sentinel.bypass_reuse(
                        request=request,
                        context=self._context,
                        history_codec_id=self._history_codec_id,
                        call_role=self._call_role,
                        prior_receipt=self._result.receipt,
                    )
                return self._sentinel.request_drift_fallback(
                    request=request,
                    context=self._context,
                    history_codec_id=self._history_codec_id,
                    call_role=self._call_role,
                    policy_evaluated=self._result.receipt.policy_evaluated,
                    policy_output_sha256=self._result.receipt.policy_output_sha256,
                )
            self._result = self._sentinel.before_model_call(
                request,
                self._context,
                self._history_codec_id,
                self._call_role,
            )
            return self._result

    def matches(
        self,
        sentinel: PromptSentinel,
        *,
        host_id: str,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
    ) -> bool:
        return (
            self._sentinel is sentinel
            and self._context.host_id == host_id
            and self._history_codec_id == history_codec_id
            and self._call_role is call_role
        )


__all__ = [
    "GLOBAL_SENTINEL_KILL_SWITCH",
    "PromptSentinel",
    "SentinelGlobalSwitch",
    "SentinelLogicalCall",
    "bind_sentinel_logical_call",
    "current_sentinel_logical_call",
    "global_sentinel_kill_switch_active",
    "set_global_sentinel_kill_switch",
]
