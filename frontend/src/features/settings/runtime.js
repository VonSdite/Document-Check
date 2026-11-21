import { showToast } from '@/components/ui/toast'
import { request as fetch } from '@/api/client'

document.addEventListener('change', (event) => {
  const field = event.target.closest('[data-auto-save-setting]')
  if (!(field instanceof HTMLInputElement)) {
    return
  }
  const form = field.form
  if (!(form instanceof HTMLFormElement)) {
    return
  }

  const savedChecked = field.dataset.savedChecked === 'true'
  const targetChecked = field.checked
  const formData = new FormData(form)
  const actionUrl = form.getAttribute('action') || window.location.href
  const method = form.getAttribute('method') || 'POST'
  formData.set(field.name, targetChecked ? 'on' : 'off')
  field.disabled = true
  fetch(actionUrl, {
    method,
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
      return response.text().then((text) => ({ response, text }))
    })
    .then(({ response, text }) => {
      const contentType = response.headers.get('content-type') || ''
      const data = text && contentType.includes('application/json') ? JSON.parse(text) : null
      if (response.redirected && response.url.includes('/login')) {
        throw new Error('login expired')
      }
      if (text && !data) {
        console.warn('自动保存返回了非 JSON 内容，已按本次开关状态显示。', {
          status: response.status,
          url: response.url,
          contentType,
          bodyPreview: text.slice(0, 300),
        })
      }
      const saved = data?.llm_stream_trace_enabled ?? targetChecked
      field.checked = saved
      field.dataset.savedChecked = saved ? 'true' : 'false'
      showToast('定位日志设置已保存。', 'success')
    })
    .catch(() => {
      field.checked = savedChecked
      showToast('定位日志设置保存失败，请稍后重试。', 'error')
    })
    .finally(() => {
      field.disabled = false
    })
})

document.addEventListener('change', (event) => {
  const input = event.target
  if (input instanceof HTMLSelectElement && input.name === 'proxy_mode') {
    const form = input.closest('.settings-network-form')
    if (form) {
      form.dataset.networkProxyMode = input.value
      const proxyInput = form.querySelector('input[name="proxy"]')
      if (proxyInput instanceof HTMLInputElement) {
        proxyInput.required = input.value === 'custom'
      }
      return
    }
  }
})
