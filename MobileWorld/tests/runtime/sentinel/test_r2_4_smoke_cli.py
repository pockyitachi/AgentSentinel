from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptPricingV1,
    live_attempt_pricing_projection,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_run import RunAuthorizationStatusV1
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionResourceTopologyV1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPOSITORY_ROOT / "MobileWorld" / "scripts"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"test_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _builder_arguments(tmp_path: Path) -> list[str]:
    secret = tmp_path / "openai.key"
    secret.write_bytes(b"test-only-secret-never-read")
    secret.chmod(0o600)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture_arguments: list[str] = []
    for host in ("qwen", "mai"):
        served_model_id = f"{host}-smoke-model"
        fixture_name = (
            "qwen_flat_progress.captured.v1.json"
            if host == "qwen"
            else "mai_raw_replay.captured.v1.json"
        )
        captured = json.loads(
            (
                REPOSITORY_ROOT
                / "MobileWorld"
                / "tests"
                / "offline"
                / "fixtures"
                / "g1_5_history_codecs"
                / fixture_name
            ).read_text(encoding="utf-8")
        )
        request = captured["application_request"]
        request["model"] = served_model_id
        request_sha256 = _sha(canonical_json_bytes(cast(JsonValue, request)))
        captured["fixture_request_sha256"] = request_sha256
        for binding in captured["curated_span_bindings"]:
            binding["source_request_sha256"] = request_sha256
        for mode in ("off", "shadow", "active"):
            path = fixtures / f"{host}-{mode}.json"
            path.write_bytes(canonical_json_bytes(cast(JsonValue, captured)))
            fixture_arguments.extend((f"--{host}-{mode}-fixture", str(path)))
    common = [
        "--output",
        str(tmp_path / "draft.json"),
        "--repository-root",
        str(REPOSITORY_ROOT),
        "--runtime-output-root",
        str(tmp_path / "smoke-output"),
        "--secret-file",
        str(secret),
        "--source-commit",
        "a" * 40,
        "--run-id",
        "r24-smoke-cli-test",
        "--authorization-id",
        "owner-review-test",
        "--authorized-by",
        "owner",
        "--issued-at-utc",
        "2026-09-05T00:00:00Z",
        "--expires-at-utc",
        "2026-09-06T00:00:00Z",
        "--runtime-config-sha256",
        _sha(b"runtime"),
        "--qwen-snapshot-path",
        str(tmp_path / "models" / "qwen" / "snapshot"),
        "--qwen-snapshot-storage-root",
        str(tmp_path / "models" / "qwen"),
        "--qwen-snapshot-tree-sha256",
        _sha(b"qwen-tree"),
        "--qwen-snapshot-total-bytes",
        "100",
        "--qwen-snapshot-file-count",
        "2",
        "--qwen-actor-endpoint",
        "http://127.0.0.1:18081/v1",
        "--qwen-served-model-id",
        "qwen-smoke-model",
        "--qwen-smoke-task-id",
        "qwen-smoke-task",
        "--mai-snapshot-path",
        str(tmp_path / "models" / "mai" / "snapshot"),
        "--mai-snapshot-storage-root",
        str(tmp_path / "models" / "mai"),
        "--mai-snapshot-tree-sha256",
        _sha(b"mai-tree"),
        "--mai-snapshot-total-bytes",
        "200",
        "--mai-snapshot-file-count",
        "3",
        "--mai-actor-endpoint",
        "http://127.0.0.1:18082/v1",
        "--mai-served-model-id",
        "mai-smoke-model",
        "--mai-smoke-task-id",
        "mai-smoke-task",
        "--resource-preflight-wall-time-seconds",
        "100",
        "--qwen-to-mai-handoff-wall-time-seconds",
        "900",
        "--resource-cleanup-wall-time-seconds",
        "300",
        "--smoke-wall-time-seconds",
        "60",
        "--smoke-cost-usd-micros",
        "100",
    ]
    return common + fixture_arguments


