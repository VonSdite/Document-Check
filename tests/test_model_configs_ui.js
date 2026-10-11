const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/app.js"), "utf8");

function createEditor(configs) {
  const events = {};
  class Element {
    constructor() {
      this.dataset = {};
      this.children = [];
      this.className = "";
      this.classList = { contains: (name) => this.className.split(" ").includes(name) };
    }
    append(...elements) {
      elements.forEach((element) => {
        element.parentElement = this;
        this.children.push(element);
      });
    }
    appendChild(element) { this.append(element); }
    replaceChildren(...elements) { this.children = []; this.append(...elements); }
    setAttribute(name, value) { this[name] = value; }
    matches(selector) {
      return selector.split(",").some((item) => {
        item = item.trim();
        if (item.startsWith(".")) return this.classList.contains(item.slice(1));
        const attribute = /^\[data-([a-z-]+)\]$/.exec(item);
        if (!attribute) return false;
        const key = attribute[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        return key in this.dataset;
      });
    }
    closest(selector) {
      return this.matches(selector) ? this : this.parentElement?.closest(selector) || null;
    }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [
        ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  class Input extends Element { constructor() { super(); this.value = ""; } }
  class Select extends Element {
    appendChild(option) {
      super.appendChild(option);
      if (option.selected) this.value = option.value;
    }
  }
  class TextArea extends Input {}
  class Form extends Element {}
  class Button extends Element {}
  const context = {
    document: {
      body: new Element(),
      querySelector() { return null; }, querySelectorAll() { return []; },
      getElementById() { return null; },
      createElement(tag) {
        const type = { input: Input, select: Select, textarea: TextArea, button: Button }[tag] || Element;
        return new type();
      },
      addEventListener(type, handler) { (events[type] ||= []).push(handler); },
    },
    window: { addEventListener() {}, setTimeout() {} },
    Element, HTMLElement: Element, HTMLInputElement: Input, HTMLSelectElement: Select,
    HTMLTextAreaElement: TextArea, HTMLFormElement: Form, HTMLButtonElement: Button,
    HTMLDialogElement: class extends Element {},
  };
  runInNewContext(script, context);
  const form = new Form();
  form.className = "provider-modal-form";
  form.dataset = { modelOutputDefault: "32768", modelOutputMin: "512", modelOutputMax: "262144" };
  const body = new Element();
  body.dataset.modelRows = "1";
  const hidden = new Input();
  hidden.dataset.modelConfigs = "1";
  form.append(body, hidden);
  context.renderModelRows(form, configs);
  function emit(type, target) {
    const event = { target, preventDefault() {} };
    events[type].forEach((handler) => handler(event));
  }
  return { context, form, hidden, emit, read: () => JSON.parse(hidden.value) };
}

test("editing a custom model output limit saves it independently for each thinking variant", () => {
  const ui = createEditor([
    { model_name: "custom-model", reasoning_effort: "high", max_output_tokens: 65536 },
    { model_name: "custom-model", force_disable_thinking: true, max_output_tokens: 4096 },
  ]);
  const limits = ui.form.querySelectorAll("[data-model-output-tokens]");
  assert.equal(limits.length, 2);
  assert.equal(limits[0].min, "512");
  assert.equal(limits[0].max, "262144");
  limits[0].value = "131072";
  ui.emit("input", limits[0]);
  assert.deepEqual(ui.read().map((model) => model.max_output_tokens), [131072, 4096]);
  assert.equal(ui.read()[0].reasoning_effort, "high");
  ui.emit("submit", ui.form);
  assert.deepEqual(ui.read().map((model) => model.max_output_tokens), [131072, 4096]);
});

test("tidying models and applying fetched selections preserve existing output limits", () => {
  const ui = createEditor([
    { model_name: "z-custom", max_output_tokens: 65536 },
    { model_name: "a-custom", force_disable_thinking: true, max_output_tokens: 4096 },
  ]);
  ui.context.tidyModelRows(ui.form);
  assert.deepEqual(ui.read().map((model) => [model.model_name, model.max_output_tokens]), [
    ["a-custom", 4096], ["z-custom", 65536],
  ]);
  ui.context.editorForm = ui.form;
  runInNewContext(`
    activeFetchModelForm = editorForm;
    fetchedModelCandidates = ["z-custom", "new-model"];
    fetchedModelSelection = new Set(fetchedModelCandidates);
    applyFetchedModelsSelection();
  `, ui.context);
  assert.deepEqual(ui.read().map((model) => [model.model_name, model.max_output_tokens]), [
    ["a-custom", 4096], ["z-custom", 65536], ["new-model", 32768],
  ]);
});

test("invalid model limits remain in submitted data for backend validation", () => {
  const ui = createEditor([{ model_name: "custom-model", max_output_tokens: 65536 }]);
  const limit = ui.form.querySelector("[data-model-output-tokens]");
  limit.value = "511";
  ui.emit("input", limit);
  assert.equal(ui.read()[0].max_output_tokens, 511);
});
