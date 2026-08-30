# G1 GPU Live Smoke Contract v1 — Amendment 2

Status: **LOCKED narrow operational correction for ALE-322 / D-034**  
Document type: Normative Git-controller and evidence-epoch amendment  
Amendment ID: `mobileworld.g1.gpu-live-smoke/contract-v1-amendment-2`  
Amends: `G1_GPU_LIVE_SMOKE_CONTRACT_V1.md` and
`G1_GPU_LIVE_SMOKE_CONTRACT_V1_AMENDMENT_1.md`  
Authorization: `G1_4_DECISION_LOG.md` D-034 Amendment 2 dated 2026-08-30 UTC  
Decision date: 2026-08-30 UTC

## 1. Purpose, failure fact, and precedence

The authority-v3 attempt authorized by Amendment 1 failed closed before any
GPU probe, model load, service launch, request, provider invocation, replay, or
MobileWorld action. It completed zero of the exact twenty-two calls. Host Git
2.34 interpreted the literal configuration value `core.fsmonitor=false` as an
executable fsmonitor hook path and spawned `/usr/bin/false`. The owned-command
controller correctly rejected that descendant. This is a launch-controller
compatibility failure, not a GPU, model, provider, or response result.

The owner authorizes one new, narrower generation that replaces only the Git
fsmonitor-disable spelling and advances every mutable launch/evidence path to a
fresh epoch. The base contract and Amendment 1 remain byte-frozen historical
text. This Amendment 2 controls only where it explicitly conflicts with them.
It does not reinterpret the authority-v3 failure or any v1, v2, or v3 artifact.

## 2. Version and path topology

The only admissible new generation is:

| Subject | Failed generation | New generation |
| --- | --- | --- |
| authority | `mobileworld.g1.gpu-live-smoke-authority/v3` | `mobileworld.g1.gpu-live-smoke-authority/v4` |
| launch shim binding | `mobileworld.g1.gpu-live-smoke-launch-shim/v2` | `mobileworld.g1.gpu-live-smoke-launch-shim/v3` |
| launch token prefix | `D034_STAGE0_V2` | `D034_STAGE0_V3` |
| preparation receipt | `mobileworld.g1.gpu-live-smoke-preparation/v2` | unchanged v2 |
| Stage1 pre-exec receipt | `mobileworld.g1.gpu-live-smoke-stage1-preexec/v2` | unchanged v2 |
| pre-import runtime census | `mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v2` | unchanged v2 |
| network-namespace receipt | `mobileworld.g1.gpu-live-smoke-network-namespace/v2` | unchanged v2 |
| stored execution receipt | `mobileworld.g1.gpu-live-smoke-execution/v2` | unchanged v2 |

The new operational paths are exactly:

```text
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/launch-shim.v3
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/authority.v4.json
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/evidence-v4
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/runtime-scratch-v4
```

The authority schema permits owner-private absolute CPU-test roots with the
exact `evidence-v4` and `runtime-scratch-v4` suffixes. The production loader and
launch entrypoint require the complete shared paths above. Any CPU test that
may access or create evidence or scratch MUST patch the production constants to
temporary roots; a read-only load or preparation fixture may carry the formal
path strings without touching them. No CPU test may write a formal shared root.

Authority v1 through v3, launch shims v1/v2, tokens V1/V2, and evidence or
runtime-scratch generations v1 through v3 are obsolete for execution. In
particular, fixed authority v3, launch-shim v2, its failed run and content
objects under evidence-v3, and runtime-scratch-v3 remain immutable historical
evidence. They MUST NOT be retried, reused, renamed, truncated, overwritten,
deleted, moved into the v4 closure, or treated as successful evidence.

## 3. Exact no-spawn Git controller

Every production Git command MUST use the following adjacent configuration
arguments, with an empty value after the equals sign:

```text
-c core.fsmonitor= -c core.hooksPath=/dev/null
```

The literal `core.fsmonitor=false` is forbidden in the production runner and
CLI. Substituting `false`, `/usr/bin/false`, another executable, a boolean-like
word, or omitting the empty command-line override is not equivalent. The closed
Git environment, exact `/usr/bin/git` file binding, repository binding,
timeouts, output caps, pidfd acquisition, descendant-forbidden policy, and
clean-tree requirements remain unchanged.

The five Git call sites are the host inspection `rev-parse`, the production
source-closure `rev-parse` and `status --porcelain=v1 --untracked-files=all`,
and the Stage2 CLI source-closure copies of those two calls. Each resulting
owned-command receipt MUST report `observed_descendant_count=0`, an empty
descendant-identity array, and zero numeric/Popen signal use. Any descendant,
nonzero return code, unexpected output, dirty or untracked status, timeout,
binding drift, or receipt drift remains terminal and fail closed.

Conformance MUST run actual host Git through the production owned-command
controller against a clean local CPU fixture whose lower-priority repository
configuration explicitly sets `core.fsmonitor=/usr/bin/false`. The exact empty
command-line override MUST suppress that executable value: both `rev-parse` and
`status` succeed, status is empty, and no descendant is observed. A mock-only
argument assertion is insufficient for this compatibility boundary.

