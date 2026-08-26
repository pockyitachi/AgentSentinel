import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from mobile_world.runtime.audit.blob_store import BlobIntegrityError, BlobStore


def test_put_bytes_uses_exact_contract_reference_and_permissions(tmp_path) -> None:
    store = BlobStore(tmp_path)
    payload = b"\x00exact\nblob\xff"

    reference = store.put_bytes(payload, "application/octet-stream")
    digest = hashlib.sha256(payload).hexdigest()

    assert reference == {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(payload),
        "media_type": "application/octet-stream",
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }
    path = store.resolve(reference)
    assert path.read_bytes() == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.verify(reference)
    assert store.read_bytes(reference) == payload


def test_existing_blob_is_verified_without_being_replaced(tmp_path) -> None:
    store = BlobStore(tmp_path)
    reference = store.put_bytes(b"same bytes", "text/plain")
    path = store.resolve(reference)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    inode_before = path.stat().st_ino
    mtime_before = path.stat().st_mtime_ns

    duplicate = store.put_bytes(b"same bytes", "text/plain")

    assert duplicate == reference
    assert path.stat().st_ino == inode_before
    assert path.stat().st_mtime_ns == mtime_before


def test_corrupt_existing_digest_path_is_never_overwritten(tmp_path) -> None:
    store = BlobStore(tmp_path)
    reference = store.put_bytes(b"original", "text/plain")
    path = store.resolve(reference)
    path.write_bytes(b"corrupt!")

    with pytest.raises(BlobIntegrityError):
        store.put_bytes(b"original", "text/plain")

    assert path.read_bytes() == b"corrupt!"


def test_concurrent_identical_writers_converge_on_one_blob(tmp_path) -> None:
    store = BlobStore(tmp_path)
    payload = b"shared" * 10_000

    with ThreadPoolExecutor(max_workers=12) as executor:
        references = list(
            executor.map(
                lambda _: store.put_bytes(payload, "application/octet-stream"),
                range(40),
            )
        )

    assert all(reference == references[0] for reference in references)
    assert store.read_bytes(references[0]) == payload
    digest_directory = tmp_path / "blobs" / "sha256" / references[0]["digest"][:2]
    assert [path.name for path in digest_directory.iterdir()] == [references[0]["digest"]]


def test_resolve_rejects_noncanonical_or_traversing_reference(tmp_path) -> None:
    store = BlobStore(tmp_path)
    reference = store.put_bytes(b"safe", "text/plain")
    malicious = {**reference, "relative_path": "../../outside"}

    with pytest.raises(BlobIntegrityError):
        store.resolve(malicious)


@pytest.mark.parametrize(
    ("data", "media_type", "exception"),
    [
        ("not bytes", "text/plain", TypeError),
        (b"bytes", "", ValueError),
        (b"bytes", None, ValueError),
    ],
)
def test_put_bytes_validates_inputs(data, media_type, exception) -> None:
    store = BlobStore("unused")
    with pytest.raises(exception):
        store.put_bytes(data, media_type)
