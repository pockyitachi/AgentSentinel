# R2.5 Frozen MobileWorld Pilot Protocol v1

Status: **CPU PROTOCOL AND ANALYSIS CANDIDATE; PILOT NOT AUTHORIZED OR EXECUTED**

Contract ID: `mobileworld.runtime.r2-5-mobileworld-pilot/contract-v1`

Schemas:

- `schemas/r2_5/cohort_selection.v1.schema.json`
- `schemas/r2_5/executable_task_source.v1.schema.json`
- `schemas/r2_5/frozen_pilot_manifest.v1.schema.json`
- `schemas/r2_5/artifact_bundle.v1.schema.json`
- `schemas/r2_5/pilot_analysis.v1.schema.json`

Decision date: 2026-09-03 UTC

## 1. Decision and dependency

R2.5 is a small, frozen, matched MobileWorld pilot. It may begin only in the
same owner-authorized sequence after both Qwen and MAI R2.4 OFF/SHADOW/ACTIVE
live-smoke matrices pass. A failed, incomplete, expired, or unaccounted smoke
stops before any R2.5 reset or GUI action.

The pilot compares two arms for each selected task and host:

```text
BASELINE       = Sentinel mode OFF
JOINT_SENTINEL = Sentinel mode ACTIVE with independent history-free rubric
```

The term `JOINT_SENTINEL` means both Sentinel semantic axes are active in the
arm. It does not mean the joint-provider topology is used. The production pilot
topology is frozen to `ISOLATED_HISTORY_FREE`: rubric and history-policy model
calls remain independent.

The CPU candidate defines selection, authority, reset/input binding, execution,
audit, and analysis contracts. It contains no pilot outcome. Success rate,
error reduction, or causal-effect claims require actual owner-authorized
execution and committed evidence.

## 2. Source cohort and static-time eligibility

The source is one explicit, strict-JSON, exact-byte-bound GUI-only task list.
Selection is reproducible from its exact bytes and current clean task-definition
registry. Every source row receives one closed disposition and, when
applicable, definition-source and selection hashes. Missing tasks,
AskUser/user-interaction tasks, MCP tasks, and tasks detected as depending on
ambient wall clock/date are excluded.

The v1 time authority is deliberately conservative:

```text
STATIC_WALL_CLOCK_INDEPENDENT_ONLY
```

A reset seed does not freeze `datetime.now()`, backend time sync, or a dynamic
third-party state. Eligibility is an operational static-source screen under
`PYTHON_SOURCE_WALL_CLOCK_SCAN_V1`, not proof of transitive wall-clock
independence or third-party determinism. Tasks with detected or unknown time
dependence therefore do not enter this pilot, and that limitation remains in
live reporting. Reintroducing them requires a new explicit effective-time
authority and backend proof, not a relaxed boolean.

Eligible rows are ranked by the versioned deterministic selection algorithm.
The frozen cohort contains 20--30 unique tasks. The independent cohort-
selection artifact persists the complete source audit and selected prefix;
the task source, pilot manifest, run authority, and artifact bundle bind its
exact path, bytes, and SHA-256. Production resolution reopens and recomputes
these artifacts; a caller-supplied `STATIC` label or naked digest is
insufficient.

## 3. Exact task parameters and reset authority

Each task is bound to canonical MobileWorld initialization parameters:

```json
{"task_name":"<exact registered task>","trial":<exact source-bound positive integer>}
```

The exact canonical bytes determine `task_parameters_sha256`. Parameters may
be inline or stored in a content-addressed external blob. The resolver rejects
the legacy hash-only task-source shape and any path, byte count, hash, task ID,
trial, or schema drift.

Each resolved task also carries one deterministic non-negative int32-range
reset seed, shared across both hosts and both arms. The resolver and driver bind
the task name, trial, parameter hash, seed, seed policy, static-time authority,
and cohort-selection hash. The backend initialization request consumes and
validates the exact task name, trial, parameter hash, and reset seed; the seed
policy, static-time authority, and cohort hash remain run/lease/evidence
bindings. Merely recording the backend-consumed values without passing them to
the backend is a failure.

## 4. Frozen matched matrix

