# G1 Locked Analysis Plan v1

Status: **LOCKED before treatment generation**  
Protocol: `mobileworld.g1.causal-replay/protocol-v1`  
Analysis ID: `mobileworld.g1.causal-replay/analysis-v1`  
Decision date: 2026-08-26 UTC

## 1. Confirmatory outcome and contrasts

The binary confirmatory outcome is whether the production-parser-normalized next action matches
the case's independently frozen accepted-action set. Models are analyzed separately.

Qwen co-primary contrasts:

1. `MASK - ORIGINAL`;
2. `MASK_CORRECTION - ORIGINAL`.

The estimand is task-equally weighted: first average paired differences over two technical
repeats, three seeds, and all admitted decisions in a task; then average task means. MAI applies
the same estimator to the Qwen-winning arm selected by the frozen rule below.

Secondary analyses are `ORACLE_CLEAN - ORIGINAL` as a curated reference contrast (not an assumed
numerical upper bound), all five categorical outcomes, structured-action change, stable
rescue/regression, natural-versus-replayed Original agreement, action
entropy, modal-action share, and descriptive `REFUTED`/`STALE` strata. They do not change the
confirmatory decision.

## 2. Decoding, repetitions, and isolation

- Actor decoding remains the host-native `temperature=0.0` configuration.
- Provider seeds are exactly `[1729, 2718, 31415]`.
- Each seed/arm is invoked twice as two fresh technical repeats.
- An intervention case therefore has 30 calls: 5 arms × 3 seeds × 2 repeats.
- A clean-control case has 12 calls: 2 arms × 3 seeds × 2 repeats.
- No response, session, conversation, or KV state carries to another call.

If the pinned endpoint cannot honor the seed field, the experiment stops before treatment
generation and requires a versioned amendment. Unseeded repeats cannot be relabeled as seeded
pairs after results exist.

## 3. Arm order

Arm order is deterministic and position-balanced using the public salt:

```text
mobileworld-g1-arm-order-v1-20260826
```

For each unit, enumerate blocks in `(seed list order, repeat 1 then 2)`. A SHA-256 of the exact
UTF-8 bytes of `salt|model_id|unit_id` fixes both the first rotation and one direction, where
`unit_id=case_id` for an intervention and `unit_id=control_id` for a clean control.
`digest[0] mod arm_count` is the initial rotation and `digest[1] mod 2` maps `0 -> +1`,
`1 -> -1`. Block index advances the rotation by that fixed direction; individual repeats are
**not** reversed. For zero-based block `b` and output position `j`, the scheduled arm is exactly
`base_order[(j + initial_rotation + direction * b) mod arm_count]`.

For intervention cases, `arm_count=5` and the base order is
`ORIGINAL, MASK, MASK_CORRECTION, ORACLE_CLEAN, SHAM_BENIGN_EDIT`.
Six consecutive rotations therefore put every arm in every position once and in exactly one
position twice, so the within-case position-count range is exactly one. The schedule builder must
assert this invariant before publication. For clean controls, `arm_count=2` and the base order is
`ORIGINAL, SHAM_BENIGN_EDIT`; the same hash, direction, and rotation formula yields exact 3/3
first-position balance across six blocks. The arm catalog freezes these as distinct
`STRICT_FIVE_ARM_ROTATION_V1` and `CLEAN_TWO_ARM_BALANCED_V1` contracts before invocation.

Each emitted run record is an immutable plan with `status=PLANNED`; execution and retry state is
recorded only in append-only outcome records.

## 4. Missingness and retries

- Provider transport, HTTP 5xx, or timeout: at most two retries after the first attempt.
- Retry request bytes, seed, model config, and arm must be identical.
- Parser failure, refusal, empty output, and no-op are outcomes, never retry reasons.
- Exhaustion is `MISSING`; do not substitute a seed or case.
- A complete pair is one `case × seed × repeat × contrast` for which both `ORIGINAL` and
  the contrasted arm returned a response. Primary estimates use exactly those pairs; a pair with
  either or both calls missing is excluded from the complete-pair estimate.
