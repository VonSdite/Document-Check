import { mountComponent } from '@/app/mount'
import ModelEditor from '@/features/models/ModelEditor.vue'
import '@/features/models/provider'
document.querySelectorAll<HTMLElement>('[data-model-editor]').forEach((root) => {
  const form = root.closest('form')!
  const input = root.querySelector<HTMLInputElement>('[data-model-configs]')!
  mountComponent(root, ModelEditor, { form, initial: JSON.parse(input.value || '[]') })
})
