import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import * as deliveryApi from '../../api/delivery.js'
import CopyButton from '../../components/CopyButton.jsx'
import PageHeader from '../../components/PageHeader.jsx'
import SelectDropdown from '../../components/SelectDropdown.jsx'
import { DetailSkeleton, ListSkeleton } from '../../components/Skeleton.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import StatusMessage from '../../components/StatusMessage.jsx'
import { useWorkspace } from '../../context/WorkspaceContext.jsx'
import { agencyMeta, regulatorMeta } from '../../utils/activeDocument.js'
import { formatTime, parseBackendDate } from '../../utils/dateTime.js'

const RUN_KIND_OPTIONS = [
  { value: 'ALL', label: '全部链路类型', description: '同时纳管文档解析与规则验证批次' },
  { value: 'GENERATION', label: '规则与沙箱 (Step 2/3)', description: '建表、规则审批、Oracle 沙箱验证与生产导出' },
  { value: 'INGESTION', label: '文档解析 (Step 0/1)', description: '监管文档结构检查、分块与并行提取' },
]

const ANOMALY_FILTER_OPTIONS = [
  { value: 'ALL', label: '全部批次状态' },
  { value: 'CHAIN_BROKEN', label: '仅看哈希链异常' },
  { value: 'HAS_HUMAN', label: '含人工确权/审批' },
  { value: 'HAS_GOVERNANCE', label: '含门禁拦截或自愈' },
  { value: 'HAS_BUNDLE', label: '已签署生产部署包' },
  { value: 'FAILED', label: '执行失败 / 待处理' },
]

const EVENT_GROUP_OPTIONS = [
  { value: 'ALL', label: '全部事件类型' },
  { value: 'HUMAN', label: '仅人工确权与审批' },
  { value: 'GOVERNANCE', label: '仅门禁拦截与自愈' },
  { value: 'MILESTONE', label: '仅核心里程碑节点' },
  { value: 'BROKEN', label: '仅验签异常事件' },
]

const HUMAN_EVENT_KINDS = new Set([
  'HUMAN_DECISIONS_RECORDED',
  'TEST_RESULTS_CONFIRMED',
  'RULE_PARAM_EDITED',
  'RUN_CANCELLED',
  'REAUDIT_REQUESTED',
  'APPROVAL_REQUIRED',
  'TEST_CONFIRMATION_REQUIRED',
])

const GOVERNANCE_EVENT_KINDS = new Set([
  'BUS_REJECTED',
  'GO_REDIRECT',
  'NODE_RETRY',
  'RUN_RESUMED',
  'RULE_BACKFILL',
])

const MILESTONE_EVENT_KINDS = new Set([
  'RUN_CREATED',
  'GENERATION_PLANNED',
  'SCHEMA_ASSIGNED',
  'RULES_BOUND',
  'ORACLE_ASSETS_PREPARED',
  'GENERATORS_ASSIGNED',
  'AUDIT_CASES_ASSIGNED',
  'AUDIT_STARTED',
  'AUDIT_COMPLETED',
  'AUDIT_REJECTED',
  'ORACLE_AUDITED',
  'RUN_COMPLETED',
  'RUN_FAILED',
  'INGESTION_STAGE',
  'INGESTION_COMPLETED',
])

const NODE_LABELS = {
  table_confirmation: '报表范围确认',
  plan_barrier: 'BSP 生成规划汇合',
  schema_mapper: 'Schema 字段类型映射',
  schema_barrier: 'Schema 分配汇合',
  rule_binder: '校验规则编译绑定',
  rule_barrier: '规则编译汇合',
  oracle_asset_builder: 'Oracle 建表与校验资产生成',
  human_gate: '人工规则审批门禁',
  data_generator_binder: '合规测试数据生成规划',
  audit_case_binder: '反例变异测试用例绑定',
  oracle_executor: 'Oracle 23 沙箱全量审计',
  test_confirmation: '测试结果人工确权',
  export: '生产部署包签署',
  human_retry: '人工触发断点续跑',
  prepare: '文档预检与结构分析',
  document_overview: '文档目录概览',
  initial_chunking: '初始切块',
  prepare_completed: '预检完成',
  extract: '文档并行解析',
  parallel_extraction: '分块提取汇合',
}

function shortHash(hash, head = 12) {
  if (!hash) return '—'
  const text = String(hash)
  if (text.length <= head + 4) return text
  return `${text.slice(0, head)}…${text.slice(-4)}`
}

function formatDurationMs(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value === 0) return '<1ms'
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60_000) return `${(value / 1000).toFixed(2)}s`
  const mins = Math.floor(value / 60_000)
  const secs = ((value % 60_000) / 1000).toFixed(1)
  return `${mins}m ${secs}s`
}

function toneForEventKind(kind, chainOk = true) {
  if (!chainOk) return 'red'
  const k = String(kind || '').toUpperCase()
  if (k.includes('FAIL') || k.includes('ERROR') || k.includes('REJECT') || k.includes('CANCEL')) return 'red'
  if (k.includes('REDIRECT') || k.includes('INTERRUPT') || k.includes('APPROVAL') || k.includes('RESUMED') || k.includes('RETRY') || k.includes('EDIT')) return 'amber'
  if (k.includes('COMPLETED') || k.includes('PASSED') || k.includes('APPROVED') || k.includes('CONFIRMED') || k.includes('DECISIONS')) return 'green'
  if (k.includes('BACKFILL') || k.includes('FANOUT') || k.includes('BARRIER')) return 'violet'
  return 'blue'
}

