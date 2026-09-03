#!/usr/bin/env python3
"""Copy one named dotenv value into a fresh owner-only raw secret file.

This operator tool never prints, hashes, measures, or otherwise reports the
secret value.  Merely checking that this script imports or displaying its help
does not read the source file.  Reading a real source still requires explicit
owner authorization.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

_MAX_SOURCE_BYTES = 65_536


class _SecretInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install one dotenv key as a fresh raw 0600 production secret."
    )
    parser.add_argument("--source-env", required=True, type=Path)
    parser.add_argument("--environment-key", default="OPENAI_API_KEY")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_source(path: Path) -> bytes:
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise _SecretInstallError("NOFOLLOW_UNAVAILABLE")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or not 0 < metadata.st_size <= _MAX_SOURCE_BYTES
        ):
            raise _SecretInstallError("INVALID_SOURCE_SECRET_FILE")
        remaining = metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                raise _SecretInstallError("SOURCE_SECRET_CHANGED")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _SecretInstallError("SOURCE_SECRET_CHANGED")
        final = os.fstat(descriptor)
        if (
            final.st_dev != metadata.st_dev
            or final.st_ino != metadata.st_ino
            or final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise _SecretInstallError("SOURCE_SECRET_CHANGED")
        return b"".join(chunks)
    except _SecretInstallError:
        raise
    except OSError as exc:
        raise _SecretInstallError("SOURCE_SECRET_UNAVAILABLE") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _extract_value(source: bytes, environment_key: str) -> bytearray:
    if (
        type(environment_key) is not str
        or not environment_key
        or not environment_key.replace("_", "A").isalnum()
        or environment_key[0].isdigit()
    ):
        raise _SecretInstallError("INVALID_ENVIRONMENT_KEY")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _SecretInstallError("INVALID_SOURCE_SECRET_FILE") from exc
    matches: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == environment_key:
            candidate = value.strip()
            if len(candidate) >= 2 and candidate[:1] == candidate[-1:] and candidate[0] in "'\"":
                candidate = candidate[1:-1]
            matches.append(candidate)
    if len(matches) != 1:
        raise _SecretInstallError("SOURCE_SECRET_KEY_MISSING_OR_DUPLICATE")
    value = matches[0]
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > _MAX_SOURCE_BYTES
    ):
        raise _SecretInstallError("INVALID_SOURCE_SECRET_VALUE")
    return bytearray(value.encode("utf-8"))


def _install(output: Path, value: bytearray) -> None:
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise _SecretInstallError("OUTPUT_SECRET_NOT_FRESH")
    try:
        parent = output.parent.resolve(strict=True)
        parent_metadata = output.parent.lstat()
    except OSError as exc:
        raise _SecretInstallError("OUTPUT_SECRET_PARENT_INVALID") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_gid != os.getegid()
        or parent != output.parent.absolute()
    ):
        raise _SecretInstallError("OUTPUT_SECRET_PARENT_INVALID")
    destination = parent / output.name
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short secret write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or metadata.st_size != len(value)
        ):
            raise OSError("secret output metadata differs")
        directory_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if created:
            try:
                os.unlink(destination)
            except OSError:
                pass
        raise _SecretInstallError("OUTPUT_SECRET_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    secret = bytearray()
    try:
        source = _read_source(arguments.source_env)
        secret = _extract_value(source, arguments.environment_key)
        _install(arguments.output, secret)
    except _SecretInstallError as exc:
        print(json.dumps({"error_code": exc.code, "ok": False}, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        for index in range(len(secret)):
            secret[index] = 0
    print(json.dumps({"ok": True, "secret_installed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
