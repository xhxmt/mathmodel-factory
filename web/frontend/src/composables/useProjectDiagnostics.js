import { onBeforeUnmount, ref } from 'vue'
import { Projects } from '../lib/api.js'

export function createProjectDiagnosticsController({ projectsApi = Projects } = {}) {
  const diagnostics = ref(null)
  const diagnosticsLoading = ref(false)
  let disposed = false

  async function fetchDiagnostics(baseName) {
    if (!baseName || disposed) return null
    diagnosticsLoading.value = true
    try {
      diagnostics.value = await projectsApi.diagnostics(baseName)
      return diagnostics.value
    } catch (error) {
      diagnostics.value = null
      return null
    } finally {
      diagnosticsLoading.value = false
    }
  }

  function resetDiagnostics() {
    diagnostics.value = null
    diagnosticsLoading.value = false
  }

  function stopDiagnostics() {
    disposed = true
  }

  return { diagnostics, diagnosticsLoading, fetchDiagnostics, resetDiagnostics, stopDiagnostics }
}

export function useProjectDiagnostics(options) {
  const controller = createProjectDiagnosticsController(options)
  onBeforeUnmount(controller.stopDiagnostics)
  return controller
}
