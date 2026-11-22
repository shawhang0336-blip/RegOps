import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { getSavedDocumentId, setSavedDocumentId } from '../utils/activeDocument.js'
import PageHeader from './PageHeader.jsx'
import DocumentsPage from './DocumentsPage.jsx'
import MetadataPage from './MetadataPage.jsx'
import CodeSetsPage from './CodeSetsPage.jsx'

export default function PreparationWorkspace() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const view = ['fields', 'codes'].includes(params.get('view')) ? params.get('view') : 'fields'
  const documentId = params.get('document_id') || ''
  const [readiness, setReadiness] = useState(null)
  const [codeSummary, setCodeSummary] = useState(null)
  const [reportFilter, setReportFilter] = useState('all')
  const [codeFilter, setCodeFilter] = useState('all')

  useEffect(() => {
    let cancelled = false
    api('/api/documents').then((list) => {
      if (cancelled || documentId) return
      const saved = getSavedDocumentId()
      const preferred = (saved && list.find((doc) => doc.id === saved)?.id) || list[0]?.id
      if (preferred) changeDocument(preferred)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [documentId])

  useEffect(() => {
    if (documentId) setSavedDocumentId(documentId)
    setReadiness(null)
    setCodeSummary(null)
  }, [documentId])

  useEffect(() => {
    if (!documentId || view !== 'codes' || readiness) return undefined
    let cancelled = false
    const query = `?document_id=${encodeURIComponent(documentId)}`
    api(`/api/metadata/readiness${query}`).then((payload) => {
      if (!cancelled) setReadiness(payload)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [documentId, view, readiness])

  function changeDocument(nextDocumentId) {
    setSavedDocumentId(nextDocumentId || '')
    const next = new URLSearchParams(params)
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
        description="导入文件后，只需处理有问题的报表和码表；已就绪的内容可以直接进入第二步生成。"
      />
      <DocumentsPage
        embedded
        importOnly
        autoParse
        selectedDocumentId={documentId}
        onShowAssets={changeView}
        onSelectDocument={changeDocument}
      />

      {documentId && (
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
      <div className="tab-panel-stage" key={view}>
        {view === 'fields' && (
          <MetadataPage
            embedded
            onOpenCodeSets={() => openCodes('blocked')}
            onReadinessChange={setReadiness}
            reportFilter={reportFilter}
            onReportFilterChange={setReportFilter}
          />
        )}
        {view === 'codes' && (
          <CodeSetsPage
            embedded
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
