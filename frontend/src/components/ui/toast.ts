import { reactive } from 'vue'
export const notifications = reactive<{ id: number; message: string; category: string }[]>([])
let sequence = 0
export function dismissToast(id: number) {
  const index = notifications.findIndex((item) => item.id === id)
  if (index >= 0) notifications.splice(index, 1)
}
export function showToast(message: string, category = 'success') {
  const id = ++sequence
  notifications.push({ id, message, category })
  window.setTimeout(() => dismissToast(id), 5000)
}
