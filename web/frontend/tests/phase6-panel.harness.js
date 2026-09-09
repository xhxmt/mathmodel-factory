import { createApp, h, ref } from 'vue'

import Phase6ProjectSnapshotPanel from '../src/components/Phase6ProjectSnapshotPanel.vue'

const rawDeadlineMs = new URLSearchParams(globalThis.location.search).get('deadlineMs')
const parsedDeadlineMs = rawDeadlineMs === null ? undefined : Number(rawDeadlineMs)
const deadlineMs = Number.isSafeInteger(parsedDeadlineMs) && parsedDeadlineMs > 0
  ? parsedDeadlineMs
  : undefined

createApp({
  name: 'Phase6PanelBrowserHarness',
  setup() {
    const navigations = ref([])
    globalThis.__phase6Harness = {
      navigations,
    }
    return () => h('main', { 'data-testid': 'phase6-harness' }, [
      h('button', { type: 'button', 'data-testid': 'before-panel' }, 'before'),
      h(Phase6ProjectSnapshotPanel, {
        baseName: 'demo',
        deadlineMs,
        onNavigate(action) {
          navigations.value.push(action.id)
        },
      }),
      h('button', { type: 'button', 'data-testid': 'after-panel' }, 'after'),
      h('output', { 'data-testid': 'navigations' }, navigations.value.join(',')),
    ])
  },
}).mount('#app')
