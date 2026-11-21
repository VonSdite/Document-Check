import { showToast } from '@/components/ui/toast'
import { request as fetch } from '@/api/client'

const ipUsernameSaveTimers = new WeakMap()

function scheduleIpUsernameInputSave(input, delay = 700) {
  window.clearTimeout(ipUsernameSaveTimers.get(input))
  if (input.value.trim() === (input.dataset.savedValue || '')) {
    return
  }
  ipUsernameSaveTimers.set(
    input,
    window.setTimeout(() => saveIpUsernameInput(input), delay),
  )
}

function saveIpUsernameInput(input) {
  window.clearTimeout(ipUsernameSaveTimers.get(input))
  const form = input.closest('[data-ip-username-form]')
  if (!(form instanceof HTMLFormElement)) {
    return
  }
  const savedValue = input.dataset.savedValue || ''
  const nextValue = input.value.trim()
  if (nextValue === savedValue) {
    input.value = nextValue
    return
  }
  if (input.dataset.pendingValue !== undefined) {
    return
  }

  const formData = new FormData(form)
  formData.set(input.name, nextValue)
  input.dataset.pendingValue = nextValue
  input.classList.add('is-saving')
  fetch(form.getAttribute('action') || window.location.href, {
    method: form.getAttribute('method') || 'POST',
    body: formData,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'X-Requested-With': 'fetch',
    },
  })
    .then((response) => {
      if (!response.ok) {
        throw new Error('save failed')
      }
      return response.json()
    })
    .then((data) => {
      const savedUsername = String(data.username || '')
      input.dataset.savedValue = savedUsername
      if (input.value.trim() === nextValue) {
        input.value = savedUsername
      }
      showToast(savedUsername ? 'IP 用户名已保存。' : 'IP 用户名已清除。', 'success')
    })
    .catch(() => {
      if (input.value.trim() === nextValue) {
        input.value = savedValue
      }
      showToast('IP 用户名保存失败，请稍后重试。', 'error')
    })
    .finally(() => {
      delete input.dataset.pendingValue
      input.classList.remove('is-saving')
      if (input.value.trim() !== (input.dataset.savedValue || '')) {
        saveIpUsernameInput(input)
      }
    })
}

document.addEventListener('change', (event) => {
  const input = event.target.closest('[data-ip-username-input]')
  if (!(input instanceof HTMLInputElement)) {
    return
  }
  saveIpUsernameInput(input)
})

document.addEventListener('input', (event) => {
  const input = event.target.closest('[data-ip-username-input]')
  if (!(input instanceof HTMLInputElement)) {
    return
  }
  scheduleIpUsernameInputSave(input)
})

document.addEventListener('submit', (event) => {
  const form = event.target.closest('[data-ip-username-form]')
  if (!(form instanceof HTMLFormElement)) {
    return
  }
  event.preventDefault()
  const input = form.querySelector('[data-ip-username-input]')
  if (input instanceof HTMLInputElement) {
    saveIpUsernameInput(input)
    input.blur()
  }
})
