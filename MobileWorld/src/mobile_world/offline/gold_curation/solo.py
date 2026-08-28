"""Mechanically non-formal, single-curator first-pass workspace.

This module deliberately uses a separate registry, manifest, and journal from
the formal double-blind workspace.  Its records can preserve human research
work, but they can never satisfy an independent review, resolution,
adjudication, admission, replay, or formal-export gate.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from mobile_world.offline.gold_curation.contracts import (
    REVIEW_PROPOSAL_SCHEMA_VERSION,
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    json_copy,
    require,
    role_channel,
    validate_identity,
    validate_review_payload,
)
from mobile_world.offline.gold_curation.publication import (
    ACTIVE_G1_3_CAPSULE_SET_SHA256,
    ACTIVE_G1_3_MANIFEST_SHA256,
    ACTIVE_G1_3_PUBLICATION,
    CurationPublication,
)
from mobile_world.offline.gold_curation.schema_validation import validate_schema_record
from mobile_world.offline.gold_curation.store import (
    MAX_EVENT_BYTES,
    AnnotationStore,
    _formal_schema_hashes,
    _is_within,
    _read_regular,
    _write_all,
    _write_once_regular,
    stable_principal_commitment,
)

SOLO_REGISTRY_SCHEMA_VERSION: Final = "mobileworld.g1.solo-first-pass-curator-registry/v1"
SOLO_WORKSPACE_SCHEMA_VERSION: Final = "mobileworld.g1.solo-first-pass-workspace-manifest/v1"
SOLO_EVENT_SCHEMA_VERSION: Final = "mobileworld.g1.solo-first-pass-event/v1"
SOLO_REVIEW_TIER: Final = "NON_FORMAL_SOLO_FIRST_PASS"
SOLO_REVIEW_ROLES: Final = (
    "ACTION_GOLD_PRIMARY",
    "TRANSFORMATION_PRIMARY",
    "CONSISTENCY_AUDIT_PRIMARY",
)
SOLO_ROLE_BY_CHANNEL: Final = {
    "ACTION_GOLD": "ACTION_GOLD_PRIMARY",
    "TRANSFORMATION": "TRANSFORMATION_PRIMARY",
    "CONSISTENCY_AUDIT": "CONSISTENCY_AUDIT_PRIMARY",
}
SOLO_PHASES: Final = (*SOLO_ROLE_BY_CHANNEL, "COMPLETE")
SOLO_EVENT_KINDS: Final = ("SOLO_DRAFT_SAVED", "SOLO_FIRST_PASS_LOCKED")


@dataclass(frozen=True, slots=True)
class SoloCuratorRegistry:
    """One real principal authorized for non-formal first-pass surfaces."""

    canonical_bytes: bytes
    sha256: str
    principal_id: str
    _access_secret: str
    source_path: Path

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> SoloCuratorRegistry:
        supplied = Path(path)
        require(
            not supplied.is_symlink(),
            "SOLO_CURATOR_REGISTRY_INVALID",
            "solo curator registry cannot be a symlink",
        )
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as exc:
            raise CurationError(
                "SOLO_CURATOR_REGISTRY_INVALID", "solo curator registry does not exist"
            ) from exc
        repository_root = Path(__file__).resolve().parents[5]
        for forbidden in (repository_root, ACTIVE_G1_3_PUBLICATION):
            require(
                not _is_within(resolved, forbidden.resolve(strict=False)),
                "SOLO_CURATOR_REGISTRY_INVALID",
                "solo curator registry must be repository-external",
            )
        metadata = supplied.stat(follow_symlinks=False)
        require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and metadata.st_mode & 0o077 == 0,
            "SOLO_CURATOR_REGISTRY_INVALID",
            "solo curator registry must be owner-only, regular, and singly linked",
        )
        data = _read_regular(supplied, owner_restricted=True)
        assert data is not None
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CurationError(
                "SOLO_CURATOR_REGISTRY_INVALID", "solo curator registry is not valid JSON"
            ) from exc
        require(
            isinstance(value, dict)
            and set(value) == {"schema_version", "principal"}
            and value.get("schema_version") == SOLO_REGISTRY_SCHEMA_VERSION,
            "SOLO_CURATOR_REGISTRY_INVALID",
            "solo curator registry envelope is invalid",
        )
        principal = value.get("principal")
        require(
            isinstance(principal, dict) and set(principal) == {"principal_id", "access_secret"},
            "SOLO_CURATOR_REGISTRY_INVALID",
            "solo curator principal is not closed",
        )
        principal_id = principal.get("principal_id")
        access_secret = principal.get("access_secret")
        validate_identity(principal_id, SOLO_REVIEW_ROLES[0])
        require(
            isinstance(access_secret, str) and len(access_secret.encode("utf-8")) >= 16,
            "SOLO_CURATOR_REGISTRY_INVALID",
            "solo curator secret must contain at least 16 UTF-8 bytes",
        )
        semantic = {
            "schema_version": SOLO_REGISTRY_SCHEMA_VERSION,
            "principal": {
                "principal_id": principal_id,
                "access_secret_sha256": hashlib.sha256(access_secret.encode("utf-8")).hexdigest(),
                "allowed_roles": list(SOLO_REVIEW_ROLES),
                "counts_as_independent_review": False,
            },
        }
        canonical = canonical_json_bytes(semantic)
        return cls(
            canonical_bytes=canonical,
            sha256=hashlib.sha256(canonical).hexdigest(),
            principal_id=cast(str, principal_id),
            _access_secret=cast(str, access_secret),
            source_path=resolved,
        )

    def authenticate(self, principal_id: Any, role: Any, access_secret: Any) -> tuple[str, str]:
        principal_id, role = validate_identity(principal_id, role)
        require(
            role in SOLO_REVIEW_ROLES
            and isinstance(access_secret, str)
            and hmac.compare_digest(principal_id, self.principal_id)
            and hmac.compare_digest(access_secret, self._access_secret),
            "REVIEWER_AUTHENTICATION_FAILED",
            "solo curator principal, role, or access secret is invalid",
        )
        return principal_id, role

    def permits(self, principal_id: str, role: str) -> bool:
        return hmac.compare_digest(principal_id, self.principal_id) and role in SOLO_REVIEW_ROLES

    def stable_principal_commitment(self, principal_id: str) -> str:
        """Link the owner principal across workspaces without exposing its secret."""

        require(
            hmac.compare_digest(principal_id, self.principal_id),
            "REVIEWER_AUTHENTICATION_FAILED",
            "solo curator is not in the owner registry",
        )
        return stable_principal_commitment(principal_id, self._access_secret)


class SoloFirstPassStore(AnnotationStore):
    """Append-only precursor journal that is ineligible for formal promotion."""

    workspace_mode = "SOLO_FIRST_PASS"
    journal_filename = "solo-first-pass-events.jsonl"
    manifest_filename = "solo-first-pass-workspace-manifest.json"
    assignment_key_filename = "solo-assignment-key.bin"
    incompatible_workspace_filenames = (
        "workspace-manifest.json",
        "annotation-events.jsonl",
        "assignment-key.bin",
    )

    def __init__(
        self,
        root: str | os.PathLike[str],
        publication: CurationPublication,
        reviewer_registry: SoloCuratorRegistry,
        *,
        repository_root: str | os.PathLike[str] | None = None,
        codec_gate_receipt_path: str | os.PathLike[str] | None = None,
        g1_5_publication_manifest_path: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(
            root,
            publication,
            reviewer_registry,  # type: ignore[arg-type]
            repository_root=repository_root,
            codec_gate_receipt_path=codec_gate_receipt_path,
            g1_5_publication_manifest_path=g1_5_publication_manifest_path,
        )

    @property
    def solo_registry(self) -> SoloCuratorRegistry:
        return cast(SoloCuratorRegistry, self.reviewer_registry)

    @property
    def formal_annotation_open(self) -> bool:
        return False

    @property
    def first_pass_lock_open(self) -> bool:
        return self._verified_codec_gate_receipt_sha256() is not None

    @property
    def available_roles(self) -> tuple[str, ...]:
        return SOLO_REVIEW_ROLES

    def _bind_manifest(self) -> None:
        codec_gate_sha256 = self._verified_codec_gate_receipt_sha256()
        value = {
            "schema_version": SOLO_WORKSPACE_SCHEMA_VERSION,
            "record_type": "solo_first_pass_workspace_manifest",
            "workspace_id": self.workspace_id,
            "workspace_mode": self.workspace_mode,
            "contract_version": "mobileworld.g1.gold-history-intervention-solo-first-pass/amendment-v1",
            "issue": "ALE-324",
            "story": "G1.6",
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
            "target_unit_count": 190,
            "formal_schema_sha256s": _formal_schema_hashes(),
            "codec_gate_receipt_sha256": codec_gate_sha256,
            "identity_policy": {
                "identity_key_commitment_sha256": self._identity_key_commitment_sha256,
                "owner_registry_sha256": self.solo_registry.sha256,
                "one_real_principal": True,
                "aliases_as_independent_reviewers_allowed": False,
                "same_principal_cross_channel_first_pass": True,
            },
            "stage_policy": {
                "global_stage_order": [
                    "ACTION_GOLD",
                    "TRANSFORMATION",
                    "CONSISTENCY_AUDIT",
                ],
                "units_required_per_stage": 190,
                "stage_lock_immutable": True,
                "later_stage_opens_only_after_prior_stage_complete": True,
                "formal_workspace_must_be_separate": True,
                "precursor_visible_to_blind_formal_reviewers": False,
            },
            "authority": {
                "review_tier": SOLO_REVIEW_TIER,
                "counts_as_independent_review": False,
                "formal_resolution_eligible": False,
                "adjudication_eligible": False,
                "formal_export_eligible": False,
                "admission_eligible": False,
                "promotion_allowed": False,
                "replay_eligible": False,
                "cross_channel_exposed": True,
            },
            "readiness": {
                "workspace_initialized": True,
                "first_pass_lock_open": codec_gate_sha256 is not None,
                "formal_annotation_open": False,
                "curation_and_admission_sealed": False,
                "admission_ready": False,
                "execution_ready": False,
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
                "formal_replay_ready": False,
            },
            "safety": {
                "loopback_only": True,
                "external_network_used": False,
                "provider_client_created": False,
                "provider_invoked": False,
                "gpu_probed": False,
                "gpu_used": False,
                "model_loaded": False,
                "replay_executed": False,
                "mobileworld_gui_or_tool_action_executed": False,
                "treatment_response_count": 0,
            },
        }
        validate_schema_record("solo_annotation_workspace.schema.json", value)
        data = canonical_json_bytes(value) + b"\n"
        self._workspace_manifest_bytes = data
        _write_once_regular(self._manifest, data)

    def identity_commitment(self, reviewer_id: str) -> str:
        require(
            hmac.compare_digest(reviewer_id, self.solo_registry.principal_id),
            "REVIEWER_AUTHENTICATION_FAILED",
            "solo curator is not in the owner registry",
        )
        return hmac.new(
            self._assignment_key,
            (
                f"mobileworld.g1.solo-first-pass.curator/v1\0{self.workspace_id}\0{reviewer_id}"
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

    def assert_identity_role(self, reviewer_id: str, reviewer_role: str) -> str:
        validate_identity(reviewer_id, reviewer_role)
        require(
            self.solo_registry.permits(reviewer_id, reviewer_role),
            "REVIEWER_AUTHENTICATION_FAILED",
            "solo curator role differs from the owner registry",
        )
        return self.identity_commitment(reviewer_id)

    def authenticate_identity(
        self, reviewer_id: Any, reviewer_role: Any, access_secret: Any
    ) -> tuple[str, str, str]:
        principal, role = self.solo_registry.authenticate(reviewer_id, reviewer_role, access_secret)
        return principal, role, self.assert_identity_role(principal, role)

    @staticmethod
    def _decode_solo_events(data: bytes) -> list[dict[str, Any]]:
        if not data:
            return []
        require(
            data.endswith(b"\n") and b"\r" not in data,
            "SOLO_LEDGER_INVALID",
            "solo ledger must use exactly one LF after every canonical event",
        )
        events: list[dict[str, Any]] = []
        previous: str | None = None
        required = {
            "schema_version",
            "record_type",
            "event_id",
            "event_seq",
            "previous_event_sha256",
            "event_sha256",
            "event_kind",
            "created_at_ns",
            "unit_id",
            "channel",
            "assignment_id",
            "source_packet_sha256",
            "assignment_packet_sha256",
            "reviewer_identity_sha256",
            "reviewer_role",
            "proposal_schema_version",
            "codec_gate_receipt_sha256",
            "payload",
            "payload_sha256",
            "material_projection_sha256",
            "review_tier",
            "counts_as_independent_review",
            "formal_resolution_eligible",
            "admission_eligible",
            "promotion_allowed",
            "replay_eligible",
            "cross_channel_exposed",
        }
        for index, raw in enumerate(data[:-1].split(b"\n")):
            require(bool(raw), "SOLO_LEDGER_INVALID", "solo ledger contains a blank record")
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CurationError(
                    "SOLO_LEDGER_INVALID", "solo ledger contains invalid JSON"
                ) from exc
            require(
                isinstance(value, dict)
                and set(value) == required
                and raw == canonical_json_bytes(value),
                "SOLO_LEDGER_INVALID",
                "solo event is not a closed canonical JSON object",
            )
            event = cast(dict[str, Any], value)
            require(
                event["schema_version"] == SOLO_EVENT_SCHEMA_VERSION
                and event["record_type"] == "solo_first_pass_event"
                and event["event_kind"] in SOLO_EVENT_KINDS,
                "SOLO_LEDGER_INVALID",
                "solo event version or kind is invalid",
            )
            require(
                event["event_seq"] == index and event["previous_event_sha256"] == previous,
                "SOLO_LEDGER_INVALID",
                "solo event chain is discontinuous",
            )
            event_subject = {key: item for key, item in event.items() if key != "event_sha256"}
            require(
                event["event_sha256"] == canonical_sha256(event_subject),
                "SOLO_LEDGER_INVALID",
                "solo event digest differs",
            )
            id_subject = {
                key: item for key, item in event.items() if key not in {"event_id", "event_sha256"}
            }
            require(
                event["event_id"] == "g1soloannotation-" + canonical_sha256(id_subject)[:24]
                and event["payload_sha256"] == canonical_sha256(event["payload"]),
                "SOLO_LEDGER_INVALID",
                "solo event ID or payload digest differs",
            )
            validate_schema_record("solo_annotation_event.schema.json", event)
            validate_schema_record("review_proposal.schema.json", event["payload"])
            previous = event["event_sha256"]
            events.append(event)
        return events

    @staticmethod
    def _phase_from_locked(locked: Mapping[str, set[str]], target: int) -> str:
        if len(locked["ACTION_GOLD"]) < target:
            require(
                not locked["TRANSFORMATION"] and not locked["CONSISTENCY_AUDIT"],
                "SOLO_LEDGER_INVALID",
                "later solo stage precedes completion of Action Gold",
            )
            return "ACTION_GOLD"
        if len(locked["TRANSFORMATION"]) < target:
            require(
                not locked["CONSISTENCY_AUDIT"],
                "SOLO_LEDGER_INVALID",
                "Consistency precedes completion of Transformation",
            )
            return "TRANSFORMATION"
        if len(locked["CONSISTENCY_AUDIT"]) < target:
            return "CONSISTENCY_AUDIT"
        return "COMPLETE"

    def _phase_from_events(self, events: list[dict[str, Any]]) -> str:
        unit_ids = {item["unit_id"] for item in self.publication.list_units()}
        locked: dict[str, set[str]] = {channel: set() for channel in SOLO_ROLE_BY_CHANNEL}
        for event in events:
            require(
                event["unit_id"] in unit_ids,
                "SOLO_LEDGER_INVALID",
                "solo event unit is outside the active publication",
            )
            if event["event_kind"] == "SOLO_FIRST_PASS_LOCKED":
                channel_locks = locked[event["channel"]]
                require(
                    event["unit_id"] not in channel_locks,
                    "SOLO_LEDGER_INVALID",
                    "solo stage has duplicate immutable locks",
                )
                channel_locks.add(event["unit_id"])
        return self._phase_from_locked(locked, len(unit_ids))

    def _solo_primary_checkpoint_from_events(self, events: list[dict[str, Any]]) -> str:
        locks = [
            event
            for event in events
            if event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
            and event["channel"] in {"ACTION_GOLD", "TRANSFORMATION"}
        ]
        unit_count = len(self.publication.list_units())
        require(
            len(locks) == unit_count * 2
            and len({(event["unit_id"], event["channel"]) for event in locks}) == unit_count * 2,
            "SOLO_STAGE_BLOCKED",
            "solo consistency requires all Action and Transformation locks",
        )
        rows = [
            {
                "unit_id": event["unit_id"],
                "channel": event["channel"],
                "event_id": event["event_id"],
                "payload_sha256": event["payload_sha256"],
                "material_projection_sha256": event["material_projection_sha256"],
                "source_packet_sha256": event["source_packet_sha256"],
            }
            for event in sorted(locks, key=lambda item: (item["channel"], item["unit_id"]))
        ]
        return canonical_sha256(
            {
                "schema_version": "mobileworld.g1.solo-first-pass-primary-checkpoint/v1",
                "workspace_id": self.workspace_id,
                "counts_as_formal_resolution": False,
                "locks": rows,
            }
        )

    def _source_binding_for_events(
        self, unit_id: str, channel: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        checkpoint = (
            self._solo_primary_checkpoint_from_events(events)
            if channel == "CONSISTENCY_AUDIT"
            else None
        )
        return self.publication.source_packet_binding(
            unit_id,
            channel,
            curation_resolution_set_sha256=checkpoint,
        )

    def _validate_solo_event_semantics(
        self, events: list[dict[str, Any]], *, start_index: int = 0
    ) -> None:
        units = {item["unit_id"]: item for item in self.publication.list_units()}
        require(
            type(start_index) is int and 0 <= start_index <= len(events),
            "SOLO_LEDGER_INVALID",
            "solo semantic validation start is invalid",
        )
        gate_sha256 = self._verified_codec_gate_receipt_sha256()
        locked: dict[str, set[str]] = {channel: set() for channel in SOLO_ROLE_BY_CHANNEL}
        checkpoint_sha256: str | None = None
        for index, event in enumerate(events):
            unit = units.get(event["unit_id"])
            require(unit is not None, "SOLO_LEDGER_INVALID", "solo event unit is unknown")
            assert unit is not None
            channel = event["channel"]
            role = event["reviewer_role"]
            require(
                self._phase_from_locked(locked, len(units)) == channel
                and SOLO_ROLE_BY_CHANNEL.get(channel) == role,
                "SOLO_LEDGER_INVALID",
                "solo event violates the global stage order",
            )
            require(
                event["reviewer_identity_sha256"]
                == self.identity_commitment(self.solo_registry.principal_id),
                "SOLO_LEDGER_INVALID",
                "solo event identity is not the registered real principal",
            )
            validated = validate_review_payload(
                channel,
                event["payload"],
                clean_control=unit["unit_kind"] == "CLEAN_CONTROL",
            )
            require(
                validated == event["payload"],
                "SOLO_LEDGER_INVALID",
                "solo proposal differs from its canonical validated form",
            )
            self.publication.validate_review_payload_binding(event["unit_id"], channel, validated)
            require(
                event["unit_id"] not in locked[channel],
                "SOLO_LEDGER_INVALID",
                "solo journal contains an event after an immutable stage lock",
            )
            is_lock = event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
            expected_material = (
                canonical_sha256(self.material_projection_for(event["unit_id"], channel, validated))
                if is_lock
                else None
            )
            require(
                event["material_projection_sha256"] == expected_material
                and event["codec_gate_receipt_sha256"] == (gate_sha256 if is_lock else None),
                "SOLO_LEDGER_INVALID",
                "solo lock material or codec provenance differs",
            )
            expected_assignment = self.assignment_id(event["unit_id"], role)
            require(
                event["assignment_id"] == expected_assignment,
                "SOLO_LEDGER_INVALID",
                "solo assignment binding differs",
            )
            if channel == "CONSISTENCY_AUDIT":
                if checkpoint_sha256 is None:
                    checkpoint_sha256 = self._solo_primary_checkpoint_from_events(events[:index])
                source_binding = self.publication.source_packet_binding(
                    event["unit_id"],
                    channel,
                    curation_resolution_set_sha256=checkpoint_sha256,
                )
            else:
                source_binding = self.publication.source_packet_binding(
                    event["unit_id"], channel, curation_resolution_set_sha256=None
                )
            source_bytes = self._assert_source_packet_ref(event["source_packet_sha256"])
            require(
                event["source_packet_sha256"] == source_binding["source_packet_sha256"]
                and source_bytes == canonical_json_bytes(source_binding["source_packet"]),
                "SOLO_LEDGER_INVALID",
                "solo source packet differs from the active projection",
            )
            assignment_packet = self._assert_assignment_packet_ref(
                event["assignment_packet_sha256"]
            )
            from mobile_world.offline.gold_curation.server import _browser_packet

            source_packet = (
                self.publication.consistency_packet(event["unit_id"])
                if channel == "CONSISTENCY_AUDIT"
                else self.publication.packet(event["unit_id"], channel)
            )
            expected_packet = _browser_packet(
                source_packet,
                assignment_id=event["assignment_id"],
                role=role,
                reviewer_identity_sha256=event["reviewer_identity_sha256"],
                source_binding=source_binding,
                compared_review_event_ids=[],
            )
            require(
                assignment_packet == expected_packet,
                "SOLO_LEDGER_INVALID",
                "solo assignment packet differs from its rederived projection",
            )
            if is_lock:
                require(
                    event["unit_id"] not in locked[channel],
                    "SOLO_LEDGER_INVALID",
                    "solo stage has duplicate immutable locks",
                )
                locked[channel].add(event["unit_id"])

    def read_events(self) -> list[dict[str, Any]]:
        self._assert_workspace_manifest()
        data = _read_regular(self._journal, missing_ok=True, owner_restricted=True)
        events = [] if data is None else self._decode_solo_events(data)
        self._validate_solo_event_semantics(events)
        return events

    def current_phase(self) -> str:
        return self._phase_from_events(self.read_events())

    def assert_channel_open(self, channel: str) -> None:
        require(
            channel in SOLO_ROLE_BY_CHANNEL,
            "CHANNEL_INVALID",
            "solo first-pass channel is invalid",
        )
        require(
            self.current_phase() == channel,
            "SOLO_STAGE_BLOCKED",
            "complete and lock every item in the preceding solo stage first",
        )

    def has_locked_first_pass(
        self,
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
    ) -> bool:
        identity = self.assert_identity_role(reviewer_id, reviewer_role)
        channel = role_channel(reviewer_role)
        return any(
            event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
            and event["unit_id"] == unit_id
            and event["channel"] == channel
            and event["reviewer_identity_sha256"] == identity
            for event in self.read_events()
        )

    def consistency_ready(self, unit_id: str) -> bool:
        require(
            any(item["unit_id"] == unit_id for item in self.publication.list_units()),
            "UNIT_UNKNOWN",
            "unit is not in the publication",
        )
        return self.current_phase() in {"CONSISTENCY_AUDIT", "COMPLETE"}

    def curation_resolution_set_sha256(self, unit_id: str) -> str:
        require(
            any(item["unit_id"] == unit_id for item in self.publication.list_units()),
            "UNIT_UNKNOWN",
            "unit is not in the publication",
        )
        return self._solo_primary_checkpoint_from_events(self.read_events())

    def _locked_append_solo(
        self,
        *,
        event_kind: str,
        unit_id: str,
        channel: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        reviewer_identity_sha256: str,
        reviewer_role: str,
        payload: dict[str, Any],
        semantic_validator: Any,
    ) -> dict[str, Any]:
        self._assert_workspace_manifest()
        expected_source = self.bind_source_packet(unit_id, channel)
        require(
            source_packet_sha256 == expected_source["source_packet_sha256"],
            "PACKET_BINDING_INVALID",
            "solo event source packet differs",
        )
        require(
            assignment_id == self.assignment_id(unit_id, reviewer_role),
            "ASSIGNMENT_INVALID",
            "solo event assignment differs",
        )
        self._assert_source_packet_ref(source_packet_sha256)
        assignment_packet = self._assert_assignment_packet_ref(assignment_packet_sha256)
        require(
            assignment_packet["assignment_id"] == assignment_id
            and assignment_packet["source_packet_sha256"] == source_packet_sha256
            and assignment_packet["reviewer_identity_sha256"] == reviewer_identity_sha256
            and assignment_packet["review_role"] == reviewer_role
            and assignment_packet["channel"] == channel,
            "PACKET_BINDING_INVALID",
            "solo event differs from its assignment packet",
        )
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._journal, flags, 0o600)
        except OSError as exc:
            raise CurationError("ANNOTATION_STORE_INVALID", "solo ledger cannot be opened") from exc
        try:
            opened = os.fstat(fd)
            require(
                stat.S_ISREG(opened.st_mode)
                and opened.st_uid == os.geteuid()
                and opened.st_nlink == 1
                and opened.st_mode & 0o077 == 0,
                "ANNOTATION_STORE_INVALID",
                "solo journal ownership, link count, or mode is unsafe",
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            events = self._decode_solo_events(b"".join(chunks))
            self._validate_solo_event_semantics(events)
            reused = semantic_validator(events)
            if isinstance(reused, dict):
                return cast(dict[str, Any], json_copy(reused))
            is_lock = event_kind == "SOLO_FIRST_PASS_LOCKED"
            gate_sha256 = self._require_codec_gate() if is_lock else None
            subject = {
                "schema_version": SOLO_EVENT_SCHEMA_VERSION,
                "record_type": "solo_first_pass_event",
                "event_seq": len(events),
                "previous_event_sha256": events[-1]["event_sha256"] if events else None,
                "event_kind": event_kind,
                "created_at_ns": time.time_ns(),
                "unit_id": unit_id,
                "channel": channel,
                "assignment_id": assignment_id,
                "source_packet_sha256": source_packet_sha256,
                "assignment_packet_sha256": assignment_packet_sha256,
                "reviewer_identity_sha256": reviewer_identity_sha256,
                "reviewer_role": reviewer_role,
                "proposal_schema_version": REVIEW_PROPOSAL_SCHEMA_VERSION,
                "codec_gate_receipt_sha256": gate_sha256,
                "payload": json_copy(payload),
                "payload_sha256": canonical_sha256(payload),
                "material_projection_sha256": canonical_sha256(
                    self.material_projection_for(unit_id, channel, payload)
                )
                if is_lock
                else None,
                "review_tier": SOLO_REVIEW_TIER,
                "counts_as_independent_review": False,
                "formal_resolution_eligible": False,
                "admission_eligible": False,
                "promotion_allowed": False,
                "replay_eligible": False,
                "cross_channel_exposed": True,
            }
            event = dict(subject)
            event["event_id"] = "g1soloannotation-" + canonical_sha256(subject)[:24]
            event["event_sha256"] = canonical_sha256(event)
            validate_schema_record("solo_annotation_event.schema.json", event)
            validate_schema_record("review_proposal.schema.json", event["payload"])
            self._validate_solo_event_semantics([*events, event], start_index=len(events))
            encoded = canonical_json_bytes(event)
            require(
                len(encoded) <= MAX_EVENT_BYTES,
                "ANNOTATION_EVENT_TOO_LARGE",
                "solo event is too large",
            )
            _write_all(fd, encoded + b"\n")
            os.fsync(fd)
            return cast(dict[str, Any], json_copy(event))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def save_draft(
        self,
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        channel = role_channel(reviewer_role)
        self.assert_channel_open(channel)
        unit = next(
            (item for item in self.publication.list_units() if item["unit_id"] == unit_id), None
        )
        require(unit is not None, "UNIT_UNKNOWN", "unit is not in the publication")
        assert unit is not None
        validated = validate_review_payload(
            channel, payload, clean_control=unit["unit_kind"] == "CLEAN_CONTROL"
        )
        self.publication.validate_review_payload_binding(unit_id, channel, validated)

        def guard(events: list[dict[str, Any]]) -> None:
            require(
                self._phase_from_events(events) == channel,
                "SOLO_STAGE_BLOCKED",
                "solo stage changed before the draft could be appended",
            )
            require(
                not any(
                    event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
                    and event["unit_id"] == unit_id
                    and event["channel"] == channel
                    for event in events
                ),
                "SOLO_FIRST_PASS_ALREADY_LOCKED",
                "solo first-pass record is immutable after locking",
            )

        return self._locked_append_solo(
            event_kind="SOLO_DRAFT_SAVED",
            unit_id=unit_id,
            channel=channel,
            assignment_id=assignment_id,
            source_packet_sha256=source_packet_sha256,
            assignment_packet_sha256=assignment_packet_sha256,
            reviewer_identity_sha256=reviewer_identity_sha256,
            reviewer_role=reviewer_role,
            payload=validated,
            semantic_validator=guard,
        )

    def submit_review(
        self,
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        channel = role_channel(reviewer_role)
        self._require_codec_gate()
        unit = next(
            (item for item in self.publication.list_units() if item["unit_id"] == unit_id), None
        )
        require(unit is not None, "UNIT_UNKNOWN", "unit is not in the publication")
        assert unit is not None
        validated = validate_review_payload(
            channel, payload, clean_control=unit["unit_kind"] == "CLEAN_CONTROL"
        )
        self.publication.validate_review_payload_binding(unit_id, channel, validated)

        def guard(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            existing = [
                event
                for event in events
                if event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
                and event["unit_id"] == unit_id
                and event["channel"] == channel
            ]
            if existing:
                require(
                    len(existing) == 1
                    and existing[0]["payload"] == validated
                    and existing[0]["assignment_id"] == assignment_id
                    and existing[0]["source_packet_sha256"] == source_packet_sha256
                    and existing[0]["assignment_packet_sha256"] == assignment_packet_sha256,
                    "SOLO_FIRST_PASS_ALREADY_LOCKED",
                    "solo first-pass stage already locked different bytes",
                )
                return existing[0]
            require(
                self._phase_from_events(events) == channel,
                "SOLO_STAGE_BLOCKED",
                "solo stage changed before the lock could be appended",
            )
            return None

        return self._locked_append_solo(
            event_kind="SOLO_FIRST_PASS_LOCKED",
            unit_id=unit_id,
            channel=channel,
            assignment_id=assignment_id,
            source_packet_sha256=source_packet_sha256,
            assignment_packet_sha256=assignment_packet_sha256,
            reviewer_identity_sha256=reviewer_identity_sha256,
            reviewer_role=reviewer_role,
            payload=validated,
            semantic_validator=guard,
        )

    def latest_draft(
        self, unit_id: str, reviewer_id: str, reviewer_role: str
    ) -> dict[str, Any] | None:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        drafts = [
            event
            for event in self.read_events()
            if event["event_kind"] == "SOLO_DRAFT_SAVED"
            and event["unit_id"] == unit_id
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            and event["reviewer_role"] == reviewer_role
        ]
        return None if not drafts else cast(dict[str, Any], json_copy(drafts[-1]["payload"]))

    def _status_for_events(
        self,
        events: list[dict[str, Any]],
        unit_id: str,
        role: str,
        reviewer_id: str,
    ) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, role)
        channel = role_channel(role)
        stage = self._phase_from_events(events)
        locked = any(
            event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
            and event["unit_id"] == unit_id
            and event["channel"] == channel
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            for event in events
        )
        drafted = any(
            event["event_kind"] == "SOLO_DRAFT_SAVED"
            and event["unit_id"] == unit_id
            and event["channel"] == channel
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            for event in events
        )
        own_state = "FIRST_PASS_LOCKED" if locked else "DRAFTING" if drafted else "NOT_ASSIGNED"
        if locked:
            state = workflow_state = "FIRST_PASS_LOCKED"
            can_open = False
        elif stage == channel:
            state = workflow_state = own_state
            can_open = True
        else:
            state = workflow_state = (
                "FIRST_PASS_COMPLETE" if stage == "COMPLETE" else "WAITING_FOR_PREVIOUS_STAGE"
            )
            can_open = False
        return {
            "state": state,
            "own_state": own_state,
            "workflow_state": workflow_state,
            "can_open": can_open,
            "solo_phase": stage,
        }

    def status_for(self, unit_id: str, role: str, reviewer_id: str) -> dict[str, Any]:
        return self._status_for_events(self.read_events(), unit_id, role, reviewer_id)

    def statuses_for_role(self, role: str, reviewer_id: str) -> dict[str, dict[str, Any]]:
        events = self.read_events()
        return {
            item["unit_id"]: self._status_for_events(events, item["unit_id"], role, reviewer_id)
            for item in self.publication.list_units()
        }

    def channel_resolution(self, unit_id: str, channel: str) -> None:
        require(channel in SOLO_ROLE_BY_CHANNEL, "CHANNEL_INVALID", "channel is invalid")
        require(
            any(item["unit_id"] == unit_id for item in self.publication.list_units()),
            "UNIT_UNKNOWN",
            "unit is not in the publication",
        )
        return None

    def export_workspace_receipt(self) -> dict[str, Any]:
        raise CurationError(
            "SOLO_FIRST_PASS_FORMAL_EXPORT_BLOCKED",
            "solo first-pass records cannot be exported as formal G1.6 review evidence",
        )

    def precursor_receipt(self) -> dict[str, Any]:
        events = self.read_events()
        lock_counts = {
            channel: sum(
                event["event_kind"] == "SOLO_FIRST_PASS_LOCKED" and event["channel"] == channel
                for event in events
            )
            for channel in SOLO_ROLE_BY_CHANNEL
        }
        body = {
            "schema_version": "mobileworld.g1.solo-first-pass-precursor-receipt/v1",
            "record_type": "solo_first_pass_precursor_receipt",
            "workspace_id": self.workspace_id,
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "event_count": len(events),
            "last_event_sha256": events[-1]["event_sha256"] if events else None,
            "lock_counts": lock_counts,
            "current_phase": self._phase_from_events(events),
            "review_tier": SOLO_REVIEW_TIER,
            "counts_as_independent_review": False,
            "formal_resolution_eligible": False,
            "adjudication_eligible": False,
            "formal_export_eligible": False,
            "admission_eligible": False,
            "promotion_allowed": False,
            "replay_eligible": False,
            "cross_channel_exposed": True,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}
