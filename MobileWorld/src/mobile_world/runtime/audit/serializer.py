"""Side-effect-free, lossless serialization for raw audit artifacts.

The authoritative SDK-argument snapshot is a typed artifact graph.  Large or
binary leaves are content-addressed while the graph retains enough information
to reconstruct the canonical application-layer JSON view.  All traversal is
read-only with respect to the live request, response, or image object.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as datetime_module
import decimal
import enum
import hashlib
import importlib.metadata
import io
import json
import math
import pathlib
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from PIL import Image

from mobile_world.runtime.audit.blob_store import BlobRef, BlobStore

ARTIFACT_GRAPH_VERSION = "mobileworld.audit.artifact/v1"
ARTIFACT_GRAPH_MEDIA_TYPE = "application/vnd.mobileworld.audit.artifact+json"

_DATA_URL_PATTERN = re.compile(r"^data:(?P<metadata>[^,]*),(?P<payload>.*)$", re.DOTALL)
_REMOTE_URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SIGNED_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "key",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-signature",
        "x-amz-security-token",
        "x-goog-credential",
        "x-goog-signature",
    }
)


class SerializationError(ValueError):
    """Raised rather than silently claiming losslessness for an unsupported value."""


@dataclass(frozen=True)
class ArtifactSnapshot:
    """A stored authoritative graph plus its inspectable request representation."""

    snapshot_blob: BlobRef
    artifact_graph: dict[str, Any]
    request_view: Any
    request_images: tuple[dict[str, Any], ...]
    canonical_sha256: str
    canonical_byte_length: int
    serialization_fidelity: str
    warnings: tuple[str, ...]


@dataclass
class _SerializationState:
    request_images: list[dict[str, Any]]
    warnings: list[str]
    used_repr_fallback: bool = False


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes, rejecting NaN and Infinity."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SerializationError(f"value is not canonical-JSON serializable: {error}") from error
    return text.encode("utf-8")


class ArtifactSerializer:
    """Create and rehydrate typed content-addressed artifact graphs."""

    def __init__(
        self,
        blob_store: BlobStore,
        *,
        forbidden_values: Iterable[str | bytes] = (),
    ) -> None:
        self.blob_store = blob_store
        forbidden_bytes: list[bytes] = []
        values = (
            (forbidden_values,) if isinstance(forbidden_values, (str, bytes)) else forbidden_values
        )
        for value in values:
            if isinstance(value, str):
                encoded = value.encode("utf-8")
            elif isinstance(value, bytes):
                encoded = value
            else:
                raise TypeError("forbidden_values must contain only strings or bytes")
            if encoded and encoded not in forbidden_bytes:
                forbidden_bytes.append(encoded)
        self._forbidden_bytes = tuple(forbidden_bytes)

    def snapshot_sdk_arguments(self, arguments: Mapping[str, Any]) -> ArtifactSnapshot:
        """Snapshot the exact SDK argument mapping without mutating it."""

        if not isinstance(arguments, Mapping):
            raise TypeError("SDK arguments must be a mapping")
        return self.snapshot(arguments, root_path="")

    def snapshot(
        self,
        value: Any,
        *,
        root_path: str = "",
        allow_repr_fallback: bool = False,
    ) -> ArtifactSnapshot:
        """Store a typed artifact graph for *value*.

        Unsupported objects fail closed at the serialization boundary by
        default.  A caller collecting best-effort raw provider extensions may
        opt into a marked ``repr_fallback`` snapshot, which must make the
        surrounding task's ``capture_complete`` false.
        """

        state = _SerializationState(request_images=[], warnings=[])
        node, request_view, canonical_value = self._encode(
            value,
            path=root_path,
            state=state,
            allow_repr_fallback=allow_repr_fallback,
        )
        graph = {"artifact_graph_version": ARTIFACT_GRAPH_VERSION, "root": node}
        graph_bytes = canonical_json_bytes(graph)
        graph_ref = self.blob_store.put_bytes(graph_bytes, ARTIFACT_GRAPH_MEDIA_TYPE)

        canonical_bytes = canonical_json_bytes(canonical_value)
        rehydrated = self.rehydrate(graph)
        if canonical_json_bytes(rehydrated) != canonical_bytes:
            raise SerializationError("artifact graph failed its canonical rehydration check")

        return ArtifactSnapshot(
            snapshot_blob=graph_ref,
            artifact_graph=graph,
            request_view=request_view,
            request_images=tuple(state.request_images),
            canonical_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
            canonical_byte_length=len(canonical_bytes),
            serialization_fidelity="repr_fallback" if state.used_repr_fallback else "lossless",
            warnings=tuple(state.warnings),
        )

    def load_graph(self, reference: BlobRef) -> dict[str, Any]:
        """Load and validate a graph from its authoritative blob reference."""

        raw = self.blob_store.read_bytes(reference)
        try:
            graph = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda constant: (_raise_invalid_constant(constant)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SerializationError(f"artifact graph is not valid UTF-8 JSON: {error}") from error
        if not isinstance(graph, dict):
            raise SerializationError("artifact graph root must be a mapping")
        self._validate_graph(graph)
        return graph

    def rehydrate(self, graph_or_reference: dict[str, Any] | BlobRef) -> Any:
        """Reconstruct the canonical pre-artifactization application value."""

        if _looks_like_blob_ref(graph_or_reference):
            graph = self.load_graph(graph_or_reference)
        else:
            graph = graph_or_reference
            self._validate_graph(graph)
        return self._decode_node(graph["root"])

    def serialize_observation_image(
        self,
        image: Image.Image,
        *,
        source_bytes: bytes | None = None,
        source_media_type: str = "image/png",
    ) -> dict[str, Any]:
        """Store an exact runtime pixel matrix as a canonical lossless PNG."""

        if not isinstance(image, Image.Image):
            raise TypeError("image must be a PIL Image")

        before_mode = image.mode
        before_size = image.size
        before_pixels = image.tobytes()
        before_palette = image.getpalette() if image.mode == "P" else None

        buffer = io.BytesIO()
        image.copy().save(buffer, format="PNG")
        canonical_png = buffer.getvalue()
        self._reject_forbidden_bytes(canonical_png, path="observation.pixel_blob")
        if source_bytes is not None:
            self._reject_forbidden_bytes(source_bytes, path="observation.source_blob")

        try:
            with Image.open(io.BytesIO(canonical_png)) as restored:
                restored.load()
                if (
                    restored.mode != before_mode
                    or restored.size != before_size
                    or restored.tobytes() != before_pixels
                    or (before_palette is not None and restored.getpalette() != before_palette)
                ):
                    raise SerializationError(
                        "canonical PNG did not preserve the runtime pixel matrix"
                    )
        except SerializationError:
            raise
        except Exception as error:
            raise SerializationError(f"failed to validate canonical PNG: {error}") from error

        # Assert that serialization itself did not alter the live PIL object.
        if (
            image.mode != before_mode
            or image.size != before_size
            or image.tobytes() != before_pixels
        ):
            raise SerializationError("observation serialization mutated the live PIL image")

        pixel_blob = self.blob_store.put_bytes(canonical_png, "image/png")
        source_blob = (
            self.blob_store.put_bytes(source_bytes, source_media_type)
            if source_bytes is not None
            else None
        )
        return {
            "pixel_blob": pixel_blob,
            "source_blob": source_blob,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "representation": "canonical_png_from_runtime_pixels",
        }

    def _encode(
        self,
        value: Any,
        *,
        path: str,
        state: _SerializationState,
        allow_repr_fallback: bool,
    ) -> tuple[dict[str, Any], Any, Any]:
        if value is None:
            return {"node_type": "null"}, None, None
        if isinstance(value, bool):
            return {"node_type": "bool", "value": value}, value, value
        if isinstance(value, int):
            return {"node_type": "int", "value": value}, value, value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise SerializationError(f"non-finite float at {path or '<root>'}")
            return {"node_type": "float", "value": value}, value, value
        if isinstance(value, str):
            self._reject_forbidden_bytes(value.encode("utf-8"), path=path)
            if _contains_signed_url_query(value):
                raise SerializationError(
                    f"signed URL credential was excluded at {path or '<root>'}"
                )
            data_url = self._externalize_data_url(value, path=path, state=state)
            if data_url is not None:
                node, request_view = data_url
                return node, request_view, value
            if _REMOTE_URL_PATTERN.match(value) and _is_image_content_path(path):
                state.request_images.append(
                    {
                        "content_path": path,
                        "original_url": value,
                        "original_text_blob": None,
                        "content_blob": None,
                        "media_type": None,
                        "width": None,
                        "height": None,
                        "capture_status": "url_preserved_content_unavailable",
                    }
                )
            return {"node_type": "string", "value": value}, value, value
        if isinstance(value, (bytes, bytearray, memoryview)):
            exact_bytes = bytes(value)
            self._reject_forbidden_bytes(exact_bytes, path=path)
            blob = self.blob_store.put_bytes(exact_bytes, "application/octet-stream")
            original_type = type(value).__name__
            typed = {
                "$typed_value": {
                    "kind": original_type,
                    "base64": base64.b64encode(exact_bytes).decode("ascii"),
                }
            }
            node = {
                "node_type": "binary",
                "original_type": original_type,
                "blob": blob,
            }
            view = {"$externalized_blob": {"blob": blob, "original_type": original_type}}
            return node, view, typed

        if isinstance(value, Mapping):
            graph_items: list[dict[str, Any]] = []
            view_mapping: dict[str, Any] = {}
            canonical_mapping: dict[str, Any] = {}
            has_non_string_key = False
            for key, item in value.items():
                key_node, _key_view, _key_canonical = self._encode(
                    key,
                    path=f"{path}.<key>" if path else "<key>",
                    state=state,
                    allow_repr_fallback=allow_repr_fallback,
                )
                item_path = _mapping_path(path, key)
                item_node, item_view, item_canonical = self._encode(
                    item,
                    path=item_path,
                    state=state,
                    allow_repr_fallback=allow_repr_fallback,
                )
                graph_items.append({"key": key_node, "value": item_node})
                if isinstance(key, str):
                    view_mapping[key] = item_view
                    canonical_mapping[key] = item_canonical
                else:
                    has_non_string_key = True

            node = {
                "node_type": "mapping",
                "class": _qualified_class_name(value),
                "items": graph_items,
            }
            if has_non_string_key:
                # SDK arguments should normally never reach this branch.  The
                # typed representation keeps unusual mappings reconstructable.
                all_view_items = []
                all_canonical_items = []
                for graph_item in graph_items:
                    all_view_items.append(
                        {
                            "key": self._node_to_view(graph_item["key"]),
                            "value": self._node_to_view(graph_item["value"]),
                        }
                    )
                    all_canonical_items.append(
                        {
                            "key": self._decode_node(graph_item["key"]),
                            "value": self._decode_node(graph_item["value"]),
                        }
                    )
                typed_view = {"$typed_mapping": all_view_items}
                typed_canonical = {"$typed_mapping": all_canonical_items}
                return node, typed_view, typed_canonical
            return node, view_mapping, canonical_mapping

        if isinstance(value, list):
            return self._encode_sequence(
                value,
                kind="list",
                path=path,
                state=state,
                allow_repr_fallback=allow_repr_fallback,
            )
        if isinstance(value, tuple):
            node, item_views, item_canonical = self._encode_sequence(
                value,
                kind="tuple",
                path=path,
                state=state,
                allow_repr_fallback=allow_repr_fallback,
            )
            return (
                node,
                {"$typed_value": {"kind": "tuple", "items": item_views}},
                {"$typed_value": {"kind": "tuple", "items": item_canonical}},
            )

        official = self._official_object_dump(value)
        if official is not None:
            serializer_name, dumped = official
            dumped_node, dumped_view, dumped_canonical = self._encode(
                dumped,
                path=path,
                state=state,
                allow_repr_fallback=allow_repr_fallback,
            )
            node = {
                "node_type": "serialized_object",
                "class": _qualified_class_name(value),
                "package_version": _package_version(value),
                "serializer": serializer_name,
                "value": dumped_node,
            }
            return node, dumped_view, dumped_canonical

        scalar_typed = self._typed_scalar(value)
        if scalar_typed is not None:
            kind, scalar_value = scalar_typed
            if isinstance(scalar_value, str):
                self._reject_forbidden_bytes(scalar_value.encode("utf-8"), path=path)
            typed = {
                "$typed_value": {
                    "kind": kind,
                    "class": _qualified_class_name(value),
                    "value": scalar_value,
                }
            }
            node = {
                "node_type": "typed_scalar",
                "kind": kind,
                "class": _qualified_class_name(value),
                "package_version": _package_version(value),
                "value": scalar_value,
            }
            return node, typed, typed

        if allow_repr_fallback:
            representation = repr(value)
            self._reject_forbidden_bytes(representation.encode("utf-8"), path=path)
            state.used_repr_fallback = True
            state.warnings.append(
                f"repr fallback for {_qualified_class_name(value)} at {path or '<root>'}"
            )
            typed = {
                "$typed_value": {
                    "kind": "repr_fallback",
                    "class": _qualified_class_name(value),
                    "value": representation,
                }
            }
            node = {
                "node_type": "repr_fallback",
                "class": _qualified_class_name(value),
                "package_version": _package_version(value),
                "value": representation,
            }
            return node, typed, typed

        raise SerializationError(
            f"unsupported value {_qualified_class_name(value)} at {path or '<root>'}"
        )

    def _encode_sequence(
        self,
        values: list[Any] | tuple[Any, ...],
        *,
        kind: str,
        path: str,
        state: _SerializationState,
        allow_repr_fallback: bool,
    ) -> tuple[dict[str, Any], list[Any], list[Any]]:
        graph_items = []
        view_items = []
        canonical_items = []
        for index, item in enumerate(values):
            item_node, item_view, item_canonical = self._encode(
                item,
                path=f"{path}[{index}]" if path else f"[{index}]",
                state=state,
                allow_repr_fallback=allow_repr_fallback,
            )
            graph_items.append(item_node)
            view_items.append(item_view)
            canonical_items.append(item_canonical)
        return (
            {"node_type": "sequence", "sequence_type": kind, "items": graph_items},
            view_items,
            canonical_items,
        )

    def _externalize_data_url(
        self, value: str, *, path: str, state: _SerializationState
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        match = _DATA_URL_PATTERN.match(value)
        if match is None:
            return None
        metadata = match.group("metadata")
        metadata_parts = metadata.split(";") if metadata else []
        media_type = metadata_parts[0] if metadata_parts and metadata_parts[0] else "text/plain"
        if not any(part.lower() == "base64" for part in metadata_parts[1:]):
            return None

        payload = match.group("payload")
        content_blob: BlobRef | None = None
        decoded_bytes: bytes | None = None
        capture_status = "captured"
        canonical_base64 = False
        try:
            compact_payload = "".join(payload.split())
            padded_payload = compact_payload + ("=" * (-len(compact_payload) % 4))
            decoded_bytes = base64.b64decode(padded_payload, validate=True)
            self._reject_forbidden_bytes(decoded_bytes, path=path)
            canonical_base64 = base64.b64encode(decoded_bytes).decode("ascii") == payload
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            capture_status = "decode_failed"
            state.warnings.append(f"could not decode data URL at {path or '<root>'}: {error}")

        width = None
        height = None
        if decoded_bytes is not None and media_type.lower().startswith("image/"):
            try:
                with Image.open(io.BytesIO(decoded_bytes)) as request_image:
                    width, height = request_image.size
            except Exception as error:
                capture_status = "captured_dimensions_unavailable"
                state.warnings.append(
                    f"could not inspect request image dimensions at {path or '<root>'}: {error}"
                )

        original_blob = self.blob_store.put_bytes(value.encode("utf-8"), "text/plain;charset=utf-8")
        if decoded_bytes is not None:
            content_blob = self.blob_store.put_bytes(decoded_bytes, media_type)

        externalized = {
            "original_text_blob": original_blob,
            "content_blob": content_blob,
            "media_type": media_type,
            "base64_alphabet": "standard",
            "content_path": path,
        }
        node = {
            "node_type": "data_url",
            **externalized,
            "capture_status": capture_status,
            "canonical_base64": canonical_base64,
        }
        request_view = {"$externalized_data_url": externalized}

        if media_type.lower().startswith("image/"):
            state.request_images.append(
                {
                    "content_path": path,
                    "original_text_blob": original_blob,
                    "content_blob": content_blob,
                    "media_type": media_type,
                    "width": width,
                    "height": height,
                    "capture_status": capture_status,
                    "canonical_base64": canonical_base64,
                }
            )
        return node, request_view

    def _reject_forbidden_bytes(self, value: bytes, *, path: str) -> None:
        if any(forbidden in value for forbidden in self._forbidden_bytes):
            raise SerializationError(f"configured secret was excluded at {path or '<root>'}")
        if _contains_signed_url_query(value.decode("utf-8", errors="ignore")):
            raise SerializationError(f"signed URL credential was excluded at {path or '<root>'}")

    def _decode_node(self, node: dict[str, Any]) -> Any:
        if not isinstance(node, dict) or not isinstance(node.get("node_type"), str):
            raise SerializationError("invalid artifact graph node")
        node_type = node["node_type"]
        if node_type == "null":
            return None
        if node_type in {"bool", "int", "float", "string"}:
            return node["value"]
        if node_type == "data_url":
            try:
                return self.blob_store.read_bytes(node["original_text_blob"]).decode("utf-8")
            except UnicodeDecodeError as error:
                raise SerializationError("data URL text blob is not UTF-8") from error
        if node_type == "binary":
            exact_bytes = self.blob_store.read_bytes(node["blob"])
            return {
                "$typed_value": {
                    "kind": node["original_type"],
                    "base64": base64.b64encode(exact_bytes).decode("ascii"),
                }
            }
        if node_type == "sequence":
            values = [self._decode_node(item) for item in node["items"]]
            if node["sequence_type"] == "tuple":
                return {"$typed_value": {"kind": "tuple", "items": values}}
            return values
        if node_type == "mapping":
            decoded_items = [
                (self._decode_node(item["key"]), self._decode_node(item["value"]))
                for item in node["items"]
            ]
            if all(isinstance(key, str) for key, _ in decoded_items):
                return {key: value for key, value in decoded_items}
            return {
                "$typed_mapping": [{"key": key, "value": value} for key, value in decoded_items]
            }
        if node_type == "serialized_object":
            return self._decode_node(node["value"])
        if node_type in {"typed_scalar", "repr_fallback"}:
            kind = node.get("kind", "repr_fallback")
            return {
                "$typed_value": {
                    "kind": kind,
                    "class": node["class"],
                    "value": node["value"],
                }
            }
        raise SerializationError(f"unknown artifact node_type: {node_type}")

    def _node_to_view(self, node: dict[str, Any]) -> Any:
        node_type = node.get("node_type")
        if node_type == "data_url":
            return {
                "$externalized_data_url": {
                    "original_text_blob": node["original_text_blob"],
                    "content_blob": node["content_blob"],
                    "media_type": node["media_type"],
                    "base64_alphabet": node["base64_alphabet"],
                    "content_path": node["content_path"],
                }
            }
        # For unusual non-string mapping keys, the canonical typed view is
        # sufficient and avoids duplicating a second recursive implementation.
        return self._decode_node(node)

    @staticmethod
    def _official_object_dump(value: Any) -> tuple[str, Any] | None:
        if hasattr(value, "model_dump") and callable(value.model_dump):
            try:
                return "model_dump(mode=json,exclude_none=false)", value.model_dump(
                    mode="json", exclude_none=False
                )
            except TypeError:
                return "model_dump(exclude_none=false)", value.model_dump(exclude_none=False)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            dumped = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
            return "dataclasses.fields", dumped
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return "to_dict", value.to_dict()
        return None

    @staticmethod
    def _typed_scalar(value: Any) -> tuple[str, Any] | None:
        if isinstance(value, enum.Enum):
            enum_value = value.value
            if isinstance(enum_value, (str, int, float, bool)) or enum_value is None:
                return "enum", enum_value
            return "enum", repr(enum_value)
        if isinstance(
            value, (datetime_module.datetime, datetime_module.date, datetime_module.time)
        ):
            return type(value).__name__, value.isoformat()
        if isinstance(value, decimal.Decimal):
            return "decimal", str(value)
        if isinstance(value, uuid.UUID):
            return "uuid", str(value)
        if isinstance(value, pathlib.PurePath):
            return "path", str(value)
        return None

    @staticmethod
    def _validate_graph(graph: Any) -> None:
        if not isinstance(graph, dict):
            raise SerializationError("artifact graph must be a mapping")
        if set(graph) != {"artifact_graph_version", "root"}:
            raise SerializationError("artifact graph has an invalid top-level shape")
        if graph["artifact_graph_version"] != ARTIFACT_GRAPH_VERSION:
            raise SerializationError("unsupported artifact graph version")
        if not isinstance(graph["root"], dict):
            raise SerializationError("artifact graph root node must be a mapping")


def snapshot_sdk_arguments(arguments: Mapping[str, Any], blob_store: BlobStore) -> ArtifactSnapshot:
    """Functional wrapper for SDK-boundary integration code."""

    return ArtifactSerializer(blob_store).snapshot_sdk_arguments(arguments)


def serialize_observation_image(
    image: Image.Image,
    blob_store: BlobStore,
    *,
    source_bytes: bytes | None = None,
    source_media_type: str = "image/png",
) -> dict[str, Any]:
    """Functional wrapper for runner/environment integration code."""

    return ArtifactSerializer(blob_store).serialize_observation_image(
        image,
        source_bytes=source_bytes,
        source_media_type=source_media_type,
    )


def _mapping_path(path: str, key: Any) -> str:
    if isinstance(key, str) and key.isidentifier():
        return f"{path}.{key}" if path else key
    encoded_key = json.dumps(key, ensure_ascii=False, allow_nan=False, default=repr)
    return f"{path}[{encoded_key}]" if path else f"[{encoded_key}]"


def _is_image_content_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(".image_url.url") or lowered.endswith('["image_url"]["url"]')


def _contains_signed_url_query(value: str) -> bool:
    for match in _URL_PATTERN.finditer(value):
        try:
            query_keys = {
                key.casefold()
                for key, _ in parse_qsl(
                    urlsplit(match.group(0)).query,
                    keep_blank_values=True,
                )
            }
        except (TypeError, ValueError):
            continue
        if query_keys & _SIGNED_QUERY_KEYS or any(key.startswith("x-amz-") for key in query_keys):
            return True
    return False


def _qualified_class_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _package_version(value: Any) -> str | None:
    module_name = type(value).__module__.split(".", 1)[0]
    if module_name == "builtins":
        return None
    try:
        return importlib.metadata.version(module_name)
    except importlib.metadata.PackageNotFoundError:
        module = __import__(module_name)
        version = getattr(module, "__version__", None)
        return str(version) if version is not None else None


def _looks_like_blob_ref(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {
        "algorithm",
        "digest",
        "byte_length",
        "media_type",
        "relative_path",
    }


def _raise_invalid_constant(constant: str) -> Any:
    raise SerializationError(f"non-finite JSON constant in artifact graph: {constant}")


__all__ = [
    "ARTIFACT_GRAPH_MEDIA_TYPE",
    "ARTIFACT_GRAPH_VERSION",
    "ArtifactSerializer",
    "ArtifactSnapshot",
    "SerializationError",
    "canonical_json_bytes",
    "serialize_observation_image",
    "snapshot_sdk_arguments",
]
