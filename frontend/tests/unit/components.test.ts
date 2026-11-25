import { afterEach, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { UiCheckPicker, UiSecretInput, UiSelect } from '@/components/ui'
import { mergeFiles } from '@/components/ui/files'
import { readTaskComposer } from '@/features/tasks/contract'

afterEach(() => {
  document.body.innerHTML = ''
})
it('多次选择文件按来源去重并执行数量上限', () => {
  const first = new File(['one'], 'a.txt', { lastModified: 1 })
  const repeated = new File(['one'], 'a.txt', { lastModified: 1 })
  const second = new File(['two'], 'b.txt', { lastModified: 2 })
  expect(mergeFiles([first], [repeated, second])).toEqual({ files: [repeated, second], omitted: 0 })
  expect(mergeFiles([first], [second], 1)).toEqual({ files: [first], omitted: 1 })
})
it('密码控件切换可见性仍通过原生表单提交', async () => {
  const form = document.createElement('form')
  document.body.append(form)
  const wrapper = mount(UiSecretInput, {
    attachTo: form,
    props: { attributes: { name: 'api_key', autocomplete: 'new-password' }, value: 'example-only' },
  })
  expect(new FormData(form).get('api_key')).toBe('example-only')
  await wrapper.get('button').trigger('click')
  expect(wrapper.get('input').attributes('type')).toBe('text')
  await wrapper.get('input').setValue('updated')
  expect(new FormData(form).get('api_key')).toBe('updated')
  wrapper.unmount()
})
it('服务器提供的标签作为文本显示', () => {
  const text = '<img src=x onerror=alert(1)>{{ globalThis.test = true }}'
  const wrapper = mount(UiSelect, {
    props: { modelValue: 'a', name: 'model_id', options: [{ value: 'a', label: text }] },
  })
  expect(wrapper.get('option').text()).toBe(text)
  expect(wrapper.find('img').exists()).toBe(false)
  wrapper.unmount()
})
it('挂载协议保留用户当前选择和提交令牌', () => {
  const root = document.createElement('div')
  root.innerHTML =
    '<form data-require-checks="true"><input type="hidden" name="submission_token" value="page-token"><label>模型<select name="model_id"><option value="first">A</option><option value="second">B</option></select></label><div class="check-row-title"><span>检查项</span></div><label><input name="checks" type="checkbox" value="one"><span>检查一</span></label></form>'
  root.querySelector<HTMLSelectElement>('select')!.value = 'second'
  root.querySelector<HTMLInputElement>('[name=checks]')!.checked = true
  const props = readTaskComposer(root)
  expect(props.model).toBe('second')
  expect(props.selectedChecks).toEqual(['one'])
  expect(props.hidden).toEqual([{ name: 'submission_token', value: 'page-token' }])
})

it('方案只读检查项通过隐藏字段完整提交，挂载时忽略重复字段', () => {
  const root = document.createElement('div')
  root.innerHTML =
    '<form><label>模型<select name="model_id"><option value="model">A</option></select></label><input type="hidden" name="submission_token" value="token"></form>'
  document.body.append(root)
  const form = root.querySelector('form')!
  const wrapper = mount(UiCheckPicker, {
    attachTo: form,
    props: {
      modelValue: ['1', '2'],
      readonly: true,
      label: '检查项',
      options: [
        { value: '1', label: '检查一', help: '' },
        { value: '2', label: '检查二', help: '' },
      ],
    },
  })
  expect(new FormData(form).getAll('checks')).toEqual(['1', '2'])
  expect(
    wrapper
      .findAll<HTMLInputElement>('input[type=checkbox]')
      .every((input) => input.element.checked && input.element.disabled),
  ).toBe(true)
  const props = readTaskComposer(root)
  expect(props.checks.map((item) => item.value)).toEqual(['1', '2'])
  expect(props.selectedChecks).toEqual(['1', '2'])
  expect(props.hidden).toEqual([{ name: 'submission_token', value: 'token' }])
  wrapper.unmount()
})
