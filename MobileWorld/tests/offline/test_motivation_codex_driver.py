from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mobile_world.offline.motivation_prompt import build_review_prompt, response_schema
from mobile_world.offline.motivation_review import (
    EXPECTED_TASK_COUNT,
    REVIEW_SCHEMA_VERSION,
    canonical_json_bytes,
    canonical_sha256,
)

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_motivation_codex_review.py"
_SPEC = importlib.util.spec_from_file_location("motivation_codex_driver", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
driver = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = driver
_SPEC.loader.exec_module(driver)

DATASET_SHA = "a" * 64
SELECTION_SHA = "b" * 64
RECONSTRUCTION_SHA = "c" * 64
SPAN_SHA = "d" * 64


def _card(index: int, *, blob_sha256: str | None = None) -> dict[str, Any]:
    task_name = f"Task{index:03d}"
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "task_card",
        "evaluation_run_id": "evaluation-1",
        "dataset_sha256": DATASET_SHA,
        "selection_sha256": SELECTION_SHA,
        "task": {
            "catalog_index": index,
            "task_name": task_name,
            "task_run_id": f"task-run-{index:03d}",
            "raw_run_id": "raw-run-001",
            "source_id": "source-001",
            "source_relative_run_path": "source-001/audit/raw/runs/raw-run-001",
            "task_stream_relative_path": f"tasks/task-run-{index:03d}/events.jsonl",
        },
        "outcome_blinded": True,
        "instruction": f"Complete fixture task {index}",
        "coverage": {
            "integrity_valid": True,
            "capture_complete": True,
            "decision_count": 2,
            "reconstructed_decision_count": 2,
            "history_bearing_decision_count": 1,
            "unique_history_claim_count": 1,
            "actual_exposure_count": 1,
            "scanner_candidate_count": 1,
            "dropped_candidate_count": 0,
            "full_reconstruction_sha256": RECONSTRUCTION_SHA,
        },
        "trajectory_outline": [
            {
                "step": 1,
                "prediction_excerpt": "source",
                "parsed_action": "click",
                "ui_delta": "changed",
                "history_claim_ids": ["candidate-1"],
            },
            {
                "step": 2,
                "prediction_excerpt": "target",
                "parsed_action": "finished",
                "ui_delta": None,
                "history_claim_ids": [],
            },
        ],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "retrieval_reasons": ["fixture"],
                "claim": {
                    "text": "The action succeeded",
                    "claim_type": "SUCCESS_CLAIM",
                    "source_steps": [1],
                    "representation_type": "raw_replay",
                    "provenance_confidence": "EXACT",
                },
                "exposure": {
                    "target_step": 2,
                    "request_path": "messages[3].content[0]",
                    "was_actually_in_request": True,
                    "span_sha256": SPAN_SHA,
                },
                "evidence_refs": [
                    {
                        "ref_id": "ref-source",
                        "role": "source_prediction",
                        "event_id": f"event-source-{index}",
                        "step": 1,
                        "field_path": "payload.prediction_raw",
                        "blob_sha256": blob_sha256,
                        "excerpt": "The action succeeded",
                    },
                    {
                        "ref_id": "ref-target",
                        "role": "target_request",
                        "event_id": f"event-target-{index}",
                        "step": 2,
                        "field_path": "payload.request_view.messages[3].content[0]",
                        "blob_sha256": None,
                        "excerpt": "The action succeeded",
                    },
                ],
            }
        ],
    }


def _cards(*, first_blob_sha256: str | None = None) -> list[dict[str, Any]]:
    return [
        _card(index, blob_sha256=first_blob_sha256 if index == 1 else None)
        for index in range(1, EXPECTED_TASK_COUNT + 1)
    ]


def _source_base(tmp_path: Path) -> Path:
    source_base = tmp_path / "sources"
    (source_base / "source-001" / "audit" / "raw" / "runs" / "raw-run-001").mkdir(parents=True)
    return source_base


def _write_cards(path: Path, cards: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(canonical_json_bytes(card) for card in cards))


def _write_outcomes(path: Path, cards: list[dict[str, Any]]) -> None:
    records = []
    for card in cards:
        index = card["task"]["catalog_index"]
        success = index % 2 == 1
        records.append(
            {
                "task_name": card["task"]["task_name"],
                "catalog_index": index,
                "app": f"fixture-app-{index % 3}",
                "outcome": "SUCCESS" if success else "FAILURE",
                "score": 1.0 if success else 0.0,
            }
        )
    path.write_bytes(b"".join(canonical_json_bytes(record) for record in records))


