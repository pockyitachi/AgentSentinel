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
records and are not rewritten. The subsequent owner-approved report publication is described by
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

## Historical publication boundaries

The v1 safe projection and v2 report publication deliberately excluded raw cards, requests,
trajectories, reviewer rationales, evaluator excerpts, model responses, operational receipts,
machine-local run manifests, logs, replay data, and all Collector screenshots outside the exact
39-file report allowlist. These historical locks remain unchanged and continue to describe only
those narrower publication scopes.

## Later owner-approved raw archive

On 2026-09-01 the owner separately authorized the exact final archive at
[`../failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/`](../failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/)
for public Git publication. It contains 2,842 regular files / 119,555,475 bytes and has path-sorted
inventory SHA-256
`a97f9d4541c339d3cb6782bf499eed61ade9bfe68270419b7a62f500f4aa944a`. Unlike this safe
projection, it includes raw cards and requests, model responses, reviewer text and rationales,
operational receipts, logs, and machine-local paths. No confirmed live secret was found in the
pre-publication review. The owner accepts that the bytes may remain permanently in Git history,
forks, mirrors, and caches.

This additive exception does not modify, supersede, or expand `publication_lock.v1.json` or
`publication_lock.v2.json`; neither historical lock binds or certifies the later raw archive. The
deleted `_02` attempt is not authorized, and no other collection, capsule, replay, or audit data is
approved by this decision. The size is acceptable for ordinary Git under the owner's explicit
public-data decision; the archive does not require Git LFS.

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
