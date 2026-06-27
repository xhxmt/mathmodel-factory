export function parseRealtimeMessage(payload) {
  if (typeof payload !== 'string') return null
  try {
    return JSON.parse(payload)
  } catch {
    return null
  }
}
