/* Operant's workbench deliberately uses browser primitives only. */

const ROLE_ORDER = ["main", "planner", "explorer", "coder", "reviewer", "workflow"];
const ROLE_LABELS = {
  main: "Main",
  planner: "Planner",
  explorer: "Explorer",
  coder: "Coder",
  reviewer: "Reviewer",
  workflow: "Workflow",
};
const TOOL_NAMES = ["read_file", "search_files", "git_diff", "apply_patch", "run_command"];

const state = {
  models: [],
  roles: [],
  tasks: [],
  currentSession: null,
  selectedTask: null,
  taskTrace: null,
  activeTaskId: "",
  events: [],
  activeSessionId: "",
  abortController: null,
  approvals: new Map(),
};

function byId(id) {
  return document.getElementById(id);
}

function makeElement(tag, className = "", text = undefined) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function appendText(parent, tag, className, text) {
  const node = makeElement(tag, className, text);
  parent.append(node);
  return node;
}

function setNotice(message, kind = "") {
  const notice = byId("globalNotice");
  notice.textContent = message || "";
  notice.className = `notice${kind ? ` ${kind}` : ""}`;
}

function taskStatusClass(status) {
  if (["completed"].includes(status)) return "completed";
  if (["running"].includes(status)) return "running";
  if (["cancelled"].includes(status)) return "cancelled";
  if (["interrupted", "manual_reconcile_required", "failed"].includes(status)) return "failed";
  return "created";
}

function taskStatusText(status) {
  return {
    created: "未开始",
    running: "运行中",
    interrupted: "已中断",
    manual_reconcile_required: "需要人工核对",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status || "未知";
}

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.body !== undefined && typeof request.body !== "string") {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(path, request);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return payload;
}

function optionalNumber(id) {
  const value = byId(id).value.trim();
  return value === "" ? undefined : Number(value);
}

function fillSelect(select, items, label, selected = "") {
  const previous = selected || select.value;
  select.replaceChildren();
  if (label) {
    const placeholder = makeElement("option", "", label);
    placeholder.value = "";
    select.append(placeholder);
  }
  for (const item of items) {
    const option = makeElement("option", "", item.label);
    option.value = item.value;
    select.append(option);
  }
  const hasPrevious = [...select.options].some((option) => option.value === previous);
  if (hasPrevious) select.value = previous;
}

function modelById(id) {
  return state.models.find((model) => model.id === id);
}

function roleById(id) {
  return state.roles.find((role) => role.id === id);
}

function profileLabel(model) {
  return model ? `${model.name} · ${model.model_id}` : "未找到模型 Profile";
}

function appendTags(parent, values, className = "tag") {
  const row = makeElement("div", "tag-row");
  for (const value of values) appendText(row, "span", className, value);
  parent.append(row);
  return row;
}

function policyTags(policy) {
  const tags = (policy.allowed_tools || []).map((tool) => `tool:${tool}`);
  if (policy.workspace_write) tags.push("workspace 写入");
  if (policy.command_execution) tags.push("命令执行");
  if (!tags.length) tags.push("只读 / 无工具");
  return tags;
}

function renderModels() {
  const list = byId("modelsList");
  list.replaceChildren();
  byId("modelCount").textContent = String(state.models.length);
  if (!state.models.length) {
    list.className = "registry-list empty-state";
    list.textContent = "还没有 Model Profile。";
    return;
  }
  list.className = "registry-list";
  for (const model of state.models) {
    const card = makeElement("article", "registry-item");
    appendText(card, "h4", "", model.name);
    appendText(card, "p", "", `${model.provider} · ${model.model_id}`);
    appendText(card, "p", "", `Base URL：${model.base_url}`);
    appendText(card, "p", "", `Secret Ref：${model.secret_ref} · 默认 effort：${model.default_effort}`);
    appendTags(card, [`${(model.supported_efforts || []).join(" / ")}`, model.enabled ? "active" : "inactive"], model.enabled ? "tag tag-green" : "tag tag-red");
    list.append(card);
  }
}

