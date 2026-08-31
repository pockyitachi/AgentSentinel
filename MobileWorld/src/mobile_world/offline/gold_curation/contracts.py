"""Closed option catalogs and validation for the G1.6 annotation workspace.

These records are research-workspace inputs.  They are not formal G1 gold,
admission, execution, or provider-authorization artifacts.  Formal G1.6
outputs continue to use the frozen schemas under ``schemas/g1``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Final, cast

WORKSPACE_PROTOCOL_VERSION: Final = "mobileworld.g1.gold-curation-workspace/protocol-v1"
ANNOTATION_EVENT_SCHEMA_VERSION: Final = "mobileworld.g1.gold-curation-annotation-event/v1"
REVIEW_PROPOSAL_SCHEMA_VERSION: Final = "mobileworld.g1.gold-curation-review-proposal/v1"
REVIEW_PACKET_SCHEMA_VERSION: Final = "mobileworld.g1.gold-curation-review-packet/v1"

CHANNELS: Final = ("ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT")
REVIEW_STAGES: Final = ("PRIMARY", "SECONDARY")
REVIEW_ROLES: Final = tuple(f"{channel}_{stage}" for channel in CHANNELS for stage in REVIEW_STAGES)
ADJUDICATOR_ROLE: Final = "ADJUDICATOR"
ALL_ROLES: Final = (*REVIEW_ROLES, ADJUDICATOR_ROLE)

DISPOSITIONS: Final = ("ACCEPT", "EXCLUDE")
EXCLUSION_REASONS: Final = (
    "SOURCE_REFERENCE_UNRESOLVED",
    "REQUEST_HASH_MISMATCH",
    "STATE_HASH_MISMATCH",
    "TARGET_SPAN_UNRESOLVED",
    "PROVENANCE_BELOW_HIGH",
    "NOT_REFUTED_OR_STALE",
    "NO_EXPLICIT_UPTAKE",
    "NOT_STRICT_MHR",
    "ORIGINAL_ACTION_UNPARSEABLE",
    "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING",
    "FUTURE_EVIDENCE_LEAKAGE",
    "NO_GOLD_CONSENSUS",
    "NO_VALID_CORRECTION",
    "NO_VALID_ORACLE_VIEW",
    "NO_MATCHED_SHAM",
    "ARM_PROTOCOL_INVALID",
    "DUPLICATE_CAPSULE",
)

PREDICATE_KINDS: Final = (
    "EXACT_NORMALIZED_ACTION",
    "POINT_REGION",
    "DRAG_REGION",
    "TEXT_VARIANTS",
    "DIRECTION_SET",
)
ACTION_TYPES: Final = (
    "click",
    "double_tap",
    "scroll",
    "swipe",
    "input_text",
    "navigate_home",
    "navigate_back",
    "keyboard_enter",
    "open_app",
    "status",
    "wait",
    "long_press",
    "answer",
    "finished",
    "drag",
    "ask_user",
    "mcp",
)
POINT_ACTION_TYPES: Final = ("click", "double_tap", "long_press")
TEXT_ACTION_TYPES: Final = ("input_text", "open_app", "status", "answer", "finished", "ask_user")
DIRECTION_ACTION_TYPES: Final = ("scroll", "swipe")
DIRECTIONS: Final = ("left", "right", "up", "down", "any")
CARDINAL_DIRECTIONS: Final = DIRECTIONS[:-1]
NORMALIZED_ACTION_VALUE_FIELDS: Final = {
    "action_json",
    "action_name",
    "action_type",
    "app_name",
    "clear_text",
    "direction",
    "end_x",
    "end_y",
    "goal_status",
    "index",
    "keycode",
    "start_x",
    "start_y",
    "text",
    "x",
    "y",
}
COORDINATE_TOLERANCE_MODES: Final = ("PIXEL_RADIUS",)

CONSISTENCY_LABELS: Final = (
    "HISTORY_CONSISTENT_GUI_TASK_INCONSISTENT",
    "HISTORY_AND_GUI_TASK_CONSISTENT",
    "HISTORY_INCONSISTENT",
    "AMBIGUOUS",
    "UNPARSEABLE_ORIGINAL_ACTION",
)

TRANSFORMATION_DISAGREEMENT_FIELDS: Final = (
    "DISPOSITION",
    "FOCAL_TARGET_SET",
    "ORACLE_TARGET_SET",
    "CORRECTION_BYTES",
    "SHAM_SPAN",
    "SHAM_MATCH",
    "DELIMITER_REPAIR",
)
ACTION_DISAGREEMENT_FIELDS: Final = (
    "DISPOSITION",
    "ACCEPTED_ACTION_PREDICATES",
    "ACTION_TOLERANCE",
)
CONSISTENCY_DISAGREEMENT_FIELDS: Final = ("CONSISTENCY_LABEL",)

EVENT_KINDS: Final = ("DRAFT_SAVED", "REVIEW_SUBMITTED", "ADJUDICATION_SUBMITTED")
UNIT_ID_RE = re.compile(r"^(?:g1case|g1control)-[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CurationError(RuntimeError):
    """A stable, fail-closed G1.6 workspace error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def json_copy(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value))


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise CurationError(code, message)


def role_channel(role: str) -> str:
    require(role in ALL_ROLES, "ROLE_INVALID", "reviewer role is not in the closed catalog")
    if role == ADJUDICATOR_ROLE:
        return ADJUDICATOR_ROLE
    return role.rsplit("_", 1)[0]


