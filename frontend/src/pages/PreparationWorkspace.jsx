import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import DocumentsPage from './DocumentsPage.jsx'
import MetadataPage from './MetadataPage.jsx'
import CodeSetsPage from './CodeSetsPage.jsx'

const VIEWS = [
  ['fields', '报表字段'],
  ['codes', '码表'],
]

export default function PreparationWorkspace() {
  const [params, setParams] = useSearchParams()
  const view = VIEWS.some(([key]) => key === params.get('view')) ? params.get('view') : 'fields'
  const documentId = params.get('document_id') || ''

  useEffect(() => {
    v2('/documents').then((list) => {
      if (!documentId && list[0]) changeDocument(list[0].id)
    }).catch(() => {})
  }, [documentId])

  function changeDocument(nextDocumentId) {
    const next = new URLSearchParams(params)
    next.set('document_id', nextDocumentId)
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
    } else if (nextView === 'codes') {
      if (assetId) next.set('set', assetId)
      next.delete('table')
    }
    setParams(next)
  }

  return (
    <>
      <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="规范解析与核对"
        description="上传文件后自动解析，在同一页面核对报表字段和码表。"
      />
      <DocumentsPage embedded importOnly autoParse onShowAssets={changeView} onSelectDocument={changeDocument} />
      <nav className="asset-workspace-tabs" aria-label="导入与核对视图">
        {VIEWS.map(([key, label]) => (
          <button type="button" key={key} className={view === key ? 'active' : ''} onClick={() => changeView(key)}>
            {label}
          </button>
        ))}
      </nav>
      {view === 'fields' && <MetadataPage embedded />}
      {view === 'codes' && <CodeSetsPage embedded onDocumentChange={changeDocument} />}
    </>
  )
}
