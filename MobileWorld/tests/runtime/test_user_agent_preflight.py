from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_world.core.api import env as env_api
from mobile_world.core.subcommands import env as env_cli
from mobile_world.runtime.user_agent_config import (
    UserAgentConfigurationError,
    validate_user_agent_config,
    validate_user_agent_env_file,
)
from mobile_world.runtime.utils.models import ContainerConfig


def _valid_config() -> dict[str, str]:
    return {
        "USER_AGENT_API_KEY": "fixture-secret-value",
        "USER_AGENT_BASE_URL": "https://example.invalid/v1",
        "USER_AGENT_MODEL": "fixture-model",
    }


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def _parse_env_run(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    env_cli.configure_parser(subparsers)
    return parser.parse_args(["env", "run", *arguments])


def test_config_validation_accepts_complete_openai_compatible_configuration() -> None:
    validate_user_agent_config(_valid_config())


@pytest.mark.parametrize(
    "updates",
    [
        {"USER_AGENT_API_KEY": ""},
        {"USER_AGENT_API_KEY": "your_user_agent_llm_api_key"},
        {"USER_AGENT_BASE_URL": "your_user_agent_base_url"},
        {"USER_AGENT_BASE_URL": "not-a-url"},
        {"USER_AGENT_MODEL": "   "},
    ],
)
def test_config_validation_rejects_invalid_values_without_echoing_them(
    updates: dict[str, str],
) -> None:
    values = {**_valid_config(), **updates}

    with pytest.raises(UserAgentConfigurationError) as raised:
        validate_user_agent_config(values)

    message = str(raised.value)
    assert "preflight failed" in message
    assert "fixture-secret-value" not in message
    assert "https://example.invalid/v1" not in message
    assert "not-a-url" not in message


def test_env_file_validation_is_cpu_only_and_secret_safe(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.env"
    invalid_path = tmp_path / "invalid.env"
    _write_env(valid_path, _valid_config())
    _write_env(
        invalid_path,
        {
            **_valid_config(),
            "USER_AGENT_BASE_URL": "invalid-base-url-fixture",
        },
    )

    validate_user_agent_env_file(valid_path)
    with pytest.raises(UserAgentConfigurationError) as raised:
        validate_user_agent_env_file(invalid_path)

    assert "invalid-base-url-fixture" not in str(raised.value)
    assert "fixture-secret-value" not in str(raised.value)


def test_api_launch_rejects_bad_config_before_any_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "bad.env"
    _write_env(env_path, {"USER_AGENT_MODEL": "fixture-model"})
    docker_called = False

    def unexpected_docker_call(*args: object, **kwargs: object) -> object:
        nonlocal docker_called
        docker_called = True
        raise AssertionError("Docker must not be called after a failed preflight")

    monkeypatch.setattr(env_api, "run_command", unexpected_docker_call)
    config = ContainerConfig(
        name="fixture_container",
        backend_port=16800,
        viewer_port=17860,
        vnc_port=15800,
        adb_port=15556,
        env_file_path=env_path,
    )

    result = env_api.launch_container(config, wait_ready=False)

    assert result.success is False
    assert "USER_AGENT_API_KEY" in (result.error_message or "")
    assert docker_called is False


def test_api_launch_requires_env_file_before_any_docker_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_called = False

    def unexpected_docker_call(*args: object, **kwargs: object) -> object:
        nonlocal docker_called
        docker_called = True
        raise AssertionError("Docker must not be called without simulated-user config")

    monkeypatch.setattr(env_api, "run_command", unexpected_docker_call)
    config = ContainerConfig(
        name="fixture_container",
        backend_port=16800,
        viewer_port=17860,
        vnc_port=15800,
        adb_port=15556,
        env_file_path=None,
    )

    result = env_api.launch_container(config, wait_ready=False)

    assert result.success is False
    assert "environment file" in (result.error_message or "")
    assert docker_called is False


def test_cli_explicit_env_file_overrides_cwd_dotenv_without_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implicit_path = tmp_path / ".env"
    explicit_path = tmp_path / "formal-user-agent.env"
    _write_env(implicit_path, {"USER_AGENT_MODEL": "invalid-implicit-config"})
    _write_env(explicit_path, _valid_config())
    captured_volumes: list[list[tuple[str, str]]] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_cli,
        "find_available_ports",
        lambda *args, **kwargs: [(16800, 17860, 15800, 15556)],
    )
    monkeypatch.setattr(env_cli, "find_next_container_index", lambda *args, **kwargs: 0)

    def fake_build_run_command(*args: object, **kwargs: object) -> list[str]:
        captured_volumes.append(kwargs["volumes"])  # type: ignore[arg-type]
        return ["docker", "fixture"]

    monkeypatch.setattr(env_cli, "build_run_command", fake_build_run_command)
    args = _parse_env_run("--dry-run", "--env-file", str(explicit_path))

    env_cli._launch_containers(args)

    assert captured_volumes == [[(str(explicit_path.resolve()), "/app/service/.env")]]


def test_backend_preflight_runs_before_task_registry_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mobile_world.core import server

    calls: list[str] = []
    monkeypatch.setattr(
        server,
        "validate_user_agent_environment",
        lambda: calls.append("preflight"),
    )
    monkeypatch.setattr(
        server,
        "TaskRegistry",
        lambda: calls.append("registry") or SimpleNamespace(tasks=[]),
    )

    server.initialize_suite_family("mobile_world")

    assert calls == ["preflight", "registry"]
