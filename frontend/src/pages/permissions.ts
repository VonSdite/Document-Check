import '@/features/permissions/editor'
import { mountComponent } from '@/app/mount'
import UserDomainEditor from '@/features/permissions/UserDomainEditor.vue'
import UserDomainFilter from '@/features/permissions/UserDomainFilter.vue'
import { availableDomains } from '@/features/permissions/domains'

const page = document.querySelector<HTMLElement>('[data-user-domains-page]')
if (page) {
  availableDomains.value = JSON.parse(page.dataset.domainOptions || '[]')
  const filter = page.querySelector<HTMLElement>('[data-domain-filter]')
  if (filter)
    mountComponent(filter, UserDomainFilter, {
      selected:
        filter.querySelector<HTMLSelectElement>('select')?.value ?? filter.dataset.selected ?? '',
    })
  page.querySelectorAll<HTMLElement>('[data-user-domain-editor]').forEach((root) => {
    mountComponent(root, UserDomainEditor, {
      subject: root.dataset.subject,
      label: root.dataset.label,
      initial: JSON.parse(root.dataset.domains || '[]'),
      url: page.dataset.saveUrl,
      csrfToken: page.dataset.csrfToken,
      nameLimit: Number(page.dataset.domainNameLimit),
      domainLimit: Number(page.dataset.userDomainLimit),
    })
  })
}
window.addEventListener('beforeunload', (event) => {
  if (document.querySelector('[data-domain-saving="true"]')) {
    event.preventDefault()
    event.returnValue = ''
  }
})
