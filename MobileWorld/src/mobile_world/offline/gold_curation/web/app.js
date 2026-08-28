"use strict";

const state = {
  config: null,
  profile: null,
  assignments: [],
  filter: "ALL",
  active: null,
  predicates: [],
  focalSpans: [],
  oracleSpans: [],
  protectedSpans: [],
  shamSpan: null,
  delimiterRepairs: [],
  correctionCandidates: [],
  transformationPreview: null,
  aiCandidates: null,
  aiDecisionInFlight: null,
  aiReviewFeedback: null,
  formDirty: false,
  coordinateTarget: null,
  pendingOpen: null,
};

const WORKFLOW_STATES = [
  "NOT_ASSIGNED", "DRAFTING", "FINALIZED", "WAITING_FOR_PEER",
  "ADJUDICATION_REQUIRED", "ADJUDICATING", "RESOLVED", "BLOCKED_INVALID_INPUT",
  "FIRST_PASS_LOCKED", "WAITING_FOR_PREVIOUS_STAGE", "FIRST_PASS_COMPLETE",
];

const COORDINATE_DRAG_THRESHOLD_PX = 4;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);

function toast(message, error = false) {
  const element = $("#toast");
  const openDialogs = $$("dialog[open]");
  const host = openDialogs.length ? openDialogs[openDialogs.length - 1] : document.body;
  if (element.parentElement !== host) host.appendChild(element);
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3000);
}

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (options.method && options.method !== "GET" && state.profile?.csrf_token) headers["x-g1-csrf-token"] = state.profile.csrf_token;
  const response = await fetch(path, {...options, headers, cache: "no-store"});
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
  return body;
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function spanFromOffsets(record, charStart, charEnd) {
  const codepoints = Array.from(record.exact_text);
  if (!(Number.isInteger(charStart) && Number.isInteger(charEnd) && 0 <= charStart && charStart < charEnd && charEnd <= codepoints.length)) throw new Error("选区 codepoint 边界无效");
  const prefix = codepoints.slice(0, charStart).join("");
  const exactText = codepoints.slice(charStart, charEnd).join("");
  return {
    record_id: record.record_id,
    char_start: charStart,
    char_end: charEnd,
    utf8_byte_start: new TextEncoder().encode(prefix).length,
    utf8_byte_end: new TextEncoder().encode(prefix + exactText).length,
    exact_text: exactText,
    span_sha256: await sha256Hex(exactText),
    human_selected: true,
  };
}

function uniqueSpanPush(target, span) {
  const identity = `${span.record_id}:${span.char_start}:${span.char_end}:${span.span_sha256}`;
  if (!target.some((item) => `${item.record_id}:${item.char_start}:${item.char_end}:${item.span_sha256}` === identity)) target.push(span);
}

function roleLabel(role) {
  const label = ({
    ACTION_GOLD_PRIMARY: "Action Gold · Primary",
    ACTION_GOLD_SECONDARY: "Action Gold · Secondary",
    TRANSFORMATION_PRIMARY: "Transformation · Primary",
    TRANSFORMATION_SECONDARY: "Transformation · Secondary",
    CONSISTENCY_AUDIT_PRIMARY: "Consistency Audit · Primary",
    CONSISTENCY_AUDIT_SECONDARY: "Consistency Audit · Secondary",
    ADJUDICATOR: "Material Disagreement · Adjudicator",
  })[role] || role;
  return state.config?.solo_first_pass && role.endsWith("_PRIMARY") ? `单人初筛 · ${label.replace(" · Primary", "")}` : label;
}

function statusLabel(value) {
  return ({NOT_ASSIGNED: "未开始", DRAFTING: "草稿中", FINALIZED: "已冻结", WAITING_FOR_PEER: "等待 peer", ADJUDICATION_REQUIRED: "待裁决", ADJUDICATING: "裁决中", RESOLVED: "已解决", BLOCKED_INVALID_INPUT: "输入阻断", FIRST_PASS_LOCKED: "初筛已锁", WAITING_FOR_PREVIOUS_STAGE: "等待前序阶段", FIRST_PASS_COMPLETE: "单人初筛完成"})[value] || value;
}

function slug(value) { return value.toLowerCase().replaceAll("_", "-"); }

function renderBreakdowns(breakdowns = {}) {
  const root = $("#progress-breakdowns");
  const dimensions = [["model", "Model"], ["unit_kind", "Unit kind"], ["channel", "Channel"], ["role", "Role"], ["state", "State"]];
  root.innerHTML = dimensions.map(([key, label]) => {
    const values = breakdowns[key] || {};
    const rows = Object.entries(values).sort(([left], [right]) => left.localeCompare(right)).map(([value, counts]) => {
      const summary = typeof counts === "number" ? String(counts) : WORKFLOW_STATES.filter((name) => counts[name]).map((name) => `${statusLabel(name)} ${counts[name]}`).join(" · ") || "0";
      return `<div class="breakdown-row"><code>${escapeHtml(value)}</code><span>${escapeHtml(summary)}</span></div>`;
    }).join("") || '<div class="breakdown-row"><code>none</code><span>0</span></div>';
    return `<article class="breakdown-card"><h3>${label}</h3>${rows}</article>`;
  }).join("");
}

function showProfileDialog() {
  $("#reviewer-role").innerHTML = `<option value="">— 人工选择 reviewer role —</option>${state.config.roles.map((role) => `<option value="${role}">${roleLabel(role)}</option>`).join("")}`;
  if (state.profile) {
    $("#reviewer-id").value = state.profile.reviewer_id;
    $("#reviewer-role").value = state.profile.role;
  }
  if (state.config.solo_first_pass) {
    $("#profile-policy-copy").textContent = "同一个真实身份按 Action → Transformation → Consistency 做非正式初筛；三个阶段都不计独立 review，也不能晋升为正式证据。";
  }
  $("#profile-dialog").showModal();
}

async function saveProfile(event) {
  event.preventDefault();
  const reviewer_id = $("#reviewer-id").value.trim();
  const role = $("#reviewer-role").value;
  const access_secret = $("#reviewer-secret").value;
  const reviewerBytes = new TextEncoder().encode(reviewer_id);
  if (!reviewer_id || reviewer_id.includes("\0") || reviewerBytes.length > 256) return toast("Reviewer ID 必须是 1–256 bytes 的 exact UTF-8 principal", true);
  if (!role) return toast("必须明确选择 reviewer role", true);
  if (new TextEncoder().encode(access_secret).length < 16) return toast("Access secret 至少需要 16 个 UTF-8 bytes", true);
  const session = await api("/api/session", {method: "POST", body: JSON.stringify({reviewer_id, role, access_secret})});
  $("#reviewer-secret").value = "";
  state.profile = {reviewer_id, role, csrf_token: session.csrf_token, reviewer_identity_sha256: session.reviewer_identity_sha256};
  $("#profile-dialog").close();
  $("#profile-name").textContent = reviewer_id;
  $("#profile-role").textContent = roleLabel(role);
  await loadAssignments();
}

async function loadAssignments() {
  if (!state.profile) return;
  const data = await api("/api/assignments");
  state.config.current_phase = data.current_phase;
  state.assignments = data.items;
  $("#stat-total").textContent = data.total;
  $("#nav-total").textContent = data.total;
  WORKFLOW_STATES.forEach((name) => {
    const source = ["NOT_ASSIGNED", "DRAFTING", "FINALIZED"].includes(name) ? data.own_counts : data.workflow_counts;
    $(`#stat-${slug(name)}`).textContent = source[name] || 0;
  });
  renderBreakdowns(data.breakdowns);
  if (state.config.solo_first_pass) {
    $("#solo-first-pass-banner span").textContent = `当前阶段：${data.current_phase}。全部 190 条锁定后才开放下一阶段；结果不计独立 review。`;
  }
  renderAssignments();
}

function renderAssignments() {
  const list = $("#assignment-list");
  const search = $("#assignment-search").value.trim();
  const items = state.assignments.filter((item) => (state.filter === "ALL" || item.own_status === state.filter || item.workflow_status === state.filter || item.state === state.filter) && (!search || String(item.ordinal).includes(search)));
  if (!items.length) {
    list.className = "assignment-list empty-state";
    list.innerHTML = "<div class=\"empty-glyph\">✓</div><h2>当前筛选没有任务</h2><p>可以切换状态筛选或 reviewer role。</p>";
    return;
  }
  list.className = "assignment-list";
  list.innerHTML = items.map((item) => {
    const blocked = item.can_open === false;
    const workflow = item.workflow_status !== item.own_status ? `<span class="status-pill ${escapeHtml(item.workflow_status)}">${escapeHtml(statusLabel(item.workflow_status))}</span>` : "";
    return `<article class="assignment-row"><span class="assignment-index">#${String(item.ordinal).padStart(3, "0")}</span><div><h3>${escapeHtml(roleLabel(item.review_role))}</h3><p>${escapeHtml(item.channel)} · opaque blind packet</p></div><div class="status-stack"><span class="status-pill ${escapeHtml(item.own_status)}">${escapeHtml(statusLabel(item.own_status))}</span>${workflow}</div><button class="open-button" data-assignment="${escapeHtml(item.assignment_id)}" data-channel="${escapeHtml(item.channel)}" ${blocked ? "disabled" : ""}>打开</button></article>`;
  }).join("");
  $$(".open-button", list).forEach((button) => button.addEventListener("click", () => openAssignment(button.dataset.assignment, button.dataset.channel)));
}

function evidenceMarkup(packet) {
  const imageSuffix = state.profile.role === "ADJUDICATOR" ? `?channel=${encodeURIComponent(packet.channel)}` : "";
  const evidence = packet.evidence.map((item) => `<article class="evidence-card"><h3>${escapeHtml(item.evidence_role)}</h3><pre class="evidence-content">${escapeHtml(typeof item.content === "string" ? item.content : JSON.stringify(item.content, null, 2))}</pre><small>${escapeHtml(item.evidence_token)}</small></article>`).join("");
  const image = `<article class="evidence-card"><h3>当前 GUI · S<sub>t</sub></h3><div class="screenshot-wrap"><img id="target-screenshot" src="/api/assignments/${packet.assignment_id}/image${imageSuffix}" alt="Target-pre GUI screenshot" draggable="false" tabindex="0"><div id="ai-candidate-overlays" class="ai-candidate-overlays" aria-hidden="true"></div><div id="coordinate-selection" class="coordinate-selection" hidden aria-hidden="true"></div></div><p class="screenshot-meta">${packet.current_screenshot.width} × ${packet.current_screenshot.height} · 原始像素坐标；橙色框仅表示当前 AI 候选，不是证据；人工框选支持鼠标/触控拖拽</p></article>`;
  let history = "";
  if (packet.visibility.history_visible && packet.source_records) {
    history = `<div class="section-label">Captured source history</div>${packet.source_records.map((record, index) => `<article class="history-record" data-record-index="${index}"><header><span>${escapeHtml(record.author_role)}</span><span>record ${index + 1}</span></header><pre data-exact-record="${index}"></pre><div><button class="candidate-chip select-span" data-record="${index}" data-target="focal">选区 → Focal / clean anchor</button><button class="candidate-chip select-span" data-record="${index}" data-target="oracle">选区 → Oracle</button><button class="candidate-chip select-span" data-record="${index}" data-target="sham">选区 → Sham</button><button class="candidate-chip select-span" data-record="${index}" data-target="protected">选区 → Protected</button><button class="candidate-chip select-span" data-record="${index}" data-target="repair">选区 → Delimiter repair</button></div></article>`).join("")}`;
  }
  const candidates = packet.target_candidates ? `<article class="evidence-card"><h3>机械候选（只作来源提示）</h3>${packet.target_candidates.map((candidate, index) => `<div class="candidate-row"><code>${escapeHtml(candidate.selection_hint?.exact_text || "需人工选区")}</code><button class="candidate-chip" data-candidate="${index}" data-target="focal" ${candidate.selection_hint ? "" : "disabled"}>人工确认 → Focal / anchor</button>${packet.case_profile.case_type === "MISLEADING_HISTORY" ? `<button class="candidate-chip" data-candidate="${index}" data-target="oracle" ${candidate.selection_hint ? "" : "disabled"}>人工确认 → Oracle</button>` : ""}</div>`).join("")}</article>` : "";
  const natural = packet.natural_action ? `<article class="evidence-card"><h3>历史自然动作 · descriptive only</h3><pre class="review-json">${escapeHtml(JSON.stringify(packet.natural_action.normalized_action, null, 2))}</pre><p class="screenshot-meta">不能回写 Gold/Plan；不得使用 replay response。</p></article>` : "";
  return `<section class="evidence-pane"><div class="section-label">Allowed evidence only</div><article class="evidence-card packet-binding"><b>Source packet</b><code>${escapeHtml(packet.source_packet_sha256)}</code><b>Assignment packet</b><code>${escapeHtml(packet.assignment_packet_sha256)}</code><b>Reviewer</b><code>${escapeHtml(packet.reviewer_identity_sha256)}</code></article><article class="evidence-card"><h3>任务指令</h3><div class="task-text">${escapeHtml(packet.task.instruction)}</div></article>${image}${evidence}${candidates}${history}${natural}</section>`;
}

