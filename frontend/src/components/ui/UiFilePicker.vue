<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import UiButton from './UiButton.vue'
import UiIcon from './UiIcon.vue'
import { fileIdentity, fileSize, mergeFiles } from './files'
const props = withDefaults(
  defineProps<{
    name: string
    label: string
    accept: string
    multiple?: boolean
    required?: boolean
    limit?: number
    help?: string
    initialFiles?: File[]
  }>(),
  { limit: 0, help: '', initialFiles: () => [] },
)
const emit = defineEmits<{ change: [] }>()
const input = ref<HTMLInputElement>()
const files = ref<File[]>(props.initialFiles)
const notice = ref('')
const summary = computed(() =>
  files.value.length
    ? `${files.value.length} 个文件 · ${fileSize(files.value.reduce((size, file) => size + file.size, 0))}`
    : '选择或拖入文件',
)
function assign(next: File[]) {
  if (!input.value) return
  const transfer = new DataTransfer()
  next.forEach((file) => transfer.items.add(file))
  input.value.files = transfer.files
  input.value.setCustomValidity(next.length || !props.required ? '' : `请选择${props.label}。`)
  files.value = next
  emit('change')
}
function select(selected: File[]) {
  if (!selected.length) return
  const merged = mergeFiles(
    props.multiple ? files.value : [],
    selected,
    props.multiple ? props.limit : 1,
  )
  notice.value = merged.omitted
    ? `最多选择 ${props.limit || 1} 个文件，已保留前 ${merged.files.length} 个。`
    : ''
  assign(merged.files)
}
function drop(event: DragEvent) {
  if (event.dataTransfer) select(Array.from(event.dataTransfer.files))
}
onMounted(() => {
  if (files.value.length) assign(files.value)
})
</script>
<template>
  <div class="form-field multi-file-field ui-file-picker">
    <span class="field-label">
      {{ label }}
      <span v-if="help" class="help-tip" tabindex="0" :data-tip="help">?</span>
    </span>
    <div class="ui-file-drop" @dragover.prevent @drop.prevent="drop">
      <label class="ui-file-trigger">
        <input
          ref="input"
          class="file-input"
          :name="name"
          type="file"
          :accept="accept"
          :multiple="multiple"
          :required="required"
          :aria-label="label"
          @change="select(Array.from(($event.target as HTMLInputElement).files || []))"
        />
        <UiIcon name="upload" />
        <span>{{ summary }}</span>
      </label>
      <UiButton
        v-if="files.length"
        size="small"
        variant="ghost"
        :aria-label="`清除${label}`"
        @click="assign([])"
      >
        <UiIcon name="close" :size="16" />
      </UiButton>
    </div>
    <div v-if="files.length" class="ui-file-list" data-file-list>
      <div v-for="(file, index) in files" :key="fileIdentity(file)" class="ui-file-row">
        <span :title="file.name">{{ file.name }}</span>
        <small>{{ fileSize(file.size) }}</small>
        <UiButton
          variant="ghost"
          size="small"
          :aria-label="`移除 ${file.name}`"
          @click="assign(files.filter((_, position) => position !== index))"
        >
          <UiIcon name="close" :size="14" />
        </UiButton>
      </div>
    </div>
    <small v-if="notice" role="status" class="ui-file-notice">{{ notice }}</small>
  </div>
</template>
