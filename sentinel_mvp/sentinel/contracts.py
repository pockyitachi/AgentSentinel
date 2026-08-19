"""Public data contracts for the Sentinel history gate.

The contracts deliberately separate two questions:

* :class:`EpistemicStatus` says what the evidence establishes about a claim.
* :class:`GateOperation` says what the prompt renderer is allowed to do with it.

``Verdict`` is the small, user-facing vocabulary used by the MVP.  It maps
one-to-one to the canonical operation names used in the proposal; keeping the
mapping explicit prevents an evidence verdict from accidentally becoming a
prompt-edit instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class _StringEnum(str, Enum):
    """A JSON-friendly Enum with useful ``str(value)`` behaviour."""

    def __str__(self) -> str:
        return self.value


class EpistemicStatus(_StringEnum):
    """Evidence status of a historical claim."""

    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class GateOperation(_StringEnum):
    """Canonical history-gate operations from proposal section 3.4."""

    KEEP = "KEEP"
    DROP = "DROP"
    REPLACE = "REPLACE"
    ARCHIVE = "ARCHIVE"
    KEEP_UNCERTAIN = "KEEP_UNCERTAIN"


class Verdict(_StringEnum):
    """Claim-level renderer verdicts exposed by the MVP API."""

    KEEP = "KEEP"
    MASK = "MASK"
    CORRECT = "CORRECT"
    LOW_RELEVANCE = "LOW_RELEVANCE"
    ABSTAIN = "ABSTAIN"


VERDICT_TO_OPERATION: Mapping[Verdict, GateOperation] = {
    Verdict.KEEP: GateOperation.KEEP,
    Verdict.MASK: GateOperation.DROP,
    Verdict.CORRECT: GateOperation.REPLACE,
    Verdict.LOW_RELEVANCE: GateOperation.ARCHIVE,
    Verdict.ABSTAIN: GateOperation.KEEP_UNCERTAIN,
}

OPERATION_TO_VERDICT: Mapping[GateOperation, Verdict] = {
    operation: verdict for verdict, operation in VERDICT_TO_OPERATION.items()
}


def operation_for(verdict: Verdict | str) -> GateOperation:
    """Return the canonical gate operation for a renderer verdict."""

    return VERDICT_TO_OPERATION[Verdict(verdict)]


def verdict_for(operation: GateOperation | str) -> Verdict:
    """Return the renderer verdict corresponding to a canonical operation."""

    return OPERATION_TO_VERDICT[GateOperation(operation)]


@dataclass(slots=True)
class EvidenceRef:
    """A reference to evidence kept in Sentinel's audit sidecar.

    ``direct`` must only be set when this evidence itself shows the relevant
    GUI/executor fact.  A heuristic score, task failure, or another model's
    assertion is not direct evidence.
    """

    evidence_id: str
    source_type: str
    description: str = ""
    direct: bool = False
    step_index: int | None = None
    locator: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("evidence_id must be non-empty")
        if not self.source_type:
            raise ValueError("source_type must be non-empty")

    @property
    def id(self) -> str:
        """Duck-typed adapter alias for ``evidence_id``."""

        return self.evidence_id

    @property
    def ref(self) -> str:
        """Duck-typed adapter alias for ``evidence_id``."""

        return self.evidence_id

    @property
    def source(self) -> str:
        """Duck-typed adapter alias for ``source_type``."""

        return self.source_type


@dataclass(slots=True)
class Claim:
    """An atomic claim located by a half-open character span ``[start, end)``.

    Spans always refer to the *original* text of ``record_id``.  This makes
    several claims in one history record independently editable and keeps the
    operation reversible in the sidecar.
    """

    claim_id: str
    record_id: str
    text: str
    start: int
    end: int
    epistemic_status: EpistemicStatus = EpistemicStatus.UNVERIFIABLE
    verdict: Verdict = Verdict.ABSTAIN
    correction: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    rationale: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        self.record_id = str(self.record_id)
        self.epistemic_status = EpistemicStatus(self.epistemic_status)
        self.verdict = Verdict(self.verdict)
        self.evidence_refs = tuple(self.evidence_refs)
        if not self.claim_id:
            raise ValueError("claim_id must be non-empty")
        if not self.record_id:
            raise ValueError("record_id must be non-empty")
        if self.start < 0 or self.end < self.start:
            raise ValueError("claim span must satisfy 0 <= start <= end")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    @property
    def source_record_id(self) -> str:
        """Proposal-compatible alias for ``record_id``."""

        return self.record_id

    @property
    def source_span(self) -> tuple[int, int]:
        """Return the half-open source span."""

        return (self.start, self.end)

    @property
    def gate_operation(self) -> GateOperation:
        """Requested canonical operation before safety checks."""

        return operation_for(self.verdict)

    @property
    def has_direct_evidence(self) -> bool:
        return any(evidence.direct for evidence in self.evidence_refs)


@dataclass(slots=True)
class InitInput:
    """Input to the one-time task initialization call."""

    task_instruction: str
    task_id: str | None = None
    host: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_instruction.strip():
            raise ValueError("task_instruction must be non-empty")


@dataclass(slots=True)
class StepInput:
    """Runtime input immediately before a host model call.

    The mappings mirror proposal section 3.4 without imposing a benchmark-
    specific message or screenshot representation.
    """

    task_instruction: str
    step_index: int
    history: Sequence[Any]
    claims: Sequence[Claim] = ()
    task_id: str | None = None
    rubric: Mapping[str, Any] = field(default_factory=dict)
    host_request: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    sidecar: Mapping[str, Any] = field(default_factory=dict)
    host_capabilities: Mapping[str, Any] = field(default_factory=dict)
    runtime_budget: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_instruction.strip():
            raise ValueError("task_instruction must be non-empty")
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")


@dataclass(slots=True)
class PromptOperation:
    """One audited, reversible prompt operation on a claim span."""

    record_id: str
    claim_id: str
    source_span: tuple[int, int]
    operation: GateOperation
    original_text: str
    replacement_text: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    epistemic_status: EpistemicStatus = EpistemicStatus.UNVERIFIABLE
    reason: str = ""
    reversible: bool = True
    applied: bool = False
    requested_verdict: Verdict = Verdict.ABSTAIN

    def __post_init__(self) -> None:
        self.record_id = str(self.record_id)
        self.operation = GateOperation(self.operation)
        self.epistemic_status = EpistemicStatus(self.epistemic_status)
        self.requested_verdict = Verdict(self.requested_verdict)
        self.evidence_refs = tuple(self.evidence_refs)
        start, end = self.source_span
        if start < 0 or end < start:
            raise ValueError("source_span must satisfy 0 <= start <= end")

    @property
    def start(self) -> int:
        return self.source_span[0]

    @property
    def end(self) -> int:
        return self.source_span[1]

    @property
    def verdict(self) -> Verdict:
        """Effective renderer verdict after all safety checks."""

        return verdict_for(self.operation)

    @property
    def evidence(self) -> tuple[EvidenceRef, ...]:
        """Duck-typed adapter alias for ``evidence_refs``."""

        return self.evidence_refs

    @property
    def rationale(self) -> str:
        """Duck-typed adapter alias for the effective operation reason."""

        return self.reason


@dataclass(slots=True)
class SentinelOutput:
    """Audited operations plus a benchmark-neutral history preview.

    ``filtered_history`` is deliberately an audit preview, not a protocol-
    complete model request.  A host adapter is the only component allowed to
    render these operations into its real role/content/tool message schema.
    """

    filtered_history: list[Any]
    operations: list[PromptOperation]
    correction_block: str = ""
    claims: list[Claim] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    task_id: str | None = None
    step_index: int | None = None

    @property
    def active_history(self) -> list[Any]:
        """Backward-compatible alias for the non-deployment audit preview."""

        return self.filtered_history

    @property
    def audit_preview_history(self) -> list[Any]:
        """Explicit name for ``filtered_history``'s actual contract."""

        return self.filtered_history

    @property
    def history_decisions(self) -> list[PromptOperation]:
        """Proposal-compatible alias for ``operations``."""

        return self.operations

    @property
    def changed(self) -> bool:
        return any(operation.applied for operation in self.operations)
