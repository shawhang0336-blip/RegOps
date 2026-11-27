import { useMemo, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import SelectDropdown from '../components/SelectDropdown.jsx'
import { useFeedback } from '../context/FeedbackContext.jsx'
import { useWorkspace } from '../context/WorkspaceContext.jsx'
import {
  REGULATOR_PROJECTS,
  REGULATORY_AGENCIES,
  agencyCodeForReport,
  agencyMeta,
  normalizeReportCode,
  regulatorMeta,
} from '../utils/activeDocument.js'

export default function ProjectContextBar() {
  const {
    packages,
    activeAgency,
    activeRegulator,
    activePackageId,
    activePackage,
    packagesInActiveRegulator,
    agencyGroups,
    reportsInActiveAgency,
    selectAgency,
    selectRegulator,
    selectPackage,
    createProjectPackage,
    deleteProjectPackage,
  } = useWorkspace()
  const { confirm } = useFeedback()
  const navigate = useNavigate()
  const location = useLocation()
  const [params, setParams] = useSearchParams()

  const [showCreate, setShowCreate] = useState(false)
  const [newAgency, setNewAgency] = useState(() => (activeAgency && activeAgency !== 'ALL' ? activeAgency : 'NFRA'))
  const [newReport, setNewReport] = useState(() => {
    if (activeRegulator && activeRegulator !== 'ALL') return activeRegulator
    return 'YBT'
  })
  const [customReport, setCustomReport] = useState('')
  const [newName, setNewName] = useState('')
  const [newVersion, setNewVersion] = useState('')
  const [parentId, setParentId] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  function syncUrlPackage(nextPkgId) {
    if (!['/documents', '/generation', '/runs', '/versions', '/audit', '/'].includes(location.pathname)) return
    const next = new URLSearchParams(params)
    if (nextPkgId) {
      next.set('package_id', nextPkgId)
      next.delete('document_id')
    } else {
      next.delete('package_id')
    }
    next.delete('table')
    next.delete('set')
    if (location.pathname === '/audit') {
      next.delete('run')
    }
    setParams(next, { replace: true })
  }

  function handleSelectAgency(code) {
    const clean = code === 'ALL' ? 'ALL' : code
    selectAgency(clean)
    const matches = clean === 'ALL'
      ? packages
      : packages.filter((p) => agencyCodeForReport(p.regulator) === clean)
    if (matches.length > 0) {
      if (!matches.some((m) => m.id === activePackageId)) {
        syncUrlPackage(matches[0].id)
      }
    } else {
      syncUrlPackage('')
    }
  }

  function handleSelectReport(code) {
    const clean = code === 'ALL' ? 'ALL' : normalizeReportCode(code)
    selectRegulator(clean)
    const targetAg = clean !== 'ALL' ? agencyCodeForReport(clean) : activeAgency
    const matches = packages.filter((p) => {
      const rep = normalizeReportCode(p.regulator)
      if (clean !== 'ALL') return rep === clean
      if (targetAg && targetAg !== 'ALL') return agencyCodeForReport(rep) === targetAg
      return true
    })
    if (matches.length > 0) {
      if (!matches.some((m) => m.id === activePackageId)) {
        syncUrlPackage(matches[0].id)
      }
    } else {
      syncUrlPackage('')
    }
  }

  function handleSelectPackage(nextPkgId) {
    selectPackage(nextPkgId)
    syncUrlPackage(nextPkgId)
  }

  function handleCreateAgencyChange(nextAg) {
    setNewAgency(nextAg)
    const firstReport = REGULATOR_PROJECTS.find((r) => r.agency === nextAg)
    if (firstReport) {
      setNewReport(firstReport.code)
      setCustomReport('')
    } else {
      setNewReport('CUSTOM_INPUT')
    }
  }

  function openCreateForm() {
    const presetAg = activeAgency && activeAgency !== 'ALL'
      ? activeAgency
      : activePackage
        ? agencyCodeForReport(activePackage.regulator)
        : 'NFRA'
    setNewAgency(presetAg)
    const presetRep = activeRegulator && activeRegulator !== 'ALL'
      ? activeRegulator
      : REGULATOR_PROJECTS.find((r) => r.agency === presetAg)?.code || 'CUSTOM_INPUT'
    const isBuiltin = REGULATOR_PROJECTS.some((r) => r.code === presetRep)
    if (isBuiltin) {
      setNewReport(presetRep)
      setCustomReport('')
    } else {
      setNewReport('CUSTOM_INPUT')
      setCustomReport(presetRep === 'CUSTOM_INPUT' ? '' : presetRep)
    }
    setParentId(activePackageId || '')
    setErr('')
    setShowCreate((prev) => !prev)
  }

  async function handleCreateSubmit(event) {
    event.preventDefault()
    const targetReport = (newReport === 'CUSTOM_INPUT' ? customReport : newReport).trim()
    if (!targetReport || !newName.trim()) {
      setErr('请填写监管报表体系与版本名称')
      return
    }
    setBusy(true)
    setErr('')
    try {
      const created = await createProjectPackage({
        name: newName.trim(),
        regulator: targetReport,
        spec_version: newVersion.trim() || '1.0',
        parent_package_id: parentId || null,
      })
      setNewName('')
      setNewVersion('')
      setParentId('')
      setShowCreate(false)
      if (!['/documents', '/generation', '/runs', '/versions', '/'].includes(location.pathname)) {
        navigate(`/documents?package_id=${encodeURIComponent(created.id)}`)
      } else {
        syncUrlPackage(created.id)
      }
    } catch (error) {
      setErr(error.message || '创建规范包失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteCurrentPackage() {
    if (!activePackage) return
    const meta = regulatorMeta(activePackage.regulator)
    const ok = await confirm({
      title: `删除版本规范包「${activePackage.name}」？`,
      message: `所属监管体系：${meta.agencyLabel} · ${meta.label}（含 ${activePackage.document_count} 份文档、${activePackage.table_count} 张表）`,
      details: '将标记该规范包为已删除，避免误选；历史运行记录仍保留在事件账本中。',
      confirmLabel: '确认删除',
      tone: 'danger',
    })
    if (!ok) return
    setBusy(true)
    try {
      await deleteProjectPackage(activePackage.id)
      syncUrlPackage('')
    } catch (error) {
      setErr(error.message || '删除规范包失败')
    } finally {
      setBusy(false)
    }
  }

  const activeAgencyInfo = agencyMeta(activeAgency)
  const activeReportInfo = regulatorMeta(activePackage?.regulator || activeRegulator)
  const activeAgencyCount = activeAgency === 'ALL'
    ? packages.length
    : agencyGroups.find((g) => g.code === activeAgency)?.count || 0

  const emptyScopeLabel = activeRegulator !== 'ALL'
    ? `${activeReportInfo.agencyShortLabel} · ${activeReportInfo.label}`
    : activeAgency !== 'ALL'
      ? activeAgencyInfo.label
      : '全部监管机构'

  const packageDropdownOptions = useMemo(
    () =>
      packagesInActiveRegulator.map((pkg) => {
        const pm = regulatorMeta(pkg.regulator)
        const details = [
          pkg.code_set_count > 0 ? `码表 ${pkg.code_set_count}` : null,
          pkg.rule_evidence_count > 0 ? `补充规则 ${pkg.rule_evidence_count} 条` : null,
          pkg.matrix_report_count > 0 ? `矩阵表样 ${pkg.matrix_report_count} 张` : null,
        ]
          .filter(Boolean)
          .join(' · ')
        return {
          value: pkg.id,
          badge: `${pm.agencyShortLabel} · ${pm.shortLabel}`,
          label: pkg.name,
          tag: pkg.spec_version && pkg.spec_version !== 'UNKNOWN' ? `v${pkg.spec_version}` : '',
          meta: `${pkg.document_count} 文档 · ${pkg.table_count} 表`,
          description: details || pm.label,
        }
      }),
    [packagesInActiveRegulator],
  )

  const createAgencyOptions = useMemo(
    () =>
      REGULATORY_AGENCIES.map((ag) => ({
        value: ag.code,
        label: ag.label,
        description: ag.hint,
      })),
    [],
  )

  const createReportOptions = useMemo(() => {
    const items = REGULATOR_PROJECTS.filter((item) => item.agency === newAgency).map((item) => ({
      value: item.code,
      label: item.label,
      tag: item.paradigm === 'MATRIX' ? '矩阵' : '',
      description: item.hint,
    }))
    items.push({
      value: 'CUSTOM_INPUT',
      label: '自定义监管报表',
      description: '地方派出机构或行内特色监管报送项目',
    })
    return items
  }, [newAgency])

  const parentPackageOptions = useMemo(
    () => [
      { value: '', label: '全新版本（无前置基线）', description: '从零建立独立规范包' },
      ...packages.map((p) => {
        const pm = regulatorMeta(p.regulator)
        return {
          value: p.id,
          badge: pm.shortLabel,
          label: p.name,
          meta: `${p.table_count} 表`,
          description: `升级自 ${pm.agencyShortLabel} · ${pm.label}`,
        }
      }),
    ],
    [packages],
  )

  return (
    <section className="project-context-bar" aria-label="银行监管机构、监管报表与版本切换栏">
      {/* 一级：监管机构 */}
      <div className="project-pills-row project-agency-row">
        <span className="project-bar-label">监管机构</span>
        <div className="project-pills" role="tablist" aria-label="按监管机构筛选">
          <button
            type="button"
            role="tab"
            aria-selected={activeAgency === 'ALL'}
            className={`project-pill agency-pill ${activeAgency === 'ALL' ? 'active' : ''}`}
            onClick={() => handleSelectAgency('ALL')}
          >
            全部机构 <span>{packages.length}</span>
          </button>
          {agencyGroups.map((agency) => (
            <button
              key={agency.code}
              type="button"
              role="tab"
              aria-selected={activeAgency === agency.code}
              className={`project-pill agency-pill ${activeAgency === agency.code ? 'active' : ''}`}
              title={agency.hint}
              onClick={() => handleSelectAgency(agency.code)}
            >
              {agency.label} <span>{agency.count}</span>
            </button>
          ))}
        </div>
      </div>

      {/* 二级：监管报表 */}
      <div className="project-pills-row project-report-row">
        <span className="project-bar-label">监管报表</span>
        <div className="project-pills" role="tablist" aria-label="按监管报表体系筛选">
          <button
            type="button"
            role="tab"
            aria-selected={activeRegulator === 'ALL'}
            className={`project-pill report-pill ${activeRegulator === 'ALL' ? 'active' : ''}`}
            onClick={() => handleSelectReport('ALL')}
          >
            {activeAgency === 'ALL' ? '全部报表' : `${activeAgencyInfo.shortLabel}全部报表`} <span>{activeAgencyCount}</span>
          </button>
          {reportsInActiveAgency.map((report) => (
            <button
              key={report.code}
              type="button"
              role="tab"
              aria-selected={activeRegulator === report.code}
              className={`project-pill report-pill ${activeRegulator === report.code ? 'active' : ''}`}
              title={report.hint}
              onClick={() => handleSelectReport(report.code)}
            >
              {activeAgency === 'ALL' ? `${agencyMeta(report.agency).shortLabel} · ${report.label}` : report.label}
              <span>{report.count}</span>
            </button>
          ))}
          {activeAgency === 'ALL' && (
            <span className="project-bar-tip">点选上方监管机构可展开对应全部监管报表体系</span>
          )}
        </div>
      </div>

      {/* 三级：当前版本包 */}
      <div className="project-version-row">
        <div className="project-version-controls">
          <span className="project-bar-label">当前版本包</span>
          {packagesInActiveRegulator.length > 0 ? (
            <SelectDropdown
              ariaLabel="选择监管规范版本包"
              value={activePackageId}
              options={packageDropdownOptions}
              disabled={busy}
              minWidth={360}
              className="project-package-dropdown"
              onChange={handleSelectPackage}
            />
          ) : (
            <span className="project-empty-inline">
              「{emptyScopeLabel}」尚无版本包，点击右侧创建首个版本即可上传文档
            </span>
          )}

          {activeReportInfo.paradigm === 'MATRIX' && (
            <span className="project-paradigm-badge" title={activeReportInfo.hint}>
              二维表样矩阵 + 公式体系
            </span>
          )}
          {activePackage && (
            <div className="project-version-stat-pills">
              {activePackage.code_set_count > 0 && (
                <span className="project-stat-chip">码表 <strong>{activePackage.code_set_count}</strong></span>
              )}
              {activePackage.rule_evidence_count > 0 && (
                <span className="project-stat-chip">补充规则 <strong>{activePackage.rule_evidence_count}</strong> 条</span>
              )}
              {activePackage.matrix_report_count > 0 && (
                <span className="project-stat-chip">矩阵表样 <strong>{activePackage.matrix_report_count}</strong> 张</span>
              )}
            </div>
          )}
        </div>

        <div className="project-version-actions">
          <button
            type="button"
            className={`button sm ${packagesInActiveRegulator.length === 0 ? 'primary' : 'secondary'}`}
            disabled={busy}
            onClick={openCreateForm}
          >
            {showCreate ? '取消新建' : packagesInActiveRegulator.length === 0 ? '+ 创建首个版本' : '+ 新建版本包'}
          </button>
          {activePackage && (
            <button
              type="button"
              className="button subtle danger-hover sm"
              disabled={busy}
              onClick={handleDeleteCurrentPackage}
            >
              删除此版本
            </button>
          )}
        </div>
      </div>

      {showCreate && (
        <form className="package-create-inline" onSubmit={handleCreateSubmit}>
          <SelectDropdown
            ariaLabel="所属监管机构"
            size="sm"
            minWidth={195}
            value={newAgency}
            options={createAgencyOptions}
            disabled={busy}
            onChange={handleCreateAgencyChange}
          />
          <SelectDropdown
            ariaLabel="所属监管报表"
            size="sm"
            minWidth={210}
            value={newReport}
            options={createReportOptions}
            disabled={busy}
            onChange={setNewReport}
          />
          {newReport === 'CUSTOM_INPUT' && (
            <input
              type="text"
              placeholder="自定义报表名（如 上海自贸区监测）"
              value={customReport}
              onChange={(e) => setCustomReport(e.target.value)}
              required
            />
          )}
          <input
            type="text"
            placeholder="版本名称（如 一表通2.0试点 / EAST 5.0）"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            required
            style={{ minWidth: 220 }}
          />
          <input
            type="text"
            placeholder="版本号（如 2.0）"
            value={newVersion}
            onChange={(e) => setNewVersion(e.target.value)}
            style={{ width: 112 }}
          />
          {packages.length > 0 && (
            <SelectDropdown
              ariaLabel="继承自旧版本"
              size="sm"
              minWidth={210}
              value={parentId}
              options={parentPackageOptions}
              disabled={busy}
              onChange={setParentId}
            />
          )}
          <button type="submit" className="button primary sm" disabled={busy}>
            {busy ? '创建中...' : '确认创建'}
          </button>
          {err && <span className="danger-text" style={{ fontSize: 12 }}>{err}</span>}
        </form>
      )}
    </section>
  )
}
