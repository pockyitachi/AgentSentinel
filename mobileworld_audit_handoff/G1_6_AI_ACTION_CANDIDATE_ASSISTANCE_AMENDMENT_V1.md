# G1.6 AI Action-Gold Candidate Assistance Amendment V1

Contract ID: `mobileworld.g1.ai-action-gold-candidate-assistance/amendment-v1`

This additive amendment implements D-031. It does not modify the formal double-blind contract or
the D-030 solo-first-pass authority boundary.

## 1. Purpose and authority

Three isolated Codex research streams may prepare untrusted `ACTION_GOLD` candidate predicates for
the active 190-unit G1.3 population. The only human curator reviews those suggestions in the local
solo webpage. The streams are not human reviewers and their agreement is not consensus evidence.

Every candidate record fixes all of these values:

- `counts_as_independent_review=false`;
- `formal_resolution_eligible=false`;
- `admission_eligible=false`;
- `replay_eligible=false`;
- `auto_apply_allowed=false`;
- `human_review_required=true`.

The campaign cannot select `ACCEPT`, `EXCLUDE`, or `NO_GOLD_CONSENSUS`; it may only offer atomic
action predicates or `ABSTAIN`. Human review remains required for disposition, completeness,
evidence, geometry, tolerance, every predicate, and every human confirmation.

## 2. Frozen input visibility

Each stream receives byte-identical packet semantics and the same frozen prompt. An input contains
only:

- exact task instruction;
- exact target-pre canonical screenshot;
- allowlisted pre-cutoff tool-response or ask-user evidence, if present;
- stable source evidence identifiers needed to bind a candidate before browser pseudonymization.

It MUST NOT contain history, target-history claims, natural target output, target result or post
state, later trajectory, outcome/evaluator/checker data, transformation data, any human draft or
decision, peer-agent output, replay/treatment response, provider configuration, secrets, or raw
filesystem paths. A stream cannot read another stream's output and cannot regenerate after human
feedback. Stored rationales are concise evidence-linked explanations, never chain-of-thought.

## 3. Isolated lifecycle

Generation states are:

```text
PLANNED -> PACKET_FROZEN -> OUTPUT_CAPTURED -> VALIDATED_AVAILABLE
                                            -> ABSTAIN_AVAILABLE
                                            -> REJECTED_INVALID
```

Human candidate-item states are:

```text
PENDING -> ADOPT_TO_FORM
        -> ADOPT_WITH_EDITS_TO_FORM
        -> USE_AS_SUPPLEMENT
        -> IGNORE
```

A decided item may receive a later append-only `DECISION_SUPERSEDED` event. There is no candidate
`FINAL` or `LOCKED` state. Form state remains
`CLEAN -> DIRTY_UNSAVED -> explicit SOLO_DRAFT_SAVED`; candidate interaction stops at
`DIRTY_UNSAVED`.

## 4. Storage and identity separation

The campaign uses a third owner-restricted repo-external root:

```text
campaign-manifest.json
packets/sha256/<hh>/<digest>.json
screenshots/sha256/<hh>/<digest>.png
outputs/sha256/<hh>/<digest>.json
generation-receipts/slot-{A|B|C}.json
campaign-receipt.json
candidate-human-decisions.jsonl
candidate-human-decisions.lock
human-exposures/<human_identity_commitment>.json
```

The candidate root MUST be disjoint from the Git repository, active G1.3 publication, formal
annotation root, and solo-first-pass root. Packet, screenshot, and output objects are canonical and
content-addressed. The manifest, three self-hashed generation receipts, and terminal campaign
receipt are canonical write-once records. All are regular, owner-restricted, and no-follow. The
human decision journal is independent of both annotation journals. Candidate data has no import,
promotion, copy-to-journal, formal export, or admission API.

Before returning any candidate bytes, the server atomically writes or idempotently reuses a
self-hashed `AI_ASSISTED_SOLO_CURATOR` exposure record. A failed exposure write fails the candidate
response closed.

The campaign has exactly three neutral slots `A`, `B`, and `C`, exactly 190 frozen packet objects,
and exactly 570 terminal output envelopes before it is available in the webpage. Slots do not carry
model quality, score, rank, reviewer identity, or voting meaning.

