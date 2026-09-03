# R2.4 Qwen and MAI Runtime Vertical Slices Contract v1

Status: **IN PROGRESS; CPU REMEDIATION CANDIDATE; OWNER RE-REVIEW PENDING; LIVE AUTHORITY UNISSUED; MERGE/LIVE NO-GO**

Contract ID: `mobileworld.runtime.r2-4-qwen-mai-vertical-slices/contract-v1`

Run-authority schema: `schemas/r2_4/run_authority_manifest.v1.schema.json`

CPU-topology schemas:

- `schemas/r2_4/topology_comparison.v1.schema.json`
- `schemas/r2_4/cpu_topology_artifact.v1.schema.json`

Live-rubric schemas:

- `schemas/r2_4/rubric_generate_output.v1.schema.json`
- `schemas/r2_4/rubric_track_output.v1.schema.json`
- `schemas/r2_4/rubric_backend_extension.v1.schema.json`
- `schemas/r2_4/rubric_call_receipt.v1.schema.json`

Decision date: 2026-09-03 UTC

## 1. Decision and claim boundary

R2.4 connects the accepted R2.1 seam and R2.2 validity policy to the R2.3
multi-path rubric for the two first runtime hosts:

```text
Qwen3-VL or MAI assembles the exact actor request
  -> common PromptSentinel seam
  -> versioned runtime History-Codec overlay
  -> one same-cutoff Collector evidence bundle
  -> isolated, history-free rubric generate/track/link
  -> evidence-grounded history policy
  -> deterministic admission, renderer, and invariants
  -> exact Original in OFF/SHADOW/fallback, candidate in authorized ACTIVE
  -> unchanged actor provider, parser, and action path
```

The repository candidate scaffolds this intended behavior with CPU fixtures,
injected fake transports, sealed resource/execution doubles, and strict
external-output contracts. It does **not** by itself establish that a live OpenAI request, GPU
model, MobileWorld backend, GUI action, smoke result, or task-effectiveness
result exists. Those claims require an owner-authorized manifest, a passing
production preflight, actual execution, and committed repo-external receipts.

The Sentinel edits only model-bound history. It does not select, recommend,
parse, or execute an actor action. The rubric is an independent task-path
observer; it is not history-truth evidence or action authority.

## 2. Runtime History-Codec overlay

Frozen G1.5 Codecs remain byte-unchanged and `live_ready=false`. R2.4 adds an
explicit runtime overlay declaration that binds:

- host family and frozen base Codec identity/version;
- base capability hash and overlay implementation hash;
- the exact runtime target-discovery algorithm; and
- a declaration hash carried by the additive R2.4 result/receipt.

The overlay first applies the frozen structural extractor. When the curated
binding catalog is empty, it may discover only the unique non-empty text gap
between frozen protected regions of an eligible history record. It then binds
the current request hash, character and UTF-8 coordinates, re-extracts using
that binding, and revalidates record topology, protected regions, target
census, and non-overlap. Ambiguity, unsupported message shape, image/current
observation mismatch, or an absent required region fails closed to Original.

First-step no-history is a typed `NO_HISTORY` result with an empty history-
policy decision census. It is not represented as a synthetic record or a fake
`KEEP` decision. In an owner-authorized non-OFF production call, the independent
rubric still performs task-start generation and current-state tracking from its
history-free Collector input; the history policy is not called and the actor
receives exact Original. The next history-bearing decision reuses that rubric
and performs only tracking plus history policy. An extracted history with no
eligible target similarly produces a typed zero-target bridge and no fabricated
history decision.

## 3. Collector and independent rubric input

One bounded, locked read of Collector JSONL produces a same-cutoff bundle for
both semantic axes. The bundle binds the exact task, task-run identity, current
`step_started` observation, image bytes/hash/dimensions, and only causally prior
completed-transition evidence. It excludes future events, outcome/checker
labels, actor reasoning, model identity, request-path details, action advice,
and raw actor history from the R2.3 input.

R2.3 rubric generation occurs once per task run. Tracking occurs once per new
Collector stimulus, not once per provider retry. Reusing one logical call
returns a detached cached result; assigning a second logical call to the same
stimulus fails closed. The coordinator executes task-start generation, state
tracking, and post-state relevance before returning the R2.2 evidence input.
Any rubric failure prevents the history-policy transport from starting.