function hydrateExactHistory(packet) {
  $$("[data-exact-record]").forEach((element) => { element.textContent = packet.source_records[Number(element.dataset.exactRecord)].exact_text; });
}

function commonDisposition(draft, reasons) {
  return `<article class="form-card"><h3>Review disposition</h3><p class="screenshot-meta">没有默认答案；必须由当前 reviewer 明确选择。</p><div class="field-row"><label>结论<select id="disposition"><option value="">— 人工选择 —</option><option value="ACCEPT" ${draft.disposition === "ACCEPT" ? "selected" : ""}>ACCEPT</option><option value="EXCLUDE" ${draft.disposition === "EXCLUDE" ? "selected" : ""}>EXCLUDE</option></select></label><label>排除原因<select id="exclusion-reason"><option value="">— 仅 EXCLUDE —</option>${reasons.map((reason) => `<option value="${reason}" ${draft.exclusion_reason === reason ? "selected" : ""}>${reason}</option>`).join("")}</select></label></div></article>`;
}

function predicateActionOptions(kind) {
  if (!kind) return [];
  if (kind === "POINT_REGION") return state.config.point_action_types;
  if (kind === "DRAG_REGION") return ["drag"];
  if (kind === "TEXT_VARIANTS") return state.config.text_action_types;
  if (kind === "DIRECTION_SET") return state.config.direction_action_types;
  return state.config.action_types;
}

function emptyNormalizedAction(actionType) {
  return {
    class: "mobile_world.runtime.utils.models.JSONAction",
    serializer: "pydantic model_dump(mode=json, exclude_none=false)",
    serializer_version: "2.11.7",
    value: {
      action_json: null, action_name: null, action_type: actionType, app_name: null,
      clear_text: null, direction: null, end_x: null, end_y: null, goal_status: null,
      index: null, keycode: null, start_x: null, start_y: null, text: null, x: null, y: null,
    },
  };
}

function exactActionFields(predicate) {
  const actionType = predicate.action_type || "";
  const value = predicate.normalized_action?.value || emptyNormalizedAction(actionType).value;
  const confirmed = Boolean(predicate.normalized_action) && predicate._exact_fields_confirmed !== false;
  const presence = (key) => value[key] === null || value[key] === undefined ? "NULL" : "VALUE";
  const stringField = (key, label, textarea = false) => `<div class="field-row"><label>${label} presence<select data-a="${key}_mode"><option value="NULL" ${presence(key) === "NULL" ? "selected" : ""}>null</option><option value="VALUE" ${presence(key) === "VALUE" ? "selected" : ""}>exact string</option></select></label><label>${label}${textarea ? `<textarea data-a="${key}">${escapeHtml(value[key] ?? "")}</textarea>` : `<input data-a="${key}" value="${escapeHtml(value[key] ?? "")}">`}</label></div>`;
  const integerField = (key, label) => `<div class="field-row"><label>${label} presence<select data-a="${key}_mode"><option value="NULL" ${presence(key) === "NULL" ? "selected" : ""}>null</option><option value="VALUE" ${presence(key) === "VALUE" ? "selected" : ""}>integer</option></select></label><label>${label}<input type="number" step="1" data-a="${key}" value="${value[key] ?? ""}"></label></div>`;
  return `<p class="screenshot-meta">完整 pinned JSONAction 字段；页面不替 reviewer 推断哪些 optional 字段应为 null。index 与 x/y 互斥由 production validator 复核。</p><label class="inline-check"><input type="checkbox" data-a="confirm_exact_fields" ${confirmed ? "checked" : ""}>我确认下列每个 optional production field 的 null/value 状态</label>${integerField("index", "index")}${integerField("x", "x")}${integerField("y", "y")}${stringField("text", "text", true)}<label>direction<select data-a="direction"><option value="" ${value.direction === null ? "selected" : ""}>null</option>${state.config.directions.filter((item) => item !== "any").map((item) => `<option ${value.direction === item ? "selected" : ""}>${item}</option>`).join("")}</select></label>${stringField("goal_status", "goal_status")}${stringField("app_name", "app_name")}${stringField("keycode", "keycode")}<label>clear_text<select data-a="clear_text"><option value="NULL" ${value.clear_text === null ? "selected" : ""}>null</option><option value="TRUE" ${value.clear_text === true ? "selected" : ""}>true</option><option value="FALSE" ${value.clear_text === false ? "selected" : ""}>false</option></select></label>${integerField("start_x", "start_x")}${integerField("start_y", "start_y")}${integerField("end_x", "end_x")}${integerField("end_y", "end_y")}${stringField("action_name", "action_name")}<div class="field-row"><label>action_json presence<select data-a="action_json_mode"><option value="NULL" ${presence("action_json") === "NULL" ? "selected" : ""}>null</option><option value="VALUE" ${presence("action_json") === "VALUE" ? "selected" : ""}>exact JSON object</option></select></label><label>action_json<textarea data-a="action_json">${escapeHtml(JSON.stringify(value.action_json ?? {}, null, 2))}</textarea></label></div>`;
}

function regionFields(prefix, region = {}, additional = []) {
  const shape = region.shape || "";
  const vertices = shape === "POLYGON" ? (region.vertices || []).map((item) => item.join(",")).join("\n") : "";
  return `<label>Shape<select data-p="${prefix}_shape"><option value="">— 人工选择 —</option><option ${shape === "BOUNDING_BOX" ? "selected" : ""}>BOUNDING_BOX</option><option ${shape === "POLYGON" ? "selected" : ""}>POLYGON</option></select></label><div class="field-row four"><label>x min<input type="number" min="0" data-p="${prefix}_x_min" value="${region.x_min ?? ""}"></label><label>y min<input type="number" min="0" data-p="${prefix}_y_min" value="${region.y_min ?? ""}"></label><label>x max<input type="number" min="0" data-p="${prefix}_x_max" value="${region.x_max ?? ""}"></label><label>y max<input type="number" min="0" data-p="${prefix}_y_max" value="${region.y_max ?? ""}"></label></div><label>Polygon vertices（每行 x,y）<textarea data-p="${prefix}_vertices">${escapeHtml(vertices)}</textarea></label><label>Additional region set JSON（可继续用截图两次点选追加）<textarea data-p="${prefix}_additional_regions">${escapeHtml(JSON.stringify(additional, null, 2))}</textarea></label>`;
}

function predicateFields(predicate, index, packet) {
  const kind = predicate.predicate_kind || "";
  const evidenceChecks = packet.evidence.map((item) => `<label><input type="checkbox" data-p="evidence_id" value="${item.evidence_token}" ${(predicate.evidence_ids || []).includes(item.evidence_token) ? "checked" : ""}>${escapeHtml(item.evidence_role)}</label>`).join("");
  let fields = "";
  if (!kind) fields = '<p class="screenshot-meta">先明确选择 predicate kind；页面不会替你推断动作或区域。</p>';
  if (kind === "POINT_REGION") fields = `${regionFields("region", predicate.regions?.[0], predicate.regions?.slice(1) || [])}<div class="field-row"><label>Tolerance px<input type="number" min="0" step="1" data-p="tolerance_px" value="${predicate.tolerance_px ?? ""}"></label><button type="button" class="add-button coordinate-pick" data-coordinate="${index}:region">在截图上拖拽框选并追加 region</button></div>`;
  if (kind === "DRAG_REGION") fields = `<p class="screenshot-meta">Start region set</p>${regionFields("start", predicate.start_regions?.[0], predicate.start_regions?.slice(1) || [])}<button type="button" class="add-button coordinate-pick" data-coordinate="${index}:start">截图拖拽框选并追加 start</button><p class="screenshot-meta">End region set</p>${regionFields("end", predicate.end_regions?.[0], predicate.end_regions?.slice(1) || [])}<button type="button" class="add-button coordinate-pick" data-coordinate="${index}:end">截图拖拽框选并追加 end</button><div class="check-grid">${state.config.directions.map((value) => `<label><input type="checkbox" data-p="allowed_direction" value="${value}" ${(predicate.allowed_directions || []).includes(value) ? "checked" : ""}>${value}</label>`).join("")}</div><div class="field-row"><label>Min displacement px<input type="number" min="0" step="1" data-p="minimum_displacement_px" value="${predicate.minimum_displacement_px ?? ""}"></label><label>Tolerance px<input type="number" min="0" step="1" data-p="tolerance_px" value="${predicate.tolerance_px ?? ""}"></label></div>`;
  if (kind === "TEXT_VARIANTS") fields = `<div class="field-row"><label>Field<input data-p="field" value="${escapeHtml(predicate.field || "")}"></label><label>Case sensitivity<select data-p="case_sensitive"><option value="">— 人工选择 —</option><option value="true" ${predicate.case_sensitive === true ? "selected" : ""}>case-sensitive</option><option value="false" ${predicate.case_sensitive === false ? "selected" : ""}>case-insensitive</option></select></label></div><label>允许值（JSON string array；保留每个值内的 exact 换行与 NFC bytes）<textarea data-p="allowed_values">${escapeHtml(JSON.stringify(predicate.allowed_values || [], null, 2))}</textarea></label>`;
  if (kind === "DIRECTION_SET") fields = `<div class="check-grid">${state.config.directions.filter((value) => value !== "any").map((value) => `<label><input type="checkbox" data-p="allowed_direction" value="${value}" ${(predicate.allowed_directions || []).includes(value) ? "checked" : ""}>${value}</label>`).join("")}</div>`;
  if (kind === "EXACT_NORMALIZED_ACTION") fields = `<div class="section-label">Production normalized action fields</div><div data-exact-action-fields>${exactActionFields(predicate)}</div>`;
  return `${fields}<div class="section-label">Evidence citations</div><div class="check-grid">${evidenceChecks}</div><label>Semantic rationale<textarea data-p="rationale">${escapeHtml(predicate.rationale || "")}</textarea></label><label class="inline-check"><input type="checkbox" data-p="human_selected" ${predicate.human_selected ? "checked" : ""}>我人工确认此 predicate</label>`;
}

