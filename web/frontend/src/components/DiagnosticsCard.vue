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
      <div class="diag-section-title mono">AUDIT TIMELINE · {{ coordinate }}</div>
      <div v-for="event in events" :key="event.event_id || event.revision" class="diag-event mono">
        r{{ event.revision }} · {{ event.type }} · {{ event.message }}
        <template v-if="event.subject_stage || event.result_stage">
          · {{ eventCoordinate(event, 'subject') }} → {{ eventCoordinate(event, 'result') }}
        </template>
      </div>
    </div>
    <div v-if="evidence.length" class="diag-events">
      <div class="diag-section-title mono">EVIDENCE</div>
      <div v-for="item in evidence" :key="item.path || JSON.stringify(item)" class="diag-event mono">
        {{ item.path || item }}<template v-if="item.revision !== undefined"> · r{{ item.revision }}</template>
      </div>
    </div>
    <div v-if="recovery" class="diag-recovery">
      <div class="diag-section-title mono">RECOVERY STATUS</div>
      <div class="diag-event mono">
        {{ recovery.canonical_type }}
        <template v-if="recovery.decision"> · {{ recovery.decision }}</template>
        <template v-if="recovery.resume_after_step !== null && recovery.resume_after_step !== undefined">
          · resume after Step {{ recovery.resume_after_step }}
        </template>
        <template v-if="recovery.recovery_target?.stage">
          · Stage {{ recovery.recovery_target.stage }}
        </template>
        <template v-if="recovery.recovery_target?.subtask">
          / {{ recovery.recovery_target.subtask }}
        </template>
        <template v-if="recovery.invalidated_checkpoints?.length">
          · invalidate {{ recovery.invalidated_checkpoints.length }} checkpoint(s)
        </template>
      </div>
      <div v-if="recovery.next_task" class="diag-event mono">
        NEXT · {{ recovery.next_task }}
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
    recovery() {
      return this.diagnostics?.recovery?.latest || null
    },
    evidence() {
      return Array.isArray(this.diagnostics?.status?.evidence)
        ? this.diagnostics.status.evidence
        : []
    },
    coordinate() {
      const status = this.diagnostics?.status || {}
      const parts = []
      if (status.current_stage) parts.push(`Stage ${status.current_stage}`)
      if (status.current_subtask) parts.push(status.current_subtask)
      if (status.current_step !== null && status.current_step !== undefined) parts.push(`Step ${status.current_step}`)
      return parts.join(' / ') || 'project'
    },
    severityClass() {
      const code = this.diagnostics?.status?.reason_code
      return {
        'is-warn': code === 'NO_LOG_PROGRESS' || code === 'LOCK_STALE_RECLAIMED',
        'is-block': code === 'AWAITING_STEP8_5' || code === 'VERIFY_OUTPUT_FAILED' || code === 'CONSULTATION_PENDING' || code === 'HUMAN_DECISION_REQUIRED' || code === 'ORPHANED_DECISION_ARTIFACT' || code === 'DECISION_RECEIPT_MISMATCH' || code === 'WORKFLOW_REPLAY_MISMATCH' || String(code || '').startsWith('PERMANENT_'),
      }
    },
  },
  methods: {
    actionLabel,
    eventCoordinate(event, prefix) {
      const parts = []
      if (event?.[`${prefix}_stage`]) parts.push(`Stage ${event[`${prefix}_stage`]}`)
      if (event?.[`${prefix}_subtask`]) parts.push(event[`${prefix}_subtask`])
      if (event?.[`${prefix}_step`] !== null && event?.[`${prefix}_step`] !== undefined) parts.push(`Step ${event[`${prefix}_step`]}`)
      return parts.join('/') || 'project'
    },
  },
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
.diag-recovery { display: flex; flex-direction: column; gap: 6px; }
.diag-section-title { font-size: 10px; letter-spacing: 0.08em; color: var(--ink-3); }
.diag-event { font-size: 11px; color: var(--ink-2); padding: 7px 9px; border-radius: var(--r-sm); background: var(--panel-2); }
@media (max-width: 720px) {
  .diag-top { flex-direction: column; }
  .diag-actions { justify-content: flex-start; }
}
</style>
