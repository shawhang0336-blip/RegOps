import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import * as deliveryApi from '../../api/delivery.js'
import * as generationApi from '../../api/generation.js'
import { getBlob } from '../../api/platform.js'
import { DataTableModal } from '../../components/DataTableModal.jsx'
import PageHeader from '../../components/PageHeader.jsx'
import { DetailSkeleton, ListSkeleton } from '../../components/Skeleton.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import StatusMessage from '../../components/StatusMessage.jsx'
import { useFeedback } from '../../context/FeedbackContext.jsx'
import { useEventStream } from '../../hooks/useEventStream.js'
import { formatTime } from '../../utils/dateTime.js'
import HistoryCleanupModal from './HistoryCleanupModal.jsx'
import RunTraceTimeline from './RunTraceTimeline.jsx'

const RESULT_LABELS = {
  EFFECTIVE: { label: '规则有效', tone: 'green' },
  FAILED: { label: '验证失败', tone: 'red' },
  UNCOVERED: { label: '未覆盖', tone: 'amber' },
}

function formatOverrides(overrides) {
  const entries = Object.entries(overrides || {})
  if (!entries.length) return 'AI 正常样本，无字段变异'
  return entries.map(([field, value]) => `${field}: ${value === null ? 'NULL' : JSON.stringify(value)}`).join('；')
}

function resultBadge(result) {
  const meta = RESULT_LABELS[result] || { label: result || '未验证', tone: 'neutral' }
  return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
}

function batchTitle(run) {
  const names = (run.tables || []).map((table) => table.table_name).filter(Boolean)
  if (names.length === 1) return names[0]
  if (names.length > 1) return `${names[0]} 等 ${names.length} 张报表`
  return '未命名验证批次'
}

function batchTables(run) {
  return (run.tables || []).map((table) => table.table_code).filter(Boolean).join('、')
}

