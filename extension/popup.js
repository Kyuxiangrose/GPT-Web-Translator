"use strict";

const elements = {
  siteName: document.getElementById("siteName"),
  serviceDot: document.getElementById("serviceDot"),
  autoToggle: document.getElementById("autoToggle"),
  siteToggle: document.getElementById("siteToggle"),
  modeZh: document.getElementById("modeZh"),
  modeBilingual: document.getElementById("modeBilingual"),
  stopButton: document.getElementById("stopButton"),
  restoreButton: document.getElementById("restoreButton"),
  statusText: document.getElementById("statusText"),
  countText: document.getElementById("countText"),
  pageTokens: document.getElementById("pageTokens"),
  historyTokens: document.getElementById("historyTokens"),
  pageCost: document.getElementById("pageCost"),
  historyCost: document.getElementById("historyCost"),
  accountBalance: document.getElementById("accountBalance"),
  balanceNote: document.getElementById("balanceNote"),
  tokenNote: document.getElementById("tokenNote"),
  clearTokens: document.getElementById("clearTokens"),
  tokenConfirm: document.getElementById("tokenConfirm"),
  cancelClearTokens: document.getElementById("cancelClearTokens"),
  confirmClearTokens: document.getElementById("confirmClearTokens")
};

let activeTabId = null;
let pageAvailable = false;
let activePageId = "";
let refreshingTokens = false;
let clearingTokens = false;

function sendToBackground(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, (response) => {
    resolve(chrome.runtime.lastError ? { ok: false } : response || { ok: false });
  }));
}

function renderTokenStats(response) {
  const stats = response.stats || {};
  const format = (value) => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
  const formatCost = (value) => Number.isSafeInteger(value) && value >= 0
    ? `¥${(value / 1000000000).toFixed(6)}` : "—";
  elements.pageTokens.textContent = format(stats.page_total_tokens);
  elements.historyTokens.textContent = format(stats.history_total_tokens);
  elements.pageCost.textContent = formatCost(stats.page_total_cost_nano_yuan);
  elements.historyCost.textContent = formatCost(stats.history_total_cost_nano_yuan);
  const missingUsage = (stats.history_missing_usage || 0) + (stats.page_missing_usage || 0);
  const missingCost = (stats.history_missing_cost || 0) + (stats.page_missing_cost || 0);
  elements.tokenNote.textContent = response.stale
    ? (response.error === "请重启更新后的本机翻译服务" ? response.error : "服务离线，显示上次同步值")
    : missingUsage ? "部分响应未提供 usage，仅显示已知用量"
    : missingCost ? "部分旧记录缺少金额明细，仅显示已知金额"
    : "Token 按 API 实际用量 · 金额按官方单价";
  elements.clearTokens.disabled = !response.ok || Boolean(response.stale) || clearingTokens;
}

function renderAccountBalance(response) {
  if (!response.ok || !response.balance) {
    elements.accountBalance.textContent = "—";
    elements.balanceNote.textContent = response.error || "暂时无法读取账户余额";
    return;
  }
  const infos = response.balance.balance_infos || [];
  const preferred = infos.find((item) => item.currency === "CNY") || infos[0];
  const value = preferred && Number(preferred.total_balance);
  if (!preferred || !Number.isFinite(value) || value < 0) {
    elements.accountBalance.textContent = "—";
    elements.balanceNote.textContent = "DeepSeek 返回的余额格式异常";
    return;
  }
  const symbol = preferred.currency === "CNY" ? "¥" : "$";
  elements.accountBalance.textContent = `${symbol}${value.toFixed(2)}`;
  elements.balanceNote.textContent = response.balance.is_available
    ? "DeepSeek 官方当前余额 · 最多每分钟刷新一次"
    : "DeepSeek 官方余额不足或暂不可用";
}

async function refreshAccountBalance() {
  const response = await sendToBackground({ type: "GWT_GET_ACCOUNT_BALANCE" });
  renderAccountBalance(response);
}

async function refreshTokenStats() {
  if (refreshingTokens || clearingTokens) return;
  refreshingTokens = true;
  try {
    const page = await sendToPage({ type: "GWT_GET_STATE" });
    activePageId = page.ok && page.state ? page.state.pageId || "" : "";
    const response = await sendToBackground({ type: "GWT_GET_TOKEN_STATS", pageId: activePageId });
    if (!clearingTokens) renderTokenStats(response);
  } finally {
    refreshingTokens = false;
  }
}

