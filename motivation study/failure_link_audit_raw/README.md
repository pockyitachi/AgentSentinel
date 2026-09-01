# Epic 1 failure-link audit: complete final raw archive

This directory publishes the complete final source archive
`six_model_failure_link_audit_v1_20260824_03` (`_03`). The repository is public, and the owner
explicitly authorized publication of these exact `_03` bytes on 2026-09-01.

The sibling [`epic1_failure_link_audit_v1`](../epic1_failure_link_audit_v1/) directory is the
compact, safer machine-readable result projection and remains the preferred entry point for
ordinary analysis. This raw archive is the larger provenance record behind that projection. It
includes raw cards and application requests, model responses, reviewer rationales and evaluator
excerpts, operational receipts, rejected attempts, migration artifacts, and machine-local
manifests, logs, and paths.

No confirmed live credential or secret was found during the publication review. The archive does,
however, contain benchmark identity-like and email-like strings. They must not be assumed to
identify real people or accounts. Publication also means these bytes may persist in Git history,
forks, mirrors, and caches even after a later repository deletion.

The obsolete `_02` attempt was deleted before this publication and is not included. The owner's
exception covers only the exact `_03` archive identified by
[`archive_manifest.v1.json`](archive_manifest.v1.json). It grants no authority to invoke a model,
use a GPU or network, perform replay, or execute an action.

## Integrity verification

From the repository root, GNU `find`, `sort`, `xargs`, and `sha256sum` can reproduce the recorded
path-sorted tree inventory digest:

```bash
(cd "motivation study/failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03" && \
  LC_ALL=C find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) | sha256sum
```

The expected digest is
`a97f9d4541c339d3cb6782bf499eed61ade9bfe68270419b7a62f500f4aa944a`. The inventory contains
exactly 2,842 regular files totaling 119,555,475 bytes.
