"""Isolated, non-authoritative AI candidate assistance for G1.6 Action Gold.

The annotation website only reads an already sealed campaign.  Candidate generation is an
offline Codex research activity and is deliberately absent from the HTTP surface.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]

from mobile_world.offline.gold_curation.contracts import (
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    json_copy,
    require,
    validate_action_payload,
)
from mobile_world.offline.gold_curation.publication import (
    ACTIVE_G1_3_CAPSULE_SET_SHA256,
    ACTIVE_G1_3_MANIFEST_SHA256,
    ACTIVE_G1_3_PUBLICATION,
    CurationPublication,
)
from mobile_world.offline.gold_curation.store import (
    STABLE_PRINCIPAL_COMMITMENT_SCHEME,
    ReviewerRegistry,
    _ensure_child_directory,
    _ensure_root,
    _read_regular,
    _write_all,
    _write_once_regular,
)

AI_SCHEMA_ROOT: Final = (
    Path(__file__).resolve().parents[5] / "mobileworld_audit_handoff" / "schemas" / "g1_6_ai"
)
AI_SCHEMA_FILENAMES: Final = {
    "ai_action_gold_candidate_campaign.schema.json",
    "ai_action_gold_candidate_packet.schema.json",
    "ai_action_gold_candidate_output.schema.json",
    "ai_candidate_human_decision_event.schema.json",
    "ai_candidate_human_exposure.schema.json",
    "ai_candidate_generation_receipt.schema.json",
    "ai_candidate_campaign_receipt.schema.json",
    "ai_action_gold_candidate_browser.schema.json",
}
AI_PROMPT_PATH: Final = "mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md"
AI_PROMPT_ID: Final = "mobileworld.g1.ai-action-gold-candidate-prompt/v1"
AGENT_SLOTS: Final = ("A", "B", "C")
DECISIONS: Final = (
    "ADOPT_TO_FORM",
    "ADOPT_WITH_EDITS_TO_FORM",
    "USE_AS_SUPPLEMENT",
    "IGNORE",
)
MAX_DECISION_JOURNAL_BYTES: Final = 32 * 1024 * 1024
MAX_AGENT_DRAFT_BYTES: Final = 32 * 1024 * 1024

PACKET_SCHEMA_VERSION: Final = "mobileworld.g1.ai-action-gold-candidate-packet/v1"
CAMPAIGN_SCHEMA_VERSION: Final = "mobileworld.g1.ai-action-gold-candidate-campaign/v1"
OUTPUT_SCHEMA_VERSION: Final = "mobileworld.g1.ai-action-gold-candidate-output/v1"
DECISION_SCHEMA_VERSION: Final = "mobileworld.g1.ai-candidate-human-decision-event/v1"
RECEIPT_SCHEMA_VERSION: Final = "mobileworld.g1.ai-candidate-campaign-receipt/v1"
GENERATION_ATTESTATION_SCHEMA_VERSION: Final = (
    "mobileworld.g1.ai-candidate-generation-attestation/v1"
)

_AI_SCHEMA_ID_PREFIX: Final = "https://agentsentinel.local/schemas/g1_6_ai/"
_HEX_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_URL_RE: Final = re.compile(r"(?i)(?:\b(?:https?|ftp|file)://|\bwww\.|\bmailto:)")
_CREDENTIAL_RE: Final = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)"
    r"\s*[:=]\s*\S+|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}|\bAKIA[0-9A-Z]{16})"
)
_RAW_PATH_RE: Final = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])"
    r"(?:\.\.?/|~/|[a-z]:[\\/]|\\\\[^\\\s]+\\|/(?!/)[^\s'\"]+)"
)
_HTML_RE: Final = re.compile(r"(?s)<[^>]*>|[<>]")

_AUTHORITY: Final = {
    "counts_as_independent_review": False,
    "formal_resolution_eligible": False,
    "admission_eligible": False,
    "replay_eligible": False,
    "auto_apply_allowed": False,
    "human_review_required": True,
}
_OUTPUT_SAFETY: Final = {
    "target_actor_model_invoked": False,
    "project_gpu_used": False,
    "external_network_used": False,
    "replay_executed": False,
    "action_executed": False,
    "treatment_response_generation_allowed": False,
}
_CAMPAIGN_SAFETY: Final = {
    **_OUTPUT_SAFETY,
    "annotation_journal_write_allowed": False,
}
_INPUT_ATTESTATION: Final = {
    "only_frozen_packet_used": True,
    "history_used": False,
    "natural_action_used": False,
    "post_or_later_used": False,
    "outcome_used": False,
    "transformation_used": False,
    "human_review_used": False,
    "peer_agent_output_used": False,
    "chain_of_thought_stored": False,
}
_GENERATION_ATTESTATION_KEYS: Final = {
    "schema_version",
    "agent_slot",
    "draft_file_sha256",
    "generation_mode",
    "input_attestation",
    "peer_agent_output_visible",
    "human_feedback_visible",
    "provider_client_created",
    "project_model_weights_loaded",
    "safety",
    "attestation_sha256",
}


def _walk_schema_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_walk_schema_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_walk_schema_nodes(child))
    return nodes


def _resolve_json_pointer(schema: Any, fragment: str) -> None:
    require(
        fragment == "" or fragment.startswith("/"),
        "AI_CANDIDATE_SCHEMA_INVALID",
        "AI candidate schema uses an unsupported reference fragment",
    )
    current = schema
    if not fragment:
        return
    for encoded in fragment[1:].split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        require(
            isinstance(current, dict) and part in current,
            "AI_CANDIDATE_SCHEMA_INVALID",
            "AI candidate schema reference fragment is unresolved",
        )
        current = current[part]


def _assert_local_ai_schema_references(schemas: Mapping[str, dict[str, Any]]) -> None:
    """Resolve the complete `$ref` closure without invoking a URI retrieval path."""

    for schema_id, schema in schemas.items():
        for node in _walk_schema_nodes(schema):
            reference = node.get("$ref")
            if reference is None:
                continue
            require(
                isinstance(reference, str) and bool(reference),
                "AI_CANDIDATE_SCHEMA_INVALID",
                "AI candidate schema reference is invalid",
            )
            resolved, fragment = urldefrag(urljoin(schema_id, reference))
            require(
                resolved in schemas,
                "AI_CANDIDATE_SCHEMA_INVALID",
                "AI candidate schema reference is not in the pinned local schema set",
            )
            _resolve_json_pointer(schemas[resolved], fragment)


def _deny_ai_schema_retrieval(uri: str) -> Any:
    del uri
    raise CurationError(
        "AI_CANDIDATE_SCHEMA_INVALID",
        "AI candidate schema retrieval outside the pinned local set is forbidden",
    )


@lru_cache(maxsize=1)
def _load_ai_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    try:
        for schema_filename in AI_SCHEMA_FILENAMES:
            data = _read_regular(AI_SCHEMA_ROOT / schema_filename)
            assert data is not None
            schema = json.loads(data)
            expected_id = _AI_SCHEMA_ID_PREFIX + schema_filename
            require(
                isinstance(schema, dict)
                and schema.get("$id") == expected_id
                and expected_id not in schemas,
                "AI_CANDIDATE_SCHEMA_INVALID",
                "AI candidate schema identity is invalid",
            )
            Draft202012Validator.check_schema(schema)
            schemas[expected_id] = schema
        require(
            len(schemas) == len(AI_SCHEMA_FILENAMES),
            "AI_CANDIDATE_SCHEMA_INVALID",
            "AI candidate schema inventory differs",
        )
        _assert_local_ai_schema_references(schemas)
    except CurationError:
        raise
    except Exception as exc:
        raise CurationError(
            "AI_CANDIDATE_SCHEMA_INVALID", "AI candidate schema cannot be validated"
        ) from exc
    return schemas


@lru_cache(maxsize=8)
def _ai_validator(filename: str) -> Draft202012Validator:
    require(
        filename in AI_SCHEMA_FILENAMES,
        "AI_CANDIDATE_SCHEMA_INVALID",
        "unknown AI candidate schema",
    )
    schemas = _load_ai_schemas()
    value = schemas[_AI_SCHEMA_ID_PREFIX + filename]
    resolver = RefResolver.from_schema(
        value,
        store=schemas,
        handlers={
            "http": _deny_ai_schema_retrieval,
            "https": _deny_ai_schema_retrieval,
            "file": _deny_ai_schema_retrieval,
            "ftp": _deny_ai_schema_retrieval,
        },
    )
    return Draft202012Validator(value, resolver=resolver)


def _validate_untrusted_agent_value(value: Any, *, path: str = "$agent") -> None:
    """Recursively reject active/secret/path-bearing or non-canonical agent strings."""

    if isinstance(value, str):
        require(
            unicodedata.normalize("NFC", value) == value,
            "AI_CANDIDATE_UNTRUSTED_STRING",
            f"agent-authored string at {path} is not NFC",
        )
        require(
            not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value),
            "AI_CANDIDATE_UNTRUSTED_STRING",
            f"agent-authored string at {path} contains control characters",
        )
        require(
            _URL_RE.search(value) is None
            and _CREDENTIAL_RE.search(value) is None
            and _RAW_PATH_RE.search(value) is None
            and _HTML_RE.search(value) is None,
            "AI_CANDIDATE_UNTRUSTED_STRING",
            f"agent-authored string at {path} contains forbidden active or sensitive text",
        )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_untrusted_agent_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_untrusted_agent_value(key, path=f"{path}.<key>")
            _validate_untrusted_agent_value(item, path=f"{path}.{key}")


def validate_ai_schema_record(filename: str, value: Any) -> None:
    errors = sorted(_ai_validator(filename).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.absolute_path
        )
        raise CurationError(
            "AI_CANDIDATE_SCHEMA_MISMATCH",
            f"{filename} rejects runtime record at {path}: {first.message}",
        )


def _self_hash(value: dict[str, Any], field: str) -> str:
    subject = {key: json_copy(item) for key, item in value.items() if key != field}
    return canonical_sha256(subject)


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("AI_CANDIDATE_INVALID", f"{label} is invalid JSON") from exc
    require(isinstance(value, dict), "AI_CANDIDATE_INVALID", f"{label} must be an object")
    return cast(dict[str, Any], value)


def _schema_sha256(filename: str) -> str:
    data = _read_regular(AI_SCHEMA_ROOT / filename)
    assert data is not None
    return hashlib.sha256(data).hexdigest()


def _object_bytes(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _content_object_reference(kind: str, suffix: str, data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "path": f"{kind}/sha256/{digest[:2]}/{digest}{suffix}",
        "sha256": digest,
        "byte_count": len(data),
    }


def _write_content_object(root: Path, kind: str, suffix: str, data: bytes) -> dict[str, Any]:
    reference = _content_object_reference(kind, suffix, data)
    digest = cast(str, reference["sha256"])
    kind_root = _ensure_child_directory(root, kind)
    sha_root = _ensure_child_directory(kind_root, "sha256")
    prefix_root = _ensure_child_directory(sha_root, digest[:2])
    relative = f"{kind}/sha256/{digest[:2]}/{digest}{suffix}"
    _write_once_regular(prefix_root / f"{digest}{suffix}", data)
    require(
        reference["path"] == relative,
        "AI_CANDIDATE_INVALID",
        "candidate content reference construction differs",
    )
    return reference


def _publish_generation_slot(
    root: Path,
    *,
    agent_slot: str,
    output_objects: list[tuple[dict[str, Any], bytes]],
    receipt: dict[str, Any],
) -> None:
    """Stage one complete slot and roll back every newly linked object on failure."""

    receipt_bytes = _object_bytes(receipt)
    receipt_path = root / "generation-receipts" / f"slot-{agent_slot}.json"
    existing_receipt = _read_regular(receipt_path, missing_ok=True, owner_restricted=True)
    if existing_receipt is not None:
        require(
            existing_receipt == receipt_bytes,
            "ANNOTATION_STORE_COLLISION",
            "generation receipt collision",
        )
        for reference, data in output_objects:
            stored = _read_regular(root / reference["path"], owner_restricted=True)
            require(
                stored == data,
                "ANNOTATION_STORE_COLLISION",
                "generation output collision",
            )
        return

    staging = root / f".capture-slot-{agent_slot}.staging"
    require(
        not os.path.lexists(staging),
        "AI_CANDIDATE_CAPTURE_INCOMPLETE",
        "candidate capture staging already exists",
    )
    try:
        staging.mkdir(mode=0o700)
    except OSError as exc:
        raise CurationError(
            "AI_CANDIDATE_CAPTURE_INCOMPLETE", "candidate capture staging cannot be created"
        ) from exc
    created_files: list[Path] = []
    created_directories: list[Path] = []
    staged_files: list[Path] = []
    try:
        for reference, data in output_objects:
            staged = staging / f"{reference['sha256']}.json"
            _write_once_regular(staged, data)
            staged_files.append(staged)
        require(
            len(staged_files) == 190 and len({item.name for item in staged_files}) == 190,
            "AI_CANDIDATE_CAPTURE_INCOMPLETE",
            "candidate capture staging population differs",
        )
        for reference, data in output_objects:
            relative = PurePosixPath(cast(str, reference["path"]))
            current = root
            for part in relative.parts[:-1]:
                child = current / part
                existed = child.exists()
                current = _ensure_child_directory(current, part)
                if not existed:
                    created_directories.append(current)
            final = current / relative.name
            existing = _read_regular(final, missing_ok=True, owner_restricted=True)
            if existing is not None:
                require(
                    existing == data,
                    "ANNOTATION_STORE_COLLISION",
                    "generation output collision",
                )
                continue
            staged = staging / f"{reference['sha256']}.json"
            try:
                os.link(staged, final, follow_symlinks=False)
            except FileExistsError:
                existing = _read_regular(final, owner_restricted=True)
                require(
                    existing == data,
                    "ANNOTATION_STORE_COLLISION",
                    "generation output collision",
                )
            except OSError as exc:
                raise CurationError(
                    "AI_CANDIDATE_CAPTURE_INCOMPLETE",
                    "candidate output cannot be atomically linked from staging",
                ) from exc
            else:
                created_files.append(final)
        receipt_root_existed = (root / "generation-receipts").exists()
        receipt_root = _ensure_child_directory(root, "generation-receipts")
        if not receipt_root_existed:
            created_directories.append(receipt_root)
        _write_once_regular(receipt_root / f"slot-{agent_slot}.json", receipt_bytes)
        created_files.append(receipt_root / f"slot-{agent_slot}.json")
    except Exception:
        for path in reversed(created_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        for path in staged_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            staging.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CurationError(
                "AI_CANDIDATE_CAPTURE_INCOMPLETE",
                "candidate capture staging cleanup failed",
            ) from exc


def _read_content_object(
    root: Path,
    reference: dict[str, Any],
    *,
    kind: str,
    suffix: str,
) -> bytes:
    relative = reference.get("path")
    digest = reference.get("sha256")
    byte_count = reference.get("byte_count")
    require(
        isinstance(relative, str)
        and isinstance(digest, str)
        and len(digest) == 64
        and type(byte_count) is int
        and byte_count >= 1,
        "AI_CANDIDATE_REF_INVALID",
        "candidate object reference is invalid",
    )
    relative_text = cast(str, relative)
    digest_text = cast(str, digest)
    expected = f"{kind}/sha256/{digest_text[:2]}/{digest_text}{suffix}"
    require(
        relative_text == expected,
        "AI_CANDIDATE_REF_INVALID",
        "candidate object path differs",
    )
    pure = PurePosixPath(relative_text)
    require(
        not pure.is_absolute() and all(part not in {"", ".", ".."} for part in pure.parts),
        "AI_CANDIDATE_REF_INVALID",
        "candidate object path escapes its root",
    )
    current = root
    for part in pure.parts:
        current = current / part
        require(
            not current.is_symlink(),
            "AI_CANDIDATE_REF_INVALID",
            "candidate object path traverses a symlink",
        )
    data = _read_regular(current, owner_restricted=True)
    assert data is not None
    require(
        len(data) == cast(int, byte_count) and hashlib.sha256(data).hexdigest() == digest_text,
        "AI_CANDIDATE_REF_INVALID",
        "candidate object size or digest differs",
    )
    return data


def _candidate_filesystem_census(root: Path) -> tuple[set[str], set[str]]:
    """Return owner-restricted regular files/directories without following any link."""

    files: set[str] = set()
    directories: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise CurationError(
                "AI_CANDIDATE_CENSUS_INVALID", "candidate root cannot be enumerated"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CurationError(
                    "AI_CANDIDATE_CENSUS_INVALID", "candidate entry cannot be inspected"
                ) from exc
            require(
                not stat.S_ISLNK(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and metadata.st_mode & 0o077 == 0,
                "AI_CANDIDATE_CENSUS_INVALID",
                "candidate entry ownership, mode, or link type is unsafe",
            )
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                pending.append(path)
            else:
                require(
                    stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
                    "AI_CANDIDATE_CENSUS_INVALID",
                    "candidate entry is not a singly linked regular file",
                )
                files.add(relative)
    return files, directories


def _expected_parent_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        pure = PurePosixPath(relative)
        require(
            not pure.is_absolute() and all(part not in {"", ".", ".."} for part in pure.parts),
            "AI_CANDIDATE_CENSUS_INVALID",
            "candidate expected path is invalid",
        )
        parent = pure.parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _expected_campaign_files(
    manifest: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    generation_receipts: Mapping[str, Mapping[str, Any]],
    *,
    sealed: bool,
) -> set[str]:
    files = {"campaign-manifest.json"}
    files.update(cast(str, reference["path"]) for reference in manifest["packet_refs"])
    files.update(cast(str, packet["screenshot"]["path"]) for packet in packets.values())
    for slot, receipt in generation_receipts.items():
        files.add(f"generation-receipts/slot-{slot}.json")
        files.update(cast(str, reference["path"]) for reference in receipt["output_refs"])
    if sealed:
        files.add("campaign-receipt.json")
    return files


def _assert_exact_campaign_census(
    root: Path,
    manifest: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    generation_receipts: Mapping[str, Mapping[str, Any]],
    *,
    sealed: bool,
    allow_workspace_records: bool = False,
) -> None:
    actual_files, actual_directories = _candidate_filesystem_census(root)
    expected_files = _expected_campaign_files(manifest, packets, generation_receipts, sealed=sealed)
    if allow_workspace_records:
        for relative in actual_files:
            if relative in {
                "candidate-human-decisions.jsonl",
                "candidate-human-decisions.lock",
            } or re.fullmatch(r"human-exposures/[0-9a-f]{64}\.json", relative):
                expected_files.add(relative)
    output_files = {relative for relative in actual_files if relative.startswith("outputs/sha256/")}
    expected_outputs = {
        relative for relative in expected_files if relative.startswith("outputs/sha256/")
    }
    require(
        len(output_files) <= 570
        and output_files == expected_outputs
        and actual_files == expected_files
        and actual_directories == _expected_parent_directories(expected_files),
        "AI_CANDIDATE_CENSUS_INVALID",
        "candidate root contains a missing, extra, or orphan filesystem entry",
    )


def _existing_generation_receipts(root: Path, actual_files: set[str]) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for slot in AGENT_SLOTS:
        relative = f"generation-receipts/slot-{slot}.json"
        if relative in actual_files:
            receipts[slot] = _load_generation_receipt(root, slot)
    return receipts


def _semantic_predicate(item: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json_copy(item["predicate"]))


def _packet_id(*, campaign_id: str, unit_id: str) -> str:
    return (
        "g1aipacket-"
        + canonical_sha256(
            {"campaign_id": campaign_id, "unit_id": unit_id, "channel": "ACTION_GOLD"}
        )[:24]
    )


def _campaign_id(
    *, prompt_sha256: str, packet_schema_sha256: str, output_schema_sha256: str
) -> str:
    return (
        "g1aicampaign-"
        + canonical_sha256(
            {
                "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
                "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
                "prompt_sha256": prompt_sha256,
                "packet_schema_sha256": packet_schema_sha256,
                "output_schema_sha256": output_schema_sha256,
            }
        )[:24]
    )


def _evidence_id(item: Mapping[str, Any]) -> str:
    source_event = item.get("source_event")
    require(
        isinstance(item.get("evidence_role"), str)
        and isinstance(item.get("content_sha256"), str)
        and isinstance(source_event, dict)
        and isinstance(source_event.get("event_id"), str),
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "candidate source evidence identity inputs are invalid",
    )
    source_event_value = cast(dict[str, Any], source_event)
    return (
        "evidence-"
        + canonical_sha256(
            {
                "role": item["evidence_role"],
                "sha256": item["content_sha256"],
                "event_id": source_event_value["event_id"],
            }
        )[:24]
    )


def _decision_event_id(value: Mapping[str, Any]) -> str:
    subject = {
        key: json_copy(item)
        for key, item in value.items()
        if key not in {"event_id", "event_sha256"}
    }
    return "g1aidecision-" + canonical_sha256(subject)[:24]


def _candidate_id(value: dict[str, Any], *, campaign_id: str, unit_id: str, agent_slot: str) -> str:
    subject = {
        key: json_copy(item)
        for key, item in value.items()
        if key not in {"candidate_id", "candidate_sha256"}
    }
    return (
        "g1aicandidate-"
        + canonical_sha256(
            {
                "campaign_id": campaign_id,
                "unit_id": unit_id,
                "agent_slot": agent_slot,
                "candidate": subject,
            }
        )[:24]
    )


def _validate_candidate_items(
    publication: CurationPublication,
    unit_id: str,
    packet: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    campaign_id: str,
    agent_slot: str,
) -> list[dict[str, Any]]:
    width = packet["screenshot"]["width"]
    height = packet["screenshot"]["height"]
    for item in items:
        predicate = item["predicate"]
        if predicate["predicate_kind"] != "EXACT_NORMALIZED_ACTION":
            continue
        action = predicate["normalized_action"]["value"]
        for coordinate, limit in (
            ("x", width),
            ("start_x", width),
            ("end_x", width),
            ("y", height),
            ("start_y", height),
            ("end_y", height),
        ):
            value = action[coordinate]
            require(
                value is None or type(value) is int and 0 <= value < limit,
                "AI_CANDIDATE_GEOMETRY_INVALID",
                f"candidate exact action {coordinate} is outside target-pre pixels",
            )
    formal_predicates = [
        {
            **_semantic_predicate(item),
            "evidence_ids": json_copy(item["evidence_ids"]),
            "rationale": item["concise_rationale"],
            "human_selected": True,
        }
        for item in items
    ]
    payload = validate_action_payload(
        {
            "proposal_kind": "ACTION_GOLD",
            "disposition": "ACCEPT",
            "exclusion_reason": None,
            "predicates": formal_predicates,
            "evidence_rationale": "AI candidates remain untrusted and require human review.",
            "closed_world_confirmed": True,
            "all_reasonable_actions_enumerated": True,
        }
    )
    publication.validate_review_payload_binding(unit_id, "ACTION_GOLD", payload)
    admitted = set(packet["evidence_ids"])
    validated: list[dict[str, Any]] = []
    for original, formal in zip(items, payload["predicates"], strict=True):
        require(
            set(original["evidence_ids"]) <= admitted,
            "AI_CANDIDATE_VISIBILITY_VIOLATION",
            "candidate cites evidence outside its frozen packet",
        )
        semantic = {
            key: json_copy(value)
            for key, value in formal.items()
            if key not in {"evidence_ids", "rationale", "human_selected"}
        }
        require(
            semantic == original["predicate"],
            "AI_CANDIDATE_INVALID",
            "candidate predicate differs after formal production validation",
        )
        expected_sha = _self_hash(original, "candidate_sha256")
        require(
            original["candidate_sha256"] == expected_sha
            and original["candidate_id"]
            == _candidate_id(
                original,
                campaign_id=campaign_id,
                unit_id=unit_id,
                agent_slot=agent_slot,
            ),
            "AI_CANDIDATE_INVALID",
            "candidate identity or digest differs",
        )
        validated.append(cast(dict[str, Any], json_copy(original)))
    return validated


def _validate_packet(
    root: Path,
    publication: CurationPublication,
    value: dict[str, Any],
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_ai_schema_record("ai_action_gold_candidate_packet.schema.json", value)
    require(
        value["packet_sha256"] == _self_hash(value, "packet_sha256"),
        "AI_CANDIDATE_INVALID",
        "candidate packet self digest differs",
    )
    require(
        hashlib.sha256(value["task_instruction"].encode("utf-8")).hexdigest()
        == value["task_instruction_sha256"],
        "AI_CANDIDATE_INVALID",
        "candidate task instruction digest differs",
    )
    source = publication.packet(value["unit_id"], "ACTION_GOLD")
    require(
        all(item["evidence_id"] == _evidence_id(item) for item in source["evidence"]),
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "active blind source evidence identity differs from its frozen derivation",
    )
    target_pre = [item for item in source["evidence"] if item["evidence_role"] == "target_pre"]
    require(
        len(target_pre) == 1,
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "candidate packet must have one target-pre evidence item",
    )
    expected_auxiliary = [
        {
            "evidence_id": item["evidence_id"],
            "evidence_role": item["evidence_role"],
            "content_sha256": item["content_sha256"],
            "content": json_copy(item["content"]),
        }
        for item in source["evidence"]
        if item["evidence_role"] in {"tool_response", "ask_user_response"}
    ]
    require(
        value["packet_id"] == _packet_id(campaign_id=value["campaign_id"], unit_id=value["unit_id"])
        and value["source_packet_sha256"] == canonical_sha256(source)
        and value["task_instruction"] == source["task"]["instruction"]
        and value["task_instruction_sha256"] == source["task"]["instruction_sha256"]
        and value["evidence_ids"] == [item["evidence_id"] for item in source["evidence"]]
        and value["auxiliary_evidence"] == expected_auxiliary,
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "candidate packet differs from the active blind source packet",
    )
    screenshot_data = _read_content_object(
        root, value["screenshot"], kind="screenshots", suffix=".png"
    )
    actual, media_type, digest = publication.screenshot_bytes(value["unit_id"])
    require(
        screenshot_data == actual
        and value["screenshot"]["evidence_id"] == target_pre[0]["evidence_id"]
        and media_type == value["screenshot"]["media_type"]
        and digest == value["screenshot"]["sha256"]
        and value["screenshot"]["width"] == source["current_screenshot"]["width"]
        and value["screenshot"]["height"] == source["current_screenshot"]["height"],
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "candidate screenshot differs from active target-pre pixels",
    )
    if reference is not None:
        data = _object_bytes(value)
        require(
            hashlib.sha256(data).hexdigest() == reference["sha256"]
            and len(data) == reference["byte_count"],
            "AI_CANDIDATE_REF_INVALID",
            "candidate packet reference differs",
        )
    return cast(dict[str, Any], json_copy(value))


def _validate_output(
    publication: CurationPublication,
    campaign: dict[str, Any],
    packet: dict[str, Any],
    value: dict[str, Any],
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_untrusted_agent_value(
        {
            "candidate_items": value.get("candidate_items"),
            "abstain_reason": value.get("abstain_reason"),
        }
    )
    validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", value)
    require(
        value["agent_output_sha256"] == _self_hash(value, "agent_output_sha256"),
        "AI_CANDIDATE_INVALID",
        "candidate output self digest differs",
    )
    require(
        value["campaign_id"] == campaign["campaign_id"]
        and value["unit_id"] == packet["unit_id"]
        and value["source_packet_sha256"] == packet["source_packet_sha256"]
        and value["prompt_sha256"] == campaign["prompt_binding"]["sha256"],
        "AI_CANDIDATE_SOURCE_MISMATCH",
        "candidate output source binding differs",
    )
    items = cast(list[dict[str, Any]], value["candidate_items"])
    if items:
        value["candidate_items"] = _validate_candidate_items(
            publication,
            value["unit_id"],
            packet,
            items,
            campaign_id=value["campaign_id"],
            agent_slot=value["agent_slot"],
        )
    if reference is not None:
        data = _object_bytes(value)
        require(
            hashlib.sha256(data).hexdigest() == reference["sha256"]
            and len(data) == reference["byte_count"],
            "AI_CANDIDATE_REF_INVALID",
            "candidate output reference differs",
        )
    return cast(dict[str, Any], json_copy(value))


def prepare_ai_action_gold_campaign(
    root: str | os.PathLike[str],
    publication: CurationPublication,
    *,
    repository_root: str | os.PathLike[str] | None = None,
    forbidden_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Freeze the 190 minimal blind packets; never invoke a model or external capability."""

    repo = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
    campaign_root = _ensure_root(Path(root), (repo, ACTIVE_G1_3_PUBLICATION, *forbidden_roots))
    initial_files, initial_directories = _candidate_filesystem_census(campaign_root)
    require(
        not initial_files and not initial_directories,
        "AI_CANDIDATE_CENSUS_INVALID",
        "candidate campaign preparation requires an empty root",
    )
    prompt_path = repo / AI_PROMPT_PATH
    prompt_data = _read_regular(prompt_path)
    assert prompt_data is not None
    prompt_sha = hashlib.sha256(prompt_data).hexdigest()
    packet_schema_sha = _schema_sha256("ai_action_gold_candidate_packet.schema.json")
    output_schema_sha = _schema_sha256("ai_action_gold_candidate_output.schema.json")
    campaign_id = _campaign_id(
        prompt_sha256=prompt_sha,
        packet_schema_sha256=packet_schema_sha,
        output_schema_sha256=output_schema_sha,
    )
    refs: list[dict[str, Any]] = []
    packet_values: dict[str, dict[str, Any]] = {}
    units = sorted(publication.list_units(), key=lambda item: item["unit_id"])
    require(len(units) == 190, "AI_CANDIDATE_SOURCE_MISMATCH", "active unit count differs")
    for unit in units:
        unit_id = cast(str, unit["unit_id"])
        source = publication.packet(unit_id, "ACTION_GOLD")
        screenshot_data, media_type, screenshot_sha = publication.screenshot_bytes(unit_id)
        screenshot_ref = _write_content_object(
            campaign_root, "screenshots", ".png", screenshot_data
        )
        target_pre = [item for item in source["evidence"] if item["evidence_role"] == "target_pre"]
        require(
            len(target_pre) == 1,
            "AI_CANDIDATE_SOURCE_MISMATCH",
            "candidate packet must have one target-pre evidence item",
        )
        auxiliary = [
            {
                "evidence_id": item["evidence_id"],
                "evidence_role": item["evidence_role"],
                "content_sha256": item["content_sha256"],
                "content": json_copy(item["content"]),
            }
            for item in source["evidence"]
            if item["evidence_role"] in {"tool_response", "ask_user_response"}
        ]
        packet: dict[str, Any] = {
            "schema_version": PACKET_SCHEMA_VERSION,
            "record_type": "ai_action_gold_candidate_packet",
            "campaign_id": campaign_id,
            "packet_id": _packet_id(campaign_id=campaign_id, unit_id=unit_id),
            "unit_id": unit_id,
            "source_packet_sha256": canonical_sha256(source),
            "task_instruction": source["task"]["instruction"],
            "task_instruction_sha256": source["task"]["instruction_sha256"],
            "evidence_ids": [item["evidence_id"] for item in source["evidence"]],
            "auxiliary_evidence": auxiliary,
            "screenshot": {
                "evidence_id": target_pre[0]["evidence_id"],
                **screenshot_ref,
                "media_type": media_type,
                "width": source["current_screenshot"]["width"],
                "height": source["current_screenshot"]["height"],
            },
            "visibility": {
                "task_instruction_visible": True,
                "target_pre_visible": True,
                "history_visible": False,
                "natural_target_output_visible": False,
                "target_post_visible": False,
                "later_trajectory_visible": False,
                "outcome_visible": False,
                "transformation_visible": False,
                "human_review_visible": False,
                "peer_agent_output_visible": False,
                "replay_response_visible": False,
            },
            "authority": {
                "counts_as_independent_review": False,
                "formal_resolution_eligible": False,
                "admission_eligible": False,
                "replay_eligible": False,
            },
        }
        packet["packet_sha256"] = _self_hash(packet, "packet_sha256")
        _validate_packet(campaign_root, publication, packet)
        packet_ref = _write_content_object(campaign_root, "packets", ".json", _object_bytes(packet))
        refs.append({"unit_id": unit_id, **packet_ref})
        packet_values[unit_id] = cast(dict[str, Any], json_copy(packet))
    manifest: dict[str, Any] = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "record_type": "ai_action_gold_candidate_campaign",
        "campaign_id": campaign_id,
        "created_at_ns": time.time_ns(),
        "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
        "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
        "unit_count": 190,
        "agent_slots": list(AGENT_SLOTS),
        "prompt_binding": {
            "prompt_id": AI_PROMPT_ID,
            "path": AI_PROMPT_PATH,
            "sha256": prompt_sha,
        },
        "packet_schema_sha256": packet_schema_sha,
        "output_schema_sha256": output_schema_sha,
        "packet_refs": refs,
        "generation_policy": {
            "three_isolated_streams": True,
            "same_frozen_prompt": True,
            "peer_outputs_visible": False,
            "human_feedback_regeneration_allowed": False,
            "ranking_allowed": False,
            "merging_allowed": False,
            "chain_of_thought_stored": False,
            "ai_semantic_suggestion_performed": True,
        },
        "safety": json_copy(_CAMPAIGN_SAFETY),
    }
    manifest["campaign_manifest_sha256"] = _self_hash(manifest, "campaign_manifest_sha256")
    validate_ai_schema_record("ai_action_gold_candidate_campaign.schema.json", manifest)
    _write_once_regular(campaign_root / "campaign-manifest.json", _object_bytes(manifest))
    _assert_exact_campaign_census(
        campaign_root,
        manifest,
        packet_values,
        {},
        sealed=False,
    )
    return cast(dict[str, Any], json_copy(manifest))


