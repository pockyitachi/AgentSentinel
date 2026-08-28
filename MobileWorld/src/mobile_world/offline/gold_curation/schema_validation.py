"""Offline-only validation against the additive G1.6 workspace schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.gold_curation.contracts import CurationError

SCHEMA_ROOT: Final = (
    Path(__file__).resolve().parents[5] / "mobileworld_audit_handoff" / "schemas" / "g1_6"
)
SCHEMA_FILENAMES: Final = {
    "annotation_event.schema.json",
    "annotation_workspace.schema.json",
    "browser_transformation_preview.schema.json",
    "curator_packet.schema.json",
    "review_proposal.schema.json",
}


@lru_cache(maxsize=5)
def _validator(filename: str) -> Draft202012Validator:
    if filename not in SCHEMA_FILENAMES:
        raise CurationError("WORKSPACE_SCHEMA_INVALID", "unknown G1.6 workspace schema")
    try:
        value = json.loads((SCHEMA_ROOT / filename).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError(
            "WORKSPACE_SCHEMA_INVALID", "G1.6 workspace schema cannot be loaded"
        ) from exc
    if not isinstance(value, dict):
        raise CurationError("WORKSPACE_SCHEMA_INVALID", "G1.6 workspace schema is not an object")
    schema = cast(dict[str, Any], value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise CurationError(
            "WORKSPACE_SCHEMA_INVALID", "G1.6 workspace schema fails meta-validation"
        ) from exc
    return Draft202012Validator(schema)


def validate_schema_record(filename: str, value: Any) -> None:
    errors = sorted(_validator(filename).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.absolute_path
        )
        raise CurationError(
            "WORKSPACE_SCHEMA_MISMATCH",
            f"{filename} rejects runtime record at {path}: {first.message}",
        )
