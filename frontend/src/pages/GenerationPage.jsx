import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge, formatTime } from './SharedUI.jsx'

const PIPELINE_STAGES = [
  ['01', '生成表结构、码表和规则', '生成 DDL、码表和校验 SQL，并在 Oracle 空表上空跑验证'],
  ['02', '确认校验规则', '确认 AI 建议新增的规则'],
  ['03', '生成测试数据', '按已确认规则生成正常与异常测试数据'],
  ['04', '执行数据库测试', '在 Oracle 中检查误拦截和漏拦截'],
  ['05', '确认结果并下载', '确认测试结果后下载码表、表结构和校验 SQL'],
]

const ARTIFACTS = [
  { key: 'ddl', label: 'DDL', hint: '表名/字段名要能落成 Oracle 标识符' },
  { key: 'rule_materials', label: '规则原料', hint: '校验原文在、码值白名单拿得到' },
  { key: 'code_sets', label: '码值', hint: '检查通过会自动发布，不用手点；有缺失才拦' },
]

export default function GenerationPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const [tables, setTables] = useState([])
  const [selected, setSelected] = useState([])
  const [readiness, setReadiness] = useState({})
  const [acceptWarnings, setAcceptWarnings] = useState(false)
  const [checking, setChecking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    async function init() {
      setChecking(true)
      try {
        const [list, readinessPayload] = await Promise.all([
          v2('/metadata/tables?status=PUBLISHED'),
          v2('/metadata/readiness').catch(() => null),
        ])
        const map = Object.fromEntries(
          (readinessPayload?.tables || []).map((item) => [item.table_id, item])
        )
        setTables(list)
        setReadiness(map)

        const preferredTableId = params.get('table')
        if (preferredTableId && list.some((t) => t.id === preferredTableId)) {
          setSelected([preferredTableId])
        } else {
          setSelected(list.filter((t) => map[t.id]?.ddl?.ready).map((t) => t.id))
        }
      } catch (err) {
        setError(err.message)
      } finally {
        setChecking(false)
      }
    }
    init()
  }, [])

  const ddlReady = (id) => readiness[id]?.ddl?.ready !== false
  // 码值只看「有没有需要人补的东西」：码表发布是自动的（发布表时会发），
  // 所以「待发布」不参与判定，否则用户会为了一个不用他管的动作被挡住
  const codeProblems = (section) => section?.defects || []
  const fullyReady = (id) => {
    const item = readiness[id]
    if (!item) return false
    return item.ddl.ready && item.rule_materials.ready && codeProblems(item.code_sets).length === 0
  }
  // 只有会让阶段 2 直接失败的问题才硬拦。码值这项看真缺陷，不看发布状态。
  const hardBlockers = (id) => {
    const item = readiness[id]
    if (!item) return []
    return [...(item.rule_materials.blockers || []), ...codeProblems(item.code_sets)]
  }
  const pendingWarnings = (id) => {
    const item = readiness[id]
    if (!item) return []
    return [
      ...(item.rule_materials.warnings || []),
      ...(item.code_sets.warnings || []),
    ].filter((w) => w.severity !== 'ERROR')
  }

  function toggle(id) {
    const item = readiness[id]
    if (item) {
      const blocked = hardBlockers(id)
      if (blocked.length) {
        const first = blocked[0]
        setError(
          `${item.table_code} 还不能装配：${first.message}。请先在码表页批量发布（或补齐缺失码值）后再选。`
        )
        return
      }
      if (!item.ddl.ready) {
        const reason = item.ddl.blockers[0]?.message || '字段定义不完整'
        setError(`${item.table_code} 还不能生成 DDL：${reason}`)
        return
      }
    }
    setSelected(
      selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]
    )
  }

  function selectAllReady() {
    setSelected(tables.filter((t) => fullyReady(t.id)).map((t) => t.id))
  }

  async function create() {
    if (!selected.length) {
      setError('请至少勾选一张已发布的元数据表。')
      return
    }
    const blocked = selected.filter((id) => hardBlockers(id).length > 0)
    if (blocked.length) {
      const first = hardBlockers(blocked[0])[0]
      setError(
        `勾选的表里有 ${blocked.length} 张码值未就绪（例如 ${first.message}）。这一项无法确认跳过——阶段 2 会直接报错退出，请先在码表页批量发布或补齐码值。`
      )
      return
    }
    if (selected.some((id) => !ddlReady(id))) {
      setError('勾选的表里有 DDL 不就绪的（字段名超长或类型无法映射），请先处理。')
      return
    }
    const warned = selected.filter((id) => pendingWarnings(id).length > 0)
    if (warned.length > 0 && !acceptWarnings) {
      setError('勾选的表里有规则原料不全的，AI 只能凭字段名猜规则；确认已知风险后再继续。')
      return
    }

    setBusy(true)
    setError('')
    try {
      const run = await v2('/generation/runs', {
        method: 'POST',
        body: JSON.stringify({
          table_ids: selected,
          provider: 'live',
          accept_warnings: acceptWarnings,
        }),
      })
      const auto = Object.values(run.auto_published_code_sets || {}).reduce(
        (total, list) => total + (list || []).length,
        0
      )
      const finalized = (run.auto_finalized_tables || []).length
      const parts = []
      if (finalized > 0) parts.push(`已自动定稿 ${finalized} 张表的字段定义`)
      if (auto > 0) parts.push(`自动发布 ${auto} 张检查通过的码表`)
      setNotice(
        parts.length
          ? `${parts.join('，')}。生成运行已启动，正在跳转至执行与审计中心。`
          : '生成运行已启动，正在跳转至执行与审计中心跟踪实时进度。'
      )
      navigate(`/runs?run=${run.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const fullCount = tables.filter((t) => fullyReady(t.id)).length
  const ddlBlockedCount = tables.filter((t) => !ddlReady(t.id)).length
  const warnedSelected = selected.filter((id) => pendingWarnings(id).length > 0).length

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
          <span>三项就绪</span>
          <strong>{fullCount} 张</strong>
          <small>能建表、能灌码值、规则原料齐</small>
        </div>
        <div>
          <span>DDL 卡住</span>
          <strong>{ddlBlockedCount} 张</strong>
          <small>{ddlBlockedCount > 0 ? '字段名超长或类型无法映射' : '当前没有卡住项'}</small>
        </div>
        <div>
          <span>本次已选择</span>
          <strong>{selected.length} 张</strong>
          <small>{warnedSelected > 0 ? `其中 ${warnedSelected} 张带已知风险` : '可以选择一张或多张报表'}</small>
        </div>
      </section>

      {warnedSelected > 0 && (
        <label className="inline-option warn-ack">
          <input
            type="checkbox"
            checked={acceptWarnings}
            onChange={(e) => setAcceptWarnings(e.target.checked)}
          />
          我已知悉：勾选的 {warnedSelected} 张表里，部分字段的规则或码值尚未就绪，继续推进会产生缺失的规则或让阶段 2 报错。
        </label>
      )}

      <section className="panel generation-panel">
        <div className="panel-heading">
          <div>
            <h2>选择本次要生成的报表</h2>
            <p>只列出已发布的报表。规则由本阶段的 AI 依据校验原文生成，这里只保证原料齐备。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={selectAllReady}>
              选择三项就绪 ({fullCount})
            </button>
            <button
              type="button"
              className="button secondary sm"
              onClick={() => setSelected(tables.filter((t) => ddlReady(t.id)).map((t) => t.id))}
            >
              选择 DDL 就绪 ({tables.length - ddlBlockedCount})
            </button>
            <button type="button" className="button secondary sm" onClick={() => setSelected([])}>
              清空选择
            </button>
          </div>
        </div>

        {tables.length > 0 ? (
          <div className="generation-table-list">
            {tables.map((table) => {
              const item = readiness[table.id]
              const isSelected = selected.includes(table.id)
              const blockers = item?.ddl?.blockers || []
              const blocked = item ? !item.ddl.ready : false
              return (
                <div
                  key={table.id}
                  className={`generation-table-card ${isSelected ? 'selected' : ''} ${blocked ? 'has-blocker' : ''}`}
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
                      {item ? (
                        <div className="readiness-row">
                          {ARTIFACTS.map((artifact) => {
                            const section = item[artifact.key]
                            // 码值只看「有没有需要人补的东西」：发布是自动的，
                            // 「待发布」不占状态位
                            const problems =
                              artifact.key === 'code_sets'
                                ? section.defects || []
                                : section.blockers || []
                            // 这张表根本没有这一项（例如没有码值引用的报表）
                            const na = section.applicable === false
                            return (
                              <span
                                key={artifact.key}
                                className={`ready-chip ${na ? 'na' : problems.length ? 'bad' : 'ok'}`}
                                title={
                                  na
                                    ? `${artifact.label} 不适用：这张表没有引用任何码表`
                                    : problems.length
                                      ? problems.map((b) => b.message).join('；')
                                      : artifact.key === 'code_sets'
                                        ? '码值齐备，不需要补充；码表会在发布时自动发出'
                                        : `${artifact.label} 可以生成（${artifact.hint}）`
                                }
                              >
                                {artifact.label} {na ? '—' : problems.length ? `✕${problems.length}` : '✓'}
                              </span>
                            )
                          })}
                        </div>
                      ) : (
                        <StatusBadge tone="neutral">未评估</StatusBadge>
                      )}
                    </div>
                    <div className="generation-card-meta">
                      <code>{table.table_code}</code>
                      <StatusBadge tone="neutral">发布版 v{table.published_version}</StatusBadge>
                      <span className="muted-text">{table.field_count} 个字段 · 更新于 {formatTime(table.updated_at)}</span>
                    </div>

                    {blocked && (
                      <div className="blocker-alert-list" onClick={(e) => e.stopPropagation()}>
                        {blockers.slice(0, 3).map((b, idx) => (
                          <div key={idx} className="blocker-alert-item">
                            <span>{b.message}</span>
                            <button
                              type="button"
                              className="button secondary sm"
                              onClick={() => navigate(`/metadata?table=${table.id}`)}
                            >
                              去处理 →
                            </button>
                          </div>
                        ))}
                        {blockers.length > 3 && (
                          <div className="muted-text">另有 {blockers.length - 3} 项…</div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="empty">
            当前没有已发布的报表。请先在「核对字段」里把三项就绪的表批量发布。
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
