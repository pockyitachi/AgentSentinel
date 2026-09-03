from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptPricingV1,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    parse_production_runtime_config,
    production_runtime_config_sha256,
)


def _script() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "build_r2_4_production_inputs.py"


def _run(tmp_path: Path, *, backend_mode: int = 0o600) -> subprocess.CompletedProcess[str]:
    input_root = tmp_path / "inputs"
    input_root.mkdir(mode=0o700)
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    backend = tmp_path / "backend.env"
    backend.write_text("TEST_ONLY=true\n", encoding="utf-8")
    backend.chmod(backend_mode)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(_script()),
            "--output-dir",
            str(tmp_path / "built"),
            "--repository-root",
            str(Path(__file__).resolve().parents[4]),
            "--authorized-pilot-input-root",
            str(input_root),
            "--process-log-root",
            str(logs),
            "--backend-port",
            "7160",
            "--backend-device",
            "emulator-5554",
            "--backend-image-id-sha256",
            hashlib.sha256(b"cpu-test-image").hexdigest(),
            "--backend-environment-file",
            str(backend),
            "--qwen-gpu-index",
            "0",
            "--mai-gpu-index",
            "1",
            "--vllm-python-executable",
            sys.executable,
            "--vllm-version",
            "0.10.0",
            "--pricing-id",
            "owner-pinned-gpt56-2026-09-03",
            "--input-price-usd-micros-per-million",
            "4000000",
            "--cached-input-price-usd-micros-per-million",
            "400000",
            "--output-price-usd-micros-per-million",
            "20000000",
            "--pricing-source-sha256",
            hashlib.sha256(b"official-price-source").hexdigest(),
            "--pricing-effective-at-utc",
            "2026-09-03T00:00:00Z",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_builder_writes_canonical_runtime_and_pricing_inputs(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    output = tmp_path / "built"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    runtime_path = output / "runtime-config.json"
    pricing_path = output / "pricing.json"
    assert stat.S_IMODE(runtime_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(pricing_path.stat().st_mode) == 0o600
    runtime_raw = runtime_path.read_bytes()
    runtime_value = cast(JsonValue, json.loads(runtime_raw))
    assert canonical_json_bytes(runtime_value) == runtime_raw
    runtime = parse_production_runtime_config(runtime_value)
    assert production_runtime_config_sha256(runtime) == summary["runtime_config_sha256"]
    pricing_raw = pricing_path.read_bytes()
    pricing_value = json.loads(pricing_raw)
    assert canonical_json_bytes(cast(JsonValue, pricing_value)) == pricing_raw
    pricing = LiveAttemptPricingV1(
        pricing_id=pricing_value["pricing_id"],
        model=pricing_value["model"],
        input_usd_micros_per_million_tokens=pricing_value["input_usd_micros_per_million_tokens"],
        cached_input_usd_micros_per_million_tokens=pricing_value[
            "cached_input_usd_micros_per_million_tokens"
        ],
        output_usd_micros_per_million_tokens=pricing_value["output_usd_micros_per_million_tokens"],
        source_sha256=pricing_value["source_sha256"],
        effective_at_utc=pricing_value["effective_at_utc"],
        rounding_policy=pricing_value["rounding_policy"],
        schema_version=pricing_value["schema_version"],
    )
    assert live_attempt_pricing_sha256(pricing) == summary["pricing_sha256"]


def test_builder_rejects_non_owner_backend_env_without_output(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)

    result = _run(tmp_path, backend_mode=0o644)

    assert result.returncode == 2
    assert "BACKEND_ENVIRONMENT_FILE_INVALID" in result.stderr
    assert not (tmp_path / "built").exists()
