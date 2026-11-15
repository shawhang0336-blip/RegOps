import { useEffect, useState } from 'react'
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

  function toggle(id) {
    setSelected(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id])
  }

  async function generate() {
    if (!selected.length) {
      setError('请先勾选要生成的报表。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const run = await v2('/generation/runs', {
        method: 'POST',
        body: JSON.stringify({ table_ids: selected, provider: 'live' }),
      })
      setNotice(`已启动生成，正在跳转执行中心。`)
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
            onClick={generate}
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
              <p>{tables.length} 张 · 已选择 {selected.length} 张</p>
            </div>
          </div>
          <div className="list-stack">
            {tables.map((row) => (
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
                    <button type="button" className="button secondary sm" onClick={downloadBundle}>
                      下载产出包
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

function RulesView({ rules, groups, expandedRule, onToggle }) {
  if (!rules.length) {
    return <div className="empty">这张表还没有生成校验规则。</div>
  }
  const order = ['REQUIRED', 'ENUM', 'COND_REQUIRED', 'LENGTH', 'LENGTH_MAX', 'REGEXP', 'NOT_IN', 'FOREIGN_KEY']
  const sorted = [...rules].sort(
    (a, b) => order.indexOf(a.operator) - order.indexOf(b.operator) || a.field.localeCompare(b.field)
  )
  return (
    <div className="rules-workspace">
      <div className="rule-groups-bar">
        {Object.entries(groups).map(([op, count]) => (
          <span key={op} className="basis-pill">
            {rules.find((rule) => rule.operator === op)?.operator_label || op} {count}
          </span>
        ))}
      </div>
      <div className="rule-card-list">
        {sorted.map((rule) => {
          const expanded = expandedRule === rule.rule_key
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
              {expanded && (
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