def _load_manifest(root: Path, repository_root: Path) -> dict[str, Any]:
    data = _read_regular(root / "campaign-manifest.json", owner_restricted=True)
    assert data is not None
    manifest = _parse_json_object(data, "candidate campaign manifest")
    require(data == _object_bytes(manifest), "AI_CANDIDATE_INVALID", "manifest is not canonical")
    validate_ai_schema_record("ai_action_gold_candidate_campaign.schema.json", manifest)
    require(
        manifest["campaign_manifest_sha256"] == _self_hash(manifest, "campaign_manifest_sha256")
        and manifest["campaign_id"]
        == _campaign_id(
            prompt_sha256=manifest["prompt_binding"]["sha256"],
            packet_schema_sha256=manifest["packet_schema_sha256"],
            output_schema_sha256=manifest["output_schema_sha256"],
        )
        and manifest["publication_manifest_sha256"] == ACTIVE_G1_3_MANIFEST_SHA256
        and manifest["capsule_set_sha256"] == ACTIVE_G1_3_CAPSULE_SET_SHA256
        and manifest["packet_schema_sha256"]
        == _schema_sha256("ai_action_gold_candidate_packet.schema.json")
        and manifest["output_schema_sha256"]
        == _schema_sha256("ai_action_gold_candidate_output.schema.json"),
        "AI_CANDIDATE_INVALID",
        "campaign manifest binding differs",
    )
    require(
        [item["unit_id"] for item in manifest["packet_refs"]]
        == sorted(item["unit_id"] for item in manifest["packet_refs"]),
        "AI_CANDIDATE_INVALID",
        "candidate packet index is not in frozen unit order",
    )
    prompt_data = _read_regular(repository_root / manifest["prompt_binding"]["path"])
    assert prompt_data is not None
    require(
        hashlib.sha256(prompt_data).hexdigest() == manifest["prompt_binding"]["sha256"],
        "AI_CANDIDATE_INVALID",
        "candidate prompt binding differs",
    )
    return manifest


