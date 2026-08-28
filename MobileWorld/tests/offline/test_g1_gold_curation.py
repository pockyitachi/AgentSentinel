"""CPU-only conformance tests for the private ALE-324/G1.6 curation workspace.

The suite deliberately uses the in-process ASGI transport, immutable source
fixtures, and repository-external temporary directories.  It never opens a
listening socket, invokes a provider/model, probes a GPU, performs replay, or
executes a MobileWorld action.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import types
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MOBILEWORLD_SOURCE_ROOT = REPOSITORY_ROOT / "MobileWorld/src"
G16_SCHEMA_ROOT = REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/g1_6"
REGISTRY_LOCK = REPOSITORY_ROOT / "mobileworld_audit_handoff/g1/registry.lock.v1.json"

# Importing mobile_world normally imports the Android runtime.  This test is
# intentionally an offline import-closure check, so expose only the package
# search path and let Python import mobile_world.offline.* directly.
if "mobile_world" not in sys.modules:
    mobile_world_package = types.ModuleType("mobile_world")
    mobile_world_package.__path__ = [str(MOBILEWORLD_SOURCE_ROOT / "mobile_world")]
    sys.modules["mobile_world"] = mobile_world_package

from mobile_world.offline.gold_curation import contracts as contracts_module  # noqa: E402
from mobile_world.offline.gold_curation.contracts import (  # noqa: E402
    ANNOTATION_EVENT_SCHEMA_VERSION,
    REVIEW_PACKET_SCHEMA_VERSION,
    REVIEW_PROPOSAL_SCHEMA_VERSION,
    WORKSPACE_PROTOCOL_VERSION,
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    option_catalog,
    validate_review_payload,
)
from mobile_world.offline.gold_curation.publication import (  # noqa: E402
    ACTIVE_G1_3_CAPSULE_SET_SHA256,
    ACTIVE_G1_3_MANIFEST_SHA256,
    ACTIVE_G1_3_PUBLICATION,
    CurationPublication,
    _open_regular_beneath,
    _safe_parts,
)
from mobile_world.offline.gold_curation.server import (  # noqa: E402
    MAX_HTTP_REQUEST_BYTES,
    _browser_packet,
    _browser_transformation_preview,
    create_app,
)
from mobile_world.offline.gold_curation.solo import (  # noqa: E402
    SOLO_EVENT_SCHEMA_VERSION,
    SOLO_REGISTRY_SCHEMA_VERSION,
    SOLO_REVIEW_ROLES,
    SOLO_WORKSPACE_SCHEMA_VERSION,
    SoloCuratorRegistry,
    SoloFirstPassStore,
)
from mobile_world.offline.gold_curation.store import (  # noqa: E402
    REVIEWER_REGISTRY_SCHEMA_VERSION,
    WORKSPACE_MANIFEST_SCHEMA_VERSION,
    AnnotationStore,
    ReviewerRegistry,
    write_codec_gate_receipt,
)

SHA256_ZERO = "0" * 64
SCREENSHOT_BYTES = b"\x89PNG\r\n\x1a\nG1.6-test-fixture"


class _EmptyAIExposureWorkspace:
    def exposed_stable_principal_commitments(self) -> frozenset[str]:
        return frozenset()

    def formal_registry_guard(self, _registry: ReviewerRegistry) -> Any:
        return nullcontext()


_EMPTY_AI_EXPOSURE_WORKSPACE = _EmptyAIExposureWorkspace()


def _create_formal_test_app(
    publication: Any,
    store: AnnotationStore,
) -> Any:
    return create_app(
        publication,
        store,
        ai_exposure_workspace=_EMPTY_AI_EXPOSURE_WORKSPACE,  # type: ignore[arg-type]
    )


PRINCIPALS: tuple[tuple[str, str, str, str | None], ...] = (
    ("action-primary", "ACTION_GOLD_PRIMARY", "secret-action-primary-0001", None),
    ("action-secondary", "ACTION_GOLD_SECONDARY", "secret-action-secondary-01", None),
    ("transform-primary", "TRANSFORMATION_PRIMARY", "secret-transform-primary-01", None),
    (
        "transform-secondary",
        "TRANSFORMATION_SECONDARY",
        "secret-transform-secondary",
        None,
    ),
    (
        "consistency-primary",
        "CONSISTENCY_AUDIT_PRIMARY",
        "secret-consistency-primary",
        None,
    ),
    (
        "consistency-secondary",
        "CONSISTENCY_AUDIT_SECONDARY",
        "secret-consistency-secondary",
        None,
    ),
    (
        "third-adjudicator",
        "ADJUDICATOR",
        "secret-third-adjudicator-01",
        "ACTION_GOLD",
    ),
    *tuple(
        (
            f"worker-{index:02d}",
            "ACTION_GOLD_PRIMARY",
            f"secret-concurrent-worker-{index:02d}",
            None,
        )
        for index in range(24)
    ),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InProcessASGIClient:
    """Synchronous facade over HTTPX's socket-free ASGI transport.

    Starlette 1.0's ``TestClient`` blocking portal hangs in the execution
    environment even for a one-route FastAPI app.  This facade preserves the
    same request/cookie semantics while invoking ASGI directly in-process.
    """

    def __init__(
        self,
        app: Any,
        *,
        base_url: str = "http://127.0.0.1",
        client: tuple[str, int] = ("127.0.0.1", 50000),
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.client = client
        self.cookies = httpx.Cookies()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, client=self.client)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self.base_url,
                cookies=self.cookies,
            ) as client:
                response = await client.request(method, path, **kwargs)
                self.cookies.update(response.cookies)
                return response

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def __enter__(self) -> InProcessASGIClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def _unit_id(index: int, *, clean_control: bool = False) -> str:
    prefix = "g1control" if clean_control else "g1case"
    return f"{prefix}-{index:024x}"


def _synthetic_units() -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for index in range(152):
        qwen = index < 139
        units.append(
            {
                "unit_id": _unit_id(index),
                "unit_kind": "STRICT_MHR",
                "model_id": "qwen3vl_8b" if qwen else "mai_ui_8b",
                "history_family": "flat_progress" if qwen else "raw_replay",
                "source_key": f"strict/{index:03d}",
            }
        )
    for index in range(38):
        qwen = index < 30
        units.append(
            {
                "unit_id": _unit_id(index, clean_control=True),
                "unit_kind": "CLEAN_CONTROL",
                "model_id": "qwen3vl_8b" if qwen else "mai_ui_8b",
                "history_family": "flat_progress" if qwen else "raw_replay",
                "source_key": f"control/{index:03d}",
            }
        )
    return units


def _internal_transformation_preview(
    *,
    clean_control: bool = False,
    sham_matched: bool = True,
) -> dict[str, Any]:
    """Build a representative internal G1.5 preview with confidential bindings."""

    arms = (
        ["ORIGINAL", "SHAM_BENIGN_EDIT"]
        if clean_control
        else ["ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"]
    )
    path = ["messages", 7, "content", 0, "text"]

    def arm_record(arm: str, index: int) -> dict[str, Any]:
        operation_id = f"internal-operation-{index}"
        return {
            "arm": arm,
            "rendered_history": [
                {
                    "container_path": path,
                    "record_ids": ["internal-record-stable-id"],
                    "source_text": "misleading fact and harmless detail",
                    "rendered_text": f"{arm}: rendered history",
                }
            ],
            "diffs": [
                {
                    "operation_id": operation_id,
                    "container_path": path,
                    "source_char_start": 0,
                    "source_char_end": 15,
                    "original_text": "misleading fact",
                    "rendered_text": "" if arm != "ORIGINAL" else "misleading fact",
                    "mapping_kind": "COPIED" if arm == "ORIGINAL" else "DELETED",
                }
            ],
            "list_insertions": [],
            "source_mappings": [
                {
                    "container_path": path,
                    "source_char_start": 0,
                    "source_char_end": 15,
                    "rendered_char_start": 0,
                    "rendered_char_end": 15 if arm == "ORIGINAL" else 0,
                    "kind": "COPIED" if arm == "ORIGINAL" else "DELETED",
                    "operation_id": operation_id,
                }
            ],
            "human_diff": "RAW-HUMAN-DIFF-STABLE-ID-MARKER payload.request_view.messages[7]",
            "target_only_diff": True,
            "source_mapping_reversible": True,
            "provider_invocation_allowed": False,
            "rendered_request_sha256": "4" * 64,
            "validation_receipt_sha256": "5" * 64,
        }

    return {
        "preview_scope": "CPU_ONLY_READ_ONLY",
        "plan_set_profile": "G1_CLEAN_CONTROL" if clean_control else "G1_STRICT_MHR",
        "preview_receipt_sha256": "9" * 64,
        "codec_id": "qwen-flat-progress-v1-CONFIDENTIAL",
        "tokenizer_id": "Qwen/CONFIDENTIAL-TOKENIZER",
        "source_request_sha256": "1" * 64,
        "history_ir_sha256": "2" * 64,
        "transformation_plan_sha256": "3" * 64,
        "correction_ranking": (
            None
            if clean_control
            else {
                "special_tokens_enabled": False,
                "tie_break_order": [
                    "token_count",
                    "utf8_byte_count",
                    "codepoint_count",
                    "lexicographic_utf8_bytes",
                ],
                "candidates": [
                    {
                        "text": "The corrected historical fact.",
                        "token_count": 6,
                        "utf8_byte_count": 30,
                        "codepoint_count": 30,
                        "rank": 1,
                    }
                ],
            }
        ),
        "correction_anchors": (
            []
            if clean_control
            else [
                {
                    "binding_id": "internal-binding-stable-id",
                    "target_record_id": "internal-record-stable-id",
                    "anchor": {
                        "container_path": path,
                        "insert_index": 1,
                        "expected_role": "user",
                        "placement": "BEFORE",
                        "context_kind": "TEXT_CONTENT_BLOCK",
                        "visible_prefix": "SENTINEL correction: ",
                        "visible_suffix": "",
                    },
                }
            ]
        ),
        "sham_token_match": {
            "special_tokens_enabled": False,
            "focal_token_count": 6,
            "sham_token_count": 6 if sham_matched else 20,
            "match_formula": "(5*sham>=4*focal && 4*sham<=5*focal) || abs(sham-focal)<=4",
            "matched": sham_matched,
        },
        "delimiter_repairs": (
            [
                {
                    "repair_id": "internal-repair-stable-id",
                    "arm": "SHAM_BENIGN_EDIT" if clean_control else "MASK",
                    "operation": "DELETE_ORPHAN_SEPARATOR",
                }
            ]
        ),
        "arms": [arm_record(arm, index) for index, arm in enumerate(arms)],
        "provider_invocation_allowed": False,
        "provider_invocation_count": 0,
        "treatment_response_generation_allowed": False,
        "treatment_response_count": 0,
        "network_used": False,
        "gpu_used": False,
        "replay_executed": False,
        "gui_action_executed": False,
    }


class FakePublication:
    """Minimal read-only publication double for store and ASGI tests."""

    def __init__(self) -> None:
        self._units = _synthetic_units()
        self.validation_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.preview_calls: list[tuple[str, dict[str, Any]]] = []

    def list_units(self) -> list[dict[str, Any]]:
        return deepcopy(self._units)

    def _unit(self, unit_id: str) -> dict[str, Any]:
        return next(item for item in self._units if item["unit_id"] == unit_id)

    def source_packet_binding(
        self,
        unit_id: str,
        channel: str,
        *,
        curation_resolution_set_sha256: str | None = None,
    ) -> dict[str, Any]:
        unit = self._unit(unit_id)
        if channel == "CONSISTENCY_AUDIT":
            assert curation_resolution_set_sha256 is not None
        else:
            assert curation_resolution_set_sha256 is None
        subject = {
            "contract_version": "mobileworld.g1.gold-history-intervention/contract-v1",
            "channel": channel,
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
            "capsule_body_sha256": _sha256(unit_id.encode()),
            "unit": deepcopy(unit),
            "channel_input_sha256": _sha256(f"{unit_id}:{channel}:input".encode()),
            "ordered_evidence": [
                {
                    "evidence_id": "evidence-" + _sha256(unit_id.encode())[:24],
                    "projection_sha256": _sha256(f"{unit_id}:evidence".encode()),
                }
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
        digest = canonical_sha256(subject)
        return {
            "source_packet_id": "g1packet-" + digest[:24],
            "source_packet_sha256": digest,
            "source_packet": subject,
        }

    def packet(self, unit_id: str, channel: str) -> dict[str, Any]:
        unit = self._unit(unit_id)
        evidence = [
            {
                "evidence_id": "evidence-" + "1" * 24,
                "evidence_role": "task_instruction",
                "content_sha256": _sha256(b"Open the fixture application"),
                "model_visible_at_or_before_request": True,
                "source_event": {"event_id": "event-task", "event_seq": 1},
                "content": "Open the fixture application",
            },
            {
                "evidence_id": "evidence-" + "3" * 24,
                "evidence_role": "target_pre",
                "content_sha256": _sha256(b"target-pre-fixture"),
                "model_visible_at_or_before_request": True,
                "source_event": {"event_id": "event-pre", "event_seq": 2},
                "content": {
                    "screenshot": {
                        "width": 100,
                        "height": 200,
                        "mode": "RGB",
                        "representation": "PIXEL_BLOB",
                    },
                    "accessibility_tree": None,
                    "tool_call": None,
                    "ask_user_response": None,
                },
            },
        ]
        packet: dict[str, Any] = {
            "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
            "record_type": "gold_curation_review_packet",
            "channel": channel,
            "packet_id": "g1packet-" + _sha256(f"{unit_id}:{channel}".encode())[:24],
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_body_sha256": _sha256(unit_id.encode()),
            "unit": {
                **unit,
                "target_step": 3,
                "request_cutoff_event_id": "event-cutoff",
                "request_cutoff_event_seq": 3,
            },
            "task": {
                "task_name": "fixture-task",
                "instruction": "Open the fixture application",
                "instruction_sha256": _sha256(b"Open the fixture application"),
            },
            "evidence": evidence,
            "current_screenshot": {
                "available": True,
                "width": 100,
                "height": 200,
                "sha256": _sha256(SCREENSHOT_BYTES),
            },
            "visibility": {
                "history_visible": channel != "ACTION_GOLD",
                "natural_target_output_visible": False,
                "target_post_visible": False,
                "later_trajectory_visible": False,
                "outcome_visible": False,
                "replay_response_visible": False,
            },
            "mechanical_source_suggestions_only": True,
        }
        if channel == "TRANSFORMATION":
            record_text = (
                "Earlier progress: 页面🙂仍需检查。"
                if unit["model_id"] == "qwen3vl_8b"
                else 'Earlier reasoning🙂 <tool_call>{"action":"click"}</tool_call> tail'
            )
            record_id = "record-" + _sha256(unit_id.encode())[:24]
            packet["evidence"].insert(
                0,
                {
                    "evidence_id": "evidence-" + "2" * 24,
                    "evidence_role": "source_history",
                    "content_sha256": _sha256(record_text.encode()),
                    "model_visible_at_or_before_request": True,
                    "source_event": {"event_id": "event-history", "event_seq": 0},
                    "content": record_text,
                },
            )
            start = record_text.index("Earlier")
            end = start + len("Earlier")
            packet["source_records"] = [
                {
                    "record_id": record_id,
                    "record_sha256": _sha256(record_text.encode()),
                    "author_role": "assistant",
                    "exact_text": record_text,
                }
            ]
            packet["target_candidates"] = [
                {
                    "container_sha256": _sha256(record_text.encode()),
                    "edit_span_status": (
                        "G1_1_FROZEN"
                        if unit["model_id"] == "qwen3vl_8b"
                        else "G1_6_REVIEW_REQUIRED"
                    ),
                    "focal_edit_spans": [
                        {
                            "char_start": start,
                            "char_end": end,
                            "exact_text": record_text[start:end],
                        }
                    ],
                }
            ]
            packet["target_candidate_status"] = (
                "G1_1_FROZEN_EXACT" if unit["model_id"] == "qwen3vl_8b" else "G1_6_REVIEW_REQUIRED"
            )
            packet["reviewer_must_select_semantics"] = True
        return packet

    def consistency_packet(self, unit_id: str) -> dict[str, Any]:
        packet = self.packet(unit_id, "TRANSFORMATION")
        packet["channel"] = "CONSISTENCY_AUDIT"
        packet["natural_action"] = {
            "normalized_action": {"action_type": "click", "x": 10, "y": 20},
            "normalized_action_sha256": _sha256(b"natural-action"),
            "parse_outcome": "PARSED",
            "historical_reference_only": True,
        }
        packet["visibility"]["natural_target_output_visible"] = True
        packet["descriptive_only_not_gold_input"] = True
        packet["replay_response_used"] = False
        return packet

    def screenshot_bytes(self, unit_id: str) -> tuple[bytes, str, str]:
        self._unit(unit_id)
        return SCREENSHOT_BYTES, "image/png", _sha256(SCREENSHOT_BYTES)

    def record_bindings(self, unit_id: str) -> dict[str, dict[str, str]]:
        self._unit(unit_id)
        record_id = "record-" + _sha256(unit_id.encode())[:24]
        return {
            record_id: {
                "record_identity_sha256": _sha256(f"{unit_id}:record".encode()),
                "request_path": "payload.request_view.messages[0].content",
            }
        }

    def preview_tokenizer_status(self) -> dict[str, bool]:
        return {"qwen3vl_8b": True, "mai_ui_8b": True}

    def build_transformation_preview(
        self, unit_id: str, preview_inputs: dict[str, Any]
    ) -> dict[str, Any]:
        unit = self._unit(unit_id)
        self.preview_calls.append((unit_id, deepcopy(preview_inputs)))
        return _internal_transformation_preview(clean_control=unit["unit_kind"] == "CLEAN_CONTROL")

    def validate_review_payload_binding(
        self, unit_id: str, channel: str, payload: dict[str, Any]
    ) -> None:
        self._unit(unit_id)
        self.validation_calls.append((unit_id, channel, deepcopy(payload)))


def _action_payload(*, x_min: int = 4) -> dict[str, Any]:
    return {
        "proposal_kind": "ACTION_GOLD",
        "disposition": "ACCEPT",
        "exclusion_reason": None,
        "predicates": [
            {
                "predicate_kind": "POINT_REGION",
                "action_type": "click",
                "rationale": "The visible control is the complete reasonable next step.",
                "evidence_ids": ["evidence-" + "1" * 24],
                "human_selected": True,
                "regions": [
                    {
                        "shape": "BOUNDING_BOX",
                        "x_min": x_min,
                        "y_min": 8,
                        "x_max": x_min + 10,
                        "y_max": 20,
                    }
                ],
                "tolerance_px": 2,
            }
        ],
        "evidence_rationale": "Task instruction and target-pre GUI support this action set.",
        "closed_world_confirmed": True,
        "all_reasonable_actions_enumerated": True,
    }


def _normalized_action(action_type: str = "wait") -> dict[str, Any]:
    return {
        "class": "mobile_world.runtime.utils.models.JSONAction",
        "serializer": "pydantic model_dump(mode=json, exclude_none=false)",
        "serializer_version": "2.11.7",
        "value": {
            "action_json": None,
            "action_name": None,
            "action_type": action_type,
            "app_name": None,
            "clear_text": None,
            "direction": None,
            "end_x": None,
            "end_y": None,
            "goal_status": None,
            "index": None,
            "keycode": None,
            "start_x": None,
            "start_y": None,
            "text": None,
            "x": None,
            "y": None,
        },
    }


def _exact_action_payload() -> dict[str, Any]:
    value = _action_payload()
    value["predicates"] = [
        {
            "predicate_kind": "EXACT_NORMALIZED_ACTION",
            "action_type": "wait",
            "normalized_action": _normalized_action(),
            "evidence_ids": ["evidence-" + "1" * 24],
            "rationale": "Waiting is the exact accepted normalized production action.",
            "human_selected": True,
        }
    ]
    return value


def _browser_action_payload(packet: dict[str, Any], *, x_min: int = 4) -> dict[str, Any]:
    payload = _action_payload(x_min=x_min)
    payload["predicates"][0]["evidence_ids"] = [packet["evidence"][0]["evidence_token"]]
    return payload


def _excluded_action_payload() -> dict[str, Any]:
    return {
        "proposal_kind": "ACTION_GOLD",
        "disposition": "EXCLUDE",
        "exclusion_reason": "NO_GOLD_CONSENSUS",
        "predicates": [],
        "evidence_rationale": "The visible state has no enumerable one-step action set.",
        "closed_world_confirmed": False,
        "all_reasonable_actions_enumerated": False,
    }


def _span(record_id: str, text: str, start: int, end: int) -> dict[str, Any]:
    exact = text[start:end]
    return {
        "record_id": record_id,
        "char_start": start,
        "char_end": end,
        "utf8_byte_start": len(text[:start].encode("utf-8")),
        "utf8_byte_end": len(text[:end].encode("utf-8")),
        "exact_text": exact,
        "span_sha256": _sha256(exact.encode()),
        "human_selected": True,
    }


def _transformation_payload(
    *,
    record_id: str = "record-fixture",
    record_text: str = "misleading fact and harmless detail",
    focal_start: int = 0,
    focal_end: int = 15,
    correction_evidence_id: str = "evidence-" + "2" * 24,
) -> dict[str, Any]:
    focal = _span(record_id, record_text, focal_start, focal_end)
    sham_start = record_text.index("harmless")
    sham = _span(record_id, record_text, sham_start, len(record_text))
    return {
        "proposal_kind": "TRANSFORMATION",
        "unit_kind": "STRICT_MHR",
        "history_family": "flat_progress",
        "disposition": "ACCEPT",
        "exclusion_reason": None,
        "focal_target_spans": [focal],
        "oracle_target_spans": [deepcopy(focal)],
        "correction_candidates": [
            {
                "text": "The corrected historical fact.",
                "rationale": "This is the shortest evidence-supported factual correction.",
                "human_authored": True,
            }
        ],
        "correction_text": "The corrected historical fact.",
        "correction_evidence_ids": [correction_evidence_id],
        "correction_is_minimal_fact": True,
        "correction_contains_no_advice": True,
        "oracle_preserves_non_target_history": True,
        "protected_spans": [],
        "delimiter_repairs": [],
        "sham_span": sham,
        "sham_match_checks": {
            "same_role": True,
            "same_content_kind": True,
            "same_representation_class": True,
            "relative_third_matched": True,
            "same_record_preferred_or_depth_within_one": True,
            "token_size_matched": True,
            "no_entailment": True,
            "no_contradiction": True,
            "no_lexical_alias": True,
            "not_hard_task_requirement": True,
            "not_action_discriminant": True,
        },
        "clean_control_reference_anchor_confirmed": False,
        "preview_receipt_sha256": "9" * 64,
        "preview_human_confirmed": True,
        "rationale": "Human-selected target, correction, oracle, and benign sham.",
    }


def _binding_only_transformation_payload(**kwargs: Any) -> dict[str, Any]:
    """Return a schema-valid proposal with preview enforcement disabled for binding tests.

    The caller can validate the original accepted proposal separately; production finalization
    always requires the exact preview receipt.  These focused tests intentionally exercise only
    source-span and delimiter binding without constructing a G1.5 tokenizer-backed preview.
    """

    payload = _transformation_payload(**kwargs)
    payload["preview_receipt_sha256"] = None
    return payload


def _browser_preview_inputs(packet: dict[str, Any]) -> dict[str, Any]:
    record = packet["source_records"][0]
    text = record["exact_text"]
    focal_end = len("Earlier")
    sham_start = max(focal_end, len(text) - 4)
    focal = _span(record["record_id"], text, 0, focal_end)
    evidence_token = next(
        item["evidence_token"]
        for item in packet["evidence"]
        if item["evidence_role"] == "task_instruction"
    )
    return {
        "focal_target_spans": [focal],
        "oracle_target_spans": [deepcopy(focal)],
        "correction_candidates": [
            {
                "text": "The corrected historical fact.",
                "rationale": "Human-authored from exact pre-cutoff evidence.",
                "human_authored": True,
            }
        ],
        "correction_evidence_ids": [evidence_token],
        "protected_spans": [],
        "delimiter_repairs": [],
        "sham_span": _span(record["record_id"], text, sham_start, len(text)),
    }


def _consistency_payload(
    label: str = "HISTORY_AND_GUI_TASK_CONSISTENT",
) -> dict[str, Any]:
    return {
        "proposal_kind": "CONSISTENCY_AUDIT",
        "consistency_label": label,
        "history_consistency_rationale": "The original action follows the visible history.",
        "gui_task_consistency_rationale": "The original action also follows the task and GUI.",
        "replay_response_used": False,
        "descriptive_only": True,
    }


class MemoryBindingPublication(CurationPublication):
    """Exercise production span binding without reading or mutating a capsule."""

    def __init__(
        self,
        *,
        model_id: str,
        record_text: str,
        frozen_focal: dict[str, Any] | None = None,
    ) -> None:
        self.row = {
            "unit_id": _unit_id(1),
            "unit_kind": "STRICT_MHR",
            "model_id": model_id,
            "history_family": "flat_progress" if model_id == "qwen3vl_8b" else "raw_replay",
            "capsule_body_sha256": SHA256_ZERO,
        }
        self.record_id = "record-binding"
        self.capsule = {
            "runtime": {
                "non_history_envelope": {"current_screenshot": {"width": 100, "height": 200}},
                "treatment_surface": {
                    "source_records": [
                        {
                            "record_id": self.record_id,
                            "record_sha256": _sha256(record_text.encode()),
                            "exact_text": record_text,
                        }
                    ],
                    "target_exposures": (
                        []
                        if frozen_focal is None
                        else [{"focal_edit_spans": [deepcopy(frozen_focal)]}]
                    ),
                },
            },
            "curator_only": {
                "transformation": {"evidence_refs": [{"evidence_id": "evidence-" + "2" * 24}]}
            },
        }

    def _load_capsule(self, unit_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        assert unit_id == self.row["unit_id"]
        return self.row, self.capsule


def _write_reviewer_registry(
    path: Path,
    *,
    principals: tuple[tuple[str, str, str, str | None], ...] = PRINCIPALS,
    mode: int = 0o600,
) -> Path:
    value = {
        "schema_version": REVIEWER_REGISTRY_SCHEMA_VERSION,
        "principals": [
            {
                "principal_id": principal_id,
                "role": role,
                "access_secret": secret,
                "adjudication_channel": adjudication_channel,
            }
            for principal_id, role, secret, adjudication_channel in principals
        ],
    }
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    path.chmod(mode)
    return path


def _write_synthetic_g1_5_publication(repository: Path) -> Path:
    repository.mkdir(mode=0o700)

    def repo_reference(
        relative: str,
        data: bytes,
        *,
        digest_key: str = "sha256",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "path": relative,
            digest_key: _sha256(data),
            **(extra or {}),
        }

    def copy_frozen_repository_file(relative: str) -> None:
        source = REPOSITORY_ROOT / relative
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    frozen_publication = json.loads(
        (
            REPOSITORY_ROOT / "mobileworld_audit_handoff/g1_5/cpu_publication_manifest.v1.json"
        ).read_bytes()
    )
    preview_api = deepcopy(frozen_publication["preview_api"])
    copy_frozen_repository_file(preview_api["implementation"]["path"])
    copy_frozen_repository_file(preview_api["dependencies"]["human_diff_renderer"]["path"])
    copy_frozen_repository_file(preview_api["output_schema"]["path"])
    copy_frozen_repository_file(
        preview_api["pinned_tokenizers"][0]["model_config_manifest"]["path"]
    )

    arms = ["ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"]
    selected_codecs: list[dict[str, Any]] = []
    for history_family, codec_id in (
        ("flat_progress", "qwen-flat-progress-v1"),
        ("raw_replay", "mai-raw-replay-v1"),
    ):
        declaration = {
            "codec_id": codec_id,
            "contract_version": "v1",
            "history_family": history_family,
            "level": "VALIDITY_TRANSFORMATION",
            "supported_operations": ["DROP", "REPLACE"],
            "supported_arms": arms,
            "live_ready": False,
        }
        conformance = {
            "codec_id": codec_id,
            "codec_contract_version": "v1",
            "history_family": history_family,
            "capability_sha256": canonical_sha256(declaration),
            "checkpoint_scope": "CPU_ONLY",
            "provider_invocation_allowed": False,
            "provider_invocation_count": 0,
            "treatment_response_count": 0,
            "network_used": False,
            "gpu_used": False,
            "gui_action_executed": False,
            "arms": [
                {
                    "arm": arm,
                    "provider_invocation_allowed": False,
                    "target_only_diff": True,
                    "source_mapping_reversible": True,
                }
                for arm in arms
            ],
        }
        selected_codecs.append(
            {
                "codec_id": codec_id,
                "codec_contract_version": "v1",
                "history_family": history_family,
                "implementation": repo_reference(
                    f"codecs/{codec_id}.py", f"# {codec_id}\n".encode()
                ),
                "capability": {
                    "declaration": declaration,
                    "sha256": canonical_sha256(declaration),
                },
                "source_fixture": repo_reference(
                    f"fixtures/{codec_id}.json",
                    canonical_json_bytes({"codec_id": codec_id, "fixture": "CPU_ONLY"}),
                ),
                "conformance_receipt": repo_reference(
                    f"receipts/{codec_id}.json",
                    canonical_json_bytes(conformance),
                    digest_key="file_sha256",
                ),
            }
        )

    publication = {
        "schema_version": "mobileworld.g1.history-codec-cpu-publication/v1",
        "issue": "ALE-323",
        "story": "G1.5",
        "publication_scope": "SECRET_FREE_CPU_CONFORMANCE_ONLY",
        "status": "CPU_CHECKPOINT_IMPLEMENTED_LIVE_SMOKE_DEFERRED",
        "selected_codecs": selected_codecs,
        "preview_api": preview_api,
        "shared_bindings": {
            "history_ir_schema": repo_reference(
                "shared/history-ir.schema.json",
                canonical_json_bytes({"schema": "history-ir-v1"}),
            ),
            "renderer": repo_reference("shared/renderer.py", b"# deterministic CPU renderer\n"),
            "tokenizer_binding": repo_reference(
                "shared/tokenizer.json",
                canonical_json_bytes({"tokenizer_required": False}),
                extra={"tokenizer_required": False},
            ),
        },
        "safety": {
            "formal_g1_data": False,
            "live_smoke_completed": False,
            "provider_invocation_allowed": False,
            "provider_invocation_count": 0,
            "treatment_response_generation_allowed": False,
            "treatment_response_count": 0,
            "network_used": False,
            "gpu_used": False,
            "gui_action_executed": False,
        },
    }
    manifest = repository / "g1_5_cpu_publication.json"
    manifest.write_bytes(canonical_json_bytes(publication))
    return manifest


def _principal(principal_id: str) -> tuple[str, str, str, str | None]:
    return next(item for item in PRINCIPALS if item[0] == principal_id)


def _session(
    client: InProcessASGIClient,
    principal_id: str,
    *,
    origin: str = "http://127.0.0.1",
) -> tuple[str, dict[str, Any]]:
    reviewer_id, role, access_secret, _ = _principal(principal_id)
    response = client.post(
        "/api/session",
        json={
            "reviewer_id": reviewer_id,
            "role": role,
            "access_secret": access_secret,
        },
        headers={"origin": origin},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["csrf_token"], body


def _event_packet_bindings(
    store: AnnotationStore,
    *,
    unit_id: str,
    reviewer_id: str,
    reviewer_role: str,
    channel: str | None = None,
) -> dict[str, str]:
    effective_channel = channel or contracts_module.role_channel(reviewer_role)
    source = store.bind_source_packet(unit_id, effective_channel)
    assignment_id = store.assignment_id(
        unit_id,
        reviewer_role,
        channel=effective_channel if reviewer_role == "ADJUDICATOR" else None,
    )
    compared_review_event_ids = []
    if reviewer_role == "ADJUDICATOR":
        compared_review_event_ids = [
            event["event_id"]
            for event in store.read_events()
            if event["event_kind"] == "REVIEW_SUBMITTED"
            and event["unit_id"] == unit_id
            and event["channel"] == effective_channel
        ]
    source_packet = (
        store.publication.consistency_packet(unit_id)
        if effective_channel == "CONSISTENCY_AUDIT"
        else store.publication.packet(unit_id, effective_channel)
    )
    assignment_packet = _browser_packet(
        source_packet,
        assignment_id=assignment_id,
        role=reviewer_role,
        reviewer_identity_sha256=store.identity_commitment(reviewer_id),
        source_binding=source,
        compared_review_event_ids=compared_review_event_ids,
    )
    return {
        "assignment_id": assignment_id,
        "source_packet_sha256": source["source_packet_sha256"],
        "assignment_packet_sha256": store.bind_assignment_packet(assignment_packet),
    }


def _save_draft(
    store: AnnotationStore,
    *,
    unit_id: str,
    reviewer_id: str,
    reviewer_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return store.save_draft(
        unit_id=unit_id,
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
        payload=payload,
        **_event_packet_bindings(
            store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
        ),
    )


def _submit_review(
    store: AnnotationStore,
    *,
    unit_id: str,
    reviewer_id: str,
    reviewer_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return store.submit_review(
        unit_id=unit_id,
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
        payload=payload,
        **_event_packet_bindings(
            store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
        ),
    )


def _submit_adjudication(
    store: AnnotationStore,
    *,
    unit_id: str,
    channel: str,
    reviewer_id: str,
    resolved_payload: dict[str, Any],
    rationale: str,
) -> dict[str, Any]:
    return store.submit_adjudication(
        unit_id=unit_id,
        channel=channel,
        reviewer_id=reviewer_id,
        resolved_payload=resolved_payload,
        rationale=rationale,
        **_event_packet_bindings(
            store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role="ADJUDICATOR",
            channel=channel,
        ),
    )


@pytest.fixture
def fake_publication() -> FakePublication:
    return FakePublication()


@pytest.fixture
def reviewer_registry(tmp_path: Path) -> ReviewerRegistry:
    return ReviewerRegistry.load(_write_reviewer_registry(tmp_path / "reviewers.json"))


def _write_solo_registry(path: Path) -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": SOLO_REGISTRY_SCHEMA_VERSION,
                "principal": {
                    "principal_id": "one-real-curator",
                    "access_secret": "solo-curator-secret-0001",
                },
            }
        )
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def annotation_store(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> AnnotationStore:
    return AnnotationStore(
        tmp_path / "annotation-state",
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
    )


@pytest.fixture
def open_annotation_store(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> AnnotationStore:
    synthetic_repository = tmp_path / "synthetic-g1-5-repository"
    manifest = _write_synthetic_g1_5_publication(synthetic_repository)
    receipt = write_codec_gate_receipt(
        manifest,
        tmp_path / "codec-gate",
        repository_root=synthetic_repository,
    )
    return AnnotationStore(
        tmp_path / "open-annotation-state",
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
        repository_root=synthetic_repository,
        codec_gate_receipt_path=receipt,
        g1_5_publication_manifest_path=manifest,
    )


def _open_solo_store(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> SoloFirstPassStore:
    synthetic_repository = tmp_path / "solo-synthetic-g1-5-repository"
    manifest = _write_synthetic_g1_5_publication(synthetic_repository)
    receipt = write_codec_gate_receipt(
        manifest,
        tmp_path / "solo-codec-gate",
        repository_root=synthetic_repository,
    )
    registry = SoloCuratorRegistry.load(_write_solo_registry(tmp_path / "solo-curator.json"))
    return SoloFirstPassStore(
        tmp_path / "solo-first-pass-state",
        fake_publication,  # type: ignore[arg-type]
        registry,
        repository_root=synthetic_repository,
        codec_gate_receipt_path=receipt,
        g1_5_publication_manifest_path=manifest,
    )


@pytest.fixture(scope="module")
def active_publication() -> CurationPublication:
    if not ACTIVE_G1_3_PUBLICATION.is_dir():
        pytest.skip("active repo-external G1.3 publication is unavailable")
    return CurationPublication()


def test_active_publication_has_exact_190_unit_accounting_and_is_read_only(
    active_publication: CurationPublication,
) -> None:
    manifest_path = ACTIVE_G1_3_PUBLICATION / "capsule_manifest.json"
    index_path = ACTIVE_G1_3_PUBLICATION / "capsule_index.jsonl"
    before = (_sha256(manifest_path.read_bytes()), _sha256(index_path.read_bytes()))

    rows = active_publication.list_units()
    assert len(rows) == len({item["unit_id"] for item in rows}) == 190
    assert Counter((item["unit_kind"], item["model_id"]) for item in rows) == {
        ("STRICT_MHR", "qwen3vl_8b"): 139,
        ("STRICT_MHR", "mai_ui_8b"): 13,
        ("CLEAN_CONTROL", "qwen3vl_8b"): 30,
        ("CLEAN_CONTROL", "mai_ui_8b"): 8,
    }
    assert Counter(item["history_family"] for item in rows) == {
        "flat_progress": 169,
        "raw_replay": 21,
    }
    registry_lock = json.loads(REGISTRY_LOCK.read_bytes())
    assert registry_lock["counts"]["strict_mhr_case_count"] == 152
    assert registry_lock["counts"]["clean_control_selected_count"] == 38
    assert registry_lock["counts"]["clean_control_reserve_count"] == 38
    assert registry_lock["counts"]["included_count"] == 0
    assert before == (_sha256(manifest_path.read_bytes()), _sha256(index_path.read_bytes()))


def test_publication_rejects_index_bytes_or_metadata_not_bound_by_pinned_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / ACTIVE_G1_3_MANIFEST_SHA256
    root.mkdir()
    manifest_bytes = (ACTIVE_G1_3_PUBLICATION / "capsule_manifest.json").read_bytes()
    index_rows = [
        json.loads(line)
        for line in (ACTIVE_G1_3_PUBLICATION / "capsule_index.jsonl").read_bytes().splitlines()
    ]
    (root / "capsule_manifest.json").write_bytes(manifest_bytes)
    index_rows[0]["model_id"] = (
        "qwen3vl_8b" if index_rows[0]["model_id"] == "mai_ui_8b" else "mai_ui_8b"
    )
    index_rows[0]["history_family"] = (
        "flat_progress" if index_rows[0]["history_family"] == "raw_replay" else "raw_replay"
    )
    (root / "capsule_index.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in index_rows)
    )
    with pytest.raises(CurationError) as exc:
        CurationPublication(root)
    assert exc.value.code == "PUBLICATION_INDEX_INVALID"


@pytest.mark.parametrize("history_family", ["flat_progress", "raw_replay"])
def test_real_channel_packets_are_deterministic_and_visibility_isolated(
    active_publication: CurationPublication, history_family: str
) -> None:
    unit = next(
        item
        for item in active_publication.list_units()
        if item["history_family"] == history_family and item["unit_kind"] == "STRICT_MHR"
    )
    action = active_publication.packet(unit["unit_id"], "ACTION_GOLD")
    transformation = active_publication.packet(unit["unit_id"], "TRANSFORMATION")
    assert action == active_publication.packet(unit["unit_id"], "ACTION_GOLD")
    assert transformation == active_publication.packet(unit["unit_id"], "TRANSFORMATION")

    assert {item["evidence_role"] for item in action["evidence"]} <= {
        "task_instruction",
        "target_pre",
        "tool_response",
        "ask_user_response",
    }
    assert action["visibility"] == {
        "history_visible": False,
        "natural_target_output_visible": False,
        "target_post_visible": False,
        "later_trajectory_visible": False,
        "outcome_visible": False,
        "replay_response_visible": False,
    }
    assert "source_records" not in action
    assert "target_candidates" not in action
    assert "source_history" in {item["evidence_role"] for item in transformation["evidence"]}
    assert transformation["visibility"]["history_visible"] is True
    assert all(
        transformation["visibility"][key] is False
        for key in (
            "natural_target_output_visible",
            "target_post_visible",
            "later_trajectory_visible",
            "outcome_visible",
            "replay_response_visible",
        )
    )
    expected = "G1_1_FROZEN_EXACT" if history_family == "flat_progress" else "G1_6_REVIEW_REQUIRED"
    assert transformation["target_candidate_status"] == expected

    forbidden = {
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

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(child) for child in value), set())
        return set()

    assert keys(action).isdisjoint(forbidden)
    assert keys(transformation).isdisjoint(forbidden)


def test_schema_documents_are_valid_and_runtime_versions_are_pinned() -> None:
    schemas = {
        path.name: json.loads(path.read_bytes())
        for path in sorted(G16_SCHEMA_ROOT.glob("*.schema.json"))
    }
    assert set(schemas) == {
        "annotation_event.schema.json",
        "annotation_workspace.schema.json",
        "browser_transformation_preview.schema.json",
        "curator_packet.schema.json",
        "review_proposal.schema.json",
        "solo_annotation_event.schema.json",
        "solo_annotation_workspace.schema.json",
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("https://agentsentinel.local/schemas/g1_6/")

    assert (
        schemas["annotation_event.schema.json"]["properties"]["schema_version"]["const"]
        == ANNOTATION_EVENT_SCHEMA_VERSION
    )
    assert (
        schemas["annotation_event.schema.json"]["properties"]["proposal_schema_version"]["const"]
        == REVIEW_PROPOSAL_SCHEMA_VERSION
    )
    assert (
        schemas["annotation_workspace.schema.json"]["properties"]["schema_version"]["const"]
        == WORKSPACE_MANIFEST_SCHEMA_VERSION
    )
    assert (
        schemas["solo_annotation_event.schema.json"]["properties"]["schema_version"]["const"]
        == SOLO_EVENT_SCHEMA_VERSION
    )
    assert (
        schemas["solo_annotation_workspace.schema.json"]["properties"]["schema_version"]["const"]
        == SOLO_WORKSPACE_SCHEMA_VERSION
    )
    assert (
        schemas["curator_packet.schema.json"]["properties"]["schema_version"]["const"]
        == REVIEW_PACKET_SCHEMA_VERSION
    )
    assert WORKSPACE_PROTOCOL_VERSION == "mobileworld.g1.gold-curation-workspace/protocol-v1"

    proposal_validator = Draft202012Validator(schemas["review_proposal.schema.json"])
    proposal_validator.validate(_action_payload())
    proposal_validator.validate(_transformation_payload())
    proposal_validator.validate(_consistency_payload())


def test_browser_transformation_preview_is_schema_exact_and_assignment_scoped() -> None:
    schema = json.loads(
        (G16_SCHEMA_ROOT / "browser_transformation_preview.schema.json").read_bytes()
    )
    validator = Draft202012Validator(schema)
    assignment_a = "g1assignment-" + "a" * 32
    assignment_b = "g1assignment-" + "b" * 32

    strict_internal = _internal_transformation_preview()
    strict_a = _browser_transformation_preview(
        deepcopy(strict_internal), assignment_id=assignment_a
    )
    assert strict_a == _browser_transformation_preview(
        deepcopy(strict_internal), assignment_id=assignment_a
    )
    validator.validate(strict_a)
    assert strict_a["plan_set_profile"] == "G1_STRICT_MHR"
    assert [item["arm"] for item in strict_a["arms"]] == [
        "ORIGINAL",
        "MASK",
        "MASK_CORRECTION",
        "ORACLE_CLEAN",
        "SHAM_BENIGN_EDIT",
    ]
    assert strict_a["acceptance_ready"] is True

    clean = _browser_transformation_preview(
        _internal_transformation_preview(clean_control=True),
        assignment_id=assignment_a,
    )
    validator.validate(clean)
    assert clean["plan_set_profile"] == "G1_CLEAN_CONTROL"
    assert clean["correction_ranking"] is None
    assert clean["correction_anchors"] == []
    assert [item["arm"] for item in clean["arms"]] == [
        "ORIGINAL",
        "SHAM_BENIGN_EDIT",
    ]

    serialized = canonical_json_bytes(strict_a).decode("utf-8")
    for forbidden_key_or_value in (
        "codec_id",
        "tokenizer",
        "source_request",
        "history_ir",
        "transformation_plan",
        "rendered_request_sha256",
        "validation_receipt_sha256",
        "binding_id",
        "target_record_id",
        "record_id",
        "operation_id",
        "repair_id",
        "container_path",
        "internal-binding-stable-id",
        "internal-record-stable-id",
        "internal-operation-",
        "internal-repair-stable-id",
        "payload.request_view.messages[7]",
        "RAW-HUMAN-DIFF-STABLE-ID-MARKER",
        "Qwen/CONFIDENTIAL-TOKENIZER",
    ):
        assert forbidden_key_or_value not in serialized

    def opaque_tokens(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(opaque_tokens(item) for item in value.values()), set())
        if isinstance(value, list):
            return set().union(*(opaque_tokens(item) for item in value), set())
        if isinstance(value, str) and re.fullmatch(
            r"(?:binding|record|container|operation|repair)-[0-9a-f]{24}", value
        ):
            return {value}
        return set()

    strict_b = _browser_transformation_preview(
        deepcopy(strict_internal), assignment_id=assignment_b
    )
    tokens_a = opaque_tokens(strict_a)
    tokens_b = opaque_tokens(strict_b)
    assert tokens_a
    assert tokens_b
    assert tokens_a.isdisjoint(tokens_b)
    assert strict_a["preview_receipt_sha256"] == strict_b["preview_receipt_sha256"]


def test_browser_transformation_preview_fails_closed_and_sham_blocks_acceptance() -> None:
    assignment_id = "g1assignment-" + "c" * 32
    schema = json.loads(
        (G16_SCHEMA_ROOT / "browser_transformation_preview.schema.json").read_bytes()
    )
    validator = Draft202012Validator(schema)

    sham_mismatch = _browser_transformation_preview(
        _internal_transformation_preview(sham_matched=False),
        assignment_id=assignment_id,
    )
    validator.validate(sham_mismatch)
    assert sham_mismatch["sham_token_match"]["matched"] is False
    assert sham_mismatch["acceptance_ready"] is False

    malformed_previews: list[dict[str, Any]] = []
    provider_enabled = _internal_transformation_preview()
    provider_enabled["provider_invocation_allowed"] = True
    malformed_previews.append(provider_enabled)

    provider_count = _internal_transformation_preview()
    provider_count["provider_invocation_count"] = 1
    malformed_previews.append(provider_count)

    non_target_diff = _internal_transformation_preview()
    non_target_diff["arms"][1]["target_only_diff"] = False
    malformed_previews.append(non_target_diff)

    irreversible = _internal_transformation_preview()
    irreversible["arms"][2]["source_mapping_reversible"] = False
    malformed_previews.append(irreversible)

    arm_provider_enabled = _internal_transformation_preview()
    arm_provider_enabled["arms"][3]["provider_invocation_allowed"] = True
    malformed_previews.append(arm_provider_enabled)

    reordered = _internal_transformation_preview()
    reordered["arms"][0], reordered["arms"][1] = reordered["arms"][1], reordered["arms"][0]
    malformed_previews.append(reordered)

    for malformed in malformed_previews:
        with pytest.raises(CurationError):
            _browser_transformation_preview(malformed, assignment_id=assignment_id)


def test_runtime_option_catalog_is_closed_and_all_capabilities_fail_closed() -> None:
    catalog = option_catalog()
    assert set(catalog) == {
        "protocol_version",
        "channels",
        "roles",
        "dispositions",
        "exclusion_reasons",
        "predicate_kinds",
        "action_types",
        "point_action_types",
        "text_action_types",
        "direction_action_types",
        "directions",
        "coordinate_tolerance_modes",
        "consistency_labels",
        "safety",
    }
    assert catalog["channels"] == ["ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT"]
    assert catalog["safety"] == {
        "local_loopback_only": True,
        "external_network_allowed": False,
        "provider_invocation_allowed": False,
        "gpu_allowed": False,
        "model_loading_allowed": False,
        "formal_replay_allowed": False,
        "gui_action_execution_allowed": False,
        "treatment_response_generation_allowed": False,
    }


def test_missing_codec_gate_allows_draft_but_blocks_every_formal_output(
    annotation_store: AnnotationStore,
) -> None:
    assert annotation_store.formal_annotation_open is False
    unit_id = _unit_id(9)
    draft = _save_draft(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    assert draft["event_kind"] == "DRAFT_SAVED"
    assert draft["codec_gate_receipt_sha256"] is None

    with pytest.raises(CurationError) as exc:
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(),
        )
    assert exc.value.code == "CODEC_GATE_NOT_OPEN"

    with pytest.raises(CurationError) as exc:
        annotation_store.submit_adjudication(
            unit_id=unit_id,
            channel="ACTION_GOLD",
            reviewer_id="third-adjudicator",
            assignment_id=annotation_store.assignment_id(
                unit_id, "ADJUDICATOR", channel="ACTION_GOLD"
            ),
            source_packet_sha256=SHA256_ZERO,
            assignment_packet_sha256=SHA256_ZERO,
            resolved_payload=_action_payload(),
            rationale="The gate must be checked before adjudication can finalize.",
        )
    assert exc.value.code == "CODEC_GATE_NOT_OPEN"

    with pytest.raises(CurationError) as exc:
        annotation_store.export_workspace_receipt()
    assert exc.value.code == "CODEC_GATE_NOT_OPEN"


def test_verified_codec_gate_opens_effective_finalization_without_mutating_manifest_flag(
    open_annotation_store: AnnotationStore,
) -> None:
    assert open_annotation_store.formal_annotation_open is True
    assert open_annotation_store.codec_gate_receipt is not None
    assert open_annotation_store.codec_gate_receipt["checks"] == {
        "selected_codec_count": 2,
        "codec_ids_distinct": True,
        "capabilities_sufficient": True,
        "conformance_receipts_valid": True,
        "fixture_only": False,
        "cpu_only": True,
        "provider_client_created": False,
        "provider_invoked": False,
        "external_network_used": False,
        "gpu_probed": False,
        "gpu_used": False,
        "model_loaded": False,
        "replay_executed": False,
    }
    manifest = json.loads((open_annotation_store.root / "workspace-manifest.json").read_bytes())
    assert manifest["readiness"]["formal_annotation_open"] is False
    assert manifest["readiness"]["codec_publication_required_for_formal_annotation"] is True


def test_codec_gate_is_revalidated_on_read_append_and_export(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    repository = tmp_path / "runtime-revalidation-repository"
    manifest = _write_synthetic_g1_5_publication(repository)
    receipt = write_codec_gate_receipt(
        manifest,
        tmp_path / "runtime-revalidation-gate",
        repository_root=repository,
    )
    store = AnnotationStore(
        tmp_path / "runtime-revalidation-state",
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
        repository_root=repository,
        codec_gate_receipt_path=receipt,
        g1_5_publication_manifest_path=manifest,
    )
    event = _submit_review(
        store,
        unit_id=_unit_id(12),
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    assert store.codec_gate_receipt is not None
    expected_gate_sha256 = store.codec_gate_receipt["receipt_sha256"]
    assert event["codec_gate_receipt_sha256"] == expected_gate_sha256
    assert store.export_workspace_receipt()["codec_gate_receipt_sha256"] == expected_gate_sha256

    implementation = repository / "codecs/qwen-flat-progress-v1.py"
    implementation.write_bytes(b"# changed after workspace bootstrap\n")
    for operation in (
        store.read_events,
        lambda: _submit_review(
            store,
            unit_id=_unit_id(13),
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(),
        ),
        store.export_workspace_receipt,
    ):
        with pytest.raises(CurationError) as exc:
            operation()
        assert exc.value.code == "CODEC_GATE_INVALID"


def test_workspace_rejects_a_different_valid_codec_gate_after_formal_event(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    repository_a = tmp_path / "codec-repository-a"
    manifest_a = _write_synthetic_g1_5_publication(repository_a)
    receipt_a = write_codec_gate_receipt(
        manifest_a,
        tmp_path / "codec-gate-a",
        repository_root=repository_a,
    )
    workspace = tmp_path / "codec-bound-state"
    store_a = AnnotationStore(
        workspace,
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
        repository_root=repository_a,
        codec_gate_receipt_path=receipt_a,
        g1_5_publication_manifest_path=manifest_a,
    )
    event = _submit_review(
        store_a,
        unit_id=_unit_id(14),
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )

    repository_b = tmp_path / "codec-repository-b"
    manifest_b = _write_synthetic_g1_5_publication(repository_b)
    publication_b = json.loads(manifest_b.read_bytes())
    qwen_binding = next(
        item
        for item in publication_b["selected_codecs"]
        if item["history_family"] == "flat_progress"
    )
    implementation_b = repository_b / qwen_binding["implementation"]["path"]
    implementation_bytes_b = b"# independently verified alternate qwen implementation\n"
    implementation_b.write_bytes(implementation_bytes_b)
    qwen_binding["implementation"]["sha256"] = _sha256(implementation_bytes_b)
    manifest_b.write_bytes(canonical_json_bytes(publication_b))
    receipt_b = write_codec_gate_receipt(
        manifest_b,
        tmp_path / "codec-gate-b",
        repository_root=repository_b,
    )
    store_b = AnnotationStore(
        workspace,
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
        repository_root=repository_b,
        codec_gate_receipt_path=receipt_b,
        g1_5_publication_manifest_path=manifest_b,
    )
    assert store_b.codec_gate_receipt is not None
    assert event["codec_gate_receipt_sha256"] != store_b.codec_gate_receipt["receipt_sha256"]
    with pytest.raises(CurationError) as exc:
        store_b.read_events()
    assert exc.value.code == "ANNOTATION_LEDGER_INVALID"


def test_codec_gate_rejects_tamper_symlink_and_referenced_file_hash_mismatch(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    repository = tmp_path / "synthetic-codec-repository"
    manifest = _write_synthetic_g1_5_publication(repository)

    tampered_receipt = write_codec_gate_receipt(
        manifest,
        tmp_path / "tampered-gate",
        repository_root=repository,
    )
    tampered = json.loads(tampered_receipt.read_bytes())
    tampered["checks"]["cpu_only"] = False
    tampered_receipt.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            tmp_path / "tampered-gate-state",
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
            repository_root=repository,
            codec_gate_receipt_path=tampered_receipt,
            g1_5_publication_manifest_path=manifest,
        )
    assert exc.value.code == "CODEC_GATE_INVALID"

    valid_receipt = write_codec_gate_receipt(
        manifest,
        tmp_path / "valid-gate",
        repository_root=repository,
    )
    linked_receipt = tmp_path / "linked-codec-gate.json"
    linked_receipt.symlink_to(valid_receipt)
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            tmp_path / "linked-gate-state",
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
            repository_root=repository,
            codec_gate_receipt_path=linked_receipt,
            g1_5_publication_manifest_path=manifest,
        )
    assert exc.value.code == "CODEC_GATE_INVALID"

    implementation = repository / "codecs/qwen-flat-progress-v1.py"
    implementation.write_bytes(b"# tampered codec implementation\n")
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            tmp_path / "tampered-reference-state",
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
            repository_root=repository,
            codec_gate_receipt_path=valid_receipt,
            g1_5_publication_manifest_path=manifest,
        )
    assert exc.value.code == "CODEC_GATE_INVALID"


def test_codec_gate_rejects_non_object_or_incomplete_arm_conformance(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "malformed-conformance-repository"
    manifest_path = _write_synthetic_g1_5_publication(repository)
    manifest = json.loads(manifest_path.read_bytes())
    codec = manifest["selected_codecs"][0]
    receipt_path = repository / codec["conformance_receipt"]["path"]
    receipt = json.loads(receipt_path.read_bytes())
    receipt["arms"] = ["not-a-closed-arm-record"] * 5
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_path.write_bytes(receipt_bytes)
    codec["conformance_receipt"]["file_sha256"] = _sha256(receipt_bytes)
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(CurationError) as exc:
        write_codec_gate_receipt(
            manifest_path,
            tmp_path / "malformed-conformance-gate",
            repository_root=repository,
        )
    assert exc.value.code == "CODEC_GATE_INVALID"


def test_qwen_requires_exact_frozen_flat_progress_span() -> None:
    text = "prefix🙂错误事实 suffix harmless"
    start = text.index("错误事实")
    end = start + len("错误事实")
    frozen = _span("record-binding", text, start, end)
    publication = MemoryBindingPublication(
        model_id="qwen3vl_8b", record_text=text, frozen_focal=frozen
    )
    payload = _binding_only_transformation_payload(
        record_id=publication.record_id,
        record_text=text,
        focal_start=start,
        focal_end=end,
    )
    publication.validate_review_payload_binding(
        publication.row["unit_id"], "TRANSFORMATION", payload
    )

    changed = deepcopy(payload)
    changed["focal_target_spans"] = [_span(publication.record_id, text, start - 1, end)]
    changed["oracle_target_spans"] = deepcopy(changed["focal_target_spans"])
    with pytest.raises(CurationError, match="Qwen focal target") as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"], "TRANSFORMATION", changed
        )
    assert exc.value.code == "TARGET_SPAN_UNRESOLVED"


def test_delimiter_repairs_mirror_formal_whitelist_and_causal_empty_rules() -> None:
    text = "Step 1: misleading; harmless"
    focal_start = text.index("misleading")
    focal_end = focal_start + len("misleading")
    frozen = _span("record-binding", text, focal_start, focal_end)
    publication = MemoryBindingPublication(
        model_id="qwen3vl_8b", record_text=text, frozen_focal=frozen
    )
    payload = _transformation_payload(
        record_id=publication.record_id,
        record_text=text,
        focal_start=focal_start,
        focal_end=focal_end,
    )
    marker = _span(publication.record_id, text, 0, focal_start)
    separator = _span(
        publication.record_id,
        text,
        focal_end,
        text.index("harmless"),
    )
    payload["delimiter_repairs"] = [
        {
            "arm": "MASK",
            "operation": "DELETE_ORPHAN_SEPARATOR",
            "deleted_syntax_span": marker,
            "rationale": "The Step marker is empty only after the focal deletion.",
            "human_selected": True,
        },
        {
            "arm": "MASK",
            "operation": "DELETE_ORPHAN_SEPARATOR",
            "deleted_syntax_span": separator,
            "rationale": "The adjacent separator is orphaned by the focal deletion.",
            "human_selected": True,
        },
    ]
    validate_review_payload("TRANSFORMATION", payload, clean_control=False)
    payload["preview_receipt_sha256"] = None
    publication.validate_review_payload_binding(
        publication.row["unit_id"], "TRANSFORMATION", payload
    )

    arbitrary_word = deepcopy(payload)
    arbitrary_word["delimiter_repairs"] = [
        {
            **arbitrary_word["delimiter_repairs"][0],
            "deleted_syntax_span": _span(publication.record_id, text, focal_start, focal_end),
        }
    ]
    with pytest.raises(CurationError) as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"], "TRANSFORMATION", arbitrary_word
        )
    assert exc.value.code == "DELIMITER_REPAIR_INVALID"

    nonempty_correction = deepcopy(payload)
    for repair in nonempty_correction["delimiter_repairs"]:
        repair["arm"] = "MASK_CORRECTION"
    with pytest.raises(CurationError) as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"], "TRANSFORMATION", nonempty_correction
        )
    assert exc.value.code == "DELIMITER_REPAIR_INVALID"


def test_mai_uses_unicode_codepoint_offsets_and_protects_tool_call_bytes() -> None:
    text = '前🙂 premise <tool_call>{"action":"click"}</tool_call> harmless tail'
    start = text.index("premise")
    end = start + len("premise")
    byte_start = len(text[:start].encode("utf-8"))
    byte_end = len(text[:end].encode("utf-8"))
    assert byte_start != start
    assert text.encode("utf-8")[byte_start:byte_end].decode("utf-8") == text[start:end]

    publication = MemoryBindingPublication(model_id="mai_ui_8b", record_text=text)
    payload = _transformation_payload(
        record_id=publication.record_id,
        record_text=text,
        focal_start=start,
        focal_end=end,
    )
    protected_start = text.index("<tool_call>")
    protected_end = text.index("</tool_call>") + len("</tool_call>")
    payload["history_family"] = "raw_replay"
    payload["protected_spans"] = [
        _span(publication.record_id, text, protected_start, protected_end)
    ]
    validate_review_payload("TRANSFORMATION", payload, clean_control=False)
    payload["preview_receipt_sha256"] = None
    publication.validate_review_payload_binding(
        publication.row["unit_id"], "TRANSFORMATION", payload
    )

    wrong_utf8_offsets = deepcopy(payload)
    for key in ("focal_target_spans", "oracle_target_spans"):
        wrong_utf8_offsets[key][0]["utf8_byte_start"] += 1
    with pytest.raises(CurationError) as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"], "TRANSFORMATION", wrong_utf8_offsets
        )
    assert exc.value.code == "TARGET_SPAN_UNRESOLVED"

    byte_offsets_misused_as_codepoints = deepcopy(payload)
    for key in ("focal_target_spans", "oracle_target_spans"):
        byte_offsets_misused_as_codepoints[key] = [
            {
                **deepcopy(payload[key][0]),
                "char_start": byte_start,
                "char_end": byte_end,
            }
        ]
    with pytest.raises(CurationError) as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"],
            "TRANSFORMATION",
            byte_offsets_misused_as_codepoints,
        )
    assert exc.value.code == "TARGET_SPAN_UNRESOLVED"

    protected = deepcopy(payload)
    protected["focal_target_spans"] = [
        _span(publication.record_id, text, protected_start, protected_end)
    ]
    protected["oracle_target_spans"] = deepcopy(protected["focal_target_spans"])
    with pytest.raises(CurationError) as exc:
        publication.validate_review_payload_binding(
            publication.row["unit_id"], "TRANSFORMATION", protected
        )
    assert exc.value.code == "TARGET_SPAN_PROTECTED"


def test_review_payload_validation_is_closed_and_never_accepts_automatic_fields() -> None:
    assert validate_review_payload("ACTION_GOLD", _action_payload(), clean_control=False)
    injected = {**_action_payload(), "model_recommendation": "click the control"}
    with pytest.raises(CurationError) as exc:
        validate_review_payload("ACTION_GOLD", injected, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"

    auto_span = _transformation_payload()
    auto_span["focal_target_spans"][0]["automatic_semantic_inference"] = True
    with pytest.raises(CurationError) as exc:
        validate_review_payload("TRANSFORMATION", auto_span, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"

    invalid_direction = _action_payload()
    invalid_direction["predicates"] = [
        {
            "predicate_kind": "DIRECTION_SET",
            "action_type": "scroll",
            "allowed_directions": ["any"],
            "evidence_ids": ["evidence-" + "1" * 24],
            "rationale": "An unconstrained direction is not a closed action predicate.",
            "human_selected": True,
        }
    ]
    with pytest.raises(CurationError) as exc:
        validate_review_payload("ACTION_GOLD", invalid_direction, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"
    proposal_schema = json.loads((G16_SCHEMA_ROOT / "review_proposal.schema.json").read_bytes())
    assert list(Draft202012Validator(proposal_schema).iter_errors(invalid_direction))


def test_correction_candidate_and_preview_guards_match_runtime_and_schema() -> None:
    proposal_schema = json.loads((G16_SCHEMA_ROOT / "review_proposal.schema.json").read_bytes())
    proposal_validator = Draft202012Validator(proposal_schema)
    base = _transformation_payload()
    validate_review_payload("TRANSFORMATION", base, clean_control=False)
    proposal_validator.validate(base)

    invalid_payloads: list[tuple[str, dict[str, Any]]] = []

    missing_candidates = deepcopy(base)
    missing_candidates.pop("correction_candidates")
    invalid_payloads.append(("missing correction candidates", missing_candidates))

    empty_candidates = deepcopy(base)
    empty_candidates["correction_candidates"] = []
    invalid_payloads.append(("strict case with no correction candidate", empty_candidates))

    automatic_candidate = deepcopy(base)
    automatic_candidate["correction_candidates"][0]["human_authored"] = False
    invalid_payloads.append(("candidate not human authored", automatic_candidate))

    open_candidate = deepcopy(base)
    open_candidate["correction_candidates"][0]["model_generated"] = True
    invalid_payloads.append(("candidate with an automatic field", open_candidate))

    duplicate_candidates = deepcopy(base)
    duplicate_candidates["correction_candidates"].append(
        deepcopy(duplicate_candidates["correction_candidates"][0])
    )
    invalid_payloads.append(("duplicate candidate text", duplicate_candidates))

    unselected_correction = deepcopy(base)
    unselected_correction["correction_text"] = "A correction not authored as a candidate."
    invalid_payloads.append(("selected correction absent from candidates", unselected_correction))

    missing_preview_receipt = deepcopy(base)
    missing_preview_receipt.pop("preview_receipt_sha256")
    invalid_payloads.append(("missing preview receipt", missing_preview_receipt))

    null_preview_receipt = deepcopy(base)
    null_preview_receipt["preview_receipt_sha256"] = None
    invalid_payloads.append(("null accepted preview receipt", null_preview_receipt))

    noncanonical_preview_receipt = deepcopy(base)
    noncanonical_preview_receipt["preview_receipt_sha256"] = "A" * 64
    invalid_payloads.append(("noncanonical preview receipt", noncanonical_preview_receipt))

    unconfirmed_preview = deepcopy(base)
    unconfirmed_preview["preview_human_confirmed"] = False
    invalid_payloads.append(("preview not human confirmed", unconfirmed_preview))

    for label, payload in invalid_payloads:
        with pytest.raises(CurationError) as exc:
            validate_review_payload("TRANSFORMATION", payload, clean_control=False)
        assert exc.value.code == "PROPOSAL_INVALID", label
        # JSON Schema cannot express that one scalar must equal a member object's text;
        # the closed runtime validator enforces that cross-field membership invariant.
        if label != "selected correction absent from candidates":
            assert list(proposal_validator.iter_errors(payload)), label

    excluded = deepcopy(base)
    excluded.update(
        {
            "disposition": "EXCLUDE",
            "exclusion_reason": "NO_VALID_CORRECTION",
            "focal_target_spans": [],
            "oracle_target_spans": [],
            "correction_candidates": [],
            "correction_text": "",
            "correction_evidence_ids": [],
            "correction_is_minimal_fact": False,
            "correction_contains_no_advice": False,
            "oracle_preserves_non_target_history": False,
            "protected_spans": [],
            "delimiter_repairs": [],
            "sham_span": None,
            "sham_match_checks": None,
            "preview_receipt_sha256": None,
            "preview_human_confirmed": False,
        }
    )
    validate_review_payload("TRANSFORMATION", excluded, clean_control=False)
    proposal_validator.validate(excluded)

    excluded_with_preview = deepcopy(excluded)
    excluded_with_preview["preview_receipt_sha256"] = "9" * 64
    excluded_with_preview["preview_human_confirmed"] = True
    with pytest.raises(CurationError) as exc:
        validate_review_payload("TRANSFORMATION", excluded_with_preview, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"
    assert list(proposal_validator.iter_errors(excluded_with_preview))


def test_exact_normalized_action_uses_the_pinned_production_wrapper() -> None:
    assert (
        validate_review_payload("ACTION_GOLD", _exact_action_payload(), clean_control=False)
        == _exact_action_payload()
    )
    finished_without_text = _exact_action_payload()
    finished_without_text["predicates"][0]["action_type"] = "finished"
    finished_without_text["predicates"][0]["normalized_action"] = _normalized_action("finished")
    validate_review_payload("ACTION_GOLD", finished_without_text, clean_control=False)
    for invalid in (
        {"action_type": "wait"},
        {**_normalized_action(), "unknown": "silently accepted"},
        {**_normalized_action(), "serializer_version": "2.11.8"},
        {
            **_normalized_action(),
            "value": {**_normalized_action()["value"], "action_type": "click"},
        },
        {
            **_normalized_action("keyboard_enter"),
            "value": {
                **_normalized_action("keyboard_enter")["value"],
                "keycode": "ENTER",
            },
        },
        {
            **_normalized_action("click"),
            "value": {
                **_normalized_action("click")["value"],
                "index": 1,
                "x": 2,
                "y": 3,
            },
        },
    ):
        payload = _exact_action_payload()
        payload["predicates"][0]["normalized_action"] = invalid
        with pytest.raises(CurationError) as exc:
            validate_review_payload("ACTION_GOLD", payload, clean_control=False)
        assert exc.value.code == "PROPOSAL_INVALID"


def test_action_tolerance_projection_preserves_predicate_association() -> None:
    primary = _action_payload(x_min=4)
    second = _action_payload(x_min=40)["predicates"][0]
    second["tolerance_px"] = 20
    primary["predicates"].append(second)
    secondary = deepcopy(primary)
    secondary["predicates"][0]["tolerance_px"] = 20
    secondary["predicates"][1]["tolerance_px"] = 2
    assert contracts_module.disagreement_fields("ACTION_GOLD", primary, secondary) == [
        "ACTION_TOLERANCE"
    ]


def test_action_set_rejects_material_duplicate_predicates_regions_and_non_nfc_text() -> None:
    multiline = _action_payload()
    multiline["predicates"] = [
        {
            "predicate_kind": "TEXT_VARIANTS",
            "action_type": "input_text",
            "field": "text",
            "unicode_normalization": "NFC",
            "case_sensitive": True,
            "allowed_values": ["line 1\nline 2"],
            "evidence_ids": ["evidence-" + "1" * 24],
            "rationale": "A newline inside one exact value remains one accepted variant.",
            "human_selected": True,
        }
    ]
    validate_review_payload("ACTION_GOLD", multiline, clean_control=False)

    duplicate_predicate = _action_payload()
    second = deepcopy(duplicate_predicate["predicates"][0])
    second["rationale"] = "A different rationale cannot make the same material predicate unique."
    duplicate_predicate["predicates"].append(second)
    with pytest.raises(CurationError) as exc:
        validate_review_payload("ACTION_GOLD", duplicate_predicate, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"

    duplicate_polygon = _action_payload()
    duplicate_polygon["predicates"][0]["regions"] = [
        {"shape": "POLYGON", "vertices": [[1, 1], [8, 1], [8, 9], [1, 9]]},
        {"shape": "POLYGON", "vertices": [[8, 9], [8, 1], [1, 1], [1, 9]]},
    ]
    with pytest.raises(CurationError) as exc:
        validate_review_payload("ACTION_GOLD", duplicate_polygon, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"

    decomposed = _action_payload()
    decomposed["predicates"] = [
        {
            "predicate_kind": "TEXT_VARIANTS",
            "action_type": "input_text",
            "field": "text",
            "unicode_normalization": "NFC",
            "case_sensitive": True,
            "allowed_values": ["e\u0301"],
            "evidence_ids": ["evidence-" + "1" * 24],
            "rationale": "The submitted bytes must already satisfy the declared normalization.",
            "human_selected": True,
        }
    ]
    with pytest.raises(CurationError) as exc:
        validate_review_payload("ACTION_GOLD", decomposed, clean_control=False)
    assert exc.value.code == "PROPOSAL_INVALID"


def test_source_tree_has_no_provider_gpu_network_replay_or_action_capability() -> None:
    source_paths = sorted(
        (MOBILEWORLD_SOURCE_ROOT / "mobile_world/offline/gold_curation").glob("*.py")
    )
    source_paths.append(REPOSITORY_ROOT / "MobileWorld/scripts/run_g1_gold_curation.py")
    forbidden_import_roots = {
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "socket",
        "subprocess",
        "openai",
        "anthropic",
        "torch",
        "transformers",
        "vllm",
        "docker",
        "kubernetes",
    }
    forbidden_calls = {
        "urlopen",
        "getaddrinfo",
        "create_connection",
        "Popen",
        "check_call",
        "check_output",
        "cuda",
        "execute_action",
        "execute_live_arm",
        "restore",
        "replay",
    }
    for path in source_paths:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert imported.isdisjoint(forbidden_import_roots), (path, imported)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(
                    module == item or module.startswith(item + ".")
                    for item in forbidden_import_roots
                ), (path, module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                else:
                    continue
                assert name not in forbidden_calls, (path, name, node.lineno)

    launcher_path = REPOSITORY_ROOT / "MobileWorld/scripts/run_g1_gold_curation.py"
    launcher_bytes = launcher_path.read_bytes()
    launcher = ast.parse(launcher_bytes)
    uvicorn_calls = [
        node
        for node in ast.walk(launcher)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert len(uvicorn_calls) == 1
    host = next(keyword.value for keyword in uvicorn_calls[0].keywords if keyword.arg == "host")
    assert isinstance(host, ast.Constant) and host.value == "127.0.0.1"
    launcher_text = launcher_bytes.decode("utf-8")
    for required_flag in (
        "--annotation-root",
        "--reviewer-registry",
        "--g1-5-publication-manifest",
        "--codec-gate-receipt",
    ):
        assert required_flag in launcher_text
    assert "website mode requires --annotation-root and --reviewer-registry" in launcher_text
    assert "must be supplied together" in launcher_text
    assert "workers=1" in launcher_text
    assert "reload=False" in launcher_text
    assert "proxy_headers=False" in launcher_text


def test_web_assets_are_local_no_store_and_have_no_authoritative_browser_database() -> None:
    web_root = MOBILEWORLD_SOURCE_ROOT / "mobile_world/offline/gold_curation/web"
    sources = {path.name: path.read_text(encoding="utf-8") for path in web_root.iterdir()}
    combined = "\n".join(sources.values())
    assert re.search(r"https?://", combined, flags=re.IGNORECASE) is None
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "serviceWorker",
        "WebSocket",
        "EventSource",
        "console.",
    ):
        assert forbidden not in combined
    assert 'href="/assets/styles.css"' in sources["index.html"]
    assert 'src="/assets/app.js"' in sources["index.html"]
    assert 'cache: "no-store"' in sources["app.js"]
    assert 'id="packet-binding-dialog"' in sources["index.html"]
    assert 'data-status="ADJUDICATING"' in sources["index.html"]
    assert 'data-status="BLOCKED_INVALID_INPUT"' in sources["index.html"]
    assert "/binding${query.toString()" in sources["app.js"]
    assert "state.delimiterRepairs" in sources["app.js"]
    assert "syncDelimiterRepairs()" in sources["app.js"]
    assert "function renderSpanLists(repairsAlreadySynchronized = false)" in sources["app.js"]
    assert "if (!repairsAlreadySynchronized) syncDelimiterRepairs();" in sources["app.js"]
    assert "renderSpanLists(true);" in sources["app.js"]
    assert "firstCorner" in sources["app.js"]
    assert 'name="consistency-label"' in sources["app.js"]
    assert "data-field-resolution" in sources["app.js"]
    assert "item.binding_token" in sources["app.js"]
    assert (
        '$("#preview-human-confirmed").disabled = !preview.acceptance_ready;' in sources["app.js"]
    )
    assert "binding_id" not in sources["app.js"]
    assert "!sham.matched" not in sources["app.js"]
    assert "!preview.sham_token_match.matched" not in sources["app.js"]
    assert 'data-p="normalized_action"' not in sources["app.js"]
    assert "JSON.stringify(predicate.allowed_values || [], null, 2)" in sources["app.js"]
    assert 'JSON.parse(value("allowed_values") || "[]")' in sources["app.js"]
    assert "每行一个" not in sources["app.js"]
    assert "allowed_values: (value(" not in sources["app.js"]
    assert 'data-a="${key}_mode"' in sources["app.js"]
    assert "function syncPredicatesExcept(excludedIndex = null)" in sources["app.js"]
    assert "syncPredicatesExcept(); state.predicates.push" in sources["app.js"]
    assert "syncPredicatesExcept(index); state.predicates.splice" in sources["app.js"]
    assert "syncPredicatesExcept(index); state.predicates[index] =" in sources["app.js"]
    assert 'card.querySelector("[data-exact-action-fields]")' in sources["app.js"]
    assert "target.innerHTML = exactActionFields" in sources["app.js"]
    assert '["index", "x", "y", "start_x", "start_y", "end_x", "end_y"]' in sources["app.js"]
    assert '["text", "goal_status", "app_name", "keycode", "action_name"]' in sources["app.js"]
    for structured_action_field in (
        'data-a="confirm_exact_fields"',
        'data-a="direction"',
        'data-a="clear_text"',
        'data-a="action_json_mode"',
        'data-a="action_json"',
    ):
        assert structured_action_field in sources["app.js"]
    assert 'data-p="region_additional_regions"' not in sources["app.js"]
    for stale_or_inferred in (
        "include-repair",
        "state.repairSpan",
        "custom-resolution",
        "radius =",
    ):
        assert stale_or_inferred not in sources["app.js"]
    assert 'data.status?.own_state === "DRAFTING"' in sources["app.js"]


def test_coordinate_picker_supports_scaled_pointer_drag_and_two_corner_fallback() -> None:
    web_root = MOBILEWORLD_SOURCE_ROOT / "mobile_world/offline/gold_curation/web"
    app = (web_root / "app.js").read_text(encoding="utf-8")
    styles = (web_root / "styles.css").read_text(encoding="utf-8")

    for marker in (
        'id="coordinate-selection"',
        'draggable="false"',
        "COORDINATE_DRAG_THRESHOLD_PX = 4",
        "function coordinatePoint(event, image, packet)",
        "event.clientX - bounds.left",
        "event.clientY - bounds.top",
        "packet.current_screenshot.width / bounds.width",
        "packet.current_screenshot.height / bounds.height",
        "function normalizedCoordinateRegion(start, end)",
        "x_min: Math.min(start.x, end.x)",
        "y_min: Math.min(start.y, end.y)",
        "x_max: Math.max(start.x, end.x)",
        "y_max: Math.max(start.y, end.y)",
        "image.onpointerdown",
        "image.onpointermove",
        "image.onpointerup",
        "image.onpointercancel",
        "image.onlostpointercapture",
        "image.setPointerCapture(event.pointerId)",
        "releaseCoordinateCapture(image, event.pointerId)",
        "target.pointerId !== null",
        "event.isPrimary === false",
        "Math.hypot(end.renderedX - start.renderedX, end.renderedY - start.renderedY)",
        "target.firstCorner = end",
        "writeCoordinateRegion(first, end)",
        'image.closest(".screenshot-wrap")',
        'event.key !== "Escape"',
        "在截图上拖拽框选并追加 region",
    ):
        assert marker in app

    assert ".screenshot-wrap.picking img { touch-action: none; user-select: none; }" in styles
    assert ".coordinate-selection" in styles
    assert ".coordinate-selection[hidden] { display: none; }" in styles


def test_dashboard_derives_blocked_invalid_input_from_authoritative_packet_bytes(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_unit_id = fake_publication.list_units()[0]["unit_id"]
    original_packet = fake_publication.packet

    def packet(unit_id: str, channel: str) -> dict[str, Any]:
        if unit_id == blocked_unit_id:
            raise CurationError("PACKET_EVIDENCE_INCOMPLETE", "fixture packet is invalid")
        return original_packet(unit_id, channel)

    monkeypatch.setattr(fake_publication, "packet", packet)
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        _session(client, "action-primary")
        response = client.get("/api/assignments")
    assert response.status_code == 200
    body = response.json()
    blocked = [item for item in body["items"] if item["state"] == "BLOCKED_INVALID_INPUT"]
    assert len(blocked) == 1
    assert blocked[0]["can_open"] is False
    assert body["counts"]["BLOCKED_INVALID_INPUT"] == 1
    assert body["workflow_counts"]["BLOCKED_INVALID_INPUT"] == 1


def test_owner_registry_is_closed_private_repo_external_and_secret_redacted(
    tmp_path: Path,
) -> None:
    registry_path = _write_reviewer_registry(tmp_path / "reviewers.json")
    registry = ReviewerRegistry.load(registry_path)
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600
    assert registry.source_path == registry_path.resolve()
    assert registry.sha256 == _sha256(registry.canonical_bytes)
    canonical = json.loads(registry.canonical_bytes)
    assert canonical["schema_version"] == REVIEWER_REGISTRY_SCHEMA_VERSION
    assert all(
        set(item) == {"principal_id", "role", "access_secret_sha256", "adjudication_channel"}
        for item in canonical["principals"]
    )
    for _, _, secret, _ in PRINCIPALS:
        assert secret.encode() not in registry.canonical_bytes

    public_registry = _write_reviewer_registry(tmp_path / "public.json", mode=0o644)
    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(public_registry)
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"

    unicode_registry = ReviewerRegistry.load(
        _write_reviewer_registry(
            tmp_path / "unicode-reviewers.json",
            principals=(("研究员-甲", "ACTION_GOLD_PRIMARY", "exact-unicode-secret-0001", None),),
        )
    )
    assert unicode_registry.authenticate(
        "研究员-甲", "ACTION_GOLD_PRIMARY", "exact-unicode-secret-0001"
    ) == ("研究员-甲", "ACTION_GOLD_PRIMARY")

    linked = tmp_path / "linked.json"
    linked.symlink_to(registry_path)
    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(linked)
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"

    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(Path(__file__))
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"

    exact_utf8_secret = "secret-密碼-🔐-exact-bytes"
    utf8_registry = ReviewerRegistry.load(
        _write_reviewer_registry(
            tmp_path / "utf8-secret.json",
            principals=(
                (
                    "utf8-secret-reviewer",
                    "ACTION_GOLD_PRIMARY",
                    exact_utf8_secret,
                    None,
                ),
            ),
        )
    )
    expected_semantic_registry = {
        "schema_version": REVIEWER_REGISTRY_SCHEMA_VERSION,
        "principals": [
            {
                "principal_id": "utf8-secret-reviewer",
                "role": "ACTION_GOLD_PRIMARY",
                "access_secret_sha256": _sha256(exact_utf8_secret.encode("utf-8")),
                "adjudication_channel": None,
            }
        ],
    }
    assert utf8_registry.canonical_bytes == canonical_json_bytes(expected_semantic_registry)


def test_owner_registry_rejects_aliases_multiple_roles_and_open_shapes(tmp_path: Path) -> None:
    duplicate = (
        ("same-principal", "ACTION_GOLD_PRIMARY", "secret-same-principal-one", None),
        ("same-principal", "TRANSFORMATION_PRIMARY", "secret-same-principal-two", None),
    )
    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(
            _write_reviewer_registry(tmp_path / "duplicate.json", principals=duplicate)
        )
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"

    malformed = {
        "schema_version": REVIEWER_REGISTRY_SCHEMA_VERSION,
        "principals": [
            {
                "principal_id": "named-reviewer",
                "role": "ACTION_GOLD_PRIMARY",
                "access_secret": "sufficiently-long-secret",
                "alias": "alternate-name",
            }
        ],
    }
    path = tmp_path / "open-shape.json"
    path.write_bytes(canonical_json_bytes(malformed))
    path.chmod(0o600)
    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(path)
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"


@pytest.mark.parametrize(
    "principals",
    [
        (
            (
                "channelled-initial-reviewer",
                "ACTION_GOLD_PRIMARY",
                "secret-channelled-initial-reviewer",
                "ACTION_GOLD",
            ),
        ),
        (("unbound-adjudicator", "ADJUDICATOR", "secret-unbound-adjudicator", None),),
        (
            (
                "unknown-channel-adjudicator",
                "ADJUDICATOR",
                "secret-unknown-channel-adjudicator",
                "UNKNOWN_CHANNEL",
            ),
        ),
        (
            ("same-adjudicator", "ADJUDICATOR", "secret-adjudicator-action", "ACTION_GOLD"),
            (
                "same-adjudicator",
                "ADJUDICATOR",
                "secret-adjudicator-transform",
                "TRANSFORMATION",
            ),
        ),
    ],
)
def test_owner_registry_rejects_cross_role_or_cross_channel_principals(
    tmp_path: Path,
    principals: tuple[tuple[str, str, str, str | None], ...],
) -> None:
    with pytest.raises(CurationError) as exc:
        ReviewerRegistry.load(
            _write_reviewer_registry(
                tmp_path / f"invalid-channel-{len(principals)}.json",
                principals=principals,
            )
        )
    assert exc.value.code == "REVIEWER_REGISTRY_INVALID"


def test_workspace_identity_and_assignment_hmac_formulas_are_exact(
    annotation_store: AnnotationStore,
) -> None:
    assignment_key = (annotation_store.root / "assignment-key.bin").read_bytes()
    reviewer_id = "action-primary"
    expected_identity = hmac.new(
        assignment_key,
        (
            "mobileworld.g1.gold-curation.reviewer/v1\0"
            f"{annotation_store.workspace_id}\0{reviewer_id}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert annotation_store.identity_commitment(reviewer_id) == expected_identity

    unit_id = _unit_id(0)
    role = "ACTION_GOLD_PRIMARY"
    expected_assignment_digest = hmac.new(
        assignment_key,
        (
            "mobileworld.g1.gold-curation.assignment/v1\0"
            f"{annotation_store.workspace_id}\0ACTION_GOLD\0{role}\0{unit_id}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert annotation_store.assignment_id(unit_id, role) == (
        "g1assignment-" + expected_assignment_digest[:32]
    )


def test_adjudicator_is_rejected_outside_owner_bound_channel(
    annotation_store: AnnotationStore,
) -> None:
    assert annotation_store.adjudicator_channel_for("third-adjudicator") == "ACTION_GOLD"
    with pytest.raises(CurationError) as exc:
        annotation_store.adjudication_case(_unit_id(0), "TRANSFORMATION", "third-adjudicator")
    assert exc.value.code == "REVIEWER_ROLE_COLLISION"


@pytest.mark.parametrize(
    ("reviewer_id", "role", "secret"),
    [
        ("action-primary", "ACTION_GOLD_PRIMARY", "wrong-secret-that-is-long"),
        ("unknown-alias", "ACTION_GOLD_PRIMARY", "secret-action-primary-0001"),
        ("action-primary", "TRANSFORMATION_PRIMARY", "secret-action-primary-0001"),
    ],
)
def test_session_rejects_wrong_secret_unknown_alias_and_role_mismatch(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
    reviewer_id: str,
    role: str,
    secret: str,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        response = client.post(
            "/api/session",
            json={"reviewer_id": reviewer_id, "role": role, "access_secret": secret},
            headers={"origin": "http://127.0.0.1"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "REVIEWER_AUTHENTICATION_FAILED"
    assert secret not in response.text


def test_session_cookie_csrf_host_origin_headers_and_identity_nonleakage(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as anonymous:
        response = anonymous.get("/api/assignments")
        assert response.status_code == 401
        response = anonymous.get("/api/config", headers={"host": "curation.example.test"})
        assert response.status_code == 403
        assert response.json() == {"error": "LOOPBACK_HOST_REQUIRED"}
        response = anonymous.get(
            "/api/config",
            headers={"origin": "https://external.example.test"},
        )
        assert response.status_code == 403
        assert response.json() == {"error": "LOOPBACK_ORIGIN_REQUIRED"}

    with InProcessASGIClient(app) as client:
        config_response = client.get("/api/config")
        assert config_response.status_code == 200
        config = config_response.json()
        assert config["readiness"] == {
            "codec_gate_open": False,
            "human_curation_complete": False,
            "formal_g1_6_bundle": False,
            "admission_ready": False,
            "execution_ready": False,
            "formal_replay_ready": False,
        }
        assert config["safety"] == {
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
        }
        reviewer_id, role, access_secret, _ = _principal("action-primary")
        session_response = client.post(
            "/api/session",
            json={
                "reviewer_id": reviewer_id,
                "role": role,
                "access_secret": access_secret,
            },
            headers={"origin": "http://127.0.0.1"},
        )
        assert session_response.status_code == 200
        body = session_response.json()
        assert set(body) == {"reviewer_identity_sha256", "reviewer_role", "csrf_token"}
        assert body["reviewer_role"] == role
        assert re.fullmatch(r"[0-9a-f]{64}", body["reviewer_identity_sha256"])
        cookie = session_response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie
        assert reviewer_id not in cookie
        assert access_secret not in cookie
        assert reviewer_id not in session_response.text
        assert access_secret not in session_response.text

        assignment_id = annotation_store.assignment_id(_unit_id(0), role)
        packet_response = client.get(f"/api/assignments/{assignment_id}/packet")
        assert packet_response.status_code == 200
        request = {
            "assignment_id": assignment_id,
            "payload": _browser_action_payload(packet_response.json()["packet"]),
        }
        assert client.post("/api/reviews/draft", json=request).status_code == 403
        assert (
            client.post(
                "/api/reviews/draft",
                json=request,
                headers={"x-g1-csrf-token": "wrong-token"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/reviews/draft",
                json=request,
                headers={
                    "origin": "https://external.example.test",
                    "x-g1-csrf-token": body["csrf_token"],
                },
            ).status_code
            == 403
        )
        saved = client.post(
            "/api/reviews/draft",
            json=request,
            headers={
                "origin": "http://127.0.0.1",
                "x-g1-csrf-token": body["csrf_token"],
            },
        )
        assert saved.status_code == 200

        for response in (session_response, saved):
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["cross-origin-resource-policy"] == "same-origin"
            csp = response.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "connect-src 'self'" in csp
            assert "object-src 'none'" in csp


def test_different_loopback_origin_is_rejected_as_cross_origin(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.get(
            "/api/config",
            headers={"origin": "http://localhost:9999"},
        )
    assert response.status_code == 403


def test_state_change_requires_origin_even_with_a_valid_csrf_token(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        csrf, _ = _session(client, "action-primary")
        assignment_id = annotation_store.assignment_id(_unit_id(0), "ACTION_GOLD_PRIMARY")
        packet = client.get(f"/api/assignments/{assignment_id}/packet").json()["packet"]
        response = client.post(
            "/api/reviews/draft",
            json={
                "assignment_id": assignment_id,
                "payload": _browser_action_payload(packet),
            },
            headers={"x-g1-csrf-token": csrf},
        )
    assert response.status_code == 403


def test_transformation_preview_endpoint_is_role_assignment_and_body_bound(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    unit_id = _unit_id(0)
    assignment_id = annotation_store.assignment_id(unit_id, "TRANSFORMATION_PRIMARY")
    wrong_role_assignment = annotation_store.assignment_id(unit_id, "TRANSFORMATION_SECONDARY")
    schema = json.loads(
        (G16_SCHEMA_ROOT / "browser_transformation_preview.schema.json").read_bytes()
    )

    with InProcessASGIClient(app) as transformation_client:
        csrf, _ = _session(transformation_client, "transform-primary")
        packet_response = transformation_client.get(f"/api/assignments/{assignment_id}/packet")
        assert packet_response.status_code == 200
        preview_inputs = _browser_preview_inputs(packet_response.json()["packet"])
        headers = {
            "origin": "http://127.0.0.1",
            "x-g1-csrf-token": csrf,
        }

        missing_csrf = transformation_client.post(
            "/api/transformation-previews",
            json={"assignment_id": assignment_id, "preview_inputs": preview_inputs},
            headers={"origin": "http://127.0.0.1"},
        )
        assert missing_csrf.status_code == 403

        open_body = transformation_client.post(
            "/api/transformation-previews",
            json={
                "assignment_id": assignment_id,
                "preview_inputs": preview_inputs,
                "model_hint": "forbidden",
            },
            headers=headers,
        )
        assert open_body.status_code == 400
        assert open_body.json()["error"] == "REQUEST_INVALID"

        wrong_assignment = transformation_client.post(
            "/api/transformation-previews",
            json={
                "assignment_id": wrong_role_assignment,
                "preview_inputs": preview_inputs,
            },
            headers=headers,
        )
        assert wrong_assignment.status_code == 400
        assert wrong_assignment.json()["error"] == "ASSIGNMENT_INVALID"

        response = transformation_client.post(
            "/api/transformation-previews",
            json={"assignment_id": assignment_id, "preview_inputs": preview_inputs},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        preview = response.json()
        Draft202012Validator(schema).validate(preview)
        assert preview["acceptance_ready"] is True
        assert fake_publication.preview_calls[-1][0] == unit_id
        stable_inputs = fake_publication.preview_calls[-1][1]
        assert (
            stable_inputs["focal_target_spans"][0]["record_id"]
            != preview_inputs["focal_target_spans"][0]["record_id"]
        )
        assert (
            stable_inputs["correction_evidence_ids"][0]
            != preview_inputs["correction_evidence_ids"][0]
        )
        for raw_value in (
            "internal-binding-stable-id",
            "internal-record-stable-id",
            "payload.request_view.messages[7]",
            "RAW-HUMAN-DIFF-STABLE-ID-MARKER",
        ):
            assert raw_value not in response.text

        for checked in (missing_csrf, open_body, wrong_assignment, response):
            assert checked.headers["cache-control"] == "no-store"

    with InProcessASGIClient(app) as action_client:
        action_csrf, _ = _session(action_client, "action-primary")
        action_assignment = annotation_store.assignment_id(unit_id, "ACTION_GOLD_PRIMARY")
        denied = action_client.post(
            "/api/transformation-previews",
            json={
                "assignment_id": action_assignment,
                "preview_inputs": preview_inputs,
            },
            headers={
                "origin": "http://127.0.0.1",
                "x-g1-csrf-token": action_csrf,
            },
        )
        assert denied.status_code == 400
        assert denied.json()["error"] == "CHANNEL_INVALID"
        assert denied.headers["cache-control"] == "no-store"


def test_non_loopback_peer_is_rejected_even_with_allowed_host(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app, client=("203.0.113.9", 43100)) as client:
        response = client.get("/api/config", headers={"host": "127.0.0.1"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("headers", "content", "status", "error"),
    [
        (
            {"origin": "http://127.0.0.1", "content-type": "text/plain"},
            b"{}",
            415,
            "JSON_CONTENT_TYPE_REQUIRED",
        ),
        (
            {
                "origin": "http://127.0.0.1",
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            b"{}",
            415,
            "CONTENT_ENCODING_FORBIDDEN",
        ),
        (
            {"origin": "http://127.0.0.1", "content-type": "application/json"},
            b"not-json",
            400,
            "REQUEST_INVALID",
        ),
        (
            {"origin": "http://127.0.0.1", "content-type": "application/json"},
            b"[]",
            400,
            "REQUEST_INVALID",
        ),
        (
            {
                "origin": "http://127.0.0.1",
                "content-type": "application/json",
                "content-length": str(MAX_HTTP_REQUEST_BYTES + 1),
            },
            b"{}",
            413,
            "REQUEST_BODY_TOO_LARGE",
        ),
    ],
)
def test_http_boundary_requires_bounded_closed_json(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
    headers: dict[str, str],
    content: bytes,
    status: int,
    error: str,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        response = client.post("/api/session", content=content, headers=headers)
    assert response.status_code == status
    assert response.json()["error"] == error
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_blinded_assignment_packets_are_role_bound_and_leak_no_stable_identity(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    app = _create_formal_test_app(fake_publication, annotation_store)
    unit_id = _unit_id(0)
    primary_assignment = annotation_store.assignment_id(unit_id, "ACTION_GOLD_PRIMARY")
    secondary_assignment = annotation_store.assignment_id(unit_id, "ACTION_GOLD_SECONDARY")
    assert primary_assignment != secondary_assignment

    with InProcessASGIClient(app) as primary_client, InProcessASGIClient(app) as secondary_client:
        _session(primary_client, "action-primary")
        _session(secondary_client, "action-secondary")
        primary_response = primary_client.get(f"/api/assignments/{primary_assignment}/packet")
        secondary_response = secondary_client.get(f"/api/assignments/{secondary_assignment}/packet")
        assert primary_response.status_code == secondary_response.status_code == 200
        assert (
            secondary_client.get(f"/api/assignments/{primary_assignment}/packet").status_code == 400
        )

    primary = primary_response.json()
    secondary = secondary_response.json()
    assert set(primary) == {"packet", "draft", "status"}
    packet_schema = json.loads((G16_SCHEMA_ROOT / "curator_packet.schema.json").read_bytes())
    Draft202012Validator(packet_schema).validate(primary["packet"])
    Draft202012Validator(packet_schema).validate(secondary["packet"])
    assert primary["draft"] is secondary["draft"] is None
    assert primary["packet"]["task"] == secondary["packet"]["task"]
    assert [item["content"] for item in primary["packet"]["evidence"]] == [
        item["content"] for item in secondary["packet"]["evidence"]
    ]
    assert primary["packet"]["visibility"] == secondary["packet"]["visibility"]
    assert primary["packet"]["visibility"]["history_visible"] is False
    assert primary["packet"]["visibility"]["peer_reviews_visible"] is False
    assert "source_records" not in primary["packet"]
    assert "target_candidates" not in primary["packet"]

    encoded = json.dumps(primary, ensure_ascii=False)
    for secret in (
        unit_id,
        "g1case-",
        "g1control-",
        "source_key",
        "capsule_body_sha256",
        "publication_manifest_sha256",
        "event-task",
        "event-pre",
        "action-primary",
        _principal("action-primary")[2],
    ):
        assert secret not in encoded


def test_assignment_packet_digest_is_server_derived_and_cannot_be_forged(
    annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    unit_id = _unit_id(8)
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        csrf, _ = _session(client, "action-primary")
        assignment_id = annotation_store.assignment_id(unit_id, "ACTION_GOLD_PRIMARY")
        response = client.get(f"/api/assignments/{assignment_id}/packet")
        assert response.status_code == 200
        packet = response.json()["packet"]

        tampered_packet = {**packet, "assignment_packet_sha256": "f" * 64}
        with pytest.raises(CurationError) as exc:
            annotation_store.bind_assignment_packet(tampered_packet)
        assert exc.value.code == "PACKET_BINDING_INVALID"

        injected_digest = client.post(
            "/api/reviews/draft",
            json={
                "assignment_id": assignment_id,
                "assignment_packet_sha256": "f" * 64,
                "payload": _browser_action_payload(packet),
            },
            headers={
                "origin": "http://127.0.0.1",
                "x-g1-csrf-token": csrf,
            },
        )
        assert injected_digest.status_code == 400
        assert injected_digest.json()["error"] == "REQUEST_INVALID"

    primary = _event_packet_bindings(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
    )
    secondary = _event_packet_bindings(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-secondary",
        reviewer_role="ACTION_GOLD_SECONDARY",
    )
    with pytest.raises(CurationError) as exc:
        annotation_store.save_draft(
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            assignment_id=primary["assignment_id"],
            source_packet_sha256=primary["source_packet_sha256"],
            assignment_packet_sha256=secondary["assignment_packet_sha256"],
            payload=_action_payload(),
        )
    assert exc.value.code == "PACKET_BINDING_INVALID"


def test_primary_cannot_observe_secondary_submission_before_or_after_finalize(
    open_annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(1)
    _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-secondary",
        reviewer_role="ACTION_GOLD_SECONDARY",
        payload=_action_payload(),
    )
    app = _create_formal_test_app(fake_publication, annotation_store)
    assignment_id = annotation_store.assignment_id(unit_id, "ACTION_GOLD_PRIMARY")
    with InProcessASGIClient(app) as client:
        _session(client, "action-primary")
        before = client.get(f"/api/assignments/{assignment_id}/packet")
        assert before.status_code == 200
        assert "secondary" not in json.dumps(before.json()).lower()
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(),
        )
        after = client.get(f"/api/assignments/{assignment_id}/packet")
        assert after.status_code == 200
        assert "secondary" not in json.dumps(after.json()).lower()


def test_reviewer_identity_is_disjoint_within_and_across_channels(
    open_annotation_store: AnnotationStore,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(2)
    _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    with pytest.raises(CurationError) as exc:
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_SECONDARY",
            payload=_action_payload(),
        )
    assert exc.value.code == "REVIEWER_AUTHENTICATION_FAILED"

    with pytest.raises(CurationError) as exc:
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="TRANSFORMATION_PRIMARY",
            payload=_transformation_payload(),
        )
    assert exc.value.code == "REVIEWER_AUTHENTICATION_FAILED"

    _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-secondary",
        reviewer_role="ACTION_GOLD_SECONDARY",
        payload=_action_payload(),
    )
    events = annotation_store.read_events()
    identities = {event["reviewer_identity_sha256"] for event in events}
    assert len(identities) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", item) for item in identities)
    assert "action-primary" not in canonical_json_bytes(events).decode()
    assert "action-secondary" not in canonical_json_bytes(events).decode()


def test_material_disagreement_requires_identity_disjoint_third_party_adjudication(
    open_annotation_store: AnnotationStore,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(3)
    _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-secondary",
        reviewer_role="ACTION_GOLD_SECONDARY",
        payload=_excluded_action_payload(),
    )
    assert annotation_store.channel_resolution(unit_id, "ACTION_GOLD") is None
    case = annotation_store.adjudication_case(unit_id, "ACTION_GOLD", "third-adjudicator")
    assert case["disagreement_fields"] == [
        "DISPOSITION",
        "ACCEPTED_ACTION_PREDICATES",
        "ACTION_TOLERANCE",
    ]
    assert set(case) == {
        "unit_id",
        "channel",
        "disagreement_fields",
        "primary",
        "secondary",
    }
    with pytest.raises(CurationError) as exc:
        annotation_store.adjudication_case(unit_id, "ACTION_GOLD", "action-primary")
    assert exc.value.code == "REVIEWER_AUTHENTICATION_FAILED"

    event = _submit_adjudication(
        annotation_store,
        unit_id=unit_id,
        channel="ACTION_GOLD",
        reviewer_id="third-adjudicator",
        resolved_payload=_action_payload(),
        rationale="The visible target supports the primary complete action set.",
    )
    assert event["event_kind"] == "ADJUDICATION_SUBMITTED"
    assert annotation_store.codec_gate_receipt is not None
    assert (
        event["codec_gate_receipt_sha256"] == annotation_store.codec_gate_receipt["receipt_sha256"]
    )
    resolution = annotation_store.channel_resolution(unit_id, "ACTION_GOLD")
    assert resolution is not None
    assert resolution["resolution_kind"] == "ADJUDICATED"
    assert resolution["payload"] == _action_payload()
    with pytest.raises(CurationError) as exc:
        _submit_adjudication(
            annotation_store,
            unit_id=unit_id,
            channel="ACTION_GOLD",
            reviewer_id="third-adjudicator",
            resolved_payload=_excluded_action_payload(),
            rationale="A finalized adjudication cannot be overwritten.",
        )
    assert exc.value.code == "ADJUDICATION_ALREADY_SUBMITTED"


def test_adjudicator_http_packet_is_peer_visible_but_assignment_scoped(
    open_annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(10)
    primary = _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    secondary = _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-secondary",
        reviewer_role="ACTION_GOLD_SECONDARY",
        payload=_excluded_action_payload(),
    )
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as adjudicator:
        csrf, _ = _session(adjudicator, "third-adjudicator")
        assignments = adjudicator.get("/api/assignments")
        assert assignments.status_code == 200
        assert assignments.json()["total"] == 1
        assignment = assignments.json()["items"][0]
        assert assignment["channel"] == "ACTION_GOLD"
        assert assignment["state"] == "ADJUDICATING"
        assert assignment["workflow_status"] == "ADJUDICATION_REQUIRED"
        assert assignments.json()["counts"] == {"ADJUDICATING": 1}

        response = adjudicator.get(
            f"/api/assignments/{assignment['assignment_id']}/packet?channel=ACTION_GOLD"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        packet = body["packet"]
        assert packet["visibility"]["peer_reviews_visible"] is True
        assert set(packet["compared_review_event_ids"]) == {
            primary["event_id"],
            secondary["event_id"],
        }
        assert "reviewer_identity_sha256" not in body["adjudication"]["primary"]
        assert "reviewer_identity_sha256" not in body["adjudication"]["secondary"]
        encoded_case = canonical_json_bytes(body["adjudication"])
        assert ("evidence-" + "1" * 24).encode() not in encoded_case
        assert all(
            evidence_id.startswith("evidence-")
            for peer in ("primary", "secondary")
            for predicate in body["adjudication"][peer]["payload"].get("predicates", [])
            for evidence_id in predicate["evidence_ids"]
        )

        submitted = adjudicator.post(
            "/api/adjudications/submit",
            json={
                "assignment_id": assignment["assignment_id"],
                "channel": "ACTION_GOLD",
                "resolved_payload": _browser_action_payload(packet),
                "field_resolutions": {
                    field: f"Human resolution for {field} based on the visible packet."
                    for field in body["adjudication"]["disagreement_fields"]
                },
                "rationale": "The visible target supports the complete primary action set.",
            },
            headers={
                "origin": "http://127.0.0.1",
                "x-g1-csrf-token": csrf,
            },
        )
        assert submitted.status_code == 200, submitted.text

        wrong_channel = adjudicator.get(
            f"/api/assignments/{assignment['assignment_id']}/packet?channel=TRANSFORMATION"
        )
        assert wrong_channel.status_code == 400

    resolution = annotation_store.channel_resolution(unit_id, "ACTION_GOLD")
    assert resolution is not None and resolution["resolution_kind"] == "ADJUDICATED"


def test_matching_independent_reviews_resolve_without_adjudication(
    open_annotation_store: AnnotationStore,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(4)
    for reviewer_id, reviewer_role in (
        ("action-primary", "ACTION_GOLD_PRIMARY"),
        ("action-secondary", "ACTION_GOLD_SECONDARY"),
    ):
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            payload=_action_payload(),
        )
    resolution = annotation_store.channel_resolution(unit_id, "ACTION_GOLD")
    assert resolution is not None
    assert resolution["resolution_kind"] == "INDEPENDENT_AGREEMENT"
    assert resolution["disagreement_fields"] == []
    with pytest.raises(CurationError) as exc:
        annotation_store.adjudication_case(unit_id, "ACTION_GOLD", "third-adjudicator")
    assert exc.value.code == "ADJUDICATION_NOT_REQUIRED"


def test_consistency_audit_is_sealed_until_gold_and_transformation_resolve(
    open_annotation_store: AnnotationStore,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(5)
    assert annotation_store.consistency_ready(unit_id) is False
    assert annotation_store.status_for(
        unit_id, "CONSISTENCY_AUDIT_PRIMARY", "consistency-primary"
    ) == {
        "state": "WAITING_FOR_PEER",
        "own_state": "NOT_ASSIGNED",
        "workflow_state": "WAITING_FOR_PEER",
        "can_open": False,
    }
    with pytest.raises(CurationError) as exc:
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="consistency-primary",
            reviewer_role="CONSISTENCY_AUDIT_PRIMARY",
            payload=_consistency_payload(),
        )
    assert exc.value.code == "CONSISTENCY_AUDIT_NOT_READY"

    for reviewer_id, reviewer_role in (
        ("action-primary", "ACTION_GOLD_PRIMARY"),
        ("action-secondary", "ACTION_GOLD_SECONDARY"),
    ):
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            payload=_action_payload(),
        )
    assert annotation_store.consistency_ready(unit_id) is False
    for reviewer_id, reviewer_role in (
        ("transform-primary", "TRANSFORMATION_PRIMARY"),
        ("transform-secondary", "TRANSFORMATION_SECONDARY"),
    ):
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            payload=_transformation_payload(),
        )
    assert annotation_store.consistency_ready(unit_id) is True

    for reviewer_id, reviewer_role in (
        ("consistency-primary", "CONSISTENCY_AUDIT_PRIMARY"),
        ("consistency-secondary", "CONSISTENCY_AUDIT_SECONDARY"),
    ):
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            payload=_consistency_payload(),
        )
    resolution = annotation_store.channel_resolution(unit_id, "CONSISTENCY_AUDIT")
    assert resolution is not None
    assert resolution["resolution_kind"] == "INDEPENDENT_AGREEMENT"


def test_append_only_journal_is_canonical_hash_chained_and_final_is_immutable(
    open_annotation_store: AnnotationStore,
) -> None:
    annotation_store = open_annotation_store
    unit_id = _unit_id(6)
    _save_draft(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    _save_draft(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(x_min=5),
    )
    final = _submit_review(
        annotation_store,
        unit_id=unit_id,
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(x_min=6),
    )
    with pytest.raises(CurationError) as exc:
        _save_draft(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(x_min=7),
        )
    assert exc.value.code == "REVIEW_ALREADY_SUBMITTED"
    with pytest.raises(CurationError) as exc:
        _submit_review(
            annotation_store,
            unit_id=unit_id,
            reviewer_id="action-primary",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(x_min=7),
        )
    assert exc.value.code == "REVIEW_ALREADY_SUBMITTED"

    ledger = annotation_store.root / "annotation-events.jsonl"
    lines = ledger.read_bytes().splitlines()
    events = annotation_store.read_events()
    event_schema = json.loads((G16_SCHEMA_ROOT / "annotation_event.schema.json").read_bytes())
    assert len(lines) == len(events) == 3
    assert all(line == canonical_json_bytes(event) for line, event in zip(lines, events))
    previous: str | None = None
    for seq, event in enumerate(events):
        assert event["event_seq"] == seq
        assert event["previous_event_sha256"] == previous
        subject = {key: value for key, value in event.items() if key != "event_sha256"}
        assert event["event_sha256"] == canonical_sha256(subject)
        id_subject = {
            key: value for key, value in event.items() if key not in {"event_id", "event_sha256"}
        }
        assert event["event_id"] == "g1annotation-" + canonical_sha256(id_subject)[:24]
        assert event["payload_sha256"] == canonical_sha256(event["payload"])
        assert event["codec_gate_receipt_sha256"] == (
            None
            if event["event_kind"] == "DRAFT_SAVED"
            else annotation_store.codec_gate_receipt["receipt_sha256"]
        )
        assert event["material_projection_sha256"] == (
            None
            if event["event_kind"] == "DRAFT_SAVED"
            else canonical_sha256(
                annotation_store.material_projection_for(
                    event["unit_id"], event["channel"], event["payload"]
                )
            )
        )
        Draft202012Validator(event_schema).validate(event)
        for directory, digest in (
            ("packets", event["source_packet_sha256"]),
            ("assignment-packets", event["assignment_packet_sha256"]),
        ):
            artifact = annotation_store.root / directory / "sha256" / digest[:2] / f"{digest}.json"
            artifact_bytes = artifact.read_bytes()
            if directory == "packets":
                assert _sha256(artifact_bytes) == digest
            else:
                assignment_packet = json.loads(artifact_bytes)
                assert artifact_bytes == canonical_json_bytes(assignment_packet)
                assert assignment_packet["assignment_packet_sha256"] == digest
                assert (
                    canonical_sha256(
                        {
                            key: value
                            for key, value in assignment_packet.items()
                            if key != "assignment_packet_sha256"
                        }
                    )
                    == digest
                )
        previous = event["event_sha256"]
    assert final == events[-1]

    manifest = json.loads((annotation_store.root / "workspace-manifest.json").read_bytes())
    workspace_schema = json.loads(
        (G16_SCHEMA_ROOT / "annotation_workspace.schema.json").read_bytes()
    )
    Draft202012Validator(workspace_schema).validate(manifest)
    assert manifest["target_unit_count"] == 190
    assert (
        manifest["identity_policy"]["owner_registry_sha256"]
        == annotation_store.reviewer_registry.sha256
    )
    assert manifest["identity_policy"]["identity_key_commitment_sha256"] == _sha256(
        (annotation_store.root / "assignment-key.bin").read_bytes()
    )
    assert manifest["readiness"]["formal_annotation_open"] is False
    assert all(
        value is False
        for key, value in manifest["readiness"].items()
        if key.endswith("_allowed") or key.endswith("_ready")
    )
    assert stat.S_IMODE(annotation_store.root.stat().st_mode) == 0o700
    for path in (
        ledger,
        annotation_store.root / "workspace-manifest.json",
        annotation_store.root / "assignment-key.bin",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    serialized = canonical_json_bytes(events) + canonical_json_bytes(manifest)
    for principal_id, _, access_secret, _ in PRINCIPALS:
        assert principal_id.encode() not in serialized
        assert access_secret.encode() not in serialized

    receipt = annotation_store.export_workspace_receipt()
    assert receipt["event_count"] == 3
    assert receipt["last_event_sha256"] == events[-1]["event_sha256"]
    assert annotation_store.codec_gate_receipt is not None
    assert (
        receipt["codec_gate_receipt_sha256"]
        == annotation_store.codec_gate_receipt["receipt_sha256"]
    )
    assert receipt["formal_g1_6_bundle"] is False
    for guard in (
        "admission_ready",
        "execution_ready",
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
        "gpu_used",
        "model_invoked",
        "formal_replay_performed",
    ):
        assert receipt[guard] is False


def test_journal_read_rejects_non_lf_material_tamper_and_hardlink(
    open_annotation_store: AnnotationStore,
) -> None:
    store = open_annotation_store
    _submit_review(
        store,
        unit_id=_unit_id(7),
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    ledger = store.root / "annotation-events.jsonl"
    original = ledger.read_bytes()
    assert original.endswith(b"\n")

    for malformed in (original[:-1], original[:-1] + b"\r\n"):
        ledger.write_bytes(malformed)
        ledger.chmod(0o600)
        with pytest.raises(CurationError) as exc:
            store.read_events()
        assert exc.value.code == "ANNOTATION_LEDGER_INVALID"
        ledger.write_bytes(original)
        ledger.chmod(0o600)

    event = json.loads(original)
    event["material_projection_sha256"] = SHA256_ZERO
    id_subject = {
        key: value for key, value in event.items() if key not in {"event_id", "event_sha256"}
    }
    event["event_id"] = "g1annotation-" + canonical_sha256(id_subject)[:24]
    event["event_sha256"] = canonical_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )
    ledger.write_bytes(canonical_json_bytes(event) + b"\n")
    ledger.chmod(0o600)
    with pytest.raises(CurationError) as exc:
        store.read_events()
    assert exc.value.code == "ANNOTATION_LEDGER_INVALID"
    ledger.write_bytes(original)
    ledger.chmod(0o600)

    surprise = store.root / "unexpected-journal-hardlink"
    os.link(ledger, surprise)
    with pytest.raises(CurationError) as exc:
        store.read_events()
    assert exc.value.code == "ANNOTATION_STORE_INVALID"
    surprise.unlink()
    assert store.read_events()[0]["event_kind"] == "REVIEW_SUBMITTED"


def test_concurrent_appends_remain_linear_canonical_and_lossless(
    annotation_store: AnnotationStore,
) -> None:
    def append(index: int) -> str:
        event = _save_draft(
            annotation_store,
            unit_id=_unit_id(20 + index),
            reviewer_id=f"worker-{index:02d}",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(x_min=4 + index),
        )
        return event["event_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        event_ids = list(pool.map(append, range(24)))
    assert len(set(event_ids)) == 24
    events = annotation_store.read_events()
    assert len(events) == 24
    assert [event["event_seq"] for event in events] == list(range(24))
    assert [event["previous_event_sha256"] for event in events] == [
        None,
        *[event["event_sha256"] for event in events[:-1]],
    ]
    lines = (annotation_store.root / "annotation-events.jsonl").read_bytes().splitlines()
    assert lines == [canonical_json_bytes(event) for event in events]


def test_store_and_publication_reject_symlinks_path_escape_and_tampering(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    repository = tmp_path / "fake-repository"
    repository.mkdir()
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            repository / "state",
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
            repository_root=repository,
        )
    assert exc.value.code == "ANNOTATION_ROOT_FORBIDDEN"

    real_root = tmp_path / "real-state"
    real_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-state"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            linked_root,
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
        )
    assert exc.value.code == "ANNOTATION_ROOT_INVALID"

    store = AnnotationStore(
        tmp_path / "journal-state",
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
    )
    outside = tmp_path / "outside-ledger.jsonl"
    outside.write_bytes(b"do-not-follow\n")
    (store.root / "annotation-events.jsonl").symlink_to(outside)
    with pytest.raises(CurationError) as exc:
        store.read_events()
    assert exc.value.code == "ANNOTATION_STORE_INVALID"

    with pytest.raises(CurationError) as exc:
        _safe_parts("../escape.json")
    assert exc.value.code == "REFERENCE_INVALID"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside_artifact = tmp_path / "outside-artifact"
    outside_artifact.write_bytes(b"sensitive")
    (artifact_root / "linked").symlink_to(outside_artifact)
    with pytest.raises(CurationError) as exc:
        _open_regular_beneath(artifact_root, "linked")
    assert exc.value.code in {"REFERENCE_UNRESOLVED", "REFERENCE_INVALID"}

    tamper_store = AnnotationStore(
        tmp_path / "tamper-state",
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
    )
    _save_draft(
        tamper_store,
        unit_id=_unit_id(90),
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    ledger = tamper_store.root / "annotation-events.jsonl"
    event = json.loads(ledger.read_bytes())
    event["payload"]["evidence_rationale"] = "tampered"
    ledger.write_bytes(canonical_json_bytes(event) + b"\n")
    with pytest.raises(CurationError) as exc:
        tamper_store.read_events()
    assert exc.value.code == "ANNOTATION_LEDGER_INVALID"


def test_workspace_manifest_is_write_once_and_registry_bound(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    root = tmp_path / "bound-state"
    AnnotationStore(
        root,
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
    )
    altered_principals = tuple(item for item in PRINCIPALS if item[0] != "worker-23")
    other_registry = ReviewerRegistry.load(
        _write_reviewer_registry(tmp_path / "other-reviewers.json", principals=altered_principals)
    )
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            root,
            fake_publication,  # type: ignore[arg-type]
            other_registry,
        )
    assert exc.value.code == "WORKSPACE_BINDING_MISMATCH"


def test_inprocess_http_workflow_never_uses_external_network_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    open_annotation_store: AnnotationStore,
    fake_publication: FakePublication,
) -> None:
    annotation_store = open_annotation_store
    import socket
    import subprocess

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("external capability path was reached")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    app = _create_formal_test_app(fake_publication, annotation_store)
    with InProcessASGIClient(app) as client:
        csrf, _ = _session(client, "action-primary")
        assignments = client.get("/api/assignments")
        assert assignments.status_code == 200
        assert assignments.json()["total"] == 190
        assignment_value = assignments.json()
        assert set(assignment_value["breakdowns"]) == {
            "model",
            "unit_kind",
            "channel",
            "role",
            "state",
        }
        assert sum(assignment_value["breakdowns"]["state"].values()) == 190
        first_assignment = assignment_value["items"][0]
        assert {
            "own_status",
            "workflow_status",
            "state",
            "can_open",
        } <= set(first_assignment)
        assert "model_id" not in first_assignment and "unit_kind" not in first_assignment
        assignment_id = first_assignment["assignment_id"]
        binding = client.get(f"/api/assignments/{assignment_id}/binding")
        assert binding.status_code == 200
        binding_value = binding.json()
        assert set(binding_value) == {
            "assignment_id",
            "channel",
            "review_role",
            "reviewer_identity_sha256",
            "source_packet_sha256",
            "assignment_packet_sha256",
            "visibility_notice",
        }
        assert not {
            "task",
            "evidence",
            "source_records",
            "current_screenshot",
            "natural_action",
        } & set(binding_value)
        packet = client.get(f"/api/assignments/{assignment_id}/packet")
        assert packet.status_code == 200
        assert (
            packet.json()["packet"]["source_packet_sha256"] == binding_value["source_packet_sha256"]
        )
        assert (
            packet.json()["packet"]["assignment_packet_sha256"]
            == binding_value["assignment_packet_sha256"]
        )
        submitted = client.post(
            "/api/reviews/submit",
            json={
                "assignment_id": assignment_id,
                "payload": _browser_action_payload(packet.json()["packet"]),
            },
            headers={"x-g1-csrf-token": csrf, "origin": "http://127.0.0.1"},
        )
        assert submitted.status_code == 200


def test_runtime_annotation_event_validates_against_additive_schema(
    annotation_store: AnnotationStore,
) -> None:
    event = _save_draft(
        annotation_store,
        unit_id=_unit_id(7),
        reviewer_id="action-primary",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    schema = json.loads((G16_SCHEMA_ROOT / "annotation_event.schema.json").read_bytes())
    Draft202012Validator(schema).validate(event)


def test_solo_registry_is_one_real_principal_with_three_nonformal_surfaces(
    tmp_path: Path,
) -> None:
    registry = SoloCuratorRegistry.load(_write_solo_registry(tmp_path / "solo.json"))
    assert registry.principal_id == "one-real-curator"
    assert registry.sha256 == _sha256(registry.canonical_bytes)
    for role in SOLO_REVIEW_ROLES:
        assert registry.authenticate("one-real-curator", role, "solo-curator-secret-0001") == (
            "one-real-curator",
            role,
        )
    with pytest.raises(CurationError) as exc:
        registry.authenticate(
            "one-real-curator", "ACTION_GOLD_SECONDARY", "solo-curator-secret-0001"
        )
    assert exc.value.code == "REVIEWER_AUTHENTICATION_FAILED"
    with pytest.raises(CurationError) as exc:
        registry.authenticate(
            "alias-for-same-person", SOLO_REVIEW_ROLES[0], "solo-curator-secret-0001"
        )
    assert exc.value.code == "REVIEWER_AUTHENTICATION_FAILED"


def test_solo_first_pass_is_separate_nonformal_immutable_authority(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> None:
    store = _open_solo_store(tmp_path, fake_publication)
    unit_id = fake_publication.list_units()[0]["unit_id"]
    assert store.workspace_mode == "SOLO_FIRST_PASS"
    assert store.formal_annotation_open is False
    assert store.first_pass_lock_open is True
    assert store.current_phase() == "ACTION_GOLD"

    event = _submit_review(
        store,  # type: ignore[arg-type]
        unit_id=unit_id,
        reviewer_id="one-real-curator",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    assert event["event_kind"] == "SOLO_FIRST_PASS_LOCKED"
    assert event["review_tier"] == "NON_FORMAL_SOLO_FIRST_PASS"
    for guard in (
        "counts_as_independent_review",
        "formal_resolution_eligible",
        "admission_eligible",
        "promotion_allowed",
        "replay_eligible",
    ):
        assert event[guard] is False
    assert event["cross_channel_exposed"] is True
    assert not (store.root / "annotation-events.jsonl").exists()
    assert (store.root / "solo-first-pass-events.jsonl").is_file()
    assert store.channel_resolution(unit_id, "ACTION_GOLD") is None
    assert store._final_reviews(store.read_events(), unit_id, "ACTION_GOLD") == {}

    with pytest.raises(CurationError) as exc:
        _save_draft(
            store,  # type: ignore[arg-type]
            unit_id=unit_id,
            reviewer_id="one-real-curator",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(x_min=7),
        )
    assert exc.value.code == "SOLO_FIRST_PASS_ALREADY_LOCKED"
    with pytest.raises(CurationError) as exc:
        store.export_workspace_receipt()
    assert exc.value.code == "SOLO_FIRST_PASS_FORMAL_EXPORT_BLOCKED"
    receipt = store.precursor_receipt()
    assert receipt["review_tier"] == "NON_FORMAL_SOLO_FIRST_PASS"
    for guard in (
        "counts_as_independent_review",
        "formal_resolution_eligible",
        "adjudication_eligible",
        "formal_export_eligible",
        "admission_eligible",
        "promotion_allowed",
        "replay_eligible",
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
    ):
        assert receipt[guard] is False
    assert receipt["cross_channel_exposed"] is True
    assert receipt["lock_counts"] == {
        "ACTION_GOLD": 1,
        "TRANSFORMATION": 0,
        "CONSISTENCY_AUDIT": 0,
    }

    manifest = json.loads((store.root / "solo-first-pass-workspace-manifest.json").read_bytes())
    solo_schema = json.loads(
        (G16_SCHEMA_ROOT / "solo_annotation_workspace.schema.json").read_bytes()
    )
    validator = Draft202012Validator(solo_schema)
    validator.validate(manifest)
    assert manifest["workspace_mode"] == "SOLO_FIRST_PASS"
    assert manifest["authority"]["counts_as_independent_review"] is False
    assert manifest["authority"]["formal_export_eligible"] is False
    for field in (
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
    ):
        missing = deepcopy(manifest)
        missing["readiness"].pop(field)
        assert not validator.is_valid(missing)
        enabled = deepcopy(manifest)
        enabled["readiness"][field] = True
        assert not validator.is_valid(enabled)


def test_solo_and_formal_workspace_modes_cannot_share_a_root_or_assignment_key(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
) -> None:
    solo = _open_solo_store(tmp_path, fake_publication)
    assert (solo.root / "solo-assignment-key.bin").is_file()
    assert not (solo.root / "assignment-key.bin").exists()
    with pytest.raises(CurationError) as exc:
        AnnotationStore(
            solo.root,
            fake_publication,  # type: ignore[arg-type]
            reviewer_registry,
        )
    assert exc.value.code == "WORKSPACE_MODE_MISMATCH"
    assert not (solo.root / "assignment-key.bin").exists()

    formal_root = tmp_path / "formal-state"
    AnnotationStore(
        formal_root,
        fake_publication,  # type: ignore[arg-type]
        reviewer_registry,
    )
    solo_registry = SoloCuratorRegistry.load(
        _write_solo_registry(tmp_path / "other-solo-curator.json")
    )
    with pytest.raises(CurationError) as exc:
        SoloFirstPassStore(
            formal_root,
            fake_publication,  # type: ignore[arg-type]
            solo_registry,
        )
    assert exc.value.code == "WORKSPACE_MODE_MISMATCH"
    assert not (formal_root / "solo-assignment-key.bin").exists()


def test_solo_and_formal_bootstrap_atomically_claim_workspace_mode(
    tmp_path: Path,
    fake_publication: FakePublication,
    reviewer_registry: ReviewerRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    root = tmp_path / "concurrent-mode-root"
    root.mkdir(mode=0o700)
    solo_registry = SoloCuratorRegistry.load(
        _write_solo_registry(tmp_path / "concurrent-solo-curator.json")
    )
    barrier = threading.Barrier(2)
    original = AnnotationStore._assert_workspace_mode_isolated

    def synchronized_check(store: AnnotationStore) -> None:
        original(store)
        barrier.wait(timeout=5)

    monkeypatch.setattr(AnnotationStore, "_assert_workspace_mode_isolated", synchronized_check)

    def open_store(mode: str) -> str:
        try:
            if mode == "formal":
                AnnotationStore(
                    root,
                    fake_publication,  # type: ignore[arg-type]
                    reviewer_registry,
                )
            else:
                SoloFirstPassStore(
                    root,
                    fake_publication,  # type: ignore[arg-type]
                    solo_registry,
                )
        except CurationError as exc:
            return exc.code
        return "OPENED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(open_store, ("formal", "solo")))
    assert outcomes == ["OPENED", "WORKSPACE_MODE_MISMATCH"]
    marker = json.loads((root / "workspace-mode.json").read_bytes())
    assert marker["workspace_mode"] in {"FORMAL_DOUBLE_BLIND", "SOLO_FIRST_PASS"}
    assert not (
        (root / "assignment-key.bin").exists() and (root / "solo-assignment-key.bin").exists()
    )


def test_solo_global_stage_gate_requires_all_190_action_locks(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> None:
    store = _open_solo_store(tmp_path, fake_publication)
    units = fake_publication.list_units()
    with pytest.raises(CurationError) as exc:
        _save_draft(
            store,  # type: ignore[arg-type]
            unit_id=units[0]["unit_id"],
            reviewer_id="one-real-curator",
            reviewer_role="TRANSFORMATION_PRIMARY",
            payload=_transformation_payload(),
        )
    assert exc.value.code == "SOLO_STAGE_BLOCKED"

    action_locks = [
        {
            "event_kind": "SOLO_FIRST_PASS_LOCKED",
            "unit_id": unit["unit_id"],
            "channel": "ACTION_GOLD",
        }
        for unit in units
    ]
    assert store._phase_from_events(action_locks) == "TRANSFORMATION"
    transformation_locks = [
        {
            "event_kind": "SOLO_FIRST_PASS_LOCKED",
            "unit_id": unit["unit_id"],
            "channel": "TRANSFORMATION",
        }
        for unit in units
    ]
    assert store._phase_from_events([*action_locks, *transformation_locks]) == ("CONSISTENCY_AUDIT")
    consistency_locks = [
        {
            "event_kind": "SOLO_FIRST_PASS_LOCKED",
            "unit_id": unit["unit_id"],
            "channel": "CONSISTENCY_AUDIT",
        }
        for unit in units
    ]
    assert (
        store._phase_from_events([*action_locks, *transformation_locks, *consistency_locks])
        == "COMPLETE"
    )


def test_solo_lock_transport_retry_is_idempotent_after_phase_advances(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> None:
    fake_publication._units = [fake_publication._units[0]]
    store = _open_solo_store(tmp_path, fake_publication)
    unit_id = fake_publication.list_units()[0]["unit_id"]
    first = _submit_review(
        store,  # type: ignore[arg-type]
        unit_id=unit_id,
        reviewer_id="one-real-curator",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    assert store.current_phase() == "TRANSFORMATION"
    retry = _submit_review(
        store,  # type: ignore[arg-type]
        unit_id=unit_id,
        reviewer_id="one-real-curator",
        reviewer_role="ACTION_GOLD_PRIMARY",
        payload=_action_payload(),
    )
    assert retry == first
    assert len(store.read_events()) == 1
    with pytest.raises(CurationError) as exc:
        _submit_review(
            store,  # type: ignore[arg-type]
            unit_id=unit_id,
            reviewer_id="one-real-curator",
            reviewer_role="ACTION_GOLD_PRIMARY",
            payload=_action_payload(x_min=7),
        )
    assert exc.value.code == "SOLO_FIRST_PASS_ALREADY_LOCKED"
    assert len(store.read_events()) == 1


def test_solo_http_lock_transport_retry_is_idempotent_after_phase_advances(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> None:
    fake_publication._units = [fake_publication._units[0]]
    store = _open_solo_store(tmp_path, fake_publication)
    app = create_app(fake_publication, store)  # type: ignore[arg-type]
    with InProcessASGIClient(app) as client:
        session = client.post(
            "/api/session",
            json={
                "reviewer_id": "one-real-curator",
                "role": "ACTION_GOLD_PRIMARY",
                "access_secret": "solo-curator-secret-0001",
            },
        )
        assert session.status_code == 200
        csrf = session.json()["csrf_token"]
        assignment_id = client.get("/api/assignments").json()["items"][0]["assignment_id"]
        packet = client.get(f"/api/assignments/{assignment_id}/packet")
        assert packet.status_code == 200
        body = {
            "assignment_id": assignment_id,
            "payload": _browser_action_payload(packet.json()["packet"]),
        }
        headers = {
            "origin": "http://127.0.0.1",
            "x-g1-csrf-token": csrf,
        }
        first = client.post("/api/solo/lock", json=body, headers=headers)
        assert first.status_code == 200
        assert store.current_phase() == "TRANSFORMATION"
        retry = client.post("/api/solo/lock", json=body, headers=headers)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        assert len(store.read_events()) == 1


def test_solo_http_surface_labels_authority_and_blocks_formal_endpoints(
    tmp_path: Path,
    fake_publication: FakePublication,
) -> None:
    store = _open_solo_store(tmp_path, fake_publication)
    app = create_app(fake_publication, store)  # type: ignore[arg-type]
    with InProcessASGIClient(app) as client:
        config = client.get("/api/config").json()
        assert config["workspace_mode"] == "SOLO_FIRST_PASS"
        assert config["solo_first_pass"] is True
        assert config["formal_annotation_open"] is False
        assert config["first_pass_lock_open"] is True
        assert config["roles"] == list(SOLO_REVIEW_ROLES)
        assert config["review_authority"] == {
            "counts_as_independent_review": False,
            "formal_resolution_eligible": False,
            "adjudication_eligible": False,
            "formal_export_eligible": False,
            "admission_eligible": False,
            "promotion_allowed": False,
            "replay_eligible": False,
            "cross_channel_exposed": True,
        }
        session = client.post(
            "/api/session",
            json={
                "reviewer_id": "one-real-curator",
                "role": "ACTION_GOLD_PRIMARY",
                "access_secret": "solo-curator-secret-0001",
            },
        )
        assert session.status_code == 200
        csrf = session.json()["csrf_token"]
        assignments = client.get("/api/assignments").json()
        assert assignments["total"] == 190
        assert assignments["current_phase"] == "ACTION_GOLD"
        blocked = client.post(
            "/api/reviews/submit",
            json={"assignment_id": assignments["items"][0]["assignment_id"], "payload": {}},
            headers={"origin": "http://127.0.0.1", "x-g1-csrf-token": csrf},
        )
        assert blocked.status_code == 400
        assert blocked.json()["error"] == "SOLO_FIRST_PASS_FORMAL_SUBMISSION_BLOCKED"

        transformation_session = client.post(
            "/api/session",
            json={
                "reviewer_id": "one-real-curator",
                "role": "TRANSFORMATION_PRIMARY",
                "access_secret": "solo-curator-secret-0001",
            },
        )
        assert transformation_session.status_code == 200
        transformation_csrf = transformation_session.json()["csrf_token"]
        future_assignment = client.get("/api/assignments").json()["items"][0]["assignment_id"]
        future_lock = client.post(
            "/api/solo/lock",
            json={"assignment_id": future_assignment, "payload": {}},
            headers={
                "origin": "http://127.0.0.1",
                "x-g1-csrf-token": transformation_csrf,
            },
        )
        assert future_lock.status_code == 400
        assert future_lock.json()["error"] == "SOLO_STAGE_BLOCKED"
        html = (
            MOBILEWORLD_SOURCE_ROOT / "mobile_world/offline/gold_curation/web/index.html"
        ).read_text()
        assert "SOLO FIRST PASS · 非正式单人初筛" in html
        assert "不计独立 review" in html
        assert 'class="guide-card accent-green formal-only"' in html
        assert 'id="profile-dialog-eyebrow"' in html
        script = (
            MOBILEWORLD_SOURCE_ROOT / "mobile_world/offline/gold_curation/web/app.js"
        ).read_text()
        assert '$$(".formal-only").forEach((element) => { element.hidden = true; });' in script
        assert '$$(".solo-only").forEach((element) => { element.hidden = false; });' in script
        assert '$("#profile-dialog-eyebrow").textContent = "单人非正式初筛身份";' in script
        assert 'state.config.solo_first_pass ? "初筛已锁" : "已提交"' in script
        assert "同一真实身份按 Action → Transformation → Consistency" in script
