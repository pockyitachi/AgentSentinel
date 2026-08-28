from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.gold_curation import (
    AICandidateWorkspace,
    AnnotationStore,
    CurationPublication,
    ReviewerRegistry,
    SoloCuratorRegistry,
    SoloFirstPassStore,
    capture_ai_candidate_slot,
    create_app,
    prepare_ai_action_gold_campaign,
    seal_ai_candidate_campaign,
)
from mobile_world.offline.gold_curation.ai_assistance import (
    AGENT_SLOTS,
    AI_SCHEMA_FILENAMES,
    AI_SCHEMA_ROOT,
    _assert_local_ai_schema_references,
    _validate_candidate_items,
    _validate_packet,
    _validate_untrusted_agent_value,
    validate_ai_schema_record,
)
from mobile_world.offline.gold_curation.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.offline.gold_curation.solo import SOLO_REGISTRY_SCHEMA_VERSION
from mobile_world.offline.gold_curation.store import REVIEWER_REGISTRY_SCHEMA_VERSION

PYTHON_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = PYTHON_ROOT.parent


def _normalized_wait() -> dict[str, Any]:
    return {
        "class": "mobile_world.runtime.utils.models.JSONAction",
        "serializer": "pydantic model_dump(mode=json, exclude_none=false)",
        "serializer_version": "2.11.7",
        "value": {
            "action_json": None,
            "action_name": None,
            "action_type": "wait",
            "app_name": None,
            "clear_text": None,
            "direction": None,
            "end_x": None,
            "end_y": None,
            "goal_status": None,
            "index": None,
            "keycode": None,
            "start_x": None,
            "start_y": None,
            "text": None,
            "x": None,
            "y": None,
        },
    }


def _write_slot_draft(path: Path, publication: CurationPublication) -> None:
    rows: list[dict[str, Any]] = []
    for unit in sorted(publication.list_units(), key=lambda item: item["unit_id"]):
        packet = publication.packet(unit["unit_id"], "ACTION_GOLD")
        target_pre = next(
            item for item in packet["evidence"] if item["evidence_role"] == "target_pre"
        )
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "response_kind": "CANDIDATES",
                "candidate_items": [
                    {
                        "predicate": {
                            "predicate_kind": "EXACT_NORMALIZED_ACTION",
                            "action_type": "wait",
                            "normalized_action": _normalized_wait(),
                        },
                        "evidence_ids": [target_pre["evidence_id"]],
                        "concise_rationale": "Structural fixture: wait remains visible-only.",
                        "uncertainty_note": "Fixture candidate is not a scientific judgment.",
                    }
                ],
                "abstain_reason": None,
            }
        )
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    path.chmod(0o600)


def _generation_attestation(slot: str, draft: Path) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "mobileworld.g1.ai-candidate-generation-attestation/v1",
        "agent_slot": slot,
        "draft_file_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
        "generation_mode": "ISOLATED_CODEX_RESEARCH_STREAM",
        "input_attestation": {
            "only_frozen_packet_used": True,
            "history_used": False,
            "natural_action_used": False,
            "post_or_later_used": False,
            "outcome_used": False,
            "transformation_used": False,
            "human_review_used": False,
            "peer_agent_output_used": False,
            "chain_of_thought_stored": False,
        },
        "peer_agent_output_visible": False,
        "human_feedback_visible": False,
        "provider_client_created": False,
        "project_model_weights_loaded": False,
        "safety": {
            "target_actor_model_invoked": False,
            "project_gpu_used": False,
            "external_network_used": False,
            "replay_executed": False,
            "action_executed": False,
            "treatment_response_generation_allowed": False,
        },
    }
    value["attestation_sha256"] = canonical_sha256(value)
    return value


@pytest.fixture(scope="module")
def sealed_campaign(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CurationPublication]:
    root = tmp_path_factory.mktemp("g1-ai-campaign")
    root.chmod(0o700)
    publication = CurationPublication()
    manifest = prepare_ai_action_gold_campaign(root, publication)
    assert len(manifest["packet_refs"]) == 190
    for slot in AGENT_SLOTS:
        draft = root.parent / f"slot-{slot}.jsonl"
        _write_slot_draft(draft, publication)
        receipt = capture_ai_candidate_slot(
            root,
            publication,
            agent_slot=slot,
            draft_jsonl_path=draft,
            generation_attestation=_generation_attestation(slot, draft),
        )
        assert receipt["output_count"] == 190
    receipt = seal_ai_candidate_campaign(root, publication)
    assert receipt["output_count"] == 570
    return root, publication


def _write_solo_registry(path: Path) -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": SOLO_REGISTRY_SCHEMA_VERSION,
                "principal": {
                    "principal_id": "one-real-curator",
                    "access_secret": "solo-curator-secret-0001",
                },
            }
        )
    )
    path.chmod(0o600)
    return path


def _write_formal_registry(path: Path, *, secret: str = "solo-curator-secret-0001") -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": REVIEWER_REGISTRY_SCHEMA_VERSION,
                "principals": [
                    {
                        "principal_id": "one-real-curator",
                        "role": "ACTION_GOLD_PRIMARY",
                        "access_secret": secret,
                        "adjudication_channel": None,
                    }
                ],
            }
        )
        + b"\n"
    )
    path.chmod(0o600)
    return path


