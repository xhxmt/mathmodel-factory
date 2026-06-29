import { computed, ref } from 'vue'
import { Projects } from '../lib/api.js'

const filterChips = [
  { key: 'all', label: '全部' },
  { key: 'running', label: '运行中' },
  { key: 'completed', label: '已完成' },
  { key: 'paused', label: '已暂停' },
]

function fp(p) {
  return `${p.status}|${p.current_step}|${p.progress_percent}|${p.pid}|${p.consultation_pending}|${p.consultation_gate}|${p.last_updated}`
}

function notifyNewlyAwaiting(list, awaitingSeen, notify) {
  const nowAwaiting = list.filter((p) => p.consultation_pending).map((p) => p.base_name)
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

  const needsYou = computed(() => projects.value.filter((p) => p.consultation_pending))
  const others = computed(() => projects.value.filter((p) => !p.consultation_pending))
  const filteredOthers = computed(() => {
    const q = query.value.trim().toLowerCase()
    return others.value.filter((p) => {
      if (q && !p.base_name.toLowerCase().includes(q)) return false
      if (statusFilter.value === 'all') return true
      if (statusFilter.value === 'running') return p.is_running || p.status === 'running'
      return p.status === statusFilter.value
    })
  })
  const counts = computed(() => ({
    needs: needsYou.value.length,
    running: projects.value.filter((p) => p.is_running || p.status === 'running').length,
    completed: projects.value.filter((p) => p.status === 'completed').length,
    total: projects.value.length,
  }))
  const selectedProject = computed(() => projects.value.find((p) => p.base_name === selectedBase.value) || null)

  function patchProject(newProject, notify = () => {}) {
    const arr = projects.value
    const idx = arr.findIndex((p) => p.base_name === newProject.base_name)
    if (idx === -1) {
      projects.value = [...arr, newProject]
    } else if (fp(arr[idx]) !== fp(newProject)) {
      Object.assign(arr[idx], newProject)
    }
    lastListFp = projects.value.map(fp).join('\u0001')
    if (awaitingSeen !== null) {
      if (newProject.consultation_pending && !awaitingSeen.has(newProject.base_name)) {
        notify(newProject.base_name)
        awaitingSeen.add(newProject.base_name)
      } else if (!newProject.consultation_pending) {
        awaitingSeen.delete(newProject.base_name)
      }
    }
  }

  function applyProjects(list, notify = () => {}) {
    const safeList = Array.isArray(list) ? list : []
    const agg = safeList.map(fp).join('\u0001')
    if (awaitingSeen === null) {
      projects.value = safeList
      lastListFp = agg
      awaitingSeen = new Set(safeList.filter((p) => p.consultation_pending).map((p) => p.base_name))
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
    filteredOthers,
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
