const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/permissions.js"), "utf8");
const codes = ["tasks.view_all", "tasks.manage_all", "stats.view_all", "rules.manage"];
const settle = () => new Promise(setImmediate);

function createUi(initial = [[]]) {
  const requests = [];
  const windowEvents = {};
  const rows = initial.map((selected, index) => {
    const status = { textContent: "", className: "muted" };
    const choices = codes.map((value) => ({
      name: "permissions", value, type: "checkbox",
      checked: selected.includes(value), disabled: false, events: {},
      addEventListener(type, listener) { this.events[type] = listener; },
    }));
    const form = {
      action: "https://document.example.test/admin/permissions",
      dataset: {}, events: {},
      elements: [
        ...choices,
        { name: "subject", value: `ip:10.0.0.${index + 1}`, type: "hidden" },
        { name: "csrf_token", value: "page-token", type: "hidden" },
      ],
      querySelector() { return status; },
      addEventListener(type, listener) { this.events[type] = listener; },
    };
    return {
      form, status, choices,
      checked() { return choices.filter((input) => input.checked).map((input) => input.value); },
      change(value, checked) {
        const input = choices.find((choice) => choice.value === value);
        assert.equal(input.disabled, false);
        input.checked = checked;
        input.events.change({ target: input });
      },
    };
  });
  class FormData {
    constructor(form) {
      this.entries = form.elements
        .filter((input) => input.type !== "checkbox" || input.checked)
        .map((input) => [input.name, input.value]);
    }
    getAll(name) { return this.entries.filter(([key]) => key === name).map(([, value]) => value); }
  }
  runInNewContext(script, {
    document: {
      querySelectorAll() { return rows.map((row) => row.form); },
      querySelector() { return rows.find((row) => row.form.dataset.saving === "true")?.form; },
    },
    window: { addEventListener(type, listener) { windowEvents[type] = listener; } },
    FormData,
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({ url, ...options, resolve, reject }));
    },
  });
  return {
    rows, requests,
    leave() {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      windowEvents.beforeunload(event);
      return event.prevented;
    },
  };
}

function saved(request) {
  request.resolve({
    ok: true, redirected: false,
    json: async () => ({ permissions: request.body.getAll("permissions") }),
  });
}

test("checking manage includes view and saves immediately with the page token", async () => {
  const ui = createUi();
  const [row] = ui.rows;
  row.change("tasks.manage_all", true);
  assert.deepEqual(row.checked(), ["tasks.view_all", "tasks.manage_all"]);
  assert.equal(ui.requests.length, 1);
  const request = ui.requests[0];
  assert.equal(request.method, "POST");
  assert.equal(request.headers["X-Requested-With"], "fetch");
  assert.deepEqual(request.body.getAll("permissions"), row.checked());
  assert.deepEqual(request.body.getAll("subject"), ["ip:10.0.0.1"]);
  assert.deepEqual(request.body.getAll("csrf_token"), ["page-token"]);
  assert.equal(row.status.textContent, "保存中…");
  assert.equal(ui.leave(), true);
  saved(request);
  await settle();
  assert.equal(row.status.textContent, "已保存");
  assert.equal(ui.leave(), false);
});

test("unchecking view revokes manage while keeping unrelated permissions", async () => {
  const ui = createUi([["tasks.view_all", "tasks.manage_all", "rules.manage"]]);
  const [row] = ui.rows;
  row.change("tasks.view_all", false);
  assert.deepEqual(row.checked(), ["rules.manage"]);
  assert.deepEqual(ui.requests[0].body.getAll("permissions"), ["rules.manage"]);
  saved(ui.requests[0]);
  await settle();
  assert.deepEqual(row.checked(), ["rules.manage"]);
});

test("unchecking manage retains view and all permissions can be cleared", async () => {
  const ui = createUi([["tasks.view_all", "tasks.manage_all"]]);
  const [row] = ui.rows;
  row.change("tasks.manage_all", false);
  assert.deepEqual(row.checked(), ["tasks.view_all"]);
  saved(ui.requests[0]);
  await settle();
  row.change("tasks.view_all", false);
  assert.deepEqual(ui.requests[1].body.getAll("permissions"), []);
  saved(ui.requests[1]);
  await settle();
  assert.deepEqual(row.checked(), []);
});

test("rapid clicks serialize saves and stale responses preserve the latest choices", async () => {
  const ui = createUi();
  const [row] = ui.rows;
  row.change("tasks.manage_all", true);
  row.change("tasks.view_all", false);
  row.change("stats.view_all", true);
  assert.equal(ui.requests.length, 1);
  saved(ui.requests[0]);
  await settle();
  assert.equal(ui.requests.length, 2);
  assert.deepEqual(row.checked(), ["stats.view_all"]);
  assert.deepEqual(ui.requests[1].body.getAll("permissions"), ["stats.view_all"]);
  assert.equal(row.status.textContent, "保存中…");
  saved(ui.requests[1]);
  await settle();
  assert.deepEqual(row.checked(), ["stats.view_all"]);
  assert.equal(row.status.textContent, "已保存");
});

test("failed saves restore the confirmed choices and allow another change", async (t) => {
  for (const failure of ["network", "http", "login", "html", "invalid"]) {
    await t.test(failure, async () => {
      const ui = createUi([["rules.manage"]]);
      const [row] = ui.rows;
      row.change("tasks.manage_all", true);
      const request = ui.requests[0];
      if (failure === "network") request.reject(new Error("offline"));
      else request.resolve({
        ok: failure !== "http", redirected: failure === "login",
        json: async () => {
          if (failure === "html") throw new Error("invalid JSON");
          return {};
        },
      });
      await settle();
      assert.deepEqual(row.checked(), ["rules.manage"]);
      assert.match(row.status.textContent, /保存失败/);
      assert.equal(row.status.className, "danger");
      assert.equal(ui.leave(), false);
      row.change("stats.view_all", true);
      saved(ui.requests[1]);
      await settle();
      assert.deepEqual(row.checked(), ["stats.view_all", "rules.manage"]);
      assert.equal(row.status.textContent, "已保存");
    });
  }
});

test("a failed earlier request still saves the newer queued selection", async () => {
  const ui = createUi();
  const [row] = ui.rows;
  row.change("tasks.manage_all", true);
  row.change("tasks.view_all", false);
  row.change("rules.manage", true);
  ui.requests[0].reject(new Error("offline"));
  await settle();
  assert.deepEqual(ui.requests[1].body.getAll("permissions"), ["rules.manage"]);
  saved(ui.requests[1]);
  await settle();
  assert.equal(row.status.textContent, "已保存");
  assert.deepEqual(row.checked(), ["rules.manage"]);
});

test("different user rows save independently", async () => {
  const ui = createUi([[], []]);
  ui.rows[0].change("tasks.view_all", true);
  ui.rows[1].change("rules.manage", true);
  assert.equal(ui.requests.length, 2);
  assert.deepEqual(ui.requests[0].body.getAll("subject"), ["ip:10.0.0.1"]);
  assert.deepEqual(ui.requests[1].body.getAll("subject"), ["ip:10.0.0.2"]);
  saved(ui.requests[1]);
  await settle();
  assert.equal(ui.rows[0].status.textContent, "保存中…");
  assert.equal(ui.rows[1].status.textContent, "已保存");
  saved(ui.requests[0]);
  await settle();
  assert.deepEqual(ui.rows[0].checked(), ["tasks.view_all"]);
  assert.deepEqual(ui.rows[1].checked(), ["rules.manage"]);
});
