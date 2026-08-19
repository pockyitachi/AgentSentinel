"""Deterministic, causally valid replay for curated Seed baseline cases.

The replay connects the benchmark-neutral Sentinel gate to the Seed host
adapter.  Decisions in the bundled v2 file are manually reviewed gold labels;
they exercise the runtime boundary and are never described as automatic
deployment predictions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .contracts import Claim, EpistemicStatus, EvidenceRef, GateOperation, verdict_for
from .history_filter import filter_history
from .seed_adapter import adapt_seed_history, extract_seed_records


FIXTURE_SCHEMA = "seed-baseline-replay-fixture/v1"
DECISION_SCHEMA = "sentinel-curated-gate-decisions/v2"


class ReplayValidationError(ValueError):
    """Raised when a fixture cannot be replayed without ambiguity or leakage."""


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str:
    if not path.is_file():
        raise ReplayValidationError(f"missing replay artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_id(path: Path) -> str:
    match = re.search(r"-0-(\d+)\.png$", path.name)
    return f"S{match.group(1)}" if match else path.name


def _history_for_seed(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "step_id": f"s{int(item['step'])}",
            "raw_response": item["prediction"],
        }
        for item in fixture["history_responses_before_target"]
    ]


def _historical_observations(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load only observations whose path and digest were frozen in the fixture."""

    declared = fixture.get("historical_observations_before_target", [])
    history = fixture["history_responses_before_target"]
    if len(declared) != len(history):
        raise ReplayValidationError(
            "fixture must freeze one historical observation per history response"
        )

    references: list[dict[str, Any]] = []
    for expected, item in zip(history, declared, strict=True):
        step = int(item["step"])
        if step != int(expected["step"]):
            raise ReplayValidationError("historical observation step alignment changed")
        path = Path(item["path"]).resolve()
        actual_hash = _file_hash(path)
        if actual_hash != item["sha256"]:
            raise ReplayValidationError(f"historical screenshot hash changed: {path}")
        references.append(
            {"screenshot_ref": str(path), "step": step, "sha256": actual_hash}
        )
    return references


def _fixture_evidence_index(fixture: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(Path(item["path"]).resolve()): item
        for item in fixture["evidence_screenshots"]
    }


def _evidence_for_target(
    fixture: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    replay_step: int,
) -> tuple[EvidenceRef, ...]:
    fixture_evidence = _fixture_evidence_index(fixture)
    evidence_refs: list[EvidenceRef] = []
    for raw_path in target.get("evidence_refs", []):
        path = Path(raw_path).resolve()
        item = fixture_evidence.get(str(path))
        if item is None:
            raise ReplayValidationError(
                f"{target['claim_id']}: evidence is not declared by fixture: {path}"
            )
        evidence_step = int(item["step"])
        if evidence_step > replay_step:
            raise ReplayValidationError(
                f"{target['claim_id']}: future evidence S{evidence_step} is unavailable "
                f"before decision step {replay_step}"
            )
        actual_hash = _file_hash(path)
        if actual_hash != item["sha256"]:
            raise ReplayValidationError(f"evidence hash changed: {path}")
        evidence_refs.append(
            EvidenceRef(
                evidence_id=_evidence_id(path),
                source_type="screenshot",
                description=str(item["purpose"]),
                direct=True,
                step_index=evidence_step,
                locator=str(path),
                metadata={
                    "sha256": actual_hash,
                    "curated_gold": True,
                    "available_at_step": evidence_step,
                },
            )
        )
    return tuple(evidence_refs)


