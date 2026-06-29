import { onBeforeUnmount } from 'vue'

function defaultScheduler() {
  return {
    setInterval: (...args) => setInterval(...args),
    clearInterval: (id) => clearInterval(id),
  }
}

export function createProjectPollingController({
  intervalMs = 3000,
  backoffIntervalMs = null,
  backoffWhen = () => false,
  scheduler = defaultScheduler(),
  documentRef = typeof document !== 'undefined' ? document : null,
} = {}) {
  let timer = null
  let visibilityHandler = null

  function stopPolling() {
    if (timer) {
      scheduler.clearInterval(timer)
      timer = null
    }
    if (visibilityHandler && documentRef) {
      documentRef.removeEventListener('visibilitychange', visibilityHandler)
      visibilityHandler = null
    }
  }

  // When backoffWhen() is true (e.g. a live WS is pushing updates), slow the tick
  // down toward backoffIntervalMs without restarting the timer. We keep one timer
  // at the base interval and only actually fire on every Nth tick, where N is the
  // ratio between the backoff and base intervals. This is approximate by design —
  // it is a best-effort consolation cadence, the WS being the primary signal.
  const skipRatio = backoffIntervalMs && backoffIntervalMs > intervalMs
    ? Math.round(backoffIntervalMs / intervalMs)
    : 1
  let skipCount = 0

  function startPolling(
    tick,
    {
      shouldRun = () => true,
      backoffWhen: perTickBackoff = backoffWhen,
      onHidden = () => {},
      onVisible = tick,
    } = {},
  ) {
    stopPolling()
    skipCount = 0
    timer = scheduler.setInterval(() => {
      if (documentRef?.hidden) return
      if (!shouldRun()) return
      if (skipRatio > 1 && perTickBackoff()) {
        skipCount += 1
        if (skipCount < skipRatio) return
        skipCount = 0
      }
      tick()
    }, intervalMs)
    if (documentRef) {
      visibilityHandler = () => {
        if (documentRef.hidden) onHidden()
        else if (shouldRun()) onVisible()
      }
      documentRef.addEventListener('visibilitychange', visibilityHandler)
    }
  }

  return { startPolling, stopPolling }
}

export function useProjectPolling(options) {
  const controller = createProjectPollingController(options)
  onBeforeUnmount(controller.stopPolling)
  return controller
}
