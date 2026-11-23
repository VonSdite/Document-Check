<script setup lang="ts">
import { computed } from 'vue'
import { selectedSubjects } from './domains'

const props = defineProps<{ subjects: string[]; label: string }>()
const checked = computed(
  () =>
    props.subjects.length > 0 &&
    props.subjects.every((subject) => selectedSubjects.value.includes(subject)),
)
const indeterminate = computed(
  () =>
    !checked.value && props.subjects.some((subject) => selectedSubjects.value.includes(subject)),
)
function change(event: Event) {
  selectedSubjects.value = (event.target as HTMLInputElement).checked
    ? [...new Set([...selectedSubjects.value, ...props.subjects])]
    : selectedSubjects.value.filter((subject) => !props.subjects.includes(subject))
}
</script>
<template>
  <input
    type="checkbox"
    :aria-label="label"
    :title="label"
    :checked="checked"
    :indeterminate="indeterminate"
    @change="change"
  />
</template>
