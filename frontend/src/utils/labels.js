/** 全站领域状态词典、字段/问题文案映射与错误提示归一化（纯数据/纯函数，零 React 依赖）。 */

export const STATUS_META = {
  PUBLISHED: { label: '已发布', tone: 'green' },
  DRAFT: { label: '待核对', tone: 'amber' },
  AI_SELF_VALIDATED: { label: '自动检查完成', tone: 'blue' },
  NEEDS_REVIEW: { label: '需要核对', tone: 'red' },
  RAW_EXTRACTED: { label: '已识别内容', tone: 'blue' },
  OVERVIEW_REQUIRED: { label: '待检查结构', tone: 'amber' },
  OVERVIEW_REVIEWED: { label: '结构检查完成', tone: 'green' },
  OVERVIEW_NEEDS_REVIEW: { label: '结构需要确认', tone: 'amber' },
  OVERVIEW_FAILED: { label: '结构检查未完成', tone: 'red' },
  STRUCTURE_REVIEWED: { label: '结构检查完成', tone: 'green' },
  UPLOADED: { label: '等待结构检查', tone: 'amber' },
  BLOCK_EXTRACTION_FAILED: { label: '初步切块未完成', tone: 'red' },
  EXTRACTING: { label: '正在提取', tone: 'blue' },
  EXTRACTED: { label: '提取完成', tone: 'green' },
  EXTRACTION_PARTIAL: { label: '部分完成', tone: 'amber' },
  EXTRACTION_FAILED: { label: '提取未完成', tone: 'red' },
  COMPLETED: { label: '验证通过', tone: 'green' },
  WAITING_APPROVAL: { label: '等待规则确认', tone: 'amber' },
  WAITING_TEST_APPROVAL: { label: '等待测试确认', tone: 'amber' },
  RUNNING: { label: '正在生成', tone: 'blue' },
  AUDITING: { label: '正在验证', tone: 'blue' },
  READY_TO_AUDIT: { label: '等待验证', tone: 'blue' },
  FAILED: { label: '需要处理', tone: 'red' },
  APPROVED: { label: '已批准', tone: 'green' },
  REJECTED: { label: '已拒绝', tone: 'red' },
  PENDING: { label: '待确认', tone: 'amber' },
  PASSED: { label: '校验通过', tone: 'green' },
  ERROR: { label: '错误阻断', tone: 'red' },
  WARNING: { label: '复核提示', tone: 'amber' },
  RESOLVED: { label: '码值已就绪', tone: 'green' },
  UNRESOLVED: { label: '需要补充码值', tone: 'red' },
  DOC_EXPLICIT: { label: '文档明确规则', tone: 'blue' },
  AI_EXPANDED: { label: 'AI 建议新增', tone: 'violet' },
  MANUAL: { label: '人工维护', tone: 'neutral' },
  DOCUMENT_REFERENCE: { label: '从文档识别', tone: 'blue' },
  PROGRAM: { label: '程序检查', tone: 'blue' },
  AI_REVIEW: { label: 'AI 复核', tone: 'violet' },
}

export const REVISION_LABELS = {
  AI_EXTRACTION: '自动识别',
  AI_REVIEW: 'AI 复核',
  HUMAN_EDIT: '人工修改',
  HUMAN_ADD: '新增字段',
  HUMAN_DELETE: '删除字段',
  DRAFT_CREATED: '创建修改版本',
  PUBLISHED: '发布',
}

export const FIELD_LABELS = {
  field_cn: '中文名称',
  field_en: '英文列名',
  data_type: '数据类型',
  length: '字段长度',
  decimal_places: '小数位数',
  required: '是否必填',
  code_reference_text: '码值表引用',
  field_definition: '字段业务定义',
  check_text_raw: '原始校验规则',
  confidence: 'AI 置信度',
}

export const BLOCKER_REASONS = {
  CODE_REFERENCE_NOT_BOUND: '字段引用的码值表尚未发布，请先维护并发布码值',
  REQUIRED_CODE_SET_UNRESOLVED: '必填字段缺少可用的码值，请补充并发布码值表',
}

