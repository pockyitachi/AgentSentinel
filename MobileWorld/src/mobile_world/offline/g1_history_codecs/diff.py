"""Deterministic human-readable companion to the G1.2 machine diff receipt."""

from __future__ import annotations

from mobile_world.offline.causal_replay.contracts import (
    RenderResult,
    canonical_json_bytes,
    json_path_text,
)


def _quoted(value: str) -> str:
    return value.encode("unicode_escape").decode("ascii")


def render_human_diff(result: RenderResult) -> str:
    """Render exact text/list changes without timestamps or environment data."""

    lines = [
        f"requested_arm={result.requested_arm.value}",
        f"effective_arm={result.effective_arm.value if result.effective_arm else 'NONE'}",
        f"fallback_state={result.fallback_state.value}",
        f"count_as_treatment={str(result.count_as_treatment).lower()}",
        f"source_request_sha256={result.source_request_sha256}",
        f"rendered_request_sha256={result.rendered_request_sha256}",
    ]
    if not result.diffs and not result.list_insertions:
        lines.append("changes=NONE")
        return "\n".join(lines) + "\n"
    for diff in result.diffs:
        lines.extend(
            (
                f"diff.operation_id={diff.operation_id}",
                f"diff.path={json_path_text(diff.container_path)}",
                f"diff.source_chars={diff.source_char_start}:{diff.source_char_end}",
                f"diff.kind={diff.mapping_kind.value}",
                f"diff.before={_quoted(diff.original_text)}",
                f"diff.after={_quoted(diff.rendered_text)}",
            )
        )
    for insertion in result.list_insertions:
        lines.extend(
            (
                f"insertion.operation_id={insertion.operation_id}",
                f"insertion.path={json_path_text(insertion.container_path)}",
                f"insertion.source_index={insertion.source_index}",
                f"insertion.rendered_index={insertion.rendered_index}",
                f"insertion.sha256={insertion.inserted_value_sha256}",
                "insertion.value=" + canonical_json_bytes(insertion.inserted_value).decode("utf-8"),
            )
        )
    return "\n".join(lines) + "\n"
