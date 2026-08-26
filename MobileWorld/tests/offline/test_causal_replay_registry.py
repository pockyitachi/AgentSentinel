from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import mobile_world.offline.causal_replay_registry as registry
from mobile_world.offline.causal_replay_registry import (
    CausalReplayRegistryError,
    load_source_configuration,
    validate_case_record,
    write_registry_artifacts,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _coordinates(text: str, start: int, end: int) -> dict:
    selected = text[start:end]
    return {
        "char_start": start,
        "char_end": end,
        "utf8_byte_start": len(text[:start].encode()),
        "utf8_byte_end": len(text[:end].encode()),
        "span_sha256": _sha(selected),
    }


def _candidate(*, text: str, path: str, source_step: int = 1) -> dict:
    return {
        "candidate_id": f"candidate-{_sha(path)[:24]}",
        "claim": {
            "text": text,
            "source_steps": [source_step],
            "provenance_confidence": "EXACT",
            "representation_type": "flat_progress",
        },
        "exposure": {"request_path": path, "span_sha256": _sha(text), "target_step": 2},
    }


def test_qwen_span_binds_conclusion_and_enclosing_step() -> None:
    conclusion = "I opened Settings."
    content = f"prefix; Step 1: {conclusion}; suffix"
    start = content.index(conclusion)
    end = start + len(conclusion)
    step_start = content.index("Step 1:")
    step_end = end + 1
    candidate = _candidate(
        text=conclusion,
        path=f"payload.request_view.messages[1].content[0].text[{start}:{end}]",
    )
    step = {
        "I_t": {
            "assistant_exposures": [
                {
                    "mapping_status": "exact_qwen_flat_progress",
                    "span_start": start,
                    "span_end": end,
                    "exposed_text_sha256": _sha(conclusion),
                    "step_span_start": step_start,
                    "step_span_end": step_end,
                    "step_span_sha256": _sha(content[step_start:step_end]),
                }
            ]
        }
    }
    resolved = registry._resolve_target_span(
        "FLAT_PROGRESS",
        candidate,
        {"messages": [{}, {"content": [{"text": content}]}]},
        step,
    )
    assert resolved["conclusion_text"] == conclusion
    assert resolved["enclosing_step_span"]["text"] == content[step_start:step_end]


def test_raw_replay_binding_uses_request_path_and_hash_not_normalized_claim() -> None:
    raw = "<thinking> exact spacing </thinking>\n<tool_call>{}</tool_call>"
    candidate = _candidate(
        text="<thinking> exact spacing </thinking> <tool_call>{}</tool_call>",
        path="payload.request_view.messages[7].content",
        source_step=4,
    )
    candidate["claim"]["representation_type"] = "raw_replay"
    candidate["exposure"]["span_sha256"] = _sha(raw)
    step = {
        "I_t": {
            "assistant_exposures": [
                {
                    "mapping_status": "exact_content_monotonic",
                    "message_index": 7,
                    "source_step_index": 4,
                }
            ]
        }
    }
    messages = [{"content": "unused"} for _ in range(8)]
    messages[7]["content"] = raw
    resolved = registry._resolve_target_span("RAW_REPLAY", candidate, {"messages": messages}, step)
    assert resolved["raw_request_text"] == raw
    assert resolved["container_sha256"] == _sha(raw)
    assert resolved["char_start"] == resolved["utf8_byte_start"] == 0
    assert resolved["char_end"] == len(raw)
    assert resolved["utf8_byte_end"] == len(raw.encode())
    assert resolved["edit_span_status"] == "G1_6_PENDING"
    assert resolved["focal_edit_spans"] == []
    assert resolved["curation_envelope"]["editable"] is False
    assert (
        raw[resolved["curation_envelope"]["char_start"] : resolved["curation_envelope"]["char_end"]]
        == "exact spacing"
    )
    assert "<tool_call>{}</tool_call>" in resolved["raw_request_text"]

    candidate["claim"]["text"] = "reviewer paraphrase"
    assert (
        registry._resolve_target_span("RAW_REPLAY", candidate, {"messages": messages}, step)
        == resolved
    )


def test_pre_gold_case_is_immutable_and_inclusion_is_append_only() -> None:
    case = _minimal_prepared_case()
    validate_case_record(case)
    case["case_status"] = "INCLUDED"
    with pytest.raises(CausalReplayRegistryError, match="pre gold case status invalid"):
        validate_case_record(case)
    case["case_status"] = "CANDIDATE_FROZEN"
    with pytest.raises(CausalReplayRegistryError, match="pre gold evidence root forbidden"):
        validate_case_record(case, evidence_root="/tmp")


@pytest.mark.parametrize("action_type", ["unknown", "error_env"])
def test_original_action_rejects_placeholders(action_type: str) -> None:
    with pytest.raises(CausalReplayRegistryError, match="placeholder forbidden"):
        registry._validate_original_action(
            {"value": {"action_type": action_type}},
            {"parse_outcome": "returned", "parse_exception": None},
        )


def test_clean_control_selection_is_stable_and_meets_task_breadth() -> None:
    pool = [
        {
            "task_name": task,
            "rank": _sha(f"{task}-{index}"),
            "candidate": {"candidate_id": f"{task}-{index}"},
        }
        for task in ("a", "b", "c")
        for index in range(2)
    ]
    first = registry._select_clean_controls(pool, target=4, minimum_tasks=3)
    second = registry._select_clean_controls(list(reversed(pool)), target=4, minimum_tasks=3)
    assert [item["candidate"]["candidate_id"] for item in first] == [
        item["candidate"]["candidate_id"] for item in second
    ]
    assert len({item["task_name"] for item in first}) == 3


def test_paired_unit_identity_binds_model_config_and_provider_call_not_candidate_id() -> None:
    source = registry.RegistrySource(
        source_key="qwen",
        study_role="PRIMARY",
        history_family="FLAT_PROGRESS",
        model_id="qwen3vl_8b",
        audit_root="audit/qwen",
        final_reviews_relative_path="review/final/reviews.jsonl",
        final_reviews_sha256="1" * 64,
        curated_manifest="curated/manifest.json",
        curated_manifest_sha256="2" * 64,
        model_manifest="/external/model-manifest.json",
        model_manifest_sha256="3" * 64,
    )
    capsule = {
        "decision": {"request_event_id": "request-event-1"},
        "request_view_sha256": "4" * 64,
        "current_gui_blob": {"digest": "5" * 64},
        "sdk_arguments_snapshot_blob": {"digest": "6" * 64},
    }

    def identity(candidate_id: str, *, value: registry.RegistrySource = source) -> str:
        assert candidate_id
        return registry._paired_unit_identity(
            "clean-control",
            source=value,
            model_config_record_sha256="7" * 64,
            task_run_id="task-run-1",
            target_step=2,
            capsule=capsule,
        )

    assert identity("candidate-before-rename") == identity("candidate-after-rename")
    assert identity("candidate", value=replace(source, source_key="qwen-alias")) == identity(
        "candidate"
    )
    assert identity("candidate", value=replace(source, model_manifest_sha256="8" * 64)) != identity(
        "candidate"
    )
    changed_record = registry._paired_unit_identity(
        "clean-control",
        source=source,
        model_config_record_sha256="9" * 64,
        task_run_id="task-run-1",
        target_step=2,
        capsule=capsule,
    )
    assert changed_record != identity("candidate")
    changed_call = deepcopy(capsule)
    changed_call["decision"]["request_event_id"] = "request-event-2"
    assert registry._paired_unit_identity(
        "clean-control",
        source=source,
        model_config_record_sha256="7" * 64,
        task_run_id="task-run-1",
        target_step=2,
        capsule=changed_call,
    ) != identity("candidate")


def _current_contract_files() -> list[dict[str, str]]:
    repo_root = Path(__file__).parents[3]
    relative_paths = {
        "mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md",
        "mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md",
        "mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
        "MobileWorld/src/mobile_world/offline/causal_replay_registry.py",
        "MobileWorld/scripts/build_g1_causal_replay_registry.py",
        "MobileWorld/tests/offline/test_causal_replay_registry.py",
        *{
            path.relative_to(repo_root).as_posix()
            for path in (repo_root / "mobileworld_audit_handoff/schemas/g1").glob("*.schema.json")
        },
    }
    return [
        {
            "path": relative,
            "sha256": hashlib.sha256((repo_root / relative).read_bytes()).hexdigest(),
        }
        for relative in sorted(relative_paths)
    ]


def test_config_fails_closed_on_forbidden_input_and_wrong_live_assignment(tmp_path: Path) -> None:
    source = {
        "source_key": "qwen",
        "study_role": "PRIMARY",
        "history_family": "FLAT_PROGRESS",
        "model_id": "qwen3vl_8b",
        "audit_root": "audit/outcomes",
        "final_reviews_relative_path": "review/final/reviews.jsonl",
        "final_reviews_sha256": "0" * 64,
        "curated_manifest": "curated/manifest.json",
        "curated_manifest_sha256": "0" * 64,
        "model_manifest": str(tmp_path / "model.json"),
        "model_manifest_sha256": "0" * 64,
        "expected_task_count": 1,
        "expected_strict_case_count": 1,
        "expected_strict_task_count": 1,
        "expected_clean_pool_count": 0,
        "clean_control_target": 30,
        "clean_control_min_tasks": 30,
    }
    config = {
        "protocol_version": registry.PROTOCOL_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "contract_files": _current_contract_files(),
        "sources": [source],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(CausalReplayRegistryError, match="forbidden future evidence input"):
        load_source_configuration(path)
    config["sources"][0]["audit_root"] = "audit/qwen"
    config["sources"][0]["model_id"] = "mai_ui_8b"
    path.write_text(json.dumps(config))
    with pytest.raises(CausalReplayRegistryError, match="source model history assignment invalid"):
        load_source_configuration(path)


def test_write_once_never_overwrites(tmp_path: Path) -> None:
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {"registry_manifest.json": b"{}\n", "cases.jsonl": b"{}\n"},
    }
    output = tmp_path / "registry"
    write_registry_artifacts(artifacts, output)
    with pytest.raises(CausalReplayRegistryError, match="output exists"):
        write_registry_artifacts(artifacts, output)
    assert (output / "cases.jsonl").read_bytes() == b"{}\n"


def test_writer_does_not_remove_race_created_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {"registry_manifest.json": b"{}\n"},
    }
    output = tmp_path / "registry"
    real_mkdir = registry.os.mkdir

    def racing_mkdir(
        path: str | bytes | Path, mode: int = 0o777, *args: object, **kwargs: object
    ) -> None:
        if Path(path) == output:
            real_mkdir(path, mode, *args, **kwargs)
            (output / "other-writer-marker").write_bytes(b"preserve")
            raise FileExistsError(str(path))
        real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(registry.os, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        write_registry_artifacts(artifacts, output)
    assert (output / "other-writer-marker").read_bytes() == b"preserve"


def test_writer_cleanup_removes_only_its_successful_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {
            "cases.jsonl": b"{}\n",
            "registry_manifest.json": b"{}\n",
        },
    }
    output = tmp_path / "registry"
    real_link = registry.os.link
    call_count = 0

    def interrupted_link(source: Path, destination: Path, *args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            real_link(source, destination, *args, **kwargs)
            return
        (output / "unknown-concurrent-file").write_bytes(b"preserve")
        raise OSError("injected publish interruption")

    monkeypatch.setattr(registry.os, "link", interrupted_link)
    with pytest.raises(OSError, match="injected publish interruption"):
        write_registry_artifacts(artifacts, output)
    assert not (output / "cases.jsonl").exists()
    assert (output / "unknown-concurrent-file").read_bytes() == b"preserve"


def test_writer_does_not_clean_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {
            "cases.jsonl": b"{}\n",
            "registry_manifest.json": b"{}\n",
        },
    }
    output = tmp_path / "registry"
    displaced = tmp_path / "registry-displaced"
    real_link = registry.os.link
    call_count = 0

    def replacing_link(source: Path, destination: Path, *args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            real_link(source, destination, *args, **kwargs)
            return
        output.rename(displaced)
        output.mkdir()
        raise OSError("injected directory replacement")

    monkeypatch.setattr(registry.os, "link", replacing_link)
    with pytest.raises(OSError, match="injected directory replacement"):
        write_registry_artifacts(artifacts, output)
    assert output.is_dir()
    assert not any(output.iterdir())
    assert displaced.is_dir()
    assert not (displaced / "cases.jsonl").exists()


def test_writer_rejects_path_replacement_after_final_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {"registry_manifest.json": b"{}\n"},
    }
    output = tmp_path / "registry"
    displaced = tmp_path / "registry-displaced"
    real_fchmod = registry.os.fchmod

    def replacing_fchmod(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        if mode == 0o555:
            output.rename(displaced)
            output.mkdir()

    monkeypatch.setattr(registry.os, "fchmod", replacing_fchmod)
    with pytest.raises(CausalReplayRegistryError, match="output directory replaced"):
        write_registry_artifacts(artifacts, output)
    assert output.is_dir()
    assert not any(output.iterdir())
    assert displaced.is_dir()
    assert not (displaced / "registry_manifest.json").exists()


def test_writer_rejects_repository_local_derived_data() -> None:
    repo_root = Path(__file__).parents[3]
    artifacts = {
        "manifest": {"ok": True},
        "file_payloads": {"registry_manifest.json": b"{}\n"},
    }
    with pytest.raises(CausalReplayRegistryError, match="repo local registry output forbidden"):
        write_registry_artifacts(artifacts, repo_root / ".g1-derived-test-output")


def test_registry_file_set_rejects_extra_response_directory_and_symlink(tmp_path: Path) -> None:
    expected = {"registry_manifest.json", "case_registry.pre_gold.jsonl"}
    (tmp_path / "registry_manifest.json").write_bytes(b"{}\n")
    case_path = tmp_path / "case_registry.pre_gold.jsonl"
    case_path.write_bytes(b"{}\n")
    registry._validate_registry_root_file_set(tmp_path, expected)

    treatment = tmp_path / "treatment_response.jsonl"
    treatment.write_bytes(b"{}\n")
    with pytest.raises(CausalReplayRegistryError, match="registry root file set mismatch"):
        registry._validate_registry_root_file_set(tmp_path, expected)
    treatment.unlink()

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with pytest.raises(CausalReplayRegistryError, match="registry root file set mismatch"):
        registry._validate_registry_root_file_set(tmp_path, expected)
    unknown.rmdir()

    case_path.unlink()
    case_path.symlink_to(tmp_path / "registry_manifest.json")
    with pytest.raises(CausalReplayRegistryError, match="registry root entry not regular"):
        registry._validate_registry_root_file_set(tmp_path, expected)


def test_history_depth_is_recomputed_from_complete_semantic_records() -> None:
    qwen_text = "Task progress: Step 1: first.; Step 2: second.; \n"
    qwen_view = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": qwen_text}],
            }
        ]
    }
    qwen_records = registry._semantic_history_records(qwen_view)
    assert len(qwen_records) == 2
    for index, (path, start, end, span_hash) in enumerate(qwen_records):
        target = {
            "record_binding": {
                "request_path": path,
                "semantic_record": {
                    **_coordinates(qwen_text, start, end),
                    "span_sha256": span_hash,
                },
            }
        }
        assert registry._expected_history_depth(qwen_view, target) == 2 - index

    mai_view = {
        "messages": [
            {"role": "assistant", "content": "oldest"},
            {"role": "user", "content": [{"type": "image_url"}]},
            {"role": "assistant", "content": "middle"},
            {"role": "assistant", "content": "newest"},
            {"role": "user", "content": [{"type": "image_url"}]},
        ]
    }
    for message_index, expected_depth in ((0, 3), (2, 2), (3, 1)):
        text = mai_view["messages"][message_index]["content"]
        path = f"payload.request_view.messages[{message_index}].content"
        target = {
            "record_binding": {
                "request_path": path,
                "semantic_record": _coordinates(text, 0, len(text)),
            }
        }
        assert registry._expected_history_depth(mai_view, target) == expected_depth


