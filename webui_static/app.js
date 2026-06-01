const state = {
  selectedTaskId: null,
  activeTab: "result",
  providers: [],
  pollTimer: null,
  toastTimer: null,
};

const PREFS_VERSION = 2;

const els = {
  statusText: document.getElementById("statusText"),
  summaryChips: document.getElementById("summaryChips"),
  queueMeta: document.getElementById("queueMeta"),
  providerSelector: document.getElementById("providerSelector"),
  questionInput: document.getElementById("questionInput"),
  browserSelect: document.getElementById("browserSelect"),
  timeoutInput: document.getElementById("timeoutInput"),
  parallelInput: document.getElementById("parallelInput"),
  showBrowserCheckbox: document.getElementById("showBrowserCheckbox"),
  queueTable: document.getElementById("queueTable"),
  providerStateTable: document.getElementById("providerStateTable"),
  detailTitle: document.getElementById("detailTitle"),
  detailSubtitle: document.getElementById("detailSubtitle"),
  resultText: document.getElementById("resultText"),
  logText: document.getElementById("logText"),
  resultPane: document.getElementById("resultPane"),
  logPane: document.getElementById("logPane"),
  tabResult: document.getElementById("tabResult"),
  tabLog: document.getElementById("tabLog"),
  openSessionsBtn: document.getElementById("openSessionsBtn"),
  enqueueBtn: document.getElementById("enqueueBtn"),
  forceStopBtn: document.getElementById("forceStopBtn"),
  clearQueueBtn: document.getElementById("clearQueueBtn"),
  closeSessionsBtn: document.getElementById("closeSessionsBtn"),
  toast: document.getElementById("toast"),
};

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem("theory-answer-webui-prefs") || "{}");
    if (saved.browser) {
      els.browserSelect.value = saved.browser;
    }
    if (saved.timeout) {
      els.timeoutInput.value = String(saved.timeout);
    }
    if (saved.parallelTasks) {
      els.parallelInput.value = String(saved.parallelTasks);
    }
    if (saved.prefsVersion === PREFS_VERSION && typeof saved.showBrowser === "boolean") {
      els.showBrowserCheckbox.checked = saved.showBrowser;
    } else {
      els.showBrowserCheckbox.checked = false;
    }
    return saved.providerKeys || [];
  } catch {
    els.showBrowserCheckbox.checked = false;
    return [];
  }
}

function savePrefs() {
  localStorage.setItem(
    "theory-answer-webui-prefs",
    JSON.stringify({
      prefsVersion: PREFS_VERSION,
      browser: els.browserSelect.value,
      timeout: Number(els.timeoutInput.value || 90),
      parallelTasks: Number(els.parallelInput.value || 1),
      showBrowser: els.showBrowserCheckbox.checked,
      providerKeys: getSelectedProviderKeys(),
    }),
  );
}

function getSelectedProviderKeys() {
  return Array.from(document.querySelectorAll(".provider-option input:checked")).map((input) => input.value);
}

function syncParallelInputState() {
  const disabled = els.showBrowserCheckbox.checked;
  els.parallelInput.disabled = disabled;
  els.parallelInput.title = disabled ? "显示浏览器窗口时固定为串行执行" : "";
}

function renderProviderSelector(providers, preferredKeys = []) {
  const keys = providers.map((provider) => provider.key).join(",");
  const currentKeys = state.providers.map((provider) => provider.key).join(",");
  if (keys === currentKeys && state.providers.length > 0) {
    return;
  }

  state.providers = providers;
  const preferred = new Set(preferredKeys.length ? preferredKeys : providers.map((provider) => provider.key));
  els.providerSelector.innerHTML = providers
    .map(
      (provider) => `
        <label class="provider-option">
          <input type="checkbox" value="${provider.key}" ${preferred.has(provider.key) ? "checked" : ""} />
          <span>${escapeHtml(provider.name)}</span>
        </label>
      `,
    )
    .join("");

  els.providerSelector.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", savePrefs);
  });
}

function statusClass(status) {
  const map = {
    待执行: "status-pending",
    待初始化: "status-pending",
    运行中: "status-running",
    终止中: "status-stopping",
    已完成: "status-completed",
    已失败: "status-failed",
    已终止: "status-stopping",
    已关闭: "status-pending",
    未启动: "status-pending",
    未选择: "status-unselected",
    等待平台: "status-waiting",
    完成: "status-completed",
    失败: "status-failed",
    opening: "status-running",
    launching: "status-running",
    typing: "status-running",
    submitting: "status-running",
    waiting_answer: "status-waiting",
    waiting_login: "status-waiting",
    navigating: "status-running",
    ready: "status-completed",
    done: "status-completed",
    error: "status-failed",
  };
  return map[status] || "status-pending";
}