elements.clearTokens.addEventListener("click", () => {
  elements.tokenConfirm.hidden = false;
  elements.cancelClearTokens.focus();
});
elements.cancelClearTokens.addEventListener("click", () => { elements.tokenConfirm.hidden = true; });
elements.confirmClearTokens.addEventListener("click", async () => {
  if (clearingTokens) return;
  clearingTokens = true;
  elements.confirmClearTokens.disabled = true;
  elements.clearTokens.disabled = true;
  const response = await sendToBackground({
    type: "GWT_CLEAR_TOKEN_STATS", pageId: activePageId, confirmed: true
  });
  clearingTokens = false;
  elements.confirmClearTokens.disabled = false;
  renderTokenStats(response);
  if (response.ok) elements.tokenConfirm.hidden = true;
  else elements.tokenNote.textContent = response.error || "清除失败，请确认本机服务已启动";
});

function queryActiveTab() {
  return new Promise((resolve) => chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs[0] || null)));
}

function sendToPage(message) {
  return new Promise((resolve) => {
    if (!activeTabId) {
      resolve({ ok: false });
      return;
    }
    chrome.tabs.sendMessage(activeTabId, message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false });
        return;
      }
      resolve(response || { ok: false });
    });
  });
}

function checkHealth() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "GWT_HEALTH" }, (response) => {
      if (chrome.runtime.lastError) resolve({ ok: false });
      else resolve(response || { ok: false });
    });
  });
}

function setControlsEnabled(enabled) {
  for (const key of ["autoToggle", "siteToggle", "modeZh", "modeBilingual", "stopButton", "restoreButton"]) {
    elements[key].disabled = !enabled;
  }
}

function renderState(state) {
  if (!state) return;
  pageAvailable = true;
  elements.siteName.textContent = state.site || "当前网站";
  elements.autoToggle.checked = Boolean(state.autoTranslate);
  elements.siteToggle.checked = Boolean(state.siteEnabled);
  elements.modeZh.classList.toggle("active", state.mode !== "bilingual");
  elements.modeBilingual.classList.toggle("active", state.mode === "bilingual");
  elements.statusText.textContent = state.status || "就绪";
  elements.statusText.classList.toggle("error", Boolean(state.errorCode));
  elements.countText.textContent = state.translatedCount
    ? `已处理 ${state.translatedCount} 处文字${state.pendingCount ? `，等待 ${state.pendingCount} 处` : ""}`
    : "";
  setControlsEnabled(true);
}

async function perform(message) {
  if (!pageAvailable) return;
  setControlsEnabled(false);
  const response = await sendToPage(message);
  if (response.ok) renderState(response.state);
  else {
    elements.statusText.textContent = response.error || "操作失败，请刷新页面后重试";
    elements.statusText.classList.add("error");
    setControlsEnabled(true);
  }
}

elements.autoToggle.addEventListener("change", () => perform({ type: "GWT_SET_AUTO", enabled: elements.autoToggle.checked }));
elements.siteToggle.addEventListener("change", () => perform({ type: "GWT_SET_SITE", enabled: elements.siteToggle.checked }));
elements.modeZh.addEventListener("click", () => perform({ type: "GWT_SET_MODE", mode: "zh" }));
elements.modeBilingual.addEventListener("click", () => perform({ type: "GWT_SET_MODE", mode: "bilingual" }));
elements.stopButton.addEventListener("click", () => perform({ type: "GWT_STOP" }));
elements.restoreButton.addEventListener("click", () => perform({ type: "GWT_RESTORE" }));

async function initialize() {
  setControlsEnabled(false);
  const [tab, health] = await Promise.all([queryActiveTab(), checkHealth()]);
  elements.serviceDot.classList.toggle("ready", Boolean(health.ok && health.payload && health.payload.configured));
  elements.serviceDot.classList.toggle("error", !health.ok || (health.payload && !health.payload.configured));
  if (!tab || !tab.id) {
    elements.statusText.textContent = "无法读取当前页面";
    return;
  }
  activeTabId = tab.id;
  // Poll only while the popup is open; no new MV3 permissions or background timer.
  refreshTokenStats();
  refreshAccountBalance();
  setInterval(refreshTokenStats, 1500);
  setInterval(refreshAccountBalance, 60000);
  const response = await sendToPage({ type: "GWT_GET_STATE" });
  if (!response.ok) {
    elements.siteName.textContent = "此页面不支持翻译";
    elements.statusText.textContent = "浏览器内部页面不能由扩展修改";
    return;
  }
  renderState(response.state);
  if (!health.ok) {
    elements.statusText.textContent = "本机翻译服务未启动";
    elements.statusText.classList.add("error");
  } else if (health.payload && !health.payload.configured) {
    elements.statusText.textContent = "尚未配置 DeepSeek API Key";
    elements.statusText.classList.add("error");
  }
}

initialize();
