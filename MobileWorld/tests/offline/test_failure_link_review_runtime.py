from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mobile_world.offline.failure_link_prompt import (
    PROMPT_VERSION,
    build_adjudication_prompt,
    build_review_prompt,
)
from mobile_world.offline.failure_link_review_runtime import (
    MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS,
    REVIEWER_DISABLED_FEATURES,
    ReviewRetryExhausted,
    ReviewRuntimeError,
    ReviewUnit,
    execute_review_unit,
    sha256_bytes,
    write_once,
)
from mobile_world.offline.motivation_review import canonical_json_bytes


def _unit(*, image_paths: tuple[Path, ...] = ()) -> ReviewUnit:
    image_payloads = tuple(path.read_bytes() for path in image_paths)
    return ReviewUnit(
        phase="A",
        stage="PRIMARY",
        unit_id="phase-a-primary-0001",
        task_key="model/task",
        card_sha256="a" * 64,
        image_paths=image_paths,
        image_sha256s=tuple(sha256_bytes(payload) for payload in image_payloads),
        image_byte_lengths=tuple(len(payload) for payload in image_payloads),
    )


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {"answer": {"type": "string", "const": "OK"}},
        "required": ["answer"],
        "type": "object",
    }


def _prompt(feedback: str | None) -> str:
    return f"fixture prompt; feedback={feedback or 'NONE'}"


def _validator(value: Any) -> dict[str, Any]:
    if value != {"answer": "OK"}:
        raise ValueError("answer must be OK")
    return dict(value)


def _binding() -> dict[str, Any]:
    return {"causal_claim_supported": False, "prompt_version": PROMPT_VERSION}


def _output_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("-o") + 1])


def test_safe_argv_atomic_receipt_and_verified_resume(tmp_path: Path) -> None:
    image = tmp_path / "evidence.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "OK"}))
        return subprocess.CompletedProcess(argv, 0, stdout=b"ignored", stderr=b"")

    artifact = execute_review_unit(
        unit=_unit(image_paths=(image,)),
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex-fixture",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=2,
        timeout_seconds=30,
        resume=False,
        runner=runner,
    )
    assert artifact.resumed is False
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["codex-fixture", "exec", "--ephemeral"]
    assert ["-s", "read-only"] == argv[argv.index("-s") : argv.index("-s") + 2]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--strict-config" in argv
    assert argv[-1] == "-"
    assert argv[argv.index("--image") + 1] == str(image)
    for feature in REVIEWER_DISABLED_FEATURES:
        index = argv.index(feature)
        assert argv[index - 1] == "--disable"
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["cwd"] == Path(argv[argv.index("-C") + 1])
    receipt = json.loads((artifact.directory / "receipt.json").read_bytes())
    assert receipt["accepted_attempt"] == 1
    assert receipt["image_attachments"] == [
        {
            "attachment_index": 1,
            "blob_sha256": sha256_bytes(image.read_bytes()),
            "byte_length": image.stat().st_size,
        }
    ]
    assert receipt["receipt_binding"] == _binding()
    assert receipt["max_prompt_bytes"] == MAX_PROMPT_BYTES
    assert receipt["max_prompt_chars"] == MAX_PROMPT_CHARS
    assert receipt["attempts"][0]["stdout_byte_count"] == len(b"ignored")
    assert b"ignored" not in (artifact.directory / "receipt.json").read_bytes()

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resume must not invoke Codex")

    resumed = execute_review_unit(
        unit=_unit(image_paths=(image,)),
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex-fixture",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=2,
        timeout_seconds=30,
        resume=True,
        runner=forbidden_runner,
    )
    assert resumed.resumed is True
    assert resumed.receipt_sha256 == artifact.receipt_sha256


