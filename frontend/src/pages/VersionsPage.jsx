import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function VersionsPage() {
  const [tables, setTables] = useState([])
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => { v2('/metadata/tables').then(setTables).catch((err) => setError(err.message)) }, [])
  async function select(id) { setDetail(await v2(`/metadata/tables/${id}`)) }
  return <>
    <PageHeader title="版本与审计" description="比较 AI 初值、人工修改与发布版本，保留每次语义变化的责任链。" />
    <StatusMessage error={error} />
    <section className="workspace-grid versions-layout">
      <div className="panel list-panel"><div className="panel-heading"><h2>选择元数据版本</h2></div>{tables.map((table) => <button className="list-row" key={table.id} onClick={() => select(table.id)}><strong>{table.table_name}</strong><span>v{table.current_version} · {table.status}</span></button>)}</div>
      <div className="panel">{detail ? <><div className="panel-heading"><div><h2>{detail.table_name}</h2><p>当前 v{detail.current_version} · 发布 v{detail.published_version || '未发布'}</p></div></div><div className="validation-list">{detail.revisions.map((item, index) => <div key={index}><strong>{item.revision_kind} · {item.editor}</strong><p>{item.reason} · {item.created_at}</p><details><summary>查看前后差异</summary><div className="diff-grid"><pre>{JSON.stringify(item.old_value, null, 2)}</pre><pre>{JSON.stringify(item.new_value, null, 2)}</pre></div></details></div>)}</div></> : <p className="empty">请选择一张表查看版本。</p>}</div>
    </section>
  </>
}
