import { createApp, h, ref } from 'vue'
import AuditStatusPanel from '../src/components/AuditStatusPanel.vue'
import { normalizeProjectStatus } from '../src/lib/contracts.js'
const project = ref(normalizeProjectStatus({ status: 'ready', revision: 1 }))
globalThis.setStatusFixture = (raw) => { project.value = normalizeProjectStatus(raw) }
createApp({ setup: () => () => h(AuditStatusPanel, { project: project.value }) }).mount('#app')
