import { showToast } from '@/components/ui/toast'
import { request as fetch } from '@/api/client'

const PROMPT_TAB_SESSION_KEY = 'document-check:admin-prompt-tab'

document.querySelectorAll('[data-profile-switch]').forEach((select) => {
  const switchProfile = () => select.form?.requestSubmit()
  select.addEventListener('change', switchProfile)
  const initial = Array.from(select.options).find((option) => option.defaultSelected)?.value
  if (initial && initial !== select.value) switchProfile()
})

document.querySelectorAll('[data-profile-composition]').forEach((form) => {
  const choices = Array.from(form.querySelectorAll('[data-profile-check-choice]'))
  const search = form.querySelector('[data-profile-check-search]')
  const updateCount = () => {
    form.querySelector('[data-profile-check-count]').textContent = String(
      form.querySelectorAll('[name=profile_checks]:checked').length,
    )
  }
  form.addEventListener('change', updateCount)
  updateCount()
  const filterChoices = () => {
    const terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
    choices.forEach((choice) => {
      const text = choice.dataset.searchText.toLocaleLowerCase()
      choice.hidden = !terms.every((term) => text.includes(term))
    })
    form.querySelector('[data-profile-check-empty]').hidden = choices.some(
      (choice) => !choice.hidden,
    )
  }
  search.addEventListener('input', filterChoices)
  filterChoices()
})

function promptTabButtons(container) {
  return Array.from(container.querySelectorAll('[data-prompt-tab]'))
}

function activatePromptTab(container, taskType, focus = false) {
  const tabs = promptTabButtons(container)
  const activeTab = tabs.find((tab) => tab.dataset.promptTab === taskType)
  if (!activeTab) {
    return
  }

  tabs.forEach((tab) => {
    const active = tab === activeTab
    tab.classList.toggle('is-active', active)
    tab.setAttribute('aria-selected', active ? 'true' : 'false')
    tab.tabIndex = active ? 0 : -1
  })
  container.querySelectorAll('[data-prompt-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.promptPanel !== taskType
  })

  try {
    window.sessionStorage.setItem(PROMPT_TAB_SESSION_KEY, taskType)
  } catch (_error) {
    // 浏览器禁用会话存储时，当前页面内的切换仍然有效。
  }
  if (focus) {
    activeTab.focus()
  }
}

document.querySelectorAll('[data-prompt-tabs]').forEach((container) => {
  const tabs = promptTabButtons(container)
  let savedTaskType = ''
  try {
    savedTaskType = window.sessionStorage.getItem(PROMPT_TAB_SESSION_KEY) || ''
  } catch (_error) {
    savedTaskType = ''
  }
  const initialTab =
    tabs.find((tab) => tab.dataset.promptTab === savedTaskType) ||
    tabs.find((tab) => tab.getAttribute('aria-selected') === 'true') ||
    tabs[0]
  if (initialTab) {
    activatePromptTab(container, initialTab.dataset.promptTab || '')
  }
})

document.addEventListener('click', (event) => {
  const tab = event.target.closest('[data-prompt-tab]')
  const container = tab?.closest('[data-prompt-tabs]')
  if (!tab || !container) {
    return
  }
  activatePromptTab(container, tab.dataset.promptTab || '')
})

document.addEventListener('keydown', (event) => {
  const tab = event.target.closest('[data-prompt-tab]')
  const container = tab?.closest('[data-prompt-tabs]')
  if (!tab || !container || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    return
  }

  const tabs = promptTabButtons(container)
  const currentIndex = tabs.indexOf(tab)
  let targetIndex
  if (event.key === 'Home') {
    targetIndex = 0
  } else if (event.key === 'End') {
    targetIndex = tabs.length - 1
  } else {
    const offset = event.key === 'ArrowRight' ? 1 : -1
    targetIndex = (currentIndex + offset + tabs.length) % tabs.length
  }

  const targetTab = tabs[targetIndex]
  if (targetTab) {
    event.preventDefault()
    activatePromptTab(container, targetTab.dataset.promptTab || '', true)
  }
})

