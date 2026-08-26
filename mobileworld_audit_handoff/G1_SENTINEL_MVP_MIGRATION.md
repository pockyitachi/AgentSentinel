# Migration from `sentinel_mvp` to the G1 Portable Contract

Status: **Compatibility note for ALE-320 / G1.2**
Applies to: `mobileworld.g1.portable-sentinel/contract-v1`
Legacy source: repository directory `sentinel_mvp/`
Migration date: 2026-08-26 UTC

## 1. Disposition

`sentinel_mvp` is retained as a legacy behavioral reference, not as the production G1 replay
package. The G1 implementation lives under `mobile_world.offline.causal_replay` and MUST NOT
import `sentinel_mvp`.

ALE-320 migrates the useful operation vocabulary, exact-span intent, conservative validation,
caller-input immutability, correction attribution, and sidecar provenance into a new portable
contract and conformance suite. It does not preserve legacy output bytes where those bytes
conflict with the locked G1.1 protocol.

This is migration by explicit semantic mapping, not a wrapper around the old Seed-specific code.
The old directory remains unchanged so prior artifacts can still be inspected.

## 2. What the legacy MVP established

The legacy package demonstrated several useful ideas before G1 was authorized:

- a five-operation vocabulary: `KEEP`, `DROP`, `REPLACE`, `ARCHIVE`, and
  `KEEP_UNCERTAIN`;
- a distinction between evidence status and a rendering operation;
- claim IDs, record IDs, source spans, original text, and evidence references;
- deterministic application of multiple non-overlapping edits in descending offset order;
- preservation of caller-owned history values;
- conservative abstention for missing records, ambiguous IDs, mismatched text, weak evidence,
  and some overlapping edits;
- Sentinel-authored correction context rather than an unmarked actor assertion;
- a Seed-specific history adapter that preserves all prior assistant text while limiting its
  screenshot window;
- a derived replay result containing raw/filtered values, hashes, operations, warnings, evidence,
  and audit metadata.

Those are design inputs. The legacy replay was still a deterministic rendering demo over curated
Seed fixtures. It did not send a complete application-layer request to a real provider and is not
causal replay evidence.

## 3. Component mapping

| Legacy component | Useful responsibility | G1.2 destination | Disposition |
| --- | --- | --- | --- |
| `sentinel/contracts.py` | operation, claim, evidence, and output vocabulary | versioned portable types and schemas | concepts migrated; types replaced |
| `sentinel/history_filter.py` | non-mutating span validation and deterministic edit order | Sentinel Core plus protocol validator | semantics tightened; renderer replaced |
| `sentinel/seed_adapter.py` | Seed message parsing, screenshot window, host rendering | `raw_replay` fixture mapping; future G1.5 live codec | fixture behavior represented; code not imported |
| `sentinel/replay.py` | curated fixture validation, provenance, hashes, sidecar-style result | conformance kit and derived G1 sidecar | audit ideas migrated; replay path replaced |
| legacy fixtures | deterministic examples from Seed baseline | synthetic/redacted six-family fixtures | absolute-path research fixtures not reused as conformance input |
| legacy unit tests | examples of intended safety behavior | portable core and six-family conformance tests | useful assertions migrated; old suite is not the G1 gate |

## 4. Operation compatibility

### 4.1 `KEEP`

The portable `KEEP` preserves its exact bound source. It is compatible with the legacy operation
at span level. It is not equivalent to the G1 `ORIGINAL` arm: Original is a whole-request
invariant covering every history and non-history field.

### 4.2 `DROP`

The legacy generic filter replaces a dropped span with
`[SENTINEL: directly refuted claim removed]`; the Seed adapter deletes it. G1 resolves this
inconsistency in favor of the locked protocol: `DROP` is a pure deletion of exact target bytes.
It inserts no marker, instruction, or explanation. Only separately declared, protocol-eligible
empty-shell repair may remove additional syntax.

### 4.3 `REPLACE`

The legacy generic filter puts a marker in the old record and emits a correction block, while
legacy tests still expect an inline corrected actor record. The Seed adapter instead removes the
bad source span and appends correction context to a user observation.

G1 makes one rule authoritative: the misleading target is removed and the exact curated
correction is added at a codec-declared, host-safe location as explicitly Sentinel-authored
context. It is never rewritten into historical assistant speech. The correction has evidence
provenance and may not contain a next-action recommendation, gold predicate, or post-target
evidence.

### 4.4 `ARCHIVE`