def _chain(card: dict[str, Any], screen: str) -> dict[str, Any]:
    if screen == "POSITIVE":
        validity = "REFUTED"
        invalid_subtypes = ["FALSE_CLAIM"]
        uptake = "EXPLICIT_USE"
        effects = ["WRONG_ACTION"]
        confound = "NONE"
    elif screen == "UNCERTAIN":
        validity = "UNVERIFIABLE"
        invalid_subtypes = []
        uptake = "UNKNOWN"
        effects = ["UNKNOWN_EFFECT"]
        confound = "UNKNOWN"
    else:
        validity = "SUPPORTED"
        invalid_subtypes = []
        uptake = "NO_OBSERVED_UPTAKE"
        effects = ["NO_VISIBLE_HARM"]
        confound = "NONE"
    candidate = card["candidates"][0]
    return {
        "candidate_id": candidate["candidate_id"],
        "history_validity": validity,
        "invalid_subtypes": invalid_subtypes,
        "uptake_evidence": uptake,
        "state_confound": confound,
        "downstream_effects": effects,
        "evidence_ref_ids": sorted(ref["ref_id"] for ref in candidate["evidence_refs"]),
        "confidence": "HIGH",
        "rationale": "The compact cited evidence supports this formal label.",
    }


def _review_from_case(case: dict[str, Any], screen: str) -> dict[str, Any]:
    identity = dict(case["expected_review_identity"])
    card = case["task_card"]
    return {
        **identity,
        "coverage_verdict": "SUFFICIENT",
        "chains": [_chain(card, screen)],
        "task_screen_class": screen,
        "summary": f"Formal {screen.lower()} fixture review.",
    }


class FakeCodex:
    def __init__(
        self,
        *,
        primary: dict[str, str] | None = None,
        secondary: dict[str, str] | None = None,
        fail_first: bool = False,
        invalid_effects_first: bool = False,
        wrong_screen_first: bool = False,
    ) -> None:
        self.primary = primary or {}
        self.secondary = secondary or {}
        self.fail_first = fail_first
        self.invalid_effects_first = invalid_effects_first
        self.wrong_screen_first = wrong_screen_first
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        prompt = kwargs["input"].decode()
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        output_path = Path(argv[argv.index("-o") + 1])
        schema = json.loads(schema_path.read_bytes())
        phase = schema["properties"]["phase"]["enum"][0]
        batch_id = schema["properties"]["batch_id"]["enum"][0]
        call = {"argv": argv, "kwargs": kwargs, "phase": phase, "prompt": prompt}
        self.calls.append(call)
        if self.fail_first and len(self.calls) == 1:
            return subprocess.CompletedProcess(argv, 9, b"first stdout", b"first stderr")

        if phase == "ADJUDICATION":
            marker = "FORMAL_BLIND_ADJUDICATION_CASES_JSON="
            cases = json.loads(prompt.split(marker, 1)[1].strip())
            reviews = [_review_from_case(case, "UNCERTAIN") for case in cases]
        else:
            marker = "FORMAL_BLIND_REVIEW_CASES_JSON="
            cases = json.loads(prompt.split(marker, 1)[1].strip())
            verdicts = self.primary if phase == "PASS1" else self.secondary
            reviews = [
                _review_from_case(
                    case,
                    verdicts.get(case["task_card"]["task"]["task_name"], "NEGATIVE"),
                )
                for case in cases
            ]
        if self.invalid_effects_first and len(self.calls) == 1:
            reviews[0]["chains"][0]["downstream_effects"] = []
        if self.wrong_screen_first and len(self.calls) == 1:
            reviews[0]["task_screen_class"] = "POSITIVE"
        identity = cases[0]["task_card"]
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": REVIEW_SCHEMA_VERSION,
                    "record_type": "review_batch",
                    "evaluation_run_id": identity["evaluation_run_id"],
                    "dataset_sha256": identity["dataset_sha256"],
                    "selection_sha256": identity["selection_sha256"],
                    "batch_id": batch_id,
                    "phase": phase,
                    "reviews": reviews,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, b"codex event stream", b"")


def _loaded_fixture(tmp_path: Path) -> tuple[tuple[Any, ...], dict[str, dict[str, Any]]]:
    source_base = _source_base(tmp_path)
    cards_path = tmp_path / "cards.jsonl"
    _write_cards(cards_path, _cards())
    loaded = driver.load_task_cards(cards_path, source_base=source_base)
    return loaded, {card.task_name: card.payload for card in loaded}


