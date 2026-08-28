# G1.6 Private Annotation Workspace Runbook

Status: CPU-only manual-curation checkpoint for ALE-324 / G1.6.

This runbook explains how the owner can open the local annotation website after reviewing the
reviewer roster and storage paths.  It does not authorize or start a model, provider, external
network client, GPU process, replay, treatment response, or MobileWorld/generated GUI/tool action.  Human
clicks in the annotation browser are curation inputs and are never executed as MobileWorld
actions.  The site binds only to
`127.0.0.1`, uses local assets, and stores its append-only journal outside Git.

## What the website covers

The website exposes every human research choice currently required before a formal G1.6 export:

- `ACTION_GOLD`: accepted next-action alternatives, exact normalized actions, point/drag region
  sets, text variants, direction sets, tolerances, evidence references, and rationales;
- `TRANSFORMATION`: focal/oracle/sham spans, protected tool-call spans, human correction
  alternatives, correction evidence and rationales, delimiter repairs, exact G1.5 CPU previews,
  target-only diffs, reversible mappings, and explicit preview confirmation;
- `CONSISTENCY_AUDIT`: the post-resolution descriptive history/GUI consistency label and two
  rationales; and
- independent `PRIMARY`, `SECONDARY`, and channel-bound `ADJUDICATOR` flows.  A third review opens
  only for a mechanically detected material disagreement.

The browser never receives the complete capsule, raw filesystem paths, provider configuration,
captured raw target response, post-action state, later trajectory, outcome, or the other curation
channel's proposal.  `ACTION_GOLD` and `TRANSFORMATION` never receive the historical natural
action.  Only the separately gated post-curation `CONSISTENCY_AUDIT` receives its exact normalized
action and parse outcome, as required for that descriptive task.  Final submissions are immutable
append-only records.

## Prerequisites

1. Run from the AgentSentinel repository root on the audited host.
2. Keep the active G1.3 v1.1 publication and the checked-in G1.5 CPU publication byte-unchanged.
3. Select a repository-external annotation root and codec-gate root owned by the current user.
4. Create a repository-external reviewer registry with mode `0600`.
5. Use genuinely independent reviewer principals.  Creating several aliases for one reviewer
   does not satisfy the contract's independence requirement.

The exact local CPU environment that contains the pinned `tokenizers==0.22.2` runtime is:

```text
/shared/linqiang/evofsm_project/SkyRL-AndroidWorld/skyrl-agent/.venv/bin/python
```

The loader verifies the frozen Qwen and MAI tokenizer artifacts before importing that runtime.
It uses `Tokenizer.from_str` on already verified local bytes and always counts with
`add_special_tokens=False`.  It never downloads a tokenizer or loads model weights.

Before either CLI command, the owner must create the shared repository-external parent once with
mode `0700` (the implementation creates only the final configured leaf):

```bash
install -d -m 0700 /shared/linqiang/mobileworld_causal_replay_data/g1_6
```

## 1. Prepare the G1.5 codec-gate receipt

Choose a repository-external output root, then run the inspection-only command:

```bash
PYTHONPATH=MobileWorld/src \
  /shared/linqiang/agent_monitor/AgentSentinel/MobileWorld/.venv/bin/python \
  MobileWorld/scripts/run_g1_gold_curation.py \
  --g1-5-publication-manifest \
  mobileworld_audit_handoff/g1_5/cpu_publication_manifest.v1.json \
  --prepare-codec-gate-output-root \
  /shared/linqiang/mobileworld_causal_replay_data/g1_6/codec-gate-v1
```

The command prints the exact content-addressed receipt path.  Preserve that path for the website
command.  The receipt binds both CPU codecs, the preview implementation and schema, the diff
dependency, the G1.1 model manifest, and both pinned tokenizer records.  A boolean or caller-made
"verified" flag is never accepted.

## 2. Create the owner reviewer registry

Create a JSON file outside the repository, replace every placeholder with an owner-issued unique
principal and secret, and set mode `0600`.  The closed shape is:

