#!/usr/bin/env python3
"""Run the frozen two-process Codex review for observational failure linkage.

``phase-a`` has no outcome argument and exits after freezing two complete blind
passes plus material adjudications.  ``phase-b`` is a separate invocation.  It
verifies the pinned Phase-A driver freeze, every receipt, and the deterministic
Phase-A resolution before the public attribution builder is allowed to open
outcomes or evaluator evidence.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from pathlib import Path, PurePosixPath
from typing import Any

import tiktoken

from mobile_world.offline.failure_attribution import (
    ATTRIBUTION_SCHEMA_VERSION,
    FailureAttributionError,
    PhaseABundle,
    PhaseAResolution,
    PhaseBBundle,
    PhaseBResolution,
    SourceBundle,
    build_phase_a_bundle,
    build_phase_b_bundle,
    load_phase_a_resolution,
    load_phase_b_resolution,
    phase_a_material_disagreement,
    phase_a_review_schema,
    phase_b_material_disagreement,
    phase_b_review_schema,
    resolve_phase_a_reviews,
    resolve_phase_b_reviews,
    validate_phase_a_reviews,
    validate_phase_b_reviews,
    write_phase_a_bundle,
    write_phase_a_resolution,
    write_phase_b_bundle,
    write_phase_b_resolution,
)
from mobile_world.offline.failure_link_prompt import (
    ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    INLINE_CARD_TRANSPORT_ENCODING,
    LARGE_CARD_ENCODING_THRESHOLD_BYTES,
    LEGACY_PROMPT_VERSION,
    PROMPT_VERSION,
    build_adjudication_prompt,
    build_legacy_v3_review_prompt,
    build_review_prompt,
    prepare_card_for_prompt,
    select_card_transport_encoding,
)
from mobile_world.offline.failure_link_review_runtime import (
    MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
    MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS,
    REQUEST_PAYLOAD_OVERHEAD_BYTES,
    REVIEWER_DISABLED_FEATURES,
    RUNTIME_SCHEMA_VERSION,
    ReviewRetryExhausted,
    ReviewRuntimeError,
    ReviewUnit,
    StageArtifact,
    execute_review_unit,
    file_sha256,
    sha256_bytes,
    verify_frozen_review_artifact,
    write_once,
)
from mobile_world.offline.motivation_review import canonical_json_bytes

DRIVER_SCHEMA_VERSION = "mobileworld.audit.failure-link-codex-driver/v4"
EXPECTED_SOURCE_COUNT = 6
EXPECTED_TASK_COUNT = 116
EXPECTED_CHAIN_COUNT = 272
EXPECTED_FAILURE_STRICT_MHR_TASK_COUNT = 108
EXPECTED_SUCCESS_CONTROL_TASK_COUNT = 8
EXPECTED_ALL_FAILURE_TASK_COUNT = 574
MAX_SYSTEMIC_RETRY_EXHAUSTIONS = 3
MAX_ATTACHMENTS_PER_CARD = 128
OFFICIAL_MAX_ATTACHMENT_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 350 * 1024 * 1024
PROMPT_TOKEN_ENCODING = "o200k_base"
MAX_PROMPT_TOKENS = 180_000
O200K_BASE_ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
TIKTOKEN_VERSION = distribution_version("tiktoken")
DEFAULT_PROMPT_TOKENIZER_ASSET = (
    Path("/tmp") / "data-gym-cache" / "fb374d419588a4632f3f557e76b4b70aebbca790"
)

_O200K_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)
_PROMPT_TOKENIZERS: dict[Path, tiktoken.Encoding] = {}

DEFAULT_PRIMARY_MODEL = "gpt-5.6-terra"
DEFAULT_SECONDARY_MODEL = "gpt-5.6-sol"
DEFAULT_ADJUDICATOR_MODEL = "gpt-5.6-sol"
DEFAULT_PHASE_A_PRIMARY_REVIEWER = "failure-link-phase-a-primary-terra-v4"
DEFAULT_PHASE_A_SECONDARY_REVIEWER = "failure-link-phase-a-secondary-sol-v4"
DEFAULT_PHASE_A_ADJUDICATOR_REVIEWER = "failure-link-phase-a-adjudicator-sol-v4"
DEFAULT_PHASE_B_PRIMARY_REVIEWER = "failure-link-phase-b-primary-terra-v4"
DEFAULT_PHASE_B_SECONDARY_REVIEWER = "failure-link-phase-b-secondary-sol-v4"
DEFAULT_PHASE_B_ADJUDICATOR_REVIEWER = "failure-link-phase-b-adjudicator-sol-v4"

LEGACY_DRIVER_SCHEMA_VERSION = "mobileworld.audit.failure-link-codex-driver/v3"
LEGACY_RUNTIME_SCHEMA_VERSION = "mobileworld.audit.failure-link-codex-runtime/v3"
LEGACY_V3_INLINE_CARD_TRANSPORT_ENCODING = "mobileworld.audit.failure-link-card-inline-legacy-v3"
PRIMARY_MIGRATION_SCHEMA_VERSION = "mobileworld.audit.failure-link-primary-migration/v1"
LEGACY_PRIMARY_ORIGIN = "LEGACY_V3_MIGRATION"
CURRENT_PRIMARY_ORIGIN = "CURRENT_V4"
EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS = (42, 66)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FailureLinkDriverError(RuntimeError):
    """A deterministic CLI, phase-gate, or frozen-artifact invariant failed."""


class SystemicReviewFailure(FailureLinkDriverError):
    """A review stage reached the bounded systemic-failure stop."""


@dataclass(frozen=True, slots=True)
class Attachment:
    blob_sha256: str
    path: Path
    ref_id: str
    role: str
    step: int


@dataclass(frozen=True, slots=True)
class ReviewCard:
    payload: dict[str, Any]
    card_sha256: str
    attachments: tuple[Attachment, ...]
    image_paths: tuple[Path, ...]
    image_sha256s: tuple[str, ...]
    image_byte_lengths: tuple[int, ...]
    attachment_map: tuple[dict[str, Any], ...]

    @property
    def task_key(self) -> str:
        return self.payload["task_key"]


@dataclass(frozen=True, slots=True)
class LegacyPrimarySeed:
    ordinal: int
    unit: ReviewUnit
    source_directory: Path
    artifact: StageArtifact


@dataclass(frozen=True, slots=True)
class PrimaryMigrationPlan:
    source_root: Path
    source_run_manifest: dict[str, Any]
    source_run_manifest_sha256: str
    source_snapshot: dict[str, Any]
    source_snapshot_sha256: str
    accepted_set: dict[str, Any]
    accepted_set_sha256: str
    missing_task_keys: tuple[str, ...]
    seeds: tuple[LegacyPrimarySeed, ...]


@dataclass(frozen=True, slots=True)
class PrimaryMigrationOverlay:
    freeze: dict[str, Any]
    freeze_sha256: str
    source_run_manifest: dict[str, Any]
    source_snapshot: dict[str, Any]
    accepted_set: dict[str, Any]
    missing_task_keys: tuple[str, ...]
    artifacts_by_task: Mapping[str, StageArtifact]


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def run_phase_a(
    *,
    source_bundles: Sequence[SourceBundle],
    source_base: Path,
    output_root: Path,
    primary_model: str,
    secondary_model: str,
    adjudicator_model: str,
    primary_reviewer_id: str,
    secondary_reviewer_id: str,
    adjudicator_reviewer_id: str,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    resume: bool,
    dry_run: bool,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
    phase_a_v3_primary_seed_root: Path | None = None,
    phase_a_v3_primary_seed_run_manifest_sha256: str | None = None,
    phase_a_v3_primary_seed_snapshot_sha256: str | None = None,
    phase_a_v3_primary_seed_accepted_set_sha256: str | None = None,
    runner: Runner = subprocess.run,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
    expected_task_count: int = EXPECTED_TASK_COUNT,
    expected_chain_count: int = EXPECTED_CHAIN_COUNT,
) -> dict[str, Any]:
    """Run only outcome-blind Phase A, freeze it, and return."""

    _validate_reviewer_ids(primary_reviewer_id, secondary_reviewer_id, adjudicator_reviewer_id)
    if len(source_bundles) != expected_source_count:
        raise FailureLinkDriverError(
            f"Phase A requires exactly {expected_source_count} source bundles"
        )
    phase_a_bundle = build_phase_a_bundle(source_bundles)
    _validate_phase_a_counts(
        phase_a_bundle,
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    cards = _load_review_cards(phase_a_bundle.cards, source_base=source_base)
    seed_values = (
        phase_a_v3_primary_seed_root,
        phase_a_v3_primary_seed_run_manifest_sha256,
        phase_a_v3_primary_seed_snapshot_sha256,
        phase_a_v3_primary_seed_accepted_set_sha256,
    )
    if any(value is not None for value in seed_values) and not all(
        value is not None for value in seed_values
    ):
        raise FailureLinkDriverError(
            "legacy primary migration requires source root plus all three SHA-256 pins"
        )
    migration_plan = (
        _prepare_primary_migration(
            source_root=phase_a_v3_primary_seed_root,
            expected_run_manifest_sha256=(phase_a_v3_primary_seed_run_manifest_sha256),
            expected_source_snapshot_sha256=(phase_a_v3_primary_seed_snapshot_sha256),
            expected_accepted_set_sha256=(phase_a_v3_primary_seed_accepted_set_sha256),
            cards=cards,
            schema=phase_a_review_schema(),
            bundle_manifest=phase_a_bundle.manifest,
            primary_model=primary_model,
        )
        if phase_a_v3_primary_seed_root is not None
        and phase_a_v3_primary_seed_run_manifest_sha256 is not None
        and phase_a_v3_primary_seed_snapshot_sha256 is not None
        and phase_a_v3_primary_seed_accepted_set_sha256 is not None
        else None
    )
    _validate_primary_migration_reviewer_independence(
        migration_plan,
        primary_reviewer_id=primary_reviewer_id,
        secondary_reviewer_id=secondary_reviewer_id,
        adjudicator_reviewer_id=adjudicator_reviewer_id,
    )
    if dry_run:
        profile = _dry_run_profile(
            phase="A",
            cards=cards,
            schema=phase_a_review_schema(),
            reviewer_id=primary_reviewer_id,
            prompt_tokenizer_asset=prompt_tokenizer_asset,
        )
        return {
            "causal_claim_supported": False,
            "codex_invocations": 0,
            "dry_run": True,
            "outcomes_opened": False,
            "phase": "A",
            "profile": profile,
            "primary_migration": (
                {
                    "accepted_set_sha256": migration_plan.accepted_set_sha256,
                    "legacy_primary_count": len(migration_plan.seeds),
                    "missing_task_keys": list(migration_plan.missing_task_keys),
                    "source_run_manifest_sha256": (migration_plan.source_run_manifest_sha256),
                    "source_snapshot_sha256": migration_plan.source_snapshot_sha256,
                }
                if migration_plan is not None
                else None
            ),
            "source_count": len(source_bundles),
            "task_count": len(cards),
            "write_count": 0,
        }

    _ensure_separate_output(
        output_root,
        source_base,
        source_bundles,
        extra_forbidden=(migration_plan.source_root,) if migration_plan is not None else (),
    )
    _prepare_output_root(output_root, resume=resume)
    run_manifest = _run_manifest(
        phase="A",
        bundle_manifest=phase_a_bundle.manifest,
        source_bundles=source_bundles,
        primary_model=primary_model,
        secondary_model=secondary_model,
        adjudicator_model=adjudicator_model,
        primary_reviewer_id=primary_reviewer_id,
        secondary_reviewer_id=secondary_reviewer_id,
        adjudicator_reviewer_id=adjudicator_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        phase_a_driver_freeze_sha256=None,
        primary_task_count=len(cards),
        primary_migration=migration_plan,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    write_once(output_root / "run_manifest.json", canonical_json_bytes(run_manifest))
    _freeze_bundle_snapshot(
        phase_a_bundle,
        output_root / "input",
        phase="A",
        resume=resume,
    )
    migration_overlay = (
        _materialize_primary_migration(
            plan=migration_plan,
            output_root=output_root,
            run_manifest=run_manifest,
            bundle_manifest=phase_a_bundle.manifest,
            cards=cards,
            schema=phase_a_review_schema(),
        )
        if migration_plan is not None
        else None
    )

    schema = phase_a_review_schema()
    primary, primary_artifacts = _execute_pass(
        phase="A",
        stage="PRIMARY",
        cards=cards,
        schema=schema,
        bundle_manifest=phase_a_bundle.manifest,
        output_root=output_root,
        model=primary_model,
        reviewer_id=primary_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
        migrated_artifacts_by_task=(
            migration_overlay.artifacts_by_task if migration_overlay is not None else None
        ),
    )
    secondary, secondary_artifacts = _execute_pass(
        phase="A",
        stage="SECONDARY",
        cards=cards,
        schema=schema,
        bundle_manifest=phase_a_bundle.manifest,
        output_root=output_root,
        model=secondary_model,
        reviewer_id=secondary_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    primary_by_key = {review["task_key"]: review for review in primary}
    secondary_by_key = {review["task_key"]: review for review in secondary}
    material_cards = tuple(
        card
        for card in cards
        if phase_a_material_disagreement(
            primary_by_key[card.task_key], secondary_by_key[card.task_key], card.payload
        )
    )
    adjudications, adjudication_artifacts = _execute_pass(
        phase="A",
        stage="ADJUDICATION",
        cards=material_cards,
        schema=schema,
        bundle_manifest=phase_a_bundle.manifest,
        output_root=output_root,
        model=adjudicator_model,
        reviewer_id=adjudicator_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        primary_by_key=primary_by_key,
        secondary_by_key=secondary_by_key,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    resolution = resolve_phase_a_reviews(phase_a_bundle, primary, secondary, adjudications)
    _validate_resolution_common(
        resolution,
        phase="A",
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    _freeze_resolution(
        resolution,
        output_root / "resolution",
        phase="A",
        bundle=phase_a_bundle,
        resume=resume,
    )
    artifacts = (*primary_artifacts, *secondary_artifacts, *adjudication_artifacts)
    driver_freeze = _driver_freeze(
        phase="A",
        bundle_manifest=phase_a_bundle.manifest,
        resolution=resolution,
        output_root=output_root,
        artifacts=artifacts,
        run_manifest=run_manifest,
        phase_a_driver_freeze_sha256=None,
        primary_migration=migration_overlay,
    )
    _verify_receipt_index(
        output_root,
        driver_freeze["receipts"],
        resolution=resolution,
        bundle_manifest=phase_a_bundle.manifest,
        cards=cards,
        primary_migration=migration_overlay,
    )
    freeze_bytes = canonical_json_bytes(driver_freeze)
    write_once(output_root / "driver_freeze.json", freeze_bytes)
    return {
        "adjudication_review_count": len(adjudications),
        "causal_claim_supported": False,
        "driver_freeze_sha256": sha256_bytes(freeze_bytes),
        "dry_run": False,
        "material_disagreement_task_count": len(material_cards),
        "outcomes_opened": False,
        "phase": "A",
        "resolution_id": resolution.manifest["resolution_id"],
        "task_count": len(cards),
    }


def run_phase_b(
    *,
    source_bundles: Sequence[SourceBundle],
    phase_a_root: Path,
    phase_a_driver_freeze_sha256: str,
    source_base: Path,
    output_root: Path,
    primary_model: str,
    secondary_model: str,
    adjudicator_model: str,
    primary_reviewer_id: str,
    secondary_reviewer_id: str,
    adjudicator_reviewer_id: str,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    resume: bool,
    dry_run: bool,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
    runner: Runner = subprocess.run,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
    expected_task_count: int = EXPECTED_TASK_COUNT,
    expected_chain_count: int = EXPECTED_CHAIN_COUNT,
    expected_failure_task_count: int = EXPECTED_FAILURE_STRICT_MHR_TASK_COUNT,
    expected_success_control_count: int = EXPECTED_SUCCESS_CONTROL_TASK_COUNT,
    expected_all_failure_task_count: int = EXPECTED_ALL_FAILURE_TASK_COUNT,
) -> dict[str, Any]:
    """Verify Phase A first, then open outcomes and run all-card Phase B."""

    _validate_reviewer_ids(primary_reviewer_id, secondary_reviewer_id, adjudicator_reviewer_id)
    if not _SHA256_RE.fullmatch(phase_a_driver_freeze_sha256):
        raise FailureLinkDriverError("--phase-a-driver-freeze-sha256 is invalid")
    if len(source_bundles) != expected_source_count:
        raise FailureLinkDriverError(
            f"Phase B requires exactly {expected_source_count} source bundles"
        )

    # Hard ordering gate: everything through this call is outcome blind.
    phase_a_bundle = build_phase_a_bundle(source_bundles)
    _validate_phase_a_counts(
        phase_a_bundle,
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    phase_a_resolution, phase_a_freeze = _verify_phase_a_driver_freeze(
        phase_a_root=phase_a_root,
        expected_freeze_sha256=phase_a_driver_freeze_sha256,
        phase_a_bundle=phase_a_bundle,
        source_base=source_base,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    phase_a_reviewer_ids = set(phase_a_freeze["configured_reviewer_ids"]) | set(
        phase_a_freeze["receipt_reviewer_ids"]
    )
    overlap = phase_a_reviewer_ids & {
        primary_reviewer_id,
        secondary_reviewer_id,
        adjudicator_reviewer_id,
    }
    if overlap:
        raise FailureLinkDriverError(
            f"Phase B reviewer identities must be distinct from Phase A: {sorted(overlap)}"
        )

    # This is the first operation permitted to read outcomes/evaluator evidence.
    phase_b_bundle = build_phase_b_bundle(
        phase_a_bundle, phase_a_resolution, source_base=source_base
    )
    _validate_phase_b_counts(
        phase_b_bundle,
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
        expected_failure_task_count=expected_failure_task_count,
        expected_success_control_count=expected_success_control_count,
        expected_all_failure_task_count=expected_all_failure_task_count,
    )
    cards = _load_review_cards(phase_b_bundle.cards, source_base=source_base)
    if dry_run:
        profile = _dry_run_profile(
            phase="B",
            cards=cards,
            schema=phase_b_review_schema(),
            reviewer_id=primary_reviewer_id,
            prompt_tokenizer_asset=prompt_tokenizer_asset,
        )
        return {
            "causal_claim_supported": False,
            "codex_invocations": 0,
            "dry_run": True,
            "outcomes_opened": True,
            "phase": "B",
            "profile": profile,
            "success_control_task_count": expected_success_control_count,
            "task_count": len(cards),
            "write_count": 0,
        }

    _ensure_separate_output(
        output_root, source_base, source_bundles, extra_forbidden=(phase_a_root,)
    )
    _prepare_output_root(output_root, resume=resume)
    run_manifest = _run_manifest(
        phase="B",
        bundle_manifest=phase_b_bundle.manifest,
        source_bundles=source_bundles,
        primary_model=primary_model,
        secondary_model=secondary_model,
        adjudicator_model=adjudicator_model,
        primary_reviewer_id=primary_reviewer_id,
        secondary_reviewer_id=secondary_reviewer_id,
        adjudicator_reviewer_id=adjudicator_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        phase_a_driver_freeze_sha256=phase_a_driver_freeze_sha256,
        primary_task_count=len(cards),
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    write_once(output_root / "run_manifest.json", canonical_json_bytes(run_manifest))
    _freeze_bundle_snapshot(
        phase_b_bundle,
        output_root / "input",
        phase="B",
        resume=resume,
    )
    schema = phase_b_review_schema()
    primary, primary_artifacts = _execute_pass(
        phase="B",
        stage="PRIMARY",
        cards=cards,
        schema=schema,
        bundle_manifest=phase_b_bundle.manifest,
        output_root=output_root,
        model=primary_model,
        reviewer_id=primary_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
        phase_a_driver_freeze_sha256=phase_a_driver_freeze_sha256,
    )
    secondary, secondary_artifacts = _execute_pass(
        phase="B",
        stage="SECONDARY",
        cards=cards,
        schema=schema,
        bundle_manifest=phase_b_bundle.manifest,
        output_root=output_root,
        model=secondary_model,
        reviewer_id=secondary_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
        phase_a_driver_freeze_sha256=phase_a_driver_freeze_sha256,
    )
    primary_by_key = {review["task_key"]: review for review in primary}
    secondary_by_key = {review["task_key"]: review for review in secondary}
    material_cards = tuple(
        card
        for card in cards
        if phase_b_material_disagreement(
            primary_by_key[card.task_key], secondary_by_key[card.task_key], card.payload
        )
    )
    adjudications, adjudication_artifacts = _execute_pass(
        phase="B",
        stage="ADJUDICATION",
        cards=material_cards,
        schema=schema,
        bundle_manifest=phase_b_bundle.manifest,
        output_root=output_root,
        model=adjudicator_model,
        reviewer_id=adjudicator_reviewer_id,
        codex_bin=codex_bin,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        resume=resume,
        runner=runner,
        primary_by_key=primary_by_key,
        secondary_by_key=secondary_by_key,
        phase_a_driver_freeze_sha256=phase_a_driver_freeze_sha256,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    resolution = resolve_phase_b_reviews(
        phase_b_bundle,
        primary,
        secondary,
        adjudications,
        all_failure_task_count=expected_all_failure_task_count,
    )
    _validate_resolution_common(
        resolution,
        phase="B",
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    if resolution.metrics.get("causal_claim_supported") is not False:
        raise FailureLinkDriverError("Phase B metrics attempted to support a causal claim")
    _freeze_resolution(
        resolution,
        output_root / "resolution",
        phase="B",
        bundle=phase_b_bundle,
        resume=resume,
    )
    artifacts = (*primary_artifacts, *secondary_artifacts, *adjudication_artifacts)
    driver_freeze = _driver_freeze(
        phase="B",
        bundle_manifest=phase_b_bundle.manifest,
        resolution=resolution,
        output_root=output_root,
        artifacts=artifacts,
        run_manifest=run_manifest,
        phase_a_driver_freeze_sha256=phase_a_driver_freeze_sha256,
    )
    _verify_receipt_index(
        output_root,
        driver_freeze["receipts"],
        resolution=resolution,
        bundle_manifest=phase_b_bundle.manifest,
        cards=cards,
    )
    freeze_bytes = canonical_json_bytes(driver_freeze)
    write_once(output_root / "driver_freeze.json", freeze_bytes)
    return {
        "adjudication_review_count": len(adjudications),
        "causal_claim_supported": False,
        "driver_freeze_sha256": sha256_bytes(freeze_bytes),
        "dry_run": False,
        "material_disagreement_task_count": len(material_cards),
        "outcomes_opened": True,
        "phase": "B",
        "resolution_id": resolution.manifest["resolution_id"],
        "success_control_task_count": expected_success_control_count,
        "task_count": len(cards),
    }


def _execute_pass(
    *,
    phase: str,
    stage: str,
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    output_root: Path,
    model: str,
    reviewer_id: str,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    resume: bool,
    runner: Runner,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
    migrated_artifacts_by_task: Mapping[str, StageArtifact] | None = None,
    primary_by_key: Mapping[str, Mapping[str, Any]] | None = None,
    secondary_by_key: Mapping[str, Mapping[str, Any]] | None = None,
    phase_a_driver_freeze_sha256: str | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[StageArtifact, ...]]:
    if migrated_artifacts_by_task and (phase != "A" or stage != "PRIMARY"):
        raise FailureLinkDriverError("legacy migration is allowed only for Phase-A PRIMARY")
    artifacts: list[StageArtifact] = []
    exhausted: list[str] = []
    migrated_seen: set[str] = set()
    for ordinal, card in enumerate(cards, start=1):
        _validate_review_card_attachments(card)
        identity = _review_identity(
            phase=phase,
            stage=stage,
            reviewer_id=reviewer_id,
            card=card.payload,
            card_sha256=card.card_sha256,
        )
        unit = ReviewUnit(
            phase=phase,
            stage=stage,
            unit_id=f"{phase.lower()}-{stage.lower()}-{ordinal:04d}-{card.card_sha256[:12]}",
            task_key=card.task_key,
            card_sha256=card.card_sha256,
            image_paths=card.image_paths,
            image_sha256s=card.image_sha256s,
            image_byte_lengths=card.image_byte_lengths,
        )
        migrated = (
            migrated_artifacts_by_task.get(card.task_key)
            if migrated_artifacts_by_task is not None
            else None
        )
        if migrated is not None:
            if migrated.unit != unit or not migrated.resumed:
                raise FailureLinkDriverError(
                    f"migrated primary artifact identity mismatch: {card.task_key}"
                )
            artifacts.append(migrated)
            migrated_seen.add(card.task_key)
            continue
        card_transport_encoding = select_card_transport_encoding(card.payload)
        prompt_card, card_transport = prepare_card_for_prompt(
            card.payload,
            transport_encoding=card_transport_encoding,
        )
        if stage == "ADJUDICATION":
            if primary_by_key is None or secondary_by_key is None:
                raise FailureLinkDriverError("adjudication requires both independent passes")
            first = primary_by_key[card.task_key]
            second = secondary_by_key[card.task_key]

            def render_prompt(
                feedback: str | None,
                *,
                card: ReviewCard = card,
                identity: Mapping[str, Any] = identity,
                first: Mapping[str, Any] = first,
                second: Mapping[str, Any] = second,
            ) -> str:
                prompt = build_adjudication_prompt(
                    phase=phase,
                    reviewer_id=reviewer_id,
                    identity=identity,
                    card=card.payload,
                    schema=schema,
                    primary_review=first,
                    secondary_review=second,
                    material_disagreement={
                        "material": True,
                        "public_predicate": f"phase_{phase.lower()}_material_disagreement",
                    },
                    attachment_map=card.attachment_map,
                    validation_feedback=feedback,
                    card_transport_encoding=card_transport_encoding,
                )
                _validate_prompt_budget(
                    prompt,
                    unit_id=unit.unit_id,
                    tokenizer_asset_path=prompt_tokenizer_asset,
                )
                return prompt

            extra_binding = {
                "primary_review_sha256": _canonical_sha256(first),
                "secondary_review_sha256": _canonical_sha256(second),
            }
        else:

            def render_prompt(
                feedback: str | None,
                *,
                card: ReviewCard = card,
                identity: Mapping[str, Any] = identity,
            ) -> str:
                prompt = build_review_prompt(
                    phase=phase,
                    stage=stage,
                    reviewer_id=reviewer_id,
                    identity=identity,
                    card=card.payload,
                    schema=schema,
                    attachment_map=card.attachment_map,
                    validation_feedback=feedback,
                    card_transport_encoding=card_transport_encoding,
                )
                _validate_prompt_budget(
                    prompt,
                    unit_id=unit.unit_id,
                    tokenizer_asset_path=prompt_tokenizer_asset,
                )
                return prompt

            extra_binding = {}

        def validate_response(
            response: Mapping[str, Any],
            *,
            card: ReviewCard = card,
            identity: Mapping[str, Any] = identity,
        ) -> Mapping[str, Any]:
            return _validated_review_response(
                response,
                phase=phase,
                card=card,
                identity=identity,
            )

        receipt_binding = {
            "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
            "bundle_card_set_sha256": bundle_manifest[
                "phase_a_card_set_sha256" if phase == "A" else "phase_b_card_set_sha256"
            ],
            "causal_claim_supported": False,
            "card_transport_encoding": card_transport_encoding,
            "card_transport_sha256": _canonical_sha256(card_transport),
            "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
            "driver_schema_version": DRIVER_SCHEMA_VERSION,
            "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
            "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
            "max_prompt_bytes": MAX_PROMPT_BYTES,
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "max_prompt_tokens": MAX_PROMPT_TOKENS,
            "phase_a_driver_freeze_sha256": phase_a_driver_freeze_sha256,
            "prompt_version": PROMPT_VERSION,
            "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
            "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
            "tiktoken_version": TIKTOKEN_VERSION,
            "prompt_card_sha256": _canonical_sha256(prompt_card),
            **extra_binding,
        }
        try:
            artifacts.append(
                execute_review_unit(
                    unit=unit,
                    output_root=output_root,
                    model=model,
                    reviewer_id=reviewer_id,
                    codex_bin=codex_bin,
                    schema=schema,
                    render_prompt=render_prompt,
                    validate_response=validate_response,
                    receipt_binding=receipt_binding,
                    max_attempts=max_attempts,
                    timeout_seconds=timeout_seconds,
                    resume=resume,
                    runner=runner,
                )
            )
        except ReviewRetryExhausted:
            exhausted.append(unit.unit_id)
            if len(exhausted) >= MAX_SYSTEMIC_RETRY_EXHAUSTIONS:
                raise SystemicReviewFailure(
                    f"systemic stop after {len(exhausted)} exhausted {phase}/{stage} "
                    "units; completed singleton artifacts were preserved"
                ) from None
    if exhausted:
        raise FailureLinkDriverError(
            f"incomplete {phase}/{stage} pass; retry with --resume: {exhausted}"
        )
    if migrated_artifacts_by_task is not None and migrated_seen != set(migrated_artifacts_by_task):
        raise FailureLinkDriverError("migrated primary artifact coverage contains unknown tasks")
    return (
        tuple(artifact.response for artifact in artifacts),
        tuple(artifacts),
    )


def _review_identity(
    *,
    phase: str,
    stage: str,
    reviewer_id: str,
    card: Mapping[str, Any],
    card_sha256: str,
) -> dict[str, Any]:
    stage_slug = stage.lower()
    identity = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": (
            "failure_attribution_phase_a_review"
            if phase == "A"
            else "failure_attribution_phase_b_review"
        ),
        "attribution_run_id": card["attribution_run_id"],
        "review_id": f"failure-link-v4-{phase.lower()}-{stage_slug}-{card_sha256[:24]}",
        "reviewer_id": reviewer_id,
        "task_key": card["task_key"],
        "model_id": card["model_id"],
        "task_name": card["task"]["task_name"],
        "catalog_index": card["task"]["catalog_index"],
    }
    if phase == "A":
        identity["phase_a_card_sha256"] = card_sha256
        identity["outcome_blinded"] = True
    else:
        identity["phase_b_card_sha256"] = card_sha256
    return identity


def _legacy_v3_review_identity(
    *,
    phase: str,
    stage: str,
    reviewer_id: str,
    card: Mapping[str, Any],
    card_sha256: str,
) -> dict[str, Any]:
    identity = _review_identity(
        phase=phase,
        stage=stage,
        reviewer_id=reviewer_id,
        card=card,
        card_sha256=card_sha256,
    )
    identity["review_id"] = f"failure-link-{phase.lower()}-{stage.lower()}-{card_sha256[:24]}"
    return identity


def _validated_review_response(
    response: Mapping[str, Any],
    *,
    phase: str,
    card: ReviewCard,
    identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    if phase == "A":
        validated = validate_phase_a_reviews([response], [card.payload])
    else:
        validated = validate_phase_b_reviews([response], [card.payload])
    result = validated[card.task_key]
    for key, expected in identity.items():
        actual = result.get(key)
        if actual != expected:
            raise ValueError(
                f"review identity mismatch for {card.task_key}: {key}; "
                f"expected={_bounded_json(expected)}; actual={_bounded_json(actual)}"
            )
    return result


def _dry_run_profile(
    *,
    phase: str,
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
    reviewer_id: str,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
) -> dict[str, Any]:
    card_sizes: list[int] = []
    prompt_char_counts: list[int] = []
    prompt_sizes: list[int] = []
    prompt_token_counts: list[int] = []
    image_counts: list[int] = []
    image_byte_counts: list[int] = []
    estimated_request_byte_counts: list[int] = []
    mapping_counts: list[int] = []
    prompt_by_task: dict[str, int] = {}
    transport_counts = {
        ASSISTANT_EXPOSURES_TRANSPORT_ENCODING: 0,
        INLINE_CARD_TRANSPORT_ENCODING: 0,
    }
    per_card: list[dict[str, Any]] = []
    for card in cards:
        _validate_review_card_attachments(card)
        identity = _review_identity(
            phase=phase,
            stage="PRIMARY",
            reviewer_id=reviewer_id,
            card=card.payload,
            card_sha256=card.card_sha256,
        )
        card_transport_encoding = select_card_transport_encoding(card.payload)
        prompt_text = build_review_prompt(
            phase=phase,
            stage="PRIMARY",
            reviewer_id=reviewer_id,
            identity=identity,
            card=card.payload,
            schema=schema,
            attachment_map=card.attachment_map,
            card_transport_encoding=card_transport_encoding,
        )
        prompt_token_count = _validate_prompt_budget(
            prompt_text,
            unit_id=f"dry-run/{phase.lower()}/{card.task_key}",
            tokenizer_asset_path=prompt_tokenizer_asset,
        )
        prompt = prompt_text.encode("utf-8")
        card_size = len(canonical_json_bytes(card.payload))
        prompt_size = len(prompt)
        image_count = len(card.image_paths)
        image_byte_count = sum(card.image_byte_lengths)
        estimated_request_byte_count = (
            prompt_size
            + sum(4 * ((byte_length + 2) // 3) for byte_length in card.image_byte_lengths)
            + REQUEST_PAYLOAD_OVERHEAD_BYTES
        )
        mapping_count = len(card.attachment_map)
        card_sizes.append(card_size)
        prompt_char_counts.append(len(prompt_text))
        prompt_sizes.append(prompt_size)
        prompt_token_counts.append(prompt_token_count)
        image_counts.append(image_count)
        image_byte_counts.append(image_byte_count)
        estimated_request_byte_counts.append(estimated_request_byte_count)
        mapping_counts.append(mapping_count)
        prompt_by_task[card.task_key] = prompt_size
        transport_counts[card_transport_encoding] += 1
        per_card.append(
            {
                "attachment_mapping_count": mapping_count,
                "card_transport_encoding": card_transport_encoding,
                "card_bytes": card_size,
                "image_attachment_count": image_count,
                "image_attachment_total_bytes": image_byte_count,
                "estimated_request_payload_bytes": estimated_request_byte_count,
                "prompt_bytes": prompt_size,
                "prompt_chars": len(prompt_text),
                "prompt_tokens": prompt_token_count,
                "task_key": card.task_key,
            }
        )
    oversize = sorted(
        task_key for task_key, size in prompt_by_task.items() if size > MAX_PROMPT_BYTES
    )
    if oversize:
        raise FailureLinkDriverError(
            "one or more singleton prompts exceed the frozen byte limit; evidence "
            f"must not be truncated or silently split: {oversize}"
        )
    request_oversize = [
        record["task_key"]
        for record in per_card
        if record["estimated_request_payload_bytes"] > MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES
    ]
    if request_oversize:
        raise FailureLinkDriverError(
            "one or more singleton estimated request payloads exceed the "
            f"conservative byte limit: {request_oversize}"
        )
    max_task_key = max(prompt_by_task, key=prompt_by_task.__getitem__)
    max_image_count = max(image_counts)
    return {
        "attachment_mapping_count": _distribution(mapping_counts),
        "card_bytes": _distribution(card_sizes),
        "card_transport_encoding_counts": transport_counts,
        "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
        "estimated_request_payload_bytes": _distribution(estimated_request_byte_counts),
        "estimated_request_payload_byte_limit": (MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES),
        "image_attachment_count": _distribution(image_counts),
        "image_attachment_total_bytes": _distribution(image_byte_counts),
        "image_attachment_limit": MAX_ATTACHMENTS_PER_CARD,
        "image_attachment_total_byte_limit": MAX_ATTACHMENT_TOTAL_BYTES,
        "official_image_attachment_total_byte_limit": (OFFICIAL_MAX_ATTACHMENT_TOTAL_BYTES),
        "limits_satisfied": True,
        "max_image_attachment_task_keys": [
            record["task_key"]
            for record in per_card
            if record["image_attachment_count"] == max_image_count
        ],
        "max_prompt_task_key": max_task_key,
        "per_card": per_card,
        "prompt_byte_limit": MAX_PROMPT_BYTES,
        "prompt_bytes": _distribution(prompt_sizes),
        "prompt_char_limit": MAX_PROMPT_CHARS,
        "prompt_chars": _distribution(prompt_char_counts),
        "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
        "prompt_token_limit": MAX_PROMPT_TOKENS,
        "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
        "prompt_tokens": _distribution(prompt_token_counts),
        "tiktoken_version": TIKTOKEN_VERSION,
    }


def _distribution(values: Sequence[int]) -> dict[str, int | float]:
    if not values:
        return {"max": 0, "median": 0, "min": 0, "p95": 0}
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    median_value: int | float
    if count % 2:
        median_value = ordered[middle]
    else:
        median_value = (ordered[middle - 1] + ordered[middle]) / 2
    p95_index = max(0, (95 * count + 99) // 100 - 1)
    return {
        "max": ordered[-1],
        "median": median_value,
        "min": ordered[0],
        "p95": ordered[p95_index],
    }


def _prompt_tokenizer(asset_path: Path) -> tiktoken.Encoding:
    _reject_symlink_path(asset_path, boundary=Path(asset_path.absolute().anchor))
    try:
        resolved = asset_path.resolve(strict=True)
        payload = resolved.read_bytes()
    except OSError as exc:
        raise FailureLinkDriverError(
            f"cannot read required offline {PROMPT_TOKEN_ENCODING} asset: {asset_path}: {exc}"
        ) from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != O200K_BASE_ASSET_SHA256:
        raise FailureLinkDriverError(
            f"offline {PROMPT_TOKEN_ENCODING} asset SHA-256 mismatch: {actual_sha256}"
        )
    cached = _PROMPT_TOKENIZERS.get(resolved)
    if cached is not None:
        return cached
    mergeable_ranks: dict[bytes, int] = {}
    try:
        for line in payload.splitlines():
            token, rank = line.split()
            mergeable_ranks[base64.b64decode(token, validate=True)] = int(rank)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise FailureLinkDriverError(
            f"cannot parse offline {PROMPT_TOKEN_ENCODING} asset: {resolved}"
        ) from exc
    encoding = tiktoken.Encoding(
        name=f"{PROMPT_TOKEN_ENCODING}-offline-{O200K_BASE_ASSET_SHA256[:12]}",
        pat_str=_O200K_PATTERN,
        mergeable_ranks=mergeable_ranks,
        special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    )
    _PROMPT_TOKENIZERS[resolved] = encoding
    return encoding


def _prompt_token_count(prompt: str, *, tokenizer_asset_path: Path) -> int:
    try:
        encoding = _prompt_tokenizer(tokenizer_asset_path)
        return len(encoding.encode(prompt, disallowed_special=()))
    except (KeyError, ValueError) as exc:
        raise FailureLinkDriverError(
            f"cannot tokenize failure-link prompt with {PROMPT_TOKEN_ENCODING}: {exc}"
        ) from exc


def _validate_prompt_budget(
    prompt: str,
    *,
    unit_id: str,
    tokenizer_asset_path: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
) -> int:
    if len(prompt) > MAX_PROMPT_CHARS:
        raise FailureLinkDriverError(
            f"prompt exceeds frozen character limit for {unit_id}: "
            f"{len(prompt)} > {MAX_PROMPT_CHARS}"
        )
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise FailureLinkDriverError(
            f"prompt exceeds frozen UTF-8 byte limit for {unit_id}: "
            f"{prompt_bytes} > {MAX_PROMPT_BYTES}"
        )
    token_count = _prompt_token_count(prompt, tokenizer_asset_path=tokenizer_asset_path)
    if token_count > MAX_PROMPT_TOKENS:
        raise FailureLinkDriverError(
            f"prompt exceeds frozen {PROMPT_TOKEN_ENCODING} token limit for {unit_id}: "
            f"{token_count} > {MAX_PROMPT_TOKENS}"
        )
    return token_count


def _bounded_json(value: Any) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(rendered) <= 320:
        return rendered
    digest = sha256_bytes(rendered.encode("utf-8"))
    return f"{rendered[:240]}…(sha256={digest},chars={len(rendered)})"


def _migration_source_snapshot(source_root: Path) -> dict[str, Any]:
    _reject_symlink_path(source_root, boundary=Path(source_root.absolute().anchor))
    try:
        root = source_root.resolve(strict=True)
    except OSError as exc:
        raise FailureLinkDriverError(f"cannot resolve migration source root: {exc}") from exc
    if not root.is_dir():
        raise FailureLinkDriverError(f"migration source root is not a directory: {root}")
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        _reject_symlink_path(path, boundary=root)
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            if not _allowed_migration_source_directory(relative):
                raise FailureLinkDriverError(
                    f"unexpected directory in migration source: {relative}"
                )
            directories.append(relative)
            continue
        if not path.is_file() or not _allowed_migration_source_file(relative):
            raise FailureLinkDriverError(f"unexpected file in migration source: {relative}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise FailureLinkDriverError(
                f"cannot read migration source artifact {path}: {exc}"
            ) from exc
        files.append(
            {
                "byte_count": len(payload),
                "relative_path": relative,
                "sha256": sha256_bytes(payload),
            }
        )
    return {
        "directories": directories,
        "files": files,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
        "source_root_realpath": str(root),
    }


def _allowed_migration_source_directory(relative: str) -> bool:
    if relative in {"batches", "failures", "input", "rejected"}:
        return True
    if relative in {
        "batches/phase-a-primary",
        "failures/phase-a-primary",
        "rejected/phase-a-primary",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(?:batches|failures|rejected)/phase-a-primary/"
            r"a-primary-[0-9]{4}-[0-9a-f]{12}",
            relative,
        )
    )


def _allowed_migration_source_file(relative: str) -> bool:
    if relative in {
        "input/cards.jsonl",
        "input/manifest.json",
        "input/review_schema.json",
        "run_manifest.json",
    }:
        return True
    if re.fullmatch(
        r"batches/phase-a-primary/a-primary-[0-9]{4}-[0-9a-f]{12}/"
        r"(?:model_response|output_schema|receipt|response)\.json",
        relative,
    ):
        return True
    if re.fullmatch(
        r"rejected/phase-a-primary/a-primary-[0-9]{4}-[0-9a-f]{12}/"
        r"attempt-[0-9]{2}-[0-9a-f]{64}\.json",
        relative,
    ):
        return True
    return bool(
        re.fullmatch(
            r"failures/phase-a-primary/a-primary-[0-9]{4}-[0-9a-f]{12}/"
            r"[0-9a-f]{64}\.json",
            relative,
        )
    )


def _legacy_v3_expected_receipt(
    *,
    unit: ReviewUnit,
    card: ReviewCard,
    schema: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    source_run_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], Callable[[str | None], str], Callable[[Mapping[str, Any]], Any]]:
    reviewer_id = source_run_manifest["primary_reviewer_id"]
    identity = _legacy_v3_review_identity(
        phase="A",
        stage="PRIMARY",
        reviewer_id=reviewer_id,
        card=card.payload,
        card_sha256=card.card_sha256,
    )

    def render_prompt(feedback: str | None) -> str:
        return build_legacy_v3_review_prompt(
            phase="A",
            stage="PRIMARY",
            reviewer_id=reviewer_id,
            identity=identity,
            card=card.payload,
            schema=schema,
            attachment_map=card.attachment_map,
            validation_feedback=feedback,
        )

    def validate_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
        return _validated_review_response(
            response,
            phase="A",
            card=card,
            identity=identity,
        )

    image_attachments = _review_unit_image_records(unit)
    schema_bytes = canonical_json_bytes(dict(schema))
    base_prompt_bytes = render_prompt(None).encode("utf-8")
    receipt_binding = {
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "bundle_card_set_sha256": bundle_manifest["phase_a_card_set_sha256"],
        "causal_claim_supported": False,
        "driver_schema_version": LEGACY_DRIVER_SCHEMA_VERSION,
        "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
        "phase_a_driver_freeze_sha256": None,
        "prompt_version": LEGACY_PROMPT_VERSION,
    }
    expected_receipt = {
        "base_prompt_sha256": sha256_bytes(base_prompt_bytes),
        "card_sha256": unit.card_sha256,
        "codex_bin": source_run_manifest["codex_bin"],
        "image_attachment_set_sha256": _canonical_sha256(image_attachments),
        "image_attachment_total_bytes": sum(record["byte_length"] for record in image_attachments),
        "image_attachments": image_attachments,
        "max_attempts": source_run_manifest["max_attempts"],
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "model": source_run_manifest["primary_model"],
        "phase": "A",
        "receipt_binding": receipt_binding,
        "receipt_binding_sha256": _canonical_sha256(receipt_binding),
        "request_payload_overhead_bytes": REQUEST_PAYLOAD_OVERHEAD_BYTES,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "reviewer_id": reviewer_id,
        "runtime_schema_version": LEGACY_RUNTIME_SCHEMA_VERSION,
        "schema_sha256": sha256_bytes(schema_bytes),
        "stage": "PRIMARY",
        "task_key": unit.task_key,
        "timeout_seconds": source_run_manifest["timeout_seconds"],
        "unit_id": unit.unit_id,
    }
    return expected_receipt, render_prompt, validate_response


def _validate_legacy_primary_source_run_manifest(
    source_run_manifest: Mapping[str, Any],
    *,
    bundle_manifest: Mapping[str, Any],
    primary_model: str,
) -> None:
    expected = {
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "causal_claim_supported": False,
        "driver_schema_version": LEGACY_DRIVER_SCHEMA_VERSION,
        "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "outcomes_opened_at_manifest_write": False,
        "phase": "A",
        "phase_a_driver_freeze_sha256": None,
        "primary_model": primary_model,
        "prompt_version": LEGACY_PROMPT_VERSION,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
    }
    for field, expected_value in expected.items():
        if source_run_manifest.get(field) != expected_value:
            raise FailureLinkDriverError(f"legacy primary seed run manifest mismatch: {field}")
    for field in ("primary_reviewer_id", "codex_bin"):
        if not isinstance(source_run_manifest.get(field), str) or not source_run_manifest[field]:
            raise FailureLinkDriverError(
                f"legacy primary seed run manifest field is invalid: {field}"
            )
    for field in ("max_attempts", "timeout_seconds"):
        if type(source_run_manifest.get(field)) is not int or source_run_manifest[field] <= 0:
            raise FailureLinkDriverError(
                f"legacy primary seed run manifest field is invalid: {field}"
            )


def _current_v4_expected_receipt(
    *,
    unit: ReviewUnit,
    card: ReviewCard,
    schema: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
    prompt_tokenizer_asset: Path,
    primary_review: Mapping[str, Any] | None = None,
    secondary_review: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Callable[[str | None], str], Callable[[Mapping[str, Any]], Any]]:
    """Rebuild one current-protocol receipt without invoking a reviewer."""

    stage_manifest_prefix = {
        "PRIMARY": "primary",
        "SECONDARY": "secondary",
        "ADJUDICATION": "adjudicator",
    }[unit.stage]
    reviewer_id = run_manifest[f"{stage_manifest_prefix}_reviewer_id"]
    model = run_manifest[f"{stage_manifest_prefix}_model"]
    identity = _review_identity(
        phase=unit.phase,
        stage=unit.stage,
        reviewer_id=reviewer_id,
        card=card.payload,
        card_sha256=card.card_sha256,
    )
    card_transport_encoding = select_card_transport_encoding(card.payload)
    prompt_card, card_transport = prepare_card_for_prompt(
        card.payload,
        transport_encoding=card_transport_encoding,
    )
    extra_binding: dict[str, Any] = {}
    if unit.stage == "ADJUDICATION":
        if primary_review is None or secondary_review is None:
            raise FailureLinkDriverError(
                f"frozen adjudication lacks both independent reviews: {unit.task_key}"
            )

        def render_prompt(feedback: str | None) -> str:
            prompt = build_adjudication_prompt(
                phase=unit.phase,
                reviewer_id=reviewer_id,
                identity=identity,
                card=card.payload,
                schema=schema,
                primary_review=primary_review,
                secondary_review=secondary_review,
                material_disagreement={
                    "material": True,
                    "public_predicate": (f"phase_{unit.phase.lower()}_material_disagreement"),
                },
                attachment_map=card.attachment_map,
                validation_feedback=feedback,
                card_transport_encoding=card_transport_encoding,
            )
            _validate_prompt_budget(
                prompt,
                unit_id=unit.unit_id,
                tokenizer_asset_path=prompt_tokenizer_asset,
            )
            return prompt

        extra_binding = {
            "primary_review_sha256": _canonical_sha256(primary_review),
            "secondary_review_sha256": _canonical_sha256(secondary_review),
        }
    else:

        def render_prompt(feedback: str | None) -> str:
            prompt = build_review_prompt(
                phase=unit.phase,
                stage=unit.stage,
                reviewer_id=reviewer_id,
                identity=identity,
                card=card.payload,
                schema=schema,
                attachment_map=card.attachment_map,
                validation_feedback=feedback,
                card_transport_encoding=card_transport_encoding,
            )
            _validate_prompt_budget(
                prompt,
                unit_id=unit.unit_id,
                tokenizer_asset_path=prompt_tokenizer_asset,
            )
            return prompt

    def validate_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
        return _validated_review_response(
            response,
            phase=unit.phase,
            card=card,
            identity=identity,
        )

    receipt_binding = {
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "bundle_card_set_sha256": bundle_manifest[
            "phase_a_card_set_sha256" if unit.phase == "A" else "phase_b_card_set_sha256"
        ],
        "causal_claim_supported": False,
        "card_transport_encoding": card_transport_encoding,
        "card_transport_sha256": _canonical_sha256(card_transport),
        "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_prompt_tokens": MAX_PROMPT_TOKENS,
        "phase_a_driver_freeze_sha256": run_manifest["phase_a_driver_freeze_sha256"],
        "prompt_version": PROMPT_VERSION,
        "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
        "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
        "tiktoken_version": TIKTOKEN_VERSION,
        "prompt_card_sha256": _canonical_sha256(prompt_card),
        **extra_binding,
    }
    image_attachments = _review_unit_image_records(unit)
    schema_bytes = canonical_json_bytes(dict(schema))
    base_prompt_bytes = render_prompt(None).encode("utf-8")
    expected_receipt = {
        "base_prompt_sha256": sha256_bytes(base_prompt_bytes),
        "card_sha256": unit.card_sha256,
        "codex_bin": run_manifest["codex_bin"],
        "image_attachment_set_sha256": _canonical_sha256(image_attachments),
        "image_attachment_total_bytes": sum(record["byte_length"] for record in image_attachments),
        "image_attachments": image_attachments,
        "max_attempts": run_manifest["max_attempts"],
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "model": model,
        "phase": unit.phase,
        "receipt_binding": receipt_binding,
        "receipt_binding_sha256": _canonical_sha256(receipt_binding),
        "request_payload_overhead_bytes": REQUEST_PAYLOAD_OVERHEAD_BYTES,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "reviewer_id": reviewer_id,
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "schema_sha256": sha256_bytes(schema_bytes),
        "stage": unit.stage,
        "task_key": unit.task_key,
        "timeout_seconds": run_manifest["timeout_seconds"],
        "unit_id": unit.unit_id,
    }
    return expected_receipt, render_prompt, validate_response


def _prepare_primary_migration(
    *,
    source_root: Path,
    expected_run_manifest_sha256: str,
    expected_source_snapshot_sha256: str,
    expected_accepted_set_sha256: str,
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    primary_model: str,
) -> PrimaryMigrationPlan:
    for name, value in (
        ("seed run manifest", expected_run_manifest_sha256),
        ("seed source snapshot", expected_source_snapshot_sha256),
        ("seed accepted set", expected_accepted_set_sha256),
    ):
        if not _SHA256_RE.fullmatch(value):
            raise FailureLinkDriverError(f"invalid {name} SHA-256 pin")
    pinned_snapshot = _migration_source_snapshot(source_root)
    if _canonical_sha256(pinned_snapshot) != expected_source_snapshot_sha256:
        raise FailureLinkDriverError("legacy primary migration source snapshot SHA-256 mismatch")
    pinned_root = Path(pinned_snapshot["source_root_realpath"])
    if file_sha256(pinned_root / "run_manifest.json") != expected_run_manifest_sha256:
        raise FailureLinkDriverError("legacy primary seed run manifest SHA-256 mismatch")
    plan = _inspect_primary_migration(
        source_root=source_root,
        cards=cards,
        schema=schema,
        bundle_manifest=bundle_manifest,
        primary_model=primary_model,
    )
    if plan.source_snapshot != pinned_snapshot:
        raise FailureLinkDriverError("legacy primary migration source changed after pin validation")
    if plan.source_run_manifest_sha256 != expected_run_manifest_sha256:
        raise FailureLinkDriverError("legacy primary seed run manifest SHA-256 mismatch")
    if plan.accepted_set_sha256 != expected_accepted_set_sha256:
        raise FailureLinkDriverError("legacy primary accepted-set SHA-256 mismatch")
    return plan


def _inspect_primary_migration(
    *,
    source_root: Path,
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    primary_model: str,
) -> PrimaryMigrationPlan:
    """Read-only construct and fully verify a migration plan before pin comparison."""

    snapshot = _migration_source_snapshot(source_root)
    snapshot_sha256 = _canonical_sha256(snapshot)
    root = Path(snapshot["source_root_realpath"])
    run_manifest_path = root / "run_manifest.json"
    source_run_manifest_sha256 = file_sha256(run_manifest_path)
    source_run_manifest = _read_canonical_object(run_manifest_path)
    _validate_legacy_primary_source_run_manifest(
        source_run_manifest,
        bundle_manifest=bundle_manifest,
        primary_model=primary_model,
    )
    expected_input = {
        "cards.jsonl": b"".join(canonical_json_bytes(card.payload) for card in cards),
        "manifest.json": canonical_json_bytes(dict(bundle_manifest)),
        "review_schema.json": canonical_json_bytes(dict(schema)),
    }
    for name, expected_bytes in expected_input.items():
        path = root / "input" / name
        _reject_symlink_path(path, boundary=root)
        try:
            actual_bytes = path.read_bytes()
        except OSError as exc:
            raise FailureLinkDriverError(
                f"cannot read legacy migration input {path}: {exc}"
            ) from exc
        if actual_bytes != expected_bytes:
            raise FailureLinkDriverError(f"legacy migration input mismatch: {name}")

    batch_root = root / "batches" / "phase-a-primary"
    _reject_symlink_path(batch_root, boundary=root)
    if not batch_root.is_dir():
        raise FailureLinkDriverError("legacy migration primary batch root is missing")
    expected_directory_names = {
        f"a-primary-{ordinal:04d}-{card.card_sha256[:12]}"
        for ordinal, card in enumerate(cards, start=1)
    }
    actual_directory_names = {
        path.name for path in batch_root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    unexpected = sorted(actual_directory_names - expected_directory_names)
    if unexpected:
        raise FailureLinkDriverError(
            f"legacy migration has unexpected accepted unit directories: {unexpected}"
        )

    seeds: list[LegacyPrimarySeed] = []
    accepted_records: list[dict[str, Any]] = []
    missing_ordinals: list[int] = []
    missing_task_keys: list[str] = []
    for ordinal, card in enumerate(cards, start=1):
        unit_id = f"a-primary-{ordinal:04d}-{card.card_sha256[:12]}"
        source_directory = batch_root / unit_id
        if not source_directory.exists():
            missing_ordinals.append(ordinal)
            missing_task_keys.append(card.task_key)
            continue
        expected_names = {
            "model_response.json",
            "output_schema.json",
            "receipt.json",
            "response.json",
        }
        actual_names = {path.name for path in source_directory.iterdir() if path.is_file()}
        if actual_names != expected_names:
            raise FailureLinkDriverError(f"legacy migration unit file set mismatch: {unit_id}")
        unit = ReviewUnit(
            phase="A",
            stage="PRIMARY",
            unit_id=unit_id,
            task_key=card.task_key,
            card_sha256=card.card_sha256,
            image_paths=card.image_paths,
            image_sha256s=card.image_sha256s,
            image_byte_lengths=card.image_byte_lengths,
        )
        expected_receipt, render_prompt, validate_response = _legacy_v3_expected_receipt(
            unit=unit,
            card=card,
            schema=schema,
            bundle_manifest=bundle_manifest,
            source_run_manifest=source_run_manifest,
        )
        artifact = verify_frozen_review_artifact(
            unit=unit,
            artifact_root=root,
            target=source_directory,
            schema=schema,
            render_prompt=render_prompt,
            validate_response=validate_response,
            expected_receipt=expected_receipt,
        )
        receipt = _read_canonical_object(source_directory / "receipt.json")
        accepted_records.append(
            {
                "accepted_attempt": receipt["accepted_attempt"],
                "accepted_prompt_sha256": receipt["accepted_prompt_sha256"],
                "attempt_count": receipt["attempt_count"],
                "attempt_prompt_sha256s": [
                    attempt["prompt_sha256"] for attempt in receipt["attempts"]
                ],
                "card_sha256": card.card_sha256,
                "model_response_sha256": receipt["model_response_sha256"],
                "ordinal": ordinal,
                "receipt_sha256": artifact.receipt_sha256,
                "response_sha256": artifact.response_sha256,
                "schema_sha256": receipt["schema_sha256"],
                "source_receipt_relative_path": (source_directory / "receipt.json")
                .relative_to(root)
                .as_posix(),
                "task_key": card.task_key,
                "unit_id": unit_id,
            }
        )
        seeds.append(
            LegacyPrimarySeed(
                ordinal=ordinal,
                unit=unit,
                source_directory=source_directory,
                artifact=artifact,
            )
        )
    if tuple(missing_ordinals) != EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS:
        raise FailureLinkDriverError(
            "legacy primary migration missing ordinals differ from the pinned 42/66 set: "
            f"{missing_ordinals}"
        )
    if len(seeds) != len(cards) - len(EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS):
        raise FailureLinkDriverError("legacy primary migration accepted coverage is not 114/116")
    accepted_set = {
        "records": accepted_records,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
    }
    accepted_set_sha256 = _canonical_sha256(accepted_set)
    if _migration_source_snapshot(root) != snapshot:
        raise FailureLinkDriverError("legacy primary migration source changed during verification")
    return PrimaryMigrationPlan(
        source_root=root,
        source_run_manifest=source_run_manifest,
        source_run_manifest_sha256=source_run_manifest_sha256,
        source_snapshot=snapshot,
        source_snapshot_sha256=snapshot_sha256,
        accepted_set=accepted_set,
        accepted_set_sha256=accepted_set_sha256,
        missing_task_keys=tuple(missing_task_keys),
        seeds=tuple(seeds),
    )


def _materialize_primary_migration(
    *,
    plan: PrimaryMigrationPlan,
    output_root: Path,
    run_manifest: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
) -> PrimaryMigrationOverlay:
    if _migration_source_snapshot(plan.source_root) != plan.source_snapshot:
        raise FailureLinkDriverError(
            "legacy primary migration source changed before materialization"
        )
    migration_root = output_root / "migration" / "v3-primary"
    _reject_symlink_path(migration_root, boundary=output_root)
    source_files = {record["relative_path"]: record for record in plan.source_snapshot["files"]}
    metadata_payloads = {
        "accepted_set.json": canonical_json_bytes(plan.accepted_set),
        "source_run_manifest.json": canonical_json_bytes(plan.source_run_manifest),
        "source_snapshot.json": canonical_json_bytes(plan.source_snapshot),
    }
    for name, payload in metadata_payloads.items():
        write_once(migration_root / name, payload)

    cards_by_key = {card.task_key: card for card in cards}
    artifacts_by_task: dict[str, StageArtifact] = {}
    index_records: list[dict[str, Any]] = []
    for seed in plan.seeds:
        target = migration_root / "artifacts" / seed.unit.unit_id
        for name in (
            "model_response.json",
            "output_schema.json",
            "receipt.json",
            "response.json",
        ):
            source_path = seed.source_directory / name
            relative_source = source_path.relative_to(plan.source_root).as_posix()
            source_record = source_files.get(relative_source)
            if source_record is None:
                raise FailureLinkDriverError(
                    f"legacy artifact is absent from pinned source snapshot: {relative_source}"
                )
            _reject_symlink_path(source_path, boundary=plan.source_root)
            try:
                payload = source_path.read_bytes()
            except OSError as exc:
                raise FailureLinkDriverError(
                    f"cannot copy legacy primary artifact {source_path}: {exc}"
                ) from exc
            if (
                len(payload) != source_record["byte_count"]
                or sha256_bytes(payload) != source_record["sha256"]
            ):
                raise FailureLinkDriverError(
                    f"legacy primary artifact drift before materialization: {relative_source}"
                )
            write_once(target / name, payload)
        _reject_symlink_path(target, boundary=output_root)
        actual_names = {path.name for path in target.iterdir() if path.is_file()}
        if actual_names != {
            "model_response.json",
            "output_schema.json",
            "receipt.json",
            "response.json",
        }:
            raise FailureLinkDriverError(
                f"materialized legacy primary unit file set mismatch: {seed.unit.unit_id}"
            )
        card = cards_by_key[seed.unit.task_key]
        expected_receipt, render_prompt, validate_response = _legacy_v3_expected_receipt(
            unit=seed.unit,
            card=card,
            schema=schema,
            bundle_manifest=bundle_manifest,
            source_run_manifest=plan.source_run_manifest,
        )
        artifact = verify_frozen_review_artifact(
            unit=seed.unit,
            artifact_root=output_root,
            target=target,
            schema=schema,
            render_prompt=render_prompt,
            validate_response=validate_response,
            expected_receipt=expected_receipt,
        )
        artifacts_by_task[seed.unit.task_key] = artifact
        index_records.append(
            {
                "card_sha256": seed.unit.card_sha256,
                "materialized_receipt_relative_path": (target / "receipt.json")
                .relative_to(output_root)
                .as_posix(),
                "model_response_sha256": file_sha256(target / "model_response.json"),
                "ordinal": seed.ordinal,
                "origin": LEGACY_PRIMARY_ORIGIN,
                "prompt_version": LEGACY_PROMPT_VERSION,
                "receipt_sha256": artifact.receipt_sha256,
                "response_sha256": artifact.response_sha256,
                "runtime_schema_version": LEGACY_RUNTIME_SCHEMA_VERSION,
                "schema_sha256": file_sha256(target / "output_schema.json"),
                "source_receipt_relative_path": (seed.source_directory / "receipt.json")
                .relative_to(plan.source_root)
                .as_posix(),
                "task_key": seed.unit.task_key,
                "transport_encoding": LEGACY_V3_INLINE_CARD_TRANSPORT_ENCODING,
                "unit_id": seed.unit.unit_id,
            }
        )
    if _migration_source_snapshot(plan.source_root) != plan.source_snapshot:
        raise FailureLinkDriverError(
            "legacy primary migration source changed during materialization"
        )
    index = {
        "records": index_records,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
    }
    index_bytes = canonical_json_bytes(index)
    write_once(migration_root / "index.json", index_bytes)
    freeze = {
        "accepted_set_sha256": plan.accepted_set_sha256,
        "current_bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "current_input_cards_sha256": file_sha256(output_root / "input" / "cards.jsonl"),
        "current_input_manifest_sha256": file_sha256(output_root / "input" / "manifest.json"),
        "current_review_schema_sha256": file_sha256(output_root / "input" / "review_schema.json"),
        "current_run_manifest_sha256": _canonical_sha256(run_manifest),
        "index_sha256": sha256_bytes(index_bytes),
        "legacy_primary_count": len(index_records),
        "missing_task_keys": list(plan.missing_task_keys),
        "new_current_primary_count": len(plan.missing_task_keys),
        "outcomes_opened": False,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
        "source_root_realpath": str(plan.source_root),
        "source_run_manifest_sha256": plan.source_run_manifest_sha256,
        "source_snapshot_sha256": plan.source_snapshot_sha256,
    }
    freeze_bytes = canonical_json_bytes(freeze)
    write_once(migration_root / "freeze.json", freeze_bytes)
    _verify_materialized_migration_tree(
        migration_root=migration_root,
        expected_unit_ids={seed.unit.unit_id for seed in plan.seeds},
    )
    return PrimaryMigrationOverlay(
        freeze=freeze,
        freeze_sha256=sha256_bytes(freeze_bytes),
        source_run_manifest=plan.source_run_manifest,
        source_snapshot=plan.source_snapshot,
        accepted_set=plan.accepted_set,
        missing_task_keys=plan.missing_task_keys,
        artifacts_by_task=artifacts_by_task,
    )


def _verify_materialized_migration_tree(
    *, migration_root: Path, expected_unit_ids: set[str]
) -> None:
    _reject_symlink_path(migration_root, boundary=migration_root)
    if not migration_root.is_dir():
        raise FailureLinkDriverError("materialized migration root is missing")
    expected_root_files = {
        "accepted_set.json",
        "freeze.json",
        "index.json",
        "source_run_manifest.json",
        "source_snapshot.json",
    }
    actual_root_files = {path.name for path in migration_root.iterdir() if path.is_file()}
    if actual_root_files != expected_root_files:
        raise FailureLinkDriverError("materialized migration root file set mismatch")
    actual_root_directories = {path.name for path in migration_root.iterdir() if path.is_dir()}
    if actual_root_directories != {"artifacts"}:
        raise FailureLinkDriverError("materialized migration root directory set mismatch")
    if {path.name for path in migration_root.iterdir()} != expected_root_files | {"artifacts"}:
        raise FailureLinkDriverError("materialized migration root entry set mismatch")
    artifacts_root = migration_root / "artifacts"
    _reject_symlink_path(artifacts_root, boundary=migration_root)
    if not artifacts_root.is_dir():
        raise FailureLinkDriverError("materialized migration artifact root is missing")
    actual_units = {path.name for path in artifacts_root.iterdir() if path.is_dir()}
    if actual_units != expected_unit_ids:
        raise FailureLinkDriverError("materialized migration unit set mismatch")
    if {path.name for path in artifacts_root.iterdir()} != expected_unit_ids:
        raise FailureLinkDriverError("materialized migration artifact entry set mismatch")
    expected_unit_files = {
        "model_response.json",
        "output_schema.json",
        "receipt.json",
        "response.json",
    }
    for unit_id in expected_unit_ids:
        unit_root = artifacts_root / unit_id
        if {path.name for path in unit_root.iterdir()} != expected_unit_files or any(
            not path.is_file() for path in unit_root.iterdir()
        ):
            raise FailureLinkDriverError(
                f"materialized migration unit file set mismatch: {unit_id}"
            )
    for path in migration_root.rglob("*"):
        _reject_symlink_path(path, boundary=migration_root)


def _primary_migration_manifest(
    plan: PrimaryMigrationPlan | None, *, task_count: int, phase: str
) -> tuple[dict[str, int], dict[str, Any] | None]:
    if phase != "A" or plan is None:
        return {CURRENT_PRIMARY_ORIGIN: task_count, LEGACY_PRIMARY_ORIGIN: 0}, None
    return (
        {
            CURRENT_PRIMARY_ORIGIN: len(plan.missing_task_keys),
            LEGACY_PRIMARY_ORIGIN: len(plan.seeds),
        },
        {
            "accepted_set_sha256": plan.accepted_set_sha256,
            "missing_task_keys": list(plan.missing_task_keys),
            "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
            "source_root_realpath": str(plan.source_root),
            "source_run_manifest_sha256": plan.source_run_manifest_sha256,
            "source_snapshot_sha256": plan.source_snapshot_sha256,
        },
    )


def _validate_primary_migration_reviewer_independence(
    plan: PrimaryMigrationPlan | None,
    *,
    primary_reviewer_id: str,
    secondary_reviewer_id: str,
    adjudicator_reviewer_id: str,
) -> None:
    if plan is None:
        return
    legacy_primary_reviewer_id = plan.source_run_manifest["primary_reviewer_id"]
    if legacy_primary_reviewer_id in {
        primary_reviewer_id,
        secondary_reviewer_id,
        adjudicator_reviewer_id,
    }:
        raise FailureLinkDriverError(
            "current Phase-A reviewer identities must be distinct from the migrated "
            f"legacy primary reviewer: {legacy_primary_reviewer_id}"
        )


def _load_primary_migration_overlay(
    *,
    output_root: Path,
    run_manifest: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
) -> PrimaryMigrationOverlay | None:
    summary = run_manifest.get("primary_migration")
    origins = run_manifest.get("primary_review_origin_counts")
    if summary is None:
        if origins != {CURRENT_PRIMARY_ORIGIN: len(cards), LEGACY_PRIMARY_ORIGIN: 0}:
            raise FailureLinkDriverError("run manifest primary origin counts are invalid")
        return None
    if not isinstance(summary, Mapping) or not isinstance(origins, Mapping):
        raise FailureLinkDriverError("run manifest primary migration declaration is invalid")
    migration_root = output_root / "migration" / "v3-primary"
    _reject_symlink_path(migration_root, boundary=output_root)
    accepted_set = _read_canonical_object(migration_root / "accepted_set.json")
    freeze = _read_canonical_object(migration_root / "freeze.json")
    index = _read_canonical_object(migration_root / "index.json")
    source_run_manifest = _read_canonical_object(migration_root / "source_run_manifest.json")
    source_snapshot = _read_canonical_object(migration_root / "source_snapshot.json")
    _validate_legacy_primary_source_run_manifest(
        source_run_manifest,
        bundle_manifest=bundle_manifest,
        primary_model=run_manifest["primary_model"],
    )
    source_snapshot_files = _validate_materialized_source_snapshot(
        source_snapshot=source_snapshot,
        source_run_manifest=source_run_manifest,
        output_root=output_root,
        declared_source_root=freeze.get("source_root_realpath"),
    )
    frozen_missing_task_keys = freeze.get("missing_task_keys")
    expected_missing_task_keys = [
        cards[ordinal - 1].task_key for ordinal in EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS
    ]
    if frozen_missing_task_keys != expected_missing_task_keys:
        raise FailureLinkDriverError("materialized migration missing-task set is invalid")
    expected_summary = {
        "accepted_set_sha256": _canonical_sha256(accepted_set),
        "missing_task_keys": expected_missing_task_keys,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
        "source_root_realpath": freeze.get("source_root_realpath"),
        "source_run_manifest_sha256": _canonical_sha256(source_run_manifest),
        "source_snapshot_sha256": _canonical_sha256(source_snapshot),
    }
    if dict(summary) != expected_summary:
        raise FailureLinkDriverError("run manifest primary migration summary mismatch")
    expected_origins = {
        CURRENT_PRIMARY_ORIGIN: len(expected_summary["missing_task_keys"]),
        LEGACY_PRIMARY_ORIGIN: len(accepted_set.get("records", [])),
    }
    if dict(origins) != expected_origins:
        raise FailureLinkDriverError("run manifest primary migration counts mismatch")
    expected_freeze_fields = {
        "accepted_set_sha256": _canonical_sha256(accepted_set),
        "current_bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "current_input_cards_sha256": file_sha256(output_root / "input" / "cards.jsonl"),
        "current_input_manifest_sha256": file_sha256(output_root / "input" / "manifest.json"),
        "current_review_schema_sha256": file_sha256(output_root / "input" / "review_schema.json"),
        "current_run_manifest_sha256": _canonical_sha256(run_manifest),
        "index_sha256": _canonical_sha256(index),
        "legacy_primary_count": expected_origins[LEGACY_PRIMARY_ORIGIN],
        "missing_task_keys": expected_summary["missing_task_keys"],
        "new_current_primary_count": expected_origins[CURRENT_PRIMARY_ORIGIN],
        "outcomes_opened": False,
        "schema_version": PRIMARY_MIGRATION_SCHEMA_VERSION,
        "source_root_realpath": expected_summary["source_root_realpath"],
        "source_run_manifest_sha256": expected_summary["source_run_manifest_sha256"],
        "source_snapshot_sha256": expected_summary["source_snapshot_sha256"],
    }
    if freeze != expected_freeze_fields:
        raise FailureLinkDriverError("materialized primary migration freeze mismatch")
    if index.get("schema_version") != PRIMARY_MIGRATION_SCHEMA_VERSION or not isinstance(
        index.get("records"), list
    ):
        raise FailureLinkDriverError("materialized primary migration index is invalid")
    if accepted_set.get("schema_version") != PRIMARY_MIGRATION_SCHEMA_VERSION or not isinstance(
        accepted_set.get("records"), list
    ):
        raise FailureLinkDriverError("materialized primary accepted set is invalid")
    if len(accepted_set["records"]) != len(cards) - len(EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS):
        raise FailureLinkDriverError("materialized primary accepted coverage is not 114/116")
    if any(not isinstance(record, Mapping) for record in accepted_set["records"]) or any(
        not isinstance(record, Mapping) for record in index["records"]
    ):
        raise FailureLinkDriverError("materialized primary migration row is invalid")
    expected_accepted_ordinals = [
        ordinal
        for ordinal in range(1, len(cards) + 1)
        if ordinal not in EXPECTED_LEGACY_PRIMARY_MISSING_ORDINALS
    ]
    if [record.get("ordinal") for record in accepted_set["records"]] != (
        expected_accepted_ordinals
    ) or [record.get("ordinal") for record in index["records"]] != (expected_accepted_ordinals):
        raise FailureLinkDriverError("materialized primary migration ordering is invalid")
    accepted_by_task = {record["task_key"]: record for record in accepted_set["records"]}
    cards_by_key = {card.task_key: card for card in cards}
    artifacts_by_task: dict[str, StageArtifact] = {}
    unit_ids: set[str] = set()
    seen_tasks: set[str] = set()
    for raw in index["records"]:
        if not isinstance(raw, Mapping):
            raise FailureLinkDriverError("materialized primary migration index row is invalid")
        task_key = raw.get("task_key")
        if task_key in seen_tasks or task_key not in cards_by_key:
            raise FailureLinkDriverError("materialized migration task coverage is invalid")
        seen_tasks.add(task_key)
        accepted = accepted_by_task.get(task_key)
        if not isinstance(accepted, Mapping):
            raise FailureLinkDriverError("migration index lacks accepted-set binding")
        for field in (
            "card_sha256",
            "model_response_sha256",
            "ordinal",
            "receipt_sha256",
            "response_sha256",
            "schema_sha256",
            "source_receipt_relative_path",
            "task_key",
            "unit_id",
        ):
            if raw.get(field) != accepted.get(field):
                raise FailureLinkDriverError(f"migration index accepted-set mismatch: {field}")
        if (
            raw.get("origin") != LEGACY_PRIMARY_ORIGIN
            or raw.get("prompt_version") != LEGACY_PROMPT_VERSION
            or raw.get("runtime_schema_version") != LEGACY_RUNTIME_SCHEMA_VERSION
            or raw.get("transport_encoding") != LEGACY_V3_INLINE_CARD_TRANSPORT_ENCODING
        ):
            raise FailureLinkDriverError("migration index legacy protocol declaration mismatch")
        ordinal = raw["ordinal"]
        if type(ordinal) is not int or not 1 <= ordinal <= len(cards):
            raise FailureLinkDriverError("migration index ordinal is invalid")
        card = cards[ordinal - 1]
        if card.task_key != task_key or card.card_sha256 != raw["card_sha256"]:
            raise FailureLinkDriverError("migration index ordinal/card binding mismatch")
        unit_id = f"a-primary-{ordinal:04d}-{card.card_sha256[:12]}"
        if raw["unit_id"] != unit_id:
            raise FailureLinkDriverError("migration index unit identity mismatch")
        expected_source_receipt = (
            Path("batches") / "phase-a-primary" / unit_id / "receipt.json"
        ).as_posix()
        if raw["source_receipt_relative_path"] != expected_source_receipt:
            raise FailureLinkDriverError("migration index source receipt path mismatch")
        relative_receipt = _safe_relative_path(
            raw.get("materialized_receipt_relative_path"),
            "migration.materialized_receipt_relative_path",
        )
        target = (output_root / Path(*relative_receipt.parts)).parent
        expected_target = migration_root / "artifacts" / unit_id
        if target != expected_target:
            raise FailureLinkDriverError("migration index target path mismatch")
        source_unit_root = f"batches/phase-a-primary/{unit_id}"
        for name, expected_sha256 in (
            ("model_response.json", raw["model_response_sha256"]),
            ("output_schema.json", raw["schema_sha256"]),
            ("receipt.json", raw["receipt_sha256"]),
            ("response.json", raw["response_sha256"]),
        ):
            source_record = source_snapshot_files.get(f"{source_unit_root}/{name}")
            materialized_path = target / name
            if (
                source_record is None
                or source_record["sha256"] != expected_sha256
                or source_record["sha256"] != file_sha256(materialized_path)
                or source_record["byte_count"] != materialized_path.stat().st_size
            ):
                raise FailureLinkDriverError(
                    f"migration source snapshot/materialized copy mismatch: {unit_id}/{name}"
                )
        unit = ReviewUnit(
            phase="A",
            stage="PRIMARY",
            unit_id=unit_id,
            task_key=task_key,
            card_sha256=card.card_sha256,
            image_paths=card.image_paths,
            image_sha256s=card.image_sha256s,
            image_byte_lengths=card.image_byte_lengths,
        )
        expected_receipt, render_prompt, validate_response = _legacy_v3_expected_receipt(
            unit=unit,
            card=card,
            schema=schema,
            bundle_manifest=bundle_manifest,
            source_run_manifest=source_run_manifest,
        )
        artifact = verify_frozen_review_artifact(
            unit=unit,
            artifact_root=output_root,
            target=target,
            schema=schema,
            render_prompt=render_prompt,
            validate_response=validate_response,
            expected_receipt=expected_receipt,
        )
        if (
            artifact.receipt_sha256 != raw["receipt_sha256"]
            or artifact.response_sha256 != raw["response_sha256"]
        ):
            raise FailureLinkDriverError("migration index materialized artifact hash mismatch")
        artifacts_by_task[task_key] = artifact
        unit_ids.add(unit_id)
    if set(accepted_by_task) != seen_tasks:
        raise FailureLinkDriverError("migration index/accepted-set coverage mismatch")
    missing_task_keys = tuple(expected_summary["missing_task_keys"])
    if seen_tasks | set(missing_task_keys) != set(cards_by_key) or seen_tasks & set(
        missing_task_keys
    ):
        raise FailureLinkDriverError("migration accepted/missing task partition is invalid")
    _verify_materialized_migration_tree(
        migration_root=migration_root,
        expected_unit_ids=unit_ids,
    )
    return PrimaryMigrationOverlay(
        freeze=freeze,
        freeze_sha256=file_sha256(migration_root / "freeze.json"),
        source_run_manifest=source_run_manifest,
        source_snapshot=source_snapshot,
        accepted_set=accepted_set,
        missing_task_keys=missing_task_keys,
        artifacts_by_task=artifacts_by_task,
    )


def _validate_materialized_source_snapshot(
    *,
    source_snapshot: Mapping[str, Any],
    source_run_manifest: Mapping[str, Any],
    output_root: Path,
    declared_source_root: Any,
) -> dict[str, Mapping[str, Any]]:
    if (
        source_snapshot.get("schema_version") != PRIMARY_MIGRATION_SCHEMA_VERSION
        or source_snapshot.get("source_root_realpath") != declared_source_root
        or not isinstance(declared_source_root, str)
        or not Path(declared_source_root).is_absolute()
    ):
        raise FailureLinkDriverError("materialized migration source snapshot identity is invalid")
    directories = source_snapshot.get("directories")
    files = source_snapshot.get("files")
    if not isinstance(directories, list) or not isinstance(files, list):
        raise FailureLinkDriverError("materialized migration source snapshot is malformed")
    if any(
        not isinstance(path, str) or not _allowed_migration_source_directory(path)
        for path in directories
    ) or directories != sorted(set(directories)):
        raise FailureLinkDriverError("materialized migration source directories are invalid")
    files_by_path: dict[str, Mapping[str, Any]] = {}
    for record in files:
        if (
            not isinstance(record, Mapping)
            or set(record) != {"byte_count", "relative_path", "sha256"}
            or not isinstance(record.get("relative_path"), str)
            or not _allowed_migration_source_file(record["relative_path"])
            or type(record.get("byte_count")) is not int
            or record["byte_count"] < 0
            or not isinstance(record.get("sha256"), str)
            or not _SHA256_RE.fullmatch(record["sha256"])
            or record["relative_path"] in files_by_path
        ):
            raise FailureLinkDriverError("materialized migration source file row is invalid")
        files_by_path[record["relative_path"]] = record
    if list(files_by_path) != sorted(files_by_path):
        raise FailureLinkDriverError("materialized migration source files are not ordered")
    expected_bound_files = {
        "run_manifest.json": canonical_json_bytes(dict(source_run_manifest)),
        "input/cards.jsonl": (output_root / "input" / "cards.jsonl").read_bytes(),
        "input/manifest.json": (output_root / "input" / "manifest.json").read_bytes(),
        "input/review_schema.json": (output_root / "input" / "review_schema.json").read_bytes(),
    }
    for relative, payload in expected_bound_files.items():
        record = files_by_path.get(relative)
        if (
            record is None
            or record["byte_count"] != len(payload)
            or record["sha256"] != sha256_bytes(payload)
        ):
            raise FailureLinkDriverError(
                f"materialized migration source snapshot binding mismatch: {relative}"
            )
    return files_by_path


def _validate_review_card_attachments(card: ReviewCard) -> None:
    if not (len(card.image_paths) == len(card.image_sha256s) == len(card.image_byte_lengths)):
        raise FailureLinkDriverError(f"image attachment vectors do not align for {card.task_key}")
    if len(card.image_paths) > MAX_ATTACHMENTS_PER_CARD:
        raise FailureLinkDriverError(
            f"too many evidence images for {card.task_key}: {len(card.image_paths)}"
        )
    total_bytes = sum(card.image_byte_lengths)
    if total_bytes > MAX_ATTACHMENT_TOTAL_BYTES:
        raise FailureLinkDriverError(
            f"evidence image bytes exceed hard limit for {card.task_key}: "
            f"{total_bytes} > {MAX_ATTACHMENT_TOTAL_BYTES}"
        )
    for index, (path, expected_sha256, expected_byte_length) in enumerate(
        zip(
            card.image_paths,
            card.image_sha256s,
            card.image_byte_lengths,
            strict=True,
        ),
        start=1,
    ):
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise FailureLinkDriverError(
                f"invalid image digest for {card.task_key} attachment {index}"
            )
        if expected_byte_length <= 0:
            raise FailureLinkDriverError(
                f"invalid image byte length for {card.task_key} attachment {index}"
            )
        try:
            actual_byte_length = path.stat().st_size
        except OSError as exc:
            raise FailureLinkDriverError(f"cannot stat evidence image {path}: {exc}") from exc
        if actual_byte_length != expected_byte_length:
            raise FailureLinkDriverError(
                f"evidence image byte length drift for {card.task_key} attachment {index}"
            )
        if file_sha256(path) != expected_sha256:
            raise FailureLinkDriverError(
                f"evidence image SHA-256 drift for {card.task_key} attachment {index}"
            )


def _review_card_image_records(card: ReviewCard) -> list[dict[str, Any]]:
    _validate_review_card_attachments(card)
    return [
        {
            "attachment_index": index,
            "blob_sha256": digest,
            "byte_length": byte_length,
        }
        for index, (digest, byte_length) in enumerate(
            zip(card.image_sha256s, card.image_byte_lengths, strict=True),
            start=1,
        )
    ]


def _review_unit_image_records(unit: ReviewUnit) -> list[dict[str, Any]]:
    return [
        {
            "attachment_index": index,
            "blob_sha256": digest,
            "byte_length": byte_length,
        }
        for index, (digest, byte_length) in enumerate(
            zip(unit.image_sha256s, unit.image_byte_lengths, strict=True),
            start=1,
        )
    ]


def _load_review_cards(
    cards: Sequence[Mapping[str, Any]], *, source_base: Path
) -> tuple[ReviewCard, ...]:
    _reject_symlink_path(source_base, boundary=Path(source_base.absolute().anchor))
    base = source_base.resolve(strict=True)
    digest_cache: dict[tuple[Path, str], Path | None] = {}
    result = []
    for raw_card in cards:
        card = dict(raw_card)
        card_sha256 = _canonical_sha256(card)
        task = card["task"]
        run_root = _source_run_root(base, task["source_relative_run_path"])
        attachments = _card_attachments(card, run_root=run_root, digest_cache=digest_cache)
        digest_by_path: dict[Path, str] = {}
        for attachment in attachments:
            prior_digest = digest_by_path.setdefault(attachment.path, attachment.blob_sha256)
            if prior_digest != attachment.blob_sha256:
                raise FailureLinkDriverError(
                    f"one evidence image path has conflicting digests: {attachment.path}"
                )
        unique_paths = tuple(digest_by_path)
        image_sha256s = tuple(digest_by_path[path] for path in unique_paths)
        try:
            image_byte_lengths = tuple(path.stat().st_size for path in unique_paths)
        except OSError as exc:
            raise FailureLinkDriverError(f"cannot stat evidence image: {exc}") from exc
        indices = {path: index for index, path in enumerate(unique_paths, start=1)}
        byte_lengths_by_path = dict(zip(unique_paths, image_byte_lengths, strict=True))
        attachment_map = tuple(
            {
                "attachment_index": indices[item.path],
                "blob_sha256": item.blob_sha256,
                "byte_length": byte_lengths_by_path[item.path],
                "ref_id": item.ref_id,
                "role": item.role,
                "step": item.step,
            }
            for item in attachments
        )
        result.append(
            ReviewCard(
                payload=card,
                card_sha256=card_sha256,
                attachments=attachments,
                image_paths=unique_paths,
                image_sha256s=image_sha256s,
                image_byte_lengths=image_byte_lengths,
                attachment_map=attachment_map,
            )
        )
        _validate_review_card_attachments(result[-1])
    return tuple(result)


def _card_attachments(
    card: Mapping[str, Any],
    *,
    run_root: Path,
    digest_cache: dict[tuple[Path, str], Path | None],
) -> tuple[Attachment, ...]:
    requested: list[tuple[str, str, int, str]] = []
    for frozen in card["frozen_strict_mhr_chains"]:
        candidate = frozen["candidate"]
        candidate_id = candidate["candidate_id"]
        for evidence in candidate["evidence_refs"]:
            digest = evidence.get("blob_sha256")
            if isinstance(digest, str):
                requested.append(
                    (
                        digest,
                        str(evidence["role"]),
                        int(evidence["step"]),
                        f"{candidate_id}/{evidence['ref_id']}",
                    )
                )
    for trace_name in ("prefix_trace", "terminal_trace"):
        for step in card[trace_name]["steps"]:
            step_index = int(step["step_index"])
            for state_key, state_role in (
                ("state_before", "pre"),
                ("state_after", "post"),
            ):
                role = f"{trace_name}_{state_role}"
                state = step.get(state_key)
                observation = state.get("observation") if isinstance(state, Mapping) else None
                digest = _observation_screenshot_digest(observation)
                if digest is not None:
                    requested.append((digest, role, step_index, f"{role}/{step_index}"))
    attachments: list[Attachment] = []
    seen: set[tuple[str, str, int, str]] = set()
    for digest, role, step, ref_id in sorted(
        requested, key=lambda item: (item[2], item[1], item[0], item[3])
    ):
        identity = (digest, role, step, ref_id)
        if identity in seen:
            continue
        seen.add(identity)
        path = _resolve_image_blob(run_root, digest, digest_cache=digest_cache)
        if path is None:
            continue
        attachments.append(
            Attachment(
                blob_sha256=digest,
                path=path,
                ref_id=ref_id,
                role=role,
                step=step,
            )
        )
    return tuple(attachments)


def _observation_screenshot_digest(observation: Any) -> str | None:
    if not isinstance(observation, Mapping):
        return None
    screenshot = observation.get("screenshot")
    if not isinstance(screenshot, Mapping):
        return None
    pixel_blob = screenshot.get("pixel_blob")
    if not isinstance(pixel_blob, Mapping):
        return None
    digest = pixel_blob.get("digest")
    return digest if isinstance(digest, str) and _SHA256_RE.fullmatch(digest) else None


def _resolve_image_blob(
    run_root: Path,
    digest: str,
    *,
    digest_cache: dict[tuple[Path, str], Path | None],
) -> Path | None:
    if not _SHA256_RE.fullmatch(digest):
        raise FailureLinkDriverError(f"invalid evidence blob digest: {digest!r}")
    cache_key = (run_root, digest)
    if cache_key in digest_cache:
        return digest_cache[cache_key]
    unresolved = run_root / "blobs" / "sha256" / digest[:2] / digest
    _reject_symlink_path(unresolved, boundary=run_root)
    path = unresolved.resolve(strict=True)
    try:
        path.relative_to(run_root)
    except ValueError as exc:
        raise FailureLinkDriverError(f"evidence blob escapes source run: {path}") from exc
    if not path.is_file() or file_sha256(path) != digest:
        raise FailureLinkDriverError(f"evidence blob failed strong verification: {path}")
    if not _supported_image(path):
        digest_cache[cache_key] = None
        return None
    digest_cache[cache_key] = path
    return path


def _supported_image(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(16)
    return bool(
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def _source_run_root(source_base: Path, relative_text: str) -> Path:
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise FailureLinkDriverError(f"unsafe source run path: {relative_text!r}")
    unresolved = source_base / Path(*relative.parts)
    _reject_symlink_path(unresolved, boundary=source_base)
    run_root = unresolved.resolve(strict=True)
    try:
        run_root.relative_to(source_base)
    except ValueError as exc:
        raise FailureLinkDriverError(f"source run escapes --source-base: {run_root}") from exc
    if not run_root.is_dir():
        raise FailureLinkDriverError(f"source run is not a directory: {run_root}")
    return run_root


def _verify_phase_a_driver_freeze(
    *,
    phase_a_root: Path,
    expected_freeze_sha256: str,
    phase_a_bundle: PhaseABundle,
    source_base: Path,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
    expected_task_count: int,
    expected_chain_count: int,
) -> tuple[PhaseAResolution, dict[str, Any]]:
    _reject_symlink_path(phase_a_root, boundary=Path(phase_a_root.absolute().anchor))
    root = phase_a_root.resolve(strict=True)
    freeze_path = root / "driver_freeze.json"
    _reject_symlink_path(freeze_path, boundary=root)
    if file_sha256(freeze_path) != expected_freeze_sha256:
        raise FailureLinkDriverError("Phase-A driver freeze SHA-256 mismatch")
    freeze = _read_canonical_object(freeze_path)
    expected_freeze_protocol = {
        "causal_claim_supported": False,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "outcomes_opened": False,
        "phase": "A",
        "phase_a_driver_freeze_sha256": None,
        "prompt_version": PROMPT_VERSION,
    }
    for field, expected in expected_freeze_protocol.items():
        if freeze.get(field) != expected:
            raise FailureLinkDriverError(f"invalid Phase-A driver freeze protocol: {field}")
    run_manifest_path = root / "run_manifest.json"
    _reject_symlink_path(run_manifest_path, boundary=root)
    run_manifest = _read_canonical_object(run_manifest_path)
    if freeze.get("run_manifest_sha256") != _canonical_sha256(run_manifest):
        raise FailureLinkDriverError("Phase-A frozen run manifest mismatch")
    _validate_run_manifest_prompt_contract(run_manifest, phase="A")
    if run_manifest.get("bundle_manifest_sha256") != _canonical_sha256(phase_a_bundle.manifest):
        raise FailureLinkDriverError("Phase-A run manifest bundle binding mismatch")
    configured_reviewer_ids = sorted(
        {
            run_manifest["primary_reviewer_id"],
            run_manifest["secondary_reviewer_id"],
            run_manifest["adjudicator_reviewer_id"],
        }
    )
    if freeze.get("configured_reviewer_ids") != configured_reviewer_ids:
        raise FailureLinkDriverError("Phase-A configured reviewer identity set mismatch")
    _verify_bundle_snapshot(phase_a_bundle, root / "input", phase="A")
    resolution_path = root / "resolution"
    _reject_resolution_symlinks(resolution_path, phase="A")
    resolution = load_phase_a_resolution(resolution_path, phase_a_bundle)
    _validate_resolution_common(
        resolution,
        phase="A",
        expected_task_count=expected_task_count,
        expected_chain_count=expected_chain_count,
    )
    if freeze.get("bundle_manifest_sha256") != _canonical_sha256(phase_a_bundle.manifest):
        raise FailureLinkDriverError("Phase-A frozen bundle manifest mismatch")
    if freeze.get("resolution_manifest_sha256") != _canonical_sha256(resolution.manifest):
        raise FailureLinkDriverError("Phase-A frozen resolution manifest mismatch")
    phase_a_cards = _load_review_cards(phase_a_bundle.cards, source_base=source_base)
    primary_migration = _load_primary_migration_overlay(
        output_root=root,
        run_manifest=run_manifest,
        bundle_manifest=phase_a_bundle.manifest,
        cards=phase_a_cards,
        schema=phase_a_review_schema(),
    )
    expected_migration_freeze_sha256 = (
        primary_migration.freeze_sha256 if primary_migration is not None else None
    )
    if freeze.get("primary_migration_freeze_sha256") != expected_migration_freeze_sha256:
        raise FailureLinkDriverError("Phase-A primary migration freeze binding mismatch")
    if freeze.get("primary_review_origin_counts") != run_manifest.get(
        "primary_review_origin_counts"
    ):
        raise FailureLinkDriverError("Phase-A primary review origin count mismatch")
    expected_receipt_reviewer_ids = {
        run_manifest["primary_reviewer_id"],
        run_manifest["secondary_reviewer_id"],
    }
    if resolution.manifest["counts"]["adjudication_review_count"]:
        expected_receipt_reviewer_ids.add(run_manifest["adjudicator_reviewer_id"])
    if primary_migration is not None:
        expected_receipt_reviewer_ids.add(
            primary_migration.source_run_manifest["primary_reviewer_id"]
        )
    if freeze.get("receipt_reviewer_ids") != sorted(expected_receipt_reviewer_ids):
        raise FailureLinkDriverError("Phase-A receipt reviewer identity set mismatch")
    _verify_current_review_artifacts(
        output_root=root,
        raw_records=freeze.get("receipts"),
        resolution=resolution,
        bundle_manifest=phase_a_bundle.manifest,
        cards=phase_a_cards,
        schema=phase_a_review_schema(),
        run_manifest=run_manifest,
        primary_migration=primary_migration,
        prompt_tokenizer_asset=prompt_tokenizer_asset,
    )
    _verify_receipt_index(
        root,
        freeze.get("receipts"),
        resolution=resolution,
        bundle_manifest=phase_a_bundle.manifest,
        cards=phase_a_cards,
        primary_migration=primary_migration,
    )
    return resolution, freeze


def _verify_current_review_artifacts(
    *,
    output_root: Path,
    raw_records: Any,
    resolution: PhaseAResolution | PhaseBResolution,
    bundle_manifest: Mapping[str, Any],
    cards: Sequence[ReviewCard],
    schema: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
    primary_migration: PrimaryMigrationOverlay | None,
    prompt_tokenizer_asset: Path,
) -> None:
    """Fully replay current-protocol prompt/receipt chains without subprocesses."""

    if not isinstance(raw_records, list):
        raise FailureLinkDriverError("driver freeze receipt index is invalid")
    cards_by_key = {card.task_key: card for card in cards}
    card_ordinals = {card.task_key: ordinal for ordinal, card in enumerate(cards, start=1)}
    primary_by_key = {review["task_key"]: review for review in resolution.primary_reviews}
    secondary_by_key = {review["task_key"]: review for review in resolution.secondary_reviews}
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise FailureLinkDriverError("driver freeze receipt row is invalid")
        task_key = raw.get("task_key")
        stage = raw.get("stage")
        is_legacy = bool(
            primary_migration is not None
            and stage == "PRIMARY"
            and task_key in primary_migration.artifacts_by_task
        )
        if is_legacy:
            continue
        if task_key not in cards_by_key or stage not in {
            "PRIMARY",
            "SECONDARY",
            "ADJUDICATION",
        }:
            raise FailureLinkDriverError("current frozen review identity is invalid")
        card = cards_by_key[task_key]
        ordinal = card_ordinals[task_key]
        unit_id = (
            f"{run_manifest['phase'].lower()}-{stage.lower()}-{ordinal:04d}-{card.card_sha256[:12]}"
        )
        if raw.get("unit_id") != unit_id:
            raise FailureLinkDriverError("current frozen review unit identity mismatch")
        expected_relative = (
            Path("batches")
            / f"phase-{run_manifest['phase'].lower()}-{stage.lower()}"
            / unit_id
            / "receipt.json"
        )
        relative = _safe_relative_path(raw.get("receipt_path"), "current receipt path")
        if relative != PurePosixPath(expected_relative.as_posix()):
            raise FailureLinkDriverError("current frozen review receipt path mismatch")
        target = (output_root / expected_relative).parent
        unit = ReviewUnit(
            phase=run_manifest["phase"],
            stage=stage,
            unit_id=unit_id,
            task_key=task_key,
            card_sha256=card.card_sha256,
            image_paths=card.image_paths,
            image_sha256s=card.image_sha256s,
            image_byte_lengths=card.image_byte_lengths,
        )
        expected_receipt, render_prompt, validate_response = _current_v4_expected_receipt(
            unit=unit,
            card=card,
            schema=schema,
            bundle_manifest=bundle_manifest,
            run_manifest=run_manifest,
            prompt_tokenizer_asset=prompt_tokenizer_asset,
            primary_review=primary_by_key.get(task_key),
            secondary_review=secondary_by_key.get(task_key),
        )
        artifact = verify_frozen_review_artifact(
            unit=unit,
            artifact_root=output_root,
            target=target,
            schema=schema,
            render_prompt=render_prompt,
            validate_response=validate_response,
            expected_receipt=expected_receipt,
        )
        if artifact.receipt_sha256 != raw.get(
            "receipt_sha256"
        ) or artifact.response_sha256 != raw.get("response_sha256"):
            raise FailureLinkDriverError("current frozen review artifact hash mismatch")


def _verify_receipt_index(
    output_root: Path,
    raw_records: Any,
    *,
    resolution: PhaseAResolution | PhaseBResolution,
    bundle_manifest: Mapping[str, Any],
    cards: Sequence[ReviewCard],
    primary_migration: PrimaryMigrationOverlay | None = None,
) -> None:
    if not isinstance(raw_records, list):
        raise FailureLinkDriverError("driver freeze receipt index is invalid")
    seen: set[str] = set()
    stage_task_keys: dict[str, set[str]] = {
        "PRIMARY": set(),
        "SECONDARY": set(),
        "ADJUDICATION": set(),
    }
    card_hashes = {card.task_key: card.card_sha256 for card in cards}
    card_transports = {
        card.task_key: select_card_transport_encoding(card.payload) for card in cards
    }
    prompt_card_bindings = {}
    for card in cards:
        prompt_card, transport = prepare_card_for_prompt(
            card.payload,
            transport_encoding=card_transports[card.task_key],
        )
        prompt_card_bindings[card.task_key] = {
            "card_transport_sha256": _canonical_sha256(transport),
            "prompt_card_sha256": _canonical_sha256(prompt_card),
        }
    expected_image_attachments = {card.task_key: _review_card_image_records(card) for card in cards}
    expected_reviews = {
        "PRIMARY": {review["task_key"]: review for review in resolution.primary_reviews},
        "SECONDARY": {review["task_key"]: review for review in resolution.secondary_reviews},
        "ADJUDICATION": {review["task_key"]: review for review in resolution.adjudication_reviews},
    }
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise FailureLinkDriverError(f"driver freeze receipt[{index}] is invalid")
        relative = _safe_relative_path(raw.get("receipt_path"), f"receipts[{index}]")
        key = relative.as_posix()
        if key in seen:
            raise FailureLinkDriverError("driver freeze receipt path is duplicated")
        seen.add(key)
        path = output_root / Path(*relative.parts)
        _reject_symlink_path(path, boundary=output_root)
        if file_sha256(path) != raw.get("receipt_sha256"):
            raise FailureLinkDriverError(f"frozen receipt hash mismatch: {path}")
        receipt = _read_canonical_object(path)
        for field in (
            "task_key",
            "stage",
            "unit_id",
            "card_sha256",
            "response_sha256",
            "image_attachment_set_sha256",
            "image_attachment_total_bytes",
            "image_attachments",
        ):
            if receipt.get(field) != raw.get(field):
                raise FailureLinkDriverError(f"frozen receipt field mismatch: {field}")
        task_key = receipt["task_key"]
        stage = receipt["stage"]
        if stage not in stage_task_keys or task_key in stage_task_keys[stage]:
            raise FailureLinkDriverError("frozen receipt stage/task coverage is duplicated")
        stage_task_keys[stage].add(task_key)
        if receipt["card_sha256"] != card_hashes.get(task_key):
            raise FailureLinkDriverError("frozen receipt card binding mismatch")
        image_attachments = expected_image_attachments.get(task_key)
        if image_attachments is None or receipt["image_attachments"] != image_attachments:
            raise FailureLinkDriverError("frozen receipt image attachment binding mismatch")
        if receipt["image_attachment_set_sha256"] != _canonical_sha256(image_attachments):
            raise FailureLinkDriverError("frozen receipt image attachment hash mismatch")
        if receipt["image_attachment_total_bytes"] != sum(
            record["byte_length"] for record in image_attachments
        ):
            raise FailureLinkDriverError("frozen receipt image attachment byte mismatch")
        binding = receipt.get("receipt_binding")
        if not isinstance(binding, Mapping) or binding.get(
            "bundle_manifest_sha256"
        ) != _canonical_sha256(bundle_manifest):
            raise FailureLinkDriverError("frozen receipt bundle binding mismatch")
        is_legacy = bool(
            primary_migration is not None
            and stage == "PRIMARY"
            and task_key in primary_migration.artifacts_by_task
        )
        expected_protocol = {
            "origin": LEGACY_PRIMARY_ORIGIN if is_legacy else CURRENT_PRIMARY_ORIGIN,
            "prompt_version": LEGACY_PROMPT_VERSION if is_legacy else PROMPT_VERSION,
            "runtime_schema_version": (
                LEGACY_RUNTIME_SCHEMA_VERSION if is_legacy else RUNTIME_SCHEMA_VERSION
            ),
            "transport_encoding": (
                LEGACY_V3_INLINE_CARD_TRANSPORT_ENCODING if is_legacy else card_transports[task_key]
            ),
        }
        for field, expected_value in expected_protocol.items():
            if raw.get(field) != expected_value:
                raise FailureLinkDriverError(f"frozen receipt protocol mismatch: {field}")
        if is_legacy:
            migrated = primary_migration.artifacts_by_task[task_key]
            if (
                path != migrated.directory / "receipt.json"
                or receipt.get("runtime_schema_version") != LEGACY_RUNTIME_SCHEMA_VERSION
                or binding.get("prompt_version") != LEGACY_PROMPT_VERSION
                or binding.get("driver_schema_version") != LEGACY_DRIVER_SCHEMA_VERSION
                or "card_transport_encoding" in binding
            ):
                raise FailureLinkDriverError("frozen migrated receipt legacy binding mismatch")
        else:
            expected_binding_fields = {
                "card_transport_encoding": card_transports[task_key],
                **prompt_card_bindings[task_key],
                "driver_schema_version": DRIVER_SCHEMA_VERSION,
                "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
                "max_prompt_bytes": MAX_PROMPT_BYTES,
                "max_prompt_chars": MAX_PROMPT_CHARS,
                "max_prompt_tokens": MAX_PROMPT_TOKENS,
                "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
                "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
                "prompt_version": PROMPT_VERSION,
                "tiktoken_version": TIKTOKEN_VERSION,
            }
            for field, expected_value in expected_binding_fields.items():
                if binding.get(field) != expected_value:
                    raise FailureLinkDriverError(
                        f"frozen receipt prompt transport binding mismatch: {field}"
                    )
            expected_runtime_fields = {
                "max_prompt_bytes": MAX_PROMPT_BYTES,
                "max_prompt_chars": MAX_PROMPT_CHARS,
                "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
            }
            for field, expected_value in expected_runtime_fields.items():
                if receipt.get(field) != expected_value:
                    raise FailureLinkDriverError(f"frozen receipt runtime limit mismatch: {field}")
        response_path = path.parent / "response.json"
        model_response_path = path.parent / "model_response.json"
        schema_path = path.parent / "output_schema.json"
        for artifact_path in (response_path, model_response_path, schema_path):
            _reject_symlink_path(artifact_path, boundary=output_root)
        if file_sha256(response_path) != receipt.get("response_sha256"):
            raise FailureLinkDriverError("frozen response hash mismatch")
        if file_sha256(model_response_path) != receipt.get("model_response_sha256"):
            raise FailureLinkDriverError("frozen model response hash mismatch")
        if file_sha256(schema_path) != receipt.get("schema_sha256"):
            raise FailureLinkDriverError("frozen output schema hash mismatch")
        response = _read_canonical_object(response_path)
        if response.get("task_key") != task_key:
            raise FailureLinkDriverError("frozen response task binding mismatch")
        expected_review = expected_reviews[stage].get(task_key)
        if expected_review is None or response != expected_review:
            raise FailureLinkDriverError(
                "frozen accepted response does not map to the resolved review pass"
            )
        if receipt["response_sha256"] != _canonical_sha256(expected_review):
            raise FailureLinkDriverError("frozen resolved-review response hash mismatch")
    counts = resolution.manifest["counts"]
    expected = (
        counts["primary_review_count"]
        + counts["secondary_review_count"]
        + counts["adjudication_review_count"]
    )
    if len(raw_records) != expected:
        raise FailureLinkDriverError("driver freeze receipt coverage is incomplete")
    all_task_keys = set(card_hashes)
    material_task_keys = {
        record["task_key"]
        for record in resolution.manifest["task_resolutions"]
        if record["material_disagreement"] is True
    }
    if stage_task_keys["PRIMARY"] != all_task_keys:
        raise FailureLinkDriverError("frozen primary receipt coverage is incomplete")
    if stage_task_keys["SECONDARY"] != all_task_keys:
        raise FailureLinkDriverError("frozen secondary receipt coverage is incomplete")
    if stage_task_keys["ADJUDICATION"] != material_task_keys:
        raise FailureLinkDriverError("frozen adjudication receipt coverage is incorrect")
    migrated_tasks = (
        set(primary_migration.artifacts_by_task) if primary_migration is not None else set()
    )
    legacy_records = {
        record["task_key"]
        for record in raw_records
        if record.get("origin") == LEGACY_PRIMARY_ORIGIN
    }
    if legacy_records != migrated_tasks:
        raise FailureLinkDriverError("frozen migrated receipt origin coverage mismatch")


def _driver_freeze(
    *,
    phase: str,
    bundle_manifest: Mapping[str, Any],
    resolution: PhaseAResolution | PhaseBResolution,
    output_root: Path,
    artifacts: Sequence[StageArtifact],
    run_manifest: Mapping[str, Any],
    phase_a_driver_freeze_sha256: str | None,
    primary_migration: PrimaryMigrationOverlay | None = None,
) -> dict[str, Any]:
    receipt_records = []
    receipt_reviewer_ids: set[str] = set()
    for artifact in sorted(artifacts, key=lambda item: (item.unit.stage, item.unit.task_key)):
        receipt_path = artifact.directory / "receipt.json"
        receipt = _read_canonical_object(receipt_path)
        is_legacy = bool(
            primary_migration is not None
            and artifact.unit.stage == "PRIMARY"
            and artifact.unit.task_key in primary_migration.artifacts_by_task
        )
        origin = LEGACY_PRIMARY_ORIGIN if is_legacy else CURRENT_PRIMARY_ORIGIN
        receipt_reviewer_ids.add(receipt["reviewer_id"])
        image_attachments = _review_unit_image_records(artifact.unit)
        receipt_records.append(
            {
                "card_sha256": artifact.unit.card_sha256,
                "image_attachment_set_sha256": _canonical_sha256(image_attachments),
                "image_attachment_total_bytes": sum(
                    record["byte_length"] for record in image_attachments
                ),
                "image_attachments": image_attachments,
                "origin": origin,
                "prompt_version": (LEGACY_PROMPT_VERSION if is_legacy else PROMPT_VERSION),
                "receipt_path": receipt_path.relative_to(output_root).as_posix(),
                "receipt_sha256": artifact.receipt_sha256,
                "response_sha256": artifact.response_sha256,
                "runtime_schema_version": (
                    LEGACY_RUNTIME_SCHEMA_VERSION if is_legacy else RUNTIME_SCHEMA_VERSION
                ),
                "stage": artifact.unit.stage,
                "task_key": artifact.unit.task_key,
                "transport_encoding": (
                    LEGACY_V3_INLINE_CARD_TRANSPORT_ENCODING
                    if is_legacy
                    else receipt["receipt_binding"]["card_transport_encoding"]
                ),
                "unit_id": artifact.unit.unit_id,
            }
        )
    freeze = {
        "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "causal_claim_supported": False,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "outcomes_opened": phase == "B",
        "phase": phase,
        "phase_a_driver_freeze_sha256": phase_a_driver_freeze_sha256,
        "primary_migration_freeze_sha256": (
            primary_migration.freeze_sha256 if primary_migration is not None else None
        ),
        "primary_review_origin_counts": run_manifest["primary_review_origin_counts"],
        "prompt_version": PROMPT_VERSION,
        "receipts": receipt_records,
        "resolution_manifest_sha256": _canonical_sha256(resolution.manifest),
        "configured_reviewer_ids": sorted(
            {
                run_manifest["primary_reviewer_id"],
                run_manifest["secondary_reviewer_id"],
                run_manifest["adjudicator_reviewer_id"],
            }
        ),
        "receipt_reviewer_ids": sorted(receipt_reviewer_ids),
        "run_manifest_sha256": _canonical_sha256(run_manifest),
    }
    if phase == "A" and freeze["outcomes_opened"] is not False:
        raise FailureLinkDriverError("Phase A freeze cannot open outcomes")
    return freeze


def _freeze_bundle_snapshot(
    bundle: PhaseABundle | PhaseBBundle,
    target: Path,
    *,
    phase: str,
    resume: bool,
) -> None:
    _reject_symlink_path(target, boundary=target)
    if target.exists():
        if not resume:
            raise FailureLinkDriverError(f"bundle snapshot already exists: {target}")
        _verify_bundle_snapshot(bundle, target, phase=phase)
        return
    writer = write_phase_a_bundle if phase == "A" else write_phase_b_bundle
    _atomic_public_write(lambda path: writer(bundle, path), target)
    _verify_bundle_snapshot(bundle, target, phase=phase)


def _verify_bundle_snapshot(
    bundle: PhaseABundle | PhaseBBundle, target: Path, *, phase: str
) -> None:
    expected = {
        "manifest.json": canonical_json_bytes(bundle.manifest),
        "cards.jsonl": b"".join(canonical_json_bytes(card) for card in bundle.cards),
        "review_schema.json": canonical_json_bytes(
            phase_a_review_schema() if phase == "A" else phase_b_review_schema()
        ),
    }
    for name, data in expected.items():
        path = target / name
        _reject_symlink_path(path, boundary=target)
        if not path.is_file() or path.read_bytes() != data:
            raise FailureLinkDriverError(f"frozen {phase} bundle snapshot drift: {path}")


def _freeze_resolution(
    resolution: PhaseAResolution | PhaseBResolution,
    target: Path,
    *,
    phase: str,
    bundle: PhaseABundle | PhaseBBundle,
    resume: bool,
) -> None:
    loader = load_phase_a_resolution if phase == "A" else load_phase_b_resolution
    _reject_symlink_path(target, boundary=target)
    if target.exists():
        if not resume:
            raise FailureLinkDriverError(f"resolution already exists: {target}")
        _reject_resolution_symlinks(target, phase=phase)
        loaded = loader(target, bundle)  # type: ignore[arg-type]
        if loaded.manifest != resolution.manifest:
            raise FailureLinkDriverError(f"frozen {phase} resolution drift")
        return
    writer = write_phase_a_resolution if phase == "A" else write_phase_b_resolution
    _atomic_public_write(lambda path: writer(resolution, path), target)
    _reject_resolution_symlinks(target, phase=phase)
    loaded = loader(target, bundle)  # type: ignore[arg-type]
    if loaded.manifest != resolution.manifest:
        raise FailureLinkDriverError(f"frozen {phase} resolution failed verification")


def _atomic_public_write(writer: Callable[[Path], Any], target: Path) -> None:
    filesystem_root = Path(target.absolute().anchor)
    _reject_symlink_path(target, boundary=filesystem_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_path(target, boundary=filesystem_root)
    container = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    payload = container / "payload"
    try:
        writer(payload)
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        _fsync_directory(payload)
        if target.exists():
            raise FailureLinkDriverError(f"frozen target appeared concurrently: {target}")
        os.rename(payload, target)
        _fsync_directory(target.parent)
    finally:
        if container.exists():
            shutil.rmtree(container)


def _validate_phase_a_counts(
    bundle: PhaseABundle, *, expected_task_count: int, expected_chain_count: int
) -> None:
    counts = bundle.manifest["counts"]
    if counts["strict_mhr_task_count"] != expected_task_count:
        raise FailureLinkDriverError(f"Phase A task count drift: {counts['strict_mhr_task_count']}")
    if counts["strict_mhr_chain_count"] != expected_chain_count:
        raise FailureLinkDriverError(
            f"Phase A chain count drift: {counts['strict_mhr_chain_count']}"
        )
    if bundle.manifest.get("outcome_blinded") is not True:
        raise FailureLinkDriverError("Phase A bundle is not outcome blind")
    if bundle.manifest.get("causal_claim_supported") is not False:
        raise FailureLinkDriverError("Phase A bundle attempted a causal claim")


def _validate_phase_b_counts(
    bundle: PhaseBBundle,
    *,
    expected_task_count: int,
    expected_chain_count: int,
    expected_failure_task_count: int,
    expected_success_control_count: int,
    expected_all_failure_task_count: int,
) -> None:
    counts = bundle.manifest["counts"]
    expected = {
        "phase_b_task_count": expected_task_count,
        "phase_b_chain_count": expected_chain_count,
        "failure_strict_mhr_task_count": expected_failure_task_count,
        "success_control_task_count": expected_success_control_count,
        "all_failure_task_count": expected_all_failure_task_count,
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise FailureLinkDriverError(
                f"Phase B count drift for {key}: {counts.get(key)} != {value}"
            )
    if bundle.manifest.get("causal_claim_supported") is not False:
        raise FailureLinkDriverError("Phase B bundle attempted a causal claim")


def _validate_resolution_common(
    resolution: PhaseAResolution | PhaseBResolution,
    *,
    phase: str,
    expected_task_count: int,
    expected_chain_count: int,
) -> None:
    manifest = resolution.manifest
    if manifest.get("phase") != phase:
        raise FailureLinkDriverError(f"resolution phase mismatch: {phase}")
    if manifest.get("causal_claim_supported") is not False:
        raise FailureLinkDriverError("resolution attempted a causal claim")
    counts = manifest["counts"]
    if counts["task_count"] != expected_task_count:
        raise FailureLinkDriverError("resolution task coverage is incomplete")
    chain_key = "strict_mhr_chain_count" if phase == "A" else "chain_count"
    if counts[chain_key] != expected_chain_count:
        raise FailureLinkDriverError("resolution chain coverage is incomplete")
    if counts["primary_review_count"] != expected_task_count:
        raise FailureLinkDriverError("primary review coverage is incomplete")
    if counts["secondary_review_count"] != expected_task_count:
        raise FailureLinkDriverError("secondary review coverage is incomplete")
    if counts["unresolved_task_count"] != 0:
        raise FailureLinkDriverError("resolution contains unresolved tasks")
    if phase == "A" and manifest.get("outcomes_opened") is not False:
        raise FailureLinkDriverError("Phase A resolution opened outcomes")


def _run_manifest(
    *,
    phase: str,
    bundle_manifest: Mapping[str, Any],
    source_bundles: Sequence[SourceBundle],
    primary_model: str,
    secondary_model: str,
    adjudicator_model: str,
    primary_reviewer_id: str,
    secondary_reviewer_id: str,
    adjudicator_reviewer_id: str,
    codex_bin: str,
    max_attempts: int,
    timeout_seconds: int,
    phase_a_driver_freeze_sha256: str | None,
    primary_task_count: int,
    primary_migration: PrimaryMigrationPlan | None = None,
    prompt_tokenizer_asset: Path = DEFAULT_PROMPT_TOKENIZER_ASSET,
) -> dict[str, Any]:
    _prompt_tokenizer(prompt_tokenizer_asset)
    tokenizer_realpath = prompt_tokenizer_asset.resolve(strict=True)
    origin_counts, migration_manifest = _primary_migration_manifest(
        primary_migration,
        task_count=primary_task_count,
        phase=phase,
    )
    return {
        "adjudicator_model": adjudicator_model,
        "adjudicator_reviewer_id": adjudicator_reviewer_id,
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "bundle_manifest_sha256": _canonical_sha256(bundle_manifest),
        "causal_claim_supported": False,
        "card_transport_encodings": sorted(
            [ASSISTANT_EXPOSURES_TRANSPORT_ENCODING, INLINE_CARD_TRANSPORT_ENCODING]
        ),
        "codex_bin": codex_bin,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_prompt_tokens": MAX_PROMPT_TOKENS,
        "max_attempts": max_attempts,
        "outcomes_opened_at_manifest_write": phase == "B",
        "phase": phase,
        "phase_a_driver_freeze_sha256": phase_a_driver_freeze_sha256,
        "primary_model": primary_model,
        "primary_migration": migration_manifest,
        "primary_review_origin_counts": origin_counts,
        "primary_reviewer_id": primary_reviewer_id,
        "prompt_version": PROMPT_VERSION,
        "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
        "prompt_tokenizer_asset_realpath": str(tokenizer_realpath),
        "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
        "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "secondary_model": secondary_model,
        "secondary_reviewer_id": secondary_reviewer_id,
        "source_bundles": [
            {"model_id": source.model_id, "root": str(source.root)} for source in source_bundles
        ],
        "timeout_seconds": timeout_seconds,
        "tiktoken_version": TIKTOKEN_VERSION,
    }


def _validate_run_manifest_prompt_contract(manifest: Mapping[str, Any], *, phase: str) -> None:
    expected = {
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "card_transport_encodings": sorted(
            [ASSISTANT_EXPOSURES_TRANSPORT_ENCODING, INLINE_CARD_TRANSPORT_ENCODING]
        ),
        "causal_claim_supported": False,
        "driver_schema_version": DRIVER_SCHEMA_VERSION,
        "large_card_encoding_threshold_bytes": LARGE_CARD_ENCODING_THRESHOLD_BYTES,
        "max_attachment_count": MAX_ATTACHMENTS_PER_CARD,
        "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
        "max_estimated_request_payload_bytes": MAX_ESTIMATED_REQUEST_PAYLOAD_BYTES,
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_prompt_tokens": MAX_PROMPT_TOKENS,
        "outcomes_opened_at_manifest_write": phase == "B",
        "phase": phase,
        "prompt_token_encoding": PROMPT_TOKEN_ENCODING,
        "prompt_tokenizer_asset_sha256": O200K_BASE_ASSET_SHA256,
        "prompt_version": PROMPT_VERSION,
        "reviewer_disabled_features": list(REVIEWER_DISABLED_FEATURES),
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "tiktoken_version": TIKTOKEN_VERSION,
    }
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            raise FailureLinkDriverError(f"frozen run manifest prompt contract mismatch: {field}")
    if phase == "A" and manifest.get("phase_a_driver_freeze_sha256") is not None:
        raise FailureLinkDriverError(
            "frozen Phase-A run manifest cannot bind a prior Phase-A freeze"
        )
    if phase == "B" and not _SHA256_RE.fullmatch(
        str(manifest.get("phase_a_driver_freeze_sha256", ""))
    ):
        raise FailureLinkDriverError("frozen Phase-B run manifest has no valid Phase-A pin")
    try:
        _validate_reviewer_ids(
            manifest["primary_reviewer_id"],
            manifest["secondary_reviewer_id"],
            manifest["adjudicator_reviewer_id"],
        )
    except (KeyError, TypeError) as exc:
        raise FailureLinkDriverError("frozen run manifest reviewer identities are invalid") from exc
    for field in (
        "primary_model",
        "secondary_model",
        "adjudicator_model",
        "codex_bin",
        "prompt_tokenizer_asset_realpath",
    ):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise FailureLinkDriverError(f"frozen run manifest field is invalid: {field}")
    if not Path(manifest["prompt_tokenizer_asset_realpath"]).is_absolute():
        raise FailureLinkDriverError("frozen run manifest tokenizer asset path is not absolute")
    for field in ("max_attempts", "timeout_seconds"):
        if type(manifest.get(field)) is not int or manifest[field] <= 0:
            raise FailureLinkDriverError(f"frozen run manifest field is invalid: {field}")


def _prepare_output_root(output_root: Path, *, resume: bool) -> None:
    _reject_symlink_path(output_root, boundary=Path(output_root.absolute().anchor))
    if output_root.exists():
        if not output_root.is_dir():
            raise FailureLinkDriverError(f"output root is not a directory: {output_root}")
        if not resume:
            raise FailureLinkDriverError(f"output root already exists: {output_root}")
    else:
        if resume:
            raise FailureLinkDriverError(f"resume output root does not exist: {output_root}")
        output_root.mkdir(parents=True, exist_ok=False)


def _ensure_separate_output(
    output_root: Path,
    source_base: Path,
    sources: Sequence[SourceBundle],
    *,
    extra_forbidden: Sequence[Path] = (),
) -> None:
    output = output_root.resolve()
    forbidden = [source_base.resolve(strict=True)]
    forbidden.extend(source.root.resolve(strict=True) for source in sources)
    forbidden.extend(path.resolve(strict=True) for path in extra_forbidden)
    for root in forbidden:
        if output == root or root in output.parents or output in root.parents:
            raise FailureLinkDriverError(
                f"output root must be disjoint from immutable input root: {root}"
            )


def _validate_reviewer_ids(primary: str, secondary: str, adjudicator: str) -> None:
    values = (primary, secondary, adjudicator)
    if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != 3:
        raise FailureLinkDriverError(
            "primary, secondary, and adjudicator reviewer IDs must be non-empty and distinct"
        )


def _safe_relative_path(value: Any, path: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise FailureLinkDriverError(f"{path} must be a non-empty relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise FailureLinkDriverError(f"unsafe frozen relative path: {value!r}")
    return relative


def _read_canonical_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise FailureLinkDriverError(f"symlink artifact is forbidden: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FailureLinkDriverError(f"cannot read canonical JSON {path}: {exc}") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FailureLinkDriverError(f"cannot read canonical JSON {path}: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise FailureLinkDriverError(f"artifact is not one canonical JSON object: {path}")
    return value


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_symlink_path(path: Path, *, boundary: Path) -> None:
    current = path.absolute()
    boundary_absolute = boundary.absolute()
    while True:
        if current.is_symlink():
            raise FailureLinkDriverError(f"symlink artifact path is forbidden: {current}")
        if current.absolute() == boundary_absolute:
            return
        if current.parent == current:
            raise FailureLinkDriverError(f"artifact path escaped boundary: {path}")
        current = current.parent


def _reject_resolution_symlinks(path: Path, *, phase: str) -> None:
    names = {
        "manifest.json",
        "primary_reviews.jsonl",
        "secondary_reviews.jsonl",
        "adjudication_reviews.jsonl",
        "final_reviews.jsonl",
    }
    if phase == "B":
        names.add("metrics.json")
    _reject_symlink_path(path, boundary=path)
    for name in names:
        _reject_symlink_path(path / name, boundary=path)


def _bundle_argument(value: str) -> tuple[str, Path]:
    model_id, separator, root = value.partition("=")
    if not separator or not model_id or not root:
        raise argparse.ArgumentTypeError("--bundle must be MODEL_ID=/absolute/bundle/root")
    return model_id, Path(root)


def _source_bundles(values: Sequence[tuple[str, Path]]) -> tuple[SourceBundle, ...]:
    return tuple(SourceBundle.from_root(model_id, root) for model_id, root in values)


def _add_review_arguments(parser: argparse.ArgumentParser, *, phase: str) -> None:
    parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        type=_bundle_argument,
        metavar="MODEL_ID=ROOT",
    )
    parser.add_argument("--source-base", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--primary-model", default=DEFAULT_PRIMARY_MODEL)
    parser.add_argument("--secondary-model", default=DEFAULT_SECONDARY_MODEL)
    parser.add_argument("--adjudicator-model", default=DEFAULT_ADJUDICATOR_MODEL)
    if phase == "A":
        parser.add_argument("--primary-reviewer-id", default=DEFAULT_PHASE_A_PRIMARY_REVIEWER)
        parser.add_argument("--secondary-reviewer-id", default=DEFAULT_PHASE_A_SECONDARY_REVIEWER)
        parser.add_argument(
            "--adjudicator-reviewer-id", default=DEFAULT_PHASE_A_ADJUDICATOR_REVIEWER
        )
    else:
        parser.add_argument("--primary-reviewer-id", default=DEFAULT_PHASE_B_PRIMARY_REVIEWER)
        parser.add_argument("--secondary-reviewer-id", default=DEFAULT_PHASE_B_SECONDARY_REVIEWER)
        parser.add_argument(
            "--adjudicator-reviewer-id", default=DEFAULT_PHASE_B_ADJUDICATOR_REVIEWER
        )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--prompt-tokenizer-asset",
        type=Path,
        default=DEFAULT_PROMPT_TOKENIZER_ASSET,
        help="verified local o200k_base.tiktoken asset; never downloaded by this driver",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    phase_a = subparsers.add_parser(
        "phase-a", help="run outcome-blind Phase A and freeze before exiting"
    )
    _add_review_arguments(phase_a, phase="A")
    phase_a.add_argument("--phase-a-v3-primary-seed-root", type=Path)
    phase_a.add_argument("--phase-a-v3-primary-seed-run-manifest-sha256")
    phase_a.add_argument("--phase-a-v3-primary-seed-snapshot-sha256")
    phase_a.add_argument("--phase-a-v3-primary-seed-accepted-set-sha256")
    phase_b = subparsers.add_parser(
        "phase-b", help="verify the Phase-A freeze, then open outcomes for Phase B"
    )
    _add_review_arguments(phase_b, phase="B")
    phase_b.add_argument("--phase-a-dir", type=Path, required=True)
    phase_b.add_argument("--phase-a-driver-freeze-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        common = {
            "source_bundles": _source_bundles(args.bundle),
            "source_base": args.source_base,
            "output_root": args.output_dir,
            "primary_model": args.primary_model,
            "secondary_model": args.secondary_model,
            "adjudicator_model": args.adjudicator_model,
            "primary_reviewer_id": args.primary_reviewer_id,
            "secondary_reviewer_id": args.secondary_reviewer_id,
            "adjudicator_reviewer_id": args.adjudicator_reviewer_id,
            "codex_bin": args.codex_bin,
            "max_attempts": args.max_attempts,
            "timeout_seconds": args.timeout_seconds,
            "resume": args.resume,
            "dry_run": args.dry_run,
            "prompt_tokenizer_asset": args.prompt_tokenizer_asset,
        }
        if args.command == "phase-a":
            result = run_phase_a(
                **common,
                phase_a_v3_primary_seed_root=args.phase_a_v3_primary_seed_root,
                phase_a_v3_primary_seed_run_manifest_sha256=(
                    args.phase_a_v3_primary_seed_run_manifest_sha256
                ),
                phase_a_v3_primary_seed_snapshot_sha256=(
                    args.phase_a_v3_primary_seed_snapshot_sha256
                ),
                phase_a_v3_primary_seed_accepted_set_sha256=(
                    args.phase_a_v3_primary_seed_accepted_set_sha256
                ),
            )
        else:
            result = run_phase_b(
                **common,
                phase_a_root=args.phase_a_dir,
                phase_a_driver_freeze_sha256=args.phase_a_driver_freeze_sha256,
            )
    except (
        FailureAttributionError,
        FailureLinkDriverError,
        ReviewRuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps({"error": str(exc), "status": "ERROR"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": "OK", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