async function createCheckItemWithoutReload(form) {
  const submitButton = form.querySelector('button[type="submit"]')
  const originalText = submitButton?.textContent || '创建检查项'
  if (submitButton instanceof HTMLButtonElement) {
    submitButton.disabled = true
    submitButton.textContent = '创建中...'
  }
  form.setAttribute('aria-busy', 'true')

  try {
    const response = await fetch(form.getAttribute('action') || window.location.href, {
      method: form.getAttribute('method') || 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-Requested-With': 'fetch',
      },
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok || !data.ok) {
      throw new Error(data.error || '检查项创建失败，请稍后重试。')
    }

    const panel = form.closest('[data-prompt-panel]')
    const rows = panel?.querySelector('[data-check-item-table] tbody')
    const template = document.createElement('template')
    template.innerHTML = String(data.html || '').trim()
    const createdRow = template.content.querySelector('[data-check-item-row]')
    const createdDetail = template.content.querySelector('[data-check-item-detail]')
    if (!rows || !createdRow || !createdDetail) {
      throw new Error('检查项已创建，但列表更新失败，请刷新页面查看。')
    }

    rows.prepend(template.content)
    form.reset()
    const nameInput = form.querySelector('input[name="name"]')
    if (nameInput instanceof HTMLInputElement) {
      nameInput.focus({ preventScroll: true })
    }
    showToast(data.message || '扩展检查项已创建，可继续添加。', 'success')
  } catch (error) {
    showToast(error instanceof Error ? error.message : '检查项创建失败，请稍后重试。', 'error')
  } finally {
    form.removeAttribute('aria-busy')
    if (submitButton instanceof HTMLButtonElement) {
      submitButton.disabled = false
      submitButton.textContent = originalText
    }
  }
}

document.addEventListener('submit', (event) => {
  const form = event.target.closest('[data-create-check-item-form]')
  if (!(form instanceof HTMLFormElement)) {
    return
  }
  event.preventDefault()
  createCheckItemWithoutReload(form)
})

async function deleteCheckItemWithoutReload(form) {
  const itemId = form.querySelector('input[name="item_id"]')?.value || ''
  const row = document.querySelector(`[data-check-item-row][data-check-item-id="${itemId}"]`)
  const detail = document.querySelector(`[data-check-item-detail="${itemId}"]`)
  const rows = row?.closest('tbody')
    ? Array.from(row.closest('tbody').querySelectorAll('[data-check-item-row]'))
    : []
  const rowIndex = rows.indexOf(row)
  const focusRow = rows[rowIndex + 1] || rows[rowIndex - 1] || null
  const submitButton = form.querySelector('button[type="submit"]')
  if (submitButton instanceof HTMLButtonElement) {
    submitButton.disabled = true
  }
  form.setAttribute('aria-busy', 'true')

  try {
    const response = await fetch(form.getAttribute('action') || window.location.href, {
      method: form.getAttribute('method') || 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-Requested-With': 'fetch',
      },
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok || !data.ok) {
      throw new Error(data.error || '检查项删除失败，请稍后重试。')
    }

    detail?.remove()
    row?.remove()
    const focusTarget = focusRow?.querySelector('[data-check-item-toggle]')
    if (focusTarget instanceof HTMLButtonElement) {
      focusTarget.focus({ preventScroll: true })
    }
    showToast(data.message || '扩展检查项已删除。', 'success')
  } catch (error) {
    showToast(error instanceof Error ? error.message : '检查项删除失败，请稍后重试。', 'error')
  } finally {
    form.removeAttribute('aria-busy')
    if (submitButton instanceof HTMLButtonElement) {
      submitButton.disabled = false
    }
  }
}

document.addEventListener('submit', (event) => {
  const form = event.target.closest('[data-delete-check-item-form]')
  if (!(form instanceof HTMLFormElement)) {
    return
  }
  event.preventDefault()
  deleteCheckItemWithoutReload(form)
})

let checkItemDragArmed = false
let draggedCheckItemRow = null
let suppressCheckItemClick = false

