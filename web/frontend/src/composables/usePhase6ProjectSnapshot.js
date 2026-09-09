import { onBeforeUnmount, ref } from 'vue'

import {
  buildPhase6SnapshotErrorViewModel,
  buildVerifiedPhase6SnapshotViewModel,
} from '../lib/phase6SnapshotProjection.js'
import { buildProjectSnapshotViewModel } from '../lib/projectSnapshotUi.js'
import { createPhase6SnapshotRequestCoordinator } from '../lib/phase6SnapshotClient.js'


export function createPhase6ProjectSnapshotController({
  request,
  logger = console,
  deadlineMs = import.meta.env?.VITE_PHASE6_SNAPSHOT_DEADLINE_MS,
  clock,
} = {}) {
  const coordinator = createPhase6SnapshotRequestCoordinator({ request, deadlineMs, clock })
  const viewModel = ref(buildProjectSnapshotViewModel({ state: 'loading' }))
  const loading = ref(false)
  let currentBaseName = ''
  let expectedRevision = null
  let controllerGeneration = 0

  function safeLog(label) {
    if (!logger || typeof logger.warn !== 'function') return
    try { logger.warn(label) } catch { /* diagnostics cannot alter UI state */ }
  }

  async function load(baseName, { retainCoordinate = true } = {}) {
    const loadGeneration = ++controllerGeneration
    const changedProject = baseName !== currentBaseName
    currentBaseName = baseName || ''
    if (changedProject || !retainCoordinate) expectedRevision = null
    if (!currentBaseName) {
      reset()
      return null
    }

    loading.value = true
    viewModel.value = buildProjectSnapshotViewModel({ state: 'loading' })
    const result = await coordinator.load(currentBaseName, { expectedRevision })
    if (loadGeneration !== controllerGeneration) return null
    if (!result.applied) {
      loading.value = false
      if (result.reason === 'aborted') {
        safeLog('Phase 6 snapshot request aborted')
        viewModel.value = buildPhase6SnapshotErrorViewModel(null)
        return viewModel.value
      }
      return null
    }
    loading.value = false
    if (result.error) {
      safeLog('Phase 6 snapshot request failed')
      viewModel.value = buildPhase6SnapshotErrorViewModel(result.error)
      return viewModel.value
    }

    const next = buildVerifiedPhase6SnapshotViewModel(result.payload, currentBaseName)
    viewModel.value = next
    expectedRevision = next.state === 'ready' ? next.revision : null
    return next
  }

  function reset() {
    controllerGeneration += 1
    coordinator.cancel('reset')
    currentBaseName = ''
    expectedRevision = null
    loading.value = false
    viewModel.value = buildProjectSnapshotViewModel({ state: 'loading' })
  }

  function cancel() {
    controllerGeneration += 1
    coordinator.cancel('user_cancel')
    loading.value = false
    viewModel.value = buildPhase6SnapshotErrorViewModel(null)
  }

  function stop() {
    controllerGeneration += 1
    coordinator.cancel('navigation')
    loading.value = false
  }

  return { viewModel, loading, load, reset, cancel, stop }
}

export function usePhase6ProjectSnapshot(options) {
  const controller = createPhase6ProjectSnapshotController(options)
  onBeforeUnmount(controller.stop)
  return controller
}
