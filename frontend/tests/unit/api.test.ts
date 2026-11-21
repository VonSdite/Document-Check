import { afterEach, expect, it, vi } from 'vitest'
import { request } from '@/api/client'
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})
it('同源请求携带会话和返回页面，保留业务请求头', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  await request('/models/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  const options = fetcher.mock.calls[0][1]
  expect(options.credentials).toBe('same-origin')
  expect(options.headers.get('X-Requested-With')).toBe('fetch')
  expect(options.headers.get('Content-Type')).toBe('application/json')
  expect(options.headers.get('X-Return-To')).toBe('/')
})
it('阻止将业务凭据发往其他站点', async () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  await expect(request('https://external.example.test/models', { body: 'secret' })).rejects.toThrow(
    '本站地址',
  )
  expect(fetcher).not.toHaveBeenCalled()
})
it('资料更新忽略较旧的版本和不同用户', async () => {
  document.body.innerHTML =
    '<span class="nav-identity" data-subject="user:one" data-profile-version="4">current</span>'
  const identity = document.querySelector<HTMLElement>('.nav-identity')!
  for (const [subject, version, label, expected] of [
    ['user:one', '3', 'old', 'current'],
    ['user:two', '5', 'other', 'current'],
    ['user:one', '5', 'latest', 'latest'],
  ]) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('{}', {
          headers: { 'X-User-Profile': JSON.stringify({ subject, version, label, avatar: '' }) },
        }),
      ),
    )
    await request('/task-statuses')
    expect(identity.textContent).toBe(expected)
  }
})
