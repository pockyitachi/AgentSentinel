"""Read-only, role-projected access to the active G1.3 capsule publication."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    EvidenceRef,
    JsonValue,
    PortableContractError,
    SpanRole,
)
from mobile_world.offline.g1_history_codecs import (
    DelimiterRepairBinding,
    MaiRawReplayHistoryCodec,
    PinnedTokenCounter,
    QwenFlatProgressHistoryCodec,
    bind_human_record_spans,
    build_clean_control_preview,
    build_five_arm_preview,
)
from mobile_world.offline.gold_curation.contracts import (
    REVIEW_PACKET_SCHEMA_VERSION,
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    json_copy,
    require,
    validate_transformation_preview_inputs,
)

ACTIVE_G1_3_MANIFEST_SHA256: Final = (
    "8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402"
)
ACTIVE_G1_3_CAPSULE_SET_SHA256: Final = (
    "7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed"
)
ACTIVE_G1_3_PUBLICATION: Final = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_3/capsules/sha256/"
    + ACTIVE_G1_3_MANIFEST_SHA256
)
DEFAULT_SOURCE_RUN_ROOTS: Final = {
    "01M0HAPBKPN5HJHFB6HQ3ME74M": Path(
        "/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/"
        "audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M"
    ),
    "01M0EZ6T4XF06CPS3Z69XXW4ZB": Path(
        "/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/"
        "audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB"
    ),
    "01M0J8N7SZPG34GGGPW9QRCDND": Path(
        "/shared/linqiang/mobileworld_audit_data/"
        "qwen3vl_8b_gui117_g7_thanksgiving_rerun2_20260821_01/"
        "audit/raw/runs/01M0J8N7SZPG34GGGPW9QRCDND"
    ),
}
PUBLICATION_STORE_ID: Final = "G1_3_PUBLICATION"
REQUIRED_FALSE_GUARDS: Final = (
    "execution_ready",
    "provider_invocation_allowed",
    "treatment_response_generation_allowed",
    "provider_invoked",
    "gpu_used",
    "gui_action_executed",
    "generated_action_executed",
    "raw_collector_mutated",
    "automatic_semantic_inference_performed",
    "runtime_sentinel_enabled",
)
ACTION_EVIDENCE_ROLES: Final = {
    "task_instruction",
    "target_pre",
    "tool_response",
    "ask_user_response",
}
TRANSFORMATION_EVIDENCE_ROLES: Final = {
    *ACTION_EVIDENCE_ROLES,
    "source_history",
    "source_pre",
}
FORBIDDEN_PACKET_KEYS: Final = {
    "post_action_audit",
    "natural_decision",
    "original_response",
    "post_state_ref",
    "executor_result_ref",
    "outcome",
    "task_ended",
    "replay_response",
    "treatment_response",
    "benchmark_checker",
}
DELIMITER_REPAIR_PATTERNS: Final = {
    "DELETE_EMPTY_DELIMITER": (
        re.compile(r"<thinking>\s*"),
        re.compile(r"\s*</thinking>"),
    ),
    "DELETE_ORPHAN_SEPARATOR": (
        re.compile(r"(?:Step\s+[1-9]\d*|Thought)\s*:\s*"),
        re.compile(r"\s*;\s*"),
    ),
}
G1_5_PREVIEW_SCHEMA_SHA256: Final = (
    "36e7edf94c7439750db8f252003a1b6d1c11ab4dad2a69c3a5ff1fecbf1764ca"
)
G1_5_CPU_PUBLICATION_SHA256: Final = (
    "cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018"
)
G1_5_PREVIEW_SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[5]
    / "mobileworld_audit_handoff"
    / "schemas"
    / "g1_5"
    / "history_codec_preview.schema.json"
)
PINNED_PREVIEW_TOKENIZERS: Final = {
    "qwen3vl_8b": {
        "tokenizer_id": "Qwen/Qwen3-VL-8B-Instruct",
        "tokenizer_sha256": ("e97afc56a6ce6b1d0d78345efc2b27c9853e9251d1e2f2bb0ff60b9b99926efd"),
    },
    "mai_ui_8b": {
        "tokenizer_id": "Tongyi-MAI/MAI-UI-8B",
        "tokenizer_sha256": ("dac3c7c7da1bcb043402cb3571a0867f98153c4fd3f3c0614153a6ea27518d23"),
    },
}
TRANSFORMATION_PREVIEW_INPUT_KEYS: Final = {
    "focal_target_spans",
    "oracle_target_spans",
    "correction_candidates",
    "correction_evidence_ids",
    "protected_spans",
    "delimiter_repairs",
    "sham_span",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@lru_cache(maxsize=1)
def _g1_5_preview_validator() -> Draft202012Validator:
    try:
        data = G1_5_PREVIEW_SCHEMA_PATH.read_bytes()
        require(
            _sha256(data) == G1_5_PREVIEW_SCHEMA_SHA256,
            "PREVIEW_SCHEMA_INVALID",
            "G1.5 preview schema bytes differ from the frozen CPU publication",
        )
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError(
            "PREVIEW_SCHEMA_INVALID", "G1.5 preview schema cannot be loaded"
        ) from exc
    require(
        isinstance(value, dict),
        "PREVIEW_SCHEMA_INVALID",
        "G1.5 preview schema is not an object",
    )
    schema = cast(dict[str, Any], value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise CurationError(
            "PREVIEW_SCHEMA_INVALID", "G1.5 preview schema fails meta-validation"
        ) from exc
    return Draft202012Validator(schema)


def _validate_g1_5_preview_record(value: dict[str, Any]) -> None:
    errors = sorted(_g1_5_preview_validator().iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.absolute_path
        )
        raise CurationError(
            "PREVIEW_SCHEMA_MISMATCH",
            f"G1.5 preview schema rejects CPU output at {path}: {first.message}",
        )


def _delimiter_repair_is_causally_empty(
    text: str,
    selected_syntax: str,
    *,
    start: int,
    end: int,
    target_intervals: list[tuple[int, int]],
    repair_intervals: list[tuple[int, int]],
    replacement_intervals: list[tuple[int, int, str]],
) -> bool:
    """Mirror the frozen formal G1 delimiter-repair emptiness proof."""

    stripped = selected_syntax.strip()
    all_intervals = [*target_intervals, *repair_intervals]
    if re.fullmatch(r"(?:Step\s+[1-9]\d*|Thought)\s*:", stripped) is not None:
        if stripped.startswith("Step"):
            line_start = text.rfind("\n", 0, start) + 1
            previous_separator = text.rfind(";", line_start, start)
            scope_start = max(line_start, previous_separator + 1)
            next_separator = text.find(";", end)
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            scope_end = next_separator + 1 if 0 <= next_separator < line_end else line_end
        else:
            scope_start = text.rfind("\n", 0, start) + 1
            scope_end = text.find("\n", end)
            if scope_end < 0:
                scope_end = len(text)
    elif stripped in {"<thinking>", "</thinking>"}:
        if stripped == "<thinking>":
            opening_start = start + selected_syntax.index("<thinking>")
            opening_end = opening_start + len("<thinking>")
            closing_start = text.find("</thinking>", opening_end)
        else:
            closing_start = start + selected_syntax.index("</thinking>")
            opening_start = text.rfind("<thinking>", 0, closing_start)
            opening_end = opening_start + len("<thinking>")
        if opening_start < 0 or closing_start < 0:
            return False
        closing_end = closing_start + len("</thinking>")
        if not (
            any(left <= opening_start and opening_end <= right for left, right in repair_intervals)
            and any(
                left <= closing_start and closing_end <= right for left, right in repair_intervals
            )
        ):
            return False
        scope_start, scope_end = opening_end, closing_start
    elif stripped == ";":
        semicolon = start + selected_syntax.index(";")
        line_start = text.rfind("\n", 0, semicolon) + 1
        previous_separator = text.rfind(";", line_start, semicolon)
        scope_start = max(line_start, previous_separator + 1)
        scope_end = semicolon + 1
        if not any(
            scope_start <= target_start < target_end <= semicolon
            for target_start, target_end in target_intervals
        ):
            return False
    else:
        return False
    if not any(
        scope_start <= target_start < target_end <= scope_end
        for target_start, target_end in target_intervals
    ):
        return False
    cursor = scope_start
    remaining: list[str] = []
    for left, right in sorted(all_intervals):
        clipped_left = max(scope_start, left)
        clipped_right = min(scope_end, right)
        if clipped_left >= clipped_right or clipped_right <= cursor:
            continue
        if clipped_left > cursor:
            remaining.append(text[cursor:clipped_left])
        cursor = max(cursor, clipped_right)
    if cursor < scope_end:
        remaining.append(text[cursor:scope_end])
    remaining.extend(
        replacement
        for left, right, replacement in replacement_intervals
        if scope_start <= left < right <= scope_end
    )
    return "".join(remaining).strip() == ""


def _validate_delimiter_repairs(
    *,
    unit_kind: str,
    history_family: str,
    payload: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> None:
    repairs = cast(list[dict[str, Any]], payload.get("delimiter_repairs", []))
    if not repairs:
        return
    allowed_arms = (
        {"SHAM_BENIGN_EDIT"}
        if unit_kind == "CLEAN_CONTROL"
        else {"MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"}
    )
    require(
        all(repair["arm"] in allowed_arms for repair in repairs),
        "DELIMITER_REPAIR_INVALID",
        "delimiter repair targets an arm absent from this unit protocol",
    )
    selected_by_arm: dict[str, list[dict[str, Any]]] = {
        "MASK": cast(list[dict[str, Any]], payload.get("focal_target_spans", [])),
        "MASK_CORRECTION": cast(list[dict[str, Any]], payload.get("focal_target_spans", [])),
        "ORACLE_CLEAN": cast(list[dict[str, Any]], payload.get("oracle_target_spans", [])),
        "SHAM_BENIGN_EDIT": (
            [cast(dict[str, Any], payload["sham_span"])]
            if isinstance(payload.get("sham_span"), dict)
            else []
        ),
    }
    repairs_by_arm: dict[str, list[dict[str, Any]]] = {}
    for repair in repairs:
        repairs_by_arm.setdefault(repair["arm"], []).append(repair)
    for arm, arm_repairs in repairs_by_arm.items():
        selected_targets = selected_by_arm[arm]
        target_intervals: dict[str, list[tuple[int, int]]] = {}
        for target in selected_targets:
            target_intervals.setdefault(target["record_id"], []).append(
                (target["char_start"], target["char_end"])
            )
        declared_repairs: dict[str, list[tuple[int, int]]] = {}
        for repair in arm_repairs:
            span = repair["deleted_syntax_span"]
            declared_repairs.setdefault(span["record_id"], []).append(
                (span["char_start"], span["char_end"])
            )
        for record_intervals in declared_repairs.values():
            ordered = sorted(record_intervals)
            require(
                all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair spans overlap",
            )
        protected_by_record: dict[str, list[tuple[int, int]]] = {}
        for protected in cast(list[dict[str, Any]], payload.get("protected_spans", [])):
            protected_by_record.setdefault(protected["record_id"], []).append(
                (protected["char_start"], protected["char_end"])
            )
        replacement_intervals: dict[str, list[tuple[int, int, str]]] = {}
        if arm == "MASK_CORRECTION":
            replacement = payload.get("correction_text")
            require(
                isinstance(replacement, str) and bool(replacement.strip()),
                "DELIMITER_REPAIR_INVALID",
                "mask-correction delimiter proof requires correction bytes",
            )
            replacement_text = cast(str, replacement)
            for target in selected_targets:
                replacement_intervals.setdefault(target["record_id"], []).append(
                    (target["char_start"], target["char_end"], replacement_text)
                )
        for repair in arm_repairs:
            span = repair["deleted_syntax_span"]
            record_id = span["record_id"]
            intervals = target_intervals.get(record_id, [])
            require(
                bool(intervals),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair changes a record not selected for its arm",
            )
            text = records[record_id]["exact_text"]
            selected = span["exact_text"]
            stripped = selected.strip()
            family_syntax_valid = (
                history_family == "flat_progress"
                and (re.fullmatch(r"Step\s+[1-9]\d*\s*:", stripped) is not None or stripped == ";")
            ) or (
                history_family == "raw_replay"
                and (
                    re.fullmatch(r"Thought\s*:", stripped) is not None
                    or stripped in {"<thinking>", "</thinking>"}
                )
            )
            require(
                family_syntax_valid
                and any(
                    pattern.fullmatch(selected) is not None
                    for pattern in DELIMITER_REPAIR_PATTERNS[repair["operation"]]
                ),
                "DELIMITER_REPAIR_INVALID",
                "deleted syntax is outside the formal delimiter whitelist",
            )
            start, end = span["char_start"], span["char_end"]
            require(
                all(end <= left or right <= start for left, right in intervals),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair overlaps a semantic edit",
            )
            require(
                any(
                    (
                        end <= target_start
                        and (not text[end:target_start] or text[end:target_start].isspace())
                    )
                    or (
                        target_end <= start
                        and (not text[target_end:start] or text[target_end:start].isspace())
                    )
                    for target_start, target_end in intervals
                ),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair is not adjacent to a selected target",
            )
            require(
                all(
                    end <= left or right <= start
                    for left, right in protected_by_record.get(record_id, [])
                ),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair overlaps a protected span",
            )
            require(
                _delimiter_repair_is_causally_empty(
                    text,
                    selected,
                    start=start,
                    end=end,
                    target_intervals=intervals,
                    repair_intervals=declared_repairs[record_id],
                    replacement_intervals=replacement_intervals.get(record_id, []),
                ),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair is not causally empty after the arm edit",
            )


def _safe_parts(relative_path: Any) -> tuple[str, ...]:
    require(
        isinstance(relative_path, str) and bool(relative_path),
        "REFERENCE_INVALID",
        "artifact path is missing",
    )
    assert isinstance(relative_path, str)
    path = PurePosixPath(relative_path)
    require(not path.is_absolute(), "REFERENCE_INVALID", "artifact path must be relative")
    require(
        bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
        and str(path) == relative_path
        and "//" not in relative_path,
        "REFERENCE_INVALID",
        "artifact path is not canonical",
    )
    return path.parts


def _open_regular_beneath(root: Path, relative_path: str) -> bytes:
    parts = _safe_parts(relative_path)
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = root_flags
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root, root_flags)
    except OSError as exc:
        raise CurationError("SOURCE_ROOT_INVALID", "artifact root cannot be opened safely") from exc
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=fd)
            except OSError as exc:
                raise CurationError(
                    "REFERENCE_UNRESOLVED", "artifact directory cannot be opened safely"
                ) from exc
            os.close(fd)
            fd = next_fd
        try:
            file_fd = os.open(parts[-1], file_flags, dir_fd=fd)
        except OSError as exc:
            raise CurationError("REFERENCE_UNRESOLVED", "artifact cannot be opened safely") from exc
        try:
            before = os.fstat(file_fd)
            require(
                stat.S_ISREG(before.st_mode), "REFERENCE_INVALID", "artifact is not a regular file"
            )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_fd)
            require(
                (before.st_dev, before.st_ino, before.st_size)
                == (after.st_dev, after.st_ino, after.st_size),
                "REFERENCE_CHANGED",
                "artifact changed while being read",
            )
            data = b"".join(chunks)
            require(
                len(data) == before.st_size,
                "REFERENCE_CHANGED",
                "artifact length changed while being read",
            )
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)


def _parse_json(data: bytes, label: str, *, jsonl: bool = False) -> Any:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("SOURCE_JSON_INVALID", f"{label} is not valid JSON") from exc
    canonical = canonical_json_bytes(value)
    if jsonl:
        require(
            data in {canonical, canonical + b"\n"},
            "SOURCE_JSON_NONCANONICAL",
            f"{label} is not canonical",
        )
    return value


def _check_forbidden_keys(value: Any, *, allowed_natural_action: bool = False) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_PACKET_KEYS:
                if not (allowed_natural_action and key == "natural_decision"):
                    raise CurationError(
                        "PACKET_VISIBILITY_VIOLATION", f"forbidden key {key} leaked into packet"
                    )
            _check_forbidden_keys(child, allowed_natural_action=allowed_natural_action)
    elif isinstance(value, list):
        for child in value:
            _check_forbidden_keys(child, allowed_natural_action=allowed_natural_action)


class CurationPublication:
    """Pinned G1.3 publication reader that returns channel-specific projections."""

    def __init__(
        self,
        root: str | os.PathLike[str] = ACTIVE_G1_3_PUBLICATION,
        *,
        source_run_roots: dict[str, Path] | None = None,
        preview_token_counters: Mapping[str, PinnedTokenCounter] | None = None,
    ) -> None:
        supplied = Path(root)
        require(
            not supplied.is_symlink(), "PUBLICATION_ROOT_INVALID", "publication root is a symlink"
        )
        try:
            self.root = supplied.resolve(strict=True)
        except OSError as exc:
            raise CurationError(
                "PUBLICATION_ROOT_INVALID", "publication root does not exist"
            ) from exc
        require(
            self.root.is_dir(), "PUBLICATION_ROOT_INVALID", "publication root is not a directory"
        )
        require(
            self.root.name == ACTIVE_G1_3_MANIFEST_SHA256,
            "PUBLICATION_NOT_PINNED",
            "publication is not the active v1.1 root",
        )
        self.source_run_roots = dict(source_run_roots or DEFAULT_SOURCE_RUN_ROOTS)
        raw_counters = dict(preview_token_counters or {})
        require(
            set(raw_counters) <= set(PINNED_PREVIEW_TOKENIZERS),
            "TOKENIZER_BINDING_INVALID",
            "preview token counter is bound to an unsupported model",
        )
        for model_id, counter in raw_counters.items():
            expected = PINNED_PREVIEW_TOKENIZERS[model_id]
            require(
                isinstance(counter, PinnedTokenCounter)
                and counter.tokenizer_id == expected["tokenizer_id"]
                and counter.tokenizer_sha256 == expected["tokenizer_sha256"],
                "TOKENIZER_BINDING_INVALID",
                "preview token counter differs from the frozen G1.5 tokenizer binding",
            )
        self._preview_token_counters = raw_counters
        self._manifest = self._load_and_validate_manifest()
        self._rows = self._load_index(self._manifest)
        self._row_by_unit = {row["unit_id"]: row for row in self._rows}
        require(
            len(self._row_by_unit) == 190,
            "PUBLICATION_INDEX_INVALID",
            "publication must contain 190 unique units",
        )

    def _load_and_validate_manifest(self) -> dict[str, Any]:
        data = _open_regular_beneath(self.root, "capsule_manifest.json")
        require(
            _sha256(data) == ACTIVE_G1_3_MANIFEST_SHA256,
            "PUBLICATION_NOT_PINNED",
            "manifest digest differs",
        )
        manifest = _parse_json(data, "capsule manifest", jsonl=True)
        require(
            isinstance(manifest, dict),
            "PUBLICATION_INDEX_INVALID",
            "capsule manifest must be an object",
        )
        require(
            manifest.get("capsule_set_sha256") == ACTIVE_G1_3_CAPSULE_SET_SHA256,
            "PUBLICATION_NOT_PINNED",
            "capsule set digest differs",
        )
        safety = manifest.get("safety", {})
        readiness = manifest.get("readiness", {})
        require(
            isinstance(safety, dict) and isinstance(readiness, dict),
            "PUBLICATION_GUARD_INVALID",
            "manifest guard records are missing",
        )
        for key in ("provider_invocation_allowed", "treatment_response_generation_allowed"):
            require(
                safety.get(key) is False and readiness.get(key) is False,
                "PUBLICATION_GUARD_INVALID",
                f"manifest guard {key} must be false",
            )
        require(
            readiness.get("execution_ready") is False,
            "PUBLICATION_GUARD_INVALID",
            "manifest execution guard must be false",
        )
        counts = manifest.get("counts")
        population = manifest.get("population")
        units = manifest.get("units")
        require(
            isinstance(counts, dict)
            and counts
            == {
                "capsuled_count": 190,
                "duplicate_unit_count": 0,
                "excluded_count": 0,
                "g1_1_source_record_mutation_count": 0,
                "manifest_unit_count": 190,
                "reserve_unit_capsule_count": 0,
                "target_unit_count": 190,
                "unaccounted_unit_count": 0,
            },
            "PUBLICATION_INDEX_INVALID",
            "manifest population counts differ from the active publication",
        )
        require(
            isinstance(population, dict)
            and population.get("target_unit_count") == 190
            and population.get("strict_mhr_candidate_frozen_count") == 152
            and population.get("selected_clean_control_count") == 38
            and population.get("qwen_target_count") == 169
            and population.get("mai_target_count") == 21,
            "PUBLICATION_INDEX_INVALID",
            "manifest population strata differ from the active publication",
        )
        require(
            isinstance(units, list) and len(units) == 190,
            "PUBLICATION_INDEX_INVALID",
            "manifest must bind exactly 190 unit rows",
        )
        return cast(dict[str, Any], manifest)

    def _load_index(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        files = manifest.get("files")
        require(
            isinstance(files, dict) and isinstance(files.get("capsule_index"), dict),
            "PUBLICATION_INDEX_INVALID",
            "manifest capsule-index reference is missing",
        )
        files_dict = cast(dict[str, Any], files)
        index_ref = cast(dict[str, Any], files_dict["capsule_index"])
        require(
            index_ref.get("relative_path") == "capsule_index.jsonl"
            and index_ref.get("media_type") == "application/x-ndjson",
            "PUBLICATION_INDEX_INVALID",
            "manifest capsule-index reference is not the frozen canonical reference",
        )
        data = _open_regular_beneath(self.root, cast(str, index_ref["relative_path"]))
        require(
            type(index_ref.get("byte_count")) is int
            and index_ref["byte_count"] == len(data)
            and isinstance(index_ref.get("sha256"), str)
            and index_ref["sha256"] == _sha256(data),
            "PUBLICATION_INDEX_INVALID",
            "capsule index bytes differ from the pinned manifest reference",
        )
        rows: list[dict[str, Any]] = []
        for index, line in enumerate(data.splitlines()):
            require(bool(line), "PUBLICATION_INDEX_INVALID", "capsule index contains a blank row")
            value = _parse_json(line, f"capsule index row {index}", jsonl=True)
            require(
                isinstance(value, dict),
                "PUBLICATION_INDEX_INVALID",
                "capsule index row is not an object",
            )
            row = cast(dict[str, Any], value)
            require(
                row.get("disposition") == "CAPSULED",
                "PUBLICATION_INDEX_INVALID",
                "index contains a non-capsule row",
            )
            require(
                row.get("unit_kind") in {"STRICT_MHR", "CLEAN_CONTROL"},
                "PUBLICATION_INDEX_INVALID",
                "unit kind is invalid",
            )
            require(
                row.get("model_id") in {"qwen3vl_8b", "mai_ui_8b"},
                "PUBLICATION_INDEX_INVALID",
                "model ID is invalid",
            )
            ref = row.get("capsule_ref")
            require(
                isinstance(ref, dict), "PUBLICATION_INDEX_INVALID", "capsule reference is missing"
            )
            ref_value = cast(dict[str, Any], ref)
            _safe_parts(ref_value.get("relative_path"))
            rows.append(row)
        require(
            len(rows) == 190, "PUBLICATION_INDEX_INVALID", "capsule index must contain 190 rows"
        )
        require(
            rows == manifest["units"],
            "PUBLICATION_INDEX_INVALID",
            "capsule index rows differ from the manifest unit inventory",
        )
        return rows

    def _load_capsule(self, unit_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self._row_by_unit.get(unit_id)
        require(row is not None, "UNIT_UNKNOWN", "unit is not in the active G1.3 publication")
        assert row is not None
        ref = row["capsule_ref"]
        data = _open_regular_beneath(self.root, ref["relative_path"])
        require(
            len(data) == ref["byte_count"], "CAPSULE_REF_MISMATCH", "capsule byte count differs"
        )
        require(_sha256(data) == ref["sha256"], "CAPSULE_REF_MISMATCH", "capsule digest differs")
        wrapper = _parse_json(data, "replay capsule", jsonl=True)
        require(
            isinstance(wrapper, dict) and isinstance(wrapper.get("capsule"), dict),
            "CAPSULE_INVALID",
            "capsule wrapper is invalid",
        )
        capsule = cast(dict[str, Any], wrapper["capsule"])
        require(
            wrapper.get("schema_version") == "mobileworld.g1.replay-capsule/v1.1",
            "CAPSULE_INVALID",
            "capsule schema generation is not active v1.1",
        )
        require(
            canonical_sha256(capsule)
            == wrapper.get("capsule_body_sha256")
            == row.get("capsule_body_sha256"),
            "CAPSULE_REF_MISMATCH",
            "capsule body digest differs",
        )
        unit = capsule.get("unit", {})
        require(
            isinstance(unit, dict)
            and unit.get("registry_record_ref", {}).get("record_id") == unit_id,
            "CAPSULE_INVALID",
            "capsule unit binding differs",
        )
        assert isinstance(unit, dict)
        require(
            all(
                unit.get(key) == row.get(key)
                for key in ("unit_id", "unit_kind", "model_id", "history_family", "source_key")
            ),
            "CAPSULE_INVALID",
            "capsule unit metadata differs from its pinned index row",
        )
        safety = capsule.get("safety", {})
        require(
            isinstance(safety, dict), "CAPSULE_GUARD_INVALID", "capsule safety record is missing"
        )
        for key in REQUIRED_FALSE_GUARDS:
            require(
                safety.get(key) is False,
                "CAPSULE_GUARD_INVALID",
                f"capsule guard {key} must be false",
            )
        require(
            type(safety.get("treatment_response_count")) is int
            and safety["treatment_response_count"] == 0,
            "CAPSULE_GUARD_INVALID",
            "capsule treatment count must be zero",
        )
        return row, capsule

    def _read_reference(self, ref: Any, *, expected_store: str | None = None) -> bytes:
        require(isinstance(ref, dict), "REFERENCE_INVALID", "content reference must be an object")
        store_id = ref.get("store_id")
        if expected_store is not None:
            require(
                store_id == expected_store,
                "REFERENCE_INVALID",
                "content reference has the wrong store",
            )
        if store_id == PUBLICATION_STORE_ID:
            root = self.root
        else:
            source_root = self.source_run_roots.get(cast(str, store_id))
            require(
                source_root is not None,
                "REFERENCE_STORE_UNKNOWN",
                "source run store is not explicitly admitted",
            )
            root = cast(Path, source_root)
            require(
                not root.is_symlink(), "REFERENCE_STORE_INVALID", "source run root is a symlink"
            )
        data = _open_regular_beneath(root, cast(str, ref.get("relative_path")))
        byte_count = ref.get("byte_count", ref.get("byte_length"))
        digest = ref.get("sha256", ref.get("digest"))
        require(
            type(byte_count) is int and byte_count == len(data),
            "REFERENCE_LENGTH_MISMATCH",
            "content reference byte count differs",
        )
        require(
            isinstance(digest, str) and digest == _sha256(data),
            "REFERENCE_HASH_MISMATCH",
            "content reference digest differs",
        )
        return data

    def _evidence_projection(self, capsule: dict[str, Any], channel: str) -> list[dict[str, Any]]:
        curator = capsule["curator_only"][channel.lower()]
        allowed = (
            ACTION_EVIDENCE_ROLES if channel == "ACTION_GOLD" else TRANSFORMATION_EVIDENCE_ROLES
        )
        evidence: list[dict[str, Any]] = []
        for item in curator["evidence_refs"]:
            role = item["evidence_role"]
            require(
                role in allowed,
                "PACKET_VISIBILITY_VIOLATION",
                "evidence role is forbidden for channel",
            )
            data = self._read_reference(item["content_ref"], expected_store=PUBLICATION_STORE_ID)
            content: Any
            media_type = item["content_ref"]["media_type"]
            if media_type.startswith("application/json"):
                content = _parse_json(data, "curation evidence", jsonl=True)
                if role == "target_pre" and isinstance(content, dict):
                    screenshot = content.get("screenshot")
                    content = {
                        "screenshot": {
                            "width": screenshot.get("width")
                            if isinstance(screenshot, dict)
                            else None,
                            "height": screenshot.get("height")
                            if isinstance(screenshot, dict)
                            else None,
                            "mode": screenshot.get("mode")
                            if isinstance(screenshot, dict)
                            else None,
                            "representation": screenshot.get("representation")
                            if isinstance(screenshot, dict)
                            else None,
                        },
                        "accessibility_tree": content.get("accessibility_tree"),
                        "tool_call": content.get("tool_call"),
                        "ask_user_response": content.get("ask_user_response"),
                    }
            else:
                content = data.decode("utf-8")
            evidence.append(
                {
                    "evidence_id": item["evidence_id"],
                    "evidence_role": role,
                    "content_sha256": item["content_ref"]["sha256"],
                    "model_visible_at_or_before_request": item[
                        "model_visible_at_or_before_request"
                    ],
                    "source_event": json_copy(item["source_event"]),
                    "content": content,
                }
            )
        roles = {item["evidence_role"] for item in evidence}
        require(
            {"task_instruction", "target_pre"} <= roles,
            "PACKET_EVIDENCE_INCOMPLETE",
            "packet lacks task/current-GUI evidence",
        )
        if channel == "TRANSFORMATION":
            require(
                "source_history" in roles,
                "PACKET_EVIDENCE_INCOMPLETE",
                "transformation packet lacks history",
            )
        return evidence

    def list_units(self) -> list[dict[str, Any]]:
        return [
            {
                "unit_id": row["unit_id"],
                "unit_kind": row["unit_kind"],
                "model_id": row["model_id"],
                "history_family": row["history_family"],
                "source_key": row["source_key"],
            }
            for row in self._rows
        ]

    def source_packet_binding(
        self,
        unit_id: str,
        channel: str,
        *,
        curation_resolution_set_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Build the reviewer-neutral, hash-only packet identity subject."""

        require(
            channel in {"ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT"},
            "CHANNEL_INVALID",
            "packet channel is invalid",
        )
        row, capsule = self._load_capsule(unit_id)
        evidence_channel = "TRANSFORMATION" if channel == "CONSISTENCY_AUDIT" else channel
        curator = capsule["curator_only"][evidence_channel.lower()]
        if channel == "CONSISTENCY_AUDIT":
            require(
                isinstance(curation_resolution_set_sha256, str)
                and len(curation_resolution_set_sha256) == 64
                and all(char in "0123456789abcdef" for char in curation_resolution_set_sha256),
                "CONSISTENCY_AUDIT_NOT_READY",
                "consistency source packet lacks the frozen curation resolution set",
            )
        else:
            require(
                curation_resolution_set_sha256 is None,
                "PACKET_BINDING_INVALID",
                "formal curation packet cannot bind a descriptive resolution set",
            )
        unit = capsule["unit"]
        subject: dict[str, Any] = {
            "contract_version": "mobileworld.g1.gold-history-intervention/contract-v1",
            "channel": channel,
            "input_policy": {
                "ACTION_GOLD": "mobileworld.g1.action-gold-pre-cutoff-no-history/v1",
                "TRANSFORMATION": "mobileworld.g1.transformation-pre-cutoff-history-no-target-output/v1",
                "CONSISTENCY_AUDIT": "mobileworld.g1.post-freeze-consistency-audit/v1",
            }[channel],
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
            "capsule_body_sha256": row["capsule_body_sha256"],
            "unit": {
                "unit_id": unit_id,
                "unit_kind": row["unit_kind"],
                "model_id": row["model_id"],
                "history_family": row["history_family"],
                "target_step": unit["target_step"],
                "request_cutoff_event_id": unit["request_cutoff"]["event_id"],
                "request_cutoff_event_seq": unit["request_cutoff"]["event_seq"],
            },
            "channel_input_sha256": canonical_sha256(curator),
            "ordered_evidence": [
                {
                    "evidence_id": item["evidence_id"],
                    "evidence_role": item["evidence_role"],
                    "projection_sha256": item["content_ref"]["sha256"],
                    "source_event_id": item["source_event"]["event_id"],
                    "source_event_seq": item["source_event"]["seq"],
                }
                for item in curator["evidence_refs"]
            ],
            "base_visibility": {
                "history_visible": channel != "ACTION_GOLD",
                "accepted_action_visible": False,
                "natural_target_output_visible": channel == "CONSISTENCY_AUDIT",
                "target_post_visible": False,
                "later_trajectory_visible": False,
                "outcome_visible": False,
                "replay_response_visible": False,
            },
            "curation_resolution_set_sha256": curation_resolution_set_sha256,
        }
        if channel == "CONSISTENCY_AUDIT":
            natural = capsule["post_action_audit"]["natural_decision"]
            subject["natural_action_sha256"] = natural["parsed_action_sha256"]
        digest = canonical_sha256(subject)
        return {
            "source_packet_id": "g1packet-" + digest[:24],
            "source_packet_sha256": digest,
            "source_packet": subject,
        }

    def packet(self, unit_id: str, channel: str) -> dict[str, Any]:
        require(
            channel in {"ACTION_GOLD", "TRANSFORMATION"},
            "CHANNEL_INVALID",
            "packet channel is invalid",
        )
        row, capsule = self._load_capsule(unit_id)
        unit = capsule["unit"]
        task = capsule["source_provenance"]["task"]
        packet: dict[str, Any] = {
            "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
            "record_type": "gold_curation_review_packet",
            "channel": channel,
            "packet_id": "g1packet-"
            + canonical_sha256(
                {"unit_id": unit_id, "channel": channel, "capsule": row["capsule_body_sha256"]}
            )[:24],
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_body_sha256": row["capsule_body_sha256"],
            "unit": {
                "unit_id": unit_id,
                "unit_kind": row["unit_kind"],
                "model_id": row["model_id"],
                "history_family": row["history_family"],
                "target_step": unit["target_step"],
                "request_cutoff_event_id": unit["request_cutoff"]["event_id"],
                "request_cutoff_event_seq": unit["request_cutoff"]["event_seq"],
            },
            "task": {
                "task_name": task["task_name"],
                "instruction": task["instruction"]["exact_text"],
                "instruction_sha256": task["instruction"]["utf8_sha256"],
            },
            "evidence": self._evidence_projection(capsule, channel),
            "current_screenshot": {
                "available": True,
                "width": capsule["runtime"]["non_history_envelope"]["current_screenshot"]["width"],
                "height": capsule["runtime"]["non_history_envelope"]["current_screenshot"][
                    "height"
                ],
                "sha256": capsule["runtime"]["non_history_envelope"]["current_screenshot"][
                    "pixel_blob"
                ]["sha256"],
            },
            "visibility": {
                "history_visible": channel == "TRANSFORMATION",
                "natural_target_output_visible": False,
                "target_post_visible": False,
                "later_trajectory_visible": False,
                "outcome_visible": False,
                "replay_response_visible": False,
            },
            "mechanical_source_suggestions_only": True,
        }
        if channel == "TRANSFORMATION":
            treatment = capsule["runtime"]["treatment_surface"]
            packet["source_records"] = json_copy(treatment["source_records"])
            packet["target_candidates"] = json_copy(treatment["target_exposures"])
            packet["target_candidate_status"] = (
                "G1_1_FROZEN_EXACT" if row["model_id"] == "qwen3vl_8b" else "G1_6_REVIEW_REQUIRED"
            )
            packet["reviewer_must_select_semantics"] = True
        _check_forbidden_keys(packet)
        return packet

    def consistency_packet(self, unit_id: str) -> dict[str, Any]:
        """Return descriptive natural-action evidence after gold curation is sealed.

        The server is responsible for gating this method until both primary and
        secondary reviews for the two curator channels have final resolution.
        """

        row, capsule = self._load_capsule(unit_id)
        base = self.packet(unit_id, "TRANSFORMATION")
        natural = capsule["post_action_audit"]["natural_decision"]
        action_data = self._read_reference(
            natural["parsed_action_ref"], expected_store=PUBLICATION_STORE_ID
        )
        try:
            normalized_action = json.loads(action_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CurationError(
                "NATURAL_ACTION_INVALID", "natural action artifact is invalid JSON"
            ) from exc
        packet = {
            **base,
            "channel": "CONSISTENCY_AUDIT",
            "packet_id": "g1packet-"
            + canonical_sha256(
                {
                    "unit_id": unit_id,
                    "channel": "CONSISTENCY_AUDIT",
                    "capsule": row["capsule_body_sha256"],
                }
            )[:24],
            "natural_action": {
                "normalized_action": normalized_action,
                "normalized_action_sha256": natural["parsed_action_sha256"],
                "parse_outcome": natural["parse_outcome"],
                "historical_reference_only": True,
            },
            "visibility": {
                "history_visible": True,
                "natural_target_output_visible": True,
                "target_post_visible": False,
                "later_trajectory_visible": False,
                "outcome_visible": False,
                "replay_response_visible": False,
            },
            "descriptive_only_not_gold_input": True,
            "replay_response_used": False,
        }
        return packet

    def preview_tokenizer_status(self) -> dict[str, bool]:
        """Report only local counter availability; never expose paths or tokenizer bytes."""

        return {
            model_id: model_id in self._preview_token_counters
            for model_id in sorted(PINNED_PREVIEW_TOKENIZERS)
        }

    def _preview_counter(self, model_id: str) -> PinnedTokenCounter:
        counter = self._preview_token_counters.get(model_id)
        require(
            counter is not None,
            "PINNED_TOKENIZER_UNAVAILABLE",
            "the exact local pinned tokenizer counter is unavailable; preview and finalization "
            "remain blocked",
        )
        assert counter is not None
        return counter

    def _preview_application_request(self, capsule: dict[str, Any]) -> JsonValue:
        semantic = capsule.get("runtime", {}).get("model_visible", {}).get("semantic_request")
        require(
            isinstance(semantic, dict),
            "PREVIEW_REQUEST_INVALID",
            "capsule semantic request binding is missing",
        )
        ref = semantic.get("canonical_semantic_request_ref")
        data = self._read_reference(ref, expected_store=PUBLICATION_STORE_ID)
        request = _parse_json(data, "canonical semantic request", jsonl=True)
        require(
            semantic.get("canonical_semantic_request_sha256") == _sha256(data)
            and canonical_json_bytes(request) in {data, data.removesuffix(b"\n")},
            "PREVIEW_REQUEST_INVALID",
            "canonical semantic request bytes differ from the capsule binding",
        )
        return cast(JsonValue, request)

    @staticmethod
    def _preview_shape_payload(*, row: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        clean = row["unit_kind"] == "CLEAN_CONTROL"
        candidates = cast(list[dict[str, Any]], inputs["correction_candidates"])
        correction_text = "" if clean or not candidates else candidates[0].get("text", "")
        return {
            "unit_kind": row["unit_kind"],
            "history_family": row["history_family"],
            "disposition": "ACCEPT",
            "focal_target_spans": json_copy(inputs["focal_target_spans"]),
            "oracle_target_spans": json_copy(inputs["oracle_target_spans"]),
            "correction_text": correction_text,
            "correction_evidence_ids": json_copy(inputs["correction_evidence_ids"]),
            "protected_spans": json_copy(inputs["protected_spans"]),
            "delimiter_repairs": json_copy(inputs["delimiter_repairs"]),
            "sham_span": json_copy(inputs["sham_span"]),
            "preview_receipt_sha256": "0" * 64,
        }

    @staticmethod
    def _preview_binding_id(span: dict[str, Any], role: SpanRole) -> str:
        return (
            "g1binding-"
            + canonical_sha256(
                {
                    "record_id": span["record_id"],
                    "char_start": span["char_start"],
                    "char_end": span["char_end"],
                    "span_sha256": span["span_sha256"],
                    "span_role": role.value,
                }
            )[:24]
        )

    def _build_transformation_preview(
        self, unit_id: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        require(
            all(key in value for key in TRANSFORMATION_PREVIEW_INPUT_KEYS),
            "PREVIEW_INPUT_INVALID",
            "transformation preview input is incomplete",
        )
        inputs = {key: json_copy(value[key]) for key in TRANSFORMATION_PREVIEW_INPUT_KEYS}
        row, capsule = self._load_capsule(unit_id)
        inputs = validate_transformation_preview_inputs(
            inputs, clean_control=row["unit_kind"] == "CLEAN_CONTROL"
        )
        shape_payload = self._preview_shape_payload(row=row, inputs=inputs)
        binding_payload = cast(dict[str, Any], json_copy(shape_payload))
        binding_payload["preview_receipt_sha256"] = None
        self.validate_review_payload_binding(unit_id, "TRANSFORMATION", binding_payload)

        application_request = self._preview_application_request(capsule)
        treatment = capsule["runtime"]["treatment_surface"]
        codec_type = (
            QwenFlatProgressHistoryCodec
            if row["model_id"] == "qwen3vl_8b"
            else MaiRawReplayHistoryCodec
        )
        base_codec = codec_type()
        selections_by_id: dict[str, dict[str, JsonValue]] = {}

        def bind(span: dict[str, Any], role: SpanRole) -> str:
            binding_id = self._preview_binding_id(span, role)
            selection: dict[str, JsonValue] = {
                "binding_id": binding_id,
                "record_id": span["record_id"],
                "char_start": span["char_start"],
                "char_end": span["char_end"],
                "utf8_byte_start": span["utf8_byte_start"],
                "utf8_byte_end": span["utf8_byte_end"],
                "exact_text": span["exact_text"],
                "span_sha256": span["span_sha256"],
                "span_role": role.value,
                "human_selected": True,
            }
            previous = selections_by_id.setdefault(binding_id, selection)
            require(
                previous == selection,
                "PREVIEW_BINDING_COLLISION",
                "two human spans collide on one preview binding ID",
            )
            return binding_id

        focal_spans = cast(list[dict[str, Any]], inputs["focal_target_spans"])
        oracle_spans = cast(list[dict[str, Any]], inputs["oracle_target_spans"])
        sham_span = cast(dict[str, Any], inputs["sham_span"])
        focal_ids = tuple(bind(span, SpanRole.EDITABLE_CLAIM) for span in focal_spans)
        oracle_ids = tuple(bind(span, SpanRole.EDITABLE_CLAIM) for span in oracle_spans)
        sham_id = bind(sham_span, SpanRole.BENIGN_SHAM)
        repair_rows: list[tuple[dict[str, Any], str]] = []
        for repair in cast(list[dict[str, Any]], inputs["delimiter_repairs"]):
            shell_id = bind(
                cast(dict[str, Any], repair["deleted_syntax_span"]),
                SpanRole.ELIGIBLE_PROTOCOL_SHELL,
            )
            repair_rows.append((repair, shell_id))

        bindings = bind_human_record_spans(
            application_request=application_request,
            base_codec=base_codec,
            source_records=cast(list[dict[str, JsonValue]], treatment["source_records"]),
            selections=tuple(selections_by_id.values()),
        )
        codec = codec_type(bindings)
        targets_by_arm: dict[ArmKind, tuple[tuple[str, str], ...]] = {
            ArmKind.MASK: tuple(
                (span["record_id"], binding_id)
                for span, binding_id in zip(focal_spans, focal_ids, strict=True)
            ),
            ArmKind.MASK_CORRECTION: tuple(
                (span["record_id"], binding_id)
                for span, binding_id in zip(focal_spans, focal_ids, strict=True)
            ),
            ArmKind.ORACLE_CLEAN: tuple(
                (span["record_id"], binding_id)
                for span, binding_id in zip(oracle_spans, oracle_ids, strict=True)
            ),
            ArmKind.SHAM_BENIGN_EDIT: ((sham_span["record_id"], sham_id),),
        }
        repairs: list[DelimiterRepairBinding] = []
        for index, (repair, shell_id) in enumerate(repair_rows):
            arm = ArmKind(repair["arm"])
            repair_record_id = repair["deleted_syntax_span"]["record_id"]
            target_ids = tuple(
                binding_id
                for record_id, binding_id in targets_by_arm[arm]
                if record_id == repair_record_id
            )
            require(
                bool(target_ids),
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair has no selected semantic target in the same record",
            )
            repairs.append(
                DelimiterRepairBinding(
                    repair_id="g1repair-"
                    + canonical_sha256(
                        {
                            "index": index,
                            "arm": repair["arm"],
                            "operation": repair["operation"],
                            "shell_binding_id": shell_id,
                            "target_binding_ids": list(target_ids),
                        }
                    )[:24],
                    arm=arm,
                    operation=repair["operation"],
                    shell_binding_id=shell_id,
                    target_binding_ids=target_ids,
                )
            )

        evidence_by_id = {
            item["evidence_id"]: item
            for item in capsule["curator_only"]["transformation"]["evidence_refs"]
        }
        correction_refs = tuple(
            EvidenceRef(
                evidence_id=evidence_id,
                sha256=evidence_by_id[evidence_id]["content_ref"]["sha256"],
                role=evidence_by_id[evidence_id]["evidence_role"],
                event_seq=evidence_by_id[evidence_id]["source_event"]["seq"],
            )
            for evidence_id in cast(list[str], inputs["correction_evidence_ids"])
        )
        require(
            all(
                evidence_by_id[item.evidence_id]["evidence_role"] != "source_history"
                for item in correction_refs
            ),
            "NO_VALID_CORRECTION",
            "a potentially misleading source-history record cannot prove its own correction",
        )
        counter = self._preview_counter(row["model_id"])
        try:
            if row["unit_kind"] == "CLEAN_CONTROL":
                record = cast(
                    dict[str, Any],
                    build_clean_control_preview(
                        application_request=application_request,
                        codec=codec,
                        focal_reference_binding_id=focal_ids[0],
                        sham_binding_id=sham_id,
                        token_counter=counter,
                        delimiter_repairs=tuple(repairs),
                    ).to_dict(),
                )
            else:
                record = cast(
                    dict[str, Any],
                    build_five_arm_preview(
                        application_request=application_request,
                        codec=codec,
                        focal_binding_ids=focal_ids,
                        oracle_binding_ids=oracle_ids,
                        sham_binding_id=sham_id,
                        correction_candidates=tuple(
                            item["text"]
                            for item in cast(list[dict[str, str]], inputs["correction_candidates"])
                        ),
                        correction_evidence_refs=correction_refs,
                        token_counter=counter,
                        delimiter_repairs=tuple(repairs),
                    ).to_dict(),
                )
        except PortableContractError as exc:
            message = str(exc).partition(": ")[2] or str(exc)
            raise CurationError(exc.code, message) from exc
        _validate_g1_5_preview_record(record)
        require(
            all(
                arm.get("target_only_diff") is True
                and arm.get("source_mapping_reversible") is True
                and arm.get("provider_invocation_allowed") is False
                for arm in record["arms"]
            )
            and all(
                record.get(key) is False
                for key in (
                    "provider_invocation_allowed",
                    "treatment_response_generation_allowed",
                    "network_used",
                    "gpu_used",
                    "replay_executed",
                    "gui_action_executed",
                )
            ),
            "PREVIEW_SAFETY_INVALID",
            "G1.5 CPU preview safety or reversible target-only guards differ",
        )
        require(
            type(record["sham_token_match"]["focal_token_count"]) is int
            and record["sham_token_match"]["focal_token_count"] > 0
            and type(record["sham_token_match"]["sham_token_count"]) is int
            and record["sham_token_match"]["sham_token_count"] > 0
            and (
                record["correction_ranking"] is None
                or all(
                    type(candidate["token_count"]) is int and candidate["token_count"] > 0
                    for candidate in record["correction_ranking"]["candidates"]
                )
            ),
            "TOKEN_COUNTER_INVALID",
            "non-empty selected history or correction text must have a positive token count",
        )
        forbidden_keys = {
            "application_request",
            "original_request",
            "rendered_request",
            "sdk_arguments",
            "image_url",
        }

        def reject_full_request(child: Any) -> None:
            if isinstance(child, dict):
                require(
                    not (set(child) & forbidden_keys),
                    "PREVIEW_VISIBILITY_VIOLATION",
                    "CPU preview attempted to expose a full request or image payload",
                )
                for nested in child.values():
                    reject_full_request(nested)
            elif isinstance(child, list):
                for nested in child:
                    reject_full_request(nested)

        reject_full_request(record)
        receipt_subject = {
            "schema_version": "mobileworld.g1.gold-curation-preview-receipt-subject/v1",
            "unit_id": unit_id,
            "capsule_body_sha256": row["capsule_body_sha256"],
            "g1_5_cpu_publication_sha256": G1_5_CPU_PUBLICATION_SHA256,
            "preview_schema_sha256": G1_5_PREVIEW_SCHEMA_SHA256,
            "source_packet_sha256": self.source_packet_binding(unit_id, "TRANSFORMATION")[
                "source_packet_sha256"
            ],
            "preview_inputs_sha256": canonical_sha256(inputs),
            "preview": record,
        }
        return {
            **record,
            "preview_receipt_sha256": canonical_sha256(receipt_subject),
        }

    def build_transformation_preview(self, unit_id: str, preview_inputs: Any) -> dict[str, Any]:
        """Build a closed CPU-only preview from assignment-unblinded human selections."""

        require(
            isinstance(preview_inputs, dict)
            and set(preview_inputs) == TRANSFORMATION_PREVIEW_INPUT_KEYS,
            "PREVIEW_INPUT_INVALID",
            "transformation preview request shape is not closed",
        )
        try:
            return self._build_transformation_preview(unit_id, cast(dict[str, Any], preview_inputs))
        except PortableContractError as exc:
            message = str(exc).partition(": ")[2] or str(exc)
            raise CurationError(exc.code, message) from exc

    def screenshot_bytes(self, unit_id: str) -> tuple[bytes, str, str]:
        _, capsule = self._load_capsule(unit_id)
        screenshot = capsule["runtime"]["non_history_envelope"]["current_screenshot"]
        ref = screenshot["pixel_blob"]
        data = self._read_reference(ref)
        require(
            ref.get("media_type") == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"),
            "SCREENSHOT_INVALID",
            "target-pre screenshot is not a canonical PNG artifact",
        )
        try:
            with Image.open(io.BytesIO(data)) as image:
                require(
                    image.format == "PNG"
                    and image.mode == "RGBA"
                    and image.size == (screenshot["width"], screenshot["height"])
                    and image.width * image.height <= 10_000_000,
                    "SCREENSHOT_INVALID",
                    "target-pre screenshot dimensions or mode differ",
                )
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise CurationError("SCREENSHOT_INVALID", "target-pre PNG failed decoding") from exc
        return data, cast(str, ref["media_type"]), cast(str, ref["sha256"])

    def record_bindings(self, unit_id: str) -> dict[str, dict[str, str]]:
        """Derive the frozen formal record identity/path for every selectable source record."""

        _, capsule = self._load_capsule(unit_id)
        request_event_id = capsule["unit"]["request_event_id"]
        records = capsule["runtime"]["treatment_surface"]["source_records"]
        result: dict[str, dict[str, str]] = {}
        for record in records:
            path = "payload.request_view" + "".join(
                f"[{part}]" if type(part) is int else f".{part}"
                for part in record["container_path"]
            )
            identity = hashlib.sha256(
                "\x1f".join(
                    (
                        "request-record",
                        request_event_id,
                        path,
                        record["record_sha256"],
                    )
                ).encode()
            ).hexdigest()
            require(
                record["record_id"] not in result,
                "SOURCE_HISTORY_INVALID",
                "source record IDs are not unique",
            )
            result[record["record_id"]] = {
                "record_identity_sha256": identity,
                "request_path": path,
            }
        for candidate in capsule["runtime"]["treatment_surface"]["target_exposures"]:
            matches = [
                record
                for record in records
                if record["record_sha256"] == candidate["container_sha256"]
                and record["container_path"] == candidate["semantic_request_container_path"]
            ]
            require(
                len(matches) == 1
                and result[matches[0]["record_id"]]
                == {
                    "record_identity_sha256": candidate["record_identity_sha256"],
                    "request_path": candidate["registry_request_path"],
                },
                "SOURCE_HISTORY_INVALID",
                "derived record identity differs from the frozen target exposure",
            )
        return result

    def validate_review_payload_binding(
        self, unit_id: str, channel: str, payload: dict[str, Any]
    ) -> None:
        """Mechanically bind reviewer selections to their admitted packet bytes."""

        row, capsule = self._load_capsule(unit_id)
        if channel == "ACTION_GOLD":
            screenshot = capsule["runtime"]["non_history_envelope"]["current_screenshot"]
            width, height = screenshot["width"], screenshot["height"]
            admitted_evidence_ids = {
                item["evidence_id"]
                for item in capsule["curator_only"]["action_gold"]["evidence_refs"]
            }
            for predicate in payload.get("predicates", []):
                require(
                    set(predicate["evidence_ids"]) <= admitted_evidence_ids,
                    "PACKET_VISIBILITY_VIOLATION",
                    "action predicate cites evidence outside its admitted blind packet",
                )
                regions = [
                    *predicate.get("regions", []),
                    *predicate.get("start_regions", []),
                    *predicate.get("end_regions", []),
                ]
                for region in regions:
                    if region["shape"] == "BOUNDING_BOX":
                        require(
                            0 <= region["x_min"] < region["x_max"] <= width
                            and 0 <= region["y_min"] < region["y_max"] <= height,
                            "ACTION_REGION_OUTSIDE_SCREENSHOT",
                            "accepted coordinate region is outside the target-pre screenshot",
                        )
                    else:
                        require(
                            all(
                                0 <= vertex[0] < width and 0 <= vertex[1] < height
                                for vertex in region["vertices"]
                            ),
                            "ACTION_REGION_OUTSIDE_SCREENSHOT",
                            "accepted polygon is outside the target-pre screenshot",
                        )
            return
        if channel == "CONSISTENCY_AUDIT":
            require(
                payload.get("replay_response_used") is False,
                "CONSISTENCY_AUDIT_CONTAMINATED",
                "consistency label used replay evidence",
            )
            return
        treatment = capsule["runtime"]["treatment_surface"]
        require(
            payload.get("unit_kind") == row["unit_kind"]
            and payload.get("history_family") == row["history_family"],
            "PROPOSAL_BINDING_INVALID",
            "transformation profile differs from the frozen unit",
        )
        records = {record["record_id"]: record for record in treatment["source_records"]}

        def bind_span(span: dict[str, Any], label: str) -> None:
            record = records.get(span["record_id"])
            require(
                record is not None,
                "TARGET_SPAN_UNRESOLVED",
                f"{label} record is not in source history",
            )
            assert record is not None
            exact_text = record["exact_text"]
            start, end = span["char_start"], span["char_end"]
            require(
                end <= len(exact_text) and exact_text[start:end] == span["exact_text"],
                "TARGET_SPAN_UNRESOLVED",
                f"{label} does not match exact source code points",
            )
            require(
                span["utf8_byte_start"] == len(exact_text[:start].encode("utf-8"))
                and span["utf8_byte_end"] == len(exact_text[:end].encode("utf-8")),
                "TARGET_SPAN_UNRESOLVED",
                f"{label} UTF-8 byte offsets differ from the source record",
            )
            digest = hashlib.sha256(span["exact_text"].encode()).hexdigest()
            require(
                span.get("span_sha256") == digest,
                "TARGET_SPAN_UNRESOLVED",
                f"{label} digest differs",
            )
            if row["model_id"] == "mai_ui_8b" and label not in {"sham", "protected"}:
                tool_start = exact_text.find("<tool_call>")
                tool_end = exact_text.find("</tool_call>")
                if tool_start >= 0:
                    protected_end = (
                        len(exact_text) if tool_end < 0 else tool_end + len("</tool_call>")
                    )
                    require(
                        end <= tool_start or start >= protected_end,
                        "TARGET_SPAN_PROTECTED",
                        "MAI target overlaps protected tool-call bytes",
                    )

        for key in ("focal_target_spans", "oracle_target_spans", "protected_spans"):
            for span in payload.get(key, []):
                bind_span(span, "protected" if key == "protected_spans" else key)
            spans = payload.get(key, [])
            require(
                spans
                == sorted(
                    spans,
                    key=lambda item: (
                        item["record_id"],
                        item["char_start"],
                        item["char_end"],
                        item["span_sha256"],
                    ),
                ),
                "TARGET_SPAN_UNRESOLVED",
                f"{key} must use canonical source order",
            )
            for previous, current in zip(spans, spans[1:], strict=False):
                if previous["record_id"] == current["record_id"]:
                    require(
                        previous["char_end"] <= current["char_start"],
                        "TARGET_SPAN_UNRESOLVED",
                        f"{key} contains overlapping spans",
                    )
        sham = payload.get("sham_span")
        if isinstance(sham, dict):
            bind_span(sham, "sham")
            for target in [
                *payload.get("focal_target_spans", []),
                *payload.get("oracle_target_spans", []),
            ]:
                if target["record_id"] == sham["record_id"]:
                    require(
                        sham["char_end"] <= target["char_start"]
                        or sham["char_start"] >= target["char_end"],
                        "NO_MATCHED_SHAM",
                        "sham span overlaps a treatment target",
                    )
        if (
            row["model_id"] == "qwen3vl_8b"
            and row["unit_kind"] == "STRICT_MHR"
            and payload.get("disposition") == "ACCEPT"
        ):
            frozen = [
                {
                    "record_id": treatment["source_records"][0]["record_id"],
                    "char_start": span["char_start"],
                    "char_end": span["char_end"],
                    "exact_text": span["exact_text"],
                    "span_sha256": span["span_sha256"],
                }
                for target in treatment["target_exposures"]
                for span in target["focal_edit_spans"]
            ]
            require(
                [
                    {
                        key: item[key]
                        for key in (
                            "record_id",
                            "char_start",
                            "char_end",
                            "exact_text",
                            "span_sha256",
                        )
                    }
                    for item in payload["focal_target_spans"]
                ]
                == frozen,
                "TARGET_SPAN_UNRESOLVED",
                "Qwen focal target must equal the frozen G1.1 exact exposure",
            )
        oracle_keys = {
            (span["record_id"], span["char_start"], span["char_end"], span["span_sha256"])
            for span in payload.get("oracle_target_spans", [])
        }
        focal_keys = {
            (span["record_id"], span["char_start"], span["char_end"], span["span_sha256"])
            for span in payload.get("focal_target_spans", [])
        }
        if row["unit_kind"] == "STRICT_MHR" and payload.get("disposition") == "ACCEPT":
            require(
                focal_keys <= oracle_keys,
                "NO_VALID_ORACLE_VIEW",
                "oracle target set must include every focal target",
            )
        if row["model_id"] == "mai_ui_8b" and payload.get("disposition") == "ACCEPT":
            protected = cast(list[dict[str, Any]], payload.get("protected_spans", []))
            require(
                any("<tool_call>" in item["exact_text"] for item in protected),
                "TARGET_SPAN_PROTECTED",
                "MAI protected spans must include the exact tool-call region",
            )
            target_record_ids = {
                item["record_id"]
                for item in [
                    *cast(list[dict[str, Any]], payload.get("focal_target_spans", [])),
                    *cast(list[dict[str, Any]], payload.get("oracle_target_spans", [])),
                    *(
                        [cast(dict[str, Any], payload["sham_span"])]
                        if isinstance(payload.get("sham_span"), dict)
                        else []
                    ),
                ]
            }
            expected_tool_calls = {
                (
                    record_id,
                    match.start(),
                    match.end(),
                    hashlib.sha256(match.group(0).encode("utf-8")).hexdigest(),
                )
                for record_id in target_record_ids
                for match in re.finditer(
                    r"<tool_call>.*?</tool_call>",
                    records[record_id]["exact_text"],
                    flags=re.DOTALL,
                )
            }
            actual_tool_calls = {
                (
                    item["record_id"],
                    item["char_start"],
                    item["char_end"],
                    item["span_sha256"],
                )
                for item in protected
            }
            require(
                actual_tool_calls == expected_tool_calls,
                "TARGET_SPAN_PROTECTED",
                "MAI protected spans must equal the complete tool-call set",
            )
        if row["model_id"] == "qwen3vl_8b":
            require(
                payload.get("protected_spans") == [],
                "TARGET_SPAN_PROTECTED",
                "Qwen flat-progress records do not admit protected tool-call spans",
            )
        semantic_targets = [
            *cast(list[dict[str, Any]], payload.get("focal_target_spans", [])),
            *cast(list[dict[str, Any]], payload.get("oracle_target_spans", [])),
            *(
                [cast(dict[str, Any], payload["sham_span"])]
                if isinstance(payload.get("sham_span"), dict)
                else []
            ),
        ]
        for semantic_target in semantic_targets:
            for protected_span in cast(list[dict[str, Any]], payload.get("protected_spans", [])):
                if semantic_target["record_id"] == protected_span["record_id"]:
                    require(
                        semantic_target["char_end"] <= protected_span["char_start"]
                        or protected_span["char_end"] <= semantic_target["char_start"],
                        "TARGET_SPAN_PROTECTED",
                        "semantic target overlaps a protected source span",
                    )
        for repair in payload.get("delimiter_repairs", []):
            bind_span(repair["deleted_syntax_span"], "delimiter repair")
        _validate_delimiter_repairs(
            unit_kind=row["unit_kind"],
            history_family=row["history_family"],
            payload=payload,
            records=records,
        )
        evidence_ids = {
            item["evidence_id"]
            for item in capsule["curator_only"]["transformation"]["evidence_refs"]
        }
        require(
            set(payload.get("correction_evidence_ids", [])) <= evidence_ids,
            "NO_VALID_CORRECTION",
            "correction cites evidence outside the pre-cutoff transformation packet",
        )
        if (
            payload.get("disposition") == "ACCEPT"
            and payload.get("preview_receipt_sha256") is not None
        ):
            try:
                preview = self._build_transformation_preview(unit_id, payload)
            except PortableContractError as exc:
                message = str(exc).partition(": ")[2] or str(exc)
                raise CurationError(exc.code, message) from exc
            require(
                preview["preview_receipt_sha256"] == payload["preview_receipt_sha256"],
                "PREVIEW_RECEIPT_MISMATCH",
                "accepted proposal differs from its exact CPU preview receipt",
            )
            require(
                preview["sham_token_match"]["matched"] is True,
                "NO_MATCHED_SHAM",
                "accepted sham fails the frozen pinned-tokenizer size rule",
            )
            ranking = preview.get("correction_ranking")
            if row["unit_kind"] == "STRICT_MHR":
                require(
                    isinstance(ranking, dict)
                    and ranking["candidates"][0]["text"] == payload["correction_text"],
                    "NO_VALID_CORRECTION",
                    "final correction is not the deterministic minimal ranked candidate",
                )
