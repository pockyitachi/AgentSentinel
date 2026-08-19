# Sentinel Seed replay: one concrete trajectory point

This demo uses only the unmodified `seed_baseline` trajectory for
`CheckSetMeetTimeTask`.

## Before action 7

Seed's active text history contains six earlier assistant responses. In `P6`,
the actor states:

> the email said "Saturday, Nov 1st, 10:00 AM to 11:00 AM"

The preserved source-email screenshot `S3` instead shows November 15 at 3 PM.
The curated replay label therefore has:

```json
{
  "epistemic_status": "REFUTED",
  "operation": "REPLACE",
  "record_id": "s6",
  "evidence": "S3"
}
```

## What Sentinel produces

The history gate removes only that refuted span from `P6`; it retains every
other part of `P1 ... P6`. The Seed adapter then renders the next model input
with screenshots `S5, S6, S7` and attaches this block to the current `S7` user
observation:

```text
<sentinel_history_gate>
Verified history corrections for this decision:
- [s6] November 1 is a fabricated date. Recover from the current calendar
  page and create Board Meeting on November 15 at 3:00 PM. (evidence: S3)
</sentinel_history_gate>
```

So the operational output is not merely “this pre-step is risky.” It is a
concrete replacement prompt view:

```text
all valid prior assistant text
+ P6 with the refuted claim span removed
+ current screenshot S7
+ evidence-grounded Sentinel correction
```

The original `P6` remains unchanged in the audit sidecar. If the evidence were
not direct, the attempted `REPLACE` would fail closed to `KEEP_UNCERTAIN` and
the old text would remain visible with an unverified warning.

The complete machine-readable before/after result for this case and four other
Seed cases is in `output/seed_replay_demo_v1.json`.