def test_ai_candidate_schemas_are_closed_and_meta_valid() -> None:
    assert len(AI_SCHEMA_FILENAMES) == 8
    for filename in sorted(AI_SCHEMA_FILENAMES):
        value = json.loads((AI_SCHEMA_ROOT / filename).read_bytes())
        Draft202012Validator.check_schema(value)
        assert value["additionalProperties"] is False
        assert value["$id"].startswith("https://agentsentinel.local/schemas/g1_6_ai/")


def test_unknown_ai_schema_ref_fails_locally_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("schema validation attempted DNS")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_dns)
    schema_id = "https://agentsentinel.local/schemas/g1_6_ai/local-test.schema.json"
    with pytest.raises(Exception) as exc_info:
        _assert_local_ai_schema_references(
            {
                schema_id: {
                    "$id": schema_id,
                    "$ref": "https://unknown.invalid/remote.schema.json#/$defs/value",
                }
            }
        )
    assert getattr(exc_info.value, "code", None) == "AI_CANDIDATE_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "unsafe",
    (
        "Open https://example.invalid",
        "Read /tmp/private-token.txt",
        "<script>alert(1)</script>",
        "password=not-allowed-here",
        "line one\nline two",
        "See (/tmp/private)",
        "read:[../secret]",
        "source=/home/user/x",
        "use{~/secret}",
    ),
)
def test_untrusted_agent_strings_are_rejected_by_runtime_and_schema(
    sealed_campaign: tuple[Path, CurationPublication], unsafe: str
) -> None:
    with pytest.raises(Exception) as runtime_error:
        _validate_untrusted_agent_value({"nested": [{"value": unsafe}]})
    assert getattr(runtime_error.value, "code", None) == "AI_CANDIDATE_UNTRUSTED_STRING"

    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[0]
    changed = json.loads(canonical_json_bytes(workspace.outputs_for_unit(unit_id)[0]))
    changed["candidate_items"][0]["concise_rationale"] = unsafe
    with pytest.raises(Exception):
        validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", changed)
    for filename in (
        "ai_action_gold_candidate_output.schema.json",
        "ai_action_gold_candidate_browser.schema.json",
    ):
        schema = json.loads((AI_SCHEMA_ROOT / filename).read_bytes())
        assert re.fullmatch(schema["$defs"]["safeAgentString"]["pattern"], unsafe) is None


def test_untrusted_agent_strings_reject_non_nfc_recursively() -> None:
    with pytest.raises(Exception) as exc_info:
        _validate_untrusted_agent_value({"predicate": {"text": "e\u0301"}})
    assert getattr(exc_info.value, "code", None) == "AI_CANDIDATE_UNTRUSTED_STRING"


