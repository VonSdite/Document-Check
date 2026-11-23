import { request } from './client'

export interface ProductDomain {
  id: number
  name: string
}

export async function saveUserDomains(
  url: string,
  csrfToken: string,
  subject: string,
  domains: string[],
  signal: AbortSignal,
): Promise<{ subject: string; domains: ProductDomain[]; available_domains: ProductDomain[] }> {
  const form = new FormData()
  form.set('action', 'domains')
  form.set('csrf_token', csrfToken)
  form.set('subject', subject)
  domains.forEach((name) => form.append('domains', name))
  const response = await request(url, {
    method: 'POST',
    body: form,
    signal,
    headers: { Accept: 'application/json' },
  })
  const data = await response.json().catch(() => null)
  if (
    !response.ok ||
    response.redirected ||
    !Array.isArray(data?.domains) ||
    !Array.isArray(data?.available_domains)
  ) {
    throw new Error(data?.error || '保存领域失败，请刷新后重试。')
  }
  return data
}
