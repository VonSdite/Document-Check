import { ref } from 'vue'
export const theme = ref(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light')
export function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = theme.value
  try {
    localStorage.setItem('theme', theme.value)
  } catch {
    /* 当前主题继续生效。 */
  }
}
