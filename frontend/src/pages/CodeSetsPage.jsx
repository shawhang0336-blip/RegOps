import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function CodeSetsPage() {
  const [sets, setSets] = useState([])
  const [detail, setDetail] = useState(null)
  const [editor, setEditor] = useState('')
  const [newSet, setNewSet] = useState({ code: '', name: '', definition: '' })
  const [newValue, setNewValue] = useState({ value: '', label_cn: '' })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() { setSets(await v2('/code-sets')) }
  useEffect(() => { load().catch((err) => setError(err.message)) }, [])
  async function select(id) { setDetail(await v2(`/code-sets/${id}`)) }
  function requireEditor() { if (!editor.trim()) { setError('请填写维护人。'); return false } return true }

  async function createSet() {
    try {
      const body = await v2('/code-sets', { method: 'POST', body: JSON.stringify({ ...newSet, source_type: 'MANUAL' }) })
      await load(); await select(body.id); setNewSet({ code: '', name: '', definition: '' })
    } catch (err) { setError(err.message) }
  }

  async function addValue() {
    if (!requireEditor()) return
    try {
      const body = await v2(`/code-sets/${detail.id}/values`, { method: 'POST', body: JSON.stringify({ expected_version: detail.current_version, editor, ...newValue }) })
      setDetail(body); setNewValue({ value: '', label_cn: '' }); await load()
    } catch (err) { setError(err.message) }
  }

  async function toggle(value) {
    if (!requireEditor()) return
    try {
      const body = await v2(`/code-sets/${detail.id}/values/${value.id}`, { method: 'PATCH', body: JSON.stringify({ expected_version: detail.current_version, editor, reason: '启停码值', changes: { enabled: !value.enabled } }) })
      setDetail(body)
    } catch (err) { setError(err.message) }
  }

  async function publish() {
    if (!requireEditor()) return
    try {
      const body = await v2(`/code-sets/${detail.id}/publish`, { method: 'POST', body: JSON.stringify({ expected_version: detail.current_version, editor, reason: '发布码值版本', changes: {} }) })
      setDetail(body); await load(); setNotice('码值版本已发布，字段引用状态已刷新。')
    } catch (err) { setError(err.message) }
  }

  async function importExcel(event) {
    if (!requireEditor()) return
    const file = event.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    form.append('expected_version', detail.current_version)
    try {
      const body = await v2(`/code-sets/${detail.id}/import`, { method: 'POST', body: form })
      setDetail(body.code_set); setNotice(`已导入 ${body.imported} 个码值。`)
    } catch (err) { setError(err.message) } finally { event.target.value = '' }
  }

  return <>
    <PageHeader title="码值中心" description="文档引用、人工维护和外部导入统一版本化；没有码值时保持 UNRESOLVED，不由 AI 虚构。" actions={<input value={editor} onChange={(event) => setEditor(event.target.value)} placeholder="维护人" />} />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="workspace-grid code-layout">
      <div className="panel list-panel"><div className="panel-heading"><h2>码值表</h2><span>{sets.length}</span></div>{sets.map((set) => <button className="list-row" key={set.id} onClick={() => select(set.id)}><strong>{set.name}</strong><span>{set.code} · {set.value_count} 项</span><em>{set.status}{set.unresolved_reference_count ? ` · ${set.unresolved_reference_count} 未解决` : ''}</em></button>)}
        <details className="inline-form"><summary>新建码值表</summary><input placeholder="代码" value={newSet.code} onChange={(e) => setNewSet({ ...newSet, code: e.target.value })} /><input placeholder="名称" value={newSet.name} onChange={(e) => setNewSet({ ...newSet, name: e.target.value })} /><textarea placeholder="定义" value={newSet.definition} onChange={(e) => setNewSet({ ...newSet, definition: e.target.value })} /><button className="button primary" onClick={createSet}>创建</button></details>
      </div>
      <div className="panel">{detail ? <>
        <div className="panel-heading"><div><h2>{detail.name}</h2><p>{detail.code} · {detail.status} · v{detail.current_version}</p></div><div className="actions"><label className="button secondary">导入 Excel<input className="file-input" type="file" accept=".xlsx" onChange={importExcel} /></label><button className="button primary" onClick={publish}>发布版本</button></div></div>
        <div className="table-wrap"><table><thead><tr><th>码值</th><th>中文含义</th><th>英文含义</th><th>状态</th><th>来源</th><th>操作</th></tr></thead><tbody>{detail.values.map((value) => <tr key={value.id}><td><code>{value.value}</code></td><td>{value.label_cn}</td><td>{value.label_en}</td><td>{value.enabled ? '启用' : '停用'}</td><td>{JSON.stringify(value.source_locator)}</td><td><button onClick={() => toggle(value)}>{value.enabled ? '停用' : '启用'}</button></td></tr>)}</tbody></table></div>
        <div className="add-value-form"><input placeholder="码值" value={newValue.value} onChange={(e) => setNewValue({ ...newValue, value: e.target.value })} /><input placeholder="中文含义" value={newValue.label_cn} onChange={(e) => setNewValue({ ...newValue, label_cn: e.target.value })} /><button className="button primary" onClick={addValue}>添加码值</button></div>
      </> : <p className="empty">请选择或新建码值表。</p>}</div>
    </section>
  </>
}
