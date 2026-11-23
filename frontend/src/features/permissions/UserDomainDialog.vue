<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { UiButton, UiDialog, UiIcon, showToast } from '@/components/ui'
import { addUserDomains, saveUserDomains } from '@/api/user-domains'
import { assignedDomains, availableDomains } from './domains'

const props = defineProps<{
  label: string
  subjects: string[]
  initial: string[]
  append?: boolean
  url: string
  csrfToken: string
  nameLimit: number
  domainLimit: number
}>()
const open = defineModel<boolean>('open', { required: true })
const emit = defineEmits<{ saved: [] }>()
const draft = ref<string[]>([])
const name = ref('')
const error = ref('')
const saving = ref(false)
let controller: AbortController | undefined

watch(
  open,
  (value) => {
    if (value) {
      draft.value = [...props.initial]
      name.value = ''
      error.value = ''
    } else controller?.abort()
  },
  { immediate: true },
)

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
  if (props.append && !draft.value.length) {
    error.value = '请至少选择一个领域。'
    return
  }
  controller = new AbortController()
  const pending = controller
  saving.value = true
  error.value = ''
  try {
    const result = props.append
      ? await addUserDomains(
          props.url,
          props.csrfToken,
          props.subjects,
          draft.value,
          pending.signal,
        )
      : await saveUserDomains(
          props.url,
          props.csrfToken,
          props.subjects[0],
          draft.value,
          pending.signal,
        ).then(({ subject, domains, available_domains }) => ({
          users: [{ subject, domains }],
          available_domains,
        }))
    if (pending.signal.aborted) return
    for (const user of result.users) assignedDomains.set(user.subject, user.domains)
    availableDomains.value = result.available_domains
    open.value = false
    showToast(
      props.append ? `已为 ${result.users.length} 位用户添加领域。` : '领域已保存。',
      'success',
    )
    emit('saved')
  } catch (cause) {
    if (!pending.signal.aborted)
      error.value = cause instanceof Error ? cause.message : '保存领域失败，请重试。'
  } finally {
    saving.value = false
  }
}
onBeforeUnmount(() => controller?.abort())
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="append ? '批量添加领域' : '领域'"
    :busy="saving"
    :data-domain-saving="saving || undefined"
  >
    <template v-if="open">
      <p class="domain-editor-user">{{ label }}</p>
      <p v-if="append" class="domain-editor-hint muted">保留每个人已有的领域，重复标签自动合并。</p>
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
      <UiButton variant="primary" :loading="saving" @click="save">
        {{ append ? '添加到所选用户' : '保存' }}
      </UiButton>
    </template>
  </UiDialog>
</template>