def test_natural_task_directed_text_is_not_misclassified_as_a_credential(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    natural = "No unambiguous task-directed and/or app-directed next action is visible."
    _validate_untrusted_agent_value({"abstain_reason": natural})
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[0]
    changed = json.loads(canonical_json_bytes(workspace.outputs_for_unit(unit_id)[0]))
    changed["candidate_items"][0]["concise_rationale"] = natural
    validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", changed)


def test_stable_exposure_commitment_links_solo_and_formal_and_blocks_eligibility(
    tmp_path: Path,
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    solo = SoloCuratorRegistry.load(_write_solo_registry(tmp_path / "solo-stable.json"))
    formal = ReviewerRegistry.load(_write_formal_registry(tmp_path / "formal-stable.json"))
    stable = solo.stable_principal_commitment("one-real-curator")
    assert stable == formal.stable_principal_commitment("one-real-curator")

    isolated_root = tmp_path / "stable-exposure-campaign"
    shutil.copytree(root, isolated_root)
    workspace = AICandidateWorkspace(isolated_root, publication)
    workspace_scoped = hashlib.sha256(b"separate-workspace-scoped-commitment").hexdigest()
    exposure = workspace.record_exposure(workspace_scoped, stable)
    serialized = canonical_json_bytes(exposure)
    assert b"solo-curator-secret-0001" not in serialized
    assert hashlib.sha256(b"solo-curator-secret-0001").hexdigest().encode() not in serialized
    assert workspace.exposed_stable_principal_commitments() >= {stable}
    with pytest.raises(Exception) as registry_error:
        workspace.assert_formal_registry_eligible(formal)
    assert getattr(registry_error.value, "code", None) == "FORMAL_REVIEWER_AI_EXPOSURE_INELIGIBLE"

    formal_store = AnnotationStore(tmp_path / "formal-state", publication, formal)
    with pytest.raises(Exception) as store_error:
        formal_store.assert_formal_ai_assistance_eligibility(
            workspace.exposed_stable_principal_commitments()
        )
    assert getattr(store_error.value, "code", None) == "FORMAL_REVIEWER_AI_EXPOSURE_INELIGIBLE"

    distinct = ReviewerRegistry.load(
        _write_formal_registry(
            tmp_path / "formal-distinct.json", secret="different-formal-secret-0002"
        )
    )
    workspace.assert_formal_registry_eligible(distinct)


def test_formal_app_requires_exposure_guard_hides_assist_routes_and_rechecks_sessions(
    tmp_path: Path,
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    formal = ReviewerRegistry.load(
        _write_formal_registry(
            tmp_path / "formal-app.json",
            secret="formal-unexposed-secret-0003",
        )
    )
    store = AnnotationStore(tmp_path / "formal-app-state", publication, formal)
    with pytest.raises(Exception) as missing_guard:
        create_app(publication, store)
    assert getattr(missing_guard.value, "code", None) == "AI_CANDIDATE_EXPOSURE_GUARD_REQUIRED"

    workspace = AICandidateWorkspace(root, publication, forbidden_roots=(store.root,))
    app = create_app(publication, store, ai_exposure_workspace=workspace)
    route_paths = {getattr(route, "path", None) for route in app.routes}
    assert not any(
        isinstance(path, str) and path.startswith("/api/assist/") for path in route_paths
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43210))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8766",
        ) as client:
            session = await client.post(
                "/api/session",
                json={
                    "reviewer_id": "one-real-curator",
                    "role": "ACTION_GOLD_PRIMARY",
                    "access_secret": "formal-unexposed-secret-0003",
                },
            )
            assert session.status_code == 200
            missing = await client.get("/api/assist/progress")
            assert missing.status_code == 404
            workspace.record_exposure(
                hashlib.sha256(b"formal-workspace-scoped").hexdigest(),
                formal.stable_principal_commitment("one-real-curator"),
            )
            blocked = await client.get("/api/assignments")
            assert blocked.status_code == 400
            assert blocked.json()["error"] == "FORMAL_REVIEWER_AI_EXPOSURE_INELIGIBLE"

    asyncio.run(exercise())


def test_sealed_campaign_is_exactly_190_by_three_and_non_authoritative(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    assert workspace.receipt["packet_count"] == 190
    assert workspace.receipt["output_count"] == 570
    assert workspace.receipt["disclosure"] == {
        "ai_semantic_suggestion_performed": True,
        "blind_task_and_gui_entered_codex_context": True,
        "three_agents_are_independent_human_reviewers": False,
    }
    assert workspace.receipt["authority"]["counts_as_independent_review"] is False
    assert workspace.receipt["authority"]["human_review_required"] is True
    assert [item["agent_slot"] for item in workspace.receipt["generation_receipts"]] == [
        "A",
        "B",
        "C",
    ]
    for binding in workspace.receipt["generation_receipts"]:
        generation_bytes = (root / binding["path"]).read_bytes()
        generation = json.loads(generation_bytes)
        assert binding["schema_version"] == generation["schema_version"]
        assert binding["receipt_sha256"] == generation["receipt_sha256"]
        assert binding["sha256"] == hashlib.sha256(generation_bytes).hexdigest()
        assert generation["generation_attestation"]["agent_slot"] == binding["agent_slot"]
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[0]
    outputs = workspace.outputs_for_unit(unit_id)
    assert [item["agent_slot"] for item in outputs] == ["A", "B", "C"]
    assert len({item["candidate_items"][0]["candidate_id"] for item in outputs}) == 3


def test_candidate_packet_auxiliary_evidence_is_rederived_from_blind_source(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    reference = workspace.manifest["packet_refs"][0]
    packet = json.loads((root / reference["path"]).read_bytes())
    packet["auxiliary_evidence"] = [
        {
            "evidence_id": packet["screenshot"]["evidence_id"],
            "evidence_role": "tool_response",
            "content_sha256": "0" * 64,
            "content": {"forged": True},
        }
    ]
    packet["packet_sha256"] = canonical_sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    with pytest.raises(Exception):
        _validate_packet(root, publication, packet)

    original = json.loads((root / reference["path"]).read_bytes())
    forged_packet_id = json.loads(canonical_json_bytes(original))
    forged_packet_id["packet_id"] = "g1aipacket-" + "f" * 24
    forged_packet_id["packet_sha256"] = canonical_sha256(
        {key: value for key, value in forged_packet_id.items() if key != "packet_sha256"}
    )
    with pytest.raises(Exception):
        _validate_packet(root, publication, forged_packet_id)

    forged_target_pre = json.loads(canonical_json_bytes(original))
    forged_target_pre["screenshot"]["evidence_id"] = next(
        evidence_id
        for evidence_id in forged_target_pre["evidence_ids"]
        if evidence_id != forged_target_pre["screenshot"]["evidence_id"]
    )
    forged_target_pre["packet_sha256"] = canonical_sha256(
        {key: value for key, value in forged_target_pre.items() if key != "packet_sha256"}
    )
    with pytest.raises(Exception):
        _validate_packet(root, publication, forged_target_pre)


@pytest.mark.parametrize(
    ("coordinate", "value"),
    (("x", -1), ("x", 1080), ("y", -1), ("y", 2400), ("end_x", 1080)),
)
def test_exact_action_candidate_coordinates_are_bound_to_target_pre_pixels(
    sealed_campaign: tuple[Path, CurationPublication],
    coordinate: str,
    value: int,
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = workspace.manifest["packet_refs"][0]["unit_id"]
    packet_ref = workspace.manifest["packet_refs"][0]
    packet = json.loads((root / packet_ref["path"]).read_bytes())
    candidate = json.loads(
        canonical_json_bytes(workspace.outputs_for_unit(unit_id)[0]["candidate_items"][0])
    )
    normalized = _normalized_wait()
    normalized["value"]["action_type"] = "click"
    normalized["value"][coordinate] = value
    candidate["predicate"] = {
        "predicate_kind": "EXACT_NORMALIZED_ACTION",
        "action_type": "click",
        "normalized_action": normalized,
    }
    if value < 0:
        changed_output = json.loads(canonical_json_bytes(workspace.outputs_for_unit(unit_id)[0]))
        changed_output["candidate_items"] = [candidate]
        with pytest.raises(Exception):
            validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", changed_output)
    with pytest.raises(Exception) as exc_info:
        _validate_candidate_items(
            publication,
            unit_id,
            packet,
            [candidate],
            campaign_id=workspace.campaign_id,
            agent_slot="A",
        )
    assert getattr(exc_info.value, "code", None) == "AI_CANDIDATE_GEOMETRY_INVALID"


def test_candidate_decisions_are_separate_append_only_and_supersedable(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[0]
    candidate = workspace.outputs_for_unit(unit_id)[0]["candidate_items"][0]
    identity = hashlib.sha256(b"one-real-curator").hexdigest()
    first = workspace.record_decision(
        unit_id=unit_id,
        candidate_id=candidate["candidate_id"],
        candidate_sha256=candidate["candidate_sha256"],
        human_identity_commitment=identity,
        decision="ADOPT_WITH_EDITS_TO_FORM",
        human_note="I will verify every production field.",
    )
    same = workspace.record_decision(
        unit_id=unit_id,
        candidate_id=candidate["candidate_id"],
        candidate_sha256=candidate["candidate_sha256"],
        human_identity_commitment=identity,
        decision="ADOPT_WITH_EDITS_TO_FORM",
        human_note="I will verify every production field.",
    )
    assert same["event_id"] == first["event_id"]
    second = workspace.record_decision(
        unit_id=unit_id,
        candidate_id=candidate["candidate_id"],
        candidate_sha256=candidate["candidate_sha256"],
        human_identity_commitment=identity,
        decision="IGNORE",
        human_note="Not supported after checking visible evidence.",
    )
    assert second["event_kind"] == "DECISION_SUPERSEDED"
    assert second["supersedes_event_id"] == first["event_id"]
    events = workspace.read_decisions()
    assert [item["event_seq"] for item in events[-2:]] == [len(events) - 2, len(events) - 1]
    assert all(item["authority"]["formal_journal_event_id"] is None for item in events)
    assert all(item["authority"]["solo_journal_event_id"] is None for item in events)

    forged = json.loads(canonical_json_bytes(events[0]))
    forged["event_id"] = "g1aidecision-" + "f" * 24
    forged["event_sha256"] = canonical_sha256(
        {key: value for key, value in forged.items() if key != "event_sha256"}
    )
    with pytest.raises(Exception) as event_error:
        workspace._decode_decisions(canonical_json_bytes(forged) + b"\n")  # noqa: SLF001
    assert getattr(event_error.value, "code", None) == "AI_DECISION_JOURNAL_INVALID"


def test_unit_decision_completion_is_identity_scoped_and_all_abstain_passes(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[1]
    identity = hashlib.sha256(b"candidate-completion-curator").hexdigest()
    candidates = [
        item for output in workspace.outputs_for_unit(unit_id) for item in output["candidate_items"]
    ]
    assert len(candidates) == 3

    with pytest.raises(Exception) as missing:
        workspace.assert_unit_decisions_complete(unit_id, identity)
    assert getattr(missing.value, "code", None) == "AI_CANDIDATE_DECISIONS_INCOMPLETE"
    for candidate in candidates[:-1]:
        workspace.record_decision(
            unit_id=unit_id,
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
            human_identity_commitment=identity,
            decision="IGNORE",
            human_note="Explicitly reviewed for the completion-gate test.",
        )
    with pytest.raises(Exception) as one_missing:
        workspace.assert_unit_decisions_complete(unit_id, identity)
    assert getattr(one_missing.value, "code", None) == "AI_CANDIDATE_DECISIONS_INCOMPLETE"
    workspace.record_decision(
        unit_id=unit_id,
        candidate_id=candidates[-1]["candidate_id"],
        candidate_sha256=candidates[-1]["candidate_sha256"],
        human_identity_commitment=identity,
        decision="IGNORE",
        human_note="Explicitly reviewed for the completion-gate test.",
    )
    workspace.assert_unit_decisions_complete(unit_id, identity)
    with pytest.raises(Exception) as other_identity:
        workspace.assert_unit_decisions_complete(unit_id, "f" * 64)
    assert getattr(other_identity.value, "code", None) == "AI_CANDIDATE_DECISIONS_INCOMPLETE"

    class AllAbstainUnit:
        @staticmethod
        def outputs_for_unit(requested_unit_id: str) -> list[dict[str, Any]]:
            assert requested_unit_id == "all-abstain-unit"
            return [
                {
                    "agent_slot": slot,
                    "response_kind": "ABSTAIN",
                    "candidate_items": [],
                    "abstain_reason": "No atomic item.",
                }
                for slot in AGENT_SLOTS
            ]

        @staticmethod
        def latest_decisions(_human_identity_commitment: str) -> dict[str, dict[str, Any]]:
            return {}

    AICandidateWorkspace.assert_unit_decisions_complete(  # type: ignore[arg-type]
        AllAbstainUnit(), "all-abstain-unit", "e" * 64
    )


def test_capture_requires_explicit_attestation_and_invalid_last_row_leaves_no_orphan(
    tmp_path: Path,
) -> None:
    publication = CurationPublication()
    root = tmp_path / "prepared-campaign"
    root.mkdir(mode=0o700)
    prepare_ai_action_gold_campaign(root, publication)
    draft = tmp_path / "slot-A-invalid.jsonl"
    _write_slot_draft(draft, publication)

    invalid_attestation = _generation_attestation("A", draft)
    invalid_attestation["project_model_weights_loaded"] = True
    invalid_attestation["attestation_sha256"] = canonical_sha256(
        {key: value for key, value in invalid_attestation.items() if key != "attestation_sha256"}
    )
    with pytest.raises(Exception) as attestation_error:
        capture_ai_candidate_slot(
            root,
            publication,
            agent_slot="A",
            draft_jsonl_path=draft,
            generation_attestation=invalid_attestation,
        )
    assert (
        getattr(attestation_error.value, "code", None)
        == "AI_CANDIDATE_GENERATION_ATTESTATION_INVALID"
    )
    assert not (root / "outputs").exists()
    assert not (root / "generation-receipts").exists()

    rows = [json.loads(line) for line in draft.read_bytes().splitlines()]
    rows[-1]["candidate_items"][0]["concise_rationale"] = "https://forbidden.invalid"
    draft.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    draft.chmod(0o600)
    with pytest.raises(Exception) as draft_error:
        capture_ai_candidate_slot(
            root,
            publication,
            agent_slot="A",
            draft_jsonl_path=draft,
            generation_attestation=_generation_attestation("A", draft),
        )
    assert getattr(draft_error.value, "code", None) in {
        "AI_CANDIDATE_SCHEMA_MISMATCH",
        "AI_CANDIDATE_UNTRUSTED_STRING",
    }
    assert not (root / "outputs").exists()
    assert not (root / "generation-receipts").exists()
    assert not any(path.name.startswith(".capture-slot-") for path in root.iterdir())


def test_workspace_rejects_orphan_output_and_terminal_receipt_rebinding(
    tmp_path: Path,
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    orphan_root = tmp_path / "orphan-campaign"
    shutil.copytree(root, orphan_root)
    orphan = orphan_root / "outputs" / "sha256" / "ff" / ("f" * 64 + ".json")
    orphan.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    orphan.parent.chmod(0o700)
    orphan.write_bytes(b"{}\n")
    orphan.chmod(0o600)
    with pytest.raises(Exception) as orphan_error:
        AICandidateWorkspace(orphan_root, publication)
    assert getattr(orphan_error.value, "code", None) == "AI_CANDIDATE_CENSUS_INVALID"

    rebound_root = tmp_path / "rebound-campaign"
    shutil.copytree(root, rebound_root)
    receipt_path = rebound_root / "campaign-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["generation_receipts"][0]["sha256"] = "f" * 64
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    receipt_path.write_bytes(canonical_json_bytes(receipt) + b"\n")
    receipt_path.chmod(0o600)
    with pytest.raises(Exception) as receipt_error:
        AICandidateWorkspace(rebound_root, publication)
    assert getattr(receipt_error.value, "code", None) == "AI_CANDIDATE_INVALID"

    publication_root = tmp_path / "receipt-publication-rebind"
    shutil.copytree(root, publication_root)
    publication_receipt_path = publication_root / "campaign-receipt.json"
    publication_receipt = json.loads(publication_receipt_path.read_bytes())
    publication_receipt["publication_manifest_sha256"] = "e" * 64
    publication_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in publication_receipt.items() if key != "receipt_sha256"}
    )
    publication_receipt_path.write_bytes(canonical_json_bytes(publication_receipt) + b"\n")
    publication_receipt_path.chmod(0o600)
    with pytest.raises(Exception) as publication_error:
        AICandidateWorkspace(publication_root, publication)
    assert getattr(publication_error.value, "code", None) == "AI_CANDIDATE_INVALID"

    campaign_root = tmp_path / "campaign-id-rebind"
    shutil.copytree(root, campaign_root)
    campaign_manifest_path = campaign_root / "campaign-manifest.json"
    campaign_manifest = json.loads(campaign_manifest_path.read_bytes())
    campaign_manifest["campaign_id"] = "g1aicampaign-" + "f" * 24
    campaign_manifest["campaign_manifest_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in campaign_manifest.items()
            if key != "campaign_manifest_sha256"
        }
    )
    campaign_manifest_path.write_bytes(canonical_json_bytes(campaign_manifest) + b"\n")
    campaign_manifest_path.chmod(0o600)
    with pytest.raises(Exception) as campaign_error:
        AICandidateWorkspace(campaign_root, publication)
    assert getattr(campaign_error.value, "code", None) == "AI_CANDIDATE_INVALID"


def test_output_schema_rejects_human_or_authoritative_claims(
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    workspace = AICandidateWorkspace(root, publication)
    unit_id = sorted(item["unit_id"] for item in publication.list_units())[0]
    value = workspace.outputs_for_unit(unit_id)[0]
    for key, replacement in (
        ("counts_as_independent_review", True),
        ("formal_resolution_eligible", True),
        ("auto_apply_allowed", True),
        ("human_review_required", False),
    ):
        changed = json.loads(canonical_json_bytes(value))
        changed["authority"][key] = replacement
        with pytest.raises(Exception):
            validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", changed)
    changed = json.loads(canonical_json_bytes(value))
    changed["candidate_items"][0]["predicate"]["human_selected"] = True
    with pytest.raises(Exception):
        validate_ai_schema_record("ai_action_gold_candidate_output.schema.json", changed)


def test_loopback_api_projects_opaque_candidates_and_never_writes_solo_journal(
    tmp_path: Path,
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    registry = SoloCuratorRegistry.load(_write_solo_registry(tmp_path / "solo.json"))
    store = SoloFirstPassStore(tmp_path / "solo-state", publication, registry)
    workspace = AICandidateWorkspace(root, publication, forbidden_roots=(store.root,))
    app = create_app(publication, store, ai_candidate_workspace=workspace)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43210))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8766",
        ) as client:
            session = await client.post(
                "/api/session",
                json={
                    "reviewer_id": "one-real-curator",
                    "role": "ACTION_GOLD_PRIMARY",
                    "access_secret": "solo-curator-secret-0001",
                },
            )
            assert session.status_code == 200
            csrf = session.json()["csrf_token"]
            assignments = (await client.get("/api/assignments")).json()["items"]
            assignment_id = assignments[0]["assignment_id"]
            before = store._journal.exists()  # noqa: SLF001
            exposures_before = set((workspace.root / "human-exposures").glob("*.json"))
            response = await client.get(f"/api/assist/action-gold/{assignment_id}")
            assert response.status_code == 200
            value = response.json()
            validate_ai_schema_record("ai_action_gold_candidate_browser.schema.json", value)
            exposure_path = (
                workspace.root
                / "human-exposures"
                / f"{store.identity_commitment('one-real-curator')}.json"
            )
            exposure = json.loads(exposure_path.read_bytes())
            exposures_after_first = set((workspace.root / "human-exposures").glob("*.json"))
            assert exposures_after_first == exposures_before | {exposure_path}
            validate_ai_schema_record("ai_candidate_human_exposure.schema.json", exposure)
            assert exposure["exposure_role"] == "AI_ASSISTED_SOLO_CURATOR"
            assert exposure["authority"]["formal_reviewer_eligible"] is False
            repeated = await client.get(f"/api/assist/action-gold/{assignment_id}")
            assert repeated.status_code == 200
            assert repeated.content == response.content
            assert set((workspace.root / "human-exposures").glob("*.json")) == (
                exposures_after_first
            )
            assert [item["agent_slot"] for item in value["agent_outputs"]] == ["A", "B", "C"]
            serialized = json.dumps(value, sort_keys=True)
            assert "g1aicandidate-" not in serialized
            assert "source_packet_sha256" not in serialized
            candidate = value["agent_outputs"][0]["candidate_items"][0]
            malformed = await client.post(
                "/api/assist/candidate-decisions",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "x-g1-csrf-token": csrf,
                },
                json={
                    "assignment_id": assignment_id,
                    "candidate_token": candidate["candidate_token"],
                    "decision": "ADOPT_TO_FORM",
                    "human_note": [],
                    "human_confirmed_item_review": True,
                    "human_verified_visible_evidence": True,
                    "ai_candidate_is_not_evidence": True,
                    "annotation_form_not_saved_or_finalized": True,
                },
            )
            assert malformed.status_code == 400
            assert malformed.json()["error"] == "AI_DECISION_INVALID"
            assert store._journal.exists() is before  # noqa: SLF001
            wrong_assignment = await client.post(
                "/api/assist/candidate-decisions",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "x-g1-csrf-token": csrf,
                },
                json={
                    "assignment_id": assignments[1]["assignment_id"],
                    "candidate_token": candidate["candidate_token"],
                    "decision": "ADOPT_TO_FORM",
                    "human_note": "Cross-assignment tokens must not resolve.",
                    "human_confirmed_item_review": True,
                    "human_verified_visible_evidence": True,
                    "ai_candidate_is_not_evidence": True,
                    "annotation_form_not_saved_or_finalized": True,
                },
            )
            assert wrong_assignment.status_code == 400
            assert wrong_assignment.json()["error"] == "AI_CANDIDATE_UNKNOWN"
            missing_attestation = await client.post(
                "/api/assist/candidate-decisions",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "x-g1-csrf-token": csrf,
                },
                json={
                    "assignment_id": assignment_id,
                    "candidate_token": candidate["candidate_token"],
                    "decision": "ADOPT_TO_FORM",
                    "human_note": "No implicit human attestation.",
                    "human_confirmed_item_review": False,
                    "human_verified_visible_evidence": True,
                    "ai_candidate_is_not_evidence": True,
                    "annotation_form_not_saved_or_finalized": True,
                },
            )
            assert missing_attestation.status_code == 400
            assert missing_attestation.json()["error"] == "AI_DECISION_ATTESTATION_REQUIRED"
            assert store._journal.exists() is before  # noqa: SLF001
            decision = await client.post(
                "/api/assist/candidate-decisions",
                headers={
                    "Origin": "http://127.0.0.1:8766",
                    "x-g1-csrf-token": csrf,
                },
                json={
                    "assignment_id": assignment_id,
                    "candidate_token": candidate["candidate_token"],
                    "decision": "ADOPT_TO_FORM",
                    "human_note": "Checked only the visible task and screenshot.",
                    "human_confirmed_item_review": True,
                    "human_verified_visible_evidence": True,
                    "ai_candidate_is_not_evidence": True,
                    "annotation_form_not_saved_or_finalized": True,
                },
            )
            assert decision.status_code == 200
            assert decision.json()["annotation_form_saved"] is False
            assert store._journal.exists() is before  # noqa: SLF001

    asyncio.run(exercise())


