let activeHelpTip = null
let activeHelpTipAnchor = null

function closestHelpTip(target) {
  if (!(target instanceof Element)) {
    return null
  }
  return target.closest('.help-tip[data-tip]')
}

function closeHelpTip() {
  if (activeHelpTip) {
    activeHelpTip.remove()
  }
  if (activeHelpTipAnchor) {
    activeHelpTipAnchor.classList.remove('is-floating-tip')
  }
  activeHelpTip = null
  activeHelpTipAnchor = null
}

function placeHelpTip(tooltip, anchor) {
  const rect = anchor.getBoundingClientRect()
  const margin = 12
  const width = tooltip.offsetWidth
  const height = tooltip.offsetHeight
  let left = rect.left + rect.width / 2 - width / 2
  let top = rect.top - height - 10
  let placement = 'top'

  if (left < margin) {
    left = margin
  }
  if (left + width > window.innerWidth - margin) {
    left = window.innerWidth - width - margin
  }
  if (top < margin) {
    top = rect.bottom + 10
    placement = 'bottom'
  }
  if (top + height > window.innerHeight - margin) {
    top = window.innerHeight - height - margin
  }

  const arrowLeft = Math.min(width - 12, Math.max(12, rect.left + rect.width / 2 - left))
  tooltip.style.left = `${left}px`
  tooltip.style.top = `${Math.max(margin, top)}px`
  tooltip.style.setProperty('--tip-arrow-left', `${arrowLeft}px`)
  tooltip.dataset.placement = placement
}

function showHelpTip(anchor) {
  const message = anchor.dataset.tip?.trim()
  if (!message) {
    return
  }
  if (activeHelpTipAnchor === anchor) {
    return
  }

  closeHelpTip()

  const tooltip = document.createElement('div')
  tooltip.className = 'floating-help-tip'
  tooltip.setAttribute('role', 'tooltip')
  tooltip.textContent = message

  const tooltipRoot = anchor.closest('dialog[open]') || document.body
  tooltipRoot.appendChild(tooltip)
  activeHelpTip = tooltip
  activeHelpTipAnchor = anchor
  anchor.classList.add('is-floating-tip')
  placeHelpTip(tooltip, anchor)
  window.requestAnimationFrame(() => tooltip.classList.add('is-visible'))
}

document.addEventListener('pointerover', (event) => {
  const anchor = closestHelpTip(event.target)
  if (!anchor) {
    return
  }
  showHelpTip(anchor)
})

document.addEventListener('pointerout', (event) => {
  const anchor = closestHelpTip(event.target)
  if (!anchor || anchor !== activeHelpTipAnchor) {
    return
  }
  if (event.relatedTarget instanceof Node && anchor.contains(event.relatedTarget)) {
    return
  }
  closeHelpTip()
})

document.addEventListener('focusin', (event) => {
  const anchor = closestHelpTip(event.target)
  if (anchor) {
    showHelpTip(anchor)
  }
})

document.addEventListener('focusout', (event) => {
  if (event.target === activeHelpTipAnchor) {
    closeHelpTip()
  }
})

window.addEventListener('resize', closeHelpTip)
window.addEventListener('scroll', closeHelpTip, true)

export { closeHelpTip }
