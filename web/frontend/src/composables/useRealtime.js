import { ref } from 'vue'
import { authWsTicket } from '../lib/api.js'
import { parseRealtimeMessage } from '../lib/realtime.js'

const wsConnected = ref(false)

export function buildWsUrl(ticket) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
}

export function useRealtime() {
  let ws = null
  let reconnect = null
  let reconnectDelay = 1000
  let intentionalClose = false
  const BACKOFF_MAX = 15000

  async function connect(onMessage) {
    const { ticket } = await authWsTicket()
    const connectUrl = buildWsUrl(ticket)

    ws = new WebSocket(connectUrl)
    ws.onopen = () => {
      wsConnected.value = true
      reconnectDelay = 1000
    }
    ws.onmessage = (ev) => {
      const message = parseRealtimeMessage(ev.data)
      if (message !== null) onMessage(message)
    }
    ws.onclose = () => {
      wsConnected.value = false
      if (intentionalClose) {
        intentionalClose = false
        return
      }
      reconnect = setTimeout(() => {
        connect(onMessage)
      }, reconnectDelay)
      reconnectDelay = Math.min(reconnectDelay * 2, BACKOFF_MAX)
    }
    ws.onerror = () => {
      try {
        ws.close()
      } catch (error) {
        // ignore close failures
      }
    }
  }

  function close() {
    intentionalClose = true
    if (ws) ws.close()
    if (reconnect) clearTimeout(reconnect)
    ws = null
    reconnect = null
    wsConnected.value = false
  }

  return { wsConnected, connect, close }
}
