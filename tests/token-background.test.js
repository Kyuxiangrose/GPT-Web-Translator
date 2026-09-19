"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extension/background.js"), "utf8");

function worker(storage = {}) {
  let listener;
  const context = vm.createContext({
    AbortController, setTimeout, clearTimeout, console,
    fetch: async () => { throw new Error("offline"); },
    chrome: {
      storage: { local: {
        async get(key) { await new Promise(r => setImmediate(r)); return { [key]: structuredClone(storage[key]) }; },
        async set(values) { await new Promise(r => setImmediate(r)); Object.assign(storage, structuredClone(values)); }
      } },
      runtime: { onMessage: { addListener(fn) { listener = fn; } } }
    }
  });
  vm.runInContext(source, context);
  return {
    context, storage,
    run(code) { return vm.runInContext(code, context); },
    send(message) { return new Promise(resolve => listener(message, {}, resolve)); },
    respond(snapshot, ok = true) {
      context.fetch = async () => ({ ok, json: async () => ({
        ok, token_stats: snapshot, ...(ok ? {} : { error: { code: "invalid_json", message: "bad translation" } })
      }) });
    }
  };
}

function snapshot(revision, history, page = "gwt_page_one", total = history) {
  return { store_id: "test-ledger", revision, history_total_tokens: history,
    history_missing_usage: 0, started_at: 1, page_id: page,
    page_total_tokens: total, page_missing_usage: 0,
    history_total_cost_nano_yuan: history * 1000, history_missing_cost: 0,
    page_total_cost_nano_yuan: total * 1000, page_missing_cost: 0,
    currency: "CNY", pricing_version: "test-pricing" };
}

test("out-of-order responses and duplicate snapshots never add usage twice", async () => {
  const w = worker();
  w.context.snapshots = [snapshot(3, 30), snapshot(2, 20), snapshot(3, 30)];
  await w.run("Promise.all(snapshots.map(rememberTokenStats))");
  assert.equal(w.storage.gwtTokenStats.history_total_tokens, 30);
  assert.equal(w.storage.gwtTokenStats.pages.gwt_page_one.total, 30);
  assert.equal(w.storage.gwtTokenStats.history_total_cost_nano_yuan, 30000);
});

test("two concurrent tabs preserve each page and newest global total", async () => {
  const w = worker();
  w.context.snapshots = [snapshot(2, 30, "gwt_page_two", 20), snapshot(1, 10)];
  await w.run("Promise.all(snapshots.map(rememberTokenStats))");
  assert.equal(w.storage.gwtTokenStats.history_total_tokens, 30);
  assert.equal(w.storage.gwtTokenStats.pages.gwt_page_one.total, 10);
  assert.equal(w.storage.gwtTokenStats.pages.gwt_page_two.total, 20);
  assert.equal(w.storage.gwtTokenStats.pages.gwt_page_two.cost_nano_yuan, 20000);
});

test("old in-flight snapshot cannot resurrect cleared history", async () => {
  const w = worker();
  w.context.snapshots = [snapshot(3, 0, "gwt_page_one", 30), snapshot(2, 30)];
  await w.run("Promise.all(snapshots.map(rememberTokenStats))");
  assert.equal(w.storage.gwtTokenStats.history_total_tokens, 0);
  assert.equal(w.storage.gwtTokenStats.pages.gwt_page_one.total, 30);
});

test("worker recreation reads persisted mirror while service is offline", async () => {
  const w = worker();
  w.respond(snapshot(1, 55));
  await w.send({ type: "GWT_GET_TOKEN_STATS", pageId: "gwt_page_one" });
  const restarted = worker(w.storage);
  const result = await restarted.send({ type: "GWT_GET_TOKEN_STATS", pageId: "gwt_page_one" });
  assert.equal(result.stale, true);
  assert.equal(result.stats.history_total_tokens, 55);
  assert.equal(result.stats.page_total_tokens, 55);
  assert.equal(result.stats.history_total_cost_nano_yuan, 55000);
  const refreshed = await restarted.send({ type: "GWT_GET_TOKEN_STATS", pageId: "gwt_new_document" });
  assert.equal(refreshed.stats.page_total_tokens, null);
});

test("usage survives a failed translation response", async () => {
  const w = worker();
  w.respond(snapshot(1, 77), false);
  const response = await w.send({ type: "GWT_TRANSLATE_BATCH", payload: { session_id: "gwt_test_session" } });
  assert.equal(response.ok, false);
  assert.equal(w.storage.gwtTokenStats.history_total_tokens, 77);
});

test("clear without confirmation never reaches server", async () => {
  const w = worker();
  let requests = 0;
  w.context.fetch = async () => { requests++; throw new Error("unexpected"); };
  const result = await w.send({ type: "GWT_CLEAR_TOKEN_STATS", pageId: "gwt_page_one" });
  assert.equal(result.ok, false);
  assert.equal(requests, 0);
});

test("mirror failure does not invalidate a successful translation", async () => {
  const w = worker();
  w.respond(snapshot(1, 99));
  w.context.chrome.storage.local.set = async () => { throw new Error("disk"); };
  const result = await w.send({ type: "GWT_TRANSLATE_BATCH", payload: { session_id: "gwt_test_session" } });
  assert.equal(result.ok, true);
});

test("official account balance is returned separately from local usage", async () => {
  const w = worker();
  w.context.fetch = async (_url, options) => {
    assert.equal(options.method, "POST");
    return { ok: true, json: async () => ({
      ok: true,
      account_balance: {
        is_available: true,
        balance_infos: [{ currency: "CNY", total_balance: "9.99" }]
      }
    }) };
  };
  const result = await w.send({ type: "GWT_GET_ACCOUNT_BALANCE" });
  assert.equal(result.ok, true);
  assert.equal(result.balance.balance_infos[0].total_balance, "9.99");
  assert.equal(w.storage.gwtTokenStats, undefined);
});
