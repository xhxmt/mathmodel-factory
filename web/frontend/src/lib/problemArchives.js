function runTimestamp(project) {
  const raw = project?.last_updated
  if (raw === undefined || raw === null || raw === '') return 0
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  const parsed = Date.parse(String(raw).replace(' ', 'T'))
  return Number.isFinite(parsed) ? parsed : 0
}
function archiveKey(project) {
  const explicit = String(project?.problem_key || '').trim()
  return explicit || `project:${String(project?.base_name || '')}`
}

function archiveTitle(project) {
  return String(project?.problem_title || project?.base_name || '未命名题目').trim()
}

function matchesStatus(project, filter) {
  if (!filter || filter === 'all') return true
  if (filter === 'running') return Boolean(project?.is_running) || project?.status === 'running'
  return project?.status === filter
}

export function buildProblemArchives(projects) {
  const groups = new Map()
  for (const project of Array.isArray(projects) ? projects : []) {
    if (!project || !project.base_name) continue
    const key = archiveKey(project)
    if (!groups.has(key)) groups.set(key, { key, title: archiveTitle(project), runs: [] })
    groups.get(key).runs.push(project)
  }

  return [...groups.values()]
    .map((archive) => {
      const runs = [...archive.runs].sort((left, right) => (
        runTimestamp(right) - runTimestamp(left)
        || String(right.base_name).localeCompare(String(left.base_name))
      ))
      const latest = runs[0]
      return {
        ...archive,
        title: archiveTitle(latest) || archive.title,
        runs,
        latest,
        run_count: runs.length,
        completed_count: runs.filter((run) => run.status === 'completed').length,
        running_count: runs.filter((run) => run.is_running || run.status === 'running').length,
        needs_attention_count: runs.filter((run) => run.consultation_pending || run.selection_pending).length,
      }
    })
    .sort((left, right) => (
      runTimestamp(right.latest) - runTimestamp(left.latest)
      || left.title.localeCompare(right.title, 'zh-CN')
    ))
}

export function filterProblemArchives(archives, { query = '', statusFilter = 'all' } = {}) {
  const needle = String(query || '').trim().toLowerCase()
  return (Array.isArray(archives) ? archives : []).filter((archive) => {
    if (needle) {
      const haystack = [archive.title, archive.key, ...archive.runs.map((run) => run.base_name)]
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(needle)) return false
    }
    return archive.runs.some((run) => matchesStatus(run, statusFilter))
  })
}
