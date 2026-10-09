document.querySelectorAll("[data-permission-form]").forEach((form) => {
  const choices = Array.from(form.elements).filter((input) => input.name === "permissions");
  const viewAll = choices.find((input) => input.value === "tasks.view_all");
  const manageAll = choices.find((input) => input.value === "tasks.manage_all");
  if (!viewAll || !manageAll) return;
  let saved = choices.filter((input) => input.checked).map((input) => input.value);
  let saving = false;
  let pending = false;
  const apply = (permissions) => {
    choices.forEach((input) => { input.checked = permissions.includes(input.value); });
  };
  const save = async () => {
    pending = true;
    if (saving) return;
    saving = true;
    form.dataset.saving = "true";
    while (pending) {
      pending = false;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
        });
        if (!response.ok || response.redirected) throw new Error("保存失败");
        const result = await response.json();
        if (!Array.isArray(result.permissions)) throw new Error("保存失败");
        saved = result.permissions;
        if (!pending) {
          apply(saved);
        }
      } catch {
        if (!pending) {
          apply(saved);
          showToast("保存失败，请刷新后重试", "error");
        }
      }
    }
    saving = false;
    delete form.dataset.saving;
  };
  choices.forEach((input) => {
    input.addEventListener("change", () => {
      if (input === manageAll && manageAll.checked) viewAll.checked = true;
      if (input === viewAll && !viewAll.checked) manageAll.checked = false;
      save();
    });
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); });
});

window.addEventListener("beforeunload", (event) => {
  if (document.querySelector('[data-permission-form][data-saving="true"]')) {
    event.preventDefault();
    event.returnValue = "";
  }
});
