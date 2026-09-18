"use strict";

const BACKEND_BASE = "http://127.0.0.1:8765";
const CLIENT_HEADER = "gwt-extension-v1";
const pendingBySession = new Map();
const TOKEN_STORAGE_KEY = "gwtTokenStats";
let tokenStorageQueue = Promise.resolve();

function rememberTokenStats(snapshot) {
  // All contexts display authoritative snapshots. Never add response usage here.
  // Serialize mirror writes and reject late revisions after a clear/new response.
  const operation = tokenStorageQueue.then(async () => {
    if (!snapshot || typeof snapshot.store_id !== "string" ||
        !Number.isSafeInteger(snapshot.revision) || snapshot.revision < 0 ||
        !Number.isSafeInteger(snapshot.history_total_tokens) || snapshot.history_total_tokens < 0 ||
        !Number.isSafeInteger(snapshot.page_total_tokens) || snapshot.page_total_tokens < 0 ||
        !Number.isSafeInteger(snapshot.history_total_cost_nano_yuan) || snapshot.history_total_cost_nano_yuan < 0 ||
        !Number.isSafeInteger(snapshot.page_total_cost_nano_yuan) || snapshot.page_total_cost_nano_yuan < 0) return;
    const saved = (await chrome.storage.local.get(TOKEN_STORAGE_KEY))[TOKEN_STORAGE_KEY];
    const sameStore = saved && saved.store_id === snapshot.store_id;
    const pages = sameStore ? { ...saved.pages } : {};
    const previousPage = pages[snapshot.page_id];
    if (snapshot.page_id && (!previousPage || snapshot.revision >= previousPage.revision)) {
      pages[snapshot.page_id] = {
        total: snapshot.page_total_tokens, missing: snapshot.page_missing_usage,
        cost_nano_yuan: snapshot.page_total_cost_nano_yuan,
        missing_cost: snapshot.page_missing_cost,
        revision: snapshot.revision, seenAt: Date.now()
      };
    }
    // Only this offline mirror is bounded; the SQLite ledger retains every page.
    const recentPages = Object.fromEntries(Object.entries(pages)
      .sort((a, b) => b[1].seenAt - a[1].seenAt).slice(0, 256));
    const history = sameStore && saved.revision > snapshot.revision ? saved : {
      store_id: snapshot.store_id, revision: snapshot.revision,
      history_total_tokens: snapshot.history_total_tokens,
      history_missing_usage: snapshot.history_missing_usage,
      history_total_cost_nano_yuan: snapshot.history_total_cost_nano_yuan,
      history_missing_cost: snapshot.history_missing_cost,
      currency: snapshot.currency,
      pricing_version: snapshot.pricing_version,
      started_at: snapshot.started_at
    };
    await chrome.storage.local.set({ [TOKEN_STORAGE_KEY]: { ...history, pages: recentPages } });
  });
  tokenStorageQueue = operation.catch(() => {});
  return operation;
}

async function storedTokenStats(pageId) {
  await tokenStorageQueue;
  const saved = (await chrome.storage.local.get(TOKEN_STORAGE_KEY))[TOKEN_STORAGE_KEY];
  const page = saved && saved.pages && saved.pages[pageId];
  return {
    history_total_tokens: saved ? saved.history_total_tokens : null,
    history_missing_usage: saved ? saved.history_missing_usage : 0,
    history_total_cost_nano_yuan: saved &&
      Number.isSafeInteger(saved.history_total_cost_nano_yuan)
      ? saved.history_total_cost_nano_yuan : null,
    history_missing_cost: saved ? saved.history_missing_cost || 0 : 0,
    page_total_tokens: pageId && page ? page.total : null,
    page_missing_usage: page ? page.missing : 0,
    page_total_cost_nano_yuan: pageId && page &&
      Number.isSafeInteger(page.cost_nano_yuan) ? page.cost_nano_yuan : null,
    page_missing_cost: page ? page.missing_cost || 0 : 0,
    currency: saved ? saved.currency : "CNY",
    pricing_version: saved ? saved.pricing_version : null,
    page_id: pageId
  };
}