Preparation starts only from an empty root. Capture requires a closed, self-hashed generation
attestation for the exact slot and draft bytes, validates and compiles all 190 rows before making
any output reachable, then atomically publishes the content objects and one write-once generation
receipt. A late failure rolls back that slot instead of leaving orphan output. The terminal receipt
binds all three generation-receipt file hashes, self hashes, byte counts, and their complete 570
output-reference closure. Every open re-censuses the filesystem: only reachable manifest,
packet/screenshot/output objects, the three generation receipts, terminal receipt, and the
specified dynamic decision-lock/journal/exposure paths are accepted. Extra/orphan objects,
symlinks, hard links, wrong owner, or wrong file/directory modes fail closed.

Candidate exposure writes take the campaign lock exclusively. A formal CLI bootstrap and every
formal HTTP operation hold the same lock in shared mode while re-reading the exposure set and
checking the owner registry. Thus a new exposure cannot race a formal authoritative operation.
Formal application construction without this exposure guard is invalid.

## 5. Candidate and decision semantics

An atomic candidate embeds one closed semantic action predicate, source evidence references, a
concise rationale, an uncertainty note, and its own canonical digest. It cannot assert
`human_selected`, closed-world coverage, completeness, or human identity. Unknown evidence,
invalid production-normalized action, out-of-bounds geometry, non-NFC text, duplicate material
predicates, URLs, HTML control fields, paths, credentials, or extra fields fail closed.

The browser replaces stable evidence and candidate identities with assignment-scoped opaque tokens.
It displays three neutral columns and may mark byte-equivalent duplicates, but MUST NOT rank,
collapse, vote, choose a winner, preselect, or bulk-apply. Each atomic item has exactly four explicit
decisions: `ADOPT_TO_FORM`, `ADOPT_WITH_EDITS_TO_FORM`, `USE_AS_SUPPLEMENT`, or `IGNORE`.
The human must first select an initially unchecked control attesting that they personally inspected
the task, screenshot, and cited visible evidence. Browser code MUST NOT infer or silently assert
that attestation from a candidate-button click.

Every decision event asserts:

- `human_confirmed_item_review=true`;
- `human_verified_visible_evidence=true`;
- `ai_candidate_is_not_evidence=true`;
- `annotation_form_not_saved_or_finalized=true`;
- `counts_as_independent_review=false`;
- `formal_journal_event_id=null`;
- `solo_journal_event_id=null`.

Adopt, edit, and supplement decisions deep-copy the candidate into browser memory with
`human_selected=false`. The copy is never deduplicated or collapsed. Because the accepted set has
changed, every existing predicate's human confirmation and the global closed-world/completeness
confirmations are reset to false. A decision POST changes only the candidate decision journal.
Saving or locking the human form remains a separate deliberate call to the existing solo endpoint.
When a sealed campaign is mounted, the solo server MUST reject an `ACTION_GOLD` stage lock until
that same curator identity has an explicit decision event for every atomic candidate in the unit.
Draft saves remain available before this gate. An all-`ABSTAIN` unit has no atomic candidate and
therefore satisfies the per-item gate vacuously. Because decisions are append-only and may only be
superseded by another decided state, a completed unit cannot return to `PENDING` after the global
solo phase advances.

### 5.1 D-032 simplified solo projection

For the owner-authorized D-032 solo UI, the browser projects three explicit human choices while
retaining the existing append-only decision schema: `BEST` / `最优（直接用）` maps to
`ADOPT_TO_FORM`, `CORRECT` / `正确（也可用）` maps to `USE_AS_SUPPLEMENT`, and `WRONG` /
`错误（不用）` maps to `IGNORE`. `BEST` is only a non-formal curator preference label. It MUST NOT
create a rank, winner, vote, Agent priority, consensus, or ordered accepted set. The three Agent
columns remain neutral and every atomic item remains separately decided.

This projection narrowly supersedes both the four-button/separate-checkbox presentation above and,
for the simple path only, the immediate candidate deep-copy plus `DIRTY_UNSAVED` lifecycle. A
three-way choice appends only its candidate-decision event; it does not materialize an annotation
form. Retained predicates are materialized only after the separate final lock click. Opening the
manual editor continues to use the original dirty-form lifecycle and permanently disables the
simple lock for that page.

Each of the three buttons must visibly state that clicking it is the curator's explicit attestation
that they personally checked the task, target-pre screenshot, cited visible evidence, and candidate
action. There is no default, bulk choice, inferred click, note field, or hidden checkbox. Each click
still appends exactly one existing candidate-decision event and MUST NOT write the solo annotation
journal.

