# G1 GPU0 Live-Smoke Contract v1

Contract ID: `mobileworld.g1.gpu-live-smoke/contract-v1`
Decision authority: `D-034`
Owner authorization date: `2026-08-30`
Status: **AUTHORIZED_NON_FORMAL_SECRET_FREE_LOOPBACK_SMOKE_ONLY**

## 1. Purpose and non-formal boundary

This contract is the sole narrow live/GPU exception for the deferred engineering
smoke checks of ALE-322 / G1.4 and ALE-323 / G1.5. It authorizes exactly one
secret-free, synthetic, non-case batch on the owner-selected shared physical
GPU 0. It does not authorize a formal capsule, a natural task, a treatment
response, a replay, a backend restore, or a MobileWorld/generated action.

The exception is additive and does not modify any formal G1 record or gate. In
particular, all formal G1.3 v1.1 capsules remain read-only and retain exactly:

- `execution_ready=false`;
- `provider_invocation_allowed=false`;
- `treatment_response_generation_allowed=false`.

The G1.4 formal `OpenAICompatibleProviderCodec.send` and
`execute_live_arm` paths remain fail-only. A D-034 implementation must use a
separate smoke-only entrypoint that rejects formal capsule/request identifiers.
Passing this batch can satisfy only the explicitly deferred engineering
live-smoke evidence item. It cannot by itself create formal replay, admission,
publication, G1.6 gold, G1.7, or runtime Sentinel authority.

D-034 supersedes D-027's downstream-seal prerequisite only for this synthetic
non-case engineering smoke. G1.6/G1.7 gold, admission, scorer, restorer,
run-ready, and execution seals are neither consumed nor synthesized here; they
remain mandatory for any future formal/case replay and remain false or absent.

## 2. Frozen authority and environment

The batch must be bound before any model is loaded to a schema-valid authority
artifact and the schema-valid, content-addressed smoke packet. The authority is
frozen as follows:

- `schema_version=mobileworld.g1.gpu-live-smoke-authority/v2`;
- `decision_id=D-034`;
- `authorized_scope=SYNTHETIC_NON_CASE_GPU_LIVE_SMOKE_22_CALLS`;
- `authorized=true`, with exact owner UID, UTC issue/expiry interval, and an
  out-of-band SHA-256 over canonical bytes.

| Field | Frozen value |
| --- | --- |
| physical GPU ordinal | `0` |
| GPU UUID | `GPU-991ac45f-e9e9-1c25-590c-fb49ca752965` |
| shared GPU | `true` |
| exclusive lease claimed | `false` |
| minimum free memory | exactly `68719476736` bytes (64 GiB), rechecked before each model start |
| public API bind | `127.0.0.1:18007` only |
| request endpoint | `http://127.0.0.1:18007/v1/chat/completions` |
| client environment | MobileWorld virtual environment; `openai==1.106.1` |
| server environment | SkyRL virtual environment; `openai==2.15.0`; `vllm==0.11.0`; `torch==2.8.0+cu126` |
| visible device | `CUDA_VISIBLE_DEVICES=GPU-991ac45f-e9e9-1c25-590c-fb49ca752965`; server-local `cuda:0` only after UUID verification |
| service order | `qwen3vl_8b`, full release, then `mai_ui_8b` |
| SDK retries | `max_retries=0` |
| streaming | `false` |
| logical calls | exactly `22` |
| physical HTTP requests | at most one per logical call; exactly `22` for a PASS batch |

The client and server environments are deliberately different. The OpenAI SDK
version is a client fact; vLLM and torch versions are server facts. Evidence
must bind each side's lexical Python path to the same exact resolved,
executable, regular ELF path and SHA-256, record the resolved executable and
environment identity, and fail closed if any path, hash, or version differs.
The vLLM process identity must bind the same resolved server executable.

Authority v2 also binds the only admissible tool shell and outer launch shim.
The tool shell is lexical `/bin/sh`, with its complete symlink-component chain
bound to resolved `/usr/bin/dash`. The authority closes the resolved dash ELF,
its `PT_INTERP`, sole `DT_NEEDED` dependency, absence of RPATH/RUNPATH, selected
loader and libc files, `/etc/ld.so.cache`, and absence of
`/etc/ld.so.preload`. Every lexical symlink component and resolved regular
binary carries its exact path/target, hash/bytes where applicable, ownership,
mode, and link count. The selected libc resolution must be unique.
The three dynamic ELF identities are explicit: dash has `EI_OSABI=0`, while
the selected loader and libc both have `EI_OSABI=3`. The authority also binds
the exact ordered `/` and `/etc` component chain used before reading
`ld.so.cache` or proving `ld.so.preload` absent. Removing, adding, reordering,
substituting, or changing the owner/mode/link metadata of any component in the
shell, interpreter, system-configuration, or ambient-loader path census is
invalid.