async function getTokenStats(pageId, clear = false, confirmed = false) {
  try {
    if (clear && !confirmed) throw new Error("请先确认清除历史统计");
    const payload = await fetchJson(clear ? "/v1/usage/clear" : "/v1/usage", {
      method: "POST", body: { page_id: pageId, confirmed }
    });
    if (!payload.token_stats ||
        !Number.isSafeInteger(payload.token_stats.history_total_cost_nano_yuan) ||
        !Number.isSafeInteger(payload.token_stats.page_total_cost_nano_yuan)) {
      throw new Error("请重启更新后的本机翻译服务");
    }
    return { ok: true, stats: await storedTokenStats(pageId), stale: false };
  } catch (error) {
    return {
      ok: !clear, stats: await storedTokenStats(pageId), stale: true,
      error: error.code === "not_found" ? "请重启更新后的本机翻译服务" : error.message
    };
  }
}

function addController(sessionId, controller) {
  if (!pendingBySession.has(sessionId)) pendingBySession.set(sessionId, new Set());
  pendingBySession.get(sessionId).add(controller);
}

function removeController(sessionId, controller) {
  const set = pendingBySession.get(sessionId);
  if (!set) return;
  set.delete(controller);
  if (!set.size) pendingBySession.delete(sessionId);
}

function abortSession(sessionId) {
  const set = pendingBySession.get(sessionId);
  if (!set) return;
  for (const controller of set) controller.abort();
  pendingBySession.delete(sessionId);
}

async function fetchJson(path, options, sessionId) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 65000);
  if (sessionId) addController(sessionId, controller);
  try {
    const response = await fetch(`${BACKEND_BASE}${path}`, {
      method: options.method || "GET",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "X-GWT-Client": CLIENT_HEADER
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      signal: controller.signal
    });
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (payload && payload.token_stats) {
      try {
        await rememberTokenStats(payload.token_stats);
      } catch (_error) {
        // A mirror failure must not invalidate a translation already paid for.
        // The durable source of truth remains the backend's SQLite ledger.
      }
    }
    if (!response.ok || !payload || payload.ok === false) {
      const message = payload && payload.error && payload.error.message
        ? payload.error.message
        : "本机翻译服务返回异常";
      const error = new Error(message);
      error.code = payload && payload.error ? payload.error.code : "backend_error";
      throw error;
    }
    return payload;
  } catch (error) {
    if (error && error.name === "AbortError") {
      const aborted = new Error("翻译已停止");
      aborted.code = "cancelled";
      throw aborted;
    }
    if (error && error.code) throw error;
    const friendly = new Error("无法连接本机翻译服务，请先运行“启动翻译服务.bat”");
    friendly.code = "backend_unreachable";
    throw friendly;
  } finally {
    clearTimeout(timeoutId);
    if (sessionId) removeController(sessionId, controller);
  }
}

async function translateBatch(payload) {
  return fetchJson("/v1/translate", { method: "POST", body: payload }, payload.session_id);
}

async function cancelSession(sessionId) {
  abortSession(sessionId);
  try {
    return await fetchJson("/v1/cancel", {
      method: "POST",
      body: { session_id: sessionId }
    });
  } catch (error) {
    if (error && error.code === "backend_unreachable") return { ok: false };
    throw error;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;
  if (message.type === "GWT_GET_TOKEN_STATS" || message.type === "GWT_CLEAR_TOKEN_STATS") {
    getTokenStats(message.pageId || "", message.type === "GWT_CLEAR_TOKEN_STATS", message.confirmed === true)
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, error: "无法读取 Token 统计" }));
    return true;
  }
  if (message.type === "GWT_TRANSLATE_BATCH") {
    translateBatch(message.payload)
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({
        ok: false,
        error: {
          code: error.code || "translation_failed",
          message: error.message || "翻译失败，请稍后重试"
        }
      }));
    return true;
  }
  if (message.type === "GWT_CANCEL_SESSION") {
    cancelSession(message.sessionId)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === "GWT_HEALTH") {
    fetchJson("/health", { method: "GET" })
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: { code: error.code, message: error.message } }));
    return true;
  }
  return false;
});
