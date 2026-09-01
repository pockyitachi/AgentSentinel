# Epic 1 failure-link audit: machine-readable result projection

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

[`public_summary.v1.json`](public_summary.v1.json) and
[`publication_lock.v1.json`](publication_lock.v1.json) preserve the first, safe-only publication
in which screenshots and the PDF were deliberately excluded. They are retained as historical
records and are not rewritten. The current owner-approved report publication is described by
[`public_summary.v2.json`](public_summary.v2.json) and authenticated by
[`publication_lock.v2.json`](publication_lock.v2.json); v2 binds the same eight source-projection
files plus the repository-local Markdown, fixed PDF, screenshot manifest and schema, renderer, and
v2 publication schemas.

The internal Phase B result (`60/108` plausible-or-strong observed contribution) and the report's
more conservative, mutually exclusive reader projection (`10` direct stops plus `48` indirect
derailments, or `58/108`) are deliberately both retained. They answer different observational
classification questions and neither is a causal count.

## Related report publication

The owner subsequently approved a narrow public-evidence exception for the report itself:

- [Markdown report](../misleading_history_audit_report.md);
- [fixed PDF report](../misleading_history_audit_report_20260825.pdf); and
- [content-addressed report assets](../report_assets/), including the exact 39 PNG screenshots and
  their [publication manifest](../report_assets/screenshot_manifest.v1.json).

That exception is limited to the screenshots actually cited by the report and the one fixed PDF.
The repository is public, and the published bytes may remain in Git history, forks, and caches even
after a later deletion. Visible phone/code, identity-like, email-like, file-name, and local-address
values are synthetic/demo benchmark fixtures, not asserted real accounts or usable credentials.
Third-party application UI, trademarks, and imagery appear in the screenshots; their separate
redistribution rights were not independently verified. Any medical-looking text in a screenshot is
inert benchmark evidence, not medical advice or project endorsement.

## Deliberately excluded

Raw cards, requests, trajectories, reviewer rationales, evaluator excerpts, model responses,
operational receipts, machine-local run manifests, logs, replay data, and all Collector screenshots
outside the exact 39-file report allowlist remain outside Git. These exclusions are about data
governance, not Git's file-size limit. The included projection and report publication are small
enough for ordinary Git and do not require Git LFS.

## Offline report rendering

[`render_misleading_history_audit_pdf.py`](../../MobileWorld/scripts/render_misleading_history_audit_pdf.py)
is the checked-in, path-safe derivative of the renderer used for the final report layout. It
resolves the report's 39 repository-local, content-addressed screenshot paths, validates every
image hash, and refuses to replace an existing build directory or output file. The checked-in PDF
above is the canonical fixed rendering; a local rebuild must use a new output path:

```bash
python MobileWorld/scripts/render_misleading_history_audit_pdf.py \
  "motivation study/misleading_history_audit_report.md" \
  /tmp/new-absent-build-directory \
  /tmp/new-absent-output.pdf
```

The command reconstructs the already owner-approved report asset set only; it does not authorize
publication of any other raw evidence.