function renderPredicates() {
  cancelCoordinatePicker();
  const root = $("#predicate-list");
  if (!root) return;
  const packet = state.active.data.packet;
  root.innerHTML = state.predicates.map((predicate, index) => {
    const kind = predicate.predicate_kind || "";
    const options = predicateActionOptions(kind);
    if (!options.includes(predicate.action_type)) predicate.action_type = "";
    if (kind === "EXACT_NORMALIZED_ACTION" && predicate.action_type && predicate.normalized_action?.value?.action_type !== predicate.action_type) predicate.normalized_action = emptyNormalizedAction(predicate.action_type);
    return `<div class="predicate-card" data-index="${index}"><div class="predicate-head"><select data-p="predicate_kind"><option value="">— predicate kind —</option>${state.config.predicate_kinds.map((value) => `<option ${kind === value ? "selected" : ""}>${value}</option>`).join("")}</select><select data-p="action_type"><option value="">— action type —</option>${options.map((value) => `<option ${predicate.action_type === value ? "selected" : ""}>${value}</option>`).join("")}</select><button type="button" class="remove-button" data-remove="${index}">×</button></div>${predicateFields(predicate, index, packet)}</div>`;
  }).join("");
  $$("[data-remove]", root).forEach((button) => { button.onclick = () => { const index = Number(button.dataset.remove); try { syncPredicatesExcept(index); state.predicates.splice(index, 1); renderPredicates(); } catch (error) { toast(error.message, true); } }; });
  $$("select[data-p=predicate_kind]", root).forEach((select) => { select.onchange = () => { const card = select.closest(".predicate-card"); const index = Number(card.dataset.index); try { syncPredicatesExcept(index); state.predicates[index] = {predicate_kind: select.value, action_type: "", evidence_ids: [], rationale: "", human_selected: false}; renderPredicates(); } catch (error) { select.value = state.predicates[index]?.predicate_kind || ""; toast(error.message, true); } }; });
  $$("select[data-p=action_type]", root).forEach((select) => { select.onchange = () => { const card = select.closest(".predicate-card"); const kind = card.querySelector("select[data-p=predicate_kind]")?.value; if (kind === "EXACT_NORMALIZED_ACTION") { const target = card.querySelector("[data-exact-action-fields]"); target.innerHTML = exactActionFields({action_type: select.value, normalized_action: select.value ? emptyNormalizedAction(select.value) : null, _exact_fields_confirmed: false}); } }; });
  $$(".coordinate-pick", root).forEach((button) => { button.onclick = () => {
    cancelCoordinatePicker();
    const [index, target] = button.dataset.coordinate.split(":");
    state.coordinateTarget = {index: Number(index), target, firstCorner: null, pointerId: null, dragStart: null};
    const image = $("#target-screenshot");
    image?.closest(".screenshot-wrap")?.classList.add("picking");
    image?.focus({preventScroll: true});
    toast("请在左侧截图按住并拖出矩形；也可依次点击两个角点");
  }; });
}

function readRegion(card, prefix) {
  const value = (name) => card.querySelector(`[data-p="${prefix}_${name}"]`)?.value;
  const shape = value("shape");
  if (!shape) throw new Error(`${prefix} region shape 必须人工选择`);
  if (shape === "POLYGON") return {shape: "POLYGON", vertices: (value("vertices") || "").split("\n").filter((line) => line.length).map((line) => line.split(",").map((item) => Number(item)))};
  const coordinates = Object.fromEntries(["x_min", "y_min", "x_max", "y_max"].map((key) => {
    const raw = value(key);
    if (raw === undefined || raw === "") throw new Error(`${prefix}.${key} 必须明确填写`);
    return [key, Number(raw)];
  }));
  return {shape: "BOUNDING_BOX", ...coordinates};
}

function readRegionSet(card, prefix) {
  const primary = readRegion(card, prefix);
  const additional = JSON.parse(card.querySelector(`[data-p="${prefix}_additional_regions"]`)?.value || "[]");
  if (!Array.isArray(additional)) throw new Error(`${prefix} additional regions 必须是 JSON array`);
  return [primary, ...additional];
}

function collectPredicate(card, lenient = false) {
  const value = (key) => card.querySelector(`[data-p="${key}"]`)?.value;
  const checked = (key) => card.querySelector(`[data-p="${key}"]`)?.checked === true;
  const kind = value("predicate_kind");
  if (!lenient && !kind) throw new Error("每个 predicate 必须明确选择 kind");
  if (!lenient && !value("action_type")) throw new Error("每个 predicate 必须明确选择 action type");
  const predicate = {predicate_kind: kind, action_type: value("action_type"), evidence_ids: $$('[data-p="evidence_id"]:checked', card).map((item) => item.value), rationale: value("rationale") || "", human_selected: checked("human_selected")};
  try {
    if (kind === "POINT_REGION") {
      if (value("tolerance_px") === "") throw new Error("Point tolerance 必须人工填写");
      Object.assign(predicate, {regions: readRegionSet(card, "region"), tolerance_px: Number(value("tolerance_px"))});
    }
    if (kind === "DRAG_REGION") {
      if (value("minimum_displacement_px") === "" || value("tolerance_px") === "") throw new Error("Drag displacement 与 tolerance 必须人工填写");
      Object.assign(predicate, {start_regions: readRegionSet(card, "start"), end_regions: readRegionSet(card, "end"), allowed_directions: $$('[data-p="allowed_direction"]:checked', card).map((item) => item.value), minimum_displacement_px: Number(value("minimum_displacement_px")), tolerance_px: Number(value("tolerance_px"))});
    }
    if (kind === "TEXT_VARIANTS") {
      if (!value("case_sensitive")) throw new Error("Text variant case sensitivity 必须人工选择");
      const allowedValues = JSON.parse(value("allowed_values") || "[]");
      if (!Array.isArray(allowedValues) || allowedValues.some((item) => typeof item !== "string")) throw new Error("Text variants 必须是 JSON string array");
      Object.assign(predicate, {field: value("field"), unicode_normalization: "NFC", case_sensitive: value("case_sensitive") === "true", allowed_values: allowedValues});
    }
    if (kind === "DIRECTION_SET") predicate.allowed_directions = $$('[data-p="allowed_direction"]:checked', card).map((item) => item.value);
    if (kind === "EXACT_NORMALIZED_ACTION") {
      const normalized = emptyNormalizedAction(predicate.action_type);
      const action = normalized.value;
      const actionValue = (key) => card.querySelector(`[data-a="${key}"]`)?.value;
      if (card.querySelector('[data-a="confirm_exact_fields"]')?.checked !== true) throw new Error("必须人工确认所有 optional production fields 的 null/value 状态");
      const optionalInteger = (key) => {
        if (actionValue(`${key}_mode`) === "NULL") return null;
        const raw = actionValue(key);
        if (raw === undefined || raw === "") throw new Error(`${key} 必须明确填写`);
        const parsed = Number(raw);
        if (!Number.isInteger(parsed)) throw new Error(`${key} 必须是整数`);
        return parsed;
      };
      const optionalString = (key) => actionValue(`${key}_mode`) === "NULL" ? null : (actionValue(key) ?? "");
      for (const key of ["index", "x", "y", "start_x", "start_y", "end_x", "end_y"]) action[key] = optionalInteger(key);
      for (const key of ["text", "goal_status", "app_name", "keycode", "action_name"]) action[key] = optionalString(key);
      action.direction = actionValue("direction") || null;
      const clearText = actionValue("clear_text");
      action.clear_text = clearText === "NULL" ? null : clearText === "TRUE";
      if (actionValue("action_json_mode") === "NULL") action.action_json = null;
      else {
        action.action_json = JSON.parse(actionValue("action_json") || "{}");
        if (!action.action_json || Array.isArray(action.action_json) || typeof action.action_json !== "object") throw new Error("action_json 必须是 JSON object");
      }
      predicate.normalized_action = normalized;
    }
  } catch (error) { if (!lenient) throw error; }
  return predicate;
}

function syncPredicatesExcept(excludedIndex = null) {
  $$(".predicate-card").forEach((card) => {
    const index = Number(card.dataset.index);
    if (index !== excludedIndex) state.predicates[index] = collectPredicate(card);
  });
}

function syncPredicatesLenient() {
  $$(".predicate-card").forEach((card) => {
    state.predicates[Number(card.dataset.index)] = collectPredicate(card, true);
  });
}

const AI_DECISION_LABELS = {
  ADOPT_TO_FORM: "采用",
  ADOPT_WITH_EDITS_TO_FORM: "修改后采用",
  USE_AS_SUPPLEMENT: "作为补充",
  IGNORE: "不采用",
};

function actionTypeLabel(actionType) {
  return ({
    answer: "回答",
    ask_user: "询问用户",
    click: "点击",
    double_tap: "双击",
    drag: "拖拽",
    finished: "完成任务",
    input_text: "输入文字",
    keyboard_enter: "按回车",
    long_press: "长按",
    navigate_back: "返回",
    navigate_home: "回到主屏",
    open_app: "打开应用",
    scroll: "滚动",
    swipe: "滑动",
    wait: "等待",
  })[actionType] || actionType;
}

function candidateRegionBounds(region) {
  if (region?.shape === "BOUNDING_BOX") return region;
  if (region?.shape !== "POLYGON" || !Array.isArray(region.vertices) || !region.vertices.length) return null;
  const xs = region.vertices.map((point) => point[0]);
  const ys = region.vertices.map((point) => point[1]);
  return {x_min: Math.min(...xs), y_min: Math.min(...ys), x_max: Math.max(...xs), y_max: Math.max(...ys)};
}

function candidatePredicateSummary(predicate) {
  const action = actionTypeLabel(predicate.action_type);
  const rows = [];
  if (predicate.predicate_kind === "POINT_REGION") {
    (predicate.regions || []).forEach((region, index) => {
      const bounds = candidateRegionBounds(region);
      if (bounds) rows.push(`区域 ${index + 1}：左 ${bounds.x_min} · 上 ${bounds.y_min} · 右 ${bounds.x_max} · 下 ${bounds.y_max}`);
    });
    rows.push(`允许误差：${predicate.tolerance_px} px`);
  }
  if (predicate.predicate_kind === "DRAG_REGION") {
    const start = candidateRegionBounds(predicate.start_regions?.[0]);
    const end = candidateRegionBounds(predicate.end_regions?.[0]);
    if (start) rows.push(`起点区域：(${start.x_min}, ${start.y_min}) → (${start.x_max}, ${start.y_max})`);
    if (end) rows.push(`终点区域：(${end.x_min}, ${end.y_min}) → (${end.x_max}, ${end.y_max})`);
    if (predicate.allowed_directions?.length) rows.push(`方向：${predicate.allowed_directions.join(" / ")}`);
  }
  if (predicate.predicate_kind === "TEXT_VARIANTS") {
    rows.push(`字段：${predicate.field}`);
    rows.push(`允许内容：${(predicate.allowed_values || []).map((value) => JSON.stringify(value)).join(" / ")}`);
  }
  if (predicate.predicate_kind === "DIRECTION_SET") rows.push(`允许方向：${(predicate.allowed_directions || []).join(" / ")}`);
  if (predicate.predicate_kind === "EXACT_NORMALIZED_ACTION") {
    const populated = Object.entries(predicate.normalized_action?.value || {}).filter(([key, value]) => key !== "action_type" && value !== null);
    rows.push(populated.length ? populated.map(([key, value]) => `${key}=${JSON.stringify(value)}`).join(" · ") : "其余 optional 字段均为 null");
  }
  const raw = JSON.stringify(predicate, null, 2);
  return `<div class="ai-action-summary"><span class="ai-action-kicker">建议动作</span><strong>${escapeHtml(action)}</strong>${rows.map((row) => `<p>${escapeHtml(row)}</p>`).join("")}<details class="ai-technical-details"><summary>查看技术字段</summary><pre>${escapeHtml(raw)}</pre></details></div>`;
}

function aiCandidatePanel() {
  if (!state.config?.ai_candidate_assistance?.enabled) return "";
  return `<article class="form-card ai-candidate-panel"><header><div><span class="eyebrow">第 1 步 · 简易候选审核</span><h3>逐条选择，不是在三个 Agent 中选一个</h3></div><span class="ai-warning">AI 候选不是证据</span></header><p class="ai-simple-copy">A / B / C 三列地位相同，不是投票也没有推荐顺序。每张卡只需：看左侧任务与截图 → 勾选“我已核对” → 点一个大按钮。选择会立即记录，但不会自动保存或锁定标注。</p><div id="ai-review-feedback" class="ai-inline-feedback" role="status" aria-live="polite"></div><div id="ai-candidate-columns" class="ai-candidate-columns"><p class="screenshot-meta">正在读取已冻结候选…</p></div><button type="button" class="quiet-button ai-open-advanced" data-open-advanced>我想自己填写 / 打开高级编辑</button></article>`;
}

