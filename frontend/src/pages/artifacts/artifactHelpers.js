export const GENERATION_FILTERS = [
  ['all', '全部生成状态'],
  ['ungenerated', '未生成'],
  ['running', '生成中'],
  ['verified', '已验证'],
  ['failed', '验证失败'],
]

export const READINESS_FILTERS = [
  ['all', '全部可选状态'],
  ['selectable', '可选'],
  ['blocked', '不可选'],
  ['code_issues', '码表需处理'],
]

// 规则还在生成/空跑中的状态：需要自动刷新规则列表
export const ACTIVE_RUN_STATUSES = ['NEW', 'RUNNING', 'AUDITING', 'READY_TO_AUDIT']
export const POLL_INTERVAL_MS = 5000

export const RULE_ORDER = [
  'REQUIRED',
  'ENUM',
  'COND_REQUIRED',
  'COND_VALUE',
  'LENGTH',
  'LENGTH_MAX',
  'REGEXP',
  'NOT_IN',
  'NUM_RANGE',
  'FIELD_COMPARE',
  'ARITHMETIC_BALANCE',
  'UNIQUE_KEY',
  'FOREIGN_KEY',
]

export function runTone(row) {
  if (!row.generated) return 'neutral'
  if (row.run_status === 'FAILED') return 'red'
  if (row.run_status === 'WAITING_APPROVAL') return 'amber'
  if (row.run_status === 'WAITING_TEST_APPROVAL') return 'green'
  if (row.run_status === 'COMPLETED') return row.all_passed ? 'green' : 'amber'
  return 'blue'
}

export function runLabel(row) {
  if (!row.generated) return '未生成'
  if (row.run_status === 'FAILED') return '验证失败'
  if (row.run_status === 'WAITING_APPROVAL') return '待确认规则'
  if (row.run_status === 'WAITING_TEST_APPROVAL') return '待确认测试'
  if (row.run_status === 'COMPLETED') return row.all_passed ? '已验证' : '验证未过'
  return '生成中'
}

export function ruleStatusLabel(status) {
  if (status === 'APPROVED') return '已批准'
  if (status === 'PENDING') return '待审'
  if (status === 'REJECTED') return '已拒绝'
  if (status === 'SUPERSEDED') return '已替换'
  return status || '未知'
}

export function formatValue(value) {
  if (Array.isArray(value)) {
    if (value.every((item) => item && typeof item === 'object' && 'field' in item)) {
      return value.map((item) => `${item.coef ?? 1}×${item.field}`).join(' + ')
    }
    return value.map((item) => (item !== null && typeof item === 'object' ? JSON.stringify(item) : String(item))).join('、')
  }
  if (value !== null && typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function formatParams(params, compact = false) {
  const entries = Object.entries(params || {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => {
    if (compact && Array.isArray(value) && value.length > 3) {
      return `${key}: ${value.slice(0, 3).join('、')} 等 ${value.length} 项`
    }
    return `${key}: ${formatValue(value)}`
  }).join('；')
}

export function sortRules(rows) {
  return [...rows].sort(
    (a, b) => RULE_ORDER.indexOf(a.operator) - RULE_ORDER.indexOf(b.operator) || a.field.localeCompare(b.field),
  )
}

export function groupStatusMeta(runStatus) {
  if (runStatus === 'FAILED') return ['red', '验证失败']
  if (runStatus === 'COMPLETED') return ['green', '已完成']
  if (runStatus === 'WAITING_APPROVAL') return ['amber', '待确认规则']
  if (runStatus === 'WAITING_TEST_APPROVAL') return ['green', '待确认测试']
  if (ACTIVE_RUN_STATUSES.includes(runStatus)) return ['blue', '生成中']
  return ['neutral', runStatus || '未知']
}