The inherited environment may contain exactly
`LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64` in the loader-sensitive namespace.
That lexical path, its symlink target and every lexical/resolved ancestor are
authority-bound as root-owned directories without group/world write. A
recursive census must prove that tree contains no `libc.so*`, `ld-linux*`, or
other loader candidate. `LD_PRELOAD`, `LD_AUDIT`, `LD_PROFILE`,
`LD_DEBUG_OUTPUT`, every other `LD_*`, `GLIBC_TUNABLES`, `GCONV_PATH`, `ENV`,
`BASH_ENV`, `SHELLOPTS`, every `BASH_FUNC_*`, and `IFS` must be absent.

The launch shim itself is the
owner-private, single-linked, mode-`0500`, static x86-64 ELF executable at
`/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/launch-shim.v1`;
its lexical and resolved paths must be identical. The authority records its
exact SHA-256, byte count, owner UID/GID `1035:1035`, source path and source
SHA-256, plus every protocol input needed for the transition: the runner CLI,
smoke packet and model-config-manifest paths, outer bootstrap SHA-256 and byte
count, execution confirmation, shell option `-c`, and token prefix
`D034_STAGE0_V1`. Authority v1 artifacts do not contain this closure and are
obsolete; they must never be accepted for execution.

The shim is not Bash, a shebang script, a dynamic executable, or a general
command interpreter. Its ELF closure is exact: `EM_X86_64`, `ET_EXEC`, no
`PT_INTERP`, no `PT_DYNAMIC`, no `DT_NEEDED`, no RPATH/RUNPATH, no init/fini
arrays, no TLS segment, no writable-executable segment, and a non-executable
stack. The execution tool uses `/bin/sh` with `login=false`, `tty=false`, and
one literal command containing only the shell builtin `exec`, the absolute shim
path, `-c`, and one colon-delimited safe token. The command is exactly
`exec <absolute-shim> -c D034_STAGE0_V1:<absolute-authority-path>:<lowercase-64-hex-authority-sha256>`;
it contains no quoting, expansion, redirection, pipe, control operator,
whitespace-bearing argument, or lookup through `PATH`. Whitespace drift, an
extra field, an escape, a duplicate authority argument, a noncanonical path, or
any other shell syntax is invalid. The authority binds only the command prefix,
template hash/bytes, exact grammar, and 64-hex suffix length; binding the full
command digest inside the authority would create an authority-hash cycle. The
tool call must explicitly pass `shell=/bin/sh` and `login=false`; omitting the
shell parameter (which selects an ambient default shell) or passing any other
shell is invalid. The honored shell selection is a recorded fact, not an
inference from the command text.
full command SHA-256/byte count are instead recorded in preparation/runtime
receipts without storing the raw command. The authority SHA is passed out of
band and is never embedded into the shim bytes. The only accepted authority
path is
`/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/authority.v2.json`.
The raw command, token and complete argv are never persisted because the
evidence secret scanner deliberately treats token-bearing field names as
forbidden. Instead, the preparation/runtime receipts freeze the complete
command SHA-256 and byte count, authority SHA-256, exact command grammar and
safe prefix, `/bin/sh -c`, `login=false`, `tty=false`, and the shim invocation
hash/length facts. The namespace receipt nests the verified transition under
`pre_gate_launch_shim`, and the Stage1 receipt adds a no-follow, same-inode
`launch_shim_revalidation` before the private-runtime census. All state
`pre_gate_invocation_attestation_formally_proven=false`.
Those receipts do not misstate the fallback as a direct static-shell call:
they state `tool_shell_parameter_honored=true`,
`direct_exec_formally_proven=false`, `pre_gate_tool_shell_used=true`,
`tool_shell_inherits_ambient_environment=true`,
`ambient_loader_environment_policy_bound=true`, and
`launch_shim_clears_environment=true`. They also state
`pre_gate_dynamic_loader_closure_formally_proven=false`. Raw command and token
bytes remain absent from evidence even though their post-authority digests and
byte counts are exact.

