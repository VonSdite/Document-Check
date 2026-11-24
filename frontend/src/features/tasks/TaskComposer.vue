<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { UiButton, UiCheckPicker, UiFilePicker, UiSelect } from '@/components/ui'
import type { TaskComposerProps } from './contract'
import { syncTaskFormValidity } from './forms'
const props = defineProps<TaskComposerProps>()
const form = ref<HTMLFormElement>()
const model = ref(props.model)
const round = ref(props.round)
const checks = ref(props.selectedChecks)
const profile = ref(props.profile || '')
const profileStorageKey = `document-check:last-profile:${props.action || window.location.pathname}`
const selectedProfile = computed(() => props.profiles?.find((item) => item.id === profile.value))
const profileOptions = computed(
  () => props.profiles?.map((item) => ({ value: item.id, label: item.name })) || [],
)
const checkOptions = computed(() =>
  props.profiles === undefined
    ? props.checks
    : (selectedProfile.value?.checks || []).map((item) => ({
        value: String(item.id),
        label: item.name,
        help: item.description || item.name,
      })),
)
const checkDrafts = new Map<string, string[]>([[profile.value, [...checks.value]]])
function selectProfile(value: string) {
  checkDrafts.set(profile.value, [...checks.value])
  profile.value = value
  checks.value =
    checkDrafts.get(value) ||
    (selectedProfile.value?.checks || [])
      .filter((item) => item.default_selected)
      .map((item) => String(item.id))
}
try {
  const saved = localStorage.getItem('document-check:last-model-id')
  if (saved && props.models.some((option) => option.value === saved)) model.value = saved
  const savedProfile = localStorage.getItem(profileStorageKey)
  if (
    savedProfile &&
    props.profiles?.some((item) => item.id === savedProfile) &&
    savedProfile !== profile.value
  )
    selectProfile(savedProfile)
} catch {
  /* 浏览器存储不可用时使用服务端默认值。 */
}
function remember() {
  try {
    localStorage.setItem('document-check:last-model-id', model.value)
    if (profile.value) localStorage.setItem(profileStorageKey, profile.value)
  } catch {
    /* 保持表单可提交。 */
  }
}
async function validate() {
  await nextTick()
  if (form.value) syncTaskFormValidity(form.value)
}
watch([model, round, checks, profile], validate, { deep: true })
watch(model, remember)
watch(profile, remember)
onMounted(validate)
</script>
<template>
  <form
    ref="form"
    method="post"
    :action="action || undefined"
    enctype="multipart/form-data"
    class="panel create-panel ui-task-composer"
    autocomplete="off"
    v-bind="attributes"
    @submit="remember"
  >
    <input
      v-for="field in hidden"
      :key="field.name"
      type="hidden"
      :name="field.name"
      :value="field.value"
    />
    <h2 class="ui-composer-title">新建任务</h2>
    <div
      class="ui-composer-fields"
      :class="{
        'ui-composer-fields--multiple': files.length > 1,
        'ui-composer-fields--rounds': rounds.length > 0,
      }"
    >
      <UiFilePicker v-for="field in files" :key="field.name" v-bind="field" @change="validate" />
      <label>
        {{ modelLabel }}
        <UiSelect v-model="model" name="model_id" :options="models" required />
      </label>
      <label v-if="rounds.length">
        检查轮数
        <UiSelect v-model="round" name="check_rounds" :options="rounds" required />
      </label>
    </div>
    <div v-if="props.profiles !== undefined" class="task-profile-field">
      <label>
        检查方案
        <UiSelect
          :model-value="profile"
          name="profile_id"
          :options="profileOptions"
          required
          @update:model-value="selectProfile"
        />
      </label>
      <p class="subline">
        {{ selectedProfile?.description || '暂无可用检查方案，请联系规则维护人员。' }}
        同批文档使用所选方案。
      </p>
    </div>
    <UiCheckPicker v-model="checks" :options="checkOptions" :label="checkLabel" :help="checkHelp" />
    <div class="ui-composer-footer">
      <div class="submit-slot">
        <UiButton type="submit" variant="primary">提交检查</UiButton>
        <span class="submit-progress" data-submit-progress hidden>
          {{ attributes['data-submitting-message'] }}
        </span>
      </div>
    </div>
  </form>
</template>