function checkItemDetailFor(row) {
  return document.querySelector(`[data-check-item-detail="${row.dataset.checkItemId}"]`)
}

function setCheckItemOpen(row, open) {
  const detail = checkItemDetailFor(row)
  const toggle = row.querySelector('[data-check-item-toggle]')
  row.classList.toggle('is-expanded', open)
  row.setAttribute('aria-expanded', open ? 'true' : 'false')
  if (toggle) {
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
  }
  if (detail) {
    detail.hidden = !open
  }
}

function moveCheckItemPair(row, reference) {
  const tbody = row.parentElement
  const detail = checkItemDetailFor(row)
  if (!tbody || reference === row || reference === detail) {
    return
  }
  const fragment = document.createDocumentFragment()
  fragment.appendChild(row)
  if (detail) {
    fragment.appendChild(detail)
  }
  tbody.insertBefore(fragment, reference || null)
}

function saveCheckItemOrder(table) {
  const formData = new FormData()
  formData.append('action', 'reorder_check_items')
  formData.append('profile_id', table.dataset.checkProfile || 'default')
  formData.append('task_type', table.dataset.checkItemTaskType || 'document_check')
  table.querySelectorAll('[data-check-item-row]').forEach((row) => {
    formData.append('item_ids', row.dataset.checkItemId)
  })
  return fetch(window.location.href, {
    method: 'POST',
    body: formData,
    headers: { 'X-Requested-With': 'fetch' },
  })
}

document.addEventListener('click', (event) => {
  if (suppressCheckItemClick) {
    suppressCheckItemClick = false
    return
  }
  const row = event.target.closest('[data-check-item-row]')
  if (!row || event.target.closest('[data-check-item-drag], .check-item-delete-button')) {
    return
  }
  setCheckItemOpen(row, !row.classList.contains('is-expanded'))
})

document.addEventListener('pointerdown', (event) => {
  checkItemDragArmed = Boolean(event.target.closest('[data-check-item-drag]'))
})

document.addEventListener('pointerup', () => {
  checkItemDragArmed = false
})

document.addEventListener('dragstart', (event) => {
  const row = event.target.closest('[data-check-item-row]')
  if (!row || !checkItemDragArmed) {
    event.preventDefault()
    return
  }
  draggedCheckItemRow = row
  row.classList.add('is-dragging')
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', row.dataset.checkItemId)
})

document.addEventListener('dragover', (event) => {
  if (!draggedCheckItemRow) {
    return
  }
  const targetRow = event.target.closest('[data-check-item-row]')
  if (
    !targetRow ||
    targetRow === draggedCheckItemRow ||
    targetRow.closest('[data-check-item-table]') !==
      draggedCheckItemRow.closest('[data-check-item-table]')
  ) {
    return
  }
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
})

document.addEventListener('drop', (event) => {
  if (!draggedCheckItemRow) {
    return
  }
  const targetRow = event.target.closest('[data-check-item-row]')
  const table = draggedCheckItemRow.closest('[data-check-item-table]')
  if (
    !targetRow ||
    targetRow === draggedCheckItemRow ||
    !table ||
    targetRow.closest('[data-check-item-table]') !== table
  ) {
    return
  }
  event.preventDefault()
  suppressCheckItemClick = true
  window.setTimeout(() => {
    suppressCheckItemClick = false
  }, 120)

  const targetDetail = checkItemDetailFor(targetRow)
  const targetRect = targetRow.getBoundingClientRect()
  const reference =
    event.clientY > targetRect.top + targetRect.height / 2
      ? targetDetail?.nextElementSibling
      : targetRow
  moveCheckItemPair(draggedCheckItemRow, reference)
  saveCheckItemOrder(table)
    .then((response) => {
      if (!response.ok) {
        window.location.reload()
      }
    })
    .catch(() => window.location.reload())
})

document.addEventListener('dragend', () => {
  if (draggedCheckItemRow) {
    draggedCheckItemRow.classList.remove('is-dragging')
  }
  draggedCheckItemRow = null
  checkItemDragArmed = false
})
