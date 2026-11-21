<script setup lang="ts">
import { onBeforeUnmount, ref, watch, nextTick } from 'vue'
import UiButton from './UiButton.vue'
import UiIcon from './UiIcon.vue'
const props = defineProps<{
  open: boolean
  title: string
  placement?: 'left' | 'center'
  busy?: boolean
}>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const dialog = ref<HTMLDialogElement>()
let previousFocus: HTMLElement | null = null
watch(
  () => props.open,
  async (open) => {
    await nextTick()
    if (open && dialog.value && !dialog.value.open) {
      previousFocus = document.activeElement as HTMLElement
      dialog.value.showModal()
    } else if (!open) dialog.value?.close()
  },
  { immediate: true },
)
function close() {
  if (!props.busy) emit('update:open', false)
}
function closed() {
  emit('update:open', false)
  previousFocus?.focus()
}
onBeforeUnmount(() => dialog.value?.close())
</script>
<template>
  <dialog
    ref="dialog"
    class="ui-dialog"
    :class="{ 'ui-dialog--left': placement === 'left' }"
    :aria-label="title"
    @cancel.prevent="close"
    @close="closed"
    @click="
      (event) => {
        if (event.target === dialog) close()
      }
    "
  >
    <section class="ui-dialog-card">
      <header class="ui-dialog-head">
        <h2>{{ title }}</h2>
        <UiButton variant="ghost" aria-label="关闭" :disabled="busy" @click="close">
          <UiIcon name="close" />
        </UiButton>
      </header>
      <div class="ui-dialog-content"><slot /></div>
      <footer v-if="$slots.footer" class="ui-dialog-footer">
        <slot name="footer" :close="close" />
      </footer>
    </section>
  </dialog>
</template>