export const ISSUE_LABELS = {
  AI_REVIEW_UNAVAILABLE: 'AI 复核暂不可用',
  AI_REVIEW_FALLBACK: '已使用程序交叉检查',
  LOW_CONFIDENCE: '识别置信度较低',
  MISSING_EVIDENCE: '缺少原文依据',
  SOURCE_PARSE_REVIEW_REQUIRED: '来源内容需要核对',
  TYPE_MISMATCH: '字段类型与原文不一致',
  TYPE_LENGTH_MISMATCH: '类型与长度都与原文不一致',
  LENGTH_MISMATCH: '字段长度与原文不一致',
  MISSING_OPERATOR: '校验条件缺少运算符',
  MISSING_ENUM: '原文列出的取值未录入',
  MISSING_COND_REQUIRED: '条件必填缺少说明',
  COND_REQUIRED_MISSING: '条件必填缺少说明',
  CODE_REF_MISSING: '码值表引用缺失',
  MISSING_FIELD: '原文提及的字段未识别',
}

// AI 复核给出的问题码是自由文本，实际有几百种。按前缀归到人能手判的类别，
// 比直接把 TYPE_MISMATCH_B0615 这种原始码丢给用户看要有用得多。
const ISSUE_FAMILIES = [
  [/^(TYPE_|VALUE_TYPE)/, '字段类型与原文不一致'],
  [/^(LENGTH_|UNVERIFIED_LENGTH|UNDOCUMENTED_LENGTH|TYPE_LENGTH)/, '字段长度需确认'],
  [/^MISSING_/, '原文要求未落到字段上'],
  [/^(COND_|REQUIRED_|UNRESOLVED_COND)/, '条件必填规则待确认'],
  [/^(CODE_|ENUM|UNRESOLVED_CODE)/, '码值待确认'],
  [/^(TEXT_|SOURCE_)/, '校验原文有残留或歧义'],
  [/^(FORMAT_|PATTERN_|REGEX)/, '格式规则待确认'],
  [/^(DEFAULT_|NULL_)/, '默认值/空值规则待确认'],
  [/^UNIT_/, '计量单位未定义'],
  [/^(AI_)/, 'AI 复核说明'],
  [/CONFIDENCE/, '识别置信度较低'],
]

export function friendlyError(message) {
  const text = String(message || '操作未完成，请稍后重试')
  if (/Model returned no tool call|failed deterministic validation/i.test(text)) {
    return 'AI 复核没有返回规定的结构化结果；程序检查仍已完成，请人工核对后继续使用。'
  }
  if (/ReadTimeout|timed out|timeout/i.test(text)) {
    return 'AI 服务响应超时。已保存的内容不会丢失，请稍后重试。'
  }
  if (/Failed to fetch|NetworkError|network error/i.test(text)) {
    return '暂时无法连接服务，请检查后端服务是否已启动。'
  }
  return text
}

export function issueLabel(code) {
  const key = String(code || '').trim()
  if (!key) return '复核提示'
  if (ISSUE_LABELS[key]) return ISSUE_LABELS[key]
  for (const [pattern, label] of ISSUE_FAMILIES) {
    if (pattern.test(key)) return label
  }
  return key
}

export function issueSeverity(issue) {
  if (['AI_REVIEW_UNAVAILABLE', 'AI_REVIEW_FALLBACK'].includes(issue?.code)) return 'WARNING'
  return issue?.severity || 'WARNING'
}

export function groupValidationIssues(issues) {
  const lowConfidence = issues.filter((issue) => issue.code === 'LOW_CONFIDENCE')
  const others = issues.filter((issue) => issue.code !== 'LOW_CONFIDENCE')
  if (!lowConfidence.length) return issues
  return [
    ...others,
    {
      severity: 'WARNING',
      code: 'LOW_CONFIDENCE',
      field_en: '',
      message: `${lowConfidence.length} 个字段的识别置信度较低，请对照原文核对字段名、类型和长度。`,
    },
  ]
}
