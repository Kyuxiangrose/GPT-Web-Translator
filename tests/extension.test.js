"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

const extensionRoot = path.resolve(__dirname, "..", "extension");
let browser;

test.before(async () => {
  const executablePath = process.env.GWT_BROWSER_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
  browser = await chromium.launch({ headless: true, executablePath });
});

test.after(async () => {
  if (browser) await browser.close();
});

async function withPage(html, callback) {
  const page = await browser.newPage();
  try {
    await page.setContent(html);
    await page.addScriptTag({ path: path.join(extensionRoot, "core.js") });
    return await callback(page);
  } finally {
    await page.close();
  }
}

test("privacy filter excludes user input, editable text, code and hidden content", async () => {
  const values = await withPage(`
    <main>
      <p>Public article text for translation.</p>
      <a href="/products?q=1#part">Learn more about DeepSeek</a>
      <input value="private typed secret">
      <textarea>private draft message</textarea>
      <div contenteditable="true">private editable note</div>
      <pre>npm install private-package</pre>
      <p style="display:none">hidden account data</p>
      <div style="display:none"><p>hidden by an ancestor</p></div>
    </main>`, (page) => page.evaluate(() => GWTCore.collectTextNodes(document).map((node) => node.data.trim())));
  assert.deepEqual(values, ["Public article text for translation.", "Learn more about DeepSeek"]);
});

test("identity filter preserves X names, YouTube channels, handles and generic author names", async () => {
  const values = await withPage(`
    <main>
      <article>
        <div data-testid="User-Name">
          <a href="https://x.com/TruthSocial"><span>Truth Social</span><span>@TruthSocial</span></a>
        </div>
        <p data-testid="tweetText">One year later, the city is shining again.</p>
      </article>
      <section>
        <ytd-channel-name><a href="https://www.youtube.com/@OpenAI">OpenAI</a></ytd-channel-name>
        <h2>A practical guide to building reliable agents</h2>
      </section>
      <section>
        <a rel="author" href="https://news.example.com/profile/global-news-desk">Global News Desk</a>
        <p>Officials released a detailed statement this morning.</p>
      </section>
    </main>`, (page) => page.evaluate(() => GWTCore.collectTextNodes(document).map((node) => node.data.trim())));
  assert.deepEqual(values, [
    "One year later, the city is shining again.",
    "A practical guide to building reliable agents",
    "Officials released a detailed statement this morning."
  ]);
});

test("inline identity extraction marks display names and handles for backend masking", async () => {
  const result = await withPage("<p>ok</p>", (page) => page.evaluate(() => ({
    prefixed: GWTCore.extractProtectedIdentityTerms("The Western Journal (@WestJournalism): One year later, D.C. is shining again."),
    mention: GWTCore.extractProtectedIdentityTerms("Thanks to @OpenAI for publishing the research."),
    plain: GWTCore.extractProtectedIdentityTerms("A normal article sentence without account identifiers.")
  })));
  assert.deepEqual(result.prefixed, ["The Western Journal", "@WestJournalism"]);
  assert.deepEqual(result.mention, ["@OpenAI"]);
  assert.deepEqual(result.plain, []);
});

test("language detection skips simplified Chinese but not English or traditional Chinese", async () => {
  const result = await withPage("<p>ok</p>", (page) => page.evaluate(() => ({
    simplified: GWTCore.isMostlySimplifiedChinese(["这是一个简体中文网页，我们正在查看页面内容和翻译设置。"], "zh-CN"),
    english: GWTCore.isMostlySimplifiedChinese(["This is an English article about software design."], "en"),
    traditional: GWTCore.isMostlySimplifiedChinese(["這是一個繁體中文網頁，我們正在檢視頁面內容與翻譯設定。"], "zh-TW"),
    localizedGitHub: GWTCore.isMostlySimplifiedChinese([
      "代码", "议题", "拉取请求", "项目", "安全和质量",
      "Diagram Design gives Claude practical patterns for creating clear technical diagrams.",
      "docs: unify product naming across plugin documentation"
    ], "zh-CN")
  })));
  assert.equal(result.simplified, true);
  assert.equal(result.english, false);
  assert.equal(result.traditional, false);
  assert.equal(result.localizedGitHub, false);
});

