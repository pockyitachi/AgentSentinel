from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mobile_world.offline.failure_attribution import PhaseABundle, PhaseAResolution
from mobile_world.offline.failure_link_review_runtime import (
    ReviewRetryExhausted,
    ReviewUnit,
    StageArtifact,
    sha256_bytes,
)
from mobile_world.offline.motivation_review import canonical_json_bytes

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_failure_link_codex_review.py"
_SPEC = importlib.util.spec_from_file_location("failure_link_codex_driver", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
driver = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = driver
_SPEC.loader.exec_module(driver)


def _phase_a_card(index: int) -> dict[str, Any]:
    return {
        "schema_version": "mobileworld.audit.failure-attribution/v1",
        "record_type": "failure_attribution_phase_a_card",
        "attribution_run_id": "fixture-run-a",
        "phase": "A",
        "outcome_blinded": True,
        "causal_claim_supported": False,
        "task_key": f"fixture-model/Task{index}",
        "model_id": "fixture-model",
        "task": {
            "catalog_index": index,
            "task_name": f"Task{index}",
            "source_relative_run_path": f"source/run-{index}",
        },
        "instruction": "fixture",
        "source_binding": {},
        "frozen_strict_mhr_chains": [],
        "terminal_trace": {"start_step": 1, "end_step": 1, "steps": []},
        "annotation_template": [],
    }


def _review_card(index: int) -> Any:
    payload = _phase_a_card(index)
    return driver.ReviewCard(
        payload=payload,
        card_sha256=driver._canonical_sha256(payload),
        attachments=(),
        image_paths=(),
        image_sha256s=(),
        image_byte_lengths=(),
        attachment_map=(),
    )


def _phase_a_bundle(task_count: int = 2) -> PhaseABundle:
    cards = tuple(_phase_a_card(index) for index in range(1, task_count + 1))
    return PhaseABundle(
        manifest={
            "attribution_run_id": "fixture-run-a",
            "outcome_blinded": True,
            "causal_claim_supported": False,
            "phase_a_card_set_sha256": "a" * 64,
            "counts": {
                "strict_mhr_task_count": task_count,
                "strict_mhr_chain_count": task_count,
            },
        },
        cards=cards,
        sources=(),
    )


def test_phase_a_cli_rejects_outcomes_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        driver._parser().parse_args(
            [
                "phase-a",
                "--bundle",
                "fixture-model=/fixture",
                "--source-base",
                "/source",
                "--output-dir",
                "/output",
                "--outcomes",
                "/forbidden.jsonl",
            ]
        )
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --outcomes" in capsys.readouterr().err
    assert "outcomes" not in inspect.signature(driver.run_phase_a).parameters


def test_phase_a_dry_run_has_zero_writes_subprocesses_and_outcome_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _phase_a_bundle()
    monkeypatch.setattr(driver, "build_phase_a_bundle", lambda sources: bundle)
    monkeypatch.setattr(
        driver,
        "_load_review_cards",
        lambda cards, source_base: tuple(_review_card(index) for index in (1, 2)),
    )
    output = tmp_path / "must-not-exist"
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not invoke Codex")

    result = driver.run_phase_a(
        source_bundles=(object(),),
        source_base=tmp_path,
        output_root=output,
        primary_model="p",
        secondary_model="s",
        adjudicator_model="a",
        primary_reviewer_id="p-reviewer",
        secondary_reviewer_id="s-reviewer",
        adjudicator_reviewer_id="a-reviewer",
        codex_bin="codex",
        max_attempts=1,
        timeout_seconds=1,
        resume=False,
        dry_run=True,
        runner=forbidden_runner,
        expected_source_count=1,
        expected_task_count=2,
        expected_chain_count=2,
    )
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert before == after
    assert not output.exists()
    assert result["outcomes_opened"] is False
    assert result["codex_invocations"] == 0
    assert result["write_count"] == 0
    assert result["profile"]["prompt_bytes"]["max"] > 0
    assert len(result["profile"]["per_card"]) == 2
    assert result["profile"]["limits_satisfied"] is True


def test_offline_tokenizer_is_hash_pinned_and_never_uses_lazy_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver._PROMPT_TOKENIZERS.clear()
    monkeypatch.setattr(
        driver.tiktoken,
        "get_encoding",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("token preflight must not use tiktoken's lazy downloader")
        ),
    )
    assert driver.file_sha256(driver.DEFAULT_PROMPT_TOKENIZER_ASSET) == (
        driver.O200K_BASE_ASSET_SHA256
    )
    assert (
        driver._prompt_token_count(
            "hello", tokenizer_asset_path=driver.DEFAULT_PROMPT_TOKENIZER_ASSET
        )
        == 1
    )
    assert (
        driver._prompt_token_count(
            "你好，世界", tokenizer_asset_path=driver.DEFAULT_PROMPT_TOKENIZER_ASSET
        )
        == 3
    )
    assert (
        driver._prompt_token_count(
            "<|endoftext|>", tokenizer_asset_path=driver.DEFAULT_PROMPT_TOKENIZER_ASSET
        )
        == 7
    )