def test_smoke_authority_builder_is_canonical_pilot_free_and_never_reads_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("build_r2_4_smoke_authority")
    secret = tmp_path / "openai.key"
    arguments = _builder_arguments(tmp_path)
    secret_info = secret.stat()
    real_read = module.os.read
    secret_read_count = 0

    def guarded_read(descriptor: int, count: int) -> bytes:
        nonlocal secret_read_count
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (secret_info.st_dev, secret_info.st_ino):
            secret_read_count += 1
            raise AssertionError("secret content read")
        return real_read(descriptor, count)

    monkeypatch.setattr(module.os, "read", guarded_read)
    assert module.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    raw = (tmp_path / "draft.json").read_bytes()
    manifest = parse_smoke_authority_manifest(json.loads(raw))
    projection = smoke_authority_manifest_projection(manifest)

    assert raw == canonical_json_bytes(cast(JsonValue, projection))
    assert stat.S_IMODE((tmp_path / "draft.json").stat().st_mode) == 0o600
    assert summary["manifest_sha256"] == smoke_authority_manifest_sha256(manifest)
    assert summary["pilot_authorized"] is False
    assert secret_read_count == 0
    assert manifest.authorization.status is RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED
    assert manifest.max_sequence_actor_calls == 6
    assert manifest.max_sequence_openai_calls == 12
    assert manifest.max_sequence_cost_usd_micros == 600
    assert manifest.max_sequence_wall_time_seconds == 1_660
    assert {"pilot", "cohort", "task_source"}.isdisjoint(projection)


def test_promotion_cli_schema_discriminates_smoke_and_changes_status_only(tmp_path: Path) -> None:
    builder = _load_script("build_r2_4_smoke_authority")
    assert builder.main(_builder_arguments(tmp_path)) == 0
    draft = parse_smoke_authority_manifest(json.loads((tmp_path / "draft.json").read_bytes()))
    draft_sha256 = smoke_authority_manifest_sha256(draft)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "promote_r2_4_r2_5_authority.py"),
            "--draft-manifest",
            str(tmp_path / "draft.json"),
            "--confirm-draft-sha256",
            draft_sha256,
            "--output",
            str(tmp_path / "owner.json"),
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--owner-approved",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["execution_scope"] == "R24_LIVE_SMOKE_ONLY"
    promoted = json.loads((tmp_path / "owner.json").read_bytes())
    expected = smoke_authority_manifest_projection(draft)
    expected["authorization"]["status"] = "OWNER_AUTHORIZED"
    assert promoted == expected


