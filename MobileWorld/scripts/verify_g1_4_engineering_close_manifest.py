"""Verify the sealed, non-formal G1.4 engineering-close evidence bundle."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator

MANIFEST_RELATIVE_PATH = Path(
    "mobileworld_audit_handoff/g1_4/nonformal_live_smoke_manifest.v1.json"
)
INSTALL_RECORD_RELATIVE_PATH = Path(
    "mobileworld_audit_handoff/g1_4/nonformal_live_smoke_install_record.v1.json"
)
SCHEMA_RELATIVE_PATH = Path(
    "mobileworld_audit_handoff/schemas/g1_4/nonformal_live_smoke_manifest.v1.schema.json"
)
RUNNER_RELATIVE_PATH = Path("MobileWorld/scripts/run_g1_gpu_smoke_simple.py")
FIXTURE_RELATIVE_PATH = Path(
    "MobileWorld/tests/offline/fixtures/g1_gpu_smoke_simple/requests.v1.json"
)

EXPECTED_SOURCE_BINDINGS = {
    str(RUNNER_RELATIVE_PATH): "runner",
    "MobileWorld/scripts/verify_g1_4_engineering_close_manifest.py": "verifier",
    "MobileWorld/scripts/run_g1_4_engineering_close_cpu_gates.py": "validation_producer",
    "MobileWorld/scripts/install_g1_4_engineering_close_bundle.py": "installer",
    str(FIXTURE_RELATIVE_PATH): "fixture",
    "MobileWorld/tests/offline/test_g1_gpu_smoke_simple.py": "runner_tests",
    "MobileWorld/tests/offline/test_g1_4_engineering_close_manifest.py": "verifier_tests",
    "mobileworld_audit_handoff/G1_4_NONFORMAL_LIVE_SMOKE_ENGINEERING_CLOSE_AMENDMENT_V1.md": (
        "amendment"
    ),
    str(SCHEMA_RELATIVE_PATH): "schema",
    "mobileworld_audit_handoff/g1/model_config_manifest.v1.json": "model_manifest",
    "MobileWorld/src/mobile_world/agents/utils/prompts/qwen3vl.py": "qwen_prompt",
    "MobileWorld/src/mobile_world/agents/utils/prompts/mai_ui.py": "mai_prompt",
    "MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py": "qwen_parser",
    "MobileWorld/src/mobile_world/agents/implementations/mai_ui_agent.py": "mai_parser",
}

ORIGINAL_EVIDENCE_ROOT = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/"
    "direct-gpu4-qwen-mai-20260831-productionprompt"
)
EXPECTED_ARTIFACTS = {
    "run.jsonl": {
        "original_path": str(ORIGINAL_EVIDENCE_ROOT / "run.jsonl"),
        "sha256": "27c97d1f29119e9b3a087e756814e64fb9b822d829135b1af4b747468a7e38cd",
        "byte_count": 58011,
    },
    "qwen.server.log": {
        "original_path": str(ORIGINAL_EVIDENCE_ROOT / "qwen.server.log"),
        "sha256": "ce511552f520e631f42b79fe8bbb7e4a45e12646ae463969f1de3e4bc3604f71",
        "byte_count": 16204,
    },
    "mai.server.log": {
        "original_path": str(ORIGINAL_EVIDENCE_ROOT / "mai.server.log"),
        "sha256": "6996c4f1d6b938a2b7a48f10d45a44d086cd71695cbf47cfc669e0a670d8a2a0",
        "byte_count": 15963,
    },
}

EXPECTED_EVENT_COUNTS = {
    "run_started": 1,
    "server_started": 2,
    "server_ready": 2,
    "response_received": 22,
    "call_succeeded": 22,
    "server_stopped": 2,
    "run_completed": 1,
}
EXPECTED_ENVIRONMENT_OVERRIDES = {
    "CUDA_VISIBLE_DEVICES": "4",
    "TRITON_PTXAS_PATH": "/usr/local/cuda/bin/ptxas",
    "TRITON_CUOBJDUMP_PATH": "/usr/local/cuda/bin/cuobjdump",
    "TRITON_NVDISASM_PATH": "/usr/local/cuda/bin/nvdisasm",
    "PYTHONPATH": "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages",
    "PYTHONSAFEPATH": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}
EXPECTED_CONFIG_DIFFERENCES = [
    {
        "field": "ambient_child_environment",
        "frozen": "closed reference environment",
        "observed": "ambient environment inherited outside 15 explicit overrides",
        "effect": "exact frozen environment equivalence is unproven",
    },
    {
        "field": "gpu_index",
        "frozen": 0,
        "observed": 4,
        "effect": "the accepted smoke used the separately owner-authorized shared GPU4",
    },
    {
        "field": "package_versions",
        "frozen": {
            "flashinfer": None,
            "torch": "2.8.0+cu126",
            "transformers": "4.57.4",
        },
        "observed": {
            "flashinfer-python": "0.6.6",
            "torch": "2.10.0",
            "transformers": "5.6.0",
        },
        "effect": "package-level serving-environment equivalence is unproven",
    },
    {
        "field": "python_environment",
        "frozen": "frozen reference runtime",
        "observed": "shared Miniconda Python 3.12 plus vllm_env site-packages",
        "effect": "interpreter and environment equivalence is unproven",
    },
    {
        "field": "request_timeout_seconds",
        "frozen": 120,
        "observed": 180,
        "effect": "timeout behavior is compatibility-only",
    },
    {
        "field": "swap_space_gib",
        "frozen": 0,
        "observed": "flag omitted because vLLM 0.19.1 rejected it",
        "effect": "swap-space configuration equivalence is unproven",
    },
    {
        "field": "transport_and_sdk",
        "frozen": "OpenAI SDK 1.106.1 Provider Codec",
        "observed": "Python stdlib http.client; installed OpenAI 2.32.0 unused",
        "effect": "formal Provider Codec and SDK fidelity are unproven",
    },
    {
        "field": "vllm_version",
        "frozen": "0.11.0",
        "observed": "0.19.1",
        "effect": "exact frozen serving-runtime equivalence is unproven",
    },
]
EXPECTED_MATCHED_CONTROLS = [
    "dtype_bfloat16",
    "enforce_eager_true",
    "fixture_call_order_and_metadata",
    "gpu_memory_utilization_0.24",
    "loopback_127.0.0.1_port_18007",
    "max_model_len_32768",
    "max_num_seqs_1",
    "model_snapshot_paths_and_served_names",
    "prefix_caching_disabled",
    "production_host_parser_sources",
    "production_system_prompt_sources",
    "request_model_temperature_and_seed_schedule",
]
EXPECTED_DEFERRED = [
    "backend_dependency_none_proof",
    "exact_frozen_serving_environment",
    "formal_openai_compatible_provider_codec",
    "full_attempt_usage_latency_error_delivery_receipts",
    "g1_7_run_ready_execution_and_formal_replay_seals",
    "population_statistical_replay",
    "sdk_hidden_retry_fidelity",
    "session_kv_and_fresh_invocation_isolation",
]


class VerificationError(RuntimeError):
    """Stable verification failure."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise VerificationError(f"non-finite JSON constant: {value}")