function summarizeAuditEvent(ev) {
  const p = ev.payload || {}
  switch (ev.kind) {
    case 'RUN_CREATED':
      return p.run_kind === 'INGESTION'
        ? `创建文档解析任务（${p.file_name || p.phase || 'extract'}）`
        : `创建规则验证批次（模型：${p.provider || 'live'}，操作员：${p.operator || '操作员'}）`
    case 'GENERATION_PLANNED':
      return `生成执行计划与预算配额${p.table_code ? ` · ${p.table_code}` : ''}`
    case 'SCHEMA_ASSIGNED':
      return `绑定沙箱 Schema 与建表元数据${p.table_code ? ` · ${p.table_code}` : ''}`
    case 'RULES_BOUND':
      return `绑定校验规则，待审批 ${p.pending_count ?? 0} 条`
    case 'RULE_BACKFILL':
      return `确定性补齐 ${p.total ?? 0} 条规则（${p.kind || 'metadata'}）`
    case 'APPROVAL_REQUIRED':
      return p.question || '挂起等待人工审批校验规则（HumanGate Interrupt）'
    case 'HUMAN_DECISIONS_RECORDED':
      return `操作员「${p.approver || '操作员'}」完成 ${(p.decisions || []).length} 条规则审批签批`
    case 'ORACLE_ASSETS_PREPARED':
      return `生成 Oracle 建表 DDL 与校验 SQL 资产${p.pending_count ? `（待确认规则 ${p.pending_count} 条）` : ''}`
    case 'GENERATORS_ASSIGNED':
      return `完成合规测试样本生成规则配置${p.table_code ? ` · ${p.table_code}` : ''}`
    case 'AUDIT_CASES_ASSIGNED':
      return `完成反例变异审计测试用例绑定${p.table_code ? ` · ${p.table_code}` : ''}`
    case 'AUDIT_STARTED':
      return `启动第 ${p.attempt ?? 1} 轮 Oracle 23 沙箱全量执行验证`
    case 'AUDIT_COMPLETED':
    case 'ORACLE_AUDITED':
      return p.result?.audit_summary?.all_passed
        ? 'Oracle 23 沙箱全量规则验证通过（100% 有效覆盖）'
        : 'Oracle 23 沙箱验证完成（存在未通过或未覆盖项）'
    case 'AUDIT_REJECTED':
      return 'Oracle 沙箱验证未通过并触发门禁阻断'
    case 'TEST_CONFIRMATION_REQUIRED':
      return p.question || '挂起等待操作员确认测试结果并放行生产导出'
    case 'TEST_RESULTS_CONFIRMED':
      return `操作员「${p.approver || '操作员'}」确认沙箱测试结论，放行生产部署包`
    case 'RULE_PARAM_EDITED':
      return `人工修改规则参数（${p.rule_key || p.field || '参数'}）`
    case 'BUS_REJECTED':
      return `Bus 物理门禁拦截工具 ${p.tool || 'unknown'}：${p.reason || '参数或契约校验失败'}`
    case 'GO_REDIRECT':
      return `自愈回边跳转 → ${p.target || '目标节点'}${p.reason ? `（${p.reason}）` : ''}`
    case 'NODE_RETRY':
      return `节点重试（${p.reason || '自动容错重试'}）`
    case 'RUN_RESUMED':
      return `从 EventLog 检查点断点续跑${p.resume_from ? `（${p.resume_from}）` : ''}`
    case 'BSP_FANOUT':
      return `BSP 并发扇出 ${(p.table_codes || []).length} 张监管报表`
    case 'BSP_BARRIER_JOIN':
      return 'BSP 并发阶段汇合完成，推进下一执行节点'
    case 'INGESTION_STAGE':
      return `文档解析阶段推进：${p.stage || p.message || ev.node || '处理中'}`
    case 'INGESTION_COMPLETED':
      return '文档解析与资产落库全部完成'
    case 'RUN_CANCELLED':
      return `人工中止执行：${p.reason || 'Run cancelled'}`
    case 'RUN_FAILED':
      return p.result?.error || p.error || '执行异常中断'
    case 'RUN_COMPLETED':
      return '全流程抵达终态，所有校验与归档事件已封链'
    default: {
      if (p.reason) return String(p.reason)
      if (p.message) return String(p.message)
      if (p.error) return String(p.error)
      const keys = Object.keys(p)
      return keys.length ? `载荷字段：${keys.slice(0, 5).join('、')}` : '状态推进事件'
    }
  }
}

function spanStatusTone(status) {
  const s = String(status || '').toLowerCase()
  if (s === 'error' || s === 'failed') return 'red'
  if (s === 'interrupted' || s === 'waiting') return 'amber'
  return 'green'
}

function spanStatusLabel(status) {
  const s = String(status || '').toLowerCase()
  if (s === 'error' || s === 'failed') return '异常'
  if (s === 'interrupted' || s === 'waiting') return '挂起等待'
  return '完成'
}

