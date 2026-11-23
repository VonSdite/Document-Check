<script setup lang="ts">
import { ref } from 'vue'
import { UiButton } from '@/components/ui'
import UserDomainDialog from './UserDomainDialog.vue'
import { selectedSubjects } from './domains'

defineProps<{ url: string; csrfToken: string; nameLimit: number; domainLimit: number }>()
const open = ref(false)
const targets = ref<string[]>([])
function edit() {
  targets.value = [...selectedSubjects.value]
  open.value = true
}
</script>
<template>
  <div class="domain-bulk-bar">
    <UiButton :disabled="!selectedSubjects.length" @click="edit">批量添加领域</UiButton>
    <span class="muted" aria-live="polite">已选 {{ selectedSubjects.length }} 人</span>
    <UiButton
      v-if="selectedSubjects.length"
      size="small"
      variant="ghost"
      @click="selectedSubjects = []"
    >
      清除选择
    </UiButton>
    <UserDomainDialog
      v-model:open="open"
      append
      :label="`已选 ${targets.length} 位用户`"
      :subjects="targets"
      :initial="[]"
      :url="url"
      :csrf-token="csrfToken"
      :name-limit="nameLimit"
      :domain-limit="domainLimit"
      @saved="selectedSubjects = []"
    />
  </div>
</template>
