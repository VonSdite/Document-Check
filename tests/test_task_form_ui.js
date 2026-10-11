const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/app.js"), "utf8");

function createUi({ labels = ["文档"], model = "model-1", checks = [true, true], protect = true } = {}) {
  const events = {};
  const windowEvents = {};
  class Element {
    constructor() {
      this.dataset = {};
      this.classList = { contains() { return false; } };
    }
    closest() { return null; }
    matches() { return false; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
  }
  class Input extends Element {
    constructor() {
      super();
      this.validationMessage = "";
    }
    setCustomValidity(message) { this.validationMessage = message; }
  }
  class Select extends Input {}
  class Button extends Element {}
  class Form extends Element {
    querySelector(selector) {
      if (selector === 'select[name="model_id"]') return this.model;
      if (selector === 'input[name="checks"]') return this.checks[0] || null;
      if (selector === 'input[name="checks"]:checked') return this.checks.find((input) => input.checked) || null;
      if (selector === "[data-check-validation]") return this.emptyChecks;
      return null;
    }
    querySelectorAll(selector) {
      if (selector === 'input[type="file"][required]') return this.files;
      if (selector === 'button[type="submit"], input[type="submit"]') return [this.button];
      return [];
    }
    checkValidity() {
      return [...this.files, this.model, ...this.checks, this.emptyChecks]
        .filter(Boolean).every((input) => !input.validationMessage);
    }
    reportValidity() {
      this.reportedInput = [...this.files, this.model, ...this.checks, this.emptyChecks]
        .filter(Boolean).find((input) => input.validationMessage);
      return this.checkValidity();
    }
  }
  const form = new Form();
  form.dataset = {
    requireChecks: "true", checkRequiredMessage: "请至少选择一个检查项。",
    preventDoubleSubmit: protect ? "true" : "false", submittingLabel: "提交中...",
  };
  form.files = labels.map((label) => {
    const input = new Input();
    input.form = form;
    input.type = "file";
    input.files = [];
    input.closest = (selector) => selector === ".form-field"
      ? { querySelector() { return { textContent: label }; } } : null;
    return input;
  });
  form.model = Object.assign(new Select(), { form, name: "model_id", value: model, disabled: false, selectedOptions: [] });
  form.checks = checks.map((checked) => Object.assign(new Input(), { form, name: "checks", checked }));
  form.emptyChecks = checks.length ? null : Object.assign(new Input(), { form });
  form.button = Object.assign(new Button(), { disabled: false, textContent: "提交检查" });
  runInNewContext(script, {
    document: {
      querySelector() { return null; },
      querySelectorAll(selector) {
        return selector === 'form[data-require-checks="true"]' || selector === "form[data-prevent-double-submit='true']" ? [form] : [];
      },
      addEventListener(type, handler) { (events[type] ||= []).push(handler); },
    },
    window: {
      addEventListener(type, handler) { (windowEvents[type] ||= []).push(handler); },
      localStorage: { setItem() {} },
    },
    Element, HTMLElement: Element, HTMLInputElement: Input, HTMLSelectElement: Select,
    HTMLButtonElement: Button, HTMLFormElement: Form, HTMLDialogElement: class extends Element {},
  });
  function emit(type, target = form) {
    const event = { target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    events[type].forEach((handler) => handler(event));
    return event;
  }
  return { form, emit, windowEvents };
}

test("missing files keep submit clickable and report each document at its input", () => {
  for (const labels of [["文档"], ["素材文档", "资料文档"], ["文档A", "文档B"], ["PDF 文档"], ["视频"]]) {
    const { form, emit } = createUi({ labels });
    for (const input of form.files) {
      assert.equal(form.button.disabled, false);
      assert.equal(emit("submit").defaultPrevented, true);
      assert.equal(form.reportedInput, input);
      assert.match(input.validationMessage, /^请选择.+。$/);
      input.files = [{ name: "selected.file" }];
      emit("change", input);
      assert.equal(input.validationMessage, "");
    }
    assert.equal(form.checkValidity(), true);
  }
});

test("missing models and checks report on their controls while submit stays enabled", () => {
  const { form, emit } = createUi({ model: "", checks: [false, false] });
  form.files[0].files = [{ name: "document.txt" }];
  emit("change", form.files[0]);
  assert.equal(emit("submit").defaultPrevented, true);
  assert.equal(form.reportedInput, form.model);
  assert.match(form.model.validationMessage, /模型管理/);
  assert.equal(form.model.disabled, false);
  assert.equal(form.button.disabled, false);
  form.model.value = "model-1";
  emit("change", form.model);
  emit("submit");
  assert.equal(form.reportedInput, form.checks[0]);
  assert.equal(form.checks[0].validationMessage, "请至少选择一个检查项。");
  form.checks[1].checked = true;
  emit("change", form.checks[1]);
  assert.equal(form.checkValidity(), true);
  form.checks[1].checked = false;
  emit("change", form.checks[1]);
  assert.equal(form.checkValidity(), false);
});

test("unavailable checks use a validation input in the empty check group", () => {
  const { form, emit } = createUi({ checks: [] });
  form.files[0].files = [{ name: "document.txt" }];
  emit("change", form.files[0]);
  assert.equal(emit("submit").defaultPrevented, true);
  assert.equal(form.reportedInput, form.emptyChecks);
  assert.match(form.emptyChecks.validationMessage, /请联系管理员启用/);
  assert.equal(form.button.disabled, false);
});

test("valid submissions retain duplicate prevention and recover on return to the page", () => {
  const { form, emit, windowEvents } = createUi();
  form.files[0].files = [{ name: "document.txt" }];
  emit("change", form.files[0]);
  assert.equal(emit("submit").defaultPrevented, false);
  assert.equal(form.button.disabled, true);
  assert.equal(form.button.textContent, "提交中...");
  assert.equal(emit("submit").defaultPrevented, true);
  windowEvents.pageshow.forEach((handler) => handler());
  assert.equal(form.button.disabled, false);
  assert.equal(form.button.textContent, "提交检查");
  assert.equal(form.dataset.submitting, undefined);
  form.files[0].files = [];
  emit("change", form.files[0]);
  assert.equal(form.checkValidity(), false);
  assert.equal(form.button.disabled, false);
  assert.equal(form.files[0].validationMessage, "请选择文档。");
});
