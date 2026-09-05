from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from PIL import Image

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPOSITORY_ROOT / "MobileWorld/scripts/rebind_r2_4_smoke_fixture.py"
FIXTURE_ROOT = REPOSITORY_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs"

CASES = (
    ("qwen", "qwen_flat_progress.captured.v1.json", "Qwen3-VL-8B-Instruct"),
    ("mai", "mai_raw_replay.captured.v1.json", "MAI-UI-8B"),
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_r2_4_fixture_rebind", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")))


def _canonical(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(cast(JsonValue, value))


def _run(
    module: ModuleType,
    *,
    tmp_path: Path,
    host: str,
    served_model_id: str,
    value: dict[str, Any],
) -> tuple[int, Path, Path, bytes]:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.json"
    output = tmp_path / "rebound.json"
    raw = _canonical(value)
    source.write_bytes(raw)
    result = module.main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--expected-host",
            host,
            "--served-model-id",
            served_model_id,
            "--repository-root",
            str(REPOSITORY_ROOT),
        ]
    )
    return result, source, output, raw


@pytest.mark.parametrize("host,fixture_name,_served_model_id", CASES)
def test_same_model_rebind_is_byte_identical(
    host: str,
    fixture_name: str,
    _served_model_id: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    value = _fixture(fixture_name)
    source_model = value["application_request"]["model"]
    result, source, output, raw = _run(
        module,
        tmp_path=tmp_path,
        host=host,
        served_model_id=source_model,
        value=value,
    )

    assert result == 0
    assert source.read_bytes() == raw
    assert output.read_bytes() == raw
    metadata = output.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["fixture_artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["application_request_sha256"] == value["fixture_request_sha256"]


@pytest.mark.parametrize("host,fixture_name,served_model_id", CASES)
def test_real_served_model_rebind_changes_only_model_derived_bindings(
    host: str,
    fixture_name: str,
    served_model_id: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    source_value = _fixture(fixture_name)
    result, source, output, source_raw = _run(
        module,
        tmp_path=tmp_path,
        host=host,
        served_model_id=served_model_id,
        value=source_value,
    )

    assert result == 0
    assert source.read_bytes() == source_raw
    output_raw = output.read_bytes()
    rebound = json.loads(output_raw)
    request = rebound["application_request"]
    assert request["model"] == served_model_id
    request_sha256 = hashlib.sha256(_canonical(request)).hexdigest()
    assert rebound["fixture_request_sha256"] == request_sha256
    assert rebound["expected_rendered_request_sha256"]["ORIGINAL"] == request_sha256
    assert {item["source_request_sha256"] for item in rebound["curated_span_bindings"]} == {
        request_sha256
    }
    assert all(
        rebound["expected_rendered_request_sha256"][arm]
        != source_value["expected_rendered_request_sha256"][arm]
        for arm in source_value["expected_rendered_request_sha256"]
    )

    restored = deepcopy(rebound)
    restored["application_request"]["model"] = source_value["application_request"]["model"]
    restored["fixture_request_sha256"] = source_value["fixture_request_sha256"]
    for binding, source_binding in zip(
        restored["curated_span_bindings"],
        source_value["curated_span_bindings"],
        strict=True,
    ):
        binding["source_request_sha256"] = source_binding["source_request_sha256"]
    restored["expected_rendered_request_sha256"] = source_value["expected_rendered_request_sha256"]
    assert restored == source_value

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["fixture_artifact_sha256"] == hashlib.sha256(output_raw).hexdigest()
    assert receipt["application_request_sha256"] == request_sha256
    assert receipt["fixture_artifact_sha256"] != receipt["application_request_sha256"]


def test_wrong_host_fails_before_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_script()
    result, _, output, _ = _run(
        module,
        tmp_path=tmp_path,
        host="mai",
        served_model_id="MAI-UI-8B",
        value=_fixture("qwen_flat_progress.captured.v1.json"),
    )

    assert result == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().err)["error_code"] == "SOURCE_HOST_MISMATCH"


def test_cross_host_fixture_metadata_fails_before_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    value = _fixture("qwen_flat_progress.captured.v1.json")
    value["fixture_id"] = "g15-mai-raw-replay-captured-redacted-v1"
    value["human_diff_golden"] = "mai_raw_replay.expected_diff.v1.txt"
    result, _, output, _ = _run(
        module,
        tmp_path=tmp_path,
        host="qwen",
        served_model_id="Qwen3-VL-8B-Instruct",
        value=value,
    )

    assert result == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().err)["error_code"] == "SOURCE_HOST_MISMATCH"


def test_non_single_pixel_png_fails_before_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    value = _fixture("qwen_flat_progress.captured.v1.json")
    image = Image.new("RGB", (2, 2), color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    request = value["application_request"]
    for message in request["messages"]:
        for block in message.get("content", []):
            if block.get("type") == "image_url":
                block["image_url"]["url"] = f"data:image/png;base64,{encoded}"
    request_sha256 = hashlib.sha256(_canonical(request)).hexdigest()
    value["fixture_request_sha256"] = request_sha256
    for binding in value["curated_span_bindings"]:
        binding["source_request_sha256"] = request_sha256
    result, _, output, _ = _run(
        module,
        tmp_path=tmp_path,
        host="qwen",
        served_model_id="Qwen3-VL-8B-Instruct",
        value=value,
    )

    assert result == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().err)["error_code"] == "SOURCE_PNG_INVALID"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda value: value["curated_span_bindings"][0].__setitem__(
                "char_end", value["curated_span_bindings"][0]["char_end"] - 1
            ),
            "SOURCE_TARGET_BINDING_INVALID",
        ),
        (
            lambda value: value["expected_rendered_request_sha256"].__setitem__("MASK", "f" * 64),
            "SOURCE_RENDERED_HASH_MISMATCH",
        ),
    ),
)
def test_stale_binding_or_rendered_hash_fails_closed(
    mutation: Any,
    expected_code: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    value = _fixture("qwen_flat_progress.captured.v1.json")
    mutation(value)
    result, _, output, _ = _run(
        module,
        tmp_path=tmp_path,
        host="qwen",
        served_model_id="Qwen3-VL-8B-Instruct",
        value=value,
    )

    assert result == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().err)["error_code"] == expected_code


def test_existing_output_is_not_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    tmp_path.chmod(0o700)
    output = tmp_path / "rebound.json"
    output.write_bytes(b"owner-existing")
    value = _fixture("qwen_flat_progress.captured.v1.json")
    source = tmp_path / "source.json"
    source.write_bytes(_canonical(value))

    result = module.main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--expected-host",
            "qwen",
            "--served-model-id",
            "Qwen3-VL-8B-Instruct",
            "--repository-root",
            str(REPOSITORY_ROOT),
        ]
    )

    assert result == 2
    assert output.read_bytes() == b"owner-existing"
    assert json.loads(capsys.readouterr().err)["error_code"] == "OUTPUT_NOT_FRESH"