function renderRoles() {
  const list = byId("rolesList");
  list.replaceChildren();
  byId("roleCount").textContent = String(state.roles.length);
  if (!state.roles.length) {
    list.className = "registry-list empty-state";
    list.textContent = "还没有 Role Preset。";
    return;
  }
  list.className = "registry-list";
  for (const role of state.roles) {
    const card = makeElement("article", "registry-item");
    appendText(card, "h4", "", `${role.name} · v${role.version}`);
    appendText(card, "p", "", `${profileLabel(modelById(role.model_profile_id))} · effort ${role.effort} · memory ${role.memory_scope}`);
    appendText(card, "p", "", role.system_prompt.length > 180 ? `${role.system_prompt.slice(0, 180)}…` : role.system_prompt);
    appendTags(card, policyTags(role.tool_policy), "tag");
    list.append(card);
  }
}

function roleOptions() {
  return state.roles.map((role) => ({ value: role.id, label: `${role.name} · ${role.id}` }));
}

function refreshSelectors() {
  const existingRole = byId("sessionRoleSelect").value || (roleById("role_main") ? "role_main" : "");
  const roleItems = roleOptions();
  fillSelect(byId("sessionRoleSelect"), roleItems, "请选择 Role Preset", existingRole);
  fillSelect(byId("roleModelProfile"), state.models.map((model) => ({ value: model.id, label: profileLabel(model) })), "请选择 Model Profile");
  fillSelect(byId("sessionModelOverride"), state.models.map((model) => ({ value: model.id, label: profileLabel(model) })), "使用角色默认模型");
  fillSelect(byId("workflowMainRole"), roleItems, "不使用 Main 汇总", roleById("role_main") ? "role_main" : "");
  fillSelect(byId("workflowPlannerRole"), roleItems, "请选择 Planner", roleById("role_planner") ? "role_planner" : "");
  fillSelect(byId("workflowCoderRole"), roleItems, "请选择 Coder", roleById("role_coder") ? "role_coder" : "");
  fillSelect(byId("workflowReviewerRole"), roleItems, "请选择 Reviewer", roleById("role_reviewer") ? "role_reviewer" : "");
  const explorer = byId("workflowExplorerRoles");
  const selectedExplorers = [...explorer.selectedOptions].map((option) => option.value);
  fillSelect(explorer, roleItems, "", selectedExplorers[0] || (roleById("role_explorer") ? "role_explorer" : ""));
  for (const option of explorer.options) option.selected = selectedExplorers.includes(option.value) || (!selectedExplorers.length && option.value === "role_explorer");
  updateRolePreview();
}

function readRoleForm() {
  const allowedTools = [...document.querySelectorAll('input[name="allowed-tool"]:checked')].map((input) => input.value);
  const approvalRequired = byId("roleApprovalRequired").value.split(",").map((item) => item.trim()).filter(Boolean);
  const budget = {
    max_turns: Number(byId("roleMaxTurns").value || 12),
    max_consecutive_test_failures: Number(byId("roleMaxFailures").value || 2),
    timeout_seconds: Number(byId("roleTimeout").value || 300),
  };
  const maxOutputTokens = optionalNumber("roleMaxOutputTokens");
  const maxCostUsd = optionalNumber("roleMaxCostUsd");
  if (maxOutputTokens !== undefined) budget.max_output_tokens = maxOutputTokens;
  if (maxCostUsd !== undefined) budget.max_cost_usd = maxCostUsd;
  return {
    name: byId("roleName").value.trim(),
    system_prompt: byId("rolePrompt").value.trim(),
    model_profile_id: byId("roleModelProfile").value,
    effort: byId("roleEffort").value,
    tool_policy: {
      allowed_tools: allowedTools,
      workspace_write: byId("roleWorkspaceWrite").checked,
      command_execution: byId("roleCommandExecution").checked,
      approval_required: approvalRequired,
    },
    budget,
    memory_scope: byId("roleMemoryScope").value.trim() || "session",
  };
}

function draftRole() {
  const payload = readRoleForm();
  return {
    ...payload,
    id: "instant-role",
    version: 1,
    status: "active",
    model_profile_id: payload.model_profile_id,
    created_at: new Date().toISOString(),
  };
}

function appendPreviewCell(parent, label, value) {
  const cell = makeElement("div", "preview-cell");
  appendText(cell, "span", "preview-label", label);
  appendText(cell, "span", "preview-value", value);
  parent.append(cell);
}