def test_v4_run_manifest_freezes_prompt_runtime_and_outcome_boundary() -> None:
    manifest = driver._run_manifest(
        phase="A",
        bundle_manifest={"phase_a_card_set_sha256": "a" * 64},
        source_bundles=(),
        primary_model="primary-model",
        secondary_model="secondary-model",
        adjudicator_model="adjudicator-model",
        primary_reviewer_id="primary-reviewer-v4",
        secondary_reviewer_id="secondary-reviewer-v4",
        adjudicator_reviewer_id="adjudicator-reviewer-v4",
        codex_bin="codex",
        max_attempts=3,
        timeout_seconds=30,
        phase_a_driver_freeze_sha256=None,
        primary_task_count=116,
    )
    driver._validate_run_manifest_prompt_contract(manifest, phase="A")
    assert manifest["primary_review_origin_counts"] == {
        driver.CURRENT_PRIMARY_ORIGIN: 116,
        driver.LEGACY_PRIMARY_ORIGIN: 0,
    }
    assert manifest["primary_migration"] is None
    assert manifest["outcomes_opened_at_manifest_write"] is False
    assert manifest["prompt_version"] == driver.PROMPT_VERSION
    assert manifest["runtime_schema_version"] == driver.RUNTIME_SCHEMA_VERSION

    tampered = dict(manifest)
    tampered["outcomes_opened_at_manifest_write"] = True
    with pytest.raises(driver.FailureLinkDriverError, match="outcomes_opened"):
        driver._validate_run_manifest_prompt_contract(tampered, phase="A")


