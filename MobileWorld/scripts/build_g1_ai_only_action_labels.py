#!/usr/bin/env python3
"""Build or validate the isolated D-033 AI-only Action-label publication."""

from __future__ import annotations

import argparse
from pathlib import Path

from mobile_world.offline.gold_curation import (
    ACTIVE_G1_3_PUBLICATION,
    AICandidateWorkspace,
    CurationPublication,
)
from mobile_world.offline.gold_curation.ai_only_labels import (
    AIOnlyActionLabelPublication,
    build_ai_only_action_label_publication,
)


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    return first == second or first in second.parents or second in first.parents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--human-journal", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--batch-1", type=Path)
    parser.add_argument("--batch-2", type=Path)
    parser.add_argument("--batch-3", type=Path)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Reopen an existing publication without reading batch drafts or writing files",
    )
    args = parser.parse_args()
    for first, second in (
        (args.candidate_root, args.human_journal.parent),
        (args.output_root, args.candidate_root),
        (args.output_root, args.human_journal.parent),
        (args.output_root, ACTIVE_G1_3_PUBLICATION),
    ):
        if _paths_overlap(first, second):
            parser.error("candidate, human-workspace, and AI-only roots must be disjoint")
    batch_paths = (args.batch_1, args.batch_2, args.batch_3)
    if args.validate_only:
        if any(path is not None for path in batch_paths):
            parser.error("--validate-only cannot be combined with batch drafts")
    elif any(path is None for path in batch_paths):
        parser.error("build mode requires --batch-1, --batch-2, and --batch-3")
    publication = CurationPublication()
    candidate_workspace = AICandidateWorkspace(
        args.candidate_root,
        publication,
        forbidden_roots=(args.human_journal.parent, args.output_root),
    )
    if not args.validate_only:
        assert all(path is not None for path in batch_paths)
        build_ai_only_action_label_publication(
            args.output_root,
            candidate_workspace,
            args.human_journal,
            {
                "BATCH_1": args.batch_1,
                "BATCH_2": args.batch_2,
                "BATCH_3": args.batch_3,
            },
        )
    sealed = AIOnlyActionLabelPublication(
        args.output_root,
        candidate_workspace,
        args.human_journal,
    )
    counts = sealed.manifest["counts"]
    print(f"Publication: {sealed.publication_id}")
    print(f"AI-only labels: {counts['ai_only_labeled_units']}")
    print(
        "Units omitted from AI-only labeling due to prior human locks: "
        f"{counts['human_locked_units']}"
    )
    print(f"Accepted-candidate units: {counts['accepted_candidate_units']}")
    print(f"Excluded units: {counts['excluded_units']}")
    print("Authority: AI_ONLY / NON_HUMAN / NON_FORMAL / NOT_PROMOTABLE")
    print("GPU/provider/external-network/replay/action paths remained disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
