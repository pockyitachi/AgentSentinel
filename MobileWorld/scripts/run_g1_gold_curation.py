#!/usr/bin/env python3
"""Start the private loopback-only ALE-324/G1.6 annotation workspace."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from mobile_world.offline.gold_curation import (
    AnnotationStore,
    CurationPublication,
    ReviewerRegistry,
    create_app,
    load_local_pinned_token_counters,
    write_codec_gate_receipt,
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
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.prepare_codec_gate_output_root is not None:
        if args.g1_5_publication_manifest is None:
            parser.error("--prepare-codec-gate-output-root requires --g1-5-publication-manifest")
        if args.load_local_pinned_tokenizers or any(
            value is not None
            for value in (args.annotation_root, args.reviewer_registry, args.codec_gate_receipt)
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
    reviewer_registry = ReviewerRegistry.load(args.reviewer_registry)
    store = AnnotationStore(
        args.annotation_root,
        publication,
        reviewer_registry,
        codec_gate_receipt_path=args.codec_gate_receipt,
        g1_5_publication_manifest_path=args.g1_5_publication_manifest,
    )
    app = create_app(publication, store)
    print(f"G1.6 private workspace: http://127.0.0.1:{args.port}")
    print(f"Annotation state: {store.root}")
    print(f"Formal annotation open: {str(store.formal_annotation_open).lower()}")
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