Before opening any GPU, network, model, provider, service, or project runtime,
the bound dynamic dash/loader/libc chain performs only the literal builtin
`exec` transition. The shim then rejects ambient arguments, environment, and
grammar drift, clears the inherited
environment, closes every inherited descriptor numbered 3 or above, validates
descriptors 0/1/2 are non-sockets, and validates its own bytes plus the sealed
source (`0400`), runner CLI (`0400`), smoke packet (`0600`), model manifest
(`0400`), and root-owned Python executable against the authority. Each host
input is UID/GID `1035:1035` and singly linked except the Python ELF, which is
root-owned, executable, resolved, and singly linked. The shim enters that exact
Python executable through its already validated no-follow file descriptor with
only `LC_CTYPE=C.UTF-8`. Any mismatch fails
with zero live side effects. Self-hashing cannot prove that a caller did not
replace the shim before the kernel executed it; owner-only sealed parent/file
metadata and exact post-entry validation narrow that residual but do not remove
it. Nor can post-entry validation prove that the dynamic loader and dash bytes
actually used before shim entry matched their inspected bindings. Therefore
`direct_exec_formally_proven=false` and
`pre_gate_dynamic_loader_closure_formally_proven=false`. This remains a
disclosed non-formal boundary and does not make
`toctou_free_runtime_binding_proven` or formal runtime immutability true.

The CPU-only shim builder compiles the sealed source with the authority-bound
static freestanding flags, records the compiler path/resolved path/hash/bytes,
exact argv/environment/return code and bounded compiler-log hash/bytes, installs
the new destination without replacement, reopens it, and runs the full ELF
parser. Its receipt states `static_elf_dependency_closure_proven=true`, while
`source_to_binary_reproducibility_formally_proven=false`; successful static ELF
inspection does not claim reproducible compilation. A failed build preserves
its exact owner-only staging directory and never recursively deletes it.

The launcher and workload runtimes are deliberately different too. The first,
host-side stage is the root-owned `/usr/bin/python3.10` interpreter and its
authority-bound stdlib tree; its resolved path, executable hash and byte count,
version, flags, tree hash/count/bytes, ownership, modes, and link policy are
closed authority facts. It may do only stdlib authority parsing, inherited-FD
closure, and the first namespace exec. Immediately after `unshare`, a second
system stage must still execute the same bound root Python 3.10, not private
code. Before any private-runtime exec it rehashes the private Python ELF and
complete private stdlib tree (including `encodings`), prepares loopback and
scratch, and then uses `setpriv` plus a fresh `env -i` to exec the third stage.
That third workload stage uses one owner-private, sealed Python 3.12 tree inside
the namespace. Its root, interpreter and stdlib,
complete tree hash/count/bytes, ownership, modes, and no-link policy are also
closed authority facts and are revalidated before use and after execution.
The client and server site-packages are separate owner-private, sealed reflink
copies beneath that private runtime, not the mutable source environments. Each
copy has its own exact complete-tree hash/count/bytes and owner/mode/no-link
policy, with matching pre/post censuses. The private runtime, both site-package
trees, repository, evidence root, runtime scratch, and both model snapshots
must be pairwise disjoint in both ancestor directions.

The private-runtime builder has one narrow source-only hardlink exception:
regular files in the Conda Python 3.12 **stdlib source tree** may have
`st_nlink >= 1`. Source Python ELF and client/server site-package hardlinks
remain forbidden. For every copied stdlib regular file, the builder binds the
caller-observed no-follow identity to the opened source FD, hashes and rechecks
that same FD and its full identity (including link count, mtime, and ctime)
before and after `FICLONE`, reopens and hashes the destination, and requires
equal content with distinct source/destination device-inode identity. It also
records the canonical per-path source-link census, hardlinked-entry count, and
maximum link count. This exception is copy-only: every private destination
regular file must still be owner-only, sealed to its exact mode, independently
reopened, and `st_nlink=1`; no source hardlink is installed or accepted in the
private runtime.

Those private copies materially narrow ambient-runtime drift but do not turn
D-034 into a formal execution seal. Namespace root is the same mapped owner
and can in principle change owner-read-only modes between the two censuses;
pre/post equality detects observed mutation but is not a TOCTOU-free proof.
Native ELF `DT_NEEDED` dependency closure is also not proven by the Python-tree
census. Every terminal receipt therefore states
`formal_runtime_immutability_proven=false`,
`toctou_free_runtime_binding_proven=false`, and
`native_dt_needed_dependency_closure_proven=false`. These disclosed residuals
are admissible only for this non-formal smoke and cannot complete ALE-322 or
ALE-323 formally.