def validate_identity(reviewer_id: Any, role: Any) -> tuple[str, str]:
    require(
        isinstance(reviewer_id, str)
        and bool(reviewer_id)
        and 1 <= len(reviewer_id.encode("utf-8")) <= 256
        and "\x00" not in reviewer_id,
        "REVIEWER_ID_INVALID",
        "reviewer ID must be a non-empty exact UTF-8 principal of at most 256 bytes",
    )
    require(isinstance(role, str) and role in ALL_ROLES, "ROLE_INVALID", "reviewer role is invalid")
    return reviewer_id, role


def _closed_object(
    value: Any, *, allowed: set[str], required: set[str], label: str
) -> dict[str, Any]:
    require(isinstance(value, Mapping), "PROPOSAL_INVALID", f"{label} must be an object")
    result = dict(cast(Mapping[str, Any], value))
    require(set(result) <= allowed, "PROPOSAL_INVALID", f"{label} contains unknown fields")
    require(required <= set(result), "PROPOSAL_INVALID", f"{label} is missing required fields")
    return result


def _nonempty_text(value: Any, label: str, *, maximum: int = 20_000) -> str:
    require(
        isinstance(value, str) and bool(value.strip()), "PROPOSAL_INVALID", f"{label} is required"
    )
    require(len(value.encode("utf-8")) <= maximum, "PROPOSAL_INVALID", f"{label} is too large")
    return cast(str, value)


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    require(isinstance(value, list), "PROPOSAL_INVALID", f"{label} must be an array")
    result: list[str] = []
    for item in value:
        result.append(_nonempty_text(item, label, maximum=4_000))
    require(allow_empty or bool(result), "PROPOSAL_INVALID", f"{label} must not be empty")
    require(len(result) == len(set(result)), "PROPOSAL_INVALID", f"{label} must be unique")
    return result


def _validate_region(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, Mapping), "PROPOSAL_INVALID", f"{label} must be an object")
    shape = value.get("shape")
    if shape == "BOUNDING_BOX":
        region = _closed_object(
            value,
            allowed={"shape", "x_min", "y_min", "x_max", "y_max"},
            required={"shape", "x_min", "y_min", "x_max", "y_max"},
            label=label,
        )
        for key in ("x_min", "y_min", "x_max", "y_max"):
            require(
                type(region[key]) is int and region[key] >= 0,
                "PROPOSAL_INVALID",
                f"{label}.{key} is invalid",
            )
        require(
            region["x_min"] < region["x_max"] and region["y_min"] < region["y_max"],
            "PROPOSAL_INVALID",
            f"{label} has empty bounds",
        )
        return region
    require(shape == "POLYGON", "PROPOSAL_INVALID", f"{label} shape is invalid")
    region = _closed_object(
        value,
        allowed={"shape", "vertices"},
        required={"shape", "vertices"},
        label=label,
    )
    vertices = region["vertices"]
    require(
        isinstance(vertices, list) and len(vertices) >= 3,
        "PROPOSAL_INVALID",
        f"{label} polygon needs at least three vertices",
    )
    points: list[tuple[int, int]] = []
    for vertex in vertices:
        require(
            isinstance(vertex, list)
            and len(vertex) == 2
            and all(type(item) is int and item >= 0 for item in vertex),
            "PROPOSAL_INVALID",
            f"{label} polygon vertex is invalid",
        )
        points.append((vertex[0], vertex[1]))
    doubled_area = abs(
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
    )
    require(doubled_area > 0, "PROPOSAL_INVALID", f"{label} polygon is degenerate")
    return region


def _canonical_region_identity(region: Mapping[str, Any]) -> dict[str, Any]:
    if region["shape"] == "BOUNDING_BOX":
        return cast(dict[str, Any], json_copy(region))
    vertices = [
        (cast(list[int], item)[0], cast(list[int], item)[1])
        for item in cast(list[Any], region["vertices"])
    ]
    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices.pop()
    variants: list[tuple[tuple[int, int], ...]] = []
    for sequence in (vertices, list(reversed(vertices))):
        for index in range(len(sequence)):
            variants.append(tuple(sequence[index:] + sequence[:index]))
    canonical = min(variants)
    return {"shape": "POLYGON", "vertices": [list(item) for item in canonical]}


def _project_action_predicate(raw: Mapping[str, Any]) -> dict[str, Any]:
    predicate = {
        key: json_copy(value)
        for key, value in raw.items()
        if key not in {"evidence_ids", "rationale", "human_selected", "tolerance_px"}
    }
    if "normalized_action" in predicate:
        predicate["normalized_action_sha256"] = canonical_sha256(predicate.pop("normalized_action"))
    for key in ("allowed_directions", "allowed_values"):
        if key in predicate:
            predicate[key] = sorted(predicate[key])
    for key in ("regions", "start_regions", "end_regions"):
        if key in predicate:
            canonical_regions = [
                _canonical_region_identity(cast(Mapping[str, Any], region))
                for region in cast(list[Any], predicate[key])
            ]
            predicate[key] = sorted(canonical_regions, key=canonical_json_bytes)
    return predicate


