const BUILD_ENV = import.meta.env ?? Object.freeze({})

/**
 * Phase 6 UI activation is deliberately strict.  Truthy strings, query
 * parameters, local storage, and runtime payloads cannot turn the feature on.
 */
export function phase6FullShadowEnabled(environment = BUILD_ENV) {
  return environment?.VITE_PHASE6_FULL_SHADOW_ENABLED === 'true'
}

export const PHASE6_FULL_SHADOW_UI_ENABLED = phase6FullShadowEnabled()

