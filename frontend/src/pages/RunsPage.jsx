import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, v2 } from '../api.js'
import { useOperator } from '../context/OperatorContext.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { DataTableModal, StatusBadge } from './SharedUI.jsx'

const PIPELINE_STEPS = [
  { num: '01', label: 'AI 生成计划', match: ['PLAN', 'INIT'] },
  { num: '02', label: 'Schema 扇出', match: ['SCHEMA', 'DDL'] },
  { num: '03', label: '规则扇出与汇合', match: ['RULE', 'BARRIER'] },
  { num: '04', label: '拓展规则确权', match: ['APPROVAL', 'WAIT', 'HUMAN'] },
  { num: '05', label: '生成器扇出', match: ['DATA', 'GEN', 'MUTATION'] },
  { num: '06', label: '审计用例扇出', match: ['CASE', 'SQL'] },
  { num: '07', label: 'Oracle 双表执行', match: ['ORACLE', 'SANDBOX', 'AUDIT'] },
  { num: '08', label: '审计收敛与部署包', match: ['COMPLETE', 'DONE', 'RETRY'] },
]

function resolveStepIndex(run) {
  if (!run) return -1
  if (run.status === 'COMPLETED') return 7
  if (run.status === 'WAITING_APPROVAL') return 3
  const node = (run.current_node || '').toUpperCase()
  for (let i = PIPELINE_STEPS.length - 1; i >= 0; i -= 1) {
    if (PIPELINE_STEPS[i].match.some((m) => node.includes(m))) return i
  }
  return 2
}

