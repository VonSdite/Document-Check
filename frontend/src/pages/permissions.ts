import '@/features/permissions/editor'
import { mountComponent } from '@/app/mount'
import UserDomainEditor from '@/features/permissions/UserDomainEditor.vue'
import UserDomainFilter from '@/features/permissions/UserDomainFilter.vue'
import UserDomainBulkEditor from '@/features/permissions/UserDomainBulkEditor.vue'
import UserDomainSelection from '@/features/permissions/UserDomainSelection.vue'
import { assignedDomains, availableDomains, selectedSubjects } from '@/features/permissions/domains'

const page = document.querySelector<HTMLElement>('[data-user-domains-page]')
if (page) {
  availableDomains.value = JSON.parse(page.dataset.domainOptions || '[]')
  assignedDomains.clear()
  selectedSubjects.value = []
  const options = {
    url: page.dataset.saveUrl,
    csrfToken: page.dataset.csrfToken,
    nameLimit: Number(page.dataset.domainNameLimit),
    domainLimit: Number(page.dataset.userDomainLimit),
  }
  const filter = page.querySelector<HTMLElement>('[data-domain-filter]')
  if (filter)
    mountComponent(filter, UserDomainFilter, {
      selected:
        filter.querySelector<HTMLSelectElement>('select')?.value ?? filter.dataset.selected ?? '',
    })
  const editors = Array.from(page.querySelectorAll<HTMLElement>('[data-user-domain-editor]'))
  const subjects = editors.map((root) => root.dataset.subject || '')
  const bulk = page.querySelector<HTMLElement>('[data-domain-bulk-editor]')
  if (bulk) mountComponent(bulk, UserDomainBulkEditor, options)
  page.querySelectorAll<HTMLElement>('[data-user-domain-select]').forEach((root) => {
    mountComponent(root, UserDomainSelection, {
      subjects: root.dataset.subject ? [root.dataset.subject] : subjects,
      label: root.dataset.label,
    })
  })
  editors.forEach((root) => {
    mountComponent(root, UserDomainEditor, {
      ...options,
      subject: root.dataset.subject,
      label: root.dataset.label,
      initial: JSON.parse(root.dataset.domains || '[]'),
    })
  })
}
window.addEventListener('beforeunload', (event) => {
  if (document.querySelector('[data-domain-saving="true"]')) {
    event.preventDefault()
    event.returnValue = ''
  }
})
