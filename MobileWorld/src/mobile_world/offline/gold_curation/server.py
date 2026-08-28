"""Loopback-only FastAPI surface for the G1.6 manual annotation workspace."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from mobile_world.offline.gold_curation.ai_assistance import (
    AICandidateWorkspace,
    validate_ai_schema_record,
)
from mobile_world.offline.gold_curation.contracts import (
    ADJUDICATOR_ROLE,
    CurationError,
    canonical_sha256,
    json_copy,
    option_catalog,
    require,
    role_channel,
)
from mobile_world.offline.gold_curation.publication import CurationPublication
from mobile_world.offline.gold_curation.schema_validation import validate_schema_record
from mobile_world.offline.gold_curation.store import AnnotationStore

STATIC_ROOT = Path(__file__).with_name("web")
ALLOWED_HOSTS = {"127.0.0.1", "::1"}
MAX_HTTP_REQUEST_BYTES = 2 * 1024 * 1024


def _same_request_origin(request: Request, origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == request.url.scheme
        and parsed.hostname == request.url.hostname
        and origin_port == request_port
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _loopback_peer(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _opaque_token(assignment_id: str, kind: str, value: str) -> str:
    return f"{kind}-" + hashlib.sha256(f"{assignment_id}|{kind}|{value}".encode()).hexdigest()[:24]


def _preview_inserted_text(value: Any) -> str:
    require(
        isinstance(value, dict) and set(value) == {"type", "text"},
        "PREVIEW_VISIBILITY_VIOLATION",
        "preview insertion is not a closed visible text block",
    )
    require(
        value["type"] == "text" and isinstance(value["text"], str),
        "PREVIEW_VISIBILITY_VIOLATION",
        "preview insertion is not a visible text block",
    )
    return cast(str, value["text"])


def _browser_preview_human_diff(arm: dict[str, Any]) -> str:
    """Render a human diff from the already-pseudonymized browser projection."""

    lines = [f"arm={arm['arm']}"]
    if not arm["diffs"] and not arm["list_insertions"]:
        lines.append("changes=NONE")
        return "\n".join(lines) + "\n"
    for index, diff in enumerate(arm["diffs"], start=1):
        lines.extend(
            (
                f"change={index}",
                f"container={diff['container_token']}",
                f"source_chars={diff['source_char_start']}:{diff['source_char_end']}",
                f"kind={diff['mapping_kind']}",
                "before=" + json.dumps(diff["original_text"], ensure_ascii=False),
                "after=" + json.dumps(diff["rendered_text"], ensure_ascii=False),
            )
        )
    for index, insertion in enumerate(arm["list_insertions"], start=1):
        lines.extend(
            (
                f"insertion={index}",
                f"container={insertion['container_token']}",
                f"source_index={insertion['source_index']}",
                f"rendered_index={insertion['rendered_index']}",
                "inserted_text=" + json.dumps(insertion["inserted_text"], ensure_ascii=False),
            )
        )
    return "\n".join(lines) + "\n"


def _browser_transformation_preview(
    preview: dict[str, Any], *, assignment_id: str
) -> dict[str, Any]:
    """Project a G1.5 preview without stable identities, paths, or request hashes."""

    require(
        isinstance(preview.get("preview_receipt_sha256"), str)
        and len(preview["preview_receipt_sha256"]) == 64,
        "PREVIEW_VISIBILITY_VIOLATION",
        "preview lacks its confidential source-bound receipt",
    )

    scoped_tokens: dict[str, dict[str, str]] = {}
    token_owners: dict[str, dict[str, str]] = {}

    def scoped_token(kind: str, value: str) -> str:
        by_value = scoped_tokens.setdefault(kind, {})
        if value in by_value:
            return by_value[value]
        token = _opaque_token(assignment_id, kind, value)
        owners = token_owners.setdefault(kind, {})
        require(
            token not in owners or owners[token] == value,
            "PREVIEW_VISIBILITY_VIOLATION",
            "two internal preview identities collide in one browser token namespace",
        )
        owners[token] = value
        by_value[value] = token
        return token

    def binding_token(value: Any) -> str:
        require(
            isinstance(value, str) and bool(value),
            "PREVIEW_VISIBILITY_VIOLATION",
            "preview contains an invalid internal binding",
        )
        return scoped_token("binding", value)

    def record_token(value: Any) -> str:
        require(
            isinstance(value, str) and bool(value),
            "PREVIEW_VISIBILITY_VIOLATION",
            "preview contains an invalid internal record identity",
        )
        return scoped_token("record", value)

    path_tokens: dict[str, str] = {}

    def path_token(value: Any) -> str:
        require(
            isinstance(value, list)
            and bool(value)
            and all(
                (isinstance(item, str) and bool(item)) or type(item) is int and item >= 0
                for item in value
            ),
            "PREVIEW_VISIBILITY_VIOLATION",
            "preview contains an invalid internal request path",
        )
        identity = canonical_sha256(value)
        if identity not in path_tokens:
            path_tokens[identity] = scoped_token("container", f"ordinal:{len(path_tokens)}")
        return path_tokens[identity]

    ranking = preview["correction_ranking"]
    projected_ranking = None
    if ranking is not None:
        projected_ranking = {
            "special_tokens_enabled": ranking["special_tokens_enabled"],
            "tie_break_order": json_copy(ranking["tie_break_order"]),
            "candidates": [
                {
                    "text": item["text"],
                    "token_count": item["token_count"],
                    "utf8_byte_count": item["utf8_byte_count"],
                    "codepoint_count": item["codepoint_count"],
                    "rank": item["rank"],
                }
                for item in ranking["candidates"]
            ],
        }

    projected_anchors = []
    for item in preview["correction_anchors"]:
        anchor = item["anchor"]
        projected_anchors.append(
            {
                "binding_token": binding_token(item["binding_id"]),
                "target_record_token": record_token(item["target_record_id"]),
                "anchor": {
                    "container_token": path_token(anchor["container_path"]),
                    "insert_index": anchor["insert_index"],
                    "expected_role": anchor["expected_role"],
                    "placement": anchor["placement"],
                    "context_kind": anchor["context_kind"],
                    "visible_prefix": anchor["visible_prefix"],
                    "visible_suffix": anchor["visible_suffix"],
                },
            }
        )

    sham = preview["sham_token_match"]
    projected_sham = {
        "special_tokens_enabled": sham["special_tokens_enabled"],
        "focal_token_count": sham["focal_token_count"],
        "sham_token_count": sham["sham_token_count"],
        "match_formula": sham["match_formula"],
        "matched": sham["matched"],
    }

    projected_repairs = [
        {
            "repair_token": scoped_token("repair", item["repair_id"]),
            "arm": item["arm"],
            "operation": item["operation"],
        }
        for item in preview["delimiter_repairs"]
    ]

    projected_arms: list[dict[str, Any]] = []
    for arm in preview["arms"]:
        diffs = [
            {
                "operation_token": scoped_token("operation", item["operation_id"]),
                "container_token": path_token(item["container_path"]),
                "source_char_start": item["source_char_start"],
                "source_char_end": item["source_char_end"],
                "original_text": item["original_text"],
                "rendered_text": item["rendered_text"],
                "mapping_kind": item["mapping_kind"],
            }
            for item in arm["diffs"]
        ]
        insertions = [
            {
                "operation_token": scoped_token("operation", item["operation_id"]),
                "container_token": path_token(item["container_path"]),
                "source_index": item["source_index"],
                "rendered_index": item["rendered_index"],
                "inserted_text": _preview_inserted_text(item["inserted_value"]),
            }
            for item in arm["list_insertions"]
        ]
        mappings = [
            {
                "container_token": path_token(item["container_path"]),
                "source_char_start": item["source_char_start"],
                "source_char_end": item["source_char_end"],
                "rendered_char_start": item["rendered_char_start"],
                "rendered_char_end": item["rendered_char_end"],
                "kind": item["kind"],
                "operation_token": (
                    scoped_token("operation", item["operation_id"])
                    if item["operation_id"] is not None
                    else None
                ),
            }
            for item in arm["source_mappings"]
        ]
        projected_arm = {
            "arm": arm["arm"],
            "rendered_history": [
                {
                    "container_token": path_token(item["container_path"]),
                    "record_tokens": [record_token(value) for value in item["record_ids"]],
                    "source_text": item["source_text"],
                    "rendered_text": item["rendered_text"],
                }
                for item in arm["rendered_history"]
            ],
            "diffs": diffs,
            "list_insertions": insertions,
            "source_mappings": mappings,
            "target_only_diff": arm["target_only_diff"],
            "source_mapping_reversible": arm["source_mapping_reversible"],
            "provider_invocation_allowed": arm["provider_invocation_allowed"],
        }
        projected_arm["human_diff"] = _browser_preview_human_diff(projected_arm)
        projected_arms.append(projected_arm)

    result = {
        "schema_version": "mobileworld.g1.gold-curation-browser-preview/v1",
        "record_type": "gold_curation_browser_preview",
        "preview_scope": preview["preview_scope"],
        "plan_set_profile": preview["plan_set_profile"],
        "preview_receipt_sha256": preview["preview_receipt_sha256"],
        "correction_ranking": projected_ranking,
        "correction_anchors": projected_anchors,
        "sham_token_match": projected_sham,
        "delimiter_repairs": projected_repairs,
        "arms": projected_arms,
        "acceptance_ready": bool(sham["matched"])
        and all(
            item["target_only_diff"] is True
            and item["source_mapping_reversible"] is True
            and item["provider_invocation_allowed"] is False
            for item in projected_arms
        )
        and preview["provider_invocation_allowed"] is False
        and preview["provider_invocation_count"] == 0
        and preview["treatment_response_generation_allowed"] is False
        and preview["treatment_response_count"] == 0
        and preview["network_used"] is False
        and preview["gpu_used"] is False
        and preview["replay_executed"] is False
        and preview["gui_action_executed"] is False,
        "provider_invocation_allowed": preview["provider_invocation_allowed"],
        "provider_invocation_count": preview["provider_invocation_count"],
        "treatment_response_generation_allowed": preview["treatment_response_generation_allowed"],
        "treatment_response_count": preview["treatment_response_count"],
        "network_used": preview["network_used"],
        "gpu_used": preview["gpu_used"],
        "replay_executed": preview["replay_executed"],
        "gui_action_executed": preview["gui_action_executed"],
    }
    validate_schema_record("browser_transformation_preview.schema.json", result)
    return result


async def _closed_json_request(
    request: Request, *, required: set[str], label: str
) -> dict[str, Any]:
    try:
        value = await request.json()
    except (UnicodeError, ValueError) as exc:
        raise CurationError("REQUEST_INVALID", f"{label} is not valid JSON") from exc
    require(isinstance(value, dict), "REQUEST_INVALID", f"{label} must be an object")
    result = cast(dict[str, Any], value)
    require(set(result) == required, "REQUEST_INVALID", f"{label} shape is not closed")
    return result


def _span_hint(source: dict[str, Any], record_text: str) -> dict[str, Any]:
    start = source["char_start"]
    end = source["char_end"]
    exact = source["exact_text"]
    require(
        type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(record_text)
        and record_text[start:end] == exact,
        "PACKET_EVIDENCE_INCOMPLETE",
        "target candidate span differs from its source record",
    )
    return {
        "char_start": start,
        "char_end": end,
        "utf8_byte_start": len(record_text[:start].encode("utf-8")),
        "utf8_byte_end": len(record_text[:end].encode("utf-8")),
        "exact_text": exact,
        "span_sha256": hashlib.sha256(exact.encode("utf-8")).hexdigest(),
    }


def _browser_packet(
    packet: dict[str, Any],
    *,
    assignment_id: str,
    role: str,
    reviewer_identity_sha256: str,
    source_binding: dict[str, Any],
    compared_review_event_ids: list[str],
) -> dict[str, Any]:
    """Remove stable source identities before a packet crosses into the browser."""

    unit = packet["unit"]
    result: dict[str, Any] = {
        "schema_version": "mobileworld.g1.gold-curation-review-packet/v1",
        "record_type": "gold_curation_review_packet",
        "channel": packet["channel"],
        "source_packet_id": source_binding["source_packet_id"],
        "assignment_id": assignment_id,
        "review_role": role,
        "reviewer_identity_sha256": reviewer_identity_sha256,
        "case_profile": {
            "case_type": "MISLEADING_HISTORY"
            if unit["unit_kind"] == "STRICT_MHR"
            else "CLEAN_CONTROL",
            "history_profile": "FLAT_PROGRESS"
            if unit["history_family"] == "flat_progress"
            else "RAW_REPLAY",
            "target_step": unit["target_step"],
        },
        "task": {"instruction": packet["task"]["instruction"]},
        "evidence": [
            {
                "evidence_token": _opaque_token(assignment_id, "evidence", item["evidence_id"]),
                "evidence_role": item["evidence_role"],
                "content": item["content"],
                "model_visible_at_or_before_request": True,
            }
            for item in packet["evidence"]
        ],
        "current_screenshot": {
            "available": packet["current_screenshot"]["available"],
            "width": packet["current_screenshot"]["width"],
            "height": packet["current_screenshot"]["height"],
            "image_token": assignment_id,
        },
        "visibility": {
            "history_visible": packet["visibility"]["history_visible"],
            "accepted_action_visible": False,
            "peer_reviews_visible": role == ADJUDICATOR_ROLE,
            "natural_target_output_visible": packet["visibility"]["natural_target_output_visible"],
            "target_post_visible": False,
            "later_trajectory_visible": False,
            "outcome_visible": False,
            "replay_response_visible": False,
            "whole_capsule_visible": False,
            "general_artifact_resolver_visible": False,
        },
        "compared_review_event_ids": compared_review_event_ids,
        "mechanical_source_suggestions_only": True,
        "curation_resolution_set_sha256": source_binding["source_packet"][
            "curation_resolution_set_sha256"
        ],
        "source_packet_sha256": source_binding["source_packet_sha256"],
    }
    if "source_records" in packet:
        record_tokens = {
            record["record_id"]: _opaque_token(assignment_id, "record", record["record_id"])
            for record in packet["source_records"]
        }
        result["source_records"] = [
            {
                "record_id": record_tokens[record["record_id"]],
                "author_role": record["author_role"],
                "exact_text": record["exact_text"],
            }
            for record in packet["source_records"]
        ]
        if packet["channel"] == "TRANSFORMATION":
            result["target_candidates"] = []
            for candidate in packet.get("target_candidates", []):
                focal_spans = candidate.get("focal_edit_spans")
                source = (
                    focal_spans[0]
                    if isinstance(focal_spans, list) and focal_spans
                    else candidate.get("curation_envelope") or candidate.get("exposure_span")
                )
                require(
                    isinstance(source, dict),
                    "PACKET_EVIDENCE_INCOMPLETE",
                    "target candidate lacks an inspectable source span",
                )
                matching_records = [
                    item
                    for item in packet["source_records"]
                    if item.get("record_sha256") == candidate.get("container_sha256")
                ]
                require(
                    len(matching_records) == 1,
                    "PACKET_EVIDENCE_INCOMPLETE",
                    "target candidate does not bind exactly one source record",
                )
                record = matching_records[0]
                result["target_candidates"].append(
                    {
                        "candidate_status": candidate.get("edit_span_status"),
                        "record_id": record_tokens[record["record_id"]],
                        "selection_hint": _span_hint(source, record["exact_text"]),
                        "source_provenance_only": True,
                    }
                )
            result["target_candidate_status"] = packet["target_candidate_status"]
            result["reviewer_must_select_semantics"] = True
    if "natural_action" in packet:
        result["natural_action"] = json_copy(packet["natural_action"])
        result["descriptive_only_not_gold_input"] = True
        result["replay_response_used"] = False
    result["assignment_packet_sha256"] = canonical_sha256(result)
    validate_schema_record("curator_packet.schema.json", result)
    return result


def _transform_payload_identities(
    publication: CurationPublication,
    *,
    unit_id: str,
    assignment_id: str,
    payload: dict[str, Any],
    to_browser: bool,
) -> dict[str, Any]:
    packet = publication.packet(unit_id, "TRANSFORMATION")
    record_map = {
        record["record_id"]: _opaque_token(assignment_id, "record", record["record_id"])
        for record in packet["source_records"]
    }
    evidence_map = {
        item["evidence_id"]: _opaque_token(assignment_id, "evidence", item["evidence_id"])
        for item in packet["evidence"]
    }
    if not to_browser:
        record_map = {value: key for key, value in record_map.items()}
        evidence_map = {value: key for key, value in evidence_map.items()}
    result = cast(dict[str, Any], json_copy(payload))
    for key in ("focal_target_spans", "oracle_target_spans", "protected_spans"):
        for span in result.get(key, []):
            require(
                span.get("record_id") in record_map,
                "PROPOSAL_IDENTITY_INVALID",
                "span record token is not in this assignment",
            )
            span["record_id"] = record_map[span["record_id"]]
    sham = result.get("sham_span")
    if isinstance(sham, dict):
        require(
            sham.get("record_id") in record_map,
            "PROPOSAL_IDENTITY_INVALID",
            "sham record token is not in this assignment",
        )
        sham["record_id"] = record_map[sham["record_id"]]
    for repair in result.get("delimiter_repairs", []):
        span = repair.get("deleted_syntax_span")
        require(
            isinstance(span, dict) and span.get("record_id") in record_map,
            "PROPOSAL_IDENTITY_INVALID",
            "delimiter repair record token is not in this assignment",
        )
        span["record_id"] = record_map[span["record_id"]]
    ids = result.get("correction_evidence_ids", [])
    require(
        all(item in evidence_map for item in ids),
        "PROPOSAL_IDENTITY_INVALID",
        "correction evidence token is not in this assignment",
    )
    result["correction_evidence_ids"] = [evidence_map[item] for item in ids]
    return result


def _action_payload_identities(
    publication: CurationPublication,
    *,
    unit_id: str,
    assignment_id: str,
    payload: dict[str, Any],
    to_browser: bool,
) -> dict[str, Any]:
    packet = publication.packet(unit_id, "ACTION_GOLD")
    evidence_map = {
        item["evidence_id"]: _opaque_token(assignment_id, "evidence", item["evidence_id"])
        for item in packet["evidence"]
    }
    if not to_browser:
        evidence_map = {value: key for key, value in evidence_map.items()}
    result = cast(dict[str, Any], json_copy(payload))
    for predicate in result.get("predicates", []):
        ids = predicate.get("evidence_ids", [])
        require(
            isinstance(ids, list) and all(item in evidence_map for item in ids),
            "PROPOSAL_IDENTITY_INVALID",
            "predicate evidence token is not in this assignment",
        )
        predicate["evidence_ids"] = [evidence_map[item] for item in ids]
    return result


def create_app(
    publication: CurationPublication,
    store: AnnotationStore,
    *,
    ai_candidate_workspace: AICandidateWorkspace | None = None,
    ai_exposure_workspace: AICandidateWorkspace | None = None,
) -> FastAPI:
    """Construct the private app without opening a socket or starting a server."""

    solo_mode = store.workspace_mode == "SOLO_FIRST_PASS"
    require(
        ai_candidate_workspace is None or solo_mode,
        "AI_CANDIDATE_MODE_INVALID",
        "AI candidate assistance is available only in the non-formal solo workspace",
    )
    require(
        ai_exposure_workspace is None or not solo_mode,
        "AI_CANDIDATE_MODE_INVALID",
        "formal AI-exposure enforcement is unavailable in a solo workspace",
    )
    require(
        solo_mode or ai_exposure_workspace is not None,
        "AI_CANDIDATE_EXPOSURE_GUARD_REQUIRED",
        "formal workspace requires the sealed D-031 exposure guard",
    )
    app = FastAPI(
        title="G1.6 Gold Curation Workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    sessions: dict[str, dict[str, str]] = {}

    @app.middleware("http")
    async def security_boundary(request: Request, call_next: Any) -> Response:
        host = request.url.hostname
        response: Response
        if not _loopback_peer(request):
            response = JSONResponse({"error": "LOOPBACK_PEER_REQUIRED"}, status_code=403)
        elif host not in ALLOWED_HOSTS:
            response = JSONResponse({"error": "LOOPBACK_HOST_REQUIRED"}, status_code=403)
        else:
            origin = request.headers.get("origin")
            mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
            origin_required = mutating and request.url.path != "/api/session"
            if (origin_required and origin is None) or (
                origin is not None and not _same_request_origin(request, origin)
            ):
                response = JSONResponse({"error": "LOOPBACK_ORIGIN_REQUIRED"}, status_code=403)
            elif (
                mutating
                and request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                response = JSONResponse({"error": "JSON_CONTENT_TYPE_REQUIRED"}, status_code=415)
            elif mutating and request.headers.get("content-encoding") is not None:
                response = JSONResponse({"error": "CONTENT_ENCODING_FORBIDDEN"}, status_code=415)
            elif mutating and request.headers.get("content-length") is None:
                response = JSONResponse({"error": "CONTENT_LENGTH_REQUIRED"}, status_code=411)
            else:
                content_length_valid = True
                if mutating:
                    try:
                        content_length = int(request.headers["content-length"])
                        content_length_valid = 0 <= content_length <= MAX_HTTP_REQUEST_BYTES
                    except (KeyError, ValueError):
                        content_length_valid = False
                body_too_large = not content_length_valid
                if not body_too_large and mutating:
                    body_too_large = len(await request.body()) > MAX_HTTP_REQUEST_BYTES
                if body_too_large:
                    response = JSONResponse({"error": "REQUEST_BODY_TOO_LARGE"}, status_code=413)
                    response.headers["Connection"] = "close"
                else:
                    public_path = request.url.path in {
                        "/",
                        "/api/config",
                        "/api/session",
                    } or request.url.path.startswith("/assets/")
                    token = request.cookies.get("g1_session")
                    session = sessions.get(token or "")
                    if not public_path and session is None:
                        response = JSONResponse(
                            {"error": "REVIEWER_SESSION_REQUIRED"}, status_code=401
                        )
                    elif (
                        mutating
                        and request.url.path != "/api/session"
                        and (
                            session is None
                            or request.headers.get("x-g1-csrf-token") != session["csrf_token"]
                        )
                    ):
                        response = JSONResponse({"error": "CSRF_TOKEN_REQUIRED"}, status_code=403)
                    else:
                        request.state.reviewer_session = session
                        if ai_exposure_workspace is None:
                            response = await call_next(request)
                        else:
                            try:
                                with ai_exposure_workspace.formal_registry_guard(
                                    store.reviewer_registry
                                ):
                                    response = await call_next(request)
                            except CurationError as exc:
                                response = JSONResponse(
                                    {"error": exc.code, "message": exc.message},
                                    status_code=400,
                                )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; connect-src 'self'; "
            "style-src 'self'; script-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.exception_handler(CurationError)
    async def curation_error(_: Request, exc: CurationError) -> JSONResponse:
        status = (
            409 if exc.code.endswith(("NOT_READY", "NOT_REQUIRED", "ALREADY_SUBMITTED")) else 400
        )
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=status)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/assets/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(STATIC_ROOT / "app.js", media_type="text/javascript; charset=utf-8")

    @app.get("/assets/styles.css")
    async def styles() -> FileResponse:
        return FileResponse(STATIC_ROOT / "styles.css", media_type="text/css; charset=utf-8")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        catalog = option_catalog()
        roles = list(getattr(store, "available_roles", tuple(catalog["roles"])))
        current_phase = store.current_phase() if solo_mode else None  # type: ignore[attr-defined]
        result = {
            **catalog,
            "roles": roles,
            "unit_count": 190,
            "workspace_mode": store.workspace_mode,
            "authoritative_state": "REPO_EXTERNAL_APPEND_ONLY_JOURNAL",
            "formal_annotation_open": store.formal_annotation_open,
            "solo_first_pass": solo_mode,
            "ai_candidate_assistance": {
                "enabled": ai_candidate_workspace is not None,
                "campaign_id": None
                if ai_candidate_workspace is None
                else ai_candidate_workspace.campaign_id,
                "ai_semantic_suggestion_performed": ai_candidate_workspace is not None,
                "three_agents_are_independent_human_reviewers": False,
                "counts_as_independent_review": False,
                "auto_apply_allowed": False,
                "human_review_required": True,
            },
            "first_pass_lock_open": bool(getattr(store, "first_pass_lock_open", False)),
            "current_phase": current_phase,
            "review_authority": {
                "counts_as_independent_review": not solo_mode,
                "formal_resolution_eligible": not solo_mode,
                "adjudication_eligible": not solo_mode,
                "formal_export_eligible": False,
                "admission_eligible": False,
                "promotion_allowed": False,
                "replay_eligible": False,
                "cross_channel_exposed": solo_mode,
            },
            "readiness": {
                "codec_gate_open": store.preview_gate_open,
                "human_curation_complete": False,
                "formal_g1_6_bundle": False,
                "admission_ready": False,
                "execution_ready": False,
                "formal_replay_ready": False,
            },
            "safety": {
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
                "external_network_used": False,
                "provider_client_created": False,
                "provider_invoked": False,
                "gpu_probed": False,
                "gpu_used": False,
                "model_loaded": False,
                "replay_executed": False,
                "gui_or_tool_action_executed": False,
            },
            "preview_tokenizer_available": publication.preview_tokenizer_status(),
            "codec_gate_receipt_sha256": None
            if store.codec_gate_receipt is None
            else store.codec_gate_receipt["receipt_sha256"],
        }
        return result

    @app.post("/api/session")
    async def create_session(request: Request) -> JSONResponse:
        body = await _closed_json_request(
            request,
            required={"reviewer_id", "role", "access_secret"},
            label="session request",
        )
        if ai_exposure_workspace is not None:
            store.assert_formal_ai_assistance_eligibility(
                ai_exposure_workspace.exposed_stable_principal_commitments()
            )
        reviewer_id, role, identity_commitment = store.authenticate_identity(
            body["reviewer_id"], body["role"], body["access_secret"]
        )
        token = secrets.token_urlsafe(32)
        session = {
            "reviewer_id": reviewer_id,
            "reviewer_role": role,
            "reviewer_identity_sha256": identity_commitment,
            "csrf_token": secrets.token_urlsafe(32),
        }
        sessions[token] = session
        response = JSONResponse(
            {
                "reviewer_identity_sha256": session["reviewer_identity_sha256"],
                "reviewer_role": role,
                "csrf_token": session["csrf_token"],
            }
        )
        response.set_cookie(
            "g1_session",
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    def browser_ai_candidates(
        *,
        unit_id: str,
        assignment_id: str,
        reviewer_identity_sha256: str,
        stable_principal_commitment: str,
    ) -> dict[str, Any]:
        require(
            ai_candidate_workspace is not None,
            "AI_CANDIDATE_ASSISTANCE_UNAVAILABLE",
            "no sealed AI candidate campaign is configured",
        )
        assert ai_candidate_workspace is not None
        ai_candidate_workspace.record_exposure(
            reviewer_identity_sha256,
            stable_principal_commitment,
        )
        source = publication.packet(unit_id, "ACTION_GOLD")
        evidence_tokens = {
            item["evidence_id"]: _opaque_token(assignment_id, "evidence", item["evidence_id"])
            for item in source["evidence"]
        }
        latest = ai_candidate_workspace.latest_decisions(reviewer_identity_sha256)
        outputs: list[dict[str, Any]] = []
        for output in ai_candidate_workspace.outputs_for_unit(unit_id):
            items: list[dict[str, Any]] = []
            for candidate in output["candidate_items"]:
                require(
                    all(item in evidence_tokens for item in candidate["evidence_ids"]),
                    "AI_CANDIDATE_VISIBILITY_VIOLATION",
                    "candidate cites evidence outside this assignment",
                )
                current = latest.get(candidate["candidate_id"])
                items.append(
                    {
                        "candidate_token": _opaque_token(
                            assignment_id, "candidate", candidate["candidate_id"]
                        ),
                        "candidate_kind": "ACTION_PREDICATE",
                        "predicate": json_copy(candidate["predicate"]),
                        "evidence_tokens": [
                            evidence_tokens[item] for item in candidate["evidence_ids"]
                        ],
                        "concise_rationale": candidate["concise_rationale"],
                        "uncertainty_note": candidate["uncertainty_note"],
                        "current_decision": None
                        if current is None
                        else {
                            "decision": current["decision"],
                            "human_note": current["human_note"],
                            "decision_event_token": _opaque_token(
                                assignment_id, "decision", current["event_id"]
                            ),
                        },
                    }
                )
            outputs.append(
                {
                    "agent_slot": output["agent_slot"],
                    "response_kind": output["response_kind"],
                    "candidate_items": items,
                    "abstain_reason": output["abstain_reason"],
                }
            )
        require(
            [item["agent_slot"] for item in outputs] == ["A", "B", "C"],
            "AI_CANDIDATE_INVALID",
            "candidate browser projection slot order differs",
        )
        result = {
            "assignment_id": assignment_id,
            "agent_outputs": outputs,
            "notice": {
                "ai_candidate_is_not_evidence": True,
                "three_agents_are_independent_human_reviewers": False,
                "no_vote_rank_or_winner": True,
                "copy_changes_browser_memory_only": True,
                "annotation_form_not_saved_or_finalized": True,
            },
            "authority": {
                "counts_as_independent_review": False,
                "formal_resolution_eligible": False,
                "admission_eligible": False,
                "replay_eligible": False,
                "auto_apply_allowed": False,
                "human_review_required": True,
            },
        }
        validate_ai_schema_record("ai_action_gold_candidate_browser.schema.json", result)
        return result

    def reviewer_session(request: Request) -> tuple[str, str]:
        session = request.state.reviewer_session
        require(
            isinstance(session, dict),
            "REVIEWER_SESSION_REQUIRED",
            "reviewer session is missing",
        )
        if ai_exposure_workspace is not None:
            store.assert_formal_ai_assistance_eligibility(
                ai_exposure_workspace.exposed_stable_principal_commitments()
            )
        return session["reviewer_id"], session["reviewer_role"]

    def assignment_projection(
        *,
        unit_id: str,
        assignment_id: str,
        reviewer_id: str,
        reviewer_role: str,
        channel: str,
        compared_review_event_ids: list[str] | None = None,
        enforce_solo_phase: bool = True,
    ) -> dict[str, Any]:
        if solo_mode and enforce_solo_phase:
            store.assert_channel_open(channel)  # type: ignore[attr-defined]
        if channel == "CONSISTENCY_AUDIT":
            require(
                store.consistency_ready(unit_id),
                "CONSISTENCY_AUDIT_NOT_READY",
                "consistency audit remains sealed",
            )
            source = publication.consistency_packet(unit_id)
        else:
            source = publication.packet(unit_id, channel)
        binding = store.bind_source_packet(unit_id, channel)
        projected = _browser_packet(
            source,
            assignment_id=assignment_id,
            role=reviewer_role,
            reviewer_identity_sha256=store.identity_commitment(reviewer_id),
            source_binding=binding,
            compared_review_event_ids=compared_review_event_ids or [],
        )
        store.bind_assignment_packet(projected)
        return projected

    def assignment_status(
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
        channel: str,
        precomputed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project all eight UI states from authoritative bytes, not browser memory."""

        status = precomputed or store.status_for(unit_id, reviewer_role, reviewer_id)
        try:
            if channel == "CONSISTENCY_AUDIT" and store.consistency_ready(unit_id):
                publication.consistency_packet(unit_id)
            else:
                publication.packet(
                    unit_id,
                    "TRANSFORMATION" if channel == "CONSISTENCY_AUDIT" else channel,
                )
        except CurationError:
            return {
                **status,
                "state": "BLOCKED_INVALID_INPUT",
                "workflow_state": "BLOCKED_INVALID_INPUT",
                "can_open": False,
            }
        return status

    @app.get("/api/assignments")
    async def assignments(request: Request) -> dict[str, Any]:
        reviewer_id, role = reviewer_session(request)
        items: list[dict[str, Any]] = []
        metric_rows: list[dict[str, Any]] = []
        if role == ADJUDICATOR_ROLE:
            pending_items: list[tuple[str, dict[str, Any], str]] = []
            for unit in publication.list_units():
                status = store.status_for(unit["unit_id"], role, reviewer_id)
                for channel in status.get("channels", []):
                    assignment_id = store.assignment_id(unit["unit_id"], role, channel=channel)
                    pending_items.append((assignment_id, unit, channel))
            for ordinal, (assignment_id, unit, channel) in enumerate(
                sorted(pending_items, key=lambda item: item[0]), start=1
            ):
                status = assignment_status(
                    unit_id=unit["unit_id"],
                    reviewer_id=reviewer_id,
                    reviewer_role=role,
                    channel=channel,
                )
                item = {
                    "assignment_id": assignment_id,
                    "ordinal": ordinal,
                    "channel": channel,
                    "review_role": role,
                    "own_status": status["own_state"],
                    "workflow_status": status["workflow_state"],
                    "state": status["state"],
                    "can_open": status["can_open"],
                    "packet_version": "v1",
                }
                items.append(item)
                metric_rows.append({**item, **unit})
        else:
            channel = role_channel(role)
            solo_statuses = (
                store.statuses_for_role(role, reviewer_id)  # type: ignore[attr-defined]
                if solo_mode
                else None
            )
            blinded_units = sorted(
                (
                    store.assignment_id(unit["unit_id"], role),
                    unit,
                )
                for unit in publication.list_units()
            )
            for ordinal, (assignment_id, unit) in enumerate(blinded_units, start=1):
                status = assignment_status(
                    unit_id=unit["unit_id"],
                    reviewer_id=reviewer_id,
                    reviewer_role=role,
                    channel=channel,
                    precomputed=None if solo_statuses is None else solo_statuses[unit["unit_id"]],
                )
                item = {
                    "assignment_id": assignment_id,
                    "ordinal": ordinal,
                    "channel": channel,
                    "review_role": role,
                    "own_status": status["own_state"],
                    "workflow_status": status["workflow_state"],
                    "state": status["state"],
                    "can_open": status["can_open"],
                    "packet_version": "v1",
                }
                items.append(item)
                metric_rows.append({**item, **unit})

        def counts_for(key: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for item in items:
                value = cast(str, item[key])
                result[value] = result.get(value, 0) + 1
            return result

        def grouped(dimension: str) -> dict[str, dict[str, int]]:
            result: dict[str, dict[str, int]] = {}
            for row in metric_rows:
                value = cast(str, row[dimension])
                bucket = result.setdefault(value, {})
                state = cast(str, row["state"])
                bucket[state] = bucket.get(state, 0) + 1
            return result

        return {
            "items": items,
            "counts": counts_for("state"),
            "own_counts": counts_for("own_status"),
            "workflow_counts": counts_for("workflow_status"),
            "breakdowns": {
                "model": grouped("model_id"),
                "unit_kind": grouped("unit_kind"),
                "channel": grouped("channel"),
                "role": grouped("review_role"),
                "state": counts_for("state"),
            },
            "total": len(items),
            "current_phase": store.current_phase() if solo_mode else None,  # type: ignore[attr-defined]
        }

    @app.get("/api/assignments/{assignment_id}/packet")
    async def packet(
        request: Request, assignment_id: str, channel: str | None = None
    ) -> dict[str, Any]:
        reviewer_id, role = reviewer_session(request)
        unit_id = store.resolve_assignment(
            assignment_id,
            role,
            channel=channel if role == ADJUDICATOR_ROLE else None,
        )
        if role == ADJUDICATOR_ROLE:
            require(
                channel in {"ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT"},
                "CHANNEL_INVALID",
                "adjudication channel is required",
            )
            assert channel is not None
            case = store.adjudication_case(unit_id, channel, reviewer_id)
            case.pop("unit_id", None)
            case["primary"].pop("reviewer_identity_sha256", None)
            case["secondary"].pop("reviewer_identity_sha256", None)
            if channel == "TRANSFORMATION":
                case["primary"]["payload"] = _transform_payload_identities(
                    publication,
                    unit_id=unit_id,
                    assignment_id=assignment_id,
                    payload=case["primary"]["payload"],
                    to_browser=True,
                )
                case["secondary"]["payload"] = _transform_payload_identities(
                    publication,
                    unit_id=unit_id,
                    assignment_id=assignment_id,
                    payload=case["secondary"]["payload"],
                    to_browser=True,
                )
            elif channel == "ACTION_GOLD":
                case["primary"]["payload"] = _action_payload_identities(
                    publication,
                    unit_id=unit_id,
                    assignment_id=assignment_id,
                    payload=case["primary"]["payload"],
                    to_browser=True,
                )
                case["secondary"]["payload"] = _action_payload_identities(
                    publication,
                    unit_id=unit_id,
                    assignment_id=assignment_id,
                    payload=case["secondary"]["payload"],
                    to_browser=True,
                )
            projected = assignment_projection(
                unit_id=unit_id,
                assignment_id=assignment_id,
                reviewer_id=reviewer_id,
                reviewer_role=role,
                channel=channel,
                compared_review_event_ids=[
                    case["primary"]["event_id"],
                    case["secondary"]["event_id"],
                ],
            )
            return {
                "packet": projected,
                "adjudication": case,
            }
        expected_channel = role_channel(role)
        projected = assignment_projection(
            unit_id=unit_id,
            assignment_id=assignment_id,
            reviewer_id=reviewer_id,
            reviewer_role=role,
            channel=expected_channel,
        )
        draft = store.latest_draft(unit_id, reviewer_id, role)
        if draft is not None and expected_channel == "TRANSFORMATION":
            draft = _transform_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=assignment_id,
                payload=draft,
                to_browser=True,
            )
        elif draft is not None and expected_channel == "ACTION_GOLD":
            draft = _action_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=assignment_id,
                payload=draft,
                to_browser=True,
            )
        return {
            "packet": projected,
            "draft": draft,
            "status": store.status_for(unit_id, role, reviewer_id),
        }

    async def action_gold_ai_candidates(request: Request, assignment_id: str) -> dict[str, Any]:
        """Return only sealed, assignment-scoped suggestions; never generate or save a review."""

        reviewer_id, role = reviewer_session(request)
        require(
            solo_mode and role_channel(role) == "ACTION_GOLD",
            "AI_CANDIDATE_ROLE_INVALID",
            "AI Action-Gold candidates are available only in the solo Action-Gold stage",
        )
        unit_id = store.resolve_assignment(assignment_id, role)
        projected = assignment_projection(
            unit_id=unit_id,
            assignment_id=assignment_id,
            reviewer_id=reviewer_id,
            reviewer_role=role,
            channel="ACTION_GOLD",
        )
        require(
            projected["channel"] == "ACTION_GOLD",
            "AI_CANDIDATE_SOURCE_MISMATCH",
            "candidate assignment channel differs",
        )
        return browser_ai_candidates(
            unit_id=unit_id,
            assignment_id=assignment_id,
            reviewer_identity_sha256=store.identity_commitment(reviewer_id),
            stable_principal_commitment=store.reviewer_registry.stable_principal_commitment(
                reviewer_id
            ),
        )

    async def ai_candidate_progress(request: Request) -> dict[str, Any]:
        reviewer_id, role = reviewer_session(request)
        require(
            solo_mode and role_channel(role) == "ACTION_GOLD",
            "AI_CANDIDATE_ROLE_INVALID",
            "AI candidate progress is available only to the solo Action-Gold curator",
        )
        require(
            ai_candidate_workspace is not None,
            "AI_CANDIDATE_ASSISTANCE_UNAVAILABLE",
            "no sealed AI candidate campaign is configured",
        )
        assert ai_candidate_workspace is not None
        return ai_candidate_workspace.progress(store.identity_commitment(reviewer_id))

    async def record_ai_candidate_decision(request: Request) -> dict[str, Any]:
        reviewer_id, role = reviewer_session(request)
        require(
            solo_mode and role_channel(role) == "ACTION_GOLD",
            "AI_CANDIDATE_ROLE_INVALID",
            "AI candidate decisions are available only in the solo Action-Gold stage",
        )
        require(
            ai_candidate_workspace is not None,
            "AI_CANDIDATE_ASSISTANCE_UNAVAILABLE",
            "no sealed AI candidate campaign is configured",
        )
        assert ai_candidate_workspace is not None
        body = await _closed_json_request(
            request,
            required={
                "assignment_id",
                "candidate_token",
                "decision",
                "human_note",
                "human_confirmed_item_review",
                "human_verified_visible_evidence",
                "ai_candidate_is_not_evidence",
                "annotation_form_not_saved_or_finalized",
            },
            label="AI candidate human decision",
        )
        require(
            body["human_confirmed_item_review"] is True
            and body["human_verified_visible_evidence"] is True
            and body["ai_candidate_is_not_evidence"] is True
            and body["annotation_form_not_saved_or_finalized"] is True,
            "AI_DECISION_ATTESTATION_REQUIRED",
            "every AI candidate decision requires all explicit human attestations",
        )
        require(
            isinstance(body["assignment_id"], str)
            and isinstance(body["candidate_token"], str)
            and isinstance(body["decision"], str)
            and isinstance(body["human_note"], str),
            "AI_DECISION_INVALID",
            "AI candidate decision fields have invalid types",
        )
        assignment_id = body["assignment_id"]
        unit_id = store.resolve_assignment(assignment_id, role)
        assignment_projection(
            unit_id=unit_id,
            assignment_id=assignment_id,
            reviewer_id=reviewer_id,
            reviewer_role=role,
            channel="ACTION_GOLD",
        )
        candidates = [
            item
            for output in ai_candidate_workspace.outputs_for_unit(unit_id)
            for item in output["candidate_items"]
        ]
        matches = [
            item
            for item in candidates
            if _opaque_token(assignment_id, "candidate", item["candidate_id"])
            == body["candidate_token"]
        ]
        require(
            len(matches) == 1,
            "AI_CANDIDATE_UNKNOWN",
            "candidate token is not in this assignment",
        )
        candidate = matches[0]
        ai_candidate_workspace.record_exposure(
            store.identity_commitment(reviewer_id),
            store.reviewer_registry.stable_principal_commitment(reviewer_id),
        )
        event = ai_candidate_workspace.record_decision(
            unit_id=unit_id,
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
            human_identity_commitment=store.identity_commitment(reviewer_id),
            decision=body["decision"],
            human_note=body["human_note"],
        )
        return {
            "recorded": True,
            "candidate_token": body["candidate_token"],
            "decision": event["decision"],
            "human_note": event["human_note"],
            "decision_event_token": _opaque_token(assignment_id, "decision", event["event_id"]),
            "annotation_form_saved": False,
            "annotation_form_finalized": False,
            "counts_as_independent_review": False,
        }

    if ai_candidate_workspace is not None:
        app.add_api_route(
            "/api/assist/action-gold/{assignment_id}",
            action_gold_ai_candidates,
            methods=["GET"],
        )
        app.add_api_route(
            "/api/assist/progress",
            ai_candidate_progress,
            methods=["GET"],
        )
        app.add_api_route(
            "/api/assist/candidate-decisions",
            record_ai_candidate_decision,
            methods=["POST"],
        )

    @app.get("/api/assignments/{assignment_id}/binding")
    async def packet_binding(
        request: Request, assignment_id: str, channel: str | None = None
    ) -> dict[str, Any]:
        """Return only the blind identity/visibility preflight, never packet evidence."""

        reviewer_id, role = reviewer_session(request)
        unit_id = store.resolve_assignment(
            assignment_id,
            role,
            channel=channel if role == ADJUDICATOR_ROLE else None,
        )
        compared_review_event_ids: list[str] = []
        if role == ADJUDICATOR_ROLE:
            require(
                channel in {"ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT"},
                "CHANNEL_INVALID",
                "adjudication channel is required",
            )
            assert channel is not None
            case = store.adjudication_case(unit_id, channel, reviewer_id)
            compared_review_event_ids = [
                case["primary"]["event_id"],
                case["secondary"]["event_id"],
            ]
            expected_channel = channel
        else:
            expected_channel = role_channel(role)
        projected = assignment_projection(
            unit_id=unit_id,
            assignment_id=assignment_id,
            reviewer_id=reviewer_id,
            reviewer_role=role,
            channel=expected_channel,
            compared_review_event_ids=compared_review_event_ids,
        )
        return {
            "assignment_id": assignment_id,
            "channel": expected_channel,
            "review_role": role,
            "reviewer_identity_sha256": projected["reviewer_identity_sha256"],
            "source_packet_sha256": projected["source_packet_sha256"],
            "assignment_packet_sha256": projected["assignment_packet_sha256"],
            "visibility_notice": {
                "only_pre_cutoff_role_projected_evidence": True,
                "peer_answers_hidden_before_adjudication": role != ADJUDICATOR_ROLE,
                "only_same_channel_finalized_peers_visible": role == ADJUDICATOR_ROLE,
                "post_state_outcome_replay_hidden": True,
                "whole_capsule_and_paths_hidden": True,
                "provider_model_gpu_actions_unavailable": True,
            },
        }

    @app.get("/api/assignments/{assignment_id}/image")
    async def image(request: Request, assignment_id: str, channel: str | None = None) -> Response:
        reviewer_id, role = reviewer_session(request)
        if role == ADJUDICATOR_ROLE:
            require(
                channel == store.adjudicator_channel_for(reviewer_id),
                "REVIEWER_ROLE_COLLISION",
                "adjudicator is owner-bound to a different channel",
            )
        unit_id = store.resolve_assignment(
            assignment_id,
            role,
            channel=channel if role == ADJUDICATOR_ROLE else None,
        )
        if solo_mode:
            store.assert_channel_open(role_channel(role))  # type: ignore[attr-defined]
        if role.startswith("CONSISTENCY_AUDIT"):
            require(
                store.consistency_ready(unit_id),
                "CONSISTENCY_AUDIT_NOT_READY",
                "consistency audit remains sealed",
            )
        data, media_type, digest = publication.screenshot_bytes(unit_id)
        return Response(
            data,
            media_type=media_type,
            headers={"ETag": f'"sha256-{digest}"', "Cache-Control": "no-store"},
        )

    @app.post("/api/transformation-previews")
    async def transformation_preview(request: Request) -> dict[str, Any]:
        """Render a CPU-only G1.5 preview without exposing the complete request."""

        reviewer_id, role = reviewer_session(request)
        body = await _closed_json_request(
            request,
            required={"assignment_id", "preview_inputs"},
            label="transformation preview request",
        )
        compared_review_event_ids: list[str] = []
        if role == ADJUDICATOR_ROLE:
            require(
                store.adjudicator_channel_for(reviewer_id) == "TRANSFORMATION",
                "CHANNEL_INVALID",
                "only the transformation adjudicator may render this preview",
            )
            unit_id = store.resolve_assignment(
                body["assignment_id"], role, channel="TRANSFORMATION"
            )
            case = store.adjudication_case(unit_id, "TRANSFORMATION", reviewer_id)
            compared_review_event_ids = [
                case["primary"]["event_id"],
                case["secondary"]["event_id"],
            ]
        else:
            require(
                role_channel(role) == "TRANSFORMATION",
                "CHANNEL_INVALID",
                "only a transformation reviewer may render this preview",
            )
            unit_id = store.resolve_assignment(body["assignment_id"], role)
        assignment_projection(
            unit_id=unit_id,
            assignment_id=body["assignment_id"],
            reviewer_id=reviewer_id,
            reviewer_role=role,
            channel="TRANSFORMATION",
            compared_review_event_ids=compared_review_event_ids,
        )
        require(
            isinstance(body["preview_inputs"], dict),
            "PREVIEW_INPUT_INVALID",
            "transformation preview inputs must be an object",
        )
        stable_inputs = _transform_payload_identities(
            publication,
            unit_id=unit_id,
            assignment_id=body["assignment_id"],
            payload=cast(dict[str, Any], body["preview_inputs"]),
            to_browser=False,
        )
        preview = publication.build_transformation_preview(unit_id, stable_inputs)
        return _browser_transformation_preview(
            preview,
            assignment_id=body["assignment_id"],
        )

    async def persist_review(request: Request, *, lock: bool) -> dict[str, Any]:
        reviewer_id, reviewer_role = reviewer_session(request)
        body = await _closed_json_request(
            request,
            required={"assignment_id", "payload"},
            label="first-pass lock request" if lock else "draft request",
        )
        unit_id = store.resolve_assignment(body["assignment_id"], reviewer_role)
        channel = role_channel(reviewer_role)
        if lock and solo_mode and channel == "ACTION_GOLD" and ai_candidate_workspace is not None:
            ai_candidate_workspace.assert_unit_decisions_complete(
                unit_id,
                store.identity_commitment(reviewer_id),
            )
        try:
            projected = assignment_projection(
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                reviewer_id=reviewer_id,
                reviewer_role=reviewer_role,
                channel=channel,
            )
        except CurationError as exc:
            retrying_existing_solo_lock = (
                solo_mode
                and lock
                and exc.code == "SOLO_STAGE_BLOCKED"
                and store.has_locked_first_pass(  # type: ignore[attr-defined]
                    unit_id=unit_id,
                    reviewer_id=reviewer_id,
                    reviewer_role=reviewer_role,
                )
            )
            if not retrying_existing_solo_lock:
                raise
            # Only an already durable lock may rederive its old packet after the global phase has
            # advanced.  The journal lock below still requires every binding and payload byte to
            # match exactly; future-stage lock requests never reach payload/token validation.
            projected = assignment_projection(
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                reviewer_id=reviewer_id,
                reviewer_role=reviewer_role,
                channel=channel,
                enforce_solo_phase=False,
            )
        payload = body["payload"]
        if channel == "TRANSFORMATION":
            payload = _transform_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                payload=payload,
                to_browser=False,
            )
        elif channel == "ACTION_GOLD":
            payload = _action_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                payload=payload,
                to_browser=False,
            )
        method = store.submit_review if lock else store.save_draft
        event = method(
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            assignment_id=body["assignment_id"],
            source_packet_sha256=projected["source_packet_sha256"],
            assignment_packet_sha256=projected["assignment_packet_sha256"],
            payload=payload,
        )
        return {
            "saved": not lock,
            "locked": lock and solo_mode,
            "submitted": lock and not solo_mode,
            "event_id": event["event_id"],
            "payload_sha256": event["payload_sha256"],
        }

    @app.post("/api/reviews/draft")
    async def save_draft(request: Request) -> dict[str, Any]:
        require(
            not solo_mode,
            "SOLO_ENDPOINT_REQUIRED",
            "solo first-pass drafts must use the explicitly non-formal endpoint",
        )
        return await persist_review(request, lock=False)

    @app.post("/api/solo/draft")
    async def save_solo_draft(request: Request) -> dict[str, Any]:
        require(
            solo_mode,
            "FORMAL_ENDPOINT_REQUIRED",
            "solo first-pass endpoint is unavailable in a formal workspace",
        )
        return await persist_review(request, lock=False)

    @app.post("/api/reviews/submit")
    async def submit_review(request: Request) -> dict[str, Any]:
        require(
            not solo_mode,
            "SOLO_FIRST_PASS_FORMAL_SUBMISSION_BLOCKED",
            "solo first-pass records cannot enter the formal review endpoint",
        )
        return await persist_review(request, lock=True)

    @app.post("/api/solo/lock")
    async def lock_solo_first_pass(request: Request) -> dict[str, Any]:
        require(
            solo_mode,
            "FORMAL_ENDPOINT_REQUIRED",
            "solo first-pass endpoint is unavailable in a formal workspace",
        )
        return await persist_review(request, lock=True)

    @app.post("/api/adjudications/submit")
    async def submit_adjudication(request: Request) -> dict[str, Any]:
        require(
            not solo_mode,
            "SOLO_FIRST_PASS_ADJUDICATION_BLOCKED",
            "solo first-pass records cannot enter adjudication",
        )
        reviewer_id, reviewer_role = reviewer_session(request)
        require(
            reviewer_role == ADJUDICATOR_ROLE,
            "ROLE_INVALID",
            "only adjudicator session may resolve disagreement",
        )
        body = await _closed_json_request(
            request,
            required={
                "assignment_id",
                "channel",
                "resolved_payload",
                "field_resolutions",
                "rationale",
            },
            label="adjudication request",
        )
        unit_id = store.resolve_assignment(
            body["assignment_id"], ADJUDICATOR_ROLE, channel=body["channel"]
        )
        case = store.adjudication_case(unit_id, body["channel"], reviewer_id)
        field_resolutions = body["field_resolutions"]
        require(
            isinstance(field_resolutions, dict)
            and set(field_resolutions) == set(case["disagreement_fields"])
            and all(
                isinstance(value, str) and bool(value.strip())
                for value in field_resolutions.values()
            ),
            "ADJUDICATION_INVALID",
            "every material disagreement requires an explicit human resolution",
        )
        require(
            isinstance(body["rationale"], str) and bool(body["rationale"].strip()),
            "ADJUDICATION_INVALID",
            "overall adjudication rationale is required",
        )
        projected = assignment_projection(
            unit_id=unit_id,
            assignment_id=body["assignment_id"],
            reviewer_id=reviewer_id,
            reviewer_role=ADJUDICATOR_ROLE,
            channel=body["channel"],
            compared_review_event_ids=[
                case["primary"]["event_id"],
                case["secondary"]["event_id"],
            ],
        )
        resolved_payload = body["resolved_payload"]
        if body["channel"] == "TRANSFORMATION":
            resolved_payload = _transform_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                payload=resolved_payload,
                to_browser=False,
            )
        elif body["channel"] == "ACTION_GOLD":
            resolved_payload = _action_payload_identities(
                publication,
                unit_id=unit_id,
                assignment_id=body["assignment_id"],
                payload=resolved_payload,
                to_browser=False,
            )
        event = store.submit_adjudication(
            unit_id=unit_id,
            channel=body["channel"],
            reviewer_id=reviewer_id,
            assignment_id=body["assignment_id"],
            source_packet_sha256=projected["source_packet_sha256"],
            assignment_packet_sha256=projected["assignment_packet_sha256"],
            resolved_payload=resolved_payload,
            rationale=body["rationale"].strip()
            + "\n\n"
            + "\n".join(
                f"{field}: {field_resolutions[field].strip()}"
                for field in case["disagreement_fields"]
            ),
        )
        return {
            "submitted": True,
            "event_id": event["event_id"],
            "payload_sha256": event["payload_sha256"],
        }

    return app