export default function RunsPage() {
  const [params] = useSearchParams()
  const { requireOperator } = useOperator()

  const [runs, setRuns] = useState([])
  const [active, setActive] = useState(null)
  const [plans, setPlans] = useState([])
  const [artifact, setArtifact] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [focusedRuleIndex, setFocusedRuleIndex] = useState(0)
  const [tableFilter, setTableFilter] = useState('ALL')
  const [sourceFilter, setSourceFilter] = useState('ALL')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const select = useCallback(async (id) => {
    const [run, planList] = await Promise.all([
      api(`/api/runs/${id}`),
      v2(`/generation/plans/${id}`).catch(() => []),
    ])
    setActive(run)
    setPlans(planList)
    setFocusedRuleIndex(0)
  }, [])

  const load = useCallback(
    async (preferredId) => {
      const list = await api('/api/runs')
      setRuns(list)
      const target = preferredId || active?.id || params.get('run') || list[0]?.id
      if (target) {
        await select(target)
      }
    },
    [active?.id, params, select]
  )

  useEffect(() => {
    load(params.get('run')).catch((err) => setError(err.message))
  }, [])

  // Auto-poll while run is actively executing
  useEffect(() => {
    if (!active || !['RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(active.status)) {
      return undefined
    }
    const timer = window.setInterval(() => {
      if (!busy) {
        load(active.id).catch(() => {})
      }
    }, 5000)
    return () => window.clearInterval(timer)
  }, [active?.id, active?.status, busy, load])

  async function openArtifact(uri, title) {
    const body = await api(`/api/blobs/${uri.replace('blob://', '')}`)
    setArtifact({ title, content: body.content })
  }

  async function previewOracleTable(tableCode, physicalTable) {
    const rows = await api(`/api/runs/${active.id}/oracle-preview?table_code=${tableCode}&limit=50`)
    setArtifact({
      title: `Oracle 沙箱合成数据预览 · ${tableCode} (${physicalTable})`,
      rows: Array.isArray(rows) ? rows : [],
      content: JSON.stringify(rows, null, 2),
    })
  }

  const pending = useMemo(
    () => active?.rules?.filter((rule) => rule.status === 'PENDING') || [],
    [active]
  )

  const setAllDecisions = useCallback(
    (decision) => {
      setDecisions(Object.fromEntries(pending.map((rule) => [rule.rule_hash, decision])))
    },
    [pending]
  )

  const submitApprovals = useCallback(async () => {
    const approver = requireOperator(setError)
    if (!approver) return
    if (pending.some((rule) => !decisions[rule.rule_hash])) {
      setError('请对全部待确权规则选择「批准」或「拒绝」（可使用快捷键 Shift+A 全批 / Shift+R 全拒）。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/runs/${active.id}/approvals`, {
        method: 'POST',
        body: JSON.stringify({
          approver,
          provider: 'live',
          decisions: pending.map((rule) => ({
            rule_hash: rule.rule_hash,
            decision: decisions[rule.rule_hash],
          })),
        }),
      })
      if (body.detail) {
        throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
      }
      setActive(body)
      setDecisions({})
      setNotice('人工确权决定已写入不可变事件账本，流水线已自动推进后续造数与沙箱审计。')
      await load(body.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }, [active?.id, decisions, load, pending, requireOperator])

  // Keyboard shortcuts for rapid rule approval
  useEffect(() => {
    if (!pending.length || artifact) return undefined

    function handleKeyDown(e) {
      const tag = document.activeElement?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !busy) {
          e.preventDefault()
          submitApprovals()
        }
        return
      }

      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !busy) {
        e.preventDefault()
        submitApprovals()
        return
      }

      if (e.shiftKey && (e.key === 'A' || e.key === 'a')) {
        e.preventDefault()
        setAllDecisions('APPROVED')
        return
      }

      if (e.shiftKey && (e.key === 'R' || e.key === 'r')) {
        e.preventDefault()
        setAllDecisions('REJECTED')
        return
      }

      if (e.key === 'ArrowDown' || e.key === 'j' || e.key === 'J') {
        e.preventDefault()
        setFocusedRuleIndex((idx) => Math.min(pending.length - 1, idx + 1))
        return
      }

      if (e.key === 'ArrowUp' || e.key === 'k' || e.key === 'K') {
        e.preventDefault()
        setFocusedRuleIndex((idx) => Math.max(0, idx - 1))
        return
      }

      if (!e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const currentRule = pending[focusedRuleIndex]
        if (!currentRule) return
        if (e.key === 'a' || e.key === 'A' || e.key === '1') {
          e.preventDefault()
          setDecisions((prev) => ({ ...prev, [currentRule.rule_hash]: 'APPROVED' }))
          setFocusedRuleIndex((idx) => Math.min(pending.length - 1, idx + 1))
        } else if (e.key === 'r' || e.key === 'R' || e.key === '2') {
          e.preventDefault()
          setDecisions((prev) => ({ ...prev, [currentRule.rule_hash]: 'REJECTED' }))
          setFocusedRuleIndex((idx) => Math.min(pending.length - 1, idx + 1))
        }
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [pending, focusedRuleIndex, artifact, busy, setAllDecisions, submitApprovals])

  async function retry() {
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/runs/${active.id}/retry`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setActive(body)
      await load(body.id)
      setNotice('已从失败节点触发自愈重试，AI 将根据沙箱反馈修正规则或数据资产后再执行。')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const summary = active?.result?.audit_summary
  const stepIdx = resolveStepIndex(active)
  const tableCodes = useMemo(
    () => [...new Set((active?.rules || []).map((r) => r.table_code))],
    [active]
  )

  const filteredRules = useMemo(() => {
    return (active?.rules || []).filter((rule) => {
      if (tableFilter !== 'ALL' && rule.table_code !== tableFilter) return false
      if (sourceFilter !== 'ALL' && rule.source !== sourceFilter) return false
      return true
    })
  }, [active, tableFilter, sourceFilter])

  const explicitCount = (active?.rules || []).filter((r) => r.source === 'DOC_EXPLICIT').length
  const expandedCount = (active?.rules || []).filter((r) => r.source === 'AI_EXPANDED').length
  const decidedCount = pending.filter((r) => Boolean(decisions[r.rule_hash])).length

  return (
    <>
      <PageHeader
        eyebrow="STAGE 05 · 8 阶段实时进度、人工确权与沙箱双表审计"
        title="执行与审计中心"
        description="实时跟踪 AI 生成计划、规则扇出、拓展规则人工确权、Oracle 沙箱双表验证与生产部署包导出。"
        actions={
          active && (
            <>
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={() => load(active.id)}
              >
                刷新状态
              </button>
              {active.status === 'FAILED' && (
                <button type="button" className="button primary" disabled={busy} onClick={retry}>
                  从失败点自愈重试
                </button>
              )}
            </>
          )
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      {active && (
        <section className="metrics">
          <div>
            <span>当前运行状态</span>
            <strong>
              <StatusBadge status={active.status} />
            </strong>
            <small>当前节点：{active.current_node || '—'}</small>
          </div>
          <div>
            <span>装配规则总数</span>
            <strong>{active.rules?.length || 0} 条</strong>
            <small>
              文档显性 {explicitCount} · AI 拓展 {expandedCount}
            </small>
          </div>
          <div>
            <span>待人工确权</span>
            <strong>{pending.length} 条</strong>
            <small>
              {pending.length > 0 ? `已决 ${decidedCount} / ${pending.length} 条` : '全部规则已确权'}
            </small>
          </div>
          <div>
            <span>Oracle 沙箱审计</span>
            <strong>{summary ? `${summary.case_count} 用例` : '待执行'}</strong>
            <small>
              {summary
                ? `${summary.false_positive_count} 误报 · ${summary.false_negative_count} 漏报`
                : '确权后自动执行双表比对'}
            </small>
          </div>
        </section>
      )}

      <section className="workspace-grid runs-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>运行批次列表</h2>
              <p>共 {runs.length} 次运行记录</p>
            </div>
            <StatusBadge tone="neutral">{runs.length}</StatusBadge>
          </div>

          {runs.length > 0 ? (
            <div className="list-stack">
              {runs.map((run) => (
                <button
                  type="button"
                  className={`list-row ${active?.id === run.id ? 'selected' : ''}`}
                  key={run.id}
                  onClick={() => {
                    setArtifact(null)
                    setDecisions({})
                    select(run.id)
                  }}
                >
                  <div className="list-row-top">
                    <strong>运行 #{run.id.slice(0, 8)}</strong>
                    <StatusBadge status={run.status} />
                  </div>
                  <span>当前阶段：{run.current_node}</span>
                  <em>事件数：{run.event_count ?? '—'}</em>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">暂无运行批次，请先在「生成范围与预检」启动生成。</div>
          )}
        </div>

        <div className="panel run-detail">
          {active ? (
            <>
              <div className="panel-heading">
                <div>
                  <div className="actions">
                    <h2>运行批次 #{active.id.slice(0, 8)}</h2>
                    <StatusBadge status={active.status} />
                    {summary && (
                      <StatusBadge tone={summary.all_passed ? 'green' : 'amber'}>
                        {summary.all_passed ? '沙箱零差异通过' : '存在审计差异'}
                      </StatusBadge>
                    )}
                  </div>
                  <p>
                    完整批次 ID：<code>{active.id}</code> · 当前节点：<code>{active.current_node}</code>
                  </p>
                </div>
              </div>

              <div className="pipeline-stepper">
                {PIPELINE_STEPS.map((step, idx) => {
                  let stateClass = ''
                  if (active.status === 'COMPLETED' || idx < stepIdx) stateClass = 'done'
                  else if (idx === stepIdx) stateClass = active.status === 'FAILED' ? 'failed' : 'current'
                  return (
                    <div key={step.num} className={`pipeline-step ${stateClass}`}>
                      <span className="pipeline-step-num">STAGE {step.num}</span>
                      <strong>{step.label}</strong>
                    </div>
                  )
                })}
              </div>

              {active.status === 'COMPLETED' && summary?.all_passed && (
                <div className="production-download">
                  <div className="production-download-info">
                    <strong>沙箱审计全部通过，生产部署包已就绪</strong>
                    <span>包含正式表名 DDL 与确定性校验 SQL（已剥离测试数据与沙箱辅助字段）</span>
                  </div>
                  <a
                    className="button primary"
                    href={`/api/v2/generation/runs/${active.id}/production-bundle`}
                  >
                    下载生产部署包 (.zip)
                  </a>
                </div>
              )}

              {pending.length > 0 && (
                <section className="approval-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>
                        AI 拓展规则人工确权 ({decidedCount} / {pending.length} 已选择)
                      </h2>
                      <p>批准决定与当前规则内容哈希强绑定；支持键盘快捷键极速逐条审批或批量审批。</p>
                    </div>
                    <div className="actions">
                      <button
                        type="button"
                        className="button secondary sm"
                        onClick={() => setAllDecisions('REJECTED')}
                      >
                        全部拒绝 <kbd className="kbd-hint">Shift+R</kbd>
                      </button>
                      <button
                        type="button"
                        className="button primary sm"
                        onClick={() => setAllDecisions('APPROVED')}
                      >
                        全部批准 <kbd className="kbd-hint">Shift+A</kbd>
                      </button>
                    </div>
                  </div>

                  <div className="shortcut-bar">
                    <span>
                      <kbd className="kbd-hint">↑</kbd> <kbd className="kbd-hint">↓</kbd> 或{' '}
                      <kbd className="kbd-hint">J</kbd> <kbd className="kbd-hint">K</kbd> 切换焦点规则
                    </span>
                    <span>
                      <kbd className="kbd-hint">A</kbd> 批准当前并下一条
                    </span>
                    <span>
                      <kbd className="kbd-hint">R</kbd> 拒绝当前并下一条
                    </span>
                    <span>
                      <kbd className="kbd-hint">Ctrl+Enter</kbd> 提交审批入账
                    </span>
                  </div>

                  {pending.map((rule, idx) => {
                    const isFocused = idx === focusedRuleIndex
                    return (
                      <div
                        className={`approval-rule ${isFocused ? 'focused-rule' : ''}`}
                        key={rule.rule_hash}
                        onClick={() => setFocusedRuleIndex(idx)}
                      >
                        <div>
                          <div className="actions">
                            <span className="rule-index-pill">#{idx + 1}</span>
                            <strong>{rule.rule_key}</strong>
                            <code>{rule.table_code}</code>
                            <StatusBadge tone="violet">{rule.definition.operator}</StatusBadge>
                            {rule.definition.field && <code>字段: {rule.definition.field}</code>}
                          </div>
                          <p>{rule.definition.rationale}</p>
                          <small className="muted-text">
                            参数：<code>{JSON.stringify(rule.definition.params)}</code> · 规则哈希：
                            <code>{rule.rule_hash.slice(0, 16)}…</code>
                          </small>
                        </div>
                        <div className="segmented">
                          <button
                            type="button"
                            className={decisions[rule.rule_hash] === 'APPROVED' ? 'chosen approve' : ''}
                            onClick={(e) => {
                              e.stopPropagation()
                              setDecisions({ ...decisions, [rule.rule_hash]: 'APPROVED' })
                              setFocusedRuleIndex(Math.min(pending.length - 1, idx + 1))
                            }}
                          >
                            批准 <kbd className="kbd-hint">A</kbd>
                          </button>
                          <button
                            type="button"
                            className={decisions[rule.rule_hash] === 'REJECTED' ? 'chosen reject' : ''}
                            onClick={(e) => {
                              e.stopPropagation()
                              setDecisions({ ...decisions, [rule.rule_hash]: 'REJECTED' })
                              setFocusedRuleIndex(Math.min(pending.length - 1, idx + 1))
                            }}
                          >
                            拒绝 <kbd className="kbd-hint">R</kbd>
                          </button>
                        </div>
                      </div>
                    )
                  })}

                  <div className="approval-submit">
                    <span className="muted-text">将使用右上角「当前操作/审批人」身份签名入账</span>
                    <button
                      type="button"
                      className="button primary"
                      disabled={busy}
                      onClick={submitApprovals}
                    >
                      {busy ? '正在写入事件账本并继续...' : '确认提交审批并推进流水线 →'}
                      <kbd className="kbd-hint">Ctrl+Enter</kbd>
                    </button>
                  </div>
                </section>
              )}

              {active.result && (
                <section className="run-section">
                  <div className="panel-heading" style={{ marginBottom: 8 }}>
                    <div>
                      <h2>编译产物与 Oracle 沙箱实体表预览</h2>
                      <p>点击任意产物或沙箱表可在弹窗中以结构化表格或代码视图查看详情</p>
                    </div>
                  </div>
                  <div className="artifact-actions">
                    {[
                      ['ddl_blob', '查看 MySQL 建表 DDL'],
                      ['oracle_ddl_blob', '查看 Oracle 建表 DDL'],
                      ['validation_sql_blob', '查看确定性校验 SQL'],
                      ['audit_report_blob', '查看独立审计报告'],
                    ]
                      .filter(([key]) => active.result[key])
                      .map(([key, title]) => (
                        <button
                          type="button"
                          className="button secondary"
                          key={key}
                          onClick={() => openArtifact(active.result[key], title)}
                        >
                          {title}
                        </button>
                      ))}
                  </div>

                  {active.result.oracle_tables && (
                    <div className="oracle-tables">
                      {Object.entries(active.result.oracle_tables).map(([code, table]) => (
                        <button
                          type="button"
                          className="oracle-table-btn"
                          key={code}
                          onClick={() => previewOracleTable(code, table)}
                        >
                          <div>
                            <strong>预览沙箱表 · {code}</strong>
                            <small className="muted-text">点击打开二维数据表格视图</small>
                          </div>
                          <code>{table}</code>
                        </button>
                      ))}
                    </div>
                  )}
                </section>
              )}

              {plans.length > 0 && (
                <details className="run-section" open>
                  <summary>AI 生成计划与预算配额 ({plans.length} 张表)</summary>
                  <div className="plan-grid">
                    {plans.map((item) => (
                      <div className="plan-card" key={item.table_code}>
                        <div className="plan-card-header">
                          <strong>{item.table_code}</strong>
                          <StatusBadge tone="blue">AI 规划配额</StatusBadge>
                        </div>
                        <p>{item.plan.rationale}</p>
                        <div className="plan-metrics-pills">
                          <span className="locator-pill">必做规则：{item.plan.mandatory_rule_target}</span>
                          <span className="locator-pill">拓展规则：{item.plan.expanded_rule_target}</span>
                          <span className="locator-pill">黄金样本：{item.plan.golden_count}</span>
                          <span className="locator-pill">每规则变异：{item.plan.mutations_per_rule}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              )}

              <details className="run-section" open>
                <summary>RuleIR 规则与算子清单 ({filteredRules.length} / {active.rules.length})</summary>
                <div className="block-toolbar">
                  <div className="actions">
                    <select
                      value={tableFilter}
                      onChange={(e) => setTableFilter(e.target.value)}
                      aria-label="按表筛选"
                    >
                      <option value="ALL">全部目标表 ({tableCodes.length})</option>
                      {tableCodes.map((code) => (
                        <option key={code} value={code}>
                          {code}
                        </option>
                      ))}
                    </select>
                    <select
                      value={sourceFilter}
                      onChange={(e) => setSourceFilter(e.target.value)}
                      aria-label="按来源筛选"
                    >
                      <option value="ALL">全部规则来源</option>
                      <option value="DOC_EXPLICIT">仅看文档显性规则 ({explicitCount})</option>
                      <option value="AI_EXPANDED">仅看 AI 拓展规则 ({expandedCount})</option>
                    </select>
                  </div>
                </div>

                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>规则标识 / 所属表</th>
                        <th>目标字段</th>
                        <th>规则来源</th>
                        <th>当前状态</th>
                        <th>确定性算子与依据</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredRules.map((rule) => (
                        <tr key={rule.rule_hash}>
                          <td>
                            <strong>{rule.rule_key}</strong>
                            <small>
                              <code>{rule.table_code}</code> · 哈希 {rule.rule_hash.slice(0, 10)}…
                            </small>
                          </td>
                          <td>
                            <code>{rule.definition.field || '跨字段/表级'}</code>
                          </td>
                          <td>
                            <StatusBadge status={rule.source} />
                          </td>
                          <td>
                            <StatusBadge status={rule.status} />
                          </td>
                          <td>
                            <div className="actions" style={{ marginBottom: 4 }}>
                              <code>{rule.definition.operator}</code>
                              <span className="muted-text">{rule.definition.rationale}</span>
                            </div>
                            <details className="event-payload">
                              <summary>查看算子参数与原文证据</summary>
                              <pre>
                                {JSON.stringify(
                                  {
                                    params: rule.definition.params,
                                    evidence: rule.definition.evidence,
                                  },
                                  null,
                                  2
                                )}
                              </pre>
                            </details>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>

              <details className="run-section">
                <summary>不可变事件因果账本 ({active.events.length} 条记录)</summary>
                <div className="timeline">
                  {active.events.map((event) => (
                    <div className="event" key={event.seq}>
                      <span className="event-seq">{String(event.seq).padStart(2, '0')}</span>
                      <div className="event-main">
                        <div className="event-main-top">
                          <strong>{event.kind}</strong>
                          <span className="locator-pill">节点：{event.node}</span>
                        </div>
                        <details className="event-payload">
                          <summary>
                            {event.kind === 'AI_TOOL_ASSIGNED' ? '展开完整 AI 赋值参数' : '展开事件负载'}
                          </summary>
                          <pre>
                            {JSON.stringify(
                              event.kind === 'AI_TOOL_ASSIGNED' ? event.payload.arguments : event.payload,
                              null,
                              2
                            )}
                          </pre>
                        </details>
                      </div>
                      <code title={event.event_hash}>{event.event_hash.slice(0, 12)}…</code>
                    </div>
                  ))}
                </div>
              </details>
            </>
          ) : (
            <div className="empty">请从左侧选择一次运行批次查看详情。</div>
          )}
        </div>
      </section>

      <DataTableModal artifact={artifact} onClose={() => setArtifact(null)} />
    </>
  )
}
