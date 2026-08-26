"""Secret-safe preflight checks for the simulated-user LLM configuration."""

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv

REQUIRED_USER_AGENT_ENV_KEYS = (
    "USER_AGENT_API_KEY",
    "USER_AGENT_BASE_URL",
    "USER_AGENT_MODEL",
)

_PLACEHOLDER_VALUES = {
    "USER_AGENT_API_KEY": frozenset({"your_user_agent_llm_api_key"}),
    "USER_AGENT_BASE_URL": frozenset({"your_user_agent_base_url"}),
}


class UserAgentConfigurationError(RuntimeError):
    """Raised before a run when simulated-user configuration is unusable."""


def validate_user_agent_config(values: Mapping[str, str | None]) -> None:
    """Validate effective ``USER_AGENT_*`` values without exposing their contents."""

    missing = []
    placeholders = []
    for key in REQUIRED_USER_AGENT_ENV_KEYS:
        value = values.get(key)
        if value is None or not value.strip():
            missing.append(key)
        elif value.strip() in _PLACEHOLDER_VALUES.get(key, ()):
            placeholders.append(key)

    invalid = []
    base_url = values.get("USER_AGENT_BASE_URL")
    if base_url and base_url.strip() and "USER_AGENT_BASE_URL" not in placeholders:
        parsed_url = urlsplit(base_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            invalid.append("USER_AGENT_BASE_URL")

    problems = []
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if placeholders:
        problems.append(f"placeholder: {', '.join(placeholders)}")
    if invalid:
        problems.append(f"invalid: {', '.join(invalid)}")
    if problems:
        details = "; ".join(problems)
        raise UserAgentConfigurationError(
            "Simulated-user configuration preflight failed "
            f"({details}). No configuration values were logged."
        )


def validate_user_agent_env_file(env_file_path: Path) -> None:
    """Validate a dotenv file before it is mounted into a MobileWorld container."""

    path = Path(env_file_path)
    if not path.is_file():
        raise UserAgentConfigurationError(
            f"Simulated-user environment file is not a readable file: {path}"
        )
    try:
        values = dotenv_values(path)
    except Exception as exc:
        raise UserAgentConfigurationError(
            f"Could not read simulated-user environment file: {path}"
        ) from exc
    validate_user_agent_config(values)


def validate_user_agent_environment() -> None:
    """Validate the effective backend environment before the server accepts work."""

    load_dotenv(override=False)
    validate_user_agent_config(os.environ)