def test_solo_action_lock_requires_every_mounted_candidate_decision_but_draft_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sealed_campaign: tuple[Path, CurationPublication],
) -> None:
    root, publication = sealed_campaign
    registry = SoloCuratorRegistry.load(_write_solo_registry(tmp_path / "solo-lock.json"))
    store = SoloFirstPassStore(tmp_path / "solo-lock-state", publication, registry)
    monkeypatch.setattr(store, "_verified_codec_gate_receipt_sha256", lambda: "a" * 64)
    workspace = AICandidateWorkspace(root, publication, forbidden_roots=(store.root,))
    app = create_app(publication, store, ai_candidate_workspace=workspace)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43210))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8766",
        ) as client:
            session = await client.post(
                "/api/session",
                json={
                    "reviewer_id": "one-real-curator",
                    "role": "ACTION_GOLD_PRIMARY",
                    "access_secret": "solo-curator-secret-0001",
                },
            )
            assert session.status_code == 200
            headers = {
                "Origin": "http://127.0.0.1:8766",
                "x-g1-csrf-token": session.json()["csrf_token"],
            }
            assignment_id = (await client.get("/api/assignments")).json()["items"][0][
                "assignment_id"
            ]
            candidates_response = await client.get(f"/api/assist/action-gold/{assignment_id}")
            assert candidates_response.status_code == 200
            candidates = [
                item
                for output in candidates_response.json()["agent_outputs"]
                for item in output["candidate_items"]
            ]
            assert len(candidates) == 3
            predicate = json.loads(canonical_json_bytes(candidates[0]["predicate"]))
            predicate.update(
                {
                    "evidence_ids": candidates[0]["evidence_tokens"],
                    "rationale": candidates[0]["concise_rationale"],
                    "human_selected": True,
                }
            )
            body = {
                "assignment_id": assignment_id,
                "payload": {
                    "proposal_kind": "ACTION_GOLD",
                    "disposition": "ACCEPT",
                    "exclusion_reason": None,
                    "predicates": [predicate],
                    "evidence_rationale": "Human-verified visible task and target-pre evidence.",
                    "closed_world_confirmed": True,
                    "all_reasonable_actions_enumerated": True,
                },
            }

            draft = await client.post("/api/solo/draft", headers=headers, json=body)
            assert draft.status_code == 200
            before_lock = store.read_events()
            blocked = await client.post("/api/solo/lock", headers=headers, json=body)
            assert blocked.status_code == 400
            assert blocked.json()["error"] == "AI_CANDIDATE_DECISIONS_INCOMPLETE"
            assert store.read_events() == before_lock

            for candidate in candidates:
                decision = await client.post(
                    "/api/assist/candidate-decisions",
                    headers=headers,
                    json={
                        "assignment_id": assignment_id,
                        "candidate_token": candidate["candidate_token"],
                        "decision": "IGNORE",
                        "human_note": "Explicitly reviewed before locking the human-authored form.",
                        "human_confirmed_item_review": True,
                        "human_verified_visible_evidence": True,
                        "ai_candidate_is_not_evidence": True,
                        "annotation_form_not_saved_or_finalized": True,
                    },
                )
                assert decision.status_code == 200
            locked = await client.post("/api/solo/lock", headers=headers, json=body)
            assert locked.status_code == 200, locked.text
            assert locked.json()["locked"] is True
            assert len(store.read_events()) == len(before_lock) + 1

    asyncio.run(exercise())