def test_prompt_preflight_has_independent_char_byte_and_token_fail_closed_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        driver,
        "_prompt_token_count",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("earlier prompt gate must short-circuit tokenization")
        ),
    )
    with pytest.raises(driver.FailureLinkDriverError, match="character limit"):
        driver._validate_prompt_budget(
            "x" * (driver.MAX_PROMPT_CHARS + 1),
            unit_id="char-limit",
            tokenizer_asset_path=tmp_path / "unused",
        )
    with pytest.raises(driver.FailureLinkDriverError, match="UTF-8 byte limit"):
        driver._validate_prompt_budget(
            "界" * (driver.MAX_PROMPT_BYTES // 3 + 1),
            unit_id="byte-limit",
            tokenizer_asset_path=tmp_path / "unused",
        )

    monkeypatch.setattr(
        driver,
        "_prompt_token_count",
        lambda *args, **kwargs: driver.MAX_PROMPT_TOKENS + 1,
    )
    with pytest.raises(driver.FailureLinkDriverError, match="token limit"):
        driver._validate_prompt_budget(
            "small",
            unit_id="token-limit",
            tokenizer_asset_path=tmp_path / "unused",
        )


def test_missing_tokenizer_asset_stops_before_runner_or_artifact_write(
    tmp_path: Path,
) -> None:
    calls = 0

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("missing offline tokenizer must precede subprocess")

    output = tmp_path / "review"
    with pytest.raises(driver.FailureLinkDriverError, match="required offline"):
        driver._execute_pass(
            phase="A",
            stage="PRIMARY",
            cards=(_review_card(1),),
            schema={},
            bundle_manifest={"phase_a_card_set_sha256": "a" * 64},
            output_root=output,
            model="model",
            reviewer_id="reviewer",
            codex_bin="codex",
            max_attempts=1,
            timeout_seconds=1,
            resume=False,
            runner=forbidden_runner,
            prompt_tokenizer_asset=tmp_path / "missing.tiktoken",
        )
    assert calls == 0
    assert not output.exists()


def test_v4_identity_is_noncolliding_and_retry_feedback_carries_expected_actual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _review_card(1)
    current_identity = driver._review_identity(
        phase="A",
        stage="PRIMARY",
        reviewer_id="reviewer",
        card=card.payload,
        card_sha256=card.card_sha256,
    )
    legacy_identity = driver._legacy_v3_review_identity(
        phase="A",
        stage="PRIMARY",
        reviewer_id="reviewer",
        card=card.payload,
        card_sha256=card.card_sha256,
    )
    assert current_identity["review_id"] != legacy_identity["review_id"]
    assert "-v4-" in current_identity["review_id"]
    assert all(
        value.endswith("-v4")
        for value in (
            driver.DEFAULT_PHASE_A_PRIMARY_REVIEWER,
            driver.DEFAULT_PHASE_A_SECONDARY_REVIEWER,
            driver.DEFAULT_PHASE_A_ADJUDICATOR_REVIEWER,
            driver.DEFAULT_PHASE_B_PRIMARY_REVIEWER,
            driver.DEFAULT_PHASE_B_SECONDARY_REVIEWER,
            driver.DEFAULT_PHASE_B_ADJUDICATOR_REVIEWER,
        )
    )

    monkeypatch.setattr(
        driver,
        "validate_phase_a_reviews",
        lambda responses, cards: {card.task_key: dict(responses[0])},
    )
    prompts: list[str] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        prompts.append(kwargs["input"].decode("utf-8"))
        response = dict(current_identity)
        if len(prompts) == 1:
            response["review_id"] = legacy_identity["review_id"]
        Path(argv[argv.index("-o") + 1]).write_bytes(canonical_json_bytes(response))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    reviews, artifacts = driver._execute_pass(
        phase="A",
        stage="PRIMARY",
        cards=(card,),
        schema={},
        bundle_manifest={"phase_a_card_set_sha256": "a" * 64},
        output_root=tmp_path / "review",
        model="model",
        reviewer_id="reviewer",
        codex_bin="codex",
        max_attempts=2,
        timeout_seconds=1,
        resume=False,
        runner=runner,
    )
    assert reviews == (current_identity,)
    assert len(artifacts) == 1
    assert len(prompts) == 2
    assert f'expected=\\"{current_identity["review_id"]}\\"' in prompts[1]
    assert f'actual=\\"{legacy_identity["review_id"]}\\"' in prompts[1]
    receipt = json.loads((artifacts[0].directory / "receipt.json").read_bytes())
    assert receipt["attempt_count"] == 2
    assert receipt["receipt_binding"]["prompt_version"] == driver.PROMPT_VERSION


def test_card_attachments_include_digest_bound_prefix_and_terminal_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix_digest = "a" * 64
    terminal_digest = "b" * 64
    paths = {
        prefix_digest: tmp_path / "prefix.png",
        terminal_digest: tmp_path / "terminal.png",
    }
    for path in paths.values():
        path.write_bytes(b"fixture")
    card = _phase_a_card(1)
    card["prefix_trace"] = {
        "steps": [
            {
                "step_index": 1,
                "state_before": {
                    "observation": {"screenshot": {"pixel_blob": {"digest": prefix_digest}}}
                },
                "state_after": {},
            }
        ]
    }
    card["terminal_trace"] = {
        "steps": [
            {
                "step_index": 2,
                "state_before": {},
                "state_after": {
                    "observation": {"screenshot": {"pixel_blob": {"digest": terminal_digest}}}
                },
            }
        ]
    }

    def fake_resolve(
        run_root: Path,
        digest: str,
        *,
        digest_cache: dict[tuple[Path, str], Path | None],
    ) -> Path:
        del run_root, digest_cache
        return paths[digest]

    monkeypatch.setattr(driver, "_resolve_image_blob", fake_resolve)
    attachments = driver._card_attachments(card, run_root=tmp_path, digest_cache={})
    assert [(item.role, item.step) for item in attachments] == [
        ("prefix_trace_pre", 1),
        ("terminal_trace_post", 2),
    ]


def test_attachment_total_byte_limit_fails_without_truncation(tmp_path: Path) -> None:
    payload = _phase_a_card(1)
    oversized = driver.ReviewCard(
        payload=payload,
        card_sha256=driver._canonical_sha256(payload),
        attachments=(),
        image_paths=(tmp_path / "must-not-be-read.png",),
        image_sha256s=("a" * 64,),
        image_byte_lengths=(driver.MAX_ATTACHMENT_TOTAL_BYTES + 1,),
        attachment_map=(),
    )
    with pytest.raises(driver.FailureLinkDriverError, match="exceed hard limit"):
        driver._dry_run_profile(
            phase="A",
            cards=(oversized,),
            schema={},
            reviewer_id="reviewer",
        )


def test_source_blob_symlink_is_rejected_before_resolve(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nfixture"
    digest = sha256_bytes(payload)
    external = tmp_path / "external.png"
    external.write_bytes(payload)
    run_root = tmp_path / "run"
    blob_path = run_root / "blobs" / "sha256" / digest[:2] / digest
    blob_path.parent.mkdir(parents=True)
    blob_path.symlink_to(external)
    with pytest.raises(driver.FailureLinkDriverError, match="symlink artifact path is forbidden"):
        driver._resolve_image_blob(run_root, digest, digest_cache={})


def test_phase_b_gate_precedes_public_outcome_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _phase_a_bundle(task_count=1)
    monkeypatch.setattr(driver, "build_phase_a_bundle", lambda sources: bundle)
    opened = False

    def forbidden_phase_b_builder(*args: Any, **kwargs: Any) -> Any:
        nonlocal opened
        opened = True
        raise AssertionError("outcomes opened before Phase-A freeze verification")

    monkeypatch.setattr(driver, "build_phase_b_bundle", forbidden_phase_b_builder)
    monkeypatch.setattr(
        driver,
        "_verify_phase_a_driver_freeze",
        lambda **kwargs: (_ for _ in ()).throw(
            driver.FailureLinkDriverError("Phase-A receipt hash mismatch")
        ),
    )
    with pytest.raises(driver.FailureLinkDriverError, match="receipt hash mismatch"):
        driver.run_phase_b(
            source_bundles=(object(),),
            phase_a_root=tmp_path / "phase-a",
            phase_a_driver_freeze_sha256="a" * 64,
            source_base=tmp_path,
            output_root=tmp_path / "phase-b",
            primary_model="p",
            secondary_model="s",
            adjudicator_model="a",
            primary_reviewer_id="bp",
            secondary_reviewer_id="bs",
            adjudicator_reviewer_id="ba",
            codex_bin="codex",
            max_attempts=1,
            timeout_seconds=1,
            resume=False,
            dry_run=True,
            expected_source_count=1,
            expected_task_count=1,
            expected_chain_count=1,
        )
    assert opened is False
    assert not (tmp_path / "phase-b").exists()


@pytest.mark.parametrize("phase_a_id_field", ["configured_reviewer_ids", "receipt_reviewer_ids"])
def test_phase_b_reviewer_independence_includes_configured_and_legacy_receipt_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase_a_id_field: str,
) -> None:
    bundle = _phase_a_bundle(task_count=1)
    monkeypatch.setattr(driver, "build_phase_a_bundle", lambda sources: bundle)
    freeze = {
        "configured_reviewer_ids": [],
        "receipt_reviewer_ids": [],
    }
    freeze[phase_a_id_field] = ["bp"]
    monkeypatch.setattr(
        driver,
        "_verify_phase_a_driver_freeze",
        lambda **kwargs: (
            PhaseAResolution({}, (), (), (), ()),
            freeze,
        ),
    )
    monkeypatch.setattr(
        driver,
        "build_phase_b_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reviewer overlap must precede outcomes")
        ),
    )
    with pytest.raises(driver.FailureLinkDriverError, match="distinct from Phase A"):
        driver.run_phase_b(
            source_bundles=(object(),),
            phase_a_root=tmp_path / "phase-a",
            phase_a_driver_freeze_sha256="a" * 64,
            source_base=tmp_path,
            output_root=tmp_path / "phase-b",
            primary_model="p",
            secondary_model="s",
            adjudicator_model="a",
            primary_reviewer_id="bp",
            secondary_reviewer_id="bs",
            adjudicator_reviewer_id="ba",
            codex_bin="codex",
            max_attempts=1,
            timeout_seconds=1,
            resume=False,
            dry_run=True,
            expected_source_count=1,
            expected_task_count=1,
            expected_chain_count=1,
        )


