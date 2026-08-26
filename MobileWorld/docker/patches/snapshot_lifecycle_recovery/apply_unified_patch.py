#!/usr/bin/env python3
"""Apply one exact single-file unified diff without external dependencies."""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path, PurePosixPath


ALLOWED_TARGETS = {
    PurePosixPath("app/docker/start_emulator.sh"),
    PurePosixPath("app/service/src/mobile_world/core/server.py"),
    PurePosixPath("app/service/src/mobile_world/runtime/controller.py"),
    PurePosixPath("app/service/src/mobile_world/runtime/utils/docker.py"),
    PurePosixPath("app/service/src/mobile_world/runtime/utils/helpers.py"),
    PurePosixPath("app/service/src/mobile_world/tasks/base.py"),
}
HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def target_from_header(line: str, prefix: str) -> PurePosixPath:
    if not line.startswith(prefix):
        raise ValueError(f"missing {prefix.strip()} header")
    raw = line[len(prefix) :].rstrip("\n").split("\t", 1)[0]
    if not raw.startswith("b/"):
        raise ValueError("target path must use the b/ prefix")
    target = PurePosixPath(raw[2:])
    if target not in ALLOWED_TARGETS:
        raise ValueError(f"target is outside the patch allowlist: {target}")
    return target


def apply_exact(source: list[str], patch_lines: list[str]) -> tuple[PurePosixPath, list[str]]:
    if len(patch_lines) < 3 or not patch_lines[0].startswith("--- a/"):
        raise ValueError("patch must be a single-file a/ to b/ unified diff")
    old_target = PurePosixPath(patch_lines[0][len("--- a/") :].rstrip("\n").split("\t", 1)[0])
    target = target_from_header(patch_lines[1], "+++ ")
    if old_target != target:
        raise ValueError("renames are not supported")

    output: list[str] = []
    source_cursor = 0
    index = 2
    saw_hunk = False
    while index < len(patch_lines):
        match = HUNK_HEADER.match(patch_lines[index])
        if match is None:
            raise ValueError(f"unexpected patch line outside a hunk: {patch_lines[index]!r}")
        saw_hunk = True
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        hunk_start = old_start - 1
        if hunk_start < source_cursor:
            raise ValueError("overlapping or out-of-order hunks")
        output.extend(source[source_cursor:hunk_start])
        source_cursor = hunk_start
        consumed = 0
        emitted = 0
        index += 1

        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if line.startswith("\\ No newline at end of file"):
                raise ValueError("no-newline markers are not supported")
            if not line or line[0] not in " +-":
                raise ValueError(f"invalid unified-diff body line: {line!r}")
            marker = line[0]
            content = line[1:]
            if marker in " -":
                if source_cursor >= len(source) or source[source_cursor] != content:
                    raise ValueError(
                        f"source mismatch at line {source_cursor + 1}: expected {content!r}"
                    )
                source_cursor += 1
                consumed += 1
            if marker in " +":
                output.append(content)
                emitted += 1
            index += 1

        if consumed != old_count or emitted != new_count:
            raise ValueError(
                f"hunk count mismatch: consumed/emitted {consumed}/{emitted}, "
                f"expected {old_count}/{new_count}"
            )

    if not saw_hunk:
        raise ValueError("patch contains no hunks")
    output.extend(source[source_cursor:])
    return target, output


def main() -> None:
    args = parse_args()
    patch_lines = args.patch.read_text(encoding="utf-8").splitlines(keepends=True)
    target = target_from_header(patch_lines[1], "+++ ")
    root = args.root.resolve(strict=True)
    target_path = root.joinpath(*target.parts)
    metadata = target_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"target must be a regular non-symlink file: {target}")
    resolved_target = target_path.resolve(strict=True)
    resolved_target.relative_to(root)
    source = resolved_target.read_text(encoding="utf-8").splitlines(keepends=True)
    parsed_target, output = apply_exact(source, patch_lines)
    if parsed_target != target:
        raise AssertionError("parsed target changed")
    if args.check:
        return

    temporary = resolved_target.with_name(f".{resolved_target.name}.patch-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as file:
            file.writelines(output)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, resolved_target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
