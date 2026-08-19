# Online Experience Validation: Detecting and Correcting Harmful Reused Experience During Agent Execution

*Research proposal — v3 (internally reviewed), August 2, 2026. Target: ICLR 2027 (abstract Sep 18, paper Sep 25, AoE). Working system name: RECANT (placeholder).*

---

## 中文摘要(给组会用)

Coding agent 现在普遍会把过去任务攒下的"经验"注入上下文当参考(学术界:SWE-Exp、SWE-Bench-CL 一条线;工业界:Devin Knowledge、CLAUDE.md、Cursor rules)。但经验的好坏不是它自身的属性,而是"经验 × 当前任务 × 代码库当前状态"这个组合的属性——而现有系统的把关集中在存、取、任务结束后三个时刻,**执行中途经验基本被无条件信任**。"经验会把 agent 带偏"已被 2026 年多篇实证论文坐实,但没有系统在执行中把失败**归因**到具体经验并当场处理。我们提出**在线经验验证**:把注入的经验拆成可核对的说法,执行中用程序信号(实体绑定失败、预言落空、归因到经验的失败循环、原地打转)持续对账,把失败归因到具体某条经验(区分"赖经验"还是"赖 agent 自己"),分级处理(标注→改写→降级→撤掉),任务后把"在什么条件下被推翻"写回库。贡献按序:① OEV 问题的形式化(对象:经验本身;时机:执行中;闭环:检测→归因→干预→写回);② 一个带实测有害标签的公开经验测试床 + 首个把"检测的价值"和"处理的价值"分开量的五组因果实验;③ 第一个实例化完整闭环的机制。验证在 SWE-bench Verified 上按时间切分:每个库较早的题蒸馏经验库(用开源 SWE-Exp 管线),较晚的约 200 道题做评测;坏经验用旧 commit 蒸馏自然造出,每条的有害性用同模型、n≥8 的配对实验实测打标。最小版本纯程序信号 + API 模型,**不需要 GPU**。冲 ICLR 2027,8 月 25 日设 go/no-go 闸门,ICML 2027 为后备。

---

## 0. Contributions (ranked, used verbatim throughout)

1. **Problem formalization.** Online Experience Validation (OEV): validating *reused experience* against *unfolding execution evidence*, with per-experience attribution, graded in-episode intervention on the guidance itself, and store write-back — formal I/O, harm definition, and constraints in §1. (The *phenomenon* that experience misleads is prior work and is cited as motivation; the *problem formulation* — its object, timing, loop, and constraints — is ours.)
2. **A labeled testbed and the first causal decomposition of experience-validation value.** A released experience store over SWE-bench Verified with counterfactually measured harm labels, three misleading-experience generators, and a 5-arm evaluation separating detection value from correction value from removal value — no published evaluation isolates these (existing work is memory-on vs memory-off).
3. **A mechanism instantiating the full loop** — claim decomposition, execution-grounded sequential evidence accumulation with asymmetric thresholds, provenance-based attribution with a self-blame bucket, a graded guidance-level correction ladder, and negative-example write-back.

## 1. Problem definition

**Setup.** A base agent A solves task q (a repository issue) in environment Env, producing trajectory τ = (a₀, o₀, a₁, o₁, …). An experience store M retrieves E = {e₁…eₘ} — workflows, insights, prior-fix summaries distilled from earlier tasks — injected into A's context at t=0.

**Definition (harmful experience).** e is *harmful* for (q, A) iff P[success | inject e] < P[success | no e]. Two properties make this the crux:

1. **Validity is a property of the (experience, task, current-repo-state) combination, not of the experience.** The same e can help one task and poison another (documented for cross-repo memory transfer). Write-time and retrieval-time validation are therefore *structurally* limited: before execution, only text similarity is observable — and surface similarity is exactly how mis-transferred experience disguises itself. Execution is when the evidence that adjudicates the combination comes into existence.
2. **Harmfulness is counterfactual and unobservable at runtime.** It can be measured by paired runs (how we obtain ground-truth labels), but the running agent never sees it. The research question: *what observable execution evidence proxies for it, and how much evidence justifies acting?* This property also constrains prior art: any attribution method that needs counterfactual replays (e.g., MemAudit-style post-hoc auditing) is structurally unavailable mid-episode — a point central to our novelty defense (§3).

