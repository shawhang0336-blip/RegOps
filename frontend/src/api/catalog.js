/** 监管资产目录 API：文档 / 表字段元数据 / 码表（对应后端 `api/catalog/`）。 */
import { api, downloadFile } from './client.js'

function querySuffix(entries) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(entries || {})) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

// ── 文档接入与切块 (/api/documents/*) ─────────────────────────────────────────

export function listDocuments() {
  return api('/api/documents')
}

export function getDocument(documentId) {
  return api(`/api/documents/${documentId}`)
}

export function uploadDocument(file) {
  const form = new FormData()
  form.append('file', file)
  return api('/api/documents', { method: 'POST', body: form })
}

export function deleteDocument(documentId) {
  return api(`/api/documents/${documentId}`, { method: 'DELETE' })
}

export function getDocumentProgress(documentId) {
  return api(`/api/documents/${documentId}/progress`)
}

export function cancelDocument(documentId, reason = '由操作员在文档工作台主动中止') {
  return api(`/api/documents/${documentId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function prepareDocument(documentId) {
  return api(`/api/documents/${documentId}/prepare`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function reextractDocument(documentId) {
  return api(`/api/documents/${documentId}/reextract`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function analyzeDocumentStructure(documentId) {
  return api(`/api/documents/${documentId}/analyze-structure`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function getDocumentArchitecture(documentId) {
  return api(`/api/documents/${documentId}/architecture`)
}

export function getRawBlock(documentId, blockId) {
  return api(`/api/documents/${documentId}/blocks/${blockId}`)
}

export function adjustBlockStructure(documentId, blockId, changes) {
  return api(`/api/documents/${documentId}/blocks/${blockId}/structure`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function extractDocumentParallel(documentId, blockIds, targets = ['metadata', 'code_sets']) {
  return api(`/api/documents/${documentId}/extract-parallel`, {
    method: 'POST',
    body: JSON.stringify({ block_ids: blockIds, targets }),
  })
}

export function diffDocuments(baseId, targetId) {
  return api(`/api/documents/diff${querySuffix({ base_id: baseId, target_id: targetId })}`)
}

// ── 多文件规范包 (/api/packages/*) ────────────────────────────────────────────

export function listPackages() {
  return api('/api/packages')
}

export function createPackage(payload) {
  return api('/api/packages', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getPackage(packageId) {
  return api(`/api/packages/${packageId}`)
}

export function deletePackage(packageId) {
  return api(`/api/packages/${packageId}`, { method: 'DELETE' })
}

export function uploadPackageFiles(packageId, files, { docRole = 'AUTO', prepare = true } = {}) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  form.append('doc_role', docRole)
  form.append('prepare', prepare ? 'true' : 'false')
  return api(`/api/packages/${packageId}/files`, { method: 'POST', body: form })
}

export function updatePackageFileRole(packageId, documentId, docRole) {
  return api(`/api/packages/${packageId}/files/${documentId}`, {
    method: 'PATCH',
    body: JSON.stringify({ doc_role: docRole }),
  })
}

export function extractPackage(packageId, payload = {}) {
  return api(`/api/packages/${packageId}/extract`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function reconcilePackage(packageId) {
  return api(`/api/packages/${packageId}/reconcile`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function listPackageRuleEvidences(packageId, { tableCode } = {}) {
  return api(`/api/packages/${packageId}/rule-evidences${querySuffix({ table_code: tableCode })}`)
}

export function diffPackages(baseId, targetId) {
  return api(`/api/packages/diff${querySuffix({ base_id: baseId, target_id: targetId })}`)
}

// ── 报表与字段元数据 (/api/metadata/*) ───────────────────────────────────────

export function listMetadataTables({ documentId, packageId, status } = {}) {
  return api(`/api/metadata/tables${querySuffix({ document_id: documentId, package_id: packageId, status })}`)
}

export function getMetadataTable(tableId) {
  return api(`/api/metadata/tables/${tableId}`)
}

export function getMetadataReadiness({ documentId, packageId } = {}) {
  return api(`/api/metadata/readiness${querySuffix({ document_id: documentId, package_id: packageId })}`)
}

export function deleteMetadataTable(tableId, payload) {
  return api(`/api/metadata/tables/${tableId}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  })
}

export function editMetadataField(tableId, fieldId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields/${fieldId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteMetadataField(tableId, fieldId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields/${fieldId}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  })
}

export function addMetadataField(tableId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function downloadFieldTypeTemplate({ documentId, packageId } = {}) {
  return downloadFile(
    `/api/metadata/types/template${querySuffix({ document_id: documentId, package_id: packageId })}`,
    '字段类型修正模板.csv',
  )
}

export function importFieldTypes(formData) {
  return api('/api/metadata/types/import', { method: 'POST', body: formData })
}

// ── 公共与内联码表 (/api/code-sets/*) ────────────────────────────────────────

export function listCodeSets({ documentId, packageId, includePlaceholders } = {}) {
  return api(`/api/code-sets${querySuffix({
    document_id: documentId,
    package_id: packageId,
    include_placeholders: includePlaceholders ? 'true' : undefined,
  })}`)
}

export function getCodeSet(codeSetId) {
  return api(`/api/code-sets/${codeSetId}`)
}

export function createCodeSet(payload) {
  return api('/api/code-sets', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function deleteCodeSet(codeSetId) {
  return api(`/api/code-sets/${codeSetId}`, { method: 'DELETE' })
}

export function uploadCodeSetDocument(formData) {
  return api('/api/code-sets/upload-file', { method: 'POST', body: formData })
}

export function listUnresolvedCodeReferences({ documentId, packageId } = {}) {
  return api(`/api/code-sets/references/unresolved${querySuffix({ document_id: documentId, package_id: packageId })}`)
}

export function relinkCodeReferences() {
  return api('/api/code-sets/references/relink', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function getCodeSetReadiness({ documentId, packageId, referencedOnly } = {}) {
  return api(`/api/code-sets/readiness${querySuffix({
    document_id: documentId,
    package_id: packageId,
    referenced_only: referencedOnly ? 'true' : undefined,
  })}`)
}

export function addCodeValue(codeSetId, payload) {
  return api(`/api/code-sets/${codeSetId}/values`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function editCodeValue(codeSetId, valueId, payload) {
  return api(`/api/code-sets/${codeSetId}/values/${valueId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function importCodeValues(codeSetId, formData) {
  return api(`/api/code-sets/${codeSetId}/import`, { method: 'POST', body: formData })
}
