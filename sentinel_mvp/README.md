# Sentinel MVP (Legacy Prototype)

> [!WARNING]
> This package is a legacy, single-host, offline prototype. It is retained as
> a behavioral reference for curated history transformations; it is not the
> formal G1 implementation, a production/runtime interceptor, an automatic
> verifier, or a six-model adapter layer.

This directory was built from the historical Seed baseline audit to validate
derived-history plumbing for one host:

1. reconstruct the history visible before a selected Seed decision;
2. represent a curated claim, its evidence, and an explicit transformation;
3. apply a claim/span-level history operation;
4. render the resulting Seed history view;
5. preserve the untouched source history and provenance in a sidecar record.

The prototype does not call a provider, mutate MobileWorld, infer whether a
claim is true, or establish that a curated transformation improves an agent's
next action.

## Relationship to the current project

Epic 1 is complete: six host-native history representations were collected and
audited across 702 MobileWorld model-task cases. G1.1's immutable pre-gold
causal-replay protocol/registry and G1.2's portable contract are also complete.

The accepted portable package lives at
`MobileWorld/src/mobile_world/offline/causal_replay/`; this legacy package must
not be treated as that formal implementation. ALE-321 / G1.3 completed and
formally published a separate CPU-only offline capsule layer from frozen
Collector v1 artifacts. The formal G1.3 implementation does not import this
legacy package and does not modify its legacy code, tests, or fixtures; this
README-only scope synchronization does not change the prototype's behavior.

G1.3 targets exactly 190 frozen units: 152 strict-MHR candidates plus 38
selected clean controls. The separate 38 reserve clean controls are census-only
and produce neither a capsule nor an exclusion.

G1.3 does not apply a treatment or choose a Transformation Plan. It freezes
exact recorded decision inputs and visibility boundaries with
`deployment_prediction=false`; it does not include automatic claim extraction,
factual verification, correction generation, provider/model invocation, GPU,
GUI/action/replay, or live runtime interception. This README is not broader
implementation authorization. ALE-322 / G1.4 now has a separate CPU-only build
authorization under D-025 in `mobileworld_audit_handoff/G1_4_DECISION_LOG.md`, but this legacy package remains unchanged and is not
part of that runner; live model/provider/GPU proof remains deferred.

See the [project overview](../README.md), [current status](../mobileworld_audit_handoff/STATUS.md),
and [G1 causal-replay protocol](../mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md).

## Legacy gate operations

- `KEEP`: retain the selected span.
- `DROP`: remove a targeted, safely removable span.
- `REPLACE`: substitute a minimal curated correction.
- `ARCHIVE`: remove a true but inactive-branch span from the active view
  while preserving it in the sidecar.
- `KEEP_UNCERTAIN`: retain content when the curated decision abstains.

The prototype masks only the targeted span. A replacement is emitted as a
Sentinel-authored block rather than rewriting an old assistant message as if
the actor had originally said it. The raw history remains unchanged.

## Seed representation captured by the prototype

The historical fixture used Doubao Seed-2.0-Pro through MobileWorld's
`seed_agent`. Its default `history_n=3` limits retained images, not prior
assistant text:

- before action t, all earlier assistant responses P1 ... P(t-1) remain;
- normally only the latest three screenshots remain once t >= 3;
- non-image tool/user-result messages are not removed by the image counter.

This fixture illustrated why a textual claim can remain active after its
original visual evidence has left the prompt. It is not one of the six
canonical Epic 1 model audits.

## Legacy conceptual pipeline

~~~text
Seed fixture history + current GUI + curated transformation plan
                              |
                     deterministic gate
                              |
          KEEP / DROP / REPLACE / ARCHIVE / KEEP_UNCERTAIN
                              |
                    Seed-specific renderer
                              |
                 derived active-history view
                  + transformation sidecar
~~~

The original actor was not called on the rendered output.

## Reproducibility limitations

The checked-in fixtures and demo output preserve historical absolute
screenshot references and hashes. The source QR-MW trajectory tree and image
bytes are not part of this repository. In addition, the legacy demo runner
defaults to a no-longer-tracked v1 decision filename while the retained
decision artifact is v2. Therefore the old end-to-end commands are preserved
in source as provenance but must not be presented as a portable current
workflow.

The pure transformation modules and tests remain useful as historical
behavioral examples. Any new executable work belongs in the formal G1 package
and must follow the current handoff contracts rather than extending this
prototype in place.

## Scope boundary

Every fixture decision is curated/gold, not a deployment prediction. The
prototype validates serialization, filtering semantics, and reversible audit
records only. It provides no evidence that automatic verification, online
rubric tracking, or a runtime pre-call gate is implemented or calibrated.
