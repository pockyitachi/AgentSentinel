from __future__ import annotations

from pathlib import Path

from mobile_world.runtime.sentinel.r2_4.topology_cpu import (
    produce_cpu_fake_topology_artifact_bytes,
)


def cpu_topology_artifact_bytes() -> bytes:
    return produce_cpu_fake_topology_artifact_bytes(
        repository_root=Path(__file__).resolve().parents[4]
    )


def write_cpu_topology_artifact(path: Path) -> tuple[str, int]:
    import hashlib

    raw = cpu_topology_artifact_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)
