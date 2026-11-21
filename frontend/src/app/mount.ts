import { createApp, type Component } from 'vue'
const mounted = new WeakMap<Element, ReturnType<typeof createApp>>()
export function mountComponent(
  root: HTMLElement,
  component: Component,
  props: Record<string, unknown> = {},
) {
  if (mounted.has(root)) return
  const app = createApp(component, props)
  app.mount(root)
  mounted.set(root, app)
  root.dataset.componentReady = 'true'
}
export function unmountComponent(root: HTMLElement) {
  mounted.get(root)?.unmount()
  mounted.delete(root)
}
