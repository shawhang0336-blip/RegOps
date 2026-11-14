import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function GenerationPage() {
  const navigate = useNavigate()
  const [tables, setTables] = useState([])
  const [selected, setSelected] = useState([])
  const [approver, setApprover] = useState('')
  const [blockers, setBlockers] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    v2('/metadata/tables?status=PUBLISHED').then(setTables).catch((err) => setError(err.message))
  }, [])

  function toggle(id) { setSelected(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]) }

  async function checkBlockers() {
    const result = {}
    for (const id of selected) result[id] = (await v2(`/generation/blockers/${id}`)).blockers
    setBlockers(result)
    return result
  }

  async function create() {
    if (!selected.length || !approver.trim()) { setError('请选择表并填写确认人。'); return }
    setBusy(true)
    setError('')
    try {
      const issues = await checkBlockers()
      if (Object.values(issues).some((items) => items.length)) {
        setError('存在未解决码值引用，请先在码值中心处理。')
        return
      }
      const run = await v2('/generation/runs', { method: 'POST', body: JSON.stringify({ table_ids: selected, approver, provider: 'live' }) })
      setNotice('生成运行已创建，AI 计划及后续工具参数均可在执行中心查看。')
      navigate(`/runs?run=${run.id}`)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return <>
    <PageHeader title="生成中心" description="只使用已发布元数据和已解决码值。AI 决定规则与数据计划，程序负责预算和执行。" actions={<button className="button primary" disabled={busy || !selected.length} onClick={create}>启动生成</button>} />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="panel generation-panel">
      <div className="panel-heading"><div><h2>选择生成范围</h2><p>{tables.length} 张已发布表</p></div><div className="actions"><button className="button secondary" onClick={() => setSelected(tables.map((table) => table.id))}>全选</button><button className="button secondary" onClick={() => setSelected([])}>清空</button><input value={approver} onChange={(event) => setApprover(event.target.value)} placeholder="范围确认人" /></div></div>
      <div className="generation-table-list">{tables.map((table) => <label key={table.id} className={selected.includes(table.id) ? 'selected' : ''}>
        <input type="checkbox" checked={selected.includes(table.id)} onChange={() => toggle(table.id)} />
        <span><strong>{table.table_name}</strong><code>{table.table_code} · v{table.published_version} · {table.field_count} 字段</code>{blockers[table.id]?.length > 0 && <em>{JSON.stringify(blockers[table.id])}</em>}</span>
      </label>)}</div>
      {!tables.length && <p className="empty">请先在元数据工作台发布至少一张表。</p>}
    </section>
    <section className="panel process-explainer"><h2>后续执行顺序</h2><div className="process-steps">{['AI 生成计划', 'Schema 扇出', '规则扇出与跨表汇合', '拓展规则确权', '生成器扇出', '审计用例扇出', 'Oracle 双表执行', '差异回边'].map((step, index) => <div key={step}><span>{String(index + 1).padStart(2, '0')}</span><strong>{step}</strong></div>)}</div></section>
  </>
}
