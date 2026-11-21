<script setup lang="ts">
defineProps<{
  modelValue: string
  name: string
  options: { value: string; label: string; badge?: string }[]
  required?: boolean
  disabled?: boolean
}>()
defineEmits<{ 'update:modelValue': [value: string] }>()
</script>
<template>
  <span class="ui-select">
    <select
      :name="name"
      :value="modelValue"
      :required="required"
      :disabled="disabled"
      @change="$emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
    >
      <option v-for="option in options" :key="option.value" :value="option.value">
        {{ option.label }}{{ option.badge ? `（${option.badge}）` : '' }}
      </option>
    </select>
  </span>
</template>
