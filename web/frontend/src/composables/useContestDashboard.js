import { onBeforeUnmount, ref } from 'vue'
import { Projects } from '../lib/api.js'

export function createContestDashboardController({ projectsApi = Projects } = {}) {
  const contestDashboard = ref(null)
  const contestDashboardLoading = ref(false)
  let disposed = false

  async function fetchContestDashboard(baseName) {
    if (!baseName || disposed) return null
    contestDashboardLoading.value = true
    try {
      contestDashboard.value = await projectsApi.contestDashboard(baseName)
      return contestDashboard.value
    } catch (_) {
      contestDashboard.value = null
      return null
    } finally {
      contestDashboardLoading.value = false
    }
  }

  function resetContestDashboard() {
    contestDashboard.value = null
    contestDashboardLoading.value = false
  }

  function stopContestDashboard() { disposed = true }

  return {
    contestDashboard,
    contestDashboardLoading,
    fetchContestDashboard,
    resetContestDashboard,
    stopContestDashboard,
  }
}

export function useContestDashboard(options) {
  const controller = createContestDashboardController(options)
  onBeforeUnmount(controller.stopContestDashboard)
  return controller
}