For `N` frozen tasks, the exact matrix contains `4N` cells, where
`20 <= N <= 30` and therefore `80 <= cells <= 120`:

```text
for task in frozen order:
  for host in (QWEN3_VL, MAI_UI):
    for arm in (BASELINE, JOINT_SENTINEL):
      run one isolated cell
```

Every cell binds task ID, task-parameter hash, reset seed, host, arm, Sentinel
mode, sequence index, resource identity, and run authority. No cell may be
silently skipped, retried as a replacement observation, resumed from an old
result, or reordered.

Matching is exact at the input level. The reset receipt separately binds an
observable initial-state commitment consisting of screenshot pixels, task-goal
hash, task/trial/parameter hash, and seed. It does not prove equality of all
hidden Android, application, backend, or third-party state. All four cells for
a task must meet the frozen observable matching rule before comparative
analysis; otherwise that matched group is invalid rather than assumed
equivalent, and the hidden-state limitation remains explicit.

## 5. Environment and action lifecycle

Each cell uses one explicitly owned MobileWorld backend/device and one selected
actor model service. Production execution is single-cell, single-backend, and
non-streaming with no hidden runner concurrency or automatic experimental-case
retry. A bounded, serialized in-request device recovery may reinitialize an
unhealthy emulator; it is recorded as infrastructure recovery and does not
create a replacement observation.

The cell lifecycle is:

```text
re-attest model/backend ownership and remaining authority
  -> task teardown and reinitialization boundary on the owned backend
  -> initialize exact task/trial/seed
  -> bind initial state
  -> repeated actor decisions up to the cell bound
  -> parse and validate one permitted action
  -> execute only in R2.5
  -> record transition and post-state
  -> obtain exact official score/reason
  -> teardown and prove cleanup
```

Every backend request uses a proxy-disabled loopback session and a timeout no
longer than the remaining case/owner deadline. HTTP status, response schema,
echoed action, finite official score, task binding, and teardown success are
validated rather than coerced. Failed actions become failed transitions; they
are never recorded as successful execution. Only the closed pilot action
vocabulary is executable.

Resource PID/container/model identity and health are re-attested before actor,
reset, task, goal, action, score, and teardown dispatch. Failure, expiry,
ownership drift, unknown cleanup, or unknown provider cost stops the sequence.

## 6. Sentinel semantics in the two arms

BASELINE performs zero rubric and history-policy OpenAI calls and sends the
host's exact Original request to the actor provider.

JOINT_SENTINEL uses the R2.4 runtime overlay and same-cutoff Collector bundle.
For one task run, rubric generation occurs once. The expected first decision
has no actor history: it performs rubric generation plus one history-free track,
performs no history-policy call, and sends exact Original. Each later
history-bearing logical actor decision performs one rubric track and at most one
history-policy operation. Provider or parse retries reuse the same Sentinel
result and do not advance rubric state.

The R2.3 path component observes task progress without reading actor history.
The R2.2 component edits only admitted history spans. There is no actor-action
authority and no active history archive in v1.

## 7. Bounds and fail-stop behavior

The frozen pilot declares exact per-cell and sequence maxima for:

- steps and logical actor decisions, with physical actor attempts recorded
  independently in runtime audit;
- independent rubric and history-policy OpenAI calls;
- monotonic wall time;
- per-attempt output-token limits, observed input/cached-input/output token
  census, and owner-pinned cost caps; and
- GUI actions, which cannot exceed admitted actor decisions.

One stage-owned atomic ledger reserves worst-case OpenAI cost before every
dispatch and settles it from terminal usage. A per-cell view cannot reuse the
entire pilot budget. Owner authorization expiry is converted into a monotonic
sequence deadline and rechecked at every external dispatch.

A timeout, provider error, parser error, invalid Sentinel output, action
failure, score failure, audit failure, cleanup failure, or evidence-publication
failure is a typed failed cell/stage. The sequence does not omit the cell and
continue as if it were missing at random. Evidence already produced by a
failed unit is retained in an owner-only recovery journal.

## 8. Durable evidence

The pilot stage persists one canonical cell/census projection containing:

