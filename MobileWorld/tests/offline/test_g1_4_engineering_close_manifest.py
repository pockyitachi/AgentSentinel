from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parents[3]
SCRIPT = REPO_ROOT / "MobileWorld/scripts/verify_g1_4_engineering_close_manifest.py"
SPEC = importlib.util.spec_from_file_location("verify_g1_4_close_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def _runner() -> Any:
    return verify._load_runner(REPO_ROOT)


def _fixture_calls() -> list[dict[str, Any]]:
    return verify._load_fixture(REPO_ROOT)[1]


def _response(model_id: str, *, marker: str = "same") -> bytes:
    tool_call = '{"name":"mobile_use","arguments":{"action":"wait","time":1}}'
    if model_id == "qwen3vl_8b":
        content = f"Thought: {marker}\nAction: wait\n<tool_call>{tool_call}</tool_call>"
    else:
        content = f"<thinking>{marker}</thinking><tool_call>{tool_call}</tool_call>"
    return json.dumps(
        {"choices": [{"message": {"content": content}}]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _build_events() -> tuple[list[dict[str, Any]], Any, list[dict[str, Any]]]:
    runner = _runner()
    calls = _fixture_calls()
    parsers = runner._load_host_parsers()
    events: list[dict[str, Any]] = [
        {
            "call_count": 22,
            "event": "run_started",
            "gpu_index": 4,
            "host": "127.0.0.1",
            "port": 18007,
            "runner_python": (
                "/shared/linqiang/agent_monitor/AgentSentinel/MobileWorld/.venv/bin/python"
            ),
        }
    ]
    for model_index, model in enumerate(runner.MODELS):
        events.extend(
            [
                {"event": "server_started", "model_id": model.model_id, "pid": 1001 + model_index},
                {"event": "server_ready", "model_id": model.model_id},
            ]
        )
        for fixture_call in [call for call in calls if call["model_id"] == model.model_id]:
            raw = _response(model.model_id)
            response_sha = hashlib.sha256(raw).hexdigest()
            content, parser_output = runner._validate_response(model.model_id, 200, raw, parsers)
            request = dict(fixture_call["application_request"])
            request["seed"] = fixture_call["seed"]
            events.extend(
                [
                    {
                        "event": "response_received",
                        "call_id": fixture_call["call_id"],
                        "model_id": model.model_id,
                        "http_status": 200,
                        "response_byte_count": len(raw),
                        "response_sha256": response_sha,
                        "response_body_base64": base64.b64encode(raw).decode(),
                    },
                    {
                        "event": "call_succeeded",
                        "call_id": fixture_call["call_id"],
                        "model_id": model.model_id,
                        "phase": fixture_call["phase"],
                        "seed": fixture_call["seed"],
                        "repeat": fixture_call["repeat"],
                        "arm": fixture_call["arm"],
                        "http_status": 200,
                        "request_sha256": runner._canonical_sha256(request),
                        "response_sha256": response_sha,
                        "content": content,
                        "host_parser_output": parser_output,
                        "generated_action_executed": False,
                    },
                ]
            )
        events.append(
            {"event": "server_stopped", "model_id": model.model_id, "result": "session_terminated"}
        )
    events.append({"event": "run_completed", "successful_call_count": 22})
    for sequence, event in enumerate(events):
        event["sequence"] = sequence
    return events, runner, calls


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(verify._canonical_json(event) for event in events))


def _assert_run_rejected(tmp_path: Path, events: list[dict[str, Any]]) -> None:
    path = tmp_path / "run.jsonl"
    _write_events(path, events)
    _, runner, calls = _build_events()
    with pytest.raises(verify.VerificationError):
        verify._verify_run(path, calls, runner)


def test_synthetic_positive_run_closes_all_22_calls(tmp_path: Path) -> None:
    events, runner, calls = _build_events()
    path = tmp_path / "run.jsonl"
    _write_events(path, events)
    assert verify._verify_run(path, calls, runner) == {
        "qwen_root_pid": 1001,
        "mai_root_pid": 1002,
    }


@pytest.mark.parametrize(
    ("event_index", "key", "value"),
    [
        (4, "call_id", "wrong-call"),
        (4, "seed", 999),
        (3, "http_status", 500),
        (4, "request_sha256", "0" * 64),
        (4, "host_parser_output", {"wrong": True}),
        (4, "generated_action_executed", True),
    ],
)
def test_run_rejects_call_metadata_status_request_parser_and_action_drift(
    tmp_path: Path,
    event_index: int,
    key: str,
    value: object,
) -> None:
    events, _, _ = _build_events()
    events[event_index][key] = value
    _assert_run_rejected(tmp_path, events)


def test_run_rejects_non_adjacent_response_and_call(tmp_path: Path) -> None:
    events, _, _ = _build_events()
    events[3], events[4] = events[4], events[3]
    events[3]["sequence"] = 3
    events[4]["sequence"] = 4
    _assert_run_rejected(tmp_path, events)


def test_run_rejects_same_seed_repeat_content_drift(tmp_path: Path) -> None:
    events, runner, calls = _build_events()
    parsers = runner._load_host_parsers()
    raw = _response("qwen3vl_8b", marker="different")
    response_sha = hashlib.sha256(raw).hexdigest()
    content, parser_output = runner._validate_response("qwen3vl_8b", 200, raw, parsers)
    events[5].update(
        response_body_base64=base64.b64encode(raw).decode(),
        response_byte_count=len(raw),
        response_sha256=response_sha,
    )
    events[6].update(
        content=content,
        host_parser_output=parser_output,
        response_sha256=response_sha,
    )
    path = tmp_path / "run.jsonl"
    _write_events(path, events)
    with pytest.raises(verify.VerificationError, match="same-seed repeat mismatch"):
        verify._verify_run(path, calls, runner)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_strict_json_rejects_nonfinite_constants(constant: bytes) -> None:
    with pytest.raises(verify.VerificationError, match="non-finite JSON"):
        verify._strict_json_loads(b'{"value":' + constant + b"}")


def test_strict_json_rejects_duplicate_keys() -> None:
    with pytest.raises(verify.VerificationError, match="duplicate JSON key"):
        verify._strict_json_loads(b'{"value":1,"value":2}')


def _log_text(*, root_pid: int = 1001, engine_pid: int = 2001, status: int = 200) -> str:
    model = {
        "snapshot": "/models/qwen",
        "served_name": "Qwen",
    }
    lines = [
        f"(APIServer pid={root_pid}) model {model['snapshot']} served {model['served_name']}",
        f"(EngineCore pid={engine_pid}) Initializing a V1 LLM engine (v0.19.1)",
    ]
    lines.extend(
        f'(APIServer pid={root_pid}) "POST /v1/chat/completions HTTP/1.1" {status} '
        + ("OK" if status == 200 else "ERROR")
        for _ in range(11)
    )
    lines.append(f"(APIServer pid={root_pid}) Application shutdown complete.")
    return "\n".join(lines) + "\n"


def test_server_log_positive_census(tmp_path: Path) -> None:
    path = tmp_path / "qwen.log"
    path.write_text(_log_text())
    verify._verify_log(
        path,
        model={
            "snapshot": "/models/qwen",
            "served_name": "Qwen",
            "engine_pid": 2001,
        },
        root_pid=1001,
    )


@pytest.mark.parametrize(
    "text",
    [
        _log_text().replace(" 200 OK", " 500 ERROR", 1),
        _log_text().replace("Application shutdown complete.\n", ""),
        _log_text().replace("EngineCore pid=2001", "EngineCore pid=9999"),
    ],
)
def test_server_log_rejects_status_shutdown_and_pid_drift(tmp_path: Path, text: str) -> None:
    path = tmp_path / "qwen.log"
    path.write_text(text)
    with pytest.raises(verify.VerificationError):
        verify._verify_log(
            path,
            model={
                "snapshot": "/models/qwen",
                "served_name": "Qwen",
                "engine_pid": 2001,
            },
            root_pid=1001,
        )


def test_source_binding_roles_are_exact_and_unique() -> None:
    assert len(verify.EXPECTED_SOURCE_BINDINGS) == 14
    assert len(set(verify.EXPECTED_SOURCE_BINDINGS.values())) == 14


def test_environment_override_surface_is_exact() -> None:
    runner_raw = (REPO_ROOT / verify.RUNNER_RELATIVE_PATH).read_bytes()
    assert verify._literal_environment_keys(runner_raw) == set(
        verify.EXPECTED_ENVIRONMENT_OVERRIDES
    )


def test_manifest_and_schema_paths_are_pinned_before_read(tmp_path: Path) -> None:
    with pytest.raises(verify.VerificationError, match="manifest path"):
        verify.verify(tmp_path / "manifest.json", tmp_path / "schema.json")


def test_no_follow_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(verify.VerificationError, match="without following links"):
        verify._read_regular_nofollow(link)


def test_content_addressed_artifact_expectations_are_exact() -> None:
    assert set(verify.EXPECTED_ARTIFACTS) == {
        "run.jsonl",
        "qwen.server.log",
        "mai.server.log",
    }
    assert all(len(value["sha256"]) == 64 for value in verify.EXPECTED_ARTIFACTS.values())


def _artifact_manifest() -> dict[str, Any]:
    evidence_root = Path(
        "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/"
        "g1_4_engineering_close_20260831/evidence"
    )
    artifacts = []
    for logical_name, expected in verify.EXPECTED_ARTIFACTS.items():
        artifacts.append(
            {
                "logical_name": logical_name,
                "original_path": expected["original_path"],
                "original_mode": "0664",
                "original_physically_immutable": False,
                "sealed_object_path": str(
                    evidence_root
                    / "objects"
                    / "sha256"
                    / expected["sha256"][:2]
                    / expected["sha256"]
                ),
                "sha256": expected["sha256"],
                "byte_count": expected["byte_count"],
                "uid": 1035,
                "gid": 1035,
                "mode": "0400",
                "nlink": 1,
            }
        )
    return {"evidence_root": str(evidence_root), "artifacts": artifacts}


def test_artifact_declarations_accept_exact_three_roles_and_paths() -> None:
    artifacts, originals = verify._verify_artifact_declarations(_artifact_manifest())
    assert set(artifacts) == set(verify.EXPECTED_ARTIFACTS)
    assert set(originals) == set(verify.EXPECTED_ARTIFACTS)


@pytest.mark.parametrize("mutation", ["duplicate_role", "foreign_sealed_path"])
def test_artifact_declarations_reject_duplicate_roles_and_foreign_paths(mutation: str) -> None:
    manifest = _artifact_manifest()
    if mutation == "duplicate_role":
        manifest["artifacts"][1]["logical_name"] = manifest["artifacts"][0]["logical_name"]
    else:
        manifest["artifacts"][0]["sealed_object_path"] = "/tmp/foreign-object"
    with pytest.raises(verify.VerificationError):
        verify._verify_artifact_declarations(manifest)


def test_difference_and_handoff_censuses_are_exact() -> None:
    assert [value["field"] for value in verify.EXPECTED_CONFIG_DIFFERENCES] == sorted(
        value["field"] for value in verify.EXPECTED_CONFIG_DIFFERENCES
    )
    assert verify.EXPECTED_MATCHED_CONTROLS == sorted(verify.EXPECTED_MATCHED_CONTROLS)
    assert verify.EXPECTED_DEFERRED == sorted(verify.EXPECTED_DEFERRED)


def _validation_receipt() -> tuple[dict[str, Any], Any]:
    producer = verify._load_validation_producer(REPO_ROOT)
    commands: list[dict[str, Any]] = []
    outputs = {
        "simple_smoke_pytest": b".\n23 passed in 0.01s\n",
        "manifest_verifier_pytest": b".\n32 passed in 0.01s\n",
        "history_codec_pytest": b".\n28 passed in 0.01s\n",
        "ruff_check": b"All checks passed!\n",
        "ruff_format_check": f"{len(producer.PYTHON_FILES)} files already formatted\n".encode(),
        "python_compile": b"",
        "schema_meta_validation": b"schema-meta-pass\n",
        "git_diff_check": b"",
    }
    for name, argv in producer._commands():
        stdout = outputs[name]
        commands.append(
            {
                "name": name,
                "argv": argv,
                "cwd": str(REPO_ROOT),
                "environment": producer.BASE_ENVIRONMENT,
                "return_code": 0,
                "stdout_base64": base64.b64encode(stdout).decode(),
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_base64": "",
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            }
        )
    return {
        "schema_version": "mobileworld.g1.engineering-close-validation/v1",
        "source_commit": "a" * 40,
        "simple_smoke_test_count": 23,
        "manifest_verifier_test_count": 32,
        "history_codec_test_count": 28,
        "ruff_check_passed": True,
        "ruff_format_check_passed": True,
        "python_compile_passed": True,
        "schema_meta_validation_passed": True,
        "git_diff_check_passed": True,
        "post_hoc_runtime_packages": producer.PACKAGE_METADATA,
        "commands": commands,
    }, producer


def _write_validation_receipt(tmp_path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "validation.json"
    raw = verify._canonical_json(receipt)
    path.write_bytes(raw)
    path.chmod(0o400)
    return {
        "source_commit": "a" * 40,
        "validation": {
            "receipt_path": str(path),
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "receipt_byte_count": len(raw),
            "manifest_verifier_test_count": 32,
        },
    }


def test_validation_receipt_positive_exact_command_census(tmp_path: Path) -> None:
    receipt, _ = _validation_receipt()
    manifest = _write_validation_receipt(tmp_path, receipt)
    verify._verify_validation_receipt(manifest, REPO_ROOT)


@pytest.mark.parametrize("mutation", ["name", "argv", "duplicate", "stdout"])
def test_validation_receipt_rejects_command_forgery(tmp_path: Path, mutation: str) -> None:
    receipt, _ = _validation_receipt()
    if mutation == "name":
        receipt["commands"][0]["name"] = "true"
    elif mutation == "argv":
        receipt["commands"][0]["argv"] = ["/usr/bin/true"]
    elif mutation == "duplicate":
        receipt["commands"][1] = deepcopy(receipt["commands"][0])
    else:
        stdout = b"forged\n"
        receipt["commands"][0]["stdout_base64"] = base64.b64encode(stdout).decode()
        receipt["commands"][0]["stdout_sha256"] = hashlib.sha256(stdout).hexdigest()
    manifest = _write_validation_receipt(tmp_path, receipt)
    with pytest.raises(verify.VerificationError):
        verify._verify_validation_receipt(manifest, REPO_ROOT)


def test_environment_verifier_rejects_extra_mutation() -> None:
    runner = _runner()
    original = runner._server_environment

    def mutated(gpu_index: int) -> dict[str, str]:
        result = original(gpu_index)
        result["UNDECLARED"] = "1"
        return result

    runner._server_environment = mutated
    with pytest.raises(verify.VerificationError, match="not exact"):
        verify._verify_runner_environment(
            runner, (REPO_ROOT / verify.RUNNER_RELATIVE_PATH).read_bytes()
        )
