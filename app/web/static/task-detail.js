(() => {
  const root = document.querySelector("[data-task-detail]");
  if (!root) return;
  const phaseLabels = {
    pending: "待执行", checking: "检查", waiting: "等待",
    thinking: "思考", output: "输出", retrying: "重试", canceling: "取消中",
  };
  const singleCancel = ["document_check", "consistency_check", "language_consistency_check"].includes(root.dataset.taskType);
  const markup = new WeakMap();
  root.querySelectorAll("[data-detail-region], [data-detail-result]").forEach((node) => markup.set(node, node.innerHTML));
  let pending = false;
  let actionVersion = 0;

  function interactionActive() {
    const focused = document.activeElement;
    const selection = window.getSelection();
    return (root.contains(focused) && focused.matches("input, textarea, select"))
      || (selection && !selection.isCollapsed && root.contains(selection.anchorNode));
  }

  function replaceContent(current, next) {
    const html = next.innerHTML;
    if (next.dataset.checkTerminal) current.dataset.checkTerminal = next.dataset.checkTerminal;
    if (markup.get(current) === html) return;
    const expanded = [...current.querySelectorAll("details")].map((node) => node.open);
    const output = current.querySelector("[data-model-output]");
    const outputScroll = output?.querySelector("[data-output-scroll]")?.scrollTop;
    current.innerHTML = html;
    const nextOutput = current.querySelector("[data-model-output]");
    if (output && nextOutput) {
      if (!output.querySelector("[data-output-requests]").childElementCount) {
        output.querySelector("[data-output-fallback]").replaceWith(nextOutput.querySelector("[data-output-fallback]"));
      }
      nextOutput.replaceWith(output);
      output.querySelector("[data-output-scroll]").scrollTop = outputScroll;
    }
    current.querySelectorAll("details").forEach((node, index) => { node.open = expanded[index] ?? node.open; });
    markup.set(current, html);
  }

  function applyFragments(html) {
    const documentFragment = new DOMParser().parseFromString(html, "text/html");
    const anchor = [...root.querySelectorAll("[data-detail-result]")].find((node) => node.getBoundingClientRect().bottom > 0);
    const anchorTop = anchor?.getBoundingClientRect().top;
    root.querySelectorAll("[data-detail-region]").forEach((current) => {
      const next = documentFragment.querySelector(`[data-detail-region="${current.dataset.detailRegion}"]`);
      if (next) replaceContent(current, next);
    });
    const stack = root.querySelector("[data-report-results]");
    const currentByCode = new Map([...stack.querySelectorAll("[data-detail-result]")].map((node) => [node.dataset.detailResult, node]));
    const nextCodes = new Set();
    documentFragment.querySelectorAll("[data-detail-result]").forEach((next) => {
      const code = next.dataset.detailResult;
      nextCodes.add(code);
      const current = currentByCode.get(code);
      if (current) replaceContent(current, next);
      else { stack.append(next); markup.set(next, next.innerHTML); }
    });
    currentByCode.forEach((node, code) => { if (!nextCodes.has(code)) node.remove(); });
    if (anchor?.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);
  }

  function applyProgress(data) {
    const badge = root.querySelector(".report-status-panel .status-pill");
    badge.textContent = data.status_label;
    badge.className = `status-pill status-${data.status}`;
    root.querySelector(".report-status-panel .progress span").style.width = `${data.progress}%`;
    root.querySelectorAll("[data-detail-result]").forEach((node) => {
      const state = data.checks[node.dataset.detailResult] || {};
      const phase = state.phase || (data.active && node.dataset.checkTerminal !== "1" ? "pending" : "");
      if (["completed", "failed", "canceled"].includes(phase)) node.dataset.checkTerminal = "1";
      const label = node.querySelector("[data-check-activity]");
      const text = data.active ? phaseLabels[phase] || "" : "";
      label.textContent = text + (text && state.attempt > 1 ? ` · 第 ${state.attempt}/3 次尝试` : "");
      label.hidden = !text;
      const button = node.querySelector("[data-cancel-check]");
      button.hidden = !(singleCancel && data.status === "running" && ["waiting", "thinking", "output", "retrying"].includes(phase));
    });
    root.dataset.detailActive = data.active ? "1" : "0";
    root.querySelector("[data-detail-refresh-message]").textContent = data.active ? "每 10 秒更新" : "任务已结束";
  }

  async function refresh(force = false) {
    if (pending || document.hidden || interactionActive() || (!force && root.dataset.detailActive !== "1")) return;
    pending = true;
    const version = actionVersion;
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("_poll", "1");
      url.searchParams.set("revision", root.dataset.detailRevision);
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15000) });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("refresh");
      const data = await response.json();
      if (interactionActive() || version !== actionVersion) return;
      if (data.html) applyFragments(data.html);
      applyProgress(data);
      root.dataset.detailRevision = data.revision;
    } catch (_error) {
      root.querySelector("[data-detail-refresh-message]").textContent = "状态更新暂时失败，将自动重试";
    } finally { pending = false; }
  }

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel-check]");
    if (!button || button.disabled) return;
    actionVersion += 1;
    button.disabled = true;
    try {
      const response = await fetch(root.dataset.cancelCheckUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: button.dataset.cancelCheck }), signal: AbortSignal.timeout(15000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "取消请求失败，请重试。");
      const label = button.closest("[data-detail-result]").querySelector("[data-check-activity]");
      label.hidden = false;
      label.textContent = "取消中";
      button.hidden = true;
      await refresh(true);
    } catch (error) {
      if (typeof showToast === "function") showToast(error.message || "取消请求失败，请重试。", "error");
    } finally { button.disabled = false; }
  });
  setInterval(refresh, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