def test_delimiter_repairs_bind_selected_source_syntax() -> None:
    focal_text = "<thinking>\nThought: bad premise\nunrelated tail ; ❌ →\n</thinking>"
    benign_text = "<thinking>\nThought: benign note\n</thinking>"
    request_view = {
        "messages": [
            {"role": "assistant", "content": focal_text},
            {"role": "assistant", "content": benign_text},
            {"role": "user", "content": [{"type": "image_url"}]},
        ]
    }

    def target(message_index: int, text: str, phrase: str, digit: str) -> dict:
        start = text.index(phrase)
        return {
            "target_id": f"g1span-{digit * 24}",
            "record_binding": {
                "record_identity_sha256": digit * 64,
                "request_path": f"payload.request_view.messages[{message_index}].content",
            },
            "edit_span": _coordinates(text, start, start + len(phrase)),
        }

    focal = target(0, focal_text, "bad premise", "1")
    benign = target(1, benign_text, "benign note", "2")

    def repair(text: str, record_identity: str, digit: str) -> dict:
        start = text.index("Thought:")
        end = start + len("Thought: ")
        return {
            "repair_id": f"g1repair-{digit * 24}",
            "record_identity_sha256": record_identity,
            "operation": "DELETE_ORPHAN_SEPARATOR",
            "deleted_syntax_span": _coordinates(text, start, end),
            "semantic_content_added": False,
        }

    focal_repair = repair(focal_text, "1" * 64, "1")
    benign_repair = repair(benign_text, "2" * 64, "2")
    repairs = {
        "MASK": [focal_repair],
        "MASK_CORRECTION": [],
        "ORACLE_CLEAN": [deepcopy(focal_repair)],
        "SHAM_BENIGN_EDIT": [benign_repair],
    }
    registry._validate_delimiter_repairs(
        request_view,
        repairs,
        focal_targets=[focal],
        oracle_targets=[focal],
        benign_target=benign,
        protected_spans=[],
        correction_text_by_target_id={focal["target_id"]: "correct premise"},
    )

    wrongly_reused_mask_repair = deepcopy(repairs)
    wrongly_reused_mask_repair["MASK_CORRECTION"] = deepcopy(wrongly_reused_mask_repair["MASK"])
    with pytest.raises(CausalReplayRegistryError, match="delimiter repair not causally empty"):
        registry._validate_delimiter_repairs(
            request_view,
            wrongly_reused_mask_repair,
            focal_targets=[focal],
            oracle_targets=[focal],
            benign_target=benign,
            protected_spans=[],
            correction_text_by_target_id={focal["target_id"]: "correct premise"},
        )

    contaminated = deepcopy(repairs)
    contaminated_span = contaminated["MASK"][0]["deleted_syntax_span"]
    contaminated_span.update(
        _coordinates(
            focal_text, focal_text.index("Thought:"), focal_text.index("premise") + len("premise")
        )
    )
    with pytest.raises(CausalReplayRegistryError, match="syntax not whitelisted"):
        registry._validate_delimiter_repairs(
            request_view,
            contaminated,
            focal_targets=[focal],
            oracle_targets=[focal],
            benign_target=benign,
            protected_spans=[],
            correction_text_by_target_id={focal["target_id"]: "correct premise"},
        )

    for token, expected_error in (
        (";", "not adjacent to selected target"),
        ("❌", "syntax not whitelisted"),
        ("→", "syntax not whitelisted"),
    ):
        forged = deepcopy(repairs)
        start = focal_text.rindex(token)
        forged["MASK"][0]["deleted_syntax_span"] = _coordinates(
            focal_text, start, start + len(token)
        )
        with pytest.raises(CausalReplayRegistryError, match=expected_error):
            registry._validate_delimiter_repairs(
                request_view,
                forged,
                focal_targets=[focal],
                oracle_targets=[focal],
                benign_target=benign,
                protected_spans=[],
                correction_text_by_target_id={focal["target_id"]: "correct premise"},
            )

    nonempty_text = "<thinking>\nThought: bad premise unrelated tail\n</thinking>"
    nonempty_view = {
        "messages": [
            {"role": "assistant", "content": nonempty_text},
            {"role": "assistant", "content": benign_text},
            {"role": "user", "content": [{"type": "image_url"}]},
        ]
    }
    nonempty_focal = target(0, nonempty_text, "bad premise", "1")
    nonempty_repair = repair(nonempty_text, "1" * 64, "3")
    nonempty_repairs = {
        "MASK": [nonempty_repair],
        "MASK_CORRECTION": [],
        "ORACLE_CLEAN": [deepcopy(nonempty_repair)],
        "SHAM_BENIGN_EDIT": [benign_repair],
    }
    with pytest.raises(CausalReplayRegistryError, match="delimiter repair not causally empty"):
        registry._validate_delimiter_repairs(
            nonempty_view,
            nonempty_repairs,
            focal_targets=[nonempty_focal],
            oracle_targets=[nonempty_focal],
            benign_target=benign,
            protected_spans=[],
            correction_text_by_target_id={nonempty_focal["target_id"]: "correct premise"},
        )

    empty_wrapper_text = "<thinking>bad premise</thinking>"
    empty_wrapper_view = {
        "messages": [
            {"role": "assistant", "content": empty_wrapper_text},
            {"role": "assistant", "content": benign_text},
        ]
    }
    empty_wrapper_focal = target(0, empty_wrapper_text, "bad premise", "4")

    def wrapper_repair(token: str, digit: str) -> dict:
        start = empty_wrapper_text.index(token)
        return {
            "repair_id": f"g1repair-{digit * 24}",
            "record_identity_sha256": "4" * 64,
            "operation": "DELETE_EMPTY_DELIMITER",
            "deleted_syntax_span": _coordinates(
                empty_wrapper_text,
                start,
                start + len(token),
            ),
            "semantic_content_added": False,
        }

    wrapper_pair = [wrapper_repair("<thinking>", "4"), wrapper_repair("</thinking>", "5")]
    wrapper_repairs = {
        "MASK": wrapper_pair,
        "MASK_CORRECTION": [],
        "ORACLE_CLEAN": deepcopy(wrapper_pair),
        "SHAM_BENIGN_EDIT": [benign_repair],
    }
    replacement = {empty_wrapper_focal["target_id"]: "correct premise"}
    registry._validate_delimiter_repairs(
        empty_wrapper_view,
        wrapper_repairs,
        focal_targets=[empty_wrapper_focal],
        oracle_targets=[empty_wrapper_focal],
        benign_target=benign,
        protected_spans=[],
        correction_text_by_target_id=replacement,
    )
    correction_deletes_nonempty_wrapper = deepcopy(wrapper_repairs)
    correction_deletes_nonempty_wrapper["MASK_CORRECTION"] = deepcopy(wrapper_pair)
    with pytest.raises(CausalReplayRegistryError, match="delimiter repair not causally empty"):
        registry._validate_delimiter_repairs(
            empty_wrapper_view,
            correction_deletes_nonempty_wrapper,
            focal_targets=[empty_wrapper_focal],
            oracle_targets=[empty_wrapper_focal],
            benign_target=benign,
            protected_spans=[],
            correction_text_by_target_id=replacement,
        )


