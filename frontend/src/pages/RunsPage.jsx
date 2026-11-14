import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function RunsPage() {
  const [params] = useSearchParams()
  const [runs, setRuns] = useState([])
  const [active, setActive] = useState(null)
  const [plans, setPlans] = useState([])
  const [artifact, setArtifact] = useState(null)
  const [approver, setApprover] = useState('')
  const [decisions, setDecisions] = useState({})
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  async function load(id) {
    const list = await api('/api/runs')
    setRuns(list)
    const target = id || params.get('run') || list[0]?.id
    if (target) select(target)
  }
  useEffect(() => { load().catch((err) => setError(err.message)) }, [])
  async function select(id) {
    const run = await api(`/api/runs/${id}`)
    setActive(run)
    setArtifact(null)
    setPlans(await v2(`/generation/plans/${id}`).catch(() => []))
  }
  async function openArtifact(uri, title) {
    const body = await api(`/api/blobs/${uri.replace('blob://', '')}`)
    setArtifact({ title, content: body.content })
  }
  async function preview(tableCode) {
    const rows = await api(`/api/runs/${active.id}/oracle-preview?table_code=${tableCode}&limit=50`)
    setArtifact({ title: `Oracle · ${tableCode}`, content: JSON.stringify(rows, null, 2) })
  }
  const pending = active?.rules.filter((rule) => rule.status === 'PENDING') || []

  async function submitApprovals() {
    if (!approver.trim()) { setError('请填写审批人。'); return }
    if (pending.some((rule) => !decisions[rule.rule_hash])) { setError('请对全部待审批规则选择批准或拒绝。'); return }
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/runs/${active.id}/approvals`, { method: 'POST', body: JSON.stringify({
        approver,
        provider: 'live',
        decisions: pending.map((rule) => ({ rule_hash: rule.rule_hash, decision: decisions[rule.rule_hash] })),
      }) })
      if (body.detail) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
      setActive(body)
      setDecisions({})
      setNotice('审批已写入事件账本，后续阶段已继续执行。')
      await load(body.id)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function retry() {
    setBusy(true)
    try {
      const body = await api(`/api/runs/${active.id}/retry`, { method: 'POST', body: JSON.stringify({}) })
      setActive(body)
      setNotice('已从失败点重试，AI 会先修正失败资产再执行。')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const summary = active?.result?.audit_summary

  return <>
    <PageHeader title="执行中心" description="每阶段 AI 参数、人工审批、扇出屏障、Oracle 结果和失败回边均可检查。" />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="workspace-grid runs-layout">
      <div className="panel list-panel"><div className="panel-heading"><h2>运行列表</h2><span>{runs.length}</span></div>{runs.map((run) => <button className="list-row" key={run.id} onClick={() => select(run.id)}><strong>{run.id.slice(0, 8)}</strong><span>{run.current_node}</span><em>{run.status}</em></button>)}</div>
      <div className="panel run-detail">{active ? <>
        <div className="panel-heading"><div><h2>{active.status}</h2><p>{active.id} · {active.current_node}</p></div>{summary && <span className={summary.all_passed ? 'badge badge-green' : 'badge badge-amber'}>{summary.case_count} 用例 · {summary.false_positive_count} 误报 · {summary.false_negative_count} 漏报</span>}</div>
        {plans.length > 0 && <details className="run-section" open><summary>AI 生成计划</summary>{plans.map((item) => <div className="plan-card" key={item.table_code}><strong>{item.table_code}</strong><p>{item.plan.rationale}</p><code>必做 {item.plan.mandatory_rule_target} · 拓展 {item.plan.expanded_rule_target} · 黄金 {item.plan.golden_count} · 每规则变异 {item.plan.mutations_per_rule}</code></div>)}</details>}
        <details className="run-section" open><summary>规则与版本</summary><div className="table-wrap"><table><thead><tr><th>规则</th><th>来源</th><th>状态</th><th>算子与参数</th></tr></thead><tbody>{active.rules.map((rule) => <tr key={rule.rule_hash}><td>{rule.rule_key}<small>{rule.table_code}</small></td><td>{rule.source}</td><td>{rule.status}</td><td><details><summary>{rule.definition.operator}</summary><pre>{JSON.stringify(rule.definition.params, null, 2)}</pre></details></td></tr>)}</tbody></table></div></details>
        {pending.length > 0 && <section className="approval-panel"><div className="panel-heading"><div><h2>AI 拓展规则人工确权</h2><p>批准后按当前规则内容哈希生效，参数变化需重新审批。</p></div></div>{pending.map((rule) => <div className="approval-rule" key={rule.rule_hash}><div><strong>{rule.rule_key}</strong><p>{rule.definition.rationale}</p><code>{rule.rule_hash}</code></div><div className="segmented"><button className={decisions[rule.rule_hash] === 'APPROVED' ? 'chosen approve' : ''} onClick={() => setDecisions({ ...decisions, [rule.rule_hash]: 'APPROVED' })}>批准</button><button className={decisions[rule.rule_hash] === 'REJECTED' ? 'chosen reject' : ''} onClick={() => setDecisions({ ...decisions, [rule.rule_hash]: 'REJECTED' })}>拒绝</button></div></div>)}<div className="approval-submit"><input value={approver} onChange={(event) => setApprover(event.target.value)} placeholder="审批人" /><button className="button primary" disabled={busy} onClick={submitApprovals}>写入审批并继续</button></div></section>}
        {active.status === 'FAILED' && <button className="button primary" disabled={busy} onClick={retry}>从失败点重试</button>}
        {active.status === 'COMPLETED' && summary?.all_passed && <div className="production-download"><a className="button primary" href={`/api/v2/generation/runs/${active.id}/production-bundle`}>下载生产部署包（DDL + 校验 SQL）</a><span>正式表名 · 无测试数据 · 无沙箱字段</span></div>}
        {active.result && <div className="artifact-actions">{[['ddl_blob', 'MySQL DDL'], ['oracle_ddl_blob', 'Oracle DDL'], ['validation_sql_blob', '校验 SQL'], ['audit_report_blob', '审计报告']].filter(([key]) => active.result[key]).map(([key, title]) => <button key={key} onClick={() => openArtifact(active.result[key], title)}>{title}</button>)}</div>}
        {active.result?.oracle_tables && <div className="oracle-tables"><p>Oracle 实际数据</p>{Object.entries(active.result.oracle_tables).map(([code, table]) => <button key={code} onClick={() => preview(code)}><span>{code}</span><code>{table}</code></button>)}</div>}
        <details className="run-section" open><summary>事件账本（{active.events.length}）</summary><div className="timeline">{active.events.map((event) => <div className="event" key={event.seq}><span className="event-seq">{event.seq}</span><div className="event-main"><strong>{event.kind}</strong><p>{event.node}</p><details className="event-payload"><summary>{event.kind === 'AI_TOOL_ASSIGNED' ? '完整 AI 赋值' : '事件参数'}</summary><pre>{JSON.stringify(event.kind === 'AI_TOOL_ASSIGNED' ? event.payload.arguments : event.payload, null, 2)}</pre></details></div><code>{event.event_hash.slice(0, 12)}…</code></div>)}</div></details>
      </> : <p className="empty">请选择一次运行。</p>}</div>
    </section>
    {artifact && <div className="modal-backdrop" onClick={() => setArtifact(null)}><section className="modal" onClick={(event) => event.stopPropagation()}><div className="panel-heading"><h2>{artifact.title}</h2><button className="close-button" onClick={() => setArtifact(null)}>关闭</button></div><pre>{artifact.content}</pre></section></div>}
  </>
}