function renderSummary(summary, maxParallelTasks) {
  els.summaryChips.innerHTML = `
    <span class="chip neutral">总 ${summary.total}</span>
    <span class="chip amber">待执行 ${summary.pending}</span>
    <span class="chip cyan">运行中 ${summary.running}</span>
    <span class="chip green">已完成 ${summary.completed}</span>
    <span class="chip rose">已失败 ${summary.failed}</span>
    <span class="chip slate">已终止 ${summary.stopped}</span>
  `;
  els.queueMeta.textContent = `队列：总 ${summary.total} 题 | 并发 ${maxParallelTasks}`;
  els.clearQueueBtn.disabled = !summary.clearable;
}

function renderQueue(tasks) {
  els.queueTable.innerHTML = `
    <div class="table">
      <div class="row header queue-row">
        <div class="cell">任务</div>
        <div class="cell">状态</div>
        <div class="cell">推荐答案</div>
        <div class="cell">平台</div>
        <div class="cell">题目摘要</div>
      </div>
      ${tasks
        .map((task) => {
          const answer =
            task.recommendation !== "-" && task.exact_ratio !== "-"
              ? `${task.recommendation} [${task.exact_ratio}]`
              : task.recommendation;
          const selectedClass = task.task_id === state.selectedTaskId ? "selected" : "";
          return `
            <div class="row queue-row ${selectedClass}" data-task-id="${task.task_id}">
              <div class="cell">${escapeHtml(task.task_id)}</div>
              <div class="cell"><span class="status-badge ${statusClass(task.status)}">${escapeHtml(task.status)}</span></div>
              <div class="cell">${escapeHtml(answer)}</div>
              <div class="cell">${escapeHtml((task.providers || []).join(" / "))}</div>
              <div class="cell">${escapeHtml(task.title || "")}</div>
            </div>
          `;
        })
        .join("")}
    </div>
  `;

  els.queueTable.querySelectorAll(".queue-row[data-task-id]").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTaskId = row.dataset.taskId;
      refreshState();
    });
  });
}

function renderProviderTable(rows) {
  els.providerStateTable.innerHTML = `
    <div class="table">
      <div class="row header provider-row">
        <div class="cell">平台</div>
        <div class="cell">状态</div>
        <div class="cell">识别选项</div>
        <div class="cell">说明</div>
      </div>
      ${rows
        .map(
          (row) => `
            <div class="row provider-row">
              <div class="cell">${escapeHtml(row.name)}</div>
              <div class="cell"><span class="status-badge ${statusClass(row.status)}">${escapeHtml(row.status)}</span></div>
              <div class="cell">${escapeHtml(row.option || "-")}</div>
              <div class="cell">${escapeHtml(row.detail || "")}</div>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderDetail(snapshot) {
  const selectedTask = snapshot.selected_task;
  if (selectedTask) {
    const ratioText = selectedTask.exact_ratio !== "-" ? ` | ${selectedTask.exact_ratio}` : "";
    els.detailTitle.textContent = `${selectedTask.task_id} | ${selectedTask.title}`;
    els.detailSubtitle.textContent = `${selectedTask.status} | 推荐答案 ${selectedTask.recommendation}${ratioText}`;
    els.resultText.textContent = selectedTask.result_text || "暂无结果。";
    els.logText.textContent = (selectedTask.logs || []).join("\n") || "暂无日志。";
    renderProviderTable(selectedTask.providers || []);
    return;
  }

  els.detailTitle.textContent = "结果详情";
  els.detailSubtitle.textContent = "选择一条任务查看详情";
  els.resultText.textContent = buildOverviewText(snapshot.queue_summary || {});
  els.logText.textContent = (snapshot.system_logs || []).join("\n") || "暂无日志。";
  renderProviderTable(snapshot.session_providers || []);
}

function buildOverviewText(summary) {
  return [
    "当前没有选中任务。",
    "",
    `总 ${summary.total || 0} 题，待执行 ${summary.pending || 0}，运行中 ${summary.running || 0}，已完成 ${summary.completed || 0}，已失败 ${summary.failed || 0}，已终止 ${summary.stopped || 0}。`,
  ].join("\n");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.style.background = isError ? "rgba(146, 36, 52, 0.96)" : "rgba(24, 33, 43, 0.94)";
  els.toast.classList.add("visible");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => {
    els.toast.classList.remove("visible");
  }, 2600);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let message = `请求失败：${response.status}`;
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch {
      // ignore
    }
    throw new Error(message);
  }
  return response.json();
}

