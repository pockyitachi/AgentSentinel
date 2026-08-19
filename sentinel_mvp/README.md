# Sentinel MVP

This directory turns the `seed_baseline` audit into executable middleware
plumbing.  It is deliberately offline and deterministic: it does not call a
paid model, mutate QR-MW, or claim that its curated fixture decisions are a
general-purpose verifier.

The first milestone is to prove the deployment contract end to end:

1. reconstruct the history visible before a selected Seed decision;
2. represent an auditable claim and its evidence;
3. apply a claim/span-level history-gate operation;
4. emit the filtered history and evidence-grounded correction block that the
   actor would receive on its next model call;
5. preserve the untouched raw history in the fixture/sidecar record.

Canonical gate operations follow proposal section 3.4:

- `KEEP`
- `DROP`
- `REPLACE`
- `ARCHIVE`
- `KEEP_UNCERTAIN`

At render time, `DROP` masks only the targeted claim span, `REPLACE` inserts a
minimal correction, `ARCHIVE` removes a true but inactive-branch span from the
active view, and `KEEP_UNCERTAIN` leaves uncertain text untouched.

The bundled replay fixtures are drawn only from
`traj_logs/seed_baseline`; images remain in QR-MW and are referenced by absolute
path plus SHA-256 rather than copied.

## What Seed-2.0-Pro receives before each action

The audited `seed_baseline` run uses Doubao Seed-2.0-Pro
(`doubao-seed-2-0-pro-260215`) with MobileWorld's `seed_agent`. Its default
`history_n=3` is an **image limit, not a step/text limit**:

- before action `t`, all prior assistant responses `P1 ... P(t-1)` remain in
  the prompt;
- only the latest three screenshot observations remain, including the current
  screenshot: normally `S(t-2), S(t-1), S(t)` once `t >= 3`;
- non-image tool/user-result observation messages are not removed by the image
  counter.

For example, before action 50 the actor receives text from `P1 ... P49`, but
only screenshots `S48, S49, S50`. This is why an old textual claim can remain
active after its original visual evidence has disappeared.

## Runtime boundary

```text
Seed history + current GUI + claim/evidence decision
                    |
             Sentinel core gate
                    |
       KEEP / DROP / REPLACE / ARCHIVE /
               KEEP_UNCERTAIN
                    |
              Seed host adapter
                    |
 filtered_history_for_next_prompt
 + current GUI
 + Sentinel-authored correction block
```

The host adapter removes only the targeted claim span. A `REPLACE` correction
is emitted as a Sentinel-authored user block beside the current observation;
it is not rewritten into the old assistant message as if the actor had said it
originally. The raw history remains untouched in the sidecar.

## Run

From `/Users/apigo/Desktop/agent monitor`:

```bash
env PYTHONPATH='/Users/apigo/Desktop/agent monitor/sentinel_mvp' \
  python3 -m unittest discover -s sentinel_mvp/tests -v

python3 sentinel_mvp/tools/build_seed_fixtures.py --mode check
python3 sentinel_mvp/tools/run_replay_demo.py --check
python3 sentinel_mvp/tools/run_replay_demo.py
```

The final command writes
`sentinel_mvp/output/seed_replay_demo_v1.json`. Every replay result contains:

- `sentinel_input`: target step, current screenshot, claim span, evidence and
  requested operation;
- `sentinel_output.filtered_history_for_next_prompt`: the exact derived Seed
  history strings for the next call;
- `sentinel_output.actor_messages_for_next_model_call`: the host-rendered
  history/current-observation messages (system/task prompts remain the host's
  responsibility);
- `sentinel_output.correction_block`: evidence-grounded guidance attached to
  the current user observation;
- `audit`: before/after source record, hashes, provenance and the explicit fact
  that this first gate decision is curated rather than automatically predicted.

## Scope boundary

This MVP tests the adapter, contracts, filtering semantics, and replay
reproducibility.  Automatic claim extraction and evidence verification are the
next component.  Until that component is calibrated, fixture gate decisions
must be described as curated/gold decisions, not deployment predictions.
