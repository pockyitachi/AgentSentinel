# G1 Replay Capsule Contract v1 — Amendment 1

Status: **ACCEPTED corrective amendment for ALE-321 / G1.3**
Document type: Normative compatible schema and publication correction
Amendment ID: `mobileworld.g1.replay-capsule/contract-v1-amendment-1`
Amends: `mobileworld.g1.replay-capsule/contract-v1`
Authorization: `DECISION_LOG.md` D-024
Protocol: `mobileworld.g1.causal-replay/protocol-v1` (unchanged)
Portable contract: `mobileworld.g1.portable-sentinel/contract-v1` (unchanged)
Decision date: 2026-08-27 UTC

## 1. Correction

Section 13 of `G1_REPLAY_CAPSULE_CONTRACT_V1.md` requires every valid capsule to
carry three independent, fail-closed authorization fields in its closed `safety`
object:

```json
{
  "execution_ready": false,
  "provider_invocation_allowed": false,
  "treatment_response_generation_allowed": false
}
```

The first published v1 implementation emitted `execution_ready=false` but omitted
the other two fields from the replay-capsule schema and capsule builder. This
amendment corrects that implementation mismatch. It does not change the scientific
protocol, frozen population, source evidence, visibility rules, or scope boundary.

## 2. Field semantics

The following fields are distinct and MUST NOT substitute for one another:

- `provider_invoked=false` is historical telemetry: no provider was called while
  materializing or validating the artifact.
- `provider_invocation_allowed=false` is an authorization guard: a downstream
  consumer is forbidden to invoke a provider from this G1.3 artifact.
- `treatment_response_generation_allowed=false` is an authorization guard: a
  downstream consumer is forbidden to generate a treatment response from this
  G1.3 artifact.
- `execution_ready=false` states that the capsule is not ready for experimental
  execution.

All three authorization/readiness fields MUST be present and exactly boolean
`false`. Missing fields, `true`, numeric stand-ins, strings, nulls, or any other
non-boolean values are invalid. JSON Schema `const: false` provides both value and
type enforcement because JSON booleans are distinct from numbers.

## 3. Version topology

The already-published v1 schemas remain byte-frozen and retain their historical
identifiers. They MUST NOT be edited in place:

| Record | Historical schema | Corrected schema |
| --- | --- | --- |
| Replay capsule | `mobileworld.g1.replay-capsule/v1` | `mobileworld.g1.replay-capsule/v1.1` |
| Publication manifest | `mobileworld.g1.replay-capsule-manifest/v1` | `mobileworld.g1.replay-capsule-manifest/v1.1` |
| Integrity report | `mobileworld.g1.replay-capsule-integrity/v1` | `mobileworld.g1.replay-capsule-integrity/v1.1` |

The corrected JSON Schemas have distinct filenames and `$id` values:

- `schemas/g1_3/replay_capsule.v1_1.schema.json`;
- `schemas/g1_3/capsule_manifest.v1_1.schema.json`;
- `schemas/g1_3/capsule_integrity.v1_1.schema.json`.

`capsule_exclusion.schema.json` and `field_visibility.schema.json` remain at v1.
An exclusion is not an emitted capsule, and the visibility policy classifies the
closed `/safety` metadata root without defining its children. Neither schema needs
a semantic change for this correction.

The G1 causal-replay protocol and portable Sentinel contract remain at v1. This is
a compatible correction to the G1.3 materialization contract, not a new arm,
population, intervention, codec, provider, or execution protocol.

The corrected builder identifies itself as
`mobileworld.g1.replay-capsule-builder/v1.1`. The v1.1 manifest's closed
`builder_contract` object MUST bind the builder, capsule, manifest, integrity, and
amendment identifiers explicitly so an artifact cannot mix historical and corrected
schema generations.

## 4. Corrected aggregate records

Every corrected publication manifest MUST expose both authorization guards as
`false` in its closed `safety` object. Its closed `readiness` object MUST also carry:

```json
{
  "execution_ready": false,
  "provider_invocation_allowed": false,
  "treatment_response_generation_allowed": false
}
```

Every corrected integrity report MUST expose both authorization guards as `false`
in its closed `safety` object alongside `provider_invoked=false`,
`treatment_response_count=0`, and `execution_ready=false`.

Structural and source-bound validators MUST select schemas by the artifact's
declared version. Historical v1 publications MAY remain structurally inspectable
under the frozen v1 schemas, but they MUST be identified as superseded and MUST NOT
receive current formal/source-bound acceptance. Only a corrected v1.1 publication
may be accepted as the formal G1.3 input for downstream work.

## 5. Validation requirements

In addition to the base contract's validation suite, conformance MUST prove that:

1. every emitted v1.1 capsule contains all three authorization/readiness fields
   set to boolean `false`;
2. capsule validation rejects each authorization field when missing, `true`, or
   non-boolean;
3. the manifest and integrity report carry consistent fail-closed guards;
4. two independent builds from the same immutable sources are byte-identical;
5. source-bound validation rebuilds the corrected publication byte-for-byte;
6. the frozen 152 strict-MHR plus 38 selected-control population is unchanged,
   and all 38 reserve controls remain census-only;
7. Collector v1, G1.1, G1.2, and the former G1.3 publication remain byte-unchanged;
8. no model, provider, GPU, GUI/action, replay, automatic semantic inference, or
   runtime Sentinel path is reached.

## 6. Historical publication and supersession

The former publication remains an immutable historical artifact at:

```text
/shared/linqiang/mobileworld_causal_replay_data/g1_3/capsules/sha256/
c2af8b8393e2df2da21bedcc98614e60a08b8254dc03da373ce72d67fe7c76c5
```

Its manifest SHA-256 is
`c2af8b8393e2df2da21bedcc98614e60a08b8254dc03da373ce72d67fe7c76c5`.
It MUST NOT be modified, repaired, chmodded, deleted, overwritten, or reused as the
destination for corrected bytes. After a corrected v1.1 artifact set passes all
required validation, that new content-addressed publication supersedes the former
publication for formal G1 use. The former path and hashes remain historically
identifiable.

## 7. Scope remains closed

This amendment authorizes only the ALE-321/G1.3 conformance correction. It does not
authorize ALE-322/G1.4 or any provider/model invocation, GPU use, GUI/action/replay,
treatment generation, automatic semantic inference, curation decision, raw-event
mutation, or runtime Sentinel behavior.
