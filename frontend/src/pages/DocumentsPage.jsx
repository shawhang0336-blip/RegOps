import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function DocumentsPage() {
  const navigate = useNavigate()
  const [documents, setDocuments] = useState([])
  const [detail, setDetail] = useState(null)
  const [selected, setSelected] = useState([])
  const [raw, setRaw] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() { setDocuments(await v2('/documents')) }
  useEffect(() => { load().catch((err) => setError(err.message)) }, [])

  async function selectDocument(id) {
    setDetail(await v2(`/documents/${id}`))
    setSelected([])
    setRaw(null)
  }

  async function upload(event) {
    const file = event.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    setBusy(true)
    setError('')
    try {
      const document = await v2('/documents', { method: 'POST', body: form })
      await load()
      await selectDocument(document.id)
      setNotice(`已提取 ${document.block_count} 个原始块。请选择需要 AI 形成元数据的块。`)
    } catch (err) { setError(err.message) } finally { setBusy(false); event.target.value = '' }
  }

  async function showRaw(blockId) {
    setRaw(await v2(`/documents/${detail.id}/blocks/${blockId}`))
  }

  async function extract() {
    if (!selected.length) return
    setBusy(true)
    setError('')
    try {
      const result = await v2('/metadata/extract', { method: 'POST', body: JSON.stringify({ block_ids: selected }) })
      setNotice(`真实 GLM 已完成 ${result.table_ids.length} 张表的元数据赋值与自审，请进入工作台复核。`)
      navigate('/metadata')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return <>
    <PageHeader title="文件中心" description="PDF、DOCX、XLSX 先形成带原始位置的 RawBlock，再交给 AI 形成元数据。" actions={<label className="button primary">上传文件<input className="file-input" type="file" accept=".pdf,.docx,.xlsx" disabled={busy} onChange={upload} /></label>} />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="workspace-grid document-layout">
      <div className="panel list-panel"><div className="panel-heading"><h2>文件列表</h2><span>{documents.length}</span></div>{documents.map((document) => <button className="list-row" key={document.id} onClick={() => selectDocument(document.id)}><strong>{document.file_name}</strong><span>{document.file_type.toUpperCase()} · {document.block_count} 块</span></button>)}</div>
      <div className="panel"><div className="panel-heading"><div><h2>原始块</h2><p>{detail?.file_name || '选择左侧文件'}</p></div>{selected.length > 0 && <button className="button primary" disabled={busy} onClick={extract}>AI 解析 {selected.length} 块</button>}</div>
        {detail && <div className="block-list">{detail.blocks.map((block) => <div className={`block-row ${block.eligible === false ? 'ineligible' : ''}`} key={block.id}>
          <input type="checkbox" disabled={block.eligible === false} checked={selected.includes(block.id)} onChange={(event) => setSelected(event.target.checked ? [...selected, block.id] : selected.filter((id) => id !== block.id))} />
          <button onClick={() => showRaw(block.id)}><strong>{block.source_locator.section || block.source_locator.sheet || `表格 ${block.ordinal + 1}`}</strong><span>{block.field_count ? `${block.field_count} 字段 · ` : ''}{block.reason || block.block_type}</span></button>
        </div>)}</div>}
      </div>
    </section>
    {raw && <section className="panel raw-panel"><div className="panel-heading"><h2>原始证据</h2><button className="close-button" onClick={() => setRaw(null)}>关闭</button></div><pre>{raw.raw_content}</pre><details><summary>结构化原始内容</summary><pre>{JSON.stringify(raw.content_json, null, 2)}</pre></details></section>}
  </>
}
