const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/web/static/model-output.js"), "utf8");
const detailScript = readFileSync(join(__dirname, "../app/web/static/task-detail.js"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));
const dataKey = (name) => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class TextNode {
  constructor(data) { this.data = data; }
  appendData(data) { this.data += data; }
  get textContent() { return this.data; }
}

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.dataset = {};
    this.children = [];
    this.className = "";
    this.open = false;
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 500;
    this.clientHeight = 200;
    this.offsetHeight = 200;
  }
  append(...nodes) {
    nodes.forEach((node) => { node.remove?.(); node.parentElement = this; this.children.push(node); });
  }
  replaceChildren(...nodes) {
    this.children.forEach((node) => { node.parentElement = null; });
    this.children = [];
    this.append(...nodes);
  }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
    this.parentElement = null;
  }
  replaceWith(node) {
    const parent = this.parentElement;
    const index = parent.children.indexOf(this);
    node.remove();
    parent.children.splice(index, 1, node);
    node.parentElement = parent;
    this.parentElement = null;
  }
  get childElementCount() { return this.children.filter((node) => node instanceof Element).length; }
  get textContent() { return this.children.map((node) => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  get innerHTML() { return this.html || ""; }
  set innerHTML(html) {
    this.html = html;
    this.replaceChildren();
    const stack = [this];
    for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith("</")) { stack.pop(); continue; }
      if (!token.startsWith("<")) { stack.at(-1).append(new TextNode(token)); continue; }
      const [, tag, attributes] = /^<([\w-]+)(.*?)>$/.exec(token);
      const node = new Element(tag);
      for (const [, name, value = ""] of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
        if (name.startsWith("data-")) node.dataset[dataKey(name.slice(5))] = value;
        else if (name === "class") node.className = value;
        else if (name === "open") node.open = true;
      }
      stack.at(-1).append(node);
      stack.push(node);
    }
  }
  matches(selector) {
    return selector.split(",").some((part) => {
      const pieces = part.trim().split(/\s+/);
      if (pieces.length > 1) return this.matches(pieces.pop()) && Boolean(this.parentElement?.closest(pieces.join(" ")));
      if (part.trim().startsWith(".")) return this.className.split(" ").includes(part.trim().slice(1));
      const tag = part.trim().match(/^[\w-]+/)?.[0];
      if (tag && tag !== this.tagName) return false;
      return [...part.matchAll(/\[([^\]]+)\]/g)].every(([, name]) => name === "open" ? this.open : Object.hasOwn(this.dataset, dataKey(name.slice(5))));
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) {
    return this.children.filter((node) => node instanceof Element).flatMap((node) => [
      ...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const outputMarkup = '<details data-model-output="a"><summary>查看检查过程<small data-output-status></small></summary><div data-output-scroll><div data-output-requests></div><div data-output-fallback><details data-output-thinking><summary>思考</summary><p>等待输出</p></details><pre>暂无输出</pre></div></div></details>';
class CustomEvent {
  constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
}

function createUi(codes = ["a"], active = false) {
  const root = new Element();
  root.dataset = { modelOutputUrl: "/tasks/1/model-output", detailActive: active ? "1" : "0" };
  const listeners = {};
  const timers = new Map();
  let timerId = 0;
  root.addEventListener = (type, callback) => { (listeners[type] ||= []).push(callback); };
  root.dispatchEvent = (event) => { (listeners[event.type] || []).forEach((callback) => callback(event)); };
  codes.forEach((code) => {
    const article = new Element();
    article.dataset = { detailResult: code, outputExecution: "0", checkAttempt: "1" };
    article.innerHTML = outputMarkup.replace('data-model-output="a"', `data-model-output="${code}"`);
    root.append(article);
  });
  const responses = [];
  const requests = [];
  const context = {
    document: {
      hidden: false,
      querySelector() { return root; },
      addEventListener() {},
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => new TextNode(text),
    },
    window: { location: { href: "http://localhost/tasks/1" } },
    URL, AbortSignal, CustomEvent,
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url) {
      requests.push(url);
      const response = responses.shift();
      assert.ok(response, "unexpected output request");
      return { ok: true, headers: { get() { return "application/json"; } }, async json() { return response; } };
    },
  };
  runInNewContext(script, context);
  return {
    root, context, requests,
    panel(code = "a") { return root.querySelectorAll("[data-model-output]").find((node) => node.dataset.modelOutput === code); },
    blocks(code = "a") { return this.panel(code).querySelectorAll("[data-output-request]"); },
    queue(events, extra = {}) { responses.push({ events, cursor: requests.length * 100 + 100, active: true, more: false, checks: {}, ...extra }); },
    async toggle(node, open = !node.open) {
      node.open = open;
      root.dispatchEvent({ type: "toggle", target: node });
      await settle();
    },
    async poll() {
      const [id, callback] = [...timers].at(-1);
      timers.delete(id);
      callback();
      await settle();
    },
  };
}

function event(stream, kind, text = "", extra = {}) {
  return { stream, kind, text, codes: ["a"], attempt: 1, executions: { a: 0 }, ...extra };
}

test("all historical requests are separate collapsed blocks with independently expandable thinking", async () => {
  const ui = createUi();
  ui.queue([event("first", "start"), event("first", "thinking", "推理一"), event("first", "content", "输出一")], { more: true });
  await ui.toggle(ui.panel(), true);
  assert.equal(ui.blocks().length, 0);
  assert.equal(ui.panel().querySelector("[data-output-status]").textContent, "正在读取检查过程…");
  ui.queue([event("first", "end"), event("second", "start"), event("second", "thinking", "<script>原文</script>"), event("second", "content", "输出二"), event("second", "end")], { active: false });
  await ui.poll();
  const [first, second] = ui.blocks();
  assert.equal(ui.blocks().length, 2);
  assert.match(first.querySelector("summary").textContent, /第 1 次请求.*已结束/);
  assert.match(second.querySelector("summary").textContent, /第 2 次请求.*已结束/);
  assert.equal(first.open, false);
  assert.equal(second.open, false);
  assert.equal(first.querySelectorAll("pre")[1].textContent, "");
  await ui.toggle(first, true);
  assert.equal(first.querySelectorAll("pre")[1].textContent, "输出一");
  assert.equal(first.querySelectorAll("pre")[0].textContent, "");
  await ui.toggle(first.querySelector("[data-output-thinking]"), true);
  assert.equal(first.querySelectorAll("pre")[0].textContent, "推理一");
  await ui.toggle(second, true);
  await ui.toggle(second.querySelector("[data-output-thinking]"), true);
  assert.equal(second.querySelectorAll("pre")[0].textContent, "<script>原文</script>");
  assert.equal(second.querySelector("script"), null);
  assert.equal(ui.requests.length, 2);
});

test("concurrent streams append in their own block and preserve user folding while receiving deltas", async () => {
  const ui = createUi();
  ui.queue([event("one", "start"), event("one", "thinking", "开始🙂"), event("one", "content", "正文一")]);
  await ui.toggle(ui.panel(), true);
  const one = ui.blocks()[0];
  assert.equal(one.open, true);
  const thinking = one.querySelector("[data-output-thinking]");
  assert.equal(thinking.open, false);
  await ui.toggle(thinking, true);
  assert.equal(one.querySelectorAll("pre")[0].textContent, "开始🙂");
  await ui.toggle(one, false);
  ui.queue([event("two", "start"), event("two", "content", "正文二"), event("one", "thinking", "继续"), event("one", "content", "追加"), event("one", "end")]);
  await ui.poll();
  assert.equal(ui.blocks()[0], one);
  assert.equal(one.open, false);
  assert.equal(thinking.open, true);
  assert.equal(one.querySelectorAll("pre")[1].textContent, "正文一");
  await ui.toggle(one, true);
  assert.equal(one.querySelectorAll("pre")[0].textContent, "开始🙂继续");
  assert.equal(one.querySelectorAll("pre")[1].textContent, "正文一追加");
  assert.equal(ui.blocks()[1].querySelectorAll("pre")[1].textContent, "正文二");
  await ui.toggle(one, false);
  await ui.toggle(one, true);
  assert.equal(one.querySelectorAll("pre")[1].textContent, "正文一追加");
});

test("HTTP retry attempts remain visible and resetting one execution preserves other checks", async () => {
  const ui = createUi(["a", "b"]);
  const shared = { codes: ["a", "b"], executions: { a: 0, b: 0 } };
  ui.queue([event("shared", "start", "", shared), event("shared", "content", "共享输出", shared), event("shared", "end", "", shared), event("retry", "start", "", { attempt: 2 }), event("retry", "content", "重试输出", { attempt: 2 })], { checks: { a: { attempt: 2 } } });
  await ui.toggle(ui.panel(), true);
  assert.equal(ui.blocks().length, 2);
  assert.match(ui.blocks()[1].querySelector("summary").textContent, /第 2 次尝试/);
  ui.queue([]);
  await ui.toggle(ui.panel("b"), true);
  const sharedBlock = ui.blocks("b")[0];
  await ui.toggle(sharedBlock, true);
  assert.equal(sharedBlock.querySelectorAll("pre")[1].textContent, "共享输出");
  ui.panel().closest("[data-detail-result]").dataset.outputExecution = "1";
  ui.root.dispatchEvent(new CustomEvent("check-output-reset", { detail: { code: "a" } }));
  assert.equal(ui.blocks().length, 0);
  ui.queue([event("shared", "content", "迟到", shared), event("fresh", "start", "", { executions: { a: 1 } }), event("fresh", "content", "新执行", { executions: { a: 1 } })]);
  await ui.poll();
  assert.equal(ui.blocks().length, 1);
  assert.equal(ui.blocks()[0].querySelectorAll("pre")[1].textContent, "新执行");
  assert.equal(ui.blocks("b")[0], sharedBlock);
  assert.equal(sharedBlock.querySelectorAll("pre")[1].textContent, "共享输出迟到");
});

test("closing the process uses state-only polling without advancing the output cursor", async () => {
  const ui = createUi(["a"], true);
  ui.queue([event("one", "start")], { cursor: 123 });
  await ui.toggle(ui.panel(), true);
  await ui.toggle(ui.panel(), false);
  ui.queue([], { cursor: 123 });
  await ui.poll();
  assert.equal(ui.requests.at(-1).searchParams.get("state_only"), "1");
  ui.queue([event("one", "content", "折叠期间的输出")], { cursor: 456 });
  await ui.toggle(ui.panel(), true);
  assert.equal(ui.requests.at(-1).searchParams.get("cursor"), "123");
  assert.equal(ui.requests.at(-1).searchParams.get("state_only"), null);
  assert.equal(ui.blocks()[0].querySelectorAll("pre")[1].textContent, "折叠期间的输出");
});

test("report fragment replacement preserves nested request and thinking states when process moves below results", async () => {
  const ui = createUi();
  ui.queue([event("one", "start"), event("one", "thinking", "思考文本"), event("one", "content", "内容")]);
  await ui.toggle(ui.panel(), true);
  const panel = ui.panel();
  const request = ui.blocks()[0];
  const thinking = request.querySelector("[data-output-thinking]");
  await ui.toggle(thinking, true);
  await ui.toggle(request, false);
  panel.querySelector("[data-output-scroll]").scrollTop = 93;
  const current = panel.closest("[data-detail-result]");
  const next = new Element();
  next.dataset = { ...current.dataset, checkTerminal: "1" };
  next.innerHTML = '<details class="suppressed"><summary>其他信息</summary></details>' + outputMarkup + '<div class="agent-check-details">输入 token 100</div>';
  const start = detailScript.indexOf("  function replaceContent(");
  const end = detailScript.indexOf("  function applyFragments(", start);
  const context = { markup: new WeakMap(), resetCheck() { assert.fail("unexpected execution reset"); } };
  runInNewContext(detailScript.slice(start, end), context);
  context.replaceContent(current, next);
  assert.equal(current.querySelector("[data-model-output]"), panel);
  assert.equal(panel.open, true);
  assert.equal(request.open, false);
  assert.equal(thinking.open, true);
  assert.equal(panel.querySelector("[data-output-scroll]").scrollTop, 93);
  assert.equal(current.querySelector(".suppressed").open, false);
});

test("automatic retries clear stale report content while only a new execution clears request history", () => {
  const ui = createUi();
  const status = new Element();
  status.className = "report-status-panel";
  ui.root.append(status);
  const current = ui.panel().closest("[data-detail-result]");
  const events = [];
  ui.root.addEventListener("check-output-reset", (event) => events.push(event.detail.code));
  const start = detailScript.indexOf("  function resetCheck(");
  const end = detailScript.indexOf("  function showCanceledCheck(", start);
  const context = { root: ui.root, markup: new WeakMap(), actionVersion: 0, CustomEvent };
  runInNewContext(detailScript.slice(start, end), context);
  context.resetCheck(current, 0, 2);
  assert.equal(events.length, 0);
  context.resetCheck(current, 1, 1);
  assert.deepEqual(events, ["a"]);
});