def test_phase_a_current_artifact_gate_rebuilds_attempt_prompt_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _review_card(1)
    schema: dict[str, Any] = {}
    bundle_manifest = {"phase_a_card_set_sha256": "a" * 64}
    run_manifest = {
        "adjudicator_model": "adjudicator-model",
        "adjudicator_reviewer_id": "adjudicator-reviewer-v4",
        "codex_bin": "codex",
        "max_attempts": 2,
        "phase": "A",
        "phase_a_driver_freeze_sha256": None,
        "primary_model": "primary-model",
        "primary_reviewer_id": "primary-reviewer-v4",
        "secondary_model": "secondary-model",
        "secondary_reviewer_id": "secondary-reviewer-v4",
        "timeout_seconds": 30,
    }
    identity = driver._review_identity(
        phase="A",
        stage="PRIMARY",
        reviewer_id=run_manifest["primary_reviewer_id"],
        card=card.payload,
        card_sha256=card.card_sha256,
    )
    monkeypatch.setattr(
        driver,
        "validate_phase_a_reviews",
        lambda responses, cards: {card.task_key: dict(responses[0])},
    )
    unit = ReviewUnit(
        phase="A",
        stage="PRIMARY",
        unit_id=f"a-primary-0001-{card.card_sha256[:12]}",
        task_key=card.task_key,
        card_sha256=card.card_sha256,
    )
    expected_receipt, render_prompt, validate_response = driver._current_v4_expected_receipt(
        unit=unit,
        card=card,
        schema=schema,
        bundle_manifest=bundle_manifest,
        run_manifest=run_manifest,
        prompt_tokenizer_asset=driver.DEFAULT_PROMPT_TOKENIZER_ASSET,
    )

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        Path(argv[argv.index("-o") + 1]).write_bytes(canonical_json_bytes(identity))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    output = tmp_path / "phase-a"
    artifact = driver.execute_review_unit(
        unit=unit,
        output_root=output,
        model=run_manifest["primary_model"],
        reviewer_id=run_manifest["primary_reviewer_id"],
        codex_bin=run_manifest["codex_bin"],
        schema=schema,
        render_prompt=render_prompt,
        validate_response=validate_response,
        receipt_binding=expected_receipt["receipt_binding"],
        max_attempts=run_manifest["max_attempts"],
        timeout_seconds=run_manifest["timeout_seconds"],
        resume=False,
        runner=runner,
    )
    raw = {
        "receipt_path": (artifact.directory / "receipt.json").relative_to(output).as_posix(),
        "receipt_sha256": artifact.receipt_sha256,
        "response_sha256": artifact.response_sha256,
        "stage": "PRIMARY",
        "task_key": card.task_key,
        "unit_id": unit.unit_id,
    }
    resolution = PhaseAResolution(
        manifest={},
        primary_reviews=(identity,),
        secondary_reviews=(),
        adjudication_reviews=(),
        final_reviews=(),
    )
    driver._verify_current_review_artifacts(
        output_root=output,
        raw_records=[raw],
        resolution=resolution,
        bundle_manifest=bundle_manifest,
        cards=(card,),
        schema=schema,
        run_manifest=run_manifest,
        primary_migration=None,
        prompt_tokenizer_asset=driver.DEFAULT_PROMPT_TOKENIZER_ASSET,
    )

    receipt_path = artifact.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["attempts"][0]["prompt_sha256"] = "f" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    raw["receipt_sha256"] = driver.file_sha256(receipt_path)
    with pytest.raises(driver.ReviewRuntimeError, match="resume prompt mismatch"):
        driver._verify_current_review_artifacts(
            output_root=output,
            raw_records=[raw],
            resolution=resolution,
            bundle_manifest=bundle_manifest,
            cards=(card,),
            schema=schema,
            run_manifest=run_manifest,
            primary_migration=None,
            prompt_tokenizer_asset=driver.DEFAULT_PROMPT_TOKENIZER_ASSET,
        )


