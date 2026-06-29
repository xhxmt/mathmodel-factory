import { computed, onBeforeUnmount, ref } from 'vue'
import { Projects } from '../lib/api.js'
import { createProjectPollingController } from './useProjectPolling.js'

function trimTail(lines) {
  const raw = Array.isArray(lines) ? lines.slice() : []
  while (raw.length && raw[raw.length - 1] === '') raw.pop()
  return raw
}

function findTailOverlap(buffer, raw) {
  const bufTexts = buffer.map((line) => line.text)
  let overlap = 0
  if (!bufTexts.length) return overlap
  const maxOverlap = Math.min(raw.length, bufTexts.length, 400)
  for (let k = maxOverlap; k > 0; k--) {
    let ok = true
    for (let i = 0; i < k; i++) {
      if (raw[i] !== bufTexts[bufTexts.length - k + i]) {
        ok = false
        break
      }
    }
    if (ok) {
      overlap = k
      break
    }
  }
  return overlap
}

export function logLevel(text) {
  if (/\b(error|fail(ed)?|traceback|exception|fatal)\b|❌/i.test(text)) return 'err'
  if (/\b(warn(ing)?)\b|⚠/i.test(text)) return 'warn'
  if (/\b(ok|success|done|completed|pass(ed)?|converged)\b|✓|✅/i.test(text)) return 'ok'
  if (/^\s*[>$#]/.test(text) || /\b(step|info)\b/i.test(text)) return 'info'
  return ''
}

export function createProjectLogsController({
  projectsApi = Projects,
  schedule = true,
  documentRef = typeof document !== 'undefined' ? document : null,
} = {}) {
  const lines = ref([])
  const file = ref('')
  const loading = ref(false)
  const following = ref(true)
  const query = ref('')
  const wrap = ref(false)
  const atBottom = ref(true)
  let seq = 0
  let lastSig = ''
  let lastFile = ''
  let abortController = null
  const polling = createProjectPollingController({ intervalMs: 3000, documentRef })

  const filtered = computed(() => {
    const q = query.value.trim().toLowerCase()
    if (!q) return lines.value
    return lines.value.filter((line) => line.text.toLowerCase().includes(q))
  })

  function abortFetch() {
    if (abortController) {
      try { abortController.abort() } catch (error) { /* ignore */ }
      abortController = null
    }
  }

  async function fetchNow(baseName, silent = false) {
    if (!baseName) return
    if (!silent) loading.value = true
    abortFetch()
    const ac = new AbortController()
    abortController = ac
    try {
      const data = await projectsApi.logs(baseName, 400, ac.signal)
      if (abortController === ac) abortController = null
      const raw = trimTail(data?.logs)
      const nextFile = data?.file || ''
      const sig = `${nextFile}\u0001${raw.length}\u0001${raw[0] ?? ''}\u0001${raw[raw.length - 1] ?? ''}`
      if (sig === lastSig && nextFile === lastFile) return
      lastSig = sig
      lastFile = nextFile
      file.value = nextFile

      const overlap = findTailOverlap(lines.value, raw)
      if (overlap === 0) {
        lines.value = raw.map((text) => ({ n: ++seq, text }))
      } else {
        const fresh = raw.slice(overlap)
        if (fresh.length) {
          const cap = 5000
          const next = lines.value.concat(fresh.map((text) => ({ n: ++seq, text })))
          if (next.length > cap) next.splice(0, next.length - cap)
          lines.value = next
        }
      }
    } catch (error) {
      if (error?.code !== 'ERR_CANCELED') return
    } finally {
      if (!silent) loading.value = false
    }
  }

  function resetLogs() {
    abortFetch()
    lines.value = []
    file.value = ''
    loading.value = false
    seq = 0
    lastSig = ''
    lastFile = ''
  }

  function stopPolling() {
    polling.stopPolling()
    abortFetch()
  }

  function startPolling(baseNameGetter) {
    if (!schedule) return
    polling.startPolling(
      () => fetchNow(baseNameGetter(), true),
      {
        shouldRun: () => following.value,
        onHidden: abortFetch,
        onVisible: () => fetchNow(baseNameGetter(), true),
      },
    )
  }

  return {
    lines,
    file,
    loading,
    following,
    query,
    wrap,
    atBottom,
    filtered,
    fetchNow,
    resetLogs,
    startPolling,
    stopPolling,
    level: logLevel,
  }
}

export function useProjectLogs(options) {
  const controller = createProjectLogsController(options)
  onBeforeUnmount(controller.stopPolling)
  return controller
}