function flattenedAiCandidates(data) {
  return data.agent_outputs.flatMap((output) => output.candidate_items.map((item) => ({agentSlot: output.agent_slot, item})));
}

function aiCandidateCard(entry) {
  const {agentSlot, item} = entry;
  const current = item.current_decision?.decision || null;
  const currentLabel = current ? `已记录：${AI_DECISION_LABELS[current]}` : "待选择（不是第 4 个 Agent）";
  const uncertainty = item.uncertainty_note ? `<div class="ai-uncertainty"><b>需要注意：</b>${escapeHtml(item.uncertainty_note)}</div>` : "";
  const buttons = Object.entries(AI_DECISION_LABELS).map(([decision, label]) => `<button type="button" data-ai-decision="${decision}" aria-pressed="${current === decision}">${escapeHtml(label)}</button>`).join("");
  return `<article class="ai-candidate-item ${current ? "is-decided" : "is-pending"}" data-ai-candidate="${escapeHtml(item.candidate_token)}"><header class="ai-candidate-title"><div><span>Agent ${escapeHtml(agentSlot)}</span><b>${escapeHtml(currentLabel)}</b></div><span class="ai-candidate-number">独立候选</span></header>${candidatePredicateSummary(item.predicate)}<div class="ai-rationale"><b>为什么可能合理</b><p>${escapeHtml(item.concise_rationale)}</p></div>${uncertainty}<label class="ai-item-attestation"><input type="checkbox" data-ai-evidence-verified><span><b>我已亲自核对</b>左侧任务、截图和这条候选引用的可见 evidence；我知道 AI 候选本身不是证据。</span></label><div class="ai-decision-actions">${buttons}</div><div class="ai-item-feedback" data-ai-item-feedback role="alert" aria-live="assertive"></div><details class="ai-optional-details"><summary>可选备注与 evidence ID</summary><label>人工备注（可留空）<textarea data-ai-note maxlength="4000">${escapeHtml(item.current_decision?.human_note || "")}</textarea></label><p class="ai-evidence-links">Evidence: ${item.evidence_tokens.map((token) => `<code>${escapeHtml(token)}</code>`).join(" ")}</p></details></article>`;
}

function renderAiCandidateOverlays(candidate) {
  const layer = $("#ai-candidate-overlays");
  if (!layer) return;
  layer.innerHTML = "";
  if (!candidate) return;
  const predicate = candidate.predicate;
  const width = state.active?.data.packet.current_screenshot.width;
  const height = state.active?.data.packet.current_screenshot.height;
  if (!(width > 0 && height > 0)) return;
  const regions = predicate.predicate_kind === "POINT_REGION"
    ? (predicate.regions || []).map((region) => ({region, label: "AI 候选"}))
    : predicate.predicate_kind === "DRAG_REGION"
      ? [...(predicate.start_regions || []).map((region) => ({region, label: "AI 起点"})), ...(predicate.end_regions || []).map((region) => ({region, label: "AI 终点"}))]
      : [];
  layer.innerHTML = regions.map(({region, label}) => {
    const bounds = candidateRegionBounds(region);
    if (!bounds || !(bounds.x_max > bounds.x_min && bounds.y_max > bounds.y_min)) return "";
    const style = `left:${100 * bounds.x_min / width}%;top:${100 * bounds.y_min / height}%;width:${100 * (bounds.x_max - bounds.x_min) / width}%;height:${100 * (bounds.y_max - bounds.y_min) / height}%`;
    return `<div class="ai-candidate-overlay" style="${style}"><span>${escapeHtml(label)}</span></div>`;
  }).join("");
}

function openAdvancedActionForm(scroll = true) {
  const advanced = $("#advanced-action-form");
  if (!advanced) return;
  advanced.open = true;
  if (scroll) advanced.scrollIntoView({behavior: "smooth", block: "start"});
}

function bindOpenAdvancedButtons() {
  $$('[data-open-advanced]').forEach((button) => { button.onclick = () => openAdvancedActionForm(); });
}

function updateAiLockControl() {
  if (!(state.config?.solo_first_pass && state.active?.data.packet.channel === "ACTION_GOLD" && state.config?.ai_candidate_assistance?.enabled)) return;
  const submit = $("#submit-review");
  if (!submit) return;
  if (!state.aiCandidates) {
    submit.disabled = true;
    submit.textContent = "候选尚未读取";
    return;
  }
  const pending = flattenedAiCandidates(state.aiCandidates).filter(({item}) => !item.current_decision).length;
  submit.disabled = !state.config.first_pass_lock_open || pending > 0;
  submit.textContent = pending > 0 ? `先完成候选选择（还剩 ${pending}）` : state.config.first_pass_lock_open ? "锁定本阶段（非正式）" : "等待 G1.5 CPU codec gate";
}

function setAiDecisionUiBusy(busy) {
  const close = $("#close-workbench");
  const save = $("#save-draft");
  const submit = $("#submit-review");
  if (close) close.disabled = busy;
  if (save) save.disabled = busy;
  if (submit) submit.disabled = busy;
  $$('[data-ai-decision]').forEach((button) => { button.disabled = busy; });
  if (!busy) updateAiLockControl();
}

function renderAiCandidates() {
  const root = $("#ai-candidate-columns");
  if (!root) return;
  const data = state.aiCandidates;
  if (!data) {
    root.innerHTML = '<div class="ai-inline-feedback error">已冻结候选暂不可用。当前不能锁定本任务；你仍可打开高级编辑并保存草稿。</div>';
    bindOpenAdvancedButtons();
    renderAiCandidateOverlays(null);
    updateAiLockControl();
    return;
  }
  const all = flattenedAiCandidates(data);
  const pending = all.filter(({item}) => !item.current_decision);
  const completed = all.length - pending.length;
  const progress = `<div class="ai-review-progress"><div><b>候选审核 ${completed} / ${all.length}</b><span>${pending.length ? `还剩 ${pending.length} 条需要选择` : "所有 AI 候选都已有明确决定"}</span></div><progress value="${completed}" max="${Math.max(all.length, 1)}"></progress></div>`;
  const columns = data.agent_outputs.map((output) => {
    const items = output.candidate_items.map((item) => aiCandidateCard({agentSlot: output.agent_slot, item})).join("");
    const body = output.response_kind === "ABSTAIN" ? `<div class="ai-abstain"><b>无需选择 · ABSTAIN</b><p>${escapeHtml(output.abstain_reason)}</p></div>` : items;
    return `<section class="ai-agent-column"><header><b>Agent ${escapeHtml(output.agent_slot)}</b><span>${output.candidate_items.length} 条候选</span></header>${body}</section>`;
  }).join("");
  const completion = pending.length ? "" : `<div class="ai-review-complete"><b>✓ 候选选择完成</b><p>${all.length ? "这些决定已写入独立候选日志。被采用的动作仍只是未保存表单，关闭页面后不会自动恢复；请现在进入第 2 步，逐项确认字段后再保存或锁定。" : "三个 Agent 都没有提供 atomic candidate。你仍需自己决定 ACCEPT / EXCLUDE 并填写最终表单。"}</p><button type="button" class="primary-button" data-open-advanced>进入第 2 步 · 最终人工确认</button></div>`;
  root.innerHTML = `${progress}<div class="ai-agent-grid">${columns}</div>${completion}`;
  const feedback = $("#ai-review-feedback");
  if (feedback) {
    feedback.textContent = state.aiReviewFeedback?.message || "";
    feedback.className = `ai-inline-feedback${state.aiReviewFeedback?.error ? " error" : state.aiReviewFeedback ? " success" : ""}`;
  }
  $$('[data-ai-decision]', root).forEach((button) => { button.onclick = () => decideAiCandidate(button); });
  $$('.ai-candidate-item', root).forEach((card) => {
    const candidateToken = card.dataset.aiCandidate;
    const candidate = all.find(({item}) => item.candidate_token === candidateToken)?.item || null;
    card.onpointerenter = () => renderAiCandidateOverlays(candidate);
    card.onfocusin = () => renderAiCandidateOverlays(candidate);
    card.onpointerleave = () => { if (!card.contains(document.activeElement)) renderAiCandidateOverlays(null); };
    card.onfocusout = (event) => { if (!card.contains(event.relatedTarget)) renderAiCandidateOverlays(null); };
  });
  bindOpenAdvancedButtons();
  renderAiCandidateOverlays(null);
  updateAiLockControl();
}

async function loadAiCandidates(assignmentId) {
  if (!state.config?.ai_candidate_assistance?.enabled) return;
  try {
    state.aiCandidates = await api(`/api/assist/action-gold/${assignmentId}`);
    renderAiCandidates();
  } catch (error) {
    state.aiCandidates = null;
    renderAiCandidates();
    toast(`AI 候选不可用：${error.message}`, true);
  }
}

async function decideAiCandidate(button) {
  const card = button.closest("[data-ai-candidate]");
  const assignmentId = state.active?.assignmentId;
  const candidateData = state.aiCandidates;
  const candidateToken = card.dataset.aiCandidate;
  const decision = button.dataset.aiDecision;
  const candidate = candidateData.agent_outputs.flatMap((output) => output.candidate_items).find((item) => item.candidate_token === candidateToken);
  if (!candidate) return toast("候选已失效，请重新打开任务", true);
  const evidenceVerified = card.querySelector("[data-ai-evidence-verified]");
  if (!evidenceVerified?.checked) {
    const message = "先勾选上面的“我已亲自核对”，再选择采用或不采用。";
    card.classList.add("needs-attention");
    const itemFeedback = card.querySelector("[data-ai-item-feedback]");
    itemFeedback.textContent = message;
    state.aiReviewFeedback = {message, error: true};
    const feedback = $("#ai-review-feedback");
    if (feedback) { feedback.textContent = message; feedback.className = "ai-inline-feedback error"; }
    evidenceVerified.focus();
    evidenceVerified.scrollIntoView({behavior: "smooth", block: "center"});
    return toast(message, true);
  }
  if (state.aiDecisionInFlight) return;
  const previousDecision = candidate.current_decision?.decision || null;
  const requestMarker = {assignmentId, candidateToken};
  state.aiDecisionInFlight = requestMarker;
  card.setAttribute("aria-busy", "true");
  setAiDecisionUiBusy(true);
  const itemFeedback = card.querySelector("[data-ai-item-feedback]");
  itemFeedback.textContent = "正在记录这一个选择…";
  try {
    const humanNote = card.querySelector("[data-ai-note]").value;
    const recorded = await api("/api/assist/candidate-decisions", {
      method: "POST",
      body: JSON.stringify({
        assignment_id: assignmentId,
        candidate_token: candidateToken,
        decision,
        human_note: humanNote,
        human_confirmed_item_review: true,
        human_verified_visible_evidence: true,
        ai_candidate_is_not_evidence: true,
        annotation_form_not_saved_or_finalized: true,
      }),
    });
    if (state.active?.assignmentId !== assignmentId || state.aiCandidates !== candidateData || state.aiDecisionInFlight !== requestMarker) {
      toast("选择已记录，但当前页面已经切换；旧候选没有复制到新任务，请重新打开原任务。", true);
      return;
    }
    candidate.current_decision = {decision: recorded.decision, human_note: recorded.human_note, decision_event_token: recorded.decision_event_token};
    if (decision !== "IGNORE") {
      syncPredicatesLenient();
      const material = structuredClone(candidate.predicate);
      state.predicates.forEach((item) => {
        item.human_selected = false;
        item._exact_fields_confirmed = false;
      });
      state.predicates.push({...material, evidence_ids: structuredClone(candidate.evidence_tokens), rationale: candidate.concise_rationale, human_selected: false, _exact_fields_confirmed: false});
      renderPredicates();
      $("#closed-world").checked = false;
      $("#all-actions").checked = false;
      state.formDirty = true;
      $("#autosave-state").textContent = "未保存 · AI 候选已复制";
    }
    let message = decision === "IGNORE" ? "已记录：不采用。人工表单没有改变。" : `已记录：${AI_DECISION_LABELS[decision]}。候选已复制到未保存表单。`;
    if (previousDecision && previousDecision !== "IGNORE" && decision === "IGNORE") message += " 之前已复制的内容不会自动删除，如需删除请在第 2 步操作。";
    if (previousDecision && previousDecision !== "IGNORE" && decision !== "IGNORE") message += " 这是一次新的显式复制，旧副本不会自动合并。";
    state.aiReviewFeedback = {message, error: false};
    renderAiCandidates();
    toast(message);
    if (decision === "ADOPT_WITH_EDITS_TO_FORM") openAdvancedActionForm();
  } catch (error) {
    const message = `这次选择没有记录：${error.message}`;
    state.aiReviewFeedback = {message, error: true};
    itemFeedback.textContent = message;
    const feedback = $("#ai-review-feedback");
    if (feedback) { feedback.textContent = message; feedback.className = "ai-inline-feedback error"; }
    toast(message, true);
  } finally {
    if (state.aiDecisionInFlight === requestMarker) state.aiDecisionInFlight = null;
    card.removeAttribute("aria-busy");
    if (!state.aiDecisionInFlight) setAiDecisionUiBusy(false);
  }
}