def test_systemic_stop_preserves_completed_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = tuple(_review_card(index) for index in range(1, 6))
    calls: list[str] = []
    preserved = tmp_path / "preserved"
    preserved.mkdir()

    def fake_execute(*, unit: ReviewUnit, **kwargs: Any) -> StageArtifact:
        calls.append(unit.unit_id)
        if len(calls) == 1:
            return StageArtifact(
                unit=unit,
                response={"task_key": unit.task_key},
                response_sha256="b" * 64,
                receipt_sha256="c" * 64,
                directory=preserved,
                resumed=False,
            )
        raise ReviewRetryExhausted("fixture exhaustion")

    monkeypatch.setattr(driver, "execute_review_unit", fake_execute)
    with pytest.raises(driver.SystemicReviewFailure, match="artifacts were preserved"):
        driver._execute_pass(
            phase="A",
            stage="PRIMARY",
            cards=cards,
            schema={},
            bundle_manifest={"phase_a_card_set_sha256": "d" * 64},
            output_root=tmp_path / "review",
            model="model",
            reviewer_id="reviewer",
            codex_bin="codex",
            max_attempts=1,
            timeout_seconds=1,
            resume=False,
            runner=lambda *args, **kwargs: None,
        )
    assert len(calls) == 4
    assert preserved.is_dir()


def _write_receipt_artifact(
    root: Path,
    *,
    stage: str,
    card: dict[str, Any],
    bundle_manifest_sha256: str,
) -> dict[str, Any]:
    task_key = card["task_key"]
    card_sha256 = driver._canonical_sha256(card)
    prompt_card, card_transport = driver.prepare_card_for_prompt(
        card,
        transport_encoding=driver.INLINE_CARD_TRANSPORT_ENCODING,
    )
    unit_id = f"a-{stage.lower()}-0001-{card_sha256[:12]}"
    directory = root / "batches" / f"phase-a-{stage.lower()}" / unit_id
    directory.mkdir(parents=True)
    response = {"task_key": task_key}
    image_attachments: list[dict[str, Any]] = []
    schema = {"type": "object"}
    response_bytes = canonical_json_bytes(response)
    schema_bytes = canonical_json_bytes(schema)
    (directory / "response.json").write_bytes(response_bytes)
    (directory / "model_response.json").write_bytes(response_bytes)
    (directory / "output_schema.json").write_bytes(schema_bytes)
    receipt = {
        "card_sha256": card_sha256,
        "image_attachment_set_sha256": driver._canonical_sha256(image_attachments),
        "image_attachment_total_bytes": 0,
        "image_attachments": image_attachments,
        "model_response_sha256": sha256_bytes(response_bytes),
        "max_prompt_bytes": driver.MAX_PROMPT_BYTES,
        "max_prompt_chars": driver.MAX_PROMPT_CHARS,
        "receipt_binding": {
            "bundle_manifest_sha256": bundle_manifest_sha256,
            "card_transport_encoding": driver.INLINE_CARD_TRANSPORT_ENCODING,
            "card_transport_sha256": driver._canonical_sha256(card_transport),
            "driver_schema_version": driver.DRIVER_SCHEMA_VERSION,
            "large_card_encoding_threshold_bytes": (driver.LARGE_CARD_ENCODING_THRESHOLD_BYTES),
            "max_prompt_bytes": driver.MAX_PROMPT_BYTES,
            "max_prompt_chars": driver.MAX_PROMPT_CHARS,
            "max_prompt_tokens": driver.MAX_PROMPT_TOKENS,
            "prompt_token_encoding": driver.PROMPT_TOKEN_ENCODING,
            "prompt_tokenizer_asset_sha256": driver.O200K_BASE_ASSET_SHA256,
            "prompt_version": driver.PROMPT_VERSION,
            "prompt_card_sha256": driver._canonical_sha256(prompt_card),
            "tiktoken_version": driver.TIKTOKEN_VERSION,
        },
        "response_sha256": sha256_bytes(response_bytes),
        "reviewer_id": f"{stage.lower()}-reviewer",
        "runtime_schema_version": driver.RUNTIME_SCHEMA_VERSION,
        "schema_sha256": sha256_bytes(schema_bytes),
        "stage": stage,
        "task_key": task_key,
        "unit_id": unit_id,
    }
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_path = directory / "receipt.json"
    receipt_path.write_bytes(receipt_bytes)
    return {
        "card_sha256": card_sha256,
        "image_attachment_set_sha256": driver._canonical_sha256(image_attachments),
        "image_attachment_total_bytes": 0,
        "image_attachments": image_attachments,
        "origin": driver.CURRENT_PRIMARY_ORIGIN,
        "prompt_version": driver.PROMPT_VERSION,
        "receipt_path": receipt_path.relative_to(root).as_posix(),
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "response_sha256": sha256_bytes(response_bytes),
        "runtime_schema_version": driver.RUNTIME_SCHEMA_VERSION,
        "stage": stage,
        "task_key": task_key,
        "transport_encoding": driver.INLINE_CARD_TRANSPORT_ENCODING,
        "unit_id": unit_id,
    }


