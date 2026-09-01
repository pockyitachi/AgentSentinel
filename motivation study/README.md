# Motivation study

This top-level directory contains the canonical Epic 1 research deliverables, separate from the
MobileWorld implementation tree:

- [`misleading_history_audit_report.md`](misleading_history_audit_report.md) is the final
  six-model observational report in Markdown, with repository-relative links to all 39 evidence
  screenshots.
- [`misleading_history_audit_report_20260825.pdf`](misleading_history_audit_report_20260825.pdf)
  is the fixed 30-page PDF rendering of that report.
- [`report_assets/`](report_assets/) contains the content-addressed screenshot publication,
  including the 39 exact PNG bytes under [`screenshots/`](report_assets/screenshots/) and the
  [`screenshot_manifest.v1.json`](report_assets/screenshot_manifest.v1.json) machine-readable
  publication binding.
- [`epic1_failure_link_audit_v1/`](epic1_failure_link_audit_v1/) is the machine-readable
  publication of aggregate results, frozen schemas, provenance hashes, and publication
  boundaries.
- [`failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/`](failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/)
  is the exact final Phase A/Phase B failure-link review archive published under a separate owner
  exception.

## Public-evidence notice

The repository is public. The owner explicitly approved publication of exactly these 39 report
screenshots and this one PDF; after publication, the bytes may persist in Git history, forks, and
caches even if a later commit removes them. The screenshots contain synthetic/demo benchmark
fixture values that resemble credentials or personal information, including a demo phone number
and code, generic names and email-like identifiers, and emulator-local addresses. They are not
presented as real accounts or usable credentials.

Some screenshots reproduce third-party application UI, trademarks, and photographic imagery.
Their separate redistribution rights were not independently verified; inclusion here documents
research evidence and does not grant an additional license to third-party content. Any medical or
health-looking statement visible inside a screenshot is inert benchmark content, not medical
advice, a factual health claim by this project, or an endorsement.

The screenshot/PDF exception above was intentionally narrow. On 2026-09-01 the owner made a
separate, additive decision to publish the exact final failure-link archive at
[`failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/`](failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/).
It contains 2,842 regular files / 119,555,475 bytes and has path-sorted inventory SHA-256
`a97f9d4541c339d3cb6782bf499eed61ade9bfe68270419b7a62f500f4aa944a`. The archive includes raw
cards and requests, model responses, reviewer text and rationales, operational receipts, logs, and
machine-local paths. No confirmed live secret was found in the pre-publication review. The owner
accepts that these public bytes may remain permanently in Git history, forks, mirrors, and caches.

This later raw-archive exception does not rewrite or expand the historical v1/v2 publication
locks, which remain records of their safe projection and report-publication scopes. The older
`_02` attempt was deleted and is not authorized for publication. All other raw collection,
capsule, replay, and audit data remain outside Git. Implementation, validation code, and the report
renderer remain under `MobileWorld/`.
