import { closeHelpTip } from './help'

let activeConfirmPopover = null
let activeConfirmAnchor = null

document.addEventListener('change', (event) => {
  if (
    event.target instanceof HTMLSelectElement &&
    event.target.matches('[data-page-size-select]')
  ) {
    event.target.form?.requestSubmit()
  }
})

function closeConfirmPopover() {
  if (!activeConfirmPopover) {
    return
  }
  activeConfirmPopover.remove()
  activeConfirmPopover = null
  activeConfirmAnchor = null
}

function placeConfirmPopover(popover, anchor) {
  const rect = anchor.getBoundingClientRect()
  const margin = 12
  const width = popover.offsetWidth
  const height = popover.offsetHeight
  let left = rect.left + (rect.width - width) / 2
  let top = rect.bottom + 8
  let placement = 'bottom'

  if (left < margin) {
    left = margin
  }
  if (left + width > window.innerWidth - margin) {
    left = window.innerWidth - width - margin
  }
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8
    placement = 'top'
  }
  if (top < margin) {
    top = margin
  }

  popover.style.left = `${left}px`
  popover.style.top = `${top}px`
  popover.dataset.placement = placement
  popover.style.setProperty(
    '--confirm-arrow-left',
    `${Math.max(18, Math.min(width - 18, rect.left + rect.width / 2 - left))}px`,
  )
}

function showConfirmPopover(anchor, message, onConfirm) {
  closeConfirmPopover()

  const popover = document.createElement('div')
  popover.className = 'confirm-popover'
  popover.setAttribute('role', 'dialog')
  popover.setAttribute('aria-live', 'polite')
  popover.setAttribute('aria-label', message)
  popover.innerHTML = `
    <div class="confirm-popover-message"></div>
    <div class="confirm-popover-actions">
      <button class="small-button" type="button" data-confirm-cancel>取消</button>
      <button class="small-button danger" type="button" data-confirm-ok>确认</button>
    </div>
  `
  popover.querySelector('.confirm-popover-message').textContent = message
  const popoverRoot = anchor.closest('dialog[open]') || document.body
  popoverRoot.appendChild(popover)
  activeConfirmPopover = popover
  activeConfirmAnchor = { element: anchor, rect: anchor.getBoundingClientRect() }
  placeConfirmPopover(popover, anchor)

  window.setTimeout(() => {
    const okButton = popover.querySelector('[data-confirm-ok]')
    okButton?.focus({ preventScroll: true })
  })

  popover.addEventListener('click', (event) => {
    if (event.target.closest('[data-confirm-cancel]')) {
      closeConfirmPopover()
      return
    }
    if (event.target.closest('[data-confirm-ok]')) {
      closeConfirmPopover()
      onConfirm()
    }
  })
}

document.addEventListener('submit', (event) => {
  const form = event.target
  if (!(form instanceof HTMLFormElement)) {
    return
  }
  const message = form.dataset.confirm
  if (!message || form.dataset.confirmed === 'true') {
    delete form.dataset.confirmed
    return
  }

  event.preventDefault()
  const submitter = event.submitter
  const anchor = submitter instanceof HTMLElement ? submitter : form
  showConfirmPopover(anchor, message, () => {
    form.dataset.confirmed = 'true'
    if (submitter instanceof HTMLElement && typeof form.requestSubmit === 'function') {
      form.requestSubmit(submitter)
    } else {
      form.submit()
    }
  })
})

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-confirm-click]')
  if (!target) {
    if (activeConfirmPopover && !event.target.closest('.confirm-popover')) {
      closeConfirmPopover()
    }
    return
  }
  const message = target.dataset.confirmClick
  if (!message) {
    return
  }

  event.preventDefault()
  showConfirmPopover(target, message, () => {
    const form = target.form
    if (form instanceof HTMLFormElement && typeof form.requestSubmit === 'function') {
      form.requestSubmit(target)
      return
    }
    target.click()
  })
})

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeConfirmPopover()
    closeHelpTip()
  }
})

window.addEventListener('resize', closeConfirmPopover)
window.addEventListener(
  'scroll',
  (event) => {
    const anchor = activeConfirmAnchor
    if (!anchor || (event.target !== document && !event.target.contains?.(anchor.element))) return
    const rect = anchor.element.getBoundingClientRect()
    if (Math.abs(rect.top - anchor.rect.top) > 0.5 || Math.abs(rect.left - anchor.rect.left) > 0.5)
      closeConfirmPopover()
  },
  true,
)

export { showConfirmPopover, closeConfirmPopover, activeConfirmPopover }
