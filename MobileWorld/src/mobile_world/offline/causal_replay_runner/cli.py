"""No-send CLI for G1.4 scheduling and capsule-binding inspection."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_json_bytes
from mobile_world.offline.causal_replay_runner.capsule_loader import load_replay_capsule
from mobile_world.offline.causal_replay_runner.contracts import ReplayRunnerError, UnitKind
from mobile_world.offline.causal_replay_runner.live_preparation import load_live_preparation
from mobile_world.offline.causal_replay_runner.schedule import schedule_for_unit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    schedule = subparsers.add_parser("schedule", help="print the locked six-block arm schedule")
    schedule.add_argument("--unit-kind", choices=[item.value for item in UnitKind], required=True)
    schedule.add_argument("--unit-id", required=True)
    schedule.add_argument("--model-id", required=True)
    validate = subparsers.add_parser(
        "validate-capsule", help="load one capsule using a prior SOURCE_BOUND receipt"
    )
    validate.add_argument("--capsule-root", required=True)
    validate.add_argument("--unit-id", required=True)
    validate.add_argument("--directory-receipt", required=True)
    subparsers.add_parser(
        "live-status", help="report the intentionally deferred live/GPU execution boundary"
    )
    prepare_live = subparsers.add_parser(
        "prepare-live-code",
        help="validate frozen live-proof inputs without probing or starting any resource",
    )
    prepare_live.add_argument("--model-config-manifest", required=True)
    return parser


def _load_receipt(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayRunnerError(
            "DIRECTORY_RECEIPT_INVALID", "receipt is not readable JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ReplayRunnerError("DIRECTORY_RECEIPT_INVALID", "receipt must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output: dict[str, JsonValue]
    try:
        if args.command == "schedule":
            entries = schedule_for_unit(
                unit_kind=UnitKind(args.unit_kind),
                unit_id=args.unit_id,
                model_id=args.model_id,
            )
            output = {
                "provider_invocation_allowed": False,
                "gpu_used": False,
                "entries": [entry.to_dict() for entry in entries],
            }
        elif args.command == "validate-capsule":
            capsule = load_replay_capsule(
                args.capsule_root,
                unit_id=args.unit_id,
                directory_receipt=_load_receipt(args.directory_receipt),
            )
            output = {
                "valid": True,
                "runtime_projection_only": True,
                "binding": capsule.public_binding(),
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
            }
        elif args.command == "prepare-live-code":
            output = load_live_preparation(args.model_config_manifest).to_dict()
        else:
            output = {
                "status": "DEFERRED_PENDING_OWNER_GPU_RESOURCE_REVIEW",
                "live_code_prepared": True,
                "static_model_configuration_validated": False,
                "execution_ready": False,
                "live_transport_validation_complete": False,
                "live_history_codec_ready": False,
                "curated_transformations_ready": False,
                "run_ready_seal_present": False,
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
                "formal_replay_ready": False,
                "client_factory_invoked": False,
                "network_used": False,
                "subprocess_started": False,
                "gpu_probed": False,
                "gpu_used": False,
                "model_loaded": False,
                "provider_invoked": False,
                "replay_executed": False,
                "generated_action_executed": False,
            }
    except ReplayRunnerError as exc:
        print(
            canonical_json_bytes(
                {
                    "valid": False,
                    "error_code": exc.code,
                    "message": str(exc),
                    "execution_ready": False,
                    "live_transport_validation_complete": False,
                    "live_history_codec_ready": False,
                    "curated_transformations_ready": False,
                    "run_ready_seal_present": False,
                    "provider_invocation_allowed": False,
                    "treatment_response_generation_allowed": False,
                    "formal_replay_ready": False,
                    "client_factory_invoked": False,
                    "network_used": False,
                    "subprocess_started": False,
                    "gpu_probed": False,
                    "gpu_used": False,
                    "model_loaded": False,
                    "provider_invoked": False,
                    "replay_executed": False,
                    "generated_action_executed": False,
                }
            ).decode()
        )
        return 2
    print(canonical_json_bytes(cast(JsonValue, output)).decode())
    return 0
