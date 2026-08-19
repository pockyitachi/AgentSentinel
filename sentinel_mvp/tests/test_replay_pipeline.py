from __future__ import annotations

import json
from pathlib import Path
import unittest

from sentinel.replay import ReplayValidationError, run_replay_bundle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReplayPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = json.loads(
            (PROJECT_ROOT / "fixtures" / "seed_baseline_replay_v1.json").read_text()
        )
        cls.decisions = json.loads(
            (PROJECT_ROOT / "fixtures" / "curated_gate_decisions_v1.json").read_text()
        )
        cls.bundle = run_replay_bundle(cls.fixtures, cls.decisions)

    def test_all_five_curated_replays_are_applied(self) -> None:
        self.assertEqual(self.bundle["summary"]["fixture_count"], 5)
        self.assertEqual(self.bundle["summary"]["operations_applied"], 5)
        self.assertTrue(self.bundle["summary"]["caller_histories_unchanged"])
        self.assertTrue(
            self.bundle["summary"]["refuted_claims_absent_from_active_history"]
        )

    def test_output_is_the_actual_next_prompt_history_view(self) -> None:
        for result in self.bundle["results"]:
            output = result["sentinel_output"]
            wrong = result["audit"]["removed_refuted_claim"]
            self.assertEqual(output["effective_operation"], "REPLACE")
            self.assertTrue(output["applied"])
            self.assertNotIn(wrong, result["audit"]["source_record_after"])
            self.assertTrue(
                result["audit"]["refuted_claim_absent_from_all_active_history"]
            )
            self.assertTrue(
                all(
                    wrong not in record
                    for record in output["filtered_history_for_next_prompt"]
                )
            )
            self.assertEqual(
                len(output["filtered_history_for_next_prompt"]),
                result["sentinel_input"]["history_record_count"],
            )
            self.assertEqual(len(output["retained_observation_refs"]), 3)
            self.assertIn(
                "<sentinel_history_gate>", output["correction_block"]["text"]
            )
            self.assertEqual(
                output["actor_messages_for_next_model_call"][-1]["role"], "user"
            )
            self.assertEqual(
                output["actor_messages_for_next_model_call"][-1]["content"][-1],
                output["correction_block"],
            )

    def test_bundle_rejects_non_one_to_one_fixture_sets(self) -> None:
        decisions = json.loads(json.dumps(self.decisions))
        decisions["decisions"].pop()
        with self.assertRaises(ReplayValidationError):
            run_replay_bundle(self.fixtures, decisions)


if __name__ == "__main__":
    unittest.main()