function actionForm(draft = {}) {
  state.predicates = structuredClone(draft.predicates || []);
  const advancedOpen = !state.config?.ai_candidate_assistance?.enabled || Boolean(draft.disposition);
  return `<section class="form-pane">${aiCandidatePanel()}<details id="advanced-action-form" class="advanced-action-form" ${advancedOpen ? "open" : ""}><summary><span><b>第 2 步 · 最终人工确认</b><small>只有要修改候选或准备保存 / 锁定时才需要打开</small></span><span class="advanced-chevron">⌄</span></summary><div class="advanced-action-body">${commonDisposition(draft, ["NO_GOLD_CONSENSUS"])}<article class="form-card"><h3>Accepted next-action set</h3><p class="screenshot-meta">逐项核对所有保留动作的字段、坐标、误差、证据和理由；候选选择不会替你完成这些确认。</p><div id="predicate-list"></div><button type="button" id="add-predicate" class="add-button">＋ 添加 accepted predicate</button><label class="inline-check"><input id="closed-world" type="checkbox" ${draft.closed_world_confirmed ? "checked" : ""}>我确认这是 closed-world accepted set</label><label class="inline-check"><input id="all-actions" type="checkbox" ${draft.all_reasonable_actions_enumerated ? "checked" : ""}>已枚举所有合理的一步动作</label></article><article class="form-card"><h3>Evidence rationale</h3><textarea id="evidence-rationale" placeholder="用一句话说明你依据左侧哪些可见证据做出最终判断">${escapeHtml(draft.evidence_rationale || "")}</textarea></article></div></details></section>`;
}

function spanList(title, id, spans) {
  return `<div class="section-label">${title}</div><div id="${id}" class="span-list">${spans.map((span, index) => `<div class="span-item"><b>${escapeHtml(span.record_id)}</b><code>${escapeHtml(span.exact_text)}</code><span>cp ${span.char_start}–${span.char_end} · utf8 ${span.utf8_byte_start}–${span.utf8_byte_end}</span><button type="button" class="remove-button" data-span-list="${id}" data-index="${index}">×</button></div>`).join("") || "<p class=\"screenshot-meta\">尚未选择</p>"}</div>`;
}

function renderSpanLists(repairsAlreadySynchronized = false) {
  if (!repairsAlreadySynchronized) syncDelimiterRepairs();
  const groups = [["focal-list", "Focal / clean reference anchor", state.focalSpans], ["oracle-list", "Oracle target set", state.oracleSpans], ["protected-list", "Protected spans", state.protectedSpans], ["sham-list", "Matched sham span", state.shamSpan ? [state.shamSpan] : []]];
  groups.forEach(([id, title, spans]) => { const current = $(`#${id}`); if (current) current.outerHTML = new DOMParser().parseFromString(spanList(title, id, spans), "text/html").body.querySelector(`#${id}`).outerHTML; });
  renderDelimiterRepairs();
  bindSpanRemove();
}

function invalidateTransformationPreview() {
  state.transformationPreview = null;
  const confirmed = $("#preview-human-confirmed");
  if (confirmed) confirmed.checked = false;
  renderTransformationPreview();
}

function bindSpanRemove() {
  $$("[data-span-list]").forEach((button) => { button.onclick = () => {
    if (button.dataset.spanList === "focal-list") state.focalSpans.splice(Number(button.dataset.index), 1);
    if (button.dataset.spanList === "oracle-list") state.oracleSpans.splice(Number(button.dataset.index), 1);
    if (button.dataset.spanList === "protected-list") state.protectedSpans.splice(Number(button.dataset.index), 1);
    if (button.dataset.spanList === "sham-list") state.shamSpan = null;
    invalidateTransformationPreview();
    renderSpanLists();
  }; });
}

function syncDelimiterRepairs() {
  const cards = $$('[data-repair-index]');
  if (!cards.length) return state.delimiterRepairs;
  state.delimiterRepairs = cards.map((card) => {
    const index = Number(card.dataset.repairIndex);
    return {
      arm: card.querySelector('[data-repair="arm"]').value,
      operation: card.querySelector('[data-repair="operation"]').value,
      deleted_syntax_span: state.delimiterRepairs[index].deleted_syntax_span,
      rationale: card.querySelector('[data-repair="rationale"]').value,
      human_selected: card.querySelector('[data-repair="human_selected"]').checked,
    };
  });
  return state.delimiterRepairs;
}

function delimiterRepairsMarkup() {
  const clean = state.active?.data?.packet?.case_profile?.case_type === "CLEAN_CONTROL";
  const arms = clean ? ["SHAM_BENIGN_EDIT"] : ["MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"];
  if (!state.delimiterRepairs.length) return '<p class="screenshot-meta">尚未选择；必须先在 exact history 中人工选中 whitelist syntax。</p>';
  return state.delimiterRepairs.map((repair, index) => `<div class="repair-card" data-repair-index="${index}"><div class="predicate-head"><b>Repair ${index + 1}</b><button type="button" class="remove-button" data-remove-repair="${index}">×</button></div><code>${escapeHtml(repair.deleted_syntax_span.exact_text)}</code><span>cp ${repair.deleted_syntax_span.char_start}–${repair.deleted_syntax_span.char_end}</span><div class="field-row"><label>Arm<select data-repair="arm"><option value="">— 人工选择 —</option>${arms.map((arm) => `<option ${repair.arm === arm ? "selected" : ""}>${arm}</option>`).join("")}</select></label><label>Operation<select data-repair="operation"><option value="">— 人工选择 —</option>${["DELETE_EMPTY_DELIMITER", "DELETE_ORPHAN_SEPARATOR"].map((operation) => `<option ${repair.operation === operation ? "selected" : ""}>${operation}</option>`).join("")}</select></label></div><label>为什么该语法在完整 arm edit 后为空/孤立<textarea data-repair="rationale">${escapeHtml(repair.rationale || "")}</textarea></label><label class="inline-check"><input type="checkbox" data-repair="human_selected" ${repair.human_selected ? "checked" : ""}>我逐项人工确认该 repair，未从其他 arm 自动复制</label></div>`).join("");
}

function renderDelimiterRepairs() {
  const root = $("#delimiter-repair-list");
  if (!root) return;
  root.innerHTML = delimiterRepairsMarkup();
  $$('[data-remove-repair]', root).forEach((button) => { button.onclick = () => {
    syncDelimiterRepairs();
    state.delimiterRepairs.splice(Number(button.dataset.removeRepair), 1);
    invalidateTransformationPreview();
    renderDelimiterRepairs();
  }; });
  $$('[data-repair]', root).forEach((input) => {
    input.oninput = invalidateTransformationPreview;
    input.onchange = invalidateTransformationPreview;
  });
}

function syncCorrectionCandidates() {
  const cards = $$("[data-correction-index]");
  if (!cards.length) return state.correctionCandidates;
  state.correctionCandidates = cards.map((card) => ({
    text: card.querySelector('[data-correction="text"]').value,
    rationale: card.querySelector('[data-correction="rationale"]').value,
    human_authored: card.querySelector('[data-correction="human_authored"]').checked,
  }));
  return state.correctionCandidates;
}

function correctionCandidatesMarkup() {
  if (!state.correctionCandidates.length) return '<p class="screenshot-meta">尚无候选；必须由 reviewer 自己写出一个或多个 evidence-supported correction。</p>';
  return state.correctionCandidates.map((candidate, index) => `<div class="repair-card" data-correction-index="${index}"><div class="predicate-head"><b>Correction candidate ${index + 1}</b><button type="button" class="remove-button" data-remove-correction="${index}">×</button></div><label>Exact correction bytes<textarea data-correction="text">${escapeHtml(candidate.text || "")}</textarea></label><label>为什么该候选由 pre-cutoff evidence 支持且只陈述事实<textarea data-correction="rationale">${escapeHtml(candidate.rationale || "")}</textarea></label><label class="inline-check"><input type="checkbox" data-correction="human_authored" ${candidate.human_authored ? "checked" : ""}>我本人撰写此候选；页面未生成、补全或排序语义</label></div>`).join("");
}

function renderCorrectionCandidates() {
  const root = $("#correction-candidate-list");
  if (!root) return;
  root.innerHTML = correctionCandidatesMarkup();
  $$('[data-remove-correction]', root).forEach((button) => { button.onclick = () => {
    syncCorrectionCandidates();
    state.correctionCandidates.splice(Number(button.dataset.removeCorrection), 1);
    state.transformationPreview = null;
    renderCorrectionCandidates();
    renderTransformationPreview();
  }; });
  $$('[data-correction]', root).forEach((input) => {
    input.oninput = invalidateTransformationPreview;
    input.onchange = invalidateTransformationPreview;
  });
}

function renderTransformationPreview() {
  const root = $("#transformation-preview");
  if (!root) return;
  const preview = state.transformationPreview;
  if (!preview?.preview_receipt_sha256) {
    root.innerHTML = '<p class="screenshot-meta">尚未生成。没有有效 preview receipt 时不能提交 ACCEPT。</p>';
    const selected = $("#correction-text");
    if (selected) selected.value = "";
    const confirmed = $("#preview-human-confirmed");
    if (confirmed) confirmed.disabled = true;
    return;
  }
  const ranking = preview.correction_ranking;
  const metrics = ranking ? `<table class="preview-table"><thead><tr><th>Rank</th><th>Correction</th><th>Tokens</th><th>UTF-8</th><th>Codepoints</th></tr></thead><tbody>${ranking.candidates.map((item) => `<tr><td>${item.rank}</td><td>${escapeHtml(item.text)}</td><td>${item.token_count}</td><td>${item.utf8_byte_count}</td><td>${item.codepoint_count}</td></tr>`).join("")}</tbody></table>` : '<p class="screenshot-meta">Clean control 无 correction ranking。</p>';
  const sham = preview.sham_token_match;
  const shamCheck = `<article class="preview-sham ${sham.matched ? "matched" : "blocked"}"><h4>Pinned-tokenizer sham check</h4><p>Focal ${sham.focal_token_count} tokens · Sham ${sham.sham_token_count} tokens · ${sham.matched ? "MATCHED ✓" : "NOT MATCHED — 必须重新人工选择 sham"}</p><code>${escapeHtml(sham.match_formula)}</code></article>`;
  const anchors = (preview.correction_anchors || []).map((item) => `<article class="preview-anchor"><b>${escapeHtml(item.binding_token)}</b><code>${escapeHtml(JSON.stringify(item.anchor))}</code></article>`).join("");
  const arms = preview.arms.map((arm) => `<article class="preview-arm"><header><b>${escapeHtml(arm.arm)}</b><span>${arm.target_only_diff ? "target-only ✓" : "blocked"} · ${arm.source_mapping_reversible ? "reversible ✓" : "blocked"}</span></header><pre>${escapeHtml(arm.human_diff)}</pre><details><summary>Rendered history + mappings</summary><pre>${escapeHtml(JSON.stringify({rendered_history: arm.rendered_history, diffs: arm.diffs, list_insertions: arm.list_insertions, source_mappings: arm.source_mappings}, null, 2))}</pre></details></article>`).join("");
  root.innerHTML = `<div class="preview-receipt"><b>CPU preview receipt</b><code>${escapeHtml(preview.preview_receipt_sha256)}</code><span>provider/network/GPU/replay/action = 0</span></div>${metrics}${shamCheck}${anchors}<div class="preview-arms">${arms}</div>`;
  const selected = $("#correction-text");
  if (selected) selected.value = ranking?.candidates?.[0]?.text || "";
  $("#preview-human-confirmed").disabled = !preview.acceptance_ready;
}

