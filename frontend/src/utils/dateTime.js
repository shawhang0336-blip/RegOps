const NAIVE_ISO_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

/**
 * 后端 ORM 均使用 `datetime.utcnow()` 存储并输出无时区后缀的 UTC ISO 字符串；
 * 若不补 `Z`，浏览器会误按本地时间解析，导致东八区所有展示时间整体慢 8 小时。
 */
export function parseBackendDate(iso) {
  if (!iso) return null
  if (iso instanceof Date) return Number.isNaN(iso.getTime()) ? null : iso
  const raw = String(iso).trim()
  if (!raw) return null
  const normalized = NAIVE_ISO_RE.test(raw) ? `${raw.replace(' ', 'T')}Z` : raw
  const d = new Date(normalized)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatTime(iso, options = {}) {
  if (!iso) return '—'
  try {
    const d = parseBackendDate(iso)
    if (!d) return iso
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      ...options,
    })
  } catch {
    return iso
  }
}
