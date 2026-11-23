import { afterEach, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import UserDomainBulkEditor from '@/features/permissions/UserDomainBulkEditor.vue'
import UserDomainSelection from '@/features/permissions/UserDomainSelection.vue'
import UserDomainEditor from '@/features/permissions/UserDomainEditor.vue'
import { assignedDomains, availableDomains, selectedSubjects } from '@/features/permissions/domains'
import { addUserDomains } from '@/api/user-domains'

vi.mock('@/api/user-domains', () => ({ saveUserDomains: vi.fn(), addUserDomains: vi.fn() }))
HTMLDialogElement.prototype.showModal = function () {
  this.open = true
}
HTMLDialogElement.prototype.close = function () {
  this.open = false
}
enableAutoUnmount(afterEach)
afterEach(() => {
  assignedDomains.clear()
  availableDomains.value = []
  selectedSubjects.value = []
  document.body.innerHTML = ''
  vi.resetAllMocks()
})
const options = {
  url: '/admin/permissions',
  csrfToken: 'page-token',
  nameLimit: 40,
  domainLimit: 20,
}

it('全选、部分选择和取消选择同步到每个用户，批量入口按选择启用', async () => {
  const bulk = mount(UserDomainBulkEditor, { props: options })
  const all = mount(UserDomainSelection, {
    props: { subjects: ['a', 'b'], label: '选择本页全部用户' },
  })
  const first = mount(UserDomainSelection, {
    props: { subjects: ['a'], label: '用户甲：选择用户' },
  })
  expect(bulk.get('button').attributes('disabled')).toBeDefined()
  await first.get('input').setValue(true)
  expect(all.get<HTMLInputElement>('input').element.indeterminate).toBe(true)
  expect(bulk.text()).toContain('已选 1 人')
  await all.get('input').setValue(true)
  expect(selectedSubjects.value).toEqual(['a', 'b'])
  expect(all.get<HTMLInputElement>('input').element.indeterminate).toBe(false)
  await all.get('input').setValue(false)
  expect(first.get<HTMLInputElement>('input').element.checked).toBe(false)
  expect(bulk.get('button').attributes('disabled')).toBeDefined()
})

it('批量失败保留选人和草稿，重试成功同步每行及后续单人编辑', async () => {
  selectedSubjects.value = ['a', 'b']
  const bulk = mount(UserDomainBulkEditor, { props: options, attachTo: document.body })
  const row = mount(UserDomainEditor, {
    props: { ...options, subject: 'a', label: '用户甲', initial: [{ id: 1, name: '电源' }] },
    attachTo: document.body,
  })
  await bulk.get('button').trigger('click')
  await flushPromises()
  await bulk.get('input[placeholder="输入领域名称"]').setValue('工控')
  const save = bulk.findAll('button').find((button) => button.text() === '添加到所选用户')!
  vi.mocked(addUserDomains).mockRejectedValueOnce(new Error('保存失败，请重试。'))
  await save.trigger('click')
  await flushPromises()
  expect(bulk.get('[role=alert]').text()).toContain('保存失败')
  expect(selectedSubjects.value).toEqual(['a', 'b'])
  expect(bulk.get('.domain-editor-selection').text()).toContain('工控')
  const domains = [
    { id: 1, name: '电源' },
    { id: 2, name: '工控' },
  ]
  vi.mocked(addUserDomains).mockResolvedValueOnce({
    users: [
      { subject: 'a', domains },
      { subject: 'b', domains: [domains[1]] },
    ],
    available_domains: domains,
  })
  await save.trigger('click')
  await flushPromises()
  expect(addUserDomains).toHaveBeenLastCalledWith(
    '/admin/permissions',
    'page-token',
    ['a', 'b'],
    ['工控'],
    expect.any(AbortSignal),
  )
  expect(selectedSubjects.value).toEqual([])
  expect(row.get('.user-domain-tags').text()).toContain('电源')
  expect(row.get('.user-domain-tags').text()).toContain('工控')
  await row.get('button[aria-label="用户甲：编辑领域"]').trigger('click')
  await flushPromises()
  expect(row.get('.domain-editor-selection').text()).toContain('工控')
})

it('批量提交前要求领域，取消保留选人，保存中禁止重发并取消卸载请求', async () => {
  selectedSubjects.value = ['a', 'b']
  const bulk = mount(UserDomainBulkEditor, { props: options, attachTo: document.body })
  await bulk.get('button').trigger('click')
  await flushPromises()
  const save = () => bulk.findAll('button').find((button) => button.text() === '添加到所选用户')!
  await save().trigger('click')
  expect(bulk.get('[role=alert]').text()).toBe('请至少选择一个领域。')
  expect(addUserDomains).not.toHaveBeenCalled()
  await bulk
    .findAll('button')
    .find((button) => button.text() === '取消')!
    .trigger('click')
  await flushPromises()
  expect(selectedSubjects.value).toEqual(['a', 'b'])
  await bulk.get('button').trigger('click')
  await flushPromises()
  await bulk.get('input[placeholder="输入领域名称"]').setValue('工控')
  let resolve!: (value: Awaited<ReturnType<typeof addUserDomains>>) => void
  vi.mocked(addUserDomains).mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  await save().trigger('click')
  expect(save().attributes('disabled')).toBeDefined()
  expect(bulk.get('fieldset').attributes('disabled')).toBeDefined()
  await save().trigger('click')
  expect(addUserDomains).toHaveBeenCalledTimes(1)
  const signal = vi.mocked(addUserDomains).mock.calls[0][4]
  bulk.unmount()
  expect(signal.aborted).toBe(true)
  resolve({ users: [{ subject: 'a', domains: [{ id: 1, name: '工控' }] }], available_domains: [] })
  await flushPromises()
  expect(assignedDomains.size).toBe(0)
  expect(selectedSubjects.value).toEqual(['a', 'b'])
})
