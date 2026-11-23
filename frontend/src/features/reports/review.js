import { showToast } from '@/components/ui/toast'
import { openModal } from '@/components/ui/modal'
import { request as fetch } from '@/api/client'

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-report-import-trigger]')
  if (!(trigger instanceof HTMLButtonElement)) {
    return
  }
  const input = trigger.form?.querySelector('[data-report-import-input]')
  if (input instanceof HTMLInputElement) {
    input.click()
  }
})

document.addEventListener('change', (event) => {
  const input = event.target.closest('[data-report-import-input]')
  if (!(input instanceof HTMLInputElement) || !input.files?.[0]) {
    return
  }
  const file = input.files[0]
  if (!file.name.toLowerCase().endsWith('.xlsx')) {
    input.value = ''
    showToast('回填文件仅支持系统导出的 xlsx 格式报告。', 'error')
    return
  }
  const form = input.form
  const trigger = form?.querySelector('[data-report-import-trigger]')
  const label = form?.querySelector('[data-report-import-label]')
  if (trigger instanceof HTMLButtonElement) {
    trigger.disabled = true
  }
  if (label) {
    label.textContent = '正在回填...'
  }
  form?.requestSubmit()
})

function reportItemControls(item) {
  return {
    acceptance: item.querySelectorAll('[data-report-acceptance-status]'),
    reason: item.querySelector('[data-report-rejection-reason]'),
    reasonTrigger: item.querySelector('[data-report-rejection-trigger]'),
    note: item.querySelector('[data-report-rejection-note]'),
  }
}

function reportControlNodes(control) {
  if (control instanceof NodeList) {
    return Array.from(control)
  }
  return control ? [control] : []
}

function reportControlValue(control, fallback = '') {
  const nodes = reportControlNodes(control)
  const selected = nodes.find((node) =>
    node instanceof HTMLInputElement && node.type === 'radio' ? node.checked : true,
  )
  return selected?.value || fallback
}

function setReportControlValue(control, value) {
  reportControlNodes(control).forEach((node) => {
    if (node instanceof HTMLInputElement && node.type === 'radio') {
      node.checked = node.value === value
    } else if (node instanceof HTMLInputElement || node instanceof HTMLSelectElement) {
      node.value = value
    }
  })
}

function setReportControlSavedValue(control, value) {
  reportControlNodes(control).forEach((node) => {
    node.dataset.savedValue = value
  })
}

function reportControlDisabled(control) {
  const nodes = reportControlNodes(control)
  return nodes.length > 0 && nodes.every((node) => node.disabled)
}

function syncReportAcceptanceFields(item) {
  const controls = reportItemControls(item)
  if (controls.acceptance.length === 0) {
    return
  }
  const rejected = reportControlValue(controls.acceptance) === 'rejected'
  const acceptanceDisabled = reportControlDisabled(controls.acceptance)
  const rejectionControls = item.querySelector('[data-report-rejection-controls]')
  if (rejectionControls instanceof HTMLElement) {
    rejectionControls.hidden = !rejected
  }
  ;[controls.reason, controls.reasonTrigger, controls.note].forEach((control) => {
    reportControlNodes(control).forEach((node) => {
      if (
        node instanceof HTMLSelectElement ||
        node instanceof HTMLInputElement ||
        node instanceof HTMLButtonElement
      ) {
        node.disabled = acceptanceDisabled || !rejected
      }
    })
  })
}

function setReportItemControlsDisabled(item, disabled) {
  Object.values(reportItemControls(item)).forEach((control) => {
    reportControlNodes(control).forEach((node) => {
      if (
        node instanceof HTMLSelectElement ||
        node instanceof HTMLInputElement ||
        node instanceof HTMLButtonElement
      ) {
        node.disabled = disabled
      }
    })
  })
}

function revertReportItemControls(item) {
  const controls = reportItemControls(item)
  const savedAcceptance =
    reportControlNodes(controls.acceptance)[0]?.dataset.savedValue || 'pending'
  setReportControlValue(controls.acceptance, savedAcceptance)
  item.dataset.acceptanceStatus = savedAcceptance
  const savedReason = reportControlNodes(controls.reason)[0]?.dataset.savedValue || ''
  setReportControlValue(controls.reason, savedReason)
  setReportControlSavedValue(controls.reason, savedReason)
  if (controls.note instanceof HTMLInputElement) {
    controls.note.value = controls.note.dataset.savedValue || ''
  }
  syncReportAcceptanceFields(item)
  syncReportRejectionTrigger(item)
}

