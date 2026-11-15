import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, v2 } from '../api.js'
import { useOperator } from '../context/OperatorContext.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { DataTableModal, StatusBadge, formatTime } from './SharedUI.jsx'

const PIPELINE_STEPS = [
  { num: '01', label: '生成表结构、码表和规则', match: ['PLAN', 'INIT', 'SCHEMA', 'DDL', 'RULE', 'BINDER', 'ORACLE_ASSET'] },
  { num: '02', label: '确认校验规则', match: ['APPROVAL', 'HUMAN', 'BARRIER'] },
  { num: '03', label: '生成测试数据', match: ['DATA', 'GEN', 'MUTATION'] },
  { num: '04', label: '执行数据库测试', match: ['CASE', 'SQL', 'ORACLE', 'SANDBOX', 'AUDIT'] },
  { num: '05', label: '确认结果并下载', match: ['COMPLETE', 'DONE', 'RETRY', 'EXPORT', 'TEST_CONFIRMATION'] },
]

const STAGE_LABELS = {
  generation_planner: '生成计划',
  schema_mapper: '表结构映射',
  rule_binder: '规则装配',
  data_generator_binder: '测试数据生成',
  audit_case_binder: '审计用例生成',
  oracle_asset_builder: 'Oracle 建表',
  oracle_executor: 'Oracle 测试执行',
  created: '初始化',
}

const ERROR_KIND_LABELS = {
  timeout: '模型响应超时',
  model_error: '模型调用失败',
  validation: '参数校验失败',
  internal: '内部错误',
}

