document.querySelectorAll("[data-permission-form]").forEach((form) => {
  const view = form.elements.namedItem("permissions");
  const choices = Array.from(view || []);
  const viewAll = choices.find((input) => input.value === "tasks.view_all");
  const manageAll = choices.find((input) => input.value === "tasks.manage_all");
  if (!viewAll || !manageAll) return;
  const sync = () => {
    if (manageAll.checked) viewAll.checked = true;
    viewAll.disabled = manageAll.checked;
  };
  manageAll.addEventListener("change", sync);
  sync();
});
