import base64
import copy
import dataclasses
import hashlib
import io
import json

import pytest
from PIL import Image

from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.serializer import (
    ARTIFACT_GRAPH_VERSION,
    ArtifactSerializer,
    SerializationError,
    canonical_json_bytes,
)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _multimodal_arguments(data_url: str) -> dict:
    return {
        "model": "fixture-model",
        "messages": [
            {"role": "system", "content": "system text"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.0,
        "stream": False,
    }


def test_sdk_snapshot_externalizes_data_url_and_round_trips_exact_request(tmp_path) -> None:
    store = BlobStore(tmp_path)
    serializer = ArtifactSerializer(store)
    image_bytes = _png_bytes(Image.new("RGB", (3, 2), (10, 20, 30)))
    data_url = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"
    arguments = _multimodal_arguments(data_url)
    untouched = copy.deepcopy(arguments)

    snapshot = serializer.snapshot_sdk_arguments(arguments)

    assert arguments == untouched
    assert snapshot.serialization_fidelity == "lossless"
    assert snapshot.artifact_graph["artifact_graph_version"] == ARTIFACT_GRAPH_VERSION
    assert store.verify(snapshot.snapshot_blob)
    assert serializer.rehydrate(snapshot.snapshot_blob) == arguments
    assert snapshot.canonical_sha256 == hashlib.sha256(canonical_json_bytes(arguments)).hexdigest()

    externalized = snapshot.request_view["messages"][1]["content"][1]["image_url"]["url"]
    metadata = externalized["$externalized_data_url"]
    assert store.read_bytes(metadata["original_text_blob"]).decode("utf-8") == data_url
    assert store.read_bytes(metadata["content_blob"]) == image_bytes
    assert metadata["content_path"] == "messages[1].content[1].image_url.url"

    assert snapshot.request_images == (
        {
            "content_path": "messages[1].content[1].image_url.url",
            "original_text_blob": metadata["original_text_blob"],
            "content_blob": metadata["content_blob"],
            "media_type": "image/png",
            "width": 3,
            "height": 2,
            "capture_status": "captured",
            "canonical_base64": True,
        },
    )
    graph_bytes = store.read_bytes(snapshot.snapshot_blob)
    assert data_url.encode("utf-8") not in graph_bytes


def test_noncanonical_base64_preserves_exact_original_text(tmp_path) -> None:
    store = BlobStore(tmp_path)
    serializer = ArtifactSerializer(store)
    image_bytes = _png_bytes(Image.new("L", (2, 2), 17))
    canonical_payload = base64.b64encode(image_bytes).decode("ascii")
    wrapped_payload = "\n".join(
        canonical_payload[index : index + 16] for index in range(0, len(canonical_payload), 16)
    )
    data_url = f"data:image/png;base64,{wrapped_payload}"

    snapshot = serializer.snapshot_sdk_arguments(_multimodal_arguments(data_url))

    image = snapshot.request_images[0]
    assert image["canonical_base64"] is False
    assert store.read_bytes(image["content_blob"]) == image_bytes
    assert store.read_bytes(image["original_text_blob"]).decode("utf-8") == data_url
    assert (
        serializer.rehydrate(snapshot.snapshot_blob)["messages"][1]["content"][1]["image_url"][
            "url"
        ]
        == data_url
    )


def test_remote_image_url_is_preserved_without_fetch(tmp_path) -> None:
    serializer = ArtifactSerializer(BlobStore(tmp_path))
    url = "https://images.example/private.png?opaque=model-visible-value"
    arguments = _multimodal_arguments(url)

    snapshot = serializer.snapshot_sdk_arguments(arguments)

    assert snapshot.request_view == arguments
    assert serializer.rehydrate(snapshot.snapshot_blob) == arguments
    assert snapshot.request_images == (
        {
            "content_path": "messages[1].content[1].image_url.url",
            "original_url": url,
            "original_text_blob": None,
            "content_blob": None,
            "media_type": None,
            "width": None,
            "height": None,
            "capture_status": "url_preserved_content_unavailable",
        },
    )


@pytest.mark.parametrize(
    ("mode", "color"),
    [("RGB", (1, 2, 3)), ("RGBA", (1, 2, 3, 4)), ("L", 123)],
)
def test_observation_image_preserves_pixels_and_live_object(tmp_path, mode, color) -> None:
    store = BlobStore(tmp_path)
    serializer = ArtifactSerializer(store)
    image = Image.new(mode, (4, 3), color)
    pixels_before = image.tobytes()
    source_bytes = b"exact environment response bytes"

    reference = serializer.serialize_observation_image(image, source_bytes=source_bytes)

    assert reference["width"] == 4
    assert reference["height"] == 3
    assert reference["mode"] == mode
    assert reference["representation"] == "canonical_png_from_runtime_pixels"
    assert store.read_bytes(reference["source_blob"]) == source_bytes
    with Image.open(io.BytesIO(store.read_bytes(reference["pixel_blob"]))) as restored:
        restored.load()
        assert restored.mode == mode
        assert restored.size == image.size
        assert restored.tobytes() == pixels_before
    assert image.mode == mode
    assert image.size == (4, 3)
    assert image.tobytes() == pixels_before


def test_binary_leaf_is_typed_externalized_and_reconstructable(tmp_path) -> None:
    store = BlobStore(tmp_path)
    serializer = ArtifactSerializer(store)
    snapshot = serializer.snapshot_sdk_arguments({"payload": b"\x00\xff"})

    assert snapshot.request_view["payload"]["$externalized_blob"]["original_type"] == "bytes"
    assert serializer.rehydrate(snapshot.snapshot_blob) == {
        "payload": {"$typed_value": {"kind": "bytes", "base64": "AP8="}}
    }


def test_official_model_dump_is_used_and_class_metadata_is_retained(tmp_path) -> None:
    class FakeSDKObject:
        def __init__(self) -> None:
            self.value = {"content": "raw", "optional": None}

        def model_dump(self, *, mode, exclude_none):
            assert mode == "json"
            assert exclude_none is False
            return dict(self.value)

    serializer = ArtifactSerializer(BlobStore(tmp_path))
    sdk_object = FakeSDKObject()

    snapshot = serializer.snapshot({"response": sdk_object})

    response_node = snapshot.artifact_graph["root"]["items"][0]["value"]
    assert response_node["node_type"] == "serialized_object"
    assert response_node["class"].endswith("FakeSDKObject")
    assert response_node["serializer"] == "model_dump(mode=json,exclude_none=false)"
    assert serializer.rehydrate(snapshot.snapshot_blob) == {
        "response": {"content": "raw", "optional": None}
    }
    assert sdk_object.value == {"content": "raw", "optional": None}


def test_unknown_object_requires_explicit_marked_repr_fallback(tmp_path) -> None:
    class Unknown:
        pass

    serializer = ArtifactSerializer(BlobStore(tmp_path))
    with pytest.raises(SerializationError):
        serializer.snapshot(Unknown())

    snapshot = serializer.snapshot(Unknown(), allow_repr_fallback=True)
    assert snapshot.serialization_fidelity == "repr_fallback"
    assert snapshot.warnings


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(tmp_path, value) -> None:
    serializer = ArtifactSerializer(BlobStore(tmp_path))
    with pytest.raises(SerializationError):
        serializer.snapshot_sdk_arguments({"temperature": value})


def test_canonical_json_is_compact_sorted_utf8_and_rejects_nan() -> None:
    assert canonical_json_bytes({"z": "汉字", "a": 1}) == '{"a":1,"z":"汉字"}'.encode()
    with pytest.raises(SerializationError):
        canonical_json_bytes({"bad": float("nan")})


def test_stored_graph_is_valid_json_without_inline_binary_payload(tmp_path) -> None:
    store = BlobStore(tmp_path)
    snapshot = ArtifactSerializer(store).snapshot_sdk_arguments({"bytes": b"secret-ish-bytes"})

    graph = json.loads(store.read_bytes(snapshot.snapshot_blob))
    assert graph["artifact_graph_version"] == ARTIFACT_GRAPH_VERSION
    assert b"secret-ish-bytes" not in store.read_bytes(snapshot.snapshot_blob)


@pytest.mark.parametrize("container_kind", ["mapping", "dataclass", "model_dump", "to_dict"])
def test_forbidden_values_are_rejected_before_any_artifact_is_persisted(
    tmp_path, container_kind
) -> None:
    secret = "configured-secret-value"

    @dataclasses.dataclass
    class DataclassValue:
        nested: str

    class ModelDumpValue:
        def model_dump(self, *, mode, exclude_none):
            assert mode == "json"
            assert exclude_none is False
            return {"nested": secret}

    class ToDictValue:
        def to_dict(self):
            return {"nested": secret}

    values = {
        "mapping": {"nested": secret},
        "dataclass": DataclassValue(secret),
        "model_dump": ModelDumpValue(),
        "to_dict": ToDictValue(),
    }
    serializer = ArtifactSerializer(
        BlobStore(tmp_path),
        forbidden_values=(secret,),
    )

    with pytest.raises(SerializationError, match="configured secret was excluded"):
        serializer.snapshot(values[container_kind])

    persisted = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert all(secret.encode() not in path.read_bytes() for path in persisted)


def test_forbidden_value_in_observation_source_is_not_persisted(tmp_path) -> None:
    secret = b"configured-binary-secret"
    serializer = ArtifactSerializer(
        BlobStore(tmp_path),
        forbidden_values=(secret,),
    )

    with pytest.raises(SerializationError, match="configured secret was excluded"):
        serializer.serialize_observation_image(
            Image.new("RGB", (1, 1), (1, 2, 3)),
            source_bytes=b"prefix:" + secret,
        )

    persisted = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert all(secret not in path.read_bytes() for path in persisted)


@pytest.mark.parametrize(
    "value",
    [
        "https://objects.example/frame.png?X-Amz-Signature=credential",
        b"prefix https://objects.example/frame.png?token=credential suffix",
    ],
)
def test_signed_url_credentials_are_rejected_before_persistence(tmp_path, value) -> None:
    serializer = ArtifactSerializer(BlobStore(tmp_path))

    with pytest.raises(SerializationError, match="signed URL credential was excluded"):
        serializer.snapshot(value)

    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"credential" not in persisted
