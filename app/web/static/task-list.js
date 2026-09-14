(() => {
  if (!document.querySelector('[data-refresh-region="task-list"]')) return;
  const AUTO_REFRESH_KEY = "document-check:auto-refresh";
  const AUTO_REFRESH_INTERACTION_PAUSE_MS = 8000;
  let autoRefreshTimer = null;
  let autoRefreshPending = false;
  let autoRefreshSuspendedUntil = 0;
  let taskListPointerInside = false;
  let taskListFocusInside = false;

  function updateBulkTaskControls() {
    const checkboxes = Array.from(
      document.querySelectorAll("[data-bulk-task]"),
    );
    const selected = checkboxes.filter((checkbox) => checkbox.checked);
    const selectedCount = selected.length;
    const queuedCount = selected.filter(
      (checkbox) => checkbox.dataset.taskStatus === "queued",
    ).length;
    const toggle = document.querySelector("[data-bulk-task-toggle]");
    const form = document.querySelector("[data-bulk-delete-form]");
    const button = document.querySelector("[data-bulk-delete-button]");
    const count = document.querySelector("[data-bulk-delete-count]");

    if (toggle instanceof HTMLInputElement) {
      toggle.disabled = checkboxes.length === 0;
      toggle.checked =
        checkboxes.length > 0 && selectedCount === checkboxes.length;
      toggle.indeterminate =
        selectedCount > 0 && selectedCount < checkboxes.length;
    }
    if (button instanceof HTMLButtonElement) {
      button.disabled = selectedCount === 0;
    }
    if (count) {
      count.textContent = selectedCount ? ` (${selectedCount})` : "";
    }
    if (form instanceof HTMLFormElement) {
      const queuedMessage = queuedCount
        ? `其中 ${queuedCount} 个排队任务将先取消，`
        : "";
      form.dataset.confirm = `确认删除选中的 ${selectedCount} 个任务？${queuedMessage}删除后不可恢复。`;
    }
  }

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) {
      return;
    }
    if (target.matches("[data-bulk-task-toggle]")) {
      document.querySelectorAll("[data-bulk-task]").forEach((checkbox) => {
        checkbox.checked = target.checked;
      });
      updateBulkTaskControls();
      return;
    }
    if (target.matches("[data-bulk-task]")) {
      updateBulkTaskControls();
    }
  });

  function autoRefreshEnabled() {
    try {
      const saved = window.localStorage.getItem(AUTO_REFRESH_KEY);
      return saved === null || saved === "1";
    } catch {
      return true;
    }
  }

  function setAutoRefreshEnabled(enabled) {
    try {
      window.localStorage.setItem(AUTO_REFRESH_KEY, enabled ? "1" : "0");
    } catch {
      // Ignore storage errors; auto-refresh still works for the current page.
    }
  }

  function hasActiveRefreshTasks() {
    const stats = document.querySelector('[data-refresh-region="stats"]');
    return stats?.dataset.refreshActive === "1";
  }

  function suspendAutoRefresh(duration = AUTO_REFRESH_INTERACTION_PAUSE_MS) {
    autoRefreshSuspendedUntil = Math.max(
      autoRefreshSuspendedUntil,
      Date.now() + duration,
    );
  }

  function taskListInteractiveTarget(target) {
    if (!(target instanceof Element)) {
      return null;
    }
    return target.closest(
      '[data-refresh-region="task-list"] a, [data-refresh-region="task-list"] button, [data-refresh-region="task-list"] form, [data-refresh-region="task-list"] input, [data-refresh-region="task-list"] select',
    );
  }

  function autoRefreshSuspended() {
    return (
      Boolean(activeConfirmPopover) ||
      taskListPointerInside ||
      taskListFocusInside ||
      Date.now() < autoRefreshSuspendedUntil
    );
  }

  function updateRefreshToggle() {
    const toggle = document.querySelector("[data-auto-refresh-toggle]");
    if (!(toggle instanceof HTMLButtonElement)) {
      return;
    }
    const enabled = autoRefreshEnabled();
    const active = hasActiveRefreshTasks();
    const label = toggle.querySelector("[data-refresh-label]");
    toggle.classList.toggle("is-on", enabled && active);
    toggle.classList.toggle("is-off", !enabled || !active);
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    if (label) {
      label.textContent = enabled ? (active ? "刷新中" : "无活动") : "已暂停";
    }
  }

  const listMarkup = new WeakMap();
  document
    .querySelectorAll('[data-task-id], [data-refresh-region="task-list"]')
    .forEach((node) => listMarkup.set(node, node.innerHTML));
  let listRequestVersion = 0;
  let listRequestController = null;
  let lastTaskListFocusRefresh = 0;

  function listFeedback(text) {
    const target = document.querySelector("[data-task-list-feedback]");
    if (target) target.textContent = text;
  }

  function updateTaskFilterHint() {
    const form = document.querySelector("[data-task-filters]");
    const hint = form?.querySelector("[data-task-filter-hint]");
    if (!hint) return;
    const list = document.querySelector('[data-refresh-region="task-list"]');
    const applied = new URL(list.dataset.listUrl, window.location.href)
      .searchParams;
    const pending = ["status", "review_status", "keyword"].some(
      (name) =>
        form.elements.namedItem(name).value.trim() !== (applied.get(name) || ""),
    );
    hint.textContent = pending
      ? "条件已修改，点击“筛选”或按回车后更新列表。"
      : "点击“筛选”或按回车应用条件。";
    hint.classList.toggle("is-pending", pending);
  }

  function copyListAttributes(current, next) {
    for (const [key, value] of Object.entries(next.dataset))
      current.dataset[key] = value;
  }

  function replaceTaskList(current, next) {
    const html = next.innerHTML;
    if (listMarkup.get(current) === html) return;
    const selected = new Set(
      [...current.querySelectorAll("[data-bulk-task]:checked")].map(
        (input) => input.value,
      ),
    );
    const left = current.querySelector(".table-wrap")?.scrollLeft || 0;
    const oldTable = current.querySelector("table");
    const newTable = next.querySelector("table");
    if (oldTable && newTable) {
      const body = oldTable.tBodies[0];
      const oldRows = new Map(
        [...body.rows].map((row) => [row.dataset.taskId, row]),
      );
      const nextIds = new Set();
      [...newTable.tBodies[0].rows].forEach((row, index) => {
        const id = row.dataset.taskId;
        nextIds.add(id);
        let currentRow = oldRows.get(id);
        if (currentRow) {
          if (listMarkup.get(currentRow) !== row.innerHTML) {
            currentRow.innerHTML = row.innerHTML;
            listMarkup.set(currentRow, row.innerHTML);
          }
          copyListAttributes(currentRow, row);
        } else {
          currentRow = row;
          listMarkup.set(currentRow, row.innerHTML);
        }
        if (body.children[index] !== currentRow)
          body.insertBefore(currentRow, body.children[index] || null);
      });
      oldRows.forEach((row, id) => {
        if (!nextIds.has(id)) row.remove();
      });
      const pagination = current.querySelector(".pagination");
      const nextPagination = next.querySelector(".pagination");
      if (
        pagination &&
        nextPagination &&
        pagination.outerHTML !== nextPagination.outerHTML
      )
        pagination.replaceWith(nextPagination);
    } else {
      current.innerHTML = next.innerHTML;
      current
        .querySelectorAll("[data-task-id]")
        .forEach((row) => listMarkup.set(row, row.innerHTML));
    }
    // 缓存服务端内容，勾选状态和列宽由页面保留。
    listMarkup.set(current, html);
    current.querySelectorAll("[data-bulk-task]").forEach((input) => {
      input.checked = selected.has(input.value);
    });
    const wrap = current.querySelector(".table-wrap");
    if (wrap) wrap.scrollLeft = left;
    taskListFocusInside = Boolean(
      taskListInteractiveTarget(document.activeElement),
    );
    taskListPointerInside = Boolean(
      current.querySelector("a:hover, button:hover, input:hover, select:hover"),
    );
    updateBulkTaskControls();
  }

  function applyTaskListPage(
    nextDocument,
    { historyMode = "replace", syncFilters = false } = {},
  ) {
    const current = document.querySelector('[data-refresh-region="task-list"]');
    const next = nextDocument.querySelector(
      '[data-refresh-region="task-list"]',
    );
    const stats = document.querySelector('[data-refresh-region="stats"]');
    const nextStats = nextDocument.querySelector(
      '[data-refresh-region="stats"]',
    );
    if (!next || !nextStats)
      throw new Error("任务列表读取失败，请重试或重新登录。");
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    current.style.minHeight = "";
    if (stats && stats.innerHTML !== nextStats.innerHTML)
      stats.innerHTML = nextStats.innerHTML;
    if (stats) copyListAttributes(stats, nextStats);
    replaceTaskList(current, next);
    copyListAttributes(current, next);
    const url = new URL(next.dataset.listUrl, window.location.href);
    if (url.href !== window.location.href)
      history[historyMode === "push" ? "pushState" : "replaceState"](
        { taskList: true },
        "",
        url,
      );
    const filters = document.querySelector("[data-task-filters]");
    if (syncFilters && filters) {
      ["status", "review_status", "keyword"].forEach((name) => {
        filters.elements.namedItem(name).value =
          url.searchParams.get(name) || "";
      });
    }
    if (filters)
      filters.elements.namedItem("per_page").value =
        url.searchParams.get("per_page");
    updateTaskFilterHint();
    document
      .querySelectorAll('[data-task-list-panel] input[name="next"]')
      .forEach((input) => {
        input.value = url.pathname + url.search;
      });
    // 筛选结果变少时，为当前视口保留必要高度。
    const missingHeight =
      scrollY + window.innerHeight - document.documentElement.scrollHeight;
    if (missingHeight > 0)
      current.style.minHeight = `${current.offsetHeight + missingHeight}px`;
    window.scrollTo({ left: scrollX, top: scrollY, behavior: "instant" });
  }

  function requestStillCurrent(token, force) {
    return (
      token.version === listRequestVersion && (force || !autoRefreshSuspended())
    );
  }

  async function fetchTaskPage(url, token, options) {
    const target = new URL(url, window.location.href);
    target.searchParams.set("_partial", "1");
    const response = await fetch(target, {
      cache: "no-store",
      signal: token.signal,
      headers: { "X-Requested-With": "fetch" },
    });
    if (!response.ok) throw new Error("任务列表刷新失败，请重试。");
    const html = await response.text();
    if (requestStillCurrent(token, options.force))
      applyTaskListPage(
        new DOMParser().parseFromString(html, "text/html"),
        options,
      );
  }

  async function refreshTaskStatuses(refreshUrl, token, options) {
    const taskRows = [
      ...document.querySelectorAll("[data-task-id][data-task-status]"),
    ];
    const url = new URL(refreshUrl, window.location.href);
    url.searchParams.set(
      "ids",
      taskRows.map((row) => row.dataset.taskId).join(","),
    );
    const response = await fetch(url, {
      cache: "no-store",
      signal: token.signal,
      headers: { Accept: "application/json", "X-Requested-With": "fetch" },
    });
    if (
      !response.ok ||
      !response.headers.get("content-type")?.includes("application/json")
    )
      throw new Error("任务状态刷新失败，请重试。");
    const data = await response.json();
    if (!requestStillCurrent(token, options.force)) return;
    const stats = document.querySelector('[data-refresh-region="stats"]');
    const returned = new Map(
      (data.tasks || []).map((task) => [String(task.id), task]),
    );
    const changed =
      stats?.dataset.refreshActive !== (data.active ? "1" : "0") ||
      Object.entries(data.counts || {}).some(
        ([key, value]) =>
          stats?.querySelector(`[data-task-stat="${key}"]`)?.textContent !==
          String(value),
      ) ||
      returned.size !== taskRows.length ||
      taskRows.some(
        (row) =>
          returned.get(row.dataset.taskId)?.status !== row.dataset.taskStatus ||
          returned.get(row.dataset.taskId)?.review_key !==
            row.dataset.taskReviewKey,
      );
    if (changed) {
      await fetchTaskPage(window.location.href, token, options);
      return;
    }
    taskRows.forEach((row) => {
      const task = returned.get(row.dataset.taskId);
      const label = row.querySelector(".task-status-cell .status-pill");
      if (label) label.textContent = task.status_label;
      const progress = row.querySelector(".progress.mini span");
      if (progress) progress.style.width = `${task.progress}%`;
    });
  }

  async function runTaskListRefresh({
    target = null,
    full = false,
    force = false,
    historyMode = "replace",
    syncFilters = false,
  } = {}) {
    if (
      document.hidden ||
      (!force && (autoRefreshPending || autoRefreshSuspended()))
    )
      return;
    listRequestController?.abort();
    listRequestController = new AbortController();
    const token = {
      version: ++listRequestVersion,
      signal: AbortSignal.any([
        listRequestController.signal,
        AbortSignal.timeout(15000),
      ]),
    };
    autoRefreshPending = true;
    const button = document.querySelector("[data-manual-task-refresh]");
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    listFeedback(full || target ? "更新中…" : "");
    try {
      const options = { force, historyMode, syncFilters };
      const params = new URL(window.location.href).searchParams;
      if (
        target ||
        full ||
        ["status", "review_status", "keyword", "owner", "ip"].some((key) =>
          params.get(key),
        )
      ) {
        await fetchTaskPage(target || window.location.href, token, options);
      } else {
        const refreshUrl = document.querySelector(
          '[data-refresh-region="stats"]',
        )?.dataset.refreshUrl;
        await refreshTaskStatuses(refreshUrl, token, options);
      }
      if (token.version === listRequestVersion)
        listFeedback(full || target ? "已更新" : "");
    } catch (error) {
      if (token.version === listRequestVersion && error.name !== "AbortError")
        listFeedback(error.message || "刷新失败，请重试。");
    } finally {
      if (token.version === listRequestVersion) {
        autoRefreshPending = false;
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
        applyAutoRefreshState();
      }
    }
  }

  function refreshTaskRegions() {
    if (!hasActiveRefreshTasks()) {
      stopAutoRefresh();
      updateRefreshToggle();
      return;
    }
    return runTaskListRefresh();
  }

  function refreshTaskListAfterReturn() {
    if (
      document.hidden ||
      autoRefreshPending ||
      Date.now() - lastTaskListFocusRefresh < 1000
    )
      return;
    lastTaskListFocusRefresh = Date.now();
    return runTaskListRefresh({ full: true, force: true });
  }

  function navigateTaskList(url, syncFilters = false, historyMode = "push") {
    return runTaskListRefresh({
      target: url,
      full: true,
      force: true,
      historyMode,
      syncFilters,
    });
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (
      !form.matches(
        '[data-task-filters], [data-refresh-region="task-list"] .page-size-form, [data-refresh-region="task-list"] .page-jump-form',
      )
    )
      return;
    event.preventDefault();
    const url = new URL(
      form.action || window.location.href,
      window.location.href,
    );
    url.search = new URLSearchParams(new FormData(form)).toString();
    if (form.matches("[data-task-filters]")) url.searchParams.set("page", "1");
    navigateTaskList(url);
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-page-size-select]"))
      event.target.form.requestSubmit();
    if (event.target.closest("[data-task-filters]")) updateTaskFilterHint();
  });
  document.addEventListener("input", (event) => {
    if (event.target.closest("[data-task-filters]")) updateTaskFilterHint();
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-manual-task-refresh]")) {
      runTaskListRefresh({ full: true, force: true });
      return;
    }
    const link = event.target.closest(
      '[data-refresh-region="task-list"] .pagination a',
    );
    if (
      !link ||
      event.button !== 0 ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    if (!link.classList.contains("disabled")) navigateTaskList(link.href);
  });
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  window.addEventListener("popstate", () =>
    navigateTaskList(window.location.href, true, "replace"),
  );
  window.addEventListener("focus", refreshTaskListAfterReturn);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshTaskListAfterReturn();
  });

  function startAutoRefresh() {
    if (
      autoRefreshTimer ||
      !document.querySelector("[data-auto-refresh-toggle]") ||
      !hasActiveRefreshTasks()
    ) {
      return;
    }
    autoRefreshTimer = window.setInterval(refreshTaskRegions, 10000);
  }

  function stopAutoRefresh() {
    if (!autoRefreshTimer) {
      return;
    }
    window.clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }

  function applyAutoRefreshState() {
    updateRefreshToggle();
    if (autoRefreshEnabled() && hasActiveRefreshTasks()) {
      startAutoRefresh();
    } else {
      stopAutoRefresh();
    }
  }

  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-auto-refresh-toggle]");
    if (!toggle) {
      return;
    }
    setAutoRefreshEnabled(!autoRefreshEnabled());
    applyAutoRefreshState();
  });

  document.addEventListener("pointerover", (event) => {
    if (!taskListInteractiveTarget(event.target)) {
      return;
    }
    taskListPointerInside = true;
    suspendAutoRefresh();
  });

  document.addEventListener("pointerout", (event) => {
    if (
      !taskListPointerInside ||
      taskListInteractiveTarget(event.relatedTarget)
    ) {
      return;
    }
    taskListPointerInside = false;
    suspendAutoRefresh(2000);
  });

  document.addEventListener("pointerdown", (event) => {
    if (taskListInteractiveTarget(event.target)) {
      suspendAutoRefresh();
    }
  });

  document.addEventListener("focusin", (event) => {
    if (!taskListInteractiveTarget(event.target)) {
      return;
    }
    taskListFocusInside = true;
    suspendAutoRefresh();
  });

  document.addEventListener("focusout", () => {
    window.setTimeout(() => {
      taskListFocusInside = Boolean(
        taskListInteractiveTarget(document.activeElement),
      );
      if (!taskListFocusInside) {
        suspendAutoRefresh(2000);
      }
    });
  });

  updateBulkTaskControls();
  updateTaskFilterHint();
  applyAutoRefreshState();
})();
