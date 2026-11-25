export const BUDGET_META = {
  max_rules_per_table: {
    label: '单表规则数量上限',
    desc: 'AI 生成的显性 + 拓展规则总数不得超过此上限',
    unit: '条 / 表',
    min: 1,
    max: 500,
  },
  max_cases_per_table: {
    label: '每张报表的测试数据上限',
    desc: '正常与异常测试数据的总行数上限',
    unit: '行 / 表',
    min: 1,
    max: 20000,
  },
  max_retries: {
    label: '失败后的自动修正次数',
    desc: '验证发现误拦截或漏拦截时，允许自动修正的最大轮数',
    unit: '轮',
    min: 0,
    max: 20,
  },
  max_llm_concurrency: {
    label: 'AI 同时处理任务数',
    desc: '同一阶段允许并行处理的 AI 任务数量',
    unit: '个',
    min: 1,
    max: 32,
  },
  max_llm_rpm: {
    label: '每分钟最大请求数',
    desc: '客户端平滑限流上限，遇 429 限流时自动全局冷却（0 表示不限速）',
    unit: '次 / 分钟',
    min: 0,
    max: 6000,
  },
  max_calls_per_table: {
    label: '单表 AI 调用上限',
    desc: '两级预算隔离上限：单张报表或内容块超出后仅隔离本表并归还父预算（0 表示不限制）',
    unit: '次 / 表',
    min: 0,
    max: 500,
  },
  max_llm_calls: {
    label: '单批次 AI 调用上限',
    desc: '内核 BudgetGuard 熔断保护；超过后自动停止并保留检查点（0 表示仅统计不熔断）',
    unit: '次 / 批次',
    min: 0,
    max: 10000,
  },
  max_total_tokens: {
    label: '单批次 Token 上限',
    desc: '累计输入+输出 Token 超过上限后自动熔断，防止异常循环空烧费用（0 表示不限制）',
    unit: 'Tokens',
    min: 0,
    max: 100000000,
  },
  max_elapsed_seconds: {
    label: '单批次最长计算耗时',
    desc: '不含人工确认等待时间；纯计算耗时超过上限后自动熔断（0 表示不限制）',
    unit: '秒',
    min: 0,
    max: 86400,
  },
}

export function validateBudgetInput(budget) {
  const normalized = {}
  for (const [key, raw] of Object.entries(budget || {})) {
    const meta = BUDGET_META[key]
    if (!meta) continue
    if (raw === '' || raw === null || raw === undefined) {
      return { ok: false, error: `「${meta.label}」不能为空，请填写 ${meta.min} ~ ${meta.max} 之间的整数。` }
    }
    if (typeof raw === 'boolean') {
      return { ok: false, error: `「${meta.label}」必须是 ${meta.min} ~ ${meta.max} 之间的整数。` }
    }
    const num = Number(raw)
    if (!Number.isInteger(num) || num < meta.min || num > meta.max) {
      return { ok: false, error: `「${meta.label}」必须是 ${meta.min} ~ ${meta.max} 之间的整数。` }
    }
    normalized[key] = num
  }
  return { ok: true, value: normalized }
}
