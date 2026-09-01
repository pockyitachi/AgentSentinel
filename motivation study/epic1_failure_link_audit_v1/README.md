# Epic 1 failure-link audit: public repository projection

This directory is the safe, machine-readable repository projection of the final six-model
failure-link audit `six_model_failure_link_audit_v1_20260824_03`.

It contains the original canonical bytes of eight source artifacts that passed a privacy and
publication-boundary review:

- both frozen review JSON Schemas;
- the Phase B input manifest;
- the Phase A and Phase B resolution manifests;
- the Phase B aggregate metrics; and
- the Phase A and Phase B driver freezes, which contain only identifiers, hashes, sizes, relative
  receipt references, and frozen runtime/reviewer labels.

[`public_summary.v1.json`](public_summary.v1.json) provides the compact reader-facing and internal
aggregate results. [`publication_lock.v1.json`](publication_lock.v1.json) binds every included
source artifact, the full external source-tree census, the final report, and the schemas used to
validate this projection.

The internal Phase B result (`60/108` plausible-or-strong observed contribution) and the report's
more conservative, mutually exclusive reader projection (`10` direct stops plus `48` indirect
derailments, or `58/108`) are deliberately both retained. They answer different observational
classification questions and neither is a causal count.

## Deliberately excluded

Raw cards, requests, trajectories, reviewer rationales, evaluator excerpts, model responses,
operational receipts, screenshots, and machine-local run manifests remain outside Git. The final
PDF also remains outside Git because it embeds 39 raw Collector screenshots, including
credential-like synthetic UI content and third-party imagery that need a separate privacy and
redistribution review. Its exact hash and byte count remain recorded in the public summary.

These exclusions are about data governance, not Git's file-size limit. The included projection is
small enough for ordinary Git and does not require Git LFS.

## Offline report rendering

[`render_misleading_history_audit_pdf.py`](../../MobileWorld/scripts/render_misleading_history_audit_pdf.py)
is the checked-in, path-safe derivative of the renderer used for the final report layout. It
requires the report's 39 external, content-addressed screenshot paths to exist locally, validates
every image hash, and refuses to replace an existing build directory or output file:

```bash
python MobileWorld/scripts/render_misleading_history_audit_pdf.py \
  "motivation study/misleading_history_audit_report.md" \
  /tmp/new-absent-build-directory \
  /tmp/new-absent-output.pdf
```

The command is for authorized local reconstruction only; it does not make the embedded raw images
eligible for Git publication.
