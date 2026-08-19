from __future__ import annotations

from copy import deepcopy
import unittest

from sentinel import (
    ARCHIVE_MARKER,
    MASK_MARKER,
    Claim,
    EpistemicStatus,
    EvidenceRef,
    GateOperation,
    Verdict,
    filter_history,
)
from sentinel.seed_adapter import adapt_seed_history, extract_seed_records


def direct_evidence(evidence_id: str = "S2") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type="screenshot",
        description="visible GUI evidence",
        direct=True,
    )


def weak_evidence(evidence_id: str = "heuristic-1") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type="similarity_score",
        description="retrieval signal only",
        direct=False,
    )


class HistoryFilterTests(unittest.TestCase):
    def test_keep_and_keep_uncertain_preserve_original_text(self) -> None:
        history = [{"id": "R1", "text": "Search opened. Cart may be empty."}]
        claims = [
            Claim(
                claim_id="c-supported",
                record_id="R1",
                text="Search opened",
                start=0,
                end=len("Search opened"),
                epistemic_status=EpistemicStatus.SUPPORTED,
                verdict=Verdict.KEEP,
                evidence_refs=(direct_evidence(),),
            ),
            Claim(
                claim_id="c-unknown",
                record_id="R1",
                text="Cart may be empty",
                start=15,
                end=32,
                epistemic_status=EpistemicStatus.UNVERIFIABLE,
                verdict=Verdict.ABSTAIN,
            ),
        ]

        output = filter_history(history, claims)

        self.assertEqual(output.filtered_history, history)
        self.assertFalse(output.changed)
        self.assertEqual(
            [operation.operation for operation in output.operations],
            [GateOperation.KEEP, GateOperation.KEEP_UNCERTAIN],
        )
        self.assertEqual(output.correction_block, "")

    def test_direct_refuted_drop_masks_only_the_claim_span(self) -> None:
        history = [{"id": "R1", "text": "Before. Filter succeeded. After."}]
        bad_claim = "Filter succeeded"
        start = history[0]["text"].index(bad_claim)
        claim = Claim(
            claim_id="c-filter",
            record_id="R1",
            text=bad_claim,
            start=start,
            end=start + len(bad_claim),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.MASK,
            evidence_refs=(direct_evidence("S3"),),
        )

        output = filter_history(history, [claim])

        rendered = output.filtered_history[0]["text"]
        self.assertEqual(rendered, f"Before. {MASK_MARKER}. After.")
        self.assertNotIn(bad_claim, rendered)
        self.assertEqual(output.operations[0].operation, GateOperation.DROP)
        self.assertTrue(output.operations[0].applied)
        self.assertIn("Evidence: S3", output.correction_block)
        self.assertNotIn(bad_claim, output.correction_block)

    def test_direct_refuted_replace_inserts_minimal_correction(self) -> None:
        history = [{"id": "R6", "text": "Email says November 1. Continue."}]
        bad_claim = "Email says November 1"
        start = history[0]["text"].index(bad_claim)
        claim = Claim(
            claim_id="c-date",
            record_id="R6",
            text=bad_claim,
            start=start,
            end=start + len(bad_claim),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.CORRECT,
            correction="Email says November 15",
            evidence_refs=(direct_evidence("S3-email"),),
        )

        output = filter_history(history, [claim])

        self.assertEqual(
            output.filtered_history[0]["text"],
            "Email says November 15. Continue.",
        )
        self.assertEqual(output.operations[0].operation, GateOperation.REPLACE)
        self.assertTrue(output.operations[0].applied)
        self.assertIn("Email says November 15", output.correction_block)
        self.assertIn("S3-email", output.correction_block)

    def test_weak_evidence_downgrades_destructive_edit(self) -> None:
        history = [{"id": "R2", "text": "The filter succeeded."}]
        bad_claim = "The filter succeeded"
        claim = Claim(
            claim_id="c-weak",
            record_id="R2",
            text=bad_claim,
            start=0,
            end=len(bad_claim),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.MASK,
            evidence_refs=(weak_evidence(),),
        )

        output = filter_history(history, [claim])

        self.assertEqual(output.filtered_history, history)
        self.assertEqual(output.operations[0].operation, GateOperation.KEEP_UNCERTAIN)
        self.assertFalse(output.operations[0].applied)
        self.assertIn("no direct evidence", output.operations[0].reason)
        self.assertFalse(output.changed)

    def test_span_text_mismatch_fails_closed(self) -> None:
        history = [{"id": "R1", "text": "first fact; second fact"}]
        claim = Claim(
            claim_id="c-mismatch",
            record_id="R1",
            text="other fact",
            start=0,
            end=len("first fact"),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.MASK,
            evidence_refs=(direct_evidence(),),
        )

        output = filter_history(history, [claim])

        self.assertEqual(output.filtered_history, history)
        self.assertEqual(output.operations[0].operation, GateOperation.KEEP_UNCERTAIN)
        self.assertFalse(output.operations[0].applied)
        self.assertTrue(any("does not match" in warning for warning in output.warnings))

    def test_non_overlapping_claims_in_one_record_are_edited_independently(self) -> None:
        text = "Wrong date. Useful transition. Old detour."
        history = [{"id": "R1", "text": text}]
        wrong_date = "Wrong date"
        old_detour = "Old detour"
        date_start = text.index(wrong_date)
        detour_start = text.index(old_detour)
        claims = [
            Claim(
                claim_id="c-date",
                record_id="R1",
                text=wrong_date,
                start=date_start,
                end=date_start + len(wrong_date),
                epistemic_status=EpistemicStatus.REFUTED,
                verdict=Verdict.CORRECT,
                correction="Correct date",
                evidence_refs=(direct_evidence("S-date"),),
            ),
            Claim(
                claim_id="c-detour",
                record_id="R1",
                text=old_detour,
                start=detour_start,
                end=detour_start + len(old_detour),
                epistemic_status=EpistemicStatus.SUPPORTED,
                verdict=Verdict.LOW_RELEVANCE,
                rationale="inactive rubric branch",
            ),
        ]

        output = filter_history(history, claims)

        self.assertEqual(
            output.filtered_history[0]["text"],
            f"Correct date. Useful transition. {ARCHIVE_MARKER}.",
        )
        self.assertEqual(
            [operation.operation for operation in output.operations],
            [GateOperation.REPLACE, GateOperation.ARCHIVE],
        )
        self.assertTrue(all(operation.applied for operation in output.operations))

    def test_overlapping_material_edits_all_abstain(self) -> None:
        history = [{"id": "R1", "text": "abcdef"}]
        evidence = (direct_evidence(),)
        claims = [
            Claim(
                claim_id="c-left",
                record_id="R1",
                text="bcd",
                start=1,
                end=4,
                epistemic_status=EpistemicStatus.REFUTED,
                verdict=Verdict.MASK,
                evidence_refs=evidence,
            ),
            Claim(
                claim_id="c-right",
                record_id="R1",
                text="de",
                start=3,
                end=5,
                epistemic_status=EpistemicStatus.REFUTED,
                verdict=Verdict.MASK,
                evidence_refs=evidence,
            ),
        ]

        output = filter_history(history, claims)

        self.assertEqual(output.filtered_history, history)
        self.assertEqual(
            [operation.operation for operation in output.operations],
            [GateOperation.KEEP_UNCERTAIN, GateOperation.KEEP_UNCERTAIN],
        )
        self.assertTrue(all(not operation.applied for operation in output.operations))
        self.assertEqual(output.correction_block, "")
        self.assertEqual(
            sum("overlapping material edits" in warning for warning in output.warnings),
            2,
        )

    def test_filter_does_not_mutate_history_claims_or_evidence(self) -> None:
        history = [{"id": "R1", "text": "Bad premise. Keep this."}]
        bad_claim = "Bad premise"
        evidence = direct_evidence("S-original")
        claim = Claim(
            claim_id="c1",
            record_id="R1",
            text=bad_claim,
            start=0,
            end=len(bad_claim),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.MASK,
            evidence_refs=(evidence,),
        )
        history_before = deepcopy(history)
        claim_state_before = (
            claim.text,
            claim.source_span,
            claim.verdict,
            claim.evidence_refs,
        )

        output = filter_history(history, [claim])

        self.assertTrue(output.changed)
        self.assertEqual(history, history_before)
        self.assertEqual(
            (
                claim.text,
                claim.source_span,
                claim.verdict,
                claim.evidence_refs,
            ),
            claim_state_before,
        )

    def test_core_operation_is_consumed_directly_by_seed_adapter(self) -> None:
        raw = "<think>Email says November 1. Search is done.</think>tap"
        history = [{"step_id": "s6", "raw_response": raw}]
        records = extract_seed_records(history)
        bad_claim = "Email says November 1."
        start = raw.index(bad_claim)
        claim = Claim(
            claim_id="c-date",
            record_id="s6",
            text=bad_claim,
            start=start,
            end=start + len(bad_claim),
            epistemic_status=EpistemicStatus.REFUTED,
            verdict=Verdict.CORRECT,
            correction="Email says November 15.",
            evidence_refs=(direct_evidence("S3"),),
        )
        core_output = filter_history(records, [claim])

        seed_output = adapt_seed_history(history, operations=core_output.operations)

        self.assertEqual(seed_output.operation_results[0].operation, "REPLACE")
        self.assertTrue(seed_output.operation_results[0].applied)
        self.assertNotIn(bad_claim, seed_output.filtered_history_responses[0])
        self.assertIn("Email says November 15", seed_output.correction_user_block["text"])
        self.assertIn("evidence: S3", seed_output.correction_user_block["text"])


if __name__ == "__main__":
    unittest.main()
