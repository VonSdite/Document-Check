import { afterEach, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import UserDomainEditor from '@/features/permissions/UserDomainEditor.vue'
import UserDomainFilter from '@/features/permissions/UserDomainFilter.vue'
import { availableDomains } from '@/features/permissions/domains'
import { saveUserDomains } from '@/api/user-domains'

vi.mock('@/api/user-domains', () => ({ saveUserDomains: vi.fn() }))
HTMLDialogElement.prototype.showModal = function () {
  this.open = true
}
HTMLDialogElement.prototype.close = function () {
  this.open = false
}
enableAutoUnmount(afterEach)
afterEach(() => {
  availableDomains.value = []
  document.body.innerHTML = ''
  vi.resetAllMocks()
})

async function editor() {
  availableDomains.value = [{ id: 1, name: '电源' }]
  const wrapper = mount(UserDomainEditor, {
    attachTo: document.body,
    props: {
      subject: 'cookie_session:user-a',
      label: '用户甲',
      initial: [{ id: 1, name: '电源' }],
      url: '/admin/permissions',
      csrfToken: 'page-token',
      nameLimit: 40,
      domainLimit: 20,
    },
  })
  await wrapper.get('button[aria-label="用户甲：编辑产品领域"]').trigger('click')
  await flushPromises()
  return wrapper
}

it('保存失败保留已存标签和草稿，重试成功后更新共享目录', async () => {
  const wrapper = await editor()
  const filter = mount(UserDomainFilter, { props: { selected: '' } })
  await wrapper.get('input[placeholder="输入领域名称"]').setValue(' 工控 ')
  const save = wrapper.findAll('button').find((button) => button.text() === '保存')!
  vi.mocked(saveUserDomains).mockRejectedValueOnce(new Error('连接中断，请重试。'))
  await save.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toBe('连接中断，请重试。')
  expect(wrapper.get('dialog').attributes('open')).toBeDefined()
  expect(wrapper.get('.domain-editor-selection').text()).toContain('工控')
  expect(wrapper.get('.user-domain-editor > .user-domain-tags').text()).toBe('电源')
  const domains = [
    { id: 1, name: '电源' },
    { id: 2, name: '工控' },
  ]
  vi.mocked(saveUserDomains).mockResolvedValueOnce({
    subject: 'cookie_session:user-a',
    domains,
    available_domains: domains,
  })
  await save.trigger('click')
  await flushPromises()
  expect(saveUserDomains).toHaveBeenLastCalledWith(
    '/admin/permissions',
    'page-token',
    'cookie_session:user-a',
    ['电源', '工控'],
    expect.any(AbortSignal),
  )
  expect(wrapper.get('dialog').attributes('open')).toBeUndefined()
  expect(wrapper.get('.user-domain-editor > .user-domain-tags').text()).toContain('工控')
  expect(filter.findAll('option').map((option) => option.text())).toEqual([
    '全部领域',
    '未标记',
    '电源',
    '工控',
  ])
})

it('取消编辑后重新打开使用已保存标签，用户输入作为文本展示', async () => {
  const wrapper = await editor()
  await wrapper.get('[aria-label="移除领域 电源"]').trigger('click')
  await wrapper.get('input[placeholder="输入领域名称"]').setValue('<img src=x onerror=alert(1)>')
  await wrapper.get('.domain-editor-add').trigger('submit')
  expect(wrapper.get('.domain-editor-selection').text()).toContain('<img src=x onerror=alert(1)>')
  expect(wrapper.find('img').exists()).toBe(false)
  await wrapper
    .findAll('button')
    .find((button) => button.text() === '取消')!
    .trigger('click')
  await flushPromises()
  await wrapper.get('button[aria-label="用户甲：编辑产品领域"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('.domain-editor-selection').text()).toContain('电源')
  expect(wrapper.get('.domain-editor-selection').text()).not.toContain('<img')
  expect(saveUserDomains).not.toHaveBeenCalled()
})

it('保存中禁用编辑和重复提交，卸载取消请求并忽略迟到结果', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof saveUserDomains>>) => void
  vi.mocked(saveUserDomains).mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  const wrapper = await editor()
  const save = wrapper.findAll('button').find((button) => button.text() === '保存')!
  await save.trigger('click')
  expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
  expect(wrapper.get('[aria-label="关闭"]').attributes('disabled')).toBeDefined()
  expect(save.attributes('disabled')).toBeDefined()
  await save.trigger('click')
  expect(saveUserDomains).toHaveBeenCalledTimes(1)
  const signal = vi.mocked(saveUserDomains).mock.calls[0][4]
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  resolve({ subject: 'cookie_session:user-a', domains: [], available_domains: [] })
  await flushPromises()
  expect(availableDomains.value).toEqual([{ id: 1, name: '电源' }])
})

it('名称与数量超过上限时显示错误并保留草稿', async () => {
  const wrapper = await editor()
  await wrapper.setProps({ nameLimit: 2, domainLimit: 1 })
  const input = wrapper.get('input[placeholder="输入领域名称"]')
  await input.setValue('新领域')
  await wrapper.get('.domain-editor-add').trigger('submit')
  expect(wrapper.get('[role=alert]').text()).toContain('领域名称最多 2 个字符')
  await input.setValue('工控')
  await wrapper
    .findAll('button')
    .find((button) => button.text() === '保存')!
    .trigger('click')
  expect(wrapper.get('[role=alert]').text()).toContain('每个用户最多设置 1 个领域')
  expect(wrapper.get('.domain-editor-selection').text()).toContain('电源')
  expect(saveUserDomains).not.toHaveBeenCalled()
})
