#!/usr/bin/env python3
"""Materialize an exact owner-authorized manifest after explicit approval."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mobile_world.runtime.sentinel.r2_4.authority_promotion import (
    AuthorityPromotionError,
    load_canonical_draft_authority_v1,
    promote_draft_authority_v1,
    write_fresh_owner_authority_v1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import authority_manifest_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "After explicit owner approval, change only authorization.status in one exact "
            "canonical DRAFT manifest and create a fresh repo-external 0600 output. This "
            "command itself does not establish or record that approval."
        )
    )
    parser.add_argument("--draft-manifest", required=True, type=Path)
    parser.add_argument("--confirm-draft-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--owner-approved",
        action="store_true",
        help="Required operator assertion; use only after the owner explicitly approves the run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if not arguments.owner_approved:
            raise AuthorityPromotionError(
                "OWNER_APPROVAL_ASSERTION_REQUIRED",
                "promotion requires the explicit operator assertion",
            )
        draft = load_canonical_draft_authority_v1(
            arguments.draft_manifest,
            repository_root=arguments.repository_root,
        )
        draft_sha256 = authority_manifest_sha256(draft)
        promoted = promote_draft_authority_v1(
            draft,
            confirmed_draft_sha256=arguments.confirm_draft_sha256,
        )
        promoted_sha256 = write_fresh_owner_authority_v1(
            promoted,
            arguments.output,
            repository_root=arguments.repository_root,
        )
    except (AuthorityPromotionError, OSError, ValueError) as exc:
        code = getattr(exc, "code", None)
        if type(code) is not str:
            code = "AUTHORITY_PROMOTION_FAILED"
        sys.stderr.buffer.write(canonical_json_bytes({"error_code": code, "ok": False}) + b"\n")
        return 2
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "authorized_manifest_path": str(arguments.output),
                "authorized_manifest_sha256": promoted_sha256,
                "draft_manifest_sha256": draft_sha256,
                "ok": True,
                "projection_change": "AUTHORIZATION_STATUS_ONLY",
            }
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