def test_static_schemas_are_valid_strict_and_non_deployment() -> None:
    schema_root = Path(__file__).parents[3] / "mobileworld_audit_handoff" / "schemas" / "g1"
    schemas = [json.loads(path.read_text()) for path in sorted(schema_root.glob("*.schema.json"))]
    assert schemas
    for schema in schemas:
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["curated"] == {"const": True}
        assert schema["properties"]["deployment_prediction"] == {"const": False}


def test_g1_6_admission_never_claims_execution_readiness() -> None:
    root = Path(__file__).parents[3] / "mobileworld_audit_handoff" / "schemas" / "g1"
    admission = json.loads((root / "admission.schema.json").read_text())
    seal = json.loads((root / "admission_seal.schema.json").read_text())
    assert "run_ready" not in admission["properties"]
    assert admission["properties"]["execution_ready"] == {"const": False}
    assert admission["properties"]["treatment_response_generation_allowed"] == {"const": False}
    assert seal["properties"]["admission_ready"] == {"const": True}
    assert seal["properties"]["execution_ready"] == {"const": False}
    assert seal["properties"]["treatment_response_generation_allowed"] == {"const": False}


def _admission_validation_receipt(
    admission_status: str,
    *,
    reason_codes: list[str],
    mechanical_failure_evidence: list[dict],
) -> dict:
    checks = (
        {
            "all_refs_hash_resolved": True,
            "payload_schemas_valid": True,
            "evidence_cutoff_valid": True,
            "review_ledgers_valid": True,
            "transformations_valid": True,
            "arm_plans_valid": True,
            "future_evidence_leakage_zero": True,
            "treatment_response_count_zero": True,
            "exclusion_reason_valid": "NOT_APPLICABLE",
        }
        if admission_status == "INCLUDED"
        else registry._expected_excluded_validation_checks(reason_codes)
    )
    return {
        "schema_version": "mobileworld.g1.causal-replay-admission-validation/v1",
        "record_type": "causal_replay_admission_validation",
        "protocol_version": registry.PROTOCOL_VERSION,
        "validation_id": "g1admissionvalidation-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "admission_id": "g1admission-0123456789abcdef01234567",
        "unit_id": "g1case-0123456789abcdef01234567",
        "admission_status": admission_status,
        "validation_result": (
            "INCLUDED_VALIDATED"
            if admission_status == "INCLUDED"
            else "EXCLUSION_EVIDENCE_VALIDATED"
        ),
        "reason_codes": reason_codes,
        "valid": True,
        "checks": checks,
        "mechanical_failure_evidence": mechanical_failure_evidence,
        "treatment_response_count": 0,
    }


def test_admission_validation_receipt_has_status_specific_truthful_checks() -> None:
    included = _admission_validation_receipt(
        "INCLUDED",
        reason_codes=[],
        mechanical_failure_evidence=[],
    )
    registry._validate_static_schema("admission_validation.schema.json", included)

    mechanical = {
        "reason_code": "SOURCE_REFERENCE_UNRESOLVED",
        "validator_id": "SOURCE_REGISTRY_RECORD_VALIDATOR",
        "validator_failure_code": "source_registry_file_hash_mismatch",
    }
    excluded = _admission_validation_receipt(
        "EXCLUDED",
        reason_codes=["SOURCE_REFERENCE_UNRESOLVED"],
        mechanical_failure_evidence=[mechanical],
    )
    registry._validate_static_schema("admission_validation.schema.json", excluded)

    false_all_green = deepcopy(excluded)
    false_all_green["checks"] = deepcopy(included["checks"])
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry._validate_static_schema("admission_validation.schema.json", false_all_green)
    missing_mechanical_proof = deepcopy(excluded)
    missing_mechanical_proof["mechanical_failure_evidence"] = []
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry._validate_static_schema(
            "admission_validation.schema.json", missing_mechanical_proof
        )


def test_mechanical_exclusion_replays_gate_and_rejects_forged_failure_code(
    tmp_path: Path,
) -> None:
    frozen = {
        "eligibility_only_refs": {
            "facts": {
                "coverage_complete": True,
                "actual_exposure": True,
                "provenance_exact_or_high": False,
                "validity_refuted_or_stale": True,
                "explicit_use": True,
                "low_state_confound": True,
            }
        }
    }
    validator_id, failure_code = registry._replay_mechanical_exclusion(
        "PROVENANCE_BELOW_HIGH",
        root=tmp_path,
        source_base=tmp_path,
        record={},
        frozen_record=frozen,
        expected_unit={},
        referenced={},
        arm_plans={},
    )
    assert (validator_id, failure_code) == (
        "STRICT_MHR_GATE_VALIDATOR",
        "admission_provenance_below_high",
    )
    registry._validate_mechanical_failure_receipt(
        {
            "reason_code": "PROVENANCE_BELOW_HIGH",
            "validator_id": validator_id,
            "validator_failure_code": failure_code,
        },
        reason="PROVENANCE_BELOW_HIGH",
        expected_validator=validator_id,
        actual_failure=failure_code,
    )
    with pytest.raises(CausalReplayRegistryError, match="failure receipt mismatch"):
        registry._validate_mechanical_failure_receipt(
            {
                "reason_code": "PROVENANCE_BELOW_HIGH",
                "validator_id": validator_id,
                "validator_failure_code": "self_reported_fake_failure",
            },
            reason="PROVENANCE_BELOW_HIGH",
            expected_validator=validator_id,
            actual_failure=failure_code,
        )
    frozen["eligibility_only_refs"]["facts"]["provenance_exact_or_high"] = True
    with pytest.raises(CausalReplayRegistryError, match="exclusion not reproduced"):
        registry._replay_mechanical_exclusion(
            "PROVENANCE_BELOW_HIGH",
            root=tmp_path,
            source_base=tmp_path,
            record={},
            frozen_record=frozen,
            expected_unit={},
            referenced={},
            arm_plans={},
        )


def _admission_record_for_schema(reason_codes: list[str]) -> dict:
    unit_ref = _curation_input_manifest(
        "ACTION_GOLD",
        [
            _curation_evidence_ref("target_pre", "1"),
            _curation_evidence_ref("task_instruction", "2"),
        ],
    )["unit_ref"]
    return {
        "schema_version": "mobileworld.g1.causal-replay-admission/v1",
        "record_type": "causal_replay_admission",
        "protocol_version": registry.PROTOCOL_VERSION,
        "admission_id": "g1admission-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "curation_phase": "G1_6_FROZEN",
        "source_registry_status": "CANDIDATE_FROZEN",
        "unit_ref": unit_ref,
        "source_registry_record_ref": {
            "relative_path": "case_registry.pre_gold.jsonl",
            "file_sha256": "8" * 64,
            "file_byte_count": 1,
            "record_index": 0,
            "record_id": unit_ref["unit_id"],
            "record_sha256": "9" * 64,
        },
        "admission_status": "EXCLUDED",
        "reason_codes": reason_codes,
        "action_gold_bundle_ref": None,
        "action_gold_review_ledger_ref": None,
        "transformation_plan_ref": None,
        "transformation_review_ledger_ref": None,
        "review_identity_sets": {"action_gold": [], "transformation": []},
        "cross_channel_reviewer_identities_disjoint": True,
        "applicable_arms": list(registry.ARM_IDS),
        "arm_plan_refs": {arm_id: None for arm_id in registry.ARM_IDS},
        "validation_receipt_ref": _content_ref(
            "mobileworld.g1.causal-replay-admission-validation/v1", digit="a"
        ),
        "curation_and_admission_sealed": True,
        "admission_ready": False,
        "execution_ready": False,
        "treatment_response_generation_allowed": False,
        "sealed_before_treatment": True,
        "treatment_response_count_seen": 0,
    }


