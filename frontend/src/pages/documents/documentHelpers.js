export const IMPORT_STAGES = [
  ['校验并保存文件', '检查格式、大小和文件指纹'],
  ['检查整份文件结构', 'AI 判断文档结构与切块方式'],
  ['程序执行初步切块', '按文件类型生成初始内容块'],
  ['提取报表字段和码表', '自动解析并复核，完成后直接进入结果'],
]

export const COLUMN_MAPPING_FIELDS = [
  ['field_cn', '字段中文名列'],
  ['field_en', '字段英文名列'],
  ['field_definition', '字段定义列'],
  ['check_text_raw', '字段校验列'],
  ['data_type', '数据类型列'],
  ['length', '字段长度列'],
]

export async function fileSha256(file) {
  if (typeof window === 'undefined' || !window.crypto?.subtle) {
    return null
  }
  const content = await file.arrayBuffer()
  const digest = await window.crypto.subtle.digest('SHA-256', content)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function updateImportProgress(current, document) {
  if (!current || !document) return current
  if (document.status === 'OVERVIEW_REQUIRED') return { ...current, stage: 1, status: 'WORKING' }
  if (['OVERVIEW_REVIEWED', 'OVERVIEW_NEEDS_REVIEW'].includes(document.status)) return { ...current, stage: 2, status: 'WORKING' }
  if (document.status === 'OVERVIEW_FAILED') return { ...current, stage: 1, status: 'FAILED' }
  if (document.status === 'BLOCK_EXTRACTION_FAILED') return { ...current, stage: 2, status: 'FAILED' }
  if (document.status === 'EXTRACTING') return { ...current, stage: 3, status: 'WORKING' }
  if (document.status === 'EXTRACTED') return { ...current, stage: 4, status: 'DONE' }
  if (['EXTRACTION_PARTIAL', 'EXTRACTION_FAILED'].includes(document.status)) return { ...current, stage: 3, status: 'FAILED' }
  if (document.status === 'UPLOADED' || document.status === 'STRUCTURE_REVIEWED') {
    if (current.awaitReextract) return { ...current, stage: 2, status: 'WORKING' }
    return current.autoParse
      ? { ...current, stage: 3, status: 'WORKING' }
      : { ...current, stage: 4, status: 'DONE' }
  }
  return current
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}m${String(rest).padStart(2, '0')}s`
}