function transformationForm(packet, draft = {}) {
  state.focalSpans = structuredClone(draft.focal_target_spans || []);
  state.oracleSpans = structuredClone(draft.oracle_target_spans || []);
  state.protectedSpans = structuredClone(draft.protected_spans || []);
  state.shamSpan = structuredClone(draft.sham_span || null);
  state.delimiterRepairs = structuredClone(draft.delimiter_repairs || []);
  state.correctionCandidates = structuredClone(draft.correction_candidates || []);
  state.transformationPreview = null;
  const checks = ["same_role", "same_content_kind", "same_representation_class", "relative_third_matched", "same_record_preferred_or_depth_within_one", "token_size_matched", "no_entailment", "no_contradiction", "no_lexical_alias", "not_hard_task_requirement", "not_action_discriminant"];
  const correctionEvidence = packet.evidence.filter((item) => item.evidence_role !== "source_history").map((item) => `<label><input type="checkbox" data-correction-evidence="${item.evidence_token}" ${(draft.correction_evidence_ids || []).includes(item.evidence_token) ? "checked" : ""}>${escapeHtml(item.evidence_role)}</label>`).join("");
  const clean = packet.case_profile.case_type === "CLEAN_CONTROL";
  const strictControls = clean ? `<article class="form-card"><h3>Clean-control reference anchor</h3>${spanList("One benign focal/reference anchor", "focal-list", state.focalSpans)}<label class="inline-check"><input id="clean-anchor-confirmed" type="checkbox" ${draft.clean_control_reference_anchor_confirmed ? "checked" : ""}>我人工确认该 anchor 是 benign reference，不是 misleading premise</label></article>` : `<article class="form-card"><h3>Canonical target sets</h3>${spanList("Focal target set", "focal-list", state.focalSpans)}${spanList("Oracle target set", "oracle-list", state.oracleSpans)}</article><article class="form-card"><h3>Human-authored correction alternatives</h3><p class="screenshot-meta">网页不生成候选。所有候选都由 reviewer 自己填写；机械层只按 pinned tokenizer → UTF-8 bytes → codepoints → UTF-8 lexicographic 排序。</p><div id="correction-candidate-list">${correctionCandidatesMarkup()}</div><button type="button" id="add-correction-candidate" class="add-button">+ 人工新增候选</button><label>机械选中的 final correction（只读）<textarea id="correction-text" readonly>${escapeHtml(draft.correction_text || "")}</textarea></label><div class="check-grid">${correctionEvidence}</div><div class="check-grid"><label><input id="minimal-fact" type="checkbox" ${draft.correction_is_minimal_fact ? "checked" : ""}>每个候选只含最小事实</label><label><input id="no-advice" type="checkbox" ${draft.correction_contains_no_advice ? "checked" : ""}>不含建议</label><label><input id="preserve-history" type="checkbox" ${draft.oracle_preserves_non_target_history ? "checked" : ""}>保留非目标 history</label></div></article>`;
  const rawProtection = packet.case_profile.history_profile === "RAW_REPLAY" ? `<article class="form-card"><h3>Protected tool-call spans</h3>${spanList("Protected spans", "protected-list", state.protectedSpans)}<p class="screenshot-meta">必须包含 exact &lt;tool_call&gt; tag/payload；focal/oracle 不得重叠。</p></article>` : "<div id=\"protected-list\" hidden></div>";
  return `<section class="form-pane">${commonDisposition(draft, ["TARGET_SPAN_UNRESOLVED", "NO_VALID_CORRECTION", "NO_VALID_ORACLE_VIEW", "NO_MATCHED_SHAM"])}${strictControls}${rawProtection}<article class="form-card"><h3>Protocol-safe delimiter repairs（可选，多项/多 arm）</h3><p class="screenshot-meta">仅允许 Step N:/;（flat progress）或 Thought:/thinking tags（raw replay）；服务端会按每个 arm 完整重算 causal-empty。</p><div id="delimiter-repair-list">${delimiterRepairsMarkup()}</div></article><article class="form-card"><h3>Matched benign sham</h3>${spanList("Matched sham span", "sham-list", state.shamSpan ? [state.shamSpan] : [])}<div class="check-grid">${checks.map((key) => `<label><input type="checkbox" data-sham-check="${key}" ${draft.sham_match_checks?.[key] ? "checked" : ""}>${key.replaceAll("_", " ")}</label>`).join("")}</div></article><article class="form-card preview-card"><h3>G1.5 CPU-only arm preview</h3><p class="screenshot-meta">只读渲染；不会构造 provider client，不会发送请求，不会使用 GPU、replay 或执行 action。任何输入变化都会由服务端 hash 重算拦截。</p><button type="button" id="build-transformation-preview" class="primary-button">机械生成/重算 preview</button><div id="transformation-preview"></div><label class="inline-check"><input id="preview-human-confirmed" type="checkbox" disabled>我逐臂检查 correction anchors、target-only diff 与 reversible mapping</label></article><article class="form-card"><h3>Reviewer rationale</h3><textarea id="transform-rationale">${escapeHtml(draft.rationale || "")}</textarea></article></section>`;
}

function consistencyForm(draft = {}) {
  const labels = state.config.consistency_labels.map((value) => `<label class="choice-card"><input type="radio" name="consistency-label" value="${value}" ${draft.consistency_label === value ? "checked" : ""}><span>${value}</span></label>`).join("");
  return `<section class="form-pane"><article class="form-card"><h3>Original action consistency · descriptive only</h3><p class="screenshot-meta">无默认标签；逐项点击一个明确结论。</p><div id="consistency-labels" class="choice-grid">${labels}</div><label>History consistency rationale<textarea id="history-rationale">${escapeHtml(draft.history_consistency_rationale || "")}</textarea></label><label>GUI / task consistency rationale<textarea id="gui-rationale">${escapeHtml(draft.gui_task_consistency_rationale || "")}</textarea></label><label class="inline-check"><input id="no-replay-used" type="checkbox" ${draft.replay_response_used === false ? "checked" : ""}>确认未使用 replay treatment response</label><label class="inline-check"><input id="descriptive-only" type="checkbox" ${draft.descriptive_only ? "checked" : ""}>确认该标签仅描述，不进入 gold/admission/replay</label></article></section>`;
}

function adjudicationForm(data) {
  const adjudication = data.adjudication;
  const fieldResolutions = adjudication.disagreement_fields.map((field) => `<label>${escapeHtml(field)} 的明确裁决<textarea data-field-resolution="${escapeHtml(field)}" required placeholder="说明最终值及为何解决该 material disagreement；不得只写采用某一 reviewer"></textarea></label>`).join("");
  const packet = data.packet;
  const editor = packet.channel === "ACTION_GOLD" ? actionForm({}) : packet.channel === "TRANSFORMATION" ? transformationForm(packet, {}) : consistencyForm({});
  const editorBody = editor.replace(/^<section class="form-pane">/, "").replace(/<\/section>$/, "");
  return `<section class="form-pane"><article class="form-card"><h3>Material disagreement</h3><p>${adjudication.disagreement_fields.map((field) => `<span class="status-pill ADJUDICATION_REQUIRED">${escapeHtml(field)}</span>`).join(" ")}</p><div class="adjudication-columns"><div><h4>Primary · immutable reference</h4><pre class="review-json">${escapeHtml(JSON.stringify(adjudication.primary.payload, null, 2))}</pre></div><div><h4>Secondary · immutable reference</h4><pre class="review-json">${escapeHtml(JSON.stringify(adjudication.secondary.payload, null, 2))}</pre></div></div><p class="screenshot-meta">下面是空白、同通道的完整 workbench。必须逐项重新点击/填写；不会复制、合并或默认采用任一 peer。</p></article><div class="section-label">Independent resolved proposal</div>${editorBody}<article class="form-card"><div class="section-label">逐项 material resolution</div>${fieldResolutions}<label>整体 Adjudication rationale<textarea id="adjudication-rationale" required></textarea></label></article></section>`;
}

async function captureSelection(packet, button) {
  syncDelimiterRepairs();
  const recordIndex = Number(button.dataset.record);
  const record = packet.source_records[recordIndex];
  const pre = button.closest(".history-record").querySelector("pre");
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed || !pre.contains(selection.anchorNode) || !pre.contains(selection.focusNode)) throw new Error("请先在该 exact history record 中选中文本");
  const range = selection.getRangeAt(0);
  const prefixRange = document.createRange();
  prefixRange.selectNodeContents(pre);
  prefixRange.setEnd(range.startContainer, range.startOffset);
  const exact = range.toString();
  const prefix = prefixRange.toString();
  if (pre.textContent !== record.exact_text || !record.exact_text.startsWith(prefix) || record.exact_text.slice(prefix.length, prefix.length + exact.length) !== exact) throw new Error("浏览器 DOM 文本与 frozen exact bytes 不一致");
  const span = await spanFromOffsets(record, Array.from(prefix).length, Array.from(prefix + exact).length);
  if (button.dataset.target === "focal") uniqueSpanPush(state.focalSpans, span);
  if (button.dataset.target === "oracle") uniqueSpanPush(state.oracleSpans, span);
  if (button.dataset.target === "protected") uniqueSpanPush(state.protectedSpans, span);
  if (button.dataset.target === "sham") state.shamSpan = span;
  if (button.dataset.target === "repair") {
    state.delimiterRepairs.push({arm: "", operation: "", deleted_syntax_span: span, rationale: "", human_selected: false});
  }
  invalidateTransformationPreview();
  selection.removeAllRanges();
  renderSpanLists(true);
}

function bindSpanSelection(packet) {
  $$(".select-span").forEach((button) => { button.onclick = async () => { try { await captureSelection(packet, button); toast("已捕获 exact Unicode/UTF-8 span"); } catch (error) { toast(error.message, true); } }; });
  $$("[data-candidate]").forEach((button) => { button.onclick = () => {
    syncDelimiterRepairs();
    const candidate = packet.target_candidates[Number(button.dataset.candidate)];
    const span = {...candidate.selection_hint, record_id: candidate.record_id, human_selected: true};
    if (button.dataset.target === "oracle") uniqueSpanPush(state.oracleSpans, span); else uniqueSpanPush(state.focalSpans, span);
    invalidateTransformationPreview();
    renderSpanLists(true);
    toast("机械来源提示已由你人工确认");
  }; });
  bindSpanRemove();
}

function previewInputs(packet) {
  const clean = packet.case_profile.case_type === "CLEAN_CONTROL";
  const candidates = clean ? [] : syncCorrectionCandidates();
  const repairs = syncDelimiterRepairs();
  repairs.forEach((repair, index) => {
    if (!repair.arm || !repair.operation || !repair.rationale.trim() || !repair.human_selected) throw new Error(`Delimiter repair ${index + 1} 必须逐项选择 arm/operation、填写理由并人工确认`);
  });
  candidates.forEach((candidate, index) => {
    if (!candidate.text.trim() || !candidate.rationale.trim() || !candidate.human_authored) throw new Error(`Correction candidate ${index + 1} 必须填写 exact bytes、理由并人工确认`);
  });
  return {
    focal_target_spans: state.focalSpans,
    oracle_target_spans: clean ? [] : state.oracleSpans,
    correction_candidates: candidates,
    correction_evidence_ids: clean ? [] : $$('[data-correction-evidence]:checked').map((element) => element.dataset.correctionEvidence),
    protected_spans: packet.case_profile.history_profile === "RAW_REPLAY" ? state.protectedSpans : [],
    delimiter_repairs: repairs,
    sham_span: state.shamSpan,
  };
}