- manifest and cohort/task/reset bindings;
- every cell identity, arm, host, initial-state commitment hash, and cleanup
  commitment hash;
- every logical decision and raw/final request hash;
- rubric/history-policy attempt roots, dispatch/token/cost census, fallback,
  edit, abstention, unsupported, error, and archive state;
- per-decision production-audit-detail hashes, parsed/executed action hashes,
  and termination status;
- official score/success and a reason hash with its validated backend binding;
  and
- per-cell and stage latency and resource/audit roots.

Full request, attempt, provider locator, edit/fallback, reset, cleanup, and
transition evidence lives in hash-bound external production-audit details and
Collector artifacts. The stage receipt binds the evidence SHA-256; the
canonical wrapper/file reader validates byte size rather than storing it in
that receipt. An opaque hash without its canonical preimage is not a valid
pilot result. If a failure occurs after one or more external calls, the current
unit journal must bind those attempts, costs, actions, and terminal phase before
control returns. If final publication fails, previously fsynced evidence
remains recoverable under an owner-only partial marker and is never labeled
successful.

Raw Collector streams remain label-free and immutable. Derived semantic and
experimental results live only in the access-controlled sidecar/output root.
Secrets and hidden provider chain-of-thought are excluded, but observable actor
output remains losslessly captured; model-emitted thought-like text may
therefore appear as ordinary output or later host-native history.

## 9. Analysis contract

`analyze_pilot_stage_v1` consumes the frozen pilot, its stage evidence, and the
provided hash-keyed production audit details; omitted details are reported in
explicit missing denominators. The external `analyze_pilot_artifacts_v1`
reader and CLI are stricter: they require every referenced owner-only mode-0600
detail and reject missing or mismatched bytes before publishing analysis.

The analysis provides, per host and overall:

- matched BASELINE versus JOINT_SENTINEL task success and score summaries;
- steps, actor calls, OpenAI calls, tokens, cost, and wall time;
- termination and provider/parser/action failure census;
- history edit, abstention, fallback, invalid, unsupported, and error rates;
- archive census; and
- only those repeated-action or premature-stop lower bounds directly supported
  by persisted evidence.

Metrics that require an independent semantic label remain
`NOT_MEASURABLE`, including unnecessary/wrong actions, unnecessary/wrong
history edits, and clean-history false-edit rate when no clean-history gold is
present. Missing labels are never converted into zeros.

The analysis CLI reads a canonical owner-only stage wrapper, re-hashes all
referenced details, and writes one fresh repo-external mode-0600 canonical
artifact. It does not mutate the pilot evidence.

## 10. Interpretation

This is a bounded paired engineering pilot, not a formal causal study. A
difference between arms may be reported descriptively with its exact matched
denominator and missing/failure census. It does not by itself establish a
general causal effect, cross-model ranking, production readiness, or expansion
to all GUI-117 tasks.

R2.6 expansion is conditional on owner review of the complete R2.4/R2.5
evidence. Dynamic-time, AskUser, MCP, missing-definition, and otherwise
excluded tasks do not silently re-enter that expansion.

## 11. Explicit exclusions

This protocol does not authorize:

- execution before both R2.4 host smokes pass;
- smoke GUI actions or pilot actions outside the exact frozen cells;
- hidden retries, parallel cells, skipped failures, result reuse, or budget
  overrun;
- treating task seed as a wall-clock freeze;
- active archive, action advice by Sentinel, or Collector label mutation;
- publishing secrets, hidden provider chain-of-thought, raw private artifacts,
  or output inside Git; or
- merge, push, release, Linear mutation, automatic acceptance, or R2.6.

## 12. Repository-candidate handoff

The CPU candidate handoff must state exact files and hashes, test/static/schema
results, source/cohort census, external artifact census, cleanup, and all work
not performed. The live handoff, if separately authorized and successful, must
add the promoted authority hash, preflight/runtime/pricing hashes, resource
receipts, smoke evidence, full pilot evidence, analysis artifact, cost/token
census, cleanup/recovery state, and exact stop condition.

Until those live artifacts exist and the owner reviews them, R2.5 remains
pending and must not be described as run, successful, Done, or accepted.
