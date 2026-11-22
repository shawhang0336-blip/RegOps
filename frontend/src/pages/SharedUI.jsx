import { useEffect, useRef, useState } from 'react'
import { formatTime, parseBackendDate } from '../utils/dateTime.js'

export { formatTime, parseBackendDate }

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

export function StatusBadge({ status, children, tone }) {
  const meta = STATUS_META[status] || { label: status || '未知', tone: 'neutral' }
  const finalTone = tone || meta.tone
  return <span className={`badge badge-${finalTone}`}>{children ?? meta.label}</span>
}

export function SourceLocatorTag({ locator }) {
  if (!locator || typeof locator !== 'object') return <span className="muted-text">—</span>
  const tags = []
  if (locator.section) tags.push(`章节 ${locator.section}`)
  if (locator.sheet) tags.push(`工作表 ${locator.sheet}`)
  if (locator.page) tags.push(`第 ${locator.page} 页`)
  if (locator.table_index !== undefined) tags.push(`表 #${Number(locator.table_index) + 1}`)
  if (locator.file) tags.push(`导入文件: ${locator.file}`)
  if (locator.editor) tags.push(`维护人: ${locator.editor}`)
  if (locator.source) tags.push(locator.source === 'HUMAN' ? '人工录入' : locator.source)
  if (locator.quote) tags.push(`原文: "${String(locator.quote).slice(0, 36)}..."`)

  if (!tags.length) {
    return <code className="code-pill">{JSON.stringify(locator)}</code>
  }
  return (
    <div className="locator-tags">
      {tags.map((item, idx) => (
        <span key={idx} className="locator-pill">{item}</span>
      ))}
    </div>
  )
}

