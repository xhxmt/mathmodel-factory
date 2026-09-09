const PREVIOUS_KEYS = new Set(['ArrowLeft', 'ArrowUp'])
const NEXT_KEYS = new Set(['ArrowRight', 'ArrowDown'])

/**
 * Return the next index for the Phase 6 action-center roving tab stop.
 * Unknown keys deliberately return null so the panel never captures normal
 * browser or assistive-technology keyboard input.
 */
export function nextPhase6ActionIndex(key, currentIndex, actionSource) {
  const enabledIndices = Array.isArray(actionSource)
    ? actionSource.flatMap((action, index) => (action?.disabled === true ? [] : [index]))
    : Number.isSafeInteger(actionSource) && actionSource > 0
      ? Array.from({ length: actionSource }, (_value, index) => index)
      : []
  if (enabledIndices.length === 0) return null
  const currentPosition = enabledIndices.indexOf(currentIndex)
  const position = currentPosition >= 0 ? currentPosition : 0
  if (key === 'Home') return enabledIndices[0]
  if (key === 'End') return enabledIndices.at(-1)
  if (PREVIOUS_KEYS.has(key)) {
    return enabledIndices[(position - 1 + enabledIndices.length) % enabledIndices.length]
  }
  if (NEXT_KEYS.has(key)) return enabledIndices[(position + 1) % enabledIndices.length]
  return null
}
