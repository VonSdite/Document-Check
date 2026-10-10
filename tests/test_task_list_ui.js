const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/task-list.js"), "utf8");
const settle = () => new Promise(setImmediate);

function createUi(currentPermissions, nextPermissions) {
  const events = {};
  const requests = [];
  let reloads = 0;
  const node = (innerHTML, dataset = {}) => ({
    innerHTML, dataset, style: {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  });
  const list = node("current list", { listUrl: "/images" });
  const nextList = node("updated list", { listUrl: "/images" });
  const stats = node("current stats", {
    permissionSignature: currentPermissions, refreshActive: "0",
  });
  const nextStats = node("updated stats", {
    permissionSignature: nextPermissions, refreshActive: "0",
  });
  const feedback = { textContent: "" };
  const select = (selector, next = false) => {
    if (selector === '[data-refresh-region="task-list"]') return next ? nextList : list;
    if (selector === '[data-refresh-region="stats"]') return next ? nextStats : stats;
    if (selector === "[data-task-list-feedback]") return feedback;
    return null;
  };
  class Element {}
  runInNewContext(script, {
    document: {
      hidden: false, activeElement: null,
      documentElement: { scrollHeight: 1000 },
      querySelector: select,
      querySelectorAll() { return []; },
      addEventListener(type, handler) { (events[type] ||= []).push(handler); },
    },
    window: {
      location: { href: "https://document.example.test/images", reload() { reloads++; } },
      scrollX: 0, scrollY: 0, innerHeight: 600,
      scrollTo() {}, addEventListener() {},
      localStorage: { getItem() { return "0"; } },
    },
    history: {}, Element,
    HTMLInputElement: class extends Element {},
    HTMLButtonElement: class extends Element {},
    HTMLFormElement: class extends Element {},
    DOMParser: class {
      parseFromString() { return { querySelector(selector) { return select(selector, true); } }; }
    },
    AbortController, AbortSignal, URL, activeConfirmPopover: null,
    async fetch(url, options) {
      requests.push({ url, options });
      return { ok: true, async text() { return "updated page"; } };
    },
  });
  return {
    list, stats, feedback, requests,
    get reloads() { return reloads; },
    refresh() {
      const target = {
        closest(selector) { return selector === "[data-manual-task-refresh]" ? {} : null; },
      };
      events.click.forEach((handler) => handler({ target }));
    },
  };
}

test("manual refresh reloads the page after grants or revocation to update navigation and task scope", async () => {
  for (const [before, after] of [
    ["0:0:0:0", "1:0:0:0"],
    ["1:0:0:0", "0:0:0:0"],
    ["1:0:0:0", "1:1:0:0"],
    ["1:0:0:0", "1:0:1:0"],
    ["0:0:0:0", "0:0:0:1"],
  ]) {
    const ui = createUi(before, after);
    ui.refresh();
    await settle();
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.requests[0].url.pathname, "/images");
    assert.equal(ui.requests[0].url.searchParams.get("_partial"), "1");
    assert.equal(ui.reloads, 1);
    assert.equal(ui.list.innerHTML, "current list");
    assert.equal(ui.stats.innerHTML, "current stats");
    assert.equal(ui.feedback.textContent, "");
  }
});

test("manual refresh updates the list without reloading when permissions stay the same", async () => {
  const ui = createUi("1:0:0:0", "1:0:0:0");
  ui.refresh();
  await settle();
  assert.equal(ui.reloads, 0);
  assert.equal(ui.list.innerHTML, "updated list");
  assert.equal(ui.stats.innerHTML, "updated stats");
  assert.equal(ui.feedback.textContent, "");
});