export default function RunsPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { confirm } = useFeedback()
  const [runs, setRuns] = useState([])
  const [selectedRunId, setSelectedRunId] = useState(null)
  const [active, setActive] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [validation, setValidation] = useState(null)
  const [plans, setPlans] = useState([])
  const [busEvents, setBusEvents] = useState([])
  const [replay, setReplay] = useState(null)
  const [traceTab, setTraceTab] = useState('ledger')
  const [tableIdByCode, setTableIdByCode] = useState({})
  const [artifact, setArtifact] = useState(null)
  const [runSearch, setRunSearch] = useState('')
  const [runStatusFilter, setRunStatusFilter] = useState('ALL')
  const [showCleanup, setShowCleanup] = useState(false)
  const [tableFilter, setTableFilter] = useState('ALL')
  const [ruleResultFilter, setRuleResultFilter] = useState('ALL')
  const [caseFilter, setCaseFilter] = useState('ALL')
  const [validationTab, setValidationTab] = useState('rules')
  const [busy, setBusy] = useState(false)
  const [bundleBusy, setBundleBusy] = useState(false)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const requestRef = useRef(0)
  const closeCleanup = useCallback(() => setShowCleanup(false), [])

  const select = useCallback(async (id, { silent = false } = {}) => {
    const reqId = ++requestRef.current
    setSelectedRunId(id)
    if (!silent) setDetailLoading(true)
    try {
      const [run, detail, planList, busData, replayData] = await Promise.all([
        deliveryApi.getRun(id),
        deliveryApi.getRunValidationDetail(id).catch(() => null),
        generationApi.listGenerationPlans(id).catch(() => []),
        deliveryApi.getRunBusEvents(id).catch(() => ({ events: [] })),
        deliveryApi.getRunReplay(id).catch(() => null),
      ])
      if (reqId !== requestRef.current) return
      setActive(run)
      setValidation(detail)
      setPlans(planList)
      setBusEvents(busData?.events || [])
      setReplay(replayData)
      if (!silent) {
        setArtifact(null)
        setTableFilter('ALL')
        setRuleResultFilter('ALL')
        setCaseFilter('ALL')
      }
    } finally {
      if (reqId === requestRef.current && !silent) setDetailLoading(false)
    }
  }, [])

  const load = useCallback(async (preferredId, options = {}) => {
    const list = await deliveryApi.listRuns()
    setRuns(list)
    const target = preferredId || active?.id || params.get('run') || list[0]?.id
    if (target) {
      await select(target, options)
    } else {
      setActive(null)
      setValidation(null)
      setPlans([])
      setBusEvents([])
      setReplay(null)
    }
  }, [active?.id, params, select])

  const runQueryParam = params.get('run')

  useEffect(() => {
    setLoading(true)
    load(runQueryParam)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
    generationApi.listArtifactsStatus().then((payload) => {
      setTableIdByCode(Object.fromEntries((payload.tables || []).map((table) => [table.table_code, table.id])))
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (runQueryParam && active?.id && runQueryParam !== active.id) {
      select(runQueryParam).catch((err) => setError(err.message))
    }
  }, [runQueryParam])

  const runIsActive = Boolean(active && ['RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(active.status))
  const refreshActiveRun = useCallback(() => {
    if (!busy && active?.id) load(active.id, { silent: true }).catch(() => {})
  }, [active?.id, busy, load])

  useEventStream({
    enabled: runIsActive,
    runId: active?.id || '',
    topics: 'node.enter,node.exit,node.redirect,node.error,wave.barrier,tool.rejected,budget.exceeded,run.interrupted,run.cancelled,run.completed,run.failed,event.appended',
    fallbackIntervalMs: 5000,
    onEvent: refreshActiveRun,
    onFallbackTick: refreshActiveRun,
  })

  async function openArtifact(uri, title) {
    try {
      const body = await getBlob(uri)
      setArtifact({ title, content: body.content })
    } catch (err) {
      setError(err.message)
    }
  }

  async function previewUrl(url, title) {
    if (!url) return
    setPreviewBusy(true)
    setError('')
    try {
      const rows = await deliveryApi.fetchRunPreview(url)
      setArtifact({ title, rows: Array.isArray(rows) ? rows : [], content: JSON.stringify(rows, null, 2) })
    } catch (err) {
      setError(err.message)
    } finally {
      setPreviewBusy(false)
    }
  }

  function previewBaseline(table) {
    setArtifact({
      title: `AI 字段赋值基线 · ${table.table_code}`,
      rows: table.golden_record ? [table.golden_record] : [],
      content: JSON.stringify(table.golden_record || {}, null, 2),
    })
  }

  async function previewCase(testCase) {
    const table = validation?.test_data?.find((item) => item.table_code === testCase.table_code)
    if (!table?.audit_preview_url) return
    setPreviewBusy(true)
    setError('')
    try {
      const rows = await deliveryApi.fetchRunPreview(table.audit_preview_url)
      const list = Array.isArray(rows) ? rows : []
      const row = list.find((item) => item.record_id === testCase.case_id)
      if (!row) {
        throw new Error(
          list.length >= 100
            ? `前 100 行预览中未找到用例 ${testCase.case_id}（当前表测试数据超过 100 行）`
            : `没有在测试数据中找到用例 ${testCase.case_id}`,
        )
      }
      setArtifact({
        title: `${testCase.case_kind === 'GOLDEN' ? 'AI 正常样本' : '规则变异样本'} · ${testCase.case_id}`,
        rows: [row],
        content: JSON.stringify({ case: testCase, record: row }, null, 2),
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setPreviewBusy(false)
    }
  }

  async function downloadBundle() {
    if (!active) return
    setBundleBusy(true)
    setError('')
    try {
      const filename = await generationApi.downloadProductionBundle(active.id)
      setNotice(`生产部署包「${filename}」已开始下载。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBundleBusy(false)
    }
  }

  async function retry() {
    setBusy(true)
    setError('')
    try {
      const body = await deliveryApi.retryRun(active.id)
      setNotice('已重新处理失败项，正在刷新验证结果。')
      await load(body.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function cancelActiveRun() {
    if (!active) return
    const confirmed = await confirm({
      title: `中止运行中的批次「${batchTitle(active)}」？`,
      message: `当前执行节点：${active.current_node || 'created'}`,
      details: '系统会向内核取消令牌投递中止信号，在下一个安全点停止 LLM 调用并保留检查点。中止后可随时从断点继续执行，也可直接删除该批次。',
      confirmLabel: '确认中止',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      const res = await deliveryApi.cancelRun(active.id, '由操作员在控制台主动中止')
      setNotice(
        res.cancelled
          ? '批次已中止并保留检查点，可随时点击“从断点继续执行”恢复。'
          : '已向内核投递协作式取消令牌，等待当前子任务安全收尾后停止。',
      )
      await load(active.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function confirmTests() {
    setBusy(true)
    setError('')
    try {
      const body = await deliveryApi.confirmRunTestResults(active.id)
      setNotice('测试结果已确认，生产部署包已开放下载。')
      await load(body.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function deleteRun(run) {
    const confirmed = await confirm({
      title: `删除验证批次「${batchTitle(run)}」？`,
      message: `生成时间：${formatTime(run.created_at)}`,
      details: '会同时删除该批次的规则、事件账本、临时产物和对应的 Oracle 沙箱表，不可恢复。运行中的批次不能删除。',
      confirmLabel: '确认删除',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      const result = await deliveryApi.deleteRun(run.id)
      setNotice(`已删除批次「${batchTitle(run)}」${result.sandbox_cleanup?.length ? `，${result.sandbox_cleanup.length} 张沙箱表已不存在或跳过` : ''}。`)
      const list = await deliveryApi.listRuns()
      setRuns(list)
      if (active?.id === run.id) {
        if (list[0]) {
          await select(list[0].id)
        } else {
          setSelectedRunId(null)
          setActive(null)
          setValidation(null)
          setPlans([])
        }
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function editTable(tableCode) {
    const tableId = tableIdByCode[tableCode]
    navigate(tableId ? `/generation?table=${tableId}` : '/generation')
  }

  const summary = validation?.summary || active?.result?.audit_summary || {}
  const testData = validation?.test_data || []
  const rules = validation?.rules || []
  const cases = validation?.cases || []
  const pending = useMemo(() => active?.rules?.filter((rule) => rule.status === 'PENDING') || [], [active])
  const tableCodes = useMemo(() => [...new Set([
    ...testData.map((item) => item.table_code),
    ...rules.map((item) => item.table_code),
    ...cases.map((item) => item.table_code),
  ])], [testData, rules, cases])
  const filteredRules = rules.filter((rule) => {
    if (tableFilter !== 'ALL' && rule.table_code !== tableFilter) return false
    if (ruleResultFilter !== 'ALL' && rule.result !== ruleResultFilter) return false
    return true
  })
  const filteredCases = cases.filter((testCase) => {
    if (tableFilter !== 'ALL' && testCase.table_code !== tableFilter) return false
    if (caseFilter === 'FAILED' && testCase.passed) return false
    if (['GOLDEN', 'MUTATION'].includes(caseFilter) && testCase.case_kind !== caseFilter) return false
    return true
  })
  const failedCases = cases.filter((testCase) => !testCase.passed)
  const uncoveredCount = rules.filter((rule) => rule.result === 'UNCOVERED').length
  const effectiveCount = rules.filter((rule) => rule.result === 'EFFECTIVE').length
  const coverageIncomplete = summary.coverage_complete === false || uncoveredCount > 0
  const readyToDownload = active?.status === 'COMPLETED' && summary.all_passed && !validation?.audit_stale && !coverageIncomplete
  const goldenTotal = testData.reduce((total, item) => total + (item.golden_count || 0), 0)

  const runEvents = active?.events || []
  const latestApprovalInterrupt = useMemo(
    () => [...runEvents].reverse().find((ev) => ev.kind === 'APPROVAL_REQUIRED'),
    [runEvents]
  )
  const latestTestInterrupt = useMemo(
    () => [...runEvents].reverse().find((ev) => ev.kind === 'TEST_CONFIRMATION_REQUIRED'),
    [runEvents]
  )
  const busRejectEvents = useMemo(
    () => runEvents.filter((ev) => ev.kind === 'BUS_REJECTED'),
    [runEvents]
  )
  const goRedirectEvents = useMemo(
    () => runEvents.filter((ev) => ev.kind === 'GO_REDIRECT'),
    [runEvents]
  )
  const resumeEvents = useMemo(
    () => runEvents.filter((ev) => ev.kind === 'RUN_RESUMED'),
    [runEvents]
  )
  const backfillEvents = useMemo(
    () => runEvents.filter((ev) => ev.kind === 'RULE_BACKFILL'),
    [runEvents]
  )
  const hasAuditReport = Boolean(active?.result?.audit_report_blob)
  const errorDetails = active?.result?.error_details || null
  const errorMessage = active?.result?.error || ''
  const isCancelled = Boolean(active?.result?.cancelled)
  const isBudgetTripped = errorDetails?.error_kind === 'budget'
  const shouldAutoExpandTrace =
    busRejectEvents.length > 0 || goRedirectEvents.length > 0 || resumeEvents.length > 0 || isBudgetTripped || isCancelled

  useEffect(() => {
    setTraceOpen(shouldAutoExpandTrace)
  }, [active?.id, shouldAutoExpandTrace])
  const budgetUsage = active?.result?.budget_usage || errorDetails?.budget_snapshot || null
  const budgetSpeedup = budgetUsage && budgetUsage.elapsed_seconds > 0 && budgetUsage.total_llm_seconds > 0
    ? (budgetUsage.total_llm_seconds / budgetUsage.elapsed_seconds).toFixed(1)
    : null

  const outcome = !active ? null : active.status === 'WAITING_APPROVAL'
    ? {
        tone: 'attention',
        title: latestApprovalInterrupt?.payload?.question || `还有 ${pending.length} 条规则需要在第二步确认`,
        description: active.result?.oracle_precheck?.all_passed
          ? `中断节点：${latestApprovalInterrupt?.payload?.resume_node || 'human_gate'}（来源：${latestApprovalInterrupt?.payload?.pending_input?.origin || 'rule_binder'}）· Oracle 建表与 ${active.result.oracle_precheck.statement_count} 条规则空跑已通过。`
          : '第三步不修改规则。完成批准或拒绝后，系统会从中断点自动恢复并继续生成 AI 测试数据。',
      }
    : validation?.audit_stale
      ? { tone: 'danger', title: '规则在审计后发生过修改', description: '当前报告属于旧规则版本，请返回第二步重新生成并验证。' }
      : active.status === 'FAILED'
        ? {
            tone: isCancelled ? 'attention' : 'danger',
            title: isCancelled
              ? `批次已在节点 ${active.current_node || 'unknown'} 主动中止`
              : isBudgetTripped
                ? `已触达内核预算上限（${errorDetails?.metric || 'budget'}），BudgetGuard 已自动熔断`
                : hasAuditReport
                  ? '本次沙箱验证发现未通过规则'
                  : `执行在节点 ${active.current_node || 'unknown'} 中断`,
            description: isCancelled
              ? `${errorMessage || '操作员已取消本次执行'}。EventLog 检查点已保留，点击“从断点继续执行”可原地续跑，也可直接删除批次。`
              : isBudgetTripped
                ? `${errorMessage} 可前往“系统设置”放宽预算阈值后点击“从断点继续执行”，已完成阶段无需重跑。`
                : hasAuditReport
                  ? '可点击下方“AI 靶向自愈修复并重验”由 Scheduler 触发 GO_REDIRECT 只重绑失败规则，或回第二步手动微调参数。'
                  : `${errorDetails?.suggestion || errorMessage || '运行中途因超时或服务重启中断'}。点击“从断点继续执行”将通过 RUN_RESUMED 从上一个已完成节点原地续跑，不重跑已完成阶段。`,
          }
        : active.status === 'WAITING_TEST_APPROVAL'
          ? {
              tone: 'success',
              title: latestTestInterrupt?.payload?.question || 'Oracle 验证已完成，等待测试结果确认',
              description: `中断节点：${latestTestInterrupt?.payload?.resume_node || 'test_confirmation'} · 可以先浏览 AI 测试数据和逐规则结果，再确认开放生产包导出。`,
            }
          : active.status === 'COMPLETED'
            ? { tone: readyToDownload ? 'success' : 'attention', title: readyToDownload ? '验证通过，已抵达 export 终态，生产产出包可下载' : '验证完成，但仍有事项需要处理', description: readyToDownload ? '生产包包含报表与码表 DDL、码值 INSERT/MERGE 和校验 SQL。' : '请检查未覆盖规则或审计版本，再开放生产下载。' }
            : { tone: 'working', title: `Scheduler 正在驱动节点：${active.current_node || 'created'}`, description: '页面会自动刷新；生成完成后可直接浏览每张报表的测试数据。' }

  const visibleRuns = useMemo(() => {
    const q = runSearch.trim().toLowerCase()
    return runs.filter((run) => {
      if (runStatusFilter === 'ACTIVE' && !['NEW', 'RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(run.status)) return false
      if (runStatusFilter === 'WAITING' && !['WAITING_APPROVAL', 'WAITING_TEST_APPROVAL'].includes(run.status)) return false
      if (runStatusFilter === 'COMPLETED' && run.status !== 'COMPLETED') return false
      if (runStatusFilter === 'FAILED' && run.status !== 'FAILED') return false
      if (!q) return true
      return (
        batchTitle(run).toLowerCase().includes(q) ||
        batchTables(run).toLowerCase().includes(q) ||
        String(run.id || '').toLowerCase().includes(q)
      )
    })
  }, [runs, runSearch, runStatusFilter])

  return (
    <>
      <PageHeader
        eyebrow="第 3 步 · 共 3 步"
        title="AI 测试验证与下载"
        description="AI 按报表字段、业务含义和规则块生成测试数据；系统执行正常样本与定向变异验证。规则问题返回第二步修改。"
        actions={
          <div className="actions">
            {runIsActive && <span className="live-stream-pill">实时推送中</span>}
            {runIsActive && (
              <button type="button" className="button danger secondary" disabled={busy} onClick={cancelActiveRun}>
                中止执行
              </button>
            )}
            {runs.length > 1 && (
              <button type="button" className="button secondary" disabled={busy} onClick={() => setShowCleanup(true)}>
                清理历史批次
              </button>
            )}
            {active && (
              <button type="button" className="button secondary" disabled={busy} onClick={() => load(active.id).catch((err) => setError(err.message))}>
                刷新状态
              </button>
            )}
          </div>
        }
      />
      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      {active && (
        <section className="metrics run-metrics">
          <div><span>AI 测试报表</span><strong>{testData.length} 张</strong><small>{testData.reduce((n, item) => n + item.generator_count, 0)} 个字段由 AI 赋值</small></div>
          <div><span>AI 正常样本</span><strong>{goldenTotal} 条</strong><small>按真实字段和码表生成</small></div>
          <div><span>审计与变异用例</span><strong>{summary.case_count ?? cases.length} 条</strong><small>Oracle 实际执行判定</small></div>
          <div><span>规则有效性</span><strong>{effectiveCount} / {rules.length}</strong><small>{uncoveredCount ? `${uncoveredCount} 条未覆盖` : '全部规则均已覆盖'}</small></div>
        </section>
      )}

      <section className="workspace-grid runs-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div><h2>验证批次</h2><p>共 {runs.length} 个批次，按报表和生成时间标识</p></div>
            <StatusBadge tone="neutral">{runs.length}</StatusBadge>
          </div>
          <div className="list-filters" style={{ marginBottom: 10 }}>
            <input
              type="search"
              placeholder="搜索报表名称或批次号..."
              value={runSearch}
              onChange={(e) => setRunSearch(e.target.value)}
            />
            <select aria-label="筛选批次状态" value={runStatusFilter} onChange={(e) => setRunStatusFilter(e.target.value)}>
              <option value="ALL">全部状态</option>
              <option value="ACTIVE">运行中</option>
              <option value="WAITING">待人工确认</option>
              <option value="COMPLETED">已通过</option>
              <option value="FAILED">需处理 / 失败</option>
            </select>
          </div>
          {loading ? (
            <ListSkeleton rows={5} />
          ) : (
          <div className="list-stack">
            {visibleRuns.map((run) => (
              <div
                key={run.id}
                role="button"
                tabIndex={0}
                className={`list-row batch-list-row ${(selectedRunId || active?.id) === run.id ? 'selected' : ''}`}
                onClick={() => select(run.id).catch((err) => setError(err.message))}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    select(run.id).catch((err) => setError(err.message))
                  }
                }}
              >
                <button
                  type="button"
                  className="row-delete batch-delete"
                  title={['RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(run.status) ? '运行中的批次不能删除' : '删除这个验证批次'}
                  disabled={busy || ['RUNNING', 'AUDITING', 'READY_TO_AUDIT'].includes(run.status)}
                  onClick={(event) => {
                    event.stopPropagation()
                    deleteRun(run)
                  }}
                >
                  ×
                </button>
                <div className="list-row-top"><strong title={batchTitle(run)}>{batchTitle(run)}</strong><StatusBadge status={run.status} /></div>
                <span>{formatTime(run.created_at)}{batchTables(run) ? ` · ${batchTables(run)}` : ''}</span>
                <em>{run.rules?.length || 0} 条规则 · 第 {run.attempt} 次生成</em>
              </div>
            ))}
            {!visibleRuns.length && (
              <div className="empty">
                {runSearch || runStatusFilter !== 'ALL' ? '没有符合筛选条件的验证批次。' : '还没有验证批次，请先在第二步生成。'}
              </div>
            )}
          </div>
          )}
        </div>

        <div className={`panel run-detail ${detailLoading && active ? 'panel-transitioning' : ''}`}>
          {(loading || detailLoading) && !active ? (
            <DetailSkeleton blocks={5} />
          ) : !active ? <div className="empty">请选择一个验证批次。</div> : (
            <>
              {outcome && (
                <div className={`run-outcome ${outcome.tone}`}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                    <div>
                      <strong>{outcome.title}</strong>
                      <p>{outcome.description}</p>
                    </div>
                    {runIsActive && (
                      <button type="button" className="button danger secondary sm" disabled={busy} onClick={cancelActiveRun}>
                        中止执行
                      </button>
                    )}
                    {isBudgetTripped && (
                      <button type="button" className="button secondary sm" onClick={() => navigate('/settings')}>
                        调整预算上限
                      </button>
                    )}
                  </div>
                </div>
              )}

              <div className="validation-flow" aria-label="第三步验证流程">
                <span className={testData.length ? 'done' : 'current'}>01 AI 生成测试数据</span>
                <span className={cases.length ? 'done' : testData.length ? 'current' : ''}>02 Oracle 执行验证</span>
                <span className={readyToDownload ? 'done' : cases.length ? 'current' : ''}>03 下载生产产出</span>
              </div>

              {active.status === 'WAITING_APPROVAL' && (
                <div className="action-callout">
                  <div><strong>规则确认属于第二步</strong><p>请在规则工作台批准或拒绝待确认规则，当前批次会自动继续。</p></div>
                  <button type="button" className="button primary" onClick={() => navigate('/generation')}>返回第二步确认规则</button>
                </div>
              )}

              <section className="run-section test-data-section">
                <div className="panel-heading">
                  <div><h2>AI 测试数据</h2><p>AI 按字段业务含义、真实码值和规则块赋值；可分别浏览正常样本与定向变异数据。</p></div>
                  {previewBusy && <StatusBadge tone="blue">正在读取数据</StatusBadge>}
                </div>
                {testData.length ? (
                  <div className="test-data-grid">
                    {testData.map((table) => {
                      const modeLabel = table.generation_mode === 'DETERMINISTIC'
                        ? '确定性生成'
                        : table.generation_mode === 'HYBRID_DETERMINISTIC_AI'
                          ? '程序+AI 混合'
                          : 'AI 赋值'
                      return (
                      <article className="test-data-card" key={table.table_code}>
                        <div className="test-data-card-head"><div><strong>{table.table_name}</strong><code>{table.table_code}</code></div><StatusBadge tone={table.generation_mode === 'DETERMINISTIC' ? 'blue' : 'violet'}>{modeLabel}</StatusBadge></div>
                        <div className="test-data-metrics"><span>{table.generator_count} 个字段</span><span>{table.golden_count} 条正常样本</span><span>{table.audit_count} 条审计用例</span></div>
                        {table.generation_plan?.rationale && <p>{table.generation_plan.rationale}</p>}
                        <div className="actions">
                          <button type="button" className="button primary sm" disabled={previewBusy || !table.golden_preview_url} onClick={() => previewUrl(table.golden_preview_url, `AI 正常样本 · ${table.table_code}`)}>浏览 AI 正常样本</button>
                          <button type="button" className="button secondary sm" disabled={previewBusy || !table.audit_preview_url} onClick={() => previewUrl(table.audit_preview_url, `审计与变异数据 · ${table.table_code}`)}>浏览变异数据</button>
                          <button type="button" className="button-link" onClick={() => previewBaseline(table)}>查看 AI 赋值基线</button>
                        </div>
                      </article>
                      )
                    })}
                  </div>
                ) : <div className="empty">当前阶段还没有可浏览的 AI 测试数据。规则确认后会自动生成。</div>}
              </section>

              <section className="run-section">
                <div className="panel-heading">
                  <div><h2>规则有效性</h2><p>有效、失败和未覆盖分开统计；命中结果来自 Oracle，而不是 AI 自我声明。</p></div>
                  <button type="button" className="button secondary sm" disabled={!active.result?.audit_report_blob} onClick={() => openArtifact(active.result.audit_report_blob, '完整 Oracle 验证报告')}>查看完整报告</button>
                </div>
                <div className="audit-metrics">
                  <div><span>正常样本误拦截</span><strong>{summary.false_positive_count ?? '—'}</strong></div>
                  <div><span>变异样本漏拦截</span><strong>{summary.false_negative_count ?? '—'}</strong></div>
                  <div><span>规则覆盖率</span><strong>{summary.coverage_rate != null ? `${Math.round(summary.coverage_rate * 100)}%` : '—'}</strong></div>
                  <div><span>未覆盖规则</span><strong>{uncoveredCount}</strong></div>
                </div>

                <nav className="validation-tab-bar">
                  <button type="button" className={validationTab === 'rules' ? 'active' : ''} onClick={() => setValidationTab('rules')}>逐规则结果 ({rules.length})</button>
                  <button type="button" className={validationTab === 'cases' ? 'active' : ''} onClick={() => setValidationTab('cases')}>逐用例结果 ({cases.length})</button>
                </nav>
                <div className="list-filter-bar">
                  <select value={tableFilter} onChange={(e) => setTableFilter(e.target.value)}><option value="ALL">全部报表</option>{tableCodes.map((code) => <option key={code} value={code}>{code}</option>)}</select>
                  {validationTab === 'rules' ? (
                    <select value={ruleResultFilter} onChange={(e) => setRuleResultFilter(e.target.value)}><option value="ALL">全部验证结果</option><option value="EFFECTIVE">规则有效</option><option value="FAILED">验证失败</option><option value="UNCOVERED">未覆盖</option></select>
                  ) : (
                    <select value={caseFilter} onChange={(e) => setCaseFilter(e.target.value)}><option value="ALL">全部用例</option><option value="GOLDEN">AI 正常样本</option><option value="MUTATION">规则变异样本</option><option value="FAILED">失败用例</option></select>
                  )}
                </div>

                {validationTab === 'rules' ? (
                  <div className="table-wrap"><table><thead><tr><th>报表／字段</th><th>规则</th><th>目标用例</th><th>实际命中</th><th>漏／误拦截</th><th>结果</th><th>操作</th></tr></thead><tbody>
                    {filteredRules.map((rule) => (
                      <tr key={rule.rule_hash}><td><strong>{rule.table_code}</strong><small>{rule.field}</small></td><td><code>{rule.operator}</code><small>{rule.rule_key}</small></td><td>{rule.expected_case_ids.length}</td><td>{rule.hit_case_ids.length}</td><td>{rule.missing_case_ids.length} / {rule.unexpected_case_ids.length}</td><td>{resultBadge(rule.result)}</td><td>{rule.result !== 'EFFECTIVE' && <button type="button" className="button-link" onClick={() => editTable(rule.table_code)}>返回第二步修改</button>}</td></tr>
                    ))}
                  </tbody></table>{!filteredRules.length && <div className="empty table-empty">没有符合筛选条件的规则。</div>}</div>
                ) : (
                  <div className="table-wrap"><table><thead><tr><th>用例</th><th>类型</th><th>字段赋值／变异</th><th>预期／实际命中</th><th>结果</th><th>数据</th></tr></thead><tbody>
                    {filteredCases.map((testCase) => (
                      <tr key={`${testCase.table_code}-${testCase.case_id}`}><td><strong>{testCase.case_id}</strong><small>{testCase.table_code}</small></td><td><StatusBadge tone={testCase.case_kind === 'GOLDEN' ? 'green' : 'violet'}>{testCase.case_kind === 'GOLDEN' ? 'AI 正常' : '定向变异'}</StatusBadge></td><td className="case-overrides" title={formatOverrides(testCase.overrides)}>{formatOverrides(testCase.overrides)}</td><td>{testCase.expected_rules?.length || 0} / {testCase.actual_rules?.length || 0}{(testCase.missing_rules?.length || testCase.unexpected_rules?.length) ? <small className="danger-text">漏 {testCase.missing_rules?.join('、') || 0} · 误 {testCase.unexpected_rules?.join('、') || 0}</small> : null}</td><td><StatusBadge tone={testCase.passed ? 'green' : 'red'}>{testCase.passed ? '符合预期' : '不符合预期'}</StatusBadge></td><td><button type="button" className="button-link" disabled={previewBusy} onClick={() => previewCase(testCase)}>浏览数据</button></td></tr>
                    ))}
                  </tbody></table>{!filteredCases.length && <div className="empty table-empty">没有符合筛选条件的用例。</div>}</div>
                )}
              </section>

              {(failedCases.length > 0 || active.status === 'FAILED' || validation?.audit_stale || uncoveredCount > 0) && (
                <section className="run-section issue-section">
                  <div className="panel-heading">
                    <div>
                      <h2>问题诊断与恢复</h2>
                      <p>
                        {active.status === 'FAILED' && !hasAuditReport
                          ? `失败节点：${active.current_node} · 支持从 EventLog 最近检查点原地断点续跑（RUN_RESUMED）。`
                          : '支持 AI 靶向自愈重绑（GO_REDIRECT）或返回第二步手工微调参数后轻量重审。'}
                      </p>
                    </div>
                    {active.status === 'FAILED' && (
                      <button type="button" className="button primary sm" disabled={busy} onClick={retry}>
                        {hasAuditReport ? 'AI 靶向自愈修复并重验' : '从断点继续执行 (Resume)'}
                      </button>
                    )}
                  </div>
                  {active.status === 'FAILED' && errorMessage && (
                    <div className="action-callout danger">
                      <div>
                        <strong>{errorDetails?.stage ? `阶段 ${errorDetails.stage} 异常` : '运行异常详情'}</strong>
                        <p>{errorMessage}</p>
                        {errorDetails?.suggestion && <small>{errorDetails.suggestion}</small>}
                      </div>
                    </div>
                  )}
                  {validation?.audit_stale && <div className="action-callout danger"><div><strong>当前审计已经过期</strong><p>规则哈希与审计时不一致，必须重新生成并验证。</p></div><button type="button" className="button primary sm" onClick={() => navigate('/generation')}>返回第二步</button></div>}
                  {uncoveredCount > 0 && <div className="action-callout"><div><strong>{uncoveredCount} 条规则没有有效覆盖</strong><p>未覆盖不能视为规则有效，也不会开放生产下载。</p></div><button type="button" className="button secondary sm" onClick={() => { setValidationTab('rules'); setRuleResultFilter('UNCOVERED') }}>查看未覆盖规则</button></div>}
                  <div className="issue-list">
                    {failedCases.slice(0, 10).map((testCase) => (
                      <div className="issue-row" key={`${testCase.table_code}-${testCase.case_id}-issue`}><div><strong>{testCase.table_code} · {testCase.case_id}</strong><p>{testCase.missing_rules?.length ? `漏拦截：${testCase.missing_rules.join('、')}` : ''}{testCase.unexpected_rules?.length ? ` 误拦截：${testCase.unexpected_rules.join('、')}` : ''}</p></div><div className="actions"><button type="button" className="button secondary sm" disabled={previewBusy} onClick={() => previewCase(testCase)}>查看数据</button><button type="button" className="button-link" onClick={() => editTable(testCase.table_code)}>去第二步修改</button></div></div>
                    ))}
                  </div>
                </section>
              )}

              <section className="run-section download-section">
                <div className="panel-heading"><div><h2>生产产出包</h2><p>同时输出 Oracle 23 与 MySQL 8 生产脚本及 SHA-256 完整性清单，不包含任何 AI 测试数据或沙箱表名。</p></div>{readyToDownload && <StatusBadge tone="green">可以下载</StatusBadge>}</div>
                <ul className="bundle-file-list">
                  <li><code>oracle/01~05_*.sql</code><span>Oracle 报表 DDL、码表 DDL、INSERT ALL / MERGE 与生产校验 SQL</span></li>
                  <li><code>mysql/01~04_*.sql</code><span>MySQL 8 报表 DDL、码表 DDL、UPSERT 与生产校验 SQL</span></li>
                  <li><code>manifest.json</code><span>全量交付物 SHA-256 校验清单与审计版本防篡改摘要</span></li>
                  <li><code>rules.json / audit_summary.json</code><span>结构化规则定义与沙箱审计结论</span></li>
                </ul>
                {readyToDownload ? (
                  <div className="production-download">
                    <div className="production-download-info">
                      <strong>生产部署包已就绪</strong>
                      <span>点击下载前后端将再次校验规则哈希未被篡改并实时签名打包。</span>
                    </div>
                    <button type="button" className="button primary" disabled={bundleBusy} onClick={downloadBundle}>
                      {bundleBusy ? '正在打包下载...' : '下载生产部署包 (.zip)'}
                    </button>
                  </div>
                ) : active.status === 'WAITING_TEST_APPROVAL' && summary.all_passed && !coverageIncomplete ? (
                  <div className="action-callout"><div><strong>验证已通过</strong><p>确认本次测试结果后开放生产下载。</p></div><button type="button" className="button primary" disabled={busy} onClick={confirmTests}>确认测试结果并开放下载</button></div>
                ) : (
                  <div className="empty">当前还不能下载：{validation?.audit_stale ? '审计版本已过期。' : coverageIncomplete ? '仍有规则未被有效覆盖。' : active.status === 'WAITING_APPROVAL' ? '请先完成第二步规则确认。' : '请等待 Oracle 验证完成并全部通过。'}</div>
                )}
              </section>

              <details
                className="run-section technical-run-details"
                open={traceOpen}
                onToggle={(e) => setTraceOpen(e.currentTarget.open)}
              >
                <summary>
                  ProdAgent 内核轨迹 · 当前节点 <code>{active.current_node || 'created'}</code>
                  {' · '}EventLog ({runEvents.length})
                  {budgetUsage ? ` · AI ${budgetUsage.llm_calls ?? 0} 次 / ${(budgetUsage.total_tokens ?? 0).toLocaleString()} tok` : ''}
                  {' · '}Bus 门禁拦截 ({busRejectEvents.length})
                  {' · '}自愈回边 ({goRedirectEvents.length})
                  {replay && ` · ${replay.chain_valid ? '哈希链完整' : '哈希链异常'}`}
                </summary>

                <div className="audit-metrics" style={{ marginTop: 12, marginBottom: 12 }}>
                  <div>
                    <span>当前 Plan 节点</span>
                    <strong><code>{active.current_node || 'created'}</code></strong>
                  </div>
                  <div>
                    <span>Bus 物理拦截</span>
                    <strong>{busRejectEvents.length} 次</strong>
                  </div>
                  <div>
                    <span>goto 自愈 / 断点续跑</span>
                    <strong>{goRedirectEvents.length} / {resumeEvents.length} 次</strong>
                  </div>
                  <div>
                    <span>确定性规则补齐</span>
                    <strong>{backfillEvents.reduce((acc, ev) => acc + (ev.payload?.total || 0), 0)} 条</strong>
                  </div>
                </div>

                {budgetUsage && (
                  <div className="action-callout" style={{ marginBottom: 12 }}>
                    <div style={{ width: '100%' }}>
                      <strong>
                        BudgetGuard 资源消耗统计
                        {budgetSpeedup && Number(budgetSpeedup) >= 1.1 ? ` · BSP 并发加速 ${budgetSpeedup}x` : ''}
                      </strong>
                      <div className="audit-metrics" style={{ marginTop: 8, marginBottom: 8 }}>
                        <div>
                          <span>AI 调用次数</span>
                          <strong>
                            {budgetUsage.llm_calls ?? 0}
                            {budgetUsage.limits?.max_llm_calls ? ` / ${budgetUsage.limits.max_llm_calls}` : ' 次'}
                          </strong>
                        </div>
                        <div>
                          <span>累计 Token 消耗</span>
                          <strong>
                            {(budgetUsage.total_tokens ?? 0).toLocaleString()}
                            {budgetUsage.limits?.max_total_tokens ? ` / ${Number(budgetUsage.limits.max_total_tokens).toLocaleString()}` : ''}
                          </strong>
                          <small>
                            输入 {(budgetUsage.prompt_tokens ?? 0).toLocaleString()} · 输出 {(budgetUsage.completion_tokens ?? 0).toLocaleString()}
                          </small>
                        </div>
                        <div>
                          <span>AI 累计 / 墙钟耗时</span>
                          <strong>
                            {budgetUsage.total_llm_seconds ?? 0}s / {budgetUsage.elapsed_seconds ?? 0}s
                          </strong>
                          {budgetUsage.limits?.max_elapsed_seconds && (
                            <small>熔断上限 {budgetUsage.limits.max_elapsed_seconds}s</small>
                          )}
                        </div>
                        <div>
                          <span>调度步数 / 门禁拦截</span>
                          <strong>{budgetUsage.steps ?? 0} 步 / {budgetUsage.tool_rejections ?? 0} 次</strong>
                        </div>
                      </div>
                      {budgetUsage.by_tool && Object.keys(budgetUsage.by_tool).length > 0 && (
                        <small className="muted-text">
                          按工具分布：
                          {Object.entries(budgetUsage.by_tool)
                            .map(([tool, count]) => `${tool} ×${count}`)
                            .join(' · ')}
                        </small>
                      )}
                    </div>
                  </div>
                )}

                {busRejectEvents.length > 0 && (
                  <div className="action-callout" style={{ marginBottom: 12 }}>
                    <div>
                      <strong>Bus 门禁成功拦截了 {busRejectEvents.length} 次 AI 参数幻觉并触发自动修正</strong>
                      <ul>
                        {busRejectEvents.map((ev) => (
                          <li key={ev.seq}>
                            <code>#{ev.seq} {ev.node} · {ev.payload?.tool}</code>：{ev.payload?.reason}
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>
                )}

                <div className="artifact-actions" style={{ marginBottom: 12 }}>
                  {[
                    ['oracle_ddl_blob', 'Oracle 建表语句'],
                    ['validation_sql_blob', '校验规则 SQL'],
                    ['audit_report_blob', '完整验证报告'],
                  ].filter(([key]) => active.result?.[key]).map(([key, title]) => (
                    <button type="button" className="button secondary sm" key={key} onClick={() => openArtifact(active.result[key], title)}>{title}</button>
                  ))}
                </div>

                <nav className="validation-tab-bar" style={{ marginBottom: 8 }}>
                  <button type="button" className={traceTab === 'ledger' ? 'active' : ''} onClick={() => setTraceTab('ledger')}>
                    EventLog 不可变事件账本 ({runEvents.length})
                  </button>
                  <button type="button" className={traceTab === 'bus' ? 'active' : ''} onClick={() => setTraceTab('bus')}>
                    Bus 全生命周期广播流 ({busEvents.length})
                  </button>
                  {plans.length > 0 && (
                    <button type="button" className={traceTab === 'plan' ? 'active' : ''} onClick={() => setTraceTab('plan')}>
                      AI 生成预算规划 ({plans.length})
                    </button>
                  )}
                </nav>

                {traceTab === 'ledger' && (
                  <RunTraceTimeline
                    items={runEvents}
                    mode="ledger"
                    emptyText="当前批次尚无事件账本记录。"
                  />
                )}
                {traceTab === 'bus' && (
                  <RunTraceTimeline
                    items={busEvents}
                    mode="bus"
                    emptyText="当前进程内存中暂无该历史批次的实时 Bus 广播缓存（新发起或续跑批次会实时记录 node.* / wave.* / tool.* / event.appended 信号）。"
                  />
                )}
                {traceTab === 'plan' && plans.length > 0 && (
                  <pre className="run-json">{JSON.stringify(plans, null, 2)}</pre>
                )}
              </details>
            </>
          )}
        </div>
      </section>
      {artifact && <DataTableModal artifact={artifact} onClose={() => setArtifact(null)} />}
      <HistoryCleanupModal
        open={showCleanup}
        onClose={closeCleanup}
        onCompleted={async (result) => {
          setNotice(
            `已清理 ${result.deleted_count} 个历史验证批次${result.sandbox_cleanup?.length ? `（${result.sandbox_cleanup.length} 张沙箱表已跳过或清理）` : ''}。`,
          )
          await load().catch((err) => setError(err.message))
        }}
      />
    </>
  )
}
