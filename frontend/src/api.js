export function v2(path, options = {}) {
  return api(`/api/v2${path}`, options)
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body instanceof FormData
      ? { ...options.headers }
      : { 'Content-Type': 'application/json', ...options.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(body.detail || `请求失败（${response.status}）`)
  }
  return body
}