def test_invalid_response_retry_is_feedback_bound(tmp_path: Path) -> None:
    prompts: list[bytes] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        prompts.append(kwargs["input"])
        response = {"answer": "BAD"} if len(prompts) == 1 else {"answer": "OK"}
        _output_path(argv).write_bytes(canonical_json_bytes(response))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    artifact = execute_review_unit(
        unit=_unit(),
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=2,
        timeout_seconds=30,
        resume=False,
        runner=runner,
    )
    assert len(prompts) == 2
    assert prompts[0] != prompts[1]
    receipt = json.loads((artifact.directory / "receipt.json").read_bytes())
    assert receipt["attempts"][0]["error_kind"] == "invalid_response"
    assert receipt["attempts"][1]["validation_feedback_sha256"] is not None
    rejected = list((tmp_path / "review" / "rejected").rglob("*.json"))
    assert len(rejected) == 1


@pytest.mark.parametrize(
    ("prompt", "message"),
    [
        ("x" * (MAX_PROMPT_CHARS + 1), "character limit"),
        ("界" * (MAX_PROMPT_BYTES // 3 + 1), "byte limit"),
    ],
)
def test_prompt_limits_stop_before_runner_and_artifact_write(
    tmp_path: Path, prompt: str, message: str
) -> None:
    calls = 0

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("prompt preflight must precede subprocess")

    output = tmp_path / "review"
    with pytest.raises(ReviewRuntimeError, match=message):
        execute_review_unit(
            unit=_unit(),
            output_root=output,
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=lambda feedback: prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=False,
            runner=forbidden_runner,
        )
    assert calls == 0
    assert not output.exists()


def test_retry_feedback_prompt_is_rechecked_before_second_subprocess(tmp_path: Path) -> None:
    calls = 0
    base = "x" * (MAX_PROMPT_CHARS - 10)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "BAD"}))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(ReviewRuntimeError, match="character limit"):
        execute_review_unit(
            unit=_unit(),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=lambda feedback: base + (feedback or ""),
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=2,
            timeout_seconds=30,
            resume=False,
            runner=runner,
        )
    assert calls == 1


def test_resume_detects_response_and_receipt_tampering(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "OK"}))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    artifact = execute_review_unit(
        unit=_unit(),
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=1,
        timeout_seconds=30,
        resume=False,
        runner=runner,
    )
    response_path = artifact.directory / "response.json"
    original = response_path.read_bytes()
    response_path.write_bytes(canonical_json_bytes({"answer": "TAMPERED"}))
    with pytest.raises(ReviewRuntimeError, match="response hash mismatch"):
        execute_review_unit(
            unit=_unit(),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=True,
            runner=runner,
        )
    response_path.write_bytes(original)
    receipt_path = artifact.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["model"] = "tampered"
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(ReviewRuntimeError, match="receipt mismatch"):
        execute_review_unit(
            unit=_unit(),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=True,
            runner=runner,
        )


def test_resume_rehashes_and_rejects_mutated_image_attachment(tmp_path: Path) -> None:
    image = tmp_path / "evidence.png"
    image.write_bytes(b"original-image-bytes")
    unit = _unit(image_paths=(image,))

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "OK"}))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    execute_review_unit(
        unit=unit,
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=1,
        timeout_seconds=30,
        resume=False,
        runner=runner,
    )
    image.write_bytes(b"mutated00image0bytes")
    with pytest.raises(ReviewRuntimeError, match="image SHA-256 mismatch"):
        execute_review_unit(
            unit=unit,
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=True,
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("resume must reject attachment before subprocess")
            ),
        )


def test_attempt_rehashes_and_rejects_attachment_changed_during_runner(
    tmp_path: Path,
) -> None:
    image = tmp_path / "evidence.png"
    image.write_bytes(b"original-image-bytes")
    unit = _unit(image_paths=(image,))

    def mutating_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "OK"}))
        image.write_bytes(b"mutated00image0bytes")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(ReviewRuntimeError, match="image SHA-256 mismatch"):
        execute_review_unit(
            unit=unit,
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=False,
            runner=mutating_runner,
        )


def test_runtime_rejects_symlinked_image_attachment(tmp_path: Path) -> None:
    external = tmp_path / "external.png"
    external.write_bytes(b"image-bytes")
    linked = tmp_path / "linked.png"
    linked.symlink_to(external)
    with pytest.raises(ReviewRuntimeError, match="symlink artifact path is forbidden"):
        execute_review_unit(
            unit=_unit(image_paths=(linked,)),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=False,
            runner=lambda *args, **kwargs: None,
        )


