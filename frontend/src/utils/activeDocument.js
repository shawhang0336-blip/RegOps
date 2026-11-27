const ACTIVE_DOC_KEY = 'regops_active_document_id'
const ACTIVE_PKG_KEY = 'regops_active_package_id'
const ACTIVE_REG_KEY = 'regops_active_regulator'
const ACTIVE_AGENCY_KEY = 'regops_active_agency'

/**
 * 商业银行监管机构目录（一级分类：按 2023 金融监管机构改革后的现行银行报送口径）。
 */
export const REGULATORY_AGENCIES = [
  {
    code: 'NFRA',
    label: '国家金融监督管理总局',
    shortLabel: '金融监管总局',
    hint: '原银保监会：一表通、EAST 现场检查、1104 非现场监管、客户风险统计与专项报表',
  },
  {
    code: 'PBOC',
    label: '中国人民银行',
    shortLabel: '人民银行',
    hint: '金融基础数据（金数）、金融统计大集中、反洗钱（AML）、二代征信与跨境人民币',
  },
  {
    code: 'SAFE',
    label: '国家外汇管理局',
    shortLabel: '外汇局',
    hint: '金融机构外汇业务数据采集、对外金融资产负债及交易统计（FAL）、国际收支申报（BOP）',
  },
  {
    code: 'OTHER',
    label: '财政部 / 登记托管 / 其他',
    shortLabel: '财政部 / 其他',
    hint: '财政部金融企业财务快报、银行业理财登记、证监会托管代销及地方派出机构特色报表',
  },
]

/**
 * 商业银行监管报表体系目录（二级分类：归属各自监管机构）。
 */
export const REGULATOR_PROJECTS = [
  // ── 国家金融监督管理总局（NFRA） ──
  {
    code: 'YBT',
    agency: 'NFRA',
    label: '一表通',
    shortLabel: '一表通',
    paradigm: 'RELATIONAL',
    hint: '金融监管总局可信区采集标准、业务口径与交叉检核规则（一表通 2.0）',
  },
  {
    code: 'EAST',
    agency: 'NFRA',
    label: 'EAST',
    shortLabel: 'EAST',
    paradigm: 'RELATIONAL',
    hint: '银行业金融机构现场检查分析系统标准化规范（如 EAST 5.0）与检核规则',
  },
  {
    code: '1104',
    agency: 'NFRA',
    label: '1104 非现场',
    shortLabel: '1104',
    paradigm: 'MATRIX',
    hint: '非现场监管报表（G/S 系列二维指标矩阵表样与表内/表间公式核查体系）',
  },
  {
    code: 'NFRA_CRS',
    agency: 'NFRA',
    label: '客户风险统计',
    shortLabel: '客户风险',
    paradigm: 'RELATIONAL',
    hint: '大额风险暴露、集团客户及法人/个人授信客户风险统计报送',
  },
  {
    code: 'NFRA_SPECIAL',
    agency: 'NFRA',
    label: '专项监管报表',
    shortLabel: '专项报表',
    paradigm: 'MATRIX',
    hint: '绿色金融、普惠金融、消费者权益保护、流动性监测等专项报表',
  },

  // ── 中国人民银行（PBOC） ──
  {
    code: 'PBOC_FBD',
    agency: 'PBOC',
    label: '金融基础数据（金数）',
    shortLabel: '人行金数',
    paradigm: 'RELATIONAL',
    hint: '人民银行金融基础数据统计制度（银发〔2019〕384号等逐笔明细采集）',
  },
  {
    code: 'PBOC_STAT',
    agency: 'PBOC',
    label: '金融统计大集中',
    shortLabel: '大集中',
    paradigm: 'MATRIX',
    hint: '全科目统计、人行 A/B/C/D 类汇总报表、利率报备及资管产品统计',
  },
  {
    code: 'PBOC_AML',
    agency: 'PBOC',
    label: '反洗钱（AML）',
    shortLabel: '反洗钱',
    paradigm: 'RELATIONAL',
    hint: '大额交易与可疑交易报告、客户身份识别及洗钱风险数据报送',
  },
  {
    code: 'PBOC_CREDIT',
    agency: 'PBOC',
    label: '二代征信',
    shortLabel: '二代征信',
    paradigm: 'RELATIONAL',
    hint: '企业及个人信用信息基础数据库（二代征信）数据报文与校验规则',
  },
  {
    code: 'PBOC_RCPMIS',
    agency: 'PBOC',
    label: '跨境人民币（RCPMIS）',
    shortLabel: '跨境人民币',
    paradigm: 'RELATIONAL',
    hint: '人民币跨境收付信息管理系统（RCPMIS）报文与业务要素核查',
  },

  // ── 国家外汇管理局（SAFE） ──
  {
    code: 'SAFE_BIZ',
    agency: 'SAFE',
    label: '外汇业务数据采集',
    shortLabel: '外汇业务采集',
    paradigm: 'RELATIONAL',
    hint: '金融机构外汇业务数据采集规范（1.3/1.4 版，含账户、资本项目、结售汇）',
  },
  {
    code: 'SAFE_FAL',
    agency: 'SAFE',
    label: '对外金融资产负债及交易（FAL）',
    shortLabel: '对外金融(FAL)',
    paradigm: 'RELATIONAL',
    hint: '对外金融资产负债及交易统计制度（如汇发〔2025〕22号）',
  },
  {
    code: 'SAFE_BOP',
    agency: 'SAFE',
    label: '国际收支申报（BOP）',
    shortLabel: '国际收支(BOP)',
    paradigm: 'RELATIONAL',
    hint: '通过银行进行国际收支统计申报与涉外收支业务要素核查',
  },

  // ── 财政部 / 登记托管 / 其他（OTHER） ──
  {
    code: 'MOF_FIN',
    agency: 'OTHER',
    label: '财政部财务快报',
    shortLabel: '财政财务快报',
    paradigm: 'MATRIX',
    hint: '财政部金融企业月度财务快报、季度及年度财务决算报表',
  },
  {
    code: 'CDDC_WM',
    agency: 'OTHER',
    label: '银行业理财登记',
    shortLabel: '理财登记',
    paradigm: 'RELATIONAL',
    hint: '全国银行业理财信息登记系统（产品发行、存续、底层资产与持仓明细）',
  },
  {
    code: 'CSRC_CUSTODY',
    agency: 'OTHER',
    label: '证监会（托管与代销）',
    shortLabel: '证监会托管',
    paradigm: 'RELATIONAL',
    hint: '商业银行证券投资基金托管、公募代销与资管业务监管报送',
  },
]