function applyReportItemSave(item, data) {
  const version = item.querySelector('[data-review-token]')
  if (version && data.review_token) version.value = data.review_token
  const attribution = item.querySelector('[data-review-attribution]')
  if (attribution) attribution.textContent = `初审 ${data.review_initial_label || ''}`
  const finalStatus = item.querySelector('[data-final-review-status]')
  if (finalStatus) finalStatus.textContent = `终审 ${data.final_review_label || '待终审'}`
  const controls = reportItemControls(item)
  const savedAcceptance =
    data.acceptance_status || reportControlValue(controls.acceptance, 'pending')
  const savedReason = data.rejection_reason || ''
  const savedNote = data.rejection_note || ''
  setReportControlValue(controls.acceptance, savedAcceptance)
  setReportControlSavedValue(controls.acceptance, savedAcceptance)
  setReportControlValue(controls.reason, savedReason)
  setReportControlSavedValue(controls.reason, savedReason)
  if (controls.note instanceof HTMLInputElement) {
    controls.note.value = savedNote
    controls.note.dataset.savedValue = savedNote
  }
  item.dataset.acceptanceStatus = savedAcceptance
  syncReportAcceptanceFields(item)
  syncReportRejectionTrigger(item)
}

function reportItemPayload(item) {
  const controls = reportItemControls(item)
  return {
    result_code: item.dataset.resultCode,
    item_id: item.dataset.itemId,
    review_token: item.querySelector('[data-review-token]')?.value || '',
    acceptance_status: reportControlValue(controls.acceptance, 'pending'),
    rejection_reason: reportControlValue(controls.reason),
    rejection_note: controls.note instanceof HTMLInputElement ? controls.note.value.trim() : '',
  }
}

function validateReportAcceptance(item) {
  const controls = reportItemControls(item)
  const payload = reportItemPayload(item)
  syncReportAcceptanceFields(item)
  if (payload.acceptance_status !== 'rejected') {
    return true
  }
  if (!payload.rejection_reason) {
    openReportRejectionDialog(item)
    return false
  }
  if (payload.rejection_reason === 'other' && !payload.rejection_note) {
    showToast('选择其他原因时必须填写具体原因。', 'error')
    controls.note?.focus()
    return false
  }
  return true
}

let activeReportRejectionItem = null

function reportRejectionChoice(reason) {
  return (
    Array.from(document.querySelectorAll('[data-report-rejection-choice]')).find(
      (choice) => choice.dataset.reportRejectionChoice === reason,
    ) || null
  )
}

function syncReportRejectionTrigger(item) {
  const controls = reportItemControls(item)
  if (!(controls.reasonTrigger instanceof HTMLButtonElement)) {
    return
  }
  const reason = reportControlValue(controls.reason)
  const choice = reportRejectionChoice(reason)
  const label = controls.reasonTrigger.querySelector('[data-report-rejection-trigger-label]')
  const hint = controls.reasonTrigger.querySelector('[data-report-rejection-trigger-hint]')
  controls.reasonTrigger.classList.toggle('has-value', Boolean(choice))
  if (label) {
    label.textContent = choice?.dataset.rejectionLabel || '选择不认可原因'
  }
  if (hint) {
    hint.textContent = choice?.dataset.rejectionHint || '查看判断标准后选择'
  }
}

function openReportRejectionDialog(item) {
  const modal = document.querySelector('[data-report-rejection-modal]')
  const trigger = reportItemControls(item).reasonTrigger
  if (!(modal instanceof HTMLDialogElement) || !(trigger instanceof HTMLButtonElement)) {
    return
  }
  activeReportRejectionItem = item
  const currentReason = reportControlValue(reportItemControls(item).reason)
  modal.querySelectorAll('[data-report-rejection-choice]').forEach((choice) => {
    choice.classList.toggle('is-selected', choice.dataset.reportRejectionChoice === currentReason)
    choice.setAttribute(
      'aria-pressed',
      choice.dataset.reportRejectionChoice === currentReason ? 'true' : 'false',
    )
  })
  openModal(modal.id)
  window.requestAnimationFrame(() => placeReportRejectionModal(modal, trigger))
}

function placeReportRejectionModal(modal, anchor) {
  if (!(modal instanceof HTMLDialogElement) || !modal.open || !(anchor instanceof HTMLElement)) {
    return
  }
  const rect = anchor.getBoundingClientRect()
  const margin = 12
  const gap = 8
  const width = modal.offsetWidth
  const height = modal.offsetHeight
  let left = rect.right + gap
  let top = rect.top
  if (left + width > window.innerWidth - margin) {
    left = rect.left - width - gap
  }
  if (left < margin) {
    left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))
  }
  if (top + height > window.innerHeight - margin) {
    top = window.innerHeight - height - margin
  }
  if (top < margin) {
    top = margin
  }
  modal.style.left = `${left}px`
  modal.style.top = `${top}px`
}

