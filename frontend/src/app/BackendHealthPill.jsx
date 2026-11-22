import { useCallback, useEffect, useRef, useState } from 'react'
import { getHealth } from '../api/platform.js'

export default function BackendHealthPill() {
  const [health, setHealth] = useState({ state: 'checking', info: null, checkedAt: null })
  const [probing, setProbing] = useState(false)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const check = useCallback(async (manual = false) => {
    if (manual && mountedRef.current) setProbing(true)
    const started = Date.now()
    try {
      const data = await getHealth()
      if (manual) {
        const elapsed = Date.now() - started
        if (elapsed < 260) await new Promise((resolve) => window.setTimeout(resolve, 260 - elapsed))
      }
      if (mountedRef.current) setHealth({ state: 'online', info: data, checkedAt: new Date() })
    } catch {
      if (mountedRef.current) setHealth({ state: 'offline', info: null, checkedAt: new Date() })
    } finally {
      if (manual && mountedRef.current) setProbing(false)
    }
  }, [])

  useEffect(() => {
    check(false)
    const timer = window.setInterval(() => check(false), 30000)
    return () => window.clearInterval(timer)
  }, [check])

  const effectiveState = probing ? 'checking' : health.state
  const label = effectiveState === 'online'
    ? '已连接'
    : effectiveState === 'offline'
      ? '连接异常'
      : '检测中...'

  const checkedText = health.checkedAt
    ? ` · 检测于 ${health.checkedAt.toLocaleTimeString('zh-CN', { hour12: false })}`
    : ''
  const title = effectiveState === 'online'
    ? `服务正常 · 数据库：${health.info?.database || 'ok'} · 模型：${health.info?.llm_provider || 'ok'}${checkedText}（点击重新检测）`
    : effectiveState === 'offline'
      ? '无法连接后端服务，请确认 uvicorn 已启动（点击重试）'
      : '正在检测后端服务状态...'

  return (
    <button
      type="button"
      className={`workbench-status status-${effectiveState}`}
      title={title}
      disabled={probing}
      onClick={() => check(true)}
    >
      <span className="workbench-status-dot" />
      <span className="workbench-status-text">{label}</span>
    </button>
  )
}