def test_response_schema_stays_in_structured_outputs_subset() -> None:
    schema = response_schema(
        phase="PASS1",
        batch_id="pass1-0001-c001-c001",
        expected_count=1,
        identity={
            "evaluation_run_id": "evaluation-1",
            "dataset_sha256": DATASET_SHA,
            "selection_sha256": SELECTION_SHA,
        },
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
    )
    unsupported = {
        "$schema",
        "const",
        "maxItems",
        "maxLength",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "uniqueItems",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            assert not (set(value) & unsupported)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(schema)


def test_prompt_states_post_schema_contract_rules() -> None:
    prompt = build_review_prompt(
        phase="PASS1",
        batch_id="pass1-0001-c001-c001",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        cases=[],
    )
    assert "downstream_effects must never be empty" in prompt
    assert "NOT_A_FACTUAL_CLAIM is permitted only for ACTION_INTENT or PLAN" in prompt
    assert "coverage.decision_count equals" in prompt
    assert "otherwise NEGATIVE" in prompt
    assert "at most 1000 characters" in prompt


def test_load_formal_task_cards_sorts_and_rejects_unknown_fields(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    cards_path = tmp_path / "cards.jsonl"
    cards = list(reversed(_cards()))
    _write_cards(cards_path, cards)
    loaded = driver.load_task_cards(cards_path, source_base=source_base)
    assert [card.catalog_index for card in loaded] == list(range(1, EXPECTED_TASK_COUNT + 1))
    cards[0]["official_score"] = 1
    _write_cards(cards_path, cards)
    with pytest.raises(driver.ReviewDriverError, match="formal task-card validation"):
        driver.load_task_cards(cards_path, source_base=source_base)


def test_load_cards_requires_canonical_jsonl_and_exact_117(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    cards_path = tmp_path / "cards.jsonl"
    cards_path.write_text(json.dumps(_card(1)) + "\n", encoding="utf-8")
    with pytest.raises(driver.ReviewDriverError, match="non-canonical JSONL"):
        driver.load_task_cards(cards_path, source_base=source_base)

    _write_cards(cards_path, _cards()[:-1])
    with pytest.raises(driver.ReviewDriverError, match="formal task-card validation"):
        driver.load_task_cards(cards_path, source_base=source_base)


def test_candidate_images_are_targeted_hashed_and_put_in_prompt(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    image_bytes = b"\x89PNG\r\n\x1a\ncompact-fixture-image"
    digest = hashlib.sha256(image_bytes).hexdigest()
    run_root = source_base / "source-001" / "audit" / "raw" / "runs" / "raw-run-001"
    image_path = run_root / "blobs" / "sha256" / digest[:2] / digest
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image_bytes)
    cards_path = tmp_path / "cards.jsonl"
    cards = _cards(first_blob_sha256=digest)
    cards[0]["candidates"][0]["evidence_refs"][1]["blob_sha256"] = digest
    _write_cards(cards_path, cards)
    loaded = driver.load_task_cards(cards_path, source_base=source_base)
    assert loaded[0].image_paths == (image_path.resolve(),)
    assert len(loaded[0].image_attachments) == 2
    attachment = loaded[0].image_attachments[0]
    assert attachment.candidate_id == "candidate-1"
    assert attachment.ref_id == "ref-source"

    all_cards = {card.task_name: card.payload for card in loaded}
    batch = driver.fixed_batches(loaded[:1], 1, phase="PASS1")[0]
    fake = FakeCodex()
    driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        resume=False,
        runner=fake,
    )
    assert str(image_path.resolve()) not in fake.calls[0]["prompt"]
    assert fake.calls[0]["prompt"].count('"attachment_index":1') == 2
    argv = fake.calls[0]["argv"]
    assert argv.count("--image") == 1
    assert argv[argv.index("--image") + 1] == str(image_path.resolve())


def test_fixed_batches_follow_canonical_order(tmp_path: Path) -> None:
    cards, _ = _loaded_fixture(tmp_path)
    batches = driver.fixed_batches(cards[:5], 2, phase="PASS1")
    assert [[card.catalog_index for card in batch.cards] for batch in batches] == [
        [1, 2],
        [3, 4],
        [5],
    ]
    assert [batch.batch_id for batch in batches] == [
        "pass1-0001-c001-c002",
        "pass1-0002-c003-c004",
        "pass1-0003-c005-c005",
    ]


def test_retry_exhausted_singleton_does_not_discard_later_work(tmp_path: Path) -> None:
    cards, _ = _loaded_fixture(tmp_path)
    batches = driver.fixed_batches(cards[:4], 1, phase="PASS1")
    calls: list[str] = []

    def execute(batch: Any) -> Any:
        calls.append(batch.batch_id)
        if batch.cards[0].catalog_index == 1:
            raise driver.BatchRetryExhausted("fixture exhaustion")
        return driver.StageArtifact(
            phase=batch.phase,
            batch_id=batch.batch_id,
            result={"reviews": []},
            response_sha256="a" * 64,
            receipt_sha256="b" * 64,
            directory=tmp_path,
            resumed=False,
        )

    with pytest.raises(driver.ReviewDriverError, match="artifacts were preserved"):
        driver._execute_batches_resiliently(
            batches,
            output_root=tmp_path / "output",
            execute=execute,
        )
    assert calls == [batch.batch_id for batch in batches]
    summaries = list((tmp_path / "output" / "failures" / "pass1" / "stage").iterdir())
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_bytes())
    assert summary["failed_batches"] == [
        {"batch_id": batches[0].batch_id, "error": "fixture exhaustion"}
    ]
    assert summary["completed_batch_ids"] == [batch.batch_id for batch in batches[1:]]


def test_retry_exhaustion_stage_limit_bounds_systemic_failures(tmp_path: Path) -> None:
    cards, _ = _loaded_fixture(tmp_path)
    batches = driver.fixed_batches(cards[:4], 1, phase="PASS1")
    calls: list[str] = []

    def always_exhausted(batch: Any) -> Any:
        calls.append(batch.batch_id)
        raise driver.BatchRetryExhausted("systemic fixture exhaustion")

    with pytest.raises(driver.ReviewDriverError):
        driver._execute_batches_resiliently(
            batches,
            output_root=tmp_path / "output",
            execute=always_exhausted,
        )
    assert calls == [batch.batch_id for batch in batches[: driver.MAX_STAGE_RETRY_EXHAUSTIONS]]


def test_execute_batch_uses_safe_argv_and_atomic_hashed_receipt(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:2], 8, phase="PASS1")[0]
    fake = FakeCodex()
    artifact = driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=2,
        timeout_seconds=10,
        resume=False,
        runner=fake,
    )
    assert sorted(path.name for path in artifact.directory.iterdir()) == [
        "model_response.json",
        "output_schema.json",
        "receipt.json",
        "response.json",
    ]
    call = fake.calls[0]
    argv = call["argv"]
    assert argv[:5] == ["codex", "exec", "--ephemeral", "-s", "read-only"]
    assert argv[-1] == "-"
    assert "FORMAL_BLIND_REVIEW_CASES_JSON" not in " ".join(argv)
    assert call["kwargs"]["shell"] is False
    assert call["kwargs"]["check"] is False
    assert call["kwargs"]["cwd"] == Path(argv[argv.index("-C") + 1])
    disabled = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--disable"]
    assert disabled == list(driver.REVIEWER_DISABLED_FEATURES)
    assert "shell_tool" in disabled
    assert "view_image" in disabled
    assert "browser_use" in disabled
    receipt_bytes = (artifact.directory / "receipt.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    response_bytes = (artifact.directory / "response.json").read_bytes()
    assert receipt["response_sha256"] == driver._sha256(response_bytes)
    assert receipt["attempts"][0]["stdout_byte_count"] > 0
    assert "codex event stream" not in receipt_bytes.decode()


def test_finite_retry_does_not_store_stream_text(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    fake = FakeCodex(fail_first=True)
    artifact = driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=2,
        timeout_seconds=10,
        resume=False,
        runner=fake,
    )
    receipt = json.loads((artifact.directory / "receipt.json").read_bytes())
    assert [attempt["error_kind"] for attempt in receipt["attempts"]] == [
        "nonzero_exit",
        None,
    ]
    assert "first stderr" not in json.dumps(receipt)


def test_contract_retry_receives_bounded_structured_feedback(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    fake = FakeCodex(invalid_effects_first=True)
    artifact = driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=2,
        timeout_seconds=10,
        resume=False,
        runner=fake,
    )
    assert len(fake.calls) == 2
    assert "VALIDATION_FEEDBACK=" not in fake.calls[0]["prompt"]
    assert "VALIDATION_FEEDBACK=" in fake.calls[1]["prompt"]
    assert "downstream_effect_empty" in fake.calls[1]["prompt"]
    receipt = json.loads((artifact.directory / "receipt.json").read_bytes())
    assert [attempt["error_kind"] for attempt in receipt["attempts"]] == [
        "invalid_response",
        None,
    ]
    assert receipt["attempts"][0]["prompt_sha256"] != receipt["attempts"][1]["prompt_sha256"]
    assert receipt["accepted_prompt_sha256"] == receipt["attempts"][1]["prompt_sha256"]
    feedback = receipt["attempts"][0]["error_detail"]
    assert receipt["attempts"][1]["validation_feedback_sha256"] == driver._sha256(feedback.encode())
    rejected_relative = receipt["attempts"][0]["rejected_model_response_path"]
    rejected_path = tmp_path / "output" / rejected_relative
    assert rejected_path.is_file()
    assert (
        driver._sha256(rejected_path.read_bytes())
        == receipt["attempts"][0]["model_response_sha256"]
    )


def test_screen_class_is_mechanically_normalized_with_model_response_preserved(
    tmp_path: Path,
) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    artifact = driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        resume=False,
        runner=FakeCodex(wrong_screen_first=True),
    )
    model_response = json.loads((artifact.directory / "model_response.json").read_bytes())
    normalized = json.loads((artifact.directory / "response.json").read_bytes())
    receipt = json.loads((artifact.directory / "receipt.json").read_bytes())
    assert model_response["reviews"][0]["task_screen_class"] == "POSITIVE"
    assert normalized["reviews"][0]["task_screen_class"] == "NEGATIVE"
    assert receipt["attempts"][0]["derived_field_corrections"] == ["reviews[0].task_screen_class"]


