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

This narrow publication exception does not include raw requests, trajectories, model responses,
reviewer text, operational receipts, logs, replay data, or any other Collector blob. Those data
remain outside Git under the repository's data-governance rules. Implementation, validation code,
and the report renderer remain under `MobileWorld/`.