def _legacy_migration_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    Path,
    tuple[Any, ...],
    dict[str, Any],
    dict[str, Any],
    Any,
]:
    source = tmp_path / "legacy-phase-a"
    cards = tuple(_review_card(index) for index in range(1, 117))
    schema = {"type": "object"}
    bundle_manifest = {"phase_a_card_set_sha256": "a" * 64}
    source_run_manifest = {
        "attribution_schema_version": driver.ATTRIBUTION_SCHEMA_VERSION,
        "bundle_manifest_sha256": driver._canonical_sha256(bundle_manifest),
        "causal_claim_supported": False,
        "codex_bin": "codex",
        "driver_schema_version": driver.LEGACY_DRIVER_SCHEMA_VERSION,
        "max_attachment_count": driver.MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": driver.MAX_ATTACHMENT_TOTAL_BYTES,
        "max_attempts": 3,
        "max_estimated_request_payload_bytes": (driver.MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES),
        "outcomes_opened_at_manifest_write": False,
        "phase": "A",
        "phase_a_driver_freeze_sha256": None,
        "primary_model": driver.DEFAULT_PRIMARY_MODEL,
        "primary_reviewer_id": "legacy-primary-v3",
        "prompt_version": driver.LEGACY_PROMPT_VERSION,
        "reviewer_disabled_features": list(driver.REVIEWER_DISABLED_FEATURES),
        "timeout_seconds": 1800,
    }
    driver.write_once(
        source / "run_manifest.json",
        canonical_json_bytes(source_run_manifest),
    )
    driver.write_once(
        source / "input" / "cards.jsonl",
        b"".join(canonical_json_bytes(card.payload) for card in cards),
    )
    driver.write_once(
        source / "input" / "manifest.json",
        canonical_json_bytes(bundle_manifest),
    )
    driver.write_once(
        source / "input" / "review_schema.json",
        canonical_json_bytes(schema),
    )
    for ordinal, card in enumerate(cards, start=1):
        if ordinal in driver.EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS:
            continue
        unit_id = f"a-primary-{ordinal:04d}-{card.card_sha256[:12]}"
        unit_root = source / "batches" / "phase-a-primary" / unit_id
        response_bytes = canonical_json_bytes({"task_key": card.task_key})
        schema_bytes = canonical_json_bytes(schema)
        driver.write_once(unit_root / "model_response.json", response_bytes)
        driver.write_once(unit_root / "output_schema.json", schema_bytes)
        driver.write_once(unit_root / "response.json", response_bytes)
        receipt = {
            "accepted_attempt": 1,
            "accepted_prompt_sha256": "b" * 64,
            "attempt_count": 1,
            "attempts": [{"prompt_sha256": "b" * 64}],
            "model_response_sha256": sha256_bytes(response_bytes),
            "response_sha256": sha256_bytes(response_bytes),
            "schema_sha256": sha256_bytes(schema_bytes),
        }
        driver.write_once(unit_root / "receipt.json", canonical_json_bytes(receipt))

    def fake_verify(*, unit: ReviewUnit, target: Path, **kwargs: Any) -> StageArtifact:
        response = json.loads((target / "response.json").read_bytes())
        return StageArtifact(
            unit=unit,
            response=response,
            response_sha256=driver.file_sha256(target / "response.json"),
            receipt_sha256=driver.file_sha256(target / "receipt.json"),
            directory=target,
            resumed=True,
        )

    monkeypatch.setattr(driver, "verify_frozen_review_artifact", fake_verify)
    plan = driver._inspect_primary_migration(
        source_root=source,
        cards=cards,
        schema=schema,
        bundle_manifest=bundle_manifest,
        primary_model=driver.DEFAULT_PRIMARY_MODEL,
    )
    return source, cards, schema, bundle_manifest, plan


