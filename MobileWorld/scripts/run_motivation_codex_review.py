#!/usr/bin/env python3
"""Run resumable, outcome-blind Codex review over formal task cards.

Only compact, validated task cards and their card-local evidence images enter a
review prompt. PASS1 is frozen before the outcome sidecar is opened for the
formal stratified PASS2 selection. Outcome records and raw event streams are
never supplied to a reviewer or copied into this derived artifact tree.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_world.offline.motivation_prompt import (
    PREVIOUS_PROMPT_VERSION,
    PROMPT_VERSION,
    build_adjudication_prompt,
    build_previous_adjudication_prompt_v2,
    build_previous_review_prompt_v2,
    build_review_prompt,
    response_schema,
)
from mobile_world.offline.motivation_review import (
    DEFAULT_NEGATIVE_AUDIT_RATE,
    EXPECTED_TASK_COUNT,
    REVIEW_SCHEMA_VERSION,
    ReviewValidationError,
    adjudication_needed,
    canonical_json_bytes,
    canonical_sha256,
    compute_metrics,
    derive_task_screen_class,
    load_canonical_json_line,
    select_pass2,
    validate_primary_coverage,
    validate_review_batch,
    validate_task_cards,
)

DRIVER_SCHEMA_VERSION = "mobileworld.audit.motivation-codex-driver/v2"
LEGACY_DRIVER_SCHEMA_VERSION = "mobileworld.audit.motivation-codex-driver/v1"
LEGACY_PROMPT_VERSION = "mobileworld.audit.motivation-codex-prompt/v1"
SUPPORTED_SEED_PROMPT_VERSIONS = frozenset(
    {LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, PROMPT_VERSION}
)
DEFAULT_PRIMARY_MODEL = "gpt-5.6-terra"
DEFAULT_SECONDARY_MODEL = "gpt-5.6-sol"
DEFAULT_ADJUDICATOR_MODEL = "gpt-5.6-sol"
DEFAULT_PRIMARY_REVIEWER = "codex-primary-terra-v1"
DEFAULT_SECONDARY_REVIEWER = "codex-secondary-sol-v1"
DEFAULT_ADJUDICATOR_REVIEWER = "codex-adjudicator-sol-v1"
MAX_CARD_FILE_BYTES = 64 * 1024 * 1024
MAX_OUTCOME_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_PROMPT_BYTES = 8 * 1024 * 1024
DEFAULT_BATCH_SIZE = 1
MAX_STAGE_RETRY_EXHAUSTIONS = 3
_PASS1_BATCH_ID_RE = re.compile(r"^pass1-[0-9]{4}-c[0-9]{3}-c[0-9]{3}$")
REVIEWER_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
)


class ReviewDriverError(RuntimeError):
    """A deterministic input, process, or artifact invariant failed."""


class BatchRetryExhausted(ReviewDriverError):
    """One isolated model-review batch exhausted its finite retries."""


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    candidate_id: str
    ref_id: str
    role: str
    step: int
    blob_sha256: str
    path: Path

    def prompt_record(self, *, attachment_index: int) -> dict[str, Any]:
        return {
            "attachment_index": attachment_index,
            "blob_sha256": self.blob_sha256,
            "candidate_id": self.candidate_id,
            "ref_id": self.ref_id,
            "role": self.role,
            "step": self.step,
        }


@dataclass(frozen=True, slots=True)
class BlindCard:
    task_name: str
    catalog_index: int
    payload: dict[str, Any]
    card_sha256: str
    image_attachments: tuple[ImageAttachment, ...]

    @property
    def image_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(item.path for item in self.image_attachments))


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    phase: str
    batch_id: str
    cards: tuple[BlindCard, ...]


@dataclass(frozen=True, slots=True)
class StageArtifact:
    phase: str
    batch_id: str
    result: dict[str, Any]
    response_sha256: str
    receipt_sha256: str
    directory: Path
    resumed: bool


@dataclass(frozen=True, slots=True)
class SeedPass1Artifact:
    batch_id: str
    reviews: tuple[dict[str, Any], ...]
    response_sha256: str
    receipt_sha256: str
    schema_sha256: str
    task_names: tuple[str, ...]
    directory: Path
    prompt_version: str


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReviewDriverError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_strict_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewDriverError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewDriverError(f"JSON artifact must be an object: {path}")
    return value


def _write_file(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_once(path: Path, data: bytes) -> None:
    """Atomically create an immutable artifact, or verify identical bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ReviewDriverError(f"frozen artifact differs from requested bytes: {path}")
        return
    descriptor, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_text)
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ReviewDriverError(f"frozen artifact differs from requested bytes: {path}")
        else:
            _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_canonical_jsonl(path: Path, *, maximum_bytes: int) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise ReviewDriverError(f"JSONL input exceeds byte limit: {path}")
        data = path.read_bytes()
    except OSError as exc:
        raise ReviewDriverError(f"cannot read JSONL input {path}: {exc}") from exc
    if not data:
        raise ReviewDriverError(f"JSONL input is empty: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if line in {b"\n", b"\r\n"}:
            raise ReviewDriverError(f"blank JSONL line at {path}:{line_number}")
        try:
            records.append(load_canonical_json_line(line))
        except ValueError as exc:
            raise ReviewDriverError(
                f"non-canonical JSONL record at {path}:{line_number}: {exc}"
            ) from exc
    return records


def _supported_image(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError as exc:
        raise ReviewDriverError(f"cannot inspect candidate blob {path}: {exc}") from exc
    return bool(
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def _resolve_card_images(
    card: Mapping[str, Any],
    *,
    source_base: Path,
    digest_cache: dict[str, Path],
) -> tuple[ImageAttachment, ...]:
    source_root = source_base.resolve(strict=True)
    relative_text = card["task"]["source_relative_run_path"]
    relative = PurePosixPath(relative_text)
    run_root = (source_root / Path(*relative.parts)).resolve(strict=True)
    try:
        run_root.relative_to(source_root)
    except ValueError as exc:
        raise ReviewDriverError(
            f"source run escapes --source-base for {card['task']['task_name']}"
        ) from exc
    if not run_root.is_dir():
        raise ReviewDriverError(f"source run is not a directory: {run_root}")

    attachments: list[ImageAttachment] = []
    seen: set[tuple[str, str]] = set()
    for candidate in card["candidates"]:
        for evidence_ref in candidate["evidence_refs"]:
            digest = evidence_ref["blob_sha256"]
            if digest is None:
                continue
            cache_key = f"{run_root}\0{digest}"
            resolved = digest_cache.get(cache_key)
            if resolved is None:
                candidate_path = (run_root / "blobs" / "sha256" / digest[:2] / digest).resolve(
                    strict=True
                )
                try:
                    candidate_path.relative_to(run_root)
                except ValueError as exc:
                    raise ReviewDriverError(
                        f"candidate blob escapes source run: {candidate_path}"
                    ) from exc
                if not candidate_path.is_file():
                    raise ReviewDriverError(
                        f"candidate blob is not a regular file: {candidate_path}"
                    )
                if _file_sha256(candidate_path) != digest:
                    raise ReviewDriverError(f"candidate blob sha256 mismatch: {candidate_path}")
                resolved = candidate_path
                digest_cache[cache_key] = resolved
            if not _supported_image(resolved):
                continue
            identity = (candidate["candidate_id"], evidence_ref["ref_id"])
            if identity in seen:
                continue
            seen.add(identity)
            attachments.append(
                ImageAttachment(
                    candidate_id=candidate["candidate_id"],
                    ref_id=evidence_ref["ref_id"],
                    role=evidence_ref["role"],
                    step=evidence_ref["step"],
                    blob_sha256=digest,
                    path=resolved,
                )
            )
    return tuple(
        sorted(
            attachments,
            key=lambda item: (item.candidate_id, item.ref_id, str(item.path)),
        )
    )


def load_task_cards(
    cards_path: Path,
    *,
    source_base: Path,
    expected_count: int = EXPECTED_TASK_COUNT,
) -> tuple[BlindCard, ...]:
    """Load, formally validate, and enrich exactly 117 blind task cards."""

    if expected_count != EXPECTED_TASK_COUNT:
        raise ReviewDriverError(
            f"formal v1 review requires exactly {EXPECTED_TASK_COUNT} task cards"
        )
    records = _read_canonical_jsonl(cards_path, maximum_bytes=MAX_CARD_FILE_BYTES)
    keyed: dict[str, dict[str, Any]] = {}
    for card in records:
        task = card.get("task")
        task_name = task.get("task_name") if isinstance(task, Mapping) else None
        if not isinstance(task_name, str) or not task_name:
            raise ReviewDriverError("task card lacks task.task_name")
        if task_name in keyed:
            raise ReviewDriverError(f"duplicate task card: {task_name}")
        keyed[task_name] = card
    try:
        validated = validate_task_cards(keyed, expected_task_count=expected_count)
    except ValueError as exc:
        raise ReviewDriverError(f"formal task-card validation failed: {exc}") from exc

    try:
        source_base.resolve(strict=True)
    except OSError as exc:
        raise ReviewDriverError(f"cannot resolve --source-base {source_base}: {exc}") from exc
    digest_cache: dict[str, Path] = {}
    cards = [
        BlindCard(
            task_name=name,
            catalog_index=card["task"]["catalog_index"],
            payload=card,
            card_sha256=canonical_sha256(card),
            image_attachments=_resolve_card_images(
                card, source_base=source_base, digest_cache=digest_cache
            ),
        )
        for name, card in validated.items()
    ]
    return tuple(sorted(cards, key=lambda item: item.catalog_index))


def fixed_batches(
    cards: Sequence[BlindCard], batch_size: int, *, phase: str
) -> tuple[ReviewBatch, ...]:
    if batch_size <= 0:
        raise ReviewDriverError("batch_size must be positive")
    phase_slug = phase.lower().replace("_", "-")
    result: list[ReviewBatch] = []
    for offset in range(0, len(cards), batch_size):
        members = tuple(cards[offset : offset + batch_size])
        if not members:
            continue
        ordinal = len(result) + 1
        result.append(
            ReviewBatch(
                phase=phase,
                batch_id=(
                    f"{phase_slug}-{ordinal:04d}-"
                    f"c{members[0].catalog_index:03d}-c{members[-1].catalog_index:03d}"
                ),
                cards=members,
            )
        )
    return tuple(result)


def _identity(card: BlindCard, *, phase: str, reviewer_id: str) -> dict[str, Any]:
    phase_slug = phase.lower().replace("_", "-")
    return {
        "card_sha256": card.card_sha256,
        "catalog_index": card.catalog_index,
        "dataset_sha256": card.payload["dataset_sha256"],
        "evaluation_run_id": card.payload["evaluation_run_id"],
        "phase": phase,
        "record_type": "task_review",
        "review_id": (f"review-{phase_slug}-c{card.catalog_index:03d}-{card.card_sha256[:16]}"),
        "reviewer_id": reviewer_id,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "selection_sha256": card.payload["selection_sha256"],
        "task_name": card.task_name,
    }


def _review_case(
    card: BlindCard,
    *,
    phase: str,
    reviewer_id: str,
    attachment_indices: Mapping[Path, int],
) -> dict[str, Any]:
    return {
        "expected_review_identity": _identity(card, phase=phase, reviewer_id=reviewer_id),
        "resolved_candidate_images": [
            item.prompt_record(attachment_index=attachment_indices[item.path])
            for item in card.image_attachments
        ],
        "task_card": card.payload,
    }


def _legacy_review_case_v1(card: BlindCard, *, phase: str, reviewer_id: str) -> dict[str, Any]:
    """Reconstruct the frozen v1 case bytes used by an explicitly anchored seed."""

    return {
        "expected_review_identity": _identity(card, phase=phase, reviewer_id=reviewer_id),
        "resolved_candidate_images": [
            {
                "blob_sha256": item.blob_sha256,
                "candidate_id": item.candidate_id,
                "path": str(item.path),
                "ref_id": item.ref_id,
                "role": item.role,
                "step": item.step,
            }
            for item in card.image_attachments
        ],
        "task_card": card.payload,
    }


def _adjudication_case(
    card: BlindCard,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    reviewer_id: str,
    attachment_indices: Mapping[Path, int],
) -> dict[str, Any]:
    case = _review_case(
        card,
        phase="ADJUDICATION",
        reviewer_id=reviewer_id,
        attachment_indices=attachment_indices,
    )
    case["primary_review"] = dict(primary)
    case["secondary_review"] = dict(secondary)
    return case


def _common_identity(cards: Sequence[BlindCard]) -> dict[str, str]:
    if not cards:
        raise ReviewDriverError("review batch cannot be empty")
    first = cards[0].payload
    return {
        "dataset_sha256": first["dataset_sha256"],
        "evaluation_run_id": first["evaluation_run_id"],
        "selection_sha256": first["selection_sha256"],
    }


def _validate_stage_result(
    result: Mapping[str, Any],
    *,
    batch: ReviewBatch,
    all_cards_by_task: Mapping[str, Mapping[str, Any]],
    reviewer_id: str,
) -> tuple[dict[str, Any], ...]:
    if result.get("batch_id") != batch.batch_id:
        raise ReviewDriverError("Codex response batch_id mismatch")
    try:
        reviews = validate_review_batch(result, all_cards_by_task, batch.phase)
    except ValueError as exc:
        raise ReviewDriverError(f"formal review-batch validation failed: {exc}") from exc
    if len(reviews) != len(batch.cards):
        raise ReviewDriverError("Codex response review count mismatch")
    for card, review in zip(batch.cards, reviews, strict=True):
        expected = _identity(card, phase=batch.phase, reviewer_id=reviewer_id)
        for key, value in expected.items():
            if review.get(key) != value:
                raise ReviewDriverError(
                    f"Codex response identity mismatch for {card.task_name}: {key}"
                )
    return reviews


def _normalize_derived_review_fields(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Set mechanically derived fields while preserving the model JSON separately."""

    normalized = copy.deepcopy(dict(result))
    reviews = normalized.get("reviews")
    if not isinstance(reviews, list):
        return normalized, ()
    corrections: list[str] = []
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            continue
        try:
            expected = derive_task_screen_class(
                review.get("coverage_verdict"), review.get("chains")
            )
        except ValueError:
            continue
        if review.get("task_screen_class") != expected:
            review["task_screen_class"] = expected
            corrections.append(f"reviews[{index}].task_screen_class")
    return normalized, tuple(corrections)


def _validation_feedback(exc: BaseException) -> str:
    """Return bounded machine-readable feedback without adding outcome information."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ReviewValidationError):
            return json.dumps(
                {
                    "code": current.code,
                    "message": str(current),
                    "path": current.path,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )[:1000]
        current = current.__cause__
    return json.dumps(
        {
            "code": "review_driver_validation_error",
            "message": str(exc)[:800],
            "path": "$",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _phase_directory(phase: str) -> str:
    return phase.lower().replace("_", "-")


def _validate_resumed_attempt_chain(
    receipt: Mapping[str, Any],
    *,
    batch_id: str,
    render_prompt: Callable[[str | None], bytes],
    model_response: Mapping[str, Any],
    response_bytes: bytes,
) -> None:
    """Rebuild every recorded prompt and bind the accepted model response."""

    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ReviewDriverError(f"resume receipt has no attempt chain for {batch_id}")
    attempt_count = receipt.get("attempt_count")
    accepted_attempt = receipt.get("accepted_attempt")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count != len(attempts)
        or isinstance(accepted_attempt, bool)
        or not isinstance(accepted_attempt, int)
        or accepted_attempt != len(attempts)
    ):
        raise ReviewDriverError(f"resume attempt count mismatch for {batch_id}")

    validation_feedback: str | None = None
    for index, attempt_value in enumerate(attempts, start=1):
        if not isinstance(attempt_value, Mapping):
            raise ReviewDriverError(f"resume attempt record is invalid for {batch_id}")
        if attempt_value.get("attempt") != index:
            raise ReviewDriverError(f"resume attempt order mismatch for {batch_id}")
        expected_feedback_sha256 = (
            _sha256(validation_feedback.encode("utf-8"))
            if validation_feedback is not None
            else None
        )
        if attempt_value.get("validation_feedback_sha256") != expected_feedback_sha256:
            raise ReviewDriverError(f"resume feedback chain mismatch for {batch_id}")
        expected_prompt_sha256 = _sha256(render_prompt(validation_feedback))
        if attempt_value.get("prompt_sha256") != expected_prompt_sha256:
            raise ReviewDriverError(f"resume attempt prompt mismatch for {batch_id}")

        error_kind = attempt_value.get("error_kind")
        is_accepted = index == len(attempts)
        if is_accepted:
            if error_kind is not None:
                raise ReviewDriverError(f"resume accepted attempt is not successful for {batch_id}")
        elif not isinstance(error_kind, str) or not error_kind:
            raise ReviewDriverError(f"resume pre-accept attempt lacks an error for {batch_id}")
        if error_kind == "invalid_response":
            error_detail = attempt_value.get("error_detail")
            if not isinstance(error_detail, str) or not error_detail:
                raise ReviewDriverError(
                    f"resume invalid-response feedback is missing for {batch_id}"
                )
            validation_feedback = error_detail

    accepted_record = attempts[-1]
    accepted_prompt_sha256 = accepted_record.get("prompt_sha256")
    if receipt.get("accepted_prompt_sha256") != accepted_prompt_sha256:
        raise ReviewDriverError(f"resume accepted prompt mismatch for {batch_id}")
    if accepted_record.get("model_response_sha256") != receipt.get("model_response_sha256"):
        raise ReviewDriverError(f"resume accepted model response mismatch for {batch_id}")

    normalized_response, corrections = _normalize_derived_review_fields(model_response)
    if canonical_json_bytes(normalized_response) != response_bytes:
        raise ReviewDriverError(f"resume normalized response mismatch for {batch_id}")
    if accepted_record.get("derived_field_corrections") != list(corrections):
        raise ReviewDriverError(f"resume derived-field corrections mismatch for {batch_id}")


def _resume_artifact(
    *,
    batch: ReviewBatch,
    target: Path,
    expected_receipt: Mapping[str, Any],
    schema_bytes: bytes,
    render_prompt: Callable[[str | None], bytes],
    all_cards_by_task: Mapping[str, Mapping[str, Any]],
    reviewer_id: str,
) -> StageArtifact:
    receipt_path = target / "receipt.json"
    model_response_path = target / "model_response.json"
    response_path = target / "response.json"
    schema_path = target / "output_schema.json"
    if not (
        receipt_path.is_file()
        and model_response_path.is_file()
        and response_path.is_file()
        and schema_path.is_file()
    ):
        raise ReviewDriverError(f"incomplete resumable batch artifact: {target}")
    receipt = _read_json_object(receipt_path)
    for key, value in expected_receipt.items():
        if receipt.get(key) != value:
            raise ReviewDriverError(f"resume receipt mismatch for {batch.batch_id}: {key}")
    if schema_path.read_bytes() != schema_bytes:
        raise ReviewDriverError(f"resume schema mismatch for {batch.batch_id}")
    response_bytes = response_path.read_bytes()
    if _sha256(response_bytes) != receipt.get("response_sha256"):
        raise ReviewDriverError(f"resume response sha256 mismatch for {batch.batch_id}")
    model_response_bytes = model_response_path.read_bytes()
    if _sha256(model_response_bytes) != receipt.get("model_response_sha256"):
        raise ReviewDriverError(f"resume model response sha256 mismatch for {batch.batch_id}")
    model_response = _read_json_object(model_response_path)
    if canonical_json_bytes(model_response) != model_response_bytes:
        raise ReviewDriverError(f"resume model response is not canonical for {batch.batch_id}")
    result = _read_json_object(response_path)
    if canonical_json_bytes(result) != response_bytes:
        raise ReviewDriverError(f"resume response is not canonical for {batch.batch_id}")
    _validate_resumed_attempt_chain(
        receipt,
        batch_id=batch.batch_id,
        render_prompt=render_prompt,
        model_response=model_response,
        response_bytes=response_bytes,
    )
    _validate_stage_result(
        result,
        batch=batch,
        all_cards_by_task=all_cards_by_task,
        reviewer_id=reviewer_id,
    )
    return StageArtifact(
        phase=batch.phase,
        batch_id=batch.batch_id,
        result=result,
        response_sha256=receipt["response_sha256"],
        receipt_sha256=_file_sha256(receipt_path),
        directory=target,
        resumed=True,
    )


def _load_seed_pass1_artifact(
    directory: Path,
    *,
    expected_receipt_sha256: str,
    cards_by_name: Mapping[str, BlindCard],
    all_cards_by_task: Mapping[str, Mapping[str, Any]],
    model: str,
    reviewer_id: str,
) -> SeedPass1Artifact:
    """Load one hash-bound legacy PASS1 batch without rewriting its artifacts."""

    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise ReviewDriverError(f"cannot resolve PASS1 seed batch {directory}: {exc}") from exc
    if not resolved.is_dir() or directory.is_symlink():
        raise ReviewDriverError(f"PASS1 seed batch must be a physical directory: {directory}")
    receipt_path = resolved / "receipt.json"
    model_response_path = resolved / "model_response.json"
    response_path = resolved / "response.json"
    schema_path = resolved / "output_schema.json"
    for path in (receipt_path, response_path, schema_path):
        if not path.is_file() or path.is_symlink():
            raise ReviewDriverError(f"PASS1 seed artifact missing or symbolic: {path}")
    receipt_sha256 = _file_sha256(receipt_path)
    if receipt_sha256 != expected_receipt_sha256:
        raise ReviewDriverError(f"PASS1 seed receipt anchor mismatch: {resolved}")
    receipt = _read_json_object(receipt_path)
    if receipt.get("driver_schema_version") not in {
        LEGACY_DRIVER_SCHEMA_VERSION,
        DRIVER_SCHEMA_VERSION,
    }:
        raise ReviewDriverError(f"unsupported PASS1 seed driver version: {resolved}")
    prompt_version = receipt.get("prompt_version")
    if prompt_version not in SUPPORTED_SEED_PROMPT_VERSIONS:
        raise ReviewDriverError(f"unsupported PASS1 seed prompt version: {resolved}")
    if prompt_version != LEGACY_PROMPT_VERSION and (
        not model_response_path.is_file() or model_response_path.is_symlink()
    ):
        raise ReviewDriverError(f"PASS1 seed model response missing or symbolic: {resolved}")
    if receipt.get("phase") != "PASS1":
        raise ReviewDriverError(f"PASS1 seed has the wrong phase: {resolved}")
    if receipt.get("model") != model or receipt.get("reviewer_id") != reviewer_id:
        raise ReviewDriverError(f"PASS1 seed reviewer configuration mismatch: {resolved}")
    task_names = receipt.get("task_names")
    if (
        not isinstance(task_names, list)
        or not task_names
        or any(not isinstance(name, str) or not name for name in task_names)
        or len(task_names) != len(set(task_names))
    ):
        raise ReviewDriverError(f"PASS1 seed task_names are invalid: {resolved}")
    try:
        seed_cards = tuple(cards_by_name[name] for name in task_names)
    except KeyError as exc:
        raise ReviewDriverError(f"PASS1 seed references an unknown task: {exc.args[0]}") from exc
    if list(seed_cards) != sorted(seed_cards, key=lambda card: card.catalog_index):
        raise ReviewDriverError(f"PASS1 seed task order is not canonical: {resolved}")
    batch_id = receipt.get("batch_id")
    if not isinstance(batch_id, str) or _PASS1_BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ReviewDriverError(f"PASS1 seed batch_id is invalid: {resolved}")
    if prompt_version == LEGACY_PROMPT_VERSION:
        cases = [
            _legacy_review_case_v1(card, phase="PASS1", reviewer_id=reviewer_id)
            for card in seed_cards
        ]
    else:
        image_paths = tuple(dict.fromkeys(path for card in seed_cards for path in card.image_paths))
        attachment_indices = {path: index for index, path in enumerate(image_paths, start=1)}
        cases = [
            _review_case(
                card,
                phase="PASS1",
                reviewer_id=reviewer_id,
                attachment_indices=attachment_indices,
            )
            for card in seed_cards
        ]
    if receipt.get("input_sha256") != _sha256(canonical_json_bytes(cases)):
        raise ReviewDriverError(f"PASS1 seed input hash mismatch: {resolved}")
    schema_bytes = schema_path.read_bytes()
    if receipt.get("schema_sha256") != _sha256(schema_bytes):
        raise ReviewDriverError(f"PASS1 seed schema hash mismatch: {resolved}")
    if prompt_version != LEGACY_PROMPT_VERSION:
        prompt_builder = (
            build_previous_review_prompt_v2
            if prompt_version == PREVIOUS_PROMPT_VERSION
            else build_review_prompt
        )

        def render_seed_prompt(validation_feedback: str | None) -> bytes:
            return prompt_builder(
                phase="PASS1",
                batch_id=batch_id,
                reviewer_id=reviewer_id,
                cases=cases,
                validation_feedback=validation_feedback,
            ).encode("utf-8")

        if receipt.get("base_prompt_sha256") != _sha256(render_seed_prompt(None)):
            raise ReviewDriverError(f"PASS1 seed base prompt hash mismatch: {resolved}")
        expected_schema_bytes = canonical_json_bytes(
            response_schema(
                phase="PASS1",
                batch_id=batch_id,
                expected_count=len(seed_cards),
                identity=_common_identity(seed_cards),
                reviewer_id=reviewer_id,
            )
        )
        if schema_bytes != expected_schema_bytes:
            raise ReviewDriverError(f"PASS1 seed output schema mismatch: {resolved}")
    response_bytes = response_path.read_bytes()
    if receipt.get("response_sha256") != _sha256(response_bytes):
        raise ReviewDriverError(f"PASS1 seed response hash mismatch: {resolved}")
    response = _read_json_object(response_path)
    if canonical_json_bytes(response) != response_bytes:
        raise ReviewDriverError(f"PASS1 seed response is not canonical: {resolved}")
    if prompt_version != LEGACY_PROMPT_VERSION:
        model_response_bytes = model_response_path.read_bytes()
        if receipt.get("model_response_sha256") != _sha256(model_response_bytes):
            raise ReviewDriverError(f"PASS1 seed model response hash mismatch: {resolved}")
        model_response = _read_json_object(model_response_path)
        if canonical_json_bytes(model_response) != model_response_bytes:
            raise ReviewDriverError(f"PASS1 seed model response is not canonical: {resolved}")
        _validate_resumed_attempt_chain(
            receipt,
            batch_id=batch_id,
            render_prompt=render_seed_prompt,
            model_response=model_response,
            response_bytes=response_bytes,
        )
    reviews = _validate_stage_result(
        response,
        batch=ReviewBatch(phase="PASS1", batch_id=batch_id, cards=seed_cards),
        all_cards_by_task=all_cards_by_task,
        reviewer_id=reviewer_id,
    )
    return SeedPass1Artifact(
        batch_id=batch_id,
        reviews=reviews,
        response_sha256=_sha256(response_bytes),
        receipt_sha256=receipt_sha256,
        schema_sha256=_sha256(schema_bytes),
        task_names=tuple(task_names),
        directory=resolved,
        prompt_version=prompt_version,
    )


def load_seed_pass1_artifacts(
    directories: Sequence[Path],
    *,
    expected_receipt_sha256s: Sequence[str],
    cards: Sequence[BlindCard],
    all_cards_by_task: Mapping[str, Mapping[str, Any]],
    model: str,
    reviewer_id: str,
) -> tuple[SeedPass1Artifact, ...]:
    cards_by_name = {card.task_name: card for card in cards}
    artifacts: list[SeedPass1Artifact] = []
    seen_tasks: set[str] = set()
    seen_directories: set[Path] = set()
    if len(directories) != len(expected_receipt_sha256s):
        raise ReviewDriverError(
            "each PASS1 seed batch requires one explicit receipt SHA-256 anchor"
        )
    for directory, expected_receipt_sha256 in zip(
        directories, expected_receipt_sha256s, strict=True
    ):
        if len(expected_receipt_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_receipt_sha256
        ):
            raise ReviewDriverError("PASS1 seed receipt anchor must be lowercase SHA-256")
        artifact = _load_seed_pass1_artifact(
            directory,
            expected_receipt_sha256=expected_receipt_sha256,
            cards_by_name=cards_by_name,
            all_cards_by_task=all_cards_by_task,
            model=model,
            reviewer_id=reviewer_id,
        )
        if artifact.directory in seen_directories:
            raise ReviewDriverError(f"PASS1 seed batch is duplicated: {artifact.directory}")
        seen_directories.add(artifact.directory)
        names = {review["task_name"] for review in artifact.reviews}
        overlap = seen_tasks & names
        if overlap:
            raise ReviewDriverError(f"PASS1 seed tasks are duplicated: {sorted(overlap)}")
        seen_tasks.update(names)
        artifacts.append(artifact)
    return tuple(artifacts)


def _seed_manifest_record(artifact: SeedPass1Artifact) -> dict[str, Any]:
    return {
        "batch_id": artifact.batch_id,
        "prompt_version": artifact.prompt_version,
        "response_sha256": artifact.response_sha256,
        "receipt_sha256": artifact.receipt_sha256,
        "schema_sha256": artifact.schema_sha256,
        "source_directory": str(artifact.directory),
        "task_names": list(artifact.task_names),
    }


def _install_seed_pass1_artifact(output_root: Path, artifact: SeedPass1Artifact) -> dict[str, Any]:
    """Copy a small verified legacy batch into the new derived review root."""

    source_files = {
        "legacy_output_schema.json": artifact.directory / "output_schema.json",
        "legacy_receipt.json": artifact.directory / "receipt.json",
        "legacy_response.json": artifact.directory / "response.json",
    }
    expected_hashes = {
        "legacy_output_schema.json": artifact.schema_sha256,
        "legacy_receipt.json": artifact.receipt_sha256,
        "legacy_response.json": artifact.response_sha256,
    }
    copied_bytes: dict[str, bytes] = {}
    for name, source in source_files.items():
        data = source.read_bytes()
        if _sha256(data) != expected_hashes[name]:
            raise ReviewDriverError(f"PASS1 seed changed while importing: {source}")
        copied_bytes[name] = data
    import_record = {
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "import_kind": "validated_legacy_pass1_batch",
        **_seed_manifest_record(artifact),
    }
    import_bytes = canonical_json_bytes(import_record)
    import_root = (output_root / "imports" / "pass1").resolve(strict=False)
    target = (import_root / artifact.batch_id).resolve(strict=False)
    try:
        target.relative_to(import_root)
    except ValueError as exc:
        raise ReviewDriverError("PASS1 import target escapes output root") from exc
    if target.exists():
        expected = {**copied_bytes, "import_receipt.json": import_bytes}
        for name, data in expected.items():
            path = target / name
            if not path.is_file() or path.read_bytes() != data:
                raise ReviewDriverError(f"existing PASS1 import differs: {target}")
        return import_record
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact.batch_id}.tmp-", dir=target.parent))
    try:
        for name, data in copied_bytes.items():
            _write_file(temporary / name, data)
        _write_file(temporary / "import_receipt.json", import_bytes)
        _fsync_directory(temporary)
        os.rename(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return import_record


def _attempt_record(
    *,
    attempt: int,
    returncode: int | None,
    stdout: bytes,
    stderr: bytes,
    elapsed_seconds: float,
    error_kind: str | None,
    error_detail: str | None = None,
    prompt_sha256: str,
    validation_feedback_sha256: str | None,
    derived_field_corrections: Sequence[str] = (),
    model_response_sha256: str | None = None,
    rejected_model_response_path: str | None = None,
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "elapsed_milliseconds": round(elapsed_seconds * 1000),
        "error_kind": error_kind,
        "error_detail": error_detail,
        "derived_field_corrections": list(derived_field_corrections),
        "model_response_sha256": model_response_sha256,
        "prompt_sha256": prompt_sha256,
        "rejected_model_response_path": rejected_model_response_path,
        "returncode": returncode,
        "stderr_byte_count": len(stderr),
        "stderr_sha256": _sha256(stderr),
        "stdout_byte_count": len(stdout),
        "stdout_sha256": _sha256(stdout),
        "validation_feedback_sha256": validation_feedback_sha256,
    }


def execute_stage_batch(
    *,
    batch: ReviewBatch,
    all_cards_by_task: Mapping[str, Mapping[str, Any]],
    output_root: Path,
    model: str,
    reviewer_id: str,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    resume: bool,
    prompt_version: str = PROMPT_VERSION,
    runner: Runner = subprocess.run,
    primary_by_task: Mapping[str, Mapping[str, Any]] | None = None,
    secondary_by_task: Mapping[str, Mapping[str, Any]] | None = None,
) -> StageArtifact:
    """Execute one formal batch and commit response/schema/receipt atomically."""

    if max_attempts <= 0 or timeout_seconds <= 0:
        raise ReviewDriverError("attempt and timeout limits must be positive")
    if prompt_version not in {PREVIOUS_PROMPT_VERSION, PROMPT_VERSION}:
        raise ReviewDriverError(f"unsupported executable prompt version: {prompt_version}")
    batch_image_paths = tuple(
        dict.fromkeys(path for card in batch.cards for path in card.image_paths)
    )
    attachment_indices = {path: index for index, path in enumerate(batch_image_paths, start=1)}
    if batch.phase == "ADJUDICATION":
        if primary_by_task is None or secondary_by_task is None:
            raise ReviewDriverError("adjudication requires both independent reviews")
        cases = [
            _adjudication_case(
                card,
                primary_by_task[card.task_name],
                secondary_by_task[card.task_name],
                reviewer_id=reviewer_id,
                attachment_indices=attachment_indices,
            )
            for card in batch.cards
        ]
    else:
        cases = [
            _review_case(
                card,
                phase=batch.phase,
                reviewer_id=reviewer_id,
                attachment_indices=attachment_indices,
            )
            for card in batch.cards
        ]

    def render_prompt(
        validation_feedback: str | None,
    ) -> bytes:
        if prompt_version == PROMPT_VERSION:
            review_builder = build_review_prompt
            adjudication_builder = build_adjudication_prompt
        elif prompt_version == PREVIOUS_PROMPT_VERSION:
            review_builder = build_previous_review_prompt_v2
            adjudication_builder = build_previous_adjudication_prompt_v2
        else:  # Internal callers pass one of the two frozen builders above.
            raise ReviewDriverError(f"unsupported prompt renderer: {prompt_version}")
        if batch.phase == "ADJUDICATION":
            prompt = adjudication_builder(
                batch_id=batch.batch_id,
                reviewer_id=reviewer_id,
                cases=cases,
                validation_feedback=validation_feedback,
            )
        else:
            prompt = review_builder(
                phase=batch.phase,
                batch_id=batch.batch_id,
                reviewer_id=reviewer_id,
                cases=cases,
                validation_feedback=validation_feedback,
            )
        return prompt.encode("utf-8")

    base_prompt_bytes = render_prompt(None)
    if len(base_prompt_bytes) > MAX_BATCH_PROMPT_BYTES:
        raise ReviewDriverError(f"batch prompt exceeds byte limit: {batch.batch_id}")
    identity = _common_identity(batch.cards)
    schema = response_schema(
        phase=batch.phase,
        batch_id=batch.batch_id,
        expected_count=len(batch.cards),
        identity=identity,
        reviewer_id=reviewer_id,
    )
    schema_bytes = canonical_json_bytes(schema)
    input_sha = _sha256(canonical_json_bytes(cases))

    def expected_receipt_for(prompt_version: str, prompt_bytes: bytes) -> dict[str, Any]:
        return {
            "batch_id": batch.batch_id,
            "base_prompt_sha256": _sha256(prompt_bytes),
            "codex_bin": codex_bin,
            "driver_schema_version": DRIVER_SCHEMA_VERSION,
            "input_sha256": input_sha,
            "max_attempts": max_attempts,
            "max_stage_retry_exhaustions": MAX_STAGE_RETRY_EXHAUSTIONS,
            "model": model,
            "phase": batch.phase,
            "prompt_version": prompt_version,
            "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
            "reviewer_id": reviewer_id,
            "schema_sha256": _sha256(schema_bytes),
            "task_names": [card.task_name for card in batch.cards],
            "timeout_seconds": timeout_seconds,
        }

    expected_receipt = expected_receipt_for(prompt_version, base_prompt_bytes)
    stage_root = output_root / "batches" / _phase_directory(batch.phase)
    target = stage_root / batch.batch_id
    if target.exists():
        if not resume:
            raise ReviewDriverError(f"batch artifact already exists: {target}")
        return _resume_artifact(
            batch=batch,
            target=target,
            expected_receipt=expected_receipt,
            schema_bytes=schema_bytes,
            render_prompt=render_prompt,
            all_cards_by_task=all_cards_by_task,
            reviewer_id=reviewer_id,
        )

    stage_root.mkdir(parents=True, exist_ok=True)
    attempt_records: list[dict[str, Any]] = []
    validated_result: dict[str, Any] | None = None
    accepted_model_result: dict[str, Any] | None = None
    accepted_prompt_sha256: str | None = None
    validation_feedback: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt_bytes = render_prompt(validation_feedback)
        if len(prompt_bytes) > MAX_BATCH_PROMPT_BYTES:
            raise ReviewDriverError(f"retry prompt exceeds byte limit: {batch.batch_id}")
        prompt_sha256 = _sha256(prompt_bytes)
        feedback_sha256 = (
            _sha256(validation_feedback.encode("utf-8"))
            if validation_feedback is not None
            else None
        )
        stdout = b""
        stderr = b""
        returncode: int | None = None
        error_kind: str | None = None
        error_detail: str | None = None
        derived_field_corrections: tuple[str, ...] = ()
        model_response_sha256: str | None = None
        model_response_bytes: bytes | None = None
        rejected_model_response_path: str | None = None
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="motivation-codex-") as temporary_text:
            temporary = Path(temporary_text)
            schema_path = temporary / "output_schema.json"
            output_path = temporary / "response.json"
            schema_path.write_bytes(schema_bytes)
            argv = [
                codex_bin,
                "exec",
                "--ephemeral",
                "-s",
                "read-only",
                "-m",
                model,
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--strict-config",
                "--color",
                "never",
                "-C",
                str(temporary),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
            ]
            for feature in REVIEWER_DISABLED_FEATURES:
                argv.extend(["--disable", feature])
            for image_path in batch_image_paths:
                argv.extend(["--image", str(image_path)])
            argv.append("-")
            try:
                completed = runner(
                    argv,
                    input=prompt_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=temporary,
                    shell=False,
                    check=False,
                    timeout=timeout_seconds,
                )
                stdout = completed.stdout or b""
                stderr = completed.stderr or b""
                returncode = completed.returncode
                if returncode != 0:
                    error_kind = "nonzero_exit"
                elif not output_path.is_file():
                    error_kind = "missing_output"
                else:
                    try:
                        model_candidate = _read_json_object(output_path)
                        model_response_bytes = canonical_json_bytes(model_candidate)
                        model_response_sha256 = _sha256(model_response_bytes)
                        candidate, derived_field_corrections = _normalize_derived_review_fields(
                            model_candidate
                        )
                        _validate_stage_result(
                            candidate,
                            batch=batch,
                            all_cards_by_task=all_cards_by_task,
                            reviewer_id=reviewer_id,
                        )
                        validated_result = candidate
                        accepted_model_result = model_candidate
                        accepted_prompt_sha256 = prompt_sha256
                    except ReviewDriverError as exc:
                        error_kind = "invalid_response"
                        error_detail = _validation_feedback(exc)
                        validation_feedback = error_detail
                    except ValueError as exc:
                        error_kind = "invalid_response"
                        error_detail = _validation_feedback(exc)
                        validation_feedback = error_detail
                    if error_kind == "invalid_response" and model_response_bytes is not None:
                        rejected_path = (
                            output_root
                            / "rejected"
                            / _phase_directory(batch.phase)
                            / batch.batch_id
                            / f"attempt-{attempt:02d}-{model_response_sha256}.json"
                        )
                        _write_once(rejected_path, model_response_bytes)
                        rejected_model_response_path = rejected_path.relative_to(
                            output_root
                        ).as_posix()
            except subprocess.TimeoutExpired as exc:
                error_kind = "timeout"
                stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
                stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            except OSError:
                error_kind = "process_error"
        attempt_records.append(
            _attempt_record(
                attempt=attempt,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                elapsed_seconds=time.monotonic() - started,
                error_kind=error_kind,
                error_detail=error_detail,
                prompt_sha256=prompt_sha256,
                validation_feedback_sha256=feedback_sha256,
                derived_field_corrections=derived_field_corrections,
                model_response_sha256=model_response_sha256,
                rejected_model_response_path=rejected_model_response_path,
            )
        )
        if validated_result is not None:
            break

    if validated_result is None:
        failure = {
            **expected_receipt,
            "attempts": attempt_records,
            "failure": "retry_limit_exhausted",
        }
        failure_bytes = canonical_json_bytes(failure)
        failure_sha256 = _sha256(failure_bytes)
        _write_once(
            output_root
            / "failures"
            / _phase_directory(batch.phase)
            / batch.batch_id
            / f"{failure_sha256}.json",
            failure_bytes,
        )
        raise BatchRetryExhausted(f"Codex batch failed after finite retries: {batch.batch_id}")

    if accepted_model_result is None or accepted_prompt_sha256 is None:
        raise ReviewDriverError(f"accepted response provenance missing: {batch.batch_id}")
    model_response_bytes = canonical_json_bytes(accepted_model_result)
    response_bytes = canonical_json_bytes(validated_result)
    receipt = {
        **expected_receipt,
        "accepted_attempt": len(attempt_records),
        "accepted_prompt_sha256": accepted_prompt_sha256,
        "attempt_count": len(attempt_records),
        "attempts": attempt_records,
        "model_response_sha256": _sha256(model_response_bytes),
        "response_sha256": _sha256(response_bytes),
    }
    receipt_bytes = canonical_json_bytes(receipt)
    temporary_target = Path(tempfile.mkdtemp(prefix=f".{batch.batch_id}.tmp-", dir=stage_root))
    try:
        _write_file(temporary_target / "model_response.json", model_response_bytes)
        _write_file(temporary_target / "output_schema.json", schema_bytes)
        _write_file(temporary_target / "response.json", response_bytes)
        _write_file(temporary_target / "receipt.json", receipt_bytes)
        _fsync_directory(temporary_target)
        os.rename(temporary_target, target)
        _fsync_directory(stage_root)
    finally:
        if temporary_target.exists():
            shutil.rmtree(temporary_target)
    return StageArtifact(
        phase=batch.phase,
        batch_id=batch.batch_id,
        result=validated_result,
        response_sha256=_sha256(response_bytes),
        receipt_sha256=_sha256(receipt_bytes),
        directory=target,
        resumed=False,
    )


def _reviews_from_artifacts(
    artifacts: Sequence[StageArtifact],
) -> list[dict[str, Any]]:
    return [review for artifact in artifacts for review in artifact.result["reviews"]]


def _execute_batches_resiliently(
    batches: Sequence[ReviewBatch],
    *,
    output_root: Path,
    execute: Callable[[ReviewBatch], StageArtifact],
) -> list[StageArtifact]:
    """Preserve later singleton work while bounding systemic retry burn."""

    artifacts: list[StageArtifact] = []
    exhausted: list[dict[str, str]] = []
    for batch in batches:
        try:
            artifacts.append(execute(batch))
        except BatchRetryExhausted as exc:
            exhausted.append({"batch_id": batch.batch_id, "error": str(exc)})
            if len(exhausted) >= MAX_STAGE_RETRY_EXHAUSTIONS:
                break
    if not exhausted:
        return artifacts
    phase = batches[0].phase if batches else "unknown"
    failure_summary = {
        "completed_batch_ids": [artifact.batch_id for artifact in artifacts],
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "failed_batches": exhausted,
        "halted_at_failure_limit": len(exhausted) >= MAX_STAGE_RETRY_EXHAUSTIONS,
        "phase": phase,
        "stage_failure_limit": MAX_STAGE_RETRY_EXHAUSTIONS,
    }
    summary_bytes = canonical_json_bytes(failure_summary)
    _write_once(
        output_root
        / "failures"
        / _phase_directory(phase)
        / "stage"
        / f"{_sha256(summary_bytes)}.json",
        summary_bytes,
    )
    raise ReviewDriverError(
        f"{phase} has {len(exhausted)} retry-exhausted batch(es); "
        "completed singleton artifacts were preserved for --resume"
    )


def _load_outcomes(
    path: Path, cards_by_task: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    records = _read_canonical_jsonl(path, maximum_bytes=MAX_OUTCOME_FILE_BYTES)
    outcomes: dict[str, dict[str, Any]] = {}
    for record in records:
        task_name = record.get("task_name")
        if not isinstance(task_name, str) or not task_name:
            raise ReviewDriverError("outcome record lacks task_name")
        if task_name in outcomes:
            raise ReviewDriverError(f"duplicate outcome record: {task_name}")
        outcomes[task_name] = record
    if set(outcomes) != set(cards_by_task):
        raise ReviewDriverError("outcome sidecar does not cover exact task-card names")
    return outcomes


def _manifest(
    *,
    cards_path: Path,
    cards_file_sha256: str,
    source_base: Path,
    batch_size: int,
    primary_model: str,
    secondary_model: str,
    adjudicator_model: str,
    primary_reviewer_id: str,
    secondary_reviewer_id: str,
    adjudicator_reviewer_id: str,
    negative_rate: float,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    pass1_seed_batches: Sequence[Mapping[str, Any]],
    prompt_version: str,
) -> dict[str, Any]:
    return {
        "adjudicator_model": adjudicator_model,
        "adjudicator_reviewer_id": adjudicator_reviewer_id,
        "batch_size": batch_size,
        "cards_file": str(cards_path.resolve()),
        "cards_file_sha256": cards_file_sha256,
        "codex_bin": codex_bin,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "max_attempts": max_attempts,
        "max_stage_retry_exhaustions": MAX_STAGE_RETRY_EXHAUSTIONS,
        "negative_audit_rate": negative_rate,
        "pass1_seed_batches": list(pass1_seed_batches),
        "primary_model": primary_model,
        "primary_reviewer_id": primary_reviewer_id,
        "prompt_version": prompt_version,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "secondary_model": secondary_model,
        "secondary_reviewer_id": secondary_reviewer_id,
        "source_base": str(source_base.resolve()),
        "task_count": EXPECTED_TASK_COUNT,
        "timeout_seconds": timeout_seconds,
    }


def _resolve_run_prompt_version(output_root: Path, *, resume: bool) -> str:
    """Keep an existing run on its frozen prompt while fresh runs use v3."""

    if not resume or not output_root.exists():
        return PROMPT_VERSION
    manifest_path = output_root / "run_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReviewDriverError("resumable review root lacks a physical run_manifest.json")
    prompt_version = _read_json_object(manifest_path).get("prompt_version")
    if prompt_version not in {PREVIOUS_PROMPT_VERSION, PROMPT_VERSION}:
        raise ReviewDriverError(
            "existing review prompt version is not directly resumable; "
            "import its hash-bound PASS1 batches into a fresh run"
        )
    return prompt_version


def _ensure_output_scope(output_root: Path, source_base: Path, cards: Sequence[BlindCard]) -> None:
    proposed = output_root.resolve(strict=False)
    source_root = source_base.resolve(strict=True)
    for card in cards:
        relative = PurePosixPath(card.payload["task"]["source_relative_run_path"])
        run_root = (source_root / Path(*relative.parts)).resolve(strict=True)
        try:
            proposed.relative_to(run_root)
        except ValueError:
            continue
        raise ReviewDriverError("derived review output must stay outside every raw source run")


def _freeze_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> str:
    data = b"".join(canonical_json_bytes(record) for record in records)
    _write_once(path, data)
    return _sha256(data)


def _screen_counts(reviews: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(review["task_screen_class"] for review in reviews)
    return {name.lower(): counts[name] for name in ("POSITIVE", "UNCERTAIN", "NEGATIVE")}


def run_review(
    *,
    cards_path: Path,
    outcomes_path: Path,
    source_base: Path,
    output_root: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pass1_seed_batch_dirs: Sequence[Path] = (),
    pass1_seed_receipt_sha256s: Sequence[str] = (),
    primary_model: str = DEFAULT_PRIMARY_MODEL,
    secondary_model: str = DEFAULT_SECONDARY_MODEL,
    adjudicator_model: str = DEFAULT_ADJUDICATOR_MODEL,
    primary_reviewer_id: str = DEFAULT_PRIMARY_REVIEWER,
    secondary_reviewer_id: str = DEFAULT_SECONDARY_REVIEWER,
    adjudicator_reviewer_id: str = DEFAULT_ADJUDICATOR_REVIEWER,
    negative_rate: float = DEFAULT_NEGATIVE_AUDIT_RATE,
    codex_bin: str = "codex",
    max_attempts: int = 3,
    timeout_seconds: int = 1800,
    resume: bool = False,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Run PASS1, independent review, and disagreement adjudication."""

    reviewer_ids = {
        primary_reviewer_id,
        secondary_reviewer_id,
        adjudicator_reviewer_id,
    }
    if len(reviewer_ids) != 3:
        raise ReviewDriverError("primary, secondary, and adjudicator IDs must be distinct")
    if batch_size <= 0 or max_attempts <= 0 or timeout_seconds <= 0:
        raise ReviewDriverError("batch, attempt, and timeout values must be positive")
    if (
        isinstance(negative_rate, bool)
        or not isinstance(negative_rate, (int, float))
        or not math.isfinite(negative_rate)
        or not 0.10 <= negative_rate <= 0.20
    ):
        raise ReviewDriverError("negative_rate must be a finite number between 0.10 and 0.20")

    cards_sha_before = _file_sha256(cards_path)
    cards = load_task_cards(cards_path, source_base=source_base)
    cards_sha_after = _file_sha256(cards_path)
    if cards_sha_before != cards_sha_after:
        raise ReviewDriverError("task-card file changed while it was being loaded")
    cards_by_task = {card.task_name: card.payload for card in cards}
    _ensure_output_scope(output_root, source_base, cards)
    seed_artifacts = load_seed_pass1_artifacts(
        pass1_seed_batch_dirs,
        expected_receipt_sha256s=pass1_seed_receipt_sha256s,
        cards=cards,
        all_cards_by_task=cards_by_task,
        model=primary_model,
        reviewer_id=primary_reviewer_id,
    )
    seeded_task_names = {
        review["task_name"] for artifact in seed_artifacts for review in artifact.reviews
    }
    pending_cards = tuple(card for card in cards if card.task_name not in seeded_task_names)
    pass1_batches = fixed_batches(pending_cards, batch_size, phase="PASS1")
    seed_manifest_records = tuple(_seed_manifest_record(artifact) for artifact in seed_artifacts)
    if dry_run:
        return {
            "dry_run": True,
            "files_written": 0,
            "outcomes_opened": False,
            "pass1_pending_task_count": len(pending_cards),
            "pass1_seed_batch_count": len(seed_artifacts),
            "pass1_seed_task_count": len(seeded_task_names),
            "primary_batch_count": len(pass1_batches),
            "resolved_candidate_image_count": sum(len(card.image_attachments) for card in cards),
            "task_count": len(cards),
        }

    if output_root.exists() and not resume:
        raise ReviewDriverError("output directory already exists; pass --resume to verify/reuse it")
    run_prompt_version = _resolve_run_prompt_version(output_root, resume=resume)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(
        cards_path=cards_path,
        cards_file_sha256=cards_sha_after,
        source_base=source_base,
        batch_size=batch_size,
        primary_model=primary_model,
        secondary_model=secondary_model,
        adjudicator_model=adjudicator_model,
        primary_reviewer_id=primary_reviewer_id,
        secondary_reviewer_id=secondary_reviewer_id,
        adjudicator_reviewer_id=adjudicator_reviewer_id,
        negative_rate=negative_rate,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        pass1_seed_batches=seed_manifest_records,
        prompt_version=run_prompt_version,
    )
    _write_once(output_root / "run_manifest.json", canonical_json_bytes(manifest))

    installed_seed_records = [
        _install_seed_pass1_artifact(output_root, artifact) for artifact in seed_artifacts
    ]

    primary_artifacts = _execute_batches_resiliently(
        pass1_batches,
        output_root=output_root,
        execute=lambda batch: execute_stage_batch(
            batch=batch,
            all_cards_by_task=cards_by_task,
            output_root=output_root,
            model=primary_model,
            reviewer_id=primary_reviewer_id,
            codex_bin=codex_bin,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            resume=resume,
            prompt_version=run_prompt_version,
            runner=runner,
        ),
    )
    primary_reviews = [
        review for artifact in seed_artifacts for review in artifact.reviews
    ] + _reviews_from_artifacts(primary_artifacts)
    try:
        primary_by_task = validate_primary_coverage(primary_reviews)
    except ValueError as exc:
        raise ReviewDriverError(f"PASS1 coverage validation failed: {exc}") from exc
    primary_reviews = sorted(primary_by_task.values(), key=lambda item: item["catalog_index"])
    primary_sha = _freeze_jsonl(output_root / "frozen" / "pass1_reviews.jsonl", primary_reviews)
    primary_freeze = {
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "imported_seed_batches": installed_seed_records,
        "pass1_reviews_sha256": primary_sha,
        "review_count": len(primary_reviews),
        "generated_batch_response_sha256": [
            artifact.response_sha256 for artifact in primary_artifacts
        ],
        "seed_batch_response_sha256": [artifact.response_sha256 for artifact in seed_artifacts],
    }
    _write_once(
        output_root / "frozen" / "pass1_freeze.json",
        canonical_json_bytes(primary_freeze),
    )

    # This is the first and only point at which the workflow opens outcomes.
    outcomes_sha_before = _file_sha256(outcomes_path)
    outcomes = _load_outcomes(outcomes_path, cards_by_task)
    outcomes_sha_after = _file_sha256(outcomes_path)
    if outcomes_sha_before != outcomes_sha_after:
        raise ReviewDriverError("outcome sidecar changed while it was being loaded")
    first_card = cards[0].payload
    try:
        selection = select_pass2(
            primary_reviews,
            outcomes,
            first_card["dataset_sha256"],
            first_card["selection_sha256"],
            rate=negative_rate,
        )
    except ValueError as exc:
        raise ReviewDriverError(f"formal PASS2 selection failed: {exc}") from exc
    selection_bytes = canonical_json_bytes(selection)
    selection_sha = _sha256(selection_bytes)
    _write_once(output_root / "selection" / "pass2_selection.json", selection_bytes)
    selection_receipt = {
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "outcomes_file_sha256": outcomes_sha_after,
        "pass1_reviews_sha256": primary_sha,
        "pass2_selection_sha256": selection_sha,
    }
    _write_once(
        output_root / "selection" / "receipt.json",
        canonical_json_bytes(selection_receipt),
    )

    card_lookup = {card.task_name: card for card in cards}
    selected_cards = tuple(
        sorted(
            (card_lookup[record["task_name"]] for record in selection["tasks"]),
            key=lambda card: card.catalog_index,
        )
    )
    # All selected tasks use the same reviewer-visible phase.  Keeping the
    # negative-audit routing label out of the prompt prevents the secondary
    # reviewer from inferring the frozen primary class.
    secondary_batches = fixed_batches(selected_cards, batch_size, phase="PASS2")
    secondary_artifacts = _execute_batches_resiliently(
        secondary_batches,
        output_root=output_root,
        execute=lambda batch: execute_stage_batch(
            batch=batch,
            all_cards_by_task=cards_by_task,
            output_root=output_root,
            model=secondary_model,
            reviewer_id=secondary_reviewer_id,
            codex_bin=codex_bin,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            resume=resume,
            prompt_version=run_prompt_version,
            runner=runner,
        ),
    )
    secondary_reviews = _reviews_from_artifacts(secondary_artifacts)
    secondary_by_task = {review["task_name"]: review for review in secondary_reviews}
    if len(secondary_by_task) != len(selection["tasks"]):
        raise ReviewDriverError("independent-review coverage does not match formal selection")
    for record in selection["tasks"]:
        review = secondary_by_task.get(record["task_name"])
        if review is None or review["phase"] != "PASS2":
            raise ReviewDriverError("independent review must use the blind PASS2 phase")
    secondary_sha = _freeze_jsonl(
        output_root / "frozen" / "secondary_reviews.jsonl",
        sorted(secondary_reviews, key=lambda item: item["catalog_index"]),
    )

    disagreement_names = []
    for task_name, secondary in secondary_by_task.items():
        try:
            if adjudication_needed(primary_by_task[task_name], secondary):
                disagreement_names.append(task_name)
        except ValueError as exc:
            raise ReviewDriverError(
                f"independent review comparison failed for {task_name}: {exc}"
            ) from exc
    disagreement_cards = sorted(
        (card_lookup[name] for name in disagreement_names),
        key=lambda item: item.catalog_index,
    )
    adjudication_batches = fixed_batches(disagreement_cards, batch_size, phase="ADJUDICATION")
    adjudication_artifacts = _execute_batches_resiliently(
        adjudication_batches,
        output_root=output_root,
        execute=lambda batch: execute_stage_batch(
            batch=batch,
            all_cards_by_task=cards_by_task,
            output_root=output_root,
            model=adjudicator_model,
            reviewer_id=adjudicator_reviewer_id,
            codex_bin=codex_bin,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            resume=resume,
            prompt_version=run_prompt_version,
            runner=runner,
            primary_by_task=primary_by_task,
            secondary_by_task=secondary_by_task,
        ),
    )
    adjudication_reviews = _reviews_from_artifacts(adjudication_artifacts)
    adjudication_by_task = {review["task_name"]: review for review in adjudication_reviews}
    if set(adjudication_by_task) != set(disagreement_names):
        raise ReviewDriverError("adjudication coverage does not match disagreements")

    final_reviews: list[dict[str, Any]] = []
    for card in cards:
        final_reviews.append(
            adjudication_by_task.get(card.task_name, primary_by_task[card.task_name])
        )
    final_sha = _freeze_jsonl(output_root / "final" / "reviews.jsonl", final_reviews)
    try:
        metrics = compute_metrics(final_reviews, cards_by_task, outcomes)
    except ValueError as exc:
        raise ReviewDriverError(f"motivation metric computation failed: {exc}") from exc
    metrics_bytes = canonical_json_bytes(metrics)
    metrics_sha = _sha256(metrics_bytes)
    _write_once(output_root / "final" / "metrics.json", metrics_bytes)
    summary = {
        "adjudication_batch_count": len(adjudication_batches),
        "adjudication_task_count": len(adjudication_reviews),
        "dry_run": False,
        "final_reviews_sha256": final_sha,
        "final_screen_counts": _screen_counts(final_reviews),
        "material_disagreement_count": len(disagreement_names),
        "motivation_metrics_sha256": metrics_sha,
        "motivation_strength": metrics["motivation_strength"],
        "negative_audit_task_count": sum(
            record["selection_reason"] == "NEGATIVE_RANDOM_AUDIT" for record in selection["tasks"]
        ),
        "outcome_fields_supplied_to_reviewer": False,
        "outcomes_opened_after_pass1_freeze": True,
        "pass1_reviews_sha256": primary_sha,
        "pass1_screen_counts": _screen_counts(primary_reviews),
        "pass2_selection_sha256": selection_sha,
        "pass1_seed_batch_count": len(seed_artifacts),
        "pass1_seed_task_count": len(seeded_task_names),
        "pass2_task_count": sum(
            record["selection_reason"] in {"PRIMARY_POSITIVE", "PRIMARY_UNCERTAIN"}
            for record in selection["tasks"]
        ),
        "primary_batch_count": len(pass1_batches),
        "resolved_candidate_image_count": sum(len(card.image_attachments) for card in cards),
        "secondary_batch_count": len(secondary_batches),
        "secondary_reviews_sha256": secondary_sha,
        "secondary_task_count": len(secondary_reviews),
        "task_count": len(cards),
    }
    _write_once(output_root / "final" / "summary.json", canonical_json_bytes(summary))
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", required=True, type=Path)
    parser.add_argument("--outcomes", required=True, type=Path)
    parser.add_argument("--source-base", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--pass1-seed-batch-dir",
        action="append",
        default=[],
        type=Path,
        help="Verified prior PASS1 batch to import; may be repeated.",
    )
    parser.add_argument(
        "--pass1-seed-receipt-sha256",
        action="append",
        default=[],
        help="Expected receipt SHA-256 for the corresponding PASS1 seed batch.",
    )
    parser.add_argument("--primary-model", default=DEFAULT_PRIMARY_MODEL)
    parser.add_argument("--secondary-model", default=DEFAULT_SECONDARY_MODEL)
    parser.add_argument("--adjudicator-model", default=DEFAULT_ADJUDICATOR_MODEL)
    parser.add_argument("--primary-reviewer-id", default=DEFAULT_PRIMARY_REVIEWER)
    parser.add_argument("--secondary-reviewer-id", default=DEFAULT_SECONDARY_REVIEWER)
    parser.add_argument("--adjudicator-reviewer-id", default=DEFAULT_ADJUDICATOR_REVIEWER)
    parser.add_argument("--negative-rate", type=float, default=DEFAULT_NEGATIVE_AUDIT_RATE)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = run_review(
            cards_path=args.cards,
            outcomes_path=args.outcomes,
            source_base=args.source_base,
            output_root=args.output_dir,
            batch_size=args.batch_size,
            pass1_seed_batch_dirs=args.pass1_seed_batch_dir,
            pass1_seed_receipt_sha256s=args.pass1_seed_receipt_sha256,
            primary_model=args.primary_model,
            secondary_model=args.secondary_model,
            adjudicator_model=args.adjudicator_model,
            primary_reviewer_id=args.primary_reviewer_id,
            secondary_reviewer_id=args.secondary_reviewer_id,
            adjudicator_reviewer_id=args.adjudicator_reviewer_id,
            negative_rate=args.negative_rate,
            codex_bin=args.codex_bin,
            max_attempts=args.max_attempts,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    except (ReviewDriverError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