The authority also binds the exact clean Git commit, the resolved worktree and
`MobileWorld/src` roots, `/usr/bin/git` and its hash, the runner-module and CLI
hashes, and the nine critical source files named by the authority schema,
including the freestanding launch-shim source. The
live runner must resolve every critical import to that one source root, verify
the worktree is byte-clean including untracked files, and rehash the files from
no-follow regular-file descriptors. Editable-install or ambient import drift is
not admissible. Both the `rev-parse HEAD` and
`status --porcelain=v1 --untracked-files=all` source-controller calls use
`core.fsmonitor=false`, `core.hooksPath=/dev/null`, stdin from `/dev/null`, and
the exact closed Git environment recorded by this contract; user/system Git
configuration, hooks, credential prompts, pagers, and fsmonitor execution are
not admissible.

The three-stage production freeze binds runner-module SHA-256
`634d62a048d9cd0621419602b39ee92cc6b70e1d87fac72b954dbc6de8e2f824`
over exactly 511,123 bytes,
CLI SHA-256
`1c61b4496a857e1942fb314450a724f14b7d68263295455b7e13922a4671e823`
over exactly 140,286 bytes,
and outer stdlib-bootstrap SHA-256
`70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b`
over exactly 4,645 UTF-8 bytes. The auxiliary-command launch gate is separately
bound to SHA-256
`70c01194e4ed6ad7cf54a5ffb0caa72bb9d8fa1694544665d53707b90279b061`
over exactly 228 UTF-8 bytes. Any movement requires a fresh authority,
schema/test parity pass, and contract update before execution.
The launch-shim C source is frozen at SHA-256
`7e7f2ab5356a50a00c2a28beacfb8fda96402c51b1daa5fb245dff187f35c390`
over exactly 55,526 bytes. The deterministic production build from the bound
compiler/flags has SHA-256
`f0b645daa52279e46391a04444d617eb619f23b91f797d0710cb579fc5753f4c`
over exactly 26,272 bytes; authority generation must independently inspect and
bind those installed bytes rather than trusting this prose value.

Execution uses exact Python flags `-I -S -B -X pycache_prefix=/dev/null` for
both client and server. The initial outer execute stage is stdlib-only: before
the first `unshare` exec it must not bootstrap site-packages or import
`mobile_world`, `loguru`, `PIL`, or `openai`. It first closes and revalidates
every inherited descriptor numbered 3 or above, proves descriptors 0/1/2 are
non-sockets, then performs the first exec using an exact minimal environment
with no inherited `LD_*`, Python, cache, credential, or ambient application
variables. The system stage must fail with zero GPU/model/server/client/request
side effects if any private interpreter, stdlib, or `encodings` byte differs.
Only after that system-stage census may the private `env -i` stage insert the
authority-bound source and private site-packages paths. The private CLI hashes
both site-package trees at `PRE_IMPORT` before importing them. The `site` module
and `.pth` execution remain disabled in every stage.

Every execute is mechanically isolated by authority-bound
`/usr/bin/env -i`, `unshare --user --map-root-user --net`, `/usr/bin/ip`, and
`/usr/bin/setpriv`. Every GPU identity, capacity, and process probe additionally
rehashes the authority-bound resolved regular `/usr/bin/nvidia-smi` executable
and verifies its exact SHA-256 and byte count immediately before invocation.
Any executable-path, hash, byte-count, or no-follow identity drift fails before
starting a probe, model process, client, or request. The namespace must have
exact UID/GID maps from host owner 1035 to inner owner 0. Host-root system files
are deliberately outside that one-ID map: they are bound as UID/GID `0:0` in
Stage0 and must appear as the kernel overflow identity `65534:65534` in Stage1.
Tree aggregates normalize those two observations to one `SYSTEM_ROOT` owner
role; any other mapping or raw owner fails before private exec. The namespace
has exact interface census `['lo']` from `/proc/net/dev`, no
usable IPv4/IPv6 default or non-loopback route, a working loopback self-connect,
zero capabilities, and `no_new_privs`. The launcher environment is an exact
secret-free allowlist and external networking must be mechanically unavailable.
The namespace receipt binds the actual outer-FD-closure receipt and the inner
0/1/2-only FD census.
Stage1 additionally revalidates the complete tool-shell authority subject in
the overflow-identity view, including exact shell/interpreter/system/ambient
component counts, all three ELF OSABI values, loader-tree census, cache
selection and preload absence. Its receipt records raw system owner
`65534:65534`, the normalized `SYSTEM_ROOT` role, and exact equality with the
host-root authority subject. The runtime namespace receipt carries the same
closed `pre_gate_tool_shell` proof; neither receipt upgrades the disclosed
dynamic-loader residual to a formal claim.

