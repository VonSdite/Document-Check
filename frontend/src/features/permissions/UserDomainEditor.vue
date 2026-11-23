<script setup lang="ts">
import { computed, ref } from 'vue'
import { UiButton } from '@/components/ui'
import type { ProductDomain } from '@/api/user-domains'
import { assignedDomains } from './domains'
import UserDomainDialog from './UserDomainDialog.vue'

const props = defineProps<{
  subject: string
  label: string
  initial: ProductDomain[]
  url: string
  csrfToken: string
  nameLimit: number
  domainLimit: number
}>()
const saved = computed(() => assignedDomains.get(props.subject) ?? props.initial)
const open = ref(false)
</script>
<template>
  <div class="user-domain-editor">
    <div class="user-domain-tags">
      <span v-for="domain in saved" :key="domain.id" class="user-domain-tag">
        {{ domain.name }}
      </span>
      <span v-if="!saved.length" class="muted">未标记</span>
    </div>
    <UiButton size="small" variant="ghost" :aria-label="`${label}：编辑领域`" @click="open = true">
      {{ saved.length ? '编辑领域' : '设置领域' }}
    </UiButton>
    <UserDomainDialog
      v-model:open="open"
      :label="label"
      :subjects="[subject]"
      :initial="saved.map((domain) => domain.name)"
      :url="url"
      :csrf-token="csrfToken"
      :name-limit="nameLimit"
      :domain-limit="domainLimit"
    />
  </div>
</template>