- For each contrast, first average the returned complete seed×repeat pairs within each case;
  then average the non-empty case means with equal case weight inside the task. Thus a case with
  more returned pairs cannot receive more weight than another non-empty case. A task with zero
  complete pairs for that contrast is excluded only from that complete-case estimate and is
  reported explicitly; it remains in the prespecified worst-case sensitivity. The sign-flip test
  and cluster bootstrap use the same contrast-specific set of tasks having at least one complete
  pair, without replacing an empty task or borrowing a task from the other contrast.
- A prespecified worst-case sensitivity scores intervention missing as non-acceptable and
  Original missing as acceptable. If both are missing, those two arm-specific assignments still
  apply, yielding the conservative paired difference -1.
- For each non-Original arm `a`, define its matched support as the exact
  `case × seed × repeat` units on which both `a` and `ORIGINAL` were scheduled. Compute
  `missing_rate(a)` and `missing_rate(Original_for_a)` on that same support. Intervention-arm
  supports contain only intervention cases. Report `SHAM_BENIGN_EDIT` separately on intervention
  and clean-control supports; neither support may be pooled to hide a differential in the other.
  A model stratum is `INVALID` if any arm/support missing rate exceeds 5% or if any matched
  absolute arm-minus-Original missing-rate difference exceeds 3 percentage points.

Every attempt is retained; no favorable retry may replace a valid earlier response.

## 5. Repeatability and nondeterminism

For every case/arm/seed, report disagreement between the two repeats in canonical structured
action, gold label, refusal, unparseable, and no-op status. Also report cross-seed action entropy
and modal-action share.

Repeat-disagreement denominators contain only `case × arm × seed` groups where both repeats
returned; groups with one or two missing calls are reported separately. Cross-seed entropy and
modal share use returned calls only and always report their returned/scheduled denominator.

A `STABLE_RESCUE` first requires all six `ORIGINAL` and all six intervention calls to return a
non-missing outcome. It then requires:

- intervention acceptable on at least 5 of 6 calls;
- Original acceptable on at most 1 of 6 calls;
- at least 2 of 3 seeds have both repeats changing from Original non-acceptable to intervention
  acceptable.

`MISSING` is never treated as non-acceptable for stable rescue/regression. `STABLE_REGRESSION`
swaps Original and intervention in that definition. If same-seed repeat
gold-label disagreement exceeds 20% for a model, that stratum is `INVALID`; high-variance cases
remain in the report and are not post-hoc deleted.

## 6. Paired inference

- Within each model and contrast, tasks are sorted lexicographically by the exact UTF-8 byte tuple
  `(task_name, task_run_id)`; cases within task are sorted by `case_id`; complete pairs within
  case follow frozen seed-list order and then repeat index. All means use IEEE-754 `float64` in
  that order under the pinned NumPy version.
- Qwen uses a task-cluster paired sign-flip test for both co-primary contrasts.
- With at most 20 task clusters, enumerate all sign flips; otherwise use 100,000 Monte Carlo
  sign flips with analysis seed `2026082601`. The one-sided alternative is positive improvement;
  a Monte Carlo p-value is `(1 + count(null statistic >= observed statistic)) / 100001`.
- Exact sign flips enumerate integer bit patterns from `0` through `2**n_tasks-1`, mapping bit 0
  to -1 and bit 1 to +1 in canonical task order. For each Monte Carlo contrast, independently
  reinitialize `numpy.random.Generator(numpy.random.PCG64(2026082601))`, draw exactly
  `rng.integers(0, 2, size=(100000, n_tasks), dtype=np.int8)`, and apply the same 0/1 sign map.
- Apply Holm correction to the two one-sided p-values.
- Produce 50,000 task-cluster percentile bootstrap draws using seed `2026082602`; an entire
  task's decisions, seeds, and repeats travel together.
