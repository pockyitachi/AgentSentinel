from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

import mobile_world.offline.gold_curation.ai_only_labels as ai_only_labels
from mobile_world.offline.gold_curation.ai_only_labels import (
    AI_ONLY_SCHEMA_FILENAMES,
    AI_ONLY_SCHEMA_ROOT,
    AIOnlyActionLabelPublication,
    _filesystem_census,
    build_ai_only_action_label_publication,
    validate_ai_only_schema_record,
)
from mobile_world.offline.gold_curation.contracts import (
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    material_projection,
)
from mobile_world.offline.gold_curation.solo import SOLO_EVENT_SCHEMA_VERSION, SOLO_REVIEW_TIER

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _unit(index: int) -> str:
    return f"g1case-{index:024x}"


def _candidate(index: int) -> str:
    return f"g1aicandidate-{index:024x}"


class _FakeCandidateWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(mode=0o700)
        self.campaign_id = "g1aicampaign-" + "1" * 24
        self.publication = _FakePublication(root.parent / "g1-3-publication")
        self.manifest: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": _hex("campaign-manifest-self"),
            "packet_refs": [
                {"unit_id": _unit(index), "sha256": _hex(f"packet-{index}")} for index in range(190)
            ],
        }
        self.receipt: dict[str, Any] = {
            "receipt_sha256": _hex("campaign-receipt-self"),
            "candidate_set_sha256": _hex("candidate-set"),
            "output_refs": [
                {
                    "unit_id": _unit(index),
                    "agent_slot": slot,
                    "path": f"outputs/{index}-{slot}.json",
                    "sha256": _hex(f"output-{index}-{slot}"),
                    "byte_count": 1,
                }
                for index in range(190)
                for slot in ("A", "B", "C")
            ],
        }
        self._outputs: dict[str, list[dict[str, Any]]] = {}
        for index in range(190):
            unit_id = _unit(index)
            source_sha = canonical_sha256(self.publication.packet(unit_id, "ACTION_GOLD"))
            self._outputs[unit_id] = [
                {
                    "agent_slot": "A",
                    "source_packet_sha256": source_sha,
                    "candidate_items": [
                        {
                            "candidate_id": _candidate(index),
                            "candidate_sha256": _hex(f"candidate-{index}"),
                            "predicate": {
                                "predicate_kind": "TEXT_VARIANTS",
                                "action_type": "open_app",
                                "field": "app_name",
                                "allowed_values": [f"Fixture {index}"],
                                "case_sensitive": False,
                                "unicode_normalization": "NFC",
                            },
                        }
                    ],
                },
                {
                    "agent_slot": "B",
                    "source_packet_sha256": source_sha,
                    "candidate_items": [],
                },
                {
                    "agent_slot": "C",
                    "source_packet_sha256": source_sha,
                    "candidate_items": [],
                },
            ]
        for name, value in (
            ("campaign-manifest.json", self.manifest),
            ("campaign-receipt.json", self.receipt),
        ):
            path = root / name
            path.write_bytes(canonical_json_bytes(value) + b"\n")
            path.chmod(0o600)

    def outputs_for_unit(self, unit_id: str) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self._outputs[unit_id]))


class _FakePublication:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(mode=0o700)

    def validate_review_payload_binding(
        self, unit_id: str, channel: str, payload: dict[str, Any]
    ) -> None:
        assert unit_id.startswith("g1case-")
        assert channel == "ACTION_GOLD"
        assert payload["proposal_kind"] == "ACTION_GOLD"

    def packet(self, unit_id: str, channel: str) -> dict[str, Any]:
        assert channel == "ACTION_GOLD"
        return {
            "schema_version": "fixture/blind-action-packet/v1",
            "unit_id": unit_id,
            "channel": channel,
        }

    def source_packet_binding(self, unit_id: str, channel: str) -> dict[str, Any]:
        assert channel == "ACTION_GOLD"
        index = int(unit_id.rsplit("-", 1)[1], 16)
        return {"source_packet_sha256": _hex(f"human-source-{index}")}