Live rubric proof is accepted only against module-owned trust anchors. The
validator recomputes the fixed operation and prompt-bundle hashes and binds the
exact same-cutoff Collector stimulus, current-image bytes/hash/dimensions,
Responses envelope, provider-output hash, requested/returned model identity,
and usage to the coordinated logical call. A completed backend call without
the corresponding trusted Coordinator root fails closed; caller-supplied or
downstream-rehashed proof fields do not establish authority.

Until a separately trusted record-level R2.2 resolver is installed, record
relevance remains graph-derived (`ACTIVE_PATH`, `INACTIVE_BRANCH`,
`PATH_INDEPENDENT`, or `UNKNOWN`), while every v1 disposition remains
`RETAIN`; no runtime path executes `ARCHIVE`.

## 4. Validity policy and rendering

R2.4 preserves the accepted R2.2 SHADOW contract and introduces versioned
execution authorities rather than weakening it. CPU ACTIVE is admitted only
for an exact offline fake transport. Owner-authorized live ACTIVE additionally
requires a request-, case-, stage-, manifest-, preflight-, and transport-bound
lease.

The deterministic admission layer continues to support `KEEP`, `DROP`, and
`KEEP_UNCERTAIN`. A R2.4 `REPLACE` is permitted only as a renderer-owned closed
template promoted from an independently admitted `DROP`:

- a directly refuted fact uses the fixed refutation template; and
- a temporally invalidated fact uses a distinct stale-after-invalidation
  template.

Backend prose is never inserted into the actor request. Replacement target,
evidence, relation, reason, template, source descriptor, execution authority,
and complete policy output are hash-bound before rendering. Every candidate is
independently revalidated and exactly reversible. Any partial transformation,
binding drift, invalid output, uncertainty, timeout, audit failure, or renderer
failure sends the exact Original.

## 5. Host and retry invariants

The common seam branches on registered capability and execution scope, not on
actor model name. Qwen and MAI adapters only expose their native request shape
and logical-call context.

- `OFF` performs no Codec, rubric, or history-policy semantic work.
- `SHADOW` evaluates once but the actor provider receives exact Original.
- authorized `ACTIVE` evaluates once and the actor provider receives only a
  fully admitted candidate; otherwise it receives exact Original.
- Sentinel-owned OpenAI calls use `call_role=sentinel` and bypass the hook.
- Base transport retries, Qwen outer parse retries, MAI normalization, and a
  streaming attempt reuse the same logical Sentinel result.
- Caller requests, cached authority objects, public results, and nested
  records are recursively detached.
- Images, tool definitions, system/task text, current observation, message
  roles/order, and sampling/provider envelope remain invariant.

Production actor dispatch is non-streaming and bounded. The current case and
owner deadlines are checked immediately before each physical dispatch.
Provider model identity must equal the frozen served-model identity; proxy
environment inheritance is disabled for both actor and backend loopback
clients.

## 6. CPU topology comparison

The frozen topology artifact is produced by module-owned CPU execution, not by
accepting caller-supplied result hashes. The producer uses one matched
Collector stimulus and the real R2.3 session and GPT-5.6 policy admission paths
with fake providers:

- isolated evaluation performs two semantic provider dispatches, one rubric
  tracking call and one history-policy call;
- joint evaluation performs one shared provider dispatch and independently
  admits the resulting rubric and policy projections; and
- a separate joint-provider failure probe performs one dispatch and records
  both dependent outputs as unavailable.

The canonical artifact persists the complete comparison, component census,
latencies, agreement/divergence, failure coupling, and the failure probe. Its
strict parser re-derives all hashes and rejects the older comparison-only
shape. The selected pilot topology remains
`ISOLATED_HISTORY_FREE`; joint topology is explicitly non-independent and
cannot silently replace the isolated primary path.

## 7. Live authority and resources

A DRAFT manifest is planning data only. It grants no permission. A separate
promotion command may create a new `OWNER_AUTHORIZED` manifest only after the
operator supplies and confirms the exact DRAFT SHA-256. Promotion changes only
the authorization status, writes a fresh repo-external mode-0600 file, and
does not execute, inspect, or contact any resource.

The live sequence is fixed:

```text
deep resource preflight
  -> Qwen OFF / SHADOW / ACTIVE smoke
  -> MAI OFF / SHADOW / ACTIVE smoke
  -> R2.5 pilot only if every smoke case passes
```

The manifest binds the exact source commit, two actor snapshots and logical
tree digests, loopback endpoints/model IDs, two independent OpenAI Responses
stages (`RUBRIC`, then `HISTORY_POLICY`), captured smoke fixtures, R2.5 frozen
pilot, topology artifact, output root, expiry, and additive time/call/cost
budgets. Arbitrary shell commands are not accepted as manifest input.

