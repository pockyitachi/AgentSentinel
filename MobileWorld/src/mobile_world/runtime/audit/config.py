"""Configuration primitives for the opt-in runtime audit collector."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CollectorMode(StrEnum):
    """The sole runtime failure policy allowed by the v1 handoff contract."""

    FAIL_OPEN_WITH_INCOMPLETE_MARKER = "fail_open_with_incomplete_marker"


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """Resolved collector configuration without side effects.

    Constructing or resolving this object never creates a directory.  The
    default is deliberately disabled so importing MobileWorld cannot enable
    audit collection implicitly.
    """

    enabled: bool = False
    log_root: Path | None = None
    collector_mode: CollectorMode | str = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    store_stream_chunks: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if not isinstance(self.store_stream_chunks, bool):
            raise TypeError("store_stream_chunks must be a bool")

        try:
            mode = CollectorMode(self.collector_mode)
        except ValueError as exc:
            choices = ", ".join(mode.value for mode in CollectorMode)
            raise ValueError(f"collector_mode must be one of: {choices}") from exc
        object.__setattr__(self, "collector_mode", mode)

        if self.log_root is not None:
            if isinstance(self.log_root, str) and not self.log_root.strip():
                raise ValueError("log_root must not be empty")
            object.__setattr__(self, "log_root", Path(self.log_root))

    @classmethod
    def from_cli_values(
        cls,
        *,
        enable_audit: bool = False,
        audit_log_root: str | Path | None = None,
        audit_store_stream_chunks: bool = True,
    ) -> AuditConfig:
        """Map the documented CLI names to the internal immutable config."""

        return cls(
            enabled=enable_audit,
            log_root=Path(audit_log_root) if audit_log_root is not None else None,
            collector_mode=CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER,
            store_stream_chunks=audit_store_stream_chunks,
        )

    @classmethod
    def disabled(cls) -> AuditConfig:
        """Return the canonical default-off configuration."""

        return cls()

    def resolve_log_root(self, log_file_root: str | Path) -> Path:
        """Resolve the audit root without touching the filesystem."""

        if self.log_root is not None:
            return self.log_root
        return Path(log_file_root) / "audit_raw"

    def validated_external_log_root(self, repository_root: str | Path) -> Path:
        """Return the resolved audit root after proving it is outside the repository.

        This method is intended only for the enabled bootstrap path.  It also
        rejects a broad ancestor of the repository, so a configuration typo
        cannot select a workspace-owning directory as the raw evidence root.
        """

        if not self.enabled:
            raise ValueError("external audit root validation requires enabled collection")
        if self.log_root is None:  # Kept defensive if construction changes later.
            raise ValueError("audit_log_root must be explicit when audit collection is enabled")

        candidate = self.log_root.expanduser().resolve(strict=False)
        repository = Path(repository_root).expanduser().resolve(strict=False)
        if candidate == repository or _is_relative_to(candidate, repository):
            raise ValueError("audit_log_root must be outside the Git repository")
        if _is_relative_to(repository, candidate):
            raise ValueError("audit_log_root must not contain the Git repository")
        return candidate

    def to_manifest_config(self) -> dict[str, bool | str | None]:
        """Return non-secret resolved values suitable for a run manifest."""

        return {
            "audit_enabled": self.enabled,
            "audit_log_root": str(self.log_root) if self.log_root is not None else None,
            "audit_collector_mode": self.collector_mode.value,
            "audit_store_stream_chunks": self.store_stream_chunks,
        }

    # CLI-spelled read-only aliases make integration explicit while retaining
    # concise internal field names.
    @property
    def enable_audit(self) -> bool:
        return self.enabled

    @property
    def audit_log_root(self) -> Path | None:
        return self.log_root

    @property
    def audit_collector_mode(self) -> CollectorMode:
        return CollectorMode(self.collector_mode)

    @property
    def audit_store_stream_chunks(self) -> bool:
        return self.store_stream_chunks


DEFAULT_AUDIT_CONFIG = AuditConfig()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