async function requestTransformationPreview() {
  try {
    const packet = state.active.data.packet;
    const response = await api("/api/transformation-previews", {
      method: "POST",
      body: JSON.stringify({assignment_id: state.active.assignmentId, preview_inputs: previewInputs(packet)}),
    });
    state.transformationPreview = response;
    $("#preview-human-confirmed").checked = false;
    renderTransformationPreview();
    toast("CPU-only preview 已机械生成；请逐臂检查后确认");
  } catch (error) {
    invalidateTransformationPreview();
    toast(error.message, true);
  }
}

function coordinatePoint(event, image, packet) {
  const bounds = image.getBoundingClientRect();
  if (!(bounds.width > 0 && bounds.height > 0)) throw new Error("截图尚未完成布局，请稍后重试");
  const renderedX = Math.min(bounds.width, Math.max(0, event.clientX - bounds.left));
  const renderedY = Math.min(bounds.height, Math.max(0, event.clientY - bounds.top));
  return {
    x: Math.round(renderedX * packet.current_screenshot.width / bounds.width),
    y: Math.round(renderedY * packet.current_screenshot.height / bounds.height),
    renderedX,
    renderedY,
    ratioX: renderedX / bounds.width,
    ratioY: renderedY / bounds.height,
  };
}

function normalizedCoordinateRegion(start, end) {
  return {
    x_min: Math.min(start.x, end.x),
    y_min: Math.min(start.y, end.y),
    x_max: Math.max(start.x, end.x),
    y_max: Math.max(start.y, end.y),
  };
}

function drawCoordinateSelection(start, end, anchor = false) {
  const overlay = $("#coordinate-selection");
  if (!overlay) return;
  overlay.style.left = `${100 * (anchor ? start.ratioX : Math.min(start.ratioX, end.ratioX))}%`;
  overlay.style.top = `${100 * (anchor ? start.ratioY : Math.min(start.ratioY, end.ratioY))}%`;
  overlay.style.width = anchor ? "8px" : `${100 * Math.abs(end.ratioX - start.ratioX)}%`;
  overlay.style.height = anchor ? "8px" : `${100 * Math.abs(end.ratioY - start.ratioY)}%`;
  overlay.style.transform = anchor ? "translate(-50%, -50%)" : "";
  overlay.classList.toggle("anchor", anchor);
  overlay.hidden = false;
}

function hideCoordinateSelection() {
  const overlay = $("#coordinate-selection");
  if (!overlay) return;
  overlay.hidden = true;
  overlay.classList.remove("anchor");
  for (const property of ("left top width height transform").split(" ")) overlay.style[property] = "";
}

function releaseCoordinateCapture(image, pointerId) {
  if (!image || pointerId === null || pointerId === undefined) return;
  try {
    if (typeof image.hasPointerCapture === "function" && image.hasPointerCapture(pointerId)) image.releasePointerCapture(pointerId);
  } catch (_error) {
    // The browser may already have released capture during pointer cancellation.
  }
}

function resetCoordinateGesture(message = null, error = false) {
  const image = $("#target-screenshot");
  const target = state.coordinateTarget;
  releaseCoordinateCapture(image, target?.pointerId);
  if (target) {
    target.pointerId = null;
    target.dragStart = null;
    target.firstCorner = null;
  }
  image?.closest(".screenshot-wrap")?.classList.remove("dragging");
  hideCoordinateSelection();
  if (message) toast(message, error);
}

function cancelCoordinatePicker(message = null, error = false) {
  const image = $("#target-screenshot");
  releaseCoordinateCapture(image, state.coordinateTarget?.pointerId);
  state.coordinateTarget = null;
  image?.closest(".screenshot-wrap")?.classList.remove("picking", "dragging");
  hideCoordinateSelection();
  if (message) toast(message, error);
}

function writeCoordinateRegion(start, end) {
  const target = state.coordinateTarget;
  if (!target) return;
  const values = normalizedCoordinateRegion(start, end);
  if (values.x_min === values.x_max || values.y_min === values.y_max) throw new Error("框选必须形成非空矩形");
  const card = $(`.predicate-card[data-index="${target.index}"]`);
  if (!card) throw new Error("目标 predicate 已变化，请重新进入框选模式");
  const prefix = target.target;
  const coordinateInputs = Object.fromEntries(["x_min", "y_min", "x_max", "y_max"].map((key) => [key, card.querySelector(`[data-p="${prefix}_${key}"]`)]));
  const shapeInput = card.querySelector(`[data-p="${prefix}_shape"]`);
  if (!shapeInput || Object.values(coordinateInputs).some((input) => !input)) throw new Error("目标 region 表单不完整，请重新选择 predicate");
  const primaryEmpty = Object.values(coordinateInputs).every((input) => input.value === "");
  if (primaryEmpty) {
    shapeInput.value = "BOUNDING_BOX";
    Object.entries(values).forEach(([key, value]) => { coordinateInputs[key].value = value; });
  } else {
    const additional = card.querySelector(`[data-p="${prefix}_additional_regions"]`);
    if (!additional) throw new Error("Additional region 表单不完整，请重新选择 predicate");
    const regions = JSON.parse(additional.value || "[]");
    if (!Array.isArray(regions)) throw new Error("Additional region set 必须是 JSON array");
    regions.push({shape: "BOUNDING_BOX", ...values});
    additional.value = JSON.stringify(regions, null, 2);
  }
  cancelCoordinatePicker();
  toast(`已写入人工框选 [${values.x_min},${values.y_min}]–[${values.x_max},${values.y_max}]`);
}

function bindCoordinatePicker(packet) {
  const image = $("#target-screenshot");
  if (!image) return;
  image.onpointerdown = (event) => {
    const target = state.coordinateTarget;
    if (!target || target.pointerId !== null || event.isPrimary === false || (event.pointerType === "mouse" && event.button !== 0)) return;
    try {
      const point = coordinatePoint(event, image, packet);
      target.pointerId = event.pointerId;
      target.dragStart = point;
      image.closest(".screenshot-wrap")?.classList.add("dragging");
      if (typeof image.setPointerCapture === "function") image.setPointerCapture(event.pointerId);
      drawCoordinateSelection(point, point);
      event.preventDefault();
    } catch (error) {
      cancelCoordinatePicker(error.message, true);
    }
  };
  image.onpointermove = (event) => {
    const target = state.coordinateTarget;
    if (!target || target.pointerId !== event.pointerId || !target.dragStart) return;
    drawCoordinateSelection(target.dragStart, coordinatePoint(event, image, packet));
    event.preventDefault();
  };
  image.onpointerup = (event) => {
    const target = state.coordinateTarget;
    if (!target || target.pointerId !== event.pointerId || !target.dragStart) return;
    try {
      const end = coordinatePoint(event, image, packet);
      const start = target.dragStart;
      const dragged = Math.hypot(end.renderedX - start.renderedX, end.renderedY - start.renderedY) >= COORDINATE_DRAG_THRESHOLD_PX;
      target.pointerId = null;
      target.dragStart = null;
      releaseCoordinateCapture(image, event.pointerId);
      image.closest(".screenshot-wrap")?.classList.remove("dragging");
      if (dragged) {
        target.firstCorner = null;
        writeCoordinateRegion(start, end);
      } else if (target.firstCorner) {
        const first = target.firstCorner;
        target.firstCorner = null;
        writeCoordinateRegion(first, end);
      } else {
        target.firstCorner = end;
        drawCoordinateSelection(end, end, true);
        toast(`第一个角点 (${end.x}, ${end.y}) 已记录；请点击对角点或重新拖拽`);
      }
      event.preventDefault();
    } catch (error) {
      cancelCoordinatePicker(error.message, true);
    }
  };
  image.onpointercancel = (event) => {
    if (state.coordinateTarget?.pointerId !== event.pointerId) return;
    resetCoordinateGesture("本次框选手势已取消，可直接重新拖拽", true);
  };
  image.onlostpointercapture = (event) => {
    if (state.coordinateTarget?.pointerId !== event.pointerId) return;
    resetCoordinateGesture("指针离开了框选区域，可直接重新拖拽", true);
  };
  image.onkeydown = (event) => {
    if (event.key !== "Escape" || !state.coordinateTarget) return;
    cancelCoordinatePicker("已取消框选");
    event.preventDefault();
  };
}