def _validate_normalized_action(value: Any, action_type: str) -> dict[str, Any]:
    action = _closed_object(
        value,
        allowed={"class", "serializer", "serializer_version", "value"},
        required={"class", "serializer", "serializer_version", "value"},
        label="normalized action",
    )
    require(
        action["class"] == "mobile_world.runtime.utils.models.JSONAction"
        and action["serializer"] == "pydantic model_dump(mode=json, exclude_none=false)"
        and action["serializer_version"] == "2.11.7",
        "PROPOSAL_INVALID",
        "normalized action production binding differs",
    )
    action_value = _closed_object(
        action["value"],
        allowed=NORMALIZED_ACTION_VALUE_FIELDS,
        required=NORMALIZED_ACTION_VALUE_FIELDS,
        label="normalized action value",
    )
    require(
        action_value["action_type"] == action_type,
        "PROPOSAL_INVALID",
        "exact action type differs from predicate",
    )
    for key in ("x", "y", "start_x", "start_y", "end_x", "end_y", "index"):
        require(
            action_value[key] is None or type(action_value[key]) is int,
            "PROPOSAL_INVALID",
            f"normalized action {key} has the wrong type",
        )
    for key in ("text", "direction", "goal_status", "app_name", "keycode", "action_name"):
        require(
            action_value[key] is None or isinstance(action_value[key], str),
            "PROPOSAL_INVALID",
            f"normalized action {key} has the wrong type",
        )
    require(
        action_value["clear_text"] is None or type(action_value["clear_text"]) is bool,
        "PROPOSAL_INVALID",
        "normalized action clear_text has the wrong type",
    )
    require(
        action_value["action_json"] is None or isinstance(action_value["action_json"], dict),
        "PROPOSAL_INVALID",
        "normalized action action_json has the wrong type",
    )
    require(
        action_value["direction"] is None or action_value["direction"] in CARDINAL_DIRECTIONS,
        "PROPOSAL_INVALID",
        "normalized action direction is invalid",
    )
    try:
        from mobile_world.runtime.utils.models import JSONAction

        production_action = JSONAction(**action_value)
        production_value = production_action.model_dump(mode="json", exclude_none=False)
    except (TypeError, ValueError) as exc:
        raise CurationError(
            "PROPOSAL_INVALID",
            "normalized action fails the pinned production JSONAction validator",
        ) from exc
    require(
        production_value == action_value,
        "PROPOSAL_INVALID",
        "normalized action bytes differ after pinned production normalization",
    )
    action["value"] = action_value
    return action


def _validate_predicate(value: Any) -> dict[str, Any]:
    predicate = _closed_object(
        value,
        allowed={
            "predicate_kind",
            "action_type",
            "rationale",
            "evidence_ids",
            "human_selected",
            "normalized_action",
            "regions",
            "start_regions",
            "end_regions",
            "allowed_directions",
            "minimum_displacement_px",
            "tolerance_px",
            "field",
            "unicode_normalization",
            "case_sensitive",
            "allowed_values",
        },
        required={
            "predicate_kind",
            "action_type",
            "rationale",
            "evidence_ids",
            "human_selected",
        },
        label="predicate",
    )
    kind = predicate["predicate_kind"]
    action_type = predicate["action_type"]
    require(kind in PREDICATE_KINDS, "PROPOSAL_INVALID", "predicate kind is invalid")
    require(action_type in ACTION_TYPES, "PROPOSAL_INVALID", "action type is invalid")
    _nonempty_text(predicate["rationale"], "predicate rationale", maximum=4_000)
    evidence_ids = _string_list(predicate["evidence_ids"], "predicate evidence IDs")
    require(
        all(re.fullmatch(r"evidence-[0-9a-f]{24}", item) for item in evidence_ids),
        "PROPOSAL_INVALID",
        "predicate evidence ID is invalid",
    )
    require(
        predicate["human_selected"] is True,
        "PROPOSAL_INVALID",
        "predicate must be explicitly human-selected",
    )
    if kind == "EXACT_NORMALIZED_ACTION":
        predicate["normalized_action"] = _validate_normalized_action(
            predicate.get("normalized_action"), action_type
        )
    elif kind == "POINT_REGION":
        require(
            action_type in POINT_ACTION_TYPES,
            "PROPOSAL_INVALID",
            "point predicate has incompatible action type",
        )
        regions = predicate.get("regions")
        require(
            isinstance(regions, list) and bool(regions),
            "PROPOSAL_INVALID",
            "point predicate requires regions",
        )
        predicate["regions"] = [
            _validate_region(item, "point region") for item in cast(list[Any], regions)
        ]
        require(
            len(predicate["regions"])
            == len(
                {
                    canonical_sha256(_canonical_region_identity(region))
                    for region in cast(list[dict[str, Any]], predicate["regions"])
                }
            ),
            "PROPOSAL_INVALID",
            "point predicate contains a material-duplicate region",
        )
        require(
            type(predicate.get("tolerance_px")) is int and predicate["tolerance_px"] >= 0,
            "PROPOSAL_INVALID",
            "point tolerance is invalid",
        )
    elif kind == "DRAG_REGION":
        require(action_type == "drag", "PROPOSAL_INVALID", "drag predicate requires drag action")
        for key in ("start_regions", "end_regions"):
            regions = predicate.get(key)
            require(
                isinstance(regions, list) and bool(regions),
                "PROPOSAL_INVALID",
                f"drag predicate requires {key}",
            )
            predicate[key] = [_validate_region(item, key) for item in cast(list[Any], regions)]
            require(
                len(predicate[key])
                == len(
                    {
                        canonical_sha256(_canonical_region_identity(region))
                        for region in cast(list[dict[str, Any]], predicate[key])
                    }
                ),
                "PROPOSAL_INVALID",
                f"drag predicate {key} contains a material-duplicate region",
            )
        directions = _string_list(predicate.get("allowed_directions"), "drag directions")
        require(set(directions) <= set(DIRECTIONS), "PROPOSAL_INVALID", "drag direction is invalid")
        require(
            type(predicate.get("minimum_displacement_px")) is int
            and predicate["minimum_displacement_px"] >= 0,
            "PROPOSAL_INVALID",
            "drag displacement is invalid",
        )
        require(
            type(predicate.get("tolerance_px")) is int and predicate["tolerance_px"] >= 0,
            "PROPOSAL_INVALID",
            "drag tolerance is invalid",
        )
    elif kind == "TEXT_VARIANTS":
        require(
            action_type in TEXT_ACTION_TYPES,
            "PROPOSAL_INVALID",
            "text predicate has incompatible action type",
        )
        expected_field = {
            "input_text": "text",
            "answer": "text",
            "finished": "text",
            "ask_user": "text",
            "open_app": "app_name",
            "status": "goal_status",
        }[action_type]
        require(
            predicate.get("field") == expected_field,
            "PROPOSAL_INVALID",
            "text predicate field differs from the action type",
        )
        require(
            predicate.get("unicode_normalization") == "NFC",
            "PROPOSAL_INVALID",
            "unicode normalization is invalid",
        )
        require(
            type(predicate.get("case_sensitive")) is bool,
            "PROPOSAL_INVALID",
            "case_sensitive must be boolean",
        )
        predicate["allowed_values"] = _string_list(
            predicate.get("allowed_values"), "allowed text values"
        )
        require(
            all(
                unicodedata.is_normalized("NFC", item)
                for item in cast(list[str], predicate["allowed_values"])
            ),
            "PROPOSAL_INVALID",
            "allowed text values must already be exact NFC bytes",
        )
    elif kind == "DIRECTION_SET":
        require(
            action_type in DIRECTION_ACTION_TYPES,
            "PROPOSAL_INVALID",
            "direction predicate has incompatible action type",
        )
        directions = _string_list(predicate.get("allowed_directions"), "directions")
        require(
            set(directions) <= set(CARDINAL_DIRECTIONS),
            "PROPOSAL_INVALID",
            "direction is invalid",
        )
    return cast(dict[str, Any], json_copy(predicate))


