(() => {
  const root = document.querySelector("[data-model-output-url]");
  if (!root) return;
  const streams = new Map();
  const rendered = new WeakMap();
  let cursor = 0;
  let pending = false;
  let finished = false;
  let timer;

  function opened() {
    return [...root.querySelectorAll("[data-model-output][open]")];
  }

  function render(panel) {
    const scroll = panel.querySelector("[data-output-scroll]");
    const following = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
    const container = panel.querySelector("[data-output-requests]");
    let nodes = rendered.get(panel);
    if (!nodes) { nodes = new Map(); rendered.set(panel, nodes); }
    streams.forEach((stream, id) => {
      if (!stream.codes.includes(panel.dataset.modelOutput)) return;
      let view = nodes.get(id);
      if (!view) {
        const section = document.createElement("section");
        section.className = "model-output-request";
        section.innerHTML = '<p class="model-output-heading"></p><h3>思考</h3><pre class="model-output-text model-output-thinking"></pre><h3>正文</h3><pre class="model-output-text"></pre>';
        const texts = [...section.querySelectorAll("pre")].map((pre) => {
          const node = document.createTextNode("");
          pre.append(node);
          return node;
        });
        view = { heading: section.querySelector("p"), thinking: texts[0], content: texts[1], thinkingLength: 0, contentLength: 0 };
        container.append(section);
        nodes.set(id, view);
      }
      view.heading.textContent = [
        `请求 ${stream.index}`, stream.label, stream.codes.length > 1 ? "合并请求" : "",
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
    });
    panel.querySelector("[data-output-fallback]").hidden = nodes.size > 0;
    panel.querySelector("[data-output-status]").textContent = "";
    if (following) scroll.scrollTop = scroll.scrollHeight;
  }

  function schedule(delay = 1000) {
    clearTimeout(timer);
    if (!document.hidden && opened().length && !finished) timer = setTimeout(read, delay);
  }

  async function read() {
    if (pending || document.hidden || !opened().length || finished) return;
    pending = true;
    let delay = 1000;
    try {
      const url = new URL(root.dataset.modelOutputUrl, window.location.href);
      url.searchParams.set("cursor", cursor);
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15000) });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("output");
      const data = await response.json();
      if (data.reset) {
        streams.clear();
        root.querySelectorAll("[data-model-output]").forEach((panel) => {
          rendered.delete(panel);
          panel.querySelector("[data-output-requests]").replaceChildren();
        });
      }
      data.events.forEach((event) => {
        let stream = streams.get(event.stream);
        if (!stream) {
          stream = { index: streams.size + 1, codes: event.codes, label: event.label, attempt: event.attempt, thinking: "", content: "", ended: false };
          streams.set(event.stream, stream);
        }
        if (event.kind === "thinking" || event.kind === "content") stream[event.kind] += event.text;
        if (event.kind === "end") stream.ended = true;
      });
      cursor = data.cursor;
      finished = !data.active && !data.more;
      opened().forEach(render);
      if (data.more) delay = 50;
    } catch (_error) {
      opened().forEach((panel) => { panel.querySelector("[data-output-status]").textContent = "读取失败，稍后重试"; });
      delay = 3000;
    } finally { pending = false; schedule(delay); }
  }

  root.addEventListener("toggle", (event) => {
    const panel = event.target;
    if (!panel.matches("[data-model-output]")) return;
    if (panel.open) { render(panel); read(); }
    else panel.querySelector("[data-output-status]").textContent = "";
    schedule();
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) read(); else clearTimeout(timer); });
})();
