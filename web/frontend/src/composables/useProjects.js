import { computed, ref } from 'vue'
import { Projects } from '../lib/api.js'
import { buildProblemArchives, filterProblemArchives } from '../lib/problemArchives.js'
import { needsHuman } from '../lib/projectState.js'
import { normalizeProjectStatus } from '../lib/contracts.js'

const filterChips = [
  { key: 'all', label: '全部' },
  { key: 'running', label: '运行中' },
  { key: 'completed', label: '已完成' },
  { key: 'paused', label: '已暂停' },
  { key: 'interrupted', label: '已中断' },
  { key: 'failed', label: '失败' },
  { key: 'retrying', label: '重试中' },
]

function fp(p) {
  // Evidence/currentness can change without a workflow revision. Compare the full view.
  return JSON.stringify(p)
}

function notifyNewlyAwaiting(list, awaitingSeen, notify) {
  const nowAwaiting = list.filter(needsHuman).map((p) => p.base_name)
  for (const baseName of nowAwaiting) {
    if (!awaitingSeen.has(baseName)) notify(baseName)
  }
  return new Set(nowAwaiting)
}

export function createProjectStore({ projectsApi = Projects } = {}) {
  const projects = ref([])
  const loading = ref(true)
  const selectedBase = ref(null)
  const query = ref('')
  const statusFilter = ref('all')
  let lastListFp = ''
  let awaitingSeen = null

  const needsYou = computed(() => projects.value.filter(needsHuman))
  const others = computed(() => projects.value.filter((p) => !needsHuman(p)))
  const archives = computed(() => buildProblemArchives(others.value))
  const filteredOthers = computed(() => {
    const q = query.value.trim().toLowerCase()
    return others.value.filter((p) => {
      if (q && !p.base_name.toLowerCase().includes(q)) return false
      if (statusFilter.value === 'all') return true
      if (statusFilter.value === 'running') return p.is_running || p.status === 'running'
      return p.status === statusFilter.value
    })
  })
  const filteredArchives = computed(() => filterProblemArchives(archives.value, {
    query: query.value,
    statusFilter: statusFilter.value,
  }))
  const counts = computed(() => ({
    needs: needsYou.value.length,
    running: projects.value.filter((p) => p.is_running || p.status === 'running').length,
    completed: projects.value.filter((p) => p.status === 'completed').length,
    total: projects.value.length,
    problems: buildProblemArchives(projects.value).length,
  }))
  const selectedProject = computed(() => projects.value.find((p) => p.base_name === selectedBase.value) || null)

  function patchProject(newProject, notify = () => {}) {
    newProject = normalizeProjectStatus(newProject)
    const arr = projects.value
    const idx = arr.findIndex((p) => p.base_name === newProject.base_name)
    if (idx === -1) {
      projects.value = [...arr, newProject]
    } else if (fp(arr[idx]) !== fp(newProject)) {
      Object.assign(arr[idx], newProject)
    }
    lastListFp = projects.value.map(fp).join('\u0001')
    if (awaitingSeen !== null) {
      if (needsHuman(newProject) && !awaitingSeen.has(newProject.base_name)) {
        notify(newProject.base_name)
        awaitingSeen.add(newProject.base_name)
      } else if (!needsHuman(newProject)) {
        awaitingSeen.delete(newProject.base_name)
      }
    }
  }

  function applyProjects(list, notify = () => {}) {
    const safeList = Array.isArray(list) ? list.map(normalizeProjectStatus) : []
    const agg = safeList.map(fp).join('\u0001')
    if (awaitingSeen === null) {
      projects.value = safeList
      lastListFp = agg
      awaitingSeen = new Set(safeList.filter(needsHuman).map((p) => p.base_name))
      return
    }
    if (agg === lastListFp) return
    lastListFp = agg
    const cur = projects.value
    const byBase = new Map(safeList.map((p) => [p.base_name, p]))
    for (const newProject of safeList) {
      const idx = cur.findIndex((p) => p.base_name === newProject.base_name)
      if (idx === -1) cur.push(newProject)
      else if (fp(cur[idx]) !== fp(newProject)) Object.assign(cur[idx], newProject)
    }
    const next = cur.filter((p) => byBase.has(p.base_name))
    if (next.length !== cur.length) projects.value = next
    awaitingSeen = notifyNewlyAwaiting(safeList, awaitingSeen, notify)
  }

  async function fetchProjects(notify = () => {}) {
    try {
      applyProjects(await projectsApi.list(), notify)
    } finally {
      loading.value = false
    }
  }

  function openProject(project) {
    selectedBase.value = project.base_name
  }

  function openByBase(baseName) {
    selectedBase.value = baseName
  }

  function closeWorkspace() {
    selectedBase.value = null
  }

  function resetProjects() {
    projects.value = []
    loading.value = true
    selectedBase.value = null
    query.value = ''
    statusFilter.value = 'all'
    lastListFp = ''
    awaitingSeen = null
  }

  return {
    projects,
    loading,
    selectedBase,
    query,
    statusFilter,
    filterChips,
    needsYou,
    others,
    archives,
    filteredOthers,
    filteredArchives,
    counts,
    selectedProject,
    fetchProjects,
    applyProjects,
    patchProject,
    openProject,
    openByBase,
    closeWorkspace,
    resetProjects,
  }
}

const defaultProjectStore = createProjectStore()

export function useProjects() {
  return defaultProjectStore
}