No credential is required or permitted. A syntactically required loopback-only
SDK token must be a fixed public sentinel such as `EMPTY`, must not be read from
an environment variable or secret store, and must be excluded from request and
evidence artifacts. DNS and non-loopback network access are forbidden.

## 3. Frozen models and cache-integrity qualification

The two allowed bindings are:

| `model_id` | repository | revision | served name |
| --- | --- | --- | --- |
| `qwen3vl_8b` | `Qwen/Qwen3-VL-8B-Instruct` | `0c351dd01ed87e9c1b53cbc748cba10e6187ff3b` | `Qwen3-VL-8B-Instruct` |
| `mai_ui_8b` | `Tongyi-MAI/MAI-UI-8B` | `e00a0097abb9cc621cac5172d8c4809f0839c94e` | `MAI-UI-8B` |

Local cached weights may be used even though the current cache is writable,
but this is a **non-formal qualification only**. Before each model launch and
again after its guarded shutdown, the runner must enumerate and hash the entire
resolved snapshot tree. The inventory must include every relative entry,
entry type, resolved target for links, byte count, and SHA-256 for every regular
file. Pre/post aggregate and per-file hashes must be identical. A mismatch or
incomplete inventory invalidates the batch.

This pre/post comparison detects observed mutation; it is not a TOCTOU-free
proof of immutable model bytes. Evidence and status text must state
`formal_model_immutability_proven=false` and
`toctou_free_model_binding_proven=false`. No model file may be downloaded,
repaired, replaced, or written by the batch; offline/cache-only mode is
mandatory.

## 4. Exact 22-call plan

The only allowed input is a versioned, secret-free, synthetic non-case fixture.
Its exact bytes and each rendered application request must be hashed into the
packet before the first model starts. `synthetic_non_case=true`,
`formal_capsule=false`, and `contains_real_task_data=false` are required.

Calls are strictly ordered. Qwen owns ordinals 1-11. Only after Qwen has exited,
port 18007 is free, and its GPU allocation is absent may MAI own ordinals 12-22.

| Ordinal | Call ID | Phase | Model | Seed | Repeat / arm |
| ---: | --- | --- | --- | ---: | --- |
| 1 | `g14-qwen-s1729-r1` | `G1_4_CANARY` | Qwen | 1729 | repeat 1 |
| 2 | `g14-qwen-s1729-r2` | `G1_4_CANARY` | Qwen | 1729 | repeat 2 |
| 3 | `g14-qwen-s2718-r1` | `G1_4_CANARY` | Qwen | 2718 | repeat 1 |
| 4 | `g14-qwen-s2718-r2` | `G1_4_CANARY` | Qwen | 2718 | repeat 2 |
| 5 | `g14-qwen-s31415-r1` | `G1_4_CANARY` | Qwen | 31415 | repeat 1 |
| 6 | `g14-qwen-s31415-r2` | `G1_4_CANARY` | Qwen | 31415 | repeat 2 |
| 7 | `g15-qwen-original-s1729` | `G1_5_CODEC` | Qwen | 1729 | `ORIGINAL` |
| 8 | `g15-qwen-mask-s1729` | `G1_5_CODEC` | Qwen | 1729 | `MASK` |
| 9 | `g15-qwen-mask-correction-s1729` | `G1_5_CODEC` | Qwen | 1729 | `MASK_CORRECTION` |
| 10 | `g15-qwen-oracle-clean-s1729` | `G1_5_CODEC` | Qwen | 1729 | `ORACLE_CLEAN` |
| 11 | `g15-qwen-sham-benign-edit-s1729` | `G1_5_CODEC` | Qwen | 1729 | `SHAM_BENIGN_EDIT` |
| 12 | `g14-mai-s1729-r1` | `G1_4_CANARY` | MAI | 1729 | repeat 1 |
| 13 | `g14-mai-s1729-r2` | `G1_4_CANARY` | MAI | 1729 | repeat 2 |
| 14 | `g14-mai-s2718-r1` | `G1_4_CANARY` | MAI | 2718 | repeat 1 |
| 15 | `g14-mai-s2718-r2` | `G1_4_CANARY` | MAI | 2718 | repeat 2 |
| 16 | `g14-mai-s31415-r1` | `G1_4_CANARY` | MAI | 31415 | repeat 1 |
| 17 | `g14-mai-s31415-r2` | `G1_4_CANARY` | MAI | 31415 | repeat 2 |
| 18 | `g15-mai-original-s1729` | `G1_5_CODEC` | MAI | 1729 | `ORIGINAL` |
| 19 | `g15-mai-mask-s1729` | `G1_5_CODEC` | MAI | 1729 | `MASK` |
| 20 | `g15-mai-mask-correction-s1729` | `G1_5_CODEC` | MAI | 1729 | `MASK_CORRECTION` |
| 21 | `g15-mai-oracle-clean-s1729` | `G1_5_CODEC` | MAI | 1729 | `ORACLE_CLEAN` |
| 22 | `g15-mai-sham-benign-edit-s1729` | `G1_5_CODEC` | MAI | 1729 | `SHAM_BENIGN_EDIT` |

