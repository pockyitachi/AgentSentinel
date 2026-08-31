#!/usr/bin/env python3
"""Start the private loopback-only ALE-324/G1.6 annotation workspace."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

import uvicorn

from mobile_world.offline.gold_curation import (
    AICandidateWorkspace,
    AnnotationStore,
    CurationPublication,
    ReviewerRegistry,
    SoloCuratorRegistry,
    SoloFirstPassStore,
    create_app,
    load_local_pinned_token_counters,
    write_codec_gate_receipt,
)


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve(strict=False)
    second_resolved = second.resolve(strict=False)
    return (
        first_resolved == second_resolved
        or first_resolved in second_resolved.parents
        or second_resolved in first_resolved.parents
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--annotation-root",
        type=Path,
        help="Repository-external directory for the append-only annotation journal",
    )
    parser.add_argument(
        "--reviewer-registry",
        type=Path,
        help="Restricted repo-external owner registry for reviewer principals, roles, and secrets",
    )
    parser.add_argument(
        "--solo-first-pass",
        action="store_true",
        help=(
            "Run the mechanically non-formal one-person first-pass workspace; records never "
            "count as independent reviews and cannot be promoted or formally exported"
        ),
    )
    parser.add_argument(
        "--g1-5-publication-manifest",
        type=Path,
        help="Checked-in ALE-323 CPU publication manifest used to verify the formal annotation gate",
    )
    parser.add_argument(
        "--codec-gate-receipt",
        type=Path,
        help="Repo-external content-addressed codec gate receipt; omitted keeps finalization blocked",
    )
    parser.add_argument(
        "--prepare-codec-gate-output-root",
        type=Path,
        help="Create the repo-external codec gate receipt and exit without starting the website",
    )
    parser.add_argument(
        "--load-local-pinned-tokenizers",
        action="store_true",
        help=(
            "Load only the hash-verified local Qwen/MAI tokenizer JSON files for CPU previews; "
            "requires tokenizers==0.22.2 and never loads model weights"
        ),
    )
    parser.add_argument(
        "--ai-candidate-root",
        type=Path,
        help=(
            "Read an already sealed, repo-external D-031 Action-Gold candidate campaign; "
            "solo mode displays non-authoritative suggestions, while formal mode uses only its "
            "exposure records to exclude ineligible principals"
        ),
    )
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.prepare_codec_gate_output_root is not None:
        if args.g1_5_publication_manifest is None:
            parser.error("--prepare-codec-gate-output-root requires --g1-5-publication-manifest")
        if (
            args.solo_first_pass
            or args.load_local_pinned_tokenizers
            or any(
                value is not None
                for value in (
                    args.annotation_root,
                    args.reviewer_registry,
                    args.codec_gate_receipt,
                    args.ai_candidate_root,
                )
            )
        ):
            parser.error("codec-gate preparation cannot be combined with website arguments")
        path = write_codec_gate_receipt(
            args.g1_5_publication_manifest,
            args.prepare_codec_gate_output_root,
        )
        print(path)
        return 0
    if args.annotation_root is None or args.reviewer_registry is None:
        parser.error("website mode requires --annotation-root and --reviewer-registry")
    if not args.solo_first_pass and args.ai_candidate_root is None:
        parser.error("formal website mode requires --ai-candidate-root for D-031 exposure checks")
    if args.ai_candidate_root is not None and _paths_overlap(
        args.annotation_root, args.ai_candidate_root
    ):
        parser.error("--annotation-root and --ai-candidate-root must be disjoint")
    if (args.codec_gate_receipt is None) != (args.g1_5_publication_manifest is None):
        parser.error(
            "--codec-gate-receipt and --g1-5-publication-manifest must be supplied together"
        )
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    preview_counters = None
    if args.load_local_pinned_tokenizers:
        model_config_manifest = (
            Path(__file__).resolve().parents[2]
            / "mobileworld_audit_handoff"
            / "g1"
            / "model_config_manifest.v1.json"
        )
        preview_counters = load_local_pinned_token_counters(model_config_manifest)
    publication = CurationPublication(preview_token_counters=preview_counters)
    ai_candidate_workspace = (
        None
        if args.ai_candidate_root is None
        else AICandidateWorkspace(
            args.ai_candidate_root,
            publication,
            forbidden_roots=(args.annotation_root,),
        )
    )
    reviewer_registry: SoloCuratorRegistry | ReviewerRegistry
    if args.solo_first_pass:
        reviewer_registry = SoloCuratorRegistry.load(args.reviewer_registry)
    else:
        reviewer_registry = ReviewerRegistry.load(args.reviewer_registry)
        assert ai_candidate_workspace is not None
        ai_candidate_workspace.assert_formal_registry_eligible(reviewer_registry)
    if args.solo_first_pass:
        solo_registry = cast(SoloCuratorRegistry, reviewer_registry)
        store: SoloFirstPassStore | AnnotationStore = SoloFirstPassStore(
            args.annotation_root,
            publication,
            solo_registry,
            codec_gate_receipt_path=args.codec_gate_receipt,
            g1_5_publication_manifest_path=args.g1_5_publication_manifest,
        )
    else:
        formal_registry = cast(ReviewerRegistry, reviewer_registry)
        assert ai_candidate_workspace is not None
        with ai_candidate_workspace.formal_registry_guard(formal_registry):
            store = AnnotationStore(
                args.annotation_root,
                publication,
                formal_registry,
                codec_gate_receipt_path=args.codec_gate_receipt,
                g1_5_publication_manifest_path=args.g1_5_publication_manifest,
            )
            store.assert_formal_ai_assistance_eligibility(
                ai_candidate_workspace.exposed_stable_principal_commitments()
            )
    app = (
        create_app(publication, store)
        if ai_candidate_workspace is None
        else create_app(
            publication,
            store,
            ai_candidate_workspace=ai_candidate_workspace if args.solo_first_pass else None,
            ai_exposure_workspace=ai_candidate_workspace if not args.solo_first_pass else None,
        )
    )
    print(f"G1.6 private workspace: http://127.0.0.1:{args.port}")
    print(f"Annotation state: {store.root}")
    print(f"Formal annotation open: {str(store.formal_annotation_open).lower()}")
    if args.solo_first_pass:
        assert isinstance(store, SoloFirstPassStore)
        print("Mode: SOLO_FIRST_PASS / NON_FORMAL / NOT_PROMOTABLE")
        print(f"Current phase: {store.current_phase()}")
    if ai_candidate_workspace is not None:
        print(
            "AI assistance: SEALED_NON_AUTHORITATIVE / HUMAN_REVIEW_REQUIRED / "
            f"{ai_candidate_workspace.campaign_id}"
        )
    print("GPU/model/provider/network/replay/action paths remain disabled.")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