```json
{
  "schema_version": "mobileworld.g1.owner-reviewer-registry/v1",
  "principals": [
    {"principal_id":"action-primary","role":"ACTION_GOLD_PRIMARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_1"},
    {"principal_id":"action-secondary","role":"ACTION_GOLD_SECONDARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_2"},
    {"principal_id":"transform-primary","role":"TRANSFORMATION_PRIMARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_3"},
    {"principal_id":"transform-secondary","role":"TRANSFORMATION_SECONDARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_4"},
    {"principal_id":"consistency-primary","role":"CONSISTENCY_AUDIT_PRIMARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_5"},
    {"principal_id":"consistency-secondary","role":"CONSISTENCY_AUDIT_SECONDARY","adjudication_channel":null,"access_secret":"REPLACE_WITH_UNIQUE_SECRET_6"},
    {"principal_id":"action-adjudicator","role":"ADJUDICATOR","adjudication_channel":"ACTION_GOLD","access_secret":"REPLACE_WITH_UNIQUE_SECRET_7"},
    {"principal_id":"transform-adjudicator","role":"ADJUDICATOR","adjudication_channel":"TRANSFORMATION","access_secret":"REPLACE_WITH_UNIQUE_SECRET_8"},
    {"principal_id":"consistency-adjudicator","role":"ADJUDICATOR","adjudication_channel":"CONSISTENCY_AUDIT","access_secret":"REPLACE_WITH_UNIQUE_SECRET_9"}
  ]
}
```

Each secret must contain at least 16 UTF-8 bytes.  Do not put real secrets in Git, shell history,
screenshots, issue comments, or the annotation journal.  The immutable workspace manifest records
only the registry's redacted semantic digest.

## 3. Owner-started foreground launch

Substitute the exact receipt and registry paths selected above:

```bash
PYTHONPATH=MobileWorld/src \
  /shared/linqiang/evofsm_project/SkyRL-AndroidWorld/skyrl-agent/.venv/bin/python \
  MobileWorld/scripts/run_g1_gold_curation.py \
  --annotation-root \
  /shared/linqiang/mobileworld_causal_replay_data/g1_6/annotation-workspace-v1 \
  --reviewer-registry /ABSOLUTE/REPO-EXTERNAL/reviewer-registry.v1.json \
  --g1-5-publication-manifest \
  mobileworld_audit_handoff/g1_5/cpu_publication_manifest.v1.json \
  --codec-gate-receipt /ABSOLUTE/REPO-EXTERNAL/sha256/xx/DIGEST.json \
  --load-local-pinned-tokenizers \
  --port 8766
```

Open `http://127.0.0.1:8766` in a browser on the same machine.  Keep the process in the owner's
foreground terminal.  Do not add workers, reload mode, proxy headers, a wildcard/non-loopback
bind, TLS termination, an external tunnel, or remote hosting.

The website refuses final review submission unless the exact codec gate is open.  Transformation
preview confirmation also remains blocked if either pinned local tokenizer is unavailable or if
any correction, evidence, span, delimiter repair, or sham selection changes after preview.

## 4. Human workflow

1. Each initial reviewer signs in only with the principal and role assigned in the owner registry.
2. Before a packet opens, confirm the blinded assignment, reviewer commitment, source-packet
   digest, assignment-packet digest, and visibility rules.
3. Save drafts as needed.  A draft is append-only but may be superseded by a later draft from the
   same role.
4. Submit only after every explicit human attestation is reviewed.  The final event is immutable.
5. The secondary reviewer cannot inspect the primary review before both are final.
6. If material fields disagree, the channel-bound adjudicator receives the two immutable same-
   channel proposals and must independently fill a complete resolved proposal plus one explicit
   resolution for every disagreement field.  There is no copy-primary or majority shortcut.
7. `CONSISTENCY_AUDIT` opens only after both formal channels resolve and remains descriptive; it
   cannot modify action gold, transformations, admission, scoring, or replay.

## 5. Checkpoint and completion boundary

The current implementation is an annotation-workspace checkpoint, not a frozen G1.6 bundle.  It
may create packets, previews, drafts, final human reviews, adjudications, and a non-authorizing
workspace receipt.  It deliberately reports:

```text
formal_g1_6_bundle=false
admission_ready=false
execution_ready=false
provider_invocation_allowed=false
treatment_response_generation_allowed=false
formal_replay_performed=false
```

Formal export/sealing remains fail-closed until all 190 units have the required independent
reviews and adjudications, the scorer/refusal-classifier bindings are frozen, and the exporter plus
cross-artifact validators pass the already-frozen G1.1 action/Transformation Plan/admission/seal
schemas.  The still-additive blinded-catalog/report records and their bindings must also be closed.
No treatment response generated later may revise a frozen human plan or accepted-action set.