def _validate_span(value: Any, label: str) -> dict[str, Any]:
    span = _closed_object(
        value,
        allowed={
            "record_id",
            "char_start",
            "char_end",
            "utf8_byte_start",
            "utf8_byte_end",
            "exact_text",
            "span_sha256",
            "human_selected",
        },
        required={
            "record_id",
            "char_start",
            "char_end",
            "utf8_byte_start",
            "utf8_byte_end",
            "exact_text",
            "span_sha256",
            "human_selected",
        },
        label=label,
    )
    _nonempty_text(span["record_id"], f"{label}.record_id", maximum=200)
    exact = _nonempty_text(span["exact_text"], f"{label}.exact_text", maximum=20_000)
    require(
        type(span["char_start"]) is int
        and type(span["char_end"]) is int
        and 0 <= span["char_start"] < span["char_end"],
        "PROPOSAL_INVALID",
        f"{label} offsets are invalid",
    )
    require(
        type(span["utf8_byte_start"]) is int
        and type(span["utf8_byte_end"]) is int
        and 0 <= span["utf8_byte_start"] < span["utf8_byte_end"],
        "PROPOSAL_INVALID",
        f"{label} UTF-8 offsets are invalid",
    )
    require(
        span["span_sha256"] == hashlib.sha256(exact.encode()).hexdigest(),
        "PROPOSAL_INVALID",
        f"{label} digest differs",
    )
    require(
        span["human_selected"] is True,
        "PROPOSAL_INVALID",
        f"{label} must be explicitly human-selected",
    )
    return span


def validate_action_payload(value: Any) -> dict[str, Any]:
    payload = _closed_object(
        value,
        allowed={
            "proposal_kind",
            "disposition",
            "exclusion_reason",
            "predicates",
            "evidence_rationale",
            "closed_world_confirmed",
            "all_reasonable_actions_enumerated",
        },
        required={
            "proposal_kind",
            "disposition",
            "exclusion_reason",
            "predicates",
            "evidence_rationale",
            "closed_world_confirmed",
            "all_reasonable_actions_enumerated",
        },
        label="action-gold proposal",
    )
    require(
        payload["proposal_kind"] == "ACTION_GOLD",
        "PROPOSAL_INVALID",
        "action proposal kind is invalid",
    )
    require(payload["disposition"] in DISPOSITIONS, "PROPOSAL_INVALID", "disposition is invalid")
    if payload["disposition"] == "EXCLUDE":
        require(
            payload["exclusion_reason"] == "NO_GOLD_CONSENSUS",
            "PROPOSAL_INVALID",
            "exclusion reason is invalid",
        )
        require(
            payload["predicates"] == [],
            "PROPOSAL_INVALID",
            "excluded action proposal cannot contain predicates",
        )
    else:
        require(
            payload["exclusion_reason"] is None,
            "PROPOSAL_INVALID",
            "accepted action proposal cannot have an exclusion reason",
        )
        require(
            isinstance(payload["predicates"], list) and bool(payload["predicates"]),
            "PROPOSAL_INVALID",
            "accepted action proposal requires predicates",
        )
        payload["predicates"] = [_validate_predicate(item) for item in payload["predicates"]]
        material_identities = [
            canonical_sha256(_project_action_predicate(predicate))
            for predicate in cast(list[dict[str, Any]], payload["predicates"])
        ]
        require(
            len(material_identities) == len(set(material_identities)),
            "PROPOSAL_INVALID",
            "accepted predicate set contains a material duplicate",
        )
    _nonempty_text(payload["evidence_rationale"], "action evidence rationale")
    for key in ("closed_world_confirmed", "all_reasonable_actions_enumerated"):
        require(type(payload[key]) is bool, "PROPOSAL_INVALID", f"{key} must be boolean")
    if payload["disposition"] == "ACCEPT":
        require(
            payload["closed_world_confirmed"] and payload["all_reasonable_actions_enumerated"],
            "PROPOSAL_INVALID",
            "accepted gold must confirm closed-world coverage",
        )
    return cast(dict[str, Any], json_copy(payload))


