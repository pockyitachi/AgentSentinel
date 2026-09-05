"""Mechanical, local-only promotion of an exact draft run authority.

Calling these functions is not evidence that the owner approved a run.  The
operator must invoke the CLI only after that approval and must supply the exact
canonical draft hash.  Promotion changes only ``authorization.status`` and has
no network, GPU, model, backend, secret-read, or action side effect.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24SmokeRunAuthorityManifestV1,
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_MANIFEST_BYTES = 1_048_576
_READ_CHUNK_BYTES = 65_536


class AuthorityPromotionError(ValueError):
    """Stable, value-free failure raised by the promotion boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _external_path(path: object, repository_root: Path, name: str, *, strict: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or "\x00" in str(path):
        raise AuthorityPromotionError("INVALID_PATH", f"{name} must be absolute")
    try:
        repository = repository_root.resolve(strict=True)
        resolved = path.resolve(strict=strict)
        resolved.relative_to(repository)
    except ValueError:
        return path if strict else resolved
    except OSError as exc:
        raise AuthorityPromotionError("INVALID_PATH", f"{name} is unavailable") from exc
    raise AuthorityPromotionError("REPOSITORY_PATH_FORBIDDEN", f"{name} must stay outside Git")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityPromotionError("DUPLICATE_JSON_KEY", "draft repeats a JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise AuthorityPromotionError("NONCANONICAL_DRAFT", "draft contains a non-finite number")


def _smoke_authority_file_identity(info: os.stat_result) -> tuple[int, ...]:
    """Return every stable metadata field bound across an authority read."""

    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_safe_smoke_authority_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or not 1 <= info.st_size <= _MAX_MANIFEST_BYTES
    ):
        raise AuthorityPromotionError(
            "INVALID_DRAFT_FILE",
            "authority must be an owner-owned 0600 singly-linked bounded regular file",
        )


def _read_stable_smoke_authority_file(path: Path, *, repository_root: Path) -> bytes:
    """Read a smoke authority once and bind the descriptor and final pathname."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    path_only = getattr(os, "O_PATH", None)
    if type(no_follow) is not int or type(path_only) is not int:
        raise AuthorityPromotionError(
            "INVALID_DRAFT_FILE", "secure authority descriptor flags are unavailable"
        )
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    path_descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow | close_on_exec)
        before = os.fstat(descriptor)
        _require_safe_smoke_authority_file(before)

        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_MANIFEST_BYTES:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_BYTES, _MAX_MANIFEST_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)

        after = os.fstat(descriptor)
        _require_safe_smoke_authority_file(after)
        if _smoke_authority_file_identity(before) != _smoke_authority_file_identity(after):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority metadata changed during read"
            )
        raw = b"".join(chunks)
        if len(raw) != after.st_size:
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority size changed during read"
            )

        path_info = os.lstat(path)
        if stat.S_ISLNK(path_info.st_mode):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority path became a symlink during read"
            )
        if _smoke_authority_file_identity(after) != _smoke_authority_file_identity(path_info):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority path changed identity during read"
            )

        resolved = path.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
        try:
            resolved.relative_to(repository)
        except ValueError:
            pass
        else:
            raise AuthorityPromotionError(
                "REPOSITORY_PATH_FORBIDDEN", "authority must stay outside Git"
            )
        resolved_info = os.stat(resolved, follow_symlinks=False)
        if _smoke_authority_file_identity(after) != _smoke_authority_file_identity(resolved_info):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority path no longer resolves to its descriptor"
            )
        path_descriptor = os.open(path, path_only | no_follow | close_on_exec)
        rebound = os.fstat(path_descriptor)
        _require_safe_smoke_authority_file(rebound)
        if _smoke_authority_file_identity(after) != _smoke_authority_file_identity(rebound):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "authority path changed identity during final binding"
            )
        return raw
    except AuthorityPromotionError:
        raise
    except OSError as exc:
        raise AuthorityPromotionError(
            "INVALID_DRAFT_FILE", "authority cannot be read stably"
        ) from exc
    finally:
        if path_descriptor >= 0:
            os.close(path_descriptor)
        if descriptor >= 0:
            os.close(descriptor)


def load_canonical_draft_authority_v1(
    path: Path,
    *,
    repository_root: Path,
) -> R24R25RunAuthorityManifestV1:
    """Read one external owner-only canonical draft through one descriptor."""

    if path.is_symlink():
        raise AuthorityPromotionError("INVALID_DRAFT_FILE", "draft cannot be a symlink")
    trusted_path = _external_path(path, repository_root, "draft manifest", strict=True)
    descriptor = -1
    try:
        descriptor = os.open(trusted_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or not 1 <= info.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise AuthorityPromotionError(
                "INVALID_DRAFT_FILE", "draft must be an owner-owned 0600 bounded regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    except AuthorityPromotionError:
        raise
    except OSError as exc:
        raise AuthorityPromotionError("INVALID_DRAFT_FILE", "draft cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not 1 <= len(raw) <= _MAX_MANIFEST_BYTES:
        raise AuthorityPromotionError("INVALID_DRAFT_FILE", "draft size changed during read")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        draft = parse_authority_manifest(value)
    except AuthorityPromotionError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise AuthorityPromotionError("INVALID_DRAFT", "draft failed strict parsing") from exc
    canonical = canonical_json_bytes(cast(JsonValue, authority_manifest_projection(draft)))
    if raw != canonical:
        raise AuthorityPromotionError(
            "NONCANONICAL_DRAFT", "draft bytes differ from the module-owned projection"
        )
    if draft.authorization.status is not RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED:
        raise AuthorityPromotionError("DRAFT_REQUIRED", "input is not an unpromoted draft")
    return draft


def promote_draft_authority_v1(
    draft: R24R25RunAuthorityManifestV1,
    *,
    confirmed_draft_sha256: str,
) -> R24R25RunAuthorityManifestV1:
    """Return a detached authority whose only projection change is status."""

    if type(draft) is not R24R25RunAuthorityManifestV1:
        raise AuthorityPromotionError("UNTRUSTED_DRAFT", "draft type differs")
    try:
        trusted = parse_authority_manifest(authority_manifest_projection(draft))
    except ValueError as exc:
        raise AuthorityPromotionError("INVALID_DRAFT", "draft failed reconstruction") from exc
    if trusted.authorization.status is not RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED:
        raise AuthorityPromotionError("DRAFT_REQUIRED", "input is not an unpromoted draft")
    if type(confirmed_draft_sha256) is not str or _SHA256.fullmatch(confirmed_draft_sha256) is None:
        raise AuthorityPromotionError("INVALID_DRAFT_CONFIRMATION", "draft hash is invalid")
    draft_sha256 = authority_manifest_sha256(trusted)
    if confirmed_draft_sha256 != draft_sha256:
        raise AuthorityPromotionError("DRAFT_CONFIRMATION_MISMATCH", "confirmed draft hash differs")
    promoted = replace(
        trusted,
        authorization=replace(
            trusted.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    before = authority_manifest_projection(trusted)
    expected = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(cast(JsonValue, before))))
    authorization = expected.get("authorization")
    if type(authorization) is not dict:
        raise AuthorityPromotionError("PROMOTION_INVARIANT_FAILED", "authorization disappeared")
    authorization["status"] = RunAuthorizationStatusV1.OWNER_AUTHORIZED.value
    after = authority_manifest_projection(promoted)
    if after != expected:
        raise AuthorityPromotionError(
            "PROMOTION_INVARIANT_FAILED", "promotion changed more than authorization status"
        )
    try:
        return parse_authority_manifest(after)
    except ValueError as exc:
        raise AuthorityPromotionError(
            "PROMOTION_INVARIANT_FAILED", "promoted manifest failed reconstruction"
        ) from exc


def write_fresh_owner_authority_v1(
    manifest: R24R25RunAuthorityManifestV1,
    output: Path,
    *,
    repository_root: Path,
) -> str:
    """Atomically create one fresh external 0600 canonical authority file."""

    if type(manifest) is not R24R25RunAuthorityManifestV1:
        raise AuthorityPromotionError("UNTRUSTED_AUTHORITY", "authority type differs")
    try:
        trusted = parse_authority_manifest(authority_manifest_projection(manifest))
    except ValueError as exc:
        raise AuthorityPromotionError(
            "INVALID_AUTHORITY", "authority failed reconstruction"
        ) from exc
    if trusted.authorization.status is not RunAuthorizationStatusV1.OWNER_AUTHORIZED:
        raise AuthorityPromotionError("OWNER_AUTHORITY_REQUIRED", "output must be promoted")
    target = _external_path(output, repository_root, "authorized output", strict=False)
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise AuthorityPromotionError("INVALID_PATH", "output parent is unavailable") from exc
    if not parent.is_dir() or target.exists() or target.is_symlink():
        raise AuthorityPromotionError("OUTPUT_NOT_FRESH", "authorized output must be fresh")
    payload = canonical_json_bytes(cast(JsonValue, authority_manifest_projection(trusted)))
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        if created:
            try:
                os.unlink(target)
            except OSError:
                pass
        raise AuthorityPromotionError("AUTHORITY_WRITE_FAILED", "fresh write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return authority_manifest_sha256(trusted)


def _load_canonical_smoke_authority_v1(
    path: Path,
    *,
    repository_root: Path,
    required_status: RunAuthorizationStatusV1,
) -> R24SmokeRunAuthorityManifestV1:
    """Read one canonical external 0600 smoke authority through one descriptor."""

    if path.is_symlink():
        raise AuthorityPromotionError("INVALID_DRAFT_FILE", "draft cannot be a symlink")
    trusted_path = _external_path(path, repository_root, "draft manifest", strict=True)
    raw = _read_stable_smoke_authority_file(trusted_path, repository_root=repository_root)
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        draft = parse_smoke_authority_manifest(decoded)
        canonical = canonical_json_bytes(
            cast(JsonValue, smoke_authority_manifest_projection(draft))
        )
    except AuthorityPromotionError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise AuthorityPromotionError("INVALID_DRAFT", "draft failed strict parsing") from exc
    if raw != canonical:
        raise AuthorityPromotionError(
            "NONCANONICAL_DRAFT", "draft bytes differ from the module-owned projection"
        )
    if draft.authorization.status is not required_status:
        code = (
            "DRAFT_REQUIRED"
            if required_status is RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED
            else "OWNER_AUTHORITY_REQUIRED"
        )
        raise AuthorityPromotionError(code, "smoke authority status differs")
    return draft


def load_canonical_draft_smoke_authority_v1(
    path: Path,
    *,
    repository_root: Path,
) -> R24SmokeRunAuthorityManifestV1:
    """Read one canonical external 0600 R2.4-smoke-only draft."""

    return _load_canonical_smoke_authority_v1(
        path,
        repository_root=repository_root,
        required_status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
    )


def load_owner_authorized_smoke_authority_v1(
    path: Path,
    *,
    repository_root: Path,
) -> R24SmokeRunAuthorityManifestV1:
    """Read one canonical external 0600 promoted R2.4 smoke authority."""

    return _load_canonical_smoke_authority_v1(
        path,
        repository_root=repository_root,
        required_status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
    )


def promote_draft_smoke_authority_v1(
    draft: R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_draft_sha256: str,
) -> R24SmokeRunAuthorityManifestV1:
    """Promote only the status of one exact R2.4-smoke-only draft."""

    if type(draft) is not R24SmokeRunAuthorityManifestV1:
        raise AuthorityPromotionError("UNTRUSTED_DRAFT", "smoke draft type differs")
    try:
        trusted = parse_smoke_authority_manifest(smoke_authority_manifest_projection(draft))
    except ValueError as exc:
        raise AuthorityPromotionError("INVALID_DRAFT", "smoke draft failed reconstruction") from exc
    if trusted.authorization.status is not RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED:
        raise AuthorityPromotionError("DRAFT_REQUIRED", "input is not an unpromoted draft")
    if type(confirmed_draft_sha256) is not str or _SHA256.fullmatch(confirmed_draft_sha256) is None:
        raise AuthorityPromotionError("INVALID_DRAFT_CONFIRMATION", "draft hash is invalid")
    if confirmed_draft_sha256 != smoke_authority_manifest_sha256(trusted):
        raise AuthorityPromotionError("DRAFT_CONFIRMATION_MISMATCH", "confirmed draft hash differs")
    promoted = replace(
        trusted,
        authorization=replace(
            trusted.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    before = smoke_authority_manifest_projection(trusted)
    expected = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(cast(JsonValue, before))))
    authorization = expected.get("authorization")
    if type(authorization) is not dict:
        raise AuthorityPromotionError("PROMOTION_INVARIANT_FAILED", "authorization disappeared")
    authorization["status"] = RunAuthorizationStatusV1.OWNER_AUTHORIZED.value
    after = smoke_authority_manifest_projection(promoted)
    if after != expected:
        raise AuthorityPromotionError(
            "PROMOTION_INVARIANT_FAILED", "promotion changed more than authorization status"
        )
    return parse_smoke_authority_manifest(after)


def write_fresh_owner_smoke_authority_v1(
    manifest: R24SmokeRunAuthorityManifestV1,
    output: Path,
    *,
    repository_root: Path,
) -> str:
    """Atomically create one fresh external canonical smoke-only authority."""

    if type(manifest) is not R24SmokeRunAuthorityManifestV1:
        raise AuthorityPromotionError("UNTRUSTED_AUTHORITY", "smoke authority type differs")
    trusted = parse_smoke_authority_manifest(smoke_authority_manifest_projection(manifest))
    if trusted.authorization.status is not RunAuthorizationStatusV1.OWNER_AUTHORIZED:
        raise AuthorityPromotionError("OWNER_AUTHORITY_REQUIRED", "output must be promoted")
    target = _external_path(output, repository_root, "authorized output", strict=False)
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise AuthorityPromotionError("INVALID_PATH", "output parent is unavailable") from exc
    if not parent.is_dir() or target.exists() or target.is_symlink():
        raise AuthorityPromotionError("OUTPUT_NOT_FRESH", "authorized output must be fresh")
    payload = canonical_json_bytes(cast(JsonValue, smoke_authority_manifest_projection(trusted)))
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        if created:
            try:
                os.unlink(target)
            except OSError:
                pass
        raise AuthorityPromotionError("AUTHORITY_WRITE_FAILED", "fresh write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return smoke_authority_manifest_sha256(trusted)


__all__ = [
    "AuthorityPromotionError",
    "load_canonical_draft_authority_v1",
    "load_canonical_draft_smoke_authority_v1",
    "load_owner_authorized_smoke_authority_v1",
    "promote_draft_authority_v1",
    "promote_draft_smoke_authority_v1",
    "write_fresh_owner_smoke_authority_v1",
    "write_fresh_owner_authority_v1",
]
