import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { createPackage, deletePackage, listPackages } from '../api/catalog.js'
import {
  REGULATOR_PROJECTS,
  getSavedPackageId,
  getSavedRegulator,
  regulatorMeta,
  setSavedPackageId,
  setSavedRegulator,
} from '../utils/activeDocument.js'

const WorkspaceContext = createContext({
  packages: [],
  loading: true,
  activeRegulator: 'ALL',
  activePackageId: '',
  activePackage: null,
  packagesInActiveRegulator: [],
  regulatorGroups: [],
  selectRegulator: () => {},
  selectPackage: () => {},
  refreshPackages: async () => [],
  createProjectPackage: async () => null,
  deleteProjectPackage: async () => {},
})

function normalizeRegKey(code) {
  const clean = String(code || '').trim()
  if (!clean) return 'CUSTOM'
  return /^[A-Za-z0-9_-]+$/.test(clean) ? clean.toUpperCase() : clean
}

export function WorkspaceProvider({ children }) {
  const [packages, setPackages] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeRegulator, setActiveRegulatorState] = useState(() => getSavedRegulator() || 'ALL')
  const [activePackageId, setActivePackageIdState] = useState(() => getSavedPackageId() || '')

  const refreshPackages = useCallback(async () => {
    try {
      const list = await listPackages()
      setPackages(Array.isArray(list) ? list : [])
      setActivePackageIdState((prevId) => {
        if (prevId && list.some((p) => p.id === prevId)) return prevId
        const savedPkg = getSavedPackageId()
        if (savedPkg && list.some((p) => p.id === savedPkg)) return savedPkg
        const firstId = list[0]?.id || ''
        if (firstId) setSavedPackageId(firstId)
        return firstId
      })
      return list
    } catch {
      return []
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshPackages()
  }, [refreshPackages])

  const activePackage = useMemo(
    () => packages.find((pkg) => pkg.id === activePackageId) || null,
    [packages, activePackageId],
  )

  useEffect(() => {
    if (activePackage && activeRegulator !== 'ALL') {
      const pkgReg = normalizeRegKey(activePackage.regulator)
      if (pkgReg !== normalizeRegKey(activeRegulator)) {
        setActiveRegulatorState(pkgReg)
        setSavedRegulator(pkgReg)
      }
    }
  }, [activePackage])

  const regulatorGroups = useMemo(() => {
    const counts = new Map()
    for (const pkg of packages) {
      const key = normalizeRegKey(pkg.regulator)
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

  const packagesInActiveRegulator = useMemo(() => {
    if (!activeRegulator || activeRegulator === 'ALL') return packages
    const target = normalizeRegKey(activeRegulator)
    return packages.filter((pkg) => normalizeRegKey(pkg.regulator) === target)
  }, [packages, activeRegulator])

  const selectPackage = useCallback(
    (nextPackageId, { syncRegulator = true } = {}) => {
      const cleanId = nextPackageId || ''
      setActivePackageIdState(cleanId)
      setSavedPackageId(cleanId)
      if (syncRegulator && cleanId) {
        const hit = packages.find((p) => p.id === cleanId)
        if (hit) {
          const reg = normalizeRegKey(hit.regulator)
          setActiveRegulatorState(reg)
          setSavedRegulator(reg)
        }
      }
    },
    [packages],
  )

  const selectRegulator = useCallback(
    (nextRegulator) => {
      const norm = nextRegulator === 'ALL' ? 'ALL' : normalizeRegKey(nextRegulator)
      setActiveRegulatorState(norm)
      setSavedRegulator(norm)
      if (norm === 'ALL') return
      const matches = packages.filter((pkg) => normalizeRegKey(pkg.regulator) === norm)
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

  const createProjectPackage = useCallback(
    async (payload) => {
      const created = await createPackage(payload)
      setPackages((prev) => [created, ...prev])
      const reg = normalizeRegKey(created.regulator)
      setActiveRegulatorState(reg)
      setSavedRegulator(reg)
      setActivePackageIdState(created.id)
      setSavedPackageId(created.id)
      return created
    },
    [],
  )

  const deleteProjectPackage = useCallback(
    async (packageId) => {
      await deletePackage(packageId)
      const list = await refreshPackages()
      if (packageId === activePackageId) {
        const nextId = list[0]?.id || ''
        setActivePackageIdState(nextId)
        setSavedPackageId(nextId)
      }
    },
    [activePackageId, refreshPackages],
  )

  const value = useMemo(
    () => ({
      packages,
      loading,
      activeRegulator,
      activePackageId,
      activePackage,
      packagesInActiveRegulator,
      regulatorGroups,
      selectRegulator,
      selectPackage,
      refreshPackages,
      createProjectPackage,
      deleteProjectPackage,
    }),
    [
      packages,
      loading,
      activeRegulator,
      activePackageId,
      activePackage,
      packagesInActiveRegulator,
      regulatorGroups,
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
