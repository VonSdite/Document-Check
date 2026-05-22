let activeConfirmPopover = null;

function closeConfirmPopover() {
  if (!activeConfirmPopover) {
    return;
  }
  activeConfirmPopover.remove();
  activeConfirmPopover = null;
}

function placeConfirmPopover(popover, anchor) {
  const rect = anchor.getBoundingClientRect();
  const margin = 12;
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  let left = rect.right - width;
  let top = rect.bottom + 8;

  if (left < margin) {
    left = margin;
  }
  if (left + width > window.innerWidth - margin) {
    left = window.innerWidth - width - margin;
  }
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8;
  }
  if (top < margin) {
    top = margin;
  }

  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

function showConfirmPopover(anchor, message, onConfirm) {
  closeConfirmPopover();

  const popover = document.createElement("div");
  popover.className = "confirm-popover";
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-live", "polite");
  popover.innerHTML = `
    <div class="confirm-popover-title">确认操作</div>
    <div class="confirm-popover-message"></div>
    <div class="confirm-popover-actions">
      <button class="small-button" type="button" data-confirm-cancel>取消</button>
      <button class="small-button danger" type="button" data-confirm-ok>确认</button>
    </div>
  `;
  popover.querySelector(".confirm-popover-message").textContent = message;
  document.body.appendChild(popover);
  activeConfirmPopover = popover;
  placeConfirmPopover(popover, anchor);

  window.setTimeout(() => {
    const okButton = popover.querySelector("[data-confirm-ok]");
    okButton?.focus();
  });

  popover.addEventListener("click", (event) => {
    if (event.target.closest("[data-confirm-cancel]")) {
      closeConfirmPopover();
      return;
    }
    if (event.target.closest("[data-confirm-ok]")) {
      closeConfirmPopover();
      onConfirm();
    }
  });
}

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const message = form.dataset.confirm;
  if (!message || form.dataset.confirmed === "true") {
    delete form.dataset.confirmed;
    return;
  }

  event.preventDefault();
  const submitter = event.submitter;
  const anchor = submitter instanceof HTMLElement ? submitter : form;
  showConfirmPopover(anchor, message, () => {
    form.dataset.confirmed = "true";
    if (submitter instanceof HTMLElement && typeof form.requestSubmit === "function") {
      form.requestSubmit(submitter);
    } else {
      form.submit();
    }
  });
});

function hideToast(toast) {
  toast.classList.add("is-hiding");
  window.setTimeout(() => toast.remove(), 220);
}

function showToast(message, category = "success") {
  let stack = document.querySelector(".flash-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "flash-stack";
    stack.setAttribute("aria-live", "polite");
    stack.setAttribute("aria-atomic", "true");
    document.body.appendChild(stack);
  }

  const toast = document.createElement("button");
  toast.type = "button";
  toast.className = `flash ${category}`;
  toast.textContent = message;
  stack.appendChild(toast);
  window.setTimeout(() => hideToast(toast), 4200);
}

document.querySelectorAll(".flash").forEach((toast) => {
  window.setTimeout(() => hideToast(toast), 4200);
});

document.addEventListener("click", (event) => {
  const toast = event.target.closest(".flash");
  if (!toast) {
    return;
  }
  hideToast(toast);
});

document.addEventListener("change", (event) => {
  const field = event.target.closest("[data-auto-save-setting]");
  if (!(field instanceof HTMLInputElement)) {
    return;
  }
  const form = field.form;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }

  const savedChecked = field.dataset.savedChecked === "true";
  const targetChecked = field.checked;
  const formData = new FormData(form);
  const actionUrl = form.getAttribute("action") || window.location.href;
  const method = form.getAttribute("method") || "POST";
  formData.set(field.name, targetChecked ? "on" : "off");
  field.disabled = true;
  fetch(actionUrl, {
    method,
    body: formData,
    credentials: "same-origin",
    headers: {
      "Accept": "application/json",
      "X-Requested-With": "fetch",
    },
  })
    .then((response) => {
      if (!response.ok) {
        throw new Error("save failed");
      }
      return response.text().then((text) => ({ response, text }));
    })
    .then(({ response, text }) => {
      const contentType = response.headers.get("content-type") || "";
      const data = text && contentType.includes("application/json") ? JSON.parse(text) : null;
      if (response.redirected && response.url.includes("/login")) {
        throw new Error("login expired");
      }
      if (text && !data) {
        console.warn("自动保存返回了非 JSON 内容，已按本次开关状态显示。", {
          status: response.status,
          url: response.url,
          contentType,
          bodyPreview: text.slice(0, 300),
        });
      }
      const saved = data?.llm_stream_trace_enabled ?? targetChecked;
      field.checked = saved;
      field.dataset.savedChecked = saved ? "true" : "false";
      showToast("定位日志设置已保存。", "success");
    })
    .catch(() => {
      field.checked = savedChecked;
      showToast("定位日志设置保存失败，请稍后重试。", "error");
    })
    .finally(() => {
      field.disabled = false;
    });
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-confirm-click]");
  if (!target) {
    if (activeConfirmPopover && !event.target.closest(".confirm-popover")) {
      closeConfirmPopover();
    }
    return;
  }
  const message = target.dataset.confirmClick;
  if (!message) {
    return;
  }

  event.preventDefault();
  showConfirmPopover(target, message, () => {
    const form = target.form;
    if (form instanceof HTMLFormElement && typeof form.requestSubmit === "function") {
      form.requestSubmit(target);
      return;
    }
    target.click();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeConfirmPopover();
    closeHelpTip();
  }
});

