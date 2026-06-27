import { computed, ref } from 'vue'
import { Projects } from '../lib/api.js'

const projects = ref([])
const loading = ref(true)
const selectedBase = ref(null)
const query = ref('')
const statusFilter = ref('all')

const filterChips = [
  { key: 'all', label: '全部' },
  { key: 'running', label: '运行中' },
  { key: 'completed', label: '已完成' },
  { key: 'paused', label: '已暂停' },
]

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

function fp(p) {
  return `${p.status}|${p.current_step}|${p.progress_percent}|${p.pid}|${p.consultation_pending}|${p.consultation_gate}|${p.last_updated}`
}

let lastListFp = ''
let awaitingSeen = null

function notifyNewlyAwaiting(list, notify) {
  const nowAwaiting = list.filter((p) => p.consultation_pending).map((p) => p.base_name)
  if (awaitingSeen === null) {
    awaitingSeen = new Set(nowAwaiting)
    return
  }
  for (const baseName of nowAwaiting) {
    if (!awaitingSeen.has(baseName)) notify(baseName)
  }
  awaitingSeen = new Set(nowAwaiting)
}

function patchProject(newProject, notify) {
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
  const agg = list.map(fp).join('\u0001')
  if (awaitingSeen === null) {
    projects.value = list
    lastListFp = agg
    awaitingSeen = new Set(list.filter((p) => p.consultation_pending).map((p) => p.base_name))
    return
  }
  if (agg === lastListFp) return
  lastListFp = agg
  const cur = projects.value
  const byBase = new Map(list.map((p) => [p.base_name, p]))
  for (const newProject of list) {
    const idx = cur.findIndex((p) => p.base_name === newProject.base_name)
    if (idx === -1) cur.push(newProject)
    else if (fp(cur[idx]) !== fp(newProject)) Object.assign(cur[idx], newProject)
  }
  const next = cur.filter((p) => byBase.has(p.base_name))
  if (next.length !== cur.length) projects.value = next
  notifyNewlyAwaiting(list, notify)
}

async function fetchProjects(notify = () => {}) {
  try {
    applyProjects(await Projects.list(), notify)
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

export function useProjects() {
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
