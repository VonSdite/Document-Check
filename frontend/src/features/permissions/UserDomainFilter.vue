<script setup lang="ts">
import { ref } from 'vue'
import { availableDomains } from './domains'
const props = defineProps<{ selected: string }>()
const selected = ref(props.selected)
</script>
<template>
  <select v-model="selected" name="domain" aria-label="领域筛选">
    <option value="">全部领域</option>
    <option value="unassigned">未标记</option>
    <option v-for="domain in availableDomains" :key="domain.id" :value="String(domain.id)">
      {{ domain.name }}
    </option>
  </select>
</template>