There are no replacement calls, retries, warm-up generations, probe
generations, or result-dependent calls. A health endpoint may be polled but
must not generate model output. A logical-call failure is recorded and is
terminal for the PASS claim; it must not cause a substitute request.

The G1.4 pairs must use independently constructed SDK invocation state, with no
conversation/session reuse and no request mutation other than the frozen seed.
The G1.5 calls must preserve the frozen request/diff/mapping for their named arm
and use the corresponding selected CPU codec. Returned text and parsed actions
are evidence-only inert data and must never be executed or fed into a later
request.

## 5. Shared-GPU and process isolation

GPU 0 is shared. Existing foreign processes are permitted and must be treated
as immutable external facts, never as cleanup targets. The runner must capture
a preflight and postflight GPU process snapshot sufficient to distinguish its
own service processes from all pre-existing/foreign processes. It may use the
card only if the observed live capacity assessment is adequate for the frozen
single-model launch; it must not claim exclusive ownership.
The authority's capacity floor is exactly 64 GiB (`68719476736` bytes), not a
caller-selected positive value. It must be checked again immediately before
each of the Qwen and MAI launches. Falling below the floor stops the batch
before that launch; the runner must not reclaim memory from another process.
That immediate launch-boundary preflight must also repeat the exact GPU-process
baseline/isolation check, port-free check, authority-expiry check, and bound
`nvidia-smi` executable check before `Popen`; its content-addressed receipt is
part of `SERVICE_LAUNCHED` and the model lifecycle receipt.

The following fail-closed rules are mandatory:

1. The sole permitted foreign-PID inspection is the minimum non-secret identity
   read needed for shared-card invariance and PID-reuse protection: current UID
   and the start-time field from `/proc/<pid>/stat`. No signal, renice, cgroup
   change, debugger attach, or any other foreign-PID operation is permitted;
   specifically, foreign `cmdline`, `exe`, `environ`, `fd`, `cwd`, `mem`, maps,
   stack, and other `/proc` files or links must not be read or collected.
2. If port 18007 is occupied before launch, the batch stops with evidence. It
   must not kill the listener or choose another port.
3. Service discovery is restricted to the exact direct child and Linux
   `/proc/<owned-pid>/task/<tid>/children` traversal. It must not enumerate the
   global process table or read a foreign process's command, executable, or FD
   tree. Every signal uses a pidfd; numeric `os.kill`, `killpg`, and any broad
   process-group fallback are forbidden.
4. Each normal service launch receipt must capture PID, current UID, `/proc`
   start-time identity, PGID, SID, executable hash, command hash, model ID,
   served name, GPU UUID, port, environment hash, and its provisional owned
   tree. `SERVICE_LAUNCHED` must be durably persisted with that frozen tree
   before the guard becomes cleanup-eligible. If publication fails, an
   independently persisted
   `SERVICE_TREE_FROZEN_FOR_FAILED_START_CLEANUP` event is required before any
   stop attempt; otherwise zero signals are permitted.
5. The one narrow failed-acquisition exception begins only after this runner's
   `Popen` returns. The direct child must be pidfd-pinned immediately and bound
   by exact Popen PID, direct PPID, namespace UID, start time, `PGID=SID=PID`,
   intended command/model/GPU/host/port, and same-UID task-child ancestry. A
   `PROVISIONAL_ACQUISITION_FROZEN` event containing that exact tree must be
   persisted before pidfd cleanup; the result is then recorded as
   `PROVISIONAL_ACQUISITION_CLEANUP` and never as `SERVICE_LAUNCHED`. Failure to
   pin, freeze, or persist the tree authorizes no signal and makes release and
   evidence closure unproven.