## 4. Fresh construction and mixed-epoch rejection

The v4 attempt requires a new clean detached source at the reviewed commit, new
owner-sealed source modes with no bytecode cache, a deterministically rebuilt
and `NO_REPLACE`-installed launch-shim v3, a newly issued canonical authority
v4, and repeated deterministic preparation from the fixed authority path.
Source, runner module, runner CLI, shim source and ELF, bootstrap, launch gate,
packet, manifest, runtimes, models, tools, and tool-shell bindings MUST be
reinspected and bound by exact hashes before any execution authorization is
considered live.

The reviewed v4 production byte closure is exact:

- runner module SHA-256
  `d46f4bb6f394d3a439d4af2ddc64b912f6cb27721bc45288843a2ed688634c73`
  over 522,295 bytes;
- runner CLI SHA-256
  `87deaaea31323888a2e6a51d0fe5991de210a331b3f9e81c3cf453f69585091c`
  over 152,402 bytes;
- launch-shim C source SHA-256
  `d4cb428617708458c5c1a4cbd8be6cf8ea710ac3f69bab837863ee101b5f40d7`
  over 55,526 bytes;
- deterministic launch-shim v3 ELF SHA-256
  `652118e76279a766e30927951ff432a1729ca9c81a34f172e8f10cfc2bc1e928`
  over 26,272 bytes;
- outer bootstrap SHA-256
  `70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b`
  over 4,645 bytes; and
- owned-command gate SHA-256
  `70c01194e4ed6ad7cf54a5ffb0caa72bb9d8fa1694544665d53707b90279b061`
  over 228 bytes.

Any later production byte change invalidates this closure and requires a new
reviewed freeze before v4 authority generation or execution.

Authority schema, manual loader, preparation, static shim, and entrypoint MUST
reject every obsolete or mixed tuple, including authority v3 with a v4 path,
authority v4 with shim v2, token V2, evidence-v3, runtime-scratch-v3, or a tool
shell command prefix naming an old path or token. No field may be inferred or
mechanically upgraded from the failed authority.

## 5. Failure evidence remains authoritative

The v3 terminal FAIL and its append-only content-addressed closure remain the
authoritative record of that attempt. A v4 preparation or execution MUST use
fresh roots and a fresh run identifier; it cannot resume, repair, relabel, or
complete the v3 run. The zero-of-twenty-two call fact MUST remain visible in
status reporting. A later v4 PASS would be a distinct attempt and would not
erase the v3 failure.

Before v4 launch, the fixed shim and authority targets must be absent, then
installed with no-follow, no-replace semantics and reopened for exact
owner/mode/link-count/hash validation. Evidence-v4 and runtime-scratch-v4 must
be fresh. Any collision or preexisting content is terminal; no cleanup or
overwrite is authorized.

## 6. Unchanged safety and truth boundaries

All Amendment 1 supplementary-group rules remain exact: host identity
`1035:1035`, primary-first groups `[1035,109,999]`, sorted
`os.getgroups()` `[109,999,1035]`, inner sorted groups
`[0,65534,65534]`, `setgroups=deny`, `setpriv --keep-groups`, five zero
capability sets and `NoNewPrivs=1` at Stage2. The retained KVM and Docker
AF_UNIX capability residual remains disclosed but is not permission to open or
invoke either facility. Point-in-time own-FD and AF_UNIX censuses must remain
measured and zero at both stage boundaries.

The GPU remains shared physical GPU 0 with the exact authorized UUID and 64 GiB
free-memory floor. No foreign process may be stopped, signaled, modified, or
inspected beyond the already authorized UID/start-time reads. All external
INET/INET6, real provider, credential, replay, backend restore, generated
action, MobileWorld action, Docker/KVM action, response feedback, formal
publication, and scope expansion prohibitions remain unchanged. The exact
Qwen-then-MAI lifecycle, twenty-two-call matrix, zero retry, non-stream, and
inert-response rules remain unchanged.

## 7. Conformance and authorization boundary

Before any v4 live attempt, conformance MUST prove that:

1. all current authority and preparation schemas validate the v4 positive
   fixture and reject v3 or mixed epochs;
2. the literal `core.fsmonitor=false` is absent and all five Git argument sites
   use the exact empty override plus disabled hooks path;
3. the actual-Git lower-priority executable fsmonitor regression passes with
   zero descendants and clean output;
4. current schema, loader, preparation, static shim, and entrypoint reject old
   authority, shim, token, evidence, or scratch generations;
5. the complete CPU suite, schema/meta-schema validation, formatter, linter,
   compile checks, deterministic shim build, and diff checks pass at the frozen
   source bytes; and
6. an independent review approves the fresh sealed source, fixed installed
   shim, authority draft, repeated preparation, and fixed-path readback before
   live execution.

This amendment authorizes only the stated narrow correction and fresh v4
construction. It does not itself assert that the artifact or live-entry gates
have passed, authorize a GPU launch before those gates, expand D-034, or mark
ALE-322/ALE-323 complete. Any further code, contract, path, epoch, input,
resource, process, network, model, call, or evidence change requires a new
owner decision and reviewed versioned amendment.
