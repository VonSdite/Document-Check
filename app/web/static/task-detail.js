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
  let actionPending = false;
  let refreshQueued = false;

  function interactionActive() {
    const focused = document.activeElement;
    const selection = window.getSelection();
    return actionPending || Boolean(document.querySelector(".confirm-popover"))
      || (root.contains(focused) && focused.matches("input, textarea, select"))
      || (selection && !selection.isCollapsed && root.contains(selection.anchorNode));
  }

  function replaceContent(current, next) {
    const html = next.innerHTML;
    if (next.matches("[data-detail-result]")
        && (Number(next.dataset.outputExecution) > Number(current.dataset.outputExecution)
          || (Number(next.dataset.checkAttempt) > 1 && Number(next.dataset.checkAttempt) > Number(current.dataset.checkAttempt)))) {
      resetCheck(current, Number(next.dataset.outputExecution), Number(next.dataset.checkAttempt));
    }
    if (next.dataset.checkTerminal) current.dataset.checkTerminal = next.dataset.checkTerminal;
    if (next.dataset.checkPhase) current.dataset.checkPhase = next.dataset.checkPhase;
    if (next.dataset.checkExecution) current.dataset.checkExecution = next.dataset.checkExecution;
    if (next.dataset.outputExecution) current.dataset.outputExecution = next.dataset.outputExecution;
    if (next.dataset.checkAttempt) current.dataset.checkAttempt = next.dataset.checkAttempt;
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
    root.style.minHeight = "";
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
    if (anchor?.isConnected) {
      const targetY = window.scrollY + anchor.getBoundingClientRect().top - anchorTop;
      const missingHeight = targetY + window.innerHeight - document.documentElement.scrollHeight;
      if (missingHeight > 0) root.style.minHeight = `${root.offsetHeight + missingHeight}px`;
      window.scrollTo(window.scrollX, targetY);
    }
  }

  function resetCheck(node, outputExecution, attempt = 0) {
    root.style.minHeight = `${root.offsetHeight}px`;
    const status = root.querySelector(".report-status-panel");
    status.querySelectorAll(".report-error").forEach((error) => error.remove());
    markup.delete(status);
    [...node.children].forEach((child) => {
      if (!child.matches(".panel-head, [data-model-output]")) child.remove();
    });
    node.dataset.outputExecution = outputExecution;
    node.dataset.checkAttempt = attempt;
    markup.delete(node);
    actionVersion += 1;
    root.dispatchEvent(new CustomEvent("check-output-reset", { detail: { code: node.dataset.detailResult } }));
  }

  function showCanceledCheck(node) {
    let error = node.querySelector(":scope > .report-error");
    if (!error) {
      error = document.createElement("div");
      error.className = "error-box report-error";
      const anchor = node.querySelector("[data-model-output]") || node.querySelector(".panel-head");
      anchor.after(error);
    }
    const message = "本检查项已取消：本检查项已由用户取消，已接收的内容仅供参考。";
    if (error.textContent !== message) {
      error.textContent = message;
      markup.delete(node);
    }
  }

  function applyCheckStates(data) {
    root.querySelectorAll("[data-detail-result]").forEach((node) => {
      const state = data.checks[node.dataset.detailResult] || {};
      const execution = state.execution ?? Number(node.dataset.checkExecution);
      const outputExecution = execution + (state.retry_requested ? 1 : 0);
      const attempt = state.attempt || 0;
      if (outputExecution > Number(node.dataset.outputExecution)
          || (attempt > 1 && attempt > Number(node.dataset.checkAttempt))) {
        resetCheck(node, outputExecution, attempt);
      }
      if (state.phase) node.dataset.checkAttempt = attempt;
      const phase = state.retry_requested ? "pending" : state.phase || node.dataset.checkPhase || "";
      node.dataset.checkPhase = phase;
      node.dataset.checkExecution = state.execution ?? node.dataset.checkExecution;
      node.dataset.checkTerminal = ["completed", "failed", "canceled"].includes(phase) ? "1" : "0";
      if (phase === "canceled") showCanceledCheck(node);
      const label = node.querySelector("[data-check-activity]");
      const text = data.active ? phaseLabels[phase] || "" : "";
      label.textContent = text ? `状态：${text}` + (state.attempt > 1 ? ` · 第 ${state.attempt}/3 次尝试` : "") : "";
      label.hidden = !text;
      const button = node.querySelector("[data-cancel-check]");
      button.hidden = !(singleCancel && ["queued", "running"].includes(data.status) && ["pending", "checking", "waiting", "thinking", "output", "retrying"].includes(phase));
      node.querySelector("[data-retry-check]").hidden = !((singleCancel || !data.active) && data.status !== "canceling" && data.phase !== "finalizing" && ["failed", "canceled", "canceling"].includes(phase));
    });
  }

  function applyProgress(data) {
    const badge = root.querySelector(".report-status-panel .status-pill");
    badge.textContent = data.status_label;
    badge.className = `status-pill status-${data.status}`;
    root.querySelector(".report-status-panel .progress span").style.width = `${data.progress}%`;
    applyCheckStates(data);
    root.dataset.detailActive = data.active ? "1" : "0";
    const refreshMessage = root.querySelector("[data-detail-refresh-message]");
    refreshMessage.textContent = "";
    refreshMessage.hidden = true;
    root.dispatchEvent(new CustomEvent("task-detail-updated"));
  }

  async function refresh(force = false) {
    if (pending) { refreshQueued ||= force; return; }
    if (document.hidden || interactionActive() || (!force && root.dataset.detailActive !== "1")) return;
    pending = true;
    refreshQueued = false;
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
      const refreshMessage = root.querySelector("[data-detail-refresh-message]");
      refreshMessage.textContent = "状态更新暂时失败，将自动重试";
      refreshMessage.hidden = false;
    } finally {
      pending = false;
      if (refreshQueued) { refreshQueued = false; refresh(true); }
    }
  }

  async function performCheckAction(button, action) {
    if (!button.isConnected || button.disabled || button.hidden || actionPending) return;
    actionVersion += 1;
    actionPending = true;
    button.disabled = true;
    try {
      const node = button.closest("[data-detail-result]");
      const response = await fetch(action === "retry" ? root.dataset.retryCheckUrl : root.dataset.cancelCheckUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: node.dataset.detailResult, execution: Number(node.dataset.checkExecution) }), signal: AbortSignal.timeout(15000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "操作失败，请重试。");
      root.dispatchEvent(new CustomEvent("task-check-action"));
      const phase = data.status;
      const text = phaseLabels[phase] || "";
      const label = node.querySelector("[data-check-activity]");
      label.hidden = !text;
      label.textContent = text ? `状态：${text}` : "";
      node.dataset.checkPhase = phase;
      node.dataset.checkTerminal = ["completed", "failed", "canceled"].includes(phase) ? "1" : "0";
      if (phase === "canceled") showCanceledCheck(node);
      node.querySelector("[data-cancel-check]").hidden = action !== "retry" || !singleCancel;
      node.querySelector("[data-retry-check]").hidden = action === "retry";
      root.dataset.detailRevision = "";
      if (action === "retry") {
        resetCheck(node, data.output_execution ?? Number(node.dataset.checkExecution) + 1);
        node.dataset.checkExecution = data.execution ?? node.dataset.checkExecution;
        node.dataset.checkPhase = "pending";
        node.dataset.checkTerminal = "0";
        root.dataset.detailActive = "1";
        root.dispatchEvent(new CustomEvent("task-detail-updated"));
      }
    } catch (error) {
      if (typeof showToast === "function") showToast(error.message || "操作失败，请重试。", "error");
    } finally { button.disabled = false; actionPending = false; }
    await refresh(true);
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cancel-check], [data-retry-check]");
    if (!button || button.disabled || actionPending) return;
    event.preventDefault();
    event.stopPropagation();
    if (button.matches("[data-retry-check]")) { performCheckAction(button, "retry"); return; }
    showConfirmPopover(button, "确认取消？", () => performCheckAction(button, "cancel"));
  });
  root.addEventListener("task-check-states", (event) => applyCheckStates(event.detail));
  root.addEventListener("model-output-start", (event) => {
    const codes = new Set(event.detail.codes);
    root.querySelectorAll("[data-detail-result]").forEach((node) => {
      if (codes.has(node.dataset.detailResult) && node.dataset.checkTerminal === "0") {
        node.querySelectorAll(".report-error").forEach((error) => error.remove());
        markup.delete(node);
      }
    });
  });
  setInterval(refresh, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
