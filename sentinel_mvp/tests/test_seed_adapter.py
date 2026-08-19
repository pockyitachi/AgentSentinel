from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import unittest

from sentinel.seed_adapter import adapt_seed_history, extract_seed_records


def image_ref(name: str) -> dict:
    return {"image_url": f"data:image/png;base64,{name}"}


def direct(ref: str) -> dict:
    return {"ref": ref, "direct": True}


class SeedAdapterTests(unittest.TestCase):
    def test_extract_uses_raw_response_as_the_span_coordinate_space(self) -> None:
        raw = "<think>reasoning claim</think>visible action"
        records = extract_seed_records([{"step_id": "s7", "raw_response": raw}])

        self.assertEqual(records[0]["id"], "s7")
        self.assertEqual(records[0]["text"], raw)
        self.assertEqual(records[0]["source_index"], 0)

    def test_preserves_reasoning_content_step_ids_and_seed_image_window(self) -> None:
        history = [
            {"step_id": f"s{i}", "raw_response": f"<think>why {i}</think>act {i}"}
            for i in range(1, 5)
        ]
        historical = [image_ref(f"old-{i}") for i in range(1, 5)]

        output = adapt_seed_history(
            history,
            historical,
            image_ref("current"),
            history_n=3,
        )

        self.assertEqual(
            [step.step_id for step in output.filtered_assistant_history],
            ["s1", "s2", "s3", "s4"],
        )
        self.assertEqual(output.filtered_assistant_history[2].reasoning_content, "why 3")
        self.assertEqual(output.filtered_assistant_history[2].content, "act 3")

        assistant_messages = [
            message for message in output.actor_messages if message["role"] == "assistant"
        ]
        image_messages = [
            message
            for message in output.actor_messages
            if message["role"] == "user"
            and message["content"][0]["type"] == "image_url"
        ]
        self.assertEqual(len(assistant_messages), 4)  # all text history survives
        self.assertEqual(len(image_messages), 3)  # only latest three images survive
        self.assertEqual(
            [m["content"][0]["image_url"]["url"].rsplit(",", 1)[-1] for m in image_messages],
            ["old-3", "old-4", "current"],
        )

    def test_direct_drop_removes_only_claim_and_does_not_mutate_inputs(self) -> None:
        raw = "<think>The date is Nov 1. Keep checking.</think><tool_call>tap</tool_call>"
        bad = "The date is Nov 1."
        history = [{"step_id": "s6", "raw_response": raw, "meta": {"owned": True}}]
        operations = [
            {
                "record_id": "s6",
                "operation": "DROP",
                "start": raw.index(bad),
                "end": raw.index(bad) + len(bad),
                "original_text": bad,
                "evidence": [direct("S3")],
            }
        ]
        history_before = deepcopy(history)
        operations_before = deepcopy(operations)

        output = adapt_seed_history(history, operations=operations)

        step = output.filtered_assistant_history[0]
        self.assertNotIn(bad, step.raw_response)
        self.assertIn("Keep checking.", step.reasoning_content)
        self.assertEqual(step.content, "<tool_call>tap</tool_call>")
        self.assertEqual(output.operation_results[0].operation, "DROP")
        self.assertTrue(output.operation_results[0].applied)
        self.assertEqual(history, history_before)
        self.assertEqual(operations, operations_before)

    def test_drop_without_direct_evidence_fails_closed_and_is_marked_unverified(self) -> None:
        raw = "<think>The filter succeeded.</think>tap next"
        claim = "The filter succeeded."
        output = adapt_seed_history(
            [{"step_id": "s2", "raw_response": raw}],
            operations=[
                {
                    "record_id": "s2",
                    "operation": "DROP",
                    "start": raw.index(claim),
                    "end": raw.index(claim) + len(claim),
                    "original_text": claim,
                    "evidence": [{"ref": "similarity-only", "direct": False}],
                }
            ],
        )

        self.assertEqual(output.filtered_history_responses, (raw,))
        self.assertEqual(output.operation_results[0].operation, "KEEP_UNCERTAIN")
        self.assertFalse(output.operation_results[0].applied)
        self.assertIn("UNVERIFIED", output.correction_user_block["text"])

    def test_replace_removes_old_claim_and_attaches_grounded_user_correction(self) -> None:
        raw = "<think>Email says November 1 at 10 AM.</think><tool_call>tap Nov 1</tool_call>"
        claim = "Email says November 1 at 10 AM."
        current = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,current"},
                }
            ],
        }
        current_before = deepcopy(current)

        output = adapt_seed_history(
            [{"step_id": "s6", "raw_response": raw}],
            [image_ref("before")],
            current,
            [
                {
                    "record_id": "s6",
                    "claim_id": "c-date",
                    "operation": "REPLACE",
                    "start": raw.index(claim),
                    "end": raw.index(claim) + len(claim),
                    "original_text": claim,
                    "replacement_text": "The email says November 15 at 3 PM.",
                    "evidence": [direct("S3")],
                }
            ],
        )

        self.assertNotIn(claim, output.filtered_history_responses[0])
        self.assertIn("November 15 at 3 PM", output.correction_user_block["text"])
        self.assertIn("evidence: S3", output.correction_user_block["text"])
        latest = output.actor_messages[-1]
        self.assertEqual(latest["role"], "user")
        self.assertEqual(latest["content"][0]["type"], "image_url")
        self.assertEqual(latest["content"][1], output.correction_user_block)
        self.assertEqual(current, current_before)

    def test_archive_whole_record_uses_canonical_alias_mapping(self) -> None:
        output = adapt_seed_history(
            [
                {"step_id": "s1", "raw_response": "<think>search</think>tap"},
                {"step_id": "s2", "raw_response": "<think>laptop detour</think>tap"},
            ],
            operations=[
                {
                    "target_step_id": "s2",
                    "verdict": "LOW_RELEVANCE",
                    "rationale": "inactive rubric branch",
                }
            ],
        )

        self.assertEqual(
            [step.step_id for step in output.filtered_assistant_history], ["s1"]
        )
        self.assertEqual(output.operation_results[0].operation, "ARCHIVE")
        self.assertTrue(output.operation_results[0].applied)
        self.assertIsNone(output.correction_user_block)

    def test_mask_and_correct_aliases_emit_only_canonical_operations(self) -> None:
        raw = "<think>wrong date</think>tap"
        claim = "wrong date"
        base = {
            "record_id": "s1",
            "start": raw.index(claim),
            "end": raw.index(claim) + len(claim),
            "original_text": claim,
            "evidence": [direct("S1")],
        }
        masked = adapt_seed_history(
            [{"step_id": "s1", "raw_response": raw}],
            operations=[{**base, "operation": "MASK"}],
        )
        corrected = adapt_seed_history(
            [{"step_id": "s1", "raw_response": raw}],
            operations=[
                {**base, "operation": "CORRECT", "correction": "right date"}
            ],
        )

        self.assertEqual(masked.operation_results[0].operation, "DROP")
        self.assertEqual(corrected.operation_results[0].operation, "REPLACE")

    def test_mismatched_ambiguous_span_is_not_applied(self) -> None:
        raw = "<think>same claim and same claim</think>tap"
        output = adapt_seed_history(
            [{"step_id": "s1", "raw_response": raw}],
            operations=[
                {
                    "record_id": "s1",
                    "operation": "DROP",
                    "start": 999,
                    "end": 1004,
                    "original_text": "same claim",
                    "evidence": [direct("S1")],
                }
            ],
        )

        self.assertEqual(output.filtered_history_responses, (raw,))
        self.assertEqual(output.operation_results[0].operation, "KEEP_UNCERTAIN")
        self.assertFalse(output.operation_results[0].applied)

    def test_accepts_dataclass_style_core_operation(self) -> None:
        @dataclass(frozen=True)
        class Operation:
            record_id: str
            verdict: str
            start: int
            end: int
            original_text: str
            replacement_text: str | None
            rationale: str
            evidence: tuple[dict, ...]
            claim_id: str = "c1"

        raw = "<think>false premise</think>tap"
        claim = "false premise"
        operation = Operation(
            record_id="s1",
            verdict="DROP",
            start=raw.index(claim),
            end=raw.index(claim) + len(claim),
            original_text=claim,
            replacement_text=None,
            rationale="refuted",
            evidence=(direct("S1"),),
        )

        output = adapt_seed_history(
            [{"step_id": "s1", "raw_response": raw}], operations=[operation]
        )
        self.assertNotIn(claim, output.filtered_history_responses[0])


if __name__ == "__main__":
    unittest.main()