def _load_packets(
    root: Path, publication: CurationPublication, manifest: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    packets: dict[str, dict[str, Any]] = {}
    for reference in manifest["packet_refs"]:
        data = _read_content_object(root, reference, kind="packets", suffix=".json")
        value = _parse_json_object(data, "candidate packet")
        require(data == _object_bytes(value), "AI_CANDIDATE_INVALID", "packet is not canonical")
        packet = _validate_packet(root, publication, value, reference)
        require(
            packet["campaign_id"] == manifest["campaign_id"]
            and packet["unit_id"] == reference["unit_id"]
            and packet["unit_id"] not in packets,
            "AI_CANDIDATE_INVALID",
            "candidate packet index differs",
        )
        packets[packet["unit_id"]] = packet
    require(len(packets) == 190, "AI_CANDIDATE_INVALID", "candidate packet count differs")
    return packets


def _drafts_from_jsonl(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    data = _read_regular(path, owner_restricted=True)
    assert data is not None
    require(
        bool(data)
        and len(data) <= MAX_AGENT_DRAFT_BYTES
        and data.endswith(b"\n")
        and b"\r" not in data,
        "AI_CANDIDATE_DRAFT_INVALID",
        "agent draft must be LF-terminated JSONL",
    )
    rows: list[dict[str, Any]] = []
    for line in data.splitlines():
        require(bool(line), "AI_CANDIDATE_DRAFT_INVALID", "agent draft has an empty line")
        rows.append(_parse_json_object(line, "agent draft row"))
    return data, rows


def _validate_generation_attestation(
    value: Mapping[str, Any], *, agent_slot: str, draft_file_sha256: str
) -> dict[str, Any]:
    attestation = cast(dict[str, Any], json_copy(value))
    require(
        set(attestation) == _GENERATION_ATTESTATION_KEYS
        and attestation.get("schema_version") == GENERATION_ATTESTATION_SCHEMA_VERSION
        and attestation.get("agent_slot") == agent_slot
        and attestation.get("draft_file_sha256") == draft_file_sha256
        and attestation.get("generation_mode") == "ISOLATED_CODEX_RESEARCH_STREAM"
        and attestation.get("input_attestation") == _INPUT_ATTESTATION
        and attestation.get("peer_agent_output_visible") is False
        and attestation.get("human_feedback_visible") is False
        and attestation.get("provider_client_created") is False
        and attestation.get("project_model_weights_loaded") is False
        and attestation.get("safety") == _OUTPUT_SAFETY
        and attestation.get("attestation_sha256") == _self_hash(attestation, "attestation_sha256"),
        "AI_CANDIDATE_GENERATION_ATTESTATION_INVALID",
        "caller-provided candidate generation attestation differs",
    )
    return attestation


def capture_ai_candidate_slot(
    root: str | os.PathLike[str],
    publication: CurationPublication,
    *,
    agent_slot: str,
    draft_jsonl_path: str | os.PathLike[str],
    generation_attestation: Mapping[str, Any],
    repository_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Compile one isolated stream's 190 draft rows into validated immutable envelopes."""

    require(agent_slot in AGENT_SLOTS, "AI_CANDIDATE_DRAFT_INVALID", "agent slot is invalid")
    repo = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
    campaign_root = Path(root).resolve(strict=True)
    actual_files, _ = _candidate_filesystem_census(campaign_root)
    manifest = _load_manifest(campaign_root, repo)
    packets = _load_packets(campaign_root, publication, manifest)
    require(
        "campaign-receipt.json" not in actual_files,
        "AI_CANDIDATE_CAPTURE_INCOMPLETE",
        "sealed candidate campaign cannot accept another capture",
    )
    existing_receipts = _existing_generation_receipts(campaign_root, actual_files)
    _assert_exact_campaign_census(
        campaign_root,
        manifest,
        packets,
        existing_receipts,
        sealed=False,
    )
    draft_data, rows = _drafts_from_jsonl(Path(draft_jsonl_path))
    draft_sha256 = hashlib.sha256(draft_data).hexdigest()
    attestation = _validate_generation_attestation(
        generation_attestation,
        agent_slot=agent_slot,
        draft_file_sha256=draft_sha256,
    )
    require(len(rows) == 190, "AI_CANDIDATE_DRAFT_INVALID", "agent draft count differs")
    require(
        [row.get("unit_id") for row in rows] == sorted(packets),
        "AI_CANDIDATE_DRAFT_INVALID",
        "agent draft units must exactly match the sorted frozen inventory",
    )
    by_unit: dict[str, dict[str, Any]] = {}
    output_refs: list[dict[str, Any]] = []
    output_objects: list[tuple[dict[str, Any], bytes]] = []
    for row in rows:
        require(
            set(row) == {"unit_id", "response_kind", "candidate_items", "abstain_reason"},
            "AI_CANDIDATE_DRAFT_INVALID",
            "agent draft row is not closed",
        )
        unit_id = row["unit_id"]
        require(
            isinstance(unit_id, str) and unit_id in packets and unit_id not in by_unit,
            "AI_CANDIDATE_DRAFT_INVALID",
            "agent draft unit is unknown or duplicated",
        )
        by_unit[unit_id] = row
        raw_items = row["candidate_items"]
        require(
            isinstance(raw_items, list),
            "AI_CANDIDATE_DRAFT_INVALID",
            "candidate items must be an array",
        )
        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            require(
                isinstance(raw_item, dict)
                and set(raw_item)
                == {"predicate", "evidence_ids", "concise_rationale", "uncertainty_note"},
                "AI_CANDIDATE_DRAFT_INVALID",
                "candidate draft item is not closed",
            )
            item = {
                "candidate_kind": "ACTION_PREDICATE",
                **json_copy(raw_item),
            }
            item["candidate_id"] = _candidate_id(
                item,
                campaign_id=manifest["campaign_id"],
                unit_id=unit_id,
                agent_slot=agent_slot,
            )
            item["candidate_sha256"] = _self_hash(item, "candidate_sha256")
            require(
                item["candidate_id"]
                == _candidate_id(
                    item,
                    campaign_id=manifest["campaign_id"],
                    unit_id=unit_id,
                    agent_slot=agent_slot,
                ),
                "AI_CANDIDATE_DRAFT_INVALID",
                "candidate identity construction differs",
            )
            items.append(item)
        response_kind = row["response_kind"]
        require(
            (response_kind == "CANDIDATES" and bool(items) and row["abstain_reason"] is None)
            or (
                response_kind == "ABSTAIN"
                and not items
                and isinstance(row["abstain_reason"], str)
                and bool(row["abstain_reason"].strip())
            ),
            "AI_CANDIDATE_DRAFT_INVALID",
            "candidate response kind and content differ",
        )
        output: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "record_type": "ai_action_gold_candidate_output",
            "campaign_id": manifest["campaign_id"],
            "unit_id": unit_id,
            "source_packet_sha256": packets[unit_id]["source_packet_sha256"],
            "agent_slot": agent_slot,
            "prompt_sha256": manifest["prompt_binding"]["sha256"],
            "response_kind": response_kind,
            "candidate_items": items,
            "abstain_reason": row["abstain_reason"],
            "input_attestation": json_copy(attestation["input_attestation"]),
            "authority": json_copy(_AUTHORITY),
            "safety": json_copy(attestation["safety"]),
        }
        output["agent_output_sha256"] = _self_hash(output, "agent_output_sha256")
        output = _validate_output(publication, manifest, packets[unit_id], output)
        output_data = _object_bytes(output)
        reference = _content_object_reference("outputs", ".json", output_data)
        output_objects.append((reference, output_data))
        output_refs.append({"unit_id": unit_id, "agent_slot": agent_slot, **reference})
    require(set(by_unit) == set(packets), "AI_CANDIDATE_DRAFT_INVALID", "agent units differ")
    output_refs.sort(key=lambda item: item["unit_id"])
    receipt: dict[str, Any] = {
        "schema_version": "mobileworld.g1.ai-candidate-generation-receipt/v1",
        "record_type": "ai_candidate_generation_receipt",
        "campaign_id": manifest["campaign_id"],
        "agent_slot": agent_slot,
        "draft_file_sha256": draft_sha256,
        "generation_attestation": json_copy(attestation),
        "output_count": 190,
        "output_refs": output_refs,
        "peer_agent_output_visible": attestation["peer_agent_output_visible"],
        "human_feedback_visible": attestation["human_feedback_visible"],
        "chain_of_thought_stored": attestation["input_attestation"]["chain_of_thought_stored"],
        "target_actor_model_invoked": attestation["safety"]["target_actor_model_invoked"],
        "project_gpu_used": attestation["safety"]["project_gpu_used"],
        "external_network_used": attestation["safety"]["external_network_used"],
        "replay_executed": attestation["safety"]["replay_executed"],
        "action_executed": attestation["safety"]["action_executed"],
    }
    receipt["receipt_sha256"] = _self_hash(receipt, "receipt_sha256")
    validate_ai_schema_record("ai_candidate_generation_receipt.schema.json", receipt)
    _publish_generation_slot(
        campaign_root,
        agent_slot=agent_slot,
        output_objects=output_objects,
        receipt=receipt,
    )
    final_files, _ = _candidate_filesystem_census(campaign_root)
    final_receipts = _existing_generation_receipts(campaign_root, final_files)
    _assert_exact_campaign_census(
        campaign_root,
        manifest,
        packets,
        final_receipts,
        sealed=False,
    )
    return cast(dict[str, Any], json_copy(receipt))


def _load_generation_receipt(root: Path, slot: str) -> dict[str, Any]:
    data = _read_regular(root / "generation-receipts" / f"slot-{slot}.json", owner_restricted=True)
    assert data is not None
    value = _parse_json_object(data, "generation receipt")
    validate_ai_schema_record("ai_candidate_generation_receipt.schema.json", value)
    require(
        data == _object_bytes(value)
        and set(value)
        == {
            "schema_version",
            "record_type",
            "campaign_id",
            "agent_slot",
            "draft_file_sha256",
            "generation_attestation",
            "output_count",
            "output_refs",
            "peer_agent_output_visible",
            "human_feedback_visible",
            "chain_of_thought_stored",
            "target_actor_model_invoked",
            "project_gpu_used",
            "external_network_used",
            "replay_executed",
            "action_executed",
            "receipt_sha256",
        }
        and value["agent_slot"] == slot
        and value["generation_attestation"]
        == _validate_generation_attestation(
            value["generation_attestation"],
            agent_slot=slot,
            draft_file_sha256=value["draft_file_sha256"],
        )
        and value["output_count"] == 190
        and len(value["output_refs"]) == 190
        and [item["unit_id"] for item in value["output_refs"]]
        == sorted(item["unit_id"] for item in value["output_refs"])
        and all(item["agent_slot"] == slot for item in value["output_refs"])
        and value["peer_agent_output_visible"] is False
        and value["peer_agent_output_visible"]
        == value["generation_attestation"]["peer_agent_output_visible"]
        and value["human_feedback_visible"] is False
        and value["human_feedback_visible"]
        == value["generation_attestation"]["human_feedback_visible"]
        and value["chain_of_thought_stored"] is False
        and value["chain_of_thought_stored"]
        == value["generation_attestation"]["input_attestation"]["chain_of_thought_stored"]
        and value["target_actor_model_invoked"] is False
        and value["target_actor_model_invoked"]
        == value["generation_attestation"]["safety"]["target_actor_model_invoked"]
        and value["project_gpu_used"] is False
        and value["project_gpu_used"]
        == value["generation_attestation"]["safety"]["project_gpu_used"]
        and value["external_network_used"] is False
        and value["external_network_used"]
        == value["generation_attestation"]["safety"]["external_network_used"]
        and value["replay_executed"] is False
        and value["replay_executed"] == value["generation_attestation"]["safety"]["replay_executed"]
        and value["action_executed"] is False
        and value["action_executed"] == value["generation_attestation"]["safety"]["action_executed"]
        and value["receipt_sha256"] == _self_hash(value, "receipt_sha256"),
        "AI_CANDIDATE_INVALID",
        "generation receipt differs",
    )
    return value


def _generation_receipt_binding(root: Path, slot: str, value: Mapping[str, Any]) -> dict[str, Any]:
    path = root / "generation-receipts" / f"slot-{slot}.json"
    data = _read_regular(path, owner_restricted=True)
    assert data is not None
    require(
        data == _object_bytes(cast(dict[str, Any], value)),
        "AI_CANDIDATE_INVALID",
        "generation receipt bytes differ from the validated record",
    )
    return {
        "schema_version": value["schema_version"],
        "agent_slot": slot,
        "path": f"generation-receipts/slot-{slot}.json",
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "receipt_sha256": value["receipt_sha256"],
    }


def seal_ai_candidate_campaign(
    root: str | os.PathLike[str],
    publication: CurationPublication,
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Close all three isolated slot receipts into one 570-output campaign receipt."""

    repo = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
    campaign_root = Path(root).resolve(strict=True)
    actual_files, _ = _candidate_filesystem_census(campaign_root)
    manifest = _load_manifest(campaign_root, repo)
    packets = _load_packets(campaign_root, publication, manifest)
    generation_receipts = _existing_generation_receipts(campaign_root, actual_files)
    require(
        set(generation_receipts) == set(AGENT_SLOTS),
        "AI_CANDIDATE_INVALID",
        "candidate campaign requires exactly three generation receipts",
    )
    already_sealed = "campaign-receipt.json" in actual_files
    _assert_exact_campaign_census(
        campaign_root,
        manifest,
        packets,
        generation_receipts,
        sealed=already_sealed,
    )
    refs: list[dict[str, Any]] = []
    generation_bindings: list[dict[str, Any]] = []
    for slot in AGENT_SLOTS:
        generation = generation_receipts[slot]
        require(
            generation["campaign_id"] == manifest["campaign_id"],
            "AI_CANDIDATE_INVALID",
            "generation receipt campaign differs",
        )
        generation_bindings.append(_generation_receipt_binding(campaign_root, slot, generation))
        for reference in generation["output_refs"]:
            data = _read_content_object(campaign_root, reference, kind="outputs", suffix=".json")
            output = _parse_json_object(data, "candidate output")
            _validate_output(
                publication, manifest, packets[reference["unit_id"]], output, reference
            )
            require(
                output["agent_slot"] == slot and output["unit_id"] == reference["unit_id"],
                "AI_CANDIDATE_INVALID",
                "candidate output index differs",
            )
            refs.append(cast(dict[str, Any], json_copy(reference)))
    pairs = {(item["unit_id"], item["agent_slot"]) for item in refs}
    require(
        len(refs) == 570
        and len(pairs) == 570
        and {unit_id for unit_id, _ in pairs} == set(packets),
        "AI_CANDIDATE_INVALID",
        "candidate output population differs",
    )
    refs.sort(key=lambda item: (item["unit_id"], item["agent_slot"]))
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "record_type": "ai_candidate_campaign_receipt",
        "campaign_id": manifest["campaign_id"],
        "campaign_manifest_sha256": manifest["campaign_manifest_sha256"],
        "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
        "packet_count": 190,
        "agent_slot_count": 3,
        "output_count": 570,
        "slot_output_counts": {slot: 190 for slot in AGENT_SLOTS},
        "generation_receipts": generation_bindings,
        "output_refs": refs,
        "candidate_set_sha256": canonical_sha256(
            [
                {
                    "unit_id": item["unit_id"],
                    "agent_slot": item["agent_slot"],
                    "sha256": item["sha256"],
                }
                for item in refs
            ]
        ),
        "disclosure": {
            "ai_semantic_suggestion_performed": True,
            "blind_task_and_gui_entered_codex_context": True,
            "three_agents_are_independent_human_reviewers": False,
        },
        "authority": {
            "counts_as_independent_review": False,
            "formal_resolution_eligible": False,
            "admission_eligible": False,
            "promotion_allowed": False,
            "replay_eligible": False,
            "human_review_required": True,
        },
        "safety": json_copy(_CAMPAIGN_SAFETY),
    }
    receipt["receipt_sha256"] = _self_hash(receipt, "receipt_sha256")
    validate_ai_schema_record("ai_candidate_campaign_receipt.schema.json", receipt)
    _write_once_regular(campaign_root / "campaign-receipt.json", _object_bytes(receipt))
    _assert_exact_campaign_census(
        campaign_root,
        manifest,
        packets,
        generation_receipts,
        sealed=True,
    )
    return cast(dict[str, Any], json_copy(receipt))


class AICandidateWorkspace:
    """Read a sealed campaign and append only separate human candidate decisions."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        publication: CurationPublication,
        *,
        repository_root: str | os.PathLike[str] | None = None,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        repo = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
        require(
            Path(root).exists(),
            "AI_CANDIDATE_ROOT_INVALID",
            "candidate workspace root is missing",
        )
        self.root = _ensure_root(Path(root), (repo, ACTIVE_G1_3_PUBLICATION, *forbidden_roots))
        self.publication = publication
        self._repository_root = repo
        actual_files, _ = _candidate_filesystem_census(self.root)
        self.manifest = _load_manifest(self.root, repo)
        self._packets = _load_packets(self.root, publication, self.manifest)
        generation_receipts = _existing_generation_receipts(self.root, actual_files)
        require(
            set(generation_receipts) == set(AGENT_SLOTS)
            and "campaign-receipt.json" in actual_files,
            "AI_CANDIDATE_CENSUS_INVALID",
            "sealed candidate workspace inventory is incomplete",
        )
        _assert_exact_campaign_census(
            self.root,
            self.manifest,
            self._packets,
            generation_receipts,
            sealed=True,
            allow_workspace_records=True,
        )
        receipt_data = _read_regular(self.root / "campaign-receipt.json", owner_restricted=True)
        assert receipt_data is not None
        self.receipt = _parse_json_object(receipt_data, "candidate campaign receipt")
        require(
            receipt_data == _object_bytes(self.receipt),
            "AI_CANDIDATE_INVALID",
            "campaign receipt is not canonical",
        )
        validate_ai_schema_record("ai_candidate_campaign_receipt.schema.json", self.receipt)
        require(
            self.receipt["receipt_sha256"] == _self_hash(self.receipt, "receipt_sha256")
            and self.receipt["campaign_id"] == self.manifest["campaign_id"]
            and self.receipt["publication_manifest_sha256"] == ACTIVE_G1_3_MANIFEST_SHA256
            and self.receipt["campaign_manifest_sha256"]
            == self.manifest["campaign_manifest_sha256"],
            "AI_CANDIDATE_INVALID",
            "candidate receipt binding differs",
        )
        require(
            [(item["unit_id"], item["agent_slot"]) for item in self.receipt["output_refs"]]
            == sorted(
                (item["unit_id"], item["agent_slot"]) for item in self.receipt["output_refs"]
            ),
            "AI_CANDIDATE_INVALID",
            "candidate sealed output index is not in frozen unit/slot order",
        )
        generation_refs: list[dict[str, Any]] = []
        generation_bindings: list[dict[str, Any]] = []
        for slot in AGENT_SLOTS:
            generation = generation_receipts[slot]
            require(
                generation["campaign_id"] == self.campaign_id,
                "AI_CANDIDATE_INVALID",
                "candidate generation receipt campaign differs",
            )
            generation_bindings.append(_generation_receipt_binding(self.root, slot, generation))
            generation_refs.extend(json_copy(generation["output_refs"]))
        generation_refs.sort(key=lambda item: (item["unit_id"], item["agent_slot"]))
        require(
            generation_refs == self.receipt["output_refs"]
            and generation_bindings == self.receipt["generation_receipts"],
            "AI_CANDIDATE_INVALID",
            "candidate generation receipts differ from the terminal receipt bindings",
        )
        self._outputs: dict[str, dict[str, dict[str, Any]]] = {}
        for reference in self.receipt["output_refs"]:
            data = _read_content_object(self.root, reference, kind="outputs", suffix=".json")
            value = _parse_json_object(data, "candidate output")
            output = _validate_output(
                publication,
                self.manifest,
                self._packets[reference["unit_id"]],
                value,
                reference,
            )
            by_slot = self._outputs.setdefault(reference["unit_id"], {})
            require(
                reference["agent_slot"] not in by_slot,
                "AI_CANDIDATE_INVALID",
                "candidate output slot is duplicated",
            )
            by_slot[reference["agent_slot"]] = output
        require(
            len(self._outputs) == 190
            and all(set(value) == set(AGENT_SLOTS) for value in self._outputs.values()),
            "AI_CANDIDATE_INVALID",
            "candidate workspace output matrix differs",
        )
        expected_set_sha = canonical_sha256(
            [
                {
                    "unit_id": item["unit_id"],
                    "agent_slot": item["agent_slot"],
                    "sha256": item["sha256"],
                }
                for item in self.receipt["output_refs"]
            ]
        )
        require(
            self.receipt["candidate_set_sha256"] == expected_set_sha,
            "AI_CANDIDATE_INVALID",
            "candidate set digest differs",
        )
        self._exposure_records()
        self._journal = self.root / "candidate-human-decisions.jsonl"
        self._journal_lock = self.root / "candidate-human-decisions.lock"
        self._candidate_index = {
            item["candidate_id"]: (unit_id, item)
            for unit_id, slots in self._outputs.items()
            for output in slots.values()
            for item in output["candidate_items"]
        }
        require(
            len(self._candidate_index)
            == sum(
                len(output["candidate_items"])
                for slots in self._outputs.values()
                for output in slots.values()
            ),
            "AI_CANDIDATE_INVALID",
            "candidate IDs collide across the campaign",
        )
        self.read_decisions()

    @property
    def campaign_id(self) -> str:
        return cast(str, self.manifest["campaign_id"])

    def outputs_for_unit(self, unit_id: str) -> list[dict[str, Any]]:
        require(
            unit_id in self._outputs,
            "AI_CANDIDATE_UNIT_UNKNOWN",
            "candidate unit is unknown",
        )
        return [json_copy(self._outputs[unit_id][slot]) for slot in AGENT_SLOTS]

    def candidate(self, candidate_id: str, *, unit_id: str) -> dict[str, Any]:
        indexed = self._candidate_index.get(candidate_id)
        require(
            indexed is not None and indexed[0] == unit_id,
            "AI_CANDIDATE_UNKNOWN",
            "candidate does not belong to this assignment",
        )
        assert indexed is not None
        return cast(dict[str, Any], json_copy(indexed[1]))

    def record_exposure(
        self,
        human_identity_commitment: str,
        stable_principal_commitment: str,
    ) -> dict[str, Any]:
        """Record AI-assisted visibility before returning any candidate bytes."""

        require(
            bool(_HEX_SHA256_RE.fullmatch(human_identity_commitment))
            and bool(_HEX_SHA256_RE.fullmatch(stable_principal_commitment)),
            "AI_CANDIDATE_EXPOSURE_INVALID",
            "candidate exposure identity commitments are invalid",
        )
        value: dict[str, Any] = {
            "schema_version": "mobileworld.g1.ai-candidate-human-exposure/v1",
            "record_type": "ai_candidate_human_exposure",
            "campaign_id": self.campaign_id,
            "campaign_receipt_sha256": self.receipt["receipt_sha256"],
            "human_identity_commitment": human_identity_commitment,
            "stable_principal_commitment": stable_principal_commitment,
            "stable_principal_commitment_scheme": STABLE_PRINCIPAL_COMMITMENT_SCHEME,
            "exposure_role": "AI_ASSISTED_SOLO_CURATOR",
            "candidate_outputs_visible": True,
            "authority": {
                "counts_as_independent_review": False,
                "formal_reviewer_eligible": False,
                "formal_adjudicator_eligible": False,
                "formal_resolution_eligible": False,
                "admission_eligible": False,
                "replay_eligible": False,
            },
        }
        value["exposure_sha256"] = _self_hash(value, "exposure_sha256")
        validate_ai_schema_record("ai_candidate_human_exposure.schema.json", value)
        with self._campaign_lock(exclusive=True):
            exposure_root = _ensure_child_directory(self.root, "human-exposures")
            path = exposure_root / f"{human_identity_commitment}.json"
            _write_once_regular(path, _object_bytes(value))
            stored = _read_regular(path, owner_restricted=True)
            assert stored is not None
            require(
                stored == _object_bytes(value),
                "AI_CANDIDATE_EXPOSURE_INVALID",
                "candidate exposure record differs",
            )
        return cast(dict[str, Any], json_copy(value))

    @contextmanager
    def _campaign_lock(self, *, exclusive: bool) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(self._journal_lock, flags, 0o600)
        except OSError as exc:
            raise CurationError(
                "AI_CANDIDATE_LOCK_INVALID", "candidate campaign lock cannot be opened"
            ) from exc
        try:
            opened = os.fstat(lock_fd)
            require(
                stat.S_ISREG(opened.st_mode)
                and opened.st_uid == os.geteuid()
                and opened.st_nlink == 1
                and opened.st_size == 0
                and opened.st_mode & 0o077 == 0,
                "AI_CANDIDATE_LOCK_INVALID",
                "candidate campaign lock ownership, links, mode, or bytes are unsafe",
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _exposure_records(self) -> list[dict[str, Any]]:
        exposure_root = self.root / "human-exposures"
        if not exposure_root.exists():
            return []
        require(
            not exposure_root.is_symlink() and exposure_root.is_dir(),
            "AI_CANDIDATE_EXPOSURE_INVALID",
            "candidate exposure root is unsafe",
        )
        records: list[dict[str, Any]] = []
        for path in sorted(exposure_root.iterdir(), key=lambda item: item.name):
            require(
                path.name.endswith(".json")
                and bool(_HEX_SHA256_RE.fullmatch(path.name.removesuffix(".json"))),
                "AI_CANDIDATE_EXPOSURE_INVALID",
                "candidate exposure filename is invalid",
            )
            data = _read_regular(path, owner_restricted=True)
            assert data is not None
            value = _parse_json_object(data, "candidate exposure")
            validate_ai_schema_record("ai_candidate_human_exposure.schema.json", value)
            require(
                data == _object_bytes(value)
                and value["campaign_id"] == self.campaign_id
                and value["campaign_receipt_sha256"] == self.receipt["receipt_sha256"]
                and value["human_identity_commitment"] == path.stem
                and value["stable_principal_commitment_scheme"]
                == STABLE_PRINCIPAL_COMMITMENT_SCHEME
                and value["exposure_sha256"] == _self_hash(value, "exposure_sha256"),
                "AI_CANDIDATE_EXPOSURE_INVALID",
                "candidate exposure record binding differs",
            )
            records.append(cast(dict[str, Any], json_copy(value)))
        return records

    def exposed_stable_principal_commitments(self) -> frozenset[str]:
        """Return only the non-secret commitments required by formal eligibility guards."""

        return frozenset(
            cast(str, record["stable_principal_commitment"]) for record in self._exposure_records()
        )

    def assert_formal_registry_eligible(self, registry: ReviewerRegistry) -> None:
        """Fail closed if a formal owner registry reuses an exposed principal and secret."""

        require(
            isinstance(registry, ReviewerRegistry),
            "FORMAL_REVIEWER_ELIGIBILITY_INVALID",
            "formal reviewer registry is invalid",
        )
        registry.assert_formal_ai_assistance_eligibility(
            self.exposed_stable_principal_commitments()
        )

    @contextmanager
    def formal_registry_guard(self, registry: ReviewerRegistry) -> Iterator[None]:
        """Hold the campaign read lock across one formal authoritative operation."""

        with self._campaign_lock(exclusive=False):
            self.assert_formal_registry_eligible(registry)
            yield

    def _decode_decisions(self, data: bytes) -> list[dict[str, Any]]:
        if not data:
            return []
        require(
            len(data) <= MAX_DECISION_JOURNAL_BYTES and data.endswith(b"\n") and b"\r" not in data,
            "AI_DECISION_JOURNAL_INVALID",
            "candidate decision journal framing is invalid",
        )
        events: list[dict[str, Any]] = []
        previous_sha: str | None = None
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for seq, line in enumerate(data.splitlines()):
            require(bool(line), "AI_DECISION_JOURNAL_INVALID", "decision journal has blank line")
            event = _parse_json_object(line, "candidate decision event")
            require(
                line == canonical_json_bytes(event),
                "AI_DECISION_JOURNAL_INVALID",
                "candidate decision event is not canonical",
            )
            validate_ai_schema_record("ai_candidate_human_decision_event.schema.json", event)
            require(
                event["event_seq"] == seq
                and event["previous_event_sha256"] == previous_sha
                and event["event_id"] == _decision_event_id(event)
                and event["event_sha256"] == _self_hash(event, "event_sha256"),
                "AI_DECISION_JOURNAL_INVALID",
                "candidate decision chain differs",
            )
            indexed = self._candidate_index.get(event["candidate_id"])
            require(
                event["campaign_id"] == self.campaign_id
                and indexed is not None
                and indexed[0] == event["unit_id"]
                and indexed[1]["candidate_sha256"] == event["candidate_sha256"],
                "AI_DECISION_JOURNAL_INVALID",
                "candidate decision source binding differs",
            )
            key = (event["human_identity_commitment"], event["candidate_id"])
            prior = latest.get(key)
            require(
                (
                    prior is None
                    and event["event_kind"] == "CANDIDATE_DECISION_RECORDED"
                    and event["supersedes_event_id"] is None
                )
                or (
                    prior is not None
                    and event["event_kind"] == "DECISION_SUPERSEDED"
                    and event["supersedes_event_id"] == prior["event_id"]
                ),
                "AI_DECISION_JOURNAL_INVALID",
                "candidate decision supersession differs",
            )
            latest[key] = event
            events.append(event)
            previous_sha = event["event_sha256"]
        return events

    def read_decisions(self) -> list[dict[str, Any]]:
        data = _read_regular(self._journal, missing_ok=True, owner_restricted=True) or b""
        return cast(list[dict[str, Any]], json_copy(self._decode_decisions(data)))

    def latest_decisions(self, human_identity_commitment: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in self.read_decisions():
            if event["human_identity_commitment"] == human_identity_commitment:
                latest[event["candidate_id"]] = event
        return latest

    def assert_unit_decisions_complete(self, unit_id: str, human_identity_commitment: str) -> None:
        """Require one explicit human decision for every atomic candidate in a unit."""

        require(
            bool(_HEX_SHA256_RE.fullmatch(human_identity_commitment)),
            "AI_DECISION_INVALID",
            "candidate human identity commitment is invalid",
        )
        candidate_ids = {
            cast(str, item["candidate_id"])
            for output in self.outputs_for_unit(unit_id)
            for item in output["candidate_items"]
        }
        decided_ids = set(self.latest_decisions(human_identity_commitment))
        require(
            candidate_ids <= decided_ids,
            "AI_CANDIDATE_DECISIONS_INCOMPLETE",
            "every AI candidate for this unit requires an explicit human decision before lock",
        )

    @contextmanager
    def simple_action_lock_payload(
        self,
        unit_id: str,
        human_identity_commitment: str,
    ) -> Iterator[dict[str, Any]]:
        """Derive one simple solo lock from the latest decisions under the journal lock.

        The shared campaign lock is intentionally held until the caller has durably appended the
        solo event.  A decision supersession takes the same lock exclusively, so another browser
        tab cannot change the retained set between this derivation and the authoritative append.
        """

        require(
            bool(_HEX_SHA256_RE.fullmatch(human_identity_commitment)),
            "AI_DECISION_INVALID",
            "candidate human identity commitment is invalid",
        )
        with self._campaign_lock(exclusive=False):
            candidates = [
                cast(dict[str, Any], item)
                for output in self.outputs_for_unit(unit_id)
                for item in output["candidate_items"]
            ]
            latest = self.latest_decisions(human_identity_commitment)
            require(
                all(item["candidate_id"] in latest for item in candidates),
                "AI_CANDIDATE_DECISIONS_INCOMPLETE",
                "every AI candidate for this unit requires an explicit human decision before lock",
            )
            decisions = [latest[cast(str, item["candidate_id"])]["decision"] for item in candidates]
            require(
                all(
                    decision in {"ADOPT_TO_FORM", "USE_AS_SUPPLEMENT", "IGNORE"}
                    for decision in decisions
                ),
                "AI_SIMPLE_LOCK_DECISION_INVALID",
                "simple lock requires a current best, correct, or wrong choice for every candidate",
            )
            retained = [
                item
                for item, decision in zip(candidates, decisions, strict=True)
                if decision in {"ADOPT_TO_FORM", "USE_AS_SUPPLEMENT"}
            ]
            require(
                bool(retained),
                "AI_SIMPLE_LOCK_NO_ACCEPTED_CANDIDATE",
                "simple lock requires at least one human-retained candidate",
            )
            payload = validate_action_payload(
                {
                    "proposal_kind": "ACTION_GOLD",
                    "disposition": "ACCEPT",
                    "exclusion_reason": None,
                    "predicates": [
                        {
                            **_semantic_predicate(item),
                            "evidence_ids": json_copy(item["evidence_ids"]),
                            "rationale": item["concise_rationale"],
                            "human_selected": True,
                        }
                        for item in retained
                    ],
                    "evidence_rationale": (
                        "我已逐条对照任务、target-pre 截图和可见 evidence，确认保留候选的动作、"
                        "字段、截图位置和理由，并确认它们构成当前截图下完整的合理一步动作集合。"
                    ),
                    "closed_world_confirmed": True,
                    "all_reasonable_actions_enumerated": True,
                }
            )
            self.publication.validate_review_payload_binding(unit_id, "ACTION_GOLD", payload)
            yield cast(dict[str, Any], json_copy(payload))

    def record_decision(
        self,
        *,
        unit_id: str,
        candidate_id: str,
        candidate_sha256: str,
        human_identity_commitment: str,
        decision: str,
        human_note: str,
    ) -> dict[str, Any]:
        require(decision in DECISIONS, "AI_DECISION_INVALID", "candidate decision is invalid")
        require(
            len(human_note.encode("utf-8")) <= 4000,
            "AI_DECISION_INVALID",
            "candidate human note is invalid",
        )
        require(
            len(human_identity_commitment) == 64,
            "AI_DECISION_INVALID",
            "candidate human identity commitment is invalid",
        )
        candidate = self.candidate(candidate_id, unit_id=unit_id)
        require(
            candidate["candidate_sha256"] == candidate_sha256,
            "AI_DECISION_INVALID",
            "candidate decision digest differs",
        )
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(self._journal_lock, flags, 0o600)
        except OSError as exc:
            raise CurationError(
                "AI_DECISION_JOURNAL_INVALID", "candidate decision lock cannot be opened"
            ) from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            lock_stat = os.fstat(lock_fd)
            require(
                stat.S_ISREG(lock_stat.st_mode)
                and lock_stat.st_uid == os.geteuid()
                and lock_stat.st_nlink == 1
                and lock_stat.st_mode & 0o077 == 0,
                "AI_DECISION_JOURNAL_INVALID",
                "candidate decision lock ownership, links, or mode are unsafe",
            )
            data = _read_regular(self._journal, missing_ok=True, owner_restricted=True) or b""
            events = self._decode_decisions(data)
            previous = [
                event
                for event in events
                if event["human_identity_commitment"] == human_identity_commitment
                and event["candidate_id"] == candidate_id
            ]
            prior = previous[-1] if previous else None
            if (
                prior is not None
                and prior["decision"] == decision
                and prior["human_note"] == human_note
            ):
                return cast(dict[str, Any], json_copy(prior))
            event: dict[str, Any] = {
                "schema_version": DECISION_SCHEMA_VERSION,
                "record_type": "ai_candidate_human_decision_event",
                "event_seq": len(events),
                "previous_event_sha256": events[-1]["event_sha256"] if events else None,
                "event_kind": "DECISION_SUPERSEDED"
                if prior is not None
                else "CANDIDATE_DECISION_RECORDED",
                "created_at_ns": time.time_ns(),
                "campaign_id": self.campaign_id,
                "unit_id": unit_id,
                "candidate_id": candidate_id,
                "candidate_sha256": candidate_sha256,
                "human_identity_commitment": human_identity_commitment,
                "decision": decision,
                "supersedes_event_id": prior["event_id"] if prior is not None else None,
                "human_note": human_note,
                "attestations": {
                    "human_confirmed_item_review": True,
                    "human_verified_visible_evidence": True,
                    "ai_candidate_is_not_evidence": True,
                    "annotation_form_not_saved_or_finalized": True,
                },
                "authority": {
                    "counts_as_independent_review": False,
                    "formal_journal_event_id": None,
                    "solo_journal_event_id": None,
                    "formal_resolution_eligible": False,
                    "admission_eligible": False,
                    "replay_eligible": False,
                },
            }
            event["event_id"] = _decision_event_id(event)
            event["event_sha256"] = _self_hash(event, "event_sha256")
            validate_ai_schema_record("ai_candidate_human_decision_event.schema.json", event)
            candidate_data = data + canonical_json_bytes(event) + b"\n"
            self._decode_decisions(candidate_data)
            append_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self._journal, append_flags, 0o600)
            except OSError as exc:
                raise CurationError(
                    "AI_DECISION_JOURNAL_INVALID", "candidate decision journal cannot be opened"
                ) from exc
            try:
                _write_all(fd, canonical_json_bytes(event) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            return cast(dict[str, Any], json_copy(event))
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def progress(self, human_identity_commitment: str) -> dict[str, Any]:
        latest = self.latest_decisions(human_identity_commitment)
        total = len(self._candidate_index)
        per_decision = {decision: 0 for decision in DECISIONS}
        for event in latest.values():
            per_decision[event["decision"]] += 1
        reviewed_units = 0
        for unit_id, slots in self._outputs.items():
            candidate_ids = [
                item["candidate_id"]
                for output in slots.values()
                for item in output["candidate_items"]
            ]
            if all(candidate_id in latest for candidate_id in candidate_ids):
                reviewed_units += 1
        return {
            "campaign_id": self.campaign_id,
            "unit_count": 190,
            "agent_output_count": 570,
            "candidate_item_count": total,
            "decided_candidate_item_count": len(latest),
            "pending_candidate_item_count": total - len(latest),
            "reviewed_unit_count": reviewed_units,
            "decisions": per_decision,
            "counts_as_independent_review": False,
            "formal_resolution_eligible": False,
            "human_review_required": True,
        }
