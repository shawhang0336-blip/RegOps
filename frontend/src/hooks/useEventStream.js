import { useEffect, useRef } from 'react'
import { api } from '../api.js'

const DEFAULT_BUS_TOPICS = [
  'run.created',
  'run.resumed',
  'run.interrupted',
  'run.completed',
  'run.failed',
  'node.enter',
  'node.exit',
  'node.redirect',
  'node.error',
  'wave.fanout',
  'wave.barrier',
  'tool.rejected',
  'event.appended',
  'document.progress',
]

/**
 * 通用轮询 Hook：始终调用最新 callback，无陈旧闭包，自动清理定时器。
 */
export function usePolling(callback, { enabled = true, intervalMs = 5000 } = {}) {
  const cbRef = useRef(callback)
  cbRef.current = callback

  useEffect(() => {
    if (!enabled || !intervalMs) return undefined
    const timer = window.setInterval(() => {
      cbRef.current?.()
    }, intervalMs)
    return () => window.clearInterval(timer)
  }, [enabled, intervalMs])
}

/**
 * 订阅后端 ProdAgent Bus 的 SSE 实时事件流（`/api/events/stream`），并在连接异常时自动退化为低频轮询兜底。
 */
export function useEventStream({
  enabled = true,
  runId = '',
  documentId = '',
  topics = null,
  onEvent,
  debounceMs = 250,
  fallbackIntervalMs = 5000,
  onFallbackTick,
} = {}) {
  const onEventRef = useRef(onEvent)
  const onFallbackRef = useRef(onFallbackTick)
  onEventRef.current = onEvent
  onFallbackRef.current = onFallbackTick

  const topicsKey = Array.isArray(topics) ? topics.join(',') : (topics || '')

  useEffect(() => {
    if (!enabled) return undefined

    let source = null
    let closed = false
    let debounceTimer = null
    let fallbackTimer = null
    let currentFallbackMs = null
    let sseHealthy = false

    const startFallback = (ms) => {
      const normalized = ms || 0
      if (currentFallbackMs === normalized && (fallbackTimer || normalized === 0)) return
      if (fallbackTimer) {
        window.clearInterval(fallbackTimer)
        fallbackTimer = null
      }
      currentFallbackMs = normalized
      if (!normalized || !onFallbackRef.current) return
      fallbackTimer = window.setInterval(() => {
        if (!closed) onFallbackRef.current?.()
      }, normalized)
    }

    const dispatch = (payload, topic) => {
      if (!onEventRef.current) return
      if (!debounceMs) {
        onEventRef.current(payload, topic)
        return
      }
      if (debounceTimer) window.clearTimeout(debounceTimer)
      debounceTimer = window.setTimeout(() => {
        if (!closed) onEventRef.current?.(payload, topic)
      }, debounceMs)
    }

    if (typeof window !== 'undefined' && typeof window.EventSource !== 'undefined') {
      const params = new URLSearchParams()
      if (runId) params.set('run_id', runId)
      if (documentId) params.set('document_id', documentId)
      if (topicsKey) params.set('topics', topicsKey)
      const query = params.toString()
      source = new window.EventSource(`/api/events/stream${query ? `?${query}` : ''}`)

      const listenTopics = topicsKey
        ? topicsKey.split(',').map((t) => t.trim()).filter(Boolean)
        : DEFAULT_BUS_TOPICS

      const handleMessage = (event) => {
        sseHealthy = true
        let parsed = null
        try {
          parsed = JSON.parse(event.data)
        } catch {
          parsed = { raw: event.data }
        }
        dispatch(parsed, event.type)
      }

      listenTopics.forEach((topic) => source.addEventListener(topic, handleMessage))
      source.onopen = () => {
        sseHealthy = true
        // SSE 建连正常时，降频为 3 倍兜底心跳，省去高频轮询
        startFallback(fallbackIntervalMs ? Math.max(fallbackIntervalMs * 3, 15000) : 0)
      }
      source.onerror = () => {
        if (sseHealthy) sseHealthy = false
        // SSE 暂不可用时自动恢复常规轮询频率
        startFallback(fallbackIntervalMs)
      }
    }

    // 初始先挂兜底轮询，一旦 SSE onopen 会自动降频
    startFallback(fallbackIntervalMs)

    return () => {
      closed = true
      if (debounceTimer) window.clearTimeout(debounceTimer)
      if (fallbackTimer) window.clearInterval(fallbackTimer)
      if (source) source.close()
    }
  }, [enabled, runId, documentId, topicsKey, debounceMs, fallbackIntervalMs])
}

/**
 * 监听指定文档导入/解析进度直至完成：优先由 SSE `document.progress` 触发立即拉取，
 * 同时保留低频轮询兜底，保证不丢任何终态。
 */
export function watchDocumentProgress(
  documentId,
  onUpdate,
  { interval = 1500, timeout = 20 * 60 * 1000, signal } = {},
) {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve(null)
      return
    }
    const startedAt = Date.now()
    let finished = false
    let fetching = false
    let latest = null
    let source = null
    let pollTimer = null
    let timeoutTimer = null

    const onAbort = () => cleanup(latest)

    const cleanup = (result) => {
      if (finished) return
      finished = true
      if (pollTimer) window.clearInterval(pollTimer)
      if (timeoutTimer) window.clearTimeout(timeoutTimer)
      if (source) source.close()
      if (signal) signal.removeEventListener('abort', onAbort)
      resolve(result)
    }

    if (signal) {
      signal.addEventListener('abort', onAbort, { once: true })
    }

    const pull = async () => {
      if (finished || fetching || signal?.aborted) return
      if (Date.now() - startedAt >= timeout) {
        cleanup(latest)
        return
      }
      fetching = true
      try {
        const next = await api(`/api/documents/${documentId}/progress`).catch(() => null)
        if (next && !finished && !signal?.aborted) {
          latest = next
          onUpdate?.(next)
          if (!next.processing) {
            cleanup(next)
          }
        }
      } finally {
        fetching = false
      }
    }

    if (typeof window !== 'undefined' && typeof window.EventSource !== 'undefined') {
      const qs = new URLSearchParams({ document_id: documentId, topics: 'document.progress' })
      source = new window.EventSource(`/api/events/stream?${qs.toString()}`)
      source.addEventListener('document.progress', () => {
        pull()
      })
    }

    pollTimer = window.setInterval(pull, interval)
    timeoutTimer = window.setTimeout(() => cleanup(latest), timeout)
    pull()
  })
}
