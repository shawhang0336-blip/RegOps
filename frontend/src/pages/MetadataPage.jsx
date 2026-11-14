import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

const emptyField = { field_cn: '', field_en: '', field_definition: '', check_text_raw: '', data_type: 'VARCHAR', length: 128, decimal_places: 0, required: false, code_reference_text: '', confidence: 1, evidence_json: { source: 'HUMAN' } }

export default function MetadataPage() {
  const [tables, setTables] = useState([])
  const [detail, setDetail] = useState(null)
  const [editor, setEditor] = useState('')
  const [newField, setNewField] = useState(emptyField)
  const [showAdd, setShowAdd] = useState(false)
  const [tab, setTab] = useState('fields')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() { setTables(await v2('/metadata/tables')) }
  useEffect(() => { load().catch((err) => setError(err.message)) }, [])
  async function select(id) { setDetail(await v2(`/metadata/tables/${id}`)); setTab('fields') }
  function change(id, key, value) { setDetail({ ...detail, fields: detail.fields.map((field) => field.id === id ? { ...field, [key]: value } : field) }) }
  function requireEditor() { if (!editor.trim()) { setError('请先填写编辑/审批人。'); return false } return true }

  async function save(field) {
    if (!requireEditor()) return
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, { method: 'PATCH', body: JSON.stringify({ expected_version: detail.current_version, editor, reason: '人工复核元数据', changes: {
        field_cn: field.field_cn, field_en: field.field_en, field_definition: field.field_definition, check_text_raw: field.check_text_raw,
        data_type: field.data_type, length: Number(field.length), decimal_places: Number(field.decimal_places), required: field.required, code_reference_text: field.code_reference_text,
      } }) })
      setDetail(body); setNotice(`${field.field_en} 已保存为新草稿版本。`)
    } catch (err) { setError(err.message) }
  }

  async function remove(field) {
    if (!requireEditor() || !window.confirm(`确认删除字段 ${field.field_en}？`) ) return
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, { method: 'DELETE', body: JSON.stringify({ expected_version: detail.current_version, editor, reason: '人工删除', changes: {} }) })
      setDetail(body)
    } catch (err) { setError(err.message) }
  }

  async function add() {
    if (!requireEditor()) return
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields`, { method: 'POST', body: JSON.stringify({ expected_version: detail.current_version, editor, reason: '人工补充字段', field: { ...newField, length: Number(newField.length), decimal_places: Number(newField.decimal_places) } }) })
      setDetail(body); setShowAdd(false); setNewField(emptyField)
    } catch (err) { setError(err.message) }
  }

  async function publish() {
    if (!requireEditor()) return
    try {
      const body = await v2(`/metadata/tables/${detail.id}/publish`, { method: 'POST', body: JSON.stringify({ expected_version: detail.current_version, approver: editor }) })
      setDetail(body); await load(); setNotice(`已发布 ${body.table_code} v${body.published_version}，可用于第二阶段。`)
    } catch (err) { setError(err.message) }
  }

  async function createDraft() {
    if (!requireEditor()) return
    try {
      const body = await v2(`/metadata/tables/${detail.id}/draft`, { method: 'POST', body: JSON.stringify({ expected_version: detail.current_version, approver: editor }) })
      await load(); await select(body.id); setNotice('已从发布版本创建可编辑草稿。')
    } catch (err) { setError(err.message) }
  }

  return <>
    <PageHeader title="元数据工作台" description="AI 初值、程序验证、独立自审与人工修改分栏保留；发布后才可用于生成。" actions={<div className="approval-submit"><input value={editor} onChange={(event) => setEditor(event.target.value)} placeholder="编辑 / 审批人" /></div>} />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="workspace-grid metadata-layout">
      <div className="panel list-panel"><div className="panel-heading"><h2>元数据表</h2><span>{tables.length}</span></div>{tables.map((table) => <button className="list-row" key={table.id} onClick={() => select(table.id)}><strong>{table.table_name}</strong><span>{table.table_code} · {table.field_count} 字段</span><em>{table.status} · v{table.current_version}</em></button>)}</div>
      <div className="panel metadata-detail">
        {detail ? <>
          <div className="panel-heading"><div><h2>{detail.table_name}</h2><p>{detail.table_code} · {detail.status} · v{detail.current_version}</p></div><div className="actions"><button className="button secondary" onClick={() => setShowAdd(!showAdd)}>增加字段</button>{detail.status === 'PUBLISHED' ? <button className="button secondary" onClick={createDraft}>新建草稿</button> : <button className="button primary" onClick={publish}>发布版本</button>}</div></div>
          <div className="tab-bar"><button className={tab === 'fields' ? 'active' : ''} onClick={() => setTab('fields')}>字段</button><button className={tab === 'validation' ? 'active' : ''} onClick={() => setTab('validation')}>验证报告</button><button className={tab === 'revisions' ? 'active' : ''} onClick={() => setTab('revisions')}>变更记录</button></div>
          {tab === 'fields' && <div className="table-wrap"><table><thead><tr><th>中文名 / 英文名</th><th>字段定义</th><th>原始校验</th><th>类型 / 长度</th><th>必填 / 码值引用</th><th>操作</th></tr></thead><tbody>{detail.fields.map((field) => <tr key={field.id}>
            <td><input value={field.field_cn} onChange={(e) => change(field.id, 'field_cn', e.target.value)} /><input value={field.field_en} onChange={(e) => change(field.id, 'field_en', e.target.value)} /><details><summary>AI 初值与证据</summary><pre>{JSON.stringify(field.ai_value_json, null, 2)}</pre><code>{JSON.stringify(field.evidence_json)}</code></details></td>
            <td><textarea value={field.field_definition} onChange={(e) => change(field.id, 'field_definition', e.target.value)} /></td>
            <td><textarea value={field.check_text_raw} onChange={(e) => change(field.id, 'check_text_raw', e.target.value)} /></td>
            <td><select value={field.data_type} onChange={(e) => change(field.id, 'data_type', e.target.value)}><option>VARCHAR</option><option>NUMBER</option></select><input type="number" value={field.length} onChange={(e) => change(field.id, 'length', e.target.value)} /></td>
            <td><label className="check-label"><input type="checkbox" checked={field.required} onChange={(e) => change(field.id, 'required', e.target.checked)} />必填</label><input placeholder="码值表引用" value={field.code_reference_text} onChange={(e) => change(field.id, 'code_reference_text', e.target.value)} /></td>
            <td><div className="row-actions"><button onClick={() => save(field)}>保存</button><button className="danger" onClick={() => remove(field)}>删除</button></div></td>
          </tr>)}</tbody></table></div>}
          {showAdd && <div className="add-field-form">{Object.entries(newField).filter(([key]) => !['evidence_json', 'decimal_places', 'confidence'].includes(key)).map(([key, value]) => <label key={key}><span>{key}</span>{key === 'required' ? <input type="checkbox" checked={value} onChange={(e) => setNewField({ ...newField, [key]: e.target.checked })} /> : <input value={value} type={key === 'length' ? 'number' : 'text'} onChange={(e) => setNewField({ ...newField, [key]: e.target.value })} />}</label>)}<button className="button primary" onClick={add}>添加</button></div>}
          {tab === 'validation' && <div className="validation-list">{detail.validations.map((item, index) => <div key={index}><strong>{item.validator_kind} · {item.status}</strong><pre>{JSON.stringify(item.issues, null, 2)}</pre></div>)}</div>}
          {tab === 'revisions' && <div className="validation-list">{detail.revisions.map((item, index) => <div key={index}><strong>{item.revision_kind} · {item.editor}</strong><p>{item.reason} · {item.created_at}</p><details><summary>查看差异</summary><pre>{JSON.stringify({ old: item.old_value, new: item.new_value }, null, 2)}</pre></details></div>)}</div>}
        </> : <p className="empty">请选择一张元数据表。</p>}
      </div>
    </section>
  </>
}
