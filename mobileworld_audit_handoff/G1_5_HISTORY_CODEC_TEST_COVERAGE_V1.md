# G1.5 History Codec Conformance Coverage v1

Scope: ALE-323 CPU checkpoint only. The focused suite is
`MobileWorld/tests/offline/test_g1_history_codecs.py`; it collects 28 tests and applies the same common
matrix to both real Codec implementations. This is requirements coverage, not live/GPU evidence.

| Contract requirement | Qwen evidence | MAI evidence |
| --- | --- | --- |
| Secret-free captured-shape fixture, Draft 2020-12 schema, source provenance | parameterized fixture/schema test | parameterized fixture/schema test |
| Deterministic extraction, stable IR/record/claim/relationship identity | common five-arm checkpoint, repeated `to_dict()` | same |
| SYSTEM/TASK/HISTORY/CURRENT_OBSERVATION/TOOL_PROTOCOL partitions | common checkpoint | common checkpoint |
| Immutable request-bound curated catalog, char + UTF-8 offsets, multilingual/emoji | common checkpoint plus stale/overlap/path/protected negatives | same |
| ORIGINAL exact identity | checked request SHA, zero diff/insertion, golden diff | same |
| MASK exact selected span and preserved minimal shell | `Step N: ; ` plus exact tool/ask results (alone or fixed-order combined) and current image | empty thinking content plus message/wrapper identity |
| Multiple MASK and multi-target ORACLE | same-container multi-record Qwen path | cross-container assistant messages |
| MASK_CORRECTION evidence/Sentinel authorship/current anchor | one new text block before current image | one new text block before current image |
| SHAM exactly one `BENIGN_SHAM` span | common checkpoint | common checkpoint |
| Non-target system/task/tool/model/settings/roles/order/images unchanged | G1.4 invariance plus data-URL and shell assertions | G1.4 invariance plus role/order/adjacency assertions |
| Machine diff, human golden diff, deterministic repeat | five checked-in request hashes and all-arm golden file | same |
| Exact `restore_original` reversibility | every arm plus multi-target case | same |
| Qwen malformed framing/ordinal/terminator/tool wrapper/opaque payload rejection | dedicated grammar negatives | not applicable |
| MAI hidden-image consecutive assistants, canonical/preserved `</think>` wrappers, whitespace-only inter-wrapper shell, tool JSON, current-image/visible-observation adjacency | not applicable | dedicated grammar and retention negatives |
| Unsupported treatment is never a silent Original treatment | parameterized core fallback test | same |
| Formal capsule guards remain false/zero | five tampered-guard cases | shared checkpoint code |
| Real Codec registry plus G1.4 runner integration | fake and LIVE preflight both stop before encode/send/normalize | same |
| Shared core/checkpoint has no model-ID branching or client/network/subprocess/GPU imports | static AST/source-boundary assertion | same |
| Content-addressed CPU publication, selected Codec/capability/IR/renderer/no-tokenizer bindings, fresh receipt reproduction | manifest/schema/hash assertions | same |
| Exact G1.3 source-record + G1.6 human-span conversion without fuzzy relocation | record-relative char/UTF-8 binding test | same |
| Human-authored correction alternatives, pinned-tokenizer counts, deterministic four-field tie-break, unavailable-tokenizer block | shared injected-counter test | same |
| Arbitrary admitted human draft → strict five-arm read-only history, exact correction anchors, machine/human target-only diff, reversible mapping | Qwen Step/semicolon eligible-shell positive | shared renderer path; MAI wrapper repair explicitly blocks under frozen whole-record rule |
| Clean-control profile → Original/Sham only plus summed-focal/sham pinned-token match | parameterized clean preview | same |
| Family-specific eligible-shell admission and unsupported repair fallback | only original protected Step/terminal-semicolon bytes; internal/nonterminal semicolon rejected | paired canonical thinking tags and leading `Thought:` extract; tool wrapper rejected; retained tool call causes typed causal-empty block |
| Preview public-input hardening and minimal browser projection | callback exceptions/nondeterminism/bool/string traps typed; no full request in serialized output | same |

Checked-in fixtures also bind the expected rendered-request SHA-256 for all 10 codec/arm combinations
and deterministic human-readable before/after mappings. No provider response, parser output, GPU use,
live endpoint, formal replay, or action execution is covered or claimed; those remain the exact D-028
10-call GPU backlog.
