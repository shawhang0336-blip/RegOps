import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { createPackage, deletePackage, listPackages } from '../api/catalog.js'
import {
  REGULATOR_PROJECTS,
  REGULATORY_AGENCIES,
  agencyCodeForReport,
  getSavedAgency,
  getSavedPackageId,
  getSavedRegulator,
  normalizeReportCode,
  regulatorMeta,
  setSavedAgency,
  setSavedPackageId,
  setSavedRegulator,
} from '../utils/activeDocument.js'

const WorkspaceContext = createContext({
  packages: [],
  loading: true,
  activeAgency: 'ALL',
  activeRegulator: 'ALL',
  activePackageId: '',
  activePackage: null,
  packagesInActiveRegulator: [],
  agencyGroups: [],
  regulatorGroups: [],
  reportsInActiveAgency: [],
  selectAgency: () => {},
  selectRegulator: () => {},
  selectPackage: () => {},
  refreshPackages: async () => [],
  createProjectPackage: async () => null,
  deleteProjectPackage: async () => {},
})

function pkgMatchesScope(pkg, agencyCode, reportCode) {
  const pkgReport = normalizeReportCode(pkg.regulator)
  const pkgAgency = agencyCodeForReport(pkgReport)
  if (reportCode && reportCode !== 'ALL') {
    return pkgReport === normalizeReportCode(reportCode)
  }
  if (agencyCode && agencyCode !== 'ALL') {
    return pkgAgency === agencyCode
  }
  return true
}

