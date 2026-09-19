(function () {
  "use strict";

  const Core = globalThis.GWTCore;
  if (!Core || !document.body) return;

  const store = new Core.TranslationStore(document);
  // One identity for the lifetime of this document, independent of stop/start.
  const pageId = Core.makeSessionId();
  const state = {
    autoTranslate: true,
    siteEnabled: true,
    mode: "zh",
    status: "正在初始化……",
    stopped: true,
    restored: false,
    pageIsSimplifiedChinese: false,
    sessionId: Core.makeSessionId(),
    glossary: {},
    previousText: "",
    errorCode: ""
  };

  let observer = null;
  let scanTimer = null;
  let processing = false;
  let rescanRequested = false;
  let generation = 0;

  function storageGet(defaults) {
    return new Promise((resolve) => chrome.storage.local.get(defaults, resolve));
  }

  function storageSet(values) {
    return new Promise((resolve) => chrome.storage.local.set(values, resolve));
  }

  function sendRuntimeMessage(message) {
    return new Promise((resolve) => {
      let settled = false;
      const unavailable = () => ({
        ok: false,
        error: { code: "runtime_error", message: "扩展后台暂时不可用，请刷新页面后重试" }
      });
      const finish = (response) => {
        if (settled) return;
        settled = true;
        resolve(response);
      };
      try {
        const pending = chrome.runtime.sendMessage(message, (response) => {
          try {
            if (chrome.runtime.lastError) {
              finish(unavailable());
              return;
            }
            finish(response || { ok: false, error: { code: "empty_response", message: "扩展后台没有返回结果" } });
          } catch (_error) {
            finish(unavailable());
          }
        });
        if (pending && typeof pending.then === "function") {
          pending.then(
            (response) => {
              if (response !== undefined) finish(response);
            },
            () => finish(unavailable())
          );
        }
      } catch (_error) {
        finish(unavailable());
      }
    });
  }

  function currentSiteKey() {
    return Core.siteKey(location.hostname);
  }

  function publicState() {
    return {
      autoTranslate: state.autoTranslate,
      pageId,
      siteEnabled: state.siteEnabled,
      mode: state.mode,
      status: state.status,
      stopped: state.stopped,
      restored: state.restored,
      pageIsSimplifiedChinese: state.pageIsSimplifiedChinese,
      site: currentSiteKey(),
      translatedCount: store.translatedCount(),
      pendingCount: store.pendingCount(),
      errorCode: state.errorCode
    };
  }

  function setStatus(message, errorCode) {
    state.status = message;
    state.errorCode = errorCode || "";
  }

  function ensureObserver() {
    if (observer || state.stopped) return;
    observer = new MutationObserver((mutations) => {
      if (state.stopped) return;
      let shouldScan = false;
      for (const mutation of mutations) {
        if (mutation.type === "characterData") {
          if (store.handleExternalTextMutation(mutation.target)) shouldScan = true;
          continue;
        }
        for (const node of mutation.addedNodes) {
          if (node.nodeType === 1 && node.matches && node.matches(".gwt-bilingual")) continue;
          if (node.parentElement && node.parentElement.closest(".gwt-bilingual")) continue;
          shouldScan = true;
          break;
        }
        if (shouldScan) break;
      }
      if (shouldScan) scheduleScan(650);
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  function disconnectObserver() {
    if (observer) observer.disconnect();
    observer = null;
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = null;
  }

  function scheduleScan(delay) {
    if (state.stopped) return;
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = setTimeout(() => {
      scanTimer = null;
      scanAndTranslate();
    }, delay == null ? 250 : delay);
  }

  function safePage() {
    return {
      title: Core.normalizeText(document.title).slice(0, 300),
      site: currentSiteKey(),
      language: String(document.documentElement.lang || "").slice(0, 40)
    };
  }

  async function scanAndTranslate() {
    if (state.stopped) return;
    if (processing) {
      rescanRequested = true;
      return;
    }
    processing = true;
    const myGeneration = generation;
    try {
      const nodes = Core.collectTextNodes(document, { limit: 1600 });
      const newRecords = [];
      for (const node of nodes) {
        if (!store.has(node) && Core.shouldTranslateText(node.data, document.documentElement.lang)) {
          newRecords.push(store.track(node));
        }
      }
      if (!newRecords.length) {
        if (!state.stopped) setStatus(store.translatedCount() ? "翻译完成" : "未发现需要翻译的文字");
        return;
      }

      setStatus("正在翻译……");
      const batches = Core.batchRecords(newRecords, 20, 9000);
      for (let batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
        if (state.stopped || generation !== myGeneration) return;
        const batch = batches[batchIndex];
        const following = batches[batchIndex + 1] || [];
        const request = {
          session_id: state.sessionId,
          page_id: pageId,
          page: safePage(),
          context: {
            previous_text: state.previousText.slice(-1600),
            next_text: following.slice(0, 5).map((record) => Core.normalizeText(record.original)).join("\n").slice(0, 1200),
            glossary: state.glossary
          },
          segments: batch.map((record) => ({
            id: record.id,
            text: Core.normalizeText(record.original),
            protected: Core.extractProtectedIdentityTerms(record.original)
          }))
        };
        const response = await sendRuntimeMessage({ type: "GWT_TRANSLATE_BATCH", payload: request });
        if (state.stopped || generation !== myGeneration) return;
        if (!response.ok) {
          for (let remainingIndex = batchIndex; remainingIndex < batches.length; remainingIndex += 1) {
            for (const record of batches[remainingIndex]) store.discard(record);
          }
          const error = response.error || {};
          if (error.code !== "cancelled") setStatus(error.message || "翻译失败，请稍后重试", error.code || "translation_failed");
          return;
        }
        const payload = response.payload || {};
        const translationById = new Map((payload.translations || []).map((item) => [item.id, item.text]));
        for (const record of batch) {
          const translated = translationById.get(record.id);
          if (translated) store.setTranslation(record, translated);
          else store.discard(record);
        }
        if (payload.glossary && typeof payload.glossary === "object") {
          state.glossary = Object.assign({}, state.glossary, payload.glossary);
          const entries = Object.entries(state.glossary).slice(-30);
          state.glossary = Object.fromEntries(entries);
        }
        state.previousText = `${state.previousText}\n${batch.map((record) => Core.normalizeText(record.original)).join("\n")}`.slice(-1600);
      }
      if (!state.stopped) setStatus("翻译完成");
    } finally {
      processing = false;
      if (rescanRequested && !state.stopped) {
        rescanRequested = false;
        scheduleScan(250);
      }
    }
  }

  async function stopTranslation(message) {
    generation += 1;
    state.stopped = true;
    disconnectObserver();
    const oldSession = state.sessionId;
    state.sessionId = Core.makeSessionId();
    store.discardPending();
    setStatus(message || "已停止翻译");
    sendRuntimeMessage({ type: "GWT_CANCEL_SESSION", sessionId: oldSession });
  }

  async function restoreOriginal() {
    await stopTranslation("已恢复原文");
    store.restoreAll();
    state.restored = true;
    setStatus("已恢复原文");
  }

  async function startTranslation(force) {
    if (!state.autoTranslate || !state.siteEnabled) {
      setStatus(!state.autoTranslate ? "自动翻译已关闭" : "此网站已禁止自动翻译");
      state.stopped = true;
      return;
    }
    const sampleNodes = Core.collectTextNodes(document, { limit: 700 });
    const sampleTexts = sampleNodes.map((node) => Core.normalizeText(node.data));
    state.pageIsSimplifiedChinese = Core.isMostlySimplifiedChinese(sampleTexts, document.documentElement.lang);
    if (state.pageIsSimplifiedChinese && !force) {
      state.stopped = true;
      setStatus("无需翻译（页面已是简体中文）");
      return;
    }
    state.stopped = false;
    state.restored = false;
    state.sessionId = Core.makeSessionId();
    generation += 1;
    ensureObserver();
    await scanAndTranslate();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message.type !== "string") return false;
    (async () => {
      if (message.type === "GWT_GET_STATE") return publicState();
      if (message.type === "GWT_SET_MODE") {
        state.mode = message.mode === "bilingual" ? "bilingual" : "zh";
        store.showCached(state.mode);
        await storageSet({ displayMode: state.mode });
        setStatus(state.restored ? "已使用缓存译文" : state.status);
        return publicState();
      }
      if (message.type === "GWT_SET_AUTO") {
        state.autoTranslate = Boolean(message.enabled);
        await storageSet({ autoTranslate: state.autoTranslate });
        if (state.autoTranslate) await startTranslation(false);
        else await stopTranslation("自动翻译已关闭");
        return publicState();
      }
      if (message.type === "GWT_SET_SITE") {
        state.siteEnabled = Boolean(message.enabled);
        const saved = await storageGet({ siteRules: {} });
        const rules = Object.assign({}, saved.siteRules || {}, { [currentSiteKey()]: state.siteEnabled });
        await storageSet({ siteRules: rules });
        if (state.siteEnabled && state.autoTranslate) await startTranslation(false);
        else await stopTranslation("此网站已禁止自动翻译");
        return publicState();
      }
      if (message.type === "GWT_STOP") {
        await stopTranslation("已停止翻译");
        return publicState();
      }
      if (message.type === "GWT_RESTORE") {
        await restoreOriginal();
        return publicState();
      }
      return publicState();
    })().then((result) => sendResponse({ ok: true, state: result }))
      .catch(() => sendResponse({ ok: false, error: "操作失败，请刷新页面后重试" }));
    return true;
  });

  async function initialize() {
    const saved = await storageGet({ autoTranslate: true, displayMode: "zh", siteRules: {} });
    state.autoTranslate = saved.autoTranslate !== false;
    state.mode = saved.displayMode === "bilingual" ? "bilingual" : "zh";
    store.mode = state.mode;
    const rules = saved.siteRules || {};
    state.siteEnabled = rules[currentSiteKey()] !== false;
    await startTranslation(false);
  }

  initialize().catch(() => setStatus("扩展初始化失败，请刷新页面后重试", "init_failed"));
})();