function renderRolePreview(role) {
  const preview = byId("rolePreview");
  preview.replaceChildren();
  if (!role) {
    preview.textContent = "请选择角色后，这里会显示 Prompt 摘要、模型、effort、权限和预算。";
    return;
  }
  const profile = modelById(role.model_profile_id);
  const grid = makeElement("div", "preview-grid");
  appendPreviewCell(grid, "角色", `${role.name} · v${role.version || 1}`);
  appendPreviewCell(grid, "模型", profileLabel(profile));
  appendPreviewCell(grid, "effort", role.effort);
  appendPreviewCell(grid, "Memory scope", role.memory_scope || "session");
  preview.append(grid);
  appendTags(preview, policyTags(role.tool_policy), "tag");
  const budget = role.budget || {};
  appendText(preview, "p", "prompt-summary", `Prompt 摘要：${role.system_prompt.length > 320 ? `${role.system_prompt.slice(0, 320)}…` : role.system_prompt}`);
  appendText(preview, "p", "muted", `预算：${budget.max_turns} turns · ${budget.timeout_seconds}s · 连续测试失败 ${budget.max_consecutive_test_failures} 次`);
}

function updateRolePreview() {
  const source = byId("sessionRoleSource").value;
  const editor = byId("instantRoleEditor");
  const existing = source === "existing" ? roleById(byId("sessionRoleSelect").value) : null;
  editor.hidden = source !== "instant";
  byId("sessionRoleSelectLabel").hidden = source !== "existing";
  if (source === "instant") {
    renderRolePreview(byId("roleName").value || byId("rolePrompt").value ? draftRole() : null);
  } else {
    renderRolePreview(existing);
  }
}

function currentSessionSnapshot(session) {
  const snapshot = session.role_snapshot;
  const panel = byId("currentSession");
  panel.replaceChildren();
  panel.className = "snapshot";
  const grid = makeElement("div", "snapshot-grid");
  appendPreviewCell(grid, "Session", session.id);
  appendPreviewCell(grid, "角色", `${snapshot.role_name} · v${snapshot.role_version}`);
  appendPreviewCell(grid, "模型", `${snapshot.model_profile_name} · ${snapshot.model_id}`);
  appendPreviewCell(grid, "effort", snapshot.effort);
  panel.append(grid);
  appendTags(panel, policyTags(snapshot.tool_policy), "tag");
  appendText(panel, "p", "prompt-summary", `Prompt 摘要：${snapshot.system_prompt.length > 320 ? `${snapshot.system_prompt.slice(0, 320)}…` : snapshot.system_prompt}`);
  appendText(panel, "p", "muted", `预算：${snapshot.budget.max_turns} turns · ${snapshot.budget.timeout_seconds}s · memory ${snapshot.memory_scope}`);
  byId("currentSessionStatus").textContent = "已创建";
  byId("currentSessionStatus").className = "status-pill status-created";
  byId("runSessionButton").disabled = false;
  byId("cancelSessionButton").disabled = false;
}

function taskLabel(task) {
  const text = task.task || "未命名任务";
  return text.length > 100 ? `${text.slice(0, 100)}…` : text;
}

function appendTaskCell(parent, label, value) {
  const cell = makeElement("div", "preview-cell");
  appendText(cell, "span", "preview-label", label);
  appendText(cell, "span", "preview-value", value);
  parent.append(cell);
}

function renderTaskTrace(trace) {
  const panel = byId("taskTrace");
  panel.replaceChildren();
  if (!trace) {
    panel.className = "task-trace empty-state";
    panel.textContent = "选择任务后显示任务级 Trace。";
    return;
  }
  panel.className = "task-trace";
  const grid = makeElement("div", "trace-grid");
  const fields = [
    ["状态", taskStatusText(trace.status)],
    ["当前阶段", trace.current_stage || "—"],
    ["Workflow 事件", trace.workflow_event_count],
    ["子任务数", trace.subtask_count],
    ["Session 数", trace.session_count],
    ["模型调用", trace.model_calls],
    ["工具调用", trace.tool_calls],
    ["工具失败", trace.tool_failures],
    ["返工轮数", trace.correction_rounds],
    ["耗时（毫秒）", trace.duration_ms],
    ["Prompt tokens", trace.prompt_tokens ?? "—"],
    ["Completion tokens", trace.completion_tokens ?? "—"],
  ];
  for (const [label, value] of fields) appendTaskCell(grid, label, value ?? "—");
  panel.append(grid);
  if (trace.resumed_from_id) appendText(panel, "p", "muted", `恢复自任务：${trace.resumed_from_id}`);
  if (trace.final_verdict) appendText(panel, "p", "muted", `最终 verdict：${trace.final_verdict}`);
  if (trace.error_types && trace.error_types.length) {
    const errors = makeElement("div", "trace-errors");
    appendText(errors, "span", "preview-label", "错误类型");
    appendTags(errors, trace.error_types, "tag tag-red");
    panel.append(errors);
  }
  const safeTrace = { ...trace };
  delete safeTrace.workflow_run_id;
  appendText(panel, "code", "trace-json", JSON.stringify(safeTrace, null, 2));
}

