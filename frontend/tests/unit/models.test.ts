import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ModelEditor from '@/features/models/ModelEditor.vue'
import { normalizeChatCompletionsEndpoint } from '@/features/models/provider'
import { mergeFetched, normalizeModels } from '@/features/models/configuration'
import { modelsApi } from '@/api/models'

vi.mock('@/api/models', () => ({ modelsApi: { discover: vi.fn(), test: vi.fn() } }))
HTMLDialogElement.prototype.showModal = function () {
  this.open = true
}
HTMLDialogElement.prototype.close = function () {
  this.open = false
}
afterEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('模型配置', () => {
  it('根地址、基础路径和完整地址统一为请求端点', () => {
    for (const [input, output] of [
      ['http://127.0.0.1:31943', 'http://127.0.0.1:31943/v1/chat/completions'],
      [' https://example.test/v1/// ', 'https://example.test/v1/chat/completions'],
      ['https://example.test/proxy/v1/', 'https://example.test/proxy/v1/chat/completions'],
      [
        'https://example.test/v1/chat/completions?mode=test',
        'https://example.test/v1/chat/completions?mode=test',
      ],
    ])
      expect(normalizeChatCompletionsEndpoint(input)).toBe(output)
  })
  it('无效地址保留给用户纠正', () => {
    for (const input of [
      '',
      'example.test/v1',
      'ftp://example.test',
      'https:///v1',
      'http://[::1',
      'http://example.test:invalid/v1',
    ])
      expect(normalizeChatCompletionsEndpoint(input)).toBe(input)
  })
  it('同名的思考开关配置分别保留，重复项和空行合并', () => {
    expect(
      normalizeModels([
        { model_name: ' a ' },
        { model_name: 'a' },
        { model_name: 'a', force_disable_thinking: true },
        { model_name: '' },
      ]),
    ).toHaveLength(2)
  })
  it('拉取结果取消某项时保留未返回的模型和关闭思考配置', () => {
    const current = normalizeModels([
      { model_name: 'a' },
      { model_name: 'a', force_disable_thinking: true },
      { model_name: 'b', reasoning_effort: 'high' },
    ])
    const result = mergeFetched(current, ['a', 'c'], ['c'])
    expect(result.map((row) => [row.model_name, row.force_disable_thinking])).toEqual([
      ['a', true],
      ['b', false],
      ['c', false],
    ])
    expect(result[1].reasoning_effort).toBe('high')
  })
  function editor() {
    const form = document.createElement('form')
    form.className = 'provider-modal-form'
    form.dataset.fetchModelsUrl = '/models/fetch'
    form.dataset.testModelUrl = '/models/test'
    form.dataset.testImageModelUrl = '/models/test-image'
    form.innerHTML =
      '<input name="api_base" value="https://example.test"><input name="api_key" value="example-key"><input name="request_timeout" value="600"><div id="editor"></div>'
    document.body.append(form)
    const wrapper = mount(ModelEditor, {
      attachTo: form.querySelector('#editor')!,
      props: {
        form,
        initial: [
          { model_name: 'model-a', reasoning_effort: 'high', force_disable_thinking: false },
        ],
      },
    })
    return { form, wrapper }
  }
  it('拉取与测试均发送补全后的地址和本次草稿', async () => {
    vi.mocked(modelsApi.discover).mockResolvedValue({ fetched_models: ['model-a'] })
    vi.mocked(modelsApi.test).mockResolvedValue({ ok: true, message: '测试通过' })
    const { form, wrapper } = editor()
    await wrapper.get('[data-fetch-models]').trigger('click')
    await flushPromises()
    expect(modelsApi.discover).toHaveBeenCalledWith(
      '/models/fetch',
      expect.objectContaining({
        api_base: 'https://example.test/v1/chat/completions',
        api_key: 'example-key',
      }),
      expect.any(AbortSignal),
    )
    form.querySelector<HTMLInputElement>('[name=api_base]')!.value =
      'https://example.test/proxy/v1/'
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '测试')!
      .trigger('click')
    await flushPromises()
    expect(modelsApi.test).toHaveBeenCalledWith(
      '/models/test',
      expect.objectContaining({
        api_base: 'https://example.test/proxy/v1/chat/completions',
        model_name: 'model-a',
        reasoning_effort: 'high',
      }),
      expect.any(AbortSignal),
    )
    wrapper.unmount()
  })
  it('移除组件会取消未完成的模型请求', async () => {
    vi.mocked(modelsApi.discover).mockReturnValue(new Promise(() => {}))
    const { wrapper } = editor()
    await wrapper.get('[data-fetch-models]').trigger('click')
    const signal = vi.mocked(modelsApi.discover).mock.calls[0][2]!
    expect(signal.aborted).toBe(false)
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
  })
  it('连接草稿变化会取消拉取，旧结果不弹出选择框', async () => {
    let resolve!: (value: { fetched_models: string[] }) => void
    vi.mocked(modelsApi.discover).mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const { form, wrapper } = editor()
    await wrapper.get('[data-fetch-models]').trigger('click')
    const input = form.querySelector<HTMLInputElement>('[name=api_base]')!
    input.value = 'https://another.example.test'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    resolve({ fetched_models: ['stale-model'] })
    await flushPromises()
    expect(wrapper.get('dialog').attributes('open')).toBeUndefined()
    expect(wrapper.text()).not.toContain('stale-model')
    wrapper.unmount()
  })
  it('图片识别测试单独验证，修改模型后要求重新验证', async () => {
    vi.mocked(modelsApi.test).mockResolvedValue({ ok: true, message: '图片识别测试通过。' })
    const { wrapper } = editor()
    await wrapper.get('[data-test-image-model]').trigger('click')
    await flushPromises()
    expect(modelsApi.test).toHaveBeenCalledWith(
      '/models/test-image',
      expect.objectContaining({ model_name: 'model-a' }),
      expect.any(AbortSignal),
    )
    expect(wrapper.get('[data-image-capability]').text()).toContain('可检查图片和视频')
    await wrapper.get('[data-model-name]').setValue('text-model')
    expect(wrapper.get('[data-image-capability]').text()).toContain('尚未验证')
    wrapper.unmount()
  })
  it('模型连接改变会取消图片测试，旧结果不恢复验证状态', async () => {
    let resolve!: (value: { ok: boolean; message: string }) => void
    vi.mocked(modelsApi.test).mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const { form, wrapper } = editor()
    await wrapper.get('[data-test-image-model]').trigger('click')
    const signal = vi.mocked(modelsApi.test).mock.calls[0][2]!
    const key = form.querySelector<HTMLInputElement>('[name=api_key]')!
    key.value = 'changed-key'
    key.dispatchEvent(new Event('input', { bubbles: true }))
    expect(signal.aborted).toBe(true)
    resolve({ ok: true, message: '过期结果' })
    await flushPromises()
    expect(wrapper.get('[data-image-capability]').text()).toContain('尚未验证')
    expect(wrapper.text()).not.toContain('过期结果')
    wrapper.unmount()
  })
  it('图片测试失败保留可理解的提示', async () => {
    vi.mocked(modelsApi.test).mockRejectedValue(new Error('模型未正确读取测试图片。'))
    const { wrapper } = editor()
    await wrapper.get('[data-test-image-model]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-image-capability]').text()).toContain('未通过')
    expect(wrapper.text()).toContain('模型未正确读取测试图片')
    wrapper.unmount()
  })
  it('计算图片识别状态时保留正在输入的 API 地址', async () => {
    const { form, wrapper } = editor()
    const input = form.querySelector<HTMLInputElement>('[name=api_base]')!
    expect(input.value).toBe('https://example.test')
    for (const value of ['https://a', 'https://api.', 'https://api.example.test/proxy']) {
      input.value = value
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await flushPromises()
      expect(input.value).toBe(value)
    }
    wrapper.unmount()
  })
})