const LEGACY_CODE_ALIASES = {
  SAFE: 'SAFE_BIZ',
  PBOC: 'PBOC_FBD',
  CSRC: 'CSRC_CUSTODY',
}

export function agencyMeta(agencyCode) {
  const clean = String(agencyCode || '').trim().toUpperCase()
  const hit = REGULATORY_AGENCIES.find((item) => item.code === clean)
  if (hit) return hit
  if (!clean || clean === 'ALL') {
    return {
      code: 'ALL',
      label: '全部监管机构',
      shortLabel: '全部机构',
      hint: '查看商业银行全部监管机构下的报表规范包',
    }
  }
  return REGULATORY_AGENCIES[REGULATORY_AGENCIES.length - 1]
}

export function agencyCodeForReport(reportCode) {
  const clean = String(reportCode || '').trim()
  const upper = clean.toUpperCase()
  if (!clean || upper === 'ALL') return 'ALL'
  if (REGULATORY_AGENCIES.some((a) => a.code === upper)) return upper
  const canonical = LEGACY_CODE_ALIASES[upper] || upper
  const hit = REGULATOR_PROJECTS.find((item) => item.code === canonical || item.code === clean)
  if (hit) return hit.agency
  if (upper.startsWith('NFRA_')) return 'NFRA'
  if (upper.startsWith('PBOC_')) return 'PBOC'
  if (upper.startsWith('SAFE_')) return 'SAFE'
  return 'OTHER'
}

export function regulatorMeta(code) {
  const clean = String(code || '').trim()
  const upper = clean.toUpperCase()
  if (!clean || upper === 'ALL') {
    return {
      code: 'ALL',
      agency: 'ALL',
      agencyLabel: '全部监管机构',
      agencyShortLabel: '全部机构',
      label: '全部监管报表',
      shortLabel: '全部报表',
      paradigm: 'ALL',
      hint: '查看全部监管机构与报表体系下的规范包',
    }
  }
  const agencyHit = REGULATORY_AGENCIES.find((a) => a.code === upper)
  if (agencyHit && !LEGACY_CODE_ALIASES[upper]) {
    return {
      code: agencyHit.code,
      agency: agencyHit.code,
      agencyLabel: agencyHit.label,
      agencyShortLabel: agencyHit.shortLabel,
      label: agencyHit.label,
      shortLabel: agencyHit.shortLabel,
      paradigm: 'ALL',
      hint: agencyHit.hint,
    }
  }
  const canonical = LEGACY_CODE_ALIASES[upper] || upper
  const hit = REGULATOR_PROJECTS.find((item) => item.code === canonical || item.code === clean)
  if (hit) {
    const ag = agencyMeta(hit.agency)
    return {
      ...hit,
      agencyLabel: ag.label,
      agencyShortLabel: ag.shortLabel,
      fullLabel: `${ag.shortLabel} · ${hit.label}`,
    }
  }
  const ag = agencyMeta(agencyCodeForReport(clean))
  const fallbackLabel = clean === 'CUSTOM' ? '其他监管报表' : clean
  return {
    code: clean,
    agency: ag.code,
    agencyLabel: ag.label,
    agencyShortLabel: ag.shortLabel,
    label: fallbackLabel,
    shortLabel: clean === 'CUSTOM' ? '其他报表' : clean,
    fullLabel: `${ag.shortLabel} · ${fallbackLabel}`,
    paradigm: 'RELATIONAL',
    hint: '自定义监管或地方派出机构特色报送报表',
  }
}

export function normalizeReportCode(code) {
  const clean = String(code || '').trim()
  if (!clean) return 'OTHER'
  const norm = /^[A-Za-z0-9_-]+$/.test(clean) ? clean.toUpperCase() : clean
  return LEGACY_CODE_ALIASES[norm] || norm
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

export function getSavedAgency() {
  try {
    return window.localStorage.getItem(ACTIVE_AGENCY_KEY) || ''
  } catch {
    return ''
  }
}

export function setSavedAgency(agency) {
  try {
    if (agency) window.localStorage.setItem(ACTIVE_AGENCY_KEY, agency)
    else window.localStorage.removeItem(ACTIVE_AGENCY_KEY)
  } catch {
    // ignore storage errors
  }
}