def _authorized_manifest(tmp_path: Path) -> object:
    builder = _load_script("build_r2_4_smoke_authority")
    assert builder.main(_builder_arguments(tmp_path)) == 0
    draft = parse_smoke_authority_manifest(json.loads((tmp_path / "draft.json").read_bytes()))
    return replace(
        draft,
        authorization=replace(
            draft.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )


def _pricing() -> LiveAttemptPricingV1:
    return LiveAttemptPricingV1(
        pricing_id="test-pricing",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1,
        cached_input_usd_micros_per_million_tokens=1,
        output_usd_micros_per_million_tokens=1,
        source_sha256=_sha(b"pricing-source"),
        effective_at_utc="2026-09-05T00:00:00Z",
    )


def _install_runner_fakes(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    manifest: object,
    *,
    execute: bool,
) -> tuple[list[str], str, str, str]:
    manifest_sha = smoke_authority_manifest_sha256(manifest)
    runtime_sha = manifest.runtime_config_sha256
    pricing = _pricing()
    pricing_sha = live_attempt_pricing_sha256(pricing)
    report = SimpleNamespace(eligible_for_post_preflight_factory=True)
    calls: list[str] = []
    fixture_root = Path(getattr(manifest, "smoke_plans")[0].cases[0].request_fixture_path).parent
    runtime = SimpleNamespace(
        resource_topology=ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED,
        qwen_gpu_index=5,
        mai_gpu_index=5,
        authorized_pilot_input_root=str(fixture_root),
    )
    monkeypatch.setattr(
        module, "load_owner_authorized_smoke_authority_v1", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        module,
        "_load_canonical_input",
        lambda path, **kwargs: (
            cast(JsonValue, {"runtime": True})
            if Path(path).name == "runtime.json"
            else cast(JsonValue, live_attempt_pricing_projection(pricing))
        ),
    )
    monkeypatch.setattr(module, "parse_production_runtime_config", lambda value: runtime)
    monkeypatch.setattr(module, "production_runtime_config_sha256", lambda value: runtime_sha)
    monkeypatch.setattr(module, "run_r24_smoke_production_preflight_v1", lambda *a, **k: report)
    monkeypatch.setattr(
        module, "r24_smoke_production_preflight_report_sha256", lambda value: "b" * 64
    )
    monkeypatch.setattr(
        module,
        "r24_smoke_production_preflight_report_projection",
        lambda value: {"all_checks_passed": True},
    )
    if execute:
        factory, resource, drivers, broker = object(), object(), object(), object()
        monkeypatch.setattr(
            module, "require_production_post_preflight_factory_v1", lambda *a, **k: factory
        )

        def build_resource(*args: object, **kwargs: object) -> object:
            calls.append("resource")
            return resource

        def build_drivers(**kwargs: object) -> object:
            assert kwargs["factory"] is factory
            assert kwargs["resource_lifecycle"] is resource
            calls.append("driver")
            return drivers

        def build_broker(value: object) -> object:
            assert value is factory
            calls.append("broker")
            return broker

        class _Executor:
            def execute(self, value: object) -> object:
                assert value is manifest
                calls.append("execute")
                return SimpleNamespace(status=SimpleNamespace(value="COMPLETE"))

        def build_executor(value: object, **kwargs: object) -> object:
            assert value is manifest
            assert kwargs["post_preflight_factory"] is factory
            assert kwargs["resource_adapter"] is resource
            assert kwargs["driver_adapters"] is drivers
            assert kwargs["case_authority_broker_provider"] is broker
            calls.append("smoke_executor")
            return _Executor()

        monkeypatch.setattr(
            module, "build_production_resource_lifecycle_adapter_v1", build_resource
        )
        monkeypatch.setattr(
            module,
            "_production_cleanup_bound_seconds",
            lambda *args, **kwargs: (8, b"bound", "0" * 64),
        )
        monkeypatch.setattr(
            module, "ExternalProductionRuntimeAuditSinkV1", lambda *a, **k: object()
        )
        monkeypatch.setattr(module, "build_production_driver_v1", build_drivers)
        monkeypatch.setattr(
            module, "build_production_case_authority_broker_provider_v1", build_broker
        )
        monkeypatch.setattr(module, "build_production_r24_smoke_executor_v1", build_executor)
        monkeypatch.setattr(
            module, "r24_smoke_sequence_result_projection", lambda value: {"pilot_executed": False}
        )
    return calls, manifest_sha, runtime_sha, pricing_sha


def test_smoke_runner_defaults_to_dry_run_and_has_no_pilot_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _authorized_manifest(tmp_path)
    module = _load_script("run_r2_4_smoke")
    calls, manifest_sha, runtime_sha, pricing_sha = _install_runner_fakes(
        module, monkeypatch, manifest, execute=False
    )
    source = (SCRIPTS / "run_r2_4_smoke.py").read_text(encoding="utf-8")
    assert "r2_5" not in source
    assert "build_production_executor_v1" not in source
    now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = module.main(
        [
            "--authority-manifest",
            str(tmp_path / "owner.json"),
            "--confirm-manifest-sha256",
            manifest_sha,
            "--runtime-config",
            str(tmp_path / "runtime.json"),
            "--confirm-runtime-config-sha256",
            runtime_sha,
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--confirm-pricing-sha256",
            pricing_sha,
            "--preflight-checked-at-utc",
            now,
        ]
    )
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 0
    assert output["dry_run"] is True
    assert output["pilot_reachable"] is False
    assert calls == []


@pytest.mark.parametrize(
    "fault",
    ["sibling_root", "hardlink_outside", "root_symlink", "intermediate_symlink", "leaf_symlink"],
)
def test_smoke_runner_rejects_unbound_fixture_path_before_preflight_or_resource_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    manifest = _authorized_manifest(tmp_path)
    module = _load_script("run_r2_4_smoke")
    _, manifest_sha, runtime_sha, pricing_sha = _install_runner_fakes(
        module, monkeypatch, manifest, execute=False
    )
    fixture_root = tmp_path / "fixtures"
    authorized_root = fixture_root
    fixture = fixture_root / "qwen-off.json"
    if fault == "sibling_root":
        authorized_root = tmp_path / "authorized-fixtures"
        authorized_root.mkdir()
    elif fault == "hardlink_outside":
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_fixture = outside / fixture.name
        fixture.rename(outside_fixture)
        os.link(outside_fixture, fixture)
    elif fault == "root_symlink":
        authorized_root = tmp_path / "fixture-root-alias"
        authorized_root.symlink_to(fixture_root, target_is_directory=True)
    elif fault == "intermediate_symlink":
        stored_fixtures = tmp_path / "stored-fixtures"
        fixture_root.rename(stored_fixtures)
        fixture_root.symlink_to(stored_fixtures, target_is_directory=True)
        authorized_root = tmp_path
    else:
        fixture.unlink()
        fixture.symlink_to("qwen-shadow.json")
    runtime = SimpleNamespace(
        resource_topology=ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED,
        qwen_gpu_index=5,
        mai_gpu_index=5,
        authorized_pilot_input_root=str(authorized_root),
    )
    monkeypatch.setattr(module, "parse_production_runtime_config", lambda value: runtime)
    forbidden_calls: list[str] = []

    def forbid(name: str):
        def forbidden(*args: object, **kwargs: object) -> object:
            forbidden_calls.append(name)
            raise AssertionError(f"unexpected {name}")

        return forbidden

    monkeypatch.setattr(
        module,
        "run_r24_smoke_production_preflight_v1",
        forbid("preflight"),
    )
    monkeypatch.setattr(
        module,
        "build_production_resource_lifecycle_adapter_v1",
        forbid("resource"),
    )
    monkeypatch.setattr(
        module,
        "ExternalProductionRuntimeAuditSinkV1",
        forbid("audit"),
    )
    monkeypatch.setattr(
        module,
        "build_production_driver_v1",
        forbid("driver"),
    )
    now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = module.main(
        [
            "--authority-manifest",
            str(tmp_path / "owner.json"),
            "--confirm-manifest-sha256",
            manifest_sha,
            "--runtime-config",
            str(tmp_path / "runtime.json"),
            "--confirm-runtime-config-sha256",
            runtime_sha,
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--confirm-pricing-sha256",
            pricing_sha,
            "--preflight-checked-at-utc",
            now,
        ]
    )
    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert result == 2
    assert error["error_code"] == "SMOKE_FIXTURE_PATH_REJECTED"
    assert forbidden_calls == []


def test_smoke_runner_rejects_fixture_component_swap_after_preflight_before_resource_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _authorized_manifest(tmp_path)
    module = _load_script("run_r2_4_smoke")
    calls, manifest_sha, runtime_sha, pricing_sha = _install_runner_fakes(
        module, monkeypatch, manifest, execute=True
    )
    fixture_root = tmp_path / "fixtures"
    stored_fixtures = tmp_path / "stored-fixtures"

    def swap_component(*args: object, **kwargs: object) -> object:
        fixture_root.rename(stored_fixtures)
        fixture_root.symlink_to(stored_fixtures, target_is_directory=True)
        return SimpleNamespace(eligible_for_post_preflight_factory=True)

    audit_calls: list[Path] = []
    monkeypatch.setattr(module, "run_r24_smoke_production_preflight_v1", swap_component)
    monkeypatch.setattr(
        module,
        "ExternalProductionRuntimeAuditSinkV1",
        lambda path, **kwargs: audit_calls.append(path),
    )
    now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = module.main(
        [
            "--authority-manifest",
            str(tmp_path / "owner.json"),
            "--confirm-manifest-sha256",
            manifest_sha,
            "--runtime-config",
            str(tmp_path / "runtime.json"),
            "--confirm-runtime-config-sha256",
            runtime_sha,
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--confirm-pricing-sha256",
            pricing_sha,
            "--preflight-checked-at-utc",
            now,
            "--confirm-preflight-report-sha256",
            "b" * 64,
            "--production-audit-root",
            str(tmp_path / "audit"),
            "--execute",
        ]
    )
    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert result == 2
    assert error["error_code"] == "SMOKE_FIXTURE_PATH_REJECTED"
    assert calls == []
    assert audit_calls == []


def test_smoke_runner_execute_uses_one_exact_smoke_builder_identity_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _authorized_manifest(tmp_path)
    module = _load_script("run_r2_4_smoke")
    calls, manifest_sha, runtime_sha, pricing_sha = _install_runner_fakes(
        module, monkeypatch, manifest, execute=True
    )
    now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = module.main(
        [
            "--authority-manifest",
            str(tmp_path / "owner.json"),
            "--confirm-manifest-sha256",
            manifest_sha,
            "--runtime-config",
            str(tmp_path / "runtime.json"),
            "--confirm-runtime-config-sha256",
            runtime_sha,
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--confirm-pricing-sha256",
            pricing_sha,
            "--preflight-checked-at-utc",
            now,
            "--confirm-preflight-report-sha256",
            "b" * 64,
            "--production-audit-root",
            str(tmp_path / "audit"),
            "--execute",
        ]
    )
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 0
    assert calls == ["resource", "driver", "broker", "smoke_executor", "execute"]
    assert output["result"]["pilot_executed"] is False


def test_smoke_runner_rejects_cleanup_reserve_before_audit_root_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _authorized_manifest(tmp_path)
    module = _load_script("run_r2_4_smoke")
    _, manifest_sha, runtime_sha, pricing_sha = _install_runner_fakes(
        module, monkeypatch, manifest, execute=True
    )
    audit_calls: list[Path] = []
    monkeypatch.setattr(
        module,
        "_production_cleanup_bound_seconds",
        lambda *args, **kwargs: (
            manifest.max_resource_cleanup_wall_time_seconds + 1,
            b"sealed-preimage",
            "c" * 64,
        ),
    )
    monkeypatch.setattr(
        module,
        "ExternalProductionRuntimeAuditSinkV1",
        lambda path, **kwargs: audit_calls.append(path),
    )
    now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    audit_root = tmp_path / "audit-must-not-exist"
    result = module.main(
        [
            "--authority-manifest",
            str(tmp_path / "owner.json"),
            "--confirm-manifest-sha256",
            manifest_sha,
            "--runtime-config",
            str(tmp_path / "runtime.json"),
            "--confirm-runtime-config-sha256",
            runtime_sha,
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--confirm-pricing-sha256",
            pricing_sha,
            "--preflight-checked-at-utc",
            now,
            "--confirm-preflight-report-sha256",
            "b" * 64,
            "--production-audit-root",
            str(audit_root),
            "--execute",
        ]
    )
    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert result == 2
    assert error["error_code"] == "INSUFFICIENT_RESOURCE_CLEANUP_RESERVE"
    assert audit_calls == []
    assert not audit_root.exists()


def test_smoke_runner_secure_owner_loader_rejects_draft_before_other_inputs(
    tmp_path: Path,
) -> None:
    builder = _load_script("build_r2_4_smoke_authority")
    assert builder.main(_builder_arguments(tmp_path)) == 0
    draft = parse_smoke_authority_manifest(json.loads((tmp_path / "draft.json").read_bytes()))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_r2_4_smoke.py"),
            "--authority-manifest",
            str(tmp_path / "draft.json"),
            "--confirm-manifest-sha256",
            smoke_authority_manifest_sha256(draft),
            "--runtime-config",
            str(tmp_path / "absent-runtime.json"),
            "--confirm-runtime-config-sha256",
            "0" * 64,
            "--pricing",
            str(tmp_path / "absent-pricing.json"),
            "--confirm-pricing-sha256",
            "0" * 64,
            "--preflight-checked-at-utc",
            "2026-09-05T00:00:00Z",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error_code"] == "OWNER_AUTHORITY_REQUIRED"


@pytest.mark.parametrize("fault", ["chmod", "path_swap", "hardlink"])
def test_smoke_runner_execution_input_loader_rejects_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    module = _load_script("run_r2_4_smoke")
    source = tmp_path / "input.json"
    source.write_bytes(canonical_json_bytes({"value": 1}))
    source.chmod(0o600)
    if fault == "hardlink":
        alias = tmp_path / "alias.json"
        os.link(source, alias)
        with pytest.raises(module._CliError, match="INVALID_EXECUTION_INPUT"):
            module._load_canonical_input(source)
        return

    real_read = module.os.read
    fired = False

    def mutate_after_read(descriptor: int, count: int) -> bytes:
        nonlocal fired
        chunk = real_read(descriptor, count)
        if chunk and not fired:
            fired = True
            if fault == "chmod":
                source.chmod(0o640)
            else:
                moved = tmp_path / "moved.json"
                source.rename(moved)
                source.symlink_to(moved)
        return chunk

    monkeypatch.setattr(module.os, "read", mutate_after_read)
    with pytest.raises(module._CliError, match="INVALID_EXECUTION_INPUT"):
        module._load_canonical_input(source)
    assert fired


@pytest.mark.parametrize("alias_kind", ["exact", "normalized", "hardlink"])
def test_smoke_runner_rejects_secret_inode_as_execution_input_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_kind: str,
) -> None:
    module = _load_script("run_r2_4_smoke")
    secret = tmp_path / "secret.json"
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    secret.chmod(0o600)
    pin = module._pin_secret_metadata(secret)
    subdirectory = tmp_path / "subdirectory"
    subdirectory.mkdir()
    if alias_kind == "normalized":
        candidate = subdirectory / ".." / secret.name
    elif alias_kind == "hardlink":
        candidate = tmp_path / "secret-hardlink.json"
        os.link(secret, candidate)
    else:
        candidate = secret
    secret_dev_ino = cast(tuple[int, int], pin.identity[:2])
    real_read = module.os.read
    secret_reads = 0

    def guarded_read(descriptor: int, count: int) -> bytes:
        nonlocal secret_reads
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == secret_dev_ino:
            secret_reads += 1
            raise AssertionError("secret content read")
        return real_read(descriptor, count)

    monkeypatch.setattr(module.os, "read", guarded_read)
    try:
        with pytest.raises(module._CliError):
            module._load_canonical_input(
                candidate,
                forbidden_identity=secret_dev_ino,
            )
    finally:
        os.close(pin.descriptor)
    assert secret_reads == 0


