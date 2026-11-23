import { request } from './client'

export interface ProductDomain {
  id: number
  name: string
}

export interface UserDomainAssignment {
  subject: string
  domains: ProductDomain[]
}

export async function addUserDomains(
  url: string,
  csrfToken: string,
  subjects: string[],
  domains: string[],
  signal: AbortSignal,
): Promise<{ users: UserDomainAssignment[]; available_domains: ProductDomain[] }> {
  const form = new FormData()
  form.set('action', 'add_domains')
  form.set('csrf_token', csrfToken)
  subjects.forEach((subject) => form.append('subjects', subject))
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
    !Array.isArray(data?.users) ||
    data.users.length !== subjects.length ||
    !data.users.every(
      (user: UserDomainAssignment) =>
        subjects.includes(user.subject) && Array.isArray(user.domains),
    ) ||
    !Array.isArray(data?.available_domains)
  ) {
    throw new Error(data?.error || '批量添加领域失败，请刷新后重试。')
  }
  return data
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
