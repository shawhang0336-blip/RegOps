import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { BLOCKER_REASONS, StatusBadge, formatTime } from './SharedUI.jsx'

const PIPELINE_STAGES = [
  ['01', '生成表结构、码表和规则', '生成 DDL、码表和校验 SQL，并在 Oracle 空表上空跑验证'],
  ['02', '确认校验规则', '确认 AI 建议新增的规则'],
  ['03', '生成测试数据', '按已确认规则生成正常与异常测试数据'],
  ['04', '执行数据库测试', '在 Oracle 中检查误拦截和漏拦截'],
  ['05', '确认结果并下载', '确认测试结果后下载码表、表结构和校验 SQL'],
]

export default function GenerationPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const [tables, setTables] = useState([])
  const [selected, setSelected] = useState([])
  const [blockers, setBlockers] = useState({})
  const [checking, setChecking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    async function init() {
      setChecking(true)
      try {
        const list = await v2('/metadata/tables?status=PUBLISHED')
        setTables(list)
        const blockerEntries = await Promise.all(
          list.map(async (t) => {
            const res = await v2(`/generation/blockers/${t.id}`).catch(() => ({ blockers: [] }))
            return [t.id, res.blockers || []]
          })
        )
        const blockerMap = Object.fromEntries(blockerEntries)
        setBlockers(blockerMap)

        const preferredTableId = params.get('table')
        if (preferredTableId && list.some((t) => t.id === preferredTableId)) {
          setSelected([preferredTableId])
        } else {
          const readyIds = list
            .filter((t) => !(blockerMap[t.id] && blockerMap[t.id].length > 0))
            .map((t) => t.id)
          setSelected(readyIds)
        }
      } catch (err) {
        setError(err.message)
      } finally {
        setChecking(false)
      }
    }
    init()
  }, [])

  function toggle(id) {
    setSelected(
      selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]
    )
  }

  function selectAllReady() {
    const readyIds = tables
      .filter((t) => !(blockers[t.id] && blockers[t.id].length > 0))
      .map((t) => t.id)
    setSelected(readyIds)
  }

  async function create() {
    if (!selected.length) {
      setError('请至少勾选一张已发布的元数据表。')
      return
    }
    const blockedSelected = selected.filter((id) => (blockers[id] || []).length > 0)
    if (blockedSelected.length > 0) {
      setError('当前勾选的表中存在未解决的码值引用阻断项，请先点击卡片上的「前往消解码值」处理后再启动生成。')
      return
    }

    setBusy(true)
    setError('')
    try {
      const run = await v2('/generation/runs', {
        method: 'POST',
        body: JSON.stringify({ table_ids: selected, provider: 'live' }),
      })
      setNotice('生成运行已启动，正在跳转至执行与审计中心跟踪实时进度。')
      navigate(`/runs?run=${run.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const readyCount = tables.filter((t) => !(blockers[t.id] && blockers[t.id].length > 0)).length
  const blockedCount = tables.length - readyCount

  return (
    <>
      <PageHeader
        eyebrow="第 2 步 · 共 3 步"
        title="生成表结构、码表和规则"
        description="根据已发布字段生成 DDL、码表和校验 SQL，在 Oracle 验证库建表后，由您确认规则。"
        actions={
          <button
            type="button"
            className="button primary"
            disabled={busy || checking || !selected.length}
            onClick={create}
          >
            {busy ? '正在启动 AI 规则装配...' : `开始生成 (${selected.length} 张报表)`}
          </button>
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <section className="metrics metrics-three">
        <div>
          <span>可以生成</span>
          <strong>{readyCount} 张</strong>
          <small>字段和码值均已就绪</small>
        </div>
        <div>
          <span>需要先处理</span>
          <strong>{blockedCount} 张</strong>
          <small>{blockedCount > 0 ? '请补充缺失的码值' : '当前没有待处理项'}</small>
        </div>
        <div>
          <span>本次已选择</span>
          <strong>{selected.length} 张</strong>
          <small>可以选择一张或多张报表</small>
        </div>
      </section>

      <section className="panel generation-panel">
        <div className="panel-heading">
          <div>
            <h2>选择本次要生成的报表</h2>
            <p>只能选择已发布的报表；如果提示缺少码值，请先完成补充。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={selectAllReady}>
              选择所有可生成报表 ({readyCount})
            </button>
            <button
              type="button"
              className="button secondary sm"
              onClick={() => setSelected(tables.map((t) => t.id))}
            >
              全选全部 ({tables.length})
            </button>
            <button type="button" className="button secondary sm" onClick={() => setSelected([])}>
              清空选择
            </button>
          </div>
        </div>

        {tables.length > 0 ? (
          <div className="generation-table-list">
            {tables.map((table) => {
              const tableBlockers = blockers[table.id] || []
              const hasBlocker = tableBlockers.length > 0
              const isSelected = selected.includes(table.id)
              return (
                <div
                  key={table.id}
                  className={`generation-table-card ${isSelected ? 'selected' : ''} ${hasBlocker ? 'has-blocker' : ''}`}
                  onClick={() => toggle(table.id)}
                >
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => {}}
                    onClick={(e) => e.stopPropagation()}
                  />
                  <div className="generation-card-body">
                    <div className="generation-card-top">
                      <strong>{table.table_name}</strong>
                      <StatusBadge tone={hasBlocker ? 'red' : 'green'}>
                        {hasBlocker ? `缺少 ${tableBlockers.length} 项码值` : '可以生成'}
                      </StatusBadge>
                    </div>
                    <div className="generation-card-meta">
                      <code>{table.table_code}</code>
                      <StatusBadge tone="neutral">发布版 v{table.published_version}</StatusBadge>
                      <span className="muted-text">{table.field_count} 个字段 · 更新于 {formatTime(table.updated_at)}</span>
                    </div>

                    {hasBlocker && (
                      <div className="blocker-alert-list" onClick={(e) => e.stopPropagation()}>
                        {tableBlockers.map((b, idx) => (
                          <div key={idx} className="blocker-alert-item">
                            <span>
                              字段 <code>{b.field_en}</code>：{BLOCKER_REASONS[b.reason] || b.reason}
                            </span>
                            <button
                              type="button"
                              className="button secondary sm"
                              onClick={() =>
                                navigate(
                                  b.code_set_id
                                    ? `/code-sets?set=${b.code_set_id}`
                                    : `/metadata?table=${table.id}`
                                )
                              }
                            >
                              去补充 →
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="empty">
            当前没有可生成的报表。请先在“核对字段”中检查并发布报表。
          </div>
        )}
      </section>

      <section className="panel simple-flow">
        <div className="panel-heading">
          <div>
            <h2>接下来会发生什么</h2>
            <p>大部分步骤自动完成，只在有新增规则时需要您确认。</p>
          </div>
        </div>
        <div className="pipeline-stepper">
          {PIPELINE_STAGES.map(([num, label, desc]) => (
            <div key={num} className="pipeline-step" title={desc}>
              <span className="pipeline-step-num">步骤 {num}</span>
              <strong>{label}</strong>
            </div>
          ))}
        </div>
      </section>
    </>
  )
}
