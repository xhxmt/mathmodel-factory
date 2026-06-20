// Global toast store (module-singleton state) + desktop notification helper.
import { ref } from 'vue'

const toasts = ref([])
let seq = 0

function dismiss(id) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

function push(t) {
  const id = ++seq
  const toast = {
    id,
    type: t.type || 'info', // info | ok | bad | amber
    title: t.title || '',
    message: t.message || '',
    timeout: t.timeout ?? 4800,
  }
  toasts.value.push(toast)
  if (toast.timeout) setTimeout(() => dismiss(id), toast.timeout)
  return id
}

export function useToasts() {
  return {
    toasts,
    push,
    dismiss,
    success: (message, title = '') => push({ type: 'ok', message, title }),
    error: (message, title = '') => push({ type: 'bad', message, title, timeout: 7000 }),
    warn: (message, title = '') => push({ type: 'amber', message, title }),
    info: (message, title = '') => push({ type: 'info', message, title }),
  }
}

// Best-effort desktop notification (asks permission lazily).
export function notifyDesktop(title, body) {
  if (!('Notification' in window)) return
  if (Notification.permission === 'granted') {
    try { new Notification(title, { body }) } catch (e) { /* ignore */ }
  } else if (Notification.permission !== 'denied') {
    Notification.requestPermission().then((p) => {
      if (p === 'granted') { try { new Notification(title, { body }) } catch (e) { /* ignore */ } }
    })
  }
}
