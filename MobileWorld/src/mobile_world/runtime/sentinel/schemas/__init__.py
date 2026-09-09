"""Package-local JSON Schemas required by the ordinary Sentinel runtime."""

from __future__ import annotations

from importlib.resources import files

_PACKAGED_SCHEMAS = frozenset(
    {
        ("r2_2", "evidence_packet.v1.schema.json"),
        ("r2_2", "policy_proposal.v1.schema.json"),
        ("r2_3", "rubric.v1.schema.json"),
        ("r2_3", "tracker_output.v1.schema.json"),
        ("r2_3", "tracking_packet.v1.schema.json"),
        ("r2_4", "rubric_generate_output.v1.schema.json"),
        ("r2_4", "rubric_track_output.v1.schema.json"),
    }
)


def read_sentinel_schema_bytes(version: str, filename: str) -> bytes:
    """Read one explicitly packaged runtime schema without repository-root discovery."""

    if type(version) is not str or type(filename) is not str:
        raise TypeError("schema version and filename must be exact strings")
    if (version, filename) not in _PACKAGED_SCHEMAS:
        raise ValueError("unknown packaged Sentinel schema")
    return files(__package__).joinpath(version, filename).read_bytes()


__all__ = ["read_sentinel_schema_bytes"]