export function WorkspaceProvider({ children }) {
  const [packages, setPackages] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeRegulator, setActiveRegulatorState] = useState(() => {
    const saved = getSavedRegulator()
    if (!saved || saved === 'ALL') return 'ALL'
    return normalizeReportCode(saved)
  })
  const [activeAgency, setActiveAgencyState] = useState(() => {
    const savedAg = getSavedAgency()
    if (savedAg) return savedAg
    const savedRep = getSavedRegulator()
    if (savedRep && savedRep !== 'ALL') return agencyCodeForReport(savedRep)
    return ''
  })
  const [activePackageId, setActivePackageIdState] = useState(() => getSavedPackageId() || '')

  const refreshPackages = useCallback(async () => {
    try {
      const list = await listPackages()
      const safeList = Array.isArray(list) ? list : []
      setPackages(safeList)

      let resolvedPkgId = ''
      setActivePackageIdState((prevId) => {
        if (prevId && safeList.some((p) => p.id === prevId)) {
          resolvedPkgId = prevId
          return prevId
        }
        const savedPkg = getSavedPackageId()
        if (savedPkg && safeList.some((p) => p.id === savedPkg)) {
          resolvedPkgId = savedPkg
          return savedPkg
        }
        const firstId = safeList[0]?.id || ''
        resolvedPkgId = firstId
        if (firstId) setSavedPackageId(firstId)
        return firstId
      })

      // 首次访问（尚无本地机构偏好）时，默认定位到首个已有版本包的监管机构，直接呈现「机构 -> 报表 -> 版本包」清晰层级
      setActiveAgencyState((prevAgency) => {
        if (prevAgency) return prevAgency
        const firstPkg = safeList.find((p) => p.id === resolvedPkgId) || safeList[0]
        const initAgency = firstPkg ? agencyCodeForReport(firstPkg.regulator) : 'NFRA'
        setSavedAgency(initAgency)
        return initAgency
      })

      return safeList
    } catch {
      return []
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshPackages()
  }, [refreshPackages])

  const effectiveAgency = activeAgency || 'ALL'

  const activePackage = useMemo(
    () => packages.find((pkg) => pkg.id === activePackageId) || null,
    [packages, activePackageId],
  )

  const agencyGroups = useMemo(() => {
    const counts = new Map()
    for (const pkg of packages) {
      const ag = agencyCodeForReport(pkg.regulator)
      counts.set(ag, (counts.get(ag) || 0) + 1)
    }
    return REGULATORY_AGENCIES.map((item) => ({
      ...item,
      count: counts.get(item.code) || 0,
    }))
  }, [packages])

  const regulatorGroups = useMemo(() => {
    const counts = new Map()
    for (const pkg of packages) {
      const key = normalizeReportCode(pkg.regulator)
      counts.set(key, (counts.get(key) || 0) + 1)
    }
    const groups = REGULATOR_PROJECTS.map((item) => ({
      ...item,
      count: counts.get(item.code) || 0,
      builtin: true,
    }))
    const builtinSet = new Set(REGULATOR_PROJECTS.map((item) => item.code))
    for (const [key, count] of counts.entries()) {
      if (!builtinSet.has(key)) {
        groups.push({ ...regulatorMeta(key), count, builtin: false })
      }
    }
    return groups
  }, [packages])

  const reportsInActiveAgency = useMemo(() => {
    if (effectiveAgency === 'ALL') {
      // 全部机构视图下仅平铺已有版本包的报表（避免 16+ 项挤占多行），点击具体监管机构则展开该机构全部报表体系
      return regulatorGroups.filter((item) => item.count > 0 || item.code === activeRegulator)
    }
    return regulatorGroups.filter((item) => item.agency === effectiveAgency)
  }, [regulatorGroups, effectiveAgency, activeRegulator])

  const packagesInActiveRegulator = useMemo(
    () => packages.filter((pkg) => pkgMatchesScope(pkg, effectiveAgency, activeRegulator)),
    [packages, effectiveAgency, activeRegulator],
  )

  const selectPackage = useCallback(
    (nextPackageId, { syncRegulator = true } = {}) => {
      const cleanId = nextPackageId || ''
      setActivePackageIdState(cleanId)
      setSavedPackageId(cleanId)
      if (syncRegulator && cleanId) {
        const hit = packages.find((p) => p.id === cleanId)
        if (hit && !pkgMatchesScope(hit, effectiveAgency, activeRegulator)) {
          const rep = normalizeReportCode(hit.regulator)
          const ag = agencyCodeForReport(rep)
          setActiveAgencyState(ag)
          setSavedAgency(ag)
          setActiveRegulatorState(rep)
          setSavedRegulator(rep)
        }
      }
    },
    [packages, effectiveAgency, activeRegulator],
  )

  const selectAgency = useCallback(
    (nextAgency) => {
      const normAg = nextAgency === 'ALL' ? 'ALL' : String(nextAgency || 'NFRA').toUpperCase()
      setActiveAgencyState(normAg)
      setSavedAgency(normAg)
      setActiveRegulatorState('ALL')
      setSavedRegulator('ALL')
      const matches = packages.filter((pkg) => pkgMatchesScope(pkg, normAg, 'ALL'))
      if (matches.length > 0) {
        if (!matches.some((p) => p.id === activePackageId)) {
          setActivePackageIdState(matches[0].id)
          setSavedPackageId(matches[0].id)
        }
      } else {
        setActivePackageIdState('')
        setSavedPackageId('')
      }
    },
    [packages, activePackageId],
  )

  const selectRegulator = useCallback(
    (nextReport) => {
      const normRep = nextReport === 'ALL' ? 'ALL' : normalizeReportCode(nextReport)
      setActiveRegulatorState(normRep)
      setSavedRegulator(normRep)
      let targetAg = effectiveAgency
      if (normRep !== 'ALL') {
        targetAg = agencyCodeForReport(normRep)
        setActiveAgencyState(targetAg)
        setSavedAgency(targetAg)
      }
      const matches = packages.filter((pkg) => pkgMatchesScope(pkg, targetAg, normRep))
      if (matches.length > 0) {
        if (!matches.some((p) => p.id === activePackageId)) {
          setActivePackageIdState(matches[0].id)
          setSavedPackageId(matches[0].id)
        }
      } else {
        setActivePackageIdState('')
        setSavedPackageId('')
      }
    },
    [packages, effectiveAgency, activePackageId],
  )

  const createProjectPackage = useCallback(async (payload) => {
    const created = await createPackage(payload)
    setPackages((prev) => [created, ...prev])
    const rep = normalizeReportCode(created.regulator)
    const ag = agencyCodeForReport(rep)
    setActiveAgencyState(ag)
    setSavedAgency(ag)
    setActiveRegulatorState(rep)
    setSavedRegulator(rep)
    setActivePackageIdState(created.id)
    setSavedPackageId(created.id)
    return created
  }, [])

  const deleteProjectPackage = useCallback(
    async (packageId) => {
      await deletePackage(packageId)
      const list = await refreshPackages()
      if (packageId === activePackageId) {
        const scoped = list.filter((pkg) => pkgMatchesScope(pkg, effectiveAgency, activeRegulator))
        const nextId = scoped[0]?.id || list[0]?.id || ''
        setActivePackageIdState(nextId)
        setSavedPackageId(nextId)
      }
    },
    [activePackageId, effectiveAgency, activeRegulator, refreshPackages],
  )

  const value = useMemo(
    () => ({
      packages,
      loading,
      activeAgency: effectiveAgency,
      activeRegulator,
      activePackageId,
      activePackage,
      packagesInActiveRegulator,
      agencyGroups,
      regulatorGroups,
      reportsInActiveAgency,
      selectAgency,
      selectRegulator,
      selectPackage,
      refreshPackages,
      createProjectPackage,
      deleteProjectPackage,
    }),
    [
      packages,
      loading,
      effectiveAgency,
      activeRegulator,
      activePackageId,
      activePackage,
      packagesInActiveRegulator,
      agencyGroups,
      regulatorGroups,
      reportsInActiveAgency,
      selectAgency,
      selectRegulator,
      selectPackage,
      refreshPackages,
      createProjectPackage,
      deleteProjectPackage,
    ],
  )

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace() {
  return useContext(WorkspaceContext)
}
