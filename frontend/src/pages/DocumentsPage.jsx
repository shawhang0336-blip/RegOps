import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { SourceLocatorTag, StatusBadge, formatTime } from './SharedUI.jsx'

export default function DocumentsPage() {
  const navigate = useNavigate()
  const [documents, setDocuments] = useState([])
  const [detail, setDetail] = useState(null)
  const [selected, setSelected] = useState([])
  const [raw, setRaw] = useState(null)
  const [onlyEligible, setOnlyEligible] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const list = await v2('/documents')
    setDocuments(list)
    const targetId = preferredId || detail?.id || list[0]?.id
    if (targetId) {
      await selectDocument(targetId)
    }
  }

  useEffect(() => {
    load().catch((err) => setError(err.message))
  }, [])

  async function selectDocument(id) {
    const doc = await v2(`/documents/${id}`)
    setDetail(doc)
    const eligibleIds = (doc.blocks || []).filter((b) => b.eligible !== false).map((b) => b.id)
    setSelected(eligibleIds)
    setRaw(null)
  }

  async function handleFileUpload(file) {
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    setBusy(true)
    setError('')
    try {
      const document = await v2('/documents', { method: 'POST', body: form })
      await load(document.id)
      setNotice(`已从「${document.file_name}」提取 ${document.block_count} 个原始块，并为您默认勾选全部合规候选块。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
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

  async function showRaw(blockId) {
    if (!detail) return
    setRaw(await v2(`/documents/${detail.id}/blocks/${blockId}`))
  }

  async function extract() {
    if (!selected.length) return
    setBusy(true)
    setError('')
    try {
      const result = await v2('/metadata/extract', {
        method: 'POST',
        body: JSON.stringify({ block_ids: selected }),
      })
      const firstTableId = result.table_ids?.[0] || ''
      setNotice(`GLM 已完成 ${result.table_ids.length} 张表的结构化元数据提取与独立自审，正在进入元数据工作台。`)
      navigate(firstTableId ? `/metadata?table=${firstTableId}` : '/metadata')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const allBlocks = detail?.blocks || []
  const eligibleBlocks = allBlocks.filter((b) => b.eligible !== false)
  const visibleBlocks = onlyEligible ? eligibleBlocks : allBlocks

  function toggleSelectAllEligible() {
    if (selected.length === eligibleBlocks.length) {
      setSelected([])
    } else {
      setSelected(eligibleBlocks.map((b) => b.id))
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="STAGE 01 · 规范导入与原始块提取"
        title="规范文件中心"
        description="支持上传 PDF、DOCX、XLSX 监管规范文件，自动切分带原文页码与章节位置的 RawBlock，再交由 AI 提取表级元数据。"
        actions={
          <label className="button primary">
            {busy ? '正在处理...' : '+ 上传规范文件'}
            <input
              className="file-input"
              type="file"
              accept=".pdf,.docx,.xlsx"
              disabled={busy}
              onChange={onFileInputChange}
            />
          </label>
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <label
        className={`upload-dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="upload-dropzone-text">
          <strong>拖拽监管规范文件到此处，或点击直接上传</strong>
          <span>支持格式：.pdf / .docx / .xlsx（单文件上限 30 MB，自动计算 SHA-256 防重并保留原文证据锚点）</span>
        </div>
        <span className="button secondary">选择本地文件</span>
        <input
          className="file-input"
          type="file"
          accept=".pdf,.docx,.xlsx"
          disabled={busy}
          onChange={onFileInputChange}
        />
      </label>

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
                  <span>{doc.block_count} 个原始块 · 版本 {doc.spec_version || '—'}</span>
                  <em>导入于 {formatTime(doc.created_at)}</em>
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
              <div className="panel-heading">
                <div>
                  <h2>{detail.file_name}</h2>
                  <p>
                    共切分 {allBlocks.length} 个原始块（其中 {eligibleBlocks.length} 个符合字段表特征） · SHA-256:{' '}
                    <code>{detail.sha256?.slice(0, 12)}…</code>
                  </p>
                </div>
                <div className="actions">
                  <button
                    type="button"
                    className="button primary"
                    disabled={busy || !selected.length}
                    onClick={extract}
                  >
                    {busy ? 'AI 正在提取并自审...' : `AI 解析选中块 (${selected.length}) →`}
                  </button>
                </div>
              </div>

              <div className="block-toolbar">
                <div className="actions">
                  <button type="button" className="button secondary sm" onClick={toggleSelectAllEligible}>
                    {selected.length === eligibleBlocks.length && eligibleBlocks.length > 0
                      ? '取消全选'
                      : `全选合规候选块 (${eligibleBlocks.length})`}
                  </button>
                  <button
                    type="button"
                    className="button secondary sm"
                    onClick={() => setOnlyEligible(!onlyEligible)}
                  >
                    {onlyEligible ? '显示全部原始块' : '仅看合规候选块'}
                  </button>
                </div>
                <span className="muted-text">点击任意块可预览右侧/下方原文内容与结构化表格数据</span>
              </div>

              <div className="block-list">
                {visibleBlocks.map((block) => {
                  const isSelected = selected.includes(block.id)
                  const isCurrentRaw = raw?.id === block.id
                  const title =
                    block.source_locator?.section ||
                    block.source_locator?.sheet ||
                    `原始表格 #${block.ordinal + 1}`
                  return (
                    <div
                      className={`block-row ${block.eligible === false ? 'ineligible' : ''} ${isCurrentRaw ? 'active-block' : ''}`}
                      key={block.id}
                    >
                      <input
                        type="checkbox"
                        disabled={block.eligible === false}
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
                          {block.field_count ? (
                            <StatusBadge tone="green">{block.field_count} 字段</StatusBadge>
                          ) : null}
                          <StatusBadge tone={block.eligible === false ? 'neutral' : 'blue'}>
                            {block.eligible === false ? '跳过（非采集表）' : '可提取'}
                          </StatusBadge>
                        </div>
                      </button>
                    </div>
                  )
                })}
              </div>

              {raw && (
                <section className="raw-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>原始块证据预览</h2>
                      <p>块哈希：<code>{raw.raw_hash?.slice(0, 16)}…</code></p>
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
