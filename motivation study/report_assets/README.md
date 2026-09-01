# Epic 1 report assets

This directory contains the 39 content-addressed PNG screenshots referenced, in order, by
[`misleading_history_audit_report.md`](../misleading_history_audit_report.md). The companion PDF is
published next to that Markdown report. [`screenshot_manifest.v1.json`](screenshot_manifest.v1.json)
binds every screenshot's repository-relative path, SHA-256 digest, byte count, dimensions, media
type, ordinal, and report alt text; its closed JSON Schema is
[`screenshot_manifest.v1.schema.json`](screenshot_manifest.v1.schema.json).

## Publication boundary

These screenshots come from synthetic MobileWorld fixtures and are intentionally visible in this
public Git repository. Their inclusion, together with the companion PDF, is a narrow owner-approved
exception for the exact 39 figures used by the final Epic 1 report. It does not authorize publishing
other raw collections, requests, trajectories, reviewer text, receipts, screenshots, replay data,
or model outputs.

Some figures contain synthetic identity-, phone-, verification-code-, account-, health-, or
credential-like interface content. They do not contain known live credentials or secrets. Any
medical or health-related content is synthetic research evidence and is not medical advice.
Third-party image, application-UI, trademark, and redistribution rights were not independently
verified; repository inclusion does not grant downstream rights beyond those otherwise available.

## Integrity and use

Screenshot filenames are their lowercase SHA-256 digests plus `.png`. All 39 files are 1080 by
2400 pixels and total 18,315,499 bytes. Consumers that render the report or inject its evidence into
another local workflow should preserve manifest order and alt text, reject missing or extra assets,
and verify each digest, byte count, PNG media type, and dimensions before use. The manifest contains
no machine-local source path.
