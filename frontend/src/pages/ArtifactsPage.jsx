import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge, formatTime } from './SharedUI.jsx'

const TABS = [
  ['rules', '校验规则'],
  ['ddl', '表结构'],
  ['codes', '码表'],
  ['audit', '验证结果'],
]

const STATUS_FILTERS = [
  ['all', '全部'],
  ['ungenerated', '未生成'],
  ['running', '生成中'],
  ['verified', '已验证'],
  ['failed', '验证失败'],
]

function runTone(row) {
  if (!row.generated) return 'neutral'
  if (row.run_status === 'FAILED') return 'red'
  if (row.run_status === 'COMPLETED') return row.all_passed ? 'green' : 'amber'
  return 'blue'
}

function runLabel(row) {
  if (!row.generated) return '未生成'
  if (row.run_status === 'FAILED') return '验证失败'
  if (row.run_status === 'COMPLETED') return row.all_passed ? '已验证' : '验证未过'
  return '生成中'
}

export default function ArtifactsPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const [tables, setTables] = useState([])
  const [selected, setSelected] = useState([])
  const [currentId, setCurrentId] = useState('')
  const [detail, setDetail] = useState(null)
  const [tab, setTab] = useState('rules')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [expandedRule, setExpandedRule] = useState(null)
  const [statusFilter, setStatusFilter] = useState('all')
  const [docFilter, setDocFilter] = useState('all')
  const [search, setSearch] = useState('')

  async function load(preferredId) {
    const data = await v2('/artifacts')
    setTables(data.tables || [])
    const target = preferredId || params.get('table') || data.tables[0]?.id
    if (target) await select(target)
  }

  async function select(id) {
    setCurrentId(id)
    const data = await v2(`/artifacts/${id}`)
    setDetail(data)
  }

  useEffect(() => {
    setDetail(null)
    load().catch((err) => setError(err.message))
  }, [])

  const documents = useMemo(() => {
    const map = new Map()
    tables.forEach((item) => {
      if (item.document_id) map.set(item.document_id, item.document_name || item.document_id)
    })
    return [...map.entries()]
  }, [tables])

  const filteredTables = useMemo(() => {
    return tables.filter((row) => {
      if (statusFilter === 'ungenerated' && row.generated) return false
      if (statusFilter === 'running' && !(row.generated && !['COMPLETED', 'FAILED'].includes(row.run_status))) return false
      if (statusFilter === 'verified' && !(row.run_status === 'COMPLETED' && row.all_passed)) return false
      if (statusFilter === 'failed' && row.run_status !== 'FAILED') return false
      if (docFilter !== 'all' && row.document_id !== docFilter) return false
      const q = search.trim().toLowerCase()
      if (q && !row.table_name.toLowerCase().includes(q) && !row.table_code.toLowerCase().includes(q)) return false
      return true
    })
  }, [tables, statusFilter, docFilter, search])

  function toggle(id) {
    setSelected(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id])
  }

  async function generate(tableIds) {
    const ids = tableIds || selected
    if (!ids.length) {
      setError('请先勾选要生成的报表。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const run = await v2('/generation/runs', {
        method: 'POST',
        body: JSON.stringify({ table_ids: ids, provider: 'live' }),
      })
      setNotice('已启动生成，正在跳转执行中心。')
      navigate(`/runs?run=${run.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function downloadBundle() {
    if (!detail?.run?.id) return
    window.open(`/api/v2/generation/runs/${detail.run.id}/production-bundle`, '_blank')
  }

  const currentRow = tables.find((item) => item.id === currentId)

  return (
    <>
      <PageHeader
        eyebrow="第 2 步 · 共 3 步"
        title="规则工作台 · 生成表结构、码表和规则"
        description="选表生成后，在这里看每张表最终产出的校验规则、表结构、码表迁移和验证结果。规则是核心，默认展示。"
        actions={
          <button
            type="button"
            className="button primary"
            disabled={busy || !selected.length}
            onClick={() => generate()}
          >
            {busy ? '正在启动...' : `开始生成 (${selected.length} 张报表)`}
          </button>
        }
      />
      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <div className="generation-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>报表清单</h2>
              <p>{filteredTables.length} / {tables.length} 张 · 已选择 {selected.length} 张</p>
            </div>
          </div>
          <div className="list-filter-bar">
            <input
              type="search"
              placeholder="检索表名或代码..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              {STATUS_FILTERS.map(([key, label]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
            {documents.length > 1 && (
              <select value={docFilter} onChange={(e) => setDocFilter(e.target.value)}>
                <option value="all">全部文件</option>
                {documents.map(([id, name]) => (
                  <option key={id} value={id}>{name}</option>
                ))}
              </select>
            )}
          </div>
          <div className="list-stack">
            {filteredTables.length === 0 && (
              <div className="empty">没有符合筛选的报表。</div>
            )}
            {filteredTables.map((row) => (
              <div
                key={row.id}
                className={`list-row ${currentId === row.id ? 'selected' : ''}`}
                role="button"
                tabIndex={0}
                onClick={() => select(row.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    select(row.id)
                  }
                }}
              >
                <div className="list-row-top">
                  <strong>{row.table_name}</strong>
                  <StatusBadge tone={runTone(row)}>{runLabel(row)}</StatusBadge>
                </div>
                <span>
                  <code>{row.table_code}</code>
                </span>
                <div className="row-actions" onClick={(event) => event.stopPropagation()}>
                  <label className="inline-option">
                    <input
                      type="checkbox"
                      checked={selected.includes(row.id)}
                      onChange={() => toggle(row.id)}
                    />
                    加入本次生成
                  </label>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          {!detail ? (
            <div className="empty">加载中...</div>
          ) : (
            <>
              <div className="panel-heading">
                <div>
                  <h2>{detail.table.table_name}</h2>
                  <p>
                    <code>{detail.table.table_code}</code> · {detail.table.field_count} 个字段
                    {detail.run && ` · 最近生成 ${formatTime(detail.run.created_at)}`}
                  </p>
                </div>
                {detail.run && (
                  <div className="actions">
                    <button
                      type="button"
                      className="button secondary sm"
                      disabled={busy}
                      onClick={() => generate([detail.table.id])}
                    >
                      重新生成这张表
                    </button>
                    <button type="button" className="button secondary sm" onClick={downloadBundle}>
                      下载产出包
                    </button>
                  </div>
                )}
                {!detail.run && (
                  <div className="actions">
                    <button
                      type="button"
                      className="button primary sm"
                      disabled={busy}
                      onClick={() => generate([detail.table.id])}
                    >
                      生成这张表
                    </button>
                  </div>
                )}
              </div>

              <nav className="tab-bar" aria-label="产物视图">
                {TABS.map(([key, label]) => (
                  <button
                    type="button"
                    key={key}
                    className={tab === key ? 'active' : ''}
                    onClick={() => setTab(key)}
                  >
                    {label}
                    {key === 'rules' && detail.rules.length > 0 && ` (${detail.rules.length})`}
                  </button>
                ))}
              </nav>

              {!detail.run && (
                <div className="empty">
                  这张表还没有生成过。在左侧勾选后点「开始生成」，生成 DDL、码表和校验规则。
                </div>
              )}

              {tab === 'rules' && detail.run && (
                <RulesView
                  rules={detail.rules}
                  groups={detail.rule_groups}
                  expandedRule={expandedRule}
                  onToggle={setExpandedRule}
                  onSaved={() => select(currentId)}
                  onNotice={setNotice}
                />
              )}

              {tab === 'ddl' && (
                <div className="artifact-block">
                  <h3>建表 SQL（Oracle）</h3>
                  {detail.ddl ? (
                    <pre className="sql-pre">{detail.ddl}</pre>
                  ) : (
                    <div className="empty">还没有生成表结构。</div>
                  )}
                </div>
              )}

              {tab === 'codes' && (
                <div className="artifact-block">
                  <h3>码表迁移（灌入 Oracle 沙箱）</h3>
                  {detail.code_sets.length > 0 ? (
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>码表</th>
                          <th>码值数</th>
                          <th>迁移到 Oracle</th>
                          <th>目标表</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.code_sets.map((set) => (
                          <tr key={set.code}>
                            <td>
                              <strong>{set.name}</strong>
                              <small>{set.code}</small>
                            </td>
                            <td>{set.value_count}</td>
                            <td>
                              <StatusBadge tone={set.oracle_records > 0 ? 'green' : 'neutral'}>
                                {set.oracle_records} 条
                              </StatusBadge>
                            </td>
                            <td><code>{set.oracle_table || '—'}</code></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <div className="empty">这张表没有关联码表。</div>
                  )}
                </div>
              )}

              {tab === 'audit' && (
                <AuditView audit={detail.audit} runId={detail.run?.id} />
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}

function RulesView({ rules, groups, expandedRule, onToggle, onSaved, onNotice }) {
  const [operatorFilter, setOperatorFilter] = useState('all')
  const [fieldSearch, setFieldSearch] = useState('')
  const [editingRule, setEditingRule] = useState(null)

  if (!rules.length) {
    return <div className="empty">这张表还没有生成校验规则。</div>
  }
  const order = ['REQUIRED', 'ENUM', 'COND_REQUIRED', 'LENGTH', 'LENGTH_MAX', 'REGEXP', 'NOT_IN', 'FOREIGN_KEY']
  const sorted = [...rules].sort(
    (a, b) => order.indexOf(a.operator) - order.indexOf(b.operator) || a.field.localeCompare(b.field)
  )
  const visible = sorted.filter((rule) => {
    if (operatorFilter !== 'all' && rule.operator !== operatorFilter) return false
    const q = fieldSearch.trim().toLowerCase()
    if (q && !rule.field.toLowerCase().includes(q) && !rule.rule_key.toLowerCase().includes(q)) return false
    return true
  })

  return (
    <div className="rules-workspace">
      <div className="list-filter-bar rule-filter-bar">
        <select value={operatorFilter} onChange={(e) => setOperatorFilter(e.target.value)}>
          <option value="all">全部算子 ({rules.length})</option>
          {Object.entries(groups).map(([op, count]) => (
            <option key={op} value={op}>
              {rules.find((rule) => rule.operator === op)?.operator_label || op} ({count})
            </option>
          ))}
        </select>
        <input
          type="search"
          placeholder="按字段或规则名检索..."
          value={fieldSearch}
          onChange={(e) => setFieldSearch(e.target.value)}
        />
      </div>
      <div className="rule-card-list">
        {visible.map((rule) => {
          const expanded = expandedRule === rule.rule_key
          const editing = editingRule === rule.row_id
          return (
            <div key={rule.rule_key} className="rule-card">
              <div className="rule-card-head" onClick={() => onToggle(expanded ? null : rule.rule_key)}>
                <code className="rule-field">{rule.field}</code>
                <StatusBadge tone="blue">{rule.operator_label}</StatusBadge>
                <StatusBadge tone={rule.source === 'DOC_EXPLICIT' ? 'green' : 'violet'}>
                  {rule.source === 'DOC_EXPLICIT' ? '原文明确' : 'AI 建议'}
                </StatusBadge>
                <StatusBadge tone={rule.status === 'APPROVED' ? 'green' : rule.status === 'PENDING' ? 'amber' : 'neutral'}>
                  {rule.status === 'APPROVED' ? '已批准' : rule.status === 'PENDING' ? '待审' : rule.status}
                </StatusBadge>
                <span className="rule-expand">{expanded ? '收起' : '展开'}</span>
                <button
                  type="button"
                  className="button-link rule-edit-btn"
                  onClick={(event) => {
                    event.stopPropagation()
                    setEditingRule(editing ? null : rule.row_id)
                  }}
                >
                  {editing ? '收起编辑' : '编辑参数'}
                </button>
              </div>
              {Object.keys(rule.params || {}).length > 0 && (
                <div className="rule-params">
                  {Object.entries(rule.params).map(([key, value]) => (
                    <span key={key}>
                      <em>{key}</em> {Array.isArray(value) ? value.join('、') : String(value)}
                    </span>
                  ))}
                </div>
              )}
              {rule.sql && <pre className="rule-sql">{rule.sql}</pre>}
              {editing && (
                <RuleEditForm
                  rule={rule}
                  onCancel={() => setEditingRule(null)}
                  onSaved={() => {
                    setEditingRule(null)
                    onNotice?.(`已保存「${rule.rule_key}」的参数，点「重新生成这张表」验证改动。`)
                    onSaved?.()
                  }}
                />
              )}
              {expanded && !editing && (
                <div className="rule-detail">
                  <p className="rule-rationale">{rule.rationale}</p>
                  {rule.evidence && (
                    <blockquote className="rule-evidence">
                      <span>
                        原文依据 · {rule.evidence.section} · 第 {rule.evidence.page} 页 · 字段 {rule.evidence.field}
                      </span>
                      {rule.evidence.quote}
                    </blockquote>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {visible.length === 0 && <div className="empty">没有符合筛选的规则。</div>}
      </div>
    </div>
  )
}

function AuditView({ audit, runId }) {
  if (!audit) {
    return <div className="empty">还没有跑过验证。</div>
  }
  const passed = audit.all_passed
  return (
    <div className="artifact-block">
      <div className={`audit-banner ${passed ? 'ok' : 'bad'}`}>
        <strong>{passed ? '验证通过' : '验证未通过'}</strong>
        <span>
          {audit.case_count} 条用例 · {audit.active_rule_count} 条规则 · 覆盖率{' '}
          {audit.coverage_rate != null ? `${Math.round(audit.coverage_rate * 100)}%` : '—'}
        </span>
      </div>
      <div className="audit-metrics">
        <div><span>误报（该拦没拦）</span><strong>{audit.false_negative_count ?? '—'}</strong></div>
        <div><span>漏报（不该拦却拦）</span><strong>{audit.false_positive_count ?? '—'}</strong></div>
        <div><span>共触发</span><strong>{audit.co_triggered_count ?? '—'}</strong></div>
        <div><span>覆盖完整</span><strong>{audit.coverage_complete ? '是' : '否'}</strong></div>
      </div>
    </div>
  )
}

function RuleEditForm({ rule, onCancel, onSaved }) {
  const [params, setParams] = useState({ ...(rule.params || {}) })
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function set(key, value) {
    setParams({ ...params, [key]: value })
  }

  async function save() {
    setBusy(true)
    setError('')
    try {
      const payload = { ...params }
      if (Array.isArray(payload.values)) {
        payload.values = payload.values.map((item) => String(item).trim()).filter(Boolean)
      }
      if (Array.isArray(payload.if_values)) {
        payload.if_values = payload.if_values.map((item) => String(item).trim()).filter(Boolean)
      }
      await v2(`/artifacts/rules/${rule.row_id}`, {
        method: 'PATCH',
        body: JSON.stringify({ params: payload, reason }),
      })
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const operator = rule.operator
  return (
    <div className="rule-edit-form" onClick={(event) => event.stopPropagation()}>
      <h4>编辑参数 · {rule.field}（{rule.operator_label}）</h4>

      {operator === 'REQUIRED' && (
        <p className="muted-text">必填规则没有参数，整条规则就是「去掉空白后必须有值」。</p>
      )}

      {(operator === 'LENGTH' || operator === 'LENGTH_MAX') && (
        <label>
          <span>{operator === 'LENGTH' ? '定长' : '最大长度'}（字符数）</span>
          <input
            type="number"
            min="1"
            value={params.length ?? ''}
            onChange={(e) => set('length', Number(e.target.value))}
          />
        </label>
      )}

      {(operator === 'ENUM' || operator === 'NOT_IN') && (
        <label>
          <span>允许取值（每行一个）</span>
          <textarea
            rows={Math.min(8, (params.values || []).length + 1)}
            value={(params.values || []).join('\n')}
            onChange={(e) => set('values', e.target.value.split('\n'))}
          />
        </label>
      )}

      {operator === 'REGEXP' && (
        <label>
          <span>正则（Oracle REGEXP_LIKE 语义）</span>
          <input
            value={params.pattern ?? ''}
            onChange={(e) => set('pattern', e.target.value)}
            placeholder="^\d{8}$"
          />
        </label>
      )}

      {operator === 'COND_REQUIRED' && (
        <>
          <label>
            <span>条件字段</span>
            <input
              value={params.if_field ?? ''}
              onChange={(e) => set('if_field', e.target.value)}
            />
          </label>
          <label>
            <span>条件</span>
            <select
              value={params.if_operator ?? 'EQ'}
              onChange={(e) => set('if_operator', e.target.value)}
            >
              <option value="EQ">等于（EQ）</option>
              <option value="NE">不等于（NE）</option>
            </select>
          </label>
          <label>
            <span>条件取值（每行一个）</span>
            <textarea
              rows={Math.min(6, (params.if_values || []).length + 1)}
              value={(params.if_values || []).join('\n')}
              onChange={(e) => set('if_values', e.target.value.split('\n'))}
            />
          </label>
        </>
      )}

      {operator === 'FOREIGN_KEY' && (
        <>
          <label>
            <span>引用表</span>
            <input
              value={params.references_table ?? ''}
              onChange={(e) => set('references_table', e.target.value)}
            />
          </label>
          <label>
            <span>引用字段</span>
            <input
              value={params.references_field ?? ''}
              onChange={(e) => set('references_field', e.target.value)}
            />
          </label>
        </>
      )}

      {operator !== 'REQUIRED' && (
        <label>
          <span>修改原因（可选）</span>
          <input
            placeholder="例：原文限 28，业务实际允许 32"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
      )}

      {error && <p className="danger-text">{error}</p>}
      <div className="actions">
        <button type="button" className="button primary sm" disabled={busy || operator === 'REQUIRED'} onClick={save}>
          {busy ? '保存中...' : '保存参数'}
        </button>
        <button type="button" className="button secondary sm" onClick={onCancel}>
          取消
        </button>
      </div>
      <p className="muted-text">保存后不会自动重跑验证，点上方「重新生成这张表」确认改动是否通过。</p>
    </div>
  )
}
