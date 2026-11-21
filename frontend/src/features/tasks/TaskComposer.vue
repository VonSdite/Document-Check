<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { UiButton, UiCheckPicker, UiFilePicker, UiSelect } from '@/components/ui'
import type { TaskComposerProps } from './contract'
import { syncTaskFormValidity } from './forms'
const props = defineProps<TaskComposerProps>()
const form = ref<HTMLFormElement>()
const model = ref(props.model)
const round = ref(props.round)
const checks = ref(props.selectedChecks)
try {
  const saved = localStorage.getItem('document-check:last-model-id')
  if (saved && props.models.some((option) => option.value === saved)) model.value = saved
} catch {
  /* 浏览器存储不可用时使用服务端默认值。 */
}
function remember() {
  try {
    localStorage.setItem('document-check:last-model-id', model.value)
  } catch {
    /* 保持表单可提交。 */
  }
}
async function validate() {
  await nextTick()
  if (form.value) syncTaskFormValidity(form.value)
}
watch([model, round, checks], validate, { deep: true })
watch(model, remember)
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
    <div class="ui-composer-fields" :class="{ 'ui-composer-fields--multiple': files.length > 1 }">
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
    <UiCheckPicker v-model="checks" :options="props.checks" :label="checkLabel" :help="checkHelp" />
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
