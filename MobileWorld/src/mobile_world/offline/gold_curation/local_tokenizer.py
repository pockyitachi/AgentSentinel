"""Verified local-only token counters for the G1.6 transformation preview."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, cast

from mobile_world.offline.g1_history_codecs import PinnedTokenCounter
from mobile_world.offline.gold_curation.contracts import CurationError, canonical_sha256, require

MODEL_CONFIG_MANIFEST_SHA256: Final = (
    "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f"
)
TOKENIZERS_VERSION: Final = "0.22.2"
PINNED_MODELS: Final = {
    "qwen3vl_8b": {
        "tokenizer_id": "Qwen/Qwen3-VL-8B-Instruct",
        "tokenizer_binding_sha256": (
            "e97afc56a6ce6b1d0d78345efc2b27c9853e9251d1e2f2bb0ff60b9b99926efd"
        ),
        "revision": "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
    },
    "mai_ui_8b": {
        "tokenizer_id": "Tongyi-MAI/MAI-UI-8B",
        "tokenizer_binding_sha256": (
            "dac3c7c7da1bcb043402cb3571a0867f98153c4fd3f3c0614153a6ea27518d23"
        ),
        "revision": "e00a0097abb9cc621cac5172d8c4809f0839c94e",
    },
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_exact(
    path: Path,
    *,
    byte_count: int,
    sha256: str,
    allowed_symlink_root: Path | None = None,
) -> bytes:
    try:
        is_symlink = path.is_symlink()
        resolved = path.resolve(strict=True)
        if is_symlink:
            require(
                allowed_symlink_root is not None
                and resolved.is_relative_to(allowed_symlink_root.resolve(strict=True)),
                "PINNED_TOKENIZER_UNAVAILABLE",
                "a frozen tokenizer artifact is an unbound symlink",
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise CurationError(
            "PINNED_TOKENIZER_UNAVAILABLE", "a frozen tokenizer artifact is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode),
            "PINNED_TOKENIZER_UNAVAILABLE",
            "a frozen tokenizer artifact is not a regular file",
        )
        chunks: list[bytes] = []
        remaining = byte_count + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    require(
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and len(data) == byte_count
        and _sha256(data) == sha256,
        "PINNED_TOKENIZER_UNAVAILABLE",
        "a frozen tokenizer artifact differs from its pinned bytes",
    )
    return data


def _load_manifest(path: Path) -> dict[str, Any]:
    require(
        not path.is_symlink(),
        "PINNED_TOKENIZER_UNAVAILABLE",
        "model-config manifest cannot be a symlink",
    )
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError(
            "PINNED_TOKENIZER_UNAVAILABLE", "model-config manifest cannot be loaded"
        ) from exc
    require(
        _sha256(data) == MODEL_CONFIG_MANIFEST_SHA256 and isinstance(value, dict),
        "PINNED_TOKENIZER_UNAVAILABLE",
        "model-config manifest differs from the frozen G1.1 binding",
    )
    return cast(dict[str, Any], value)


def load_local_pinned_token_counters(
    model_config_manifest_path: str | os.PathLike[str],
    *,
    snapshot_roots: Mapping[str, str | os.PathLike[str]] | None = None,
) -> dict[str, PinnedTokenCounter]:
    """Load exact tokenizer JSON bytes locally and return deterministic CPU counters.

    This function never imports Transformers, opens a network client, or loads model
    weights.  ``tokenizers`` is imported lazily only after every frozen artifact is
    verified; absence or version drift fails closed.
    """

    manifest = _load_manifest(Path(model_config_manifest_path))
    models = manifest.get("models")
    require(
        isinstance(models, list),
        "PINNED_TOKENIZER_UNAVAILABLE",
        "model-config manifest lacks its model records",
    )
    model_rows = cast(list[Any], models)
    by_id = {
        item.get("model_id"): item
        for item in model_rows
        if isinstance(item, dict) and isinstance(item.get("model_id"), str)
    }
    require(
        set(PINNED_MODELS) <= set(by_id),
        "PINNED_TOKENIZER_UNAVAILABLE",
        "model-config manifest lacks a selected G1 tokenizer",
    )
    try:
        installed_version = importlib.metadata.version("tokenizers")
    except importlib.metadata.PackageNotFoundError as exc:
        raise CurationError(
            "PINNED_TOKENIZER_UNAVAILABLE",
            "tokenizers==0.22.2 is not installed in this CPU annotation environment",
        ) from exc
    require(
        installed_version == TOKENIZERS_VERSION,
        "PINNED_TOKENIZER_UNAVAILABLE",
        "the local tokenizers runtime differs from frozen version 0.22.2",
    )
    supplied_roots = {key: Path(value) for key, value in (snapshot_roots or {}).items()}
    require(
        set(supplied_roots) <= set(PINNED_MODELS),
        "PINNED_TOKENIZER_UNAVAILABLE",
        "a tokenizer snapshot was supplied for an unsupported model",
    )
    verified_tokenizer_json: dict[str, bytes] = {}
    for model_id, expected in PINNED_MODELS.items():
        model = cast(dict[str, Any], by_id[model_id])
        tokenizer = model.get("tokenizer")
        require(
            isinstance(tokenizer, dict)
            and tokenizer.get("revision") == expected["revision"]
            and tokenizer.get("tokenizers_version") == TOKENIZERS_VERSION
            and tokenizer.get("use_fast") is True
            and tokenizer.get("trust_remote_code") is False
            and tokenizer.get("counting_call") == "tokenizer.encode(text, add_special_tokens=False)"
            and canonical_sha256(tokenizer) == expected["tokenizer_binding_sha256"],
            "PINNED_TOKENIZER_UNAVAILABLE",
            "a selected tokenizer record differs from the frozen G1.5 binding",
        )
        tokenizer_record = cast(dict[str, Any], tokenizer)
        root_value = supplied_roots.get(model_id, Path(model["local_snapshot_reference"]))
        root = Path(root_value)
        require(
            not root.is_symlink() and root.is_dir(),
            "PINNED_TOKENIZER_UNAVAILABLE",
            "a selected tokenizer snapshot root is unavailable",
        )
        artifacts = tokenizer_record.get("artifacts")
        require(
            isinstance(artifacts, list) and bool(artifacts),
            "PINNED_TOKENIZER_UNAVAILABLE",
            "a selected tokenizer artifact inventory is missing",
        )
        artifact_rows = cast(list[Any], artifacts)
        allowed_symlink_root = (
            Path(model["local_snapshot_reference"]).parent.parent / "blobs"
            if model_id not in supplied_roots
            else None
        )
        tokenizer_json: bytes | None = None
        seen: set[str] = set()
        for raw_artifact in artifact_rows:
            artifact = cast(dict[str, Any], raw_artifact)
            require(
                isinstance(raw_artifact, dict)
                and set(artifact) == {"path", "byte_count", "sha256"}
                and isinstance(artifact["path"], str)
                and len(artifact["path"]) > 0
                and "/" not in artifact["path"]
                and "\\" not in artifact["path"]
                and artifact["path"] not in seen
                and type(artifact["byte_count"]) is int
                and artifact["byte_count"] > 0
                and isinstance(artifact["sha256"], str),
                "PINNED_TOKENIZER_UNAVAILABLE",
                "a selected tokenizer artifact inventory is invalid",
            )
            seen.add(artifact["path"])
            data = _read_regular_exact(
                root / artifact["path"],
                byte_count=artifact["byte_count"],
                sha256=artifact["sha256"],
                allowed_symlink_root=allowed_symlink_root,
            )
            if artifact["path"] == "tokenizer.json":
                tokenizer_json = data
        require(
            tokenizer_json is not None,
            "PINNED_TOKENIZER_UNAVAILABLE",
            "a selected tokenizer.json artifact is missing",
        )
        assert tokenizer_json is not None
        verified_tokenizer_json[model_id] = tokenizer_json

    try:
        tokenizers_module = importlib.import_module("tokenizers")
        tokenizer_type = tokenizers_module.Tokenizer
    except (AttributeError, ImportError) as exc:
        raise CurationError(
            "PINNED_TOKENIZER_UNAVAILABLE", "the local tokenizers runtime cannot be loaded"
        ) from exc

    result: dict[str, PinnedTokenCounter] = {}
    for model_id, expected in PINNED_MODELS.items():
        try:
            loaded = tokenizer_type.from_str(verified_tokenizer_json[model_id].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CurationError(
                "PINNED_TOKENIZER_UNAVAILABLE", "a frozen tokenizer JSON cannot be decoded"
            ) from exc

        def count_without_special_tokens(text: str, *, loaded: Any = loaded) -> int:
            require(
                isinstance(text, str),
                "TOKEN_COUNTER_INVALID",
                "token counter input must be exact Unicode text",
            )
            encoded = loaded.encode(text, add_special_tokens=False)
            ids = encoded.ids
            require(
                isinstance(ids, list) and all(type(item) is int and item >= 0 for item in ids),
                "TOKEN_COUNTER_INVALID",
                "local tokenizer returned a non-integer token sequence",
            )
            return len(ids)

        result[model_id] = PinnedTokenCounter(
            tokenizer_id=expected["tokenizer_id"],
            tokenizer_sha256=expected["tokenizer_binding_sha256"],
            count_without_special_tokens=count_without_special_tokens,
        )
    return result