function renderTaskDetail(task) {
  const panel = byId("taskDetail");
  panel.replaceChildren();
  if (!task) {
    panel.className = "task-detail empty-state";
    panel.textContent = "选择任务后显示状态、阶段、工作区和恢复信息。";
    byId("resumeTaskButton").disabled = true;
    byId("cancelTaskButton").disabled = true;
    byId("refreshTraceButton").disabled = true;
    return;
  }
  panel.className = "task-detail";
  const grid = makeElement("div", "task-detail-grid");
  appendTaskCell(grid, "任务 ID", task.id);
  appendTaskCell(grid, "状态", taskStatusText(task.status));
  appendTaskCell(grid, "当前阶段", task.current_stage || "—");
  appendTaskCell(grid, "Workspace", task.workspace);
  appendTaskCell(grid, "创建时间", task.created_at || "—");
  appendTaskCell(grid, "更新时间", task.updated_at || "—");
  panel.append(grid);
  appendText(panel, "p", "prompt-summary", `任务：${task.task}`);
  if (task.resumed_from_id) appendText(panel, "p", "muted", `恢复自：${task.resumed_from_id}`);
  if (task.last_error_type) appendText(panel, "p", "muted", `最近错误：${task.last_error_type}`);
  const resumable = ["interrupted", "manual_reconcile_required"].includes(task.status);
  const cancellable = !["completed", "failed", "cancelled"].includes(task.status);
  byId("resumeTaskButton").disabled = !resumable;
  byId("cancelTaskButton").disabled = !cancellable;
  byId("refreshTraceButton").disabled = false;
}

function renderTasks() {
  const list = byId("tasksList");
  const select = byId("taskSelect");
  list.replaceChildren();
  select.replaceChildren();
  byId("taskSelect").append(makeElement("option", "", state.tasks.length ? "选择一个任务" : "暂无任务"));
  byId("taskSelect").options[0].value = "";
  if (!state.tasks.length) {
    list.className = "tasks-list empty-state";
    list.textContent = "还没有持久化任务。";
    return;
  }
  list.className = "tasks-list";
  for (const task of state.tasks) {
    const item = makeElement("button", "task-item", undefined);
    item.type = "button";
    if (task.id === state.activeTaskId) item.classList.add("selected");
    appendText(item, "h4", "", taskLabel(task));
    appendText(item, "p", "", `${taskStatusText(task.status)} · ${task.current_stage || "created"}`);
    appendText(item, "p", "", `${task.id} · ${task.workspace}`);
    item.addEventListener("click", () => selectTask(task.id));
    list.append(item);
    const option = makeElement("option", "", `${taskLabel(task)} · ${taskStatusText(task.status)}`);
    option.value = task.id;
    if (task.id === state.activeTaskId) option.selected = true;
    select.append(option);
  }
}

function normaliseTaskEvent(event) {
  return {
    ...event,
    role: event.role || "workflow",
    session_id: event.session_id || "",
    event_type: event.event_type,
    payload: event.payload || {},
  };
}