After all atomic items have a non-legacy three-way decision and at least one item is retained, a
separate `确认选择并锁定本任务` click may mechanically create one existing-schema solo `ACCEPT`
payload. That click explicitly confirms every retained predicate field, screenshot region, evidence
reference and rationale, plus closed-world completeness. The browser sends only a closed
`ai_simple_lock=true` request. While holding the candidate-decision journal's shared lock, the server
re-reads the latest decision for every item, rejects missing or legacy states, derives the retained
predicate set from frozen stable candidate bytes, validates the complete payload, and holds that
lock through the durable solo append. Candidate-decision supersession requires the same lock in
exclusive mode, so a second tab cannot change the selected set between validation and append. The
derived payload sets `human_selected`, `closed_world_confirmed`, and
`all_reasonable_actions_enumerated` to true and calls the existing solo lock operation exactly once.
It remains a non-formal precursor. All-wrong/all-ABSTAIN units,
legacy `ADOPT_WITH_EDITS_TO_FORM`, duplicate material, invalid/stale inputs, and rejected locks fail
closed into the preserved manual editor; the browser MUST NOT infer `EXCLUDE`, merge, deduplicate,
repair, retry, or fabricate a predicate.

Selecting or focusing a candidate must also persistently display an assignment-bound, non-
interactive visual overlay on the target-pre screenshot. Agent A/B/C use distinct labels and
colors; point predicates show every region and center mark; drag predicates show start/end regions
and a direction arrow only when the endpoints are unambiguous; non-spatial predicates state that
they have no fixed screenshot coordinate. This overlay is explanation only, never evidence or an
executed coordinate, and it yields to the existing explicit manual coordinate picker.

## 6. HTTP boundary

The only additive routes are:

- `GET /api/assist/action-gold/{assignment_id}`;
- `GET /api/assist/progress`;
- `POST /api/assist/candidate-decisions`.

They require the existing loopback, same-origin, authenticated solo session. Candidate access is
allowed only for the currently open `ACTION_GOLD` solo assignment. The assist component has no
generation endpoint and no formal/solo annotation-journal write method. Routes named generate,
regenerate, rank, merge, winner, consensus, accept-all, save-review, submit, finalize, lock, export,
admit, replay, or execute MUST NOT exist.

The three assist routes are registered only when a sealed campaign is mounted on a
`SOLO_FIRST_PASS` application. A formal application does not register them and uses the campaign
root only through the locked exposure-eligibility boundary; candidate responses are never
available to a formal session.

## 7. Safety and disclosure

The annotation server does not instantiate a model/provider client, start a subprocess, access an
external network, probe/use a GPU, load weights, replay a task, execute a MobileWorld action, or
generate a treatment response. Candidate generation is performed only by the three explicitly
authorized Codex research streams before outputs are frozen; the website never triggers it.

The campaign receipt MUST record `ai_semantic_suggestion_performed=true` and
`target_actor_model_invoked=false`, `project_gpu_used=false`, `external_network_used=false`,
`replay_executed=false`, `action_executed=false`, and
`treatment_response_generation_allowed=false`. It also records that blind task/GUI evidence entered
Codex agent context. The exposure binds both the current workspace-scoped identity and a
domain-separated owner-registry principal commitment that is reproducible across solo and formal
workspace keys without exposing the access secret. Formal registry/store admission MUST consume
the exposure set and reject a matching principal before authentication or any authoritative
operation. A principal exposed to candidates is `AI_ASSISTED_SOLO_CURATOR` and is excluded from
every future formal G1.6 reviewer/adjudicator role.

## 8. Additive schemas and acceptance

The normative schemas are:

- `schemas/g1_6_ai/ai_action_gold_candidate_campaign.schema.json`;
- `schemas/g1_6_ai/ai_action_gold_candidate_packet.schema.json`;
- `schemas/g1_6_ai/ai_action_gold_candidate_output.schema.json`;
- `schemas/g1_6_ai/ai_candidate_human_decision_event.schema.json`;
- `schemas/g1_6_ai/ai_candidate_human_exposure.schema.json`;
- `schemas/g1_6_ai/ai_candidate_generation_receipt.schema.json`;
- `schemas/g1_6_ai/ai_candidate_campaign_receipt.schema.json`;
- `schemas/g1_6_ai/ai_action_gold_candidate_browser.schema.json`.

Acceptance requires 190 unique packets, three distinct slot envelopes per packet, 570 total terminal
outputs, closed-schema/runtime parity, exact packet/prompt/output hashes, no forbidden evidence,
browser pseudonymization, explicit per-item decisions, append-only supersession, no auto-apply, zero
annotation-journal writes from assist routes, and unchanged formal/solo packet bytes when the
campaign is absent or present. The frozen campaign ID and terminal publication binding are
mechanically rederived; exact-action coordinates must be nonnegative and strictly inside the
target-pre pixel dimensions.
