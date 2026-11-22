import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, v2 } from '../api.js'
import { useEventStream } from '../hooks/useEventStream.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge, formatTime } from './SharedUI.jsx'
import {
  ACTIVE_RUN_STATUSES,
  GENERATION_FILTERS,
  POLL_INTERVAL_MS,
  READINESS_FILTERS,
  runLabel,
  runTone,
} from './artifacts/artifactHelpers.js'
import { ArtifactDetail, ProductStatusBar } from './artifacts/ArtifactDetail.jsx'
import { AllRulesView, RulesView } from './artifacts/RulesViews.jsx'

export default function ArtifactsPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const linkedTableId = params.get('table')

  const [tables, setTables] = useState([])
  const [readinessById, setReadinessById] = useState({})
  const [selected, setSelected] = useState([])
  const [currentId, setCurrentId] = useState(linkedTableId || '')
  const [detail, setDetail] = useState(null)
  const [overview, setOverview] = useState(null)
  const [focusMode, setFocusMode] = useState(Boolean(linkedTableId))
  const [workspaceTab, setWorkspaceTab] = useState(params.get('view') === 'rules' || linkedTableId ? 'rules' : 'list')
  const [artifactView, setArtifactView] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [preflight, setPreflight] = useState(null)
  const [reauditBusy, setReauditBusy] = useState(false)
  const [statusFilter, setStatusFilter] = useState('all')
  const [readinessFilter, setReadinessFilter] = useState('all')
  const [docFilter, setDocFilter] = useState('all')
  const [search, setSearch] = useState('')
  const requestRef = useRef(0)
  const tableWrapRef = useRef(null)
  const listScrollRef = useRef(0)

  async function load(preferredId) {
    const [data, readinessPayload, rulesOverview] = await Promise.all([
      v2('/artifacts'),
      v2('/metadata/readiness').catch(() => null),
      v2('/artifacts/rules').catch(() => null),
    ])
    setTables(data.tables || [])
    if (rulesOverview) setOverview(rulesOverview)
    const map = Object.fromEntries(
      (readinessPayload?.tables || []).map((item) => [item.table_id, item])
    )
    setReadinessById(map)
    const target = preferredId || linkedTableId || currentId || data.tables.find((item) => item.generated && item.rule_count > 0)?.id
    if (target) await select(target)
  }

  async function select(id, options = {}) {
    const requestId = ++requestRef.current
    setCurrentId(id)
    if (!options.background) {
      setDetailLoading(true)
      setArtifactView(null)
    }
    try {
      const data = await v2(`/artifacts/${id}`)
      if (requestId === requestRef.current) {
        setDetail(data)
        setRefreshedAt(new Date())
      }
    } catch (err) {
      if (requestId === requestRef.current) setError(err.message)
    } finally {
      if (requestId === requestRef.current) setDetailLoading(false)
    }
  }

  async function refreshSummary() {
    const data = await v2('/artifacts').catch(() => null)
    if (data) {
      const nextTables = data.tables || []
      setTables(nextTables)
      const validIds = new Set(nextTables.map((row) => row.id))
      setSelected((prev) => prev.filter((id) => validIds.has(id)))
    }
  }

  function openMetadataTable(tableId, documentId = '') {
    const resolvedDocId = documentId || tables.find((row) => row.id === tableId)?.document_id || ''
    const qs = new URLSearchParams({ view: 'fields', table: tableId })
    if (resolvedDocId) qs.set('document_id', resolvedDocId)
    navigate(`/documents?${qs.toString()}`)
  }

  async function refreshOverview() {
    const data = await v2('/artifacts/rules').catch(() => null)
    if (data) {
      setOverview(data)
      setRefreshedAt(new Date())
    }
  }

  async function manualRefresh() {
    setRefreshing(true)
    try {
      const tasks = [refreshSummary(), refreshOverview()]
      if (focusMode && currentId) tasks.push(select(currentId, { background: true }))
      await Promise.all(tasks)
    } catch (err) {
      setError(err.message)
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    load().catch((err) => setError(err.message)).finally(() => setLoading(false))
  }, [])

  const runStatus = detail?.run?.status
  const runActive = Boolean(runStatus && ACTIVE_RUN_STATUSES.includes(runStatus))

  const runTopics = 'node.enter,node.exit,node.redirect,node.error,wave.barrier,tool.rejected,run.interrupted,run.completed,run.failed,event.appended'

  // 聚焦单表且还在生成/空跑 → 订阅 Bus SSE 实时刷新（附低频轮询兜底）
  useEventStream({
    enabled: Boolean(currentId && runActive && focusMode),
    runId: detail?.run?.id || '',
    topics: runTopics,
    fallbackIntervalMs: POLL_INTERVAL_MS,
    onEvent: () => select(currentId, { background: true }).catch(() => {}),
    onFallbackTick: () => select(currentId, { background: true }).catch(() => {}),
  })

  // 总览下任一表还在生成 → 订阅 Bus SSE 实时刷新总览（附低频轮询兜底）
  const overviewActive = Boolean(overview?.tables?.some((item) => ACTIVE_RUN_STATUSES.includes(item.run?.status)))
  useEventStream({
    enabled: overviewActive,
    topics: runTopics,
    fallbackIntervalMs: POLL_INTERVAL_MS,
    onEvent: () => refreshOverview().catch(() => {}),
    onFallbackTick: () => refreshOverview().catch(() => {}),
  })

  // 跑完（或进入待确认）时同步刷新一次清单，让报表清单里的规则条数跟上
  const prevRunStatusRef = useRef(null)
  useEffect(() => {
    const previous = prevRunStatusRef.current
    prevRunStatusRef.current = runStatus || null
    if (!previous || previous === runStatus) return
    if (ACTIVE_RUN_STATUSES.includes(previous) && runStatus && !ACTIVE_RUN_STATUSES.includes(runStatus)) {
      refreshSummary()
      refreshOverview()
    }
  }, [runStatus])

  useEffect(() => {
    if (workspaceTab === 'list' && tableWrapRef.current) {
      tableWrapRef.current.scrollTop = listScrollRef.current
    }
  }, [workspaceTab])

  const documents = useMemo(() => {
    const map = new Map()
    tables.forEach((item) => {
      if (item.document_id) map.set(item.document_id, item.document_name || item.document_id)
    })
    return [...map.entries()]
  }, [tables])

  const codeProblems = (section) => section?.defects || []

  function selectable(id) {
    const item = readinessById[id]
    if (!Object.keys(readinessById).length || !item) return null
    if (tables.find((row) => row.id === id)?.status === 'NEEDS_REVIEW') return false
    return item.ddl.ready && item.rule_materials.ready && codeProblems(item.code_sets).length === 0
  }

  function blockReason(id) {
    const row = tables.find((item) => item.id === id)
    if (row?.status === 'NEEDS_REVIEW') return '有违反原文的字段问题，请先去元数据页处理标红项'
    const item = readinessById[id]
    if (!item) return ''
    if (!item.ddl.ready) return item.ddl.blockers[0]?.message || '字段定义无法落成 Oracle 表'
    const hard = [...(item.rule_materials.blockers || []), ...codeProblems(item.code_sets)]
    return hard[0]?.message || ''
  }

  const selectableCount = useMemo(() => {
    const ok = tables.filter((row) => selectable(row.id) === true).length
    const bad = tables.filter((row) => selectable(row.id) === false).length
    return { ok, bad, pending: tables.length - ok - bad }
  }, [tables, readinessById])

  const filteredTables = useMemo(() => {
    return tables.filter((row) => {
      const available = selectable(row.id)
      if (readinessFilter === 'selectable' && available !== true) return false
      if (readinessFilter === 'blocked' && available !== false) return false
      if (readinessFilter === 'code_issues' && !codeProblems(readinessById[row.id]?.code_sets).length) return false
      if (statusFilter === 'ungenerated' && row.generated) return false
      if (statusFilter === 'running' && !(row.generated && !['COMPLETED', 'FAILED'].includes(row.run_status))) return false
      if (statusFilter === 'verified' && !(row.run_status === 'COMPLETED' && row.all_passed)) return false
      if (statusFilter === 'failed' && row.run_status !== 'FAILED') return false
      if (docFilter !== 'all' && row.document_id !== docFilter) return false
      const q = search.trim().toLowerCase()
      if (q && !String(row.table_name || '').toLowerCase().includes(q) && !String(row.table_code || '').toLowerCase().includes(q)) return false
      return true
    })
  }, [tables, statusFilter, readinessFilter, docFilter, search, readinessById])

  function toggle(id) {
    if (selected.includes(id)) {
      setSelected(selected.filter((item) => item !== id))
      return
    }
    if (selectable(id) !== true) return
    setSelected([...selected, id])
  }

  function openTable(id) {
    setFocusMode(true)
    setWorkspaceTab('rules')
    select(id)
  }

  function showRules() {
    setArtifactView(null)
    window.requestAnimationFrame(() => {
      document.getElementById('rule-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

  async function generate(tableIds, options = {}) {
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
        body: JSON.stringify({ table_ids: ids, provider: 'live', accept_warnings: Boolean(options.acceptWarnings) }),
      })
      setPreflight(null)
      setNotice('已启动生成，正在跳转执行中心。')
      navigate(`/runs?run=${run.id}`)
    } catch (err) {
      // 软告警（外部码表引用、非必填项无校验原文等）需要人工确认后重发
      if (err.status === 409 && err.detail && typeof err.detail === 'object' && err.detail.requires_ack) {
        setPreflight({ tableIds: ids, detail: err.detail })
      } else {
        setError(err.message)
      }
    } finally {
      setBusy(false)
    }
  }

  async function reaudit() {
    if (!detail?.run) return
    setReauditBusy(true)
    setError('')
    try {
      await api(`/api/runs/${detail.run.id}/reaudit`, { method: 'POST' })
      setNotice('已发起重新验证：规则保持不动（含手工修改），只重跑 AI 造数与 Oracle 验证。')
      select(currentId, { background: true })
      refreshOverview()
    } catch (err) {
      setError(err.message)
    } finally {
      setReauditBusy(false)
    }
  }

  const currentRow = tables.find((item) => item.id === currentId)
  const currentSelectable = detail ? selectable(detail.table.id) : null
  const currentBlockReason = detail ? blockReason(detail.table.id) : ''

  return (
    <>
      <PageHeader
        eyebrow="第 2 步 · 共 3 步"
        title="规则工作台"
        description="先在报表清单中筛选和勾选，再逐表检查校验规则。表结构、码表和验证结果集中在当前报表的产物状态中。"
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

      {preflight && (
        <div className="action-callout preflight-callout">
          <div>
            <strong>继续生成前请确认以下风险</strong>
            <p>{preflight.detail.message}</p>
            <ul>
              {(preflight.detail.tables || []).map((item) => (
                <li key={item.table_code}>
                  <code>{item.table_code}</code>：{(item.blockers || []).map((blocker) => blocker.message).join('；')}
                </li>
              ))}
            </ul>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={() => setPreflight(null)}>返回处理</button>
            <button
              type="button"
              className="button primary sm"
              disabled={busy}
              onClick={() => generate(preflight.tableIds, { acceptWarnings: true })}
            >
              确认已知风险并继续生成
            </button>
          </div>
        </div>
      )}

      <nav className="tab-bar workspace-main-tabs" aria-label="规则工作台视图" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={workspaceTab === 'list'}
          className={workspaceTab === 'list' ? 'active' : ''}
          onClick={() => setWorkspaceTab('list')}
        >
          报表清单 <span>{tables.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={workspaceTab === 'rules'}
          className={workspaceTab === 'rules' ? 'active' : ''}
          onClick={() => { setFocusMode(false); setWorkspaceTab('rules') }}
        >
          校验规则 {overview ? <span>{overview.tables.length} 张表</span> : currentRow ? <span>{currentRow.table_code}</span> : <span>未选表</span>}
        </button>
      </nav>

      {workspaceTab === 'list' && (
        <section className="panel artifact-list-panel" role="tabpanel">
          <div className="panel-heading">
            <div>
              <h2>报表清单</h2>
              <p>产物状态来自最近一次生成；是否可选由当前元数据、码表和 Oracle 约束共同决定。</p>
            </div>
            <div className="report-counts" aria-label="报表可选统计">
              <span>可选 <strong>{selectableCount.ok}</strong></span>
              <span>不可选 <strong>{selectableCount.bad}</strong></span>
              {selectableCount.pending > 0 && <span>评估中 <strong>{selectableCount.pending}</strong></span>}
              <span>已选 <strong>{selected.length}</strong></span>
            </div>
          </div>

          <div className="list-filter-bar report-filter-bar">
            <input
              type="search"
              placeholder="检索报表名称或表代码..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <select aria-label="可选状态" value={readinessFilter} onChange={(e) => setReadinessFilter(e.target.value)}>
              {READINESS_FILTERS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            <select aria-label="生成状态" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              {GENERATION_FILTERS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            {documents.length > 1 && (
              <select aria-label="来源文件" value={docFilter} onChange={(e) => setDocFilter(e.target.value)}>
                <option value="all">全部文件</option>
                {documents.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
              </select>
            )}
          </div>

          {loading ? (
            <div className="empty">正在加载报表清单...</div>
          ) : (
            <>
              <div
                className="table-wrap report-table-wrap"
                ref={tableWrapRef}
                onScroll={(event) => { listScrollRef.current = event.currentTarget.scrollTop }}
              >
                <table className="data-table report-table">
                  <thead>
                    <tr>
                      <th className="selection-column">选择</th>
                      <th>报表</th>
                      <th>表结构 DDL</th>
                      <th>码表</th>
                      <th>校验规则</th>
                      <th>最近生成</th>
                      <th>可选状态与原因</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredTables.map((row) => {
                      const available = selectable(row.id)
                      const reason = blockReason(row.id)
                      return (
                        <tr key={row.id} className={currentId === row.id ? 'current-report' : ''}>
                          <td className="selection-column">
                            <input
                              type="checkbox"
                              aria-label={`选择 ${row.table_name}`}
                              checked={selected.includes(row.id)}
                              disabled={available !== true}
                              title={available === false ? reason : available === null ? '就绪度评估中' : '加入本次生成'}
                              onChange={() => toggle(row.id)}
                            />
                          </td>
                          <td className="report-name-cell">
                            <button type="button" className="report-name-button" onClick={() => openTable(row.id)}>
                              {row.table_name}
                            </button>
                            <small><code>{row.table_code}</code></small>
                          </td>
                          <td>
                            <StatusBadge tone={!row.generated ? 'neutral' : row.ddl_generated ? 'green' : 'red'}>
                              {!row.generated ? '待生成' : row.ddl_generated ? '已生成' : '未生成'}
                            </StatusBadge>
                          </td>
                          <td>
                            <StatusBadge tone={!row.generated ? 'neutral' : row.code_set_count > 0 ? 'green' : 'neutral'}>
                              {!row.generated ? '待生成' : row.code_set_count > 0 ? `${row.code_set_count} 张已迁移` : '无关联码表'}
                            </StatusBadge>
                          </td>
                          <td>
                            <StatusBadge tone={!row.generated ? 'neutral' : row.rule_count > 0 ? 'green' : 'red'}>
                              {!row.generated ? '待生成' : `${row.rule_count || 0} 条`}
                            </StatusBadge>
                          </td>
                          <td>
                            <StatusBadge tone={runTone(row)}>{runLabel(row)}</StatusBadge>
                            {row.created_at && <small>{formatTime(row.created_at)}</small>}
                          </td>
                          <td className="availability-cell">
                            <StatusBadge tone={available === true ? 'green' : available === false ? 'red' : 'blue'}>
                              {available === true ? '可选' : available === false ? '不可选' : '评估中'}
                            </StatusBadge>
                            {available === false && (
                              <>
                                <small className="danger-text" title={reason}>{reason || '存在未处理的生成阻断项'}</small>
                                <button type="button" className="button-link" onClick={() => openMetadataTable(row.id, row.document_id)}>
                                  定位问题
                                </button>
                              </>
                            )}
                          </td>
                          <td>
                            <button type="button" className="button secondary sm" onClick={() => openTable(row.id)}>
                              {row.generated ? '查看规则' : '查看状态'}
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                {filteredTables.length === 0 && <div className="empty table-empty">没有符合筛选条件的报表。</div>}
              </div>
              <div className="selection-footer">
                <span>当前筛选 {filteredTables.length} 张，已选择 {selected.length} 张。切换视图不会清空勾选和筛选条件。</span>
                <button type="button" className="button primary" disabled={busy || !selected.length} onClick={() => generate()}>
                  {busy ? '正在启动...' : `生成所选报表 (${selected.length})`}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {workspaceTab === 'rules' && (
        <section className={`panel rule-workbench-panel tab-panel-stage ${focusMode && detailLoading && detail ? 'panel-transitioning' : ''}`} role="tabpanel">
          {!focusMode ? (
            <AllRulesView
              overview={overview}
              loading={loading && !overview}
              refreshedAt={refreshedAt}
              refreshing={refreshing}
              onRefresh={manualRefresh}
              onFocus={(id) => { setFocusMode(true); select(id) }}
              onNotice={setNotice}
              onError={setError}
              onSaved={refreshOverview}
              onGoList={() => setWorkspaceTab('list')}
            />
          ) : detailLoading && !detail ? (
            <div className="empty">正在加载报表产物...</div>
          ) : !detail ? (
            <div className="empty">
              <p>这张表还没有产物数据。</p>
              <button type="button" className="button secondary" onClick={() => setFocusMode(false)}>查看全部规则</button>
              <button type="button" className="button primary" onClick={() => setWorkspaceTab('list')}>返回报表清单</button>
            </div>
          ) : (
            <>
              <div className="panel-heading rule-page-heading">
                <div>
                  <button type="button" className="button-link back-to-list" onClick={() => setFocusMode(false)}>查看全部规则</button>
                  <button type="button" className="button-link back-to-list" onClick={() => setWorkspaceTab('list')}>返回报表清单</button>
                  <h2>{detail.table.table_name}</h2>
                  <p>
                    <code>{detail.table.table_code}</code> · {detail.table.field_count} 个字段
                    {detail.run && ` · 最近生成 ${formatTime(detail.run.created_at)}`}
                  </p>
                </div>
                <div className="actions">
                  {refreshedAt && (
                    <small className="refresh-stamp">
                      {runActive && <span className="live-dot" aria-hidden="true" />}
                      更新于 {refreshedAt.toLocaleTimeString('zh-CN', { hour12: false })}
                    </small>
                  )}
                  <button
                    type="button"
                    className="button ghost sm"
                    disabled={refreshing}
                    onClick={manualRefresh}
                    title="重新拉取当前报表的规则与产物状态"
                  >
                    {refreshing ? '刷新中...' : '刷新规则'}
                  </button>
                  {detail.run && !runActive && (
                    <button
                      type="button"
                      className="button secondary sm"
                      disabled={reauditBusy}
                      title="保留当前规则（含手工修改与审批结论），只重跑 AI 造数与 Oracle 沙箱验证"
                      onClick={reaudit}
                    >
                      {reauditBusy ? '已提交...' : '重新验证（保留规则）'}
                    </button>
                  )}
                  <button
                    type="button"
                    className="button secondary sm"
                    disabled={busy || currentSelectable !== true}
                    title={currentSelectable === false
                      ? currentBlockReason
                      : detail.run
                        ? '从头重新生成：AI 会重新绑定规则，抽屉里手工改过的参数会被覆盖'
                        : ''}
                    onClick={() => generate([detail.table.id])}
                  >
                    {detail.run ? '重新生成并验证' : '生成这张表'}
                  </button>
                  {detail.run && (
                    <button type="button" className="button secondary sm" onClick={() => navigate(`/runs?run=${detail.run.id}`)}>查看测试与下载</button>
                  )}
                </div>
              </div>

              {currentSelectable === false && (
                <div className="current-blocker">
                  <strong>当前不可重新生成</strong>
                  <span>{currentBlockReason}</span>
                  <button type="button" className="button-link" onClick={() => openMetadataTable(detail.table.id)}>去处理</button>
                </div>
              )}

              <ProductStatusBar detail={detail} activeView={artifactView} onSelect={setArtifactView} onShowRules={showRules} />

              {artifactView && <ArtifactDetail detail={detail} view={artifactView} onClose={() => setArtifactView(null)} />}

              {!detail.run && (
                <div className="generation-empty-banner">
                  这张表还没有生成过。确认可生成后，点击上方“生成这张表”，即可创建 DDL、迁移码表并生成校验规则。
                </div>
              )}

              {runActive && (
                <div className="generation-running-banner">
                  <span className="live-dot" aria-hidden="true" />
                  <div>
                    <strong>AI 正在生成校验规则</strong>
                    <span>
                      当前已落库 {detail.rules.length} 条，规则会分批出现；本页每 {POLL_INTERVAL_MS / 1000} 秒自动刷新，
                      也可以点右上角“刷新规则”立即拉取。
                    </span>
                  </div>
                </div>
              )}

              <div id="rule-list" className="rule-list-anchor">
                <RulesView
                  key={detail.table.id}
                  rules={detail.rules}
                  runId={detail.run?.id}
                  runStatus={detail.run?.status}
                  onSaved={() => { select(currentId, { background: true }); refreshOverview() }}
                  onContinue={() => navigate(`/runs?run=${detail.run.id}`)}
                  onNotice={setNotice}
                  onError={setError}
                />
              </div>
            </>
          )}
        </section>
      )}
    </>
  )
}
