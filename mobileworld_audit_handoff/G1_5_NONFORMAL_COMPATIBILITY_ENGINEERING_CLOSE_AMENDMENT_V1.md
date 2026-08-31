# G1.5 Non-Formal Compatibility Engineering-Close Amendment v1

Contract ID: `mobileworld.g1.history-codec-engineering-close/amendment-v1`

Date: 2026-08-31 UTC

Status: owner-approved, additive, post-hoc scope amendment

## 1. Purpose

This amendment separates completion of the ALE-323 History Codec engineering delivery from formal
live readiness. It accepts the completed provider-free Qwen flat-progress and MAI raw-replay Codec
implementations, their deterministic CPU publication and preview surface, and the already sealed
non-formal production-prompt/parser compatibility observations. It does not retroactively turn those
observations into execution of the formal History Codec-to-Provider Codec path.

After the evidence conditions in this amendment are met, the two authoritative state axes are:

- `engineering_close_status=CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED`; and
- `formal_live_ready_status=DEFERRED_TO_G1_7_NOT_AUTHORIZED`.

The shorter labels `G1.5 live-ready`, `formal live smoke passed`, and `replay ready` are not
equivalent and MUST NOT be used. Both v1 Codec declarations remain exactly `live_ready=false`.

## 2. Amendment to the D-028 completion taxonomy

`G1_5_DECISION_LOG.md` D-028 and `G1_5_HISTORY_CODEC_CONTRACT_V1.md` remain the historical and
technical record of the CPU checkpoint and the future formal ten-call matrix. The ten-call matrix is
no longer a prerequisite for closing the bounded ALE-323 engineering delivery. It becomes an explicit
G1.7 formal-live-readiness duty instead.

This amendment changes no frozen or hash-bound G1.5 CPU publication byte, schema, receipt, fixture,
golden diff, capability declaration, or G1.4 evidence byte. In particular, the following facts remain
false:

- execution of the real History Codec to formal Provider Codec path;
- a hash-bound `live_ready=true` capability or admission seal;
- formal Provider Codec acceptance or SDK hidden-retry proof;
- complete per-attempt final-request, diff, mapping, response, usage, latency, error, retry, and
  delivery evidence;
- exact serving-environment equivalence;
- backend-dependency, fresh-session, or KV-cache isolation proof;
- G1.7 run-ready, execution-authorization, or formal-replay seals;
- treatment-response generation, formal replay, or generated action execution.

The sealed D-035 G1.4 manifest's `claims.g15_complete=false` field remains immutable and historically
correct under the pre-D-036 D-028/formal-completion taxonomy. D-036 does not rewrite that evidence or
reinterpret it as a formal G1.5 pass; it adds a separate, later governance decision that closes only
ALE-323's bounded engineering axis. Consumers MUST evaluate the D-035 manifest together with D-036
rather than treating the historical field as the current engineering-story state.

## 3. Accepted engineering evidence

The engineering-close evidence consists of:

1. the production Qwen flat-progress and MAI raw-replay Codec implementations, including exact
   extraction, immutable curated-span binding, five-arm rendering, target-only diffs, reversible
   source mappings, and deterministic fail-closed behavior;
2. the pure preview API for human-supplied focal, oracle, sham, delimiter, and correction inputs;
3. the shared schema, structure-preserving secret-free fixtures, golden diffs, conformance tests, and
   G1.4 fail-closed integration;
4. the checked-in CPU publication manifest with SHA-256
   `cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018`;
5. the accepted Codec implementation commit
   `3f56238c1eef7bb948dd60a77fb23c12dbacf2ea` and preview commit
   `7b096f51d2bfdd3b91e12f0340e65080881144c9` in the downstream governance tree;
6. the 28 focused History Codec regressions; and
7. the ten G1.5 arm-shaped calls inside the D-035 sealed 22-call run, accepted only as non-formal
   syntactic compatibility observations: ten HTTP 200 responses, ten successful existing-host-parser
   projections, zero retry, and zero generated-action execution. The binding D-035 manifest SHA-256 is
   `f70cee09e4870f3b0ab8dcd0d187efacd49362731c976b0872b4243600305179`.

The D-035 observations do not invoke the checked-in G1.5 Codec implementation. They are supporting
compatibility evidence, not the formal ten-call matrix and not proof that the rendered output was
produced by the accepted Codec bytes.

## 4. Runtime truth and limitations

The accepted CPU evidence establishes that both Codecs structurally extract and reconstruct their
admitted host-native history families while preserving non-history request content under the frozen
G1.2 core. The accepted live observations establish only that the ten prebuilt arm-shaped synthetic
requests were accepted by the observed Qwen/MAI services and parsed by the production host parsers.

The compatibility runner used Python standard-library HTTP rather than the formal
`OpenAICompatibleProviderCodec`. Its source/runtime/configuration association is post-hoc. It ran on
GPU4 with vLLM 0.19.1 and other disclosed differences from the frozen formal environment. It did not
seal the complete final request, Codec diff and reversible mapping, provider envelope, usage, latency,
error, application-visible attempt/retry, or SDK-hidden-retry evidence required for formal readiness.
It did not prove exact serving equivalence, `backend_dependency=NONE`, fresh invocation/session
isolation, or absence of KV-cache carry-over.

The smoke packet was secret-free and non-case. It was not a formal capsule, generated no formal
treatment response, performed no replay or backend restore, and executed no returned GUI/tool/action.
All G1.3 authorization guards remain false.

## 5. G1.7 handoff

G1.7 owns the deferred formal-live-readiness work and MUST complete it before any G1.8/G1.9 treatment
generation:

- execute the actual accepted History Codec through the formal `OpenAICompatibleProviderCodec` and
  existing host parser for Qwen flat-progress and MAI raw-replay;
- retain, per arm and Codec, the complete final request, Codec diff, reversible source mapping,
  provider envelope, raw/normalized response or error, usage, latency, every application-visible
  attempt/retry, parser classification, and common block seed;
- prove the exact SDK/client/version and zero hidden SDK retry behavior;
- prove the accepted serving environment, `backend_dependency=NONE`, and fresh invocation/session/
  KV-cache isolation; and
- issue separate hash-bound live-admission, run-ready, execution-authorization, and formal-replay
  seals without mutating the v1 capability bytes.

The formal matrix remains **2 Codecs x 5 arms x 1 logical invocation = 10 logical provider
invocations**. Any model/provider/GPU invocation requires a new explicit owner authorization. Returned
actions remain inert data and MUST NOT be executed. Failure of a future G1.7 formal gate does not
reopen the completed ALE-323 engineering delivery; a semantic change to Codec bytes requires a new
versioned publication and decision.

## 6. Acceptance and governance

Engineering close becomes effective only when:

1. this additive amendment and D-036 are committed on a clean tree;
2. no frozen G1.5 CPU publication, capability, receipt, schema, fixture, or G1.4 D-035 evidence byte is
   modified;
3. the 28 focused History Codec regressions and repository diff checks pass;
4. the decision log, AGENTS files, STATUS, READMEs, ALE-323, and ALE-325 use the exact two-axis state
   and G1.7 handoff above; and
5. independent review finds no remaining P0/P1 within this amended scope.

This amendment grants no model, provider, network, GPU, replay, treatment, backend, GUI, tool, or
action authority.