def _excluded_payload() -> dict[str, Any]:
    return {
        "proposal_kind": "ACTION_GOLD",
        "disposition": "EXCLUDE",
        "exclusion_reason": "NO_GOLD_CONSENSUS",
        "predicates": [],
        "evidence_rationale": "Fixture has no reliable candidate.",
        "closed_world_confirmed": False,
        "all_reasonable_actions_enumerated": False,
    }


def _write_human_journal(path: Path) -> Path:
    path.parent.mkdir(mode=0o700)
    previous: str | None = None
    rows: list[dict[str, Any]] = []
    for index in range(4):
        payload = _excluded_payload()
        subject: dict[str, Any] = {
            "schema_version": SOLO_EVENT_SCHEMA_VERSION,
            "record_type": "solo_first_pass_event",
            "event_seq": index,
            "previous_event_sha256": previous,
            "event_kind": "SOLO_FIRST_PASS_LOCKED",
            "created_at_ns": index,
            "unit_id": _unit(index),
            "channel": "ACTION_GOLD",
            "assignment_id": f"g1assignment-{index:032x}",
            "source_packet_sha256": _hex(f"human-source-{index}"),
            "assignment_packet_sha256": _hex(f"assignment-packet-{index}"),
            "reviewer_identity_sha256": _hex("fixture-reviewer"),
            "reviewer_role": "ACTION_GOLD_PRIMARY",
            "proposal_schema_version": "mobileworld.g1.gold-curation-review-proposal/v1",
            "codec_gate_receipt_sha256": _hex("codec-gate"),
            "payload": payload,
            "payload_sha256": canonical_sha256(payload),
            "material_projection_sha256": canonical_sha256(
                material_projection("ACTION_GOLD", payload)
            ),
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
        previous = event["event_sha256"]
        rows.append(event)
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    path.chmod(0o600)
    return path


def _write_batches(root: Path) -> dict[str, Path]:
    root.mkdir(mode=0o700)
    remaining = [_unit(index) for index in range(4, 190)]
    result: dict[str, Path] = {}
    for batch_index, slot in enumerate(("BATCH_1", "BATCH_2", "BATCH_3")):
        rows: list[dict[str, Any]] = []
        for unit_id in remaining[batch_index * 62 : (batch_index + 1) * 62]:
            index = int(unit_id.rsplit("-", 1)[1], 16)
            rows.append(
                {
                    "unit_id": unit_id,
                    "label_kind": "ACCEPT_CANDIDATES",
                    "retained_candidate_ids": [_candidate(index)],
                    "candidate_decisions": [
                        {
                            "candidate_id": _candidate(index),
                            "decision": "RETAIN",
                            "reason": "SUPPORTED",
                        }
                    ],
                    "exclusion_reason": None,
                    "concise_rationale": "The frozen fixture candidate is supported.",
                    "uncertainty_note": None,
                }
            )
        path = root / f"{slot.lower()}.jsonl"
        path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
        path.chmod(0o600)
        result[slot] = path
    return result


def _make_publication_writable(root: Path) -> None:
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _remove_empty_directories(root: Path) -> None:
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if not any(path.iterdir()):
            path.rmdir()


def _write_json_object(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = canonical_json_bytes(value) + b"\n"
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def _rewrite_publication_metadata(
    root: Path,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    refs = manifest["label_refs"]
    index_data = b"".join(canonical_json_bytes(item) + b"\n" for item in refs)
    manifest["label_index"] = {
        "path": "label-index.jsonl",
        "sha256": hashlib.sha256(index_data).hexdigest(),
        "byte_count": len(index_data),
    }
    manifest["label_set_sha256"] = canonical_sha256(
        [
            {
                "unit_id": item["unit_id"],
                "sha256": item["sha256"],
                "label_sha256": item["label_sha256"],
            }
            for item in refs
        ]
    )
    manifest["publication_manifest_sha256"] = ai_only_labels._self_hash(
        manifest, "publication_manifest_sha256"
    )
    manifest_data = _write_json_object(root / "publication-manifest.json", manifest)
    (root / "label-index.jsonl").chmod(0o600)
    (root / "label-index.jsonl").write_bytes(index_data)
    receipt["publication_id"] = manifest["publication_id"]
    receipt["publication_manifest_file_sha256"] = hashlib.sha256(manifest_data).hexdigest()
    receipt["publication_manifest_sha256"] = manifest["publication_manifest_sha256"]
    receipt["label_index"] = manifest["label_index"]
    receipt["label_set_sha256"] = manifest["label_set_sha256"]
    receipt["counts"] = manifest["counts"]
    receipt["receipt_sha256"] = ai_only_labels._self_hash(receipt, "receipt_sha256")
    _write_json_object(root / "publication-receipt.json", receipt)
    _remove_empty_directories(root)
    expected_files = {
        "publication-manifest.json",
        "publication-receipt.json",
        "label-index.jsonl",
        *(item["path"] for item in refs),
    }
    ai_only_labels._seal_publication_tree(root, expected_files)


def _replace_label(
    root: Path,
    manifest: dict[str, Any],
    index: int,
    label: dict[str, Any],
) -> None:
    old_reference = manifest["label_refs"][index]
    old_path = root / old_reference["path"]
    old_path.unlink()
    label["label_sha256"] = ai_only_labels._self_hash(label, "label_sha256")
    data = canonical_json_bytes(label) + b"\n"
    reference = ai_only_labels._content_reference("labels", ".json", data)
    reference.update(
        {
            "unit_id": label["unit_id"],
            "label_kind": label["label_kind"],
            "label_sha256": label["label_sha256"],
        }
    )
    _write_json_object(root / reference["path"], label)
    manifest["label_refs"][index] = reference


@pytest.fixture
def source_fixture(tmp_path: Path) -> tuple[_FakeCandidateWorkspace, Path, dict[str, Path]]:
    candidate = _FakeCandidateWorkspace(tmp_path / "candidate")
    journal = _write_human_journal(tmp_path / "human-workspace" / "solo-events.jsonl")
    batches = _write_batches(tmp_path / "batch-drafts")
    return candidate, journal, batches


def test_ai_only_schemas_are_closed_local_and_meta_valid() -> None:
    assert len(AI_ONLY_SCHEMA_FILENAMES) == 4
    for filename in AI_ONLY_SCHEMA_FILENAMES:
        value = json.loads((AI_ONLY_SCHEMA_ROOT / filename).read_bytes())
        Draft202012Validator.check_schema(value)
        assert value["additionalProperties"] is False
        assert value["$id"].startswith("https://agentsentinel.local/schemas/g1_6_ai_only/")
        assert "https://" not in json.dumps(value.get("$defs", {})).replace(
            "https://json-schema.org", ""
        )


def test_draft_schema_and_runtime_reject_false_authority_and_bad_decisions() -> None:
    valid = {
        "unit_id": _unit(4),
        "label_kind": "ACCEPT_CANDIDATES",
        "retained_candidate_ids": [_candidate(4)],
        "candidate_decisions": [
            {"candidate_id": _candidate(4), "decision": "RETAIN", "reason": "SUPPORTED"}
        ],
        "exclusion_reason": None,
        "concise_rationale": "Visible fixture evidence supports this candidate.",
        "uncertainty_note": None,
    }
    validate_ai_only_schema_record("ai_only_action_label_draft.schema.json", valid)
    invalid = dict(valid, retained_candidate_ids=[])
    with pytest.raises(CurationError) as exc_info:
        validate_ai_only_schema_record("ai_only_action_label_draft.schema.json", invalid)
    assert exc_info.value.code == "AI_ONLY_SCHEMA_MISMATCH"
    invalid = dict(valid, human_selected=True)
    with pytest.raises(CurationError) as exc_info:
        validate_ai_only_schema_record("ai_only_action_label_draft.schema.json", invalid)
    assert exc_info.value.code == "AI_ONLY_SCHEMA_MISMATCH"


def test_build_reopen_and_double_build_are_byte_identical(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    output_one = tmp_path / "output-one"
    output_two = tmp_path / "output-two"
    first = build_ai_only_action_label_publication(
        output_one,
        candidate,  # type: ignore[arg-type]
        journal,
        batches,
        repository_root=REPOSITORY_ROOT,
    )
    second = build_ai_only_action_label_publication(
        output_two,
        candidate,  # type: ignore[arg-type]
        journal,
        batches,
        repository_root=REPOSITORY_ROOT,
    )
    assert first == second
    first_files = {
        path.relative_to(output_one).as_posix(): path.read_bytes()
        for path in output_one.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(output_two).as_posix(): path.read_bytes()
        for path in output_two.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    sealed = AIOnlyActionLabelPublication(
        output_one,
        candidate,  # type: ignore[arg-type]
        journal,
        repository_root=REPOSITORY_ROOT,
    )
    assert sealed.manifest["counts"] == {
        "campaign_units": 190,
        "human_locked_units": 4,
        "ai_only_labeled_units": 186,
        "accepted_candidate_units": 186,
        "excluded_units": 0,
        "retained_candidates": 186,
        "rejected_candidates": 0,
        "decided_candidates": 186,
    }
    assert len(sealed.labels) == 186
    assert all(label["authority"]["human_selected"] is False for label in sealed.labels)
    assert all(label["disclosure"]["human_review_performed"] is False for label in sealed.labels)


def test_population_candidate_tamper_and_duplicate_fail_before_publication(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    row = json.loads(batches["BATCH_1"].read_bytes().splitlines()[0])
    row["candidate_decisions"][0]["candidate_id"] = _candidate(999)
    rows = batches["BATCH_1"].read_bytes().splitlines()
    rows[0] = canonical_json_bytes(row)
    batches["BATCH_1"].write_bytes(b"\n".join(rows) + b"\n")
    batches["BATCH_1"].chmod(0o600)
    output = tmp_path / "never-created"
    with pytest.raises(CurationError) as exc_info:
        build_ai_only_action_label_publication(
            output,
            candidate,  # type: ignore[arg-type]
            journal,
            batches,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_DECISION_INVALID"
    assert not output.exists()


def test_reopen_rejects_content_tamper_extra_symlink_and_hardlink(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    output = tmp_path / "output"
    build_ai_only_action_label_publication(
        output,
        candidate,  # type: ignore[arg-type]
        journal,
        batches,
        repository_root=REPOSITORY_ROOT,
    )
    tampered = tmp_path / "tampered"
    shutil.copytree(output, tampered)
    label = next((tampered / "labels").rglob("*.json"))
    label.chmod(0o600)
    label.write_bytes(label.read_bytes().replace(b"SUPPORTED", b"WRONG_ACTION", 1))
    with pytest.raises(CurationError):
        AIOnlyActionLabelPublication(
            tampered,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    extra = output / "extra.json"
    output.chmod(0o700)
    extra.write_bytes(b"{}\n")
    extra.chmod(0o400)
    output.chmod(0o500)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            output,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"
    output.chmod(0o700)
    extra.chmod(0o600)
    extra.unlink()
    symlink = output / "bad-link"
    symlink.symlink_to(output / "publication-manifest.json")
    output.chmod(0o500)
    with pytest.raises(CurationError) as exc_info:
        _filesystem_census(output)
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"
    output.chmod(0o700)
    symlink.unlink()
    hardlink = output / "bad-hardlink"
    os.link(output / "publication-manifest.json", hardlink)
    output.chmod(0o500)
    with pytest.raises(CurationError) as exc_info:
        _filesystem_census(output)
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"
    output.chmod(0o700)
    hardlink.unlink()


def test_reopen_rejects_root_journal_symlinks_changed_sources_and_unsafe_modes(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    output = tmp_path / "output"
    build_ai_only_action_label_publication(
        output,
        candidate,  # type: ignore[arg-type]
        journal,
        batches,
        repository_root=REPOSITORY_ROOT,
    )
    root_link = tmp_path / "publication-link"
    root_link.symlink_to(output, target_is_directory=True)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            root_link,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_ROOT_INVALID"
    journal_link = tmp_path / "journal-link.jsonl"
    journal_link.symlink_to(journal)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            output,
            candidate,  # type: ignore[arg-type]
            journal_link,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_HUMAN_PREFIX_INVALID"

    original_journal = journal.read_bytes()
    journal.chmod(0o600)
    journal.write_bytes(b"X" + original_journal[1:])
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            output,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_HUMAN_PREFIX_INVALID"
    journal.write_bytes(original_journal)

    candidate._outputs[_unit(4)][0]["source_packet_sha256"] = _hex("changed-source")
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            output,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_SOURCE_INVALID"
    candidate._outputs[_unit(4)][0]["source_packet_sha256"] = canonical_sha256(
        candidate.publication.packet(_unit(4), "ACTION_GOLD")
    )

    unsafe_file = tmp_path / "unsafe-file"
    shutil.copytree(output, unsafe_file)
    (unsafe_file / "publication-manifest.json").chmod(0o600)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            unsafe_file,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"
    unsafe_root = tmp_path / "unsafe-root"
    shutil.copytree(output, unsafe_root)
    unsafe_root.chmod(0o700)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            unsafe_root,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"
    unsafe_directory = tmp_path / "unsafe-directory"
    shutil.copytree(output, unsafe_directory)
    (unsafe_directory / "labels").chmod(0o700)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            unsafe_directory,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_CENSUS_INVALID"


def test_build_rejects_active_source_overlap_duplicate_drafts_and_atomic_collision(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, journal, batches = source_fixture
    with pytest.raises(CurationError) as exc_info:
        build_ai_only_action_label_publication(
            candidate.publication.root / "forbidden-output",
            candidate,  # type: ignore[arg-type]
            journal,
            batches,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_ROOT_INVALID"
    duplicate_drafts = dict(batches)
    duplicate_drafts["BATCH_2"] = duplicate_drafts["BATCH_1"]
    with pytest.raises(CurationError) as exc_info:
        build_ai_only_action_label_publication(
            tmp_path / "duplicate-drafts-output",
            candidate,  # type: ignore[arg-type]
            journal,
            duplicate_drafts,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_BATCH_INVALID"

    original_rename = ai_only_labels._rename_directory_noreplace

    def collide(*, parent_fd: int, source_name: str, destination_name: str) -> None:
        os.mkdir(destination_name, mode=0o700, dir_fd=parent_fd)
        original_rename(
            parent_fd=parent_fd,
            source_name=source_name,
            destination_name=destination_name,
        )

    monkeypatch.setattr(ai_only_labels, "_rename_directory_noreplace", collide)
    collision_output = tmp_path / "collision-output"
    with pytest.raises(CurationError) as exc_info:
        build_ai_only_action_label_publication(
            collision_output,
            candidate,  # type: ignore[arg-type]
            journal,
            batches,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_ROOT_INVALID"
    assert collision_output.is_dir() and not any(collision_output.iterdir())
    failed_stages = list(tmp_path.glob(".collision-output.staging-*"))
    assert len(failed_stages) == 1
    assert len(_filesystem_census(failed_stages[0])) == 189


def test_reopen_rejects_coherent_population_decision_and_content_path_tamper(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    original = tmp_path / "original"
    build_ai_only_action_label_publication(
        original,
        candidate,  # type: ignore[arg-type]
        journal,
        batches,
        repository_root=REPOSITORY_ROOT,
    )

    duplicate_unit = tmp_path / "duplicate-unit"
    shutil.copytree(original, duplicate_unit)
    _make_publication_writable(duplicate_unit)
    manifest = json.loads((duplicate_unit / "publication-manifest.json").read_bytes())
    receipt = json.loads((duplicate_unit / "publication-receipt.json").read_bytes())
    source_label = json.loads((duplicate_unit / manifest["label_refs"][-2]["path"]).read_bytes())
    source_label["concise_rationale"] += " Coordinated duplicate fixture."
    _replace_label(duplicate_unit, manifest, -1, source_label)
    _rewrite_publication_metadata(duplicate_unit, manifest, receipt)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            duplicate_unit,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_POPULATION_INVALID"

    duplicate_decision = tmp_path / "duplicate-decision"
    shutil.copytree(original, duplicate_decision)
    _make_publication_writable(duplicate_decision)
    manifest = json.loads((duplicate_decision / "publication-manifest.json").read_bytes())
    receipt = json.loads((duplicate_decision / "publication-receipt.json").read_bytes())
    label = json.loads((duplicate_decision / manifest["label_refs"][0]["path"]).read_bytes())
    duplicated = dict(label["candidate_decisions"][0])
    duplicated.update({"decision": "REJECT", "reason": "WRONG_ACTION"})
    label["candidate_decisions"].append(duplicated)
    label["retained_candidate_refs"][0]["candidate_sha256"] = "f" * 64
    _replace_label(duplicate_decision, manifest, 0, label)
    manifest["counts"]["rejected_candidates"] += 1
    manifest["counts"]["decided_candidates"] += 1
    _rewrite_publication_metadata(duplicate_decision, manifest, receipt)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            duplicate_decision,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_LABEL_INVALID"

    wrong_path = tmp_path / "wrong-content-path"
    shutil.copytree(original, wrong_path)
    _make_publication_writable(wrong_path)
    manifest = json.loads((wrong_path / "publication-manifest.json").read_bytes())
    receipt = json.loads((wrong_path / "publication-receipt.json").read_bytes())
    reference = manifest["label_refs"][0]
    source = wrong_path / reference["path"]
    alternate_prefix = "00" if source.parent.name != "00" else "ff"
    destination = wrong_path / "labels" / "sha256" / alternate_prefix / source.name
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    source.rename(destination)
    reference["path"] = destination.relative_to(wrong_path).as_posix()
    _rewrite_publication_metadata(wrong_path, manifest, receipt)
    with pytest.raises(CurationError) as exc_info:
        AIOnlyActionLabelPublication(
            wrong_path,
            candidate,  # type: ignore[arg-type]
            journal,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_LABEL_INVALID"


def test_material_duplicate_fails_before_publication(
    tmp_path: Path,
    source_fixture: tuple[_FakeCandidateWorkspace, Path, dict[str, Path]],
) -> None:
    candidate, journal, batches = source_fixture
    duplicate_candidate = {
        "candidate_id": _candidate(10_000),
        "candidate_sha256": _hex("duplicate-material-candidate"),
        "predicate": json.loads(
            json.dumps(candidate._outputs[_unit(4)][0]["candidate_items"][0]["predicate"])
        ),
    }
    candidate._outputs[_unit(4)][1]["candidate_items"].append(duplicate_candidate)
    rows = batches["BATCH_1"].read_bytes().splitlines()
    row = json.loads(rows[0])
    row["retained_candidate_ids"].append(duplicate_candidate["candidate_id"])
    row["candidate_decisions"].append(
        {
            "candidate_id": duplicate_candidate["candidate_id"],
            "decision": "RETAIN",
            "reason": "SUPPORTED",
        }
    )
    rows[0] = canonical_json_bytes(row)
    batches["BATCH_1"].write_bytes(b"\n".join(rows) + b"\n")
    batches["BATCH_1"].chmod(0o600)
    output = tmp_path / "never-created-duplicate"
    with pytest.raises(CurationError) as exc_info:
        build_ai_only_action_label_publication(
            output,
            candidate,  # type: ignore[arg-type]
            journal,
            batches,
            repository_root=REPOSITORY_ROOT,
        )
    assert exc_info.value.code == "AI_ONLY_MATERIAL_DUPLICATE"
    assert not output.exists()