function saveReportItemReview(item) {
  const results = item.closest('[data-report-results]')
  if (!(results instanceof HTMLElement)) {
    return
  }
  const url = results.dataset.reportClassificationUrl
  if (!url || !validateReportAcceptance(item)) {
    return
  }
  const payload = reportItemPayload(item)

  setReportItemControlsDisabled(item, true)
  item.classList.add('is-saving')
  fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Requested-With': 'fetch',
    },
    body: JSON.stringify(payload),
  })
    .then((response) => response.json())
    .then((data) => {
      if (!data.ok) {
        throw new Error(data.error || 'save failed')
      }
      applyReportItemSave(item, data)
      updateReportCounts(data.totals || {})
    })
    .catch((error) => {
      revertReportItemControls(item)
      showToast(error.message || '报告条目复核结果保存失败，请稍后重试。', 'error')
    })
    .finally(() => {
      setReportItemControlsDisabled(item, false)
      syncReportAcceptanceFields(item)
      item.classList.remove('is-saving')
    })
}

document.addEventListener('change', (event) => {
  const control = event.target.closest(
    '[data-report-acceptance-status], [data-report-rejection-reason], [data-report-rejection-note]',
  )
  if (!(control instanceof HTMLSelectElement) && !(control instanceof HTMLInputElement)) {
    return
  }
  const item = control.closest('[data-report-item]')
  if (!(item instanceof HTMLElement)) {
    return
  }
  saveReportItemReview(item)
})

const reportRejectionModal = document.querySelector('[data-report-rejection-modal]')
if (reportRejectionModal instanceof HTMLDialogElement) {
  reportRejectionModal.addEventListener('close', () => {
    const item = activeReportRejectionItem
    activeReportRejectionItem = null
    if (!(item instanceof HTMLElement) || reportItemPayload(item).rejection_reason) {
      return
    }
    const controls = reportItemControls(item)
    const savedAcceptance =
      reportControlNodes(controls.acceptance)[0]?.dataset.savedValue || 'pending'
    setReportControlValue(controls.acceptance, savedAcceptance)
    item.dataset.acceptanceStatus = savedAcceptance
    syncReportAcceptanceFields(item)
  })
}

function repositionReportRejectionModal() {
  if (!(activeReportRejectionItem instanceof HTMLElement)) {
    return
  }
  const modal = document.querySelector('[data-report-rejection-modal]')
  const trigger = reportItemControls(activeReportRejectionItem).reasonTrigger
  if (modal instanceof HTMLDialogElement && trigger instanceof HTMLButtonElement) {
    placeReportRejectionModal(modal, trigger)
  }
}

window.addEventListener('resize', repositionReportRejectionModal)
window.addEventListener('scroll', repositionReportRejectionModal, true)

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-report-rejection-trigger]')
  if (trigger instanceof HTMLButtonElement) {
    const item = trigger.closest('[data-report-item]')
    if (item instanceof HTMLElement) {
      openReportRejectionDialog(item)
    }
    return
  }

  const choice = event.target.closest('[data-report-rejection-choice]')
  if (
    !(choice instanceof HTMLButtonElement) ||
    !(activeReportRejectionItem instanceof HTMLElement)
  ) {
    return
  }
  const item = activeReportRejectionItem
  const controls = reportItemControls(item)
  const reason = choice.dataset.reportRejectionChoice || ''
  setReportControlValue(controls.reason, reason)
  syncReportRejectionTrigger(item)
  const modal = choice.closest('dialog')
  if (modal instanceof HTMLDialogElement) {
    modal.close()
  }
  activeReportRejectionItem = null
  if (
    reason === 'other' &&
    controls.note instanceof HTMLInputElement &&
    !controls.note.value.trim()
  ) {
    controls.note.focus()
    return
  }
  saveReportItemReview(item)
})

function updateReportCounts(totals) {
  Object.entries(totals || {}).forEach(([key, value]) => {
    document.querySelectorAll(`[data-report-count="${key}"]`).forEach((node) => {
      node.textContent = String(value ?? 0)
    })
  })
}

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-video-timestamp]')
  if (!trigger) {
    return
  }
  const player = document.querySelector('[data-report-video-player]')
  if (!(player instanceof HTMLVideoElement)) {
    return
  }
  const timestamp = Number(trigger.dataset.videoTimestamp)
  if (!Number.isFinite(timestamp) || timestamp < 0) {
    return
  }
  player.currentTime = timestamp
  player.scrollIntoView({ behavior: 'smooth', block: 'center' })
  const playback = player.play()
  if (playback instanceof Promise) {
    playback.catch(() => {})
  }
})
