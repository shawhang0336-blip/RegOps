import { useMemo, useRef, useState } from 'react'
import { getMetadataTable } from '../../../api/catalog.js'
import { RevisionDiffView } from '../../../components/RevisionDiffView.jsx'
import SelectDropdown from '../../../components/SelectDropdown.jsx'
import { DetailSkeleton, ListSkeleton } from '../../../components/Skeleton.jsx'
import { StatusBadge } from '../../../components/StatusBadge.jsx'
import { regulatorMeta } from '../../../utils/activeDocument.js'
import { formatTime } from '../../../utils/dateTime.js'

const REVISION_KIND_OPTIONS = [
  { value: 'ALL', label: '全部变更类型' },
  { value: 'HUMAN_EDIT', label: '人工修改字段' },
  { value: 'HUMAN_ADD', label: '人工新增字段' },
  { value: 'HUMAN_DELETE', label: '人工删除字段' },
  { value: 'PUBLISHED', label: '版本发布' },
  { value: 'DRAFT_CREATED', label: '创建修改草稿' },
  { value: 'AI_REVIEW', label: 'AI 独立复核' },
  { value: 'AI_EXTRACTION', label: 'AI 初始识别' },
]

export default function ChangeFeedTab({
  revisions,
  revisionSummary,
  revisionsLoading,
  humanOnly,
  onToggleHumanOnly,
  kindFilter,
  onChangeKindFilter,
  tables,
  tablesLoading,
  onOpenTable,
  onError,
}) {
  const [viewMode, setViewMode] = useState('timeline')
  const [searchText, setSearchText] = useState('')
  const [tableSearch, setTableSearch] = useState('')
  const [onlyEditedTables, setOnlyEditedTables] = useState(false)
  const [selectedTableId, setSelectedTableId] = useState('')
  const [tableDetail, setTableDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tableKindFilter, setTableKindFilter] = useState('ALL')
  const reqRef = useRef(0)

  async function selectTable(id) {
    if (!id) return
    const reqId = ++reqRef.current
    setSelectedTableId(id)
    setDetailLoading(true)
    try {
      const data = await getMetadataTable(id)
      if (reqId === reqRef.current) setTableDetail(data)
    } catch (err) {
      if (reqId === reqRef.current) onError?.(err.message || '加载单表修订记录失败')
    } finally {
      if (reqId === reqRef.current) setDetailLoading(false)
    }
  }

  const filteredFeed = useMemo(() => {
    const q = searchText.trim().toLowerCase()
    if (!q) return revisions
    return revisions.filter((item) => {
      const text = [
        item.table_name,
        item.table_code,
        item.package_name,
        item.editor,
        item.reason,
        item.revision_kind,
        item.new_value?.field_en,
        item.new_value?.field_cn,
        item.old_value?.field_en,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return text.includes(q)
    })
  }, [revisions, searchText])

  const sortedTables = useMemo(() => {
    const q = tableSearch.trim().toLowerCase()
    const list = tables.filter((t) => {
      if (onlyEditedTables && !(t.human_revision_count > 0)) return false
      if (!q) return true
      return (
        (t.table_name || '').toLowerCase().includes(q) ||
        (t.table_code || '').toLowerCase().includes(q)
      )
    })
    return [...list].sort((a, b) => {
      if ((b.human_revision_count || 0) !== (a.human_revision_count || 0)) {
        return (b.human_revision_count || 0) - (a.human_revision_count || 0)
      }
      const ta = a.last_revised_at || ''
      const tb = b.last_revised_at || ''
      return tb.localeCompare(ta)
    })
  }, [tables, tableSearch, onlyEditedTables])

  const filteredTableRevisions = useMemo(() => {
    const list = tableDetail?.revisions || []
    return list.filter((item) => tableKindFilter === 'ALL' || item.revision_kind === tableKindFilter)
  }, [tableDetail?.revisions, tableKindFilter])

  const governanceTotal =
    (revisionSummary?.human_edits || 0) +
    (revisionSummary?.publishes || 0) +
    (revisionSummary?.drafts || 0)

  return (
    <section className="panel ver-tab-panel">
      <div className="panel-heading">
        <div>
          <h2>变更动态与修订时间线</h2>
          <p>
            按时间倒序呈现监管报表字段的人工修订、草稿创建与版本发布动态，支持一键切换按单表追溯完整演进。
          </p>
        </div>
        <div className="ver-segmented" role="tablist" aria-label="变更查看视角">
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === 'timeline'}
            className={viewMode === 'timeline' ? 'active' : ''}
            onClick={() => setViewMode('timeline')}
          >
            全范围变更流 ({revisions.length})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === 'by-table'}
            className={viewMode === 'by-table' ? 'active' : ''}
            onClick={() => {
              setViewMode('by-table')
              if (!selectedTableId && sortedTables[0]?.id) {
                selectTable(sortedTables[0].id)
              }
            }}
          >
            按单表演进 ({tables.length})
          </button>
        </div>
      </div>

      {viewMode === 'timeline' ? (
        <>
          <div className="ver-toolbar">
            <input
              type="search"
              placeholder="搜索报表名、字段名、操作人或变更说明…"
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
            />
            <SelectDropdown
              ariaLabel="按变更类型筛选"
              size="sm"
              minWidth={165}
              value={kindFilter}
              options={REVISION_KIND_OPTIONS}
              onChange={(val) => {
                onChangeKindFilter(val)
                if (val === 'AI_EXTRACTION' || val === 'AI_REVIEW') {
                  onToggleHumanOnly(false)
                }
              }}
            />
            <button
              type="button"
              className={`button sm ${humanOnly ? 'primary' : 'secondary'}`}
              onClick={() => onToggleHumanOnly(!humanOnly)}
              title="过滤掉大批量系统初始 AI 提取记录，聚焦人工改字段与版本发布"
            >
              {humanOnly ? `✓ 仅看人工修改与发布 (${governanceTotal})` : '仅看人工修改与发布'}
            </button>
          </div>

          {revisionsLoading ? (
            <DetailSkeleton blocks={4} />
          ) : filteredFeed.length > 0 ? (
            <div className="revision-list">
              {filteredFeed.map((item) => {
                const rm = item.regulator ? regulatorMeta(item.regulator) : null
                return (
                  <RevisionDiffView
                    key={item.id}
                    revision={item}
                    tableContext={{
                      table_name: item.table_name,
                      table_code: item.table_code,
                      package_name: item.package_name,
                      regulatorLabel: rm ? `${rm.agencyShortLabel} · ${rm.shortLabel}` : null,
                    }}
                    onOpenTable={() => onOpenTable(item.table_id, item.document_id)}
                  />
                )
              })}
            </div>
          ) : (
            <div className="ver-empty-callout">
              <strong>
                {humanOnly && (revisionSummary?.ai_events || 0) > 0
                  ? '当前范围内尚无人工字段修订或发布记录'
                  : '当前筛选条件下暂无变更记录'}
              </strong>
              <p>
                {humanOnly && (revisionSummary?.ai_events || 0) > 0
                  ? `该范围内共有 ${revisionSummary.ai_events} 条系统 AI 自动解析与复核记录已被自动去噪隐藏。`
                  : '前往「01 规范解析与核对」修订字段或发布报表后，每次改动都会自动留痕于此。'}
              </p>
              {humanOnly && (revisionSummary?.ai_events || 0) > 0 && (
                <button
                  type="button"
                  className="button secondary sm"
                  onClick={() => onToggleHumanOnly(false)}
                >
                  显示全部记录（含 {revisionSummary.ai_events} 条 AI 解析）
                </button>
              )}
            </div>
          )}
        </>
      ) : (
        <div className="workspace-grid versions-layout ver-by-table-grid">
          <aside className="ver-table-sidebar">
            <div className="ver-sidebar-filters">
              <input
                type="search"
                placeholder="搜索报表名称或代码…"
                value={tableSearch}
                onChange={(e) => setTableSearch(e.target.value)}
              />
              <button
                type="button"
                className={`button sm ${onlyEditedTables ? 'primary' : 'secondary'}`}
                onClick={() => setOnlyEditedTables((v) => !v)}
              >
                {onlyEditedTables ? '✓ 仅看有人工修改的表' : '筛选有人工修改的表'}
              </button>
            </div>

            {tablesLoading ? (
              <ListSkeleton rows={6} />
            ) : sortedTables.length > 0 ? (
              <div className="list-stack ver-table-list">
                {sortedTables.map((table) => {
                  const selected = (selectedTableId || tableDetail?.id) === table.id
                  return (
                    <button
                      type="button"
                      className={`list-row ${selected ? 'selected' : ''}`}
                      key={table.id}
                      onClick={() => selectTable(table.id)}
                    >
                      <div className="list-row-top">
                        <strong>{table.table_name}</strong>
                        <StatusBadge status={table.status} />
                      </div>
                      <span className="ver-table-row-meta">
                        <code>{table.table_code}</code> · v{table.current_version}
                        {table.human_revision_count > 0 && (
                          <strong className="ver-human-pill">人工改 {table.human_revision_count}</strong>
                        )}
                      </span>
                      <em className="ver-table-row-foot">
                        <span>共 {table.revision_count ?? 0} 条记录</span>
                        <span>{table.last_revised_at ? formatTime(table.last_revised_at) : '—'}</span>
                      </em>
                    </button>
                  )
                })}
              </div>
            ) : (
              <div className="empty">
                {onlyEditedTables ? '当前范围内暂无人工修改过的报表。' : '没有匹配的报表。'}
              </div>
            )}
          </aside>

          <div className={`ver-table-detail ${detailLoading && tableDetail ? 'panel-transitioning' : ''}`}>
            {detailLoading && !tableDetail ? (
              <DetailSkeleton blocks={4} />
            ) : tableDetail ? (
              <>
                <div className="ver-table-detail-head">
                  <div>
                    <div className="actions">
                      <h3>{tableDetail.table_name}</h3>
                      <code>{tableDetail.table_code}</code>
                      <StatusBadge status={tableDetail.status} />
                    </div>
                    <p className="muted-text">
                      当前版本 v{tableDetail.current_version} · 发布基线{' '}
                      {tableDetail.published_version ? `v${tableDetail.published_version}` : '未发布'} · 共{' '}
                      {tableDetail.revisions.length} 条变更记录
                    </p>
                  </div>
                  <div className="actions">
                    <SelectDropdown
                      ariaLabel="筛选单表变更类型"
                      size="sm"
                      minWidth={155}
                      value={tableKindFilter}
                      options={REVISION_KIND_OPTIONS}
                      onChange={setTableKindFilter}
                    />
                    <button
                      type="button"
                      className="button secondary sm"
                      onClick={() => onOpenTable(tableDetail.id, tableDetail.document_id)}
                    >
                      打开此表 →
                    </button>
                  </div>
                </div>

                {filteredTableRevisions.length > 0 ? (
                  <div className="revision-list">
                    {filteredTableRevisions.map((item, idx) => (
                      <RevisionDiffView key={item.id || idx} revision={item} />
                    ))}
                  </div>
                ) : (
                  <div className="empty">当前筛选条件下没有变更记录。</div>
                )}
              </>
            ) : (
              <div className="empty">请从左侧选择一张报表，查看字段级完整演进历史。</div>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
