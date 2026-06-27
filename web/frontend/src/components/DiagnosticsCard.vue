<template>
  <section class="diag panel" :class="severityClass">
    <div class="diag-top">
      <div>
        <div class="diag-kicker mono">DIAGNOSTICS</div>
        <div class="diag-title">{{ title }}</div>
        <div class="diag-summary">{{ summary }}</div>
      </div>
      <div class="diag-actions">
        <button
          v-for="action in actions"
          :key="action.id"
          class="btn btn-sm btn-ghost"
          @click="$emit('action', action.id)"
        >
          {{ actionLabel(action.id) }}
        </button>
      </div>
    </div>

    <div v-if="events.length" class="diag-events">
      <div v-for="event in events" :key="`${event.ts}-${event.type}`" class="diag-event mono">
        {{ event.type }} · {{ event.message }}
      </div>
    </div>
  </section>
</template>

<script>
import { actionLabel } from '../lib/diagnostics.js'

export default {
  name: 'DiagnosticsCard',
  props: {
    diagnostics: { type: Object, required: true },
  },
  emits: ['action'],
  computed: {
    title() {
      return this.diagnostics?.status?.reason_summary || '当前无诊断阻塞'
    },
    summary() {
      return this.diagnostics?.status?.reason_code || 'runner 未报告诊断原因'
    },
    actions() {
      return this.diagnostics?.actions || []
    },
    events() {
      return this.diagnostics?.events || []
    },
    severityClass() {
      const code = this.diagnostics?.status?.reason_code
      return {
        'is-warn': code === 'NO_LOG_PROGRESS' || code === 'LOCK_STALE_RECLAIMED',
        'is-block': code === 'AWAITING_STEP8_5' || code === 'VERIFY_OUTPUT_FAILED' || code === 'CONSULTATION_PENDING',
      }
    },
  },
  methods: { actionLabel },
}
</script>

<style scoped>
.diag { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; border-left: 3px solid var(--line); }
.diag.is-warn { border-left-color: var(--amber); background: var(--amber-dim); }
.diag.is-block { border-left-color: var(--bad); background: color-mix(in srgb, var(--bad-dim) 65%, transparent); }
.diag-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.diag-kicker { font-size: 10px; letter-spacing: 0.1em; color: var(--ink-3); }
.diag-title { font-size: 14px; font-weight: 700; color: var(--ink); margin-top: 4px; }
.diag-summary { font-size: 12px; color: var(--ink-2); margin-top: 4px; }
.diag-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.diag-events { display: flex; flex-direction: column; gap: 6px; }
.diag-event { font-size: 11px; color: var(--ink-2); padding: 7px 9px; border-radius: var(--r-sm); background: var(--panel-2); }
@media (max-width: 720px) {
  .diag-top { flex-direction: column; }
  .diag-actions { justify-content: flex-start; }
}
</style>