window.addEventListener("resize", closeConfirmPopover);
window.addEventListener("scroll", closeConfirmPopover, true);

let activeHelpTip = null;
let activeHelpTipAnchor = null;

function closestHelpTip(target) {
  if (!(target instanceof Element)) {
    return null;
  }
  return target.closest(".help-tip[data-tip]");
}

function closeHelpTip() {
  if (activeHelpTip) {
    activeHelpTip.remove();
  }
  if (activeHelpTipAnchor) {
    activeHelpTipAnchor.classList.remove("is-floating-tip");
  }
  activeHelpTip = null;
  activeHelpTipAnchor = null;
}

function placeHelpTip(tooltip, anchor) {
  const rect = anchor.getBoundingClientRect();
  const margin = 12;
  const width = tooltip.offsetWidth;
  const height = tooltip.offsetHeight;
  let left = rect.left + rect.width / 2 - width / 2;
  let top = rect.top - height - 10;
  let placement = "top";

  if (left < margin) {
    left = margin;
  }
  if (left + width > window.innerWidth - margin) {
    left = window.innerWidth - width - margin;
  }
  if (top < margin) {
    top = rect.bottom + 10;
    placement = "bottom";
  }
  if (top + height > window.innerHeight - margin) {
    top = window.innerHeight - height - margin;
  }

  const arrowLeft = Math.min(width - 12, Math.max(12, rect.left + rect.width / 2 - left));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(margin, top)}px`;
  tooltip.style.setProperty("--tip-arrow-left", `${arrowLeft}px`);
  tooltip.dataset.placement = placement;
}

function showHelpTip(anchor) {
  const message = anchor.dataset.tip?.trim();
  if (!message) {
    return;
  }
  if (activeHelpTipAnchor === anchor) {
    return;
  }

  closeHelpTip();

  const tooltip = document.createElement("div");
  tooltip.className = "floating-help-tip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.textContent = message;

  const tooltipRoot = anchor.closest("dialog[open]") || document.body;
  tooltipRoot.appendChild(tooltip);
  activeHelpTip = tooltip;
  activeHelpTipAnchor = anchor;
  anchor.classList.add("is-floating-tip");
  placeHelpTip(tooltip, anchor);
  window.requestAnimationFrame(() => tooltip.classList.add("is-visible"));
}

document.addEventListener("pointerover", (event) => {
  const anchor = closestHelpTip(event.target);
  if (!anchor) {
    return;
  }
  showHelpTip(anchor);
});

document.addEventListener("pointerout", (event) => {
  const anchor = closestHelpTip(event.target);
  if (!anchor || anchor !== activeHelpTipAnchor) {
    return;
  }
  if (event.relatedTarget instanceof Node && anchor.contains(event.relatedTarget)) {
    return;
  }
  closeHelpTip();
});

document.addEventListener("focusin", (event) => {
  const anchor = closestHelpTip(event.target);
  if (anchor) {
    showHelpTip(anchor);
  }
});

document.addEventListener("focusout", (event) => {
  if (event.target === activeHelpTipAnchor) {
    closeHelpTip();
  }
});

window.addEventListener("resize", closeHelpTip);
window.addEventListener("scroll", closeHelpTip, true);

let checkItemDragArmed = false;
let draggedCheckItemRow = null;
let suppressCheckItemClick = false;

function checkItemDetailFor(row) {
  return document.querySelector(`[data-check-item-detail="${row.dataset.checkItemId}"]`);
}

function setCheckItemOpen(row, open) {
  const detail = checkItemDetailFor(row);
  const toggle = row.querySelector("[data-check-item-toggle]");
  row.classList.toggle("is-expanded", open);
  row.setAttribute("aria-expanded", open ? "true" : "false");
  if (toggle) {
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (detail) {
    detail.hidden = !open;
  }
}

function moveCheckItemPair(row, reference) {
  const tbody = row.parentElement;
  const detail = checkItemDetailFor(row);
  if (!tbody || reference === row || reference === detail) {
    return;
  }
  const fragment = document.createDocumentFragment();
  fragment.appendChild(row);
  if (detail) {
    fragment.appendChild(detail);
  }
  tbody.insertBefore(fragment, reference || null);
}

function saveCheckItemOrder(table) {
  const formData = new FormData();
  formData.append("action", "reorder_check_items");
  table.querySelectorAll("[data-check-item-row]").forEach((row) => {
    formData.append("item_ids", row.dataset.checkItemId);
  });
  return fetch(window.location.href, {
    method: "POST",
    body: formData,
    headers: { "X-Requested-With": "fetch" },
  });
}

document.addEventListener("click", (event) => {
  if (suppressCheckItemClick) {
    suppressCheckItemClick = false;
    return;
  }
  const row = event.target.closest("[data-check-item-row]");
  if (!row || event.target.closest("[data-check-item-drag], .check-item-delete-button")) {
    return;
  }
  setCheckItemOpen(row, !row.classList.contains("is-expanded"));
});

document.addEventListener("pointerdown", (event) => {
  checkItemDragArmed = Boolean(event.target.closest("[data-check-item-drag]"));
});

document.addEventListener("pointerup", () => {
  checkItemDragArmed = false;
});

document.addEventListener("dragstart", (event) => {
  const row = event.target.closest("[data-check-item-row]");
  if (!row || !checkItemDragArmed) {
    event.preventDefault();
    return;
  }
  draggedCheckItemRow = row;
  row.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", row.dataset.checkItemId);
});

document.addEventListener("dragover", (event) => {
  if (!draggedCheckItemRow) {
    return;
  }
  const targetRow = event.target.closest("[data-check-item-row]");
  if (!targetRow || targetRow === draggedCheckItemRow) {
    return;
  }
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
});

document.addEventListener("drop", (event) => {
  if (!draggedCheckItemRow) {
    return;
  }
  const targetRow = event.target.closest("[data-check-item-row]");
  const table = draggedCheckItemRow.closest("[data-check-item-table]");
  if (!targetRow || targetRow === draggedCheckItemRow || !table) {
    return;
  }
  event.preventDefault();
  suppressCheckItemClick = true;
  window.setTimeout(() => {
    suppressCheckItemClick = false;
  }, 120);

  const targetDetail = checkItemDetailFor(targetRow);
  const targetRect = targetRow.getBoundingClientRect();
  const reference = event.clientY > targetRect.top + targetRect.height / 2
    ? targetDetail?.nextElementSibling
    : targetRow;
  moveCheckItemPair(draggedCheckItemRow, reference);
  saveCheckItemOrder(table)
    .then((response) => {
      if (!response.ok) {
        window.location.reload();
      }
    })
    .catch(() => window.location.reload());
});

document.addEventListener("dragend", () => {
  if (draggedCheckItemRow) {
    draggedCheckItemRow.classList.remove("is-dragging");
  }
  draggedCheckItemRow = null;
  checkItemDragArmed = false;
});

function createPasswordToggleButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "password-toggle";
  button.dataset.passwordToggle = "true";
  button.setAttribute("aria-label", "显示密码");
  button.setAttribute("aria-pressed", "false");
  button.title = "显示密码";
  button.innerHTML = `
    <svg class="password-icon password-icon-eye" aria-hidden="true" focusable="false" viewBox="0 0 24 24">
      <path d="M2.1 12s3.6-7 9.9-7 9.9 7 9.9 7-3.6 7-9.9 7-9.9-7-9.9-7Z"></path>
      <circle cx="12" cy="12" r="3"></circle>
    </svg>
    <svg class="password-icon password-icon-off" aria-hidden="true" focusable="false" viewBox="0 0 24 24">
      <path d="M10.7 5.1A10.9 10.9 0 0 1 12 5c6.3 0 9.9 7 9.9 7a18 18 0 0 1-2.7 3.6"></path>
      <path d="M6.6 6.6C3.8 8.5 2.1 12 2.1 12s3.6 7 9.9 7a10 10 0 0 0 5.4-1.6"></path>
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"></path>
      <path d="M3 3l18 18"></path>
    </svg>
  `;
  return button;
}

function enhancePasswordInput(input) {
  if (!(input instanceof HTMLInputElement) || input.dataset.passwordEnhanced === "true") {
    return;
  }
  input.dataset.passwordEnhanced = "true";

  const wrapper = document.createElement("div");
  wrapper.className = "password-field";
  input.before(wrapper);
  wrapper.appendChild(input);
  wrapper.appendChild(createPasswordToggleButton());
}

document.querySelectorAll('input[type="password"]').forEach(enhancePasswordInput);

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-password-toggle]");
  if (!(toggle instanceof HTMLButtonElement)) {
    return;
  }

  event.preventDefault();
  const wrapper = toggle.closest(".password-field");
  const input = wrapper?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) {
    return;
  }

  const showPassword = input.type === "password";
  input.type = showPassword ? "text" : "password";
  wrapper.classList.toggle("is-visible", showPassword);
  toggle.setAttribute("aria-pressed", showPassword ? "true" : "false");
  const label = showPassword ? "隐藏密码" : "显示密码";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
});

function openModal(id) {
  const modal = document.getElementById(id);
  if (!(modal instanceof HTMLDialogElement)) {
    return;
  }
  closeHelpTip();
  if (typeof modal.showModal === "function") {
    modal.showModal();
  } else {
    modal.setAttribute("open", "");
  }
  const firstField = modal.querySelector("input:not([type='hidden']), select, textarea, button");
  window.setTimeout(() => firstField?.focus());
}

function closeModal(target) {
  const modal = target.closest("dialog");
  if (!(modal instanceof HTMLDialogElement)) {
    return;
  }
  closeHelpTip();
  if (typeof modal.close === "function") {
    modal.close();
  } else {
    modal.removeAttribute("open");
  }
}

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-modal-open]");
  if (opener) {
    event.preventDefault();
    openModal(opener.dataset.modalOpen);
    return;
  }

  const closer = event.target.closest("[data-modal-close]");
  if (closer) {
    event.preventDefault();
    closeModal(closer);
    return;
  }
});

let activeFetchModelForm = null;
let fetchedModelCandidates = [];
let fetchedModelSelection = new Set();
let fetchedModelExistingSelection = new Set();

function modelConfigKey(modelName, forceDisableThinking) {
  return `${forceDisableThinking ? "1" : "0"}:${modelName}`;
}

function normalizeModelConfigs(value) {
  const source = Array.isArray(value) ? value : [];
  const models = [];
  const seen = new Set();
  source.forEach((item) => {
    const modelName = String(item?.model_name || item?.id || item || "").trim();
    const forceDisableThinking = Boolean(item?.force_disable_thinking);
    const key = modelConfigKey(modelName, forceDisableThinking);
    if (!modelName || seen.has(key)) {
      return;
    }
    seen.add(key);
    models.push({
      model_name: modelName,
      force_disable_thinking: forceDisableThinking,
    });
  });
  return models;
}

function parseModelConfigs(form) {
  const hidden = form.querySelector("[data-model-configs]");
  if (!(hidden instanceof HTMLInputElement) || !hidden.value) {
    return [];
  }
  try {
    return normalizeModelConfigs(JSON.parse(hidden.value));
  } catch {
    return [];
  }
}

function collectModelConfigs(form) {
  return normalizeModelConfigs(
    Array.from(form.querySelectorAll("[data-model-row]")).map((row) => {
      const input = row.querySelector("[data-model-name]");
      const checkbox = row.querySelector("[data-model-thinking]");
      return {
        model_name: input instanceof HTMLInputElement ? input.value : "",
        force_disable_thinking: checkbox instanceof HTMLInputElement && checkbox.checked,
      };
    }),
  );
}

function writeModelConfigs(form, configs = collectModelConfigs(form)) {
  const normalized = normalizeModelConfigs(configs);
  const hidden = form.querySelector("[data-model-configs]");
  const fallback = form.querySelector("[data-model-list-fallback]");
  if (hidden instanceof HTMLInputElement) {
    hidden.value = JSON.stringify(normalized);
  }
  if (fallback instanceof HTMLTextAreaElement) {
    fallback.value = normalized.map((item) => item.model_name).join("\n");
  }
  updateModelSummary(form, normalized.length);
  return normalized;
}

function updateModelSummary(form, count = collectModelConfigs(form).length) {
  const summary = form.querySelector("[data-model-summary]");
  if (summary) {
    summary.textContent = count ? `共 ${count} 个模型` : "暂无模型";
  }
}

function appendModelAddRow(body, { empty = false } = {}) {
  const row = document.createElement("tr");
  row.className = `model-editor-add-row${empty ? " is-empty" : ""}`;
  const cell = document.createElement("td");
  cell.colSpan = 3;
  const button = document.createElement("button");
  button.className = "model-editor-add-button";
  button.type = "button";
  button.dataset.modelRowAdd = "1";
  button.textContent = "+ 新增模型";
  if (empty) {
    const emptyState = document.createElement("div");
    emptyState.className = "model-editor-empty-state";
    const text = document.createElement("span");
    text.textContent = "暂无模型";
    emptyState.append(text, button);
    cell.appendChild(emptyState);
  } else {
    cell.appendChild(button);
  }
  row.appendChild(cell);
  body.appendChild(row);
}

function renderModelRows(form, configs) {
  const body = form.querySelector("[data-model-rows]");
  if (!body) {
    return;
  }
  body.replaceChildren();
  const rows = (Array.isArray(configs) ? configs : []).map((item) => ({
    model_name: String(item?.model_name || item?.id || item || "").trim(),
    force_disable_thinking: Boolean(item?.force_disable_thinking),
  }));

  if (!rows.length) {
    appendModelAddRow(body, { empty: true });
    writeModelConfigs(form, rows);
    return;
  }

  rows.forEach((model) => {
    const row = document.createElement("tr");
    row.dataset.modelRow = "1";

    const nameCell = document.createElement("td");
    const nameInput = document.createElement("input");
    nameInput.className = "model-name-input";
    nameInput.type = "text";
    nameInput.dataset.modelName = "1";
    nameInput.value = model.model_name;
    nameInput.placeholder = "请输入模型名称";
    nameCell.appendChild(nameInput);

    const thinkingCell = document.createElement("td");
    const thinkingLabel = document.createElement("label");
    thinkingLabel.className = "model-thinking-toggle";
    const thinkingCheckbox = document.createElement("input");
    thinkingCheckbox.type = "checkbox";
    thinkingCheckbox.dataset.modelThinking = "1";
    thinkingCheckbox.checked = model.force_disable_thinking;
    const thinkingText = document.createElement("span");
    thinkingText.textContent = "关闭";
    thinkingLabel.append(thinkingCheckbox, thinkingText);
    thinkingCell.appendChild(thinkingLabel);

    const actionCell = document.createElement("td");
    actionCell.className = "right";
    const deleteButton = document.createElement("button");
    deleteButton.className = "small-button danger";
    deleteButton.type = "button";
    deleteButton.dataset.modelRowDelete = "1";
    deleteButton.textContent = "删除";
    actionCell.appendChild(deleteButton);

    row.append(nameCell, thinkingCell, actionCell);
    body.appendChild(row);
  });
  appendModelAddRow(body);
  writeModelConfigs(form, rows);
}

document.querySelectorAll(".provider-modal-form").forEach((form) => {
  renderModelRows(form, parseModelConfigs(form));
});

function addModelRow(form) {
  renderModelRows(form, [...collectModelConfigs(form), { model_name: "", force_disable_thinking: false }]);
  const inputs = form.querySelectorAll("[data-model-name]");
  const lastInput = inputs[inputs.length - 1];
  window.setTimeout(() => lastInput?.focus());
}

function tidyModelRows(form) {
  const configs = collectModelConfigs(form).sort((left, right) => {
    const nameOrder = left.model_name.localeCompare(right.model_name);
    if (nameOrder !== 0) {
      return nameOrder;
    }
    return Number(left.force_disable_thinking) - Number(right.force_disable_thinking);
  });
  renderModelRows(form, configs);
  showToast(configs.length ? `模型列表已整理，保留 ${configs.length} 个模型。` : "当前没有可整理的模型。", "success");
}

function setFetchButtonLoading(button, loading) {
  button.disabled = loading;
  button.textContent = loading ? "拉取中..." : "拉取模型";
}

async function fetchModelsForForm(form, button) {
  writeModelConfigs(form);
  const apiBase = form.elements.api_base?.value?.trim() || "";
  const apiKey = form.elements.api_key?.value?.trim() || "";
  const proxyMode = form.elements.proxy_mode?.value || "direct";
  const proxy = form.elements.proxy?.value?.trim() || "";
  const requestTimeout = form.elements.request_timeout?.value || "30";
  const sslVerify = form.elements.ssl_verify?.checked ? "on" : "off";
  if (!apiBase) {
    showToast("请先填写 API 地址。", "error");
    return;
  }
  if (proxyMode === "custom" && !proxy) {
    showToast("自定义代理模式需要填写代理地址。", "error");
    return;
  }

  const params = new URLSearchParams({
    api_base: apiBase,
    proxy_mode: proxyMode,
    request_timeout: requestTimeout,
    ssl_verify: sslVerify,
  });
  if (apiKey) {
    params.set("api_key", apiKey);
  }
  if (proxy) {
    params.set("proxy", proxy);
  }

  setFetchButtonLoading(button, true);
  try {
    const response = await fetch(`${form.dataset.fetchModelsUrl}?${params.toString()}`, {
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "X-Requested-With": "fetch",
      },
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "拉取模型失败");
    }
    openFetchModelPicker(form, result.fetched_models || []);
    showToast(`已获取到 ${(result.fetched_models || []).length} 个模型。`, "success");
  } catch (error) {
    showToast(error?.message || "拉取模型失败。", "error");
  } finally {
    setFetchButtonLoading(button, false);
  }
}

function filteredFetchedModels() {
  const query = document.querySelector("[data-fetch-model-search]")?.value?.trim().toLowerCase() || "";
  if (!query) {
    return fetchedModelCandidates;
  }
  return fetchedModelCandidates.filter((model) => model.toLowerCase().includes(query));
}

function syncFetchedSelectAll(filtered = filteredFetchedModels()) {
  const checkbox = document.querySelector("[data-fetch-model-select-all]");
  if (!(checkbox instanceof HTMLInputElement)) {
    return;
  }
  const selectedCount = filtered.filter((model) => fetchedModelSelection.has(model)).length;
  checkbox.disabled = filtered.length === 0;
  checkbox.checked = filtered.length > 0 && selectedCount === filtered.length;
  checkbox.indeterminate = selectedCount > 0 && selectedCount < filtered.length;
}

function renderFetchModelPicker() {
  const list = document.querySelector("[data-fetch-model-list]");
  const summary = document.querySelector("[data-fetch-model-summary]");
  if (!list || !summary) {
    return;
  }
  const filtered = filteredFetchedModels();
  summary.textContent = `共拉取 ${fetchedModelCandidates.length} 个模型，当前显示 ${filtered.length} 个，已选 ${fetchedModelSelection.size} 个`;
  syncFetchedSelectAll(filtered);
  list.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "model-editor-empty";
    empty.textContent = "没有匹配的模型";
    list.appendChild(empty);
    return;
  }
  filtered.forEach((model) => {
    const label = document.createElement("label");
    label.className = `fetch-model-item${fetchedModelSelection.has(model) ? " is-selected" : ""}`;
    const main = document.createElement("span");
    main.className = "fetch-model-item-main";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.fetchModelItem = model;
    checkbox.checked = fetchedModelSelection.has(model);
    const name = document.createElement("span");
    name.className = "fetch-model-name";
    name.textContent = model;
    main.append(checkbox, name);
    label.appendChild(main);
    if (fetchedModelExistingSelection.has(model)) {
      const badge = document.createElement("span");
      badge.className = "fetch-model-badge";
      badge.textContent = "已在清单";
      label.appendChild(badge);
    } else {
      const spacer = document.createElement("span");
      spacer.className = "fetch-model-badge-placeholder";
      label.appendChild(spacer);
    }
    list.appendChild(label);
  });
}

function openFetchModelPicker(form, models) {
  const currentModels = collectModelConfigs(form);
  const currentDefaultNames = new Set(
    currentModels.filter((item) => !item.force_disable_thinking).map((item) => item.model_name),
  );
  fetchedModelCandidates = normalizeModelConfigs(models).map((item) => item.model_name);
  fetchedModelExistingSelection = new Set(fetchedModelCandidates.filter((model) => currentDefaultNames.has(model)));
  fetchedModelSelection = new Set(fetchedModelExistingSelection);
  activeFetchModelForm = form;
  const search = document.querySelector("[data-fetch-model-search]");
  if (search instanceof HTMLInputElement) {
    search.value = "";
  }
  renderFetchModelPicker();
  openModal("fetch-model-modal");
}

function applyFetchedModelsSelection() {
  if (!activeFetchModelForm) {
    return;
  }
  const currentModels = collectModelConfigs(activeFetchModelForm);
  const currentDefaultByName = new Map(
    currentModels.filter((item) => !item.force_disable_thinking).map((item) => [item.model_name, item]),
  );
  const fetchedSet = new Set(fetchedModelCandidates);
  const nextModels = [];
  const seen = new Set();

  currentModels.forEach((model) => {
    if (
      !model.force_disable_thinking &&
      fetchedSet.has(model.model_name) &&
      !fetchedModelSelection.has(model.model_name)
    ) {
      return;
    }
    seen.add(modelConfigKey(model.model_name, model.force_disable_thinking));
    nextModels.push(model);
  });

  fetchedModelCandidates.forEach((modelName) => {
    const key = modelConfigKey(modelName, false);
    if (!fetchedModelSelection.has(modelName) || seen.has(key)) {
      return;
    }
    seen.add(key);
    nextModels.push(currentDefaultByName.get(modelName) || { model_name: modelName, force_disable_thinking: false });
  });

  renderModelRows(activeFetchModelForm, nextModels);
  const modal = document.getElementById("fetch-model-modal");
  if (modal instanceof HTMLDialogElement && typeof modal.close === "function") {
    modal.close();
  } else {
    modal?.removeAttribute("open");
  }
  showToast("模型列表已更新。", "success");
}

document.addEventListener("click", (event) => {
  const addButton = event.target.closest("[data-model-row-add]");
  if (addButton) {
    event.preventDefault();
    const form = addButton.closest(".provider-modal-form");
    if (form) {
      addModelRow(form);
    }
    return;
  }

  const tidyButton = event.target.closest("[data-model-list-tidy]");
  if (tidyButton) {
    event.preventDefault();
    const form = tidyButton.closest(".provider-modal-form");
    if (form) {
      tidyModelRows(form);
    }
    return;
  }

  const deleteButton = event.target.closest("[data-model-row-delete]");
  if (deleteButton) {
    event.preventDefault();
    const form = deleteButton.closest(".provider-modal-form");
    const row = deleteButton.closest("[data-model-row]");
    row?.remove();
    if (form) {
      writeModelConfigs(form);
      if (!form.querySelector("[data-model-row]")) {
        renderModelRows(form, []);
      }
    }
    return;
  }

  const fetchButton = event.target.closest("[data-fetch-models]");
  if (fetchButton) {
    event.preventDefault();
    const form = fetchButton.closest(".provider-modal-form");
    if (form) {
      fetchModelsForForm(form, fetchButton);
    }
    return;
  }

  const applyButton = event.target.closest("[data-fetch-model-apply]");
  if (applyButton) {
    event.preventDefault();
    applyFetchedModelsSelection();
  }
});

document.addEventListener("input", (event) => {
  const input = event.target;
  const form = input.closest?.(".provider-modal-form");
  if (form && input.matches?.("[data-model-name]")) {
    writeModelConfigs(form);
    return;
  }
  if (input.matches?.("[data-fetch-model-search]")) {
    renderFetchModelPicker();
  }
});

document.addEventListener("change", (event) => {
  const input = event.target;
  const form = input.closest?.(".provider-modal-form");
  if (form && input.matches?.("[data-model-thinking]")) {
    writeModelConfigs(form);
    return;
  }
  if (input.matches?.("[data-fetch-model-item]")) {
    const modelName = input.dataset.fetchModelItem;
    if (input.checked) {
      fetchedModelSelection.add(modelName);
    } else {
      fetchedModelSelection.delete(modelName);
    }
    renderFetchModelPicker();
    return;
  }
  if (input.matches?.("[data-fetch-model-select-all]")) {
    filteredFetchedModels().forEach((modelName) => {
      if (input.checked) {
        fetchedModelSelection.add(modelName);
      } else {
        fetchedModelSelection.delete(modelName);
      }
    });
    renderFetchModelPicker();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.classList.contains("provider-modal-form")) {
    writeModelConfigs(form);
  }
});

const LAST_MODEL_KEY = "document-check:last-model-id";

function readLastModelId() {
  try {
    return window.localStorage.getItem(LAST_MODEL_KEY) || "";
  } catch {
    return "";
  }
}

function writeLastModelId(value) {
  try {
    window.localStorage.setItem(LAST_MODEL_KEY, value);
  } catch {
    // Ignore storage errors; the form still works without browser persistence.
  }
}

function selectContainsValue(select, value) {
  return Array.from(select.options).some((option) => option.value === value);
}

document.querySelectorAll('select[name="model_id"]').forEach((select) => {
  if (!(select instanceof HTMLSelectElement)) {
    return;
  }
  const lastModelId = readLastModelId();
  if (lastModelId && selectContainsValue(select, lastModelId)) {
    select.value = lastModelId;
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const select = form.querySelector('select[name="model_id"]');
  if (select instanceof HTMLSelectElement && select.value) {
    writeLastModelId(select.value);
  }
});

function clearFileControl(target) {
  const control = target.closest(".file-upload-control");
  if (!control) {
    return;
  }
  const input = control.querySelector(".file-input");
  const name = control.querySelector(".file-name");
  if (input instanceof HTMLInputElement) {
    input.value = "";
  }
  if (name) {
    name.textContent = "未选择文件";
    name.removeAttribute("title");
  }
  control.classList.remove("has-file");
  if (input instanceof HTMLInputElement) {
    renderSelectedFileList(control, input);
  }
}

function fileListFor(control) {
  return control.closest(".multi-file-field")?.querySelector("[data-file-list]");
}

function fileLimitFor(input) {
  const limit = Number(input.dataset.fileLimit || "0");
  return Number.isInteger(limit) && limit > 0 ? limit : 0;
}

function setInputFiles(input, files) {
  if (typeof DataTransfer === "undefined") {
    return false;
  }
  const transfer = new DataTransfer();
  files.forEach((file) => transfer.items.add(file));
  input.files = transfer.files;
  return true;
}

function sameFile(left, right) {
  return left.name === right.name && left.size === right.size && left.lastModified === right.lastModified;
}

function mergeFiles(existingFiles, selectedFiles) {
  const merged = [...existingFiles];
  selectedFiles.forEach((file) => {
    if (!merged.some((item) => sameFile(item, file))) {
      merged.push(file);
    }
  });
  return merged;
}

function trimFilesToLimit(input) {
  const limit = fileLimitFor(input);
  const files = Array.from(input.files || []);
  if (!limit || files.length <= limit) {
    return 0;
  }
  if (!setInputFiles(input, files.slice(0, limit))) {
    return 0;
  }
  return files.length - limit;
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes)) {
    return "";
  }
  if (bytes < 1024 * 1024) {
    return `${Math.max(0.1, bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function renderSelectedFileList(control, input, trimmedCount = 0) {
  const list = fileListFor(control);
  if (!list) {
    return;
  }
  const files = Array.from(input.files || []);
  list.replaceChildren();
  list.hidden = files.length === 0;
  if (!files.length) {
    return;
  }

  const limit = fileLimitFor(input);
  const summary = document.createElement("div");
  summary.className = "selected-file-summary";
  const summaryText = document.createElement("span");
  summaryText.textContent = limit ? `已选择 ${files.length}/${limit} 个文件` : `已选择 ${files.length} 个文件`;
  summary.appendChild(summaryText);
  if (!limit || files.length < limit) {
    const addButton = document.createElement("button");
    addButton.className = "selected-file-add";
    addButton.type = "button";
    addButton.dataset.fileAdd = "1";
    addButton.textContent = "继续添加";
    summary.appendChild(addButton);
  }
  list.appendChild(summary);

  const items = document.createElement("ul");
  items.className = "selected-file-items";
  files.forEach((file, index) => {
    const item = document.createElement("li");
    item.className = "selected-file-item";

    const order = document.createElement("span");
    order.className = "selected-file-index";
    order.textContent = String(index + 1);

    const name = document.createElement("span");
    name.className = "selected-file-name";
    name.textContent = file.name;
    name.title = file.name;

    const size = document.createElement("span");
    size.className = "selected-file-size";
    size.textContent = formatFileSize(file.size);

    const remove = document.createElement("button");
    remove.className = "selected-file-remove";
    remove.type = "button";
    remove.dataset.fileRemoveIndex = String(index);
    remove.setAttribute("aria-label", `移除文件：${file.name}`);
    remove.textContent = "×";

    item.append(order, name, size, remove);
    items.appendChild(item);
  });
  list.appendChild(items);

  if (trimmedCount > 0) {
    const warning = document.createElement("div");
    warning.className = "selected-file-warning";
    warning.textContent = `最多选择 ${limit} 个文件，已忽略多出的 ${trimmedCount} 个。`;
    list.appendChild(warning);
  }
}

function updateFileControl(control, input) {
  const name = control.querySelector(".file-name");
  const trimmedCount = trimFilesToLimit(input);
  const files = Array.from(input.files || []);
  if (!name || files.length === 0) {
    clearFileControl(control);
    return;
  }
  const fileNames = files.map((file) => file.name);
  if (input.multiple) {
    const limit = fileLimitFor(input);
    const canAdd = !limit || files.length < limit;
    name.textContent = limit
      ? `已选择 ${files.length} / ${limit} 个文件${canAdd ? "，可继续添加" : ""}`
      : `已选择 ${files.length} 个文件`;
  } else {
    name.textContent = fileNames[0];
  }
  name.setAttribute("title", fileNames.join("\n"));
  control.classList.add("has-file");
  renderSelectedFileList(control, input, trimmedCount);
}

function openFilePicker(control) {
  const currentInput = control.querySelector(".file-input");
  if (!(currentInput instanceof HTMLInputElement)) {
    return;
  }
  const existingFiles = currentInput.multiple ? Array.from(currentInput.files || []) : [];

  const picker = document.createElement("input");
  picker.type = "file";
  picker.className = currentInput.className;
  picker.name = currentInput.name;
  picker.accept = currentInput.accept;
  picker.required = currentInput.required;
  picker.multiple = currentInput.multiple;
  picker.disabled = currentInput.disabled;
  Object.entries(currentInput.dataset).forEach(([key, value]) => {
    picker.dataset[key] = value;
  });

  picker.addEventListener(
    "change",
    () => {
      if (!picker.files?.[0]) {
        picker.remove();
        return;
      }
      const selectedFiles = Array.from(picker.files || []);
      picker.removeAttribute("style");
      currentInput.replaceWith(picker);
      if (currentInput.multiple && existingFiles.length) {
        setInputFiles(picker, mergeFiles(existingFiles, selectedFiles));
      }
      updateFileControl(control, picker);
    },
    { once: true },
  );
  picker.addEventListener("cancel", () => picker.remove(), { once: true });

  picker.style.position = "fixed";
  picker.style.left = "-9999px";
  picker.style.top = "0";
  document.body.appendChild(picker);
  picker.click();
}

document.addEventListener("click", (event) => {
  const control = event.target.closest(".file-upload-control");
  if (!control || event.target.closest(".file-clear")) {
    return;
  }
  event.preventDefault();
  openFilePicker(control);
});

document.addEventListener("click", (event) => {
  const clear = event.target.closest(".file-clear");
  if (!clear) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  clearFileControl(clear);
});

document.addEventListener("click", (event) => {
  const add = event.target.closest("[data-file-add]");
  if (!add) {
    return;
  }
  event.preventDefault();
  const field = add.closest(".multi-file-field");
  const control = field?.querySelector(".file-upload-control");
  if (control) {
    openFilePicker(control);
  }
});

document.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-file-remove-index]");
  if (!remove) {
    return;
  }
  event.preventDefault();
  const field = remove.closest(".multi-file-field");
  const input = field?.querySelector(".file-input");
  const control = field?.querySelector(".file-upload-control");
  const removeIndex = Number(remove.dataset.fileRemoveIndex);
  if (!(input instanceof HTMLInputElement) || !control || !Number.isInteger(removeIndex)) {
    return;
  }
  const files = Array.from(input.files || []).filter((_, index) => index !== removeIndex);
  if (!setInputFiles(input, files)) {
    input.value = "";
  }
  updateFileControl(control, input);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const clear = event.target.closest(".file-clear");
  if (!clear) {
    return;
  }
  event.preventDefault();
  clearFileControl(clear);
});