def test_web_has_no_generation_vote_bulk_accept_or_candidate_autosave() -> None:
    app_js = (PYTHON_ROOT / "src/mobile_world/offline/gold_curation/web/app.js").read_text(
        encoding="utf-8"
    )
    server = (PYTHON_ROOT / "src/mobile_world/offline/gold_curation/server.py").read_text(
        encoding="utf-8"
    )
    assert "第 1 步 · 简易候选审核" in app_js
    assert "逐条选择，不是在三个 Agent 中选一个" in app_js
    assert "ADOPT_TO_FORM" in app_js
    assert "ADOPT_WITH_EDITS_TO_FORM" in app_js
    assert "USE_AS_SUPPLEMENT" in app_js
    assert "IGNORE" in app_js
    assert "data-ai-evidence-verified" in app_js
    assert "if (!evidenceVerified?.checked)" in app_js
    assert "alreadyPresent" not in app_js
    assert "semanticPredicateFingerprint" not in app_js
    assert "item.human_selected = false" in app_js
    assert '$("#closed-world").checked = false' in app_js
    assert '$("#all-actions").checked = false' in app_js
    assert 'api("/api/assist/candidate-decisions"' in app_js
    decision_slice = app_js[
        app_js.index("async function decideAiCandidate") : app_js.index("function actionForm")
    ]
    assert 'api("/api/solo/draft"' not in decision_slice
    assert 'api("/api/solo/lock"' not in decision_slice
    assert "persist(" not in decision_slice
    assert "window.confirm" not in decision_slice
    for forbidden in ("/generate", "/regenerate", "/rank", "/merge", "/accept-all"):
        assert f'@app.post("{forbidden}' not in server