**The OEV problem.** At each step t, given (q, E, τ_t), output per-experience validity assessments and interventions d_t(e_i) ∈ {keep, annotate, revise, demote, revoke}, plus an end-of-episode store update, maximizing task success subject to a false-invalidation constraint on benign experience. Three sub-problems: **detection under indirection** (harm inferred from symptoms, none individually conclusive); **attribution** (bad guidance vs the agent's own error vs environment noise — mid-episode, without counterfactual replays); **sequential abandonment** (how much contrary evidence justifies acting, given that falsely invalidating good experience is the costlier error — the over-rejection pathology our StepReflect work quantified for step verifiers).

**Effect framing (three outcomes).** Bad experience shifts probability mass between clean success → success-after-costly-detours → failure. Two separable claims with separate metrics: **rescue** (failures → successes; resolve-rate deltas) and **detour-shortening** (on commonly-solved instances: wasted steps/tokens, detection latency, recovery time). Plus a safety bound: on an all-benign store, the monitor must not tax clean reuse.

## 2. Why now

**The phenomenon is established; the in-episode loop is missing.** Agents "experience-follow" flawed records even against contradicting observations (ACL 2026, arXiv:2505.16067); condensed experience causally misleads across 4 frameworks / 13 backbones / 9 environments (ICML 2026, arXiv:2601.22436); cross-domain memory reuse degrades coding agents via superficially-similar-but-wrong memories (arXiv:2604.14004); retrieval-time screening provably misses (MemoryGraft, arXiv:2512.16962); models detect invalidated memories only 55.2% of the time by judgment alone (STALE, arXiv:2605.06527).

**The setting is real and industrial.** Academic: SWE-Exp (arXiv:2507.23361, open source, 73.0% Pass@1 on Verified with Claude 4 Sonnet), SWE-Bench-CL (arXiv:2507.00014), Learn-by-Interact, SWE-MeM, EET. Industrial: Devin's Knowledge feature, Claude Code's CLAUDE.md/auto-memory, Cursor rules, OpenHands microagents — all inject stored experience into coding agents with no in-execution validation, and stale knowledge misleading the agent is a recognized user complaint.

## 3. Positioning and novelty

Validation of reused experience today concentrates at write time (ExpeL, AWM, Voyager, Mem0, SWE-Exp's bank construction), retrieval time (AutoGuide, Self-RAG/CRAG-style critics, poisoning screens), and post-episode (Reflexion, AutoManual, ReMe, Live-Evo, ReasoningBank). Two systems do act at execution time — AgentRR runs pre-specified check functions during experience *replay*, and ExpWeaver gates *whether to consult* experience per step — so we claim no categorical first. **Our claim is the specific conjunction no system provides: evidence-accumulating detection + per-experience attribution + graded intervention on the guidance itself + store write-back, all within the episode.**

| Neighbor | What it does | Missing vs the conjunction |
|---|---|---|
| AgentRR (2505.17716) | Check functions during replay — same timing cell as us, different mechanism | Checks are pre-specified precondition gates; no evidence accumulation, no attribution, no revision, no write-back |
| ExpWeaver (2605.07164) | Per-step gating of whether to consult experience | Prospective only; never audits consulted guidance against outcomes |
| Doctor-RAG (2604.00865) | Mid-trajectory diagnose-and-repair (agentic RAG) | Repairs the trajectory; no persistent experience object; no write-back |
| Live-Evo (2602.02369) | Online downweighting of stale experience | Between-task outcome feedback; no intra-episode detection or intervention |
| AlphaOPT (2510.18428) | Narrows experience applicability conditions | Offline, cross-task, solver-labeled domain |
| SWE-PRM (2509.02360) | Runtime course-correction for SWE agents | Corrects the trajectory; no experience object, no attribution |
| SWE-Exp (2507.23361) | Experience bank incl. failure experiences for coding | Curation offline between tasks; injected experience trusted unconditionally |
| MemAudit (2605.23723) | Causal attribution of harm to memory entries | Post-hoc: requires counterfactual replays unavailable mid-episode; adversarial framing; delete-only |
| STALE (2605.06527) | Benchmarks "does the agent know memory is invalid" | Declarative dialogue memory; measurement only — its 55.2% argues *for* us |

**The combination attack, answered structurally and empirically.** The predictable objection is "AgentRR + ExpWeaver + Doctor-RAG + Live-Evo + MemAudit = this paper." The structural answer is §1 property 2: the strongest attribution method in that list (MemAudit) *cannot be composed into an episode* because its causal evidence — paired counterfactual runs — does not exist until the episode is over; mid-episode attribution must be built from a different substance (provenance linkage + effect mismatch), which is our mechanism. The empirical answer: the baseline suite (§5) includes proxies for each neighbor — an AgentRR-style precondition-gate arm, a per-step LLM-critic arm, a SWE-PRM-style trajectory-correction arm, a Live-Evo-style between-episode reweighting arm — so the deltas are measured, not asserted.

**Relation to the runtime-layer paradigm (ECLoop, Ledger — external work; StepReflect — ours).** These share our shell: wrap an unmodified agent, convert free-form context into structured checkable units, intervene at step boundaries. The trust direction inverts: ECLoop audits the agent against conditions compiled from the authoritative issue (conditions presumed true); Ledger tracks freshness of the agent's own first-hand observations (records presumed true, only aging); we audit *second-hand injected text* (presumed suspect) against first-hand evidence. First-hand observation is the witness; injected experience is the defendant; the monitor is the court — Ledger-style bookkeeping is our evidence registry, not our contribution. This framing stands self-contained; it does not depend on those papers' specifics.

**Self-RAG-collapse defense.** Our detector **measures** (guidance implies predicted effects; execution returns observed effects; mismatch accumulates) rather than **judges**. The deterministic-only variant is reported in every table; the per-step LLM-relevance critic runs at token-matched budget and must lose — if it merely ties, the honest headline becomes "equal accuracy at ~10× lower cost and lower false invalidation."

**Ownership note.** Only StepReflect is our group's work. ECLoop/Ledger are under-review external papers shared by the advisor: we cite the paradigm and reimplement generic components (output parsing, AST lookup, change counters) — standard engineering, no proprietary dependence. **Action item: all nine neighbor full texts re-read and every table cell re-verified before the Sep 18 abstract** (current characterizations partly derive from abstracts).

## 4. System design

A runtime layer wrapped around an unmodified agent, interposed **synchronously at step boundaries** (parse observation → update state → possibly rewrite the guidance block → next model call sees the result). Deterministic signals cost milliseconds against multi-second model calls; everything is logged and replayable.

**Two data structures.**
- **Claim table**: each injected experience compiled once into atomic typed claims — entity-location, effect-prediction, procedural, applicability-precondition — each with bound entity, predicted event pattern, provenance span, status ∈ {untested, supported, contradicted, unbound}, suspicion score, escalation level. Unresolvable references are **recorded as staleness evidence, not dropped**.
- **Event ledger**: per-step structured records — normalized/categorized command, exit code, test pass/fail sets, file/line read coverage, per-file change counters.

**Five modules.** (1) *Compiler* (once, at injection): LLM proposes claims; program grounds them against the repo (symbol table / call graph / search) — the LLM cannot introduce uncheckable claims. (2) *Observer* (every step): parses output into ledger events; pure program. (3) *Attributor* (every step): links actions to claims — hard rule: an action referencing an entity that appears **only** in the guidance and in no prior read record is guidance-driven; soft rule: agent-cited claim IDs (compliance audited). Unlinked failures accrue to a **self bucket**. (4) *Judge* (every step): four failure-event types (action errors; predicted-vs-observed effect mismatch; entity absent after adequate search; stall/loops under linked steps) feed a per-claim sequential score; hard evidence outweighs soft; **asymmetric double thresholds** price false invalidation above delayed detection; confirmed claims lock *for this episode only* (locks void if the referenced region is later edited — validity never generalizes across tasks, per §1 property 1). Two budgeted probes gated by deterministic triggers: an API-model check for semantic contradictions, and a masked-guidance counterfactual (re-query next action with the suspect span hidden; ≤3/episode). (5a) *Corrector*: graded ladder on the guidance **only** — annotate (with evidence citation) → revise (re-ground renamed entities; must re-compile before re-entry) → demote (to a disputed section) → revoke (+ explicit counter-note; silent removal may not undo anchoring — ablated). Implemented by re-rendering the guidance block at a fixed position at the end of context (cache-preserving). The corrector never touches actions; replanning is the agent's own. (5b) *Write-back* (episode end): revoked/revised claims persist as narrowed applicability conditions with evidence and repo fingerprint. No global good/bad labels (§1 property 1).

**Safeguards**: per-episode intervention caps, cooldowns, default-keep on parse failure, no revocation within the first B steps, full audit log.

**ICLR-minimal version = modules 1–5 with deterministic-only judging** (probes via API models, no fine-tuning) — **zero GPU**. Framed as *sequential evidence accumulation with calibrated asymmetric thresholds* — the design point defended against AgentRR's static gates — not as "learned detection" (the fine-tuned StepReflect-recipe verifier is ICML-version work). The deterministic-only system is also a required reported variant in every table.

## 5. Evaluation design

**Chronological split (leakage-safe by construction).** Group SWE-bench Verified instances by repo, sort by PR merge date (SWE-Bench-CL protocol). The earlier ~60% of each repo ("past period", ~300 instances) exists to *build stores and develop the system*; the later ~40% (**eval split, ~200 instances**) is the main table. Experience flows strictly forward in time (all referenced commits precede the eval task's base commit); no experience derives from the task it is injected into; near-duplicate answer content is screened and reported; results stratified per-repo (django dominance disclosed). **Development quarantine:** E0–E3 (§7) run exclusively on past-period instances (using time-respecting mini-stores from even earlier tasks); one full repo and one misleading-experience generator are additionally held out of all development and reported separately; the full detector (signals, weights, attribution rules, ladder) is **frozen before any eval-split run** — a 50-instance dev subset of the past period calibrates thresholds. The eval split is untouched until frozen main runs.

**Testbed (all public; no in-house infra assumed).** Scaffold: mini-swe-agent v2 primary (literature GPT-5-mini baselines ≈56% calibrate our harness), Codex CLI as transfer if time permits. Models: GPT-5-mini primary; MiniMax-M2.5 secondary (subset). Store distillation reuses the **open-source SWE-Exp pipeline/format** — making the static-reuse arm a published, non-strawman baseline. Store, labels, and generators are released.

**Misleading-experience construction.** Generators: (T1) **version-stale** — the honest pipeline run at an older commit; real intervening refactors invalidate bindings; nothing synthetic. (T2) **mis-transfer** — high-similarity experience from a different repo/bug. (T3) **targeted corruption** — wrong path / inverted order / stripped conditions. One generator is development-held-out (§ above). Contamination rate π ∈ {0, 0.25, 0.5} for dose-response.

**Ground-truth harm labels (fixed protocol).** Same backbone and scaffold as the main arms. Per experience-item: n=8 injected runs vs n=8 no-injection controls (controls shared across items on the same instance), exact paired test with declared ε and FDR; per-item label posteriors published; label uncertainty propagated into detection metrics (AUROC reported under label resampling). A higher-n gold subset audits the rest and is the sole source for the removal-reference arm. Two metric families kept separate: **claim-validity detection** (against directly checkable ground truth — e.g., a stale binding is verifiable from the repo) and **harm detection** (against counterfactual labels).

**Arms** (identical model/prompt/tools/assignments): C1 static reuse (SWE-Exp-style prepend-and-trust) · C2 retrieval-time relevance check (state-conditioned selection + LLM critic at t=0 only) · C3 detection-only (verdicts rendered as annotations; guidance untouched) · C4 detection+correction (full ladder + write-back) · C5 **removal reference** (items excised at t=0 per gold labels). C5 is a reference point, **not an assumed ceiling** — mid-episode revision can salvage partially-valid experience that wholesale removal discards, so C4 > C5 is possible and meaningful. Adversary/floor arms: no-experience; per-step LLM critic (token-matched, designated must-lose); AgentRR-style precondition-gate; SWE-PRM-style trajectory-only correction; Reflexion-style post-hoc retry; Live-Evo-style between-episode reweighting (sequential setting). **Placebo controls** (the arms' own logic demands they lose): matched-rate random-revocation and random-annotation — separating detection quality from the effects of prompt surgery and context shortening; per-arm guidance-token counts reported.

**Pre-registered primary contrast:** C4 vs C1 under contamination (π=0.25), paired per instance, 3 seeds, exact McNemar — powered via a pilot discordance estimate from E1. The full ladder ordering (C1<C2<C3<C4) is reported descriptively with CIs, not as a family of significance claims. **Do-no-harm bound:** at π=0, C4 within 3pp of C1, certified by paired bootstrap over instances × 3 seeds (a 1pp margin is below measurement resolution at n≈200).

**Metrics.** Rescue: Pass@1; RRU (relative reduction in unresolved instances, i.e., ΔPass@1 / (1 − baseline Pass@1)); paired recoveries/regressions. Detour: computed by a **single frozen shadow-mode instrument replayed identically over every arm's logs including C1** (the attributor cannot be both component-under-test and measuring device): wasted guidance-linked steps/tokens on gold-harmful items, detection latency (first linked failure → first guidance-modifying escalation; annotate does not count), recovery time. Detection: claim-validity P/R; harm-detection P/R/AUROC with label-uncertainty bands; **false-invalidation rate = revoke-or-demote events per benign experience-item-episode, ≤5%**. Economics: cost, validator overhead, cost-per-resolved; critic comparison via matched total token budget with full cost curves. Compounding (sequential arm): pass-rate trend across stream position, write-back on/off.

## 6. Ablations (100-instance subset of the eval split, π=0.25, 3 seeds; run after freeze)

Signal-family knockouts (expected alignment: grounding-failure ↔ T1/T2, effect-mismatch ↔ T3, loops/stalls ↔ vague experiences — reported per generator including the held-out one); deterministic-only vs +API-probe; **attribution off** (blame-by-proximity); **claim decomposition off** (blob-level trust score); ladder granularity (annotate-only / revoke-only / full; revoke-silent vs revoke-with-counter-note); threshold sweep (false-invalidation vs detection-latency Pareto — the abandonment-timing figure); write-back on/off; symmetric vs asymmetric thresholds (predicted to reproduce the over-rejection pathology at the guidance level).

## 7. First experiments (all on past-period instances; eval split untouched)

- **E0 (Aug 3–14): stand up the public harness.** SWE-bench eval environment + mini-swe-agent on an x86 CPU docker machine (≥16 cores / 64 GB / few hundred GB disk); reproduce baseline on 10–20 instances against literature numbers; clone SWE-Exp, run distillation, adapt store format for injection.
- **E1 (Aug 14–21, ~$800 + ~$400 labeling): establish the harm.** 100 past-period instances × {no-experience, clean-reuse, contaminated-reuse (T1+T2)}, 3 seeds, provenance tagging on. First days may use the partial store while E0 finishes (explicit pipelining). Deliverables: per-generator harm effect (target ≥5pp paired drop), experience-following rate, inert fraction, harmed trajectories with full logs, pilot discordance rate for power analysis.
- **E2 (Aug 18–24, ~$100): offline signal detectability.** Replay E1 logs through observer/attributor; four deterministic signals post-hoc; AUROC separating gold-harmful from benign, time-to-first-signal, false-positive rate on clean trajectories; per-step LLM critic head-to-head on the same logs (the $100).
- **Go/no-go gate: Aug 25** (full E1+E2 in hand: ≥5pp harm and detectable signals). Pass → ICLR sprint; fail → iterate injection salience *on past-period data only*, retarget ICML 2027.
- **E3 (Aug 25–Sep 2, ~$500): minimal closed loop.** Best signal + revoke-with-counter-note (budget 2) wired into mini-swe-agent; harmed past-period subset under annotate-only / revoke / gold-removal — the miniature C3/C4/C5 contrast. Detector frozen at completion.

## 8. Timeline & resources (ICLR 2027)

Sep 2–12: frozen main runs — C1/C4/C5 on the full ~200-instance eval split × 3 seeds; C2/C3, adversary and placebo arms, and ablations on the 100-instance subset. Sep 8–12: re-verify all neighbor full texts (novelty sweep rerun). Sep 12–18: analysis, figures, complete draft → **abstract Sep 18**. Sep 18–25: polish, appendix, stragglers → **paper Sep 25**. arXiv preprint as soon as E3 lands, regardless of venue. Fallbacks: ICML 2027 (~late Jan; adds fine-tuned verifier, full compounding study, Codex transfer) → ICLR workshops (~Feb 2027).

**Resources: no GPU.** API budget ~$1.8k for E0–E3 (incl. labeling); ~$7–9k total (labeling n=8 protocol and 3-seed main arms are the increases; control-run sharing keeps labeling affordable). One x86 Linux docker machine (CPU). The schedule has essentially no slack; the Aug 25 gate is the protection.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Injected harm doesn't reproduce | E1 gate before system building; raise salience/contamination on past-period data only; report harm-vs-model-strength as a finding |
| Label noise corrupts detection metrics | n=8 same-backbone protocol, published per-item posteriors, uncertainty-propagated metrics, gold subset audit |
| Development leaks into evaluation | Chronological quarantine (E0–E3 on past period), held-out repo + generator, detector frozen before eval-split runs |
| False invalidation taxes clean runs | FIR ≤5% (defined denominator); asymmetric thresholds; caps/cooldowns; 3pp bootstrap-certified do-no-harm bound |
| Prompt-surgery confound | Random-revocation/annotation placebo arms at matched rates; guidance-token accounting |
| "Synthetic strawman" objection | T1 fully natural; sequential arm zero-injection; measured labels; per-generator reporting incl. held-out generator |
| Attribution confound | Only-in-guidance entity rule + self bucket; masked-guidance probe; manual audit of ~50 invalidation events |
| Combination-novelty attack | Structural argument (counterfactuals unavailable mid-episode) + neighbor-proxy baselines measured empirically |
| Self-RAG-collapse | Deterministic signals carry detection; token-matched critic baseline; honest pivot if tied (equal accuracy, ~10× cheaper) |
| Concurrent work | Early arXiv; Sep novelty re-sweep; moat = attribution mechanism + labeled released testbed |
| New stack, no in-house coding infra | E0 explicitly scheduled; SWE-Exp open pipeline replaces from-scratch distillation; gate failure → ICML without drama |
| Smaller-than-planned eval n (~200 not 500) | Consequence of leakage-safe chronological design; addressed by pairing, 3 seeds, pre-registered single primary contrast |

## 10. Phase 2 (ICML/journal version): GUI transfer

Port via our own stack — CoAdapt-GUI as substrate, StepReflect as the effect-mismatch detector, accessibility tree as the grounding index, app-version drift as natural staleness. MAGNET (arXiv:2601.19199, between-episode memory evolution under UI drift) must be differentiated; the GUI section is a transfer study, not a second core contribution.

---

*Caveats: 2025–2026 arXiv characterizations partly derive from abstracts/snippets (Jul 30–Aug 2, 2026); §3's action item schedules full-text verification before the abstract deadline. ECLoop/Ledger are anonymous under-review PDFs — cite public versions if/when available, otherwise describe techniques generically.*