test("mixed-language filter translates prose but skips Chinese UI, paths and filenames", async () => {
  const result = await withPage("<p>ok</p>", (page) => page.evaluate(() => ({
    chineseUi: GWTCore.shouldTranslateText("安全和质量", "zh-CN"),
    englishDescription: GWTCore.shouldTranslateText("Create clear technical diagrams for product documentation.", "zh-CN"),
    mixedProse: GWTCore.shouldTranslateText("使用 Diagram Design to create accessible diagrams", "zh-CN"),
    repositoryPath: GWTCore.shouldTranslateText("skills/diagram-design", "zh-CN"),
    filename: GWTCore.shouldTranslateText("CONTRIBUTING.md", "zh-CN")
  })));
  assert.equal(result.chineseUi, false);
  assert.equal(result.englishDescription, true);
  assert.equal(result.mixedProse, true);
  assert.equal(result.repositoryPath, false);
  assert.equal(result.filename, false);
});

test("Chinese and bilingual modes reuse one translation and preserve link target", async () => {
  const result = await withPage('<a id="target" href="/products/deepseek?q=1#details">Learn more about DeepSeek</a>', (page) => page.evaluate(() => {
    const link = document.getElementById("target");
    const href = link.getAttribute("href");
    const node = link.firstChild;
    const store = new GWTCore.TranslationStore(document);
    const record = store.track(node);
    store.setTranslation(record, "了解更多 DeepSeek 信息");
    const chinese = link.textContent;
    store.setMode("bilingual");
    const bilingual = link.textContent;
    const wrappers = link.querySelectorAll(".gwt-bilingual").length;
    store.setMode("zh");
    const chineseAgain = link.textContent;
    store.restoreAll();
    return {
      hrefBefore: href,
      hrefAfter: link.getAttribute("href"),
      chinese,
      bilingual,
      wrappers,
      chineseAgain,
      restored: link.textContent,
      translatedCount: store.translatedCount()
    };
  }));
  assert.equal(result.hrefAfter, result.hrefBefore);
  assert.equal(result.chinese, "了解更多 DeepSeek 信息");
  assert.match(result.bilingual, /Learn more about DeepSeek/);
  assert.match(result.bilingual, /了解更多 DeepSeek 信息/);
  assert.equal(result.wrappers, 1);
  assert.equal(result.chineseAgain, result.chinese);
  assert.equal(result.restored, "Learn more about DeepSeek");
  assert.equal(result.translatedCount, 1);
});

test("dynamic content adds only new untracked nodes", async () => {
  const result = await withPage('<main id="feed"><p>First foreign-language post appears here.</p></main>', (page) => page.evaluate(() => {
    const store = new GWTCore.TranslationStore(document);
    const initial = GWTCore.collectTextNodes(document);
    for (const node of initial) store.track(node);
    const post = document.createElement("p");
    post.textContent = "A newly loaded second post appears during scrolling.";
    document.getElementById("feed").appendChild(post);
    const all = GWTCore.collectTextNodes(document);
    return {
      initial: initial.length,
      all: all.length,
      untracked: all.filter((node) => !store.has(node)).map((node) => node.data.trim())
    };
  }));
  assert.equal(result.initial, 1);
  assert.equal(result.all, 2);
  assert.deepEqual(result.untracked, ["A newly loaded second post appears during scrolling."]);
});

test("batching respects segment and character limits", async () => {
  const result = await withPage("<p>ok</p>", (page) => page.evaluate(() => {
    const records = [
      { original: "A".repeat(20) },
      { original: "B".repeat(20) },
      { original: "C".repeat(20) },
      { original: "D".repeat(20) }
    ];
    return GWTCore.batchRecords(records, 2, 45).map((batch) => batch.length);
  }));
  assert.deepEqual(result, [2, 2]);
});