def test_smoke_runner_rejects_replaced_secret_path_before_reading_new_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("run_r2_4_smoke")
    secret = tmp_path / "secret.json"
    secret.write_bytes(canonical_json_bytes({"secret": "inode-a"}))
    secret.chmod(0o600)
    pin = module._pin_secret_metadata(secret)
    held_identity = cast(tuple[int, int], pin.identity[:2])
    old_secret = tmp_path / "secret-inode-a.json"
    secret.rename(old_secret)
    secret.write_bytes(canonical_json_bytes({"secret": "inode-b"}))
    secret.chmod(0o600)
    replacement = secret.stat()
    assert (replacement.st_dev, replacement.st_ino) != held_identity
    real_read = module.os.read
    replacement_reads = 0

    def guarded_read(descriptor: int, count: int) -> bytes:
        nonlocal replacement_reads
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (replacement.st_dev, replacement.st_ino):
            replacement_reads += 1
            raise AssertionError("replacement secret content read")
        return real_read(descriptor, count)

    monkeypatch.setattr(module.os, "read", guarded_read)
    try:
        with pytest.raises(module._CliError, match="SECRET_METADATA_DRIFT"):
            module._load_canonical_input(
                secret,
                forbidden_identity=held_identity,
                secret_pin=pin,
            )
    finally:
        os.close(pin.descriptor)
    assert replacement_reads == 0