function resolveStepIndex(run) {
  if (!run) return -1
  if (run.status === 'COMPLETED') return 4
  if (run.status === 'WAITING_APPROVAL') return 1
  if (run.status === 'WAITING_TEST_APPROVAL') return 4
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
      setError('请对每条新增规则选择“批准”或“拒绝”，再提交确认。')
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
      setNotice('确认结果已保存，系统将继续生成测试数据并进行数据库验证。')
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

  async function confirmTests() {
    const approver = requireOperator(setError)
    if (!approver) return
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/runs/${active.id}/test-confirmation`, {
        method: 'POST',
        body: JSON.stringify({ approver }),
      })
      setActive(body)
      await load(body.id)
      setNotice('测试结果已确认，现在可以下载生产部署包。')
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
  const outcome = !active ? null : active.status === 'COMPLETED' && summary?.all_passed
    ? { tone: 'success', title: '验证已通过，可以下载部署包', description: '部署包包含 Oracle 建表语句、码值数据和校验规则 SQL。' }
    : active.status === 'WAITING_APPROVAL'
      ? { tone: 'attention', title: `有 ${pending.length} 条新增规则需要您确认`, description: active?.result?.oracle_precheck?.all_passed ? 'Oracle 建表和规则空跑已通过，确认规则后再生成测试数据。' : '表结构、码表和规则 SQL 已生成，确认规则后再生成测试数据。' }
      : active.status === 'WAITING_TEST_APPROVAL'
        ? { tone: 'success', title: '数据库测试已通过，请确认测试结果', description: '确认后才开放生产部署包下载；您也可以先查看验证报告和测试数据。' }
        : active.status === 'FAILED'
        ? { tone: 'danger', title: '本次验证发现问题', description: '请查看验证报告；可以从失败位置重新处理，不需要重新上传文件。' }
        : { tone: 'working', title: '系统正在生成和验证', description: '您可以留在本页查看进度，需要人工确认时系统会提示。' }

  return (
    <>
      <PageHeader
        eyebrow="第 3 步 · 共 3 步"
        title="测试与下载"
        description="生成测试数据并执行 Oracle 测试；确认结果后下载码表、表结构和校验 SQL。"
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
              {active.status === 'WAITING_TEST_APPROVAL' && (
                <button type="button" className="button primary" disabled={busy} onClick={confirmTests}>确认测试结果并开放下载</button>
              )}
              {active.status === 'FAILED' && (
                <button type="button" className="button primary" disabled={busy} onClick={retry}>
                  重新处理失败项
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
            <span>当前状态</span>
            <strong><StatusBadge status={active.status} /></strong>
            <small>{outcome?.title}</small>
          </div>
          <div>
            <span>校验规则</span>
            <strong>{active.rules?.length || 0} 条</strong>
            <small>文档明确 {explicitCount} · 建议新增 {expandedCount}</small>
          </div>
          <div>
            <span>等待确认</span>
            <strong>{pending.length} 条</strong>
            <small>{pending.length > 0 ? `已选择 ${decidedCount} / ${pending.length} 条` : '当前无需人工确认'}</small>
          </div>
          <div>
            <span>数据库验证</span>
            <strong>{summary ? `${summary.case_count} 项测试` : '尚未开始'}</strong>
            <small>{summary ? `${summary.false_positive_count} 项误拦截 · ${summary.false_negative_count} 项漏拦截` : '确认规则后自动执行'}</small>
          </div>
        </section>
      )}

      <section className="workspace-grid runs-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>生成记录</h2>
              <p>共 {runs.length} 个批次</p>
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
                    <strong>生成批次 {run.id.slice(0, 8)}</strong>
                    <StatusBadge status={run.status} />
                  </div>
                  <span>{run.status === 'COMPLETED' ? '可以下载部署包' : PIPELINE_STEPS[resolveStepIndex(run)]?.label}</span>
                  {(run.created_at || run.updated_at) && <em>{formatTime(run.created_at || run.updated_at)}</em>}
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
                    <h2>本次生成结果</h2>
                    <StatusBadge status={active.status} />
                  </div>
                  <p>批次 {active.id.slice(0, 8)}{(active.created_at || active.updated_at) ? ` · ${formatTime(active.created_at || active.updated_at)}` : ''}</p>
                </div>
              </div>

              <section className={`run-outcome ${outcome.tone}`}>
                <strong>{outcome.title}</strong>
                <span>{outcome.description}</span>
              </section>

              {active.status === 'FAILED' && active.result?.error_details && (
                <section className="panel error-detail-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>失败原因</h2>
                      <p className="muted-text">{active.result.error}</p>
                    </div>
                  </div>
                  <div className="error-detail-grid">
                    <div>
                      <span>失败阶段</span>
                      <strong>{STAGE_LABELS[active.result.error_details.stage] || active.result.error_details.stage}</strong>
                    </div>
                    <div>
                      <span>错误类型</span>
                      <strong>{ERROR_KIND_LABELS[active.result.error_details.error_kind] || active.result.error_details.error_kind}</strong>
                    </div>
                    {active.result.error_details.table_code && (
                      <div>
                        <span>报表</span>
                        <strong><code>{active.result.error_details.table_code}</code></strong>
                      </div>
                    )}
                    {active.result.error_details.field && (
                      <div>
                        <span>字段</span>
                        <strong><code>{active.result.error_details.field}</code></strong>
                      </div>
                    )}
                    {active.result.error_details.tool_name && (
                      <div>
                        <span>AI 工具</span>
                        <strong><code>{active.result.error_details.tool_name}</code></strong>
                      </div>
                    )}
                  </div>
                  {active.result.error_details.suggestion && (
                    <p className="error-suggestion">{active.result.error_details.suggestion}</p>
                  )}
                </section>
              )}

              <div className="pipeline-stepper">
                {PIPELINE_STEPS.map((step, idx) => {
                  let stateClass = ''
                  if (active.status === 'COMPLETED' || idx < stepIdx) stateClass = 'done'
                  else if (idx === stepIdx) stateClass = active.status === 'FAILED' ? 'failed' : 'current'
                  return (
                    <div key={step.num} className={`pipeline-step ${stateClass}`}>
                      <span className="pipeline-step-num">步骤 {step.num}</span>
                      <strong>{step.label}</strong>
                    </div>
                  )
                })}
              </div>

              {active.status === 'COMPLETED' && summary?.all_passed && (
                <div className="production-download">
                  <div className="production-download-info">
                    <strong>生产部署包已就绪</strong>
                    <span>包含 Oracle 建表语句、码值数据和校验规则 SQL，不包含测试数据。</span>
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
                      <h2>请确认 AI 建议新增的规则</h2>
                      <p>请根据业务口径选择批准或拒绝；文档明确要求的规则无需在此确认。</p>
                    </div>
                    <div className="actions">
                      <button
                        type="button"
                        className="button secondary sm"
                        onClick={() => setAllDecisions('REJECTED')}
                      >
                        全部拒绝
                      </button>
                      <button
                        type="button"
                        className="button primary sm"
                        onClick={() => setAllDecisions('APPROVED')}
                      >
                        全部批准
                      </button>
                    </div>
                  </div>

                  <details className="technical-details">
                    <summary>查看批量操作快捷键</summary>
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
                  </details>

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
                            <span className="rule-index-pill">{idx + 1}</span>
                            <strong>{rule.definition.field ? `字段 ${rule.definition.field}` : '报表级校验'}</strong>
                            <StatusBadge tone="violet">AI 建议新增</StatusBadge>
                          </div>
                          <p>{rule.definition.rationale}</p>
                          <details className="technical-details">
                            <summary>查看规则技术详情</summary>
                            <pre>{JSON.stringify({ rule_key: rule.rule_key, table_code: rule.table_code, operator: rule.definition.operator, params: rule.definition.params, rule_hash: rule.rule_hash }, null, 2)}</pre>
                          </details>
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
                            批准
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
                            拒绝
                          </button>
                        </div>
                      </div>
                    )
                  })}

                  <div className="approval-submit">
                    <span className="muted-text">确认结果会记录在变更历史中</span>
                    <button
                      type="button"
                      className="button primary"
                      disabled={busy}
                      onClick={submitApprovals}
                    >
                      {busy ? '正在提交并继续...' : '提交确认并继续'}
                    </button>
                  </div>
                </section>
              )}

              {active.result && (
                <section className="run-section">
                  <div className="panel-heading" style={{ marginBottom: 8 }}>
                    <div>
                      <h2>生成结果</h2>
                      <p>查看本次生成的建表语句、校验规则、验证报告和测试数据。</p>
                    </div>
                  </div>
                  <div className="artifact-actions">
                    {[
                      ['ddl_blob', 'MySQL 建表语句'],
                      ['oracle_ddl_blob', 'Oracle 建表语句'],
                      ['validation_sql_blob', '校验规则 SQL'],
                      ['audit_report_blob', '验证报告'],
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
                            <strong>查看测试数据 · {code}</strong>
                            <small className="muted-text">点击查看数据表</small>
                          </div>
                          <code>{table}</code>
                        </button>
                      ))}
                    </div>
                  )}
                </section>
              )}

              {plans.length > 0 && (
                <details className="run-section">
                  <summary>查看生成计划 ({plans.length} 张报表)</summary>
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

              <details className="run-section">
                <summary>查看全部校验规则 ({filteredRules.length} / {active.rules.length})</summary>
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
                <summary>查看技术日志 ({active.events.length} 条)</summary>
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