async function refreshState() {
  const params = new URLSearchParams();
  if (state.selectedTaskId) {
    params.set("selected_task_id", state.selectedTaskId);
  }

  try {
    const snapshot = await requestJson(`/api/state?${params.toString()}`);
    if (!state.selectedTaskId && snapshot.tasks && snapshot.tasks.length > 0) {
      state.selectedTaskId = snapshot.tasks[0].task_id;
      return refreshState();
    }
    if (state.selectedTaskId && !snapshot.selected_task) {
      state.selectedTaskId = null;
    }

    els.statusText.textContent = snapshot.status_text || "准备就绪";
    renderSummary(snapshot.queue_summary || {}, snapshot.max_parallel_tasks || 1);
    renderQueue(snapshot.tasks || []);
    renderDetail(snapshot);
  } catch (error) {
    showToast(error.message || "刷新状态失败", true);
  }
}

function setTab(tabName) {
  state.activeTab = tabName;
  els.tabResult.classList.toggle("active", tabName === "result");
  els.tabLog.classList.toggle("active", tabName === "log");
  els.resultPane.classList.toggle("active", tabName === "result");
  els.logPane.classList.toggle("active", tabName === "log");
}

async function openSessions() {
  savePrefs();
  try {
    await requestJson("/api/sessions/open", {
      method: "POST",
      body: JSON.stringify({
        provider_keys: getSelectedProviderKeys(),
        browser: els.browserSelect.value,
        show_browser: els.showBrowserCheckbox.checked,
      }),
    });
    showToast("网页会话初始化中...");
    refreshState();
  } catch (error) {
    showToast(error.message || "打开网页会话失败", true);
  }
}

async function enqueueTasks() {
  savePrefs();
  try {
    const payload = await requestJson("/api/tasks/enqueue", {
      method: "POST",
      body: JSON.stringify({
        question_text: els.questionInput.value,
        provider_keys: getSelectedProviderKeys(),
        timeout_seconds: Number(els.timeoutInput.value || 90),
        browser: els.browserSelect.value,
        show_browser: els.showBrowserCheckbox.checked,
        max_parallel_tasks: els.showBrowserCheckbox.checked ? 1 : Number(els.parallelInput.value || 1),
      }),
    });
    if (payload.task_ids && payload.task_ids.length > 0) {
      state.selectedTaskId = payload.task_ids[0];
    }
    showToast(`已加入队列：${(payload.task_ids || []).length} 题`);
    refreshState();
  } catch (error) {
    showToast(error.message || "加入队列失败", true);
  }
}

async function forceStop() {
  try {
    const payload = await requestJson("/api/tasks/force-stop", {
      method: "POST",
      body: JSON.stringify({ task_ids: [] }),
    });
    if (!payload.task_ids || payload.task_ids.length === 0) {
      showToast("当前没有可强制结束的任务");
    } else {
      showToast(`已请求终止 ${payload.task_ids.length} 个任务`);
    }
    refreshState();
  } catch (error) {
    showToast(error.message || "强制结束失败", true);
  }
}

async function clearQueue() {
  try {
    const payload = await requestJson("/api/tasks/clear", {
      method: "POST",
      body: JSON.stringify({ task_ids: [] }),
    });
    if (!payload.task_ids || payload.task_ids.length === 0) {
      showToast("当前没有可清空的队列内容");
    } else {
      showToast(`已清空队列：${payload.task_ids.length} 题`);
    }
    refreshState();
  } catch (error) {
    showToast(error.message || "清空队列失败", true);
  }
}

async function closeSessions() {
  try {
    await requestJson("/api/sessions/close", { method: "POST" });
    showToast("浏览器会话关闭中...");
    refreshState();
  } catch (error) {
    showToast(error.message || "关闭会话失败", true);
  }
}

function startPolling() {
  refreshState();
  state.pollTimer = window.setInterval(refreshState, 1200);
}

function init() {
  const preferredKeys = loadPrefs();
  syncParallelInputState();

  els.tabResult.addEventListener("click", () => setTab("result"));
  els.tabLog.addEventListener("click", () => setTab("log"));
  els.openSessionsBtn.addEventListener("click", openSessions);
  els.enqueueBtn.addEventListener("click", enqueueTasks);
  els.forceStopBtn.addEventListener("click", forceStop);
  els.clearQueueBtn.addEventListener("click", clearQueue);
  els.closeSessionsBtn.addEventListener("click", closeSessions);
  els.browserSelect.addEventListener("change", savePrefs);
  els.timeoutInput.addEventListener("change", savePrefs);
  els.parallelInput.addEventListener("input", savePrefs);
  els.parallelInput.addEventListener("change", savePrefs);
  els.showBrowserCheckbox.addEventListener("change", () => {
    syncParallelInputState();
    savePrefs();
  });

  requestJson("/api/state")
    .then((snapshot) => {
      renderProviderSelector(snapshot.providers || [], preferredKeys);
      refreshState();
    })
    .catch((error) => showToast(error.message || "初始化失败", true));

  startPolling();
}

init();