def test_curator_exclusion_schema_requires_the_matching_review_ledger() -> None:
    action = _admission_record_for_schema(["NO_GOLD_CONSENSUS"])
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry._validate_static_schema("admission.schema.json", action)
    action["action_gold_review_ledger_ref"] = _content_ref(
        "mobileworld.g1.causal-replay-curation-review-ledger/v1", digit="b"
    )
    registry._validate_static_schema("admission.schema.json", action)

    transformation = _admission_record_for_schema(["NO_VALID_CORRECTION"])
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry._validate_static_schema("admission.schema.json", transformation)
    transformation["transformation_review_ledger_ref"] = _content_ref(
        "mobileworld.g1.causal-replay-curation-review-ledger/v1", digit="c"
    )
    registry._validate_static_schema("admission.schema.json", transformation)


def test_gold_predicates_bind_exact_action_type_and_typed_text_field() -> None:
    exact = {
        "predicate_kind": "EXACT_NORMALIZED_ACTION",
        "action_type": "wait",
    }
    wait_action = {"value": {"action_type": "wait"}}
    registry._validate_action_predicate_semantics(exact, normalized_action=wait_action)
    with pytest.raises(CausalReplayRegistryError, match="exact action predicate type mismatch"):
        registry._validate_action_predicate_semantics(
            {**exact, "action_type": "click"},
            normalized_action=wait_action,
        )

    text_base = {
        "predicate_id": "g1predicate-0123456789abcdef01234567",
        "predicate_kind": "TEXT_VARIANTS",
        "unicode_normalization": "NFC",
        "case_sensitive": True,
        "allowed_values": ["value"],
    }
    schema = json.loads(
        (
            Path(__file__).parents[3]
            / "mobileworld_audit_handoff/schemas/g1/action_gold_bundle.schema.json"
        ).read_text()
    )["$defs"]["textVariantsPredicate"]
    validator = Draft202012Validator(schema)
    valid_pairs = {
        "input_text": "text",
        "answer": "text",
        "finished": "text",
        "ask_user": "text",
        "open_app": "app_name",
        "status": "goal_status",
    }
    for action_type, field in valid_pairs.items():
        predicate = {**text_base, "action_type": action_type, "field": field}
        assert not list(validator.iter_errors(predicate))
        registry._validate_action_predicate_semantics(predicate)

    for action_type, wrong_field in (
        ("open_app", "text"),
        ("finished", "goal_status"),
    ):
        predicate = {**text_base, "action_type": action_type, "field": wrong_field}
        assert list(validator.iter_errors(predicate))
        with pytest.raises(CausalReplayRegistryError, match="text predicate field mismatch"):
            registry._validate_action_predicate_semantics(predicate)


def _schema_validator(name: str) -> Draft202012Validator:
    root = Path(__file__).parents[3] / "mobileworld_audit_handoff" / "schemas" / "g1"
    schema = json.loads((root / name).read_text())
    return Draft202012Validator(schema)


def _content_ref(schema_version: str, *, digit: str = "a") -> dict:
    return {
        "relative_path": f"artifacts/{digit}.json",
        "sha256": digit * 64,
        "byte_count": 1,
        "schema_version": schema_version,
    }


def _retry_policy() -> dict:
    return {
        "maximum_provider_attempts": 3,
        "retryable_failures": ["TIMEOUT", "HTTP_5XX", "CONNECTION_ERROR"],
        "request_bytes_identical_across_attempts": True,
        "seed_identical_across_attempts": True,
        "decoding_config_identical_across_attempts": True,
        "parser_failure_retried": False,
        "refusal_retried": False,
        "no_op_retried": False,
    }


def _isolation() -> dict:
    return {
        "fresh_provider_conversation": True,
        "conversation_carry_over": False,
        "kv_cache_carry_over": False,
        "response_fed_to_later_request": False,
        "generated_action_executed": False,
    }


def _run_record() -> dict:
    case_id = "g1case-0123456789abcdef01234567"
    order = list(registry.arm_order(model_id="qwen3vl_8b", case_id=case_id, block_index=1))
    arm_id = "ORIGINAL"
    return {
        "schema_version": "mobileworld.g1.causal-replay-run/v1",
        "record_type": "causal_replay_run",
        "protocol_version": registry.PROTOCOL_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "run_id": "g1run-0123456789abcdef01234567",
        "unit_kind": "STRICT_MHR",
        "case_id": case_id,
        "model_id": "qwen3vl_8b",
        "arm_id": arm_id,
        "status": "PLANNED",
        "unit_record_sha256": "1" * 64,
        "source_registry_manifest_sha256": "2" * 64,
        "admission_record_sha256": "3" * 64,
        "run_ready_seal_sha256": "4" * 64,
        "frozen_capsule_sha256": "5" * 64,
        "arm_plan_sha256": "6" * 64,
        "action_gold_bundle_sha256": "7" * 64,
        "transformation_plan_sha256": "8" * 64,
        "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
        "provider_codec_manifest_sha256": "9" * 64,
        "parser_manifest_sha256": "a" * 64,
        "scorer_manifest_sha256": "b" * 64,
        "schedule_manifest_sha256": "c" * 64,
        "source_request_sha256": "d" * 64,
        "request_sha256": "e" * 64,
        "request_byte_count": 100,
        "non_history_projection_sha256": "f" * 64,
        "history_projection_sha256": "0" * 64,
        "repeat_index": 1,
        "block_index": 1,
        "arm_order_index": order.index(arm_id),
        "replay_seed": 1729,
        "arm_order_contract": "STRICT_FIVE_ARM_ROTATION_V1",
        "arm_order_salt": registry.ARM_ORDER_SALT,
        "arm_order_input_sha256": _sha(f"{registry.ARM_ORDER_SALT}|qwen3vl_8b|{case_id}"),
        "block_arm_order": order,
        "block_arm_order_sha256": registry.canonical_sha256(order),
        "allowed_deltas": ["REPLAY_SEED", "TRANSPORT_VOLATILES"],
        "retry_policy": _retry_policy(),
        "isolation": _isolation(),
    }


def test_run_schema_freezes_seed_repeat_and_arm_specific_deltas() -> None:
    validator = _schema_validator("run.schema.json")
    run = _run_record()
    assert not list(validator.iter_errors(run))
    registry.validate_run_record(run)
    started = {**run, "status": "STARTED"}
    assert list(validator.iter_errors(started))
    with pytest.raises(CausalReplayRegistryError, match="run plan status invalid"):
        registry.validate_run_record(started)
    rotated = deepcopy(run)
    rotated["block_arm_order"] = rotated["block_arm_order"][1:] + rotated["block_arm_order"][:1]
    rotated["block_arm_order_sha256"] = registry.canonical_sha256(rotated["block_arm_order"])
    rotated["arm_order_index"] = rotated["block_arm_order"].index(rotated["arm_id"])
    assert not list(validator.iter_errors(rotated))
    with pytest.raises(CausalReplayRegistryError, match="run block arm order mismatch"):
        registry.validate_run_record(rotated)
    bad_seed = {**run, "replay_seed": 7}
    assert list(validator.iter_errors(bad_seed))
    bad_delta = {
        **run,
        "allowed_deltas": [
            "REGISTERED_HISTORY_EDIT",
            "REPLAY_SEED",
            "TRANSPORT_VOLATILES",
        ],
    }
    assert list(validator.iter_errors(bad_delta))
    clean_original = {
        **run,
        "unit_kind": "CLEAN_CONTROL",
        "case_id": "g1control-0123456789abcdef01234567",
        "arm_order_contract": "CLEAN_TWO_ARM_BALANCED_V1",
        "arm_order_index": 0,
        "block_arm_order": ["ORIGINAL", "SHAM_BENIGN_EDIT"],
    }
    assert not list(validator.iter_errors(clean_original))
    clean_sham = {
        **clean_original,
        "arm_id": "SHAM_BENIGN_EDIT",
        "allowed_deltas": [
            "REGISTERED_HISTORY_EDIT",
            "REPLAY_SEED",
            "TRANSPORT_VOLATILES",
        ],
    }
    assert not list(validator.iter_errors(clean_sham))
    assert list(validator.iter_errors({**clean_sham, "arm_id": "MASK"}))


def test_outcome_schema_represents_missing_without_response() -> None:
    validator = _schema_validator("outcome.schema.json")
    transport = _content_ref("mobileworld.g1.transport-receipt/v1", digit="1")
    outcome = {
        "schema_version": "mobileworld.g1.causal-replay-outcome/v1",
        "record_type": "causal_replay_outcome",
        "protocol_version": registry.PROTOCOL_VERSION,
        "outcome_id": "g1outcome-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "run_id": "g1run-0123456789abcdef01234567",
        "unit_kind": "STRICT_MHR",
        "case_id": "g1case-0123456789abcdef01234567",
        "model_id": "qwen3vl_8b",
        "arm_id": "MASK",
        "run_record_sha256": "0" * 64,
        "unit_record_sha256": "1" * 64,
        "admission_record_sha256": "2" * 64,
        "run_ready_seal_sha256": "3" * 64,
        "frozen_capsule_sha256": "4" * 64,
        "arm_plan_sha256": "5" * 64,
        "action_gold_bundle_sha256": "6" * 64,
        "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
        "parser_manifest_sha256": "7" * 64,
        "scorer_manifest_sha256": "8" * 64,
        "schedule_manifest_sha256": "9" * 64,
        "request_sha256": "a" * 64,
        "status": "MISSING",
        "attempts": [
            {
                "attempt_index": index,
                "status": "FAILED",
                "request_sha256": "a" * 64,
                "request_byte_count": 100,
                "transport_receipt_ref": transport,
                "response_blob": None,
                "error": {"error_class": "TIMEOUT", "message_sha256": "b" * 64},
                "retry_decision": "STOP_EXHAUSTED" if index == 3 else "RETRY",
                "attempt_record_sha256": str(index) * 64,
            }
            for index in range(1, 4)
        ],
        "attempts_sha256": "c" * 64,
        "missing_reason": "PROVIDER_ATTEMPTS_EXHAUSTED",
        "response_blob": None,
        "parse_class": "MISSING",
        "parser_result": None,
        "parser_receipt_ref": None,
        "action_blob": None,
        "outcome_class": "MISSING",
        "classification_contract": "MISSING>ONE_VALID_ACTION_SCORE>ZERO_ACTION_REFUSAL>UNPARSEABLE/v1",
        "classification_receipt_ref": _content_ref(
            "mobileworld.g1.outcome-classification-receipt/v1", digit="d"
        ),
    }
    assert not list(validator.iter_errors(outcome))
    bad = {**outcome, "response_blob": {"relative_path": "x", "sha256": "4" * 64, "byte_count": 1}}
    assert list(validator.iter_errors(bad))
    response = _content_ref("mobileworld.g1.provider-response/v1", digit="e")
    action = _content_ref("mobileworld.g1.normalized-action/v1", digit="f")
    completed = {
        **outcome,
        "status": "COMPLETED",
        "attempts": [
            {
                "attempt_index": 1,
                "status": "COMPLETED",
                "request_sha256": "a" * 64,
                "request_byte_count": 100,
                "transport_receipt_ref": transport,
                "response_blob": response,
                "error": None,
                "retry_decision": "STOP_SUCCESS",
                "attempt_record_sha256": "1" * 64,
            }
        ],
        "missing_reason": None,
        "response_blob": response,
        "parse_class": "PARSEABLE_ACTION",
        "parser_result": {
            "legal_action_count": 1,
            "refusal_classifier_match": False,
            "matched_gold_predicate_ids": ["g1predicate-0123456789abcdef01234567"],
            "no_op_kind": None,
            "normalized_action_sha256": "f" * 64,
        },
        "parser_receipt_ref": _content_ref("mobileworld.g1.parser-receipt/v1", digit="2"),
        "action_blob": action,
        "outcome_class": "ACCEPTABLE",
    }
    assert not list(validator.iter_errors(completed))
    gold_allowed_wait = deepcopy(completed)
    gold_allowed_wait["parser_result"]["no_op_kind"] = "WAIT"
    assert not list(validator.iter_errors(gold_allowed_wait))
    missing_gold_wait = deepcopy(gold_allowed_wait)
    missing_gold_wait["parser_result"]["matched_gold_predicate_ids"] = []
    assert list(validator.iter_errors(missing_gold_wait))
    wrongly_classified_gold_wait = deepcopy(gold_allowed_wait)
    wrongly_classified_gold_wait["outcome_class"] = "NO_OP"
    assert list(validator.iter_errors(wrongly_classified_gold_wait))
    clean_completed = {
        **completed,
        "unit_kind": "CLEAN_CONTROL",
        "case_id": "g1control-0123456789abcdef01234567",
        "arm_id": "SHAM_BENIGN_EDIT",
    }
    assert not list(validator.iter_errors(clean_completed))
    assert list(validator.iter_errors({**clean_completed, "arm_id": "MASK"}))


