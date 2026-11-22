import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ApiError, api, downloadFile, formatApiErrorDetail } from '../src/api/client.js'
import { formatTime, parseBackendDate } from '../src/utils/dateTime.js'
import { BUDGET_META, validateBudgetInput } from '../src/utils/budget.js'
import { fileSha256, updateImportProgress } from '../src/features/catalog/documents/documentHelpers.js'
import { tableGate, typeBasisMeta } from '../src/features/catalog/metadata/metadataHelpers.js'
import { formatParams, groupStatusMeta, runLabel, sortRules } from '../src/features/generation/artifactHelpers.js'
import { watchDocumentProgress } from '../src/hooks/useEventStream.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SRC_DIR = path.resolve(__dirname, '../src')

function walkFiles(dir, exts) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walkFiles(full, exts))
    else if (!exts || exts.includes(path.extname(entry.name))) out.push(full)
  }
  return out
}

test('ApiError formats FastAPI 422 array detail into readable Chinese instead of raw JSON', () => {
  const err = new ApiError(422, [
    { loc: ['body', 'field', 'data_type'], msg: "Input should be 'VARCHAR' or 'NUMBER'", type: 'literal_error' },
    { loc: ['body', 'field', 'length'], msg: 'Input should be greater than 0', type: 'greater_than' },
  ])
  assert.equal(err.status, 422)
  assert.match(err.message, /field\.data_type: Input should be 'VARCHAR' or 'NUMBER'/)
  assert.match(err.message, /field\.length: Input should be greater than 0/)
  assert.doesNotMatch(err.message, /^\[/)
  assert.equal(formatApiErrorDetail({ message: '规则哈希已变更' }, 409), '规则哈希已变更')
  assert.equal(formatApiErrorDetail(null, 502), '请求失败（502）')
})

test('api() omits Content-Type on GET without body and handles non-JSON 502 HTML cleanly', async () => {
  const origFetch = globalThis.fetch
  let capturedInit = null
  try {
    globalThis.fetch = async (_url, init) => {
      capturedInit = init
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ ok: true }),
      }
    }
    const res = await api('/api/health')
    assert.deepEqual(res, { ok: true })
    assert.equal(capturedInit.headers['Content-Type'], undefined)

    globalThis.fetch = async () => ({
      ok: false,
      status: 502,
      text: async () => '<html><body>502 Bad Gateway</body></html>',
    })
    await assert.rejects(() => api('/api/health'), (err) => {
      assert.equal(err.status, 502)
      assert.equal(err.message, '请求失败（502）')
      return true
    })
  } finally {
    globalThis.fetch = origFetch
  }
})

test('downloadFile surfaces backend 409 tamper-gate error instead of navigating to raw JSON', async () => {
  const origFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => ({
      ok: false,
      status: 409,
      json: async () => ({ detail: '规则在审计通过后被修改，必须重新审计后才能打包下载' }),
    })
    await assert.rejects(
      () => downloadFile('/api/generation/runs/abc/production-bundle', 'production_abc.zip'),
      (err) => {
        assert.equal(err.status, 409)
        assert.match(err.message, /规则在审计通过后被修改/)
        return true
      },
    )
  } finally {
    globalThis.fetch = origFetch
  }
})

test('parseBackendDate treats naive UTC ISO strings as UTC (fixes 8-hour Beijing time drift)', () => {
  const naive = parseBackendDate('2026-11-22T06:50:23.123456')
  const explicitUtc = parseBackendDate('2026-11-22T06:50:23.123456Z')
  assert.ok(naive instanceof Date)
  assert.equal(naive.getTime(), explicitUtc.getTime())

  const formattedUtc8 = formatTime('2026-11-22T06:50:23', { timeZone: 'Asia/Shanghai', hour12: false })
  assert.match(formattedUtc8, /14:50:23/)

  // Explicit offset must be preserved untouched
  const withOffset = parseBackendDate('2026-11-22T14:50:23+08:00')
  assert.equal(withOffset.getTime(), explicitUtc.getTime() - 123)
  assert.equal(formatTime(''), '—')
})

test('validateBudgetInput blocks empty string, float, bool, and out-of-bounds values', () => {
  // Empty string must NOT silently convert to 0 (which disables BudgetGuard!)
  assert.equal(validateBudgetInput({ max_llm_calls: '' }).ok, false)
  assert.equal(validateBudgetInput({ max_llm_concurrency: 0 }).ok, false)
  assert.equal(validateBudgetInput({ max_llm_concurrency: 99 }).ok, false)
  assert.equal(validateBudgetInput({ max_retries: -1 }).ok, false)
  assert.equal(validateBudgetInput({ max_retries: 2.5 }).ok, false)
  assert.equal(validateBudgetInput({ max_rules_per_table: true }).ok, false)

  const ok = validateBudgetInput({
    max_rules_per_table: '50',
    max_cases_per_table: '1000',
    max_retries: '3',
    max_llm_concurrency: '4',
    max_llm_calls: '120',
    max_total_tokens: '1500000',
    max_elapsed_seconds: '900',
  })
  assert.equal(ok.ok, true)
  assert.equal(ok.value.max_llm_concurrency, 4)
  assert.equal(Object.keys(BUDGET_META).length, 7)
})

test('fileSha256 safely returns null on non-HTTPS intranet where crypto.subtle is missing', async () => {
  const origWindow = globalThis.window
  try {
    globalThis.window = {}
    const digest = await fileSha256({ arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer })
    assert.equal(digest, null)
  } finally {
    globalThis.window = origWindow
  }
})

