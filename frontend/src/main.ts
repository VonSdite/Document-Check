import AppShell from '@/app/AppShell.vue'
import { readNavigation } from '@/app/navigation'
import { mountComponent } from '@/app/mount'
import { pages } from '@/app/pages'
import { UiSecretInput, UiToastHost, showToast } from '@/components/ui'
import '@/components/ui/confirmation'
import '@/components/ui/help'
import '@/components/ui/modal'
import '@/components/ui/table-resize'
import '@/features/tasks/forms'
import '@/styles/index.css'

const navigation = document.getElementById('app-navigation')
if (navigation) mountComponent(navigation, AppShell, { ...readNavigation(navigation) })

const messages = Array.from(document.querySelectorAll<HTMLElement>('.flash')).map((node) => ({
  message: node.textContent || '',
  category: node.classList.contains('error') ? 'error' : 'success',
}))
document.querySelectorAll('.flash-stack').forEach((stack) => stack.remove())
const toastRoot = document.createElement('div')
document.body.append(toastRoot)
mountComponent(toastRoot, UiToastHost)
messages.forEach((message) => showToast(message.message, message.category))

document.querySelectorAll<HTMLInputElement>('input[type=password]').forEach((input) => {
  const attributes = Object.fromEntries(
    Array.from(input.attributes)
      .filter((attribute) => !['type', 'value'].includes(attribute.name))
      .map((attribute) => [attribute.name, attribute.value]),
  )
  const root = document.createElement('div')
  input.replaceWith(root)
  mountComponent(root, UiSecretInput, { attributes, value: input.value })
})

for (const page of pages) {
  if (document.querySelector(page.selector))
    page.load().catch(() => showToast('页面组件加载失败，请刷新后重试。', 'error'))
}
document.documentElement.dataset.frontendReady = 'true'