def test_outcome_validator_cross_binds_run_and_attempt_hashes() -> None:
    run = _run_record()
    transport = _content_ref("mobileworld.g1.transport-receipt/v1", digit="1")
    attempts = []
    for index in range(1, 4):
        attempt = {
            "attempt_index": index,
            "status": "FAILED",
            "request_sha256": run["request_sha256"],
            "request_byte_count": run["request_byte_count"],
            "transport_receipt_ref": transport,
            "response_blob": None,
            "error": {"error_class": "TIMEOUT", "message_sha256": "a" * 64},
            "retry_decision": "STOP_EXHAUSTED" if index == 3 else "RETRY",
        }
        attempt["attempt_record_sha256"] = registry._attempt_record_hash(attempt)
        attempts.append(attempt)
    outcome = {
        "schema_version": "mobileworld.g1.causal-replay-outcome/v1",
        "record_type": "causal_replay_outcome",
        "protocol_version": registry.PROTOCOL_VERSION,
        "outcome_id": "g1outcome-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "run_id": run["run_id"],
        "unit_kind": run["unit_kind"],
        "case_id": run["case_id"],
        "model_id": run["model_id"],
        "arm_id": run["arm_id"],
        "run_record_sha256": registry.canonical_sha256(run),
        **{
            field: run[field]
            for field in (
                "unit_record_sha256",
                "admission_record_sha256",
                "run_ready_seal_sha256",
                "frozen_capsule_sha256",
                "arm_plan_sha256",
                "action_gold_bundle_sha256",
                "model_config_manifest_sha256",
                "parser_manifest_sha256",
                "scorer_manifest_sha256",
                "schedule_manifest_sha256",
                "request_sha256",
            )
        },
        "status": "MISSING",
        "attempts": attempts,
        "attempts_sha256": registry.canonical_sha256(attempts),
        "missing_reason": "PROVIDER_ATTEMPTS_EXHAUSTED",
        "response_blob": None,
        "parse_class": "MISSING",
        "parser_result": None,
        "parser_receipt_ref": None,
        "action_blob": None,
        "outcome_class": "MISSING",
        "classification_contract": "MISSING>ONE_VALID_ACTION_SCORE>ZERO_ACTION_REFUSAL>UNPARSEABLE/v1",
        "classification_receipt_ref": _content_ref(
            "mobileworld.g1.outcome-classification-receipt/v1", digit="d"
        ),
    }
    registry.validate_outcome_record(outcome, run_record=run)
    outcome["attempts"][0]["request_sha256"] = "f" * 64
    outcome["attempts"][0]["attempt_record_sha256"] = registry._attempt_record_hash(
        outcome["attempts"][0]
    )
    outcome["attempts_sha256"] = registry.canonical_sha256(outcome["attempts"])
    with pytest.raises(CausalReplayRegistryError, match="outcome attempt request mismatch"):
        registry.validate_outcome_record(outcome, run_record=run)

    response = _content_ref("mobileworld.g1.provider-response/v1", digit="e")
    action = _content_ref("mobileworld.g1.normalized-action/v1", digit="f")
    completed_attempt = {
        "attempt_index": 1,
        "status": "COMPLETED",
        "request_sha256": run["request_sha256"],
        "request_byte_count": run["request_byte_count"],
        "transport_receipt_ref": transport,
        "response_blob": response,
        "error": None,
        "retry_decision": "STOP_SUCCESS",
    }
    completed_attempt["attempt_record_sha256"] = registry._attempt_record_hash(completed_attempt)
    no_op = {
        **outcome,
        "status": "COMPLETED",
        "attempts": [completed_attempt],
        "attempts_sha256": registry.canonical_sha256([completed_attempt]),
        "missing_reason": None,
        "response_blob": response,
        "parse_class": "PARSEABLE_ACTION",
        "parser_result": {
            "legal_action_count": 1,
            "refusal_classifier_match": False,
            "matched_gold_predicate_ids": [],
            "no_op_kind": "WAIT",
            "normalized_action_sha256": "f" * 64,
        },
        "parser_receipt_ref": _content_ref("mobileworld.g1.parser-receipt/v1", digit="2"),
        "action_blob": action,
        "outcome_class": "NO_OP",
    }
    registry.validate_outcome_record(no_op, run_record=run)
    gold_allowed_wait = deepcopy(no_op)
    gold_allowed_wait["parser_result"]["matched_gold_predicate_ids"] = [
        "g1predicate-0123456789abcdef01234567"
    ]
    gold_allowed_wait["outcome_class"] = "ACCEPTABLE"
    registry.validate_outcome_record(gold_allowed_wait, run_record=run)
    missing_gold_wait = deepcopy(gold_allowed_wait)
    missing_gold_wait["parser_result"]["matched_gold_predicate_ids"] = []
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry.validate_outcome_record(missing_gold_wait, run_record=run)
    wrongly_classified_gold_wait = deepcopy(gold_allowed_wait)
    wrongly_classified_gold_wait["outcome_class"] = "NO_OP"
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry.validate_outcome_record(wrongly_classified_gold_wait, run_record=run)
    no_op["action_blob"] = {**action, "sha256": "e" * 64}
    with pytest.raises(CausalReplayRegistryError, match="normalized action hash mismatch"):
        registry.validate_outcome_record(no_op, run_record=run)


def _arm_target(*, target_kind: str = "FOCAL_MHR", digit: str = "1") -> dict:
    return {
        "ordinal": 0,
        "target_id": "g1span-0123456789abcdef01234567",
        "target_kind": target_kind,
        "source_candidate_ids": ["candidate-1"],
        "record_identity_sha256": digit * 64,
        "request_path": "payload.request_view.messages[1].content",
        "message_index": 1,
        "content_item_index": None,
        "message_role": "assistant",
        "content_item_kind": "SCALAR_TEXT",
        "representation_record_class": "MAI_RAW_ASSISTANT_MESSAGE",
        "record_sha256": "2" * 64,
        "record_codepoint_count": 20,
        "record_utf8_byte_count": 20,
        "char_start": 2,
        "char_end": 6,
        "utf8_byte_start": 2,
        "utf8_byte_end": 6,
        "span_sha256": "3" * 64,
        "location_bucket": {
            "message_role": "assistant",
            "content_item_kind": "SCALAR_TEXT",
            "representation_record_class": "MAI_RAW_ASSISTANT_MESSAGE",
            "relative_third": "LEADING",
        },
    }


