from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator
from openai import OpenAI

from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    EvidencePacketSchemaSnapshotV1,
    ProposalSchemaSnapshotV1,
)
from mobile_world.runtime.sentinel.r2_4.lean_rubric import (
    DirectOpenAIRubricProviderV1,
    LeanOpenAIRubricBackendV1,
    lean_rubric_generate_schema,
    lean_rubric_track_schema,
)
from mobile_world.runtime.sentinel.schemas import read_sentinel_schema_bytes


def test_ordinary_lean_runtime_loads_its_schemas_from_the_mobile_world_package() -> None:
    packaged = {
        ("r2_2", "evidence_packet.v1.schema.json"),
        ("r2_2", "policy_proposal.v1.schema.json"),
        ("r2_3", "rubric.v1.schema.json"),
        ("r2_3", "tracker_output.v1.schema.json"),
        ("r2_3", "tracking_packet.v1.schema.json"),
        ("r2_4", "rubric_generate_output.v1.schema.json"),
        ("r2_4", "rubric_track_output.v1.schema.json"),
    }
    raw_by_key: dict[tuple[str, str], bytes] = {}
    for key in packaged:
        raw = read_sentinel_schema_bytes(*key)
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
        raw_by_key[key] = raw

    assert ProposalSchemaSnapshotV1.from_checked_in().as_dict() == json.loads(
        raw_by_key[("r2_2", "policy_proposal.v1.schema.json")]
    )
    assert EvidencePacketSchemaSnapshotV1.from_checked_in().as_dict() == json.loads(
        raw_by_key[("r2_2", "evidence_packet.v1.schema.json")]
    )
    assert lean_rubric_generate_schema().as_dict() == json.loads(
        raw_by_key[("r2_4", "rubric_generate_output.v1.schema.json")]
    )
    assert lean_rubric_track_schema().as_dict() == json.loads(
        raw_by_key[("r2_4", "rubric_track_output.v1.schema.json")]
    )

    client = OpenAI(api_key="offline-test", base_url="http://127.0.0.1:1/v1", max_retries=0)
    try:
        descriptor = LeanOpenAIRubricBackendV1(
            provider=DirectOpenAIRubricProviderV1(client=client, timeout_seconds=1.0)
        ).descriptor
    finally:
        client.close()

    assert (
        descriptor.rubric_schema_sha256
        == hashlib.sha256(raw_by_key[("r2_3", "rubric.v1.schema.json")]).hexdigest()
    )
    assert (
        descriptor.tracking_packet_schema_sha256
        == hashlib.sha256(raw_by_key[("r2_3", "tracking_packet.v1.schema.json")]).hexdigest()
    )
    assert (
        descriptor.tracker_schema_sha256
        == hashlib.sha256(raw_by_key[("r2_3", "tracker_output.v1.schema.json")]).hexdigest()
    )