- Each bootstrap draw samples exactly the observed number of tasks with replacement and carries
  every case/seed/repeat belonging to each sampled task. Report ordinary two-sided percentile 95%
  intervals. For each co-primary contrast, take the 2.5th percentile of its 50,000
  bootstrap-statistic distribution as the marginal 97.5% one-sided lower bound; together the two
  marginal bounds are the frozen Bonferroni 95% simultaneous lower-bound procedure.
- Every bootstrap quantile uses NumPy's `quantile(..., method="linear")` convention
  (Hyndman–Fan type 7); no implementation may switch interpolation rules after seeing results.
- For each contrast or gate, independently initialize `Generator(PCG64(the frozen seed))` and
  create the full index matrix with
  `rng.integers(0, n_tasks, size=(50000, n_tasks), dtype=np.int64)`. Rows are evaluated in array
  order against the canonical task array; no earlier contrast may advance another contrast's RNG.
- Do not pool Qwen and MAI.

Both models run all five arms under the frozen schedule. The frozen Qwen-winning arm is the arm
among `MASK` and `MASK_CORRECTION` that passes every Qwen gate and has the larger task-weighted
point estimate; an exact tie selects `MASK_CORRECTION`. MAI's confirmatory replication contrast is
that predeclared winner; its other intervention contrast is secondary. If neither arm passes
Qwen, MAI is still run and reported under the unchanged protocol, but there is no confirmatory
replication arm and a fully valid experiment is classified `NULL`.

## 7. Clean and sham gates

For Qwen, freeze at least 30 clean cases from at least 30 distinct tasks. A clean paired call is
one `case × seed × repeat` for which both `ORIGINAL` and `SHAM_BENIGN_EDIT` returned. It is
eligible for the clean-regression denominator exactly when `ORIGINAL` is `ACCEPTABLE`, and it is a
regression exactly when the paired sham is not `ACCEPTABLE`. A task is denominator-eligible when
it has at least one such eligible paired call across its frozen clean cases, and its binary clean
regression event is one if **any** eligible pair regresses. Thus every task contributes at most
one Bernoulli observation to the exact interval:

- at least 29 distinct clean tasks must be denominator-eligible or the model stratum is `INVALID`;
- the task-level `Original acceptable -> Sham non-acceptable` event rate must be at most 5%;
- its one-sided 95% exact Clopper-Pearson upper bound must be at most 10%;
- on intervention cases, the 90% CI for `SHAM_BENIGN_EDIT - ORIGINAL` must lie wholly within
  `[-0.10, +0.10]`.

The intervention-case sham contrast uses the same nested complete-pair estimator: average paired
sham-minus-Original outcomes within case, then non-empty cases within task, then tasks. Its 90%
interval is the 5th and 95th percentiles of 50,000 task-cluster bootstrap draws with seed
`2026082603`, using the frozen linear quantile convention above.

For MAI, use the same paired-call eligibility and task-level binary regression event:

- at least 5 distinct clean tasks must be denominator-eligible;
- clean regression point rate must be at most 10%;
- there must be no `STABLE_REGRESSION` on a clean case.
- on intervention cases, the task-weighted absolute
  `SHAM_BENIGN_EDIT - ORIGINAL` point difference must be at most 0.10, with no stable sham rescue
  or stable sham regression.

The smaller MAI task count does not support a confirmatory equivalence interval. Sham failure is
an experiment-validity failure, not evidence that misleading history is harmless.

## 8. Quantitative G1 decision

All integrity gates must pass first:

- zero unresolved references and zero hash mismatches;
- zero future evidence leakage into gold or transformations;
- 100% equality of non-history provider-visible fields;
- Qwen and MAI intervention/clean sample minima;
- missingness, repeatability, and clean/sham gates;
- no generated action executed and no response reused as input.

An intervention arm passes Qwen only if all hold:

- task-weighted gold-alignment Delta is at least +0.10;
- Holm-adjusted one-sided paired p-value is below .05;
- simultaneous one-sided lower confidence bound is above 0;
- `STABLE_RESCUE` appears in at least 3 distinct tasks;
- complete-pair and worst-case missing sensitivities both retain positive direction and at
  least +0.10 effect.

