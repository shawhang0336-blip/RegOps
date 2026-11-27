const ACTIVE_DOC_KEY = 'regops_active_document_id'
const ACTIVE_PKG_KEY = 'regops_active_package_id'
const ACTIVE_REG_KEY = 'regops_active_regulator'

export const REGULATOR_PROJECTS = [
  {
    code: 'SAFE',
    label: 'SAFE 外汇局',
    shortLabel: 'SAFE',
    paradigm: 'RELATIONAL',
    hint: '金融机构外汇业务数据采集规范 / 对外金融资产负债及交易统计',
  },
  {
    code: 'EAST',
    label: 'EAST 5.0',
    shortLabel: 'EAST',
    paradigm: 'RELATIONAL',
    hint: '银行业金融机构监管数据标准化规范与检核规则',
  },
  {
    code: 'YBT',
    label: '一表通',
    shortLabel: '一表通',
    paradigm: 'RELATIONAL',
    hint: '可信区数据采集标准、业务口径与交叉检核规则',
  },
  {
    code: '1104',
    label: '1104 非现场',
    shortLabel: '1104',
    paradigm: 'MATRIX',
    hint: '二维指标矩阵表样（G/S 系列）与表内/表间公式核查体系',
  },
  {
    code: 'PBOC',
    label: '人民银行',
    shortLabel: '人行',
    paradigm: 'RELATIONAL',
    hint: '金融基础数据 / 大集中全科目 / 资管产品 / 反洗钱报送',
  },
  {
    code: 'CSRC',
    label: '证监会',
    shortLabel: '证监会',
    paradigm: 'MATRIX',
    hint: '证券公司风险控制指标计算标准及计算报表体系',
  },
]

export function regulatorMeta(code) {
  const clean = String(code || '').trim()
  const upper = clean.toUpperCase()
  const hit = REGULATOR_PROJECTS.find((item) => item.code === upper || item.code === clean)
  if (hit) return hit
  if (!clean || upper === 'ALL') {
    return {
      code: 'ALL',
      label: '全部监管项目',
      shortLabel: '全部',
      paradigm: 'ALL',
      hint: '查看全部监管体系下的规范包与历史报表',
    }
  }
  return {
    code: clean,
    label: clean === 'CUSTOM' ? '其他监管项目' : clean,
    shortLabel: clean === 'CUSTOM' ? '其他' : clean,
    paradigm: 'RELATIONAL',
    hint: '自定义监管或地方派出机构特色报送项目',
  }
}

export function getSavedDocumentId() {
  try {
    return window.localStorage.getItem(ACTIVE_DOC_KEY) || ''
  } catch {
    return ''
  }
}

export function setSavedDocumentId(documentId) {
  try {
    if (documentId) window.localStorage.setItem(ACTIVE_DOC_KEY, documentId)
    else window.localStorage.removeItem(ACTIVE_DOC_KEY)
  } catch {
    // ignore storage errors
  }
}

export function getSavedPackageId() {
  try {
    return window.localStorage.getItem(ACTIVE_PKG_KEY) || ''
  } catch {
    return ''
  }
}

export function setSavedPackageId(packageId) {
  try {
    if (packageId) window.localStorage.setItem(ACTIVE_PKG_KEY, packageId)
    else window.localStorage.removeItem(ACTIVE_PKG_KEY)
  } catch {
    // ignore storage errors
  }
}

export function getSavedRegulator() {
  try {
    return window.localStorage.getItem(ACTIVE_REG_KEY) || ''
  } catch {
    return ''
  }
}

export function setSavedRegulator(regulator) {
  try {
    if (regulator) window.localStorage.setItem(ACTIVE_REG_KEY, regulator)
    else window.localStorage.removeItem(ACTIVE_REG_KEY)
  } catch {
    // ignore storage errors
  }
}