export function EvidenceCard({ field }) {
  const ai = field?.ai_value_json || {}
  const ev = field?.evidence_json || {}
  const compareKeys = ['field_cn', 'field_en', 'data_type', 'length', 'decimal_places', 'required', 'code_reference_text']
  const diffs = compareKeys.filter((key) => ai[key] !== undefined && String(ai[key]) !== String(field[key]))

  return (
    <div className="evidence-card">
      <div className="evidence-card-header">
        <span className="evidence-title">原文溯源与 AI 初值对比</span>
        {field.confidence !== undefined && (
          <StatusBadge tone={field.confidence >= 0.9 ? 'green' : 'amber'}>
            置信度 {Math.round(Number(field.confidence || 0) * 100)}%
          </StatusBadge>
        )}
      </div>
      <div className="evidence-row">
        <span className="evidence-label">证据位置</span>
        <SourceLocatorTag locator={ev} />
      </div>
      {ev.quote && (
        <blockquote className="evidence-quote">{ev.quote}</blockquote>
      )}
      {diffs.length > 0 ? (
        <div className="evidence-diff-box">
          <div className="evidence-label">人工已修正属性（对比 AI 初值）：</div>
          <div className="mini-diff-list">
            {diffs.map((key) => (
              <div key={key} className="mini-diff-item">
                <span>{FIELD_LABELS[key] || key}：</span>
                <del>{String(ai[key] ?? '空')}</del>
                <span className="diff-arrow">→</span>
                <ins>{String(field[key] ?? '空')}</ins>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="evidence-consistent">当前字段属性值与 AI 初始提取结果完全一致，未做人工偏移。</p>
      )}
    </div>
  )
}

export function RevisionDiffView({ revision }) {
  const [showRaw, setShowRaw] = useState(false)
  const kind = revision.revision_kind
  const oldVal = revision.old_value || {}
  const newVal = revision.new_value || {}

  const renderStructuredBody = () => {
    if (kind === 'AI_EXTRACTION') {
      const draft = newVal.draft || {}
      const trace = newVal.trace || {}
      return (
        <div className="revision-summary-box">
          <div className="revision-kv-grid">
            <div><span>解析表名</span><strong>{draft.table_name || '—'} ({draft.table_code || '—'})</strong></div>
            <div><span>提取字段数</span><strong>{Array.isArray(draft.fields) ? `${draft.fields.length} 个字段` : '—'}</strong></div>
            <div><span>调用模型</span><strong>{trace.model || trace.provider || 'AI 模型'}</strong></div>
            <div><span>并发批次</span><strong>{trace.batch_count ? `${trace.batch_count} 批` : '单批'}</strong></div>
          </div>
          {draft.table_definition && <p className="revision-note">表定义：{draft.table_definition}</p>}
        </div>
      )
    }

    if (kind === 'AI_REVIEW') {
      const issues = Array.isArray(newVal.issues) ? newVal.issues : []
      return (
        <div className="revision-summary-box">
          {issues.length === 0 ? (
            <div className="validation-pass-banner">AI 独立复核全部字段通过，初稿与复核稿完全一致。</div>
          ) : (
            <div className="issue-card-list">
              {issues.map((issue, idx) => (
                <div key={idx} className={`issue-card severity-${(issue.severity || 'WARNING').toLowerCase()}`}>
                  <StatusBadge status={issue.severity || 'WARNING'} />
                  <div>
                    <strong>{issue.field_en || '整表级'} · {issue.code}</strong>
                    <p>{issue.message}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )
    }

    if (kind === 'PUBLISHED' || kind === 'DRAFT_CREATED') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note">
            {kind === 'PUBLISHED'
              ? `已发布版本 v${newVal.version || '—'}，可用于生成规则和数据库验证。`
              : `已创建可修改版本 v${newVal.version || '—'}。`}
          </p>
        </div>
      )
    }

    if (kind === 'HUMAN_DELETE') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note danger-text">
            已删除字段：<strong>{oldVal.field_cn || ''} ({oldVal.field_en || '未知字段'})</strong>
          </p>
        </div>
      )
    }

    if (kind === 'HUMAN_ADD') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note">
            新增字段：<strong>{newVal.field_cn} ({newVal.field_en})</strong> · {newVal.data_type}({newVal.length}) · {newVal.required ? '必填' : '选填'}
          </p>
        </div>
      )
    }

    // Default: HUMAN_EDIT or field-level diff
    const keys = Object.keys(FIELD_LABELS)
    const changedKeys = keys.filter((k) => {
      if (oldVal[k] === undefined && newVal[k] === undefined) return false
      return String(oldVal[k] ?? '') !== String(newVal[k] ?? '')
    })

    if (changedKeys.length === 0) {
      return <p className="revision-note">保存了字段快照（无核心属性值变更）。</p>
    }

    return (
      <div className="diff-table-wrap">
        <table className="diff-table">
          <thead>
            <tr>
              <th>变更属性 ({newVal.field_en || oldVal.field_en || '字段'})</th>
              <th>修改前 (Old)</th>
              <th>修改后 (New)</th>
            </tr>
          </thead>
          <tbody>
            {changedKeys.map((key) => (
              <tr key={key}>
                <td className="diff-field-name">{FIELD_LABELS[key] || key}</td>
                <td className="diff-cell-old"><del>{ formatDiffValue(key, oldVal[key]) }</del></td>
                <td className="diff-cell-new"><ins>{ formatDiffValue(key, newVal[key]) }</ins></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div className="revision-card">
      <div className="revision-card-header">
        <div className="revision-meta">
          <StatusBadge
            tone={
              kind === 'PUBLISHED'
                ? 'green'
                : kind === 'AI_EXTRACTION' || kind === 'AI_REVIEW'
                  ? 'violet'
                  : 'blue'
            }
          >
            {REVISION_LABELS[kind] || kind}
          </StatusBadge>
          <strong>{revision.reason || '变更记录'}</strong>
          <span className="muted-text">{revision.editor || '系统'}</span>
        </div>
        <div className="revision-actions">
          <span className="revision-time">{formatTime(revision.created_at)}</span>
          <button
            type="button"
            className="button-link"
            onClick={() => setShowRaw(!showRaw)}
          >
            {showRaw ? '隐藏原始 JSON' : '原始报文'}
          </button>
        </div>
      </div>
      {renderStructuredBody()}
      {showRaw && (
        <div className="diff-grid raw-diff-drawer">
          <div>
            <span className="raw-label">变更前 (old_value)</span>
            <pre>{JSON.stringify(revision.old_value, null, 2)}</pre>
          </div>
          <div>
            <span className="raw-label">变更后 (new_value)</span>
            <pre>{JSON.stringify(revision.new_value, null, 2)}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

function formatDiffValue(key, val) {
  if (val === null || val === undefined || val === '') return '（空）'
  if (key === 'required') return val ? '是（必填）' : '否（选填）'
  return String(val)
}

export function DataTableModal({ artifact, onClose }) {
  const [filter, setFilter] = useState('')
  const [rawMode, setRawMode] = useState(false)
  const [copied, setCopied] = useState(false)
  const copyTimerRef = useRef(null)

  useEffect(() => {
    setFilter('')
    setRawMode(false)
    setCopied(false)
  }, [artifact])

  useEffect(() => () => {
    if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
  }, [])

  useEffect(() => {
    if (!artifact) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [artifact, onClose])

  if (!artifact) return null

  const rows = Array.isArray(artifact.rows) ? artifact.rows : null
  const columns = rows && rows.length > 0 ? Object.keys(rows[0]) : []
  const filteredRows = rows
    ? rows.filter((row) =>
        !filter.trim() ||
        Object.values(row).some((v) => String(v ?? '').toLowerCase().includes(filter.toLowerCase()))
      )
    : []

  async function copyContent() {
    const text = rows ? JSON.stringify(rows, null, 2) : artifact.content || ''
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const area = document.createElement('textarea')
      area.value = text
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      document.body.removeChild(area)
    }
    setCopied(true)
    if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
    copyTimerRef.current = window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="panel-heading modal-header">
          <div>
            <h2>{artifact.title}</h2>
            {rows && (
              <p>
                共加载 {rows.length} 行记录 · {columns.length} 个字段
                {filter && `（筛选后 ${filteredRows.length} 行）`}
              </p>
            )}
          </div>
          <div className="actions">
            {rows && (
              <input
                type="search"
                placeholder="搜索表格内容..."
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            )}
            {rows && (
              <button
                type="button"
                className="button secondary"
                onClick={() => setRawMode(!rawMode)}
              >
                {rawMode ? '切换表格视图' : '查看 JSON'}
              </button>
            )}
            <button type="button" className="button secondary" onClick={copyContent}>
              {copied ? '已复制' : '复制内容'}
            </button>
            <button type="button" className="button primary" onClick={onClose}>
              关闭窗口
            </button>
          </div>
        </div>

        {rows && !rawMode ? (
          rows.length > 0 ? (
            <div className="table-wrap modal-table-wrap">
              <table className="data-preview-table">
                <thead>
                  <tr>
                    <th>#</th>
                    {columns.map((col) => (
                      <th key={col}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.map((row, idx) => (
                    <tr key={idx}>
                      <td className="row-index">{idx + 1}</td>
                      {columns.map((col) => (
                        <td key={col}>
                          {row[col] === null || row[col] === undefined ? (
                            <span className="null-pill">NULL</span>
                          ) : typeof row[col] === 'object' ? (
                            <code>{JSON.stringify(row[col])}</code>
                          ) : (
                            String(row[col])
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state">该沙箱表当前暂无数据行。</div>
          )
        ) : (
          <pre className="modal-code-viewer">
            {rows ? JSON.stringify(rows, null, 2) : artifact.content}
          </pre>
        )}
      </section>
    </div>
  )
}