def test_explicit_v3_primary_migration_is_pinned_write_once_and_exact_114_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cards, schema, bundle_manifest, plan = _legacy_migration_fixture(tmp_path, monkeypatch)
    assert len(plan.seeds) == 114
    assert plan.missing_task_keys == (cards[41].task_key, cards[65].task_key)
    assert (
        driver._prepare_primary_migration(
            source_root=source,
            expected_run_manifest_sha256=plan.source_run_manifest_sha256,
            expected_source_snapshot_sha256=plan.source_snapshot_sha256,
            expected_accepted_set_sha256=plan.accepted_set_sha256,
            cards=cards,
            schema=schema,
            bundle_manifest=bundle_manifest,
            primary_model=driver.DEFAULT_PRIMARY_MODEL,
        ).accepted_set_sha256
        == plan.accepted_set_sha256
    )
    driver._validate_primary_migration_reviewer_independence(
        plan,
        primary_reviewer_id=driver.DEFAULT_PHASE_A_PRIMARY_REVIEWER,
        secondary_reviewer_id=driver.DEFAULT_PHASE_A_SECONDARY_REVIEWER,
        adjudicator_reviewer_id=driver.DEFAULT_PHASE_A_ADJUDICATOR_REVIEWER,
    )
    legacy_primary_reviewer_id = plan.source_run_manifest["primary_reviewer_id"]
    current_reviewer_ids = {
        "primary_reviewer_id": driver.DEFAULT_PHASE_A_PRIMARY_REVIEWER,
        "secondary_reviewer_id": driver.DEFAULT_PHASE_A_SECONDARY_REVIEWER,
        "adjudicator_reviewer_id": driver.DEFAULT_PHASE_A_ADJUDICATOR_REVIEWER,
    }
    for reviewer_role in current_reviewer_ids:
        colliding_reviewer_ids = current_reviewer_ids | {reviewer_role: legacy_primary_reviewer_id}
        with pytest.raises(
            driver.FailureLinkDriverError,
            match="distinct from the migrated legacy primary reviewer",
        ):
            driver._validate_primary_migration_reviewer_independence(
                plan,
                **colliding_reviewer_ids,
            )

    output = tmp_path / "fresh-v4"
    output.mkdir()
    run_manifest = {
        "phase": "A",
        "primary_model": driver.DEFAULT_PRIMARY_MODEL,
        "primary_migration": {
            "accepted_set_sha256": plan.accepted_set_sha256,
            "missing_task_keys": list(plan.missing_task_keys),
            "schema_version": driver.PRIMARY_MIGRATION_SCHEMA_VERSION,
            "source_root_realpath": str(plan.source_root),
            "source_run_manifest_sha256": plan.source_run_manifest_sha256,
            "source_snapshot_sha256": plan.source_snapshot_sha256,
        },
        "primary_review_origin_counts": {
            driver.CURRENT_PRIMARY_ORIGIN: 2,
            driver.LEGACY_PRIMARY_ORIGIN: 114,
        },
    }
    driver.write_once(output / "run_manifest.json", canonical_json_bytes(run_manifest))
    driver.write_once(
        output / "input" / "cards.jsonl",
        b"".join(canonical_json_bytes(card.payload) for card in cards),
    )
    driver.write_once(output / "input" / "manifest.json", canonical_json_bytes(bundle_manifest))
    driver.write_once(output / "input" / "review_schema.json", canonical_json_bytes(schema))
    overlay = driver._materialize_primary_migration(
        plan=plan,
        output_root=output,
        run_manifest=run_manifest,
        bundle_manifest=bundle_manifest,
        cards=cards,
        schema=schema,
    )
    loaded = driver._load_primary_migration_overlay(
        output_root=output,
        run_manifest=run_manifest,
        bundle_manifest=bundle_manifest,
        cards=cards,
        schema=schema,
    )
    assert loaded is not None
    assert loaded.freeze_sha256 == overlay.freeze_sha256
    assert len(loaded.artifacts_by_task) == 114
    assert len(list((output / "migration" / "v3-primary" / "artifacts").rglob("*.json"))) == 456
    assert not (output / "migration" / "v3-primary" / "rejected").exists()
    assert not (output / "migration" / "v3-primary" / "failures").exists()

    unexpected = output / "migration" / "v3-primary" / "unexpected"
    unexpected.mkdir()
    with pytest.raises(driver.FailureLinkDriverError, match="directory set mismatch"):
        driver._verify_materialized_migration_tree(
            migration_root=output / "migration" / "v3-primary",
            expected_unit_ids={seed.unit.unit_id for seed in plan.seeds},
        )

    symlink_output = tmp_path / "symlink-v4"
    symlink_output.mkdir()
    external = tmp_path / "external-migration-target"
    external.mkdir()
    (symlink_output / "migration").symlink_to(external, target_is_directory=True)
    with pytest.raises(driver.FailureLinkDriverError, match="symlink artifact path"):
        driver._materialize_primary_migration(
            plan=plan,
            output_root=symlink_output,
            run_manifest=run_manifest,
            bundle_manifest=bundle_manifest,
            cards=cards,
            schema=schema,
        )
    assert list(external.iterdir()) == []


def test_migration_source_drift_fails_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cards, schema, bundle_manifest, plan = _legacy_migration_fixture(tmp_path, monkeypatch)
    first_seed = plan.seeds[0]
    (first_seed.source_directory / "response.json").write_bytes(
        canonical_json_bytes({"task_key": "tampered"})
    )
    output = tmp_path / "fresh-v4"
    output.mkdir()
    with pytest.raises(driver.FailureLinkDriverError, match="changed before materialization"):
        driver._materialize_primary_migration(
            plan=plan,
            output_root=output,
            run_manifest={},
            bundle_manifest=bundle_manifest,
            cards=cards,
            schema=schema,
        )
    assert not (output / "migration").exists()


