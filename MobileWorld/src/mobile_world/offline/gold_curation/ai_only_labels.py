"""Deterministic, non-human publication of D-033 AI-only Action labels.

This module never writes an annotation journal and never constructs a human proposal.  It binds
three isolated batch drafts to the already sealed D-031 candidate campaign, excludes the four
units already locked by the owner, and publishes only non-authoritative research labels.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.gold_curation.ai_assistance import (
    AGENT_SLOTS,
    AICandidateWorkspace,
    _validate_untrusted_agent_value,
)
from mobile_world.offline.gold_curation.contracts import (
    CurationError,
    _project_action_predicate,
    canonical_json_bytes,
    canonical_sha256,
    json_copy,
    material_projection,
    require,
    validate_action_payload,
)
from mobile_world.offline.gold_curation.publication import (
    ACTIVE_G1_3_CAPSULE_SET_SHA256,
    ACTIVE_G1_3_MANIFEST_SHA256,
)
from mobile_world.offline.gold_curation.solo import SoloFirstPassStore
from mobile_world.offline.gold_curation.store import _read_regular, _write_once_regular

AI_ONLY_SCHEMA_ROOT: Final = (
    Path(__file__).resolve().parents[5] / "mobileworld_audit_handoff" / "schemas" / "g1_6_ai_only"
)
AI_ONLY_SCHEMA_FILENAMES: Final = (
    "ai_only_action_label_draft.schema.json",
    "ai_only_action_label.schema.json",
    "ai_only_action_label_manifest.schema.json",
    "ai_only_action_label_receipt.schema.json",
)
AI_ONLY_CONTRACT_PATH: Final = (
    "mobileworld_audit_handoff/G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md"
)
BATCH_SLOTS: Final = ("BATCH_1", "BATCH_2", "BATCH_3")
BATCH_SIZE: Final = 62
CAMPAIGN_UNIT_COUNT: Final = 190
HUMAN_LOCKED_UNIT_COUNT: Final = 4
AI_ONLY_LABEL_COUNT: Final = 186
MAX_BATCH_DRAFT_BYTES: Final = 32 * 1024 * 1024

LABEL_SCHEMA_VERSION: Final = "mobileworld.g1.ai-only-action-label/v1"
MANIFEST_SCHEMA_VERSION: Final = "mobileworld.g1.ai-only-action-label-publication-manifest/v1"
RECEIPT_SCHEMA_VERSION: Final = "mobileworld.g1.ai-only-action-label-publication-receipt/v1"

_DISCLOSURE: Final = {
    "ai_semantic_labeling_performed": True,
    "human_review_performed": False,
    "chain_of_thought_stored": False,
}
_AUTHORITY: Final = {
    "human_selected": False,
    "counts_as_independent_review": False,
    "formal_resolution_eligible": False,
    "formal_export_eligible": False,
    "admission_eligible": False,
    "promotion_allowed": False,
    "replay_eligible": False,
    "auto_apply_allowed": False,
    "solo_journal_write_allowed": False,
}
_SAFETY: Final = {
    "target_actor_model_invoked": False,
    "project_provider_client_created": False,
    "project_provider_invoked": False,
    "external_network_used": False,
    "project_gpu_probed": False,
    "project_gpu_used": False,
    "project_model_weights_loaded": False,
    "replay_executed": False,
    "action_executed": False,
    "treatment_response_generated": False,
    "annotation_journal_written": False,
}
_REJECT_REASONS: Final = {
    "MATERIAL_DUPLICATE",
    "LESS_COMPLETE_VARIANT",
    "WRONG_ACTION",
    "BAD_GEOMETRY",
    "INSUFFICIENT_EVIDENCE",
    "OUT_OF_SCOPE",
}


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    return _is_within(first, second) or _is_within(second, first)


def _self_hash(value: Mapping[str, Any], field: str) -> str:
    digest: str = canonical_sha256(
        {key: json_copy(item) for key, item in value.items() if key != field}
    )
    return digest


def _object_bytes(value: Mapping[str, Any]) -> bytes:
    encoded: bytes = canonical_json_bytes(value)
    return encoded + b"\n"


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("AI_ONLY_INVALID", f"{label} is invalid JSON") from exc
    require(isinstance(value, dict), "AI_ONLY_INVALID", f"{label} must be an object")
    return cast(dict[str, Any], value)


def _assert_local_refs(value: Any) -> None:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if reference is not None:
            require(
                isinstance(reference, str) and reference.startswith("#"),
                "AI_ONLY_SCHEMA_INVALID",
                "AI-only schema reference must remain within its checked-in document",
            )
        for child in value.values():
            _assert_local_refs(child)
    elif isinstance(value, list):
        for child in value:
            _assert_local_refs(child)


@lru_cache(maxsize=1)
def _load_schemas() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for filename in AI_ONLY_SCHEMA_FILENAMES:
        data = _read_regular(AI_ONLY_SCHEMA_ROOT / filename)
        assert data is not None
        value = _parse_json_object(data, f"AI-only schema {filename}")
        require(
            value.get("$id") == f"https://agentsentinel.local/schemas/g1_6_ai_only/{filename}",
            "AI_ONLY_SCHEMA_INVALID",
            "AI-only schema identity differs",
        )
        _assert_local_refs(value)
        Draft202012Validator.check_schema(value)
        result[filename] = value
    return result


def validate_ai_only_schema_record(filename: str, value: Any) -> None:
    schemas = _load_schemas()
    require(filename in schemas, "AI_ONLY_SCHEMA_INVALID", "unknown AI-only schema")
    errors = sorted(
        Draft202012Validator(schemas[filename]).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        first = errors[0]
        location = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.absolute_path
        )
        raise CurationError(
            "AI_ONLY_SCHEMA_MISMATCH",
            f"{filename} rejects runtime record at {location}: {first.message}",
        )


def _schema_bindings() -> dict[str, str]:
    bindings: dict[str, str] = {}
    for filename in AI_ONLY_SCHEMA_FILENAMES:
        data = _read_regular(AI_ONLY_SCHEMA_ROOT / filename)
        assert data is not None
        bindings[filename] = hashlib.sha256(data).hexdigest()
    return bindings


def _read_owner_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    require(not path.is_symlink(), "AI_ONLY_INPUT_INVALID", "input cannot be a symlink")
    data: bytes | None = _read_regular(path, owner_restricted=True)
    assert data is not None
    require(
        max_bytes is None or len(data) <= max_bytes,
        "AI_ONLY_INPUT_INVALID",
        "input exceeds its byte limit",
    )
    return data


@contextmanager
def _locked_human_journal(path: Path) -> Iterator[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CurationError(
            "AI_ONLY_HUMAN_PREFIX_INVALID", "human journal cannot be opened"
        ) from exc
    try:
        opened = os.fstat(fd)
        require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.geteuid()
            and opened.st_nlink == 1
            and opened.st_mode & 0o077 == 0,
            "AI_ONLY_HUMAN_PREFIX_INVALID",
            "human journal ownership, links, or mode are unsafe",
        )
        fcntl.flock(fd, fcntl.LOCK_SH)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        yield b"".join(chunks)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _human_prefix_binding(
    data: bytes,
    workspace: AICandidateWorkspace,
    campaign_units: set[str],
) -> dict[str, Any]:
    events = SoloFirstPassStore._decode_solo_events(data)
    require(
        len(events) == HUMAN_LOCKED_UNIT_COUNT
        and all(
            event["event_kind"] == "SOLO_FIRST_PASS_LOCKED" and event["channel"] == "ACTION_GOLD"
            for event in events
        ),
        "AI_ONLY_HUMAN_PREFIX_INVALID",
        "AI-only build requires exactly four Action-Gold human locks",
    )
    unit_ids = [cast(str, event["unit_id"]) for event in events]
    require(
        len(set(unit_ids)) == HUMAN_LOCKED_UNIT_COUNT and set(unit_ids) <= campaign_units,
        "AI_ONLY_HUMAN_PREFIX_INVALID",
        "human-locked unit population differs from the candidate campaign",
    )
    for event in events:
        unit_id = cast(str, event["unit_id"])
        payload = validate_action_payload(event["payload"])
        workspace.publication.validate_review_payload_binding(unit_id, "ACTION_GOLD", payload)
        expected_candidate_source = canonical_sha256(
            workspace.publication.packet(unit_id, "ACTION_GOLD")
        )
        expected_human_source = workspace.publication.source_packet_binding(unit_id, "ACTION_GOLD")[
            "source_packet_sha256"
        ]
        candidate_sources = {
            output["source_packet_sha256"] for output in workspace.outputs_for_unit(unit_id)
        }
        require(
            candidate_sources == {expected_candidate_source}
            and event["source_packet_sha256"] == expected_human_source
            and event["material_projection_sha256"]
            == canonical_sha256(material_projection("ACTION_GOLD", payload)),
            "AI_ONLY_HUMAN_PREFIX_INVALID",
            "human lock differs from its candidate source or material projection",
        )
    locked_units = [
        {
            "unit_id": event["unit_id"],
            "event_id": event["event_id"],
            "event_sha256": event["event_sha256"],
            "payload_sha256": event["payload_sha256"],
            "material_projection_sha256": event["material_projection_sha256"],
            "source_packet_sha256": event["source_packet_sha256"],
        }
        for event in sorted(events, key=lambda item: item["unit_id"])
    ]
    return {
        "journal_prefix_sha256": hashlib.sha256(data).hexdigest(),
        "journal_prefix_byte_count": len(data),
        "event_count": HUMAN_LOCKED_UNIT_COUNT,
        "head_event_sha256": events[-1]["event_sha256"],
        "locked_units": locked_units,
    }


def _assert_human_prefix(
    path: Path,
    binding: Mapping[str, Any],
    workspace: AICandidateWorkspace,
) -> None:
    with _locked_human_journal(path) as current:
        prefix_count = cast(int, binding["journal_prefix_byte_count"])
        require(
            len(current) >= prefix_count
            and hashlib.sha256(current[:prefix_count]).hexdigest()
            == binding["journal_prefix_sha256"],
            "AI_ONLY_HUMAN_PREFIX_INVALID",
            "human journal no longer contains the bound immutable prefix",
        )
        current_binding = _human_prefix_binding(
            current[:prefix_count], workspace, set(_campaign_unit_ids(workspace))
        )
        require(
            current_binding == binding,
            "AI_ONLY_HUMAN_PREFIX_INVALID",
            "human journal prefix projection differs",
        )


def _campaign_binding(workspace: AICandidateWorkspace) -> dict[str, Any]:
    manifest_data = _read_owner_file(workspace.root / "campaign-manifest.json")
    receipt_data = _read_owner_file(workspace.root / "campaign-receipt.json")
    atomic_count = sum(
        len(output["candidate_items"])
        for unit_id in _campaign_unit_ids(workspace)
        for output in workspace.outputs_for_unit(unit_id)
    )
    return {
        "campaign_id": workspace.campaign_id,
        "campaign_manifest_file_sha256": hashlib.sha256(manifest_data).hexdigest(),
        "campaign_manifest_sha256": workspace.manifest["campaign_manifest_sha256"],
        "campaign_receipt_file_sha256": hashlib.sha256(receipt_data).hexdigest(),
        "campaign_receipt_sha256": workspace.receipt["receipt_sha256"],
        "candidate_set_sha256": workspace.receipt["candidate_set_sha256"],
        "packet_count": len(workspace.manifest["packet_refs"]),
        "output_count": len(workspace.receipt["output_refs"]),
        "atomic_candidate_count": atomic_count,
    }


def _campaign_unit_ids(workspace: AICandidateWorkspace) -> list[str]:
    unit_ids = sorted(cast(str, item["unit_id"]) for item in workspace.manifest["packet_refs"])
    require(
        len(unit_ids) == CAMPAIGN_UNIT_COUNT and len(set(unit_ids)) == CAMPAIGN_UNIT_COUNT,
        "AI_ONLY_POPULATION_INVALID",
        "candidate campaign unit population differs",
    )
    return unit_ids


def _output_reference_map(
    workspace: AICandidateWorkspace,
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for reference in workspace.receipt["output_refs"]:
        key = (cast(str, reference["unit_id"]), cast(str, reference["agent_slot"]))
        require(
            key not in result, "AI_ONLY_SOURCE_INVALID", "candidate output binding is duplicated"
        )
        result[key] = cast(dict[str, Any], json_copy(reference))
    return result


def _candidate_inventory(
    workspace: AICandidateWorkspace,
    unit_id: str,
    output_refs: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    source_packet_sha256: str | None = None
    for slot, output in zip(AGENT_SLOTS, workspace.outputs_for_unit(unit_id), strict=True):
        require(
            output["agent_slot"] == slot and (unit_id, slot) in output_refs,
            "AI_ONLY_SOURCE_INVALID",
            "candidate output slot binding differs",
        )
        output_ref = output_refs[(unit_id, slot)]
        bindings.append({"agent_slot": slot, "output_object_sha256": output_ref["sha256"]})
        current_source = cast(str, output["source_packet_sha256"])
        source_packet_sha256 = source_packet_sha256 or current_source
        require(
            source_packet_sha256 == current_source,
            "AI_ONLY_SOURCE_INVALID",
            "candidate outputs do not share one source packet",
        )
        for item in output["candidate_items"]:
            candidates.append(
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_sha256": item["candidate_sha256"],
                    "agent_slot": slot,
                    "output_object_sha256": output_ref["sha256"],
                    "predicate": json_copy(item["predicate"]),
                }
            )
    require(
        source_packet_sha256 is not None,
        "AI_ONLY_SOURCE_INVALID",
        "candidate unit has no source packet",
    )
    require(
        len({item["candidate_id"] for item in candidates}) == len(candidates),
        "AI_ONLY_SOURCE_INVALID",
        "candidate IDs collide within a unit",
    )
    return cast(str, source_packet_sha256), bindings, candidates


def _load_batch_rows(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    data = _read_owner_file(path, max_bytes=MAX_BATCH_DRAFT_BYTES)
    require(
        bool(data) and data.endswith(b"\n") and b"\r" not in data,
        "AI_ONLY_BATCH_INVALID",
        "batch draft must be LF-framed JSONL",
    )
    rows: list[dict[str, Any]] = []
    for raw in data[:-1].split(b"\n"):
        require(bool(raw), "AI_ONLY_BATCH_INVALID", "batch draft contains a blank row")
        row = _parse_json_object(raw, "AI-only batch row")
        validate_ai_only_schema_record("ai_only_action_label_draft.schema.json", row)
        _validate_untrusted_agent_value(row["concise_rationale"], path="$draft.concise_rationale")
        if row["uncertainty_note"] is not None:
            _validate_untrusted_agent_value(row["uncertainty_note"], path="$draft.uncertainty_note")
        rows.append(row)
    require(
        len(rows) == BATCH_SIZE
        and [row["unit_id"] for row in rows] == sorted(row["unit_id"] for row in rows),
        "AI_ONLY_BATCH_INVALID",
        "batch draft must contain 62 sorted rows",
    )
    return data, rows


def _validate_batch_row(
    row: Mapping[str, Any],
    *,
    publication_id: str,
    batch_slot: str,
    source_packet_sha256: str,
    source_output_bindings: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    inventory = {cast(str, item["candidate_id"]): item for item in candidates}
    raw_decisions = cast(list[dict[str, Any]], row["candidate_decisions"])
    decision_map = {item["candidate_id"]: item for item in raw_decisions}
    require(
        len(decision_map) == len(raw_decisions) and set(decision_map) == set(inventory),
        "AI_ONLY_DECISION_INVALID",
        "every frozen candidate must be decided exactly once",
    )
    retained_ids = cast(list[str], row["retained_candidate_ids"])
    retained_set = set(retained_ids)
    require(
        len(retained_set) == len(retained_ids)
        and retained_set
        == {
            candidate_id
            for candidate_id, item in decision_map.items()
            if item["decision"] == "RETAIN"
        },
        "AI_ONLY_DECISION_INVALID",
        "retained candidate list differs from candidate decisions",
    )
    for decision in decision_map.values():
        require(
            (decision["decision"] == "RETAIN" and decision["reason"] == "SUPPORTED")
            or (decision["decision"] == "REJECT" and decision["reason"] in _REJECT_REASONS),
            "AI_ONLY_DECISION_INVALID",
            "candidate decision reason differs from its retain/reject state",
        )
    if row["label_kind"] == "ACCEPT_CANDIDATES":
        require(
            bool(retained_set) and row["exclusion_reason"] is None,
            "AI_ONLY_DECISION_INVALID",
            "accepted AI-only label requires retained candidates and no exclusion",
        )
    else:
        require(
            not retained_set and row["exclusion_reason"] is not None,
            "AI_ONLY_DECISION_INVALID",
            "excluded AI-only label cannot retain candidates",
        )
    material = [
        canonical_sha256(
            _project_action_predicate(cast(Mapping[str, Any], inventory[item]["predicate"]))
        )
        for item in retained_ids
    ]
    require(
        len(material) == len(set(material)),
        "AI_ONLY_MATERIAL_DUPLICATE",
        "retained AI-only candidates contain a material duplicate",
    )
    decisions: list[dict[str, Any]] = []
    retained_refs: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = cast(str, candidate["candidate_id"])
        reference = {
            key: json_copy(candidate[key])
            for key in (
                "candidate_id",
                "candidate_sha256",
                "agent_slot",
                "output_object_sha256",
            )
        }
        raw_decision = decision_map[candidate_id]
        decisions.append(
            {
                **reference,
                "decision": raw_decision["decision"],
                "reason": raw_decision["reason"],
            }
        )
        if candidate_id in retained_set:
            retained_refs.append(reference)
    label: dict[str, Any] = {
        "schema_version": LABEL_SCHEMA_VERSION,
        "record_type": "ai_only_action_label",
        "publication_id": publication_id,
        "unit_id": row["unit_id"],
        "source_packet_sha256": source_packet_sha256,
        "source_output_bindings": json_copy(list(source_output_bindings)),
        "label_kind": row["label_kind"],
        "retained_candidate_refs": retained_refs,
        "candidate_decisions": decisions,
        "exclusion_reason": row["exclusion_reason"],
        "concise_rationale": row["concise_rationale"],
        "uncertainty_note": row["uncertainty_note"],
        "provenance": {
            "labeling_mode": "THREE_ISOLATED_CODEX_SHARDS",
            "batch_slot": batch_slot,
            "only_frozen_blind_packet_and_candidates_used": True,
            "human_material_used": False,
            "peer_batch_output_used": False,
            "new_candidate_generated": False,
        },
        "disclosure": json_copy(_DISCLOSURE),
        "authority": json_copy(_AUTHORITY),
        "safety": json_copy(_SAFETY),
    }
    label["label_sha256"] = _self_hash(label, "label_sha256")
    validate_ai_only_schema_record("ai_only_action_label.schema.json", label)
    return label


def _content_reference(kind: str, suffix: str, data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "path": f"{kind}/sha256/{digest[:2]}/{digest}{suffix}",
        "sha256": digest,
        "byte_count": len(data),
    }


def _safe_relative_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    require(
        not pure.is_absolute()
        and ".." not in pure.parts
        and "\\" not in relative
        and "\x00" not in relative,
        "AI_ONLY_CENSUS_INVALID",
        "publication reference path is unsafe",
    )
    return root.joinpath(*pure.parts)


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = path.stat(follow_symlinks=False)
    require(
        not path.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_mode & 0o077 == 0,
        "AI_ONLY_PUBLICATION_INVALID",
        "publication directory is unsafe",
    )


def _write_reference(root: Path, reference: Mapping[str, Any], data: bytes) -> None:
    path = _safe_relative_path(root, cast(str, reference["path"]))
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        _ensure_directory(current)
    _write_once_regular(path, data)


def _filesystem_census(root: Path, *, sealed: bool = True) -> set[str]:
    root_stat = root.stat(follow_symlinks=False)
    require(
        not root.is_symlink()
        and stat.S_ISDIR(root_stat.st_mode)
        and root_stat.st_uid == os.geteuid()
        and root_stat.st_mode & 0o077 == 0,
        "AI_ONLY_CENSUS_INVALID",
        "publication root is unsafe",
    )
    if sealed:
        require(
            stat.S_IMODE(root_stat.st_mode) == 0o500,
            "AI_ONLY_CENSUS_INVALID",
            "sealed publication root must be owner-read/execute only",
        )
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            metadata = child.stat(follow_symlinks=False)
            relative = child.relative_to(root).as_posix()
            require(not child.is_symlink(), "AI_ONLY_CENSUS_INVALID", "symlink is forbidden")
            if stat.S_ISDIR(metadata.st_mode):
                require(
                    metadata.st_uid == os.geteuid() and metadata.st_mode & 0o077 == 0,
                    "AI_ONLY_CENSUS_INVALID",
                    "publication directory ownership or mode is unsafe",
                )
                if sealed:
                    require(
                        stat.S_IMODE(metadata.st_mode) == 0o500,
                        "AI_ONLY_CENSUS_INVALID",
                        "sealed publication directory mode differs",
                    )
                pending.append(child)
            else:
                require(
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_uid == os.geteuid()
                    and metadata.st_nlink == 1
                    and metadata.st_mode & 0o077 == 0,
                    "AI_ONLY_CENSUS_INVALID",
                    "publication file ownership, links, or mode are unsafe",
                )
                if sealed:
                    require(
                        stat.S_IMODE(metadata.st_mode) == 0o400,
                        "AI_ONLY_CENSUS_INVALID",
                        "sealed publication file mode differs",
                    )
                files.add(relative)
    return files


def _assert_exact_census(root: Path, expected_files: set[str], *, sealed: bool = True) -> None:
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    require(
        _filesystem_census(root, sealed=sealed) == expected_files
        and actual_directories == expected_directories,
        "AI_ONLY_CENSUS_INVALID",
        "AI-only publication filesystem census differs",
    )


def _seal_publication_tree(root: Path, expected_files: set[str]) -> None:
    _assert_exact_census(root, expected_files, sealed=False)
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda item: item.as_posix(),
    )
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in files:
        os.chmod(path, 0o400, follow_symlinks=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for path in (*directories, root):
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fchmod(descriptor, 0o500)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _assert_exact_census(root, expected_files)


def _rename_directory_noreplace(*, parent_fd: int, source_name: str, destination_name: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    require(
        function is not None,
        "AI_ONLY_ATOMIC_PUBLISH_UNSUPPORTED",
        "renameat2(RENAME_NOREPLACE) is required for AI-only publication",
    )
    assert function is not None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise CurationError("AI_ONLY_ROOT_INVALID", "AI-only output root already exists")
    raise CurationError(
        "AI_ONLY_ATOMIC_PUBLISH_FAILED",
        f"renameat2(RENAME_NOREPLACE) failed: {os.strerror(error_number)}",
    )


def _source_binding(
    workspace: AICandidateWorkspace,
    human_prefix: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "g1_3_publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
        "g1_3_capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
        "candidate_campaign": _campaign_binding(workspace),
        "human_exclusion_prefix": json_copy(human_prefix),
    }


def _contract_sha256(repository_root: Path) -> str:
    data = _read_regular(repository_root / AI_ONLY_CONTRACT_PATH)
    assert data is not None
    return hashlib.sha256(data).hexdigest()


def _publication_id(
    source_binding: Mapping[str, Any],
    contract_sha256: str,
    schema_bindings: Mapping[str, str],
    batch_inputs: Sequence[Mapping[str, Any]],
) -> str:
    subject = {
        "schema_version": "mobileworld.g1.ai-only-action-label-publication-id-subject/v1",
        "source_binding": json_copy(source_binding),
        "contract_sha256": contract_sha256,
        "schema_bindings": json_copy(schema_bindings),
        "batch_inputs": json_copy(list(batch_inputs)),
    }
    digest: str = canonical_sha256(subject)
    return "g1aionly-" + digest[:24]


def build_ai_only_action_label_publication(
    output_root: str | os.PathLike[str],
    candidate_workspace: AICandidateWorkspace,
    human_journal_path: str | os.PathLike[str],
    batch_drafts: Mapping[str, str | os.PathLike[str]],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate three isolated drafts and atomically publish 186 non-human labels."""

    repo = Path(repository_root or _repository_root()).resolve(strict=True)
    destination = Path(output_root)
    require(
        not destination.exists()
        and not destination.is_symlink()
        and destination.parent.exists()
        and not destination.parent.is_symlink(),
        "AI_ONLY_ROOT_INVALID",
        "AI-only output root must be absent below an existing parent",
    )
    parent_stat = destination.parent.stat(follow_symlinks=False)
    require(
        stat.S_ISDIR(parent_stat.st_mode)
        and parent_stat.st_uid == os.geteuid()
        and parent_stat.st_mode & 0o077 == 0,
        "AI_ONLY_ROOT_INVALID",
        "AI-only output parent must be owner-restricted",
    )
    destination_resolved = destination.resolve(strict=False)
    journal_supplied = Path(human_journal_path)
    require(
        not journal_supplied.is_symlink(),
        "AI_ONLY_HUMAN_PREFIX_INVALID",
        "human journal cannot be a symlink",
    )
    journal = journal_supplied.resolve(strict=True)
    forbidden = (
        repo,
        candidate_workspace.root.resolve(strict=True),
        candidate_workspace.publication.root.resolve(strict=True),
        journal.parent,
    )
    require(
        all(not _paths_overlap(destination_resolved, item) for item in forbidden),
        "AI_ONLY_ROOT_INVALID",
        "AI-only output root must be disjoint from repository and source roots",
    )
    require(
        set(batch_drafts) == set(BATCH_SLOTS),
        "AI_ONLY_BATCH_INVALID",
        "exactly three named batch drafts are required",
    )
    resolved_drafts: dict[str, Path] = {}
    for slot in BATCH_SLOTS:
        supplied = Path(batch_drafts[slot])
        require(
            not supplied.is_symlink(),
            "AI_ONLY_BATCH_INVALID",
            "batch draft cannot be a symlink",
        )
        resolved = supplied.resolve(strict=True)
        require(
            all(
                not _is_within(resolved, forbidden_root.resolve(strict=False))
                for forbidden_root in (
                    repo,
                    candidate_workspace.root,
                    candidate_workspace.publication.root,
                    journal.parent,
                    destination,
                )
            ),
            "AI_ONLY_BATCH_INVALID",
            "batch draft must be disjoint from repository and publication roots",
        )
        resolved_drafts[slot] = resolved
    require(
        len(set(resolved_drafts.values())) == len(BATCH_SLOTS),
        "AI_ONLY_BATCH_INVALID",
        "batch draft paths must be distinct",
    )
    campaign_units = _campaign_unit_ids(candidate_workspace)
    output_refs = _output_reference_map(candidate_workspace)
    schemas = _schema_bindings()
    contract_sha256 = _contract_sha256(repo)
    with _locked_human_journal(journal) as human_data:
        human_prefix = _human_prefix_binding(human_data, candidate_workspace, set(campaign_units))
        human_units = {item["unit_id"] for item in human_prefix["locked_units"]}
        ai_units = [unit_id for unit_id in campaign_units if unit_id not in human_units]
        require(
            len(ai_units) == AI_ONLY_LABEL_COUNT,
            "AI_ONLY_POPULATION_INVALID",
            "AI-only remainder must contain exactly 186 units",
        )
        loaded_batches: dict[str, tuple[bytes, list[dict[str, Any]]]] = {}
        batch_inputs: list[dict[str, Any]] = []
        for index, slot in enumerate(BATCH_SLOTS):
            data, rows = _load_batch_rows(resolved_drafts[slot])
            expected_units = ai_units[index * BATCH_SIZE : (index + 1) * BATCH_SIZE]
            require(
                [row["unit_id"] for row in rows] == expected_units,
                "AI_ONLY_BATCH_INVALID",
                f"{slot} unit shard differs from the frozen 62-unit assignment",
            )
            loaded_batches[slot] = (data, rows)
            batch_inputs.append(
                {
                    "batch_slot": slot,
                    "draft_file_sha256": hashlib.sha256(data).hexdigest(),
                    "draft_file_byte_count": len(data),
                    "label_count": BATCH_SIZE,
                    "first_unit_id": expected_units[0],
                    "last_unit_id": expected_units[-1],
                    "only_assigned_blind_inputs_used": True,
                    "peer_batch_output_used": False,
                    "human_material_used": False,
                }
            )
        source_binding = _source_binding(candidate_workspace, human_prefix)
        publication_id = _publication_id(
            source_binding,
            contract_sha256,
            schemas,
            batch_inputs,
        )
        label_objects: list[tuple[dict[str, Any], bytes, dict[str, Any]]] = []
        all_rows: list[tuple[str, dict[str, Any]]] = []
        for slot in BATCH_SLOTS:
            all_rows.extend((slot, row) for row in loaded_batches[slot][1])
        for slot, row in all_rows:
            unit_id = cast(str, row["unit_id"])
            source_packet, bindings, candidates = _candidate_inventory(
                candidate_workspace, unit_id, output_refs
            )
            label = _validate_batch_row(
                row,
                publication_id=publication_id,
                batch_slot=slot,
                source_packet_sha256=source_packet,
                source_output_bindings=bindings,
                candidates=candidates,
            )
            data = _object_bytes(label)
            reference = _content_reference("labels", ".json", data)
            reference.update(
                {
                    "unit_id": unit_id,
                    "label_kind": label["label_kind"],
                    "label_sha256": label["label_sha256"],
                }
            )
            label_objects.append((label, data, reference))
        require(
            len(label_objects) == AI_ONLY_LABEL_COUNT
            and [item[0]["unit_id"] for item in label_objects] == ai_units,
            "AI_ONLY_POPULATION_INVALID",
            "compiled AI-only label order or count differs",
        )
        index_rows = [json_copy(item[2]) for item in label_objects]
        index_data = b"".join(canonical_json_bytes(row) + b"\n" for row in index_rows)
        index_ref = {
            "path": "label-index.jsonl",
            "sha256": hashlib.sha256(index_data).hexdigest(),
            "byte_count": len(index_data),
        }
        accepted_count = sum(item[0]["label_kind"] == "ACCEPT_CANDIDATES" for item in label_objects)
        retained_count = sum(len(item[0]["retained_candidate_refs"]) for item in label_objects)
        decided_count = sum(len(item[0]["candidate_decisions"]) for item in label_objects)
        counts = {
            "campaign_units": CAMPAIGN_UNIT_COUNT,
            "human_locked_units": HUMAN_LOCKED_UNIT_COUNT,
            "ai_only_labeled_units": AI_ONLY_LABEL_COUNT,
            "accepted_candidate_units": accepted_count,
            "excluded_units": AI_ONLY_LABEL_COUNT - accepted_count,
            "retained_candidates": retained_count,
            "rejected_candidates": decided_count - retained_count,
            "decided_candidates": decided_count,
        }
        label_set_sha256 = canonical_sha256(
            [
                {
                    "unit_id": reference["unit_id"],
                    "sha256": reference["sha256"],
                    "label_sha256": reference["label_sha256"],
                }
                for _, _, reference in label_objects
            ]
        )
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "record_type": "ai_only_action_label_publication_manifest",
            "publication_id": publication_id,
            "source_binding": source_binding,
            "contract_sha256": contract_sha256,
            "schema_bindings": schemas,
            "batch_inputs": batch_inputs,
            "label_index": index_ref,
            "label_refs": index_rows,
            "counts": counts,
            "label_set_sha256": label_set_sha256,
            "disclosure": json_copy(_DISCLOSURE),
            "authority": json_copy(_AUTHORITY),
            "safety": json_copy(_SAFETY),
        }
        manifest["publication_manifest_sha256"] = _self_hash(
            manifest, "publication_manifest_sha256"
        )
        validate_ai_only_schema_record("ai_only_action_label_manifest.schema.json", manifest)
        manifest_data = _object_bytes(manifest)
        receipt: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "record_type": "ai_only_action_label_publication_receipt",
            "publication_id": publication_id,
            "publication_manifest_file_sha256": hashlib.sha256(manifest_data).hexdigest(),
            "publication_manifest_sha256": manifest["publication_manifest_sha256"],
            "label_index": index_ref,
            "label_set_sha256": label_set_sha256,
            "counts": counts,
            "census": {
                "regular_file_count": AI_ONLY_LABEL_COUNT + 3,
                "label_object_count": AI_ONLY_LABEL_COUNT,
                "symlink_count": 0,
                "hardlink_count": 0,
                "extra_file_count": 0,
            },
            "disclosure": json_copy(_DISCLOSURE),
            "authority": json_copy(_AUTHORITY),
            "safety": json_copy(_SAFETY),
        }
        receipt["receipt_sha256"] = _self_hash(receipt, "receipt_sha256")
        validate_ai_only_schema_record("ai_only_action_label_receipt.schema.json", receipt)
        receipt_data = _object_bytes(receipt)
        expected_files = {
            "publication-manifest.json",
            "publication-receipt.json",
            "label-index.jsonl",
            *(cast(str, item[2]["path"]) for item in label_objects),
        }
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
        )
        staging.chmod(0o700)
        parent_fd: int | None = None
        try:
            for _, data, reference in label_objects:
                _write_reference(staging, reference, data)
            _write_once_regular(staging / "label-index.jsonl", index_data)
            _write_once_regular(staging / "publication-manifest.json", manifest_data)
            _write_once_regular(staging / "publication-receipt.json", receipt_data)
            _seal_publication_tree(staging, expected_files)
            parent_fd = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            current_parent = os.fstat(parent_fd)
            require(
                (current_parent.st_dev, current_parent.st_ino)
                == (parent_stat.st_dev, parent_stat.st_ino),
                "AI_ONLY_ROOT_INVALID",
                "AI-only output parent changed during compilation",
            )
            _rename_directory_noreplace(
                parent_fd=parent_fd,
                source_name=staging.name,
                destination_name=destination.name,
            )
            os.fsync(parent_fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
    return cast(dict[str, Any], json_copy(receipt))


class AIOnlyActionLabelPublication:
    """Fail-closed reader for one immutable D-033 publication."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        candidate_workspace: AICandidateWorkspace,
        human_journal_path: str | os.PathLike[str],
        *,
        repository_root: str | os.PathLike[str] | None = None,
    ) -> None:
        repo = Path(repository_root or _repository_root()).resolve(strict=True)
        supplied = Path(root)
        require(
            supplied.exists() and not supplied.is_symlink(),
            "AI_ONLY_ROOT_INVALID",
            "AI-only publication is missing or is a symlink",
        )
        resolved = supplied.resolve(strict=True)
        journal_supplied = Path(human_journal_path)
        require(
            journal_supplied.exists() and not journal_supplied.is_symlink(),
            "AI_ONLY_HUMAN_PREFIX_INVALID",
            "human journal is missing or is a symlink",
        )
        journal = journal_supplied.resolve(strict=True)
        require(
            all(
                not _paths_overlap(resolved, forbidden)
                for forbidden in (
                    repo,
                    candidate_workspace.root.resolve(strict=True),
                    candidate_workspace.publication.root.resolve(strict=True),
                    journal.parent,
                )
            ),
            "AI_ONLY_ROOT_INVALID",
            "AI-only publication overlaps a forbidden source root",
        )
        self.root = resolved
        manifest_data = _read_owner_file(resolved / "publication-manifest.json")
        receipt_data = _read_owner_file(resolved / "publication-receipt.json")
        self.manifest = _parse_json_object(manifest_data, "AI-only publication manifest")
        self.receipt = _parse_json_object(receipt_data, "AI-only publication receipt")
        require(
            manifest_data == _object_bytes(self.manifest)
            and receipt_data == _object_bytes(self.receipt),
            "AI_ONLY_INVALID",
            "AI-only manifest or receipt is not canonical",
        )
        validate_ai_only_schema_record("ai_only_action_label_manifest.schema.json", self.manifest)
        validate_ai_only_schema_record("ai_only_action_label_receipt.schema.json", self.receipt)
        require(
            self.manifest["publication_manifest_sha256"]
            == _self_hash(self.manifest, "publication_manifest_sha256")
            and self.receipt["receipt_sha256"] == _self_hash(self.receipt, "receipt_sha256")
            and self.receipt["publication_id"] == self.manifest["publication_id"]
            and self.receipt["publication_manifest_file_sha256"]
            == hashlib.sha256(manifest_data).hexdigest()
            and self.receipt["publication_manifest_sha256"]
            == self.manifest["publication_manifest_sha256"],
            "AI_ONLY_INVALID",
            "AI-only publication self binding differs",
        )
        require(
            self.manifest["publication_id"]
            == _publication_id(
                self.manifest["source_binding"],
                cast(str, self.manifest["contract_sha256"]),
                cast(Mapping[str, str], self.manifest["schema_bindings"]),
                cast(Sequence[Mapping[str, Any]], self.manifest["batch_inputs"]),
            ),
            "AI_ONLY_INVALID",
            "AI-only publication identity differs from its bound inputs",
        )
        require(
            self.manifest["contract_sha256"] == _contract_sha256(repo)
            and self.manifest["schema_bindings"] == _schema_bindings()
            and self.manifest["source_binding"]["g1_3_publication_manifest_sha256"]
            == ACTIVE_G1_3_MANIFEST_SHA256
            and self.manifest["source_binding"]["g1_3_capsule_set_sha256"]
            == ACTIVE_G1_3_CAPSULE_SET_SHA256
            and self.manifest["source_binding"]["candidate_campaign"]
            == _campaign_binding(candidate_workspace),
            "AI_ONLY_SOURCE_INVALID",
            "AI-only checked-in or candidate-campaign binding differs",
        )
        _assert_human_prefix(
            journal,
            self.manifest["source_binding"]["human_exclusion_prefix"],
            candidate_workspace,
        )
        campaign_units = _campaign_unit_ids(candidate_workspace)
        human_units = {
            cast(str, item["unit_id"])
            for item in self.manifest["source_binding"]["human_exclusion_prefix"]["locked_units"]
        }
        require(
            len(human_units) == HUMAN_LOCKED_UNIT_COUNT and human_units <= set(campaign_units),
            "AI_ONLY_POPULATION_INVALID",
            "human-excluded unit population differs from the campaign",
        )
        ai_units = [unit_id for unit_id in campaign_units if unit_id not in human_units]
        require(
            len(ai_units) == AI_ONLY_LABEL_COUNT,
            "AI_ONLY_POPULATION_INVALID",
            "AI-only campaign remainder differs",
        )
        batch_inputs = cast(list[dict[str, Any]], self.manifest["batch_inputs"])
        require(
            len(batch_inputs) == len(BATCH_SLOTS),
            "AI_ONLY_BATCH_INVALID",
            "AI-only batch binding count differs",
        )
        for index, (slot, batch_input) in enumerate(zip(BATCH_SLOTS, batch_inputs, strict=True)):
            expected_units = ai_units[index * BATCH_SIZE : (index + 1) * BATCH_SIZE]
            require(
                batch_input["batch_slot"] == slot
                and batch_input["label_count"] == BATCH_SIZE
                and batch_input["first_unit_id"] == expected_units[0]
                and batch_input["last_unit_id"] == expected_units[-1],
                "AI_ONLY_BATCH_INVALID",
                "AI-only batch shard binding differs",
            )
        label_refs = cast(list[dict[str, Any]], self.manifest["label_refs"])
        require(
            len(label_refs) == AI_ONLY_LABEL_COUNT
            and [item["unit_id"] for item in label_refs] == ai_units,
            "AI_ONLY_POPULATION_INVALID",
            "AI-only label index population or ordering differs",
        )
        index_data = _read_owner_file(resolved / "label-index.jsonl")
        require(
            hashlib.sha256(index_data).hexdigest() == self.manifest["label_index"]["sha256"]
            and len(index_data) == self.manifest["label_index"]["byte_count"]
            and bool(index_data)
            and index_data.endswith(b"\n")
            and b"\r" not in index_data,
            "AI_ONLY_INDEX_INVALID",
            "AI-only label index framing or digest differs",
        )
        index_rows = [
            _parse_json_object(raw, "AI-only label index row")
            for raw in index_data[:-1].split(b"\n")
        ]
        require(
            all(
                raw == canonical_json_bytes(row)
                for raw, row in zip(index_data[:-1].split(b"\n"), index_rows, strict=True)
            )
            and index_rows == label_refs,
            "AI_ONLY_INDEX_INVALID",
            "AI-only label index bytes differ from manifest refs",
        )
        output_refs = _output_reference_map(candidate_workspace)
        labels: list[dict[str, Any]] = []
        for label_index, reference in enumerate(label_refs):
            path = _safe_relative_path(resolved, cast(str, reference["path"]))
            data = _read_owner_file(path)
            expected_content_reference = _content_reference("labels", ".json", data)
            require(
                all(
                    reference[key] == expected_content_reference[key]
                    for key in ("path", "sha256", "byte_count")
                ),
                "AI_ONLY_LABEL_INVALID",
                "AI-only label content reference differs",
            )
            label = _parse_json_object(data, "AI-only label")
            require(
                data == _object_bytes(label)
                and label["publication_id"] == self.manifest["publication_id"]
                and label["unit_id"] == reference["unit_id"]
                and label["label_kind"] == reference["label_kind"]
                and label["label_sha256"] == reference["label_sha256"]
                and label["label_sha256"] == _self_hash(label, "label_sha256"),
                "AI_ONLY_LABEL_INVALID",
                "AI-only label canonical or self binding differs",
            )
            validate_ai_only_schema_record("ai_only_action_label.schema.json", label)
            _validate_untrusted_agent_value(
                label["concise_rationale"], path="$label.concise_rationale"
            )
            if label["uncertainty_note"] is not None:
                _validate_untrusted_agent_value(
                    label["uncertainty_note"], path="$label.uncertainty_note"
                )
            source_packet, bindings, candidates = _candidate_inventory(
                candidate_workspace, cast(str, label["unit_id"]), output_refs
            )
            inventory = {cast(str, item["candidate_id"]): item for item in candidates}
            expected_candidate_ids = [item["candidate_id"] for item in candidates]
            decisions = cast(list[dict[str, Any]], label["candidate_decisions"])
            decision_ids = [item["candidate_id"] for item in decisions]
            expected_slot = BATCH_SLOTS[label_index // BATCH_SIZE]
            require(
                label["source_packet_sha256"] == source_packet
                and label["source_output_bindings"] == bindings
                and decision_ids == expected_candidate_ids
                and len(set(decision_ids)) == len(decision_ids)
                and label["provenance"]["batch_slot"] == expected_slot,
                "AI_ONLY_LABEL_INVALID",
                "AI-only label source inventory differs",
            )
            retained_decision_ids: list[str] = []
            expected_retained_refs: list[dict[str, Any]] = []
            for decision in decisions:
                candidate = inventory[decision["candidate_id"]]
                require(
                    all(
                        decision[key] == candidate[key]
                        for key in (
                            "candidate_sha256",
                            "agent_slot",
                            "output_object_sha256",
                        )
                    ),
                    "AI_ONLY_LABEL_INVALID",
                    "AI-only candidate decision source reference differs",
                )
                require(
                    (decision["decision"] == "RETAIN" and decision["reason"] == "SUPPORTED")
                    or (decision["decision"] == "REJECT" and decision["reason"] in _REJECT_REASONS),
                    "AI_ONLY_DECISION_INVALID",
                    "AI-only candidate decision reason differs from its state",
                )
                if decision["decision"] == "RETAIN":
                    retained_decision_ids.append(cast(str, decision["candidate_id"]))
                    expected_retained_refs.append(
                        {
                            key: json_copy(candidate[key])
                            for key in (
                                "candidate_id",
                                "candidate_sha256",
                                "agent_slot",
                                "output_object_sha256",
                            )
                        }
                    )
            retained_refs = cast(list[dict[str, Any]], label["retained_candidate_refs"])
            retained_ids = [cast(str, item["candidate_id"]) for item in retained_refs]
            require(
                retained_ids == retained_decision_ids
                and len(set(retained_ids)) == len(retained_ids)
                and retained_refs == expected_retained_refs,
                "AI_ONLY_LABEL_INVALID",
                "AI-only retained candidate references differ",
            )
            require(
                (
                    label["label_kind"] == "ACCEPT_CANDIDATES"
                    and bool(retained_ids)
                    and label["exclusion_reason"] is None
                )
                or (
                    label["label_kind"] == "EXCLUDE"
                    and not retained_ids
                    and label["exclusion_reason"] is not None
                ),
                "AI_ONLY_DECISION_INVALID",
                "AI-only label disposition differs from retained candidates",
            )
            material = [
                canonical_sha256(
                    _project_action_predicate(
                        cast(Mapping[str, Any], inventory[candidate_id]["predicate"])
                    )
                )
                for candidate_id in retained_decision_ids
            ]
            require(
                len(material) == len(set(material)),
                "AI_ONLY_MATERIAL_DUPLICATE",
                "AI-only publication retains a material duplicate",
            )
            labels.append(label)
        label_set_sha256 = canonical_sha256(
            [
                {
                    "unit_id": item["unit_id"],
                    "sha256": item["sha256"],
                    "label_sha256": item["label_sha256"],
                }
                for item in label_refs
            ]
        )
        retained_count = sum(len(item["retained_candidate_refs"]) for item in labels)
        decided_count = sum(len(item["candidate_decisions"]) for item in labels)
        accepted_count = sum(item["label_kind"] == "ACCEPT_CANDIDATES" for item in labels)
        counts = {
            "campaign_units": CAMPAIGN_UNIT_COUNT,
            "human_locked_units": HUMAN_LOCKED_UNIT_COUNT,
            "ai_only_labeled_units": AI_ONLY_LABEL_COUNT,
            "accepted_candidate_units": accepted_count,
            "excluded_units": AI_ONLY_LABEL_COUNT - accepted_count,
            "retained_candidates": retained_count,
            "rejected_candidates": decided_count - retained_count,
            "decided_candidates": decided_count,
        }
        require(
            label_set_sha256
            == self.manifest["label_set_sha256"]
            == self.receipt["label_set_sha256"]
            and counts == self.manifest["counts"] == self.receipt["counts"]
            and self.receipt["label_index"] == self.manifest["label_index"],
            "AI_ONLY_INVALID",
            "AI-only label-set, counts, or receipt binding differs",
        )
        expected_files = {
            "publication-manifest.json",
            "publication-receipt.json",
            "label-index.jsonl",
            *(cast(str, item["path"]) for item in label_refs),
        }
        _assert_exact_census(resolved, expected_files)
        self.labels = labels

    @property
    def publication_id(self) -> str:
        return cast(str, self.manifest["publication_id"])

    def label_for(self, unit_id: str) -> dict[str, Any]:
        label = next((item for item in self.labels if item["unit_id"] == unit_id), None)
        require(label is not None, "AI_ONLY_UNIT_UNKNOWN", "AI-only unit is unknown")
        assert label is not None
        return cast(dict[str, Any], json_copy(label))


__all__ = [
    "AI_ONLY_SCHEMA_FILENAMES",
    "AI_ONLY_SCHEMA_ROOT",
    "AIOnlyActionLabelPublication",
    "BATCH_SLOTS",
    "build_ai_only_action_label_publication",
    "validate_ai_only_schema_record",
]
