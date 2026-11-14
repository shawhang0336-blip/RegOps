import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import { useOperator } from '../context/OperatorContext.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { BLOCKER_REASONS, StatusBadge, formatTime } from './SharedUI.jsx'

const PIPELINE_STAGES = [
  ['01', 'AI 生成计划', '在预算上限内计算必做/拓展规则与造数配额'],
  ['02', 'Schema 扇出', '并发生成 MySQL 与 Oracle 沙箱建表 DDL'],
  ['03', '规则扇出与汇合', '单表显性/拓展规则装配与跨表屏障归约'],
  ['04', '拓展规则确权', '人工按规则内容哈希审批 AI 拓展规则'],
  ['05', '生成器扇出', '生成黄金正向样本与针对性变异负样本'],
  ['06', '审计用例扇出', '编译确定性校验 SQL 与预期命中矩阵'],
  ['07', 'Oracle 双表执行', '在真实 Oracle 沙箱建表、装数并执行校验'],
  ['08', '差异回边与收敛', '零误报零漏报则产出生产包，否则自愈回边'],
]

export default function GenerationPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { requireOperator } = useOperator()

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
    const approver = requireOperator(setError)
    if (!approver) return
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
        body: JSON.stringify({ table_ids: selected, approver, provider: 'live' }),
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
        eyebrow="STAGE 04 · 生产范围锁定与码值阻断预检"
        title="生成范围与预检中心"
        description="仅使用已发布定版的元数据表与已消解的公共码值；进入页面即自动完成全表预检，杜绝运行中途因码值缺失而失败。"
        actions={
          <button
            type="button"
            className="button primary"
            disabled={busy || checking || !selected.length}
            onClick={create}
          >
            {busy ? '正在启动 AI 规则装配...' : `启动 AI 规则与数据生成 (${selected.length} 张表) →`}
          </button>
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <section className="metrics">
        <div>
          <span>已发布元数据表</span>
          <strong>{tables.length}</strong>
          <small>已锁定 SHA-256 快照</small>
        </div>
        <div>
          <span>预检就绪可生成</span>
          <strong>{readyCount}</strong>
          <small>所有必填码值均已绑定</small>
        </div>
        <div>
          <span>存在码值阻断</span>
          <strong>{blockedCount}</strong>
          <small>{blockedCount > 0 ? '需前往码值中心补充' : '全部表均无阻断'}</small>
        </div>
        <div>
          <span>当前已勾选范围</span>
          <strong>{selected.length} 张表</strong>
          <small>支持单表或多表联合扇出</small>
        </div>
      </section>

      <section className="panel generation-panel">
        <div className="panel-heading">
          <div>
            <h2>选择本次生成范围</h2>
            <p>勾选需要装配校验规则与合成沙箱数据的已发布表（系统已为您自动预检码值依赖）</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={selectAllReady}>
              全选无阻断表 ({readyCount})
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
                        {hasBlocker ? `${tableBlockers.length} 项码值阻断` : '预检通过'}
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
                              前往消解码值 →
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
            当前尚无已发布的元数据表。请先在「元数据与版本台」完成字段复核并点击「发布定版」。
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>下游 8 阶段确定性扇出与沙箱自愈架构</h2>
            <p>启动生成后，执行中心将按以下 8 个阶段实时推进并记录不可篡改的事件账本</p>
          </div>
        </div>
        <div className="pipeline-stepper">
          {PIPELINE_STAGES.map(([num, label, desc]) => (
            <div key={num} className="pipeline-step" title={desc}>
              <span className="pipeline-step-num">STAGE {num}</span>
              <strong>{label}</strong>
            </div>
          ))}
        </div>
      </section>
    </>
  )
}
