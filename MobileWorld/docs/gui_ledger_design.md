# Qwen GUI Ledger

This first version implements a deterministic execution ledger for Qwen GUI-only
MobileWorld evaluation. It adds no model call. The existing Qwen actor remains
responsible for interpreting the task, reading the current screenshot, and
choosing the next action.

## Relationship to Ledger

[Ledger, sections 3.1–3.4](https://arxiv.org/html/2608.00808#S3) maintains execution
state alongside history and uses it at two boundaries: Inform before the actor
call, and Govern before action execution. Inform is regenerated for each request;
it is not appended to persistent history. Govern's Nudge executes the command
and augments the returned observation. Its Reuse outcome instead skips execution
when an earlier read result is provably reusable.

We preserve those two boundaries and Nudge semantics. The GUI adaptation uses
existing screenshot observations and structured executed actions in place of
file reads, edits, and shell commands. It does not implement Reuse: equal GUI
pixels do not establish that hidden application state or a previous tool result
is still valid. The paper does not publish an exact Inform template or numerical
policy thresholds; this implementation's wording and thresholds are local
choices, not claimed verbatim reproduction.

## Scope and configuration

```bash
mw eval --agent-type qwen3vl [ordinary evaluation options] --gui-ledger full
```

| Mode | Inform | Govern | Extra model calls |
| --- | --- | --- | --- |
| `off` (default) | Absent | Absent | 0 |
| `inform` | Fresh temporary state view | Absent | 0 |
| `full` | Fresh temporary state view | Allow or Nudge | 0 |

Enabled modes accept only the registered `qwen3vl` actor and GUI-only tasks.
`--enable-mcp` and `--enable-user-interaction` are rejected before evaluation
resources start. Ledger has no secondary model, credential, provider client,
vision inference, OCR extraction, semantic progress judge, or history editor.
The retired `--sentinel*` options are not accepted.

### Optional UI-tree state observations (2026-10-06; independent rules revised 2026-10-07)

The UI-tree extension is a separate opt-in, not a change to existing arms:

```bash
mw eval --agent-type qwen3vl [ordinary evaluation options] --gui-ledger full --gui-ledger-ui-tree
```

It also supports `inform`; combining the option with `off` is rejected. With
the option absent, no UI-tree request is made and the existing pixel-only
Inform/Full behavior remains unchanged. No extra model call is added. The
component names for this extension are **State Tracker**, **State Reporter**
(the existing Inform boundary), and **Action Check** (the existing Govern
boundary); the Python methods retain their existing names.

#### Acquisition and call sites

The audited runner creates one `GuiLedger(..., ui_tree_enabled=True)` per task
attempt. After the normal initial screenshot, it calls
`AndroidEnvClient.get_ui_tree()` once. Each normally returned `execute_action()`
already provides the next screenshot; the runner then obtains one new tree.
That post-action tree is reused as the next step's pre-action sample. Terminal
actions and failed executions do not trigger a post-tree request. An incomplete
Collector chain disables Ledger and further tree acquisition. Optional tree
failure alone does not disable the actor or the pixel-only observation path.

`GET /ui_tree` is separate from the existing `/xml` interface, whose legacy
retry policy is unchanged. It never initializes a device. A busy device or
missing initialized controller returns an unavailable sample immediately.
The controller performs one UI Automator dump to a fresh UUID-named file under
`/data/local/tmp`, reads at most 512 KiB + 1 byte, rejects oversize output, and
attempts to remove only that file. Dump/read/cleanup subprocess waits have
6/1/1-second limits; the dump also has a device-side 5-second kill timeout.
No XML file is intentionally retained on the backend. Cleanup can fail when
the device/ADB is unavailable; this does not justify broad deletion or a retry.

The client uses a 12-second budget, bounded streamed response size, no redirects,
and a closed device/source/status envelope. Socket waits are bounded but are
not a strict wall-clock deadline: an in-flight socket read can overshoot the
remaining budget. All failures return fixed reason codes, not exception text.
No request to the new endpoint is made by default.

The plain acquisition result has this shape:

```json
{"status":"ok","source":"uiautomator","xml":"<hierarchy ...>...</hierarchy>","reason":null}
```

Unavailable samples have `status="unavailable"`, `xml=null`, and a closed reason
such as `unsupported_endpoint`, `device_busy`, or `capture_timeout`.

The screenshot and XML are sequential samples, **not an atomic snapshot**.
Old backends without `/ui_tree` degrade to unavailable; they do not gain tree
support merely because the client option is enabled. No live availability or
task-effectiveness result is established by the CPU implementation tests.

#### State Tracker: input, operation, output

`observe(step, screenshot, ui_tree=sample)` parses the current sample and rebuilds
its bounded current facts. A fact does not require the action's old target to
survive a page transition. `record_transition(step, action, outcome=...,
screenshot=..., decision=..., ui_tree=sample)` runs independent rules against
the pre-action sample, actual structured action and post-action sample. There
is no overall UI `SAME / CHANGED / UNKNOWN` classification: a target can be
absent or ambiguous while other rules still record concrete observations.
Screenshot comparison remains a separate sampled-pixel measurement.
The parser accepts at most 512 KiB, 2,048 nodes, depth 32, and 4,096 characters
per attribute. It rejects malformed XML, DTD/entity declarations, NUL-encoded
declaration bypasses, and invalid hierarchy structures. Missing boolean fields
are unknown, never implicitly false.

**Current facts and their anchors.** Each selected fact has a sample-local
`node_ref`, such as `n0`. This reference identifies an exposed node in this
sample, not a widget across time. When a suitable labelled ancestor exists,
`scope_ref` and `scope_label` refer to that same nearest described ancestor,
which is neither private nor editable. Otherwise `scope_ref` falls back to the
sample-local parent reference without inventing a label. These references keep two identically
labelled controls distinguishable within an observation without claiming that
their business identities are known.

When available, a separate cross-sample anchor describes the node's package,
resource ID, class, and ancestor ID context. Bounds and mutable values are not
part of that anchor. It is usable only when the description is unique in the
respective samples. Nodes with missing IDs, or duplicates that this context
cannot disambiguate, still contribute current facts but do not support
cross-sample matching. A selector match is not proof of business-object identity,
and no whole-tree hash is treated as complete environment-state identity.

The display label prefers `content-desc`, then `text`; editable text is a value,
not a label. Known properties are retained independently: a missing field does
not discard other known properties. `checked` is meaningful here only for a
node explicitly marked `checkable=true`; known `selected`, `enabled`, and
`focused` flags are reported independently for selection, actionable, editable,
or selected controls. An editable, explicitly
non-password node can supply a bounded current input value. Password=true,
unknown password status, or a private ancestor suppresses node text; this is a
privacy guard, not a general guarantee that all public UI text is non-sensitive.

Facts are selected deterministically, within 24 rows total. Focused editable
controls, selection controls, selected controls, and disabled actionable
controls are the high-priority groups (priority levels 0 through 3, not a
three-row limit). Subject to their space requirements, up to six slots are
reserved for plain static labels with no reported control properties. These
labels are grouped by `scope_label`, or otherwise by package, and taken in
round-robin order so one group does not consume every reserved slot. Within
each group, `TextView` roles come first, then nodes with nonempty `text`, then
XML order. This uses exposed structure, not task keywords; a nonempty text
field alone does not establish that an image node is body text. Remaining
slots follow the normal priority order, including labelled actionable and
scrollable controls. This keeps a button-heavy tree from automatically taking
every slot intended for a heading, status label, or other exposed text. The
final character budget can still omit rows.

Each label or value is bounded to 96 characters. The complete bounded fact
index remains available internally for comparison, so report truncation does
not look like a node disappearing. A separate complete raw-selector presence
set also tracks descriptions whose facts are private, ambiguous, or not
reportable. It prevents a change from unique to ambiguous, or a missing
reportable field, from being counted as disappearance. These are bounded views
of the exposed hierarchy, not claims that unobserved nodes do not exist.

**Fact changes after execution.** Only two available samples can support
comparison. Uniquely anchored nodes can yield explicit attribute changes,
plus counts of anchored descriptions newly observed or not observed in the
later sample. At most six changed facts are retained per comparison. Both
samples must expose a property to compare it; truncated input values are not
compared as exact text. A literal display-label change is reported only when
both matched facts have a label and neither label is truncated. It is a string
change, not a judgement about the label's meaning. A missing old target does
not erase the new sample's
current facts. "Not observed" does not mean deleted, hidden permanently, successful
navigation, or task completion. Neither temporal succession nor an executor
return proves that the action caused a sampled change.

**Independent rule records.** Each execution produces six rule results. Each
result has a fixed rule name, one status, an optional fixed reason, and bounded
scalar measurements. `RECORDED` means this rule measured its specified data;
zero changes are a valid measurement, not a claim of no task progress.
`NOT_APPLICABLE` means the action is outside that rule's scope. `UNAVAILABLE`
names the missing sample, ambiguous selector, private value, or other unmet
data requirement. One rule's unavailable result never erases another rule's
recorded facts. None of these statuses means action success or failure.

| Rule | Input and operation | Recorded output |
| --- | --- | --- |
| `current_sample` | Read the post-action tree independently of the previous target | Exposed node/fact counts; current bounded facts remain available to Reporter |
| `node_presence` | Compare uniquely scoped anchors in both available samples, consulting complete raw selector presence | Matched, newly observed and no-longer-observed anchor counts |
| `field_changes` | Compare known booleans, bounds and untruncated public text/description for anchored facts | Matched-node and changed-node/field counts; a separate bounded projection keeps at most six detailed property/label changes |
| `action_target` | Find candidates from the original click coordinates or focused input; if unique, match its anchor in the later tree without re-targeting at old coordinates | Candidate count, matching/presence result, compared/changed field counts and bounds change |
| `input_value` | For text input, compare eligible full bounded public text with actual requested text; if the original field cannot be matched, separately inspect a unique current focused field | Available literal equalities with an explicit matched-field/current-field basis; cross-time text change only for a matched field |
| `scroll_region` | For a valid drag/swipe contained in one uniquely identifiable scrollable region, compare its exposed descendants | Unique literal-content matches and moved entries; complete public scopes additionally support content appearance/absence and regional sample equality |

Missing target ID or an overlapping coordinate hit does not prevent presence
or field rules elsewhere in the tree. A page jump may therefore yield a
no-longer-observed target plus new anchors and new current facts, instead of an
overall `UNKNOWN`. Ordinary drags are not automatically called scrolls: the
scroll rule needs suitable geometry and a unique exposed scrollable container.
Node displacement or newly exposed content is a sample measurement, not a
proof that a gesture was delivered or caused the change.

The input rule's `CURRENT_FOCUSED_FIELD` basis is specifically a current-state
observation, not a re-identification of the earlier input recipient. An input
equal to the requested string does not establish that the current action
produced it. Scroll matching uses unique literal public text/description within
the selected container, not recycled row IDs as business-object identifiers;
duplicate content is not guessed into a unique row match. Incomplete/private
scope content suppresses content-added/removed and scope-equality claims.

**Action-scoped observations for repetition.** Repetition checks use a bounded
projection of the uniquely selected target's immediate containing-parent
subtree, or the target itself when it has no parent. The selected target anchor
is bound separately, so two siblings sharing the same parent do not become the
same action scope. Scroll actions instead use the uniquely anchored scrollable
container's subtree. This replaces the former all-nine-attributes target gate
and avoids comparing a button alone while adjacent status/content changes.
The projection includes exposed descendant structure, known attributes, field
presence and bounds. Missing unrelated boolean fields remain missing instead of
being fabricated as false; a known field becoming missing changes the
projection. Required geometry or public text/description that is missing,
private or truncated prevents an equality claim. The same scoped anchor must
be found in the later sample; moving the target does not reassign its identity
to another node at the old coordinates. Repeated selectors do not establish
business-object identity, and subtree equality is not full environment-state
equality.

#### State Reporter: bounded facts, not raw XML

`render_inform()` adds current tree availability and counts, bounded current
facts, and at most the last three historical rule records. Each rule reports its
own measurements or specific data gap, without an overall UI comparison label.
Historical rule records are newest first so the character budget favors recent
evidence. The facts are rebuilt from the current sample on every `observe()`;
a label or value from an
older sample must not be presented as still current. Historical changes are
explicitly marked historical. An unavailable current tree does not cause the
last available facts to be reused as current.

A separate bounded last-observed index admits new entries only from the
selected facts with a usable anchor. It is limited by `max_records` (64 by
default) and keyed by anchor. Existing entries are refreshed from the complete
bounded fact index when matched again, even if this round's report selection
omits them, so a later departure does not resurrect an older retained value.
The reporter may show at
most three retained facts that are not currently observed and whose last-seen
step is earlier than the current step, in a separate `Last observed UI facts`
historical section. This preserves a dated observation after a jump without
asserting that its old value remains true. A missing current sample clears
current facts, not the explicit age of those historical records. Sample-local
IDs alone never qualify a fact for this cross-sample index.

The UI-report section has an 8,192-character upper bound. It uses JSON data
serialization, with angle brackets, ampersands, Ledger section markers, and
Unicode control/format/surrogate/line-separator/paragraph-separator characters
escaped (including supplementary-plane characters via valid JSON escapes).
UI content is explicitly identified as untrusted observation data, not
instructions. The limits can omit facts, so actor output must not treat this as
an exhaustive interface inventory. No raw XML is passed to the actor. Unlike
the initial UI-tree implementation, the revised reporter intentionally passes
bounded labels and eligible input values; it is no longer a counts-only report.

For example, the following is an **abbreviated explanatory rendering of a
synthetic fixture**, not a verbatim prompt or a real-run result:

```text
Current sample:
- n3, label="Sunday", checked=false
- n4, label="Saturday", checked=true
Historical comparison after attempt step 1:
- matched Saturday selector: checked=false -> true
```

The current Sunday fact requires no match to a prior action target. It says
only what this sample exposed; it does not infer that the task requests both
days, recommend a click, or declare the alarm saved. The original actor remains
responsible for relating these facts to the task. This temporary report is
regenerated for each decision and is not appended to persistent history.

#### Action Check: scoped repetition, unchanged execution

Before executing a proposed action, `govern()` checks the action against
completed executions and comparable action-scoped observations. Exact action
comparison remains the default; the same-control click exception below uses a
separate advisory comparison key, not a changed normalized execution action. The default
repeat threshold remains two preceding consecutive returned executions plus
the newly proposed action. Each prior scoped observation must remain equal
before and after execution, and the current scope must match those samples.
Anchored node appearance/disappearance in those executions vetoes this simple
repeat rule, even outside the selected subtree; stable button fields alone
must not override an observed change of exposed structure.

**Same-control clicks with different coordinates.** The additional click-only
rule can treat two coordinate clicks as the same repeated interaction when
each resolves to the same unique, explicitly exposed discrete widget class.
The complete class name must equal one of `android.widget.Button`,
`android.widget.ImageButton`, `android.widget.CompoundButton`,
`android.widget.CheckBox`, `android.widget.RadioButton`,
`android.widget.ToggleButton`, or `android.widget.Switch`; a matching class-name
suffix is insufficient. The target must explicitly report `enabled=true` and
`scrollable=false`, and must be a leaf: **any exposed child, even a static one,
disqualifies coordinate equivalence**. This excludes generic
clickable containers and coordinate-sensitive surfaces from this equivalence
rule; simply sharing a label, resource ID, or bounding box is not enough.
The complete public containing-parent subtree (or the target itself if
rootless), selected target anchor and action kind must all match across the
current sample and every compared pre/post sample. Missing, private, truncated,
ambiguous or changed evidence cannot establish that equivalence.
These class attributes are observed metadata, not proof that the application
uses a native implementation or that this is the same business object. There
is no blanket rejection based on a WebView ancestor; all the candidate and
scope conditions still apply.

The new comparison key omits the click coordinate only after those checks.
It does not alter `_action_key`, rewrite coordinates, snap a click to the
control center, replace an action object, or assert which view received the
event. At the default threshold, two contiguous prior returned clicks plus
the current candidate must qualify; anchored appearance/disappearance still
breaks the chain. When the repeated chain includes differing coordinates
matched through this equivalence, the
notice reason is `REPEATED_CLICK_SAME_UI_CONTROL`. Existing budgets and
cooldowns apply, and the original current click still executes unchanged.
The notice reaches the actor only on its next call as execution feedback.

This exception is not used for long presses, double taps, input, swipes,
pixel-based repetition or either cycle rule: those retain exact normalized
action comparison. The UI-tree-disabled and Ledger-off paths are unchanged.
This revision implements only same-control click repetition; a general
previously-visited-state rule is deferred.

The UI short-loop rule separately checks an `A B A B` sequence of connected
complete exposed-UI-sample/action transitions. That rule uses a whole exposed
sample signature to connect different action targets, not the local scope key.
Every node must qualify for the public bounded sample projection; otherwise
the UI cycle rule has insufficient evidence. It can recognize a recurring
transition pattern even when its individual edges change fields. Neither rule
depends on an overall UI comparison status. A partial current fact alone does not satisfy the scoped
equality requirements, and an ambiguous coordinate hit cannot become a known
event recipient merely to issue a warning.

Field-only changes outside a verified action scope, such as a clock, need not
invalidate the simple scope-repeat comparison; changes inside it do. The
separate appearance/disappearance guard above still applies. The screenshot
rules remain, but must not override positive UI changes observed by the applicable rules. A
presence change, field change, measured regional movement, or a changed
complete public sample/scope signature is evidence to
avoid a contradictory pixel-only repetition notice, not a judgement of task
progress. Existing notice budgets and cooldowns still apply.

`ALLOW` and `NUDGE` both execute the **original action object** through the
unchanged host path. A Nudge is received by the actor on its **next** decision,
not by a second call during the current decision. Repeated scoped observations
can be legitimate: the notice explicitly does not establish command failure,
background-state equality, or task failure. No blocking, Reuse, semantic task
judge or Qwen protocol correction is introduced.

#### Evidence, privacy, availability and evaluation

Collector's existing `observation.accessibility_tree` stores the acquisition
payload for both step starts and completed transitions. The raw XML can contain
sensitive on-screen text and is subject to existing Collector storage/access
controls; the opt-in help text explicitly discloses this. The actor observation
does not receive raw XML; the State Reporter supplies the bounded data view
described above. Derived summaries/sidecars retain per-rule status/reason/count
metadata, including `ui_retained_fact_count` for the last-observed index, not UI
labels, values, or raw XML. Collector's actual model
request still records the report that the actor received under its existing
raw-audit controls. Action execution timing is finalized before
the optional post-action tree request; total task wall time still includes
acquisition overhead.

The derived format is `ui_rule_format="independent_observations_v1"`, with
`last_ui_rule_<rule>_status`, `last_ui_rule_<rule>_reason`, and cumulative
`ui_rule_<rule>_<status>_count` fields (lowercase status in count-field names).
The removed UI target-comparison field
is not populated with an artificial replacement score. In UI-enabled reports
and summaries, missing image evidence is named `SAMPLE_UNAVAILABLE`; the
pre-existing screenshot-only arm is unchanged.

UI Automator observes only the hierarchy the app exposes. A custom-drawn view
may expose one node instead of all its internal controls; an app can omit
accessibility information, and password/private or ambiguous nodes cannot
support comparisons. Conversely, a tree may expose off-screen or overlapping
views; a reported node is not proof of visual visibility or foreground focus.
The reporter does not guess which of two overlapping WebViews is active.
See Android's [custom-view accessibility guidance](https://developer.android.com/guide/topics/ui/accessibility/views/custom-views).
No completeness guarantee or bypass of application restrictions is implemented.
Old screenshot-only runs contain no UI-tree evidence and cannot be retroactively
used as real examples of these new attributes.

UI trees are an **additional observation source**. A future experiment needs to
separate that information advantage from the effect of tracking/reporting/checks;
a higher success rate against a screenshot-only actor is not by itself evidence
that the harness logic caused the gain. This option does not start an evaluation.

## Components and their inputs and outputs

| Component | Input | Operation | Output |
| --- | --- | --- | --- |
| Evaluation CLI | Actor, task options, `--gui-ledger` | Validate supported mode; enable Collector | Runner's `gui_ledger_mode` and audit lifecycle |
| Task runner | Current screenshot; parsed action; actual execution outcome | Maintain one ledger per task attempt; call the two boundaries | Temporary Inform, Govern decision, completed transition |
| Deterministic ledger | Task text, observation pixels, structured actions, outcome | Fingerprint observations; retain bounded records; compare repeated interactions | Observation index, change state, action records, Allow/Nudge |
| Qwen adapter | Normal Qwen request, fresh Inform, received Govern notice | Append Inform to this request; retain received notices with execution feedback | Actor request retaining the original task/image and native history plus received feedback |
| Collector | Existing runtime events and the actual model request | Capture ordinary audited execution | Raw audit outside the source tree |

## One step

1. The runner obtains the observation already required by normal execution and
   supplies it to the task's ledger. Ledger never requests an extra screenshot.
2. Ledger renders a compact state view using the task anchor and its current
   observation/execution records. Qwen appends this as a trailing text block
   after the existing current-image block for this request.
3. Qwen performs its usual actor call and parses the proposed action. Inform
   does not enter Qwen's persistent action history and is regenerated next step.
4. In `full`, Govern checks the proposed action against completed interactions.
   It returns Allow or Nudge. In both cases the original action executes through
   the existing executor.
5. The normal execution outcome and subsequent observation update the ledger.
   When Nudge applies, the runner attaches a notice to the returned observation.
   Qwen retains a notice only when it actually receives that observation on its
   next prediction. That received feedback can persist, unlike Inform.

A new task or retried task attempt receives a new ledger. Concurrent tasks do
not share state. Provider and parse retries keep the same already assembled
request and do not advance the ledger. They may make the normal actor retry
calls; Ledger adds no separate model call.

## State and interpretation

Observation identity is derived locally from decoded image dimensions and RGBA
pixels. It is independent of PNG/JPEG container metadata. It means only exact
visible-pixel equality. An unseen, missing, or unreadable observation cannot
support an equality or freshness claim.

Structured actions are normalized from their actual executor fields. Actor
prose, reasoning, action descriptions, and task-completion claims are not
execution evidence. An executor return records a return, not successful
fulfilment of the user's objective. A changed screenshot records a visible
change, not progress toward the task.

Inform exposes the task anchor, current observed focus, and a bounded index of
previous observations and their relationship to the current view. It describes
only mechanically observed conditions. It does not announce subgoal completion,
tell the actor which GUI control to select, delete old history, or correct old
statements. It performs no OCR or semantic date/amount extraction. With the
UI-tree option enabled, bounded literal labels or eligible input values may
nevertheless contain dates, amounts, or other on-screen text.

Inform's command labels use the current Qwen `mobile_use` vocabulary, not the
executor's internal action names. The display-only projection is:

| Executed action (internal) | Inform label |
| --- | --- |
| `navigate_home` / `navigate_back` / `keyboard_enter` | `system_button (button="Home")` / `system_button (button="Back")` / `system_button (button="Enter")` |
| `input_text` | `type` |
| `drag` / `swipe` | `swipe` |
| `finished` | `terminate` |
| `click`, `long_press`, `answer`, `ask_user`, `wait` | Same action name |
| Anything not represented above, including legacy `open_app` | `GUI operation (label omitted)` |

The fixed button discriminator is retained, but coordinates, input text, answer
content, and termination status are withheld. The labels describe past
executions; they are not complete tool calls or instructions to repeat them.
The neutral fallback neither invents a supported action nor declares a completed
execution invalid. The original task anchor is not searched/replaced.

This prevents the ledger-generated command list from advertising internal names
such as `navigate_home` that Qwen's tool protocol does not accept. It does not
relax the parser or guarantee that the actor will never produce an invalid
action. Internal records, exact action keys, Govern comparisons, execution,
and raw Collector events keep their original representations unchanged. This
projection is deliberately Qwen-only, matching the current supported scope.

### Preserve the existing Qwen host

GUI Ledger is an optional harness, not a repair of the underlying Qwen agent.
The existing MobileWorld Qwen system/tool prompts, response parser, action
conversion, retry behavior, and native history-update semantics remain the
baseline in all three modes. With Ledger `off`, neither an Inform block nor
received Ledger feedback is added. Enabled modes retain only the thin hooks
described above: temporary Inform and received Nudge execution feedback.

The earlier independent host-protocol changes have been withdrawn. Ledger does
not remove `Menu` or `time` from the existing prompt, impose stricter required
`text`/`status` validation, or change coordinate conversion and parser retries.
Existing inconsistencies between the host's advertised schema and its execution
support are not corrected as part of this experiment. Ledger adapts its own
display labels to the existing `mobile_use` vocabulary instead of changing the
host to accommodate its labels. Its internal action keys and Govern logic are
unaffected by this display-only projection.

CPU regressions compare OFF requests, parsed actions, retry attempts, and native
history against the pre-Ledger host behavior. Inform is reused unchanged across
the host's normal retries, without advancing Ledger state. Experimental modes
must still use identical host and run configurations for a controlled comparison.

## Screenshot-only Action Check rules and shared bounds

For example, in a CPU fixture with two identical executed clicks and unchanged
sampled images, the third decision receives this Inform (not a real run result):

```text
[GUI Ledger: current execution state]
Task anchor:
Open the requested page
Current focus: sampled observation O1 at attempt step 3.
Observation index (bounded recent sampled images):
- O1: first seen at attempt step 1; last seen at 3; seen in 3 step(s).
Recent executed commands (mobile_use labels; private arguments withheld):
- Attempt step 1: click; executor=returned; sampled image=SAME (O1 -> O1).
- Attempt step 2: click; executor=returned; sampled image=SAME (O1 -> O1).
SAME means identical sampled pixels, not identical full environment state. CHANGED does not establish task progress. Executor return does not prove goal success.
[/GUI Ledger]
```

If Qwen now proposes the identical click, Govern returns
`NUDGE / REPEATED_ACTION_SAME_SAMPLED_IMAGE`. The click executes. Only after a
normal executor return does the runner queue this feedback for step 4:

```text
GUI Ledger: this action matches 2 consecutive prior executions whose before/after sampled screenshots were identical to the current sample. This action was still executed. Pixel equality does not prove unchanged background state or task failure.
```

Step 4's Qwen history associates that received notice with step 3. Its Inform
is freshly rendered and now includes the third completed execution. No model
has judged the earlier conclusions true or false, and no action was dropped.

The core lives in `src/mobile_world/runtime/gui_ledger.py`. Its default repeat
threshold is three, counting the newly proposed action: the two preceding
contiguous executed actions must have returned, have the same normalized action,
and have identical before/after pixels matching the current observation. A
different or missing observation, an execution exception, an unsupported action,
or a gap in step indices breaks this repeat chain.

The short-loop rule examines the last four completed transitions. Their triples
`(before observation, normalized action, after observation)` must form `A B A B`
with distinct A and B, connected observations, and normal executor returns. The
current observation must match the last resulting observation, and the proposed
action must begin that same sequence again. This yields a Nudge for a possible
loop, not a conclusion that the actions are wrong.

After a Nudge, at least three intervening completed executions are required
before another Nudge. At most five notices are issued per task attempt. Unknown,
malformed, or noneligible actions default to Allow; terminal actions and waits
are not repetition-intervention targets. Both outcomes still execute normally.

The ledger retains at most 64 transitions and 64 observation identities. Inform
shows the most recent six of each. The task anchor is bounded to 4,096 characters
with an explicit truncation notice; the original task in the actor prompt is
unchanged. Fingerprinting accepts images up to 16,777,216 pixels and 8,192 pixels
per dimension. Larger or unreadable images become unknown. Action comparison
uses bounded field-preserving normalization and retains an argument hash rather
than duplicating input text in the ledger.

The screenshot-only path can miss repetition when only a clock, animation, or
other incidental pixels change. It does not crop, mask, use OCR, or infer
semantic screen identity. The opt-in UI rules above can separately compare a
bounded action scope; they do not change the meaning of equal pixels.

`observe(step, screenshot)` registers the current sample. `render_inform()` and
`govern(step, action)` only read state. `record_transition(...)` commits each
completed step once. `summary()` exposes attempt position, retained record
counts, completed executions, notice count, and the last outcome/image comparison.

## Audit and failure behavior

Enabling Ledger also enables the ordinary passive Collector. `--audit-log-root`
selects an external destination; if omitted, the CLI selects a fresh directory
under the system temporary directory. The manifest records the requested
`gui_ledger_mode`, `gui_ledger_driver="deterministic"`, and
`gui_ledger_extra_model_calls=0`. If audit bootstrap fails, evaluation continues
with effective Ledger mode `off`.

The original environment observation and executor result remain raw evidence.
Ledger notices are derived feedback, not rewritten Collector facts. The actual
actor request records which Inform and received feedback reached the model.
After an executed step, one best-effort, secret-free derived record is written
to `run_root/gui_ledger/<task_run_id>/<step>.json`. It contains mode, decision
kind/reason, record counts, and the last execution/image-comparison status,
not task text, action arguments, images, or raw image hashes. Logging failure
does not disable Ledger or interrupt the task. `nudge_count` counts executed
Nudge decisions, not notices proven to have reached a later actor request.
If the Collector evidence chain becomes incomplete, Ledger is disabled for
the rest of that physical task attempt; the actor and executor continue.
Unsupported actions or incomplete observations default to Allow. Runtime Ledger
errors degrade to normal actor/executor behavior.

## Evaluation boundary

CPU fixtures test the mechanics and preserve normal execution when disabled.
They do not establish a success-rate gain. A later authorized comparison should
use identical actor/task configurations with `off`, `inform`, and `full`, and
inspect trajectories as well as task scores. No claim about improved completion,
reduced model use, or reliable hidden-state detection follows from implementing
this layer alone.

Old UI-tree runs may retain `last_ui_target_comparison=UNKNOWN`; this is the
legacy target-only metric and remains immutable historical evidence. An offline
check of that cohort must separately report each new rule's recorded,
not-applicable and unavailable counts. Rule counts overlap, and a recorded
zero-change measurement is different from positive observed change. Do not
count current-tree availability, post-only input equality, or target absence
as a successful re-match of the old target or a successful action. Replaying
saved observations through deterministic rules measures information coverage,
not what the actor would have done after receiving different feedback.

## Local validation materials

Historical run analyses, real-trajectory walkthroughs and retained usage
reports are local, unpublished materials, not dependencies of this design.
The public implementation and synthetic tests describe the mechanics; they
do not establish an effectiveness or token-saving result. Offline observation
coverage from an earlier revision must not be presented as a replay or
success-rate result for the current same-control click rule.