The same arm replicates on MAI only if all hold:

- task-weighted Delta is at least +0.10;
- an exact one-sided task-level paired sign-flip p-value is below .10;
- the one-sided 90% task-cluster bootstrap lower bound is above 0;
- `STABLE_RESCUE` appears in at least 2 distinct tasks;
- stable regressions do not outnumber stable rescues;
- all MAI integrity and clean gates pass.

The MAI confirmatory lower bound uses the same case-within-task nested estimator and the 10th
percentile of 50,000 task-cluster bootstrap draws with seed `2026082604`, again using the frozen
linear quantile convention.

Final classification is exactly one of:

- `PASS`: the same arm passes Qwen and replicates on MAI;
- `NULL`: the experiment is valid, but no same arm passes both strata;
- `INVALID`: an integrity, sample, missingness, nondeterminism, or clean/sham gate fails.

`PASS` supports a causal **next-action** finding only for the tested frozen checkpoints and model
revisions. `NULL` preserves the observational contribution but blocks a causal runtime-gate
claim. `INVALID` is not evidence of no effect.

## 9. Pre-treatment admission and exclusions

G1.6 admission is a pure function of the G1.1 candidate registry and content-hashed G1.6
gold/transformation bundles. Include every valid candidate, but keep execution disabled until the
independent G1.7 preflight seal. Admission minimums are:

- Qwen intervention: 30 cases across at least 25 tasks;
- MAI intervention: 8 cases across at least 5 tasks;
- Qwen clean: 30 cases across at least 30 tasks;
- MAI clean: 8 cases across at least 7 tasks.

The thresholds are operationally linked to the frozen gates rather than chosen after replay:
with zero task-level clean regressions, 29 Qwen denominator-eligible tasks give a one-sided 95%
Clopper–Pearson upper bound of about 9.82% (30 give about 9.50%), making the 10% bound testable;
five non-zero MAI task contrasts give a smallest exact one-sided sign-flip p-value of 1/32; and
the 25-of-35 Qwen intervention-task floor prevents a few repeated-decision tasks from dominating
the primary estimate. Case minima additionally preserve decision-level coverage.

All exclusions use the protocol's closed reason-code list and are frozen before the first
treatment response. A curation exclusion is valid only when the corresponding same-unit,
same-input independent review ledger resolves to `EXCLUDE` with that exact reason; a mechanical
exclusion is valid only when the pinned validator reproduces that exact failure code. The
admission receipt has separate included and excluded branches, uses `NOT_APPLICABLE` rather than
fabricated passing checks, and records that the exclusion reason itself was validated. Results
never change eligibility, gold tolerance, action parser, missingness, or thresholds.

Admission also requires complete pre-cutoff curator inputs: action-gold contains both exact
task-instruction and current-GUI (`target_pre`) evidence, while transformation contains exact
`source_history` evidence. Delimiter repair is validated per arm after applying that arm's full
edit; a non-empty `MASK_CORRECTION` insertion cannot inherit syntax deletion that was valid only
for an empty `MASK` record.

## 10. Inference scope

The case census is a frozen, explicit-uptake natural-decision population, not a probability sample,
and the hash-balanced arm schedule is deterministic rather than a randomized sampling design.
The sign-flip p-values and cluster-bootstrap intervals therefore describe model-based/resampling
uncertainty for the frozen task clusters; the sign-flip test additionally relies on task-level
effect symmetry/exchangeability under its null. They are not design-based population inference
and do not justify extrapolation to all GUI tasks, other model checkpoints, or a deployment
distribution. Each source trajectory supplies one observed decision state, and the accepted
next-action endpoint remains a surrogate rather than task success. The sample minima are
pre-treatment feasibility and cluster-diversity floors, not an outcome-informed power guarantee;
in particular, MAI's five-to-seven-task exact test and cluster bootstrap have coarse resolution.
A valid `NULL` may therefore reflect an effect below the frozen gate or limited power and must not
be interpreted as equivalence or evidence that misleading history is harmless.