def validate_transformation_payload(value: Any, *, clean_control: bool) -> dict[str, Any]:
    payload = _closed_object(
        value,
        allowed={
            "proposal_kind",
            "unit_kind",
            "history_family",
            "disposition",
            "exclusion_reason",
            "focal_target_spans",
            "oracle_target_spans",
            "correction_candidates",
            "correction_text",
            "correction_evidence_ids",
            "correction_is_minimal_fact",
            "correction_contains_no_advice",
            "oracle_preserves_non_target_history",
            "protected_spans",
            "delimiter_repairs",
            "sham_span",
            "sham_match_checks",
            "clean_control_reference_anchor_confirmed",
            "preview_receipt_sha256",
            "preview_human_confirmed",
            "rationale",
        },
        required={
            "proposal_kind",
            "unit_kind",
            "history_family",
            "disposition",
            "exclusion_reason",
            "focal_target_spans",
            "oracle_target_spans",
            "correction_candidates",
            "correction_text",
            "correction_evidence_ids",
            "correction_is_minimal_fact",
            "correction_contains_no_advice",
            "oracle_preserves_non_target_history",
            "protected_spans",
            "delimiter_repairs",
            "sham_span",
            "sham_match_checks",
            "clean_control_reference_anchor_confirmed",
            "preview_receipt_sha256",
            "preview_human_confirmed",
            "rationale",
        },
        label="transformation proposal",
    )
    require(
        payload["proposal_kind"] == "TRANSFORMATION",
        "PROPOSAL_INVALID",
        "transformation proposal kind is invalid",
    )
    require(
        payload["unit_kind"] == ("CLEAN_CONTROL" if clean_control else "STRICT_MHR"),
        "PROPOSAL_INVALID",
        "transformation unit kind differs from the assigned unit",
    )
    require(
        payload["history_family"] in {"flat_progress", "raw_replay"},
        "PROPOSAL_INVALID",
        "transformation history family is invalid",
    )
    require(payload["disposition"] in DISPOSITIONS, "PROPOSAL_INVALID", "disposition is invalid")
    if payload["disposition"] == "EXCLUDE":
        require(
            payload["exclusion_reason"]
            in {
                "TARGET_SPAN_UNRESOLVED",
                "NO_VALID_CORRECTION",
                "NO_VALID_ORACLE_VIEW",
                "NO_MATCHED_SHAM",
            },
            "PROPOSAL_INVALID",
            "exclusion reason is invalid",
        )
    else:
        require(
            payload["exclusion_reason"] is None,
            "PROPOSAL_INVALID",
            "accepted transformation cannot have exclusion reason",
        )
    for key in ("focal_target_spans", "oracle_target_spans", "protected_spans"):
        require(isinstance(payload[key], list), "PROPOSAL_INVALID", f"{key} must be an array")
        payload[key] = [_validate_span(item, key) for item in payload[key]]
    if payload["disposition"] == "EXCLUDE":
        require(
            payload["focal_target_spans"] == []
            and payload["oracle_target_spans"] == []
            and payload["protected_spans"] == []
            and payload["correction_candidates"] == []
            and payload["correction_text"] == ""
            and payload["correction_evidence_ids"] == []
            and payload["delimiter_repairs"] == []
            and payload["sham_span"] is None
            and payload["sham_match_checks"] is None,
            "PROPOSAL_INVALID",
            "excluded transformation must not retain a partial plan",
        )
        require(
            payload["correction_is_minimal_fact"] is False
            and payload["correction_contains_no_advice"] is False
            and payload["oracle_preserves_non_target_history"] is False
            and payload["clean_control_reference_anchor_confirmed"] is False
            and payload["preview_receipt_sha256"] is None
            and payload["preview_human_confirmed"] is False,
            "PROPOSAL_INVALID",
            "excluded transformation must keep all acceptance guards false",
        )
        _nonempty_text(payload["rationale"], "transformation rationale")
        return cast(dict[str, Any], json_copy(payload))

    require(
        bool(payload["focal_target_spans"]),
        "PROPOSAL_INVALID",
        "accepted transformation requires a focal/reference span",
    )
    candidates = payload["correction_candidates"]
    require(
        isinstance(candidates, list),
        "PROPOSAL_INVALID",
        "correction candidates must be an array",
    )
    candidate_texts: list[str] = []
    for raw_candidate in candidates:
        candidate = _closed_object(
            raw_candidate,
            allowed={"text", "rationale", "human_authored"},
            required={"text", "rationale", "human_authored"},
            label="correction candidate",
        )
        text = _nonempty_text(candidate["text"], "correction candidate text")
        _nonempty_text(candidate["rationale"], "correction candidate rationale", maximum=4_000)
        require(
            candidate["human_authored"] is True,
            "PROPOSAL_INVALID",
            "correction candidates must be explicitly human-authored",
        )
        candidate_texts.append(text)
    require(
        len(candidate_texts) == len(set(candidate_texts)),
        "PROPOSAL_INVALID",
        "correction candidate texts must be unique",
    )
    require(
        isinstance(payload["preview_receipt_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", payload["preview_receipt_sha256"]) is not None
        and payload["preview_human_confirmed"] is True,
        "PROPOSAL_INVALID",
        "accepted transformation requires one confirmed CPU preview receipt",
    )
    evidence_ids = _string_list(
        payload["correction_evidence_ids"],
        "correction evidence IDs",
        allow_empty=clean_control,
    )
    require(
        all(re.fullmatch(r"evidence-[0-9a-f]{24}", item) for item in evidence_ids),
        "PROPOSAL_INVALID",
        "correction evidence ID is invalid",
    )
    for key in (
        "correction_is_minimal_fact",
        "correction_contains_no_advice",
        "oracle_preserves_non_target_history",
    ):
        require(type(payload[key]) is bool, "PROPOSAL_INVALID", f"{key} must be boolean")
    if not clean_control:
        require(
            bool(payload["oracle_target_spans"]),
            "PROPOSAL_INVALID",
            "strict case needs oracle spans",
        )
        _nonempty_text(payload["correction_text"], "correction text")
        require(
            bool(candidate_texts) and payload["correction_text"] in candidate_texts,
            "PROPOSAL_INVALID",
            "selected correction must be one human-authored candidate",
        )
        require(
            payload["correction_is_minimal_fact"]
            and payload["correction_contains_no_advice"]
            and payload["oracle_preserves_non_target_history"],
            "PROPOSAL_INVALID",
            "accepted transformation must satisfy correction/oracle guards",
        )
        require(
            payload["clean_control_reference_anchor_confirmed"] is False,
            "PROPOSAL_INVALID",
            "strict case cannot claim a clean-control anchor",
        )
    else:
        require(
            len(payload["focal_target_spans"]) == 1
            and payload["oracle_target_spans"] == []
            and payload["correction_candidates"] == []
            and payload["correction_text"] == ""
            and payload["correction_evidence_ids"] == []
            and payload["correction_is_minimal_fact"] is False
            and payload["correction_contains_no_advice"] is False
            and payload["oracle_preserves_non_target_history"] is False
            and payload["clean_control_reference_anchor_confirmed"] is True,
            "PROPOSAL_INVALID",
            "accepted clean control requires one confirmed benign reference anchor only",
        )
    if payload["history_family"] == "raw_replay":
        require(
            bool(payload["protected_spans"]),
            "PROPOSAL_INVALID",
            "raw-replay transformation requires protected tool-call spans",
        )
    repairs = payload["delimiter_repairs"]
    require(isinstance(repairs, list), "PROPOSAL_INVALID", "delimiter repairs must be an array")
    for repair in repairs:
        item = _closed_object(
            repair,
            allowed={"arm", "operation", "deleted_syntax_span", "rationale", "human_selected"},
            required={"arm", "operation", "deleted_syntax_span", "rationale", "human_selected"},
            label="delimiter repair",
        )
        require(
            item["arm"] in {"MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"},
            "PROPOSAL_INVALID",
            "delimiter repair arm is invalid",
        )
        require(
            item["operation"] in {"DELETE_EMPTY_DELIMITER", "DELETE_ORPHAN_SEPARATOR"},
            "PROPOSAL_INVALID",
            "delimiter repair operation is invalid",
        )
        _validate_span(item["deleted_syntax_span"], "delimiter repair span")
        _nonempty_text(item["rationale"], "delimiter repair rationale", maximum=4_000)
        require(
            item["human_selected"] is True,
            "PROPOSAL_INVALID",
            "delimiter repair must be explicitly human-selected",
        )
    require(
        payload["sham_span"] is not None,
        "PROPOSAL_INVALID",
        "every unit needs a matched benign sham span",
    )
    payload["sham_span"] = _validate_span(payload["sham_span"], "sham span")
    checks = _closed_object(
        payload["sham_match_checks"],
        allowed={
            "same_role",
            "same_content_kind",
            "same_representation_class",
            "relative_third_matched",
            "same_record_preferred_or_depth_within_one",
            "token_size_matched",
            "no_entailment",
            "no_contradiction",
            "no_lexical_alias",
            "not_hard_task_requirement",
            "not_action_discriminant",
        },
        required={
            "same_role",
            "same_content_kind",
            "same_representation_class",
            "relative_third_matched",
            "same_record_preferred_or_depth_within_one",
            "token_size_matched",
            "no_entailment",
            "no_contradiction",
            "no_lexical_alias",
            "not_hard_task_requirement",
            "not_action_discriminant",
        },
        label="sham match checks",
    )
    require(
        all(type(v) is bool for v in checks.values()),
        "PROPOSAL_INVALID",
        "sham checks must be boolean",
    )
    require(all(checks.values()), "PROPOSAL_INVALID", "accepted sham must pass every match check")
    _nonempty_text(payload["rationale"], "transformation rationale")
    return cast(dict[str, Any], json_copy(payload))


def validate_transformation_preview_inputs(value: Any, *, clean_control: bool) -> dict[str, Any]:
    """Validate only human selections needed by the mechanical CPU preview.

    This intentionally does not invent or pre-satisfy semantic reviewer attestations.
    Those remain required on the separately validated review proposal.
    """

    payload = _closed_object(
        value,
        allowed={
            "focal_target_spans",
            "oracle_target_spans",
            "correction_candidates",
            "correction_evidence_ids",
            "protected_spans",
            "delimiter_repairs",
            "sham_span",
        },
        required={
            "focal_target_spans",
            "oracle_target_spans",
            "correction_candidates",
            "correction_evidence_ids",
            "protected_spans",
            "delimiter_repairs",
            "sham_span",
        },
        label="transformation preview inputs",
    )
    for key in ("focal_target_spans", "oracle_target_spans", "protected_spans"):
        require(isinstance(payload[key], list), "PREVIEW_INPUT_INVALID", f"{key} must be an array")
        payload[key] = [_validate_span(item, key) for item in payload[key]]
    require(
        bool(payload["focal_target_spans"]),
        "PREVIEW_INPUT_INVALID",
        "preview requires a focal/reference span",
    )
    candidates = payload["correction_candidates"]
    require(
        isinstance(candidates, list),
        "PREVIEW_INPUT_INVALID",
        "correction candidates must be an array",
    )
    candidate_texts: list[str] = []
    for raw_candidate in candidates:
        candidate = _closed_object(
            raw_candidate,
            allowed={"text", "rationale", "human_authored"},
            required={"text", "rationale", "human_authored"},
            label="correction candidate",
        )
        candidate_texts.append(_nonempty_text(candidate["text"], "correction candidate text"))
        _nonempty_text(candidate["rationale"], "correction candidate rationale", maximum=4_000)
        require(
            candidate["human_authored"] is True,
            "PREVIEW_INPUT_INVALID",
            "preview candidates must be explicitly human-authored",
        )
    require(
        len(candidate_texts) == len(set(candidate_texts)),
        "PREVIEW_INPUT_INVALID",
        "preview candidate texts must be unique",
    )
    evidence_ids = _string_list(
        payload["correction_evidence_ids"],
        "correction evidence IDs",
        allow_empty=clean_control,
    )
    require(
        all(re.fullmatch(r"evidence-[0-9a-f]{24}", item) for item in evidence_ids),
        "PREVIEW_INPUT_INVALID",
        "preview correction evidence ID is invalid",
    )
    if clean_control:
        require(
            len(payload["focal_target_spans"]) == 1
            and payload["oracle_target_spans"] == []
            and candidates == []
            and evidence_ids == [],
            "PREVIEW_INPUT_INVALID",
            "clean preview requires one reference anchor and no correction/oracle fields",
        )
    else:
        require(
            bool(payload["oracle_target_spans"]) and bool(candidate_texts) and bool(evidence_ids),
            "PREVIEW_INPUT_INVALID",
            "strict preview requires oracle spans, correction candidates, and evidence",
        )
    repairs = payload["delimiter_repairs"]
    require(
        isinstance(repairs, list), "PREVIEW_INPUT_INVALID", "delimiter repairs must be an array"
    )
    for raw_repair in repairs:
        repair = _closed_object(
            raw_repair,
            allowed={"arm", "operation", "deleted_syntax_span", "rationale", "human_selected"},
            required={"arm", "operation", "deleted_syntax_span", "rationale", "human_selected"},
            label="delimiter repair",
        )
        allowed_arms = (
            {"SHAM_BENIGN_EDIT"}
            if clean_control
            else {"MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"}
        )
        require(
            repair["arm"] in allowed_arms
            and repair["operation"] in {"DELETE_EMPTY_DELIMITER", "DELETE_ORPHAN_SEPARATOR"},
            "PREVIEW_INPUT_INVALID",
            "preview delimiter repair arm or operation is invalid",
        )
        _validate_span(repair["deleted_syntax_span"], "delimiter repair span")
        _nonempty_text(repair["rationale"], "delimiter repair rationale", maximum=4_000)
        require(
            repair["human_selected"] is True,
            "PREVIEW_INPUT_INVALID",
            "preview delimiter repair must be explicitly human-selected",
        )
    require(
        payload["sham_span"] is not None,
        "PREVIEW_INPUT_INVALID",
        "preview requires one human-selected sham span",
    )
    payload["sham_span"] = _validate_span(payload["sham_span"], "sham span")
    return cast(dict[str, Any], json_copy(payload))


def validate_consistency_payload(value: Any) -> dict[str, Any]:
    payload = _closed_object(
        value,
        allowed={
            "proposal_kind",
            "consistency_label",
            "history_consistency_rationale",
            "gui_task_consistency_rationale",
            "replay_response_used",
            "descriptive_only",
        },
        required={
            "proposal_kind",
            "consistency_label",
            "history_consistency_rationale",
            "gui_task_consistency_rationale",
            "replay_response_used",
            "descriptive_only",
        },
        label="consistency proposal",
    )
    require(
        payload["proposal_kind"] == "CONSISTENCY_AUDIT",
        "PROPOSAL_INVALID",
        "consistency proposal kind is invalid",
    )
    require(
        payload["consistency_label"] in CONSISTENCY_LABELS,
        "PROPOSAL_INVALID",
        "consistency label is invalid",
    )
    _nonempty_text(payload["history_consistency_rationale"], "history consistency rationale")
    _nonempty_text(payload["gui_task_consistency_rationale"], "GUI/task consistency rationale")
    require(
        payload["replay_response_used"] is False,
        "PROPOSAL_INVALID",
        "replay responses are forbidden",
    )
    require(
        payload["descriptive_only"] is True,
        "PROPOSAL_INVALID",
        "consistency audit must remain descriptive only",
    )
    return cast(dict[str, Any], json_copy(payload))


def validate_review_payload(channel: str, value: Any, *, clean_control: bool) -> dict[str, Any]:
    require(channel in CHANNELS, "CHANNEL_INVALID", "curation channel is invalid")
    if channel == "ACTION_GOLD":
        return validate_action_payload(value)
    if channel == "TRANSFORMATION":
        return validate_transformation_payload(value, clean_control=clean_control)
    return validate_consistency_payload(value)


def material_projection(
    channel: str,
    payload: Mapping[str, Any],
    *,
    record_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> Any:
    if channel == "ACTION_GOLD":
        predicates: list[dict[str, Any]] = []
        tolerances: list[dict[str, Any]] = []
        for raw in cast(list[dict[str, Any]], payload.get("predicates", [])):
            predicate = _project_action_predicate(raw)
            if "tolerance_px" in raw:
                tolerances.append(
                    {
                        "predicate_identity_sha256": canonical_sha256(predicate),
                        "tolerance_px": raw["tolerance_px"],
                    }
                )
            predicates.append(predicate)
        tolerances.sort(key=canonical_json_bytes)
        return {
            "disposition": payload.get("disposition"),
            "exclusion_reason": payload.get("exclusion_reason"),
            "predicates": sorted(predicates, key=canonical_json_bytes),
            "tolerances_px": tolerances,
        }
    if channel == "TRANSFORMATION":

        def semantic_span(value: Any) -> Any:
            if not isinstance(value, Mapping):
                return value
            record_id = value["record_id"]
            binding = record_bindings.get(record_id) if record_bindings is not None else None
            if binding is None:
                record_identity_sha256 = canonical_sha256(["workspace-record", record_id])
                request_path = f"workspace-record:{record_id}"
            else:
                record_identity_sha256 = binding["record_identity_sha256"]
                request_path = binding["request_path"]
            return {
                "record_identity_sha256": record_identity_sha256,
                "request_path": request_path,
                "char_start": value["char_start"],
                "char_end": value["char_end"],
                "utf8_byte_start": value["utf8_byte_start"],
                "utf8_byte_end": value["utf8_byte_end"],
                "span_sha256": value["span_sha256"],
            }

        repairs = [
            {
                "arm": item["arm"],
                "operation": item["operation"],
                "deleted_syntax_span": semantic_span(item["deleted_syntax_span"]),
            }
            for item in cast(list[dict[str, Any]], payload.get("delimiter_repairs", []))
        ]
        return {
            "disposition": payload.get("disposition"),
            "exclusion_reason": payload.get("exclusion_reason"),
            "focal_target_spans": [
                semantic_span(item)
                for item in cast(list[Any], payload.get("focal_target_spans", []))
            ],
            "oracle_target_spans": [
                semantic_span(item)
                for item in cast(list[Any], payload.get("oracle_target_spans", []))
            ],
            "correction_text": payload.get("correction_text"),
            "delimiter_repairs": sorted(repairs, key=canonical_json_bytes),
            "sham_span": semantic_span(payload.get("sham_span")),
            "sham_match_checks": payload.get("sham_match_checks"),
        }
    return {"consistency_label": payload.get("consistency_label")}


def disagreement_fields(
    channel: str,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    record_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    primary_projection = material_projection(channel, primary, record_bindings=record_bindings)
    secondary_projection = material_projection(channel, secondary, record_bindings=record_bindings)
    if canonical_sha256(primary_projection) == canonical_sha256(secondary_projection):
        return []
    fields: list[str] = []
    if primary.get("disposition") != secondary.get("disposition") or primary.get(
        "exclusion_reason"
    ) != secondary.get("exclusion_reason"):
        fields.append("DISPOSITION")
    if channel == "ACTION_GOLD":
        if canonical_sha256(primary_projection["predicates"]) != canonical_sha256(
            secondary_projection["predicates"]
        ):
            fields.append("ACCEPTED_ACTION_PREDICATES")
        if canonical_sha256(primary_projection["tolerances_px"]) != canonical_sha256(
            secondary_projection["tolerances_px"]
        ):
            fields.append("ACTION_TOLERANCE")
    elif channel == "TRANSFORMATION":
        pairs = (
            ("focal_target_spans", "FOCAL_TARGET_SET"),
            ("oracle_target_spans", "ORACLE_TARGET_SET"),
            ("correction_text", "CORRECTION_BYTES"),
            ("sham_span", "SHAM_SPAN"),
            ("sham_match_checks", "SHAM_MATCH"),
            ("delimiter_repairs", "DELIMITER_REPAIR"),
        )
        fields.extend(
            label
            for key, label in pairs
            if canonical_sha256(primary_projection.get(key))
            != canonical_sha256(secondary_projection.get(key))
        )
    elif primary.get("consistency_label") != secondary.get("consistency_label"):
        fields.append("CONSISTENCY_LABEL")
    return list(dict.fromkeys(fields))


def option_catalog() -> dict[str, Any]:
    return {
        "protocol_version": WORKSPACE_PROTOCOL_VERSION,
        "channels": list(CHANNELS),
        "roles": list(ALL_ROLES),
        "dispositions": list(DISPOSITIONS),
        "exclusion_reasons": list(EXCLUSION_REASONS),
        "predicate_kinds": list(PREDICATE_KINDS),
        "action_types": list(ACTION_TYPES),
        "point_action_types": list(POINT_ACTION_TYPES),
        "text_action_types": list(TEXT_ACTION_TYPES),
        "direction_action_types": list(DIRECTION_ACTION_TYPES),
        "directions": list(DIRECTIONS),
        "coordinate_tolerance_modes": list(COORDINATE_TOLERANCE_MODES),
        "consistency_labels": list(CONSISTENCY_LABELS),
        "safety": {
            "local_loopback_only": True,
            "external_network_allowed": False,
            "provider_invocation_allowed": False,
            "gpu_allowed": False,
            "model_loading_allowed": False,
            "formal_replay_allowed": False,
            "gui_action_execution_allowed": False,
            "treatment_response_generation_allowed": False,
        },
    }
