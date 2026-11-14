import { useState } from 'react'

export const STATUS_META = {
  PUBLISHED: { label: '已发布', tone: 'green' },
  DRAFT: { label: '草稿待审', tone: 'amber' },
  AI_SELF_VALIDATED: { label: 'AI 自审通过', tone: 'blue' },
  NEEDS_REVIEW: { label: '需人工复核', tone: 'red' },
  RAW_EXTRACTED: { label: '已切块提取', tone: 'blue' },
  COMPLETED: { label: '审计通过', tone: 'green' },
  WAITING_APPROVAL: { label: '待人工确权', tone: 'amber' },
  RUNNING: { label: '正在执行', tone: 'blue' },
  AUDITING: { label: '沙箱审计中', tone: 'blue' },
  READY_TO_AUDIT: { label: '待沙箱审计', tone: 'blue' },
  FAILED: { label: '执行异常', tone: 'red' },
  APPROVED: { label: '已批准', tone: 'green' },
  REJECTED: { label: '已拒绝', tone: 'red' },
  PENDING: { label: '待确权', tone: 'amber' },
  PASSED: { label: '校验通过', tone: 'green' },
  ERROR: { label: '错误阻断', tone: 'red' },
  WARNING: { label: '复核提示', tone: 'amber' },
  RESOLVED: { label: '已绑定码值', tone: 'green' },
  UNRESOLVED: { label: '码值未决', tone: 'red' },
  DOC_EXPLICIT: { label: '文档显性规则', tone: 'blue' },
  AI_EXPANDED: { label: 'AI 拓展规则', tone: 'violet' },
  MANUAL: { label: '人工维护', tone: 'neutral' },
  DOCUMENT_REFERENCE: { label: '文档引用自动建表', tone: 'blue' },
  PROGRAM: { label: '确定性程序校验', tone: 'blue' },
  AI_REVIEW: { label: 'GLM 独立复核', tone: 'violet' },
}

export const REVISION_LABELS = {
  AI_EXTRACTION: 'AI 初始提取赋值',
  AI_REVIEW: 'AI 独立交叉复核',
  HUMAN_EDIT: '人工精修字段',
  HUMAN_ADD: '人工新增字段',
  HUMAN_DELETE: '人工删除字段',
  DRAFT_CREATED: '从发布版派生草稿',
  PUBLISHED: '正式发布定版',
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
  CODE_REFERENCE_NOT_BOUND: '引用了码值表但尚未发布绑定记录（请重新发布元数据表或前往码值中心维护）',
  REQUIRED_CODE_SET_UNRESOLVED: '必填字段关联的码值表尚无启用项或未发布',
}

export function StatusBadge({ status, children, tone }) {
  const meta = STATUS_META[status] || { label: status || '未知', tone: 'neutral' }
  const finalTone = tone || meta.tone
  return <span className={`badge badge-${finalTone}`}>{children || meta.label}</span>
}

export function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
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
            <div><span>调用模型</span><strong>{trace.model || trace.provider || 'GLM'}</strong></div>
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
            <div className="validation-pass-banner">GLM 独立复核全部字段通过，初稿与复核稿完全一致。</div>
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
              ? `已锁定并发布不可变快照版本 v${newVal.version || '—'}，可用于下游规则装配与沙箱造数。`
              : `已从发布基线派生新草稿版本 v${newVal.version || '—'}。`}
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
          <strong>操作人：{revision.editor || '系统'}</strong>
          <span className="muted-text">{revision.reason}</span>
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
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // ignore
    }
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