def test_smoke_authority_builder_rejects_noncanonical_or_wrong_host_fixture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("build_r2_4_smoke_authority")
    arguments = _builder_arguments(tmp_path)
    qwen_fixture = tmp_path / "fixtures" / "qwen-off.json"
    value = json.loads(qwen_fixture.read_bytes())
    value["codec_id"] = "mobileworld.g1.history-codec.mai-raw-replay"
    qwen_fixture.write_text(json.dumps(value, indent=2), encoding="utf-8")

    assert module.main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == ("SMOKE_FIXTURE_SCHEMA_REJECTED")
    assert not (tmp_path / "draft.json").exists()


def test_smoke_authority_builder_rejects_fixture_for_another_served_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("build_r2_4_smoke_authority")
    arguments = _builder_arguments(tmp_path)
    fixture = tmp_path / "fixtures" / "qwen-off.json"
    value = json.loads(fixture.read_bytes())
    request = value["application_request"]
    request["model"] = "surrogate-model-not-authorized-for-live-smoke"
    request_sha256 = _sha(canonical_json_bytes(cast(JsonValue, request)))
    value["fixture_request_sha256"] = request_sha256
    for binding in value["curated_span_bindings"]:
        binding["source_request_sha256"] = request_sha256
    fixture.write_bytes(canonical_json_bytes(cast(JsonValue, value)))

    assert module.main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == ("SMOKE_FIXTURE_SCHEMA_REJECTED")
    assert not (tmp_path / "draft.json").exists()