The canonical name is retained for future full-transformation codecs. It requires an explicit
curated inactive-path target and is never inferred by the core. It is not used to implement G1
`MASK`, `ORACLE_CLEAN`, or `SHAM_BENIGN_EDIT`, all of which use exact `DROP` semantics under the
locked protocol.

### 4.5 `KEEP_UNCERTAIN`

The non-editing disposition is retained. It records that source text stayed unchanged. In a
future runtime this can accompany an explicit Original fallback, but in `G1_SCIENTIFIC` mode it
cannot silently replace a requested treatment or be counted as that arm. An unsupported or
invalid scientific plan fails before provider invocation.

The five names are retained as vocabulary/schema compatibility, not as a generic five-operation
execution API in G1.2. The current G1 arm projection executes only exact `DROP` and `REPLACE`;
`ORIGINAL` carries no operation, `KEEP`/`KEEP_UNCERTAIN` cannot substitute for a requested
treatment, and `ARCHIVE` remains reserved for a future full-transformation contract.

## 5. Strengthened source binding

The portable contract intentionally rejects several legacy conveniences:

| Concern | Legacy behavior | Portable G1 rule |
| --- | --- | --- |
| coordinate space | character offsets over a selected record | matching half-open character and UTF-8 byte spans |
| source identity | record ID plus text/span | request path, message/block/record coordinates, record hash, span text and span hash |
| omitted span | Seed adapter treats absent start/end as whole record | whole-record scope must be explicit and hash-bound |
| empty span | legacy dataclasses allow `start == end` | material target must be non-empty |
| relocated span | Seed adapter uniquely searches `original_text` after offset mismatch | no fuzzy recovery or relocation |
| duplicate/ambiguous target | some paths abstain per operation | entire scientific treatment is invalid |
| overlap | generic filter downgrades conflicting operations while the Seed path has separate logic | overlapping material spans invalidate the complete plan |
| application order | descending character offsets | deterministic reconstruction from validated dual-coordinate source spans after the full plan validates |
| failure policy | may preserve source as `KEEP_UNCERTAIN` and continue | G1 scientific path fails closed before provider; future runtime fallback is explicit |

This strictness is necessary to prove that history was the only intended application-request
difference.

## 6. Host and request fidelity changes

### 6.1 Full request, not a prompt fragment

The legacy replay explicitly constructs a host-composition fragment. System/task prompts, tools,
provider arguments, and final image resolution remain outside that fragment. It therefore cannot
establish Original equivalence or non-history invariance.

The portable History Codec begins with the untouched captured application-layer request. It maps
history inside that request and renders a complete final request. Original must preserve all
roles, message order, multimodal blocks, current observation, tool schemas, model fields, and
sampling fields.

### 6.2 No synthesized observation semantics

Legacy Seed fixtures convert screenshot path/hash metadata into adapter-specific observation
objects and correction blocks. Those objects are adequate for the demo but are not proof of the
exact content blocks received by a provider.

G1 never substitutes a description or fixture convenience for a captured observation block. A
codec preserves the exact current-observation structure and places a correction only through an
explicitly supported Sentinel-authored context channel.

### 6.3 Protocol relationships are explicit

Legacy records are primarily flat text with Seed step IDs. The portable IR also records roles,
multimodal blocks, tool-call/result pairing, surrounding observations, folded source versions,
action/result alignment, and structured H/L/M membership. A target cannot orphan or reorder a
host-required relationship.

The IR preserves host-visible absence as well as presence. For example, the previous-actions
fixture keeps the ordered actions and the unique current screenshot but explicitly records that
historical result records are not model-visible; it does not fabricate results. Likewise, a
rolling-summary source version that is no longer in the current request is a content-addressed
external lineage reference rather than invented prompt content. Every task/system/history/current
observation/tool-protocol region is marked present, co-located, or absent from the host contract.

### 6.4 Six families, not one Seed adapter

Seed behavior supplies one useful `raw_replay` example. It does not prove portability. G1.2 uses
one conformance contract for all frozen family IDs:

```text
raw_replay
flat_progress
rolling_summary
flat_previous_actions
hybrid_folding
structured_folding
```

These are fixture mappings only. Live `flat_progress` and `raw_replay` codecs belong to G1.5;
production live adapters for the other four are not G1 requirements.

## 7. Evidence and correction changes