Production preflight is side-effect-free. It verifies the owner-confirmed
manifest hash, clean/current Git authority, model trees, fixtures and pilot
artifacts, loopback endpoint shape, output freshness, and the external
secret's owner/mode/type metadata without reading the secret value. Executor
construction and the resource stage separately attest the operator-confirmed
pricing/runtime configuration, installed OpenAI SDK, vLLM executable, backend
environment metadata, and GPU/Docker/model state. Each later case lease binds
the passing preflight, task/reset/cell, logical call, raw request hash, and
deadline. The per-attempt authority additionally binds role, transport, and
call/token/cost limits.

Model and backend lifecycle code uses fixed commands, exclusive GPU leases,
owned PID/container identity, exact model registry checks, loopback health,
per-dispatch re-attestation, host-local disable/kill controls, bounded cleanup,
and retained recovery evidence when cleanup cannot complete. No CPU test
starts Docker, loads weights, opens the network, or executes a GUI action.

Each production unit freezes separate execution and cleanup deadlines within
the owner stage deadline. The reserved cleanup window is hash-bound to the
runtime configuration, cross-bound to the seven-second TERM/KILL/waitpid upper
bound, and requires at least eight seconds of production shutdown grace.
Insufficient authority window fails before I/O. Reset, runtime/model/backend,
task-goal, actor, action, score, and later-cell work may use only the execution
deadline; CLEANUP is the sole closed recovery stage that may use the cleanup
deadline. Expiry or teardown failure remains typed, journaled failure evidence
and cannot be promoted to success.

## 8. OpenAI secret, transport, cancellation, and accounting

The live key is a repo-external regular file owned by the current operator,
mode `0600`, non-symlink, and bounded in size. Preflight validates bounded size
metadata but never reads, hashes, logs, or persists the value or reports its
size. The production child may read it only after a request-bound attempt is
authorized and immediately before constructing the module-owned OpenAI client.
The parent process receives no key value.

The Responses transport binds the official endpoint, exact model, SDK version,
`store=false`, zero SDK retries, output/timeout bounds, proxy-disabled client,
manifest/preflight/case lease, and pricing table. Each rubric or history-policy
attempt runs in a clean spawned child. A deadline causes TERM, then bounded
KILL if necessary, followed by `waitpid`; the run cannot continue while worker
termination is unconfirmed.

Strict production scope suppresses the pinned OpenAI, httpx, and httpcore
standard loggers during the Sentinel Responses child setup/dispatch and actor
Chat Completions dispatch, including when `OPENAI_LOG=debug` is present.
Base-managed actor client construction is scoped as well. The scope is
restored after the call and does not change ordinary non-production
diagnostics. Request, task, evidence, image, or secret content must never reach
ordinary production logs.

Every sealed request component, including JSON Schema, is compared as
canonical JSON bytes rather than with Python container equality. JSON numeric
and boolean values are therefore type-distinct; a mutation such as `1` to
`true` fails before secret access or provider dispatch.

The accepted R2.3 v1 descriptor, receipt, and schema bytes remain unchanged and
cannot represent a live Responses call. Live rubric execution uses additive,
versioned R2.4 backend-extension and call-receipt records. The extension binds
the accepted R2.3 compatibility descriptor plus the sealed R2.4 provider,
configuration, schema, execution-scope, transport-authority, and requested
model identities. The R2.4 call receipt records both requested and returned
model; a missing or mismatched returned identity fails closed. CPU/fake mode
keeps both model fields null. An unreaped worker or otherwise unconfirmed
termination is recorded as `TERMINATION_UNCONFIRMED`, never ordinary `FAILED`.
The first exact occurrence trips a module-owned one-way run-fatal latch before
the final actor SDK gate. It blocks the current Original actor call and every
later reset, runtime/model, backend, task-goal, action, score, or cell dispatch;
only the bounded CLEANUP recovery stage may still run.

`DISPATCHED` is emitted immediately before the SDK provider invocation and is
conservatively counted as one attempt; the narrow signal-to-call crash window
may overcount, never undercount. Failure while opening the secret or
building/validating the request is zero dispatch and zero exact cost. A
post-dispatch cancellation or provider failure is one call; when provider usage
is unavailable, cost is explicitly unknown and the stage cannot pass. Known
usage and cost remain recorded even when they exceed a bound. Worst-case cost
is atomically reserved before dispatch and settled from the terminal receipt.