def _claim_from_target(
    fixture: Mapping[str, Any],
    target: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    *,
    replay_step: int,
) -> Claim:
    record_id = f"s{int(target['record_step'])}"
    records = [item for item in history if item["step_id"] == record_id]
    if len(records) != 1:
        raise ReplayValidationError(
            f"{target['claim_id']}: expected exactly one record {record_id}"
        )

    raw = str(records[0]["raw_response"])
    anchor = str(target["anchor_text"])
    anchor_start = raw.find(anchor)
    if anchor_start < 0 or raw.find(anchor, anchor_start + 1) >= 0:
        raise ReplayValidationError(
            f"{target['claim_id']}: anchor_text must occur exactly once"
        )

    edit_scope = str(target["edit_scope"]).upper()
    if edit_scope == "WHOLE_RECORD":
        start, end, claim_text = 0, len(raw), raw
    elif edit_scope == "SPAN":
        start, end, claim_text = anchor_start, anchor_start + len(anchor), anchor
    else:
        raise ReplayValidationError(
            f"{target['claim_id']}: unsupported edit_scope {edit_scope}"
        )

    operation = GateOperation(target["operation"])
    correction = target.get("correction")
    if operation is GateOperation.REPLACE and not str(correction or "").strip():
        raise ReplayValidationError(f"{target['claim_id']}: REPLACE needs correction")

    return Claim(
        claim_id=str(target["claim_id"]),
        record_id=record_id,
        text=claim_text,
        start=start,
        end=end,
        epistemic_status=EpistemicStatus(target["epistemic_status"]),
        verdict=verdict_for(operation),
        correction=str(correction) if correction is not None else None,
        evidence_refs=_evidence_for_target(
            fixture, target, replay_step=replay_step
        ),
        rationale=str(
            target.get("rationale")
            or "curated, screenshot-grounded replay decision"
        ),
        confidence=1.0,
    )


def _forbidden_terms_present(value: Any, forbidden: Sequence[str]) -> list[str]:
    rendered = json.dumps(value, ensure_ascii=False).casefold()
    return [term for term in forbidden if term.casefold() in rendered]


