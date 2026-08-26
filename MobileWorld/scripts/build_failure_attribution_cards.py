#!/usr/bin/env python3
"""Build two-phase failure-attribution cards from frozen audit artifacts.

This command never invokes a reviewer.  ``phase-a`` is outcome-blind and has no
outcome or raw-source argument.  ``phase-b`` refuses to open outcomes until a
complete Phase-A primary/secondary/material-adjudication freeze validates
against the exact regenerated Phase-A card set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from mobile_world.offline.failure_attribution import (
    FailureAttributionError,
    SourceBundle,
    build_phase_a_bundle,
    build_phase_b_bundle,
    load_phase_a_resolution,
    write_phase_a_bundle,
    write_phase_b_bundle,
)
from mobile_world.offline.motivation_review import canonical_json_bytes


def _bundle_argument(value: str) -> tuple[str, Path]:
    model_id, separator, path = value.partition("=")
    if not separator or not model_id or not path:
        raise argparse.ArgumentTypeError("--bundle must be MODEL_ID=/absolute/bundle/root")
    return model_id, Path(path)


def _source_bundles(values: list[tuple[str, Path]]) -> tuple[SourceBundle, ...]:
    return tuple(SourceBundle.from_root(model_id, root) for model_id, root in values)


def _read_single_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailureAttributionError("artifact_json", str(exc), path=str(path)) from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise FailureAttributionError(
            "artifact_json",
            "artifact must be one canonical JSON object",
            path=str(path),
        )
    return value


def _assert_frozen_phase_a(phase_a_dir: Path, rebuilt: Any) -> None:
    manifest_path = phase_a_dir / "manifest.json"
    cards_path = phase_a_dir / "cards.jsonl"
    stored_manifest = _read_single_json(manifest_path)
    if stored_manifest != rebuilt.manifest:
        raise FailureAttributionError(
            "phase_a_manifest_drift",
            "stored Phase-A manifest differs from deterministic regeneration",
            path=str(manifest_path),
        )
    expected_cards = b"".join(canonical_json_bytes(card) for card in rebuilt.cards)
    actual_cards = cards_path.read_bytes()
    if actual_cards != expected_cards:
        raise FailureAttributionError(
            "phase_a_card_drift",
            "stored Phase-A cards differ from deterministic regeneration",
            path=str(cards_path),
            context={
                "expected_sha256": hashlib.sha256(expected_cards).hexdigest(),
                "actual_sha256": hashlib.sha256(actual_cards).hexdigest(),
            },
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    phase_a = subparsers.add_parser("phase-a", help="build outcome-blind Phase-A cards")
    phase_a.add_argument(
        "--bundle",
        action="append",
        required=True,
        type=_bundle_argument,
        metavar="MODEL_ID=ROOT",
        help="frozen model audit bundle; repeat once per model",
    )
    phase_a.add_argument("--output-dir", type=Path, required=True)
    phase_a.add_argument("--dry-run", action="store_true")

    phase_b = subparsers.add_parser(
        "phase-b", help="join outcomes/evaluator only after Phase-A reviews freeze"
    )
    phase_b.add_argument(
        "--bundle",
        action="append",
        required=True,
        type=_bundle_argument,
        metavar="MODEL_ID=ROOT",
    )
    phase_b.add_argument("--phase-a-dir", type=Path, required=True)
    phase_b.add_argument("--phase-a-freeze-dir", type=Path, required=True)
    phase_b.add_argument("--source-base", type=Path, required=True)
    phase_b.add_argument("--output-dir", type=Path, required=True)
    phase_b.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        sources = _source_bundles(args.bundle)
        phase_a_bundle = build_phase_a_bundle(sources)
        if args.command == "phase-a":
            result: dict[str, Any] = {
                "phase_a": write_phase_a_bundle(
                    phase_a_bundle, args.output_dir, dry_run=args.dry_run
                ),
                "counts": phase_a_bundle.manifest["counts"],
            }
        else:
            _assert_frozen_phase_a(args.phase_a_dir.resolve(strict=True), phase_a_bundle)
            phase_a_resolution = load_phase_a_resolution(
                args.phase_a_freeze_dir.resolve(strict=True), phase_a_bundle
            )
            phase_b_bundle = build_phase_b_bundle(
                phase_a_bundle,
                phase_a_resolution,
                source_base=args.source_base,
            )
            result = {
                "phase_b": write_phase_b_bundle(
                    phase_b_bundle, args.output_dir, dry_run=args.dry_run
                ),
                "counts": phase_b_bundle.manifest["counts"],
            }
    except (FailureAttributionError, OSError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "OK", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
