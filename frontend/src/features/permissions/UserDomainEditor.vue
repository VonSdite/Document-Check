<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { UiButton, UiDialog, UiIcon, showToast } from '@/components/ui'
import { saveUserDomains, type ProductDomain } from '@/api/user-domains'
import { availableDomains } from './domains'

const props = defineProps<{
  subject: string
  label: string
  initial: ProductDomain[]
  url: string
  csrfToken: string
  nameLimit: number
  domainLimit: number
}>()
const saved = ref(props.initial)
const open = ref(false)
const draft = ref<string[]>([])
const name = ref('')
const error = ref('')
const saving = ref(false)
let controller: AbortController | undefined

function edit() {
  draft.value = saved.value.map((domain) => domain.name)
  name.value = ''
  error.value = ''
  open.value = true
}
function addName() {
  const value = name.value.trim().replace(/\s+/g, ' ')
  if (!value) return true
  if (Array.from(value).length > props.nameLimit) {
    error.value = `领域名称最多 ${props.nameLimit} 个字符。`
    return false
  }
  const existing = availableDomains.value.find(
    (domain) => domain.name.toLowerCase() === value.toLowerCase(),
  )
  const canonical = existing?.name || value
  if (!draft.value.some((item) => item.toLowerCase() === canonical.toLowerCase())) {
    if (draft.value.length >= props.domainLimit) {
      error.value = `每个用户最多设置 ${props.domainLimit} 个领域。`
      return false
    }
    draft.value.push(canonical)
  }
  name.value = ''
  error.value = ''
  return true
}
async function save() {
  if (saving.value || !addName()) return
  if (draft.value.length > props.domainLimit) {
    error.value = `每个用户最多设置 ${props.domainLimit} 个领域。`
    return
  }
  controller = new AbortController()
  const pending = controller
  saving.value = true
  error.value = ''
  try {
    const result = await saveUserDomains(
      props.url,
      props.csrfToken,
      props.subject,
      draft.value,
      pending.signal,
    )
    if (pending.signal.aborted) return
    saved.value = result.domains
    availableDomains.value = result.available_domains
    open.value = false
    showToast('产品领域已保存。', 'success')
  } catch (cause) {
    if (!pending.signal.aborted)
      error.value = cause instanceof Error ? cause.message : '保存领域失败，请重试。'
  } finally {
    saving.value = false
  }
}
watch(open, (value) => {
  if (!value) controller?.abort()
})
onBeforeUnmount(() => controller?.abort())
</script>
<template>
  <div class="user-domain-editor" :data-domain-saving="saving || undefined">
    <div class="user-domain-tags">
      <span v-for="domain in saved" :key="domain.id" class="user-domain-tag">
        {{ domain.name }}
      </span>
      <span v-if="!saved.length" class="muted">未标记</span>
    </div>
    <UiButton size="small" variant="ghost" :aria-label="`${label}：编辑产品领域`" @click="edit">
      {{ saved.length ? '编辑领域' : '设置领域' }}
    </UiButton>
    <UiDialog v-model:open="open" title="产品领域" :busy="saving">
      <template v-if="open">
        <p class="domain-editor-user">{{ label }}</p>
        <fieldset class="domain-editor-fields" :disabled="saving">
          <div class="domain-editor-selection">
            <span class="field-label">已选领域</span>
            <div class="user-domain-tags">
              <span v-for="value in draft" :key="value" class="user-domain-tag">
                {{ value }}
                <button
                  type="button"
                  :aria-label="`移除领域 ${value}`"
                  @click="draft = draft.filter((item) => item !== value)"
                >
                  <UiIcon name="close" :size="14" />
                </button>
              </span>
              <span v-if="!draft.length" class="muted">未选择领域</span>
            </div>
          </div>
          <form class="domain-editor-add" @submit.prevent="addName">
            <label>
              新领域
              <input v-model="name" placeholder="输入领域名称" :maxlength="nameLimit * 2" />
            </label>
            <UiButton type="submit">添加</UiButton>
          </form>
          <div v-if="availableDomains.length" class="domain-editor-options">
            <span class="field-label">选择已有领域</span>
            <div class="domain-editor-choices">
              <label v-for="domain in availableDomains" :key="domain.id" class="ui-checkbox-label">
                <input v-model="draft" type="checkbox" :value="domain.name" />
                <span>{{ domain.name }}</span>
              </label>
            </div>
          </div>
        </fieldset>
        <p v-if="error" class="domain-editor-error" role="alert">{{ error }}</p>
      </template>
      <template #footer="{ close }">
        <UiButton :disabled="saving" @click="close">取消</UiButton>
        <UiButton variant="primary" :loading="saving" @click="save">保存</UiButton>
      </template>
    </UiDialog>
  </div>
</template>
