# G1.5 History Codec Capabilities and Fallbacks v1

This document is normative for the additive G1.5 CPU checkpoint and inherits the frozen G1.2 failure
policy. It grants no live execution authority.

## Capability matrix

| Property | Qwen flat-progress | MAI raw-replay |
| --- | --- | --- |
| Codec ID | `mobileworld.g1.history-codec.qwen-flat-progress` | `mobileworld.g1.history-codec.mai-raw-replay` |
| History family | `flat_progress` | `raw_replay` |
| Contract | `v1` | `v1` |
| Level / scope | `VALIDITY_TRANSFORMATION` / `LIVE` | `VALIDITY_TRANSFORMATION` / `LIVE` |
| CPU-supported arms | all five | all five |
| Operations | `DROP`, single-anchor `REPLACE` | `DROP`, single-anchor `REPLACE` |
| Correction ownership | new `SENTINEL` text block before current image | new `SENTINEL` text block before current image |
| Preserves shell | step prefix/terminator/external suffix | message identity, canonical or preserved `</think>` wrapper, tool wrapper/JSON |
| Preserves multimodal/adjacency | yes | yes, including retained initial image or consecutive assistants after old-image removal |
| `live_ready` | `false` | `false` |
| Provider fallback | blocked before encode/send | blocked before encode/send |

All five arms are *offline representation capabilities*. No row means that a provider, endpoint, model,
GPU, parser, replay, or response path is currently enabled.

## Stable fail-closed groups

| Boundary | Representative stable codes | Required result |
| --- | --- | --- |
| Captured root/message/block drift | `CAPTURED_REQUEST_NOT_OBJECT`, `MESSAGES_MISSING`, `*_MESSAGE_SHAPE_MISMATCH`, `*_ROLE_MISMATCH`, `*_CONTENT_SHAPE_MISMATCH`, `CURRENT_IMAGE_INVALID` | extraction raises; provider disallowed |
| Qwen framing/grammar | `QWEN_PROGRESS_BLOCK_AMBIGUOUS`, `QWEN_PROGRESS_BLOCK_MISMATCH`, `QWEN_STEP_PARSE_FAILED`, `QWEN_STEP_ORDINAL_MISMATCH`, `QWEN_STEP_BOUNDARY_AMBIGUOUS`, `QWEN_STEP_TERMINATOR_MISMATCH`, `QWEN_TOOL_RESULT_WRAPPER_INVALID`, `QWEN_ASK_RESPONSE_INVALID` | extraction raises; no fuzzy recovery |
| MAI replay/protocol | `MAI_ASSISTANT_CONTENT_INVALID`, `MAI_WRAPPER_AMBIGUOUS`, `MAI_WRAPPER_ORDER_MISMATCH`, `MAI_TOOL_WRAPPER_INVALID`, `MAI_BROKEN_OBSERVATION_ADJACENCY`, `MAI_HISTORY_ROLE_UNSUPPORTED`, `EMPTY_HISTORY_IR` | extraction raises; canonical and preserved `</think>` shapes plus old-image removal are accepted |
| Curated binding drift | `TARGET_BINDING_OBJECT_INVALID`, `TARGET_BINDING_ID_MISSING`, `TARGET_BINDING_PATH_NON_CANONICAL`, `TARGET_BINDING_REQUEST_DIGEST_INVALID`, `TARGET_BINDING_COORDINATE_INVALID`, `TARGET_BINDING_TEXT_INVALID`, `TARGET_BINDING_SPAN_DIGEST_INVALID`, `TARGET_BINDING_ROLE_INVALID`, `DUPLICATE_TARGET_BINDING`, `OVERLAPPING_TARGET_BINDINGS`, `TARGET_BINDING_REQUEST_SET_MISMATCH`, `TARGET_BINDING_REQUEST_MISMATCH`, `TARGET_BINDING_PATH_MISSING`, `TARGET_BINDING_STALE`, `TARGET_BINDING_AMBIGUOUS`, `TARGET_BINDING_OUTSIDE_EDITABLE_HISTORY`, `TARGET_BINDING_PROTOCOL_OVERLAP` | extraction raises before IR admission |
| IR/plan/core mismatch | frozen G1.2 `CODEC_*`, `PLAN_*`, target/path/text/hash/overlap/protected-span and evidence/anchor codes | validate/render raises or blocks; provider disallowed |
| Unsupported treatment | `UNSUPPORTED_ARM_OR_OPERATION` | `BLOCKED_BEFORE_PROVIDER`, `effective_arm=null`, `count_as_treatment=false`; never silent Original treatment |
| Shared correction anchor | `AMBIGUOUS_CORRECTION_ANCHOR` | multiple corrections at one anchor blocked; no implicit order |
| Capsule/CPU authority | `G15_CAPSULE_GUARD_INVALID`, `G15_CPU_CAPABILITY_INVALID`, `G15_CPU_PROVIDER_AUTHORIZATION_LEAK` | checkpoint aborts, no provider boundary |
| G1.4 runner authority | `PREFLIGHT_PROVIDER_AUTHORIZATION_BLOCKED`, `LIVE_EXECUTION_DEFERRED` | fake encode/send/normalize counts remain zero; live entrypoint disabled |

`PortableContractError` and `ReplayRunnerError` generated on these paths carry
`provider_invocation_allowed=false`. Repeating the same invalid input MUST reproduce the same code and
deterministic detail without consulting environment state.

## Fallback rules

1. ORIGINAL is a requested arm, not a fallback for a failed treatment.
2. G1 scientific mode always uses `FailurePolicy.BLOCK`; `BEST_EFFORT_ORIGINAL` is not admitted.
3. A stale/ambiguous target is not relocated even if its exact text occurs elsewhere.
4. A targeted semantic interval may become empty, but its host protocol shell/message remains unless a
   future explicit plan and contract authorize shell removal.
5. External tool/ask observations, current screenshots, tool-call payloads, and actor authorship are
   never eligible fallback targets.
6. A correction with missing evidence, non-Sentinel authorship, stale anchor, or assistant-owned anchor
   blocks; it is never converted to Mask or Original.
7. No test, wrapper, or caller may change `live_ready=false` or any formal capsule guard to exercise a
   provider path. Live capability requires a new versioned seal and D-028 prerequisites.
8. Qwen's unescaped `; Step N: ` grammar requires the existing capture-provenance boundary admission;
   v1 does not claim that request text alone can disambiguate an adversarial actor-authored delimiter.
9. MAI v1 admits a final current screenshot. A distinct final current tool/ask text observation is
   unsupported and blocks until a versioned fixture/capability extension exists.
