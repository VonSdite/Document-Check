const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/app.js"), "utf8");

function createUi(value) {
  const events = {};
  const requests = [];
  class Element {
    constructor() {
      this.dataset = {};
      this.classList = { contains: (name) => name === "provider-modal-form" };
    }
    closest() { return null; }
    matches() { return false; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
  }
  class Input extends Element {}
  class Form extends Element {}
  const form = new Form();
  const input = Object.assign(new Input(), { value, name: "api_base", form });
  input.closest = (selector) => selector === ".provider-modal-form" ? form : null;
  input.matches = (selector) => selector === '[name="api_base"]';
  form.elements = { api_base: input };
  form.dataset = { fetchModelsUrl: "/models/fetch", testModelUrl: "/models/test" };
  const context = {
    URL,
    document: {
      querySelector() { return null; },
      querySelectorAll() { return []; },
      addEventListener(type, handler) { (events[type] ||= []).push(handler); },
    },
    window: { addEventListener() {} },
    Element, HTMLElement: Element, HTMLInputElement: Input, HTMLFormElement: Form,
    HTMLSelectElement: class extends Element {}, HTMLTextAreaElement: class extends Element {},
    HTMLButtonElement: class extends Element {}, HTMLDialogElement: class extends Element {},
    async fetch(url, options) {
      requests.push({ url, body: JSON.parse(options.body) });
      return { ok: true, async json() { return { ok: true, fetched_models: [] }; } };
    },
  };
  runInNewContext(script, context);
  context.showToast = () => {};
  context.openFetchModelPicker = () => {};
  function emit(type, target) {
    const event = { target, preventDefault() { this.defaultPrevented = true; } };
    events[type].forEach((handler) => handler(event));
    return event;
  }
  return { form, input, emit, context, requests, Input };
}

test("API field completes root, base and partial chat URLs when focus leaves", () => {
  for (const [value, expected] of [
    ["http://127.0.0.1:31943", "http://127.0.0.1:31943/v1/chat/completions"],
    [" https://example.test/v1/// ", "https://example.test/v1/chat/completions"],
    ["https://example.test/proxy/v1/", "https://example.test/proxy/v1/chat/completions"],
    ["https://example.test/api/paas/v4", "https://example.test/api/paas/v4/chat/completions"],
    ["https://example.test/v1/chat/", "https://example.test/v1/chat/completions"],
    ["http://[::1]:31943/", "http://[::1]:31943/v1/chat/completions"],
    ["https://example.test/v1/?api-version=2026-01&key=a%2Fb#section", "https://example.test/v1/chat/completions?api-version=2026-01&key=a%2Fb#section"],
    ["https://example.test/custom/chat/completions?key=value/", "https://example.test/custom/chat/completions?key=value/"],
  ]) {
    const { input, emit } = createUi(value);
    emit("focusout", input);
    assert.equal(input.value, expected);
    emit("focusout", input);
    assert.equal(input.value, expected);
  }
});

test("empty and invalid API fields stay available for correction", () => {
  for (const value of ["", "example.test/v1", "ftp://example.test", "https:///v1", "http://[::1", "http://example.test:invalid/v1"]) {
    const { input, emit } = createUi(value);
    emit("focusout", input);
    assert.equal(input.value, value);
  }
});

test("saving a provider completes the API URL without a preceding focus event", () => {
  const { form, input, emit } = createUi("http://127.0.0.1:31943/v1");
  assert.equal(emit("submit", form).defaultPrevented, undefined);
  assert.equal(input.value, "http://127.0.0.1:31943/v1/chat/completions");
});

test("fetching models and testing a model send the completed API URL", async () => {
  const { form, input, context, requests, Input } = createUi("https://example.test");
  await context.fetchModelsForForm(form, {});
  assert.equal(input.value, "https://example.test/v1/chat/completions");
  input.value = "https://example.test/proxy/v1/";
  const nameInput = Object.assign(new Input(), { value: "model-a" });
  const row = { querySelector: (selector) => selector === "[data-model-name]" ? nameInput : null };
  await context.testModelForRow(form, row, {});
  assert.equal(input.value, "https://example.test/proxy/v1/chat/completions");
  assert.deepEqual(requests.map(({ url, body }) => [url, body.api_base]), [
    ["/models/fetch", "https://example.test/v1/chat/completions"],
    ["/models/test", "https://example.test/proxy/v1/chat/completions"],
  ]);
  assert.equal(requests[1].body.model_name, "model-a");
});