async function installContentScript(page, html, language, runtimeFailure) {
  await page.setContent(html);
  await page.evaluate(({ lang, runtimeFailure }) => {
    document.documentElement.lang = lang || "en";
    window.__translateCalls = 0;
    window.__translateSegments = [];
    window.__translateProtected = [];
    window.__translatePageIds = [];
    window.__contentListener = null;
    const storage = { autoTranslate: true, displayMode: "zh", siteRules: {} };
    window.chrome = {
      storage: {
        local: {
          get(defaults, callback) { callback(Object.assign({}, defaults, storage)); },
          set(values, callback) { Object.assign(storage, values); if (callback) callback(); }
        }
      },
      runtime: {
        lastError: null,
        onMessage: {
          addListener(listener) { window.__contentListener = listener; }
        },
        sendMessage(message, callback) {
          if (runtimeFailure === "throw") throw new Error("Extension context invalidated.");
          if (runtimeFailure === "reject") return Promise.reject(new Error("Extension context invalidated."));
          if (message.type === "GWT_TRANSLATE_BATCH") {
            window.__translateCalls += 1;
            window.__translatePageIds.push(message.payload.page_id);
            window.__translateSegments.push(...message.payload.segments.map((segment) => segment.text));
            window.__translateProtected.push(...message.payload.segments.map((segment) => ({
              text: segment.text,
              protected: segment.protected
            })));
            const translations = message.payload.segments.map((segment) => ({
              id: segment.id,
              text: `译文(${segment.text})`
            }));
            setTimeout(() => callback({ ok: true, payload: { translations, glossary: {} } }), 0);
            return;
          }
          setTimeout(() => callback({ ok: true }), 0);
        }
      }
    };
  }, { lang: language, runtimeFailure });
  await page.addScriptTag({ path: path.join(extensionRoot, "core.js") });
  await page.addScriptTag({ path: path.join(extensionRoot, "content.js") });
}

test("extension reload context invalidation is handled without an unhandled error", async () => {
  for (const runtimeFailure of ["throw", "reject"]) {
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    try {
      await installContentScript(
        page,
        '<main><p>A foreign paragraph remains open while the extension reloads.</p></main>',
        "en",
        runtimeFailure
      );
      await page.waitForFunction(() => window.__contentListener !== null);
      await page.waitForTimeout(100);
      const response = await sendContentMessage(page, { type: "GWT_GET_STATE" });
      assert.equal(response.ok, true);
      assert.equal(response.state.errorCode, "runtime_error");
      assert.deepEqual(pageErrors, []);
    } finally {
      await page.close();
    }
  }
});

async function sendContentMessage(page, message) {
  return page.evaluate((value) => new Promise((resolve, reject) => {
    if (!window.__contentListener) {
      reject(new Error("content listener missing"));
      return;
    }
    window.__contentListener(value, {}, resolve);
  }), message);
}

test("content script mode switches make zero extra API calls; stop blocks dynamic content; restore is exact", async () => {
  const page = await browser.newPage();
  try {
    await installContentScript(page, '<main id="feed"><p id="article">A complete foreign article paragraph appears here.</p></main>', "en");
    await page.waitForFunction(() => window.__translateCalls === 1 && document.getElementById("article").textContent.startsWith("译文("));
    const callsAfterTranslate = await page.evaluate(() => window.__translateCalls);
    await sendContentMessage(page, { type: "GWT_SET_MODE", mode: "bilingual" });
    await sendContentMessage(page, { type: "GWT_SET_MODE", mode: "zh" });
    const callsAfterModes = await page.evaluate(() => window.__translateCalls);
    assert.equal(callsAfterModes, callsAfterTranslate);

    await sendContentMessage(page, { type: "GWT_STOP" });
    await page.evaluate(() => {
      const item = document.createElement("p");
      item.id = "dynamic";
      item.textContent = "A dynamic post loaded after the stop command.";
      document.getElementById("feed").appendChild(item);
    });
    await page.waitForTimeout(900);
    assert.equal(await page.evaluate(() => window.__translateCalls), callsAfterTranslate);
    assert.equal(await page.locator("#dynamic").textContent(), "A dynamic post loaded after the stop command.");

    await sendContentMessage(page, { type: "GWT_RESTORE" });
    assert.equal(await page.locator("#article").textContent(), "A complete foreign article paragraph appears here.");
  } finally {
    await page.close();
  }
});

test("content script does not call API for a simplified Chinese page", async () => {
  const page = await browser.newPage();
  try {
    await installContentScript(page, '<main><p>这是一个简体中文网页，我们正在查看页面内容和翻译设置，不需要调用接口。</p></main>', "zh-CN");
    await page.waitForFunction(() => window.__contentListener !== null);
    await page.waitForTimeout(250);
    const state = await sendContentMessage(page, { type: "GWT_GET_STATE" });
    assert.equal(await page.evaluate(() => window.__translateCalls), 0);
    assert.equal(state.state.pageIsSimplifiedChinese, true);
  } finally {
    await page.close();
  }
});

