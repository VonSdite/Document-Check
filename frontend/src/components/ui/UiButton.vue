<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
    size?: 'small' | 'normal'
    type?: 'button' | 'submit' | 'reset'
    loading?: boolean
    disabled?: boolean
  }>(),
  { variant: 'secondary', size: 'normal', type: 'button' },
)
</script>
<template>
  <button
    :type="type"
    class="ui-button"
    :class="[`ui-button--${variant}`, { 'small-button': size === 'small' }]"
    :disabled="disabled || loading"
    :aria-busy="loading || undefined"
  >
    <span v-if="loading" class="ui-spinner" aria-hidden="true"></span>
    <slot />
  </button>
</template>
