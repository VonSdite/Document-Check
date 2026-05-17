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
  }
});

window.addEventListener("resize", closeConfirmPopover);
window.addEventListener("scroll", closeConfirmPopover, true);

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
}

function updateFileControl(control, input) {
  const name = control.querySelector(".file-name");
  const file = input.files?.[0];
  if (!name || !file) {
    return;
  }
  name.textContent = file.name;
  name.setAttribute("title", file.name);
  control.classList.add("has-file");
}

function openFilePicker(control) {
  const currentInput = control.querySelector(".file-input");
  if (!(currentInput instanceof HTMLInputElement)) {
    return;
  }

  const picker = document.createElement("input");
  picker.type = "file";
  picker.className = currentInput.className;
  picker.name = currentInput.name;
  picker.accept = currentInput.accept;
  picker.required = currentInput.required;
  picker.multiple = currentInput.multiple;
  picker.disabled = currentInput.disabled;

  picker.addEventListener(
    "change",
    () => {
      if (!picker.files?.[0]) {
        picker.remove();
        return;
      }
      picker.removeAttribute("style");
      currentInput.replaceWith(picker);
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
  const file = input.files?.[0];
  if (!file) {
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
