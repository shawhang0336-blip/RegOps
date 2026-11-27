import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  createPackage,
  extractPackage,
  getMetadataReadiness,
  getPackage,
  listDocuments,
  listPackages,
  updatePackageFileRole,
  uploadPackageFiles,
} from '../../api/catalog.js'
import PageHeader from '../../components/PageHeader.jsx'
import StatusMessage from '../../components/StatusMessage.jsx'
import { getSavedDocumentId, setSavedDocumentId } from '../../utils/activeDocument.js'
import CodeSetsPage from './codeSets/CodeSetsPage.jsx'
import DocumentsPage from './documents/DocumentsPage.jsx'
import MetadataPage from './metadata/MetadataPage.jsx'

const REGULATOR_OPTIONS = [
  { value: 'SAFE', label: 'SAFE（外汇局）' },
  { value: 'YBT', label: '一表通 2.0' },
  { value: 'EAST', label: 'EAST 5.0' },
  { value: '1104', label: '1104 非现场' },
  { value: 'PBOC', label: '人行征信/反洗钱' },
  { value: 'CUSTOM', label: '其他监管规范' },
]

const DOC_ROLE_OPTIONS = [
  { value: 'AUTO', label: '自动识别' },
  { value: 'MAIN_SPEC', label: '主规范文档' },
  { value: 'SCHEMA_APPENDIX', label: '字段结构附表' },
  { value: 'CODE_SET_APPENDIX', label: '公共代码表' },
  { value: 'RULE_SUPPLEMENT', label: '独立核查规则' },
  { value: 'REVISION_NOTE', label: '修订说明对照' },
]

