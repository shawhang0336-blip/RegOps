export const emptyField = {
  field_cn: '',
  field_en: '',
  field_definition: '',
  check_text_raw: '',
  data_type: 'VARCHAR',
  length: 128,
  decimal_places: 0,
  required: false,
  code_reference_text: '',
  confidence: 1,
  evidence_json: { source: 'HUMAN' },
}

export const TYPE_BASIS_META = {
  SOURCE: { label: '原文有依据', tone: 'green' },
  INFERRED: { label: '类型是推断', tone: 'amber' },
  MANUAL: { label: '人工确认', tone: 'blue' },
  UNKNOWN: { label: '待确认', tone: 'neutral' },
}

export function typeBasisMeta(basis) {
  return TYPE_BASIS_META[String(basis || 'UNKNOWN').toUpperCase()] || TYPE_BASIS_META.UNKNOWN
}

export function tableGate(table, item) {
  if (!item) return { key: 'pending', label: '评估中', tone: 'blue', reason: '正在评估生成条件' }
  let reason = ''
  if (table.status === 'NEEDS_REVIEW') reason = `${table.review_blocking_count || 1} 项字段问题需要处理`
  else if (!item.ddl?.ready) reason = item.ddl?.blockers?.[0]?.message || '表结构暂不能生成'
  else if (!item.rule_materials?.ready) reason = item.rule_materials?.blockers?.[0]?.message || '校验信息不完整'
  else if ((item.code_sets?.defects || []).length) reason = item.code_sets.defects[0]?.message || '关联码表需要补充'
  if (reason) return { key: 'blocked', label: '需处理', tone: 'red', reason }
  return { key: 'ready', label: '可生成', tone: 'green', reason: '' }
}
