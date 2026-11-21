import { postJson } from './client'
export interface ProviderConnection {
  api_base: string
  api_key: string
  request_timeout: string
}
export const modelsApi = {
  discover: (url: string, body: ProviderConnection, signal?: AbortSignal) =>
    postJson<{ fetched_models: string[] }>(url, body, signal),
  test: (
    url: string,
    body: ProviderConnection & {
      model_name: string
      reasoning_effort: string
      force_disable_thinking: boolean
    },
    signal?: AbortSignal,
  ) => postJson<{ ok: boolean; message?: string }>(url, body, signal),
}
