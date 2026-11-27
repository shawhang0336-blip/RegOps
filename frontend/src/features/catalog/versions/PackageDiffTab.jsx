import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { diffPackages } from '../../../api/catalog.js'
import { createGenerationRun } from '../../../api/generation.js'
import SelectDropdown from '../../../components/SelectDropdown.jsx'
import { DetailSkeleton } from '../../../components/Skeleton.jsx'
import { StatusBadge } from '../../../components/StatusBadge.jsx'
import { normalizeReportCode, regulatorMeta } from '../../../utils/activeDocument.js'

const IMPACT_META = {
  BREAKING: { label: '破坏性变更', tone: 'red' },
  RULE_IMPACT: { label: '规则受影响', tone: 'amber' },
  METADATA_ONLY: { label: '仅描述变更', tone: 'blue' },
  NONE: { label: '无变化', tone: 'neutral' },
}

const CHANGE_KIND_META = {
  ADDED: { label: '新增', tone: 'green' },
  REMOVED: { label: '下线', tone: 'red' },
  MODIFIED: { label: '变更', tone: 'amber' },
  UNCHANGED: { label: '无变化', tone: 'neutral' },
}

const ATTR_LABELS = {
  data_type: '数据类型',
  length: '列宽',
  decimal_places: '小数位',
  required: '必填属性',
  check_text_raw: '校验原文',
  code_reference_text: '码表引用',
  field_cn: '中文名称',
}

const TABLE_FILTER_OPTIONS = [
  { value: 'CHANGED', label: '仅看有变化报表' },
  { value: 'BREAKING', label: '仅看破坏性变更' },
  { value: 'ALL', label: '全部报表' },
]

function buildImpactReportMarkdown(diff) {
  const lines = [
    `# 监管制度升级影响面评估报告`,
    ``,
    `- **基线版本（旧版）**：${diff.base_package?.name}（v${diff.base_package?.spec_version}）`,
    `- **目标版本（新版）**：${diff.target_package?.name}（v${diff.target_package?.spec_version}）`,
    `- **评估时间**：${new Date().toLocaleString('zh-CN')}`,
    ``,
    `## 一、影响面总览`,
    `- 新增报表：${diff.summary.added_tables} 张 | 下线报表：${diff.summary.removed_tables} 张 | 变更报表：${diff.summary.modified_tables} 张 | 无变化：${diff.summary.unchanged_tables} 张`,
    `- 破坏性变更报表：${diff.summary.breaking_tables} 张 | 规则受影响报表：${diff.summary.rule_impact_tables} 张 | 建议增量重跑：${diff.summary.affected_table_count} 张`,
  ]
  if (diff.summary.code_sets) {
    const cs = diff.summary.code_sets
    lines.push(
      `- 码表变化：新增 ${cs.added} 个 / 下线 ${cs.removed} 个 / 变更 ${cs.modified} 个（码值 +${cs.added_values} / -${cs.removed_values}）`,
    )
  }
  if (diff.summary.rule_evidences) {
    const re = diff.summary.rule_evidences
    lines.push(
      `- 补充核查规则变化：新增 ${re.added} 条 / 下线 ${re.removed} 条 / 修订 ${re.modified ?? 0} 条`,
    )
  }
  lines.push(``, `## 二、变更报表清单`)
  const changedTables = (diff.tables || []).filter((t) => t.change_kind !== 'UNCHANGED')
  if (!changedTables.length) {
    lines.push(`无结构或校验变更报表。`)
  } else {
    for (const t of changedTables) {
      lines.push(
        `### ${t.table_name || t.table_code} (\`${t.table_code}\`) — ${CHANGE_KIND_META[t.change_kind]?.label || t.change_kind} / ${IMPACT_META[t.impact]?.label || t.impact}`,
      )
      if (t.added_fields?.length) {
        lines.push(`- **新增字段 (${t.added_fields.length})**：${t.added_fields.map((f) => `${f.field_en}(${f.data_type}(${f.length}))`).join('、')}`)
      }
      if (t.removed_fields?.length) {
        lines.push(`- **删除字段 (${t.removed_fields.length})**：${t.removed_fields.map((f) => f.field_en).join('、')}`)
      }
      if (t.modified_fields?.length) {
        for (const mf of t.modified_fields) {
          const desc = mf.changes.map((c) => `${ATTR_LABELS[c.attr] || c.attr}: ${c.old} → ${c.new}`).join('；')
          lines.push(`- **修改字段 \`${mf.field_en}\`**：${desc}`)
        }
      }
      lines.push(``)
    }
  }
  const changedCodeSets = (diff.code_sets || []).filter((c) => c.change_kind !== 'UNCHANGED')
  if (changedCodeSets.length) {
    lines.push(`## 三、码表码值变更清单`)
    for (const cs of changedCodeSets) {
      lines.push(`- **${cs.name} (\`${cs.code}\`)**：${CHANGE_KIND_META[cs.change_kind]?.label || cs.change_kind}（码值 ${cs.value_counts.base} → ${cs.value_counts.target}）`)
      if (cs.added_values?.length) {
        lines.push(`  - 新增码值：${cs.added_values.slice(0, 20).map((v) => `${v.value}=${v.label_cn}`).join('、')}`)
      }
      if (cs.removed_values?.length) {
        lines.push(`  - 停用/删除码值：${cs.removed_values.slice(0, 20).map((v) => `${v.value}=${v.label_cn}`).join('、')}`)
      }
    }
    lines.push(``)
  }
  return lines.join('\n')
}

