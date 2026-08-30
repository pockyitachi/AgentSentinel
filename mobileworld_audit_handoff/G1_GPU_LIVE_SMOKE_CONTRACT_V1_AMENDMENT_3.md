# G1 GPU Live Smoke Contract v1 — Amendment 3

Status: **LOCKED narrow server-child import correction for ALE-322 / D-034**
Document type: Normative server-environment and evidence-epoch amendment
Amendment ID: `mobileworld.g1.gpu-live-smoke/contract-v1-amendment-3`
Amends: `G1_GPU_LIVE_SMOKE_CONTRACT_V1.md`, Amendment 1, and Amendment 2
Authorization: `G1_4_DECISION_LOG.md` D-034 Amendment 3 dated 2026-08-30 UTC
Decision date: 2026-08-30 UTC

## 1. Purpose, v4 failure fact, and precedence

The authority-v4 attempt failed closed with `GPU_SMOKE_SERVER_EXITED_EARLY`
and completed zero of the exact twenty-two logical or physical HTTP calls. A
read-only GPU0 baseline probe occurred and the owned Qwen vLLM API-server root
process was launched. Readiness was never established, no during-service GPU
receipt was produced, and the evidence does not prove that model weights were
loaded or that an owned GPU allocation occurred. The server's owned internal
child invoked the private Python 3.12 interpreter with
`-m vllm.model_executor.models.registry`, exited with status 1, and recorded
`ModuleNotFoundError: No module named 'vllm'`. The closed server environment did
not expose the authority-bound private server site-packages tree to that
non-isolated child.

The service exited by itself. Signal intent and sent counts were zero, foreign
target count was zero, and port, session, and own-GPU release were proven.
Provider, replay, generated-action, and MobileWorld-action counts remained
zero. These statements supersede any inference that the v4 attempt failed
before a GPU probe or before process launch; they do not claim model loading.

The owner authorizes only the server-child import correction and fresh epoch
defined below. The byte-frozen base contract and Amendments 1 and 2 remain
historical normative text. This Amendment 3 controls only where it explicitly
conflicts with them and does not reinterpret any earlier authority, receipt,
evidence, scratch, or failure artifact.

## 2. Version and path topology

The only admissible new generation is:

| Subject | Failed generation | New generation |
| --- | --- | --- |
| authority | `mobileworld.g1.gpu-live-smoke-authority/v4` | `mobileworld.g1.gpu-live-smoke-authority/v5` |
| launch shim binding | `mobileworld.g1.gpu-live-smoke-launch-shim/v3` | `mobileworld.g1.gpu-live-smoke-launch-shim/v4` |
| launch token prefix | `D034_STAGE0_V3` | `D034_STAGE0_V4` |
| server-environment receipt | `mobileworld.g1.gpu-live-smoke-server-environment/v1` | `mobileworld.g1.gpu-live-smoke-server-environment/v2` |
| preparation receipt | `mobileworld.g1.gpu-live-smoke-preparation/v2` | unchanged v2 |
| Stage1 pre-exec receipt | `mobileworld.g1.gpu-live-smoke-stage1-preexec/v2` | unchanged v2 |
| pre-import runtime census | `mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v2` | unchanged v2 |
| network-namespace receipt | `mobileworld.g1.gpu-live-smoke-network-namespace/v2` | unchanged v2 |
| stored execution receipt | `mobileworld.g1.gpu-live-smoke-execution/v2` | unchanged v2 |

The new operational paths are exactly:

```text
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/launch-shim.v4
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/authority.v5.json
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/evidence-v5
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/runtime-scratch-v5
```

The authority schema permits owner-private absolute CPU-test roots ending in
the exact `evidence-v5` and `runtime-scratch-v5` suffixes. The production loader
and launch entrypoint require the complete shared paths above. Any CPU test
that may access or create evidence or scratch MUST patch the production
constants to temporary roots. Read-only load and preparation fixtures may
carry the formal strings without touching those roots. No CPU test may write a
formal shared root.

Authority v1 through v4, launch shims v1 through v3, tokens V1 through V3, and
evidence or runtime-scratch generations v1 through v4 are obsolete for
execution. The authority-v4 file, launch-shim v3, evidence-v4 terminal failure
closure, server log, and runtime-scratch-v4 remain immutable historical
evidence. They MUST NOT be retried, reused, renamed, truncated, overwritten,
deleted, moved into the v5 closure, or relabeled as successful.

## 3. Exact closed server Python environment

The server environment remains a closed allowlist and inherits no ambient
environment. In addition to every previously bound key, it MUST contain
exactly these Python-startup controls:

```text
PYTHONPATH=<authority.server_runtime.site_packages_path>
PYTHONSAFEPATH=1
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
```

`PYTHONPATH` is derived, not independently authorized. It MUST equal the one
existing authority-bound path
`<authority.private_runtime.root>/site-packages/server`. The value MUST be one
absolute lexical path with no path separator, empty component, source checkout,
editable/shared environment, client tree, extra directory, or second entry.
The existing whole-tree SHA-256, entry-count, byte-count, owner-role, mode,
symlink, hardlink, and pre/post census bindings for that private server tree
remain the authority for all bytes reachable through this path. Amendment 3
does not add an unbound or redundant runtime path.

Every model launch MUST store a canonical server-environment v2 receipt before
the process launch. The receipt binds the exact raw environment object, sorted
key list, canonical environment digest, closed-allowlist fact, no-ambient fact,
and a closed derived `python_child_import` receipt. The derived receipt MUST
bind the authority-derived server path, expected non-isolated child module
invocation, inherited-exact-environment/no-explicit-env facts, CPython safe-path,
no-user-site and no-bytecode policy, current-working-directory import exclusion,
and absence of startup customization. Raw secrets remain forbidden.