async function selectTask(taskId) {
  if (!taskId) {
    state.selectedTask = null;
    state.taskTrace = null;
    state.activeTaskId = "";
    state.events = [];
    renderTasks();
    renderTaskDetail(null);
    renderTaskTrace(null);
    renderEventBoard();
    return;
  }
  try {
    const [task, events, trace] = await Promise.all([
      api(`/v1/tasks/${encodeURIComponent(taskId)}`),
      api(`/v1/tasks/${encodeURIComponent(taskId)}/events`),
      api(`/v1/tasks/${encodeURIComponent(taskId)}/trace`),
    ]);
    state.activeTaskId = task.id;
    state.selectedTask = task;
    state.taskTrace = trace;
    state.events = events.map(normaliseTaskEvent);
    renderTasks();
    renderTaskDetail(task);
    renderTaskTrace(trace);
    renderEventBoard();
    setNotice(`已加载任务 ${task.id} 的事件和 Trace。`, "");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function loadTasks(preferredTaskId = state.activeTaskId) {
  try {
    state.tasks = await api("/v1/tasks");
    renderTasks();
    const taskId = preferredTaskId && state.tasks.some((task) => task.id === preferredTaskId)
      ? preferredTaskId
      : state.tasks[0]?.id;
    if (taskId) {
      await selectTask(taskId);
    } else {
      state.selectedTask = null;
      state.taskTrace = null;
      renderTaskDetail(null);
      renderTaskTrace(null);
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function refreshTaskTrace() {
  if (!state.activeTaskId) return;
  try {
    state.taskTrace = await api(`/v1/tasks/${encodeURIComponent(state.activeTaskId)}/trace`);
    renderTaskTrace(state.taskTrace);
    setNotice("任务级 Trace 已刷新。", "");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function resumeTask() {
  if (!state.activeTaskId) return setNotice("请先选择任务。", "error");
  await startStream(
    `/v1/tasks/${encodeURIComponent(state.activeTaskId)}/resume`,
    { allow_coder_replay: byId("allowCoderReplay").checked },
    "恢复任务",
    { taskStream: true },
  );
}

async function cancelTask() {
  if (!state.activeTaskId) return setNotice("请先选择任务。", "error");
  try {
    const result = await api(`/v1/tasks/${encodeURIComponent(state.activeTaskId)}/cancel`, { method: "POST" });
    setNotice(result.accepted ? "已请求取消任务。" : "该任务当前不可取消。", "info");
    await loadTasks(state.activeTaskId);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function rolePayloadValid(payload) {
  if (!payload.name || !payload.system_prompt || !payload.model_profile_id) {
    throw new Error("即时角色需要名称、System Prompt 和 Model Profile");
  }
  if (!modelById(payload.model_profile_id)) throw new Error("请选择有效的 Model Profile");
  return payload;
}

function sessionOptions() {
  const body = {};
  const source = byId("sessionRoleSource").value;
  if (source === "existing") {
    if (!byId("sessionRoleSelect").value) throw new Error("请选择 Role Preset");
    body.role_id = byId("sessionRoleSelect").value;
  } else {
    body.new_role = rolePayloadValid(readRoleForm());
  }
  if (byId("sessionModelOverride").value) body.model_profile_id = byId("sessionModelOverride").value;
  if (byId("sessionEffortOverride").value) body.effort = byId("sessionEffortOverride").value;
  const budgetOverrides = {};
  const timeout = optionalNumber("sessionTimeoutOverride");
  const turns = optionalNumber("sessionTurnsOverride");
  if (timeout !== undefined) budgetOverrides.timeout_seconds = timeout;
  if (turns !== undefined) budgetOverrides.max_turns = turns;
  if (Object.keys(budgetOverrides).length) body.budget_overrides = budgetOverrides;
  return body;
}

async function createSession(event) {
  event.preventDefault();
  try {
    const session = await api("/v1/sessions", { method: "POST", body: sessionOptions() });
    state.currentSession = session;
    state.activeSessionId = session.id;
    currentSessionSnapshot(session);
    setNotice(`Session ${session.id} 已创建。`, "");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function saveRole() {
  try {
    const role = await api("/v1/roles", { method: "POST", body: rolePayloadValid(readRoleForm()) });
    setNotice(`Role Preset ${role.name} 已保存。`, "");
    await loadRegistry();
    byId("sessionRoleSource").value = "existing";
    byId("sessionRoleSelect").value = role.id;
    updateRolePreview();
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function useInstantRole() {
  try {
    const payload = sessionOptions();
    const session = await api("/v1/sessions", { method: "POST", body: payload });
    state.currentSession = session;
    state.activeSessionId = session.id;
    currentSessionSnapshot(session);
    setNotice(`即时角色 Session ${session.id} 已创建。`, "");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function createModel(event) {
  event.preventDefault();
  const body = {
    name: byId("modelName").value.trim(),
    model_id: byId("modelId").value.trim(),
    base_url: byId("modelBaseUrl").value.trim(),
    secret_ref: byId("modelSecretRef").value.trim(),
    default_effort: byId("modelDefaultEffort").value,
  };
  const contextWindow = optionalNumber("modelContextWindow");
  const tokenBudget = optionalNumber("modelTokenBudget");
  if (contextWindow !== undefined) body.context_window = contextWindow;
  if (tokenBudget !== undefined) body.default_token_budget = tokenBudget;
  try {
    const model = await api("/v1/models", { method: "POST", body });
    setNotice(`Model Profile ${model.name} 已保存。`, "");
    byId("modelForm").reset();
    byId("modelSecretRef").value = "OPERANT_API_KEY";
    await loadRegistry();
    byId("roleModelProfile").value = model.id;
    updateRolePreview();
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function parseResult(value) {
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch (_error) { return value; }
}

function eventRole(event) {
  if (event.role) return event.role;
  if (state.currentSession && event.session_id === state.currentSession.id) return "main";
  return "workflow";
}

function eventSessionId(event) {
  return event.session_id || (state.currentSession ? state.currentSession.id : "");
}

function normaliseSseEvent(sseType, payload) {
  if (!payload || typeof payload !== "object") return { role: "workflow", session_id: "", event_type: sseType, payload: {} };
  if (payload.event_type && payload.role !== undefined) return payload;
  return { role: "main", session_id: eventSessionId(payload), event_type: payload.event_type || sseType, payload: payload.payload || {} };
}

function statusForRole(role) {
  const relevant = state.events.filter((event) => event.role === role);
  let status = "created";
  for (const event of relevant) {
    if (event.event_type === "agent.started") status = "running";
    if (["agent.completed", "workflow.completed"].includes(event.event_type)) status = "completed";
    if (["agent.failed", "agent.no_progress", "agent.max_turns", "workflow.failed", "workflow.review_verdict_missing", "workflow.rework_limit_reached"].includes(event.event_type)) status = "failed";
    if (event.event_type === "agent.cancelled") status = "cancelled";
    if (event.event_type === "agent.timed_out") status = "timed_out";
  }
  return status;
}

function statusText(status) {
  return { created: "未开始", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时" }[status] || status;
}

function kindForEvent(eventType, payload) {
  if (eventType.includes("approval")) return "审批请求";
  if (eventType.startsWith("tool.")) return "工具调用";
  if (eventType.includes("test")) return "测试";
  if (eventType.includes("diff") || eventType.includes("result") || eventType.includes("completed")) return "diff / 结果";
  if (payload && (payload.diff || payload.result)) return "diff / 结果";
  return "事件";
}

function appendResultBlock(card, title, value) {
  const block = makeElement("div", "result-block");
  appendText(block, "strong", "", title);
  const pre = makeElement("pre", "", typeof value === "string" ? value : JSON.stringify(value, null, 2));
  block.append(pre);
  card.append(block);
}

async function decideApproval(sessionId, toolCallId, approved) {
  const key = `${sessionId}:${toolCallId}`;
  try {
    await api(`/v1/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(toolCallId)}`, {
      method: "POST", body: { approved },
    });
    state.approvals.set(key, approved ? "已批准" : "已拒绝");
    setNotice(`${approved ? "已批准" : "已拒绝"}工具调用 ${toolCallId}。`, "");
    renderEventBoard();
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function appendApproval(card, event) {
  const payload = event.payload || {};
  const sessionId = eventSessionId(event);
  const toolCallId = String(payload.tool_call_id || "");
  const approval = makeElement("div", "approval-card");
  appendText(approval, "strong", "", "审批请求");
  appendText(approval, "p", "", `${payload.name || "tool"} · ${payload.category || "unknown"}`);
  appendText(approval, "p", "", payload.detail || "该工具调用需要人工确认。");
  const decision = state.approvals.get(`${sessionId}:${toolCallId}`);
  if (decision) {
    appendText(approval, "p", "", decision);
  } else {
    const row = makeElement("div", "button-row");
    const approve = makeElement("button", "button button-primary", "批准");
    approve.type = "button";
    approve.addEventListener("click", () => decideApproval(sessionId, toolCallId, true));
    const deny = makeElement("button", "button button-danger", "拒绝");
    deny.type = "button";
    deny.addEventListener("click", () => decideApproval(sessionId, toolCallId, false));
    row.append(approve, deny);
    approval.append(row);
  }
  card.append(approval);
}

function appendEventDetails(card, event) {
  const payload = event.payload || {};
  const result = parseResult(payload.result);
  if (event.event_type === "tool.started") appendText(card, "p", "", `工具：${payload.name || "unknown"} · call ${payload.tool_call_id || ""}`);
  if (event.event_type === "tool.approval_required") appendApproval(card, event);
  if (event.event_type === "test.failure_feedback") appendResultBlock(card, "测试反馈", payload.summary || payload);
  if (event.event_type === "model.completed" && payload.content) appendResultBlock(card, "模型结果", payload.content);
  if (event.event_type === "workflow.subtask_result" && payload.result) appendResultBlock(card, "子任务结果", payload.result);
  if (event.event_type.startsWith("tool.") && payload.result !== undefined) {
    if (result && typeof result === "object" && result.argv) appendResultBlock(card, "测试 / 命令结果", result);
    if (result && typeof result === "object" && result.diff !== undefined) appendResultBlock(card, "diff / 结果", result.diff);
    if (result && typeof result === "object" && result.test_failure) appendResultBlock(card, "测试反馈", result.test_failure);
    if (typeof result === "string") appendResultBlock(card, "工具结果", result);
  }
  const compactPayload = { ...payload };
  delete compactPayload.result;
  if (Object.keys(compactPayload).length) appendText(card, "code", "event-payload", JSON.stringify(compactPayload, null, 2));
}

function eventCard(event) {
  const card = makeElement("article", "event-card");
  appendText(card, "span", "event-kind", kindForEvent(event.event_type, event.payload));
  appendText(card, "h4", "", event.event_type);
  const turn = event.payload && event.payload.turn !== undefined ? event.payload.turn : event.turn;
  if (turn !== undefined) appendText(card, "p", "", `turn ${turn} · session ${event.session_id || "workflow"}`);
  appendEventDetails(card, event);
  return card;
}

function renderEventBoard() {
  const board = byId("eventBoard");
  board.replaceChildren();
  if (!state.events.length) {
    board.append(makeElement("div", "empty-state", "运行任务后，Main / Planner / Explorer / Coder / Reviewer 的事件会按角色显示在这里。"));
    return;
  }
  for (const role of ROLE_ORDER) {
    const column = makeElement("section", "role-column");
    const heading = makeElement("h3");
    appendText(heading, "span", "", ROLE_LABELS[role]);
    const status = statusForRole(role);
    appendText(heading, "span", `status-pill status-${status}`, statusText(status));
    column.append(heading);
    const list = makeElement("div", "event-list");
    const events = state.events.filter((event) => event.role === role).slice(-60);
    if (!events.length) appendText(list, "p", "muted", "暂无事件");
    for (const event of events) list.append(eventCard(event));
    column.append(list);
    board.append(column);
  }
}

function receiveEvent(sseType, data) {
  let payload;
  try { payload = JSON.parse(data); } catch (_error) {
    payload = { event_type: sseType, payload: { text: data } };
  }
  const event = normaliseSseEvent(sseType, payload);
  event.role = event.role || eventRole(event);
  event.session_id = event.session_id || eventSessionId(event);
  if (event.workflow_run_id) state.activeTaskId = event.workflow_run_id;
  state.activeSessionId = event.session_id || state.activeSessionId;
  state.events.push(event);
  renderEventBoard();
  if (event.role === "main" && event.event_type === "agent.completed") {
    byId("currentSessionStatus").textContent = "已完成";
    byId("currentSessionStatus").className = "status-pill status-completed";
  }
}

async function consumeSse(response) {
  if (!response.body) throw new Error("服务器没有返回可读的 SSE 流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consumeBlock = (block) => {
    if (!block.trim()) return;
    let eventType = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length) receiveEvent(eventType, data.join("\n"));
  };
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) consumeBlock(block);
  }
  buffer += decoder.decode();
  consumeBlock(buffer);
}

async function startStream(path, body, kind, options = {}) {
  if (state.abortController) state.abortController.abort();
  state.abortController = new AbortController();
  state.events = [];
  state.approvals.clear();
  renderEventBoard();
  setNotice(`${kind} 已启动，正在接收 SSE 事件。`, "info");
  byId("runSessionButton").disabled = true;
  byId("runWorkflowButton").disabled = true;
  byId("cancelSessionButton").disabled = false;
  byId("cancelWorkflowButton").disabled = false;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal: state.abortController.signal,
    });
    if (!response.ok) {
      const contentType = response.headers.get("content-type") || "";
      const errorBody = contentType.includes("application/json") ? await response.json() : await response.text();
      throw new Error(errorBody.detail || errorBody || `启动失败（${response.status}）`);
    }
    await consumeSse(response);
    if (options.taskStream && state.activeTaskId) await loadTasks(state.activeTaskId);
    setNotice(`${kind} 事件流已结束。`, "");
  } catch (error) {
    if (error.name === "AbortError") setNotice(`${kind} 的浏览器连接已取消。`, "info");
    else setNotice(error.message, "error");
  } finally {
    state.abortController = null;
    byId("runSessionButton").disabled = !state.currentSession;
    byId("runWorkflowButton").disabled = false;
  }
}

async function runSession(event) {
  event.preventDefault();
  if (!state.currentSession) return setNotice("请先创建 Session。", "error");
  const message = byId("sessionMessage").value.trim();
  const workspace = byId("sessionWorkspace").value.trim();
  if (!message || !workspace) return setNotice("消息和 Workspace 不能为空。", "error");
  state.activeSessionId = state.currentSession.id;
  await startStream(`/v1/sessions/${encodeURIComponent(state.currentSession.id)}/runs`, { message, workspace }, "单 Session");
}

function selectedValues(select) {
  return [...select.selectedOptions].map((option) => option.value).filter(Boolean);
}

async function runWorkflow(event) {
  event.preventDefault();
  const body = {
    task: byId("workflowTask").value.trim(),
    workspace: byId("workflowWorkspace").value.trim(),
    main_role_id: byId("workflowMainRole").value || null,
    planner_role_id: byId("workflowPlannerRole").value,
    explorer_role_ids: selectedValues(byId("workflowExplorerRoles")),
    coder_role_id: byId("workflowCoderRole").value,
    reviewer_role_id: byId("workflowReviewerRole").value,
    max_parallel_explorers: Number(byId("workflowMaxParallel").value || 2),
    max_rework_rounds: Number(byId("workflowMaxRework").value || 1),
  };
  if (!body.task || !body.workspace || !body.planner_role_id || !body.coder_role_id || !body.reviewer_role_id) {
    return setNotice("Workflow 需要任务、Workspace、Planner、Coder 和 Reviewer。", "error");
  }
  await startStream("/v1/workflows/coding/runs", body, "coding Workflow", { taskStream: true });
}

async function cancelActive() {
  const sessionId = state.activeSessionId || (state.currentSession && state.currentSession.id);
  if (!sessionId) return setNotice("没有可以取消的 Session。", "error");
  try {
    const result = await api(`/v1/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: "POST" });
    setNotice(result.accepted ? `已请求取消 Session ${sessionId}。` : "该 Session 当前没有运行中的任务。", "info");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function loadRegistry() {
  try {
    const [models, roles] = await Promise.all([api("/v1/models"), api("/v1/roles")]);
    state.models = models;
    state.roles = roles;
    renderModels();
    renderRoles();
    refreshSelectors();
    setNotice("注册表已刷新。", "");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function bindEvents() {
  byId("refreshButton").addEventListener("click", loadRegistry);
  byId("refreshTasksButton").addEventListener("click", () => loadTasks(state.activeTaskId));
  byId("modelForm").addEventListener("submit", createModel);
  byId("sessionForm").addEventListener("submit", createSession);
  byId("saveRoleButton").addEventListener("click", saveRole);
  byId("useInstantRoleButton").addEventListener("click", useInstantRole);
  byId("sessionRoleSource").addEventListener("change", updateRolePreview);
  byId("sessionRoleSelect").addEventListener("change", updateRolePreview);
  for (const id of ["roleName", "rolePrompt", "roleModelProfile", "roleEffort", "roleMemoryScope", "roleWorkspaceWrite", "roleCommandExecution", "roleApprovalRequired", "roleMaxTurns", "roleMaxFailures", "roleTimeout", "roleMaxOutputTokens", "roleMaxCostUsd"]) {
    byId(id).addEventListener("input", updateRolePreview);
    byId(id).addEventListener("change", updateRolePreview);
  }
  for (const input of document.querySelectorAll('input[name="allowed-tool"]')) input.addEventListener("change", updateRolePreview);
  byId("sessionRunForm").addEventListener("submit", runSession);
  byId("workflowForm").addEventListener("submit", runWorkflow);
  byId("cancelSessionButton").addEventListener("click", cancelActive);
  byId("cancelWorkflowButton").addEventListener("click", cancelActive);
  byId("taskSelect").addEventListener("change", (event) => selectTask(event.target.value));
  byId("resumeTaskButton").addEventListener("click", resumeTask);
  byId("cancelTaskButton").addEventListener("click", cancelTask);
  byId("refreshTraceButton").addEventListener("click", refreshTaskTrace);
}

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  updateRolePreview();
  loadRegistry();
  loadTasks();
});