def run_replay_fixture(
    fixture: Mapping[str, Any], decision: Mapping[str, Any], *, history_n: int = 3
) -> dict[str, Any]:
    """Run all curated operations for one replay point through the Seed gate."""

    if fixture["fixture_id"] != decision["fixture_id"]:
        raise ReplayValidationError("fixture_id mismatch")
    if fixture["provenance"]["run_group"] != "seed_baseline":
        raise ReplayValidationError("only seed_baseline fixtures are accepted")

    trajectory_path = Path(fixture["provenance"]["trajectory_path"])
    if _file_hash(trajectory_path) != fixture["provenance"]["trajectory_sha256"]:
        raise ReplayValidationError(f"trajectory hash changed: {trajectory_path}")

    replay_step = int(fixture["replay_point"]["target_step"])
    history = _history_for_seed(fixture)
    history_snapshot = deepcopy(history)
    input_hash_before = _json_hash(history)

    targets = decision.get("targets", [])
    if not isinstance(targets, list) or not targets:
        raise ReplayValidationError("each replay decision needs non-empty targets")
    claims = [
        _claim_from_target(
            fixture, target, history, replay_step=replay_step
        )
        for target in targets
    ]
    if len({claim.claim_id for claim in claims}) != len(claims):
        raise ReplayValidationError("claim_id values must be unique within a fixture")

    forbidden = [str(term) for term in decision.get("forbidden_texts", [])]
    if not forbidden:
        raise ReplayValidationError("each replay decision needs forbidden_texts")
    missing_from_raw = [
        term
        for term in forbidden
        if term.casefold() not in json.dumps(history, ensure_ascii=False).casefold()
    ]
    if missing_from_raw:
        raise ReplayValidationError(
            f"forbidden_texts do not occur in raw history: {missing_from_raw}"
        )

    generic_records = extract_seed_records(history)
    core_output = filter_history(
        generic_records,
        claims,
        task_id=str(fixture["task_name"]),
        step_index=replay_step,
    )

    historical_observations = _historical_observations(fixture)
    current_observation = {
        "screenshot_ref": fixture["current_observation_path"],
        "step": replay_step,
        "sha256": fixture["current_observation_sha256"],
    }
    current_path = Path(current_observation["screenshot_ref"])
    if _file_hash(current_path) != current_observation["sha256"]:
        raise ReplayValidationError(f"current observation hash changed: {current_path}")

    seed_output = adapt_seed_history(
        history,
        historical_observations,
        current_observation,
        core_output.operations,
        history_n=history_n,
    )

    input_hash_after = _json_hash(history)
    if history != history_snapshot or input_hash_before != input_hash_after:
        raise ReplayValidationError("Sentinel mutated caller-owned raw history")
    if len(core_output.operations) != len(claims):
        raise ReplayValidationError("core did not return one operation per claim")
    if len(seed_output.operation_results) != len(claims):
        raise ReplayValidationError("host did not return one result per claim")
    if not all(operation.applied for operation in core_output.operations):
        raise ReplayValidationError("a curated core operation failed closed")
    if not all(result.applied for result in seed_output.operation_results):
        details = [result.detail for result in seed_output.operation_results]
        raise ReplayValidationError(f"a curated host operation failed: {details}")

    filtered_by_id = {
        item.step_id: item.raw_response
        for item in seed_output.filtered_assistant_history
    }
    original_by_id = {item["step_id"]: item["raw_response"] for item in history}
    target_ids = list(dict.fromkeys(claim.record_id for claim in claims))
    records_before = {record_id: original_by_id[record_id] for record_id in target_ids}
    records_after = {record_id: filtered_by_id.get(record_id) for record_id in target_ids}

    # This is a host-composition fragment, not a full API request: local image
    # refs still need resolving and the unchanged system/task/tools envelope is
    # intentionally absent from offline fixtures.
    host_fragment = list(seed_output.actor_messages)
    remaining_forbidden = _forbidden_terms_present(host_fragment, forbidden)
    if remaining_forbidden:
        raise ReplayValidationError(
            f"invalid premise still enters host history fragment: {remaining_forbidden}"
        )
    if any(
        operation.operation is GateOperation.REPLACE
        for operation in core_output.operations
    ) and seed_output.correction_user_block is None:
        raise ReplayValidationError("REPLACE did not emit a Sentinel correction block")

    retained_refs = [
        str(item.get("screenshot_ref") if isinstance(item, Mapping) else item)
        for item in seed_output.retained_observation_refs
    ]
    claim_scope_by_id = {
        str(target["claim_id"]): str(target["edit_scope"]).upper()
        for target in targets
    }

    return {
        "schema_version": "sentinel-seed-replay-result/v2",
        "fixture_id": fixture["fixture_id"],
        "task_name": fixture["task_name"],
        "task_goal": fixture["task_goal"],
        "provenance": deepcopy(fixture["provenance"]),
        "sentinel_input": {
            "target_step": replay_step,
            "history_policy": {
                "assistant_text": "all prior Seed responses",
                "image_observations": f"latest {history_n} including current",
            },
            "history_record_count": len(history),
            "current_observation": current_observation,
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "record_id": claim.record_id,
                    "edit_scope": claim_scope_by_id[claim.claim_id],
                    "span": [claim.start, claim.end],
                    "text": claim.text,
                    "epistemic_status": claim.epistemic_status.value,
                    "requested_operation": claim.gate_operation.value,
                    "evidence_refs": [asdict(item) for item in claim.evidence_refs],
                }
                for claim in claims
            ],
            "forbidden_texts": forbidden,
        },
        "sentinel_output": {
            "operations_applied": len(seed_output.operation_results),
            "filtered_history_for_next_prompt": list(
                seed_output.filtered_history_responses
            ),
            "host_history_fragment": {
                "messages": host_fragment,
                "requires_unchanged_host_envelope": True,
                "local_image_refs_resolved": False,
                "not_a_complete_api_request": True,
            },
            "correction_block": deepcopy(seed_output.correction_user_block),
            "retained_observation_refs": retained_refs,
            "operation_results": [
                asdict(item) for item in seed_output.operation_results
            ],
            "warnings": list(core_output.warnings),
        },
        "audit": {
            "manual_gold_decision": True,
            "automatic_verifier_used": False,
            "target_records_before": records_before,
            "target_records_after": records_after,
            "forbidden_texts": forbidden,
            "forbidden_texts_absent_from_host_fragment": True,
            "raw_history_sha256_before": input_hash_before,
            "raw_history_sha256_after": input_hash_after,
            "caller_history_unchanged": input_hash_before == input_hash_after,
        },
        "recorded_baseline_target": deepcopy(fixture["target"]),
    }


