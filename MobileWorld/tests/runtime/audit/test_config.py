from pathlib import Path

import pytest

from mobile_world.runtime.audit.config import (
    DEFAULT_AUDIT_CONFIG,
    AuditConfig,
    CollectorMode,
)


def test_default_config_is_disabled_and_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    audit_root = tmp_path / "logs" / "audit_raw"

    config = AuditConfig()

    assert config == DEFAULT_AUDIT_CONFIG
    assert config.enabled is False
    assert config.enable_audit is False
    assert config.collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert config.store_stream_chunks is True
    assert config.resolve_log_root(tmp_path / "logs") == audit_root
    assert not audit_root.exists()


def test_cli_values_are_normalized_without_creating_the_output_root(tmp_path: Path) -> None:
    audit_root = tmp_path / "outside-repository"

    config = AuditConfig.from_cli_values(
        enable_audit=True,
        audit_log_root=str(audit_root),
        audit_store_stream_chunks=False,
    )

    assert config.enabled is True
    assert config.log_root == audit_root
    assert config.audit_collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert config.audit_store_stream_chunks is False
    assert config.to_manifest_config() == {
        "audit_enabled": True,
        "audit_log_root": str(audit_root),
        "audit_collector_mode": "fail_open_with_incomplete_marker",
        "audit_store_stream_chunks": False,
    }
    assert not audit_root.exists()


def test_enabled_collection_requires_an_explicit_raw_root() -> None:
    config = AuditConfig(enabled=True)

    with pytest.raises(ValueError, match="audit_log_root must be explicit"):
        config.validated_external_log_root(Path("/tmp/repository"))


def test_enabled_root_must_be_outside_and_must_not_contain_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()
    outside = tmp_path / "audit-data"

    config = AuditConfig(enabled=True, log_root=outside)
    assert config.validated_external_log_root(repository) == outside.resolve()
    assert not outside.exists()

    with pytest.raises(ValueError, match="outside the Git repository"):
        AuditConfig(enabled=True, log_root=repository / "raw").validated_external_log_root(
            repository
        )
    with pytest.raises(ValueError, match="must not contain"):
        AuditConfig(enabled=True, log_root=tmp_path).validated_external_log_root(repository)


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"enabled": 1}, TypeError),
        ({"store_stream_chunks": 1}, TypeError),
        ({"collector_mode": "unknown"}, ValueError),
        ({"log_root": ""}, ValueError),
    ],
)
def test_invalid_config_is_rejected(kwargs: dict[str, object], exception: type[Exception]) -> None:
    with pytest.raises(exception):
        AuditConfig(**kwargs)