export default function PreparationWorkspace() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const view = ['fields', 'codes'].includes(params.get('view')) ? params.get('view') : 'fields'
  const documentId = params.get('document_id') || ''
  const packageId = params.get('package_id') || ''
  const [readiness, setReadiness] = useState(null)
  const [codeSummary, setCodeSummary] = useState(null)
  const [reportFilter, setReportFilter] = useState('all')
  const [codeFilter, setCodeFilter] = useState('all')

  const [packages, setPackages] = useState([])
  const [pkgDetail, setPkgDetail] = useState(null)
  const [showNewPkg, setShowNewPkg] = useState(false)
  const [newPkgName, setNewPkgName] = useState('')
  const [newPkgReg, setNewPkgReg] = useState('SAFE')
  const [pkgBusy, setPkgBusy] = useState(false)
  const [pkgMsg, setPkgMsg] = useState('')
  const [pkgErr, setPkgErr] = useState('')

  useEffect(() => {
    listPackages().then(setPackages).catch(() => {})
  }, [])

  useEffect(() => {
    if (!packageId) {
      setPkgDetail(null)
      return undefined
    }
    let cancelled = false
    getPackage(packageId)
      .then((d) => { if (!cancelled) setPkgDetail(d) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [packageId])

  useEffect(() => {
    let cancelled = false
    listDocuments().then((list) => {
      if (cancelled || documentId || packageId) return
      const saved = getSavedDocumentId()
      const preferred = (saved && list.find((doc) => doc.id === saved)?.id) || list[0]?.id
      if (preferred) changeDocument(preferred)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [documentId, packageId])

  useEffect(() => {
    if (documentId) setSavedDocumentId(documentId)
    setReadiness(null)
    setCodeSummary(null)
  }, [documentId, packageId])

  useEffect(() => {
    if ((!documentId && !packageId) || view !== 'codes' || readiness) return undefined
    let cancelled = false
    getMetadataReadiness(packageId ? { packageId } : { documentId }).then((payload) => {
      if (!cancelled) setReadiness(payload)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [documentId, packageId, view, readiness])

  function changeDocument(nextDocumentId) {
    setSavedDocumentId(nextDocumentId || '')
    const next = new URLSearchParams(params)
    next.delete('package_id')
    if (nextDocumentId) next.set('document_id', nextDocumentId)
    else next.delete('document_id')
    next.delete('table')
    next.delete('set')
    setParams(next)
  }

  function selectPackage(nextPkgId) {
    const next = new URLSearchParams(params)
    if (nextPkgId) {
      next.set('package_id', nextPkgId)
      next.delete('document_id')
    } else {
      next.delete('package_id')
    }
    next.delete('table')
    next.delete('set')
    setParams(next)
  }

  function changeView(nextView, assetId = '') {
    const next = new URLSearchParams(params)
    next.set('view', nextView)
    if (nextView === 'fields') {
      if (assetId) next.set('table', assetId)
      next.delete('set')
    } else {
      if (assetId) next.set('set', assetId)
      next.delete('table')
    }
    setParams(next)
  }

  function openReports(filter = 'all') {
    setReportFilter(filter)
    changeView('fields')
  }

  function openCodes(filter = 'all') {
    setCodeFilter(filter)
    changeView('codes')
  }

  async function handleCreatePackage(event) {
    event.preventDefault()
    if (!newPkgName.trim()) return
    setPkgBusy(true)
    setPkgErr('')
    try {
      const created = await createPackage({ name: newPkgName.trim(), regulator: newPkgReg })
      setPackages((prev) => [created, ...prev])
      setNewPkgName('')
      setShowNewPkg(false)
      selectPackage(created.id)
      setPkgMsg(`已创建规范包「${created.name}」，可一次批量上传多份 Word / Excel / PDF / ZIP。`)
    } catch (err) {
      setPkgErr(err.message || '创建规范包失败')
    } finally {
      setPkgBusy(false)
    }
  }

  async function handleUploadPackageFiles(event) {
    const fileList = Array.from(event.target.files || [])
    event.target.value = ''
    if (!fileList.length || !packageId) return
    setPkgBusy(true)
    setPkgErr('')
    setPkgMsg('')
    try {
      const res = await uploadPackageFiles(packageId, fileList)
      const detail = await getPackage(packageId)
      setPkgDetail(detail)
      setPackages(await listPackages())
      setPkgMsg(`已导入 ${res.uploaded?.length || fileList.length} 份文件，点击“整包提取并融合”即可合并跨文件字段与核查规则。`)
    } catch (err) {
      setPkgErr(err.message || '批量导入文件失败')
    } finally {
      setPkgBusy(false)
    }
  }

  async function handleRoleChange(docId, role) {
    if (!packageId) return
    setPkgBusy(true)
    try {
      await updatePackageFileRole(packageId, docId, role)
      setPkgDetail(await getPackage(packageId))
    } catch (err) {
      setPkgErr(err.message || '更新文档角色失败')
    } finally {
      setPkgBusy(false)
    }
  }

  async function handleExtractPackage() {
    if (!packageId) return
    setPkgBusy(true)
    setPkgErr('')
    setPkgMsg('')
    try {
      const res = await extractPackage(packageId)
      setPkgDetail(await getPackage(packageId))
      setPackages(await listPackages())
      const rec = res.reconcile || {}
      setPkgMsg(
        `整包解析完成：融合 ${rec.merged_groups || 0} 组多文件同表、关联 ${rec.linked_references || 0} 处码表引用、沉淀 ${rec.rule_evidence_count || 0} 条补充核查规则${rec.conflict_count ? `（发现 ${rec.conflict_count} 处跨文件冲突待确认）` : ''}。`,
      )
      setReadiness(null)
      setCodeSummary(null)
      const next = new URLSearchParams(params)
      next.set('package_id', packageId)
      next.delete('document_id')
      setParams(next)
    } catch (err) {
      setPkgErr(err.message || '整包解析失败')
    } finally {
      setPkgBusy(false)
    }
  }

  const summary = readiness?.summary
  const tableCount = summary?.table_count || 0
  const readyCount = summary?.ready_all || 0
  const blockedCount = Math.max(0, tableCount - readyCount)
  const tableCodeDefects = (readiness?.tables || []).filter(
    (item) => item.code_sets?.applicable && (item.code_sets.defects || []).length > 0
  ).length
  const codeBlockedCount = codeSummary?.blocked ?? tableCodeDefects

  return (
    <>
      <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="规范解析与核对"
        description="支持按多文件规范包（Word + Excel + PDF + ZIP 混装融合）或单文件导入；只需处理有问题的报表和码表，已就绪内容可直接进入第二步生成。"
      />

      <section className="current-file-bar compact" aria-label="多文件规范包工作区">
        <div className="current-file-main">
          <span className="current-file-eyebrow">规范包模式（多文件混装融合）</span>
          <div className="current-file-heading">
            <select
              aria-label="选择规范包"
              value={packageId}
              disabled={pkgBusy}
              onChange={(e) => selectPackage(e.target.value)}
            >
              <option value="">单文件模式（不使用规范包）</option>
              {packages.map((pkg) => (
                <option key={pkg.id} value={pkg.id}>
                  [{pkg.regulator}] {pkg.name}（{pkg.document_count} 文件 / {pkg.table_count} 表）
                </option>
              ))}
            </select>
            <button type="button" className="button secondary sm" disabled={pkgBusy} onClick={() => setShowNewPkg((v) => !v)}>
              {showNewPkg ? '取消新建' : '+ 新建规范包'}
            </button>
            {packageId && (
              <>
                <label className="button secondary sm" style={{ cursor: 'pointer' }}>
                  批量上传多文件 / ZIP
                  <input
                    type="file"
                    multiple
                    accept=".pdf,.docx,.xlsx,.zip,.doc,.xls"
                    disabled={pkgBusy}
                    style={{ display: 'none' }}
                    onChange={handleUploadPackageFiles}
                  />
                </label>
                <button
                  type="button"
                  className="button primary sm"
                  disabled={pkgBusy || !pkgDetail?.documents?.length}
                  onClick={handleExtractPackage}
                >
                  {pkgBusy ? '处理中...' : '整包提取并跨文件融合'}
                </button>
              </>
            )}
          </div>
          {showNewPkg && (
            <form onSubmit={handleCreatePackage} style={{ display: 'flex', gap: '8px', marginTop: '8px', flexWrap: 'wrap' }}>
              <input
                type="text"
                placeholder="规范包名称（如 SAFE_2025 / 一表通2.0）"
                value={newPkgName}
                onChange={(e) => setNewPkgName(e.target.value)}
                required
              />
              <select value={newPkgReg} onChange={(e) => setNewPkgReg(e.target.value)}>
                {REGULATOR_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
              <button type="submit" className="button primary sm" disabled={pkgBusy}>确认创建</button>
            </form>
          )}
          {pkgDetail && pkgDetail.documents?.length > 0 && (
            <div style={{ marginTop: '8px', display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {pkgDetail.documents.map((doc) => (
                <div key={doc.id} style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '4px 8px', border: '1px solid var(--border-subtle, #ddd)', borderRadius: '6px', fontSize: '12px' }}>
                  <span>{doc.file_name}</span>
                  <select
                    aria-label={`文件角色-${doc.file_name}`}
                    value={doc.doc_role || 'AUTO'}
                    disabled={pkgBusy}
                    onChange={(e) => handleRoleChange(doc.id, e.target.value)}
                  >
                    {DOC_ROLE_OPTIONS.map((r) => (
                      <option key={r.value} value={r.value}>{r.label}</option>
                    ))}
                  </select>
                </div>
              ))}
              {pkgDetail.rule_evidence_count > 0 && (
                <span className="current-file-meta">已提取补充核查规则：{pkgDetail.rule_evidence_count} 条</span>
              )}
              {pkgDetail.matrix_report_count > 0 && (
                <span className="current-file-meta">已识别 1104 指标矩阵表：{pkgDetail.matrix_report_count} 个</span>
              )}
            </div>
          )}
          <StatusMessage error={pkgErr} notice={pkgMsg} onClose={() => { setPkgErr(''); setPkgMsg('') }} />
        </div>
      </section>

      {!packageId && (
        <DocumentsPage
          embedded
          importOnly
          autoParse
          selectedDocumentId={documentId}
          onShowAssets={changeView}
          onSelectDocument={changeDocument}
        />
      )}

      {(documentId || packageId) && (
        <section className="preparation-overview" aria-label="第一步核对进度">
          <div className="preparation-metrics">
            <button type="button" className={reportFilter === 'all' && view === 'fields' ? 'active' : ''} onClick={() => openReports('all')}>
              <span>报表总数</span><strong>{summary ? tableCount : '—'}</strong>
            </button>
            <button type="button" className={reportFilter === 'ready' && view === 'fields' ? 'active' : ''} onClick={() => openReports('ready')}>
              <span>可生成</span><strong className="success-text">{summary ? readyCount : '—'}</strong>
            </button>
            <button type="button" className={reportFilter === 'blocked' && view === 'fields' ? 'active' : ''} onClick={() => openReports('blocked')}>
              <span>需处理</span><strong className={blockedCount ? 'danger-text' : 'success-text'}>{summary ? blockedCount : '—'}</strong>
            </button>
            <button type="button" className={codeFilter === 'blocked' && view === 'codes' ? 'active' : ''} onClick={() => openCodes('blocked')}>
              <span>码表需补</span><strong className={codeBlockedCount ? 'danger-text' : 'success-text'}>{summary || codeSummary ? codeBlockedCount : '—'}</strong>
            </button>
          </div>
          <div className="preparation-overview-footer">
            <p>
              {summary
                ? blockedCount > 0
                  ? `还有 ${blockedCount} 张报表暂不可生成，但可以继续使用已就绪的 ${readyCount} 张。`
                  : `全部 ${readyCount} 张报表都已就绪，可以进入第二步选表生成。`
                : '正在评估报表和码表，请稍候；也可以先查看已识别的内容。'}
            </p>
            <button
              type="button"
              className="button primary preparation-next"
              onClick={() => navigate(documentId ? `/generation?document_id=${encodeURIComponent(documentId)}` : '/generation')}
            >
              进入第二步
            </button>
          </div>
        </section>
      )}

      <nav className="asset-workspace-tabs" aria-label="导入与核对视图">
        <button type="button" className={view === 'fields' ? 'active' : ''} onClick={() => openReports(reportFilter)}>
          报表与字段 {summary && <span>{tableCount}</span>}
        </button>
        <button type="button" className={view === 'codes' ? 'active' : ''} onClick={() => openCodes(codeFilter)}>
          码值表 {codeSummary && <span>{codeSummary.total}</span>}
          {!codeSummary && codeBlockedCount > 0 && <span>{codeBlockedCount} 需补</span>}
        </button>
      </nav>
      <div className="tab-panel-stage" key={`${view}-${packageId || documentId}`}>
        {view === 'fields' && (
          <MetadataPage
            embedded
            packageId={packageId}
            onOpenCodeSets={() => openCodes('blocked')}
            onReadinessChange={setReadiness}
            reportFilter={reportFilter}
            onReportFilterChange={setReportFilter}
          />
        )}
        {view === 'codes' && (
          <CodeSetsPage
            embedded
            packageId={packageId}
            onDocumentChange={changeDocument}
            onSummaryChange={setCodeSummary}
            statusFilter={codeFilter}
            onStatusFilterChange={setCodeFilter}
          />
        )}
      </div>
    </>
  )
}