def test_smoke_authority_builder_rejects_stale_curated_request_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("build_r2_4_smoke_authority")
    arguments = _builder_arguments(tmp_path)
    fixture = tmp_path / "fixtures" / "qwen-off.json"
    value = json.loads(fixture.read_bytes())
    value["curated_span_bindings"][0]["source_request_sha256"] = "0" * 64
    fixture.write_bytes(canonical_json_bytes(cast(JsonValue, value)))

    assert module.main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == ("SMOKE_FIXTURE_SCHEMA_REJECTED")
    assert not (tmp_path / "draft.json").exists()


def test_smoke_authority_builder_rejects_gif_disguised_as_png(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("build_r2_4_smoke_authority")
    arguments = _builder_arguments(tmp_path)
    fixture = tmp_path / "fixtures" / "qwen-off.json"
    value = json.loads(fixture.read_bytes())
    request = value["application_request"]
    for message in request["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "image_url":
                block["image_url"]["url"] = (
                    "data:image/png;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
                )
    value["fixture_request_sha256"] = _sha(canonical_json_bytes(cast(JsonValue, request)))
    fixture.write_bytes(canonical_json_bytes(cast(JsonValue, value)))

    assert module.main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == ("SMOKE_FIXTURE_SCHEMA_REJECTED")
    assert not (tmp_path / "draft.json").exists()
