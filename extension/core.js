(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.GWTCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "CODE", "PRE", "KBD", "SAMP", "VAR",
    "SVG", "CANVAS", "MATH", "IFRAME", "OBJECT", "EMBED", "INPUT",
    "TEXTAREA", "SELECT", "OPTION"
  ]);

  const SKIP_SELECTOR = [
    "input", "textarea", "select", "option", "[contenteditable]", "[contenteditable='true']",
    "[role='textbox']", "[role='searchbox']", "[role='combobox']", "[aria-hidden='true']",
    "[hidden]", "[translate='no']", ".notranslate", ".gwt-bilingual"
  ].join(",");

  const SIMPLIFIED_MARKERS = new Set("这为国发后里个们说时会来对从还没过开关门书车东与无云电广马风龙叶网体简边际钟万专业术语译显内应复条样览级仅将当进处据实于并达让见气动页统点线买卖听话爱习长头乐产强争数".split(""));
  const TRADITIONAL_MARKERS = new Set("這為國發後裡個們說時會來對從還沒過開關門書車東與無雲電廣馬風龍葉網體簡邊際鐘萬專業術語譯顯內應復條樣覽級僅將當進處據實於並達讓見氣動頁統點線買賣聽話愛習長頭樂產強爭數".split(""));
  const IDENTITY_TAGS = new Set(["YTD-CHANNEL-NAME"]);
  const IDENTITY_MARKER_PATTERN = /(?:^|[\s_-])(?:user[\s_-]?name|display[\s_-]?name|screen[\s_-]?name|nick[\s_-]?name|author[\s_-]?(?:name|link)|channel[\s_-]?name|creator[\s_-]?name|account[\s_-]?name|profile[\s_-]?name|owner[\s_-]?name|byline)(?:$|[\s_-])/i;
  const SOCIAL_PROFILE_HOSTS = /(?:^|\.)(?:x\.com|twitter\.com|instagram\.com|threads\.net|facebook\.com|tiktok\.com)$/i;
  const RESERVED_SOCIAL_PATHS = new Set([
    "about", "compose", "explore", "hashtag", "home", "i", "intent", "login", "messages",
    "notifications", "privacy", "search", "settings", "share", "signup", "tos"
  ]);

  function normalizeText(value) {
    return String(value || "").replace(/\u00a0/g, " ").replace(/[ \t\f\v]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  }

  function splitOuterWhitespace(value) {
    const text = String(value || "");
    const leading = (text.match(/^\s*/) || [""])[0];
    const trailing = (text.match(/\s*$/) || [""])[0];
    const end = Math.max(leading.length, text.length - trailing.length);
    return { leading, core: text.slice(leading.length, end), trailing };
  }

  function isUrlLike(text) {
    const value = normalizeText(text);
    return /^(?:https?:\/\/|www\.)\S+$/i.test(value) || /^[\w.-]+\.(?:com|org|net|io|dev|cn)(?:\/\S*)?$/i.test(value);
  }

  function isTechnicalOnly(text) {
    const value = normalizeText(text);
    if (!value) return true;
    if (isUrlLike(value)) return true;
    if (/^@[\p{L}\p{N}_.-]{1,64}$/u.test(value)) return true;
    if (/^(?:[A-Za-z]:\\|\.{0,2}[\\/]|\/)[\w .\\/()[\]{}@#$%&+=~-]+$/.test(value)) return true;
    if (/^[\w.@~+-]+(?:[\\/][\w.@~+-]+)+$/.test(value)) return true;
    if (/^\.?[\w@~+-]+\.(?:md|mdx|txt|json|ya?ml|toml|ini|xml|html?|css|m?js|cjs|ts|tsx|jsx|py|java|go|rs|rb|php|cs|cpp|h|hpp|sh|ps1|bat|cmd|lock|svg|png|jpe?g|gif|webp|pdf|zip)$/i.test(value)) return true;
    if (/^(?:npm|pnpm|yarn|pip|git|docker|curl|wget|cd|mkdir|python|node)\s+[-\w@./:=]+(?:\s+[-\w@./:=]+)*$/i.test(value)) return true;
    if (/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:\([^)]*\))?;?$/.test(value)) return true;
    const letters = (value.match(/\p{L}/gu) || []).length;
    const symbols = (value.match(/[{}[\]<>_=|`~\\]/g) || []).length;
    return letters < 2 || (symbols >= 4 && symbols > letters / 2);
  }

  function extractProtectedIdentityTerms(value) {
    const text = normalizeText(value);
    if (!text) return [];
    const terms = [];
    const add = (term) => {
      const clean = normalizeText(term);
      if (clean && clean.length <= 160 && text.includes(clean) && !terms.includes(clean)) terms.push(clean);
    };
    for (const match of text.matchAll(/@[\p{L}\p{N}_.-]{1,64}/gu)) add(match[0]);
    const identityPatterns = [
      /^(?<name>[^\n:：]{1,80}?)\s*\(\s*(?<handle>@[\p{L}\p{N}_.-]{1,64})\s*\)\s*(?=[:：·|—–-])/u,
      /^(?<name>[^\n:：@]{1,80}?)\s+(?<handle>@[\p{L}\p{N}_.-]{1,64})\s*(?=[·|:：—–-])/u
    ];
    for (const pattern of identityPatterns) {
      const match = text.match(pattern);
      if (match && match.groups) {
        add(match.groups.name);
        add(match.groups.handle);
      }
    }
    return terms.sort((left, right) => right.length - left.length);
  }

  function shouldSkipElement(element) {
    if (!element || element.nodeType !== 1) return true;
    if (SKIP_TAGS.has(element.tagName)) return true;
    if (typeof element.closest === "function" && element.closest(SKIP_SELECTOR)) return true;
    if (element.isContentEditable) return true;
    return false;
  }

  function hasIdentityMarker(element) {
    if (!element || element.nodeType !== 1) return false;
    if (IDENTITY_TAGS.has(element.tagName)) return true;
    const rel = String(element.getAttribute("rel") || "").toLowerCase().split(/\s+/);
    if (rel.includes("author")) return true;
    const itemprop = String(element.getAttribute("itemprop") || "").toLowerCase().split(/\s+/);
    if (itemprop.includes("author")) return true;
    const markers = [
      element.id,
      element.className && typeof element.className === "string" ? element.className : "",
      element.getAttribute("data-testid"),
      element.getAttribute("data-e2e"),
      element.getAttribute("data-qa"),
      element.getAttribute("data-cy"),
      element.getAttribute("part")
    ].filter(Boolean).join(" ");
    return IDENTITY_MARKER_PATTERN.test(markers);
  }

  function linkTargetsProfile(element) {
    if (!element || element.tagName !== "A") return false;
    const rawHref = element.getAttribute("href");
    if (!rawHref || /^(?:#|javascript:|mailto:|tel:)/i.test(rawHref)) return false;
    let url;
    try {
      url = new URL(rawHref, element.ownerDocument.baseURI);
    } catch (_error) {
      return false;
    }
    const path = url.pathname.replace(/\/+$/, "") || "/";
    if (/^\/@[^/]+$/i.test(path) || /^\/(?:u|user|users|profile|profiles|member|members|author|authors|channel|channels|creator|creators|in)\/[^/]+$/i.test(path)) {
      return true;
    }
    if (/(?:^|\.)youtube\.com$/i.test(url.hostname) && /^\/(?:@[^/]+|channel|c|user)\/?/i.test(path)) {
      return true;
    }
    if (SOCIAL_PROFILE_HOSTS.test(url.hostname) && /^\/[A-Za-z0-9_.-]+$/.test(path)) {
      return !RESERVED_SOCIAL_PATHS.has(path.slice(1).toLowerCase());
    }
    if (/(?:^|\.)facebook\.com$/i.test(url.hostname) && /^\/profile\.php$/i.test(path) && url.searchParams.has("id")) {
      return true;
    }
    return false;
  }

  function isIdentityTextNode(node) {
    if (!node || node.nodeType !== 3 || !node.parentElement) return false;
    const text = normalizeText(node.data);
    if (/^@[\p{L}\p{N}_.-]{1,64}$/u.test(text)) return true;
    let current = node.parentElement;
    for (let depth = 0; current && depth < 6; depth += 1) {
      if (hasIdentityMarker(current) || linkTargetsProfile(current)) return true;
      current = current.parentElement;
    }
    return false;
  }

  function elementIsVisible(element) {
    if (!element || element.nodeType !== 1) return false;
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    let current = element;
    while (current && current.nodeType === 1) {
      if (current.hidden || current.getAttribute("aria-hidden") === "true") return false;
      if (view && typeof view.getComputedStyle === "function") {
        const style = view.getComputedStyle(current);
        if (!style || style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) {
          return false;
        }
      }
      current = current.parentElement;
    }
    return true;
  }

  function isEligibleTextNode(node) {
    if (!node || node.nodeType !== 3 || !node.parentElement) return false;
    if (shouldSkipElement(node.parentElement) || isIdentityTextNode(node) || !elementIsVisible(node.parentElement)) return false;
    const text = normalizeText(node.data);
    if (text.length < 2 || text.length > 4000) return false;
    if (!/\p{L}/u.test(text) || isTechnicalOnly(text)) return false;
    return true;
  }

  function collectTextNodes(rootNode, options) {
    const settings = Object.assign({ limit: 1200 }, options || {});
    const doc = rootNode.nodeType === 9 ? rootNode : rootNode.ownerDocument;
    const root = rootNode.nodeType === 9 ? rootNode.body : rootNode;
    if (!doc || !root || typeof doc.createTreeWalker !== "function") return [];
    const nodeFilter = doc.defaultView && doc.defaultView.NodeFilter ? doc.defaultView.NodeFilter : globalThis.NodeFilter;
    const walker = doc.createTreeWalker(root, nodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return isEligibleTextNode(node) ? nodeFilter.FILTER_ACCEPT : nodeFilter.FILTER_REJECT;
      }
    });
    const nodes = [];
    while (nodes.length < settings.limit) {
      const node = walker.nextNode();
      if (!node) break;
      nodes.push(node);
    }
    return nodes;
  }

  function textLanguageStats(value) {
    const text = normalizeText(value);
    const cjk = (text.match(/[\u3400-\u9fff]/g) || []).length;
    const latin = (text.match(/[A-Za-z]/g) || []).length;
    const kana = (text.match(/[\u3040-\u30ff]/g) || []).length;
    const hangul = (text.match(/[\uac00-\ud7af]/g) || []).length;
    const letters = (text.match(/\p{L}/gu) || []).length;
    const other = Math.max(0, letters - cjk - latin - kana - hangul);
    const latinWords = (text.match(/[A-Za-z]+(?:['’-][A-Za-z]+)*/g) || []).length;
    let simplified = 0;
    let traditional = 0;
    for (const char of text) {
      if (SIMPLIFIED_MARKERS.has(char)) simplified += 1;
      if (TRADITIONAL_MARKERS.has(char)) traditional += 1;
    }
    return { text, cjk, latin, kana, hangul, other, letters, latinWords, simplified, traditional };
  }

  function shouldTranslateText(value, lang) {
    const stats = textLanguageStats(value);
    if (!stats.text || isTechnicalOnly(stats.text)) return false;
    const language = String(lang || "").toLowerCase().replace(/_/g, "-");

    if (stats.kana > 0 || stats.hangul > 0 || stats.other >= 2) return true;
    if (/^(?:ja|ko)(?:-|$)/.test(language) && stats.cjk > 0) return true;
    if (/^zh(?:-tw|-hk|-mo|-hant)(?:-|$)/.test(language) && stats.cjk > 0) return true;

    if (stats.cjk > 0) {
      if (stats.traditional > stats.simplified && stats.traditional > 0) return true;
      const latinShare = stats.latin / Math.max(stats.latin + stats.cjk, 1);
      return stats.latin >= 8 && stats.latinWords >= 2 && latinShare >= 0.45;
    }
    return stats.latin >= 2;
  }

  function isMostlySimplifiedChinese(texts, lang) {
    const language = String(lang || "").toLowerCase().replace(/_/g, "-");
    const values = (texts || []).map(normalizeText).filter(Boolean);
    const hasForeignProse = values.some((value) => {
      if (!shouldTranslateText(value, language)) return false;
      const stats = textLanguageStats(value);
      if (stats.kana + stats.hangul + stats.other >= 4) return true;
      return stats.latin >= 12 && stats.latinWords >= 3;
    });
    if (hasForeignProse) return false;

    if (/^zh(?:-cn|-sg|-hans)(?:-|$)/.test(language)) return true;
    if (/^zh(?:-tw|-hk|-mo|-hant)(?:-|$)/.test(language)) return false;
    if (/^(ja|ko)(?:-|$)/.test(language)) return false;

    const sample = normalizeText(values.join(" ")).slice(0, 16000);
    if (!sample) return false;
    const cjk = (sample.match(/[\u3400-\u9fff]/g) || []).length;
    const letters = (sample.match(/\p{L}/gu) || []).length;
    const kana = (sample.match(/[\u3040-\u30ff]/g) || []).length;
    const hangul = (sample.match(/[\uac00-\ud7af]/g) || []).length;
    if (cjk < 16 || cjk / Math.max(letters, 1) < 0.45 || kana > 4 || hangul > 4) return false;
    let simplified = 0;
    let traditional = 0;
    for (const char of sample) {
      if (SIMPLIFIED_MARKERS.has(char)) simplified += 1;
      if (TRADITIONAL_MARKERS.has(char)) traditional += 1;
    }
    if (traditional >= 2 && traditional > simplified) return false;
    return simplified >= 2 || traditional === 0;
  }

  function siteKey(hostname) {
    return String(hostname || "").toLowerCase().replace(/^www\./, "");
  }

  function makeSessionId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return `gwt_${globalThis.crypto.randomUUID().replace(/-/g, "")}`;
    }
    return `gwt_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`;
  }

  function batchRecords(records, maxSegments, maxChars) {
    const batches = [];
    let current = [];
    let chars = 0;
    for (const record of records) {
      const length = normalizeText(record.original).length;
      if (current.length && (current.length >= maxSegments || chars + length > maxChars)) {
        batches.push(current);
        current = [];
        chars = 0;
      }
      current.push(record);
      chars += length;
    }
    if (current.length) batches.push(current);
    return batches;
  }

  class TranslationStore {
    constructor(document) {
      this.document = document;
      this.byNode = new WeakMap();
      this.records = new Set();
      this.internalNodes = new WeakSet();
      this.mode = "zh";
      this.sequence = 0;
    }

    has(node) {
      return this.byNode.has(node);
    }

    get(node) {
      return this.byNode.get(node) || null;
    }

    track(node) {
      const existing = this.byNode.get(node);
      if (existing) return existing;
      const record = {
        id: `s${++this.sequence}`,
        node,
        original: node.data,
        translation: "",
        wrapper: null,
        pending: true
      };
      this.byNode.set(node, record);
      this.records.add(record);
      return record;
    }

    discard(record) {
      if (!record) return;
      this.records.delete(record);
      this.byNode.delete(record.node);
    }

    markInternal(node) {
      this.internalNodes.add(node);
      const clear = () => this.internalNodes.delete(node);
      if (typeof queueMicrotask === "function") queueMicrotask(clear);
      else Promise.resolve().then(clear);
    }

    isInternal(node) {
      return this.internalNodes.has(node);
    }

    setTranslation(record, translatedText) {
      if (!record || typeof translatedText !== "string" || !translatedText.trim()) return;
      record.translation = translatedText.trim();
      record.pending = false;
      this.applyRecord(record, this.mode);
    }

    setMode(mode) {
      this.mode = mode === "bilingual" ? "bilingual" : "zh";
      for (const record of this.records) {
        if (record.translation) this.applyRecord(record, this.mode);
      }
    }

    showCached(mode) {
      this.setMode(mode || this.mode);
    }

    applyRecord(record, mode) {
      const node = record.node;
      const pieces = splitOuterWhitespace(record.original);
      if (mode === "bilingual") {
        if (record.wrapper && record.wrapper.isConnected) {
          const translated = record.wrapper.querySelector(".gwt-translation");
          if (translated) translated.textContent = record.translation;
          return;
        }
        if (!node.isConnected) return;
        const wrapper = this.document.createElement("span");
        wrapper.className = "gwt-bilingual";
        wrapper.setAttribute("data-gwt", "bilingual");
        const original = this.document.createElement("span");
        original.className = "gwt-original";
        original.textContent = pieces.core;
        const translated = this.document.createElement("span");
        translated.className = "gwt-translation";
        translated.textContent = record.translation;
        wrapper.append(original, translated);
        this.markInternal(node);
        node.replaceWith(wrapper);
        record.wrapper = wrapper;
        return;
      }
      if (record.wrapper && record.wrapper.isConnected) {
        this.markInternal(node);
        record.wrapper.replaceWith(node);
      }
      record.wrapper = null;
      if (node.isConnected) {
        this.markInternal(node);
        node.data = `${pieces.leading}${record.translation}${pieces.trailing}`;
      }
    }

    restoreAll() {
      for (const record of this.records) {
        const node = record.node;
        if (record.wrapper && record.wrapper.isConnected) {
          this.markInternal(node);
          record.wrapper.replaceWith(node);
        }
        record.wrapper = null;
        if (node.isConnected) {
          this.markInternal(node);
          node.data = record.original;
        }
      }
    }

    handleExternalTextMutation(node) {
      if (this.isInternal(node)) return false;
      const record = this.byNode.get(node);
      if (!record) return true;
      const expected = record.translation
        ? `${splitOuterWhitespace(record.original).leading}${record.translation}${splitOuterWhitespace(record.original).trailing}`
        : record.original;
      if (!record.wrapper && node.data !== expected && node.data !== record.original) {
        this.discard(record);
        return true;
      }
      return false;
    }

    translatedCount() {
      let count = 0;
      for (const record of this.records) if (record.translation) count += 1;
      return count;
    }

    pendingCount() {
      let count = 0;
      for (const record of this.records) if (record.pending) count += 1;
      return count;
    }

    discardPending() {
      for (const record of Array.from(this.records)) {
        if (record.pending) this.discard(record);
      }
    }
  }

  return {
    normalizeText,
    splitOuterWhitespace,
    isUrlLike,
    isTechnicalOnly,
    extractProtectedIdentityTerms,
    shouldSkipElement,
    hasIdentityMarker,
    linkTargetsProfile,
    isIdentityTextNode,
    elementIsVisible,
    isEligibleTextNode,
    collectTextNodes,
    shouldTranslateText,
    isMostlySimplifiedChinese,
    siteKey,
    makeSessionId,
    batchRecords,
    TranslationStore
  };
});
