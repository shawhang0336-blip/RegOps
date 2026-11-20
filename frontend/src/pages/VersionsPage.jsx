import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { v2 } from '../api.js'
import { DetailSkeleton, ListSkeleton } from '../components/Skeleton.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { RevisionDiffView, StatusBadge } from './SharedUI.jsx'

export default function VersionsPage() {
  const navigate = useNavigate()
  const [tables, setTables] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tableSearch, setTableSearch] = useState('')
  const [filterKind, setFilterKind] = useState('ALL')
  const [error, setError] = useState('')
  const requestRef = useRef(0)

  async function select(id) {
    const reqId = ++requestRef.current
    setSelectedId(id)
    setDetailLoading(true)
    try {
      const data = await v2(`/metadata/tables/${id}`)
      if (reqId === requestRef.current) setDetail(data)
    } catch (err) {
      if (reqId === requestRef.current) setError(err.message)
    } finally {
      if (reqId === requestRef.current) setDetailLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    v2('/metadata/tables')
      .then(async (list) => {
        setTables(list)
        if (list[0]?.id) {
          await select(list[0].id)
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  const visibleTables = tables.filter((table) => {
    const q = tableSearch.trim().toLowerCase()
    if (!q) return true
    return (
      (table.table_name || '').toLowerCase().includes(q) ||
      (table.table_code || '').toLowerCase().includes(q)
    )
  })

  const revisions = (detail?.revisions || []).filter(
    (item) => filterKind === 'ALL' || item.revision_kind === filterKind,
  )

  return (
    <>
      <PageHeader
        eyebrow="修改可追溯"
        title="变更记录"
        description="按报表查看每次自动识别、人工修改和发布记录，并对比修改前后的内容。"
        actions={
          detail && (
            <button
              type="button"
              className="button secondary"
              onClick={() => navigate(`/metadata?table=${detail.id}`)}
            >
              打开此表 →
            </button>
          )
        }
      />

      <StatusMessage error={error} onClose={() => setError('')} />

      <section className="workspace-grid versions-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>选择报表</h2>
              <p>共 {tables.length} 张表</p>
            </div>
            <StatusBadge tone="neutral">{tables.length}</StatusBadge>
          </div>

          <div className="list-filters" style={{ marginBottom: 10 }}>
            <input
              type="search"
              placeholder="搜索报表名称或代码..."
              value={tableSearch}
              onChange={(e) => setTableSearch(e.target.value)}
            />
          </div>

          {loading ? (
            <ListSkeleton rows={6} />
          ) : visibleTables.length > 0 ? (
            <div className="list-stack">
              {visibleTables.map((table) => (
                <button
                  type="button"
                  className={`list-row ${(selectedId || detail?.id) === table.id ? 'selected' : ''}`}
                  key={table.id}
                  onClick={() => select(table.id)}
                >
                  <div className="list-row-top">
                    <strong>{table.table_name}</strong>
                    <StatusBadge status={table.status} />
                  </div>
                  <span>
                    <code>{table.table_code}</code> · 当前 v{table.current_version}
                  </span>
                  <em>{table.published_version ? `已发布 v${table.published_version}` : '未发布'}</em>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">
              {tableSearch ? '没有匹配的报表。' : '暂无元数据表版本记录。'}
            </div>
          )}
        </div>

        <div className={`panel ${detailLoading && detail ? 'panel-transitioning' : ''}`}>
          {detailLoading && !detail ? (
            <DetailSkeleton blocks={4} />
          ) : detail ? (
            <>
              <div className="panel-heading">
                <div>
                  <div className="actions">
                    <h2>{detail.table_name}</h2>
                    <code>{detail.table_code}</code>
                    <StatusBadge status={detail.status} />
                  </div>
                  <p>
                    当前版本 v{detail.current_version} · 发布基线{' '}
                    {detail.published_version ? `v${detail.published_version}` : '未发布'} · 共{' '}
                    {detail.revisions.length} 条变更记录
                  </p>
                </div>
                <div className="actions">
                  <select
                    value={filterKind}
                    onChange={(e) => setFilterKind(e.target.value)}
                    aria-label="筛选变更类型"
                  >
                    <option value="ALL">全部变更类型 ({detail.revisions.length})</option>
                    <option value="HUMAN_EDIT">人工修改</option>
                    <option value="PUBLISHED">发布记录</option>
                    <option value="AI_EXTRACTION">自动识别</option>
                    <option value="AI_REVIEW">AI 复核</option>
                  </select>
                </div>
              </div>

              {revisions.length > 0 ? (
                <div className="revision-list">
                  {revisions.map((item, index) => (
                    <RevisionDiffView key={index} revision={item} />
                  ))}
                </div>
              ) : (
                <div className="empty">当前筛选条件下没有变更记录。</div>
              )}
            </>
          ) : (
            <div className="empty">请从左侧选择一张表查看版本演进与字段级差异。</div>
          )}
        </div>
      </section>
    </>
  )
}