6. A stop signal may target only an exact recorded PID whose live UID, start
   time, PGID, SID, executable/command binding, model, GPU UUID, and port still
   match its receipt. PID reuse or any mismatch means no signal is sent and the
   batch fails closed. A root that has already exited does not erase a still
   live, exactly recorded descendant; cleanup may target only that revalidated
   descendant after persisting the corresponding frozen-tree receipt. A dead
   root with no previously recorded exact descendant tree authorizes zero
   signals and leaves release unproven.
7. Cleanup state and its append-only signal trace must survive partial failure.
   An outer emergency retry may revalidate and target only remaining identities
   from the same persisted tree. The process/guard/failed-acquisition handle may
   be retired only after guarded exit, port release, and absence of the runner's
   own GPU allocation are all proven.
8. The set of process-management targets must be a subset of the two recorded
   service trees, `foreign_process_target_count` and broad-signal count must be
   exactly zero, and every `INTENDED`/`SENT` signal ledger entry must identify
   the persisted cleanup attempt and use `signal_api=PIDFD`.

Every bounded auxiliary command used for Git, loopback setup, runtime probes,
or GPU inspection follows a separate, narrower owned-command protocol. The
runner starts a stdlib-only launch-gate child in a new session, pidfd-pins and
procdir-pins its exact root identity before releasing the one-byte gate token,
polls stdout/stderr under explicit byte caps and a deadline, and forbids any
descendant. `subprocess.run(timeout=...)`, `Popen.kill`, `Popen.send_signal`,
`Popen.communicate(timeout=...)`, numeric `os.kill`, and all equivalent numeric
PID timeout paths are forbidden. Timeout, output overflow, an unexpected late
fork, or a post-acquisition census exception may clean only the proven helper
tree, child first, through pidfds. Because a forked helper child may legitimately
`exec` before the forbidden-descendant cleanup runs, that narrow cleanup
revalidates the held pidfd/procdir plus UID/PID/start-time/PGID/SID continuity;
it does not relax the full executable/argv binding required for a model service.
If the helper root was not proven, or is reused/foreign, the gate remains closed
and zero signals are permitted.

If a foreign process independently changes or exits during the batch, the
runner may compare only its recorded UID and `/proc/<pid>/stat` start time; it
must not investigate further or act on it. If this minimum comparison cannot
prove invariance, the batch records
`GPU_SMOKE_FOREIGN_PROCESS_INVARIANCE_UNPROVEN` and cannot PASS.

## 6. Evidence closure

Live evidence belongs in a repo-external, owner-controlled output root. The Git
repository contains only this contract, schemas, secret-free fixtures, and CPU
tests. A complete publication contains, at minimum:

1. the exact authority and smoke-packet artifacts and their SHA-256 references;
2. client/server environment receipts;
3. preflight/postflight GPU identity, capacity, allocation, process, and port
   snapshots;
4. complete pre/post model snapshot-tree inventories for both models;
5. two ordered service lifecycle receipts including guarded stop decisions;
6. exactly 22 ordered call receipts, each binding the final application request,
   transmitted request projection, raw response or failure, usage, latency,
   physical-request count, retry count, and host-parser classification;
7. G1.5 request/diff/reversible-mapping hashes for each of the ten arm calls;
8. an operation ledger proving zero foreign-PID targets, zero action execution,
   zero broad signals, zero response feedback, zero non-loopback connection,
   exact socket/network observations, exact signal intent/sent counts, and zero
   secret use;
9. an exact-file-set content-addressed manifest plus schema, hash, and secret
   scan results;
10. the namespace/outer-FD closure, source/runtime, owner-only scratch pre/post,
    sealed private-runtime and separate client/server site-package pre/post
    censuses, launcher scratch, server-environment, immediate-launch preflight,
    exact `nvidia-smi` binding, and actual-byte schema-validation receipts.

There are eleven normative JSON schemas under `schemas/g1_gpu_smoke/`:
authority, packet, preparation, event, call, lifecycle, owned auxiliary command,
manifest, stored execution receipt, returned execution-locator envelope, and
error. The stored execution receipt deliberately contains no locator fields.
The returned envelope adds only `terminal_receipt`, `run_relative_path`,
`terminal_file`, and `manifest_file`. Every inline owned-command receipt is
first canonicalized into a deduplicated content object and replaced by its
content reference in enclosing event, stage, runtime, GPU, failure, and terminal
evidence. The PASS/FAIL operation ledger and schema-validation receipt bind the
same sorted exact reference census. The pre-terminal verifier reads the actual
on-disk content-object bytes and validates ten stored schemas, including each
owned-command object; the eleventh locator schema is validated over the returned
envelope on CPU so it does not create a stored self-reference.

