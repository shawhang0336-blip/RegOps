import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge, formatTime } from './SharedUI.jsx'

const GENERATION_FILTERS = [
  ['all', '全部生成状态'],
  ['ungenerated', '未生成'],
  ['running', '生成中'],
  ['verified', '已验证'],
  ['failed', '验证失败'],
]

const READINESS_FILTERS = [
  ['all', '全部可选状态'],
  ['selectable', '可选'],
  ['blocked', '不可选'],
  ['code_issues', '码表需处理'],
]

// 规则还在生成/空跑中的状态：需要自动轮询刷新规则列表
const ACTIVE_RUN_STATUSES = ['NEW', 'RUNNING', 'AUDITING', 'READY_TO_AUDIT']
const POLL_INTERVAL_MS = 5000

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

function ruleStatusLabel(status) {
  if (status === 'APPROVED') return '已批准'
  if (status === 'PENDING') return '待审'
  if (status === 'REJECTED') return '已拒绝'
  if (status === 'SUPERSEDED') return '已替换'
  return status || '未知'
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join('、')
  if (value !== null && typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function formatParams(params, compact = false) {
  const entries = Object.entries(params || {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => {
    if (compact && Array.isArray(value) && value.length > 3) {
      return `${key}: ${value.slice(0, 3).join('、')} 等 ${value.length} 项`
    }
    return `${key}: ${formatValue(value)}`
  }).join('；')
}

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
      setDetail(null)
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
    if (data) setTables(data.tables || [])
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

  // 聚焦单表且还在生成/空跑 → 每 5 秒静默刷新当前表产物
  useEffect(() => {
    if (!currentId || !runActive || !focusMode) return undefined
    const timer = window.setInterval(() => {
      select(currentId, { background: true }).catch(() => {})
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [currentId, runActive, focusMode])

  // 总览下任一表还在生成 → 每 5 秒静默刷新总览
  const overviewActive = Boolean(overview?.tables?.some((item) => ACTIVE_RUN_STATUSES.includes(item.run?.status)))
  useEffect(() => {
    if (!overviewActive) return undefined
    const timer = window.setInterval(() => {
      refreshOverview().catch(() => {})
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [overviewActive])

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
                                <button type="button" className="button-link" onClick={() => navigate(`/metadata?table=${row.id}`)}>
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
        <section className="panel rule-workbench-panel" role="tabpanel">
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
                  <button
                    type="button"
                    className="button secondary sm"
                    disabled={busy || currentSelectable !== true}
                    title={currentSelectable === false ? currentBlockReason : ''}
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
                  <button type="button" className="button-link" onClick={() => navigate(`/metadata?table=${detail.table.id}`)}>去处理</button>
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

function ProductStatusBar({ detail, activeView, onSelect, onShowRules }) {
  const hasRun = Boolean(detail.run)
  const running = hasRun && !['COMPLETED', 'FAILED'].includes(detail.run.status)
  const activeRules = detail.rules.filter((rule) => rule.status !== 'SUPERSEDED')
  const pendingRules = activeRules.filter((rule) => rule.status === 'PENDING')
  const cards = [
    {
      key: 'ddl',
      label: '表结构 DDL',
      value: !hasRun ? '待生成' : detail.ddl ? '已生成' : '未生成',
      hint: detail.ddl ? '点击查看 Oracle 建表 SQL' : '查看表结构产物状态',
      tone: !hasRun ? 'neutral' : detail.ddl ? 'green' : 'red',
    },
    {
      key: 'codes',
      label: '码表',
      value: !hasRun ? '待生成' : detail.code_sets.length ? `${detail.code_sets.length} 张已迁移` : '无关联码表',
      hint: detail.code_sets.length ? '点击查看码值和 Oracle 迁移数量' : '查看码表产物状态',
      tone: !hasRun || !detail.code_sets.length ? 'neutral' : 'green',
    },
    {
      key: 'rules',
      label: '校验规则',
      value: !hasRun ? '待生成' : `${activeRules.length} 条`,
      hint: pendingRules.length ? `${pendingRules.length} 条待确认` : '规则是当前工作台的主要内容',
      tone: !hasRun ? 'neutral' : activeRules.length ? 'green' : 'red',
    },
    {
      key: 'audit',
      label: '验证结果',
      value: detail.audit
        ? (detail.audit.all_passed ? '全部通过' : '验证未过')
        : detail.precheck
          ? (detail.precheck.all_passed ? '空跑通过' : '空跑未过')
          : running ? '验证中' : '未验证',
      hint: detail.audit
        ? `${detail.audit.case_count || 0} 条验证用例`
        : detail.precheck
          ? `${detail.precheck.rule_count || 0} 条规则 · ${detail.precheck.statement_count || 0} 条 SQL`
          : '查看验证产物状态',
      tone: detail.audit
        ? (detail.audit.all_passed ? 'green' : 'red')
        : detail.precheck
          ? (detail.precheck.all_passed ? 'green' : 'red')
          : running ? 'blue' : 'neutral',
    },
  ]

  return (
    <div className="product-status-grid" aria-label="当前报表产物状态">
      {cards.map((card) => (
        <button
          type="button"
          key={card.key}
          className={`product-status-card tone-${card.tone} ${activeView === card.key ? 'active' : ''}`}
          aria-pressed={activeView === card.key}
          onClick={() => card.key === 'rules' ? onShowRules() : onSelect(activeView === card.key ? null : card.key)}
        >
          <span>{card.label}</span>
          <strong>{card.value}</strong>
          <small>{card.hint}</small>
        </button>
      ))}
    </div>
  )
}

function ArtifactDetail({ detail, view, onClose }) {
  return (
    <div className="artifact-inline-detail">
      <div className="artifact-inline-heading">
        <strong>{view === 'ddl' ? 'Oracle 表结构' : view === 'codes' ? '码表迁移' : '验证结果'}</strong>
        <button type="button" className="button-link" onClick={onClose}>收起</button>
      </div>
      {view === 'ddl' && (
        detail.ddl ? <pre className="sql-pre">{detail.ddl}</pre> : <div className="empty">还没有生成表结构。</div>
      )}
      {view === 'codes' && (
        detail.code_sets.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>码表</th><th>码值数</th><th>迁移到 Oracle</th><th>目标表</th></tr></thead>
              <tbody>
                {detail.code_sets.map((set) => (
                  <tr key={set.code}>
                    <td><strong>{set.name}</strong><small>{set.code}</small></td>
                    <td>{set.value_count}</td>
                    <td><StatusBadge tone={set.oracle_records > 0 ? 'green' : 'neutral'}>{set.oracle_records} 条</StatusBadge></td>
                    <td><code>{set.oracle_table || '—'}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty">这张表没有关联码表。</div>
      )}
      {view === 'audit' && (
        detail.audit ? <AuditView audit={detail.audit} /> : detail.precheck ? (
          <>
            <div className={`audit-banner ${detail.precheck.all_passed ? 'ok' : 'bad'}`}>
              <strong>{detail.precheck.all_passed ? 'Oracle 空表预检通过' : 'Oracle 空表预检未通过'}</strong>
              <span>{detail.precheck.rule_count} 条规则 · {detail.precheck.statement_count} 条 SQL 均已在空表执行</span>
            </div>
            <p className="muted-text">空跑只验证建表、规则 SQL 可执行；完整规则有效性仍需在第三步生成 AI 测试数据后审计。</p>
          </>
        ) : <AuditView audit={null} />
      )}
    </div>
  )
}

const RULE_ORDER = ['REQUIRED', 'ENUM', 'COND_REQUIRED', 'LENGTH', 'LENGTH_MAX', 'REGEXP', 'NOT_IN', 'FOREIGN_KEY']

function sortRules(rows) {
  return [...rows].sort(
    (a, b) => RULE_ORDER.indexOf(a.operator) - RULE_ORDER.indexOf(b.operator) || a.field.localeCompare(b.field)
  )
}

function groupStatusMeta(runStatus) {
  if (runStatus === 'FAILED') return ['red', '验证失败']
  if (runStatus === 'COMPLETED') return ['green', '已完成']
  if (runStatus === 'WAITING_APPROVAL') return ['amber', '待确认']
  if (ACTIVE_RUN_STATUSES.includes(runStatus)) return ['blue', '生成中']
  return ['neutral', runStatus || '未知']
}

function RuleTable({ rows, showTable = false, decisions = {}, onDecision, canApprove, onOpenRule, activeKey }) {
  return (
    <div className="table-wrap rule-table-wrap">
      <table className={`data-table rule-table ${showTable ? 'with-table-col' : ''}`}>
        <thead>
          <tr>
            {showTable && <th className="rule-table-col">报表</th>}
            <th>字段</th><th>规则说明</th><th>算子</th><th>参数</th><th>来源</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((rule) => (
            <tr key={rule.row_id} className={activeKey === rule.row_id ? 'active-rule' : ''}>
              {showTable && (
                <td className="rule-table-col">
                  <strong>{rule._tableName}</strong>
                  <small><code>{rule._tableCode}</code></small>
                </td>
              )}
              <td><code className="rule-field">{rule.field}</code></td>
              <td><div className="rule-description" title={rule.rationale || rule.sql || ''}>{rule.rationale || rule.sql || '—'}</div></td>
              <td><StatusBadge tone="blue">{rule.operator_label}</StatusBadge></td>
              <td className="rule-param-cell" title={formatParams(rule.params)}>{formatParams(rule.params, true)}</td>
              <td>
                <StatusBadge tone={rule.source === 'DOC_EXPLICIT' ? 'green' : 'violet'}>
                  {rule.source === 'DOC_EXPLICIT' ? '原文明确' : 'AI 建议'}
                </StatusBadge>
              </td>
              <td>
                <StatusBadge tone={rule.status === 'APPROVED' ? 'green' : rule.status === 'PENDING' ? 'amber' : 'neutral'}>
                  {ruleStatusLabel(rule.status)}
                </StatusBadge>
              </td>
              <td>
                <div className="rule-row-actions">
                  {canApprove?.(rule) && onDecision && (
                    <>
                      <button type="button" className={`button sm ${decisions[rule.rule_hash] === 'APPROVED' ? 'primary' : 'secondary'}`} onClick={() => onDecision(rule.rule_hash, 'APPROVED')}>批准</button>
                      <button type="button" className={`button sm ${decisions[rule.rule_hash] === 'REJECTED' ? 'danger' : 'secondary'}`} onClick={() => onDecision(rule.rule_hash, 'REJECTED')}>拒绝</button>
                    </>
                  )}
                  <button type="button" className="button-link" onClick={() => onOpenRule(rule)}>查看 / 编辑</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="empty table-empty">没有符合筛选条件的规则。</div>}
    </div>
  )
}

function RulesView({ rules, runId, runStatus, onSaved, onContinue, onNotice, onError }) {
  const [operatorFilter, setOperatorFilter] = useState('all')
  const [fieldSearch, setFieldSearch] = useState('')
  const [activeRuleKey, setActiveRuleKey] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [showSuperseded, setShowSuperseded] = useState(false)
  const pendingRules = rules.filter((rule) => rule.status === 'PENDING')
  const decidedCount = pendingRules.filter((rule) => decisions[rule.rule_hash]).length

  function setAllDecisions(decision) {
    setDecisions(Object.fromEntries(pendingRules.map((rule) => [rule.rule_hash, decision])))
  }

  async function submitApprovals() {
    if (pendingRules.some((rule) => !decisions[rule.rule_hash])) {
      onError?.('请先对每条待确认规则选择“批准”或“拒绝”。')
      return
    }
    setApprovalBusy(true)
    try {
      const body = await api(`/api/runs/${runId}/approvals`, {
        method: 'POST',
        body: JSON.stringify({
          provider: 'live',
          decisions: pendingRules.map((rule) => ({ rule_hash: rule.rule_hash, decision: decisions[rule.rule_hash] })),
        }),
      })
      setDecisions({})
      if (body.status === 'WAITING_APPROVAL') {
        onNotice?.('本表规则已确认；整批仍等待其他报表处理。')
        onSaved?.()
      } else {
        onNotice?.('规则确认完成，AI 将继续生成测试数据并执行 Oracle 验证。')
        onContinue?.()
      }
    } catch (err) {
      onError?.(err.message)
    } finally {
      setApprovalBusy(false)
    }
  }

  if (!rules.length) {
    return <div className="empty">这张表还没有生成校验规则。</div>
  }

  const activeRules = rules.filter((rule) => rule.status !== 'SUPERSEDED')
  const supersededRules = rules.filter((rule) => rule.status === 'SUPERSEDED')
  const source = showSuperseded ? rules : activeRules

  const sorted = sortRules(source)
  const operatorCounts = {}
  activeRules.forEach((rule) => {
    operatorCounts[rule.operator] = (operatorCounts[rule.operator] || 0) + 1
  })
  const visible = sorted.filter((rule) => {
    if (operatorFilter !== 'all' && rule.operator !== operatorFilter) return false
    const q = fieldSearch.trim().toLowerCase()
    if (q && !rule.field.toLowerCase().includes(q) && !rule.rule_key.toLowerCase().includes(q)) return false
    return true
  })
  const activeIndex = visible.findIndex((rule) => rule.rule_key === activeRuleKey)
  const activeRule = activeIndex >= 0 ? visible[activeIndex] : null

  return (
    <div className="rules-workspace">
      <div className="rules-section-heading">
        <div>
          <h3>校验规则 <span>{activeRules.length} 条</span></h3>
          <p>点击“查看 / 编辑”在右侧抽屉中检查参数、SQL 和原文依据，可连续切换上一条或下一条。</p>
        </div>
        <span>当前显示 {visible.length} 条</span>
      </div>
      {pendingRules.length > 0 && runStatus === 'WAITING_APPROVAL' && (
        <div className="action-callout rule-approval-callout">
          <div>
            <strong>本表有 {pendingRules.length} 条规则待确认</strong>
            <p>已选择 {decidedCount} / {pendingRules.length} 条。批准或拒绝都在第二步完成，整批处理完后自动进入 AI 测试。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('APPROVED')}>全部批准</button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('REJECTED')}>全部拒绝</button>
            <button type="button" className="button primary sm" disabled={approvalBusy || decidedCount !== pendingRules.length} onClick={submitApprovals}>
              {approvalBusy ? '正在提交...' : '提交确认并继续测试'}
            </button>
          </div>
        </div>
      )}
      <div className="list-filter-bar rule-filter-bar">
        <select aria-label="规则算子" value={operatorFilter} onChange={(e) => { setOperatorFilter(e.target.value); setActiveRuleKey(null) }}>
          <option value="all">全部算子 ({activeRules.length})</option>
          {Object.entries(operatorCounts).map(([op, count]) => (
            <option key={op} value={op}>{rules.find((rule) => rule.operator === op)?.operator_label || op} ({count})</option>
          ))}
        </select>
        <input
          type="search"
          placeholder="按字段或规则名检索..."
          value={fieldSearch}
          onChange={(e) => { setFieldSearch(e.target.value); setActiveRuleKey(null) }}
        />
        {supersededRules.length > 0 && (
          <button
            type="button"
            className={`button sm ${showSuperseded ? 'secondary' : 'ghost'}`}
            aria-pressed={showSuperseded}
            title="被去重替换的重复规则，默认不参与校验"
            onClick={() => { setShowSuperseded(!showSuperseded); setActiveRuleKey(null) }}
          >
            {showSuperseded ? '隐藏' : '显示'}已替换 ({supersededRules.length})
          </button>
        )}
      </div>

      <RuleTable
        rows={visible}
        decisions={decisions}
        onDecision={(hash, decision) => setDecisions({ ...decisions, [hash]: decision })}
        canApprove={(rule) => rule.status === 'PENDING' && runStatus === 'WAITING_APPROVAL'}
        onOpenRule={(rule) => setActiveRuleKey(rule.rule_key)}
        activeKey={activeRule?.row_id}
      />

      {activeRule && (
        <RuleDrawer
          key={activeRule.row_id}
          rule={activeRule}
          index={activeIndex}
          total={visible.length}
          onPrevious={activeIndex > 0 ? () => setActiveRuleKey(visible[activeIndex - 1].rule_key) : null}
          onNext={activeIndex < visible.length - 1 ? () => setActiveRuleKey(visible[activeIndex + 1].rule_key) : null}
          onClose={() => setActiveRuleKey(null)}
          onSaved={onSaved}
          onNotice={onNotice}
        />
      )}
    </div>
  )
}

function AllRulesView({ overview, loading, refreshedAt, refreshing, onRefresh, onFocus, onNotice, onError, onSaved, onGoList }) {
  const [tableSearch, setTableSearch] = useState('')
  const [queueOpen, setQueueOpen] = useState(false)
  const [activeRuleId, setActiveRuleId] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [approvalBusy, setApprovalBusy] = useState(false)

  if (loading) {
    return <div className="empty">正在加载全部规则...</div>
  }
  const groups = (overview?.tables || []).map((group) => {
    const rows = group.rules.map((rule) => ({
      ...rule,
      _tableId: group.table.id,
      _tableCode: group.table.table_code,
      _tableName: group.table.table_name,
      _runId: group.run.id,
      _runStatus: group.run.status,
    }))
    return {
      ...group,
      rows,
      aiCount: rows.filter((rule) => rule.status !== 'SUPERSEDED' && rule.source === 'AI_EXPANDED').length,
      docCount: rows.filter((rule) => rule.status !== 'SUPERSEDED' && rule.source === 'DOC_EXPLICIT').length,
    }
  })

  if (!groups.length) {
    return (
      <div className="empty">
        <p>还没有生成过任何校验规则。</p>
        <button type="button" className="button primary" onClick={onGoList}>去报表清单选择报表生成</button>
      </div>
    )
  }

  const totals = {
    tables: groups.length,
    active: groups.reduce((sum, group) => sum + group.active_count, 0),
    pending: groups.reduce((sum, group) => sum + group.pending_count, 0),
    ai: groups.reduce((sum, group) => sum + group.aiCount, 0),
  }
  const anyRunning = groups.some((group) => ACTIVE_RUN_STATUSES.includes(group.run.status))
  const pendingRows = sortRules(
    groups.flatMap((group) => group.rows).filter((rule) => rule.status === 'PENDING' && rule._runStatus === 'WAITING_APPROVAL')
  )
  const pendingTableCount = new Set(pendingRows.map((rule) => rule._tableId)).size
  const decidedCount = pendingRows.filter((rule) => decisions[rule.rule_hash]).length

  const q = tableSearch.trim().toLowerCase()
  const visibleGroups = groups.filter((group) => {
    if (!q) return true
    return group.table.table_code.toLowerCase().includes(q) || String(group.table.table_name).toLowerCase().includes(q)
  })

  const activeIndex = pendingRows.findIndex((rule) => rule.row_id === activeRuleId)
  const activeRule = activeIndex >= 0 ? pendingRows[activeIndex] : null

  function setAllDecisions(decision) {
    setDecisions(Object.fromEntries(pendingRows.map((rule) => [rule.rule_hash, decision])))
  }

  async function submitApprovals() {
    if (pendingRows.some((rule) => !decisions[rule.rule_hash])) {
      onError?.('请先对每条待确认规则选择“批准”或“拒绝”。')
      return
    }
    setApprovalBusy(true)
    try {
      const byRun = new Map()
      pendingRows.forEach((rule) => {
        const list = byRun.get(rule._runId) || []
        list.push({ rule_hash: rule.rule_hash, decision: decisions[rule.rule_hash] })
        byRun.set(rule._runId, list)
      })
      const results = []
      for (const [runId, list] of byRun) {
        results.push(await api(`/api/runs/${runId}/approvals`, {
          method: 'POST',
          body: JSON.stringify({ provider: 'live', decisions: list }),
        }))
      }
      setDecisions({})
      if (results.some((body) => body.status === 'WAITING_APPROVAL')) {
        onNotice?.('规则已确认；仍有其他待确认项处理完后才会进入 AI 测试。')
      } else {
        onNotice?.('规则确认完成，AI 将继续生成测试数据并执行 Oracle 验证，可到第三步查看。')
      }
      onSaved?.()
    } catch (err) {
      onError?.(err.message)
    } finally {
      setApprovalBusy(false)
    }
  }

  return (
    <div className="rules-workspace rules-overview">
      <div className="rules-section-heading">
        <div>
          <h3>全部校验规则 <span>{totals.active} 条 · {totals.tables} 张表</span></h3>
          <p>按表汇总最近一次生成的规则；点规则条数或表名进入这张表的规则明细。</p>
        </div>
        <div className="actions">
          {refreshedAt && (
            <small className="refresh-stamp">
              {anyRunning && <span className="live-dot" aria-hidden="true" />}
              更新于 {refreshedAt.toLocaleTimeString('zh-CN', { hour12: false })}
            </small>
          )}
          <button type="button" className="button ghost sm" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? '刷新中...' : '刷新规则'}
          </button>
        </div>
      </div>

      {pendingRows.length > 0 && (
        <div className="action-callout rule-approval-callout">
          <div>
            <strong>共 {pendingRows.length} 条规则待确认，分布在 {pendingTableCount} 张表</strong>
            <p>已选择 {decidedCount} / {pendingRows.length} 条。全部决定后一次提交，确认完成后自动进入 AI 测试。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={() => setQueueOpen(!queueOpen)}>
              {queueOpen ? '收起队列' : '展开队列'}
            </button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('APPROVED')}>全部批准</button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('REJECTED')}>全部拒绝</button>
            <button type="button" className="button primary sm" disabled={approvalBusy || decidedCount !== pendingRows.length} onClick={submitApprovals}>
              {approvalBusy ? '正在提交...' : '提交确认并继续测试'}
            </button>
          </div>
        </div>
      )}

      {pendingRows.length > 0 && queueOpen && (
        <section className="pending-queue">
          <RuleTable
            rows={pendingRows}
            showTable
            decisions={decisions}
            onDecision={(hash, decision) => setDecisions({ ...decisions, [hash]: decision })}
            canApprove={() => true}
            onOpenRule={(rule) => setActiveRuleId(rule.row_id)}
            activeKey={activeRule?.row_id}
          />
        </section>
      )}

      <div className="list-filter-bar rule-filter-bar">
        <input
          type="search"
          placeholder="按报表名称或表代码检索..."
          value={tableSearch}
          onChange={(e) => setTableSearch(e.target.value)}
        />
        <span className="rule-list-hint">AI 建议 {totals.ai} 条 · 待确认 {totals.pending} 条</span>
      </div>

      <div className="table-wrap report-table-wrap">
        <table className="data-table report-table rules-table-summary">
          <thead>
            <tr>
              <th>报表</th>
              <th className="num-col">校验规则</th>
              <th className="num-col">AI 建议</th>
              <th className="num-col">待确认</th>
              <th className="num-col">已替换</th>
              <th>生成状态</th>
              <th>Oracle 空跑</th>
              <th>最近生成</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {visibleGroups.map((group) => {
              const [tone, label] = groupStatusMeta(group.run.status)
              return (
                <tr key={group.table.id}>
                  <td className="report-name-cell">
                    <button type="button" className="report-name-button" onClick={() => onFocus(group.table.id)}>
                      {group.table.table_name}
                    </button>
                    <small><code>{group.table.table_code}</code></small>
                  </td>
                  <td className="num-col">
                    <button
                      type="button"
                      className="rule-count-link"
                      title={`查看 ${group.table.table_name} 的 ${group.active_count} 条校验规则`}
                      onClick={() => onFocus(group.table.id)}
                    >
                      {group.active_count} 条
                    </button>
                  </td>
                  <td className="num-col">
                    {group.aiCount > 0 ? <StatusBadge tone="violet">{group.aiCount} 条</StatusBadge> : <span className="muted-text">—</span>}
                  </td>
                  <td className="num-col">
                    {group.pending_count > 0 ? <StatusBadge tone="amber">{group.pending_count} 条</StatusBadge> : <span className="muted-text">—</span>}
                  </td>
                  <td className="num-col">
                    {group.superseded_count > 0 ? <span className="muted-text">{group.superseded_count} 条</span> : <span className="muted-text">—</span>}
                  </td>
                  <td><StatusBadge tone={tone}>{label}</StatusBadge></td>
                  <td>
                    {group.precheck ? (
                      <StatusBadge tone={group.precheck.all_passed ? 'green' : 'red'}>
                        {group.precheck.all_passed ? '通过' : '未通过'}
                      </StatusBadge>
                    ) : <span className="muted-text">未空跑</span>}
                  </td>
                  <td>{group.run.created_at ? <small>{formatTime(group.run.created_at)}</small> : '—'}</td>
                  <td>
                    <button type="button" className="button secondary sm" onClick={() => onFocus(group.table.id)}>查看规则</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {visibleGroups.length === 0 && <div className="empty table-empty">没有符合检索条件的报表。</div>}
      </div>

      {activeRule && (
        <RuleDrawer
          key={activeRule.row_id}
          rule={activeRule}
          index={activeIndex}
          total={pendingRows.length}
          onPrevious={activeIndex > 0 ? () => setActiveRuleId(pendingRows[activeIndex - 1].row_id) : null}
          onNext={activeIndex < pendingRows.length - 1 ? () => setActiveRuleId(pendingRows[activeIndex + 1].row_id) : null}
          onClose={() => setActiveRuleId(null)}
          onSaved={onSaved}
          onNotice={onNotice}
        />
      )}
    </div>
  )
}


function RuleDrawer({ rule, index, total, onPrevious, onNext, onClose, onSaved, onNotice }) {
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])

  const evidenceLocation = rule.evidence ? [
    rule.evidence.section,
    rule.evidence.page ? `第 ${rule.evidence.page} 页` : null,
    rule.evidence.field ? `字段 ${rule.evidence.field}` : null,
  ].filter(Boolean).join(' · ') : ''

  return (
    <div className="rule-drawer-backdrop" onClick={onClose}>
      <aside className="rule-drawer" role="dialog" aria-modal="true" aria-labelledby="rule-drawer-title" onClick={(event) => event.stopPropagation()}>
        <header className="rule-drawer-header">
          <div>
            <span className="rule-drawer-kicker">
              规则详情{Number.isInteger(index) && total ? ` · 第 ${index + 1} / ${total} 条` : ''}
              {rule._tableName ? ` · ${rule._tableName}` : ''}
            </span>
            <h2 id="rule-drawer-title">{rule.field} · {rule.operator_label}</h2>
            <code>{rule.rule_key}</code>
          </div>
          <button type="button" className="button secondary sm" autoFocus onClick={onClose}>关闭</button>
        </header>

        <div className="rule-drawer-navigation">
          {onPrevious && <button type="button" className="button secondary sm" onClick={onPrevious}>上一条</button>}
          {onNext && <button type="button" className="button secondary sm" onClick={onNext}>下一条</button>}
          <button type="button" className="button primary sm" onClick={() => setEditing(!editing)}>{editing ? '返回详情' : '编辑参数'}</button>
        </div>

        <div className="rule-drawer-body">
          <div className="rule-drawer-badges">
            <StatusBadge tone="blue">{rule.operator_label}</StatusBadge>
            <StatusBadge tone={rule.source === 'DOC_EXPLICIT' ? 'green' : 'violet'}>{rule.source === 'DOC_EXPLICIT' ? '原文明确' : 'AI 建议'}</StatusBadge>
            <StatusBadge tone={rule.status === 'APPROVED' ? 'green' : rule.status === 'PENDING' ? 'amber' : 'neutral'}>{ruleStatusLabel(rule.status)}</StatusBadge>
          </div>

          <section className="rule-drawer-section">
            <h3>规则说明</h3>
            <p className="rule-rationale">{rule.rationale || '暂无规则说明。'}</p>
          </section>

          <section className="rule-drawer-section">
            <h3>参数</h3>
            {Object.keys(rule.params || {}).length ? (
              <dl className="rule-parameter-list">
                {Object.entries(rule.params).map(([key, value]) => (
                  <div key={key}><dt>{key}</dt><dd>{formatValue(value)}</dd></div>
                ))}
              </dl>
            ) : <p className="muted-text">这条规则没有可配置参数。</p>}
          </section>

          {rule.sql && (
            <section className="rule-drawer-section">
              <h3>编译后的校验条件</h3>
              <pre className="rule-sql">{rule.sql}</pre>
            </section>
          )}

          {rule.evidence && (
            <section className="rule-drawer-section">
              <h3>原文依据</h3>
              <blockquote className="rule-evidence">
                {evidenceLocation && <span>{evidenceLocation}</span>}
                {rule.evidence.quote || '原文没有提供可展示的引用内容。'}
              </blockquote>
            </section>
          )}

          {editing && (
            <RuleEditForm
              rule={rule}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false)
                onNotice?.(`已保存「${rule.rule_key}」的参数，请重新生成并验证当前报表。`)
                onSaved?.()
              }}
            />
          )}
        </div>
      </aside>
    </div>
  )
}

function AuditView({ audit }) {
  if (!audit) return <div className="empty">还没有跑过验证。</div>
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
        <div><span>漏报（该拦没拦）</span><strong>{audit.false_negative_count ?? '—'}</strong></div>
        <div><span>误报（不该拦却拦）</span><strong>{audit.false_positive_count ?? '—'}</strong></div>
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
    <div className="rule-edit-form">
      <h4>编辑参数 · {rule.field}（{rule.operator_label}）</h4>

      {operator === 'REQUIRED' && <p className="muted-text">必填规则没有参数，整条规则就是“去掉空白后必须有值”。</p>}

      {(operator === 'LENGTH' || operator === 'LENGTH_MAX') && (
        <label>
          <span>{operator === 'LENGTH' ? '定长' : '最大长度'}（字符数）</span>
          <input type="number" min="1" value={params.length ?? ''} onChange={(e) => set('length', Number(e.target.value))} />
        </label>
      )}

      {(operator === 'ENUM' || operator === 'NOT_IN') && (
        <label>
          <span>允许取值（每行一个）</span>
          <textarea rows={Math.min(8, (params.values || []).length + 1)} value={(params.values || []).join('\n')} onChange={(e) => set('values', e.target.value.split('\n'))} />
        </label>
      )}

      {operator === 'REGEXP' && (
        <label>
          <span>正则（Oracle REGEXP_LIKE 语义）</span>
          <input value={params.pattern ?? ''} onChange={(e) => set('pattern', e.target.value)} placeholder="^\d{8}$" />
        </label>
      )}

      {operator === 'COND_REQUIRED' && (
        <>
          <label><span>条件字段</span><input value={params.if_field ?? ''} onChange={(e) => set('if_field', e.target.value)} /></label>
          <label>
            <span>条件</span>
            <select value={params.if_operator ?? 'EQ'} onChange={(e) => set('if_operator', e.target.value)}>
              <option value="EQ">等于（EQ）</option>
              <option value="NE">不等于（NE）</option>
            </select>
          </label>
          <label>
            <span>条件取值（每行一个）</span>
            <textarea rows={Math.min(6, (params.if_values || []).length + 1)} value={(params.if_values || []).join('\n')} onChange={(e) => set('if_values', e.target.value.split('\n'))} />
          </label>
        </>
      )}

      {operator === 'FOREIGN_KEY' && (
        <>
          <label><span>引用表</span><input value={params.references_table ?? ''} onChange={(e) => set('references_table', e.target.value)} /></label>
          <label><span>引用字段</span><input value={params.references_field ?? ''} onChange={(e) => set('references_field', e.target.value)} /></label>
        </>
      )}

      {operator !== 'REQUIRED' && (
        <label>
          <span>修改原因（可选）</span>
          <input placeholder="例：原文限 28，业务实际允许 32" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}

      {error && <p className="danger-text">{error}</p>}
      <div className="actions">
        <button type="button" className="button primary sm" disabled={busy || operator === 'REQUIRED'} onClick={save}>{busy ? '保存中...' : '保存参数'}</button>
        <button type="button" className="button secondary sm" onClick={onCancel}>取消</button>
      </div>
      <p className="muted-text">保存后不会自动重跑验证，请使用当前报表的“重新生成并验证”确认改动。</p>
    </div>
  )
}
