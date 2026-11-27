function syncTaskFormValidity(form) {
  if (!(form instanceof HTMLFormElement) || form.dataset.requireChecks !== 'true') {
    return
  }
  form.querySelectorAll('input[type="file"][required]').forEach((input) => {
    const label =
      input.closest('.form-field')?.querySelector('.field-label')?.textContent.trim() || '文件'
    input.setCustomValidity(input.files?.length ? '' : `请选择${label}。`)
  })
  const model = form.querySelector('select[name="model_id"]')
  if (model instanceof HTMLSelectElement) {
    model.setCustomValidity(
      model.value ? '' : form.dataset.modelRequiredMessage || '请先在模型管理中添加并启用模型。',
    )
  }
  const firstCheck = form.querySelector('input[name="checks"]')
  const checkInput = firstCheck || form.querySelector('[data-check-validation]')
  if (checkInput instanceof HTMLInputElement) {
    const message = firstCheck
      ? form.dataset.checkRequiredMessage || '请至少选择一个检查项。'
      : '暂无可用检查项，请联系管理员启用。'
    checkInput.setCustomValidity(form.querySelector('input[name="checks"]:checked') ? '' : message)
  }
}

document.querySelectorAll('form[data-require-checks="true"]').forEach(syncTaskFormValidity)

document.addEventListener('change', (event) => {
  syncTaskFormValidity(event.target.form)
})

document.addEventListener('submit', (event) => {
  const form = event.target
  if (!(form instanceof HTMLFormElement) || form.dataset.requireChecks !== 'true') {
    return
  }
  syncTaskFormValidity(form)
  if (!form.checkValidity()) {
    event.preventDefault()
    form.reportValidity()
  }
})

document.addEventListener('submit', (event) => {
  const form = event.target
  if (!(form instanceof HTMLFormElement) || form.dataset.preventDoubleSubmit !== 'true') {
    return
  }
  if (event.defaultPrevented) {
    return
  }
  if (form.dataset.submitting === 'true') {
    event.preventDefault()
    return
  }

  form.dataset.submitting = 'true'
  form.setAttribute('aria-busy', 'true')

  const progress = form.querySelector('[data-submit-progress]')
  if (progress instanceof HTMLElement) {
    progress.hidden = false
    progress.textContent = form.dataset.submittingMessage || progress.textContent
  }

  form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => {
    if (button instanceof HTMLButtonElement) {
      button.dataset.originalText = button.textContent || ''
      button.textContent = form.dataset.submittingLabel || '提交中...'
    } else if (button instanceof HTMLInputElement) {
      button.dataset.originalValue = button.value || ''
      button.value = form.dataset.submittingLabel || '提交中...'
    }
    button.disabled = true
  })
})

function resetDoubleSubmitForm(form) {
  if (!(form instanceof HTMLFormElement) || form.dataset.preventDoubleSubmit !== 'true') {
    return
  }
  delete form.dataset.submitting
  form.removeAttribute('aria-busy')
  const progress = form.querySelector('[data-submit-progress]')
  if (progress instanceof HTMLElement) {
    progress.hidden = true
  }
  form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => {
    button.disabled =
      form.dataset.requireImageModel === 'true' &&
      !form.querySelector('select[name="model_id"]')?.value
    if (button instanceof HTMLButtonElement && 'originalText' in button.dataset) {
      button.textContent = button.dataset.originalText || ''
      delete button.dataset.originalText
    } else if (button instanceof HTMLInputElement && 'originalValue' in button.dataset) {
      button.value = button.dataset.originalValue || ''
      delete button.dataset.originalValue
    }
  })
}

window.addEventListener('pageshow', () => {
  document
    .querySelectorAll("form[data-prevent-double-submit='true']")
    .forEach(resetDoubleSubmitForm)
})

export { syncTaskFormValidity, resetDoubleSubmitForm }
