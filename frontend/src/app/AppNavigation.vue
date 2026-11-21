<script setup lang="ts">
import { UiIcon } from '@/components/ui'
import type { NavigationItem } from './navigation'
defineProps<{ items: NavigationItem[]; collapsed?: boolean }>()
</script>
<template>
  <nav class="app-nav nav" aria-label="主导航">
    <a
      v-for="item in items"
      :key="item.href"
      :href="item.href"
      class="app-nav-link"
      :class="{ active: item.active }"
      :title="item.title || item.label"
      :aria-label="item.label"
      :aria-current="item.active ? 'page' : undefined"
    >
      <UiIcon :name="item.icon" />
      <span v-if="!collapsed">{{ item.title || item.label }}</span>
    </a>
  </nav>
</template>
