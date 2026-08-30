# G1 GPU Live Smoke Contract v1 — Amendment 1

Status: **LOCKED narrow operational correction for ALE-322 / D-034**  
Document type: Normative authority, launcher, namespace, and evidence epoch amendment  
Amendment ID: `mobileworld.g1.gpu-live-smoke/contract-v1-amendment-1`  
Amends: `G1_GPU_LIVE_SMOKE_CONTRACT_V1.md`  
Authorization: `G1_4_DECISION_LOG.md` D-034 amendment dated 2026-08-30 UTC  
Decision date: 2026-08-30 UTC

## 1. Purpose and precedence

The base v1 contract remains byte-frozen historical text. Its authority-v2
attempt could not complete the live preflight because the host's existing
supplementary groups cannot be cleared inside the one-ID user namespace. No
GPU probe, model load, server, request, provider invocation, replay, or
MobileWorld action occurred in that failed attempt.

The owner explicitly authorizes one narrower operational correction: retain the
exact existing supplementary-group identities while preserving zero
capabilities, `NoNewPrivs=1` at Stage2, and every existing foreign-process,
network, action, model, call-count, and evidence restriction. This amendment is
normative for any later D-034 attempt. Where it conflicts with the base
contract, this amendment controls. It does not rewrite or reinterpret any v1 or
v2 authority, receipt, evidence, scratch, or failure artifact.

## 2. Version and path topology

The corrected generation is closed as follows:

| Subject | Historical generation | Amended generation |
| --- | --- | --- |
| authority | `mobileworld.g1.gpu-live-smoke-authority/v2` | `mobileworld.g1.gpu-live-smoke-authority/v3` |
| launch shim binding | `mobileworld.g1.gpu-live-smoke-launch-shim/v1` | `mobileworld.g1.gpu-live-smoke-launch-shim/v2` |
| launch token prefix | `D034_STAGE0_V1` | `D034_STAGE0_V2` |
| preparation receipt | `mobileworld.g1.gpu-live-smoke-preparation/v1` | `mobileworld.g1.gpu-live-smoke-preparation/v2` |
| Stage1 pre-exec receipt | `mobileworld.g1.gpu-live-smoke-stage1-preexec/v1` | `mobileworld.g1.gpu-live-smoke-stage1-preexec/v2` |
| pre-import runtime census | `mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v1` | `mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v2` |
| network-namespace receipt | `mobileworld.g1.gpu-live-smoke-network-namespace/v1` | `mobileworld.g1.gpu-live-smoke-network-namespace/v2` |
| stored execution receipt | `mobileworld.g1.gpu-live-smoke-execution/v1` | `mobileworld.g1.gpu-live-smoke-execution/v2` |

The only admissible operational paths for the amended attempt are:

```text
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/launch-shim.v2
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/authority.v3.json
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/evidence-v3
/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/runtime-scratch-v3
```

The authority JSON Schema permits an owner-private absolute CPU-test root with
the exact `evidence-v3` or `runtime-scratch-v3` epoch suffix. The production
loader and launch entrypoint additionally require the complete shared paths
above. Any CPU test that may access or create evidence or scratch MUST patch the
production constants to temporary roots; read-only load/prepare fixtures may
carry the formal path strings without touching those paths. No CPU test may
write the formal shared roots.

Authority v1/v2, launch-shim v1, evidence v1/v2, and runtime-scratch v1/v2 are
obsolete for execution. They remain read-only historical evidence and MUST NOT
be retried, reused, renamed, truncated, overwritten, recursively deleted, or
treated as inputs to the amended attempt.

## 3. Exact owner-approved supplementary-group policy

The authority's closed `network_namespace.supplementary_groups` object is
`mobileworld.g1.gpu-live-smoke-supplementary-groups/v1` and MUST state exactly:

```json
{
  "schema_version": "mobileworld.g1.gpu-live-smoke-supplementary-groups/v1",
  "owner_approved": true,
  "policy": "OWNER_APPROVED_RETAIN_EXACT_GROUPS_ZERO_CAPS_V1",
  "host_group_vector": [1035, 109, 999],
  "host_primary_gid": 1035,
  "host_supplementary_gids": [109, 999],
  "host_os_getgroups_sorted": [109, 999, 1035],
  "inside_supplementary_gids_sorted": [0, 65534, 65534],
  "inside_groups_empty_required": false,
  "setpriv_group_option": "--keep-groups",
  "setgroups_control_expected": "deny",
  "capability_sets_all_zero_required": true,
  "no_new_privs_required": true,
  "docker_group_gid": 999,
  "kvm_group_gid": 109,
  "docker_kvm_filesystem_access_allowed": false,
  "docker_kvm_socket_access_allowed": false,
  "docker_kvm_action_allowed": false,
  "docker_af_unix_capability_retained": true,
  "kvm_device_capability_retained": true,
  "docker_kvm_invocation_allowed": false,
  "docker_kvm_use_mechanically_proven_absent": false,
  "formal_supplementary_group_isolation_proven": false,
  "nonformal_residual_disclosed": true
}
```

The host authority remains exact UID/GID `1035:1035`. The primary-first host
group vector and the sorted `os.getgroups()` vector are intentionally distinct.
The one-ID UID/GID mapping remains exactly host `1035` to inner `0`; unmapped
host identities are observed as overflow ID `65534`, including duplicates.
`setgroups` MUST read `deny`. Stage2 uses `setpriv --keep-groups` while dropping
the bounding, permitted, effective, inheritable, and ambient capability sets.

Retaining GID 109 and GID 999 acknowledges residual ability associated with KVM
and Docker AF_UNIX resources. It is not authority to open `/dev/kvm`, a Docker
socket or filesystem object, invoke Docker/KVM, inspect or signal another
process, or widen any pidfd target. Those operations remain forbidden even when
the kernel group membership could permit them.