def test_outer_freeze_receipts_bind_response_schema_and_complete_passes(
    tmp_path: Path,
) -> None:
    card = _phase_a_card(1)
    bundle_manifest = {"phase_a_card_set_sha256": "a" * 64}
    bundle_hash = driver._canonical_sha256(bundle_manifest)
    records = [
        _write_receipt_artifact(
            tmp_path,
            stage=stage,
            card=card,
            bundle_manifest_sha256=bundle_hash,
        )
        for stage in ("PRIMARY", "SECONDARY")
    ]
    resolution = PhaseAResolution(
        manifest={
            "counts": {
                "primary_review_count": 1,
                "secondary_review_count": 1,
                "adjudication_review_count": 0,
            },
            "task_resolutions": [{"task_key": card["task_key"], "material_disagreement": False}],
        },
        primary_reviews=({"task_key": card["task_key"]},),
        secondary_reviews=({"task_key": card["task_key"]},),
        adjudication_reviews=(),
        final_reviews=(),
    )
    driver._verify_receipt_index(
        tmp_path,
        records,
        resolution=resolution,
        bundle_manifest=bundle_manifest,
        cards=(_review_card(1),),
    )
    response_path = tmp_path / Path(records[0]["receipt_path"]).parent / "response.json"
    response_path.write_bytes(canonical_json_bytes({"task_key": "tampered"}))
    with pytest.raises(driver.FailureLinkDriverError, match="response hash mismatch"):
        driver._verify_receipt_index(
            tmp_path,
            records,
            resolution=resolution,
            bundle_manifest=bundle_manifest,
            cards=(_review_card(1),),
        )


def test_outer_freeze_rejects_self_consistent_response_not_in_resolution(
    tmp_path: Path,
) -> None:
    card = _phase_a_card(1)
    bundle_manifest = {"phase_a_card_set_sha256": "a" * 64}
    bundle_hash = driver._canonical_sha256(bundle_manifest)
    records = [
        _write_receipt_artifact(
            tmp_path,
            stage=stage,
            card=card,
            bundle_manifest_sha256=bundle_hash,
        )
        for stage in ("PRIMARY", "SECONDARY")
    ]
    expected = {"task_key": card["task_key"], "rationale": "frozen resolution"}
    resolution = PhaseAResolution(
        manifest={
            "counts": {
                "primary_review_count": 1,
                "secondary_review_count": 1,
                "adjudication_review_count": 0,
            },
            "task_resolutions": [{"task_key": card["task_key"], "material_disagreement": False}],
        },
        primary_reviews=(expected,),
        secondary_reviews=(expected,),
        adjudication_reviews=(),
        final_reviews=(expected,),
    )
    with pytest.raises(
        driver.FailureLinkDriverError, match="does not map to the resolved review pass"
    ):
        driver._verify_receipt_index(
            tmp_path,
            records,
            resolution=resolution,
            bundle_manifest=bundle_manifest,
            cards=(_review_card(1),),
        )


def test_outer_freeze_rejects_symlinked_receipt_parent(tmp_path: Path) -> None:
    card = _phase_a_card(1)
    bundle_manifest = {"phase_a_card_set_sha256": "a" * 64}
    bundle_hash = driver._canonical_sha256(bundle_manifest)
    records = [
        _write_receipt_artifact(
            tmp_path,
            stage=stage,
            card=card,
            bundle_manifest_sha256=bundle_hash,
        )
        for stage in ("PRIMARY", "SECONDARY")
    ]
    expected = {"task_key": card["task_key"]}
    resolution = PhaseAResolution(
        manifest={
            "counts": {
                "primary_review_count": 1,
                "secondary_review_count": 1,
                "adjudication_review_count": 0,
            },
            "task_resolutions": [{"task_key": card["task_key"], "material_disagreement": False}],
        },
        primary_reviews=(expected,),
        secondary_reviews=(expected,),
        adjudication_reviews=(),
        final_reviews=(expected,),
    )
    receipt_parent = tmp_path / Path(records[0]["receipt_path"]).parent
    real_parent = tmp_path / "same-bytes-receipt-directory"
    receipt_parent.rename(real_parent)
    receipt_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(driver.FailureLinkDriverError, match="symlink artifact path is forbidden"):
        driver._verify_receipt_index(
            tmp_path,
            records,
            resolution=resolution,
            bundle_manifest=bundle_manifest,
            cards=(_review_card(1),),
        )


def test_phase_a_freeze_root_symlink_is_rejected_before_load(tmp_path: Path) -> None:
    real_root = tmp_path / "real-phase-a"
    real_root.mkdir()
    linked_root = tmp_path / "linked-phase-a"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(driver.FailureLinkDriverError, match="symlink artifact path is forbidden"):
        driver._verify_phase_a_driver_freeze(
            phase_a_root=linked_root,
            expected_freeze_sha256="a" * 64,
            phase_a_bundle=_phase_a_bundle(task_count=1),
            source_base=tmp_path,
            expected_task_count=1,
            expected_chain_count=1,
        )
