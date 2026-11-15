import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import DocumentsPage from './DocumentsPage.jsx'
import MetadataPage from './MetadataPage.jsx'
import CodeSetsPage from './CodeSetsPage.jsx'

const DEFAULT_VIEW = 'fields'

export default function PreparationWorkspace() {
  const [params, setParams] = useSearchParams()
  // 只留报表字段一个主视图。码表不再作为一个并列的入口——它由阶段 2 自动生成、
  // 发布时自动发出，不需要人在这里逐张核对。保留 view=codes 是为了：
  // ① 解析结果里只有码表没有报表时自动跳过去；② 元数据页遇到码值缺陷时能过去补。
  const view = params.get('view') === 'codes' ? 'codes' : DEFAULT_VIEW
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
        description="上传文件后自动解析，在这里核对报表字段。码表由下一步自动生成并发布。"
      />
      <DocumentsPage embedded importOnly autoParse onShowAssets={changeView} onSelectDocument={changeDocument} />
      {view === 'codes' ? (
        <>
          <div className="asset-scope-bar">
            <span>
              正在查看码表（由阶段 2 自动生成，发布表时自动发出）
            </span>
            <button type="button" className="button secondary sm" onClick={() => changeView('fields')}>
              ← 返回报表字段
            </button>
          </div>
          <CodeSetsPage embedded onDocumentChange={changeDocument} />
        </>
      ) : (
        <MetadataPage embedded onOpenCodeSets={() => changeView('codes')} />
      )}
    </>
  )
}
