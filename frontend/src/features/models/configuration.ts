export interface ModelConfig {
  model_name: string
  reasoning_effort: string
  force_disable_thinking: boolean
}
export const efforts = ['', 'low', 'medium', 'high', 'xhigh', 'max']
export function normalizeModels(values: Partial<ModelConfig>[]): ModelConfig[] {
  const seen = new Set<string>()
  return values.flatMap((value) => {
    const name = String(value.model_name || '').trim()
    const disabled = Boolean(value.force_disable_thinking)
    const key = JSON.stringify([name, disabled])
    if (!name || seen.has(key)) return []
    seen.add(key)
    return [
      {
        model_name: name,
        force_disable_thinking: disabled,
        reasoning_effort: efforts.includes(value.reasoning_effort || '')
          ? value.reasoning_effort || ''
          : '',
      },
    ]
  })
}
export function mergeFetched(current: ModelConfig[], fetched: string[], selected: string[]) {
  const incoming = new Set(fetched)
  const selection = new Set(selected)
  return normalizeModels([
    ...current.filter(
      (item) =>
        item.force_disable_thinking ||
        !incoming.has(item.model_name) ||
        selection.has(item.model_name),
    ),
    ...selected.map((model_name) => ({
      model_name,
      reasoning_effort: '',
      force_disable_thinking: false,
    })),
  ])
}