test("content script translates English prose inside a GitHub page localized to Chinese", async () => {
  const page = await browser.newPage();
  try {
    await installContentScript(page, `
      <main>
        <nav><span id="ui">代码</span><span>议题</span><span>拉取请求</span><span>项目</span></nav>
        <p id="description">Create clear technical diagrams for product documentation.</p>
        <a href="/skills/diagram-design">skills/diagram-design</a>
        <span>CONTRIBUTING.md</span>
      </main>`, "zh-CN");
    await page.waitForFunction(() => window.__translateCalls === 1 && document.getElementById("description").textContent.startsWith("译文("));
    const state = await sendContentMessage(page, { type: "GWT_GET_STATE" });
    assert.equal(state.state.pageIsSimplifiedChinese, false);
    assert.equal(await page.locator("#ui").textContent(), "代码");
    assert.equal(await page.locator('a[href="/skills/diagram-design"]').textContent(), "skills/diagram-design");
    assert.equal(await page.getByText("CONTRIBUTING.md", { exact: true }).textContent(), "CONTRIBUTING.md");
  } finally {
    await page.close();
  }
});

test("content script never sends or changes account display names and handles", async () => {
  const page = await browser.newPage();
  try {
    await installContentScript(page, `
      <main>
        <article>
          <div data-testid="User-Name">
            <a href="https://x.com/WesternJournal"><span id="display-name">The Western Journal</span></a>
            <span id="handle">@WestJournalism</span>
          </div>
          <p id="post" data-testid="tweetText">The Western Journal (@WestJournalism): One year later, the city is shining again.</p>
        </article>
        <section>
          <ytd-channel-name><a id="channel" href="https://www.youtube.com/@OpenAI">OpenAI</a></ytd-channel-name>
          <p id="video-description">A complete introduction to reliable AI systems.</p>
        </section>
      </main>`, "en");
    await page.waitForFunction(() => window.__translateCalls === 1 && document.getElementById("post").textContent.startsWith("译文("));
    const result = await page.evaluate(() => ({
      segments: window.__translateSegments,
      protectedSegments: window.__translateProtected,
      displayName: document.getElementById("display-name").textContent,
      handle: document.getElementById("handle").textContent,
      channel: document.getElementById("channel").textContent
    }));
    assert.deepEqual(result.segments, [
      "The Western Journal (@WestJournalism): One year later, the city is shining again.",
      "A complete introduction to reliable AI systems."
    ]);
    assert.deepEqual(result.protectedSegments[0].protected, ["The Western Journal", "@WestJournalism"]);
    assert.deepEqual(result.protectedSegments[1].protected, []);
    assert.equal(result.displayName, "The Western Journal");
    assert.equal(result.handle, "@WestJournalism");
    assert.equal(result.channel, "OpenAI");
  } finally {
    await page.close();
  }
});

test("page identity is stable across batches and modes but unique per page load and tab", async () => {
  const page = await browser.newPage();
  const other = await browser.newPage();
  const html = "<main>" + Array.from({ length: 41 }, (_, n) =>
    "<p>Foreign article paragraph number " + n + " for translation.</p>").join("") + "</main>";
  try {
    await installContentScript(page, html, "en");
    await page.waitForFunction(() => window.__translateCalls === 3);
    const first = (await sendContentMessage(page, { type: "GWT_GET_STATE" })).state.pageId;
    assert.deepEqual(await page.evaluate(() => [...new Set(window.__translatePageIds)]), [first]);
    await sendContentMessage(page, { type: "GWT_STOP" });
    await sendContentMessage(page, { type: "GWT_SET_MODE", mode: "bilingual" });
    await sendContentMessage(page, { type: "GWT_SET_AUTO", enabled: true });
    assert.equal((await sendContentMessage(page, { type: "GWT_GET_STATE" })).state.pageId, first);
    await installContentScript(other, "<p>Another independent browser tab.</p>", "en");
    const second = (await sendContentMessage(other, { type: "GWT_GET_STATE" })).state.pageId;
    assert.notEqual(second, first);
    await page.goto("about:blank");
    await installContentScript(page, "<p>A refreshed version of the page.</p>", "en");
    const refreshed = (await sendContentMessage(page, { type: "GWT_GET_STATE" })).state.pageId;
    assert.notEqual(refreshed, first);
  } finally {
    await page.close();
    await other.close();
  }
});
