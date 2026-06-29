import { onBeforeUnmount, ref } from 'vue'
import { Projects } from '../lib/api.js'

export function stepPayloadFingerprint(payload) {
  if (!payload) return ''
  const steps = Array.isArray(payload.steps) ? payload.steps : []
  const artifacts = steps.map((step) => {
    const items = Array.isArray(step.artifacts) ? step.artifacts : []
    const latest = items.reduce((mx, item) => Math.max(mx, item.mtime ? Date.parse(String(item.mtime).replace(' ', 'T')) : 0), 0)
    const totalSize = items.reduce((sum, item) => sum + (item.size || 0), 0)
    return `${items.length}/${latest}/${totalSize}`
  }).join(',')
  const gate = payload.editorial_gate
  return [
    payload.current_step,
    payload.verdict,
    payload.open_issues,
    payload.paper_available,
    gate?.ready,
    gate?.verdict,
    artifacts,
  ].join('|')
}

export function createProjectStepsController({ projectsApi = Projects } = {}) {
  const stepsData = ref(null)
  const loading = ref(false)
  let abortController = null
  let lastFingerprint = ''
  let inFlight = null

  function stopSteps() {
    if (abortController) {
      try { abortController.abort() } catch (error) { /* ignore */ }
      abortController = null
    }
    inFlight = null
  }

  function resetSteps() {
    stopSteps()
    stepsData.value = null
    lastFingerprint = ''
    loading.value = false
  }

  async function fetchSteps(baseName) {
    if (!baseName) return null
    if (inFlight) return inFlight
    if (abortController) {
      try { abortController.abort() } catch (error) { /* ignore */ }
    }
    const ac = new AbortController()
    abortController = ac
    loading.value = true
    inFlight = (async () => {
      try {
        const payload = await projectsApi.steps(baseName, ac.signal)
        if (abortController === ac) abortController = null
        const nextFingerprint = stepPayloadFingerprint(payload)
        if (nextFingerprint !== lastFingerprint) {
          lastFingerprint = nextFingerprint
          stepsData.value = payload
        }
        return stepsData.value
      } catch (error) {
        if (error?.code !== 'ERR_CANCELED') return stepsData.value
        return null
      } finally {
        if (inFlight) inFlight = null
        loading.value = false
      }
    })()
    return inFlight
  }

  return {
    stepsData,
    loading,
    fetchSteps,
    resetSteps,
    stopSteps,
    stepsFp: stepPayloadFingerprint,
  }
}

export function useProjectSteps(options) {
  const controller = createProjectStepsController(options)
  onBeforeUnmount(controller.stopSteps)
  return controller
}
