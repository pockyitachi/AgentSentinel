#!/usr/bin/env python3
"""Run the curated Seed baseline replay through Sentinel end to end."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.replay import load_and_run_replay_bundle  # noqa: E402


DEFAULT_FIXTURES = PROJECT_ROOT / "fixtures" / "seed_baseline_replay_v1.json"
DEFAULT_DECISIONS = PROJECT_ROOT / "fixtures" / "curated_gate_decisions_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "seed_replay_demo_v1.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--history-n", type=int, default=3)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and print the summary without writing output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = load_and_run_replay_bundle(
        args.fixtures,
        args.decisions,
        history_n=args.history_n,
    )
    if not args.check:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
