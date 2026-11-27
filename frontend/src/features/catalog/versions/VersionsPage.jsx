import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listMetadataRevisions, listMetadataTables } from '../../../api/catalog.js'
import { listRuns } from '../../../api/delivery.js'
import PageHeader from '../../../components/PageHeader.jsx'
import { StatusBadge } from '../../../components/StatusBadge.jsx'
import StatusMessage from '../../../components/StatusMessage.jsx'
import { useWorkspace } from '../../../context/WorkspaceContext.jsx'
import { agencyMeta, normalizeReportCode, regulatorMeta } from '../../../utils/activeDocument.js'
import ChangeFeedTab from './ChangeFeedTab.jsx'
import PackageDiffTab from './PackageDiffTab.jsx'
import RunSqlDiffTab from './RunSqlDiffTab.jsx'

export default function VersionsPage() {
  const navigate = useNavigate()
  const { packages, activeAgency, activeRegulator, activePackageId, activePackage } = useWorkspace()

  const activeScopeRegulator =
    activeRegulator !== 'ALL' ? activeRegulator : (activeAgency !== 'ALL' ? activeAgency : '')
  const hasWorkspaceFilter = Boolean(activePackageId || activeScopeRegulator)

  const [scopeByProject, setScopeByProject] = useState(false)
  const [activeTab, setActiveTab] = useState('feed')

  // Tab 1 变更流状态（默认聚焦人工修改与发布，去噪隐藏大批量 AI 初始提取）
  const [humanOnly, setHumanOnly] = useState(true)
  const [kindFilter, setKindFilter] = useState('ALL')
  const [revisions, setRevisions] = useState([])
  const [revisionSummary, setRevisionSummary] = useState({
    total: 0,
    human_edits: 0,
    publishes: 0,
    drafts: 0,
    ai_events: 0,
    tables_touched: 0,
  })
  const [revisionsLoading, setRevisionsLoading] = useState(true)

  // 共享资产状态（报表列表 + 验证批次列表）
  const [tables, setTables] = useState([])
  const [runs, setRuns] = useState([])
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const queryScope = useMemo(() => {
    if (!scopeByProject || !hasWorkspaceFilter) return {}
    return {
      packageId: activePackageId || undefined,
      regulator: activeScopeRegulator || undefined,
    }
  }, [scopeByProject, hasWorkspaceFilter, activePackageId, activeScopeRegulator])

  const loadRevisions = useCallback(async () => {
    setRevisionsLoading(true)
    try {
      const data = await listMetadataRevisions({
        ...queryScope,
        humanOnly,
        revisionKind: kindFilter,
        limit: 120,
      })
      setRevisions(data?.revisions || [])
      if (data?.summary) setRevisionSummary(data.summary)
    } catch (err) {
      setError(err.message || '加载变更动态失败')
    } finally {
      setRevisionsLoading(false)
    }
  }, [queryScope, humanOnly, kindFilter])

  const loadAssets = useCallback(async () => {
    setAssetsLoading(true)
    try {
      const [tableList, runList] = await Promise.all([
        listMetadataTables(queryScope).catch(() => []),
        listRuns(queryScope).catch(() => []),
      ])
      setTables(tableList)
      setRuns(runList)
    } catch (err) {
      setError(err.message || '加载资产版本信息失败')
    } finally {
      setAssetsLoading(false)
    }
  }, [queryScope])

  useEffect(() => {
    loadRevisions()
  }, [loadRevisions])

  useEffect(() => {
    loadAssets()
  }, [loadAssets])

  function handleOpenTable(tableId, documentId) {
    const qs = new URLSearchParams({ view: 'fields', table: tableId })
    if (documentId) qs.set('document_id', documentId)
    navigate(`/documents?${qs.toString()}`)
  }

  // 统计同一监管报表下存在多版本的体系数 & 同表有多次运行的报表数
  const upgradeablePackageStats = useMemo(() => {
    const byReg = new Map()
    for (const pkg of packages) {
      const code = normalizeReportCode(pkg.regulator)
      byReg.set(code, (byReg.get(code) || 0) + 1)
    }
    const multiRegCount = [...byReg.values()].filter((c) => c >= 2).length
    const inheritedCount = packages.filter((p) => Boolean(p.parent_package_id)).length
    return { total: packages.length, multiRegCount, inheritedCount }
  }, [packages])

  const comparableTableRuns = useMemo(() => {
    const counts = new Map()
    for (const r of runs) {
      for (const t of r.tables || []) {
        if (t.table_code) counts.set(t.table_code, (counts.get(t.table_code) || 0) + 1)
      }
    }
    return [...counts.values()].filter((c) => c >= 2).length
  }, [runs])

  const scopeLabel = useMemo(() => {
    if (activePackage) {
      const rm = regulatorMeta(activePackage.regulator)
      return `${rm.agencyShortLabel} · ${activePackage.name}`
    }
    if (activeRegulator !== 'ALL') {
      const rm = regulatorMeta(activeRegulator)
      return `${rm.agencyShortLabel} · ${rm.label}`
    }
    if (activeAgency !== 'ALL') return agencyMeta(activeAgency).label
    return '全部监管机构'
  }, [activeAgency, activePackage, activeRegulator])

  const publishedTableCount = useMemo(
    () => tables.filter((t) => t.status === 'PUBLISHED').length,
    [tables],
  )

  return (
    <div className="versions-center-page">
      <PageHeader
        eyebrow="资产治理 · 版本迭代与影响面"
        title="版本演进与变更中心"
        description="追踪监管报表字段的人工修改与发布动态，评估新旧制度升级对报表/码表/规则的影响面，对比历史验证批次的 Oracle SQL 差异。"
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
                  title={`仅查看当前范围：${scopeLabel}`}
                  onClick={() => setScopeByProject(true)}
                >
                  仅当前范围
                </button>
              </div>
            )}
            <button
              type="button"
              className="button secondary sm"
              onClick={() => navigate('/audit')}
            >
              前往审计中心 (哈希链) →
            </button>
          </>
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      {/* 顶部 4 张资产演进 KPI 卡 */}
      <section className="audit-kpi-grid" aria-label="版本演进概览指标">
        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>人工修订与版本发布</span>
            <StatusBadge tone="green">{revisionSummary.publishes} 次发布</StatusBadge>
          </div>
          <strong className="audit-kpi-value">{revisionSummary.human_edits} 次人工改字段</strong>
          <small className="audit-kpi-sub">
            累计 {revisionSummary.total} 条记录（含 {revisionSummary.ai_events} 条 AI 自动识别/复核）
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>监管规范包版本基线</span>
            <StatusBadge tone={upgradeablePackageStats.multiRegCount > 0 ? 'blue' : 'neutral'}>
              {upgradeablePackageStats.multiRegCount > 0
                ? `${upgradeablePackageStats.multiRegCount} 体系可升级对比`
                : '均为单版基线'}
            </StatusBadge>
          </div>
          <strong className="audit-kpi-value">{upgradeablePackageStats.total} 套规范包</strong>
          <small className="audit-kpi-sub">
            {upgradeablePackageStats.inheritedCount > 0
              ? `其中 ${upgradeablePackageStats.inheritedCount} 套设置了前置基线继承关系`
              : '新建下年度规范包并继承旧版后，可自动生成升级影响面报告'}
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>当前范围报表资产</span>
            <StatusBadge tone="blue">{publishedTableCount} 已发布</StatusBadge>
          </div>
          <strong className="audit-kpi-value">{tables.length} 张报表</strong>
          <small className="audit-kpi-sub">
            {scopeByProject && hasWorkspaceFilter ? `当前范围：${scopeLabel}` : '全行所有有效规范包纳管报表'}
          </small>
        </article>

        <article className="audit-kpi-card">
          <div className="audit-kpi-head">
            <span>可对比验证批次</span>
            <StatusBadge tone="violet">Oracle SQL</StatusBadge>
          </div>
          <strong className="audit-kpi-value">{runs.length} 个批次</strong>
          <small className="audit-kpi-sub">
            {comparableTableRuns > 0
              ? `${comparableTableRuns} 张报表拥有多批次历史，支持同表 SQL Diff`
              : '同一张表运行 2 次以上即可自动配对对比生产 SQL 变化'}
          </small>
        </article>
      </section>

      <nav className="validation-tab-bar" aria-label="版本变更视图切换">
        <button
          type="button"
          className={activeTab === 'feed' ? 'active' : ''}
          onClick={() => setActiveTab('feed')}
        >
          1. 变更动态与修订时间线 ({revisionSummary.human_edits + revisionSummary.publishes})
        </button>
        <button
          type="button"
          className={activeTab === 'pkg-diff' ? 'active' : ''}
          onClick={() => setActiveTab('pkg-diff')}
        >
          2. 制度升级对比 & 影响面
        </button>
        <button
          type="button"
          className={activeTab === 'run-diff' ? 'active' : ''}
          onClick={() => setActiveTab('run-diff')}
        >
          3. 规则与 SQL 版本对比 ({runs.length})
        </button>
      </nav>

      {activeTab === 'feed' && (
        <ChangeFeedTab
          revisions={revisions}
          revisionSummary={revisionSummary}
          revisionsLoading={revisionsLoading}
          humanOnly={humanOnly}
          onToggleHumanOnly={setHumanOnly}
          kindFilter={kindFilter}
          onChangeKindFilter={setKindFilter}
          tables={tables}
          tablesLoading={assetsLoading}
          onOpenTable={handleOpenTable}
          onError={setError}
        />
      )}

      {activeTab === 'pkg-diff' && (
        <PackageDiffTab
          packages={packages}
          activePackageId={activePackageId}
          activePackage={activePackage}
          activeRegulator={activeRegulator}
          onNotice={setNotice}
          onError={setError}
        />
      )}

      {activeTab === 'run-diff' && (
        <RunSqlDiffTab
          runs={runs}
          onError={setError}
        />
      )}
    </div>
  )
}
