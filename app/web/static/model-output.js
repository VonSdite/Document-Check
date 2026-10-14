(() => {
  const root = document.querySelector("[data-model-output-url]");
  if (!root) return;
  const streams = new Map();
  const requestsByCode = new Map();
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
    fallback.querySelector("pre").textContent = "暂无输出。";
    panel.querySelector("[data-output-scroll]").scrollTop = 0;
  }

  function render(panel, follow = true) {
    panel.querySelector("[data-output-status]").textContent = "";
    const ids = requestsByCode.get(panel.dataset.modelOutput);
    if (!ids?.size) return;
    const scroll = panel.querySelector("[data-output-scroll]");
    const following = follow && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
    const container = panel.querySelector("[data-output-requests]");
    let views = rendered.get(panel);
    if (!views) {
      views = new Map();
      rendered.set(panel, views);
    }
    let index = 0;
    ids.forEach((id) => {
      const stream = streams.get(id);
      let view = views.get(id);
      index += 1;
      if (!view) {
        const section = document.createElement("details");
        section.className = "model-output-request";
        section.dataset.outputRequest = id;
        section.open = !stream.ended;
        section.innerHTML = '<summary class="model-output-heading"></summary><details class="model-output-thinking-details" data-output-thinking><summary>思考</summary><pre class="model-output-text model-output-thinking"></pre></details><h3>输出</h3><pre class="model-output-text"></pre>';
        const texts = [...section.querySelectorAll("pre")].map((pre) => {
          const node = document.createTextNode("");
          pre.append(node);
          return node;
        });
        view = { section, heading: section.querySelector("summary"), thinkingDetails: section.querySelector("[data-output-thinking]"), thinking: texts[0], content: texts[1], thinkingLength: 0, contentLength: 0 };
        container.append(section);
        views.set(id, view);
      }
      const heading = [
        `第 ${index} 次请求`, stream.label, stream.codes.length > 1 ? "合并请求" : "",
        stream.attempt > 1 ? `第 ${stream.attempt} 次尝试` : "", stream.ended ? "已结束" : "接收中",
      ].filter(Boolean).join(" · ");
      if (view.heading.textContent !== heading) view.heading.textContent = heading;
      if (!view.section.open) return;
      ["thinking", "content"].forEach((kind) => {
        if (kind === "thinking" && !view.thinkingDetails.open) return;
        const lengthKey = `${kind}Length`;
        if (!stream[kind]) {
          view[kind].data = stream.ended ? (kind === "thinking" ? "未返回思考内容。" : "未返回输出。") : "等待输出…";
        } else {
          if (!view[lengthKey]) view[kind].data = "";
          view[kind].appendData(stream[kind].slice(view[lengthKey]));
          view[lengthKey] = stream[kind].length;
        }
      });
    });
    panel.querySelector("[data-output-fallback]").hidden = true;
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
        requestsByCode.clear();
        root.querySelectorAll("[data-model-output]").forEach(clearPanel);
      }
      const panels = new Map([...root.querySelectorAll("[data-model-output]")].map((panel) => [panel.dataset.modelOutput, panel]));
      const started = new Set();
      data.events.forEach((event) => {
        const codes = (event.codes || []).filter((code) => {
          const node = panels.get(code)?.closest("[data-detail-result]");
          if (!node || (event.executions?.[code] || 0) < Number(node.dataset.outputExecution || 0)) return false;
          return true;
        });
        if (!codes.length) return;
        let stream = streams.get(event.stream);
        if (!stream) {
          stream = { codes: event.codes, label: event.label, attempt: event.attempt, thinking: "", content: "", ended: false };
          streams.set(event.stream, stream);
        }
        codes.forEach((code) => {
          let ids = requestsByCode.get(code);
          if (!ids) { ids = new Set(); requestsByCode.set(code, ids); }
          if (!ids.has(event.stream)) {
            ids.add(event.stream);
            started.add(code);
          }
        });
        if (event.kind === "thinking" || event.kind === "content") stream[event.kind] += event.text;
        if (event.kind === "end") stream.ended = true;
      });
      if (started.size) root.dispatchEvent(new CustomEvent("model-output-start", { detail: { codes: [...started] } }));
      if (!stateOnly) cursor = data.cursor;
      finished = !data.active && !data.more && root.dataset.detailActive !== "1";
      if (!data.more) opened().forEach((panel) => render(panel));
      if (data.more) {
        opened().forEach((panel) => { panel.querySelector("[data-output-status]").textContent = "正在读取检查过程…"; });
        delay = 50;
      }
    } catch (_error) {
      opened().forEach((panel) => { panel.querySelector("[data-output-status]").textContent = "读取失败，稍后重试"; });
      delay = 3000;
    } finally { pending = false; schedule(delay); }
  }

  root.addEventListener("task-check-action", () => { version += 1; });
  root.addEventListener("check-output-reset", (event) => {
    version += 1;
    const code = event.detail.code;
    requestsByCode.delete(code);
    const visible = new Set([...requestsByCode.values()].flatMap((ids) => [...ids]));
    streams.forEach((_stream, id) => { if (!visible.has(id)) streams.delete(id); });
    root.querySelectorAll("[data-model-output]").forEach((panel) => {
      if (panel.dataset.modelOutput === code) clearPanel(panel);
    });
    finished = false;
    schedule(0);
  });
  root.addEventListener("toggle", (event) => {
    const panel = event.target;
    if (!panel.matches("[data-model-output]")) {
      if (panel.open && panel.matches("[data-output-request], [data-output-thinking]")) {
        const output = panel.closest("[data-model-output]");
        if (output?.open) render(output, false);
      }
      return;
    }
    if (panel.open) { finished = false; render(panel, false); read(); }
    else panel.querySelector("[data-output-status]").textContent = "";
    schedule();
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) read(); else clearTimeout(timer); });
  root.addEventListener("task-detail-updated", () => {
    if (root.dataset.detailActive === "1") { finished = false; schedule(0); }
  });
  schedule();
})();
