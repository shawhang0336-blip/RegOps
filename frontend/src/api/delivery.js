/** 运行批次控制、审批与审计交付 API（对应后端 `api/delivery/`）。 */
import { api } from './client.js'

export function listRuns() {
  return api('/api/runs')
}

export function getRun(runId) {
  return api(`/api/runs/${runId}`)
}

export function getRunValidationDetail(runId) {
  return api(`/api/runs/${runId}/validation-detail`)
}

export function getRunBusEvents(runId) {
  return api(`/api/runs/${runId}/bus-events`)
}

export function getRunReplay(runId) {
  return api(`/api/runs/${runId}/replay`)
}

export function diffRuns(baseRunId, targetRunId) {
  const qs = new URLSearchParams({ base_run_id: baseRunId, target_run_id: targetRunId }).toString()
  return api(`/api/runs/diff?${qs}`)
}

export function submitRunApprovals(runId, decisions, provider = 'live') {
  return api(`/api/runs/${runId}/approvals`, {
    method: 'POST',
    body: JSON.stringify({ provider, decisions }),
  })
}

export function confirmRunTestResults(runId) {
  return api(`/api/runs/${runId}/test-confirmation`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function retryRun(runId) {
  return api(`/api/runs/${runId}/retry`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function reauditRun(runId) {
  return api(`/api/runs/${runId}/reaudit`, {
    method: 'POST',
  })
}

export function cancelRun(runId, reason = '由操作员在控制台主动中止') {
  return api(`/api/runs/${runId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function deleteRun(runId) {
  return api(`/api/runs/${runId}`, {
    method: 'DELETE',
    body: JSON.stringify({}),
  })
}

export function getHistoryCleanupPreview() {
  return api('/api/runs/history/cleanup-preview')
}

export function cleanupHistoryRuns() {
  return api('/api/runs/history/cleanup', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function fetchRunPreview(previewUrl) {
  return api(previewUrl)
}
