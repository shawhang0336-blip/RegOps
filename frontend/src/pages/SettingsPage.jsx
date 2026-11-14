import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'

export default function SettingsPage() {
  const [settings, setSettings] = useState(null)
  const [budget, setBudget] = useState({})
  const [results, setResults] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() {
    const body = await v2('/settings')
    setSettings(body)
    setBudget(body.execution_budget)
  }
  useEffect(() => { load().catch((err) => setError(err.message)) }, [])

  async function test(kind) {
    setError('')
    try {
      const result = await v2(`/settings/test/${kind}`, { method: 'POST', body: JSON.stringify({}) })
      setResults({ ...results, [kind]: result })
    } catch (err) {
      setResults({ ...results, [kind]: { ok: false, error: err.message } })
    }
  }

  async function saveBudget() {
    try {
      await v2('/settings/execution_budget', { method: 'PUT', body: JSON.stringify({ value: Object.fromEntries(Object.entries(budget).map(([key, value]) => [key, Number(value)])) }) })
      setNotice('执行预算已保存，后续生成计划按新预算校验。')
    } catch (err) { setError(err.message) }
  }

  return <>
    <PageHeader title="系统设置" description="数据库、模型与执行预算。密钥仅由服务端持有，页面只显示配置状态。" />
    <StatusMessage error={error} notice={notice} onClose={() => setNotice('')} />
    <section className="settings-grid">
      {['mysql', 'oracle', 'llm'].map((kind) => <div className="panel setting-card" key={kind}>
        <div className="panel-heading"><h2>{{ mysql: 'MySQL 元数据库', oracle: 'Oracle 沙箱', llm: 'GLM 模型服务' }[kind]}</h2></div>
        <p className="setting-state">{kind === 'llm' ? (settings?.llm.credential_configured ? '密钥已配置' : '未配置密钥') : (settings?.[kind]?.configured ? '已配置' : '未配置')}</p>
        {kind === 'llm' && <code>{settings?.llm.model}</code>}
        <button className="button secondary" onClick={() => test(kind)}>测试连接</button>
        {results[kind] && <pre className={results[kind].ok ? 'connection-result success' : 'connection-result error'}>{JSON.stringify(results[kind], null, 2)}</pre>}
      </div>)}
    </section>
    <section className="panel budget-panel">
      <div className="panel-heading"><div><h2>执行预算</h2><p>AI 可以决定规则和数据数量，但不能突破这些确定性上限。</p></div><button className="button primary" onClick={saveBudget}>保存预算</button></div>
      <div className="budget-fields">{Object.entries(budget).map(([key, value]) => <label key={key}><span>{{ max_rules_per_table: '单表规则上限', max_cases_per_table: '单表用例上限', max_retries: '最大回边次数', max_llm_concurrency: '模型并发数' }[key] || key}</span><input type="number" min="1" value={value} onChange={(event) => setBudget({ ...budget, [key]: event.target.value })} /></label>)}</div>
    </section>
  </>
}
