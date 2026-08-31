# G1.4 Non-Formal Live-Smoke Engineering-Close Amendment v1

Contract ID: `mobileworld.g1.exact-request-replay-engineering-close/amendment-v1`

Date: 2026-08-31 UTC

Status: owner-approved, additive, post-hoc scope amendment

## 1. Purpose

This amendment separates completion of the ALE-322 engineering delivery from formal replay
readiness. It narrows the accepted engineering outcome to the already completed CPU/fake exact-
request runner, inert live-preparation code, and one production-shaped, non-formal two-model
compatibility smoke. It does not retroactively turn that smoke into formal Provider Codec, serving-
environment, isolation, treatment, or replay evidence.

After the evidence conditions in this amendment are met, the two authoritative state axes are:

- `engineering_close_status=NONFORMAL_LIVE_SMOKE_PASSED`; and
- `formal_replay_status=DEFERRED_TO_G1_7_NOT_AUTHORIZED`.

The shorter labels `G1.4 complete`, `formal live proof passed`, and `replay ready` are not equivalent
and MUST NOT be used. `ENGINEERING_COMPLETE_FORMAL_REPLAY_DEFERRED` may appear only as an explanatory
summary, never as a replacement for the owner's exact engineering-close label.

## 2. Amendment to the original completion taxonomy

Section 9 of `G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md` remains authoritative for future
formal replay readiness. Its G1.5/G1.6/G1.7 prerequisites are no longer prerequisites for closing
the bounded ALE-322 engineering delivery. Instead, they are explicit deferred dependencies owned by
the future G1.7 preflight/formal-replay tranche.

This amendment changes no formal G1.3 capsule byte or authorization guard. The following facts remain
false:

- formal Provider Codec acceptance;
- formal serving-environment equivalence;
- G1.5 live-ready capability;
- G1.6 formal paired plan, gold, admission, or seal;
- G1.7 run-ready or execution authorization;
- backend-dependency and fresh-repeat isolation proof;
- formal replay readiness or execution;
- treatment-response generation;
- generated GUI/tool/action execution.

## 3. Accepted engineering evidence

The engineering-close evidence consists of:

1. the committed G1.4 CPU/fake checkpoint and inert live-preparation checkpoint;
2. the committed simple runner, frozen 22-call secret-free fixture, and CPU tests;
3. exact bindings to the existing production system-prompt renderers and host parsers;
4. one completed GPU4 compatibility run with Qwen followed by MAI;
5. exactly 12 G1.4 repeated canaries and 10 G1.5 codec-arm compatibility calls;
6. 22 HTTP 200 responses, 22 successful existing-host-parser projections, zero retry, zero fallback,
   and zero generated-action execution;
7. release of both owned services, their process/session trees, the loopback port, and their GPU
   allocation without signalling or modifying the foreign GPU process; and
8. a schema-valid manifest plus read-only content-addressed copies of exactly `run.jsonl`,
   `qwen.server.log`, and `mai.server.log`.

The manifest schema is
`schemas/g1_4/nonformal_live_smoke_manifest.v1.schema.json`. The manifest MUST bind the clean source
commit and the exact runner, fixture, tests, model manifest, prompt sources, parser sources, evidence
objects, runtime facts, known configuration differences, and deferred claims.

## 4. Runtime truth and limitations

The accepted smoke proves production prompt/parser semantics on the observed compatibility runtime.
It is not an exact frozen-serving-config proof.

The manifest MUST disclose exactly the versioned difference census accepted by this amendment,
including at least these differences from the frozen G1.4 manifest:

- GPU index 4 was used instead of the earlier GPU0 D-034 boundary;
- vLLM 0.19.1 was used instead of frozen vLLM 0.11.0;
- the runner used Python stdlib `http.client`, not the frozen OpenAI SDK 1.106.1 Provider Codec;
- the request timeout was 180 seconds instead of 120 seconds;
- the server used shared Miniconda Python plus `vllm_env`, not the frozen reference environment;
- the observed torch/transformers/flashinfer package versions differ from the frozen package set;
- the 0.19.1 launch did not express the frozen `swap_space_gib=0` setting, so equivalence is
  unproven; and
- ambient child environment outside the runner's closed overrides was inherited and was not sealed.

`generation_config_mode="model" -> --generation-config auto` is the existing contract's explicit
semantic mapping and is not itself a discrepancy.

The live run did not contemporaneously record runner, fixture, prompt, parser, configuration, or
interpreter hashes. Their exact associations in the engineering-close manifest are post-hoc and
MUST be marked `post_hoc_association=true` and `contemporaneous_source_binding=false`; they are
verified against the preserved run and committed implementation but MUST NOT be described as a
pre-registered live binding.

The preflight and postflight GPU/`ps`/`ss` facts existed only in the owner-authorized main execution
transcript. Because their raw output was not written into the run directory, the manifest MUST say
that raw host-probe receipts are unavailable. It may record the observed summary but MUST NOT call it
a formal host-evidence seal. The owned root and engine PID release is likewise a transcript-attested,
post-hoc reverified fact rather than a formal process-tree release receipt.

## 5. G1.7 handoff

The following work moves to G1.7 and MUST NOT reopen ALE-322 engineering delivery:

- enabling and accepting the formal `OpenAICompatibleProviderCodec` live path;
- exact SDK/client/version and hidden-retry proof;
- complete per-attempt request, response, usage, latency, error, and delivery receipts;
- exact frozen serving-image/environment equivalence;
- backend dependency and fresh-invocation/session/KV-cache isolation;
- run-ready, execution-authorization, and formal replay seals.

Any later GPU/model invocation, formal replay, treatment generation, or action execution still needs
a new explicit owner authorization. This amendment grants none.

## 6. Acceptance and governance

Engineering close becomes effective only when:

1. the implementation/amendment/schema commit is clean;
2. the external content-addressed evidence bundle is installed with no-replace semantics and reopened
   read-only;
3. the checked-in manifest is byte-identical to the external manifest and validates against its
   schema;
4. the manifest binds the implementation commit and all recorded artifact hashes;
5. focused CPU tests, History Codec regressions, lint, formatting, compilation, schema validation,
   dedicated manifest-verifier adversarial tests, and diff checks pass;
6. independent cold review finds no remaining P0/P1 within this amended scope; and
7. the decision log, AGENTS files, STATUS, and ALE-322 description use the exact two-axis state
   `NONFORMAL_LIVE_SMOKE_PASSED` / `DEFERRED_TO_G1_7_NOT_AUTHORIZED`.

G1.5 remains a separate story. Its ten calls in this smoke are compatibility coverage only and do not
close ALE-323.
