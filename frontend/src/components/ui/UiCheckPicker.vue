<script setup lang="ts">
defineProps<{
  modelValue: string[]
  options: { value: string; label: string; help: string }[]
  label: string
  help?: string
  readonly?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()
function change(values: string[], value: string, checked: boolean) {
  emit('update:modelValue', checked ? [...values, value] : values.filter((item) => item !== value))
}
</script>
<template>
  <div class="check-row">
    <div class="check-row-title">
      <span>{{ label }}</span>
      <span v-if="help" class="help-tip" tabindex="0" :data-tip="help">?</span>
    </div>
    <div class="check-chip-grid">
      <label v-for="option in options" :key="option.value" class="check-chip">
        <input
          name="checks"
          type="checkbox"
          :value="option.value"
          :checked="modelValue.includes(option.value)"
          :disabled="readonly"
          autocomplete="off"
          @change="change(modelValue, option.value, ($event.target as HTMLInputElement).checked)"
        />
        <span>{{ option.label }}</span>
        <span class="help-tip check-chip-tip" tabindex="0" :data-tip="option.help">?</span>
        <input
          v-if="readonly && modelValue.includes(option.value)"
          type="hidden"
          name="checks"
          :value="option.value"
        />
      </label>
      <template v-if="!options.length">
        <input
          class="check-validation-input"
          data-check-validation
          :aria-label="label"
          tabindex="-1"
          required
        />
        <div class="empty-state compact">
          <h3>暂无可用{{ label }}</h3>
        </div>
      </template>
    </div>
  </div>
</template>