def test_resume_verifies_artifact_and_skips_subprocess(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    output = tmp_path / "output"
    arguments = {
        "batch": batch,
        "all_cards_by_task": all_cards,
        "output_root": output,
        "model": "gpt-5.6-terra",
        "reviewer_id": driver.DEFAULT_PRIMARY_REVIEWER,
        "codex_bin": "codex",
        "max_attempts": 1,
        "timeout_seconds": 10,
    }
    driver.execute_stage_batch(**arguments, resume=False, runner=FakeCodex())

    def forbidden_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("resume must not invoke Codex")

    artifact = driver.execute_stage_batch(**arguments, resume=True, runner=forbidden_runner)
    assert artifact.resumed is True


def test_resume_reconstructs_and_verifies_frozen_v2_prompt_receipt(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    output = tmp_path / "output"
    arguments = {
        "batch": batch,
        "all_cards_by_task": all_cards,
        "output_root": output,
        "model": "gpt-5.6-terra",
        "reviewer_id": driver.DEFAULT_PRIMARY_REVIEWER,
        "codex_bin": "codex",
        "max_attempts": 1,
        "timeout_seconds": 10,
        "prompt_version": driver.PREVIOUS_PROMPT_VERSION,
    }
    artifact = driver.execute_stage_batch(**arguments, resume=False, runner=FakeCodex())
    receipt_path = artifact.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["prompt_version"] == driver.PREVIOUS_PROMPT_VERSION
    base_prompt_sha256 = receipt["base_prompt_sha256"]
    receipt["base_prompt_sha256"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="base_prompt_sha256"):
        driver.execute_stage_batch(**arguments, resume=True, runner=FakeCodex())

    receipt["base_prompt_sha256"] = base_prompt_sha256
    accepted_prompt_sha256 = receipt["accepted_prompt_sha256"]
    receipt["accepted_prompt_sha256"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="accepted prompt"):
        driver.execute_stage_batch(**arguments, resume=True, runner=FakeCodex())

    receipt["accepted_prompt_sha256"] = accepted_prompt_sha256
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    def forbidden_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("verified v2 resume must not invoke Codex")

    resumed = driver.execute_stage_batch(**arguments, resume=True, runner=forbidden_runner)

    assert resumed.resumed is True


def test_resume_verifies_retry_feedback_and_prompt_chain(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    output = tmp_path / "output"
    arguments = {
        "batch": batch,
        "all_cards_by_task": all_cards,
        "output_root": output,
        "model": "gpt-5.6-terra",
        "reviewer_id": driver.DEFAULT_PRIMARY_REVIEWER,
        "codex_bin": "codex",
        "max_attempts": 2,
        "timeout_seconds": 10,
        "prompt_version": driver.PREVIOUS_PROMPT_VERSION,
    }
    artifact = driver.execute_stage_batch(
        **arguments,
        resume=False,
        runner=FakeCodex(invalid_effects_first=True),
    )
    receipt_path = artifact.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["attempt_count"] == 2
    receipt["attempts"][1]["validation_feedback_sha256"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="feedback chain"):
        driver.execute_stage_batch(**arguments, resume=True, runner=FakeCodex())


def test_resume_binds_model_response_to_normalized_response(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    output = tmp_path / "output"
    arguments = {
        "batch": batch,
        "all_cards_by_task": all_cards,
        "output_root": output,
        "model": "gpt-5.6-terra",
        "reviewer_id": driver.DEFAULT_PRIMARY_REVIEWER,
        "codex_bin": "codex",
        "max_attempts": 1,
        "timeout_seconds": 10,
    }
    artifact = driver.execute_stage_batch(
        **arguments,
        resume=False,
        runner=FakeCodex(wrong_screen_first=True),
    )
    model_response_path = artifact.directory / "model_response.json"
    receipt_path = artifact.directory / "receipt.json"
    model_response = json.loads(model_response_path.read_bytes())
    model_response["reviews"][0]["summary"] = "Tampered but otherwise schema-valid summary."
    model_response_bytes = canonical_json_bytes(model_response)
    model_response_path.write_bytes(model_response_bytes)
    model_response_sha256 = driver._sha256(model_response_bytes)
    receipt = json.loads(receipt_path.read_bytes())
    receipt["model_response_sha256"] = model_response_sha256
    receipt["attempts"][-1]["model_response_sha256"] = model_response_sha256
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="normalized response"):
        driver.execute_stage_batch(**arguments, resume=True, runner=FakeCodex())


def test_hash_anchored_v2_seed_rebuilds_prompt_and_output_schema(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    seed_batch = driver.fixed_batches(cards[:2], 2, phase="PASS1")[0]
    seed = driver.execute_stage_batch(
        batch=seed_batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "v2-seed",
        model=driver.DEFAULT_PRIMARY_MODEL,
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        resume=False,
        prompt_version=driver.PREVIOUS_PROMPT_VERSION,
        runner=FakeCodex(),
    )
    receipt_path = seed.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    base_prompt_sha256 = receipt["base_prompt_sha256"]
    receipt["base_prompt_sha256"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="base prompt hash"):
        driver.load_seed_pass1_artifacts(
            [seed.directory],
            expected_receipt_sha256s=[driver._file_sha256(receipt_path)],
            cards=cards,
            all_cards_by_task=all_cards,
            model=driver.DEFAULT_PRIMARY_MODEL,
            reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        )

    receipt["base_prompt_sha256"] = base_prompt_sha256
    schema_path = seed.directory / "output_schema.json"
    schema = json.loads(schema_path.read_bytes())
    schema["additionalProperties"] = True
    schema_bytes = canonical_json_bytes(schema)
    schema_path.write_bytes(schema_bytes)
    receipt["schema_sha256"] = driver._sha256(schema_bytes)
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(driver.ReviewDriverError, match="output schema"):
        driver.load_seed_pass1_artifacts(
            [seed.directory],
            expected_receipt_sha256s=[driver._file_sha256(receipt_path)],
            cards=cards,
            all_cards_by_task=all_cards,
            model=driver.DEFAULT_PRIMARY_MODEL,
            reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        )


def test_existing_v2_run_continues_and_resumes_without_mixing_v3(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    cards = _cards()
    cards_path = tmp_path / "cards.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_cards(cards_path, cards)
    _write_outcomes(outcomes_path, cards)
    output = tmp_path / "v2-review"
    output.mkdir()
    manifest = driver._manifest(
        cards_path=cards_path,
        cards_file_sha256=driver._file_sha256(cards_path),
        source_base=source_base,
        batch_size=EXPECTED_TASK_COUNT,
        primary_model=driver.DEFAULT_PRIMARY_MODEL,
        secondary_model=driver.DEFAULT_SECONDARY_MODEL,
        adjudicator_model=driver.DEFAULT_ADJUDICATOR_MODEL,
        primary_reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        secondary_reviewer_id=driver.DEFAULT_SECONDARY_REVIEWER,
        adjudicator_reviewer_id=driver.DEFAULT_ADJUDICATOR_REVIEWER,
        negative_rate=driver.DEFAULT_NEGATIVE_AUDIT_RATE,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        pass1_seed_batches=(),
        prompt_version=driver.PREVIOUS_PROMPT_VERSION,
    )
    (output / "run_manifest.json").write_bytes(canonical_json_bytes(manifest))

    first_runner = FakeCodex()
    first = driver.run_review(
        cards_path=cards_path,
        outcomes_path=outcomes_path,
        source_base=source_base,
        output_root=output,
        batch_size=EXPECTED_TASK_COUNT,
        max_attempts=1,
        timeout_seconds=10,
        resume=True,
        runner=first_runner,
    )

    assert first["dry_run"] is False
    assert len(first_runner.calls) == 2
    receipts = [
        json.loads(path.read_bytes()) for path in sorted((output / "batches").rglob("receipt.json"))
    ]
    assert receipts
    assert {receipt["prompt_version"] for receipt in receipts} == {driver.PREVIOUS_PROMPT_VERSION}

    def forbidden_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("complete v2 resume must not invoke Codex")

    resumed = driver.run_review(
        cards_path=cards_path,
        outcomes_path=outcomes_path,
        source_base=source_base,
        output_root=output,
        batch_size=EXPECTED_TASK_COUNT,
        max_attempts=1,
        timeout_seconds=10,
        resume=True,
        runner=forbidden_runner,
    )

    assert resumed == first


def test_dry_run_never_reads_outcomes_writes_or_invokes(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    cards_path = tmp_path / "cards.jsonl"
    _write_cards(cards_path, _cards())
    output = tmp_path / "must-not-exist"

    def forbidden_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("dry-run must not invoke Codex")

    summary = driver.run_review(
        cards_path=cards_path,
        outcomes_path=tmp_path / "does-not-exist.jsonl",
        source_base=source_base,
        output_root=output,
        batch_size=16,
        dry_run=True,
        runner=forbidden_runner,
    )
    assert summary["primary_batch_count"] == math.ceil(EXPECTED_TASK_COUNT / 16)
    assert summary["outcomes_opened"] is False
    assert summary["files_written"] == 0
    assert not output.exists()


@pytest.mark.parametrize("rate", [0.0, 1.0, True, float("nan")])
def test_invalid_negative_rate_fails_before_loading_or_invoking(tmp_path: Path, rate: Any) -> None:
    def forbidden_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("invalid rate must fail before Codex")

    with pytest.raises(driver.ReviewDriverError, match="between 0.10 and 0.20"):
        driver.run_review(
            cards_path=tmp_path / "missing-cards.jsonl",
            outcomes_path=tmp_path / "missing-outcomes.jsonl",
            source_base=tmp_path,
            output_root=tmp_path / "must-not-exist",
            negative_rate=rate,
            dry_run=True,
            runner=forbidden_runner,
        )


def test_new_run_imports_hash_bound_legacy_pass1_and_starts_at_task_five(
    tmp_path: Path,
) -> None:
    source_base = _source_base(tmp_path)
    cards = _cards()
    cards_path = tmp_path / "cards.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_cards(cards_path, cards)
    _write_outcomes(outcomes_path, cards)
    loaded = driver.load_task_cards(cards_path, source_base=source_base)
    all_cards = {card.task_name: card.payload for card in loaded}
    seed_batch = driver.fixed_batches(loaded[:4], 4, phase="PASS1")[0]
    seed = driver.execute_stage_batch(
        batch=seed_batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "legacy-review",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        resume=False,
        runner=FakeCodex(),
    )
    legacy_receipt_path = seed.directory / "receipt.json"
    legacy_receipt = json.loads(legacy_receipt_path.read_bytes())
    legacy_receipt["driver_schema_version"] = driver.LEGACY_DRIVER_SCHEMA_VERSION
    legacy_receipt["prompt_version"] = driver.LEGACY_PROMPT_VERSION
    legacy_cases = [
        driver._legacy_review_case_v1(
            card,
            phase="PASS1",
            reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        )
        for card in loaded[:4]
    ]
    legacy_receipt["input_sha256"] = driver._sha256(canonical_json_bytes(legacy_cases))
    legacy_receipt_path.write_bytes(canonical_json_bytes(legacy_receipt))

    with pytest.raises(driver.ReviewDriverError, match="receipt anchor mismatch"):
        driver.run_review(
            cards_path=cards_path,
            outcomes_path=outcomes_path,
            source_base=source_base,
            output_root=tmp_path / "must-not-exist",
            pass1_seed_batch_dirs=[seed.directory],
            pass1_seed_receipt_sha256s=["0" * 64],
            dry_run=True,
            runner=FakeCodex(),
        )

    safe_receipt_bytes = legacy_receipt_path.read_bytes()
    unsafe_receipt = json.loads(safe_receipt_bytes)
    unsafe_receipt["batch_id"] = "../../escape"
    legacy_receipt_path.write_bytes(canonical_json_bytes(unsafe_receipt))
    with pytest.raises(driver.ReviewDriverError, match="batch_id is invalid"):
        driver.run_review(
            cards_path=cards_path,
            outcomes_path=outcomes_path,
            source_base=source_base,
            output_root=tmp_path / "must-not-exist",
            pass1_seed_batch_dirs=[seed.directory],
            pass1_seed_receipt_sha256s=[driver._file_sha256(legacy_receipt_path)],
            dry_run=True,
            runner=FakeCodex(),
        )
    legacy_receipt_path.write_bytes(safe_receipt_bytes)

    fake = FakeCodex()
    output = tmp_path / "review-v3"
    summary = driver.run_review(
        cards_path=cards_path,
        outcomes_path=outcomes_path,
        source_base=source_base,
        output_root=output,
        batch_size=1,
        pass1_seed_batch_dirs=[seed.directory],
        pass1_seed_receipt_sha256s=[driver._file_sha256(legacy_receipt_path)],
        max_attempts=1,
        timeout_seconds=10,
        runner=fake,
    )
    assert summary["pass1_seed_batch_count"] == 1
    assert summary["pass1_seed_task_count"] == 4
    assert summary["primary_batch_count"] == EXPECTED_TASK_COUNT - 4
    assert fake.calls[0]["phase"] == "PASS1"
    assert '"task_name":"Task005"' in fake.calls[0]["prompt"]
    assert '"task_name":"Task004"' not in fake.calls[0]["prompt"]
    imported = output / "imports" / "pass1" / seed_batch.batch_id
    assert sorted(path.name for path in imported.iterdir()) == [
        "import_receipt.json",
        "legacy_output_schema.json",
        "legacy_receipt.json",
        "legacy_response.json",
    ]
    frozen_reviews = (output / "frozen" / "pass1_reviews.jsonl").read_bytes().splitlines()
    assert len(frozen_reviews) == EXPECTED_TASK_COUNT


def test_full_flow_uses_formal_selection_and_blind_second_pass(tmp_path: Path) -> None:
    source_base = _source_base(tmp_path)
    cards = _cards()
    cards_path = tmp_path / "cards.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_cards(cards_path, cards)
    _write_outcomes(outcomes_path, cards)
    fake = FakeCodex(
        primary={"Task001": "POSITIVE", "Task002": "UNCERTAIN"},
        secondary={"Task001": "POSITIVE", "Task002": "NEGATIVE"},
    )
    output = tmp_path / "review"
    summary = driver.run_review(
        cards_path=cards_path,
        outcomes_path=outcomes_path,
        source_base=source_base,
        output_root=output,
        batch_size=EXPECTED_TASK_COUNT,
        primary_model="gpt-5.6-terra",
        secondary_model="gpt-5.6-sol",
        adjudicator_model="gpt-5.6-sol",
        max_attempts=2,
        timeout_seconds=10,
        resume=False,
        dry_run=False,
        runner=fake,
    )
    assert summary["primary_batch_count"] == 1
    assert summary["pass2_task_count"] == 2
    assert summary["negative_audit_task_count"] == math.ceil(115 * 0.15)
    assert summary["secondary_task_count"] == 20
    assert summary["material_disagreement_count"] == 1
    assert summary["adjudication_task_count"] == 1
    assert summary["outcome_fields_supplied_to_reviewer"] is False
    assert [call["phase"] for call in fake.calls] == [
        "PASS1",
        "PASS2",
        "ADJUDICATION",
    ]
    models = [call["argv"][call["argv"].index("-m") + 1] for call in fake.calls]
    assert models == [
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt-5.6-sol",
    ]
    for call in fake.calls[1:2]:
        assert '"primary_class"' not in call["prompt"]
        assert '"selection_reason"' not in call["prompt"]
        assert '"primary_review"' not in call["prompt"]
        assert "fixture-app-" not in call["prompt"]
        assert "NEGATIVE_AUDIT" not in call["prompt"]

    final_path = output / "final" / "reviews.jsonl"
    final_bytes = final_path.read_bytes()
    assert driver._sha256(final_bytes) == summary["final_reviews_sha256"]
    final_reviews = [json.loads(line) for line in final_bytes.splitlines()]
    assert len(final_reviews) == EXPECTED_TASK_COUNT
    task_two = next(review for review in final_reviews if review["task_name"] == "Task002")
    assert task_two["phase"] == "ADJUDICATION"
    assert task_two["task_screen_class"] == "UNCERTAIN"
    assert (output / "frozen" / "pass1_reviews.jsonl").is_file()
    assert (output / "selection" / "pass2_selection.json").is_file()
    metrics_bytes = (output / "final" / "metrics.json").read_bytes()
    metrics = json.loads(metrics_bytes)
    assert driver._sha256(metrics_bytes) == summary["motivation_metrics_sha256"]
    assert metrics["denominators"] == {
        "audited_task_count": EXPECTED_TASK_COUNT,
        "reviewed_candidate_count": EXPECTED_TASK_COUNT,
    }
    assert summary["motivation_strength"] == metrics["motivation_strength"]


def test_review_response_uses_authoritative_card_hash(tmp_path: Path) -> None:
    cards, all_cards = _loaded_fixture(tmp_path)
    batch = driver.fixed_batches(cards[:1], 1, phase="PASS1")[0]
    fake = FakeCodex()
    artifact = driver.execute_stage_batch(
        batch=batch,
        all_cards_by_task=all_cards,
        output_root=tmp_path / "output",
        model="gpt-5.6-terra",
        reviewer_id=driver.DEFAULT_PRIMARY_REVIEWER,
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=10,
        resume=False,
        runner=fake,
    )
    review = artifact.result["reviews"][0]
    assert review["card_sha256"] == canonical_sha256(cards[0].payload)
