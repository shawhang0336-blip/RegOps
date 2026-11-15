import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'

const statusLabels = {
  WAITING_APPROVAL: '等待人工确权',
  COMPLETED: '审计通过',
  FAILED: '需要处理',
  RUNNING: '正在装配',
  AUDITING: '正在审计',
  READY_TO_AUDIT: '等待审计',
}

const sourceLabels = {
  DOC_EXPLICIT: '文档显性',
  AI_EXPANDED: 'AI 拓展',
}

function Badge({ children, tone = 'neutral' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

function App() {
  const [specs, setSpecs] = useState([])
  const [runs, setRuns] = useState([])
  const [selectedSpecId, setSelectedSpecId] = useState('')
  const [specDetail, setSpecDetail] = useState(null)
  const [activeRun, setActiveRun] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [artifact, setArtifact] = useState(null)
  const [provider] = useState('live')
  const [tableFilter, setTableFilter] = useState('ALL')
  const [selectedTables, setSelectedTables] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadRuns = useCallback(async (preferredRunId) => {
    const runList = await api('/api/runs')
    setRuns(runList)
    const targetId = preferredRunId || activeRun?.id || runList[0]?.id
    if (targetId) {
      setActiveRun(await api(`/api/runs/${targetId}`))
    } else {
      setActiveRun(null)
    }
  }, [activeRun?.id])

  const loadSpecs = useCallback(async () => {
    const specList = await api('/api/specs')
    setSpecs(specList)
    const targetId = selectedSpecId || specList.find((spec) => spec.document_kind === 'CATALOG')?.id || specList[0]?.id || ''
    setSelectedSpecId(targetId)
    if (targetId) setSpecDetail(await api(`/api/specs/${targetId}`))
  }, [selectedSpecId])

  useEffect(() => {
    Promise.all([loadSpecs(), loadRuns()]).catch((err) => setError(err.message))
  }, [])

  useEffect(() => {
    if (!activeRun || !['RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(activeRun.status)) return undefined
    const timer = window.setInterval(() => {
      if (!busy) loadRuns(activeRun.id).catch((err) => setError(err.message))
    }, 10000)
    return () => window.clearInterval(timer)
  }, [activeRun?.id, activeRun?.status, busy, loadRuns])

  async function selectSpec(id) {
    setSelectedSpecId(id)
    setSelectedTables([])
    setSpecDetail(id ? await api(`/api/specs/${id}`) : null)
  }

  async function selectRun(id) {
    setError('')
    setActiveRun(await api(`/api/runs/${id}`))
    setDecisions({})
    setArtifact(null)
  }

  async function importSpec() {
    setBusy(true)
    setError('')
    try {
      const spec = await api('/api/specs/import-safe-13', { method: 'POST', body: JSON.stringify({}) })
      setNotice(`已发现 ${spec.table_count} 个表候选，${spec.eligible_count} 个符合第一步。请先确认表范围，再启动 AI。`)
      await loadSpecs()
      await selectSpec(spec.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function uploadDocument(event) {
    const file = event.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    setBusy(true)
    setError('')
    try {
      const catalog = await api('/api/documents', { method: 'POST', body: form })
      setNotice(`文件已解析：发现 ${catalog.table_count} 个表候选，${catalog.eligible_count} 个符合第一步。请确认范围后再运行。`)
      await loadSpecs()
      await selectSpec(catalog.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
      event.target.value = ''
    }
  }

  async function confirmTables() {
    if (!selectedTables.length) {
      setError('请选择至少一张符合条件的表。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const confirmed = await api(`/api/specs/${selectedSpecId}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ table_codes: selectedTables }),
      })
      setNotice(`已确认 ${confirmed.table_count} 张表。只有这个不可变快照可以进入后续 AI 装配和 Oracle 建表。`)
      await loadSpecs()
      await selectSpec(confirmed.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function createRun() {
    if (!selectedSpecId) return
    setBusy(true)
    setError('')
    try {
      const run = await api('/api/runs', {
        method: 'POST',
        body: JSON.stringify({ spec_id: selectedSpecId, provider }),
      })
      setNotice('真实 GLM 已完成本阶段结构化赋值；完整工具参数已写入事件账本。')
      await loadRuns(run.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const pendingRules = useMemo(
    () => activeRun?.rules.filter((rule) => rule.status === 'PENDING') || [],
    [activeRun],
  )

  const filteredRules = useMemo(
    () => activeRun?.rules.filter((rule) => tableFilter === 'ALL' || rule.table_code === tableFilter) || [],
    [activeRun, tableFilter],
  )

  function setAllDecisions(decision) {
    setDecisions(Object.fromEntries(pendingRules.map((rule) => [rule.rule_hash, decision])))
  }

  async function submitDecisions() {
    const payload = pendingRules.map((rule) => ({
      rule_hash: rule.rule_hash,
      decision: decisions[rule.rule_hash],
    }))
    if (payload.some((item) => !item.decision)) {
      setError('请对每条待确权规则选择批准或拒绝。')
      return
    }
    setBusy(true)
    setError('')
    try {
      await api(`/api/runs/${activeRun.id}/approvals`, {
        method: 'POST',
        body: JSON.stringify({ decisions: payload, provider }),
      })
      setNotice('审批事实已入账，运行状态已由事件流重新推进。')
      setDecisions({})
      await loadRuns(activeRun.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function retryRun() {
    setBusy(true)
    setError('')
    try {
      await api(`/api/runs/${activeRun.id}/retry`, { method: 'POST', body: JSON.stringify({}) })
      setNotice('已从持久化设计重试；若审计仍有差异，程序会自动回边要求 AI 修正规版本。')
      await loadRuns(activeRun.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function openArtifact(uri, title) {
    const digest = uri.replace('blob://', '')
    const blob = await api(`/api/blobs/${digest}`)
    setArtifact({ title, content: blob.content })
  }

  async function openOracleData(tableCode) {
    const rows = await api(`/api/runs/${activeRun.id}/oracle-preview?table_code=${tableCode}&limit=50`)
    setArtifact({ title: `Oracle 合成数据 · ${tableCode}`, content: JSON.stringify(rows, null, 2) })
  }

  const explicitCount = activeRun?.rules.filter((rule) => rule.source === 'DOC_EXPLICIT').length || 0
  const audit = activeRun?.result?.audit_summary

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">R</div>
          <div><strong>RegOps</strong><span>监管规则装配台</span></div>
        </div>
        <nav className="nav-list">
          <a className="active" href="#rules"><span>01</span>规则装配</a>
          <a href="#approval"><span>02</span>人工确权</a>
          <a href="#audit"><span>03</span>独立审计</a>
          <a href="#events"><span>04</span>事件账本</a>
        </nav>
        <div className="sidebar-bottom">
          <div className="environment-dot" />
          <div><strong>{provider === 'scripted' ? '离线剧本模式' : '真实模型模式'}</strong><span>{provider === 'scripted' ? '不调用真实 LLM' : '使用服务端模型配置'}</span></div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div>
            <p className="eyebrow">SAFE 1.3 / CHAPTER 6</p>
            <h1>通用监管规则装配与 Oracle 审计</h1>
            <p className="subtitle">Schema、规则、造数、审计均由 GLM 结构化赋值；多阶段扇出与全部参数透明可查。</p>
          </div>
          <div className="actions">
            <span className="badge badge-violet">GLM-5.3-Flash · 真实调用</span>
            <label className="button secondary">上传 PDF<input className="file-input" type="file" accept="application/pdf" disabled={busy} onChange={uploadDocument} /></label>
            <button className="button secondary" disabled={busy} onClick={importSpec}>解析内置 1.3</button>
            <button className="button primary" disabled={busy || !specDetail?.selection_confirmed} onClick={createRun}>启动真实 AI 装配</button>
          </div>
        </header>

        {error && <div className="message error" role="alert">{error}</div>}
        {notice && <div className="message notice">{notice}<button onClick={() => setNotice('')}>关闭</button></div>}

        <section className="workspace-grid">
          <div className="panel spec-panel">
            <div className="panel-heading"><span>规范快照</span><Badge tone="blue">真实 PDF</Badge></div>
            {specs.length > 0 ? (
              <select value={selectedSpecId} onChange={(event) => selectSpec(event.target.value)}>
                {specs.map((spec) => <option key={spec.id} value={spec.id}>{spec.table_name}</option>)}
              </select>
            ) : <p className="empty">尚未导入，点击“解析 1.3 规范”开始。</p>}
            {specDetail && (
              <>
                <h2>{specDetail.table_name}</h2>
                <p>{specDetail.table_code}</p>
                <dl className="spec-meta">
                  <div><dt>字段白名单</dt><dd>{specDetail.field_count}</dd></div>
                  <div><dt>规范版本</dt><dd>{specDetail.version}</dd></div>
                  <div><dt>来源校验</dt><dd title={specDetail.source_sha256}>SHA-256 · {specDetail.source_sha256.slice(0, 10)}…</dd></div>
                </dl>
                {specDetail.document_kind === 'CATALOG' && <>
                  <div className="catalog-summary"><Badge tone="green">{specDetail.eligible_count} 个符合第一步</Badge><span>共 {specDetail.table_count} 个候选</span></div>
                  <div className="catalog-list">{specDetail.tables.map((table) => <label className={`catalog-item ${table.eligible ? '' : 'ineligible'}`} key={table.table_code}>
                    <input type="checkbox" disabled={!table.eligible} checked={selectedTables.includes(table.table_code)} onChange={(event) => setSelectedTables(event.target.checked ? [...selectedTables, table.table_code] : selectedTables.filter((code) => code !== table.table_code))} />
                    <span><strong>{table.table_name}</strong><small>{table.eligible ? `${table.field_count} 字段 · ${table.reason}` : table.reason}</small></span>
                  </label>)}</div>
                  <div className="confirm-bar"><button className="button primary" disabled={busy || !selectedTables.length} onClick={confirmTables}>确认 {selectedTables.length} 张表</button></div>
                </>}
                {specDetail.document_kind !== 'CATALOG' && specDetail.tables && <div className="table-chip-list">{specDetail.tables.map((table) => <div key={table.table_code}><strong>{table.table_name}</strong><span>{table.field_count} 字段</span></div>)}</div>}
                <details className="field-drawer">
                  <summary>查看 {specDetail.field_count} 个真实字段</summary>
                  <div className="field-cloud">{specDetail.fields.map((field) => <code key={field.name} title={field.check_text}>{field.name}</code>)}</div>
                </details>
              </>
            )}
          </div>

          <div className="panel runs-panel">
            <div className="panel-heading"><span>最近运行</span><span className="count">{runs.length}</span></div>
            <div className="run-list">
              {runs.map((run) => (
                <button key={run.id} className={`run-item ${activeRun?.id === run.id ? 'selected' : ''}`} onClick={() => selectRun(run.id)}>
                  <span className={`status-dot ${run.status === 'COMPLETED' ? 'success' : run.status === 'WAITING_APPROVAL' ? 'warning' : ''}`} />
                  <span><strong>{run.id.slice(0, 8)}</strong><small>{run.current_node}</small></span>
                  <Badge tone={run.status === 'COMPLETED' ? 'green' : run.status === 'WAITING_APPROVAL' ? 'amber' : 'neutral'}>{statusLabels[run.status] || run.status}</Badge>
                </button>
              ))}
              {!runs.length && <p className="empty">导入规范后创建第一次运行。</p>}
            </div>
          </div>
        </section>

        {activeRun && (
          <>
            <section className="metrics" id="rules">
              <div><span>运行状态</span><strong>{statusLabels[activeRun.status] || activeRun.status}</strong><small>{activeRun.current_node}</small></div>
              <div><span>显性规则</span><strong>{explicitCount}</strong><small>原文证据自动绑定</small></div>
              <div><span>待确权</span><strong>{pendingRules.length}</strong><small>按内容哈希审批</small></div>
              <div><span>独立审计</span><strong>{audit ? `${audit.case_count} 例` : '—'}</strong><small>{audit ? `${audit.backend} · ${audit.all_passed ? '全部通过' : '存在差异'}` : '尚未执行'}</small></div>
            </section>

            <section className="panel rules-panel">
              <div className="panel-heading">
                <div><h2>RuleIR 规则清单</h2><p>运行 {activeRun.id}</p></div>
                <div className="actions"><select className="provider-select" aria-label="按表筛选规则" value={tableFilter} onChange={(event) => setTableFilter(event.target.value)}><option value="ALL">全部表</option>{[...new Set(activeRun.rules.map((rule) => rule.table_code))].map((code) => <option key={code} value={code}>{code}</option>)}</select><Badge tone={activeRun.status === 'COMPLETED' ? 'green' : 'blue'}>{filteredRules.length} / {activeRun.rules.length} 条规则</Badge></div>
              </div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>规则 / 算子</th><th>目标字段</th><th>来源</th><th>状态</th><th>证据与参数</th></tr></thead>
                  <tbody>
                    {filteredRules.map((rule) => (
                      <tr key={rule.rule_hash}>
                        <td><strong>{rule.rule_key}</strong><small>{rule.table_code} · {rule.definition.operator}</small></td>
                        <td><code>{rule.definition.field}</code></td>
                        <td><Badge tone={rule.source === 'AI_EXPANDED' ? 'violet' : 'blue'}>{sourceLabels[rule.source]}</Badge></td>
                        <td>{rule.status}</td>
                        <td>
                          <details>
                            <summary>查看</summary>
                            <div className="evidence-popover">
                              <p>{rule.definition.rationale}</p>
                              <pre>{JSON.stringify(rule.definition.params, null, 2)}</pre>
                              <blockquote>{rule.definition.evidence.quote}</blockquote>
                              <small>{rule.definition.evidence.section} · p.{rule.definition.evidence.page} · {rule.rule_hash.slice(0, 16)}…</small>
                            </div>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            {pendingRules.length > 0 && activeRun.status === 'WAITING_APPROVAL' && (
              <section className="panel approval-panel" id="approval">
                <div className="approval-title">
                  <div><p className="eyebrow">HUMAN GATE</p><h2>拓展规则人工确权</h2><p>批准的是当前规则内容哈希；任何参数变化都会生成新的待审批版本。</p></div>
                  <div className="actions"><button className="button secondary" onClick={() => setAllDecisions('REJECTED')}>全部拒绝</button><button className="button primary" onClick={() => setAllDecisions('APPROVED')}>全部批准</button></div>
                </div>
                {pendingRules.map((rule) => (
                  <div className="approval-rule" key={rule.rule_hash}>
                    <div><strong>{rule.rule_key}</strong><p>{rule.definition.rationale}</p><code>{rule.rule_hash}</code></div>
                    <div className="segmented">
                      <button className={decisions[rule.rule_hash] === 'APPROVED' ? 'chosen approve' : ''} onClick={() => setDecisions({ ...decisions, [rule.rule_hash]: 'APPROVED' })}>批准</button>
                      <button className={decisions[rule.rule_hash] === 'REJECTED' ? 'chosen reject' : ''} onClick={() => setDecisions({ ...decisions, [rule.rule_hash]: 'REJECTED' })}>拒绝</button>
                    </div>
                  </div>
                ))}
                <div className="approval-submit"><button className="button primary" disabled={busy} onClick={submitDecisions}>写入审批并继续</button></div>
              </section>
            )}

            {activeRun.status === 'FAILED' && <section className="panel audit-panel" id="audit"><div className="panel-heading"><div><h2>运行未完成</h2><p>失败事实已保留，可在事件账本查看此前所有 AI 赋值。</p></div><Badge tone="amber">FAILED</Badge></div><pre className="failure-detail">{activeRun.result?.error || 'Oracle 审计存在差异。请查看审计报告，或从失败点重试并让 AI 自动修正。'}</pre><button className="button primary" disabled={busy} onClick={retryRun}>从失败点重试</button></section>}
            {activeRun.result && !activeRun.result.error && (
              <section className="panel audit-panel" id="audit">
                <div className="panel-heading"><div><h2>独立沙箱审计</h2><p>{audit?.coverage_statement}</p></div><Badge tone={audit?.all_passed ? 'green' : 'amber'}>{audit?.all_passed ? '用例集通过' : '存在差异'}</Badge></div>
                {audit && <div className="audit-numbers"><span><strong>{audit.case_count}</strong> 独立用例</span><span><strong>{audit.false_positive_count}</strong> 误报</span><span><strong>{audit.false_negative_count}</strong> 漏报</span><span><strong>{audit.active_rule_count}</strong> 生效规则</span></div>}
                <div className="artifact-actions">
                  <button onClick={() => openArtifact(activeRun.result.ddl_blob, 'MySQL 建表 DDL')}>查看 MySQL DDL</button>
                  {activeRun.result.oracle_ddl_blob && <button onClick={() => openArtifact(activeRun.result.oracle_ddl_blob, 'Oracle 目标表 DDL')}>查看 Oracle DDL</button>}
                  <button onClick={() => openArtifact(activeRun.result.validation_sql_blob, '实际执行的校验 SQL')}>查看校验 SQL</button>
                  <button onClick={() => openArtifact(activeRun.result.audit_report_blob, '独立审计报告')}>查看审计报告</button>
                </div>
                {activeRun.result.oracle_tables && <div className="oracle-tables"><p>Oracle 实体沙箱表 · 点击查看实际合成数据</p>{Object.entries(activeRun.result.oracle_tables).map(([code, physical]) => <button key={code} onClick={() => openOracleData(code)}><span>{code}</span><code>{physical}</code></button>)}</div>}
              </section>
            )}

            <section className="panel events-panel" id="events">
              <div className="panel-heading"><div><h2>事件因果账本</h2><p>每次读取都会重放事件并校验运行投影。</p></div><Badge>{activeRun.events.length} 个事实</Badge></div>
              <div className="timeline">
                {activeRun.events.map((event) => <div className="event" key={event.seq}><span className="event-seq">{String(event.seq).padStart(2, '0')}</span><div className="event-main"><strong>{event.kind}</strong><p>{event.node}{event.kind === 'BSP_FANOUT' ? ` · ${event.payload.table_codes.length} 张表并发` : ''}{event.kind === 'BSP_BARRIER_JOIN' && event.payload.table_results ? ` · ${event.payload.table_results.length} 个结果按键归约` : ''}</p><details className="event-payload"><summary>{event.kind === 'AI_TOOL_ASSIGNED' ? '查看完整 AI 赋值' : '查看事件参数'}</summary><pre>{JSON.stringify(event.kind === 'AI_TOOL_ASSIGNED' ? event.payload.arguments : event.payload, null, 2)}</pre></details></div><code title={event.event_hash}>{event.event_hash.slice(0, 12)}…</code></div>)}
              </div>
            </section>
          </>
        )}
      </main>

      {artifact && <div className="modal-backdrop" onClick={() => setArtifact(null)}><section className="modal" onClick={(event) => event.stopPropagation()}><div className="panel-heading"><h2>{artifact.title}</h2><button className="close-button" onClick={() => setArtifact(null)}>关闭</button></div><pre>{artifact.content}</pre></section></div>}
    </div>
  )
}

export default App