def _arm_plan(*, arm_id: str = "MASK", unit_kind: str = "STRICT_MHR") -> dict:
    target = _arm_target()
    plan = {
        "schema_version": "mobileworld.g1.causal-replay-arm-plan/v1",
        "record_type": "causal_replay_arm_plan",
        "protocol_version": registry.PROTOCOL_VERSION,
        "arm_plan_id": "g1armplan-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "unit_kind": unit_kind,
        "case_id": (
            "g1case-0123456789abcdef01234567"
            if unit_kind == "STRICT_MHR"
            else "g1control-0123456789abcdef01234567"
        ),
        "model_id": "mai_ui_8b",
        "arm_id": arm_id,
        "operation": "DELETE",
        "unit_record_sha256": "4" * 64,
        "frozen_capsule_sha256": "5" * 64,
        "transformation_plan_sha256": "6" * 64,
        "action_gold_bundle_sha256": "7" * 64,
        "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
        "focal_target_set_sha256": "8" * 64,
        "oracle_target_set_sha256": "9" * 64,
        "selected_target_set_sha256": "8" * 64,
        "target_spans": [target],
        "insertions": [],
        "delimiter_repairs": [],
        "sham_match": None,
        "invariants": {
            "targets_ordered_unique_nonoverlapping": True,
            "selected_set_hash_verified": True,
            "mask_targets_equal_focal": True,
            "mask_correction_targets_equal_focal": True,
            "oracle_targets_equal_oracle_set": True,
            "oracle_is_focal_superset": True,
            "insertions_bind_deleted_targets": True,
            "delimiter_repairs_bind_source_syntax": True,
            "sham_binds_exactly_one_benign_and_one_focal": True,
            "sham_nonoverlapping_with_misleading_targets": True,
            "sham_token_counts_recomputed": True,
            "sham_location_buckets_recomputed": True,
            "only_original_and_sham_applicable": "NOT_APPLICABLE",
        },
    }
    if unit_kind == "CLEAN_CONTROL":
        plan["invariants"].update(
            mask_targets_equal_focal="NOT_APPLICABLE",
            mask_correction_targets_equal_focal="NOT_APPLICABLE",
            oracle_targets_equal_oracle_set="NOT_APPLICABLE",
            oracle_is_focal_superset="NOT_APPLICABLE",
            only_original_and_sham_applicable=True,
        )
    if arm_id == "ORIGINAL":
        plan.update(
            operation="NONE",
            selected_target_set_sha256=None,
            target_spans=[],
        )
    elif arm_id == "MASK_CORRECTION":
        plan.update(
            operation="DELETE_THEN_INSERT",
            insertions=[
                {
                    "target_id": target["target_id"],
                    "insertion_position": "DELETED_SPAN_START",
                    "correction_utf8_ref": _content_ref("mobileworld.g1.utf8-text/v1"),
                    "correction_sha256": "a" * 64,
                    "token_count": 2,
                    "utf8_byte_count": 4,
                    "codepoint_count": 4,
                }
            ],
        )
    elif arm_id == "ORACLE_CLEAN":
        plan.update(
            operation="DELETE_ALL_RELEVANT_MISLEADING_PREMISES",
            selected_target_set_sha256="9" * 64,
            target_spans=[{**target, "target_kind": "ORACLE_RELEVANT_MHR"}],
        )
    elif arm_id == "SHAM_BENIGN_EDIT":
        benign = {
            **target,
            "target_kind": "BENIGN_SHAM",
            "target_id": "g1span-abcdef0123456789abcdef01",
        }
        plan.update(
            selected_target_set_sha256="a" * 64,
            target_spans=[benign],
            sham_match={
                "matched_focal_target": target,
                "benign_target": benign,
                "tokenizer_binding": {
                    "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
                    "model_id": "mai_ui_8b",
                    "tokenizer_revision": "1" * 40,
                    "tokenizer_artifact_set_sha256": "b" * 64,
                    "counting_call": "tokenizer.encode(text, add_special_tokens=False)",
                    "add_special_tokens": False,
                },
                "focal_token_count": 4,
                "benign_token_count": 4,
                "absolute_token_difference": 0,
                "token_ratio_numerator": 4,
                "token_ratio_denominator": 4,
                "token_match_rule": "RATIO_80_TO_125_PERCENT_OR_ABSOLUTE_DIFFERENCE_AT_MOST_4",
                "focal_location_bucket": target["location_bucket"],
                "benign_location_bucket": benign["location_bucket"],
                "same_request_record": True,
                "focal_history_depth": 1,
                "benign_history_depth": 1,
                "history_depth_difference": 0,
                "same_record_unavailable_reviewed": False,
                "semantic_review_ledger_sha256": "c" * 64,
            },
        )
    return plan


def test_arm_schema_requires_exact_bound_targets_and_rejects_unknowns() -> None:
    validator = _schema_validator("arm.schema.json")
    plan = _arm_plan()
    assert not list(validator.iter_errors(plan))
    assert list(validator.iter_errors({**plan, "unexpected": True}))
    missing_identity = deepcopy(plan)
    del missing_identity["target_spans"][0]["record_identity_sha256"]
    assert list(validator.iter_errors(missing_identity))
    original = _arm_plan(arm_id="ORIGINAL")
    assert not list(validator.iter_errors(original))
    correction = _arm_plan(arm_id="MASK_CORRECTION")
    assert not list(validator.iter_errors(correction))
    assert list(validator.iter_errors({**correction, "insertions": []}))
    oracle = _arm_plan(arm_id="ORACLE_CLEAN")
    assert not list(validator.iter_errors(oracle))
    sham = _arm_plan(arm_id="SHAM_BENIGN_EDIT")
    assert not list(validator.iter_errors(sham))
    clean_original = _arm_plan(arm_id="ORIGINAL", unit_kind="CLEAN_CONTROL")
    clean_sham = _arm_plan(arm_id="SHAM_BENIGN_EDIT", unit_kind="CLEAN_CONTROL")
    assert not list(validator.iter_errors(clean_original))
    assert not list(validator.iter_errors(clean_sham))
    false_clean_invariant = deepcopy(clean_original)
    false_clean_invariant["invariants"]["oracle_is_focal_superset"] = True
    assert list(validator.iter_errors(false_clean_invariant))
    assert registry._expected_transformation_invariants("CLEAN_CONTROL") == {
        "targets_ordered_unique_nonoverlapping": True,
        "oracle_is_focal_superset": "NOT_APPLICABLE",
        "mask_targets_equal_focal": "NOT_APPLICABLE",
        "mask_correction_targets_equal_focal": "NOT_APPLICABLE",
        "oracle_targets_equal_oracle_set": "NOT_APPLICABLE",
        "sham_nonoverlapping_with_misleading_targets": True,
        "protected_spans_untouched": True,
        "future_evidence_leakage_zero": True,
        "only_original_and_sham_applicable": True,
    }
    assert list(
        validator.iter_errors(
            {**plan, "unit_kind": "CLEAN_CONTROL", "case_id": clean_original["case_id"]}
        )
    )


