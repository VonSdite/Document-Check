import { closeHelpTip } from './help'

function openModal(id) {
  const modal = document.getElementById(id)
  if (!(modal instanceof HTMLDialogElement)) {
    return
  }
  closeHelpTip()
  if (typeof modal.showModal === 'function') {
    modal.showModal()
  } else {
    modal.setAttribute('open', '')
  }
  const firstField = modal.querySelector("input:not([type='hidden']), select, textarea, button")
  window.setTimeout(() => firstField?.focus())
}

function closeModal(target) {
  const modal = target.closest('dialog')
  if (!(modal instanceof HTMLDialogElement)) {
    return
  }
  closeHelpTip()
  if (typeof modal.close === 'function') {
    modal.close()
  } else {
    modal.removeAttribute('open')
  }
}

document.addEventListener('click', (event) => {
  const opener = event.target.closest('[data-modal-open]')
  if (opener) {
    event.preventDefault()
    openModal(opener.dataset.modalOpen)
    return
  }

  const closer = event.target.closest('[data-modal-close]')
  if (closer) {
    event.preventDefault()
    closeModal(closer)
    return
  }
})

export { openModal, closeModal }
