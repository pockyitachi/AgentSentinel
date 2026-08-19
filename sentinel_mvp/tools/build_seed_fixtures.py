#!/usr/bin/env python3
"""Build deterministic replay fixtures from five Seed baseline trajectories.

The source log tree is treated as read-only.  Fixtures contain absolute paths and
SHA-256 digests for screenshots; image bytes are never copied into the workspace.

Examples:
    python build_seed_fixtures.py --mode check
    python build_seed_fixtures.py --mode stdout
    python build_seed_fixtures.py --mode write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_INPUT_ROOT = Path(
    "/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline"
)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "seed_baseline_replay_v1.json"
)
SCHEMA_VERSION = "seed-baseline-replay-fixture/v1"


class FixtureValidationError(RuntimeError):
    """Raised when source data cannot satisfy the fixture contract."""


@dataclass(frozen=True)
class EvidenceSpec:
    step: int
    purpose: str


@dataclass(frozen=True)
class ReplaySpec:
    fixture_id: str
    task_name: str
    source_step: int
    target_step: int
    evidence: tuple[EvidenceSpec, ...]


REPLAY_SPECS = (
    ReplaySpec(
        fixture_id="check_conference_and_send_sms_s10_to_s11",
        task_name="CheckConferenceAndSendSmsTask1",
        source_step=10,
        target_step=11,
        evidence=(
            EvidenceSpec(3, "direct_refutation_of_source_claim"),
            EvidenceSpec(12, "recorded_downstream_outcome"),
        ),
    ),
    ReplaySpec(
        fixture_id="schedule_lunch_via_sms_s16_to_s17",
        task_name="ScheduleLunchViaSmsTask",
        source_step=16,
        target_step=17,
        evidence=(
            EvidenceSpec(4, "direct_refutation_of_source_claim"),
            EvidenceSpec(16, "source_state_before_action"),
            EvidenceSpec(18, "recorded_downstream_outcome"),
        ),
    ),
    ReplaySpec(
        fixture_id="mastodon_adjust_toots_s32_to_s33",
        task_name="MastodonAdjustTootsTask",
        source_step=32,
        target_step=33,
        evidence=(
            EvidenceSpec(24, "earlier_control_state"),
            EvidenceSpec(33, "direct_refutation_and_target_state"),
            EvidenceSpec(34, "recorded_downstream_outcome"),
        ),
    ),
    ReplaySpec(
        fixture_id="check_set_meet_time_s6_to_s7",
        task_name="CheckSetMeetTimeTask",
        source_step=6,
        target_step=7,
        evidence=(
            EvidenceSpec(3, "direct_refutation_of_source_claim"),
            EvidenceSpec(29, "recorded_downstream_outcome"),
        ),
    ),
    ReplaySpec(
        fixture_id="check_interview_times_s20_to_s21",
        task_name="CheckInterviewTimesTask",
        source_step=20,
        target_step=21,
        evidence=(
            EvidenceSpec(3, "task_fact_source_email"),
            EvidenceSpec(6, "task_fact_source_email"),
            EvidenceSpec(12, "task_fact_source_email"),
        ),
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise FixtureValidationError(f"{label} must be absolute: {path}")
    if not path.is_file():
        raise FixtureValidationError(f"{label} does not exist: {path}")
    return path


def screenshot_path(input_root: Path, task_name: str, step: int) -> Path:
    return (
        input_root
        / task_name
        / "screenshots"
        / f"{task_name}-0-{step}.png"
    )


def screenshot_ref(path: Path, step: int, role: str) -> dict[str, Any]:
    require_file(path, role)
    return {
        "step": step,
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
    }


def load_steps(traj_path: Path) -> list[dict[str, Any]]:
    require_file(traj_path, "trajectory")
    try:
        payload = json.loads(traj_path.read_text(encoding="utf-8"))
        steps = payload["0"]["traj"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FixtureValidationError(
            f"unexpected trajectory schema in {traj_path}: {exc}"
        ) from exc
    if not isinstance(steps, list) or not steps:
        raise FixtureValidationError(f"trajectory is empty: {traj_path}")
    for expected_step, item in enumerate(steps, start=1):
        if not isinstance(item, dict) or item.get("step") != expected_step:
            raise FixtureValidationError(
                f"non-contiguous step sequence in {traj_path}: "
                f"expected {expected_step}, got {item!r}"
            )
    return steps


def validate_step_record(
    record: dict[str, Any], expected_step: int, task_goal: str, label: str
) -> None:
    if record.get("step") != expected_step:
        raise FixtureValidationError(
            f"{label} step mismatch: expected {expected_step}, "
            f"got {record.get('step')!r}"
        )
    if record.get("task_goal") != task_goal:
        raise FixtureValidationError(f"task_goal changes at {label} step")
    if not isinstance(record.get("prediction"), str) or not record["prediction"].strip():
        raise FixtureValidationError(f"missing prediction at {label} step")
    if not isinstance(record.get("action"), dict):
        raise FixtureValidationError(f"missing parsed action at {label} step")


def build_fixture(input_root: Path, spec: ReplaySpec) -> dict[str, Any]:
    if "_backup_" in spec.task_name:
        raise FixtureValidationError(f"backup trajectory is forbidden: {spec.task_name}")
    if spec.target_step != spec.source_step + 1:
        raise FixtureValidationError(
            f"replay point must be adjacent: {spec.source_step}->{spec.target_step}"
        )

    task_dir = input_root / spec.task_name
    if task_dir.parent != input_root or not task_dir.is_dir():
        raise FixtureValidationError(f"task directory does not exist: {task_dir}")
    traj_path = task_dir / "traj.json"
    steps = load_steps(traj_path)
    if spec.target_step > len(steps):
        raise FixtureValidationError(
            f"target step {spec.target_step} exceeds {len(steps)} steps: {traj_path}"
        )

    source = steps[spec.source_step - 1]
    target = steps[spec.target_step - 1]
    task_goal = source.get("task_goal")
    if not isinstance(task_goal, str) or not task_goal.strip():
        raise FixtureValidationError(f"missing task_goal in {traj_path}")
    validate_step_record(source, spec.source_step, task_goal, "source")
    validate_step_record(target, spec.target_step, task_goal, "target")

    history_responses = []
    historical_observations = []
    for history_step in range(1, spec.target_step):
        history_record = steps[history_step - 1]
        validate_step_record(
            history_record,
            history_step,
            task_goal,
            f"history[{history_step}]",
        )
        history_responses.append(
            {
                "step": history_step,
                "prediction": history_record["prediction"],
                "action": history_record["action"],
            }
        )
        historical_observations.append(
            screenshot_ref(
                screenshot_path(input_root, spec.task_name, history_step),
                history_step,
                "historical_pre_action_observation",
            )
        )

    source_screen = screenshot_path(input_root, spec.task_name, spec.source_step)
    target_screen = screenshot_path(input_root, spec.task_name, spec.target_step)
    evidence = []
    for item in spec.evidence:
        if item.step < 1 or item.step > len(steps):
            raise FixtureValidationError(
                f"evidence step {item.step} is outside trajectory: {traj_path}"
            )
        evidence.append(
            {
                **screenshot_ref(
                    screenshot_path(input_root, spec.task_name, item.step),
                    item.step,
                    "evidence",
                ),
                "purpose": item.purpose,
            }
        )

    return {
        "fixture_id": spec.fixture_id,
        "task_name": spec.task_name,
        "task_goal": task_goal,
        "provenance": {
            "run_group": "seed_baseline",
            "trajectory_path": str(traj_path),
            "trajectory_sha256": sha256_file(traj_path),
            "source_tree_read_only": True,
            "agent_type": "seed_agent",
            "model_name": "doubao-seed-2-0-pro-260215",
            "model_name_source": "QR-MW experiment report/launch configuration",
            "history_n": 3,
            "history_n_source": (
                "current seed_agent source default plus three-image prompt "
                "fragments observed in the run thread logs; runtime_conf was "
                "not serialized into traj.json"
            ),
        },
        "replay_point": {
            "source_step": spec.source_step,
            "target_step": spec.target_step,
            "adjacent": True,
            "timing": (
                "S_source precedes source_prediction and source_action; "
                "S_target is the post-source-action observation that precedes "
                "target_prediction."
            ),
        },
        "history_responses_before_target": history_responses,
        "historical_observations_before_target": historical_observations,
        "current_observation_path": str(target_screen),
        "current_observation_sha256": sha256_file(target_screen),
        "source": {
            "prediction": source["prediction"],
            "action": source["action"],
            "screenshot": screenshot_ref(
                source_screen,
                spec.source_step,
                "S_source_pre_action_observation",
            ),
        },
        "target": {
            "prediction": target["prediction"],
            "recorded_action": target["action"],
            "screenshot": screenshot_ref(
                target_screen,
                spec.target_step,
                "S_target_post_source_action_pre_target_prediction",
            ),
        },
        "evidence_screenshots": evidence,
    }


def build_bundle(input_root: Path) -> dict[str, Any]:
    input_root = input_root.resolve()
    if input_root.name != "seed_baseline" or not input_root.is_dir():
        raise FixtureValidationError(
            f"input root must be an existing seed_baseline directory: {input_root}"
        )
    fixtures = [build_fixture(input_root, spec) for spec in REPLAY_SPECS]
    ids = [item["fixture_id"] for item in fixtures]
    if len(ids) != len(set(ids)):
        raise FixtureValidationError("fixture IDs are not unique")
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_count": len(fixtures),
        "source_root": str(input_root),
        "image_storage": "absolute_paths_and_sha256_only_no_copies",
        "fixtures": fixtures,
    }


def render_bundle(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_atomic(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, output)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--mode",
        choices=("check", "stdout", "write"),
        default="check",
        help="check an existing fixture, print generated JSON, or write atomically",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        content = render_bundle(build_bundle(args.input_root))
        if args.mode == "stdout":
            sys.stdout.write(content)
            return 0
        output = args.output.resolve()
        if args.mode == "write":
            write_atomic(output, content)
            print(f"wrote 5 validated replay fixtures to {output}")
            return 0
        require_file(output, "fixture bundle")
        existing = output.read_text(encoding="utf-8")
        if existing != content:
            raise FixtureValidationError(
                f"fixture bundle is stale; regenerate with --mode write: {output}"
            )
        print(f"validated 5 replay fixtures: {output}")
        return 0
    except FixtureValidationError as exc:
        print(f"fixture validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
