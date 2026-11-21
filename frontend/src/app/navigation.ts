export interface NavigationItem {
  label: string
  title: string
  href: string
  active: boolean
  icon: string
}
export interface NavigationProps {
  home: string
  items: NavigationItem[]
  identity: string
  subject: string
  version: string
  avatar: string
  role: string
  logout: string
}
const icons: Record<string, string> = {
  概览: 'overview',
  单文档: 'document',
  多文档: 'compare',
  跨语种: 'language',
  图片: 'image',
  视频: 'video',
  模型: 'models',
  'IP 用户': 'users',
  用户权限: 'shield',
  设置: 'settings',
  规则: 'shield',
}
export function readNavigation(root: HTMLElement): NavigationProps {
  const identity = root.querySelector<HTMLElement>('.nav-identity')!
  return {
    home: root.querySelector<HTMLAnchorElement>('.brand')?.getAttribute('href') || '/',
    items: Array.from(root.querySelectorAll<HTMLAnchorElement>('.nav a')).map((link) => ({
      label: link.textContent?.trim() || '',
      title: link.title,
      href: link.getAttribute('href') || '/',
      active: link.classList.contains('active'),
      icon: icons[link.textContent?.trim() || ''] || 'document',
    })),
    identity: identity.textContent?.trim() || '',
    subject: identity.dataset.subject || '',
    version: identity.dataset.profileVersion || '0',
    avatar: root.querySelector<HTMLImageElement>('.nav-avatar')?.getAttribute('src') || '',
    role: root.querySelector('.admin-tag')?.textContent?.trim() || '',
    logout: root.querySelector<HTMLFormElement>('.logout-form')?.getAttribute('action') || '',
  }
}
