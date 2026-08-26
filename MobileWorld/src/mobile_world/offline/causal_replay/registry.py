"""Explicit registries for history-family and provider codecs."""

from __future__ import annotations

from collections.abc import Callable

from mobile_world.offline.causal_replay.contracts import (
    HistoryFamily,
    JsonValue,
    PortableContractError,
    ProviderCodec,
    canonical_sha256,
)
from mobile_world.offline.causal_replay.core import validate_codec_capabilities
from mobile_world.offline.causal_replay.history_codec import HistoryCodec


class HistoryCodecRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, tuple[HistoryFamily, str, str, Callable[[], HistoryCodec]]] = {}
        self._by_family_version: dict[tuple[HistoryFamily, str], str] = {}

    def register(self, codec: HistoryCodec) -> None:
        self.register_factory(
            history_family=codec.history_family,
            contract_version=codec.contract_version,
            codec_id=codec.codec_id,
            factory=lambda: codec,
        )

    def register_factory(
        self,
        *,
        history_family: HistoryFamily,
        contract_version: str,
        codec_id: str,
        factory: Callable[[], HistoryCodec],
    ) -> None:
        if codec_id in self._by_id:
            raise PortableContractError("DUPLICATE_CODEC_ID", "history codec ID already exists")
        key = (history_family, contract_version)
        if key in self._by_family_version:
            raise PortableContractError(
                "DUPLICATE_FAMILY_CODEC",
                "one registry may expose one codec per family/contract version",
            )
        candidate = factory()
        if (
            candidate.codec_id != codec_id
            or candidate.history_family is not history_family
            or candidate.contract_version != contract_version
            or candidate.capabilities.contract_version != contract_version
        ):
            raise PortableContractError(
                "CODEC_FACTORY_MISMATCH", "factory product differs from its registry key"
            )
        validate_codec_capabilities(candidate.capabilities)
        capability_sha256 = canonical_sha256(candidate.capabilities.to_dict())
        self._by_id[codec_id] = (
            history_family,
            contract_version,
            capability_sha256,
            factory,
        )
        self._by_family_version[key] = codec_id

    def by_id(self, codec_id: str, contract_version: str = "v1") -> HistoryCodec:
        try:
            family, version, capability_sha256, factory = self._by_id[codec_id]
        except KeyError as exc:
            raise PortableContractError("UNKNOWN_CODEC", "history codec is not registered") from exc
        if version != contract_version:
            raise PortableContractError(
                "UNKNOWN_CODEC_VERSION", "history codec contract version is not registered"
            )
        candidate = factory()
        if (
            candidate.codec_id != codec_id
            or candidate.history_family is not family
            or candidate.contract_version != version
            or candidate.capabilities.contract_version != version
            or canonical_sha256(candidate.capabilities.to_dict()) != capability_sha256
        ):
            raise PortableContractError(
                "CODEC_FACTORY_DRIFT",
                "resolved codec differs from its immutable registry declaration",
            )
        validate_codec_capabilities(candidate.capabilities)
        return candidate

    def by_family(self, family: HistoryFamily, contract_version: str = "v1") -> HistoryCodec:
        try:
            return self.by_id(self._by_family_version[(family, contract_version)])
        except KeyError as exc:
            raise PortableContractError("UNKNOWN_HISTORY_FAMILY", "family has no codec") from exc

    def manifest(self) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            self.by_id(codec_id, declaration[1]).capabilities.to_dict()
            for codec_id, declaration in sorted(self._by_id.items())
        )


class ProviderCodecRegistry:
    def __init__(self) -> None:
        self._by_id: dict[tuple[str, str], Callable[[], ProviderCodec]] = {}

    def register(self, codec: ProviderCodec) -> None:
        self.register_factory(
            codec_id=codec.codec_id,
            contract_version=codec.contract_version,
            factory=lambda: codec,
        )

    def register_factory(
        self,
        *,
        codec_id: str,
        contract_version: str,
        factory: Callable[[], ProviderCodec],
    ) -> None:
        key = (codec_id, contract_version)
        if key in self._by_id:
            raise PortableContractError("DUPLICATE_CODEC_ID", "provider codec ID already exists")
        candidate = factory()
        if candidate.codec_id != codec_id or candidate.contract_version != contract_version:
            raise PortableContractError(
                "CODEC_FACTORY_MISMATCH", "factory product differs from its registry key"
            )
        self._by_id[key] = factory

    def by_id(self, codec_id: str, contract_version: str = "v1") -> ProviderCodec:
        try:
            factory = self._by_id[(codec_id, contract_version)]
        except KeyError as exc:
            raise PortableContractError(
                "UNKNOWN_PROVIDER_CODEC", "provider codec is not registered"
            ) from exc
        candidate = factory()
        if candidate.codec_id != codec_id or candidate.contract_version != contract_version:
            raise PortableContractError(
                "CODEC_FACTORY_DRIFT",
                "resolved provider codec differs from its immutable registry key",
            )
        return candidate

    def codec_ids(self) -> tuple[str, ...]:
        return tuple(sorted(codec_id for codec_id, _version in self._by_id))