def _validate_bundle_headers(
    fixture_bundle: Mapping[str, Any], decision_bundle: Mapping[str, Any]
) -> None:
    if fixture_bundle.get("schema_version") != FIXTURE_SCHEMA:
        raise ReplayValidationError("unsupported fixture schema")
    if decision_bundle.get("schema_version") != DECISION_SCHEMA:
        raise ReplayValidationError("unsupported decision schema")
    scope = decision_bundle.get("scope")
    if not isinstance(scope, Mapping):
        raise ReplayValidationError("decision bundle scope is missing")
    if scope.get("run_group") != "seed_baseline":
        raise ReplayValidationError("decision bundle is not seed_baseline")
    if scope.get("deployment_prediction") is not False:
        raise ReplayValidationError("curated replay must set deployment_prediction=false")
    if "manual" not in str(scope.get("decision_source", "")).casefold():
        raise ReplayValidationError("decision_source must explicitly be manual")
    for field in ("annotation_method", "review_status", "created_at"):
        if not str(scope.get(field, "")).strip():
            raise ReplayValidationError(f"decision scope is missing {field}")


def run_replay_bundle(
    fixture_bundle: Mapping[str, Any],
    decision_bundle: Mapping[str, Any],
    *,
    history_n: int = 3,
) -> dict[str, Any]:
    """Run every curated decision and require a one-to-one fixture match."""

    _validate_bundle_headers(fixture_bundle, decision_bundle)
    fixture_items = fixture_bundle.get("fixtures", [])
    decision_items = decision_bundle.get("decisions", [])
    fixtures = {item["fixture_id"]: item for item in fixture_items}
    decisions = {item["fixture_id"]: item for item in decision_items}
    if len(fixtures) != len(fixture_items) or len(decisions) != len(decision_items):
        raise ReplayValidationError("fixture_id values must be unique")
    if not fixtures or fixtures.keys() != decisions.keys():
        raise ReplayValidationError(
            "fixture and decision bundles must contain the same non-empty fixture_id set"
        )

    results = [
        run_replay_fixture(
            fixtures[fixture_id], decisions[fixture_id], history_n=history_n
        )
        for fixture_id in fixtures
    ]
    total_operations = sum(
        item["sentinel_output"]["operations_applied"] for item in results
    )
    return {
        "schema_version": "sentinel-seed-replay-bundle-result/v2",
        "scope": {
            "run_group": "seed_baseline",
            "automatic_verifier_used": False,
            "decision_source": decision_bundle["scope"]["decision_source"],
            "decision_bundle_sha256": _json_hash(decision_bundle),
            "history_n": history_n,
        },
        "summary": {
            "fixture_count": len(results),
            "fixtures_applied": sum(
                item["sentinel_output"]["operations_applied"] > 0
                for item in results
            ),
            "operations_applied": total_operations,
            "caller_histories_unchanged": all(
                item["audit"]["caller_history_unchanged"] for item in results
            ),
            "forbidden_facts_absent_from_host_fragments": all(
                item["audit"]["forbidden_texts_absent_from_host_fragment"]
                for item in results
            ),
        },
        "results": results,
    }


def load_and_run_replay_bundle(
    fixtures_path: str | Path,
    decisions_path: str | Path,
    *,
    history_n: int = 3,
) -> dict[str, Any]:
    """Load two JSON bundles and run the deterministic replay."""

    with Path(fixtures_path).open("r", encoding="utf-8") as handle:
        fixtures = json.load(handle)
    with Path(decisions_path).open("r", encoding="utf-8") as handle:
        decisions = json.load(handle)
    return run_replay_bundle(fixtures, decisions, history_n=history_n)


__all__ = [
    "ReplayValidationError",
    "load_and_run_replay_bundle",
    "run_replay_bundle",
    "run_replay_fixture",
]