@pytest.mark.parametrize(
    "artifact_name",
    ("receipt.json", "model_response.json", "response.json", "output_schema.json"),
)
def test_resume_rejects_symlinked_artifact(tmp_path: Path, artifact_name: str) -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        _output_path(argv).write_bytes(canonical_json_bytes({"answer": "OK"}))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    artifact = execute_review_unit(
        unit=_unit(),
        output_root=tmp_path / "review",
        model="gpt-fixture",
        reviewer_id="reviewer-primary",
        codex_bin="codex",
        schema=_schema(),
        render_prompt=_prompt,
        validate_response=_validator,
        receipt_binding=_binding(),
        max_attempts=1,
        timeout_seconds=30,
        resume=False,
        runner=runner,
    )
    artifact_path = artifact.directory / artifact_name
    external = tmp_path / f"same-bytes-{artifact_name}"
    external.write_bytes(artifact_path.read_bytes())
    artifact_path.unlink()
    artifact_path.symlink_to(external)

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("symlink rejection must precede a resumed subprocess")

    with pytest.raises(ReviewRuntimeError, match="symlink artifact path is forbidden"):
        execute_review_unit(
            unit=_unit(),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=1,
            timeout_seconds=30,
            resume=True,
            runner=forbidden_runner,
        )


def test_retry_exhaustion_preserves_failure_receipt(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 9, stdout=b"not stored", stderr=b"also hidden")

    with pytest.raises(ReviewRetryExhausted):
        execute_review_unit(
            unit=_unit(),
            output_root=tmp_path / "review",
            model="gpt-fixture",
            reviewer_id="reviewer-primary",
            codex_bin="codex",
            schema=_schema(),
            render_prompt=_prompt,
            validate_response=_validator,
            receipt_binding=_binding(),
            max_attempts=2,
            timeout_seconds=30,
            resume=False,
            runner=runner,
        )
    failures = list((tmp_path / "review" / "failures").rglob("*.json"))
    assert len(failures) == 1
    payload = failures[0].read_bytes()
    assert b"not stored" not in payload
    assert b"also hidden" not in payload
    receipt = json.loads(payload)
    assert len(receipt["attempts"]) == 2


def test_write_once_rejects_frozen_drift(tmp_path: Path) -> None:
    path = tmp_path / "frozen.json"
    write_once(path, b"one")
    write_once(path, b"one")
    with pytest.raises(ReviewRuntimeError, match="frozen artifact differs"):
        write_once(path, b"two")


def test_prompts_preserve_blinding_and_observational_boundary() -> None:
    identity = {"reviewer_id": "r", "task_key": "m/t"}
    card = {"task_key": "m/t", "outcome_blinded": True}
    schema = _schema()
    phase_a = build_review_prompt(
        phase="A",
        stage="PRIMARY",
        reviewer_id="r",
        identity=identity,
        card=card,
        schema=schema,
        attachment_map=[],
    )
    assert "outcome blind" in phase_a
    assert "Do not infer or guess the task score" in phase_a
    assert "affected_predicate" in phase_a
    assert "prefix" in phase_a
    assert "target_contribution" in phase_a
    assert "do not make a causal claim" in phase_a
    adjudication = build_adjudication_prompt(
        phase="B",
        reviewer_id="a",
        identity=identity,
        card={"task_key": "m/t", "outcome": {"outcome": "FAILURE"}},
        schema=schema,
        primary_review={"answer": "one"},
        secondary_review={"answer": "two"},
        material_disagreement={"paths": ["chains[0].failure_link_level"]},
        attachment_map=[],
    )
    assert "counterfactual causation" in adjudication
    assert "evaluator_revealed_alternatives" in adjudication
    assert "actual substring" in adjudication
    assert "score alone" in adjudication
    assert "no frozen Phase-A competing defect" in adjudication
    assert "do not choose a reviewer by identity" in adjudication