export default function PackageDiffTab({
  packages,
  activePackageId,
  activePackage,
  activeRegulator,
  onNotice,
  onError,
}) {
  const navigate = useNavigate()
  const [allowCrossRegulator, setAllowCrossRegulator] = useState(false)
  const [basePkgId, setBasePkgId] = useState('')
  const [targetPkgId, setTargetPkgId] = useState('')
  const [diffData, setDiffData] = useState(null)
  const [diffBusy, setDiffBusy] = useState(false)
  const [genBusy, setGenBusy] = useState(false)
  const [subTab, setSubTab] = useState('tables')
  const [tableFilter, setTableFilter] = useState('CHANGED')

  const focusRegulator = useMemo(() => {
    if (activePackage?.regulator) return normalizeReportCode(activePackage.regulator)
    if (activeRegulator && activeRegulator !== 'ALL') return normalizeReportCode(activeRegulator)
    return ''
  }, [activePackage, activeRegulator])

  const sameRegulatorPackages = useMemo(() => {
    if (!focusRegulator) return packages
    return packages.filter((p) => normalizeReportCode(p.regulator) === focusRegulator)
  }, [packages, focusRegulator])

  // 找出所有本身存在 ≥2 个版本的监管体系，用于智能提示
  const multiVersionRegulators = useMemo(() => {
    const groups = new Map()
    for (const pkg of packages) {
      const code = normalizeReportCode(pkg.regulator)
      groups.set(code, (groups.get(code) || 0) + 1)
    }
    return [...groups.entries()].filter(([, count]) => count >= 2).map(([code]) => code)
  }, [packages])

  const selectablePackages = allowCrossRegulator
    ? packages
    : sameRegulatorPackages.length >= 2
      ? sameRegulatorPackages
      : []

  const isSingleVersionInFocus = !allowCrossRegulator && sameRegulatorPackages.length < 2

  useEffect(() => {
    if (!selectablePackages.length) {
      setBasePkgId('')
      setTargetPkgId('')
      setDiffData(null)
      return
    }
    const targetDef =
      (activePackageId && selectablePackages.some((p) => p.id === activePackageId))
        ? activePackageId
        : selectablePackages[0].id
    const targetObj = selectablePackages.find((p) => p.id === targetDef)
    const baseDef =
      (targetObj?.parent_package_id && selectablePackages.some((p) => p.id === targetObj.parent_package_id))
        ? targetObj.parent_package_id
        : (selectablePackages.find((p) => p.id !== targetDef)?.id || '')

    setTargetPkgId((prev) => (prev && selectablePackages.some((p) => p.id === prev) ? prev : targetDef))
    setBasePkgId((prev) => (prev && selectablePackages.some((p) => p.id === prev) ? prev : baseDef))
  }, [selectablePackages, activePackageId])

  const runDiff = useCallback(async (bId, tId) => {
    if (!bId || !tId || bId === tId) {
      setDiffData(null)
      return
    }
    setDiffBusy(true)
    onError?.('')
    try {
      const result = await diffPackages(bId, tId)
      setDiffData(result)
    } catch (err) {
      onError?.(err.message || '执行规范包版本对比失败')
    } finally {
      setDiffBusy(false)
    }
  }, [onError])

  useEffect(() => {
    if (basePkgId && targetPkgId && basePkgId !== targetPkgId) {
      runDiff(basePkgId, targetPkgId)
    } else {
      setDiffData(null)
    }
  }, [basePkgId, targetPkgId, runDiff])

  async function handleIncrementalRun() {
    const ids = diffData?.affected_table_ids || []
    if (!ids.length) return
    setGenBusy(true)
    onError?.('')
    try {
      const created = await createGenerationRun({ tableIds: ids, acceptWarnings: true })
      onNotice?.(`已对 ${ids.length} 张受影响报表发起增量规则生成与验证。`)
      navigate(`/runs?run=${created.id}`)
    } catch (err) {
      onError?.(err.message || '发起增量重跑失败')
    } finally {
      setGenBusy(false)
    }
  }

  function handleExportMarkdown() {
    if (!diffData) return
    const md = buildImpactReportMarkdown(diffData)
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `regulatory-upgrade-impact-${(diffData.target_package?.regulator || 'spec').toLowerCase()}.md`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    onNotice?.('已导出监管制度升级影响面评估报告 (.md)。')
  }

  const packageOptions = useMemo(
    () =>
      selectablePackages.map((pkg) => {
        const pm = regulatorMeta(pkg.regulator)
        return {
          value: pkg.id,
          badge: `${pm.agencyShortLabel} · ${pm.shortLabel}`,
          label: pkg.name,
          tag: pkg.spec_version && pkg.spec_version !== 'UNKNOWN' ? `v${pkg.spec_version}` : '',
          meta: `${pkg.table_count} 表 · ${pkg.code_set_count} 码表 · ${pkg.rule_evidence_count} 规则`,
          description: pkg.parent_package_id ? '继承自历史版本包' : pm.label,
        }
      }),
    [selectablePackages],
  )

  const visibleTables = useMemo(() => {
    const list = diffData?.tables || []
    return list.filter((item) => {
      if (tableFilter === 'CHANGED') return item.change_kind !== 'UNCHANGED'
      if (tableFilter === 'BREAKING') return item.impact === 'BREAKING'
      return true
    })
  }, [diffData?.tables, tableFilter])

  const changedCodeSets = useMemo(
    () => (diffData?.code_sets || []).filter((cs) => cs.change_kind !== 'UNCHANGED'),
    [diffData?.code_sets],
  )

  const ruleDetails = diffData?.rule_evidences || { added: [], removed: [], modified: [] }
  const ruleChangedCount =
    (ruleDetails.added?.length || 0) +
    (ruleDetails.removed?.length || 0) +
    (ruleDetails.modified?.length || 0)

  const focusMeta = focusRegulator ? regulatorMeta(focusRegulator) : null
  const isCrossRegPair =
    diffData?.base_package?.regulator &&
    diffData?.target_package?.regulator &&
    normalizeReportCode(diffData.base_package.regulator) !== normalizeReportCode(diffData.target_package.regulator)

  return (
    <section className="panel ver-tab-panel">
      <div className="panel-heading">
        <div>
          <h2>制度升级对比 & 影响面评估</h2>
          <p>
            对比同一监管报表的新旧年度规范包，自动识别报表字段破坏性变更、码表码值增删及补充核查规则变化，支持一键对受影响表发起增量重跑。
          </p>
        </div>
        <div className="actions">
          <button
            type="button"
            className={`button sm ${allowCrossRegulator ? 'primary' : 'secondary'}`}
            onClick={() => setAllowCrossRegulator((v) => !v)}
            title="默认仅同监管报表体系内做升级对比；开启后可自由选择任意两套规范包"
          >
            {allowCrossRegulator ? '✓ 已允许跨体系自由对比' : '高级：跨体系自由对比'}
          </button>
          {diffData && (
            <button type="button" className="button secondary sm" onClick={handleExportMarkdown}>
              导出评估报告 (.md)
            </button>
          )}
        </div>
      </div>

      {isSingleVersionInFocus ? (
        <div className="ver-guidance-card">
          <div className="ver-guidance-badge">单版本基线状态</div>
          <h3>
            {focusMeta
              ? `「${focusMeta.agencyShortLabel} · ${focusMeta.label}」当前仅有 ${sameRegulatorPackages.length} 个版本规范包，暂无前置旧版可对比`
              : '当前尚未形成同一监管报表体系的多版本规范包'}
          </h3>
          <p>
            监管升级对比用于比较同一报表体系的新旧版本（例如 <code>EAST 5.0 → EAST 5.1</code> 或 <code>一表通 2.0 → 2.1</code>）。
            待监管下发新年度修订文档时，在顶部切换栏点击 <strong>「+ 新建版本包」</strong> 并设置「继承自当前包」，导入解析后系统将在此自动标出全部破坏性字段、码值变动与需增量重跑的报表。
          </p>
          <div className="ver-guidance-actions">
            {multiVersionRegulators.length > 0 && (
              <span className="muted-text">
                已有多个版本的体系：{multiVersionRegulators.map((r) => regulatorMeta(r).label).join('、')}
              </span>
            )}
            {packages.length >= 2 && (
              <button
                type="button"
                className="button secondary sm"
                onClick={() => setAllowCrossRegulator(true)}
              >
                仍要跨体系自由选择两个包对比
              </button>
            )}
          </div>
        </div>
      ) : (
        <>
          <div className="ver-pair-bar">
            <div className="ver-pair-slot">
              <span className="ver-pair-label">基线规范包（旧版）</span>
              <SelectDropdown
                ariaLabel="选择基线规范包"
                value={basePkgId}
                options={packageOptions}
                disabled={diffBusy}
                onChange={setBasePkgId}
              />
            </div>

            <button
              type="button"
              className="button secondary sm ver-swap-btn"
              title="互换新旧对比方向"
              disabled={diffBusy || !basePkgId || !targetPkgId}
              onClick={() => {
                const prevBase = basePkgId
                setBasePkgId(targetPkgId)
                setTargetPkgId(prevBase)
              }}
            >
              ⇄ 互换
            </button>

            <div className="ver-pair-slot">
              <span className="ver-pair-label">目标规范包（新版）</span>
              <SelectDropdown
                ariaLabel="选择目标规范包"
                value={targetPkgId}
                options={packageOptions}
                disabled={diffBusy}
                onChange={setTargetPkgId}
              />
            </div>
          </div>

          {basePkgId && targetPkgId && basePkgId === targetPkgId && (
            <div className="action-callout" style={{ marginBottom: 12 }}>
              <div>
                <strong>当前左右选择了同一个规范包</strong>
                <p>请选择两个不同的规范包版本，系统将自动计算升级差异与影响面。</p>
              </div>
            </div>
          )}

          {isCrossRegPair && (
            <div className="action-callout" style={{ marginBottom: 12 }}>
              <div>
                <strong>提示：正在执行跨监管体系对比</strong>
                <p>
                  基线属于「{regulatorMeta(diffData.base_package.regulator).label}」，目标属于「
                  {regulatorMeta(diffData.target_package.regulator).label}」，表代码与码表通常不重叠，仅供横向参考。
                </p>
              </div>
            </div>
          )}

          {diffBusy ? (
            <DetailSkeleton blocks={4} />
          ) : diffData ? (
            <>
              <div className="audit-metrics" style={{ marginBottom: 14 }}>
                <div>
                  <span>报表增删 / 变更</span>
                  <strong>
                    +{diffData.summary.added_tables} / -{diffData.summary.removed_tables} · 改{' '}
                    {diffData.summary.modified_tables}
                  </strong>
                  <small>无变化 {diffData.summary.unchanged_tables} 张</small>
                </div>
                <div>
                  <span>破坏性 / 规则受影响</span>
                  <strong className={diffData.summary.breaking_tables > 0 ? 'danger-text' : ''}>
                    {diffData.summary.breaking_tables} 破坏性 / {diffData.summary.rule_impact_tables} 规则受影响
                  </strong>
                  <small>建议增量重跑 {diffData.summary.affected_table_count} 张表</small>
                </div>
                <div>
                  <span>码表与码值变更</span>
                  <strong>
                    {changedCodeSets.length} 个码表变动
                  </strong>
                  <small>
                    码值 +{diffData.summary.code_sets?.added_values ?? 0} / -
                    {diffData.summary.code_sets?.removed_values ?? 0}
                  </small>
                </div>
                <div>
                  <span>补充核查规则变更</span>
                  <strong>{ruleChangedCount} 条变化</strong>
                  <small>
                    +{diffData.summary.rule_evidences?.added ?? 0} / -
                    {diffData.summary.rule_evidences?.removed ?? 0} / 改{' '}
                    {diffData.summary.rule_evidences?.modified ?? 0}
                  </small>
                </div>
              </div>

              {diffData.affected_table_ids?.length > 0 && (
                <div className="action-callout" style={{ marginBottom: 14 }}>
                  <div>
                    <strong>识别到 {diffData.affected_table_ids.length} 张受影响报表需要重新装配规则</strong>
                    <p>仅对新增与字段/校验发生变化的报表发起 Step 2 增量规则生成，未变更报表保持历史产物不动。</p>
                  </div>
                  <button
                    type="button"
                    className="button primary sm"
                    disabled={genBusy}
                    onClick={handleIncrementalRun}
                  >
                    {genBusy ? '正在发起…' : `一键增量生成 (${diffData.affected_table_ids.length} 张)`}
                  </button>
                </div>
              )}

              <nav className="validation-tab-bar" style={{ marginBottom: 12 }}>
                <button
                  type="button"
                  className={subTab === 'tables' ? 'active' : ''}
                  onClick={() => setSubTab('tables')}
                >
                  报表与字段变更 ({(diffData.tables || []).filter((t) => t.change_kind !== 'UNCHANGED').length})
                </button>
                <button
                  type="button"
                  className={subTab === 'code-sets' ? 'active' : ''}
                  onClick={() => setSubTab('code-sets')}
                >
                  码表与码值变更 ({changedCodeSets.length})
                </button>
                <button
                  type="button"
                  className={subTab === 'rules' ? 'active' : ''}
                  onClick={() => setSubTab('rules')}
                >
                  补充核查规则变更 ({ruleChangedCount})
                </button>
              </nav>

              {subTab === 'tables' && (
                <>
                  <div className="ver-toolbar" style={{ marginBottom: 10 }}>
                    <SelectDropdown
                      ariaLabel="筛选报表差异范围"
                      size="sm"
                      minWidth={170}
                      value={tableFilter}
                      options={TABLE_FILTER_OPTIONS}
                      onChange={setTableFilter}
                    />
                  </div>
                  {visibleTables.length > 0 ? (
                    <div className="list-stack">
                      {visibleTables.map((tbl) => {
                        const cMeta = CHANGE_KIND_META[tbl.change_kind] || { label: tbl.change_kind, tone: 'neutral' }
                        const iMeta = IMPACT_META[tbl.impact] || { label: tbl.impact, tone: 'neutral' }
                        return (
                          <details
                            className="run-section ver-diff-card"
                            key={`${tbl.change_kind}-${tbl.table_code}`}
                            open={tbl.impact === 'BREAKING'}
                          >
                            <summary className="ver-diff-summary">
                              <span>
                                <strong>{tbl.table_name || tbl.table_code}</strong> <code>{tbl.table_code}</code>
                                {' · '}字段 {tbl.field_counts.base} → {tbl.field_counts.target}
                              </span>
                              <span className="actions">
                                <StatusBadge tone={cMeta.tone}>{cMeta.label}</StatusBadge>
                                <StatusBadge tone={iMeta.tone}>{iMeta.label}</StatusBadge>
                              </span>
                            </summary>
                            <div className="ver-diff-body">
                              {tbl.added_fields.length > 0 && (
                                <div className="ver-chip-group">
                                  <span className="ver-chip-label is-add">+ 新增字段 ({tbl.added_fields.length})</span>
                                  <div className="ver-chips">
                                    {tbl.added_fields.map((f) => (
                                      <span className="ver-code-chip is-add" key={f.field_en}>
                                        <code>{f.field_en}</code> {f.field_cn} ({f.data_type}({f.length})
                                        {f.required ? '·必填' : ''})
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {tbl.removed_fields.length > 0 && (
                                <div className="ver-chip-group">
                                  <span className="ver-chip-label is-del">- 删除字段 ({tbl.removed_fields.length})</span>
                                  <div className="ver-chips">
                                    {tbl.removed_fields.map((f) => (
                                      <span className="ver-code-chip is-del" key={f.field_en}>
                                        <code>{f.field_en}</code> {f.field_cn}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {tbl.modified_fields.length > 0 && (
                                <div className="table-wrap">
                                  <table>
                                    <thead>
                                      <tr>
                                        <th>字段</th>
                                        <th>风险等级</th>
                                        <th>具体变更（旧版 → 新版）</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {tbl.modified_fields.map((mf) => {
                                        const fm = IMPACT_META[mf.impact] || { label: mf.impact, tone: 'neutral' }
                                        return (
                                          <tr key={mf.field_en}>
                                            <td>
                                              <code>{mf.field_en}</code>
                                              <small>{mf.field_cn}</small>
                                            </td>
                                            <td>
                                              <StatusBadge tone={fm.tone}>{fm.label}</StatusBadge>
                                            </td>
                                            <td>
                                              {mf.changes.map((c, idx) => (
                                                <div className="ver-attr-change" key={idx}>
                                                  <strong>{ATTR_LABELS[c.attr] || c.attr}：</strong>
                                                  <del className="ver-old">{String(c.old)}</del>
                                                  <span>→</span>
                                                  <ins className="ver-new">{String(c.new)}</ins>
                                                </div>
                                              ))}
                                            </td>
                                          </tr>
                                        )
                                      })}
                                    </tbody>
                                  </table>
                                </div>
                              )}
                              {tbl.change_kind === 'UNCHANGED' && (
                                <p className="muted-text">该表字段结构与校验原文完全一致。</p>
                              )}
                            </div>
                          </details>
                        )
                      })}
                    </div>
                  ) : (
                    <div className="empty">当前筛选条件下没有差异报表。</div>
                  )}
                </>
              )}

              {subTab === 'code-sets' && (
                changedCodeSets.length > 0 ? (
                  <div className="list-stack">
                    {changedCodeSets.map((cs) => {
                      const cMeta = CHANGE_KIND_META[cs.change_kind] || { label: cs.change_kind, tone: 'neutral' }
                      const iMeta = IMPACT_META[cs.impact] || { label: cs.impact, tone: 'neutral' }
                      return (
                        <details className="run-section ver-diff-card" key={`${cs.change_kind}-${cs.code}`} open>
                          <summary className="ver-diff-summary">
                            <span>
                              <strong>{cs.name}</strong> <code>{cs.code}</code>
                              {' · '}码值 {cs.value_counts.base} → {cs.value_counts.target}
                            </span>
                            <span className="actions">
                              <StatusBadge tone={cMeta.tone}>{cMeta.label}</StatusBadge>
                              <StatusBadge tone={iMeta.tone}>{iMeta.label}</StatusBadge>
                            </span>
                          </summary>
                          <div className="ver-diff-body">
                            {cs.added_values?.length > 0 && (
                              <div className="ver-chip-group">
                                <span className="ver-chip-label is-add">+ 新增码值 ({cs.added_values.length})</span>
                                <div className="ver-chips">
                                  {cs.added_values.map((v) => (
                                    <span className="ver-code-chip is-add" key={v.value}>
                                      <code>{v.value}</code> {v.label_cn}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                            {cs.removed_values?.length > 0 && (
                              <div className="ver-chip-group">
                                <span className="ver-chip-label is-del">- 停用/删除码值 ({cs.removed_values.length})</span>
                                <div className="ver-chips">
                                  {cs.removed_values.map((v) => (
                                    <span className="ver-code-chip is-del" key={v.value}>
                                      <code>{v.value}</code> {v.label_cn}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                            {cs.modified_values?.length > 0 && (
                              <div className="table-wrap">
                                <table>
                                  <thead>
                                    <tr>
                                      <th>码值</th>
                                      <th>旧版含义 / 状态</th>
                                      <th>新版含义 / 状态</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {cs.modified_values.map((mv) => (
                                      <tr key={mv.value}>
                                        <td><code>{mv.value}</code></td>
                                        <td>
                                          <del className="ver-old">
                                            {mv.old_label_cn} ({mv.old_enabled ? '启用' : '停用'})
                                          </del>
                                        </td>
                                        <td>
                                          <ins className="ver-new">
                                            {mv.new_label_cn} ({mv.new_enabled ? '启用' : '停用'})
                                          </ins>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        </details>
                      )
                    })}
                  </div>
                ) : (
                  <div className="empty">两个版本包的码表与码值定义完全一致，或均未包含独立码表资产。</div>
                )
              )}

              {subTab === 'rules' && (
                ruleChangedCount > 0 ? (
                  <div className="ver-rule-diff-stack">
                    {ruleDetails.modified?.length > 0 && (
                      <div className="table-wrap">
                        <h3>修订规则条文 ({ruleDetails.modified.length})</h3>
                        <table>
                          <thead>
                            <tr>
                              <th>报表 / 规则号</th>
                              <th>旧版校验条文</th>
                              <th>新版校验条文</th>
                            </tr>
                          </thead>
                          <tbody>
                            {ruleDetails.modified.map((item, idx) => (
                              <tr key={`${item.table_code}-${item.rule_code}-${idx}`}>
                                <td>
                                  <strong>{item.table_code}</strong>
                                  <small>{item.rule_code} · {item.rule_name}</small>
                                </td>
                                <td><del className="ver-old">{item.old?.rule_text}</del></td>
                                <td><ins className="ver-new">{item.new?.rule_text}</ins></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    {ruleDetails.added?.length > 0 && (
                      <div className="table-wrap">
                        <h3>新增核查规则 ({ruleDetails.added.length})</h3>
                        <table>
                          <thead>
                            <tr>
                              <th>报表 / 规则号</th>
                              <th>涉及字段</th>
                              <th>新增校验条文</th>
                            </tr>
                          </thead>
                          <tbody>
                            {ruleDetails.added.map((r) => (
                              <tr key={r.id || r.rule_hash}>
                                <td>
                                  <strong>{r.table_code}</strong>
                                  <small>{r.rule_code || '补充规则'} · {r.severity}</small>
                                </td>
                                <td>{(r.target_fields || []).join('、') || '表级'}</td>
                                <td><ins className="ver-new">{r.rule_text}</ins></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    {ruleDetails.removed?.length > 0 && (
                      <div className="table-wrap">
                        <h3>移除核查规则 ({ruleDetails.removed.length})</h3>
                        <table>
                          <thead>
                            <tr>
                              <th>报表 / 规则号</th>
                              <th>涉及字段</th>
                              <th>原校验条文</th>
                            </tr>
                          </thead>
                          <tbody>
                            {ruleDetails.removed.map((r) => (
                              <tr key={r.id || r.rule_hash}>
                                <td>
                                  <strong>{r.table_code}</strong>
                                  <small>{r.rule_code || '补充规则'} · {r.severity}</small>
                                </td>
                                <td>{(r.target_fields || []).join('、') || '表级'}</td>
                                <td><del className="ver-old">{r.rule_text}</del></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="empty">两个版本包的补充核查规则集合没有差异。</div>
                )
              )}
            </>
          ) : null}
        </>
      )}
    </section>
  )
}