def _minimal_prepared_case() -> dict:
    binding = {
        "candidate_id": "candidate-1",
        "request_path": "payload.request_view.messages[1].content",
        "record_identity_sha256": "1" * 64,
        "container_sha256": "2" * 64,
        "char_start": 0,
        "char_end": 4,
        "utf8_byte_start": 0,
        "utf8_byte_end": 4,
        "span_sha256": "3" * 64,
        "edit_span_status": "G1_1_FROZEN",
        "focal_edit_spans": [
            {
                "char_start": 0,
                "char_end": 4,
                "utf8_byte_start": 0,
                "utf8_byte_end": 4,
                "span_sha256": "3" * 64,
            }
        ],
        "curation_envelope": None,
    }
    target_history = {
        **deepcopy(binding),
        "representation_type": "flat_progress",
        "transform_binding": "conclusion_span_offsets_plus_enclosing_step_span",
    }
    case = {
        "schema_version": registry.CASE_SCHEMA_VERSION,
        "record_type": "causal_replay_case",
        "case_id": "",
        "case_status": "CANDIDATE_FROZEN",
        "curated": True,
        "deployment_prediction": False,
        "source_key": "qwen",
        "study_role": "PRIMARY",
        "model_id": "qwen3vl_8b",
        "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
        "model_config_record_sha256": registry._MODEL_CONFIG_RECORD_SHA256["qwen3vl_8b"],
        "history_family": "FLAT_PROGRESS",
        "task": {"task_run_id": "task-run-1"},
        "decision": {"target_step": 2, "request_event_id": "request-event-1"},
        "frozen_capsule": {
            "model_config": {
                "manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
                "record_sha256": registry._MODEL_CONFIG_RECORD_SHA256["qwen3vl_8b"],
            },
            "request_view_sha256": "b" * 64,
            "current_gui_blob": {"digest": "c" * 64},
            "sdk_arguments_snapshot_blob": {"digest": "d" * 64},
            "original_action": {"parsed_action": {"action_type": "click"}},
        },
        "target_histories": [target_history],
        "eligibility_only_refs": {
            "members": [
                {
                    "candidate_id": "candidate-1",
                    "candidate_sha256": "4" * 64,
                }
            ]
        },
        "action_gold_refs": {
            "accepted_next_action_set_ref": None,
            "review_ledger_ref": None,
            "curator_identity_ref": None,
            "curator_view": "TASK_AND_PRE_CALL_GUI_ONLY",
            "allowed_evidence_roles": [
                "ask_user_response",
                "target_pre",
                "task_instruction",
                "tool_response",
            ],
            "forbidden_evidence_roles": sorted(registry._ACTION_GOLD_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "transformation_refs": {
            "transformation_plan_ref": None,
            "review_ledger_ref": None,
            "mask_correction_ref": None,
            "oracle_clean_ref": None,
            "sham_benign_edit_ref": None,
            "curator_identity_ref": None,
            "curator_view": "SOURCE_HISTORY_AND_PRE_CALL_GUI_NO_TARGET_DECISION",
            "audited_exposure_bindings": [binding],
            "focal_target_set": [],
            "focal_target_set_status": "G1_6_PENDING",
            "oracle_target_set_ref": None,
            "forbidden_evidence_roles": sorted(registry._TRANSFORMATION_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "arm_eligibility": registry._arm_eligibility(case_kind="STRICT_MHR"),
    }
    identity = registry._paired_unit_hash(
        "strict-mhr-case",
        model_config_manifest_sha256=case["model_config_manifest_sha256"],
        model_config_record_sha256=case["model_config_record_sha256"],
        task_run_id=case["task"]["task_run_id"],
        target_step=case["decision"]["target_step"],
        request_event_id=case["decision"]["request_event_id"],
        request_view_sha256=case["frozen_capsule"]["request_view_sha256"],
        current_gui_sha256=case["frozen_capsule"]["current_gui_blob"]["digest"],
        sdk_arguments_snapshot_sha256=case["frozen_capsule"]["sdk_arguments_snapshot_blob"][
            "digest"
        ],
    )
    case["case_id"] = f"g1case-{identity[:24]}"
    return case


def test_pre_gold_model_role_history_and_span_contract_is_fail_closed() -> None:
    qwen = _minimal_prepared_case()
    registry._validate_pre_gold_model_assignment(qwen)
    for field, wrong in (("study_role", "REPLICATION"), ("history_family", "RAW_REPLAY")):
        forged = deepcopy(qwen)
        forged[field] = wrong
        with pytest.raises(CausalReplayRegistryError, match="model role history mismatch"):
            registry._validate_pre_gold_model_assignment(forged)

    mai = {
        "model_id": "mai_ui_8b",
        "study_role": "REPLICATION",
        "history_family": "RAW_REPLAY",
        "target_histories": [
            {
                "edit_span_status": "G1_6_PENDING",
                "focal_edit_spans": [],
                "representation_type": "raw_replay",
                "transform_binding": "raw_record_hash_with_g1_6_pending_edit_span",
            }
        ],
    }
    registry._validate_pre_gold_model_assignment(mai)
    frozen_raw = deepcopy(mai)
    frozen_raw["target_histories"][0].update(
        edit_span_status="G1_1_FROZEN",
        focal_edit_spans=[{"span_sha256": "1" * 64}],
    )
    with pytest.raises(CausalReplayRegistryError, match="history span contract mismatch"):
        registry._validate_pre_gold_model_assignment(frozen_raw)


def test_pre_gold_arm_eligibility_is_exact_ordered_and_case_specific() -> None:
    case = _minimal_prepared_case()
    registry._validate_pre_gold_arm_eligibility(case)
    duplicated = deepcopy(case)
    duplicated["arm_eligibility"][1] = deepcopy(duplicated["arm_eligibility"][0])
    with pytest.raises(CausalReplayRegistryError, match="arm eligibility mismatch"):
        registry._validate_pre_gold_arm_eligibility(duplicated)

    clean = {
        "record_type": "causal_replay_clean_control",
        "arm_eligibility": registry._arm_eligibility(case_kind="CLEAN_CONTROL"),
    }
    registry._validate_pre_gold_arm_eligibility(clean)
    forged_clean = deepcopy(clean)
    forged_clean["arm_eligibility"][1].update(
        structurally_applicable=True,
        reason="G1_6_GOLD_AND_TRANSFORMATION_PENDING",
    )
    with pytest.raises(CausalReplayRegistryError, match="arm eligibility mismatch"):
        registry._validate_pre_gold_arm_eligibility(forged_clean)


def test_pre_gold_validation_mechanically_rejects_future_projection() -> None:
    case = _minimal_prepared_case()
    ledger = [
        {
            "curated": True,
            "deployment_prediction": False,
            "source_key": "qwen",
            "model_id": "qwen3vl_8b",
            "model_config_manifest_sha256": registry.MODEL_CONFIG_MANIFEST_SHA256,
            "model_config_record_sha256": registry._MODEL_CONFIG_RECORD_SHA256["qwen3vl_8b"],
            "task_run_id": "task-run-1",
            "target_step": 2,
            "candidate_id": "candidate-1",
            "candidate_sha256": "4" * 64,
            "unit_kind": "STRICT_MHR",
            "unit_id": case["case_id"],
            "disposition": "CANDIDATE_FROZEN",
        }
    ]
    validation = registry._validate_prepared(
        [case],
        [],
        ledger,
        {"curated": True, "deployment_prediction": False},
    )
    assert validation["checks"]["zero_forbidden_post_target_refs"] is True
    assert validation["checks"]["pending_evidence_channels_valid"] is True
    assert validation["pre_gold_status"]["pre_gold_future_leakage_case_count"] == 0
    assert validation["counts"]["treatment_response_count"] == 0

    case["frozen_capsule"]["target_post"] = {"digest": "2" * 64}
    with pytest.raises(CausalReplayRegistryError, match="prepared registry invalid"):
        registry._validate_prepared(
            [case],
            [],
            ledger,
            {"curated": True, "deployment_prediction": False},
        )


def test_pending_channel_allowlists_are_fail_closed() -> None:
    case = _minimal_prepared_case()
    assert registry._pending_evidence_channels_valid(case)
    case["action_gold_refs"]["allowed_evidence_roles"].append("target_action")
    assert not registry._pending_evidence_channels_valid(case)


def _curation_evidence_ref(role: str, digit: str) -> dict:
    role_contract = {
        "task_instruction": (None, None, 0, "task.instruction"),
        "target_pre": (
            "target-pre",
            3,
            2,
            "payload.observation.screenshot.pixel_blob",
        ),
        "source_history": (
            "target-request",
            4,
            2,
            "payload.request_view.messages",
        ),
    }
    event_id, event_seq, observed_step, projection_path = role_contract[role]
    return {
        "ref_id": f"g1evidence-{digit * 24}",
        "evidence_role": role,
        "relative_path": f"evidence/{digit}.json",
        "sha256": digit * 64,
        "byte_count": 1,
        "artifact_schema_version": "mobileworld.g1.curation-evidence/v1",
        "projection_path": projection_path,
        "event_id": event_id,
        "event_seq": event_seq,
        "observed_step": observed_step,
    }


def _curation_input_manifest(channel: str, evidence_refs: list[dict]) -> dict:
    return {
        "schema_version": "mobileworld.g1.curation-input-manifest/v1",
        "record_type": "causal_replay_curation_input_manifest",
        "protocol_version": registry.PROTOCOL_VERSION,
        "input_manifest_id": "g1curationinput-0123456789abcdef01234567",
        "curated": True,
        "deployment_prediction": False,
        "channel": channel,
        "unit_ref": {
            "unit_kind": "STRICT_MHR",
            "unit_id": "g1case-0123456789abcdef01234567",
            "unit_record_sha256": "1" * 64,
            "source_registry_manifest_sha256": "2" * 64,
            "frozen_capsule_sha256": "3" * 64,
            "request_view_sha256": "4" * 64,
            "current_gui_sha256": "5" * 64,
            "task_instruction_sha256": "6" * 64,
            "task_parameters_sha256": "7" * 64,
            "request_cutoff_event_id": "target-request",
            "request_cutoff_event_seq": 4,
            "target_step": 2,
            "model_id": "qwen3vl_8b",
            "history_family": "FLAT_PROGRESS",
        },
        "request_cutoff": {
            "event_id": "target-request",
            "event_seq": 4,
            "target_step": 2,
        },
        "evidence_refs": evidence_refs,
        "evidence_set_sha256": registry.canonical_sha256(evidence_refs),
        "forbidden_evidence_roles": sorted(
            registry._ACTION_GOLD_FORBIDDEN
            if channel == "ACTION_GOLD"
            else registry._TRANSFORMATION_FORBIDDEN
        ),
        "future_evidence_leakage_zero": True,
    }


def test_curation_input_requires_complete_channel_specific_evidence() -> None:
    target_pre = _curation_evidence_ref("target_pre", "1")
    instruction = _curation_evidence_ref("task_instruction", "2")
    history = _curation_evidence_ref("source_history", "3")

    gold = _curation_input_manifest("ACTION_GOLD", [target_pre, instruction])
    registry._validate_static_schema("curation_input_manifest.schema.json", gold)
    registry._validate_curation_evidence_role_completeness("ACTION_GOLD", gold["evidence_refs"])
    for missing_role, incomplete in (
        ("task_instruction", [target_pre]),
        ("target_pre", [instruction]),
    ):
        invalid = _curation_input_manifest("ACTION_GOLD", incomplete)
        with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
            registry._validate_static_schema("curation_input_manifest.schema.json", invalid)
        with pytest.raises(
            CausalReplayRegistryError,
            match="curation input required evidence roles missing",
        ) as error:
            registry._validate_curation_evidence_role_completeness("ACTION_GOLD", incomplete)
        assert error.value.context["missing"] == [missing_role]

    transformation = _curation_input_manifest("TRANSFORMATION", [history])
    registry._validate_static_schema("curation_input_manifest.schema.json", transformation)
    registry._validate_curation_evidence_role_completeness(
        "TRANSFORMATION", transformation["evidence_refs"]
    )
    missing_history = _curation_input_manifest("TRANSFORMATION", [target_pre])
    with pytest.raises(CausalReplayRegistryError, match="g1 schema validation failed"):
        registry._validate_static_schema("curation_input_manifest.schema.json", missing_history)
    with pytest.raises(
        CausalReplayRegistryError,
        match="curation input required evidence roles missing",
    ):
        registry._validate_curation_evidence_role_completeness(
            "TRANSFORMATION", missing_history["evidence_refs"]
        )

    schema_root = Path(__file__).parents[3] / "mobileworld_audit_handoff/schemas/g1"
    gold_evidence_schema = json.loads((schema_root / "action_gold_bundle.schema.json").read_text())[
        "properties"
    ]["evidence_refs"]
    transformation_evidence_schema = json.loads(
        (schema_root / "transformation_plan.schema.json").read_text()
    )["properties"]["evidence_refs"]
    assert len(gold_evidence_schema["allOf"]) == 2
    assert transformation_evidence_schema["contains"]["properties"]["evidence_role"] == {
        "const": "source_history"
    }


def test_focal_targets_require_nonempty_exact_frozen_candidate_assignment() -> None:
    frozen = {"candidate-1": {}, "candidate-2": {}}
    valid = {
        "target-1": ({"source_candidate_ids": ["candidate-1"]}, "first"),
        "target-2": ({"source_candidate_ids": ["candidate-2"]}, "second"),
    }
    registry._validate_focal_candidate_assignments(valid, frozen)

    forged_extra = {
        **valid,
        "target-forged": ({"source_candidate_ids": []}, "unbound semantic span"),
    }
    with pytest.raises(CausalReplayRegistryError, match="focal target source candidates empty"):
        registry._validate_focal_candidate_assignments(forged_extra, frozen)

    duplicated = {
        "target-1": ({"source_candidate_ids": ["candidate-1"]}, "first"),
        "target-2": (
            {"source_candidate_ids": ["candidate-1", "candidate-2"]},
            "second",
        ),
    }
    with pytest.raises(CausalReplayRegistryError, match="not exactly once"):
        registry._validate_focal_candidate_assignments(duplicated, frozen)

    unknown = {
        "target-1": ({"source_candidate_ids": ["candidate-1"]}, "first"),
        "target-2": ({"source_candidate_ids": ["candidate-unknown"]}, "second"),
    }
    with pytest.raises(CausalReplayRegistryError, match="not frozen"):
        registry._validate_focal_candidate_assignments(unknown, frozen)

    focal_items = json.loads(
        (
            Path(__file__).parents[3]
            / "mobileworld_audit_handoff/schemas/g1/transformation_plan.schema.json"
        ).read_text()
    )["properties"]["focal_target_set"]["items"]
    assert {"properties": {"source_candidate_ids": {"minItems": 1}}} in focal_items["allOf"]


def _write_curation_evidence(
    root: Path,
    *,
    projection: object,
    relative_path: str = "evidence/target-pre.json",
) -> dict:
    artifact = {
        "schema_version": "mobileworld.g1.curation-evidence/v1",
        "record_type": "causal_replay_curation_evidence",
        "protocol_version": registry.PROTOCOL_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "ref_id": "g1evidence-0123456789abcdef01234567",
        "evidence_role": "target_pre",
        "source_event_id": "target-pre",
        "source_event_seq": 3,
        "source_event_type": "step_started",
        "observed_step": 2,
        "projection_path": "payload.observation.screenshot.pixel_blob",
        "projection": projection,
        "projection_sha256": registry.canonical_sha256(projection),
        "visibility_proof": {
            "visibility_contract": "model-visible-request-projection/v1",
            "model_visible_at_or_before_request": True,
            "target_request_event_id": "target-request",
            "target_request_event_seq": 4,
            "request_locator": {
                "locator_kind": "IMAGE_CONTENT_BLOB",
                "message_index": 0,
                "content_item_index": 0,
                "field_path": "content.image_url.url.$externalized_data_url.content_blob",
                "char_start": None,
                "char_end": None,
                "utf8_byte_start": None,
                "utf8_byte_end": None,
                "span_sha256": None,
            },
        },
    }
    data = registry.canonical_json_bytes(artifact)
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "ref_id": artifact["ref_id"],
        "evidence_role": artifact["evidence_role"],
        "relative_path": relative_path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "artifact_schema_version": artifact["schema_version"],
        "projection_path": artifact["projection_path"],
        "event_id": artifact["source_event_id"],
        "event_seq": artifact["source_event_seq"],
        "observed_step": artifact["observed_step"],
    }


def test_curation_evidence_rejects_post_target_bytes_with_pre_event_locator(
    tmp_path: Path,
) -> None:
    source_base = tmp_path / "source"
    evidence_root = tmp_path / "curation"
    stream_path = source_base / "run/task.jsonl"
    stream_path.parent.mkdir(parents=True)
    evidence_root.mkdir()
    current = {
        "screenshot": {
            "pixel_blob": {
                "digest": "a" * 64,
                "byte_count": 10,
                "media_type": "image/png",
            }
        },
        "accessibility": {"nodes": ["before-target"]},
    }
    current_blob = current["screenshot"]["pixel_blob"]
    post_target = {
        "execution_result": {
            "task_ended": True,
            "outcome": "failure",
            "target_post": {"secret": "downstream"},
        }
    }
    events = [
        {
            "event_id": "task-start",
            "event_type": "task_started",
            "seq": 0,
            "payload": {"task_goal": "open settings"},
        },
        {
            "event_id": "target-pre",
            "event_type": "step_started",
            "seq": 3,
            "payload": {"step_id": "step-2", "step_index": 2, "observation": current},
        },
        {
            "event_id": "target-request",
            "event_type": "model_request",
            "seq": 4,
            "payload": {
                "step_id": "step-2",
                "step_index": 2,
                "request_view": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": {
                                            "$externalized_data_url": {"content_blob": current_blob}
                                        }
                                    },
                                }
                            ],
                        }
                    ]
                },
            },
        },
        {
            "event_id": "target-post",
            "event_type": "transition_completed",
            "seq": 5,
            "payload": {"step_id": "step-2", "step_index": 2, **post_target},
        },
    ]
    stream_data = b"".join(registry.canonical_json_bytes(event) for event in events)
    stream_path.write_bytes(stream_data)
    frozen = {
        "task": {"task_instruction_sha256": _sha("open settings")},
        "decision": {
            "target_step": 2,
            "request_event_id": "target-request",
            "request_cutoff": {
                "event_id": "target-request",
                "event_seq": 4,
                "target_step": 2,
            },
        },
        "frozen_capsule": {
            "source_locator": {
                "source_relative_run_path": "run",
                "task_stream_relative_path": "task.jsonl",
                "task_stream_sha256": hashlib.sha256(stream_data).hexdigest(),
            },
            "current_gui_blob": {"digest": "a" * 64},
        },
    }
    valid_ref = _write_curation_evidence(evidence_root, projection=current_blob)
    registry._validate_evidence_refs(
        evidence_root,
        source_base,
        [valid_ref],
        frozen_record=frozen,
        allowed_roles=frozenset({"target_pre"}),
    )

    hidden_ref = _write_curation_evidence(
        evidence_root,
        projection={**current_blob, "innocent_name": "private generator target"},
        relative_path="evidence/hidden-private-field.json",
    )
    with pytest.raises(CausalReplayRegistryError, match="curation evidence projection mismatch"):
        registry._validate_evidence_refs(
            evidence_root,
            source_base,
            [hidden_ref],
            frozen_record=frozen,
            allowed_roles=frozenset({"target_pre"}),
        )

    forged_ref = _write_curation_evidence(
        evidence_root,
        projection=post_target["execution_result"],
        relative_path="evidence/forged-pre.json",
    )
    with pytest.raises(CausalReplayRegistryError, match="curation evidence projection mismatch"):
        registry._validate_evidence_refs(
            evidence_root,
            source_base,
            [forged_ref],
            frozen_record=frozen,
            allowed_roles=frozenset({"target_pre"}),
        )


