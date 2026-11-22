export function formatApiErrorDetail(detail, status) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (!item || typeof item !== 'object') return String(item)
        const loc = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== 'body').join('.')
          : ''
        const msg = item.msg || item.message || JSON.stringify(item)
        return loc ? `${loc}: ${msg}` : msg
      })
      .join('；')
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
    try {
      return JSON.stringify(detail)
    } catch {
      return `请求失败（${status}）`
    }
  }
  return `请求失败（${status}）`
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(formatApiErrorDetail(detail, status))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData
  const hasBody = options.body !== undefined && options.body !== null
  const headers = isFormData || !hasBody
    ? { ...options.headers }
    : { 'Content-Type': 'application/json', ...options.headers }
  const response = await fetch(path, {
    ...options,
    headers,
  })
  const text = await response.text().catch(() => '')
  let body = {}
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = {}
    }
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : (text && !text.trim().startsWith('<') ? text.slice(0, 300) : '')
    throw new ApiError(response.status, detail)
  }
  return body
}

function filenameFromHeader(headerValue, fallback) {
  if (!headerValue) return fallback
  const utf8Match = headerValue.match(/filename\*=UTF-8''([^;\n]+)/i)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      return fallback
    }
  }
  const plainMatch = headerValue.match(/filename="?([^";\n]+)"?/i)
  return plainMatch?.[1] || fallback
}

export async function downloadFile(path, fallbackFilename = 'download') {
  const response = await fetch(path)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body?.detail)
  }
  const blob = await response.blob()
  const filename = filenameFromHeader(response.headers.get('Content-Disposition'), fallbackFilename)
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  } finally {
    URL.revokeObjectURL(url)
  }
  return filename
}