The manifest closes the exact event-file and content-object census. Its
manifest and terminal objects are the only two explicit self-exclusions; after
installing them the exact run-directory census must contain no missing, extra,
symlinked, hard-linked, colliding, partial, or otherwise unlisted member. Every
named run file and content object must remain a singly linked owner-only regular
file throughout no-follow readback; an external hard link is terminal evidence
invalidity even when its directory entry lies outside the run directory.

A PASS has exactly 33 ordered events:
`RUN_STARTED`, `PREFLIGHT_VALIDATED`; then, for each model in frozen order,
`MODEL_PREFLIGHT_VALIDATED`, `SERVICE_LAUNCHED`, `SERVICE_READY`, eleven
`CALL_COMPLETED` events, and `SERVICE_LIFECYCLE_CLOSED`; finally
`RUN_PASS_VALIDATED`. A failure retains the exact emitted prefix, may add
`SERVICE_TREE_FROZEN_FOR_FAILED_START_CLEANUP`,
`PROVISIONAL_ACQUISITION_FROZEN`, `PROVISIONAL_ACQUISITION_CLEANUP`,
`CALL_FAILED`, `SERVICE_CLEANUP_ATTEMPT_FAILED`,
`EMERGENCY_OWN_SERVICE_CLEANUP`, `EMERGENCY_CLEANUP_FAILED`,
`SERVER_LOG_CAPTURE_FAILED`, `RUNTIME_SCRATCH_CENSUS_FAILED`,
`RUNTIME_TREE_CENSUS_FAILED`, or
`SERVER_LOG_CAPTURE_SKIPPED_UNCLOSED_WRITER` as applicable, and must end its
retained event stream with `RUN_FAILED` before the FAIL terminal seal. These,
together with the PASS kinds, are the exact 20-value event vocabulary; no other
event kind is admissible production evidence.

Evidence may read, scan, and store a server log only after its writer is proven
closed. If cleanup is unproven and a writer may remain live, the runner must
emit `SERVER_LOG_CAPTURE_SKIPPED_UNCLOSED_WRITER`, must not race the log, and
must set `server_log_capture_complete=false`,
`evidence_closure_proven=false`, and `own_service_release_proven=false` as
applicable. A FAIL may never claim exact closure or release without those
facts.

Failures are evidence and must be retained; they must not be rewritten into a
PASS. A PASS requires all 22 logical records, 22 physical requests, zero
retries, both guarded lifecycle closures, identical model pre/post inventories,
the exact GPU UUID, zero foreign-PID targets, an exact manifest file set, and
all required checks passing.

## 7. Stable fail-closed codes

The production runner's `GPU_SMOKE_*` codes are normative; documentation and
schemas must not invent a second vocabulary. The exact v1 vocabulary is the
128-value enum in
`schemas/g1_gpu_smoke/gpu_smoke_error.schema.json`, generated from the stable
production runner plus its explicit CLI confirmation guard and frozen before
the live run. Every production terminal `error_code` must validate against that
enum. No v1 code may be added, renamed, collapsed, or silently remapped after
results are observed; a needed addition requires a new schema and contract
version before execution.

`GPU_SMOKE_UNCLASSIFIED_FAILURE` is the mandatory terminal mapping for an
otherwise unknown production exception. It remains a FAIL and may never be
ignored or mapped to PASS. The removed development sentinel
`GPU_SMOKE_EXECUTION_IMPLEMENTATION_INCOMPLETE` is deliberately absent from the
production enum and is not an executable or admissible terminal state.

## 8. Explicitly forbidden operations

D-034 does not authorize external network access, credentials, a real external
provider, formal/case/capsule input, 190-unit replay, natural task execution,
backend restore, deterministic prefix replay, Docker/MobileWorld environment
startup, emulator access, GUI/tool/action execution, response feedback,
treatment generation, G1.6 mutation or promotion, formal export/admission/seal,
runtime Sentinel behavior, or G1.7+. It does not authorize stopping, modifying,
or inspecting any foreign process beyond the exact non-secret UID and
`/proc/<pid>/stat` start-time exception in Section 5.

Any expansion of GPU, UUID, endpoint, models, fixture, seeds, repeats, arms,
call count, retry policy, process policy, network policy, or evidence rules
requires a new owner decision and a new contract version before execution.