document.addEventListener("change", (event) => {
  const input = event.target;
  if (input instanceof HTMLSelectElement && input.classList.contains("proxy-mode-select")) {
    const form = input.closest("form");
    if (form) {
      form.dataset.proxyMode = input.value;
    }
    return;
  }
  if (input instanceof HTMLSelectElement && input.name === "model_id") {
    writeLastModelId(input.value);
    return;
  }
  if (!(input instanceof HTMLInputElement) || input.type !== "file") {
    return;
  }
  const control = input.closest(".file-upload-control");
  const name = control?.querySelector(".file-name");
  if (!name) {
    return;
  }
  if (!input.files?.length) {
    return;
  }
  updateFileControl(control, input);
});

const AUTO_REFRESH_KEY = "document-check:auto-refresh";
const AUTO_REFRESH_INTERACTION_PAUSE_MS = 8000;
let autoRefreshTimer = null;
let autoRefreshPending = false;
let autoRefreshSuspendedUntil = 0;
let taskListPointerInside = false;
let taskListFocusInside = false;

function autoRefreshEnabled() {
  const saved = window.localStorage.getItem(AUTO_REFRESH_KEY);
  return saved === null || saved === "1";
}

function setAutoRefreshEnabled(enabled) {
  window.localStorage.setItem(AUTO_REFRESH_KEY, enabled ? "1" : "0");
}

