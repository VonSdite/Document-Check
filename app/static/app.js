document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const message = form.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
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
    return;
  }
  const message = target.dataset.confirmClick;
  if (message && !window.confirm(message)) {
    event.preventDefault();
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
let autoRefreshTimer = null;
let autoRefreshPending = false;

function autoRefreshEnabled() {
  const saved = window.localStorage.getItem(AUTO_REFRESH_KEY);
  return saved === null || saved === "1";
}

function setAutoRefreshEnabled(enabled) {
  window.localStorage.setItem(AUTO_REFRESH_KEY, enabled ? "1" : "0");
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
  if (autoRefreshPending || document.hidden) {
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

applyAutoRefreshState();
