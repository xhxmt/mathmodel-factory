// Shared model registry + per-step config cache (module singleton).
// Mirrors useTheme.js / useToasts.js: module-scope ref + mutators, one instance
// shared by every caller. Lets ProjectWorkspace and ModelManager share a single
// /api/models fetch, and lets the WS `models_updated` message invalidate it once.
import { ref } from 'vue'
import { Models } from '../lib/api.js'

const models = ref(null) // { registry, config, agentic_backends, valid_backends }
let loaded = false
let inFlight = null // shared promise → dedupes concurrent load() calls

async function load(force = false) {
  if (!force && loaded && models.value) return models.value
  if (inFlight) return inFlight
  inFlight = (async () => {
    try {
      const d = await Models.get()
      models.value = d
      loaded = true
      return d
    } finally {
      inFlight = null
    }
  })()
  return inFlight
}

// Called on WS `models_updated` and after any save. Refetches immediately so
// already-mounted consumers (workspace + manager) re-render reactively.
function invalidate() {
  return load(true)
}

async function saveRegistry(list) {
  const r = await Models.saveRegistry(list)
  await invalidate()
  return r
}

async function saveConfig(scope, steps) {
  const r = await Models.saveConfig(scope, steps)
  await invalidate()
  return r
}

export function useModels() {
  return { models, load, invalidate, saveRegistry, saveConfig }
}
