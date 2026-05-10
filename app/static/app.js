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

document.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "file") {
    return;
  }
  const control = input.closest(".file-upload-control");
  const name = control?.querySelector(".file-name");
  if (!name) {
    return;
  }
  name.textContent = input.files?.[0]?.name || "未选择文件";
});