def test_simple_candidate_review_is_explicit_visible_and_keeps_advanced_form() -> None:
    app_js = (PYTHON_ROOT / "src/mobile_world/offline/gold_curation/web/app.js").read_text(
        encoding="utf-8"
    )
    styles = (PYTHON_ROOT / "src/mobile_world/offline/gold_curation/web/styles.css").read_text(
        encoding="utf-8"
    )

    candidate_card = app_js[
        app_js.index("function aiCandidateCard") : app_js.index(
            "function renderAiCandidateOverlays"
        )
    ]
    assert "data-ai-evidence-verified" in candidate_card
    assert 'type="checkbox"' in candidate_card
    assert "checked" not in candidate_card
    assert 'role="alert"' in candidate_card
    assert 'aria-live="assertive"' in candidate_card
    assert "查看技术字段" in app_js
    assert "可选备注与 evidence ID" in candidate_card

    decision_slice = app_js[
        app_js.index("async function decideAiCandidate") : app_js.index("function actionForm")
    ]
    assert "if (!evidenceVerified?.checked)" in decision_slice
    assert "itemFeedback.textContent = message" in decision_slice
    assert "evidenceVerified.focus()" in decision_slice
    assert (
        'evidenceVerified.scrollIntoView({behavior: "smooth", block: "center"})' in decision_slice
    )
    assert 'card.setAttribute("aria-busy", "true")' in decision_slice
    busy_controls = app_js[
        app_js.index("function setAiDecisionUiBusy") : app_js.index("function renderAiCandidates")
    ]
    assert "button.disabled = busy" in busy_controls
    assert "close.disabled = busy" in busy_controls
    assert "save.disabled = busy" in busy_controls
    assert "submit.disabled = busy" in busy_controls
    assert "state.aiDecisionInFlight" in decision_slice
    assert "const assignmentId = state.active?.assignmentId" in decision_slice
    assert "state.active?.assignmentId !== assignmentId" in decision_slice
    assert "state.aiCandidates !== candidateData" in decision_slice
    assert "renderAiCandidates()" in decision_slice
    assert "candidate.current_decision" in decision_slice
    for attestation in (
        "human_confirmed_item_review: true",
        "human_verified_visible_evidence: true",
        "ai_candidate_is_not_evidence: true",
        "annotation_form_not_saved_or_finalized: true",
    ):
        assert attestation in decision_slice

    action_form = app_js[app_js.index("function actionForm") : app_js.index("function spanList")]
    for required_id in (
        "advanced-action-form",
        "predicate-list",
        "add-predicate",
        "closed-world",
        "all-actions",
        "evidence-rationale",
    ):
        assert required_id in action_form
    disposition_form = app_js[
        app_js.index("function commonDisposition") : app_js.index("function predicateActionOptions")
    ]
    assert 'id="disposition"' in disposition_form
    assert 'id="exclusion-reason"' in disposition_form
    assert "<details" in action_form
    assert "第 2 步 · 最终人工确认" in action_form
    collect_payload = app_js[
        app_js.index("function collectPayload") : app_js.index("async function persist")
    ]
    assert '$$(".predicate-card").map((card) => collectPredicate(card))' in collect_payload
    assert 'base.disposition === "EXCLUDE" ? [] : state.predicates' in collect_payload
    assert 'evidence_rationale: $("#evidence-rationale").value' in collect_payload
    assert 'closed_world_confirmed: $("#closed-world").checked' in collect_payload
    assert 'all_reasonable_actions_enumerated: $("#all-actions").checked' in collect_payload
    assert 'const openDialogs = $$("dialog[open]")' in app_js
    assert ".ai-decision-actions button { min-height: 48px" in styles
    assert ".ai-candidate-item.needs-attention" in styles
    assert ".ai-decision-actions button:disabled" in styles
    assert ".ai-inline-feedback.error" in styles
    assert ".advanced-action-form" in styles
    assert "grid-template-columns: repeat(3,minmax(0,1fr))" in styles
    assert "const columns = data.agent_outputs.map" in app_js
    assert "pending[0]" not in app_js
    assert 'event.target.closest(".ai-candidate-panel")' in app_js
    assert "if (state.aiDecisionInFlight) return toast" in app_js