## 4. Runtime group receipts and stage boundary

Stage1 and Stage2 each carry a closed
`mobileworld.g1.gpu-live-smoke-supplementary-groups-runtime/v1` receipt. Both
receipts record the exact duplicate-preserving inside group vector,
`/proc/self/status` group vector, `setgroups=deny`, all five capability fields,
the observed `NoNewPrivs` value, and point-in-time counts for Docker/KVM
filesystem descriptors, Docker AF_UNIX socket descriptors, Docker/KVM actions,
and foreign-process operations.

Stage1 is `STAGE1_PRE_SETPRIV`. It records the observed capability and
`NoNewPrivs` values but does not require or claim that the capability drop is
complete or that `NoNewPrivs` is zero. Stage2 is
`STAGE2_POST_SETPRIV`; every capability field MUST be the sixteen-character
hexadecimal zero value and `NoNewPrivs` MUST be `1`. At both stages the three
Docker/KVM point counts and the foreign-operation count MUST be zero. These are
point observations, not proof that retained Docker AF_UNIX or KVM capability is
continuously mechanically absent.

The Stage1 pre-exec receipt is v2 because it embeds the Stage1 group receipt.
The generic pre-import runtime census is likewise v2 because its Stage2 form
embeds the Stage2 group receipt. A v1 census cannot be accepted as evidence for
the retained-group boundary.

## 5. Network and foreign-process truth

The user/network namespace still mechanically isolates external INET and
INET6: only loopback is present, no usable default or non-loopback route exists,
and loopback self-connect succeeds. The v2 network receipt and v2 execution
ledger therefore use
`inet_inet6_external_network_mechanically_unavailable=true`. They MUST NOT use
the former broad `external_network_mechanically_unavailable` claim, because the
retained groups leave explicitly disclosed AF_UNIX/KVM residual capability.

The foreign-process rule is unchanged. No foreign PID may be signaled, altered,
attached, or inspected beyond the exact minimal UID and start-time reads already
authorized by D-034. Docker/KVM invocation and every MobileWorld/generated
action remain forbidden. All actual signals continue to require the persisted,
exact, pidfd-pinned own-process eligibility evidence defined by the base
contract.

## 6. Launch and evidence generation

Authority v3 binds launch-shim v2, token prefix `D034_STAGE0_V2`, and the fixed
authority-v3 path. The transient command grammar is:

```text
exec <absolute-launch-shim-v2> -c D034_STAGE0_V2:<absolute-authority-v3-path>:<lowercase-64-hex-authority-sha256>
```

All base-contract tool-shell, dynamic-loader residual, static-ELF shim,
same-file-descriptor validation, exact `shell=/bin/sh`, `login=false`,
`tty=false`, environment, inherited-FD, no-follow, source/runtime, packet,
model, GPU, endpoint, matrix, and no-action requirements remain in force.

The pre-freeze v3 implementation binding is exact:

- runner module SHA-256
  `f3b0ebce657361ddc997ae85e3ad7f890a5e66f41623f52dd00470f594c99f3d`
  over 520,831 bytes;
- runner CLI SHA-256
  `4b9830312dba1852ef0d5477a9a6d25418ee594d08080fda6888c522373fc776`
  over 152,412 bytes;
- launch-shim C source SHA-256
  `a01656dcd41feeef3e4d78921fbb61993f4f4fff3f0d52555dffa781cc80c831`
  over 55,526 bytes;
- deterministic launch-shim v2 ELF SHA-256
  `b185e8cf881c792bcaea374a45ca73bec892c06caa04ff015e69797a7d8584b7`
  over 26,272 bytes;
- outer bootstrap SHA-256
  `70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b`
  over 4,645 bytes.

Any later production byte change invalidates these values and requires a new
reviewed freeze before authority generation or live use.

Preparation v2 embeds the complete validated supplementary-group policy.
Execution v2 narrows only the network-ledger claim described above. The returned
execution-locator envelope has no independent version field; its referenced
`schema_version` is the stored execution v2 value. A v1 execution receipt MUST
fail v2 schema validation and MUST NOT be reinterpreted as v2 evidence.

## 7. Conformance requirements

In addition to every unchanged base-contract test, conformance MUST prove that:

1. authority, preparation, network, execution, and locator validation reject an
   obsolete or mixed generation;
2. production rejects any non-exact operational evidence/scratch root while
   every CPU fixture that may access or create those roots is confined to
   patched temporary v3 roots;
3. Stage0 observes exact host identity and groups without entering the Stage1
   call graph, and Stage1 observes exact mapped identity/groups without entering
   the Stage0 call graph;
4. Stage1 rejects group, `setgroups`, retained Docker/KVM descriptor, and AF_UNIX
   socket drift while faithfully recording capability and `NoNewPrivs` values;
5. Stage2 rejects any nonzero capability, missing `NoNewPrivs`, group,
   `setgroups`, descriptor, socket, namespace, route, or interface drift;
6. PASS and FAIL execution ledgers contain the INET/INET6-specific key and no
   broad all-channel network claim;
7. no CPU test touches GPU, model, provider, external network, service, signal,
   replay, Docker/KVM, foreign process, or MobileWorld action boundaries.

## 8. Scope remains closed

This amendment does not expand D-034's GPU, UUID, capacity floor, models,
endpoint, source, packet, seed, repeat, arm, call count, retry, process, network,
evidence, or non-formal status. It does not alter any formal G1.3/G1.4/G1.5/G1.6
gate and does not authorize replay, treatment generation, provider access,
external networking, Docker/KVM use, foreign-process action, or generated GUI
action. A further scope or epoch change requires a new owner decision and a new
versioned amendment or contract.