## 9. Audit and durable output

Live audit data is separate from Collector raw events and from the CPU-fake
detail contract. It uses owner-only repo-external directories/files and binds:

- logical call, task/case/cell, host, mode, and topology;
- raw, History IR, policy, rubric, render, exact diff, validator, candidate,
  and final request projections/hashes;
- the SDK arguments immediately adjacent to the actual actor call;
- actor attempt/response locator, parser result, action projection, and
  latency/census; and
- every OpenAI attempt authority, receipt, usage/cost status, and receipt root.

Hidden provider chain-of-thought is not requested or separately persisted in
the derived audit detail or ordinary production logs. Observable actor output
is still losslessly captured by Collector; if a model emits thought-like text
as ordinary output, it can also reappear in later host-native history and in
the owner-only raw/final request projection. Collector remains label-free.

Stage evidence is persisted as a canonical preimage, not as an orphan hash.
The sequence transaction writes resource, smoke, pilot, or failure-stage
evidence under mode `0700/0600`, fsyncs files/directories, and commits by atomic
rename. Pilot analysis is a later, separate fresh mode-0600 publication. Once
live work has occurred, a publication failure must retain recoverable
owner-only evidence and must never relabel it as success.

## 10. R2.4 live-smoke acceptance

For each host, all three cases must pass under the exact frozen fixture and
resource bindings:

1. OFF: zero semantic/OpenAI calls, exact Original, one actor provider call,
   parser success, and no action execution.
2. SHADOW: rubric generate/track plus history-policy evaluation, exact
   Original at the actor SDK, parser success, and no action execution.
3. ACTIVE: the same independent semantic stages, exact admitted final request
   at the actor SDK, parser success, and no action execution.

Each non-OFF smoke case has at most one actor call and exactly bounded rubric
generation, rubric tracking, and history-policy attempts. Unsupported shape,
zero target, uncertainty, invalid response, timeout, cancellation, provider
failure, audit failure, or resource drift must be a typed Original/failure
record with complete negative accounting. Any smoke failure stops the sequence
before R2.5.

Passing CPU tests is not a live-smoke result. Passing live smoke is transport,
parser, isolation, and safety evidence; it is not task-success improvement or
causal evidence.

## 11. Explicit exclusions

This contract does not authorize:

- mutation of frozen G1.5 publications or weakening/relabeling accepted
  R2.1/R2.2 contract semantics;
- action selection or execution during R2.4 smoke;
- storing secrets, or separately requesting/persisting hidden provider
  chain-of-thought outside the contractually retained observable actor output
  and host-native history;
- unbounded SDK retries, arbitrary commands, proxy-routed loopback traffic, or
  continuation after unknown cost/termination/resource ownership;
- active archive execution;
- merge, push, release, Linear mutation, or automatic owner acceptance; or
- any claim that R2.4 or R2.5 has run before its committed receipts exist.

## 12. Repository-candidate handoff and current disposition

The final candidate handoff must record the exact commit, file/hash map,
focused/affected/full CPU results, schema validation, static checks, external
artifacts, and cleanup census. Until the owner reviews that candidate and
separately authorizes a frozen live manifest, R2.4 remains In Progress. The
initial implementation commit is
`344e1c42596a4dca717da66374eeca3d936c3f61`; owner review classified it
**NO-GO**. Its latest remediation candidate is
`4053a90c3ed97ad76cba133d6a2e26089d7f1888` and remains pending owner
re-review. Focused R2.3--R2.5 tests passed 413/413 and the complete MobileWorld
suite passed 2102/2102; independent normal-exact red teams reported GO for the
code boundary. Ruff check/format, targeted mypy over 28 source files, all 17
R2.3--R2.5 schemas, accepted R2.3 byte-equality, and Git diff checks also
passed. `runtime/client.py` retains 16 pre-existing unrelated mypy diagnostics;
the remediation introduced none on changed lines. These CPU checks are not a
live result. The candidate must not be merged, and the six live-smoke cases
remain unauthorized.

R2.3 commit `54381b7b56b06d5aa262005af62b65269b4cf0a6` is accepted through
mainline merge `2aa0a268b7d709cf05d524e74c3fba8612f64003`, so Runtime Epic 2 is
3/6 accepted. A promoted run manifest would neither change that acceptance nor
change Linear; both live authorization and Linear workflow remain owner-managed.