Legacy `EvidenceRef.direct` is a useful safety cue but is insufficient for G1 provenance. The
portable plan binds each evidence reference by immutable identity/hash and optional non-negative
event sequence. G1.2 validates format, uniqueness, and collision consistency; the later frozen
curation/admission gate—not this package—proves request-cut-off availability and channel
eligibility. Curation provenance remains separate from history syntax.

Some legacy replay fixtures store a direct pre-target screenshot and a recorded downstream
outcome screenshot in the same broad `evidence_screenshots` collection. The replay code limits
target evidence use, but the artifact does not express the G1.1 channel separation. Portable G1
artifacts keep eligibility-only, transformation, and later outcome evidence in their authorized
channels; a correction cannot reference target output, post-state, later trajectory, evaluator,
or final outcome.

## 8. Reversibility and sidecar changes

The legacy replay preserves original history and records selected before/after strings and
hashes. The portable sidecar generalizes this into a complete derived audit envelope containing:

- untouched and final application requests plus hashes;
- canonical IR and curated plan plus hashes;
- the complete canonical paired plan set, its profile, and recomputed digest;
- exact target edits and shell repairs;
- retained, deleted, and Sentinel-generated segment mappings;
- evidence and correction provenance;
- codec/provider capabilities, warnings, validation, and fallback state;
- provider response and parsed action only after a later story is authorized to invoke one.

Reversibility is tested for exact source bytes, not merely for the presence of an original string
somewhere in the output object.

## 9. Current legacy test status

The legacy suite was inspected, not modified. On 2026-08-26 UTC, this CPU-only command was run
from the repository root:

```bash
PYTHONPATH=sentinel_mvp python -m unittest discover -s sentinel_mvp/tests -v
```

The observed result was 18 tests: 11 passed, 6 failed, and 1 errored. The failures are legacy
contract/test drift, not regressions introduced by the portable package:

- two generic-filter assertions expect inline correction bytes, while the implementation emits a
  marker plus a correction block;
- all four failing Seed-adapter assertions omit `epistemic_status="REFUTED"`; the adapter defaults
  it to `UNVERIFIABLE` and conservatively preserves history despite direct evidence;
- replay test setup asks for `curated_gate_decisions_v1.json`, while the repository contains
  `curated_gate_decisions_v2.json`;
- the replay fixtures and README pin machine-specific absolute macOS paths and therefore are not
  portable test data in this checkout.

Consequently, the old command is documented evidence about the legacy snapshot, not an ALE-320
acceptance gate. G1 does not claim that the old suite is green.

## 10. Test migration matrix

The useful intent of the legacy tests is carried forward as follows:

| Legacy test intent | Portable conformance requirement |
| --- | --- |
| keep/uncertain preserves text | exact Original/non-editing equality |
| direct drop removes one claim | pure target-only Mask diff |
| replacement has grounded correction | Sentinel-authored correction with evidence provenance |
| weak/missing evidence preserves history | invalid G1 plan fails before invocation; runtime fallback is explicit |
| span mismatch fails closed | dual-span/text/hash validation with no fuzzy relocation |
| multiple non-overlapping edits are deterministic | canonical order and deterministic output hash |
| overlapping edits abstain | complete plan rejection before rendering/invocation |
| caller inputs remain unchanged | deep raw-request, IR, and plan immutability checks |
| Seed text and image-window ordering survives | `raw_replay` fixture relationship/round-trip checks |
| replay result contains audit data | versioned full-request sidecar and reversible mapping checks |

The new conformance kit adds requirements the legacy tests never covered: all six families,
complete request round-trip, non-history equality, multimodal and tool pairing, capability
declarations, machine-readable unsupported reasons, scientific no-invocation proof, and visible
future-runtime fail-open state.

## 11. Compatibility policy

For G1, the portable contract and the locked causal protocol are authoritative. Compatibility
means:

1. preserve the useful conceptual behavior listed in Section 2;
2. explicitly document every changed semantic in this file;
3. cover the migrated intent in the portable conformance suite;
4. leave legacy artifacts inspectable and avoid pretending they are provider replay evidence;
5. never import legacy implementation code into the formal package.

No future change should make legacy snapshot tests green by weakening G1 target binding,
reintroducing marker-based Mask, allowing fuzzy span recovery, attributing a correction to an old
actor, or silently sending Original for an unsupported scientific treatment.

## 12. Scope confirmation

This migration note authorizes no provider call, response generation, GPU use, action execution,
live prompt intervention, decision-capsule materialization, gold curation, or Collector change.
All migrated outputs remain offline derived artifacts with `curated=true` and
`deployment_prediction=false` where a Transformation Plan is present.
