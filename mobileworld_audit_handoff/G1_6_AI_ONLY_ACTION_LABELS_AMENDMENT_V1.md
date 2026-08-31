# G1.6 AI-Only Action Labels Amendment V1

Status: **Locked additive CPU-only research checkpoint (D-033)**

This amendment records the owner's explicit request to let AI finish the 186 `ACTION_GOLD`
units that were not already locked by the owner in the `SOLO_FIRST_PASS` journal. It does not
change D-029 through D-032, does not turn an AI judgment into a human judgment, and does not
advance the formal G1.6 curation state machine.

## 1. Narrow authority and non-authority

Exactly three isolated Codex research agents may each inspect one disjoint 62-unit shard of the
remaining population. For every assigned unit the agent may read only the already sealed D-031
blind packet, its target-pre screenshot, and the frozen A/B/C candidate outputs and receipts. The
agent may retain or reject existing atomic candidates, or exclude the unit when no reliable
candidate exists. It may not generate a new action predicate, change candidate bytes, access
history, natural action, post/later state, outcome/checker data, transformation material, the human
decision journal, the human annotation journal, peer batch output, registry data, or secrets.

The resulting publication is `AI_ONLY_ACTION_LABELS`, not human annotation and not gold. Every
record and receipt fixes all of these fields to false:

- `human_review_performed`;
- `human_selected`;
- `counts_as_independent_review`;
- `formal_resolution_eligible`;
- `formal_export_eligible`;
- `admission_eligible`;
- `promotion_allowed`;
- `replay_eligible`;
- `auto_apply_allowed`;
- `solo_journal_write_allowed`.

The publication must set `ai_semantic_labeling_performed=true` and
`chain_of_thought_stored=false`. It cannot be imported into, promoted to, or counted by the solo
or formal G1.6 journals. It does not open `TRANSFORMATION`, does not satisfy the 190-unit human
first-pass requirement, and does not make ALE-324 complete.

## 2. Frozen population and input visibility

The compiler must bind the active G1.3 publication and capsule-set digests, the complete sealed
D-031 campaign manifest and terminal receipt, the exact 570-output candidate-set digest, and an
immutable prefix of the solo journal containing exactly four unique `ACTION_GOLD`
`SOLO_FIRST_PASS_LOCKED` events. Only the four event IDs, event/payload/material/source digests,
unit IDs, and the prefix byte/hash binding may enter the AI-only publication; reviewer identity,
assignment identity, registry bytes, keys, secrets, and human proposal text must not be copied.

The AI-only population is exactly the campaign's 190 units minus those four human-locked units.
The sorted remainder is divided into `BATCH_1`, `BATCH_2`, and `BATCH_3`, 62 units each. Each
batch must account for its exact shard in lexicographic unit-ID order. Batch agents are isolated
from one another and from all human records. Their draft file hashes are evidence of the compiler
inputs, not human or formal provenance.

## 3. Per-unit decision semantics

Each unit draft is a closed object with one of these dispositions:

- `ACCEPT_CANDIDATES`: retain at least one frozen atomic candidate;
- `EXCLUDE`: retain none because the blind evidence and frozen candidates do not support a
  reliable immediate action.

Every atomic candidate from A/B/C must occur exactly once in `candidate_decisions`. Its decision
is `RETAIN` or `REJECT`. A retained candidate must use reason `SUPPORTED`; a rejected candidate
must use one of `MATERIAL_DUPLICATE`, `LESS_COMPLETE_VARIANT`, `WRONG_ACTION`, `BAD_GEOMETRY`,
`INSUFFICIENT_EVIDENCE`, or `OUT_OF_SCOPE`. Equivalent material predicates cannot both be
retained. The compiler may detect and reject duplicates, but it may not silently choose, merge,
repair, or create a candidate.

An `EXCLUDE` record must use exactly one of `NO_CANDIDATES`, `NO_RELIABLE_CANDIDATE`,
`AMBIGUOUS_VISIBLE_EVIDENCE`, or `AUTHORIZATION_MISSING`. `ACCEPT_CANDIDATES` requires a null
exclusion reason. Concise rationale and uncertainty text must be NFC, free of control/format
characters, URLs, credentials, raw paths, and HTML. Chain-of-thought is neither requested nor
stored.

