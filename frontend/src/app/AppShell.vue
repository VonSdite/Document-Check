<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watchEffect } from 'vue'
import { UiButton, UiDialog, UiIcon } from '@/components/ui'
import type { UserProfile } from '@/api/client'
import type { NavigationProps } from './navigation'
import AppNavigation from './AppNavigation.vue'
import { theme, toggleTheme } from './theme'
const props = defineProps<NavigationProps>()
const query = window.matchMedia('(max-width: 900px)')
const mobile = ref(query.matches)
const collapsed = ref(false)
const menuOpen = ref(false)
const identity = ref(props.identity)
const avatar = ref(props.avatar)
const version = ref(props.version)
const current = computed(() => props.items.find((item) => item.active)?.title || '文档检查')
try {
  collapsed.value = localStorage.getItem('document-check:sidebar-collapsed') === '1'
} catch {
  /* 使用默认布局。 */
}
function resize() {
  mobile.value = query.matches
  menuOpen.value = false
}
function collapse() {
  collapsed.value = !collapsed.value
  try {
    localStorage.setItem('document-check:sidebar-collapsed', collapsed.value ? '1' : '0')
  } catch {
    /* 使用当前布局。 */
  }
}
function profile(event: Event) {
  const data = (event as CustomEvent<UserProfile>).detail
  if (data.subject !== props.subject || BigInt(data.version) < BigInt(version.value)) return
  identity.value = data.label
  avatar.value = data.avatar
  version.value = data.version
}
query.addEventListener('change', resize)
window.addEventListener('document-check:profile', profile)
watchEffect(() => {
  document.documentElement.dataset.sidebar = mobile.value
    ? 'mobile'
    : collapsed.value
      ? 'collapsed'
      : 'expanded'
})
onBeforeUnmount(() => {
  query.removeEventListener('change', resize)
  window.removeEventListener('document-check:profile', profile)
})
</script>
<template>
  <a href="#main-content" class="app-skip-link">跳转到内容</a>
  <aside v-if="!mobile" class="app-sidebar">
    <div class="app-sidebar-header">
      <a v-if="!collapsed" :href="home" class="app-brand">
        <span class="app-brand-mark"><UiIcon name="document" /></span>
        <span>
          <strong>文档智能门禁</strong>
          <small>文档检查工作台</small>
        </span>
      </a>
      <UiButton
        variant="ghost"
        :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
        :aria-expanded="!collapsed"
        @click="collapse"
      >
        <UiIcon :name="collapsed ? 'expand' : 'collapse'" />
      </UiButton>
    </div>
    <div v-if="!collapsed" class="app-navigation-label">工作空间</div>
    <AppNavigation :items="items" :collapsed="collapsed" />
    <footer class="app-sidebar-footer">
      <div class="app-account">
        <img
          v-if="avatar"
          class="nav-avatar"
          :src="avatar"
          alt=""
          referrerpolicy="no-referrer"
          @error="avatar = ''"
        />
        <span v-else class="app-avatar">{{ identity.slice(0, 1) || '文' }}</span>
        <div v-show="!collapsed" class="app-identity">
          <span
            class="nav-identity"
            :title="identity"
            :data-subject="subject"
            :data-profile-version="version"
          >
            {{ identity }}
          </span>
          <small>{{ role || '用户工作区' }}</small>
        </div>
      </div>
      <div class="app-sidebar-tools">
        <UiButton
          variant="ghost"
          aria-label="切换主题"
          :title="theme === 'dark' ? '切换为浅色模式' : '切换为深色模式'"
          @click="toggleTheme"
        >
          <UiIcon :name="theme === 'dark' ? 'sun' : 'moon'" />
        </UiButton>
        <form v-if="logout" class="logout-form" method="post" :action="logout">
          <UiButton variant="ghost" type="submit">退出</UiButton>
        </form>
      </div>
    </footer>
  </aside>
  <header v-else class="app-mobile-header">
    <UiButton
      variant="ghost"
      aria-label="打开导航"
      :aria-expanded="menuOpen"
      @click="menuOpen = true"
    >
      <UiIcon name="menu" />
    </UiButton>
    <span>{{ current }}</span>
    <UiButton variant="ghost" aria-label="切换主题" @click="toggleTheme">
      <UiIcon :name="theme === 'dark' ? 'sun' : 'moon'" />
    </UiButton>
  </header>
  <UiDialog v-if="mobile" v-model:open="menuOpen" title="文档智能门禁" placement="left">
    <AppNavigation :items="items" />
    <div class="app-mobile-account">
      <span
        class="nav-identity"
        :title="identity"
        :data-subject="subject"
        :data-profile-version="version"
      >
        {{ identity }}
      </span>
      <form v-if="logout" method="post" :action="logout">
        <UiButton type="submit">退出</UiButton>
      </form>
    </div>
  </UiDialog>
</template>
