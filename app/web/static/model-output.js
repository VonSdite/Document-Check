(() => {
  const root = document.querySelector("[data-model-output-url]");
  if (!root) return;
  const streams = new Map();
  const latest = new Map();
  const rendered = new WeakMap();
  let cursor = 0;
  let pending = false;
  let finished = false;
  let version = 0;
  let timer;

  function opened() {
    return [...root.querySelectorAll("[data-model-output][open]")];
  }

  function clearPanel(panel) {
    rendered.delete(panel);
    panel.querySelector("[data-output-status]").textContent = "";
    panel.querySelector("[data-output-requests]").replaceChildren();
    const fallback = panel.querySelector("[data-output-fallback]");
    fallback.hidden = false;
    fallback.querySelector("p").textContent = "等待模型输出。";
    fallback.querySelector("pre").textContent = "暂无正文。";
    panel.querySelector("[data-output-scroll]").scrollTop = 0;
  }

  function render(panel) {
    panel.querySelector("[data-output-status]").textContent = "";
    const id = latest.get(panel.dataset.modelOutput);
    const stream = streams.get(id);
    if (!stream) return;
    const scroll = panel.querySelector("[data-output-scroll]");
    const following = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
    const container = panel.querySelector("[data-output-requests]");
    let view = rendered.get(panel);
    if (!view || view.id !== id) {
      const section = document.createElement("section");
      section.className = "model-output-request";
      section.innerHTML = '<p class="model-output-heading"></p><h3>思考</h3><pre class="model-output-text model-output-thinking"></pre><h3>正文</h3><pre class="model-output-text"></pre>';
      const texts = [...section.querySelectorAll("pre")].map((pre) => {
        const node = document.createTextNode("");
        pre.append(node);
        return node;
      });
      view = { id, heading: section.querySelector("p"), thinking: texts[0], content: texts[1], thinkingLength: 0, contentLength: 0 };
      container.replaceChildren(section);
      rendered.set(panel, view);
    }
    view.heading.textContent = [
      stream.label, stream.codes.length > 1 ? "合并请求" : "",
      stream.attempt > 1 ? `第 ${stream.attempt} 次尝试` : "", stream.ended ? "请求结束" : "接收中",
    ].filter(Boolean).join(" · ");
    ["thinking", "content"].forEach((kind) => {
      const lengthKey = `${kind}Length`;
      if (!stream[kind]) {
        view[kind].data = stream.ended ? (kind === "thinking" ? "未返回思考内容。" : "未返回正文。") : "等待输出…";
      } else {
        if (!view[lengthKey]) view[kind].data = "";
        view[kind].appendData(stream[kind].slice(view[lengthKey]));
        view[lengthKey] = stream[kind].length;
      }
    });
    panel.querySelector("[data-output-fallback]").hidden = true;
    panel.querySelector("[data-output-status]").textContent = "";
    if (following) scroll.scrollTop = scroll.scrollHeight;
  }

  function schedule(delay = 1000) {
    clearTimeout(timer);
    if (!document.hidden && !finished && (opened().length || root.dataset.detailActive === "1")) {
      timer = setTimeout(read, delay);
    }
  }

  async function read() {
    if (pending || document.hidden || finished) return;
    const stateOnly = !opened().length;
    if (stateOnly && root.dataset.detailActive !== "1") return;
    pending = true;
    const requestedVersion = version;
    let delay = 1000;
    try {
      const url = new URL(root.dataset.modelOutputUrl, window.location.href);
      url.searchParams.set("cursor", cursor);
      if (stateOnly) url.searchParams.set("state_only", "1");
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15000) });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("output");
      const data = await response.json();
      if (requestedVersion !== version) { delay = 0; return; }
      root.dispatchEvent(new CustomEvent("task-check-states", { detail: data }));
      if (data.reset) {
        streams.clear();
        latest.clear();
        root.querySelectorAll("[data-model-output]").forEach(clearPanel);
      }
      const panels = new Map([...root.querySelectorAll("[data-model-output]")].map((panel) => [panel.dataset.modelOutput, panel]));
      const started = new Set();
      data.events.forEach((event) => {
        const codes = (event.codes || []).filter((code) => {
          const node = panels.get(code)?.closest("[data-detail-result]");
          if (!node || (event.executions?.[code] || 0) < Number(node.dataset.outputExecution || 0)) return false;
          const state = data.checks?.[code];
          return !(state && event.attempt < state.attempt);
        });
        if (!codes.length) return;
        let stream = streams.get(event.stream);
        if (!stream) {
          stream = { codes: event.codes, label: event.label, attempt: event.attempt, thinking: "", content: "", ended: false };
          streams.set(event.stream, stream);
        }
        codes.forEach((code) => {
          if (event.kind === "start" || !latest.has(code)) {
            latest.set(code, event.stream);
            started.add(code);
          }
        });
        if (event.kind === "thinking" || event.kind === "content") stream[event.kind] += event.text;
        if (event.kind === "end") stream.ended = true;
      });
      const visible = new Set(latest.values());
      streams.forEach((_stream, id) => { if (!visible.has(id)) streams.delete(id); });
      if (started.size) root.dispatchEvent(new CustomEvent("model-output-start", { detail: { codes: [...started] } }));
      if (!stateOnly) cursor = data.cursor;
      finished = !data.active && !data.more && root.dataset.detailActive !== "1";
      if (!data.more) opened().forEach(render);
      if (data.more) delay = 50;
    } catch (_error) {
      opened().forEach((panel) => { panel.querySelector("[data-output-status]").textContent = "读取失败，稍后重试"; });
      delay = 3000;
    } finally { pending = false; schedule(delay); }
  }

  root.addEventListener("check-output-reset", (event) => {
    version += 1;
    const code = event.detail.code;
    latest.delete(code);
    root.querySelectorAll("[data-model-output]").forEach((panel) => {
      if (panel.dataset.modelOutput === code) clearPanel(panel);
    });
    finished = false;
    schedule(0);
  });
  root.addEventListener("toggle", (event) => {
    const panel = event.target;
    if (!panel.matches("[data-model-output]")) return;
    if (panel.open) { finished = false; render(panel); read(); }
    else panel.querySelector("[data-output-status]").textContent = "";
    schedule();
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) read(); else clearTimeout(timer); });
  root.addEventListener("task-detail-updated", () => {
    if (root.dataset.detailActive === "1") { finished = false; schedule(0); }
  });
  schedule();
})();
