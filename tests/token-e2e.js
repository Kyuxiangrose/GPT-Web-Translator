"use strict";
// Run with GWT_TEST_WORK, GWT_PYTHON and NODE_PATH set. Uses an isolated profile.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const project = path.resolve(__dirname, "..");
const runRoot = fs.mkdtempSync(path.join(process.env.GWT_TEST_WORK, "token-e2e-"));
const dataDir = path.join(runRoot, "data");
const profile = path.join(runRoot, "profile");
const extension = path.join(runRoot, "extension");
const browserPath = process.env.GWT_BROWSER_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
let backend, context, port, worker;

async function startBackend(wantedPort = 0) {
  backend = spawn(process.env.GWT_PYTHON,
    [path.join(project, "backend/tests/token_e2e_fixture.py"), dataDir, String(wantedPort)],
    { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  const ready = await new Promise((resolve, reject) => {
    backend.once("error", reject);
    backend.once("exit", code => reject(new Error("Fixture exited: " + code)));
    backend.stdout.once("data", data => resolve(JSON.parse(String(data))));
  });
  port = ready.port;
}

async function stopBackend() {
  if (!backend || backend.exitCode !== null) return;
  const stopped = new Promise(resolve => backend.once("exit", resolve));
  backend.kill();
  await stopped;
}

async function launch() {
  context = await chromium.launchPersistentContext(profile, {
    executablePath: browserPath, headless: true,
    args: ["--disable-extensions-except=" + extension, "--load-extension=" + extension]
  });
  worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
}

async function state(page) {
  const url = page.url();
  return worker.evaluate(async url => {
    const tabs = await chrome.tabs.query({});
    const tab = tabs.find(tab => tab.url === url);
    const reply = await chrome.tabs.sendMessage(tab.id, { type: "GWT_GET_STATE" });
    return { ...reply.state, tabId: tab.id };
  }, url);
}

async function stats(pageId = "") {
  return worker.evaluate(async pageId => {
    const response = await fetch("http://127.0.0.1:" + PORT + "/v1/usage", {
      method: "POST", headers: { "Content-Type": "application/json", "X-GWT-Client": "gwt-extension-v1" },
      body: JSON.stringify({ page_id: pageId })
    });
    return (await response.json()).token_stats;
  }, pageId);
}

async function waitTranslated(page) {
  await page.waitForFunction(() => Array.from(document.querySelectorAll("p"))
    .every(node => node.textContent.startsWith("这段")), null, { timeout: 20000 });
}

async function popupFor(pageState) {
  const popup = await context.newPage();
  await popup.addInitScript(tabId => {
    chrome.tabs.query = (_query, callback) => callback([{ id: tabId }]);
  }, pageState.tabId);
  await popup.goto(worker.url().replace("background.js", "popup.html"));
  return popup;
}

async function main() {
  await startBackend();
  fs.cpSync(path.join(project, "extension"), extension, { recursive: true });
  const bg = path.join(extension, "background.js");
  fs.writeFileSync(bg, fs.readFileSync(bg, "utf8").replace("127.0.0.1:8765", "127.0.0.1:" + port));
  const manifestFile = path.join(extension, "manifest.json");
  fs.writeFileSync(manifestFile, fs.readFileSync(manifestFile, "utf8").replace("127.0.0.1:8765", "127.0.0.1:" + port));
  await launch();
  await worker.evaluate(port => { globalThis.PORT = port; }, port);
  const first = await context.newPage(), second = await context.newPage();
  await Promise.all([
    first.goto("http://127.0.0.1:" + port + "/fixture?count=41&label=A"),
    second.goto("http://127.0.0.1:" + port + "/fixture?count=1&label=B")
  ]);
  await Promise.all([waitTranslated(first), waitTranslated(second)]);
  let one = await state(first), two = await state(second);
  assert.notEqual(one.pageId, two.pageId);
  assert.equal((await stats(one.pageId)).page_total_tokens, 341);
  assert.equal((await stats(two.pageId)).page_total_tokens, 101);
  assert.equal((await stats()).history_total_tokens, 442);
  assert.equal((await stats(one.pageId)).page_total_cost_nano_yuan, 547500);
  assert.equal((await stats(two.pageId)).page_total_cost_nano_yuan, 125500);
  assert.equal((await stats()).history_total_cost_nano_yuan, 673000);
  const popup = await popupFor(one);
  await popup.waitForFunction(() => document.getElementById("historyTokens").textContent === "442");
  assert.equal(await popup.locator("#pageTokens").textContent(), "341");
  assert.equal(await popup.locator("#pageCost").textContent(), "¥0.000548");
  assert.equal(await popup.locator("#historyCost").textContent(), "¥0.000673");
  await popup.locator("#modeBilingual").click();
  await popup.locator("#modeZh").click();
  assert.equal((await stats()).history_total_tokens, 442);
  await popup.locator("#clearTokens").click();
  assert.equal(await popup.locator("#tokenConfirm").isVisible(), true);
  assert.equal(await popup.locator("#clearTokens").count(), 1);
  assert.equal((await stats()).history_total_tokens, 442);
  await popup.locator("#cancelClearTokens").click();
  assert.equal((await stats()).history_total_tokens, 442);
  assert.equal((await stats()).history_total_cost_nano_yuan, 673000);
  await popup.setViewportSize({ width: 340, height: 720 });
  await popup.screenshot({ path: path.join(process.env.GWT_TEST_WORK, "token-popup-preview.png") });
  assert.equal(await popup.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await first.reload();
  await waitTranslated(first);
  const refreshed = await state(first);
  assert.notEqual(refreshed.pageId, one.pageId);
  assert.equal((await stats(refreshed.pageId)).page_total_tokens, 0);
  assert.equal((await stats(refreshed.pageId)).page_total_cost_nano_yuan, 0);
  assert.equal((await stats()).history_total_tokens, 442);
  await popup.waitForFunction(() => document.getElementById("pageTokens").textContent === "0");
  await popup.close();
  const secondPopup = await popupFor(two);
  await secondPopup.waitForFunction(() => document.getElementById("pageTokens").textContent === "101");
  await secondPopup.locator("#clearTokens").click();
  await secondPopup.locator("#confirmClearTokens").click();
  await secondPopup.waitForFunction(() => document.getElementById("historyTokens").textContent === "0");
  assert.equal((await stats(two.pageId)).page_total_tokens, 101);
  assert.equal((await stats(two.pageId)).page_total_cost_nano_yuan, 125500);
  assert.equal((await stats()).history_total_cost_nano_yuan, 0);
  await second.evaluate(() => {
    const p = document.createElement("p");
    p.textContent = "A newly inserted article paragraph arrives after clearing history.";
    document.querySelector("main").append(p);
  });
  await waitTranslated(second);
  assert.equal((await stats()).history_total_tokens, 101);
  assert.equal((await stats(two.pageId)).page_total_tokens, 202);
  assert.equal((await stats()).history_total_cost_nano_yuan, 125500);
  assert.equal((await stats(two.pageId)).page_total_cost_nano_yuan, 251000);
  await secondPopup.waitForFunction(() => document.getElementById("historyTokens").textContent === "101");
  await stopBackend();
  await secondPopup.waitForFunction(() => document.getElementById("tokenNote").textContent.includes("离线"));
  assert.equal(await secondPopup.locator("#historyTokens").textContent(), "101");
  assert.equal(await secondPopup.locator("#clearTokens").isDisabled(), true);
  await context.close();
  await startBackend(port);
  await launch();
  await worker.evaluate(port => { globalThis.PORT = port; }, port);
  assert.equal((await stats()).history_total_tokens, 101);
  assert.equal((await stats(two.pageId)).page_total_tokens, 202);
  assert.equal((await stats()).history_total_cost_nano_yuan, 125500);
  console.log(JSON.stringify({ passed: true, checks: [
    "real MV3 extension and Python backend", "three batches + second tab = 442 tokens",
    "popup Token and CNY display and mode reuse", "one cancel/confirm clear for both histories", "reload and cache cost zero",
    "new dynamic request after clear", "offline mirror", "browser and backend restart persistence"
  ], runRoot }, null, 2));
}

main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (context) await context.close();
  await stopBackend();
});