def _strict_json_loads(raw: bytes) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise VerificationError("JSON is not UTF-8") from exc


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise VerificationError("value is not strict JSON") from exc
    return (text + "\n").encode()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stable_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_regular_nofollow(
    path: Path,
    *,
    mode: int | None = None,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerificationError(
            f"cannot open regular file without following links: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise VerificationError(f"not a single-link regular file: {path}")
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise VerificationError(f"unexpected mode for {path}: {stat.S_IMODE(info.st_mode):04o}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        if len(raw) != info.st_size or _stable_stat_identity(os.fstat(descriptor)) != (
            _stable_stat_identity(info)
        ):
            raise VerificationError(f"file changed while being read: {path}")
        return raw, info
    finally:
        os.close(descriptor)


def _load_json(path: Path, *, mode: int | None = None) -> tuple[bytes, dict[str, Any]]:
    raw, _ = _read_regular_nofollow(path, mode=mode)
    value = _strict_json_loads(raw)
    if not isinstance(value, dict):
        raise VerificationError(f"JSON root is not an object: {path}")
    return raw, value


def _verify_hash(path: Path, sha256: str, byte_count: int) -> os.stat_result:
    raw, info = _read_regular_nofollow(path)
    if len(raw) != byte_count or _sha256(raw) != sha256:
        raise VerificationError(f"hash or byte-count mismatch: {path}")
    return info


def _verify_artifact_declarations(
    manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, os.stat_result]]:
    evidence_root = Path(manifest["evidence_root"])
    artifact_by_name = {artifact["logical_name"]: artifact for artifact in manifest["artifacts"]}
    if set(artifact_by_name) != set(EXPECTED_ARTIFACTS) or len(manifest["artifacts"]) != 3:
        raise VerificationError("artifact role census mismatch")
    original_infos: dict[str, os.stat_result] = {}
    for logical_name, expected in EXPECTED_ARTIFACTS.items():
        artifact = artifact_by_name[logical_name]
        if (
            artifact["original_path"] != expected["original_path"]
            or artifact["sha256"] != expected["sha256"]
            or artifact["byte_count"] != expected["byte_count"]
            or artifact["original_mode"] != "0664"
            or artifact["original_physically_immutable"] is not False
        ):
            raise VerificationError(f"artifact identity mismatch: {logical_name}")
        original_info = _verify_hash(
            Path(artifact["original_path"]), artifact["sha256"], artifact["byte_count"]
        )
        if stat.S_IMODE(original_info.st_mode) != 0o664 or original_info.st_nlink != 1:
            raise VerificationError(f"original artifact metadata mismatch: {logical_name}")
        sealed = evidence_root / "objects" / "sha256" / artifact["sha256"][:2] / artifact["sha256"]
        if Path(artifact["sealed_object_path"]) != sealed or sealed.resolve() != sealed:
            raise VerificationError(
                f"artifact is not at its content-addressed path: {logical_name}"
            )
        original_infos[logical_name] = original_info
    return artifact_by_name, original_infos


def _git(repo_root: Path, *arguments: str) -> bytes:
    environment = {
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        cwd=repo_root,
        env=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or result.stderr:
        raise VerificationError(f"Git verification failed: {' '.join(arguments)}")
    return result.stdout


def _verify_source_commit(
    repo_root: Path,
    source_commit: str,
    bindings: list[dict[str, Any]],
    *,
    require_clean: bool = True,
) -> None:
    commit = _git(repo_root, "rev-parse", "--verify", f"{source_commit}^{{commit}}")
    if commit != f"{source_commit}\n".encode():
        raise VerificationError("source commit did not resolve exactly")
    _git(repo_root, "merge-base", "--is-ancestor", source_commit, "HEAD")
    if require_clean and _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise VerificationError("repository worktree is not clean")
    binding_by_path = {binding["path"]: binding for binding in bindings}
    if set(binding_by_path) != set(EXPECTED_SOURCE_BINDINGS) or len(bindings) != len(
        binding_by_path
    ):
        raise VerificationError("source-binding path census mismatch")
    for relative_text, logical_name in sorted(EXPECTED_SOURCE_BINDINGS.items()):
        relative = Path(relative_text)
        binding = binding_by_path[relative_text]
        if binding["logical_name"] != logical_name:
            raise VerificationError(f"source-binding logical name mismatch: {relative_text}")
        current_raw, _ = _read_regular_nofollow(repo_root / relative)
        committed_raw = _git(repo_root, "show", f"{source_commit}:{relative_text}")
        if current_raw != committed_raw:
            raise VerificationError(f"source changed after implementation commit: {relative_text}")
        if len(current_raw) != binding["byte_count"] or _sha256(current_raw) != binding["sha256"]:
            raise VerificationError(f"source binding mismatch: {relative_text}")


def _verify_source_tree(repo_root: Path, source_commit: str, binding: dict[str, Any]) -> None:
    expected_path = "MobileWorld/src/mobile_world"
    if binding["path"] != expected_path:
        raise VerificationError("source-tree path mismatch")
    committed_tree = (
        _git(repo_root, "rev-parse", f"{source_commit}:{expected_path}").decode().strip()
    )
    current_tree = _git(repo_root, "rev-parse", f"HEAD:{expected_path}").decode().strip()
    if committed_tree != binding["git_tree_sha1"] or current_tree != committed_tree:
        raise VerificationError("transitive source-tree binding mismatch")
    _git(repo_root, "diff", "--quiet", source_commit, "--", expected_path)


def _load_runner(repo_root: Path) -> ModuleType:
    runner_path = repo_root / RUNNER_RELATIVE_PATH
    spec = importlib.util.spec_from_file_location("_g14_close_runner", runner_path)
    if spec is None or spec.loader is None:
        raise VerificationError("cannot load the committed simple runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise VerificationError("cannot import the committed simple runner") from exc
    return module


def _load_validation_producer(repo_root: Path) -> ModuleType:
    path = repo_root / "MobileWorld/scripts/run_g1_4_engineering_close_cpu_gates.py"
    spec = importlib.util.spec_from_file_location("_g14_close_validation_producer", path)
    if spec is None or spec.loader is None:
        raise VerificationError("cannot load the validation producer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise VerificationError("cannot import the validation producer") from exc
    return module


def _literal_environment_keys(runner_raw: bytes) -> set[str]:
    tree = ast.parse(runner_raw)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_server_environment"
    )
    candidates: list[set[str]] = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Dict)
        ):
            keys = {ast.literal_eval(key) for key in node.args[0].keys if key is not None}
            candidates.append(keys)
    if len(candidates) != 1:
        raise VerificationError("runner environment override surface is ambiguous")
    return candidates[0]


def _verify_runner_environment(runner: ModuleType, runner_raw: bytes) -> None:
    if _literal_environment_keys(runner_raw) != set(EXPECTED_ENVIRONMENT_OVERRIDES):
        raise VerificationError("runner environment override key census mismatch")
    original_environment = runner.os.environ
    sentinel = {"AMBIENT_ONLY": "preserved"}
    sentinel.update({key: "__wrong__" for key in EXPECTED_ENVIRONMENT_OVERRIDES})
    runner.os.environ = sentinel
    try:
        observed = runner._server_environment(4)
    finally:
        runner.os.environ = original_environment
    expected = dict(sentinel)
    expected.update(EXPECTED_ENVIRONMENT_OVERRIDES)
    if observed != expected:
        raise VerificationError("runner environment transformation is not exact")


def _load_fixture(repo_root: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw, value = _load_json(repo_root / FIXTURE_RELATIVE_PATH)
    if _sha256(raw) != "fee3b47688f06be09f5f9f56abc64fc8dd82d5a0f571b258be31f572e676a195":
        raise VerificationError("fixture hash changed")
    if set(value) != {"schema_version", "calls"} or not isinstance(value["calls"], list):
        raise VerificationError("fixture envelope changed")
    if len(value["calls"]) != 22:
        raise VerificationError("fixture call count changed")
    return raw, value["calls"]


def _parse_run(path: Path) -> list[dict[str, Any]]:
    raw, _ = _read_regular_nofollow(path)
    raw_lines = raw.splitlines(keepends=True)
    if len(raw_lines) != 52 or any(not line.endswith(b"\n") for line in raw_lines):
        raise VerificationError("run.jsonl line census mismatch")
    events: list[dict[str, Any]] = []
    for line in raw_lines:
        event = _strict_json_loads(line)
        if not isinstance(event, dict) or _canonical_json(event) != line:
            raise VerificationError("run.jsonl contains a non-canonical event")
        events.append(event)
    return events


def _verify_run(
    path: Path,
    fixture_calls: list[dict[str, Any]],
    runner: ModuleType,
) -> dict[str, int]:
    events = _parse_run(path)
    if [event.get("sequence") for event in events] != list(range(52)):
        raise VerificationError("run.jsonl sequence mismatch")
    counts = {name: 0 for name in EXPECTED_EVENT_COUNTS}
    for event in events:
        name = event.get("event")
        if name not in counts:
            raise VerificationError(f"unexpected event: {name}")
        counts[name] += 1
    if counts != EXPECTED_EVENT_COUNTS:
        raise VerificationError("run.jsonl event census mismatch")
    expected_runner_path = (
        "/shared/linqiang/agent_monitor/AgentSentinel/MobileWorld/.venv/bin/python"
    )
    if events[0] != {
        "call_count": 22,
        "event": "run_started",
        "gpu_index": 4,
        "host": "127.0.0.1",
        "port": 18007,
        "runner_python": expected_runner_path,
        "sequence": 0,
    }:
        raise VerificationError("run_started event mismatch")
    if events[-1] != {"event": "run_completed", "sequence": 51, "successful_call_count": 22}:
        raise VerificationError("terminal event mismatch")

    expected_index = 1
    parsed_contents: dict[tuple[str, int, int], tuple[str, Any]] = {}
    parsers = runner._load_host_parsers()
    for model in runner.MODELS:
        start = events[expected_index]
        ready = events[expected_index + 1]
        if (
            start.get("event") != "server_started"
            or start.get("model_id") != model.model_id
            or type(start.get("pid")) is not int
            or set(start) != {"event", "model_id", "pid", "sequence"}
        ):
            raise VerificationError(f"server-start event mismatch: {model.model_id}")
        if ready != {
            "event": "server_ready",
            "model_id": model.model_id,
            "sequence": expected_index + 1,
        }:
            raise VerificationError(f"server-ready event mismatch: {model.model_id}")
        expected_index += 2
        model_calls = [call for call in fixture_calls if call["model_id"] == model.model_id]
        for fixture_call in model_calls:
            response = events[expected_index]
            call = events[expected_index + 1]
            if (
                response.get("event") != "response_received"
                or call.get("event") != "call_succeeded"
            ):
                raise VerificationError("response/call events are not adjacent")
            expected_metadata = {
                "call_id": fixture_call["call_id"],
                "model_id": fixture_call["model_id"],
                "phase": fixture_call["phase"],
                "seed": fixture_call["seed"],
                "repeat": fixture_call["repeat"],
                "arm": fixture_call["arm"],
            }
            for key, value in expected_metadata.items():
                if call.get(key) != value:
                    raise VerificationError(f"call metadata mismatch: {fixture_call['call_id']}")
            if (
                response.get("call_id") != fixture_call["call_id"]
                or response.get("model_id") != model.model_id
                or response.get("http_status") != 200
                or call.get("http_status") != 200
                or call.get("generated_action_executed") is not False
            ):
                raise VerificationError(f"call status mismatch: {fixture_call['call_id']}")
            raw = base64.b64decode(response["response_body_base64"], validate=True)
            if (
                len(raw) != response.get("response_byte_count")
                or _sha256(raw) != response.get("response_sha256")
                or response.get("response_sha256") != call.get("response_sha256")
            ):
                raise VerificationError(f"response body mismatch: {fixture_call['call_id']}")
            request = dict(fixture_call["application_request"])
            request["seed"] = fixture_call["seed"]
            if runner._canonical_sha256(request) != call.get("request_sha256"):
                raise VerificationError(f"request hash mismatch: {fixture_call['call_id']}")
            expected_content, expected_parser_output = runner._validate_response(
                model.model_id,
                200,
                raw,
                parsers,
            )
            if (
                call.get("content") != expected_content
                or call.get("host_parser_output") != expected_parser_output
            ):
                raise VerificationError(
                    f"host parser projection mismatch: {fixture_call['call_id']}"
                )
            if fixture_call["phase"] == "G1_4_CANARY":
                pair_key = (model.model_id, fixture_call["seed"], fixture_call["repeat"])
                parsed_contents[pair_key] = (expected_content, expected_parser_output)
            expected_index += 2
        stopped = events[expected_index]
        if (
            stopped.get("event") != "server_stopped"
            or stopped.get("model_id") != model.model_id
            or stopped.get("result") != "session_terminated"
            or set(stopped) != {"event", "model_id", "result", "sequence"}
        ):
            raise VerificationError(f"server-stop event mismatch: {model.model_id}")
        expected_index += 1
    if expected_index != 51:
        raise VerificationError("model/call schedule length mismatch")
    for model_id in ("qwen3vl_8b", "mai_ui_8b"):
        for seed in (1729, 2718, 31415):
            if parsed_contents[(model_id, seed, 1)] != parsed_contents[(model_id, seed, 2)]:
                raise VerificationError(f"same-seed repeat mismatch: {model_id}/{seed}")
    return {
        "qwen_root_pid": events[1]["pid"],
        "mai_root_pid": events[26]["pid"],
    }


def _verify_log(
    path: Path,
    *,
    model: dict[str, Any],
    root_pid: int,
) -> None:
    raw, _ = _read_regular_nofollow(path)
    text = raw.decode("utf-8")
    post_lines = [
        line for line in text.splitlines() if '"POST /v1/chat/completions HTTP/1.1"' in line
    ]
    if len(post_lines) != 11 or any(not line.endswith(" 200 OK") for line in post_lines):
        raise VerificationError(f"server POST/HTTP census mismatch: {path}")
    if text.count("Application shutdown complete.") != 1 or "v0.19.1" not in text:
        raise VerificationError(f"server lifecycle/version evidence mismatch: {path}")
    if model["snapshot"] not in text or model["served_name"] not in text:
        raise VerificationError(f"server model binding missing: {path}")
    root_pids = {int(value) for value in re.findall(r"\(APIServer pid=(\d+)\)", text)}
    engine_pids = {int(value) for value in re.findall(r"\(EngineCore pid=(\d+)\)", text)}
    if root_pids != {root_pid} or engine_pids != {model["engine_pid"]}:
        raise VerificationError(f"server PID evidence mismatch: {path}")


def _verify_runtime(
    manifest: dict[str, Any],
    repo_root: Path,
    runner: ModuleType,
    run_pids: dict[str, int],
    artifact_paths: dict[str, Path],
) -> None:
    runtime = manifest["runtime"]
    if (
        runtime["gpu_index"] != 4
        or runtime["host"] != runner.HOST
        or runtime["port"] != runner.PORT
        or runtime["request_path"] != runner.REQUEST_PATH
        or runtime["request_timeout_seconds"] != runner.REQUEST_TIMEOUT_SECONDS
        or runtime["server_site_packages"] != runner.SERVER_SITE_PACKAGES
    ):
        raise VerificationError("runtime constants differ from the committed runner")
    runner_raw, _ = _read_regular_nofollow(repo_root / RUNNER_RELATIVE_PATH)
    _verify_runner_environment(runner, runner_raw)
    if runtime["child_environment_overrides"] != EXPECTED_ENVIRONMENT_OVERRIDES:
        raise VerificationError("manifest environment overrides mismatch")

    invocation = runtime["runner_invocation"]
    lexical = Path(invocation["lexical_path"])
    lexical_info = lexical.lstat()
    if (
        not stat.S_ISLNK(lexical_info.st_mode)
        or os.readlink(lexical) != invocation["symlink_target"]
    ):
        raise VerificationError("runner invocation symlink mismatch")
    resolved = Path(invocation["resolved_path"])
    if lexical.resolve() != resolved:
        raise VerificationError("runner invocation resolution mismatch")
    _verify_hash(resolved, invocation["sha256"], invocation["byte_count"])
    server_python = runtime["server_python"]
    if Path(server_python["path"]).resolve() != Path(server_python["resolved_path"]):
        raise VerificationError("server Python resolution mismatch")
    _verify_hash(
        Path(server_python["resolved_path"]),
        server_python["sha256"],
        server_python["byte_count"],
    )
    if runner.SERVER_PYTHON != server_python["path"]:
        raise VerificationError("server Python differs from runner constant")

    models = runtime["models"]
    if [model["model_id"] for model in models] != [model.model_id for model in runner.MODELS]:
        raise VerificationError("runtime model order or census mismatch")
    for model_manifest, model_config in zip(models, runner.MODELS, strict=True):
        if (
            model_manifest["snapshot"] != model_config.snapshot
            or model_manifest["served_name"] != model_config.served_name
            or model_manifest["server_argv"] != runner._server_command(model_config)
        ):
            raise VerificationError(f"runtime model command mismatch: {model_config.model_id}")
        root_key = "qwen_root_pid" if model_config.model_id == "qwen3vl_8b" else "mai_root_pid"
        root_pid = run_pids[root_key]
        if model_manifest["root_pid"] != root_pid:
            raise VerificationError(f"runtime root PID mismatch: {model_config.model_id}")
        _verify_log(
            artifact_paths[model_config.log_name],
            model=model_manifest,
            root_pid=root_pid,
        )


def _verify_validation_receipt(
    manifest: dict[str, Any],
    repo_root: Path,
    *,
    receipt_path: Path | None = None,
) -> None:
    validation = manifest["validation"]
    path = Path(validation["receipt_path"]) if receipt_path is None else receipt_path
    raw, receipt = _load_json(path, mode=0o400)
    if (
        len(raw) != validation["receipt_byte_count"]
        or _sha256(raw) != validation["receipt_sha256"]
        or raw != _canonical_json(receipt)
    ):
        raise VerificationError("validation receipt binding mismatch")
    expected_keys = {
        "schema_version",
        "source_commit",
        "simple_smoke_test_count",
        "history_codec_test_count",
        "manifest_verifier_test_count",
        "ruff_check_passed",
        "ruff_format_check_passed",
        "python_compile_passed",
        "schema_meta_validation_passed",
        "git_diff_check_passed",
        "post_hoc_runtime_packages",
        "commands",
    }
    if set(receipt) != expected_keys or receipt["source_commit"] != manifest["source_commit"]:
        raise VerificationError("validation receipt shape or source commit mismatch")
    expected_scalars = {
        "schema_version": "mobileworld.g1.engineering-close-validation/v1",
        "simple_smoke_test_count": 23,
        "history_codec_test_count": 28,
        "manifest_verifier_test_count": validation["manifest_verifier_test_count"],
        "ruff_check_passed": True,
        "ruff_format_check_passed": True,
        "python_compile_passed": True,
        "schema_meta_validation_passed": True,
        "git_diff_check_passed": True,
    }
    for key, value in expected_scalars.items():
        if receipt[key] != value:
            raise VerificationError(f"validation receipt mismatch: {key}")
    producer = _load_validation_producer(repo_root)
    expected_commands = producer._commands()
    if not isinstance(receipt["commands"], list) or len(receipt["commands"]) != len(
        expected_commands
    ):
        raise VerificationError("validation command census mismatch")
    observed_names: list[str] = []
    for command, (expected_name, expected_argv) in zip(
        receipt["commands"], expected_commands, strict=True
    ):
        expected_shape = {
            "name",
            "argv",
            "cwd",
            "environment",
            "return_code",
            "stdout_base64",
            "stdout_sha256",
            "stderr_base64",
            "stderr_sha256",
        }
        if set(command) != expected_shape:
            raise VerificationError("validation command shape mismatch")
        if (
            command["name"] != expected_name
            or command["argv"] != expected_argv
            or command["cwd"] != str(repo_root)
            or command["environment"] != producer.BASE_ENVIRONMENT
        ):
            raise VerificationError(f"validation command identity mismatch: {expected_name}")
        stdout = base64.b64decode(command["stdout_base64"], validate=True)
        stderr = base64.b64decode(command["stderr_base64"], validate=True)
        if (
            command["return_code"] != 0
            or _sha256(stdout) != command["stdout_sha256"]
            or stderr
            or _sha256(stderr) != command["stderr_sha256"]
        ):
            raise VerificationError(f"validation command failed: {expected_name}")
        observed_names.append(expected_name)
        if expected_name.endswith("_pytest"):
            match = re.search(rb"\n(\d+) passed in [0-9.]+s\n$", stdout)
            expected_count = {
                "simple_smoke_pytest": 23,
                "manifest_verifier_pytest": 32,
                "history_codec_pytest": 28,
            }[expected_name]
            if match is None or int(match.group(1)) != expected_count:
                raise VerificationError(f"pytest result mismatch: {expected_name}")
        elif expected_name == "ruff_check" and stdout != b"All checks passed!\n":
            raise VerificationError("Ruff check output mismatch")
        elif expected_name == "ruff_format_check":
            expected = f"{len(producer.PYTHON_FILES)} files already formatted\n".encode()
            if stdout != expected:
                raise VerificationError("Ruff format output mismatch")
        elif expected_name == "schema_meta_validation" and stdout != b"schema-meta-pass\n":
            raise VerificationError("schema meta-validation output mismatch")
        elif expected_name in {"python_compile", "git_diff_check"} and stdout:
            raise VerificationError(f"unexpected output: {expected_name}")
    if len(observed_names) != len(set(observed_names)):
        raise VerificationError("duplicate validation command")
    if receipt["post_hoc_runtime_packages"] != producer.PACKAGE_METADATA:
        raise VerificationError("runtime package receipt census mismatch")
    for package in receipt["post_hoc_runtime_packages"].values():
        _verify_hash(Path(package["path"]), package["sha256"], package["byte_count"])


def _verify_install_receipt(manifest: dict[str, Any], manifest_raw: bytes, repo_root: Path) -> None:
    path = Path(manifest["installation_receipt_path"])
    raw, receipt = _load_json(path, mode=0o400)
    if raw != _canonical_json(receipt):
        raise VerificationError("install receipt is not canonical")
    expected = {
        "schema_version": "mobileworld.g1.engineering-close-installation/v1",
        "evidence_root": manifest["evidence_root"],
        "manifest_sha256": _sha256(manifest_raw),
        "artifact_count": 3,
        "evidence_root_absent_before": True,
        "validation_receipt_absent_before": True,
        "installed_no_replace": True,
        "directory_fsync_completed": True,
        "final_reopen_verified": True,
        "gpu_used": False,
        "model_used": False,
        "network_used": False,
        "signal_sent": False,
    }
    if receipt != expected:
        raise VerificationError("install receipt mismatch")
    record_raw, record = _load_json(repo_root / INSTALL_RECORD_RELATIVE_PATH)
    if record_raw != _canonical_json(record):
        raise VerificationError("checked-in install record is not canonical")
    expected_record_keys = {
        "schema_version",
        "source_commit",
        "manifest_sha256",
        "manifest_byte_count",
        "evidence_root",
        "validation_receipt_path",
        "validation_receipt_sha256",
        "validation_receipt_byte_count",
        "install_receipt_path",
        "install_receipt_sha256",
        "install_receipt_byte_count",
    }
    if set(record) != expected_record_keys:
        raise VerificationError("checked-in install record shape mismatch")
    validation = manifest["validation"]
    expected_record = {
        "schema_version": "mobileworld.g1.engineering-close-install-record/v1",
        "source_commit": manifest["source_commit"],
        "manifest_sha256": _sha256(manifest_raw),
        "manifest_byte_count": len(manifest_raw),
        "evidence_root": manifest["evidence_root"],
        "validation_receipt_path": validation["receipt_path"],
        "validation_receipt_sha256": validation["receipt_sha256"],
        "validation_receipt_byte_count": validation["receipt_byte_count"],
        "install_receipt_path": str(path),
        "install_receipt_sha256": _sha256(raw),
        "install_receipt_byte_count": len(raw),
    }
    if record != expected_record:
        raise VerificationError("checked-in install record binding mismatch")


def verify(manifest_path: Path, schema_path: Path) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    if manifest_path.resolve() != repo_root / MANIFEST_RELATIVE_PATH:
        raise VerificationError("manifest path is not the pinned checked-in path")
    if schema_path.resolve() != repo_root / SCHEMA_RELATIVE_PATH:
        raise VerificationError("schema path is not the pinned checked-in path")
    manifest_raw, manifest = _load_json(manifest_path)
    _, schema = _load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest)
    if manifest_raw != _canonical_json(manifest):
        raise VerificationError("manifest is not canonical UTF-8 JSON plus LF")
    if manifest["manifest_id"] != f"g14smoke-{EXPECTED_ARTIFACTS['run.jsonl']['sha256'][:24]}":
        raise VerificationError("manifest id is not derived from the run artifact")
    if manifest["known_config_differences"] != EXPECTED_CONFIG_DIFFERENCES:
        raise VerificationError("configuration-difference census mismatch")
    if manifest["matched_controls"] != EXPECTED_MATCHED_CONTROLS:
        raise VerificationError("matched-control census mismatch")
    if manifest["deferred_to_g1_7"] != EXPECTED_DEFERRED:
        raise VerificationError("G1.7 deferral census mismatch")

    _verify_source_commit(repo_root, manifest["source_commit"], manifest["source_bindings"])
    _verify_source_tree(repo_root, manifest["source_commit"], manifest["source_tree_binding"])
    runner = _load_runner(repo_root)
    _, fixture_calls = _load_fixture(repo_root)

    evidence_root = Path(manifest["evidence_root"])
    if evidence_root.resolve() != evidence_root:
        raise VerificationError("evidence root contains a symlinked ancestor")
    root_info = evidence_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o500
        or root_info.st_uid != 1035
        or root_info.st_gid != 1035
    ):
        raise VerificationError("evidence root metadata mismatch")

    artifact_by_name, original_infos = _verify_artifact_declarations(manifest)
    sealed_paths: dict[str, Path] = {}
    source_inodes: set[tuple[int, int]] = set()
    sealed_inodes: set[tuple[int, int]] = set()
    for logical_name, expected in EXPECTED_ARTIFACTS.items():
        artifact = artifact_by_name[logical_name]
        original_info = original_infos[logical_name]
        sealed = evidence_root / "objects" / "sha256" / artifact["sha256"][:2] / artifact["sha256"]
        sealed_info = _verify_hash(sealed, artifact["sha256"], artifact["byte_count"])
        if (
            stat.S_IMODE(sealed_info.st_mode) != 0o400
            or sealed_info.st_uid != artifact["uid"]
            or sealed_info.st_gid != artifact["gid"]
            or sealed_info.st_nlink != artifact["nlink"]
        ):
            raise VerificationError(f"sealed artifact metadata mismatch: {logical_name}")
        source_inodes.add((original_info.st_dev, original_info.st_ino))
        sealed_inodes.add((sealed_info.st_dev, sealed_info.st_ino))
        sealed_paths[logical_name] = sealed
    if source_inodes & sealed_inodes or len(sealed_inodes) != 3:
        raise VerificationError("sealed artifacts are not three distinct copies")

    manifest_sha256 = _sha256(manifest_raw)
    external_manifest = evidence_root / f"manifest-{manifest_sha256}.json"
    external_raw, external_info = _read_regular_nofollow(external_manifest, mode=0o400)
    if external_info.st_uid != 1035 or external_info.st_gid != 1035 or external_raw != manifest_raw:
        raise VerificationError("external manifest differs from checked-in manifest")

    expected_files = set(sealed_paths.values()) | {external_manifest}
    expected_directories = {
        evidence_root,
        evidence_root / "objects",
        evidence_root / "objects" / "sha256",
        *{
            evidence_root / "objects" / "sha256" / artifact["sha256"][:2]
            for artifact in manifest["artifacts"]
        },
    }
    observed_files: set[Path] = set()
    observed_directories: set[Path] = set()
    for current, directories, files in os.walk(evidence_root, followlinks=False):
        current_path = Path(current)
        current_info = current_path.lstat()
        observed_directories.add(current_path)
        if (
            current_path.is_symlink()
            or stat.S_IMODE(current_info.st_mode) != 0o500
            or current_info.st_uid != 1035
            or current_info.st_gid != 1035
        ):
            raise VerificationError(f"unsafe evidence directory: {current_path}")
        for name in directories:
            if (current_path / name).is_symlink():
                raise VerificationError(f"evidence symlink: {current_path / name}")
        observed_files.update(current_path / name for name in files)
    if observed_files != expected_files or observed_directories != expected_directories:
        raise VerificationError("evidence tree inventory mismatch")

    run_pids = _verify_run(sealed_paths["run.jsonl"], fixture_calls, runner)
    _verify_runtime(manifest, repo_root, runner, run_pids, sealed_paths)
    _verify_validation_receipt(manifest, repo_root)
    _verify_install_receipt(manifest, manifest_raw, repo_root)
    return {
        "artifact_count": 3,
        "engineering_close_status": manifest["engineering_close_status"],
        "event_count": 52,
        "formal_replay_status": manifest["formal_replay_status"],
        "manifest_sha256": manifest_sha256,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    arguments = parser.parse_args()
    result = verify(arguments.manifest, arguments.schema)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
