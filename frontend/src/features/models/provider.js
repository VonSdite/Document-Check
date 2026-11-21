document.addEventListener('click', (event) => {
  const toggle = event.target.closest('[data-provider-model-toggle]')
  if (!(toggle instanceof HTMLButtonElement)) {
    return
  }
  const group = toggle.closest('[data-provider-model-group]')
  if (!group) {
    return
  }
  const expanded = toggle.getAttribute('aria-expanded') !== 'true'
  const models = group.querySelectorAll('[data-provider-model]')
  models.forEach((model, index) => {
    model.hidden = !expanded && index >= 5
  })
  toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false')
  toggle.textContent = expanded ? '收起' : '查看全部'
  const remaining = group.querySelector('[data-provider-model-remaining]')
  if (remaining) {
    remaining.textContent = expanded
      ? '还有 0 个未显示'
      : `还有 ${toggle.dataset.overflowCount} 个未显示`
  }
})

function normalizeChatCompletionsEndpoint(value) {
  const endpoint = String(value || '').trim()
  const parts = endpoint.match(/^(https?:\/\/[^/?#\s]+)([^?#]*)([?#].*)?$/i)
  if (!parts || /\s/.test(endpoint)) {
    return endpoint
  }
  try {
    new URL(endpoint)
  } catch {
    return endpoint
  }
  let path = parts[2].replace(/\/+$/, '')
  if (!path) {
    path = '/v1/chat/completions'
  } else if (!path.endsWith('/chat/completions')) {
    path += path.endsWith('/chat') ? '/completions' : '/chat/completions'
  }
  return `${parts[1]}${path}${parts[3] || ''}`
}

function completeProviderApiBase(form) {
  const input = form.elements.api_base
  if (!(input instanceof HTMLInputElement)) {
    return ''
  }
  input.value = normalizeChatCompletionsEndpoint(input.value)
  return input.value
}

document.addEventListener('focusout', (event) => {
  const input = event.target
  const form = input.closest?.('.provider-modal-form')
  if (form && input.matches?.('[name="api_base"]')) {
    completeProviderApiBase(form)
  }
})

document.addEventListener('focusout', (event) => {
  const input = event.target
  if (input instanceof HTMLInputElement && input.name === 'api_base') {
    const form = input.closest('.provider-modal-form')
    if (form) completeProviderApiBase(form)
  }
})
document.addEventListener('submit', (event) => {
  const form = event.target
  if (form instanceof HTMLFormElement && form.matches('.provider-modal-form'))
    completeProviderApiBase(form)
})

export { normalizeChatCompletionsEndpoint, completeProviderApiBase }