`gpu_smoke_server_environment.schema.json` is the eleventh stored normative
schema. Every server-environment receipt MUST be materialized as its own
content-addressed object before an enclosing lifecycle event or terminal
receipt is serialized. Pre-seal validation and the final verifier MUST read the
actual object bytes, validate them against that schema, and prove the exact
deduplicated receipt-reference census recorded by both the operation ledger and
schema-validation receipt. The execution-locator schema remains return-only and
becomes the twelfth, nonstored schema; it does not weaken stored-object closure.

## 4. Parent and child import boundaries

The owned API-server parent continues to use the authority-bound private
interpreter with exact isolated flags `-I -S -B -X pycache_prefix=/dev/null`.
It therefore ignores `PYTHONPATH`; the existing hash-bound bootstrap inserts
the single sealed server site-packages path explicitly before running the vLLM
entrypoint. No environment path may replace or precede that manual bootstrap.

The vLLM registry child is deliberately non-isolated and inherits the exact
closed server environment without an `env` override. Before v5 authorization,
an AST-level regression MUST bind that invocation and inheritance behavior,
and a CPU-only fake private-package probe MUST demonstrate all of the following:

1. the child imports its module from the single sealed server path;
2. `sys.flags.safe_path` is true, `sys.flags.no_user_site` is true, and bytecode
   writing is disabled;
3. neither the empty path nor the child current working directory appears in
   `sys.path`;
4. the remaining import roots are only the authority-derived server path and
   the private interpreter's zip, standard-library, and `lib-dynload` roots;
5. no `sitecustomize`, `usercustomize`, or `.pth` startup customization is
   reachable; and
6. the probe creates no bytecode cache and performs no GPU, model, network,
   provider, replay, signal, Docker/KVM, or MobileWorld action.

Any path, environment, AST, child-flag, import-origin, customization, or
receipt drift is terminal before a request is allowed.

The startup-policy census MUST pin, open without following links, and inventory
the top level of the sealed server site-packages root, private standard-library
root, private `lib-dynload`, and the default private site-packages root if it
exists. Every `sitecustomize` or `usercustomize` name prefix is forbidden,
including source, bytecode, extension-module, package, and namespace-package
forms; every top-level `.pth` file is forbidden. The unbound
`lib/python312.zip` path MUST be absent, whether regular or symlinked. The
canonical census and absence policy MUST be identical before and after the
non-isolated child probe.

## 5. Fresh construction and mixed-epoch rejection

The v5 attempt requires a new clean detached source at the reviewed commit, a
new owner-sealed source tree with no bytecode cache, a deterministically rebuilt
and `NO_REPLACE`-installed launch-shim v4, a newly issued canonical authority
v5, and repeated deterministic preparation from its fixed path. The final
runner, CLI, shim source/ELF, bootstrap, launch gate, source, packet, model,
runtime, and tool hashes MUST be recorded after the v5 surface freezes and
before any v5 authority is generated. Any later production byte change
invalidates that closure.

The reviewed v5 production byte closure is exact:

- runner module SHA-256
  `2c041cdc1e40828a5b2e1147d2caa454fa07989cb6e2562cb7d762413d81aa9c`
  over 537,707 bytes;
- runner CLI SHA-256
  `54b7945eac65b327e5b8abf8600082aa681a61cd91e3fe500af85b87f3963f0b`
  over 152,402 bytes;
- launch-shim C source SHA-256
  `c76b147f627238f6f398dc1c1145144a8b55fabaa7564f9b975369535e214499`
  over 55,526 bytes;
- deterministic launch-shim v4 ELF SHA-256
  `a8c5398731ca5b5c89960bdf3b2e7ee87fd62c131987feb8a58e3bedb9564f91`
  over 26,272 bytes;
- outer bootstrap SHA-256
  `70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b`
  over 4,645 bytes; and
- owned-command gate SHA-256
  `70c01194e4ed6ad7cf54a5ffb0caa72bb9d8fa1694544665d53707b90279b061`
  over 228 bytes.

Any later change to those bytes invalidates the v5 closure and requires a new
reviewed freeze before authority generation or execution.

Schema, manual loader, preparation, static shim, and entrypoint MUST reject
every obsolete or mixed tuple, including authority v4 with a v5 path,
authority v5 with shim v3, token V3, evidence-v4, runtime-scratch-v4, a
server-environment v1 receipt, or a tool-shell command prefix naming an old
path or token. No earlier field or artifact may be inferred, upgraded, or
copied into the new epoch.

## 6. Unchanged safety and authorization boundary

All Amendment 1 and Amendment 2 boundaries remain exact: retained
supplementary groups, Stage2 zero capabilities and `NoNewPrivs=1`, Docker/KVM
residual disclosure with invocation forbidden, mechanical external INET/INET6
isolation, pidfd-only owned-process cleanup, foreign-process minimal reads and
no signaling, shared GPU0 UUID and 64-GiB floor, Qwen-then-MAI lifecycle,
twenty-two calls, zero retry, non-stream, inert responses, no credentials, no
external provider, no replay, no feedback, no generated action, and no
MobileWorld action. This correction grants no broader filesystem, process,
network, GPU, model, provider, or action authority.

Before any v5 live attempt, the complete CPU suite, schema/meta-schema checks,
formatter, linter, compile checks, deterministic shim build, diff checks, and
independent red-team review MUST pass on the exact frozen bytes. Fresh shim and
authority installation require no-follow/no-replace semantics and fixed-path
readback. Evidence-v5 and runtime-scratch-v5 MUST be absent before execution.
This amendment does not itself mark those artifact or live gates complete.