export default function AuditCenterPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { activeAgency, activeRegulator, activePackageId, activePackage } = useWorkspace()

  const activeScopeRegulator = activeRegulator !== 'ALL' ? activeRegulator : (activeAgency !== 'ALL' ? activeAgency : '')
  const hasWorkspaceFilter = Boolean(activePackageId || activeScopeRegulator)

  const [scopeByProject, setScopeByProject] = useState(false)
  const [runKindFilter, setRunKindFilter] = useState('ALL')
  const [anomalyFilter, setAnomalyFilter] = useState('ALL')
  const [searchText, setSearchText] = useState('')

  const [overview, setOverview] = useState(null)
  const [selectedRunId, setSelectedRunId] = useState(() => params.get('run') || '')
  const [detail, setDetail] = useState(null)

  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [reverifyBusy, setReverifyBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const [activeTab, setActiveTab] = useState('hash-chain')
  const [eventGroup, setEventGroup] = useState('ALL')
  const [eventSearch, setEventSearch] = useState('')
  const [reverseOrder, setReverseOrder] = useState(false)
  const [expandedSeqs, setExpandedSeqs] = useState({})

  const detailReqRef = useRef(0)
  const urlRunId = params.get('run') || ''

  const loadDetail = useCallback(async (runId, { silent = false } = {}) => {
    if (!runId) {
      setDetail(null)
      return
    }
    const reqId = ++detailReqRef.current
    setSelectedRunId(runId)
    if (!silent) setDetailLoading(true)
    try {
      const payload = await deliveryApi.getAuditRunDetail(runId)
      if (reqId !== detailReqRef.current) return
      setDetail(payload)
      if (!silent) setExpandedSeqs({})
    } catch (err) {
      if (reqId === detailReqRef.current) {
        setError(err.message || '加载审计档案失败')
      }
    } finally {
      if (reqId === detailReqRef.current && !silent) setDetailLoading(false)
    }
  }, [])

  const loadOverview = useCallback(async ({ preferredRunId = '', silent = false } = {}) => {
    if (!silent) setLoading(true)
    setError('')
    try {
      const query = {
        runKind: runKindFilter,
        limit: 120,
      }
      if (scopeByProject && hasWorkspaceFilter) {
        if (activePackageId) query.packageId = activePackageId
        if (activeScopeRegulator) query.regulator = activeScopeRegulator
      }
      const data = await deliveryApi.getAuditOverview(query)
      setOverview(data)
      const list = data?.runs || []
      const candidate =
        preferredRunId ||
        (list.some((r) => r.run_id === selectedRunId) ? selectedRunId : '') ||
        urlRunId ||
        list[0]?.run_id ||
        ''

      if (candidate) {
        await loadDetail(candidate, { silent })
      } else {
        setSelectedRunId('')
        setDetail(null)
      }
    } catch (err) {
      setError(err.message || '加载审计总览失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [
    activePackageId,
    activeScopeRegulator,
    hasWorkspaceFilter,
    loadDetail,
    runKindFilter,
    scopeByProject,
    selectedRunId,
    urlRunId,
  ])

  useEffect(() => {
    loadOverview({ preferredRunId: urlRunId })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePackageId, activeScopeRegulator, scopeByProject, runKindFilter])

  useEffect(() => {
    if (urlRunId && urlRunId !== selectedRunId) {
      loadDetail(urlRunId)
    }
  }, [urlRunId, selectedRunId, loadDetail])

  async function handleReverifyAll() {
    setReverifyBusy(true)
    setNotice('')
    try {
      await loadOverview({ preferredRunId: selectedRunId, silent: true })
      setNotice('已完成全量 SHA-256 哈希链实时重算验签。')
    } finally {
      setReverifyBusy(false)
    }
  }

  function handleSelectRun(runId) {
    const next = new URLSearchParams(params)
    next.set('run', runId)
    setParams(next, { replace: true })
    loadDetail(runId)
  }

  function handleExportEvidence() {
    if (!detail) return
    const payload = {
      exported_at: new Date().toISOString(),
      system: 'Data Rule Studio Compliance Audit Center',
      audit_dossier: detail,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `audit-evidence-${detail.run_id.slice(0, 8)}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    setNotice(`已导出批次 ${detail.run_id.slice(0, 8)} 的全链路合规审计证据 (.json)。`)
  }

  const summary = overview?.summary || {
    total_runs: 0,
    generation_runs: 0,
    ingestion_runs: 0,
    chain_valid_runs: 0,
    chain_broken_runs: 0,
    total_events: 0,
    total_bus_rejections: 0,
    total_self_heals: 0,
    total_human_confirmations: 0,
    total_llm_calls: 0,
    total_tokens: 0,
    verified_at: null,
  }

  const filteredRuns = useMemo(() => {
    const runs = overview?.runs || []
    const q = searchText.trim().toLowerCase()
    return runs.filter((run) => {
      if (anomalyFilter === 'CHAIN_BROKEN' && run.chain_valid) return false
      if (anomalyFilter === 'HAS_HUMAN' && !(run.human_action_count > 0)) return false
      if (anomalyFilter === 'HAS_GOVERNANCE' && !(run.bus_rejected_count > 0 || run.self_heal_count > 0 || run.retry_count > 0)) return false
      if (anomalyFilter === 'HAS_BUNDLE' && !run.bundle_sha256) return false
      if (anomalyFilter === 'FAILED' && !['FAILED', 'EXTRACTION_FAILED', 'OVERVIEW_FAILED', 'CANCELLED'].includes(run.status)) return false
      if (!q) return true
      const haystack = [
        run.run_id,
        run.title,
        run.package_name,
        run.regulator,
        run.status,
        ...(run.tables || []).flatMap((t) => [t.table_code, t.table_name]),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return haystack.includes(q)
    })
  }, [overview?.runs, anomalyFilter, searchText])

  const filteredEvents = useMemo(() => {
    const list = detail?.events || []
    const q = eventSearch.trim().toLowerCase()
    const matched = list.filter((ev) => {
      if (eventGroup === 'HUMAN' && !HUMAN_EVENT_KINDS.has(ev.kind)) return false
      if (eventGroup === 'GOVERNANCE' && !GOVERNANCE_EVENT_KINDS.has(ev.kind)) return false
      if (eventGroup === 'MILESTONE' && !MILESTONE_EVENT_KINDS.has(ev.kind)) return false
      if (eventGroup === 'BROKEN' && ev.chain_ok) return false
      if (!q) return true
      const text = [
        `#${ev.seq}`,
        ev.kind,
        ev.node,
        ev.event_hash,
        ev.prev_hash,
        summarizeAuditEvent(ev),
        JSON.stringify(ev.payload || {}),
      ]
        .join(' ')
        .toLowerCase()
      return text.includes(q)
    })
    return reverseOrder ? [...matched].reverse() : matched
  }, [detail?.events, eventGroup, eventSearch, reverseOrder])

  const waterfallData = useMemo(() => {
    const trace = detail?.trace
    const spans = trace?.spans || []
    if (!spans.length) return { spans: [], totalMs: 0 }
    const startMs = parseBackendDate(trace.started_at)?.getTime() ?? 0
    const endMs = parseBackendDate(trace.ended_at)?.getTime() ?? startMs
    const totalMs = Math.max(Number(trace.total_duration_ms) || (endMs - startMs) || 0, 0)
    const maxSpanDur = Math.max(...spans.map((s) => Number(s.duration_ms) || 0), 1)

    const items = spans.map((span, idx) => {
      const sStart = parseBackendDate(span.started_at)?.getTime() ?? startMs
      const dur = Math.max(Number(span.duration_ms) || 0, 0)
      let leftPct = 0
      let widthPct = 100
      if (totalMs > 0) {
        leftPct = Math.min(Math.max(((sStart - startMs) / totalMs) * 100, 0), 96)
        widthPct = Math.min(Math.max((dur / totalMs) * 100, 2.2), 100 - leftPct)
      } else {
        leftPct = (idx / Math.max(spans.length, 1)) * 75
        widthPct = Math.max((dur / maxSpanDur) * 24, 4)
      }
      return {
        ...span,
        displayLabel: NODE_LABELS[span.node] || span.label || span.node,
        leftPct,
        widthPct,
      }
    })
    return { spans: items, totalMs }
  }, [detail?.trace])

  const scopeLabel = useMemo(() => {
    if (activePackage) {
      const rm = regulatorMeta(activePackage.regulator)
      return `${rm.agencyShortLabel} · ${activePackage.name}`
    }
    if (activeRegulator !== 'ALL') {
      const rm = regulatorMeta(activeRegulator)
      return `${rm.agencyShortLabel} · ${rm.label}`
    }
    if (activeAgency !== 'ALL') {
      return agencyMeta(activeAgency).label
    }
    return '全部监管机构'
  }, [activeAgency, activePackage, activeRegulator])

  const governanceCount =
    (detail?.bus_rejections?.length || 0) +
    (detail?.human_actions?.length || 0) +
    (detail?.self_heals?.length || 0)

  function toggleSeq(seq) {
    setExpandedSeqs((prev) => ({ ...prev, [seq]: !prev[seq] }))
  }

  return (
    <div className="audit-center-page">
      <PageHeader
        eyebrow="EventLog · SHA-256 防篡改事件账本"
        title="全链路合规审计中心"
        description="实时重算全行文档解析与规则验证批次的 SHA-256 哈希链，集中审计节点耗时瀑布、AI 门禁拦截、人工确权留痕与端到端加密指纹。"
        actions={
          <>
            {hasWorkspaceFilter && (
              <div className="project-scope-toggle">
                <button
                  type="button"
                  className={`button sm ${!scopeByProject ? 'primary' : 'secondary'}`}
                  onClick={() => setScopeByProject(false)}
                >
                  全行全量视角
                </button>
                <button
                  type="button"
                  className={`button sm ${scopeByProject ? 'primary' : 'secondary'}`}
                  title={`仅审计当前选中范围：${scopeLabel}`}
                  onClick={() => setScopeByProject(true)}
                >
                  仅当前范围
                </button>
              </div>
            )}
            <button
              type="button"
              className="button secondary sm"
              disabled={reverifyBusy || loading}
              onClick={handleReverifyAll}
            >
              {reverifyBusy ? '正在重算验链…' : '重新验链'}
            </button>
            <button
              type="button"
              className="button primary sm"
              disabled={!detail}
              onClick={handleExportEvidence}
            >
              导出审计证据 (.json)
            </button>
          </>
        }
      />

      {error && <StatusMessage tone="error" message={error} onDismiss={() => setError('')} />}
      {notice && <StatusMessage tone="success" message={notice} onDismiss={() => setNotice('')} />}

      {/* 顶部 4 张全局合规 KPI 卡 */}
      <section className="audit-kpi-grid" aria-label="全链路审计概览指标">
        <article className={`audit-kpi-card ${summary.chain_broken_runs > 0 ? 'is-danger' : 'is-safe'}`}>
          <div className="audit-kpi-head">
            <span>哈希链实时验签</span>
            <StatusBadge tone={summary.chain_broken_runs > 0 ? 'red' : 'green'}>
              {summary.chain_broken_runs > 0 ? `${summary.chain_broken_runs} 链断裂` : '全部完整'}
            </StatusBadge>
          </div>
          <strong className="audit-kpi-value">
            {summary.chain_valid_runs} / {summary.total_runs}
          </strong>
          <small className="audit-kpi-sub">
            {summary.chain_broken_runs > 0
              ? `检测到 ${summary.chain_broken_runs} 个批次存在哈希篡改或断链，请立即核查`
              : `全量 SHA-256 链式哈希无篡改 · 验签于 ${formatTime(summary.verified_at)}`}
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>纳管不可变账本</span>
            <StatusBadge tone="blue">{summary.total_events} 条事件</StatusBadge>
          </div>
          <strong className="audit-kpi-value">{summary.total_runs} 个批次</strong>
          <small className="audit-kpi-sub">
            规则验证 {summary.generation_runs} 批 · 文档解析 {summary.ingestion_runs} 批
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>AI 门禁拦截与自愈</span>
            <StatusBadge tone={summary.total_bus_rejections > 0 ? 'amber' : 'neutral'}>
              {summary.total_bus_rejections > 0 ? '已阻断幻觉' : '零越权'}
            </StatusBadge>
          </div>
          <strong className="audit-kpi-value">
            {summary.total_bus_rejections} 次拦截 / {summary.total_self_heals} 次自愈
          </strong>
          <small className="audit-kpi-sub">
            Bus 工具契约物理拦截至 EventLog，支持 goto 自动回边修正
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>人工确权与 AI 资源</span>
            <StatusBadge tone="violet">BudgetGuard</StatusBadge>
          </div>
          <strong className="audit-kpi-value">{summary.total_human_confirmations} 次人工签批</strong>
          <small className="audit-kpi-sub">
            AI 调用 {summary.total_llm_calls} 次 · 累计 {Number(summary.total_tokens || 0).toLocaleString()} tokens
          </small>
        </article>
      </section>

      {/* 主审计工作台：左批次台账 + 右 4-Tab 核查工作台 */}
      <section className="audit-workspace-grid">
        <aside className="panel audit-ledger-panel" aria-label="审计批次台账">
          <div className="panel-heading">
            <div>
              <h2>全链路审计台账 ({filteredRuns.length})</h2>
              <p>{scopeByProject && hasWorkspaceFilter ? `当前范围：${scopeLabel}` : '全行所有监管机构与报表批次'}</p>
            </div>
          </div>

          <div className="audit-ledger-filters">
            <input
              type="search"
              placeholder="搜索批次 ID、报表名、规范包或监管体系…"
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
            />
            <div className="audit-filter-row">
              <SelectDropdown
                ariaLabel="按链路类型筛选"
                size="sm"
                value={runKindFilter}
                options={RUN_KIND_OPTIONS}
                onChange={setRunKindFilter}
              />
              <SelectDropdown
                ariaLabel="按异常或治理维度筛选"
                size="sm"
                value={anomalyFilter}
                options={ANOMALY_FILTER_OPTIONS}
                onChange={setAnomalyFilter}
              />
            </div>
          </div>

          {loading ? (
            <ListSkeleton rows={6} />
          ) : filteredRuns.length === 0 ? (
            <div className="empty">当前筛选条件下暂无可审计的事件账本批次。</div>
          ) : (
            <div className="audit-ledger-list" role="listbox" aria-label="可审计运行批次列表">
              {filteredRuns.map((run) => {
                const rm = run.regulator ? regulatorMeta(run.regulator) : null
                const active = run.run_id === selectedRunId
                return (
                  <button
                    key={run.run_id}
                    type="button"
                    role="option"
                    aria-selected={active}
                    className={`audit-ledger-card ${active ? 'active' : ''} ${!run.chain_valid ? 'chain-broken' : ''}`}
                    onClick={() => handleSelectRun(run.run_id)}
                  >
                    <div className="audit-ledger-card-top">
                      <span className={`audit-kind-pill ${run.run_kind === 'INGESTION' ? 'is-ingestion' : 'is-generation'}`}>
                        {run.run_kind === 'INGESTION' ? '文档解析' : '规则与沙箱'}
                      </span>
                      {rm && (
                        <span className="audit-reg-chip">
                          {rm.agencyShortLabel} · {rm.shortLabel}
                        </span>
                      )}
                      <span className={`audit-chain-pill ${run.chain_valid ? 'ok' : 'broken'}`}>
                        {run.chain_valid ? '✓ 链完整' : `✕ 断链 #${run.broken_seq}`}
                      </span>
                    </div>

                    <div className="audit-ledger-card-title">
                      <strong title={run.title}>{run.title}</strong>
                      <StatusBadge status={run.status} />
                    </div>

                    <div className="audit-ledger-card-meta">
                      <code>#{run.run_id.slice(0, 8)}</code>
                      <span>{run.event_count} 事件</span>
                      {run.human_action_count > 0 && <span>{run.human_action_count} 确权</span>}
                      {run.bus_rejected_count > 0 && (
                        <span className="danger-text">{run.bus_rejected_count} 拦截</span>
                      )}
                      {run.bundle_sha256 && <span className="audit-signed-tag">已签发包</span>}
                    </div>

                    <div className="audit-ledger-card-foot">
                      <span title={run.package_name || ''}>{run.package_name || '未绑定版本规范包'}</span>
                      <time>{formatTime(run.created_at)}</time>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </aside>

        <div className="panel audit-detail-panel">
          {detailLoading ? (
            <DetailSkeleton blocks={5} />
          ) : !detail ? (
            <div className="empty">请从左侧台账选择任意运行批次，查看完整 SHA-256 哈希链、耗时瀑布与合规证据。</div>
          ) : (
            <>
              {/* 单批次验签横幅与身份头 */}
              <div className={`audit-verify-banner ${detail.chain_valid ? 'is-safe' : 'is-danger'}`}>
                <div className="audit-verify-banner-main">
                  <div className="audit-verify-title-row">
                    <span className={`audit-kind-pill ${detail.run_kind === 'INGESTION' ? 'is-ingestion' : 'is-generation'}`}>
                      {detail.run_kind === 'INGESTION' ? `文档解析 · ${detail.phase}` : '规则与沙箱全流程'}
                    </span>
                    <h2>
                      {detail.tables?.length
                        ? detail.tables.map((t) => t.table_name || t.table_code).slice(0, 2).join('、')
                        : detail.fingerprints?.source_documents?.[0]?.name || `批次 ${detail.run_id.slice(0, 8)}`}
                    </h2>
                    <StatusBadge status={detail.status} />
                  </div>
                  <p className="audit-verify-desc">
                    {detail.chain_valid ? (
                      <>
                        <strong>SHA-256 哈希链验签通过</strong> · 共连续校验{' '}
                        <strong>{detail.events.length}</strong> 条不可变事件（尾哈希{' '}
                        <code>{shortHash(detail.fingerprints?.event_chain?.tail_hash, 14)}</code>），零篡改记录。
                      </>
                    ) : (
                      <>
                        <strong>警告：检测到哈希链断裂（seq #{detail.broken_seq}）</strong> · 该条事件的存储哈希与实时重算 SHA-256 不一致，可能存在绕过应用层的底层篡改！
                      </>
                    )}
                  </p>
                </div>

                <div className="audit-verify-banner-actions">
                  <CopyButton text={detail.run_id} label="复制批次 ID" copiedLabel="已复制 ID" />
                  {detail.run_kind === 'GENERATION' && (
                    <button
                      type="button"
                      className="button secondary sm"
                      onClick={() => navigate(`/runs?run=${encodeURIComponent(detail.run_id)}`)}
                    >
                      前往步骤三详情 →
                    </button>
                  )}
                </div>
              </div>

              {/* 批次关键元数据条 */}
              <div className="audit-meta-strip">
                <div>
                  <span>当前停靠节点</span>
                  <strong><code>{detail.current_node || 'created'}</code></strong>
                </div>
                <div>
                  <span>总墙钟耗时</span>
                  <strong>{formatDurationMs(detail.trace?.total_duration_ms)}</strong>
                </div>
                <div>
                  <span>AI 调用 / Tokens</span>
                  <strong>
                    {detail.budget?.llm_calls ?? 0} 次 · {Number(detail.budget?.total_tokens || 0).toLocaleString()}
                  </strong>
                </div>
                <div>
                  <span>规则防篡改核验</span>
                  <strong>
                    {detail.fingerprints?.rule_tamper_status === 'VERIFIED' ? (
                      <StatusBadge tone="green">零漂移 (VERIFIED)</StatusBadge>
                    ) : detail.fingerprints?.rule_tamper_status === 'DRIFTED' ? (
                      <StatusBadge tone="red">规则已漂移 (DRIFTED)</StatusBadge>
                    ) : (
                      <StatusBadge tone="neutral">不适用 / 待审计</StatusBadge>
                    )}
                  </strong>
                </div>
              </div>

              {/* 四视图切换 Tab */}
              <nav className="validation-tab-bar audit-tabs" aria-label="单批次审计视图切换">
                <button
                  type="button"
                  className={activeTab === 'hash-chain' ? 'active' : ''}
                  onClick={() => setActiveTab('hash-chain')}
                >
                  1. SHA-256 哈希链账本 ({detail.events.length})
                </button>
                <button
                  type="button"
                  className={activeTab === 'trace' ? 'active' : ''}
                  onClick={() => setActiveTab('trace')}
                >
                  2. 节点耗时瀑布 ({waterfallData.spans.length})
                </button>
                <button
                  type="button"
                  className={activeTab === 'governance' ? 'active' : ''}
                  onClick={() => setActiveTab('governance')}
                >
                  3. AI 门禁与人工确权 ({governanceCount})
                </button>
                <button
                  type="button"
                  className={activeTab === 'fingerprints' ? 'active' : ''}
                  onClick={() => setActiveTab('fingerprints')}
                >
                  4. 端到端指纹与证据导出
                </button>
              </nav>

              {/* Tab 1: SHA-256 哈希链逐条账本 */}
              {activeTab === 'hash-chain' && (
                <section className="audit-tab-body">
                  <div className="audit-chain-toolbar">
                    <input
                      type="search"
                      placeholder="按 #seq、事件名、节点、哈希前缀或载荷关键字过滤…"
                      value={eventSearch}
                      onChange={(e) => setEventSearch(e.target.value)}
                    />
                    <SelectDropdown
                      ariaLabel="按事件分类筛选"
                      size="sm"
                      minWidth={170}
                      value={eventGroup}
                      options={EVENT_GROUP_OPTIONS}
                      onChange={setEventGroup}
                    />
                    <button
                      type="button"
                      className="button secondary sm"
                      onClick={() => setReverseOrder((v) => !v)}
                    >
                      {reverseOrder ? '当前：倒序（新→旧）' : '当前：链式正序（创世→终态）'}
                    </button>
                  </div>

                  {!reverseOrder && eventGroup === 'ALL' && !eventSearch && (
                    <div className="audit-genesis-anchor">
                      <span className="audit-genesis-tag">GENESIS 创世锚点</span>
                      <code>{detail.fingerprints?.event_chain?.genesis_hash}</code>
                    </div>
                  )}

                  {filteredEvents.length === 0 ? (
                    <div className="empty">没有匹配的事件记录。</div>
                  ) : (
                    <ol className="audit-chain-list">
                      {filteredEvents.map((ev) => {
                        const open = Boolean(expandedSeqs[ev.seq])
                        return (
                          <li
                            key={ev.seq}
                            className={`audit-chain-item ${ev.chain_ok ? '' : 'is-broken'}`}
                          >
                            <div className="audit-chain-row-main">
                              <button
                                type="button"
                                className="audit-chain-toggle"
                                aria-expanded={open}
                                onClick={() => toggleSeq(ev.seq)}
                              >
                                <span className={`audit-seq-badge ${ev.chain_ok ? 'ok' : 'broken'}`}>
                                  {ev.chain_ok ? '✓' : '✕'} #{ev.seq}
                                </span>
                                <StatusBadge tone={toneForEventKind(ev.kind, ev.chain_ok)}>
                                  {ev.kind}
                                </StatusBadge>
                                {ev.node && (
                                  <span className="audit-node-tag" title={ev.node}>
                                    {NODE_LABELS[ev.node] || ev.node}
                                  </span>
                                )}
                                <span className="audit-event-summary">{summarizeAuditEvent(ev)}</span>
                                <time className="audit-event-time">{formatTime(ev.created_at)}</time>
                              </button>

                              <div className="audit-hash-link-bar">
                                <span className="audit-hash-chip" title={`前置事件哈希: ${ev.prev_hash}`}>
                                  <em>prev</em> <code>{shortHash(ev.prev_hash, 10)}</code>
                                </span>
                                <span className="audit-hash-arrow" aria-hidden="true">→</span>
                                <span className="audit-hash-chip is-self" title={`当前事件 SHA-256: ${ev.event_hash}`}>
                                  <em>sha256</em> <code>{shortHash(ev.event_hash, 12)}</code>
                                </span>
                                <CopyButton text={ev.event_hash} label="复制哈希" copiedLabel="已复制" className="button ghost sm" />
                                {!ev.chain_ok && (
                                  <span className="audit-broken-hint">
                                    验签失败！期望哈希：<code>{shortHash(ev.expected_hash, 12)}</code>
                                  </span>
                                )}
                              </div>
                            </div>

                            {open && (
                              <div className="audit-chain-payload-box">
                                <div className="audit-chain-payload-head">
                                  <span>完整事件载荷（参与 SHA-256 签名原文）</span>
                                  <CopyButton
                                    text={JSON.stringify(ev, null, 2)}
                                    label="复制 JSON"
                                    copiedLabel="已复制"
                                    className="button ghost sm"
                                  />
                                </div>
                                <pre className="trace-payload">{JSON.stringify(ev.payload, null, 2)}</pre>
                              </div>
                            )}
                          </li>
                        )
                      })}
                    </ol>
                  )}
                </section>
              )}

              {/* Tab 2: 节点耗时瀑布 (Trace Waterfall) */}
              {activeTab === 'trace' && (
                <section className="audit-tab-body">
                  <div className="audit-metrics" style={{ marginBottom: 14 }}>
                    <div>
                      <span>开始 / 结束时间</span>
                      <strong>{formatTime(detail.trace?.started_at)} → {formatTime(detail.trace?.ended_at)}</strong>
                    </div>
                    <div>
                      <span>全流程墙钟耗时</span>
                      <strong>{formatDurationMs(waterfallData.totalMs)}</strong>
                    </div>
                    <div>
                      <span>人工中断 / 重试</span>
                      <strong>{detail.trace?.interrupt_count ?? 0} 次中断 / {detail.trace?.retry_count ?? 0} 次重试</strong>
                    </div>
                    <div>
                      <span>Bus 门禁拦截</span>
                      <strong>{detail.trace?.rejection_count ?? 0} 次</strong>
                    </div>
                  </div>

                  {waterfallData.spans.length === 0 ? (
                    <div className="empty">该批次暂无可投影的执行跨度（Span）。</div>
                  ) : (
                    <div className="audit-waterfall">
                      <div className="audit-waterfall-header">
                        <span>执行节点与分片</span>
                        <span>时间轴瀑布分布（相对批次起点）</span>
                        <span>耗时 / 事件段</span>
                      </div>
                      {waterfallData.spans.map((span, idx) => (
                        <div className="audit-waterfall-row" key={`${span.node}-${span.seq_start}-${idx}`}>
                          <div className="audit-waterfall-node">
                            <div className="audit-waterfall-node-top">
                              <strong>{span.displayLabel}</strong>
                              <StatusBadge tone={spanStatusTone(span.status)}>
                                {spanStatusLabel(span.status)}
                              </StatusBadge>
                            </div>
                            <div className="audit-waterfall-node-sub">
                              <code>{span.node}</code>
                              {span.shard && <span className="audit-shard-pill">分片 {span.shard}</span>}
                              {span.retries > 0 && <span className="audit-warn-pill">重试 ×{span.retries}</span>}
                              {span.rejections > 0 && <span className="audit-err-pill">拦截 ×{span.rejections}</span>}
                            </div>
                          </div>

                          <div className="audit-waterfall-track-cell">
                            <div className="audit-waterfall-track">
                              <div
                                className={`audit-waterfall-bar tone-${spanStatusTone(span.status)}`}
                                style={{ left: `${span.leftPct}%`, width: `${span.widthPct}%` }}
                                title={`${span.displayLabel}: ${formatDurationMs(span.duration_ms)} (${formatTime(span.started_at)} ~ ${formatTime(span.ended_at)})`}
                              />
                            </div>
                            <div className="audit-waterfall-kinds">
                              {(span.kinds || []).map((k, kIdx) => (
                                <span key={`${k}-${kIdx}`}>{k}</span>
                              ))}
                            </div>
                          </div>

                          <div className="audit-waterfall-dur">
                            <strong>{formatDurationMs(span.duration_ms)}</strong>
                            <small>#{span.seq_start}–#{span.seq_end} ({span.event_count})</small>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              )}

              {/* Tab 3: AI 门禁 & 人工确权 */}
              {activeTab === 'governance' && (
                <section className="audit-tab-body audit-gov-stack">
                  <div className="audit-gov-section">
                    <div className="audit-gov-head">
                      <h3>人工审批与确权签批 ({detail.human_actions?.length || 0})</h3>
                      <span className="muted-text">所有人工介入均绑定操作人、时间与回答指纹（answer_fingerprint）写入账本</span>
                    </div>
                    {!detail.human_actions?.length ? (
                      <div className="empty">该批次尚未产生人工审批、参数修改或测试结果确权记录。</div>
                    ) : (
                      <div className="audit-gov-list">
                        {detail.human_actions.map((item) => (
                          <article className="audit-gov-card" key={`human-${item.seq}`}>
                            <div className="audit-gov-card-top">
                              <span className="audit-seq-badge ok">#{item.seq}</span>
                              <StatusBadge tone="green">{item.kind}</StatusBadge>
                              <strong>操作人：{item.actor}</strong>
                              <span className="audit-node-tag">{NODE_LABELS[item.node] || item.node}</span>
                              <time>{formatTime(item.created_at)}</time>
                            </div>
                            <p className="audit-gov-summary">{item.summary}</p>
                            <div className="audit-gov-fingerprints">
                              {item.payload?.answer_fingerprint && (
                                <span className="audit-hash-chip">
                                  <em>签批指纹</em> <code>{shortHash(item.payload.answer_fingerprint, 14)}</code>
                                </span>
                              )}
                              {item.payload?.object_hash && (
                                <span className="audit-hash-chip">
                                  <em>对象哈希</em> <code>{shortHash(item.payload.object_hash, 14)}</code>
                                </span>
                              )}
                              <span className="audit-hash-chip is-self">
                                <em>账本哈希</em> <code>{shortHash(item.event_hash, 14)}</code>
                              </span>
                            </div>
                          </article>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="audit-gov-section">
                    <div className="audit-gov-head">
                      <h3>Bus 物理门禁拦截 ({detail.bus_rejections?.length || 0})</h3>
                      <span className="muted-text">阻断 AI 编造字段名、非法参数或越权调用，防止脏状态污染数据库</span>
                    </div>
                    {!detail.bus_rejections?.length ? (
                      <div className="empty">本次运行零门禁拦截，全部 AI 工具调用均一次通过契约校验。</div>
                    ) : (
                      <div className="audit-gov-list">
                        {detail.bus_rejections.map((item) => (
                          <article className="audit-gov-card is-danger" key={`bus-${item.seq}`}>
                            <div className="audit-gov-card-top">
                              <span className="audit-seq-badge broken">#{item.seq}</span>
                              <StatusBadge tone="red">BUS_REJECTED</StatusBadge>
                              <code>{item.tool || 'unknown_tool'}</code>
                              <span className="audit-node-tag">{NODE_LABELS[item.node] || item.node}</span>
                              <time>{formatTime(item.created_at)}</time>
                            </div>
                            <p className="audit-gov-summary">{item.reason || '契约校验未通过'}</p>
                          </article>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="audit-gov-section">
                    <div className="audit-gov-head">
                      <h3>自动自愈回边与断点续跑 ({detail.self_heals?.length || 0})</h3>
                      <span className="muted-text">记录 goto 回边纠偏、节点自动重试及崩溃后的检查点恢复轨迹</span>
                    </div>
                    {!detail.self_heals?.length ? (
                      <div className="empty">本次运行未发生回边重算或断点续跑。</div>
                    ) : (
                      <div className="audit-gov-list">
                        {detail.self_heals.map((item) => (
                          <article className="audit-gov-card is-warn" key={`heal-${item.seq}`}>
                            <div className="audit-gov-card-top">
                              <span className="audit-seq-badge ok">#{item.seq}</span>
                              <StatusBadge tone="amber">{item.kind}</StatusBadge>
                              <span className="audit-node-tag">
                                {NODE_LABELS[item.node] || item.node}
                                {item.target ? ` → ${NODE_LABELS[item.target] || item.target}` : ''}
                              </span>
                              <time>{formatTime(item.created_at)}</time>
                            </div>
                            <p className="audit-gov-summary">{item.reason || '系统自动触发恢复策略'}</p>
                          </article>
                        ))}
                      </div>
                    )}
                  </div>
                </section>
              )}

              {/* Tab 4: 端到端加密指纹 & 证据导出 */}
              {activeTab === 'fingerprints' && (
                <section className="audit-tab-body">
                  <div className="audit-fingerprint-grid">
                    {/* 1. 源文件指纹 */}
                    <article className="audit-fp-card">
                      <div className="audit-fp-step">环节 01 · 监管制度原文</div>
                      <h3>源文件 SHA-256 指纹</h3>
                      {(detail.fingerprints?.source_documents || []).length > 0 ? (
                        <ul className="audit-fp-list">
                          {detail.fingerprints.source_documents.map((doc, idx) => (
                            <li key={`${doc.name}-${idx}`}>
                              <div className="audit-fp-doc-head">
                                <strong>{doc.name || '未命名制度文档'}</strong>
                                {doc.table_code && <code>{doc.table_code}</code>}
                                {doc.doc_role && <span className="audit-reg-chip">{doc.doc_role}</span>}
                              </div>
                              <div className="audit-fp-hash-row">
                                <code>{doc.sha256 || '未记录哈希'}</code>
                                {doc.sha256 && <CopyButton text={doc.sha256} label="复制" className="button ghost sm" />}
                              </div>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="muted-text">未关联原始制度文档指纹。</p>
                      )}
                    </article>

                    {/* 2. EventLog 链锚点 */}
                    <article className="audit-fp-card">
                      <div className="audit-fp-step">环节 02 · EventLog 账本链</div>
                      <h3>创世 → 尾部 SHA-256 锚点</h3>
                      <div className="audit-fp-kv">
                        <div>
                          <span>连续事件区间</span>
                          <strong>
                            #{detail.fingerprints?.event_chain?.first_seq ?? '—'} → #
                            {detail.fingerprints?.event_chain?.last_seq ?? '—'}（共{' '}
                            {detail.fingerprints?.event_chain?.event_count ?? 0} 条）
                          </strong>
                        </div>
                        <div>
                          <span>链式验签结论</span>
                          <StatusBadge tone={detail.chain_valid ? 'green' : 'red'}>
                            {detail.chain_valid ? '完整不可篡改' : `链断裂 #${detail.broken_seq}`}
                          </StatusBadge>
                        </div>
                        <div className="audit-fp-full-row">
                          <span>Tail Hash（当前终态链尾签名）</span>
                          <div className="audit-fp-hash-row">
                            <code>{detail.fingerprints?.event_chain?.tail_hash || '—'}</code>
                            <CopyButton
                              text={detail.fingerprints?.event_chain?.tail_hash}
                              label="复制"
                              className="button ghost sm"
                            />
                          </div>
                        </div>
                      </div>
                    </article>

                    {/* 3. 规则防篡改与沙箱报告 */}
                    <article className="audit-fp-card">
                      <div className="audit-fp-step">环节 03 · 规则与沙箱封存</div>
                      <h3>规则零漂移校验 & 审计报告</h3>
                      <div className="audit-fp-kv">
                        <div>
                          <span>已批准规则防篡改状态</span>
                          {detail.fingerprints?.rule_tamper_status === 'VERIFIED' ? (
                            <StatusBadge tone="green">VERIFIED · 与审计快照一致</StatusBadge>
                          ) : detail.fingerprints?.rule_tamper_status === 'DRIFTED' ? (
                            <StatusBadge tone="red">DRIFTED · 规则集合已漂移</StatusBadge>
                          ) : (
                            <StatusBadge tone="neutral">不适用 / 尚未到达审计节点</StatusBadge>
                          )}
                        </div>
                        {detail.fingerprints?.active_rule_count !== null && (
                          <div>
                            <span>纳入审计规则数</span>
                            <strong>{detail.fingerprints.active_rule_count} 条</strong>
                          </div>
                        )}
                        <div className="audit-fp-full-row">
                          <span>Oracle 审计报告 Blob 地址</span>
                          <div className="audit-fp-hash-row">
                            <code>{detail.fingerprints?.audit_report_blob || '尚未生成沙箱验证报告'}</code>
                            {detail.fingerprints?.audit_report_blob && (
                              <CopyButton
                                text={detail.fingerprints.audit_report_blob}
                                label="复制"
                                className="button ghost sm"
                              />
                            )}
                          </div>
                        </div>
                      </div>
                    </article>

                    {/* 4. 生产部署包指纹 */}
                    <article className="audit-fp-card">
                      <div className="audit-fp-step">环节 04 · 生产交付物签发</div>
                      <h3>生产部署包 (.zip) 数字指纹</h3>
                      {detail.fingerprints?.production_bundle ? (
                        <div className="audit-fp-kv">
                          <div>
                            <span>签发版本 / 时间</span>
                            <strong>
                              v{detail.fingerprints.production_bundle.version} ·{' '}
                              {formatTime(detail.fingerprints.production_bundle.created_at)}
                            </strong>
                          </div>
                          <div>
                            <span>Artifact URI</span>
                            <code>{detail.fingerprints.production_bundle.blob_uri}</code>
                          </div>
                          <div className="audit-fp-full-row">
                            <span>生产包 SHA-256</span>
                            <div className="audit-fp-hash-row">
                              <code>{detail.fingerprints.production_bundle.sha256}</code>
                              <CopyButton
                                text={detail.fingerprints.production_bundle.sha256}
                                label="复制"
                                className="button ghost sm"
                              />
                            </div>
                          </div>
                        </div>
                      ) : (
                        <p className="muted-text">
                          该批次尚未导出生产部署包，或属于文档解析阶段批次。
                        </p>
                      )}
                    </article>
                  </div>

                  <div className="action-callout" style={{ marginTop: 14 }}>
                    <div>
                      <strong>银行合规检查 / 内审归档证据导出</strong>
                      <p>
                        导出的 JSON 证据包完整封装：实时 SHA-256 逐条验签结果、全部不可变事件原文、节点耗时瀑布、人工审批签批指纹、Bus 拦截记录及端到端文件/生产包哈希。
                      </p>
                    </div>
                    <button type="button" className="button primary sm" onClick={handleExportEvidence}>
                      下载合规审计证据 (.json)
                    </button>
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  )
}
