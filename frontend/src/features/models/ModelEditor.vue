<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { modelsApi } from '@/api/models'
import { UiButton, UiDialog, showToast } from '@/components/ui'
import { normalizeChatCompletionsEndpoint } from './provider'
import { efforts, mergeFetched, normalizeModels, type ModelConfig } from './configuration'
const props = defineProps<{ form: HTMLFormElement; initial: ModelConfig[] }>()
let sequence = 0
type Row = ModelConfig & {
  id: number
  testing?: boolean
  testingImage?: boolean
  message?: string
  failed?: boolean
  imageSignature: string
  controller?: AbortController
}
const makeRow = (model: ModelConfig): Row => ({
  ...model,
  id: ++sequence,
  imageSignature: imageSignature(model),
})
const empty = (): ModelConfig => ({
  model_name: '',
  reasoning_effort: '',
  force_disable_thinking: false,
})
const rows = ref<Row[]>(
  props.initial.length ? normalizeModels(props.initial).map(makeRow) : [makeRow(empty())],
)
const configs = computed(() =>
  normalizeModels(rows.value.map((row) => ({ ...row, image_input_status: imageStatus(row) }))),
)
const fetching = ref(false)
const picker = ref(false)
const candidates = ref<string[]>([])
const selected = ref<string[]>([])
const search = ref('')
const filtered = computed(() =>
  candidates.value.filter((name) => name.toLowerCase().includes(search.value.trim().toLowerCase())),
)
const allSelected = computed(
  () => filtered.value.length > 0 && filtered.value.every((name) => selected.value.includes(name)),
)
const pending = new Set<AbortController>()
function connection() {
  const field = (name: string) =>
    (props.form.elements.namedItem(name) as HTMLInputElement | null)?.value?.trim() || ''
  return {
    api_base: normalizeChatCompletionsEndpoint(field('api_base')),
    api_key: field('api_key'),
    request_timeout: field('request_timeout') || '600',
  }
}
function cancel() {
  pending.forEach((controller) => controller.abort())
  pending.clear()
  fetching.value = false
  rows.value.forEach((row) => {
    row.testing = false
  })
}
function changed(event: Event) {
  const input = event.target as HTMLInputElement
  if (['api_base', 'api_key', 'request_timeout'].includes(input.name)) cancel()
  if (['api_base', 'api_key'].includes(input.name)) rows.value.forEach(invalidateImage)
}
function imageSignature(model: ModelConfig) {
  const { api_base, api_key } = connection()
  return JSON.stringify([
    api_base,
    api_key,
    model.model_name.trim(),
    model.reasoning_effort,
    model.force_disable_thinking,
  ])
}
function imageStatus(row: Row) {
  return row.imageSignature === imageSignature(row)
    ? row.image_input_status || 'unknown'
    : 'unknown'
}
function invalidateImage(row: Row) {
  row.controller?.abort()
  row.testing = false
  row.imageSignature = ''
  row.image_input_status = 'unknown'
  row.message = ''
}
props.form.addEventListener('input', changed)
const parentDialog = props.form.closest('dialog')
parentDialog?.addEventListener('close', cancel)
onBeforeUnmount(() => {
  cancel()
  props.form.removeEventListener('input', changed)
  parentDialog?.removeEventListener('close', cancel)
})
async function fetchModels() {
  const body = connection()
  if (!body.api_base) return showToast('请先填写 API 地址。', 'error')
  const controller = new AbortController()
  pending.add(controller)
  fetching.value = true
  try {
    const data = await modelsApi.discover(
      props.form.dataset.fetchModelsUrl!,
      body,
      controller.signal,
    )
    if (controller.signal.aborted || JSON.stringify(connection()) !== JSON.stringify(body)) return
    candidates.value = [...new Set(data.fetched_models)].sort()
    selected.value = configs.value
      .filter((item) => !item.force_disable_thinking)
      .map((item) => item.model_name)
    search.value = ''
    picker.value = true
  } catch (error) {
    if (!controller.signal.aborted)
      showToast(error instanceof Error ? error.message : '拉取模型失败。', 'error')
  } finally {
    pending.delete(controller)
    fetching.value = false
  }
}
async function testModel(row: Row, image = false) {
  const body = { ...connection(), ...normalizeModels([row])[0] }
  if (!body.api_base || !body.model_name) return showToast('请先填写 API 地址和模型 ID。', 'error')
  const controller = new AbortController()
  pending.add(controller)
  row.controller = controller
  row.testing = true
  row.testingImage = image
  row.message = image ? '正在发送测试图片并核对识别结果…' : '正在测试模型连通性…'
  const snapshot = JSON.stringify(body)
  const current = () => JSON.stringify({ ...connection(), ...normalizeModels([row])[0] })
  try {
    const data = await modelsApi.test(
      image ? props.form.dataset.testImageModelUrl! : props.form.dataset.testModelUrl!,
      body,
      controller.signal,
    )
    if (controller.signal.aborted || current() !== snapshot || !rows.value.includes(row)) return
    row.message = data.message || '模型连通性测试通过。'
    row.failed = false
    if (image) {
      row.imageSignature = imageSignature(row)
      row.image_input_status = 'verified'
    }
  } catch (error) {
    if (!controller.signal.aborted && current() === snapshot) {
      row.message = error instanceof Error ? error.message : '模型测试失败。'
      row.failed = true
      if (image) {
        row.imageSignature = imageSignature(row)
        row.image_input_status = 'failed'
      }
    }
  } finally {
    pending.delete(controller)
    if (row.controller === controller) row.testing = false
  }
}
function apply() {
  rows.value = mergeFetched(configs.value, candidates.value, selected.value).map(makeRow)
  if (!rows.value.length) rows.value.push(makeRow(empty()))
  picker.value = false
}
function selectAll(checked: boolean) {
  const values = new Set(selected.value)
  filtered.value.forEach((name) => (checked ? values.add(name) : values.delete(name)))
  selected.value = [...values]
}
</script>
<template>
  <div class="model-editor-head">
    <div>
      <span class="field-label">模型 ID 列表</span>
      <p class="ui-section-description">
        图片和视频检查需要先通过“测试图片识别”。系统会发送一张测试图片并核对结果。
      </p>
    </div>
    <div class="model-editor-actions">
      <UiButton size="small" @click="rows = configs.map(makeRow)">整理</UiButton>
      <UiButton size="small" :loading="fetching" data-fetch-models @click="fetchModels">
        拉取模型
      </UiButton>
    </div>
  </div>
  <input type="hidden" name="model_configs" data-model-configs :value="JSON.stringify(configs)" />
  <textarea
    name="models"
    data-model-list-fallback
    hidden
    :value="configs.map((item) => item.model_name).join('\n')"
  ></textarea>
  <div class="model-editor-summary" data-model-summary>共 {{ configs.length }} 个模型</div>
  <div class="model-editor-table-wrap">
    <table class="model-editor-table">
      <thead>
        <tr>
          <th>模型 ID</th>
          <th>思考强度</th>
          <th>强制关闭思考</th>
          <th class="right">操作</th>
        </tr>
      </thead>
      <tbody data-model-rows>
        <tr v-for="row in rows" :key="row.id" data-model-row>
          <td>
            <input
              v-model="row.model_name"
              data-model-name
              aria-label="模型 ID"
              placeholder="输入模型 ID"
              @input="invalidateImage(row)"
            />
            <small class="subline" data-image-capability>
              {{
                imageStatus(row) === 'verified'
                  ? '图片识别已验证 · 可检查图片和视频'
                  : imageStatus(row) === 'failed'
                    ? '图片识别测试未通过'
                    : '图片识别尚未验证'
              }}
            </small>
            <small
              v-if="row.message"
              class="ui-model-result"
              :class="{ 'is-error': row.failed }"
              role="status"
            >
              {{ row.message }}
            </small>
          </td>
          <td>
            <select
              v-model="row.reasoning_effort"
              data-model-reasoning-effort
              aria-label="思考强度"
              :disabled="row.force_disable_thinking"
              @change="invalidateImage(row)"
            >
              <option v-for="effort in efforts" :key="effort" :value="effort">
                {{ effort || '不设置' }}
              </option>
            </select>
          </td>
          <td>
            <label class="ui-checkbox-label">
              <input
                v-model="row.force_disable_thinking"
                type="checkbox"
                data-model-thinking
                aria-label="强制关闭思考"
                @change="invalidateImage(row)"
              />
              <span>关闭思考</span>
            </label>
          </td>
          <td>
            <div class="task-actions">
              <UiButton
                size="small"
                :disabled="row.testing"
                :loading="row.testing && !row.testingImage"
                @click="testModel(row)"
              >
                测试
              </UiButton>
              <UiButton
                size="small"
                :disabled="row.testing"
                :loading="row.testing && row.testingImage"
                data-test-image-model
                @click="testModel(row, true)"
              >
                测试图片识别
              </UiButton>
              <UiButton
                size="small"
                variant="danger"
                @click="rows = rows.filter((item) => item.id !== row.id)"
              >
                移除
              </UiButton>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <UiButton size="small" class="ui-model-add" @click="rows.push(makeRow(empty()))">
    ＋ 添加模型
  </UiButton>
  <UiDialog v-model:open="picker" title="选择拉取模型">
    <p class="muted">取消勾选会移除对应模型；本次未返回的模型和关闭思考的配置会保留。</p>
    <input v-model="search" type="search" placeholder="搜索模型" aria-label="搜索模型" />
    <div class="ui-fetched-heading">
      <label class="ui-checkbox-label">
        <input
          type="checkbox"
          :checked="allSelected"
          @change="selectAll(($event.target as HTMLInputElement).checked)"
        />
        选择当前结果
      </label>
      <span>
        {{ filtered.length }} 个结果 · 已选
        {{ candidates.filter((name) => selected.includes(name)).length }} 个
      </span>
    </div>
    <div class="ui-fetched-models">
      <label v-for="name in filtered" :key="name" class="ui-checkbox-label">
        <input v-model="selected" type="checkbox" :value="name" />
        <span>{{ name }}</span>
      </label>
      <p v-if="!filtered.length" class="empty-state compact">没有匹配模型</p>
    </div>
    <template #footer="{ close }">
      <UiButton @click="close">取消</UiButton>
      <UiButton variant="primary" @click="apply">加入当前清单</UiButton>
    </template>
  </UiDialog>
</template>
