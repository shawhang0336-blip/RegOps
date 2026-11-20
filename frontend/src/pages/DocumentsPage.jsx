import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { v2 } from '../api.js'
import { useEventStream, watchDocumentProgress } from '../hooks/useEventStream.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { SourceLocatorTag, StatusBadge, formatTime, friendlyError } from './SharedUI.jsx'
import CurrentFilePicker from './documents/CurrentFilePicker.jsx'
import ImportProgressPanel from './documents/ImportProgressPanel.jsx'
import {
  COLUMN_MAPPING_FIELDS,
  fileSha256,
  updateImportProgress,
} from './documents/documentHelpers.js'

export default function DocumentsPage({ embedded = false, importOnly = false, autoParse = false, onShowAssets, onSelectDocument }) {
  const navigate = useNavigate()
  const [documents, setDocuments] = useState([])
  const [detail, setDetail] = useState(null)
  const [selected, setSelected] = useState([])
  const [raw, setRaw] = useState(null)
  const [architecture, setArchitecture] = useState(null)
  const [architectureExpanded, setArchitectureExpanded] = useState(false)
  const [mappingBlock, setMappingBlock] = useState(null)
  const [mappingForm, setMappingForm] = useState({})
  const [mappingOptions, setMappingOptions] = useState([])
  const [blockFilter, setBlockFilter] = useState('all')
  const [dragging, setDragging] = useState(false)
  const [showUpload, setShowUpload] = useState(false)
  const [importProgress, setImportProgress] = useState(() => {
    try {
      return JSON.parse(window.localStorage.getItem('regops-import-progress') || 'null')
    } catch {
      return null
    }
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const list = await v2('/documents')
    setDocuments(list)
    const extracting = list.find((doc) => doc.status === 'EXTRACTING')
    if (extracting) {
      setImportProgress((current) => current || { fileName: extracting.file_name, digest: extracting.sha256, stage: 3, status: 'WORKING', awaitReextract: false, autoParse: true })
    }
    const currentId = detail?.id
    const targetId = preferredId || (list.some((doc) => doc.id === currentId) ? currentId : null) || list[0]?.id
    if (targetId) {
      await selectDocument(targetId)
    } else {
      setDetail(null)
      setSelected([])
      setRaw(null)
      setArchitecture(null)
      setMappingBlock(null)
    }
  }

  useEffect(() => {
    load().catch((err) => setError(friendlyError(err.message)))
  }, [])

  useEffect(() => {
    if (importProgress) window.localStorage.setItem('regops-import-progress', JSON.stringify(importProgress))
    else window.localStorage.removeItem('regops-import-progress')
  }, [importProgress])

  const syncDigestProgress = async () => {
    if (!importProgress?.digest) return
    try {
      const list = await v2('/documents')
      const document = list.find((item) => item.sha256 === importProgress.digest)
      if (document) setImportProgress((current) => updateImportProgress(current, document))
    } catch {
    }
  }

  useEventStream({
    enabled: importProgress?.status === 'WORKING' && Boolean(importProgress.digest),
    topics: 'document.progress',
    fallbackIntervalMs: 1200,
    onEvent: syncDigestProgress,
    onFallbackTick: syncDigestProgress,
  })

  async function selectDocument(id) {
    const doc = await v2(`/documents/${id}`)
    setDetail(doc)
    setShowUpload(false)
    if (onSelectDocument) onSelectDocument(doc.id)
    const eligibleIds = (doc.blocks || []).filter((b) => b.eligible !== false && !b.superseded).map((b) => b.id)
    setSelected(eligibleIds)
    setRaw(null)
    setArchitectureExpanded(false)
    setArchitecture(await v2(`/documents/${id}/architecture`).catch(() => null))
  }

  async function extractPreparedDocument(documentId) {
    const prepared = await v2(`/documents/${documentId}`)
    const blockIds = (prepared.blocks || []).filter((block) => block.eligible !== false && !block.superseded).map((block) => block.id)
    if (!blockIds.length) throw new Error('文件中没有可解析的内容')
    await v2(`/documents/${documentId}/extract-parallel`, {
      method: 'POST',
      body: JSON.stringify({ block_ids: blockIds, targets: ['metadata', 'code_sets'] }),
    })
    // 接口是 202：真正的结果要轮询进度拿到
    return watchDocumentProgress(documentId, setImportProgress)
  }

  async function parseCurrentDocument() {
    if (!detail) return
    setBusy(true)
    setError('')
    setImportProgress({ fileName: detail.file_name, stage: 4, status: 'WORKING', detail: '正在准备解析', counters: {}, logs: [] })
    try {
      const result = await extractPreparedDocument(detail.id)
      const tables = result?.counters?.tables || 0
      const codeSets = result?.counters?.code_sets || 0
      await load(detail.id)
      if (result?.status === 'FAILED') {
        setError('解析未完成，请查看进度记录后重试。')
        return
      }
      setNotice(`解析完成：生成 ${tables} 张报表、${codeSets} 张码表。`)
      if (onShowAssets) onShowAssets(tables ? 'fields' : 'codes', '')
    } catch (err) {
      setImportProgress((current) => current ? { ...current, status: 'FAILED', detail: friendlyError(err.message) } : null)
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function handleFileUpload(file) {
    if (!file) return
    setBusy(true)
    setError('')
    setImportProgress({ fileName: file.name, stage: 1, status: 'WORKING', detail: '正在计算文件指纹', counters: {}, logs: [] })
    try {
      const digest = await fileSha256(file)
      setImportProgress((current) => current ? { ...current, detail: '正在上传文件' } : current)
      const form = new FormData()
      form.append('file', file)
      const document = await v2('/documents', { method: 'POST', body: form })

      let snapshot = null
      if (document.processing) {
        snapshot = await watchDocumentProgress(document.id, setImportProgress)
      } else {
        snapshot = await v2(`/documents/${document.id}/progress`).catch(() => null)
        setImportProgress(snapshot && snapshot.status !== 'IDLE' ? snapshot : null)
      }

      if (snapshot?.status === 'FAILED') {
        await load(document.id)
        setError('结构检查未完成，请根据提示处理后重试。')
        return
      }

      if (autoParse) {
        const result = await extractPreparedDocument(document.id)
        const tables = result?.counters?.tables || 0
        const codeSets = result?.counters?.code_sets || 0
        const failed = result?.status === 'FAILED'
        await load(document.id)
        if (failed) {
          setError('解析未完成，请查看进度记录后重试。')
          return
        }
        setNotice(`解析完成：生成 ${tables} 张报表、${codeSets} 张码表，可在下方直接核对。`)
        if (onShowAssets) onShowAssets(tables ? 'fields' : 'codes', '')
      } else {
        await load(document.id)
        setNotice(
          document.status === 'OVERVIEW_FAILED'
            ? `已保存「${document.file_name}」，但结构检查未完成，请点击“重新检查并解析”。`
            : `已完成「${document.file_name}」的结构检查和初步切块，请点击“开始解析”。`
        )
      }
    } catch (err) {
      setImportProgress((current) => current ? { ...current, status: 'FAILED', detail: friendlyError(err.message) } : null)
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
      setShowUpload(false)
    }
  }

  function onFileInputChange(event) {
    const file = event.target.files?.[0]
    if (file) handleFileUpload(file)
    event.target.value = ''
  }

  function onDrop(event) {
    event.preventDefault()
    setDragging(false)
    if (busy) return
    const file = event.dataTransfer.files?.[0]
    if (file) handleFileUpload(file)
  }

  async function prepareDocument() {
    if (!detail) return
    setBusy(true)
    setError('')
    setImportProgress({ fileName: detail.file_name, stage: 1, status: 'WORKING', detail: '正在重新检查文档结构', counters: {}, logs: [] })
    try {
      await v2(`/documents/${detail.id}/prepare`, { method: 'POST', body: JSON.stringify({}) })
      const snapshot = await watchDocumentProgress(detail.id, setImportProgress)
      await load(detail.id)
      if (snapshot?.status === 'FAILED') {
        setError('文件结构检查仍未完成，请查看提示后重试。')
        return
      }
      if (autoParse) {
        const extraction = await extractPreparedDocument(detail.id)
        const tables = extraction?.counters?.tables || 0
        const codeSets = extraction?.counters?.code_sets || 0
        await load(detail.id)
        if (extraction?.status === 'FAILED') {
          setError('解析未完成，请查看进度记录后重试。')
          return
        }
        setNotice(`解析完成：生成 ${tables} 张报表、${codeSets} 张码表。`)
        if (onShowAssets) onShowAssets(tables ? 'fields' : 'codes', '')
      } else {
        setNotice('文件结构检查和初步切块已完成，可以继续解析。')
      }
    } catch (err) {
      setImportProgress((current) => current ? { ...current, status: 'FAILED', detail: friendlyError(err.message) } : null)
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function deleteDocument() {
    if (!detail) return
    const confirmed = window.confirm(
      `确定从文件列表移除“${detail.file_name}”吗？\n\n已生成的报表、码值和运行记录都会保留；重新上传同一文件会重新检查结构并切块。`
    )
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      await v2(`/documents/${detail.id}`, { method: 'DELETE' })
      await load()
      setNotice(`已移除「${detail.file_name}」，相关解析结果仍然保留。`)
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function reextractBlocks() {
    if (!detail || !window.confirm('重新识别会保留已核对内容，并更新当前文件的识别结果。是否继续？')) return
    setBusy(true)
    try {
      await v2(`/documents/${detail.id}/reextract`, { method: 'POST', body: JSON.stringify({}) })
      await selectDocument(detail.id)
      setNotice('已重新识别文件内容，历史核对结果仍然保留。')
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function analyzeStructure() {
    if (!detail) return
    setBusy(true)
    setError('')
    try {
      const result = await v2(`/documents/${detail.id}/analyze-structure`, { method: 'POST', body: JSON.stringify({}) })
      setArchitecture(result)
      await selectDocument(detail.id)
      setArchitectureExpanded(true)
      setNotice('文件结构检查完成，已区分报表、码值表和参考材料。')
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function adjustStructure(block) {
    setMappingBlock(block)
    setMappingForm(block.column_mapping || {})
    try {
      const rawBlock = await v2(`/documents/${detail.id}/blocks/${block.id}`)
      const content = rawBlock.content_json || {}
      const options = content.headers || content.rows?.[0] || []
      setMappingOptions(Array.from(new Set(options.map((item) => String(item).trim()).filter(Boolean))))
    } catch (err) {
      setError(friendlyError(err.message))
    }
  }

  async function confirmBlock(block) {
    setBusy(true)
    setError('')
    try {
      await v2(`/documents/${detail.id}/blocks/${block.id}/structure`, {
        method: 'PATCH',
        body: JSON.stringify({ human_required: false }),
      })
      await selectDocument(detail.id)
      setNotice(`已确认「${block.table_name || block.source_locator?.section || '当前内容'}」无误。`)
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function saveStructure() {
    if (!mappingBlock) return
    setBusy(true)
    try {
      const columnMapping = Object.fromEntries(
        Object.entries(mappingForm).filter(([, value]) => String(value).trim())
      )
      await v2(`/documents/${detail.id}/blocks/${mappingBlock.id}/structure`, {
        method: 'PATCH',
        body: JSON.stringify({ column_mapping: columnMapping, human_required: true, editor: '结构调整人工确认' }),
      })
      await selectDocument(detail.id)
      setMappingBlock(null)
      setNotice('列对应关系已保存，后续解析会使用新的设置。')
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  async function viewArchitecture() {
    if (!architecture) {
      setArchitecture(await v2(`/documents/${detail.id}/architecture`))
    }
    setArchitectureExpanded(!architectureExpanded)
  }

  async function showRaw(blockId) {
    if (!detail) return
    setRaw(await v2(`/documents/${detail.id}/blocks/${blockId}`))
    window.setTimeout(() => document.getElementById('raw-content-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  async function extractParallel(targets = ['metadata', 'code_sets']) {
    if (!selected.length || !detail) return
    setBusy(true)
    setError('')
    try {
      const result = await v2(`/documents/${detail.id}/extract-parallel`, {
        method: 'POST',
        body: JSON.stringify({ block_ids: selected, targets }),
      })
      const tableCount = result.table_ids?.length || 0
      const codeSetCount = result.code_set_ids?.length || 0
      const resolvedCount = result.resolved_reference_count || 0
      setNotice(`解析完成：识别 ${tableCount} 张报表、${codeSetCount} 张码值表，并关联 ${resolvedCount} 处字段码值。`)
      if (result.errors?.length) {
        const firstError = result.errors[0]
        setError(`${result.errors.length} 项内容未完成：${friendlyError(firstError.error)}`)
        return
      }
      if (targets.length === 1 && targets[0] === 'code_sets') {
        const firstSetId = result.code_set_ids?.[0] || ''
        if (onShowAssets) onShowAssets('codes', firstSetId)
        else navigate(firstSetId ? `/code-sets?set=${firstSetId}` : '/code-sets')
      } else {
        const firstTableId = result.table_ids?.[0] || ''
        if (onShowAssets) onShowAssets('fields', firstTableId)
        else navigate(firstTableId ? `/metadata?table=${firstTableId}` : '/metadata')
      }
    } catch (err) {
      setError(friendlyError(err.message))
    } finally {
      setBusy(false)
    }
  }

  const allBlocks = (detail?.blocks || []).filter((b) => !b.superseded)
  const needsPreparation = Boolean(detail) && (detail.status === 'OVERVIEW_FAILED' || allBlocks.length === 0)
  const codeBlock = (b) => b.candidate_role === 'CODE_SET' || b.block_type === 'CODE_SET' || b.block_type === 'CODE_SET_CANDIDATE'
  const evidenceBlock = (b) => ['RULE_EVIDENCE', 'REVISION_DIFF'].includes(b.candidate_role) || ['RULE_EVIDENCE', 'REVISION_DIFF'].includes(b.block_type)
  const tableBlocks = allBlocks.filter((b) => !codeBlock(b) && !evidenceBlock(b))
  const codeSetBlocks = allBlocks.filter(codeBlock)
  const evidenceBlocks = allBlocks.filter(evidenceBlock)
  const eligibleBlocks = allBlocks.filter((b) => b.eligible !== false)
  const totalInlineCodeSets = tableBlocks.reduce((acc, b) => acc + (b.inline_code_count || 0), 0)
  // 切块自检发现的问题也要进"需要确认"清单：公式错位、折行串行这类不用等 AI 复核
  const reviewBlocks = allBlocks.filter(
    (b) => b.human_required || (Array.isArray(b.quality_issues) && b.quality_issues.length > 0)
  )
  const reviewCount = reviewBlocks.length

  const visibleBlocks = allBlocks.filter((b) => {
    if (blockFilter === 'review') return b.human_required
    if (blockFilter === 'eligible') return b.eligible !== false
    if (blockFilter === 'tables') return !codeBlock(b) && !evidenceBlock(b)
    if (blockFilter === 'codes') return codeBlock(b)
    if (blockFilter === 'evidence') return evidenceBlock(b)
    return true
  })

  function toggleSelectAllVisible() {
    const visibleEligibleIds = visibleBlocks.filter((b) => b.eligible !== false).map((b) => b.id)
    const allSelected = visibleEligibleIds.length > 0 && visibleEligibleIds.every((id) => selected.includes(id))
    if (allSelected) {
      setSelected(selected.filter((id) => !visibleEligibleIds.includes(id)))
    } else {
      setSelected(Array.from(new Set([...selected, ...visibleEligibleIds])))
    }
  }

  if (importOnly) {
    return (
      <>
        <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />
        {(!detail || showUpload) && (
          <label
            className={`upload-dropzone ${dragging ? 'dragging' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <div className="upload-dropzone-text">
              <strong>选择或拖入规范文件，上传后自动解析</strong>
              <span>支持 PDF、Word、Excel；完成后直接在下方核对报表字段和码表</span>
            </div>
            <span className="button primary">{busy ? '正在自动解析...' : '选择文件并解析'}</span>
            <input className="file-input" type="file" accept=".pdf,.docx,.xlsx" disabled={busy} onChange={onFileInputChange} />
          </label>
        )}
        {detail && (
          <section className="current-file-bar compact">
            <div className="current-file-main">
              <span className="current-file-eyebrow">当前文件</span>
              <div className="current-file-heading">
                <CurrentFilePicker documents={documents} currentId={detail.id} busy={busy} onSelect={selectDocument} />
                <StatusBadge status={detail.status} />
              </div>
              <p className="current-file-meta">
                {(() => {
                  const current = documents.find((doc) => doc.id === detail.id)
                  if (!current) return '文件已加载'
                  return [
                    String(current.file_type || '').toUpperCase(),
                    `${current.block_count ?? 0} 个内容块`,
                    current.created_at ? `${formatTime(current.created_at)} 上传` : '',
                  ].filter(Boolean).join(' · ')
                })()}
              </p>
            </div>
            <div className="current-file-actions">
              {detail.status === 'OVERVIEW_FAILED' && <button type="button" className="button primary sm" disabled={busy} onClick={prepareDocument}>重新检查并解析</button>}
              {detail.status !== 'OVERVIEW_FAILED' && <button type="button" className="button secondary sm" disabled={busy} onClick={parseCurrentDocument}>重新解析</button>}
              <button type="button" className="button secondary sm" disabled={busy} onClick={() => setShowUpload(!showUpload)}>
                {showUpload ? '收起导入' : '导入新文件'}
              </button>
              <button type="button" className="button ghost danger-text sm" disabled={busy} onClick={deleteDocument}>删除文件</button>
            </div>
          </section>
        )}
        <ImportProgressPanel progress={importProgress?.status === 'DONE' && !showUpload ? null : importProgress} onClose={() => setImportProgress(null)} />
      </>
    )
  }

  return (
    <>
      {!embedded && (
        <PageHeader
          eyebrow="第 1 步 · 共 3 步"
          title="导入监管规范"
          description="上传 PDF、Word 或 Excel 文件，系统会自动识别报表字段和码值，您只需确认识别结果。"
        />
      )}

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <label
        className={`upload-dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="upload-dropzone-text">
          <strong>把规范文件拖到这里，或点击上传</strong>
          <span>支持 PDF、Word、Excel；可上传采集规范、统计制度、核查规则或码值文件</span>
        </div>
        <span className="button primary">选择文件</span>
        <input
          className="file-input"
          type="file"
          accept=".pdf,.docx,.xlsx"
          disabled={busy}
          onChange={onFileInputChange}
        />
      </label>

      <ImportProgressPanel progress={importProgress} onClose={() => setImportProgress(null)} />

      <section className="workspace-grid document-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>已导入文件</h2>
              <p>共 {documents.length} 份规范文档</p>
            </div>
            <StatusBadge tone="neutral">{documents.length}</StatusBadge>
          </div>

          {documents.length > 0 ? (
            <div className="list-stack">
              {documents.map((doc) => (
                <button
                  type="button"
                  className={`list-row ${detail?.id === doc.id ? 'selected' : ''}`}
                  key={doc.id}
                  onClick={() => selectDocument(doc.id)}
                >
                  <div className="list-row-top">
                    <strong>{doc.file_name}</strong>
                    <StatusBadge tone="blue">{doc.file_type.toUpperCase()}</StatusBadge>
                  </div>
                  <span>识别到 {doc.block_count} 项内容{doc.spec_version && doc.spec_version !== 'UNKNOWN' ? ` · ${doc.spec_version}` : ''}</span>
                  <em>{formatTime(doc.created_at)} 导入</em>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">尚未上传任何规范文件，请点击上方区域上传。</div>
          )}
        </div>

        <div className="panel">
          {detail ? (
            <>
              <div className="panel-heading document-summary">
                <div>
                  <h2>{detail.file_name}</h2>
                  <p>
                    识别到 {tableBlocks.length} 张报表、{codeSetBlocks.length} 张码值表
                    {reviewCount > 0 ? ` · ${reviewCount} 项需要确认` : ' · 内容已就绪'}
                  </p>
                </div>
                <div className="actions">
                  <button type="button" className="button secondary danger-text" disabled={busy} onClick={deleteDocument}>删除文件</button>
                  <button
                    type="button"
                    className="button primary"
                    disabled={busy || (!needsPreparation && !selected.length)}
                    onClick={() => needsPreparation ? prepareDocument() : extractParallel(['metadata', 'code_sets'])}
                  >
                    {busy
                      ? '正在处理，请稍候...'
                      : needsPreparation
                        ? (detail.status === 'OVERVIEW_FAILED' ? '重新检查并切块' : '检查结构并切块')
                        : `开始解析 (${selected.length} 项)`}
                  </button>
                </div>
              </div>

              <div className="architecture-bar">
                <div>
                  <strong>
                    {needsPreparation
                      ? (detail.status === 'OVERVIEW_FAILED' ? '结构检查未完成，尚未初步切块' : '需要先完成整份文件结构检查')
                      : '结构检查与初步切块已完成'}
                  </strong>
                  <span>
                    {needsPreparation
                      ? '只有结构检查完成后，程序才会生成初始内容块。'
                      : reviewCount > 0
                        ? '部分内容需要您确认；其余内容可直接解析。'
                        : '勾选需要的内容，系统会一次完成字段与码值提取。'}
                  </span>
                </div>
                <button type="button" className="button secondary sm" onClick={viewArchitecture}>
                  {architectureExpanded ? '收起结构详情' : '查看结构详情'}
                </button>
              </div>
              {architecture && architectureExpanded && (
                <div className="architecture-result">
                  <div className="architecture-summary">
                    <StatusBadge tone={architecture.warnings?.length ? 'amber' : 'green'}>
                      {architecture.warnings?.length ? `${architecture.warnings.length} 项需要确认` : '识别正常'}
                    </StatusBadge>
                    <span>以下是系统对文件结构的检查结果</span>
                  </div>
                  {architecture.plan?.stage === 'DOCUMENT_OVERVIEW' && architecture.plan?.result?.summary && (
                    <p className="overview-summary">{architecture.plan.result.summary}</p>
                  )}
                  {architecture.warnings?.map((warning, index) => (
                    <p className="architecture-warning" key={index}>{friendlyError(warning)}</p>
                  ))}
                  <details>
                    <summary>查看技术详情</summary>
                    <pre>{JSON.stringify(architecture.plan, null, 2)}</pre>
                  </details>
                </div>
              )}

              <details className="technical-details">
                <summary>高级操作与技术详情</summary>
                <div className="technical-actions">
                  <button type="button" className="button secondary sm" disabled={busy || !selected.length} onClick={() => extractParallel(['code_sets'])}>仅解析码值</button>
                  {!needsPreparation && <button type="button" className="button secondary sm" disabled={busy} onClick={reextractBlocks}>重新识别文件内容</button>}
                  {detail.status === 'UPLOADED' && <button type="button" className="button secondary sm" disabled={busy} onClick={analyzeStructure}>单独检查内容分类</button>}
                </div>
                <div className="technical-meta">
                  共 {allBlocks.length} 个原始块 · {evidenceBlocks.length} 个参考材料 · {totalInlineCodeSets} 组字段枚举 · SHA-256 <code>{detail.sha256}</code>
                </div>
              </details>

              {mappingBlock && (
                <section className="mapping-panel">
                  <div className="panel-heading">
                    <div>
                      <h3>调整列对应关系</h3>
                      <p>{mappingBlock.table_name || mappingBlock.source_locator?.section || '当前内容'}：从下拉框选择原文件中的列标题，不需要手写。</p>
                    </div>
                    <button type="button" className="button secondary sm" onClick={() => setMappingBlock(null)}>取消</button>
                  </div>
                  <div className="mapping-form-grid">
                    {COLUMN_MAPPING_FIELDS.map(([key, label]) => (
                      <label key={key}>
                        <span>{label}</span>
                        {mappingOptions.length > 0 ? (
                          <select value={mappingForm[key] || ''} onChange={(e) => setMappingForm({ ...mappingForm, [key]: e.target.value })}>
                            <option value="">自动识别 / 不使用</option>
                            {Array.from(new Set([mappingForm[key], ...mappingOptions])).filter(Boolean).map((option) => <option value={option} key={option}>{option}</option>)}
                          </select>
                        ) : (
                          <input value={mappingForm[key] || ''} onChange={(e) => setMappingForm({ ...mappingForm, [key]: e.target.value })} placeholder="未读取到表头，可填写列标题" />
                        )}
                      </label>
                    ))}
                  </div>
                  <button type="button" className="button primary" disabled={busy} onClick={saveStructure}>保存对应关系</button>
                </section>
              )}

              {reviewCount > 0 && (
                <section className="confirmation-panel">
                  <div className="confirmation-heading">
                    <div>
                      <h3>需要您确认的内容 ({reviewCount} 项)</h3>
                      <p>请对照原文确认列对应、字段类型或长度。确认无误后点击“确认无误”；需要修改则点击“调整列对应”。</p>
                    </div>
                    {reviewBlocks.length > 0 && <button type="button" className="button secondary sm" onClick={() => setBlockFilter('review')}>在下方列表中查看</button>}
                  </div>
                  <div className="confirmation-list">
                    {reviewBlocks.map((block) => (
                      <div className="confirmation-item" key={block.id}>
                        <div>
                          <strong>{block.table_name || block.source_locator?.section || `内容 ${block.ordinal + 1}`}</strong>
                          <p>
                            {(() => {
                              const issues = Array.isArray(block.quality_issues) ? block.quality_issues : []
                              const first = issues.find((item) => item.severity === 'ERROR') || issues[0]
                              if (first) {
                                return `${first.field ? `${first.field}：` : ''}${first.message}${issues.length > 1 ? `（另有 ${issues.length - 1} 项）` : ''}`
                              }
                              return friendlyError(
                                block.structure_warnings?.[0] || block.reason || '程序无法完全确认这部分结构，请对照原文检查。'
                              )
                            })()}
                          </p>
                        </div>
                        <div className="actions">
                          <button type="button" className="button secondary sm" onClick={() => showRaw(block.id)}>查看原文</button>
                          <button type="button" className="button secondary sm" onClick={() => adjustStructure(block)}>调整列对应</button>
                          <button type="button" className="button primary sm" disabled={busy} onClick={() => confirmBlock(block)}>确认无误</button>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <div className="block-toolbar">
                <div className="actions">
                  <button type="button" className="button secondary sm" onClick={toggleSelectAllVisible}>选择当前列表</button>
                  {reviewBlocks.length > 0 && <button type="button" className={`button sm ${blockFilter === 'review' ? 'primary' : 'secondary'}`} onClick={() => setBlockFilter('review')}>待确认 ({reviewBlocks.length})</button>}
                  <button type="button" className={`button sm ${blockFilter === 'all' ? 'primary' : 'secondary'}`} onClick={() => setBlockFilter('all')}>全部内容 ({allBlocks.length})</button>
                  <button type="button" className={`button sm ${blockFilter === 'tables' ? 'primary' : 'secondary'}`} onClick={() => setBlockFilter('tables')}>报表 ({tableBlocks.length})</button>
                  <button type="button" className={`button sm ${blockFilter === 'codes' ? 'primary' : 'secondary'}`} onClick={() => setBlockFilter('codes')}>码值表 ({codeSetBlocks.length})</button>
                  {evidenceBlocks.length > 0 && <button type="button" className={`button sm ${blockFilter === 'evidence' ? 'primary' : 'secondary'}`} onClick={() => setBlockFilter('evidence')}>参考材料 ({evidenceBlocks.length})</button>}
                </div>
                <span className="muted-text">点击内容可查看原文</span>
              </div>

              <div className="block-list">
                {visibleBlocks.map((block) => {
                  const isSelected = selected.includes(block.id)
                  const isCurrentRaw = raw?.id === block.id
                  const isCodeBlock =
                    block.candidate_role === 'CODE_SET' ||
                    block.block_type === 'CODE_SET' ||
                    block.block_type === 'CODE_SET_CANDIDATE'
                  const title =
                    block.table_name ||
                    block.source_locator?.section ||
                    block.source_locator?.sheet ||
                    `待确认内容 ${block.ordinal + 1}`
                  return (
                    <div
                      className={`block-row ${block.eligible === false ? 'ineligible' : ''} ${isCurrentRaw ? 'active-block' : ''}`}
                      key={block.id}
                    >
                      <input
                        type="checkbox"
                        disabled={block.eligible === false || block.superseded}
                        checked={isSelected}
                        onChange={(event) =>
                          setSelected(
                            event.target.checked
                              ? [...selected, block.id]
                              : selected.filter((id) => id !== block.id)
                          )
                        }
                      />
                      <button type="button" className="block-row-main" onClick={() => showRaw(block.id)}>
                        <div className="block-row-info">
                          <strong>{title}</strong>
                          <span>{block.reason || `类型：${block.block_type}`}</span>
                        </div>
                        <div className="actions">
                          <SourceLocatorTag locator={block.source_locator} />
                          <StatusBadge tone={isCodeBlock ? 'amber' : 'blue'}>
                            {isCodeBlock ? '码值表' : '报表'}
                          </StatusBadge>
                          {block.field_count ? (
                            <StatusBadge tone="green">
                              {isCodeBlock ? `${block.field_count} 项码值` : `${block.field_count} 字段`}
                            </StatusBadge>
                          ) : null}
                          {!isCodeBlock && block.inline_code_count > 0 && (
                            <StatusBadge tone="amber">含 {block.inline_code_count} 组取值</StatusBadge>
                          )}
                          {block.human_required && <StatusBadge tone="amber">需要确认</StatusBadge>}
                          <button
                            type="button"
                            className="button secondary sm"
                            onClick={(event) => {
                              event.stopPropagation()
                              adjustStructure(block)
                            }}
                          >
                            调整列对应
                          </button>
                        </div>
                      </button>
                    </div>
                  )
                })}
              </div>

              {raw && (
                <section className="raw-panel" id="raw-content-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>原文与识别详情</h2>
                      <p>核对系统识别内容是否与规范原文一致</p>
                    </div>
                    <div className="actions">
                      <SourceLocatorTag locator={raw.source_locator} />
                      <button type="button" className="button secondary sm" onClick={() => setRaw(null)}>
                        收起预览
                      </button>
                    </div>
                  </div>
                  <pre>{raw.raw_content}</pre>
                </section>
              )}
            </>
          ) : (
            <div className="empty">请从左侧选择一份规范文件查看切块详情。</div>
          )}
        </div>
      </section>
    </>
  )
}