function suspendAutoRefresh(duration = AUTO_REFRESH_INTERACTION_PAUSE_MS) {
  autoRefreshSuspendedUntil = Math.max(autoRefreshSuspendedUntil, Date.now() + duration);
}

function taskListInteractiveTarget(target) {
  if (!(target instanceof Element)) {
    return null;
  }
  return target.closest(
    '[data-refresh-region="task-list"] a, [data-refresh-region="task-list"] button, [data-refresh-region="task-list"] form',
  );
}

function autoRefreshSuspended() {
  return (
    Boolean(activeConfirmPopover)
    || taskListPointerInside
    || taskListFocusInside
    || Date.now() < autoRefreshSuspendedUntil
  );
}

function updateRefreshToggle() {
  const toggle = document.querySelector("[data-auto-refresh-toggle]");
  if (!(toggle instanceof HTMLButtonElement)) {
    return;
  }
  const enabled = autoRefreshEnabled();
  const label = toggle.querySelector("[data-refresh-label]");
  toggle.classList.toggle("is-on", enabled);
  toggle.classList.toggle("is-off", !enabled);
  toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
  if (label) {
    label.textContent = enabled ? "刷新中" : "已暂停";
  }
}

function replaceRefreshRegion(documentFragment, name) {
  const current = document.querySelector(`[data-refresh-region="${name}"]`);
  const next = documentFragment.querySelector(`[data-refresh-region="${name}"]`);
  if (current && next) {
    current.innerHTML = next.innerHTML;
  }
}

async function refreshTaskRegions() {
  if (autoRefreshPending || document.hidden || autoRefreshSuspended()) {
    return;
  }
  autoRefreshPending = true;
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("_refresh", Date.now().toString());
    const response = await fetch(url.toString(), {
      cache: "no-store",
      headers: { "X-Requested-With": "fetch" },
    });
    if (!response.ok) {
      return;
    }
    const html = await response.text();
    if (autoRefreshSuspended()) {
      return;
    }
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    replaceRefreshRegion(nextDocument, "stats");
    replaceRefreshRegion(nextDocument, "task-list");
  } finally {
    autoRefreshPending = false;
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer || !document.querySelector("[data-auto-refresh-toggle]")) {
    return;
  }
  autoRefreshTimer = window.setInterval(refreshTaskRegions, 2000);
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
  if (autoRefreshEnabled()) {
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
  if (!taskListPointerInside || taskListInteractiveTarget(event.relatedTarget)) {
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
    taskListFocusInside = Boolean(taskListInteractiveTarget(document.activeElement));
    if (!taskListFocusInside) {
      suspendAutoRefresh(2000);
    }
  });
});

applyAutoRefreshState();
