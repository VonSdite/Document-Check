<script setup lang="ts">
import { notifications, dismissToast } from './toast'
</script>
<template>
  <div class="flash-stack" aria-live="polite" aria-atomic="true">
    <button
      v-for="item in notifications"
      :key="item.id"
      type="button"
      class="flash"
      :class="item.category"
      @click="dismissToast(item.id)"
    >
      {{ item.message }}
    </button>
  </div>
</template>