test('watchDocumentProgress cleans up EventSource and timers immediately on AbortSignal', async () => {
  const origWindow = globalThis.window
  const origFetch = globalThis.fetch
  let esClosed = false
  let intervalCleared = false
  try {
    class MockEventSource {
      constructor() {
        this.listeners = {}
      }
      addEventListener(type, fn) {
        this.listeners[type] = fn
      }
      close() {
        esClosed = true
      }
    }
    globalThis.window = {
      EventSource: MockEventSource,
      setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (id) => {
        intervalCleared = true
        clearInterval(id)
      },
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (id) => clearTimeout(id),
    }
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ processing: true, status: 'WORKING', stage: 2 }),
    })
    const controller = new AbortController()
    const updates = []
    const promise = watchDocumentProgress('doc-1', (snap) => updates.push(snap), {
      interval: 5000,
      timeout: 60000,
      signal: controller.signal,
    })
    controller.abort()
    await promise
    assert.equal(esClosed, true)
    assert.equal(intervalCleared, true)
  } finally {
    globalThis.window = origWindow
    globalThis.fetch = origFetch
  }
})

test('helper contracts remain deterministic across document/metadata/artifact helpers', () => {
  const prog = updateImportProgress({ stage: 1, status: 'WORKING', autoParse: true }, { status: 'EXTRACTED' })
  assert.equal(prog.status, 'DONE')
  assert.equal(prog.stage, 4)

  const blocked = tableGate({ status: 'NEEDS_REVIEW', review_blocking_count: 2 }, { ddl: { ready: true }, rule_materials: { ready: true }, code_sets: { defects: [] } })
  assert.equal(blocked.key, 'blocked')
  assert.equal(typeBasisMeta('MANUAL').label, '人工确认')

  assert.equal(runLabel({ generated: true, run_status: 'WAITING_TEST_APPROVAL' }), '待确认测试')
  assert.deepEqual(groupStatusMeta('WAITING_APPROVAL'), ['amber', '待确认规则'])
  assert.match(formatParams({ values: ['A', 'B', 'C', 'D'] }, true), /等 4 项/)
  const sorted = sortRules([
    { operator: 'REGEXP', field: 'B' },
    { operator: 'REQUIRED', field: 'Z' },
    { operator: 'REQUIRED', field: 'A' },
  ])
  assert.deepEqual(sorted.map((r) => `${r.operator}:${r.field}`), ['REQUIRED:A', 'REQUIRED:Z', 'REGEXP:B'])
})

test('architecture guard: clean src/ layout aligned 1:1 with backend 4 domains', () => {
  const rootEntries = fs.readdirSync(SRC_DIR).filter((name) => !name.startsWith('.')).sort()
  assert.deepEqual(rootEntries, [
    'api',
    'app',
    'components',
    'context',
    'features',
    'hooks',
    'main.jsx',
    'styles',
    'utils',
  ])

  const apiFiles = fs.readdirSync(path.join(SRC_DIR, 'api')).sort()
  assert.deepEqual(apiFiles, ['catalog.js', 'client.js', 'delivery.js', 'generation.js', 'platform.js'])

  const featureDomains = fs.readdirSync(path.join(SRC_DIR, 'features')).sort()
  assert.deepEqual(featureDomains, ['catalog', 'delivery', 'generation', 'platform'])
})

test('architecture guard: all /api/* paths centralized in src/api/ and strict one-way layer imports', () => {
  const jsFiles = walkFiles(SRC_DIR, ['.js', '.jsx'])
  const rawApiViolations = []
  const layerViolations = []

  const importPattern = /(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"](\.[^'"]+)['"]|import\(\s*['"](\.[^'"]+)['"]\s*\)/g

  for (const file of jsFiles) {
    const rel = path.relative(SRC_DIR, file).replaceAll(path.sep, '/')
    const topDir = rel.includes('/') ? rel.split('/')[0] : 'root'
    const text = fs.readFileSync(file, 'utf-8')

    // 1. 严禁 src/api/ 以外的文件硬编码 /api/ 路径
    if (topDir !== 'api') {
      const hits = text.match(/['"`]\/api\/[^'"`]+['"`]/g)
      if (hits) rawApiViolations.push(`${rel}: ${hits.join(', ')}`)
    }

    // 2. 严格单向依赖校验：底座层严禁反向依赖 features/ 或 app/
    for (const m of text.matchAll(importPattern)) {
      const spec = m[1] || m[2]
      const target = path.relative(SRC_DIR, path.resolve(path.dirname(file), spec)).replaceAll(path.sep, '/')
      const targetTop = target.includes('/') ? target.split('/')[0] : 'root'

      if (['api', 'components', 'context', 'hooks', 'utils'].includes(topDir) && ['features', 'app'].includes(targetTop)) {
        layerViolations.push(`${rel} -> ${target}`)
      }
      if (topDir === 'utils' && ['api', 'components', 'context', 'hooks'].includes(targetTop)) {
        layerViolations.push(`${rel} -> ${target}`)
      }
      if (topDir === 'api' && ['components', 'context', 'hooks', 'utils'].includes(targetTop)) {
        layerViolations.push(`${rel} -> ${target}`)
      }
    }
  }

  assert.deepEqual(rawApiViolations, [], `禁止在 src/api/ 外裸写 /api/* 路径：${rawApiViolations.join('; ')}`)
  assert.deepEqual(layerViolations, [], `发现反向跨层依赖：${layerViolations.join('; ')}`)
})
