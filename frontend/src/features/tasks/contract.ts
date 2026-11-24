export interface SelectOption {
  value: string
  label: string
  badge?: string
}
export interface CheckProfile {
  id: string
  name: string
  description: string
  checks: { id: number; name: string; description: string; default_selected: number }[]
}
export interface TaskComposerProps {
  profiles?: CheckProfile[]
  profile?: string
  action: string
  attributes: Record<string, string>
  files: {
    name: string
    label: string
    accept: string
    multiple: boolean
    required: boolean
    limit: number
    help: string
    initialFiles: File[]
  }[]
  models: SelectOption[]
  model: string
  modelLabel: string
  rounds: SelectOption[]
  round: string
  checks: { value: string; label: string; help: string }[]
  selectedChecks: string[]
  checkLabel: string
  checkHelp: string
  hidden: { name: string; value: string }[]
}

/** 从服务端表单读取原生值，保留挂载前的输入和权限过滤结果。 */
export function readTaskComposer(root: HTMLElement): TaskComposerProps {
  const form = root.querySelector('form')!
  const model = form.querySelector<HTMLSelectElement>('[name=model_id]')!
  const rounds = form.querySelector<HTMLSelectElement>('[name=check_rounds]')
  const profile = form.querySelector<HTMLSelectElement>('[name=profile_id]')
  const profileData = form.querySelector('[data-check-profiles]')
  const options = (select: HTMLSelectElement) =>
    Array.from(select.options).map((option) => ({
      value: option.value,
      label: option.dataset.modelLabel || option.textContent?.trim() || '',
      badge: option.dataset.thinkingDisabled === 'true' ? '关闭思考' : undefined,
    }))
  return {
    profiles: profileData
      ? (JSON.parse(profileData.textContent || '[]') as CheckProfile[])
      : undefined,
    profile: profile?.value,
    action: form.getAttribute('action') || '',
    attributes: Object.fromEntries(
      Array.from(form.attributes)
        .filter((attr) => attr.name.startsWith('data-'))
        .map((attr) => [attr.name, attr.value]),
    ),
    files: Array.from(form.querySelectorAll<HTMLInputElement>('input[type=file]')).map((input) => {
      const field = input.closest('.form-field')!
      return {
        name: input.name,
        label: field.querySelector('.field-label')?.textContent?.trim() || '文档',
        accept: input.accept,
        multiple: input.multiple,
        required: input.required,
        limit: Number(input.dataset.fileLimit || 0),
        help: field.querySelector<HTMLElement>('[data-tip]')?.dataset.tip || '',
        initialFiles: Array.from(input.files || []),
      }
    }),
    models: options(model),
    model: model.value,
    modelLabel: model.closest('label')?.firstChild?.textContent?.trim() || '模型',
    rounds: rounds ? options(rounds) : [],
    round: rounds?.value || '',
    checks: Array.from(form.querySelectorAll<HTMLInputElement>('[name=checks]')).map((input) => ({
      value: input.value,
      label: input.nextElementSibling?.textContent?.trim() || '',
      help: input.closest('label')?.querySelector<HTMLElement>('[data-tip]')?.dataset.tip || '',
    })),
    selectedChecks: Array.from(
      form.querySelectorAll<HTMLInputElement>('[name=checks]:checked'),
    ).map((input) => input.value),
    checkLabel: form.querySelector('.check-row-title > span')?.textContent?.trim() || '检查项',
    checkHelp: form.querySelector<HTMLElement>('.check-row-title [data-tip]')?.dataset.tip || '',
    hidden: Array.from(form.querySelectorAll<HTMLInputElement>('input[type=hidden]')).map(
      (input) => ({ name: input.name, value: input.value }),
    ),
  }
}