## 4. Deterministic compiler and publication

The compiler performs all input parsing, schema validation, exact population accounting,
candidate/source rederivation, material-duplicate checks, and human-prefix validation before it
creates a publication root. It then publishes through a private staging directory and one atomic
Linux `renameat2(RENAME_NOREPLACE)` into a previously absent, repository-external destination.
The install must refuse even an empty directory or dangling-link target that appears concurrently;
it must never replace an existing path. If installation fails after staging begins, the compiler
leaves that private owner-only staging directory for explicit audited cleanup; it must not run a
recursive pathname cleanup that could delete a concurrently substituted path.

The publication contains only:

```text
publication-manifest.json
label-index.jsonl
labels/sha256/<prefix>/<sha256>.json
publication-receipt.json
```

All JSON objects use the frozen canonical JSON encoding plus one LF as a file record. Labels are
content-addressed and the index is ordered by unit ID. The manifest binds the four additive schema
hashes, this contract hash, all three batch file hashes, all immutable source hashes, all label
references, and every non-authority/safety guard. The receipt binds the manifest bytes, exact
counts, label-set digest, index digest, and exact-filesystem census. Reopening must reject an
unknown/extra/missing object, symlink, hard link, non-owner file, unsafe mode, non-canonical bytes,
hash mismatch, source-candidate mismatch, population mismatch, or changed human-journal prefix.
Before installation, every regular file is sealed mode `0400` and every directory/root mode `0500`;
reopen requires those exact owner-only, non-writable modes. Every label path must be exactly the
content-address derived `labels/sha256/<sha256-prefix>/<sha256>.json` path, and unexpected empty
directories are part of the forbidden extra census rather than ignored.

No label object contains or claims `ACCEPT`, `EXCLUDE` under the human proposal schema,
`human_selected=true`, closed-world human confirmation, or formal gold authority. Downstream
research must resolve retained candidate bytes through the separately sealed D-031 campaign and
must continue to carry this publication's non-authority flags.

## 5. Safety and operational boundary

This tranche permits CPU-only local file parsing, local screenshot inspection by the three Codex
research agents, deterministic validation, hashing, and repository-external publication. It does
not authorize target-actor execution, a project provider/client, external network, GPU probe/use,
model-weight loading or serving, backend restore, replay, MobileWorld/generated GUI/tool/action,
treatment response generation, website generation endpoints, or writes to either human journal.

The existing loopback annotation site may remain available for the owner's four human records,
but the AI-only compiler is an offline CLI and is not mounted as a site endpoint. The website,
human workspace, candidate campaign, G1.3 publication, and AI-only publication roots must be
pairwise non-overlapping.

## 6. Acceptance gates

The checkpoint is reportable only after all of the following pass on final bytes:

1. 186 unique labels, three exact 62-unit batch shards, and four separately bound human-locked
   exclusions account for the exact 190-unit campaign population;
2. every atomic candidate in every labeled unit is decided exactly once and every retained
   candidate resolves to immutable D-031 bytes;
3. no retained material duplicates, unknown candidates, cross-unit references, or malformed
   geometry/actions survive validation;
4. all four schemas are Draft 2020-12 meta-valid and every runtime record validates;
5. deterministic double-builds have identical manifest, index, label, and receipt bytes;
6. publication reopen, exact census, source-prefix, tamper, symlink, hard-link, and unsafe-mode
   negatives fail closed;
7. focused tests, Ruff, formatting, mypy, compile, schema validation, and diff checks pass;
8. an independent read-only red-team verifies the final repo-external publication;
9. `STATUS.md` records the local commit, source/publication hashes, counts, validation commands,
   safety disclosure, and the explicit `AI_ONLY / NON_HUMAN / NON_FORMAL` boundary.

Even after all gates pass, the correct state is
`AI_ONLY_ACTION_LABELS_PUBLISHED_NON_FORMAL / HUMAN_CURATION_INCOMPLETE`.
