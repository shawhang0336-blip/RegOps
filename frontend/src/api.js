export function v2(path, options = {}) {
  return api(`/api/v2${path}`, options)
}

export class ApiError extends Error {
  constructor(status, detail) {
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || (detail ? JSON.stringify(detail) : '') || `请求失败（${status}）`
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
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
    throw new ApiError(response.status, body.detail)
  }
  return body
}
