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
