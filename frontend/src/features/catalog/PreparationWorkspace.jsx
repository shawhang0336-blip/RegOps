import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  extractPackage,
  getMetadataReadiness,
  getPackage,
  listDocuments,
  removePackageFile,
  replacePackageFile,
  updatePackageFileRole,
  uploadPackageFiles,
} from '../../api/catalog.js'
import PageHeader from '../../components/PageHeader.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import StatusMessage from '../../components/StatusMessage.jsx'
import { useFeedback } from '../../context/FeedbackContext.jsx'
import { useWorkspace } from '../../context/WorkspaceContext.jsx'
import { getSavedDocumentId, regulatorMeta, setSavedDocumentId } from '../../utils/activeDocument.js'
import CodeSetsPage from './codeSets/CodeSetsPage.jsx'
import DocumentsPage from './documents/DocumentsPage.jsx'
import MetadataPage from './metadata/MetadataPage.jsx'

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
  const { confirm } = useFeedback()
  const { activePackageId, activePackage, activeRegulator, selectPackage, refreshPackages } = useWorkspace()

  const view = ['fields', 'codes'].includes(params.get('view')) ? params.get('view') : 'fields'
  const urlDocumentId = params.get('document_id') || ''
  const urlPackageId = params.get('package_id') || ''
  const packageId = urlPackageId || (!urlDocumentId ? activePackageId : '')
  const documentId = packageId ? '' : urlDocumentId

  const [readiness, setReadiness] = useState(null)
  const [codeSummary, setCodeSummary] = useState(null)
  const [reportFilter, setReportFilter] = useState('all')
  const [codeFilter, setCodeFilter] = useState('all')

  const [pkgDetail, setPkgDetail] = useState(null)
  const [pkgBusy, setPkgBusy] = useState(false)
  const [pkgMsg, setPkgMsg] = useState('')
  const [pkgErr, setPkgErr] = useState('')

  // 同步 URL 与全局 Workspace 选中规范包
  useEffect(() => {
    if (urlPackageId && urlPackageId !== activePackageId) {
      selectPackage(urlPackageId)
    }
  }, [urlPackageId, activePackageId, selectPackage])

  useEffect(() => {
    if (!packageId) {
      setPkgDetail(null)
      return undefined
    }
    let cancelled = false
    getPackage(packageId)
      .then((detail) => {
        if (!cancelled) setPkgDetail(detail)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [packageId])

  useEffect(() => {
    let cancelled = false
    listDocuments()
      .then((list) => {
        if (cancelled || documentId || packageId) return
        const saved = getSavedDocumentId()
        const preferred = (saved && list.find((doc) => doc.id === saved)?.id) || list[0]?.id
        if (preferred) changeDocument(preferred)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [documentId, packageId])

  useEffect(() => {
    if (documentId) setSavedDocumentId(documentId)
    setReadiness(null)
    setCodeSummary(null)
  }, [documentId, packageId])

  useEffect(() => {
    if ((!documentId && !packageId) || view !== 'codes' || readiness) return undefined
    let cancelled = false
    getMetadataReadiness(packageId ? { packageId } : { documentId })
      .then((payload) => {
        if (!cancelled) setReadiness(payload)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
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

  async function handleUploadPackageFiles(event) {
    const fileList = Array.from(event.target.files || [])
    event.target.value = ''
    if (!fileList.length || !packageId) return
    setPkgBusy(true)
    setPkgErr('')
    setPkgMsg('')
    try {
      const res = await uploadPackageFiles(packageId, fileList)
      setPkgDetail(await getPackage(packageId))
      await refreshPackages()
      setPkgMsg(`已导入 ${res.uploaded?.length || fileList.length} 份文件，点击“整包提取并跨文件融合”即可合并跨文件字段与核查规则。`)
    } catch (err) {
      setPkgErr(err.message || '批量导入文件失败')
    } finally {
      setPkgBusy(false)
    }
  }

  async function handleReplaceFile(doc, event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !packageId) return
    setPkgBusy(true)
    setPkgErr('')
    setPkgMsg('')
    try {
      const res = await replacePackageFile(packageId, doc.id, file)
      setPkgDetail(await getPackage(packageId))
      await refreshPackages()
      setPkgMsg(`已用「${res.document?.file_name || file.name}」替换更新原文件「${doc.file_name}」，点击“整包提取并跨文件融合”以刷新最新字段与规则。`)
      setReadiness(null)
    } catch (err) {
      setPkgErr(err.message || '替换更新文件失败')
    } finally {
      setPkgBusy(false)
    }
  }

  async function handleRemoveFile(doc) {
    if (!packageId) return
    const ok = await confirm({
      title: `从当前版本包移除「${doc.file_name}」？`,
      message: '将移除该文档关联；若需重新融合同表字段，请随后点击“整包提取并跨文件融合”。',
      confirmLabel: '确认移除',
      tone: 'danger',
    })
    if (!ok) return
    setPkgBusy(true)
    setPkgErr('')
    setPkgMsg('')
    try {
      await removePackageFile(packageId, doc.id)
      setPkgDetail(await getPackage(packageId))
      await refreshPackages()
      setPkgMsg(`已移除文档「${doc.file_name}」。`)
      setReadiness(null)
    } catch (err) {
      setPkgErr(err.message || '移除文档失败')
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
      await refreshPackages()
      const rec = res.reconcile || {}
      setPkgMsg(
        `整包解析完成：融合 ${rec.merged_groups || 0} 组多文件同表、关联 ${rec.linked_references || 0} 处码表引用、沉淀 ${rec.rule_evidence_count || 0} 条补充核查规则${rec.conflict_count ? `（发现 ${rec.conflict_count} 处跨文件冲突待确认）` : ''}。`,
      )
      setReadiness(null)
      setCodeSummary(null)
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
    (item) => item.code_sets?.applicable && (item.code_sets.defects || []).length > 0,
  ).length
  const codeBlockedCount = codeSummary?.blocked ?? tableCodeDefects

  const activePkgMeta = pkgDetail || activePackage
  const regInfo = regulatorMeta(activePkgMeta?.regulator || activeRegulator)

  const step2Url = packageId
    ? `/generation?package_id=${encodeURIComponent(packageId)}`
    : documentId
      ? `/generation?document_id=${encodeURIComponent(documentId)}`
      : '/generation'

  return (
    <>
      <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="规范解析与核对"
        description="一个监管报表体系即一个独立项目，每个版本包支持多份 Word / Excel / PDF / ZIP 混装上传、局部单文件替换更新与跨文件融合。"
      />

      {packageId ? (
        <section className="current-file-bar compact" aria-label="当前规范包文档清单与更新">
          <div className="current-file-main">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <span className="current-file-eyebrow">
                  {regInfo.label} · {activePkgMeta?.spec_version && activePkgMeta.spec_version !== 'UNKNOWN' ? `版本 v${activePkgMeta.spec_version}` : '当前版本包'}
                </span>
                <div className="current-file-heading" style={{ marginTop: 4 }}>
                  <strong>{activePkgMeta?.name || '当前规范包'}</strong>
                  {pkgDetail?.rule_evidence_count > 0 && (
                    <span className="current-file-meta">已提取补充核查规则：{pkgDetail.rule_evidence_count} 条</span>
                  )}
                  {pkgDetail?.matrix_report_count > 0 && (
                    <span className="current-file-meta">1104 指标矩阵：{pkgDetail.matrix_report_count} 张</span>
                  )}
                </div>
              </div>

              <div className="actions">
                <label className="button secondary sm" style={{ cursor: 'pointer' }}>
                  + 追加文件 / ZIP
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
              </div>
            </div>

            {pkgDetail?.documents?.length > 0 ? (
              <div className="package-doc-list">
                {pkgDetail.documents.map((doc) => (
                  <div key={doc.id} className="package-doc-row">
                    <div className="package-doc-info">
                      <strong>{doc.file_name}</strong>
                      <StatusBadge status={doc.status} />
                      <span className="muted-text" style={{ fontSize: 12 }}>
                        {doc.block_count || 0} 块
                      </span>
                    </div>
                    <div className="package-doc-actions">
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
                      <label className="button secondary sm" style={{ cursor: 'pointer' }} title="仅替换这一份文件并沿用当前角色">
                        替换更新
                        <input
                          type="file"
                          accept=".pdf,.docx,.xlsx,.doc,.xls"
                          disabled={pkgBusy}
                          style={{ display: 'none' }}
                          onChange={(e) => handleReplaceFile(doc, e)}
                        />
                      </label>
                      <button
                        type="button"
                        className="button subtle sm"
                        disabled={pkgBusy}
                        onClick={() => handleRemoveFile(doc)}
                      >
                        移除
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted-text" style={{ marginTop: 8 }}>
                当前版本包尚无文档，请点击右上角「+ 追加文件 / ZIP」一次上传该监管报表的规范正文、数据字典、代码表或核查规则附件。
              </p>
            )}
            <StatusMessage error={pkgErr} notice={pkgMsg} onClose={() => { setPkgErr(''); setPkgMsg('') }} />
          </div>
        </section>
      ) : (
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
              onClick={() => navigate(step2Url)}
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
