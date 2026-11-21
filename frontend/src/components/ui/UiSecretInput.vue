<script setup lang="ts">
import { ref } from 'vue'
import UiIcon from './UiIcon.vue'
defineOptions({ inheritAttrs: false })
const props = defineProps<{ value: string; attributes: Record<string, string> }>()
const value = ref(props.value)
const visible = ref(false)
</script>
<template>
  <div class="password-field" :class="{ 'is-visible': visible }">
    <input v-bind="attributes" v-model="value" :type="visible ? 'text' : 'password'" />
    <button
      class="password-toggle"
      type="button"
      :aria-label="visible ? '隐藏密码' : '显示密码'"
      :aria-pressed="visible"
      @click="visible = !visible"
    >
      <UiIcon name="eye" />
    </button>
  </div>
</template>
