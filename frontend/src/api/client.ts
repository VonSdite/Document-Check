export interface UserProfile {
  subject: string
  label: string
  avatar: string
  version: string
}

function syncProfile(response: Response) {
  const raw = response.headers.get('X-User-Profile')
  if (!raw) return
  try {
    const profile = JSON.parse(raw) as UserProfile
    const identity = document.querySelector<HTMLElement>('.nav-identity')
    if (!identity || profile.subject !== identity.dataset.subject) return
    if (BigInt(profile.version) < BigInt(identity.dataset.profileVersion || '0')) return
    identity.textContent = profile.label
    identity.title = profile.label
    identity.dataset.profileVersion = profile.version
    window.dispatchEvent(new CustomEvent('document-check:profile', { detail: profile }))
  } catch {
    // 无效的资料头不影响本次业务响应。
  }
}

/** 同源业务请求统一处理会话、返回地址和用户资料。 */
export async function request(input: string | URL | Request, options: RequestInit = {}) {
  const url = new URL(input instanceof Request ? input.url : input, window.location.href)
  if (url.origin !== new URL(window.location.href).origin) {
    throw new Error('业务请求需要使用本站地址。')
  }
  const headers = new Headers(
    options.headers ?? (input instanceof Request ? input.headers : undefined),
  )
  headers.set('X-Requested-With', 'fetch')
  headers.set('X-Return-To', window.location.pathname + window.location.search)
  const response = await fetch(input, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
    headers,
  })
  const loginUrl = response.headers.get('X-Login-URL')
  if (response.status === 401 && loginUrl) {
    window.location.assign(loginUrl)
    throw new Error('登录已过期，正在前往登录页面。')
  }
  if (response.ok) syncProfile(response)
  return response
}

export async function postJson<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await request(url, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  const data = await response.json()
  if (!response.ok || data.ok === false) throw new Error(data.error || '操作失败，请重试。')
  return data as T
}
