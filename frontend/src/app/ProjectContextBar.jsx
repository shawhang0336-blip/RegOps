import { useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useFeedback } from '../context/FeedbackContext.jsx'
import { useWorkspace } from '../context/WorkspaceContext.jsx'
import { REGULATOR_PROJECTS, regulatorMeta } from '../utils/activeDocument.js'

export default function ProjectContextBar() {
  const {
    packages,
    activeRegulator,
    activePackageId,
    activePackage,
    packagesInActiveRegulator,
    regulatorGroups,
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
  const [newReg, setNewReg] = useState(() => (activeRegulator && activeRegulator !== 'ALL' ? activeRegulator : 'SAFE'))
  const [customReg, setCustomReg] = useState('')
  const [newName, setNewName] = useState('')
  const [newVersion, setNewVersion] = useState('')
  const [parentId, setParentId] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  function syncUrlPackage(nextPkgId) {
    if (!['/documents', '/generation', '/runs', '/versions', '/'].includes(location.pathname)) return
    const next = new URLSearchParams(params)
    if (nextPkgId) {
      next.set('package_id', nextPkgId)
      next.delete('document_id')
    } else {
      next.delete('package_id')
    }
    next.delete('table')
    next.delete('set')
    setParams(next, { replace: true })
  }

  function handleSelectRegulator(code) {
    const clean = code === 'ALL' ? 'ALL' : code
    selectRegulator(clean)
    if (clean === 'ALL') return
    const matches = packages.filter((p) => {
      const pr = /^[A-Za-z0-9_-]+$/.test(String(p.regulator || '').trim())
        ? String(p.regulator).trim().toUpperCase()
        : String(p.regulator || '').trim()
      return pr === clean
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

  function openCreateForm() {
    const presetReg = activeRegulator && activeRegulator !== 'ALL' ? activeRegulator : 'SAFE'
    const isBuiltin = REGULATOR_PROJECTS.some((r) => r.code === presetReg)
    if (isBuiltin) {
      setNewReg(presetReg)
      setCustomReg('')
    } else {
      setNewReg('CUSTOM_INPUT')
      setCustomReg(presetReg)
    }
    setParentId(activePackageId || '')
    setErr('')
    setShowCreate((prev) => !prev)
  }

  async function handleCreateSubmit(event) {
    event.preventDefault()
    const targetReg = (newReg === 'CUSTOM_INPUT' ? customReg : newReg).trim()
    if (!targetReg || !newName.trim()) {
      setErr('请填写项目标识与版本名称')
      return
    }
    setBusy(true)
    setErr('')
    try {
      const created = await createProjectPackage({
        name: newName.trim(),
        regulator: targetReg,
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
    const ok = await confirm({
      title: `删除版本规范包「${activePackage.name}」？`,
      message: `所属监管项目：${regulatorMeta(activePackage.regulator).label}（含 ${activePackage.document_count} 份文档、${activePackage.table_count} 张表）`,
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

  const activeMeta = regulatorMeta(activePackage?.regulator || activeRegulator)

  return (
    <section className="project-context-bar" aria-label="监管项目与版本切换栏">
      <div className="project-pills-row">
        <span className="project-bar-label">监管项目</span>
        <div className="project-pills" role="tablist" aria-label="按监管报送体系筛选">
          <button
            type="button"
            role="tab"
            aria-selected={activeRegulator === 'ALL'}
            className={`project-pill ${activeRegulator === 'ALL' ? 'active' : ''}`}
            onClick={() => handleSelectRegulator('ALL')}
          >
            全部项目 <span>{packages.length}</span>
          </button>
          {regulatorGroups.map((group) => (
            <button
              key={group.code}
              type="button"
              role="tab"
              aria-selected={activeRegulator === group.code}
              className={`project-pill ${activeRegulator === group.code ? 'active' : ''}`}
              title={group.hint}
              onClick={() => handleSelectRegulator(group.code)}
            >
              {group.label} <span>{group.count}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="project-version-row">
        <div className="project-version-controls">
          <span className="project-bar-label">当前版本包</span>
          <select
            aria-label="选择监管规范版本包"
            value={activePackageId}
            disabled={busy}
            onChange={(e) => handleSelectPackage(e.target.value)}
          >
            <option value="">单文件兼容模式（不绑定规范包）</option>
            {packagesInActiveRegulator.map((pkg) => (
              <option key={pkg.id} value={pkg.id}>
                [{regulatorMeta(pkg.regulator).shortLabel}] {pkg.name}
                {pkg.spec_version && pkg.spec_version !== 'UNKNOWN' ? ` · v${pkg.spec_version}` : ''}
                {`（${pkg.document_count} 文档 / ${pkg.table_count} 表）`}
              </option>
            ))}
          </select>

          {activeMeta.paradigm === 'MATRIX' && (
            <span className="project-paradigm-badge" title={activeMeta.hint}>
              二维表样矩阵 + 公式体系
            </span>
          )}
          {activePackage && (
            <span className="project-version-stats">
              {activePackage.code_set_count > 0 && `码表 ${activePackage.code_set_count} · `}
              {activePackage.rule_evidence_count > 0 && `补充规则 ${activePackage.rule_evidence_count} 条`}
              {activePackage.matrix_report_count > 0 && ` · 矩阵表样 ${activePackage.matrix_report_count} 张`}
            </span>
          )}
        </div>

        <div className="project-version-actions">
          <button type="button" className="button secondary sm" disabled={busy} onClick={openCreateForm}>
            {showCreate ? '取消新建' : '+ 新建项目 / 版本'}
          </button>
          {activePackage && (
            <button
              type="button"
              className="button subtle sm"
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
          <select value={newReg} onChange={(e) => setNewReg(e.target.value)} aria-label="所属监管项目">
            {REGULATOR_PROJECTS.map((item) => (
              <option key={item.code} value={item.code}>{item.label}</option>
            ))}
            <option value="CUSTOM_INPUT">自定义监管项目（如地方分局/特色报表）</option>
          </select>
          {newReg === 'CUSTOM_INPUT' && (
            <input
              type="text"
              placeholder="自定义项目名（如 上海自贸区监测）"
              value={customReg}
              onChange={(e) => setCustomReg(e.target.value)}
              required
            />
          )}
          <input
            type="text"
            placeholder="版本名称（如 一表通2.0试点 / SAFE 1.4）"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            required
          />
          <input
            type="text"
            placeholder="版本号（如 2.0 / 2025）"
            value={newVersion}
            onChange={(e) => setNewVersion(e.target.value)}
            style={{ width: 120 }}
          />
          {packages.length > 0 && (
            <select value={parentId} onChange={(e) => setParentId(e.target.value)} aria-label="继承自旧版本">
              <option value="">全新版本（无前置基线）</option>
              {packages.map((p) => (
                <option key={p.id} value={p.id}>
                  升级自：[{regulatorMeta(p.regulator).shortLabel}] {p.name}
                </option>
              ))}
            </select>
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
