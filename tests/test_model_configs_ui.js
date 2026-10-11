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

test("editing thinking settings saves variants without output token configuration", () => {
  const ui = createEditor([
    { model_name: "custom-model", reasoning_effort: "high", max_output_tokens: 65536 },
    { model_name: "custom-model", force_disable_thinking: true, max_output_tokens: 4096 },
  ]);
  assert.equal(ui.form.querySelectorAll("[data-model-output-tokens]").length, 0);
  const reasoning = ui.form.querySelector("[data-model-reasoning-effort]");
  reasoning.value = "medium";
  ui.emit("change", reasoning);
  assert.equal(ui.read()[0].reasoning_effort, "medium");
  assert.deepEqual(ui.read().map((model) => model.force_disable_thinking), [false, true]);
  ui.emit("submit", ui.form);
  assert.equal(ui.read()[0].reasoning_effort, "medium");
  assert.ok(ui.read().every((model) => !("max_output_tokens" in model)));
});

test("tidying models and applying fetched selections preserve thinking variants", () => {
  const ui = createEditor([
    { model_name: "z-custom", max_output_tokens: 65536 },
    { model_name: "a-custom", force_disable_thinking: true, max_output_tokens: 4096 },
  ]);
  ui.context.tidyModelRows(ui.form);
  assert.deepEqual(ui.read().map((model) => [model.model_name, model.force_disable_thinking]), [
    ["a-custom", true], ["z-custom", false],
  ]);
  ui.context.editorForm = ui.form;
  runInNewContext(`
    activeFetchModelForm = editorForm;
    fetchedModelCandidates = ["z-custom", "new-model"];
    fetchedModelSelection = new Set(fetchedModelCandidates);
    applyFetchedModelsSelection();
  `, ui.context);
  assert.deepEqual(ui.read().map((model) => [model.model_name, model.force_disable_thinking]), [
    ["a-custom", true], ["z-custom", false], ["new-model", false],
  ]);
  assert.ok(ui.read().every((model) => !("max_output_tokens" in model)));
});

test("editing a model name synchronizes the submitted configuration", () => {
  const ui = createEditor([{ model_name: "custom-model", max_output_tokens: 65536 }]);
  const name = ui.form.querySelector("[data-model-name]");
  name.value = "renamed-model";
  ui.emit("input", name);
  assert.equal(ui.read()[0].model_name, "renamed-model");
  assert.deepEqual(Object.keys(ui.read()[0]).sort(), [
    "force_disable_thinking", "model_name", "reasoning_effort",
  ]);
});