def test_task_instruction_visibility_requires_exact_request_text_span() -> None:
    instruction = "open settings"
    text = f"Task: {instruction}\nUse the current screenshot."
    start = text.index(instruction)
    end = start + len(instruction)
    artifact = {
        "evidence_role": "task_instruction",
        "visibility_proof": {
            "visibility_contract": "model-visible-request-projection/v1",
            "model_visible_at_or_before_request": True,
            "target_request_event_id": "request-1",
            "target_request_event_seq": 9,
            "request_locator": {
                "locator_kind": "TEXT_SPAN",
                "message_index": 0,
                "content_item_index": 0,
                "field_path": "content.text",
                "char_start": start,
                "char_end": end,
                "utf8_byte_start": len(text[:start].encode()),
                "utf8_byte_end": len(text[:end].encode()),
                "span_sha256": _sha(instruction),
            },
        },
    }
    request = {
        "event_id": "request-1",
        "seq": 9,
        "payload": {
            "request_view": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]
            }
        },
    }
    registry._validate_model_visible_projection(
        artifact,
        target_request=request,
        expected_projection=instruction,
    )
    artifact["visibility_proof"]["request_locator"]["char_start"] += 1
    with pytest.raises(CausalReplayRegistryError, match="visibility text span"):
        registry._validate_model_visible_projection(
            artifact,
            target_request=request,
            expected_projection=instruction,
        )


def test_outcome_blind_stream_reader_stops_before_post_target_bytes(tmp_path: Path) -> None:
    prefix_event = {"event_id": "decision-1", "event_type": "agent_decision", "payload": {}}
    data = registry.canonical_json_bytes(prefix_event) + b'{"outcome":"hidden",BROKEN}\n'
    records = registry._load_jsonl_prefix_through_event(
        data,
        tmp_path / "events.jsonl",
        stop_event_id="decision-1",
    )
    assert records == [prefix_event]


def test_exclusion_reasons_are_from_closed_protocol_vocabulary() -> None:
    gates = {
        "strict_mhr": {
            "coverage_complete": True,
            "actual_exposure": True,
            "provenance_exact_or_high": False,
            "explicit_use": False,
            "validity_refuted_or_stale": False,
            "low_state_confound": True,
        },
        "clean_control": {},
    }
    reasons = registry._exclusion_reasons(gates)
    assert reasons == ["NOT_REFUTED_OR_STALE", "NO_EXPLICIT_UPTAKE", "PROVENANCE_BELOW_HIGH"]
    assert set(reasons) <= registry.LEDGER_REASON_CODES


def test_arm_catalog_has_frozen_delete_and_schedule_contract() -> None:
    catalog = registry._arm_catalog()
    validator = _schema_validator("arm_catalog.schema.json")
    assert not list(validator.iter_errors(catalog))
    arms = {arm["arm_id"]: arm for arm in catalog["arms"]}
    assert arms["MASK"]["operation"] == "DELETE"
    assert arms["SHAM_BENIGN_EDIT"]["operation"] == "DELETE"
    assert set(arms["SHAM_BENIGN_EDIT"]["case_kinds"]) == {"STRICT_MHR", "CLEAN_CONTROL"}
    assert arms["ORACLE_CLEAN"]["operation"] == "DELETE_ALL_RELEVANT_MISLEADING_PREMISES"
    assert catalog["schedule"]["hash_salt"] == "mobileworld-g1-arm-order-v1-20260826"
    assert catalog["schedule"]["direction_rule"] == "+1 if digest[1] % 2 == 0 else -1"
    schedules = catalog["schedule"]["unit_kind_schedules"]
    assert schedules["STRICT_MHR"] == {
        "unit_id_field": "case_id",
        "base_arms": list(registry.ARM_IDS),
        "arm_count": 5,
        "base_rotation_modulus": 5,
    }
    assert schedules["CLEAN_CONTROL"] == {
        "unit_id_field": "control_id",
        "base_arms": ["ORIGINAL", "SHAM_BENIGN_EDIT"],
        "arm_count": 2,
        "base_rotation_modulus": 2,
    }
    duplicate_arm = deepcopy(catalog)
    duplicate_arm["arms"][1] = deepcopy(duplicate_arm["arms"][0])
    assert list(validator.iter_errors(duplicate_arm))


def test_arm_order_is_deterministic_and_position_balanced() -> None:
    case_id = "g1case-0123456789abcdef01234567"
    orders = [
        registry.arm_order(model_id="qwen3vl_8b", case_id=case_id, block_index=block)
        for block in range(1, 7)
    ]
    assert orders == [
        registry.arm_order(model_id="qwen3vl_8b", case_id=case_id, block_index=block)
        for block in range(1, 7)
    ]
    assert all(set(order) == set(registry.ARM_IDS) for order in orders)
    for arm in registry.ARM_IDS:
        counts = [sum(order[position] == arm for order in orders) for position in range(5)]
        assert max(counts) - min(counts) <= 1


def test_arm_order_fixed_odd_direction_vector() -> None:
    case_id = "g1case-000000000000000000000002"
    exact_input = f"{registry.ARM_ORDER_SALT}|qwen3vl_8b|{case_id}".encode()
    assert hashlib.sha256(exact_input).hexdigest() == (
        "890d19d80035c00e42f0e7cf763a91c4006c7d395dcc1b5ff413a2ab75a72973"
    )
    assert registry.arm_order(model_id="qwen3vl_8b", case_id=case_id, block_index=1) == (
        "MASK_CORRECTION",
        "ORACLE_CLEAN",
        "SHAM_BENIGN_EDIT",
        "ORIGINAL",
        "MASK",
    )
    assert registry.arm_order(model_id="qwen3vl_8b", case_id=case_id, block_index=2) == (
        "MASK",
        "MASK_CORRECTION",
        "ORACLE_CLEAN",
        "SHAM_BENIGN_EDIT",
        "ORIGINAL",
    )