async function openAssignment(assignmentId, channel) {
  try {
    if (state.aiDecisionInFlight) throw new Error("候选选择正在记录，请完成后再切换任务");
    cancelCoordinatePicker();
    const query = new URLSearchParams();
    if (state.profile.role === "ADJUDICATOR") query.set("channel", channel);
    const binding = await api(`/api/assignments/${assignmentId}/binding${query.toString() ? `?${query}` : ""}`);
    state.pendingOpen = {assignmentId, channel, binding};
    $("#packet-binding-summary").innerHTML = [
      ["Role", roleLabel(binding.review_role)],
      ["Channel", binding.channel],
      ["Reviewer commitment", binding.reviewer_identity_sha256],
      ["Source packet", binding.source_packet_sha256],
      ["Assignment packet", binding.assignment_packet_sha256],
    ].map(([label, value]) => `<b>${escapeHtml(label)}</b><code>${escapeHtml(value)}</code>`).join("");
    const noticeLabels = {
      only_pre_cutoff_role_projected_evidence: "只显示当前 role 的 pre-cutoff evidence",
      peer_answers_hidden_before_adjudication: state.config.solo_first_pass ? "单人初筛不读取任何 formal peer answer" : "双审阶段看不到 peer answer",
      only_same_channel_finalized_peers_visible: "裁决只显示同一 channel 的两份 finalized peer proposal",
      post_state_outcome_replay_hidden: "post-state / outcome / replay 全部隐藏",
      whole_capsule_and_paths_hidden: "不暴露完整 capsule、路径或 store ID",
      provider_model_gpu_actions_unavailable: "无 provider / model / GPU / action 能力",
    };
    $("#packet-visibility-notice").innerHTML = Object.entries(binding.visibility_notice).filter(([, value]) => value).map(([key]) => `<div>${escapeHtml(noticeLabels[key] || key)}</div>`).join("");
    $("#packet-visibility-confirmed").checked = false;
    $("#packet-binding-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function loadAssignment(assignmentId, channel) {
  try {
    if (state.aiDecisionInFlight) throw new Error("候选选择正在记录，请完成后再切换任务");
    cancelCoordinatePicker();
    state.aiCandidates = null;
    state.aiReviewFeedback = null;
    state.formDirty = false;
    const query = new URLSearchParams();
    if (state.profile.role === "ADJUDICATOR") query.set("channel", channel);
    const data = await api(`/api/assignments/${assignmentId}/packet${query.toString() ? `?${query}` : ""}`);
    state.active = {assignmentId, channel, data};
    const packet = data.packet;
    $("#workbench-kicker").textContent = `${packet.channel} · ${packet.case_profile.history_profile} · BLIND PACKET`;
    $("#workbench-title").textContent = `标注任务 #${state.assignments.find((item) => item.assignment_id === assignmentId && item.channel === channel)?.ordinal || "—"}`;
    const draft = data.draft || {};
    const form = state.profile.role === "ADJUDICATOR" ? adjudicationForm(data) : packet.channel === "ACTION_GOLD" ? actionForm(draft) : packet.channel === "TRANSFORMATION" ? transformationForm(packet, draft) : consistencyForm(draft);
    $("#workbench-body").innerHTML = evidenceMarkup(packet) + form;
    $("#workbench-body").oninput = (event) => {
      if (event.target.closest(".ai-candidate-panel")) return;
      state.formDirty = true;
      $("#autosave-state").textContent = "未保存";
    };
    $("#workbench-body").onchange = (event) => {
      if (event.target.closest(".ai-candidate-panel")) return;
      state.formDirty = true;
      $("#autosave-state").textContent = "未保存";
    };
    bindOpenAdvancedButtons();
    hydrateExactHistory(packet);
    $("#save-draft").style.display = state.profile.role === "ADJUDICATOR" ? "none" : "inline-block";
    if (state.config.solo_first_pass) {
      $("#submit-review").textContent = state.config.first_pass_lock_open ? "锁定本阶段（非正式）" : "等待 G1.5 CPU codec gate";
      $("#submit-review").disabled = !state.config.first_pass_lock_open;
      $("#workbench-authority-copy").textContent = "锁定后不可覆盖；该记录明确不计独立 review，不进入裁决、formal export、admission 或 replay。";
    } else {
      $("#submit-review").textContent = state.profile.role === "ADJUDICATOR" ? "提交裁决" : state.config.formal_annotation_open ? "确认并提交" : "等待 G1.5 codec gate";
      $("#submit-review").disabled = !state.config.formal_annotation_open;
      $("#workbench-authority-copy").textContent = "提交后不可覆盖；material disagreement 将进入第三方 adjudication。";
    }
    if (packet.channel === "ACTION_GOLD") {
      renderPredicates();
      $("#add-predicate").onclick = () => { try { syncPredicatesExcept(); state.predicates.push({predicate_kind: "", action_type: "", evidence_ids: [], rationale: "", human_selected: false}); renderPredicates(); } catch (error) { toast(error.message, true); } };
      bindCoordinatePicker(packet);
      await loadAiCandidates(assignmentId);
    }
    if (packet.channel === "TRANSFORMATION") {
      bindSpanSelection(packet);
      renderDelimiterRepairs();
      renderCorrectionCandidates();
      renderTransformationPreview();
      const addCandidate = $("#add-correction-candidate");
      if (addCandidate) addCandidate.onclick = () => {
        syncCorrectionCandidates();
        state.correctionCandidates.push({text: "", rationale: "", human_authored: false});
        invalidateTransformationPreview();
        renderCorrectionCandidates();
      };
      $$('[data-correction-evidence]').forEach((input) => { input.onchange = invalidateTransformationPreview; });
      $("#build-transformation-preview").onclick = requestTransformationPreview;
    }
    $("#autosave-state").textContent = data.status?.own_state === "DRAFTING" ? "已加载草稿" : "未保存";
    $("#workbench-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function confirmPacketOpen(event) {
  event.preventDefault();
  if (!$("#packet-visibility-confirmed").checked) return toast("必须先确认 reviewer/digest/visibility 边界", true);
  const pending = state.pendingOpen;
  if (!pending) return toast("Packet preflight 已失效，请重新打开", true);
  $("#packet-binding-dialog").close();
  state.pendingOpen = null;
  await loadAssignment(pending.assignmentId, pending.channel);
}

function baseDisposition() {
  const disposition = $("#disposition").value;
  if (!disposition) throw new Error("必须明确选择 ACCEPT 或 EXCLUDE");
  return {disposition, exclusion_reason: disposition === "EXCLUDE" ? $("#exclusion-reason").value || null : null};
}

function collectPayload() {
  const packet = state.active.data.packet;
  if (packet.channel === "ACTION_GOLD") {
    const base = baseDisposition();
    state.predicates = $$(".predicate-card").map((card) => collectPredicate(card));
    return {proposal_kind: "ACTION_GOLD", ...base, predicates: base.disposition === "EXCLUDE" ? [] : state.predicates, evidence_rationale: $("#evidence-rationale").value, closed_world_confirmed: $("#closed-world").checked, all_reasonable_actions_enumerated: $("#all-actions").checked};
  }
  if (packet.channel === "TRANSFORMATION") {
    const base = baseDisposition();
    const clean = packet.case_profile.case_type === "CLEAN_CONTROL";
    if (base.disposition === "EXCLUDE") return {proposal_kind: "TRANSFORMATION", unit_kind: clean ? "CLEAN_CONTROL" : "STRICT_MHR", history_family: packet.case_profile.history_profile.toLowerCase(), ...base, focal_target_spans: [], oracle_target_spans: [], correction_candidates: [], correction_text: "", correction_evidence_ids: [], correction_is_minimal_fact: false, correction_contains_no_advice: false, oracle_preserves_non_target_history: false, protected_spans: [], delimiter_repairs: [], sham_span: null, sham_match_checks: null, clean_control_reference_anchor_confirmed: false, preview_receipt_sha256: null, preview_human_confirmed: false, rationale: $("#transform-rationale").value};
    const checks = {};
    $$("[data-sham-check]").forEach((element) => { checks[element.dataset.shamCheck] = element.checked; });
    const repairs = syncDelimiterRepairs();
    repairs.forEach((repair, index) => {
      if (!repair.arm || !repair.operation || !repair.rationale.trim() || !repair.human_selected) throw new Error(`Delimiter repair ${index + 1} 必须逐项选择 arm/operation、填写理由并人工确认`);
    });
    const candidates = clean ? [] : syncCorrectionCandidates();
    return {proposal_kind: "TRANSFORMATION", unit_kind: clean ? "CLEAN_CONTROL" : "STRICT_MHR", history_family: packet.case_profile.history_profile.toLowerCase(), ...base, focal_target_spans: state.focalSpans, oracle_target_spans: clean ? [] : state.oracleSpans, correction_candidates: candidates, correction_text: clean ? "" : $("#correction-text").value, correction_evidence_ids: clean ? [] : $$("[data-correction-evidence]:checked").map((element) => element.dataset.correctionEvidence), correction_is_minimal_fact: clean ? false : $("#minimal-fact").checked, correction_contains_no_advice: clean ? false : $("#no-advice").checked, oracle_preserves_non_target_history: clean ? false : $("#preserve-history").checked, protected_spans: packet.case_profile.history_profile === "RAW_REPLAY" ? state.protectedSpans : [], delimiter_repairs: repairs, sham_span: state.shamSpan, sham_match_checks: checks, clean_control_reference_anchor_confirmed: clean ? $("#clean-anchor-confirmed").checked : false, preview_receipt_sha256: state.transformationPreview?.preview_receipt_sha256 || null, preview_human_confirmed: $("#preview-human-confirmed").checked, rationale: $("#transform-rationale").value};
  }
  const selected = $('input[name="consistency-label"]:checked');
  if (!selected) throw new Error("必须明确点击一个 consistency label");
  return {proposal_kind: "CONSISTENCY_AUDIT", consistency_label: selected.value, history_consistency_rationale: $("#history-rationale").value, gui_task_consistency_rationale: $("#gui-rationale").value, replay_response_used: !$("#no-replay-used").checked, descriptive_only: $("#descriptive-only").checked};
}

async function persist(kind) {
  try {
    if (state.aiDecisionInFlight) throw new Error("候选选择正在记录，请完成后再保存或锁定");
    const payload = collectPayload();
    const path = state.config.solo_first_pass
      ? (kind === "draft" ? "/api/solo/draft" : "/api/solo/lock")
      : (kind === "draft" ? "/api/reviews/draft" : "/api/reviews/submit");
    await api(path, {method: "POST", body: JSON.stringify({assignment_id: state.active.assignmentId, payload})});
    state.formDirty = false;
    $("#autosave-state").textContent = kind === "draft" ? "草稿已追加" : state.config.solo_first_pass ? "初筛已锁" : "已提交";
    toast(kind === "draft" ? "草稿已追加到 hash-chain journal" : state.config.solo_first_pass ? "非正式第一遍已锁定（不计独立 review）" : "独立 review 已冻结");
    if (kind === "submit") { $("#workbench-dialog").close(); await loadAssignments(); }
  } catch (error) { toast(error.message, true); }
}

async function submitAdjudication() {
  try {
    const resolved_payload = collectPayload();
    const field_resolutions = {};
    $$('[data-field-resolution]').forEach((element) => { field_resolutions[element.dataset.fieldResolution] = element.value; });
    if (Object.values(field_resolutions).some((value) => !value.trim())) throw new Error("每个 material disagreement 都需要明确裁决");
    await api("/api/adjudications/submit", {method: "POST", body: JSON.stringify({assignment_id: state.active.assignmentId, channel: state.active.channel, resolved_payload, field_resolutions, rationale: $("#adjudication-rationale").value})});
    toast("裁决已冻结");
    $("#workbench-dialog").close();
    await loadAssignments();
  } catch (error) { toast(error.message, true); }
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => { button.onclick = () => { $$(".nav-item").forEach((item) => item.classList.toggle("active", item === button)); $$(".view").forEach((item) => item.classList.remove("active")); $(`#${button.dataset.view}-view`).classList.add("active"); $("#view-title").textContent = ({queue: "人工标注队列", guide: "标注准则", integrity: "完整性状态"})[button.dataset.view]; }; });
  $$("#status-filter button").forEach((button) => { button.onclick = () => { state.filter = button.dataset.status; $$("#status-filter button").forEach((item) => item.classList.toggle("selected", item === button)); renderAssignments(); }; });
  $("#assignment-search").oninput = renderAssignments;
}

async function boot() {
  state.config = await api("/api/config");
  $("#solo-first-pass-banner").hidden = !state.config.solo_first_pass;
  if (state.config.solo_first_pass) {
    $$(".formal-only").forEach((element) => { element.hidden = true; });
    $$(".solo-only").forEach((element) => { element.hidden = false; });
    $("#profile-role").textContent = "单人初筛 · 非正式";
    $("#view-kicker").textContent = "ALE-324 · G1.6 · SOLO FIRST PASS";
    $("#empty-queue-title").textContent = "选择当前单人初筛阶段";
    $("#empty-queue-copy").textContent = "同一真实身份按 Action → Transformation → Consistency 的全局顺序工作；所有结果均不计独立 review。";
    $("#profile-dialog-eyebrow").textContent = "单人非正式初筛身份";
    $("#profile-dialog-title").textContent = "选择当前开放的初筛阶段";
  }
  bindNavigation();
  const integrity = {
    ...state.config.readiness,
    ...state.config.safety,
    formal_annotation_open: state.config.formal_annotation_open,
    qwen_preview_tokenizer_available: state.config.preview_tokenizer_available?.qwen3vl_8b === true,
    mai_preview_tokenizer_available: state.config.preview_tokenizer_available?.mai_ui_8b === true,
  };
  $("#integrity-list").innerHTML = Object.entries(integrity).map(([key, value]) => `<div class="integrity-item"><span>${escapeHtml(key.replaceAll("_", " "))}</span><b>${String(value)}</b></div>`).join("");
  $("#change-profile").onclick = showProfileDialog;
  $("#profile-form").onsubmit = saveProfile;
  $("#cancel-profile").onclick = () => state.profile && $("#profile-dialog").close();
  $("#packet-binding-form").onsubmit = confirmPacketOpen;
  $("#cancel-packet-open").onclick = () => { state.pendingOpen = null; $("#packet-binding-dialog").close(); };
  $("#close-workbench").onclick = () => {
    if (state.aiDecisionInFlight) return toast("候选选择正在记录，请完成后再关闭", true);
    if (state.formDirty && !window.confirm("当前人工表单有未保存修改。确认关闭并丢弃这些未保存内容？")) return;
    cancelCoordinatePicker();
    state.formDirty = false;
    $("#workbench-dialog").close();
  };
  $("#workbench-dialog").oncancel = (event) => {
    if (state.aiDecisionInFlight) {
      event.preventDefault();
      toast("候选选择正在记录，请完成后再关闭", true);
      return;
    }
    if (state.formDirty && !window.confirm("当前人工表单有未保存修改。确认关闭并丢弃这些未保存内容？")) {
      event.preventDefault();
      return;
    }
    cancelCoordinatePicker();
    state.formDirty = false;
  };
  $("#save-draft").onclick = () => persist("draft");
  $("#submit-review").onclick = () => state.profile.role === "ADJUDICATOR" ? submitAdjudication() : persist("submit");
  showProfileDialog();
}

window.addEventListener("beforeunload", (event) => {
  if (!state.formDirty && !state.aiDecisionInFlight) return;
  event.preventDefault();
  event.returnValue = "";
});

boot().catch((error) => toast(error.message, true));
